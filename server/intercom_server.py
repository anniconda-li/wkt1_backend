"""wkt-intercom-server WebSocket relay."""

from __future__ import annotations

import argparse
import asyncio
from collections import deque
import contextlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from server.protocol import (
    APP_INTERCOM_PKT_AUDIO,
    APP_INTERCOM_PKT_CHANNEL,
    APP_INTERCOM_PKT_HEARTBEAT,
    APP_INTERCOM_PKT_PTT_START,
    APP_INTERCOM_PKT_PTT_STOP,
    APP_INTERCOM_PKT_REGISTER,
    HEADER_LEN,
    PKT_TYPES,
    Packet,
    parse_packet,
)

DEFAULT_HOST = "0.0.0.0"
DEFAULT_WS_PORT = 18081
DEFAULT_AUDIO_TRACE_EVERY_N = 50
DEFAULT_WS_MAX_FRAME_BYTES = 65536
DEFAULT_SEND_QUEUE_MAX = 80
DEFAULT_SEND_TIMEOUT_SECONDS = 2.0
DEFAULT_LOG_STATS = True
DEFAULT_LOG_AUDIO_TRACE = False
DEFAULT_REALTIME_WINDOW_MS = 400
DEFAULT_STATS_INTERVAL_MS = 1000
SLOW_WARN_INTERVAL_MS = 1000
UINT32_MOD = 1 << 32
SEQ_HALF_RANGE = 1 << 31
PROJECT_ROOT = Path(__file__).resolve().parents[1]

STATE_TYPES = {
    APP_INTERCOM_PKT_REGISTER,
    APP_INTERCOM_PKT_CHANNEL,
    APP_INTERCOM_PKT_HEARTBEAT,
}
FORWARD_TYPES = {
    APP_INTERCOM_PKT_PTT_START,
    APP_INTERCOM_PKT_AUDIO,
    APP_INTERCOM_PKT_PTT_STOP,
}


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


load_env_file(PROJECT_ROOT / ".env")


def monotonic_ms() -> float:
    return time.monotonic() * 1000.0


def next_seq(seq: int) -> int:
    return (seq + 1) % UINT32_MOD


def seq_distance(seq: int, expected: int) -> int:
    return (seq - expected) % UINT32_MOD


def percentile_ms(values: list[float], pct: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = round((len(ordered) - 1) * pct)
    return int(round(ordered[index]))


def fmt_ms(value: float) -> int:
    return int(round(max(value, 0.0)))


@dataclass(frozen=True)
class OutboundItem:
    data: bytes
    packet_type: int
    source_device: str
    channel: int
    seq: int
    created_ms: float


@dataclass
class RxWindow:
    start_ms: float
    expected_seq: int | None = None
    last_arrival_ms: float | None = None
    audio: int = 0
    bytes_total: int = 0
    gap: int = 0
    dup: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    intervals_ms: list[float] = field(default_factory=list)

    def record(self, packet: Packet, now_ms: float) -> None:
        if self.audio == 0:
            self.first_seq = packet.seq
        self.audio += 1
        self.bytes_total += len(packet.payload)
        self.last_seq = packet.seq

        if self.last_arrival_ms is not None:
            self.intervals_ms.append(now_ms - self.last_arrival_ms)
        self.last_arrival_ms = now_ms

        if self.expected_seq is None:
            self.expected_seq = next_seq(packet.seq)
            return

        distance = seq_distance(packet.seq, self.expected_seq)
        if distance == 0:
            self.expected_seq = next_seq(packet.seq)
        elif distance < SEQ_HALF_RANGE:
            self.gap += distance
            self.expected_seq = next_seq(packet.seq)
        else:
            self.dup += 1

    def reset_window(self, now_ms: float) -> None:
        self.start_ms = now_ms
        self.audio = 0
        self.bytes_total = 0
        self.gap = 0
        self.dup = 0
        self.first_seq = None
        self.last_seq = None
        self.intervals_ms.clear()


@dataclass
class TxWindow:
    start_ms: float
    enqueue_audio: int = 0
    sent_audio: int = 0
    drop_old: int = 0
    drop_slow: int = 0
    q_max: int = 0
    send_ms: list[float] = field(default_factory=list)

    def reset_window(self, now_ms: float) -> None:
        self.start_ms = now_ms
        self.enqueue_audio = 0
        self.sent_audio = 0
        self.drop_old = 0
        self.drop_slow = 0
        self.q_max = 0
        self.send_ms.clear()


class OutboundQueue:
    """Per-device live send queue. It is not a retransmission cache."""

    def __init__(
        self,
        *,
        target_device: str,
        max_items: int,
        realtime_window_ms: int,
        log_func=print,
        on_drop=None,
    ) -> None:
        self.target_device = target_device
        self.max_items = max(max_items, 1)
        self.realtime_window_ms = max(realtime_window_ms, 1)
        self.log_func = log_func
        self.on_drop = on_drop
        self._items: deque[OutboundItem] = deque()
        self._condition = asyncio.Condition()
        self.drop_audio_count = 0
        self.drop_control_count = 0
        self._last_slow_warn_ms = 0.0

    async def put(self, item: OutboundItem) -> bool:
        async with self._condition:
            now_ms = monotonic_ms()
            self._drop_expired_audio_locked(now_ms)
            if len(self._items) >= self.max_items and not self._make_room_for(item, now_ms):
                return False
            self._items.append(item)
            self._condition.notify()
            return True

    async def get(self) -> OutboundItem:
        async with self._condition:
            while True:
                while not self._items:
                    await self._condition.wait()
                now_ms = monotonic_ms()
                self._drop_expired_audio_locked(now_ms)
                if self._items:
                    return self._items.popleft()

    def qsize(self) -> int:
        return len(self._items)

    def oldest_audio_age_ms(self, now_ms: float | None = None) -> int:
        now = monotonic_ms() if now_ms is None else now_ms
        for item in self._items:
            if item.packet_type == APP_INTERCOM_PKT_AUDIO:
                return fmt_ms(now - item.created_ms)
        return 0

    def _drop_expired_audio_locked(self, now_ms: float) -> None:
        oldest_before = self.oldest_audio_age_ms(now_ms)
        dropped: list[OutboundItem] = []
        index = 0
        while index < len(self._items):
            item = self._items[index]
            if (
                item.packet_type == APP_INTERCOM_PKT_AUDIO
                and now_ms - item.created_ms > self.realtime_window_ms
            ):
                dropped.append(item)
                del self._items[index]
                continue
            index += 1

        if dropped:
            for item in dropped:
                self._record_drop(item, "drop_slow", now_ms)
            first = dropped[0]
            self._warn_slow(
                now_ms,
                source_device=first.source_device,
                channel=first.channel,
                action="drop_old",
                drop=len(dropped),
                oldest_ms=oldest_before,
                first_seq=first.seq,
            )

    def _make_room_for(self, item: OutboundItem, now_ms: float) -> bool:
        oldest_before = self.oldest_audio_age_ms(now_ms)
        for index, queued in enumerate(self._items):
            if queued.packet_type == APP_INTERCOM_PKT_AUDIO:
                del self._items[index]
                self._record_drop(queued, "drop_old", now_ms)
                self._warn_slow(
                    now_ms,
                    source_device=queued.source_device,
                    channel=queued.channel,
                    action="drop_old",
                    drop=1,
                    oldest_ms=oldest_before,
                    first_seq=queued.seq,
                )
                return True

        if item.packet_type == APP_INTERCOM_PKT_AUDIO:
            self._record_drop(item, "drop_slow", now_ms)
            self._warn_slow(
                now_ms,
                source_device=item.source_device,
                channel=item.channel,
                action="drop_slow",
                drop=1,
                oldest_ms=oldest_before,
                first_seq=item.seq,
            )
            return False

        dropped = self._items.popleft()
        self.drop_control_count += 1
        self.log_func(
            f"intercom_slow level=warn target={self.target_device} "
            f"from={dropped.source_device} ch={dropped.channel} q={len(self._items)} "
            f"oldest_ms={oldest_before} action=drop_control drop=1 "
            f"keep_ms={self.realtime_window_ms} first_seq={dropped.seq}"
        )
        return True

    def _record_drop(self, item: OutboundItem, reason: str, now_ms: float) -> None:
        if item.packet_type == APP_INTERCOM_PKT_AUDIO:
            self.drop_audio_count += 1
        if self.on_drop is not None:
            self.on_drop(
                self.target_device,
                item,
                reason,
                len(self._items),
                self.oldest_audio_age_ms(now_ms),
            )

    def _warn_slow(
        self,
        now_ms: float,
        *,
        source_device: str,
        channel: int,
        action: str,
        drop: int,
        oldest_ms: int,
        first_seq: int,
    ) -> None:
        if now_ms - self._last_slow_warn_ms < SLOW_WARN_INTERVAL_MS:
            return
        self._last_slow_warn_ms = now_ms
        self.log_func(
            f"intercom_slow level=warn target={self.target_device} from={source_device} "
            f"ch={channel} q={len(self._items)} oldest_ms={oldest_ms} "
            f"action={action} drop={drop} keep_ms={self.realtime_window_ms} "
            f"first_seq={first_seq}"
        )


@dataclass
class IntercomConnection:
    device_id: str
    websocket: Any
    outbox: OutboundQueue | None = None
    channel: int = 1
    remote_ip: str = "-"
    remote_port: str = "-"
    connected_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    rx_audio_count: int = 0
    tx_audio_count: int = 0


class IntercomHub:
    """In-memory device table and WTK1 forwarding logic."""

    def __init__(
        self,
        *,
        log_func=print,
        audio_log_every: int = DEFAULT_AUDIO_TRACE_EVERY_N,
        send_queue_max: int = DEFAULT_SEND_QUEUE_MAX,
        send_timeout_seconds: float = DEFAULT_SEND_TIMEOUT_SECONDS,
        log_stats: bool = DEFAULT_LOG_STATS,
        log_audio_trace: bool = DEFAULT_LOG_AUDIO_TRACE,
        realtime_window_ms: int = DEFAULT_REALTIME_WINDOW_MS,
        stats_interval_ms: int = DEFAULT_STATS_INTERVAL_MS,
    ) -> None:
        self.log_func = log_func
        self.audio_trace_every = max(audio_log_every, 1)
        self.send_queue_max = max(send_queue_max, 1)
        self.send_timeout_seconds = max(send_timeout_seconds, 0.1)
        self.log_stats = log_stats
        self.log_audio_trace = log_audio_trace
        self.realtime_window_ms = max(realtime_window_ms, 1)
        self.stats_interval_ms = max(stats_interval_ms, 100)
        self.connections: dict[str, IntercomConnection] = {}
        self.rx_stats: dict[tuple[str, int], RxWindow] = {}
        self.tx_stats: dict[tuple[str, str, int], TxWindow] = {}

    async def add_connection(self, connection: IntercomConnection) -> None:
        if connection.outbox is None:
            connection.outbox = self._make_outbox(connection.device_id)
        old = self.connections.get(connection.device_id)
        self.connections[connection.device_id] = connection
        if old is not None and old is not connection:
            self.log_func(
                f"intercom_conn level=info event=replaced_old_connection "
                f"device={connection.device_id} ip={connection.remote_ip} "
                f"old_ip={old.remote_ip} active={len(self.connections)}"
            )
            with contextlib.suppress(Exception):
                await old.websocket.close(code=1000, reason="replaced")
        self.log_func(
            f"intercom_conn level=info event=connect device={connection.device_id} "
            f"ip={connection.remote_ip} port={connection.remote_port} "
            f"channel={connection.channel} active={len(self.connections)}"
        )

    def remove_connection(
        self,
        connection: IntercomConnection,
        *,
        close_code: Any = "-",
        reason: str = "",
    ) -> None:
        if self.connections.get(connection.device_id) is connection:
            self.connections.pop(connection.device_id, None)
            duration_ms = fmt_ms((time.monotonic() - connection.connected_at) * 1000.0)
            self.log_func(
                f"intercom_conn level=info event=disconnect device={connection.device_id} "
                f"ip={connection.remote_ip} code={close_code} reason={safe_value(reason)} "
                f"duration_ms={duration_ms} active={len(self.connections)}"
            )

    async def handle_binary(self, connection: IntercomConnection, data: bytes) -> None:
        packet = parse_packet(data)
        if packet is None:
            self._log_parse_error(connection.device_id, "bad_wtk1", frame_len=len(data))
            return
        expected_len = HEADER_LEN + len(packet.payload)
        if len(data) != expected_len:
            self._log_parse_error(
                connection.device_id,
                "length_mismatch",
                frame_len=len(data),
                expected=expected_len,
                packet_type=packet.packet_type,
            )
            return
        if packet.device != connection.device_id:
            self._log_parse_error(
                connection.device_id,
                "device_mismatch",
                packet_device=packet.device,
                ch=packet.channel,
                seq=packet.seq,
            )
            return

        connection.last_active_at = time.monotonic()
        type_name = PKT_TYPES.get(packet.packet_type, f"type_{packet.packet_type}")

        if packet.packet_type in STATE_TYPES:
            self._update_state(connection, packet, type_name)
            return
        if packet.packet_type not in FORWARD_TYPES:
            self._log_parse_error(
                connection.device_id,
                "unsupported_type",
                packet_type=packet.packet_type,
                type_name=type_name,
                ch=packet.channel,
                seq=packet.seq,
            )
            return

        now_ms = monotonic_ms()
        if packet.packet_type == APP_INTERCOM_PKT_PTT_START and self.log_stats:
            self._reset_rx_sequence(connection.device_id, packet.channel)
        if packet.packet_type == APP_INTERCOM_PKT_AUDIO:
            connection.rx_audio_count += 1
            if self.log_stats:
                self._record_rx_audio(connection, packet, now_ms)

        targets = self._targets_for(connection, packet.channel)
        queued = await self._forward_raw(data, targets, packet, now_ms)

        if packet.packet_type == APP_INTERCOM_PKT_AUDIO:
            if self.log_audio_trace and connection.rx_audio_count % self.audio_trace_every == 0:
                target_names = ",".join(target.device_id for target in targets) or "-"
                self.log_func(
                    f"intercom_audio_trace level=info device={connection.device_id} "
                    f"ch={packet.channel} seq={packet.seq} payload={len(packet.payload)} "
                    f"targets={len(targets)} queued={queued} target_devices={target_names}"
                )
            return

        event = "start" if packet.packet_type == APP_INTERCOM_PKT_PTT_START else "stop"
        self.log_func(
            f"intercom_ptt level=info event={event} device={connection.device_id} "
            f"ch={packet.channel} seq={packet.seq} targets={len(targets)} queued={queued}"
        )

    def _update_state(self, connection: IntercomConnection, packet: Packet, type_name: str) -> None:
        old_channel = connection.channel
        if packet.packet_type in (APP_INTERCOM_PKT_REGISTER, APP_INTERCOM_PKT_CHANNEL):
            connection.channel = packet.channel or 1
        if packet.packet_type == APP_INTERCOM_PKT_HEARTBEAT:
            connection.channel = packet.channel or connection.channel

        if packet.packet_type == APP_INTERCOM_PKT_REGISTER:
            self.log_func(
                f"intercom_conn level=info event=register device={connection.device_id} "
                f"ch={connection.channel} seq={packet.seq} active={len(self.connections)}"
            )
        elif packet.packet_type == APP_INTERCOM_PKT_CHANNEL:
            self.log_func(
                f"intercom_conn level=info event=channel device={connection.device_id} "
                f"old_ch={old_channel} ch={connection.channel} seq={packet.seq}"
            )
        elif type_name != "heartbeat":
            self.log_func(
                f"intercom_conn level=info event={type_name} device={connection.device_id} "
                f"ch={connection.channel} seq={packet.seq}"
            )

    def _targets_for(self, source: IntercomConnection, channel: int) -> list[IntercomConnection]:
        return [
            target
            for target in list(self.connections.values())
            if target is not source and target.channel == channel
        ]

    async def _forward_raw(
        self,
        data: bytes,
        targets: list[IntercomConnection],
        packet: Packet,
        now_ms: float,
    ) -> int:
        queued = 0
        item = OutboundItem(
            data=data,
            packet_type=packet.packet_type,
            source_device=packet.device,
            channel=packet.channel,
            seq=packet.seq,
            created_ms=now_ms,
        )
        for target in targets:
            if target.outbox is None:
                target.outbox = self._make_outbox(target.device_id)
            if await target.outbox.put(item):
                queued += 1
                if packet.packet_type == APP_INTERCOM_PKT_AUDIO and self.log_stats:
                    self._record_tx_enqueue(target, packet, target.outbox, now_ms)
        return queued

    async def writer(self, connection: IntercomConnection) -> None:
        if connection.outbox is None:
            return
        while True:
            item = await connection.outbox.get()
            started_ms = monotonic_ms()
            try:
                await asyncio.wait_for(
                    connection.websocket.send(item.data),
                    timeout=self.send_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                self.log_func(
                    f"intercom_tx level=warn event=send_timeout target={connection.device_id} "
                    f"from={item.source_device} ch={item.channel} seq={item.seq} "
                    f"timeout_s={self.send_timeout_seconds}"
                )
                with contextlib.suppress(Exception):
                    await connection.websocket.close(code=1011, reason="send timeout")
                self.remove_connection(connection, close_code=1011, reason="send_timeout")
                return
            except Exception as exc:
                self.log_func(
                    f"intercom_error level=error event=send_error target={connection.device_id} "
                    f"from={item.source_device} ch={item.channel} seq={item.seq} "
                    f"error={safe_value(str(exc))}"
                )
                with contextlib.suppress(Exception):
                    await connection.websocket.close(code=1011, reason="send failed")
                self.remove_connection(connection, close_code=1011, reason="send_error")
                return

            elapsed_ms = monotonic_ms() - started_ms
            if item.packet_type == APP_INTERCOM_PKT_AUDIO:
                connection.tx_audio_count += 1
                if self.log_stats:
                    self._record_tx_send(connection, item, elapsed_ms)

    async def stats_loop(self) -> None:
        while True:
            await asyncio.sleep(self.stats_interval_ms / 1000.0)
            self.emit_stats()

    def emit_stats(self) -> None:
        if not self.log_stats:
            return
        now_ms = monotonic_ms()
        win = self._win_label()

        for (device, channel), stats in list(self.rx_stats.items()):
            if stats.audio <= 0:
                continue
            level = "warn" if stats.gap or stats.dup else "info"
            self.log_func(
                f"intercom_rx level={level} win={win} device={device} ch={channel} "
                f"audio={stats.audio} bytes={stats.bytes_total} gap={stats.gap} "
                f"dup={stats.dup} p50_ms={percentile_ms(stats.intervals_ms, 0.50)} "
                f"p95_ms={percentile_ms(stats.intervals_ms, 0.95)} "
                f"max_ms={fmt_ms(max(stats.intervals_ms, default=0.0))} "
                f"first_seq={stats.first_seq if stats.first_seq is not None else '-'} "
                f"last_seq={stats.last_seq if stats.last_seq is not None else '-'}"
            )
            stats.reset_window(now_ms)

        for (target, source, channel), stats in list(self.tx_stats.items()):
            if (
                stats.enqueue_audio <= 0
                and stats.sent_audio <= 0
                and stats.drop_old <= 0
                and stats.drop_slow <= 0
            ):
                continue
            connection = self.connections.get(target)
            q = connection.outbox.qsize() if connection and connection.outbox is not None else 0
            oldest_ms = (
                connection.outbox.oldest_audio_age_ms(now_ms)
                if connection and connection.outbox is not None
                else 0
            )
            target_channel = connection.channel if connection is not None else "-"
            q_max = max(stats.q_max, q)
            level = "warn" if stats.drop_old or stats.drop_slow else "info"
            self.log_func(
                f"intercom_tx level={level} win={win} target={target} from={source} "
                f"ch={channel} audio={stats.enqueue_audio} sent={stats.sent_audio} "
                f"drop_old={stats.drop_old} drop_slow={stats.drop_slow} q={q} "
                f"q_max={q_max} oldest_ms={oldest_ms} "
                f"send_p95_ms={percentile_ms(stats.send_ms, 0.95)} "
                f"send_max_ms={fmt_ms(max(stats.send_ms, default=0.0))} "
                f"target_ch={target_channel}"
            )
            stats.reset_window(now_ms)

    def _record_rx_audio(self, connection: IntercomConnection, packet: Packet, now_ms: float) -> None:
        key = (connection.device_id, packet.channel)
        stats = self.rx_stats.get(key)
        if stats is None:
            stats = RxWindow(start_ms=now_ms)
            self.rx_stats[key] = stats
        stats.record(packet, now_ms)

    def _reset_rx_sequence(self, device_id: str, channel: int) -> None:
        stats = self.rx_stats.get((device_id, channel))
        if stats is not None:
            stats.expected_seq = None
            stats.last_arrival_ms = None

    def _record_tx_enqueue(
        self,
        target: IntercomConnection,
        packet: Packet,
        outbox: OutboundQueue,
        now_ms: float,
    ) -> None:
        stats = self._tx_window(target.device_id, packet.device, packet.channel, now_ms)
        stats.enqueue_audio += 1
        stats.q_max = max(stats.q_max, outbox.qsize())

    def _record_tx_send(self, target: IntercomConnection, item: OutboundItem, elapsed_ms: float) -> None:
        stats = self._tx_window(target.device_id, item.source_device, item.channel, monotonic_ms())
        stats.sent_audio += 1
        stats.send_ms.append(elapsed_ms)
        if target.outbox is not None:
            stats.q_max = max(stats.q_max, target.outbox.qsize())

    def _record_tx_drop(
        self,
        target_device: str,
        item: OutboundItem,
        reason: str,
        q_len: int,
        _oldest_ms: int,
    ) -> None:
        if not self.log_stats or item.packet_type != APP_INTERCOM_PKT_AUDIO:
            return
        stats = self._tx_window(target_device, item.source_device, item.channel, monotonic_ms())
        if reason == "drop_old":
            stats.drop_old += 1
        else:
            stats.drop_slow += 1
        stats.q_max = max(stats.q_max, q_len)

    def _tx_window(self, target: str, source: str, channel: int, now_ms: float) -> TxWindow:
        key = (target, source, channel)
        stats = self.tx_stats.get(key)
        if stats is None:
            stats = TxWindow(start_ms=now_ms)
            self.tx_stats[key] = stats
        return stats

    def _make_outbox(self, device_id: str) -> OutboundQueue:
        return OutboundQueue(
            target_device=device_id,
            max_items=self.send_queue_max,
            realtime_window_ms=self.realtime_window_ms,
            log_func=self.log_func,
            on_drop=self._record_tx_drop,
        )

    def _log_parse_error(self, device: str, reason: str, **fields: Any) -> None:
        extra = " ".join(f"{key}={safe_value(value)}" for key, value in fields.items())
        suffix = f" {extra}" if extra else ""
        self.log_func(
            f"intercom_error level=error event=parse_error device={device} reason={reason}{suffix}"
        )

    def _win_label(self) -> str:
        if self.stats_interval_ms % 1000 == 0:
            return f"{self.stats_interval_ms // 1000}s"
        return f"{self.stats_interval_ms}ms"


async def handle_ws(websocket: Any, hub: IntercomHub, path: str | None = None) -> None:
    resolved_path = resolve_ws_path(websocket, path)
    device_id = device_from_path(resolved_path)
    if not device_id:
        await websocket.close(code=1008, reason="device query required")
        return

    remote_ip, remote_port = peer_from_websocket(websocket)
    connection = IntercomConnection(
        device_id=device_id,
        websocket=websocket,
        remote_ip=remote_ip,
        remote_port=remote_port,
    )
    await hub.add_connection(connection)
    writer_task = asyncio.create_task(hub.writer(connection))
    try:
        async for message in websocket:
            if not isinstance(message, (bytes, bytearray, memoryview)):
                hub._log_parse_error(
                    device_id,
                    "non_binary",
                    payload_len=len(message) if hasattr(message, "__len__") else "-",
                )
                continue
            await hub.handle_binary(connection, bytes(message))
    except Exception as exc:
        hub.log_func(
            f"intercom_conn level=warn event=receive_error device={device_id} "
            f"error={safe_value(str(exc))}"
        )
    finally:
        writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer_task
        hub.remove_connection(
            connection,
            close_code=getattr(websocket, "close_code", "-"),
            reason=getattr(websocket, "close_reason", ""),
        )


def resolve_ws_path(websocket: Any, path: str | None) -> str:
    if path:
        return path
    request = getattr(websocket, "request", None)
    request_path = getattr(request, "path", None)
    if request_path:
        return request_path
    return getattr(websocket, "path", "") or ""


def device_from_path(path: str) -> str:
    parsed = urlparse(path)
    if parsed.path != "/intercom/ws":
        return ""
    return parse_qs(parsed.query).get("device", [""])[0].strip()


def peer_from_websocket(websocket: Any) -> tuple[str, str]:
    remote = getattr(websocket, "remote_address", None)
    if isinstance(remote, tuple) and len(remote) >= 2:
        return str(remote[0]), str(remote[1])
    return "-", "-"


def safe_value(value: Any) -> str:
    text = str(value)
    if not text:
        return "-"
    return text.replace(" ", "_").replace("\n", "_").replace("\r", "_")


async def run_server(
    *,
    host: str,
    port: int,
    audio_log_every: int,
    send_queue_max: int,
    send_timeout_seconds: float,
    log_stats: bool,
    log_audio_trace: bool,
    realtime_window_ms: int,
    stats_interval_ms: int,
    log_func=print,
) -> None:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets dependency is not installed; run pip install -r requirements.txt") from exc

    hub = IntercomHub(
        log_func=log_func,
        audio_log_every=audio_log_every,
        send_queue_max=send_queue_max,
        send_timeout_seconds=send_timeout_seconds,
        log_stats=log_stats,
        log_audio_trace=log_audio_trace,
        realtime_window_ms=realtime_window_ms,
        stats_interval_ms=stats_interval_ms,
    )
    stats_task: asyncio.Task[None] | None = None
    async with websockets.serve(
        lambda websocket, path=None: handle_ws(websocket, hub, path),
        host,
        port,
        ping_interval=20,
        ping_timeout=20,
        max_size=DEFAULT_WS_MAX_FRAME_BYTES,
    ):
        if log_stats:
            stats_task = asyncio.create_task(hub.stats_loop())
        log_func(
            f"intercom_service level=info event=listening "
            f"url=ws://{host}:{port}/intercom/ws?device=<device>"
        )
        log_func(
            f"intercom_config level=info send_queue_max={send_queue_max} "
            f"send_timeout_s={send_timeout_seconds:.1f} log_stats={int(log_stats)} "
            f"log_audio_trace={int(log_audio_trace)} realtime_window_ms={realtime_window_ms} "
            f"stats_interval_ms={stats_interval_ms}"
        )
        try:
            await asyncio.Future()
        finally:
            if stats_task is not None:
                stats_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stats_task


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the wkt-intercom-server WebSocket relay.")
    parser.add_argument("--host", default=_env_str("INTERCOM_HOST", DEFAULT_HOST), help="WebSocket bind host")
    parser.add_argument(
        "--port",
        "--ws-port",
        dest="port",
        type=int,
        default=_env_int("INTERCOM_WS_PORT", DEFAULT_WS_PORT),
        help="WebSocket bind port",
    )
    parser.add_argument(
        "--audio-log-every",
        type=int,
        default=_env_int("INTERCOM_AUDIO_LOG_EVERY_N", DEFAULT_AUDIO_TRACE_EVERY_N),
        help="When audio trace is enabled, print one sample every N audio packets",
    )
    parser.add_argument(
        "--send-queue-max",
        type=int,
        default=_env_int("INTERCOM_SEND_QUEUE_MAX", DEFAULT_SEND_QUEUE_MAX),
        help="Maximum pending outbound frames per device",
    )
    parser.add_argument(
        "--send-timeout",
        type=float,
        default=_env_float("INTERCOM_SEND_TIMEOUT_SECONDS", DEFAULT_SEND_TIMEOUT_SECONDS),
        help="WebSocket send timeout in seconds",
    )
    parser.add_argument(
        "--log-stats",
        type=int,
        choices=(0, 1),
        default=int(_env_bool("INTERCOM_LOG_STATS", DEFAULT_LOG_STATS)),
        help="Enable one-line aggregate intercom statistics",
    )
    parser.add_argument(
        "--log-audio-trace",
        type=int,
        choices=(0, 1),
        default=int(_env_bool("INTERCOM_LOG_AUDIO_TRACE", DEFAULT_LOG_AUDIO_TRACE)),
        help="Enable sampled per-audio trace logs",
    )
    parser.add_argument(
        "--realtime-window-ms",
        type=int,
        default=_env_int("INTERCOM_REALTIME_WINDOW_MS", DEFAULT_REALTIME_WINDOW_MS),
        help="Drop queued audio older than this realtime window",
    )
    parser.add_argument(
        "--stats-interval-ms",
        type=int,
        default=_env_int("INTERCOM_STATS_INTERVAL_MS", DEFAULT_STATS_INTERVAL_MS),
        help="Aggregate stats interval in milliseconds",
    )
    args = parser.parse_args(argv)
    asyncio.run(
        run_server(
            host=args.host,
            port=args.port,
            audio_log_every=args.audio_log_every,
            send_queue_max=args.send_queue_max,
            send_timeout_seconds=args.send_timeout,
            log_stats=bool(args.log_stats),
            log_audio_trace=bool(args.log_audio_trace),
            realtime_window_ms=args.realtime_window_ms,
            stats_interval_ms=args.stats_interval_ms,
        )
    )


if __name__ == "__main__":
    main()

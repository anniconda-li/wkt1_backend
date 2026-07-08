"""WTK1 UDP intercom server loop."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import socket
import threading
import time
from typing import Any
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from server.protocol import (
    APP_INTERCOM_PKT_AUDIO,
    APP_INTERCOM_PKT_AUDIO_FEC,
    APP_INTERCOM_PKT_NACK,
    APP_INTERCOM_PKT_PTT_START,
    APP_INTERCOM_PKT_PTT_STOP,
    PKT_TYPES,
    Packet,
    HEADER_LEN,
    build_fec_payload,
    build_packet,
    parse_fec_payload,
    parse_nack_payload,
    parse_packet,
)

MAX_UDP_PACKET_BYTES = 4096
DEFAULT_AUDIO_LOG_EVERY_N = 50
DEFAULT_PACING_INTERVAL_MS = 20
DEFAULT_PREBUFFER_PACKETS = 20
DEFAULT_PREBUFFER_IDLE_FLUSH_MS = 120
DEFAULT_QUEUE_MAX_PACKETS = 80
DEFAULT_QUEUE_HIGH_WATER = 60
DEFAULT_NACK_CACHE_PACKETS = 200
DEFAULT_NACK_CACHE_SECONDS = 3.0
DEFAULT_NACK_MAX_COUNT = 16
DEFAULT_FEC_GROUP_SIZE = 4
DEFAULT_WS_PORT = 18080
DEFAULT_WS_QUEUE_MAX_AUDIO = 50
DEFAULT_WS_PING_INTERVAL_SECONDS = 20
SEQ_MOD = 0x100000000
SEQ_HALF = 0x80000000
DEFAULT_SEQ_FAR_JUMP_FRAMES = 1000
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE pairs without requiring third-party packages."""
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


def seq_next(seq: int) -> int:
    return (seq + 1) & 0xFFFFFFFF


def seq_delta(seq: int, expected_seq: int) -> int:
    """Return signed uint32 distance from expected_seq to seq."""
    delta = (seq - expected_seq) & 0xFFFFFFFF
    if delta >= SEQ_HALF:
        delta -= SEQ_MOD
    return delta


@dataclass(frozen=True)
class DownlinkKey:
    target_device: str
    source_device: str
    channel: int


@dataclass
class UplinkAudioStats:
    """Wrap-safe UDP AUDIO input continuity stats for one source/channel."""

    source_device: str
    channel: int
    log_every: int
    far_jump_frames: int
    source_addr: tuple[str, int] = ("", 0)
    input_rx: int = 0
    input_gap_events: int = 0
    input_gap_frames: int = 0
    input_late: int = 0
    input_dup: int = 0
    input_far_jump: int = 0
    expected_seq: int | None = None
    last_rx_seq: int | None = None
    _recent: OrderedDict[int, None] = field(default_factory=OrderedDict)

    def observe(self, seq: int, source_addr: tuple[str, int]) -> bool:
        self.source_addr = source_addr
        self.input_rx += 1
        should_log = self.log_every > 0 and self.input_rx % self.log_every == 0

        if seq in self._recent:
            self.input_dup += 1
            self.last_rx_seq = seq
            return True

        self._remember(seq)
        if self.expected_seq is None:
            self.expected_seq = seq_next(seq)
            self.last_rx_seq = seq
            return True

        delta = seq_delta(seq, self.expected_seq)
        if delta == 0:
            self.expected_seq = seq_next(seq)
        elif delta > 0:
            if delta > self.far_jump_frames:
                self.input_far_jump += 1
                self.expected_seq = seq_next(seq)
                should_log = True
            else:
                self.input_gap_events += 1
                self.input_gap_frames += delta
                self.expected_seq = seq_next(seq)
                should_log = True
        else:
            self.input_late += 1
            should_log = True

        self.last_rx_seq = seq
        return should_log

    def log_line(self) -> str:
        return (
            f"UDP uplink stats source={self.source_device} "
            f"addr={self.source_addr[0]}:{self.source_addr[1]} ch={self.channel} "
            f"rx={self.input_rx} gap={self.input_gap_events}/{self.input_gap_frames} "
            f"late={self.input_late} dup={self.input_dup} far={self.input_far_jump} "
            f"expected={self.expected_seq if self.expected_seq is not None else '-'} "
            f"last_rx={self.last_rx_seq if self.last_rx_seq is not None else '-'}"
        )

    def _remember(self, seq: int) -> None:
        self._recent[seq] = None
        self._recent.move_to_end(seq)
        while len(self._recent) > 256:
            self._recent.popitem(last=False)


@dataclass(frozen=True)
class DownlinkCacheKey:
    target_device: str
    source_device: str
    channel: int


@dataclass(frozen=True)
class CachedAudioPacket:
    packet_bytes: bytes
    timestamp_ms: int
    cached_at: float


class DownlinkPacketCache:
    """Short per-target/source cache of downlink AUDIO packets for NACK repair."""

    def __init__(self, *, max_packets_per_stream: int, max_age_s: float) -> None:
        self.max_packets_per_stream = max(max_packets_per_stream, 1)
        self.max_age_s = max(max_age_s, 0.1)
        self._streams: dict[DownlinkCacheKey, OrderedDict[int, CachedAudioPacket]] = {}
        self._lock = threading.Lock()

    def put(
        self,
        *,
        target_device: str,
        source_device: str,
        channel: int,
        seq: int,
        timestamp_ms: int,
        packet_bytes: bytes,
    ) -> None:
        now = time.monotonic()
        stream_key = DownlinkCacheKey(
            target_device=target_device,
            source_device=source_device,
            channel=channel,
        )
        with self._lock:
            stream = self._streams.setdefault(stream_key, OrderedDict())
            stream[seq] = CachedAudioPacket(
                packet_bytes=packet_bytes,
                timestamp_ms=timestamp_ms,
                cached_at=now,
            )
            stream.move_to_end(seq)
            self._prune_all_locked(now)

    def get(self, *, target_device: str, source_device: str, channel: int, seq: int) -> bytes | None:
        now = time.monotonic()
        stream_key = DownlinkCacheKey(
            target_device=target_device,
            source_device=source_device,
            channel=channel,
        )
        with self._lock:
            stream = self._streams.get(stream_key)
            if stream is None:
                return None
            self._prune_stream_locked(stream_key, stream, now)
            cached = stream.get(seq)
            if cached is None:
                return None
            return cached.packet_bytes

    def _prune_stream_locked(
        self,
        stream_key: DownlinkCacheKey,
        stream: OrderedDict[int, CachedAudioPacket],
        now: float,
    ) -> None:
        expire_before = now - self.max_age_s
        while stream:
            _seq, oldest = next(iter(stream.items()))
            if len(stream) <= self.max_packets_per_stream and oldest.cached_at >= expire_before:
                break
            stream.popitem(last=False)
        if not stream:
            self._streams.pop(stream_key, None)

    def _prune_all_locked(self, now: float) -> None:
        for stream_key, stream in list(self._streams.items()):
            self._prune_stream_locked(stream_key, stream, now)


class DownlinkQueue:
    """Per-target paced downlink queue for complete WTK1 AUDIO packets."""

    def __init__(
        self,
        *,
        key: DownlinkKey,
        target_addr: tuple[str, int],
        sock: socket.socket,
        send_lock: threading.Lock,
        packet_cache: DownlinkPacketCache | None = None,
        log_func=print,
        interval_s: float,
        prebuffer_packets: int,
        prebuffer_idle_flush_s: float,
        max_packets: int,
        high_water_packets: int,
        log_every: int,
        start_worker: bool = True,
    ) -> None:
        self.key = key
        self.target_addr = target_addr
        self.sock = sock
        self.send_lock = send_lock
        self.packet_cache = packet_cache
        self.log_func = log_func
        self.interval_s = max(interval_s, 0.001)
        self.prebuffer_packets = max(prebuffer_packets, 1)
        self.prebuffer_idle_flush_s = max(prebuffer_idle_flush_s, self.interval_s)
        self.max_packets = max(max_packets, 1)
        self.high_water_packets = max(min(high_water_packets, self.max_packets), 1)
        self.log_every = max(log_every, 0)
        self.drop_count = 0
        self.sent_count = 0
        self._queue: deque[bytes] = deque()
        self._condition = threading.Condition()
        self._source_stopped = False
        self._last_enqueue_at = 0.0
        self._last_high_water_log = 0.0
        if start_worker:
            threading.Thread(target=self._run, name=f"udp-paced-{key.target_device}", daemon=True).start()

    def update_target_addr(self, target_addr: tuple[str, int]) -> None:
        with self._condition:
            self.target_addr = target_addr

    def reset_stream(self) -> None:
        with self._condition:
            self._queue.clear()
            self._source_stopped = False
            self._last_enqueue_at = 0.0
            self._condition.notify_all()

    def mark_source_stopped(self) -> None:
        with self._condition:
            self._source_stopped = True
            self._condition.notify_all()

    def enqueue_packet(self, packet_bytes: bytes) -> None:
        with self._condition:
            if len(self._queue) >= self.max_packets:
                self._queue.popleft()
                self.drop_count += 1
            self._queue.append(packet_bytes)
            self._last_enqueue_at = time.monotonic()
            queue_len = len(self._queue)
            drop_count = self.drop_count
            now = self._last_enqueue_at
            if queue_len >= self.high_water_packets and now - self._last_high_water_log >= 1.0:
                self._last_high_water_log = now
                self.log_func(
                    f"UDP downlink queue high target={self.key.target_device} source={self.key.source_device} "
                    f"ch={self.key.channel} queue_len={queue_len} drop_count={drop_count}"
                )
            self._condition.notify_all()

    def enqueue_audio(self, packet_bytes: bytes) -> None:
        self.enqueue_packet(packet_bytes)

    def queue_len(self) -> int:
        with self._condition:
            return len(self._queue)

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue:
                    self._condition.wait()
                while len(self._queue) < self.prebuffer_packets and not self._source_stopped:
                    idle_for = time.monotonic() - self._last_enqueue_at
                    idle_left = self.prebuffer_idle_flush_s - idle_for
                    if idle_left <= 0:
                        break
                    self._condition.wait(timeout=max(min(self.interval_s, idle_left), 0.001))
                    if not self._queue:
                        break
                if not self._queue:
                    self._source_stopped = False
                    continue

            next_send = time.monotonic()
            while True:
                with self._condition:
                    if not self._queue:
                        self._source_stopped = False
                        break
                    packet_bytes = self._queue.popleft()
                    target_addr = self.target_addr
                    queue_len = len(self._queue)
                    drop_count = self.drop_count
                    packet = parse_packet(packet_bytes)

                now = time.monotonic()
                if now < next_send:
                    time.sleep(next_send - now)
                    now = time.monotonic()
                pacing_lag = max(0.0, now - next_send)
                try:
                    with self.send_lock:
                        self.sock.sendto(packet_bytes, target_addr)
                except OSError as exc:
                    self.log_func(
                        f"UDP paced send failed target={self.key.target_device} source={self.key.source_device} "
                        f"ch={self.key.channel} error={exc}"
                    )
                else:
                    if self.packet_cache is not None and packet is not None and packet.packet_type == APP_INTERCOM_PKT_AUDIO:
                        self.packet_cache.put(
                            target_device=self.key.target_device,
                            source_device=self.key.source_device,
                            channel=self.key.channel,
                            seq=packet.seq,
                            timestamp_ms=packet.timestamp_ms,
                            packet_bytes=packet_bytes,
                        )

                self.sent_count += 1
                if self.log_every > 0 and self.sent_count % self.log_every == 0:
                    packet_type = PKT_TYPES.get(packet.packet_type, packet.packet_type) if packet is not None else "unknown"
                    if packet is not None and packet.packet_type == APP_INTERCOM_PKT_AUDIO_FEC:
                        fec = parse_fec_payload(packet.payload)
                        base = fec.base_seq if fec is not None else packet.seq
                        self.log_func(
                            f"UDP paced send target={self.key.target_device} source={self.key.source_device} "
                            f"ch={self.key.channel} type={packet_type} base={base} "
                            f"queue_len={queue_len} drop_count={drop_count} "
                            f"send_interval_ms={self.interval_s * 1000:.1f} pacing_lag_ms={pacing_lag * 1000:.2f}"
                        )
                    else:
                        self.log_func(
                            f"UDP paced send target={self.key.target_device} source={self.key.source_device} "
                            f"ch={self.key.channel} type={packet_type} queue_len={queue_len} drop_count={drop_count} "
                            f"send_interval_ms={self.interval_s * 1000:.1f} pacing_lag_ms={pacing_lag * 1000:.2f}"
                        )

                next_send += self.interval_s
                if next_send < now - self.interval_s:
                    next_send = now + self.interval_s


@dataclass(frozen=True)
class WebSocketDownlinkItem:
    """One complete WTK1 packet waiting for WebSocket downlink."""

    packet_bytes: bytes
    packet_type: int
    source_device: str
    channel: int
    seq: int = -1


@dataclass
class WebSocketStreamStats:
    enqueue_audio: int = 0
    send_audio: int = 0
    drop_audio: int = 0
    clear_count: int = 0
    queue_max: int = 0
    pacing_lag_ms: float = 0.0


class WebSocketDownlinkQueue:
    """Per-device WebSocket downlink queue with bounded audio backlog."""

    def __init__(
        self,
        *,
        target_device: str,
        max_audio_packets: int,
        log_func=print,
        log_every: int,
        high_water_audio: int | None = None,
    ) -> None:
        self.target_device = target_device
        self.max_audio_packets = max(max_audio_packets, 1)
        default_high_water = max(int(self.max_audio_packets * 0.8), 1)
        self.high_water_audio = max(min(high_water_audio or default_high_water, self.max_audio_packets), 1)
        self.log_func = log_func
        self.log_every = max(log_every, 0)
        self.drop_count = 0
        self.enqueued_audio_count = 0
        self.sent_audio_count = 0
        self._stats: dict[tuple[str, int], WebSocketStreamStats] = {}
        self._queue: deque[WebSocketDownlinkItem] = deque()
        self._audio_count = 0
        self._closed = False
        self._condition = threading.Condition()

    def enqueue(self, item: WebSocketDownlinkItem) -> bool:
        with self._condition:
            if self._closed:
                self.log_func(
                    f"websocket enqueue dropped target={self.target_device} source={item.source_device} "
                    f"ch={item.channel} type={PKT_TYPES.get(item.packet_type, item.packet_type)} "
                    f"seq={item.seq} reason=queue_closed"
                )
                return False
            if item.packet_type == APP_INTERCOM_PKT_PTT_START:
                self._clear_audio_locked(item.source_device, item.channel, reason="ptt_start")
                self._append_priority_control_locked(item)
                self._log_enqueue_locked(item)
            elif item.packet_type == APP_INTERCOM_PKT_PTT_STOP:
                if self._audio_count > self.high_water_audio:
                    self._clear_audio_locked(item.source_device, item.channel, reason="ptt_stop_high_water")
                self._queue.append(item)
                self._log_enqueue_locked(item)
            else:
                if item.packet_type == APP_INTERCOM_PKT_AUDIO:
                    self._drop_oldest_audio_until_room_locked()
                    self._audio_count += 1
                    self.enqueued_audio_count += 1
                    self._stats_for(item).enqueue_audio += 1
                self._queue.append(item)
                self._update_queue_max_locked(item)
                self._log_enqueue_locked(item)
            self._condition.notify()
            return True

    def get(self) -> WebSocketDownlinkItem | None:
        with self._condition:
            while not self._queue and not self._closed:
                self._condition.wait()
            if not self._queue:
                return None
            item = self._queue.popleft()
            if item.packet_type == APP_INTERCOM_PKT_AUDIO:
                self._audio_count = max(self._audio_count - 1, 0)
            return item

    def close(self, *, reason: str = "closed") -> None:
        with self._condition:
            self._clear_all_audio_locked(reason=reason)
            self._closed = True
            self._queue.clear()
            self._condition.notify_all()

    def queue_len(self) -> int:
        with self._condition:
            return len(self._queue)

    def _append_priority_control_locked(self, item: WebSocketDownlinkItem) -> None:
        items = list(self._queue)
        insert_at = 0
        while insert_at < len(items) and items[insert_at].packet_type != APP_INTERCOM_PKT_AUDIO:
            insert_at += 1
        items.insert(insert_at, item)
        self._queue = deque(items)

    def _clear_audio_locked(self, source_device: str, channel: int, *, reason: str) -> None:
        kept: deque[WebSocketDownlinkItem] = deque()
        cleared = 0
        for queued in self._queue:
            if (
                queued.packet_type == APP_INTERCOM_PKT_AUDIO
                and queued.source_device == source_device
                and queued.channel == channel
            ):
                cleared += 1
                continue
            kept.append(queued)
        if cleared:
            self._queue = kept
            self._audio_count = max(self._audio_count - cleared, 0)
            stats = self._stats_for_source(source_device, channel)
            stats.clear_count += cleared
            self.log_func(
                f"websocket clear queued audio target={self.target_device} source={source_device} "
                f"ch={channel} reason={reason} cleared={cleared} queue_len={len(self._queue)}"
            )

    def _clear_all_audio_locked(self, *, reason: str) -> None:
        if self._audio_count <= 0:
            return
        kept: deque[WebSocketDownlinkItem] = deque()
        cleared_by_stream: dict[tuple[str, int], int] = {}
        for queued in self._queue:
            if queued.packet_type == APP_INTERCOM_PKT_AUDIO:
                key = (queued.source_device, queued.channel)
                cleared_by_stream[key] = cleared_by_stream.get(key, 0) + 1
                continue
            kept.append(queued)
        self._queue = kept
        self._audio_count = 0
        for (source_device, channel), cleared in cleared_by_stream.items():
            stats = self._stats_for_source(source_device, channel)
            stats.clear_count += cleared
            self.log_func(
                f"websocket clear queued audio target={self.target_device} source={source_device} "
                f"ch={channel} reason={reason} cleared={cleared} queue_len={len(self._queue)}"
            )

    def _drop_oldest_audio_until_room_locked(self) -> None:
        while self._audio_count >= self.max_audio_packets:
            items = list(self._queue)
            drop_index = next(
                (index for index, queued in enumerate(items) if queued.packet_type == APP_INTERCOM_PKT_AUDIO),
                None,
            )
            if drop_index is None:
                self._audio_count = 0
                return
            dropped = items.pop(drop_index)
            self._queue = deque(items)
            self._audio_count -= 1
            self.drop_count += 1
            stats = self._stats_for(dropped)
            stats.drop_audio += 1
            self.log_func(
                f"websocket drop old audio target={self.target_device} source={dropped.source_device} "
                f"ch={dropped.channel} seq={dropped.seq} reason=queue_full "
                f"drop_count={self.drop_count} queue_len={len(self._queue)}"
            )

    def _log_enqueue_locked(self, item: WebSocketDownlinkItem) -> None:
        type_name = PKT_TYPES.get(item.packet_type, f"type_{item.packet_type}")
        if item.packet_type == APP_INTERCOM_PKT_AUDIO:
            if self.log_every <= 0 and self.enqueued_audio_count > 1:
                return
            if self.log_every > 0 and self.enqueued_audio_count % self.log_every != 0:
                return
        self.log_func(
            f"websocket downlink enqueue target={self.target_device} source={item.source_device} "
            f"type={type_name} ch={item.channel} seq={item.seq} queue_len={len(self._queue)}"
        )

    def record_sent(self, item: WebSocketDownlinkItem, *, pacing_lag_ms: float) -> None:
        with self._condition:
            if item.packet_type != APP_INTERCOM_PKT_AUDIO:
                return
            self.sent_audio_count += 1
            stats = self._stats_for(item)
            stats.send_audio += 1
            stats.pacing_lag_ms = max(stats.pacing_lag_ms, pacing_lag_ms)
            should_log = self.log_every > 0 and stats.send_audio % self.log_every == 0
            if should_log:
                self.log_func(self._stats_line_locked(item.source_device, item.channel, stats))

    def stats_line(self, *, source_device: str, channel: int) -> str:
        with self._condition:
            stats = self._stats_for_source(source_device, channel)
            return self._stats_line_locked(source_device, channel, stats)

    def _stats_for(self, item: WebSocketDownlinkItem) -> WebSocketStreamStats:
        return self._stats_for_source(item.source_device, item.channel)

    def _stats_for_source(self, source_device: str, channel: int) -> WebSocketStreamStats:
        return self._stats.setdefault((source_device, channel), WebSocketStreamStats())

    def _update_queue_max_locked(self, item: WebSocketDownlinkItem) -> None:
        stats = self._stats_for(item)
        stats.queue_max = max(stats.queue_max, len(self._queue))

    def _stats_line_locked(self, source_device: str, channel: int, stats: WebSocketStreamStats) -> str:
        return (
            f"WS downlink stats target={self.target_device} source={source_device} ch={channel} "
            f"enqueue={stats.enqueue_audio} send={stats.send_audio} drop={stats.drop_audio} "
            f"clear={stats.clear_count} queue_len={len(self._queue)} queue_max={stats.queue_max} "
            f"pacing_lag_ms={stats.pacing_lag_ms:.2f}"
        )


@dataclass
class WebSocketConnection:
    device: str
    websocket: Any
    queue: WebSocketDownlinkQueue


class WebSocketDownlinkServer:
    """Device-bound WebSocket server for experimental downlink transport."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        interval_s: float,
        max_audio_packets: int,
        high_water_audio: int,
        log_func=print,
        log_every: int,
        ping_interval_s: int,
    ) -> None:
        self.host = host
        self.port = port
        self.interval_s = max(interval_s, 0.001)
        self.max_audio_packets = max(max_audio_packets, 1)
        self.high_water_audio = max(min(high_water_audio, self.max_audio_packets), 1)
        self.log_func = log_func
        self.log_every = max(log_every, 0)
        self.ping_interval_s = max(ping_interval_s, 1)
        self._connections: dict[str, WebSocketConnection] = {}
        self._connections_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._start_error: Exception | None = None
        self._offline_log_at: dict[str, float] = {}

    def start(self) -> bool:
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run_loop, name="intercom-websocket", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=3.0):
            self.log_func(f"websocket server start timeout host={self.host} port={self.port}")
            return False
        if self._start_error is not None:
            self.log_func(f"websocket server start failed host={self.host} port={self.port} error={self._start_error}")
            return False
        return True

    def enqueue(self, target_device: str, item: WebSocketDownlinkItem) -> bool:
        with self._connections_lock:
            connection = self._connections.get(target_device)
        if connection is None:
            now = time.monotonic()
            last = self._offline_log_at.get(target_device, 0.0)
            if now - last >= 1.0:
                self._offline_log_at[target_device] = now
                self.log_func(
                    f"websocket target offline target={target_device} source={item.source_device} "
                    f"ch={item.channel} type={PKT_TYPES.get(item.packet_type, item.packet_type)} "
                    f"seq={item.seq} reason=fallback_drop"
                )
            return False
        return connection.queue.enqueue(item)

    def _run_loop(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            self._start_error = exc
            self._started.set()
            return

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._server = self._loop.run_until_complete(self._start_websocket_server(websockets))
        except Exception as exc:
            self._start_error = exc
            self._started.set()
            return

        self.log_func(f"websocket server listening ws://{self.host}:{self.port}/intercom/ws?device=<device>")
        self._started.set()
        self._loop.run_forever()

    async def _start_websocket_server(self, websockets_module: Any) -> Any:
        return await websockets_module.serve(
            self._handler,
            self.host,
            self.port,
            ping_interval=self.ping_interval_s,
            ping_timeout=self.ping_interval_s,
            max_size=MAX_UDP_PACKET_BYTES,
        )

    async def _handler(self, websocket: Any, path: str | None = None) -> None:
        resolved_path = self._resolve_path(websocket, path)
        device = self._device_from_path(resolved_path)
        if not device:
            await websocket.close(code=1008, reason="device query required")
            return

        queue = WebSocketDownlinkQueue(
            target_device=device,
            max_audio_packets=self.max_audio_packets,
            high_water_audio=self.high_water_audio,
            log_func=self.log_func,
            log_every=self.log_every,
        )
        connection = WebSocketConnection(device=device, websocket=websocket, queue=queue)
        self._register_connection(connection)
        sender_task = asyncio.create_task(self._sender(connection))

        try:
            async for _message in websocket:
                pass
        except Exception as exc:
            self.log_func(f"websocket receive ended device={device} error={exc}")
        finally:
            self._remove_connection(connection)
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task

    async def _sender(self, connection: WebSocketConnection) -> None:
        next_audio_send: float | None = None
        try:
            while True:
                item = await asyncio.to_thread(connection.queue.get)
                if item is None:
                    return

                pacing_lag = 0.0
                if item.packet_type == APP_INTERCOM_PKT_AUDIO:
                    now = time.monotonic()
                    if next_audio_send is None or next_audio_send < now - self.interval_s:
                        next_audio_send = now
                    if now < next_audio_send:
                        await asyncio.sleep(next_audio_send - now)
                        now = time.monotonic()
                    pacing_lag = max(0.0, now - next_audio_send)

                await connection.websocket.send(item.packet_bytes)

                if item.packet_type == APP_INTERCOM_PKT_AUDIO:
                    queue_len = connection.queue.queue_len()
                    connection.queue.record_sent(item, pacing_lag_ms=pacing_lag * 1000)
                    if self.log_every > 0 and connection.queue.sent_audio_count % self.log_every == 0:
                        self.log_func(
                            f"websocket paced send target={connection.device} source={item.source_device} "
                            f"ch={item.channel} type=audio seq={item.seq} queue_len={queue_len} "
                            f"send_interval_ms={self.interval_s * 1000:.1f} pacing_lag_ms={pacing_lag * 1000:.2f}"
                        )
                    next_audio_send += self.interval_s
                else:
                    type_name = PKT_TYPES.get(item.packet_type, f"type_{item.packet_type}")
                    self.log_func(
                        f"websocket paced send target={connection.device} source={item.source_device} "
                        f"ch={item.channel} type={type_name} seq={item.seq} queue_len={connection.queue.queue_len()}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log_func(f"websocket send failed target={connection.device} error={exc}")
            self._remove_connection(connection)
            with contextlib.suppress(Exception):
                await connection.websocket.close(code=1011, reason="send failed")

    def _register_connection(self, connection: WebSocketConnection) -> None:
        old: WebSocketConnection | None = None
        with self._connections_lock:
            old = self._connections.get(connection.device)
            self._connections[connection.device] = connection

        if old is not None and old is not connection:
            old.queue.close(reason="connection_replaced")
            self.log_func(f"websocket replaced old connection device={connection.device}")
            loop = self._loop
            if loop is not None:
                loop.call_soon_threadsafe(asyncio.create_task, old.websocket.close(code=1000, reason="replaced"))

        self.log_func(f"websocket connected device={connection.device}")

    def _remove_connection(self, connection: WebSocketConnection) -> bool:
        removed = False
        with self._connections_lock:
            if self._connections.get(connection.device) is connection:
                self._connections.pop(connection.device, None)
                removed = True
        if removed:
            connection.queue.close(reason="connection_disconnected")
            self.log_func(f"websocket disconnected device={connection.device}")
        return removed

    def _resolve_path(self, websocket: Any, path: str | None) -> str:
        if path:
            return path
        request = getattr(websocket, "request", None)
        request_path = getattr(request, "path", None)
        if request_path:
            return request_path
        return getattr(websocket, "path", "") or ""

    def _device_from_path(self, path: str) -> str:
        parsed = urlparse(path)
        if parsed.path != "/intercom/ws":
            return ""
        raw_device = parse_qs(parsed.query).get("device", [""])[0].strip()
        if not raw_device:
            return ""
        return raw_device


class FecGroupBuilder:
    """Build XOR AUDIO_FEC packets from contiguous downlink AUDIO groups."""

    def __init__(self, *, group_size: int, log_func=print, log_every: int = 0) -> None:
        self.group_size = max(group_size, 2)
        self.log_func = log_func
        self.log_every = max(log_every, 0)
        self._groups: dict[DownlinkKey, list[Packet]] = {}
        self._generated_counts: dict[DownlinkKey, int] = {}

    def reset(self, key: DownlinkKey) -> None:
        self._groups.pop(key, None)

    def reset_source(self, *, source_device: str, channel: int) -> None:
        for key in list(self._groups):
            if key.source_device == source_device and key.channel == channel:
                self._groups.pop(key, None)

    def add_audio(self, key: DownlinkKey, packet: Packet) -> bytes | None:
        if packet.packet_type != APP_INTERCOM_PKT_AUDIO or not packet.payload:
            return None

        group = self._groups.setdefault(key, [])
        if group:
            expected_seq = (group[-1].seq + 1) & 0xFFFFFFFF
            if packet.seq != expected_seq:
                self.log_func(
                    f"UDP fec skip source={key.source_device} ch={key.channel} "
                    f"reason=non_contiguous expected={expected_seq} got={packet.seq}"
                )
                self._groups[key] = [packet]
                return None
            expected_payload_len = len(group[0].payload)
            if len(packet.payload) != expected_payload_len:
                self.log_func(
                    f"UDP fec skip source={key.source_device} ch={key.channel} "
                    f"reason=payload_len_mismatch expected={expected_payload_len} got={len(packet.payload)}"
                )
                self._groups[key] = [packet]
                return None

        group.append(packet)
        if len(group) < self.group_size:
            return None

        fec_packet = self._build_fec_packet(group)
        self._groups[key] = []
        generated_count = self._generated_counts.get(key, 0) + 1
        self._generated_counts[key] = generated_count
        if self.log_every > 0 and (generated_count == 1 or generated_count % self.log_every == 0):
            base_seq = group[0].seq
            payload_len = len(group[0].payload)
            self.log_func(
                f"UDP fec enqueue target={key.target_device} source={key.source_device} "
                f"ch={key.channel} base={base_seq} count={len(group)} payload={payload_len}"
            )
        return fec_packet

    def _build_fec_packet(self, group: list[Packet]) -> bytes:
        base = group[0]
        xor_payload = bytearray(base.payload)
        for packet in group[1:]:
            for index, value in enumerate(packet.payload):
                xor_payload[index] ^= value
        fec_payload = build_fec_payload(
            base_seq=base.seq,
            count=len(group),
            payload_len=len(xor_payload),
            xor_payload=bytes(xor_payload),
        )
        last = group[-1]
        return build_packet(
            packet_type=APP_INTERCOM_PKT_AUDIO_FEC,
            channel=last.channel,
            seq=base.seq,
            timestamp_ms=last.timestamp_ms,
            device=base.device,
            payload=fec_payload,
        )


def run_udp(
    host: str,
    port: int,
    *,
    log_func=print,
    downlink_transport: str | None = None,
    ws_host: str | None = None,
    ws_port: int | None = None,
) -> None:
    """Run the blocking WTK1 UDP loop."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        log_func(f"UDP 绑定失败 {host}:{port}: {exc}")
        return

    devices: dict[str, tuple[str, int, int]] = {}
    downlink_queues: dict[DownlinkKey, DownlinkQueue] = {}
    send_lock = threading.Lock()
    audio_counters: dict[tuple[str, int], int] = {}
    uplink_audio_stats: dict[tuple[str, int], UplinkAudioStats] = {}
    audio_log_every = max(_env_int("INTERCOM_AUDIO_LOG_EVERY_N", DEFAULT_AUDIO_LOG_EVERY_N), 0)
    seq_far_jump_frames = max(_env_int("INTERCOM_SEQ_FAR_JUMP_FRAMES", DEFAULT_SEQ_FAR_JUMP_FRAMES), 1)
    pacing_interval_s = max(_env_int("INTERCOM_PACING_INTERVAL_MS", DEFAULT_PACING_INTERVAL_MS), 1) / 1000.0
    prebuffer_packets = max(_env_int("INTERCOM_PREBUFFER_PACKETS", DEFAULT_PREBUFFER_PACKETS), 1)
    prebuffer_idle_flush_s = (
        max(_env_int("INTERCOM_PREBUFFER_IDLE_FLUSH_MS", DEFAULT_PREBUFFER_IDLE_FLUSH_MS), 1) / 1000.0
    )
    queue_max_packets = max(_env_int("INTERCOM_QUEUE_MAX_PACKETS", DEFAULT_QUEUE_MAX_PACKETS), 1)
    queue_high_water = max(_env_int("INTERCOM_QUEUE_HIGH_WATER", DEFAULT_QUEUE_HIGH_WATER), 1)
    nack_cache_packets = max(_env_int("INTERCOM_NACK_CACHE_PACKETS", DEFAULT_NACK_CACHE_PACKETS), 1)
    nack_cache_seconds = max(_env_float("INTERCOM_NACK_CACHE_SECONDS", DEFAULT_NACK_CACHE_SECONDS), 0.1)
    nack_max_count = max(_env_int("INTERCOM_NACK_MAX_COUNT", DEFAULT_NACK_MAX_COUNT), 1)
    fec_group_size = max(_env_int("INTERCOM_FEC_GROUP_SIZE", DEFAULT_FEC_GROUP_SIZE), 2)
    selected_transport = (downlink_transport or _env_str("INTERCOM_DOWNLINK_TRANSPORT", "udp")).strip().lower()
    if selected_transport not in {"udp", "websocket"}:
        log_func(f"INTERCOM_DOWNLINK_TRANSPORT invalid value={selected_transport!r}, fallback=udp")
        selected_transport = "udp"
    resolved_ws_host = ws_host or _env_str("INTERCOM_WS_HOST", host)
    resolved_ws_port = ws_port if ws_port is not None else _env_int("INTERCOM_WS_PORT", DEFAULT_WS_PORT)
    ws_queue_max_audio = max(_env_int("INTERCOM_WS_QUEUE_MAX_AUDIO", DEFAULT_WS_QUEUE_MAX_AUDIO), 1)
    ws_queue_high_water_audio = max(
        _env_int("INTERCOM_WS_QUEUE_HIGH_WATER_AUDIO", max(int(ws_queue_max_audio * 0.8), 1)),
        1,
    )
    ws_ping_interval_s = max(
        _env_int("INTERCOM_WS_PING_INTERVAL_SECONDS", DEFAULT_WS_PING_INTERVAL_SECONDS),
        1,
    )
    fec_log_every = max(audio_log_every // fec_group_size, 1) if audio_log_every > 0 else 0
    packet_cache = DownlinkPacketCache(
        max_packets_per_stream=nack_cache_packets,
        max_age_s=nack_cache_seconds,
    )
    fec_builder = FecGroupBuilder(
        group_size=fec_group_size,
        log_func=log_func,
        log_every=fec_log_every,
    )
    websocket_server: WebSocketDownlinkServer | None = None
    if selected_transport == "websocket":
        websocket_server = WebSocketDownlinkServer(
            host=resolved_ws_host,
            port=resolved_ws_port,
            interval_s=pacing_interval_s,
            max_audio_packets=ws_queue_max_audio,
            high_water_audio=ws_queue_high_water_audio,
            log_func=log_func,
            log_every=audio_log_every,
            ping_interval_s=ws_ping_interval_s,
        )
        if not websocket_server.start():
            log_func("websocket downlink unavailable; UDP service stopped because transport=websocket")
            return

    log_func(f"UDP WTK1 监听 {host}:{port}")
    log_func(
        "UDP downlink codec=pcm mode=paced "
        f"transport={selected_transport} "
        f"audio_log_every={audio_log_every} "
        f"pacing_interval_ms={pacing_interval_s * 1000:.1f} "
        f"prebuffer_packets={prebuffer_packets} "
        f"prebuffer_idle_flush_ms={prebuffer_idle_flush_s * 1000:.1f} "
        f"queue_max_packets={queue_max_packets} "
        f"queue_high_water={queue_high_water} "
        f"fec_group_size={fec_group_size} "
        f"nack_cache_packets={nack_cache_packets} "
        f"nack_cache_seconds={nack_cache_seconds:.1f} "
        f"nack_max_count={nack_max_count} "
        f"seq_far_jump_frames={seq_far_jump_frames}"
    )
    if selected_transport == "websocket":
        log_func(
            f"websocket downlink enabled endpoint=ws://{resolved_ws_host}:{resolved_ws_port}/intercom/ws?device=<device> "
            f"queue_max_audio={ws_queue_max_audio} queue_high_water_audio={ws_queue_high_water_audio} "
            f"ping_interval_s={ws_ping_interval_s} fallback=drop"
        )

    def get_downlink_queue(
        key: DownlinkKey,
        target_addr: tuple[str, int],
    ) -> DownlinkQueue:
        queue = downlink_queues.get(key)
        if queue is None:
            queue = DownlinkQueue(
                key=key,
                target_addr=target_addr,
                sock=sock,
                send_lock=send_lock,
                packet_cache=packet_cache,
                log_func=log_func,
                interval_s=pacing_interval_s,
                prebuffer_packets=prebuffer_packets,
                prebuffer_idle_flush_s=prebuffer_idle_flush_s,
                max_packets=queue_max_packets,
                high_water_packets=queue_high_water,
                log_every=audio_log_every,
            )
            downlink_queues[key] = queue
        else:
            queue.update_target_addr(target_addr)
        return queue

    while True:
        data, addr = sock.recvfrom(MAX_UDP_PACKET_BYTES)
        packet = parse_packet(data)
        if packet is None:
            log_func(f"UDP 原始数据 from {addr[0]}:{addr[1]} len={len(data)} data={data!r}")
            continue

        type_name = PKT_TYPES.get(packet.packet_type, f"type_{packet.packet_type}")
        devices[packet.device] = (addr[0], addr[1], packet.channel)
        should_log_audio = True
        if packet.packet_type == APP_INTERCOM_PKT_AUDIO and packet.payload:
            counter_key = (packet.device, packet.channel)
            audio_counters[counter_key] = audio_counters.get(counter_key, 0) + 1
            should_log_audio = audio_log_every > 0 and audio_counters[counter_key] % audio_log_every == 0
            stats = uplink_audio_stats.get(counter_key)
            if stats is None:
                stats = UplinkAudioStats(
                    source_device=packet.device,
                    channel=packet.channel,
                    log_every=audio_log_every,
                    far_jump_frames=seq_far_jump_frames,
                )
                uplink_audio_stats[counter_key] = stats
            if stats.observe(packet.seq, addr):
                log_func(stats.log_line())

        if packet.packet_type != APP_INTERCOM_PKT_AUDIO or should_log_audio:
            log_func(
                f"UDP {type_name} from {packet.device}@{addr[0]}:{addr[1]} "
                f"ch={packet.channel} seq={packet.seq} payload={len(packet.payload)}"
            )

        if packet.packet_type == APP_INTERCOM_PKT_NACK:
            if selected_transport == "websocket":
                log_func(
                    f"UDP nack ignored transport=websocket requester={packet.device}@{addr[0]}:{addr[1]} "
                    f"ch={packet.channel} payload={len(packet.payload)}"
                )
                continue
            threading.Thread(
                target=handle_nack_packet,
                kwargs={
                    "packet": packet,
                    "requester_addr": addr,
                    "packet_cache": packet_cache,
                    "sock": sock,
                    "send_lock": send_lock,
                    "max_count": nack_max_count,
                    "log_func": log_func,
                },
                name=f"udp-nack-{packet.device}",
                daemon=True,
            ).start()
            continue

        if packet.packet_type == APP_INTERCOM_PKT_PTT_START:
            targets = audio_targets(devices, packet, addr)
            if selected_transport == "websocket":
                raw_packet = exact_wtk1_packet_bytes(data, packet)
                for target_device, _target_addr in targets:
                    if websocket_server is None:
                        continue
                    websocket_server.enqueue(
                        target_device,
                        WebSocketDownlinkItem(
                            packet_bytes=raw_packet,
                            packet_type=packet.packet_type,
                            source_device=packet.device,
                            channel=packet.channel,
                            seq=packet.seq,
                        ),
                    )
                continue
            for target_device, target_addr in targets:
                key = DownlinkKey(target_device=target_device, source_device=packet.device, channel=packet.channel)
                fec_builder.reset(key)
                get_downlink_queue(key, target_addr).reset_stream()

        if packet.packet_type == APP_INTERCOM_PKT_PTT_STOP:
            targets = audio_targets(devices, packet, addr)
            if selected_transport == "websocket":
                raw_packet = exact_wtk1_packet_bytes(data, packet)
                for target_device, _target_addr in targets:
                    if websocket_server is None:
                        continue
                    websocket_server.enqueue(
                        target_device,
                        WebSocketDownlinkItem(
                            packet_bytes=raw_packet,
                            packet_type=packet.packet_type,
                            source_device=packet.device,
                            channel=packet.channel,
                            seq=packet.seq,
                        ),
                    )
                continue
            fec_builder.reset_source(source_device=packet.device, channel=packet.channel)
            for key, queue in list(downlink_queues.items()):
                if key.source_device == packet.device and key.channel == packet.channel:
                    queue.mark_source_stopped()

        if packet.packet_type == APP_INTERCOM_PKT_AUDIO and packet.payload:
            targets = audio_targets(devices, packet, addr)
            if not targets:
                if should_log_audio:
                    log_func(
                        f"UDP audio downlink skipped source={packet.device} ch={packet.channel} "
                        f"pcm_payload_len={len(packet.payload)} target_count=0"
                    )
                continue
            if selected_transport == "websocket":
                raw_packet = exact_wtk1_packet_bytes(data, packet)
                if should_log_audio:
                    log_func(
                        f"UDP audio downlink transport=websocket source={packet.device} ch={packet.channel} "
                        f"pcm_payload_len={len(packet.payload)} packet_len={len(raw_packet)} target_count={len(targets)}"
                    )
                for target_device, _target_addr in targets:
                    if websocket_server is None:
                        continue
                    websocket_server.enqueue(
                        target_device,
                        WebSocketDownlinkItem(
                            packet_bytes=raw_packet,
                            packet_type=packet.packet_type,
                            source_device=packet.device,
                            channel=packet.channel,
                            seq=packet.seq,
                        ),
                    )
                continue
            downlink = build_audio_downlink_packet(
                packet,
                target_count=len(targets),
                log_audio=should_log_audio,
                log_func=log_func,
            )
            if downlink is None:
                continue
            payload_len = len(downlink) - HEADER_LEN
            for target_device, target_addr in targets:
                key = DownlinkKey(target_device=target_device, source_device=packet.device, channel=packet.channel)
                queue = get_downlink_queue(key, target_addr)
                queue.enqueue_audio(downlink)
                fec_packet = fec_builder.add_audio(key, packet)
                if fec_packet is not None:
                    queue.enqueue_packet(fec_packet)
                if should_log_audio:
                    log_func(
                        f"UDP 音频入队 target={target_device}@{target_addr[0]}:{target_addr[1]} "
                        f"source={packet.device} ch={packet.channel} payload={payload_len} "
                        f"queue_len={queue.queue_len()} drop_count={queue.drop_count}"
                    )


def audio_targets(
    devices: dict[str, tuple[str, int, int]],
    packet: Packet,
    source_addr: tuple[str, int],
) -> list[tuple[str, tuple[str, int]]]:
    """Return same-channel devices except the current sender."""
    return [
        (dev, (ip, port))
        for dev, (ip, port, channel) in devices.items()
        if channel == packet.channel and dev != packet.device and (ip, port) != source_addr
    ]


def build_audio_downlink_packet(
    packet: Packet,
    *,
    target_count: int = 0,
    log_audio: bool = True,
    log_func=print,
) -> bytes | None:
    """Build one downlink audio packet from one upstream PCM packet."""
    pcm_len = len(packet.payload)
    if pcm_len <= 0:
        log_func(f"UDP audio payload empty source={packet.device} ch={packet.channel}")
        return None
    if pcm_len > 0xFFFF:
        log_func(f"UDP audio payload too large source={packet.device} ch={packet.channel} pcm_payload_len={pcm_len}")
        return None

    if log_audio:
        log_func(
            f"UDP audio downlink codec=pcm source={packet.device} ch={packet.channel} "
            f"pcm_payload_len={pcm_len} downlink_payload_len={pcm_len} "
            f"packet_len={HEADER_LEN + pcm_len} target_count={target_count}"
        )
    return build_packet(
        packet_type=APP_INTERCOM_PKT_AUDIO,
        channel=packet.channel,
        seq=packet.seq,
        timestamp_ms=packet.timestamp_ms,
        device=packet.device,
        payload=packet.payload,
    )


def exact_wtk1_packet_bytes(data: bytes, packet: Packet) -> bytes:
    """Return the complete parsed WTK1 packet bytes without any UDP trailing data."""
    return data[: HEADER_LEN + len(packet.payload)]


def handle_nack_packet(
    *,
    packet: Packet,
    requester_addr: tuple[str, int],
    packet_cache: DownlinkPacketCache,
    sock: socket.socket,
    send_lock: threading.Lock,
    max_count: int,
    log_func=print,
) -> None:
    """Opportunistically retransmit cached downlink AUDIO packets for one NACK."""
    request = parse_nack_payload(packet.payload)
    if request is None:
        log_func(
            f"UDP nack ignored requester={packet.device}@{requester_addr[0]}:{requester_addr[1]} "
            f"reason=bad_payload payload={len(packet.payload)}"
        )
        return
    if not packet.device:
        log_func(
            f"UDP nack ignored requester=@{requester_addr[0]}:{requester_addr[1]} "
            "reason=empty_requester"
        )
        return
    if request.count <= 0:
        log_func(
            f"UDP nack ignored requester={packet.device}@{requester_addr[0]}:{requester_addr[1]} "
            f"source={request.source_device} ch={request.channel} start_seq={request.start_seq} reason=count_zero"
        )
        return
    if not request.source_device or request.source_device == packet.device:
        log_func(
            f"UDP nack ignored requester={packet.device}@{requester_addr[0]}:{requester_addr[1]} "
            f"source={request.source_device} ch={request.channel} reason=bad_source"
        )
        return
    if request.channel != packet.channel:
        log_func(
            f"UDP nack ignored requester={packet.device}@{requester_addr[0]}:{requester_addr[1]} "
            f"source={request.source_device} packet_ch={packet.channel} payload_ch={request.channel} "
            "reason=channel_mismatch"
        )
        return

    count = min(request.count, max_count)
    sent = 0
    missing = 0
    failed = 0
    for offset in range(count):
        seq = (request.start_seq + offset) & 0xFFFFFFFF
        packet_bytes = packet_cache.get(
            target_device=packet.device,
            source_device=request.source_device,
            channel=request.channel,
            seq=seq,
        )
        if packet_bytes is None:
            missing += 1
            continue
        try:
            with send_lock:
                sock.sendto(packet_bytes, requester_addr)
        except OSError as exc:
            failed += 1
            log_func(
                f"UDP nack resend failed target={packet.device}@{requester_addr[0]}:{requester_addr[1]} "
                f"source={request.source_device} ch={request.channel} seq={seq} error={exc}"
            )
        else:
            sent += 1

    clipped = request.count - count
    log_func(
        f"UDP nack handled requester={packet.device}@{requester_addr[0]}:{requester_addr[1]} "
        f"source={request.source_device} ch={request.channel} start_seq={request.start_seq} "
        f"count={request.count} sent={sent} missing={missing} failed={failed} clipped={clipped}"
    )


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


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default).strip() or default


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the WTK1 UDP intercom forwarding service.")
    parser.add_argument("--host", default=_env_str("INTERCOM_HOST", "0.0.0.0"), help="UDP bind host")
    parser.add_argument(
        "--udp-port",
        "--port",
        dest="udp_port",
        type=int,
        default=_env_int("INTERCOM_UDP_PORT", 19000),
        help="UDP bind port",
    )
    parser.add_argument(
        "--downlink-transport",
        choices=("udp", "websocket"),
        default=_env_str("INTERCOM_DOWNLINK_TRANSPORT", "udp"),
        help="Server-to-device downlink transport",
    )
    parser.add_argument(
        "--ws-host",
        default=_env_str("INTERCOM_WS_HOST", _env_str("INTERCOM_HOST", "0.0.0.0")),
        help="WebSocket bind host when downlink transport is websocket",
    )
    parser.add_argument(
        "--ws-port",
        type=int,
        default=_env_int("INTERCOM_WS_PORT", DEFAULT_WS_PORT),
        help="WebSocket bind port when downlink transport is websocket",
    )
    args = parser.parse_args(argv)
    run_udp(
        args.host,
        args.udp_port,
        downlink_transport=args.downlink_transport,
        ws_host=args.ws_host,
        ws_port=args.ws_port,
    )


if __name__ == "__main__":
    main()

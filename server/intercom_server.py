"""WTK1 WebSocket intercom relay."""

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
DEFAULT_AUDIO_LOG_EVERY_N = 50
DEFAULT_WS_MAX_FRAME_BYTES = 65536
DEFAULT_SEND_QUEUE_MAX = 80
DEFAULT_SEND_TIMEOUT_SECONDS = 2.0
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


@dataclass(frozen=True)
class OutboundItem:
    data: bytes
    packet_type: int
    source_device: str
    channel: int
    seq: int


class OutboundQueue:
    """Small per-device WebSocket send queue.

    This is only a live output buffer. It is not a retransmission cache.
    """

    def __init__(self, *, target_device: str, max_items: int, log_func=print) -> None:
        self.target_device = target_device
        self.max_items = max(max_items, 1)
        self.log_func = log_func
        self._items: deque[OutboundItem] = deque()
        self._condition = asyncio.Condition()
        self.drop_audio_count = 0
        self.drop_control_count = 0

    async def put(self, item: OutboundItem) -> bool:
        async with self._condition:
            if len(self._items) >= self.max_items and not self._make_room_for(item):
                return False
            self._items.append(item)
            self._condition.notify()
            return True

    async def get(self) -> OutboundItem:
        async with self._condition:
            while not self._items:
                await self._condition.wait()
            return self._items.popleft()

    def qsize(self) -> int:
        return len(self._items)

    def _make_room_for(self, item: OutboundItem) -> bool:
        for index, queued in enumerate(self._items):
            if queued.packet_type == APP_INTERCOM_PKT_AUDIO:
                del self._items[index]
                self.drop_audio_count += 1
                self.log_func(
                    f"ws queue_drop_audio target={self.target_device} "
                    f"source={queued.source_device} ch={queued.channel} seq={queued.seq} "
                    f"reason=queue_full queue_len={len(self._items)} "
                    f"drop_count={self.drop_audio_count}"
                )
                return True

        if item.packet_type == APP_INTERCOM_PKT_AUDIO:
            self.drop_audio_count += 1
            self.log_func(
                f"ws queue_drop_audio target={self.target_device} source={item.source_device} "
                f"ch={item.channel} seq={item.seq} reason=queue_full_no_audio_slot "
                f"queue_len={len(self._items)} drop_count={self.drop_audio_count}"
            )
            return False

        dropped = self._items.popleft()
        self.drop_control_count += 1
        self.log_func(
            f"ws queue_drop_control target={self.target_device} source={dropped.source_device} "
            f"ch={dropped.channel} seq={dropped.seq} reason=queue_full_control "
            f"queue_len={len(self._items)} drop_count={self.drop_control_count}"
        )
        return True


@dataclass
class IntercomConnection:
    device_id: str
    websocket: Any
    outbox: OutboundQueue | None = None
    channel: int = 1
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
        audio_log_every: int = DEFAULT_AUDIO_LOG_EVERY_N,
        send_queue_max: int = DEFAULT_SEND_QUEUE_MAX,
        send_timeout_seconds: float = DEFAULT_SEND_TIMEOUT_SECONDS,
    ) -> None:
        self.log_func = log_func
        self.audio_log_every = max(audio_log_every, 0)
        self.send_queue_max = max(send_queue_max, 1)
        self.send_timeout_seconds = max(send_timeout_seconds, 0.1)
        self.connections: dict[str, IntercomConnection] = {}

    async def add_connection(self, connection: IntercomConnection) -> None:
        if connection.outbox is None:
            connection.outbox = OutboundQueue(
                target_device=connection.device_id,
                max_items=self.send_queue_max,
                log_func=self.log_func,
            )
        old = self.connections.get(connection.device_id)
        self.connections[connection.device_id] = connection
        if old is not None and old is not connection:
            self.log_func(f"ws replaced old connection device={connection.device_id}")
            with contextlib.suppress(Exception):
                await old.websocket.close(code=1000, reason="replaced")
        self.log_func(f"ws connect device={connection.device_id} channel={connection.channel}")

    def remove_connection(self, connection: IntercomConnection) -> None:
        if self.connections.get(connection.device_id) is connection:
            self.connections.pop(connection.device_id, None)
            self.log_func(f"ws disconnect device={connection.device_id}")

    async def handle_binary(self, connection: IntercomConnection, data: bytes) -> None:
        packet = parse_packet(data)
        if packet is None:
            self.log_func(
                f"ws parse_error device={connection.device_id} reason=bad_wtk1 payload_len={len(data)}"
            )
            return
        expected_len = HEADER_LEN + len(packet.payload)
        if len(data) != expected_len:
            self.log_func(
                f"ws parse_error device={connection.device_id} reason=length_mismatch "
                f"frame_len={len(data)} expected={expected_len} type={packet.packet_type}"
            )
            return
        if packet.device != connection.device_id:
            self.log_func(
                f"ws parse_error device={connection.device_id} reason=device_mismatch "
                f"packet_device={packet.device} ch={packet.channel} seq={packet.seq}"
            )
            return

        connection.last_active_at = time.monotonic()
        type_name = PKT_TYPES.get(packet.packet_type, f"type_{packet.packet_type}")

        if packet.packet_type in STATE_TYPES:
            self._update_state(connection, packet, type_name)
            return
        if packet.packet_type not in FORWARD_TYPES:
            self.log_func(
                f"ws parse_error device={connection.device_id} reason=unsupported_type "
                f"type={packet.packet_type} type_name={type_name} ch={packet.channel} seq={packet.seq}"
            )
            return

        if packet.packet_type == APP_INTERCOM_PKT_AUDIO:
            connection.rx_audio_count += 1
            should_log = (
                self.audio_log_every > 0
                and connection.rx_audio_count % self.audio_log_every == 0
            )
        else:
            should_log = True

        targets = self._targets_for(connection, packet.channel)
        queued = await self._forward_raw(data, targets, packet)

        if should_log:
            self._log_forward(packet, type_name, connection, targets, queued)

    def _update_state(self, connection: IntercomConnection, packet: Packet, type_name: str) -> None:
        if packet.packet_type in (APP_INTERCOM_PKT_REGISTER, APP_INTERCOM_PKT_CHANNEL):
            connection.channel = packet.channel or 1
        if packet.packet_type == APP_INTERCOM_PKT_HEARTBEAT:
            connection.channel = packet.channel or connection.channel

        if packet.packet_type != APP_INTERCOM_PKT_HEARTBEAT:
            self.log_func(
                f"ws {type_name} device={connection.device_id} ch={connection.channel} "
                f"seq={packet.seq} payload={len(packet.payload)}"
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
    ) -> int:
        queued = 0
        item = OutboundItem(
            data=data,
            packet_type=packet.packet_type,
            source_device=packet.device,
            channel=packet.channel,
            seq=packet.seq,
        )
        for target in targets:
            if target.outbox is None:
                target.outbox = OutboundQueue(
                    target_device=target.device_id,
                    max_items=self.send_queue_max,
                    log_func=self.log_func,
                )
            if await target.outbox.put(item):
                queued += 1
        return queued

    async def writer(self, connection: IntercomConnection) -> None:
        if connection.outbox is None:
            return
        while True:
            item = await connection.outbox.get()
            try:
                await asyncio.wait_for(
                    connection.websocket.send(item.data),
                    timeout=self.send_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log_func(
                    f"ws send_error target={connection.device_id} source={item.source_device} "
                    f"ch={item.channel} seq={item.seq} error={exc}"
                )
                with contextlib.suppress(Exception):
                    await connection.websocket.close(code=1011, reason="send failed")
                self.remove_connection(connection)
                return
            if item.packet_type == APP_INTERCOM_PKT_AUDIO:
                connection.tx_audio_count += 1

    def _log_forward(
        self,
        packet: Packet,
        type_name: str,
        source: IntercomConnection,
        targets: list[IntercomConnection],
        queued: int,
    ) -> None:
        target_names = ",".join(target.device_id for target in targets) or "-"
        if packet.packet_type == APP_INTERCOM_PKT_AUDIO:
            self.log_func(
                f"ws audio source={source.device_id} ch={packet.channel} seq={packet.seq} "
                f"payload={len(packet.payload)} targets={len(targets)} queued={queued} "
                f"target_devices={target_names}"
            )
            return
        self.log_func(
            f"ws forward type={type_name} source={source.device_id} ch={packet.channel} "
            f"seq={packet.seq} payload={len(packet.payload)} targets={len(targets)} "
            f"queued={queued} target_devices={target_names}"
        )


async def handle_ws(websocket: Any, hub: IntercomHub, path: str | None = None) -> None:
    resolved_path = resolve_ws_path(websocket, path)
    device_id = device_from_path(resolved_path)
    if not device_id:
        await websocket.close(code=1008, reason="device query required")
        return

    connection = IntercomConnection(device_id=device_id, websocket=websocket)
    await hub.add_connection(connection)
    writer_task = asyncio.create_task(hub.writer(connection))
    try:
        async for message in websocket:
            if not isinstance(message, (bytes, bytearray, memoryview)):
                hub.log_func(
                    f"ws parse_error device={device_id} reason=non_binary "
                    f"payload_len={len(message) if hasattr(message, '__len__') else '-'}"
                )
                continue
            await hub.handle_binary(connection, bytes(message))
    except Exception as exc:
        hub.log_func(f"ws receive ended device={device_id} error={exc}")
    finally:
        writer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await writer_task
        hub.remove_connection(connection)


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


async def run_server(
    *,
    host: str,
    port: int,
    audio_log_every: int,
    send_queue_max: int,
    send_timeout_seconds: float,
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
    )
    async with websockets.serve(
        lambda websocket, path=None: handle_ws(websocket, hub, path),
        host,
        port,
        ping_interval=20,
        ping_timeout=20,
        max_size=DEFAULT_WS_MAX_FRAME_BYTES,
    ):
        log_func(f"WTK1 intercom websocket listening ws://{host}:{port}/intercom/ws?device=<device>")
        log_func(
            f"ws send queue max={send_queue_max} send_timeout_seconds={send_timeout_seconds:.1f}"
        )
        await asyncio.Future()


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
    parser = argparse.ArgumentParser(description="Run the WTK1 WebSocket intercom relay.")
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
        default=_env_int("INTERCOM_AUDIO_LOG_EVERY_N", DEFAULT_AUDIO_LOG_EVERY_N),
        help="Print one audio forwarding log every N packets per source device",
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
    args = parser.parse_args(argv)
    asyncio.run(
        run_server(
            host=args.host,
            port=args.port,
            audio_log_every=args.audio_log_every,
            send_queue_max=args.send_queue_max,
            send_timeout_seconds=args.send_timeout,
        )
    )


if __name__ == "__main__":
    main()

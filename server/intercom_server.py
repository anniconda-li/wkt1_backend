"""WTK1 WebSocket intercom relay."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from server.protocol import (
    APP_INTERCOM_PKT_AUDIO,
    APP_INTERCOM_PKT_AUDIO_FEC,
    APP_INTERCOM_PKT_CHANNEL,
    APP_INTERCOM_PKT_HEARTBEAT,
    APP_INTERCOM_PKT_NACK,
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
    APP_INTERCOM_PKT_NACK,
    APP_INTERCOM_PKT_AUDIO_FEC,
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


@dataclass
class IntercomConnection:
    device_id: str
    websocket: Any
    channel: int = 1
    connected_at: float = field(default_factory=time.monotonic)
    last_active_at: float = field(default_factory=time.monotonic)
    rx_audio_count: int = 0
    tx_audio_count: int = 0


class IntercomHub:
    """In-memory device table and WTK1 forwarding logic."""

    def __init__(self, *, log_func=print, audio_log_every: int = DEFAULT_AUDIO_LOG_EVERY_N) -> None:
        self.log_func = log_func
        self.audio_log_every = max(audio_log_every, 0)
        self.connections: dict[str, IntercomConnection] = {}

    async def add_connection(self, connection: IntercomConnection) -> None:
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
                f"type={packet.packet_type} ch={packet.channel} seq={packet.seq}"
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
        sent = await self._forward_raw(data, targets, packet)

        if should_log:
            self._log_forward(packet, type_name, connection, targets, sent)

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
        sent = 0
        for target in targets:
            try:
                await target.websocket.send(data)
            except Exception as exc:
                self.log_func(
                    f"ws send_error target={target.device_id} source={packet.device} "
                    f"ch={packet.channel} seq={packet.seq} error={exc}"
                )
                self.remove_connection(target)
            else:
                sent += 1
                if packet.packet_type == APP_INTERCOM_PKT_AUDIO:
                    target.tx_audio_count += 1
        return sent

    def _log_forward(
        self,
        packet: Packet,
        type_name: str,
        source: IntercomConnection,
        targets: list[IntercomConnection],
        sent: int,
    ) -> None:
        target_names = ",".join(target.device_id for target in targets) or "-"
        if packet.packet_type == APP_INTERCOM_PKT_AUDIO:
            self.log_func(
                f"ws audio source={source.device_id} ch={packet.channel} seq={packet.seq} "
                f"payload={len(packet.payload)} targets={len(targets)} sent={sent} "
                f"target_devices={target_names}"
            )
            return
        self.log_func(
            f"ws forward type={type_name} source={source.device_id} ch={packet.channel} "
            f"seq={packet.seq} payload={len(packet.payload)} targets={len(targets)} "
            f"sent={sent} target_devices={target_names}"
        )


async def handle_ws(websocket: Any, hub: IntercomHub, path: str | None = None) -> None:
    resolved_path = resolve_ws_path(websocket, path)
    device_id = device_from_path(resolved_path)
    if not device_id:
        await websocket.close(code=1008, reason="device query required")
        return

    connection = IntercomConnection(device_id=device_id, websocket=websocket)
    await hub.add_connection(connection)
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
    log_func=print,
) -> None:
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets dependency is not installed; run pip install -r requirements.txt") from exc

    hub = IntercomHub(log_func=log_func, audio_log_every=audio_log_every)
    async with websockets.serve(
        lambda websocket, path=None: handle_ws(websocket, hub, path),
        host,
        port,
        ping_interval=20,
        ping_timeout=20,
        max_size=DEFAULT_WS_MAX_FRAME_BYTES,
    ):
        log_func(f"WTK1 intercom websocket listening ws://{host}:{port}/intercom/ws?device=<device>")
        await asyncio.Future()


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
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
    args = parser.parse_args(argv)
    asyncio.run(run_server(host=args.host, port=args.port, audio_log_every=args.audio_log_every))


if __name__ == "__main__":
    main()

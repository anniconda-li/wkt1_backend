"""WTK1 UDP intercom server loop."""

from __future__ import annotations

import argparse
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from server.protocol import (
    APP_INTERCOM_PKT_AUDIO,
    APP_INTERCOM_PKT_PTT_START,
    APP_INTERCOM_PKT_PTT_STOP,
    PKT_TYPES,
    Packet,
    HEADER_LEN,
    build_packet,
    parse_packet,
)

MAX_UDP_PACKET_BYTES = 4096
DEFAULT_AUDIO_LOG_EVERY_N = 50
DEFAULT_PACING_INTERVAL_MS = 20
DEFAULT_PREBUFFER_PACKETS = 20
DEFAULT_PREBUFFER_IDLE_FLUSH_MS = 120
DEFAULT_QUEUE_MAX_PACKETS = 80
DEFAULT_QUEUE_HIGH_WATER = 60
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


@dataclass(frozen=True)
class DownlinkKey:
    target_device: str
    source_device: str
    channel: int


class DownlinkQueue:
    """Per-target paced downlink queue for complete WTK1 AUDIO packets."""

    def __init__(
        self,
        *,
        key: DownlinkKey,
        target_addr: tuple[str, int],
        sock: socket.socket,
        send_lock: threading.Lock,
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

    def enqueue_audio(self, packet_bytes: bytes) -> None:
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

                self.sent_count += 1
                if self.log_every > 0 and self.sent_count % self.log_every == 0:
                    self.log_func(
                        f"UDP paced send target={self.key.target_device} source={self.key.source_device} "
                        f"ch={self.key.channel} queue_len={queue_len} drop_count={drop_count} "
                        f"send_interval_ms={self.interval_s * 1000:.1f} pacing_lag_ms={pacing_lag * 1000:.2f}"
                    )

                next_send += self.interval_s
                if next_send < now - self.interval_s:
                    next_send = now + self.interval_s


def run_udp(host: str, port: int, *, log_func=print) -> None:
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
    audio_log_every = max(_env_int("INTERCOM_AUDIO_LOG_EVERY_N", DEFAULT_AUDIO_LOG_EVERY_N), 0)
    pacing_interval_s = max(_env_int("INTERCOM_PACING_INTERVAL_MS", DEFAULT_PACING_INTERVAL_MS), 1) / 1000.0
    prebuffer_packets = max(_env_int("INTERCOM_PREBUFFER_PACKETS", DEFAULT_PREBUFFER_PACKETS), 1)
    prebuffer_idle_flush_s = (
        max(_env_int("INTERCOM_PREBUFFER_IDLE_FLUSH_MS", DEFAULT_PREBUFFER_IDLE_FLUSH_MS), 1) / 1000.0
    )
    queue_max_packets = max(_env_int("INTERCOM_QUEUE_MAX_PACKETS", DEFAULT_QUEUE_MAX_PACKETS), 1)
    queue_high_water = max(_env_int("INTERCOM_QUEUE_HIGH_WATER", DEFAULT_QUEUE_HIGH_WATER), 1)
    log_func(f"UDP WTK1 监听 {host}:{port}")
    log_func(
        "UDP downlink codec=pcm mode=paced "
        f"audio_log_every={audio_log_every} "
        f"pacing_interval_ms={pacing_interval_s * 1000:.1f} "
        f"prebuffer_packets={prebuffer_packets} "
        f"prebuffer_idle_flush_ms={prebuffer_idle_flush_s * 1000:.1f} "
        f"queue_max_packets={queue_max_packets} "
        f"queue_high_water={queue_high_water}"
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

        if packet.packet_type != APP_INTERCOM_PKT_AUDIO or should_log_audio:
            log_func(
                f"UDP {type_name} from {packet.device}@{addr[0]}:{addr[1]} "
                f"ch={packet.channel} seq={packet.seq} payload={len(packet.payload)}"
            )

        if packet.packet_type == APP_INTERCOM_PKT_PTT_START:
            for target_device, target_addr in audio_targets(devices, packet, addr):
                key = DownlinkKey(target_device=target_device, source_device=packet.device, channel=packet.channel)
                get_downlink_queue(key, target_addr).reset_stream()

        if packet.packet_type == APP_INTERCOM_PKT_PTT_STOP:
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
    args = parser.parse_args(argv)
    run_udp(args.host, args.udp_port)


if __name__ == "__main__":
    main()

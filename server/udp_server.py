"""WTK1 UDP server loop.

The UDP side is independent from FastAPI: it receives device packets, records
basic traffic, forwards audio between devices on the same channel, and echoes
single-device audio for local tests. Keeping it separate lets the HTTP app stay
focused on API state and request handling.
"""

from __future__ import annotations

import os
import socket

import core.config  # noqa: F401 - load project .env when UDP is started standalone
from server.protocol import (
    APP_INTERCOM_PKT_AUDIO,
    PKT_TYPES,
    Packet,
    HEADER_LEN,
    build_packet,
    parse_packet,
)

MAX_UDP_PACKET_BYTES = 4096
DEFAULT_AUDIO_LOG_EVERY_N = 50


def run_udp(host: str, port: int, *, log_func=print) -> None:
    """Run the blocking WTK1 UDP loop."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        log_func(f"UDP 绑定失败 {host}:{port}: {exc}")
        return
    log_func(f"UDP WTK1 监听 {host}:{port}")
    log_func(
        "UDP downlink codec=pcm "
        f"audio_log_every={_env_int('INTERCOM_AUDIO_LOG_EVERY_N', DEFAULT_AUDIO_LOG_EVERY_N)}"
    )

    devices: dict[str, tuple[str, int, int]] = {}
    audio_counters: dict[tuple[str, int], int] = {}
    audio_log_every = max(_env_int("INTERCOM_AUDIO_LOG_EVERY_N", DEFAULT_AUDIO_LOG_EVERY_N), 0)

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
            payload_len = len(downlink) - 34
            packet_type = downlink[4]
            for dev, dev_addr in targets:
                sock.sendto(downlink, dev_addr)
                if should_log_audio:
                    log_func(
                        f"UDP 音频转发至 {dev}@{dev_addr[0]}:{dev_addr[1]} "
                        f"type={PKT_TYPES.get(packet_type, packet_type)} payload={payload_len}"
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

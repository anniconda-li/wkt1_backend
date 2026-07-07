"""WTK1 UDP server loop.

The UDP side is independent from FastAPI: it receives device packets, records
basic traffic, forwards audio between devices on the same channel, and echoes
single-device audio for local tests. Keeping it separate lets the HTTP app stay
focused on API state and request handling.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Protocol

import core.config  # noqa: F401 - load project .env when UDP is started standalone
from server.protocol import (
    APP_INTERCOM_PKT_AUDIO,
    APP_INTERCOM_PKT_AUDIO_OPUS,
    PKT_TYPES,
    Packet,
    build_packet,
    parse_packet,
)

PCM_FRAME_BYTES = 640
PCM_FRAME_SAMPLES = 320
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1
DEFAULT_OPUS_BITRATE = 20000
DEFAULT_OPUS_COMPLEXITY = 3
DOWNLINK_CODECS = {"pcm", "opus"}


class AudioEncoder(Protocol):
    """Minimal interface used by the UDP loop and tests."""

    def encode(self, pcm_frame: bytes) -> bytes:
        ...


class OpusDownlinkEncoder:
    """Encode one 16 kHz mono 20 ms PCM frame into one raw Opus frame."""

    def __init__(
        self,
        *,
        sample_rate: int = OPUS_SAMPLE_RATE,
        channels: int = OPUS_CHANNELS,
        frame_samples: int = PCM_FRAME_SAMPLES,
        bitrate: int = DEFAULT_OPUS_BITRATE,
        complexity: int = DEFAULT_OPUS_COMPLEXITY,
    ) -> None:
        try:
            import opuslib
        except Exception as exc:  # pragma: no cover - depends on deployment libs
            raise RuntimeError("opuslib is not installed or libopus is unavailable") from exc

        application = getattr(opuslib, "APPLICATION_VOIP", "voip")
        self._encoder = opuslib.Encoder(sample_rate, channels, application)
        self._frame_samples = frame_samples
        try:
            self._encoder.bitrate = bitrate
        except Exception:
            pass
        try:
            self._encoder.complexity = max(0, min(int(complexity), 10))
        except Exception:
            pass

    def encode(self, pcm_frame: bytes) -> bytes:
        return self._encoder.encode(pcm_frame, self._frame_samples)


def run_udp(host: str, port: int, *, log_func=print) -> None:
    """Run the blocking WTK1 UDP loop."""
    configured_codec = os.getenv("INTERCOM_DOWNLINK_CODEC", "pcm").strip().lower() or "pcm"
    codec = downlink_codec_from_env()
    opus_encoder: AudioEncoder | None = None
    if codec == "opus":
        try:
            opus_encoder = OpusDownlinkEncoder(
                bitrate=_env_int("INTERCOM_OPUS_BITRATE", DEFAULT_OPUS_BITRATE),
                complexity=_env_int("INTERCOM_OPUS_COMPLEXITY", DEFAULT_OPUS_COMPLEXITY),
            )
        except Exception as exc:
            log_func(f"UDP Opus 编码器不可用，降级 PCM: {exc}")
            codec = "pcm"

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        log_func(f"UDP 绑定失败 {host}:{port}: {exc}")
        return
    log_func(f"UDP WTK1 监听 {host}:{port}")
    log_func(
        f"UDP downlink codec configured={configured_codec} effective={codec} "
        f"opus_bitrate={_env_int('INTERCOM_OPUS_BITRATE', DEFAULT_OPUS_BITRATE)} "
        f"opus_complexity={_env_int('INTERCOM_OPUS_COMPLEXITY', DEFAULT_OPUS_COMPLEXITY)}"
    )

    devices: dict[str, tuple[str, int, int]] = {}

    while True:
        data, addr = sock.recvfrom(2048)
        packet = parse_packet(data)
        if packet is None:
            log_func(f"UDP 原始数据 from {addr[0]}:{addr[1]} len={len(data)} data={data!r}")
            continue

        type_name = PKT_TYPES.get(packet.packet_type, f"type_{packet.packet_type}")
        devices[packet.device] = (addr[0], addr[1], packet.channel)
        log_func(
            f"UDP {type_name} from {packet.device}@{addr[0]}:{addr[1]} "
            f"ch={packet.channel} seq={packet.seq} payload={len(packet.payload)}"
        )

        if packet.packet_type == APP_INTERCOM_PKT_AUDIO and packet.payload:
            targets = audio_targets(devices, packet, addr)
            if not targets:
                log_func(
                    f"UDP audio downlink skipped source={packet.device} ch={packet.channel} "
                    f"pcm_payload_len={len(packet.payload)} target_count=0"
                )
                continue
            downlink = build_audio_downlink_packet(
                packet,
                codec=codec,
                opus_encoder=opus_encoder,
                target_count=len(targets),
                log_func=log_func,
            )
            if downlink is None:
                continue
            payload_len = len(downlink) - 34
            packet_type = downlink[4]
            for dev, dev_addr in targets:
                sock.sendto(downlink, dev_addr)
                log_func(
                    f"UDP 音频转发至 {dev}@{dev_addr[0]}:{dev_addr[1]} "
                    f"type={PKT_TYPES.get(packet_type, packet_type)} payload={payload_len}"
                )


def downlink_codec_from_env() -> str:
    """Return configured server-to-device audio codec."""
    codec = os.getenv("INTERCOM_DOWNLINK_CODEC", "pcm").strip().lower()
    if codec not in DOWNLINK_CODECS:
        return "pcm"
    return codec


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
    codec: str,
    opus_encoder: AudioEncoder | None,
    target_count: int = 0,
    log_func=print,
) -> bytes | None:
    """Build one downlink audio packet from one upstream PCM packet."""
    pcm_len = len(packet.payload)
    if pcm_len != PCM_FRAME_BYTES:
        log_func(
            f"UDP audio payload_len invalid source={packet.device} ch={packet.channel} "
            f"pcm_payload_len={pcm_len} expected={PCM_FRAME_BYTES}"
        )
        return None

    packet_type = APP_INTERCOM_PKT_AUDIO
    payload = packet.payload
    encode_cost = 0.0
    actual_codec = "pcm"

    if codec == "opus":
        if opus_encoder is None:
            log_func(f"UDP Opus 编码器未初始化，降级 PCM source={packet.device} ch={packet.channel}")
        else:
            encode_start = time.perf_counter()
            try:
                payload = opus_encoder.encode(packet.payload)
                packet_type = APP_INTERCOM_PKT_AUDIO_OPUS
                actual_codec = "opus"
            except Exception as exc:
                log_func(f"UDP Opus 编码失败，降级 PCM source={packet.device} ch={packet.channel} error={exc}")
                payload = packet.payload
                packet_type = APP_INTERCOM_PKT_AUDIO
                actual_codec = "pcm"
            encode_cost = time.perf_counter() - encode_start

    log_func(
        f"UDP audio downlink codec={actual_codec} source={packet.device} ch={packet.channel} "
        f"pcm_payload_len={pcm_len} downlink_payload_len={len(payload)} "
        f"opus_payload_len={len(payload) if actual_codec == 'opus' else 0} "
        f"encode_ms={encode_cost * 1000:.2f} target_count={target_count}"
    )
    return build_packet(
        packet_type=packet_type,
        channel=packet.channel,
        seq=packet.seq,
        timestamp_ms=packet.timestamp_ms,
        device=packet.device,
        payload=payload,
    )


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default

"""WTK1 packet helpers."""

from __future__ import annotations

from dataclasses import dataclass

MAGIC = b"WTK1"
HEADER_LEN = 34
DEVICE_LEN = 16

APP_INTERCOM_PKT_REGISTER = 1
APP_INTERCOM_PKT_CHANNEL = 2
APP_INTERCOM_PKT_PTT_START = 3
APP_INTERCOM_PKT_AUDIO = 4
APP_INTERCOM_PKT_PTT_STOP = 5
APP_INTERCOM_PKT_HEARTBEAT = 6

PKT_TYPES = {
    APP_INTERCOM_PKT_REGISTER: "register",
    APP_INTERCOM_PKT_CHANNEL: "channel",
    APP_INTERCOM_PKT_PTT_START: "ptt_start",
    APP_INTERCOM_PKT_AUDIO: "audio",
    APP_INTERCOM_PKT_PTT_STOP: "ptt_stop",
    APP_INTERCOM_PKT_HEARTBEAT: "heartbeat",
}


@dataclass
class Packet:
    """Parsed WTK1 packet."""

    packet_type: int
    channel: int
    seq: int
    timestamp_ms: int
    device: str
    payload: bytes


def read_u16(data: bytes, offset: int) -> int:
    """Read a little-endian unsigned 16-bit integer."""
    return data[offset] | (data[offset + 1] << 8)


def read_u32(data: bytes, offset: int) -> int:
    """Read a little-endian unsigned 32-bit integer."""
    return (
        data[offset]
        | (data[offset + 1] << 8)
        | (data[offset + 2] << 16)
        | (data[offset + 3] << 24)
    )


def write_u16(out: bytearray, offset: int, value: int) -> None:
    """Write a little-endian unsigned 16-bit integer."""
    out[offset] = value & 0xFF
    out[offset + 1] = (value >> 8) & 0xFF


def write_u32(out: bytearray, offset: int, value: int) -> None:
    """Write a little-endian unsigned 32-bit integer."""
    out[offset] = value & 0xFF
    out[offset + 1] = (value >> 8) & 0xFF
    out[offset + 2] = (value >> 16) & 0xFF
    out[offset + 3] = (value >> 24) & 0xFF


def build_packet(
    *,
    packet_type: int,
    channel: int,
    seq: int,
    timestamp_ms: int,
    device: str,
    payload: bytes,
) -> bytes:
    """Build one WTK1 datagram with the fixed 34-byte header."""
    if len(payload) > 0xFFFF:
        raise ValueError("payload too large for WTK1 uint16 length")

    out = bytearray(HEADER_LEN + len(payload))
    out[:4] = MAGIC
    out[4] = packet_type & 0xFF
    out[5] = HEADER_LEN
    write_u16(out, 6, channel & 0xFFFF)
    write_u32(out, 8, seq & 0xFFFFFFFF)
    write_u32(out, 12, timestamp_ms & 0xFFFFFFFF)
    device_bytes = (device or "").encode("utf-8", errors="ignore")[:DEVICE_LEN]
    out[16 : 16 + len(device_bytes)] = device_bytes
    write_u16(out, 32, len(payload))
    out[HEADER_LEN:] = payload
    return bytes(out)


def parse_packet(data: bytes) -> Packet | None:
    """Parse one WTK1 packet.

    Returns ``None`` for non-WTK1 input or truncated packets so the server
    can log and ignore bad data without raising.
    """
    if len(data) < HEADER_LEN or data[:4] != MAGIC:
        return None

    header_len = data[5]
    payload_len = read_u16(data, 32)
    if header_len != HEADER_LEN or len(data) < header_len + payload_len:
        return None

    device_raw = data[16:32].split(b"\x00", 1)[0]
    return Packet(
        packet_type=data[4],
        channel=read_u16(data, 6),
        seq=read_u32(data, 8),
        timestamp_ms=read_u32(data, 12),
        device=device_raw.decode("utf-8", errors="replace"),
        payload=data[header_len : header_len + payload_len],
    )

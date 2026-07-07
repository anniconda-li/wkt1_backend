"""WTK1 UDP protocol helpers."""

from __future__ import annotations

from dataclasses import dataclass

MAGIC = b"WTK1"
HEADER_LEN = 34
DEVICE_LEN = 16
NACK_PAYLOAD_LEN = DEVICE_LEN + 2 + 4 + 2
FEC_PAYLOAD_HEADER_LEN = 8

APP_INTERCOM_PKT_REGISTER = 1
APP_INTERCOM_PKT_CHANNEL = 2
APP_INTERCOM_PKT_PTT_START = 3
APP_INTERCOM_PKT_AUDIO = 4
APP_INTERCOM_PKT_PTT_STOP = 5
APP_INTERCOM_PKT_HEARTBEAT = 6
APP_INTERCOM_PKT_NACK = 7
APP_INTERCOM_PKT_AUDIO_FEC = 8

PKT_TYPES = {
    APP_INTERCOM_PKT_REGISTER: "register",
    APP_INTERCOM_PKT_CHANNEL: "channel",
    APP_INTERCOM_PKT_PTT_START: "ptt_start",
    APP_INTERCOM_PKT_AUDIO: "audio",
    APP_INTERCOM_PKT_PTT_STOP: "ptt_stop",
    APP_INTERCOM_PKT_HEARTBEAT: "heartbeat",
    APP_INTERCOM_PKT_NACK: "nack",
    APP_INTERCOM_PKT_AUDIO_FEC: "audio_fec",
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


@dataclass(frozen=True)
class NackRequest:
    """Parsed NACK request payload."""

    source_device: str
    channel: int
    start_seq: int
    count: int


@dataclass(frozen=True)
class FecPayload:
    """Parsed AUDIO_FEC payload."""

    base_seq: int
    count: int
    payload_len: int
    xor_payload: bytes


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
    """Parse one WTK1 datagram.

    Returns ``None`` for non-WTK1 input or truncated packets so the UDP server
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


def build_nack_payload(
    *,
    source_device: str,
    channel: int,
    start_seq: int,
    count: int,
) -> bytes:
    """Build the fixed-width binary NACK payload."""
    out = bytearray(NACK_PAYLOAD_LEN)
    source_bytes = (source_device or "").encode("utf-8", errors="ignore")[:DEVICE_LEN]
    out[: len(source_bytes)] = source_bytes
    write_u16(out, DEVICE_LEN, channel & 0xFFFF)
    write_u32(out, DEVICE_LEN + 2, start_seq & 0xFFFFFFFF)
    write_u16(out, DEVICE_LEN + 6, count & 0xFFFF)
    return bytes(out)


def parse_nack_payload(payload: bytes) -> NackRequest | None:
    """Parse the fixed-width binary NACK payload."""
    if len(payload) < NACK_PAYLOAD_LEN:
        return None
    source_raw = payload[:DEVICE_LEN].split(b"\x00", 1)[0]
    return NackRequest(
        source_device=source_raw.decode("utf-8", errors="replace"),
        channel=read_u16(payload, DEVICE_LEN),
        start_seq=read_u32(payload, DEVICE_LEN + 2),
        count=read_u16(payload, DEVICE_LEN + 6),
    )


def build_fec_payload(
    *,
    base_seq: int,
    count: int,
    payload_len: int,
    xor_payload: bytes,
) -> bytes:
    """Build the fixed-width AUDIO_FEC payload header plus XOR bytes."""
    if count <= 0 or count > 0xFF:
        raise ValueError("fec count must fit uint8")
    if payload_len <= 0 or payload_len > 0xFFFF:
        raise ValueError("fec payload_len must fit uint16")
    if len(xor_payload) != payload_len:
        raise ValueError("fec xor_payload length mismatch")

    out = bytearray(FEC_PAYLOAD_HEADER_LEN + payload_len)
    write_u32(out, 0, base_seq & 0xFFFFFFFF)
    out[4] = count & 0xFF
    write_u16(out, 5, payload_len & 0xFFFF)
    out[7] = 0
    out[FEC_PAYLOAD_HEADER_LEN:] = xor_payload
    return bytes(out)


def parse_fec_payload(payload: bytes) -> FecPayload | None:
    """Parse the AUDIO_FEC payload."""
    if len(payload) < FEC_PAYLOAD_HEADER_LEN:
        return None
    payload_len = read_u16(payload, 5)
    if len(payload) != FEC_PAYLOAD_HEADER_LEN + payload_len:
        return None
    return FecPayload(
        base_seq=read_u32(payload, 0),
        count=payload[4],
        payload_len=payload_len,
        xor_payload=payload[FEC_PAYLOAD_HEADER_LEN:],
    )

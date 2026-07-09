"""WTK1 WebSocket intercom relay tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.intercom_server import IntercomConnection, IntercomHub, device_from_path  # noqa: E402
from server.protocol import (  # noqa: E402
    APP_INTERCOM_PKT_AUDIO,
    APP_INTERCOM_PKT_AUDIO_FEC,
    APP_INTERCOM_PKT_CHANNEL,
    APP_INTERCOM_PKT_NACK,
    APP_INTERCOM_PKT_PTT_START,
    APP_INTERCOM_PKT_REGISTER,
    HEADER_LEN,
    build_packet,
    parse_packet,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed: list[tuple[int, str]] = []

    async def send(self, data: bytes) -> None:
        self.sent.append(data)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed.append((code, reason))


def packet(packet_type: int, *, device: str = "walkie-01", channel: int = 1, seq: int = 1, payload: bytes = b"") -> bytes:
    return build_packet(
        packet_type=packet_type,
        channel=channel,
        seq=seq,
        timestamp_ms=seq * 20,
        device=device,
        payload=payload,
    )


class IntercomServerTest(unittest.IsolatedAsyncioTestCase):
    async def test_build_and_parse_wtk1_packet(self) -> None:
        pcm = b"\x00\x00" * 320
        data = packet(APP_INTERCOM_PKT_AUDIO, seq=42, payload=pcm)

        parsed = parse_packet(data)

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(data[5], HEADER_LEN)
        self.assertEqual(parsed.packet_type, APP_INTERCOM_PKT_AUDIO)
        self.assertEqual(parsed.channel, 1)
        self.assertEqual(parsed.seq, 42)
        self.assertEqual(parsed.device, "walkie-01")
        self.assertEqual(parsed.payload, pcm)

    async def test_register_and_channel_update_state_without_forwarding(self) -> None:
        hub = IntercomHub(log_func=lambda _message: None, audio_log_every=0)
        source_ws = FakeWebSocket()
        target_ws = FakeWebSocket()
        source = IntercomConnection(device_id="walkie-01", websocket=source_ws)
        target = IntercomConnection(device_id="walkie-02", websocket=target_ws)
        await hub.add_connection(source)
        await hub.add_connection(target)

        await hub.handle_binary(source, packet(APP_INTERCOM_PKT_REGISTER, channel=1, seq=1))
        await hub.handle_binary(source, packet(APP_INTERCOM_PKT_CHANNEL, channel=3, seq=2))

        self.assertEqual(source.channel, 3)
        self.assertEqual(target_ws.sent, [])

    async def test_audio_forwards_raw_packet_to_same_channel_only(self) -> None:
        hub = IntercomHub(log_func=lambda _message: None, audio_log_every=1)
        source_ws = FakeWebSocket()
        same_channel_ws = FakeWebSocket()
        other_channel_ws = FakeWebSocket()
        source = IntercomConnection(device_id="walkie-01", websocket=source_ws, channel=1)
        same_channel = IntercomConnection(device_id="walkie-02", websocket=same_channel_ws, channel=1)
        other_channel = IntercomConnection(device_id="walkie-03", websocket=other_channel_ws, channel=2)
        await hub.add_connection(source)
        await hub.add_connection(same_channel)
        await hub.add_connection(other_channel)

        audio = packet(APP_INTERCOM_PKT_AUDIO, channel=1, seq=10, payload=b"\x11\x22" * 320)
        await hub.handle_binary(source, audio)

        self.assertEqual(same_channel_ws.sent, [audio])
        self.assertEqual(other_channel_ws.sent, [])
        self.assertEqual(source_ws.sent, [])

    async def test_control_nack_and_fec_forward_raw(self) -> None:
        hub = IntercomHub(log_func=lambda _message: None, audio_log_every=0)
        source_ws = FakeWebSocket()
        target_ws = FakeWebSocket()
        source = IntercomConnection(device_id="walkie-01", websocket=source_ws, channel=1)
        target = IntercomConnection(device_id="walkie-02", websocket=target_ws, channel=1)
        await hub.add_connection(source)
        await hub.add_connection(target)
        start = packet(APP_INTERCOM_PKT_PTT_START, channel=1, seq=1)
        nack = packet(APP_INTERCOM_PKT_NACK, channel=1, seq=2, payload=b"nack-payload")
        fec = packet(APP_INTERCOM_PKT_AUDIO_FEC, channel=1, seq=3, payload=b"fec-payload")

        await hub.handle_binary(source, start)
        await hub.handle_binary(source, nack)
        await hub.handle_binary(source, fec)

        self.assertEqual(target_ws.sent, [start, nack, fec])

    async def test_rejects_bad_frame_and_device_mismatch(self) -> None:
        logs: list[str] = []
        hub = IntercomHub(log_func=logs.append, audio_log_every=0)
        source_ws = FakeWebSocket()
        target_ws = FakeWebSocket()
        source = IntercomConnection(device_id="walkie-01", websocket=source_ws, channel=1)
        target = IntercomConnection(device_id="walkie-02", websocket=target_ws, channel=1)
        await hub.add_connection(source)
        await hub.add_connection(target)

        await hub.handle_binary(source, b"bad")
        await hub.handle_binary(source, packet(APP_INTERCOM_PKT_AUDIO, device="walkie-99", channel=1, seq=1, payload=b"x"))
        await hub.handle_binary(source, packet(APP_INTERCOM_PKT_AUDIO, channel=1, seq=2, payload=b"x") + b"trailing")

        self.assertEqual(target_ws.sent, [])
        self.assertTrue(any("bad_wtk1" in item for item in logs))
        self.assertTrue(any("device_mismatch" in item for item in logs))
        self.assertTrue(any("length_mismatch" in item for item in logs))

    async def test_replaces_old_connection_for_same_device(self) -> None:
        hub = IntercomHub(log_func=lambda _message: None, audio_log_every=0)
        old_ws = FakeWebSocket()
        new_ws = FakeWebSocket()
        old = IntercomConnection(device_id="walkie-01", websocket=old_ws)
        new = IntercomConnection(device_id="walkie-01", websocket=new_ws)

        await hub.add_connection(old)
        await hub.add_connection(new)

        self.assertIs(hub.connections["walkie-01"], new)
        self.assertEqual(old_ws.closed, [(1000, "replaced")])


class PathTest(unittest.TestCase):
    def test_device_from_path(self) -> None:
        self.assertEqual(device_from_path("/intercom/ws?device=walkie-02"), "walkie-02")
        self.assertEqual(device_from_path("/wrong?device=walkie-02"), "")
        self.assertEqual(device_from_path("/intercom/ws"), "")


if __name__ == "__main__":
    unittest.main()

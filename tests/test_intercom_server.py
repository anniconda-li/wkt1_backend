"""WTK1 WebSocket intercom relay tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.intercom_server import IntercomConnection, IntercomHub, device_from_path  # noqa: E402
from server.protocol import (  # noqa: E402
    APP_INTERCOM_PKT_AUDIO,
    APP_INTERCOM_PKT_CHANNEL,
    APP_INTERCOM_PKT_PTT_START,
    APP_INTERCOM_PKT_PTT_STOP,
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


async def drain_outbox(connection: IntercomConnection) -> None:
    assert connection.outbox is not None
    while connection.outbox.qsize() > 0:
        item = await connection.outbox.get()
        await connection.websocket.send(item.data)


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
        await drain_outbox(same_channel)
        await drain_outbox(other_channel)

        self.assertEqual(same_channel_ws.sent, [audio])
        self.assertEqual(other_channel_ws.sent, [])
        self.assertEqual(source_ws.sent, [])

    async def test_control_forward_raw_and_unknown_types_are_dropped(self) -> None:
        logs: list[str] = []
        hub = IntercomHub(log_func=logs.append, audio_log_every=0)
        source_ws = FakeWebSocket()
        target_ws = FakeWebSocket()
        source = IntercomConnection(device_id="walkie-01", websocket=source_ws, channel=1)
        target = IntercomConnection(device_id="walkie-02", websocket=target_ws, channel=1)
        await hub.add_connection(source)
        await hub.add_connection(target)
        start = packet(APP_INTERCOM_PKT_PTT_START, channel=1, seq=1)
        stop = packet(APP_INTERCOM_PKT_PTT_STOP, channel=1, seq=2)
        unknown = packet(7, channel=1, seq=3, payload=b"old-nack-payload")

        await hub.handle_binary(source, start)
        await hub.handle_binary(source, stop)
        await hub.handle_binary(source, unknown)
        await drain_outbox(target)

        self.assertEqual(target_ws.sent, [start, stop])
        self.assertTrue(any("unsupported_type" in item for item in logs))

    async def test_send_queue_drops_old_audio_when_full(self) -> None:
        logs: list[str] = []
        hub = IntercomHub(log_func=logs.append, audio_log_every=0, send_queue_max=2)
        source_ws = FakeWebSocket()
        target_ws = FakeWebSocket()
        source = IntercomConnection(device_id="walkie-01", websocket=source_ws, channel=1)
        target = IntercomConnection(device_id="walkie-02", websocket=target_ws, channel=1)
        await hub.add_connection(source)
        await hub.add_connection(target)
        audio1 = packet(APP_INTERCOM_PKT_AUDIO, channel=1, seq=1, payload=b"\x00\x00" * 320)
        audio2 = packet(APP_INTERCOM_PKT_AUDIO, channel=1, seq=2, payload=b"\x00\x00" * 320)
        audio3 = packet(APP_INTERCOM_PKT_AUDIO, channel=1, seq=3, payload=b"\x00\x00" * 320)

        await hub.handle_binary(source, audio1)
        await hub.handle_binary(source, audio2)
        await hub.handle_binary(source, audio3)
        await drain_outbox(target)

        self.assertEqual(target_ws.sent, [audio2, audio3])
        self.assertTrue(
            any("intercom_slow" in item and "action=drop_old" in item and "first_seq=1" in item for item in logs)
        )

    async def test_aggregate_stats_report_rx_gap_and_tx_queue(self) -> None:
        logs: list[str] = []
        hub = IntercomHub(log_func=logs.append, audio_log_every=0)
        source_ws = FakeWebSocket()
        target_ws = FakeWebSocket()
        source = IntercomConnection(device_id="walkie-01", websocket=source_ws, channel=1)
        target = IntercomConnection(device_id="walkie-02", websocket=target_ws, channel=1)
        await hub.add_connection(source)
        await hub.add_connection(target)
        audio1 = packet(APP_INTERCOM_PKT_AUDIO, channel=1, seq=1, payload=b"\x00\x00" * 320)
        audio3 = packet(APP_INTERCOM_PKT_AUDIO, channel=1, seq=3, payload=b"\x00\x00" * 320)

        await hub.handle_binary(source, audio1)
        await hub.handle_binary(source, audio3)
        hub.emit_stats()

        self.assertTrue(any("intercom_rx" in item and "gap=1" in item for item in logs))
        self.assertTrue(any("intercom_tx" in item and "audio=2" in item and "q=2" in item for item in logs))

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
        await drain_outbox(target)

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

"""UDP intercom PCM forwarding tests."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.protocol import (  # noqa: E402
    APP_INTERCOM_PKT_AUDIO,
    HEADER_LEN,
    Packet,
    build_packet,
    parse_packet,
)
from server.udp_server import (  # noqa: E402
    audio_targets,
    build_audio_downlink_packet,
)


class UdpPcmForwardTest(unittest.TestCase):
    def test_build_packet_round_trips_audio_pcm(self) -> None:
        pcm = b"\x00\x00" * 320
        data = build_packet(
            packet_type=APP_INTERCOM_PKT_AUDIO,
            channel=3,
            seq=42,
            timestamp_ms=123456,
            device="walkie-01",
            payload=pcm,
        )

        packet = parse_packet(data)

        self.assertIsNotNone(packet)
        assert packet is not None
        self.assertEqual(data[5], HEADER_LEN)
        self.assertEqual(packet.packet_type, APP_INTERCOM_PKT_AUDIO)
        self.assertEqual(packet.channel, 3)
        self.assertEqual(packet.seq, 42)
        self.assertEqual(packet.timestamp_ms, 123456)
        self.assertEqual(packet.device, "walkie-01")
        self.assertEqual(packet.payload, pcm)

    def test_pcm_audio_packet_stays_pcm_downlink_packet(self) -> None:
        pcm = b"\x00\x00" * 320
        packet = Packet(
            packet_type=APP_INTERCOM_PKT_AUDIO,
            channel=7,
            seq=1001,
            timestamp_ms=9876,
            device="walkie-01",
            payload=pcm,
        )
        logs: list[str] = []

        out = build_audio_downlink_packet(
            packet,
            target_count=2,
            log_func=logs.append,
        )

        self.assertIsNotNone(out)
        parsed = parse_packet(out or b"")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.packet_type, APP_INTERCOM_PKT_AUDIO)
        self.assertEqual(parsed.channel, packet.channel)
        self.assertEqual(parsed.seq, packet.seq)
        self.assertEqual(parsed.timestamp_ms, packet.timestamp_ms)
        self.assertEqual(parsed.device, packet.device)
        self.assertEqual(parsed.payload, pcm)
        self.assertIn("codec=pcm", logs[-1])
        self.assertIn("pcm_payload_len=640", logs[-1])
        self.assertIn("downlink_payload_len=640", logs[-1])
        self.assertIn("target_count=2", logs[-1])

    def test_large_pcm_audio_packet_is_forwarded_as_is(self) -> None:
        pcm = b"\x55\x66" * 640
        packet = Packet(
            packet_type=APP_INTERCOM_PKT_AUDIO,
            channel=1,
            seq=1,
            timestamp_ms=1,
            device="walkie-01",
            payload=pcm,
        )
        logs: list[str] = []

        out = build_audio_downlink_packet(packet, log_func=logs.append)

        self.assertIsNotNone(out)
        parsed = parse_packet(out or b"")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.packet_type, APP_INTERCOM_PKT_AUDIO)
        self.assertEqual(parsed.payload, pcm)
        self.assertIn("pcm_payload_len=1280", logs[-1])
        self.assertIn("downlink_payload_len=1280", logs[-1])

    def test_empty_pcm_audio_packet_is_dropped(self) -> None:
        packet = Packet(
            packet_type=APP_INTERCOM_PKT_AUDIO,
            channel=1,
            seq=1,
            timestamp_ms=1,
            device="walkie-01",
            payload=b"",
        )
        logs: list[str] = []

        out = build_audio_downlink_packet(packet, log_func=logs.append)

        self.assertIsNone(out)
        self.assertIn("payload empty", logs[-1])

    def test_audio_targets_exclude_sender_and_other_channels(self) -> None:
        packet = Packet(
            packet_type=APP_INTERCOM_PKT_AUDIO,
            channel=2,
            seq=1,
            timestamp_ms=1,
            device="walkie-01",
            payload=b"\x00" * 640,
        )
        devices = {
            "walkie-01": ("10.0.0.2", 19001, 2),
            "walkie-02": ("10.0.0.3", 19002, 2),
            "walkie-03": ("10.0.0.4", 19003, 3),
            "alias-same-addr": ("10.0.0.2", 19001, 2),
        }

        targets = audio_targets(devices, packet, ("10.0.0.2", 19001))

        self.assertEqual(targets, [("walkie-02", ("10.0.0.3", 19002))])


if __name__ == "__main__":
    unittest.main()

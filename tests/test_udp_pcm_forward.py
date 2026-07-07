"""UDP intercom PCM forwarding tests."""

from __future__ import annotations

import sys
import threading
import time
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
    DownlinkKey,
    DownlinkQueue,
    audio_targets,
    build_audio_downlink_packet,
)


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> int:
        self.sent.append((data, addr))
        return len(data)


def _audio_packet(seq: int, payload: bytes | None = None) -> bytes:
    return build_packet(
        packet_type=APP_INTERCOM_PKT_AUDIO,
        channel=1,
        seq=seq,
        timestamp_ms=seq * 20,
        device="walkie-01",
        payload=payload if payload is not None else b"\x00\x00" * 320,
    )


def _wait_until(predicate, *, timeout_s: float = 0.5) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


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

    def test_downlink_queue_drops_oldest_audio_when_full(self) -> None:
        logs: list[str] = []
        queue = DownlinkQueue(
            key=DownlinkKey(target_device="walkie-02", source_device="walkie-01", channel=1),
            target_addr=("10.0.0.3", 19002),
            sock=FakeSocket(),
            send_lock=threading.Lock(),
            log_func=logs.append,
            interval_s=0.02,
            prebuffer_packets=20,
            prebuffer_idle_flush_s=0.12,
            max_packets=3,
            high_water_packets=2,
            log_every=0,
            start_worker=False,
        )

        for seq in range(5):
            queue.enqueue_audio(_audio_packet(seq))

        with queue._condition:
            queued = list(queue._queue)
        seqs = [parse_packet(data).seq for data in queued if parse_packet(data) is not None]

        self.assertEqual(seqs, [2, 3, 4])
        self.assertEqual(queue.queue_len(), 3)
        self.assertEqual(queue.drop_count, 2)
        self.assertTrue(any("queue high" in item for item in logs))

    def test_downlink_queue_flushes_short_stream_after_stop(self) -> None:
        fake_sock = FakeSocket()
        queue = DownlinkQueue(
            key=DownlinkKey(target_device="walkie-02", source_device="walkie-01", channel=1),
            target_addr=("10.0.0.3", 19002),
            sock=fake_sock,
            send_lock=threading.Lock(),
            log_func=lambda _message: None,
            interval_s=0.001,
            prebuffer_packets=5,
            prebuffer_idle_flush_s=5.0,
            max_packets=10,
            high_water_packets=8,
            log_every=0,
        )

        queue.enqueue_audio(_audio_packet(10))
        queue.enqueue_audio(_audio_packet(11))
        time.sleep(0.03)
        self.assertEqual(fake_sock.sent, [])

        queue.mark_source_stopped()

        self.assertTrue(_wait_until(lambda: len(fake_sock.sent) == 2))
        sent_packets = [parse_packet(data) for data, _addr in fake_sock.sent]
        self.assertEqual([packet.seq for packet in sent_packets if packet is not None], [10, 11])
        self.assertEqual([addr for _data, addr in fake_sock.sent], [("10.0.0.3", 19002)] * 2)

    def test_downlink_queue_idle_flushes_short_stream(self) -> None:
        fake_sock = FakeSocket()
        queue = DownlinkQueue(
            key=DownlinkKey(target_device="walkie-02", source_device="walkie-01", channel=1),
            target_addr=("10.0.0.3", 19002),
            sock=fake_sock,
            send_lock=threading.Lock(),
            log_func=lambda _message: None,
            interval_s=0.001,
            prebuffer_packets=5,
            prebuffer_idle_flush_s=0.02,
            max_packets=10,
            high_water_packets=8,
            log_every=0,
        )

        queue.enqueue_audio(_audio_packet(20))

        self.assertTrue(_wait_until(lambda: len(fake_sock.sent) == 1))
        sent = parse_packet(fake_sock.sent[0][0])
        self.assertIsNotNone(sent)
        assert sent is not None
        self.assertEqual(sent.seq, 20)


if __name__ == "__main__":
    unittest.main()

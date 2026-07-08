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
    APP_INTERCOM_PKT_AUDIO_FEC,
    APP_INTERCOM_PKT_NACK,
    APP_INTERCOM_PKT_PTT_START,
    APP_INTERCOM_PKT_PTT_STOP,
    HEADER_LEN,
    Packet,
    build_fec_payload,
    build_nack_payload,
    build_packet,
    parse_fec_payload,
    parse_nack_payload,
    parse_packet,
)
from server.udp_server import (  # noqa: E402
    DownlinkKey,
    DownlinkPacketCache,
    DownlinkQueue,
    FecGroupBuilder,
    WebSocketDownlinkItem,
    WebSocketDownlinkQueue,
    audio_targets,
    build_audio_downlink_packet,
    exact_wtk1_packet_bytes,
    handle_nack_packet,
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


def _control_packet(packet_type: int, seq: int) -> bytes:
    return build_packet(
        packet_type=packet_type,
        channel=1,
        seq=seq,
        timestamp_ms=seq * 20,
        device="walkie-01",
        payload=b"",
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

    def test_fec_payload_round_trips_binary_fields(self) -> None:
        xor_payload = bytes([0x5A]) * 640
        payload = build_fec_payload(
            base_seq=100,
            count=4,
            payload_len=640,
            xor_payload=xor_payload,
        )

        fec = parse_fec_payload(payload)

        self.assertIsNotNone(fec)
        assert fec is not None
        self.assertEqual(fec.base_seq, 100)
        self.assertEqual(fec.count, 4)
        self.assertEqual(fec.payload_len, 640)
        self.assertEqual(fec.xor_payload, xor_payload)

    def test_nack_payload_round_trips_binary_fields(self) -> None:
        payload = build_nack_payload(
            source_device="walkie-01",
            channel=3,
            start_seq=123456,
            count=8,
        )

        request = parse_nack_payload(payload)

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request.source_device, "walkie-01")
        self.assertEqual(request.channel, 3)
        self.assertEqual(request.start_seq, 123456)
        self.assertEqual(request.count, 8)

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

    def test_exact_wtk1_packet_bytes_strips_udp_trailing_data(self) -> None:
        packet_bytes = _audio_packet(88)
        parsed = parse_packet(packet_bytes + b"trailing")
        self.assertIsNotNone(parsed)
        assert parsed is not None

        out = exact_wtk1_packet_bytes(packet_bytes + b"trailing", parsed)

        self.assertEqual(out, packet_bytes)

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

    def test_websocket_queue_drops_oldest_audio_when_full(self) -> None:
        logs: list[str] = []
        queue = WebSocketDownlinkQueue(
            target_device="walkie-02",
            max_audio_packets=3,
            log_func=logs.append,
            log_every=0,
        )

        for seq in range(5):
            queue.enqueue(
                WebSocketDownlinkItem(
                    packet_bytes=_audio_packet(seq),
                    packet_type=APP_INTERCOM_PKT_AUDIO,
                    source_device="walkie-01",
                    channel=1,
                )
            )

        queued: list[int] = []
        while True:
            item = queue.get()
            if item is None:
                break
            packet = parse_packet(item.packet_bytes)
            self.assertIsNotNone(packet)
            assert packet is not None
            queued.append(packet.seq)
            if not queue.queue_len():
                break

        self.assertEqual(queued, [2, 3, 4])
        self.assertEqual(queue.drop_count, 2)
        self.assertTrue(any("websocket drop old audio" in item for item in logs))

    def test_websocket_queue_ptt_stop_clears_old_audio_and_keeps_stop_first(self) -> None:
        queue = WebSocketDownlinkQueue(
            target_device="walkie-02",
            max_audio_packets=10,
            log_func=lambda _message: None,
            log_every=0,
        )
        for seq in (10, 11, 12):
            queue.enqueue(
                WebSocketDownlinkItem(
                    packet_bytes=_audio_packet(seq),
                    packet_type=APP_INTERCOM_PKT_AUDIO,
                    source_device="walkie-01",
                    channel=1,
                )
            )
        stop = _control_packet(APP_INTERCOM_PKT_PTT_STOP, 13)

        queue.enqueue(
            WebSocketDownlinkItem(
                packet_bytes=stop,
                packet_type=APP_INTERCOM_PKT_PTT_STOP,
                source_device="walkie-01",
                channel=1,
            )
        )

        item = queue.get()
        self.assertIsNotNone(item)
        assert item is not None
        parsed = parse_packet(item.packet_bytes)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.packet_type, APP_INTERCOM_PKT_PTT_STOP)
        self.assertEqual(parsed.seq, 13)
        self.assertEqual(queue.queue_len(), 0)

    def test_websocket_queue_control_order_before_audio(self) -> None:
        queue = WebSocketDownlinkQueue(
            target_device="walkie-02",
            max_audio_packets=10,
            log_func=lambda _message: None,
            log_every=0,
        )
        start = _control_packet(APP_INTERCOM_PKT_PTT_START, 1)
        stop = _control_packet(APP_INTERCOM_PKT_PTT_STOP, 2)
        queue.enqueue(
            WebSocketDownlinkItem(
                packet_bytes=start,
                packet_type=APP_INTERCOM_PKT_PTT_START,
                source_device="walkie-01",
                channel=1,
            )
        )
        queue.enqueue(
            WebSocketDownlinkItem(
                packet_bytes=_audio_packet(3),
                packet_type=APP_INTERCOM_PKT_AUDIO,
                source_device="walkie-01",
                channel=1,
            )
        )
        queue.enqueue(
            WebSocketDownlinkItem(
                packet_bytes=stop,
                packet_type=APP_INTERCOM_PKT_PTT_STOP,
                source_device="walkie-01",
                channel=1,
            )
        )

        first = queue.get()
        second = queue.get()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None
        assert second is not None
        self.assertEqual(parse_packet(first.packet_bytes).packet_type, APP_INTERCOM_PKT_PTT_START)
        self.assertEqual(parse_packet(second.packet_bytes).packet_type, APP_INTERCOM_PKT_PTT_STOP)

    def test_fec_group_builder_generates_xor_after_four_contiguous_audio_packets(self) -> None:
        logs: list[str] = []
        builder = FecGroupBuilder(group_size=4, log_func=logs.append, log_every=1)
        key = DownlinkKey(target_device="walkie-02", source_device="walkie-01", channel=1)
        packets = [
            parse_packet(_audio_packet(100, bytes([1]) * 640)),
            parse_packet(_audio_packet(101, bytes([2]) * 640)),
            parse_packet(_audio_packet(102, bytes([3]) * 640)),
            parse_packet(_audio_packet(103, bytes([4]) * 640)),
        ]
        assert all(packet is not None for packet in packets)

        out = None
        for packet in packets:
            assert packet is not None
            out = builder.add_audio(key, packet)

        self.assertIsNotNone(out)
        fec_packet = parse_packet(out or b"")
        self.assertIsNotNone(fec_packet)
        assert fec_packet is not None
        self.assertEqual(fec_packet.packet_type, APP_INTERCOM_PKT_AUDIO_FEC)
        self.assertEqual(fec_packet.channel, 1)
        self.assertEqual(fec_packet.seq, 100)
        self.assertEqual(fec_packet.device, "walkie-01")
        fec = parse_fec_payload(fec_packet.payload)
        self.assertIsNotNone(fec)
        assert fec is not None
        self.assertEqual(fec.base_seq, 100)
        self.assertEqual(fec.count, 4)
        self.assertEqual(fec.payload_len, 640)
        self.assertEqual(fec.xor_payload, bytes([1 ^ 2 ^ 3 ^ 4]) * 640)
        self.assertIn("UDP fec enqueue", logs[-1])
        self.assertIn("base=100", logs[-1])

    def test_fec_group_builder_skips_non_contiguous_group_and_restarts(self) -> None:
        logs: list[str] = []
        builder = FecGroupBuilder(group_size=4, log_func=logs.append, log_every=1)
        key = DownlinkKey(target_device="walkie-02", source_device="walkie-01", channel=1)
        first = parse_packet(_audio_packet(100, b"\x01" * 640))
        gap = parse_packet(_audio_packet(102, b"\x02" * 640))
        assert first is not None
        assert gap is not None

        self.assertIsNone(builder.add_audio(key, first))
        self.assertIsNone(builder.add_audio(key, gap))

        self.assertIn("reason=non_contiguous", logs[-1])
        out = None
        for seq in (103, 104, 105):
            packet = parse_packet(_audio_packet(seq, bytes([seq & 0xFF]) * 640))
            assert packet is not None
            out = builder.add_audio(key, packet)

        self.assertIsNotNone(out)
        fec_packet = parse_packet(out or b"")
        self.assertIsNotNone(fec_packet)
        assert fec_packet is not None
        fec = parse_fec_payload(fec_packet.payload)
        self.assertIsNotNone(fec)
        assert fec is not None
        self.assertEqual(fec.base_seq, 102)
        self.assertEqual(fec.count, 4)

    def test_fec_group_builder_skips_payload_len_mismatch_and_restarts(self) -> None:
        logs: list[str] = []
        builder = FecGroupBuilder(group_size=4, log_func=logs.append, log_every=1)
        key = DownlinkKey(target_device="walkie-02", source_device="walkie-01", channel=1)
        first = parse_packet(_audio_packet(200, b"\x01" * 640))
        mismatch = parse_packet(_audio_packet(201, b"\x02\x02"))
        assert first is not None
        assert mismatch is not None

        self.assertIsNone(builder.add_audio(key, first))
        self.assertIsNone(builder.add_audio(key, mismatch))

        self.assertIn("reason=payload_len_mismatch", logs[-1])
        out = None
        for seq in (202, 203, 204):
            packet = parse_packet(_audio_packet(seq, bytes([seq & 0xFF]) * 2))
            assert packet is not None
            out = builder.add_audio(key, packet)

        self.assertIsNotNone(out)
        fec_packet = parse_packet(out or b"")
        self.assertIsNotNone(fec_packet)
        assert fec_packet is not None
        fec = parse_fec_payload(fec_packet.payload)
        self.assertIsNotNone(fec)
        assert fec is not None
        self.assertEqual(fec.base_seq, 201)
        self.assertEqual(fec.payload_len, 2)

    def test_downlink_queue_caches_sent_audio_for_nack(self) -> None:
        fake_sock = FakeSocket()
        packet_cache = DownlinkPacketCache(max_packets_per_stream=10, max_age_s=3.0)
        queue = DownlinkQueue(
            key=DownlinkKey(target_device="walkie-02", source_device="walkie-01", channel=1),
            target_addr=("10.0.0.3", 19002),
            sock=fake_sock,
            send_lock=threading.Lock(),
            packet_cache=packet_cache,
            log_func=lambda _message: None,
            interval_s=0.001,
            prebuffer_packets=1,
            prebuffer_idle_flush_s=0.02,
            max_packets=10,
            high_water_packets=8,
            log_every=0,
        )
        audio = _audio_packet(30)

        queue.enqueue_audio(audio)

        self.assertTrue(_wait_until(lambda: len(fake_sock.sent) == 1))
        cached = packet_cache.get(
            target_device="walkie-02",
            source_device="walkie-01",
            channel=1,
            seq=30,
        )
        self.assertEqual(cached, audio)

    def test_downlink_packet_cache_evicts_oldest_per_stream(self) -> None:
        packet_cache = DownlinkPacketCache(max_packets_per_stream=2, max_age_s=3.0)

        for seq in (1, 2, 3):
            packet_cache.put(
                target_device="walkie-02",
                source_device="walkie-01",
                channel=1,
                seq=seq,
                timestamp_ms=seq * 20,
                packet_bytes=_audio_packet(seq),
            )

        self.assertIsNone(
            packet_cache.get(
                target_device="walkie-02",
                source_device="walkie-01",
                channel=1,
                seq=1,
            )
        )
        self.assertEqual(
            packet_cache.get(
                target_device="walkie-02",
                source_device="walkie-01",
                channel=1,
                seq=2,
            ),
            _audio_packet(2),
        )
        self.assertEqual(
            packet_cache.get(
                target_device="walkie-02",
                source_device="walkie-01",
                channel=1,
                seq=3,
            ),
            _audio_packet(3),
        )

    def test_nack_retransmits_cached_audio_without_rewriting_packet(self) -> None:
        fake_sock = FakeSocket()
        packet_cache = DownlinkPacketCache(max_packets_per_stream=10, max_age_s=3.0)
        cached_packets = [_audio_packet(seq) for seq in (100, 101, 103)]
        for packet_bytes in cached_packets:
            packet = parse_packet(packet_bytes)
            assert packet is not None
            packet_cache.put(
                target_device="walkie-02",
                source_device="walkie-01",
                channel=1,
                seq=packet.seq,
                timestamp_ms=packet.timestamp_ms,
                packet_bytes=packet_bytes,
            )
        nack = Packet(
            packet_type=APP_INTERCOM_PKT_NACK,
            channel=1,
            seq=77,
            timestamp_ms=999,
            device="walkie-02",
            payload=build_nack_payload(
                source_device="walkie-01",
                channel=1,
                start_seq=100,
                count=4,
            ),
        )
        logs: list[str] = []

        handle_nack_packet(
            packet=nack,
            requester_addr=("10.0.0.3", 19002),
            packet_cache=packet_cache,
            sock=fake_sock,
            send_lock=threading.Lock(),
            max_count=16,
            log_func=logs.append,
        )

        self.assertEqual([data for data, _addr in fake_sock.sent], cached_packets)
        self.assertEqual([addr for _data, addr in fake_sock.sent], [("10.0.0.3", 19002)] * 3)
        self.assertIn("sent=3", logs[-1])
        self.assertIn("missing=1", logs[-1])

    def test_nack_ignores_bad_source_and_channel_mismatch(self) -> None:
        fake_sock = FakeSocket()
        packet_cache = DownlinkPacketCache(max_packets_per_stream=10, max_age_s=3.0)
        logs: list[str] = []

        handle_nack_packet(
            packet=Packet(
                packet_type=APP_INTERCOM_PKT_NACK,
                channel=1,
                seq=1,
                timestamp_ms=1,
                device="walkie-02",
                payload=build_nack_payload(
                    source_device="walkie-02",
                    channel=1,
                    start_seq=100,
                    count=1,
                ),
            ),
            requester_addr=("10.0.0.3", 19002),
            packet_cache=packet_cache,
            sock=fake_sock,
            send_lock=threading.Lock(),
            max_count=16,
            log_func=logs.append,
        )
        handle_nack_packet(
            packet=Packet(
                packet_type=APP_INTERCOM_PKT_NACK,
                channel=1,
                seq=2,
                timestamp_ms=2,
                device="walkie-02",
                payload=build_nack_payload(
                    source_device="walkie-01",
                    channel=2,
                    start_seq=100,
                    count=1,
                ),
            ),
            requester_addr=("10.0.0.3", 19002),
            packet_cache=packet_cache,
            sock=fake_sock,
            send_lock=threading.Lock(),
            max_count=16,
            log_func=logs.append,
        )

        self.assertEqual(fake_sock.sent, [])
        self.assertIn("reason=bad_source", logs[0])
        self.assertIn("reason=channel_mismatch", logs[1])


if __name__ == "__main__":
    unittest.main()

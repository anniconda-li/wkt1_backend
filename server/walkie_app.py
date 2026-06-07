#!/usr/bin/env python3
"""FastAPI and UDP server for the WTK1 backend.

The server provides:
- UDP WTK1 packet logging and same-device audio echo with a server device name.
- FastAPI chunked WAV echo for AI voice tests.
- FastAPI JPEG upload receiver for camera tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import struct
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse

import core.config  # noqa: F401 - loads project .env
from core.paths import (
    DEFAULT_CAMERA_TEST_IMAGE,
    TMP_AUDIO_RECEIVED_WAV_DIR,
    TMP_AUDIO_REPLY_WAV_DIR,
    TMP_CAMERA_LATEST_DIR,
    TMP_CAMERA_RECEIVED_DIR,
    ensure_runtime_dirs,
    env_path,
)
from routers.photo import router as photo_router
from services.bailian_app_service import BailianAppService
from services.camera_guide_debug_service import run_camera_guide_debug_test
from services.photo_guide_service import PhotoGuideService, RETAKE_MODE, choose_mode, response_payload
from services.asr_service import transcribe_wav
from services.tour_orchestrator import FIXED_ANSWER, TourOrchestrator
from services.tts_service import ERROR_TEXT, synthesize_wav_16k
from services.vision_service import VisionObservation, VisionService


MAGIC = b"WTK1"
HEADER_LEN = 34
DEVICE_LEN = 16
SERVER_DEVICE = b"server-echo"

# =============================================================================
# User configuration
# =============================================================================
# Device firmware should point APP_BUSINESS_SERVER_HOST to this PC's LAN IP.
# APP_BUSINESS_UDP_PORT should match DEFAULT_UDP_PORT.
# APP_BUSINESS_HTTP_BASE_URL should usually be:
#   http://<PC_LAN_IP>:<DEFAULT_HTTP_PORT>
DEFAULT_BIND_HOST = "0.0.0.0"
DEFAULT_UDP_PORT = 9000
DEFAULT_HTTP_PORT = 8000
DEFAULT_WAV_SAVE_DIR = TMP_AUDIO_RECEIVED_WAV_DIR
DEFAULT_JPG_SAVE_DIR = TMP_CAMERA_RECEIVED_DIR
DEFAULT_CHUNK_SIZE = 32768
DEFAULT_AI_REPLY_REPEAT = 1
DEFAULT_AI_REPLY_EXTRA_CHUNK = False

logger = logging.getLogger(__name__)

PKT_TYPES = {
    1: "register",
    2: "channel",
    3: "ptt_start",
    4: "audio",
    5: "ptt_stop",
    6: "heartbeat",
}


@dataclass
class Packet:
    packet_type: int
    channel: int
    seq: int
    timestamp_ms: int
    device: str
    payload: bytes


@dataclass
class WavInfo:
    audio_format: int
    channels: int
    sample_rate: int
    bits_per_sample: int
    data_offset: int
    data_size: int


@dataclass
class JpegInfo:
    width: int | None = None
    height: int | None = None
    progressive: bool = False


@dataclass
class AiSession:
    session_id: str
    chunks: bytearray | None = None
    total: int = 0
    received: int = 0
    reply: bytes | None = None
    save_path: Path | None = None
    device: str = "walkie-01"
    language: str = "zh"
    question_text: str = ""
    answer_text: str = ""
    asr_text: str = ""
    image_context: str = ""
    upload_wav_path: Path | None = None
    reply_path: Path | None = None
    status: str = "started"
    audio_ready: bool = False
    reply_wav_ready: bool = False
    reply_wav_size: int = 0
    reply_duration: float = 0.0
    tts_status: str = "idle"
    tts_error: str | None = None
    tts_task: asyncio.Task | None = None
    canceled: bool = False


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def auto_tts_background_enabled() -> bool:
    value = os.getenv("AUTO_TTS_BACKGROUND", "true").strip().lower()
    return value in {"1", "true", "yes", "on"}


def read_u16(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def read_u32(data: bytes, offset: int) -> int:
    return (
        data[offset]
        | (data[offset + 1] << 8)
        | (data[offset + 2] << 16)
        | (data[offset + 3] << 24)
    )


def parse_packet(data: bytes) -> Packet | None:
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


def make_server_echo(data: bytes) -> bytes:
    out = bytearray(data)
    out[16:32] = b"\x00" * DEVICE_LEN
    out[16 : 16 + len(SERVER_DEVICE)] = SERVER_DEVICE
    return bytes(out)


def parse_wav(body: bytes) -> WavInfo | None:
    if len(body) < 44 or body[:4] != b"RIFF" or body[8:12] != b"WAVE":
        return None

    pos = 12
    audio_format = channels = sample_rate = bits_per_sample = None
    data_offset = data_size = None

    while pos + 8 <= len(body):
        chunk_id = body[pos : pos + 4]
        chunk_size = read_u32(body, pos + 4)
        chunk_data = pos + 8
        chunk_end = chunk_data + chunk_size
        if chunk_end > len(body):
            return None

        if chunk_id == b"fmt ":
            if chunk_size < 16:
                return None
            audio_format, channels, sample_rate, _byte_rate, _block_align, bits_per_sample = struct.unpack_from(
                "<HHIIHH", body, chunk_data
            )
        elif chunk_id == b"data":
            data_offset = chunk_data
            data_size = chunk_size
            break

        pos = chunk_end + (chunk_size & 1)

    if (
        audio_format is None
        or channels is None
        or sample_rate is None
        or bits_per_sample is None
        or data_offset is None
        or data_size is None
    ):
        return None

    return WavInfo(
        audio_format=audio_format,
        channels=channels,
        sample_rate=sample_rate,
        bits_per_sample=bits_per_sample,
        data_offset=data_offset,
        data_size=data_size,
    )


def pcm16_stats(pcm: bytes) -> str:
    sample_count = len(pcm) // 2
    if sample_count == 0:
        return "samples=0"

    samples = struct.unpack_from(f"<{sample_count}h", pcm[: sample_count * 2])
    min_v = min(samples)
    max_v = max(samples)
    mean = sum(samples) / sample_count
    rms = math.sqrt(sum(s * s for s in samples) / sample_count)
    peak = max(abs(min_v), abs(max_v))
    clipped = sum(1 for s in samples if s <= -32760 or s >= 32760)
    zero_cross = sum(
        1
        for prev, cur in zip(samples, samples[1:])
        if (prev < 0 <= cur) or (prev > 0 >= cur)
    )
    zcr = zero_cross / max(sample_count - 1, 1)

    return (
        f"samples={sample_count} min={min_v} max={max_v} "
        f"mean={mean:.1f} rms={rms:.1f} peak={peak} "
        f"clipped={clipped} zcr={zcr:.3f}"
    )


def save_wav(body: bytes, save_dir: Path) -> Path:
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"ai_upload_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.wav"
    path.write_bytes(body)
    return path


def validate_and_log_wav(body: bytes, save_dir: Path, prefix: str) -> tuple[bool, Path | None]:
    wav = parse_wav(body)
    if wav is None:
        log(f"{prefix} invalid WAV len={len(body)}")
        return False, None

    pcm = body[wav.data_offset : wav.data_offset + wav.data_size]
    duration = 0.0
    if wav.sample_rate > 0 and wav.channels > 0 and wav.bits_per_sample > 0:
        bytes_per_sample = wav.channels * wav.bits_per_sample // 8
        if bytes_per_sample > 0:
            duration = wav.data_size / bytes_per_sample / wav.sample_rate

    save_path = save_wav(body, save_dir)
    stats = pcm16_stats(pcm) if wav.audio_format == 1 and wav.bits_per_sample == 16 else "pcm_stats=unsupported"
    log(
        f"{prefix} WAV fmt={wav.audio_format} ch={wav.channels} rate={wav.sample_rate} "
        f"bits={wav.bits_per_sample} data={wav.data_size} duration={duration:.2f}s "
        f"{stats} saved={save_path}"
    )
    return True, save_path


def build_pcm_wav(
    pcm: bytes,
    *,
    sample_rate: int,
    channels: int,
    bits_per_sample: int,
    add_extra_chunk: bool,
) -> bytes:
    if bits_per_sample % 8 != 0:
        raise ValueError("bits_per_sample must be byte aligned")

    block_align = channels * bits_per_sample // 8
    byte_rate = sample_rate * block_align
    fmt_chunk = struct.pack(
        "<4sIHHIIHH",
        b"fmt ",
        16,
        1,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    chunks = [fmt_chunk]
    if add_extra_chunk:
        # Forces the device to find data after an extra chunk instead of assuming
        # the standard 44-byte WAV header layout.
        junk_payload = b"stream-test-extra"
        chunks.append(struct.pack("<4sI", b"JUNK", len(junk_payload)) + junk_payload)
        if len(junk_payload) & 1:
            chunks.append(b"\x00")
    chunks.append(struct.pack("<4sI", b"data", len(pcm)) + pcm)
    body = b"".join(chunks)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WAVE" + body


def make_ai_reply_wav(upload_wav: bytes, repeat: int, add_extra_chunk: bool) -> bytes | None:
    wav = parse_wav(upload_wav)
    if wav is None:
        return None
    if wav.audio_format != 1:
        return None
    pcm = upload_wav[wav.data_offset : wav.data_offset + wav.data_size]
    if repeat > 1:
        pcm = pcm * repeat
    return build_pcm_wav(
        pcm,
        sample_rate=wav.sample_rate,
        channels=wav.channels,
        bits_per_sample=wav.bits_per_sample,
        add_extra_chunk=add_extra_chunk,
    )


def parse_jpeg(body: bytes) -> JpegInfo | None:
    if len(body) < 4 or body[:2] != b"\xFF\xD8" or body[-2:] != b"\xFF\xD9":
        return None

    pos = 2
    while pos + 4 <= len(body):
        if body[pos] != 0xFF:
            pos += 1
            continue
        while pos < len(body) and body[pos] == 0xFF:
            pos += 1
        if pos >= len(body):
            break

        marker = body[pos]
        pos += 1
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(body):
            return None

        segment_len = read_u16_be(body, pos)
        if segment_len < 2 or pos + segment_len > len(body):
            return None

        if marker in (0xC0, 0xC1, 0xC2):
            if segment_len < 7:
                return None
            height = read_u16_be(body, pos + 3)
            width = read_u16_be(body, pos + 5)
            return JpegInfo(width=width, height=height, progressive=(marker == 0xC2))

        pos += segment_len

    return JpegInfo()


def read_u16_be(data: bytes, offset: int) -> int:
    return (data[offset] << 8) | data[offset + 1]


def save_jpeg(body: bytes, save_dir: Path) -> Path:
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"camera_upload_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    path.write_bytes(body)
    return path


def save_camera_raw(body: bytes, save_dir: Path) -> Path:
    save_dir.mkdir(parents=True, exist_ok=True)
    path = save_dir / f"camera_upload_invalid_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.bin"
    path.write_bytes(body)
    return path


def validate_and_log_jpeg(body: bytes, save_dir: Path, prefix: str) -> tuple[bool, Path | None, JpegInfo | None]:
    jpeg = parse_jpeg(body)
    if jpeg is None:
        save_path = save_camera_raw(body, save_dir)
        log(
            f"{prefix} invalid JPEG len={len(body)} "
            f"soi={body[:2].hex()} eoi={body[-2:].hex() if len(body) >= 2 else ''} "
            f"saved_raw={save_path}"
        )
        return False, save_path, None

    save_path = save_jpeg(body, save_dir)
    size_text = f"{jpeg.width}x{jpeg.height}" if jpeg.width and jpeg.height else "unknown"
    log(
        f"{prefix} JPEG len={len(body)} size={size_text} "
        f"progressive={int(jpeg.progressive)} saved={save_path}"
    )
    return True, save_path, jpeg


def run_udp(host: str, port: int) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        log(f"UDP bind failed on {host}:{port}: {exc}")
        return
    log(f"UDP WTK1 listening on {host}:{port}")

    devices: dict[str, tuple[str, int, int]] = {}

    while True:
        data, addr = sock.recvfrom(2048)
        packet = parse_packet(data)
        if packet is None:
            log(f"UDP raw from {addr[0]}:{addr[1]} len={len(data)} data={data!r}")
            continue

        type_name = PKT_TYPES.get(packet.packet_type, f"type_{packet.packet_type}")
        devices[packet.device] = (addr[0], addr[1], packet.channel)
        log(
            f"UDP {type_name} from {packet.device}@{addr[0]}:{addr[1]} "
            f"ch={packet.channel} seq={packet.seq} payload={len(packet.payload)}"
        )

        if packet.packet_type == 4 and packet.payload:
            targets = [
                (dev, dev_addr)
                for dev, (ip, port, channel) in devices.items()
                if dev != packet.device and channel == packet.channel
                for dev_addr in [(ip, port)]
            ]
            if targets:
                for dev, dev_addr in targets:
                    sock.sendto(data, dev_addr)
                    log(f"UDP audio forwarded to {dev}@{dev_addr[0]}:{dev_addr[1]}")
            else:
                # A single-device business test needs a downlink packet whose
                # device field is not the local device name, otherwise the
                # client drops it.
                sock.sendto(make_server_echo(data), addr)


def create_http_app(
    wav_save_dir: Path,
    jpg_save_dir: Path,
    ai_reply_repeat: int,
    ai_reply_extra_chunk: bool,
) -> FastAPI:
    ensure_runtime_dirs()
    app = FastAPI(title="WTK1 Backend")
    app.include_router(photo_router)
    app.state.save_dir = wav_save_dir
    app.state.jpg_save_dir = jpg_save_dir
    app.state.ai_sessions = {}
    app.state.ai_sessions_lock = threading.RLock()
    app.state.latest_images = {}
    app.state.latest_image_analysis = {}
    app.state.ai_reply_repeat = max(ai_reply_repeat, 1)
    app.state.ai_reply_extra_chunk = ai_reply_extra_chunk
    app.state.reply_save_dir = env_path("REPLY_WAV_SAVE_DIR", TMP_AUDIO_REPLY_WAV_DIR)
    app.state.latest_dir = env_path("LATEST_TMP_DIR", TMP_CAMERA_LATEST_DIR)
    bailian_app_service = BailianAppService()
    app.state.bailian_app_service = bailian_app_service
    app.state.tour_orchestrator = TourOrchestrator(bailian_app_service)
    app.state.vision_service = VisionService()
    app.state.photo_guide_service = PhotoGuideService(bailian_app_service)

    def get_session(session_id: str) -> AiSession:
        with app.state.ai_sessions_lock:
            session = app.state.ai_sessions.get(session_id)
            if session is None:
                raise HTTPException(status_code=404, detail={"ok": False, "error": "unknown session"})
            return session

    def is_session_canceled(ai_session: AiSession) -> bool:
        return ai_session.canceled or ai_session.status == "canceled"

    def mark_session_canceled(ai_session: AiSession) -> None:
        ai_session.canceled = True
        ai_session.status = "canceled"
        ai_session.audio_ready = False
        ai_session.reply_wav_ready = False
        ai_session.reply_wav_size = 0
        ai_session.reply_duration = 0.0
        ai_session.tts_status = "canceled"
        ai_session.tts_error = None

    def canceled_result_info(session_id: str, ai_session: AiSession) -> dict[str, object]:
        return {
            "ok": True,
            "session": session_id,
            "ready": False,
            "total": 0,
            "format": "wav",
            "text": ai_session.answer_text,
            "status": "canceled",
            "asr_text": ai_session.asr_text,
            "answer_text": ai_session.answer_text,
            "audio_ready": False,
            "reply_wav_ready": False,
            "reply_wav_size": 0,
            "reply_duration": 0,
            "tts_status": "canceled",
            "tts_error": None,
        }

    def canceled_response(session_id: str) -> dict[str, object]:
        return {
            "ok": True,
            "session": session_id,
            "status": "canceled",
            "message": "session canceled",
        }

    def reply_duration_seconds(reply: bytes) -> float:
        wav = parse_wav(reply)
        if wav is None:
            return 0.0
        bytes_per_sample = wav.channels * wav.bits_per_sample // 8
        if bytes_per_sample <= 0 or wav.sample_rate <= 0:
            return 0.0
        return wav.data_size / bytes_per_sample / wav.sample_rate

    async def generate_tts_background(session_id: str, answer_text: str) -> None:
        tts_start = time.perf_counter()
        with app.state.ai_sessions_lock:
            ai_session = app.state.ai_sessions.get(session_id)
            if ai_session is None:
                log(f"[TTS-BG] missing session={session_id}")
                return
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"TTS skipped because canceled session={session_id}")
                return
            if ai_session.audio_ready or ai_session.reply_wav_ready:
                log(f"[TTS-BG] skip session={session_id} reason=audio_ready")
                return
            if ai_session.tts_status == "running":
                log(f"[TTS-BG] skip session={session_id} reason=already_running")
                return

            ai_session.tts_status = "running"
            ai_session.tts_error = None
        log(f"[TTS-BG] start session={session_id}")
        try:
            reply = await asyncio.to_thread(synthesize_wav_16k, answer_text)
            with app.state.ai_sessions_lock:
                if is_session_canceled(ai_session):
                    mark_session_canceled(ai_session)
                    log(f"TTS result ignored because canceled session={session_id}")
                    return
            if parse_wav(reply) is None:
                raise RuntimeError("TTS generated invalid reply wav")

            write_start = time.perf_counter()
            app.state.reply_save_dir.mkdir(parents=True, exist_ok=True)
            reply_path = app.state.reply_save_dir / f"reply_{session_id}.wav"
            reply_path.write_bytes(reply)
            app.state.latest_dir.mkdir(parents=True, exist_ok=True)
            (app.state.latest_dir / "reply.wav").write_bytes(reply)
            log(f"[AI-TIME] write_reply={time.perf_counter() - write_start:.3f}s reply_wav={reply_path}")

            with app.state.ai_sessions_lock:
                if is_session_canceled(ai_session):
                    mark_session_canceled(ai_session)
                    log(f"TTS result ignored because canceled session={session_id}")
                    return
                ai_session.reply = reply
                ai_session.reply_path = reply_path
                ai_session.reply_wav_size = reply_path.stat().st_size
                ai_session.reply_duration = reply_duration_seconds(reply)
                ai_session.audio_ready = True
                ai_session.reply_wav_ready = True
                ai_session.tts_status = "done"
                ai_session.status = "audio_ready"
            cost = time.perf_counter() - tts_start
            log(f"[TTS-BG] done session={session_id} wav={reply_path} cost={cost:.3f}s")
            log(f"[AI-TIME] tts_background={cost:.3f}s")
        except Exception as exc:
            logger.exception("[TTS-BG] failed session=%s", session_id)
            with app.state.ai_sessions_lock:
                if is_session_canceled(ai_session):
                    mark_session_canceled(ai_session)
                    log(f"TTS result ignored because canceled session={session_id}")
                    return
                ai_session.status = "audio_failed"
                ai_session.audio_ready = False
                ai_session.reply_wav_ready = False
                ai_session.tts_status = "failed"
                ai_session.tts_error = str(exc)[:300]
            cost = time.perf_counter() - tts_start
            log(f"[TTS-BG] failed session={session_id} error={ai_session.tts_error}")
            log(f"[AI-TIME] tts_background={cost:.3f}s error={ai_session.tts_error}")

    def maybe_start_tts_background(ai_session: AiSession) -> None:
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"TTS skipped because canceled session={ai_session.session_id}")
                return
        if not ai_session.answer_text.strip():
            ai_session.tts_status = "disabled"
            return
        if not auto_tts_background_enabled():
            ai_session.tts_status = "disabled"
            return
        if ai_session.tts_task is not None and not ai_session.tts_task.done():
            return
        if ai_session.audio_ready or ai_session.reply_wav_ready:
            return
        ai_session.tts_status = "pending"
        ai_session.tts_task = asyncio.create_task(generate_tts_background(ai_session.session_id, ai_session.answer_text))

    async def process_text_with_cancel(
        ai_session: AiSession,
        wav_path: Path,
        *,
        spot_id: str,
        image_context: str,
        mode: str,
    ) -> tuple[str, str]:
        if mode == "fixed":
            with app.state.ai_sessions_lock:
                if is_session_canceled(ai_session):
                    log(f"LLM skipped because canceled session={ai_session.session_id}")
                    return "", ""
            return "", FIXED_ANSWER
        if mode != "asr_bailian_app":
            raise ValueError(f"unsupported TOUR_MODE: {mode}")

        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                log(f"ASR skipped because canceled session={ai_session.session_id}")
                return "", ""

        asr_start = time.perf_counter()
        try:
            asr_text = await asyncio.to_thread(transcribe_wav, wav_path)
        except Exception as exc:
            print(f"[AI-TIME] asr={time.perf_counter() - asr_start:.3f}s error={exc}", flush=True)
            raise RuntimeError(f"ASR failed: {exc}") from exc
        print(f"[AI] asr_text: {asr_text}", flush=True)
        print(f"[AI-TIME] asr={time.perf_counter() - asr_start:.3f}s text_chars={len(asr_text)}", flush=True)

        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                log(f"ASR result ignored because canceled session={ai_session.session_id}")
                return "", ""

        if is_latest_image_question(asr_text):
            with app.state.ai_sessions_lock:
                if is_session_canceled(ai_session):
                    log(f"image answer skipped because canceled session={ai_session.session_id}")
                    return "", ""
            answer_text = await answer_latest_image_question(ai_session.device)
            with app.state.ai_sessions_lock:
                if is_session_canceled(ai_session):
                    log(f"image answer ignored because canceled session={ai_session.session_id}")
                    return asr_text, ""
            return asr_text, answer_text

        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                log(f"LLM skipped because canceled session={ai_session.session_id}")
                return "", ""

        try:
            answer_text = await asyncio.to_thread(
                app.state.tour_orchestrator._ask_llm,
                asr_text,
                device=ai_session.device,
                spot_id=spot_id,
                image_context=image_context,
            )
        except Exception as exc:
            raise RuntimeError(f"Bailian app failed: {exc}") from exc
        print(f"[AI] answer_text chars: {len(answer_text)}", flush=True)

        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                log(f"LLM result ignored because canceled session={ai_session.session_id}")
                return asr_text, ""

        return asr_text, answer_text

    async def log_request(request: Request, body: bytes, op: str) -> None:
        content_type = request.headers.get("content-type", "")
        log(f"HTTP POST {request.url.path}?{request.url.query} len={len(body)} content_type={content_type!r} route_op={op!r}")

    @app.get("/debug/camera_guide/test")
    async def debug_camera_guide_test() -> JSONResponse:
        result = await run_camera_guide_debug_test(
            vision_service=app.state.vision_service,
            bailian_app_service=app.state.bailian_app_service,
            test_image_path=DEFAULT_CAMERA_TEST_IMAGE,
        )
        return JSONResponse(result, status_code=200 if result.get("ok") else 500)

    async def analyze_camera_observation(safe_device: str, image_id: str, image_path: Path) -> VisionObservation:
        vision_start = time.perf_counter()
        try:
            observation = await asyncio.to_thread(app.state.vision_service.analyze_image, image_path)
            status = "retake" if choose_mode(observation) == RETAKE_MODE else "ready"
            error = ""
        except Exception as exc:
            logger.exception("[CAMERA] vision failed device=%s image_id=%s", safe_device, image_id)
            observation = VisionObservation(reason=f"视觉识别异常：{exc}")
            status = "failed"
            error = str(exc)[:300]

        app.state.latest_image_analysis[safe_device] = {
            "image_id": image_id,
            "path": image_path,
            "time": datetime.now(),
            "status": status,
            "observation": observation,
            "error": error,
        }
        log(
            f"[CAMERA] vision image_id={image_id} status={status} "
            f"best_candidate_id={observation.best_candidate_id} "
            f"candidate_confidence={observation.candidate_confidence:.2f} "
            f"category={observation.category} safe_answer_level={observation.safe_answer_level} "
            f"retake={int(observation.need_retake)} selected_mode={choose_mode(observation)} "
            f"cost={time.perf_counter() - vision_start:.3f}s"
        )
        return observation

    def is_latest_image_question(text: str) -> bool:
        normalized = (text or "").strip()
        if not normalized:
            return False
        keywords = (
            "照片",
            "图片",
            "拍的",
            "刚拍",
            "这个是什么",
            "这是什么",
            "这个展品",
            "这件展品",
            "这个文物",
            "这件文物",
            "讲讲这个",
            "看看这个",
            "识别一下",
        )
        return any(keyword in normalized for keyword in keywords)

    async def answer_latest_image_question(safe_device: str) -> str:
        cached = app.state.latest_image_analysis.get(safe_device)
        if cached is None and safe_device != "walkie-01":
            cached = app.state.latest_image_analysis.get("walkie-01")
        if not isinstance(cached, dict):
            return "我还没有收到可以讲解的照片。你可以先拍一张展品，尽量让展品居中，再来问我。"

        observation = cached.get("observation")
        image_id = str(cached.get("image_id") or "")
        if not isinstance(observation, VisionObservation):
            return "这张照片信息不太够。请把展品放在画面中间，靠近一点，避开展柜反光后重拍。"

        guide = await asyncio.to_thread(
            app.state.photo_guide_service.build_answer,
            observation,
            device=safe_device,
            image_id=image_id,
        )
        log(
            f"[CAMERA] voice uses cached image device={safe_device} image_id={image_id} "
            f"mode={guide.mode} grounded={int(guide.grounded)} answer_chars={len(guide.answer_text)}"
        )
        return guide.answer_text

    @app.post("/ai/start")
    async def ai_start(request: Request) -> dict[str, object]:
        body = await request.body()
        await log_request(request, body, "start")
        body_json: dict[str, object] = {}
        if body:
            try:
                parsed = json.loads(body.decode("utf-8"))
                if isinstance(parsed, dict):
                    body_json = parsed
                else:
                    log("AI start JSON body is not an object; using defaults")
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                log(f"AI start JSON parse failed: {exc}; using defaults")
        device = str(body_json.get("device") or "walkie-01")
        language = str(body_json.get("language") or "zh")
        session_id = uuid.uuid4().hex[:12]
        with app.state.ai_sessions_lock:
            app.state.ai_sessions[session_id] = AiSession(
                session_id=session_id,
                device=device,
                language=language,
            )
        log(f"AI start session={session_id} device={device} language={language}")
        return {"session": session_id, "chunk_size": DEFAULT_CHUNK_SIZE}

    @app.post("/ai/cancel")
    async def ai_cancel(request: Request, session: str = Query(...)) -> dict[str, object]:
        body = await request.body()
        await log_request(request, body, "cancel")
        log(f"cancel requested session={session}")
        with app.state.ai_sessions_lock:
            ai_session = app.state.ai_sessions.get(session)
            if ai_session is None:
                log(f"cancel unknown session={session}")
                return {
                    "ok": False,
                    "session": session,
                    "status": "not_found",
                    "error": "session not found",
                }
            mark_session_canceled(ai_session)
        log(f"cancel accepted session={session}")
        return canceled_response(session)

    @app.post("/ai/upload")
    async def ai_upload(
        request: Request,
        session: str = Query(...),
        index: int = Query(0),
        offset: int = Query(0),
        total: int = Query(0),
    ) -> dict[str, bool]:
        body = await request.body()
        await log_request(request, body, "upload")
        ai_session = get_session(session)
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"upload rejected because canceled session={session}")
                raise HTTPException(
                    status_code=409,
                    detail={"ok": False, "status": "canceled", "error": "session canceled"},
                )
        if total <= 0 or offset < 0 or offset + len(body) > total:
            raise HTTPException(status_code=400, detail={"ok": False, "error": "upload range invalid"})
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"upload rejected because canceled session={session}")
                raise HTTPException(
                    status_code=409,
                    detail={"ok": False, "status": "canceled", "error": "session canceled"},
                )
            if ai_session.chunks is None:
                ai_session.status = "uploading"
                ai_session.total = total
                ai_session.chunks = bytearray(total)
            if total != ai_session.total or ai_session.chunks is None:
                raise HTTPException(status_code=409, detail={"ok": False, "error": "total changed"})
            ai_session.chunks[offset : offset + len(body)] = body
            ai_session.received += len(body)
        log(
            f"AI upload session={session} index={index} offset={offset} "
            f"len={len(body)} received={ai_session.received}/{ai_session.total}"
        )
        return {"ok": True}

    @app.post("/ai/finish")
    async def ai_finish(request: Request, session: str = Query(...)) -> dict[str, object]:
        total_start = time.perf_counter()
        body = await request.body()
        await log_request(request, body, "finish")
        ai_session = get_session(session)
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"finish ignored because canceled session={session}")
                return {"ok": True, "status": "canceled"}
            if ai_session.chunks is None or ai_session.total <= 0:
                raise HTTPException(status_code=400, detail={"ok": False, "error": "no upload"})
            if ai_session.received < ai_session.total:
                raise HTTPException(status_code=409, detail={"ok": False, "error": "upload incomplete"})
            ai_session.status = "processing"
            full_wav = bytes(ai_session.chunks)
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"finish ignored because canceled session={session}")
                return {"ok": True, "status": "canceled"}
        save_start = time.perf_counter()
        try:
            ok, save_path = validate_and_log_wav(full_wav, app.state.save_dir, f"AI finish session={session}")
        except Exception as exc:
            log(f"[AI-TIME] save_upload={time.perf_counter() - save_start:.3f}s error={exc}")
            log(f"[AI-TIME] total={time.perf_counter() - total_start:.3f}s error={exc}")
            raise
        log(f"[AI-TIME] save_upload={time.perf_counter() - save_start:.3f}s")
        if not ok:
            log(f"[AI-TIME] total={time.perf_counter() - total_start:.3f}s error=invalid wav")
            raise HTTPException(status_code=400, detail={"ok": False, "error": "invalid wav"})
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"finish ignored because canceled session={session}")
                return {"ok": True, "status": "canceled"}
            ai_session.upload_wav_path = save_path
        log(f"[AI] uploaded wav: {save_path}")
        spot_id = os.getenv("TOUR_DEFAULT_SPOT_ID", "dayanta")
        mode = os.getenv("TOUR_MODE", "asr_bailian_app")
        log(f"[AI] mode={mode} llm_provider=bailian_app")
        image_context = ai_session.image_context
        try:
            asr_text, answer_text = await process_text_with_cancel(
                ai_session,
                save_path,
                spot_id=spot_id,
                image_context=image_context,
                mode=mode,
            )
        except Exception as exc:
            if str(exc).startswith("ASR failed"):
                log(f"AI ASR failed session={session}: {exc}")
                log(f"[AI-TIME] finish_text_total={time.perf_counter() - total_start:.3f}s error={exc}")
                raise HTTPException(status_code=500, detail={"ok": False, "error": "asr failed"})
            log(f"AI orchestration failed session={session}: {exc}")
            answer_text = ERROR_TEXT
            asr_text = ai_session.asr_text

        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"LLM result ignored because canceled session={session}")
                return {"ok": True, "status": "canceled"}
            ai_session.asr_text = asr_text
            ai_session.save_path = save_path
            ai_session.answer_text = answer_text
            ai_session.status = "text_ready"
            ai_session.audio_ready = False
            ai_session.reply_wav_ready = False
            ai_session.reply = None
            ai_session.reply_path = None
            ai_session.reply_wav_size = 0
            ai_session.reply_duration = 0.0
            ai_session.tts_error = None
            ai_session.tts_status = "pending" if answer_text.strip() and auto_tts_background_enabled() else "disabled"
        log(f"[AI] text_ready session={session} answer_chars={len(answer_text)}")
        log(f"[AI-TIME] finish_text_total={time.perf_counter() - total_start:.3f}s")
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"TTS skipped because canceled session={session}")
                return {"ok": True, "status": "canceled"}
        maybe_start_tts_background(ai_session)
        return {"ok": True, "status": "processing"}

    @app.post("/ai/result_info")
    async def ai_result_info(request: Request, session: str = Query(...)) -> dict[str, object]:
        body = await request.body()
        await log_request(request, body, "result_info")
        ai_session = get_session(session)
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                return canceled_result_info(session, ai_session)
        reply_len = 0
        with app.state.ai_sessions_lock:
            if ai_session.reply_wav_ready:
                reply_len = (
                    ai_session.reply_path.stat().st_size
                    if ai_session.reply_path and ai_session.reply_path.exists()
                    else len(ai_session.reply or b"")
                )
                ai_session.reply_wav_size = reply_len
            return {
                "ok": True,
                "session": session,
                "ready": ai_session.reply_wav_ready,
                "total": reply_len,
                "format": "wav",
                "text": ai_session.answer_text,
                "status": ai_session.status,
                "asr_text": ai_session.asr_text,
                "answer_text": ai_session.answer_text,
                "audio_ready": ai_session.audio_ready,
                "reply_wav_ready": ai_session.reply_wav_ready,
                "reply_wav_size": ai_session.reply_wav_size,
                "reply_duration": ai_session.reply_duration,
                "tts_status": ai_session.tts_status,
                "tts_error": ai_session.tts_error,
            }

    @app.post("/ai/result_chunk")
    async def ai_result_chunk(
        request: Request,
        session: str = Query(...),
        offset: int = Query(0),
        len_: int = Query(DEFAULT_CHUNK_SIZE, alias="len"),
    ) -> Response:
        body = await request.body()
        await log_request(request, body, "result_chunk")
        ai_session = get_session(session)
        with app.state.ai_sessions_lock:
            if is_session_canceled(ai_session):
                mark_session_canceled(ai_session)
                log(f"result_chunk rejected because canceled session={session}")
                return JSONResponse(
                    {"ok": False, "status": "canceled", "error": "session canceled"},
                    status_code=409,
                )
            if ai_session.reply is None:
                return Response(b"not ready", status_code=409, media_type="text/plain")
            reply_path = ai_session.reply_path
            reply_bytes = ai_session.reply
        reply = reply_path.read_bytes() if reply_path and reply_path.exists() else reply_bytes
        if offset < 0 or len_ <= 0 or offset >= len(reply):
            return Response(b"range invalid", status_code=416, media_type="text/plain")
        chunk = reply[offset : offset + len_]
        log(f"AI result_chunk session={session} offset={offset} len={len(chunk)}")
        return Response(chunk, media_type="application/octet-stream")

    @app.post("/camera/upload")
    async def camera_upload(
        request: Request,
        content_type: str = Header("", alias="content-type"),
        device: str = Query("walkie-01"),
    ) -> JSONResponse:
        body = await request.body()
        await log_request(request, body, "camera_upload")
        if "image/jpeg" not in content_type.lower() and "image/jpg" not in content_type.lower():
            log(f"Camera upload content-type warning: {content_type!r}")

        ok, save_path, jpeg = validate_and_log_jpeg(body, app.state.jpg_save_dir, "Camera upload")
        if not ok or save_path is None:
            return JSONResponse(
                {
                    "ok": False,
                    "error": "invalid jpeg",
                    "len": len(body),
                    "file": save_path.as_posix() if save_path else "",
                },
                status_code=400,
            )

        width = jpeg.width if jpeg and jpeg.width is not None else 0
        height = jpeg.height if jpeg and jpeg.height is not None else 0
        safe_device = device or "walkie-01"
        image_id = save_path.stem
        app.state.latest_images[safe_device] = {
            "image_id": image_id,
            "path": save_path,
            "time": datetime.now(),
            "width": width,
            "height": height,
        }
        log(f"Camera latest image updated device={safe_device} image_id={image_id} file={save_path}")

        observation = await analyze_camera_observation(safe_device, image_id, save_path)
        mode = choose_mode(observation)
        analysis_ok = mode != RETAKE_MODE
        data = observation.to_dict()
        response_data = {
            "ok": True,
            "len": len(body),
            "width": width,
            "height": height,
            "file": save_path.as_posix(),
            "device": safe_device,
            "image_id": image_id,
            "analysis_ok": analysis_ok,
            "mode": mode,
            "best_candidate_id": data["best_candidate_id"],
            "best_candidate_name": data["best_candidate_name"],
            "candidate_confidence": data["candidate_confidence"],
            "category": data["category"],
            "top_candidates": data["top_candidates"],
            "visible_features": data["visible_features"],
            "visual_evidence": data["visual_evidence"],
            "risk": data["risk"],
            "safe_answer_level": data["safe_answer_level"],
            "need_retake": data["need_retake"] or mode == RETAKE_MODE,
            "grounded": False,
            "answer_text": ""
            if analysis_ok
            else "这张照片信息不太够。请把展品放在画面中间，靠近一点，避开展柜反光后重拍。",
            # Compatibility fields for current client/debug tooling.
            "scene_type": data["scene_type"],
            "object_category": data["object_category"],
            "visual_features": data["visual_features"],
            "readable_text": data["readable_text"],
            "possible_subject": data["possible_subject"],
            "category_confidence": data["category_confidence"],
            "specific_name_confidence": data["specific_name_confidence"],
        }
        return JSONResponse(response_data)

    @app.post("/camera/analyze_latest")
    async def camera_analyze_latest(
        request: Request,
        device: str = Query("walkie-01"),
    ) -> JSONResponse:
        body = await request.body()
        await log_request(request, body, "camera_analyze_latest")
        safe_device = device or "walkie-01"
        latest = app.state.latest_images.get(safe_device)
        if latest is None and safe_device != "walkie-01":
            latest = app.state.latest_images.get("walkie-01")
        if latest is None:
            return JSONResponse(
                {"ok": False, "device": safe_device, "error": "no camera image uploaded"},
                status_code=404,
            )

        image_path = latest.get("path")
        if not isinstance(image_path, Path) or not image_path.exists():
            return JSONResponse(
                {"ok": False, "device": safe_device, "error": "latest image missing"},
                status_code=404,
            )

        image_id = str(latest.get("image_id") or image_path.stem)
        cached = app.state.latest_image_analysis.get(safe_device)
        if (
            isinstance(cached, dict)
            and cached.get("image_id") == image_id
            and isinstance(cached.get("observation"), VisionObservation)
        ):
            observation = cached["observation"]
            log(f"[CAMERA] use cached vision image_id={image_id} status={cached.get('status')}")
        else:
            observation = await analyze_camera_observation(safe_device, image_id, image_path)

        guide_start = time.perf_counter()
        guide = await asyncio.to_thread(
            app.state.photo_guide_service.build_answer,
            observation,
            device=safe_device,
            image_id=image_id,
        )
        log(
            f"[CAMERA] guide image_id={image_id} mode={guide.mode} grounded={int(guide.grounded)} "
            f"answer_chars={len(guide.answer_text)} cost={time.perf_counter() - guide_start:.3f}s"
        )
        return JSONResponse(
            response_payload(
                device=safe_device,
                image_id=image_id,
                observation=observation,
                guide=guide,
            )
        )

    @app.post("/ai/wav")
    async def ai_wav_oneshot(request: Request) -> Response:
        body = await request.body()
        await log_request(request, body, "one_shot")
        if parse_wav(body) is None:
            return Response(b"expected audio/wav", status_code=400, media_type="text/plain")
        validate_and_log_wav(body, app.state.save_dir, "HTTP one-shot")
        return Response(body, media_type="audio/wav")

    return app


def run_http(
    host: str,
    port: int,
    wav_save_dir: Path,
    jpg_save_dir: Path,
    ai_reply_repeat: int,
    ai_reply_extra_chunk: bool,
) -> None:
    app = create_http_app(wav_save_dir, jpg_save_dir, ai_reply_repeat, ai_reply_extra_chunk)
    log(f"FastAPI AI WAV + camera JPEG test listening on {host}:{port}")
    log(f"AI base URL: http://<PC_LAN_IP>:{port}")
    log(f"AI reply repeat={max(ai_reply_repeat, 1)} extra_chunk={int(ai_reply_extra_chunk)}")
    log(f"Camera upload URL: http://<PC_LAN_IP>:{port}/camera/upload")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def main() -> None:
    parser = argparse.ArgumentParser(description="Walkie business test server")
    parser.add_argument("--host", default=DEFAULT_BIND_HOST, help="bind address")
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT, help="WTK1 UDP listen port")
    parser.add_argument("--http-port", type=int, default=DEFAULT_HTTP_PORT, help="AI WAV HTTP port")
    parser.add_argument("--wav-save-dir", default=str(DEFAULT_WAV_SAVE_DIR), help="directory for received WAV files")
    parser.add_argument("--jpg-save-dir", default=str(DEFAULT_JPG_SAVE_DIR), help="directory for received JPEG files")
    parser.add_argument(
        "--ai-reply-repeat",
        type=int,
        default=DEFAULT_AI_REPLY_REPEAT,
        help="repeat uploaded PCM this many times in AI reply WAV",
    )
    parser.add_argument(
        "--ai-reply-extra-chunk",
        action="store_true",
        default=DEFAULT_AI_REPLY_EXTRA_CHUNK,
        help="insert a JUNK chunk before reply data to test non-44-byte WAV data offsets",
    )
    args = parser.parse_args()

    threading.Thread(target=run_udp, args=(args.host, args.udp_port), daemon=True).start()
    threading.Thread(
        target=run_http,
        args=(
            args.host,
            args.http_port,
            Path(args.wav_save_dir),
            Path(args.jpg_save_dir),
            args.ai_reply_repeat,
            args.ai_reply_extra_chunk,
        ),
        daemon=True,
    ).start()

    log("Press Ctrl+C to stop")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        log("Stopped")


if __name__ == "__main__":
    main()

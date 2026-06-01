from __future__ import annotations

import asyncio
import math
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path


SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
ERROR_TEXT = "抱歉，当前导游服务暂时不可用。"


def _log(message: str) -> None:
    print(f"[TTS] {message}", flush=True)


def _pcm16_wav(pcm: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = Path(tmp.name)
    try:
        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        return path.read_bytes()
    finally:
        path.unlink(missing_ok=True)


def _silence_wav(duration_seconds: float = 1.0) -> bytes:
    samples = max(1, int(SAMPLE_RATE * duration_seconds))
    return _pcm16_wav(b"\x00\x00" * samples)


def _mock_tts_wav(text: str) -> bytes:
    duration_seconds = min(max(1.0, len(text) * 0.09), 8.0)
    sample_count = int(SAMPLE_RATE * duration_seconds)
    amplitude = 4500
    pcm = bytearray()
    for i in range(sample_count):
        envelope = min(i / 800, (sample_count - i) / 800, 1.0)
        value = int(amplitude * max(envelope, 0.0) * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
        pcm.extend(struct.pack("<h", value))
    return _pcm16_wav(bytes(pcm))


async def _edge_tts_to_mp3(text: str, mp3_path: Path) -> None:
    import edge_tts

    voice = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(mp3_path))


def _run_coro_sync(coro) -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return

    error: list[BaseException] = []

    def runner() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:  # pragma: no cover - defensive bridge
            error.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    thread.join()
    if error:
        raise error[0]


def _convert_with_ffmpeg(input_path: Path, output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; install ffmpeg or use TTS_PROVIDER=mock")

    command = [
        ffmpeg,
        "-y",
        "-i",
        str(input_path),
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-sample_fmt",
        "s16",
        str(output_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()}")


def _validate_wav_16k(wav_bytes: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = Path(tmp.name)
        tmp.write(wav_bytes)
    try:
        with wave.open(str(path), "rb") as wf:
            if wf.getnchannels() != CHANNELS:
                raise RuntimeError(f"invalid channels: {wf.getnchannels()}")
            if wf.getframerate() != SAMPLE_RATE:
                raise RuntimeError(f"invalid sample rate: {wf.getframerate()}")
            if wf.getsampwidth() != SAMPLE_WIDTH:
                raise RuntimeError(f"invalid sample width: {wf.getsampwidth()}")
        return wav_bytes
    finally:
        path.unlink(missing_ok=True)


def _synthesize_with_edge_tts(text: str) -> bytes:
    with tempfile.TemporaryDirectory() as tmp_dir:
        mp3_path = Path(tmp_dir) / "tts.mp3"
        wav_path = Path(tmp_dir) / "tts.wav"
        _run_coro_sync(_edge_tts_to_mp3(text, mp3_path))
        _convert_with_ffmpeg(mp3_path, wav_path)
        return _validate_wav_16k(wav_path.read_bytes())


def synthesize_wav_16k(text: str) -> bytes:
    provider = os.getenv("TTS_PROVIDER", "mock").strip().lower() or "mock"
    safe_text = text.strip() or ERROR_TEXT

    try:
        if provider == "edge_tts":
            return _synthesize_with_edge_tts(safe_text)
        if provider != "mock":
            _log(f"unknown TTS_PROVIDER={provider!r}; using mock")
        return _mock_tts_wav(safe_text)
    except Exception as exc:
        _log(f"TTS failed for provider={provider}: {exc}")

    try:
        if provider == "edge_tts":
            return _synthesize_with_edge_tts(ERROR_TEXT)
        return _mock_tts_wav(ERROR_TEXT)
    except Exception as exc:
        _log(f"fallback TTS failed: {exc}; returning 1s silence")
        return _silence_wav(1.0)

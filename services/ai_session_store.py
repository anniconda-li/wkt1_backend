"""AI voice session state and thread-safe storage.

FastAPI routes mutate the same session while upload, cancel, text processing and
background TTS can overlap. This module centralizes the lock and the state
transitions that must stay consistent across those flows.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class AiSession:
    """State for one ASR -> LLM -> TTS voice interaction."""

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
    audio_stopped: bool = False


class AiSessionStore:
    """Thread-safe in-memory store for AI sessions."""

    def __init__(self) -> None:
        self.sessions: dict[str, AiSession] = {}
        self.lock = threading.RLock()

    def create(self, *, device: str, language: str) -> AiSession:
        """Create and store a new session."""
        session_id = uuid.uuid4().hex[:12]
        session = AiSession(session_id=session_id, device=device, language=language)
        with self.lock:
            self.sessions[session_id] = session
        return session

    def get(self, session_id: str) -> AiSession | None:
        """Return a session or ``None`` when it does not exist."""
        with self.lock:
            return self.sessions.get(session_id)

    def cancel(self, session_id: str) -> AiSession | None:
        """Mark a session canceled and return it, or ``None`` if unknown."""
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return None
            mark_session_canceled(session)
            return session


def is_session_canceled(ai_session: AiSession) -> bool:
    """Return whether the session should stop accepting work."""
    return ai_session.canceled or ai_session.status == "canceled"


def mark_session_canceled(ai_session: AiSession) -> None:
    """Move a session into the canceled state and hide any audio result."""
    ai_session.canceled = True
    ai_session.status = "canceled"
    ai_session.audio_ready = False
    ai_session.reply_wav_ready = False
    ai_session.reply_wav_size = 0
    ai_session.reply_duration = 0.0
    ai_session.tts_status = "canceled"
    ai_session.tts_error = None


def canceled_result_info(session_id: str, ai_session: AiSession) -> dict[str, object]:
    """Build the ``/ai/result_info`` payload for a canceled session."""
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
    """Build the response payload for a successful cancel request."""
    return {
        "ok": True,
        "session": session_id,
        "status": "canceled",
        "message": "session canceled",
    }

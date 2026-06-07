from __future__ import annotations

import os
from pathlib import Path

import core.config  # noqa: F401 - loads project .env
from server.walkie_app import (
    DEFAULT_AI_REPLY_EXTRA_CHUNK,
    DEFAULT_AI_REPLY_REPEAT,
    DEFAULT_JPG_SAVE_DIR,
    DEFAULT_WAV_SAVE_DIR,
    create_http_app,
)


app = create_http_app(
    Path(os.getenv("WAV_SAVE_DIR", str(DEFAULT_WAV_SAVE_DIR))),
    Path(os.getenv("JPG_SAVE_DIR", str(DEFAULT_JPG_SAVE_DIR))),
    int(os.getenv("AI_REPLY_REPEAT", str(DEFAULT_AI_REPLY_REPEAT))),
    os.getenv("AI_REPLY_EXTRA_CHUNK", str(int(DEFAULT_AI_REPLY_EXTRA_CHUNK))).strip().lower()
    in {"1", "true", "yes", "on"},
)

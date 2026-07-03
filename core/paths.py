"""Central filesystem paths for the WTK1 backend."""

from __future__ import annotations

import os
from pathlib import Path

from core.config import PROJECT_ROOT

# Curated data.
KNOWLEDGE_DIR = PROJECT_ROOT / "knowledge"
KNOWLEDGE_CONFIG_DIR = KNOWLEDGE_DIR / "config"
PHOTO_DIR = PROJECT_ROOT / "photo"

# Test fixtures.
TESTS_DIR = PROJECT_ROOT / "tests"
TEST_DATA_DIR = TESTS_DIR / "data"
TEST_CAMERA_DIR = TEST_DATA_DIR / "camera"
TEST_AUDIO_DIR = TEST_DATA_DIR / "audio"
DEFAULT_CAMERA_TEST_IMAGE = Path(
    os.getenv("DEFAULT_CAMERA_TEST_IMAGE", str(TEST_CAMERA_DIR / "yingguo_yuying.jpg"))
)
if not DEFAULT_CAMERA_TEST_IMAGE.is_absolute():
    DEFAULT_CAMERA_TEST_IMAGE = PROJECT_ROOT / DEFAULT_CAMERA_TEST_IMAGE

# Runtime outputs. Everything under tmp/ is disposable.
TMP_DIR = PROJECT_ROOT / "tmp"
TMP_CAMERA_DIR = TMP_DIR / "camera"
TMP_CAMERA_RECEIVED_DIR = TMP_CAMERA_DIR / "received"
TMP_CAMERA_PREPROCESS_DIR = TMP_CAMERA_DIR / "preprocess"
TMP_AUDIO_DIR = TMP_DIR / "audio"
TMP_AUDIO_RECEIVED_DIR = TMP_AUDIO_DIR / "received"
TMP_AUDIO_REPLIES_DIR = TMP_AUDIO_DIR / "replies"
TMP_DEBUG_DIR = TMP_DIR / "debug"
TMP_DEBUG_AUDIO_DIR = TMP_DEBUG_DIR / "audio"

RUNTIME_DIRS = (
    TMP_CAMERA_RECEIVED_DIR,
    TMP_CAMERA_PREPROCESS_DIR,
    TMP_AUDIO_RECEIVED_DIR,
    TMP_AUDIO_REPLIES_DIR,
    TMP_DEBUG_DIR,
)

PROJECT_DIRS = (
    KNOWLEDGE_DIR,
    KNOWLEDGE_CONFIG_DIR,
    PHOTO_DIR,
    TESTS_DIR,
    TEST_DATA_DIR,
    TEST_CAMERA_DIR,
    TEST_AUDIO_DIR,
    *RUNTIME_DIRS,
)


def ensure_project_dirs() -> None:
    """Create durable project directories and runtime output directories."""
    for path in PROJECT_DIRS:
        path.mkdir(parents=True, exist_ok=True)


def ensure_runtime_dirs() -> None:
    """Create runtime output directories used by the HTTP service."""
    for path in RUNTIME_DIRS:
        path.mkdir(parents=True, exist_ok=True)


def camera_test_image_info() -> dict[str, object]:
    """Return the configured camera test image path and existence flag."""
    return {
        "target_test_image": str(DEFAULT_CAMERA_TEST_IMAGE),
        "exists": DEFAULT_CAMERA_TEST_IMAGE.exists(),
    }


def env_path(name: str, default: Path) -> Path:
    """Read a path from an environment variable, resolving relative values from the project root."""
    value = os.getenv(name, "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path

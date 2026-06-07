from __future__ import annotations

import os
import shutil
from pathlib import Path

from core.config import PROJECT_ROOT


DATA_DIR = PROJECT_ROOT / "data"
MUSEUM_REFS_DIR = DATA_DIR / "museum_refs"
KNOWLEDGE_EXPORT_DIR = DATA_DIR / "knowledge_export"
KNOWLEDGE_DOCS_DIR = PROJECT_ROOT / "knowledge_docs"
EXHIBITS_KNOWLEDGE_DIR = KNOWLEDGE_DOCS_DIR / "exhibits"
CONFIG_DIR = PROJECT_ROOT / "config"

TMP_DIR = PROJECT_ROOT / "tmp"

TMP_CAMERA_DIR = TMP_DIR / "camera"
TMP_CAMERA_RECEIVED_DIR = TMP_CAMERA_DIR / "received"
TMP_CAMERA_LATEST_DIR = TMP_CAMERA_DIR / "latest"
TMP_CAMERA_TEST_DIR = TMP_CAMERA_DIR / "test"
TMP_CAMERA_PREPROCESS_DIR = TMP_CAMERA_DIR / "preprocess"

TMP_AUDIO_DIR = TMP_DIR / "audio"
TMP_AUDIO_RECEIVED_WAV_DIR = TMP_AUDIO_DIR / "received_wav"
TMP_AUDIO_REPLY_WAV_DIR = TMP_AUDIO_DIR / "reply_wav"
TMP_AUDIO_DEBUG_REPLY_WAV_DIR = TMP_AUDIO_DIR / "debug_reply_wav"

TMP_RUNS_DIR = TMP_DIR / "runs"
TMP_DEBUG_DIR = TMP_DIR / "debug"

# Legacy tmp paths kept only as constants for old callers. Runtime code should
# use the normalized tmp/camera, tmp/audio, tmp/runs, and tmp/debug structure.
LEGACY_CAMERA_PREPROCESS_DIR = TMP_DIR / "camera_preprocess"
LEGACY_CAMERA_PREPROCESS_TEST_DIR = TMP_DIR / "camera_preprocess_test"
LEGACY_LATEST_DIR = TMP_DIR / "latest"
LEGACY_PHOTOS_DIR = TMP_DIR / "photos"
LEGACY_RECEIVED_JPG_DIR = TMP_DIR / "received_jpg"
LEGACY_RECEIVED_WAV_DIR = TMP_DIR / "received_wav"
LEGACY_REPLY_WAV_DIR = TMP_DIR / "reply_wav"
LEGACY_DEBUG_REPLY_WAV_DIR = TMP_DIR / "debug_reply_wav"
LEGACY_TEST_AI_CANCEL_DIR = TMP_DIR / "test_ai_cancel"
LEGACY_TEST_JPG_DIR = TMP_DIR / "test_jpg"
LEGACY_CAMERA_TEST_IMAGE = LEGACY_RECEIVED_JPG_DIR / "camera_upload_20260603_165431_081287.jpg"

DEFAULT_CAMERA_TEST_IMAGE = Path(
    os.getenv(
        "DEFAULT_CAMERA_TEST_IMAGE",
        str(TMP_CAMERA_TEST_DIR / "camera_upload_20260603_165431_081287.jpg"),
    )
)
if not DEFAULT_CAMERA_TEST_IMAGE.is_absolute():
    DEFAULT_CAMERA_TEST_IMAGE = PROJECT_ROOT / DEFAULT_CAMERA_TEST_IMAGE

RUNTIME_DIRS = (
    TMP_CAMERA_RECEIVED_DIR,
    TMP_CAMERA_LATEST_DIR,
    TMP_CAMERA_TEST_DIR,
    TMP_CAMERA_PREPROCESS_DIR,
    TMP_AUDIO_RECEIVED_WAV_DIR,
    TMP_AUDIO_REPLY_WAV_DIR,
    TMP_AUDIO_DEBUG_REPLY_WAV_DIR,
    TMP_RUNS_DIR,
    TMP_DEBUG_DIR,
)

MUSEUM_REF_IDS = (
    "yingguo_yuying",
    "panlongniu_daigai_tonghe",
    "denggong_gui",
    "lushan_huaci_sanzuxi",
    "junyao_tianqingyou_bo",
    "shuyao_chuilinwen_shengding",
)

PROJECT_DIRS = (
    DATA_DIR,
    MUSEUM_REFS_DIR,
    KNOWLEDGE_EXPORT_DIR,
    KNOWLEDGE_DOCS_DIR,
    EXHIBITS_KNOWLEDGE_DIR,
    CONFIG_DIR,
    *(MUSEUM_REFS_DIR / ref_id for ref_id in MUSEUM_REF_IDS),
    *RUNTIME_DIRS,
)

LEGACY_RUNTIME_DIRS = (
    LEGACY_CAMERA_PREPROCESS_DIR,
    LEGACY_CAMERA_PREPROCESS_TEST_DIR,
    LEGACY_LATEST_DIR,
    LEGACY_PHOTOS_DIR,
    LEGACY_RECEIVED_JPG_DIR,
    LEGACY_RECEIVED_WAV_DIR,
    LEGACY_REPLY_WAV_DIR,
    LEGACY_DEBUG_REPLY_WAV_DIR,
    LEGACY_TEST_AI_CANCEL_DIR,
    LEGACY_TEST_JPG_DIR,
)


def ensure_project_dirs() -> None:
    for path in PROJECT_DIRS:
        path.mkdir(parents=True, exist_ok=True)
    ensure_default_camera_test_image()


def ensure_runtime_dirs() -> None:
    ensure_project_dirs()


def ensure_default_camera_test_image() -> dict[str, object]:
    target = DEFAULT_CAMERA_TEST_IMAGE
    source = LEGACY_CAMERA_TEST_IMAGE
    copied = False
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() and source.exists():
        shutil.copy2(source, target)
        copied = True
    info = {
        "source_test_image": str(source),
        "target_test_image": str(target),
        "copied_from_legacy": copied,
    }
    print(
        "[PATHS] "
        f"source_test_image={info['source_test_image']} "
        f"target_test_image={info['target_test_image']} "
        f"copied_from_legacy={str(copied).lower()}",
        flush=True,
    )
    return info


def env_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path

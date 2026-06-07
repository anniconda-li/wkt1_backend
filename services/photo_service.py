from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core.paths import TMP_CAMERA_RECEIVED_DIR


MAX_PHOTO_BYTES = 4 * 1024 * 1024
DEFAULT_PHOTO_DIR = TMP_CAMERA_RECEIVED_DIR
DEVICE_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class SavedPhoto:
    image_id: str
    filename: str
    size: int
    path: Path


class PhotoValidationError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def sanitize_device(device: str | None) -> str:
    cleaned = DEVICE_PATTERN.sub("_", (device or "unknown_device").strip())
    cleaned = cleaned.strip("_-")
    return cleaned or "unknown_device"


def save_jpeg_photo(body: bytes, device: str | None = None, photo_dir: Path = DEFAULT_PHOTO_DIR) -> SavedPhoto:
    size = len(body)
    if size <= 0:
        raise PhotoValidationError("empty image body")
    if size > MAX_PHOTO_BYTES:
        raise PhotoValidationError("image too large", status_code=413)
    if len(body) < 4 or body[:2] != b"\xff\xd8":
        raise PhotoValidationError("invalid jpeg header")
    if body[-2:] != b"\xff\xd9":
        raise PhotoValidationError("invalid jpeg trailer")

    safe_device = sanitize_device(device)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_id = f"{safe_device}_{timestamp}_{secrets.token_hex(4)}"
    filename = f"{image_id}.jpg"

    photo_dir.mkdir(parents=True, exist_ok=True)
    path = photo_dir / filename
    path.write_bytes(body)
    return SavedPhoto(image_id=image_id, filename=filename, size=size, path=path)

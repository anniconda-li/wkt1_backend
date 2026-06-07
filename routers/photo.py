from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse

from services.photo_service import DEFAULT_PHOTO_DIR, PhotoValidationError, save_jpeg_photo, sanitize_device


logger = logging.getLogger("photo")
router = APIRouter(prefix="/photo", tags=["photo"])


@router.post("/upload")
async def upload_photo(
    request: Request,
    device: str = Query("unknown_device"),
    content_type: str = Header("", alias="content-type"),
) -> JSONResponse:
    if "image/jpeg" not in content_type.lower():
        return JSONResponse({"ok": False, "error": "content-type must be image/jpeg"}, status_code=400)

    body = await request.body()
    safe_device = sanitize_device(device)
    try:
        saved = save_jpeg_photo(body, device=safe_device, photo_dir=Path(DEFAULT_PHOTO_DIR))
    except PhotoValidationError as exc:
        logger.error("photo upload failed device=%s error=%s size=%s", safe_device, exc, len(body))
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=exc.status_code)

    logger.info("photo uploaded device=%s filename=%s size=%s", safe_device, saved.filename, saved.size)
    print(f"[photo] uploaded device={safe_device} filename={saved.filename} size={saved.size}", flush=True)
    return JSONResponse(
        {
            "ok": True,
            "image_id": saved.image_id,
            "filename": saved.filename,
            "size": saved.size,
        }
    )

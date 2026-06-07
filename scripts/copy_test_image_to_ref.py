from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import (
    DEFAULT_CAMERA_TEST_IMAGE,
    LEGACY_CAMERA_TEST_IMAGE,
    MUSEUM_REFS_DIR,
    ensure_project_dirs,
)


TARGET_IMAGE = MUSEUM_REFS_DIR / "yingguo_yuying" / "ref_1.jpg"


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy current camera test image to Yingguo Yuying reference image.")
    parser.add_argument("--overwrite", action="store_true", help="overwrite target if it already exists")
    args = parser.parse_args()

    ensure_project_dirs()
    source = _select_source()
    result = {
        "source": str(source) if source else "",
        "target": str(TARGET_IMAGE),
        "copied": False,
        "skipped_reason": "",
    }

    if source is None:
        result["skipped_reason"] = (
            "source image not found; put camera_upload_20260603_165431_081287.jpg under tmp/camera/test/"
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1

    if TARGET_IMAGE.exists() and not args.overwrite:
        result["skipped_reason"] = "target exists; pass --overwrite to replace it"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    TARGET_IMAGE.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, TARGET_IMAGE)
    result["copied"] = True
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def _select_source() -> Path | None:
    if DEFAULT_CAMERA_TEST_IMAGE.exists():
        return DEFAULT_CAMERA_TEST_IMAGE
    if LEGACY_CAMERA_TEST_IMAGE.exists():
        return LEGACY_CAMERA_TEST_IMAGE
    return None


if __name__ == "__main__":
    sys.exit(main())

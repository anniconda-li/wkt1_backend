from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.config  # noqa: E402,F401 - loads project .env
from core.paths import DEFAULT_CAMERA_TEST_IMAGE, ensure_project_dirs
from services.bailian_app_service import BailianAppService
from services.camera_guide_debug_service import DEFAULT_CAMERA_GUIDE_TEST_TEXT, run_camera_guide_debug_test
from services.vision_service import VisionService


def main() -> int:
    parser = argparse.ArgumentParser(description="Run camera guide debug flow once.")
    parser.add_argument(
        "--image",
        default=str(DEFAULT_CAMERA_TEST_IMAGE),
        help="test image path; defaults to DEFAULT_CAMERA_TEST_IMAGE",
    )
    parser.add_argument(
        "--text",
        default=DEFAULT_CAMERA_GUIDE_TEST_TEXT,
        help="fixed user question",
    )
    args = parser.parse_args()

    ensure_project_dirs()
    result = asyncio.run(
        run_camera_guide_debug_test(
            vision_service=VisionService(),
            bailian_app_service=BailianAppService(),
            test_image_path=Path(args.image),
            user_text=args.text,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())

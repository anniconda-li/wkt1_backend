from __future__ import annotations

import sys

from core.paths import DEFAULT_CAMERA_TEST_IMAGE, RUNTIME_DIRS, TMP_DIR, ensure_project_dirs


def main() -> int:
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    ensure_project_dirs()
    print("[OK] tmp/project directories ensured")
    print(f"[OK] runtime_dirs={len(RUNTIME_DIRS)}")
    print(f"[OK] default_test_image={DEFAULT_CAMERA_TEST_IMAGE}")
    print("[INFO] no files were deleted")
    return 0


if __name__ == "__main__":
    sys.exit(main())

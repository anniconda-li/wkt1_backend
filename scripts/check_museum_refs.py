from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import CONFIG_DIR, ensure_project_dirs


CANDIDATES_PATH = CONFIG_DIR / "museum_vision_candidates.json"


def main() -> int:
    ensure_project_dirs()
    candidates = _load_candidates(CANDIDATES_PATH)
    total_images = 0
    missing_images = 0

    print("id\tstandard_name\timage_path\texists")
    for candidate in candidates:
        candidate_id = str(candidate.get("id") or "")
        standard_name = str(candidate.get("standard_name") or candidate.get("name") or "")
        reference_images = candidate.get("reference_images")
        if not isinstance(reference_images, list):
            reference_images = []
        for image_path in reference_images:
            image_text = str(image_path)
            exists = (PROJECT_ROOT / image_text).exists()
            total_images += 1
            if not exists:
                missing_images += 1
            print(f"{candidate_id}\t{standard_name}\t{image_text}\t{str(exists).lower()}")

    print()
    print(f"total_candidates={len(candidates)}")
    print(f"total_reference_images={total_images}")
    print(f"missing_images={missing_images}")
    return 0


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        print(f"[ERROR] candidates file not found: {path}")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[ERROR] invalid JSON: {path} error={exc}")
        return []
    if not isinstance(data, list):
        print(f"[ERROR] candidates JSON must be a list: {path}")
        return []
    return [item for item in data if isinstance(item, dict)]


if __name__ == "__main__":
    sys.exit(main())

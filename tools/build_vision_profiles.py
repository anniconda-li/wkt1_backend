#!/usr/bin/env python3
"""Build local baseline vision profiles from ``photo/`` images.

This replaces the old Bailian knowledge-base export path. It calls the vision
model only offline, stores detailed visual descriptions locally, and keeps the
runtime path fast.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.config  # noqa: E402,F401
from core.paths import KNOWLEDGE_CONFIG_DIR, ensure_project_dirs
from services.vision_service import VisionService

CANDIDATES_PATH = KNOWLEDGE_CONFIG_DIR / "museum_vision_candidates.json"
OUTPUT_PATH = KNOWLEDGE_CONFIG_DIR / "vision_profiles.json"
PHOTO_DIR = PROJECT_ROOT / "photo"


def main() -> int:
    parser = argparse.ArgumentParser(description="从 photo/ 基准照片生成本地视觉档案")
    parser.add_argument("--id", dest="candidate_id", default="", help="只处理指定文物 ID")
    parser.add_argument("--dry-run", action="store_true", help="只检查映射和图片，不调用模型、不写文件")
    parser.add_argument("--overwrite", action="store_true", help="重新生成已有条目")
    args = parser.parse_args()

    ensure_project_dirs()
    candidates = _load_candidates(CANDIDATES_PATH)
    if args.candidate_id:
        candidates = [item for item in candidates if _candidate_id(item) == args.candidate_id]
    existing = _load_existing(OUTPUT_PATH)
    service = VisionService()

    entries: dict[str, dict[str, Any]] = {entry["candidate_id"]: entry for entry in existing}
    failed = 0
    processed = 0

    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        image_path = _resolve_reference_image(candidate)
        print(f"[VISION-PROFILE] id={candidate_id} image={_rel(image_path) if image_path else '(missing)'}")
        if image_path is None or not image_path.exists():
            failed += 1
            continue
        if args.dry_run:
            continue
        if candidate_id in entries and not args.overwrite:
            print(f"[VISION-PROFILE] skip existing id={candidate_id}")
            continue

        desc = service.analyze_image(image_path)
        entries[candidate_id] = _entry_from_description(candidate, image_path, desc.to_dict())
        processed += 1

    if args.dry_run:
        print("[VISION-PROFILE] dry run complete")
        return 0 if failed == 0 else 1

    payload = {
        "schema_version": 1,
        "source": "photo_baseline",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "entries": list(entries.values()),
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[VISION-PROFILE] saved path={_rel(OUTPUT_PATH)} processed={processed} failed={failed}")
    return 0 if failed == 0 else 1


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"候选配置必须是列表: {path}")
    return [item for item in data if isinstance(item, dict)]


def _load_existing(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return [item for item in data["entries"] if isinstance(item, dict) and item.get("candidate_id")]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict) and item.get("candidate_id")]
    return []


def _entry_from_description(candidate: dict[str, Any], image_path: Path, desc: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": _candidate_id(candidate),
        "standard_name": str(candidate.get("standard_name") or candidate.get("name") or "").strip(),
        "category": str(desc.get("category") or candidate.get("category") or "").strip(),
        "reference_image_used": _rel(image_path),
        "detailed_visual_description": str(desc.get("visual_description") or "").strip(),
        "shape_features": _str_list(desc.get("shape_features")),
        "decoration_features": _str_list(desc.get("decoration_features")),
        "color_material": _str_list(desc.get("color_material")),
        "visual_keywords": _str_list(desc.get("search_keywords")),
        "negative_rules": _str_list(candidate.get("negative_rules")),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parse_ok": bool(desc.get("visual_description")),
        "risk": str(desc.get("risk") or "").strip(),
    }


def _resolve_reference_image(candidate: dict[str, Any]) -> Path | None:
    refs = candidate.get("reference_images")
    if not isinstance(refs, list):
        return None
    for value in refs:
        path = PROJECT_ROOT / str(value)
        if path.exists():
            return path
    return None


def _candidate_id(candidate: dict[str, Any]) -> str:
    return str(candidate.get("id") or "").strip()


def _rel(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.config  # noqa: E402,F401 - loads project .env
from core.paths import CONFIG_DIR, KNOWLEDGE_EXPORT_DIR, ensure_project_dirs


CANDIDATES_PATH = CONFIG_DIR / "museum_vision_candidates.json"
VISION_INDEX_PATH = CONFIG_DIR / "museum_vision_index.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build museum visual retrieval index and Markdown knowledge export.")
    parser.add_argument("--id", dest="candidate_id", default="", help="only process one candidate id")
    parser.add_argument("--limit", type=int, default=0, help="max candidates to process")
    parser.add_argument("--overwrite", action="store_true", help="regenerate existing vision index entries")
    parser.add_argument("--dry-run", action="store_true", help="check config and image paths without calling vision model")
    parser.add_argument("--export-only", action="store_true", help="export Markdown from existing museum_vision_index.json")
    args = parser.parse_args()

    ensure_project_dirs()
    candidates = _select_candidates(_load_candidates(CANDIDATES_PATH), args.candidate_id, args.limit)
    index_by_id = _load_index(VISION_INDEX_PATH)

    print(f"[INFO] candidates={len(candidates)} dry_run={args.dry_run} export_only={args.export_only}")
    if args.dry_run:
        _print_dry_run(candidates)
        return 0

    generated = 0
    skipped_existing = 0
    missing_images = 0
    error_count = 0

    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        image_path = _first_existing_reference_image(candidate)
        existing_entry = index_by_id.get(candidate_id)

        if image_path is None:
            missing_images += 1
            print(f"[MISSING] id={candidate_id} no reference image found")
            if existing_entry is None:
                index_by_id[candidate_id] = _entry_from_candidate(candidate, None, parse_ok=False, error="missing reference image")
            continue

        if args.export_only:
            if existing_entry is None:
                index_by_id[candidate_id] = _entry_from_candidate(candidate, image_path, parse_ok=False, error="not indexed")
            continue

        if existing_entry and existing_entry.get("parse_ok") is True and not args.overwrite:
            skipped_existing += 1
            print(f"[SKIP] id={candidate_id} reason=existing_index")
            continue

        try:
            result = _call_vision_model(candidate, image_path)
            index_by_id[candidate_id] = _entry_from_candidate(
                candidate,
                image_path,
                parse_ok=True,
                error="",
                result=result,
            )
            generated += 1
            print(f"[OK] id={candidate_id} image={_project_relative(image_path)}")
        except Exception as exc:
            error_count += 1
            index_by_id[candidate_id] = _entry_from_candidate(
                candidate,
                image_path,
                parse_ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            print(f"[ERROR] id={candidate_id} error={type(exc).__name__}: {exc}", flush=True)

    selected_index = {
        _candidate_id(candidate): index_by_id.get(_candidate_id(candidate))
        or _entry_from_candidate(candidate, _first_existing_reference_image(candidate), parse_ok=False, error="not indexed")
        for candidate in candidates
    }

    if not args.export_only:
        VISION_INDEX_PATH.write_text(json.dumps(selected_index, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[WRITE] {_project_relative(VISION_INDEX_PATH)}")

    exported = _export_markdown(candidates, selected_index)
    print(
        f"[DONE] generated={generated} skipped_existing={skipped_existing} "
        f"missing_images={missing_images} errors={error_count} exported_markdown={exported}",
        flush=True,
    )
    return 0 if error_count == 0 else 1


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        print(f"[ERROR] candidates file not found: {path}")
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"candidates JSON must be a list: {path}")
    return [item for item in data if isinstance(item, dict)]


def _load_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return {
            str(entry.get("id") or entry.get("candidate_id") or ""): _normalize_index_entry(entry)
            for entry in data["entries"]
            if isinstance(entry, dict) and (entry.get("id") or entry.get("candidate_id"))
        }
    if isinstance(data, dict):
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                normalized[str(key)] = _normalize_index_entry(value)
        return normalized
    return {}


def _normalize_index_entry(entry: dict[str, Any]) -> dict[str, Any]:
    candidate_id = str(entry.get("candidate_id") or entry.get("id") or "").strip()
    vision_result = entry.get("vision_result") if isinstance(entry.get("vision_result"), dict) else {}
    detailed = str(entry.get("detailed_visual_description") or "").strip()
    if not detailed:
        detailed = str(vision_result.get("detailed_visual_description") or vision_result.get("visual_description") or "").strip()
    if not detailed:
        detailed = _join_old_visual_fields(vision_result)
    return {
        "candidate_id": candidate_id,
        "standard_name": str(entry.get("standard_name") or vision_result.get("standard_name") or "").strip(),
        "aliases": _str_list(entry.get("aliases") or vision_result.get("aliases")),
        "category": str(entry.get("category") or vision_result.get("category") or "").strip(),
        "reference_image_used": str(entry.get("reference_image_used") or entry.get("reference_image") or "").strip(),
        "detailed_visual_description": detailed,
        "visual_keywords": _str_list(entry.get("visual_keywords") or vision_result.get("visual_keywords")),
        "name_constraints": _str_list(entry.get("name_constraints") or vision_result.get("name_constraints") or vision_result.get("negative_notes")),
        "generated_at": str(entry.get("generated_at") or "").strip(),
        "parse_ok": bool(entry.get("parse_ok", entry.get("status") == "ok")),
        "error": str(entry.get("error") or "").strip(),
    }


def _select_candidates(candidates: list[dict[str, Any]], candidate_id: str, limit: int) -> list[dict[str, Any]]:
    selected = candidates
    if candidate_id:
        selected = [candidate for candidate in selected if _candidate_id(candidate) == candidate_id]
    if limit > 0:
        selected = selected[:limit]
    return selected


def _print_dry_run(candidates: list[dict[str, Any]]) -> None:
    total_images = 0
    missing_images = 0
    print("id\tstandard_name\timage_path\texists")
    for candidate in candidates:
        refs = _reference_images(candidate)
        if not refs:
            print(f"{_candidate_id(candidate)}\t{_standard_name(candidate)}\t\tfalse")
            missing_images += 1
        for ref in refs:
            total_images += 1
            exists = _project_path(ref).exists()
            if not exists:
                missing_images += 1
            print(f"{_candidate_id(candidate)}\t{_standard_name(candidate)}\t{ref}\t{str(exists).lower()}")
    print(f"total_candidates={len(candidates)}")
    print(f"total_reference_images={total_images}")
    print(f"missing_images={missing_images}")


def _call_vision_model(candidate: dict[str, Any], image_path: Path) -> dict[str, Any]:
    provider = os.getenv("VISION_PROVIDER", "dashscope").strip().lower()
    if provider == "mock":
        return _mock_vision_result(candidate)
    if provider != "dashscope":
        raise ValueError(f"unsupported VISION_PROVIDER: {provider}")

    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置，无法调用视觉模型")

    import dashscope

    dashscope.api_key = api_key
    response = dashscope.MultiModalConversation.call(
        model=os.getenv("VISION_MODEL", "qwen-vl-plus").strip(),
        messages=[
            {
                "role": "user",
                "content": [
                    {"image": _image_data_url(image_path)},
                    {"text": _build_vision_prompt(candidate)},
                ],
            }
        ],
    )
    data = _response_to_dict(response)
    status_code = data.get("status_code", getattr(response, "status_code", None))
    if status_code not in (None, 200):
        message = data.get("message", getattr(response, "message", ""))
        code = data.get("code", getattr(response, "code", ""))
        raise RuntimeError(f"视觉模型调用失败 status={status_code} code={code} message={message}")

    text = _extract_response_text(data)
    if not text:
        raise ValueError("视觉模型返回为空")
    parsed = _extract_json_object(text)
    if not parsed:
        raise ValueError(f"视觉模型返回非 JSON：{_preview_text(text, 500)}")
    return _coerce_vision_result(candidate, parsed)


def _build_vision_prompt(candidate: dict[str, Any]) -> str:
    standard_name = _standard_name(candidate)
    aliases = "、".join(_str_list(candidate.get("aliases"))) or "无"
    category = str(candidate.get("category") or "").strip()
    return f"""你是博物馆文物“视觉检索索引”生成助手。

你的任务不是写导游讲解，也不是判断文物历史信息，而是根据标准文物图片，生成一段适合后续图像检索和文本匹配的详细视觉描述。

请只根据图片中真实可见的内容描述，不要编造年代、出土地、文物等级、用途、历史故事或价值评价。

我会提供该文物的标准名称、别名和类别。你只能使用这些名称，不得根据图像自行创造新的文物名称。

展品标准名称：{standard_name}
别名：{aliases}
类别：{category}

请只输出 JSON，不要输出 Markdown，不要输出解释文字：

{{
  "standard_name": "{standard_name}",
  "aliases": [],
  "category": "{category}",
  "detailed_visual_description": "",
  "visual_keywords": [],
  "name_constraints": []
}}

字段要求：

1. detailed_visual_description 是最重要字段。
2. detailed_visual_description 必须是一段连贯的详细视觉描述，150～300 字。
3. 描述重点包括：整体形态、轮廓、结构、颜色、材质观感、表面纹饰、特殊部件、容易被视觉模型识别到的特征。
4. 不要堆砌重复短语。
5. 不要写导游讲解。
6. 不要写历史背景。
7. 不要编造不可见信息。
8. visual_keywords 控制在 8～20 个，必须是视觉检索相关词。
9. name_constraints 写名称约束，强调只能使用标准名称或别名，不得自造名称。
10. 如果图片不清晰，也要说明可见特征，但不要补全看不见的细节。"""


def _coerce_vision_result(candidate: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
    detailed = str(data.get("detailed_visual_description") or data.get("visual_description") or "").strip()
    if not detailed:
        detailed = _join_old_visual_fields(data)
    return {
        "standard_name": _standard_name(candidate),
        "aliases": _str_list(data.get("aliases")) or _str_list(candidate.get("aliases")),
        "category": str(data.get("category") or candidate.get("category") or "").strip(),
        "detailed_visual_description": _limit_text(detailed, 500),
        "visual_keywords": _str_list(data.get("visual_keywords"))[:20],
        "name_constraints": _str_list(data.get("name_constraints"))[:20],
    }


def _mock_vision_result(candidate: dict[str, Any]) -> dict[str, Any]:
    name = _standard_name(candidate)
    aliases = _str_list(candidate.get("aliases"))
    category = str(candidate.get("category") or "").strip()
    return {
        "standard_name": name,
        "aliases": aliases,
        "category": category,
        "detailed_visual_description": (
            f"{name}参考图的模拟视觉检索描述。这里应记录器物整体形态、轮廓结构、颜色材质、"
            "表面纹饰和容易被照片识别到的视觉特征，用于验证脚本流程。"
        ),
        "visual_keywords": [item for item in [category, name, *aliases] if item][:20],
        "name_constraints": [
            f"具体展品名称只能使用标准名称“{name}”或配置中的别名。",
            "不得根据视觉特征拼接新的展品名称。",
        ],
    }


def _entry_from_candidate(
    candidate: dict[str, Any],
    image_path: Path | None,
    *,
    parse_ok: bool,
    error: str,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = result or {}
    standard_name = _standard_name(candidate)
    aliases = _str_list(candidate.get("aliases"))
    return {
        "candidate_id": _candidate_id(candidate),
        "standard_name": standard_name,
        "aliases": aliases,
        "category": str(candidate.get("category") or "").strip(),
        "reference_image_used": "" if image_path is None else _project_relative(image_path),
        "detailed_visual_description": str(result.get("detailed_visual_description") or "").strip(),
        "visual_keywords": _str_list(result.get("visual_keywords")),
        "name_constraints": _str_list(result.get("name_constraints")) or [
            f"具体展品名称只能使用标准名称“{standard_name}”或配置中的别名。",
            "不得根据视觉描述、类别、形状、材质、年代、地区等信息自行拼接新的展品名称。",
        ],
        "generated_at": _utc_now(),
        "parse_ok": parse_ok,
        "error": error,
    }


def _export_markdown(candidates: list[dict[str, Any]], index_by_id: dict[str, dict[str, Any]]) -> int:
    exported = 0
    KNOWLEDGE_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        candidate_id = _candidate_id(candidate)
        markdown = _build_markdown(candidate, index_by_id.get(candidate_id))
        output_path = KNOWLEDGE_EXPORT_DIR / f"{candidate_id}.md"
        output_path.write_text(markdown, encoding="utf-8")
        print(f"[EXPORT] {_project_relative(output_path)}")
        exported += 1
    return exported


def _build_markdown(candidate: dict[str, Any], entry: dict[str, Any] | None) -> str:
    standard_name = _standard_name(candidate)
    aliases = _str_list(candidate.get("aliases"))
    category = str(candidate.get("category") or "").strip()
    entry = entry or _entry_from_candidate(candidate, _first_existing_reference_image(candidate), parse_ok=False, error="not indexed")
    detailed = str(entry.get("detailed_visual_description") or "").strip() or "暂无视觉检索描述。"
    keywords = _str_list(entry.get("visual_keywords"))
    constraints = _str_list(entry.get("name_constraints"))

    lines = [
        f"# {standard_name}",
        "",
        f"文物ID：{_candidate_id(candidate)}",
        f"标准名称：{standard_name}",
        f"别名：{'、'.join(aliases) if aliases else '无'}",
        f"类别：{category or '未填写'}",
        "",
        "## 视觉检索描述",
        "",
        detailed,
        "",
        "## 视觉检索关键词",
        "",
        "、".join(keywords) if keywords else "暂无",
        "",
        "## 名称约束",
        "",
        "具体展品名称只能使用知识库中的标准名称或别名。",
        f"标准名称：{standard_name}",
        f"允许别名：{'、'.join(aliases) if aliases else '无'}",
        "不得根据视觉描述、类别、形状、材质、年代、地区等信息自行拼接新的展品名称。",
    ]
    if constraints:
        lines.extend(constraints)
    lines.append("")
    return "\n".join(lines)


def _first_existing_reference_image(candidate: dict[str, Any]) -> Path | None:
    for ref in _reference_images(candidate):
        path = _project_path(ref)
        if path.exists():
            return path
    return None


def _reference_images(candidate: dict[str, Any]) -> list[str]:
    refs = candidate.get("reference_images")
    if not isinstance(refs, list):
        return []
    return [str(ref).strip() for ref in refs if str(ref).strip()]


def _candidate_id(candidate: dict[str, Any]) -> str:
    return str(candidate.get("id") or "").strip()


def _standard_name(candidate: dict[str, Any]) -> str:
    return str(candidate.get("standard_name") or candidate.get("name") or "").strip()


def _project_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


def _project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def _image_data_url(image_path: Path) -> str:
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _response_to_dict(response: Any) -> dict[str, Any]:
    if isinstance(response, dict):
        return response
    to_dict = getattr(response, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    data: dict[str, Any] = {}
    for name in ("status_code", "code", "message", "output", "usage", "request_id"):
        if hasattr(response, name):
            data[name] = getattr(response, name)
    return data


def _extract_response_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        output = value.get("output")
        if isinstance(output, dict):
            choices = output.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    text = _extract_response_text(choice)
                    if text:
                        return text
            text = output.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
        message = value.get("message")
        if isinstance(message, dict):
            text = _extract_response_text(message)
            if text:
                return text
        content = value.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            if parts:
                return "".join(parts).strip()
        if isinstance(content, str) and content.strip():
            return content.strip()
        for child in value.values():
            text = _extract_response_text(child)
            if text:
                return text
    if isinstance(value, list):
        for child in value:
            text = _extract_response_text(child)
            if text:
                return text
    return ""


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _join_old_visual_fields(value: dict[str, Any]) -> str:
    parts = []
    for key in (
        "visual_description",
        "shape_features",
        "material_color_features",
        "decoration_features",
        "possible_user_descriptions",
        "negative_notes",
    ):
        item = value.get(key)
        if isinstance(item, list):
            parts.extend(str(part).strip() for part in item if str(part).strip())
        elif isinstance(item, str) and item.strip():
            parts.append(item.strip())
    return "；".join(parts)


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _limit_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit]


def _preview_text(text: str, limit: int) -> str:
    normalized = (text or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


if __name__ == "__main__":
    sys.exit(main())

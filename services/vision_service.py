from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.paths import CONFIG_DIR, TMP_CAMERA_PREPROCESS_DIR


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATES_PATH = CONFIG_DIR / "museum_vision_candidates.json"
DEFAULT_PREPROCESS_DIR = TMP_CAMERA_PREPROCESS_DIR
CATEGORIES = {"玉器", "陶瓷", "青铜器", "石器", "书画", "建筑构件", "展厅", "未知"}
SAFE_LEVELS = {"certain", "likely", "possible", "category_only", "unknown"}
GUIDE_CATEGORIES = {"玉器", "陶瓷", "青铜器", "书画", "石刻", "其他", "无法判断"}


@dataclass(frozen=True)
class VisionCandidate:
    id: str
    name: str
    confidence: float
    visual_evidence: list[str] = field(default_factory=list)
    risk: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MuseumVisionCandidate:
    id: str
    name: str
    category: str
    aliases: list[str] = field(default_factory=list)
    museum: str = ""
    importance: str = ""
    standard_name: str = ""
    is_key_exhibit: bool = False
    priority: int = 0
    reference_images: list[str] = field(default_factory=list)
    guide_text: str = ""
    visual_features: list[str] = field(default_factory=list)
    negative_rules: list[str] = field(default_factory=list)
    kb_keywords: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class VisionObservation:
    best_candidate_id: str = "none"
    best_candidate_name: str = "无"
    candidate_confidence: float = 0.0
    category: str = "未知"
    top_candidates: list[VisionCandidate] = field(default_factory=list)
    visible_features: list[str] = field(default_factory=list)
    visual_evidence: list[str] = field(default_factory=list)
    risk: str = ""
    safe_answer_level: str = "unknown"
    need_retake: bool = True
    reason: str = ""

    @property
    def scene_type(self) -> str:
        if self.need_retake and self.category == "未知":
            return "模糊"
        return "展柜展品" if self.category != "未知" else "无关"

    @property
    def object_category(self) -> str:
        return self.category

    @property
    def visual_features(self) -> list[str]:
        return self.visible_features

    @property
    def readable_text(self) -> str:
        return ""

    @property
    def possible_subject(self) -> str:
        return "" if self.best_candidate_id == "none" else self.best_candidate_name

    @property
    def category_confidence(self) -> float:
        if self.category == "未知":
            return 0.0
        return max(self.candidate_confidence, 0.6 if self.safe_answer_level == "category_only" else 0.0)

    @property
    def specific_name_confidence(self) -> float:
        return self.candidate_confidence

    @property
    def can_identify_specific_item(self) -> bool:
        return self.best_candidate_id != "none" and self.safe_answer_level in {"certain", "likely"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "best_candidate_id": self.best_candidate_id,
            "best_candidate_name": self.best_candidate_name,
            "candidate_confidence": self.candidate_confidence,
            "category": self.category,
            "top_candidates": [candidate.to_dict() for candidate in self.top_candidates],
            "visible_features": list(self.visible_features),
            "visual_evidence": list(self.visual_evidence),
            "risk": self.risk,
            "safe_answer_level": self.safe_answer_level,
            "need_retake": self.need_retake,
            "reason": self.reason,
            # Compatibility fields for older callers.
            "scene_type": self.scene_type,
            "object_category": self.object_category,
            "visual_features": list(self.visible_features),
            "readable_text": "",
            "possible_subject": self.possible_subject,
            "category_confidence": self.category_confidence,
            "specific_name_confidence": self.specific_name_confidence,
            "can_identify_specific_item": self.can_identify_specific_item,
        }


class VisionService:
    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        candidates_path: str | Path | None = None,
        preprocess_dir: str | Path | None = None,
    ):
        self.provider = (provider if provider is not None else os.getenv("VISION_PROVIDER", "dashscope")).strip().lower()
        self.model = (model if model is not None else os.getenv("VISION_MODEL", "qwen-vl-plus")).strip()
        raw_candidates_path = candidates_path or os.getenv("VISION_CANDIDATES_PATH") or DEFAULT_CANDIDATES_PATH
        self.candidates_path = Path(raw_candidates_path)
        if not self.candidates_path.is_absolute():
            self.candidates_path = PROJECT_ROOT / self.candidates_path
        self.preprocess_dir = Path(preprocess_dir or os.getenv("VISION_PREPROCESS_DIR", str(DEFAULT_PREPROCESS_DIR)))
        self.candidates = load_vision_candidates(self.candidates_path)
        print(f"[VISION] loaded vision candidates count={len(self.candidates)} path={self.candidates_path}", flush=True)

    def analyze_image(self, image_path: str | Path) -> VisionObservation:
        path = Path(image_path)
        if self.provider == "mock":
            return _mock_observation(path)
        if self.provider == "dashscope":
            return self._analyze_with_dashscope(path)
        raise ValueError(f"unsupported VISION_PROVIDER: {self.provider}")

    def analyze_for_guide_context(self, image_path: str | Path) -> dict[str, Any]:
        path = Path(image_path)
        if self.provider == "mock":
            return _mock_guide_context(path)
        if self.provider == "dashscope":
            return self._analyze_guide_context_with_dashscope(path)
        raise ValueError(f"unsupported VISION_PROVIDER: {self.provider}")

    def _analyze_with_dashscope(self, image_path: Path) -> VisionObservation:
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            return VisionObservation(risk="DASHSCOPE_API_KEY 未配置", reason="DASHSCOPE_API_KEY 未配置，无法调用视觉模型")
        if not image_path.exists():
            return VisionObservation(risk="图片不存在", reason=f"图片不存在：{image_path}")

        import dashscope

        dashscope.api_key = api_key
        preprocess_path = preprocess_image_for_vision(image_path, self.preprocess_dir)
        prompt = build_candidate_prompt(self.candidates)
        print(f"[VISION] vision candidate prompt length={len(prompt)}", flush=True)

        content = []
        if preprocess_path != image_path:
            content.append({"image": _image_data_url(image_path)})
            content.append({"image": _image_data_url(preprocess_path)})
        else:
            content.append({"image": _image_data_url(preprocess_path)})
        content.append({"text": prompt})

        response = dashscope.MultiModalConversation.call(
            model=self.model,
            messages=[{"role": "user", "content": content}],
        )
        response_data = _response_to_dict(response)
        status_code = response_data.get("status_code", getattr(response, "status_code", None))
        if status_code not in (None, 200):
            message = response_data.get("message", getattr(response, "message", ""))
            code = response_data.get("code", getattr(response, "code", ""))
            return VisionObservation(risk=str(message), reason=f"视觉模型调用失败 status={status_code} code={code} message={message}")

        text = _extract_response_text(response_data)
        print(f"[VISION] vision raw response={_preview_text(text, 1200)}", flush=True)
        if not text:
            return VisionObservation(risk="视觉模型返回为空", reason="视觉模型返回为空")
        return parse_vision_observation(text, self.candidates)

    def _analyze_guide_context_with_dashscope(self, image_path: Path) -> dict[str, Any]:
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("DASHSCOPE_API_KEY 未配置，无法调用视觉模型")
        if not image_path.exists():
            raise FileNotFoundError(f"图片不存在：{image_path}")

        import dashscope

        dashscope.api_key = api_key
        preprocess_path = preprocess_image_for_vision(image_path, self.preprocess_dir)
        prompt = build_guide_context_prompt()
        print(f"[VISION] guide context prompt length={len(prompt)}", flush=True)

        content = []
        if preprocess_path != image_path:
            content.append({"image": _image_data_url(image_path)})
            content.append({"image": _image_data_url(preprocess_path)})
        else:
            content.append({"image": _image_data_url(preprocess_path)})
        content.append({"text": prompt})

        response = dashscope.MultiModalConversation.call(
            model=self.model,
            messages=[{"role": "user", "content": content}],
        )
        response_data = _response_to_dict(response)
        status_code = response_data.get("status_code", getattr(response, "status_code", None))
        if status_code not in (None, 200):
            message = response_data.get("message", getattr(response, "message", ""))
            code = response_data.get("code", getattr(response, "code", ""))
            raise RuntimeError(f"视觉模型调用失败 status={status_code} code={code} message={message}")

        text = _extract_response_text(response_data)
        print(f"[VISION] guide context raw response={_preview_text(text, 1200)}", flush=True)
        if not text:
            raise VisionJsonParseError("视觉模型返回为空", raw_response="")
        return parse_guide_context_result(text)


class VisionJsonParseError(ValueError):
    def __init__(self, message: str, *, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


def load_vision_candidates(path: Path = DEFAULT_CANDIDATES_PATH) -> list[MuseumVisionCandidate]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[VISION] failed to load candidates path={path} error={exc}", flush=True)
        return []
    candidates = []
    if not isinstance(raw, list):
        return candidates
    for item in raw:
        if not isinstance(item, dict):
            continue
        candidates.append(
            MuseumVisionCandidate(
                id=str(item.get("id") or "").strip(),
                name=str(item.get("name") or item.get("standard_name") or "").strip(),
                aliases=_str_list(item.get("aliases")),
                category=_clean_category(str(item.get("category") or "未知")),
                museum=str(item.get("museum") or "").strip(),
                importance=str(item.get("importance") or "").strip(),
                standard_name=str(item.get("standard_name") or item.get("name") or "").strip(),
                is_key_exhibit=bool(item.get("is_key_exhibit")),
                priority=_int_value(item.get("priority")),
                reference_images=_str_list(item.get("reference_images")),
                guide_text=str(item.get("guide_text") or "").strip(),
                visual_features=_str_list(item.get("visual_features")),
                negative_rules=_str_list(item.get("negative_rules")),
                kb_keywords=_str_list(item.get("kb_keywords")),
            )
        )
    return [candidate for candidate in candidates if candidate.id and candidate.name]


def build_candidate_prompt(candidates: list[MuseumVisionCandidate]) -> str:
    candidate_lines = []
    for candidate in candidates:
        aliases = "、".join(candidate.aliases) if candidate.aliases else "无"
        features = "、".join(candidate.visual_features) if candidate.visual_features else "无"
        negative_rules = "；".join(candidate.negative_rules) if candidate.negative_rules else "无"
        importance = f"；重要性：{candidate.importance}" if candidate.importance else ""
        candidate_lines.append(
            f"- id={candidate.id}；名称：{candidate.name}；别名：{aliases}；类别：{candidate.category}{importance}；"
            f"视觉特征：{features}；排除规则：{negative_rules}"
        )
    candidate_text = "\n".join(candidate_lines)
    return f"""你是博物馆展品候选匹配助手。图片来自平顶山市博物馆导游设备，游客通常会拍摄展品实物。

下面两张图来自同一张游客照片，第二张是中心裁剪增强图，请综合判断。如果只收到一张图，则以该图为准。

请判断图片中的主要展品是否像以下候选之一。你可以提出“可能/很像”的候选，但必须给出视觉依据和不确定风险。不要把不确定对象说成绝对确定。

候选展品：
{candidate_text}

输出要求：
1. 必须输出 JSON，不要 Markdown，不要多余解释。
2. top_candidates 最多 3 个，按可能性排序。
3. 如果图片很像某个候选，即使不能完全确认，也要放入 top_candidates。
4. confidence 表示“图片与候选的相似程度”，不是绝对确定程度。
5. 必须给出 visual_evidence，说明为什么像。
6. 必须给出 risk，说明为什么不确定。
7. 如果候选都不像，best_candidate_id 设为 none。
8. 不要编造图片里看不到的细节。
9. 可以使用“可能是/很像”的判断，但不能输出“就是”。
10. 如果图片偏暗、模糊、反光，不要直接判失败；只要还能看出形状和大类，就继续给候选。

输出 JSON 格式：
{{
  "best_candidate_id": "yingguo_yuying 或 none",
  "best_candidate_name": "应国玉鹰 或 无",
  "candidate_confidence": 0.0,
  "category": "玉器/陶瓷/青铜器/未知",
  "top_candidates": [
    {{
      "id": "yingguo_yuying",
      "name": "应国玉鹰",
      "confidence": 0.0,
      "visual_evidence": ["浅色玉质", "鸟形轮廓", "双翼展开"],
      "risk": "图片偏暗，纹饰细节不清"
    }}
  ],
  "visible_features": ["..."],
  "risk": "...",
  "safe_answer_level": "certain/likely/possible/category_only/unknown",
  "need_retake": false
}}"""


def build_guide_context_prompt() -> str:
    return """你是博物馆展品图片观察助手。请只根据图片本身提取可见信息，用于后续知识库检索。

要求：
1. 不要猜具体馆藏名称。
2. 不要编造年代、出土地、博物馆名称。
3. 如果没有看到说明牌，不要输出具体展品名称。
4. 只返回 JSON，不要 Markdown，不要多余解释。
5. visual_summary 控制在 80 字以内。
6. search_keywords 要适合用于平顶山市博物馆知识库检索。
7. confidence 范围 0.0 到 1.0。

输出 JSON 格式：
{
  "category": "玉器/陶瓷/青铜器/书画/石刻/其他/无法判断",
  "object_type_guess": [],
  "visual_summary": "",
  "shape_features": [],
  "decoration_features": [],
  "search_keywords": [],
  "is_clear": true,
  "confidence": 0.0,
  "risk": ""
}"""


def preprocess_image_for_vision(image_path: Path, preprocess_dir: Path = DEFAULT_PREPROCESS_DIR) -> Path:
    try:
        from PIL import Image, ImageEnhance
    except ImportError as exc:
        print(f"[VISION] preprocess skipped reason=pillow_missing error={exc}", flush=True)
        return image_path

    start = time.perf_counter()
    preprocess_dir.mkdir(parents=True, exist_ok=True)
    output_path = preprocess_dir / f"{image_path.stem}_center_enhanced_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.jpg"
    try:
        with Image.open(image_path) as image:
            image = image.convert("RGB")
            width, height = image.size
            crop_width = max(1, int(width * 0.75))
            crop_height = max(1, int(height * 0.75))
            left = max(0, (width - crop_width) // 2)
            top = max(0, (height - crop_height) // 2)
            cropped = image.crop((left, top, left + crop_width, top + crop_height))
            enhanced = ImageEnhance.Brightness(cropped).enhance(1.18)
            enhanced = ImageEnhance.Contrast(enhanced).enhance(1.22)
            enhanced.save(output_path, format="JPEG", quality=92)
    except Exception as exc:
        print(f"[VISION] preprocess failed image={image_path} error={exc}", flush=True)
        return image_path

    print(
        f"[VISION] preprocess image saved={output_path} source={image_path} cost={time.perf_counter() - start:.3f}s",
        flush=True,
    )
    return output_path


def describe_image(image_path: str | Path) -> str:
    observation = VisionService().analyze_image(image_path)
    return json.dumps(observation.to_dict(), ensure_ascii=False)


def parse_vision_observation(text: str, candidates: list[MuseumVisionCandidate] | None = None) -> VisionObservation:
    data = _extract_json_object(text)
    return _coerce_observation(data, candidates or [])


def parse_guide_context_result(text: str) -> dict[str, Any]:
    data = _extract_json_object(text)
    if not data:
        raise VisionJsonParseError("视觉模型返回非 JSON，且无法提取 JSON 对象", raw_response=text)
    return _coerce_guide_context(data)


def _coerce_guide_context(data: dict[str, Any]) -> dict[str, Any]:
    category = str(data.get("category") or "无法判断").strip()
    if category not in GUIDE_CATEGORIES:
        category = next((item for item in GUIDE_CATEGORIES if item in category), "无法判断")
    visual_summary = " ".join(str(data.get("visual_summary") or "").strip().split())
    if len(visual_summary) > 80:
        visual_summary = visual_summary[:80]
    confidence = _clamp_float(data.get("confidence"), 0.0)
    return {
        "category": category,
        "object_type_guess": _str_list(data.get("object_type_guess"))[:8],
        "visual_summary": visual_summary,
        "shape_features": _str_list(data.get("shape_features"))[:10],
        "decoration_features": _str_list(data.get("decoration_features"))[:10],
        "search_keywords": _str_list(data.get("search_keywords"))[:10],
        "is_clear": bool(data.get("is_clear")),
        "confidence": confidence,
        "risk": str(data.get("risk") or "").strip(),
    }


def _coerce_observation(data: dict[str, Any], candidates: list[MuseumVisionCandidate]) -> VisionObservation:
    by_id = {candidate.id: candidate for candidate in candidates}
    top_candidates = []
    raw_top = data.get("top_candidates")
    if isinstance(raw_top, list):
        for item in raw_top[:3]:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("id") or "").strip()
            if candidate_id == "none":
                continue
            known = by_id.get(candidate_id)
            name = str(item.get("name") or (known.name if known else "")).strip()
            top_candidates.append(
                VisionCandidate(
                    id=candidate_id,
                    name=name,
                    confidence=_clamp_float(item.get("confidence"), 0.0),
                    visual_evidence=_str_list(item.get("visual_evidence"))[:8],
                    risk=str(item.get("risk") or "").strip(),
                )
            )

    best_candidate_id = str(data.get("best_candidate_id") or "none").strip() or "none"
    if best_candidate_id != "none" and best_candidate_id not in by_id and not any(c.id == best_candidate_id for c in top_candidates):
        best_candidate_id = "none"
    best_known = by_id.get(best_candidate_id)
    best_candidate_name = str(data.get("best_candidate_name") or (best_known.name if best_known else "无")).strip() or "无"
    if best_candidate_id == "none":
        best_candidate_name = "无"

    confidence = _clamp_float(data.get("candidate_confidence"), 0.0)
    if confidence <= 0.0 and top_candidates and top_candidates[0].id == best_candidate_id:
        confidence = top_candidates[0].confidence
    category = _clean_category(str(data.get("category") or (best_known.category if best_known else "未知")))
    safe_answer_level = str(data.get("safe_answer_level") or "unknown").strip()
    if safe_answer_level not in SAFE_LEVELS:
        safe_answer_level = _infer_safe_level(best_candidate_id, confidence, category)

    visual_evidence = _str_list(data.get("visual_evidence"))
    if not visual_evidence and top_candidates:
        visual_evidence = list(top_candidates[0].visual_evidence)

    observation = VisionObservation(
        best_candidate_id=best_candidate_id,
        best_candidate_name=best_candidate_name,
        candidate_confidence=confidence,
        category=category,
        top_candidates=top_candidates,
        visible_features=_str_list(data.get("visible_features"))[:10],
        visual_evidence=visual_evidence[:8],
        risk=str(data.get("risk") or "").strip(),
        safe_answer_level=safe_answer_level,
        need_retake=bool(data.get("need_retake")),
        reason=str(data.get("reason") or "").strip(),
    )
    print(
        f"[VISION] best_candidate_id={observation.best_candidate_id} "
        f"candidate_confidence={observation.candidate_confidence:.2f} "
        f"safe_answer_level={observation.safe_answer_level}",
        flush=True,
    )
    return observation


def _infer_safe_level(best_candidate_id: str, confidence: float, category: str) -> str:
    if best_candidate_id != "none" and confidence >= 0.85:
        return "likely"
    if best_candidate_id != "none" and confidence >= 0.6:
        return "possible"
    if category != "未知":
        return "category_only"
    return "unknown"


def _clean_category(value: str) -> str:
    value = value.strip()
    if value in CATEGORIES:
        return value
    for category in CATEGORIES:
        if category in value:
            return category
    return "未知"


def _clamp_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    return max(0.0, min(1.0, number))


def _int_value(value: Any, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


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


def _preview_text(text: str, limit: int) -> str:
    normalized = (text or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _mock_observation(image_path: Path) -> VisionObservation:
    name = image_path.name.lower()
    if "blur" in name or "retake" in name:
        return VisionObservation(
            category="未知",
            risk="mock 模糊图片",
            safe_answer_level="unknown",
            need_retake=True,
            reason="mock 模糊图片",
        )
    if "yu" in name or "ying" in name or "eagle" in name:
        return VisionObservation(
            best_candidate_id="yingguo_yuying",
            best_candidate_name="应国玉鹰",
            candidate_confidence=0.72,
            category="玉器",
            top_candidates=[
                VisionCandidate(
                    id="yingguo_yuying",
                    name="应国玉鹰",
                    confidence=0.72,
                    visual_evidence=["浅色玉质", "鸟形或鹰形轮廓", "双翼展开"],
                    risk="mock 图片细节不够清楚",
                )
            ],
            visible_features=["浅色玉质", "扁平器物", "左右展开轮廓"],
            visual_evidence=["浅色玉质", "鸟形或鹰形轮廓", "双翼展开"],
            risk="mock 图片细节不够清楚",
            safe_answer_level="possible",
            need_retake=False,
        )
    return VisionObservation(
        best_candidate_id="lushan_huaci",
        best_candidate_name="鲁山花瓷",
        candidate_confidence=0.65,
        category="陶瓷",
        top_candidates=[
            VisionCandidate(
                id="lushan_huaci",
                name="鲁山花瓷",
                confidence=0.65,
                visual_evidence=["陶瓷器", "器形明显"],
                risk="mock 釉色细节不清",
            )
        ],
        visible_features=["浅色器物", "圆润轮廓", "展柜内拍摄"],
        visual_evidence=["陶瓷器", "器形明显"],
        risk="mock 默认陶瓷候选",
        safe_answer_level="possible",
        need_retake=False,
    )


def _mock_guide_context(image_path: Path) -> dict[str, Any]:
    name = image_path.name.lower()
    if "blur" in name or "retake" in name:
        return {
            "category": "无法判断",
            "object_type_guess": [],
            "visual_summary": "画面较模糊，主体展品轮廓和材质不清。",
            "shape_features": [],
            "decoration_features": [],
            "search_keywords": ["展品", "模糊", "无法判断"],
            "is_clear": False,
            "confidence": 0.2,
            "risk": "mock 模糊图片",
        }
    return {
        "category": "陶瓷",
        "object_type_guess": ["器物"],
        "visual_summary": "展柜中可见一件器物，轮廓较圆润，表面有浅色反光。",
        "shape_features": ["圆润轮廓", "器物主体"],
        "decoration_features": ["表面反光", "纹饰细节不清"],
        "search_keywords": ["陶瓷", "器物", "展柜", "平顶山市博物馆"],
        "is_clear": True,
        "confidence": 0.65,
        "risk": "mock 默认视觉描述，未判断具体展品名称",
    }

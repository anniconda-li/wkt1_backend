"""Vision description service.

The vision model has one job: describe visible artifact features. Baseline
photos and user photos share the same JSON shape, so local code can compare
them quickly without calling a remote knowledge-base app.
"""

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

from core.paths import TMP_CAMERA_PREPROCESS_DIR

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREPROCESS_DIR = TMP_CAMERA_PREPROCESS_DIR
CATEGORIES = {"玉器", "陶瓷", "青铜器", "石器", "书画", "建筑构件", "其他", "无法判断", "未知"}


@dataclass(frozen=True)
class VisualDescription:
    """Structured visual description returned by the vision model."""

    category: str = "无法判断"
    visual_description: str = ""
    shape_features: list[str] = field(default_factory=list)
    decoration_features: list[str] = field(default_factory=list)
    color_material: list[str] = field(default_factory=list)
    search_keywords: list[str] = field(default_factory=list)
    is_clear: bool = True
    confidence: float = 0.0
    risk: str = ""

    def to_search_text(self) -> str:
        parts = [self.visual_description]
        if self.shape_features:
            parts.append("形态特征：" + " ".join(self.shape_features))
        if self.decoration_features:
            parts.append("纹饰特征：" + " ".join(self.decoration_features))
        if self.color_material:
            parts.append("颜色材质：" + " ".join(self.color_material))
        if self.search_keywords:
            parts.append("关键词：" + " ".join(self.search_keywords))
        return "\n".join(part for part in parts if part)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VisionService:
    """Analyze artifact images into structured visual descriptions."""

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        preprocess_dir: str | Path | None = None,
        **_: Any,
    ):
        self.provider = (provider if provider is not None else os.getenv("VISION_PROVIDER", "dashscope")).strip().lower()
        self.model = (model if model is not None else os.getenv("VISION_MODEL", "qwen-vl-plus")).strip()
        self.preprocess_dir = Path(preprocess_dir or os.getenv("VISION_PREPROCESS_DIR", str(DEFAULT_PREPROCESS_DIR)))
        print(f"[VISION] provider={self.provider} model={self.model}", flush=True)

    def analyze_image(self, image_path: str | Path) -> VisualDescription:
        path = Path(image_path)
        if self.provider == "mock":
            return _mock_description(path)
        if self.provider == "dashscope":
            return self._analyze_description_with_dashscope(path)
        raise ValueError(f"不支持的 VISION_PROVIDER: {self.provider}")

    def analyze_for_guide_context(self, image_path: str | Path) -> dict[str, Any]:
        desc = self.analyze_image(image_path)
        return {
            "category": desc.category,
            "object_type_guess": [],
            "visual_summary": desc.visual_description[:80],
            "shape_features": desc.shape_features,
            "decoration_features": desc.decoration_features,
            "search_keywords": desc.search_keywords,
            "is_clear": desc.is_clear,
            "confidence": desc.confidence,
            "risk": desc.risk,
            "visual_description": desc.visual_description,
            "color_material": desc.color_material,
        }

    def _analyze_description_with_dashscope(self, image_path: Path) -> VisualDescription:
        api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        if not api_key:
            return VisualDescription(risk="DASHSCOPE_API_KEY 未配置", is_clear=False)
        if not image_path.exists():
            return VisualDescription(risk="图片不存在", is_clear=False)

        import dashscope

        dashscope.api_key = api_key
        preprocess_path = preprocess_image_for_vision(image_path, self.preprocess_dir)
        prompt = build_visual_description_prompt()
        print(f"[VISION] prompt_len={len(prompt)} image={image_path}", flush=True)

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
            return VisualDescription(
                risk=f"视觉模型调用失败 status={status_code} code={code} message={message}",
                is_clear=False,
            )

        text = _extract_response_text(response_data)
        print(f"[VISION] raw={_preview_text(text, 1200)}", flush=True)
        if not text:
            return VisualDescription(risk="视觉模型返回为空", is_clear=False)
        return parse_visual_description(text)


def build_visual_description_prompt() -> str:
    return """你是一个博物馆文物视觉特征描述助手。请根据图片中可见的内容，输出结构化的视觉描述，用于和本地基准视觉档案做相似度对比。

要求：
1. 只描述视觉可见的内容：形态、颜色、材质质感、表面纹饰、特殊结构。
2. 不要编造年代、出土地、用途、历史故事、文物等级。
3. 不要猜测具体文物名称，除非图片中能看到说明牌文字。
4. visual_description 是最重要字段，250-600字，尽量详细描述轮廓、比例、构件、纹饰、材质、颜色、反光、残缺、遮挡和容易混淆处。
5. shape_features、decoration_features、color_material、search_keywords 尽量给出稳定短语，便于本地关键词匹配。
6. 只输出 JSON，不要 Markdown 标记。

输出 JSON 格式：
{
  "category": "玉器/陶瓷/青铜器/石器/书画/建筑构件/其他/无法判断",
  "visual_description": "一段250-600字的连贯视觉描述，包括整体形态、结构比例、颜色、材质观感、表面纹饰细节、特殊部件、拍摄角度和不确定处。",
  "shape_features": ["整体轮廓", "形态特征", "特殊构件"],
  "decoration_features": ["纹饰", "装饰", "线条或釉斑"],
  "color_material": ["颜色描述", "材质质感", "锈色或釉色"],
  "search_keywords": ["关键词1", "关键词2", "可用于匹配的视觉短语"],
  "is_clear": true,
  "confidence": 0.9,
  "risk": "如有不确定的地方在此说明"
}"""


def parse_visual_description(text: str) -> VisualDescription:
    data = _extract_json_object(text)
    if not data:
        return VisualDescription(risk="视觉模型返回非 JSON", is_clear=False)

    visual_description = " ".join(str(data.get("visual_description") or "").strip().split())
    if len(visual_description) > 600:
        visual_description = visual_description[:600]

    desc = VisualDescription(
        category=_clean_category(str(data.get("category") or "无法判断")),
        visual_description=visual_description,
        shape_features=_str_list(data.get("shape_features"))[:14],
        decoration_features=_str_list(data.get("decoration_features"))[:14],
        color_material=_str_list(data.get("color_material"))[:14],
        search_keywords=_str_list(data.get("search_keywords"))[:20],
        is_clear=bool(data.get("is_clear", True)),
        confidence=_clamp_float(data.get("confidence"), 0.0),
        risk=str(data.get("risk") or "").strip(),
    )
    print(
        f"[VISION] category={desc.category} is_clear={desc.is_clear} "
        f"confidence={desc.confidence:.2f} desc_len={len(desc.visual_description)}",
        flush=True,
    )
    return desc


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
        f"[VISION] preprocess saved={output_path} source={image_path} cost={time.perf_counter() - start:.3f}s",
        flush=True,
    )
    return output_path


def describe_image(image_path: str | Path) -> str:
    desc = VisionService().analyze_image(image_path)
    return json.dumps(desc.to_dict(), ensure_ascii=False)


def _clean_category(value: str) -> str:
    value = value.strip()
    if value in CATEGORIES:
        return value
    for category in CATEGORIES:
        if category in value:
            return category
    return "无法判断"


def _clamp_float(value: Any, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = fallback
    return max(0.0, min(1.0, number))


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
        match = re.match(r"\{.*\}", cleaned, flags=re.S)
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
            parts = [item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
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


def _mock_description(image_path: Path) -> VisualDescription:
    name = image_path.name.lower()
    if "blur" in name or "retake" in name:
        return VisualDescription(
            category="无法判断",
            visual_description="画面较模糊，主体展品轮廓和材质不清。",
            search_keywords=["展品", "模糊", "无法判断"],
            is_clear=False,
            confidence=0.2,
            risk="mock 模糊图片",
        )
    if "yu" in name or "ying" in name or "eagle" in name or "baiyu" in name:
        return VisualDescription(
            category="玉器",
            visual_description="该文物为一件玉质雕刻品，整体呈展翼鹰形，双翼对称向左右两侧展开，边缘圆润。材质为浅黄至米白色玉石，表面打磨光滑，局部可见自然褐色沁色。鹰首位于中央偏上，喙部短而尖锐，身体以流畅的线刻表现羽毛层次，线条深浅不一。",
            shape_features=["展翼鹰形", "双翼对称展开", "扁平器物", "喙部尖锐"],
            decoration_features=["线刻羽毛纹", "线条流畅"],
            color_material=["浅黄至米白色", "玉石", "表面光滑莹润", "褐色沁色"],
            search_keywords=["玉器", "鹰形", "双翼展开", "线刻", "扁平"],
            is_clear=True,
            confidence=0.72,
            risk="mock 图片细节不够清楚",
        )
    if "heiyou" in name or "lanban" in name or "sanzuxi" in name:
        return VisualDescription(
            category="陶瓷",
            visual_description="展柜中可见一件黑釉陶瓷器，整体呈敞口弧腹造型，口沿呈花瓣状波浪形，底部有三足支撑。器身深色釉面上可见蓝色斑纹和自然垂流效果。",
            shape_features=["敞口弧腹", "花瓣状口沿", "三足"],
            decoration_features=["蓝色斑纹", "釉料垂流"],
            color_material=["黑釉", "蓝斑", "光亮釉面"],
            search_keywords=["陶瓷", "黑釉", "蓝斑", "花口", "三足"],
            is_clear=True,
            confidence=0.7,
            risk="mock 默认陶瓷描述",
        )
    return VisualDescription(
        category="青铜器",
        visual_description="这件青铜器整体造型庄重古朴，器身表面覆盖深绿色铜锈，局部可见复杂几何纹饰和兽首形装饰。器身有圆腹、带盖、环耳或三足等青铜礼器特征。",
        shape_features=["圆形器身", "带盖", "环形耳", "三足"],
        decoration_features=["几何纹样", "兽首装饰", "弦纹"],
        color_material=["深绿色铜锈", "青铜材质", "金属质感"],
        search_keywords=["青铜器", "三足", "带盖", "环耳", "纹饰"],
        is_clear=True,
        confidence=0.65,
        risk="mock 默认青铜器描述",
    )


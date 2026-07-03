"""Guide answer composition.

The service receives a visual description and a local match. If a specific
exhibit is matched, answers are grounded in a local exhibit card and the LLM is
used only to make the reply sound natural for voice playback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.visual_match_service import VisualMatchResult
from services.bailian_app_service import FALLBACK_TEXT, BailianAppService
from services.exhibit_knowledge_service import ExhibitCard, ExhibitKnowledgeStore
from services.vision_service import VisualDescription

SPECIFIC_MODE = "specific_explain"
CATEGORY_MODE = "category_guide"
RETAKE_MODE = "retake_request"

CATEGORY_THEMES = {
    "玉器": "应国文化、身份、礼仪、审美",
    "陶瓷": "鲁山花瓷、地方陶瓷工艺、釉色和器形",
    "青铜器": "古代礼制、贵族生活、应国文化",
    "石器": "早期生产生活、工具痕迹、材质和用途",
    "书画": "题材、笔墨、章法、地方文化记忆",
    "建筑构件": "建筑工艺、装饰寓意、空间礼制",
    "其他": "展览主题、参观路线、平顶山历史脉络",
    "无法判断": "平顶山博物馆展览主题",
}

LOCAL_CATEGORY_GUIDES = {
    "玉器": "这张照片更像玉器类展品。看玉器，可以先看颜色和温润感，再看造型是不是和身份、礼仪有关。平顶山一带的应国文化里，玉器常能帮助我们理解贵族审美和礼制。",
    "陶瓷": "这张照片更像陶瓷类展品。看陶瓷，可以先看器形，再看釉色、纹饰和口沿足底。平顶山周边有鲁山花瓷等陶瓷文化线索，能看出地方工艺的变化。",
    "青铜器": "这张照片更像青铜器类展品。看青铜器，可以先看器形用途，再看纹饰和锈色。它常和古代礼制、贵族生活、应国文化有关。",
    "石器": "这张照片更像石器类展品。看石器，可以注意材质、边缘磨损和形状用途。它们常指向早期生产生活，比如切割、打磨或祭祀场景。",
    "书画": "这张照片更像书画类展品。看书画，可以先看题材，再看线条、墨色、留白和题跋印章。",
    "建筑构件": "这张照片更像建筑构件。看这类展品，可以观察纹样、榫卯或装饰位置，想象它原来在建筑中的功能。",
}

RETAKE_ANSWER = "这张照片信息不太够。请把展品放在画面中间，靠近一点，避开展柜反光后重拍。"


@dataclass(frozen=True)
class GuideAnswerResult:
    mode: str
    grounded: bool
    answer_text: str
    gate_reason: str
    match_id: str = "none"
    match_name: str = "无"


class GuideAnswerService:
    """Build guide answers from local exhibit cards."""

    def __init__(
        self,
        bailian_app_service: BailianAppService | None = None,
        knowledge_store: ExhibitKnowledgeStore | None = None,
    ):
        self.bailian_app_service = bailian_app_service
        self.knowledge_store = knowledge_store or ExhibitKnowledgeStore()

    def build_answer(
        self,
        desc: VisualDescription,
        match: VisualMatchResult,
        *,
        user_question: str = "这是什么",
        device: str = "",
        image_id: str = "",
    ) -> GuideAnswerResult:
        mode, gate_reason = _choose_mode(desc, match)
        print(f"[GUIDE] mode={mode} gate_reason={gate_reason}", flush=True)

        if mode == RETAKE_MODE:
            return GuideAnswerResult(mode, False, RETAKE_ANSWER, gate_reason, match.match_id, match.match_name)
        if mode == SPECIFIC_MODE:
            card = self.knowledge_store.get(match.match_id)
            answer = self._ask_specific(desc, match, card, user_question=user_question, device=device, image_id=image_id)
            if _is_valid(answer):
                return GuideAnswerResult(mode, card is not None, _clean(answer), gate_reason, match.match_id, match.match_name)
            return self._fallback_specific(match, card, gate_reason)
        return self._build_category_answer(desc, gate_reason, user_question=user_question)

    async def build_answer_async(
        self,
        desc: VisualDescription,
        match: VisualMatchResult,
        *,
        user_question: str = "这是什么",
        device: str = "",
        image_id: str = "",
    ) -> GuideAnswerResult:
        mode, gate_reason = _choose_mode(desc, match)
        print(f"[GUIDE] mode={mode} gate_reason={gate_reason}", flush=True)

        if mode == RETAKE_MODE:
            return GuideAnswerResult(mode, False, RETAKE_ANSWER, gate_reason, match.match_id, match.match_name)
        if mode == SPECIFIC_MODE:
            card = self.knowledge_store.get(match.match_id)
            answer = await self._ask_specific_async(desc, match, card, user_question=user_question, device=device, image_id=image_id)
            if _is_valid(answer):
                return GuideAnswerResult(mode, card is not None, _clean(answer), gate_reason, match.match_id, match.match_name)
            return self._fallback_specific(match, card, gate_reason)
        return await self._build_category_answer_async(desc, gate_reason, user_question=user_question)

    def _ask_specific(
        self,
        desc: VisualDescription,
        match: VisualMatchResult,
        card: ExhibitCard | None,
        *,
        user_question: str,
        device: str,
        image_id: str,
    ) -> str:
        if self.bailian_app_service is None:
            return ""
        return self.bailian_app_service.ask(self._specific_prompt(desc, match, card, user_question=user_question))

    async def _ask_specific_async(
        self,
        desc: VisualDescription,
        match: VisualMatchResult,
        card: ExhibitCard | None,
        *,
        user_question: str,
        device: str,
        image_id: str,
    ) -> str:
        if self.bailian_app_service is None:
            return ""
        return await self.bailian_app_service.ask_async(self._specific_prompt(desc, match, card, user_question=user_question))

    def _specific_prompt(
        self,
        desc: VisualDescription,
        match: VisualMatchResult,
        card: ExhibitCard | None,
        *,
        user_question: str,
    ) -> str:
        confidence_note = (
            "匹配置信度中等，请使用'很像/可能是/建议以现场说明为准'这类保守措辞。"
            if match.confidence < 0.8
            else '可以说"很可能是"，但不要说成绝对确定。'
        )
        card_context = card.to_prompt_context() if card else "本地文物卡片缺失，只能依据视觉特征做保守讲解。"
        visible_features = "、".join(
            desc.shape_features[:4] + desc.decoration_features[:4] + desc.color_material[:4]
        ) or desc.visual_description[:120]
        return (
            "你是平顶山市博物馆语音导游。请只根据下面的本地资料和照片可见信息回答，资料没有的不要编造。\n"
            "先直接回答游客问题；如果问题很泛，再做简短讲解。能用常见追问资料时优先使用它。\n"
            "回答要适合设备语音播报，80到170字，不要 Markdown，不要项目符号。\n\n"
            f"{card_context}\n\n"
            f"视觉匹配：{match.match_name}，置信度 {match.confidence:.2f}，依据：{match.evidence or '视觉特征吻合'}。\n"
            f"游客问题：{_clean(user_question) or '这是什么'}。\n"
            f"照片可见特征：{visible_features}。\n"
            f"不确定因素：{desc.risk or '无'}。\n"
            f"{confidence_note}\n"
            "不要出现'根据知识库'、'检索结果显示'这类技术说法。"
        )

    def _fallback_specific(
        self,
        match: VisualMatchResult,
        card: ExhibitCard | None,
        gate_reason: str,
    ) -> GuideAnswerResult:
        if card and card.guide_text:
            answer = card.guide_text
        elif card:
            answer = f"这件展品很像{card.name}。你可以重点看它的材质、造型和纹饰；更具体的年代和出土信息，建议以现场展签为准。"
        else:
            answer = f"这件展品很像{match.match_name}。你可以先关注它的材质、造型和纹饰，再结合现场展签确认具体名称。"
        return GuideAnswerResult(SPECIFIC_MODE, card is not None, _clean(answer), f"{gate_reason}；本地降级讲解", match.match_id, match.match_name)

    def _ask_category(self, desc: VisualDescription, *, user_question: str) -> str:
        if self.bailian_app_service is None:
            return ""
        return self.bailian_app_service.ask(_category_prompt(desc, user_question=user_question))

    async def _ask_category_async(self, desc: VisualDescription, *, user_question: str) -> str:
        if self.bailian_app_service is None:
            return ""
        return await self.bailian_app_service.ask_async(_category_prompt(desc, user_question=user_question))

    def _build_category_answer(self, desc: VisualDescription, gate_reason: str, *, user_question: str) -> GuideAnswerResult:
        answer = self._ask_category(desc, user_question=user_question)
        if _is_valid(answer):
            return GuideAnswerResult(CATEGORY_MODE, False, _clean(answer), gate_reason)
        return _fallback_category(desc, gate_reason)

    async def _build_category_answer_async(self, desc: VisualDescription, gate_reason: str, *, user_question: str) -> GuideAnswerResult:
        answer = await self._ask_category_async(desc, user_question=user_question)
        if _is_valid(answer):
            return GuideAnswerResult(CATEGORY_MODE, False, _clean(answer), gate_reason)
        return _fallback_category(desc, gate_reason)


def _choose_mode(desc: VisualDescription, match: VisualMatchResult) -> tuple[str, str]:
    if match.is_matched:
        return SPECIFIC_MODE, f"本地视觉档案匹配成功 match_id={match.match_id} confidence={match.confidence:.2f}"
    if desc.category not in ("无法判断", "未知", ""):
        return CATEGORY_MODE, f"无具体匹配但类别已知 category={desc.category}"
    if not desc.is_clear:
        return RETAKE_MODE, "图片不清晰"
    return RETAKE_MODE, "无法识别"


def guide_response_payload(
    *,
    device: str,
    image_id: str,
    desc: VisualDescription,
    match: VisualMatchResult,
    guide: GuideAnswerResult,
) -> dict[str, Any]:
    return {
        "ok": True,
        "device": device,
        "image_id": image_id,
        "mode": guide.mode,
        "category": desc.category,
        "match_id": match.match_id,
        "match_name": match.match_name,
        "confidence": match.confidence,
        "evidence": match.evidence,
        "match_provider": match.provider,
        "visual_description": desc.visual_description,
        "shape_features": desc.shape_features,
        "decoration_features": desc.decoration_features,
        "color_material": desc.color_material,
        "search_keywords": desc.search_keywords,
        "is_clear": desc.is_clear,
        "risk": desc.risk,
        "need_retake": guide.mode == RETAKE_MODE,
        "answer_text": guide.answer_text,
        "grounded": guide.grounded,
        "gate_reason": guide.gate_reason,
    }


def _category_prompt(desc: VisualDescription, *, user_question: str) -> str:
    themes = CATEGORY_THEMES.get(desc.category, "平顶山博物馆展览主题")
    features = "、".join(desc.shape_features[:3] + desc.decoration_features[:3]) or "无"
    return (
        f"游客拍到的具体文物名称暂时无法确认，不能编造具体文物名称。\n"
        f"游客问题：{_clean(user_question) or '这是什么'}。\n"
        f'请围绕"{desc.category}"这类展品讲怎么看，并尽量结合相关主题：{themes}。\n'
        f"照片可见特征：{features}。\n"
        f"不确定因素：{desc.risk or '无'}。\n"
        "回答适合语音播报，50到120字，不要Markdown，不要项目符号，不要说识别失败。"
    )


def _fallback_category(desc: VisualDescription, gate_reason: str) -> GuideAnswerResult:
    return GuideAnswerResult(
        CATEGORY_MODE,
        False,
        LOCAL_CATEGORY_GUIDES.get(desc.category, RETAKE_ANSWER),
        f"{gate_reason}；本地类别讲解",
    )


def _is_valid(answer: str) -> bool:
    cleaned = _clean(answer)
    if not cleaned or cleaned == FALLBACK_TEXT:
        return False
    return "知识库无相关内容" not in cleaned


def _clean(answer: str) -> str:
    return " ".join((answer or "").strip().split())



"""Local exhibit knowledge cards.

The backend already knows the matched exhibit id after visual matching. For
runtime answers, a small local card is faster and safer than querying a remote
knowledge-base application.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.paths import KNOWLEDGE_CONFIG_DIR, PROJECT_ROOT

DEFAULT_CANDIDATES_PATH = KNOWLEDGE_CONFIG_DIR / "museum_vision_candidates.json"
DEFAULT_CARDS_PATH = KNOWLEDGE_CONFIG_DIR / "exhibit_cards.json"


@dataclass(frozen=True)
class ExhibitCard:
    """Curated local knowledge used to ground guide answers."""

    id: str
    name: str
    category: str
    aliases: list[str] = field(default_factory=list)
    museum: str = ""
    importance: str = ""
    basic_info: dict[str, str] = field(default_factory=dict)
    guide_text: str = ""
    visual_points: list[str] = field(default_factory=list)
    history_notes: list[str] = field(default_factory=list)
    talking_points: list[str] = field(default_factory=list)
    faq: list[dict[str, str]] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    do_not_say: list[str] = field(default_factory=list)

    def to_prompt_context(self, user_question: str = "") -> str:
        """Return a compact, source-grounded context block for the LLM."""
        basic = "；".join(f"{key}:{value}" for key, value in self.basic_info.items() if value)
        lines = [
            f"文物ID：{self.id}",
            f"标准名称：{self.name}",
            f"类别：{self.category}",
        ]
        if self.aliases:
            lines.append(f"别名：{'、'.join(self.aliases)}")
        if self.museum:
            lines.append(f"收藏单位：{self.museum}")
        if self.importance:
            lines.append(f"重要性：{self.importance}")
        if basic:
            lines.append(f"基础资料：{basic}")
        if self.guide_text:
            lines.append(f"讲解资料：{self.guide_text}")
        if self.visual_points:
            lines.append(f"视觉看点：{'、'.join(self.visual_points[:3] if _is_identity_question(user_question) else self.visual_points[:6])}")
        if _is_identity_question(user_question):
            if self.do_not_say:
                lines.append(f"禁止编造：{'、'.join(self.do_not_say[:3])}")
            return "\n".join(lines)
        history_notes = _select_relevant(self.history_notes, user_question, limit=3)
        if history_notes:
            lines.append(f"历史背景：{'、'.join(history_notes)}")
        talking_points = _select_relevant(self.talking_points, user_question, limit=4)
        if talking_points:
            lines.append(f"可讲要点：{'、'.join(talking_points)}")
        faq = _select_relevant_faq(self.faq, user_question, limit=4)
        if faq:
            faq_text = "；".join(
                f"问：{item['question']} 答：{item['answer']}"
                for item in faq
                if item.get("question") and item.get("answer")
            )
            if faq_text:
                lines.append(f"常见追问：{faq_text}")
        if self.do_not_say:
            lines.append(f"禁止编造：{'、'.join(self.do_not_say[:5])}")
        return "\n".join(lines)


class ExhibitKnowledgeStore:
    """Load local exhibit cards from JSON or candidate metadata."""

    def __init__(
        self,
        *,
        cards_path: Path = DEFAULT_CARDS_PATH,
        candidates_path: Path = DEFAULT_CANDIDATES_PATH,
    ):
        self.cards_path = _env_path("EXHIBIT_KNOWLEDGE_PATH", cards_path)
        self.candidates_path = _env_path("VISION_CANDIDATES_PATH", candidates_path)
        self.cards = self._load_cards()
        print(
            f"[KNOWLEDGE] loaded exhibit_cards={len(self.cards)} "
            f"cards_path={self.cards_path} candidates_path={self.candidates_path}",
            flush=True,
        )

    def get(self, exhibit_id: str) -> ExhibitCard | None:
        return self.cards.get((exhibit_id or "").strip())

    def count(self) -> int:
        return len(self.cards)

    def _load_cards(self) -> dict[str, ExhibitCard]:
        raw_cards = _read_json_entries(self.cards_path)
        if raw_cards:
            return {
                card.id: card
                for card in (_card_from_mapping(item) for item in raw_cards)
                if card.id and card.name
            }

        raw_candidates = _read_json_entries(self.candidates_path)
        return {
            card.id: card
            for card in (_card_from_candidate(item) for item in raw_candidates)
            if card.id and card.name
        }


def _card_from_candidate(item: dict[str, Any]) -> ExhibitCard:
    basic = item.get("basic_info") if isinstance(item.get("basic_info"), dict) else {}
    visual_points = _str_list(item.get("visual_features"))[:12]
    talking_points = _build_talking_points(item)
    return ExhibitCard(
        id=str(item.get("id") or "").strip(),
        name=str(item.get("standard_name") or item.get("name") or "").strip(),
        category=str(item.get("category") or "").strip(),
        aliases=_str_list(item.get("aliases")),
        museum=str(item.get("museum") or basic.get("collection") or "").strip(),
        importance=str(item.get("importance") or "").strip(),
        basic_info={str(key): str(value) for key, value in basic.items() if str(value).strip()},
        guide_text=str(item.get("guide_text") or "").strip(),
        visual_points=visual_points,
        history_notes=[],
        talking_points=talking_points,
        faq=[],
        source_urls=_str_list(item.get("source_urls")),
        do_not_say=[
            "不要编造资料中没有的年代",
            "不要编造资料中没有的出土地",
            "不要编造展柜位置",
            *_str_list(item.get("negative_rules"))[:3],
        ],
    )


def _card_from_mapping(item: dict[str, Any]) -> ExhibitCard:
    return ExhibitCard(
        id=str(item.get("id") or "").strip(),
        name=str(item.get("name") or item.get("standard_name") or "").strip(),
        category=str(item.get("category") or "").strip(),
        aliases=_str_list(item.get("aliases")),
        museum=str(item.get("museum") or "").strip(),
        importance=str(item.get("importance") or "").strip(),
        basic_info={
            str(key): str(value)
            for key, value in (item.get("basic_info") or {}).items()
            if str(value).strip()
        } if isinstance(item.get("basic_info"), dict) else {},
        guide_text=str(item.get("guide_text") or "").strip(),
        visual_points=_str_list(item.get("visual_points")),
        history_notes=_str_list(item.get("history_notes")),
        talking_points=_str_list(item.get("talking_points")),
        faq=_faq_list(item.get("faq")),
        source_urls=_str_list(item.get("source_urls")),
        do_not_say=_str_list(item.get("do_not_say")),
    )


def _build_talking_points(item: dict[str, Any]) -> list[str]:
    points = []
    guide_text = str(item.get("guide_text") or "").strip()
    if guide_text:
        points.append(guide_text)
    basic = item.get("basic_info") if isinstance(item.get("basic_info"), dict) else {}
    material = str(basic.get("material") or "").strip()
    usage = str(basic.get("usage") or "").strip()
    dynasty = str(basic.get("dynasty") or "").strip()
    excavation = str(basic.get("excavation") or "").strip()
    if material:
        points.append(f"材质：{material}")
    if usage:
        points.append(f"用途或性质：{usage}")
    if dynasty:
        points.append(f"时代：{dynasty}")
    if excavation:
        points.append(f"出土信息：{excavation}")
    return points


def _read_json_entries(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return [item for item in data["entries"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [dict(value, id=key) for key, value in data.items() if isinstance(value, dict)]
    return []


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _faq_list(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or item.get("q") or "").strip()
        answer = str(item.get("answer") or item.get("a") or "").strip()
        if question and answer:
            items.append({"question": question, "answer": answer})
    return items


def _select_relevant(items: list[str], query: str, *, limit: int) -> list[str]:
    if not items:
        return []
    ranked = sorted(
        enumerate(items),
        key=lambda pair: (-_relevance_score(pair[1], query), pair[0]),
    )
    return [item for _, item in ranked[:limit]]


def _select_relevant_faq(
    items: list[dict[str, str]],
    query: str,
    *,
    limit: int,
) -> list[dict[str, str]]:
    if not items:
        return []
    ranked = sorted(
        enumerate(items),
        key=lambda pair: (
            -_relevance_score(
                f"{pair[1].get('question', '')} {pair[1].get('answer', '')}",
                query,
            ),
            pair[0],
        ),
    )
    return [item for _, item in ranked[:limit]]


def _relevance_score(text: str, query: str) -> int:
    query = (query or "").strip()
    if not query:
        return 0
    text = text or ""
    score = 0
    for keyword in _query_keywords(query):
        if keyword and keyword in text:
            score += 4
    score += len((set(query) - set("这是什么的吗呢啊一件一个它")) & set(text))
    return score


def _query_keywords(query: str) -> list[str]:
    groups = {
        ("用", "用途", "干什么", "做什么", "作用"): ["用途", "性质", "佩", "盛", "酒", "水器", "礼器", "工具"],
        ("年代", "时候", "朝代", "时期"): ["时代", "西周", "春秋", "唐代", "年代", "时期"],
        ("材质", "材料", "什么做", "玉", "铜", "瓷"): ["材质", "白玉", "青铜", "陶瓷", "釉"],
        ("出土", "哪里", "在哪", "地方"): ["出土", "墓", "应国", "许国", "叶县", "平顶山"],
        ("名字", "为什么叫", "叫法", "名称"): ["叫", "名称", "束腰", "垂鳞纹", "盘龙钮", "线雕", "花口"],
        ("故事", "历史", "重要", "特别", "讲讲", "介绍"): ["历史", "重要", "文化", "关系", "贵族", "礼制", "鹰城"],
        ("看", "特征", "认出", "识别"): ["看", "特征", "视觉", "纹", "造型", "器身", "双耳", "三足"],
    }
    keywords = [query]
    for triggers, additions in groups.items():
        if any(trigger in query for trigger in triggers):
            keywords.extend(additions)
    return keywords


def _is_identity_question(query: str) -> bool:
    query = (query or "").strip()
    return any(token in query for token in ("这是什么", "叫什么", "它是什么", "这个是", "这件是"))

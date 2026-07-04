"""Local visual artifact matching.

Runtime matching is intentionally local and fast:
1. The uploaded photo is described by the vision model as ``VisualDescription``.
2. This service compares that description with prebuilt baseline vision profiles.
3. It returns the closest exhibit id without calling a Bailian/RAG application.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from core.paths import KNOWLEDGE_CONFIG_DIR, PROJECT_ROOT
from services.vision_service import VisualDescription

DEFAULT_CANDIDATES_PATH = KNOWLEDGE_CONFIG_DIR / "museum_vision_candidates.json"
DEFAULT_PROFILES_PATH = KNOWLEDGE_CONFIG_DIR / "vision_profiles.json"
DEFAULT_MIN_CONFIDENCE = 0.42
DEFAULT_MATCH_CONFIDENCE = 0.60
DEFAULT_MIN_MARGIN = 0.08
DEFAULT_SCORE_NORMALIZER = 1.20


@dataclass(frozen=True)
class VisualMatchResult:
    """Result of local visual matching."""

    match_id: str = "none"
    match_name: str = "无"
    confidence: float = 0.0
    evidence: str = ""
    raw_response: str = ""
    provider: str = "local_visual_profile"

    @property
    def is_matched(self) -> bool:
        threshold = _env_float(("VISUAL_MATCH_ACCEPT_CONFIDENCE", "ARTIFACT_MATCH_CONFIDENCE"), DEFAULT_MATCH_CONFIDENCE)
        return self.match_id != "none" and self.confidence >= threshold

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VisualMatchService:
    """Match a user's visual description against local baseline profiles."""

    def __init__(
        self,
        *,
        candidates_path: Path = DEFAULT_CANDIDATES_PATH,
        profiles_path: Path = DEFAULT_PROFILES_PATH,
    ):
        self.candidates_path = _env_path("VISION_CANDIDATES_PATH", candidates_path)
        self.profiles_path = _env_path("VISION_PROFILES_PATH", profiles_path)
        self.candidates = self._load_candidates(self.candidates_path)
        self.profiles = self._load_profiles(self.profiles_path, self.candidates)
        print(
            f"[VISUAL-MATCH] loaded candidates={len(self.candidates)} "
            f"profiles={len(self.profiles)} profiles_path={self.profiles_path}",
            flush=True,
        )

    def match(self, desc: VisualDescription) -> VisualMatchResult:
        """Synchronous wrapper for scripts."""
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError("VisualMatchService.match() cannot run inside an event loop; use match_async()")
        return asyncio.run(self.match_async(desc))

    async def match_async(self, desc: VisualDescription) -> VisualMatchResult:
        """Return the best local visual match."""
        total_start = time.perf_counter()
        best, runner_up = self._match_local(desc)
        margin = best.confidence - runner_up.confidence
        min_confidence = _env_float(("VISUAL_MATCH_MIN_CONFIDENCE", "ARTIFACT_LOCAL_MIN_CONFIDENCE"), DEFAULT_MIN_CONFIDENCE)
        min_margin = _env_float(("VISUAL_MATCH_MIN_MARGIN", "ARTIFACT_LOCAL_MIN_MARGIN"), DEFAULT_MIN_MARGIN)

        if best.confidence < min_confidence:
            result = VisualMatchResult(
                evidence=(
                    f"本地视觉档案匹配置信度过低，最佳候选 "
                    f"{best.match_name}={best.confidence:.2f}"
                ),
                provider="local_visual_profile",
            )
        elif runner_up.match_id != "none" and margin < min_margin:
            result = VisualMatchResult(
                evidence=(
                    f"本地视觉档案候选过于接近，最佳候选 {best.match_name}={best.confidence:.2f}，"
                    f"第二候选 {runner_up.match_name}={runner_up.confidence:.2f}，margin={margin:.2f}"
                ),
                provider="local_visual_profile",
            )
        else:
            result = VisualMatchResult(
                match_id=best.match_id,
                match_name=best.match_name,
                confidence=best.confidence,
                evidence=f"{best.evidence}；runner_up={runner_up.match_id}:{runner_up.confidence:.2f}；margin={margin:.2f}",
                provider="local_visual_profile",
            )

        print(
            f"[VISUAL-MATCH] provider={result.provider} match_id={result.match_id} "
            f"match_name={result.match_name} confidence={result.confidence:.2f} "
            f"cost={time.perf_counter() - total_start:.3f}s",
            flush=True,
        )
        return result

    def _match_local(self, desc: VisualDescription) -> tuple[VisualMatchResult, VisualMatchResult]:
        if not self.profiles:
            empty = VisualMatchResult(evidence="本地视觉档案为空")
            return empty, empty

        query_text = _normalize_text(
            "\n".join(
                [
                    desc.category,
                    desc.visual_description,
                    " ".join(desc.shape_features),
                    " ".join(desc.decoration_features),
                    " ".join(desc.color_material),
                    " ".join(desc.search_keywords),
                ]
            )
        )
        scored = [self._score_profile(desc, query_text, profile) for profile in self.profiles]
        scored.sort(key=lambda item: item.confidence, reverse=True)
        best = scored[0]
        runner_up = scored[1] if len(scored) > 1 else VisualMatchResult(provider="local_visual_profile")
        return best, runner_up

    def _score_profile(
        self,
        desc: VisualDescription,
        query_text: str,
        profile: dict[str, Any],
    ) -> VisualMatchResult:
        score = 0.0
        evidence: list[str] = []
        category = str(profile.get("category") or "").strip()

        if desc.category and desc.category not in {"无法判断", "未知"}:
            if category == desc.category:
                score += 0.18
                evidence.append(f"类别:{category}")
            else:
                score -= 0.12

        weighted_terms = [
            (("name", "standard_name", "aliases"), 0.06, "名称"),
            (("visual_keywords",), 0.11, "关键词"),
            (("shape_features",), 0.11, "形态"),
            (("decoration_features",), 0.10, "纹饰"),
            (("color_material",), 0.09, "材质"),
            (("visual_features", "local_match_terms"), 0.10, "候选特征"),
        ]
        for keys, weight, label in weighted_terms:
            for term in _candidate_terms(profile, keys):
                if _term_matches(query_text, term):
                    score += weight
                    evidence.append(f"{label}:{term}")

        for term in _candidate_terms(profile, ("negative_terms", "negative_rules")):
            if _term_matches(query_text, term):
                score -= 0.16
                evidence.append(f"排除:{term}")

        score += _description_overlap_score(desc, profile)
        bonus, bonus_evidence = _domain_bonus_score(profile, query_text)
        if bonus:
            score += bonus
            evidence.append(bonus_evidence)

        try:
            priority = float(profile.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0.0
        score += min(max(priority, 0.0), 100.0) / 1500.0

        normalizer = _env_float(("VISUAL_MATCH_SCORE_NORMALIZER", "ARTIFACT_SCORE_NORMALIZER"), DEFAULT_SCORE_NORMALIZER)
        confidence = max(0.0, min(score / max(normalizer, 0.1), 0.98))
        match_id = str(profile.get("id") or profile.get("candidate_id") or "none").strip() or "none"
        match_name = str(profile.get("standard_name") or profile.get("name") or "无").strip() or "无"
        evidence_text = "；".join(evidence[:10]) if evidence else "未命中明确视觉特征"
        return VisualMatchResult(
            match_id=match_id,
            match_name=match_name,
            confidence=confidence,
            evidence=evidence_text,
            provider="local_visual_profile",
        )

    def _load_candidates(self, path: Path) -> list[dict[str, Any]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[VISUAL-MATCH] candidates load failed path={path} error={exc}", flush=True)
            return []
        if not isinstance(data, list):
            print(f"[VISUAL-MATCH] candidates must be a list path={path}", flush=True)
            return []
        return [item for item in data if isinstance(item, dict)]

    def _load_profiles(self, path: Path, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {str(item.get("id") or "").strip(): dict(item) for item in candidates}
        raw_profiles = _read_profile_entries(path)
        if not raw_profiles:
            print(f"[VISUAL-MATCH] profiles missing or empty, using candidate features path={path}", flush=True)
            raw_profiles = []

        profiles_by_id: dict[str, dict[str, Any]] = {}
        for entry in raw_profiles:
            candidate_id = str(entry.get("candidate_id") or entry.get("id") or "").strip()
            if not candidate_id:
                continue
            profile = dict(by_id.get(candidate_id, {}))
            normalized = _normalize_profile_entry(entry)
            profile.update({key: value for key, value in normalized.items() if value not in ("", [])})
            profiles_by_id[candidate_id] = profile

        for candidate_id, candidate in by_id.items():
            if candidate_id not in profiles_by_id:
                profiles_by_id[candidate_id] = _normalize_profile_entry(candidate)

        return [profile for profile in profiles_by_id.values() if profile.get("id") or profile.get("candidate_id")]


def _read_profile_entries(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict) and isinstance(data.get("entries"), list):
        return [item for item in data["entries"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        return [dict(value, candidate_id=key) for key, value in data.items() if isinstance(value, dict)]
    return []


def _normalize_profile_entry(entry: dict[str, Any]) -> dict[str, Any]:
    profile = entry.get("visual_profile") if isinstance(entry.get("visual_profile"), dict) else {}
    candidate_id = str(entry.get("candidate_id") or entry.get("id") or profile.get("id") or "").strip()
    standard_name = str(entry.get("standard_name") or entry.get("name") or profile.get("name") or "").strip()
    detailed = str(
        entry.get("detailed_visual_description")
        or entry.get("visual_description")
        or profile.get("search_text")
        or profile.get("overall_shape")
        or ""
    ).strip()
    return {
        **entry,
        "id": candidate_id,
        "candidate_id": candidate_id,
        "standard_name": standard_name,
        "name": standard_name or str(entry.get("name") or "").strip(),
        "category": str(entry.get("category") or profile.get("category") or "").strip(),
        "detailed_visual_description": detailed,
        "visual_keywords": _str_list(entry.get("visual_keywords") or profile.get("visual_keywords")),
        "shape_features": _str_list(entry.get("shape_features") or profile.get("distinctive_parts")),
        "decoration_features": _str_list(entry.get("decoration_features") or profile.get("decoration")),
        "color_material": _str_list(entry.get("color_material") or profile.get("material_color")),
        "negative_rules": _str_list(entry.get("negative_rules") or profile.get("negative_rules")),
    }


def _description_overlap_score(desc: VisualDescription, profile: dict[str, Any]) -> float:
    profile_text = _normalize_text(
        "\n".join(
            [
                str(profile.get("detailed_visual_description") or ""),
                " ".join(_candidate_terms(profile, ("visual_keywords", "shape_features", "decoration_features", "color_material"))),
            ]
        )
    )
    if not profile_text:
        return 0.0
    query_terms = []
    query_terms.extend(desc.shape_features)
    query_terms.extend(desc.decoration_features)
    query_terms.extend(desc.color_material)
    query_terms.extend(desc.search_keywords)
    hits = 0
    for term in query_terms:
        normalized = _normalize_text(term)
        if len(normalized) >= 2 and normalized in profile_text:
            hits += 1
    return min(hits * 0.035, 0.18)


def _domain_bonus_score(profile: dict[str, Any], query_text: str) -> tuple[float, str]:
    profile_id = str(profile.get("id") or profile.get("candidate_id") or "").strip()
    if profile_id == "denggong_gui":
        terms = ("球形", "圆润", "带盖", "盖子", "盖顶", "环耳", "双耳", "弦纹", "环带", "圈足")
        hits = [term for term in terms if term in query_text]
        if len(hits) >= 4:
            return 0.46, f"组合特征:邓公簋({'/'.join(hits[:5])})"
    return 0.0, ""


def _env_path(name: str, default: Path) -> Path:
    value = os.getenv(name, "").strip()
    path = Path(value) if value else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _env_float(names: str | tuple[str, ...], default: float) -> float:
    for name in (names if isinstance(names, tuple) else (names,)):
        value = os.getenv(name, "").strip()
        if not value:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return default


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def _candidate_terms(candidate: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
    terms: list[str] = []
    for key in keys:
        value = candidate.get(key)
        if isinstance(value, str):
            terms.append(value)
        elif isinstance(value, list):
            terms.extend(str(item) for item in value if str(item).strip())
        elif isinstance(value, dict):
            terms.extend(str(item) for item in value.values() if str(item).strip())
    return terms


def _term_matches(query_text: str, term: str) -> bool:
    normalized = _normalize_text(term)
    if not normalized or len(normalized) < 2:
        return False
    if normalized in query_text:
        return True
    parts = [
        part
        for part in re.split(r"[、，,;/；\s]|或|和|与|及|的", normalized)
        if len(part) >= 2
    ]
    return bool(parts and any(part in query_text for part in parts))


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


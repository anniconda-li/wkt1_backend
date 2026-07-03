"""相机导游调试模块。

提供端到端的相机导游测试功能：
1. 调用视觉服务分析图片 → VisualDescription
2. 调用本地视觉档案匹配 → VisualMatchResult
3. 调用拍照导游服务生成讲解 → GuideAnswerResult

流程：拍照 → 视觉描述 → 本地匹配 → 读取本地卡片 → 问答组织讲解
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.config  # noqa: E402,F401 - 加载项目 .env 环境变量
from core.paths import DEFAULT_CAMERA_TEST_IMAGE, ensure_project_dirs
from services.bailian_app_service import BailianAppService
from services.exhibit_knowledge_service import ExhibitKnowledgeStore
from services.visual_match_service import VisualMatchService
from services.guide_answer_service import GuideAnswerService
from services.vision_service import VisionService

# 默认测试提问文本
DEFAULT_CAMERA_GUIDE_TEST_TEXT = "这是什么"
logger = logging.getLogger(__name__)


async def run_camera_guide_check(
    *,
    vision_service: VisionService,
    visual_match: VisualMatchService,
    guide_answer_service: GuideAnswerService,
    test_image_path: Path = DEFAULT_CAMERA_TEST_IMAGE,
    user_text: str = DEFAULT_CAMERA_GUIDE_TEST_TEXT,
) -> dict[str, Any]:
    """运行一次完整的相机导游测试（新架构）。

    流程：
    1. 检查测试图片是否存在
    2. 视觉分析 → VisualDescription
    3. 本地视觉匹配 → VisualMatchResult
    4. 导游讲解 → GuideAnswerResult
    5. 返回包含所有中间结果和耗时统计的字典

    Args:
        vision_service: 视觉服务实例
        visual_match: 本地视觉匹配服务实例
        guide_answer_service: 导游讲解组织服务实例
        test_image_path: 测试图片路径
        user_text: 模拟用户提问

    Returns:
        dict: 结果字典，ok=True 表示成功
    """
    total_start = time.perf_counter()
    test_image_path = Path(test_image_path)

    # 检查图片是否存在
    if not test_image_path.exists():
        return _failure(
            stage="image_not_found",
            error_type="FileNotFoundError",
            error=f"测试图片不存在：{test_image_path}",
            test_image_path=test_image_path,
            total_start=total_start,
        )

    # 第 1 步：视觉描述
    vision_start = time.perf_counter()
    try:
        desc = await asyncio.to_thread(vision_service.analyze_image, test_image_path)
    except Exception as exc:
        return _failure(
            stage="vision",
            error_type=type(exc).__name__,
            error=str(exc),
            test_image_path=test_image_path,
            total_start=total_start,
        )
    vision_elapsed_ms = _elapsed_ms(vision_start)

    # 第 2 步：本地视觉匹配
    match_start = time.perf_counter()
    try:
        match = await visual_match.match_async(desc)
    except Exception as exc:
        return _failure(
            stage="search",
            error_type=type(exc).__name__,
            error=str(exc),
            test_image_path=test_image_path,
            total_start=total_start,
            extra={"vision_elapsed_ms": vision_elapsed_ms, "desc": desc.to_dict()},
        )
    match_elapsed_ms = _elapsed_ms(match_start)

    # 第 3 步：生成导游讲解
    guide_start = time.perf_counter()
    try:
        guide = await guide_answer_service.build_answer_async(
            desc, match, user_question=user_text, device="debug", image_id=test_image_path.stem,
        )
    except Exception as exc:
        return _failure(
            stage="guide",
            error_type=type(exc).__name__,
            error=str(exc),
            test_image_path=test_image_path,
            total_start=total_start,
        )
    guide_elapsed_ms = _elapsed_ms(guide_start)

    # 成功完成
    total_elapsed_ms = _elapsed_ms(total_start)
    result = {
        "ok": True,
        "test_image_path": str(test_image_path),
        "user_text": user_text,
        "visual_description": desc.to_dict(),
        "match_result": match.to_dict(),
        "guide_result": {
            "mode": guide.mode,
            "grounded": guide.grounded,
            "answer_text": guide.answer_text,
            "gate_reason": guide.gate_reason,
        },
        "timing": {
            "vision_elapsed_ms": vision_elapsed_ms,
            "match_elapsed_ms": match_elapsed_ms,
            "guide_elapsed_ms": guide_elapsed_ms,
            "total_elapsed_ms": total_elapsed_ms,
        },
    }
    _log_debug(result)
    return result


def _failure(
    *,
    stage: str,
    error_type: str,
    error: str,
    test_image_path: Path,
    total_start: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建失败结果字典。"""
    data: dict[str, Any] = {
        "ok": False,
        "stage": stage,
        "error_type": error_type,
        "error": error,
        "test_image_path": str(test_image_path),
        "timing": {"total_elapsed_ms": _elapsed_ms(total_start)},
    }
    if extra:
        data.update(extra)
    return data


def _log_debug(payload: dict[str, Any]) -> None:
    """输出相机导游调试日志（JSON 格式）。"""
    text = json.dumps(payload, ensure_ascii=False)
    logger.info("[CAMERA-GUIDE-CHECK] %s", text)
    print(f"[CAMERA-GUIDE-CHECK] {text}", flush=True)


def _elapsed_ms(start: float) -> int:
    """计算从 start 到现在的毫秒数。"""
    return int((time.perf_counter() - start) * 1000)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行一次相机导游链路检查")
    parser.add_argument(
        "--image",
        default=str(DEFAULT_CAMERA_TEST_IMAGE),
        help=f"测试图片路径，默认使用 {DEFAULT_CAMERA_TEST_IMAGE}",
    )
    parser.add_argument("--text", default=DEFAULT_CAMERA_GUIDE_TEST_TEXT, help="模拟用户提问")
    parser.add_argument("--mock-vision", action="store_true", help="使用 mock 视觉描述，不调用视觉模型")
    parser.add_argument("--no-bailian", action="store_true", help="跳过百炼组织回答，使用本地降级讲解")
    args = parser.parse_args()

    ensure_project_dirs()
    bailian = None if args.no_bailian else BailianAppService()
    result = asyncio.run(
        run_camera_guide_check(
            vision_service=VisionService(provider="mock" if args.mock_vision else None),
            visual_match=VisualMatchService(),
            guide_answer_service=GuideAnswerService(bailian, ExhibitKnowledgeStore()),
            test_image_path=Path(args.image),
            user_text=args.text,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())



#!/usr/bin/env python3
"""文物识别管线测试脚本。

端到端测试：图片 → 视觉描述 → 知识库检索 → 文物匹配 → 导游讲解。

用法:
    # 只用图片测试
    python tools/test_artifact_pipeline.py --image photo/test_exhibit.jpg

    # 指定图片和问题
    python tools/test_artifact_pipeline.py --image photo/test_exhibit.jpg --question "这是什么"

    # 使用 mock 模式（不调用 API）
    python tools/test_artifact_pipeline.py --image photo/ying.jpg --mock

要求:
    pip install dashscope httpx pillow
    设置环境变量 DASHSCOPE_API_KEY（使用 DashScope 模式时）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import wave
from datetime import datetime
from pathlib import Path

# 将项目根目录加入 sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import core.config  # noqa: F401 - 加载 .env
from core.paths import DEFAULT_CAMERA_TEST_IMAGE, TMP_DEBUG_DIR, ensure_runtime_dirs
from services.artifact_search_service import ArtifactSearchService
from services.bailian_app_service import BailianAppService
from services.photo_guide_service import PhotoGuideService
from services.tts_service import synthesize_wav_16k
from services.vision_service import VisionService

DEFAULT_OUTPUT_DIR = TMP_DEBUG_DIR / "artifact_pipeline"


def print_separator(title: str = "") -> None:
    """打印分隔线。"""
    width = 70
    if title:
        padding = max(0, (width - len(title) - 2) // 2)
        print(f"\n{'=' * padding} {title} {'=' * padding}")
    else:
        print("-" * width)


def print_result(label: str, value: str, indent: int = 2) -> None:
    """格式化打印键值对。"""
    prefix = " " * indent
    if len(value) > 80:
        print(f"{prefix}{label}:")
        print(f"{prefix}  {value[:200]}")
        if len(value) > 200:
            print(f"{prefix}  ... (共 {len(value)} 字)")
    else:
        print(f"{prefix}{label}: {value}")


def print_list(label: str, items: list[str], indent: int = 2) -> None:
    """格式化打印列表。"""
    prefix = " " * indent
    if items:
        print(f"{prefix}{label}: {' / '.join(items)}")
    else:
        print(f"{prefix}{label}: (无)")


def resolve_output_dir(path_text: str) -> Path:
    """解析输出目录路径，支持相对项目根目录。"""
    output_dir = Path(path_text)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def validate_esp_wav(path: Path) -> dict[str, object]:
    """验证 TTS 输出是否符合 ESP32 播放要求。"""
    with wave.open(str(path), "rb") as wf:
        info = {
            "channels": wf.getnchannels(),
            "sample_width": wf.getsampwidth(),
            "sample_rate": wf.getframerate(),
            "frames": wf.getnframes(),
            "duration": wf.getnframes() / wf.getframerate() if wf.getframerate() else 0.0,
            "compression": wf.getcomptype(),
            "compression_name": wf.getcompname(),
        }

    if info["channels"] != 1:
        raise RuntimeError(f"WAV 声道数必须为 1，当前 {info['channels']}")
    if info["sample_width"] != 2:
        raise RuntimeError(f"WAV 采样位宽必须为 2，当前 {info['sample_width']}")
    if info["sample_rate"] != 16000:
        raise RuntimeError(f"WAV 采样率必须为 16000，当前 {info['sample_rate']}")
    if info["compression"] != "NONE":
        raise RuntimeError(f"WAV 压缩格式必须为 NONE，当前 {info['compression']}")
    return info


async def run_test(
    image_path: str,
    user_question: str = "这是什么",
    use_mock: bool = False,
    output_dir: str = str(DEFAULT_OUTPUT_DIR),
) -> dict:
    """运行一次完整的文物识别管线测试。

    流程：
    1. VisionService → VisualDescription
    2. ArtifactSearchService → ArtifactMatchResult
    3. PhotoGuideService → PhotoGuideResult

    Args:
        image_path: 测试图片路径
        user_question: 用户问题
        use_mock: 是否使用 mock 模式
        output_dir: TTS 回复 WAV 输出目录

    Returns:
        dict: 完整测试结果
    """
    ensure_runtime_dirs()
    image_path = Path(image_path)
    output_path_dir = resolve_output_dir(output_dir)

    if not image_path.exists():
        print(f"[ERROR] 图片不存在: {image_path}")
        return {"ok": False, "error": f"图片不存在: {image_path}"}

    print_separator("测试配置")
    print_result("图片路径", str(image_path))
    print_result("用户问题", user_question)
    print_result("输出目录", str(output_path_dir))
    print_result("视觉模式", "mock" if use_mock else os.getenv("VISION_PROVIDER", "dashscope"))
    print_result("TTS模式", os.getenv("TTS_PROVIDER", "mock"))
    vision_app_id = os.getenv("BAILIAN_VISION_APP_ID", "未配置")
    qa_app_id = os.getenv("BAILIAN_QA_APP_ID", "未配置")
    print_result("视觉检索应用ID", vision_app_id[:8] + "***" if len(vision_app_id) > 8 else vision_app_id)
    print_result("问答应用ID", qa_app_id[:8] + "***" if len(qa_app_id) > 8 else qa_app_id)

    # 初始化服务
    if use_mock:
        vision_service = VisionService(provider="mock")
    else:
        vision_service = VisionService()

    bailian_vision = BailianAppService(app_id=os.getenv("BAILIAN_VISION_APP_ID", ""))
    bailian_qa = BailianAppService(app_id=os.getenv("BAILIAN_QA_APP_ID", ""))
    artifact_search = ArtifactSearchService(bailian_vision)
    photo_guide = PhotoGuideService(bailian_qa)

    total_start = time.perf_counter()

    # ---- 第 1 步：视觉描述 ----
    print_separator("第 1 步：视觉描述")
    vision_start = time.perf_counter()
    try:
        desc = await asyncio.to_thread(vision_service.analyze_image, image_path)
    except Exception as exc:
        print(f"[FAIL] 视觉分析异常: {exc}")
        return {"ok": False, "stage": "vision", "error": str(exc)}
    vision_cost = time.perf_counter() - vision_start

    print_result("类别", desc.category)
    print_result("是否清晰", str(desc.is_clear))
    print_result("模型置信度", f"{desc.confidence:.2f}")
    print_result("视觉描述", desc.visual_description)
    print_list("形态特征", desc.shape_features)
    print_list("纹饰特征", desc.decoration_features)
    print_list("颜色材质", desc.color_material)
    print_list("搜索关键词", desc.search_keywords)
    if desc.risk:
        print_result("风险提示", desc.risk)
    print_result("耗时", f"{vision_cost:.3f}s")

    # ---- 第 2 步：知识库检索 ----
    print_separator("第 2 步：知识库检索")
    search_start = time.perf_counter()
    try:
        match = await artifact_search.search_async(desc)
    except Exception as exc:
        print(f"[FAIL] 知识库检索异常: {exc}")
        return {"ok": False, "stage": "search", "error": str(exc), "visual_description": desc.to_dict()}
    search_cost = time.perf_counter() - search_start

    print_result("匹配ID", match.match_id)
    print_result("匹配名称", match.match_name)
    print_result("匹配置信度", f"{match.confidence:.2f}")
    print_result("匹配依据", match.evidence or "(无)")
    if match.match_id != "none":
        status = "[OK] 匹配成功" if match.is_matched else "[WARN] 置信度不足"
        print_result("匹配状态", status)
    else:
        print_result("匹配状态", "[INFO] 无匹配")
    print_result("耗时", f"{search_cost:.3f}s")

    # ---- 第 3 步：导游讲解 ----
    print_separator("第 3 步：导游讲解")
    guide_start = time.perf_counter()
    try:
        guide = await photo_guide.build_answer_async(
            desc, match, device="test", image_id=image_path.stem,
        )
    except Exception as exc:
        print(f"[FAIL] 导游讲解异常: {exc}")
        return {
            "ok": False, "stage": "guide", "error": str(exc),
            "visual_description": desc.to_dict(), "match_result": match.to_dict(),
        }
    guide_cost = time.perf_counter() - guide_start

    print_result("讲解模式", guide.mode)
    print_result("是否基于检索", str(guide.grounded))
    print_result("模式原因", guide.gate_reason)
    print_result("讲解文本", guide.answer_text)
    print_result("耗时", f"{guide_cost:.3f}s")

    # ---- 第 4 步：讲解文本转 WAV ----
    print_separator("第 4 步：讲解转语音")
    tts_start = time.perf_counter()
    try:
        reply_wav = await asyncio.to_thread(synthesize_wav_16k, guide.answer_text)
        reply_wav_path = output_path_dir / f"guide_reply_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
        reply_wav_path.write_bytes(reply_wav)
        wav_info = validate_esp_wav(reply_wav_path)
    except Exception as exc:
        print(f"[FAIL] TTS 合成异常: {exc}")
        return {
            "ok": False,
            "stage": "tts",
            "error": str(exc),
            "visual_description": desc.to_dict(),
            "match_result": match.to_dict(),
            "guide_result": {
                "mode": guide.mode,
                "grounded": guide.grounded,
                "answer_text": guide.answer_text,
                "gate_reason": guide.gate_reason,
            },
        }
    tts_cost = time.perf_counter() - tts_start

    print_result("输出WAV", str(reply_wav_path))
    print_result("文件大小", f"{reply_wav_path.stat().st_size} bytes")
    print_result(
        "音频格式",
        (
            f"channels={wav_info['channels']} sample_width={wav_info['sample_width']} "
            f"sample_rate={wav_info['sample_rate']} duration={wav_info['duration']:.3f}s "
            f"compression={wav_info['compression']}"
        ),
    )
    print_result("耗时", f"{tts_cost:.3f}s")

    # ---- 总耗时 ----
    total_cost = time.perf_counter() - total_start
    print_separator("总耗时")
    print(
        f"  {total_cost:.3f}s "
        f"(视觉={vision_cost:.3f}s 检索={search_cost:.3f}s 讲解={guide_cost:.3f}s TTS={tts_cost:.3f}s)"
    )

    return {
        "ok": True,
        "image_path": str(image_path),
        "user_question": user_question,
        "reply_wav": str(reply_wav_path),
        "reply_wav_size": reply_wav_path.stat().st_size,
        "reply_wav_info": wav_info,
        "visual_description": desc.to_dict(),
        "match_result": match.to_dict(),
        "guide_result": {
            "mode": guide.mode,
            "grounded": guide.grounded,
            "answer_text": guide.answer_text,
            "gate_reason": guide.gate_reason,
        },
        "timing": {
            "vision_elapsed_s": round(vision_cost, 3),
            "search_elapsed_s": round(search_cost, 3),
            "guide_elapsed_s": round(guide_cost, 3),
            "tts_elapsed_s": round(tts_cost, 3),
            "total_elapsed_s": round(total_cost, 3),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="文物识别管线测试：图片 → 视觉描述 → KB检索 → 导游讲解",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python tools/test_artifact_pipeline.py --image photo/test.jpg
  python tools/test_artifact_pipeline.py --image photo/test.jpg --question "这是什么文物"
  python tools/test_artifact_pipeline.py --image photo/test.jpg --mock
  python tools/test_artifact_pipeline.py --image photo/test.jpg --out-dir tmp/debug/artifact_pipeline
  python tools/test_artifact_pipeline.py --image photo/test.jpg --json
        """,
    )
    parser.add_argument(
        "--image", "-i",
        type=str,
        default=str(DEFAULT_CAMERA_TEST_IMAGE),
        help=f"测试图片路径（默认: {DEFAULT_CAMERA_TEST_IMAGE}）",
    )
    parser.add_argument(
        "--question", "-q",
        type=str,
        default="这是什么",
        help="模拟用户问题（默认: 这是什么）",
    )
    parser.add_argument(
        "--mock", "-m",
        action="store_true",
        help="使用 mock 模式，不调用真实 API",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=str(DEFAULT_OUTPUT_DIR.relative_to(PROJECT_ROOT)),
        help=f"TTS 回复 WAV 输出目录（默认: {DEFAULT_OUTPUT_DIR.relative_to(PROJECT_ROOT)}）",
    )
    parser.add_argument(
        "--json", "-j",
        action="store_true",
        help="以 JSON 格式输出最终结果",
    )
    args = parser.parse_args()

    result = asyncio.run(run_test(
        image_path=args.image,
        user_question=args.question,
        use_mock=args.mock,
        output_dir=args.out_dir,
    ))

    if args.json:
        print("\n" + json.dumps(result, ensure_ascii=False, indent=2))

    if not result.get("ok"):
        print(f"\n[FAIL] 测试失败 stage={result.get('stage', 'unknown')} error={result.get('error', '')}")
        sys.exit(1)
    else:
        print(f"\n[OK] 测试完成")
        if result["guide_result"]["grounded"]:
            print(f"  匹配文物: {result['match_result']['match_name']}")
        print(f"  讲解模式: {result['guide_result']['mode']}")
        print(f"  回复音频: {result['reply_wav']}")


if __name__ == "__main__":
    main()

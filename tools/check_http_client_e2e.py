"""HTTP 客户端端到端测试脚本。

这个脚本不启动后端服务，只模拟 ESP32 客户端向已经运行的后端发送
图片和音频：
1. GET /healthz 和 /readyz 检查服务是否可用
2. POST /camera/upload 上传 JPEG
3. POST /ai/start 创建语音会话
4. POST /ai/upload 分片上传 WAV
5. POST /ai/finish 触发 ASR -> LLM -> TTS
6. 轮询 /ai/result_info
7. POST /ai/result_chunk 下载回复 WAV

用法示例：
    python tools/check_http_client_e2e.py --base-url http://127.0.0.1:18080 \
        --image tests/data/camera/yingguo_yuying.jpg \
        --wav tests/data/audio/what.wav
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = PROJECT_ROOT / "tests" / "data" / "camera" / "yingguo_yuying.jpg"
DEFAULT_WAV = PROJECT_ROOT / "tests" / "data" / "audio" / "what.wav"
DEFAULT_OUT_DIR = PROJECT_ROOT / "tmp" / "debug" / "http_client_e2e"
DEFAULT_CHUNK_SIZE = 32768


def resolve_path(path_text: str) -> Path:
    """解析命令行路径，支持相对项目根目录。"""
    path = Path(path_text)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def validate_esp_wav(path: Path) -> dict[str, object]:
    """校验上传 WAV 是否符合 ESP 客户端格式。"""
    with wave.open(str(path), "rb") as wf:
        info = {
            "channels": wf.getnchannels(),
            "sample_width": wf.getsampwidth(),
            "sample_rate": wf.getframerate(),
            "frames": wf.getnframes(),
            "duration": wf.getnframes() / wf.getframerate() if wf.getframerate() else 0.0,
            "compression": wf.getcomptype(),
        }
    if info["channels"] != 1:
        raise RuntimeError(f"WAV 必须是单声道，当前 channels={info['channels']}")
    if info["sample_width"] != 2:
        raise RuntimeError(f"WAV 必须是 16-bit，当前 sample_width={info['sample_width']}")
    if info["sample_rate"] != 16000:
        raise RuntimeError(f"WAV 必须是 16000Hz，当前 sample_rate={info['sample_rate']}")
    if info["compression"] != "NONE":
        raise RuntimeError(f"WAV 必须是 PCM/NONE，当前 compression={info['compression']}")
    return info


@contextmanager
def timed_step(name: str, timings: dict[str, float]) -> Iterator[None]:
    """记录一个测试阶段耗时，同时把结果写入 summary。"""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        timings[name] = elapsed
        print(f"[TIME] {name}={elapsed:.3f}s")


def post_json_or_raise(response: requests.Response, *, op: str) -> dict[str, Any]:
    """解析 JSON 响应并在非 2xx 时抛出清晰错误。"""
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text[:500]}
    if not 200 <= response.status_code < 300:
        raise RuntimeError(f"{op} HTTP {response.status_code}: {json.dumps(data, ensure_ascii=False)}")
    if isinstance(data, dict):
        return data
    raise RuntimeError(f"{op} 响应不是 JSON object: {data!r}")


def check_service(base_url: str, *, timeout: float, require_ready: bool) -> dict[str, Any]:
    """检查服务器健康状态，适合部署后先确认环境变量和外部依赖配置。"""
    result: dict[str, Any] = {}
    for path in ("/healthz", "/readyz"):
        response = requests.get(f"{base_url}{path}", timeout=timeout)
        data = post_json_or_raise(response, op=path)
        result[path.lstrip("/")] = data
        print(f"[CHECK] {path} {json.dumps(data, ensure_ascii=False)}")
        if require_ready and not data.get("ok"):
            raise RuntimeError(f"{path} 返回 not ready: {json.dumps(data, ensure_ascii=False)}")
    return result


def upload_camera(base_url: str, image_path: Path, *, device: str, timeout: float) -> dict[str, Any]:
    """按真实客户端方式上传 JPEG 图片。"""
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    body = image_path.read_bytes()
    response = requests.post(
        f"{base_url}/camera/upload",
        params={"device": device},
        data=body,
        headers={"Content-Type": "image/jpeg"},
        timeout=timeout,
    )
    data = post_json_or_raise(response, op="camera_upload")
    print(
        "[CAMERA] "
        f"ok={data.get('ok')} analysis_ok={data.get('analysis_ok')} "
        f"mode={data.get('mode')} image_id={data.get('image_id')} "
        f"match={data.get('match_name')}"
    )
    return data


def start_ai_session(base_url: str, *, device: str, timeout: float) -> tuple[str, int]:
    """创建 AI 会话。"""
    response = requests.post(f"{base_url}/ai/start", json={"device": device}, timeout=timeout)
    data = post_json_or_raise(response, op="ai_start")
    session = str(data.get("session") or "")
    if not session:
        raise RuntimeError(f"ai_start 缺少 session: {data}")
    chunk_size = int(data.get("chunk_size") or DEFAULT_CHUNK_SIZE)
    print(f"[AI] start session={session} server_chunk_size={chunk_size}")
    return session, chunk_size


def upload_wav(
    base_url: str,
    session: str,
    wav_path: Path,
    *,
    chunk_size: int,
    timeout: float,
) -> dict[str, Any]:
    """分片上传 WAV，模拟 ESP 客户端上传流程。"""
    if not wav_path.exists():
        raise FileNotFoundError(wav_path)
    info = validate_esp_wav(wav_path)
    body = wav_path.read_bytes()
    print(
        "[WAV] "
        f"path={wav_path} bytes={len(body)} "
        f"rate={info['sample_rate']} channels={info['channels']} duration={info['duration']:.3f}s"
    )
    total = len(body)
    index = 0
    chunks = 0
    for offset in range(0, total, chunk_size):
        chunk = body[offset : offset + chunk_size]
        response = requests.post(
            f"{base_url}/ai/upload",
            params={
                "session": session,
                "index": index,
                "offset": offset,
                "total": total,
            },
            data=chunk,
            timeout=timeout,
        )
        post_json_or_raise(response, op=f"ai_upload[{index}]")
        print(f"[AI] upload index={index} offset={offset} len={len(chunk)}")
        index += 1
        chunks += 1
    return {"bytes": total, "chunks": chunks, "wav_info": info}


def finish_ai(base_url: str, session: str, *, timeout: float) -> dict[str, Any]:
    """结束上传，触发后端完整 AI 链路。"""
    response = requests.post(f"{base_url}/ai/finish", params={"session": session}, json={}, timeout=timeout)
    data = post_json_or_raise(response, op="ai_finish")
    print(f"[AI] finish status={data.get('status')} ok={data.get('ok')}")
    return data


def poll_result_info(
    base_url: str,
    session: str,
    *,
    timeout_seconds: float,
    interval_seconds: float,
    request_timeout: float,
) -> dict[str, Any]:
    """轮询 result_info，直到回复音频就绪或超时。"""
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = requests.post(
            f"{base_url}/ai/result_info",
            params={"session": session},
            json={},
            timeout=request_timeout,
        )
        data = post_json_or_raise(response, op="ai_result_info")
        last = data
        print(
            "[AI] poll "
            f"status={data.get('status')} ready={data.get('ready')} "
            f"tts={data.get('tts_status')} total={data.get('total')} "
            f"asr={data.get('asr_text')!r}"
        )
        if data.get("ready") or data.get("reply_wav_ready") or data.get("audio_ready"):
            return data
        if str(data.get("status") or "").endswith("failed") or data.get("tts_status") == "failed":
            raise RuntimeError(f"后端处理失败: {json.dumps(data, ensure_ascii=False)}")
        time.sleep(interval_seconds)
    raise TimeoutError(f"等待 AI 结果超时，最后状态: {json.dumps(last, ensure_ascii=False)}")


def download_reply(
    base_url: str,
    session: str,
    total: int,
    output_path: Path,
    *,
    chunk_size: int,
    timeout: float,
) -> dict[str, Any]:
    """下载后端生成的回复 WAV。"""
    if total <= 0:
        raise RuntimeError(f"reply total 无效: {total}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as fh:
        offset = 0
        chunks = 0
        while offset < total:
            response = requests.post(
                f"{base_url}/ai/result_chunk",
                params={"session": session, "offset": offset, "len": min(chunk_size, total - offset)},
                json={},
                timeout=timeout,
            )
            if response.status_code != 200:
                raise RuntimeError(f"ai_result_chunk offset={offset} HTTP {response.status_code}: {response.text[:500]}")
            chunk = response.content
            if not chunk:
                raise RuntimeError(f"ai_result_chunk offset={offset} 返回空数据")
            fh.write(chunk)
            print(f"[AI] download offset={offset} len={len(chunk)}")
            offset += len(chunk)
            chunks += 1
    wav_info = validate_esp_wav(output_path)
    size = output_path.stat().st_size
    print(f"[OK] reply_wav={output_path} bytes={size}")
    return {"bytes": size, "chunks": chunks, "wav_info": wav_info}


def main() -> int:
    """运行 HTTP 客户端端到端测试。"""
    parser = argparse.ArgumentParser(description="模拟 ESP 客户端验收已部署后端 HTTP AI 链路")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080", help="后端 HTTP 基础地址")
    parser.add_argument("--device", default="walkie-01", help="设备 ID")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE.relative_to(PROJECT_ROOT)), help="要上传的 JPEG 图片")
    parser.add_argument("--wav", default=str(DEFAULT_WAV.relative_to(PROJECT_ROOT)), help="要上传的 16k 单声道 WAV")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR.relative_to(PROJECT_ROOT)), help="输出目录")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE, help="上传/下载分片大小")
    parser.add_argument("--poll-timeout", type=float, default=120.0, help="等待回复音频超时时间")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="轮询间隔")
    parser.add_argument("--request-timeout", type=float, default=30.0, help="普通 HTTP 请求超时")
    parser.add_argument(
        "--finish-timeout",
        type=float,
        default=180.0,
        help="/ai/finish 会调用 ASR 和大模型，单独给更长超时",
    )
    parser.add_argument("--no-health-check", action="store_true", help="跳过 /healthz 和 /readyz 预检查")
    parser.add_argument(
        "--allow-not-ready",
        action="store_true",
        help="/readyz 返回 ok=false 时仍继续测试，用于排查部署问题",
    )
    parser.add_argument("--skip-camera", action="store_true", help="跳过图片上传，只测试语音问答")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    image_path = resolve_path(args.image)
    wav_path = resolve_path(args.wav)
    out_dir = resolve_path(args.out_dir)
    reply_path = out_dir / "reply.wav"
    summary_path = out_dir / "summary.json"
    timings: dict[str, float] = {}
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    health_data: dict[str, Any] | None = None
    camera_data: dict[str, Any] | None = None
    upload_data: dict[str, Any] | None = None
    finish_data: dict[str, Any] | None = None
    download_data: dict[str, Any] | None = None

    if not args.no_health_check:
        with timed_step("health_check", timings):
            health_data = check_service(
                base_url,
                timeout=args.request_timeout,
                require_ready=not args.allow_not_ready,
            )

    if not args.skip_camera:
        with timed_step("camera_upload", timings):
            camera_data = upload_camera(base_url, image_path, device=args.device, timeout=args.request_timeout)

    with timed_step("ai_start", timings):
        session, server_chunk_size = start_ai_session(base_url, device=args.device, timeout=args.request_timeout)
    chunk_size = min(args.chunk_size, server_chunk_size) if server_chunk_size > 0 else args.chunk_size
    with timed_step("wav_upload", timings):
        upload_data = upload_wav(base_url, session, wav_path, chunk_size=chunk_size, timeout=args.request_timeout)
    with timed_step("ai_finish", timings):
        finish_data = finish_ai(base_url, session, timeout=args.finish_timeout)
    with timed_step("poll_result", timings):
        result_info = poll_result_info(
            base_url,
            session,
            timeout_seconds=args.poll_timeout,
            interval_seconds=args.poll_interval,
            request_timeout=args.request_timeout,
        )
    total = int(result_info.get("total") or result_info.get("reply_wav_size") or 0)
    with timed_step("download_reply", timings):
        download_data = download_reply(base_url, session, total, reply_path, chunk_size=chunk_size, timeout=args.request_timeout)

    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base_url": base_url,
        "device": args.device,
        "image": str(image_path),
        "wav": str(wav_path),
        "session": session,
        "timings": timings,
        "health": health_data,
        "camera": camera_data,
        "upload": upload_data,
        "finish": finish_data,
        "result_info": result_info,
        "download": download_data,
        "reply_wav": str(reply_path),
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] summary={summary_path}")
    print(f"[OK] total_time={sum(timings.values()):.3f}s base_url={base_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())




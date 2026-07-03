#!/usr/bin/env python3
"""WTK1 设备客户端测试脚本。

模拟 ESP32 设备端完整业务流程，测试后端所有 HTTP 接口。

测试场景：
1. 健康检查       → GET  /healthz, /readyz
2. 相机上传       → POST /camera/upload
3. AI 语音问答    → POST /ai/start → /ai/upload → /ai/finish → /ai/result_info → /ai/result_chunk
4. 取消会话       → POST /ai/cancel
5. 最新图片分析   → POST /camera/analyze_latest
6. WAV 回显      → POST /ai/wav

用法：
    # 自动启动服务器 + 运行全部测试（推荐）
    python tools/check_device_client.py --base-url http://127.0.0.1:18080 --server

    # 仅启动服务器，不运行测试（手动调试）
    python tools/check_device_client.py --base-url http://127.0.0.1:18080 --server-only

    # 连接已有服务器运行测试
    python tools/check_device_client.py --base-url http://127.0.0.1:18080

    # 仅语音测试
    python tools/check_device_client.py --base-url http://127.0.0.1:18080 --skip-camera

    # 交互式选择测试场景
    python tools/check_device_client.py --base-url http://127.0.0.1:18080 --interactive

    # 压力测试（多次语音问答）
    python tools/check_device_client.py --base-url http://127.0.0.1:18080 --stress 5
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = PROJECT_ROOT / "tests" / "data" / "camera" / "yingguo_yuying.jpg"
DEFAULT_WAV = PROJECT_ROOT / "tests" / "data" / "audio" / "what.wav"
DEFAULT_OUT_DIR = PROJECT_ROOT / "tmp" / "debug" / "device_client"
DEFAULT_CHUNK_SIZE = 32768

# -----------------------------------------------------------------------------
# 数据结构
# -----------------------------------------------------------------------------

class TestResult(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass
class ScenarioResult:
    name: str
    result: TestResult
    elapsed: float
    detail: str = ""
    data: dict = field(default_factory=dict)

    def __str__(self) -> str:
        icon = {"PASS": "✓", "FAIL": "✗", "SKIP": "○"}.get(self.result.value, "?")
        return f"[{self.result.value}] {self.name} ({self.elapsed:.2f}s) {self.detail}"


@dataclass
class TestReport:
    scenarios: list[ScenarioResult] = field(default_factory=list)
    started_at: str = ""
    finished_at: str = ""

    @property
    def passed(self) -> int:
        return sum(1 for s in self.scenarios if s.result == TestResult.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.scenarios if s.result == TestResult.FAIL)

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.scenarios if s.result == TestResult.SKIP)

    def add(self, scenario: ScenarioResult) -> None:
        self.scenarios.append(scenario)

    def summary(self) -> str:
        total = len(self.scenarios)
        return (
            f"\n{'='*60}\n"
            f"测试报告: {self.passed}/{total} 通过, {self.failed}/{total} 失败, {self.skipped}/{total} 跳过\n"
            f"{'='*60}"
        )

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": len(self.scenarios),
            "scenarios": [
                {
                    "name": s.name,
                    "result": s.result.value,
                    "elapsed": s.elapsed,
                    "detail": s.detail,
                    "data": s.data,
                }
                for s in self.scenarios
            ],
        }


# -----------------------------------------------------------------------------
# 工具函数
# -----------------------------------------------------------------------------

def is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """检测端口是否已开放。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False
    finally:
        sock.close()


def wait_for_server(host: str, port: int, timeout: float = 30.0) -> bool:
    """等待服务器就绪，超时返回 False。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_open(host, port):
            return True
        time.sleep(0.5)
    return False


def start_server_subprocess(port: int) -> subprocess.Popen | None:
    """启动 uvicorn 后端服务作为子进程，返回进程对象。"""
    host = "0.0.0.0"
    cmd = [
        sys.executable, "-m", "uvicorn",
        "main:app",
        "--host", host,
        "--port", str(port),
        "--log-level", "warning",
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=PROJECT_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        print(f"[SERVER] 启动中 PID={proc.pid} {' '.join(cmd)}")
        return proc
    except Exception as e:
        print(f"[SERVER] 启动失败: {e}")
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="WTK1 设备客户端测试")
    parser.add_argument("--base-url", default="http://127.0.0.1:18080", help="后端地址")
    parser.add_argument("--device", default="device-test-01", help="设备 ID")
    parser.add_argument("--image", default=str(DEFAULT_IMAGE.relative_to(PROJECT_ROOT)), help="测试图片路径")
    parser.add_argument("--wav", default=str(DEFAULT_WAV.relative_to(PROJECT_ROOT)), help="测试音频路径")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR.relative_to(PROJECT_ROOT)), help="输出目录")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP 请求超时(秒)")
    parser.add_argument("--finish-timeout", type=float, default=180.0, help="/ai/finish 超时(秒)")
    parser.add_argument("--poll-timeout", type=float, default=120.0, help="轮询结果超时(秒)")
    parser.add_argument("--poll-interval", type=float, default=1.0, help="轮询间隔(秒)")
    parser.add_argument("--skip-camera", action="store_true", help="跳过相机测试")
    parser.add_argument("--skip-voice", action="store_true", help="跳过语音测试")
    parser.add_argument("--interactive", action="store_true", help="交互式选择测试场景")
    parser.add_argument("--stress", type=int, default=0, help="压力测试次数(>0时运行多次语音问答)")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细输出")
    parser.add_argument(
        "--server",
        action="store_true",
        help="自动启动后端服务器（测试结束后自动关闭）",
    )
    parser.add_argument(
        "--server-only",
        action="store_true",
        help="仅启动服务器，不运行测试",
    )
    return parser.parse_args()


def resolve_path(path_text: str) -> Path:
    p = Path(path_text)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def validate_wav(path: Path) -> dict[str, Any]:
    """校验 WAV 格式是否符合 ESP32 客户端规范。"""
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


def http_get(base_url: str, path: str, timeout: float, verbose: bool = False) -> requests.Response:
    url = f"{base_url}{path}"
    if verbose:
        print(f"  GET  {url}")
    resp = requests.get(url, timeout=timeout)
    if verbose:
        print(f"  <- HTTP {resp.status_code} {resp.text[:200]}")
    return resp


def http_post(
    base_url: str,
    path: str,
    params: dict | None = None,
    data: bytes | dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
    verbose: bool = False,
) -> requests.Response:
    url = f"{base_url}{path}"
    if verbose:
        print(f"  POST {url} params={params}")
    resp = requests.post(url, params=params, data=data, headers=headers, timeout=timeout)
    if verbose:
        print(f"  <- HTTP {resp.status_code} {resp.text[:200]}")
    return resp


def json_response(resp: requests.Response, op: str) -> dict[str, Any]:
    """将响应解析为 JSON，非 2xx 时抛出异常。"""
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text[:500]}
    if not 200 <= resp.status_code < 300:
        raise RuntimeError(f"{op} HTTP {resp.status_code}: {json.dumps(data, ensure_ascii=False)}")
    return data


# -----------------------------------------------------------------------------
# 测试场景
# -----------------------------------------------------------------------------

def scenario_health_check(base_url: str, timeout: float, verbose: bool) -> ScenarioResult:
    """场景1: 健康检查 /healthz 和 /readyz"""
    start = time.perf_counter()
    try:
        # /healthz
        resp = http_get(base_url, "/healthz", timeout, verbose)
        health = json_response(resp, "healthz")
        if not health.get("ok"):
            return ScenarioResult("健康检查-healthz", TestResult.FAIL, time.perf_counter() - start, "healthz 返回 ok=false")

        # /readyz
        resp = http_get(base_url, "/readyz", timeout, verbose)
        ready = json_response(resp, "readyz")
        if not ready.get("ok"):
            return ScenarioResult("健康检查-readyz", TestResult.FAIL, time.perf_counter() - start, "readyz 返回 ok=false")

        detail = (
            f"qa_cfg={ready.get('bailian_qa_configured')} "
            f"vision={ready.get('vision_provider')} "
            f"profiles={ready.get('vision_profiles')} "
            f"cards={ready.get('exhibit_cards')} "
            f"sessions={ready.get('sessions')}"
        )
        return ScenarioResult("健康检查", TestResult.PASS, time.perf_counter() - start, detail, {"health": health, "ready": ready})
    except Exception as e:
        return ScenarioResult("健康检查", TestResult.FAIL, time.perf_counter() - start, str(e))


def scenario_camera_upload(
    base_url: str,
    device: str,
    image_path: Path,
    timeout: float,
    verbose: bool,
) -> ScenarioResult:
    """场景2: 相机 JPEG 上传 /camera/upload"""
    start = time.perf_counter()
    try:
        if not image_path.exists():
            return ScenarioResult("相机上传", TestResult.SKIP, time.perf_counter() - start, f"图片不存在: {image_path}")

        body = image_path.read_bytes()
        resp = http_post(
            base_url, "/camera/upload",
            params={"device": device},
            data=body,
            headers={"Content-Type": "image/jpeg"},
            timeout=timeout,
            verbose=verbose,
        )
        data = json_response(resp, "camera_upload")
        detail = (
            f"ok={data.get('ok')} category={data.get('category')} "
            f"is_clear={data.get('is_clear')} need_retake={data.get('need_retake')} "
            f"desc={data.get('visual_description', '')[:40].strip()}..."
        )
        result = TestResult.PASS if data.get("ok") else TestResult.FAIL
        return ScenarioResult("相机上传", result, time.perf_counter() - start, detail, data)
    except Exception as e:
        return ScenarioResult("相机上传", TestResult.FAIL, time.perf_counter() - start, str(e))


def scenario_ai_start(base_url: str, device: str, timeout: float, verbose: bool) -> tuple[ScenarioResult, str | None, int]:
    """场景3a: 创建 AI 会话 /ai/start"""
    start = time.perf_counter()
    try:
        resp = http_post(
            base_url, "/ai/start",
            data=json.dumps({"device": device}).encode(),
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            verbose=verbose,
        )
        data = json_response(resp, "ai_start")
        session = str(data.get("session") or "")
        chunk_size = int(data.get("chunk_size") or DEFAULT_CHUNK_SIZE)
        if not session:
            return ScenarioResult("AI会话创建", TestResult.FAIL, time.perf_counter() - start, "缺少 session"), None, 0
        return ScenarioResult("AI会话创建", TestResult.PASS, time.perf_counter() - start, f"session={session}"), session, chunk_size
    except Exception as e:
        return ScenarioResult("AI会话创建", TestResult.FAIL, time.perf_counter() - start, str(e)), None, 0


def scenario_ai_upload(
    base_url: str,
    session: str,
    wav_path: Path,
    chunk_size: int,
    timeout: float,
    verbose: bool,
) -> ScenarioResult:
    """场景3b: 分片上传 WAV /ai/upload"""
    start = time.perf_counter()
    try:
        if not wav_path.exists():
            return ScenarioResult("音频上传", TestResult.SKIP, time.perf_counter() - start, f"音频不存在: {wav_path}")

        info = validate_wav(wav_path)
        body = wav_path.read_bytes()
        total = len(body)
        uploaded = 0
        index = 0

        for offset in range(0, total, chunk_size):
            chunk = body[offset : offset + chunk_size]
            resp = http_post(
                base_url, "/ai/upload",
                params={"session": session, "index": index, "offset": offset, "total": total},
                data=chunk,
                timeout=timeout,
                verbose=verbose,
            )
            data = json_response(resp, f"ai_upload[{index}]")
            if not data.get("ok"):
                return ScenarioResult("音频上传", TestResult.FAIL, time.perf_counter() - start, f"块 {index} 返回 ok=false")
            uploaded += len(chunk)
            index += 1

        detail = f"chunks={index} bytes={uploaded} duration={info['duration']:.2f}s"
        return ScenarioResult("音频上传", TestResult.PASS, time.perf_counter() - start, detail, {"bytes": uploaded, "chunks": index})
    except Exception as e:
        return ScenarioResult("音频上传", TestResult.FAIL, time.perf_counter() - start, str(e))


def scenario_ai_finish(
    base_url: str,
    session: str,
    timeout: float,
    verbose: bool,
) -> ScenarioResult:
    """场景3c: 结束上传触发 ASR→LLM→TTS /ai/finish"""
    start = time.perf_counter()
    try:
        resp = http_post(
            base_url, "/ai/finish",
            params={"session": session},
            data=b"{}",
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            verbose=verbose,
        )
        data = json_response(resp, "ai_finish")
        detail = f"ok={data.get('ok')} status={data.get('status')}"
        result = TestResult.PASS if data.get("ok") else TestResult.FAIL
        return ScenarioResult("AI处理触发", result, time.perf_counter() - start, detail, data)
    except Exception as e:
        return ScenarioResult("AI处理触发", TestResult.FAIL, time.perf_counter() - start, str(e))


def scenario_ai_poll_result(
    base_url: str,
    session: str,
    timeout_seconds: float,
    interval: float,
    request_timeout: float,
    verbose: bool,
) -> tuple[ScenarioResult, dict[str, Any]]:
    """场景3d: 轮询 /ai/result_info 直到音频就绪"""
    start = time.perf_counter()
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}

    while time.monotonic() < deadline:
        try:
            resp = http_post(
                base_url, "/ai/result_info",
                params={"session": session},
                data=b"{}",
                headers={"Content-Type": "application/json"},
                timeout=request_timeout,
                verbose=verbose,
            )
            data = json_response(resp, "result_info")
            last = data

            if verbose:
                print(f"  poll: status={data.get('status')} ready={data.get('ready')} tts={data.get('tts_status')}")

            if data.get("ready") or data.get("reply_wav_ready") or data.get("audio_ready"):
                detail = f"ready={data.get('ready')} total={data.get('total')} tts={data.get('tts_status')}"
                return ScenarioResult("结果轮询", TestResult.PASS, time.perf_counter() - start, detail, data), data

            if str(data.get("status") or "").endswith("failed") or data.get("tts_status") == "failed":
                return ScenarioResult("结果轮询", TestResult.FAIL, time.perf_counter() - start, f"后端失败: {data.get('tts_error')}", data), data

        except requests.exceptions.Timeout:
            pass

        time.sleep(interval)

    return ScenarioResult("结果轮询", TestResult.FAIL, time.perf_counter() - start, f"超时，最后状态: {last.get('status')}", last), last


def scenario_ai_download_reply(
    base_url: str,
    session: str,
    total: int,
    output_path: Path,
    chunk_size: int,
    timeout: float,
    verbose: bool,
) -> ScenarioResult:
    """场景3e: 分块下载回复 WAV /ai/result_chunk"""
    start = time.perf_counter()
    try:
        if total <= 0:
            return ScenarioResult("音频下载", TestResult.SKIP, time.perf_counter() - start, "total=0 跳过")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as fh:
            offset = 0
            chunks = 0
            while offset < total:
                resp = http_post(
                    base_url, "/ai/result_chunk",
                    params={"session": session, "offset": offset, "len": min(chunk_size, total - offset)},
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    timeout=timeout,
                    verbose=verbose,
                )
                if resp.status_code != 200:
                    return ScenarioResult("音频下载", TestResult.FAIL, time.perf_counter() - start, f"HTTP {resp.status_code}")
                chunk = resp.content
                if not chunk:
                    return ScenarioResult("音频下载", TestResult.FAIL, time.perf_counter() - start, f"offset={offset} 返回空数据")
                fh.write(chunk)
                offset += len(chunk)
                chunks += 1

        info = validate_wav(output_path)
        size = output_path.stat().st_size
        detail = f"bytes={size} chunks={chunks} duration={info['duration']:.2f}s"
        return ScenarioResult("音频下载", TestResult.PASS, time.perf_counter() - start, detail, {"bytes": size, "chunks": chunks})
    except Exception as e:
        return ScenarioResult("音频下载", TestResult.FAIL, time.perf_counter() - start, str(e))


def scenario_ai_cancel(
    base_url: str,
    session: str,
    timeout: float,
    verbose: bool,
) -> ScenarioResult:
    """场景4: 取消 AI 会话 /ai/cancel"""
    start = time.perf_counter()
    try:
        resp = http_post(
            base_url, "/ai/cancel",
            params={"session": session},
            data=b"{}",
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            verbose=verbose,
        )
        data = json_response(resp, "ai_cancel")
        detail = f"ok={data.get('ok')} status={data.get('status')}"
        result = TestResult.PASS if data.get("ok") else TestResult.FAIL
        return ScenarioResult("会话取消", result, time.perf_counter() - start, detail, data)
    except Exception as e:
        return ScenarioResult("会话取消", TestResult.FAIL, time.perf_counter() - start, str(e))


def scenario_camera_analyze_latest(
    base_url: str,
    device: str,
    timeout: float,
    verbose: bool,
) -> ScenarioResult:
    """场景5: 分析最新图片 /camera/analyze_latest"""
    start = time.perf_counter()
    try:
        resp = http_post(
            base_url, "/camera/analyze_latest",
            params={"device": device},
            data=b"{}",
            headers={"Content-Type": "application/json"},
            timeout=timeout,
            verbose=verbose,
        )
        if resp.status_code == 404:
            return ScenarioResult("最新图片分析", TestResult.SKIP, time.perf_counter() - start, "无最新图片")
        data = json_response(resp, "camera_analyze_latest")
        detail = f"ok={data.get('ok')} mode={data.get('mode')} answer_chars={len(data.get('answer_text', ''))}"
        result = TestResult.PASS if data.get("ok") else TestResult.FAIL
        return ScenarioResult("最新图片分析", result, time.perf_counter() - start, detail, data)
    except Exception as e:
        return ScenarioResult("最新图片分析", TestResult.FAIL, time.perf_counter() - start, str(e))


def scenario_wav_echo(
    base_url: str,
    wav_path: Path,
    timeout: float,
    verbose: bool,
) -> ScenarioResult:
    """场景6: WAV 回显测试 /ai/wav"""
    start = time.perf_counter()
    try:
        if not wav_path.exists():
            return ScenarioResult("WAV回显", TestResult.SKIP, time.perf_counter() - start, f"音频不存在: {wav_path}")

        body = wav_path.read_bytes()
        resp = http_post(
            base_url, "/ai/wav",
            data=body,
            headers={"Content-Type": "audio/wav"},
            timeout=timeout,
            verbose=verbose,
        )
        if resp.status_code != 200:
            return ScenarioResult("WAV回显", TestResult.FAIL, time.perf_counter() - start, f"HTTP {resp.status_code}")
        if len(resp.content) != len(body):
            return ScenarioResult("WAV回显", TestResult.FAIL, time.perf_counter() - start, f"长度不匹配: 发{len(body)} 收{len(resp.content)}")
        detail = f"bytes={len(body)}"
        return ScenarioResult("WAV回显", TestResult.PASS, time.perf_counter() - start, detail, {"bytes": len(body)})
    except Exception as e:
        return ScenarioResult("WAV回显", TestResult.FAIL, time.perf_counter() - start, str(e))


# -----------------------------------------------------------------------------
# 完整语音问答流程
# -----------------------------------------------------------------------------

def run_voice_qa_flow(
    report: TestReport,
    base_url: str,
    device: str,
    wav_path: Path,
    chunk_size: int,
    timeout: float,
    finish_timeout: float,
    poll_timeout: float,
    poll_interval: float,
    verbose: bool,
    round_num: int = 1,
) -> str | None:
    """运行一次完整的 AI 语音问答流程，返回 session_id 或 None"""
    prefix = f"[第{round_num}轮]" if round_num > 1 else ""

    # 3a. 创建会话
    s, session, server_chunk = scenario_ai_start(base_url, device, timeout, verbose)
    s.name = f"{prefix}AI会话创建"
    report.add(s)
    print(f"  {s}")
    if not session:
        return None
    chunk_size = min(chunk_size, server_chunk) if server_chunk > 0 else chunk_size

    # 3b. 上传音频
    s = scenario_ai_upload(base_url, session, wav_path, chunk_size, timeout, verbose)
    s.name = f"{prefix}音频上传"
    report.add(s)
    print(f"  {s}")
    if s.result == TestResult.FAIL:
        return session  # 仍返回 session，可尝试取消

    # 3c. 触发处理
    s = scenario_ai_finish(base_url, session, finish_timeout, verbose)
    s.name = f"{prefix}AI处理触发"
    report.add(s)
    print(f"  {s}")
    if s.result == TestResult.FAIL:
        return session

    # 3d. 轮询结果
    s, result_data = scenario_ai_poll_result(base_url, session, poll_timeout, poll_interval, timeout, verbose)
    s.name = f"{prefix}结果轮询"
    report.add(s)
    print(f"  {s}")

    # 3e. 下载回复音频
    total = int(result_data.get("total") or result_data.get("reply_wav_size") or 0)
    reply_path = resolve_path(str(DEFAULT_OUT_DIR)) / f"reply_{session}.wav"
    s = scenario_ai_download_reply(base_url, session, total, reply_path, chunk_size, timeout, verbose)
    s.name = f"{prefix}音频下载"
    report.add(s)
    print(f"  {s}")

    return session


# -----------------------------------------------------------------------------
# 交互式选择
# -----------------------------------------------------------------------------

def interactive_menu() -> dict[str, bool]:
    """显示交互式菜单，返回用户选择的测试场景。"""
    print("\n请选择要运行的测试场景（输入编号，多选用逗号分隔）：")
    print("  1. 健康检查       (/healthz, /readyz)")
    print("  2. 相机上传       (/camera/upload)")
    print("  3. AI 语音问答    (完整流程)")
    print("  4. 会话取消       (/ai/cancel)")
    print("  5. 最新图片分析   (/camera/analyze_latest)")
    print("  6. WAV 回显      (/ai/wav)")
    print("  0. 运行全部\n")

    choices = input("请输入选项 [默认 0]: ").strip() or "0"
    all_scenarios = {"1", "2", "3", "4", "5", "6"}
    if choices == "0":
        selected = all_scenarios
    else:
        selected = set(c.strip() for c in choices.split(",") if c.strip())

    return {
        "health_check": "1" in selected,
        "camera_upload": "2" in selected,
        "voice_qa": "3" in selected,
        "cancel": "4" in selected,
        "analyze_latest": "5" in selected,
        "wav_echo": "6" in selected,
    }


# -----------------------------------------------------------------------------
# 主函数
# -----------------------------------------------------------------------------

def main() -> int:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    device = args.device
    image_path = resolve_path(args.image)
    wav_path = resolve_path(args.wav)
    out_dir = resolve_path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 解析服务器地址
    server_host = args.base_url.split("://")[1].split(":")[0]
    server_port = int(args.base_url.split(":")[-1].strip("/"))

    # 管理服务器子进程
    server_proc: subprocess.Popen | None = None
    auto_server = False

    if args.server or args.server_only:
        if is_port_open(server_host, server_port):
            print(f"[SERVER] 端口 {server_port} 已开放，跳过启动")
        else:
            server_proc = start_server_subprocess(server_port)
            if server_proc is None:
                print("[SERVER] 无法启动服务器，退出")
                return 1
            auto_server = True
            if not wait_for_server(server_host, server_port, timeout=15.0):
                print("[SERVER] 服务器启动超时，退出")
                server_proc.terminate()
                return 1
            print(f"[SERVER] 服务已就绪 {args.base_url}")

    if args.server_only:
        print("[SERVER] 仅启动模式，运行中... 按 Ctrl+C 停止")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            if server_proc:
                server_proc.terminate()
                server_proc.wait()
        return 0

    report = TestReport()
    report.started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'='*60}")
    print(f"WTK1 设备客户端测试")
    print(f"后端地址: {base_url}")
    print(f"设备ID:   {device}")
    print(f"测试图片: {image_path}")
    print(f"测试音频: {wav_path}")
    print(f"输出目录: {out_dir}")
    print(f"{'='*60}\n")

    # 选择测试场景
    if args.interactive:
        selected = interactive_menu()
    else:
        selected = {
            "health_check": True,
            "camera_upload": not args.skip_camera,
            "voice_qa": not args.skip_voice,
            "cancel": not args.skip_voice,
            "analyze_latest": not args.skip_camera,
            "wav_echo": not args.skip_voice,
        }

    try:
        # 场景1: 健康检查
        if selected["health_check"]:
            s = scenario_health_check(base_url, args.timeout, args.verbose)
            report.add(s)
            print(s)

        # 场景2: 相机上传
        if selected["camera_upload"]:
            s = scenario_camera_upload(base_url, device, image_path, args.timeout, args.verbose)
            report.add(s)
            print(s)

        # 场景3: AI 语音问答（完整流程）
        if selected["voice_qa"]:
            stress_count = max(1, args.stress)
            for i in range(stress_count):
                if stress_count > 1:
                    print(f"\n--- 压力测试第 {i+1}/{stress_count} 轮 ---")
                session = run_voice_qa_flow(
                    report, base_url, device, wav_path,
                    DEFAULT_CHUNK_SIZE, args.timeout,
                    args.finish_timeout, args.poll_timeout, args.poll_interval,
                    args.verbose, round_num=i + 1,
                )
                if session is None:
                    break

        # 场景4: 取消会话（新建会话后立即取消）
        if selected["cancel"]:
            s, session, _ = scenario_ai_start(base_url, device, args.timeout, args.verbose)
            s.name = "取消-创建测试会话"
            report.add(s)
            print(s)
            if session:
                s = scenario_ai_cancel(base_url, session, args.timeout, args.verbose)
                s.name = "取消-执行取消"
                report.add(s)
                print(s)

        # 场景5: 最新图片分析
        if selected["analyze_latest"]:
            s = scenario_camera_analyze_latest(base_url, device, args.timeout, args.verbose)
            report.add(s)
            print(s)

        # 场景6: WAV 回显
        if selected["wav_echo"]:
            s = scenario_wav_echo(base_url, wav_path, args.timeout, args.verbose)
            report.add(s)
            print(s)
    except KeyboardInterrupt:
        print("\n[ABORT] 用户中断测试")
        report.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # 保存报告
    report.finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    summary_path = out_dir / "test_report.json"
    summary_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    print(report.summary())
    print(f"报告已保存: {summary_path}")

    # 自动启动的服务器：测试结束后关闭
    if auto_server and server_proc:
        print(f"[SERVER] 关闭服务器 PID={server_proc.pid}")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()

    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())




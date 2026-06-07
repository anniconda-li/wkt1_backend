from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any


FALLBACK_TEXT = "不好意思，导游服务响应超时，请再问一次。"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com"
DEFAULT_TIMEOUT = 30

logger = logging.getLogger(__name__)


class BailianAppService:
    def __init__(
        self,
        api_key: str | None = None,
        app_id: str | None = None,
        base_url: str | None = None,
        timeout: int | float | None = None,
    ):
        self.api_key = (api_key if api_key is not None else os.getenv("BAILIAN_API_KEY", "")).strip()
        self.app_id = (app_id if app_id is not None else os.getenv("BAILIAN_APP_ID", "")).strip()
        self.base_url = (
            base_url if base_url is not None else os.getenv("BAILIAN_APP_BASE_URL", DEFAULT_BASE_URL)
        ).rstrip("/")
        self.timeout = _read_timeout(timeout)

    def ask(self, prompt: str) -> str:
        return asyncio.run(self.ask_async(prompt))

    async def ask_async(self, prompt: str) -> str:
        total_start = time.perf_counter()
        request_log = {
            "prompt_len": len(prompt),
            "prompt_preview": _preview_text(prompt),
            "payload_keys": ["input", "parameters"],
            "timeout": self.timeout,
            "base_url": self.base_url,
            "app_id_masked": _mask_app_id(self.app_id),
            "has_HTTP_PROXY": bool(os.getenv("HTTP_PROXY")),
            "has_HTTPS_PROXY": bool(os.getenv("HTTPS_PROXY")),
        }
        logger.info("[BAILIAN] request %s", json.dumps(request_log, ensure_ascii=False))
        print(f"[BAILIAN] request {json.dumps(request_log, ensure_ascii=False)}", flush=True)
        if not self.api_key:
            logger.error("[BAILIAN] BAILIAN_API_KEY is not configured")
            failed_after = time.perf_counter() - total_start
            _log_bailian_result(failed_after, "", "missing_api_key", failed_after)
            print(f"[BAILIAN-TIME] failed_after={failed_after:.3f}s error=missing api key", flush=True)
            return FALLBACK_TEXT
        if not self.app_id:
            logger.error("[BAILIAN] BAILIAN_APP_ID is not configured")
            failed_after = time.perf_counter() - total_start
            _log_bailian_result(failed_after, "", "missing_app_id", failed_after)
            print(f"[BAILIAN-TIME] failed_after={failed_after:.3f}s error=missing app id", flush=True)
            return FALLBACK_TEXT

        build_start = time.perf_counter()
        url = f"{self.base_url}/api/v1/apps/{self.app_id}/completion"
        payload = {
            "input": {
                "prompt": prompt,
            },
            "parameters": {},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        print(
            f"[BAILIAN-TIME] build_payload={time.perf_counter() - build_start:.3f}s "
            "input_keys=['prompt']",
            flush=True,
        )
        print(f"[BAILIAN-TIME] http_start url={url}", flush=True)

        http_start = time.perf_counter()
        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except Exception as exc:
            logger.exception("[BAILIAN] request failed")
            failed_after = time.perf_counter() - total_start
            _log_bailian_result(failed_after, "", type(exc).__name__, failed_after)
            print(f"[BAILIAN-TIME] failed_after={failed_after:.3f}s error={exc}", flush=True)
            return FALLBACK_TEXT

        response_text = response.text or ""
        print(
            f"[BAILIAN-TIME] http_total={time.perf_counter() - http_start:.3f}s "
            f"status={response.status_code} response_chars={len(response_text)}",
            flush=True,
        )

        if response.status_code != 200:
            logger.error(
                "[BAILIAN] HTTP %s response_preview=%s",
                response.status_code,
                _preview_text(response_text),
            )
            print(
                f"[BAILIAN-TIME] failed_after={time.perf_counter() - total_start:.3f}s "
                f"error=HTTP {response.status_code}",
                flush=True,
            )
            failed_after = time.perf_counter() - total_start
            _log_bailian_result(failed_after, "", f"HTTP_{response.status_code}", failed_after)
            return FALLBACK_TEXT

        json_start = time.perf_counter()
        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            logger.exception("[BAILIAN] invalid JSON response_preview=%s", _preview_text(response_text))
            print(
                f"[BAILIAN-TIME] json_parse={time.perf_counter() - json_start:.3f}s error={exc}",
                flush=True,
            )
            failed_after = time.perf_counter() - total_start
            print(f"[BAILIAN-TIME] failed_after={failed_after:.3f}s error=invalid JSON", flush=True)
            _log_bailian_result(failed_after, "", "invalid_json", failed_after)
            return FALLBACK_TEXT
        print(f"[BAILIAN-TIME] json_parse={time.perf_counter() - json_start:.3f}s", flush=True)

        extract_start = time.perf_counter()
        answer = _extract_text(data)
        if not answer:
            logger.error("[BAILIAN] response missing output.text full_json=%s", json.dumps(data, ensure_ascii=False))
            print(
                f"[BAILIAN-TIME] extract_answer={time.perf_counter() - extract_start:.3f}s answer_chars=0",
                flush=True,
            )
            failed_after = time.perf_counter() - total_start
            print(f"[BAILIAN-TIME] failed_after={failed_after:.3f}s error=missing output.text", flush=True)
            _log_bailian_result(failed_after, "", "missing_output_text", failed_after)
            return FALLBACK_TEXT

        answer = answer.strip()
        print(
            f"[BAILIAN-TIME] extract_answer={time.perf_counter() - extract_start:.3f}s "
            f"answer_chars={len(answer)}",
            flush=True,
        )
        elapsed = time.perf_counter() - total_start
        print(f"[BAILIAN-TIME] total={elapsed:.3f}s", flush=True)
        _log_bailian_result(elapsed, answer, "", None)
        return answer


def _extract_text(data: dict[str, Any]) -> str:
    output = data.get("output")
    if isinstance(output, dict):
        text = output.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
    return ""


def _read_timeout(timeout: int | float | None) -> int | float:
    if timeout is not None:
        return timeout
    raw_value = os.getenv("BAILIAN_TIMEOUT", str(DEFAULT_TIMEOUT)).strip()
    try:
        value = float(raw_value)
    except ValueError:
        logger.error("[BAILIAN] invalid BAILIAN_TIMEOUT=%r; using %s", raw_value, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    if value <= 0:
        logger.error("[BAILIAN] invalid BAILIAN_TIMEOUT=%r; using %s", raw_value, DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    return value


def _preview_text(text: str, limit: int = 500) -> str:
    normalized = (text or "").replace("\r", "\\r").replace("\n", "\\n")
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit]}..."


def _mask_app_id(app_id: str) -> str:
    if not app_id:
        return ""
    if len(app_id) <= 8:
        return f"{app_id[:1]}***{app_id[-1:]}"
    return f"{app_id[:4]}***{app_id[-4:]}"


def _log_bailian_result(
    elapsed: float,
    answer: str,
    error_type: str,
    failed_after: float | None,
) -> None:
    payload = {
        "elapsed_ms": int(elapsed * 1000),
        "answer_preview": _preview_text(answer),
        "error_type": error_type,
        "failed_after": None if failed_after is None else int(failed_after * 1000),
    }
    logger.info("[BAILIAN] response %s", json.dumps(payload, ensure_ascii=False))
    print(f"[BAILIAN] response {json.dumps(payload, ensure_ascii=False)}", flush=True)

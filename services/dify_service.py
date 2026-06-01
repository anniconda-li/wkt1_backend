from __future__ import annotations

from typing import Any

import requests


class DifyService:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def run_workflow(
        self,
        question: str,
        image_context: str = "",
        device: str = "walkie-01",
        spot_id: str = "",
        timeout: int = 60,
    ) -> str:
        if not self.base_url:
            raise ValueError("DIFY_BASE_URL is not configured")
        if not self.api_key:
            raise ValueError("DIFY_API_KEY is not configured")

        url = f"{self.base_url}/workflows/run"
        payload = {
            "inputs": {
                "question": question,
                "image_context": image_context,
                "device": device,
                "spot_id": spot_id,
            },
            "response_mode": "blocking",
            "user": device,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        print(f"[DifyService] workflow start device={device} spot_id={spot_id}", flush=True)
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise RuntimeError(f"Dify workflow request failed: {exc}") from exc

        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"Dify workflow HTTP {response.status_code}: {response.text}"
            )

        try:
            data: dict[str, Any] = response.json()
        except ValueError as exc:
            raise RuntimeError(f"Dify workflow returned invalid JSON: {response.text}") from exc

        answer = (
            data.get("data", {})
            .get("outputs", {})
            .get("answer")
        )
        if not isinstance(answer, str) or not answer.strip():
            print(f"[DifyService] missing answer in response: {data}", flush=True)
            raise RuntimeError("Dify workflow response missing data.outputs.answer")

        print(f"[DifyService] workflow answer received chars={len(answer)}", flush=True)
        return answer.strip()

from __future__ import annotations

import time
from pathlib import Path

from services.asr_service import transcribe_wav
from services.bailian_app_service import BailianAppService
from services.tts_service import synthesize_wav_16k


FIXED_ANSWER = "你好，我是景区导游助手。当前语音回复链路已经打通。"


class TourOrchestrator:
    def __init__(self, bailian_app_service: BailianAppService | None = None):
        self.bailian_app_service = bailian_app_service

    def process_session(
        self,
        wav_path: str | Path,
        device: str = "walkie-01",
        spot_id: str = "dayanta",
        image_context: str = "",
        mode: str = "fixed",
    ) -> tuple[str, bytes]:
        _asr_text, answer_text = self.process_text_session(
            wav_path,
            device=device,
            spot_id=spot_id,
            image_context=image_context,
            mode=mode,
        )

        tts_start = time.perf_counter()
        try:
            reply_wav = synthesize_wav_16k(answer_text)
        except Exception as exc:
            print(f"[AI-TIME] tts={time.perf_counter() - tts_start:.3f}s error={exc}", flush=True)
            raise RuntimeError(f"TTS failed: {exc}") from exc
        print(f"[AI-TIME] tts={time.perf_counter() - tts_start:.3f}s reply_bytes={len(reply_wav)}", flush=True)
        return answer_text, reply_wav

    def process_text_session(
        self,
        wav_path: str | Path,
        device: str = "walkie-01",
        spot_id: str = "dayanta",
        image_context: str = "",
        mode: str = "fixed",
    ) -> tuple[str, str]:
        wav_path = Path(wav_path)
        print(
            f"[TourOrchestrator] process wav={wav_path} device={device} "
            f"spot_id={spot_id} mode={mode} llm_provider=bailian_app",
            flush=True,
        )

        if mode == "fixed":
            asr_text = ""
            answer_text = FIXED_ANSWER
        elif mode == "asr_bailian_app":
            asr_start = time.perf_counter()
            try:
                asr_text = transcribe_wav(wav_path)
            except Exception as exc:
                print(f"[AI-TIME] asr={time.perf_counter() - asr_start:.3f}s error={exc}", flush=True)
                raise RuntimeError(f"ASR failed: {exc}") from exc
            print(f"[AI] asr_text: {asr_text}", flush=True)
            print(f"[AI-TIME] asr={time.perf_counter() - asr_start:.3f}s text_chars={len(asr_text)}", flush=True)
            answer_text = self._ask_llm(
                asr_text,
                device=device,
                spot_id=spot_id,
                image_context=image_context,
            )
            print(f"[AI] answer_text chars: {len(answer_text)}", flush=True)
        else:
            raise ValueError(f"unsupported TOUR_MODE: {mode}")

        return asr_text, answer_text

    def _ask_llm(
        self,
        question: str,
        *,
        device: str,
        spot_id: str,
        image_context: str,
    ) -> str:
        if self.bailian_app_service is None:
            raise RuntimeError("Bailian app service is not configured")
        bailian_start = time.perf_counter()
        try:
            answer_text = self.bailian_app_service.ask(question)
        except Exception as exc:
            print(f"[AI-TIME] bailian_app={time.perf_counter() - bailian_start:.3f}s error={exc}", flush=True)
            raise RuntimeError(f"Bailian app failed: {exc}") from exc
        print(
            f"[AI-TIME] bailian_app={time.perf_counter() - bailian_start:.3f}s "
            f"answer_chars={len(answer_text)}",
            flush=True,
        )
        return answer_text

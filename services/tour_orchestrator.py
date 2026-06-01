from __future__ import annotations

from pathlib import Path

from services.dify_service import DifyService
from services.tts_service import synthesize_wav_16k


FIXED_ANSWER = "你好，我是景区导游助手。当前语音回复链路已经打通。"
FIXED_QUESTION = "这个塔有什么故事？"


class TourOrchestrator:
    def __init__(self, dify_service: DifyService | None):
        self.dify_service = dify_service

    def process_session(
        self,
        wav_path: str | Path,
        device: str = "walkie-01",
        spot_id: str = "dayanta",
        image_context: str = "",
        mode: str = "fixed",
    ) -> tuple[str, bytes]:
        wav_path = Path(wav_path)
        print(
            f"[TourOrchestrator] process wav={wav_path} device={device} "
            f"spot_id={spot_id} mode={mode}",
            flush=True,
        )

        if mode == "fixed":
            answer_text = FIXED_ANSWER
        elif mode == "dify_fixed_question":
            if self.dify_service is None:
                raise RuntimeError("Dify service is not configured")
            answer_text = self.dify_service.run_workflow(
                question=FIXED_QUESTION,
                image_context=image_context,
                device=device,
                spot_id=spot_id,
            )
        elif mode == "asr_dify":
            raise NotImplementedError("asr_dify mode is reserved for a later version")
        else:
            raise ValueError(f"unsupported TOUR_MODE: {mode}")

        reply_wav = synthesize_wav_16k(answer_text)
        return answer_text, reply_wav

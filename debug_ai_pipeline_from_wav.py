from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import core.config  # noqa: F401 - loads project .env
from services.asr_service import transcribe_wav
from services.bailian_app_service import BailianAppService, FALLBACK_TEXT
from services.tts_service import synthesize_wav_16k


DEFAULT_WAV_PATH = Path("tmp/received_wav/ai_upload_20260602_180749_401127.wav")
DEFAULT_OUTPUT_DIR = Path("tmp/debug_reply_wav")
DEFAULT_DEVICE = "debug-server"
DEFAULT_SPOT_ID = "dayanta"
DEFAULT_MODE = "debug_wav_bailian_app"

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Debug ASR -> LLM -> TTS pipeline from a local WAV file")
    parser.add_argument("--wav", default=str(DEFAULT_WAV_PATH), help="input WAV path")
    parser.add_argument("--device", default=DEFAULT_DEVICE, help="debug device name")
    parser.add_argument("--spot-id", default=DEFAULT_SPOT_ID, help="debug spot id")
    parser.add_argument("--mode", default=DEFAULT_MODE, help="debug mode label")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    args = parse_args()
    total_start = time.perf_counter()

    wav_path = Path(args.wav)
    print(f"[DEBUG] input_wav={wav_path}")
    print(f"[DEBUG] wav_exists={wav_path.exists()}")
    if not wav_path.exists():
        print(f"[ERROR] WAV file does not exist: {wav_path}", file=sys.stderr)
        return 2
    print(f"[DEBUG] wav_size={wav_path.stat().st_size}")
    print(f"[DEBUG] device={args.device} spot_id={args.spot_id} mode={args.mode}")

    asr_start = time.perf_counter()
    try:
        asr_text = transcribe_wav(wav_path)
    except Exception:
        logger.exception("ASR failed; stop before LLM")
        print(f"[DEBUG-TIME] asr={time.perf_counter() - asr_start:.3f}s error=asr_failed")
        print(f"[DEBUG-TIME] total={time.perf_counter() - total_start:.3f}s")
        return 1
    print(f"[DEBUG] asr_text={asr_text}")
    print(f"[DEBUG-TIME] asr={time.perf_counter() - asr_start:.3f}s")

    print("[DEBUG] llm_provider=bailian_app")

    llm_start = time.perf_counter()
    try:
        answer_text = BailianAppService().ask(asr_text)
    except Exception:
        logger.exception("LLM failed; use fallback text")
        answer_text = FALLBACK_TEXT
        print(f"[DEBUG] llm_fallback_text={answer_text}")
    if answer_text == FALLBACK_TEXT:
        print(f"[DEBUG] llm_fallback_text={answer_text}")
    print(f"[DEBUG] answer_text={answer_text}")
    print(f"[DEBUG-TIME] llm={time.perf_counter() - llm_start:.3f}s")

    tts_start = time.perf_counter()
    try:
        reply_wav = synthesize_wav_16k(answer_text)
    except Exception:
        logger.exception("TTS failed")
        print(f"[DEBUG-TIME] tts={time.perf_counter() - tts_start:.3f}s error=tts_failed")
        print(f"[DEBUG-TIME] total={time.perf_counter() - total_start:.3f}s")
        return 1
    print(f"[DEBUG-TIME] tts={time.perf_counter() - tts_start:.3f}s")

    DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = DEFAULT_OUTPUT_DIR / f"debug_reply_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav"
    output_path.write_bytes(reply_wav)
    print(f"[DEBUG] output_wav={output_path}")
    print(f"[DEBUG] output_size={output_path.stat().st_size}")
    print(f"[DEBUG-TIME] total={time.perf_counter() - total_start:.3f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

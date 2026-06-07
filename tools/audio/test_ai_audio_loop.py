from __future__ import annotations

import argparse
import shutil
import sys
import wave
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.config  # noqa: F401 - loads project .env
from services.asr_service import transcribe_wav
from services.bailian_app_service import BailianAppService
from services.tts_service import synthesize_wav_16k


DEFAULT_QUESTION = "大雁塔有什么故事？"
MOCK_BAILIAN_ANSWER = "大雁塔是西安著名古迹，始建于唐代，最初用于保存玄奘从印度带回的佛经和佛像。"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "tmp" / "latest"


def validate_esp_wav(path: Path) -> dict[str, object]:
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
        raise RuntimeError(f"WAV channels must be 1, got {info['channels']}")
    if info["sample_width"] != 2:
        raise RuntimeError(f"WAV sample_width must be 2, got {info['sample_width']}")
    if info["sample_rate"] != 16000:
        raise RuntimeError(f"WAV sample_rate must be 16000, got {info['sample_rate']}")
    if info["compression"] != "NONE":
        raise RuntimeError(f"WAV compression must be NONE, got {info['compression']}")
    return info


def print_wav_info(label: str, path: Path) -> None:
    info = validate_esp_wav(path)
    print(
        f"[{label}] format: "
        f"channels={info['channels']} "
        f"sample_width={info['sample_width']} "
        f"sample_rate={info['sample_rate']} "
        f"frames={info['frames']} "
        f"duration={info['duration']:.3f}s "
        f"compression={info['compression']} ({info['compression_name']})"
    )


def get_bailian_answer(asr_text: str) -> str:
    return BailianAppService().ask(asr_text)


def prepare_output_dir(path_text: str) -> Path:
    output_dir = Path(path_text)
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    for child in output_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local question TTS -> ASR -> Bailian app -> reply TTS loop")
    parser.add_argument("--text", default=DEFAULT_QUESTION, help="input question text")
    parser.add_argument("--wav", default="", help="existing question WAV path; skips question TTS when provided")
    parser.add_argument("--answer", default="", help="manual answer text; skips Bailian when provided")
    parser.add_argument("--mock-bailian", action="store_true", help="use a local mock Bailian answer")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUTPUT_DIR.relative_to(PROJECT_ROOT)), help="output directory")
    args = parser.parse_args()

    output_dir = prepare_output_dir(args.out_dir)
    question_text_path = output_dir / "question.txt"
    asr_text_path = output_dir / "asr_text.txt"
    answer_text_path = output_dir / "answer.txt"
    question_wav_path = output_dir / "question.wav"
    reply_wav_path = output_dir / "reply.wav"

    question_text = args.text.strip() or DEFAULT_QUESTION
    question_text_path.write_text(question_text, encoding="utf-8")

    if args.wav:
        question_wav_path = Path(args.wav)
        if not question_wav_path.exists():
            raise FileNotFoundError(question_wav_path)
        validate_esp_wav(question_wav_path)
    else:
        question_wav_bytes = synthesize_wav_16k(question_text)
        question_wav_path.write_bytes(question_wav_bytes)
        validate_esp_wav(question_wav_path)

    print(f"[QUESTION] text: {question_text}")
    print(f"[QUESTION] wav: {question_wav_path}")
    print_wav_info("QUESTION", question_wav_path)

    asr_text = transcribe_wav(question_wav_path)
    asr_text_path.write_text(asr_text, encoding="utf-8")
    print(f"[ASR] text: {asr_text}")

    if args.answer.strip():
        answer_text = args.answer.strip()
    elif args.mock_bailian:
        answer_text = MOCK_BAILIAN_ANSWER
    else:
        answer_text = get_bailian_answer(asr_text)

    answer_text_path.write_text(answer_text, encoding="utf-8")
    print(f"[BAILIAN] answer: {answer_text}")

    reply_wav_bytes = synthesize_wav_16k(answer_text)
    reply_wav_path.write_bytes(reply_wav_bytes)
    validate_esp_wav(reply_wav_path)
    print(f"[TTS] reply wav: {reply_wav_path}")
    print_wav_info("TTS", reply_wav_path)
    print("[OK] ai audio loop passed")

if __name__ == "__main__":
    main()

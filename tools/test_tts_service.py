from __future__ import annotations

import sys
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.tts_service import synthesize_wav_16k


def main() -> None:
    output_path = ROOT / "tools" / "test_reply.wav"
    wav_bytes = synthesize_wav_16k("你好，我是景区导游助手。")
    output_path.write_bytes(wav_bytes)

    with wave.open(str(output_path), "rb") as wf:
        channels = wf.getnchannels()
        sample_rate = wf.getframerate()
        sample_width = wf.getsampwidth()

    assert channels == 1, channels
    assert sample_rate == 16000, sample_rate
    assert sample_width == 2, sample_width
    print(f"ok: {output_path} channels={channels} sample_rate={sample_rate} sample_width={sample_width}")


if __name__ == "__main__":
    main()

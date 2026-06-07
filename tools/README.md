# tools 目录说明

tools 目录当前只保留常用测试和维护脚本。

## audio

- `test_ai_audio_loop.py`

  当前主测试脚本。用于测试：
  问题文本 -> TTS -> ASR -> 百炼智能体应用 -> TTS -> reply.wav

  运行：

  ```powershell
  python tools\audio\test_ai_audio_loop.py --text "大雁塔有什么故事？"
  ```

- `test_asr_file.py`

  如果存在，用于测试本地 WAV 文件 ASR。

## maintenance

- `clean_tmp.py`

  用于清理 tmp 临时产物。

  ```powershell
  python tools\maintenance\clean_tmp.py
  ```

## 临时产物

`tmp/` 是运行时产物目录，可以随时清理。

最近一次测试结果在：

```text
tmp/latest/
```

最终回复音频在：

```text
tmp/latest/reply.wav
```

历史脚本已经移动到：

```text
archive/tools/
```

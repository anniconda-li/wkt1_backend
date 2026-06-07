# 当前项目状态

## 项目目标

基于 ESP32S3 的 AI 对讲导游设备。

ESP32S3：
- 录音
- 拍照
- 上传 WAV/JPG
- 拉取回复 WAV
- 播放语音

FastAPI 后端：
- ASR
- Vision
- 百炼智能体应用调用
- TTS
- WAV 格式转换

百炼智能体应用：
- RAG
- LLM 文本回答
- 不处理音频和设备协议

## 已完成

1. venv 已恢复并正常使用。
2. DashScope TTS 已跑通。
3. qwen3-tts-flash + Cherry 可以生成音频。
4. DashScope TTS 返回 24000Hz 音频。
5. ffmpeg 可以转换成 ESP32S3 需要的 WAV：16000Hz / 16-bit / mono / PCM / WAV。
6. TTS -> ffmpeg -> ASR 闭环已跑通。
7. 真实客户端上传 WAV -> ASR 已跑通。
8. 百炼智能体应用已接通。
9. 问题文本 -> TTS -> ASR -> 百炼智能体应用 -> TTS -> reply.wav 已跑通。

## 当前成功测试命令

```powershell
python tools\audio\test_ai_audio_loop.py --text "大雁塔有什么故事？"
```

不接百炼、使用 mock 回答：

```powershell
python tools\audio\test_ai_audio_loop.py --text "大雁塔有什么故事？" --mock-bailian
```

手动指定回答：

```powershell
python tools\audio\test_ai_audio_loop.py --text "大雁塔有什么故事？" --answer "大雁塔位于西安，始建于唐代，和玄奘法师保存佛经有关，是唐代长安的重要历史遗迹。"
```

## 样本文件

真实客户端上传 WAV 样本已保留在：

```text
samples/received_wav/ai_upload_20260531_124142_554048.wav
```

该样本此前 ASR 识别结果为：

```text
你好，你好。
```

## 目录约定

- `services/`：正式服务能力，包括 ASR、TTS、百炼智能体应用、Vision 占位。
- `tools/`：本地测试脚本，按 `audio/` 和 `maintenance/` 分类。
- `samples/received_wav/`：保留的真实客户端上传 WAV 样本。
- `samples/received_jpg/`：保留的真实客户端上传 JPG 样本。
- `tmp/`：运行时临时产物，不纳入 git。
- `docs/`：项目阶段记录和说明。

## 下一步计划

1. 将本地 AI 音频闭环接回 `/ai/finish` 的编排流程。
2. 接入摄像头 JPEG 最近图片上下文，补齐 Vision 服务。
3. 控制百炼回答长度，优化 ESP32S3 播放体验。
4. 用真实 ESP32S3 设备回归：录音 WAV -> ASR -> 百炼智能体应用 -> TTS -> 分片拉取播放。

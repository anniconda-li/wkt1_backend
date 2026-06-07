# WKT1 AI Guide Backend

ESP32S3 AI 对讲导游设备的本地后端测试项目。

设备侧负责录音、拍照、上传 WAV/JPG、拉取回复 WAV 并播放；后端负责 ASR、百炼智能体应用调用、TTS 和 WAV 格式转换。百炼智能体应用负责景区导游知识库 RAG 和文本回答，不处理音频和设备协议。

## 当前主测试

完整本地 AI 音频闭环：

```text
问题文本 -> TTS -> ASR -> 百炼智能体应用 -> TTS -> reply.wav
```

运行：

```powershell
python tools\audio\test_ai_audio_loop.py --text "大雁塔有什么故事？"
```

不接真实百炼，使用 mock 回答：

```powershell
python tools\audio\test_ai_audio_loop.py --text "大雁塔有什么故事？" --mock-bailian
```

生成结果在：

```text
tmp/latest/reply.wav
```

## 环境配置

复制 `.env.example` 为 `.env`，并填入真实 Key：

```powershell
copy .env.example .env
```

主要配置：

```text
DASHSCOPE_API_KEY=your_dashscope_api_key
TTS_PROVIDER=dashscope
TTS_MODEL=qwen3-tts-flash
TTS_VOICE=Cherry

ASR_PROVIDER=dashscope
ASR_MODEL=paraformer-realtime-v2

VISION_PROVIDER=dashscope
VISION_MODEL=qwen-vl-plus

AUTO_TTS_BACKGROUND=true
BAILIAN_API_KEY=your_bailian_api_key
BAILIAN_APP_ID=your_bailian_app_id
BAILIAN_APP_BASE_URL=https://dashscope.aliyuncs.com
BAILIAN_TIMEOUT=15
```

项目入口会通过 `core/config.py` 自动加载根目录 `.env`。

## ESP32S3 客户端交互逻辑

拍照上传现在是“保存图片 + 同步完成初步图像识别”的动作。客户端上传图片后，需要等待 `/camera/upload` 返回，再决定是否允许用户进入语音提问。

图片上传接口：

```text
POST /camera/upload?device=walkie-01
Content-Type: image/jpeg
Body: JPEG bytes
```

如果后端返回 `ok=true` 且 `analysis_ok=true`，表示图片已经完成初步图像分析，客户端可以提示用户开始提问：

```text
已完成图像分析，可以提问了。
```

典型返回：

```json
{
  "ok": true,
  "analysis_ok": true,
  "device": "walkie-01",
  "image_id": "camera_upload_...",
  "scene_type": "展柜展品",
  "object_category": "陶瓷",
  "mode": "category_guide",
  "need_retake": false
}
```

如果后端返回 `ok=true` 但 `analysis_ok=false`，表示这张照片信息不够，客户端不要进入语音提问流程，应提示用户重拍。提示语优先使用后端返回的 `answer_text`。

典型返回：

```json
{
  "ok": true,
  "analysis_ok": false,
  "mode": "retake_request",
  "need_retake": true,
  "answer_text": "这张照片信息不太够。请把展品放在画面中间，靠近一点，避开展柜反光后重拍。"
}
```

如果后端返回 `ok=false`，客户端按上传失败处理，可以提示网络或图片上传异常，并允许用户重试拍照。

客户端状态机建议：

```text
idle
  -> 用户拍照
  -> uploading_photo
  -> 等待 /camera/upload 返回

ok=false
  -> 提示上传失败
  -> 回到 idle

ok=true 且 analysis_ok=false
  -> 播放或显示 answer_text
  -> 要求用户重拍
  -> 回到 idle

ok=true 且 analysis_ok=true
  -> 设置 camera_ready=true
  -> 提示“已完成图像分析，可以提问了。”
  -> 等待用户按语音键提问
```

用户随后语音提问仍走原来的 AI 语音流程：

```text
POST /ai/start
POST /ai/upload
POST /ai/finish
POST /ai/result_info
POST /ai/result_chunk
```

后端会在 ASR 后判断用户是否在问刚才拍的图片，例如“这是什么”“讲讲这个展品”“刚才拍的是什么”。如果是图片相关问题，后端会使用 `/camera/upload` 时缓存的视觉识别结果回答，不会重复识别图片。如果是普通导游问题，则继续走原来的百炼语音问答。

客户端不需要调用 `/camera/analyze_latest`。该接口仅作为后端调试入口保留。

## 依赖

```powershell
.\.venv\Scripts\activate
pip install -r requirements.txt
```

还需要本机可运行 `ffmpeg`，用于把 TTS 音频转换为 ESP32S3 可播放格式：

```text
16000Hz / 16-bit / mono / PCM / WAV
```

## 目录结构

- `services/`：正式服务能力，包括 ASR、TTS、百炼智能体应用、Vision 占位。
- `tools/`：当前常用本地测试和维护脚本。
- `samples/received_wav/`：真实客户端上传 WAV 样本。
- `samples/received_jpg/`：真实客户端上传 JPG 样本。
- `tmp/`：运行时临时产物，可随时清理。
- `docs/`：项目阶段记录。
- `archive/`：已归档的历史测试脚本和旧工具。

## 常用命令

清理临时产物：

```powershell
python tools\maintenance\clean_tmp.py
```

单独测试百炼智能体应用：

```powershell
python test_bailian_app.py
```

查看当前阶段记录：

```text
docs/current_status.md
```

## 注意

- 不要提交 `.env` 或真实 API Key。
- `tmp/` 只存放运行时产物。
- 需要长期保留的音频/图片样本放到 `samples/`。

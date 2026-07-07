# WKT1 AI Guide Backend

ESP32S3 AI 对讲导游设备的本地 FastAPI 后端。

当前主线有三条：

```text
1. UDP 实时对讲：设备音频包接收和同设备回传
2. 语音问答：WAV -> ASR -> 百炼问答应用 -> TTS -> WAV
3. 图片讲解：JPG -> 视觉描述 -> 本地视觉匹配 -> 本地文物卡片 -> 百炼组织讲解 -> TTS
```

视觉识别只负责描述图片可见特征；具体文物判断在本地完成。百炼只保留一个问答应用，用来把本地资料组织成适合语音播报的回答。

## 目录

```text
core/       .env 加载和项目路径
server/     FastAPI、UDP、协议和媒体解析
services/   ASR、TTS、百炼、视觉描述、本地视觉匹配、讲解组织
photo/      文物基准照片
knowledge/  文物候选配置和视觉档案
tools/      构建、清理、手工检查脚本
tests/      自动化测试和测试数据
tmp/        运行时产物，可清理
```

## 环境

```powershell
copy .env.example .env
.\.venv\Scripts\activate
pip install -r requirements.txt
```

`.env` 里至少配置：

```text
DASHSCOPE_API_KEY=...
BAILIAN_API_KEY=...
BAILIAN_QA_APP_ID=...
VISION_PROFILES_PATH=knowledge/config/vision_profiles.json
EXHIBIT_KNOWLEDGE_PATH=knowledge/config/exhibit_cards.json
FFMPEG_BIN=ffmpeg
```

## 启动

只启动 HTTP：

```powershell
.\.venv\Scripts\python.exe -m uvicorn main:app --host 0.0.0.0 --port 18080
```

启动 HTTP + UDP：

```powershell
.\.venv\Scripts\python.exe -m server.walkie_app --host 0.0.0.0 --http-port 18080 --udp-port 19000
```

只启动 UDP 对讲：

```bash
./.venv/bin/python -c "from server.udp_server import run_udp; run_udp('0.0.0.0', 19000)"
```

对讲服务只转发 PCM 音频包。可在 `.env` 中设置音频热路径日志限频：

```text
INTERCOM_AUDIO_LOG_EVERY_N=50
```

设备上行和下行都使用 `APP_INTERCOM_PKT_AUDIO=4`、16 kHz mono s16le、20 ms、640B payload。服务器只转发给同频道其他设备，不回发给发送者本人。
`INTERCOM_AUDIO_LOG_EVERY_N` 用来限制音频热路径日志，默认每路每 50 帧约 1 秒打印一次；设置为 `0` 可关闭音频帧日志，避免日志 I/O 影响 UDP 转发节奏。

健康检查：

```text
GET /healthz
GET /readyz
```

## 图片讲解

基准数据准备：

```powershell
python tools\build_vision_profiles.py --overwrite
```

运行时流程：

```text
POST /camera/upload?device=walkie-01
-> 保存 JPEG
-> VisionService 生成 VisualDescription
-> VisualMatchService 读取 knowledge/config/vision_profiles.json 做本地匹配
-> 若仍是该设备最新上传，则一次性缓存 ready 图片上下文
-> 返回 {"ok": true, "status": "ready", "message": "ready", ...}
```

`/camera/upload` 不返回 processing/queued；设备端只需要等 HTTP 返回。若上传被新图片取代、取消、超时或照片不可用，会返回非 2xx 或 `ok:false`。可用 `POST /camera/cancel?device=walkie-01` 或 `POST /camera/upload/cancel?device=walkie-01` 让当前 pending 上传失效。

用户随后通过语音问“这是什么”“讲讲这个展品”时，后端只使用当前 latest upload 的 ready 缓存结果和本地文物卡片生成讲解，不重复识别图片，也不会使用半成品图片上下文。

## 语音问答

设备语音链路：

```text
POST /ai/start
POST /ai/upload
POST /ai/finish
POST /ai/result_info
POST /ai/result_chunk
POST /ai/cancel
```

普通问题走百炼问答应用。图片相关问题走最近一次 ready 的 `/camera/upload` 视觉描述和本地匹配结果。

## 工具

```powershell
python tools\build_vision_profiles.py --overwrite
python tools\check_artifact_pipeline.py --image tests\data\camera\yingguo_yuying.jpg --no-bailian
python tools\check_camera_guide.py --image tests\data\camera\yingguo_yuying.jpg --no-bailian
python tools\check_audio_loop.py --mock-bailian
python tools\check_bailian_app.py
python tools\check_http_client_e2e.py --base-url http://127.0.0.1:18080
python tools\check_device_client.py --base-url http://127.0.0.1:18080 --server
python tools\clean_tmp.py
```

`tools/check_*` 是人工验收工具；自动化测试放在 `tests/`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m compileall core server services tools tests
.\.venv\Scripts\python.exe tests\test_ai_cancel.py
```

## 数据约定

```text
photo/                                      基准照片
knowledge/config/museum_vision_candidates.json  文物候选、基础信息、讲解资料、基准图路径
knowledge/config/vision_profiles.json           视觉模型生成的基准视觉档案
knowledge/config/exhibit_cards.json             可选覆盖文件；不存在时从候选配置生成卡片
tests/data/camera/                              长期保留的测试图片
tmp/camera/received/                            设备上传原图
tmp/camera/preprocess/                          视觉预处理图
tmp/audio/                                      音频运行产物
tmp/debug/                                      调试输出
```

不要提交 `.env` 或真实 API Key。`tmp/` 是运行时目录，长期保留的数据放到 `photo/`、`knowledge/` 或 `tests/data/`。

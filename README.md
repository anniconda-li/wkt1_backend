# wkt-intercom-server

`wkt-intercom-server` 是 ESP32 WTK1 设备的 WebSocket 实时对讲转发服务。

项目身份约定：

| 项目项 | 名称 |
| --- | --- |
| 项目、仓库与本地目录 | `wkt-intercom-server` |
| Docker 镜像 | `wkt-intercom-server` |
| Docker 容器 | `wkt-intercom-server` |
| Compose 服务 | `intercom` |

本仓库保持独立 Git 仓库、独立 GitHub 仓库和独立部署单元。它不是
`wkt-platform` monorepo 的一部分；`wkt-platform` 仅作为本地父目录。ESP-IDF
固件项目 `walkie-talkiev1`、AI 服务和 OTA 服务均不属于本仓库。

这个项目现在只做一件事：

```text
walkie-01 <-> ws://server:18081/intercom/ws <-> walkie-02
```

AI/相机 HTTP 服务不在这个项目里改，继续使用 `18080`。对讲服务独立监听 `18081`。

## 目录

```text
server/protocol.py         WTK1 包解析和构造
server/intercom_server.py  WebSocket 对讲转发服务
tests/                     单元测试
```

## 协议

WebSocket 客户端和服务端都使用 binary frame。frame body 是完整 WTK1 包，不使用 JSON、base64 或额外封装。

```text
Byte 0-3:   "WTK1"
Byte 4:     packet type
Byte 5:     header length, 固定 34
Byte 6-7:   channel uint16 little-endian
Byte 8-11:  seq uint32 little-endian
Byte 12-15: timestamp uint32 little-endian ms
Byte 16-31: device name, 16 bytes, zero padded
Byte 32-33: payload length uint16 little-endian
Byte 34+:   payload
```

包类型：

```text
1 REGISTER
2 CHANNEL
3 PTT_START
4 AUDIO
5 PTT_STOP
6 HEARTBEAT
```

`AUDIO` payload 仍是 PCM s16le / 16kHz / mono，通常每包 20ms、640 bytes。

服务端行为：

- `REGISTER / CHANNEL / HEARTBEAT` 更新连接状态、频道和活跃时间，不转发。
- `PTT_START / AUDIO / PTT_STOP` 按包头 channel 转发给同频道其他在线设备。
- 其他 type 一律丢弃并记录 `unsupported_type`，本项目不实现 NACK、FEC、重传或纠错缓存。
- 转发时原样发送收到的 binary bytes，不重组、不改 seq、不改 timestamp、不改 payload。
- 同一个 device 重连时，新连接替换旧连接。
- 每个设备有独立发送队列和写超时；队列满时优先丢弃旧 `AUDIO`，尽量保留 `PTT_START / PTT_STOP`。
- 音频日志默认按 1 秒聚合，避免逐包打印影响实时性能。
- 队列里的旧音频超过实时窗口会被丢弃，不会无限补播历史声音。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env
```

## 启动

Ubuntu / Linux：

```bash
python -m server.intercom_server --host 0.0.0.0 --port 18081
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m server.intercom_server --host 0.0.0.0 --port 18081
```

也可以用主入口：

```bash
python main.py --host 0.0.0.0 --port 18081
```

## Docker

构建镜像：

```bash
docker build --tag wkt-intercom-server .
```

直接运行容器：

```bash
docker run --rm --name wkt-intercom-server --env-file .env -p 18081:18081 wkt-intercom-server
```

使用 Compose 启动服务：

```bash
docker compose up --build intercom
```

镜像名、容器名和 Compose 服务名分别固定为 `wkt-intercom-server`、
`wkt-intercom-server` 和 `intercom`。端口仍为 `18081`，WebSocket 路径仍为
`/intercom/ws`。

`.env` 默认配置：

```text
INTERCOM_HOST=0.0.0.0
INTERCOM_WS_PORT=18081
INTERCOM_LOG_STATS=1
INTERCOM_LOG_AUDIO_TRACE=0
INTERCOM_AUDIO_LOG_EVERY_N=50
INTERCOM_SEND_QUEUE_MAX=80
INTERCOM_SEND_TIMEOUT_SECONDS=2
INTERCOM_REALTIME_WINDOW_MS=400
INTERCOM_STATS_INTERVAL_MS=1000
```

设备连接：

```text
ws://<server_host>:18081/intercom/ws?device=walkie-01
ws://<server_host>:18081/intercom/ws?device=walkie-02
```

启动成功日志：

```text
intercom_service level=info event=listening url=ws://0.0.0.0:18081/intercom/ws?device=<device>
intercom_config level=info send_queue_max=80 send_timeout_s=2.0 log_stats=1 log_audio_trace=0 realtime_window_ms=400 stats_interval_ms=1000
intercom_conn level=info event=connect device=walkie-01 ip=1.2.3.4 port=12345 channel=1 active=1
intercom_conn level=info event=connect device=walkie-02 ip=1.2.3.5 port=12346 channel=1 active=2
```

日志示例：

```text
intercom_conn level=info event=register device=walkie-01 ch=1 seq=0 active=2
intercom_conn level=info event=channel device=walkie-01 old_ch=1 ch=2 seq=8
intercom_ptt level=info event=start device=walkie-01 ch=2 seq=9 targets=1 queued=1
intercom_rx level=info win=1s device=walkie-01 ch=2 audio=50 bytes=32000 gap=0 dup=0 p50_ms=20 p95_ms=28 max_ms=55 first_seq=10 last_seq=59
intercom_tx level=info win=1s target=walkie-02 from=walkie-01 ch=2 audio=50 sent=50 drop_old=0 drop_slow=0 q=0 q_max=4 oldest_ms=0 send_p95_ms=4 send_max_ms=18 target_ch=2
intercom_slow level=warn target=walkie-02 from=walkie-01 ch=2 q=30 oldest_ms=620 action=drop_old drop=12 keep_ms=400 first_seq=120
intercom_ptt level=info event=stop device=walkie-01 ch=2 seq=100 targets=1 queued=1
```

## 测试

```bash
python -m compileall server tests
python -m unittest discover -s tests -p "test*.py"
```

CI 会运行以上检查并构建 `wkt-intercom-server:ci` 镜像，但不会推送镜像或部署环境。

详细的容器运行与手工部署检查项见 [`docs/deployment.md`](docs/deployment.md)。不要提交
`.env`。公网服务器需要放行 TCP `18081`。

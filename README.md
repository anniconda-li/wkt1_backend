# WKT1 Intercom Backend

ESP32 WTK1 设备的 WebSocket 实时对讲转发服务。

这个仓库现在只保留对讲链路：

```text
ESP32 device A <-> WTK1 WebSocket binary <-> server <-> WTK1 WebSocket binary <-> ESP32 device B
```

已移除 AI 问答、ASR、TTS、相机上传、视觉识别、知识库和百炼应用相关代码。那些能力由其他项目承接。

## 目录

```text
server/     WTK1 协议解析和 WebSocket 对讲转发
tests/      对讲协议和队列单测
```

## 协议

保留设备端原有 WTK1 UDP 包格式：

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
7 NACK
8 AUDIO_FEC
```

`AUDIO` payload 保持设备端 PCM 原格式：PCM s16le / 16kHz / mono。服务端不改 packet type、header、seq、timestamp、device 字段或 payload 内容。

`AUDIO_FEC` 在 WebSocket 模式下会按频道原样转发；服务端不改正常 AUDIO 包格式和 seq。

`AUDIO_FEC` payload 为固定头加 XOR 数据：

```text
Byte 0-3:   base_seq uint32 little-endian，该组第一个 AUDIO seq
Byte 4:     count uint8，默认 4
Byte 5-6:   payload_len uint16 little-endian，当前 640
Byte 7:     reserved，固定 0
Byte 8+:    xor_payload[payload_len]
```

当一组 4 个 AUDIO 中恰好丢 1 个，设备端可用另外 3 个 AUDIO payload 与 `xor_payload` 再 XOR 恢复缺失的 PCM payload。

`NACK` 是机会型补洞请求，不改变正常音频转发路径，也不要求设备播放等待重传。payload 为固定 24 字节小端结构：

```text
Byte 0-15:  source_device[16]，缺的是哪个发送端设备的音频
Byte 16-17: channel uint16 little-endian
Byte 18-21: start_seq uint32 little-endian
Byte 22-23: count uint16 little-endian
```

`NACK` 在 WebSocket 模式下会按频道原样转发；服务端不会重新生成 seq，不会改 payload，不会重新编码 PCM。

## 安装

```bash
python -m venv .venv
cp .env.example .env
python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
py -m venv .venv
copy .env.example .env
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 启动

Ubuntu / Linux：

```bash
./.venv/bin/python -m server.udp_server --host 0.0.0.0 --udp-port 19000
```

Windows PowerShell：

```powershell
.\.venv\Scripts\python.exe -m server.udp_server --host 0.0.0.0 --udp-port 19000
```

也可以用主入口：

```bash
python main.py --host 0.0.0.0 --udp-port 19000
```

启动后会看到类似日志：

```text
websocket server listening ws://0.0.0.0:18081/intercom/ws?device=<device>
UDP WTK1 监听 0.0.0.0:19000
UDP downlink codec=pcm mode=paced transport=websocket ...
```

## WebSocket 全双工实验模式

默认使用 WebSocket 全双工。AI/相机 HTTP 服务继续使用 `18080`，对讲 WebSocket 独立使用 `18081`：

```text
INTERCOM_DOWNLINK_TRANSPORT=websocket
INTERCOM_WS_HOST=0.0.0.0
INTERCOM_WS_PORT=18081
INTERCOM_WS_QUEUE_MAX_AUDIO=50
INTERCOM_WS_QUEUE_HIGH_WATER_AUDIO=40
```

设备端通过同一个 WebSocket 长连接发送和接收完整 WTK1 binary packet：

```text
ws://<server-ip>:18081/intercom/ws?device=walkie-02
```

服务器用 query 参数里的 `device` 绑定连接；同一个 device 重连时，新连接覆盖旧连接并关闭旧连接。WebSocket 上行和下行 binary frame 的内容都是完整 WTK1 packet bytes：

```text
WTK1 34-byte header + payload
```

不包装 JSON，不 base64，不改变 PCM、WTK1 header、seq、timestamp、device 或 20ms/640B 音频包大小。

WebSocket 模式下，服务器会监听设备发来的 binary message，解析为 WTK1 后按包头 channel 转发给同频道其他在线设备。`REGISTER / CHANNEL / HEARTBEAT` 用于更新设备在线、频道和活跃时间；`PTT_START / AUDIO / PTT_STOP / NACK / AUDIO_FEC` 会按频道原样转发。固件当前只走 WebSocket，不再走 UDP fallback。

WebSocket 下行也有 per-device 队列和 20ms pacing；`INTERCOM_WS_QUEUE_MAX_AUDIO=50` 表示最多保留约 1 秒 audio。超过上限时丢最旧 AUDIO，保留最新 AUDIO。`PTT_STOP` 默认不会清理已经入队的 audio，而是排在当前语音流尾部发送；只有队列超过 `INTERCOM_WS_QUEUE_HIGH_WATER_AUDIO` 或连接异常时才会清理旧 audio，并打印 clear reason。

定位 seq gap 时重点看两类日志：

```text
ws_uplink_rx ws_uplink_audio source=walkie-01 ch=1 type=audio seq=123 payload=640
UDP uplink stats source=walkie-01 addr=... ch=1 rx=50 gap=2/6 late=0 dup=0 far=0 expected=1234 last_rx=1233
WS downlink stats target=walkie-02 source=walkie-01 ch=1 enqueue=50 send=50 drop=0 clear=0 queue_len=2 queue_max=4 pacing_lag_ms=0.30
```

如果 `UDP uplink stats` 已经出现 `gap>0`，说明服务器收到的设备上行 UDP 本身就缺包。
如果上行 `gap=0`，但 `WS downlink stats` 里 `drop` 或 `clear` 增加，说明缺口来自服务器下行队列。
如果上行 `gap=0`，WS `enqueue` 和 `send` 相等且 `drop=0 clear=0`，但设备端仍显示 gap，则重点查设备端 WebSocket 接收、解析、jitter buffer 或播放链路。

## 下行队列

服务端收到同频道设备的 `AUDIO` 包后，不直接突发写入 WebSocket，而是放入目标设备的 per-target 队列，再按固定节奏下发：

```text
INTERCOM_PACING_INTERVAL_MS=20
INTERCOM_PREBUFFER_PACKETS=20
INTERCOM_PREBUFFER_IDLE_FLUSH_MS=120
INTERCOM_QUEUE_MAX_PACKETS=80
INTERCOM_QUEUE_HIGH_WATER=60
INTERCOM_AUDIO_LOG_EVERY_N=50
INTERCOM_FEC_GROUP_SIZE=4
INTERCOM_NACK_CACHE_PACKETS=200
INTERCOM_NACK_CACHE_SECONDS=3
INTERCOM_NACK_MAX_COUNT=16
INTERCOM_DOWNLINK_TRANSPORT=websocket
INTERCOM_WS_HOST=0.0.0.0
INTERCOM_WS_PORT=18081
INTERCOM_WS_QUEUE_MAX_AUDIO=50
INTERCOM_WS_QUEUE_HIGH_WATER_AUDIO=40
INTERCOM_WS_PING_INTERVAL_SECONDS=20
INTERCOM_SEQ_FAR_JUMP_FRAMES=1000
```

默认含义：

- `INTERCOM_PACING_INTERVAL_MS=20`：每 20ms 发 1 个完整 WTK1 AUDIO 包。
- `INTERCOM_PREBUFFER_PACKETS=20`：新语音流先攒约 400ms 再起播。
- `INTERCOM_PREBUFFER_IDLE_FLUSH_MS=120`：短语音没攒够预缓冲时，空闲 120ms 后也会按节奏发完。
- `INTERCOM_QUEUE_MAX_PACKETS=80`：队列最多约 1.6s 音频，超过后丢最旧包，避免延迟无限增长。
- `INTERCOM_QUEUE_HIGH_WATER=60`：队列达到高水位时打印告警。
- `INTERCOM_AUDIO_LOG_EVERY_N=50`：音频热路径日志限频；设为 `0` 可关闭音频帧日志。
- `INTERCOM_FEC_GROUP_SIZE=4`：每 4 个连续 AUDIO 生成 1 个 AUDIO_FEC 包。
- `INTERCOM_NACK_CACHE_PACKETS=200`：每个目标/频道/源设备缓存最近 200 个已下发 AUDIO 包。
- `INTERCOM_NACK_CACHE_SECONDS=3`：缓存最长保留 3 秒。
- `INTERCOM_NACK_MAX_COUNT=16`：单个 NACK 最多补发 16 个连续 seq，防止一次请求挤爆下行。
- `INTERCOM_DOWNLINK_TRANSPORT=websocket`：WebSocket 全双工对讲，binary frame 是完整 WTK1 packet。
- 固件当前不走 UDP fallback；目标设备 WebSocket 不在线时，服务端只记录 drop。
- `INTERCOM_WS_QUEUE_MAX_AUDIO=50`：WebSocket 每设备最多保留约 1 秒 audio，超限丢最旧 audio。
- `INTERCOM_WS_QUEUE_HIGH_WATER_AUDIO=40`：`PTT_STOP` 到来时，只有积压超过该值才会清 audio。
- `INTERCOM_WS_PING_INTERVAL_SECONDS=20`：WebSocket ping/pong 保活间隔。
- `INTERCOM_SEQ_FAR_JUMP_FRAMES=1000`：上行 AUDIO seq 大跳变阈值，超过后按 `far` 统计并重同步。

控制包仍用于维护设备状态；服务端只转发给同频道其他设备，不回发给发送者本人。

## 测试

```bash
python -m compileall server tests
python -m unittest tests.test_udp_pcm_forward
```

不要提交 `.env`。公网服务器需要放行 TCP `18081`；如果仍保留旧 UDP 兼容输入，再额外放行 UDP `19000`。

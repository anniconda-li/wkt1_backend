# WKT1 Intercom Backend

ESP32 WTK1 设备的 UDP 实时对讲转发服务。

这个仓库现在只保留对讲链路：

```text
ESP32 device A -> WTK1 UDP AUDIO PCM -> server -> paced queue -> ESP32 device B
```

已移除 AI 问答、ASR、TTS、相机上传、视觉识别、知识库和百炼应用相关代码。那些能力由其他项目承接。

## 目录

```text
server/     WTK1 协议解析和 UDP 对讲转发
tests/      UDP 对讲单测
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

`AUDIO_FEC` 是服务端下行前向纠错包。默认每 4 个连续 AUDIO 额外生成 1 个 XOR FEC 包，跟在该组最后一个 AUDIO 后进入同一个 paced 队列。FEC 只 XOR AUDIO payload，不改正常 AUDIO 包格式和 seq。

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

服务端收到 NACK 后，会按 `target_device + channel + source_device + seq` 查最近已经下发给该目标设备的完整 AUDIO 包，找到就原样 UDP 补发；不会重新生成 seq，不会改 payload，不会重新编码 PCM。

## 安装

```bash
python -m venv .venv
cp .env.example .env
```

Windows PowerShell：

```powershell
py -m venv .venv
copy .env.example .env
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
UDP WTK1 监听 0.0.0.0:19000
UDP downlink codec=pcm mode=paced ...
```

## 下行队列

服务端收到同频道设备的 `AUDIO` 包后，不直接突发 `sendto`，而是放入目标设备的 per-target 队列，再按固定节奏下发：

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

控制包仍用于维护设备状态；服务端只转发给同频道其他设备，不回发给发送者本人。

## 测试

```bash
python -m compileall server tests
python -m unittest tests.test_udp_pcm_forward
```

不要提交 `.env`。公网服务器需要放行 UDP `19000` 入站端口。

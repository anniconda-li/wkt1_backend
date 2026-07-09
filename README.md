# WKT1 Intercom Backend

ESP32 WTK1 设备的 WebSocket 实时对讲转发服务。

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
7 NACK
8 AUDIO_FEC
```

`AUDIO` payload 仍是 PCM s16le / 16kHz / mono，通常每包 20ms、640 bytes。

服务端行为：

- `REGISTER / CHANNEL / HEARTBEAT` 更新连接状态、频道和活跃时间，不转发。
- `PTT_START / AUDIO / PTT_STOP / NACK / AUDIO_FEC` 按包头 channel 转发给同频道其他在线设备。
- 转发时原样发送收到的 binary bytes，不重组、不改 seq、不改 timestamp、不改 payload。
- 同一个 device 重连时，新连接替换旧连接。

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

`.env` 默认配置：

```text
INTERCOM_HOST=0.0.0.0
INTERCOM_WS_PORT=18081
INTERCOM_AUDIO_LOG_EVERY_N=50
```

设备连接：

```text
ws://<server_host>:18081/intercom/ws?device=walkie-01
ws://<server_host>:18081/intercom/ws?device=walkie-02
```

启动成功日志：

```text
WTK1 intercom websocket listening ws://0.0.0.0:18081/intercom/ws?device=<device>
ws connect device=walkie-01 channel=1
ws connect device=walkie-02 channel=1
```

转发日志示例：

```text
ws register device=walkie-01 ch=1 seq=0 payload=0
ws channel device=walkie-01 ch=2 seq=8 payload=0
ws forward type=ptt_start source=walkie-01 ch=2 seq=9 payload=0 targets=1 sent=1 target_devices=walkie-02
ws audio source=walkie-01 ch=2 seq=59 payload=640 targets=1 sent=1 target_devices=walkie-02
ws forward type=ptt_stop source=walkie-01 ch=2 seq=100 payload=0 targets=1 sent=1 target_devices=walkie-02
```

## 测试

```bash
python -m compileall server tests
python -m unittest discover -s tests -p "test*.py"
```

不要提交 `.env`。公网服务器需要放行 TCP `18081`。

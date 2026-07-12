# wkt-intercom-server 部署说明

本文只记录独立部署所需配置，不执行生产部署。`wkt-intercom-server` 与
`wkt-ai-server`、`wkt-ota-server` 分别使用独立 Git 仓库、镜像、容器和 Codex
任务；本地父目录 `wkt-platform` 不初始化 Git。

## 固定名称与兼容约束

- GitHub 仓库、本地目录和项目名：`wkt-intercom-server`
- Docker 镜像名：`wkt-intercom-server`
- Docker 容器名：`wkt-intercom-server`
- Compose 服务名：`intercom`
- WebSocket 端口：`18081`
- WebSocket 路径：`/intercom/ws`

仓库改名和容器化不改变 WTK1 binary frame、查询参数、环境变量或运行行为。

## 构建和本地运行

```bash
docker build --tag wkt-intercom-server .
docker run --rm --name wkt-intercom-server --env-file .env -p 18081:18081 wkt-intercom-server
```

Compose：

```bash
docker compose build intercom
docker compose up -d intercom
docker compose logs -f intercom
```

## `wkt-deploy` 接入契约

| 项目 | 约定 |
| --- | --- |
| 镜像 | `wkt-intercom-server:<tag>`；本地 Compose 使用 `wkt-intercom-server:local` |
| 默认启动命令 | `python main.py`，无需覆盖镜像 `CMD` |
| 容器端口 | `18081/tcp` |
| WebSocket 路径 | `/intercom/ws`，设备通过 `device` 查询参数标识 |
| Volume | 无；服务无持久化状态，日志写到 stdout/stderr |

运行环境变量如下。`INTERCOM_HOST` 和容器内的 `INTERCOM_WS_PORT` 必须保持固定值，
其余参数可由部署环境按需覆盖：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `INTERCOM_HOST` | `0.0.0.0` | WebSocket 监听地址 |
| `INTERCOM_WS_PORT` | `18081` | WebSocket 监听端口 |
| `INTERCOM_LOG_STATS` | `1` | 启用聚合统计日志 |
| `INTERCOM_LOG_AUDIO_TRACE` | `0` | 启用逐包音频采样日志 |
| `INTERCOM_AUDIO_LOG_EVERY_N` | `50` | 音频采样日志间隔帧数 |
| `INTERCOM_SEND_QUEUE_MAX` | `80` | 单设备下行发送队列上限 |
| `INTERCOM_SEND_TIMEOUT_SECONDS` | `2` | 单次 WebSocket 发送超时秒数 |
| `INTERCOM_REALTIME_WINDOW_MS` | `400` | 队列音频实时窗口毫秒数 |
| `INTERCOM_STATS_INTERVAL_MS` | `1000` | 聚合统计窗口毫秒数 |

镜像和当前 Compose 文件没有声明 Docker `HEALTHCHECK`。`wkt-deploy` 应使用
WebSocket 握手作为就绪和存活探针：连接
`ws://127.0.0.1:18081/intercom/ws?device=wkt-deploy-healthcheck`，收到 HTTP `101`
握手响应即视为成功，然后立即关闭连接。不要把普通 HTTP `200` 请求作为健康检查；
本服务没有 HTTP 健康端点。容器内可执行的等价探针为：

```bash
docker exec wkt-intercom-server python -c "from websockets.sync.client import connect; ws=connect('ws://127.0.0.1:18081/intercom/ws?device=wkt-deploy-healthcheck', open_timeout=3, close_timeout=1); ws.close()"
```

该命令成功退出为健康，非零退出为不健康。部署层应为探针设置超时，并使用固定的
`wkt-deploy-healthcheck` 设备名，避免与真实设备名冲突。

如需推送到镜像仓库，可增加 registry/owner 前缀和版本标签，但镜像仓库的基础名称
仍应为 `wkt-intercom-server`，例如
`ghcr.io/anniconda-li/wkt-intercom-server:<version>`。当前 GitHub Actions 只测试并构建
`wkt-intercom-server:ci`，不会登录 registry、推送镜像或部署服务器。

## 生产环境手工配置清单

部署前由维护者按实际环境手工确认：

1. 创建独立的 `wkt-intercom-server` 镜像仓库，并配置最小权限的登录凭据。
2. 在服务器创建 `.env`，按 `.env.example` 设置日志、队列和实时窗口参数。
3. 放行 TCP `18081`；如经过反向代理，启用 WebSocket Upgrade 并保持
   `/intercom/ws` 路径和查询字符串不变。
4. 为独立容器设置日志轮转、重启策略、资源限制和主机级监控。
5. 若以后增加发布工作流，单独配置 registry 和服务器 secrets，并在人工审批环境后
   才允许部署。不要与 AI 或 OTA 服务共用镜像、容器或发布作业。

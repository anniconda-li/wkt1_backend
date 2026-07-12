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

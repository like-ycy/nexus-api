# nexus-api

Nexus 云端单体 v1。

## 启动

```bash
UV_CACHE_DIR=.uv-cache uv sync --group dev
UV_CACHE_DIR=.uv-cache uv run nexus-api
```

入口文件：`src/app.py`

默认配置文件：

```bash
config/env.test.toml
```

如果需要切到其他配置文件，再显式传 `NEXUS_CONFIG_PATH`：

```bash
NEXUS_CONFIG_PATH=config/xxx.toml UV_CACHE_DIR=.uv-cache uv run nexus-api
```

## 默认协议

- 与 `nexus-edge` 当前协议兼容
- 默认监听 `0.0.0.0:8765`
- `models/` 当前主要保留 VAD 模型
- ASR/TTS 默认走独立模型服务，建议部署在单独的模型机上

## 配置

- 默认共享配置文件：`config/env.test.toml`
- 启动时通过 `src.config.load_config()` 加载为 `dict`
- 环境变量前缀：`NEXUS_`
- 支持 `NEXUS_CONFIG_PATH` 指向其他 TOML 配置文件

## 说明

- 当前版本是单体云端 v1，不拆 gateway/session/speech/dialog
- `src/app.py` 负责启动装配，WebSocket 生命周期在 `src/core/websocket_server.py`，单连接处理在 `src/core/connection_handler.py`
- HTTP 接口由 `src/core/http_server.py` 与 `src/routers/` 负责
- 业务服务在 `src/services/`，底层能力适配在 `src/providers/`，领域对象在 `src/domain/`，持久化在 `src/db/`
- 配置加载入口在 `src/config/config_loader.py`，协议定义在 `src/protocol/`
- 详细分层说明见 `ARCHITECTURE.md`

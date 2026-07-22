# nexus-api 线下服务器部署说明

本文档适用于当前重构后的云端部署方式。云端由 3 个服务组成：

- `nexus-asr-service`: 统一 ASR 服务，当前后端为 `funasr-paraformer-zh`
- `nexus-tts-service`: 统一 TTS 服务，当前后端为火山 TTS
- `nexus-api`: 主 API 服务，通过统一协议调用 ASR/TTS 服务

## 端口约定

| 服务 | 宿主机端口 | 容器端口 | 说明 |
| --- | ---: | ---: | --- |
| nexus-api HTTP | 18082 | 8080 | HTTP API |
| nexus-api WebSocket | 8765 | 8765 | 边端 WebSocket 连接 |
| ASR 服务 | 18081 | 18081 | `POST /asr` |
| TTS 服务 | 18083 | 18083 | `POST /tts/stream` |

## 服务器目录约定

代码目录：

```bash
/root/workspaces/nexus-api
```

ASR 模型目录：

```bash
/root/nexus-model-services/models/asr/funasr-paraformer-zh
```

当前 ASR 服务的 Docker Compose 会把宿主机目录 `/root/nexus-model-services/models` 挂载到容器内 `/models`，所以容器内模型路径为：

```bash
/models/asr/funasr-paraformer-zh
```

TTS 使用火山在线服务，不需要上传模型文件，只需要配置火山 `APPID` 和 `ACCESS_TOKEN`。

## 1. 上传代码

在本机 `nexus-api` 根目录执行：

```bash
rsync -av \
  --exclude='.venv' \
  --exclude='.uv-cache' \
  --exclude='.git' \
  ./ root@服务器IP:/root/workspaces/nexus-api/
```

## 2. 上传 ASR 模型

将 `funasr-paraformer-zh` 模型上传到服务器：

```bash
/root/nexus-model-services/models/asr/funasr-paraformer-zh
```

上传后在服务器确认：

```bash
ls -lah /root/nexus-model-services/models/asr/funasr-paraformer-zh
```

## 3. 启动 ASR 服务

在服务器执行：

```bash
cd /root/workspaces/nexus-api/deploy/asr_service
docker compose up -d --build
```

验证：

```bash
curl -fsS http://127.0.0.1:18081/healthz
curl -fsS http://127.0.0.1:18081/metadata
```

注意当前 `deploy/asr_service/docker-compose.yml` 默认使用 CPU：

```yaml
ASR_DEVICE: cpu
```

因此 ASR 容器不需要 GPU 运行时配置。

## 4. 启动 TTS 服务

在服务器执行：

```bash
cd /root/workspaces/nexus-api/deploy/tts_service

export HUOSHAN_TTS_APPID=你的火山APPID
export HUOSHAN_TTS_ACCESS_TOKEN=你的火山TOKEN

docker compose up -d --build
```

验证：

```bash
curl -fsS http://127.0.0.1:18083/healthz
curl -fsS http://127.0.0.1:18083/metadata
```

## 5. 启动 nexus-api 主服务

在服务器执行：

```bash
cd /root/workspaces/nexus-api
cp deploy/nexus-api/robot-test.env.example deploy/nexus-api/robot-test.env
vi deploy/nexus-api/robot-test.env
```

至少确认数据库连接配置正确：

```bash
NEXUS_DATABASE__DSN=postgresql://postgres:postgres@host.docker.internal:5432/nexus_api
```

启动主服务：

```bash
cd /root/workspaces/nexus-api/deploy/nexus-api
export $(grep -v '^#' robot-test.env | xargs)
docker compose -f docker-compose.robot-test.yml up -d --build
```

验证：

```bash
curl -fsS http://127.0.0.1:18082/health
docker ps | grep nexus
```

## 6. 服务调用关系

`nexus-api` 当前通过以下地址访问模型服务：

```bash
NEXUS_ASR__PRIMARY__ENDPOINT=http://host.docker.internal:18081/asr
NEXUS_TTS__REMOTE_SERVICE__STREAM_URL=http://host.docker.internal:18083/tts/stream
```

这些变量已经写在 `deploy/nexus-api/docker-compose.robot-test.yml` 中。只要 ASR/TTS 服务和主服务部署在同一台宿主机，默认配置即可使用。

## 7. 常用排查命令

查看容器：

```bash
docker ps | grep nexus
```

查看 ASR 日志：

```bash
docker logs -f nexus-asr-service
```

查看 TTS 日志：

```bash
docker logs -f nexus-tts-service
```

查看主服务日志：

```bash
docker logs -f nexus-api
```

重启服务：

```bash
docker restart nexus-asr-service
docker restart nexus-tts-service
docker restart nexus-api
```

停止服务：

```bash
cd /root/workspaces/nexus-api/deploy/asr_service
docker compose down

cd /root/workspaces/nexus-api/deploy/tts_service
docker compose down

cd /root/workspaces/nexus-api/deploy/nexus-api
docker compose -f docker-compose.robot-test.yml down
```

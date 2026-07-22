# nexus-api Docker deploy

主服务当前没有现成 Docker 产物，这个目录补的是主服务在 `robot-test` / `robot-prod` 上的部署方案。

## 端口约定

- WebSocket: 宿主机 `8765` -> 容器 `8765`
- HTTP: 宿主机 `18082` -> 容器 `8080`

之所以不用宿主机 `8080`，是因为 `robot-test` 上已经有 `nginx` 占用了该端口。

## 依赖约定

- 宿主机已经运行 ASR 服务：`http://127.0.0.1:18081/asr`
- 宿主机已经运行 TTS 服务：`http://127.0.0.1:18083/tts/stream`
- 容器通过 `host.docker.internal` 回连宿主机上的 ASR/TTS 服务
- 如果启用 orchestration/conversation 持久化，建议 PostgreSQL 也运行在宿主机或同网络容器内
- VAD 模型通过只读挂载提供：`models/vad/silero_vad.onnx`

## 数据库配置

参考 `percept-api` 的做法，这里也推荐通过部署环境变量注入数据库连接，而不是把真实地址写死在仓库里。

如果你和 `percept-api` 共用同一个 PostgreSQL 实例，只是想分开不同数据库，那么可以保持：

- 同一个 `host`
- 同一个 `port`
- 同一个 `user/password`
- 不同的数据库名

例如：

- `percept-api` 使用：`embodied_cloud`
- `nexus-api` 使用：`nexus_api`

也就是先在同一个 PostgreSQL 实例里创建独立数据库：

```sql
CREATE DATABASE nexus_api;
```

示例文件：

```bash
cp deploy/nexus-api/robot-test.env.example deploy/nexus-api/robot-test.env
```

示例变量：

```bash
NEXUS_DATABASE__DSN=postgresql://postgres:postgres@host.docker.internal:5432/nexus_api
NEXUS_ORCHESTRATION__ROBOT_ENDPOINT=
```

加载变量后再部署：

```bash
export $(grep -v '^#' deploy/nexus-api/robot-test.env | xargs)
bash scripts/deploy_robot_test.sh
```

## 手动部署

在本机仓库根目录执行：

```bash
bash scripts/deploy_robot_test.sh
```

如果想手动执行，核心命令如下：

```bash
tar czf - Dockerfile .dockerignore README.md pyproject.toml uv.lock src config deploy/nexus-api models/vad \
  | ssh robot-test 'mkdir -p /root/workspaces/nexus-api && tar xzf - -C /root/workspaces/nexus-api'

ssh robot-test 'cd /root/workspaces/nexus-api/deploy/nexus-api && docker-compose -f docker-compose.robot-test.yml up -d --build'
```

## 验证

```bash
ssh robot-test 'docker ps --filter name=nexus-api'
ssh robot-test 'curl -fsS http://127.0.0.1:18082/health'
```

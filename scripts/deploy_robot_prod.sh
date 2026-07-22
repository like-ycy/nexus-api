#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-robot-prod}"
REMOTE_DIR="${REMOTE_DIR:-/root/workspaces/nexus-api}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

if [[ ! -f "${PROJECT_ROOT}/models/vad/silero_vad.onnx" ]]; then
  echo "missing VAD model: ${PROJECT_ROOT}/models/vad/silero_vad.onnx" >&2
  exit 1
fi

cd "${PROJECT_ROOT}"

# 只同步主服务构建所需文件，避免把本地大模型目录整体上传到远端。
export COPYFILE_DISABLE=1

tar czf - \
  Dockerfile \
  .dockerignore \
  README.md \
  pyproject.toml \
  uv.lock \
  src \
  config \
  deploy/nexus-api \
  models/vad \
  | ssh "${REMOTE_HOST}" "mkdir -p '${REMOTE_DIR}' && tar xzf - -C '${REMOTE_DIR}'"

ssh "${REMOTE_HOST}" "cd '${REMOTE_DIR}/deploy/nexus-api' && docker-compose -f docker-compose.robot-prod.yml up -d --build"

echo "deployed to ${REMOTE_HOST}:${REMOTE_DIR}"
echo "health check: ssh ${REMOTE_HOST} 'curl -fsS http://127.0.0.1:18082/health'"

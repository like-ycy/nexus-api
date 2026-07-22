# nexus-asr-service

统一 ASR 服务，面向 `nexus-api` 提供稳定 HTTP 协议边界。

当前唯一后端：

```text
backend: funasr
model: funasr-paraformer-zh
device: cpu
```

## API

```text
GET  /healthz
GET  /metadata
POST /asr
```

`POST /asr` 使用 `multipart/form-data` 上传 16kHz 单声道 WAV：

```text
file=audio.wav
```

响应：

```json
{
  "text": "识别文本",
  "backend": "funasr",
  "model": "funasr-paraformer-zh",
  "device": "cpu",
  "sample_rate": 16000,
  "latency_ms": 123.4
}
```

## 启动

```bash
docker compose up -d --build
```

默认模型目录：

```text
/models/asr/funasr-paraformer-zh
```

默认运行设备：

```text
cpu
```

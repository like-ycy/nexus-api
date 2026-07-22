# nexus-tts-service

统一 TTS 服务，面向 `nexus-api` 提供稳定 HTTP 流式协议边界。

当前唯一后端：

```text
backend: huoshan
speaker: zh_female_qinqienvsheng_moon_bigtts
```

## API

```text
GET  /healthz
GET  /metadata
POST /tts/stream
```

`POST /tts/stream` 请求：

```json
{
  "text": "你好",
  "voice": "default"
}
```

响应为裸 PCM 流：

```text
Content-Type: application/octet-stream
X-Audio-Format: pcm_s16le
X-Sample-Rate: 24000
X-Channels: 1
```

## 启动

```bash
export HUOSHAN_TTS_APPID=...
export HUOSHAN_TTS_ACCESS_TOKEN=...
docker compose up -d --build
```

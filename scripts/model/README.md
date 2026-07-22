# FunASR 模型预下载

先安装 FunASR 额外依赖：

```bash
uv sync --group dev
```

然后下载 ASR 模型到 `models/asr/`：

```bash
UV_CACHE_DIR=.uv-cache uv run python3 scripts/model/download_funasr_asr_model.py
```

默认模型：

- `paraformer-zh`

说明：

- 实际下载通过 ModelScope 完成

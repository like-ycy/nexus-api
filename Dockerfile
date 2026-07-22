FROM docker.m.daocloud.io/library/python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libgomp1 \
        libopus0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY README.md pyproject.toml uv.lock /app/
COPY src /app/src
COPY config /app/config

RUN pip install --upgrade pip \
    && pip install \
        asyncpg==0.30.0 \
        fastapi==0.116.1 \
        httpx==0.28.1 \
        loguru==0.7.3 \
        numpy==1.26.4 \
        opuslib==3.0.1 \
        python-multipart==0.0.20 \
        sherpa-onnx==1.13.0 \
        sherpa-onnx-bin==1.13.0 \
        tomli==2.3.0 \
        uvicorn==0.35.0 \
        websockets==16.0

EXPOSE 8080 8765

ENV PYTHONPATH=/app

CMD ["python", "-m", "src.app"]

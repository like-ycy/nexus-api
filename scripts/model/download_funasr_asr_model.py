"""预下载 FunASR ASR 模型到本仓库 `models/asr`。

默认下载 `paraformer-zh` 对应的 ModelScope 仓库，然后将模型目录复制到：
`models/asr/funasr-paraformer-zh`

用法示例：

    uv sync --group dev
    UV_CACHE_DIR=.uv-cache uv run python3 scripts/model/download_funasr_asr_model.py
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

DEFAULT_MODEL_NAME = "paraformer-zh"
DEFAULT_TARGET_DIR_NAME = "funasr-paraformer-zh"
DEFAULT_MODEL_ID = "damo/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODELSCOPE_CACHE = PROJECT_ROOT / ".modelscope-cache"

os.environ.setdefault("MODELSCOPE_CACHE", str(MODELSCOPE_CACHE))
os.environ.setdefault("MODELSCOPE_HOME", str(PROJECT_ROOT / ".modelscope-home"))

MODEL_REGISTRY = {
    "paraformer-zh": DEFAULT_MODEL_ID,
}


def main() -> int:
    args = parse_args()
    return download_model(
        model_name=args.model_name,
        target_dir=args.target_dir,
        overwrite=args.overwrite,
    )


def download_model(
    *,
    model_name: str,
    target_dir: Path,
    overwrite: bool,
) -> int:
    try:
        try:
            from modelscope import snapshot_download
        except ImportError:
            from modelscope.hub.snapshot_download import snapshot_download
    except ImportError:
        print("ModelScope 未安装，请先执行: uv sync --group dev", file=sys.stderr)
        return 1

    target_dir = target_dir.resolve()
    target_dir.parent.mkdir(parents=True, exist_ok=True)

    if target_dir.exists():
        if not overwrite:
            print(
                f"目标目录已存在: {target_dir}\n"
                "如需重新下载请加 --overwrite",
                file=sys.stderr,
            )
            return 1
        shutil.rmtree(target_dir)

    print(f"开始下载 FunASR 模型: {model_name}")
    print(f"目标目录: {target_dir}")

    try:
        model_id = MODEL_REGISTRY.get(model_name, model_name)
        source_dir = Path(
            snapshot_download(
                model_id=model_id,
                cache_dir=str(PROJECT_ROOT / ".modelscope-cache"),
            )
        ).resolve()
        if not source_dir.is_dir():
            raise RuntimeError(f"未返回有效模型目录: {source_dir}")

        if source_dir == target_dir:
            print(f"模型已就位: {target_dir}")
            return 0

        shutil.copytree(source_dir, target_dir)
    except Exception as exc:
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        print(f"下载失败: {exc}", file=sys.stderr)
        return 1

    print("下载完成")
    print(f"本地模型目录: {target_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="预下载 FunASR ASR 模型")
    parser.add_argument(
        "--model-name",
        default=DEFAULT_MODEL_NAME,
        help="FunASR 模型名，默认 paraformer-zh",
    )
    parser.add_argument(
        "--target-dir",
        type=Path,
        default=PROJECT_ROOT / "models" / "asr" / DEFAULT_TARGET_DIR_NAME,
        help="本地目标目录，默认 models/asr/funasr-paraformer-zh",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="如果目标目录已存在则覆盖",
    )
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())

"""Opus 动态库加载辅助工具。"""

from __future__ import annotations

import ctypes
import ctypes.util
import platform
import sys

from pathlib import Path
from typing import cast

from src.utils.logging import logger

_ENV_OPUS_LIB = "NEXUS_OPUS_LIB"
_FIND_LIBRARY_NAMES = ("opus", "libopus")
_PATCHED_LIBRARY_NAMES = {
    "opus",
    "libopus",
    "libopus.so",
    "libopus.so.0",
    "libopus.dylib",
}
_COMMON_PATHS = {
    "darwin": (
        Path("/opt/homebrew/lib/libopus.dylib"),
        Path("/usr/local/lib/libopus.dylib"),
        Path("/usr/lib/libopus.dylib"),
    ),
    "linux": (
        Path("/usr/lib/x86_64-linux-gnu/libopus.so.0"),
        Path("/usr/lib/x86_64-linux-gnu/libopus.so"),
        Path("/lib/x86_64-linux-gnu/libopus.so.0"),
        Path("/lib/x86_64-linux-gnu/libopus.so"),
        Path("/usr/lib/aarch64-linux-gnu/libopus.so.0"),
        Path("/usr/lib/aarch64-linux-gnu/libopus.so"),
        Path("/lib/aarch64-linux-gnu/libopus.so.0"),
        Path("/lib/aarch64-linux-gnu/libopus.so"),
        Path("/usr/lib64/libopus.so.0"),
        Path("/usr/lib64/libopus.so"),
        Path("/usr/lib/libopus.so.0"),
        Path("/usr/lib/libopus.so"),
        Path("/lib64/libopus.so.0"),
        Path("/lib64/libopus.so"),
        Path("/lib/libopus.so.0"),
        Path("/lib/libopus.so"),
        Path("/opt/homebrew/lib/libopus.dylib"),
    ),
}


def setup_opus() -> str | None:
    """加载 Opus 动态库并返回实际加载的路径。"""
    loaded_path = cast(str | None, getattr(sys, "_nexus_opus_loaded_path", None))
    if bool(getattr(sys, "_nexus_opus_loaded", False)):
        return loaded_path

    library_path = find_opus_library()
    if library_path is None:
        logger.warning("未找到可用的 opus 动态库，请检查系统是否已安装 libopus")
        return None

    _patch_find_library(library_path)

    try:
        _load_library(library_path)
    except OSError as exc:
        logger.warning("加载 opus 动态库失败: {} | path={}", exc, library_path)
        return None

    setattr(sys, "_nexus_opus_loaded", True)
    setattr(sys, "_nexus_opus_loaded_path", library_path)
    logger.info("opus 动态库已加载: {}", library_path)
    return library_path


def find_opus_library() -> str | None:
    """查找当前系统可用的 Opus 动态库路径。"""
    explicit_path = _get_explicit_library_path()
    if explicit_path is not None:
        return explicit_path

    for library_name in _FIND_LIBRARY_NAMES:
        resolved = ctypes.util.find_library(library_name)
        if resolved:
            return resolved

    system = platform.system().lower()
    for candidate in _COMMON_PATHS.get(system, ()):
        if candidate.is_file():
            return str(candidate)

    return None


def _get_explicit_library_path() -> str | None:
    configured = sys.modules.get("os", __import__("os")).environ.get(_ENV_OPUS_LIB)
    if not configured:
        return None

    candidate = Path(configured).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())

    logger.warning(
        "环境变量 {} 指向的 opus 动态库不存在: {}",
        _ENV_OPUS_LIB,
        configured,
    )
    return None


def _patch_find_library(library_path: str) -> None:
    original_find_library = ctypes.util.find_library

    if getattr(ctypes.util.find_library, "_nexus_opus_patched", False):
        return

    def patched_find_library(name: str) -> str | None:
        if name in _PATCHED_LIBRARY_NAMES:
            return library_path
        return original_find_library(name)

    patched_find_library._nexus_opus_patched = True  # type: ignore[attr-defined]
    ctypes.util.find_library = patched_find_library


def _load_library(library_path: str) -> None:
    mode = getattr(ctypes, "RTLD_GLOBAL", None)
    if mode is None:
        ctypes.CDLL(library_path)
        return
    ctypes.CDLL(library_path, mode=mode)

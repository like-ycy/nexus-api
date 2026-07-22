"""项目目录结构解析工具。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProjectPaths:
    """项目关键目录的不可变快照。"""

    project_root: Path
    config_dir: Path
    package_root: Path


def get_project_paths() -> ProjectPaths:
    """根据本文件位置推导并返回项目各关键目录。"""
    project_root = Path(__file__).resolve().parents[2]
    return ProjectPaths(
        project_root=project_root,
        config_dir=project_root / "config",
        package_root=project_root / "src",
    )

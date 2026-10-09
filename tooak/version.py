"""版本号与"这份构建是哪一次提交"的印章。


为什么要**连提交号一起显示**：这个程序是从源码目录直接跑的（`./tooak.sh` → `run.py`），
没有一个"安装了什么版本"的东西可查 —— 改完代码没重启、或者手滑开到了另一份旧目录，
从界面上完全看不出来。所以除了语义版本号，还从 git 里取一次短提交号 + 提交日期 +
"有没有未提交的改动"，拼成一眼能核对的一行：

    ToOak v0.4.2 · 3f9c1a2（2026-09-29）· 有未提交改动

取不到 git（没装 git / 不是 git 目录 / 打包分发）时只显示版本号，绝不因此报错。
"""

from __future__ import annotations

import subprocess
from functools import lru_cache
from pathlib import Path

__all__ = ["VERSION", "APP_NAME", "build_stamp", "version_text", "version_line"]

#: 语义版本号。改动大到用户能感觉出来的量级就往上走一位。
VERSION = "0.4.2"

APP_NAME = "ToOak"

_REPO_DIR = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def build_stamp() -> str:
    """``提交号（日期）`` 这一截构建信息；取不到返回空串。

    """
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=_REPO_DIR,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()

    try:
        commit = git("rev-parse", "--short", "HEAD")
        if not commit:
            return ""
        stamp = commit
        date = git("log", "-1", "--format=%cd", "--date=short")
        if date:
            stamp += f"（{date}）"
        if git("status", "--porcelain"):
            stamp += " · 有未提交改动"
        return stamp
    except (OSError, subprocess.SubprocessError):   # pragma: no cover - 环境相关
        return ""


def version_text() -> str:
    """``ToOak v0.4.2 · 3f9c1a2（2026-09-29）`` —— 标题栏、状态栏、日志都用它。"""
    stamp = build_stamp()
    return f"{APP_NAME} v{VERSION}" + (f" · {stamp}" if stamp else "")


def version_line() -> str:
    """启动时打到终端/日志里的一行（含解释器与库路径，排查"开的是哪个"时最省事）。"""
    import sys

    return f"{version_text()} · Python {sys.version.split()[0]} · {_REPO_DIR}"

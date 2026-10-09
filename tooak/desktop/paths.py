"""库位置辅助：记住「上次用过的库」，让启动器（tooak.sh）与界面共用同一处。

为什么要单独一个文件：``app.py`` 与 ``main_window.py`` 都要用它，
而 ``app.py`` 会 import ``main_window`` —— 放回 app 里就会循环 import。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["state_dir", "last_library_file", "read_last_library", "write_last_library"]


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "tooak"


def last_library_file() -> Path:
    return state_dir() / "last-library"


def read_last_library(db_filename: str = "tooak.sqlite3") -> Path | None:
    """读「上次用过的库」；没记过 / 记的目录已经没有库了，就返回 None。"""
    path = last_library_file()
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    root = Path(text).expanduser()
    if not (root / db_filename).exists():
        return None
    return root


def write_last_library(root: str | Path) -> None:
    """记下「上次用过的库」（界面「打开库…」用它；换过位置后下次还开那个库）。"""
    path = last_library_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(Path(root).expanduser()), encoding="utf-8")

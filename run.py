#!/usr/bin/env python3
"""ToOak 启动脚本：``python run.py``。

不带参数时打开默认库（~/ToOakLibrary，可用环境变量 TOOAK_LIBRARY 覆盖）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tooak.desktop.app import run  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(run())

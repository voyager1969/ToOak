"""定位随程序打包的资源文件（图标、审阅页 HTML 等）。

源码运行时这些文件就在仓库里固定位置；用 PyInstaller 打成 exe 之后，
它们会被塞进 ``sys._MEIPASS``（解包目录）。这里给一个统一的入口，
两套环境都能取到，调用方不用关心"现在是不是冻结环境"。
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["resource_path"]


def resource_path(*parts: str) -> Path:
    """返回资源文件的真实路径。

    :param parts: 相对仓库根目录的路径分量，例如 ``("tooak", "desktop", "assets")``。
    """
    if getattr(sys, "frozen", False):
        # PyInstaller 冻结环境：数据文件解包到 sys._MEIPASS
        return Path(sys._MEIPASS).joinpath(*parts)
    return Path(__file__).resolve().parent.parent.joinpath(*parts)

"""打开库之前的**升级把关**：先说清楚要升级、能先备份，再动库。

为什么要单独一个模块：结构迁移是**不可逆**的（升到新版之后，旧版 ToOak 会拒绝
打开这个库，见 ``db.initialize`` 里那句"结构版本比当前程序新"），而迁移发生在
``Library.open()`` 里 —— 到那时候再提示已经晚了。所以入口在动库**之前**先
只读地问一句版本号（``db.read_schema_version``），需要升级时弹一个框：

* 说清**要发生什么**（v15 → v20，只加列 / 建表）；
* 说清**数据会不会动**（实测 0.4.4 的库升上来，事件、年轮、文件一条不丢）；
* 说清**代价**（升完就回不去了：0.4.4 打不开它）；
* 给一条**退路**："先备份库目录再升级"（一键拷一份到库目录旁边）。

"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from ..core import Library
from ..core.db import SCHEMA_VERSION, read_schema_version

__all__ = ["upgrade_pending", "backup_library", "ensure_library_is_current",
           "backup_folder_name"]

#: 备份目录名里的时间戳格式（跟库里其它时间戳一样是可读的本地时间）
STAMP_FORMAT = "%Y%m%d-%H%M%S"


def upgrade_pending(root: str | Path) -> int | None:
    """这个库要不要升级？**只读**看一眼，返回它现在的结构版本（不用升级返回 None）。

    * 版本已经是最新、或者比程序还新（比程序新的话 ``Library.open`` 会自己报错）
      → ``None``，这里只负责"老库要升级"这一种。
    """
    current = read_schema_version(Path(root) / Library.DB_FILENAME)
    if current is None or current >= SCHEMA_VERSION:
        return None
    return current


def backup_folder_name(root: str | Path, *, when: datetime | None = None) -> Path:
    """备份目录的路径：``<库目录>-备份-20261010-154233``（放在库目录**旁边**）。

    为什么放在旁边而不是库里面：备份要能整个拷走/删掉，混在库目录里容易被
    "整库备份 = 拷走这一个目录"那条规矩带进去，越滚越大。
    同一秒里点两次也不会覆盖：重名时往后加 ``-2``、``-3``。
    """
    source = Path(root).expanduser()
    stamp = (when or datetime.now()).strftime(STAMP_FORMAT)
    candidate = source.parent / f"{source.name}-备份-{stamp}"
    index = 2
    while candidate.exists():
        candidate = source.parent / f"{source.name}-备份-{stamp}-{index}"
        index += 1
    return candidate


def backup_library(root: str | Path, *, progress: Callable[[int, int], None] | None = None,
                   cancelled: Callable[[], bool] | None = None) -> Path:
    """把整个库目录拷一份到旁边（``files/``、``exports/``、``tooak.sqlite3`` 全带上）。

    ``progress`` 每拷一个文件报一次 ``(已拷, 总数)``；``cancelled`` 返回 True 时
    中途收手（**半份备份会被删掉**，绝不留一个看起来像备份、其实是残缺的目录）。

    :returns: 备份目录的路径。
    :raises OSError: 拷贝失败（磁盘满、没权限）。
    :raises InterruptedError: 用户中途取消。
    """
    source = Path(root).expanduser()
    target = backup_folder_name(source)
    # 先数一遍：进度条要有总数才像话（库目录一般是几千个文件，数一遍很快）
    total = sum(len(files) for _dirpath, _dirnames, files in os.walk(source))
    if progress is not None:
        progress(0, total)
    done = 0
    try:
        for dirpath, dirnames, files in os.walk(source):
            relative = Path(dirpath).relative_to(source)
            (target / relative).mkdir(parents=True, exist_ok=True)
            for name in files:
                if cancelled is not None and cancelled():
                    raise InterruptedError("用户取消了备份")
                shutil.copy2(Path(dirpath) / name, target / relative / name)
                done += 1
                if progress is not None:
                    progress(done, total)
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)      # 不留半份
        raise
    return target


def ensure_library_is_current(root: str | Path, *, ask: Callable[[int, int], str] | None = None,
                              backup: Callable[[Path], Path | None] | None = None) -> bool:
    """启动时把一道关：老库要先说清楚、必要时先备份，然后才让程序去打开它。

    :param ask: 提问的回调 ``(当前版本, 新版本) -> "backup" | "upgrade" | "cancel"``；
        不给就弹真的对话框（``_ask_dialog``）。测试注入假回答。
    :param backup: 备份的执行者（默认 :func:`_backup_with_progress`）。
    :returns: ``True`` = 可以继续打开（要升级就升）；``False`` = 用户选择先不动。
    """
    current = upgrade_pending(root)
    if current is None:
        return True
    answer = (ask or _ask_dialog)(current, SCHEMA_VERSION)
    if answer == "cancel":
        return False
    if answer == "backup":
        maker = backup or _backup_with_progress
        made = maker(Path(root).expanduser())
        if made is None:
            # 备份没成（用户取消 / 磁盘满）：**不要**接着升级 —— 用户点的是
            # "先备份再升级"，备份没落地就升级等于把他的退路吞了
            return False
    return True


# ---------------------------------------------------------------------------
# 界面部分（只有入口在用；测试里注入上面的回调，不弹真框）
# ---------------------------------------------------------------------------


def _ask_dialog(current: int, target: int) -> str:
    """真的弹那个框：三个出口 —— 先备份再升级 / 直接升级 / 先不动。"""
    from ..version import VERSION

    box = QMessageBox()
    box.setWindowTitle("这个库要升级结构")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(
        f"这个库是旧版本 ToOak 建的（结构 v{current}），当前程序（ToOak v{VERSION}）"
        f"要把它升级到 v{target}。\n\n"
        "升级只做「加列 / 建表」这类事：你的事件枝、年轮、文件一条都不会动。\n"
        "但升级**不可逆** —— 升完之后旧版 ToOak 会拒绝打开这个库。"
    )
    box.setInformativeText(
        "建议先备份一份库目录（整个目录拷到旁边，含 files/ 里的副本）。"
    )
    backup_button = box.addButton("先备份再升级", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("直接升级", QMessageBox.ButtonRole.DestructiveRole)
    cancel_button = box.addButton("先不动，退出", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(backup_button)
    box.exec()
    clicked = box.clickedButton()
    if clicked is cancel_button:
        return "cancel"
    return "backup" if clicked is backup_button else "upgrade"


def _backup_with_progress(root: Path) -> Path | None:
    """带进度条的备份（库可能几个 GB，不能让它看起来像卡死）。

    进度框上那个按钮写"取消备份"：用户能收手，而且**半份备份会被删掉**
    （``backup_library`` 的 finally）。失败/取消都返回 ``None``，由调用方决定
    "那就别升级了"。
    """
    dialog = QProgressDialog("正在备份库目录…", "取消备份", 0, 1)
    dialog.setWindowTitle("备份库")
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setMinimumDuration(0)
    dialog.setAutoClose(False)
    dialog.setAutoReset(False)

    def report(done: int, total: int) -> None:
        dialog.setMaximum(max(total, 1))
        dialog.setValue(done)
        QApplication.processEvents()

    try:
        target = backup_library(root, progress=report,
                                cancelled=dialog.wasCanceled)
    except InterruptedError:
        dialog.close()
        _tell("备份已取消", "没有备份，也就没有动这个库。")
        return None
    except OSError as error:
        dialog.close()
        _tell("备份失败", f"{error}\n\n库一个字节都没动；要升级的话请先自己拷一份库目录。")
        return None
    dialog.setValue(dialog.maximum())
    dialog.close()
    _tell("备份好了", f"库目录已经备份到：\n{target}\n\n接下来升级这个库。")
    return target


def _tell(title: str, text: str) -> None:
    box = QMessageBox()
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(text)
    box.exec()

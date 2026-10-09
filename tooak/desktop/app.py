"""桌面端入口：``python run.py`` 或 ``python -m tooak.desktop.app``。

用法::

    python run.py                       # 打开默认库（~/ToOakLibrary）
    python run.py -L ~/我的库           # 指定库
    python run.py --demo                # 库为空时先造一份演示数据
    python run.py --library ~/库 --serve  # 启动时同时打开 Web 审阅端
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QGuiApplication
from ..core import Library
from ..core.demo import create_demo_library
from ..version import version_line, version_text
from .main_window import MainWindow
from .paths import read_last_library, write_last_library
from .style import apply_theme, app_icon, watch_color_scheme


__all__ = ["run", "default_library_path", "read_last_library", "write_last_library"]


def default_library_path() -> Path:
    # 优先级：TOOAK_LIBRARY / TOAK_LIBRARY > 上次用过的库 > ~/ToOakLibrary
    env = os.environ.get("TOOAK_LIBRARY") or os.environ.get("TOAK_LIBRARY")
    if env:
        return Path(env).expanduser()
    remembered = read_last_library()
    if remembered is not None:
        return remembered
    return Path.home() / "ToOakLibrary"


def _enable_system_input_method() -> None:
    """Linux 下把系统 Qt 的插件目录加进搜索路径，让中文输入法可用。

    pip 装的 PySide6 自带一份 Qt，但**不带** platforminputcontexts 插件；
    fcitx/ibus 的插件装在系统 Qt 插件目录里（如 ``/usr/lib/qt6/plugins/…``）。
    不把它们加进 ``QT_PLUGIN_PATH``，Qt 就找不到输入法插件，输入框里打不进中文。

    只在 Linux 上做；Windows / macOS 走系统自己的输入法机制，不碰。用户已经
    设过 ``QT_PLUGIN_PATH`` 时也不覆盖（尊重手工配置）。
    """
    if not sys.platform.startswith("linux"):
        return
    if os.environ.get("QT_PLUGIN_PATH"):
        return
    # 各发行版常见的系统 Qt6/Qt 插件目录（按实际存在与否挑出来）
    candidates = (
        "/usr/lib/qt6/plugins",
        "/usr/lib64/qt6/plugins",
        "/usr/lib/qt/plugins",
        "/usr/lib/x86_64-linux-gnu/qt6/plugins",
    )
    extra = [path for path in candidates if os.path.isdir(path)]
    if extra:
        os.environ["QT_PLUGIN_PATH"] = os.pathsep.join(extra)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ToOak", description="ToOak 文档管理器（桌面端）")
    parser.add_argument(
        "-L", "--library", type=Path, default=default_library_path(),
        help=f"库目录（默认 {default_library_path()}）",
    )
    parser.add_argument("--demo", action="store_true", help="库为空时创建演示数据")
    parser.add_argument("--serve", action="store_true", help="启动时同时开启 Web 审阅端")
    parser.add_argument("--version", action="version", version=version_text(),
                        help="打印版本与构建信息后退出")
    return parser


def run(argv: list[str] | None = None) -> int:
    raw_args = argv if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw_args)
    library_root = Path(args.library).expanduser()
    # 显式指定了库（-L 或环境变量）就不打扰；否则按"上次用过的库 > 默认位置"解析
    explicit = bool(os.environ.get("TOOAK_LIBRARY") or os.environ.get("TOAK_LIBRARY")) \
        or any(arg in ("-L", "--library") for arg in raw_args)

    if args.demo:
        # 先在内核层把数据准备好，界面起来就有东西可看
        with create_demo_library(library_root) as library:
            print(f"演示库就绪：{library.root}")

    # 启动就打一行"这次跑的是哪个构建"：终端（或 tooak.sh 的日志）里能看到
    print(version_line(), flush=True)

    # 中文输入法：pip 装的 PySide6 不带输入法插件，得把系统 Qt 插件目录
    # 加进搜索路径（在创建 QApplication 之前做才生效）。
    _enable_system_input_method()

    app = QApplication([sys.argv[0]])
    # 整套观感（基础风格 + **钉死的浅色调色板** + 样式表）都在这一个入口里：
    # 而 Qt 6.5 起 Windows 的深色模式会给应用一套深色调色板。
    apply_theme(app)
    # 用户在运行期间切系统主题时再钉一次（否则切一次主题界面就变一次脸）
    watch_color_scheme(app)
    # 程序图标：用旧版 ToOak 的那个绿橡树图标
    app.setWindowIcon(app_icon())
    app.setApplicationName("ToOak")
    app.setApplicationDisplayName("ToOak 致橡树")
    app.setOrganizationName("ToOak")

    # 首次启动：没记过库、默认位置也没有库 → 引导选一个空间较大的位置
    first_run = False
    if not explicit and read_last_library() is None \
            and not (library_root / Library.DB_FILENAME).exists():
        first_run = True
        picked = _first_run_choose_library()
        if picked:
            library_root = picked
            write_last_library(picked)

    try:
        if first_run:
            # 没有库：新建一个演示库（橡树示例），让用户第一眼就有东西可看；
            # 有库（记住了或默认位置已存在）则走下面正常加载。
            create_demo_library(library_root).close()
        else:
            Library.open_or_create(library_root).close()
    except Exception as error:  # 库路径不可用时给出人话提示
        print(f"无法打开库 {library_root}：{error}", file=sys.stderr)
        return 2

    window = MainWindow(library_root)

    # 基于工作区（已排除任务栏）自适应大小 + 定位
    screen = QGuiApplication.primaryScreen()
    available = screen.availableGeometry()

    w = min(int(available.width() * 0.9), 1560)
    h = min(int(available.height() * 0.9), 960)
    x = available.x() + (available.width() - w) // 2
    y = available.y() + (available.height() - h) // 2

    window.resize(w, h)
    window.move(x, y)

    window.show()
    if first_run:
        _prompt_first_run_help(window)
    if args.serve:
        window.action_web.setChecked(True)
        window._toggle_web(True)
    return app.exec()


def _first_run_choose_library() -> Path | None:
    """首次启动的库位置引导：告诉用户没有库会新建演示库、有库会加载现有库，
    并建议选空间较大的磁盘（也可以就用默认位置）。"""
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    box = QMessageBox()
    box.setWindowTitle("欢迎使用 ToOak")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(
        "欢迎使用 ToOak 致橡树。\n\n"
        "这是你第一次打开，还没有库：ToOak 会为你**新建一个演示库**\n"
        "（用一棵橡树做例子，带你认识项目树、事件枝和年轮）。\n"
        "如果以后你已经有了库，ToOak 会直接加载它。\n\n"
        "请先选一个位置存放你的库（建议空间较大的磁盘，例如 D 盘）。"
    )
    pick_btn = box.addButton("选择位置…", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("用默认位置", QMessageBox.ButtonRole.DestructiveRole)
    box.setDefaultButton(pick_btn)
    box.exec()
    if box.clickedButton() is pick_btn:
        folder = QFileDialog.getExistingDirectory(
            None, "选择库所在目录（建议选空间较大的磁盘）", str(Path.home())
        )
        if folder:
            return Path(folder).expanduser()
    return None


def _prompt_first_run_help(window: "MainWindow") -> None:
    """首次使用：提示看一眼说明文档（项目树 / 事件枝 / 年轮那套）。"""
    from PySide6.QtWidgets import QMessageBox

    box = QMessageBox()
    box.setWindowTitle("开始使用 ToOak")
    box.setIcon(QMessageBox.Icon.Information)
    box.setText(
        "库已经就绪。\n\n"
        "建议先看一眼使用手册 ——\n"
        "项目是树、事件是枝、文件是果，每一稿是一圈年轮。"
    )
    view_btn = box.addButton("现在看说明", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("先跳过", QMessageBox.ButtonRole.RejectRole)
    box.exec()
    if box.clickedButton() is view_btn:
        window._open_help()


if __name__ == "__main__":
    raise SystemExit(run())

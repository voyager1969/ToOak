"""ToOak 主窗口：左侧两栏列表 + 右侧三种视图。

------------------------
::

    ┌──────────┬──────────┬──────────────────────────────┬────────────┐
    │ 项目     │ 事件     │  画布（底部：年轮窗口）      │            │
    │ 列表     │ 列表     │  · 项目树（可拖可连线）    │            │
    │          │          │  · 事件枝页（环节方框+文件卡） │            │
    │          │          │  · 年轮（时间轴）        │            │
    └──────────┴──────────┴──────────────────────────────┴────────────┘

* 点**项目** → 项目树：事件为节点，可拖动摆放、可从端口拖出连线画路径；
* 点**事件**（或双击版图上的卡片）→ 事件枝页：输入/分析/输出/参考/图示 五个方框；
* 把文件拖进方框 → 加进**当前这一稿**那个环节（不新建稿）；
* 把文件拖到**白板上的事件枝卡片**上 → 先问放进哪个环节，再加进那个事件枝的当前这一稿
  （侧栏**事件枝行**和白板**空白处**都不收文件，只给一句指路的话）；
* 把事件枝页里的**文件行拖到另一个方框** → 改这一行的环节（同一稿内，Ctrl+Z 可撤销）；
* 时间轴右边的「＋ 新增一稿」→ 立刻建一稿空的并切过去，之后的文件都进这一稿；
* 双击文件卡片 / 右键「看环节的年轮」→ 年轮视图；
* 右键文件卡片「加入到这一稿…」→ 选文件后加进当前这一稿。

导航（照旧版 ToOak/Symphytum 的视图工具栏）
------------------------------------------
* 视图上方有一条**面包屑**：`库 › 项目 › 事件 › 环节`，点哪一段就跳到那一层；
* 左边「◀ 返回」回到上一处（有历史走历史，没有就按层级往上退）；
* 右边是旧版那组「◀ 序号/总数 ▶」：在时间轴视图里走**上一稿/下一稿**，
  在版图/事件枝页里走**上一个/下一个事件枝** —— 按钮提示会写明这次会走到哪。

两种展示模式（工具栏「画布 / 时间轴」，选择记在库里）
----------------------------------------------------
* **画布**：事件枝页 = 可自己排版的方框（环节框 + 信息框 + 文件卡片）；
* **时间轴**：双击版图节点 / 点事件 = 该事件枝的版本链，箭头自动连起来，
  节点上写明每一稿的变更摘要；点某一稿，右侧就显示**这一稿改了什么**。

主窗口是**唯一写库的地方**：侧栏、各视图都只读库、只发信号，
真正落库的操作集中在这里，执行完统一刷新，避免"数据改了但某个视图还显示旧值"。
"""

from __future__ import annotations

import gc
import json
import queue
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import QDate, QEvent, QPoint, QProcess, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QMenu,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressDialog,
    QSplitter,
    QStackedWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import Library, LibraryError
from ..version import VERSION, version_text
from ..core.util import human_size
from ..core.library import DOC_STATE_LABELS, ROLE_LABELS
from ..core.projectgraph import (DEFAULT_GROUP_H, DEFAULT_GROUP_W,
                                 DEPENDENCY_KIND, build_project_graph)
from ..core.versiongraph import build_stage_graph, build_version_graph
from ..core.watcher import WATCHDOG_AVAILABLE, LibraryWatcher
from .event_view import (DEFAULT_UNITS_H, DEFAULT_UNITS_W, EventStageView,
                         is_image_name)
from .diff_dialog import DiffDialog
from .graph_view import VersionGraphView
from .project_view import (EDGE_ROUTE_CURVE, EDGE_ROUTE_ORTH, GRID_GAP,
                          ProjectBoardView)
from .sidebar import SidebarWidget
from .timeline_panel import TimelinePanel
from .canvas_toolbar import CanvasToolbar, EventToolbar
from .undo import UndoStack
from .widgets import ElidedLabel
from .zoom import ZoomControl, camera, restore_camera
from .paths import write_last_library
from ..resource import resource_path
from .style import (
    ACCENT as ACCENT_TEXT,
    DOC_STATE_COLORS,
    app_icon,
    arrow_left_icon,
    branch_pixmap,
    branch_plus_icon,
    chevron_left_icon,
    chevron_right_icon,
    folder_icon,
    import_icon,
    info_icon,
    logo_pixmap,
    panel_icon,
    scoped_style,
    state_icon,
    tree_plus_icon,
)
from .workers import (
    ExportFinished,
    ExportJob,
    ExportProgress,
    FileCopyFinished,
    FileCopyJob,
    WordCompareFinished,
    WordCompareJob,
    ZipFinished,
    ZipJob,
    ZipProgress,
    ArchiveFinished,
    ArchiveJob,
    ArchiveProgress,
    ImportFinished,
    ImportProgress,
    LegacyImportJob,
)

__all__ = ["MainWindow"]

#: 后台任务事件轮询间隔（毫秒）。工作线程通过队列回传结果，界面定时取走 ——
#: 这样工作线程完全不碰 Qt 对象，避开跨线程信号与对象生命周期的坑。
POLL_INTERVAL_MS = 80

#: 空闲时主动回收循环垃圾的间隔（秒）。
#: 为什么关掉自动循环 GC：实测在「工作线程跑 Python 代码 + 主线程触发 GC」时，
#: shiboken 遍历 Qt 包装对象会段错误。改为只在**确定没有后台任务**时手动 collect。
GC_IDLE_INTERVAL_S = 5.0

#: "连续微调"合并成一条撤销记录的时间窗（秒）。
#: 方向键每敲一下 = 一次移动，一次 5px —— 100 步的撤销栈几秒就被挤爆，
#: 想退回"这个节点原来在哪"要按几十下 Ctrl+Z。1 秒内的同一批卡片移动视为一串。
MOVE_MERGE_WINDOW_S = 1.0

#: 关窗时各后台任务最多等多久（秒）。分开定是因为它们的量级差得远：
#: 归档 / 导出 / 打包是磁盘活（几十秒量级），旧库迁移要读几千条记录。
SHUTDOWN_JOB_TIMEOUTS = {
    "archive": 15.0,
    "import": 60.0,
    "export": 30.0,
    "zip": 30.0,
    # Word 比较：Word 起来 + 比完大文档可能要几分钟，关窗时给它 90 秒
    # （到点就让它自己收尾：进程是 daemon，Word 那边本来也不会留半份文件）
    "word": 90.0,
    # 复制一份（库里新建实体文件）：复制 + 算指纹，GB 级图纸要几十秒
    "copy": 120.0,
}

#: 关窗等待时分片的粒度（秒）。每片之间跑一次事件循环，界面才不会假死。
SHUTDOWN_SLICE_S = 0.05

#: 拖文件到事件枝卡片上时，可选的环节（顺序照事件枝页方框的习惯排：先常用的一圈，
#: 最后才是收杂项的"说明 / 附件"）。值必须是 ``ROLE_LABELS`` 认识的角色 ——
#: 内核的 ``move_file_to_role`` / 归档都只认这几个。
STAGE_DROP_ROLES = ["reference", "input", "analysis", "output", "figure",
                    "note", "attachment"]


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self, library_root: str | Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.library_root = Path(library_root)
        self.library = Library.open(self.library_root)

        self._project_id: int | None = None
        self._document_id: int | None = None
        self._version_id: int | None = None
        self._stage_role: str | None = None
        self._archive_jobs: dict[int, ArchiveJob] = {}
        self._export_jobs: dict[int, ExportJob] = {}
        #: 画布上的撤销/重做（挪位置、删卡片、剪断连线…都能退回来）
        self.undo_stack = UndoStack()
        self.undo_stack.changed.connect(self._sync_undo_actions)
        self.undo_stack.changed.connect(self._sync_toolbar_undo)
        self._zip_jobs: dict[int, ZipJob] = {}
        self._import_jobs: dict[int, LegacyImportJob] = {}
        #: 「用 Word 比较」的后台任务（Word 启动要十几秒，不能占着界面线程）
        self._word_jobs: dict[int, WordCompareJob] = {}
        #: 「复制一份到这一稿」的后台任务（GB 级文件复制 + 算指纹要几十秒）
        self._copy_jobs: dict[int, FileCopyJob] = {}
        self._job_requests: dict[int, dict] = {}
        self._pending_archives: list[dict] = []
        #: 进度框**按 job id 存**：以前共用一个 `self._progress_dialog`，
        #: 两个任务叠加时先结束的那个会把别人的框一起关掉
        self._progress_dialogs: dict[int, QProgressDialog] = {}
        #: "连续微调合并成一条撤销记录"用的状态（见 _record_move）
        self._move_burst: dict | None = None
        #: 正在关窗：这时库已经/即将关闭，界面上的迟到的定时器不该再查库
        self._shutting_down = False
        self._watcher: LibraryWatcher | None = None
        self._watch_events: queue.Queue = queue.Queue()
        self._poll_timer: QTimer | None = None
        self._http_server = None
        self._http_thread: threading.Thread | None = None
        self._project_layout = "grid"
        #: 打开的"这一稿改了什么"窗口（关掉时移除，避免越攒越多）
        self._diff_dialogs: list[DiffDialog] = []
        self._last_gc = time.monotonic()
        #: 「返回」用的历史（记录"来之前在看什么"），一层层往回退
        self._nav_history: list[dict] = []
        #: 正在按历史恢复视图：这时不要再往历史里记一笔
        self._restoring = False

        # 标题只显示「ToOak 致橡树 版本号」（用户定的）；提交号仍打在状态栏
        # 右下角与启动日志里，排查"开的是哪次构建"照样有据可查。
        self.setWindowTitle(f"ToOak 致橡树 v{VERSION}")
        self.setWindowIcon(app_icon())
        self.resize(1560, 960)
        #: 是否允许弹模态对话框。测试与批处理置 False，
        #: 否则无人点击的模态框会把流程永远挂住。
        self.interactive = True
        #: 上次拖文件到事件枝卡片上时选的环节（下次问的时候默认选中它，见 _ask_stage_role）
        self._last_stage_role: str | None = None

        # 一次性数据归一：事件级"说明"并进年轮的说明（幂等，做完就没得做了）
        try:
            self.library.consolidate_version_notes()
        except Exception:
            pass

        self._build_ui()
        self._layout_save_timer = QTimer(self)
        self._layout_save_timer.setSingleShot(True)
        self._layout_save_timer.setInterval(400)
        self._layout_save_timer.timeout.connect(self.save_layout_options)
        self._rail_sizes: list[int] | None = None
        self._restore_layout_options()
        self._watch_layout_changes()
        self._build_toolbar()
        self._connect()
        self._update_status()
        # resume=True：先回到上次关窗时看的那一处（找不到就按老规矩开第一个项目）
        self._auto_select_first(resume=True)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.poll_jobs)
        self._poll_timer.start()

        # 关掉自动循环 GC（原因见 GC_IDLE_INTERVAL_S）：改为空闲时手动回收
        gc.disable()

    # ------------------------------------------------------------------ 构建

    def _build_ui(self) -> None:
        """布局：左边「项目 / 事件」两栏，中间画布，右边详情。

        左（列表）— 中（画布）— 右（这一稿的版本/差异）。
        从文件管理器往窗口里拖文件时，可以用工具栏「侧栏」把左右两栏都收起来，
        画布就占满整窗（拖放面积最大）。
        """
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        splitter.setChildrenCollapsible(False)
        self.main_splitter = splitter
        # 拉伸因子：多的宽度给画布（索引 1），侧栏保持用户拖出来的宽度
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        # --- 左：项目 / 事件 两栏 + 搜索
        self.browse_column = QWidget(splitter)
        browse_layout = QVBoxLayout(self.browse_column)
        browse_layout.setContentsMargins(8, 8, 4, 8)
        browse_layout.setSpacing(6)
        self.search_edit = QLineEdit(self.browse_column)
        self.search_edit.setPlaceholderText("搜索事件枝标题 / 文件名…")
        self.search_edit.setClearButtonEnabled(True)
        browse_layout.addWidget(self.search_edit)
        self.sidebar = SidebarWidget(self.library_root, self.browse_column)
        browse_layout.addWidget(self.sidebar, 1)
        # 侧栏这一列的宽度**由用户拖出来的分隔条决定**，不跟着内容变：
        # 给它一个下限（别被拖没），多余空间全部给右边的画布
        self.browse_column.setMinimumWidth(200)
        splitter.addWidget(self.browse_column)

        # --- 中：三种视图共用一个位置
        center = QWidget(splitter)
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)

        # 视图上方这条：左边「返回 + 面包屑」，右边「上一个 / 序号 / 下一个」
        # （照旧版 ViewToolBarWidget：导航组在右边、视图模式按钮在中段）
        header = QWidget(center)
        header.setObjectName("headerBar")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 6, 14, 6)
        header_layout.setSpacing(10)

        self.back_button = QToolButton(header)
        self.back_button.setText("返回")
        self.back_button.setIcon(arrow_left_icon(16))
        self.back_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.back_button.setToolTip("返回上一处（Alt+← / Alt+↑）")
        self.back_button.clicked.connect(self.go_back)
        # 应用标识：旧版 ToOak 图标 + 名字 + 一小段橡树枝条装饰
        brand = QWidget(header)
        brand_layout = QHBoxLayout(brand)
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(6)
        self.brand_icon = QLabel(brand)
        self.brand_icon.setPixmap(logo_pixmap(22))
        self.brand_icon.setToolTip("ToOak —— 资料管理器 · 设计 mapletinge")
        brand_layout.addWidget(self.brand_icon)
        self.brand_name = QLabel("ToOak", brand)
        scoped_style(
            self.brand_name,
            f"color:{ACCENT_TEXT};font-size:12pt;font-weight:600;letter-spacing:1px;",
        )
        brand_layout.addWidget(self.brand_name)
        head_branch = QLabel(brand)
        head_branch.setPixmap(branch_pixmap(96, 18))
        head_branch.setToolTip("橡树枝条：ToOak 图标的同一套语言")
        brand_layout.addWidget(head_branch)
        header_layout.addWidget(brand)

        header_layout.addWidget(self.back_button)

        self.crumb_bar = QWidget(header)
        self.crumb_layout = QHBoxLayout(self.crumb_bar)
        self.crumb_layout.setContentsMargins(0, 0, 0, 0)
        self.crumb_layout.setSpacing(4)
        header_layout.addWidget(self.crumb_bar)

        # 事件状态的小胶囊：点一下就切（进行中/已完成/搁置）——
        self.state_button = QToolButton(header)
        self.state_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.state_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.state_button.setToolTip(
            "这个事件枝的状态（个人进度，不是审批）：\n"
            "进行中＝绿叶 · 已完成＝棕黄橡果 · 搁置＝黄色落叶"
        )
        self.state_menu = QMenu(self.state_button)
        self.state_button.setMenu(self.state_menu)
        self.state_button.hide()
        header_layout.addWidget(self.state_button)

        # 这两个都用 ElidedLabel：文字再长也**不许撑宽中栏**
        # （普通 QLabel 会把布局的最小宽度顶大，QSplitter 只好挤压侧栏 ——
        self.view_title = ElidedLabel("项目树")
        self.view_title.setProperty("role", "title")
        self.view_title.hide()          # 标题已经由面包屑表达，留着是为了兼容旧代码
        self.view_subtitle = ElidedLabel("")
        self.view_subtitle.setProperty("role", "hint")
        header_layout.addWidget(self.view_subtitle)
        header_layout.addStretch(1)

        line = QFrame(header)
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        header_layout.addWidget(line)

        self.nav_label = QLabel("0/0", header)
        self.nav_label.setProperty("role", "hint")
        self.nav_label.setMinimumWidth(74)
        self.nav_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(self.nav_label)

        self.nav_prev = QToolButton(header)
        self.nav_prev.setIcon(chevron_left_icon(16))
        self.nav_prev.setShortcut(QKeySequence("Ctrl+Left"))
        self.nav_prev.clicked.connect(self.go_previous)
        header_layout.addWidget(self.nav_prev)

        self.nav_next = QToolButton(header)
        self.nav_next.setIcon(chevron_right_icon(16))
        self.nav_next.setShortcut(QKeySequence("Ctrl+Right"))
        self.nav_next.clicked.connect(self.go_next)
        header_layout.addWidget(self.nav_next)

        center_layout.addWidget(header)

        self.view_stack = QStackedWidget(center)
        self.board_view = ProjectBoardView(self.view_stack)
        self.event_view = EventStageView(self.view_stack)
        self.graph_view = VersionGraphView(self.view_stack)
        # 事件枝页 = 画布 + 底部"年轮"窗口（照 CLion 的 Git 窗口：可拖分隔条收放）
        self.event_page = QWidget(self.view_stack)
        event_layout = QVBoxLayout(self.event_page)
        event_layout.setContentsMargins(0, 0, 0, 0)
        event_layout.setSpacing(0)
        self.timeline_panel = TimelinePanel(self.library_root, self.event_page)
        self.event_view.attach_timeline_panel(self.timeline_panel)
        self.event_splitter = QSplitter(Qt.Orientation.Vertical, self.event_page)
        self.event_splitter.setChildrenCollapsible(False)
        self.event_splitter.addWidget(self.event_view)
        self.event_splitter.addWidget(self.timeline_panel)
        self.event_splitter.setStretchFactor(0, 1)
        self.event_splitter.setSizes([560, 280])
        event_layout.addWidget(self.event_splitter)

        self.view_stack.addWidget(self.board_view)
        self.view_stack.addWidget(self.event_page)
        self.view_stack.addWidget(self.graph_view)
        center_layout.addWidget(self.view_stack, 1)

        # 画布**右上角**的缩放控件（Ctrl+滚轮缩放的"同一件事"，只是鼠标也能点）
        self.zoom_control = ZoomControl(center)
        self.zoom_control.fitRequested.connect(self._active_view_fit)
        self.zoom_control.raise_()

        # 画布**底部中间**的浮动工具栏（照 Obsidian 白板那套）：
        # 新事件枝 / 说明框 / 分组 / 导出 / 适应 —— 点一下在中央建，拖到画布上就建在落点
        self.canvas_toolbar = CanvasToolbar(center)
        self.canvas_toolbar.createRequested.connect(self._create_item_from_toolbar)
        self.canvas_toolbar.groupRequested.connect(self._group_from_selection)
        self.canvas_toolbar.exportRequested.connect(self._export_from_toolbar)
        self.canvas_toolbar.fitRequested.connect(self._active_view_fit)
        self.canvas_toolbar.undoRequested.connect(self.undo)
        self.canvas_toolbar.redoRequested.connect(self.redo)
        self.canvas_toolbar.raise_()

        # 事件枝页自己的工具栏（只在事件枝页出现）：布局锁 / 整理排版 / 适应
        self.event_toolbar = EventToolbar(center)
        self.event_toolbar.editLayoutToggled.connect(self.set_edit_layout)
        self.event_toolbar.arrangeRequested.connect(self._reset_event_layout)
        self.event_toolbar.fitRequested.connect(self._active_view_fit)
        self.event_toolbar.raise_()

        self._center_panel = center
        center.installEventFilter(self)
        self._sync_zoom_control()

        splitter.addWidget(center)

        # 右侧栏已经去掉：版本在画布底侧的时间轴上切、改动摘要就在卡片上，
        # 想看整篇差异在时间轴卡片右键「看这一稿改了什么…」。
        splitter.setSizes([330, 1200])
        splitter.setStretchFactor(1, 1)      # 画布吃掉多余宽度
        self.setCentralWidget(splitter)

        # --- 状态栏那两个标签**只建一次**（文字在 _update_status 里改）
        # 为什么强调"只建一次"：以前 _update_status 每次都 addWidget 一个新的 QLabel，
        # 旧的不删 —— 每写一次库状态栏就多一条重复文字，widget 只增不减
        # （程序里循环 GC 是关掉的，那些旧标签连被回收的机会都没有）。
        self.status_label = QLabel("", self)
        self.statusBar().addWidget(self.status_label)
        self.watch_label = QLabel("", self)
        self.statusBar().addPermanentWidget(self.watch_label)
        #: 右下角常驻的版本号（permanent 区：临时提示不会把它顶掉）。
        #: 带提交号与"有没有未提交改动"——改完代码没重启、或开到了另一份旧目录，
        self.version_label = QLabel(version_text(), self)
        self.version_label.setToolTip(
            "当前跑的构建：版本 · 提交号 · 提交日期 · 有无未提交改动"
        )
        scoped_style(self.version_label, "color:#8a94a0;font-size:10px;")
        self.statusBar().addPermanentWidget(self.version_label)

    def _make_toolbar_action(self, text: str, slot, icon=None,
                             tip: str = "") -> QAction:
        """造一个"跟工具栏按钮同款"的动作（图标 / 状态栏提示 / tooltip / 触发）。

        单独抽出来的原因：侧栏按钮条上那三个动作（新建项目树 / 新建事件枝 / 导入文件…）
        行为一样。共用这一个工厂函数，两边就不会长出两套图标和两套提示语。
        """
        action = QAction(text, self)
        if icon is not None:
            action.setIcon(icon)
        if tip:
            action.setStatusTip(tip)
            action.setToolTip(tip)
        action.triggered.connect(slot)
        return action

    def _build_toolbar(self) -> None:
        bar = QToolBar("主工具栏", self)
        bar.setMovable(False)
        bar.setIconSize(QSize(16, 16))

        def add(text: str, slot, icon=None, tip: str = "") -> QAction:
            action = self._make_toolbar_action(text, slot, icon, tip)
            bar.addAction(action)
            return action

        # 「新建项目树 / 新建事件枝 / 导入文件…」不再挂在这个工具栏上，而是挂到**侧栏**
        # 建议挪到侧边栏"）。动作对象仍然建在这里、属性名不变（测试与别处都在用），
        # 只是**加进侧栏按钮条**而不是加进 toolbar —— 一套动作，两个地方都找不到第二份。
        self.action_new_project = self._make_toolbar_action(
            "新建项目树", self._new_project_dialog,
            tree_plus_icon(16),
            "新建一个项目树（一个工程 / 一个课题）")
        self.action_new_event = self._make_toolbar_action(
            "新建事件枝", self._new_event_dialog,
            branch_plus_icon(16),
            "在当前项目下新建一个事件枝（一件要做的事）")
        self.action_import_files = self._make_toolbar_action(
            "导入文件…", self._import_files, import_icon(16),
            "把文件归档到当前事件枝；也可以直接把文件拖到事件枝页的方框里")
        self.sidebar.set_actions([self.action_new_project, self.action_new_event,
                                  self.action_import_files])

        # 意义确实不大：界面在库被改动的地方都会自己重读一遍 ——
        # 归档/导入结束、撤销重做、目录监视回调、改名删除、换库…… 都直接调
        # `refresh_all()`（这个函数留着，见它自己的说明），用户没有"必须手动刷"的时候。
        # 顶栏那个按钮的入口没了；窗口级的 Ctrl+R / F5 快捷键还留着（见下面
        # `refresh_action`）：它不占地方，而"另一个窗口动过同一个库"这种极端情况
        # 还留一条手动重读的路。

        # 缩放不放工具栏按钮：Ctrl+滚轮缩放，比例控件在画布右下角（zoom.ZoomControl）

        self.action_rail = QAction("侧栏", self)
        self.action_rail.setCheckable(True)
        # 侧栏默认可见；真正状态由 _restore_layout_options 同步。不能写
        # `browse_column.isVisible()`—— 此时窗口还没 show()，isVisible() 恒为 False，
        self.action_rail.setChecked(True)
        self.action_rail.setIcon(panel_icon(16))
        self.action_rail.setToolTip(
            "显示 / 隐藏左侧的项目·事件枝列表。\n"
            "隐藏后画布占满整个窗口 —— 从文件管理器往这边拖文件时最方便。"
        )
        self.action_rail.triggered.connect(self.toggle_side_panels)
        bar.addAction(self.action_rail)

        # 白板专属动作（自由网格 / 依赖分层 / 重置位置）都搬到**白板自己的工具栏**上，
        # 事件枝页专属的（编辑布局 / 整理排版）搬到**事件枝页工具栏**上 ——
        self.action_layout_map = QAction("自由网格", self)
        self.action_layout_map.setCheckable(True)
        self.action_layout_map.setChecked(True)
        self.action_layout_map.setToolTip(
            "白板：节点先按网格自动排开，之后你把它们拖到哪儿就留在哪儿（位置自动保存）"
        )
        self.action_layout_layered = QAction("依赖分层", self)
        self.action_layout_layered.setCheckable(True)
        self.action_layout_layered.setToolTip("按连线的方向分层：被指向的排在最左")
        self.layout_group = QActionGroup(self)
        self.layout_group.setExclusive(True)
        for action, mode in ((self.action_layout_map, "grid"),
                             (self.action_layout_layered, "layered")):
            self.layout_group.addAction(action)
            action.triggered.connect(lambda _checked, m=mode: self._set_project_layout(m))
        self.action_layout_reset = QAction("重置位置…", self)
        self.action_layout_reset.setToolTip(
            "把本项目所有节点摆好的位置清掉、回到自动布局（会先问一句）"
        )
        self.action_layout_reset.triggered.connect(self._reset_project_layout)
        self.action_arrange_grid = QAction("网格排列…", self)
        self.action_arrange_grid.setToolTip(
            "把选中的事件排成几行几列（也可以右键点分组框，排组里那些事件）"
        )
        self.action_arrange_grid.triggered.connect(lambda: self._arrange_grid())
        self.action_edge_orth = QAction("连线走直角", self)
        self.action_edge_orth.setCheckable(True)
        # 界面恢复时这个动作还没建，所以勾选状态按画布当前的走向来
        self.action_edge_orth.setChecked(self.board_view.edge_route == EDGE_ROUTE_ORTH)
        self.action_edge_orth.setToolTip(
            "勾上：连线走直角折线（拐弯处是小圆角）；不勾：原来那种枝叶弧线。\n"
            "两种都能拖线中间的小圆点调整位置"
        )
        self.action_edge_orth.toggled.connect(self._set_edge_route)
        self.action_edit_layout = QAction("编辑布局", self)
        self.action_edit_layout.setCheckable(True)
        self.action_edit_layout.setChecked(False)
        self.action_edit_layout.setToolTip(
            "事件枝页的方框现在是锁住的（点事件枝页底部的锁按钮可以解锁编辑）"
        )
        self.action_edit_layout.toggled.connect(self.set_edit_layout)

        # 只是界面上不摆：内核与数据全在 —— watch_dirs 表、LibraryWatcher、
        # tooak/core/watcher.py、`tooak watch` 子命令、相关测试一个都没删。
        # 接回来就一行：`bar.addAction(self.action_watch)`（动作对象、勾选状态、
        # 状态栏 `watch_label` 的接线都原样留着，_toggle_watcher 也还能用）。
        self.action_watch = QAction("开始目录监视", self)
        self.action_watch.setCheckable(True)
        self.action_watch.setToolTip(
            "监视已登记的目录：往里丢文件就自动归档；文件被改动则自动产生新版本\n"
            "（当前版本暂不提供入口，功能本身完好）"
        )
        self.action_watch.triggered.connect(self._toggle_watcher)

        # 一定是默认开启的，不然就失去了这个软件的意义，这个按钮删除，默认逻辑就是进库"）。
        # 现在"进库"是**库自己的默认设置**（Library.file_mode 默认 copy，见那里），
        # 不再由界面上的一个勾选框临时决定 —— 一个能被误点掉的全局开关，
        # 比"默认就进库"危险得多：用户换台机器/重开一次程序就会得到两套行为。
        # 想改某个库的纳管方式：`Library.set_file_mode()` 或 `tooak file-mode <模式>`。

        self.action_web = QAction("Web 查看端", self)
        self.action_web.setCheckable(True)
        self.action_web.setToolTip("在本机启动 Web 查看端，供他人在浏览器里浏览与看说明")
        self.action_web.triggered.connect(self._toggle_web)
        # 动作对象与 --serve 都还在，接回来就一行：`bar.addAction(self.action_web)`。

        bar.addSeparator()

        add("打开库…", self._open_library_dialog, folder_icon(16),
            "选择另一个库所在目录并打开（会记住这个位置，下次启动还开它）")
        add("说明", self._open_help, info_icon(16),
            "打开使用说明（PDF）—— 项目是树、事件是枝、文件是果、每一稿是一圈年轮")
        # _import_legacy / start_legacy_import / tooak legacy-import 都还在，接回来就一行。

        self.addToolBar(bar)

        # 白板专属动作（自由网格 / 依赖分层 / 重置位置）收进白板工具栏的「布局 ▾」。
        # 放在这里是因为：工具栏的QAction 到这一步才建好（_build_ui 更早）
        board_layout_menu = QMenu(self)
        board_layout_menu.addAction(self.action_layout_map)
        board_layout_menu.addAction(self.action_layout_layered)
        board_layout_menu.addSeparator()
        board_layout_menu.addAction(self.action_arrange_grid)
        board_layout_menu.addAction(self.action_edge_orth)
        board_layout_menu.addSeparator()
        board_layout_menu.addAction(self.action_layout_reset)
        self.canvas_toolbar.add_layout_menu(board_layout_menu)

        # 留着它的理由：它不占工具栏位置，而"别的程序/窗口动过同一个库"这类
        # 边角情况还留一条出口；日常用不到 —— 归档、撤销、监视回调都会自己刷新。
        refresh_action = QAction("刷新", self)
        refresh_action.setShortcut(QKeySequence.StandardKey.Refresh)   # F5 / Ctrl+R
        refresh_action.triggered.connect(self.refresh_all)
        self.action_refresh = refresh_action
        self.addAction(refresh_action)

        # 返回上一处：Alt+← / Alt+↑（照旧版用 Ctrl+←/→ 走上一个/下一个）
        fit_action = QAction("适应窗口", self)
        fit_action.setShortcut(QKeySequence("Ctrl+0"))
        fit_action.triggered.connect(self._active_view_fit)
        self.addAction(fit_action)

        undo_action = QAction("撤销", self)
        undo_action.setShortcut(QKeySequence.StandardKey.Undo)          # Ctrl+Z
        undo_action.triggered.connect(self.undo)
        self.addAction(undo_action)
        redo_action = QAction("重做", self)
        redo_action.setShortcuts([QKeySequence.StandardKey.Redo,        # Ctrl+Shift+Z
                                  QKeySequence("Ctrl+Y")])
        redo_action.triggered.connect(self.redo)
        self.addAction(redo_action)
        self.action_undo, self.action_redo = undo_action, redo_action

        zoom_in_action = QAction("放大", self)
        zoom_in_action.setShortcut(QKeySequence.StandardKey.ZoomIn)
        zoom_in_action.triggered.connect(lambda: self._active_view().zoom_in())
        self.addAction(zoom_in_action)

        zoom_out_action = QAction("缩小", self)
        zoom_out_action.setShortcut(QKeySequence.StandardKey.ZoomOut)
        zoom_out_action.triggered.connect(lambda: self._active_view().zoom_out())
        self.addAction(zoom_out_action)

        back_action = QAction("返回", self)
        back_action.setShortcuts([QKeySequence("Alt+Left"), QKeySequence("Alt+Up")])
        back_action.triggered.connect(self.go_back)
        self.addAction(back_action)

    def _connect(self) -> None:
        # 侧栏
        self.sidebar.projectSelected.connect(self.show_project_board)
        self.sidebar.eventSelected.connect(self.open_event)
        self.sidebar.archiveRequested.connect(self._on_archive_requested)
        self.sidebar.statusMessage.connect(self.show_message)
        self.sidebar.libraryChanged.connect(self.refresh_all)
        self.sidebar.stateRequested.connect(self._set_event_state)
        self.sidebar.renameRequested.connect(self.rename_event)
        self.sidebar.deleteRequested.connect(self.delete_event)
        self.sidebar.newEventRequested.connect(self._new_event_in_project)

        # 项目树
        self.board_view.documentSelected.connect(self._on_board_document_selected)
        self.board_view.documentActivated.connect(self.open_event)
        self.board_view.positionChanged.connect(self._save_position)
        self.board_view.positionsChanged.connect(self._save_positions)
        self.board_view.noteRequested.connect(self._add_board_note)
        self.board_view.noteChanged.connect(self._rename_board_note)
        self.board_view.noteMoved.connect(self._move_board_note)
        self.board_view.noteRemoveRequested.connect(self._delete_board_note)
        self.board_view.groupRequested.connect(self._create_board_group)
        self.board_view.groupRenameRequested.connect(self._rename_board_group)
        self.board_view.groupDeleteRequested.connect(self._delete_board_group)
        self.board_view.groupExportRequested.connect(self._export_board_group)
        self.board_view.groupMembershipRequested.connect(self._set_group_membership)
        self.board_view.edgeCurveRequested.connect(self._set_edge_curve)
        self.board_view.exportSelectionRequested.connect(self._export_selection)
        self.board_view.createItemRequested.connect(self._on_canvas_create_requested)
        self.board_view.filesDropped.connect(self._on_board_files_dropped)
        self.board_view.deleteSelectionRequested.connect(self._delete_board_selection)
        self.board_view.newEventRequested.connect(self.create_event_at)
        self.board_view.cardPromoteRequested.connect(self._promote_card)
        self.board_view.cardOpenRequested.connect(self._open_card_file)
        self.board_view.cardFolderRequested.connect(self._open_card_folder)
        self.board_view.cardLinkRequested.connect(self._open_card_link)
        self.board_view.cardRelinkRequested.connect(self._relink_card)
        self.board_view.groupRectChanged.connect(self._save_group_rect)
        self.board_view.emptyGroupRequested.connect(self._create_empty_group)
        self.board_view.connectionRequested.connect(self._connect_events)
        self.board_view.edgeRemoveRequested.connect(self._disconnect_events)
        self.board_view.statusMessage.connect(self.show_message)
        self.board_view.gridArrangeRequested.connect(self._arrange_grid)
        self.board_view.stateRequested.connect(self._set_event_state)
        self.board_view.renameRequested.connect(self.rename_event)
        self.board_view.deleteRequested.connect(self.delete_event)

        # 事件枝页
        self.event_view.filesDropped.connect(self._on_stage_drop)
        # 文件行从一个方框拖到另一个方框 = 改这一行的环节（同一稿内，不新建稿）
        self.event_view.fileRoleChangeRequested.connect(self._move_file_role)
        self.event_view.newVersionRequested.connect(self._add_new_version)
        self.event_view.stageFlowRequested.connect(self.open_stage_flow)
        self.event_view.rebindRequested.connect(self._rebind_version_files)
        self.event_view.statusMessage.connect(self.show_message)
        # 文件卡片：打开 / 导出 / 打开所在目录 / 打开原始目录 / 删除 / 添加
        self.event_view.set_file_provider(self._stage_one_file)
        # 列表行显示图片缩略图要用"文件真实路径"（只读，不复制文件）
        self.event_view.set_file_path_provider(
            lambda version_id, file_id: self.library.blob_path(version_id, file_id)
        )
        self.event_view.fileOpenRequested.connect(self._open_one_file)
        self.event_view.fileExportRequested.connect(self._export_one_file)
        self.event_view.filesExportRequested.connect(self._export_files)
        self.event_view.fileFolderRequested.connect(self._open_file_folder)
        self.event_view.fileOriginFolderRequested.connect(self._open_origin_folder)
        self.event_view.filesDeleteRequested.connect(self._delete_files)
        self.event_view.addFilesRequested.connect(self._add_files_to_stage)
        self.event_view.openRequested.connect(self._open_one_file)
        # 右键文件 →「用 Word 比较」：菜单要知道"能和哪些更早的稿比"，
        # 而那件事要查库（本机有没有 Word、配上哪一份、哪份已经不在原处）
        self.event_view.set_word_compare_provider(self.word_compare_info)
        self.event_view.fileWordCompareRequested.connect(self._word_compare)
        # 「未登记」的行右键 → 复制一份到某一稿：菜单要写出稿号，所以问一句
        # "正在看的是哪一稿、最新的是哪一稿"（两者常常不是同一稿）
        self.event_view.set_version_targets_provider(self.version_targets)
        self.event_view.fileRegisterRequested.connect(self._register_file_to_version)
        # 信息框里直接改的东西（时间 / 相关人 / 进度 / 状态 / 说明）
        self.event_view.fieldChanged.connect(self._on_event_field_changed)
        # 方框自己排版：位置/大小按事件存库；"统一大小"记成默认值；「整理排版」清掉手工排版
        self.event_view.layoutChanged.connect(self._save_event_layout)
        self.event_view.uniformSizeChanged.connect(self._save_uniform_size)
        self.event_view.resetLayoutRequested.connect(self._reset_event_layout)
        # 底侧时间轴：点某一稿 → 画布切到那一稿（默认是最新稿）
        self.timeline_panel.versionSelected.connect(self.show_event_version)
        self.timeline_panel.newVersionRequested.connect(self.add_event_version)
        self.timeline_panel.deleteRequested.connect(self.delete_event_version)

        # 年轮
        self.graph_view.versionSelected.connect(self._on_graph_version_selected)
        self.graph_view.versionActivated.connect(self._open_version_file)
        self.graph_view.set_stage_provider(self._stage_files)


        self.search_edit.textChanged.connect(self._on_search)
        self.search_edit.returnPressed.connect(self._focus_first_hit)

    def _update_status(self) -> None:
        """刷新状态栏上的库统计与监视状态（**只改文字，不新建控件**）。"""
        stats = self.library.stats()
        counts = stats["counts"]
        self.status_label.setText(
            f"库：{stats['root']}    |    项目树 {counts['projects']} · 事件 {counts['documents']}"
            f" · 版本 {counts['versions']} · 引用文件 {counts.get('referenced_files', 0)}"
        )
        # 目录监视的**入口**暂时收起来了（见 `_build_toolbar`）：没在跑就什么都不写。
        # 以前这里恒定显示"监视：未启动 / 未安装 watchdog" —— 功能连入口都没有了，
        # 状态栏还挂着这么一句只会让人以为哪儿没配好。真的跑起来（将来把入口接回来，
        # 或者别处启动）才显示规则条数。
        if self._watcher is not None:
            self.watch_label.setText(f"监视：{len(self._watcher.rules)} 条规则")
        else:
            self.watch_label.clear()

    def _auto_select_first(self, *, resume: bool = False) -> None:
        """打开库之后总得先显示点东西。

        :param resume: 启动时用 —— 先试着回到上次关窗时看的那一处
            （见 :meth:`_restore_last_session`），恢复不了才打开第一个项目。
            从旧库导入完成时**不传**：那会儿用户要看的显然是刚导进来的库，
            不是他上次退出前待着的那个项目。
        """
        projects = self.library.list_projects()
        if not projects:
            self.view_title.setText("项目树")
            self.view_subtitle.setText("库还是空的 —— 先点「新建项目树」")
            self.board_view.set_graph(None)
            self.view_stack.setCurrentWidget(self.board_view)
            return
        if resume and self._restore_last_session(projects):
            return
        self.show_project_board(projects[0].id)

    # ---------------------------------------------- 下次打开接着看上次那一处

    #: "上次看的是哪个项目"存库的键（关窗时写一次）
    SESSION_PROJECT_KEY = "session.project_id"
    #: "上次看的是哪一处视图"存库的键（JSON，内容就是 _current_view_state）
    SESSION_VIEW_KEY = "session.view"

    def save_session_state(self) -> None:
        """记住"下次打开接着看哪儿"：上次的项目 + 具体那一处（版图/事件/环节）。

        存 id 而不是名字：项目改名、事件改名之后照样对得上。
        跟着库存（和界面尺寸一样），所以换机器、重启都还在。
        写失败一律咽掉：关窗时库可能已经不可写了，而"记住上次位置"
        是锦上添花的事，绝不能因此打断关窗。
        """
        try:
            if self._project_id:
                self.library.set_ui_option(
                    self.SESSION_PROJECT_KEY, str(int(self._project_id))
                )
            state = self._current_view_state()
            # 空库/没恢复出视图时写空串，免得下次开机拿着一条过期记录去恢复
            self.library.set_ui_option(
                self.SESSION_VIEW_KEY, json.dumps(state) if state else ""
            )
        except Exception:
            pass

    def _restore_last_session(self, projects: list) -> bool:
        """启动时回到上次关窗时看的那一处。返回是否真的恢复上了。

        容错是这里的主旋律（库被换掉、项目被删、事件被归档都可能发生），
        对不上就平稳退让，绝不因为一条过期记录让窗口开不出来：

        * 事件被归档/删了 → 退回**它那个项目的版图**（见 ``_fallback_to_board``）；
        * 项目不在了 → 再退回"上次记的那个项目"，都不可用才返回 ``False``
          让调用方打开库里的第一个项目；
        * 环节里已经没文件（``open_stage_flow`` 会拒绝显示）→ 退回事件的画布。

        恢复**不记导航历史**（"返回"的语义是"回到我刚才来的地方"）：
        开机第一下按「返回」不该退到一个用户从没看过的地方。
        这里没走 ``_restore_view``，因为那一个只管"按历史退"，不做这些校验。
        """
        known = {project.id for project in projects}
        state = self._saved_view_state()
        if not isinstance(state, dict):
            # 视图没记下来（老库、或者上次没走到写这一步）：至少把项目恢复上
            return self._fallback_to_board(0, known)

        kind = state.get("kind")
        if kind in ("event", "stage"):
            document_id = self._as_int(state.get("document_id"))
            document = self.library.get_document(document_id) if document_id else None
            node = self.library.get_node(document.node_id) if document else None
            project_id = node.project_id if node is not None else 0
            # 归档（软删）掉的事件枝、或者它那个项目都没了：不恢复事件枝页，改退版图
            if (document is None or document.status != "active"
                    or project_id not in known):
                return self._fallback_to_board(project_id, known)
            if kind == "stage":
                self.open_stage_flow(document_id, state.get("role"),
                                     self._as_int(state.get("version_id")) or None)
                # open_stage_flow 只在真的显示出来时才切到流程视图：
                # 那个环节已经没文件了 → 退回这个事件枝的画布（别把空白页留给用户）
                if self.view_stack.currentWidget() is self.graph_view:
                    return True
            if state.get("show") == "timeline":
                self._open_document(document_id, self._as_int(state.get("version_id"))
                                    or None)
            else:
                self._open_event_canvas(document_id)
            return True

        project_id = self._as_int(state.get("project_id"))
        if project_id in known:
            self.show_project_board(project_id)
            return True
        return False

    def _fallback_to_board(self, project_id: int, known: set) -> bool:
        """退不了那一页就退到版图：优先这个事件枝所属的项目，其次上次记的项目。

        为什么不直接开"库里的第一个项目"：排序第一个的项目很可能跟用户上次
        在看的东西毫不相干（换过库、或者他压根就没点过第一个）。
        """
        for candidate in (project_id,
                          self._as_int(self.library.get_ui_option(
                              self.SESSION_PROJECT_KEY, ""))):
            if candidate in known:
                self.show_project_board(candidate)
                return True
        return False

    def _saved_view_state(self) -> dict | None:
        """读回上次存的"看哪儿"。坏数据/空库一律当没有（容错优先）。"""
        raw = self.library.get_ui_option(self.SESSION_VIEW_KEY, "")
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _as_int(value) -> int:
        """把库里读出来的字符串（可能被人手改过）转成 id；转不了给 0。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------------ 视图

    # ------------------------------------------------- 界面尺寸（记住用户的调整）

    #: "排版跟随窗口"这个开关存库的键
    FOLLOW_RESIZE_KEY = "layout.follow_resize"

    #: 界面尺寸存在库里的键（跟着库走，换机器/重启都还在）
    LAYOUT_KEYS = {
        "main": "layout.main_split",
        "sidebar": "layout.sidebar_columns",
        # .v2：列宽状态换了版本 —— 旧存档里带着"最后一列 Stretch"的模式，
        # 恢复回来会把新代码设的 Interactive 覆盖掉（事件枝名那列就撑不开了）
        "header": "layout.event_header.v2",
        "rail": "layout.rail_visible",
        "event": "layout.event_split",
        "timeline": "layout.timeline_split",
    }

    def set_edit_layout(self, editing: bool, *, save: bool = True) -> None:
        """

        关着（默认）：
        * 事件枝页的方框锁住 —— 拖标题栏不动它（只提示怎么解锁）、不认拉伸把手、
          **也不画那三个把手**（不然看着像"点一下就能调"，其实拖不动）；
        * 不画格子（更清爽，也省一遍画线）；
        * 方框右键的「整理排版」也是灰的。

        打开（排版模式）：能拖能拉、有格子、整理排版可用。

        注意：它**只管排版这件事**。"窗口变大时画面跟着放大"是另一回事，
        由画布自己的"跟随窗口适应"负责（默认开着，见 EventStageView.resizeEvent）。
        """
        editing = bool(editing)
        self.event_view.edit_layout = editing
        if not editing:
            # 锁布局时把正在选中的方框一并取消：光改标志不重画/不清选中的话，
            self.event_view.select_box(None)
        # 立刻重画：锁上/解锁后，原来画着的那圈蓝环和三个把手要消掉/出现 ——
        self.event_view.viewport().update()
        self.board_view.viewport().update()
        toolbar = getattr(self, "event_toolbar", None)
        if toolbar is not None:
            toolbar.set_editing(editing)
        action = getattr(self, "action_edit_layout", None)
        if action is not None and action.isChecked() != editing:
            action.blockSignals(True)
            action.setChecked(editing)
            action.blockSignals(False)
        if save:
            try:
                self.library.set_ui_option(self.FOLLOW_RESIZE_KEY,
                                          "1" if editing else "0")
            except LibraryError:
                pass
        self.show_message(
            "已进入排版模式：可以拖方框、拉大小（会记进框体模板）"
            if editing else
            "已锁定布局：方框不会跟着鼠标动（要改排版就点工具栏的「编辑布局」）"
        )

    def _restore_layout_options(self) -> None:
        """恢复上次的界面尺寸：主分隔条、项目/事件分隔条、事件表列宽、侧栏是否收起。"""
        for splitter, key, default in (
            (self.main_splitter, self.LAYOUT_KEYS["main"], None),
            (self.event_splitter, self.LAYOUT_KEYS["event"], [560, 280]),
            (self.timeline_panel.splitter, self.LAYOUT_KEYS["timeline"], [280, 560]),
        ):
            raw = self.library.get_ui_option(key, "")
            sizes: list[int] | None = None
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list) and len(parsed) == splitter.count():
                        sizes = [int(item) for item in parsed]
                except (ValueError, TypeError):
                    sizes = None
            if sizes is None and default is not None:
                sizes = default
            if sizes is not None:
                splitter.setSizes(sizes)

        # 侧栏树的"开始"列宽（名称列自动撑满，不用记）
        raw = self.library.get_ui_option(self.LAYOUT_KEYS["sidebar"], "")
        if raw:
            try:
                parsed = json.loads(raw)
                width = int(parsed[0]) if isinstance(parsed, list) and parsed else int(raw)
                if 40 <= width <= 400:
                    self.sidebar.tree.setColumnWidth(1, width)
            except (ValueError, TypeError):
                pass

        # "编辑布局"也记住（默认关：稳定模式）
        self.set_edit_layout(
            self.library.get_ui_option(self.FOLLOW_RESIZE_KEY, "0") == "1",
            save=False,
        )

        # 连线走向也记住（默认枝叶弧线；勾过"连线走直角"就一直走直角）
        self._set_edge_route(
            self.library.get_ui_option(self.EDGE_ROUTE_KEY, EDGE_ROUTE_CURVE)
            == EDGE_ROUTE_ORTH,
            save=False,
        )

        # 侧栏收起状态也记住（用户上次收起就还是收起，"侧栏"按钮同步）
        hidden = self.library.get_ui_option(self.LAYOUT_KEYS["rail"], "1") == "0"
        if hidden:
            self.toggle_side_panels(False, save=False)

    def _watch_layout_changes(self) -> None:
        """用户拖分隔条 / 调列宽时，过一会儿（不抖了）把尺寸记进库。"""
        for splitter, key in ((self.main_splitter, self.LAYOUT_KEYS["main"]),
                              (self.event_splitter, self.LAYOUT_KEYS["event"]),
                              (self.timeline_panel.splitter,
                               self.LAYOUT_KEYS["timeline"])):
            splitter.splitterMoved.connect(
                lambda *_a, k=key: self._schedule_layout_save()
            )
        self.sidebar.tree.header().sectionResized.connect(
            lambda *_a: self._schedule_layout_save()
        )

    def _schedule_layout_save(self) -> None:
        self._layout_save_timer.start()      # 单次触发：停止拖动 0.4 秒后再写

    def save_layout_options(self) -> None:
        """把当前的界面尺寸写进库（拖动结束、关窗时都会调）。"""
        try:
            self.library.set_ui_option(
                self.LAYOUT_KEYS["main"], json.dumps(self.main_splitter.sizes())
            )
            self.library.set_ui_option(
                self.LAYOUT_KEYS["sidebar"],
                json.dumps([self.sidebar.tree.columnWidth(1)]),
            )
            self.library.set_ui_option(
                self.LAYOUT_KEYS["event"], json.dumps(self.event_splitter.sizes())
            )
            self.library.set_ui_option(
                self.LAYOUT_KEYS["timeline"],
                json.dumps(self.timeline_panel.splitter.sizes()),
            )
            # "下次打开接着看哪儿"也在这条路上写（关窗时 shutdown() 会调这里），
            # 不另开一条保存路径
            self.save_session_state()
        except Exception:
            # 关窗时定时器可能刚好才响：库没了就当作不用存，别弹异常
            pass

    def toggle_side_panels(self, visible: bool | None = None, *, save: bool = True) -> None:
        """显示 / 隐藏左侧的项目·事件枝列表（隐藏后画布占满整窗，拖文件最方便）。"""
        if visible is None:
            visible = not self.browse_column.isVisible()
        self.browse_column.setVisible(visible)
        # 恢复布局时工具栏可能还没建好，容错一下
        action = getattr(self, "action_rail", None)
        if action is not None:
            action.setChecked(visible)
        if visible:
            sizes = self.main_splitter.sizes()
            if sizes and sizes[0] <= 1:
                self.main_splitter.setSizes([330, 1200])
        else:
            self._rail_sizes = self.main_splitter.sizes()
            self.main_splitter.setSizes([0, 1500])
        if save:
            try:
                self.library.set_ui_option(
                    self.LAYOUT_KEYS["rail"], "1" if visible else "0"
                )
                if visible and getattr(self, "_rail_sizes", None):
                    # 展开时回到收起前的宽度
                    self.main_splitter.setSizes(self._rail_sizes)
                    self.save_layout_options()
            except LibraryError:
                pass
        self.show_message("侧栏已隐藏，画布占满整窗" if not visible else "侧栏已显示")

    #: 兼容旧名字
    def toggle_right_rail(self, visible: bool | None = None) -> None:
        self.toggle_side_panels(visible)

    def _active_view(self):
        widget = self.view_stack.currentWidget()
        if widget is self.event_page:
            return self.event_view          # 事件枝页画布（缩放/适应窗口都作用在它上面）
        return widget if widget is not None else self.board_view

    def _sync_zoom_control(self) -> None:
        """把右下角的缩放控件绑到当前视图上，并摆到右下角。"""
        if not hasattr(self, "zoom_control"):
            return
        self.zoom_control.bind(self._active_view())
        self._place_zoom_control()
        self._place_canvas_toolbar()

    def _place_canvas_toolbar(self) -> None:
        """两个浮动工具栏：**各页只显示自己那个**，而且位置也不一样。

        * **白板**：底部中间（照 Obsidian 白板那套，按钮是"往画布上添东西"）；
        * **事件枝页**：**右上角、缩放条正下方**。这一页的底部被"年轮"时间轴
          （版本管理）占着，工具栏压在上面既挡时间轴、又和切稿的手势打架

        """
        panel = getattr(self, "_center_panel", None)
        if panel is None:
            return
        current = self.view_stack.currentWidget()
        board = getattr(self, "canvas_toolbar", None)
        event = getattr(self, "event_toolbar", None)
        if board is not None:
            self._place_board_toolbar(board, panel, current is self.board_view)
        if event is not None:
            self._place_event_toolbar(event, panel, current is self.event_page)

    @staticmethod
    def _move_overlay(widget, panel, target: QPoint) -> None:
        """位置没变就不动 —— 拖窗口时 resize 会连着来几十次，白干的 move 省掉。"""
        widget.adjustSize()
        if widget.pos() != target:
            widget.move(target)
        widget.raise_()

    def _place_board_toolbar(self, toolbar, panel, visible: bool) -> None:
        """白板工具栏：底部中间（这一页的底部没有别的东西）。"""
        toolbar.setVisible(visible)
        if not visible:
            return
        origin = self.view_stack.mapTo(panel, QPoint(0, 0))
        self._move_overlay(
            toolbar, panel,
            QPoint(
                max((panel.width() - toolbar.width()) // 2, 0),
                max(origin.y() + self.view_stack.height() - toolbar.height() - 14, 0),
            ),
        )

    def _place_event_toolbar(self, toolbar, panel, visible: bool) -> None:
        """事件枝页工具栏：右上角、**紧贴缩放条下面**，竖排一列，与缩放条右边缘对齐。


        * **算完再夹一次**：面板比工具栏还窄时（窗口很小 / 侧栏展开），
          把 x 夹回面板里，宁可左边留白也不要跑出去。
        """
        toolbar.setVisible(visible)
        if not visible:
            return
        toolbar.adjustSize()
        control = getattr(self, "zoom_control", None)
        origin = self.view_stack.mapTo(panel, QPoint(0, 0))
        if control is not None and control.isVisible():
            # 纵坐标跟着缩放条的**实际底边**：它随字体/DPI 高低变化，两者永远不叠
            top = control.y() + control.height() + 8
            right_edge = control.x() + control.width()
        else:
            top = origin.y() + 10
            right_edge = panel.width() - 18
        x = right_edge - toolbar.width()
        x = max(min(x, panel.width() - toolbar.width() - 6), 6)
        y = max(top, 0)
        self._move_overlay(toolbar, panel, QPoint(x, y))

    def _place_overlays(self) -> None:
        """把两个浮动控件（缩放 / 画布工具栏）摆到该在的位置。"""
        self._place_zoom_control()
        self._place_canvas_toolbar()

    def _place_zoom_control(self) -> None:
        control = getattr(self, "zoom_control", None)
        panel = getattr(self, "_center_panel", None)
        if control is None or panel is None:
            return
        control.adjustSize()
        # 画布的**右上角**：纵坐标从"视图区"的顶边算起，
        # 不能从整个中间面板顶边算 —— 那会压到上面那条导航（返回/面包屑/◀ ▶）。
        origin = self.view_stack.mapTo(panel, QPoint(0, 0))
        target = QPoint(
            max(panel.width() - control.width() - 18, 0),
            max(origin.y() + 10, 0),
        )
        if control.pos() != target:          # 同上：位置没变就不动
            control.move(target)
        control.raise_()

    def eventFilter(self, obj, event):  # noqa: N802
        """中央那块大小一变（窗口缩放 / 拖分隔条）就把缩放控件摆回右下角。"""
        panel = getattr(self, "_center_panel", None)
        if obj is panel and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            # 立刻摆（测试和交互都指望它马上在正确位置），但**位置没变就不动** ——
            # 拖窗口时 resize 会连着来几十个，白干的 adjustSize + move 就省了
            self._place_overlays()
        return super().eventFilter(obj, event)

    # ---------------------------------------------------------------- 撤销 / 重做

    def undo(self) -> None:
        try:
            label = self.undo_stack.undo()
        except LibraryError as error:
            # 闭包里的库操作失败（记录已经被留在重做栈里了，见 UndoStack.undo）：
            # 界面可能只改了一半，所以照样刷一遍，别让画面跟库里的实情不一样
            self._after_undo_redo()
            self._notify("撤销失败", str(error))
            return
        if label is None:
            self.show_message("没有可撤销的操作了")
            return
        self._after_undo_redo()
        self.show_message(f"已撤销：{label}")

    def redo(self) -> None:
        try:
            label = self.undo_stack.redo()
        except LibraryError as error:
            self._after_undo_redo()
            self._notify("重做失败", str(error))
            return
        if label is None:
            self.show_message("没有可重做的操作了")
            return
        self._after_undo_redo()
        self.show_message(f"已重做：{label}")

    def _after_undo_redo(self) -> None:
        """撤销/重做之后统一刷新：界面永远从库里长出来。"""
        self.sidebar.rebuild()
        self._refresh_board()
        self.refresh_event_page()
        # 状态栏那几个数字也是"从库里长出来的"：撤销一次归档/新建/删除，
        # 事件与版本数就变了，不刷新的话数字停在旧值上（用户以为没生效）
        self._update_status()

    def refresh_event_page(self) -> None:
        """事件枝页如果开着，按当前 document/version 重新画一遍。

        这里**只**调 ``event_view.set_event``：底部那条"年轮"时间轴由它内部
        一并填好（见 ``EventStageView._fill_timeline``）。
        以前这里还多调了一次 ``timeline_panel.set_versions(library, 文档 id, 稿 id)``，
        参数个数就不对（那个方法吃 ``(entries, selected)``）—— 于是**每次撤销都抛
        TypeError**，撤销的收尾动作全被这行异常掐断。
        """
        if self.view_stack.currentWidget() is not self.event_page:
            return
        if self._document_id:
            self.event_view.set_event(self.library, self._document_id, self._version_id)

    def _sync_toolbar_undo(self) -> None:
        toolbar = getattr(self, "canvas_toolbar", None)
        if toolbar is None:
            return
        toolbar.undo_button.setEnabled(self.undo_stack.can_undo())
        toolbar.redo_button.setEnabled(self.undo_stack.can_redo())

    def _sync_undo_actions(self) -> None:
        for action, enabled, label in (
            (getattr(self, "action_undo", None), self.undo_stack.can_undo(),
             self.undo_stack.undo_label()),
            (getattr(self, "action_redo", None), self.undo_stack.can_redo(),
             self.undo_stack.redo_label()),
        ):
            if action is None:
                continue
            action.setEnabled(enabled)
            base = action.text()
            action.setToolTip(f"{base}：{label}" if label else base)

    def _record_move(self, moved: dict, before: dict,
                     stored: set[int] | None = None, label: str = "") -> None:
        """把一次移动记成可撤销的操作（挪回去 / 再挪过来）。

        ``stored`` 是"动之前库里存过位置的节点集合"：不在里面的，
        撤销时应当**把位置清掉**（回到自动布局），这才是真正的"原样"。

        **连续微调会合并成一条**：方向键每敲一下就是一次移动（一次 5px），
        100 步的栈几秒就被挤爆。判定条件是"上一条撤销记录是我刚记的这批
        document_id 的移动、且 1 秒内"。
        为什么选"合并"而不是"在 _save_positions 里忽略 autoRepeat"：
        忽略 autoRepeat 只少记了中间的**撤销点**，但那次"重做"会退回到
        **第一下 5px** 的位置 —— 用户按了十下方向键再 Ctrl+Z、Ctrl+Y，
        卡片就停在离原地 5px 的地方，位置和库里存的（最后那一下）对不上。
        合并则两个方向都准：撤销回这一串的起点，重做去这一串的最新位置。
        带 ``label`` 的显式操作（网格排列等）不参与合并 —— 它不是"微调一串"。
        """
        stored = stored if stored is not None else set(moved)
        pairs = {}
        for document_id, target in moved.items():
            old = before.get(document_id)
            if old is None:
                continue
            undo_value = old if document_id in stored else None
            if (undo_value, target) == (target, target) and document_id in stored:
                continue
            if undo_value == target:
                continue
            pairs[document_id] = (undo_value, target)
        if not pairs:
            return

        def apply(values: dict) -> None:
            """``value`` 为 ``None`` 表示"原来根本没存过位置" → 清掉，回到自动布局。"""
            applied: dict[int, tuple[float, float]] = {}
            try:
                for document_id, value in values.items():
                    if value is None:
                        self.library.clear_document_position(document_id)
                        continue
                    self.library.set_document_position(document_id, value[0], value[1])
                    applied[document_id] = value
            except LibraryError as error:      # pragma: no cover - 罕见
                self.show_message(f"恢复位置失败：{error}")
                return
            if applied:
                self.board_view.apply_positions(applied)

        name = label or f"移动 {len(pairs)} 个元素"
        key = frozenset(pairs)
        burst = self._move_burst
        merged = bool(
            not label                       # 显式操作自成一条，不跟微调串混
            and burst is not None
            and burst["key"] == key
            and time.monotonic() - burst["at"] <= MOVE_MERGE_WINDOW_S
            # 关键：栈顶必须还是我上次记的那条。中间要是用户干过别的（连线、
            # 删卡片…），就绝不能改写它 —— 那会把别人的操作吞掉
            and self.undo_stack.top() is burst["entry"]
        )
        # 合并时撤销回"这一串微调的起点"，否则回这一次动作的起点
        origin = dict(burst["origin"]) if merged else {
            document_id: old for document_id, (old, _new) in pairs.items()
        }
        targets = {document_id: new for document_id, (_old, new) in pairs.items()}
        if merged:
            self.undo_stack.replace_top(lambda: apply(origin),
                                        lambda: apply(targets), name)
            self._move_burst["at"] = time.monotonic()
            self._move_burst["entry"] = self.undo_stack.top()
            return
        self.undo_stack.push(name, lambda: apply(origin), lambda: apply(targets))
        # 带 label 的显式操作（网格排列）**不开启微调串**：不然紧接着敲方向键
        # 就会合并进"网格排列"那一条，用户想单独撤销这次排列就做不到了
        if label:
            self._move_burst = None
            return
        self._move_burst = {
            "key": key, "at": time.monotonic(), "origin": origin,
            "entry": self.undo_stack.top(),
        }

    def _active_view_fit(self) -> None:
        view = self._active_view()
        if hasattr(view, "fit_to_content"):
            view.fit_to_content()

    # ------------------------------------------------------------ 导航

    # 说明：以前有个"画布 / 时间轴"展示模式开关，现在**没有这个模式**了 ——
    # 时间轴改成画布**底侧**常驻的一条（EventTimelineStrip），点某一稿画布就切到它，
    # 默认显示最新的一稿。环节的年轮仍然是那张图（从方框右键进去）。

    def _current_view_state(self) -> dict | None:
        """现在在看什么（用于"返回"）。"""
        current = self.view_stack.currentWidget()
        if current is self.graph_view and self._document_id:
            if self._stage_role:
                return {"kind": "stage", "document_id": self._document_id,
                        "role": self._stage_role, "version_id": self._version_id}
            return {"kind": "event", "document_id": self._document_id, "show": "timeline"}
        if current is self.event_page and self._document_id:
            return {"kind": "event", "document_id": self._document_id, "show": "canvas"}
        if self._project_id:
            return {"kind": "board", "project_id": self._project_id}
        return None

    def _describe_state(self, state: dict) -> str:
        if state["kind"] == "board":
            project = self.library.get_project(state["project_id"])
            return project.name if project else "项目树"
        document = self.library.get_document(state["document_id"])
        title = document.title if document else "事件枝"
        if state["kind"] == "stage":
            return f"{title} · {ROLE_LABELS.get(state.get('role'), state.get('role'))}"
        return title

    def _record_navigation(self, target: dict) -> None:
        """切换视图前把"当前这一处"记进历史（返回时用）。"""
        if self._restoring:
            return
        current = self._current_view_state()
        if current is None or current == target:
            return
        if self._nav_history and self._nav_history[-1] == current:
            return
        self._nav_history.append(current)
        if len(self._nav_history) > 50:
            self._nav_history.pop(0)

    def _restore_view(self, state: dict) -> None:
        self._restoring = True
        try:
            kind = state.get("kind")
            if kind == "board":
                self.show_project_board(state["project_id"])
            elif kind == "stage":
                self.open_stage_flow(state["document_id"], state["role"],
                                     state.get("version_id"))
            elif kind == "event":
                if state.get("show") == "timeline":
                    self._open_document(state["document_id"], state.get("version_id"))
                else:
                    self._open_event_canvas(state["document_id"])
        finally:
            self._restoring = False

    def can_go_back(self) -> bool:
        """还有上一处可回吗（在最上层的版图且没有历史时，返回按钮就该灰掉）。"""
        if self._nav_history:
            return True
        return self.view_stack.currentWidget() is not self.board_view

    def go_back(self) -> None:
        """返回上一处：有历史就按历史退，没有就按层级往上退一层。"""
        if self._nav_history:
            state = self._nav_history.pop()
            self._restore_view(state)
            self.show_message(f"已返回：{self._describe_state(state)}")
            return
        self._up_one_level()

    def _up_one_level(self) -> None:
        """按层级往上退：环节 → 事件 → 项目树。"""
        current = self.view_stack.currentWidget()
        if current is self.graph_view and self._stage_role and self._document_id:
            self.open_event(self._document_id)
            return
        if (current in (self.graph_view, self.event_page)) and self._document_id:
            document = self.library.get_document(self._document_id)
            node = self.library.get_node(document.node_id) if document else None
            if node is not None:
                self.show_project_board(node.project_id)
                return
        if current is self.board_view:
            self.show_message("已经在最上层了（项目树）")

    # ------------------------------------------------ 上一个 / 下一个

    def _nav_targets(self) -> tuple[str, list[int], int]:
        """返回 (单位, 可选 id 列表, 当前下标)。

        在**时间轴**里走「上一稿 / 下一稿」，在版图 / 事件枝页里走「上/下一个事件枝」——
        和旧版视图工具栏那组 ◀ 序号 ▶ 是一个意思。
        """
        current = self.view_stack.currentWidget()
        if current is self.event_page and self._document_id:
            # 事件枝页：底侧就是时间轴，所以这组箭头走的是**前后稿**
            ids = [v.id for v in self.library.version_history(self._document_id)]
            index = ids.index(self._version_id) if self._version_id in ids else -1
            return "稿", ids, index
        if current is self.graph_view and self._document_id:
            graph = self.graph_view.graph()
            ids = [node.version_id for node in (graph.nodes if graph else [])]
            if ids:
                ids = list(ids)
            index = ids.index(self._version_id) if self._version_id in ids else -1
            return "稿", ids, index
        ids = self.sidebar.event_ids()
        index = ids.index(self._document_id) if self._document_id in ids else -1
        return "事件枝", ids, index

    def _update_nav(self) -> None:
        unit, ids, index = self._nav_targets()
        total = len(ids)
        self.nav_label.setText(
            f"{index + 1}/{total} {unit}" if index >= 0 and total else f"—/{total} {unit}"
        )
        # 还没选中任何一个时，两个箭头都可用（向前=第一个，向后=最后一个）
        self.nav_prev.setEnabled(total > 0 and index != 0)
        self.nav_next.setEnabled(total > 0 and index != total - 1)
        self.nav_prev.setToolTip(f"上一个{unit}（Ctrl+←）")
        self.nav_next.setToolTip(f"下一个{unit}（Ctrl+→）")
        self.back_button.setEnabled(self.can_go_back())
        self._update_breadcrumb()

    def go_previous(self) -> None:
        self._step_nav(-1)

    def go_next(self) -> None:
        self._step_nav(1)

    def _step_nav(self, delta: int) -> None:
        unit, ids, index = self._nav_targets()
        if not ids:
            return
        if index < 0:
            target = 0 if delta > 0 else len(ids) - 1
        else:
            target = index + delta
        if not (0 <= target < len(ids)):
            self.show_message(f"已经是{'第一' if delta < 0 else '最后'}个{unit}了")
            return
        target_id = ids[target]
        current = self.view_stack.currentWidget()
        if current is self.event_page and self._document_id:
            # 事件枝页：切到上一稿 / 下一稿（画布跟着走）
            self.show_event_version(target_id)
            return
        if current is self.graph_view and self._document_id:
            # 时间轴：走上一稿 / 下一稿，右侧面板跟着显示「这一稿改了什么」
            self._select_version_in_chain(target_id)
            return
        if current is self.board_view:
            # 版图：不离开版图，只把选中挪到下一个事件枝
            self._on_board_document_selected(target_id)
            self.board_view.select_document(target_id)
            self._update_nav()
            return
        self.open_event(target_id)

    def _select_version_in_chain(self, version_id: int) -> None:
        self._version_id = version_id
        self.graph_view.select_version(version_id)
        self.graph_view.ensure_version_visible(version_id)
        self._update_nav()

    # ------------------------------------------------------------ 面包屑

    def _update_breadcrumb(self) -> None:
        """`库 › 项目 › 事件 › 环节`：点哪一段就跳到那一层。"""
        while self.crumb_layout.count():
            item = self.crumb_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        project_id: int | None = None
        document_id: int | None = None
        role: str | None = None
        current = self.view_stack.currentWidget()
        if current in (self.graph_view, self.event_page) and self._document_id:
            document_id = self._document_id
            if current is self.graph_view and self._stage_role:
                role = self._stage_role
        if self._project_id:
            project_id = self._project_id

        segments: list[tuple[str, object]] = [(self.library.info.name, None)]
        if project_id:
            project = self.library.get_project(project_id)
            if project:
                segments.append((
                    project.name,
                    # 注意：Qt 的 clicked/triggered 会把 checked(bool) 当**第一个位置参数**传进来，
                    # 所以第一个参数必须显式接住它，否则它会顶掉我们自己捕获的值
                    lambda _checked=False, pid=project_id: self.show_project_board(pid),
                ))
        if document_id:
            document = self.library.get_document(document_id)
            if document:
                segments.append(
                    (document.title,
                     lambda _checked=False, did=document_id: self.open_event(did))
                )
        if role and document_id:
            segments.append(
                (ROLE_LABELS.get(role, role),
                 lambda _checked=False, did=document_id, r=role: self.open_stage_flow(did, r))
            )

        for index, (text, slot) in enumerate(segments):
            if index:
                arrow = QLabel("›", self.crumb_bar)
                arrow.setProperty("role", "hint")
                self.crumb_layout.addWidget(arrow)
            if slot is None:
                label = QLabel(text, self.crumb_bar)
                label.setProperty("role", "hint")
                self.crumb_layout.addWidget(label)
                continue
            button = QToolButton(self.crumb_bar)
            button.setText(text)
            button.setAutoRaise(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            # 最后一段 = 现在这一层，加粗并且不重复跳转
            is_current = index == len(segments) - 1
            button.setStyleSheet(
                "QToolButton{border:none;padding:2px 4px;"
                f"font-weight:{'700' if is_current else '400'};"
                f"color:{'#1b2a20' if is_current else '#0e6b3d'};}}"
                "QToolButton:hover{text-decoration:underline;}"
            )
            if not is_current:
                button.clicked.connect(slot)
            else:
                button.setCursor(Qt.CursorShape.ArrowCursor)
            self.crumb_layout.addWidget(button)

    def _set_project_layout(self, mode: str) -> None:
        self._project_layout = mode
        if self._project_id:
            self.show_project_board(self._project_id)

    def show_project_board(self, project_id: int, keep_view: bool = False,
                           switch: bool | None = None) -> None:
        """显示某个项目的版图（事件为节点，可拖动可连线）。

        :param keep_view: ``True`` = 只是刷新内容（拖完节点、连完线），
            别动用户的缩放和滚动位置。
        :param switch: 要不要**把视图切到版图**。``None``（默认）表示
            "跟随 keep_view"：刷新内容时不切，正常打开时切。
            为什么要这个参数：撤销/重做走的是"刷新内容"这条路
            （``_refresh_board(keep_view=True)``），以前它**无条件**
            ``setCurrentWidget(self.board_view)`` —— 在事件枝页按一下 Ctrl+Z
            就被踢回版图，"返回"历史里还被塞了一条假记录。
        """
        if switch is None:
            switch = not keep_view
        if switch:
            self._record_navigation({"kind": "board", "project_id": project_id})
        self._project_id = project_id
        project = self.library.get_project(project_id)
        graph = build_project_graph(self.library, project_id, layout=self._project_layout)
        self.board_view.set_graph(graph, keep_view=keep_view)
        if switch:
            self.view_stack.setCurrentWidget(self.board_view)

        self._apply_board_header(project, graph)
        self._refresh_state_chip(None)
        if switch and not keep_view:
            self.board_view.fit_to_content()
        # 侧栏也要跟着选中这个项目（notify=False：别再绕回来一次）
        self.sidebar.select_project(project_id)
        self._update_nav()
        self._sync_zoom_control()

    def _apply_board_header(self, project, graph) -> None:
        """版图上方那行标题 + 一句说明（拖动之后只更新它，不重建画布）。"""
        self.view_title.setText(project.name if project else "项目树")
        details = [f"{graph.document_count} 个事件枝"]
        if graph.manual:
            details.append("按你摆放的位置显示")
        else:
            details.append(
                "白板：拖动节点摆位置（自动保存）；右边的圆点拖出去 = 别人依赖它，"
                "左边的圆点拖出去 = 它依赖别人"
            )
        if graph.edges:
            details.append(f"{len(graph.edges)} 条连线")
        self.view_subtitle.setText(" · ".join(details))

    def open_event(self, document_id: int, version_id: int | None = None,
                   keep_view: bool = False) -> None:
        """打开事件枝页：方框画布 + **底侧时间轴**（点某一稿就切换画布，默认最新稿）。

        :param keep_view: 只是内容变了（改状态 / 改说明之后的刷新），
            保住用户当前调好的缩放与滚动位置（见 ``_refresh_current_view``）。
        """
        self._open_event_canvas(document_id, version_id, keep_view=keep_view)

    def _goto_event_project(self, document) -> None:
        """把"当前项目"对齐到这个事件枝所属的项目（面包屑 / 版图 / 上下一个事件枝都要用）。"""
        node = self.library.get_node(document.node_id)
        if node is not None:
            self._project_id = node.project_id

    def _open_event_canvas(self, document_id: int, version_id: int | None = None,
                           keep_view: bool = False) -> None:
        """事件枝页：方框画布（环节框 + 信息框 + 文件列表）+ 底侧时间轴。"""
        document = self.library.get_document(document_id)
        if document is None:
            return
        # **切事件/切稿之前**先把还没提交的编辑落库：说明框只在失焦时回写，
        # 而 ``set_event`` 里那次补提交发生在 ``_document_id / _version_id``
        # 已经改成新目标**之后** —— 用户刚敲进去、还没失焦的那句话会被写到
        # 新打开的那一稿（"我写的说明跑到别的稿上去了"就是这个方向）。
        self.event_view.commit_pending()
        if not keep_view:
            # 刷新同一个人那一页时不用记历史：记了就是一笔假记录
            # （"返回"会退到一个他从没走过的地方）
            self._record_navigation({"kind": "event", "document_id": document_id,
                                     "show": "canvas"})
        self._document_id = document_id
        self._stage_role = None
        self._version_id = version_id or document.current_version_id
        self._goto_event_project(document)
        # 先把"新方框默认多大"装回去（每个方框自己的大小在库里的排版记录里），再画
        self.event_view.set_default_units(*self._event_default_units())
        self.event_view.set_event(self.library, document_id, self._version_id,
                                  keep_view=keep_view)
        self.view_stack.setCurrentWidget(self.event_page)

        self._update_event_subtitle(document)
        # notify=False：这次打开就是侧栏选中的结果，不要再往回发一次信号
        self.sidebar.select_event(document_id, notify=False)
        self._update_nav()
        self._sync_zoom_control()

    def add_event_version(self) -> None:
        """时间轴右边的「＋ 新增一稿」：**每点一次就真建一稿空的**，并把界面切到它。

        然后归档以前用其他软件归档的文件"）—— 所以这里不做"上一稿还空着就复用"
        的去重，每一稿都是用户显式要的。
        """
        if not self._document_id:
            self.show_message("先打开一个事件枝，再加新稿")
            return
        try:
            version = self.library.create_empty_version(self._document_id)
        except LibraryError as error:
            self._notify("新增一稿失败", str(error))
            return
        self.sidebar.rebuild()
        self._update_status()
        self.open_event(self._document_id, version.id)
        self.show_message(
            f"已新增 v{version.seq}（空稿，不用先给文件）：界面已经切到它，"
            "接下来写的说明、加的文件都落在这一稿上"
        )

    def delete_event_version(self, version_id: int) -> None:
        """删掉某一稿（底部"年轮"窗口上的按钮）—— 删除一定要先确认。"""
        version = self.library.get_version(version_id)
        if version is None:
            return
        document = self.library.get_document(version.document_id)
        files = len(version.files)
        if not self._confirm(
            "删除这一稿",
            f"确定删除 {document.title if document else ''} 的 v{version.seq} 吗？\n\n"
            f"这一稿登记了 {files} 个文件；删除后这一稿的记录会消失，\n"
            "你磁盘上的原文件不动。事件至少要留一稿，最后一稿删不掉。",
        ):
            return
        try:
            self.library.delete_version(version_id)
        except LibraryError as error:
            self._notify("删除失败", str(error))
            return
        self._after_write(f"已删除 v{version.seq}")
        if self._document_id == version.document_id:
            document = self.library.get_document(self._document_id)
            self._version_id = document.current_version_id if document else None
            self.open_event(self._document_id, self._version_id)
        elif self._project_id:
            self.show_project_board(self._project_id)

    def show_version_diff(self, version_id: int) -> None:
        """看某一稿相对上一稿改了什么（需要时才弹窗口，不再占常驻右侧栏）。"""
        document_id = self._document_id
        if not document_id:
            return
        document = self.library.get_document(document_id)
        if document is None:
            return
        dialog = DiffDialog(
            self.library_root, document.title,
            self.library.version_history(document_id), version_id, self,
        )
        self._diff_dialogs.append(dialog)
        dialog.finished.connect(lambda _r, d=dialog: self._forget_diff_dialog(d))
        dialog.show()

    def _forget_diff_dialog(self, dialog) -> None:
        try:
            self._diff_dialogs.remove(dialog)
        except ValueError:
            pass

    def show_event_version(self, version_id: int) -> None:
        """把画布切到某一稿（底侧时间轴点一下就会走这里）。"""
        if not self._document_id:
            return
        # 切稿之前先落库还没提交的说明（理由同 _open_event_canvas：补提交发生在
        # ``_version_id`` 换掉之后，会把刚敲的字写到你切过去的那一稿上）
        self.event_view.commit_pending()
        self._version_id = version_id
        self.event_view.set_event(self.library, self._document_id, version_id)
        document = self.library.get_document(self._document_id)
        if document is not None:
            self._update_event_subtitle(document)
        self._update_nav()
        version = self.library.get_version(version_id)
        if version is not None:
            latest = (document.current_version_id == version_id) if document else False
            self.show_message(
                f"画布已切到 v{version.seq}" + ("（最新一稿）" if latest else "（历史稿）")
            )

    def _refresh_state_chip(self, document) -> None:
        """头部那个状态小胶囊：显示当前状态，点开就是三个状态。

        「进度 / 状态」框，这里再放一个显眼的入口。
        """
        button = getattr(self, "state_button", None)
        if button is None:
            return
        if document is None or self._document_id is None:
            button.hide()
            return
        label = DOC_STATE_LABELS.get(document.state, document.state)
        button.setText(label)
        button.setIcon(state_icon(document.state, 16))
        border, fill, text_color = DOC_STATE_COLORS.get(
            document.state, DOC_STATE_COLORS["active"]
        )
        button.setStyleSheet(
            "QToolButton{"
            f"background:{fill}; color:{text_color}; border:1px solid {border};"
            "border-radius:10px; padding:2px 10px; font-size:11px;}"
            "QToolButton::menu-indicator{image:none;}"
            f"QToolButton:hover{{background:{border}; color:#ffffff;}}"
        )
        menu = self.state_menu
        menu.clear()
        for key in DOC_STATE_LABELS:
            action = menu.addAction(state_icon(key, 16), DOC_STATE_LABELS[key])
            action.setCheckable(True)
            action.setChecked(key == document.state)
            action.triggered.connect(
                lambda _checked=False, did=self._document_id, st=key:
                self._set_event_state(did, st)
            )
        button.show()

    def _update_event_subtitle(self, document) -> None:
        node = self.library.get_node(document.node_id)
        project = self.library.get_project(node.project_id) if node else None
        shown = self.library.get_version(self._version_id or document.current_version_id)
        viewing = ""
        if shown is not None:
            viewing = f"正在看 v{shown.seq}"
            if shown.id == document.current_version_id:
                viewing += "（最新）"
            else:
                viewing += f"（共 {document.version_count} 稿）"
        details = [
            project.name if project else "",
            viewing,
            document.people,
        ]
        self.view_title.setText(document.title)
        self.view_subtitle.setText(" · ".join(bit for bit in details if bit))
        self._refresh_state_chip(document)

    # ------------------------------------------------- 白板：工具栏 / 拖入新增

    def _create_item_from_toolbar(self, kind: str) -> None:
        """工具栏点一下：在画布中央新建。"""
        point = (self.board_view.canvas_center_scene_pos()
                 if self._on_board() else None)
        if point is None:
            self.show_message("先打开一个项目白板")
            return
        self.create_item_at(kind, point.x(), point.y())

    def create_item_at(self, kind: str, x: float, y: float) -> None:
        """在画布的这个位置新建说明框 / 事件枝（工具栏拖入、点按钮都走这里）。"""
        if self._project_id is None:
            self.show_message("先选一个项目树")
            return
        if kind == "note":
            self._add_board_note(x, y)
            return
        self.create_event_at(x, y)

    def create_event_at(self, x: float, y: float, files=None) -> int | None:
        """在画布上新建一个事件枝（可以顺手把拖进来的文件归档进第一稿）。"""
        if self._project_id is None:
            return None
        project = self.library.get_project(self._project_id)
        roots = self.library.node_tree(self._project_id)
        if project is None or not roots:
            return None
        title = self._unique_event_title("新事件枝")
        x, y = self.board_view.free_spot_near(x, y)
        try:
            today = QDate.currentDate().toString(Qt.DateFormat.ISODate)
            document = self.library.create_document(roots[0].id, title, task_date=today)
            self.library.set_document_position(document.id, x, y)
        except LibraryError as error:
            self.show_message(f"新建事件枝失败：{error}")
            return None
        paths = [str(item) for item in (files or [])]
        if paths:
            # 拖进来的文件直接作为第一稿归档（reference 模式：文件留在原处）
            self._archive_paths(document.id, paths)
        self._join_group_at(document.id, x, y)
        self._refresh_board()
        # 新建的卡片选中（选中的图元会自动提到最前，回到版图时一眼看到它）
        self.board_view.select_document(document.id)
        # 并且直接进事件枝页 —— 这样立刻就能改名 / 拖文件进去，不用去列表里翻
        self.sidebar.rebuild()
        self.open_event(document.id)
        self.sidebar.select_event(document.id, notify=False)
        self.undo_stack.push(
            f"新建事件枝「{title}」",
            # 撤销"新建"= 归档（软删），这样重做还能把它带回来；
            # 和「删除事件枝」（硬删、不可撤销）是两回事。
            lambda: (self.library.delete_document(document.id),
                     self.sidebar.rebuild(), self._refresh_board()),
            lambda: (self.library.update_document(document.id, status="active"),
                     self.sidebar.rebuild(), self._refresh_board()),
        )
        self.board_view.select_document(document.id)
        self.show_message(
            f"已新建事件枝「{title}」" + ("，并把拖进来的文件归档为第一稿" if paths else
                                   "（右键可改名，双击进去归档）")
        )
        return document.id

    def _join_group_at(self, document_id: int, x: float, y: float) -> int | None:
        """新建的事件如果落在某个组框里，就直接算这个组的人（空组也吃）。"""
        for group in self._board_groups():
            rect = self.board_view.group_rect_for(group["id"])
            if rect is None:
                continue
            gx, gy, gw, gh = rect
            if gx <= x <= gx + gw and gy <= y <= gy + gh:
                try:
                    self.library.add_group_members(group["id"], [document_id])
                except LibraryError:
                    return None
                return group["id"]
        return None

    def _unique_event_title(self, base: str) -> str:
        """默认名字不重复：新事件枝、新事件枝 2、新事件枝 3…

        归档过的事件也占着它原来的名字（``find_document_by_title`` 查的是
        全部状态），所以这里**连归档的一起算**，否则会撞出"已存在同名文档"。
        """
        existing = {document.title for document in self.library.list_documents(
            self.library.node_tree(self._project_id)[0].id, include_descendants=True,
            include_archived=True,
        )}
        if base not in existing:
            return base
        index = 2
        while f"{base} {index}" in existing:
            index += 1
        return f"{base} {index}"

    def _archive_paths(self, document_id: int, paths: list[str]) -> None:
        """把一批文件作为**新的一稿**归档到某个事件枝（新建事件枝时顺带归档用）。

        走内核的 ``attach_files``（和"拖到项目树行新建事件枝"是同一条通路）：
        role 交给内核按文件名判断，这里不多猜。

        """
        try:
            self.library.attach_files(document_id, [str(item) for item in paths])
        except (LibraryError, OSError) as error:
            self.show_message(f"归档失败：{error}")

    def _on_board_files_dropped(self, document_id: int, paths) -> None:
        """文件被拖到了白板上的**某张事件枝卡片**上（用户定的新语义）。

        以前这里是"拖到白板任意位置" → 空白处放素材卡片、文件夹问要不要打包成 zip。
        白板的节点，然后再确定要放在哪个类型下"），所以现在只有一条路：

        1. 拖到哪张卡片上，就进**那个事件枝**；
        2. **先问"放进哪个环节"**（参考/输入/分析/输出/图示/说明/其他），
           答案由用户给，不替他猜 —— 猜错了文件就归错类，比多问一句麻烦得多；
        3. 落进那个事件枝**当前这一稿**（``_draft_for_files``），不新建稿 ——
           与"拖进事件枝页的方框"完全同一条通路（``_run_archive``：
           哈希在后台线程里算，不占主线程）。
        """
        if not document_id:
            return
        files, folders = self._clean_dropped_paths(paths)
        if folders:
            # 文件夹仍然不直接收（照旧）：说清楚该先打包，别静默跳过
            names = "、".join(item.name for item in folders[:3])
            self.show_message(
                f"文件夹不直接收（{names}）：先把它打包成 zip，再把 zip 拖到事件枝卡片上"
            )
        if not files:
            return
        # 先定落点（白板上的卡片不带稿：落那个事件枝的最新稿），**再**问环节：
        # 反过来会出现"用户挑完环节才被告知这批文件没地方放"，那一步就白问了
        target_version_id, allowed = self._draft_for_files(document_id)
        if not allowed:
            return                      # 提示已经在 _draft_for_files 里给过
        role = self._ask_stage_role()
        if role is None:
            return                      # 用户取消了：库里一个字节都不动
        self._last_stage_role = role
        self._run_archive(
            document_id=document_id,
            paths=list(files),
            stage_role=role,
            stage_replace=(role == "figure"),   # 图示只放一张：拖进去就是换掉旧的
            target_version_id=target_version_id,
        )

    def _ask_stage_role(self) -> str | None:
        """问一句"这批文件放进哪个环节" → 环节名；用户取消返回 ``None``。

        连着往一个事件枝里加好几份参考/输入是很常见的操作，每次都要从列表头
        重新选一遍很烦；而"最近用过"是个看得见的选择 —— 对话框里那一条是**被选中**
        摆在那里的，用户按回车之前就能看到自己选的是哪个，不存在"悄悄记错"。
        完全没得参考时给"附件"（收杂项的那个方框），它是最不押注的一类。

        自动化模式（``interactive=False``）不弹框，直接用这个默认值 ——
        没人可问的时候，宁可给一个明确的类别也不要什么都不做。
        """
        choices = list(STAGE_DROP_ROLES)
        index = choices.index(self._last_stage_role) if self._last_stage_role in choices else -1
        if index < 0:
            index = choices.index("attachment")
        if not self.interactive:
            return choices[index]
        labels = [ROLE_LABELS.get(item, item) for item in choices]
        choice, ok = QInputDialog.getItem(
            self, "放进哪个环节",
            "把文件放进这个事件枝的哪个环节？\n"
            "（选完会加进它**当前这一稿**，不会新建稿。）",
            labels, index, False,
        )
        if not ok or not choice:
            return None
        return choices[labels.index(choice)]

    def _clean_dropped_paths(self, paths) -> tuple[list[str], list[Path]]:
        """把拖进来的路径理一理：**去重（保序）** + 把文件夹挑出来。

        为什么要去重：同一个文件拖两次，归档会往**同一稿里插两行同名文件**
        （`attach_files` 不查重，它只管登记），用户看到的是一份莫名其妙的重复。
        去重按 `Path.resolve()` 做：相对路径、`..`、软链都归到同一个 key。

        :return: ``(文件路径列表, 文件夹列表)``
        """
        seen: set[str] = set()
        files: list[str] = []
        folders: list[Path] = []
        for raw in paths:
            target = Path(str(raw))
            try:
                key = str(target.resolve())
            except OSError:            # 解析不了的路径（断链软链等）：按原样算
                key = str(target)
            if key in seen:
                continue
            seen.add(key)
            if target.is_dir():
                folders.append(target)
            else:
                files.append(str(target))
        return files, folders

    def _add_material_card(self, path: str, x: float, y: float,
                           *, text: str = "",
                           taken: set | None = None,
                           project_id: int | None = None) -> int | None:
        """在画布上放一张"文件素材"卡片（只记路径，不复制、不移动）。

        ``project_id`` 用来把卡片落到**指定的**项目上：打包是后台任务，
        完成时用户可能已经切到别的项目了（见 :meth:`_after_folder_pack`）。

        **界面上已经没有调用方了**：以前空白处拖文件就是走这里放一张素材卡片，
        于是这条通路只剩方法本身和它的测试。留着而不是删掉，是因为
        "把一份还没归属的文件先摆在白板上、想清楚了再右键转成事件"是个成立的做法，
        以后要给它一个不让人困惑的入口（比如空白处右键「放一张素材卡片…」）
        可以直接接回来。
        """
        target_project = self._project_id if project_id is None else project_id
        if target_project is None:
            return None
        x, y = self.board_view.free_spot_near(x, y, taken)
        source = Path(path)
        try:
            card_id = self.library.add_board_card(
                target_project, "file", x, y,
                title=source.name, text=text, path=str(source),
            )
        except LibraryError as error:
            self.show_message(f"加素材卡片失败：{error}")
            return None
        self._refresh_board()
        exists = source.exists()
        self.show_message(
            f"已放一张素材卡片「{source.name}」"
            + ("" if exists else "（⚠ 文件已不在原处）")
            + " —— 右键可以「转为事件…」"
        )
        return card_id

    def _offer_folder_pack(self, folder: Path, x: float, y: float) -> None:
        """拖进来的是文件夹：**不收**，问要不要打包成 zip。

        **界面上已经没有调用方了** —— 它原来只挂"拖到白板空白处"这一条路上，
        现在拖文件夹进来给的是另一种提示："先打包成 zip，再把 zip 拖到事件枝卡片上"，
        不再主动问"要不要我帮你打包" —— 那个问句本身就出现在用户没想到的地方。
        方法和它的测试一起留着：打包这件事本身没问题，只是入口先关掉。
        """
        answer = True
        if self.interactive:
            answer = self._confirm(
                "文件夹不直接收",
                f"「{folder.name}」是一个文件夹。\n\n"
                "ToOak 要替代文件夹管理，所以只收**一个明确的文件**，不收文件夹"
                "（归档时说不清进的是哪一份）。\n\n"
                f"要不要把它打包成 zip（{folder.name}.zip，就放在它旁边），"
                "再作为一张素材卡片放进来？\n（原文件夹保留不动）",
            )
        if not answer:
            self.show_message("没有放进白板（文件夹原样保留）")
            return
        self._start_folder_pack(folder, x, y)

    def _start_folder_pack(self, folder: Path, x: float, y: float) -> None:
        """后台打包文件夹成 zip，完成后在画布上放一张素材卡片。"""
        job = ZipJob(self.library_root, folder)
        # **落点和"放进哪个项目"必须在发起时就定下来**：打包要跑一阵子，
        # 这期间用户完全可能切到别的项目（甚至把库关了）。以前回调里现读
        # `self._project_id` 和旧坐标，卡片就落到**别的项目**上去了
        # 更是整批结果被 `_add_material_card` 静默丢掉。
        project_id = self._project_id
        job.on_done = lambda path, files, size: self._after_folder_pack(
            path, files, size, x, y, project_id
        )
        self._zip_jobs[job.id] = job
        job.start()
        self.show_message(f"正在打包「{folder.name}」…（后台进行，可以继续用）")

    def _after_folder_pack(self, path: Path, files: int, size: int,
                           x: float, y: float,
                           project_id: int | None = None) -> None:
        self.show_message(f"打包完成：{path.name}（{files} 个文件，{human_size(size)}）")
        if project_id is None or project_id != self._project_id:
            # 打包期间项目变了：宁可**不放**，也不能把卡片放到另一个项目上
            # （那会让用户在自己的项目里看到一份不知道从哪来的东西）。
            # 但结果不能丢 —— 告诉他 zip 在哪，想看就把这个 zip 拖进来。
            self._notify(
                "文件夹已打包好",
                f"{path}\n\n（打包期间你看的项目已经变了，所以没有把它放进白板；"
                "想收下它，就把这个 zip 拖到目标项目里）",
            )
            return
        self._add_material_card(
            str(path), x, y,
            text=f"由文件夹「{path.stem}」打包而来" + ("" if files else "（空文件夹）"),
            project_id=project_id,
        )

    def _on_board_create_item(self, kind: str, x: float, y: float) -> None:
        self.create_item_at(kind, x, y)

    def _on_board_document_selected_for_toolbar(self) -> None:
        self._place_canvas_toolbar()

    def _on_board_zoom_changed(self, _percent: int) -> None:
        self._place_canvas_toolbar()

    def _on_canvas_create_requested(self, kind: str, x: float, y: float) -> None:
        self.create_item_at(kind, x, y)

    # （以前这里还有一个 _on_canvas_files_dropped(paths, x, y)：白板只发落点坐标、
    #   主窗口再按坐标决定"空白处放素材卡片还是文件夹打包"。现在白板的
    #   filesDropped 直接给"落在哪张事件枝卡片上"，于是这层转发没必要了。）

    def _on_toolbar_group(self) -> None:
        self._group_from_selection()

    def _group_from_selection(self) -> None:
        """工具栏「分组」：把画布上选中的事件归为一组。"""
        if not self._on_board():
            return
        ids = self.board_view.selected_document_ids()
        if not ids:
            self.show_message("先在画布上框选（Ctrl + 拖空白）或 Ctrl 点选几个事件枝")
            return
        self._create_board_group(ids)

    def _export_from_toolbar(self) -> None:
        """工具栏「导出…」：导选中的事件；没选就让用户先选（或导整组）。"""
        if not self._on_board():
            return
        ids = self.board_view.selected_document_ids()
        if not ids:
            groups = self._board_groups()
            if len(groups) == 1:
                self._export_board_group(groups[0]["id"])
                return
            self.show_message("先在画布上框选要导出的事件枝（Ctrl + 拖空白）")
            return
        self._export_selection(ids)

    # ------------------------------------------------- 素材卡片：转正 / 打开 / 重指定

    def _promote_card(self, card_id: int) -> None:
        """「转为事件…」：问名字 + 归入哪个分组，然后把卡片转成一个事件枝。"""
        card = self.library.get_board_card(card_id)
        if card is None:
            return
        if card["kind"] == "note":
            self.show_message("说明框不转事件枝（它就是白板上的便利贴）")
            return
        name = self._ask_text("转为事件", "事件枝名：",
                              card["title"] or Path(card["path"]).name or "新事件枝")
        if not name or not name.strip():
            return
        group_id = self._ask_group_choice()
        if group_id is False:            # 用户取消了
            return
        try:
            document_id = self.library.promote_card_to_event(
                card_id, title=name.strip(), group_id=group_id or None
            )
        except (LibraryError, ValueError) as error:
            self._notify("转为事件失败", str(error))
            return
        self.sidebar.rebuild()
        self._refresh_board()
        self.board_view.select_document(document_id)
        group = next((item for item in self._board_groups()
                      if item["id"] == group_id), None)
        self.show_message(
            f"已把这张卡片转为事件枝「{name.strip()}」"
            + (f"，并归入分组「{group['name']}」" if group else "（没归入分组）")
        )

    def _ask_group_choice(self):
        """问"归入哪个分组"：返回 组 id / None（不加入）/ False（取消）。"""
        groups = self._board_groups()
        if not groups:
            return None
        if not self.interactive:                  # 自动化：不打扰
            return None
        items = ["不加入分组"] + [f"{item['name']}（{item['id']}）" for item in groups]
        choice, ok = QInputDialog.getItem(
            self, "归入哪个分组", "把新事件枝放进哪个分组：", items, 0, False
        )
        if not ok:
            return False
        index = items.index(choice)
        return None if index == 0 else groups[index - 1]["id"]

    def _open_card_file(self, card_id: int) -> None:
        """双击文件素材卡片：用系统默认程序打开（只读地打开，不动文件）。"""
        card = self.library.get_board_card(card_id)
        if card is None or not card["path"]:
            return
        path = Path(card["path"])
        if not path.exists():
            self._notify("文件已不在原处",
                         f"{path}\n\n它可能在你的资料目录里被改名/移动了。\n"
                         "可以右键这张卡片「重新指定文件…」把它指回来。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_card_folder(self, card_id: int) -> None:
        """在文件管理器中打开这张素材卡片所指文件**所在的目录**。

        跟事件枝页的「打开所在文件夹」是同一件事，只是白板卡片刻的是库外的
        绝对路径（``board_cards.path``），不经过版本文件表 —— 所以不共用那个函数，
        但人话提示保持一致：文件没了就说清楚怎么修，别点了没反应。
        """
        card = self.library.get_board_card(card_id)
        path_text = card["path"] if card else ""
        if not path_text:
            self._notify("打不开目录", "这张卡片没有记录文件路径。")
            return
        folder = Path(path_text).parent
        if not folder.is_dir():
            self._notify("目录已不存在",
                         f"{folder}\n\n这份素材文件不在原来的地方了。"
                         "可以右键这张卡片「重新指定文件…」把它指回来。")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        self.show_message(f"已打开文件所在目录：{folder}")

    def _open_card_link(self, card_id: int) -> None:
        card = self.library.get_board_card(card_id)
        if card is None or not card["url"]:
            return
        QDesktopServices.openUrl(QUrl(card["url"]))

    def _relink_card(self, card_id: int) -> None:
        """给卡片重新指定文件 / 改链接。"""
        card = self.library.get_board_card(card_id)
        if card is None:
            return
        try:
            if card["kind"] == "file":
                chosen, _ = QFileDialog.getOpenFileName(
                    self, "重新指定这个卡片指向的文件", str(Path.home())
                )
                if not chosen:
                    return
                self.library.update_board_card(
                    card_id, path=chosen,
                    title=card["title"] or Path(chosen).name,
                )
            else:
                url = self._ask_text("修改链接", "网址：", card["url"])
                if not url:
                    return
                self.library.update_board_card(card_id, url=url.strip())
        except LibraryError as error:
            self._notify("修改失败", str(error))
            return
        self._refresh_board()
        self.show_message("卡片已更新")

    def _delete_board_selection(self) -> None:
        """按 Delete：说明框直接删、组问一句再解散、**事件不在这里删**。"""
        notes = [item.note.id for item in self.board_view._note_items.values()
                 if item.isSelected()]
        groups = [item.group.id for item in self.board_view._group_items.values()
                  if item.isSelected()]
        events = self.board_view.selected_document_ids()
        for note_id in notes:
            self._delete_board_note(note_id)
        for group_id in groups:
            self._delete_board_group(group_id)
        if events:
            self.show_message(
                f"选中的 {len(events)} 个事件枝没动 —— 删事件请右键卡片「删除事件…」"
            )

    def _on_board(self) -> bool:
        return self.view_stack.currentWidget() is self.board_view

    # ------------------------------------------------- 白板：说明框 / 分组 / 导出

    def _refresh_board(self) -> None:
        if self._project_id:
            # switch=False：这是"把版图重画一遍"，不是"用户要看版图" ——
            # 撤销/重做、挪卡片都会走到这里，绝不能顺手把视图切回去
            self.show_project_board(self._project_id, keep_view=True, switch=False)

    # ------------------------------------------------------------ 网格排列

    #: 上次用的"几行几列、间距"记在库里，下次打开还是这几个数
    GRID_OPTION_KEYS = ("board.grid_cols", "board.grid_rows", "board.grid_gap")
    #: 连线走向（枝叶弧线 / 直角折线）也记住
    EDGE_ROUTE_KEY = "board.edge_route"

    def _arrange_grid(self, document_ids=None, group_id: int = 0) -> None:
        """「网格排列…」：问一声几行几列，然后把这一批事件排整齐。

        位置只由 ``ProjectBoardView.grid_layout()`` 算，"写库 + 撤销"走和拖动
        完全相同的那条路（``_save_positions`` 的落点 + ``_record_move``），
        所以排错了 Ctrl+Z 就能回到原样。
        """
        from .arrange_dialog import GridArrangeDialog

        if self._project_id is None:
            return
        ids = [int(item) for item in (document_ids or [])]
        if not ids:
            ids = self.board_view.selected_document_ids()
        if len(ids) < 2:
            self.show_message("先框选 / Ctrl 点选两个以上事件枝，再排网格")
            return
        cols = self._grid_option_int(self.GRID_OPTION_KEYS[0])
        rows = self._grid_option_int(self.GRID_OPTION_KEYS[1])
        gap = self._grid_option_int(self.GRID_OPTION_KEYS[2]) or int(GRID_GAP)
        dialog = GridArrangeDialog(len(ids), cols, rows, gap, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        cols, rows, gap = dialog.values()
        self.library.set_ui_option(self.GRID_OPTION_KEYS[0], str(cols))
        self.library.set_ui_option(self.GRID_OPTION_KEYS[1], str(rows))
        self.library.set_ui_option(self.GRID_OPTION_KEYS[2], str(int(gap)))

        placed = self.board_view.grid_layout(ids, cols, rows, gap)
        if not placed:
            return
        before: dict[int, tuple[float, float]] = {}
        for document_id, _x, _y in placed:
            item = self.board_view.item_for(document_id)
            if item is not None:
                before[document_id] = (item.pos().x(), item.pos().y())
        moved = {document_id: (x, y) for document_id, x, y in placed}
        stored = set(self.library.document_positions(self._project_id))
        try:
            for document_id, (x, y) in moved.items():
                self.library.set_document_position(document_id, x, y)
        except LibraryError as error:
            self.show_message(f"排列失败：{error}")
            return
        self.board_view.apply_positions(moved)
        # 排的是某个组：让框跟着收紧围住它（手工拉开过的框也回到"跟着成员"）
        if group_id:
            try:
                self.library.set_group_auto(int(group_id))
            except LibraryError:
                pass
            self.board_view.refresh_groups()
        self._record_move(moved, before, stored, label="网格排列")
        self._apply_board_header(self.library.get_project(self._project_id),
                                self.board_view.graph())
        self.show_message(f"已把这 {len(moved)} 个事件枝排成 {cols} 列 × {rows} 行")

    def _grid_option_int(self, key: str) -> int:
        try:
            return int(self.library.get_ui_option(key, "0") or 0)
        except (TypeError, ValueError):
            return 0

    def _set_edge_route(self, orth: bool, *, save: bool = True) -> None:
        """"""
        route = EDGE_ROUTE_ORTH if orth else EDGE_ROUTE_CURVE
        self.board_view.set_edge_route(route)
        action = getattr(self, "action_edge_orth", None)
        if action is not None and action.isChecked() != orth:
            action.blockSignals(True)
            action.setChecked(orth)
            action.blockSignals(False)
        if save:
            self.library.set_ui_option(self.EDGE_ROUTE_KEY, route)
            self.show_message("连线改成直角折线了（拖中间的小圆点可以左右挪这道弯）"
                              if orth else "连线改回枝叶弧线了")

    def _add_board_note(self, x: float, y: float) -> None:
        """在画布空白处放一个说明文本框（只描述"这里发生了什么"）。"""
        if self._project_id is None:
            return
        x, y = self.board_view.free_spot_near(x, y)
        try:
            card_id = self.library.add_board_note(self._project_id, x, y)
        except LibraryError as error:
            self.show_message(f"加说明框失败：{error}")
            return
        self._refresh_board()
        self.show_message("加了一个说明框：双击就能写内容")
        row = self.library.get_board_card(card_id)
        if row is not None:
            self.undo_stack.push(
                "新建说明框",
                lambda: (self.library.delete_board_card(card_id), self._refresh_board()),
                lambda: (self.library.restore_board_card(row), self._refresh_board()),
            )

    def _rename_board_note(self, note_id: int, text: str) -> None:
        try:
            self.library.update_board_note(note_id, text=text)
        except LibraryError as error:
            self.show_message(f"保存说明失败：{error}")

    def _move_board_note(self, note_id: int, x: float, y: float) -> None:
        if x != x or y != y:
            return
        try:
            self.library.update_board_note(note_id, x=x, y=y)
        except LibraryError as error:
            self.show_message(f"保存说明框位置失败：{error}")

    def _delete_board_note(self, note_id: int) -> None:
        """删掉一张卡片（说明框 / 素材卡）——**可撤销**。"""
        row = self.library.get_board_card(note_id)
        if row is None:
            return
        try:
            self.library.delete_board_card(note_id)
        except LibraryError as error:
            self.show_message(f"删除卡片失败：{error}")
            return
        self._refresh_board()
        kind = "说明框" if row["kind"] == "note" else "素材卡片"
        if row["kind"] == "note" and not (row["text"] or "").strip():
            self.show_message("删掉了一个空的说明框")
        else:
            self.show_message(f"删掉了这个{kind}（Ctrl+Z 可以撤回来）")
        self.undo_stack.push(
            f"删除{kind}",
            lambda: self._restore_card(row),
            lambda: self.library.delete_board_card(note_id),
        )

    def _restore_card(self, row: dict) -> None:
        self.library.restore_board_card(row)
        self._refresh_board()

    def _create_board_group(self, document_ids) -> None:
        """把选中的一批事件归为一个组（名字问一下）。"""
        ids = [int(item) for item in (document_ids or [])]
        if self._project_id is None or not ids:
            self.show_message("先在画布上框选几个事件枝（Ctrl + 拖空白）或 Ctrl 点选")
            return
        name = self._ask_text(
            "新建分组", f"把这 {len(ids)} 个事件枝归为一组，组名：",
            default=f"{self._project_name()}分组",
        )
        if not name or not name.strip():
            return
        name = name.strip()
        try:
            group_id = self.library.create_group(self._project_id, name, ids)
        except LibraryError as error:
            self.show_message(f"建组失败：{error}")
            return
        self._refresh_board()
        self.show_message(f"已建组「{name}」（{len(ids)} 个事件枝）")
        # 进撤销栈：建组是拖动的副作用（拖进组框就会建），不进栈的话
        # 用户 Ctrl+Z 什么也撤不掉，只剩一个他并没要求过的组。
        # 写法照「新建空分组」那条：撤销 = 删组，重做 = 把组（含成员）恢复回来
        row = next((item for item in self._board_groups() if item["id"] == group_id), None)
        if row is not None:
            self.undo_stack.push(
                "新建分组",
                lambda: (self.library.delete_group(group_id), self._refresh_board()),
                lambda: (self.library.restore_group(row, ids), self._refresh_board()),
            )

    def _create_empty_group(self, x: float, y: float) -> None:
        """新建一个**空分组**：先摆好框，之后往里拖事件就自动加入（框自动变大）。"""
        if self._project_id is None:
            return
        name = self._ask_text("新建空分组", "分组名：",
                              default=f"{self._project_name()}分组")
        if not name or not name.strip():
            return
        try:
            group_id = self.library.create_group(
                self._project_id, name.strip(), [], x=x, y=y,
                w=DEFAULT_GROUP_W, h=DEFAULT_GROUP_H, manual=False,
            )
        except LibraryError as error:
            self.show_message(f"建组失败：{error}")
            return
        self._refresh_board()
        self.show_message(f"建了一个空分组「{name.strip()}」—— 往里拖事件就会加入")
        row = next((item for item in self._board_groups() if item["id"] == group_id), None)
        if row is not None:
            self.undo_stack.push(
                "新建分组",
                lambda: (self.library.delete_group(group_id), self._refresh_board()),
                lambda: (self.library.restore_group(row, []), self._refresh_board()),
            )

    def _rename_board_group(self, group_id: int) -> None:
        current = next((item for item in self._board_groups() if item["id"] == group_id), None)
        if current is None:
            return
        name = self._ask_text("改组名", "新的组名：", str(current.get("name") or ""))
        if not name or not name.strip():
            return
        note = self._ask_text(
            "组说明（可留空）", "这个组是干什么的（会写进导出的组信息里）：",
            default=str(current.get("note") or ""),
        )
        try:
            self.library.rename_group(group_id, name=name.strip(),
                                      note=note if note is not None else None)
        except LibraryError as error:
            self.show_message(f"改组失败：{error}")
            return
        self._refresh_board()

    def _delete_board_group(self, group_id: int) -> None:
        if not self._confirm(
            "解散这个组",
            "只去掉这个框和成员记录，事件和文件一个都不动。确定吗？",
        ):
            return
        row = next((item for item in self._board_groups() if item["id"] == group_id), None)
        try:
            self.library.delete_group(group_id)
        except LibraryError as error:
            self.show_message(f"解散失败：{error}")
            return
        self._refresh_board()
        self.show_message("已解散这个组（事件和文件都没动；Ctrl+Z 可以撤回来）")
        if row is not None:
            members = list(row.get("members") or [])
            self.undo_stack.push(
                "解散分组",
                lambda: (self.library.restore_group(row, members), self._refresh_board()),
                lambda: (self.library.delete_group(group_id), self._refresh_board()),
            )

    def _save_group_rect(self, group_id: int, x: float, y: float,
                         w: float, h: float) -> None:
        """用户把分组框拖大拖小了：记下来，并且从此不再跟着成员收紧。"""
        try:
            self.library.set_group_rect(group_id, x, y, w, h, manual=True)
        except LibraryError as error:
            self.show_message(f"保存分组框大小失败：{error}")
            return
        self.show_message("分组框大小已记下（不再自动跟着成员收紧）")

    def _set_group_membership(self, group_id: int, document_ids, add: bool) -> None:
        ids = [int(item) for item in (document_ids or [])]
        if not ids:
            return
        # 组变空时先把框的位置记住：不然空组会跑回原点
        rect = self.board_view.group_rect_for(group_id)
        current = next((item for item in self._board_groups()
                        if item["id"] == group_id), {})
        before_members = {int(item) for item in (current.get("members") or [])}
        was_empty = not before_members
        try:
            if add:
                self.library.add_group_members(group_id, ids)
            else:
                self.library.remove_group_members(group_id, ids)
        except LibraryError as error:
            self.show_message(f"改分组失败：{error}")
            return
        remaining = next((item for item in self._board_groups()
                          if item["id"] == group_id), {}).get("members") or []
        if not remaining and rect is not None and not was_empty:
            x, y, w, h = rect
            self.library.set_group_rect(group_id, x, y, w, h, manual=False)
        self._refresh_board()
        self.show_message(("已加入组" if add else "已移出组") + f"（{len(ids)} 个事件枝）")
        if {int(item) for item in remaining} == before_members:
            # 成员一个都没变（比如把已在组里的事件又"加入"一次）：不进撤销栈 ——
            # 一条撤了看不出任何变化的记录只会碍事
            return
        # 进撤销栈：把卡片拖进/拖出组框**是拖动的副作用**，
        # 用户常常是手滑拖进去的；不进栈的话 Ctrl+Z 撤不掉，
        # 卡片就莫名其妙留在组里了。加入和移出正好互为 undo/redo。
        self.undo_stack.push(
            "加入分组" if add else "移出分组",
            lambda: self._apply_group_membership(group_id, ids, not add),
            lambda: self._apply_group_membership(group_id, ids, add),
        )

    def _apply_group_membership(self, group_id: int, ids: list[int], add: bool) -> None:
        """撤销/重做"加入或移出组"（只动成员关系，再重画一遍画布）。"""
        try:
            if add:
                self.library.add_group_members(group_id, ids)
            else:
                self.library.remove_group_members(group_id, ids)
        except LibraryError as error:          # pragma: no cover - 罕见
            self.show_message(f"改分组失败：{error}")
            return
        self._refresh_board()

    def _set_edge_curve(self, from_id: int, to_id: int, curve: float) -> None:
        """调一条连线的弯曲度（存库 + 立刻重画）。"""
        # **先查再写**（和 _disconnect_events 里那条注释同一个道理）：
        # 原来先 set_link_curve 再 SELECT "旧值"，读到的就是刚写进去的新值 ——
        # abs(old - curve) 恒等于 0，撤销记录**永远不入栈**，
        # 于是弯度调完就再也退不回去了。
        old = self.library.db.query_one(
            "SELECT curve FROM document_links WHERE from_document_id = ? AND "
            "to_document_id = ?", (from_id, to_id))
        try:
            self.library.set_link_curve(from_id, to_id, curve)
        except LibraryError as error:
            self.show_message(f"调曲度失败：{error}")
            return
        for edge in self.board_view.edges():
            if (edge.source.node.document_id == from_id
                    and edge.target.node.document_id == to_id):
                edge.curve = float(curve)
                edge.update_path()
        self.show_message("已记下这条线的弯曲度")
        if old is not None and abs(float(old["curve"] or 0.0) - float(curve)) > 0.01:
            previous = float(old["curve"] or 0.0)

            def set_curve(value: float) -> None:
                self.library.set_link_curve(from_id, to_id, value)
                for edge in self.board_view.edges():
                    if (edge.source.node.document_id == from_id
                            and edge.target.node.document_id == to_id):
                        edge.curve = value
                        edge.update_path()

            self.undo_stack.push(
                "调连线弯度",
                lambda: set_curve(previous),
                lambda: set_curve(float(curve)),
            )

    def _export_selection(self, document_ids) -> None:
        ids = [int(item) for item in (document_ids or [])]
        if not ids:
            self.show_message("先在画布上框选要导出的事件枝（Ctrl + 拖空白）")
            return
        default = f"框选导出-{len(ids)}个事件枝"
        self._start_export(name=default, note="", source="项目白板上框选的一批事件",
                           document_ids=ids)

    def _export_board_group(self, group_id: int) -> None:
        group = next((item for item in self._board_groups() if item["id"] == group_id), None)
        if group is None:
            return
        name = f"{group.get('name') or '未命名组'}-{group_id}"
        self._start_export(name=name, note=str(group.get("note") or ""),
                           source=f"项目白板上的分组「{group.get('name') or '未命名'}」",
                           group_id=group_id)

    def _project_name(self) -> str:
        if self._project_id is None:
            return ""
        project = self.library.get_project(self._project_id)
        return project.name if project else ""

    def _board_groups(self) -> list[dict]:
        if self._project_id is None:
            return []
        try:
            return self.library.board_groups(self._project_id)
        except Exception:                    # 库异常不该把界面搞崩
            return []

    def _start_export(self, *, name: str, note: str, source: str,
                      document_ids: list[int] | None = None,
                      group_id: int | None = None,
                      target_dir: str | Path | None = None) -> None:
        """选一个目录，然后在后台线程里导（大项目复制很久，界面不能卡）。"""
        target = target_dir
        if target is None:
            target = QFileDialog.getExistingDirectory(
                self, "导出到哪个文件夹", str(Path.home())
            )
        if not target:
            return
        job = ExportJob(
            self.library_root, document_ids or [], target,
            name=name, note=note, source=source, group_id=group_id,
        )
        self._export_jobs[job.id] = job
        job.start()
        self.show_message(f"开始导出 → {target}")

    def _on_zip_finished(self, job: ZipJob, event: ZipFinished) -> None:
        if event.error:
            self._notify("打包失败", event.error)
            self.show_message(f"打包失败：{event.error}")
            return
        callback = getattr(job, "on_done", None)
        if callback is not None:
            callback(event.path, event.files, event.bytes)

    def _on_export_finished(self, job: ExportJob, event: ExportFinished) -> None:
        if event.error:
            self._notify("导出失败", event.error)
            self.show_message(f"导出失败：{event.error}")
            return
        result = event.result
        self.show_message(result.summary())
        details = [result.summary(), "", "目录里是：",
                   "· 组信息.md —— 这个组/这批数据是什么、包含哪些事件",
                   "· 导出清单.csv —— 每个文件的相对路径、大小、sha256、原始路径",
                   "· 每个事件枝一个文件夹，里面按环节分开（输入/分析/输出/参考/图示/其他）"]
        if result.missing:
            details += ["", f"⚠ 有 {len(result.missing)} 个文件已经不在原处（就地引用的"
                            "文件被挪走/删掉了），清单里能看到它们："]
            details += [f"   {title} / {item_name}" for title, item_name, _ in result.missing[:6]]
        if result.unresolved:
            details += ["", f"⚠ 有 {len(result.unresolved)} 个文件没有可用路径"]
        self._notify("导出完成", "\n".join(details))

    # ------------------------------------------------- 事件枝页：排版与信息框

    def _event_default_units(self) -> tuple[int, int]:
        """读回这个库里记着的"新方框默认格数"（没记过就用默认值）。"""
        try:
            side = int(self.library.get_ui_option("event.units", "") or DEFAULT_UNITS_W)
        except (TypeError, ValueError):
            return DEFAULT_UNITS_W, DEFAULT_UNITS_H
        return side, side

    def _save_event_layout(
        self, box_key: str, col: int, row: int, units_w: int, units_h: int
    ) -> None:
        """用户挪了 / 拉了某个方框：写进**全库共用的框体模板**。

        模板只有一份 —— 所以改完这一个，所有项目、所有事件下的节点
        下次打开都按这份排版来。
        """
        try:
            self.library.set_box_template_entry(box_key, col, row, units_w, units_h)
        except LibraryError as error:
            self.show_message(f"保存框体模板失败：{error}")

    def _save_uniform_size(self, units_w: int, units_h: int) -> None:
        """做了什么"统一大小"的动作：把它记成以后新方框的默认大小。"""
        try:
            self.library.set_ui_option("event.units", str(units_w))
        except LibraryError as error:
            self.show_message(f"保存方框大小失败：{error}")
            return
        self.show_message(f"新方框默认大小已记为 {units_w} × {units_h} 格")

    def _reset_event_layout(self, document_id: int = 0) -> None:
        """「整理排版」：清掉框体模板，所有事件都回到默认排法。"""
        try:
            self.library.clear_box_template()
        except LibraryError as error:
            self.show_message(f"整理排版失败：{error}")
            return
        self.event_view.reset_layout()
        self.show_message("框体已回到默认排法（所有事件都按这个显示）")

    def _on_event_field_changed(self, field: str, value) -> None:
        """事件枝页的信息框被改了：写库并把相关显示同步一下。

        这里**不重建画布**：重建会把正在编辑的输入框换掉、光标就丢了。
        """
        document_id = self._document_id
        if not document_id:
            return
        try:
            if field in ("task_date", "due_date", "progress"):
                self.library.set_document_field(document_id, field, value)
            elif field == "state":
                self.library.set_document_state(document_id, str(value))
            elif field == "people":
                self.library.update_document(document_id, people=str(value or ""))
            elif field == "version_note":
                # 说明是**按稿**存的（年轮可以不一样）
                if self._version_id:
                    self.library.set_version_note(self._version_id, str(value or ""))
            else:
                return
        except LibraryError as error:
            self._notify("保存失败", str(error))
            return

        document = self.library.get_document(document_id)
        self.sidebar.refresh_event_row(document_id)
        self._update_status()
        if document is not None:
            self._update_event_subtitle(document)
            if field == "state":
                # 只换方框的颜色，不重建（免得丢焦点）
                self.event_view.apply_state(document.state)
        self.show_message("已保存")

    #: 兼容旧名字（测试与内部调用都还在用 _open_document 表示"事件年轮"）
    def _open_document(self, document_id: int, version_id: int | None = None,
                       keep_view: bool = False) -> None:
        """事件的时间轴：所有环节在各时期的版本，箭头自动连起来。

        :param keep_view: 只是内容变了（改状态之后的刷新）—— 保镜头、别适应窗口。
            年轮图重建之后 ``fit_to_content`` 会把画面拉回"全都看得见"，
            连"这一稿被选中"引起的滚动也一并还原（见 zoom.restore_camera）。
        """
        document = self.library.get_document(document_id)
        if document is None:
            return
        shot = camera(self.graph_view) if keep_view else None
        if not keep_view:
            self._record_navigation({"kind": "event", "document_id": document_id,
                                     "show": "timeline"})
        self._document_id = document_id
        self._version_id = version_id or document.current_version_id
        self._stage_role = None
        self._goto_event_project(document)

        graph = build_version_graph(self.library, document_id)
        self.graph_view.set_graph(graph)
        self.view_stack.setCurrentWidget(self.graph_view)
        self.view_title.setText(document.title)
        details = [f"{document.version_count} 个版本"]
        if graph.time_span_text:
            details.append(graph.time_span_text)
        if graph.has_branches:
            details.append(f"{graph.lane_count} 条并行支线")
        self.view_subtitle.setText(" · ".join(details))
        if not keep_view:
            self.graph_view.fit_to_content()
        if self._version_id:
            self.graph_view.select_version(self._version_id)
        restore_camera(self.graph_view, shot)
        self._update_nav()
        self._sync_zoom_control()

    def open_stage_flow(self, document_id: int, role: str,
                        version_id: int | None = None,
                        keep_view: bool = False) -> None:
        """看某个环节的年轮。"""
        document = self.library.get_document(document_id)
        if document is None:
            return
        graph = build_stage_graph(self.library, document_id, role)
        if not graph.nodes:
            self.show_message(
                f"{document.title} 的「{ROLE_LABELS.get(role, role)}」还没有文件"
            )
            return
        shot = camera(self.graph_view) if keep_view else None
        if not keep_view:
            self._record_navigation(
                {"kind": "stage", "document_id": document_id, "role": role}
            )
        self._document_id = document_id
        self._stage_role = role
        self._version_id = version_id or document.current_version_id
        self._goto_event_project(document)
        self.graph_view.set_graph(graph)
        self.view_stack.setCurrentWidget(self.graph_view)

        role_label = ROLE_LABELS.get(role, role)
        self.view_title.setText(f"{document.title} · {role_label}")
        self.view_subtitle.setText(
            f"{len(graph.nodes)} 个稿 · "
            f"{DOC_STATE_LABELS.get(document.state, document.state)}"
        )
        if not keep_view:
            self.graph_view.fit_to_content()
        restore_camera(self.graph_view, shot)
        self._update_nav()
        self._sync_zoom_control()

    #: 兼容旧名字
    def _open_stage(self, document_id: int, role: str, version_id: int | None = None) -> None:
        self.open_stage_flow(document_id, role, version_id)

    def refresh_all(self) -> None:
        """重新读取库并刷新全部视图。

        顶栏那个「刷新」按钮删掉了（用户问"这个按钮的意义是什么"），但**这个函数
        必须留着**：库被改动的地方都是它自己调一遍 —— 归档/旧库导入结束、撤销/重做、
        目录监视回调、改名/删除/改状态、换库、启动。也就是说界面本来就是"该刷的时候
        自己刷"，不需要用户记得按按钮。窗口级的 Ctrl+R / F5 快捷键仍指向它
        （见 ``_build_toolbar`` 末尾的 ``refresh_action``）。
        """
        self.sidebar.rebuild()
        self._update_status()
        current = self.view_stack.currentWidget()
        if current is self.graph_view and self._document_id:
            if self._stage_role:
                self.open_stage_flow(self._document_id, self._stage_role, self._version_id)
            else:
                self._open_document(self._document_id, self._version_id)
        elif current is self.event_page and self._document_id:
            self.open_event(self._document_id, self._version_id)
        elif self._project_id:
            self.show_project_board(self._project_id)
        self.show_message("已刷新")

    # ---------------------------------------------------------------- 交互

    def _on_board_document_selected(self, document_id: int) -> None:
        """在版图上单击节点：右侧面板跟着切，但不离开版图。"""
        self._document_id = document_id
        document = self.library.get_document(document_id)
        self._version_id = document.current_version_id if document else None
        # 同步侧栏的选中（notify=False：否则又会绕回来切视图）
        self.sidebar.select_event(document_id, notify=False)

    def _save_position(self, document_id: int, x: float, y: float) -> None:
        """记住用户把节点拖到哪儿（NaN 表示清掉、回到自动布局）。"""
        try:
            if x != x or y != y:
                self.library.clear_document_position(document_id)
                self.show_message("这个节点已改回自动布局")
            else:
                self.library.set_document_position(document_id, x, y)
        except LibraryError as error:
            self.show_message(f"保存位置失败：{error}")
        if self._project_id:
            self.show_project_board(self._project_id, keep_view=True)

    def _save_positions(self, payload) -> None:
        """一次拖动的落点（可能带走一组卡片）：一次写库、只重画一遍，并记进撤销栈。"""
        positions, before = payload if isinstance(payload, tuple) else (payload, {})
        # 记下"动之前库里到底存没存过位置"：没存过的话，撤销应该是**清掉位置**
        # （回到自动布局），而不是给一个值 —— 否则从此这个节点就"手工摆放"了
        stored = set()
        if self._project_id:
            stored = set(self.library.document_positions(self._project_id))
        try:
            for document_id, (x, y) in positions.items():
                if x != x or y != y:
                    self.library.clear_document_position(document_id)
                else:
                    self.library.set_document_position(document_id, x, y)
        except LibraryError as error:
            self.show_message(f"保存位置失败：{error}")
            return
        # 就地更新坐标就行：不重建场景（不然拖一下闪一下，连续微调也没法用）
        self.board_view.apply_positions(positions)
        self._record_move(positions, before, stored)
        if self._project_id:
            self._apply_board_header(
                self.library.get_project(self._project_id), self.board_view.graph()
            )

    def _connect_events(self, source_id: int, target_id: int) -> None:
        """在版图上画一条线：建立一条**依赖**（``source`` 依赖 ``target``）。"""
        try:
            self.library.link_documents(source_id, target_id, DEPENDENCY_KIND, "依赖")
        except LibraryError as error:
            self.show_message(f"连线失败：{error}")
            return
        source = self.library.get_document(source_id)
        target = self.library.get_document(target_id)
        self.show_message(
            f"已连依赖：{source.title if source else source_id} 依赖 "
            f"{target.title if target else target_id}"
        )
        self.undo_stack.push(
            "连一条依赖",
            lambda: (self.library.unlink_documents(source_id, target_id, DEPENDENCY_KIND),
                     self._refresh_board()),
            lambda: (self.library.link_documents(source_id, target_id, DEPENDENCY_KIND,
                                                 "依赖"),
                     self._refresh_board()),
        )
        if self._project_id:
            self.show_project_board(self._project_id, keep_view=True)

    def _disconnect_events(self, source_id: int, target_id: int, kind: str) -> None:
        row = self.library.db.query_one(
            "SELECT kind, label, curve FROM document_links WHERE from_document_id = ? "
            "AND to_document_id = ? AND kind = ?", (source_id, target_id, kind))
        try:
            self.library.unlink_documents(source_id, target_id, kind)
        except LibraryError as error:
            self.show_message(f"删除连线失败：{error}")
            return
        self.show_message("已剪断这条连线（Ctrl+Z 可以接回来）")
        if row is not None:
            saved = dict(row)
            self.undo_stack.push(
                "剪断一条连线",
                lambda: (self.library.link_documents(source_id, target_id,
                                                     saved["kind"], saved["label"]),
                         self.library.set_link_curve(source_id, target_id,
                                                     saved["curve"] or 0.0, saved["kind"]),
                         self._refresh_board()),
                lambda: (self.library.unlink_documents(source_id, target_id, kind),
                         self._refresh_board()),
            )
        if self._project_id:
            self.show_project_board(self._project_id, keep_view=True)

    def _reset_project_layout(self) -> None:
        """把本项目所有节点摆好的位置清掉（**会先问一句**）。"""
        if self._project_id is None:
            return
        if not self._confirm(
            "重置位置",
            "把本项目所有事件摆好的位置都清掉、回到自动布局？\n"
            "（事件枝、文件、连线都不动，只是位置重新自动排）",
        ):
            return
        self.library.clear_project_positions(self._project_id)
        self.show_project_board(self._project_id)
        self.show_message("已重置为自动布局")

    def _on_search(self, text: str) -> None:
        # 关窗时输入框的防抖定时器可能还在响：库已经关了，别再查
        if self._shutting_down:
            return
        self.sidebar.set_filter(text)

    def _focus_first_hit(self) -> None:
        event_id = self.sidebar.first_match()
        if event_id:
            self.open_event(event_id)

    # ------------------------------------------------------------ 归档动作

    def _shown_version_id(self, document_id: int) -> int | None:
        """屏幕上**正摆着**的是这一稿吗？是就返回它，否则 ``None``。

        只看事件枝页：白板上往事件枝卡片拖文件时，``_document_id`` 可能还停在上次
        打开的那个事件枝上，但那一稿并没有摆在用户眼前（屏幕上是一张张卡片），
        不能拿它当落点。
        """
        if self._document_id != document_id or not self._version_id:
            return None
        if self.view_stack.currentWidget() is not self.event_page:
            return None
        return self._version_id

    def _draft_for_files(self, document_id: int) -> tuple[int | None, bool]:
        """这批文件该落进哪一稿 → ``(version_id, 能不能加)``。

        底部时间轴上点哪一稿，画布就在哪一稿，拖进来的文件就落在那一稿 ——
        不再像以前那样"一看是旧稿就拒收、要你先切回最新稿"。

        三种情况：

        * 事件一稿都没有（刚建的事件）→ ``(None, True)``：让归档走"建第一稿"
          那条路（它本来就是这个用途，不算"自动建新稿"）；
        * 画布上就是**最新稿**（或屏幕上没摆着这一稿，从白板卡片拖进来）
          → 直接落最新稿，不问；
        * 画布上是**旧稿**（用户在翻历史）→ 问一句落哪一稿（``_ask_draft_target``）。
          选旧稿 = 直接改那一稿的内容；这一手会动已有记录，所以必须让他看见
          取消 → ``(None, False)``，库里一个字节都不动。

        新的一稿仍然只能由「＋ 新增一稿」建出来 —— 这里绝不顺带建稿。
        """
        document = self.library.get_document(document_id)
        if document is None:
            return None, False
        latest = document.current_version_id
        shown = self._shown_version_id(document_id)
        if not shown or shown == latest:
            return latest, True
        chosen = self._ask_draft_target(document, shown, latest)
        if chosen is None:
            return None, False               # 用户取消：库里一个字节都不动
        return chosen, True

    def _ask_draft_target(self, document, shown: int, latest: int) -> int | None:
        """问一句"这批文件加进哪一稿" → 选中的稿 id；取消返回 ``None``。

        只有画布上正显示**旧稿**时才会走到这里。默认选中的是**正在看的这一稿**
        （他就是点着那一稿在操作的），但选项上把后果写清楚了：

        * 加进正在看的这一稿 = 直接改这一稿，它之后的稿里不会有这批文件
          （事件枝页上会标「最新稿未登记」）；
        * 加进最新稿 = 不动任何已有稿。

        自动化（``interactive=False``，没有人在看）时**不弹框、落最新稿** ——
        没人回答的时候，宁可只动最新稿，也不要悄悄改写历史稿。
        """
        versions = {item.id: item for item in self.library.version_history(document.id)}
        shown_version = versions.get(shown)
        latest_version = versions.get(latest)
        if shown_version is None or latest_version is None:
            return latest
        if not self.interactive:
            return latest
        labels = [
            f"v{shown_version.seq}（正在看的这一稿）",
            f"v{latest_version.seq}（最新稿）",
        ]
        choice, ok = QInputDialog.getItem(
            self, "这批文件加进哪一稿",
            f"画布上现在显示的是 v{shown_version.seq}（不是最新稿）。\n"
            f"加进 v{shown_version.seq}：直接改这一稿，"
            f"v{latest_version.seq} 里不会有它们（会标「最新稿未登记」）；\n"
            f"加进 v{latest_version.seq}：只动最新稿，已有的稿一份都不改。",
            labels, 0, False,
        )
        if not ok or not choice:
            return None
        return shown if choice == labels[0] else latest

    def _on_stage_drop(self, document_id: int, role: str, paths: list) -> None:
        """文件被拖进某个环节的方框（或方框右键"加文件"）。

        落点 = **当前这一稿**（``_draft_for_files``）：拖文件不再顺带建新稿，
        这一稿是用户点「＋ 新增一稿」时建好的。

        自动替换，渲染展示图片"）。所以这里挑出第一张图片、按 replace 语义加进去 ——
        这一稿里这个环节就只有这一张，界面上那张也会换成新的。
        """
        target_version_id, allowed = self._draft_for_files(document_id)
        if not allowed:
            return
        if paths:
            # 去重（同一路径拖两次会插两行同名文件）+ **把文件夹挑出来**：
            # 白板那边的规矩是"文件夹先问要不要打包成 zip"，事件枝页以前直接把路径
            # 交给归档，而 `attach_files` 对不是文件的路径是静默跳过的 ——
            # 用户拖了个文件夹进来，最后只得到一句"没有可归档的文件"。
            files, folders = self._clean_dropped_paths(paths)
            if folders:
                names = "、".join(item.name for item in folders[:3])
                self.show_message(
                    f"文件夹不直接收（{names}）：先把文件夹打包成 zip 再拖进来 —— "
                    "也可以拖到白板上，它会问你要不要打包"
                )
            paths = files
            if not paths:
                return          # 拖进来的全是文件夹：提示已经给过了，别再弹文件框
        else:
            label = ROLE_LABELS.get(role, role)
            if role == "figure":
                paths = self._ask_files("选择放进「图示」的那张图（只放一张）",
                                        images_only=True)
            else:
                paths = self._ask_files(f"选择要放进「{label}」的文件")
            if not paths:
                return
        if role == "figure":
            chosen = [path for path in paths if is_image_name(Path(path).name)]
            if not chosen:
                self.show_message(
                    "图示只放图片（png/jpg/bmp/gif/webp/tif/svg）—— 这次拖进来的都不是图片"
                )
                return
            if len(chosen) > 1:
                self.show_message(
                    f"图示只放一张图：用第一张「{Path(chosen[0]).name}」，"
                    f"其余 {len(chosen) - 1} 个没放进去"
                )
            self._run_archive(document_id=document_id, paths=[chosen[0]],
                              stage_role=role, stage_replace=True,
                              target_version_id=target_version_id)
            return
        self._run_archive(document_id=document_id, paths=list(paths), stage_role=role,
                          target_version_id=target_version_id)

    def _add_new_version(self, document_id: int, role: str, version_id: int) -> None:
        """某个文件卡片上右键「加入到这一稿…」：选文件后加进**当前这一稿**。

        ``version_id`` 是那行文件自己所属的稿（未登记的文件卡片上它可能是更早的稿），
        这里**不用它**：右键的语义是"往我现在看的这一稿里加"，落在哪一稿由
        ``_draft_for_files`` 说了算 —— 老代码拿它去建新稿，正是"拖个文件就多一稿"的来源。
        """
        target_version_id, allowed = self._draft_for_files(document_id)
        if not allowed:
            return
        paths = self._ask_files("选择要加进这一稿的文件")
        if not paths:
            return
        self._run_archive(document_id=document_id, paths=list(paths), stage_role=role,
                          target_version_id=target_version_id)

    def _move_file_role(self, document_id: int, version_id: int, file_id: int,
                        role: str) -> None:
        """事件枝页里把一行文件从一个环节方框**拖到另一个方框** = 改这一行的环节。

        **为什么要推到下一次事件循环再做**：这个调用发生在文件行自己那次
        ``QDrag.exec()`` 里（drop 事件是在拖动循环内部同步送达的）。而办完之后要
        重画事件枝页（``set_event`` → ``scene.clear()``），那会把**发起这次拖动的那个
        FileRow 连同它的容器一起删掉** —— 拖动循环返回时就在摸一个已经析构的对象。
        所以真正的活交给 ``QTimer.singleShot(0)``：等这次拖放彻底结束再动界面；
        ``_apply_file_role_move`` 里还会再确认一次"拖放结束了没有"。
        """
        QTimer.singleShot(0, lambda: self._apply_file_role_move(
            document_id, version_id, file_id, role))

    def _apply_file_role_move(self, document_id: int, version_id: int, file_id: int,
                              role: str, *, record: bool = True) -> None:
        """真正去改环节（撤销/重做也走这里，只是不再往栈上记一条）。

        `record=False` 的用法和 :meth:`_apply_group_membership` 一样：
        undo/redo 的闭包只在"库里的事实"上来回走，不该再压一条新记录进去。
        """
        if record and self.event_view.is_file_row_dragging():
            # 这次拖放**还没结束**。`QDrag::exec()` 跑的是自己的事件循环，
            # 它也会处理定时器 —— 0ms 排进来的这一手有可能在 exec() 返回之前
            # 就烧起来，那会儿重画会把发起拖动的那个 FileRow 删掉，而拖动循环
            # 还在用它。所以只要还"在拖"就再排一次：等 exec() 真的返回
            # （FileRow 在 finally 里把标志清掉）再动库和界面。
            QTimer.singleShot(0, lambda: self._apply_file_role_move(
                document_id, version_id, file_id, role))
            return
        version = self.library.get_version(version_id)
        if version is None:
            return
        target = next((item for item in version.files if item.id == file_id), None)
        if target is None:
            return
        old_role = target.role
        if old_role == role:
            return                  # 拖回原来的方框：什么都不做（也不用提示，本来就没改）
        if role == "figure" and not is_image_name(target.name):
            # 图示框只放图片，这条规矩拖进来和"放进图示"是一条（见 _on_stage_drop）：
            # 不拦的话方框里会多一行"不是图片，没法渲染"，谁也看不懂它为什么在那儿
            self.show_message(
                f"图示只放图片（png/jpg/bmp/gif/webp/tif/svg）：「{target.name}」不是图片，"
                "没挪过去"
            )
            return
        try:
            self.library.move_file_to_role(version_id, file_id, role)
        except LibraryError as error:
            self._notify("改环节失败", str(error))
            return
        # 说清楚是**哪一稿**里改的：画布上可能正显示着"最新稿未登记"的行
        # （那一行属于更早的稿），用户看到 "（v3）" 就知道自己改的是哪一份记录
        self._after_write(
            f"已把「{target.name}」从「{ROLE_LABELS.get(old_role, old_role)}」"
            f"移到「{ROLE_LABELS.get(role, role)}」（v{version.seq}）"
        )
        self._refresh_current_view()
        if not record:
            return
        self.undo_stack.push(
            f"把「{target.name}」移到{ROLE_LABELS.get(role, role)}",
            lambda: self._apply_file_role_move(document_id, version_id, file_id,
                                               old_role, record=False),
            lambda: self._apply_file_role_move(document_id, version_id, file_id,
                                               role, record=False),
        )

    def _on_archive_requested(self, target_id: int, paths: list, mode: str) -> None:
        """统一处理"拖入归档"请求（现在只剩"拖到侧栏**项目树行** = 新建事件枝"）。

        以前这里还有两条模式：

        * ``"document-attach"``（拖到侧栏**事件枝行** = 给那个事件枝建新的一稿）——
          那边现在只给一句指路的话）；
        * ``"stage-*"``（右侧详情面板按环节归档）—— 那个面板早就没了，没有调用方。

        一条"没人走、但一被调用就会凭空多出一稿"的老路，比删掉它危险得多，
        所以这里只留项目树行那一条，别的模式明说已经取消 ——
        别的模块再发过来时，用户看到的是一句解释，而不是默默多出来的一稿。
        """
        if mode != "project":
            self.show_message(
                "这个拖放目标已经取消了：要往事件里加文件，"
                "请拖到事件枝页的环节方框里（或白板上的事件枝卡片上再选环节）"
            )
            return
        # 拖到项目上：各自成为新事件枝
        self._run_archive(project_id=target_id, paths=list(paths))

    def _ask_files(self, title: str = "选择文件", *,
                   images_only: bool = False) -> list[str]:
        if not self.interactive:
            return []
        file_filter = ("图片 (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff *.svg)"
                       if images_only else "所有文件 (*)")
        files, _ = QFileDialog.getOpenFileNames(self, title, str(Path.home()),
                                               file_filter)
        return files

    def _run_archive(
        self, *, node_id: int | None = None, document_id: int | None = None,
        project_id: int | None = None, paths: list[str],
        as_primary: bool = False, stage_role: str | None = None,
        stage_replace: bool = False, target_version_id: int | None = None,
    ) -> None:
        """发起归档；若上一个归档还在跑，就排队等它结束。

        ``target_version_id`` 给了就表示"加进这一稿"（不新建版本）；
        不给还是老语义"归档成新的一稿"（拖到侧栏事件枝行、从别处拖到事件上等）。
        """
        if not paths:
            return
        request = {
            "node_id": node_id,
            "document_id": document_id,
            "project_id": project_id,
            "paths": list(paths),
            "as_primary": as_primary,
            "stage_role": stage_role,
            "stage_replace": stage_replace,
            "target_version_id": target_version_id,
        }
        if self._archive_jobs:
            self._pending_archives.append(request)
            self.show_message(
                f"已有归档任务在进行，已排队（待处理 {len(self._pending_archives)} 批）"
            )
            return
        self._start_archive(request)

    def _make_progress_dialog(self, title: str, label: str,
                              maximum: int) -> QProgressDialog:
        """做一个**能关掉**的进度框。

        两条都必须遵守：

          但只要任务异常逃出 ``run()``（没发 ``*Finished``），这个 WindowModal
          框就**永远关不掉**，主窗口被彻底挡住 —— 用户只能杀进程。
          这里的按钮写"隐藏（后台继续）"：它只关框，**不取消**后台任务
          （真正的取消要另做一套线程间协议，这里不做假的承诺）。
        * ``setAutoReset(False)``：否则 ``setValue(maximum)`` 会顺手 reset，
          进度框在我们还没关它之前就自己弹回来一次。
        """
        dialog = QProgressDialog(label, "隐藏（后台继续）", 0, max(1, int(maximum)),
                                 self)
        dialog.setWindowTitle(title)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)
        return dialog

    def _finish_progress_dialog(self, job_id: int) -> None:
        """结束（关掉）某个任务自己的进度框 —— **只关自己那个**。"""
        dialog = self._progress_dialogs.pop(job_id, None)
        if dialog is None:
            return
        if not dialog.wasCanceled():
            # 用户已经用"隐藏"把框收起来了：别再 setValue —— QProgressDialog
            # 的 setValue 会把收起来的框**又显示出来**（闪一下很碍眼）
            dialog.setValue(dialog.maximum())
        dialog.close()

    def _start_archive(self, request: dict) -> None:
        paths = request["paths"]
        self.show_message(f"正在归档 {len(paths)} 个文件…")

        job = ArchiveJob(
            self.library_root,
            node_id=request["node_id"],
            document_id=request["document_id"],
            project_id=request["project_id"],
            paths=paths,
            as_primary=request["as_primary"],
            stage_role=request.get("stage_role"),
            stage_replace=request.get("stage_replace", False),
            target_version_id=request.get("target_version_id"),
            request=request,
        )
        dialog = self._make_progress_dialog("归档", "正在归档…", len(paths))
        # **按 job id 存**：两个任务叠在一起时（归档 + 旧库导入），
        # 以前共用一个 self._progress_dialog，先结束的那个会把别人的框一起关掉，
        # 剩下那个任务就在没框的情况下跑（用户以为它没在动）。
        self._progress_dialogs[job.id] = dialog
        self._archive_jobs[job.id] = job
        self._job_requests[job.id] = request
        job.start()

    # ---------------------------------------------------------- 后台事件轮询

    def has_pending_work(self) -> bool:
        return bool(self._archive_jobs or self._import_jobs or self._export_jobs
                    or self._zip_jobs or self._word_jobs or self._copy_jobs
                    or self._pending_archives)

    def poll_jobs(self) -> None:
        for job_id, job in list(self._archive_jobs.items()):
            for event in job.drain_events():
                if isinstance(event, ArchiveProgress):
                    self._on_archive_progress(event)
                elif isinstance(event, ArchiveFinished):
                    self._on_archive_finished(job, event)
            if not job.is_running() and not job.events.qsize():
                self._archive_jobs.pop(job_id, None)
                self._job_requests.pop(job_id, None)
                if not self._archive_jobs and self._pending_archives:
                    self._start_archive(self._pending_archives.pop(0))

        for job_id, job in list(self._import_jobs.items()):
            for event in job.drain_events():
                if isinstance(event, ImportProgress):
                    self._on_import_progress(event)
                elif isinstance(event, ImportFinished):
                    self._on_import_finished(job, event)
            if not job.is_running() and not job.events.qsize():
                self._import_jobs.pop(job_id, None)

        for job_id, job in list(self._export_jobs.items()):
            for event in job.drain_events():
                if isinstance(event, ExportProgress):
                    self.show_message(
                        f"导出中 {event.done}/{event.total}：{event.message}"
                    )
                elif isinstance(event, ExportFinished):
                    self._on_export_finished(job, event)
            if not job.is_running() and not job.events.qsize():
                self._export_jobs.pop(job_id, None)

        for job_id, job in list(self._zip_jobs.items()):
            for event in job.drain_events():
                if isinstance(event, ZipProgress):
                    self.show_message(
                        f"打包中 {event.done}/{event.total}：{event.message}"
                    )
                elif isinstance(event, ZipFinished):
                    self._on_zip_finished(job, event)
            if not job.is_running() and not job.events.qsize():
                self._zip_jobs.pop(job_id, None)

        for job_id, job in list(self._word_jobs.items()):
            for event in job.drain_events():
                if isinstance(event, WordCompareFinished):
                    self._on_word_compare_finished(event)
            if not job.is_running() and not job.events.qsize():
                self._word_jobs.pop(job_id, None)

        for job_id, job in list(self._copy_jobs.items()):
            for event in job.drain_events():
                if isinstance(event, FileCopyFinished):
                    self._on_file_copy_finished(event)
            if not job.is_running() and not job.events.qsize():
                self._copy_jobs.pop(job_id, None)

        while True:
            try:
                event = self._watch_events.get_nowait()
            except queue.Empty:
                break
            self._on_watcher_outcome(event)

        self._maybe_collect_garbage()

    def _on_archive_progress(self, event: ArchiveProgress) -> None:
        dialog = self._progress_dialogs.get(event.job_id)
        if dialog is None or dialog.wasCanceled():
            return      # 用户把框收起来了：别用 setValue 把它又拽出来
        dialog.setValue(event.done)
        dialog.setLabelText(event.message)

    def _on_archive_finished(self, job: ArchiveJob, event: ArchiveFinished) -> None:
        self._finish_progress_dialog(job.id)

        target = self._job_requests.get(job.id, {}) or job.request
        self._update_status()
        self.sidebar.rebuild()

        document_id = target.get("document_id")
        # 落进哪一稿就**停在那一稿**：用户可能是特意在旧稿上加的文件
        # （见 ``_ask_draft_target``），归档完再把他弹回最新稿，等于白选了。
        landed = self._landed_version(document_id, target.get("target_version_id"))
        document = self.library.get_document(document_id) if document_id else None
        if document_id:
            self.open_event(document_id, landed)
        elif getattr(event, "created", None):
            # 一次拖了多个文件就跳到最后一个（刚建好的那个）
            newest = event.created[-1]
            self.sidebar.select_event(newest, notify=False)
            self.open_event(newest)
        elif self._document_id:
            self.open_event(self._document_id)
        elif self._project_id:
            self.show_project_board(self._project_id)

        where = ""
        if landed and document is not None and landed != document.current_version_id:
            version = self.library.get_version(landed)
            # 落在**旧稿**上要说明白：屏幕上没有别的地方会提示"刚加的这些没进最新稿"
            where = f"（加进 v{version.seq}，不是最新稿）" if version else ""
        self.show_message(
            f"归档完成：成功 {event.ok} 个"
            + (f"，失败 {event.failed} 个" if event.failed else "")
            + where
        )
        if event.failed:
            self._notify("部分文件未归档", "\n".join(event.summaries[:6]) or "详见状态栏")

    def _landed_version(self, document_id: int | None, version_id: int | None) -> int | None:
        """归档落点那一稿现在还能打开吗？能就返回它，不能就 ``None``（回最新稿）。

        归档是后台任务，回来时那一稿有可能已经被删了；拿一个不存在的 id 去开
        事件枝页，用户看到的是一片空白 —— 不如老实回最新稿。
        """
        if not document_id or not version_id:
            return None
        version = self.library.get_version(version_id)
        if version is None or version.document_id != document_id:
            return None
        return version_id

    def _maybe_collect_garbage(self) -> None:
        """只在没有后台任务时做循环回收（见 GC_IDLE_INTERVAL_S 的说明）。"""
        if self.has_pending_work():
            return
        now = time.monotonic()
        if now - self._last_gc < GC_IDLE_INTERVAL_S:
            return
        self._last_gc = now
        gc.collect()

    # ------------------------------------------------------------ 版本动作

    def _stage_one_file(self, version_id: int, file_id: int) -> list[Path]:
        """把某一份文件摆到暂存区（拖出去时用），文件名保持原样。"""
        version = self.library.get_version(version_id)
        if version is None:
            return []
        target = next((item for item in version.files if item.id == file_id), None)
        if target is None:
            return []
        try:
            source = self.library.blob_path(version_id, file_id)
            if source is None or not source.is_file():
                return []
            from ..core.util import safe_filename

            staging = self.library.root / "exports" / ".drag"
            staging.mkdir(parents=True, exist_ok=True)
            destination = staging / safe_filename(target.name)
            if not destination.exists() or destination.stat().st_size != target.size:
                # 引用模式下外面那份是用户的原始文件，硬链接出去会有被就地改写的风险，
                # 所以这里老老实实复制一份到暂存区
                import shutil as _shutil

                _shutil.copyfile(source, destination)
            return [destination]
        except LibraryError:
            return []

    # ------------------------------------------------- 用 Word 自己的比较

    def version_targets(self, document_id: int, shown_version_id: int) -> dict:
        """这一页的"正在看的稿"与"最新稿"（右键菜单要照着它写出稿号）。

        菜单上光写「到最新稿」会让人按错稿；所以这里把两边都问清楚，
        由菜单写出 ``v2`` / ``v3`` 具体指哪一个。
        """
        document = self.library.get_document(document_id)
        if document is None:
            return {}
        shown = self.library.get_version(shown_version_id or document.current_version_id)
        latest = self.library.get_version(document.current_version_id)
        info: dict = {}
        if shown is not None:
            info["shown"] = {"id": shown.id, "seq": shown.seq}
        if latest is not None:
            info["latest"] = {"id": latest.id, "seq": latest.seq}
        return info

    def _register_file_to_version(self, source_version_id: int, file_id: int,
                                  target_version_id: int) -> None:
        """右键「复制一份到这一稿 / 到最新稿」：把这份文件**真的复制一份**登记进目标稿。

        三件事必须说清（都在这段里）：

          "我可能是把 V1 提到 V2，而 V3 又有文件了"），绝不自己猜"最新稿"；
          而不是仅仅复制链接"）—— 确认框里把大小写出来，别让人以为不占地方；
        * 复制 + 算指纹可能是几十秒（GB 级图纸），所以**丢给后台任务**
          （``FileCopyJob``），界面不卡。
        """
        source = self.library.get_version(source_version_id)
        target = self.library.get_version(target_version_id)
        if source is None or target is None:
            return
        item = next((row for row in source.files if row.id == file_id), None)
        if item is None:
            return
        role_label = ROLE_LABELS.get(item.role, item.role)
        if not self._confirm(
            "复制一份到这一稿",
            f"把「{item.name}」复制一份到 v{target.seq} 的「{role_label}」吗？\n\n"
            f"它在库里会**多出一份实体文件**（{human_size(item.size)}，"
            "放在库的 files/ 下，与原来那一份各管各的）；\n"
            f"不新建稿，你磁盘上的原文件一个字节都不动。",
        ):
            return
        job = FileCopyJob(self.library_root, source_version_id, file_id,
                          target_version_id)
        self._copy_jobs[job.id] = job
        job.start()
        self.show_message(
            f"正在把「{item.name}」复制一份到 v{target.seq}…（后台进行，可以继续用）"
        )

    def _on_file_copy_finished(self, event: FileCopyFinished) -> None:
        """复制结束：成功了停在原地看着那一行不再是"未登记"，失败就说清为什么。"""
        if not event.ok:
            self._notify("复制到这一稿失败", event.error or "原因不明")
            self.show_message(f"复制到这一稿失败：{event.error or '原因不明'}")
            return
        result = event.result or {}
        if not result.get("added"):
            # 目标稿里本来就有同名的一份：不重复登记，但要说清它在哪个环节
            self.show_message(
                f"v{result.get('target_seq')} 里已经有「{result.get('name')}」"
                f"（{result.get('existing_role_label')}），没有重复登记"
            )
            return
        # **先刷新画布、再写状态栏**：刷新会重建事件枝页，而重建前那句
        # 「把没提交的说明框落库」会往状态栏写一句"已保存"—— 顺序反了的话，
        # 用户看到的就是"已保存"，而不是"已把 x 复制到 v2"。
        self._refresh_current_view()
        self._after_write(
            f"已把「{result.get('name')}」复制一份到 v{result.get('target_seq')} 的"
            f"「{result.get('role_label')}」（库内新建一份，"
            f"{human_size(result.get('size') or 0)}）"
            + (f"；它原来登记在 v{result.get('source_seq')}"
               if result.get("source_seq") != result.get("target_seq") else "")
        )

    def word_compare_info(self, version_id: int, file_id: int) -> dict:
        """右键文件菜单里那一块要的全部信息（见 ``Library.word_compare_candidates``）。

        只做**廉价**的查询（查库 + stat），因为右键菜单是等着的；
        真正的活（配对守门 + 起 Word）在 :meth:`_word_compare` 里丢给后台线程。
        """
        try:
            return self.library.word_compare_candidates(version_id, file_id)
        except LibraryError:
            return {}

    def _word_compare(self, version_id: int, file_id: int, old_version_id: int) -> None:
        """右键「用 Word 比较 → 与 vN 比较」：后台起 Word 比一份带修订的文档出来。"""
        job = WordCompareJob(self.library_root, version_id, file_id, old_version_id)
        self._word_jobs[job.id] = job
        job.start()
        # 说清"要等一会儿"，否则用户以为点了没反应（Word 冷启动实测十几秒）
        self.show_message("正在用 Word 比较…（要启动 Word，第一次可能要十几秒）")

    def _on_word_compare_finished(self, event: WordCompareFinished) -> None:
        """Word 比完了：成功就把结果摆到用户面前，失败就如实说清为什么。

        **成功不弹模态框**：Word 已经被留在前台显示那份带修订的文档了，
        再弹一个"完成"框等于让用户多关一次窗。失败必须弹 ——
        那时候界面上什么都没发生，只写状态栏等于没说。
        """
        if not event.ok:
            reason = event.error or "原因不明"
            self._notify("用 Word 比较失败", reason)
            # 状态栏也留着原因：真实用户扫一眼状态栏就该知道为什么没比成，
            # 而提示框关掉之后状态栏是唯一还留在那儿的线索。
            self.show_message(
                f"用 Word 比较失败：{reason}"
                + (f"（{event.labels}）" if event.labels else "")
            )
            return
        where = event.labels or ""
        self.show_message(
            f"Word 比较完成：{event.message}"
            + (f"（{where}）" if where else "")
            + f" · 结果：{event.output}"
        )

    def _open_one_file(self, version_id: int, file_id: int) -> None:
        """打开某一份具体文件。"""
        path = self.library.blob_path(version_id, file_id)
        if path is None or not path.is_file():
            self._notify("打不开", "这个文件已不在原处，可先「重新绑定失效文件…」")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self.show_message(f"已用系统默认程序打开：{path.name}")

    def _export_one_file(self, version_id: int, file_id: int) -> None:
        """把某一份文件导出到用户选的目录（保留原名）。"""
        version = self.library.get_version(version_id)
        target = next((item for item in version.files if item.id == file_id), None) if version else None
        if target is None:
            return
        if not self._confirm(
            "导出文件",
            f"导出 v{version.seq} 的「{target.name}」？\n\n"
            "（操作的是这一稿的这份文件）",
        ):
            return
        destination = QFileDialog.getExistingDirectory(self, "选择导出目录", str(Path.home()))
        if not destination:
            return
        source = self.library.blob_path(version_id, file_id)
        if source is None or not source.is_file():
            self._notify("导出失败", "这个文件已不在原处")
            return
        from ..core.util import safe_filename, unique_path
        import shutil as _shutil

        out = unique_path(Path(destination) / safe_filename(target.name))
        try:
            _shutil.copyfile(source, out)
        except OSError as error:
            # 权限不足 / 磁盘满 / 目标被占用…以前这些异常**直接逃出 Qt 槽**：
            # 用户点一下"导出"什么也没发生（异常被 Qt 吞在事件循环里），
            # 文件当然是没导出来，但他完全不知道为什么。
            self._notify("导出失败", str(error))
            return
        self.show_message(f"已导出：{out}")

    def _export_files(self, items) -> None:
        """多选菜单「导出 N 个文件到…」：选一个目录，把这 N 份一次性导出去。

        ``items`` 是 ``[(version_id, file_id), …]``。中间某一份取不回内容时**不整批失败**，
        逐份说明（和单文件导出同一条纪律：失败要说话，别静默跳过）。
        """
        items = list(items or [])
        if not items:
            return
        resolved: list[tuple[object, int, str]] = []
        for version_id, file_id in items:
            version = self.library.get_version(version_id)
            if version is None:
                continue
            name = next((f.name for f in version.files if f.id == file_id), None)
            if name is None:
                continue
            resolved.append((version, file_id, name))
        if not resolved:
            return
        lines = "".join(f"\n  v{v.seq} · {name}" for v, _fid, name in resolved)
        if not self._confirm(
            "导出文件",
            f"导出 {len(resolved)} 个文件（版本号如下）：{lines}\n\n继续？",
        ):
            return
        destination = QFileDialog.getExistingDirectory(self, "选择导出目录", str(Path.home()))
        if not destination:
            return
        from ..core.util import safe_filename, unique_path
        import shutil as _shutil

        done = 0
        failed: list[str] = []
        for version, file_id, name in resolved:
            source = self.library.blob_path(version.id, file_id)
            if source is None or not source.is_file():
                failed.append(name)
                continue
            out = unique_path(Path(destination) / safe_filename(name))
            try:
                _shutil.copyfile(source, out)
            except OSError as error:
                failed.append(f"{name}（{error}）")
                continue
            done += 1
        if failed:
            self._notify("部分未导出", "\n".join(failed[:6]) or "详见状态栏")
        self.show_message(
            f"已导出 {done} 个文件" + (f"，失败 {len(failed)} 个" if failed else "")
        )

    def _open_file_folder(self, version_id: int, file_id: int) -> None:
        """在文件管理器中打开这份文件**所在的目录**（右键菜单「打开所在文件夹」）。

        路径用公开接口 ``Library.file_path(version_file)`` 解析：引用模式（文件留在原处）
        给的是 ``version_files.location``，库内存储（copy/link）给的是 blob 路径 ——
        两条路都在那一个函数里，这里不自己拼路径（拼错了就是打开一个不存在的目录）。

        失败必须**说话**：以前 ``blob_path()`` 返回 None 时这里直接 return，
        用户点了菜单像没点一样。现在分三种情况给不同的人话。
        """
        version = self.library.get_version(version_id)
        target = None
        if version is not None:
            target = next((item for item in version.files if item.id == file_id), None)
        path = self.library.file_path(target)
        if path is None:
            self._notify("打不开目录",
                         "这一稿里找不到这份文件（可能它已经被删掉了）。")
            return
        folder = path.parent
        if not path.is_file():
            # 文件本身不在了（引用模式下最常见：被挪走/改名）。
            # 跟右键菜单里"置灰"保持一致：这里也不装作打开了，而是把
            # **它原来所在的目录**告诉他 —— 找文件的人第一步就是去那个目录翻。
            if folder.is_dir():
                self._notify("文件已不在原处",
                             f"{path}\n\n它原来在那个目录里：{folder}\n"
                             "可以用右键里的「重新绑定失效文件…」把它指回来。")
            else:
                self._notify("目录已不存在",
                             f"{folder}\n\n这份文件（以及它所在的目录）都不在原处了。"
                             "可以用右键里的「重新绑定失效文件…」把它指回来。")
            return
        if not folder.is_dir():
            # 理论上到不了（文件都在）；防一手"文件被移走的瞬间"这类竞态
            self._notify("打不开目录", f"目录不存在：{folder}")
            return
        # 「打开所在文件夹」= 打开**这份文件现在所在的那个目录**：
        # 库内副本（copy/link）就打开库的 files/<别名>/；引用模式就打开原文件目录。
        # 原始导入目录是另一个菜单项「打开原始目录」的事，别在这里混淆。
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        self.show_message(f"已打开文件所在目录：{folder}")

    def _open_origin_folder(self, version_id: int, file_id: int) -> None:
        """打开这份文件**最初导入时**所在的目录。"""
        version = self.library.get_version(version_id)
        target = next((item for item in version.files if item.id == file_id), None) if version else None
        if target is None or not target.source_path:
            self.show_message("这份文件没有记录原始目录")
            return
        folder = Path(target.source_path)
        if not folder.is_dir():
            self._notify("原始目录已不存在", str(folder))
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _delete_files(self, document_id: int, items) -> None:
        """删除文件 = 从文件**所属的那一稿**删掉这几行，不自动新增一稿。

        ``items`` = ``[(version_id, file_id), …]``。每个文件删它自己那一稿
        "如果当前选中的版本存在这个文件，操作对象就是当前版本；如果不存在，
        就是最近一个版本"。删前弹确认框**写明版本号**；确认后删行，
        并回收库里没有别的稿再引用的副本（``delete_files_from_version`` 内部 gc）。
        """
        items = list(items or [])
        if not items:
            return
        resolved: list[tuple[object, int, str]] = []
        for version_id, file_id in items:
            version = self.library.get_version(version_id)
            if version is None:
                continue
            name = next((f.name for f in version.files if f.id == file_id), None)
            if name is None:
                continue
            resolved.append((version, file_id, name))
        if not resolved:
            return
        lines = "".join(f"\n  v{v.seq} · {name}" for v, _fid, name in resolved)
        if not self._confirm(
            "删除文件",
            f"将从这些版本里删掉 {len(resolved)} 个文件：{lines}\n\n"
            "不会新增一稿；这些文件若没有别的稿再引用，会**从库里删掉副本**。\n"
            "磁盘上的原文件不动。",
        ):
            return
        by_version: dict[int, list[int]] = {}
        for version, file_id, _name in resolved:
            by_version.setdefault(version.id, []).append(file_id)
        removed = 0
        try:
            for version_id, file_ids in by_version.items():
                self.library.delete_files_from_version(version_id, file_ids)
                removed += len(file_ids)
        except LibraryError as error:
            self._notify("删除失败", str(error))
            return
        self._after_write(f"已删除 {removed} 个文件")
        self.open_event(document_id, self._shown_version_id(document_id))

    def _add_files_to_stage(self, document_id: int, role: str) -> None:
        """往某个环节添加文件（方框右键 / 文件卡片右键，选文件后加进**当前这一稿**）。

        与"把文件拖进方框"是同一条语义（见 ``_draft_for_files``）：加进正在看的
        那一稿，不新建稿 —— 新稿只能由「＋ 新增一稿」建。
        """
        target_version_id, allowed = self._draft_for_files(document_id)
        if not allowed:
            return
        paths = self._ask_files(f"选择要放进「{ROLE_LABELS.get(role, role)}」的文件")
        if paths:
            self._run_archive(document_id=document_id, paths=list(paths), stage_role=role,
                              target_version_id=target_version_id)

    def _stage_files(self, version_id: int) -> list[Path]:
        try:
            return self.library.stage_export(version_id)
        except LibraryError:
            return []

    def _open_version_file(self, version_id: int) -> None:
        try:
            paths = self.library.stage_export(version_id, tag=".open")
        except LibraryError as error:
            self.show_message(f"打开失败：{error}")
            return
        if not paths:
            self.show_message("这一稿没有可打开的文件")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(paths[0])))
        self.show_message(f"已用系统默认程序打开：{paths[0].name}")

    def _export_version(self, version_id: int) -> None:
        if not version_id:
            return
        version = self.library.get_version(version_id)
        if version is not None and not version.files:
            # 空稿（刚点「＋ 新增一稿」、文件还没加）：先别说"已导出 0 个文件"，
            # 直接讲清楚这一稿里什么都没有，别让用户以为是导出坏了
            self.show_message(f"v{version.seq} 还没有文件，没有可导出的内容")
            return
        target = QFileDialog.getExistingDirectory(self, "选择导出目录", str(Path.home()))
        if not target:
            return
        try:
            written = self.library.export_version(version_id, target)
        except LibraryError as error:
            self._notify("导出失败", str(error))
            return
        self.show_message(f"已导出 {len(written)} 个文件到 {target}")

    def _restore_version(self, version_id: int) -> None:
        version = self.library.get_version(version_id)
        if version is None or self._document_id is None:
            return
        check = self.library.check_version_files(version_id)
        broken = check["missing"] + check["changed"]
        if broken:
            names = "、".join(item["name"] for item in broken[:3])
            if not self._confirm(
                "恢复到旧版（注意）",
                f"v{version.seq} 的原始文件已不在原处或已被就地覆盖：{names}\n\n"
                "库内只保留了当时的**内容指纹**，字节本身取不回来。\n"
                "继续的话，新版本只会恢复「当时引用了哪些文件」这个事实。\n\n仍然继续？",
            ):
                return
            note = f"恢复到 v{version.seq}（原文件已变化）"
            force = True
        else:
            if not self._confirm(
                "恢复到旧版",
                f"把 v{version.seq} 的内容作为最新的稿？\n\n"
                "历史不会被改写：系统会新增一稿，并标注「回到旧版」。",
            ):
                return
            note = f"恢复到 v{version.seq}"
            force = False

        try:
            new_version, _ = self.library.restore_version(
                self._document_id, version_id, note=note, force=force
            )
        except LibraryError as error:
            self._notify("恢复失败", str(error))
            return
        self._after_write(f"已恢复到 v{version.seq}（新稿 v{new_version.seq}）")
        self.open_event(self._document_id)

    def _set_event_state(self, document_id: int, state: str) -> None:
        try:
            self.library.set_document_state(document_id, state)
        except LibraryError as error:
            self._notify("改状态失败", str(error))
            return
        label = DOC_STATE_LABELS.get(state, state)
        self.sidebar.refresh_event_row(document_id)
        self._after_write(f"状态已标记为「{label}」")
        self._refresh_current_view()

    def _add_version_note(self, version_id: int, note: str) -> None:
        try:
            self.library.add_version_note(version_id, note)
        except LibraryError as error:
            self._notify("记说明失败", str(error))
            return
        self._after_write("说明已记录")
        self._refresh_current_view()

    def _refresh_current_view(self) -> None:
        """内容变了（改了状态 / 记了说明 / 重绑了引用）之后重画当前这一页。

        关键：**镜头原地不动**。这条路上换的只是内容，不是"去了别的地方"，
        所以一律按"刷新内容"传 ``keep_view=True``：
        * 版图：以前这里没传 keep_view，``set_graph`` 不还原镜头，
          而且 ``show_project_board`` 里还会再 ``fit_to_content`` ——
          用户看到的就是"改个状态，画面跳走"；
        * 年轮：``fit_to_content`` + 选中那一稿引起的滚动。
        三处现在都保住缩放与滚动位置（``keep_view=True`` 同时也不切视图、
        不记导航历史）。
        """
        current = self.view_stack.currentWidget()
        if current is self.graph_view and self._document_id:
            if self._stage_role:
                self.open_stage_flow(self._document_id, self._stage_role,
                                     self._version_id, keep_view=True)
            else:
                self._open_document(self._document_id, self._version_id,
                                    keep_view=True)
        elif current is self.event_page and self._document_id:
            self.open_event(self._document_id, self._version_id, keep_view=True)
        elif self._project_id:
            # keep_view=True 里自带 switch=False：不 setCurrentWidget、不记历史
            self.show_project_board(self._project_id, keep_view=True)

    def _after_write(self, message: str) -> None:
        self.sidebar.rebuild()
        self._update_status()
        self.show_message(message)

    def _on_graph_version_selected(self, version_id: int) -> None:
        self._version_id = version_id
        self.graph_view.select_version(version_id)

    def _on_panel_version_selected(self, version_id: int) -> None:
        self._version_id = version_id
        if self.view_stack.currentWidget() is self.graph_view:
            self.graph_view.select_version(version_id)

    # ---------------------------------------------------------------- 结构

    def rename_event(self, document_id: int) -> None:
        """给事件改名（侧栏事件枝行右键）。"""
        document = self.library.get_document(document_id)
        if document is None:
            return
        new_title = self._ask_text("修改事件枝名称", "新名称：", document.title)
        if new_title is None:
            return
        new_title = new_title.strip()
        if not new_title or new_title == document.title:
            return
        try:
            self.library.rename_document(document_id, new_title)
        except LibraryError as error:
            self._notify("改名失败", str(error))
            return
        self.sidebar.rebuild()
        self._after_write(f"已改名为「{new_title}」")
        if self._document_id == document_id:
            self.open_event(document_id, self._version_id)
        elif self._project_id:
            self.show_project_board(self._project_id)

    def delete_event(self, document_id: int) -> None:
        """删除事件枝（侧栏事件枝行 / 白板卡片右键）——**硬删，连同年轮记录**。

        所以这里直接硬删、不进撤销栈。磁盘上的原始文件永远不动。
        """
        document = self.library.get_document(document_id)
        if document is None:
            return
        if not self._confirm(
            "删除事件枝",
            f"把「{document.title}」连同它的年轮记录一起从库里删掉。\n"
            "这一步不能撤销（Ctrl+Z 也回不来）。\n\n"
            "你磁盘上的原始文件不会被删。确定吗？",
        ):
            return
        self.library.delete_document(document_id, hard=True)
        self.sidebar.rebuild()
        self._refresh_board()
        self._leave_deleted_event(document_id)
        self.show_message(f"已删除「{document.title}」")

    def _leave_deleted_event(self, document_id: int) -> None:
        """刚删掉的事件如果正开着，就退回版图。

        为什么要显式做这件事：``_refresh_board()`` 以前会顺手把视图切到版图
        （见 ``show_project_board`` 的 ``switch`` 参数），现在它老老实实只重画内容 ——
        于是"在事件枝页上把当前这个事件枝删掉"就会停在一个**已经不存在的事件枝页**上。
        这里把那个副作用变成一句说清楚的话。
        """
        if self._document_id != document_id:
            return
        self._document_id = None
        self._version_id = None
        self._stage_role = None
        if self._project_id:
            self.show_project_board(self._project_id)

    def _ask_text(self, title: str, label: str, default: str = "") -> str | None:
        """要用户输入一行文字（interactive=False 时直接返回 None，自动化不弹框）。"""
        if not self.interactive:
            return None
        text, ok = QInputDialog.getText(self, title, label, text=default)
        return text if ok else None

    def create_project(self, name: str) -> int | None:
        """新建项目树（界面入口与自动化共用这条路径）。"""
        clean = (name or "").strip()
        if not clean:
            return None
        try:
            project = self.library.create_project(clean)
        except LibraryError as error:
            self._notify("创建失败", str(error))
            return None
        self.sidebar.rebuild()
        self.show_project_board(project.id)
        self._after_write(f"已创建项目：{project.name}")
        return project.id

    def create_event(self, name: str, project_id: int | None = None) -> int | None:
        """在当前（或指定）项目下新建事件枝。"""
        target_project = project_id or self._project_id or self.sidebar.current_project_id()
        if target_project is None:
            self._notify("先建一个项目", "还没有项目，请先新建一个项目树。")
            return None
        clean = (name or "").strip()
        if not clean:
            return None
        try:
            root = self.library.node_tree(target_project)[0]
            today = QDate.currentDate().toString(Qt.DateFormat.ISODate)
            event = self.library.create_document(root.id, clean, task_date=today)
        except LibraryError as error:
            self._notify("创建失败", str(error))
            return None
        self.sidebar.rebuild()
        self.open_event(event.id)
        self._after_write(f"已创建事件枝：{event.title}")
        return event.id

    def _new_project_dialog(self) -> None:
        name, ok = QInputDialog.getText(self, "新建项目树", "项目名称（例如：资料整理）")
        if ok and name.strip():
            self.create_project(name)

    def _new_event_dialog(self) -> None:
        name, ok = QInputDialog.getText(self, "新建事件枝", "事件枝名称（一件要做的事）")
        if ok and name.strip():
            self.create_event(name)

    def _new_event_in_project(self, project_id: int) -> None:
        """项目树行右键「新建事件枝…」：问名字，然后建在那个项目下面。"""
        name, ok = QInputDialog.getText(self, "新建事件枝", "事件枝名称（一件要做的事）")
        if ok and name.strip():
            self.create_event(name, project_id)

    def _import_files(self) -> None:
        if self._document_id is None:
            self._notify("先选一个事件枝", "请先在左侧选中一个事件枝（或先新建一个）。")
            return
        paths = self._ask_files("选择要归档的文件（会放进当前事件枝）")
        if paths:
            self._run_archive(document_id=self._document_id, paths=paths)

    # ---------------------------------------------------------------- 监视

    def _toggle_watcher(self, checked: bool) -> None:
        if checked:
            if not WATCHDOG_AVAILABLE:
                self._notify("缺少依赖", "目录监视需要 watchdog：pip install watchdog")
                self.action_watch.setChecked(False)
                return
            if not self.library.list_watch_dirs():
                directory = QFileDialog.getExistingDirectory(
                    self, "选择要监视的目录（往里面丢文件就自动归档）", str(Path.home())
                )
                if not directory:
                    self.action_watch.setChecked(False)
                    return
                project_id = self._project_id or self.sidebar.current_project_id()
                if project_id is None:
                    self._notify("先建一个项目", "请先选中一个项目。")
                    self.action_watch.setChecked(False)
                    return
                root = self.library.node_tree(project_id)[0]
                self.library.add_watch_dir(project_id, root.id, directory)

            self._watcher = LibraryWatcher(
                self.library_root, on_archived=self._watch_events.put
            )
            if self._watcher.start():
                self.action_watch.setText("停止目录监视")
                self.watch_label.setText(f"监视：{len(self._watcher.rules)} 条规则")
                self.show_message("目录监视已启动")
            else:
                self.action_watch.setChecked(False)
                self.show_message("没有可监视的目录规则")
        else:
            if self._watcher is not None:
                self._watcher.stop()
                self._watcher = None
            self.action_watch.setText("开始目录监视")
            self.watch_label.clear()          # 没在跑就什么都不显示（见 _update_status）
            self.show_message("目录监视已停止")

    def _on_watcher_outcome(self, outcome) -> None:
        self.show_message(outcome.summary)
        if outcome.ok:
            self.sidebar.rebuild()
            self._update_status()
            if self._document_id:
                self.open_event(self._document_id)

    # ------------------------------------------------------------- Web 查看端

    def _toggle_web(self, checked: bool) -> None:
        if checked:
            from ..core.api.server import create_server

            last_error: Exception | None = None
            for port in range(8765, 8780):
                try:
                    # enable_write=True：这是**桌面端自己拉起来的本机查看端**
                    # （绑 127.0.0.1，只有这台机器能访问），页面上的"记状态/写备注"
                    # 按钮就是要写库的。写接口默认不挂是为了 `serve --host` 那条
                    # 可能暴露到局域网的路，本机内嵌的这条不需要那个限制。
                    self._http_server = create_server(
                        self.library_root, "127.0.0.1", port, enable_write=True
                    )
                    break
                except OSError as error:
                    last_error = error
                    self._http_server = None
            if self._http_server is None:
                self._notify("启动失败", f"没有可用端口：{last_error}")
                self.action_web.setChecked(False)
                return
            port = self._http_server.server_address[1]
            self._http_thread = threading.Thread(
                target=self._http_server.serve_forever, daemon=True
            )
            self._http_thread.start()
            url = f"http://127.0.0.1:{port}/"
            self.action_web.setText("停止 Web 查看端")
            self.show_message(f"Web 查看端已启动：{url}")
            self._notify(
                "Web 查看端已启动",
                f"在同一台机器的浏览器打开：{url}\n"
                "可以在里面浏览事件与各环节、改状态、记备注（同一份数据）。",
            )
        else:
            if self._http_server is not None:
                self._http_server.shutdown()
                self._http_server.server_close()
                self._http_server = None
            self._http_thread = None
            self.action_web.setText("Web 查看端")
            self.show_message("Web 查看端已停止")

    def _open_help(self) -> None:
        """打开使用说明 PDF（随程序打包在 assets 里）。"""
        path = resource_path("tooak", "desktop", "assets", "tooak-help.pdf")
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            self.show_message("已打开使用说明")
        else:
            self._notify("说明文件缺失", f"没找到 {path}")

    def _open_library_dialog(self) -> None:
        """「打开库…」：选一个库目录，记住它，然后重启进程指向那个库。

        为什么用"重启"而不是就地热切换：库换了之后，侧栏 / 白板 / 事件枝页 /
        年轮 / 撤销栈 / 会话恢复全都要重建，就地切一遍的坑比重启大得多；
        重启由 run.py 从新目录重读，是唯一确定干净的路径。

        选中的目录里**还没有库**（没有 tooak.sqlite3）时，问一句要不要在这里
        新建一个 —— 这样"新建库"和"换库"走同一条路，首启引导只是它的一个变体。
        """
        folder = QFileDialog.getExistingDirectory(
            self, "选择库所在目录", str(self.library_root)
        )
        if not folder:
            return
        root = Path(folder).expanduser()
        if not (root / "tooak.sqlite3").is_file():
            if not self._confirm(
                "在这里新建库",
                f"{root}\n\n这个目录里还没有库（没有 tooak.sqlite3）。\n"
                "要在这里新建一个吗？",
            ):
                return
        write_last_library(root)
        if getattr(sys, "frozen", False):
            # 打包成 exe 后没有 run.py，直接重启自己（exe）指向新库
            QProcess.startDetached(sys.executable, ["-L", str(root)])
        else:
            run_py = Path(__file__).resolve().parents[2] / "run.py"
            # 用 QProcess 在后台拉起新实例，再退出当前窗口 —— 新实例不受
            # tooak.sh 的单实例 flock 限制（那是 shell 层的锁，这里直接跑 run.py）
            QProcess.startDetached(sys.executable, [str(run_py), "-L", str(root)])
        QApplication.instance().quit()

    # ------------------------------------------------------------- 旧库导入

    def _import_legacy(self) -> None:
        source = QFileDialog.getExistingDirectory(
            self, "选择旧库目录（里面有 data.db 与 files/）", str(Path.home())
        )
        if source:
            self.start_legacy_import(source)

    def start_legacy_import(self, source: str | Path, *, mode: str = "reference") -> bool:
        from ..core.legacy import describe_legacy

        try:
            summary = describe_legacy(source)
        except Exception as error:
            self._notify("读不了这个旧库", str(error))
            return False

        if not summary.rows:
            self._notify("旧库是空的", summary.summary())
            return False

        if not self._confirm(
            "从旧库导入",
            summary.summary()
            + "\n\n会按下列规则迁移：\n"
            "· 一个集合 → 一个项目；同一标题的多条记录 → 同一事件的多个版本；\n"
            "· 输入/输出/参考/分析/图示 → 事件里对应的环节；\n"
            "· 「依赖任务」→ 事件之间的连线。\n\n"
            "文件**留在原处不动**：新库只登记它们的路径与内容指纹，"
            "不复制、不移动、不改动旧库里的任何文件。\n\n开始导入？",
        ):
            return False

        job = LegacyImportJob(source, self.library_root, mode=mode)
        dialog = self._make_progress_dialog(
            "从旧库导入", "正在读取旧库…", max(summary.files_on_disk, 1)
        )
        self._progress_dialogs[job.id] = dialog       # 按 job id 存，见 _start_archive
        self._import_jobs[job.id] = job
        job.start()
        return True

    def _on_import_progress(self, event: ImportProgress) -> None:
        dialog = self._progress_dialogs.get(event.job_id)
        if dialog is None or dialog.wasCanceled():
            return
        if event.total:
            dialog.setMaximum(event.total)
            dialog.setValue(event.done)
        dialog.setLabelText(event.message)

    def _on_import_finished(self, job: LegacyImportJob, event: ImportFinished) -> None:
        self._finish_progress_dialog(job.id)

        stats = event.stats
        self._update_status()
        self.sidebar.rebuild()
        if stats is None:
            self._notify("导入结束", "没有拿到统计信息")
            return
        self.show_message(
            f"导入完成：事件 {stats.documents} · 版本 {stats.versions} · 文件 {stats.files_stored}"
        )
        self._notify("从旧库导入完成", stats.summary())
        self._auto_select_first()

    # ---------------------------------------------------------------- 杂项

    def show_message(self, message: str) -> None:
        self.statusBar().showMessage(message, 8000)

    def _confirm(self, title: str, message: str) -> bool:
        """需要用户点头的操作统一走这里（interactive=False 时自动通过）。"""
        if not self.interactive:
            return True
        answer = QMessageBox.question(
            self, title, message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _notify(self, title: str, message: str) -> None:
        """非阻塞提示：自动化模式下只写状态栏。"""
        if not self.interactive:
            self.show_message(f"{title}：{message}")
            return
        QMessageBox.warning(self, title, message)

    def _rebind_version_files(self, version_id: int) -> None:
        """为失效的引用重新绑定文件（文件被挪走/改名后）。"""
        if not version_id:
            return
        broken = [
            item for item in self.library.broken_references() if item.get("file_id")
        ]
        if not broken:
            self.show_message("没有失效的引用")
            return
        names = "、".join(item["name"] for item in broken[:5])
        if not self._confirm(
            "重新绑定失效文件",
            f"库里有 {len(broken)} 个引用指向的文件已不在原处：\n{names}"
            + ("…" if len(broken) > 5 else "")
            + "\n\n接下来请选出这些文件现在的位置（可多选）。\n"
            "系统按「同名同大小 → 同名同指纹 → 指纹一致」自动绑回，"
            "内容不一致的不会被误绑。",
        ):
            return

        selected, _ = QFileDialog.getOpenFileNames(
            self, "选择文件现在的位置（可多选）", str(Path.home())
        )
        if not selected:
            return

        try:
            result = self.library.rebind_from_candidates(list(selected), deep=True)
        except LibraryError as error:
            self._notify("重新绑定失败", str(error))
            return

        matched, unmatched = len(result["matched"]), len(result["unmatched"])
        self.show_message(
            f"已重新绑定 {matched} 个引用" + (f"，{unmatched} 个没匹配上" if unmatched else "")
        )
        self._after_write(f"重新绑定完成：成功 {matched} 个")
        self._refresh_current_view()
        if unmatched:
            detail = "\n".join(
                f"· {item['name']}（{item.get('reason', '没找到匹配的文件')}）"
                for item in result["unmatched"][:5]
            )
            self._notify(
                "部分引用没能绑定",
                f"以下 {unmatched} 个引用没有匹配上：\n{detail}\n\n"
                "如果文件被改了名，可以试试命令行：\ntoak rebind --search <目录> --deep --apply",
            )

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)

    def _wait_for_job(self, job, timeout: float) -> bool:
        """等一个后台任务收尾，**边等边跑事件循环**。返回它是不是真结束了。

        为什么不用 ``job.wait(timeout)`` 一把梭：那是把 GUI 线程堵死，
        界面上表现成"程序卡死/没响应"（关窗时最长能冻 75 秒，进度框还是
        WindowModal 又没取消键，用户只能强杀）。这里切成 0.05 秒一片，
        片与片之间 ``processEvents()``，窗口还能重绘、还能响应鼠标。
        """
        deadline = time.monotonic() + timeout
        while job.is_running():
            if time.monotonic() >= deadline:
                return False
            QApplication.processEvents()
            job.wait(SHUTDOWN_SLICE_S)
        return True

    def shutdown(self) -> None:
        """停止后台任务并关闭库。

        顺序很重要：**先让后台线程结束，再释放库连接**，
        否则后台任务可能在一个已经关闭的 SQLite 连接上继续操作。
        """
        self._shutting_down = True
        if getattr(self, "_layout_save_timer", None) is not None:
            self._layout_save_timer.stop()
        try:
            self.save_layout_options()
        except Exception:
            pass
        try:
            # 底部"年轮"窗口可能在算差异，先让它收尾
            self.timeline_panel.close_panel()
        except Exception:
            pass
        try:
            # 说明框里还没失焦的内容也要存下来
            self.event_view.commit_pending()
        except Exception:
            pass
        if self._poll_timer is not None:
            self._poll_timer.stop()
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None
        if self._http_server is not None:
            self._http_server.shutdown()
            self._http_server.server_close()
            self._http_server = None
        # **先关进度框再等**：模态框还挂着的时候，等待期间界面点了也没用
        for dialog in list(self._progress_dialogs.values()):
            dialog.close()
        self._progress_dialogs.clear()

        # 后台任务必须等：它们是 daemon 线程，进程一退就被杀掉 ——
        # 归档 / 导出 / 打包停在一半，用户目录里就留下半份 zip、半个导出目录。
        # 以前这里只等归档和导入，`_export_jobs` / `_zip_jobs` **完全没等**，
        # 正是"关窗时留下半截结果"的来源。
        stuck: list[str] = []
        for kind, jobs in (("archive", self._archive_jobs),
                           ("import", self._import_jobs),
                           ("export", self._export_jobs),
                           ("zip", self._zip_jobs),
                           ("word", self._word_jobs),
                           ("copy", self._copy_jobs)):
            timeout = SHUTDOWN_JOB_TIMEOUTS.get(kind, 30.0)
            for job in list(jobs.values()):
                # 一个超时就把这一类里的其它任务也跳过：已经等了 60 秒了，
                # 再一个个等下去只会让窗口更久关不掉
                if not self._wait_for_job(job, timeout):
                    stuck.append(kind)
                    break
            jobs.clear()
        if stuck:
            # 说清楚：不是"我替你留了半份"，而是"它还在自己收尾"
            self.show_message("后台任务还没结束，已让它自己收尾")
        self._job_requests.clear()
        self._pending_archives.clear()
        self.sidebar.close()
        self.library.close()

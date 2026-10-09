"""画布底部的浮动工具栏（照 Obsidian 白板那套做）。

Obsidian 白板的工具栏固定在画布底部中间，按钮是"往画布上添东西"：
新卡片、新分组…我们这里是**新事件枝 / 说明框 / 分组 / 适应 / 导出**。

两种用法都支持（用户明确要的）：
* **点一下**：在画布中央新建；
* **按住往画布上拖**：拖到哪儿就在哪儿新建（``QDrag`` + 自定义 MIME，
  落点交给画布处理）—— 和"从文件管理器拖文件进来"是同一条通路，
  所以把文件/文件夹拖到白板上也能新建事件枝并顺手归档。

为什么按钮长得像按钮、但行为像"可拖的东西"：白板的操作大多是"摆到某个位置"，
让按钮既能点又能拖，比在菜单里选"新建"再拖过去少两步。
"""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDrag, QPixmap
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from .style import prune_icon, scoped_style

__all__ = ["CanvasToolbar", "NEW_ITEM_MIME"]

#: 从工具栏拖到画布上时用的自定义 MIME：值是 "event" / "note" / "group"
NEW_ITEM_MIME = "application/x-tooak-canvas-item"


class _ToolButton(QPushButton):
    """工具栏上的按钮：点一下直接执行，按住拖出去就"把东西放到画布上"。"""

    dragged = Signal(str)

    def __init__(self, kind: str, text: str, tooltip: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self.kind = kind
        self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self._press_pos: QPoint | None = None

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if (self._press_pos is None
                or not (event.buttons() & Qt.MouseButton.LeftButton)):
            super().mouseMoveEvent(event)
            return
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if moved < 8:                      # 还没离开按钮：当点击处理
            super().mouseMoveEvent(event)
            return
        self._start_drag()

    def _start_drag(self) -> None:
        self._press_pos = None
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(NEW_ITEM_MIME, self.kind.encode("utf-8"))
        drag.setMimeData(mime)
        pixmap = QPixmap(self.size())
        pixmap.fill(Qt.GlobalColor.transparent)
        self.render(pixmap)
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPoint(pixmap.width() // 2, pixmap.height() // 2))
        drag.exec(Qt.DropAction.CopyAction)


class CanvasToolbar(QFrame):
    """画布底部的浮动工具栏。"""

    #: 点按钮（在画布中央新建）
    createRequested = Signal(str)          # "event" / "note"
    #: 把选中的事件归为一组
    groupRequested = Signal()
    #: 导出选中的事件枝
    exportRequested = Signal()
    #: 适应窗口
    fitRequested = Signal()
    #: 撤销 / 重做
    undoRequested = Signal()
    redoRequested = Signal()

    def add_layout_menu(self, menu: QMenu) -> None:
        """把"白板排法"这组动作塞进「布局 ▾」（动作由主窗口建，免得两处各建一套）。"""
        self.layout_button.setMenu(menu)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("canvasToolbar")
        self.setStyleSheet(
            "#canvasToolbar{background:rgba(255,255,255,0.94);"
            "border:1px solid #d7e3da;border-radius:10px;}"
            "#canvasToolbar QPushButton{border:none;background:transparent;"
            "color:#1b2a20;font-size:12px;padding:4px 9px;border-radius:6px;}"
            "#canvasToolbar QPushButton:hover{background:#e8f3ec;color:#0e6b3d;}"
            "#canvasToolbar QLabel{color:#6b7c72;font-size:11px;}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 4, 8, 4)
        row.setSpacing(2)

        self.event_button = _ToolButton(
            "event", "＋ 新事件枝",
            "点一下：在画布中央新建一个事件枝\n按住往画布上拖：拖到哪儿就建在哪儿\n"
            "（也可以把文件拖到**某张事件枝卡片**上 = 先选放进哪个环节，再加进那个事件枝）",
        )
        self.note_button = _ToolButton(
            "note", "＋ 说明框",
            "点一下：在画布中央加一个说明文本框\n按住往画布上拖：拖到哪儿就放哪儿\n"
            '（说明框只写"这里发生了什么"，不进归档）',
        )
        row.addWidget(self.event_button)
        row.addWidget(self.note_button)

        separator = QLabel("│", self)
        row.addWidget(separator)

        self.undo_button = QPushButton("↶ 撤销", self)
        self.undo_button.setToolTip("撤销上一步（Ctrl+Z）—— 挪位置、删卡片、剪断连线都能退回来")
        self.undo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.undo_button)

        self.redo_button = QPushButton("↷ 重做", self)
        self.redo_button.setToolTip("重做（Ctrl+Shift+Z）")
        self.redo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.redo_button)
        row.addWidget(QLabel("│", self))

        self.layout_button = QToolButton(self)
        self.layout_button.setText("布局 ▾")
        self.layout_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.layout_button.setToolTip("白板的排法：自由网格 / 依赖分层 / 重置位置")
        self.layout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.layout_button)
        row.addWidget(QLabel("│", self))

        self.group_button = QPushButton("分组", self)
        self.group_button.setToolTip("把选中的事件归为一组（先在画布上框选/Ctrl 点选）")
        self.group_button.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.group_button)

        self.export_button = QPushButton("导出…", self)
        self.export_button.setToolTip("把选中的事件导成文件夹（每个事件枝一个文件夹，里面按环节分）")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.export_button)

        self.fit_button = QPushButton("适应", self)
        self.fit_button.setToolTip("缩放到能看见全部内容（Ctrl+0）")
        self.fit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        row.addWidget(self.fit_button)

        self.hint = QLabel("空白处拖动 = 框选，中键/空格 = 平移", self)
        row.addWidget(self.hint)
        self._prune = prune_icon(14)          # 保持图标加载过（主题一致性）

        self.event_button.clicked.connect(lambda: self.createRequested.emit("event"))
        self.note_button.clicked.connect(lambda: self.createRequested.emit("note"))
        self.event_button.dragged.connect(self.createRequested.emit)
        self.note_button.dragged.connect(self.createRequested.emit)
        self.group_button.clicked.connect(self.groupRequested.emit)
        self.export_button.clicked.connect(self.exportRequested.emit)
        self.fit_button.clicked.connect(self.fitRequested.emit)
        self.undo_button.clicked.connect(self.undoRequested.emit)
        self.redo_button.clicked.connect(self.redoRequested.emit)


class EventToolbar(QFrame):
    """**事件枝页**自己的浮动工具栏（只在这一页出现）。**竖排**，贴在右上角的缩放条下面。

    为什么要分开：用户的原话是"按钮逻辑混乱堆在整个上面，建议好好分一下组，
    只放在用得到这些按钮的页面上"。于是顶栏只留**全局**动作（新建项目树/事件枝、刷新、
    侧栏、目录监视、存储模式、Web、打开库文件夹…），
    而"排版"这件事只在事件枝页出现 —— 正是因为它是那一页的事，顺带也就把

    为什么竖排：它原来在底部中间，压着"年轮"时间轴（版本管理）；
    改到右上角后横着排又太宽（三个按钮 + 提示约 320px），窗口一窄就**出界**。
    """

    #: 切换"编辑布局"（锁 / 解锁）
    editLayoutToggled = Signal(bool)
    #: 整理排版（按默认排法重排）
    arrangeRequested = Signal()
    #: 适应窗口
    fitRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("eventToolbar")
        self.setStyleSheet(
            "#eventToolbar{background:rgba(255,255,255,0.94);"
            "border:1px solid #d7e3da;border-radius:10px;}"
            "#eventToolbar QPushButton{border:none;background:transparent;"
            "color:#1b2a20;font-size:12px;padding:4px 9px;border-radius:6px;}"
            "#eventToolbar QPushButton:hover{background:#e8f3ec;color:#0e6b3d;}"
            "#eventToolbar QPushButton:checked{background:#e8f3ec;color:#0e6b3d;"
            "font-weight:600;}"
            "#eventToolbar QLabel{color:#6b7c72;font-size:11px;}"
        )
        column = QVBoxLayout(self)
        column.setContentsMargins(6, 5, 6, 5)
        column.setSpacing(2)

        # 布局锁：**文字会随状态变**，一眼看得出现在是锁着还是能改
        self.lock_button = QPushButton("\U0001f512 布局已锁定", self)
        self.lock_button.setCheckable(True)
        self.lock_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.lock_button.setToolTip(
            "现在方框是**锁住**的：拖标题栏不会挪动它（避免归档文件时误挪）。\n"
            "点一下进入「编辑布局」：可以拖方框、拉大小，会记进框体模板。"
        )
        # 竖排：每个按钮占满整列，点起来不用瞄（这一列本来就窄）
        for button in (self.lock_button,):
            button.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Fixed)
        column.addWidget(self.lock_button)

        separator = QFrame(self)
        separator.setFrameShape(QFrame.Shape.HLine)
        scoped_style(separator, "color:#e2ece5;", name="toolbarSeparator")
        separator.setFixedHeight(1)
        column.addWidget(separator)

        self.arrange_button = QPushButton("整理排版", self)
        self.arrange_button.setToolTip("清掉手工排版，所有事件都回到默认排法（只有编辑布局时可用）")
        self.arrange_button.setCursor(Qt.CursorShape.PointingHandCursor)
        column.addWidget(self.arrange_button)

        self.fit_button = QPushButton("适应", self)
        self.fit_button.setToolTip("缩放到能看见全部方框（Ctrl+0）")
        self.fit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        column.addWidget(self.fit_button)
        for button in (self.lock_button, self.arrange_button, self.fit_button):
            button.setMinimumWidth(0)
            # 这三个按钮都挂着提示：样式必须限定在按钮自己身上，否则提示框跟着变黑
            scoped_style(button, "text-align:left;")
            button.setSizePolicy(QSizePolicy.Policy.Preferred,
                                 QSizePolicy.Policy.Fixed)

        # 这一条工具栏摆在**右上角、缩放条下面**（不是底部）：那一带是"显示控制"
        # 的地盘，底部留给"年轮"时间轴/版本管理。所以提示不整句写在这儿 ——
        # 悬停在工具栏上就能看到（底部那一条白板工具栏仍然显示同样的话）。
        self.hint = QLabel("中键/空格 = 平移", self)
        self.hint.setToolTip("按住中键（或空格 + 左键）拖动 = 平移画布；空白处左键拖 = 框选")
        self.hint.setWordWrap(True)
        column.addWidget(self.hint)

        self.lock_button.toggled.connect(self._on_lock_toggled)
        self.arrange_button.clicked.connect(self.arrangeRequested.emit)
        self.fit_button.clicked.connect(self.fitRequested.emit)
        self._on_lock_toggled(False)

    def _on_lock_toggled(self, editing: bool) -> None:
        self.lock_button.setText(
            "\U0001f513 编辑布局中" if editing else "\U0001f512 布局已锁定"
        )
        self.arrange_button.setEnabled(editing)
        # 按钮文字变成了"编辑布局中"，可画布那边还是锁着）
        self.editLayoutToggled.emit(bool(editing))

    def set_editing(self, editing: bool) -> None:
        """外部（主窗口）改了状态时同步按钮（先屏蔽信号，避免又发回去绕成环）。"""
        editing = bool(editing)
        if self.lock_button.isChecked() != editing:
            self.lock_button.blockSignals(True)
            self.lock_button.setChecked(editing)
            self.lock_button.blockSignals(False)
            self._on_lock_toggled(editing)

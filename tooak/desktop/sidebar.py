"""左侧栏：一棵「项目 → 事件」的树。

------------------------------
一开始是"两栏列表"（左项目、右事件），但用户后来说"左侧栏还是精简一下，
把事件做成项目的子节点吧" —— 于是合成一棵树：

* **第一层 项目**：一个工程 / 课题一行；
* **第二层 事件**：展开某个项目就是它的事件枝（两列：**名称 / 起始**，
  名称前面带状态图案：绿叶=进行中、棕黄橡果=已完成、黄叶=搁置；被依赖的事件更新了再加琥珀三角）。

比两栏省掉了这些：splitter、两套行高统一、两套选中同步、来回跳转的 notify 参数。
环节和文件仍然**不进侧栏** —— 它们在事件枝页里以方框+卡片呈现（拖进去就归档，比在树里翻直观）。

表头下面那一排小按钮
--------------------
这三个动作都是往库里塞东西，跟这棵树是一件事。动作对象仍由 ``MainWindow`` 创建并
拥有，这里只借它画按钮 —— **同一套 QAction**，不会出现两处入口状态不一致。
侧栏被"侧栏"按钮收起来时，这排按钮跟着一起不可见（用户接受这一点：收起来就是为了
画布占满整窗、腾出地方从文件管理器拖文件）。

--------------------------
* ``↑`` / ``↓``：在树里走，走到的项目/事件会跟着切换主视图；
* ``→`` / ``←``：展开 / 折叠项目；空格也是展开/折叠；
* ``Enter``：打开当前这一项（项目 → 版图，事件 → 事件枝页）；
* ``F2``：改名当前事件枝；``Delete``：删除当前事件枝（会先问一句）；
* 直接敲字母 = 按名字跳（Qt 自带的 keyboardSearch）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QAction, QBrush, QColor, QFontMetrics, QIcon, QPainter,
                           QPen, QPixmap)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QMenu,
    QSizePolicy,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core import Library, LibraryError
from ..core.library import DOC_STATE_LABELS
from ..core.util import to_local_str
from .highlight import match_spans
from .style import (ACCENT, ATTENTION_TEXT, MATCH_FILL, MATCH_INK, TEXT,
                    group_icon, pencil_icon, prune_icon, state_glyph, state_icon,
                    trunk_icon, warning_pixmap)
from .widgets import ElidedLabel

__all__ = ["SidebarWidget", "start_stamp_of", "start_text_of"]

#: Qt 角色：树上挂的项目 / 事件 id
ID_ROLE = Qt.ItemDataRole.UserRole + 1
#: 事件节点上记状态用的角色
STATE_ROLE = Qt.ItemDataRole.UserRole + 2
#: 事件节点上记"依赖告警"用的角色（被依赖事件的标题，顿号分隔）
ATTENTION_ROLE = Qt.ItemDataRole.UserRole + 3
#: 这一行是什么：``"project"`` / ``"event"``
KIND_ROLE = Qt.ItemDataRole.UserRole + 4


def _group_children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    """某一行的**分组**子节点（也就是项目行下面那一层，见 ``_fill_events``）。"""
    return [item.child(index) for index in range(item.childCount())
            if item.child(index).data(0, KIND_ROLE) == "group"]


def _events_of(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    """项目行下面**所有**事件枝节点（分组里的也算，顺序 = 眼睛看到的顺序）。

    树多了一层分组之后，"项目行有几个孩子"不再等于"有几个事件枝" ——
    凡是按"孩子"数事件的地方都得走这里（表头的计数、上下一个事件枝、
    搜索定位、找某一个事件的节点）。
    """
    found: list[QTreeWidgetItem] = []
    for index in range(item.childCount()):
        child = item.child(index)
        if child.data(0, KIND_ROLE) == "group":
            found.extend(child.child(inner) for inner in range(child.childCount()))
        elif child.data(0, KIND_ROLE) == "event":
            found.append(child)
    return found


def start_stamp_of(document) -> str:
    """事件的"起始时间"：优先任务日期，没有就用建档时间（都是 ISO 字符串）。

    排序用原始 ISO 串 —— 字典序就是时间序，省得来回解析。
    """
    return (getattr(document, "task_date", "") or getattr(document, "created_at", "")
            or "")


def start_text_of(document, *, full: bool = False) -> str:
    """列表里显示的起始时间：**年月日**（``2022-04-17``）。"""
    stamp = start_stamp_of(document)
    if not stamp:
        return "—"
    return to_local_str(stamp, "%Y-%m-%d") or "—"


def _event_icon(state: str, alerting: bool, size: int = 15) -> QIcon:
    """事件前面的图案：状态图案（绿叶/棕黄橡果/黄叶），有依赖告警时右上角补个琥珀三角。

    一个 QTreeWidgetItem 一列只能挂一个图标，所以干脆合成一张 —— 状态和告警
    都不丢，也不用往名字里塞 ⚠ 字符（名字保持干净，测试和搜索都好比对）。
    """
    base = state_glyph(state, size)
    if not alerting or base.isNull():
        return QIcon(base)
    badge = warning_pixmap(max(size - 5, 9))
    combined = QPixmap(base.size())
    combined.fill(Qt.GlobalColor.transparent)
    painter = QPainter(combined)
    painter.drawPixmap(0, 0, base)
    painter.drawPixmap(base.width() - badge.width(), 0, badge)
    painter.end()
    return QIcon(combined)


class _EventTree(QTreeWidget):
    """能接收文件拖放的树：丢到**项目**上 = 在它下面新建事件枝。

    丢到**事件枝行**上**不接收**（用户定的）：拖文件到事件枝行 = "给这个事件枝建新的一稿"
    是个让人困惑的语义 —— 用户看到的是"我就想把这几个文件加到这次的事情里"，
    却冒出来一稿自己都不知道怎么来的。要加文件就去事件枝页拖进环节方框，
    或者拖到白板上的那张事件枝卡片上再选环节。
    """

    #: ``(kind, 目标 id, 文件路径列表)``，kind 目前只会是 ``"project"``
    filesDropped = Signal(str, int, list)
    #: 这次拖放被拒了，附一句给用户看的人话（主窗口接到状态栏上）
    dropRefused = Signal(str)

    #: 拖到事件枝行上时给的提示：告诉他正确的那条路怎么走
    EVENT_DROP_HINT = "事件枝行不收文件：把文件拖到白板上的事件枝卡片上，再选放进哪个环节"

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(False)
        # 只在"从外面拖文件进来"时接管；树内部的拖动（换项目）不需要
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def _target(self, position):
        item = self.itemAt(position.toPoint())
        if item is None:
            return None, None
        return item.data(0, KIND_ROLE), int(item.data(0, ID_ROLE) or 0)

    def _accepts(self, event) -> bool:
        if not event.mimeData().hasUrls():
            return False
        return any(url.isLocalFile() for url in event.mimeData().urls())

    def dragEnterEvent(self, event):  # noqa: N802
        if self._accepts(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if self._accepts(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        if not self._accepts(event):
            super().dropEvent(event)
            return
        # 去重（保序）：一次拖进来的 URL 里可能有同一份文件的两种写法
        # （相对路径 / 软链 / `..`），重复归档会在同一稿里插两行同名文件
        paths: list[str] = []
        seen: set[str] = set()
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = url.toLocalFile()
            try:
                key = str(Path(path).resolve())
            except OSError:            # 路径解析不了（断链的软链等）：按原样算
                key = path
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
        event.acceptProposedAction()
        kind, target_id = self._target(event.position())
        if not (kind and target_id and paths):
            return
        if kind == "event":
            # **事件枝行不再接收文件**（见类文档）。注意这里仍然 acceptProposedAction：
            # Qt 里 dragEnter 一拒绝，drop 事件根本不会送到 —— 那用户连一句解释都
            # 得不到，只看到鼠标变成"禁止"却不知道该怎么拖。收下拖放、什么都不改、
            # 给一句人话指路，才是这里要的"不接受"。
            # 库里一个字节都不动。
            self.dropRefused.emit(self.EVENT_DROP_HINT)
            return
        self.filesDropped.emit(kind, target_id, paths)


class _SearchDelegate(QStyledItemDelegate):
    """

    做法是"先让基类照常画"（图标、选中底色、省略号一个字都不动），再在
    **同一个文字矩形**上把命中的那几段补一层底色 + 深色字。为什么不自己整条重画：
    QTreeWidget 的样子（hover、选中、焦点框、两列的对齐）全是基类在管，
    照着重写一遍就是给自己找一组以后必然会对不上的像素。

    只标**看得见的文字**：整串被省略号截短时，位置映射会偏，所以按截短之后的
    那一段重新找命中（``match_spans``）；截短之后一个都不命中就干脆不标。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        #: 现在要标的关键词（``set_filter`` 会更新它）
        self.needle = ""

    def paint(self, painter, option, index) -> None:  # noqa: D102
        super().paint(painter, option, index)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        spans = match_spans(text, self.needle)
        if not spans:
            return
        # **关键**：视图递进来的 ``option`` 里**没有** HasDecoration 这一位，
        # 直接拿它去问"文字矩形"会得到一个落在**图标底下**的框（实测左边少 22px：
        # 实测文字真正从 44 开始画，而问出来的矩形是 22）—— 于是色块盖住图标和
        # 而且位置不对"）。所以先照委托自己的 ``initStyleOption`` 重排一份：
        # 它会把"这一行有图标"算进去，文字矩形的左边界与基类真正落笔的地方一致。
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        rect = style.subElementRect(QStyle.SubElement.SE_ItemViewItemText, opt, widget)
        metrics = QFontMetrics(opt.font)
        shown = metrics.elidedText(text, Qt.TextElideMode.ElideRight, rect.width())
        if shown != text:
            spans = match_spans(shown, self.needle)
        if not spans:
            return
        # 垂直位置照 Qt 的居中算法：先算文字块的顶，再加 ascent 得到基线
        top = rect.top() + (rect.height() - metrics.height()) / 2.0
        baseline = top + metrics.ascent()
        painter.save()
        for start, end in spans:
            x = rect.left() + metrics.horizontalAdvance(shown[:start])
            width = metrics.horizontalAdvance(shown[start:end])
            painter.fillRect(QRectF(x, top, width, metrics.height()),
                             QBrush(QColor(MATCH_FILL)))
            painter.setPen(QPen(QColor(MATCH_INK)))
            painter.drawText(QPointF(x, baseline), shown[start:end])
        painter.restore()


class SidebarWidget(QWidget):
    """项目 → 事件 的树。"""

    #: 选中了某个项目
    projectSelected = Signal(int)
    #: 选中了某个事件枝
    eventSelected = Signal(int)
    #: 右键事件枝行：改状态 / 改名 / 删除
    stateRequested = Signal(int, str)
    renameRequested = Signal(int)
    deleteRequested = Signal(int)
    #: 右键事件枝行：把这根枝移到**别的项目树**下（参数 = 事件 id）
    moveEventRequested = Signal(int)
    #: 右键项目树行：在它下面新建一个事件枝（参数 = 项目 id）
    newEventRequested = Signal(int)
    #: 右键项目树行：改名（参数 = 项目 id）
    renameProjectRequested = Signal(int)
    #: 右键项目树行：上移 / 下移（项目 id, ∓1）
    moveProjectRequested = Signal(int, int)
    #: 右键项目树行：把整棵树删掉（参数 = 项目 id）
    deleteProjectRequested = Signal(int)
    #: 右键**分组**行 / 点分组行的展开箭头：折叠或展开这一组
    #: （组 id, 折起来了吗）—— 和右侧白板上那个三角是同一件事，
    groupCollapseRequested = Signal(int, bool)
    #: 右键分组行：改名（组 id）
    groupRenameRequested = Signal(int)
    #: 右键分组行：解散这个组（组 id）
    groupDissolveRequested = Signal(int)
    #: 右键分组行：把组里的事件枝选中（组 id）
    groupSelectMembersRequested = Signal(int)
    #: 右键分组行：导出这一组的文件（组 id）
    groupExportRequested = Signal(int)
    #: 事件枝列表被清空（项目为空）
    selectionCleared = Signal()
    #: 请求归档（项目 id, 文件路径, 模式 "project"）
    #: 注意：以前这里还有一种 "document-attach"（拖到事件枝行 = 该事件枝新一稿），
    archiveRequested = Signal(int, list, str)
    #: 一句话状态
    statusMessage = Signal(str)
    #: 库内容变了（改名等），主窗口据此刷新
    libraryChanged = Signal()

    def __init__(self, library_root: str | Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.library_root = Path(library_root)
        self.library = Library.open(self.library_root)
        self._project_id: int | None = None
        self._event_id: int | None = None
        self._loading = False
        self._filter = ""
        self._matched_ids: set[int] | None = None
        #: 搜索时"这一枝是因为哪个文件名被搜出来的"（写进悬停提示，见 set_filter）
        self._matched_files: dict[int, list[str]] = {}

        self._build_ui()
        self.rebuild()

    # ------------------------------------------------------------------ 构建

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 8)
        layout.setSpacing(4)

        # ElidedLabel：这行字会随选中变长，但**不许它撑宽侧栏**
        self.header = ElidedLabel("库", self)
        self.header.setProperty("role", "section")
        layout.addWidget(self.header)

        # 表头下面这一排小按钮：**新建项目树 / 新建事件枝 / 导入文件**。
        # —— 这三个动作都是"往库里塞东西"，跟左边这棵树是一件事，放在树边上顺手。
        # 为什么不用 QToolBar：QToolBar 有自己的最小宽度和溢出箭头，会把侧栏顶宽
        # （侧栏宽度是用户拖出来的，见 main_window._build_ui）；一排 QToolButton 更听话。
        # 按钮**只显示图标**（setToolButtonStyle）：侧栏最窄 200 像素，带文字会挤成省略号，
        # 完整说明放 tooltip。动作本身仍然由 MainWindow 创建（同一个 QAction 加在这里，
        # 顶栏就不再加它）—— 一套动作、两处入口的老毛病不会再有。
        self.action_bar = QWidget(self)
        self.action_bar.setObjectName("sidebarActionBar")
        self.action_layout = QHBoxLayout(self.action_bar)
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(2)
        layout.addWidget(self.action_bar)
        #: 挂上来的按钮（测试与"侧栏收起时按钮跟着不可见"都用得到）
        self.action_buttons: list[QToolButton] = []

        self.tree = _EventTree(self)
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["名称", "起始"])
        self.tree.setUniformRowHeights(True)       # 230 个事件枝也不卡
        # 名字太长就省略（不出现横向滚动条，也就不去要求更宽）
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setSizePolicy(QSizePolicy.Policy.Ignored,
                                QSizePolicy.Policy.Expanding)
        self.tree.setRootIsDecorated(True)
        self.tree.setItemsExpandable(True)
        self.tree.setExpandsOnDoubleClick(False)   # 双击 = 打开，不是展开
        self.tree.setAllColumnsShowFocus(True)
        self.tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setToolTip(
            "项目 → 分组 → 事件枝。选中项目看版图，选中事件进事件枝页；\n"
            "事件前面的小图案就是状态：绿叶=进行中、棕黄橡果=已完成、黄叶=搁置；\n"
            "分组那一行的三角 = 折叠 / 展开这一组（和白板上的框是同一个状态）；\n"
            "把文件拖到**项目**或**分组**上 = 新建事件枝；事件枝行**不收**文件 ——\n"
            "要往事件里加文件，拖到白板上的事件枝卡片上再选放进哪个环节，\n"
            "或者进事件枝页直接拖进环节方框。\n"
            "键盘：↑↓ 走、←→ 折叠/展开、Enter 打开、F2 改名、Delete 删除。"
        )
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(1, 92)
        self._search_delegate = _SearchDelegate(self.tree)
        self.tree.setItemDelegate(self._search_delegate)
        layout.addWidget(self.tree)

        self.tree.filesDropped.connect(self._on_files_dropped)
        self.tree.dropRefused.connect(self.statusMessage)
        self.tree.currentItemChanged.connect(self._on_current_changed)
        # 只接 itemActivated：**双击和回车都会发它**（实测双击时
        # itemDoubleClicked 与 itemActivated 各发一次），两个都接就会
        # 一次双击把事件打开两遍（多起一个 DiffJob，界面还闪一下）。
        self.tree.itemActivated.connect(self._on_item_activated)
        # 分组那一行的三角：展开 / 收起就是**改库里的 collapsed**
        self.tree.itemExpanded.connect(
            lambda item: self._on_item_expanded(item, True))
        self.tree.itemCollapsed.connect(
            lambda item: self._on_item_expanded(item, False))
        self.tree.customContextMenuRequested.connect(self._on_menu)

    # ------------------------------------------------------------ 数据装载

    def set_actions(self, actions: list[QAction]) -> None:
        """

        传进来的 ``QAction`` **归主窗口所有**（``QAction(text, self)``），这里只是
        借它画一个按钮（``setDefaultAction``）—— 图标、tooltip、快捷键、
        "禁用状态"全跟着同一个对象走，不会出现"顶栏那份能点、侧栏那份是灰的"。
        """
        for action in actions:
            button = QToolButton(self.action_bar)
            button.setDefaultAction(action)         # 文字/图标/tooltip/触发都来自 action
            # 侧栏窄：只画图标；文字太长会挤成省略号，说明交给 tooltip
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
            button.setAutoRaise(True)
            button.setIconSize(QSize(18, 18))
            button.setToolTip(action.toolTip() or action.text())
            self.action_layout.addWidget(button)
            self.action_buttons.append(button)
        # 末尾顶一个弹簧：按钮从左边排起（不是均匀撑满整条）
        self.action_layout.addStretch(1)

    def action_button(self, text: str) -> QToolButton | None:
        """按动作文字找那个小按钮（测试点按钮用；找不到返回 None）。"""
        for button in self.action_buttons:
            action = button.defaultAction()
            if action is not None and action.text() == text:
                return button
        return None

    def rebuild(self, keep_selection: bool = True) -> None:
        """重新读库，把整棵树重画一遍（尽量保住原来的选中与展开状态）。"""
        project_id = self._project_id
        event_id = self._event_id
        expanded = self._expanded_projects()

        self._loading = True
        try:
            projects = self.library.list_projects()
            self.tree.clear()
            for project in projects:
                item = QTreeWidgetItem(self.tree)
                item.setText(0, f"{project.name}（{project.document_count}）")
                item.setIcon(0, trunk_icon(16))
                item.setData(0, ID_ROLE, project.id)
                item.setData(0, KIND_ROLE, "project")
                item.setToolTip(
                    0, f"{project.name}\n{project.document_count} 个事件枝 · "
                       f"{project.version_count} 版\n"
                       "把文件拖到这一行 = 在它下面新建事件枝"
                )
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
                self._fill_events(item, project.id)
                item.setExpanded(project.id in expanded)
        finally:
            self._loading = False

        if keep_selection and project_id is not None:
            self._project_id = None
            self._event_id = None
            if self.select_project(project_id):
                if event_id is not None:
                    self.select_event(event_id, notify=False)
            elif projects:
                # 原来选中的项目**已经不在了**（被删掉/换了库）：以前这里就
                # 什么也不做，整棵树没有当前行 —— 主窗口的 `_project_id`
                # 跟着变成 None，版图那块就空着。退回第一个项目。
                self.select_project(projects[0].id)
        elif projects:
            self.select_project(projects[0].id)
        else:
            self._project_id = None
            self._event_id = None
        self._update_header()

    def _fill_events(self, project_item: QTreeWidgetItem, project_id: int) -> None:
        """把某个项目的事件填成子节点（**按开始时间正序**，跟以前一样）。

        所以白板上的分组在这里是**一层节点**：``项目 → 分组 → 事件枝``，
        不属于任何分组的事件枝直接挂在项目下面（排在分组后面）。
        一个事件只能在一个分组下（v19 的唯一索引），所以它在这棵树里的位置是确定的。

        分组节点的展开 / 收起直接照 ``board_groups()[…]["collapsed"]`` 来 ——
        与右侧白板共用同一个字段，点哪边都是在改这一个字段（见
        ``SidebarWidget._on_item_expanded``）。

        搜着的时候项目行 / 分组行写成「命中 N / 共 M」—— 以前那串数字是从库里
        拿的总数，筛完只剩一行了它还写着「（4）」，看着像搜索没生效。
        """
        everything = []
        for root in self.library.node_tree(project_id):
            everything.extend(self.library.list_documents(root.id, include_descendants=True))
        filtering = self._matched_ids is not None
        events = ([item for item in everything if item.id in self._matched_ids]
                  if filtering else list(everything))
        events.sort(key=lambda item: (start_stamp_of(item), item.title))
        alerts = self.library.dependency_alerts(project_id)

        project = self.library.get_project(project_id)
        name = project.name if project is not None else project_item.text(0)
        project_item.setText(
            0,
            f"{name}（命中 {len(events)} / 共 {len(everything)}）" if filtering
            else f"{name}（{len(everything)}）",
        )

        groups = self.library.board_groups(project_id)
        in_group: set[int] = set()
        by_id = {event.id: event for event in events}
        for group in groups:
            for document_id in group.get("members") or []:
                in_group.add(int(document_id))
        for group in groups:
            all_members = [int(item) for item in (group.get("members") or [])]
            members = [by_id[document_id] for document_id in all_members
                       if document_id in by_id]
            node = QTreeWidgetItem(project_item)
            count = (f"命中 {len(members)} / 共 {len(all_members)}" if filtering
                     else str(len(all_members)))
            node.setText(0, f"{group['name'] or '未命名组'}（{count}）")
            node.setIcon(0, group_icon(15, ACCENT))
            node.setData(0, ID_ROLE, int(group["id"]))
            node.setData(0, KIND_ROLE, "group")
            note = str(group.get("note") or "")
            node.setToolTip(
                0,
                f"分组「{group['name'] or '未命名组'}」\n"
                + (f"{note}\n" if note else "")
                + f"{len(all_members)} 个事件枝\n"
                "点左边的三角 = 折叠 / 展开这一组（和白板上的框是同一个状态）\n"
                "右键 = 改名 / 解散 / 选中组里的事件枝 / 导出这一组的文件",
            )
            font = node.font(0)
            font.setBold(True)
            node.setFont(0, font)
            node.setForeground(0, QBrush(QColor(ACCENT)))
            # 折叠状态：**跟白板一致**（同一个 collapsed 字段）
            node.setExpanded(not group.get("collapsed"))
            for event in members:
                self._add_event_node(node, event, alerts)
        for event in events:
            if event.id in in_group:
                continue
            self._add_event_node(project_item, event, alerts)
        if filtering:
            # 过滤时：一个都没命中的项目藏起来（项目名自己命中就留着）
            project_item.setHidden(
                project_item.childCount() == 0
                and self._filter.lower() not in name.lower()
            )

    def _add_event_node(self, parent: QTreeWidgetItem, event, alerts: dict) -> None:
        """往 ``parent``（项目行 / 分组行）下挂一个事件枝节点。"""
        node = QTreeWidgetItem(parent)
        stale = alerts.get(event.id, [])
        node.setText(0, event.title)
        node.setIcon(0, _event_icon(event.state, bool(stale), 15))
        node.setText(1, start_text_of(event))
        node.setData(1, Qt.ItemDataRole.TextAlignmentRole,
                     int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter))
        node.setData(0, ID_ROLE, event.id)
        node.setData(0, KIND_ROLE, "event")
        node.setData(0, STATE_ROLE, event.state)
        if stale:
            node.setData(0, ATTENTION_ROLE, "、".join(stale))
            node.setForeground(0, QBrush(QColor(ATTENTION_TEXT)))
        latest = self.library.latest_version(event.id)
        tip = [event.title, f"开始 {start_text_of(event, full=True)}",
               f"{event.version_count} 版 · "
               f"{DOC_STATE_LABELS.get(event.state, event.state)}"]
        if latest:
            tip.append(f"最新 v{latest.seq}")
        group = (self.library.group_of_document(event.id)
                 if parent.data(0, KIND_ROLE) == "group" else None)
        if group:
            tip.append(f"在分组「{group['name'] or '未命名组'}」下")
        hits = self._matched_files.get(event.id)
        if hits:
            # 标题里没有那几个字，是因为**文件名**命中才留下的这一枝 ——
            # 不说清楚的话，树上这一行看不出跟搜索有什么关系
            tip.append("命中文件：" + "、".join(hits[:5])
                       + ("…" if len(hits) > 5 else ""))
        if stale:
            tip.append("⚠ 依赖的事件已更新：" + "、".join(stale)
                       + "（本事件还没出新稿）")
        node.setToolTip(0, "\n".join(tip))
        node.setToolTip(1, f"开始时间：{start_text_of(event, full=True)}")

    def _expanded_projects(self) -> set[int]:
        result = set()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item.isExpanded():
                result.add(int(item.data(0, ID_ROLE) or 0))
        return result

    def _update_header(self) -> None:
        projects = self.tree.topLevelItemCount()
        count = sum(len(_events_of(self.tree.topLevelItem(index)))
                    for index in range(projects))
        groups = sum(len(_group_children(self.tree.topLevelItem(index)))
                     for index in range(projects))
        name = ""
        if self._project_id is not None:
            project = self.library.get_project(self._project_id)
            name = f" · {project.name}" if project else ""
        # "事件枝"这一项**连分组里的也算**：分组是树上多出来的一层，
        # 事件并没有变少（以前只数项目行的直接子节点，加了分组之后数字会凭空变小）
        extra = f" / {groups} 个分组" if groups else ""
        self.header.setText(
            f"库（{projects} 个项目 / {count} 个事件枝{extra}）{name}"
        )

    def _on_item_expanded(self, item: QTreeWidgetItem, expanded: bool) -> None:
        """分组行的三角被点了：把"折起来了没有"写回库（与白板共用同一个字段）。

        ``_loading`` 期间不发信号：那是程序自己在按库里的状态摆树
        （``_fill_events`` / ``rebuild``），不是用户在按三角 —— 不然"照库里的
        折叠状态摆一遍"会被当成"用户又折了一次"，写库成了自说自话的回环。
        """
        if self._loading or item.data(0, KIND_ROLE) != "group":
            return
        self.groupCollapseRequested.emit(int(item.data(0, ID_ROLE) or 0), not expanded)

    def set_group_collapsed(self, group_id: int, collapsed: bool) -> None:
        """按库里的状态摆这一组在树上的展开 / 收起（主窗口改完库之后叫它）。

        只动这一个节点、不重建整棵树：折叠是"看一眼的方式"，重建会把用户
        滚到的位置和正在看的那一行都抖一下。
        """
        item = self._find_group_item(group_id)
        if item is None:
            return
        self._loading = True
        try:
            item.setExpanded(not collapsed)
        finally:
            self._loading = False

    def _selected_group_id(self) -> int | None:
        """当前选中的那一行如果是**分组**行，返回组 id。"""
        item = self.tree.currentItem()
        if item is None or item.data(0, KIND_ROLE) != "group":
            return None
        return int(item.data(0, ID_ROLE) or 0) or None

    # ---------------------------------------------------------------- 键盘

    def keyPressEvent(self, event):  # noqa: N802
        """

        回车那条分支删掉了：回车由 ``itemActivated`` 送到
        :meth:`_on_item_activated`（见 ``_build_ui`` 的接线），在这里再接一次
        就成了"一次回车打开两遍"。
        空格那条分支也删掉了：它是**死代码** —— 按键先送到有焦点的 ``self.tree``，
        只有 tree **不处理**的键才会冒泡到这个外层 QWidget（F2 / Delete 就是
        这样走到这里的，所以它们仍然有效）；而 QTreeWidget 自己把空格
        当"选中当前行"用掉了，空格永远冒不上来（实测：按空格不会走到这里，
        也不会展开）。
        不改成事件过滤器的理由：真要支持空格展开，就得先跟"空格=选中"抢键，
        收益不明确；而且提示语里本来也没承诺空格（只有 ↑↓ / ←→ / Enter /
        F2 / Delete），所以没有承诺需要同步。
        """
        key = event.key()
        if key == Qt.Key.Key_F2:
            document_id = self.current_event_id()
            if document_id:
                self.renameRequested.emit(document_id)
                event.accept()
                return
            group_id = self._selected_group_id()
            if group_id:
                self.groupRenameRequested.emit(group_id)
                event.accept()
                return
            project_id = self._selected_project_id()
            if project_id:
                self.renameProjectRequested.emit(project_id)
                event.accept()
                return
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            document_id = self.current_event_id()
            if document_id:
                self.deleteRequested.emit(document_id)
                event.accept()
                return
            # 分组行：Delete = 解散这个组（只去掉框和成员记录，事件一个都不动，
            # 与白板上右键那一项同一件事 —— 主窗口负责问一句）
            group_id = self._selected_group_id()
            if group_id:
                self.groupDissolveRequested.emit(group_id)
                event.accept()
                return
            # 选中的是**项目树**行时也认 Delete（菜单里那一项是"删除项目树…"）：
            # 键盘和右键两条路给同一件事，不然用户按了 Delete 只会以为坏了。
            project_id = self._selected_project_id()
            if project_id:
                self.deleteProjectRequested.emit(project_id)
                event.accept()
                return
        super().keyPressEvent(event)

    def _selected_project_id(self) -> int | None:
        """当前选中的那一行如果是**项目树**行，返回它的 id（否则 None）。

        为什么不能直接用 ``current_event_id()`` 的取法：项目行的 id 与事件行的 id
        在两个不同的表里，混着用就会"按 F2 改错东西"。
        """
        item = self.tree.currentItem()
        if item is None or item.data(0, KIND_ROLE) != "project":
            return None
        return int(item.data(0, ID_ROLE) or 0) or None

    # ---------------------------------------------------------------- 选中

    def _on_current_changed(self, current: QTreeWidgetItem | None,
                            _previous: QTreeWidgetItem | None) -> None:
        if self._loading or current is None:
            return
        kind = current.data(0, KIND_ROLE)
        target_id = int(current.data(0, ID_ROLE) or 0)
        if not target_id:
            return
        if kind == "group":
            # 点分组行 = 看这个项目的版图（分组本来就是白板上的东西）。
            # 不在这里"顺手把组里的都选上"：选中是白板上的手势，
            # 要那个有右键菜单里那条明确的入口（见 _dissolve…/_select… 那几条）。
            project_item = current.parent()
            project_id = int(project_item.data(0, ID_ROLE) or 0) if project_item else 0
            if project_id:
                self._event_id = None
                self._project_id = project_id
                self.projectSelected.emit(project_id)
                self._update_header()
            return
        if kind == "project":
            # 注意：即便它已经是"当前项目"也要发信号 —— 用户可能正看着某个事件枝，
            # 点一下项目树行就是想回到版图，不能因为"项目没变"就不响应。
            self._event_id = None
            self._project_id = target_id
            self.projectSelected.emit(target_id)
            self._update_header()
            return
        if target_id == self._event_id:
            return
        parent = current.parent()
        if parent is not None:
            # 事件可能挂在**分组行**下面（多了一层）：分组行的 id 是分组 id，
            # 拿它当项目 id 会发一个"选中了某个项目"的假信号（项目 id 与分组 id
            # 在两个表里，混着用就会切错版图）
            project_item = (parent if parent.data(0, KIND_ROLE) == "project"
                            else parent.parent())
            project_id = int(project_item.data(0, ID_ROLE) or 0) if project_item else 0
            if project_id and project_id != self._project_id:
                self._project_id = project_id
                self.projectSelected.emit(project_id)
        self._event_id = target_id
        self.eventSelected.emit(target_id)
        self._update_header()

    def _on_item_activated(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        self._activate(item)

    def _activate(self, item: QTreeWidgetItem) -> None:
        kind = item.data(0, KIND_ROLE)
        target_id = int(item.data(0, ID_ROLE) or 0)
        if not target_id:
            return
        if kind == "project":
            item.setExpanded(True)
            self.projectSelected.emit(target_id)
            return
        self.eventSelected.emit(target_id)

    def _on_files_dropped(self, kind: str, target_id: int, paths: list) -> None:
        """拖到**项目行 / 分组行**上：在它下面新建事件枝。

        分组行跟项目行走同一条路（同一个项目，只是新建出来的事件枝顺手进这个组）：
        分组行也就是"这棵树里的一个位置"，丢在上面却什么都不发生会让人以为拖坏了

        事件枝行那条路已经收掉了（``_EventTree.dropEvent`` 里直接拒绝并给提示），
        所以这里不再有 ``"document-attach"`` 这种模式。
        """
        if kind == "project":
            self.archiveRequested.emit(target_id, paths, "project")
        elif kind == "group":
            item = self._find_group_item(target_id)
            project_item = item.parent() if item is not None else None
            project_id = int(project_item.data(0, ID_ROLE) or 0) if project_item else 0
            if project_id:
                self.archiveRequested.emit(project_id, paths, f"group:{target_id}")

    # ---------------------------------------------------------------- 右键

    def _on_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        if item is None:
            return
        kind = item.data(0, KIND_ROLE)
        if kind == "group":
            group_id = int(item.data(0, ID_ROLE) or 0)
            if not group_id:
                return
            menu = QMenu(self)
            # 折叠 / 展开：和白板上的三角、和树里这个三角都是同一件事
            opened = item.isExpanded()
            menu.addAction(
                "把这一组折起来" if opened else "展开这一组",
                lambda: self.groupCollapseRequested.emit(group_id, not opened),
            )
            menu.addAction(pencil_icon(16), "改名分组…（F2）",
                           lambda: self.groupRenameRequested.emit(group_id))
            menu.addSeparator()
            menu.addAction("选中组里的事件枝",
                           lambda: self.groupSelectMembersRequested.emit(group_id))
            menu.addAction("导出这一组的文件…",
                           lambda: self.groupExportRequested.emit(group_id))
            menu.addSeparator()
            menu.addAction(
                prune_icon(16), "解散这个组…（Delete）",
                lambda: self.groupDissolveRequested.emit(group_id),
            ).setToolTip("只去掉这个框和成员记录，事件与文件一个都不动")
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        if kind == "project":
            project_id = int(item.data(0, ID_ROLE) or 0)
            if not project_id:
                return
            menu = QMenu(self)
            menu.addAction(
                "新建事件枝…",
                lambda: self.newEventRequested.emit(project_id),
            )
            # 改名 / 删除项目树：内核早就有（``update_project`` / ``delete_project``），
            # 功能怎么没有"）。删整棵树比删一个事件枝重得多，所以确认框要写清
            # 它下面有多少个事件枝（主窗口负责问）。
            menu.addSeparator()
            menu.addAction(
                pencil_icon(16), "改名项目树…（F2）",
                lambda: self.renameProjectRequested.emit(project_id),
            )
            # 排序也和工具栏上那两个按钮是同一件事（Ctrl+↑ / Ctrl+↓ 也行）：
            # 右键里再来一条，用户在哪顺手用哪个
            menu.addAction("上移（Ctrl+↑）",
                           lambda: self.moveProjectRequested.emit(project_id, -1))
            menu.addAction("下移（Ctrl+↓）",
                           lambda: self.moveProjectRequested.emit(project_id, 1))
            delete = menu.addAction(
                prune_icon(16), "删除项目树…",
                lambda: self.deleteProjectRequested.emit(project_id),
            )
            delete.setToolTip("把这棵树和它下面的全部事件枝、年轮记录一起从库里删掉；"
                              "不能撤销；你磁盘上的原始文件不动")
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return
        menu = QMenu(self)
        document_id = int(item.data(0, ID_ROLE) or 0)
        if not document_id:
            return
        state_menu = menu.addMenu("状态")
        for key, label in DOC_STATE_LABELS.items():
            action = state_menu.addAction(state_icon(key, 14), label)
            action.triggered.connect(
                lambda _checked=False, did=document_id, s=key:
                self.stateRequested.emit(did, s)
            )
        menu.addAction(pencil_icon(16), "改名…（F2）",
                       lambda: self.renameRequested.emit(document_id))
        # 归类分错了的事件枝要有地方挪：内核的 ``move_document`` 原来界面与 CLI
        # 都没接（只能删了重建，而重建就把年轮丢了）
        menu.addAction("移到别的项目树…",
                       lambda: self.moveEventRequested.emit(document_id))
        delete = menu.addAction(prune_icon(16), "删除…",
                                lambda: self.deleteRequested.emit(document_id))
        delete.setToolTip("连同年轮记录一起从库里删掉，不能撤销；你的原始文件不动")
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---------------------------------------------------------------- 搜索

    def set_filter(self, text: str) -> None:
        """按关键词过滤事件枝（标题命中或文件名命中都保留），并把命中的字标出来。"""
        needle = (text or "").strip()
        if needle == self._filter:
            return
        self._filter = needle
        self._matched_ids = None
        self._matched_files = {}
        if needle:
            try:
                hits = self.library.search(needle, limit=2000)
            except LibraryError:
                hits = []
            self._matched_ids = {hit["document"].id for hit in hits}
            # "这一枝为什么被搜出来"：标题里没那几个字时，多半是**文件名**命中。
            # 树上光看到一枝冒出来、字又没高亮，用户会以为搜索坏了 ——
            # 所以把命中的文件名记下来写进悬停提示（右边那页的文件行会高亮它）
            for hit in hits:
                names = [item.name for item in self._matched_file_names(hit)
                         if needle.casefold() in item.name.casefold()]
                if names:
                    self._matched_files[hit["document"].id] = names
        # 先把关键词交给 delegate，再重建树（重建时画的就是高亮过的样子）
        self._search_delegate.needle = needle
        self.rebuild()
        self.tree.viewport().update()

    def _matched_file_names(self, hit: dict) -> list:
        """这一枝里能拿来判断"为什么被搜出来"的文件行（当前稿 + 最近那一稿）。"""
        document = hit.get("document")
        if document is None:
            return []
        rows = []
        latest = hit.get("latest_version")
        if latest is not None:
            rows.extend(latest.files)
        if document.current_version_id:
            current = self.library.get_version(document.current_version_id)
            if current is not None and (latest is None or current.id != latest.id):
                rows.extend(current.files)
        return rows

    def first_match(self) -> int | None:
        """当前树里的第一个事件枝（搜索框回车用）。"""
        for index in range(self.tree.topLevelItemCount()):
            events = _events_of(self.tree.topLevelItem(index))
            if events:
                return int(events[0].data(0, ID_ROLE) or 0)
        return None

    # ------------------------------------------------------------ 外部调用

    def current_project_id(self) -> int | None:
        return self._project_id

    def current_event_id(self) -> int | None:
        return self._event_id

    def event_ids(self) -> list[int]:
        """按树里**看得见的顺序**给出事件 id —— "上一个/下一个"要跟眼睛看到的一致。

        分组里的也算（``_events_of`` 会把两层都走一遍）：分组只是归类，
        不该让组里的事件的 ↑/↓ 走不到。
        """
        ids: list[int] = []
        for index in range(self.tree.topLevelItemCount()):
            for node in _events_of(self.tree.topLevelItem(index)):
                value = int(node.data(0, ID_ROLE) or 0)
                if value:
                    ids.append(value)
        return ids

    def _find_project_item(self, project_id: int) -> QTreeWidgetItem | None:
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if int(item.data(0, ID_ROLE) or 0) == project_id:
                return item
        return None

    def _find_group_item(self, group_id: int) -> QTreeWidgetItem | None:
        for index in range(self.tree.topLevelItemCount()):
            for node in _group_children(self.tree.topLevelItem(index)):
                if int(node.data(0, ID_ROLE) or 0) == int(group_id):
                    return node
        return None

    def _find_event_item(self, document_id: int) -> QTreeWidgetItem | None:
        for index in range(self.tree.topLevelItemCount()):
            for node in _events_of(self.tree.topLevelItem(index)):
                if int(node.data(0, ID_ROLE) or 0) == document_id:
                    return node
        return None

    def _project_id_of(self, item: QTreeWidgetItem) -> int:
        """某一行**属于哪个项目树**（项目行自己 / 分组行 / 事件枝行都认）。

        事件枝可能挂在分组行下面（树有三层），所以要一路上溯到项目行 ——
        拿"上一层"的 id 当项目 id 会把分组 id 混进来（两个表里的 id）。
        """
        node = item
        while node is not None and node.data(0, KIND_ROLE) != "project":
            node = node.parent()
        return int(node.data(0, ID_ROLE) or 0) if node is not None else 0

    def select_project(self, project_id: int, *, expand: bool = True,
                       force: bool = False) -> bool:
        """按项目 id 选中它（**不发** ``projectSelected``，避免信号回环）。

        :param force: 一定要把**当前行**挪到项目行上（右键菜单"先选中、再挪一格"
            那条路要的就是这个）。默认 ``False``：当前那一行已经在这棵树里
            （事件枝行 / 分组行）时**不动它** —— "同步选中"是让侧栏指向这个项目，
            不是把用户刚点的那一行抢走。
              于是紧接着按 Delete，删掉的是**整棵项目树**。）
        """
        item = self._find_project_item(project_id)
        if item is None:
            return False
        current = self.tree.currentItem()
        if not force and current is not None \
                and self._project_id_of(current) == project_id:
            self._loading = True
            try:
                item.setExpanded(expand)
                self._project_id = project_id
                self._event_id = None
            finally:
                self._loading = False
            self._update_header()
            return True
        self._loading = True
        try:
            self.tree.setCurrentItem(item)
            item.setExpanded(expand)
            self._project_id = project_id
            self._event_id = None
        finally:
            self._loading = False
        self._update_header()
        return True

    def select_event(self, document_id: int, notify: bool = True) -> bool:
        """按事件 id 选中它（项目自动展开、并且**滚到看得见**）。

        :param notify: 是否发 ``eventSelected``；主窗口自己打开事件时传 False
            （否则"打开事件 → 同步侧栏 → 又发信号"会绕回自己）。
        """
        item = self._find_event_item(document_id)
        if item is None:
            # 事件可能属于别的项目：先定位它在哪个项目，再展开那个项目
            with Library.open(self.library_root) as probe:
                event = probe.get_document(document_id)
                if event is None:
                    return False
                node = probe.get_node(event.node_id)
                project_id = node.project_id if node else None
            if project_id is None:
                return False
            project_item = self._find_project_item(project_id)
            if project_item is None:
                return False
            self._loading = True
            try:
                self.tree.setCurrentItem(project_item)
                project_item.setExpanded(True)
                self._project_id = project_id
            finally:
                self._loading = False
            item = self._find_event_item(document_id)
            if item is None:
                return False
        self._loading = True
        try:
            self.tree.setCurrentItem(item)
            parent = item.parent()
            if parent is not None and parent.data(0, KIND_ROLE) == "group":
                # 事件挂在**分组行**下面：这一行要能看见，那一组就得是摊开的。
                # 摊开这件事**必须连库一起改**（左右两边共用 collapsed 字段），
                # 所以走信号让主窗口去写库 + 重画白板 —— 只 setExpanded 的话，
                if not parent.isExpanded():
                    self.groupCollapseRequested.emit(
                        int(parent.data(0, ID_ROLE) or 0), False)
                parent.setExpanded(True)
                parent = parent.parent()
            if parent is not None:
                parent.setExpanded(True)
                self._project_id = int(parent.data(0, ID_ROLE) or 0)
            self._event_id = document_id
            self.tree.scrollToItem(item)
        finally:
            self._loading = False
        self._update_header()
        if notify:
            self.eventSelected.emit(document_id)
        return True

    def refresh_event_row(self, document_id: int) -> None:
        """只更新某个事件枝节点（改状态/改名/改时间后不必整树重建）。"""
        event = self.library.get_document(document_id)
        item = self._find_event_item(document_id)
        if event is None or item is None:
            return
        stale = (self.library.dependency_alerts(self._project_id).get(document_id, [])
                 if self._project_id else [])
        item.setText(0, event.title)
        item.setIcon(0, _event_icon(event.state, bool(stale), 15))
        item.setData(0, STATE_ROLE, event.state)
        item.setText(1, start_text_of(event))
        if stale:
            item.setData(0, ATTENTION_ROLE, "、".join(stale))
            item.setForeground(0, QBrush(QColor(ATTENTION_TEXT)))
        else:
            item.setData(0, ATTENTION_ROLE, None)
            item.setForeground(0, QBrush(QColor(TEXT)))

    def close(self) -> None:
        try:
            self.library.close()
        except Exception:
            pass

"""底部"年轮"窗口：照 CLion 的 Git 工具窗口做。

用户的要求原话：

::

    ┌ 年轮 ──────────────────────────────[＋ 新增一稿] [删除这一稿…] ┐
    │ 版本  时间              与上一稿的差别            │ 这一稿改了什么      │
    │ v5    2025-07-06 13:46  +6 附件 · 说明：补了结论   │ （真实的逐行差异）  │
    │ v4    2025-07-06 12:11  主文件更新 · 说明：改图注 │                    │
    │ …                                                 │                    │
    │ v1    2025-07-06 12:12  创建 · 说明：先记录        │                    │
    └───────────────────────────────────────────────────┴────────────────────┘
       └──── 左边 2/3：版本列表（最新在最上，最早在最下）────┘└─ 右 1/3 ─┘

点某一行 = 画布切到那一稿，右侧同时算出"这一稿相对上一稿"的真实差异
（后台线程算，不卡界面）。那一行的差别后面还接着**这一稿的说明文字**
（见 ``event_view._fill_timeline``：格子压成一行，悬停提示里是原文）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .diff_dialog import diff_html
from .style import ACCENT, MUTED, prune_icon, scoped_style, twig_icon
from .workers import DiffJob

__all__ = ["TimelinePanel"]

#: 后台差异任务的轮询间隔（毫秒）
POLL_INTERVAL_MS = 80

#: 表格列
COL_VERSION = 0
COL_TIME = 1
COL_CHANGE = 2


class TimelinePanel(QWidget):
    """底部的"年轮"窗口：左边列表、右边差异、上面两个按钮。"""

    #: 选中了某一稿（主窗口据此把画布切过去）
    versionSelected = Signal(int)
    #: 按了「＋ 新增一稿」（主窗口负责建一稿空的并切过去）
    newVersionRequested = Signal()
    #: 按了「删除这一稿…」（主窗口负责确认 + 真删）
    deleteRequested = Signal(int)
    #: 希望看一眼某一稿的差异（内部自己也算，这里只是给主窗口一个钩子）
    diffRequested = Signal(int)

    def __init__(self, library_root: str | Path, parent: QWidget | None = None):
        super().__init__(parent)
        self.library_root = Path(library_root)
        self._versions: list = []
        self._selected_id: int | None = None
        self._job: DiffJob | None = None
        #: 用户是否点过「在后台比较」（点了就不再跳过）
        self._allow_large = False
        self._job_pair: tuple[int, int] | None = None
        #: 当前比较只盯**一份文件**（点文件时）；None = 比较整个版本
        self._file_id: int | None = None
        self._loading = False

        self.setObjectName("timelinePanel")
        self.setStyleSheet(
            "#timelinePanel{background:#fbfcfe;border-top:1px solid #e3e9f0;}"
            "#timelinePanel QLabel{color:#4a5666;}"
            "#timelinePanel QTableWidget{border:none;background:#ffffff;"
            "font-size:9pt;gridline-color:#eef2f7;}"
            "#timelinePanel QHeaderView::section{background:#f4f7fb;border:none;"
            "border-bottom:1px solid #e3e9f0;padding:3px 6px;color:#4a5666;}"
            "#timelinePanel QTextEdit{border:none;background:#ffffff;}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(6)

        # ---- 顶上一行：标题 + 两个按钮（都做在这个窗口上）
        head = QHBoxLayout()
        head.setSpacing(6)
        title = QLabel("年轮", self)
        title.setToolTip("最新的一稿在最上面，最早的一稿在最下面")
        head.addWidget(title)
        self.state_label = QLabel("", self)
        scoped_style(self.state_label, f"color:{MUTED};font-size:8.6pt;")
        head.addWidget(self.state_label)
        head.addStretch(1)

        self.add_button = QPushButton("新增一稿", self)
        # 图标是一小截**枝条**（主干 + 三片叶，跟头部那段装饰枝条同一种画法）
        self.add_button.setIcon(twig_icon(16))
        self.add_button.setIconSize(QSize(16, 16))
        self.add_button.setToolTip(
            "新增一稿（枝在长）——点下去就直接建一稿空的、界面切到它；"
            "之后的说明和文件都进这一稿，不用先给文件"
        )
        self.add_button.clicked.connect(lambda _checked=False: self.newVersionRequested.emit())
        head.addWidget(self.add_button)

        self.delete_button = QPushButton(prune_icon(16), "删除这一稿…", self)
        self.delete_button.setToolTip(
            "删掉选中的这一稿（会先问你一次）。\\n"
            "磁盘上的原文件不动；事件至少要留一稿，所以最后一稿删不掉。"
        )
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self._on_delete)
        head.addWidget(self.delete_button)
        outer.addLayout(head)

        # ---- 左边 2/3 列表 + 右边 1/3 差异
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.setChildrenCollapsible(False)

        self.table = QTableWidget(0, 3, split)
        self.table.setHorizontalHeaderLabels(["版本", "时间", "与上一稿的差别"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(COL_VERSION, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_TIME, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(COL_CHANGE, QHeaderView.ResizeMode.Stretch)
        self.table.currentCellChanged.connect(self._on_row_changed)
        split.addWidget(self.table)

        right = QWidget(split)
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(6, 0, 0, 0)
        right_layout.setSpacing(2)
        self.diff_header = QLabel("这一稿改了什么", right)
        scoped_style(self.diff_header, "font-size:8.8pt;color:#4a5666;")
        right_layout.addWidget(self.diff_header)

        # 「在后台比较」：只有"文件太大所以没自动比"时才冒出来
        self.compare_button = QPushButton("在后台比较", self)
        self.compare_button.setToolTip(
            "这个文件比较大，自动比较会算很久（界面会跟着拖住）。\n"
            "点这里在后台线程里算，算完差异就显示在下面。"
        )
        self.compare_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.compare_button.clicked.connect(self.compare_now)
        self.compare_button.setVisible(False)
        right_layout.addWidget(self.compare_button)
        self.diff_view = QTextEdit(right)
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        mono = QFont("DejaVu Sans Mono")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSizeF(8.6)
        self.diff_view.setFont(mono)
        right_layout.addWidget(self.diff_view, 1)
        split.addWidget(right)
        # 左（版本列表）窄、右（差异）宽：差异内容多，给右边更多地方
        split.setSizes([280, 560])
        self.splitter = split
        outer.addWidget(split, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll_diff)
        self.setMinimumHeight(120)

    # ---------------------------------------------------------------- 内容

    def set_versions(self, entries: list[tuple], selected: int | None) -> None:
        """填列表。``entries`` = [(version_id, 版本文字, 时间文字, 差别文字, 是不是最新)]

        **最新的一稿排在最上面，最早的一稿排在最下面**（照 CLion 的 Git 日志）。

        ``差别文字`` 里除了"与上一稿的差别"，通常还接着这一稿的说明
        （``· 说明：…``，由 ``event_view._fill_timeline`` 拼好）。
        元组可以再带第 6 项：**悬停提示的文字** —— 格子只有一行、长说明会被省略号
        截掉，提示里给的是原文（换行照旧）。不给就用格子文字当提示。
        """
        self._loading = True
        try:
            rows = list(reversed(entries))      # 早 → 晚 反过来：新在最上
            self.table.setRowCount(len(rows))
            for index, entry in enumerate(rows):
                version_id, version_text, time_text, change_text, is_latest = entry[:5]
                tooltip = entry[5] if len(entry) > 5 and entry[5] else change_text
                version_item = QTableWidgetItem(version_text + ("（最新）" if is_latest else ""))
                version_item.setData(Qt.ItemDataRole.UserRole, version_id)
                if is_latest:
                    version_item.setForeground(QBrush(QColor(ACCENT)))
                    font = version_item.font()
                    font.setBold(True)
                    version_item.setFont(font)
                time_item = QTableWidgetItem(time_text)
                change_item = QTableWidgetItem(change_text)
                change_item.setToolTip(tooltip)
                self.table.setItem(index, COL_VERSION, version_item)
                self.table.setItem(index, COL_TIME, time_item)
                self.table.setItem(index, COL_CHANGE, change_item)
        finally:
            self._loading = False
        self._versions = list(entries)
        self._selected_id = selected
        self.select(selected)

    def select(self, version_id: int | None) -> None:
        """选中某一稿（并滚进可见范围）。"""
        self._selected_id = version_id
        self._loading = True
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, COL_VERSION)
                if item is not None and item.data(Qt.ItemDataRole.UserRole) == version_id:
                    self.table.selectRow(row)
                    self.table.scrollToItem(item)
                    break
        finally:
            self._loading = False
        self._refresh_buttons()
        self._refresh_diff(version_id)

    def selected_version(self) -> int | None:
        return self._selected_id

    def rows(self) -> list[tuple[int, str, str, str]]:
        """(version_id, 版本, 时间, 差别) —— 按界面顺序（新在上）。"""
        out: list[tuple[int, str, str, str]] = []
        for row in range(self.table.rowCount()):
            version_item = self.table.item(row, COL_VERSION)
            if version_item is None:
                continue
            out.append((
                int(version_item.data(Qt.ItemDataRole.UserRole)),
                version_item.text(),
                self.table.item(row, COL_TIME).text(),
                self.table.item(row, COL_CHANGE).text(),
            ))
        return out

    # ---------------------------------------------------------------- 按钮

    def _refresh_buttons(self) -> None:
        self.delete_button.setEnabled(
            bool(self._selected_id) and self.table.rowCount() > 1
        )

    def _on_delete(self) -> None:
        if self._selected_id:
            self.deleteRequested.emit(int(self._selected_id))

    def _on_row_changed(self, row: int, _column: int, _prev_row: int, _prev_column: int) -> None:
        if self._loading or row < 0:
            return
        item = self.table.item(row, COL_VERSION)
        if item is None:
            return
        version_id = int(item.data(Qt.ItemDataRole.UserRole))
        if version_id == self._selected_id:
            return
        self._selected_id = version_id
        self.state_label.setText("")
        self._refresh_buttons()
        self.versionSelected.emit(version_id)

    # ---------------------------------------------------------------- 差异

    def _previous_of(self, version_id: int | None):
        index = next(
            (i for i, item in enumerate(self._versions) if item[0] == version_id), -1
        )
        return self._versions[index - 1] if index > 0 else None

    def _refresh_diff(self, version_id: int | None) -> None:
        """在后台算"这一稿 vs 上一稿"，算完填到右边。"""
        previous = self._previous_of(version_id)
        if previous is None:
            # 没有上一稿可比：把还在跑的任务"作废"（_job_pair 置空后，
            # 它回来的结果会被丢弃，不会盖掉这里的提示）
            self._job_pair = None
            self._timer.stop()
            self.diff_header.setText("这一稿改了什么")
            self.diff_view.setHtml(
                "<div style='color:#6b7684'>这是第一稿，没有可比较的上一稿</div>"
            )
            return
        pair = (previous[0], version_id)
        if pair == self._job_pair and self._job is not None and self._job.is_running():
            return
        self._job_pair = pair
        self.diff_header.setText(f"这一稿改了什么（{previous[1]} → 本稿）")
        self.diff_view.setHtml("<div style='color:#6b7684'>正在比较…</div>")
        # **不要**在这里等上一个任务 —— 原来 wait(2000) 会把界面卡住两秒，
        # 大文件的比较远不止两秒，用户的感觉就是"切一稿卡一下"
        # （过期结果由 _job_pair 拦掉，安全的）
        self._allow_large = False
        self._file_id = None
        self._job = DiffJob(self.library_root, previous[0], version_id)
        self._job.start()
        self._timer.start()
        if version_id:
            self.diffRequested.emit(int(version_id))

    def show_file_diff(self, version_id: int, file_id: int, file_name: str) -> None:
        """点文件时：右下窗口**只**比较这一份文件和上一稿对应环节里的文件。"""
        previous = self._previous_of(version_id)
        if previous is None:
            self._job_pair = None
            self._timer.stop()
            self._file_id = None
            self.diff_header.setText("这一份改了什么")
            self.diff_view.setHtml(
                "<div style='color:#6b7684'>这是第一稿，没有可比较的上一稿</div>"
            )
            return
        pair = (previous[0], version_id)
        if pair == self._job_pair and self._file_id == file_id \
                and self._job is not None and self._job.is_running():
            return
        self._job_pair = pair
        self._file_id = file_id
        self._allow_large = False
        self.diff_header.setText(f"{file_name} 改了什么（{previous[1]} → 本稿）")
        self.diff_view.setHtml("<div style='color:#6b7684'>正在比较…</div>")
        self._job = DiffJob(self.library_root, previous[0], version_id, file_id=file_id)
        self._job.start()
        self._timer.start()

    def _skipped_html(self, result) -> str:
        reason = " ".join(result.details) or result.summary
        return (
            "<div style='color:#8a6516;background:#fdf6e0;border:1px solid #e3cf94;"
            "border-radius:6px;padding:8px'>"
            f"{reason}</div>"
            "<div style='color:#6b7684;font-size:11px;margin-top:6px'>"
            "自动比较只在文件不大时做（大文件会算很久、把界面拖住）。"
            "点上面的「在后台比较」，算完会显示在这里。</div>"
        )

    def compare_now(self) -> None:
        """用户明确要求"大文件也比我一次"：在后台跑（界面不卡）。"""
        pair = self._job_pair
        if pair is None:
            return
        self._allow_large = True
        self.compare_button.setVisible(False)
        self.diff_view.setHtml(
            "<div style='color:#6b7684'>正在后台比较（大文件可能要一会儿）…</div>"
        )
        if self._job is not None:
            # 上一个任务（多半是"已跳过"的那次）给一小会儿收尾就行；
            # 等太久用户刚点下去就卡住了
            self._job.wait(250)
        self._job = DiffJob(self.library_root, pair[0], pair[1], allow_large=True,
                            file_id=self._file_id)
        self._job.start()
        self._timer.start()

    def _poll_diff(self) -> None:
        job = self._job
        if job is None or self._job_pair is None:
            self._timer.stop()
            return
        for event in job.drain_events():
            if (event.old_version_id, event.new_version_id) != self._job_pair:
                continue      # 用户已经切到别的稿了，丢弃过期结果
            if getattr(event.result, "skipped", False) and not self._allow_large:
                self.diff_view.setHtml(self._skipped_html(event.result))
                self.compare_button.setVisible(True)
                continue
            self.compare_button.setVisible(False)
            self.diff_view.setHtml(diff_html(event.result))
        if not job.is_running() and not job.events.qsize():
            self._timer.stop()

    def close_panel(self) -> None:
        self._timer.stop()
        if self._job is not None:
            # 退出程序时才走这里：等它写完缓存再走，但别让"退出"卡好几秒
            self._job.wait(1500)

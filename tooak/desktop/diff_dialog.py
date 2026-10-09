"""按需打开的"这一稿改了什么"窗口。

改动摘要就写在时间轴卡片上。要把**整篇差异**摊开看时，在时间轴卡片上右键
「看这一稿改了什么…」打开这个窗口 —— 需要才出现，不占常驻界面。

差异在后台线程里算（大文件抽取/比对可能要几秒），算完再填进窗口。
"""

from __future__ import annotations

import html
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .workers import DiffJob

__all__ = ["DiffDialog", "diff_html"]

#: 轮询后台差异任务的间隔（毫秒）
POLL_INTERVAL_MS = 80


def diff_html(result) -> str:
    """把差异结果渲染成 HTML（加号绿、减号红、@@ 蓝）。"""
    parts: list[str] = []
    if result.details:
        parts.append(
            "<div style='color:#6b7684;font-size:11px'>"
            + "<br>".join(html.escape(line) for line in result.details)
            + "</div>"
        )
    if result.unified:
        rows: list[str] = []
        for line in result.unified.splitlines():
            escaped = html.escape(line) or "&nbsp;"
            if line.startswith("+++") or line.startswith("---"):
                rows.append(f"<div style='color:#8a94a0'>{escaped}</div>")
            elif line.startswith("@@"):
                rows.append(f"<div style='color:#0e6b3d;font-weight:600'>{escaped}</div>")
            elif line.startswith("+"):
                rows.append(f"<div style='color:#237a4b;background:#eefaf2'>{escaped}</div>")
            elif line.startswith("-"):
                rows.append(f"<div style='color:#a8403a;background:#fdeeed'>{escaped}</div>")
            else:
                rows.append(f"<div>{escaped}</div>")
        parts.append("<div>" + "".join(rows) + "</div>")
    elif result.kind == "same":
        parts.append("<div style='color:#6b7684'>两稿内容一致</div>")
    return "".join(parts) or "<div style='color:#6b7684'>（无可显示的差异）</div>"


class DiffDialog(QDialog):
    """看"这一稿相对上一稿改了什么"（需要时才打开）。"""

    def __init__(self, library_root: str | Path, document_title: str,
                 versions: list, version_id: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.library_root = Path(library_root)
        self._versions = list(versions)
        self._version_id = version_id
        self._job: DiffJob | None = None

        self.setWindowTitle("这一稿改了什么")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self.title_label = QLabel(f"{document_title} · 这一稿改了什么", self)
        self.title_label.setProperty("role", "title")
        layout.addWidget(self.title_label)

        self.status_label = QLabel("", self)
        self.status_label.setProperty("role", "hint")
        layout.addWidget(self.status_label)

        self.view = QTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        mono = QFont("DejaVu Sans Mono")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPointSizeF(9.0)
        self.view.setFont(mono)
        layout.addWidget(self.view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        copy_button = QPushButton("复制全部", self)
        copy_button.clicked.connect(self._copy_all)
        close_button = QPushButton("关闭", self)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(copy_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._poll)
        self._start()

    # ---------------------------------------------------------------- 差异

    def _previous(self):
        index = next(
            (i for i, item in enumerate(self._versions) if item.id == self._version_id), -1
        )
        return self._versions[index - 1] if index > 0 else None

    def _start(self) -> None:
        previous = self._previous()
        if previous is None:
            self.status_label.setText("这是第一稿，没有可比较的上一稿")
            self.view.setHtml("<div style='color:#6b7684'>（没有上一稿）</div>")
            return
        current = next(
            (item for item in self._versions if item.id == self._version_id), None
        )
        self.status_label.setText(
            f"正在比较 v{previous.seq} → v{current.seq if current else '?'} …"
        )
        # 这个窗口是用户**主动点开**的（右键"看这一稿改了什么"），
        # 所以不设大小闸门：点了就是要求比较，大文件也在后台算，不挡界面。
        self._job = DiffJob(self.library_root, previous.id, self._version_id,
                            allow_large=True)
        self._job.start()
        self._timer.start()

    def _poll(self) -> None:
        job = self._job
        if job is None:
            self._timer.stop()
            return
        for event in job.drain_events():
            self._show(event.result)
        if not job.is_running() and not job.events.qsize():
            self._timer.stop()

    def _show(self, result) -> None:
        previous = self._previous()
        current = next(
            (item for item in self._versions if item.id == self._version_id), None
        )
        self.status_label.setText(
            f"v{previous.seq if previous else '?'} → "
            f"v{current.seq if current else '?'}："
            f"{(result.stat_text if hasattr(result, 'stat_text') else '')}"
        )
        self.view.setHtml(diff_html(result))

    # ---------------------------------------------------------------- 其它

    def _copy_all(self) -> None:
        self.view.selectAll()
        self.view.copy()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._timer.stop()
        if self._job is not None:
            # 只等一小会儿：后台比较是 daemon 线程，等不到就让它自己跑完，
            # 不能为了"收干净"把关窗口卡住（原来这里等 5 秒，用户感觉就是卡死）。
            self._job.wait(800)
        super().closeEvent(event)

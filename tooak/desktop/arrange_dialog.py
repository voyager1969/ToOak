"""「网格排列」的选项窗口：把一批事件排成**几行几列**。

现在设计不好看，有元素重叠。"

所以这里给的都是**直接影响观感**的选项：

* 列数 / 行数：填一个，另一个按数量自动算；勾上"按数量自动"就两个都不用管；
* 间距：卡片之间留多少空隙（默认 34，就是自动布局的节奏）；
* 顺序：按现在的摆法读（先上后下、先左后右）—— 用户在画布上看到的次序
  通常就是他想要的次序，所以默认不动它。

对话框只负责问参数，位置怎么算是 ``ProjectBoardView.grid_layout()`` 的事。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .project_view import GRID_GAP, grid_shape

__all__ = ["GridArrangeDialog"]


class GridArrangeDialog(QDialog):
    """问"排成几行几列、间距多少"。"""

    def __init__(self, count: int, cols: int = 0, rows: int = 0,
                 gap: float = GRID_GAP, parent: QWidget | None = None):
        super().__init__(parent)
        self.count = max(int(count), 1)
        auto = not cols and not rows
        start_cols, start_rows = grid_shape(self.count, cols, rows)

        self.setWindowTitle("网格排列")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(10)

        title = QLabel(f"把 {self.count} 个事件枝排成网格", self)
        title.setProperty("role", "title")
        layout.addWidget(title)

        hint = QLabel("顺序按现在的摆法读（先上后下、先左后右）。", self)
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.cols_spin = QSpinBox(self)
        self.cols_spin.setRange(1, 24)
        self.cols_spin.setValue(start_cols)
        self.cols_spin.setSuffix(" 列")
        form.addRow("列", self.cols_spin)

        self.rows_spin = QSpinBox(self)
        self.rows_spin.setRange(1, 24)
        self.rows_spin.setValue(start_rows)
        self.rows_spin.setSuffix(" 行")
        form.addRow("行", self.rows_spin)

        self.gap_spin = QSpinBox(self)
        self.gap_spin.setRange(0, 400)
        self.gap_spin.setValue(int(gap))
        self.gap_spin.setSuffix(" 像素")
        form.addRow("间距", self.gap_spin)

        layout.addLayout(form)

        self.auto_check = QCheckBox("按数量自动（只按列数排）", self)
        self.auto_check.setChecked(auto)
        layout.addWidget(self.auto_check)

        self.result_label = QLabel("", self)
        self.result_label.setProperty("role", "hint")
        layout.addWidget(self.result_label)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("排一下")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sync()
        self.cols_spin.valueChanged.connect(self._sync)
        self.rows_spin.valueChanged.connect(self._sync)
        self.auto_check.toggled.connect(self._sync)

    # ------------------------------------------------------------------ 交互

    def _sync(self) -> None:
        """勾了"按数量自动"就锁住行数（列数说了算）；没勾就顺着用户填的来。"""
        auto = self.auto_check.isChecked()
        self.rows_spin.setEnabled(not auto)
        if auto:
            self.rows_spin.blockSignals(True)
            self.rows_spin.setValue(max(1, -(-self.count // max(self.cols_spin.value(), 1))))
            self.rows_spin.blockSignals(False)
        cols, rows = self.shape()
        needed = max(1, -(-self.count // max(cols, 1)))
        text = f"{self.count} 个事件枝 → {cols} 列 × {needed} 行"
        if rows * cols < self.count:
            text += "（填不下，会自己往下多排几行）"
        elif rows * cols > self.count:
            text += "（最后一行空几格）"
        self.result_label.setText(text)

    def shape(self) -> tuple[int, int]:
        """对话框里选的 ``(列, 行)``。"""
        return self.cols_spin.value(), self.rows_spin.value()

    def values(self) -> tuple[int, int, float]:
        cols, rows = self.shape()
        return cols, rows, float(self.gap_spin.value())

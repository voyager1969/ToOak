"""画布上的"撤销 / 重做"。

为什么自己做而不是用 ``QUndoStack``
------------------------------------
Qt 的 ``QUndoStack`` 要你为每个操作写一个 ``QUndoCommand`` 子类，而且它假设
"每个操作都是对象"。可这里的操作**全都是一个数据库调用**（挪位置、删卡片、
剪断连线…），真实状态在库里而不在内存里，所以最贴合的做法是：

* 每条撤销记录 = **一句话 + 一对闭包**（``undo`` / ``redo``），
  闭包里就是"把库改回去 / 再改回来"；
* 撤销栈放在主窗口里，容器**只记命令**、不碰具体业务。

这样有个好处：任何新增的写操作，只要写两行闭包就能进撤销栈，不用继承一堆类。

几条约定
--------
* **撤销的是"库里的事实"，不是界面**：执行完闭包后统一刷新一次画布/侧栏，
  界面永远是从库里长出来的（这也是整个项目的原则）。
* **重做栈**：一旦有新动作，重做栈就清掉（和所有编辑器一致）。
* **有深度上限**（默认 100 步），免得长时间使用把内存拖住。
* **不可撤销的操作要明说**：像"彻底删除事件"这种，直接不进栈，
  并且在界面上二次确认 —— 宁可少支持，不要给假的安全感。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QObject, Signal

__all__ = ["UndoEntry", "UndoStack"]

#: 默认记多少步
DEFAULT_DEPTH = 100


@dataclass
class UndoEntry:
    """一条可撤销的操作：一句话 + 一对闭包。"""

    label: str
    undo: Callable[[], None]
    redo: Callable[[], None]


@dataclass
class UndoStack(QObject):
    """撤销 / 重做栈（只记命令，具体业务在闭包里）。"""

    #: 栈变了（界面据此更新按钮可用状态与提示）
    changed = Signal()

    depth: int = DEFAULT_DEPTH
    _done: list[UndoEntry] = field(default_factory=list)
    _undone: list[UndoEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        super().__init__()

    # ---------------------------------------------------------------- 记

    def push(self, label: str, undo: Callable[[], None],
             redo: Callable[[], None]) -> None:
        """记一条新操作（会把重做栈清掉）。"""
        self._done.append(UndoEntry(label, undo, redo))
        if len(self._done) > self.depth:
            del self._done[0]
        self._undone.clear()
        self.changed.emit()

    # ---------------------------------------------------------------- 查

    def can_undo(self) -> bool:
        return bool(self._done)

    def can_redo(self) -> bool:
        return bool(self._undone)

    def undo_label(self) -> str:
        return self._done[-1].label if self._done else ""

    def redo_label(self) -> str:
        return self._undone[-1].label if self._undone else ""

    def top(self) -> UndoEntry | None:
        """栈顶那条记录（没有就返回 ``None``）。

        给"把连续微调合并成一条"用：调用方（主窗口）需要确认
        "栈顶还是我上一次记的那条"，否则合并会把别人的操作吞掉。
        """
        return self._done[-1] if self._done else None

    def replace_top(self, undo: Callable[[], None],
                    redo: Callable[[], None], label: str = "") -> bool:
        """把栈顶那条记录的闭包换掉（用来合并连续操作），返回是否换成。

        为什么需要它：方向键一次挪 5px 也是一次"移动"，敲十几下就能把
        100 步的栈挤爆。合并的做法是"把上一条改写成：回到这一串微调**最开头**的
        位置 / 去到**最新**的位置" —— 这必须是**原地改写**，
        因为中间那些位置要么没意义，要么用户根本不想一步步退。
        """
        if not self._done:
            return False
        entry = self._done[-1]
        self._done[-1] = UndoEntry(label or entry.label, undo, redo)
        self.changed.emit()
        return True

    def __len__(self) -> int:
        return len(self._done)

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()
        self.changed.emit()

    # ---------------------------------------------------------------- 走

    def undo(self) -> str | None:
        """撤销一步，返回做了什么的说明（没得撤就返回 ``None``）。

        抛出去以后记录已经从 ``_done`` 掉出来了，就变成"既撤不回、也重做不了"——
        用户的操作凭空消失。现在用 ``try/finally``：无论闭包成没成，
        记录都进重做栈（界面还能再试一次，也能看到失败提示而不至于丢状态）。
        """
        if not self._done:
            return None
        entry = self._done.pop()
        try:
            entry.undo()
        finally:
            self._undone.append(entry)
            self.changed.emit()
        return entry.label

    def redo(self) -> str | None:
        """重做一步（没得重做返回 ``None``）——失败保留记录的理由同 :meth:`undo`。"""
        if not self._undone:
            return None
        entry = self._undone.pop()
        try:
            entry.redo()
        finally:
            self._done.append(entry)
            self.changed.emit()
        return entry.label

"""搜索命中的高亮：把用户搜的那几个字**在界面文字上标出来**。


左边那棵树、右边白板上的卡片标题、事件枝页里的文件行 —— 这三处的文字是
三套不同的画法（`QTreeWidget` 的 delegate、`QGraphicsItem.paint`、
`FileRow.paint`），共用的是**同一件事**：先找出命中的区间，再把这些区间
连底色带字色画一遍。所以算法放这里，各处的画法自己接。

两条纪律：

* **大小写不敏感**（``Report.txt`` 要能被 ``report`` 搜到，与内核那条
  ``LIKE`` 检索一致）；
* **不重叠、按先后排**：同一段文字里出现多次就标多次，但已经标进去的字符
  不重复标（``aaa`` 里搜 ``aa``，标的是前两个字符，不是三个）。
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPen

from .style import MATCH_FILL, MATCH_INK

__all__ = ["match_spans", "paint_matched_text", "paint_spans",
           "paint_wrapped_text"]


def match_spans(text: str, needle: str) -> list[tuple[int, int]]:
    """``text`` 里所有命中 ``needle`` 的区间 ``[(起, 止), …]``（止不含）。

    ``needle`` 里**用空格分开的多个词都算关键词**（与内核那条检索同一套拆词规则，
    命中的区间会**合并**（重叠或相接的并成一段）并按先后排序 ——
    两个关键词挨在一起时画成一块，而不是互相盖一半。

    空关键词、没有命中都返回空列表 —— 调用方据此"别多画东西"。
    """
    haystack = str(text or "")
    from ..core.library import split_keywords

    words = split_keywords(needle)
    if not haystack or not words:
        return []
    lowered = haystack.casefold()
    found: list[tuple[int, int]] = []
    for word in words:
        target = word.casefold()
        start = lowered.find(target)
        while start >= 0:
            end = start + len(target)
            # 直接跳到这一段末尾再往后找：这样"aaa"里搜"aa"只会标出前两个字符，
            # 不会标出重叠的第二份（重叠的高亮块看着像画歪了）
            found.append((start, end))
            start = lowered.find(target, end)
    if not found:
        return []
    found.sort()
    merged: list[tuple[int, int]] = [found[0]]
    for start, end in found[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:               # 重叠或紧挨着：并成一段
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def paint_spans(painter, shown: str, spans: list[tuple[int, int]],
                left: float, baseline: float, height: float,
                metrics) -> None:
    """把 ``shown`` 里 ``spans`` 那几段重画一遍（底色 + 深色字）。

    :param left: 文字左边缘（屏幕/场景坐标，与 painter 当前变换一致）
    :param baseline: 文字基线
    :param height: 高亮块的高度（一般就是字高）
    """
    if not spans:
        return
    painter.save()
    for start, end in spans:
        x = left + metrics.horizontalAdvance(shown[:start])
        width = metrics.horizontalAdvance(shown[start:end])
        painter.fillRect(QRectF(x, baseline - metrics.ascent(),
                                width, max(height, metrics.height())),
                         QBrush(QColor(MATCH_FILL)))
        painter.setPen(QPen(QColor(MATCH_INK)))
        painter.drawText(QPointF(x, baseline), shown[start:end])
    painter.restore()


def paint_wrapped_text(painter, rect: QRectF, text: str, needle: str, *,
                       line_h: float) -> float:
    """把 ``text`` 折行画进 ``rect``（用 rect 的宽度），命中处加高亮。

    白板卡片的标题从"省略号截断"改成**折行写全**，卡片自己长高（高度由
    ``ProjectDocumentItem.measure`` 按同一个折行结果算出来）。

    :returns: 真正用掉的高度（调用方据此把下面的几行往下排）
    """
    from .widgets import wrap_lines

    metrics = painter.fontMetrics()
    lines = wrap_lines(text, metrics, rect.width())
    for index, line in enumerate(lines):
        top = rect.top() + index * line_h
        spans = match_spans(line, needle)
        painter.drawText(QPointF(rect.left(), top + metrics.ascent()), line)
        if spans:
            paint_spans(painter, line, spans, rect.left(),
                        top + metrics.ascent(), metrics.height(), metrics)
    return len(lines) * line_h


def paint_matched_text(painter, rect: QRectF, text: str, needle: str, *,
                       elide: bool = True) -> bool:
    """用 painter **当前的字体和颜色**在 ``rect`` 里画 ``text``，命中处加高亮。

    :returns: 真的画了（有命中）返回 True；没命中返回 False，
        时，画面与以前**一个像素都不差**。

    只支持**左对齐 + 垂直居中**（界面里所有会被高亮的文字都是这个排法：
    卡片标题、文件名、分组名）；别的对齐方式一律返回 False 交给调用方 ——
    宁可不高亮，也不能把文字画歪。
    """
    spans = match_spans(text, needle)
    if not spans:
        return False
    metrics = painter.fontMetrics()
    shown = str(text)
    if elide:
        shown = metrics.elidedText(shown, Qt.TextElideMode.ElideRight,
                                   int(rect.width()))
        if shown != text:
            # 被省略号截短了：字符位置和原串对不上，重新按**看得见的那一段**找命中
            spans = match_spans(shown, needle)
            if not spans:
                return False
    baseline = rect.top() + (rect.height() + metrics.ascent()
                             - metrics.descent()) / 2.0
    painter.drawText(QPointF(rect.left(), baseline), shown)
    paint_spans(painter, shown, spans, rect.left(), baseline,
                metrics.height(), metrics)
    return True

"""几个通用小控件（跟具体业务无关）。

目前只有一个：**宽度不随文字变化的标签**。

为什么需要它
------------
Qt 的 ``QLabel`` 把"文字有多宽"当成 sizeHint 与 minimumSizeHint —— 这在固定布局里
没问题，但放在**会被人拖着调宽窄的侧栏/中栏**里就是灾难：一行动态文字（事件副标题、
"库（13 个项目 / 230 个事件枝）· 项目名"）一长，布局的最小宽度就变大，QSplitter 只好

``ElidedLabel`` 的做法：sizeHint/minimumSizeHint 固定成很小的值（宽度完全由父布局决定），
文字超出宽度时自己省略，完整内容放 tooltip。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

from .style import SELECTION_COLOR, SELECTION_GLOW

__all__ = ["ElidedLabel", "MIN_HINT_WIDTH", "paint_selection_ring",
           "SELECTION_RING_PAD", "crisp_pixmap"]

#: 选中环会画到图元外面多少：环本身偏 3px、柔光笔宽 6px（一半在外）= 共 6px。
#: **每个画选中环的图元，boundingRect 都要留够这个余量** ——
SELECTION_RING_PAD = 8.0


#: 按"设备缩放"缓存好的图：``{(名字, 逻辑尺寸, 缩放档): QPixmap}``
_CRISP_CACHE: dict[tuple, "QPixmap"] = {}
#: 缓存上限（画布缩放来回变也不至于越攒越多）
_CRISP_LIMIT = 64


def crisp_pixmap(factory, logical_size: float, painter: QPainter,
                 name: str = "") -> "QPixmap":
    """按**当前缩放**把一个小图标画清楚（图标模糊的解药）。

    画布上的图标是在图元的 ``paint()`` 里用 ``drawPixmap`` 画的：如果按 15 像素做好
    这里按 ``painter`` 的世界变换算出真实需要多少像素，重新生成一张对应大小的图，
    并设好 devicePixelRatio，让它按逻辑尺寸绘制 —— 任何缩放下都清晰。

    :param factory: ``fn(size_px) -> QPixmap``（生成器，通常是 style 里的某个 *_pixmap）
    :param logical_size: 逻辑尺寸（场景单位）
    """
    scale = abs(painter.worldTransform().m11()) or 1.0
    bucket = max(1, min(int(round(scale * 4)) / 4.0, 4.0))     # 0.25 一档，封顶 4×
    key = (name or getattr(factory, "__name__", "glyph"), round(logical_size, 1), bucket)
    cached = _CRISP_CACHE.get(key)
    if cached is not None:
        return cached
    pixels = max(int(round(logical_size * bucket)), 1)
    pixmap = factory(pixels)
    pixmap.setDevicePixelRatio(bucket)
    if len(_CRISP_CACHE) >= _CRISP_LIMIT:
        _CRISP_CACHE.clear()
    _CRISP_CACHE[key] = pixmap
    return pixmap


def paint_selection_ring(painter: QPainter, rect: QRectF,
                         radius: float = 12.0) -> None:
    """"选中"那一圈：**粗实心蓝环 + 外面一圈柔光**。

    白板上的卡片/说明框/分组框用过之后用户认可了，事件枝页的方框也用同一圈 ——
    同一个应用里"选中"长得一样，才不会有"这块风格不搭"的感觉。
    """
    painter.setBrush(Qt.BrushStyle.NoBrush)
    glow = QPen(QColor(SELECTION_GLOW))
    glow.setWidthF(6.0)
    painter.setPen(glow)
    painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), radius + 3, radius + 3)
    ring = QPen(QColor(SELECTION_COLOR))
    ring.setWidthF(3.0)
    painter.setPen(ring)
    painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), radius + 3, radius + 3)
    painter.setBrush(Qt.BrushStyle.NoBrush)


#: 这种标签对外宣称的最小宽度（真的就是"能压缩到这个程度"）
MIN_HINT_WIDTH = 10


class ElidedLabel(QLabel):
    """文字太长自己省略、**绝不撑宽父容器**的标签。"""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    # ---------------------------------------------------------------- 内容

    def setText(self, text: str) -> None:  # noqa: N802
        self._full_text = text or ""
        self._apply_elide()
        self.setToolTip(self._full_text)

    def text(self) -> str:  # noqa: N802
        """**语义上的文字**（完整的）。

        ``QLabel.text()`` 返回的是"现在画在屏幕上的那段"，被省略了就只剩一截；
        但调用方（状态判断、测试）想知道的显然是"这行字本来是什么"，
        所以这里返回完整文字；真正画出来的仍然按宽度省略。
        """
        return self._full_text

    def fullText(self) -> str:  # noqa: N802
        """兼容别名。"""
        return self._full_text

    def _apply_elide(self) -> None:
        width = self.width()
        if width <= 1:
            # 还没被布局过（宽度未知）：先原样放着，等 resizeEvent 再省略 ——
            # 否则刚构造出来的那一下会被算成空字符串，界面上一片空白
            super().setText(self._full_text)
            return
        super().setText(
            self.fontMetrics().elidedText(self._full_text,
                                          Qt.TextElideMode.ElideRight, width)
        )

    # ---------------------------------------------------------------- 几何

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        self._apply_elide()

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(MIN_HINT_WIDTH, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(MIN_HINT_WIDTH, super().minimumSizeHint().height())

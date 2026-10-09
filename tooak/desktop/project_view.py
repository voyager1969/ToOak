"""项目树：事件为节点，用户可以拖动摆放、并画线表示项目路径。

与事件枝页的分工
--------------
* **项目树**（本文件）：一个项目的全貌 —— 事件枝卡片摆在哪里由用户决定，
  连线表示**依赖**（源依赖目标）；双击卡片进入事件枝页。
* **事件枝页**（``event_view.py``）：单个事件枝里的各环节方框与文件卡片。

两种布局
--------
* **手工布局**（``manual``）：只要用户拖动过任意一个节点，就按存库的坐标摆放 ——
  这是用户自己画的图，不能被自动排版冲掉。位置存在 ``graph_positions`` 表。
* **自动布局**（``mindmap`` / ``layered``）：还没摆过时的默认排法。

连线
----
* 卡片左右各有一个小圆点，都能拖出线，方向由**你从哪边拖**决定：
  从**右**端口拖出去 = 别人依赖我，从**左**端口拖出去 = 我依赖别人；
* 线画成一截枝叶，叶尖朝着**被依赖**的那张卡片；被依赖的卡片出了新稿，
  依赖它的卡片上会挂一个告警标志；
* 走线两种：**枝叶弧线**（默认）和**直角折线**（「布局 ▾ → 连线走直角」；
  拐弯处带小圆角，并且会绕开挡在中间的卡片 —— 算法在 ``routing.py``）；
  两种都能拖线中间的小圆点调形；
* 右键一条连线 → 删除它；
* 拖动卡片本身 = 移动位置（不会误建连线）。

排列
----
* 框选 / Ctrl 点选几个事件枝后，右键「排成网格…」（或右键分组框排组里的事件），
  「布局 ▾」里也有 —— 位置由 ``grid_layout()`` 算，写库与撤销复用拖动那条路。

这两件事不冲突：拖本体是移动，拖端口是连线。

拖文件进来
----------
* 拖到**某张事件枝卡片**上 = 把文件加进那个事件枝的当前那一稿，**先问放进哪个环节**；
* 拖到**空白处**不收（只给一句指路的话）。以前空白处会放一张"素材卡片"、
  "要拖就拖到白板的节点上，然后再确定要放在哪个类型下"。
"""

from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSizeF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPainterPathStroker,
    QPen,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QMenu,
    QStyleOptionGraphicsItem,
    QWidget,
)

from ..core.projectgraph import (
    CARD_H,
    CARD_W,
    DEFAULT_GROUP_H,
    DEFAULT_GROUP_W,
    DEPENDENCY_KIND,
    BoardCard,
    BoardGroup,
    ProjectGraph,
    ProjectGraphNode,
)
from . import zoom
from .canvas_toolbar import NEW_ITEM_MIME
from .routing import (ORTH_RADIUS, ORTH_STUB, hits, rounded_polyline,
                      route_orthogonal)
from .widgets import SELECTION_RING_PAD, paint_selection_ring
from ..core.library import DOC_STATE_LABELS
from .style import (ACCENT, ATTENTION_COLOR, ATTENTION_TEXT, BORDER, MUTED,
                    SELECTION_COLOR, SELECTION_GLOW, TEXT, branch_pixmap,
                    branch_plus_icon, export_icon, folder_icon, grid_icon,
                    group_icon, leaf_icon, leaf_pixmap, logo_pixmap, note_icon,
                    open_icon, pencil_icon, prune_icon, state_colors,
                    state_glyph, state_icon)

__all__ = ["ProjectBoardView", "ProjectDocumentItem", "BoardCardItem",
           "BoardGroupItem", "grid_shape", "rounded_polyline",
           "EDGE_ROUTE_CURVE", "EDGE_ROUTE_ORTH"]


def _elide(text: str, metrics: QFontMetrics, width: float) -> str:
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(width))


#: 连线端口的半径
PORT_R = 6.5
#: 拖动节点时的对齐吸附：离别的卡片这些像素以内就"贴"上去（场景像素）
SNAP_TOLERANCE = 6.0

#: 白板四周最少留多少空白（场景像素）—— 底部/右侧要能接着摆东西
CANVAS_MARGIN = 240.0
#: 平移/缩放快贴到边时，一次性往那一侧再铺多少（场景像素）。
#: 只往"确实贴到的那一侧"长，别的边不动（滚动条不至于越缩越小）。
CANVAS_GROW = 320.0

#: 曲度手柄的半径（场景像素）
HANDLE_R = 5.5
#: 曲度的上下限（拱得太夸张就不好看了）。**两个方向都能拱**：
#: 正负号决定往弦的哪一侧弯，拖起来才顺手
MAX_CURVE = 400.0
#: 拖到很小的幅度就当"直着画"（0 是"自动"这个特殊值）
MIN_CURVE = 2.0

#: 依赖线颜色（版图上的线只有一种含义：依赖）
DEP_EDGE_COLOR = "#0e6b3d"
#: 卡片上"依赖：…"那一行的颜色
DEP_TEXT = "#3d6b52"

EDGE_ROUTE_CURVE = "curve"
EDGE_ROUTE_ORTH = "orth"
EDGE_ROUTES = (EDGE_ROUTE_CURVE, EDGE_ROUTE_ORTH)
#: 网格排列的默认间距（一批事件排成几行几列时，卡片之间留多少）
GRID_GAP = 34.0


def grid_shape(count: int, cols: int = 0, rows: int = 0) -> tuple[int, int]:
    """算出"几行几列"：给一个另一个按数量推，都不给就摆成接近正方形。"""
    count = max(int(count), 1)
    cols, rows = max(int(cols or 0), 0), max(int(rows or 0), 0)
    if cols and rows:
        return min(cols, count), min(rows, count)
    if cols:
        return cols, max(1, math.ceil(count / cols))
    if rows:
        return max(1, math.ceil(count / rows)), rows
    cols = max(1, math.ceil(math.sqrt(count)))
    return cols, max(1, math.ceil(count / cols))


def _path_segments(path: QPainterPath) -> list[QPainterPath]:
    """把一条路径拆成"每段一个子路径"（直线一段、每个圆角一段）。

    为什么需要：藤蔓要**渐细**，所以每段得用自己的线宽画；而用百分比采样去近似，
    圆角会被弦切成尖角。拆成真正的线段/弧段，既保住了粗细，又保住了圆角。
    """
    segments: list[QPainterPath] = []
    previous = QPointF()
    index = 0
    count = path.elementCount()
    while index < count:
        element = path.elementAt(index)
        kind = element.type
        if kind == QPainterPath.ElementType.MoveToElement:
            previous = QPointF(element.x, element.y)
            index += 1
        elif kind == QPainterPath.ElementType.LineToElement:
            end = QPointF(element.x, element.y)
            segment = QPainterPath(previous)
            segment.lineTo(end)
            segments.append(segment)
            previous = end
            index += 1
        elif kind == QPainterPath.ElementType.CurveToElement:
            control = QPointF(element.x, element.y)
            index += 1
            data: list[QPointF] = []
            while (index < count
                   and path.elementAt(index).type
                   == QPainterPath.ElementType.CurveToDataElement):
                data.append(QPointF(path.elementAt(index).x, path.elementAt(index).y))
                index += 1
            segment = QPainterPath(previous)
            if len(data) == 1:                 # 二次曲线 = 圆角
                segment.quadTo(control, data[0])
            elif len(data) >= 2:               # 三次曲线 = 弧线那条连线
                segment.cubicTo(control, data[0], data[1])
            if data:
                segments.append(segment)
                previous = data[-1]
        else:
            index += 1
    return segments


class ProjectDocumentItem(QGraphicsItem):
    """版图上的一张事件枝卡片：可拖动、可从右侧端口拖出连线。"""

    def __init__(self, node: ProjectGraphNode, parent: QGraphicsItem | None = None):
        super().__init__(parent)
        self.node = node
        self._hover = False
        #: ``None`` / ``"out"``（右端口，我依赖别人）/ ``"in"``（左端口，别人依赖我）
        self._connect_side: str | None = None
        #: 这一拖连线被 Esc 取消了（松手时不再连出一条依赖，见 _cancel_interactions）
        self._connect_cancelled = False
        #: 是不是正在被鼠标拖着（松手、丢焦点都要变回 False）
        self._dragging = False
        #: 多选后一起拖动：跟着自己走的那些卡片，以及它们按下时的位置
        self._group_items: list[ProjectDocumentItem] = []
        self._group_origins: dict[ProjectDocumentItem, QPointF] = {}
        self._group_anchor: QPointF | None = None
        self._moving_group = False
        #: 按下时卡片**自己的位置**（不是鼠标位置）——
        #: 卡片是跟着鼠标走的，鼠标在卡片坐标系里的位置从头到尾不变，
        #: 所以"动了没有"只能拿卡片自己的坐标比（这个 bug 让"拖了没保存"）。
        self._press_origin: QPointF | None = None

        self.setPos(node.x, node.y)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.setToolTip(self._build_tooltip())

    # ------------------------------------------------------------- 几何

    def boundingRect(self) -> QRectF:  # noqa: N802
        # 左右端口圆点画在卡片边框外，必须一起算进来 ——
        # 少了这块，拖动卡片时旧位置上会留下两个小圆点的"残影"
        # （Qt 只重画 boundingRect 范围内的区域）。留够圆点 + 描边的余量。
        pad = PORT_R + 4
        return QRectF(-pad, -6, CARD_W + pad * 2, CARD_H + 12)

    def card_rect(self) -> QRectF:
        """卡片**看得见**的那块矩形（场景坐标）。

        ``boundingRect()`` 为了两个端口圆点比卡片大了一圈（左右各 10.5、
        上下各 6）。算空位 / 排网格要用看得见的那块，否则卡与卡之间会
        """
        return QRectF(self.pos(), QSizeF(CARD_W, CARD_H))

    def out_port_rect(self) -> QRectF:
        """"""
        return QRectF(CARD_W - PORT_R, CARD_H / 2 - PORT_R, PORT_R * 2, PORT_R * 2)

    def in_port_rect(self) -> QRectF:
        """"""
        return QRectF(-PORT_R, CARD_H / 2 - PORT_R, PORT_R * 2, PORT_R * 2)

    def port_rect(self) -> QRectF:
        """兼容旧名：右侧（输出）端口。"""
        return self.out_port_rect()

    def port_scene_pos(self) -> QPointF:
        return self.mapToScene(QPointF(CARD_W - 2, CARD_H / 2))

    def input_scene_pos(self) -> QPointF:
        return self.mapToScene(QPointF(0, CARD_H / 2))

    # ------------------------------------------------------------- 绘制

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget: QWidget = None) -> None:
        node = self.node
        border_color, fill_color, text_color = state_colors(node.state)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0, 0, CARD_W, CARD_H)

        painter.setBrush(QBrush(QColor("#ffffff")))
        pen = QPen(QColor(border_color))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 9, 9)

        # 卡片自己的边框归**状态**用（绿/木/黄），选中另外用一圈饱和蓝，互不干扰
        if self.isSelected():
            paint_selection_ring(painter, rect, 9.0)

        # 左侧状态条
        strip = QPainterPath()
        strip.addRoundedRect(QRectF(0, 0, 5, CARD_H), 2.5, 2.5)
        painter.setPen(Qt.PenStyle.NoPen)
        # 色带就用状态色：绿叶=进行中、棕黄=已完成、秋黄=搁置（不再硬编码）
        painter.setBrush(QBrush(QColor(text_color)))
        painter.drawPath(strip)

        # 标题
        title_font = QFont(painter.font())
        title_font.setPointSizeF(9.8)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(QPen(QColor(TEXT)))
        metrics = QFontMetrics(title_font)
        # 有告警时右边要腾出感叹号的位置，标题就窄一点
        title_w = CARD_W - (64 if node.needs_attention else 44)
        painter.drawText(
            QRectF(13, 6, title_w, 18),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            _elide(("✓ " if node.done else "") + node.title, metrics, title_w - 2),
        )
        painter.drawText(
            QRectF(CARD_W - 42, 6, 30, 18),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            f"{node.version_count}版",
        )

        # 环节概览
        meta_font = QFont(painter.font())
        meta_font.setPointSizeF(8.2)
        meta_font.setBold(False)
        painter.setFont(meta_font)
        metrics_meta = QFontMetrics(meta_font)
        painter.setPen(QPen(QColor("#4a5666")))
        painter.drawText(
            QRectF(13, 25, CARD_W - 22, 15),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            _elide(node.stage_summary or "（还没有文件）", metrics_meta, CARD_W - 26),
        )

        # 状态（前面带一个小图案：叶子/橡果/黄叶）
        glyph = state_glyph(node.state, 13)
        if not glyph.isNull():
            painter.drawPixmap(13, 41, glyph)
        painter.setPen(QPen(QColor(text_color)))
        painter.drawText(
            QRectF(30, 41, CARD_W - 40, 15),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            _elide(node.state_label + (f" · {node.people}" if node.people else ""),
                   metrics_meta, CARD_W - 26),
        )

        change = node.change_summary
        if change:
            painter.setPen(QPen(QColor("#5a6675")))
            painter.drawText(
                QRectF(13, 57, CARD_W - 22, 15),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                _elide(change, metrics_meta, CARD_W - 26),
            )

        # 依赖了谁：直接标在卡片上（不用顺着线去数）；
        # 其中"已经出了新稿、我还没跟"的那些用琥珀色 + ⚠ 标出来。
        dependency = node.dependency_text
        if dependency:
            stale = node.needs_attention
            painter.setPen(QPen(QColor(ATTENTION_TEXT if stale else DEP_TEXT)))
            painter.drawText(
                QRectF(13, 73, CARD_W - 22, 15),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                _elide(dependency, metrics_meta, CARD_W - 26),
            )

        # 告警标志：右上角一个琥珀色感叹号三角
        if node.needs_attention:
            self._draw_attention_badge(painter)

        # 两个连线端口（左右各一个，都能拖出线）：
        #   右侧 = 别人依赖我（我的下游在这一侧）；左侧 = 我依赖别人（我依赖的在这一侧）
        # 悬停/连线中时端口实心放大，一眼看得出可以抓。
        for side, rect in (("out", self.out_port_rect()), ("in", self.in_port_rect())):
            active = self._connect_side == side
            hot = self._hover or active
            painter.setBrush(QBrush(QColor(ACCENT if hot else "#ffffff")))
            port_pen = QPen(QColor(ACCENT if hot else BORDER))
            port_pen.setWidthF(1.8 if hot else 1.4)
            painter.setPen(port_pen)
            painter.drawEllipse(rect)

    def _draw_attention_badge(self, painter: QPainter) -> None:
        """右上角的琥珀色感叹号：我依赖的事件更新了，该跟一跟了。"""
        size = 15.0
        x = CARD_W - 60
        y = 7.0
        triangle = QPainterPath()
        triangle.moveTo(x + size / 2, y)
        triangle.lineTo(x + size, y + size - 1)
        triangle.lineTo(x, y + size - 1)
        triangle.closeSubpath()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(ATTENTION_COLOR)))
        painter.drawPath(triangle)
        font = QFont(painter.font())
        font.setPointSizeF(7.6)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(
            QRectF(x, y + 2, size, size - 3),
            int(Qt.AlignmentFlag.AlignCenter),
            "!",
        )
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _build_tooltip(self) -> str:
        node = self.node
        lines = [node.title, f"{node.version_count} 个版本 · {node.state_label}"]
        if node.stages:
            lines.append(f"环节：{node.stage_summary}")
        if node.depends_on:
            lines.append("依赖：" + "、".join(node.depends_on))
        if node.depended_by:
            lines.append("被依赖：" + "、".join(node.depended_by))
        if node.needs_attention:
            lines += ["", "⚠ " + node.attention_text]
        lines += [
            "",
            "拖动卡片 = 摆位置（会自动保存）",
            "右边的圆点拖出去 = 别人依赖我；左边的圆点拖出去 = 我依赖别人",
            "双击 = 进入事件枝页",
        ]
        return "\n".join(lines)

    @property
    def _connecting(self) -> bool:
        return self._connect_side is not None

    # ------------------------------------------------------------- 交互

    def hoverEnterEvent(self, event):  # noqa: N802
        self._hover = True
        self.setZValue(20)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):  # noqa: N802
        self._hover = False
        self.setZValue(10)
        self.update()
        super().hoverLeaveEvent(event)

    #: 端口命中容差的上限（场景单位）。**一定要封顶**：缩放很小时
    #: ``9/scale`` 会算成几十上百像素，整张卡片都被算成端口，于是"按哪儿都在拉线"、
    #: 卡片根本拖不动（缩放 0.09 时实测容差 97.9，卡片只有 210 宽 —— 真 bug）
    MAX_HIT_TOLERANCE = 16.0

    def _hit_tolerance(self) -> float:
        scale = 1.0
        if self.scene() is not None and self.scene().views():
            scale = self.scene().views()[0].transform().m11() or 1.0
        return max(3.0, min(9.0 / scale, self.MAX_HIT_TOLERANCE))

    def port_at(self, pos: QPointF) -> str | None:
        """点在哪个端口上：``"out"``（右）/ ``"in"``（左）/ ``None``。"""
        tolerance = self._hit_tolerance()
        for side, rect in (("out", self.out_port_rect()), ("in", self.in_port_rect())):
            if rect.adjusted(-tolerance, -tolerance,
                             tolerance, tolerance).contains(pos):
                return side
        return None

    def port_hit(self, pos: QPointF) -> bool:
        """点是不是落在任意一个端口上（**按屏幕像素**判定，缩放多少都好按）。"""
        return self.port_at(pos) is not None

    def hoverMoveEvent(self, event):  # noqa: N802
        if self.port_hit(event.pos()):
            self.setCursor(Qt.CursorShape.CrossCursor)
        elif getattr(self, "_space_pan_key", False):
            self.setCursor(Qt.CursorShape.OpenHandCursor)   # 按住空格 = 要平移
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        # 点在端口上 -> 开始连线（不进入拖动）
        side = self.port_at(event.pos())
        if side is not None:
            self._connect_side = side
            # 新的这一拖重新开始：清掉上一次"被 Esc 取消"的标记，
            # 否则这一次的松手会被当成取消掉的那一拖，连不出线
            self._connect_cancelled = False
            self.setCursor(Qt.CursorShape.CrossCursor)
            self.update()
            event.accept()
            return
        self._press_origin = self.pos()
        # 已经多选了一批卡片：拖其中一张就整组一起挪
        self._clear_group()
        scene = self.scene()
        if scene is not None and self.isSelected():
            selected = [other for other in scene.selectedItems()
                        if isinstance(other, ProjectDocumentItem) and other is not self]
            if selected:
                self._group_items = selected
                self._group_origins = {other: other.pos() for other in selected}
                self._group_anchor = self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        # 从端口拖线时**别把卡片也拖走** —— QGraphicsItem 默认的 ItemIsMovable
        # 行为会跟着鼠标搬卡片，结果"拉一条线"把节点也挪了位置。
        if self._connecting:
            event.accept()
            return
        if self._press_origin is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self._dragging = True
        super().mouseMoveEvent(event)

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            # 选中的卡片提到最前（Obsidian 也是这样：选中的盖住别的）
            self.setZValue(15 if value else 10)
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionChange:
            # 只有**鼠标真的在拖**的时候才吸附对齐：
            # 程序化摆位（重建版图、微调落库）不能被"顺"走，否则存的坐标会变
            if self._dragging:
                scene = self.scene()
                if scene is not None and hasattr(scene, "snap_position"):
                    value = scene.snap_position(self, value)
        elif change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._follow_group()
            scene = self.scene()
            if scene is not None and hasattr(scene, "refresh_edges_for"):
                scene.refresh_edges_for(self)
        return super().itemChange(change, value)

    def _follow_group(self) -> None:
        """多选一起拖：被抓住的那张卡片动多少，其他选中的也动多少。"""
        if not self._group_items or self._moving_group or self._group_anchor is None:
            return
        self._moving_group = True
        try:
            delta = self.pos() - self._group_anchor
            for other in self._group_items:
                origin = self._group_origins.get(other)
                if origin is not None:
                    other.setPos(origin + delta)
        finally:
            self._moving_group = False

    def _clear_group(self) -> None:
        self._group_items = []
        self._group_origins = {}
        self._group_anchor = None

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._connecting:
            side = self._connect_side or "out"
            self._connect_side = None
            self.update()
            scene = self.scene()
            if scene is not None and hasattr(scene, "finish_connection"):
                scene.finish_connection(self, event.scenePos(), side)
            event.accept()
            return
        self._dragging = False
        moved = (
            self._press_origin is not None
            and (self.pos() - self._press_origin).manhattanLength() > 1.0
        )
        super().mouseReleaseEvent(event)
        if moved:
            scene = self.scene()
            if scene is not None and hasattr(scene, "notify_moved"):
                scene.notify_moved(self)

        self._press_origin = None
        self._clear_group()


class _EdgeItem(QGraphicsPathItem):
    """一条依赖线：画成一截**枝叶**，叶子朝向标明依赖方向。

    方向约定：``source`` 依赖 ``target``（"我依赖它"，source 是依赖方）。

    线从**被依赖**的那张卡片（``target``）长出来，叶尖落在**依赖它**的
    那张卡片（``source``）上 —— 也就是"谁依赖别人，叶子就长在谁身上"，
    告警标志（被依赖的更新了、这张卡还没跟）也挂在这张卡上，两者对得上。

    两端的端口挑**面对面**的那一侧（被依赖的在左边时，就是被依赖方的右端口
    接到依赖方的左端口）。
    """

    #: 藤蔓颜色（灰绿，不抢卡片）
    VINE = "#7fa98c"
    #: 叶子填充 / 描边
    LEAF = "#4f9e69"
    LEAF_EDGE = "#2f7d4a"

    def __init__(self, source: ProjectDocumentItem, target: ProjectDocumentItem,
                 kind: str, label: str = "", curve: float = 0.0,
                 route: str = EDGE_ROUTE_CURVE):
        super().__init__()
        self._bending = False
        self.source = source          # 依赖方
        self.target = target          # 被依赖方
        self.kind = kind
        self.label = label
        #: 弧线模式下的拱高
        self.arc_curve = 0.0
        #: 直角模式下那道弯的左右偏移。**和拱高分开存**：库里只有一列
        #: ``document_links.curve``，两种走线却共用它，混在一起的话
        self.orth_curve = 0.0
        #: 怎么走线：``"curve"``（枝叶弧线）/ ``"orth"``（直角折线）
        self.route = EDGE_ROUTE_CURVE
        self.set_route(route)
        # 库里那一列 curve 存的是"**当前模式下**那个调形参数"（主窗口就是这么写的），
        # 所以这里也按当前模式落进对应的槽 —— 直角模式下重开项目，那道弯才回得来
        self.curve = float(curve or 0.0)
        self.setZValue(1)
        self.setAcceptHoverEvents(True)
        self._hover = False
        self.update_path()

    @property
    def curve(self):
        """当前走线方式下的那个"调形参数"（弧线是拱高、直角线是那道弯的偏移）。

        做成属性是为了让调用方（主窗口存库、测试、手柄）**只有一个入口**：
        库里只有一列 curve，而这一个值在不同模式下含义不同，各存各的才不会互相
        冲掉；读的时候按当前模式取一个就够（主窗口仍然往同一列写）。
        """
        return self.orth_curve if self.route == EDGE_ROUTE_ORTH else self.arc_curve

    @curve.setter
    def curve(self, value) -> None:
        if self.route == EDGE_ROUTE_ORTH:
            self.orth_curve = float(value or 0.0)
        else:
            self.arc_curve = float(value or 0.0)

    def set_route(self, route: str) -> None:
        """换走线方式（弧线 ↔ 直角折线）。

        **不动**任何调形参数：两种模式各存各的（见 ``curve``），
        """
        self.route = route if route in EDGE_ROUTES else EDGE_ROUTE_CURVE
        self.update_path()

    # ------------------------------------------------------------- 几何

    def chord(self) -> tuple[QPointF, QPointF, QPointF, float]:
        """连线两端的两个锚点 + 单位法向 + 弦长（法向是"拱起来"的方向）。"""
        start, end = self._endpoints()
        dx, dy = end.x() - start.x(), end.y() - start.y()
        length = math.hypot(dx, dy) or 1.0
        normal = QPointF(dy / length, -dx / length)   # 弦逆时针转 90°
        return start, end, normal, length

    def live_orth_points(self) -> list[QPointF]:
        """**拖拽途中**用的直角折点：只做"中间竖一道"的 S 形，不做避让搜索。

        为什么不搜：避让是在若干条候选里挑最合适的一条（"压到几张卡片 → 面积 →
        总长"打分），卡片一动，打分排序就可能换一条 —— 表现就是**拖的时候线在几个
        这条 S 形是端点的连续函数，拖多久都不跳；松手时再按"聪明走线"重算一次。
        """
        start, _start_side, end, _end_side = self._endpoints(True)
        mid = self.orth_base_x() + self.curve
        low, high = sorted((start.x(), end.x()))
        mid = max(low + ORTH_STUB * 0.5, min(high - ORTH_STUB * 0.5, mid))
        if abs(mid - start.x()) < 1.0 and abs(mid - end.x()) < 1.0:
            return [start, end]
        return [start, QPointF(mid, start.y()), QPointF(mid, end.y()), end]

    def orth_points(self) -> list[QPointF]:
        """直角折线的拐点（含两端），直角转弯就是在这里定的。

        "从哪儿出去、在哪儿拐弯"交给 ``routing.route_orthogonal()``：
        它会先试中间竖一道的 S 形，再试从通道里绕过去的走法，**尽量不压到
        """
        # ``_endpoints()`` 给的端口一侧**永远是** ("out", "in") 或 ("in", "out")：
        # 线从被依赖方长向依赖方，两端各用朝向对方的那一侧。
        # （这里原来还有一个"两道端口朝同一边"的分支，那个条件不可能成立 ——
        #   永远不会跑到的假代码，删掉。）
        start, start_side, end, end_side = self._endpoints(True)
        start_dir = 1 if start_side == "out" else -1
        # 进入目标端口的方向：从对面过来，所以 stub 点在端口外面（反方向）
        end_dir = 1 if end_side == "out" else -1
        return route_orthogonal(start, start_dir, end, end_dir,
                                obstacles=self._obstacles(), curve=self.curve)

    def _obstacles(self) -> list[QRectF]:
        """除两端卡片之外、走线要绕开的东西（事件枝卡片 + 白板上的卡片）。

        一律用 ``card_rect()``（**看得见**的那块矩形）：事件枝卡片的
        ``sceneBoundingRect()`` 为了端口圆点比卡片大一圈、白板卡片的
        ``boundingRect()`` 为了选中环也大一圈，拿它们当障碍会让走线绕得莫名其妙。
        """
        view = getattr(self.scene(), "view", None)
        if view is None:
            return []
        rects = [item.card_rect() for item in view._items.values()
                 if item is not self.source and item is not self.target]
        rects += [card.card_rect() for card in view._cards.values()]
        return rects

    def settled_orth_points(self) -> list[QPointF]:
        """**定稿**时的直角走线：优先避让，但"避让也没避干净"时不许乱跳。

        为什么加这层：避让是在候选里挑"压得最少"的那条。挤的地方挑出来的那条
        可能绕到几百像素外（实测有一条从 (1991,63) 跳到 (1237,821)，**而且绕完
        还是压着 3 张卡片**）—— 用户看到的就是"松手瞬间线飞了"。
        所以规则定成：**聪明走线干净（一张都不压）才采纳**；它自己也不干净时，
        保持拖拽时那条连续的 S 形（至少位置是对的、看得懂）。
        """
        smart = self.orth_points()
        obstacles = self._obstacles()
        if hits(smart, obstacles)[0] == 0:
            return smart                       # 干净：用避让后的走线
        return self.live_orth_points()         # 不干净：别为了它甩出去

    def update_path(self) -> None:
        if self.route == EDGE_ROUTE_ORTH:
            points = self.settled_orth_points()
            path = rounded_polyline(points, ORTH_RADIUS)
        else:
            start, end, normal, length = self.chord()
            # 曲度：用户拖过就用他拖的值（正负 = 往哪一侧拱）；
            # 没拖过时"反向摆放"自动绕一点，免得线压在卡片上
            bow = self.curve if self.curve else (40.0 if end.x() < start.x() else 0.0)
            offset = QPointF(normal.x() * bow, normal.y() * bow)
            path = QPainterPath(start)
            path.cubicTo(
                start + (end - start) * 0.35 + offset,
                start + (end - start) * 0.65 + offset,
                end,
            )
        self.setPath(path)
        self.setToolTip(self._build_tooltip())

    def handle_scene_pos(self) -> QPointF:
        """握手柄的那个圆点：弧线在弧顶、直角线在**中间那道竖线**上。

        为什么直角模式下不能再用 ``path.pointAtPercent(0.5)``：那是"沿路径
        长度取一半"，两段横线的长度一变中点就沿着折线乱跑；**同一行的两张卡**
        更极端 —— 折线退化成一条直线，中点恒等于几何中点，于是手柄怎么拖都不动，
        那个点"：中间那道竖线所在的位置。
        """
        if self.route == EDGE_ROUTE_ORTH:
            start, end = self._endpoints()
            return QPointF(self.orth_base_x() + self.orth_curve,
                           (start.y() + end.y()) / 2)
        path = self.path()
        return path.pointAtPercent(0.5) if not path.isEmpty() else QPointF()

    def orth_same_row(self) -> bool:
        """直角模式下两端在同一行：折线退化成一条直线，那道弯没地方可弯。

        这种线上不该露出、也不该响应手柄 —— 拖了不但看不见效果，
        还会往库里写一个没有意义的偏移（"手柄没反应但库变了"）。
        """
        if self.route != EDGE_ROUTE_ORTH:
            return False
        start, end = self._endpoints()
        return abs(start.y() - end.y()) < 1.0

    def handle_rect(self) -> QRectF:
        center = self.handle_scene_pos()
        return QRectF(center.x() - HANDLE_R, center.y() - HANDLE_R,
                      HANDLE_R * 2, HANDLE_R * 2)

    def hit_handle(self, scene_pos: QPointF) -> bool:
        """点是不是落在曲度手柄上（**按屏幕像素**判定，缩放多少都好点）。"""
        if self.orth_same_row():
            return False
        scale = 1.0
        if self.scene() is not None and self.scene().views():
            scale = self.scene().views()[0].transform().m11() or 1.0
        tolerance = max(4.0 / scale, 2.0)
        return self.handle_rect().adjusted(-tolerance, -tolerance,
                                           tolerance, tolerance).contains(scene_pos)

    def _endpoints(self, with_sides: bool = False):
        """从被依赖方（根）长向依赖方（叶尖），两端都用朝向对方的端口。

        ``with_sides=True`` 时额外返回两侧端口是哪一边（``"out"`` = 右端口、
        ``"in"`` = 左端口）—— 直角走线要知道线是从哪一侧出去的。
        """
        dependent = self.source.sceneBoundingRect().center().x()
        depended = self.target.sceneBoundingRect().center().x()
        if dependent > depended:
            # 依赖方在右边：从被依赖方的右侧长出，叶尖落在依赖方的左侧
            start = self.target.port_scene_pos()
            end = self.source.input_scene_pos()
            sides = ("out", "in")
        else:
            start = self.target.input_scene_pos()
            end = self.source.port_scene_pos()
            sides = ("in", "out")
        if with_sides:
            return start, sides[0], end, sides[1]
        return start, end

    def _build_tooltip(self) -> str:
        dep = self.source.node.title
        on = self.target.node.title
        base = f"{on}（被依赖）\n　　↓ 枝叶长向\n{dep}（依赖它）"
        hint = ("拖动线中间的小圆点 = 把这道弯挪左挪右" if self.route == EDGE_ROUTE_ORTH
                else "拖动线中间的小圆点 = 调这条线的弯度")
        if self.kind == DEPENDENCY_KIND:
            return base + f"\n\n{hint}\n右键 = 剪断这条依赖"
        return f"{dep} —（{self.label or self.kind}）— {on}\n\n右键 = 删除这条连线"

    # --------------------------------------------------- 直接拖：调曲度

    def mousePressEvent(self, event):  # noqa: N802
        if self.hit_handle(event.scenePos()):
            self._bending = True
            self.setSelected(True)
            self.setZValue(40)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            # 松手事件丢了（按了 Esc 取消、焦点被别的窗口抢走）时，鼠标一动
            # 就会走到这里：**没按着左键 = 这一拖已经结束了**，不能再改线形
            self._bending = False
            super().mouseMoveEvent(event)
            return
        if not self._bending:
            super().mouseMoveEvent(event)
            return
        if self.route == EDGE_ROUTE_ORTH:
            # 直角线：拖手柄 = 把那道竖直的弯左右挪（挪到哪儿就在哪儿拐弯）
            self.curve = max(-MAX_CURVE, min(MAX_CURVE,
                                             event.scenePos().x() - self.orth_base_x()))
            self.update_path()
            event.accept()
            return
        start, end, normal, _ = self.chord()
        middle = (start + end) / 2
        offset = event.scenePos() - middle
        # 手柄在 t=0.5 处正好偏移 0.75 × 拱高，所以除回去
        bow = (offset.x() * normal.x() + offset.y() * normal.y()) / 0.75
        if abs(bow) < MIN_CURVE:
            bow = 0.0
        self.curve = max(-MAX_CURVE, min(MAX_CURVE, bow))
        self.update_path()
        event.accept()

    def orth_base_x(self) -> float:
        """直角折线"没被拖过"时那道竖线在哪儿（拖手柄时以它为基准算偏移）。"""
        # 和 ``orth_points()`` 同一处说明：两道端口永远是朝对面的，
        # 所以那道竖线就是两端的中线（原来这里也有一段"端口朝同一边"的死分支）
        start, end = self._endpoints()
        return (start.x() + end.x()) / 2

    def mouseReleaseEvent(self, event):  # noqa: N802
        if not getattr(self, "_bending", False):
            super().mouseReleaseEvent(event)
            return
        self._bending = False
        self.setZValue(1)
        self.update()
        if self.scene() is not None and hasattr(self.scene(), "notify_edge_curve"):
            self.scene().notify_edge_curve(self)
        event.accept()

    # ------------------------------------------------------------- 绘制

    def paint(self, painter: QPainter, option, widget=None) -> None:
        path = self.path()
        if path.isEmpty():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        hot = self._hover
        vine = QColor("#1f6b45" if hot else self.VINE)
        leaf = QColor("#3f8f5c" if hot else self.LEAF)
        leaf_edge = QColor("#1f6b45" if hot else self.LEAF_EDGE)

        # 藤蔓：从依赖方到被依赖方逐渐变细（像枝条长出去）
        #
        # **按路径自己的每一段画**，不要按百分比采样成一堆短直线：
        # 采样画出来的其实是折线，圆角会被弦切掉 —— 圆角半径 16px、而 26 个采样点
        # 之间隔着二三十像素时，拐弯处看起来还是**尖角**
        # 每段一个子路径，`quadTo` 的圆角就是真的弧；线宽沿路径插值，粗细的观感不变。
        painter.setBrush(Qt.BrushStyle.NoBrush)
        base_w = 3.0 if hot else 2.4
        total = path.length() or 1.0
        walked = 0.0
        for segment in _path_segments(path):
            length = segment.length()
            t_mid = min((walked + length / 2) / total, 1.0)
            walked += length
            pen = QPen(vine)
            pen.setWidthF(max(base_w * (1.0 - 0.55 * t_mid), 1.0))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawPath(segment)

        # 叶片：沿藤蔓交错长出（叶尖都朝着被依赖的那一方）
        for index, t in enumerate((0.22, 0.42, 0.62)):
            length = 11.0 + index * 1.6
            self._draw_leaf(painter, path, t,
                            length, length * 0.36,
                            tilt=38.0 if index % 2 == 0 else -38.0,
                            fill=leaf, edge=leaf_edge)

        # 叶尖：压在终点上的那片大叶子 = 箭头（"我依赖你"）
        self._draw_leaf(painter, path, 0.985, 17.0, 6.6, tilt=0.0,
                        fill=leaf, edge=leaf_edge)

        # 曲度手柄：悬停或选中时露出来，**直接拖它就能把线拱起来**
        # （同一行的直角线上不露手柄：那道弯没地方可弯，见 orth_same_row）
        if ((self._hover or self.isSelected() or getattr(self, "_bending", False))
                and not self.orth_same_row()):
            # 画在"能抓住的那个点"上，和 hit_handle 的口径必须一致
            center = self.handle_scene_pos()
            painter.setBrush(QBrush(QColor("#ffffff")))
            pen = QPen(QColor("#1f6b45"))
            pen.setWidthF(1.6)
            painter.setPen(pen)
            painter.drawEllipse(center, HANDLE_R, HANDLE_R)
            painter.setBrush(QBrush(QColor("#1f6b45")))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(center, HANDLE_R * 0.42, HANDLE_R * 0.42)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _draw_leaf(self, painter: QPainter, path: QPainterPath, t: float,
                   length: float, width: float, tilt: float,
                   fill: QColor, edge: QColor) -> None:
        angle = -path.angleAtPercent(t)
        point = path.pointAtPercent(t)
        painter.save()
        painter.translate(point)
        painter.rotate(angle + tilt)
        leaf = QPainterPath(QPointF(0, 0))
        leaf.cubicTo(QPointF(length * 0.30, -width),
                     QPointF(length * 0.78, -width * 0.82),
                     QPointF(length, 0))
        leaf.cubicTo(QPointF(length * 0.78, width * 0.82),
                     QPointF(length * 0.30, width),
                     QPointF(0, 0))
        painter.setPen(QPen(edge, 1.0))
        painter.setBrush(QBrush(fill))
        painter.drawPath(leaf)
        # 叶脉：一根细线，顺着叶尖方向，方向感更明确
        painter.setPen(QPen(edge, 0.8))
        painter.drawLine(QPointF(length * 0.16, 0), QPointF(length * 0.86, 0))
        painter.restore()

    def hoverEnterEvent(self, event):  # noqa: N802
        self._hover = True
        self.setZValue(30)
        self.update()
        super().hoverEnterEvent(event)

    def hoverMoveEvent(self, event):  # noqa: N802
        if self.hit_handle(event.scenePos()):
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def hoverLeaveEvent(self, event):  # noqa: N802
        self._hover = False
        self.setZValue(1)
        self.update()
        super().hoverLeaveEvent(event)

    def shape(self) -> QPainterPath:  # noqa: N802
        # 加宽命中区域，细线 + 叶子才好点中
        stroker = QPainterPathStroker()
        stroker.setWidth(18)
        return stroker.createStroke(self.path())



class BoardCardItem(QGraphicsItem):
    """白板上的一张卡片：**说明框 / 文件素材 / 网页链接**。

    三者只是"内容从哪来"不同（``card.kind``），位置、大小、编辑、删除这一套
    行为完全一样，所以一个图元类就够：

    * ``note``：自由文字，双击就地编辑（用 ``QGraphicsTextItem`` 子项当编辑器）；
    * ``file``：指向磁盘上一个文件（**只记路径，绝不复制/移动**），双击用系统
      默认程序打开；文件不在了就画成灰的；
    * ``link``：指向一个网址，双击在浏览器打开。

    **素材卡片（file/link）左上角画一片绿叶**（跟"进行中"一个形状，意思是
    "还在长、还没归属"），和事件枝卡片的状态语言（叶子/橡果/黄叶）是同一套。
    """

    #: 便利贴底色 / 描边（沿用橡树语言的浅绿）
    FILL = "#f2f8f2"
    BORDER = "#b9d3c1"
    #: 素材卡片的底色（比说明框略暖一点）
    MATERIAL_FILL = "#fbf7ec"
    MATERIAL_BORDER = "#d8c9a6"
    #: 编辑态的描边（主色）
    EDIT_BORDER = ACCENT
    #: 文件不在了：灰一点
    MISSING_FILL = "#f2f2f0"
    MISSING_BORDER = "#c9c9c4"
    PADDING = 10.0
    #: 素材卡片上那行小字（文件名 / 网址）的高度
    META_H = 18.0

    def __init__(self, note: BoardCard, view: "ProjectBoardView"):
        super().__init__()
        self.note = note                 # 老名字还在用：卡片对象
        self.view = view
        # "文件还在不在"的缓存：要在任何绘制/取文字之前就位
        self._missing_cache: bool | None = None
        self.text_item = QGraphicsTextItem(self)
        self.text_item.setPos(self.PADDING, self.PADDING)
        self.text_item.setTextWidth(max(note.width - self.PADDING * 2, 80.0))
        font = QFont()
        font.setPointSizeF(9.6)
        self.text_item.setFont(font)
        self.text_item.setDefaultTextColor(QColor(TEXT))
        self.text_item.setPlainText(self._body_text())
        self.text_item.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        # 不在编辑态时，文字子项**一个鼠标按键都不收**：
        # 否则双击会被它吃掉，说明框自己的编辑就永远不会触发
        self.text_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.text_item.document().contentsChanged.connect(self._on_text_changed)
        self._editing = False
        self._placeholder = not (note.text or "").strip()

        self.setPos(note.x, note.y)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(5)                 # 在卡片下面、分组框上面
        self.setToolTip(self._build_tooltip())

    # ------------------------------------------------------------- 内容

    @property
    def card(self) -> BoardCard:
        return self.note

    @property
    def is_material(self) -> bool:
        return self.note.is_material

    def _body_text(self) -> str:
        if self.is_material:
            return self.note.text or ("双击打开" if not self.file_missing() else "文件已不在原处")
        return self.note.text or "双击写点什么…"

    def _meta_text(self) -> str:
        """素材卡片下面那行小字：文件名 / 网址。"""
        if self.note.kind == "file":
            return self.note.path
        if self.note.kind == "link":
            return self.note.url
        return ""

    def file_missing(self) -> bool:
        """文件卡片指向的文件还在不在（只读一次，缓存到下次刷新）。"""
        if self.note.kind != "file" or not self.note.path:
            return True
        if self._missing_cache is None:
            self._missing_cache = not Path(self.note.path).exists()
        return self._missing_cache

    def _build_tooltip(self) -> str:
        if self.note.kind == "file":
            lines = ["素材卡片（文件）", self.note.path]
            if self.file_missing():
                lines += ["", "⚠ 文件已经不在原处 — 右键「重新指定文件…」"]
            lines += ["", "双击 = 用系统默认程序打开",
                      "右键 = 转为事件… / 重新指定文件… / 删除这张卡片"]
            return "\n".join(lines)
        if self.note.kind == "link":
            lines = ["素材卡片（链接）", self.note.url, "",
                     "双击 = 在浏览器打开",
                     "右键 = 转为事件… / 修改链接… / 删除这张卡片"]
            return "\n".join(lines)
        return "说明文本框\n双击 = 写内容；拖动 = 挪位置；右键 = 删除"

    # ------------------------------------------------------------- 几何

    def body_rect(self) -> QRectF:
        height = self.text_item.boundingRect().height() + self.PADDING * 2
        if self.is_material:
            height += self.META_H + 4
        return QRectF(0, 0, max(self.note.width, 180.0), max(height, 56.0))

    def card_rect(self) -> QRectF:
        """卡片**看得见**的那块矩形（场景坐标）。

        和 ``ProjectDocumentItem.card_rect()`` 是同一个口径 —— 算障碍/避让时
        两边必须一致；``boundingRect()`` 还带着选中环的余量（``SELECTION_RING_PAD``），
        拿它当障碍会让线绕得莫名其妙。
        """
        return QRectF(self.pos(), self.body_rect().size())

    def boundingRect(self) -> QRectF:  # noqa: N802
        # 选中环（粗蓝环 + 柔光）会画到方框外面 6px，必须一起算进来 ——
        pad = SELECTION_RING_PAD
        return self.body_rect().adjusted(-pad, -pad, pad, pad)

    # ------------------------------------------------------------- 绘制

    def paint(self, painter: QPainter, option, widget=None) -> None:
        rect = self.body_rect()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self.note.color:
            fill, border = self.note.color, self.BORDER
        elif self.is_material:
            missing = self.file_missing()
            fill = self.MISSING_FILL if missing else self.MATERIAL_FILL
            border = self.MISSING_BORDER if missing else self.MATERIAL_BORDER
        else:
            fill, border = self.FILL, self.BORDER
        painter.setBrush(QBrush(QColor(fill)))
        pen = QPen(QColor(self.EDIT_BORDER if self._editing else border))
        pen.setWidthF(1.6 if self._editing else 1.1)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 8, 8)

        # 素材卡片：左上角一片叶子（"这张还没归属，还在长"）
        if self.is_material:
            painter.drawPixmap(8, 6, leaf_pixmap(16))

        # 素材卡片下面那行小字（文件名 / 网址），太长就省略
        if self.is_material:
            meta_font = QFont(painter.font())
            meta_font.setPointSizeF(8.0)
            painter.setFont(meta_font)
            painter.setPen(QPen(QColor(ATTENTION_TEXT if self.file_missing() else MUTED)))
            metrics = QFontMetrics(meta_font)
            meta_rect = QRectF(self.PADDING, rect.height() - self.META_H - 6,
                               rect.width() - self.PADDING * 2, self.META_H)
            painter.drawText(
                meta_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                metrics.elidedText(self._meta_text(), Qt.TextElideMode.ElideMiddle,
                                   int(meta_rect.width())),
            )

        # 选中：粗蓝环 + 柔光
        if self.isSelected() and not self._editing:
            paint_selection_ring(painter, rect, 8.0)

    # ------------------------------------------------------------- 交互

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if self.note.kind == "file":
            self.view.cardOpenRequested.emit(self.note.id)
        elif self.note.kind == "link":
            self.view.cardLinkRequested.emit(self.note.id)
        else:
            self.begin_edit()
        event.accept()

    def begin_edit(self) -> None:
        if self.is_material:
            return                      # 素材卡片的正文不在这里编辑
        if self._placeholder:
            self.text_item.setPlainText("")
            self._placeholder = False
        self._editing = True
        self.text_item.setAcceptedMouseButtons(Qt.MouseButton.LeftButton)
        self.text_item.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextEditorInteraction
        )
        self.text_item.setFocus(Qt.FocusReason.MouseFocusReason)
        cursor = self.text_item.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.text_item.setTextCursor(cursor)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, False)
        self.update()

    def end_edit(self) -> None:
        if not self._editing:
            return
        self._editing = False
        self.text_item.setTextInteractionFlags(
            Qt.TextInteractionFlag.NoTextInteraction
        )
        self.text_item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.text_item.clearFocus()
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        text = self.text_item.toPlainText().strip()
        if not text:
            self.text_item.setPlainText("双击写点什么…")
            self._placeholder = True
        else:
            self.note.text = text
        self.prepareGeometryChange()
        self.update()
        self.view.noteChanged.emit(self.note.id, text)

    def _on_text_changed(self) -> None:
        if not self._editing:
            return
        self.prepareGeometryChange()
        self.update()

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedChange:
            self.setZValue(16 if value else 5)     # 选中的卡片提到最前
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            # 拖动过程中只更新内存里的坐标，**松手才写库**
            # （不然每挪一像素就是一条 UPDATE）
            self.note.x, self.note.y = value.x(), value.y()
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event):  # noqa: N802
        super().mouseReleaseEvent(event)
        if not self.view._rebuilding:
            self.view.noteMoved.emit(self.note.id, self.pos().x(), self.pos().y())

    def focusOutEvent(self, event):  # noqa: N802
        self.end_edit()
        super().focusOutEvent(event)


class BoardGroupItem(QGraphicsItem):
    """白板上的一组事件枝：一个大框把它们框在一起，成员记在库里。

    * 框的大小**跟着成员收紧**（成员一动就重新算），不用手工拉；
    * 拖标题栏 = 整组一起挪（成员位置一起存）；
    * 右键 = 改名 / 改说明 / 导出这一组 / 解散（事件与文件都不动）。
    """

    #: 框的留白（成员四周）
    PADDING = 26.0
    #: 标题栏高度
    HEADER_H = 30.0
    FILL = "#eef5ef"
    BORDER = "#a9c9b4"
    HEADER_FILL = "#dcebdf"

    def __init__(self, group: BoardGroup, members: list[ProjectDocumentItem],
                 view: "ProjectBoardView"):
        super().__init__()
        self.group = group
        self.members = members
        self.view = view
        self._rect = QRectF(0, 0, 10, 10)
        #: 最近一次算出来的框（空组要靠它保持位置）
        self._last_rect: QRectF | None = None
        #: 正在拖的把手：``None`` / ``"right"`` / ``"bottom"`` / ``"corner"``
        self._resize_grip: str | None = None
        self._grip_origin: QPointF | None = None
        self._grip_rect: QRectF | None = None
        #: 按下时框的位置（判断"到底动没动"，全程不变）
        self._press_anchor: QPointF | None = None
        #: 上一次同步成员用的位置（每次跟着更新）
        self._anchor: QPointF | None = None
        self._origins: dict[ProjectDocumentItem, QPointF] = {}
        #: **按下时**成员在哪儿 —— 撤销要用它。
        #: 不能复用 ``_origins``：那是"上一帧"的位置（``_follow_members`` 每帧
        #: 重锚一次），照它算出来的 before 是拖动中途的位置，撤不回原样
        self._press_origins: dict[ProjectDocumentItem, QPointF] = {}
        self._moving_members = False
        self.setZValue(0)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self._refresh_tooltip()

    # ------------------------------------------------------------- 几何

    def fit_to_members(self) -> None:
        """排一下这个框：

        * 组里有事件枝、而且**没被用户手工定过大小** → 框跟着成员收紧
          （往里拖一个新事件枝，框就自动变大把它围进来）；
        * 空组、或者用户手工拖过大小（``group.manual``）→ 用库里记的矩形，
          空组也照着默认尺寸画出来，方便先摆好位置再往里放事件枝。
        """
        group = self.group
        if self.members and not group.manual:
            rect = self.members[0].sceneBoundingRect()
            for item in self.members[1:]:
                rect = rect.united(item.sceneBoundingRect())
            target = QRectF(
                rect.left() - self.PADDING,
                rect.top() - self.PADDING - self.HEADER_H,
                rect.width() + self.PADDING * 2,
                rect.height() + self.PADDING * 2 + self.HEADER_H,
            )
            self._last_rect = target
        else:
            width = group.w or (self._last_rect.width() if self._last_rect
                                else DEFAULT_GROUP_W)
            height = group.h or (self._last_rect.height() if self._last_rect
                                 else DEFAULT_GROUP_H)
            x = group.x or (self._last_rect.left() if self._last_rect else 0.0)
            y = group.y or (self._last_rect.top() if self._last_rect else 0.0)
            target = QRectF(x, y, width, height)
            self._last_rect = target

        self.prepareGeometryChange()
        self.setPos(target.topLeft())
        self._rect = QRectF(0, 0, target.width(), target.height())
        self._refresh_tooltip()
        self.update()

    def scene_rect(self) -> QRectF:
        """框在场景里的位置（拖大拖小之后写库用）。"""
        return QRectF(self.pos().x(), self.pos().y(),
                      self._rect.width(), self._rect.height())

    #: 框外面留一点"可抓"的余量：把手画在框线上，不扩一点的话
    #: 按在框线外侧那一半会落到空白上（变成框选），抓不住
    GRAB = 7.0

    def boundingRect(self) -> QRectF:  # noqa: N802
        return self._rect.adjusted(-self.GRAB, -self.GRAB, self.GRAB, self.GRAB)

    def shape(self) -> QPainterPath:  # noqa: N802
        path = QPainterPath()
        path.addRect(self.boundingRect())
        return path

    def header_rect(self) -> QRectF:
        return QRectF(0, 0, self._rect.width(), self.HEADER_H)

    def _refresh_tooltip(self) -> None:
        lines = [self.group.title]
        if self.group.note:
            lines.append(self.group.note)
        lines += [
            "",
            "拖标题栏 = 整组一起挪（位置一起存）",
            "右键 = 改名 / 写说明 / 导出这一组的文件 / 解散这个组",
            "（解散只去掉这个框，事件和文件一个都不动）",
        ]
        self.setToolTip("\n".join(lines))

    # ------------------------------------------------------------- 绘制

    def paint(self, painter: QPainter, option, widget=None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(QColor(SELECTION_COLOR if self.isSelected() else self.BORDER))
        pen.setWidthF(2.6 if self.isSelected() else 1.2)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor(self.group.color or self.FILL)))
        painter.drawRoundedRect(self._rect, 12, 12)

        header = self.header_rect()
        header_path = QPainterPath()
        header_path.addRoundedRect(header, 12, 12)
        header_path.addRect(QRectF(0, header.height() - 12, header.width(), 12))
        painter.setBrush(QBrush(QColor(self.HEADER_FILL)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawPath(header_path.simplified())

        font = QFont(painter.font())
        font.setPointSizeF(9.4)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(ACCENT)))
        metrics = QFontMetrics(font)
        title = self.group.title
        if self.group.note:
            note_font = QFont(font)
            note_font.setBold(False)
            note_font.setPointSizeF(8.6)
            note_width = QFontMetrics(note_font).horizontalAdvance(self.group.note) + 24
            title = metrics.elidedText(
                title, Qt.TextElideMode.ElideRight,
                max(int(header.width() - note_width), 60),
            )
            painter.drawText(
                QRectF(12, 0, header.width() - 24, header.height()),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight),
                QFontMetrics(note_font).elidedText(
                    self.group.note, Qt.TextElideMode.ElideRight,
                    max(int(header.width() - metrics.horizontalAdvance(title) - 36), 40),
                ),
            )
            painter.setFont(font)
        painter.drawText(
            QRectF(12, 0, header.width() - 24, header.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            title,
        )

        # 选中：外面再套一圈柔光环（框本身已经换成选中色了）
        if self.isSelected():
            paint_selection_ring(painter, self._rect, 12.0)

        # 把三个把手画出来（拖它们改框的大小 —— 空组也要能拖大拖小）
        painter.setBrush(QBrush(QColor(ACCENT)))
        painter.setPen(Qt.PenStyle.NoPen)
        rect = self._rect
        radius = 4.0
        for center in (QPointF(rect.right(), rect.center().y()),
                       QPointF(rect.center().x(), rect.bottom()),
                       QPointF(rect.right(), rect.bottom())):
            painter.drawEllipse(center, radius, radius)

    # ------------------------------------------------------------- 交互

    #: 把手大小（屏幕像素，缩放多少都好按）
    GRIP = 9.0

    def grip_at(self, pos: QPointF) -> str | None:
        """点在哪一个把手上：右下角 / 右中 / 下中（按屏幕像素判定，**有上限**）。"""
        scale = 1.0
        if self.scene() is not None and self.scene().views():
            scale = self.scene().views()[0].transform().m11() or 1.0
        # 上限同 BoardGroupItem 的说明：不然缩小之后把手会把整个框吃掉
        tolerance = max(4.0, min(self.GRIP / scale, 16.0))
        rect = self._rect
        spots = {
            "corner": QPointF(rect.right(), rect.bottom()),
            "right": QPointF(rect.right(), rect.center().y()),
            "bottom": QPointF(rect.center().x(), rect.bottom()),
        }
        for name, center in spots.items():
            if (abs(pos.x() - center.x()) <= tolerance
                    and abs(pos.y() - center.y()) <= tolerance):
                return name
        return None

    def hoverMoveEvent(self, event):  # noqa: N802
        name = self.grip_at(event.pos())
        if name == "right":
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif name == "bottom":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif name == "corner":
            self.setCursor(Qt.CursorShape.SizeFDiagCursor)
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        name = self.grip_at(event.pos())
        if name is not None:
            # 拖把手 = 改这个组框的大小（改完就是"手工尺寸"，不再跟着成员收紧）
            self._resize_grip = name
            self._grip_origin = event.scenePos()
            self._grip_rect = QRectF(self.scene_rect())
            self.setZValue(2)
            event.accept()
            return
        self._press_anchor = self.pos()
        self._anchor = self.pos()
        self._origins = {item: item.pos() for item in self.members}
        self._press_origins = dict(self._origins)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            # 松手事件丢了（按了 Esc 取消、焦点被抢走）时，鼠标一动就会走到这里：
            # 没按着左键 = 这一拖已经结束，不能再改框的大小
            self._resize_grip = None
            super().mouseMoveEvent(event)
            return
        if self._resize_grip is None or self._grip_rect is None:
            super().mouseMoveEvent(event)
            return
        delta = event.scenePos() - self._grip_origin
        rect = QRectF(self._grip_rect)
        if self._resize_grip in ("right", "corner"):
            rect.setWidth(max(rect.width() + delta.x(), 200.0))
        if self._resize_grip in ("bottom", "corner"):
            rect.setHeight(max(rect.height() + delta.y(), 120.0))
        self.prepareGeometryChange()
        self.setPos(rect.topLeft())
        self._rect = QRectF(0, 0, rect.width(), rect.height())
        self.group.w, self.group.h = rect.width(), rect.height()
        self.group.x, self.group.y = rect.left(), rect.top()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._resize_grip is not None:
            self._resize_grip = None
            self._grip_origin = None
            self._grip_rect = None
            self.setZValue(0)
            rect = self.scene_rect()
            self.view.groupRectChanged.emit(self.group.id, rect.x(), rect.y(),
                                            rect.width(), rect.height())
            event.accept()
            return
        super().mouseReleaseEvent(event)
        moved = (self._press_anchor is not None
                 and (self.pos() - self._press_anchor).manhattanLength() > 1.0)
        self._press_anchor = None
        self._anchor = None
        rect = self.scene_rect()
        # before 要用**按下时**的位置，所以先把快照取出来（读完再清）；
        # ``_origins`` 只留给跟手逻辑 —— 它每帧都被 _follow_members 重锚，
        # 拿它当 before 会"撤回到拖动中途"（等于撤销永远不生效）
        origins = self._press_origins
        self._press_origins = {}
        self._origins = {}
        if not moved:
            return
        # 框挪到哪儿，内存里的 group 对象也跟上：下面 positionsChanged 会触发
        # refresh_groups()，手工尺寸的框是照 group.x/y/w/h 摆的，
        # 不更新的话那一瞬间框会先弹回旧位置
        self.group.x, self.group.y = rect.x(), rect.y()
        self.group.w, self.group.h = rect.width(), rect.height()
        # 框的**本体**拖完也要把矩形写回库。只发 positionsChanged 时：
        # 空组的 positions 是空 dict —— 拖了半天一个字节都没存，刷新一下框就弹回
        # 原位；手工定过尺寸的组也同理（成员位置存了、框的没存）。
        # 反过来，**自动收紧**的组不在这里报矩形：_save_group_rect 会把它标成
        # "手工尺寸"，从此不再跟着成员收紧（"往里拖个事件枝框就长大"就废了），
        # 而它的矩形本来就是从成员位置推出来的，不存也不会丢。
        if not self.members or self.group.manual:
            self.view.groupRectChanged.emit(self.group.id, rect.x(), rect.y(),
                                            rect.width(), rect.height())
        positions = {item.node.document_id: (item.pos().x(), item.pos().y())
                     for item in self.members}
        if not positions:
            return
        before = {item.node.document_id: (origin.x(), origin.y())
                  for item, origin in origins.items()}
        self.view.positionsChanged.emit((positions, before))

    def itemChange(self, change, value):  # noqa: N802
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._follow_members()
        return super().itemChange(change, value)

    def _follow_members(self) -> None:
        """框动了，框里的成员跟着一起动（保持相对位置）。"""
        if self._moving_members or self._anchor is None:
            return
        self._moving_members = True
        try:
            delta = self.pos() - self._anchor
            moved: list[ProjectDocumentItem] = []
            for item in self.members:
                origin = self._origins.get(item)
                if origin is not None:
                    item.setPos(origin + delta)
                    moved.append(item)
            # 直角走线要绕开所有卡片，几十张卡时一次全量重算是几百毫秒，
            # 而这是每帧都跑的 —— 拖分组框就卡成幻灯片（用户实测卡死）。
            # （卡片 setPos 时 itemChange 里也会刷它自己的线，这里保留一次显式调用：
            #   两处都只算"相关的线"，代价一样小，但拖动路径不依赖那条隐式路径。）
            for item in moved:
                self.view._scene.refresh_edges_for(item)
            self._anchor = self.pos()
            self._origins = {item: item.pos() for item in self.members}
        finally:
            self._moving_members = False


class _BoardScene(QGraphicsScene):
    """场景承担连线与拖动后的回调，卡片只管自己被拖到哪。"""

    def snap_position(self, item: ProjectDocumentItem, pos: QPointF) -> QPointF:
        """拖动时的对齐吸附：和别的卡片左/中/右、上/中/下差一点就贴齐。

        按住 Alt 拖 = 这次不吸附（想摆得"就是差一点"的时候用）。
        """
        from PySide6.QtWidgets import QApplication
        # 按住 Alt = 这次拖不吸附（也能中途按下 Alt）
        if not getattr(self.view, "_snap_enabled", True):
            return pos
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.AltModifier:
            return pos
        rect = QRectF(pos.x(), pos.y(), CARD_W, CARD_H)
        xs = (rect.left(), rect.center().x(), rect.right())
        ys = (rect.top(), rect.center().y(), rect.bottom())
        best_x: tuple[float, float] | None = None       # (gap, shift)
        best_y: tuple[float, float] | None = None
        for other in self.view._items.values():
            if other is item or other in getattr(item, "_group_items", []):
                continue
            # 必须和"自己"用同一个口径：自己算的是**看得见**的卡片矩形，
            # 而 sceneBoundingRect() 为了两个端口圆点左右各多了 10.5、上下各 6，
            # 拿它去对齐会让贴齐之后还差十几像素（用户实测差 10.5）
            other_rect = other.card_rect()
            other_xs = (other_rect.left(), other_rect.center().x(), other_rect.right())
            other_ys = (other_rect.top(), other_rect.center().y(), other_rect.bottom())
            for mine in xs:
                for theirs in other_xs:
                    gap = abs(mine - theirs)
                    if gap <= SNAP_TOLERANCE and (best_x is None or gap < best_x[0]):
                        best_x = (gap, theirs - mine)
            for mine in ys:
                for theirs in other_ys:
                    gap = abs(mine - theirs)
                    if gap <= SNAP_TOLERANCE and (best_y is None or gap < best_y[0]):
                        best_y = (gap, theirs - mine)
        shift_x = best_x[1] if best_x else 0.0
        shift_y = best_y[1] if best_y else 0.0
        return QPointF(pos.x() + shift_x, pos.y() + shift_y)

    #: 松手时离卡片多近也算连上（场景像素）
    #: —— 从二十几个节点里精确压到目标卡片上很难，差一点就白拖了
    DROP_TOLERANCE = 26.0

    def _nearest_card(self, scene_pos: QPointF,
                      source: ProjectDocumentItem) -> ProjectDocumentItem | None:
        """找离松手点最近的卡片（容差之内）。"""
        best: ProjectDocumentItem | None = None
        best_gap = self.DROP_TOLERANCE
        for item in self.view._items.values():
            if item is source:
                continue
            rect = item.sceneBoundingRect()
            dx = max(rect.left() - scene_pos.x(), 0.0, scene_pos.x() - rect.right())
            dy = max(rect.top() - scene_pos.y(), 0.0, scene_pos.y() - rect.bottom())
            gap = max(dx, dy)
            if gap < best_gap:
                best, best_gap = item, gap
        return best

    def __init__(self, view: "ProjectBoardView"):
        super().__init__(view)
        self.view = view

    def refresh_edges_for(self, item: ProjectDocumentItem) -> None:
        for edge in self.view.edges():
            if edge.source is item or edge.target is item:
                edge.update_path()

    def notify_edge_curve(self, edge: _EdgeItem) -> None:
        """用户把一条线拖出弧度了：存库。"""
        self.view.edgeCurveRequested.emit(
            edge.source.node.document_id, edge.target.node.document_id, edge.curve
        )

    def notify_moved(self, item: ProjectDocumentItem) -> None:
        """一张（或一组）卡片拖完了：把落点 + **原来在哪儿**交给主窗口。

        带上"原来在哪儿"是为了让主窗口能记一条可撤销的操作
        （不然撤销时不知道该挪回哪去）。
        """
        moved = {item.node.document_id: (item.pos().x(), item.pos().y())}
        before = {}
        if item._press_origin is not None:
            before[item.node.document_id] = (item._press_origin.x(),
                                            item._press_origin.y())
        for other in getattr(item, "_group_items", []) or []:
            moved[other.node.document_id] = (other.pos().x(), other.pos().y())
            origin = getattr(item, "_group_origins", {}).get(other)
            if origin is not None:
                before[other.node.document_id] = (origin.x(), origin.y())
        self.view.positionsChanged.emit((moved, before))

    def finish_connection(self, source: ProjectDocumentItem, scene_pos: QPointF,
                          side: str = "out") -> None:
        """松手时找落点上的卡片（**跳过起点自己**），再按拖出的一侧定方向。

        ``side == "out"``（从**右**端口拖出）→ **别人依赖我**：``落点 → 起点``；
        ``side == "in"``（从**左**端口拖出）→ **我依赖别人**：``起点 → 落点``。

        重叠的一小块时，先命中的可能是**起点自己**，于是判定成"拖回原处"直接返回，
        表现就是"从端口拖不出线"。现在挑第一个不是起点的卡片。
        """
        if getattr(source, "_connect_cancelled", False):
            # 这一拖已经被 Esc 取消了：松手不该再连出一条谁都没要的依赖
            source._connect_cancelled = False
            return
        target: ProjectDocumentItem | None = None
        for item in self.items(scene_pos):
            current: QGraphicsItem | None = item
            while current is not None:
                if isinstance(current, ProjectDocumentItem):
                    if current is not source:
                        target = current
                    break
                current = current.parentItem()
            if target is not None:
                break
        if target is None:
            # 差一点点没压上：按"最近的卡片"容错，别让这一拖白费
            target = self._nearest_card(scene_pos, source)
        if target is None:
            return
        if side == "out":
            # 右侧端口 = "别人依赖我"：落点那张卡是依赖方
            self.view.connectionRequested.emit(
                target.node.document_id, source.node.document_id
            )
        else:
            # 左侧端口 = "我依赖别人"：起点那张卡是依赖方
            self.view.connectionRequested.emit(
                source.node.document_id, target.node.document_id
            )


class ProjectBoardView(QGraphicsView):
    """项目树：可拖动、可连线的画布。"""

    #: 缩放比例变了（百分比）—— 右下角那个控件用它刷新
    zoomChanged = Signal(int)
    #: 选中了某张卡片（主窗口据此在右侧面板显示它）
    documentSelected = Signal(int)
    #: 双击卡片 -> 进入事件枝页
    documentActivated = Signal(int)
    #: 单张卡片的坐标（"改回自动布局"用 NaN 表示清掉）
    positionChanged = Signal(int, float, float)
    #: 拖完一次（可能是一组卡片）的落点：``{事件 id: (x, y)}``，主窗口一次写库
    #: 用 ``object`` 而不是 ``dict``：PySide 不会把 Python dict 转成 C++ 类型
    positionsChanged = Signal(object)
    #: 用户从一张卡片拖到另一张：建立一条依赖
    connectionRequested = Signal(int, int)
    #: 右键删除某条连线
    edgeRemoveRequested = Signal(int, int, str)
    #: 空白处右键：在落点新建一个事件枝（场景坐标）
    newEventRequested = Signal(float, float)
    #: 右键卡片：改状态 / 改名 / 删除事件枝
    stateRequested = Signal(int, str)
    renameRequested = Signal(int)
    deleteRequested = Signal(int)
    #: 一句话状态
    statusMessage = Signal(str)
    #: 卡片（说明框 / 文件素材 / 链接）：新建 / 改过 / 挪过 / 要删 / 转正 / 打开
    cardAddRequested = Signal(str, float, float)
    cardPromoteRequested = Signal(int)
    cardOpenRequested = Signal(int)
    #: 打开素材文件**所在的目录**（系统文件管理器）
    cardFolderRequested = Signal(int)
    cardLinkRequested = Signal(int)
    cardRelinkRequested = Signal(int)
    noteRequested = Signal(float, float)
    noteChanged = Signal(int, str)
    noteMoved = Signal(int, float, float)
    noteRemoveRequested = Signal(int)
    #: 分组：新建（给一批事件）/ 改名 / 改说明 / 解散 / 成员变化
    groupRequested = Signal(object)
    groupRenameRequested = Signal(int)
    groupDeleteRequested = Signal(int)
    groupExportRequested = Signal(int)
    groupMembershipRequested = Signal(int, object, bool)
    #: 连线曲度：``(依赖方, 被依赖方, 曲度)``
    edgeCurveRequested = Signal(int, int, float)
    #: 导出框选出来的这一批事件枝
    exportSelectionRequested = Signal(object)
    #: 在画布这个位置新建一个**空分组**
    emptyGroupRequested = Signal(float, float)
    #: 分组框被拖大拖小了：``(组 id, x, y, w, h)``
    groupRectChanged = Signal(int, float, float, float, float)
    # （以前这里还有一个 groupEmptied 信号，谁都没 emit 过、也没人连过 ——
    #   纯死代码，删了。"组变空时把框的矩形存下来"这件事主窗口在
    #   _set_group_membership() 里已经直接做了，不需要这个信号。）
    #: 在画布的某个位置新建（"event" / "note"，来自工具栏的点击或拖入）
    createItemRequested = Signal(str, float, float)
    #: 从文件管理器把文件拖到**某张事件枝卡片**上：``(事件 id, 文件路径列表)``。
    #: 主窗口收到后会先问"放进哪个环节"，再加进那个事件枝的**当前那一稿**。
    #: 白板**空白处**不再收文件（见 :meth:`dropEvent`），所以这个信号只带事件 id，
    #: 不带落点坐标 —— 落点已经不重要了，重要的是落在了哪张卡片上。
    filesDropped = Signal(int, object)
    #: 按 Delete：删掉选中的东西（说明框直接删；组要问一句；事件不动）
    deleteSelectionRequested = Signal()
    #: 「网格排列…」：``(事件 id 列表, 组 id 或 0)`` —— 主窗口负责问参数、写库、撤销
    gridArrangeRequested = Signal(object, int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._scene = _BoardScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        zoom.prepare_zoomable(self)     # Ctrl+滚轮围绕鼠标缩放
        # 平移：中键拖 / 空格 + 左键拖（**空白处左键拖留给框选**，
        # 照 Obsidian 白板的习惯来）
        zoom.prepare_space_pan(self, Qt.CursorShape.ArrowCursor,
                               empty_left_pans=False)
        #: 自适应画布：尺寸一变就重新适应；手动缩放过就不动用户的镜头
        self._auto_fit = True
        # 不能用 RubberBandDrag：它在按下后抢走鼠标，从端口拉线的
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor("#fbfcfd")))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)

        self._items: dict[int, ProjectDocumentItem] = {}
        self._edge_items: list[_EdgeItem] = []
        self._group_items: dict[int, BoardGroupItem] = {}
        self._cards: dict[int, BoardCardItem] = {}
        self._graph: ProjectGraph | None = None
        self._zoom = 1.0
        self._fitting = False
        #: 画布尺寸变化时要不要重新"适应窗口"（默认要，见 EventStageView 同一处说明）
        self.follow_resize = True
        #: 尺寸还是假的（视图没布局完）：等有真尺寸了必须适应一次
        self._pending_fit = False
        self._last_notified: int | None = None
        self._temp_line: QGraphicsPathItem | None = None
        #: 这次拖动要不要对齐吸附（按下时按着 Alt 就关掉）
        self._snap_enabled = True
        #: 连线怎么走：``curve`` 枝叶弧线 / ``orth`` 直角折线（主窗口从库里读回来）
        self.edge_route = EDGE_ROUTE_CURVE
        #: Shift 框选时要保住的原有选择
        self._band_keep: list[QGraphicsItem] = []
        self._rebuilding = False
        self._scene.selectionChanged.connect(self._on_selection_changed)

    # -------------------------------------------------------------- 载入

    def set_graph(self, graph: ProjectGraph | None, keep_view: bool = False) -> None:
        """装进一张版图。

        :param keep_view: 保住当前镜头（缩放 + 滚动位置）。拖节点、连连线之后
            只是把改动重画一遍，**不该**把用户辛苦调好的视角又拉回"适应全部"。
        """
        camera = None
        if keep_view and self._items:
            camera = (self.transform(),
                      self.horizontalScrollBar().value(),
                      self.verticalScrollBar().value(),
                      self._auto_fit,
                      self._scene.sceneRect())
        self._graph = graph
        self._rebuilding = True
        try:
            self._items.clear()
            self._edge_items.clear()
            self._group_items.clear()
            self._cards.clear()
            self._temp_line = None
            self._scene.clear()
        finally:
            self._rebuilding = False

        if graph is None or not graph.nodes:
            self._draw_placeholder("这个项目还没有事件")
            self._restore_camera(camera)
            return

        # 白板：不画列背景、也不画"项目 → 事件"的放射线。

        for node in graph.nodes:
            item = ProjectDocumentItem(node)
            self._scene.addItem(item)
            self._items[node.document_id] = item
        # 分组框先加（在最底下），再是连线、卡片 —— 卡片永远在最上面好点
        for group in graph.groups:
            members = [self._items[item] for item in group.document_ids
                       if item in self._items]
            # **空组也要画出来**：先摆好框、再往里放事件枝（框会自动变大）
            group_item = BoardGroupItem(group, members, self)
            self._scene.addItem(group_item)
            self._group_items[group.id] = group_item
        for card in graph.cards:
            card_item = BoardCardItem(card, self)
            self._scene.addItem(card_item)
            self._cards[card.id] = card_item
        for edge in graph.edges:
            source = self._items.get(edge.source_document_id)
            target = self._items.get(edge.target_document_id)
            if source is None or target is None:
                continue
            edge_item = _EdgeItem(source, target, edge.kind, edge.label, edge.curve,
                                  route=self.edge_route)
            self._scene.addItem(edge_item)
            self._edge_items.append(edge_item)

        for group_item in self._group_items.values():
            group_item.fit_to_members()
        # 连线建的时候还没进场景（取不到别的卡片），进场景之后重算一遍：
        # 直角走线要靠这个才知道该绕开谁
        self.refresh_edges()
        self._scene.setSceneRect(
            QRectF(0, 0, graph.width, graph.height).adjusted(
                -CANVAS_MARGIN, -CANVAS_MARGIN, CANVAS_MARGIN, CANVAS_MARGIN
            )
        )
        self.ensure_scene_room(grow=False)      # 先把"看得见的一屏 + 空白"铺够
        self._restore_camera(camera)

    # ------------------------------------------------------- 画布留白（自动扩展）

    def visible_scene_rect(self) -> QRectF:
        return self.mapToScene(self.viewport().rect()).boundingRect()

    def ensure_scene_room(self, *, grow: bool = True) -> None:
        """保证"看得见的这一块 + 一圈空白"都在画布范围内；不够就往外扩。

        想在外圈摆个新节点都没地方。现在按需要往外长：
        * 看得见的范围外面至少留 ``CANVAS_MARGIN``（外加半个视口）；
        * 一旦贴到这个边，就朝那一侧再铺 ``CANVAS_GROW``。
        """
        if self._graph is None and not self._items:
            return
        if (self.transform().m11() or 1.0) < 0.25:
            # 缩得很小的时候"看得见的范围"会大得离谱（实测能把场景矩形撑到两万宽），
            # 这时候不该按它去扩场景：用户是在缩小看全局，不是在找边缘
            return
        visible = self.visible_scene_rect()
        if visible.isEmpty():
            return
        margin = CANVAS_MARGIN + max(visible.width(), visible.height()) * 0.3
        wanted = visible.adjusted(-margin, -margin, margin, margin)
        rect = self._scene.sceneRect()
        if rect.contains(wanted):
            return
        left = CANVAS_GROW if grow and wanted.left() < rect.left() else 0.0
        top = CANVAS_GROW if grow and wanted.top() < rect.top() else 0.0
        right = CANVAS_GROW if grow and wanted.right() > rect.right() else 0.0
        bottom = CANVAS_GROW if grow and wanted.bottom() > rect.bottom() else 0.0
        self._scene.setSceneRect(
            rect.united(wanted).adjusted(-left, -top, right, bottom)
        )

    def _restore_camera(self, camera) -> None:
        """把重建前的镜头放回去（缩放 + 滚动条 + 场景范围）。"""
        if camera is None:
            return
        transform, h_value, v_value, auto_fit, scene_rect = camera
        self.setTransform(transform)
        self._zoom = self.transform().m11()
        self._auto_fit = auto_fit
        # 场景范围也要并回来（只并大、不缩小）。为什么：``set_graph`` 开头把场景
        # 重置成"内容 + 一圈空白"，那圈空白是按**当时的缩放**算的 —— 用户缩到
        # 150% 时算出来比原来小，滚动条范围跟着变短，旧位置就被夹住了
        # （实测垂直位置 -1193 → -1086，画面照样挪了一下）。
        # 用 united 而不是直接覆盖：新内容（撤销撤销回来的新节点）绝不能被裁掉。
        self._scene.setSceneRect(self._scene.sceneRect().united(scene_rect))
        self.horizontalScrollBar().setValue(h_value)
        self.verticalScrollBar().setValue(v_value)
        self.zoomChanged.emit(self.zoom_percent())

    def graph(self) -> ProjectGraph | None:
        return self._graph

    def apply_positions(self, positions: dict) -> None:
        """拖完只**就地**更新落点与相关连线 —— 不重建场景。

        重建会让整块画布闪一下、选中也丢了（连续用方向键微调时就废了），
        而拖动本来就只改坐标这一件事。
        """
        moved = False
        for document_id, (x, y) in positions.items():
            item = self._items.get(document_id)
            if item is None:
                continue
            item.node.x, item.node.y = x, y
            if item.pos() != QPointF(x, y):
                item.setPos(x, y)
            self._scene.refresh_edges_for(item)
            moved = True
        if moved and self._graph is not None:
            self._graph.manual = True
            self._graph.layout = "manual"
        if moved:
            self._join_groups_for(positions)  # 拖进哪个组就算哪个组的人
            self.refresh_groups()           # 分组框跟着成员收紧（等于自动变大）
            self.ensure_scene_room()        # 拖到边上时画布跟着往外长

    def _join_groups_for(self, positions: dict) -> None:
        """

        先把"该加入谁"收集完再逐个发信号：接收方**会重建整个场景**
        （``_group_items`` 被 clear 之后重填），边迭代边发信号等于在换过一茬的
        字典上继续跑 —— 有的组会被漏掉、有的会被重复处理。
        """
        pending: list[tuple[int, int]] = []
        for group_item in self._group_items.values():
            rect = group_item.scene_rect()
            members = set(group_item.group.document_ids)
            for document_id in positions:
                if document_id in members:
                    continue
                item = self._items.get(document_id)
                if item is None:
                    continue
                center = item.sceneBoundingRect().center()
                if rect.contains(center):
                    pending.append((group_item.group.id, document_id))
        for group_id, document_id in pending:
            self.groupMembershipRequested.emit(group_id, [document_id], True)

    def edges(self) -> list[_EdgeItem]:
        return self._edge_items

    def set_edge_route(self, route: str) -> None:
        """切换连线走向（枝叶弧线 / 直角折线）—— 立刻重画所有连线。"""
        route = route if route in EDGE_ROUTES else EDGE_ROUTE_CURVE
        if route == self.edge_route:
            self.refresh_edges()      # 走向没变也要重算：可能是新摆好的卡片
            return
        self.edge_route = route
        for edge in self._edge_items:
            edge.set_route(route)

    # --------------------------------------------------------- 网格排列

    def grid_layout(self, document_ids=None, cols: int = 0, rows: int = 0,
                    gap: float = GRID_GAP) -> list[tuple[int, float, float]]:
        """把一批事件排成**几行几列**，返回 ``[(事件 id, x, y), …]``（不改动画面）。

        只算位置，"写库 + 撤销"交给主窗口（和拖动落点走同一条路）。

        * 顺序按**现在的摆法**读：先上后下、先左后右 —— 用户看到的次序就是他想要的次序；
        * 起点锚在原来那批卡片的左上角，排完还在这附近，不会一下跳到天边；
        * ``cols`` / ``rows`` 给一个就行，另一个按数量算；都不给就摆成接近正方形；
        * 排完如果压到了**没参与排列**的卡片，整块一起挪到最近的空位
        """
        items = self._items_for(document_ids)
        if len(items) < 2:
            return []

        def reading_order(item: ProjectDocumentItem):
            rect = item.card_rect()
            return (round(rect.top() / 50.0) + rect.top() / 100000.0, rect.left())

        ordered = sorted(items, key=reading_order)
        count = len(ordered)
        cols, rows = grid_shape(count, cols, rows)
        # 用户可能同时给了行列、又装不下（比如 6 张卡说"3 列 1 行"）：实际会排出
        # 2 行，多出来的那行会压到 block_h 没算到的位置上（下面的卡片）。
        # 先把行数补够，再拿它算整块的高度。
        rows = max(rows, math.ceil(count / max(cols, 1)))
        box_w = max(item.card_rect().width() for item in ordered)
        box_h = max(item.card_rect().height() for item in ordered)
        anchor_x = min(item.card_rect().left() for item in ordered)
        anchor_y = min(item.card_rect().top() for item in ordered)

        step_x, step_y = box_w + gap, box_h + gap
        block_w = cols * step_x - gap
        block_h = rows * step_y - gap
        anchor_x, anchor_y = self._free_block_spot(
            anchor_x, anchor_y, block_w, block_h,
            skip={item for item in ordered},
        )

        placed: list[tuple[int, float, float]] = []
        for index, item in enumerate(ordered):
            col, row = index % cols, index // cols
            rect = item.card_rect()
            # 卡片大小不一样时（说明框、素材卡片）在格子里居中，看起来才齐
            x = anchor_x + col * step_x + (box_w - rect.width()) / 2
            y = anchor_y + row * step_y + (box_h - rect.height()) / 2
            placed.append((item.node.document_id, x, y))
        return placed

    def _items_for(self, document_ids=None) -> list[ProjectDocumentItem]:
        """要排列的那些卡片：给 id 就用给的，没给就用选中的，再没有就全排。"""
        if document_ids:
            wanted = {int(item) for item in document_ids}
            return [item for item in self._items.values()
                    if item.node.document_id in wanted]
        selected = [item for item in self._items.values() if item.isSelected()]
        return selected or list(self._items.values())

    def _free_block_spot(self, x: float, y: float, w: float, h: float,
                         skip: set | None = None) -> tuple[float, float]:
        """整块矩形挪到"不压别人的"位置（压着了就一圈圈往外找）。"""
        skip = skip or set()
        others: list[QRectF] = []
        for item in self._items.values():
            if item in skip:
                continue
            others.append(item.card_rect())
        for card in self._cards.values():
            others.append(card.sceneBoundingRect())
        for group_item in self._group_items.values():
            rect = group_item.scene_rect()
            if rect.width() <= 1 or rect.height() <= 1:
                continue
            # **已经把这批卡片罩在里面的容器（组框）不算障碍**：锚点是这批卡片
            # 自己的左上角，对组里的成员来说它必然落在这个组框内 —— 拿自己的框
            # 当障碍的话，"组里排网格"永远判成压着东西，于是整批卡片被推到组框
            # 正好压在锚点上"这种巧合：那种情况是真重叠，不能放过。
            if rect.adjusted(0.5, 0.5, -0.5, -0.5).contains(QPointF(x, y)):
                continue
            others.append(rect)

        def hits(cx: float, cy: float) -> bool:
            rect = QRectF(cx, cy, w, h)
            return any(rect.intersects(other) for other in others)

        if not hits(x, y):
            return x, y
        step_x, step_y = w + GRID_GAP, h + GRID_GAP
        for ring in range(1, 12):
            for dx, dy in ((ring, 0), (0, ring), (ring, ring), (-ring, 0),
                           (0, -ring), (-ring, ring), (ring, -ring), (-ring, -ring)):
                cx, cy = max(x + dx * step_x, 0.0), max(y + dy * step_y, 0.0)
                if not hits(cx, cy):
                    return cx, cy
        return x, y

    def refresh_edges(self) -> None:
        for edge in self._edge_items:
            edge.update_path()

    def refresh_groups(self) -> None:
        for group_item in self._group_items.values():
            group_item.fit_to_members()

    def group_rect_for(self, group_id: int) -> tuple[float, float, float, float] | None:
        item = self._group_items.get(group_id)
        if item is None:
            return None
        rect = item.scene_rect()
        return rect.x(), rect.y(), rect.width(), rect.height()

    def item_for(self, document_id: int) -> ProjectDocumentItem | None:
        return self._items.get(int(document_id))

    def selected_document_ids(self) -> list[int]:
        """当前选中的事件枝（框选/多选都算，按画布顺序）。"""
        return [item.node.document_id for item in self._items.values()
                if item.isSelected()]

    def cards(self) -> list[BoardCard]:
        return [item.note for item in self._cards.values()]

    @property
    def _note_items(self) -> dict[int, BoardCardItem]:
        """说明框那一批（老接口：界面/测试都在用这个名字）。"""
        return {card_id: item for card_id, item in self._cards.items()
                if item.note.kind == "note"}

    def groups(self) -> list[BoardGroup]:
        return [item.group for item in self._group_items.values()]

    def select_document(self, document_id: int | None) -> None:
        if document_id is None:
            return
        item = self._items.get(document_id)
        if item is None:
            return
        self._scene.clearSelection()
        item.setSelected(True)
        self.ensureVisible(item, 80, 80)

    # ------------------------------------------------------------- 绘制

    def _draw_placeholder(self, message: str) -> None:
        """空版图的占位：程序图标 + 一段枝条 + 一句提示（简洁）。"""
        self._scene.addPixmap(logo_pixmap(60)).setPos(24, 8)
        self._scene.addPixmap(branch_pixmap(210, 20)).setPos(24, 78)
        text_item = self._scene.addText(message)
        text_item.setDefaultTextColor(QColor(MUTED))
        font = QFont()
        font.setPointSizeF(11)
        text_item.setFont(font)
        text_item.setPos(24, 108)
        self._scene.setSceneRect(QRectF(0, 0, 560, 170))

    # ------------------------------------------------------------- 连线预览

    def mousePressEvent(self, event):  # noqa: N802
        # 按下去时按住 Alt = 这一次拖动不做对齐吸附
        self._snap_enabled = not bool(
            event.modifiers() & Qt.KeyboardModifier.AltModifier
        )
        # 平移：中键 / 空格 + 左键（这两种哪儿都能按）
        if zoom.space_pan_press(self, event):
            event.accept()
            return
        # 空白处左键拖 = 框选（Obsidian 的习惯）。
        # 按住 Shift 框选 = **加选**（不清掉原来的选择）
        if (event.button() == Qt.MouseButton.LeftButton
                and self.itemAt(event.position().toPoint()) is None):
            keep = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            self._band_keep = self._scene.selectedItems() if keep else []
            if not keep:
                # Qt 的橡皮筋按下本身也会清，这里明写一遍：语义是自己的，
                # 不依赖 Qt 的默认行为（某天换了拖拽模式就不成立了）。
                self._scene.clearSelection()
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            super().mousePressEvent(event)
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        if zoom.space_pan_key(self, event):
            event.accept()
            return
        if self._nudge_selection(event):
            return
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self._scene.selectedItems():
                self.deleteSelectionRequested.emit()
                event.accept()
                return
        if event.key() == Qt.Key.Key_Escape:
            for note_item in self._note_items.values():
                note_item.end_edit()
            self._cancel_interactions()
            self._scene.clearSelection()
            event.accept()
            return
        super().keyPressEvent(event)

    def _cancel_interactions(self) -> None:
        """Esc = 这一拖不算：把"正在拖"的各种状态全放掉。

        为什么非清不可：Esc 之后 Qt 不一定再给那个图元发 mouseRelease
        （鼠标已经挪开、焦点被抢走），状态挂着的话，后面"不按任何键划过"
        还会继续改线形 / 改框大小，端口那一路甚至还能连出一条谁都没要的依赖。
        """
        self._clear_temp_line()
        for item in self._scene.items():
            if isinstance(item, ProjectDocumentItem):
                item._dragging = False
                if item._connecting:
                    # 端口拖线途中取消：留个标记，松手时不再连（见 finish_connection）
                    item._connect_cancelled = True
                    item._connect_side = None
            elif isinstance(item, BoardGroupItem):
                item._resize_grip = None
            elif isinstance(item, _EdgeItem):
                item._bending = False

    def _nudge_selection(self, event) -> bool:
        """方向键微调选中的卡片（Shift = 一次挪半格），位置照样落库。"""
        steps = {
            Qt.Key.Key_Left: (-1.0, 0.0),
            Qt.Key.Key_Right: (1.0, 0.0),
            Qt.Key.Key_Up: (0.0, -1.0),
            Qt.Key.Key_Down: (0.0, 1.0),
        }
        step = steps.get(event.key())
        if step is None:
            return False
        selected = [item for item in self._scene.selectedItems()
                    if isinstance(item, ProjectDocumentItem)]
        if not selected:
            return False
        size = 25.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 5.0
        moved: dict[int, tuple[float, float]] = {}
        before: dict[int, tuple[float, float]] = {}
        for item in selected:
            before[item.node.document_id] = (item.pos().x(), item.pos().y())
            item.setPos(item.pos().x() + step[0] * size, item.pos().y() + step[1] * size)
            moved[item.node.document_id] = (item.pos().x(), item.pos().y())
        self.positionsChanged.emit((moved, before))
        self.ensure_scene_room()
        return True

    def keyReleaseEvent(self, event):  # noqa: N802
        if zoom.space_pan_key(self, event):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if zoom.space_pan_move(self, event):
            self._auto_fit = False
            zoom.end_stable_fit(self)           # 手动平移：滚动条还给用户
            self.ensure_scene_room()            # 快贴到边就往外长一截
            event.accept()
            return
        connecting = self._connecting_source()
        if connecting is not None:
            source, side = connecting
            point = event.position().toPoint()
            self._update_temp_line(source, self.mapToScene(point), side)
        super().mouseMoveEvent(event)

    def _connecting_source(self) -> tuple[ProjectDocumentItem, str] | None:
        for item in self._scene.items():
            if isinstance(item, ProjectDocumentItem) and item._connecting:
                return item, (item._connect_side or "out")
        return None

    def _update_temp_line(self, source: ProjectDocumentItem, scene_pos: QPointF,
                          side: str = "out") -> None:
        anchor = source.port_scene_pos() if side == "out" else source.input_scene_pos()
        path = QPainterPath(anchor)
        path.lineTo(scene_pos)
        if self._temp_line is None:
            pen = QPen(QColor(DEP_EDGE_COLOR))
            pen.setWidthF(2.0)
            pen.setStyle(Qt.PenStyle.DashLine)
            self._temp_line = self._scene.addPath(path, pen)
            self._temp_line.setZValue(40)
        else:
            self._temp_line.setPath(path)

    def _clear_temp_line(self) -> None:
        if self._temp_line is not None:
            self._scene.removeItem(self._temp_line)
            self._temp_line = None

    def mouseReleaseEvent(self, event):  # noqa: N802
        if zoom.space_pan_release(self, event):
            event.accept()
            return
        if self.dragMode() == QGraphicsView.DragMode.RubberBandDrag:
            super().mouseReleaseEvent(event)
            self.setDragMode(QGraphicsView.DragMode.NoDrag)   # 框选完就还给卡片
            if self._band_keep:                               # Shift 框选：加选
                for item in self._band_keep:
                    item.setSelected(True)
                self._band_keep = []
            return
        self._clear_temp_line()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event):  # noqa: N802
        """画布尺寸变了：**默认不重算**（稳定模式，见 EventStageView 的同一处说明）。

        打开「排版跟随窗口」时才重新适应；否则只让视口变大变小 ——
        白板上还额外补一下"看得见的范围"（那只是场景矩形，不影响镜头）。
        """
        super().resizeEvent(event)
        if self._pending_fit or (self.follow_resize and getattr(self, "_auto_fit", True)):
            self.fit_to_content()               # 首次拿到真尺寸时一定要适应一次
        else:
            self.ensure_scene_room(grow=False)

    # ------------------------------------------------------- 拖入新增（Obsidian 那套）

    # 拖文件进来时 **dragEnter/dragMove 一律接受**（哪怕落点是空白处）：
    # 只有收下这一次拖放，dropEvent 才有机会说一句"要拖到事件枝卡片上"。
    # 在 dragEnter 里拒绝虽然鼠标会变"禁止"，但用户拿不到任何解释，
    # 只会觉得"这个软件拖不动文件"。

    def dragEnterEvent(self, event):  # noqa: N802
        if self._drop_kind(event) is not None:
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if self._drop_kind(event) is not None:
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        kind = self._drop_kind(event)
        if kind is None:
            super().dropEvent(event)
            return
        point = self.mapToScene(event.position().toPoint())
        if kind == "files":
            paths = [url.toLocalFile() for url in event.mimeData().urls()
                     if url.isLocalFile()]
            event.acceptProposedAction()
            if not paths:
                return
            item = self.document_item_at(point)
            if item is None:
                # **白板空白处不收文件**（用户定的）：以前往这儿丢一个文件会放一张
                # 落点本身就是个陷阱 —— "要拖就拖到白板的节点上，然后再确定要放在
                # 哪个类型下"。所以空白处只给一句指路的话，库和画布都不动。
                #
                # 这里仍然 acceptProposedAction：Qt 里 dragEnter 一拒绝，drop 事件
                # 根本不会送到，用户连一句解释都看不到。收下拖放、什么都不做、
                # 把话说明白，才是这里要的"不接受"。
                self.statusMessage.emit(
                    "白板空白处不收文件：拖到某张事件枝卡片上，再选放进哪个环节"
                )
                return
            self.filesDropped.emit(int(item.node.document_id), paths)
            return
        event.acceptProposedAction()
        self.createItemRequested.emit(kind, point.x(), point.y())

    def document_item_at(self, scene_pos: QPointF) -> "ProjectDocumentItem | None":
        """落点下面是哪张**事件枝卡片**（空白处 / 说明框 / 连线 / 组框上都返回 ``None``）。

        用 ``scene.items()`` 挨个看而不是 ``itemAt()`` 只看最上面那一个：
        连线和组框可能盖在卡片上面，``itemAt()`` 会把"明明拖在卡片上"判成空白处。
        """
        for item in self._scene.items(scene_pos):
            if isinstance(item, ProjectDocumentItem):
                return item
        return None

    def _drop_kind(self, event) -> str | None:
        """这次拖进来的是什么：``"event"`` / ``"note"`` / ``"files"`` / ``None``。"""
        mime = event.mimeData()
        if mime.hasFormat(NEW_ITEM_MIME):
            kind = bytes(mime.data(NEW_ITEM_MIME)).decode("utf-8", "replace")
            return kind if kind in ("event", "note") else None
        if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            return "files"
        return None

    #: 新建卡片时，为了不叠在别人身上，往右/下让开多少（场景像素）
    FREE_STEP_X = CARD_W + 18.0
    FREE_STEP_Y = CARD_H + 18.0

    def free_spot_near(self, x: float, y: float,
                       taken: set[tuple[int, int]] | None = None) -> tuple[float, float]:
        """

        从落点开始，一圈圈往外找（右、下、右下…），碰到合适的位置就用它；
        同一次批量建几张卡片时，把已经占的位置传进 ``taken``，它们之间也不会叠。
        """
        occupied = [item.card_rect() for item in self._items.values()]
        occupied += [item.sceneBoundingRect() for item in self._cards.values()
                     if item.is_material or item.note.kind == "note"]
        rect = QRectF(x, y, CARD_W, CARD_H)
        if not any(rect.intersects(other) for other in occupied):
            return x, y
        for ring in range(1, 12):
            for dx, dy in ((ring, 0), (0, ring), (ring, ring), (-ring, 0),
                           (0, -ring), (-ring, ring), (ring, -ring), (-ring, -ring)):
                candidate = QRectF(x + dx * self.FREE_STEP_X, y + dy * self.FREE_STEP_Y,
                                   CARD_W, CARD_H)
                if any(candidate.intersects(other) for other in occupied):
                    continue
                if taken and (round(candidate.x()), round(candidate.y())) in taken:
                    continue
                if taken is not None:
                    taken.add((round(candidate.x()), round(candidate.y())))
                return candidate.x(), candidate.y()
        return x, y                       # 实在没地方就照原样放

    def canvas_center_scene_pos(self) -> QPointF:
        """画布中央对应的场景坐标（工具栏点击新建时放这儿）。"""
        return self.mapToScene(self.viewport().rect().center())

    # ------------------------------------------------------------- 缩放

    def fit_to_content(self) -> None:
        if not self._items:
            return
        if self.viewport().width() < 160 or self.viewport().height() < 160:
            # 视图还没被布局过（尺寸是假的，实测遇到过 98×28）：现在算会得到一个
            # 荒唐的缩放（0.09 倍），而且稳定模式下没人会去纠正它。
            # 记下"待适应"，等真正有尺寸了（resizeEvent）再算。
            self._pending_fit = True
            return
        if self._fitting:
            return                              # 重入保护
        self._pending_fit = False
        zoom.begin_stable_fit(self)             # 收掉滚动条，免得来回抖动
        self._fitting = True
        try:
            self.fitInView(
                self._scene.itemsBoundingRect().adjusted(-30, -30, 30, 30),
                Qt.AspectRatioMode.KeepAspectRatio,
            )
        finally:
            self._fitting = False
        self._zoom = self.transform().m11()
        self.ensure_scene_room(grow=False)      # 适应之后也要留出空白

    def zoom_in(self) -> None:
        self._apply_zoom(1.2)

    def zoom_out(self) -> None:
        self._apply_zoom(1 / 1.2)

    def zoom_percent(self) -> int:
        """当前缩放百分比（右下角控件显示的就是它）。"""
        return zoom.current_zoom_percent(self)

    def set_zoom_percent(self, percent: float) -> None:
        self._auto_fit = False
        zoom.end_stable_fit(self)        # 手动缩放：滚动条还给用户
        zoom.set_zoom_percent(self, percent)
        self.zoomChanged.emit(self.zoom_percent())

    def is_auto_fit(self) -> bool:
        return self._auto_fit

    def _apply_zoom(self, factor: float) -> None:
        target = self._zoom * factor
        if not 0.2 <= target <= 3.0:
            return
        self.scale(factor, factor)
        self._zoom = target

    def wheelEvent(self, event):  # noqa: N802
        """Ctrl+滚轮缩放（围绕鼠标）；不按 Ctrl 就是普通滚动。"""
        if zoom.wheel_zoom(self, event):
            self._zoom = self.transform().m11()
            self._auto_fit = False
            self.ensure_scene_room()            # 缩小之后看得更远，跟随补齐
            self.zoomChanged.emit(self.zoom_percent())
            event.accept()
            return
        super().wheelEvent(event)

    # ------------------------------------------------------------- 交互

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        point = event.position().toPoint()
        top = self.itemAt(point)
        item = top
        while item is not None and not isinstance(item, ProjectDocumentItem):
            item = item.parentItem()
        if isinstance(item, ProjectDocumentItem):
            self.documentActivated.emit(item.node.document_id)
            event.accept()
            return
        # 只有真的点在**空白**上才建说明卡片；点在说明框/分组/连线上时
        # 交给图元自己的双击处理（说明框双击 = 进入编辑）
        if top is None and event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(point)
            self.createItemRequested.emit("note", scene_pos.x(), scene_pos.y())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        item = self.itemAt(event.pos())
        scene_pos = self.mapToScene(event.pos())
        # 卡片/节点上那层**文字**是 QGraphicsTextItem 子项：``itemAt()`` 会把它
        # 先还给调用方，于是右键点在正文上时，下面那些 isinstance 一个都不成立，
        # 用户看到的是"空白处"的菜单（加说明框 / 建组…）——明明点在自己的卡片上。
        # 所以先沿着父项回溯一次，落到"真正可以右键的那个图元"上再分派。
        # （节点那条分支本来就有这么一趟回溯，这里只是把它提到最前面统一做。）
        while item is not None and not isinstance(
            item, (_EdgeItem, BoardCardItem, BoardGroupItem, ProjectDocumentItem)
        ):
            item = item.parentItem()

        # 1) 连线上：调曲度 / 剪断
        if isinstance(item, _EdgeItem):
            edge = item
            label = "依赖" if edge.kind == DEPENDENCY_KIND else edge.kind_label
            menu = QMenu(self)
            menu.addAction(
                "怎么调弯？",
                lambda: self.statusMessage.emit("按住线中间的小圆点拖 = 把这条线拱起来"),
            )
            menu.addSeparator()
            menu.addAction(
                prune_icon(16),
                f"剪断这条{label}连线",
                lambda: self.edgeRemoveRequested.emit(
                    edge.source.node.document_id, edge.target.node.document_id, edge.kind
                ),
            )
            menu.exec(event.globalPos())
            return

        # 2) 说明文本框
        if isinstance(item, BoardCardItem):
            card_item = item
            card = card_item.note
            menu = QMenu(self)
            if card.is_material:
                # 素材卡片：转正是它的出口（说明框不转事件）
                menu.addAction(
                    leaf_icon(16), "转为事件…",
                    lambda: self.cardPromoteRequested.emit(card.id),
                ).setToolTip("建一个事件枝，把这份文件归档成第一稿；可以顺便选归入哪个分组")
                if card.kind == "file":
                    menu.addAction(open_icon(16), "打开文件",
                                   lambda: self.cardOpenRequested.emit(card.id))
                    # 「打开所在目录」跟「打开文件」并排（跟事件枝页文件行的菜单同款）：
                    folder_action = menu.addAction(
                        folder_icon(16), "打开所在目录",
                        lambda: self.cardFolderRequested.emit(card.id),
                    )
                    if Path(card.path or "").is_file():
                        folder_action.setToolTip("在系统文件管理器里打开这份素材所在的目录")
                    else:
                        # 素材卡片记的是库外的绝对路径：文件被挪走/删掉之后
                        # 点"打开所在目录"只会开到一个不存在的路径，所以置灰 + 说清怎么修
                        folder_action.setEnabled(False)
                        folder_action.setToolTip(
                            "这份文件已不在原处，可用「重新指定文件…」把它指回来"
                        )
                    menu.addAction("重新指定文件…",
                                   lambda: self.cardRelinkRequested.emit(card.id))
                else:
                    menu.addAction(open_icon(16), "在浏览器打开",
                                   lambda: self.cardLinkRequested.emit(card.id))
                    menu.addAction(pencil_icon(16), "修改链接…",
                                   lambda: self.cardRelinkRequested.emit(card.id))
                menu.addSeparator()
            else:
                menu.addAction(pencil_icon(16), "编辑内容", card_item.begin_edit)
                menu.addSeparator()
            menu.addAction(
                prune_icon(16),
                "删除这张卡片" if card.is_material else "删除这个说明框",
                lambda: self.noteRemoveRequested.emit(card.id),
            )
            menu.exec(event.globalPos())
            return

        # 3) 分组框
        if isinstance(item, BoardGroupItem):
            group_item = item
            group = group_item.group
            menu = QMenu(self)
            menu.addAction(pencil_icon(16), "改名…",
                           lambda: self.groupRenameRequested.emit(group.id))
            menu.addAction(
                export_icon(16), "导出这一组的文件…",
                lambda: self.groupExportRequested.emit(group.id),
            )
            menu.addSeparator()
            menu.addAction(
                f"选中组里这 {len(group_item.members)} 个事件枝",
                lambda: [member.setSelected(True) for member in group_item.members],
            )
            arrange = menu.addAction(
                grid_icon(16),
                f"把这 {len(group_item.members)} 个事件枝排成网格…",
                lambda: self.gridArrangeRequested.emit(
                    [member.node.document_id for member in group_item.members],
                    group.id,
                ),
            )
            arrange.setEnabled(len(group_item.members) >= 2)
            arrange.setToolTip("选几行几列，排好后这个框会自动收紧围住它们")
            menu.addAction(
                prune_icon(16), "解散这个组",
                lambda: self.groupDeleteRequested.emit(group.id),
            ).setToolTip("只去掉这个框和成员记录，事件和文件一个都不动")
            menu.exec(event.globalPos())
            return

        # 走到这里 item 只可能是节点卡片或 None（别的类型上面都 return 了；
        # 文字子项已经在函数开头回溯到父图元了）
        menu = QMenu(self)
        if isinstance(item, ProjectDocumentItem):
            node = item.node
            menu.addAction(open_icon(16), "打开事件枝页",
                           lambda: self.documentActivated.emit(node.document_id))
            menu.addAction(
                "怎么画线？",
                lambda: self.statusMessage.emit(
                    "按住卡片右侧的小圆点拖到另一张卡片上 = 连一条依赖线"
                ),
            )
            menu.addSeparator()
            group_menu = menu.addMenu("分组")
            group_menu.setIcon(group_icon(16))
            existing = {group.id for group in self.groups()
                        if node.document_id in group.document_ids}
            for group in self.groups():
                action = group_menu.addAction(group.title)
                action.setCheckable(True)
                action.setChecked(group.id in existing)
                action.triggered.connect(
                    lambda checked, gid=group.id, did=node.document_id:
                    self.groupMembershipRequested.emit(gid, [did], checked)
                )
            group_menu.addSeparator()
            group_menu.addAction(
                "用选中的事件建一个新组…",
                lambda: self.groupRequested.emit(self.selected_document_ids()
                                                 or [node.document_id]),
            )
            if existing:
                group_menu.addAction(
                    "从这个事件枝的所有组里移出",
                    lambda: [self.groupMembershipRequested.emit(gid, [node.document_id], False)
                             for gid in existing],
                )
            menu.addAction(
                "这个节点改回自动布局",
                lambda: self._reset_position(node.document_id),
            )
            selected_now = self.selected_document_ids()
            if len(selected_now) >= 2 and node.document_id in selected_now:
                arrange = menu.addAction(
                    f"把这 {len(selected_now)} 个事件枝排成网格…",
                    lambda: self.gridArrangeRequested.emit(selected_now, 0),
                )
                arrange.setToolTip("选几行几列；排完不会压到别的卡片上")
                menu.addSeparator()
            state_menu = menu.addMenu("状态")
            state_menu.setIcon(state_icon(node.state, 14))
            for key, label in DOC_STATE_LABELS.items():
                action = state_menu.addAction(state_icon(key, 14), label)
                action.triggered.connect(
                    lambda _checked=False, did=node.document_id, s=key:
                    self.stateRequested.emit(did, s)
                )
            menu.addAction(pencil_icon(16), "改名…",
                           lambda: self.renameRequested.emit(node.document_id))
            delete = menu.addAction(
                prune_icon(16), "删除事件枝…",
                lambda: self.deleteRequested.emit(node.document_id),
            )
            delete.setToolTip("连同年轮记录一起从库里删掉，不能撤销；你的原始文件不动")
        else:
            # 空白处右键：最常用的是"新建事件枝"，放最上面
            menu.addAction(
                branch_plus_icon(16), "新增事件枝",
                lambda: self.newEventRequested.emit(scene_pos.x(), scene_pos.y()),
            )
            menu.addSeparator()
            # 说明 / 分组
            menu.addAction(
                note_icon(16), "在这里加一个说明文本框",
                lambda: self.noteRequested.emit(scene_pos.x(), scene_pos.y()),
            )
            menu.addAction(
                group_icon(16), "在这里新建一个空分组",
                lambda: self.emptyGroupRequested.emit(scene_pos.x(), scene_pos.y()),
            ).setToolTip("先摆好一个框，之后往里拖事件就会自动加入（框会自动变大）")
            menu.addSeparator()
            # 对选中事件的操作
            selected = self.selected_document_ids()
            group_action = menu.addAction(
                group_icon(16),
                f"把选中的 {len(selected)} 个事件枝归为一组…" if selected
                else "把选中的事件归为一组（先框选/Ctrl 点选）",
                lambda: self.groupRequested.emit(self.selected_document_ids()),
            )
            group_action.setEnabled(bool(selected))
            export_action = menu.addAction(
                export_icon(16),
                f"导出选中的 {len(selected)} 个事件枝…" if selected
                else "导出选中的事件枝（先框选/Ctrl 点选）",
                lambda: self.exportSelectionRequested.emit(self.selected_document_ids()),
            )
            export_action.setEnabled(bool(selected))
            export_action.setToolTip("导成文件夹：每个事件枝一个文件夹，里面按环节分")
            arrange_action = menu.addAction(
                grid_icon(16),
                f"把选中的 {len(selected)} 个事件枝排成网格…" if len(selected) >= 2
                else "把选中的事件排成网格（先框选/Ctrl 点选至少两个）",
                lambda: self.gridArrangeRequested.emit(self.selected_document_ids(), 0),
            )
            arrange_action.setEnabled(len(selected) >= 2)
            arrange_action.setToolTip("选几行几列，一次排整齐；不会压到别的卡片")
            menu.addSeparator()
            menu.addAction("全选事件", self.select_all_documents)
            menu.addAction("取消选择", lambda: self._scene.clearSelection())
        # 要重置走工具栏上那个「重置位置…」，而且会先问一句。
        menu.exec(event.globalPos())

    def select_all_documents(self) -> None:
        for item in self._items.values():
            item.setSelected(True)

    def _reset_position(self, document_id: int) -> None:
        # 用 NaN 约定"清掉这个节点的位置"
        self.positionChanged.emit(document_id, float("nan"), float("nan"))

    def _on_selection_changed(self) -> None:
        if self._rebuilding:
            return
        selected = [item for item in self._scene.selectedItems()
                    if isinstance(item, ProjectDocumentItem)]
        document_id = selected[0].node.document_id if selected else None
        self._highlight(document_id)
        if document_id is not None and document_id != self._last_notified:
            self._last_notified = document_id
            self.documentSelected.emit(document_id)
        elif document_id is None:
            self._last_notified = None

    def _highlight(self, document_id: int | None) -> None:
        """选中一张卡片时**只把相关的依赖线画醒目些**。

        （白板本来就是看全局的，变灰反而丢信息）。选中本身有边框高亮够了。
        """
        for edge in self._edge_items:
            hit = document_id is not None and (
                edge.source.node.document_id == document_id
                or edge.target.node.document_id == document_id
            )
            edge._hover = hit
            edge.setZValue(30 if hit else 1)
            edge.update()

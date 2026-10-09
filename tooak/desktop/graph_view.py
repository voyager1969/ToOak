"""时间轴视图：把一份文档 / 一个环节的历史画成流程图。

界面上的「时间轴模式」（工具栏那对「画布 / 时间轴」按钮）就是打开这个视图：
双击版图节点、或在时间轴模式下点事件枝，都会落到这里 —— 每一稿一个节点、
箭头自动连起来，节点上写着这一稿改了什么。

一张图里同时表达四件事
----------------------
1. **时间**：横轴按真实时间比例铺开（密集改动均匀展开，长期搁置真实拉开）；
2. **演化**：相邻版本之间的箭头，箭头上标着「主文件更新 / +2 附件」这类改动摘要；
3. **分支**：同一父版本分出多个子版本时落到不同泳道，分叉关系一眼可见；
4. **回到旧版**：橙色虚线从新版本指回被恢复的旧版本，虚线是「引用历史」而非「继续演化」。

交互
----
* 单击版本卡片 → 选中（右侧详情/差异面板跟着切）；
* 双击 → 用系统默认程序打开该版本的主文件；
* 拖动卡片到桌面/邮件 → 导出该版本（用 ``stage_export`` 硬链接，零拷贝）；
* Ctrl+滚轮缩放，工具栏可「适应窗口」。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QMimeData, QPointF, QRectF, Qt, QUrl, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDrag,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsScene,
    QGraphicsView,
    QStyleOptionGraphicsItem,
    QWidget,
)

from ..core.versiongraph import LANE_H, GraphEdge, GraphNode, VersionGraph
from .event_view import wrap_two_lines
from . import zoom
from .style import (
    branch_pixmap,
    logo_pixmap,
    ACCENT,
    BACKGROUND,
    BORDER,
    EDGE_COLOR,
    EDGE_RESTORE_COLOR,
    MUTED,
    ORIGIN_COLORS,
    TEXT,
    state_colors,
)

__all__ = ["VersionGraphView", "VersionNodeItem"]

#: 版本卡片的尺寸（比原来大一圈：节点上要写清楚"这一稿改了什么"，
CARD_W = 206.0
CARD_H = 124.0

#: 拖出导出时使用的回调签名：(version_id) -> [文件路径]
StageProvider = Callable[[int], list[Path]]


def _elide(text: str, metrics: QFontMetrics, width: float) -> str:
    return metrics.elidedText(text, Qt.TextElideMode.ElideMiddle, int(width))


class VersionNodeItem(QGraphicsItem):
    """流程图上的一个版本卡片。"""

    def __init__(self, node: GraphNode, stage_provider: StageProvider | None = None):
        super().__init__()
        self.node = node
        self._stage_provider = stage_provider
        self._hover = False
        self._press_pos: QPointF | None = None

        self.setPos(node.x, node.y)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)
        self.setToolTip(self._build_tooltip())

    # ------------------------------------------------------------- 几何与绘制

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(-3, -3, CARD_W + 6, CARD_H + 6)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget = None):
        node = self.node
        # 配色按**事件状态**走（进行中/已完成/搁置）——个人管理里只有这一层状态
        border_color, fill_color, text_color = state_colors(node.state)
        selected = self.isSelected()
        current = node.is_current

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(0, 0, CARD_W, CARD_H)

        card_path = QPainterPath()
        card_path.addRoundedRect(rect, 9, 9)

        # 卡片本体
        painter.setBrush(QBrush(QColor("#ffffff")))
        pen = QPen(QColor(ACCENT if selected else border_color))
        pen.setWidthF(2.4 if selected else 1.4)
        painter.setPen(pen)
        painter.drawPath(card_path)

        # 当前版本：左侧留一条状态色竖条，一眼看出「这份文档的现状」
        if current:
            strip = QPainterPath()
            strip.addRoundedRect(QRectF(0, 0, 6, CARD_H), 3, 3)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(QColor(ACCENT)))
            painter.drawPath(strip)

        # 来源小圆点
        painter.setBrush(QBrush(QColor(ORIGIN_COLORS.get(node.origin, MUTED))))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(QPointF(16, 17), 4.2, 4.2)

        # 第一行：版本号 + 时间
        seq_font = QFont(painter.font())
        seq_font.setPointSizeF(11.5)
        seq_font.setBold(True)
        painter.setFont(seq_font)
        painter.setPen(QPen(QColor(TEXT)))
        painter.drawText(QRectF(26, 8, 60, 18), int(Qt.AlignmentFlag.AlignLeft |
                                                   Qt.AlignmentFlag.AlignVCenter), node.label)

        meta_font = QFont(painter.font())
        meta_font.setPointSizeF(8.4)
        meta_font.setBold(False)
        painter.setFont(meta_font)
        painter.setPen(QPen(QColor(MUTED)))
        painter.drawText(
            QRectF(72, 8, CARD_W - 78, 18),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            node.time_text,
        )

        # 主文件名：折两行写清楚（名字比什么都重要）
        name_font = QFont(painter.font())
        name_font.setPointSizeF(9.4)
        painter.setFont(name_font)
        painter.setPen(QPen(QColor(TEXT)))
        metrics = QFontMetrics(name_font)
        name_lines = wrap_two_lines(node.source_name or "（无主文件）", metrics, CARD_W - 24)
        name_y = 27.0
        for index, line in enumerate(name_lines[:2]):
            painter.drawText(
                QRectF(12, name_y + index * 15, CARD_W - 22, 15),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), line,
            )

        # 状态 · 来源/环节 · 文件数 · 大小
        painter.setFont(meta_font)
        painter.setPen(QPen(QColor(text_color)))
        if node.role:
            detail = f"{node.state_label} · {node.role_label}"
        else:
            detail = f"{node.state_label} · {node.origin_label}"
        detail += f" · {node.file_count} 个文件"
        if node.size_text:
            detail += f" · {node.size_text}"
        painter.drawText(QRectF(12, name_y + 32, CARD_W - 22, 15),
                         int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                         _elide(detail, metrics, CARD_W - 24))

        # 改动：这一稿相对上一稿改了什么（节点上直接写明）
        change_font = QFont(painter.font())
        change_font.setPointSizeF(9.0)
        painter.setFont(change_font)
        painter.setPen(QPen(QColor("#1f5f3f" if node.change_summary else MUTED)))
        painter.drawText(
            QRectF(12, name_y + 49, CARD_W - 22, 16),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            _elide(f"改动：{node.change_summary or '—'}", metrics, CARD_W - 24),
        )

        # 说明 / 记录人（有说明就显示说明的第一行）
        extra = ""
        if node.note:
            extra = next((line.strip() for line in node.note.splitlines() if line.strip()), "")
        elif node.author:
            extra = f"记录人：{node.author}"
        if extra:
            painter.setFont(meta_font)
            painter.setPen(QPen(QColor("#7d8896")))
            painter.drawText(
                QRectF(12, name_y + 66, CARD_W - 22, 15),
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                _elide(extra, metrics, CARD_W - 24),
            )

        # 悬停高亮
        if self._hover and not selected:
            highlight = QPen(QColor(ACCENT))
            highlight.setWidthF(1.6)
            highlight.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(highlight)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(-3, -3, 3, 3), 11, 11)

    def _build_tooltip(self) -> str:
        node = self.node
        lines = [
            f"{node.label} · {node.time_text}",
            f"{node.role_label + '：' if node.role else '主文件：'}"
            f"{node.source_name or '—'}（{node.size_text}）",
            f"事件状态：{node.state_label}"
            + (f"｜环节：{node.role_label}" if node.role else f"｜来源：{node.origin_label}"),
        ]
        if node.author:
            lines.append(f"归档人：{node.author}")
        if node.note:
            lines.append(f"说明：{node.note}")
        lines.append("")
        lines.append("单击查看详情 · 双击打开文件 · 拖动到桌面可导出")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 交互

    def hoverEnterEvent(self, event):  # noqa: N802
        self._hover = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):  # noqa: N802
        self._hover = False
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        """按住并拖动 -> 把该版本的文件拖出去（桌面、邮件、另一个应用）。"""
        if self._stage_provider is None or self._press_pos is None:
            super().mouseMoveEvent(event)
            return
        distance = (event.pos() - self._press_pos).manhattanLength()
        if distance < 12:
            super().mouseMoveEvent(event)
            return

        paths = self._stage_provider(self.node.version_id)
        if not paths:
            super().mouseMoveEvent(event)
            return

        drag = QDrag(event.widget() or self.scene().views()[0])
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        mime.setText("\n".join(str(path) for path in paths))
        drag.setMimeData(mime)

        # 拖拽时用卡片自身的渲染结果当缩略图，视觉上更自然
        pixmap = QPixmap(int(CARD_W), int(CARD_H))
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        option = QStyleOptionGraphicsItem()
        self.paint(painter, option, None)
        painter.end()
        drag.setPixmap(pixmap)
        drag.setHotSpot(QPointF(pixmap.width() / 2, pixmap.height() / 2).toPoint())
        drag.exec(Qt.DropAction.CopyAction)

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._press_pos = None
        super().mouseReleaseEvent(event)


class VersionGraphView(QGraphicsView):
    """年轮视图（QGraphicsView + 自绘场景）。"""

    #: 缩放比例变了（百分比）—— 右下角那个控件用它刷新
    zoomChanged = Signal(int)
    #: 选中的版本变化
    versionSelected = Signal(int)
    #: 双击某个版本（主窗口据此用系统默认程序打开）
    versionActivated = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        zoom.prepare_zoomable(self)     # Ctrl+滚轮围绕鼠标缩放
        #: 自适应画布：尺寸一变就重新适应；手动缩放过就不动用户的镜头
        self._auto_fit = True
        self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setBackgroundBrush(QBrush(QColor("#fbfcfd")))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._items: dict[int, VersionNodeItem] = {}
        self._graph: VersionGraph | None = None
        self._stage_provider: StageProvider | None = None
        self._scene.selectionChanged.connect(self._on_selection_changed)
        self._zoom = 1.0
        #: 正在做"适应窗口"：fitInView 会同步触发 resizeEvent，
        #: 不挡住就会 fit → resize → fit … 无限重入（另两个画布都有这道锁）
        self._fitting = False
        #: 视图还没被布局过（尺寸是假的）：等拿到真尺寸再适应一次
        self._pending_fit = False
        #: 程序化选中时抑制信号 —— 否则「选中卡片 → 通知外部 → 外部再选中卡片」
        self._suppress_selection_signal = False

    # -------------------------------------------------------------- 外部接口

    def set_stage_provider(self, provider: StageProvider | None) -> None:
        """注入「拖出时导出文件」的回调（由主窗口提供，避免视图直接做 I/O）。"""
        self._stage_provider = provider

    def set_graph(self, graph: VersionGraph | None) -> None:
        """载入一份文档的版本图并重建场景。"""
        self._graph = graph
        # 先清字典再清场景：scene.clear() 销毁 C++ 图元，
        self._suppress_selection_signal = True
        try:
            self._items.clear()
            self._scene.clear()
        finally:
            self._suppress_selection_signal = False

        if graph is None or not graph.nodes:
            self._draw_placeholder("选择左侧任意一份文档，这里会画出它的年轮")
            return

        self._draw_background(graph)
        self._draw_edges(graph)
        for node in graph.nodes:
            item = VersionNodeItem(node, self._stage_provider)
            self._scene.addItem(item)
            self._items[node.version_id] = item

        self._scene.setSceneRect(
            QRectF(-40, -40, graph.width + 120, graph.height + 120)
        )
        self.select_version(graph.current_version_id)

    def ensure_version_visible(self, version_id: int) -> None:
        """把某个版本卡片滚进可视区（用 ◀ ▶ 在时间轴里走的时候要用）。"""
        item = self._items.get(version_id)
        if item is not None:
            self.ensureVisible(item, 80, 80)

    def select_version(self, version_id: int | None) -> None:
        """让某个版本卡片成为选中态（与树/详情面板同步）。

        这是**程序化**选中，不会回发 ``versionSelected``，
        否则「外部同步 → 发信号 → 外部再同步」会无限递归。
        """
        if version_id is None:
            return
        item = self._items.get(version_id)
        if item is None:
            return
        self._suppress_selection_signal = True
        try:
            self._scene.clearSelection()
            item.setSelected(True)
            self.ensureVisible(item, 80, 80)
        finally:
            self._suppress_selection_signal = False

    def graph(self) -> VersionGraph | None:
        return self._graph

    # ------------------------------------------------------------------ 缩放

    def resizeEvent(self, event):  # noqa: N802
        """画布尺寸一变就自适应（用户手动缩放过就不动他的镜头）。"""
        super().resizeEvent(event)
        if self._pending_fit or getattr(self, "_auto_fit", True):
            self.fit_to_content()           # 首次拿到真尺寸时一定要适应一次

    def fit_to_content(self) -> None:
        """缩放到刚好看见全部内容。

        三重保护，和 ``ProjectBoardView.fit_to_content`` 完全一致 ——
        * **尺寸还是假的**就 fit：实测会算出 0.09 这种荒唐的缩放；
        * **重入**：fitInView 里会同步触发 resizeEvent，栈越堆越深；
        * **滚动条来回**：fit 之后出现滚动条 → 视口变小 → 再 fit，
          结果永远抖（``zoom.begin_stable_fit`` 收掉滚动条就稳了）。
        """
        if not self._items:
            return
        if self.viewport().width() < 160 or self.viewport().height() < 160:
            self._pending_fit = True
            return
        if self._fitting:
            return                              # 重入保护
        self._pending_fit = False
        zoom.begin_stable_fit(self)             # 收掉滚动条，免得来回抖动
        self._fitting = True
        try:
            self.fitInView(self._scene.itemsBoundingRect().adjusted(-30, -30, 30, 30),
                           Qt.AspectRatioMode.KeepAspectRatio)
        finally:
            self._fitting = False
        self._zoom = self.transform().m11()
        self._auto_fit = True
        self.zoomChanged.emit(self.zoom_percent())

    def zoom_in(self) -> None:
        self._apply_zoom(1.2)

    def zoom_out(self) -> None:
        self._apply_zoom(1 / 1.2)

    def zoom_percent(self) -> int:
        """当前缩放百分比（右下角控件显示的就是它）。"""
        return zoom.current_zoom_percent(self)

    def set_zoom_percent(self, percent: float) -> None:
        self._auto_fit = False
        zoom.end_stable_fit(self)       # 手动缩放：滚动条还给用户
        zoom.set_zoom_percent(self, percent)
        self.zoomChanged.emit(self.zoom_percent())

    def is_auto_fit(self) -> bool:
        return self._auto_fit

    def reset_zoom(self) -> None:
        self.resetTransform()
        self._zoom = 1.0
        # 百分比要跟着变：右下角那个控件只认 zoomChanged，
        # 不在这里发一次的话，点"适应/重置"之后数字还停在旧值（用户以为没生效）
        self.zoomChanged.emit(self.zoom_percent())

    def _apply_zoom(self, factor: float) -> None:
        """按倍率缩放，**用 zoom 模块的上下限夹住**。

        以前这里拿绝对的 0.25 / 3.0 去判断并直接 return：
        一旦适应窗口把画面缩到 0.2 倍，``zoom_in`` 算出的目标 0.24 就被判成
        "超范围"直接返回 —— 放大缩小彻底失效，而且**一点反馈都没有**。
        改成先算目标百分比、夹到 MIN_ZOOM/MAX_ZOOM 再设。
        """
        target = max(zoom.MIN_ZOOM, min(zoom.MAX_ZOOM,
                                        self.zoom_percent() * factor))
        zoom.set_zoom_percent(self, target)
        self._zoom = self.transform().m11()
        self.zoomChanged.emit(self.zoom_percent())

    def wheelEvent(self, event):  # noqa: N802
        """Ctrl+滚轮缩放（围绕鼠标）；不按 Ctrl 就是普通滚动。"""
        if zoom.wheel_zoom(self, event):
            self._zoom = self.transform().m11()
            self._auto_fit = False
            self.zoomChanged.emit(self.zoom_percent())
            event.accept()
            return
        super().wheelEvent(event)

    # ------------------------------------------------------------------ 绘制

    def _draw_placeholder(self, message: str) -> None:
        text_item = self._scene.addText(message)
        text_item.setDefaultTextColor(QColor(MUTED))
        font = QFont()
        font.setPointSizeF(11)
        text_item.setFont(font)
        text_item.setPos(24, 24)
        self._scene.setSceneRect(QRectF(0, 0, 520, 120))

    def _draw_background(self, graph: VersionGraph) -> None:
        """泳道分隔线、时间轴与「主线/分支」标注。"""
        width = graph.width + 60
        pen = QPen(QColor(BORDER))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setWidthF(1.0)

        if graph.lane_count > 1:
            for lane in range(graph.lane_count):
                y = graph.nodes[0].y + lane * LANE_H - 18
                self._scene.addLine(0, y, width, y, pen)
                label = self._scene.addText("主线" if lane == 0 else f"分支 {lane + 1}")
                label.setDefaultTextColor(QColor(MUTED))
                font = QFont()
                font.setPointSizeF(8.5)
                label.setFont(font)
                label.setPos(2, y + 2)

        # 时间轴底线
        axis_pen = QPen(QColor(BORDER))
        axis_pen.setWidthF(1.4)
        axis_y = graph.height - 16
        self._scene.addLine(0, axis_y, width, axis_y, axis_pen)

    def _draw_edges(self, graph: VersionGraph) -> None:
        positions = {node.version_id: node for node in graph.nodes}
        labeled: dict[tuple[int, int], int] = {}

        for edge in graph.edges:
            source = positions.get(edge.source_version_id)
            target = positions.get(edge.target_version_id)
            if source is None or target is None:
                continue

            start = QPointF(source.x + CARD_W, source.y + CARD_H / 2)
            end = QPointF(target.x, target.y + CARD_H / 2)
            semantic = edge.kind != "evolution"

            path = QPainterPath(start)
            if semantic:
                # 语义边（回到旧版/合并）：往上方绕一条弧，避免和演化链混淆
                lift = min(90.0, 40.0 + abs(start.x() - end.x()) * 0.18)
                path.cubicTo(
                    QPointF(start.x() + 46, start.y() - lift),
                    QPointF(end.x() - 46, end.y() - lift),
                    end,
                )
                pen = QPen(QColor(EDGE_RESTORE_COLOR))
                pen.setStyle(Qt.PenStyle.DashLine)
            else:
                mid_x = (start.x() + end.x()) / 2
                path.cubicTo(QPointF(mid_x, start.y()), QPointF(mid_x, end.y()), end)
                pen = QPen(QColor(EDGE_COLOR))

            pen.setWidthF(1.7)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            item = self._scene.addPath(path, pen)
            item.setZValue(1)

            # 箭头
            self._add_arrow(end, semantic)

            # 改动摘要标签（**同一条边只画一次**：同一对版本之间既可能是演化边
            # 又可能是"回到旧版"的虚线边，两条都画摘要就叠在一起糊成一片）
            if edge.label:
                key = (edge.source_version_id, edge.target_version_id)
                if labeled.get(key):
                    continue
                labeled[key] = labeled.get(key, 0) + 1
                label = self._scene.addText(edge.label)
                font = QFont()
                font.setPointSizeF(8.0)
                label.setFont(font)
                label.setDefaultTextColor(QColor(EDGE_RESTORE_COLOR if semantic else "#7d8896"))
                mid_x = (start.x() + end.x()) / 2
                mid_y = (start.y() + end.y()) / 2
                if semantic:
                    label.setPos(mid_x - 24, min(start.y(), end.y()) - 34)
                else:
                    label.setPos(mid_x - 26, mid_y - 10)
                label.setZValue(2)

    def _add_arrow(self, tip: QPointF, semantic: bool) -> None:
        size = 7.0
        path = QPainterPath(QPointF(tip.x() - size, tip.y() - size / 1.6))
        path.lineTo(tip)
        path.lineTo(QPointF(tip.x() - size, tip.y() + size / 1.6))
        path.closeSubpath()
        brush = QBrush(QColor(EDGE_RESTORE_COLOR if semantic else EDGE_COLOR))
        item = self._scene.addPath(path, QPen(Qt.PenStyle.NoPen), brush)
        item.setZValue(3)

    # ------------------------------------------------------------------ 事件枝

    def _on_selection_changed(self) -> None:
        if self._suppress_selection_signal:
            return
        selected = [item for item in self._scene.selectedItems()
                    if isinstance(item, VersionNodeItem)]
        if selected:
            self.versionSelected.emit(selected[0].node.version_id)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        item = self.itemAt(event.pos())
        if isinstance(item, VersionNodeItem):
            self.versionActivated.emit(item.node.version_id)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

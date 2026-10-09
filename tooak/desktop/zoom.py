"""画布缩放：**Ctrl+滚轮** + 画布右下角的缩放控件。

为什么不在工具栏上放按钮：用户明确要求过 ——
"画布缩放用 ctrl+滚轮，在右下有比例调整控件，不要做在上面工具栏用按钮的形式"。
缩放是"看着画面顺手调"的动作，控件就该贴着画面（像看图软件、地图那样）。

三个视图（项目树 / 事件枝页 / 时间轴）共用这套：

* :func:`current_zoom_percent` / :func:`set_zoom_percent`：按百分比读写缩放；
* :func:`wheel_zoom`：Ctrl+滚轮缩放（**围绕鼠标位置**，不跑偏）；
* :class:`ZoomControl`：右下角那个小控件（− / 滑块 / + / 百分比 / 适应窗口），
  由主窗口创建、跟着当前视图走。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

__all__ = [
    "MIN_ZOOM",
    "MAX_ZOOM",
    "current_zoom_percent",
    "set_zoom_percent",
    "wheel_zoom",
    "ZoomControl",
    "prepare_zoomable",
    "prepare_space_pan",
    "begin_stable_fit",
    "end_stable_fit",
    "camera",
    "restore_camera",
    "space_pan_key",
    "space_pan_press",
    "space_pan_move",
    "space_pan_release",
    "space_pan_reset",
]

#: 缩放的上下限（百分比）
MIN_ZOOM = 10
MAX_ZOOM = 400

#: Ctrl+滚轮每一格的倍率
WHEEL_STEP = 1.15


def prepare_zoomable(view: QGraphicsView) -> None:
    """让一个 QGraphicsView 支持"围绕鼠标缩放"（Ctrl+滚轮的手感就靠它）。"""
    view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)


# ---------------------------------------------------------------------------
# "适应窗口"要稳：算之前先把滚动条收掉
# ---------------------------------------------------------------------------
#     fit 0.6789 → 出现滚动条 → 视口变小 → resizeEvent → fit 0.6882 →
#     滚动条消失 → 视口变大 → resizeEvent → fit 0.6789 → …**永远来回**，
#     而且是同步重入（fitInView 里就触发了下一次 resize），栈越堆越深。
# 修法很直接：适应窗口时滚动条一律收起 —— "全都看得见"本来也不需要滚动条，
# 视口大小稳定了，适应结果就稳定。用户一旦手动缩放/平移，再把滚动条还给他。


def begin_stable_fit(view: QGraphicsView) -> None:
    """适应窗口之前调：收掉滚动条，让视口大小不随滚动条出现/消失变化。"""
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


def end_stable_fit(view: QGraphicsView) -> None:
    """用户手动缩放或平移了：滚动条还给用户。"""
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)


# ---------------------------------------------------------------------------
# "只是内容变了"的重画：镜头拍一张，画完原样放回去
# ---------------------------------------------------------------------------
# "改完状态 → 刷新当前视图"以前走的是**重新打开**那条路 —— 画布拆掉重画之后
# 又 resetTransform + fit_to_content，用户自己调好的缩放和滚动位置就没了。
# 内容没换地方（同一个项目 / 同一个事件枝），镜头本来就没理由动，
# 所以把镜头做成"拍一张 / 放回去"两个动作，三个画布共用。


def camera(view: QGraphicsView) -> tuple:
    """拍下当前镜头：缩放变换 + 两个滚动条位置 + 现在是不是"自动适应"。

    连 ``_auto_fit`` 一起拍：不还原它的话，重画之后一次窗口 resize
    又会按"自动适应"把画面拉回去（用户只是改了状态）。
    """
    return (view.transform(),
            view.horizontalScrollBar().value(),
            view.verticalScrollBar().value(),
            getattr(view, "_auto_fit", True))


def restore_camera(view: QGraphicsView, shot: tuple | None) -> None:
    """把 :func:`camera` 拍下来的镜头放回去（重画完内容之后调）。

    顺序有讲究：**先** ``setTransform`` 再设滚动条值 —— 反过来先设值，
    缩放一变滚动范围也跟着变，旧值会被夹进新范围，镜头还是会偏。
    """
    if shot is None:
        return
    transform, h_value, v_value, auto_fit = shot
    view.setTransform(transform)
    if hasattr(view, "_zoom"):
        view._zoom = view.transform().m11()
    if hasattr(view, "_auto_fit"):
        view._auto_fit = auto_fit
    view.horizontalScrollBar().setValue(h_value)
    view.verticalScrollBar().setValue(v_value)
    if hasattr(view, "zoomChanged"):
        view.zoomChanged.emit(view.zoom_percent())


# ---------------------------------------------------------------------------
# 平移画布（抓手）：三种手势都行
# ---------------------------------------------------------------------------
# 画布比窗口大是常态（白板上节点能摆得很开），所以要有"抓手"，而且照顾不同习惯：
#   * **空白处按住左键拖**（最直觉）；
#   * **按住中键拖**（哪儿都能拖，CAD / 浏览器地图的老习惯，不挑落点）；
#   * **按住空格 + 左键拖**（Photoshop / Figma 的习惯，触控板没中键时救命）。
# 为什么不用 Qt 自带的 ``ScrollHandDrag``？它连"按在卡片上拖"也当成平移，
# 节点就拖不动了 —— 所以这里只在"中键 / 空格按住 / 落在空白处"时才接管鼠标。


def prepare_space_pan(view: QGraphicsView,
                      cursor: Qt.CursorShape = Qt.CursorShape.ArrowCursor,
                      *, empty_left_pans: bool = True) -> None:
    """初始化抓手状态。

    :param empty_left_pans: 空白处**按住左键**是否也算平移。
        项目白板照 Obsidian 改成"空白处左键拖 = 框选"，所以传 ``False``；
        事件枝页画布没有框选，保持 ``True``（空白处左键拖也能平移）。
    """
    view._space_pan_origin = None
    view._space_pan_cursor = cursor
    view._space_pan_key = False
    view._space_pan_empty_left = empty_left_pans
    # 挂一个"哨兵"事件过滤器（见 _SpacePanGuard 的说明）。
    # 为什么在这里挂而不是让视图自己写 focusOutEvent：视图那边是**别人负责的文件**，
    # 而且"空格卡住"这件事完全是缩放/抓手模块自己的状态，自己收拾最合适。
    previous = getattr(view, "_space_pan_guard", None)
    if previous is not None:               # 同一个画布被重新初始化：先摘掉旧的
        view.removeEventFilter(previous)
    guard = _SpacePanGuard(view)
    view.installEventFilter(guard)
    view._space_pan_guard = guard      # 保住 Python 引用：Qt 不替我们持有它


def _space_pan_set_cursor(view: QGraphicsView) -> None:
    if view._space_pan_origin is not None:
        cursor = Qt.CursorShape.ClosedHandCursor
    elif getattr(view, "_space_pan_key", False):
        cursor = Qt.CursorShape.OpenHandCursor
    else:
        cursor = getattr(view, "_space_pan_cursor", Qt.CursorShape.ArrowCursor)
    view.viewport().setCursor(cursor)


class _SpacePanGuard(QObject):
    """盯着画布：一旦焦点/激活状态丢了，就把"空格按住"清掉。

    为什么必须有它：空格平移只靠 ``KeyPress`` / ``KeyRelease`` 配对，
    而**Alt-Tab 切走时 KeyRelease 根本送不到这个控件** ——
    标志就留成了 True，回来以后左键拖永远是平移，白板上再也框选不了
    （要按空格得先有焦点），所以在这里复位是准确的，不是猜。
    """

    def __init__(self, view: QGraphicsView):
        # 认画布当爹：画布一被销毁，这个哨兵跟着走（生命周期由 Qt 管）
        super().__init__(view)

    def eventFilter(self, obj, event):  # noqa: N802
        if event.type() in (QEvent.Type.FocusOut, QEvent.Type.Hide,
                            QEvent.Type.WindowDeactivate):
            try:
                # 过滤器只装在画布上，所以 obj 就是画布本身 ——
                # 这里刻意**不**在哨兵里存画布的强引用（那会和画布持有的
                # `_space_pan_guard` 形成环，而本项目的循环 GC 是关掉的）
                if (getattr(obj, "_space_pan_key", False)
                        or getattr(obj, "_space_pan_origin", None) is not None):
                    space_pan_reset(obj)
            except RuntimeError:
                pass            # 画布已经被销毁（关窗）：没什么可复位的
        return False            # 只是旁观，事件照常往下走


def space_pan_key(view: QGraphicsView, event) -> bool:
    """空格按住 = 临时抓手；松开就还给画布原来的光标。返回是否吃掉了这个按键。"""
    if event.key() != Qt.Key.Key_Space:
        return False
    view._space_pan_key = event.type() == QEvent.Type.KeyPress
    _space_pan_set_cursor(view)
    return True


def space_pan_press(view: QGraphicsView, event) -> bool:
    """进入平移状态：中键按住（哪儿都行）、空格按住、（可选）左键落在空白处。"""
    button = event.button()
    middle = button == Qt.MouseButton.MiddleButton
    if not middle and button != Qt.MouseButton.LeftButton:
        return False
    hold_space = bool(getattr(view, "_space_pan_key", False))
    if not middle and not hold_space:
        if not getattr(view, "_space_pan_empty_left", True):
            return False        # 这个画布把"空白处左键拖"留给框选
        if view.itemAt(event.position().toPoint()) is not None:
            return False        # 左键按在图元上：那是图元自己的手势，不抢
    view._space_pan_origin = event.position().toPoint()
    _space_pan_set_cursor(view)
    return True


def space_pan_move(view: QGraphicsView, event) -> bool:
    """平移中：跟着鼠标挪滚动条。没在平移就返回 ``False``。"""
    origin = getattr(view, "_space_pan_origin", None)
    buttons = event.buttons()
    if origin is None or not (buttons & (Qt.MouseButton.LeftButton
                                         | Qt.MouseButton.MiddleButton)):
        return False
    point = event.position().toPoint()
    delta = point - origin
    if delta.isNull():
        return True             # 还在平移状态里，只是这一下没动
    view._space_pan_origin = point
    horizontal = view.horizontalScrollBar()
    vertical = view.verticalScrollBar()
    horizontal.setValue(horizontal.value() - delta.x())
    vertical.setValue(vertical.value() - delta.y())
    return True


def space_pan_release(view: QGraphicsView, event=None) -> bool:
    """松手：结束平移，光标恢复成这个画布平时用的样子。"""
    if getattr(view, "_space_pan_origin", None) is None:
        return False
    view._space_pan_origin = None
    _space_pan_set_cursor(view)
    return True


def space_pan_reset(view: QGraphicsView) -> None:
    """把抓手状态整个复位（焦点丢了 / 窗口失活 / 关窗时调）。

    和 :func:`space_pan_release` 的区别：那个只结束"正在拖"这一次，
    保留"空格还按着"；这个连"空格按住"一起清掉 —— 焦点都没了，
    说明用户的手早就离开这个画布了（见 ``_SpacePanGuard``）。
    """
    view._space_pan_origin = None
    view._space_pan_key = False
    _space_pan_set_cursor(view)


def current_zoom_percent(view: QGraphicsView) -> int:
    return int(round((view.transform().m11() or 1.0) * 100))


def set_zoom_percent(view: QGraphicsView, percent: float) -> int:
    """把视图缩放到指定百分比（会被上下限夹住），返回实际值。"""
    percent = max(MIN_ZOOM, min(MAX_ZOOM, float(percent)))
    current = view.transform().m11() or 1.0
    target = percent / 100.0
    if current > 0 and abs(target - current) > 1e-6:
        view.scale(target / current, target / current)
    return current_zoom_percent(view)


def wheel_zoom(view: QGraphicsView, event) -> bool:
    """Ctrl+滚轮缩放。返回 True 表示这次滚轮已经被缩放用掉了。

    没按 Ctrl 就返回 False —— 让滚轮去滚动画面（看大图时这是更常用的操作）。
    """
    if not (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
        return False
    # 横向滚轮（触控板左右滑、带横轮的鼠标）的 angleDelta().y() 是 0，
    # 不挡住的话会被当成"往下滚"= 一直缩小 —— 用户只是横着滑了一下，
    # 画面却缩没了。这种事件不吃，交给普通滚动。
    if event.angleDelta().y() == 0:
        return False
    step = WHEEL_STEP if event.angleDelta().y() > 0 else 1 / WHEEL_STEP
    set_zoom_percent(view, current_zoom_percent(view) * step)
    return True


class ZoomControl(QWidget):
    """画布右下角的缩放控件：− / 滑块 / + / 百分比 / 适应窗口。

    它自己不认识画布，靠 :meth:`bind` 绑定当前视图 ——
    主窗口切视图时重新绑一下，控件就跟着哪个视图走。
    """

    #: 用户按了"适应窗口"
    fitRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._view: QGraphicsView | None = None
        self._slider_dragging = False

        self.setObjectName("zoomControl")
        self.setStyleSheet(
            "#zoomControl{background:rgba(255,255,255,0.92);"
            "border:1px solid #dfe3e8;border-radius:8px;}"
            "#zoomControl QPushButton{border:none;background:transparent;"
            "font-size:13px;color:#1f2733;padding:1px 6px;border-radius:5px;}"
            "#zoomControl QPushButton:hover{background:#e8f1fb;}"
            "#zoomControl QPushButton:disabled{color:#b7c0cb;}"
            "#zoomControl QPushButton:checked{background:#e8f3ec;color:#0e6b3d;"
            "font-weight:600;}"
            "#zoomControl QLabel{color:#4a5666;font-size:11px;}"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 3, 6, 3)
        row.setSpacing(3)

        self.out_button = QPushButton("−", self)
        self.out_button.setFixedWidth(22)
        self.out_button.setToolTip("缩小（Ctrl+滚轮向下）")
        row.addWidget(self.out_button)

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(MIN_ZOOM, MAX_ZOOM)
        self.slider.setFixedWidth(92)
        self.slider.setToolTip("拖动调整比例")
        row.addWidget(self.slider)

        self.in_button = QPushButton("+", self)
        self.in_button.setFixedWidth(22)
        self.in_button.setToolTip("放大（Ctrl+滚轮向上）")
        row.addWidget(self.in_button)

        self.percent_label = QLabel("100%", self)
        self.percent_label.setFixedWidth(38)
        self.percent_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.percent_label.setToolTip("当前比例；点一下回到 100%")
        row.addWidget(self.percent_label)

        self.fit_button = QPushButton("适应", self)
        self.fit_button.setCheckable(True)
        self.fit_button.setToolTip(
            "缩放到刚好看见全部内容（亮着＝自适应：画布尺寸一变就自动适应）"
        )
        row.addWidget(self.fit_button)

        self.out_button.clicked.connect(lambda: self._step(1 / 1.25))
        self.in_button.clicked.connect(lambda: self._step(1.25))
        self.slider.valueChanged.connect(self._on_slider)
        self.slider.sliderPressed.connect(self._on_slider_pressed)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.fit_button.clicked.connect(self._on_fit)
        self.percent_label.mousePressEvent = self._on_percent_click  # type: ignore[method-assign]

        self.setFixedHeight(28)
        self.adjustSize()

    # ---------------------------------------------------------------- 绑定

    def bind(self, view: QGraphicsView | None) -> None:
        if view is self._view:
            self.refresh()
            return
        old = self._view
        self._view = view
        if old is not None:
            try:
                old.zoomChanged.disconnect(self.refresh)
            except (RuntimeError, TypeError):
                pass
        if view is not None and hasattr(view, "zoomChanged"):
            view.zoomChanged.connect(self.refresh)
        self.refresh()

    def view(self) -> QGraphicsView | None:
        return self._view

    # ---------------------------------------------------------------- 刷新

    def refresh(self, *_args) -> None:
        view = self._view
        if view is None:
            return
        percent = current_zoom_percent(view)
        self.slider.blockSignals(True)
        self.slider.setValue(percent)
        self.slider.blockSignals(False)
        self.percent_label.setText(f"{percent}%")
        self.out_button.setEnabled(percent > MIN_ZOOM)
        self.in_button.setEnabled(percent < MAX_ZOOM)
        if hasattr(view, "is_auto_fit"):
            self.fit_button.setChecked(bool(view.is_auto_fit()))

    # ---------------------------------------------------------------- 交互

    def _on_slider_pressed(self) -> None:
        self._slider_dragging = True

    def _on_slider_released(self) -> None:
        self._slider_dragging = False

    def _on_slider(self, value: int) -> None:
        if self._view is None:
            return
        set_zoom_percent(self._view, value)
        self._view.zoomChanged.emit(current_zoom_percent(self._view))

    def _step(self, factor: float) -> None:
        if self._view is None:
            return
        set_zoom_percent(self._view, current_zoom_percent(self._view) * factor)
        self._view.zoomChanged.emit(current_zoom_percent(self._view))

    def _on_percent_click(self, _event) -> None:
        if self._view is None:
            return
        set_zoom_percent(self._view, 100)
        self._view.zoomChanged.emit(current_zoom_percent(self._view))

    def _on_fit(self) -> None:
        self.fitRequested.emit()

"""界面样式与配色。

集中放在一处，方便整体调整观感；版本状态的颜色在流程视图、树、详情面板里共用，
保证「同一个状态在全应用里是同一种颜色」。
"""

from __future__ import annotations

import itertools
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QImage, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap)

from ..resource import resource_path

# ---------------------------------------------------------------------------
# 调色板：沿用旧版 ToOak 图标的设计语言
# ---------------------------------------------------------------------------
# 旧版图标（resources/images/icons/tooak.png）的主色是**深橡木绿 #006030**
# 配黑/灰描边，所以我们整套配色就用这套绿：主色深橡木绿、浅色是嫩叶绿、
# 点缀色用枯叶棕。中性色偏暖一点点，看着比原来的冷灰舒服。

BACKGROUND = "#f4f7f4"      # 页面底：带一点绿的暖白
PANEL = "#ffffff"
BORDER = "#d7e3da"          # 边框：淡绿灰
TEXT = "#1b2a20"            # 正文：近黑，带一点绿
MUTED = "#6b7c72"           # 次要文字：灰绿
ACCENT = "#0e6b3d"          # 主色：深橡木绿（旧图标那个绿）
ACCENT_HOVER = "#12824a"
ACCENT_SOFT = "#e8f3ec"     # 主色的浅底（选中/hover）
ACCENT_LINE = "#9dc4ac"     # 主色的浅描边
#: 选中色：**故意不用主色绿**（绿会和"已完成"的叶子、卡片自己的状态色混在一起），
SELECTION_COLOR = "#2f7ff0"
#: 选中的柔光（画在环外面的一圈半透明，低缩放下也看得见）
SELECTION_GLOW = "#8fb8f8"
ATTENTION_COLOR = "#d9a52b"  # 告警：被依赖的事件更新了（黄叶那个琥珀色）
ATTENTION_TEXT = "#8a6516"   # 告警文字色（琥珀色的深色版，白底上看得清）

#: **事件状态** -> 颜色（边框、底色、文字）。
#: 已完成用叶子，进行中用树枝，搁置用黄色落叶"）：
#:   进行中 = **树枝**（木色）／已完成 = **叶子**（叶绿）／搁置 = **黄色落叶**（秋黄）
DOC_STATE_COLORS: dict[str, tuple[str, str, str]] = {
    "active": ("#2f8f4e", "#e9f6ee", "#1d6b38"),
    "done": ("#b8862b", "#f8f1df", "#7d5a15"),
    # 搁置：秋黄落叶（比"已完成"的棕黄更亮、更偏黄，加上飘落的形状，分得开）
    "on_hold": ("#e0a72c", "#fdf6e0", "#8a6516"),
}

#: 橡果果身的木色（果帽用状态色，果身固定用这个：小尺寸下才看得出是"果子"）
ACORN_NUT = "#8a6a3d"

#: 状态图案的名字（画图案时按这个分）
DOC_STATE_GLYPHS: dict[str, str] = {
    "active": "leaf",       # 叶子：还在长（**新导入、还没转成事件的素材也用叶子**）
    "done": "acorn",        # 橡果：结出果子了 = 做完了
    "on_hold": "fall",      # 黄色落叶（斜着的叶子 + 一点飘落感）
}

def state_colors(state: str) -> tuple[str, str, str]:
    """按事件状态取色（未知状态回落到"进行中"）。

    流程视图、画布方框、侧栏状态列共用它 ——
    保证「同一个状态在全应用里是同一种颜色」。
    """
    return DOC_STATE_COLORS.get(state) or DOC_STATE_COLORS["active"]


#: 版本来源 -> 颜色（用在节点左上角的小圆点）
ORIGIN_COLORS: dict[str, str] = {
    "manual": "#0e6b3d",     # 手工归档：主色
    "watch": "#8e5fc0",
    "import": "#4f9a5f",     # 旧库导入：嫩叶绿
    "restore": "#b98a3c",    # 回到旧版：枯叶棕
    "note": "#5aa9a0",
    "link": "#7d8896",
}

#: 演化边与语义边
EDGE_COLOR = "#a9bcb0"
EDGE_RESTORE_COLOR = "#b98a3c"

FONT_FAMILY = '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "Segoe UI", sans-serif'

APP_STYLESHEET = f"""
QWidget {{
    font-family: {FONT_FAMILY};
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QDialog {{ background: {BACKGROUND}; }}

QToolBar {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    spacing: 6px;
}}
QToolBar QToolButton {{
    padding: 5px 10px;
    border-radius: 6px;
    color: {TEXT};
}}
QToolBar QToolButton:hover {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QToolBar QToolButton:disabled {{ color: #b6bec8; }}
QToolBar::separator {{ background: {BORDER}; width: 1px; margin: 4px 6px; }}

/* 头部那一排（返回 / ◀ ▶ / 状态胶囊）也是 QToolButton，但不在 QToolBar 里，
   上面那条管不到 —— 补一套同样的观感，否则 Windows 上它们渲染成原生按钮，
   和顶栏/侧栏的扁平按钮明显不统一。状态胶囊自己 setStyleSheet，优先级更高，不受影响。 */
#headerBar QToolButton {{
    padding: 4px 8px;
    border-radius: 6px;
    color: {TEXT};
    border: none;
    background: transparent;
}}
#headerBar QToolButton:hover {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
#headerBar QToolButton:disabled {{ color: #b6bec8; }}

/* 侧栏表头下面那排小按钮（新建项目树 / 新建事件枝 / 导入文件…）。
   它们不在 QToolBar 里（那东西会把侧栏顶宽），所以上面的规则管不到 ——
   这里把同一套观感补上，免得侧栏的按钮和顶栏的长得不一样。 */
#sidebarActionBar QToolButton {{
    padding: 4px 7px;
    border-radius: 6px;
    color: {TEXT};
}}
#sidebarActionBar QToolButton:hover {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
#sidebarActionBar QToolButton:pressed {{ background: {ACCENT_LINE}; }}
#sidebarActionBar QToolButton:disabled {{ color: #b6bec8; }}

QStatusBar {{ background: {PANEL}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QToolTip {{
    background: #ffffff; color: {TEXT}; border: 1px solid {ACCENT_LINE};
    padding: 4px 6px; border-radius: 4px;
}}
QStatusBar::item {{ border: none; }}

QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}

QTreeView, QListView {{
    background: {PANEL};
    border: none;
    outline: 0;
    show-decoration-selected: 1;
}}
QTreeView::item, QListView::item {{ padding: 3px 2px; border-radius: 4px; }}
QTreeView::item:hover {{ background: {ACCENT_SOFT}; }}
QTreeView::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QHeaderView::section {{
    background: {PANEL};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 4px 6px;
    color: {MUTED};
    font-size: 12px;
}}

QPlainTextEdit, QTextEdit {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    selection-background-color: {ACCENT_SOFT};
    selection-color: {TEXT};
}}
QLineEdit, QComboBox {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
}}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
/* 日期框（事件页「时间」框里那两个）**必须显式写底色**：它属于 QAbstractSpinBox，
   不写的话底色来自调色板 —— 系统切深色时，卡片是白的、卡片里这个框却是深灰的
   （用户："事件页面的卡底色固定统一，不要随深浅色模式变化"）。 */
QDateEdit, QAbstractSpinBox {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 6px;
}}
QDateEdit:focus, QAbstractSpinBox:focus {{ border-color: {ACCENT}; }}
QPushButton {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 5px 12px;
}}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; background: {ACCENT_SOFT}; }}
QPushButton:default {{ background: {ACCENT}; border-color: {ACCENT}; color: #ffffff; }}
QPushButton:disabled {{ color: #b6bec8; }}

QLabel[role="title"] {{ font-size: 16px; font-weight: 600; }}
QLabel[role="hint"] {{ color: {MUTED}; font-size: 12px; }}
QLabel[role="section"] {{
    color: {MUTED};
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1px;
}}
QLabel[role="state"] {{
    border-radius: 9px;
    padding: 1px 9px;
    font-size: 11px;
}}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 8px;
    margin-top: 14px;
    background: {PANEL};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 4px;
    color: {MUTED};
    font-size: 11px;
}}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #ccd3db; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: #b3bcc7; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #ccd3db; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* 菜单：扁平化 —— 去掉 Fusion 默认那圈立体边框，换成圆角白底 + 主色选中 */
QMenu {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 6px 26px 6px 12px;
    border-radius: 5px;
    color: {TEXT};
}}
QMenu::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
QMenu::item:disabled {{ color: #b6bec8; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

/* 下拉框：去掉 Fusion 那圈下沉按钮，只留一个扁平箭头 */
QComboBox::drop-down {{ border: none; width: 22px; }}

QListWidget::item {{ padding: 5px 6px; }}
QListWidget::item:selected {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
"""

# ---------------------------------------------------------------------------
# 程序图标与装饰（沿用旧版 ToOak 图标的设计语言）
# ---------------------------------------------------------------------------

#: 给"控件级样式表"起名字用的计数器（见 :func:`scoped_style`）
_scoped_style_ids = itertools.count(1)


def scoped_style(widget, declarations: str, *, name: str | None = None) -> None:
    """给**一个控件**套一段样式，并且**把它限定在这个控件自己身上**。

    为什么非限定不可（Windows 上实测到的"提示框黑底"）：Qt 的控件级样式表是
    **连同子控件一起**生效的，而悬浮提示（QToolTip）是挂在"显示这条提示的那个控件"
    下面的一个 QLabel —— 裸声明 ``background: transparent;`` 会把提示框也涂成透明，
    于是它渲染成**一块黑底**（实测：事件枝页文件行的提示框就是这么黑的，
    根子在方框里那个容器上的 ``container.setStyleSheet("background:transparent;")``）。
    带上选择器（``QWidget#<名字> { … }``）只命中这一个控件，提示框不再受影响，
    少一次"样式泄漏"。

    :param name: 想给这个控件起的名字（``None`` 时用现有的 objectName，再没有就自动起一个）
    """
    target = name or widget.objectName() or f"toakStyled{next(_scoped_style_ids)}"
    widget.setObjectName(target)
    widget.setStyleSheet(f"QWidget#{target} {{ {declarations} }}")


# ---------------------------------------------------------------------------
# 主题：**钉死成浅色**，不跟系统的深浅色模式走
# ---------------------------------------------------------------------------
#
# 为什么必须钉：Qt 6.5 起 Windows 的深色模式会给应用一套**深色调色板**，
# 而我们的配色是按浅色设计的（画布、方框、文件卡片这些是用 QPainter 直接画的
# 固定颜色，可**控件**不是 —— 日期框、下拉框、日历、滚动区、提示框、各种对话框
# 都会跟着调色板走）。于是同一张卡片在两种模式下看着是两样东西：卡片底色是白的，
# 卡片**里面**那个日期框却是深灰的。
#
# 钉两层：
# * ``colorScheme``（Qt ≥ 6.8）：告诉 Qt"这个应用就是浅色的"，系统切深色也不改；
# * ``palette``：把每个角色都写死成我们自己的颜色 —— 这是真正决定"控件画成什么色"
#   的那一层，也顺手把上一版修过的"提示框黑底"从根上堵掉。
# 另外接上 ``colorSchemeChanged``：用户在运行期间切系统主题时，Qt 会再塞一套深色
# 调色板进来，这里要把它**再钉一次**（不然"切一次主题，界面就变一次脸"）。


def app_palette() -> QPalette:
    """整套界面用的**固定浅色调色板**（与样式表里的颜色是同一套）。"""
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: BACKGROUND,          # 窗口底（带一点绿的暖白）
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: PANEL,                 # 输入框/列表底：纯白
        QPalette.ColorRole.AlternateBase: BACKGROUND,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: PANEL,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: PANEL,          # 提示框：白底深字（不许变黑）
        QPalette.ColorRole.ToolTipText: TEXT,
        QPalette.ColorRole.PlaceholderText: "#9aa7a0",
        QPalette.ColorRole.Light: PANEL,
        QPalette.ColorRole.Midlight: BACKGROUND,
        QPalette.ColorRole.Mid: BORDER,
        QPalette.ColorRole.Dark: MUTED,
        QPalette.ColorRole.Shadow: "#b6c2ba",
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: ACCENT,
        QPalette.ColorRole.LinkVisited: ACCENT_HOVER,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    # 禁用态也别让系统的深灰盖上来（浅底 + 灰字，与样式表里的 #b6bec8 一致）
    for role, color in ((QPalette.ColorRole.WindowText, "#b6bec8"),
                        (QPalette.ColorRole.Text, "#b6bec8"),
                        (QPalette.ColorRole.ButtonText, "#b6bec8"),
                        (QPalette.ColorRole.Base, BACKGROUND),
                        (QPalette.ColorRole.Button, BACKGROUND)):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor(color))
    return palette


def apply_theme(app) -> None:
    """把整套观感钉在一处：基础风格 + 浅色配色方案 + 调色板 + 样式表。

    启动时调一次（``desktop/app.py``）；测试的 QApplication 也走同一个入口 ——
    否则测试跑的是另一套主题，"深浅色下长什么样"这种事根本测不出来。

    可以重复调用（用户在系统里切了主题时也走它）。
    """
    # Fusion 基础风格：否则 Windows 上没被样式表覆盖的控件（QToolButton、QMenu、
    # 下拉框等）会渲染成原生样式，跟扁平主题不搭。
    app.setStyle("Fusion")
    setter = getattr(app.styleHints(), "setColorScheme", None)   # Qt ≥ 6.8 才有
    if setter is not None:
        try:
            setter(Qt.ColorScheme.Light)
        except (TypeError, ValueError):     # 平台不支持就算了，调色板那层照样管用
            pass
    app.setPalette(app_palette())
    app.setStyleSheet(APP_STYLESHEET)


def watch_color_scheme(app) -> None:
    """系统主题变了就**再钉一次**（幂等，可以重复调用）。

    不接这个信号的话：用户运行期间把 Windows 切成深色，Qt 会塞一套深色调色板进来，
    卡片里的日期框、下拉框、对话框就跟着变深 —— 正是要避免的那件事。
    """
    hints = app.styleHints()
    signal = getattr(hints, "colorSchemeChanged", None)
    if signal is None or getattr(app, "_tooak_scheme_watch", False):
        return
    signal.connect(lambda _scheme: apply_theme(app))
    app._tooak_scheme_watch = True


#: 图标目录（从旧版 ToOak/Symphytum 的 resources/images/icons 里拿过来）。
#: 用 resource_path：打包成 exe 后这些文件在 sys._MEIPASS 里，不能再用 __file__ 拼
ASSETS_DIR = resource_path("tooak", "desktop", "assets")


def app_icon() -> QIcon:
    """程序图标：旧版 ToOak 的那个（128px 与原始大图都在 assets 里）。"""
    icon = QIcon()
    for name in ("tooak128.png", "tooak.png"):
        path = ASSETS_DIR / name
        if path.is_file():
            icon.addFile(str(path))
    return icon


def logo_pixmap(size: int) -> QPixmap:
    """带透明底的程序图标（放在界面里当装饰用）。"""
    path = ASSETS_DIR / "tooak128.png"
    if not path.is_file():
        return QPixmap()
    return QPixmap(str(path)).scaled(
        size, size, Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _draw_acorn(painter: QPainter, size: float, cap: QColor,
                nut: QColor) -> None:
    """在 ``(0,0)-(size,size)`` 里画一颗橡果：果帽 + 果身 + 小柄。

    ``acorn_pixmap``（素材卡片/图标用，见下）和 ``state_glyph`` 的"已完成"
    共用这一份画法 —— 同一颗橡果只画一次，改形状不用改两处。
    """
    w = float(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(nut))
    body = QPainterPath()
    body.moveTo(w * 0.24, w * 0.46)
    body.cubicTo(w * 0.24, w * 0.86, w * 0.42, w * 0.97, w * 0.5, w * 0.97)
    body.cubicTo(w * 0.58, w * 0.97, w * 0.76, w * 0.86, w * 0.76, w * 0.46)
    body.closeSubpath()
    painter.drawPath(body)

    painter.setBrush(QBrush(cap))
    painter.drawRoundedRect(QRectF(w * 0.18, w * 0.30, w * 0.64, w * 0.20),
                            w * 0.10, w * 0.10)
    pen = QPen(cap)
    pen.setWidthF(max(w * 0.09, 1.0))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(w * 0.5, w * 0.30), QPointF(w * 0.5, w * 0.16))


def state_glyph(state: str, size: int = 16, color: str | None = None) -> QPixmap:
    """把状态画成一个小图案：**叶子（进行中）/ 橡果（已完成）/ 黄叶（搁置）**。

    用户改的口径："进行中的事件符号为叶子；已完成的改为橡果" ——
    "长着叶子 = 还在做，结出橡果 = 做完了"，搁置仍是往下飘的黄叶。
    只有颜色区分不开（用户早先反馈"状态颜色区分不明显"），所以形状也要说话。
    """
    key = DOC_STATE_GLYPHS.get(state, "leaf")
    border, _fill, text_color = state_colors(state)
    stroke = QColor(color or border)
    deep = QColor(color or text_color)

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s_ = float(size)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    def leaf_path(length: float, width: float) -> QPainterPath:
        path = QPainterPath(QPointF(0, 0))
        path.cubicTo(QPointF(length * 0.32, -width), QPointF(length * 0.78, -width * 0.72),
                     QPointF(length, 0))
        path.cubicTo(QPointF(length * 0.78, width * 0.72), QPointF(length * 0.32, width),
                     QPointF(0, 0))
        return path

    if key == "branch":
        # 进行中：**左右分叉的枝** —— 主干立着，往上左右各分出一枝（像树在抽条）
        pen = QPen(stroke)
        pen.setWidthF(max(s_ * 0.11, 1.4))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        # 主干：从底部中点直上到分叉点
        painter.drawLine(QPointF(s_ * 0.5, s_ * 0.92), QPointF(s_ * 0.5, s_ * 0.52))
        # 左枝：从分叉点往左上；右枝：往右上（两枝对称分开）
        left = QPainterPath(QPointF(s_ * 0.5, s_ * 0.54))
        left.cubicTo(QPointF(s_ * 0.44, s_ * 0.44), QPointF(s_ * 0.34, s_ * 0.36),
                     QPointF(s_ * 0.18, s_ * 0.16))
        painter.drawPath(left)
        right = QPainterPath(QPointF(s_ * 0.5, s_ * 0.54))
        right.cubicTo(QPointF(s_ * 0.56, s_ * 0.44), QPointF(s_ * 0.66, s_ * 0.36),
                      QPointF(s_ * 0.82, s_ * 0.16))
        painter.drawPath(right)
    elif key == "acorn":
        # 已完成：一颗橡果（果帽用状态色，果身用木色 —— 小尺寸下也看得出是"果子"）
        _draw_acorn(painter, s_, cap=stroke, nut=QColor(ACORN_NUT))
    elif key == "leaf":
        # 叶子：一片立着的叶 + 叶脉
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(stroke))
        painter.save()
        painter.translate(s_ * 0.12, s_ * 0.5)
        painter.rotate(-32)
        painter.drawPath(leaf_path(s_ * 0.82, s_ * 0.26))
        painter.restore()
        pen = QPen(QColor("#ffffff"))
        pen.setWidthF(max(s_ * 0.07, 1.0))
        painter.setPen(pen)
        painter.drawLine(QPointF(s_ * 0.28, s_ * 0.66), QPointF(s_ * 0.74, s_ * 0.30))
    else:
        # 搁置：一片**斜着往下飘**的黄叶（秋叶落下来的样子）
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(stroke))
        painter.save()
        painter.translate(s_ * 0.16, s_ * 0.34)
        painter.rotate(52)
        painter.drawPath(leaf_path(s_ * 0.78, s_ * 0.25))
        painter.restore()
        pen = QPen(deep)
        pen.setWidthF(max(s_ * 0.09, 1.2))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawLine(QPointF(s_ * 0.70, s_ * 0.74), QPointF(s_ * 0.86, s_ * 0.90))
    painter.end()
    return QPixmap.fromImage(image)


def state_icon(state: str, size: int = 16) -> QIcon:
    """状态小图标（列表/下拉框里的那一格）。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(state_glyph(state, size * scale))
    return icon


def branch_pixmap(width: int = 120, height: int = 26, color: str | None = None) -> QPixmap:
    """画一段**橡树枝条**装饰（一根弯枝 + 几片叶子）。

    图标本身是一棵绿色的橡树，这里的枝条就是那套语言的最小重复：
    一根主枝、左右各几片叶，叶尖朝外，配色只用主色系。
    纯代码画出来（不额外依赖美术资源），任意尺寸都清晰。
    """
    color = color or ACCENT_LINE
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    mid = height / 2.0
    stem = QPainterPath(QPointF(2, mid))
    stem.cubicTo(
        QPointF(width * 0.3, mid - height * 0.34),
        QPointF(width * 0.68, mid + height * 0.34),
        QPointF(width - 2, mid),
    )
    pen = QPen(QColor(color))
    pen.setWidthF(1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(stem)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(color)))
    leaf_len = max(height * 0.62, 7.0)
    leaf_w = leaf_len * 0.42
    for index, ratio in enumerate((0.22, 0.44, 0.66, 0.88)):
        point = stem.pointAtPercent(ratio)
        angle = -38.0 if index % 2 == 0 else 38.0
        painter.save()
        painter.translate(point)
        painter.rotate(angle)
        leaf = QPainterPath(QPointF(0, 0))
        leaf.cubicTo(QPointF(leaf_len * 0.35, -leaf_w),
                     QPointF(leaf_len * 0.75, -leaf_w * 0.7),
                     QPointF(leaf_len, 0))
        leaf.cubicTo(QPointF(leaf_len * 0.75, leaf_w * 0.7),
                     QPointF(leaf_len * 0.35, leaf_w),
                     QPointF(0, 0))
        painter.drawPath(leaf)
        painter.restore()
    painter.end()
    return QPixmap.fromImage(image)


def trunk_pixmap(size: int = 18, color: str | None = None) -> QPixmap:
    """画一棵**树干**（项目树列表前面的小图标）。

    一根上细下粗的主干、上面分出两根小杈、底下略微张开 ——
    和"进行中＝树枝""已完成＝叶子"是同一套画法，只用木色。
    """
    wood = QColor(color or DOC_STATE_COLORS["active"][0])
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)

    trunk = QPainterPath()
    trunk.moveTo(w * 0.38, w * 0.94)
    trunk.cubicTo(w * 0.40, w * 0.60, w * 0.44, w * 0.42, w * 0.50, w * 0.12)
    trunk.cubicTo(w * 0.56, w * 0.42, w * 0.60, w * 0.60, w * 0.62, w * 0.94)
    trunk.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(wood))
    painter.drawPath(trunk)

    pen = QPen(wood)
    pen.setWidthF(max(w * 0.09, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    # 两根小杈 + 底下的根
    painter.drawLine(QPointF(w * 0.46, w * 0.42), QPointF(w * 0.24, w * 0.24))
    painter.drawLine(QPointF(w * 0.54, w * 0.36), QPointF(w * 0.76, w * 0.20))
    painter.drawLine(QPointF(w * 0.42, w * 0.93), QPointF(w * 0.22, w * 0.98))
    painter.drawLine(QPointF(w * 0.58, w * 0.93), QPointF(w * 0.78, w * 0.98))
    painter.end()
    return QPixmap.fromImage(image)


def trunk_icon(size: int = 16) -> QIcon:
    """项目树列表用的小图标（树干）。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(trunk_pixmap(size * scale))
    return icon


def panel_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """侧栏开关用的小图标：一个圆角面板 + 中间竖分割线 + 左边三行列表。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.09, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(w * 0.12, w * 0.10, w * 0.76, w * 0.80),
                            w * 0.14, w * 0.14)
    painter.drawLine(QPointF(w * 0.44, w * 0.20), QPointF(w * 0.44, w * 0.80))
    for y in (0.28, 0.50, 0.72):
        painter.drawLine(QPointF(w * 0.22, w * y), QPointF(w * 0.36, w * y))
    painter.end()
    return QPixmap.fromImage(image)


def panel_icon(size: int = 16) -> QIcon:
    """侧栏开关的小图标（面板 + 列表）。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(panel_pixmap(size * scale))
    return icon


def _tool_icon(factory, size: int = 16) -> QIcon:
    """把某个 ``*_pixmap`` 包成 1×/2× 两档的 QIcon（工具栏小图标用，兼顾高分屏）。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(factory(size * scale))
    return icon


def arrow_left_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """左箭头（返回 / 上一稿）：一条横线 + 向左的尖角，线描风格。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.11, 1.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(w * 0.80, w * 0.50), QPointF(w * 0.22, w * 0.50))
    painter.drawLine(QPointF(w * 0.22, w * 0.50), QPointF(w * 0.48, w * 0.24))
    painter.drawLine(QPointF(w * 0.22, w * 0.50), QPointF(w * 0.48, w * 0.76))
    painter.end()
    return QPixmap.fromImage(image)


def arrow_left_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: arrow_left_pixmap(s), size)


def arrow_right_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """右箭头（下一稿）：左箭头的镜像。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.11, 1.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(w * 0.20, w * 0.50), QPointF(w * 0.78, w * 0.50))
    painter.drawLine(QPointF(w * 0.78, w * 0.50), QPointF(w * 0.52, w * 0.24))
    painter.drawLine(QPointF(w * 0.78, w * 0.50), QPointF(w * 0.52, w * 0.76))
    painter.end()
    return QPixmap.fromImage(image)


def arrow_right_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: arrow_right_pixmap(s), size)


def chevron_left_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """左尖角（上一稿）：没有横线，就一个"⟨"，和"返回"的箭头区分开。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.11, 1.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(w * 0.72, w * 0.22), QPointF(w * 0.28, w * 0.50))
    painter.drawLine(QPointF(w * 0.28, w * 0.50), QPointF(w * 0.72, w * 0.78))
    painter.end()
    return QPixmap.fromImage(image)


def chevron_left_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: chevron_left_pixmap(s), size)


def chevron_right_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """右尖角（下一稿）：左尖角的镜像。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.11, 1.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(w * 0.28, w * 0.22), QPointF(w * 0.72, w * 0.50))
    painter.drawLine(QPointF(w * 0.72, w * 0.50), QPointF(w * 0.28, w * 0.78))
    painter.end()
    return QPixmap.fromImage(image)


def chevron_right_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: chevron_right_pixmap(s), size)


def folder_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """文件夹（打开库）：下半主体 + 左上标签，线描。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.09, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(w * 0.10, w * 0.32, w * 0.80, w * 0.52),
                            w * 0.06, w * 0.06)
    tab = QPainterPath(QPointF(w * 0.10, w * 0.34))
    tab.lineTo(QPointF(w * 0.10, w * 0.20))
    tab.lineTo(QPointF(w * 0.36, w * 0.20))
    tab.lineTo(QPointF(w * 0.44, w * 0.34))
    painter.drawPath(tab)
    painter.end()
    return QPixmap.fromImage(image)


def folder_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: folder_pixmap(s), size)


def folder_plus_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """新建项目树：文件夹 + 里面的"＋"（和"打开库"的纯文件夹区分开）。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.09, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(w * 0.10, w * 0.32, w * 0.80, w * 0.52),
                            w * 0.06, w * 0.06)
    tab = QPainterPath(QPointF(w * 0.10, w * 0.34))
    tab.lineTo(QPointF(w * 0.10, w * 0.20))
    tab.lineTo(QPointF(w * 0.36, w * 0.20))
    tab.lineTo(QPointF(w * 0.44, w * 0.34))
    painter.drawPath(tab)
    # 文件夹中间那个"＋"
    painter.drawLine(QPointF(w * 0.40, w * 0.58), QPointF(w * 0.60, w * 0.58))
    painter.drawLine(QPointF(w * 0.50, w * 0.48), QPointF(w * 0.50, w * 0.68))
    painter.end()
    return QPixmap.fromImage(image)


def folder_plus_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: folder_plus_pixmap(s), size)


def import_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """导入文件：一条顶边横线 + 向下的箭头（"落进来"）。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.10, 1.3))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(w * 0.22, w * 0.20), QPointF(w * 0.78, w * 0.20))
    painter.drawLine(QPointF(w * 0.50, w * 0.20), QPointF(w * 0.50, w * 0.74))
    painter.drawLine(QPointF(w * 0.50, w * 0.74), QPointF(w * 0.26, w * 0.50))
    painter.drawLine(QPointF(w * 0.50, w * 0.74), QPointF(w * 0.74, w * 0.50))
    painter.end()
    return QPixmap.fromImage(image)


def import_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: import_pixmap(s), size)


def info_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """说明（帮助）：一个圆 + 里面的"i"（点 + 竖线）。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.09, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QRectF(w * 0.14, w * 0.14, w * 0.72, w * 0.72))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(ink))
    painter.drawEllipse(QPointF(w * 0.50, w * 0.34), w * 0.055, w * 0.055)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.09, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawLine(QPointF(w * 0.50, w * 0.48), QPointF(w * 0.50, w * 0.70))
    painter.end()
    return QPixmap.fromImage(image)


def info_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: info_pixmap(s), size)


def document_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """通用文件（文件图标兜底）：一张带折角的纸。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.09, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    page = QPainterPath(QPointF(w * 0.26, w * 0.10))
    page.lineTo(QPointF(w * 0.64, w * 0.10))
    page.lineTo(QPointF(w * 0.76, w * 0.22))
    page.lineTo(QPointF(w * 0.76, w * 0.90))
    page.lineTo(QPointF(w * 0.26, w * 0.90))
    page.closeSubpath()
    painter.drawPath(page)
    painter.drawLine(QPointF(w * 0.64, w * 0.10), QPointF(w * 0.64, w * 0.22))
    painter.drawLine(QPointF(w * 0.64, w * 0.22), QPointF(w * 0.76, w * 0.22))
    painter.end()
    return QPixmap.fromImage(image)


def document_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: document_pixmap(s), size)


def compare_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """比较：左右两页对照着摆，中间一条虚线（"拿这两份来对"）。

    和 document_pixmap 是同一套线描语言（方框 + 圆头笔），
    只是把"一张纸"变成"两张纸面对面"。
    """
    def draw(p: QPainter, w: float):
        left = QRectF(w * 0.08, w * 0.16, w * 0.34, w * 0.68)
        right = QRectF(w * 0.58, w * 0.16, w * 0.34, w * 0.68)
        for rect in (left, right):
            p.drawRoundedRect(rect, w * 0.06, w * 0.06)
        # 中间断开的两小段 = 虚线（实心一条会看成"分栏"而不是"对照"）
        p.drawLine(QPointF(w * 0.50, w * 0.26), QPointF(w * 0.50, w * 0.44))
        p.drawLine(QPointF(w * 0.50, w * 0.56), QPointF(w * 0.50, w * 0.74))
    return _line_icon(size, color, draw)


def compare_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: compare_pixmap(s), size)


def _line_icon(size: int, color: str | None, draw) -> QPixmap:
    """画一个线描小图标（右键菜单/按钮共用）：开透明画布 + 抗锯齿 + 圆头笔。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.09, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    draw(painter, w)
    painter.end()
    return QPixmap.fromImage(image)


def pencil_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """改名：一支斜放的铅笔（笔身 + 笔尖）。"""
    def draw(p: QPainter, w: float):
        p.drawLine(QPointF(w * 0.24, w * 0.82), QPointF(w * 0.74, w * 0.20))
        p.drawLine(QPointF(w * 0.20, w * 0.86), QPointF(w * 0.24, w * 0.82))
        p.drawLine(QPointF(w * 0.78, w * 0.14), QPointF(w * 0.74, w * 0.20))
    return _line_icon(size, color, draw)


def pencil_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: pencil_pixmap(s), size)


def export_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """导出：一个托盘 + 从中间往右上的箭头。"""
    def draw(p: QPainter, w: float):
        p.drawRoundedRect(QRectF(w * 0.12, w * 0.44, w * 0.62, w * 0.44),
                          w * 0.06, w * 0.06)
        p.drawLine(QPointF(w * 0.34, w * 0.66), QPointF(w * 0.78, w * 0.22))
        p.drawLine(QPointF(w * 0.78, w * 0.22), QPointF(w * 0.58, w * 0.16))
        p.drawLine(QPointF(w * 0.78, w * 0.22), QPointF(w * 0.72, w * 0.40))
    return _line_icon(size, color, draw)


def export_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: export_pixmap(s), size)


def grid_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """排列：2×2 的四个小方格。"""
    def draw(p: QPainter, w: float):
        for cx, cy in ((0.30, 0.30), (0.70, 0.30), (0.30, 0.70), (0.70, 0.70)):
            p.drawRoundedRect(QRectF(cx * w - w * 0.13, cy * w - w * 0.13,
                                     w * 0.26, w * 0.26), w * 0.05, w * 0.05)
    return _line_icon(size, color, draw)


def grid_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: grid_pixmap(s), size)


def group_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """分组：一个圆角大框。"""
    def draw(p: QPainter, w: float):
        p.drawRoundedRect(QRectF(w * 0.12, w * 0.18, w * 0.76, w * 0.64),
                          w * 0.08, w * 0.08)
    return _line_icon(size, color, draw)


def group_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: group_pixmap(s), size)


def note_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """说明框：一张带折角的小纸 + 两行字。"""
    def draw(p: QPainter, w: float):
        page = QPainterPath(QPointF(w * 0.16, w * 0.12))
        page.lineTo(QPointF(w * 0.68, w * 0.12))
        page.lineTo(QPointF(w * 0.84, w * 0.28))
        page.lineTo(QPointF(w * 0.84, w * 0.88))
        page.lineTo(QPointF(w * 0.16, w * 0.88))
        page.closeSubpath()
        p.drawPath(page)
        p.drawLine(QPointF(w * 0.68, w * 0.12), QPointF(w * 0.68, w * 0.28))
        p.drawLine(QPointF(w * 0.68, w * 0.28), QPointF(w * 0.84, w * 0.28))
        for y in (0.46, 0.64):
            p.drawLine(QPointF(w * 0.30, w * y), QPointF(w * 0.70, w * y))
    return _line_icon(size, color, draw)


def note_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: note_pixmap(s), size)


def open_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """打开：一张纸 + 右下往外的小箭头。"""
    def draw(p: QPainter, w: float):
        page = QPainterPath(QPointF(w * 0.14, w * 0.16))
        page.lineTo(QPointF(w * 0.54, w * 0.16))
        page.lineTo(QPointF(w * 0.62, w * 0.24))
        page.lineTo(QPointF(w * 0.62, w * 0.72))
        page.lineTo(QPointF(w * 0.14, w * 0.72))
        page.closeSubpath()
        p.drawPath(page)
        p.drawLine(QPointF(w * 0.54, w * 0.16), QPointF(w * 0.54, w * 0.24))
        p.drawLine(QPointF(w * 0.54, w * 0.24), QPointF(w * 0.62, w * 0.24))
        p.drawLine(QPointF(w * 0.46, w * 0.56), QPointF(w * 0.82, w * 0.84))
        p.drawLine(QPointF(w * 0.82, w * 0.84), QPointF(w * 0.62, w * 0.84))
        p.drawLine(QPointF(w * 0.82, w * 0.84), QPointF(w * 0.82, w * 0.64))
    return _line_icon(size, color, draw)


def open_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: open_pixmap(s), size)


def growth_branch_pixmap(size: int = 20, color: str | None = None) -> QPixmap:
    """**抽象成"＋"的树枝**：用来表示"新增一稿"。

    一根主干往上长（竖），中间左右各抽出一枝（横），三个枝头带一点小芽。
    这样它同时是"＋"（加上）和"枝在长"（继续生长）。
    """
    wood = QColor(color or DOC_STATE_COLORS["active"][0])
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)

    pen = QPen(wood)
    pen.setWidthF(max(w * 0.10, 1.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # 竖：主干，从下往上抽条（略微弯一点，像活枝而不是一条直线）
    stem = QPainterPath(QPointF(w * 0.5, w * 0.9))
    stem.cubicTo(QPointF(w * 0.44, w * 0.66), QPointF(w * 0.56, w * 0.42),
                 QPointF(w * 0.5, w * 0.16))
    painter.drawPath(stem)

    # 横：从主干中间左右各分出一枝（左枝微上、右枝微下，读起来仍是"＋"）
    left = QPainterPath(QPointF(w * 0.5, w * 0.52))
    left.cubicTo(QPointF(w * 0.38, w * 0.48), QPointF(w * 0.26, w * 0.46),
                 QPointF(w * 0.14, w * 0.42))
    painter.drawPath(left)
    right = QPainterPath(QPointF(w * 0.5, w * 0.52))
    right.cubicTo(QPointF(w * 0.62, w * 0.56), QPointF(w * 0.74, w * 0.58),
                  QPointF(w * 0.86, w * 0.62))
    painter.drawPath(right)

    # 三个枝头各带一点小芽（"在长"）
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(wood))
    bud = QPainterPath()
    for cx, cy in ((w * 0.5, w * 0.12), (w * 0.12, w * 0.41), (w * 0.88, w * 0.63)):
        bud.addEllipse(QPointF(cx, cy), w * 0.075, w * 0.075)
    painter.drawPath(bud)
    painter.end()
    return QPixmap.fromImage(image)


def twig_pixmap(size: int = 18, color: str | None = None) -> QPixmap:
    """一小截**枝条**：一根斜枝 + 三片叶（"新增一稿"的图标）。

    和头部那段装饰枝条（``branch_pixmap``）是同一种画法，只是缩到一个 16px 图标的大小：
    主干从右下往左上抽，叶尖朝外，配色沿用主色。
    现在就用一根**实实在在的枝条**。
    """
    stem = QColor(color or ACCENT)
    leaf = QColor(color or ACCENT)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)

    pen = QPen(stem)
    pen.setWidthF(max(w * 0.12, 1.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # 主干：右下 -> 左上
    branch = QPainterPath(QPointF(w * 0.18, w * 0.92))
    branch.cubicTo(QPointF(w * 0.40, w * 0.68), QPointF(w * 0.58, w * 0.44),
                   QPointF(w * 0.86, w * 0.16))
    painter.drawPath(branch)

    # 三片叶：两片朝左上/右上，一片朝右（叶尖朝外，跟装饰枝条一致）
    def leaf_at(x: float, y: float, angle: float, length: float) -> None:
        painter.save()
        painter.translate(w * x, w * y)
        painter.rotate(angle)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(leaf))
        path = QPainterPath(QPointF(0, 0))
        path.cubicTo(QPointF(length * 0.35, -length * 0.42),
                     QPointF(length * 0.75, -length * 0.30),
                     QPointF(length, 0))
        path.cubicTo(QPointF(length * 0.75, length * 0.30),
                     QPointF(length * 0.35, length * 0.42),
                     QPointF(0, 0))
        painter.drawPath(path)
        painter.restore()

    leaf_at(0.34, 0.62, -140, w * 0.42)
    leaf_at(0.56, 0.40, -40, w * 0.40)
    leaf_at(0.70, 0.30, 10, w * 0.34)
    painter.end()
    return QPixmap.fromImage(image)


def twig_icon(size: int = 16) -> QIcon:
    """“新增一稿”用的枝条图标。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(twig_pixmap(size * scale))
    return icon


def _badge_plus(base_factory, size: int, color: str | None) -> QPixmap:
    """画一张底图（左上、略小）+ 右下角一个"＋"（"新建…"那类图标共用）。"""
    ink = QColor(color or MUTED)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    base = base_factory(int(w * 0.66), color)
    painter.drawPixmap(int(w * 0.06), int(w * 0.04), base)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.11, 1.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    cx, cy = w * 0.76, w * 0.76
    painter.drawLine(QPointF(cx - w * 0.10, cy), QPointF(cx + w * 0.10, cy))
    painter.drawLine(QPointF(cx, cy - w * 0.10), QPointF(cx, cy + w * 0.10))
    painter.end()
    return QPixmap.fromImage(image)


def tree_plus_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """新建项目树：树干（树）+ 右下角"＋"。"""
    return _badge_plus(trunk_pixmap, size, color)


def tree_plus_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: tree_plus_pixmap(s), size)


def branch_plus_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """新建事件枝：枝条（枝）+ 右下角"＋"。"""
    return _badge_plus(twig_pixmap, size, color)


def branch_plus_icon(size: int = 16) -> QIcon:
    return _tool_icon(lambda s: branch_plus_pixmap(s), size)


def growth_branch_icon(size: int = 16) -> QIcon:
    """“新增一稿”按钮上的图标（抽象成＋号的枝条）。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(growth_branch_pixmap(size * scale))
    return icon


def prune_shears_pixmap(size: int = 20, color: str | None = None) -> QPixmap:
    """**修枝剪**（所有"删除"动作都用它）。

    在橡树的语言里，"删除"不是销毁而是**修剪**：剪掉这一枝，树还在。
    形状取自园艺修枝剪：上面两片交叉的刀、中间一个转轴、下面两个手柄环。
    刀刃用主色（深橡木绿），手柄用木色，和界面其他图案是一套。
    """
    blade = QColor(color or ACCENT)
    wood = QColor(DOC_STATE_COLORS["active"][0])
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pivot = QPointF(w * 0.5, w * 0.46)

    # 上面两片刀：从转轴向左右上方张开
    pen = QPen(blade)
    pen.setWidthF(max(w * 0.11, 1.5))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for dx in (-1, 1):
        blade_path = QPainterPath(pivot)
        blade_path.cubicTo(
            QPointF(w * (0.5 + 0.10 * dx), w * 0.34),
            QPointF(w * (0.5 + 0.22 * dx), w * 0.22),
            QPointF(w * (0.5 + 0.34 * dx), w * 0.10),
        )
        painter.drawPath(blade_path)

    # 下面两个手柄：往左右下方，末端一个小环
    pen = QPen(wood)
    pen.setWidthF(max(w * 0.10, 1.4))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    for dx in (-1, 1):
        handle = QPainterPath(pivot)
        handle.cubicTo(
            QPointF(w * (0.5 + 0.12 * dx), w * 0.62),
            QPointF(w * (0.5 + 0.20 * dx), w * 0.72),
            QPointF(w * (0.5 + 0.26 * dx), w * 0.78),
        )
        painter.drawPath(handle)
        painter.drawEllipse(QPointF(w * (0.5 + 0.30 * dx), w * 0.86), w * 0.10, w * 0.085)

    # 转轴：一个小实心点
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(blade))
    painter.drawEllipse(pivot, max(w * 0.07, 1.2), max(w * 0.07, 1.2))
    painter.end()
    return QPixmap.fromImage(image)


def warning_pixmap(size: int = 16, color: str | None = None) -> QPixmap:
    """**告警标记**：琥珀色三角 + 感叹号。

    "被依赖的事件出了新稿、依赖它的还没跟" —— 用黄色落叶那一套琥珀色，
    和"搁置"同色系但形状不同（三角感叹号），不会看混。
    """
    amber = QColor(color or "#d9a52b")
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    triangle = QPainterPath()
    triangle.moveTo(w * 0.5, w * 0.08)
    triangle.lineTo(w * 0.97, w * 0.90)
    triangle.lineTo(w * 0.03, w * 0.90)
    triangle.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(amber))
    painter.drawPath(triangle)

    font = QFont(painter.font())
    font.setPixelSize(max(int(w * 0.5), 6))
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QPen(QColor("#5c4310")))
    painter.drawText(image.rect(), int(Qt.AlignmentFlag.AlignCenter), "!")
    painter.end()
    return QPixmap.fromImage(image)


def warning_icon(size: int = 16) -> QIcon:
    """“这个事件枝的依赖更新了”用的图标（琥珀三角）。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(warning_pixmap(size * scale))
    return icon


#: 环节 / 信息框标题前面的小图案：按含义各画一个（都在同一格子里，风格一致）
BOX_GLYPH_KINDS = {
    "input": "arrow_in",      # 输入：箭头进来
    "analysis": "flask",      # 分析：量瓶
    "output": "arrow_out",    # 输出：箭头出去
    "reference": "book",      # 参考数据：书
    "figure": "picture",      # 图示：画框
    "extra": "box",           # 其他文件：盒子
    "note": "quill",          # 说明：笔
    "time": "calendar",       # 时间：日历
    "people": "person",       # 相关人：人
    "progress": "gauge",      # 进度：表盘
}


def box_glyph(key: str, size: int = 14, color: str | None = None) -> QPixmap:
    """

    统一风格：都用**1.4px 圆头细线**画在 ``size × size`` 的格子里（跟界面其他图标
    一样是"橡树语言的线描"），颜色默认主色。
    """
    kind = BOX_GLYPH_KINDS.get(key, "box")
    ink = QColor(color or ACCENT)
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    w = float(size)
    pen = QPen(ink)
    pen.setWidthF(max(w * 0.10, 1.2))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    if kind in ("arrow_in", "arrow_out"):
        # 一个方框 + 一根进/出的箭头
        painter.drawRoundedRect(QRectF(w * 0.10, w * 0.24, w * 0.54, w * 0.52),
                                w * 0.10, w * 0.10)
        y = w * 0.5
        if kind == "arrow_in":
            painter.drawLine(QPointF(w * 0.62, y), QPointF(w * 0.94, y))
            painter.drawLine(QPointF(w * 0.82, y - w * 0.14), QPointF(w * 0.94, y))
            painter.drawLine(QPointF(w * 0.82, y + w * 0.14), QPointF(w * 0.94, y))
        else:
            painter.drawLine(QPointF(w * 0.50, y), QPointF(w * 0.94, y))
            painter.drawLine(QPointF(w * 0.80, y - w * 0.14), QPointF(w * 0.94, y))
            painter.drawLine(QPointF(w * 0.80, y + w * 0.14), QPointF(w * 0.94, y))
    elif kind == "flask":
        # 量瓶：一个下宽上窄的瓶 + 瓶颈
        flask = QPainterPath(QPointF(w * 0.40, w * 0.14))
        flask.lineTo(QPointF(w * 0.60, w * 0.14))
        flask.lineTo(QPointF(w * 0.60, w * 0.42))
        flask.lineTo(QPointF(w * 0.86, w * 0.86))
        flask.lineTo(QPointF(w * 0.14, w * 0.86))
        flask.lineTo(QPointF(w * 0.40, w * 0.42))
        flask.closeSubpath()
        painter.drawPath(flask)
        painter.drawLine(QPointF(w * 0.24, w * 0.66), QPointF(w * 0.76, w * 0.66))
    elif kind == "book":
        # 书：两页 + 书脊
        painter.drawLine(QPointF(w * 0.5, w * 0.24), QPointF(w * 0.5, w * 0.82))
        left = QPainterPath(QPointF(w * 0.5, w * 0.28))
        left.lineTo(QPointF(w * 0.16, w * 0.22))
        left.lineTo(QPointF(w * 0.16, w * 0.76))
        left.lineTo(QPointF(w * 0.5, w * 0.82))
        painter.drawPath(left)
        right = QPainterPath(QPointF(w * 0.5, w * 0.28))
        right.lineTo(QPointF(w * 0.84, w * 0.22))
        right.lineTo(QPointF(w * 0.84, w * 0.76))
        right.lineTo(QPointF(w * 0.5, w * 0.82))
        painter.drawPath(right)
    elif kind == "picture":
        # 画框：方框 + 一座小山 + 一个太阳点
        painter.drawRoundedRect(QRectF(w * 0.12, w * 0.20, w * 0.76, w * 0.60),
                                w * 0.08, w * 0.08)
        mountain = QPainterPath(QPointF(w * 0.18, w * 0.74))
        mountain.lineTo(QPointF(w * 0.40, w * 0.46))
        mountain.lineTo(QPointF(w * 0.54, w * 0.64))
        mountain.lineTo(QPointF(w * 0.64, w * 0.54))
        mountain.lineTo(QPointF(w * 0.82, w * 0.74))
        painter.drawPath(mountain)
        painter.setBrush(QBrush(ink))
        painter.drawEllipse(QPointF(w * 0.32, w * 0.36), w * 0.06, w * 0.06)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif kind == "box":
        # 盒子：方框 + 一条封口线
        painter.drawRoundedRect(QRectF(w * 0.14, w * 0.30, w * 0.72, w * 0.50),
                                w * 0.09, w * 0.09)
        cover = QPainterPath(QPointF(w * 0.14, w * 0.44))
        cover.lineTo(QPointF(w * 0.86, w * 0.44))
        painter.drawPath(cover)
        painter.drawLine(QPointF(w * 0.5, w * 0.30), QPointF(w * 0.5, w * 0.44))
    elif kind == "quill":
        # 笔：一根斜羽管 + 笔尖
        painter.drawLine(QPointF(w * 0.22, w * 0.80), QPointF(w * 0.78, w * 0.22))
        feather = QPainterPath(QPointF(w * 0.52, w * 0.50))
        feather.cubicTo(QPointF(w * 0.66, w * 0.30), QPointF(w * 0.84, w * 0.24),
                        QPointF(w * 0.86, w * 0.16))
        feather.cubicTo(QPointF(w * 0.72, w * 0.20), QPointF(w * 0.56, w * 0.34),
                        QPointF(w * 0.52, w * 0.50))
        painter.drawPath(feather)
    elif kind == "calendar":
        # 日历：本子 + 两个挂环 + 一条横线
        painter.drawRoundedRect(QRectF(w * 0.14, w * 0.24, w * 0.72, w * 0.58),
                                w * 0.09, w * 0.09)
        painter.drawLine(QPointF(w * 0.14, w * 0.42), QPointF(w * 0.86, w * 0.42))
        painter.drawLine(QPointF(w * 0.34, w * 0.16), QPointF(w * 0.34, w * 0.30))
        painter.drawLine(QPointF(w * 0.66, w * 0.16), QPointF(w * 0.66, w * 0.30))
    elif kind == "person":
        # 人：一个头 + 肩膀
        painter.drawEllipse(QPointF(w * 0.5, w * 0.34), w * 0.16, w * 0.16)
        shoulders = QPainterPath(QPointF(w * 0.20, w * 0.84))
        shoulders.cubicTo(QPointF(w * 0.22, w * 0.60), QPointF(w * 0.78, w * 0.60),
                          QPointF(w * 0.80, w * 0.84))
        painter.drawPath(shoulders)
    else:                                  # gauge 进度
        painter.drawArc(QRectF(w * 0.14, w * 0.20, w * 0.72, w * 0.72),
                        int(200 * 16), int(140 * 16))
        painter.drawLine(QPointF(w * 0.5, w * 0.56), QPointF(w * 0.68, w * 0.34))
        painter.setBrush(QBrush(ink))
        painter.drawEllipse(QPointF(w * 0.5, w * 0.56), w * 0.06, w * 0.06)
    painter.end()
    return QPixmap.fromImage(image)


def box_icon(key: str, size: int = 14) -> QIcon:
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(box_glyph(key, size * scale))
    return icon


def acorn_pixmap(size: int = 18, color: str | None = None,
                 cap_color: str | None = None) -> QPixmap:
    """**橡果**：白板上"素材卡片"的标记（文件、网页链接）。

    事件枝卡片用的是"树枝 / 叶子 / 黄叶"表达状态；素材卡片是"还没归属的东西"，
    所以给它一颗橡果（果帽 + 果身 + 小尖），一眼就能和事件区分开。
    """
    nut = QColor(color or ACORN_NUT)                         # 木色的果身
    cap = QColor(cap_color or ACCENT)                        # 橡木绿的果帽
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    _draw_acorn(painter, float(size), cap, nut)
    painter.end()
    return QPixmap.fromImage(image)


def leaf_pixmap(size: int = 16, color: str | None = None,
                filled: bool = False) -> QPixmap:
    """一片**空心叶子**（新导入、还没转成事件的素材卡片用它）。

    同一个"还在长"的意思，但一眼能看出"这张还没归属、还没转成事件"。
    """
    tint = QColor(color or ACCENT)
    if filled:
        return state_glyph("active", size, tint.name())
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    s_ = float(size)
    pen = QPen(tint)
    pen.setWidthF(max(s_ * 0.09, 1.0))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.save()
    painter.translate(s_ * 0.12, s_ * 0.5)
    painter.rotate(-32)
    leaf = QPainterPath(QPointF(0, 0))
    leaf.cubicTo(QPointF(s_ * 0.26, -s_ * 0.26), QPointF(s_ * 0.64, -s_ * 0.19),
                 QPointF(s_ * 0.82, 0))
    leaf.cubicTo(QPointF(s_ * 0.64, s_ * 0.19), QPointF(s_ * 0.26, s_ * 0.26),
                 QPointF(0, 0))
    painter.drawPath(leaf)
    painter.drawLine(QPointF(s_ * 0.14, 0), QPointF(s_ * 0.74, 0))
    painter.restore()
    painter.end()
    return QPixmap.fromImage(image)


def leaf_icon(size: int = 16) -> QIcon:
    """素材卡片用的叶子图标。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(leaf_pixmap(size * scale))
    return icon


def acorn_icon(size: int = 16) -> QIcon:
    """「素材卡片」用的图标（橡果）。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(acorn_pixmap(size * scale))
    return icon


def prune_icon(size: int = 16) -> QIcon:
    """“删除 = 修剪”用的图标（修枝剪）。"""
    icon = QIcon()
    for scale in (1, 2):
        icon.addPixmap(prune_shears_pixmap(size * scale))
    return icon

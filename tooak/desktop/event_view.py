"""事件枝页：一块**自己排版**的画布。

取值自旧版 ToOak / Symphytum 的做法
-----------------------------------
旧版的表单视图（``views/formview`` + ``components/formlayoutmatrix``）是这样干的：

* 视图切成**格子**（``widthUnitPx`` × ``heightUnitPx``），每个字段占 N×M 格；
* 字段可以被**拖动**到别的格子，也可以被**拉伸**（选中后右侧 / 下方出现小圆点把手）；
* 位置与大小按字段存下来，下次打开还是你排的样子；
* 视图**不画任何自动推出来的线** —— 关系由数据决定，布局由用户决定。

这个事件枝页照搬这套：每个方框 = 旧版的一个"字段"，方框之间不画自动箭头。

::

    ┌ 输入文件 ┐ ┌ 分析资料 ┐ ┌ 参考数据 ┐ ┌ 图示 ┐ ┌ 输出文件 ┐ ┌ 其他文件 ┐
    └──────────┘ └──────────┘ └──────────┘ └──────┘ └──────────┘ └──────────┘
    ┌ 时间 ─────┐ ┌ 相关人 ┐ ┌ 进度 / 状态 ┐ ┌ 说明 ────┐
    └───────────┘ └────────┘ └─────────────┘ └──────────┘

框体视图（全库共用一个模板）
----------------------------
* 画布是**正方形的格子**（``UNIT × UNIT``），方框占 N×M 格；
* 用户在这里把方框挪好、拉好（拖标题栏挪位、拖把手改大小），
  **这份排版就是"框体视图"**：所有项目、所有事件下的节点都按它来排；
* 模板只有一份、按库存（``box_template`` 表），改一个方框 = 改模板里的那一个，
  别的方框不受影响，但**所有事件**下次打开都是这份排版；
* 共同的外观（标题栏高度、内边距、字号、列表行高）也是同一处定义。

操作
----
* 点一下方框 = 选中，四周出现三个把手；
* **拖标题栏** = 把方框挪到别的格子（自动吸附到格子上，不会歪）；
* **拖右中 / 下中 / 右下角的把手** = 改这一个方框的宽 / 高 / 宽高；
* 右键空白处 = 「整理排版」/ 视图缩放；右键方框 = 添加文件 / 看这个环节的年轮
* 位置**按事件存库**，大小按库记着，下次打开就是这个样子；
* 方框装不下的文件不会外溢，框里会写「还有 N 个没显示」，把方框改大就看得见。

为什么不做自动箭头
------------------
看起来像在替用户规定做事顺序 —— 而这是个人资料管理，顺序是他自己的事。
和项目树同一条原则：**线要么是数据里的关系，要么是用户自己画的**，不自动推。
"""

from __future__ import annotations

import html
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import (QDate, QEvent, QFileInfo, QPoint, QPointF, QRectF,
                            QSizeF, Qt, Signal)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QIcon,
    QFontMetrics,
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QCalendarWidget,
    QComboBox,
    QDateEdit,
    QFileIconProvider,
    QFrame,
    QGraphicsItem,
    QGraphicsProxyWidget,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QStyleOptionGraphicsItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core import Library
from ..core.library import ROLE_LABELS
from ..core.diffing import summarize_change
from ..core.util import human_size, to_local_str
from . import zoom
from .style import (ACCENT, ACCENT_LINE, ACCENT_SOFT, BORDER, BOX_GLYPH_KINDS,
                    MATCH_FILL, MATCH_INK, MUTED, TEXT, box_glyph, branch_pixmap,
                    compare_icon, document_icon, export_icon, folder_icon,
                    import_icon, info_icon, leaf_icon, logo_pixmap, open_icon,
                    pencil_icon, prune_icon, scoped_style, state_colors, twig_icon)
from .widgets import (SELECTION_RING_PAD, crisp_pixmap,
                      paint_selection_ring)

__all__ = [
    "EventStageView",
    "StageBoxItem",
    "InfoBoxItem",
    "FileRow",
    "FileEntry",
    "UNIT",
    "DEFAULT_UNITS_W",
    "DEFAULT_UNITS_H",
    "MIN_UNITS_W",
    "MAX_UNITS_W",
    "card_name_lines",
]

#: 格子（沿用旧版 FormLayoutMatrix 的"单位格"思路）：位置与大小都按格算。
#: **正方形格子**：横竖一格一样大，方框占 N×M 格（宽高可以不一样）。
#: 50px：够细（拖动/拉伸能落在 50px 的格上），又不至于让吸附变得"粘不住"。
UNIT = 50.0
#: 每隔几格画一条"主格线"，看着不至于是一团密网（也省绘制）
MAJOR_EVERY = 4
#: 格子里再留一点缝，免得相邻方框贴在一起
CELL_GAP = 12.0
#: 默认排法一行占几格（6 格的方框 = 一行 5 个，正好是 5 个环节）
GRID_COLS = 30
#: 画布底色（跟白板一致）
CANVAS_BG = "#fbfcfa"
#: 细格线 / 粗格线（橡树色系的浅绿灰，不再用旧的蓝灰）
GRID_MINOR = "#e8efe9"
GRID_MAJOR = "#d6e3da"
#: 空框里的虚线占位
PLACEHOLDER_LINE = "#cfe0d5"
MARGIN = 26.0

#: 方框的格数：**每个方框各自一份**（宽、高分开），存在全库共用的框体模板里。
#: * 默认 6×6 格（= 6×50−12 = 288px 的正方形，和以前一样大，只是格更细）；
#: * 最小 4×4 格（188px）；信息框里是真控件，最小 5×3 格（238×138px）；
#: * 最大 16×16 格（788px）。
MIN_UNITS_W, MIN_UNITS_H = 4, 4
MAX_UNITS_W, MAX_UNITS_H = 16, 16
DEFAULT_UNITS_W, DEFAULT_UNITS_H = 6, 6

HEADER_H = 34.0     # 标题栏高度（也是"拖这里可以挪动方框"的区域）
CARD_H = 38.0       # 文件卡片高度：够放两行文件名
CARD_FONT_PT = 8.6  # 列表字体（统一模板的一部分）
CARD_PAD = 24.0     # 列表左右内边距（内容宽度 = 方框宽 − 这个数）
ROW_H = 38.0        # 文件列表每一行的高度（名字一行 + 入库时间一行，紧凑）
THUMB = 28.0        # 缩略图边长（图片文件直接显示缩略图）

#: 缩略图缓存：同一个文件块只解码一次（按 sha256 记，换文件自然失效）
_THUMB_CACHE: dict[str, "QPixmap"] = {}
GRIP_R = 6.0        # 把手小圆点半径
GRIP_HIT = 12.0     # 把手的点击半径（屏幕像素；比看得见的圆点大一圈，好按）

#: 环节顺序（只用来决定"默认排法"的先后，不画任何连线）
FLOW_ROLES = ["input", "analysis", "reference", "figure", "output"]
#: 除这五个之外的角色（说明 / 主文件 / 附件）收进「其他文件」方框
EXTRA_ROLE = "extra"
#: 信息框（就在画布上编辑，改完即存库）
INFO_KEYS = ["time", "people", "progress", "note"]

FIELD_DATE_MIN = QDate(1900, 1, 1)   # 日期控件里代表"未设置"的哨兵值
_ROLE_EMPTY = "把文件拖到这里"
#: 图示框里只放一张图 —— 这些后缀算"能渲染的图片"
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif",
                  ".tiff", ".svg", ".ico")

#: 文件行自己拖出来的 MIME 类型：``版本 id:文件 id``。
#: 为什么要单独一个格式：文件行拖出去本来是"把这一份交给桌面/邮件"（带的是
#: **文件路径**），而拖到**另一个环节方框**上要的是"把这一行换个环节"。
#: 两种拖放的目标不一样，光看 urls 分不出来 —— 而且如果按"拖进来一个文件路径"
#: 处理，同一份文件会被**再归档一遍**（同一稿里多出一个同名入口）。
#: 所以文件行拖动时同时挂上这个格式：本视图见到它就当"内部改环节"，
#: 别的地方（资源管理器 / 邮件客户端）见不到、也不受影响。
FILE_ROW_MIME = "application/x-tooak-file-row"


def encode_file_row(version_id: int, file_id: int) -> bytes:
    """把"哪一稿的哪一行"编成 MIME 数据（:data:`FILE_ROW_MIME` 的载荷）。"""
    return f"{int(version_id)}:{int(file_id)}".encode("utf-8")


def decode_file_row(data: bytes) -> tuple[int, int] | None:
    """解析 :data:`FILE_ROW_MIME` 的载荷 → ``(版本 id, 文件 id)``；坏了返回 ``None``。"""
    try:
        version_text, _, file_text = bytes(data).decode("utf-8", "replace").partition(":")
        return int(version_text), int(file_text)
    except (TypeError, ValueError):
        return None


def is_image_name(name: str) -> bool:
    """按扩展名判断是不是图片（图示框只认图片）。"""
    return str(name).lower().endswith(IMAGE_SUFFIXES)


def format_archived_at(stamp: str, *, with_time: bool = False) -> str:
    """

    没有时间就写"—"。
    """
    if not stamp:
        return "—"
    text = to_local_str(stamp, "%Y-%m-%d %H:%M")
    return text or "—"


def one_line(text: str) -> str:
    """把一段可能多行的文字压成一行（连续空白折成一个空格）。

    年轮列表的每一行只有一行高度，而"说明"框里的文字可能是好几段：
    压成一行才塞得下（原文留在悬停提示里，一个字都不丢）。
    """
    return " ".join(str(text or "").split())


def _elide(text: str, metrics: QFontMetrics, width: float) -> str:
    return metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(width))


def wrap_two_lines(text: str, metrics: QFontMetrics, width: float) -> list[str]:
    """把文件名折成最多两行；第二行还放不下才省略。

    旧库里的文件名很长（``20250706综合检查与资料整理总结-提交版.docx``），
    只给一行的话全是省略号，看不出是哪一稿，所以宁可折两行。
    """
    if width <= 0:
        return [text]
    if metrics.horizontalAdvance(text) <= width:
        return [text]
    cut = len(text)
    while cut > 1 and metrics.horizontalAdvance(text[:cut]) > width:
        cut -= 1
    first, rest = text[:cut], text[cut:]
    if not rest:
        return [first]
    return [first, _elide(rest, metrics, width)]


def card_name_lines(text: str, metrics: QFontMetrics, card_width: float,
                    size_width: float = 0.0) -> tuple[list[str], bool]:
    """卡片里的文件名怎么排：尽量两行放全。

    先按"右边还留着文件大小"的宽度排；如果这样要被省略号截掉，
    就把文件大小让出去、用整行宽度再排一次 —— 名字比大小重要。
    返回 ``(两行文字, 还要不要画文件大小)``。
    """
    for reserve, keep_size in ((size_width, True), (0.0, False)):
        # 8 = 卡片左侧内边距（和 paint 里画的时候保持一致）
        available = max(card_width - 8 - reserve, 40)
        lines = wrap_two_lines(text, metrics, available)
        if not lines[-1].endswith("…"):
            return lines, keep_size
    return lines, False


def _same_deliverable(left: str, right: str) -> bool:
    """两个文件名是不是"同一份交付物的不同叫法"。

    判据两条（任一成立就算）：
    * 同名（大小写不敏感）—— 绝大多数情况；
    * 去掉日期 / 版本号 / "提交·终版·定稿"这类噪音之后仍然很像
      （``util.name_similarity`` ≥ ``library.NAME_MATCH_FLOOR``）——
      用户的习惯就是每稿改名字，用这一条才不会把"改了名的同一份东西"
      又当成"缺了一份"补一行灰的出来。

    这是**界面用**的粗判：真配对（要拿去比 Word 的那一对）仍然走内核
    ``Library.counterpart_file`` 那套更严的规则。
    """
    first, second = str(left or ""), str(right or "")
    if not first or not second:
        return False
    if first.strip().casefold() == second.strip().casefold():
        return True
    from ..core.util import name_similarity

    from ..core.library import NAME_MATCH_FLOOR
    return name_similarity(first, second) >= NAME_MATCH_FLOOR


@dataclass
class FileEntry:
    """文件列表里一行要显示的全部信息。"""

    file_id: int
    version_id: int
    name: str
    size: int
    sha256: str
    mime: str = ""
    #: 这一行**所属的那一稿**是第几稿。``stale`` 的行属于更早的那一稿，
    #: 改它名字就是在改那一稿记的名字，所以菜单上必须把稿号写出来
    version_seq: int = 0
    #: 入库时间（这一稿是什么时候归档进库的）
    created_at: str = ""
    source_path: str = ""
    #: ``""`` 正常 / ``"missing"`` 文件不在了 / ``"changed"`` 被就地改过
    status: str = ""
    #: True = 这一稿没有登记它（卡片显示的是它最后一次登记的位置）
    stale: bool = False
    #: 未登记时卡片上写的那句话。**要分清是哪一稿没登记**：正在看的就是最新稿时
    #: "我可能是在把 V1 提到 V2，而 V3 又有文件了"，两个意思不能混。
    stale_note: str = "最新稿未登记"
    #: True = 这份文件是**复制进库**的（库内 blob），不在用户自己的目录里。
    #: 界面上要据此换一句话：库内文件的"所在文件夹"是 ToOak 自己的 blobs 目录，
    #: 对用户没意义，该打开的是它**导入时所在的那个目录**。
    in_library: bool = False


# ---------------------------------------------------------------------------
# 文件卡片
# ---------------------------------------------------------------------------


def is_image(entry: FileEntry) -> bool:
    """是不是图片：优先看 mime，其次看扩展名（旧数据里 mime 可能为空）。"""
    if entry.mime.startswith("image/"):
        return True
    return entry.name.lower().rsplit(".", 1)[-1] in (
        "png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff", "svg",
    )


#: 系统图标提供者（按扩展名缓存，别每个文件都去问一遍系统）
_ICON_PROVIDER = None
_ICON_CACHE: dict[str, object] = {}


def file_icon(entry: FileEntry, path) -> "QIcon | None":
    """取这个文件的**默认图标**（系统怎么显示，这里就怎么显示）。

    图片会优先用缩略图；其它文件（pdf / docx / xlsx / zip…）用系统给的图标，
    取不到就退回通用的文件图标，绝不空着。
    """
    global _ICON_PROVIDER
    if path is not None and Path(path).exists():
        try:
            suffix = Path(path).suffix.lower() or "<none>"
        except Exception:
            suffix = "<none>"
        cached = _ICON_CACHE.get(suffix)
        if cached is not None:
            return cached
        try:
            if _ICON_PROVIDER is None:
                _ICON_PROVIDER = QFileIconProvider()
            icon = _ICON_PROVIDER.icon(QFileInfo(str(path)))
            if not icon.isNull():
                _ICON_CACHE[suffix] = icon
                return icon
        except Exception:
            pass
    return None


#: 缩略图闸门：超过这些上限就**不解码**，直接退回系统图标。
#: 事件枝页是在 UI 线程里画卡片的，一张 6000×6000 的 PNG 全解要 0.64 秒 ——
#: 打开一个事件枝就是卡半秒起，多几张就明显"卡住了"。缩略图只有 28px 大，
#: 为它解一张比屏幕大几百倍的图完全不划算。
THUMB_MAX_PIXELS = 4000 * 4000
THUMB_MAX_BYTES = 8 * 1024 * 1024


def thumbnail_for(path, key: str) -> "QPixmap | None":
    """取一张缩略图（带缓存）。读不出来 / 太大就返回 None，调用方画个通用图标。"""
    if path is None:
        return None
    try:
        resolved = str(Path(path))
    except Exception:
        return None
    cached = _THUMB_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        if Path(resolved).stat().st_size > THUMB_MAX_BYTES:
            return None                       # 文件本身就有 8MB 以上，别在 UI 线程上解
    except OSError:
        return None
    reader = QImageReader(resolved)
    # 有些格式要读完头部才知道尺寸；尺寸读不到就当"解不出来"退回图标
    size = reader.size()
    if not size.isValid() or size.width() * size.height() > THUMB_MAX_PIXELS:
        return None
    # **让解码器直接按小尺寸解**：QPixmap(path) 是"全解出来再 SmoothTransformation"，
    # 大图上白白做了几百倍的解码与缩放（6000×6000 实测 0.64 秒）
    reader.setScaledSize(
        size.scaled(int(THUMB), int(THUMB), Qt.AspectRatioMode.KeepAspectRatio)
    )
    image = reader.read()
    if image.isNull():
        return None
    scaled = QPixmap.fromImage(image)
    if len(_THUMB_CACHE) > 400:       # 别无限涨
        _THUMB_CACHE.clear()
    _THUMB_CACHE[key] = scaled
    return scaled


class FileRow(QWidget):
    """文件列表里的一行。

    旧版 ToOak/Symphytum 的文件字段就是一个带滚动条的列表控件
    （``FilesFormWidget`` 用了表格/列表），这里照那个做：
    装不下就出滚动条，而不是像以前那样把多出来的行藏起来。

    每行两小行，紧凑：
    * 第一行：文件名（显示不下就省略号，完整名字在悬停提示里）；
    * 第二行：**入库时间** · 大小 · 指纹（缺文件 / 内容变过会标出来）；
    * 左边一格：图片显示缩略图，其它文件显示系统默认图标；
    * 鼠标移上去有高亮，点一下（或弹右键菜单）保持高亮选中态，双击打开文件。
    """

    def __init__(self, view: "EventStageView", entry: FileEntry,
                 parent: QWidget | None = None,
                 box: "StageBoxItem | None" = None):
        super().__init__(parent)
        self.view = view
        self.entry = entry
        #: 这一行属于**哪一个方框**（= 哪个环节）。
        #: 右键菜单必须用它来算 role：以前 show_file_menu 里写的是
        #: ``box = box or self._boxes.get("extra")``，而调用方没传 box，
        #: 于是「输入文件」里的卡片右键显示"添加文件到「其他文件」"，
        #: 点"添加文件到「输入文件」…"也把文件归到「其他文件」去了。
        self.box = box
        # 兼容原来的字段名（旧代码/测试还在用）
        self.name = entry.name
        self.size = entry.size
        self.sha256 = entry.sha256
        self.missing = entry.status
        self.stale = entry.stale
        self._press_pos = None
        self._hover = False
        self._selected = False

        self.setObjectName("fileRow")
        self.setFixedHeight(int(ROW_H))
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)   # 悬停高亮
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.setToolTip(self._build_tooltip())
        self.setAutoFillBackground(False)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 2, 6, 2)
        row.setSpacing(7)

        self.thumb = QLabel(self)
        self.thumb.setFixedSize(int(THUMB), int(THUMB))
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._fill_thumb()
        row.addWidget(self.thumb)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        self.name_label = QLabel(self.name, self)
        self.name_label.setProperty("role", "filename")
        self.name_label.setTextFormat(Qt.TextFormat.PlainText)
        scoped_style(
            self.name_label,
            f"font-size:{CARD_FONT_PT + 0.4:.1f}pt;"
            + ("color:#6b7684;" if self.stale else ""),
            name="fileName",
        )
        text.addWidget(self.name_label)
        self.apply_search("")

        meta_bits: list[str] = []
        if self.missing == "missing":
            meta_bits.append("⚠ 文件缺失")
        # 改动是控在用户手里的" —— 文件被改过是常态，不值得在这行挂警示。
        if self.stale:
            meta_bits.append(self.entry.stale_note or "未登记")
        meta_bits.append("入库 " + format_archived_at(entry.created_at))
        meta_bits.append(human_size(self.size))
        self.meta_label = QLabel(" · ".join(meta_bits), self)
        scoped_style(self.meta_label, "font-size:8.0pt;color:#6b7684;", name="fileMeta")
        text.addWidget(self.meta_label)
        row.addLayout(text, 1)

        # 子控件不参与鼠标事件：点击 / 双击 / 右键都归这一行处理
        for child in (self.thumb, self.name_label, self.meta_label):
            child.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    # ---------------------------------------------------------------- 状态

    def set_selected(self, value: bool) -> None:
        if self._selected != value:
            self._selected = value
            self.update()

    def is_selected(self) -> bool:
        return self._selected

    def enterEvent(self, event):  # noqa: N802
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def event(self, event):  # noqa: N802
        # WA_Hover：鼠标压在子控件上时也要保持整行高亮
        if event.type() == QEvent.Type.HoverEnter:
            self._hover = True
            self.update()
        elif event.type() == QEvent.Type.HoverLeave:
            self._hover = False
            self.update()
        return super().event(event)

    def paintEvent(self, event):  # noqa: N802
        """选中 = 蓝底；悬停 = 浅蓝底（不画别的，文字交给子控件）。"""
        background = None
        if self._selected:
            background = QColor("#d8e8fa")
        elif self._hover:
            background = QColor("#eef6ea")      # 悬停：嫩叶绿
        if background is not None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(background))
            painter.drawRoundedRect(self.rect().adjusted(0, 1, -1, -1), 5, 5)
            painter.end()
        super().paintEvent(event)

    # ---------------------------------------------------------------- 缩略图

    def _fill_thumb(self) -> None:
        """左边那一格：图片显示缩略图，其它文件显示**系统默认图标**。"""
        path = self.view.file_path(self.entry.version_id, self.entry.file_id)
        if is_image(self.entry):
            pixmap = thumbnail_for(path, self.entry.sha256 or f"{self.entry.file_id}")
            if pixmap is not None:
                self.thumb.setPixmap(pixmap)
                self.thumb.setToolTip("双击打开图片")
                return
        icon = file_icon(self.entry, path)
        if icon is not None:
            self.thumb.setPixmap(icon.pixmap(int(THUMB), int(THUMB)))
            return
        # 兜底：自己画的通用文件图标（折角纸），保持和其它线描图标一个风格
        self.thumb.setPixmap(document_icon(int(THUMB) - 4).pixmap(int(THUMB) - 4,
                                                              int(THUMB) - 4))

    def apply_search(self, term: str) -> None:
        """把搜索命中的那几个字在**文件名**上标出来。


        这一处是 QLabel，所以用富文本标（`<span>` 加底色），而不是自己去 paint：
        这一行的字号 / 颜色都挂在样式表上，重画一遍等于把那套设置抄一份，
        抄完就等着以后对不上。每一段都先 `html.escape` —— `&` / `<` 这类字符
        不会把标签撑坏。

        ``highlight.match_spans`` 统一算（与左边那棵树、与内核的拆词规则同一套），
        这里只负责把区间拼成 HTML。
        """
        from .highlight import match_spans

        needle = str(term or "").strip()
        spans = match_spans(self.name, needle) if needle else []
        if not spans:
            self.name_label.setTextFormat(Qt.TextFormat.PlainText)
            self.name_label.setText(self.name)
            return
        parts: list[str] = []
        cursor = 0
        for start, end in spans:
            if start > cursor:
                parts.append(html.escape(self.name[cursor:start]))
            parts.append(
                f'<span style="background-color:{MATCH_FILL};color:{MATCH_INK};">'
                f"{html.escape(self.name[start:end])}</span>"
            )
            cursor = end
        if cursor < len(self.name):
            parts.append(html.escape(self.name[cursor:]))
        self.name_label.setTextFormat(Qt.TextFormat.RichText)
        self.name_label.setText("".join(parts))

    def _build_tooltip(self) -> str:
        notes = {
            "missing": "⚠ 这个文件已经不在原来的位置了（内容取不回来）",
            # 没有 "changed"：文件被改过是常态，不值得在提示里念叨
        }.get(self.missing, "双击打开 · 右键更多操作")
        if self.stale:
            notes = (
                f"{self.entry.stale_note or '这一稿没有登记这个文件'}，\n"
                "这里显示的是它最后一次被登记时的位置。\n"
                "（右键「复制一份到这一稿…」可以把它登记进来）\n" + notes
            )
        else:
            notes += "\n按住拖到别的环节方框 = 改它属于哪个环节（可 Ctrl+Z 撤销）"
            notes += "\n按住拖到桌面/邮件 = 导出这一份"
        return (
            f"{self.name}\n"
            f"入库时间：{format_archived_at(self.entry.created_at, with_time=True)}\n"
            f"{human_size(self.size)}\n{notes}"
        )

    # ---------------------------------------------------------------- 交互

    def _show_menu(self, pos) -> None:
        self.view.select_file_row(self)      # 右键也保持"选中"高亮
        # 把自己所属的方框一起传过去：菜单里的 role（归档到哪个环节）由它决定
        self.view.show_file_menu(self.entry, self.mapToGlobal(pos), self.box)

    def contextMenuEvent(self, event):  # noqa: N802
        """右键这一行 = **文件自己的菜单**（打开 / 打开所在文件夹 / 导出 / 改环节…）。

        **真实点击走不到这里**（实测）：行是嵌在图元代理里的控件，Qt 只把右键事件
        发给代理最外层那个控件（``QScrollArea``）就为止了，**不再往下分给行**——
        把事件直接发到 ``QScrollArea`` / 内层容器上，行一个字都收不到、什么都不弹。
        所以真实路径是 ``EventStageView.contextMenuEvent`` 按落点补发
        （见 ``file_row_at``）。
        这个方法留着是因为"事件真的送到这一行"时它必须给出文件菜单：
        直接把事件发给行、或发给行里的文件名标签，都会到这里并弹出文件菜单（实测）。
        鼠标事件不受这条限制：单击选中、双击打开都是真事件直达行（实测）。
        """
        self._show_menu(event.pos())
        event.accept()

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        self.view.fileOpenRequested.emit(self.entry.version_id, self.entry.file_id)
        event.accept()

    def mousePressEvent(self, event):  # noqa: N802
        # 只有**左键**才改选中：右键要留给"保持多选 → 弹批量菜单"，
        # 右键按下也会冒到这一行来，若在这里无条件 select_file_row 就会把
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_pos = event.position().toPoint()
        modifiers = event.modifiers()
        self.view.select_file_row(
            self,
            toggle=bool(modifiers & Qt.KeyboardModifier.ControlModifier),
            extend=bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
        )
        if not (modifiers & (Qt.KeyboardModifier.ControlModifier
                             | Qt.KeyboardModifier.ShiftModifier)):
            # 普通单击：右下比较窗口只显示这一份文件 vs 上一稿（不是整稿）
            panel = getattr(self.view, "timeline_panel", None)
            if panel is not None:
                panel.show_file_diff(self.entry.version_id, self.entry.file_id,
                                     self.entry.name)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        """按住往外拖：**拖到别的环节方框 = 改环节**，拖到桌面/邮件 = 导出这一份。

        要拖过 12 像素才算拖动（``manhattanLength() < 12``）：不设这道门槛的话，
        手指在行上抖一下就会开始拖放，**单击选中 / 双击打开**都会被抢走。
        """
        from PySide6.QtCore import QMimeData, QUrl
        from PySide6.QtGui import QDrag, QPixmap

        if self._press_pos is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return
        if (event.position().toPoint() - self._press_pos).manhattanLength() < 12:
            super().mouseMoveEvent(event)
            return
        paths = self.view.stage_file(self.entry.version_id, self.entry.file_id)
        if not paths:
            super().mouseMoveEvent(event)
            return

        drag = QDrag(self)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(path)) for path in paths])
        mime.setText("\n".join(str(path) for path in paths))
        # 再挂一个"这是事件枝页里的一行文件"的标记：落回本视图的环节方框上时，
        # 要办的是"把这行换个环节"，不是"把这份文件再归档一遍"（见 FILE_ROW_MIME）
        mime.setData(FILE_ROW_MIME, encode_file_row(self.entry.version_id,
                                                     self.entry.file_id))
        drag.setMimeData(mime)
        if self.thumb.pixmap() is not None and not self.thumb.pixmap().isNull():
            drag.setPixmap(self.thumb.pixmap())
        else:
            drag.setPixmap(QPixmap(0, 0))
        # 拖放期间给视图挂个"还在拖"的标志：视图可能正排着一次"改环节之后的刷新"，
        # 那一刷新会把这一行删掉，而拖动循环还在跑（见 begin_file_row_drag 的说明）。
        # try/finally：拖放中途抛异常（比如用户按了 Esc 之外的东西）也要把标志清掉。
        self.view.begin_file_row_drag()
        try:
            drag.exec(Qt.DropAction.CopyAction)
        finally:
            self.view.end_file_row_drag()

    def mouseReleaseEvent(self, event):  # noqa: N802
        self._press_pos = None
        super().mouseReleaseEvent(event)


class _BoxBase(QGraphicsItem):
    """画布上的一个方框：位置与大小都按"格子"算，可以自己拖、自己拉。

    标题栏里有一个**符合含义的小图案**（输入=箭头进来、分析=量瓶、输出=箭头出去、
    参考=书、图示=画框、其他=盒子、说明=笔、时间=日历、相关人=人、进度=表盘）。
    """

    #: 标题图标用哪个图案（子类按 key 覆盖，见 BOX_GLYPH_KINDS）
    glyph_key = "box"

    def __init__(self, key: str, label: str, view: "EventStageView",
                 state: str = "active"):
        super().__init__()
        self.key = key
        self.label = label
        self.view = view
        self.state = state
        self.hint = ""
        self._drag_hover = False
        self.col = 0
        self.row = 0
        #: 这个方框自己的格数（宽、高分开）——每个方框可以不一样
        self.units_w = DEFAULT_UNITS_W
        self.units_h = DEFAULT_UNITS_H
        self._press_pos: QPointF | None = None
        self.setZValue(5)
        self.setAcceptHoverEvents(True)
        # 装不下的卡片不许溢到框外面去
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemClipsChildrenToShape, True)

    # ---------------------------------------------------------------- 几何

    def box_width(self) -> float:
        return self.units_w * UNIT - CELL_GAP

    def box_height(self) -> float:
        return self.units_h * UNIT - CELL_GAP

    def box_size(self) -> float:
        """较短的那条边（列表/缩略图按它算宽度时用）。"""
        return min(self.box_width(), self.box_height())

    def boundingRect(self) -> QRectF:  # noqa: N802
        # 选中时会在方框外面画一圈"粗蓝环 + 柔光"（最远 6px），必须一起算进来：
        # 少了这块，拖动/拉伸选中的方框时旧位置会留下蓝影
        # （tools/check_painting.py 会把这类"画出界"自动扫出来）
        pad = SELECTION_RING_PAD
        return QRectF(-pad, -pad, self.box_width() + pad * 2,
                      self.box_height() + pad * 2)

    def shape(self) -> QPainterPath:  # noqa: N802
        """**命中判定只用方框本身**，不含外圈留的余量。

        boundingRect 为了让选中环不被裁掉，比方框大了 8px。默认的
        ``shape()`` 就等于 boundingRect，于是相邻方框的"余量"互相压着：
        点缝隙里那几像素会打中旁边的方框，空白拖动画布也找不到落脚点
        """
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, self.box_width(), self.box_height()),
                            12.0, 12.0)
        return path

    def min_units(self) -> tuple[int, int]:
        """这个方框能被拉到多小（信息框里有真控件，需要的地方会覆盖它）。"""
        return MIN_UNITS_W, MIN_UNITS_H

    def set_units(self, units_w: int, units_h: int) -> None:
        """设置这个方框自己的格数（只动它一个）。"""
        min_w, min_h = self.min_units()
        units_w = max(min_w, min(MAX_UNITS_W, int(units_w)))
        units_h = max(min_h, min(MAX_UNITS_H, int(units_h)))
        if (units_w, units_h) == (self.units_w, self.units_h):
            return
        self.prepareGeometryChange()
        self.units_w, self.units_h = units_w, units_h
        self.after_resize()

    def set_cell(self, col: int, row: int) -> None:
        col = max(0, int(col))
        row = max(0, int(row))
        self.col, self.row = col, row
        self.setPos(
            self.view.cell_rect(col, row, self.units_w, self.units_h).topLeft()
        )

    def after_resize(self) -> None:
        """大小变了之后的收尾（子类各自处理内容）。"""

    def is_selected(self) -> bool:
        """这个方框现在是"被选中"的吗。

        激活变成蓝框"）：就算 ``view.selected`` 还指在它身上，也不画那圈蓝环。
        这样锁布局时，正在选中/误点的方框会立刻退掉高亮。
        """
        return self.view.selected is self and self.view.edit_layout

    def set_drag_hover(self, value: bool) -> None:
        """拖文件到这个方框上方时的提示（**不是**鼠标悬停）。

        "鼠标放在节点上其实不需要有整体 hover 的动画"，所以就留了一个
        拖放提示：只有真的拖着文件悬在方框上方时才亮一下边框底色。
        """
        if self._drag_hover != value:
            self._drag_hover = value
            self.update()

    # ---------------------------------------------------------------- 绘制

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget: QWidget = None) -> None:
        rect = QRectF(0, 0, self.box_width(), self.box_height())
        selected = self.is_selected()

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 卡片底 + 细描边（跟白板上的卡片一套观感：白底、浅绿灰边、圆角）
        painter.setBrush(QBrush(QColor("#f4faf6" if self._drag_hover else "#ffffff")))
        pen = QPen(QColor(ACCENT_LINE if not (self._drag_hover or selected)
                          else ACCENT))
        pen.setWidthF(2.0 if (self._drag_hover or selected) else 1.2)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 12, 12)

        # 扁平标题栏：不画浅绿底、不画状态色条，标题和内容之间只留一条极淡分隔线
        painter.setPen(QPen(QColor(BORDER)))
        painter.drawLine(QPointF(0, HEADER_H), QPointF(rect.width(), HEADER_H))

        font = QFont(painter.font())
        font.setPointSizeF(10.0)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(TEXT)))         # 标题用近黑，比主色绿更扁平
        # 用 crisp_pixmap：按当前缩放生成对应像素数的图，放大看也不糊
        glyph = crisp_pixmap(lambda px: box_glyph(self.glyph_key, px), 15.0, painter,
                             name=f"box:{self.glyph_key}")
        text_left = 14.0
        if not glyph.isNull():
            painter.drawPixmap(QPointF(14.0, (HEADER_H - 15) / 2), glyph)
            text_left = 14.0 + 15 + 6
        painter.drawText(
            QRectF(text_left, 0, rect.width() - text_left - 12, HEADER_H),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.label,
        )

        if selected:
            paint_selection_ring(painter, rect, 12.0)

        hint = self.header_hint()
        if hint:
            meta = QFont(painter.font())
            meta.setPointSizeF(8.4)
            meta.setBold(False)
            painter.setFont(meta)
            painter.setPen(QPen(QColor(MUTED)))
            painter.drawText(
                QRectF(12, 0, rect.width() - 24, HEADER_H),
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                hint,
            )


    # ---------------------------------------------------------------- 交互

    def header_hint(self) -> str:
        """标题栏右边的提示：拉把手时显示格数，否则显示这个方框自己的状态提示。

        选中之后走 resize_hint()，而它没在拉伸时返回空串 —— 于是**一选中提示就没了**
        （"5 个文件"这类信息只在没选中时看得见）。改成"有格数就显示格数，
        没有就退回自己的提示"。
        """
        return self.view.resize_hint(self) or self.hint

    def hoverEnterEvent(self, event):  # noqa: N802
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):  # noqa: N802
        # 鼠标划走只是把"抓手"光标去掉，不动任何外观
        self.unsetCursor()
        super().hoverLeaveEvent(event)

    def hoverMoveEvent(self, event):  # noqa: N802
        if event.pos().y() <= HEADER_H:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):  # noqa: N802
        view = self.view
        self._press_pos = event.pos()
        # 只有**左键**才是"拖标题栏挪方框"：右键要留给右键菜单，
        # 右键按住再挪会连着弹菜单，方框也不知不觉被挪走了（对照视图里的写法）
        if event.button() == Qt.MouseButton.LeftButton and event.pos().y() <= HEADER_H:
            if not view.edit_layout:
                # 布局锁着：拖标题栏不动方框，只提醒一句怎么开（也不选中 ——
                view.statusMessage.emit(
                    "布局已锁定：方框不会跟着鼠标动（要点工具栏的「编辑布局」才能挪）"
                )
                event.accept()
                return
            view.select_box(self)
            # 拖标题栏 = 挪动方框（拖动卡片是"导出"，两个手势不能混）
            view.begin_move(self, event.scenePos())
            event.accept()
            return
        # 点方框主体：**只有布局开着才选中**（锁着时点一下不该亮蓝框）
        if view.edit_layout:
            view.select_box(self)
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        view = self.view
        if view.moving is not None and view.moving[0] is self:
            view.update_move(event.scenePos())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        view = self.view
        if view.moving is not None and view.moving[0] is self:
            view.finish_move()
            event.accept()
            return
        self._press_pos = None
        super().mouseReleaseEvent(event)


class StageBoxItem(_BoxBase):
    """一个环节的方框：里面是一个**带滚动条**的文件列表，文件拖进来就进这个环节。"""

    def __init__(self, role: str, label: str, state: str, view: "EventStageView"):
        super().__init__(f"role:{role}", label, view, state)
        self.role = role
        # 标题前面那个小图案：输入=箭头进来、分析=量瓶、输出=箭头出去、
        # 参考=书、图示=画框、其他=盒子
        self.glyph_key = role if role in BOX_GLYPH_KINDS else "box"
        self.files: list[FileRow] = []
        self.proxy: QGraphicsProxyWidget | None = None
        self.scroll: QScrollArea | None = None
        self.container: QWidget | None = None
        self.rows_layout: QVBoxLayout | None = None

    # ---------------------------------------------------------------- 内容

    def _build_list(self) -> None:
        """建一个带滚动条的列表（照旧版文件字段那种列表控件）。"""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:9px;margin:2px;}"
            "QScrollBar::handle:vertical{background:#c3cedb;border-radius:4px;min-height:24px;}"
            "QScrollBar::handle:vertical:hover{background:#a9b7c8;}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
            "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        )
        container = QWidget()
        # **限定在容器自己身上**：裸写 background:transparent 会连子控件一起透明，
        # 而文件行的悬浮提示是挂在这一行的子控件 —— 于是提示框变成一块黑底
        # （Windows 上实测，见 style.scoped_style 的说明）
        scoped_style(container, "background: transparent;", name="stageFileRows")
        rows_layout = QVBoxLayout(container)
        rows_layout.setContentsMargins(2, 0, 4, 2)
        rows_layout.setSpacing(2)
        rows_layout.addStretch(1)
        scroll.setWidget(container)

        # 拖放要交给画布（视图）处理：里面的控件一律不接拖放，
        # 否则"把文件拖进方框"会被列表控件吃掉
        scroll.setAcceptDrops(False)
        scroll.viewport().setAcceptDrops(False)
        container.setAcceptDrops(False)

        self.scroll = scroll
        self.container = container
        self.rows_layout = rows_layout
        self.proxy = self.view.add_widget(scroll)
        self.proxy.setParentItem(self)
        self.proxy.setAcceptDrops(False)
        self.layout_content()

    def set_files(self, entries: list[FileEntry]) -> None:
        if self.scroll is None:
            self._build_list()
        assert self.rows_layout is not None
        for row in self.files:
            # 视图可能还记着"当前选中的行"：那行马上要被删掉，
            # 不先摘掉引用的话，之后有人调 set_selected() 就是在摸已析构的对象
            # （PySide6 会抛 "Internal C++ object already deleted"）
            if self.view._selected_row is row:
                self.view._selected_row = None
            self.view._selected_rows.discard(row)
            if self.view._selection_anchor is row:
                self.view._selection_anchor = None
            row.setParent(None)
            row.deleteLater()
        self.files = []
        # 新行插在 stretch 之前
        for index, entry in enumerate(entries):
            row = FileRow(self.view, entry, self.container, box=self)
            self.rows_layout.insertWidget(index, row)
            self.files.append(row)
        self.layout_content()

    def layout_content(self) -> None:
        """把列表铺在标题栏下面（方框是正方形的，列表自己出滚动条）。"""
        if self.proxy is not None:
            self.proxy.setGeometry(
                QRectF(8, HEADER_H + 4, self.box_width() - 16,
                       max(self.box_height() - HEADER_H - 12, 40.0))
            )

    def after_resize(self) -> None:
        self.layout_content()

    def _is_empty(self) -> bool:
        """这个环节框里有没有东西（图示框覆盖它：它没有"行"，只有一张图）。"""
        return not self.files

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget: QWidget = None) -> None:
        super().paint(painter, option, widget)
        if not self._is_empty():
            return
        # 空框：里面画一个**虚线占位框** + 一句提示（比一行灰字好看，也像"待填"）
        area = QRectF(12, HEADER_H + 8, self.box_width() - 24,
                      max(self.box_height() - HEADER_H - 20, 20))
        pen = QPen(QColor(PLACEHOLDER_LINE))
        pen.setWidthF(1.2)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(area, 8, 8)
        font = QFont(painter.font())
        font.setPointSizeF(9.0)
        painter.setFont(font)
        painter.setPen(QPen(QColor(MUTED)))
        painter.drawText(area, int(Qt.AlignmentFlag.AlignCenter), _ROLE_EMPTY)


class FigureBoxItem(StageBoxItem):
    """**图示**框：只放一张图，直接渲染出来（拖新的进来自动替换）。

    现在直接把图画在图元里，并且按**当前缩放换算成设备像素**再缩放一次源图，
    所以任何缩放下都是清的；小图不放大（原尺寸居中显示），大图缩到框里。

    只认**一张**图（归档用 replace 语义），不是文件列表；空框仍然写着"把一张图拖进来"。
    """

    #: 图四周留白
    PADDING = 10.0
    #: 文件名那一行的高度
    CAPTION_H = 20.0

    def __init__(self, role: str, label: str, state: str, view: "EventStageView"):
        super().__init__(role, label, state, view)
        self.entry: FileEntry | None = None
        self._source: QPixmap | None = None
        self._message = ""
        self._fallback: QIcon | None = None
        self._cache: tuple | None = None      # (设备像素宽, 高, 缩放后的图)
        self._too_many = 0

    # ---------------------------------------------------------------- 内容

    def set_files(self, entries: list[FileEntry]) -> None:
        """只取第一张（库里若有历史遗留的多张，显示第一张并说明）。"""
        self.entry = entries[0] if entries else None
        self._too_many = max(len(entries) - 1, 0)
        self._source = None
        self._fallback = None
        self._message = ""
        self._cache = None
        entry = self.entry
        if entry is not None:
            path = self.view.file_path(entry.version_id, entry.file_id)
            if entry.status == "missing" or path is None or not Path(path).exists():
                self._message = "文件已不在原处"
                self._fallback = QFileIconProvider().icon(
                    QFileInfo(entry.source_path or entry.name)
                )
            elif is_image_name(entry.name):
                pixmap = QPixmap(str(path))
                if pixmap.isNull():
                    self._message = "这张图渲染不出来（格式可能不受支持）"
                else:
                    self._source = pixmap
            else:
                self._message = "不是图片，没法渲染"
                self._fallback = QFileIconProvider().icon(
                    QFileInfo(str(path))
                )
        self.setToolTip(self._tooltip())
        self.layout_content()
        self.update()

    def _tooltip(self) -> str:
        if self.entry is None:
            return "把一张图拖进来（只放一张，拖新的会把旧的换掉）"
        lines = ["图示（只放一张）", self.entry.name]
        if self._source is not None:
            lines.append(f"原图 {self._source.width()} × {self._source.height()} 像素")
        if self._message:
            lines.append("⚠ " + self._message)
        if self._too_many:
            lines.append(f"库里这个环节还存着 {self._too_many} 个旧图示文件（只显示第一张）")
        lines += ["", "双击 = 打开这张图", "把新的图拖进来 = 换掉这张"]
        return "\n".join(lines)

    def _is_empty(self) -> bool:
        return self.entry is None

    def layout_content(self) -> None:
        self.update()

    def after_resize(self) -> None:
        self._cache = None                     # 大小变了要重新缩放
        self.update()

    # ---------------------------------------------------------------- 绘制

    def _content_rect(self) -> QRectF:
        return QRectF(self.PADDING, HEADER_H + self.PADDING,
                      max(self.box_width() - self.PADDING * 2, 10.0),
                      max(self.box_height() - HEADER_H - self.PADDING * 2
                          - self.CAPTION_H, 10.0))

    def _scaled_for(self, device_scale: float) -> QPixmap | None:
        """按"设备像素"缩放源图（只缩不放），并缓存。

        关键是乘上视图缩放：pixmap 的像素数跟屏幕上要画的点数对上，才是清的。
        """
        if self._source is None:
            return None
        area = self._content_rect()
        fit = min(area.width() / max(self._source.width(), 1),
                  area.height() / max(self._source.height(), 1))
        fit = min(fit, 1.0)                    # **小图不放大**：放大只会更糊
        target_w = max(int(self._source.width() * fit * device_scale), 1)
        target_h = max(int(self._source.height() * fit * device_scale), 1)
        if self._cache is not None and self._cache[:2] == (target_w, target_h):
            return self._cache[2]
        scaled = self._source.scaled(
            target_w, target_h, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(device_scale)
        self._cache = (target_w, target_h, scaled)
        return scaled

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem,
              widget: QWidget = None) -> None:
        super().paint(painter, option, widget)
        if self._is_empty():
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        area = self._content_rect()
        device_scale = painter.worldTransform().m11() or 1.0
        scaled = self._scaled_for(device_scale)
        if scaled is not None:
            logical_w = scaled.width() / max(scaled.devicePixelRatio(), 1e-6)
            logical_h = scaled.height() / max(scaled.devicePixelRatio(), 1e-6)
            target = QRectF(area.center().x() - logical_w / 2,
                            area.center().y() - logical_h / 2,
                            logical_w, logical_h)
            painter.drawPixmap(target, scaled, QRectF(scaled.rect()))
            painter.setPen(QPen(QColor(ACCENT_LINE)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(target)
        elif self._fallback is not None:
            size = int(min(area.width(), area.height(), 72.0))
            self._fallback.paint(painter,
                                 int(area.center().x() - size / 2),
                                 int(area.center().y() - size / 2),
                                 size, size)
        # 文件名 + 说明（太长中间省略）
        font = QFont(painter.font())
        font.setPointSizeF(8.4)
        painter.setFont(font)
        painter.setPen(QPen(QColor(MUTED)))
        metrics = QFontMetrics(font)
        caption = self.entry.name
        if self._message:
            caption = f"{caption} · {self._message}"
        painter.drawText(
            QRectF(self.PADDING, self.box_height() - self.CAPTION_H - 4,
                   area.width(), self.CAPTION_H),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(caption, Qt.TextElideMode.ElideMiddle,
                               int(area.width())),
        )

    # ---------------------------------------------------------------- 交互

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        if self.entry is not None:
            self.view.fileOpenRequested.emit(self.entry.version_id, self.entry.file_id)
        event.accept()


class CalendarPopup(QWidget):
    """自己弹的日历。

    为什么不用 QDateEdit 自带的 calendarPopup：日期控件嵌在画布方框里
    （QGraphicsProxyWidget），它自带的日历会被"塞进日期框内部"，挤成一团点不了。
    这里改成**我们自己弹一个顶层小窗口**：位置跟鼠标走，点日期即选中并关闭。
    """

    picked = Signal(QDate)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.calendar = QCalendarWidget(self)
        self.calendar.setGridVisible(True)
        self.calendar.setVerticalHeaderFormat(
            QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self.calendar)
        self.setStyleSheet(
            "QWidget{background:#ffffff;border:1px solid #cfd8e3;border-radius:8px;}"
        )
        self.calendar.clicked.connect(self._on_clicked)

    def _on_clicked(self, date: QDate) -> None:
        self.picked.emit(date)
        self.hide()

    def popup_at(self, global_pos, current: QDate | None) -> None:
        self.calendar.setSelectedDate(
            current if (current is not None and current.isValid()) else QDate.currentDate()
        )
        self.adjustSize()
        self.move(global_pos)
        self.show()


class _NoteEdit(QPlainTextEdit):
    """说明用的小文本框：它没有"编辑完成"信号，就用失去焦点当保存时机。"""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def focusOutEvent(self, event):  # noqa: N802
        self._callback()
        super().focusOutEvent(event)


def _refuse_drops(widget: QWidget) -> None:
    """让这个控件**连子控件一起**不接拖放。

    事件枝页上"拖文件进来"只有一个语义：加进鼠标底下那个方框的环节。
    控件一旦自己接了拖放，拖到它上面就变成"把路径当文本塞进去"
    （实测 QLineEdit / QPlainTextEdit 默认 ``acceptDrops() == True``），
    提交时这段路径写进字段里 —— 用户看到的是一句莫名其妙的文件路径。
    """
    widget.setAcceptDrops(False)
    for child in widget.findChildren(QWidget):
        child.setAcceptDrops(False)


class InfoBoxItem(_BoxBase):
    """信息方框：里面嵌的是真正的输入控件，改完即存库。"""

    def min_units(self) -> tuple[int, int]:
        # 日期/下拉这些真控件有最小宽度（实测 207px），太窄会被裁掉 → 至少 5 格（238px）
        return 5, 3

    def __init__(self, key: str, label: str, view: "EventStageView"):
        super().__init__(f"info:{key}", label, view)
        self.key = key
        self.glyph_key = key if key in BOX_GLYPH_KINDS else "box"
        self.fields: dict[str, QWidget] = {}
        self.proxy: QGraphicsProxyWidget | None = None
        self.hint = "可直接编辑"

    def build(self, widgets: list[tuple[str, str, QWidget]]) -> None:
        """把若干「字段 key / 显示名 / 控件」放进方框。"""
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(4)
        for key, name, widget in widgets:
            # 日期字段传进来的是"日期框 + 日历按钮"的容器，真正取值用里面的 QDateEdit
            inner = widget.findChild(QDateEdit) if isinstance(widget, QWidget) else None
            self.fields[key] = inner if inner is not None else widget
            if not name:
                layout.addWidget(widget, 1)
                continue
            row = QHBoxLayout()
            row.setSpacing(6)
            tag = QLabel(name)
            tag.setFixedWidth(54)
            scoped_style(tag, f"color:{MUTED}; font-size:11px;")
            row.addWidget(tag)
            row.addWidget(widget, 1)
            layout.addLayout(row)
        layout.addStretch(0)

        # 信息框里的真控件自己会吃拖放：实测 note.acceptDrops() 与 people.acceptDrops()
        # 都是 True —— 把文件拖到「说明」上会被当成**文本**塞进控件，
        # 提交时这段路径就写进了"这一稿的说明"。文件一律交给画布处理（照文件列表的做法）。
        _refuse_drops(container)

        self.proxy = self.view.add_widget(container)
        self.proxy.setParentItem(self)
        self.proxy.setAcceptDrops(False)
        self.layout_fields()

    def after_resize(self) -> None:
        self.layout_fields()

    def layout_fields(self) -> None:
        if self.proxy is not None:
            self.proxy.setGeometry(
                QRectF(8, HEADER_H + 4, self.box_width() - 16,
                       max(self.box_height() - HEADER_H - 12, 40.0))
            )

    # ---------------------------------------------------------------- 取值

    def set_values(self, values: dict) -> None:
        """把库里的值填进控件（屏蔽信号，免得被当成"用户改的"）。"""
        for key, widget in self.fields.items():
            if key not in values:
                continue
            value = values[key]
            widget.blockSignals(True)
            if isinstance(widget, QDateEdit):
                date = (
                    FIELD_DATE_MIN
                    if value in (None, "")
                    else QDate.fromString(str(value), Qt.DateFormat.ISODate)
                )
                widget.setDate(date if date.isValid() else FIELD_DATE_MIN)
            elif isinstance(widget, QSpinBox):
                widget.setValue(-1 if value in (None, "") else int(value))
            elif isinstance(widget, QComboBox):
                widget.setCurrentIndex(max(widget.findData(value), 0))
            elif isinstance(widget, QPlainTextEdit):
                widget.setPlainText(str(value or ""))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value or ""))
            widget.blockSignals(False)

    def read_values(self) -> dict:
        """读回控件里现在的值（关窗或刷新前补保存用）。"""
        values: dict[str, object] = {}
        for key, widget in self.fields.items():
            if isinstance(widget, QDateEdit):
                date = widget.date()
                values[key] = (
                    "" if date == FIELD_DATE_MIN else date.toString(Qt.DateFormat.ISODate)
                )
            elif isinstance(widget, QSpinBox):
                values[key] = None if widget.value() < 0 else widget.value()
            elif isinstance(widget, QComboBox):
                values[key] = widget.currentData()
            elif isinstance(widget, QPlainTextEdit):
                values[key] = widget.toPlainText().strip()
            elif isinstance(widget, QLineEdit):
                values[key] = widget.text().strip()
        return values


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


#: "按在空白处又松手"算不算一次点击：位移在这个像素数以内就算（手抖不算拖动）
CLICK_SLOP = 4

#: 「用 Word 比较」子菜单里最多列几条更早的稿。
#: 环节年轮里攒到十几稿是常事（用户的真实数据就是），全列出来菜单会长到屏幕外；
#: 超出的那几稿在「看年轮」里点（那里本来就是挑稿的地方）。
MAX_WORD_COMPARE_ITEMS = 12


class EventStageView(QGraphicsView):
    """事件枝页：能自己排版的方框画布。"""

    #: 缩放比例变了（百分比）—— 右下角那个控件用它刷新
    zoomChanged = Signal(int)

    #: 有文件被拖进某个环节（事件 id, 环节, 文件路径列表）
    filesDropped = Signal(int, str, list)
    #: 把**这一稿里的某一行文件**拖到了另一个环节方框上
    #: ``(事件 id, 版本 id, 文件 id, 目标环节)`` —— 只是"换个环节"，不新建稿
    fileRoleChangeRequested = Signal(int, int, int, str)
    #: 打开某一份文件（版本 id, 文件 id）
    fileOpenRequested = Signal(int, int)
    #: 导出某一份文件
    fileExportRequested = Signal(int, int)
    #: 批量导出（多选菜单）：list[(version_id, file_id), …]
    filesExportRequested = Signal(object)
    #: 在文件管理器里打开它所在目录
    fileFolderRequested = Signal(int, int)
    #: 打开它最初导入时的目录
    fileOriginFolderRequested = Signal(int, int)
    #: 把某些文件从它们**所属的那一稿**删掉（事件 id, [(version_id, file_id), …]）——
    #: 不新建稿；每个文件删它自己那一稿（行上的 entry.version_id 就是它所属的稿）
    filesDeleteRequested = Signal(int, object)
    #: 往某个环节添加文件（事件 id, 环节）
    addFilesRequested = Signal(int, str)
    #: 看某个环节的年轮（事件 id, 环节）
    stageFlowRequested = Signal(int, str)
    #: 用本机程序的比较功能比这一份文件
    #: （版本 id, 文件 id, 要比的那一稿, 用哪一套：``"word"`` / ``"wps"``）
    fileWordCompareRequested = Signal(int, int, int, str)
    #: 把"未登记"的那一份文件**另外登记到某一稿**（它所属的稿, 文件 id, 目标稿）
    #: —— 界面上的「复制一份到这一稿 / 到最新稿」，目标稿由**用户看到的那行字**决定
    fileRegisterRequested = Signal(int, int, int)
    #: 给库里的一行文件**改名**（它所属的稿, 文件 id）—— 界面上的右键「改名…」。
    #: 不碰用户磁盘上的原文件；库内那份自己的副本会跟着改名
    fileRenameRequested = Signal(int, int)
    #: 兼容旧名（双击卡片等价于打开文件）
    openRequested = Signal(int, int)
    #: 请求为失效引用重新绑定（版本 id）
    rebindRequested = Signal(int)
    #: 信息框里的内容被改了（字段名, 新值）
    fieldChanged = Signal(str, object)
    #: 框体模板里的某个方框变了（方框标识, 列, 行, 宽格数, 高格数）
    layoutChanged = Signal(str, int, int, int, int)
    #: 一次把所有方框设成同一大小（宽格数, 高格数）—— 会记成新方框的默认值
    uniformSizeChanged = Signal(int, int)
    #: 请求把这一页的排版清掉、回到默认排法（事件 id）
    resetLayoutRequested = Signal()
    #: 一句话状态
    statusMessage = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        zoom.prepare_zoomable(self)     # Ctrl+滚轮围绕鼠标缩放
        # 不设 ScrollHandDrag：那样会抢走左键，方框和信息框里的控件就点不动了
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        # 平移：中键 / 空格+左键 / **空白处左键拖**（这一页没有框选，
        # 空白处拖就还是平移）
        zoom.prepare_space_pan(self, Qt.CursorShape.ArrowCursor)
        # 画布底色：跟白板同一支（暖白 + 一点绿），别再用旧的冷蓝灰 ——
        self.setBackgroundBrush(QBrush(QColor(CANVAS_BG)))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

        self.document_id: int = 0
        self.version_id: int = 0
        #: 眼下这一页看的是第几稿（文件行右键的「改名」要用它说清改的是哪一稿）
        self.version_seq: int = 0
        #: 新方框的默认格数（没排过版的方框用它；存在库里 ui:event.units）
        self.default_units = (DEFAULT_UNITS_W, DEFAULT_UNITS_H)

        self._boxes: dict[str, _BoxBase] = {}
        self._info_boxes: dict[str, InfoBoxItem] = {}
        self._values: dict[str, object] = {}
        self._file_provider = None
        self._path_provider = None
        self._word_compare_provider = None
        self._targets_provider = None

        #: 视图还没真正显示出来时先记下"等有正常尺寸再适应窗口"
        self._pending_fit = False
        self._fitting = False
        #: 画布尺寸变化时要不要重新"适应窗口"（**默认要**：用户"窗口放大的时候
        #: 没自动放大事件的控件"）—— 只在他自己缩放过/平移过之后才不动（见 _auto_fit）
        self.follow_resize = True
        #: **布局编辑模式**（默认关）：关着时方框不能拖也不能拉、也不画格子 ——
        #: 这样平时归档文件绝不会误挪方框，也省掉一整类重绘。
        self.edit_layout = False
        #: 是否处在"自适应画布"：窗口/侧栏/底部窗口一变就重新适应；
        #: 用户手动缩放过之后就不再自动动他的镜头（按「适应」或 Ctrl+0 重新打开）
        self._auto_fit = True
        #: 底部"年轮"窗口（CLion 的 Git 窗口那种）——主窗口把它放在画布下面
        self.timeline_panel = None      # 主窗口调 attach_timeline_panel() 装上
        #: 当前高亮选中的文件行（单击 / 右键都会选中它）—— 多选时是最后点中的那一行
        self._selected_row: FileRow | None = None
        #: 多选集合（Ctrl 点选 / Shift 框选），文件行的高亮以它为准
        self._selected_rows: set[FileRow] = set()
        #: Shift 连续选择的锚点（这次多选从哪一行起算）
        self._selection_anchor: FileRow | None = None
        #: 在空白处按下时的位置（松手时基本没动 = 一次"点击"，见 clear_selection）
        self._empty_press_pos: QPoint | None = None
        #: 有没有文件行正在拖放中（拖动循环还没返回，见上面那三个方法）
        self._file_row_drag = False
        #: 搜索框里现在搜的是什么（文件行上命中的字要标出来）
        self.search_term = ""
        #: 日期控件用的日历弹窗（顶层窗口，由信息框按需弹出）
        self.calendar_popup: CalendarPopup | None = None
        #: 日历里选中日期后要写回哪个日期框
        self._calendar_target = None
        #: 改大小过程中被挤动过、还没落库的方框
        self._dirty_boxes: list[_BoxBase] = []
        #: 选中 / 拖动 / 拉伸的临时状态
        self.selected: _BoxBase | None = None
        self.moving: tuple[_BoxBase, QPointF, int, int, bool] | None = None
        self.resizing: tuple[_BoxBase, QPointF, int] | None = None

    # --------------------------------------------------------------- 格子

    def cell_rect(self, col: int, row: int,
                  units_w: int | None = None, units_h: int | None = None) -> QRectF:
        """格坐标 → 场景坐标（**正方形格子**，格子之间留 CELL_GAP 的缝）。"""
        width = (self.default_units[0] if units_w is None else units_w) * UNIT - CELL_GAP
        height = (self.default_units[1] if units_h is None else units_h) * UNIT - CELL_GAP
        return QRectF(
            MARGIN + col * UNIT + CELL_GAP / 2,
            MARGIN + row * UNIT + CELL_GAP / 2,
            width,
            height,
        )

    def set_default_units(self, units_w: int, units_h: int) -> None:
        """新方框默认多大（打开库时从库里读回来）。"""
        self.default_units = (
            max(MIN_UNITS_W, min(MAX_UNITS_W, int(units_w))),
            max(MIN_UNITS_H, min(MAX_UNITS_H, int(units_h))),
        )

    def set_all_units(self, units_w: int, units_h: int, *, notify: bool = False) -> None:
        """把**所有**方框一次设成同样大小。

        留着它是给脚本 / 测试用的一次性动作：默认仍然是"每个方框各自大小"。
        """
        self.set_default_units(units_w, units_h)
        for box in self._all_boxes():
            box.set_units(units_w, units_h)
        self.repack()
        if notify:
            # 只发**这次真的被挤动过**的方框：要全量发就显式传 notify=True 并让 repack()
            # 自己决定谁动了 —— 原来这里写 ``self._dirty_boxes or self._all_boxes()``，
            # 兜底会把 10 个方框全发一遍（每条 = 一次写库事务）
            for box in self._dirty_boxes:
                self._emit_layout(box)
            self.uniformSizeChanged.emit(units_w, units_h)
        # **无条件**清空：这是"一次排版动作的收尾"。
        # 不清的话残留会跨手势带出去 —— set_all_units(9,9)（不通知）之后
        # 只拉一个把手的拉伸会连这 9 条一起补发，一次拉伸写 10 次库。
        self._dirty_boxes.clear()

    def _taken_rects(self, boxes: list[_BoxBase]) -> list[QRectF]:
        return [
            QRectF(b.col * UNIT, b.row * UNIT,
                   b.units_w * UNIT, b.units_h * UNIT)
            for b in boxes
        ]

    def _overlaps(self, col: int, row: int, units_w: int, units_h: int,
                  boxes: list[_BoxBase]) -> bool:
        candidate = QRectF(col * UNIT, row * UNIT, units_w * UNIT, units_h * UNIT)
        return any(candidate.intersects(other) for other in self._taken_rects(boxes))

    def _preferred_cell(self, index: int, units_w: int, units_h: int) -> tuple[int, int]:
        """没排过版时，第 index 个方框放哪儿：一行摆得下就一行，摆不下换行。"""
        per_row = max(GRID_COLS // max(units_w, 1), 1)
        return (index % per_row) * units_w, (index // per_row) * units_h

    def _find_free_cell(self, boxes: list[_BoxBase], units_w: int, units_h: int):
        """找一个不压到别人身上的空位（照旧版 FormLayoutMatrix 的找空位做法）。"""
        step_w = max(units_w, 1)
        step_h = max(units_h, 1)
        for row in range(0, 120, step_h):
            for col in range(0, GRID_COLS * 2, step_w):
                if not self._overlaps(col, row, units_w, units_h, boxes):
                    return col, row
        return 0, 0

    def apply_layout(self, boxes: list[_BoxBase],
                     saved: dict[str, tuple[int, int, int, int]]) -> None:
        """按库里存的**位置与大小**摆放方框；没存过的按默认排法摆（撞上了就找空位）。"""
        placed: list[_BoxBase] = []
        for index, box in enumerate(boxes):
            units_w, units_h = self.default_units
            entry = saved.get(box.key)
            if entry:
                col, row, units_w, units_h = entry
                col, row = max(0, int(col)), max(0, int(row))
            else:
                col, row = self._preferred_cell(index, units_w, units_h)
            min_w, min_h = box.min_units()
            units_w = max(min_w, min(MAX_UNITS_W, int(units_w)))
            units_h = max(min_h, min(MAX_UNITS_H, int(units_h)))
            # 必须走 set_units()：它里面有 prepareGeometryChange()，
            # 而 boundingRect() 是拿 units_w/units_h 现算的 —— 直接赋值绕过了那次通知，
            # 场景的索引与重绘区域还按旧矩形算（拖动/拉伸后留下残影、命中判定也错位）
            box.set_units(units_w, units_h)
            if self._overlaps(col, row, units_w, units_h, placed):
                # 有人先占了（比如方框变多、或存的排版撞了）：找个空位放下
                col, row = self._find_free_cell(placed, units_w, units_h)
            box.set_cell(col, row)
            box.after_resize()
            placed.append(box)
        self._update_scene_rect()

    def repack(self) -> list[_BoxBase]:
        """按当前格数重新摆一遍：位置尽量不动，压到别人的才找空位。

        返回被挪动过的方框（调用方据此决定要不要落库）。
        """
        placed: list[_BoxBase] = []
        for box in self._all_boxes():
            col, row = box.col, box.row
            if self._overlaps(col, row, box.units_w, box.units_h, placed):
                col, row = self._find_free_cell(placed, box.units_w, box.units_h)
                if box not in self._dirty_boxes:
                    self._dirty_boxes.append(box)
            box.set_cell(col, row)
            box.after_resize()
            placed.append(box)
        self._update_scene_rect()
        return list(self._dirty_boxes)

    def _all_boxes(self) -> list[_BoxBase]:
        return list(self._boxes.values()) + list(self._info_boxes.values())

    def _update_scene_rect(self) -> None:
        """按方框的位置与大小算场景范围。

        刻意**不用** ``itemsBoundingRect()``：那要遍历场景里所有图元
        （每个方框还挂着滚动列表和一堆行控件），拖动时每帧都算一次太亏；
        方框的坐标本来就在手里，直接算 max 更快。
        """
        boxes = self._all_boxes()
        if not boxes:
            wanted = QRectF(0, 0, 640, 200)
        else:
            right = max(b.x() + b.box_width() for b in boxes)
            bottom = max(b.y() + b.box_height() for b in boxes)
            wanted = QRectF(0, 0, right + MARGIN, bottom + MARGIN)
        # 只在**真的需要变**的时候才设：setSceneRect 会连带重算滚动条，
        # 拖动时每格调一次纯属浪费（而且会让视口抖）
        if getattr(self, "_scene_rect_now", None) == wanted:
            return
        self._scene_rect_now = wanted
        self._scene.setSceneRect(wanted)

    # ---------------------------------------------------------- 选中与手势

    def select_box(self, box: _BoxBase | None) -> None:
        if self.selected is box:
            return
        previous = self.selected
        self.selected = box
        if box is not None:
            box.setZValue(8)
        if previous is not None and previous is not box:
            previous.setZValue(5)
        for item in (previous, box):
            if item is not None:
                item.update()

    def clear_selection(self) -> None:
        """取消高亮：方框和文件行一起（点空白处、或按 Esc 时用）。"""
        self.select_box(None)
        self.select_file_row(None)

    def selected_box(self) -> _BoxBase | None:
        return self.selected

    # ------------------------------------------------------------ 把手

    #: 三个把手（照旧版）：右中改宽、下中改高、右下角一起改 —— 只动这一个方框
    GRIP_SPOTS = {"right": (1.0, 0.5), "bottom": (0.5, 1.0), "corner": (1.0, 1.0)}

    def box_scene_rect(self, box: _BoxBase) -> QRectF:
        """方框**本身**在场景里的矩形（不含 ``boundingRect()`` 为选中环留的 8px 余量）。

        画把手、画选中外框、判"点在哪个方框上"都得用它。拿 ``sceneBoundingRect()``
        当方框用会整体外扩 8px：角上的把手画在方框外 8px 处，
        两个方框之间只有 CELL_GAP=12px 的缝，8+8 > 12 —— 缝里那几像素还会被
        算成"后画的"方框（与 ``shape()`` 只用方框本身的初衷正好矛盾）。
        """
        return box.mapToScene(
            QRectF(0, 0, box.box_width(), box.box_height())
        ).boundingRect()

    def grip_center(self, box: _BoxBase, name: str = "corner") -> QPointF:
        rect = self.box_scene_rect(box)
        fx, fy = self.GRIP_SPOTS.get(name, (1.0, 1.0))
        return QPointF(rect.left() + rect.width() * fx, rect.top() + rect.height() * fy)

    def grip_centers(self, box: _BoxBase) -> dict[str, QPointF]:
        return {name: self.grip_center(box, name) for name in self.GRIP_SPOTS}

    def grip_at(self, viewport_pos) -> str | None:
        """点是不是落在选中方框的某个把手上（视图先判，卡片/控件就抢不走了）。

        判定用**屏幕像素**（半径 ``GRIP_HIT``）：用场景坐标判的话，
        视图一缩小，同样的场景半径在屏幕上就只剩几个像素，把手就点不中了。
        """
        box = self.selected
        if box is None:
            return None
        for name in ("corner", "right", "bottom"):
            center = self.mapFromScene(self.grip_center(box, name))
            if math.hypot(center.x() - viewport_pos.x(),
                          center.y() - viewport_pos.y()) <= GRIP_HIT:
                return name
        return None

    def _grip_cursor(self, name: str) -> Qt.CursorShape:
        return {
            "right": Qt.CursorShape.SizeHorCursor,
            "bottom": Qt.CursorShape.SizeVerCursor,
            "corner": Qt.CursorShape.SizeFDiagCursor,
        }.get(name, Qt.CursorShape.ArrowCursor)

    def drawForeground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        """选中框和小圆点把手画在最上层 —— 卡片再多也挡不住把手。

        **布局锁着时不画把手**：否则一选中就冒出三个小圆点，看着像"点一下就能调大小"，
        锁着时那一圈"选中"提示由**方框自己**（``_BoxBase.paint``）画 ——
        这里再画一遍就是同一个方框上套两圈（相对方框 (0,0,288,288) 与 (-11,-11,310,310)）。
        """
        super().drawForeground(painter, rect)
        box = self.selected
        if box is None or not self.edit_layout:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # 外框贴着**方框本身**画（不含 boundingRect 的 8px 余量）
        frame = self.box_scene_rect(box).adjusted(-3, -3, 3, 3)
        pen = QPen(QColor(ACCENT))
        pen.setWidthF(1.6)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(frame, 12, 12)

        pen = QPen(QColor(ACCENT))
        pen.setWidthF(1.4)
        painter.setPen(pen)
        painter.setBrush(QBrush(QColor("#ffffff")))
        for center in self.grip_centers(box).values():
            painter.drawEllipse(center, GRIP_R, GRIP_R)
        painter.restore()

    def mousePressEvent(self, event):  # noqa: N802
        position = event.position().toPoint()
        if event.button() == Qt.MouseButton.LeftButton and self.edit_layout:
            # 布局锁着时不认把手（否则"选中方框后随便点哪都在拉伸"）
            name = self.grip_at(position)
            if name is not None:
                self.begin_resize(self.selected, name, self.mapToScene(position))
                event.accept()
                return
        # 按在空白处：记下位置。松手时如果基本没动，就当成一次"点击空白"
        # 因为按在空白处左键拖是**平移画布**的手势，平移不该顺手清掉选中。
        # （以前这里只有"按下去就清"，而左键空白按下的那一路被平移分支抢走了，
        #   于是那条清理代码根本走不到。）
        if (event.button() == Qt.MouseButton.LeftButton
                and self.itemAt(position) is None):
            self._empty_press_pos = position
        else:
            self._empty_press_pos = None
        # 平移画布：中键 / 空格+左键（这两种哪儿都能拖）
        if zoom.space_pan_press(self, event):
            event.accept()
            return
        # 这个画布没把"空白左键拖"交给平移时，按下即取消（照旧）
        if self.itemAt(position) is None:
            self.clear_selection()
        super().mousePressEvent(event)

    def keyPressEvent(self, event):  # noqa: N802
        if zoom.space_pan_key(self, event):
            event.accept()
            return
        # 绑到 delete 键盘动作"）。真正删之前主窗口会弹一次确认框并写明版本号，
        # 所以这里放心地把选择交出去 —— 顺手把选择也清掉，免得"删完了那一行还亮着"。
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            rows = [row.entry for row in self._selected_rows]
            if rows:
                self.filesDeleteRequested.emit(
                    self.document_id, [(entry.version_id, entry.file_id) for entry in rows]
                )
                self.clear_selection()
                event.accept()
                return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):  # noqa: N802
        if zoom.space_pan_key(self, event):
            event.accept()
            return
        super().keyReleaseEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        position = event.position().toPoint()
        if self.resizing is not None:
            self.update_resize(self.mapToScene(position))
            event.accept()
            return
        if zoom.space_pan_move(self, event):
            self._auto_fit = False
            zoom.end_stable_fit(self)       # 手动平移：滚动条还给用户
            event.accept()
            return
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            name = self.grip_at(position) if self.edit_layout else None
            self.viewport().setCursor(
                self._grip_cursor(name) if name else Qt.CursorShape.ArrowCursor
            )
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self.resizing is not None:
            self.finish_resize()
            event.accept()
            return
        # 点了空白处（按下与松开之间几乎没动）→ 取消方框与文件行的高亮。
        # 判定用"位移"而不是"时间"：手抖一两像素仍算点击，真的拖了（平移）就不算。
        press = self._empty_press_pos
        self._empty_press_pos = None
        clicked_empty = (
            press is not None
            and event.button() == Qt.MouseButton.LeftButton
            and (event.position().toPoint() - press).manhattanLength() <= CLICK_SLOP
        )
        handled = zoom.space_pan_release(self, event)
        if clicked_empty:
            self.clear_selection()
        if handled:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def resize_hint(self, box: _BoxBase) -> str:
        """正在改大小时，标题栏右边显示格数（让人知道自己在拉多大）。

        因为是一套模板，这里显示的是**所有方框**的格数。
        """
        if self.resizing is not None and self.resizing[0] is box:
            return f"{box.units_w} × {box.units_h} 格"
        return ""

    def begin_move(self, box: _BoxBase, scene_pos: QPointF) -> None:
        if self.resizing is not None:
            return
        # 第五个值 = "真的开始拖了吗"：先按下只是选中，走出一段距离才算拖（照旧版的做法）
        self.moving = (box, scene_pos, box.col, box.row, False)

    def update_move(self, scene_pos: QPointF) -> None:
        if self.moving is None:
            return
        box, start, col0, row0, started = self.moving
        dx = scene_pos.x() - start.x()
        dy = scene_pos.y() - start.y()
        if not started:
            if abs(dx) + abs(dy) < 6:
                return
            started = True
            self.moving = (box, start, col0, row0, True)
        col = col0 + int(round(dx / UNIT))
        row = row0 + int(round(dy / UNIT))
        if (col, row) != (box.col, box.row):
            box.set_cell(col, row)          # setPos 会让 Qt 重画"旧位置 + 新位置"
            self._update_scene_rect()
        box.update()                        # 只重画这个方框，不整块重绘

    def finish_move(self) -> None:
        if self.moving is None:
            return
        box, _start, col0, row0, started = self.moving
        self.moving = None
        if started:
            # 放到别人身上了？挪到最近的空位（网格是给整齐用的，不该允许叠着）
            others = [item for item in self._all_boxes() if item is not box]
            if self._overlaps(box.col, box.row, box.units_w, box.units_h, others):
                col, row = self._nearest_free_cell(
                    box.col, box.row, box.units_w, box.units_h, others
                )
                box.set_cell(col, row)
                box.after_resize()
        self._update_scene_rect()
        if started and (box.col, box.row) != (col0, row0):
            self._emit_layout(box)
            self._refit_if_needed()

    def _nearest_free_cell(self, col: int, row: int, units_w: int, units_h: int,
                           boxes: list[_BoxBase]) -> tuple[int, int]:
        """从想放的那一格往外一圈圈找，返回最近的一个不压人的格子。"""
        step_w = max(units_w, 1)
        step_h = max(units_h, 1)
        offsets = sorted(
            ((dc, dr) for dc in range(-GRID_COLS, GRID_COLS + 1, step_w)
             for dr in range(0, 60, step_h)),
            key=lambda pair: abs(pair[0]) + abs(pair[1]),
        )
        for dc, dr in offsets:
            candidate_col = max(0, col + dc)
            candidate_row = max(0, row + dr)
            if not self._overlaps(candidate_col, candidate_row, units_w, units_h, boxes):
                return candidate_col, candidate_row
        return col, row

    def begin_resize(self, box: _BoxBase, grip: str, scene_pos: QPointF) -> None:
        #: (方框, 把手, 起手时的场景坐标, 起手时的宽格数, 起手时的高格数)
        self.resizing = (box, grip, scene_pos, box.units_w, box.units_h)

    def update_resize(self, scene_pos: QPointF) -> None:
        """拖把手改这个方框自己的大小：右中改宽、下中改高、右下角一起改。"""
        if self.resizing is None:
            return
        box, grip, start, units_w0, units_h0 = self.resizing
        dx = scene_pos.x() - start.x()
        dy = scene_pos.y() - start.y()
        units_w, units_h = units_w0, units_h0
        if grip in ("right", "corner"):
            units_w = units_w0 + int(round(dx / UNIT))
        if grip in ("bottom", "corner"):
            units_h = units_h0 + int(round(dy / UNIT))
        box.set_units(units_w, units_h)
        box.set_cell(box.col, box.row)
        self._update_scene_rect()
        box.update()                        # 同上：只重画这一个

    def finish_resize(self) -> None:
        if self.resizing is None:
            return
        box = self.resizing[0]
        self.resizing = None
        self.repack()
        # 被挤动的方框 + 自己这个：位置和大小都落库
        for item in {box, *self._dirty_boxes}:
            self._emit_layout(item)
        self._dirty_boxes = []
        self._refit_if_needed()

    def _emit_layout(self, box: _BoxBase) -> None:
        """上报模板里这个方框的位置与大小（全库共用一份）。"""
        self.layoutChanged.emit(
            box.key, box.col, box.row, box.units_w, box.units_h
        )

    def _refit_if_needed(self) -> None:
        """拖动/拉伸后如果内容跑出视野，自动收一下；没跑出去就别乱动镜头。"""
        rect = self._scene.itemsBoundingRect().adjusted(-24, -24, 24, 24)
        visible = self.mapToScene(self.viewport().rect()).boundingRect()
        if not visible.contains(rect):
            self.fit_to_content()

    def reset_layout(self) -> None:
        """清掉手工排的位置，按默认排法重新摆一遍（统一的大小不动）。"""
        self.apply_layout(self._all_boxes(), {})
        self.select_box(None)
        self.resetTransform()
        self.fit_to_content()

    # ------------------------------------------------------------- 构建场景

    def set_event(self, library: Library, document_id: int,
                  version_id: int | None = None, keep_view: bool = False) -> None:
        """重建画布：按**框体模板**摆好（没排过的方框用默认排法）。

        :param version_id: 要看哪一稿；不给就看最新的一稿（默认行为）。
        :param keep_view: 只是**内容变了**（改状态 / 改说明之后刷新），
            同一个事件枝的镜头原样保住。以前这里末尾无条件
            ``resetTransform() + fit_to_content()``，于是在事件枝页上
            改一下状态，用户调好的缩放和滚动位置就被拉回"适应全部"了。
        """
        document = library.get_document(document_id)
        if document is None:
            return
        # 只是刷新内容的话，先把镜头拍下来，末尾原样放回去（见 zoom.camera）
        shot = zoom.camera(self) if keep_view else None
        # **重建画布之前先把还没提交的编辑落库**：说明框只在 focusOutEvent 回写，
        # 而"归档完成 → open_event"会到这里把整页拆掉重画 —— 用户正在输入的文字
        # 没有失焦就被连同控件一起丢掉（实测：库里的 note 仍然是空的）。
        # commit_pending() 读一遍控件里的值并 emit，等于替用户补一次"失焦"。
        self.commit_pending()
        self.document_id = document_id
        version = library.get_version(version_id or document.current_version_id)
        if version is None:
            version = library.latest_version(document_id)
        self.version_id = version.id if version else 0
        self.version_seq = version.seq if version else 0
        self._fill_timeline(library, document)

        # 先清 Python 侧的引用，再清场景：否则会摸到已经被 C++ 删掉的对象
        self._boxes.clear()
        self._info_boxes.clear()
        self._values.clear()
        self._selected_row = None
        self._selected_rows.clear()
        self._selection_anchor = None
        self._dirty_boxes = []
        self.selected = None
        self.pending_move = None
        self.moving = None
        self.resizing = None
        self._scene.clear()

        if version is None:
            # "新增的事件默认增加节点模板，现在是空白的"），只是每个方框都是空的，
            # 往里拖文件就能加进这一稿的环节。
            grouped = {}
        else:
            grouped = self._collect_files(library, document, version)

        for role in FLOW_ROLES:
            entries = grouped.get(role, [])
            box_class = FigureBoxItem if role == "figure" else StageBoxItem
            box = box_class(role, ROLE_LABELS.get(role, role), document.state, self)
            self._scene.addItem(box)
            box.set_files(entries)
            box.hint = self._box_hint(entries, figure=role == "figure")
            box.setToolTip(
                f"{box.label}\n"
                "把文件拖进来 = 加进**当前这一稿**的这个环节（不会新建稿）\n"
                "把这里的文件行拖到别的方框 = 改它的环节（Ctrl+Z 可撤销）\n"
                "拖标题栏 = 挪动方框；选中后拖小圆点 = 改大小"
            )
            self._boxes[role] = box

        others = [role for role in grouped if role not in FLOW_ROLES]
        entries = [entry for role in others for entry in grouped[role]]
        box = StageBoxItem(EXTRA_ROLE, "其他文件", document.state, self)
        self._scene.addItem(box)
        box.set_files(entries)
        box.hint = self._box_hint(entries)
        box.setToolTip(
            "其他文件\n"
            "说明类文本、主文件、附件这类不好归进环节的都放这里\n"
            "拖进来的文件加进**当前这一稿**，按「附件」登记；\n"
            "把文件行拖到别的方框 = 改它的环节"
        )
        self._boxes[EXTRA_ROLE] = box

        self._build_info_boxes(document)
        self.fill_version_note(version)
        # 页面是**重建**出来的：上面那些新行还没吃到我现在的搜索关键词，
        # 这里统一补一遍（否则"搜着搜着切一稿，高亮就没了"）
        self._apply_search_to_rows()

        # 排版不是"每个事件枝各排一份"，而是全库共用的**框体模板**
        self.apply_layout(self._all_boxes(), library.box_template())
        if self.timeline_panel is not None:
            self.timeline_panel.select(self.version_id)
        if keep_view:
            zoom.restore_camera(self, shot)
        else:
            self.resetTransform()
            self.fit_to_content()

    def attach_timeline_panel(self, panel) -> None:
        """主窗口把底部"年轮"窗口交给画布（内容由画布负责填）。"""
        self.timeline_panel = panel

    def set_search(self, text: str) -> None:
        """告诉事件枝页"现在在搜什么"：文件名里命中的那几个字标出来。

        只是个画法（不筛掉任何一行）：关键词为空、或者这一行没命中时，
        文字与以前一模一样（``FileRow.apply_search``）。
        """
        term = str(text or "").strip()
        if term == self.search_term:
            return
        self.search_term = term
        self._apply_search_to_rows()

    def _apply_search_to_rows(self) -> None:
        for box in self._boxes.values():
            for row in getattr(box, "files", ()):
                row.apply_search(self.search_term)

    def _fill_timeline(self, library: Library, document) -> None:
        """填底部"年轮"窗口：每一稿一行（最新在最上），点一下就切那一稿。

        "在年轮的'与上一稿的差别'说明创建、附件变化后面，附上这版里的说明文本框
        的文字"）—— 差别说的是"文件层面动了什么"，说明说的是"我为什么这么改"，
        两样都在同一行上，扫一眼就知道这一稿是什么。

        表格是单行的，所以：**格子里**把说明压成一行（换行折成空格，太长由表格自己
        省略号截断）；**悬停提示**里保留原文（换行照旧），要看全文把鼠标停上去即可。
        """
        panel = self.timeline_panel
        if panel is None:
            return
        entries: list[tuple[int, str, str, str, bool, str]] = []
        history = library.version_history(document.id)
        for index, version in enumerate(history):
            previous = history[index - 1] if index > 0 else None
            try:
                summary = summarize_change(previous, version)
            except Exception:
                summary = ""
            if not version.files:
                # **不写"待填充"**：这一稿点出来就是真的了（用户按的「＋ 新增一稿」），
                # 可以只写说明、一个文件都不放。写"待填充"会让人以为
                summary = "（还没有文件）"
            summary = summary or "—"
            note = (getattr(version, "note", "") or "").strip()
            change = f"{summary} · 说明：{one_line(note)}" if note else summary
            tooltip = f"{summary}\n说明：{note}" if note else summary
            entries.append(
                (
                    version.id,
                    f"v{version.seq}",
                    format_archived_at(version.created_at),
                    change,
                    version.id == document.current_version_id,
                    tooltip,
                )
            )
        panel.set_versions(entries, self.version_id)

    def _collect_files(self, library: Library, document, version) -> dict[str, list[FileEntry]]:
        """这一稿的「环节 → 文件卡片」；当前稿没登记的环节回落到它最后登记的那一稿。"""

        def status_of(item) -> str:
            """这一行要不要挂个警示：**只在"文件没了"时挂**。

            改动是控在用户手里的。" 所以以前那个"大小和归档时不一样 → 标成
            「内容已变」"的判断去掉了：改文件是常态，页面上不该为此挂个 ⚠。
            文件**不在原处**是另一回事 —— 那是真的打不开了，得说。
            """
            if not item.location:
                return ""
            path = Path(item.location)
            if not path.is_file():
                return "missing"
            try:
                path.stat()
            except OSError:
                # is_file() 与 stat() 之间文件被移走/没权限读：不能让它抛出去，
                # 这里只是一个"卡片上要不要标警示"的判断，抛出去整个事件枝页都画不出来
                return "missing"
            return ""

        def make_entry(item, stale: bool) -> FileEntry:
            """一行文件卡片。

            **关键**：``version_id`` 必须和 ``file_id`` 配套 —— 画布上显示的是
            "这一稿没登记、来自更早某一稿"的文件时（``stale``），这两个 id 要取
            **那一份文件自己所属的稿**。一开始这里写死成当前稿，于是
            ``blob_path(当前稿, 旧稿的 file_id)`` 找不到文件、退回"主文件"，
            """
            # "我可能是把 V1 提到 V2，但 V3 又有文件了" —— 那时"最新稿"是 V3）
            is_latest = version.id == document.current_version_id
            return FileEntry(
                file_id=item.id,
                version_id=item.version_id if stale else version.id,
                version_seq=(older.seq if stale else version.seq),
                name=item.name,
                size=item.size,
                sha256=item.sha256,
                mime=item.mime,
                created_at=(older.created_at if stale else version.created_at),
                source_path=item.source_path,
                in_library=not item.location,
                status=status_of(item),
                stale=stale,
                stale_note=("最新稿未登记" if is_latest else f"v{version.seq} 未登记"),
            )

        grouped: dict[str, list[FileEntry]] = {}
        for item in version.files:
            grouped.setdefault(item.role, []).append(make_entry(item, False))

        # 旧库是"增量登记"的：一稿里只写那一次登记过的文件，所以这一稿某个环节里
        # **可能少了几份上一稿还在的文件**。它们要留成灰色的「未登记」行，
        # 也留灰色显示的文件，而不是现在这样最新版本里只要有东西，就不会显示其他文件缺少的情况"）。
        #
        # 两条规矩：
        # * 只在**这一稿没有任何一行提到它**时才补（同一份交付物改了名字也算"提到"，
        #   用内核那套模糊名字匹配判断，见 ``_same_deliverable``）；
        # * 每个环节只看**最近一稿登记过这个环节的**那些文件（跟以前一样），
        #   往更早的稿里无限追溯只会把画布塞满历史。
        history = library.version_history(document.id)
        for role in set(FLOW_ROLES) | set(grouped):
            current = grouped.get(role, [])
            for older in reversed(history):
                if older.id == version.id:
                    continue
                members = [item for item in older.files if item.role == role]
                if not members:
                    continue
                missing = [item for item in members
                           if not any(_same_deliverable(item.name, entry.name)
                                      for entry in current)]
                if missing:
                    # 老行的 version_id 要指向**它自己所属的那一稿**（见 make_entry 的说明）
                    for item in missing:
                        grouped.setdefault(role, []).append(make_entry(item, True))
                else:
                    # 这一稿里已经全都有对应（包括改了名字的）：不用再补灰行
                    pass
                break
        return grouped

    @staticmethod
    def _box_hint(entries: list[FileEntry], *, figure: bool = False) -> str:
        """方框抬头那句小字：**数出来的文件里，有几份这一稿没登记**。

        以前只在"整框全是灰行"时才写「N 个 · 最新稿未登记」，一旦这一稿自己也登记了
        东西（哪怕只登记了一份），提示就变成干巴巴的「2 个文件」——
        文件的情况，也留灰色显示的文件"）。所以混着的时候把灰的那几份数出来。
        """
        if not entries:
            return "把一张图拖进来" if figure else "空"
        stale = [entry for entry in entries if entry.stale]
        if not stale:
            return f"{len(entries)} 个文件"
        note = stale[0].stale_note or "未登记"
        if len(stale) == len(entries):
            return f"{len(entries)} 个 · {note}"
        return f"{len(entries)} 个文件 · 其中 {len(stale)} 份{note}"

    def _build_info_boxes(self, document) -> None:
        # 日历弹窗是视图的顶层窗口：每次重建画布换一个新的（旧的先关掉）
        if self.calendar_popup is not None:
            self.calendar_popup.close()
            self.calendar_popup.deleteLater()
        self.calendar_popup = CalendarPopup(self)
        self.calendar_popup.picked.connect(self._on_calendar_picked)

        def date_row() -> tuple[QWidget, QDateEdit]:
            """日期 = 可输入的日期框 + 一个"📅"按钮（开我们自己的日历）。"""
            edit = QDateEdit()
            # 不用自带的弹出日历：在画布方框里会被裁成一小团，点不了
            edit.setCalendarPopup(False)
            edit.setDisplayFormat("yyyy-MM-dd")
            edit.setMinimumDate(FIELD_DATE_MIN)
            edit.setSpecialValueText("未设置")
            edit.setDate(FIELD_DATE_MIN)

            button = QToolButton()
            button.setText("📅")
            button.setToolTip("点这里选日期（也可以在框里直接输入）")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setFixedWidth(24)
            button.clicked.connect(
                lambda _checked=False, e=edit, b=button: self._open_calendar(e, b)
            )

            holder = QWidget()
            layout = QHBoxLayout(holder)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(4)
            layout.addWidget(edit, 1)
            layout.addWidget(button)
            return holder, edit

        task_holder, task_date = date_row()
        due_holder, due_date = date_row()

        people = QLineEdit()
        people.setPlaceholderText("逗号分隔")
        people.setClearButtonEnabled(True)

        progress = QSpinBox()
        progress.setRange(-1, 100)
        progress.setSpecialValueText("未设置")
        progress.setSuffix(" %")

        # 这几个是真控件，默认自己接拖放（见 _refuse_drops）：
        # 日期框的容器 / 相关人 / 进度 一并关掉
        for holder in (task_holder, due_holder, people, progress):
            _refuse_drops(holder)

        note = _NoteEdit(lambda: self._emit_field("version_note", note.toPlainText().strip()))
        note.setPlaceholderText("这一稿是什么、改了什么…（每一稿可以不一样）")
        note.setToolTip("这一稿的说明：存在这一稿上，换一稿就换一份说明")
        _refuse_drops(note)

        task_date.dateChanged.connect(
            lambda date: self._emit_field(
                "task_date",
                "" if date == FIELD_DATE_MIN else date.toString(Qt.DateFormat.ISODate),
            )
        )
        due_date.dateChanged.connect(
            lambda date: self._emit_field(
                "due_date",
                "" if date == FIELD_DATE_MIN else date.toString(Qt.DateFormat.ISODate),
            )
        )
        people.editingFinished.connect(
            lambda: self._emit_field("people", people.text().strip())
        )
        progress.valueChanged.connect(
            lambda value: self._emit_field("progress", None if value < 0 else value)
        )


        layout = [
            ("time", "时间", [("task_date", "任务日期", task_holder),
                              ("due_date", "限期", due_holder)]),
            ("people", "相关人", [("people", "", people)]),
            # 状态不在这里改：入口是头部那个状态胶囊（叶子/橡果/黄叶）
            ("progress", "进度", [("progress", "进度", progress)]),
            ("note", "说明", [("version_note", "", note)]),
        ]

        values = {
            "task_date": document.task_date,
            "due_date": document.due_date,
            "people": document.people,
            "progress": document.progress,
            "description": document.description,
        }
        self._values = dict(values)

        for key, label, fields in layout:
            box = InfoBoxItem(key, label, self)
            self._scene.addItem(box)
            box.build(fields)
            box.set_values(values)
            box.setToolTip(
                f"{label}\n直接在框里改，改完就存库\n拖标题栏 = 挪动方框；选中后拖小圆点 = 改大小"
            )
            self._info_boxes[key] = box

    def _open_calendar(self, edit: QDateEdit, button: QWidget) -> None:
        """在"📅"按钮下面弹出日历（**顶层窗口**，点外面就关）。

        注意：这个方法是**视图**的，不是信息框图元的 —— 按钮那行 lambda 里的
        ``self`` 是 EventStageView（日历弹窗 ``calendar_popup`` 也归视图管）。
        以前它被写在 InfoBoxItem 上，于是点一次 📅 就报

        选中的日期写回**哪一个**控件，由 ``self._calendar_target`` 记着 ——
        弹窗只连一次信号，避免每开一次就多连一根（那会越连越多）。
        """
        popup = self.calendar_popup
        if popup is None or edit is None:
            return
        self._calendar_target = edit
        current = edit.date()
        if current == FIELD_DATE_MIN:
            current = None          # "未设置"：日历从今天开始
        try:
            popup.popup_at(button.mapToGlobal(button.rect().bottomLeft()), current)
        except RuntimeError:
            self._calendar_target = None    # 画布已重建、按钮没了

    def _on_calendar_picked(self, date: QDate) -> None:
        """日历里点了某天：写回刚才那个日期框（写回就会存库）。"""
        target = self._calendar_target
        if target is not None:
            try:
                target.setDate(date)
            except RuntimeError:
                pass          # 画布已经重建、控件没了
        self._calendar_target = None

    def fill_version_note(self, version) -> None:
        """把**这一稿的说明**填进说明框（换成别的一稿就换成那一稿的说明）。

        说明是按稿存的（``versions.note``），所以年轮可以不一样；
        界面上不再有"备注"这个概念。
        """
        box = self._info_boxes.get("note")
        if box is None or version is None:
            return
        widget = box.fields.get("version_note")
        if not isinstance(widget, QPlainTextEdit):
            return
        widget.blockSignals(True)
        widget.setPlainText((getattr(version, "note", "") or "").strip())
        widget.blockSignals(False)
        self._values["version_note"] = (getattr(version, "note", "") or "").strip()

    def _placeholder(self, message: str) -> None:
        """空画布的占位：程序图标 + 一段枝条 + 一句提示（简洁，不喧哗）。"""
        rect = QRectF(0, 0, 620, 220)
        logo = self._scene.addPixmap(logo_pixmap(72))
        logo.setPos(rect.center().x() - 36, 40)
        branch = self._scene.addPixmap(branch_pixmap(220, 22))
        branch.setPos(rect.center().x() - 110, 126)
        item = self._scene.addText(message)
        item.setDefaultTextColor(QColor(MUTED))
        font = QFont()
        font.setPointSizeF(11)
        item.setFont(font)
        item.setPos(rect.center().x() - item.boundingRect().width() / 2, 162)
        self._scene.setSceneRect(rect)
        self.fit_to_content()

    # --------------------------------------------------------------- 编辑

    def _emit_field(self, key: str, value) -> None:
        """信息框里的值变了：记下来并报给主窗口（由它写库）。"""
        if self._values.get(key) == value:
            return
        self._values[key] = value
        self.fieldChanged.emit(key, value)

    def commit_pending(self) -> None:
        """补保存还没提交的编辑（比如说明框光标还闪着就把窗口关了）。"""
        for info in list(self._info_boxes.values()):
            for key, value in info.read_values().items():
                self._emit_field(key, value)

    def apply_state(self, state: str) -> None:
        """状态变了：只换个颜色，不重建画布（免得正在编辑的框丢焦点）。"""
        for box in self._all_boxes():
            box.state = state
            box.update()

    # ----------------------------------------------------------- 拖放与菜单

    def add_widget(self, widget: QWidget) -> QGraphicsProxyWidget:
        """把普通控件嵌进场景（信息框里的输入框）。"""
        return self._scene.addWidget(widget)

    def set_file_provider(self, provider) -> None:
        """注入"拖出导出"的回调：``fn(version_id, file_id) -> list[Path]``。"""
        self._file_provider = provider

    def set_file_path_provider(self, provider) -> None:
        """注入"取文件真实路径"的回调（缩略图用，不复制文件）：
        ``fn(version_id, file_id) -> Path | None``。"""
        self._path_provider = provider

    def set_word_compare_provider(self, provider) -> None:
        """注入"这份文件能和哪些更早的稿用 Word 比"的回调：
        ``fn(version_id, file_id) -> dict``（结构见 ``Library.word_compare_candidates``）。

        业务一律在内核里算（本机有没有 Word、配上哪一份、哪些已经失效），
        视图只负责把它拼成菜单 —— 摆错一行字就会让用户以为比的是别的东西。
        """
        self._word_compare_provider = provider

    def word_compare_info(self, version_id: int, file_id: int) -> dict:
        """问一句"这份文件的 Word 比较能怎么比"；没装 provider 就返回空。"""
        if self._word_compare_provider is None:
            return {}
        try:
            return self._word_compare_provider(version_id, file_id) or {}
        except Exception:
            # 菜单里少一项，总比右键直接抛异常强（异常在 Qt 事件里不是日志，是崩）
            return {}

    def set_version_targets_provider(self, provider) -> None:
        """注入"这一页能在哪几稿之间搬文件"的回调：
        ``fn(document_id, shown_version_id) -> {"shown": {...}, "latest": {...}|None}``。

        "我可能是在把 V1 提到 V2，而 V3 又有文件了" —— 一个不写稿号的
        「复制到最新稿」会让人不知道自己按的到底是哪一稿。
        """
        self._targets_provider = provider

    def version_targets(self) -> dict:
        """这一页的"正在看的稿 / 最新稿"（拿不到就返回空，菜单据此少列一项）。"""
        if self._targets_provider is None:
            return {}
        try:
            return self._targets_provider(self.document_id, self.version_id) or {}
        except Exception:
            return {}

    def stage_file(self, version_id: int, file_id: int) -> list[Path]:
        if self._file_provider is None:
            return []
        try:
            return self._file_provider(version_id, file_id)
        except Exception:
            return []

    def file_path(self, version_id: int, file_id: int):
        if self._path_provider is None:
            return None
        try:
            return self._path_provider(version_id, file_id)
        except Exception:
            return None

    def _file_exists(self, entry: FileEntry) -> bool:
        """这份文件现在还在不在（菜单项"打开所在文件夹"据此置灰）。

        ``status`` 是**上一次刷新时**算出来的（``_collect_files.status_of``），
        这里再问一次磁盘：右键的这一刻文件可能刚被挪走 —— 菜单里给一个死链接，
        用户点了只会得到"目录不存在"。取不到路径就当作不存在（宁可不给点）。
        """
        path = self.file_path(entry.version_id, entry.file_id)
        if path is None:
            return False
        try:
            return bool(Path(path).is_file())
        except OSError:
            # is_file() 也可能因为权限/断链报错 —— 这种文件同样打不开
            return False

    def _box_at(self, scene_pos: QPointF) -> _BoxBase | None:
        # 后画的在上面，所以倒着找。
        # 判定用 shape()（= 方框本身），**不用** sceneBoundingRect()：后者含
        # SELECTION_RING_PAD=8px 的余量，而格子之间只有 CELL_GAP=12px 的缝，
        # 8+8 > 12 —— 缝里那几像素会被算成"后画的"方框，把文件丢进缝里
        # 会被静默当成"放进那个框"，与 shape() 的初衷矛盾）。
        for box in reversed(self._all_boxes()):
            if box.shape().contains(box.mapFromScene(scene_pos)):
                return box
        return None

    def _stage_box_at(self, scene_pos: QPointF) -> StageBoxItem | None:
        box = self._box_at(scene_pos)
        return box if isinstance(box, StageBoxItem) else None

    def _role_of(self, box: StageBoxItem) -> str:
        return "attachment" if box.role in (EXTRA_ROLE, "note") else box.role

    def _drag_box_at(self, event) -> "StageBoxItem | None":
        return self._stage_box_at(self.mapToScene(event.position().toPoint()))

    # ---------------------------------------------------- 文件行的拖动会话
    #
    # `QDrag::exec()` 跑的是**自己的事件循环**：drop 事件是在那次 exec() 里面
    # 同步送到本视图的，而那个嵌套循环还会处理定时器与投递事件枝。于是"落手之后
    # 立刻重画事件枝页"（`set_event` → `scene.clear()`）会把**发起拖动的那个
    # FileRow 连同容器一起删掉**，而拖动循环还在跑 —— 之后它再去碰那个对象就是
    # 在使用已析构的内存。
    #
    # 所以主窗口那边把落库+刷新排到"下一次事件循环"，并且用这两个方法问一句
    # "这次拖放真的结束了吗"：只要还在拖，就再排一次（见 `_apply_file_role_move`）。
    # 这两个方法由 `FileRow.mouseMoveEvent` 在 `exec()` 前后成对调用（try/finally）。

    def begin_file_row_drag(self) -> None:
        """文件行开始自己那次拖放（按住一行往外拖）。"""
        self._file_row_drag = True

    def end_file_row_drag(self) -> None:
        """文件行那次拖放结束了（`exec()` 已经返回，栈上不再有拖动循环）。"""
        self._file_row_drag = False

    def is_file_row_dragging(self) -> bool:
        """现在还有文件行的拖放会话没结束吗？"""
        return self._file_row_drag

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls() or event.mimeData().hasFormat(FILE_ROW_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):  # noqa: N802
        if not (event.mimeData().hasUrls() or event.mimeData().hasFormat(FILE_ROW_MIME)):
            super().dragMoveEvent(event)
            return
        # 高亮光标底下那个方框（"松手会落在这里"）。**内部拖行**也一样高亮：
        # 用户要能看到自己正往哪个环节挪。
        box = self._drag_box_at(event)
        for candidate in self._boxes.values():
            candidate.set_drag_hover(candidate is box)
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):  # noqa: N802
        for box in self._boxes.values():
            box.set_drag_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event):  # noqa: N802
        for box in self._boxes.values():
            box.set_drag_hover(False)
        mime = event.mimeData()
        # 1) 事件枝页里的一行文件被拖到别的方框上 = 改这一行的环节（同一稿内，不新建稿）
        if mime.hasFormat(FILE_ROW_MIME):
            parsed = decode_file_row(bytes(mime.data(FILE_ROW_MIME)))
            if parsed is None:
                return
            version_id, file_id = parsed
            box = self._drag_box_at(event)
            if box is None:
                self.statusMessage.emit("把文件行拖到某个环节的方框里，才能改它的环节")
                return
            event.acceptProposedAction()
            self.fileRoleChangeRequested.emit(
                self.document_id, version_id, file_id, self._role_of(box)
            )
            return
        # 2) 从文件管理器拖文件进来 = 加进当前这一稿的这个环节
        if not mime.hasUrls():
            super().dropEvent(event)
            return
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        if not paths:
            return
        box = self._drag_box_at(event)
        if box is None:
            self.statusMessage.emit("请把文件拖到某个方框里")
            return
        event.acceptProposedAction()
        self.filesDropped.emit(self.document_id, self._role_of(box), paths)

    def _canvas_menu(self, event) -> bool:
        """右键空白处：整理排版 / 视图缩放（**不放调方框大小**，那是拖把手的事）。"""
        menu = QMenu(self)
        arrange = menu.addAction(
            "整理排版（按默认排法重排）",
            self.resetLayoutRequested.emit,
        )
        arrange.setEnabled(self.edit_layout)
        if not self.edit_layout:
            arrange.setToolTip("布局已锁定：先点工具栏的「编辑布局」")
        menu.addSeparator()
        menu.addAction("适应窗口", self.fit_to_content)
        menu.addAction("放大", self.zoom_in)
        menu.addAction("缩小", self.zoom_out)
        menu.exec(event.globalPos())
        return True

    def _box_role_menu(self, box: StageBoxItem, event) -> None:
        role = self._role_of(box)
        menu = QMenu(self)
        menu.addAction(
            import_icon(16), f"添加文件到「{box.label}」…",
            lambda: self.addFilesRequested.emit(self.document_id, role),
        )
        if box.role != "note":
            menu.addAction(
                info_icon(16), f"看「{box.label}」的年轮",
                lambda: self.stageFlowRequested.emit(self.document_id, role),
            )
        menu.exec(event.globalPos())

    def file_row_at(self, scene_pos: QPointF) -> FileRow | None:
        """**场景坐标**这个点底下有没有"文件行"（用来把右键还给那一行自己）。

        **不走 mapToGlobal/mapFromGlobal**：文件行是嵌在图元代理里的普通控件
        （`QScrollArea` → 容器 → 行），代理控件的 mapToGlobal/mapFromGlobal 在视图
        缩放后会跟它**真实画的位置对不上**（实测同一行的场景矩形，全局路径算出
        (429.9, 88.0, 265.2, 37.7)，代理路径算出 (342, 70, 266, 38) —— 后者才是画布
        真正画它的地方）。所以这里用代理自己的 `mapRectToScene`（入参是 **widget 坐标**，
        行在 scroll 里的位置靠 `row.mapTo(scroll, …)`，那是纯控件层级关系，不掺缩放），
        它对缩放是准的。
        """
        for box in list(self._boxes.values()):
            proxy = getattr(box, "proxy", None)
            scroll = getattr(box, "scroll", None)
            if proxy is None or scroll is None:
                continue
            for row in getattr(box, "files", ()):
                if not row.isVisible():
                    continue
                top_left = QPointF(row.mapTo(scroll, QPoint(0, 0)))
                rect = QRectF(top_left, QSizeF(row.width(), row.height()))
                if proxy.mapRectToScene(rect).contains(scene_pos):
                    return row
        return None

    def contextMenuEvent(self, event):  # noqa: N802
        """画布空白处 / 方框上右键。

        **点在某一行文件上时，菜单归那一行**：右键事件 Qt 只发给"代理里最外层那个
        控件"（这里是 ``QScrollArea``），**不会**再往下分给行 —— 所以行自己的
        ``contextMenuEvent`` 在真实点击里根本收不到（实测：真事件冒到画布这边，
        根本没有打开它所在目录的选项"）。菜单在这里按"落点底下是哪一行"补发。
        """
        scene_pos = self.mapToScene(event.pos())
        row = self.file_row_at(scene_pos)
        if os.environ.get("TOOAK_DEBUG_MENU"):
            self._dump_menu_hit(event, scene_pos, row)
        if row is not None:
            # 菜单弹在点击处：场景 → 视口 → 全局，全走标准视图接口
            # （代理控件自己的 mapToGlobal 在缩放后是偏的，不能拿它当菜单坐标）
            global_pos = self.viewport().mapToGlobal(self.mapFromScene(scene_pos))
            if row not in self._selected_rows:
                # 右键落在"没被选中"的行上：只选它（落在已选集合里则保持多选）
                self.select_file_row(row)
            rows = self.selected_file_rows()
            if len(rows) > 1:
                self.show_files_menu(rows, global_pos)
            else:
                self.show_file_menu(row.entry, global_pos, row.box)
            event.accept()
            return
        box = self._box_at(scene_pos)
        if box is None:
            self._canvas_menu(event)
            return
        # 右键只弹菜单，不顺手把方框点亮（布局锁着时尤其不该冒出蓝框）
        if self.edit_layout:
            self.select_box(box)
        if isinstance(box, StageBoxItem):
            self._box_role_menu(box, event)
        else:
            self._canvas_menu(event)

    def _dump_menu_hit(self, event, scene_pos: QPointF, row: FileRow | None) -> None:
        """右键命中排查（``TOOAK_DEBUG_MENU=1`` 时打到 stderr，launcher 会接进日志）。"""
        import sys

        viewport = event.pos()
        print(f"[menu-hit] global={event.globalPos().x()},{event.globalPos().y()} "
              f"视口={viewport.x()},{viewport.y()} "
              f"场景={scene_pos.x():.1f},{scene_pos.y():.1f} "
              f"m11={self.transform().m11():.4f} 命中行={row.name if row else None}",
              file=sys.stderr)
        for box in list(self._boxes.values()):
            proxy = getattr(box, "proxy", None)
            scroll = getattr(box, "scroll", None)
            if proxy is None or scroll is None:
                continue
            for candidate in getattr(box, "files", ()):
                top_left = QPointF(candidate.mapTo(scroll, QPoint(0, 0)))
                rect = QRectF(top_left, QSizeF(candidate.width(), candidate.height()))
                scene_rect = proxy.mapRectToScene(rect)
                print(f"  [menu-hit] box={box.role} row={candidate.name!r} "
                      f"visible={candidate.isVisible()} "
                      f"场景rect=({scene_rect.x():.1f},{scene_rect.y():.1f},"
                      f"{scene_rect.width():.1f},{scene_rect.height():.1f}) "
                      f"contains={scene_rect.contains(scene_pos)}", file=sys.stderr)

    def _set_selection(self, rows) -> None:
        """把高亮集合换成 ``rows``（一行一行地改 flag，只重画有变化的那几行）。"""
        new = set(rows)
        for item in list(self._selected_rows):
            if item not in new:
                item.set_selected(False)
        for item in new:
            item.set_selected(True)
        self._selected_rows = new

    def select_file_row(self, row: FileRow | None, *, toggle: bool = False,
                        extend: bool = False) -> None:
        """改文件行的选中状态。

        * 普通（单击 / 右键不落在已选集合里）：**只选这一行**，其余清掉；
        * ``toggle``（Ctrl 点）：把这一行加进/移出多选集合；
        * ``extend``（Shift 点）：从锚点连选到这一行（同一方框内的连续范围），
          其余清掉 —— 文件是分行摆的，"连选"只在一个方框里有意义。
        """
        if row is None:
            self._set_selection([])
            self._selected_row = None
            self._selection_anchor = None
            return
        if toggle:
            if row in self._selected_rows:
                self._selected_rows.discard(row)
                row.set_selected(False)
            else:
                self._selected_rows.add(row)
                row.set_selected(True)
                self._selection_anchor = row
            self._selected_row = row if row in self._selected_rows else (
                next(iter(self._selected_rows), None)
            )
            return
        if extend and self._selection_anchor is not None \
                and self._selection_anchor.box is row.box:
            rows = getattr(row.box, "files", [])
            try:
                a = rows.index(self._selection_anchor)
                b = rows.index(row)
            except ValueError:
                a = b = 0
            lo, hi = sorted((a, b))
            self._set_selection(rows[lo:hi + 1])
            self._selected_row = row
            return
        # 普通：只选这一行
        self._set_selection([row])
        self._selected_row = row
        self._selection_anchor = row

    def selected_file_rows(self) -> list[FileRow]:
        """当前多选的文件行（按方框、再按框内顺序排，给批量菜单/导出/删除用）。"""
        ordered: list[FileRow] = []
        for box in list(self._boxes.values()):
            for item in getattr(box, "files", ()):
                if item in self._selected_rows:
                    ordered.append(item)
        return ordered

    def show_file_menu(self, entry: FileEntry, global_pos, box: StageBoxItem | None = None) -> None:
        """文件行的右键菜单（照搬旧版 ToOak/Symphytum 的文件字段设计）。

        ``box`` 由 ``FileRow`` 自己传进来（它记着自己属于哪个方框）；
        只有拿不到时才退回「其他文件」—— 退回意味着 role 会变成 attachment，
        所以调用方一定要传。
        """
        box = box or self._boxes.get("extra") or next(iter(self._boxes.values()), None)
        if box is None:
            return
        role = self._role_of(box) if isinstance(box, StageBoxItem) else "attachment"
        menu = QMenu(self)
        menu.addAction(
            open_icon(16), "打开文件",
            lambda: self.fileOpenRequested.emit(entry.version_id, entry.file_id)
        )
        # 让我能够打开文件管理器里的文档所在目录"）—— 同一类动作放一起，
        # 中间不要隔一条分割线，不然找起来还得扫一遍菜单。
        # 文件已不在原处（引用模式下原文件被挪走/改名、或库里的 blob 丢了）：
        # （以前主窗口那条路遇到 `blob_path() is None` 是直接 return 的）。
        # 只认 "missing"：`changed`（就地被覆盖，引用模式的固有代价）文件还在，
        # 目录照样打得开，不该跟着一起灰掉。
        folder_action = menu.addAction(
            folder_icon(16), "打开所在文件夹",
            lambda: self.fileFolderRequested.emit(entry.version_id, entry.file_id),
        )
        if entry.status == "missing" or not self._file_exists(entry):
            folder_action.setEnabled(False)
            folder_action.setToolTip(
                "这份文件已不在原处：先用「重新绑定失效文件…」把它指回来，"
                "或用「导出到…」另存一份"
            )
        elif entry.in_library:
            folder_action.setToolTip(
                "这份文件已经复制进库；打开的是**库内 files/ 里它所在的那个目录**"
                "（原始导入目录在下面的「打开原始目录」）"
            )
        else:
            folder_action.setToolTip("在系统文件管理器里打开这份文件所在的目录")
        menu.addAction(
            export_icon(16), "导出到…",
            lambda: self.fileExportRequested.emit(entry.version_id, entry.file_id)
        )
        self._add_rename_action(menu, entry)
        menu.addSeparator()
        if entry.source_path:
            menu.addAction(
                folder_icon(16), "打开原始目录",
                lambda: self.fileOriginFolderRequested.emit(entry.version_id, entry.file_id),
            )
        menu.addSeparator()
        # 建议只保留后一个，取代的功能是右键文件菜单里，提供加入到某个版本-选择版本"）：
        # 两件事都发生在**同一个环节**（`role` 取的都是方框的 role），菜单上并排放着
        # 用户根本分不清该点哪个。留下"添加文件到「方框」…"这条更直白的；
        # 而"把这一份补进别的某一稿"改用下面那个**选稿号**的子菜单，语义清楚得多。
        self._add_into_version_menu(menu, entry, box)
        menu.addAction(
            import_icon(16), f"添加文件到「{box.label}」…",
            lambda: self.addFilesRequested.emit(self.document_id, role),
        )
        self._add_register_menu(menu, entry, box)
        menu.addSeparator()
        menu.addAction(
            info_icon(16), f"看「{box.label}」的年轮",
            lambda: self.stageFlowRequested.emit(self.document_id, role),
        )
        self._add_word_compare_menu(menu, entry)
        if entry.status:
            menu.addAction(
                "重新绑定失效文件…",
                lambda: self.rebindRequested.emit(entry.version_id),
            )
        if not entry.stale:
            menu.addSeparator()
            menu.addAction(
                prune_icon(16), "删除文件",
                lambda: self.filesDeleteRequested.emit(
                    self.document_id, [(entry.version_id, entry.file_id)]
                ),
            )
        menu.exec(global_pos)

    def _add_into_version_menu(self, menu: QMenu, entry: FileEntry,
                               box: StageBoxItem | None) -> None:
        """「加入到某一稿（复制一份）→」子菜单：把这一份文件**复制一份进指定的那一稿**。

        原来那条「加入到这一稿…」和「添加文件到「方框」…」做的是同一件事
        （都落在同一个环节），并排摆着只会让人犹豫；现在改成：
        补进**别的一稿**是这条子菜单的事（点哪一稿就落哪一稿，稿号写在字里），
        往**这一稿**加文件是「添加文件到「方框」…」的事。

        稿号由 ``versionTargets`` 提供（主窗口给的 ``[{id, seq, label}]``）。

        **哪一项该灰**：是**这一行自己登记的那一稿**（``entry.version_id``），
        我现在在 v2，不应该有右键里能把文件加入 v1 这稿的选项" —— 那一行本来就
        是 v1 的，往 v1 "再复制一份"只会被内核拒掉（"本来就登记在 v1 里"）。
        反过来，"正在看的这一稿"恰恰是**最该点**的那一项：未登记的行显示在
        v2 上、它属于 v1，点 v2 就是"这一稿也认下这份文件"（最初的
        「复制一份到这一稿」做的正是这件事），绝不能灰掉。
        """
        versions = []
        if isinstance(box, StageBoxItem):
            versions = list(self.version_targets().get("drafts") or [])
        if not versions:
            return
        submenu = menu.addMenu(twig_icon(16), "加入到某一稿（复制一份）→")
        submenu.setToolTip(
            "把这一份**复制一份**登记进指定的那一稿（内容相同、各存各的实体文件）。\n"
            "点哪一稿就落在哪一稿；它本来登记着的那一稿是灰的（再放一份没有意义）"
        )
        for draft in versions:
            version_id = int(draft.get("id") or 0)
            if not version_id:
                continue
            label = str(draft.get("label") or f"v{draft.get('seq')}")
            own = version_id == int(entry.version_id or 0)
            if own:
                suffix = "（它本来就登记在这里）"
            elif draft.get("is_current"):
                suffix = "（就是现在看的）"
            else:
                suffix = ""
            action = submenu.addAction(
                leaf_icon(16), f"v{draft.get('seq')} 这一稿{suffix}",
                lambda _checked=False, target=version_id:
                self.fileRegisterRequested.emit(entry.version_id, entry.file_id, target),
            )
            action.setToolTip(
                f"把「{entry.name}」复制一份到 v{draft.get('seq')}"
                + (f"（{label}）" if label else "")
                + ("；它本来就登记在这一稿里，再放一份没有意义" if own else "")
            )
            if own:
                action.setEnabled(False)

    def _add_register_menu(self, menu: QMenu, entry: FileEntry,
                           box: StageBoxItem | None) -> None:
        """「未登记」那一行的右键项：**把它复制一份登记到某一稿**。

        在文件库里面创建一个复制版本，作为最新的稿件（注意区分我可能选择的
        不一定是实际上的最新稿，我可能是把 V1 提到 V2，但 V3 又有文件了）"。

        只对**未登记**的那一行出现：这一行是"更早某一稿登记过的文件"，
        而用户正看的这一稿里没有它 —— 点一下就是"这一稿也认下这份文件"。
        已经登记在这一稿里的行不需要它（那行的右键里有的是"删掉/换环节"）。

        """
        if not entry.stale:
            return
        targets = self.version_targets()
        shown = targets.get("shown") or {}
        latest = targets.get("latest") or {}
        shown_id, latest_id = shown.get("id"), latest.get("id")
        shown_seq = shown.get("seq")
        if not shown_id or not shown_seq:
            return
        role_label = ""
        if isinstance(box, StageBoxItem):
            role_label = ROLE_LABELS.get(self._role_of(box), "")
        shown_is_latest = not latest_id or latest_id == shown_id
        own_id = int(entry.version_id or 0)

        # ① 加进**正在看的这一稿**（页面抬头写的那一稿）
        # 未登记的行属于**更早**某一稿，所以"正在看的这一稿"正常都不是它自己；
        # 但用户可能正翻着更早的稿看（那一行反而是更新的稿登记过的），
        # 这时它就是它自己那一稿 —— 内核会拒（"本来就登记在 v{seq} 里"），
        # 所以这里如实置灰，而不是让用户点一下吃个错误。
        add_here = menu.addAction(
            leaf_icon(16),
            (f"复制一份到最新稿（v{shown_seq}）…" if shown_is_latest
             else f"复制一份到这一稿（v{shown_seq}）…"),
            lambda: self.fileRegisterRequested.emit(
                entry.version_id, entry.file_id, shown_id
            ),
        )
        if own_id == int(shown_id):
            add_here.setEnabled(False)
            add_here.setToolTip(
                f"这一行本来就登记在 v{shown_seq} 里，不用再放一份"
            )
        else:
            add_here.setToolTip(
                f"把这一行（它登记在更早的那一稿里）另外登记到 v{shown_seq} 的"
                f"「{role_label}」里。\n"
                "内容是**真的复制一份进库**（库里 files/ 下有自己的那份文件）；\n"
                "不新建稿，你磁盘上的原文件一个字节都不动"
            )
        # ② 加进**真正的最新稿**（只有它不是正在看的那一稿时才多这一项）
        if not shown_is_latest and latest_id:
            add_latest = menu.addAction(
                leaf_icon(16), f"复制一份到最新稿（v{latest.get('seq')}）…",
                lambda: self.fileRegisterRequested.emit(
                    entry.version_id, entry.file_id, latest_id
                ),
            )
            if own_id == int(latest_id):
                # 翻着更早的稿看时，这一行可能正是最新稿登记的那一份：
                # "复制到最新稿"等于"复制到它自己那一稿"，没有意义
                add_latest.setEnabled(False)
                add_latest.setToolTip(
                    f"这一行本来就登记在 v{latest.get('seq')} 里（最新稿），"
                    "不用再放一份"
                )
            else:
                add_latest.setToolTip(
                    f"登记到**最新的一稿 v{latest.get('seq')}**"
                    f"（你现在看的是 v{shown_seq}，它才是最新稿）"
                )

    def _add_rename_action(self, menu: QMenu, entry: FileEntry) -> None:
        """

        为什么要有这一项：用户习惯每稿把文件名改得更贴合当期（"结构计算书-终版"），
        而库里的名字是归档那一刻定下的；以前只能"删掉这一行再拖一次文件"，
        那会把文件重新复制一份进库、还会丢掉这一笔的登记时间。

        两条必须写清（都在这段里）：

        * 改的是**库里怎么称呼它**，不是用户磁盘上那个文件 —— 引用模式下
          用户自己的文件原名一个字都不动；库内副本（复制进库的那份）会跟着改名，
          因为它就是库自己的文件；
        * ``stale`` 的行（"未登记"那种）属于**更早的那一稿**，改它名字改的是
          那一稿记的名字，所以菜单上直接把稿号写出来，别让人以为在改眼前这一稿。
        """
        if entry.version_seq and entry.version_seq != self.version_seq:
            label = f"改名（改 v{entry.version_seq} 里记的名字）…"
            tip = (f"这一行登记在 v{entry.version_seq} 里（眼下看的这一稿没有登记它），"
                   f"所以改的是 v{entry.version_seq} 记的名字。\n"
                   "只改库里记的名字，你磁盘上的原文件不动")
        else:
            label = "改名…"
            tip = ("给这一份文件在库里换个名字（这一稿的文件清单、导出、Word 比较里\n"
                   "都会跟着变）。引用模式下**你磁盘上的原文件不动**；\n"
                   "复制进库的那一份会把库内的实体文件一起改名")
        menu.addAction(
            pencil_icon(16), label,
            lambda: self.fileRenameRequested.emit(entry.version_id, entry.file_id),
        ).setToolTip(tip)

    def _add_word_compare_menu(self, menu: QMenu, entry: FileEntry) -> None:
        """「用 Word 比较」/「用 WPS 比较」子菜单：调**本机程序自己的**比较功能
        （审阅 → 比较 → 比较文档）。

        wps 比较功能的支持"，再后来是这一条："我想指定用 word/wps 比较呢，
        在菜单里做两个吧，没有就灰掉"。

        所以这里**一家一个入口**（不再是"用 Word 或 WPS 比较"那种合成一条、
        点下去由内核挑）：用户点哪一个，就是哪一个去干活 —— 两套都装着的机器上
        "到底谁跳出来"不该由程序猜。某一家没装就把它那一条**灰掉**并写明原因，
        另一条照常能点。

        四条纪律：

        * **不是 Word 文件就整块不出现**（不是灰着占地方）：一份 PDF / 图纸
          拿去调文字处理软件毫无意义，菜单里多一行只会让人点一下试试；
        * **没装的那一家出现但置灰**，提示里写明为什么 —— 用户至少知道
          "这个软件本来有这个功能，是我这台机器没装"，而不是以为功能不存在；
        * **指定了就只用它**（``engine=kind`` 传给内核，见 ``wordcompare.compare``）：
          那类问题，一次都不该再有；
        * **只列更早的稿**，每条都写清是哪一稿的哪一份文件（跨环节配上的还要写出角色）。
          比的是"这一稿相对上一稿改了什么"，反向比较交给底侧的「各稿」窗口。
        """
        info = self.word_compare_info(entry.version_id, entry.file_id)
        if not info or not info.get("is_word"):
            return
        candidates = list(info.get("candidates") or [])
        for engine in info.get("engines") or []:
            kind = str(engine.get("kind") or "")
            label = str(engine.get("label") or kind)
            submenu = menu.addMenu(compare_icon(16), f"用 {label} 比较")
            # 提示要挂在**父菜单里那一项**上（``menuAction``）：挂在 QMenu 上
            # 只在某些风格下才显示，用户把鼠标停在"用 Word 比较"这一行时
            # 看到的多半还是空的（实测就是这个样子）
            entry_action = submenu.menuAction()
            entry_action.setToolTip(
                f"调用本机 {label} 的「审阅 → 比较」，生成一份带修订标记的文档；\n"
                "库里的文件与你的原文件一个字节都不会动"
            )
            if not engine.get("available"):
                # 没装这一家：整条子菜单灰掉（点不动），提示里说清为什么。
                # 灰的是**父菜单里那一项**（``menuAction``）：QMenu 自己是弹出窗，
                # 只 QMenu.setEnabled 在有些风格下标题照样是黑的
                submenu.setEnabled(False)
                entry_action.setEnabled(False)
                entry_action.setToolTip(
                    str(engine.get("reason") or info.get("word_reason") or "")
                )
                continue
            if not info.get("word_available"):
                # 理论上走不到（这条可用说明至少有一家装着），留着兜底
                submenu.setEnabled(False)
                entry_action.setEnabled(False)
                entry_action.setToolTip(str(info.get("reason") or ""))
                continue
            if not candidates:
                empty = submenu.addAction("没有更早的稿可以比")
                empty.setEnabled(False)
                empty.setToolTip(info.get("reason") or "")
                continue
            for candidate in candidates[:MAX_WORD_COMPARE_ITEMS]:
                prefix = "与上一稿" if candidate.get("is_previous") else "与"
                item_label = f"{prefix} {candidate['label']} 比较"
                if candidate.get("when"):
                    item_label += f"（{candidate['when']}）"
                version_id = candidate["version_id"]
                action = submenu.addAction(
                    item_label,
                    lambda _checked=False, other=version_id, use=kind:
                    self.fileWordCompareRequested.emit(
                        entry.version_id, entry.file_id, other, use
                    ),
                )
                action.setToolTip(
                    f"以 {info.get('name', '')} 为修订版、{candidate['label']} 为原文，"
                    f"让 {label} 生成一份带修订标记的文档"
                )
            if len(candidates) > MAX_WORD_COMPARE_ITEMS:
                more = submenu.addAction(
                    f"……还有 {len(candidates) - MAX_WORD_COMPARE_ITEMS} 稿更早的"
                    "（用「看年轮」挑）"
                )
                more.setEnabled(False)

    def show_files_menu(self, rows: list[FileRow], global_pos) -> None:
        """**多选**文件行的右键菜单：只有批量动作（导出 / 删除）。

        单个文件的动作（打开 / 打开所在文件夹 / 改环节）对"一批"没有意义，
        （"ctrl,shift 批量点选后，右键进行删除、导出文件"）。
        """
        menu = QMenu(self)
        count = len(rows)
        menu.addAction(
            export_icon(16), f"导出 {count} 个文件到…",
            lambda: self.filesExportRequested.emit(
                [(item.entry.version_id, item.entry.file_id) for item in rows]
            ),
        )
        menu.addSeparator()
        menu.addAction(
            prune_icon(16), f"删除 {count} 个文件",
            lambda: self.filesDeleteRequested.emit(
                self.document_id,
                [(item.entry.version_id, item.entry.file_id) for item in rows],
            ),
        )
        menu.exec(global_pos)

    def mouseDoubleClickEvent(self, event):  # noqa: N802
        """双击：文件行 = **打开文件**；空白处 = 回到"适应全部"。

        右键里才有看文件历史的功能"，所以环节流程只留在方框/文件的右键菜单里。
        """
        if (self.itemAt(event.position().toPoint()) is None
                and event.button() == Qt.MouseButton.LeftButton):
            self.fit_to_content()
            self.zoomChanged.emit(zoom.current_zoom_percent(self))
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    # --------------------------------------------------------------- 绘制

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:  # noqa: N802
        """拖动 / 拉伸的时候把格子画出来（旧版也是这么提示"往哪一格放"的）。

        省着画：
        * 只在**可见区域**（``rect``）里画，画布再大也不会画出几万条线；
        * 格子缩小到屏幕上看不清时（一格 < 6 像素），只画每 ``MAJOR_EVERY`` 格的粗线，
          这样既不会糊成一片，也不用真去画那么多线。
        """
        super().drawBackground(painter, rect)
        if not self.edit_layout:
            return                      # 布局锁着就不画格子（更清爽，也省一遍画线）
        # 编辑布局时格子一直画：一来它是"框体视图"的参照，二来拖动时我们
        # **只重画方框那一块**，要是"只在拖动时画格子"就会出现"有的地方有、有的没有"
        scale = self.transform().m11() or 1.0
        minor_visible = UNIT * scale >= 6.0
        step = 1 if minor_visible else MAJOR_EVERY

        first_col = max(int((rect.left() - MARGIN) // UNIT) - 1, 0)
        last_col = int(math.ceil((rect.right() - MARGIN) / UNIT)) + 1
        first_row = max(int((rect.top() - MARGIN) // UNIT) - 1, 0)
        last_row = int(math.ceil((rect.bottom() - MARGIN) / UNIT)) + 1

        top = MARGIN + first_row * UNIT
        bottom = MARGIN + last_row * UNIT
        left = MARGIN + first_col * UNIT
        right = MARGIN + last_col * UNIT

        minor = QPen(QColor(GRID_MINOR))
        minor.setWidthF(1.0)
        major = QPen(QColor(GRID_MAJOR))
        major.setWidthF(1.0)
        for col in range(first_col - first_col % step, last_col + 1, step):
            x = MARGIN + col * UNIT
            painter.setPen(major if col % MAJOR_EVERY == 0 else minor)
            painter.drawLine(QPointF(x, top), QPointF(x, bottom))
        for row in range(first_row - first_row % step, last_row + 1, step):
            y = MARGIN + row * UNIT
            painter.setPen(major if row % MAJOR_EVERY == 0 else minor)
            painter.drawLine(QPointF(left, y), QPointF(right, y))

    # --------------------------------------------------------------- 缩放

    def fit_to_content(self) -> None:
        """缩放到"全部看得见"。

        注意：视图在 QStackedWidget 里还没切过来时，尺寸是假的（很小），
        这时 fitInView 会算出荒唐的缩放（实测 0.04 倍，字都看不见、把手也点不中）。
        所以先记下"待适应"，等真的有了正常尺寸（resizeEvent）再算。
        """
        rect = self._scene.itemsBoundingRect()
        if rect.isEmpty():
            return
        if self.viewport().width() < 160 or self.viewport().height() < 160:
            self._pending_fit = True
            return
        if self._fitting:
            return                      # 重入保护（见下）
        self._pending_fit = False
        # 适应之前先收掉滚动条：不然"出现滚动条 → 视口变小 → 再适应 → 滚动条消失"
        zoom.begin_stable_fit(self)
        self._fitting = True
        try:
            self.fitInView(rect.adjusted(-16, -16, 16, 16),
                           Qt.AspectRatioMode.KeepAspectRatio)
        finally:
            self._fitting = False
        self._auto_fit = True
        self.zoomChanged.emit(self.zoom_percent())

    def resizeEvent(self, event):  # noqa: N802
        """

        用户的原话："平时很少去动布局，能不能修改布局做一个统一的开关，
        平时不去响应拖动窗口之类的" —— 所以：
        * 平时（``follow_resize`` 关着）：只让视口变大变小，**不重算适应** ——
          内容原地不动、不重画，拖窗口/拖分隔条就不再一顿一顿的；
        * 打开「排版跟随窗口」后：尺寸一变就重新适应（以前的行为）；
          否则打开的瞬间内容可能在屏幕外。
        * 想立刻回到"全都看得见"：按「适应」或 Ctrl+0。
        """
        super().resizeEvent(event)
        if self._pending_fit or (self.follow_resize and self._auto_fit):
            self.fit_to_content()

    def zoom_in(self) -> None:
        self.set_zoom_percent(self.zoom_percent() * 1.2)

    def zoom_out(self) -> None:
        self.set_zoom_percent(self.zoom_percent() / 1.2)

    def zoom_percent(self) -> int:
        """当前缩放百分比（右下角控件显示的就是它）。"""
        return zoom.current_zoom_percent(self)

    def set_zoom_percent(self, percent: float) -> None:
        self._auto_fit = False
        zoom.end_stable_fit(self)        # 手动缩放：滚动条还给用户
        zoom.set_zoom_percent(self, percent)
        self.zoomChanged.emit(self.zoom_percent())

    def is_auto_fit(self) -> bool:
        """现在是不是"自适应画布"（右下角/右上角那个控件的"适应"亮着就是）。"""
        return self._auto_fit

    def reset_zoom(self) -> None:
        self.set_zoom_percent(100)

    def wheelEvent(self, event):  # noqa: N802
        """Ctrl+滚轮缩放（围绕鼠标）；不按 Ctrl 时，悬在文件框上就滚那个
        框里的文件列表，否则滚整个画布。"""
        if zoom.wheel_zoom(self, event):
            self._auto_fit = False    # Ctrl+滚轮是手动缩放
            self.zoomChanged.emit(self.zoom_percent())
            event.accept()
            return
        # 文件列表过长时，滚轮应当滚列表而不是画布：把这次滚轮转给
        # 鼠标底下那个方框的滚动区（QGraphicsView 默认不会往下传）。
        box = self._stage_box_at(self.mapToScene(event.position().toPoint()))
        if box is not None and box.scroll is not None:
            bar = box.scroll.verticalScrollBar()
            if bar.maximum() > bar.minimum():
                QApplication.sendEvent(box.scroll.viewport(), event)
                event.accept()
                return
        super().wheelEvent(event)

"""白板连线的走线计算：直角折线（含避让）与圆角折线。

为什么单独一个模块：这是**纯几何**，和 Qt 图元没关系 —— 难点全在
"从哪儿出去、在哪儿拐弯、怎么绕开挡在中间的卡片"，放在这里能用几个
折点直接测，不用起界面。

用户的要求是"如有连接线，要支持直角转弯，现在设计不好看"：
* ``rounded_polyline``：直角 + 小圆角（纯直角太硬）；
* ``route_orthogonal``：挑一条**不压到别的卡片**的直角走线 ——
  直着过去被挡住时，就从空隙里绕（像电路板走线那样）。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPainterPath

__all__ = ["ORTH_RADIUS", "ORTH_STUB", "CLEARANCE", "LINE_WIDTH",
           "rounded_polyline", "route_orthogonal", "orth_candidates",
           "polyline_length", "hits"]

#: 直角折线从端口先横着伸出去多长（免得贴着卡片就拐弯）
ORTH_STUB = 26.0
#: 直角转弯处的圆角半径（纯直角太硬，圆一点更像"路"）。
#: 16 是"看得出来圆了、又不至于像在走弧线"的档位：圆角把角切掉约 6.6px，
#: 比线宽（2.4px）明显；短线段会自动按半长收敛，不会把细缝画歪。
ORTH_RADIUS = 16.0
#: 走线离卡片至少留多少（刚好贴着卡片边看起来是"压着"）
CLEARANCE = 8.0
#: 画线用的线宽 —— 判"压没压到卡片"时要按它把折线当成有宽度的条
LINE_WIDTH = 3.0
#: 求"一张卡片和一条线的重叠范围"时用来起手的极大值
_INF = float("inf")


def rounded_polyline(points: list[QPointF], radius: float = ORTH_RADIUS) -> QPainterPath:
    """把一串折点画成**圆角折线**（直角转弯 + 小圆角）。

    只处理水平和竖直段（调用方给的就是直角折点）：每个拐点切掉 ``radius``
    再接一段圆弧。相邻段太短时按半长收敛，不会画出倒退的怪线。
    """
    path = QPainterPath()
    clean: list[QPointF] = []
    for point in points:
        if not clean or (abs(point.x() - clean[-1].x()) > 0.01
                         or abs(point.y() - clean[-1].y()) > 0.01):
            clean.append(QPointF(point))
    if len(clean) < 2:
        if clean:
            path.moveTo(clean[0])
        return path
    path.moveTo(clean[0])
    for index in range(1, len(clean) - 1):
        previous, corner, following = clean[index - 1], clean[index], clean[index + 1]
        in_len = math.hypot(corner.x() - previous.x(), corner.y() - previous.y())
        out_len = math.hypot(following.x() - corner.x(), following.y() - corner.y())
        cut = max(min(radius, in_len / 2, out_len / 2), 0.0)
        if cut <= 0.5:
            path.lineTo(corner)
            continue
        before = QPointF(
            corner.x() + (previous.x() - corner.x()) * cut / in_len,
            corner.y() + (previous.y() - corner.y()) * cut / in_len,
        )
        after = QPointF(
            corner.x() + (following.x() - corner.x()) * cut / out_len,
            corner.y() + (following.y() - corner.y()) * cut / out_len,
        )
        path.lineTo(before)
        path.quadTo(corner, after)
    path.lineTo(clean[-1])
    return path


def polyline_length(points: list[QPointF]) -> float:
    total = 0.0
    for index in range(1, len(points)):
        total += math.hypot(points[index].x() - points[index - 1].x(),
                            points[index].y() - points[index - 1].y())
    return total


def _channels(obstacles: list[QRectF], start: QPointF, end: QPointF,
              vertical: bool) -> list[float]:
    """候选"走线的通道"坐标：卡片之间的缝、以及整块卡片外面的空地。

    ``vertical=True`` 时给的是竖线可以放在哪些 x 上（卡片列的缝），
    否则是横线可以放在哪些 y 上（卡片行的缝）。离卡片留 ``CLEARANCE``。
    """
    lo, hi = (start.x(), end.x()) if vertical else (start.y(), end.y())
    low, high = min(lo, hi), max(lo, hi)
    values: list[float] = [(low + high) / 2]
    edges: list[float] = []
    for rect in obstacles:
        a, b = (rect.left(), rect.right()) if vertical else (rect.top(), rect.bottom())
        edges += [a - CLEARANCE, b + CLEARANCE]
    # 只留落在两端之间的通道，再排个序，走线尽量靠近中间（短、且好认）
    middle = (low + high) / 2
    inside = sorted((value for value in edges if low + 1.0 < value < high - 1.0),
                    key=lambda value: abs(value - middle))
    values += inside
    # 实在没缝：绕到整块卡片外面去
    if obstacles:
        tops = [rect.top() for rect in obstacles] if not vertical else []
        if not vertical and tops:
            values.append(min(tops) - CLEARANCE)
        right = [rect.right() for rect in obstacles] if vertical else []
        bottoms = [rect.bottom() for rect in obstacles] if not vertical else []
        if vertical and right:
            values.append(max(right) + CLEARANCE)
        if not vertical and bottoms:
            values.append(max(bottoms) + CLEARANCE)
    # 同一个通道坐标只留一次（保持原有顺序）。同一列/同一行的卡片给出的
    # left/right（top/bottom）是**同一个数**，不去重的话一条通道会被算几十遍：
    # 实测 56 张卡排成网格时一条线有 116 条候选、其中只有 20 条是真的不一样，
    # 多出来的 96 次完全是白算 —— 每次都要过一遍所有卡片。
    unique: list[float] = []
    seen: set[float] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def orth_candidates(start: QPointF, start_dir: int, end: QPointF, end_dir: int,
                    obstacles: list[QRectF] | None = None,
                    curve: float = 0.0, stub: float = ORTH_STUB) -> list[list[QPointF]]:
    """列出几种直角走线方案（每种都是横平竖直的折点串）。

    * **S 形**：从出口横着出去，在某个 x 上竖着拐一次，再横着进目标端口；
      ``curve`` 是用户拖手柄定下的偏移，优先试它；
    * **绕行**：从出口出去、竖着走到某个 y 通道、横着穿过整个距离、再进去
      —— 中间挡着别的卡片时用它（不然线会从卡片身上压过去）。
    """
    obstacles = obstacles or []
    result: list[list[QPointF]] = []
    if abs(start.y() - end.y()) < 0.5:
        result.append([start, end])                      # 同一行：一条直线就够

    # S 形：竖线放在哪儿
    for mid in _channels(obstacles, start, end, vertical=True):
        # **一律**写成横-竖-横的四点折线（哪怕中间那道竖线落在端点的 x 上）：
        # 只贴住一端的 mid 用 [start, end] 兜底会画出一条斜段，
        # 竖线正好落在端点 x 上时这四点里有重合点，圆角折线自己会去重。
        result.append([start, QPointF(mid, start.y()), QPointF(mid, end.y()), end])

    # 绕行：横线放在哪儿（先从端口出去一截，免得贴着自己的卡片拐弯）
    out_x = start.x() + start_dir * stub
    in_x = end.x() + end_dir * stub
    for channel in _channels(obstacles, start, end, vertical=False):
        result.append([
            start,
            QPointF(out_x, start.y()),
            QPointF(out_x, channel),
            QPointF(in_x, channel),
            QPointF(in_x, end.y()),
            end,
        ])

    if curve:
        # 用户拖过手柄：把他定的位置排在最前面（干净的话就用他的）
        base = (start.x() + end.x()) / 2
        low, high = sorted((start.x(), end.x()))
        mid = max(low + stub * 0.5, min(high - stub * 0.5, base + curve))
        preferred = [start, QPointF(mid, start.y()), QPointF(mid, end.y()), end]
        result = [preferred] + [points for points in result if points != preferred]
    return result


def route_orthogonal(start: QPointF, start_dir: int, end: QPointF, end_dir: int,
                     obstacles: list[QRectF] | None = None,
                     curve: float = 0.0, stub: float = ORTH_STUB) -> list[QPointF]:
    """挑一条**尽量不压到别的卡片**的直角走线。

    打分：先比"压到几张卡片"，再比总长度 —— 都不压时取最短的那条，
    于是常见的"同一行两张卡"就是一条直线，隔着卡片就用 S 形或绕行。
    """
    obstacles = [rect for rect in (obstacles or []) if rect.width() > 0.5]
    if not obstacles:
        # 没东西挡着：同一行就一条直线，不同行就走中间竖一道的 S 形
        # （**不能**直接连成斜线 —— 直角走线不许出现斜段）
        return orth_candidates(start, start_dir, end, end_dir, [], curve, stub)[0]
    # 先按长度排：干净方案里最短的那条就是最优解，所以碰到第一条干净的可以直接收工。
    # 卡片几十张、候选几十条，这样一次走线通常只算一两条候选就结束（拖卡片时不卡）。
    candidates = sorted(orth_candidates(start, start_dir, end, end_dir, obstacles,
                                        curve, stub),
                        key=polyline_length)
    best: tuple[tuple, list[QPointF]] | None = None
    for points in candidates:
        count, area = hits(points, obstacles)
        if count == 0:
            return points
        score = (count, area, polyline_length(points))
        if best is None or score < best[0]:
            best = (score, points)
    return best[1] if best else [start, end]


def _points_bounds(points: list[QPointF]) -> QRectF:
    """折点的外接框（粗筛用：连外接框都碰不到卡片就不用细算）。"""
    if not points:
        return QRectF()
    xs = [point.x() for point in points]
    ys = [point.y() for point in points]
    return QRectF(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _segment_boxes(points: list[QPointF],
                   half: float) -> list[tuple[float, float, float, float]]:
    """每一段线按线宽描成矩形，返回 ``(left, top, right, bottom)`` 的**纯浮点**元组。

    为什么是元组而不是 ``QRectF``：下面每算一条候选线都要拿"段 × 卡片"两两求交，
    这个内层循环的量级是几百次/条线 —— 用 ``QRectF`` 的话每次都要构造 Qt 对象、
    再过一次 C++ 调用，反而比原来那版描边求交还慢（实测慢 1.8 倍）；
    换成纯浮点算式才真的省下来。
    """
    boxes = []
    for index in range(1, len(points)):
        start, end = points[index - 1], points[index]
        x1, x2 = (start.x(), end.x()) if start.x() <= end.x() else (end.x(), start.x())
        y1, y2 = (start.y(), end.y()) if start.y() <= end.y() else (end.y(), start.y())
        boxes.append((x1 - half, y1 - half, x2 + half, y2 + half))
    return boxes


def hits(points: list[QPointF], obstacles: list[QRectF]) -> tuple[int, float]:
    """这条折线压到几张卡片、一共压了多少面积（擦一点点不算，要真的越过去）。

    返回 ``(张数, 面积)``：先比张数，再比面积 —— 避不开的时候，
    挑"擦得最少"的那条，别整条从卡片身上穿过去。

    这里刻意用**解析判断**（矩形相交）而不是原来的"QPainterPathStroker 描边 +
    intersected() 布尔求交"：走线是直角折线，每一段不是横的就是竖的，
    那么"这一段和这张卡片重叠多少"就是两个矩形求交，不需要构造两条 QPainterPath
    再做布尔运算。原来那套算一条候选线就要建一次描边路径、再逐张卡片做一次
    布尔求交，几十张卡 × 几十条候选时一次全量重算是几百毫秒
    （实测 56 卡 28 线 ~370ms、90 卡 45 线 ~1100ms），拖分组框逐帧重算就是卡死。
    """
    if not obstacles:
        return 0, 0.0
    # 折线的外接框都碰不到任何卡片时，直接算没压到（省掉逐段细算）
    box = _points_bounds(points).adjusted(-2.0, -2.0, 2.0, 2.0)
    near = [rect for rect in obstacles if rect.intersects(box)]
    if not near:
        return 0, 0.0
    # 关键：折线本身是"零面积"的，拿它和矩形求交永远是空 ——
    # 必须先按线宽（LINE_WIDTH）把它描成有宽度的条再求交
    segments = _segment_boxes(points, LINE_WIDTH / 2)
    if not segments:
        return 0, 0.0
    count = 0
    area = 0.0
    for rect in near:
        # 卡片往里缩 CLEARANCE/2：从卡片边上擦过去不算"压到"（保持原口径）
        left, top = rect.left() + CLEARANCE / 2, rect.top() + CLEARANCE / 2
        right, bottom = rect.right() - CLEARANCE / 2, rect.bottom() - CLEARANCE / 2
        if right <= left or bottom <= top:
            continue
        # 一张卡片上的重叠区域合起来算一块（折线可能进出这张卡两次）
        x1 = y1 = _INF
        x2 = y2 = -_INF
        for sx1, sy1, sx2, sy2 in segments:
            ix1 = left if left > sx1 else sx1
            ix2 = sx2 if sx2 < right else right
            if ix2 <= ix1:
                continue
            iy1 = top if top > sy1 else sy1
            iy2 = sy2 if sy2 < bottom else bottom
            if iy2 <= iy1:
                continue
            if ix1 < x1:
                x1 = ix1
            if iy1 < y1:
                y1 = iy1
            if ix2 > x2:
                x2 = ix2
            if iy2 > y2:
                y2 = iy2
        if x2 <= x1 or y2 <= y1:
            continue
        # 只是从拐角上蹭一下（重叠就一两个像素）不算压到
        if x2 - x1 > 1.5 and y2 - y1 > 1.5:
            count += 1
            area += (x2 - x1) * (y2 - y1)
    return count, area


def _hits(points: list[QPointF], obstacles: list[QRectF]) -> tuple[int, float]:
    """兼容旧名（内部用）。"""
    return hits(points, obstacles)

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

__all__ = ["ORTH_RADIUS", "ORTH_STUB", "CLEARANCE", "LINE_WIDTH", "LANE_STEP",
           "BEND_COST", "OVERLAP_WEIGHT", "MAX_CHANNELS", "rounded_polyline",
           "route_orthogonal", "orth_candidates", "corridor_range",
           "same_side_corridor", "port_corridor", "side_room",
           "simplify_polyline", "polyline_length", "polyline_cost", "hits",
           "overlap_length"]

#: 直角折线从端口先横着伸出去多长（免得贴着卡片就拐弯）
ORTH_STUB = 26.0
#: 直角转弯处的圆角半径（纯直角太硬，圆一点更像"路"）。
#: 16 是"看得出来圆了、又不至于像在走弧线"的档位：圆角把角切掉约 6.6px，
#: 比线宽（2.4px）明显；短线段会自动按半长收敛，不会把细缝画歪。
ORTH_RADIUS = 16.0
#: 走线离卡片至少留多少（刚好贴着卡片边看起来是"压着"）
CLEARANCE = 8.0
#: 绕到"整块卡片外面"时离最外侧那张卡多远。
#: 用 stub 那个量级（26）而不是 CLEARANCE（8）：贴着块边 8px 走的线会从
#: 那一整行/列卡片的端口圆点旁边擦过去，看着像连到了它们身上。
OUTSIDE = ORTH_STUB
#: 同一条缝里"并排两条线"之间的间距。
#: 两条线都只能走这条缝时，一条走正中、另一条让开一格 —— 叠在一起画出来
#: 就是一条线，用户会以为只连了一处（``overlap_length`` 也按这个来罚）。
LANE_STEP = 8.0
#: 算"两条线叠在一起"时允许的偏差。很小是**故意的**：只有真的画在同一条线上
#: 才算重叠，否则"恰好都往这一带拐"的线会互相躲来躲去（用户看到的反而是乱跳）。
OVERLAP_TOLERANCE = 1.2
#: 重叠折算成长度的比例（"重叠这么多像素 ≈ 多绕这么长的路"）。
#: 为什么不是"只要不重叠就优先"：端口一边只有一个，**两条线接到同一张卡上**时，
#: 靠端口那一截必然重合。按"绝不重叠"来挑，就会为了一段本来就躲不掉的重叠，
#: 把一条 290px 的直线拽成 466px 的绕行 —— 用户看到的就是
#: 就不划算了（直线留着），而"两条线整段叠在一起"这种（重叠好几百像素）
#: 仍然会去走旁边那条车道。
OVERLAP_WEIGHT = 0.5
#: 每个拐弯按多少像素长度来算（"长度 + 拐弯数 × 这个值"）。
#: 为什么需要它：两端面对面的走线里，**所有 Z 形候选的总长一模一样**
#: （中间那道竖线放在两个端口之间的任何位置，两段横线加起来都是同一个数），
#: 于是"绕一下再拐回来"的走法会和干净的 Z 形打平 —— 打平之后谁在前全看
#: 浮点尾数，画出来就是"偶尔多绕一个没用的弯"。给拐弯记一点代价，
#: 干净的 Z 形（2 个弯）就稳稳压过 6 折点那种（4 个弯）。
BEND_COST = 12.0
#: 一条通道最多留几个候选（按离弦的远近挑）。
#: 上限是给"拖卡片"留的余量：每条候选都要拿所有卡片判一遍"压没压到"。
MAX_CHANNELS = 8
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


def polyline_cost(points: list[QPointF]) -> float:
    """走线的"代价"：总长度 + 拐弯数 × ``BEND_COST``。

    挑走线时用它代替纯长度（原因见 ``BEND_COST`` 那段）：折点越多说明绕得越花，
    直线 / 一道弯的走法就不会莫名其妙输给"绕一圈再拐回来"的走法。
    """
    return polyline_length(points) + max(len(points) - 2, 0) * BEND_COST


def simplify_polyline(points: list[QPointF]) -> list[QPointF]:
    """去掉重合点与"同一条直线上的多余拐点"。

    拼候选走线时会先按"出端口 → 拐 → 再拐 → 进端口"写死若干点，
    其中有些会落在同一直线上（比如用户拖过的那道弯正好和 stub 同 x）——
    留着它们不影响几何，但会让"折点个数"这种判据变得没法读，
    也会让圆角折线在直段中间白切一个圆角。
    """
    clean: list[QPointF] = []
    for point in points:
        if not clean or (abs(point.x() - clean[-1].x()) > 0.01
                         or abs(point.y() - clean[-1].y()) > 0.01):
            clean.append(QPointF(point))
    if len(clean) < 3:
        return clean
    result = [clean[0]]
    for index in range(1, len(clean) - 1):
        before, here, after = result[-1], clean[index], clean[index + 1]
        vertical = abs(before.x() - here.x()) < 0.01 and abs(here.x() - after.x()) < 0.01
        horizontal = abs(before.y() - here.y()) < 0.01 and abs(here.y() - after.y()) < 0.01
        if vertical or horizontal:
            continue                      # 直着走过去的中间点：删掉
        result.append(here)
    result.append(clean[-1])
    return result


def corridor_range(start: QPointF, start_dir: int, end: QPointF,
                    end_dir: int, stub: float = ORTH_STUB) -> tuple[float, float]:
    """那道竖线（走廊）**允许**落在哪个 x 区间里。

    规矩只有一条：两端都必须"先出端口、再拐弯"。为什么非要有这个限制 ——
    竖线如果落在自己卡片的内侧，第一段横线就会从端口**穿回自己的卡片里**。
    实测（同一列上下两张卡片，直角走线）：

        下卡左端口 (1490,360) → (1594,360) → (1594,234) → 上卡右端口 (1698,234)

    这条线从下卡的左端口出发、横穿下卡整个身子，再从上卡内部钻到它的右边缘 ——
    画出来就是"一条线压着两张卡片、末端顶在右边"，用户看到的是
    """
    out_x = start.x() + start_dir * stub
    in_x = end.x() + end_dir * stub
    if start_dir == end_dir:
        near = max(start.x(), end.x()) if start_dir > 0 else min(start.x(), end.x())
        edge = near + start_dir * CLEARANCE
        return (edge, float("inf")) if start_dir > 0 else (float("-inf"), edge)
    low, high = sorted((out_x, in_x))
    if low >= high:
        # 两张卡挨得太近（缝比两条 stub 还短）：没有"中间"可走，
        # 退化成一条横线，走廊就给弦的中点
        middle = (start.x() + end.x()) / 2
        return middle, middle
    return low, high


def same_side_corridor(start: QPointF, start_dir: int, end: QPointF,
                       obstacles: list[QRectF], stub: float = ORTH_STUB,
                       reach: float = ORTH_STUB * 2) -> float:
    """两端同侧（上下相邻的一对）时那道竖线该放在哪个 x 上。

    优先落在**自己与这一侧最近那张卡的中线**上 —— 也就是"这条缝的正中"：
    离自己那一列与隔壁那一列各半个缝（34px 的缝就是各 17px）。
    贴着自己卡片的边 +8px 走的那种，正好从整列卡片的端口圆点旁边擦过去，
    看起来就像连到了隔壁那些卡片上；走在缝正中既远得看得出来，
    多行分布时一整列的"⊐"也都落在同一条线上，整齐。

    这一侧本来就是空地（后面再没有卡片），或者最近的卡片远得没有意义
    （超过 ``reach``）时，退到 stub 外面一点：那时"缝的正中"可能在几百像素外，
    线会绕得莫名其妙。
    """
    near = max(start.x(), end.x()) if start_dir > 0 else min(start.x(), end.x())
    best: float | None = None
    for rect in obstacles:
        edge = rect.left() if start_dir > 0 else rect.right()
        gap = (edge - near) if start_dir > 0 else (near - edge)
        if gap <= CLEARANCE or gap > reach:
            continue
        centre = (near + edge) / 2
        if best is None or abs(centre - near) < abs(best - near):
            best = centre
    return near + start_dir * stub if best is None else best


def _lane_variants(value: float, obstacles: list[QRectF],
                   step: float = LANE_STEP) -> list[float]:
    """一条走廊左右各让一格（剩下的就是"并排的车道"）。

    让开之后不许落进卡片里（按 x 轴判：这条走廊是竖着走的）。一条都不合适时
    老老实实返回原来那条 —— 挤的地方宁可叠着，也不能让线从卡片上穿过去。
    """
    result = []
    for candidate in (value, value - step, value + step):
        if all(candidate <= rect.left() - CLEARANCE
               or candidate >= rect.right() + CLEARANCE for rect in obstacles):
            result.append(candidate)
    return result or [value]


def port_corridor(point: QPointF, direction: int, obstacles: list[QRectF],
                  stub: float = ORTH_STUB) -> float:
    """从某个端口"横着出去"时，外面那道竖线放在哪个 x 上。

    就是 ``same_side_corridor`` 的单端口版本（绕行走线两端的竖线都要用它）：
    落在这一侧那条缝的正中，而不是"离端口固定 26px"—— 固定距离在 34px 的
    """
    return same_side_corridor(point, direction, point, obstacles, stub)


def _channels(obstacles: list[QRectF], start: QPointF, end: QPointF,
              vertical: bool) -> list[float]:
    """候选"走线的通道"坐标：**每段空当的正中间**，以及整块卡片外面的空地。

    ``vertical=True`` 时给的是竖线可以放在哪些 x 上（卡片列之间的空当），
    否则是横线可以放在哪些 y 上（卡片行之间的空当）。

    多行网格里卡片之间只有 34px 的缝，贴边走的线离卡片只有 8px ——
    它正好从整列卡片的端口圆点旁边擦过去，**看起来就像连到了那些卡片上**
    既远得看得出来、又左右对称，多行分布时整片线也整齐。
    """
    if vertical:
        low, high = sorted((start.x(), end.x()))          # 走廊候选落在哪段 x 之间
        band_low, band_high = sorted((start.y(), end.y()))  # 这条线跨过的 y 范围
        band = [rect for rect in obstacles
                if rect.top() <= band_high and rect.bottom() >= band_low]
    else:
        low, high = sorted((start.y(), end.y()))
        band_low, band_high = sorted((start.x(), end.x()))
        band = [rect for rect in obstacles
                if rect.left() <= band_high and rect.right() >= band_low]
    values: list[float] = [(low + high) / 2]
    # **只看这条线真的会穿过的那一条带**（竖线看它跨的那段 y，横线看跨的那段 x）：
    # 别的行/别的列上的卡片跟这条通道没关系，把它们一起并进来，会把本来存在的
    # 缝"并"没了 —— 实测就是这么漏掉车道的：网格上方那几张孤立卡片在 x 上盖住了
    # 整列，于是列与列之间的缝算不出来，一条线只剩一个候选，两条线只能叠在一起。
    spans = sorted(
        ((rect.left(), rect.right()) if vertical else (rect.top(), rect.bottom()))
        for rect in band
    )
    merged: list[list[float]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1] + 0.5:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for index in range(len(merged) + 1):
        before = merged[index - 1][1] if index else None
        after = merged[index][0] if index < len(merged) else None
        if before is None and after is None:
            continue                      # 一张卡都没有：中间那条已经加过了
        if before is None:
            centre = after - OUTSIDE       # 整块卡片的左边
        elif after is None:
            centre = before + OUTSIDE      # 整块卡片的右边
        else:
            centre = (before + after) / 2  # 两张卡中间那条缝的正中
        values.append(centre)
        # 缝够宽时再多给两条"并排车道"：两条线都要走这条缝时，一条走正中、
        # 另一条让开一格，不要叠在一起（叠起来看着像只有一条线）。
        # 让出去之后还得离卡片留够 CLEARANCE，缝太窄就不给车道了。
        sides = [centre - before] if before is not None else []
        if after is not None:
            sides.append(after - centre)
        half = min(sides) if sides else LANE_STEP + CLEARANCE
        if half >= LANE_STEP + CLEARANCE:
            values += [centre - LANE_STEP, centre + LANE_STEP]

    # 同一个通道坐标只留一次。同一列/同一行的卡片给出的 left/right（top/bottom）
    # 经常是同一个数，不去重的话一条通道会被算几十遍：实测 56 张卡排成网格时
    # 一条线有 116 条候选、其中只有 20 条是真的不一样。
    # 排完再**按离弦的远近截断**：候选越多越准，但每条都要过一遍所有卡片，
    # 几十张卡时这是拖影的来源（实测候选从 18 条涨到 60 条，整块重算慢 6 倍）。
    middle = (low + high) / 2
    unique: list[float] = []
    for value in sorted(values, key=lambda value: abs(value - middle)):
        if value not in unique:
            unique.append(value)
    return unique[:MAX_CHANNELS]


def orth_candidates(start: QPointF, start_dir: int, end: QPointF, end_dir: int,
                    obstacles: list[QRectF] | None = None,
                    curve: float = 0.0, stub: float = ORTH_STUB,
                    lanes: bool = False) -> list[list[QPointF]]:
    """列出几种直角走线方案（每种都是横平竖直的折点串）。

    * **直线**：两端同一行、端口面对面、中间没人挡着 —— 一条横线就够；
    * **Z 形 / ⊐ 形**：先横着出端口，在某条通道上拐一道竖线，再横着进目标端口。
      通道夹在两条 stub 之外（见 ``corridor_range``），所以**任何一条候选都不会
      从自己两端的卡片身上穿过去**；``curve`` 是用户拖手柄定下的偏移，优先试它；
    * **绕行（U 形）**：从出口出去、竖着走到某条横向通道、横着穿过整个距离、再进去
      —— 中间挡着别的卡片时用它（比如同一行里夹着两张卡）。

    :param lanes: 再多给几组"并排车道"（同一条缝里左右各让一格）。默认不开：
        候选条数会翻好几倍，而绝大多数连线不挤。``route_orthogonal`` 只在
        "又干净又不压别人的线"一条都挑不出来时才回来要这一批。
    """
    obstacles = obstacles or []
    result: list[list[QPointF]] = []
    out_x = start.x() + start_dir * stub
    in_x = end.x() + end_dir * stub
    low, high = corridor_range(start, start_dir, end, end_dir, stub)

    def inside(value: float) -> bool:
        return low - 0.5 <= value <= high + 0.5

    # 同一行、端口面对面：一条直线就够（不要为了"直角"凭空拐一下）
    if (abs(start.y() - end.y()) < 0.5 and start_dir == -end_dir
            and (end.x() - start.x()) * start_dir > 0):
        result.append([start, end])

    # Z 形 / ⊐ 形：竖线放在哪条通道上（一定在两端卡片外面）
    if start_dir == end_dir:
        # 两端同侧（上下相邻的一对）：走成一道"⊐"，竖线落在这一侧的缝正中；
        # 这一侧是空地时退到 stub 外面一点。别的通道不合适 —— 这条走线要的是
        # "贴着这一列整齐地绕过去"，不是"找一条最短的缝钻过去"。
        corridor = same_side_corridor(start, start_dir, end, obstacles, stub)
        channels = [corridor, corridor - LANE_STEP, corridor + LANE_STEP]
        channels = [value for value in channels if inside(value)]
    else:
        channels = [value for value in _channels(obstacles, start, end, True)
                    if inside(value)]
    if curve:
        # 用户拖过手柄：把他定的位置排在最前面（干净的话就用他的）
        base = (start.x() + end.x()) / 2
        preferred = max(low, min(high, base + curve))
        channels = [preferred] + [value for value in channels if value != preferred]
    for mid in channels:
        result.append(simplify_polyline([
            start,
            QPointF(out_x, start.y()),
            QPointF(mid, start.y()),
            QPointF(mid, end.y()),
            QPointF(in_x, end.y()),
            end,
        ]))

    # 绕行（U 形）：横线放在哪条通道上，两端各从**这一侧的缝正中**竖着出去
    # （不贴着端口固定 26px：那样竖线离隔壁那一列的端口圆点只有几个像素）。
    # 两道竖线几乎重合的"U 形"直接不要：那和一条 Z 形一模一样，只是白多两个弯
    # （画出来是一道 1px 的台阶，很难看，也让人以为线拐了两次）。
    out_run = port_corridor(start, start_dir, obstacles, stub)
    in_run = port_corridor(end, end_dir, obstacles, stub)
    if abs(out_run - in_run) >= 2.0:
        # 车道只试"一次挪一头"：两头一起挪会多出 9 种组合 × 每条通道，
        # 几十张卡时整块重算会翻好几倍（实测慢到 700ms 以上）。
        # 一条线只要有一头让开，就不会和别人的竖线叠在同一条线上。
        combos = [(out_run, in_run)]
        if lanes:
            combos += [(value, in_run)
                       for value in _lane_variants(out_run, obstacles)
                       if value != out_run]
            combos += [(out_run, value)
                       for value in _lane_variants(in_run, obstacles)
                       if value != in_run]
        for channel in _channels(obstacles, start, end, False):
            for left_run, right_run in combos:
                result.append(simplify_polyline([
                    start,
                    QPointF(left_run, start.y()),
                    QPointF(left_run, channel),
                    QPointF(right_run, channel),
                    QPointF(right_run, end.y()),
                    end,
                ]))
    return result


def side_room(rect: QRectF, obstacles: list[QRectF], limit: float = 400.0) -> tuple[float, float]:
    """这张卡片左右两侧各有多空：返回 ``(左边空地, 右边空地)``。

    用来决定"上下相邻的两张卡片"该从哪一侧绕（见 ``_EdgeItem._endpoints``）：
    绕到空地大的那一侧，线才不会贴着隔壁那一列卡片的端口圆点走。
    一条缝再宽也没意义，所以超过 ``limit`` 就按 ``limit`` 算。
    """
    left_room = limit
    right_room = limit
    for other in obstacles:
        if other.right() <= rect.left():
            left_room = min(left_room, rect.left() - other.right())
        elif other.left() >= rect.right():
            right_room = min(right_room, other.left() - rect.right())
    return left_room, right_room


def overlap_length(points: list[QPointF], others: list[list[QPointF]]) -> float:
    """这条折线和**已经画好的别的线**重叠了多长（同一条直线上压在一起的部分）。

    为什么要算：两条线都能走"这一行下面那条缝"时，它们会画在**同一条**横线上，
    看起来只有一条线 —— 用户会以为某一处根本没连上（"排布不合适"）。
    让一条走缝的正中、另一条让开一格（``LANE_STEP``），各走各的车道才看得出条数。
    """
    if not others:
        return 0.0
    total = 0.0
    for index in range(1, len(points)):
        a, b = points[index - 1], points[index]
        if abs(a.x() - b.x()) < 0.01 and abs(a.y() - b.y()) < 0.01:
            continue
        for other in others:
            total += _segment_overlap(a, b, other)
    return total


def _segment_overlap(a: QPointF, b: QPointF, other: list[QPointF]) -> float:
    """一段线和一个折线里"共线重叠"的长度（横的跟横的比、竖的跟竖的比）。"""
    vertical = abs(a.x() - b.x()) < 0.01
    low, high = (a.y(), b.y()) if vertical else (a.x(), b.x())
    low, high = min(low, high), max(low, high)
    fixed = a.x() if vertical else a.y()
    total = 0.0
    for index in range(1, len(other)):
        c, d = other[index - 1], other[index]
        if abs(c.x() - d.x()) < 0.01 and abs(c.y() - d.y()) < 0.01:
            continue
        if (abs(c.x() - d.x()) < 0.01) != vertical:
            continue                                   # 一横一竖：只交一点，不算重叠
        if abs((c.x() if vertical else c.y()) - fixed) > OVERLAP_TOLERANCE:
            continue                                   # 不在同一条线上：不算重叠
        start, end = ((c.y(), d.y()) if vertical else (c.x(), d.x()))
        start, end = min(start, end), max(start, end)
        if end < low or start > high:
            continue
        total += max(0.0, min(high, end) - max(low, start))
    return total


def route_orthogonal(start: QPointF, start_dir: int, end: QPointF, end_dir: int,
                     obstacles: list[QRectF] | None = None,
                     curve: float = 0.0, stub: float = ORTH_STUB,
                     avoid: list[list[QPointF]] | None = None) -> list[QPointF]:
    """挑一条**尽量不压到别的卡片、也不和别的线叠在一起**的直角走线。

    打分：压到几张卡片 → 压了多少面积 → ``代价 + 重叠 × OVERLAP_WEIGHT``
    （代价 = 总长度 + 拐弯数 × ``BEND_COST``）。
    "压到卡片"排在最前，是因为那会让人以为连到了别的节点上；
    "重叠"按比例折进代价里，而不是"绝不重叠"—— 端口一边只有一个，
    两条线接到同一张卡上时靠端口那一截必然重合（见 ``OVERLAP_WEIGHT``）。

    :param avoid: 别的线**已经定稿**的折点（见 ``overlap_length``）。拖卡片时会传
        进来，让并排的线各走一条车道；给空列表就等于不管重叠。
    """
    obstacles = [rect for rect in (obstacles or []) if rect.width() > 0.5]
    others = [points for points in (avoid or []) if len(points) >= 2]
    if not obstacles:
        # 没东西挡着：同一行就一条直线，不同行就走中间竖一道的 Z 形
        # （**不能**直接连成斜线 —— 直角走线不许出现斜段）
        return orth_candidates(start, start_dir, end, end_dir, [], curve, stub)[0]

    def best_of(candidates: list[list[QPointF]]) -> tuple[list[QPointF] | None, bool]:
        """在候选里挑一条：返回（选中的走线, 是否"又干净又不压别人的线"）。

        先按代价排（干净方案里代价最低的就是最优解），碰到第一条"一张卡都不压、
        也不和别人的线叠"的直接收工 —— 卡片几十张、候选几十条时，一次走线通常
        只算一两条就结束（拖卡片时不卡）。这一条也确实是最优的：按代价排过序，
        后面那些的代价只会更高、惩罚又不会为负。
        """
        best: tuple[tuple, list[QPointF]] | None = None
        for points in sorted(candidates, key=polyline_cost):
            count, area = hits(points, obstacles)
            share = overlap_length(points, others)
            if count == 0 and not share:
                return points, True
            score = (count, area, polyline_cost(points) + share * OVERLAP_WEIGHT)
            if best is None or score < best[0]:
                best = (score, points)
        return (best[1] if best else None), False

    chosen, ideal = best_of(orth_candidates(start, start_dir, end, end_dir,
                                            obstacles, curve, stub))
    if ideal:
        return chosen                              # 又干净又不压别人的线：就是它
    # 一条理想的都挑不出来（挤的地方）：这时才回来要"并排车道"那一批，
    # 多花一点时间，换"两条线不要叠在同一条通道上"
    with_lanes, ideal = best_of(orth_candidates(start, start_dir, end, end_dir,
                                                obstacles, curve, stub, lanes=True))
    if ideal:
        return with_lanes
    return with_lanes or chosen or [start, end]


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

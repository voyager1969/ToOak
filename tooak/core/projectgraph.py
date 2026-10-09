"""项目全景图：把一个项目里的所有文档与它们之间的依赖关系画成一张图。

与 :mod:`versiongraph` 的分工
-----------------------------
* ``versiongraph``：**一份文档**的历史（时间轴 + 泳道），回答"它在不同时期长什么样"；
* ``projectgraph``：**一个项目**的全貌（文档 + 依赖边），回答"这些文档谁靠谁"。

两种布局
--------
``grouped``（默认）
    按节点（专业/子系统/阶段）分列，列内按标题排列。
    即使依赖边很少，也能一眼看清"这个项目有哪些文档、分别属于哪个部分"，
    因此对真实数据（依赖稀疏）更实用。

``layered``
    按依赖关系分层：只被依赖的排在最左，依赖别人的依次向右。
    这才是严格的依赖层次图；存在环时（历史数据里确实会有）会退化为
    "忽略回边"，并把环的数量如实报出来，而不是画成一团乱麻。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diffing import summarize_change
from .library import (
    DOC_STATE_LABELS,
    LINK_LABELS,
    REVIEW_STATE_LABELS,
    ROLE_LABELS,
    Library,
)
from pathlib import Path

from PySide6.QtCore import QRectF

from .util import to_local_str

__all__ = ["ProjectGraphNode", "ProjectGraphEdge", "ProjectGraphColumn",
           "ProjectGraph", "BoardCard", "BoardGroup",
           "DEFAULT_GROUP_W", "DEFAULT_GROUP_H",
           "build_project_graph", "LAYOUTS"]

#: 支持的版图布局
#: * ``manual``   节点位置由用户拖动决定（存库；一旦用户摆过就优先用它）
#: * ``grid``      白板：节点先按网格自动排开（没摆过时的默认）
#: * ``layered``  按连线方向分层：被指向的靠左
#: * ``grouped``  按分类节点分列（仅当你确实建了分类节点时才有意义）
LAYOUTS = ("manual", "grid", "layered", "grouped")

#: 网格布局的列数（白板的初始排布）
GRID_COLUMNS = 5

#: 版图上的连线 = **依赖**：``源 → 目标`` 读作"源依赖目标"。
#: （早期用户画的线叫"路径"，只有样式区别，语义一直是依赖，这里统一掉）
DEPENDENCY_KIND = "depends_on"
PATH_KIND = DEPENDENCY_KIND

#: 卡片尺寸（世界坐标）
CARD_W = 210.0
CARD_H = 92.0   # 标题 / 环节 / 状态 / 最近改动 / 依赖了谁 —— 五行，卡片上写全
#: 版图里"项目根卡片"的宽度（思维导图用）
ROOT_CARD_W = 168.0
#: 列宽 / 行高 / 场景留白
COLUMN_W = 250.0
ROW_H = CARD_H + 14.0   # 行距跟着卡片高度走，别把卡片叠在一起
MARGIN_X = 26.0
MARGIN_TOP = 62.0


@dataclass
class ProjectGraphNode:
    """图上的一份文档。"""

    document_id: int
    title: str
    node_id: int = 0
    node_name: str = ""
    version_count: int = 1
    latest_seq: int | None = None
    review_state: str = "draft"
    #: 事件枝状态：进行中 / 已完成 / 搁置（个人管理里唯一的状态语义）
    state: str = "active"
    done: bool = False
    progress: int | None = None
    people: str = ""
    #: 环节概览：``{"output": 1, "analysis": 3}``（最新版本里各环节的文件数）
    stages: dict[str, int] = field(default_factory=dict)
    #: 最近一稿相对上一稿改了什么（``+3 附件、-2 附件`` / ``主文件更新`` / ``创建``）
    latest_change: str = ""
    #: 最近一稿的时间（ISO 字符串，展示时转本地格式）
    latest_time: str = ""
    #: 布局结果
    column: int = 0
    row: int = 0
    x: float = 0.0
    y: float = 0.0
    #: 依赖相关度（出度 + 入度），用于视觉强调
    degree: int = 0
    #: 我依赖的那些事件枝里，有哪些已经出过比我这一稿更新的版本（"该跟一跟了"）
    stale_dependencies: list[str] = field(default_factory=list)
    #: 我依赖的事件枝（标题；卡片上直接标注，不用顺着线去数）
    depends_on: list[str] = field(default_factory=list)
    #: 依赖我的事件枝（标题；只在悬停提示里说）
    depended_by: list[str] = field(default_factory=list)

    @property
    def is_connected(self) -> bool:
        return self.degree > 0

    @property
    def needs_attention(self) -> bool:
        """被依赖的事件枝更新了、我还没跟 —— 版图上要挂个告警标志。"""
        return bool(self.stale_dependencies)

    @property
    def attention_text(self) -> str:
        if not self.stale_dependencies:
            return ""
        names = "、".join(self.stale_dependencies)
        return f"依赖的事件枝已更新：{names}（本事件枝还没出新的稿）"

    @property
    def dependency_text(self) -> str:
        """卡片上那一行"依赖：…"（被依赖方更新过的前面挂 ⚠）。"""
        if not self.depends_on:
            return ""
        stale = set(self.stale_dependencies)
        names = [f"⚠ {name}" if name in stale else name for name in self.depends_on]
        return "依赖：" + "、".join(names)

    @property
    def dependent_text(self) -> str:
        """谁依赖我（只有提示里用）。"""
        if not self.depended_by:
            return ""
        return "被 " + "、".join(self.depended_by) + " 依赖"

    @property
    def review_label(self) -> str:
        return REVIEW_STATE_LABELS.get(self.review_state, self.review_state)

    @property
    def state_label(self) -> str:
        return DOC_STATE_LABELS.get(self.state, self.state)

    @property
    def change_summary(self) -> str:
        """一行"最近改了什么"，例如 ``v8 · 07-06 · +3 附件``。"""
        if not self.latest_change:
            return ""
        stamp = ""
        if self.latest_time:
            stamp = to_local_str(self.latest_time, "%Y-%m-%d")
        bits = [f"v{self.latest_seq}" if self.latest_seq else "", stamp, self.latest_change]
        return " · ".join(bit for bit in bits if bit)

    @property
    def stage_summary(self) -> str:
        """一行环节概览，例如 ``输入 1 · 分析 3 · 输出 1``。"""
        if not self.stages:
            return ""
        order = ["input", "analysis", "output", "reference", "figure", "primary", "attachment"]
        parts = []
        for role in order:
            if role in self.stages:
                parts.append(f"{ROLE_LABELS.get(role, role)[:2]} {self.stages[role]}")
        for role, count in self.stages.items():
            if role not in order:
                parts.append(f"{role} {count}")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "node_id": self.node_id,
            "node_name": self.node_name,
            "version_count": self.version_count,
            "latest_seq": self.latest_seq,
            "review_state": self.review_state,
            "review_label": self.review_label,
            "state": self.state,
            "state_label": self.state_label,
            "stages": self.stages,
            "stage_summary": self.stage_summary,
            "done": self.done,
            "progress": self.progress,
            "people": self.people,
            "column": self.column,
            "row": self.row,
            "x": self.x,
            "y": self.y,
            "degree": self.degree,
            "stale_dependencies": self.stale_dependencies,
            "needs_attention": self.needs_attention,
            "depends_on": self.depends_on,
            "depended_by": self.depended_by,
        }


@dataclass
class ProjectGraphEdge:
    """文档之间的依赖关系。"""

    source_document_id: int      # 依赖方（下游）
    target_document_id: int      # 被依赖方（上游）
    kind: str = "depends_on"
    label: str = ""
    #: 弯曲程度（0 = 自动；正数 = 往上拱的幅度，场景像素）
    curve: float = 0.0

    @property
    def kind_label(self) -> str:
        return LINK_LABELS.get(self.kind, self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_document_id,
            "target": self.target_document_id,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "label": self.label,
            "curve": self.curve,
        }


@dataclass
class BoardCard:
    """白板上的一张卡片：说明框 / 文件素材 / 网页链接。

    三者只是"内容从哪来"不同（``kind``），位置、大小、编辑、删除、转为事件枝
    这一套行为完全一样 —— 所以一张表、一个图元类就够。
    卡片是**待归档的引用**：文件卡片只记路径，绝不复制、绝不移动用户的文件。
    """

    id: int
    kind: str = "note"          # 'note' | 'file' | 'link'
    x: float = 0.0
    y: float = 0.0
    width: float = 260.0
    height: float = 0.0         # 0 = 高度按内容自适应
    title: str = ""
    text: str = ""              # note 的正文；file/link 的备注
    path: str = ""
    url: str = ""
    origin: str = "user"
    color: str = ""

    @property
    def is_material(self) -> bool:
        """素材卡片（文件 / 链接）：白板上画叶子，可以"转为事件枝"。"""
        return self.kind in ("file", "link")

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.kind == "file" and self.path:
            return Path(self.path).name
        if self.kind == "link" and self.url:
            return self.url.split("//")[-1].split("/")[0] or self.url
        return "说明" if self.kind == "note" else "素材"

    def to_dict(self) -> dict[str, Any]:
        """给 Web 端画白板用。

        早期 :meth:`ProjectGraph.to_dict` 忘了带 cards/groups，Web 端拿到的版图里
        没有说明框、也没有分组框 —— 白板画出来就是一堆孤零零的卡片。
        """
        return {
            "id": self.id,
            "kind": self.kind,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "title": self.title,
            "display_title": self.display_title,
            "text": self.text,
            "path": self.path,
            "url": self.url,
            "origin": self.origin,
            "color": self.color,
            "is_material": self.is_material,
        }


#: 够先摆两三张卡片进去，之后往里拖事件枝还会自动变大
DEFAULT_GROUP_W = 520.0
DEFAULT_GROUP_H = 360.0


@dataclass
class BoardGroup:
    """白板上的分组：一个大框把若干事件枝框在一起，成员记在库里。"""

    id: int
    name: str = ""
    note: str = ""
    color: str = ""
    #: 组框自己的矩形（空组、或用户手工拖大拖小时用）
    x: float = 0.0
    y: float = 0.0
    w: float = 0.0
    h: float = 0.0
    #: True = 大小由用户定（不跟着成员收紧）；False = 框跟着成员收紧
    manual: bool = False
    document_ids: list[int] = field(default_factory=list)

    @property
    def title(self) -> str:
        count = len(self.document_ids)
        base = self.name or "未命名组"
        return f"{base}（{count} 个事件枝）" if count else base

    def to_dict(self) -> dict[str, Any]:
        """给 Web 端画白板用（分组的框、颜色、位置、成员）。"""
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "note": self.note,
            "color": self.color,
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "manual": self.manual,
            "document_ids": self.document_ids,
        }


@dataclass
class ProjectGraphColumn:
    """一列的标题（``grouped`` 布局里是节点名，``layered`` 里是层级号）。"""

    index: int
    title: str = ""
    subtitle: str = ""
    x: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "title": self.title,
                "subtitle": self.subtitle, "x": self.x}


@dataclass
class ProjectGraph:
    """一个项目的文档全景图（节点 + 边 + 布局）。"""

    project_id: int
    project_name: str = ""
    layout: str = "grouped"
    #: 白板上的卡片（说明框 / 文件素材 / 链接）
    cards: list[BoardCard] = field(default_factory=list)
    #: 白板上的分组
    groups: list[BoardGroup] = field(default_factory=list)
    nodes: list[ProjectGraphNode] = field(default_factory=list)
    edges: list[ProjectGraphEdge] = field(default_factory=list)
    columns: list[ProjectGraphColumn] = field(default_factory=list)
    width: float = 0.0
    height: float = 0.0
    cycle_count: int = 0
    isolated_count: int = 0
    #: 思维导图的根卡片（项目本身），其它布局为空
    roots: list[ProjectGraphNode] = field(default_factory=list)
    #: 节点位置是否来自用户的手工摆放（True 时不要再自动排版）
    manual: bool = False

    @property
    def notes(self) -> list[BoardCard]:
        """说明框（就是 kind='note' 的卡片，界面里仍这么叫）。"""
        return [card for card in self.cards if card.kind == "note"]

    @property
    def document_count(self) -> int:
        return len(self.nodes)

    @property
    def connected_count(self) -> int:
        return sum(1 for node in self.nodes if node.is_connected)

    def node_by_document(self, document_id: int) -> ProjectGraphNode | None:
        return next((item for item in self.nodes if item.document_id == document_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "project_name": self.project_name,
            "layout": self.layout,
            "document_count": self.document_count,
            "connected_count": self.connected_count,
            "isolated_count": self.isolated_count,
            "cycle_count": self.cycle_count,
            "manual": self.manual,
            "width": self.width,
            "height": self.height,
            # 白板上"人能写的东西"：说明框/素材卡片 与 分组框
            # （Web 端靠这两个键画白板，缺了它们就只剩事件枝卡片）
            "cards": [card.to_dict() for card in self.cards],
            "groups": [group.to_dict() for group in self.groups],
            "columns": [column.to_dict() for column in self.columns],
            "roots": [root.to_dict() for root in self.roots],
            "nodes": [node.to_dict() for node in self.nodes],
            "edges": [edge.to_dict() for edge in self.edges],
        }


def build_project_graph(
    library: Library, project_id: int, layout: str = "grouped"
) -> ProjectGraph:
    """构建项目全景图。

    :param layout: ``grouped``（按节点分列，默认）或 ``layered``（按依赖分层）。
    """
    project = library.get_project(project_id)
    graph = ProjectGraph(
        project_id=project_id,
        project_name=project.name if project else "",
        # 非法值统一回落到 ``grid``：早期这里回落成 ``"mindmap"`` ——
        # 那个名字既不在 LAYOUTS 里、也没有对应的布局分支，
        # 实际排的是 grid，却把一个非法名字报给前端（前端按名字选渲染方式会出错）。
        layout=layout if layout in LAYOUTS else "grid",
    )
    if project is None:
        return graph

    roots = library.node_tree(project_id)
    root_id = roots[0].id if roots else None

    nodes: list[ProjectGraphNode] = []
    for document in library.list_documents(
        root_id, include_descendants=True, include_archived=False
    ) if root_id is not None else []:
        node = library.get_node(document.node_id)
        latest = library.latest_version(document.id)
        stages: dict[str, int] = {}
        if latest is not None:
            for item in latest.files:
                stages[item.role] = stages.get(item.role, 0) + 1
        # 节点上直接写明"最近改了什么"：版图上就能看出哪个事件枝刚动过
        # （只比文件集合与指纹，不读内容，所以很快）
        latest_change = ""
        if latest is not None:
            history = library.version_history(document.id)
            index = next((i for i, item in enumerate(history) if item.id == latest.id), -1)
            previous = history[index - 1] if index > 0 else None
            latest_change = summarize_change(previous, latest)
        nodes.append(
            ProjectGraphNode(
                document_id=document.id,
                title=document.title,
                node_id=document.node_id,
                node_name=node.name if node else "",
                version_count=document.version_count,
                latest_seq=latest.seq if latest else None,
                review_state=latest.review_state if latest else "draft",
                state=document.state,
                done=document.is_done,
                progress=document.progress,
                people=document.people,
                stages=stages,
                latest_change=latest_change,
                latest_time=latest.created_at if latest else "",
            )
        )

    # 依赖边（只保留两端都在本项目里的）
    known = {node.document_id for node in nodes}
    for row in library.db.query(
        """
        SELECT l.from_document_id AS source, l.to_document_id AS target,
               l.kind, l.label, l.curve
          FROM document_links l
          JOIN documents f ON f.id = l.from_document_id
          JOIN nodes fn ON fn.id = f.node_id
         WHERE fn.project_id = ?
        """,
        (project_id,),
    ):
        if row["source"] in known and row["target"] in known:
            graph.edges.append(
                ProjectGraphEdge(int(row["source"]), int(row["target"]),
                                 row["kind"], row["label"],
                                 float(row["curve"] or 0.0))
            )

    # 卡片上要直接标注"我依赖谁"：标题从边上取，免得再去查库
    titles = {node.document_id: node.title for node in nodes}
    depends: dict[int, list[str]] = {}
    dependents: dict[int, list[str]] = {}
    for edge in graph.edges:
        source_title = titles.get(edge.source_document_id)
        target_title = titles.get(edge.target_document_id)
        if source_title and target_title:
            depends.setdefault(edge.source_document_id, []).append(target_title)
            dependents.setdefault(edge.target_document_id, []).append(source_title)
    alerts = library.dependency_alerts(project_id)
    for node in nodes:
        node.depends_on = sorted(set(depends.get(node.document_id, [])))
        node.depended_by = sorted(set(dependents.get(node.document_id, [])))
        node.stale_dependencies = list(alerts.get(node.document_id, []))

    degree: dict[int, int] = {}
    for edge in graph.edges:
        degree[edge.source_document_id] = degree.get(edge.source_document_id, 0) + 1
        degree[edge.target_document_id] = degree.get(edge.target_document_id, 0) + 1
    for node in nodes:
        node.degree = degree.get(node.document_id, 0)

    graph.nodes = nodes
    graph.isolated_count = sum(1 for node in nodes if not node.is_connected)

    # 白板上"人能写的东西"：说明文本框、分组（成员只留还在本项目里的）
    graph.cards = [
        BoardCard(id=int(row["id"]), kind=str(row["kind"] or "note"),
                  x=float(row["x"]), y=float(row["y"]), width=float(row["w"]),
                  height=float(row["h"]), title=str(row["title"] or ""),
                  text=str(row["text"] or ""), path=str(row["path"] or ""),
                  url=str(row["url"] or ""), origin=str(row["origin"] or "user"),
                  color=str(row["color"] or ""))
        for row in library.board_cards(project_id)
    ]

    for row in library.board_groups(project_id):
        members = [item for item in row["members"] if item in known]
        graph.groups.append(
            BoardGroup(id=int(row["id"]), name=str(row["name"] or ""),
                       note=str(row["note"] or ""), color=str(row["color"] or ""),
                       x=float(row["x"] or 0.0), y=float(row["y"] or 0.0),
                       w=float(row["w"] or 0.0), h=float(row["h"] or 0.0),
                       manual=bool(row["manual"]),
                       document_ids=members)
        )

    # 先算一遍自动布局（保证没有手工位置的节点也有合理坐标），
    # 再用用户摆过的位置覆盖 —— 用户自己摆的图不能被自动排版冲掉。
    # 白板：没有"项目根卡片"，也没有从项目放射出去的分支线 ——
    if graph.layout == "layered":
        _layout_layered(library, graph)
    elif graph.layout == "grouped":
        _layout_grouped(library, graph)
    else:
        _layout_grid(graph)

    stored = library.document_positions(project_id)
    if stored:
        for node in graph.nodes:
            position = stored.get(node.document_id)
            if position is not None:
                node.x, node.y = position
        graph.manual = True
        graph.layout = "manual"
        graph.roots = []
        graph.columns = []
        # 「手工摆过的」和「自动排的」混在一起时，自动排的那些**必须让开** ——
        # 否则新加的事件枝（还没位置）会正好压在用户摆好的卡片上
        _resolve_overlaps(graph, stored)
        _fit_scene(graph)
    return graph


#: 让位时一次挪多远（和网格布局一个节奏）
_SHIFT_X = CARD_W + 34.0
_SHIFT_Y = ROW_H


def _resolve_overlaps(graph: ProjectGraph,
                      stored: dict[int, tuple[float, float]]) -> None:
    """把"没有手工位置"的节点挪到不压别人的地方（手工位置一个都不动）。

    从原位置一圈圈往外找：右、下、右下、左、上… 找到第一个不压任何已放置节点的
    位置就用它。这样无论新事件枝是从哪儿建出来的（工具栏、拖文件、旧库导入），
    都不会叠在已有的卡片上。
    """
    placed: list[ProjectGraphNode] = []
    # 手工位置优先占位，谁也动不了它们
    for node in graph.nodes:
        if stored.get(node.document_id) is not None:
            placed.append(node)
    for node in graph.nodes:
        if stored.get(node.document_id) is not None:
            continue
        node.x, node.y = _first_free_spot(placed, node.x, node.y)
        placed.append(node)


def _first_free_spot(placed: list[ProjectGraphNode], x: float,
                     y: float) -> tuple[float, float]:
    """从 ``(x, y)`` 开始找一块放得下卡片、又不压 ``placed`` 的位置。"""
    def collides(cx: float, cy: float) -> bool:
        rect = QRectF(cx, cy, CARD_W, CARD_H)
        return any(rect.intersects(QRectF(other.x, other.y, CARD_W, CARD_H))
                   for other in placed)

    if not collides(x, y):
        return x, y
    for ring in range(1, 40):
        for dx, dy in ((ring, 0), (0, ring), (ring, ring), (-ring, 0),
                       (0, -ring), (-ring, ring), (ring, -ring), (-ring, -ring)):
            cx = max(x + dx * _SHIFT_X, 0.0)
            cy = max(y + dy * _SHIFT_Y, 0.0)
            if not collides(cx, cy):
                return cx, cy
    return x, y


def _fit_scene(graph: ProjectGraph) -> None:
    """按所有节点（含用户拖到任意位置的）算出场景尺寸。"""
    if not graph.nodes:
        return
    right = max(node.x + CARD_W for node in graph.nodes)
    bottom = max(node.y + CARD_H for node in graph.nodes)
    graph.width = max(graph.width, right + MARGIN_X)
    graph.height = max(graph.height, bottom + 40)


# ---------------------------------------------------------------------------
# 布局
# ---------------------------------------------------------------------------


def _layout_grid(graph: ProjectGraph) -> None:
    """白板式初始排布：把所有事件枝按网格铺开，之后交给用户自由拖动。

    """
    events = sorted(graph.nodes, key=lambda item: item.title)
    for index, node in enumerate(events):
        column = index % GRID_COLUMNS
        row = index // GRID_COLUMNS
        node.column = column
        node.row = row
        node.x = MARGIN_X + column * (CARD_W + 34)
        node.y = MARGIN_TOP + row * ROW_H

    rows = max((len(events) + GRID_COLUMNS - 1) // GRID_COLUMNS, 1)
    graph.roots = []
    graph.columns = []
    graph.width = MARGIN_X * 2 + min(len(events) or 1, GRID_COLUMNS) * (CARD_W + 34)
    graph.height = MARGIN_TOP + rows * ROW_H + 40


def _layout_grouped(library: Library, graph: ProjectGraph) -> None:
    """按节点（专业/子系统）分列：列内按标题顺序排。

    依赖稀疏时这是最有信息量的排法 —— 至少能看清项目的文档分布。
    """
    groups: dict[int, list[ProjectGraphNode]] = {}
    for node in graph.nodes:
        groups.setdefault(node.node_id, []).append(node)

    ordered: list[tuple[int, list[ProjectGraphNode]]] = []
    for node in sorted(groups, key=lambda item: (library.node_path(item))):
        members = sorted(groups[node], key=lambda item: item.title)
        ordered.append((node, members))

    for column_index, (node_id, members) in enumerate(ordered):
        column = ProjectGraphColumn(
            index=column_index,
            title=members[0].node_name if members else "",
            subtitle=f"{len(members)} 份文档",
            x=MARGIN_X + column_index * COLUMN_W,
        )
        graph.columns.append(column)
        for row_index, member in enumerate(members):
            member.column = column_index
            member.row = row_index
            member.x = column.x
            member.y = MARGIN_TOP + row_index * ROW_H

    max_rows = max((len(members) for _, members in ordered), default=1)
    graph.width = MARGIN_X * 2 + max(len(ordered), 1) * COLUMN_W
    graph.height = MARGIN_TOP + max_rows * ROW_H + 40


def _layout_layered(library: Library, graph: ProjectGraph) -> None:
    """按依赖分层：只被别人依赖的靠左，依赖别人的靠右。

    用 Kahn 拓扑排序做最长路径分层；存在环时忽略回边并如实报出环的数量
    （历史数据里确实出现过互指的情况，硬画成一团没有意义）。
    """
    nodes = {node.document_id: node for node in graph.nodes}

    # 边是「source 依赖 target」，所以拓扑方向要**反过来**传播：
    # 谁都不依赖的（被依赖的源头）在最左，依赖别人的依次向右。
    #   rev_out[X] = 依赖 X 的文档；rev_in[X] = X 依赖了几个文档
    rev_out: dict[int, list[int]] = {}
    rev_in: dict[int, int] = {document_id: 0 for document_id in nodes}
    for edge in graph.edges:
        rev_out.setdefault(edge.target_document_id, []).append(edge.source_document_id)
        rev_in[edge.source_document_id] = rev_in.get(edge.source_document_id, 0) + 1

    layer: dict[int, int] = {}
    from collections import deque

    queue = deque(sorted(key for key, value in rev_in.items() if value == 0))
    processed = 0
    while queue:
        current = queue.popleft()
        processed += 1
        for dependent in rev_out.get(current, []):
            layer[dependent] = max(layer.get(dependent, 0), layer.get(current, 0) + 1)
            rev_in[dependent] -= 1
            if rev_in[dependent] == 0:
                queue.append(dependent)

    remaining = [key for key in nodes if key not in layer]
    if processed < len(nodes) and remaining:
        # 有环：剩下的节点互相依赖，没有合理的先后顺序。
        # 按标题摊到后面几列（不假装有层次），并如实数出"回边"数量。
        base = max(layer.values(), default=-1) + 1
        for offset, document_id in enumerate(sorted(remaining, key=lambda item: nodes[item].title)):
            layer[document_id] = base + (offset // 3)

    # 环（回边）：依赖方向与层次方向相反的边
    graph.cycle_count = sum(
        1 for edge in graph.edges
        if layer.get(edge.source_document_id, 0) <= layer.get(edge.target_document_id, 0)
    )

    for node in graph.nodes:
        node.column = layer.get(node.document_id, 0)

    by_layer: dict[int, list[ProjectGraphNode]] = {}
    for node in graph.nodes:
        by_layer.setdefault(node.column, []).append(node)

    for layer_index in sorted(by_layer):
        members = sorted(by_layer[layer_index], key=lambda item: item.title)
        column = ProjectGraphColumn(
            index=layer_index,
            title=f"第 {layer_index + 1} 层",
            subtitle=f"{len(members)} 份文档",
            x=MARGIN_X + layer_index * COLUMN_W,
        )
        graph.columns.append(column)
        for row_index, member in enumerate(members):
            member.row = row_index
            member.x = column.x
            member.y = MARGIN_TOP + row_index * ROW_H

    max_rows = max((len(members) for members in by_layer.values()), default=1)
    graph.width = MARGIN_X * 2 + max(len(by_layer), 1) * COLUMN_W
    graph.height = MARGIN_TOP + max_rows * ROW_H + 40

"""版本图：把一份文档的历史组织成可绘制的流程图。

这是「文档树视图 / 流程视图」的数据源：桌面端用 QGraphicsScene 画，
第二阶段 Web 端可以直接复用同一份 :meth:`VersionGraph.to_dict` 用 SVG 画。

坐标系约定（世界坐标，单位像素，供 QGraphicsScene 直接使用）
----------------------------------------------------------
* X 轴 = 时间。先按真实时间戳做比例映射，再施加最小间距，
  于是「密集改动」被均匀展开、「长期搁置」被真实拉开 —— 时间关系不丢，又不会挤成一团。
* Y 轴 = 泳道。主线在第 0 道；同一父版本出现多个子版本（分叉）时，
  新的分支落到下一条泳道，因此「分支/合并」在图上是一眼可见的几何关系。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .diffing import summarize_change
from .library import (
    DOC_STATE_LABELS,
    ORIGIN_LABELS,
    REVIEW_STATE_LABELS,
    ROLE_LABELS,
    Document,
    Library,
    Version,
)
from .util import human_size, parse_iso, short_hash, to_local_str

__all__ = ["GraphNode", "GraphEdge", "VersionGraph", "build_version_graph",
           "build_stage_graph", "list_stage_roles", "ROLE_ORDER"]

#: 环节的显示/排序顺序：先输入，再分析/参考，最后输出（符合做事顺序）
ROLE_ORDER = ["input", "analysis", "reference", "figure", "output", "primary", "attachment"]

#: 相邻版本的最小水平间距
SLOT_X = 236.0
#: 泳道高度
LANE_H = 132.0
#: 场景左上留白
MARGIN_X = 48.0
MARGIN_Y = 64.0


@dataclass
class GraphNode:
    """流程图上的一个版本节点。"""

    version_id: int
    seq: int
    created_at: str
    source_name: str = ""
    review_state: str = "draft"
    #: 事件状态（进行中/已完成/搁置）—— 个人管理里真正驱动配色的那个字段
    state: str = "active"
    #: 该节点涉及的环节（环节视图用；事件视图为空）
    role: str = ""
    origin: str = "manual"
    author: str = ""
    note: str = ""
    sha256: str = ""
    size: int = 0
    file_count: int = 1
    parent_seq: int | None = None
    change_summary: str = ""
    is_current: bool = False
    lane: int = 0
    x: float = 0.0
    y: float = 0.0

    @property
    def label(self) -> str:
        return f"v{self.seq}"

    @property
    def short_sha(self) -> str:
        return short_hash(self.sha256)

    @property
    def time_text(self) -> str:
        return to_local_str(self.created_at, "%Y-%m-%d %H:%M")

    @property
    def day_text(self) -> str:
        return to_local_str(self.created_at, "%Y-%m-%d")

    @property
    def review_label(self) -> str:
        return REVIEW_STATE_LABELS.get(self.review_state, self.review_state)

    @property
    def state_label(self) -> str:
        return DOC_STATE_LABELS.get(self.state, self.state)

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role) if self.role else ""

    @property
    def origin_label(self) -> str:
        return ORIGIN_LABELS.get(self.origin, self.origin)

    @property
    def size_text(self) -> str:
        return human_size(self.size)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_id": self.version_id,
            "seq": self.seq,
            "label": self.label,
            "created_at": self.created_at,
            "time_text": self.time_text,
            "source_name": self.source_name,
            "review_state": self.review_state,
            "review_label": self.review_label,
            "state": self.state,
            "state_label": self.state_label,
            "role": self.role,
            "role_label": self.role_label,
            "origin": self.origin,
            "origin_label": self.origin_label,
            "author": self.author,
            "note": self.note,
            "sha256": self.sha256,
            "short_sha": self.short_sha,
            "size": self.size,
            "size_text": self.size_text,
            "file_count": self.file_count,
            "parent_seq": self.parent_seq,
            "change_summary": self.change_summary,
            "is_current": self.is_current,
            "lane": self.lane,
            "x": self.x,
            "y": self.y,
        }


@dataclass
class GraphEdge:
    """版本之间的演化/语义边。"""

    source_version_id: int
    target_version_id: int
    kind: str = "evolution"   # evolution | restore | branch | merge
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source_version_id,
            "target": self.target_version_id,
            "kind": self.kind,
            "label": self.label,
        }


@dataclass
class VersionGraph:
    """一份文档的完整版本图（节点 + 边 + 布局）。"""

    document_id: int
    document_title: str
    node_path: str = ""
    project_name: str = ""
    current_version_id: int | None = None
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    lane_count: int = 1
    width: float = 0.0
    height: float = 0.0
    has_branches: bool = False
    time_span_text: str = ""

    def node_by_version(self, version_id: int) -> GraphNode | None:
        return next((item for item in self.nodes if item.version_id == version_id), None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "document_title": self.document_title,
            "node_path": self.node_path,
            "project_name": self.project_name,
            "current_version_id": self.current_version_id,
            "lane_count": self.lane_count,
            "width": self.width,
            "height": self.height,
            "has_branches": self.has_branches,
            "time_span_text": self.time_span_text,
            "nodes": [item.to_dict() for item in self.nodes],
            "edges": [item.to_dict() for item in self.edges],
        }


def build_version_graph(library: Library, document_id: int) -> VersionGraph:
    """读取某文档的全部版本并计算出可绘制的版本图。"""
    document = library.get_document(document_id)
    if document is None:
        return VersionGraph(document_id=document_id, document_title="")

    versions = library.version_history(document_id)
    links = library.version_links(document_id)

    graph = VersionGraph(
        document_id=document_id,
        document_title=document.title,
        node_path=library.node_path(document.node_id),
        project_name=_project_name_of(library, document),
        current_version_id=document.current_version_id,
    )
    if not versions:
        return graph

    by_id = {version.id: version for version in versions}

    # ---------------------------------------------------------------- 泳道
    # 规则：首个版本落在第 0 道；子版本默认继承父版本的泳道（= 主线），
    #       但同一父版本的第 2、3… 个子版本（分叉）各自另起一条泳道，
    #       这样「分叉」在图上表现为向下岔开的新行，而不是叠在一起。
    lane_of: dict[int, int] = {}
    children_placed: dict[int, int] = {}
    roots_placed = 0
    max_lane = 0
    for version in versions:  # 已按 seq 升序
        parent = version.parent_version_id
        if parent is not None and parent in lane_of:
            lane = lane_of[parent]
            placed = children_placed.get(parent, 0)
            if placed >= 1:
                max_lane += 1
                lane = max_lane
            children_placed[parent] = placed + 1
            if children_placed[parent] > 1:
                graph.has_branches = True
        else:
            # 无父版本（v1，或父版本已被删除）
            if roots_placed > 0:
                max_lane += 1
                lane = max_lane
            else:
                lane = 0
            roots_placed += 1

        lane_of[version.id] = lane
        max_lane = max(max_lane, lane)

    # ------------------------------------------------------------------ X 轴
    # 解析失败的时间戳（历史数据里确实有）**不参与 min/max**：
    # 早期把它们当 0.0（=1970）塞进去，一条坏时间就把 min 拉到 1970，
    # 于是其余所有节点都被挤到最右端、彼此糊在一起。坏时间的节点改按索引间距排。
    stamps: list[float | None] = []
    for version in versions:
        stamp = parse_iso(version.created_at)
        stamps.append(stamp.timestamp() if stamp else None)
    known = [stamp for stamp in stamps if stamp is not None]
    # min(stamps) 提到循环外：原来写在比例式里，每个节点都重算一遍 → O(n²)
    low = min(known) if known else 0.0
    span = (max(known) - low) if known else 0.0
    base_width = max(SLOT_X * (len(versions) - 1), SLOT_X)

    positions: list[float] = []
    for index, stamp in enumerate(stamps):
        if stamp is None or span <= 0:
            # 时间坏掉（或所有版本时间相同）：按版本号等距排，不参与时间映射
            time_x = MARGIN_X + SLOT_X * index
        else:
            time_x = MARGIN_X + base_width * ((stamp - low) / span)
        if positions:
            time_x = max(time_x, positions[-1] + SLOT_X)
        positions.append(time_x)

    # ------------------------------------------------------------------ 组图
    for index, version in enumerate(versions):
        lane = lane_of.get(version.id, 0)
        parent = by_id.get(version.parent_version_id) if version.parent_version_id else None
        node = GraphNode(
            version_id=version.id,
            seq=version.seq,
            created_at=version.created_at,
            source_name=version.source_name,
            review_state=version.review_state,
            state=document.state,
            origin=version.origin,
            author=version.author,
            note=version.note,
            sha256=version.sha256,
            size=version.size,
            file_count=len(version.files),
            parent_seq=parent.seq if parent else None,
            change_summary=summarize_change(parent, version),
            is_current=(version.id == document.current_version_id),
            lane=lane,
            x=positions[index],
            y=MARGIN_Y + lane * LANE_H,
        )
        graph.nodes.append(node)
        if parent is not None:
            graph.edges.append(
                GraphEdge(parent.id, version.id, "evolution", node.change_summary)
            )

    # 语义边：回到旧版 / 分支 / 合并
    for link in links:
        kind = link.get("kind", "branch")
        graph.edges.append(
            GraphEdge(
                int(link["from_version_id"]),
                int(link["to_version_id"]),
                kind,
                link.get("label", "") or kind,
            )
        )

    graph.lane_count = max_lane + 1
    graph.width = max(positions) + SLOT_X
    graph.height = MARGIN_Y + graph.lane_count * LANE_H
    graph.time_span_text = _span_text(versions)
    return graph


def build_stage_graph(library: Library, document_id: int, role: str) -> VersionGraph:
    """某个**环节**的版本流程：这个环节在不同时期分别是哪些文件。

    ----------------------------------
    真实数据里，"环节"是稳定的槽位，而槽位里的**文件名每次都在变**：

    * ``20250623…-提交.docx`` → ``20250627…-提交.docx`` → ``20250701…`` → ``20250706…``
    * ``…研究_V1.pptx`` → ``_V2`` → ``_V3`` → ``_V4`` → ``_V5.pptx``

    所以按"文件名"做版本链只会得到一堆长度为 1 的碎片；按**环节**做，
    才真正回答"这个交付物在不同时期是什么样"。

    只保留该环节**发生变化**的版本：中间那些"这个环节没动"的版本如果也画出来，
    就会变成一排空节点，把真正要看的东西挤没。
    """
    document = library.get_document(document_id)
    if document is None:
        return VersionGraph(document_id=document_id, document_title="")
    versions = library.version_history(document_id)

    graph = VersionGraph(
        document_id=document_id,
        document_title=f"{document.title} · {ROLE_LABELS.get(role, role)}",
        node_path=library.node_path(document.node_id),
        project_name=_project_name_of(library, document),
        current_version_id=document.current_version_id,
    )
    if not versions:
        return graph

    previous_signature: tuple | None = None
    previous_names: set[str] = set()
    index = 0
    previous_node_id: int | None = None

    for version in versions:
        members = [item for item in version.files if item.role == role]
        is_current = version.id == document.current_version_id
        signature = tuple(sorted((item.name, item.sha256) for item in members))
        if (previous_signature is not None and signature == previous_signature
                and not is_current):
            # 中间那些"这个环节没动"的稿不画（否则是一排空节点，把真要看的挤没）。
            # 但**当前稿例外**：早期这里只写了个 `pass`（注释承诺"当前稿也留一个锚点"、
            # 代码却没做），紧接着的"签名相同就 continue"照样生效 ——
            # 结果是 is_current 永远为假、环节流程图里当前稿整个缺席，
            # 用户看不出"现在用的到底是哪一份"。
            continue
        if previous_signature is None and not members:
            # 这个环节还没出现过：不要在链条开头放一个空节点
            continue

        names = {item.name for item in members}
        # 只描述**这一稿里有什么**，不推断"上一稿那份被删了"——
        # 旧数据的每一稿只是"那一次登记过的文件"，不是完整快照，
        # 说"移除了"会冤枉数据（这一点是查真实数据时发现的）。
        if previous_signature is not None and signature == previous_signature:
            # 能走到这里只可能是当前稿（其余相同的已经 continue 掉了）：
            # 这个环节没变，但它是"现在在用的那一份"，要如实标出来
            summary = "与上一稿相同（当前稿）"
        elif previous_signature is None:
            summary = "初次引入" if members else "该环节为空"
        elif not members:
            summary = "本稿没有该环节的文件"
        elif not previous_names:
            summary = f"本稿放入 {len(members)} 个文件"
        elif names - previous_names and not (previous_names - names):
            summary = f"本稿新增 {len(names - previous_names)} 个文件"
        elif previous_names - names and not (names - previous_names):
            summary = f"本稿换了 {len(names - previous_names)} 个文件"
        else:
            summary = "文件已更新" if len(members) == 1 else f"{len(members)} 个文件已更新"

        primary = members[0] if members else None
        node = GraphNode(
            version_id=version.id,
            seq=version.seq,
            created_at=version.created_at,
            source_name="、".join(item.name for item in members) or "（无文件）",
            review_state=version.review_state,
            state=document.state,
            role=role,
            origin=version.origin,
            author=version.author,
            note=version.note,
            sha256=primary.sha256 if primary else "",
            size=sum(item.size for item in members),
            file_count=len(members),
            change_summary=summary,
            is_current=is_current,
            lane=0,
            x=MARGIN_X + index * SLOT_X,
            y=MARGIN_Y,
        )
        graph.nodes.append(node)
        if previous_node_id is not None:
            graph.edges.append(
                GraphEdge(previous_node_id, node.version_id, "evolution", summary)
            )
        previous_node_id = node.version_id
        previous_signature = signature
        previous_names = names
        index += 1

    graph.lane_count = 1
    graph.width = MARGIN_X * 2 + max(index, 1) * SLOT_X
    graph.height = MARGIN_Y + LANE_H
    graph.time_span_text = f"{len(graph.nodes)} 个稿"
    return graph


def list_stage_roles(library: Library, document_id: int) -> list[dict]:
    """列出某个事件里出现过的所有环节，供树/界面使用。

    每个环节给出三个**互不混淆**的数字：

    * ``files``：这个环节里出现过多少**份**不同的文件（按「文件名 + 内容指纹」去重）；
    * ``names``：出现过多少个不同的文件名；
    * ``changes``：这个环节**变过几次**（与 :func:`build_stage_graph` 的口径完全一致，
      导致树上说"7 稿"、点进去只有 2 个稿。两者必须一致，测试里锁死了这条。

    ``latest_names`` 是最近出现的文件名（最多 3 个），便于列表里一眼看出是什么。
    """
    versions = library.version_history(document_id)
    if not versions:
        return []

    document = library.get_document(document_id)
    current_version_id = document.current_version_id if document else None
    all_roles = {item.role for version in versions for item in version.files}
    result: list[dict] = []

    for role in all_roles:
        signatures: set[tuple] = set()
        names_seen: dict[str, None] = {}
        changes = 0
        previous: tuple | None = None

        for version in versions:
            members = [item for item in version.files if item.role == role]
            signature = tuple(sorted((item.name, item.sha256) for item in members))
            if previous is None and not members:
                continue      # 这个环节还没出现过
            # 口径必须与 build_stage_graph 完全一致：当前稿即使这个环节没变，
            # 也会在流程图上留一个锚点节点，所以这里也要计一次 ——
            anchored_current = (
                previous is not None and signature == previous
                and version.id == current_version_id
            )
            if signature != previous or anchored_current:
                changes += 1
            previous = signature
            for item in members:
                signatures.add((item.name, item.sha256))
                names_seen[item.name] = None

        recent = list(reversed(list(names_seen)))[:3]
        result.append(
            {
                "role": role,
                "label": ROLE_LABELS.get(role, role),
                "files": len(signatures),
                "names": len(names_seen),
                "changes": changes,
                "latest_names": recent,
            }
        )

    result.sort(key=lambda item: (
        ROLE_ORDER.index(item["role"]) if item["role"] in ROLE_ORDER else 99,
        item["role"],
    ))
    return result


def _project_name_of(library: Library, document: Document) -> str:
    node = library.get_node(document.node_id)
    if node is None:
        return ""
    project = library.get_project(node.project_id)
    return project.name if project else ""


def _span_text(versions: list[Version]) -> str:
    """时间跨度描述，如「跨度 4 个月」或「同一天内」。"""
    if not versions:
        return ""
    first = parse_iso(versions[0].created_at)
    last = parse_iso(versions[-1].created_at)
    if first is None or last is None:
        return ""
    days = (last - first).days
    if days <= 0:
        return "同一天内"
    if days < 30:
        return f"跨度 {days} 天"
    if days < 365:
        return f"跨度 {days // 30} 个月"
    return f"跨度 {days // 365} 年"

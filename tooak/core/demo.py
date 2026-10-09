"""演示库：一棵「橡树」的完整档案 —— 用橡树科普做例子，展示项目树 / 事件枝 / 年轮。

例子内容
--------
一个项目树「橡树」，下面长四根事件枝：

* 橡树的种类（参考 + 输出，已完成）
* 橡树的生长（输入 + 分析 + 输出，**两稿**演示年轮）
* 橡树的价值（参考 + 输出，搁置）
* 致橡树（输出 = 舒婷《致橡树》全诗，进行中）

它们之间有一条依赖链（种类 → 生长 → 价值 → 致橡树），白板上能看到枝叶连线；
「生长」有 v1/v2 两稿，事件页底部的年轮时间轴与版本流程视图一眼就能看懂。
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .library import Library

__all__ = ["create_demo_library"]

#: 演示用的历史时间点（相对今天的偏移天数），让时间轴有真实跨度
_TIMELINE_DAYS = [96, 74, 61, 40, 33, 19, 12, 6, 2, 0]


#: 舒婷《致橡树》（1977）。放在「致橡树」事件枝的输出文件里。
POEM_TO_THE_OAK = """《致橡树》
舒婷

我如果爱你——
绝不像攀援的凌霄花，
借你的高枝炫耀自己；
我如果爱你——
绝不学痴情的鸟儿，
为绿荫重复单调的歌曲；
也不止像泉源，
常年送来清凉的慰藉；
也不止像险峰，
增加你的高度，衬托你的威仪。
甚至日光，
甚至春雨。
不，这些都还不够！
我必须是你近旁的一株木棉，
作为树的形象和你站在一起。
根，紧握在地下；
叶，相触在云里。
每一阵风过，
我们都互相致意，
但没有人，
听懂我们的言语。
你有你的铜枝铁干，
像刀，像剑，也像戟；
我有我红硕的花朵，
像沉重的叹息，
又像英勇的火炬。
我们分担寒潮、风雷、霹雳；
我们共享雾霭、流岚、虹霓。
仿佛永远分离，
却又终身相依。
这才是伟大的爱情，
坚贞就在这里：
爱——
不仅爱你伟岸的身躯，
也爱你坚持的位置，
足下的土地。
"""


def create_demo_library(root: str | Path, name: str = "橡树") -> Library:
    """在 ``root`` 建一个「橡树」演示库（已存在则直接打开，不重复造数据）。"""
    library = Library.open_or_create(root, name=name)
    if library.list_projects():
        return library

    project = library.create_project(
        "橡树", description="一棵橡树的完整档案：科学、生长、价值与文化"
    )
    root_node = library.node_tree(project.id)[0]
    science = library.create_node(project.id, "橡树的科学", parent_id=root_node.id)
    culture = library.create_node(project.id, "橡树与文化", parent_id=root_node.id)

    # --- 种类：单稿、已完成
    kinds, _ = _stage_doc(
        library, science.id, "橡树的种类",
        [
            ("reference", "栎属常见种类.md", _KINDS_REF),
            ("output", "种类速览.md", _KINDS_OUT),
        ],
        note="先摸清有哪几类橡树，后面才好讲生长和价值。",
        state="done", people="林学组", task_date="2025-03-02",
    )

    # --- 生长：note 型、四稿（演示年轮与分叉；方框留空，方便当"练习对象"拖文件进去）
    growth = library.create_document(
        science.id, "橡树的生长", kind="note",
        state="active", people="观测组", task_date="2025-04-10",
    )
    library.add_note_version(growth.id, _GROWTH_OUT_V1, note="第一稿：先记录，再慢慢补。")
    library.add_note_version(growth.id, _GROWTH_OUT_V2, note="第二稿：补上「数树龄」的结论。")
    first = library.version_history(growth.id)[0].id
    # 从第一稿分出两个并行方向（版本流程里能看到分叉）
    library.create_version(
        growth.id, text="方案 A：只跟踪胸径的变化", author="观测组",
        note="并行方向", parent_version_id=first,
    )
    library.create_version(
        growth.id, text="方案 B：只跟踪树高的变化", author="观测组",
        note="并行方向", parent_version_id=first,
    )

    # --- 价值：单稿、搁置
    value, _ = _stage_doc(
        library, science.id, "橡树的价值",
        [
            ("reference", "橡木用途.md", _VALUE_REF),
            ("output", "价值小结.md", _VALUE_OUT),
        ],
        note="用途很多，先放着，想到再补。",
        state="on_hold", people="材料组", due_date="2025-05-30",
    )

    # --- 致橡树：单稿、进行中（输出 = 舒婷的诗）
    poem, _ = _stage_doc(
        library, culture.id, "致橡树",
        [("output", "致橡树.txt", POEM_TO_THE_OAK)],
        note="舒婷《致橡树》：以木棉致橡树 —— 独立又相依。",
        state="active", people="文化组",
    )

    # --- 依赖链：种类 → 生长 → 价值 → 致橡树（白板上能看枝叶连线）
    _link(library, growth, kinds)
    _link(library, value, growth)
    _link(library, poem, value)

    _spread_timeline(library)
    _apply_demo_layout(library, kinds, growth, value, poem)
    return library


#: 演示库用的**框体模板**（照用户真实库的排版：信息框一行在上，
#: 输入/参考/图示、分析/输出/其他 三行在下，和事件页的网格一致）。
_DEMO_BOX_TEMPLATE = {
    "time": (0, 0, 6, 3),
    "people": (6, 0, 5, 3),
    "progress": (11, 0, 6, 3),
    "note": (17, 0, 9, 3),
    "role:input": (0, 3, 8, 6),
    "role:reference": (8, 3, 8, 6),
    "role:figure": (16, 3, 10, 8),
    "role:analysis": (0, 9, 8, 6),
    "role:output": (8, 9, 8, 6),
    "role:extra": (16, 11, 10, 4),
}

#: 白板布局（照用户亲手摆的 2×2），按创建顺序：种类 / 生长 / 价值 / 致橡树。
_DEMO_BOARD_POSITIONS = [
    (134.40, -67.44),   # 橡树的种类（左上）
    (583.16, -76.63),   # 橡树的生长（右上）
    (134.40, 146.41),   # 橡树的价值（左下）
    (583.16, 146.41),   # 致橡树（右下）
]


def _apply_demo_layout(library: Library, kinds, growth, value, poem) -> None:
    """铺好演示库的框体模板（事件页）与白板位置（版图）。"""
    for key, (col, row, units_w, units_h) in _DEMO_BOX_TEMPLATE.items():
        library.set_box_template_entry(key, col, row, units_w, units_h)

    for document, (x, y) in zip((kinds, growth, value, poem), _DEMO_BOARD_POSITIONS):
        library.set_document_position(document.id, x, y)


def _stage_doc(library: Library, node_id: int, title: str, files, *, note: str = "",
               **doc_kwargs):
    """建一个事件枝，把若干 ``(环节, 文件名, 内容)`` 写进它的第一稿。

    :returns: ``(Document, 第一稿 Version)``
    """
    document = library.create_document(node_id, title, **doc_kwargs)
    version = library.create_empty_version(document.id)
    for role, filename, content in files:
        _put(library, version.id, role, filename, content)
    if note:
        library.set_version_note(version.id, note)
    return document, version


def _put(library: Library, version_id: int, role: str, filename: str, content: str):
    """把一段文本写进库里（落到 files/<别名>/<原名>，原名保持不变）。"""
    with tempfile.TemporaryDirectory(prefix="tooak-demo-file-") as d:
        path = Path(d) / filename
        path.write_text(content, encoding="utf-8")
        library.add_files_to_version(version_id, [str(path)], role=role)


def _link(library: Library, downstream, upstream) -> None:
    """登记一条依赖：下游事件枝依赖上游事件枝。"""
    library.link_documents(downstream.id, upstream.id, "depends_on", "依赖")


def _spread_timeline(library: Library) -> None:
    """把演示版本的创建时间摊开到过去几个月，让时间轴有真实跨度。

    只动演示库自己的数据；真实使用时不走这条路径。
    """
    rows = library.db.query("SELECT id FROM versions ORDER BY document_id, seq")
    now = datetime.now(timezone.utc)
    counter = 0
    for row in rows:
        days = _TIMELINE_DAYS[counter % len(_TIMELINE_DAYS)]
        stamp = (now - timedelta(days=days, hours=counter % 9)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")
        library.db.execute("UPDATE versions SET created_at = ? WHERE id = ?", (stamp, row["id"]))
        counter += 1

    for document in library.db.query("SELECT id FROM documents"):
        latest = library.db.query_one(
            "SELECT created_at FROM versions WHERE document_id = ? ORDER BY seq DESC LIMIT 1",
            (document["id"],),
        )
        if latest:
            library.db.execute(
                "UPDATE documents SET created_at = ?, updated_at = ? WHERE id = ?",
                (latest["created_at"], latest["created_at"], document["id"]),
            )


# ---------------------------------------------------------------------------
# 演示内容（橡树科普）
# ---------------------------------------------------------------------------

_KINDS_REF = """栎属（Quercus）是壳斗科下的一属，全球约 600 种，中国约 100 种。常见的有：

1. 白栎（Quercus alba）—— 北美，木材优良。
2. 红栎（Quercus rubra）—— 北美，秋叶红。
3. 麻栎（Quercus acutissima）—— 东亚，叶缘有芒刺。
4. 栓皮栎（Quercus variabilis）—— 树皮可制软木。
5. 蒙古栎（Quercus mongolica）—— 我国东北常见。
"""

_KINDS_OUT = """橡树种类速览：

- 全球约 600 种，中国约 100 种。
- 共同点：坚果（橡果）包在壳斗里。
- 代表：白栎、红栎、麻栎、栓皮栎、蒙古栎。
"""

_GROWTH_OUT_V1 = """生长小结（第一稿）：

橡树生长慢而稳，每年一圈年轮，木质致密。
"""

_GROWTH_OUT_V2 = """生长小结（第二稿）：

橡树生长慢而稳，每年一圈年轮，木质致密。
补充：树龄可以数年轮；胸径与树龄大致正相关。
"""

_VALUE_REF = """橡木与橡果的用途：

- 橡木：家具、地板、酒桶；栓皮栎的树皮可制软木。
- 橡果：动物的重要口粮；旧时人也食用（需先去单宁）。
"""

_VALUE_OUT = """价值小结：

橡树一身是宝 —— 木材致密耐用，树皮可制软木，橡果滋养山林里的动物。
"""

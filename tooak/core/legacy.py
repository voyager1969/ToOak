"""把旧版 ToOak / Symphytum 的库（``data.db`` + ``files/``）迁移进新的 ToOak 库。

旧库结构（对作者实际库的实地研究结论）
--------------------------------------
* ``collections``：**一张表 = 一个项目/工作流**（一个项目一张表，表名就是项目名）；
* 每个 collection 有内容表 ``<table_name>`` 与字段定义表 ``<table_name>_metadata``；
* 每条记录 = 一个**任务**，字段固定为：
  限期 / 标题 / 输入文件 / 输出文件 / 图示 / 完成 / 标签 / 修改时间 / 任务说明 /
  相关人 / 版本 / 任务日期 / 进度 / 参考数据 / 分析资料 / 依赖任务；
* 文件字段存的是 ``files._id`` 的逗号串（如 ``"22,"``），真实文件在 ``files/<hash_name>``；
* **同一标题的多条记录就是它的多个版本**（作者用「版本」字段手工维护）；
* 「标签」是单选项（下拉索引），值为 0 基下标；
* 「依赖任务」是逗号分隔的其它任务标题 —— 这是一条**文档间的依赖边**。

映射到新库
----------
============================  ==========================================
旧库                          新库
============================  ==========================================
collection                    项目（+ 一个根节点）
记录标题                      文档（身份即标题）
同一标题的多条记录            同一文档的多个版本（按「版本」字段排序）
输入/输出/参考/分析/图示 字段  版本内的文件角色 input/output/reference/analysis/figure
修改时间                      版本时间（保持原时间线）
标签                          子节点分组 + 文档标签（两者都建，信息不丢）
相关人 / 进度 / 完成 / 限期 / 任务日期  文档字段
任务说明                      文档描述
依赖任务                      文档间链接 depends_on
files/<hash>/<原名>           内容寻址 blob（默认硬链接，不占额外空间）
============================  ==========================================
"""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .library import FileSpec, Library, LibraryError
from .util import utcnow_iso

__all__ = [
    "LegacyLibrary",
    "LegacyCollection",
    "LegacyField",
    "LegacyFile",
    "ImportStats",
    "import_legacy",
    "describe_legacy",
    "FILE_FIELD_ROLES",
]

#: 旧库「文件类型」字段名 -> 新库版本内文件角色
FILE_FIELD_ROLES = {
    "输入文件": "input",
    "输出文件": "output",
    "参考数据": "reference",
    "分析资料": "analysis",
    "图示": "figure",
}

TITLE_FIELD = "标题"
VERSION_FIELD = "版本"
MODIFIED_FIELD = "修改时间"
DESCRIPTION_FIELD = "任务说明"
PEOPLE_FIELD = "相关人"
PROGRESS_FIELD = "进度"
DONE_FIELD = "完成"
DUE_FIELD = "限期"
TASK_DATE_FIELD = "任务日期"
TAG_FIELD = "标签"
DEPS_FIELD = "依赖任务"

#: 旧库字段类型编号 -> 可读名（用于诊断）
FIELD_TYPE_NAMES = {
    1: "文本", 2: "数字", 3: "日期", 4: "创建日期", 5: "修改日期", 6: "勾选",
    7: "下拉", 8: "进度", 9: "图片", 10: "文件", 11: "网址", 12: "邮箱",
}


# ---------------------------------------------------------------------------
# 只读读取旧库
# ---------------------------------------------------------------------------


@dataclass
class LegacyField:
    index: int
    name: str
    type_id: int = 1
    display: str = ""

    @property
    def type_name(self) -> str:
        return FIELD_TYPE_NAMES.get(self.type_id, f"类型{self.type_id}")

    @property
    def display_props(self) -> dict[str, str]:
        """解析 ``key:value;key2:value2;`` 形式的显示属性。"""
        props: dict[str, str] = {}
        for chunk in (self.display or "").split(";"):
            if ":" in chunk:
                key, _, value = chunk.partition(":")
                props[key.strip()] = value.strip()
        return props

    @property
    def combo_items(self) -> list[str]:
        """下拉字段的候选项列表。"""
        raw = self.display_props.get("items", "")
        return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass
class LegacyCollection:
    id: int
    name: str
    table_name: str
    order: int = 0
    parent: str | None = None
    fields: list[LegacyField] = field(default_factory=list)
    row_count: int = 0


@dataclass
class LegacyFile:
    id: int
    name: str
    hash_name: str
    date_added: str = ""
    original_dir_path: str = ""
    #: 解析出的磁盘路径（不存在时为 None）
    path: Path | None = None

    @property
    def size(self) -> int:
        try:
            return self.path.stat().st_size if self.path else 0
        except OSError:
            return 0


class LegacyLibrary:
    """只读地读取旧版库。"""

    DB_NAME = "data.db"
    FILES_DIR = "files"

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.db_path = self.root / self.DB_NAME
        if not self.db_path.is_file():
            raise LibraryError(f"找不到旧库数据库文件：{self.db_path}")
        self.files_root = self.root / self.FILES_DIR
        self.conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self._file_cache: dict[int, LegacyFile] | None = None

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LegacyLibrary":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---------------------------------------------------------------- 结构

    def collections(self) -> list[LegacyCollection]:
        rows = self.conn.execute(
            "SELECT * FROM collections ORDER BY c_order, _id"
        ).fetchall()
        result: list[LegacyCollection] = []
        for row in rows:
            collection = LegacyCollection(
                id=row["_id"],
                name=row["name"] or f"未命名项目 {row['_id']}",
                table_name=row["table_name"],
                order=row["c_order"] or 0,
                parent=row["parent"] if "parent" in row.keys() else None,
            )
            collection.fields = self.fields(collection.table_name)
            try:
                collection.row_count = int(
                    self.conn.execute(
                        f'SELECT COUNT(*) FROM "{collection.table_name}"'
                    ).fetchone()[0]
                )
            except sqlite3.Error:
                collection.row_count = 0
            result.append(collection)
        return result

    def fields(self, table_name: str) -> list[LegacyField]:
        metadata_table = f"{table_name}_metadata"
        try:
            rows = self.conn.execute(f'SELECT key, value FROM "{metadata_table}"').fetchall()
        except sqlite3.Error:
            return []
        props = {row["key"]: (row["value"] or "") for row in rows}
        try:
            column_count = int(props.get("column_count") or 0)
        except ValueError:
            column_count = 0

        fields: list[LegacyField] = []
        for index in range(1, max(column_count, 1)):
            name = props.get(f"col{index}_name")
            if not name:
                continue
            try:
                type_id = int(props.get(f"col{index}_type") or 1)
            except ValueError:
                type_id = 1
            fields.append(
                LegacyField(
                    index=index,
                    name=name,
                    type_id=type_id,
                    display=props.get(f"col{index}_display", ""),
                )
            )
        return fields

    # ---------------------------------------------------------------- 记录

    def records(self, collection: LegacyCollection) -> list[dict]:
        """读出一张集合表的所有记录，返回 ``{"_id": int, 字段名: 值}`` 列表。"""
        by_index = {item.index: item.name for item in collection.fields}
        try:
            rows = self.conn.execute(f'SELECT * FROM "{collection.table_name}"').fetchall()
        except sqlite3.Error:
            return []
        records: list[dict] = []
        for row in rows:
            keys = row.keys()
            record: dict = {"_id": row["_id"]}
            for index, name in by_index.items():
                key = str(index)
                record[name] = row[key] if key in keys else None
            records.append(record)
        return records

    # ---------------------------------------------------------------- 文件

    def files(self) -> dict[int, LegacyFile]:
        if self._file_cache is not None:
            return self._file_cache
        cache: dict[int, LegacyFile] = {}
        try:
            rows = self.conn.execute("SELECT * FROM files").fetchall()
        except sqlite3.Error:
            rows = []
        for row in rows:
            keys = row.keys()
            item = LegacyFile(
                id=row["_id"],
                name=row["name"] or "",
                hash_name=row["hash_name"] or "",
                date_added=row["date_added"] or "",
                original_dir_path=(
                    row["original_dir_path"] if "original_dir_path" in keys else ""
                ),
            )
            candidate = self.files_root / item.hash_name if item.hash_name else None
            if candidate is not None and candidate.is_file():
                item.path = candidate
            cache[item.id] = item
        self._file_cache = cache
        return cache

    def file(self, file_id: int) -> LegacyFile | None:
        return self.files().get(int(file_id))


# ---------------------------------------------------------------------------
# 导入
# ---------------------------------------------------------------------------


@dataclass
class ImportStats:
    """导入结果统计（给用户看的账）。"""

    projects: int = 0
    documents: int = 0
    versions: int = 0
    nodes: int = 0
    tags: int = 0
    tag_assignments: int = 0
    links: int = 0
    files_stored: int = 0
    files_reused: int = 0
    files_missing: int = 0
    files_referenced: int = 0
    bytes_stored: int = 0
    bytes_referenced: int = 0
    file_mode: str = "copy"
    skipped_records: int = 0
    errors: list[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    dry_run: bool = False

    def summary(self) -> str:
        head = "（预演，未写入）" if self.dry_run else ""
        lines = [
            f"导入完成{head}：",
            f"  项目 {self.projects} · 节点 {self.nodes} · 文档 {self.documents} · 版本 {self.versions}",
            f"  标签 {self.tags} 个（挂到文档 {self.tag_assignments} 次）· 文档依赖边 {self.links}",
        ]
        if self.file_mode == "reference":
            lines.append(
                f"  文件（就地引用，未复制）：登记 {self.files_referenced} 份不同内容"
                f" / 缺失 {self.files_missing} 份，"
                f"合计 {self.bytes_referenced / 1024 / 1024 / 1024:.2f} GB（仍在原处）"
            )
        else:
            lines.append(
                f"  文件（{self.file_mode}）：占用新内容 {self.files_stored} 份"
                f" / 重复内容去重 {self.files_reused} 份 / 缺失 {self.files_missing} 份，"
                f"合计 {self.bytes_stored / 1024 / 1024 / 1024:.2f} GB"
            )
        if self.skipped_records:
            lines.append(f"  跳过已存在记录 {self.skipped_records} 条")
        if self.errors:
            lines.append(f"  问题 {len(self.errors)} 条，例如：{self.errors[0]}")
        lines.append(f"  耗时 {self.elapsed_seconds:.1f} 秒")
        return "\n".join(lines)


Progress = Callable[[str, int, int, str], None]


def _noop_progress(stage: str, done: int, total: int, message: str) -> None:
    return None


def _local_iso_to_utc(value: str | None) -> str | None:
    """旧库存的是**本机本地时间**的 ISO 串，转成新库统一的 UTC ISO。"""
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # astimezone() 对朴素时间按本机时区解释，正是旧库的语义
        parsed = parsed.astimezone()
    return (
        parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _parse_file_ids(value) -> list[int]:
    """解析 ``"22,303,307,"`` 这类文件 id 串（容忍中文逗号与空格）。"""
    if value is None:
        return []
    text = str(value).replace("，", ",").replace("、", ",")
    ids: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            ids.append(int(chunk))
    return ids


def _int_or_none(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class LegacySummary:
    """旧库概览（不读文件内容，很快）。"""

    root: Path
    collections: int = 0
    rows: int = 0
    documents: int = 0
    multi_version_titles: int = 0
    files_registered: int = 0
    files_on_disk: int = 0
    bytes_on_disk: int = 0

    def summary(self) -> str:
        return (
            f"旧库：{self.root}\n"
            f"项目（集合）{self.collections} 个 · 记录 {self.rows} 条 · "
            f"文档约 {self.documents} 份（其中 {self.multi_version_titles} 份有多版本）\n"
            f"文件：登记 {self.files_registered} 个，磁盘上 {self.files_on_disk} 个，"
            f"合计 {self.bytes_on_disk / 1024 / 1024 / 1024:.2f} GB"
        )


def describe_legacy(source_root: str | Path) -> LegacySummary:
    """快速概览旧库（供界面在确认迁移前显示）。"""
    with LegacyLibrary(source_root) as legacy:
        summary = LegacySummary(root=legacy.root)
        files = legacy.files()
        summary.files_registered = len(files)
        summary.files_on_disk = sum(1 for item in files.values() if item.path is not None)
        summary.bytes_on_disk = sum(item.size for item in files.values())
        for collection in legacy.collections():
            if collection.row_count <= 0:
                continue
            summary.collections += 1
            records = legacy.records(collection)
            summary.rows += len(records)
            groups = _group_by_title(records)
            summary.documents += len(groups)
            summary.multi_version_titles += sum(1 for rows in groups.values() if len(rows) > 1)
    return summary


def import_legacy(
    source_root: str | Path,
    target_root: str | Path,
    *,
    collection_names: Iterable[str] | None = None,
    mode: str = "reference",
    workers: int = 8,
    group_by_tag: bool = False,
    include_files: bool = True,
    dry_run: bool = False,
    progress: Progress | None = None,
) -> ImportStats:
    """执行迁移。

    :param collection_names: 只导入这些 collection（默认全部）。
    :param mode: 文件纳管方式，默认 ``reference``：**旧文件留在原处不动**，
        库里只登记路径与内容指纹（导入只读取、不写入旧库）。
        需要库自带内容时用 ``copy``（复制）或 ``link``（硬链接，同盘零拷贝）。
    :param workers: 预取文件时的并发线程数（哈希是 I/O 密集，8 条足够打满 SSD）。
    :param group_by_tag: 是否把「标签」建成**分类子节点**。
        「标签」会照常挂成事件的标签，信息不丢，只是不占一层。
        需要"项目 → 分类 → 事件"三层的用户可以打开这个开关。
    :param include_files: 为 False 时只导入结构与文本（快速预演，跳过十几 GB 文件）。
    :param dry_run: 只统计不写库。
    """
    report = progress or _noop_progress
    started = datetime.now()

    legacy = LegacyLibrary(source_root)
    stats = ImportStats(dry_run=dry_run)

    try:
        collections = legacy.collections()
        if collection_names:
            wanted = {name.strip() for name in collection_names}
            collections = [item for item in collections if item.name in wanted]
        collections = [item for item in collections if item.row_count > 0]

        # ---- 第一步：把所有要用到的文件一次性预取进 blob 库（并行哈希）
        plan: list[tuple[LegacyCollection, list[dict]]] = []
        needed_ids: dict[int, list[str]] = {}
        for collection in collections:
            records = legacy.records(collection)
            plan.append((collection, records))
            for record in records:
                for field_name in FILE_FIELD_ROLES:
                    for file_id in _parse_file_ids(record.get(field_name)):
                        needed_ids.setdefault(file_id, []).append(
                            f"{collection.name}/{record.get(TITLE_FIELD)}"
                        )

        library = (
            None
            if dry_run
            else Library.open_or_create(target_root, name=Path(target_root).name)
        )
        try:
            stats.file_mode = mode
            if include_files and not dry_run:
                _prefetch_files(
                    legacy, library, needed_ids, stats, mode=mode, workers=workers,
                    report=report,
                )
            else:
                stats.files_missing = sum(
                    1 for file_id in needed_ids if legacy.file(file_id) is None
                )

            # ---- 第二步：建项目 / 节点 / 文档 / 版本
            total_collections = len(plan)
            for index, (collection, records) in enumerate(plan, start=1):
                report(
                    "import", index - 1, total_collections,
                    f"导入「{collection.name}」（{len(records)} 条记录）",
                )
                if not dry_run:
                    _import_collection(
                        library, legacy, collection, records, stats,
                        group_by_tag=group_by_tag, include_files=include_files,
                        dry_run=dry_run, mode=mode,
                    )
                else:
                    _count_collection(collection, records, stats)
                report("import", index, total_collections, f"完成「{collection.name}」")

            # ---- 第三步：依赖关系（需要所有文档都已存在）
            if not dry_run:
                report("links", 0, total_collections, "建立文档依赖关系")
                _import_dependency_links(library, plan, stats, group_by_tag=group_by_tag)
        finally:
            if library is not None:
                library.close()
    finally:
        legacy.close()

    stats.elapsed_seconds = (datetime.now() - started).total_seconds()
    return stats


def _prefetch_files(
    legacy: LegacyLibrary,
    library: Library,
    needed_ids: dict[int, list[str]],
    stats: ImportStats,
    *,
    mode: str,
    workers: int,
    report: Progress,
) -> None:
    """把旧库文件并行收进 blob 库。

    先做这一步是为了：① 哈希是导入里最慢的环节，并行能把十几 GB 的时间压下来；
    ② 用硬链接而非复制，磁盘占用几乎为零；
    ③ 之后建版本时哈希缓存命中，逐条建版本几乎瞬时。

    并发只碰文件系统（哈希 + 链接），**不碰数据库连接** ——
    SQLite 连接不可跨线程使用，DB 写入全部留在主线程。
    """
    tasks: list[tuple[Path, str]] = []
    for file_id in needed_ids:
        item = legacy.file(file_id)
        if item is None:
            stats.files_missing += 1
            stats.errors.append(f"文件 id {file_id} 在 files 表中不存在")
            continue
        if item.path is None:
            stats.files_missing += 1
            stats.errors.append(f"文件「{item.name}」在磁盘上缺失")
            continue
        tasks.append((item.path, item.name))

    total = len(tasks)
    if total == 0:
        return

    def handle(task: tuple[Path, str]) -> tuple[str, int]:
        path, name = task
        if mode == "reference":
            # 只读原文件算指纹，不复制、不移动、不改动
            info = library.store.register_reference(path, keep_name_hint=name)
        else:
            info = library.store.put_file(path, keep_name_hint=name, mode=mode)
        return info.sha256, info.size

    done = 0
    unique: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 16))) as pool:
        for sha256, size in pool.map(handle, tasks):
            done += 1
            unique.setdefault(sha256, size)
            if done % 20 == 0 or done == total:
                report("files", done, total, f"已处理 {done}/{total} 个文件")

    # 账目按「内容」计：同一内容只登记一次，重复的旧文件自动合并
    if mode == "reference":
        stats.files_referenced = len(unique)
        stats.bytes_referenced = sum(unique.values())
    else:
        stats.files_stored = len(unique)
        stats.files_reused = total - len(unique)
        stats.bytes_stored = sum(unique.values())


def _count_collection(collection: LegacyCollection, records: list[dict], stats: ImportStats) -> None:
    """预演模式：只统计将要导入什么（不写库、不读文件内容）。"""
    stats.projects += 1
    groups = _group_by_title(records)
    stats.documents += len(groups)
    stats.versions += len(records)

    tag_names = set()
    for record in records:
        tag = _tag_name(collection, record)
        if tag:
            tag_names.add(tag)
            stats.tag_assignments += 1
    stats.nodes += len(tag_names)
    stats.tags += len(tag_names)

    # 依赖边：只在同一集合内按标题解析（与真正导入的逻辑一致）
    for record in records:
        raw = record.get(DEPS_FIELD)
        if not raw:
            continue
        for chunk in str(raw).replace("，", ",").replace("、", ",").split(","):
            title = chunk.strip()
            if title and title in groups and title != (record.get(TITLE_FIELD) or "").strip():
                stats.links += 1


def _group_by_title(records: list[dict]) -> dict[str, list[dict]]:
    """按标题分组 —— 同一标题的多条记录即一份文档的多个版本。"""
    groups: dict[str, list[dict]] = {}
    for order, record in enumerate(records):
        title = (record.get(TITLE_FIELD) or "").strip() or f"未命名任务 {record.get('_id')}"
        record["_order"] = order
        groups.setdefault(title, []).append(record)
    return groups


def _version_sort_key(record: dict) -> tuple[int, str, int]:
    """版本排序：先按「版本」字段，再按「修改时间」，最后按原始顺序。

    作者库里偶尔出现两个记录版本号相同（历史遗留），这条兜底保证顺序稳定且可复现。
    """
    version = _int_or_none(record.get(VERSION_FIELD)) or 0
    stamp = str(record.get(MODIFIED_FIELD) or "")
    return (version, stamp, int(record.get("_order") or 0))


def _tag_name(collection: LegacyCollection, record: dict) -> str:
    """把「标签」字段（下拉索引）翻译成文本。"""
    raw = record.get(TAG_FIELD)
    if raw is None or str(raw).strip() == "":
        return ""
    field = next((item for item in collection.fields if item.name == TAG_FIELD), None)
    items = field.combo_items if field else []
    index = _int_or_none(raw)
    # 旧实现存的是 currentIndex（0 基）
    if index is not None and 0 <= index < len(items):
        return items[index]
    text = str(raw).strip()
    return text if not text.isdigit() else ""


def _import_collection(
    library: Library,
    legacy: LegacyLibrary,
    collection: LegacyCollection,
    records: list[dict],
    stats: ImportStats,
    *,
    group_by_tag: bool,
    include_files: bool,
    dry_run: bool,
    mode: str = "reference",
) -> None:
    project = library.find_project(collection.name)
    if project is None:
        project = library.create_project(
            collection.name, description=f"由旧库 collection #{collection.id} 导入"
        )
        stats.projects += 1
    root_node = library.node_tree(project.id)[0]

    tag_nodes: dict[str, int] = {}
    tag_names: set[str] = set()
    groups = _group_by_title(records)
    files_by_name = legacy.files()

    for title, group in groups.items():
        group.sort(key=_version_sort_key)
        first = group[0]

        # 标签 -> 子节点（形成「项目 → 专业/子系统 → 任务」两级树）；无标签的挂在项目根下
        tag_name = _tag_name(collection, first)
        node_id = root_node.id
        if group_by_tag and tag_name:
            node_id = tag_nodes.get(tag_name, 0)
            if not node_id:
                existing = next(
                    (
                        item
                        for item in library.list_nodes(project.id)
                        if item.parent_id == root_node.id and item.name == tag_name
                    ),
                    None,
                )
                if existing is not None:
                    node_id = existing.id
                else:
                    node_id = library.create_node(
                        project.id, tag_name, parent_id=root_node.id, kind="group"
                    ).id
                    stats.nodes += 1
                tag_nodes[tag_name] = node_id

        document = library.find_document_by_title(node_id, title)
        if document is None:
            description = str(first.get(DESCRIPTION_FIELD) or "").strip()
            finished = bool(_int_or_none(first.get(DONE_FIELD)) or 0)
            document = library.create_document(
                node_id,
                title,
                kind="file",
                description=description,
                progress=_int_or_none(first.get(PROGRESS_FIELD)),
                done=finished,
                # 旧库的「完成」勾选 -> 新库的个人状态（不是审批结论）
                state="done" if finished else "active",
                due_date=_local_iso_to_utc(first.get(DUE_FIELD)),
                task_date=_local_iso_to_utc(first.get(TASK_DATE_FIELD)),
                people=str(first.get(PEOPLE_FIELD) or "").strip(),
                extra=json.dumps(
                    {"legacy_collection": collection.name, "legacy_id": first.get("_id")},
                    ensure_ascii=False,
                ),
            )
            stats.documents += 1
        else:
            # 该文档已存在（重复导入）：把它下面的记录数一并记为跳过，账目才对得上
            stats.skipped_records += len(group)
            continue

        if tag_name:
            if tag_name not in tag_names:
                library.add_tag(project.id, tag_name)
                tag_names.add(tag_name)
                stats.tags += 1
            library.tag_document(document.id, library.add_tag(project.id, tag_name))
            stats.tag_assignments += 1

        parent_version_id: int | None = None
        for position, record in enumerate(group, start=1):
            seq = _int_or_none(record.get(VERSION_FIELD))
            if seq is None or seq < 1:
                seq = position
            created_at = _local_iso_to_utc(record.get(MODIFIED_FIELD)) or utcnow_iso()
            specs: list[FileSpec] = []
            if include_files:
                for field_name, role in FILE_FIELD_ROLES.items():
                    for file_id in _parse_file_ids(record.get(field_name)):
                        item = files_by_name.get(int(file_id))
                        if item is None or item.path is None:
                            continue
                        specs.append(
                            FileSpec(
                                path=item.path,
                                role=role,
                                name=item.name,
                                # 旧库里记着"这份文件当初从哪个目录导入"，
                                # 带过来，界面上的「打开原始目录」才有意义
                                source_path=item.original_dir_path or None,
                            )
                        )

            note = str(record.get(DESCRIPTION_FIELD) or "").strip()

            def make_version(explicit_seq: int | None):
                return library.create_version(
                    document.id,
                    files=specs or None,
                    text=None if specs else (
                        note or f"旧库记录 #{record.get('_id')}（此版本未登记文件）"
                    ),
                    note=note,
                    author=str(record.get(PEOPLE_FIELD) or "").strip(),
                    origin="import",
                    parent_version_id=parent_version_id,
                    review_state="approved" if (_int_or_none(record.get(DONE_FIELD)) or 0) else "draft",
                    force=True,
                    created_at=created_at,
                    extra=json.dumps({"legacy_row": record.get("_id")}, ensure_ascii=False),
                    seq=explicit_seq,
                    file_mode=mode,
                )

            try:
                version, _ = make_version(seq)
            except LibraryError as error:
                if "已存在 v" in str(error):
                    # 旧库历史遗留：同一个标题下有两条记录的「版本」字段都是 1。
                    # 记录本身是有效数据，不能因为编号撞车就丢掉 —— 退化为按顺序追加。
                    try:
                        version, _ = make_version(None)
                        stats.errors.append(
                            f"{title}：旧库版本号 {seq} 重复，已按顺序追加为 v{version.seq}"
                        )
                    except LibraryError as inner:
                        stats.errors.append(f"{title} v{seq}：{inner}")
                        continue
                else:
                    stats.errors.append(f"{title} v{seq}：{error}")
                    continue
            parent_version_id = version.id
            stats.versions += 1


def backfill_original_dirs(library_root: str | Path, legacy_root: str | Path) -> int:
    """把旧库的「原始导入目录」补进已经迁移好的库里。

    迁移时只记了归档目录（``files/<hash>/``），但旧库的 ``files.original_dir_path``
    记着这份文件**当初是从哪儿导入的** —— 那才是"打开原始目录"想去的地方。
    这里按 ``<hash>/<name>`` 反查旧库，把 source_path 补成真正的原始目录。
    """
    legacy = LegacyLibrary(legacy_root)
    library = Library.open(library_root)
    updated = 0
    try:
        by_hash_name = {
            item.hash_name: item.original_dir_path
            for item in legacy.files().values()
            # 旧库里有些是 "." 这类相对值，补过来也没有意义，只收绝对路径
            if item.original_dir_path and Path(item.original_dir_path).is_absolute()
        }
        if not by_hash_name:
            return 0
        rows = library.db.query(
            "SELECT id, location, name, source_path FROM version_files "
            "WHERE location != ''"
        )
        legacy_files_root = str(legacy.files_root)
        for row in rows:
            location = row["location"]
            if legacy_files_root not in location:
                continue
            # location = <旧库>/files/<hash>/<name> -> 取 <hash>/<name>
            relative = location.split(legacy_files_root.rstrip("/") + "/", 1)[-1]
            original = by_hash_name.get(relative)
            if not original or original == row["source_path"]:
                continue
            with library.db.transaction():
                library.db.execute(
                    "UPDATE version_files SET source_path = ? WHERE id = ?",
                    (original, row["id"]),
                )
            updated += 1
    finally:
        library.close()
        legacy.close()
    return updated


def _import_dependency_links(
    library: Library,
    plan: list[tuple[LegacyCollection, list[dict]]],
    stats: ImportStats,
    *,
    group_by_tag: bool,
) -> None:
    """把「依赖任务」文本转成文档间的 depends_on 边（按标题在同项目内解析）。"""
    for collection, records in plan:
        project = library.find_project(collection.name)
        if project is None:
            continue
        documents = {
            document.title: document
            for document in library.list_documents(
                library.node_tree(project.id)[0].id, include_descendants=True
            )
        }
        for record in records:
            title = (record.get(TITLE_FIELD) or "").strip()
            source = documents.get(title)
            if source is None:
                continue
            raw = record.get(DEPS_FIELD)
            if not raw:
                continue
            for chunk in str(raw).replace("，", ",").replace("、", ",").split(","):
                target_title = chunk.strip()
                if not target_title:
                    continue
                target = documents.get(target_title)
                if target is None or target.id == source.id:
                    continue
                library.link_documents(source.id, target.id, "depends_on", "依赖任务")
                stats.links += 1

"""库（Library）：ToOak 的全部业务语义都收敛在这里。

核心概念
--------
**项目 (project)** → **节点 (node)** → **文档 (document)** → **版本 (version)**

* 文档的身份是它的**标题**。同一节点下再归档一个同名文件，不会产生第二份文档，
  而是给这份文档加一个新版本 —— 这正是「同一节点文档在不同时期的状态」。
* 一个版本是一份**文件集合**：主文件 + 若干附件，因此一个版本的形态可以是
  「一份 docx」「一份图纸 + 两份数据表」「一段笔记」等，这是多形式归档的基础。
* 版本之间存在 DAG：``parent_version_id`` 记录演化顺序；``version_links`` 额外记录
  「回到旧版(restore) 」「分支(branch)」「合并(merge)」这类语义边，供流程视图绘制。
* 版本带审阅状态（draft / pending / approved / rejected），Web 端「确认版本」写这里。

本模块**不依赖任何 GUI 框架**，桌面端、CLI、HTTP API 共用同一套语义。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .db import Database, DatabaseError, SCHEMA_VERSION
from .store import (FILE_MODES, SOURCE_REFERENCE, SOURCE_STORE, BlobInfo, BlobStore,
                    suffix_of)
from .util import (
    human_size,
    name_similarity,
    parse_iso,
    safe_filename,
    sha256_bytes,
    sha256_file as _file_sha256,
    short_hash,
    to_local_str,
    unique_path,
    utcnow_iso,
)

__all__ = [
    "LibraryError",
    "FileSpec",
    "Project",
    "Node",
    "Document",
    "VersionFile",
    "Version",
    "ArchiveResult",
    "LibraryInfo",
    "Library",
    "REVIEW_STATES",
]

#: **事件状态**：这是个人管理系统的核心状态，只有"我做到哪儿了"这一层含义。
#: 注意：这不是审批流 —— 没有批准/驳回、没有多级会签、没有权限角色。
DOC_STATES = ("active", "done", "on_hold")
DOC_STATE_LABELS = {
    "active": "进行中",
    "done": "已完成",
    "on_hold": "搁置",
}
#: 旧库里存放版本"审阅状态"的字段值。历史数据保留不动，
#: 但语义已改为「当时那份内容是不是定稿」，不再是审批结论。
REVIEW_STATES = ("draft", "pending", "approved", "rejected")
REVIEW_STATE_LABELS = {
    "draft": "草稿",
    "pending": "待定",
    "approved": "定稿",
    "rejected": "作废",
}
#: 版本来源
ORIGIN_LABELS = {
    "manual": "手工归档",
    "watch": "监视归档",
    "import": "批量导入",
    "restore": "回到旧版",
    "note": "笔记",
    "link": "链接",
}


#: 版本内文件的角色。旧库里的「输入文件/输出文件/参考数据/分析资料/图示」
#: 在新库里就是同一套角色体系，因此迁移时不需要改语义。
ROLE_LABELS = {
    #: 旧库里「任务说明」这类文字内容，现在归成"说明"这个环节，
    #: 不再伪装成一个文件名（早期把它当主文件，界面上会出现 <事件名>.md 这种假文件名）
    "note": "说明",
    "primary": "主文件",
    "attachment": "附件",
    "input": "输入文件",
    "output": "输出文件",
    "reference": "参考数据",
    "analysis": "分析资料",
    "figure": "图示",
}
#: 文档之间的链接类型
LINK_LABELS = {
    "depends_on": "依赖",
    "relates_to": "相关",
    "supersedes": "替代",
}

#: 文件名模糊配对的门槛（见 ``Library.counterpart_file`` 的第 3 条规则）。
#: 0.6 是**故意保守**的：宁可说"配不上"，也不能拿一份不相干的文件冒充
#: "上一稿的那一份"（那个结论会一路带进比较结果里给用户看）。
NAME_MATCH_FLOOR = 0.6


class LibraryError(Exception):
    """库层业务异常（路径非法、重名、跨项目移动等）。"""


# ---------------------------------------------------------------------------
# 数据对象
# ---------------------------------------------------------------------------


@dataclass
class LibraryInfo:
    """库自身的元信息（存于 <库根>/tooak.json，便于在文件系统层面识别）。"""

    root: Path
    name: str
    created_at: str
    schema_version: int = SCHEMA_VERSION

    def to_json(self) -> dict[str, Any]:
        return {
            "app": "ToOak",
            "name": self.name,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


@dataclass
class Project:
    id: int
    name: str
    description: str = ""
    color: str = "#4a90d9"
    created_at: str = ""
    updated_at: str = ""
    node_count: int = 0
    document_count: int = 0
    version_count: int = 0

    @classmethod
    def from_row(cls, row) -> "Project":
        keys = row.keys()
        return cls(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            color=row["color"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            node_count=row["node_count"] if "node_count" in keys else 0,
            document_count=row["document_count"] if "document_count" in keys else 0,
            version_count=row["version_count"] if "version_count" in keys else 0,
        )


@dataclass
class Node:
    id: int
    project_id: int
    parent_id: int | None
    name: str
    kind: str = "node"
    description: str = ""
    sort_key: int = 0
    created_at: str = ""
    updated_at: str = ""
    document_count: int = 0
    #: 运行时填充的子节点（tree() 使用）
    children: list["Node"] = field(default_factory=list)

    @classmethod
    def from_row(cls, row) -> "Node":
        keys = row.keys()
        return cls(
            id=row["id"],
            project_id=row["project_id"],
            parent_id=row["parent_id"],
            name=row["name"],
            kind=row["kind"],
            description=row["description"],
            sort_key=row["sort_key"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            document_count=row["document_count"] if "document_count" in keys else 0,
        )


@dataclass
class Document:
    id: int
    node_id: int
    title: str
    kind: str = "file"
    status: str = "active"
    description: str = ""
    current_version_id: int | None = None
    created_at: str = ""
    updated_at: str = ""
    version_count: int = 0
    latest_version_seq: int | None = None
    latest_review_state: str = ""
    # 任务型元数据（对应旧库的 进度/完成/限期/任务日期/相关人）
    progress: int | None = None
    done: int = 0
    due_date: str | None = None
    task_date: str | None = None
    people: str = ""
    extra: str = ""
    #: 事件状态：active 进行中 / done 已完成 / on_hold 搁置
    state: str = "active"

    @property
    def is_done(self) -> bool:
        return self.state == "done" or bool(self.done)

    @property
    def state_label(self) -> str:
        return DOC_STATE_LABELS.get(self.state, self.state)

    @property
    def people_list(self) -> list[str]:
        return [item.strip() for item in (self.people or "").split(",") if item.strip()]

    @classmethod
    def from_row(cls, row) -> "Document":
        keys = row.keys()

        def pick(name: str, default):
            return row[name] if name in keys else default

        return cls(
            id=row["id"],
            node_id=row["node_id"],
            title=row["title"],
            kind=row["kind"],
            status=row["status"],
            description=row["description"],
            current_version_id=row["current_version_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            version_count=pick("version_count", 0),
            latest_version_seq=pick("latest_version_seq", None),
            latest_review_state=pick("latest_review_state", ""),
            progress=pick("progress", None),
            done=pick("done", 0),
            due_date=pick("due_date", None),
            task_date=pick("task_date", None),
            people=pick("people", ""),
            extra=pick("extra", ""),
            state=pick("state", "done" if pick("done", 0) else "active"),
        )


@dataclass
class VersionFile:
    id: int
    version_id: int
    blob_id: int
    name: str
    role: str = "primary"
    seq: int = 0
    size: int = 0
    sha256: str = ""
    mime: str = ""
    source_path: str = ""
    rel_path: str = ""
    #: 引用模式下记录该文件当时的绝对路径（文件留在原处）
    location: str = ""
    #: 引用文件的当前状态：'ok' | 'missing' | 'changed' | ''（库内存储时为空）
    status: str = ""

    @property
    def short_sha(self) -> str:
        return short_hash(self.sha256)

    @property
    def is_external(self) -> bool:
        return bool(self.location)

    @classmethod
    def from_row(cls, row) -> "VersionFile":
        keys = row.keys()
        return cls(
            id=row["id"],
            version_id=row["version_id"],
            blob_id=row["blob_id"],
            name=row["name"],
            role=row["role"],
            seq=row["seq"],
            size=row["size"],
            sha256=row["sha256"],
            mime=row["mime"],
            source_path=row["source_path"],
            rel_path=row["rel_path"] if "rel_path" in keys else "",
            location=row["location"] if "location" in keys else "",
        )


@dataclass
class Version:
    id: int
    document_id: int
    seq: int
    parent_version_id: int | None = None
    primary_file_id: int | None = None
    source_name: str = ""
    source_path: str = ""
    sha256: str = ""
    size: int = 0
    mime: str = ""
    file_mtime: str | None = None
    author: str = ""
    #: 这一稿**没能带上内容**的沿用文件（原文件已经不在原处）——
    #: 只在内存里用，界面据此如实提示"这一稿少带了哪些文件"
    dropped: list[str] = field(default_factory=list)
    note: str = ""
    origin: str = "manual"
    review_state: str = "draft"
    created_at: str = ""
    extra: str = ""
    files: list[VersionFile] = field(default_factory=list)

    @property
    def short_sha(self) -> str:
        return short_hash(self.sha256)

    @property
    def label(self) -> str:
        return f"v{self.seq}"

    @property
    def primary_file(self) -> VersionFile | None:
        for item in self.files:
            if item.id == self.primary_file_id:
                return item
        for item in self.files:
            if item.role == "primary":
                return item
        return self.files[0] if self.files else None

    @classmethod
    def from_row(cls, row) -> "Version":
        keys = row.keys()

        def pick(name: str, default):
            return row[name] if name in keys else default

        return cls(
            id=row["id"],
            document_id=row["document_id"],
            seq=row["seq"],
            parent_version_id=row["parent_version_id"],
            primary_file_id=row["primary_file_id"],
            source_name=row["source_name"],
            source_path=row["source_path"],
            sha256=row["sha256"],
            size=row["size"],
            mime=row["mime"],
            file_mtime=row["file_mtime"],
            author=row["author"],
            note=row["note"],
            origin=row["origin"],
            review_state=row["review_state"],
            created_at=row["created_at"],
            extra=pick("extra", ""),
        )


@dataclass
class FileSpec:
    """待归档的一个文件。

    * 普通文件：给 ``path``，内容从磁盘读取；
    * 文本内容（笔记 / 链接）：给 ``text``，内容直接写入 blob 库，``path`` 仅作命名提示。
    """

    path: Path
    role: str = "primary"
    name: str | None = None
    text: str | None = None
    #: 已知内容指纹时直接带上（沿用上一稿的文件时避免重复读盘，
    #: 对几百 MB 的图纸尤其重要）
    sha256: str | None = None
    #: 这份文件**最初是从哪个目录**被归档进来的（用于"打开原始目录"）。
    #: 为 None 时取文件当前所在目录。
    source_path: str | None = None
    #: 沿用的上一稿记录（``carry_files`` / 「删掉一稿里的另一个文件」时用）：
    #: 大小与 mtime 都没变就**直接用回原来的 blob**，不再把整个文件读一遍算指纹
    #: （实测：一个 2.97 GB 的事件每归档一次就要读 2.97 GB）。
    reuse_blob_id: int = 0
    reuse_size: int = 0
    reuse_mime: str = ""
    reuse_location: str = ""
    #: 沿用但原文件已经不在原处了（内容取不回来，只保留记录）
    reuse_missing: bool = False

    @property
    def display_name(self) -> str:
        return self.name or (self.path.name if self.path.name else "未命名")

    @property
    def is_text(self) -> bool:
        return self.text is not None


@dataclass
class ArchiveResult:
    """一次归档的结果，供界面/CLI 反馈。"""

    document: Document
    version: Version
    created_document: bool = False
    deduplicated: bool = False

    @property
    def summary(self) -> str:
        if self.deduplicated:
            return f"内容未变化，仍为 v{self.version.seq}"
        action = "新建文档" if self.created_document else "新增版本"
        return f"{action}：{self.document.title} v{self.version.seq}"


# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------


#: 文档行 + 三个派生列（版本数 / 最新稿号 / 当前稿状态）。
#: 单独抽出来是为了让 ``get_document()`` 与 ``list_documents()`` 用**同一条** SELECT：
#: 两条查询一旦分头维护也容易悄悄长歪。
_DOCUMENT_COLUMNS = """
    d.*,
    (SELECT COUNT(*) FROM versions v WHERE v.document_id = d.id) AS version_count,
    (SELECT MAX(v.seq) FROM versions v WHERE v.document_id = d.id) AS latest_version_seq,
    (SELECT v.review_state FROM versions v WHERE v.id = d.current_version_id) AS latest_review_state
"""


class Library:
    """一个 ToOak 库的入口对象。

    线程注意：底层 SQLite 连接不跨线程。需要并发访问（HTTP 服务、后台监视）时，
    用 :meth:`session` 另开一个连接的 Library 实例，它们共享同一个库目录。
    """

    DB_FILENAME = "tooak.sqlite3"
    INFO_FILENAME = "tooak.json"
    #: 改名前的旧文件名（品牌统一成 tooak 之前叫 toak）。打开老库时**自动改名**，
    #: 这样"库文件名"也只有一种写法；改名只是 rename，不碰库里的任何数据。
    LEGACY_DB_FILENAMES = ("toak.sqlite3",)
    LEGACY_INFO_FILENAMES = ("toak.json",)

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.db = Database(self.root / self.DB_FILENAME)
        self.store = BlobStore(self.root)
        self.info = self._load_info()
        # 现在照旧版 C++ 用 files/<别名>/<原名>：打开时把旧布局搬过去（幂等）
        self._migrate_blob_layout()
        # 迁移后库根 = 原来的归档目录：reference 里"其实就在库 files/ 下"的记录
        # 从绝对路径改回相对路径，整个库才可搬（拷到 Windows 不再全部"缺失"）
        self._rebase_references()

    # ------------------------------------------------------------- 生命周期

    @classmethod
    def create(cls, root: str | Path, name: str | None = None) -> "Library":
        """新建一个库（目录已存在也安全，幂等）。"""
        root_path = Path(root).expanduser().resolve()
        root_path.mkdir(parents=True, exist_ok=True)
        (root_path / "files").mkdir(exist_ok=True)
        (root_path / "exports").mkdir(exist_ok=True)

        info_path = root_path / cls.INFO_FILENAME
        if not info_path.exists():
            info = LibraryInfo(
                root=root_path,
                name=name or root_path.name,
                created_at=utcnow_iso(),
            )
            info_path.write_text(
                json.dumps(info.to_json(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        library = cls(root_path)
        library.db.set_meta("library_name", library.info.name)
        return library

    @classmethod
    def open(cls, root: str | Path) -> "Library":
        """打开已有库；目录不存在或不是库时抛 LibraryError。

        结构层的问题（库比程序新、迁移中途被打断）也在这里翻译成 LibraryError，
        让界面能用一句话讲清楚，而不是甩一段 SQLite 的英文异常。
        """
        root_path = Path(root).expanduser().resolve()
        if not root_path.is_dir():
            raise LibraryError(f"库目录不存在：{root_path}")
        # 老名字（toak.*）先迁过来，再做"这是不是一个库"的判断 ——
        # 否则改名之前建的库会因为"缺少 tooak.sqlite3"被拒之门外
        cls._migrate_legacy_names(root_path)
        if not (root_path / cls.DB_FILENAME).exists() and not (
            root_path / cls.INFO_FILENAME
        ).exists():
            raise LibraryError(f"这不是一个 ToOak 库（缺少 {cls.DB_FILENAME}）：{root_path}")
        try:
            return cls(root_path)
        except DatabaseError as error:
            raise LibraryError(str(error)) from error

    @classmethod
    def _migrate_legacy_names(cls, root_path: Path) -> list[str]:
        """把老库的 ``toak.sqlite3`` / ``toak.json`` 改名成 ``tooak.*``（只改名，不动数据）。

        为什么自动做：库文件名是"程序自己认哪个文件"的约定，不该让用户手工改；
        改名是原子操作，也不涉及 blobs 里的任何内容。目标文件已经存在时**什么都不做**
        （说明这个库已经是新名字，旧文件只是残留），交由用户自己清理。
        """
        renamed: list[str] = []
        pairs = [(cls.DB_FILENAME, cls.LEGACY_DB_FILENAMES, ("-wal", "-shm")),
                 (cls.INFO_FILENAME, cls.LEGACY_INFO_FILENAMES, ())]
        for target_name, legacy_names, siblings in pairs:
            target = root_path / target_name
            if target.exists():
                continue
            for legacy_name in legacy_names:
                legacy = root_path / legacy_name
                if not legacy.exists():
                    continue
                try:
                    legacy.rename(target)
                    for suffix in siblings:
                        side = root_path / f"{legacy_name}{suffix}"
                        if side.exists():
                            side.rename(root_path / f"{target_name}{suffix}")
                except OSError:
                    # 改不动（只读目录等）：那就照旧用旧名字，别把库打开这件事搞失败
                    break
                renamed.append(f"{legacy_name} → {target_name}")
                break
        return renamed

    def _migrate_blob_layout(self) -> int:
        """

        放进库目录、保持原名"，早期却做成了内容寻址（``blobs/`` + 哈希改名）。
        这里把残留的库内副本**移动**到新布局（``os.replace`` 原子重命名），
        **只动 ``source='store'`` 的库内文件**，库外引用 / 用户原文件一概不碰；
        搬完之后把 ``blobs/`` 里"已经没有任何登记指向它"的孤儿文件清掉、目录删掉。
        幂等：搬完 rel_path 就不再以 ``blobs/`` 开头，下次打开不会重复处理。
        """
        rows = self.db.query(
            "SELECT b.id, b.rel_path, b.sha256, b.ext FROM blobs b "
            "WHERE b.source = 'store' AND b.rel_path LIKE 'blobs/%'"
        )
        moved = 0
        for row in rows:
            old = self.root / row["rel_path"]
            name_row = self.db.query_one(
                "SELECT vf.name FROM version_files vf WHERE vf.blob_id = ? "
                "ORDER BY vf.id LIMIT 1",
                (row["id"],),
            )
            name = (
                safe_filename(name_row["name"])
                if name_row is not None and name_row["name"]
                else f"{row['sha256'][:8]}.{row['ext'] or 'txt'}"
            )
            # 别名文件夹照 store 的逻辑生成（MD5(时间 + 唯一标识)），文件名保持原名
            rel = self.store.rel_path_for(f"{row['sha256']}:{name}", name)
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if old.is_file():
                try:
                    os.replace(old, target)
                except OSError:
                    continue          # 搬不动就留着这个登记，别把打开库这件事搞失败
            self.db.execute("UPDATE blobs SET rel_path = ? WHERE id = ?", (rel, row["id"]))
            moved += 1

        # 清掉 blobs/ 里"已经没有任何登记指向它"的孤儿文件，再把空目录一并删掉
        blobs_dir = self.root / "blobs"
        if blobs_dir.is_dir():
            referenced = {
                str(self.root / item["rel_path"])
                for item in self.db.query(
                    "SELECT rel_path FROM blobs WHERE rel_path LIKE 'blobs/%'"
                )
            }
            for path in sorted(blobs_dir.rglob("*"), reverse=True):
                if path.is_file() and str(path) not in referenced:
                    try:
                        path.unlink()
                    except OSError:
                        pass
            for path in sorted(
                (item for item in blobs_dir.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    path.rmdir()
                except OSError:
                    pass
            try:
                blobs_dir.rmdir()
            except OSError:
                pass
        return moved

    def _rebase_references(self) -> int:
        """把"指向库目录内、却记成绝对路径"的 reference 记录改回相对路径。

        迁移之后库根 = 原来的归档目录（例如 ``/home/…/Resources/ToOak``），
        815 条 reference 指向的 ``…/Resources/ToOak/files/<别名>/<原名>`` 其实就在
        库内；还记成绝对路径的话，整个库拷到 Windows 就全部"缺失"。
        这里把**在库 files/ 之下**的 reference 改成相对 ``files/…`` 并标记为
        ``store``（库自带），跨机器也能找到；指向库外的 reference 保持绝对路径不动。

        幂等：改完 rel_path 不再以 ``/`` 开头，下次打开不会重复处理。
        只改登记路径，**不移动文件**。
        """
        rows = self.db.query(
            "SELECT id, rel_path FROM blobs WHERE source = 'reference'"
        )
        rebased = 0
        for row in rows:
            rel_path = row["rel_path"]
            path = Path(rel_path)
            if not path.is_absolute():
                continue
            try:
                rel = path.relative_to(self.root)
            except ValueError:
                continue          # 在库外：保持绝对路径（用户别处的东西）
            parts = rel.parts
            if not parts or parts[0] != "files":
                continue          # 库内但不在 files/ 归档目录里（如 exports/）：不动
            rel_text = rel.as_posix()
            self.db.execute(
                "UPDATE blobs SET rel_path = ?, source = 'store' WHERE id = ?",
                (rel_text, row["id"]),
            )
            self.db.execute(
                "UPDATE version_files SET location = '' WHERE blob_id = ?",
                (row["id"],),
            )
            rebased += 1
        return rebased

    @classmethod
    def open_or_create(cls, root: str | Path, name: str | None = None) -> "Library":
        try:
            return cls.open(root)
        except LibraryError:
            return cls.create(root, name)

    def session(self) -> "Library":
        """为当前线程/协程另开一个独立连接的会话（共享同一库目录）。"""
        return Library(self.root)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _load_info(self) -> LibraryInfo:
        info_path = self.root / self.INFO_FILENAME
        if info_path.exists():
            try:
                data = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {}
            return LibraryInfo(
                root=self.root,
                name=data.get("name") or self.root.name,
                created_at=data.get("created_at") or utcnow_iso(),
                schema_version=int(data.get("schema_version") or SCHEMA_VERSION),
            )
        info = LibraryInfo(root=self.root, name=self.root.name, created_at=utcnow_iso())
        info_path.write_text(
            json.dumps(info.to_json(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return info

    # ---------------------------------------------------------------- 项目

    def create_project(
        self, name: str, description: str = "", color: str = "#4a90d9"
    ) -> Project:
        clean = (name or "").strip()
        if not clean:
            raise LibraryError("项目名不能为空")
        now = utcnow_iso()
        with self.db.transaction():
            if self.db.query_one(
                "SELECT id FROM projects WHERE name = ? COLLATE NOCASE", (clean,)
            ):
                raise LibraryError(f"项目已存在：{clean}")
            project_id = self.db.insert(
                "INSERT INTO projects(name, description, color, created_at, updated_at) "
                "VALUES(?,?,?,?,?)",
                (clean, description, color, now, now),
            )
            # 每个项目自带一个根节点，界面语义是「项目 > 根节点 > … 」
            self.db.insert(
                "INSERT INTO nodes(project_id, parent_id, name, kind, description, "
                "sort_key, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, None, clean, "root", "", 0, now, now),
            )
            self._log_event("project_created", message=clean, project_id=project_id)
        project = self.get_project(project_id)
        assert project is not None
        return project

    def list_projects(self) -> list[Project]:
        rows = self.db.query(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM nodes n WHERE n.project_id = p.id)      AS node_count,
                   (SELECT COUNT(*) FROM documents d JOIN nodes n ON n.id = d.node_id
                     WHERE n.project_id = p.id AND d.status = 'active')          AS document_count,
                   (SELECT COUNT(*) FROM versions v JOIN documents d ON d.id = v.document_id
                      JOIN nodes n ON n.id = d.node_id WHERE n.project_id = p.id) AS version_count
              FROM projects p
             ORDER BY p.name COLLATE NOCASE
            """
        )
        return [Project.from_row(row) for row in rows]

    def get_project(self, project_id: int) -> Project | None:
        row = self.db.query_one(
            """
            SELECT p.*,
                   (SELECT COUNT(*) FROM nodes n WHERE n.project_id = p.id)      AS node_count,
                   (SELECT COUNT(*) FROM documents d JOIN nodes n ON n.id = d.node_id
                     WHERE n.project_id = p.id AND d.status = 'active')          AS document_count,
                   (SELECT COUNT(*) FROM versions v JOIN documents d ON d.id = v.document_id
                      JOIN nodes n ON n.id = d.node_id WHERE n.project_id = p.id) AS version_count
              FROM projects p WHERE p.id = ?
            """,
            (project_id,),
        )
        return Project.from_row(row) if row else None

    def find_project(self, name: str) -> Project | None:
        row = self.db.query_one(
            "SELECT * FROM projects WHERE name = ? COLLATE NOCASE", (name.strip(),)
        )
        return self.get_project(row["id"]) if row else None

    def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        color: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if name is not None:
            clean = name.strip()
            if not clean:
                raise LibraryError("项目名不能为空")
            sets.append("name = ?")
            params.append(clean)
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if color is not None:
            sets.append("color = ?")
            params.append(color)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([utcnow_iso(), project_id])
        with self.db.transaction():
            self.db.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", params)

    def delete_project(self, project_id: int) -> None:
        """删除项目及其全部节点/文档/版本（blob 由 gc 回收）。"""
        with self.db.transaction():
            self.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    # ---------------------------------------------------------------- 节点

    def create_node(
        self,
        project_id: int,
        name: str,
        parent_id: int | None = None,
        kind: str = "node",
        description: str = "",
    ) -> Node:
        clean = (name or "").strip()
        if not clean:
            raise LibraryError("节点名不能为空")
        if parent_id is not None:
            parent = self.get_node(parent_id)
            if parent is None:
                raise LibraryError(f"父节点不存在：{parent_id}")
            if parent.project_id != project_id:
                raise LibraryError("父节点不属于该项目")

        now = utcnow_iso()
        with self.db.transaction():
            duplicated = self.db.query_one(
                "SELECT id FROM nodes WHERE project_id = ? AND IFNULL(parent_id, -1) = ? "
                "AND name = ? COLLATE NOCASE",
                (project_id, parent_id if parent_id is not None else -1, clean),
            )
            if duplicated:
                raise LibraryError(f"同级下已有同名节点：{clean}")
            sort_key = int(
                self.db.scalar(
                    "SELECT IFNULL(MAX(sort_key), 0) + 1 FROM nodes "
                    "WHERE project_id = ? AND IFNULL(parent_id, -1) = ?",
                    (project_id, parent_id if parent_id is not None else -1),
                )
                or 1
            )
            node_id = self.db.insert(
                "INSERT INTO nodes(project_id, parent_id, name, kind, description, "
                "sort_key, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, parent_id, clean, kind, description, sort_key, now, now),
            )
            self._log_event("node_created", project_id=project_id, node_id=node_id, message=clean)
        node = self.get_node(node_id)
        assert node is not None
        return node

    def get_node(self, node_id: int) -> Node | None:
        row = self.db.query_one(
            """
            SELECT n.*,
                   (SELECT COUNT(*) FROM documents d
                     WHERE d.node_id = n.id AND d.status = 'active') AS document_count
              FROM nodes n WHERE n.id = ?
            """,
            (node_id,),
        )
        return Node.from_row(row) if row else None

    def list_nodes(self, project_id: int) -> list[Node]:
        rows = self.db.query(
            """
            SELECT n.*,
                   (SELECT COUNT(*) FROM documents d
                     WHERE d.node_id = n.id AND d.status = 'active') AS document_count
              FROM nodes n
             WHERE n.project_id = ?
             ORDER BY n.sort_key, n.name COLLATE NOCASE
            """,
            (project_id,),
        )
        return [Node.from_row(row) for row in rows]

    def node_tree(self, project_id: int) -> list[Node]:
        """返回按 parent_id 组装好的节点树（根节点为 project 的 root 节点）。"""
        nodes = self.list_nodes(project_id)
        by_id = {node.id: node for node in nodes}
        roots: list[Node] = []
        for node in nodes:
            parent = by_id.get(node.parent_id) if node.parent_id else None
            if parent is None:
                roots.append(node)
            else:
                parent.children.append(node)
        return roots

    def node_path(self, node_id: int, separator: str = " / ") -> str:
        """节点的完整路径，如「某工程 / 结构 / 外壳」。"""
        parts: list[str] = []
        current = self.get_node(node_id)
        guard = 0
        while current is not None and guard < 64:
            parts.append(current.name)
            current = self.get_node(current.parent_id) if current.parent_id else None
            guard += 1
        return separator.join(reversed(parts))

    def rename_node(self, node_id: int, name: str) -> None:
        clean = name.strip()
        if not clean:
            raise LibraryError("节点名不能为空")
        node = self.get_node(node_id)
        if node is None:
            raise LibraryError(f"节点不存在：{node_id}")
        with self.db.transaction():
            clash = self.db.query_one(
                "SELECT id FROM nodes WHERE project_id = ? AND IFNULL(parent_id, -1) = ? "
                "AND name = ? COLLATE NOCASE AND id != ?",
                (node.project_id, node.parent_id if node.parent_id else -1, clean, node_id),
            )
            if clash:
                raise LibraryError(f"同级下已有同名节点：{clean}")
            self.db.execute(
                "UPDATE nodes SET name = ?, updated_at = ? WHERE id = ?",
                (clean, utcnow_iso(), node_id),
            )
            self._log_event(
                "node_renamed", project_id=node.project_id, node_id=node_id,
                message=f"{node.name} → {clean}",
            )

    def move_node(self, node_id: int, new_parent_id: int | None) -> None:
        """把节点移到另一个父节点下（保持项目内），并校验不能移进自己的后裔。

        ``descendant_ids`` 是**含自身**的（删节点时要连自己一起删），
        所以这里的自环判断必须先把自身减掉 —— 原来写成
        ``node_id in self.descendant_ids(node_id)`` 恒为真，
        于是**任何**移动都被拒：用户把一个节点拖到无关的同级下也没反应。

        减掉自身之后，要比的是**目标父节点**：要拦的是"把节点移进它自己的
        后代里"（`new_parent` 是 `node` 的后代才会成环）。若照字面把判据写成
        ``node_id in (descendant_ids(node_id) - {node_id})``，它恒为假，
        等于把这道环检测整个删掉 —— 树被移成环之后，节点树/删除都会失控。
        """
        node = self.get_node(node_id)
        if node is None:
            raise LibraryError(f"节点不存在：{node_id}")
        if new_parent_id is None:
            raise LibraryError("不能把节点移到项目之外（根节点不可移动）")
        new_parent = self.get_node(new_parent_id)
        if new_parent is None:
            raise LibraryError(f"目标节点不存在：{new_parent_id}")
        if new_parent.project_id != node.project_id:
            raise LibraryError("不支持跨项目移动节点")
        if node_id == new_parent_id or new_parent_id in (
            self.descendant_ids(node_id) - {node_id}
        ):
            raise LibraryError("不能把节点移动到它自己的子节点下")

        now = utcnow_iso()
        with self.db.transaction():
            sort_key = int(
                self.db.scalar(
                    "SELECT IFNULL(MAX(sort_key), 0) + 1 FROM nodes "
                    "WHERE project_id = ? AND parent_id = ? AND id != ?",
                    (node.project_id, new_parent_id, node_id),
                )
                or 1
            )
            self.db.execute(
                "UPDATE nodes SET parent_id = ?, sort_key = ?, updated_at = ? WHERE id = ?",
                (new_parent_id, sort_key, now, node_id),
            )
            self._log_event(
                "node_moved", project_id=node.project_id, node_id=node_id,
                message=f"{node.name} → {new_parent.name}",
            )

    def descendant_ids(self, node_id: int) -> set[int]:
        """所有后裔节点 id（含自身）。

        一次把整个项目的 ``(id, parent_id)`` 取回内存里走树，而不是
        "每碰到一个节点就查一次它的孩子" —— 后者在深树上是每节点一条 SQL
        （实测 60 个事件的库要跑几十次），结果完全一样。
        """
        result: set[int] = {node_id}
        project = self.db.query_one("SELECT project_id FROM nodes WHERE id = ?", (node_id,))
        if project is None:
            return result
        children: dict[int, list[int]] = {}
        for row in self.db.query(
            "SELECT id, parent_id FROM nodes WHERE project_id = ?", (project["project_id"],)
        ):
            if row["parent_id"] is not None:
                children.setdefault(int(row["parent_id"]), []).append(int(row["id"]))
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            for child in children.get(current, ()):
                if child not in result:      # 结果里已有就说明走过了，顺带防住环
                    result.add(child)
                    frontier.append(child)
        return result

    def delete_node(self, node_id: int) -> None:
        """删除节点（子节点与文档一并级联删除）。"""
        node = self.get_node(node_id)
        if node is None:
            return
        if node.kind == "root":
            raise LibraryError("项目根节点不能删除，请直接删除项目")
        with self.db.transaction():
            self.db.execute("DELETE FROM nodes WHERE id = ?", (node_id,))

    # ---------------------------------------------------------------- 文档

    def create_document(
        self,
        node_id: int,
        title: str,
        kind: str = "file",
        description: str = "",
        *,
        progress: int | None = None,
        done: bool = False,
        due_date: str | None = None,
        task_date: str | None = None,
        people: str = "",
        extra: str = "",
        state: str = "active",
    ) -> Document:
        clean = (title or "").strip()
        if not clean:
            raise LibraryError("标题不能为空")
        if self.get_node(node_id) is None:
            raise LibraryError(f"节点不存在：{node_id}")

        now = utcnow_iso()
        with self.db.transaction():
            if self.find_document_by_title(node_id, clean) is not None:
                raise LibraryError(f"该节点下已存在同名文档：{clean}")
            state = self.resolve_doc_state(state)
            document_id = self.db.insert(
                "INSERT INTO documents(node_id, title, kind, status, description, "
                "current_version_id, created_at, updated_at, progress, done, due_date, "
                "task_date, people, extra, state) "
                "VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?)",
                (
                    node_id, clean, kind, "active", description, now, now,
                    progress, 1 if (done or state == "done") else 0, due_date,
                    task_date, people, extra, state,
                ),
            )
        document = self.get_document(document_id)
        assert document is not None
        return document

    def get_document(self, document_id: int) -> Document | None:
        row = self.db.query_one(
            f"SELECT {_DOCUMENT_COLUMNS} FROM documents d WHERE d.id = ?",
            (document_id,),
        )
        return Document.from_row(row) if row else None

    def find_document_by_title(self, node_id: int, title: str) -> Document | None:
        row = self.db.query_one(
            "SELECT id FROM documents WHERE node_id = ? AND title = ? COLLATE NOCASE",
            (node_id, (title or "").strip()),
        )
        return self.get_document(row["id"]) if row else None

    def list_documents(
        self, node_id: int, include_descendants: bool = False, include_archived: bool = False
    ) -> list[Document]:
        """节点下的文档（``include_descendants`` 时含所有下层的）。

        一个装了 60 个事件的项目要跑 61 条 SQL（打开项目就能感觉到）。
        排序、过滤条件、返回的 ``Document`` 字段都与以前一致。
        """
        params: list[Any] = []
        if include_descendants:
            ids = sorted(self.descendant_ids(node_id))
            placeholders = ",".join("?" for _ in ids)
            where = f"d.node_id IN ({placeholders})"
            params.extend(ids)
        else:
            where = "d.node_id = ?"
            params.append(node_id)
        if not include_archived:
            where += " AND d.status = 'active'"
        rows = self.db.query(
            f"SELECT {_DOCUMENT_COLUMNS} FROM documents d WHERE {where} "
            "ORDER BY d.title COLLATE NOCASE",
            params,
        )
        return [Document.from_row(row) for row in rows]

    def rename_document(self, document_id: int, new_title: str) -> None:
        """改标题。注意：标题即身份，改标题等于给这份文档「改名」，
        不会与另一份同名文档自动合并（这是有意的，避免误并）。"""
        clean = new_title.strip()
        if not clean:
            raise LibraryError("标题不能为空")
        document = self.get_document(document_id)
        if document is None:
            raise LibraryError(f"文档不存在：{document_id}")
        with self.db.transaction():
            clash = self.db.query_one(
                "SELECT id FROM documents WHERE node_id = ? AND title = ? COLLATE NOCASE "
                "AND id != ?",
                (document.node_id, clean, document_id),
            )
            if clash:
                raise LibraryError(f"该节点下已存在同名文档：{clean}")
            self.db.execute(
                "UPDATE documents SET title = ?, updated_at = ? WHERE id = ?",
                (clean, utcnow_iso(), document_id),
            )
            self._log_event(
                "document_renamed", document_id=document_id,
                message=f"{document.title} → {clean}",
            )

    def update_document(
        self,
        document_id: int,
        *,
        description: str | None = None,
        status: str | None = None,
        progress: int | None = None,
        done: bool | None = None,
        due_date: str | None = None,
        task_date: str | None = None,
        people: str | None = None,
        extra: str | None = None,
        state: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if description is not None:
            sets.append("description = ?")
            params.append(description)
        if status is not None:
            if status not in ("active", "archived"):
                raise LibraryError(f"非法状态：{status}")
            sets.append("status = ?")
            params.append(status)
        if progress is not None:
            sets.append("progress = ?")
            params.append(progress)
        if done is not None:
            sets.append("done = ?")
            params.append(1 if done else 0)
        if state is not None:
            state = self.resolve_doc_state(state)
            sets.append("state = ?")
            params.append(state)
            if state == "done":
                sets.append("done = 1")
            elif state == "active":
                sets.append("done = 0")
        if due_date is not None:
            sets.append("due_date = ?")
            params.append(due_date)
        if task_date is not None:
            sets.append("task_date = ?")
            params.append(task_date)
        if people is not None:
            sets.append("people = ?")
            params.append(people)
        if extra is not None:
            sets.append("extra = ?")
            params.append(extra)
        if not sets:
            return
        sets.append("updated_at = ?")
        params.extend([utcnow_iso(), document_id])
        with self.db.transaction():
            self.db.execute(f"UPDATE documents SET {', '.join(sets)} WHERE id = ?", params)

    def set_document_updated_at(self, document_id: int, stamp: str) -> None:
        """直接设置文档的更新时间（导入历史数据时用，保持原库的时间线）。"""
        with self.db.transaction():
            self.db.execute(
                "UPDATE documents SET updated_at = ? WHERE id = ?", (stamp, document_id)
            )

    @staticmethod
    def resolve_doc_state(state: str) -> str:
        if state not in DOC_STATES:
            raise LibraryError(f"未知的事件状态：{state}（可选 {list(DOC_STATES)}）")
        return state

    def set_document_state(self, document_id: int, state: str) -> None:
        """设置事件状态：进行中 / 已完成 / 搁置。

        这是个人管理里的"我做到哪儿了"，不是审批结论 —— 随时可以改回来。
        """
        self.update_document(document_id, state=state)

    #: 这几个字段是"可以清空的"：``update_document`` 里 ``None`` 表示"这次不改"，
    #: 所以要把「限期」这类值删掉必须走 :meth:`set_document_field`。
    CLEARABLE_FIELDS = ("task_date", "due_date", "progress")

    def set_document_field(self, document_id: int, field: str, value: Any) -> None:
        """设置一个可清空的字段；传 ``None`` / ``""`` 表示清掉（存 NULL）。"""
        if field not in self.CLEARABLE_FIELDS:
            raise LibraryError(
                f"不能这样改的字段：{field}（可选 {list(self.CLEARABLE_FIELDS)}）"
            )
        stored = value if value not in (None, "") else None
        with self.db.transaction():
            self.db.execute(
                f"UPDATE documents SET {field} = ?, updated_at = ? WHERE id = ?",
                (stored, utcnow_iso(), document_id),
            )

    def consolidate_version_notes(self) -> dict[str, int]:
        """把事件级的"说明"并进**各稿的说明**里，然后清空事件级字段（幂等）。

        旧库导入时同一段文字被写到了两个地方（``documents.description`` 与
        "说明"、按稿存、各稿可以不一样。规矩：

        * 哪一稿还没有说明 → 把事件级这段抄进去；
        * 所有稿都已经有自己的说明、且都不含这段文字 → 追加到**最新一稿**上
          （宁可多点文字，也绝不丢）；
        * 处理完清空 ``documents.description``，从此只有"这一稿的说明"。
        """
        rows = self.db.query(
            "SELECT id, description FROM documents "
            "WHERE description IS NOT NULL AND description <> ''"
        )
        filled = appended = cleared = 0
        for row in rows:
            document_id = int(row["id"])
            text = str(row["description"]).strip()
            if not text:
                continue
            history = self.version_history(document_id)
            if not history:
                continue
            empty = [v for v in history if not (v.note or "").strip()]
            for version in empty:
                self.set_version_note(version.id, text)
                filled += 1
            if not empty and not any(text in (v.note or "") for v in history):
                newest = history[-1]
                merged = ((newest.note or "").strip() + "\n" + text).strip()
                self.set_version_note(newest.id, merged)
                appended += 1
            with self.db.transaction():
                self.db.execute(
                    "UPDATE documents SET description = '' WHERE id = ?", (document_id,)
                )
            cleared += 1
        return {"documents": cleared, "filled": filled, "appended": appended}

    def set_version_note(self, version_id: int, note: str) -> None:
        """**整段替换**某一稿的说明（界面上的"说明"框编辑的就是它）。

        和 :meth:`add_version_note`（追加一条）区分开：
        说明是像"描述"那样编辑的，改完就是改完，不该一条条往上堆。
        """
        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        with self.db.transaction():
            self.db.execute(
                "UPDATE versions SET note = ? WHERE id = ?", ((note or "").strip(), version_id)
            )
        self._log_event(
            "version_note", document_id=version.document_id, version_id=version_id,
            message=(note or "").strip()[:200],
        )

    def add_version_note(
        self, version_id: int, note: str, author: str = ""
    ) -> None:
        """给某一稿追加一条说明（内部仍是 versions.note 字段）。

        个人系统里不需要"谁批准了谁"，但需要"当时为什么是这个样子"。
        """
        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        existing = (version.note or "").strip()
        merged = f"{existing}\n{note}".strip() if existing else note.strip()
        with self.db.transaction():
            self.db.execute(
                "UPDATE versions SET note = ?, author = COALESCE(NULLIF(?, ''), author) "
                "WHERE id = ?",
                (merged, author, version_id),
            )
            self._log_event(
                "version_note", document_id=version.document_id, version_id=version_id,
                message=note.strip()[:200],
            )

    # ---------------------------------------------------------- 版图位置

    def set_document_position(self, document_id: int, x: float, y: float) -> None:
        """记住某个事件在版图上的位置（用户拖动之后调用）。

        这是**用户自己画的图**，所以位置要持久化 —— 否则每次刷新都被自动布局
        冲回原位，等于白拖。
        """
        row = self.db.query_one(
            "SELECT node_id FROM documents WHERE id = ?", (document_id,)
        )
        if row is None:
            raise LibraryError(f"事件不存在：{document_id}")
        node = self.get_node(row["node_id"])
        if node is None:
            raise LibraryError("事件所属项目不存在")
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO graph_positions(document_id, project_id, x, y, updated_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET "
                "x = excluded.x, y = excluded.y, updated_at = excluded.updated_at",
                (document_id, node.project_id, float(x), float(y), utcnow_iso()),
            )

    def document_positions(self, project_id: int) -> dict[int, tuple[float, float]]:
        """取项目里所有手工摆放过的节点位置。"""
        rows = self.db.query(
            "SELECT document_id, x, y FROM graph_positions WHERE project_id = ?",
            (project_id,),
        )
        return {int(row["document_id"]): (float(row["x"]), float(row["y"])) for row in rows}

    def clear_document_position(self, document_id: int) -> None:
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM graph_positions WHERE document_id = ?", (document_id,)
            )

    def clear_project_positions(self, project_id: int) -> None:
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM graph_positions WHERE project_id = ?", (project_id,)
            )

    def dependency_alerts(
        self, project_id: int | None = None
    ) -> dict[int, list[str]]:
        """哪些事件"依赖的东西更新了、自己还没跟"：``{事件 id: [被依赖事件标题…]}``。

        判据很直白（不猜、不推断）：

        * 线是**依赖**：``A → B`` 表示 A 依赖 B（沿用旧库「依赖任务」的方向，
          也和时间轴的箭头、分层布局一致）；
        * 只要 **B 最新一稿的时间 > A 当前稿的时间**，就说 A 该看一眼 B 了 ——
          B 动了、A 还是老的。

        依赖它的节点要告警标志显示"。

        只认 ``kind = 'depends_on'``：「相关」「替代」只是人画的参考线，
        被它们连上的事件并没有"我得跟着更新"的义务 —— 不过滤的话，
        任何一条 relate_to 连线都会让白板报出假的"依赖告警"。
        """
        params: list[Any] = []
        # 连线类型是这里的核心条件，和"未归档"一样属于必须成立的前提，
        # 因此直接写进 WHERE 而不是查完再过滤（少把无关的连线搬进内存）
        where = "WHERE d1.status != 'archived' AND l.kind = 'depends_on'"
        if project_id is not None:
            where += " AND n1.project_id = ?"
            params.append(project_id)
        rows = self.db.query(
            f"""
            SELECT l.from_document_id AS source_id,
                   d2.title           AS dep_title,
                   (SELECT v.created_at FROM versions v WHERE v.id = d1.current_version_id)
                       AS own_time,
                   (SELECT MAX(v2.created_at) FROM versions v2 WHERE v2.document_id = d2.id)
                       AS dep_latest,
                   (SELECT MAX(v3.created_at) FROM versions v3
                     WHERE v3.document_id = d1.id) AS own_latest
              FROM document_links l
              JOIN documents d1 ON d1.id = l.from_document_id
              JOIN documents d2 ON d2.id = l.to_document_id
              JOIN nodes n1     ON n1.id = d1.node_id
              {where}
            """,
            params,
        )
        alerts: dict[int, list[str]] = {}
        for row in rows:
            dep_latest = row["dep_latest"] or ""
            own_time = row["own_time"] or row["own_latest"] or ""
            if dep_latest and own_time and dep_latest > own_time:
                titles = alerts.setdefault(int(row["source_id"]), [])
                title = str(row["dep_title"])
                # 同一对事件之间可能记了「依赖 + 相关」两条线：标题只报一次，
                # 否则告警里同一个名字出现两遍，用户会以为有两个上游
                if title not in titles:
                    titles.append(title)
        return alerts

    # --------------------------------------------- 白板：说明文本框 / 分组 / 连接曲度

    #: 白板卡片的种类：说明框 / 文件素材 / 网页链接
    CARD_KINDS = ("note", "file", "link")
    #: 卡片字段的白名单（更新时只认这些列，避免 SQL 拼接出意外）
    _CARD_FIELDS = ("kind", "x", "y", "w", "h", "title", "text", "path", "url",
                    "origin", "color")

    def board_cards(self, project_id: int,
                    kind: str | None = None) -> list[dict[str, Any]]:
        """白板上的卡片：说明框、文件素材、网页链接（**都还没归档**）。

        卡片是"待归档的引用"：文件卡片只记路径，绝不复制、绝不移动用户的文件；
        转为事件时才真正进库（见 :meth:`promote_card_to_event`）。
        """
        clauses = ["project_id = ?"]
        params: list[Any] = [project_id]
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind)
        return [
            dict(row)
            for row in self.db.query(
                f"SELECT id, kind, x, y, w, h, title, text, path, url, origin, color "
                f"FROM board_cards WHERE {' AND '.join(clauses)} ORDER BY id",
                params,
            )
        ]

    def get_board_card(self, card_id: int) -> dict[str, Any] | None:
        row = self.db.query_one(
            "SELECT id, project_id, kind, x, y, w, h, title, text, path, url, "
            "origin, color FROM board_cards WHERE id = ?",
            (card_id,),
        )
        return dict(row) if row is not None else None

    def add_board_card(self, project_id: int, kind: str, x: float, y: float, *,
                       title: str = "", text: str = "", path: str = "",
                       url: str = "", w: float = 260.0, h: float = 0.0,
                       origin: str = "user", color: str = "") -> int:
        """在白板上放一张卡片（说明框 / 文件素材 / 链接）。"""
        if kind not in self.CARD_KINDS:
            raise LibraryError(f"不认识的卡片类型：{kind}")
        now = utcnow_iso()
        with self.db.transaction():
            cursor = self.db.execute(
                "INSERT INTO board_cards(project_id, kind, x, y, w, h, title, text, "
                "path, url, origin, color, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, kind, float(x), float(y), float(w), float(h), title, text,
                 path, url, origin, color, now, now),
            )
            return int(cursor.lastrowid)

    def update_board_card(self, card_id: int, **fields: Any) -> None:
        """改卡片（位置、大小、标题、正文、路径…）。只认白名单里的列。"""
        updates: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in self._CARD_FIELDS:
                raise LibraryError(f"卡片没有这个字段：{key}")
            if value is None:
                continue
            updates.append(f"{key} = ?")
            params.append(value)
        if not updates:
            return
        updates.append("updated_at = ?")
        params.append(utcnow_iso())
        params.append(card_id)
        with self.db.transaction():
            self.db.execute(
                f"UPDATE board_cards SET {', '.join(updates)} WHERE id = ?", params
            )

    def delete_board_card(self, card_id: int) -> None:
        """删掉一张卡片（只是白板上的便利贴/引用，**不碰任何文件与归档**）。"""
        with self.db.transaction():
            self.db.execute("DELETE FROM board_cards WHERE id = ?", (card_id,))

    # ---- 说明框（kind='note'）的老接口：白板上的文字卡片，语义没变 ----

    def board_notes(self, project_id: int) -> list[dict[str, Any]]:
        """说明框 = kind='note' 的卡片（保持老接口，界面/测试都不用改）。"""
        return [
            {"id": card["id"], "x": card["x"], "y": card["y"], "width": card["w"],
             "text": card["text"], "color": card["color"]}
            for card in self.board_cards(project_id, kind="note")
        ]

    def add_board_note(self, project_id: int, x: float, y: float, text: str = "",
                       width: float = 260.0, color: str = "") -> int:
        return self.add_board_card(project_id, "note", x, y, text=text,
                                   w=width, color=color)

    def update_board_note(self, note_id: int, *, x: float | None = None,
                          y: float | None = None, width: float | None = None,
                          text: str | None = None, color: str | None = None) -> None:
        fields: dict[str, Any] = {}
        for column, value in (("x", x), ("y", y), ("w", width),
                              ("text", text), ("color", color)):
            if value is not None:
                fields[column] = value
        self.update_board_card(note_id, **fields)

    def delete_board_note(self, note_id: int) -> None:
        self.delete_board_card(note_id)

    def restore_board_card(self, row: dict[str, Any]) -> int:
        """按**原来的 id** 把一张卡片放回去（撤销"删除卡片"用）。

        id 不变很重要：白板上的选中、分组里的引用都指着它。

        但**不能拿"原来那个 id"去覆盖别人**：SQLite 会复用 rowid，
        删掉最后一张卡片再新建一张就是同一个 id。以前这里用
        ``INSERT OR REPLACE``，于是 Ctrl+Z 会把用户刚写的新卡片
        静默顶掉（内容不可逆）。占用了就如实报错，让撤销失败而不是吃掉数据。
        """
        card_id = int(row["id"])
        now = utcnow_iso()
        with self.db.transaction():
            if self.db.query_one("SELECT 1 FROM board_cards WHERE id = ?", (card_id,)):
                raise LibraryError("这个位置已经被新卡片占用了，撤销会覆盖它")
            self.db.execute(
                "INSERT INTO board_cards(id, project_id, kind, x, y, w, h, "
                "title, text, path, url, origin, color, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, int(row["project_id"]), str(row.get("kind") or "note"),
                 float(row.get("x") or 0.0), float(row.get("y") or 0.0),
                 float(row.get("w") or 260.0), float(row.get("h") or 0.0),
                 str(row.get("title") or ""), str(row.get("text") or ""),
                 str(row.get("path") or ""), str(row.get("url") or ""),
                 str(row.get("origin") or "user"), str(row.get("color") or ""),
                 str(row.get("created_at") or now), now),
            )
        return card_id

    def restore_group(self, row: dict[str, Any],
                      member_ids: Iterable[int] = ()) -> int:
        """按**原来的 id** 把分组（含成员、矩形）放回去。

        与 :meth:`restore_board_card` 同理：id 被新分组占用了就报错，
        绝不 ``REPLACE`` 掉用户新建的那个组。
        """
        group_id = int(row["id"])
        now = utcnow_iso()
        with self.db.transaction():
            if self.db.query_one("SELECT 1 FROM board_groups WHERE id = ?", (group_id,)):
                raise LibraryError("这个位置已经被新分组占用了，撤销会覆盖它")
            self.db.execute(
                "INSERT INTO board_groups(id, project_id, name, note, color, "
                "sort_key, x, y, w, h, manual, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (group_id, int(row["project_id"]), str(row.get("name") or ""),
                 str(row.get("note") or ""), str(row.get("color") or ""),
                 int(row.get("sort_key") or 0), float(row.get("x") or 0.0),
                 float(row.get("y") or 0.0), float(row.get("w") or 0.0),
                 float(row.get("h") or 0.0), int(row.get("manual") or 0),
                 str(row.get("created_at") or now), now),
            )
        self.set_group_members(group_id, member_ids)
        return group_id


    def promote_card_to_event(self, card_id: int, *, title: str | None = None,
                              group_id: int | None = None,
                              file_mode: str | None = None,
                              note: str = "") -> int:
        """把一张卡片**转为事件**：建事件 → 归档内容（第一稿）→ 进分组 → 删卡片。

        顺序是刻意这么排的：**归档成功之后才删卡片**。任何一步失败都抛异常、
        卡片原样留着 —— 不会出现"文件没进库、卡片也没了"这种两头落空。
        归档方式按"单文档逻辑"走（默认就地引用）；**没有"临时文件"这种特例**，
        也不会删你的任何文件。

        但"先建事件、后校验文件"仍然会留下**半成品**：文件已经不在原处时，
        事件行已经落库（第一稿还没归档），用户重试又被
        「该节点下已存在同名文档」挡住，这张卡片就永远转不成事件了。
        所以**能提前判的都判在建事件之前**。
        """
        card = self.get_board_card(card_id)
        if card is None:
            raise LibraryError(f"卡片不存在：{card_id}")
        project_id = int(card["project_id"])
        roots = self.node_tree(project_id)
        if not roots:
            raise LibraryError("这个项目没有根节点，建不了事件")
        name = (title or card["title"] or "").strip()
        if not name:
            name = Path(card["path"]).stem if card["path"] else "新事件"
        kind = str(card["kind"])
        if kind == "note":
            # 说明框就是白板上的便利贴，**不转事件**（用户明确要求）
            raise LibraryError("说明框不转事件：它就是白板上的便利贴，不用进归档")
        if file_mode is None:
            # 一律按"单文档逻辑"：默认就地引用，和拖一个文件进来完全一样。
            # 没有"临时文件"这种特例 —— 文件夹已经被打包成一个正常的 zip 文件了。
            file_mode = self.resolve_file_mode(None)

        # 归档前的校验全部前置（见 docstring）：内容取不回来的卡片
        # 连事件行都不该留下
        path = ""
        if kind == "file":
            path = str(card["path"] or "")
            if not path:
                raise LibraryError("这张文件卡片没有路径")
            if not Path(path).is_file():
                raise LibraryError(f"文件已经不在原处：{path}")
        else:                       # link
            url = str(card["url"] or "")
            if not (url or str(card["text"] or "")):
                raise LibraryError("这张链接卡片既没有网址也没有说明，转不成事件")

        document = self.create_document(roots[0].id, name)
        self.set_document_position(document.id, float(card["x"]), float(card["y"]))

        if kind == "file":
            self.attach_files(document.id, [path], note=note, file_mode=file_mode)
        else:                       # link
            url = str(card["url"] or "")
            body = "\n".join(item for item in (card["text"], url) if item)
            self.create_version(document.id, text=body or url or name,
                                note=note or "由白板上的链接卡片转入")

        if group_id is not None:
            self.add_group_members(group_id, [document.id])

        self.delete_board_card(card_id)
        return document.id

    # ---- 临时压缩包（用户把文件夹拖进来时打包出来的"临时文件"）----

    def temp_dir(self) -> Path:
        """临时产物的落脚点：库目录下的 ``.tmp``（随库走，随时可清）。

        为什么不用系统临时目录：那个可能**中途**被系统清掉，白板上还摆着的卡片
        一觉醒来就失效了；放在库自己的 ``.tmp`` 里可控，而且它不是"永久存放压缩包"
        的目录 —— 卡片转成事件后这里就删掉了。
        """
        path = Path(self.root) / ".tmp"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def new_temp_path(self, name: str, suffix: str = "") -> Path:
        """给临时产物起一个不重名的路径（放在 ``.tmp`` 里）。"""
        stem = "".join(char for char in name if char not in '<>:"/\\|?*').strip() or "临时"
        stamp = utcnow_iso().replace(":", "").replace("-", "")[:15]
        return self.temp_dir() / f"{stem}-{stamp}{suffix}"

    def board_groups(self, project_id: int) -> list[dict[str, Any]]:
        """白板上的分组：组名 + 说明 + 成员事件（**组里有哪些节点就是这里的记录**）。"""
        groups: list[dict[str, Any]] = []
        for row in self.db.query(
            "SELECT id, project_id, name, note, color, sort_key, x, y, w, h, manual, "
            "created_at FROM board_groups WHERE project_id = ? ORDER BY sort_key, id",
            (project_id,),
        ):
            group = dict(row)
            members = self.db.query(
                "SELECT m.document_id, d.title FROM board_group_members m "
                "JOIN documents d ON d.id = m.document_id "
                "WHERE m.group_id = ? ORDER BY d.title",
                (int(row["id"]),),
            )
            group["members"] = [int(item["document_id"]) for item in members]
            group["member_titles"] = [str(item["title"]) for item in members]
            groups.append(group)
        return groups

    def create_group(self, project_id: int, name: str,
                     document_ids: Iterable[int] = (), note: str = "",
                     color: str = "", *, x: float = 0.0, y: float = 0.0,
                     w: float = 0.0, h: float = 0.0,
                     manual: bool = False) -> int:
        """新建一个组，并把给定的事件放进去。

        ``document_ids`` 可以是空的（**空组**）：空组靠自己的矩形画出来，
        之后往里新建/拖入事件即可；``manual=True`` 表示大小由用户定、不跟着成员收紧。
        """
        now = utcnow_iso()
        with self.db.transaction():
            cursor = self.db.execute(
                "INSERT INTO board_groups(project_id, name, note, color, sort_key, "
                "x, y, w, h, manual, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, name, note, color, 0, float(x), float(y), float(w),
                 float(h), 1 if manual else 0, now, now),
            )
            group_id = int(cursor.lastrowid)
        if document_ids:
            self.set_group_members(group_id, document_ids)
        return group_id

    def set_group_rect(self, group_id: int, x: float, y: float, w: float, h: float,
                       manual: bool = True) -> None:
        """定下这个组框的位置与大小（``manual=True`` = 别跟着成员收紧）。

        窗口解散）时，用户拖了半天框子，界面不报错、库里也没变，
        看着就像"软件坏了"。现在如实抛错。
        """
        with self.db.transaction():
            cursor = self.db.execute(
                "UPDATE board_groups SET x = ?, y = ?, w = ?, h = ?, manual = ?, "
                "updated_at = ? WHERE id = ?",
                (float(x), float(y), float(w), float(h), 1 if manual else 0,
                 utcnow_iso(), group_id),
            )
            if cursor.rowcount == 0:
                raise LibraryError(f"这个分组已经不存在了：{group_id}")

    def set_group_auto(self, group_id: int) -> None:
        """这个组回到"框跟着成员收紧"。"""
        with self.db.transaction():
            self.db.execute(
                "UPDATE board_groups SET manual = 0, updated_at = ? WHERE id = ?",
                (utcnow_iso(), group_id),
            )

    def rename_group(self, group_id: int, name: str | None = None,
                     note: str | None = None, color: str | None = None) -> None:
        fields: list[str] = []
        params: list[Any] = []
        for column, value in (("name", name), ("note", note), ("color", color)):
            if value is not None:
                fields.append(f"{column} = ?")
                params.append(value)
        if not fields:
            return
        fields.append("updated_at = ?")
        params.append(utcnow_iso())
        params.append(group_id)
        with self.db.transaction():
            self.db.execute(
                f"UPDATE board_groups SET {', '.join(fields)} WHERE id = ?", params
            )

    def delete_group(self, group_id: int) -> None:
        """解散一个组（**只去掉这个框和成员记录，事件与文件一个都不动**）。"""
        with self.db.transaction():
            self.db.execute("DELETE FROM board_group_members WHERE group_id = ?",
                            (group_id,))
            self.db.execute("DELETE FROM board_groups WHERE id = ?", (group_id,))

    def set_group_members(self, group_id: int,
                          document_ids: Iterable[int]) -> None:
        """把组里的成员事件记成给定的这一批（传进来的就是全部成员）。"""
        wanted = {int(item) for item in document_ids}
        now = utcnow_iso()
        with self.db.transaction():
            self.db.execute("DELETE FROM board_group_members WHERE group_id = ?",
                            (group_id,))
            for document_id in sorted(wanted):
                self.db.execute(
                    "INSERT OR IGNORE INTO board_group_members(group_id, document_id, "
                    "added_at) VALUES(?,?,?)",
                    (group_id, document_id, now),
                )

    def add_group_members(self, group_id: int, document_ids: Iterable[int]) -> None:
        now = utcnow_iso()
        with self.db.transaction():
            for document_id in document_ids:
                self.db.execute(
                    "INSERT OR IGNORE INTO board_group_members(group_id, document_id, "
                    "added_at) VALUES(?,?,?)",
                    (group_id, int(document_id), now),
                )

    def remove_group_members(self, group_id: int,
                             document_ids: Iterable[int]) -> None:
        with self.db.transaction():
            for document_id in document_ids:
                self.db.execute(
                    "DELETE FROM board_group_members WHERE group_id = ? AND "
                    "document_id = ?",
                    (group_id, int(document_id)),
                )

    def groups_of_document(self, document_id: int) -> list[dict[str, Any]]:
        """这个事件属于哪些组。"""
        return [
            dict(row)
            for row in self.db.query(
                "SELECT g.id, g.name FROM board_groups g "
                "JOIN board_group_members m ON m.group_id = g.id "
                "WHERE m.document_id = ? ORDER BY g.sort_key, g.id",
                (document_id,),
            )
        ]

    def set_link_curve(self, from_document_id: int, to_document_id: int,
                       curve: float, kind: str = "depends_on") -> None:
        """调整一条连线的弯曲程度（0 = 自动；正数 = 往上拱多少）。

        两种"拖了没反应"都要报出来：

        * 这条连线根本不在（已经剪断/被别的窗口删了）—— ``UPDATE`` 命中 0 行；
        * **连线在、但类型对不上**：界面上按 ``(from, to)`` 找到了这条线并调用了
          这里，而调用方以为它是 ``kind`` 那种。类型写进 WHERE 的话正好是 0 行，
          又是一次静默 no-op，所以先按 ``(from, to)`` 查出**实际类型**再判断。
        """
        with self.db.transaction():
            row = self.db.query_one(
                "SELECT id, kind FROM document_links WHERE from_document_id = ? "
                "AND to_document_id = ?",
                (from_document_id, to_document_id),
            )
            if row is None:
                raise LibraryError("这条连线已经不存在了，曲度没记上")
            actual = str(row["kind"])
            if actual != kind:
                raise LibraryError(
                    f"这条连线的类型是「{LINK_LABELS.get(actual, actual)}」，"
                    f"不是「{LINK_LABELS.get(kind, kind)}」，曲度没记上"
                )
            cursor = self.db.execute(
                "UPDATE document_links SET curve = ? WHERE id = ?",
                (float(curve), int(row["id"])),
            )
            if cursor.rowcount == 0:
                raise LibraryError("这条连线已经不存在了，曲度没记上")

    # ------------------------------------------------------- 框体模板

    def box_template(self) -> dict[str, tuple[int, int, int, int]]:
        """取**全库共用**的框体模板：``{方框标识: (列, 行, 宽格数, 高格数)}``。

        **所有项目、所有事件下的节点都按这个来排** —— 所以它只有一份，
        按库存，而不是每个事件各排一份。

        网格沿用旧版 ToOak/Symphytum ``FormLayoutMatrix`` 的思路：
        画布切成正方形格子，每个方框占 N×M 格。
        """
        rows = self.db.query(
            "SELECT box_key, col, row, units_w, units_h FROM box_template"
        )
        return {
            str(row["box_key"]): (
                int(row["col"]),
                int(row["row"]),
                int(row["units_w"]),
                int(row["units_h"]),
            )
            for row in rows
        }

    def set_box_template_entry(
        self, box_key: str, col: int, row: int, units_w: int, units_h: int
    ) -> None:
        """改模板里的一个方框（用户在界面上挪了 / 拉了某个方框）。"""
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO box_template(box_key, col, row, units_w, units_h, updated_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(box_key) DO UPDATE SET "
                "col = excluded.col, row = excluded.row, "
                "units_w = excluded.units_w, units_h = excluded.units_h, "
                "updated_at = excluded.updated_at",
                (str(box_key), int(col), int(row), int(units_w), int(units_h),
                 utcnow_iso()),
            )

    def clear_box_template(self) -> None:
        """忘掉手工排版，所有事件回到默认排法。"""
        with self.db.transaction():
            self.db.execute("DELETE FROM box_template")

    # ------------------------------------------------------- 文档间关联

    def link_documents(
        self, from_document_id: int, to_document_id: int, kind: str = "depends_on",
        label: str = "",
    ) -> None:
        """建立文档之间的语义链接（旧库的「依赖任务」就落在这里）。"""
        if from_document_id == to_document_id:
            return
        with self.db.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO document_links(from_document_id, to_document_id, "
                "kind, label, created_at) VALUES(?,?,?,?,?)",
                (from_document_id, to_document_id, kind, label, utcnow_iso()),
            )

    def unlink_documents(self, from_document_id: int, to_document_id: int, kind: str | None = None) -> None:
        with self.db.transaction():
            if kind is None:
                self.db.execute(
                    "DELETE FROM document_links WHERE from_document_id = ? AND "
                    "to_document_id = ?",
                    (from_document_id, to_document_id),
                )
            else:
                self.db.execute(
                    "DELETE FROM document_links WHERE from_document_id = ? AND "
                    "to_document_id = ? AND kind = ?",
                    (from_document_id, to_document_id, kind),
                )

    def document_links(self, document_id: int, kind: str | None = None) -> list[dict[str, Any]]:
        """取一份文档的关联关系，附带对端标题，便于界面直接显示。"""
        clauses = ["(l.from_document_id = ? OR l.to_document_id = ?)"]
        params: list[Any] = [document_id, document_id]
        if kind is not None:
            clauses.append("l.kind = ?")
            params.append(kind)
        rows = self.db.query(
            f"""
            SELECT l.*, f.title AS from_title, t.title AS to_title
              FROM document_links l
              JOIN documents f ON f.id = l.from_document_id
              JOIN documents t ON t.id = l.to_document_id
             WHERE {' AND '.join(clauses)}
             ORDER BY l.kind, t.title COLLATE NOCASE
            """,
            params,
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["direction"] = "out" if row["from_document_id"] == document_id else "in"
            item["other_id"] = (
                row["to_document_id"] if item["direction"] == "out" else row["from_document_id"]
            )
            item["other_title"] = (
                row["to_title"] if item["direction"] == "out" else row["from_title"]
            )
            item["kind_label"] = LINK_LABELS.get(row["kind"], row["kind"])
            result.append(item)
        return result

    def move_document(self, document_id: int, new_node_id: int) -> None:
        document = self.get_document(document_id)
        if document is None:
            raise LibraryError(f"文档不存在：{document_id}")
        node = self.get_node(new_node_id)
        if node is None:
            raise LibraryError(f"节点不存在：{new_node_id}")
        if document.node_id == new_node_id:
            return
        with self.db.transaction():
            clash = self.db.query_one(
                "SELECT id FROM documents WHERE node_id = ? AND title = ? COLLATE NOCASE "
                "AND id != ?",
                (new_node_id, document.title, document_id),
            )
            if clash:
                raise LibraryError(
                    f"目标节点下已有同名文档「{document.title}」，无法移动"
                )
            self.db.execute(
                "UPDATE documents SET node_id = ?, updated_at = ? WHERE id = ?",
                (new_node_id, utcnow_iso(), document_id),
            )
            self._log_event(
                "document_moved", document_id=document_id, node_id=new_node_id,
                message=f"{document.title} → {node.name}",
            )

    def delete_document(self, document_id: int, hard: bool = False) -> None:
        """默认软删除（归档，可恢复）；hard=True 时连同版本一并删除。"""
        if hard:
            with self.db.transaction():
                # 版本号的 high-water 要跟着走：documents 的 rowid 会被复用，
                # 事件删掉之后新建的事件就可能拿到同一个 id，不清掉的话
                # 它的第一稿会直接从被删事件的那个号往后排（一上来就是 v4）
                self.db.execute("DELETE FROM meta WHERE key = ?",
                                (self._seq_meta_key(document_id),))
                self.db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
            # 连各稿记录一起抹掉后，回收库里没人再引用的副本
            self.gc_blobs(dry_run=False)
        else:
            self.update_document(document_id, status="archived")

    # ---------------------------------------------------------------- 版本

    @staticmethod
    def _seq_meta_key(document_id: int) -> str:
        """每个事件的版本号 high-water 存在 meta 里的键。"""
        return f"seq:{int(document_id)}"

    def create_version(
        self,
        document_id: int,
        *,
        files: Sequence[FileSpec | tuple[Path | str, str]] | None = None,
        text: str | None = None,
        note: str = "",
        author: str = "",
        origin: str = "manual",
        parent_version_id: int | None = None,
        carry_files: bool = False,
        review_state: str = "draft",
        force: bool = False,
        source_path: str = "",
        created_at: str | None = None,
        extra: str = "",
        seq: int | None = None,
        file_mode: str | None = None,
        exclude_roles: Iterable[str] | None = None,
        allow_empty: bool = False,
    ) -> tuple[Version, bool]:
        """给文档追加一个版本。

        :param files: 新版的文件集合（主文件 + 附件）。与 ``text`` 二选一。
        :param text:  文本内容（做笔记/链接类文档时用，内部会写成 utf-8 文本 blob）。
        :param carry_files: 为 True 时，把父版本的文件集合并进来（用于「只加附件」）。
        :param force: 为 True 时即使内容与当前版本完全相同也强行建版本。
        :param created_at: 指定版本时间（导入历史数据时用，保持原时间线）。
        :param seq: 指定版本号（导入旧库的「版本」字段时用）。不指定时自动编号，
            且**只增不减**：删掉最新一稿后再归档不会复用已经被用过的号
            （复用会让时间线上出现两条 v4，用户分不清哪份是哪份）。
        :param file_mode: ``reference`` / ``link`` / ``copy``；None 时用库的默认设置
            （默认 ``reference``：文件留在原处，库只登记路径与内容指纹）。
        :param allow_empty: 允许建一稿**没有任何文件也没有文本**的稿。
            只给 ``create_empty_version``（界面「＋ 新增一稿」）用；默认 False，
        :returns: ``(version, deduplicated)``；内容重复且未 force 时返回已有版本且
                  ``deduplicated=True``，不会产生噪音版本。
        """
        document = self.get_document(document_id)
        if document is None:
            raise LibraryError(f"文档不存在：{document_id}")
        if review_state not in REVIEW_STATES:
            raise LibraryError(f"非法审阅状态：{review_state}")
        file_mode = self.resolve_file_mode(file_mode)

        specs = self._normalize_specs(files, text, document)
        current = self.get_version(document.current_version_id) if document.current_version_id else None

        # **按文件名管理**：如果新归档的文件与上一稿里的某个文件同名，
        # 说明它就是那份文件的新一稿 —— 沿用它在上一稿里的环节，
        # 而不是把它当成一份新文件丢进"主文件"。
        # 没有这条，重复归档同名文件会让环节历史断掉（环节里看不到这次改动）。
        if current is not None:
            by_name = {item.name: item for item in current.files}
            for spec in specs:
                existing = by_name.get(spec.display_name)
                if existing is not None and spec.role in ("primary", "attachment"):
                    spec.role = existing.role

        # 继承父版本的文件集（只加附件场景）
        dropped: list[str] = []
        if carry_files and current is not None:
            existing_names = {spec.display_name for spec in specs}
            skipped_roles = set(exclude_roles or ())
            for version_file in current.files:
                if version_file.name in existing_names:
                    continue
                if version_file.role in skipped_roles:
                    # 被替换的环节：旧文件不进这一稿（"换稿"而不是"堆稿"）
                    continue
                path = self._file_path(version_file)
                if path is None or not path.is_file():
                    # 上一稿引用的文件已经被搬走/改名/删掉（引用模式下很常见）。
                    # **不能因此让整个归档失败** —— 用户拖进来的明明是另一份好好的
                    # 新文件，却报"文件不存在：D:\旧目录\旧附件.docx"，还指向一份
                    # 这一稿照样带上这条记录（沿用旧指纹与旧路径），只是内容确实取不回来了，
                    # 如实记在 version.dropped 里让界面说清楚。
                    dropped.append(version_file.name)
                    specs.append(
                        FileSpec(
                            path=path or Path(version_file.name),
                            role=version_file.role,
                            name=version_file.name,
                            sha256=version_file.sha256 or None,
                            reuse_blob_id=version_file.blob_id,
                            reuse_size=version_file.size,
                            reuse_mime=version_file.mime,
                            reuse_location=version_file.location,
                            reuse_missing=True,
                        )
                    )
                    continue
                specs.append(
                    FileSpec(
                        path=path,
                        role=version_file.role,
                        name=version_file.name,
                        sha256=version_file.sha256 or None,
                        reuse_blob_id=version_file.blob_id,
                        reuse_size=version_file.size,
                        reuse_mime=version_file.mime,
                        reuse_location=version_file.location,
                    )
                )
        if not specs and not allow_empty:
            raise LibraryError("版本必须至少包含一个文件或一段文本")

        # 去重判断放在"有效集合"算完之后：无论是否继承上一稿，
        # 都拿最终会落库的文件集合与当前稿比较，内容没变就不产生噪音版本。
        if not force and current is not None:
            if sorted(self._spec_signature(spec) for spec in specs) == sorted(
                (item.name, item.role, item.sha256) for item in current.files
            ):
                return current, True

        parent_id = parent_version_id if parent_version_id is not None else document.current_version_id

        # **纳管文件必须放在 BEGIN 之前**：``_store_spec`` 要把整份文件读一遍算
        # sha256、还可能把它复制进库 —— 一个 2 GB 的文件就是几十秒。以前它在事务
        # 里面跑，于是这几十秒里**独占着 SQLite 的写锁**：归档在后台线程里跑时，
        # 主线程任何一个轻量写（改个界面设置、记一条事件）都得排队等它，
        # 超过连接的 timeout 就直接抛原始异常
        # ``sqlite3.OperationalError: database is locked`` —— 而界面只接
        # LibraryError / OSError，这个异常一路冒到用户面前（表现就是"点不动了、
        # 操作失败"）。实测主线程被阻塞 2.63 秒。
        #
        # 顺序也在这里定死：**所有校验与读盘都在写库之前完成**。以前"第 3 个文件
        # 不存在"是靠事务回滚兜住的；移出事务以后没有那层保护了，所以任何一个
        # 文件出问题（不存在、读不动、复制失败）都必须在**一行库记录都还没写**
        # 的时候抛出去，绝不留下"半个版本"。
        #
        # 代价：若第 3 个文件才失败，前两个已复制进库的文件会留在磁盘上、
        # 而且没有任何库记录指向它们（连接管的 blobs 行都没有）。库本身始终是
        # 一致的，这只是一点磁盘垃圾：要清它得扫 blobs 目录
        #（``gc_blobs`` 回收的是"有登记、但没有任何版本引用"的 blob，
        #  不会看见这种没登记的文件 —— 所以别指望它）。
        # 这个窗口在改之前就存在：事务回滚同样会删掉 blobs 行，
        # 却不会把已经复制到盘上的文件删掉。
        stored: list[tuple[FileSpec, BlobInfo, str, int]] = []
        for spec in specs:
            info, stored_name, _mtime = self._store_spec(spec, document, mode=file_mode)
            # mtime_ns 顺手在这里 stat 掉：它很便宜，但没必要塞进写事务里
            try:
                mtime_ns = spec.path.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            stored.append((spec, info, stored_name, mtime_ns))

        with self.db.transaction():
            if seq is None:
                # 版本号**只增不减**：删掉最新一稿后 SQLite 会把行的位置腾出来，
                # 只看 MAX(seq)+1 的话下一稿又拿到同一个 vN，时间线上出现两条 v4
                #（"删了重做"之后用户看到两个 v4，根本分不清哪份是哪份）。
                # meta 里的 high-water 记录这个事件发到过几号，删稿不会把它降下去。
                high_water = int(self.db.get_meta(self._seq_meta_key(document_id)) or 0)
                nxt = int(
                    self.db.scalar(
                        "SELECT IFNULL(MAX(seq), 0) + 1 FROM versions WHERE document_id = ?",
                        (document_id,),
                    )
                    or 1
                )
                seq = max(nxt, high_water + 1)
            else:
                clash = self.db.query_one(
                    "SELECT id FROM versions WHERE document_id = ? AND seq = ?",
                    (document_id, seq),
                )
                if clash:
                    raise LibraryError(f"该文档已存在 v{seq}")
            # 显式指定 seq 的路径（导入旧库、恢复旧版）也要把 high-water 顶上去，
            self.db.set_meta(
                self._seq_meta_key(document_id),
                str(max(int(seq), int(self.db.get_meta(self._seq_meta_key(document_id)) or 0))),
            )
            now = created_at or utcnow_iso()
            version_id = self.db.insert(
                "INSERT INTO versions(document_id, seq, parent_version_id, primary_file_id, "
                "source_name, source_path, sha256, size, mime, file_mtime, author, note, "
                "origin, review_state, created_at, extra) "
                "VALUES(?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
                (document_id, seq, parent_id, "", source_path, "", 0, "", None,
                 author, note, origin, review_state, now, extra),
            )

            primary_file_id: int | None = None
            # 事务里只剩"写库"：读盘/算指纹/复制都已在上面做完（见那段注释）
            for index, (spec, info, stored_name, mtime_ns) in enumerate(stored):
                blob_id = self._register_blob(info)
                # 引用模式下 location 记录「这个版本当时用的是哪个文件」
                location = info.rel_path if info.external else ""
                file_id = self.db.insert(
                    "INSERT INTO version_files(version_id, blob_id, name, role, seq, size, "
                    "sha256, mime, source_path, location, mtime_ns) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (version_id, blob_id, stored_name, spec.role, index, info.size,
                     info.sha256, info.mime,
                     spec.source_path
                     or (str(spec.path.parent) if spec.path.name else ""),
                     location, mtime_ns),
                )
                if primary_file_id is None and spec.role == "primary":
                    primary_file_id = file_id
            if primary_file_id is None:  # 全部是附件时，把第一个当作主文件
                first = self.db.query_one(
                    "SELECT id FROM version_files WHERE version_id = ? ORDER BY seq LIMIT 1",
                    (version_id,),
                )
                primary_file_id = first["id"] if first else None

            primary_row = self.db.query_one(
                "SELECT * FROM version_files WHERE id = ?", (primary_file_id,)
            )
            self.db.execute(
                "UPDATE versions SET primary_file_id = ?, source_name = ?, sha256 = ?, "
                "size = ?, mime = ? WHERE id = ?",
                (
                    primary_file_id,
                    primary_row["name"] if primary_row else "",
                    primary_row["sha256"] if primary_row else "",
                    primary_row["size"] if primary_row else 0,
                    primary_row["mime"] if primary_row else "",
                    version_id,
                ),
            )
            self.db.execute(
                "UPDATE documents SET current_version_id = ?, updated_at = ? WHERE id = ?",
                (version_id, now, document_id),
            )
            self._log_event(
                "version_created", document_id=document_id, version_id=version_id,
                node_id=document.node_id, message=f"v{seq} · {ORIGIN_LABELS.get(origin, origin)}",
            )

        version = self.get_version(version_id)
        assert version is not None
        version.dropped = list(dropped)
        return version, False

    def create_empty_version(
        self,
        document_id: int,
        *,
        note: str = "",
        author: str = "",
        origin: str = "manual",
        parent_version_id: int | None = None,
    ) -> Version:
        """建一稿**还没有登记任何文件**的版本（界面「＋ 新增一稿」点一下就走这里）。

        为什么要有这条通路：以前"新一稿"只能在用户拖文件时顺带建出来，
        "先建好这一稿，我再往里加文件"：点完 ＋ 就能先写这一稿的说明，
        再把文件拖进来；也不该因为拖了一个文件就凭空多出一稿。

        数据上这是允许的：``versions`` 行照写，``primary_file_id`` 留 NULL，
        ``version_files`` 一条都没有（旧库导入进来的稿也常是这个样子）。

        然后归档以前用其他软件归档的文件"）：这里传 ``force=True`` 跳过
        ``create_version`` 的内容去重 —— 否则连续点两下，第二下会因为"空稿和
        空稿内容相同"被去重成第一稿，用户根本建不出第二个空稿。
        """
        version, _deduplicated = self.create_version(
            document_id,
            files=None,
            text=None,
            note=note,
            author=author,
            origin=origin,
            parent_version_id=parent_version_id,
            allow_empty=True,
            force=True,
        )
        return version

    def add_files_to_version(
        self,
        version_id: int,
        files: Iterable[str | Path],
        *,
        role: str | None = None,
        replace_role: bool = False,
        as_primary: bool = False,
        file_mode: str | None = None,
    ) -> tuple[Version, list[str]]:
        """把文件加进**已经存在的这一稿**（不新建版本）。

        这是"新稿"的新语义：用户先点「＋ 新增一稿」建出空稿，
        之后再拖进来的文件都落进**这一稿**里 —— 不是每拖一次就多出一稿。

        :param role: 给定就全部进这个环节（拖到"输出文件"方框上就是这条路径）；
            不给时按 ``as_primary`` 决定"主文件/附件"。
        :param replace_role: 为 True 时**先去掉这一稿里该环节已有的文件行**
            （"我换了一稿输出"）；为 False 时是追加（"再放一份参考进去"）。
        :param file_mode: 纳管方式；None 时用库的设置（与归档一致）。
        :returns: ``(version, skipped)``：``skipped`` 是路径不存在、**没有加进去**
            的文件名 —— 必须如实返回，不能静默跳过（界面要照着它说清楚）。

        与 ``create_version`` 的几处一致之处及原因：
          * 同名文件**认领它原来的环节**（这一稿是空稿时就看更早的稿），
            否则"新建一稿后把同一份文件重新拖进来"会把它降级成"主文件/附件"；
          * 已经在写库之前把文件读盘/复制的活干完（几十秒的哈希不能占着写锁）；
          * 只改这一稿的行，**历史稿一个字都不动**（版本库只增不改）。
        """
        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        document = self.get_document(version.document_id)
        if document is None:
            raise LibraryError(f"事件不存在：{version.document_id}")
        file_mode = self.resolve_file_mode(file_mode)

        specs: list[FileSpec] = []
        skipped: list[str] = []
        for index, item in enumerate(files or []):
            src = Path(item).expanduser()
            if not src.is_file():
                skipped.append(src.name)
                continue
            if role:
                # 指定了环节（拖到某个方框上）：全部进这个环节
                item_role = role
            elif index == 0 and as_primary:
                item_role = "primary"
            else:
                item_role = "attachment"
            specs.append(FileSpec(path=src, role=item_role, name=src.name))

        if not specs:
            # 一个都不在磁盘上：库不动，让调用方拿 skipped 去如实说明
            return version, skipped

        # 按文件名管理：同名文件沿用它在原来那一稿里的环节（与 create_version 同一条规则）。
        # 这一稿是空稿时，它原来的环节只可能在更早的稿里，所以往前找一份。
        by_name = {item.name: item for item in version.files}
        for older in reversed(self.version_history(document.id)):
            if older.id == version.id:
                continue
            for item in older.files:
                by_name.setdefault(item.name, item)
        for spec in specs:
            existing = by_name.get(spec.display_name)
            if existing is not None and spec.role in ("primary", "attachment"):
                spec.role = existing.role

        # 这一稿里要顶掉的旧行（只在这一稿里顶掉，历史稿照旧）：
        #   * 同名文件：用户拖进来的就是这一份文件，留两行同名文件没人分得清点哪个；
        #   * replace_role：这个环节的旧行整批换掉 —— 这是"我换了这一稿的输出"。
        replaced_names = {spec.display_name for spec in specs}
        drop_ids = [
            item.id
            for item in version.files
            if item.name in replaced_names or (role and replace_role and item.role == role)
        ]

        # 纳管文件放在事务外（与 create_version 同一条纪律）：``_store_spec`` 可能要把
        # 整份文件读一遍算 sha256、甚至复制进库，几 GB 就是几十秒。放在事务里会让这
        # 几十秒独占 SQLite 的写锁，主线程任何一个轻量写都得排队等它。
        stored: list[tuple[FileSpec, BlobInfo, str, int]] = []
        for spec in specs:
            info, stored_name, _mtime = self._store_spec(spec, document, mode=file_mode)
            try:
                mtime_ns = spec.path.stat().st_mtime_ns
            except OSError:
                mtime_ns = 0
            stored.append((spec, info, stored_name, mtime_ns))

        with self.db.transaction():
            for file_id in drop_ids:
                self.db.execute("DELETE FROM version_files WHERE id = ?", (file_id,))
            next_seq = int(
                self.db.scalar(
                    "SELECT IFNULL(MAX(seq), -1) + 1 FROM version_files WHERE version_id = ?",
                    (version_id,),
                )
                or 0
            )
            for offset, (spec, info, stored_name, mtime_ns) in enumerate(stored):
                blob_id = self._register_blob(info)
                location = info.rel_path if info.external else ""
                self.db.insert(
                    "INSERT INTO version_files(version_id, blob_id, name, role, seq, size, "
                    "sha256, mime, source_path, location, mtime_ns) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (version_id, blob_id, stored_name, spec.role, next_seq + offset, info.size,
                     info.sha256, info.mime,
                     spec.source_path
                     or (str(spec.path.parent) if spec.path.name else ""),
                     location, mtime_ns),
                )

            # 主文件指针与这一稿的摘要跟着文件集合走（规矩集中在 _resync_version_primary）
            self._resync_version_primary(version_id)
            # documents.current_version_id / updated_at **不动**：这一稿本来就是当前稿
            # （界面只允许往最新一稿里加文件），没有"又出了一稿"这件事
            self._log_event(
                "files_added", document_id=document.id, version_id=version_id,
                node_id=document.node_id,
                message=f"v{version.seq} · 加进 {len(stored)} 个文件",
            )

        fresh = self.get_version(version_id)
        assert fresh is not None
        return fresh, skipped

    def move_file_to_role(self, version_id: int, file_id: int, role: str) -> None:
        """把**这一稿里的某一行文件**换到另一个环节（同一稿内改，不新建稿）。

        为什么要它：界面里"把文件卡片从一个环节方框拖到另一个方框"就是这个动作
        改变他们的类型"。没有它时只能先删再加（走归档），那会**凭空多出一稿**，

        三条纪律：

        * **只动这一稿**：改的就是这一稿 ``version_files`` 一行的 ``role``；
          别的稿、blob、磁盘上用户的原文件一概不碰
          （本库"绝不移动/删除用户的原文件"这条在这里同样成立）。
        * **同名不留两行**：目标环节里已经有同名文件行时，把那一行**替换掉**
          —— 同一份文件在一稿里出现两个入口，用户分不清该点哪个
          （与 ``add_files_to_version`` 里"同名文件顶掉旧行"是同一条规矩）。
          目标正好是「主文件」时同理，还要顶掉原来那一行主文件：
          一稿只能有一个主文件。
        * **``versions`` 的主文件字段跟着走**：被移动的正好是主文件、或者移动之后
          这一稿已经没有 ``role='primary'`` 的行了，指针与 source_name/sha256/
          size/mime 都要重算 —— 否则导出与差异会照着 ``versions`` 里的旧摘要
          去找一份已经不在"主文件"位置上的文件。

        :param version_id: 改哪一稿。
        :param file_id: 改这一稿里的哪一行（``version_files.id``）。
        :param role: 目标环节，必须是 ``ROLE_LABELS`` 里的角色。
        :raises LibraryError: 环节不认识 / 版本不存在 / 这一稿里没有这个文件。
        """
        role = str(role or "").strip()
        if role not in ROLE_LABELS:
            # 界面上的环节（输入/输出/参考/分析/图示/说明/其他…）就是 ROLE_LABELS
            # 那几个；别的字符串写进库以后没有任何界面认得它，那是静默的坏数据
            raise LibraryError(f"不认识的环节：{role!r}")
        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        document = self.get_document(version.document_id)
        if document is None:
            raise LibraryError(f"事件不存在：{version.document_id}")
        target = next((item for item in version.files if item.id == file_id), None)
        if target is None:
            raise LibraryError(f"v{version.seq} 里没有这个文件：{file_id}")
        if target.role == role:
            return                      # 拖回原环节：本来就对，什么都不用做

        with self.db.transaction():
            # 目标环节里的同名行先删掉（只删这一稿里的，历史稿一个字不动）。
            # 目标正好是「主文件」时还要顶掉原来的主文件行：一稿只能有一个主文件，
            # 留两行 role='primary' 会让 versions.primary_file_id 指向一个说不清的位置。
            self.db.execute(
                "DELETE FROM version_files WHERE version_id = ? AND id <> ? "
                "AND (name = ? OR (role = 'primary' AND ? = 'primary'))",
                (version_id, file_id, target.name, role),
            )
            self.db.execute(
                "UPDATE version_files SET role = ? WHERE id = ?", (role, file_id)
            )
            self._resync_version_primary(version_id)
            self._log_event(
                "file_role_changed", document_id=document.id, version_id=version_id,
                node_id=document.node_id,
                message=(
                    f"v{version.seq} · {target.name}："
                    f"{ROLE_LABELS.get(target.role, target.role)} → {ROLE_LABELS[role]}"
                ),
            )

    def register_file_to_version(self, source_version_id: int, file_id: int,
                                target_version_id: int, *,
                                role: str | None = None) -> dict[str, Any]:
        """把某一稿里的某一份文件**另外登记到另一稿**（界面上的「复制一份到这一稿」）。

        在文件库里面创建一个复制版本，作为最新的稿件（注意区分我可能选择的
        不一定是实际上的最新稿，我可能是把 V1 提到 V2，但 V3 又有文件了）"。

        典型场景：用户按「＋ 新增一稿」建了一稿空的，页面上每个环节都显示着
        上一稿沿用下来的文件行（标着"未登记"）——那些行**不属于这一稿**，
        而这个动作以前只有"重新拖一遍文件"（可能把文件又复制一份进库、还可能
        因为文件名变了对不上）或者"把它当成新的一稿"（凭空多一稿）。

        三条纪律：

        * **不新建稿、不动别的稿**：只往 ``target_version_id`` 这一稿里加一条
          ``version_files``；``documents.current_version_id`` 一个字都不改 ——
          所以"把 v1 的那份提到 v2、而 v3 才是当前稿"这件事干得成，
          而不是仅仅复制链接"）：源那一行如果是**库外引用**（reference 模式，
          记录里存的是用户自己磁盘上那个路径），这里会把内容**真的复制一份进库**
          （``files/<别名>/<原名>``），目标那一稿从此拥有自己的字节 ——
          源文件以后被挪走/就地覆盖都不再影响它。源那一行本来就在库里时，
          它已经是"库里的真文件"，直接引用同一份内容即可（库按 sha256 去重，
          同一份内容在库里只存一个实体）。
        * **旧稿那份文件必须还在、还是当初那一份**：引用模式下旧稿可能被就地覆盖或
          挪走 —— 那时候登记过去只会得到一条**取不回内容**的记录（看起来有、
          打开时说没有），所以这里直接拒绝并说清怎么修（"不许说谎"那条老规矩）。

        同名文件在目标稿里已经登记过时：**不重复登记**（返回 ``added=False``），
        如实告诉你它登记在哪个环节 —— 一稿里同一份文件留两个入口，用户分不清点哪个。
        （想换环节用拖放，那是 ``move_file_to_role`` 的事。）

        :returns: ``{added, name, role, role_label, source_seq, target_seq,
            existing_role_label, copied_into_library}``（``copied_into_library``
            为 True 表示这次真的把字节复制进库了）
        :raises LibraryError: 版本不存在 / 不是同一根事件枝 / 这一稿里没有这个文件 /
            内容已不可用 / 环节不认识。
        """
        role = str(role or "").strip() or None
        if role is not None and role not in ROLE_LABELS:
            raise LibraryError(f"不认识的环节：{role!r}")
        source = self.get_version(source_version_id)
        target_version = self.get_version(target_version_id)
        if source is None or target_version is None:
            raise LibraryError("要登记的版本不存在")
        if source.document_id != target_version.document_id:
            raise LibraryError("只能在同一根事件枝的不同稿之间登记")
        if source.id == target_version.id:
            raise LibraryError(f"这一份文件本来就登记在 v{source.seq} 里")
        document = self.get_document(source.document_id)
        if document is None:
            raise LibraryError(f"事件不存在：{source.document_id}")

        item = next((row for row in source.files if row.id == file_id), None)
        if item is None:
            raise LibraryError(f"v{source.seq} 里没有这个文件：{file_id}")
        target_role = role or item.role
        if target_role not in ROLE_LABELS:
            # 旧库导入的数据里可能有没见过的角色：当成附件，别写进一个界面不认识的字符串
            target_role = "attachment"

        # 内容还在不在、还是不是当初那一份（只 stat，不读内容）
        check = self.check_version_files(source_version_id)
        for bucket, why in (("missing", "已经不在原处"),
                            ("changed", "已经被就地覆盖，内容不是当初那一份了")):
            if any(entry["file_id"] == item.id for entry in check[bucket]):
                raise LibraryError(
                    f"「{item.name}」（v{source.seq} 那份）{why}，"
                    "登记过去也取不回内容；先用「重新绑定失效文件…」把它指回来，"
                    "或者把现在磁盘上那份重新拖进方框（那会正常归档）"
                )

        existing = next((row for row in target_version.files if row.name == item.name), None)
        if existing is not None:
            return {
                "added": False,
                "name": item.name,
                "role": existing.role,
                "role_label": ROLE_LABELS.get(existing.role, existing.role),
                "source_seq": source.seq,
                "target_seq": target_version.seq,
                "existing_role_label": ROLE_LABELS.get(existing.role, existing.role),
                "copied_into_library": False,
            }

        raw = self.db.query_one("SELECT * FROM version_files WHERE id = ?", (item.id,))
        # **真复制**：把这份内容复制成**库里新的一份实体文件**（files/<新别名>/<原名>），
        # 并且**总是新插一行 blob** —— 不复用同内容的那行。复用就等于两稿共用一个文件，
        # 用户一眼就看出来了（"你确定生成的是新文件吗？怎么都是 D:\…\files\5a7893d8…"）。
        # 用户的诉求很明确："我要的是复制一份真实的文件，而不是仅仅复制链接"。
        # 纳管放在事务外（与 create_version 同一条纪律：复制 + 算指纹可能是几十秒，
        # 不能占着 SQLite 的写锁；界面那边也是丢给后台任务跑的）。
        source_path = self._file_path(item)
        info, stored_name, _mtime = self._store_spec(
            FileSpec(path=source_path, role=target_role, name=raw["name"]),
            document, mode="copy",
        )
        blob_id = self._insert_blob(info)

        with self.db.transaction():
            # 目标环节是「主文件」时先顶掉它原来的主文件行：一稿只能有一个主文件
            # （与 move_file_to_role 同一条规矩，否则 versions.primary_file_id
            # 会指向一个说不清的位置）
            if target_role == "primary":
                self.db.execute(
                    "DELETE FROM version_files WHERE version_id = ? AND role = 'primary'",
                    (target_version.id,),
                )
            next_seq = int(
                self.db.scalar(
                    "SELECT IFNULL(MAX(seq), -1) + 1 FROM version_files WHERE version_id = ?",
                    (target_version.id,),
                )
                or 0
            )
            self.db.insert(
                "INSERT INTO version_files(version_id, blob_id, name, role, seq, size, "
                "sha256, mime, source_path, location, mtime_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    target_version.id, blob_id, stored_name, target_role, next_seq,
                    info.size, info.sha256, info.mime,
                    raw["source_path"] or str(source_path.parent),
                    "",                       # 库内文件：不再是外部路径
                    raw["mtime_ns"] if "mtime_ns" in raw.keys() else 0,
                ),
            )
            self._resync_version_primary(target_version.id)
            # documents.current_version_id / updated_at **不动**：落在哪一稿是用户
            # 明确选的（可能不是最新稿），这里不是"又出了一稿"。
            # 源那一稿也**一个字都不动**：它还是原来那条记录（引用模式下照旧指向
            # 用户自己那个文件）。
            self._log_event(
                "file_registered", document_id=document.id,
                version_id=target_version.id, node_id=document.node_id,
                message=(
                    f"v{target_version.seq} · {item.name}："
                    f"复制登记自 v{source.seq}（{ROLE_LABELS.get(target_role, target_role)}），"
                    f"库内新建一份（{info.rel_path}）"
                ),
            )
        return {
            "added": True,
            "name": item.name,
            "role": target_role,
            "role_label": ROLE_LABELS.get(target_role, target_role),
            "source_seq": source.seq,
            "target_seq": target_version.seq,
            "existing_role_label": "",
            "copied_into_library": True,
            "size": info.size,
            "stored_path": info.rel_path,
        }

    def _resync_version_primary(self, version_id: int) -> None:
        """把 ``versions`` 上的主文件指针与摘要重算成"跟着这一稿的文件集合走"。

        什么时候要它：直接改过这一稿的 ``version_files`` 之后（改环节 / 加文件 /
        登记一份过来）—— 主文件行可能被顶掉、可能一个 ``role='primary'`` 的行都不剩。

        规矩照 ``create_version``：优先拿 ``role='primary'`` 的第一行；
        没有就**拿第一行顶着**（否则界面与导出让一个"空主文件"发愣）；
        一行都没有就留空（空稿本来就是这个样子）。
        """
        primary_row = self.db.query_one(
            "SELECT * FROM version_files WHERE version_id = ? AND role = 'primary' "
            "ORDER BY seq LIMIT 1",
            (version_id,),
        )
        if primary_row is None:
            primary_row = self.db.query_one(
                "SELECT * FROM version_files WHERE version_id = ? ORDER BY seq LIMIT 1",
                (version_id,),
            )
        self.db.execute(
            "UPDATE versions SET primary_file_id = ?, source_name = ?, sha256 = ?, "
            "size = ?, mime = ? WHERE id = ?",
            (
                primary_row["id"] if primary_row else None,
                primary_row["name"] if primary_row else "",
                primary_row["sha256"] if primary_row else "",
                primary_row["size"] if primary_row else 0,
                primary_row["mime"] if primary_row else "",
                version_id,
            ),
        )

    def archive_file(
        self,
        node_id: int,
        path: str | Path,
        *,
        title: str | None = None,
        note: str = "",
        author: str = "",
        origin: str = "manual",
        force: bool = False,
        file_mode: str | None = None,
    ) -> ArchiveResult:
        """把一个文件归档到节点下。

        语义（这是整个软件最常用的一条路径）：
          * 节点下**没有**同名文档 → 新建文档 + v1；
          * 节点下**已有**同名文档 → 作为该文档的新版本（内容相同则跳过）。
        标题默认取文件名主名（``报告V2.docx`` → ``报告V2``），可用 ``title`` 覆盖。
        """
        src = Path(path).expanduser()
        if not src.is_file():
            raise LibraryError(f"文件不存在：{src}")
        if src.stat().st_size == 0 and src.suffix.lower() not in (".txt", ".md"):
            # 空文件仍允许归档（有些工具会先建空文件占位），但不做特殊处理
            pass

        doc_title = (title or src.stem or src.name).strip()
        document = self.find_document_by_title(node_id, doc_title)
        created_document = False
        if document is None:
            document = self.create_document(node_id, doc_title, kind="file")
            created_document = True

        version, deduplicated = self.create_version(
            document.id,
            files=[FileSpec(path=src, role="primary", name=src.name)],
            note=note,
            author=author,
            origin=origin,
            force=force,
            source_path=str(src.parent),
            file_mode=file_mode,
            # 继承其余文件：新一稿应当只改这一个文件，不该把别的环节从这一稿里抹掉。
            # 同名文件会被"按文件名管理"那条规则认领，回到它原来的环节。
            carry_files=True,
        )
        return ArchiveResult(document, version, created_document, deduplicated)

    def archive_paths(
        self,
        node_id: int,
        paths: Iterable[str | Path],
        *,
        note: str = "",
        author: str = "",
        origin: str = "import",
    ) -> list[ArchiveResult]:
        """批量归档：每个文件各自成为一份文档（或各自的新版本）。"""
        results: list[ArchiveResult] = []
        for item in paths:
            try:
                results.append(
                    self.archive_file(
                        node_id, item, note=note, author=author, origin=origin
                    )
                )
            except LibraryError:
                continue
        return results

    def attach_files(
        self,
        document_id: int,
        paths: Iterable[str | Path],
        *,
        note: str = "",
        author: str = "",
        origin: str = "manual",
        as_primary: bool = False,
        file_mode: str | None = None,
        role: str | None = None,
        replace_role: bool = False,
    ) -> tuple[Version, bool]:
        """给事件加文件并生成新版本。

        两种语义（都对应用户的实际动作）：

        * ``role`` + ``replace_role=True``（**拖到某个环节上**）：
          这些文件成为该环节这一稿的内容，**该环节的旧文件不再继承** ——
          这就是"我换了一稿输出"；
        * ``role`` + ``replace_role=False``（面板的"往此环节加文件"）：
          这些文件**追加**进该环节，旧文件保留 —— 这是"我还想再放一份参考进去"；
        * 不给 ``role``：作为附件并入，旧文件全部继承。

        ``as_primary=True`` 且给了多个文件时，**第一个**作为主文件，其余作为附件 ——
        这样一个版本只会有一个「主文件」，语义不会含糊。

        路径不存在的文件**不能只是 continue 掉**：以前界面上照样报
        "新增版本成功 N 个"（N 按用户拖进来的个数算），用户以为都进库了。
        现在把没带上的文件名如实收进 ``Version.dropped``，由调用方去说清楚。
        """
        specs: list[FileSpec] = []
        skipped: list[str] = []
        for index, item in enumerate(paths):
            src = Path(item).expanduser()
            if not src.is_file():
                skipped.append(src.name)
                continue
            if role:
                # 指定了环节（拖到"输出文件"上）：全部进这个环节
                item_role = role
            elif index == 0 and as_primary:
                item_role = "primary"
            else:
                item_role = "attachment"
            specs.append(FileSpec(path=src, role=item_role, name=src.name))
        if not specs and not as_primary:
            raise LibraryError("没有可归档的文件")
        version, deduplicated = self.create_version(
            document_id,
            files=specs,
            note=note,
            author=author,
            origin=origin,
            carry_files=not as_primary,
            file_mode=file_mode,
            exclude_roles={role} if (role and replace_role) else None,
        )
        if skipped:
            # 与 create_version 自己算出来的"沿用文件取不回内容"合并，
            # 界面看这一个列表就知道这一稿到底少了什么
            version.dropped = [*version.dropped, *skipped]
        return version, deduplicated

    def delete_files_from_version(self, version_id: int, file_ids: Iterable[int]) -> Version:
        """把若干文件**直接从这一稿删掉**（不新建稿）。

        与 ``remove_files_from_version`` 的区别：那个遵守"版本库只增不改"的老纪律，
        表现为"新增一稿、里面没有这几个文件"；这个是**直接改这一稿的
        ``version_files``**（用户明确要的："删除文件不要自动新增一稿，新增与否
        是我自己控制说了算"）。删除就是删除，要不要新稿由他另外按「＋ 新增一稿」。

        三条纪律（与 ``move_file_to_role`` / ``add_files_to_version`` 一致）：

        * **只动这一稿**：删的只是这一稿的文件行；别的稿、blob、磁盘上用户的
          原文件一概不碰（"绝不移动/删除用户原文件"照旧成立）；
        * **主文件指针跟着走**：被删的正好是主文件时，``versions`` 的
          primary_file_id / source_name / sha256 / size / mime 重算 ——
          否则导出与差异会照着旧摘要去找一份已经不在这稿里的文件；
        * 删完可以是一稿空稿（``version_files`` 一条都没有），与"＋ 新增一稿"
          直接建出来的空稿一个样。

        :param version_id: 改哪一稿。
        :param file_ids: 删这一稿里的哪些行（``version_files.id``）。
        :returns: 删完之后重新读出的这一稿。
        :raises LibraryError: 版本不存在 / 这一稿里没有这些文件。
        """
        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        document = self.get_document(version.document_id)
        if document is None:
            raise LibraryError(f"事件不存在：{version.document_id}")
        targets = set(int(item) for item in file_ids)
        dropped = [item for item in version.files if item.id in targets]
        if not dropped:
            raise LibraryError("这一稿里没有要删除的文件")

        with self.db.transaction():
            for file_id in targets:
                self.db.execute("DELETE FROM version_files WHERE id = ?", (file_id,))
            # 主文件指针跟着走（照 create_version / move_file_to_role 的老规矩：
            # 先找 role='primary'，没有就取第一行顶着，一稿都没有就留空）
            primary_row = self.db.query_one(
                "SELECT * FROM version_files WHERE version_id = ? AND role = 'primary' "
                "ORDER BY seq LIMIT 1",
                (version_id,),
            )
            if primary_row is None:
                primary_row = self.db.query_one(
                    "SELECT * FROM version_files WHERE version_id = ? ORDER BY seq LIMIT 1",
                    (version_id,),
                )
            self.db.execute(
                "UPDATE versions SET primary_file_id = ?, source_name = ?, sha256 = ?, "
                "size = ?, mime = ? WHERE id = ?",
                (
                    primary_row["id"] if primary_row else None,
                    primary_row["name"] if primary_row else "",
                    primary_row["sha256"] if primary_row else "",
                    primary_row["size"] if primary_row else 0,
                    primary_row["mime"] if primary_row else "",
                    version_id,
                ),
            )
            self._log_event(
                "files_deleted", document_id=document.id, version_id=version_id,
                node_id=document.node_id,
                message=f"v{version.seq} 删除了 {len(dropped)} 个文件："
                        + "、".join(item.name for item in dropped[:3])
                        + ("…" if len(dropped) > 3 else ""),
            )
        # 只回收库自己的 files/，用户原文件 / 引用模式的库外文件一概不碰
        self.gc_blobs(dry_run=False)
        return self.get_version(version_id)

    def remove_files_from_version(
        self, document_id: int, file_ids: Iterable[int], note: str = ""
    ) -> tuple[Version, bool]:
        """把若干文件从**新的一稿**里去掉（不是改写历史）。

        与"恢复旧版"同一条纪律：版本库只增不改。
        删除的动作表现为"新增一稿，里面没有这几个文件"，
        因此任何一稿都还能被回看。

        注意：引用模式下这**不会删除用户的原文件**，只是新一稿不再引用它。
        """
        document = self.get_document(document_id)
        if document is None:
            raise LibraryError(f"事件不存在：{document_id}")
        current = self.latest_version(document_id)
        if current is None:
            raise LibraryError("这个事件还没有任何版本")

        targets = set(int(item) for item in file_ids)
        remaining = [item for item in current.files if item.id not in targets]
        dropped = [item for item in current.files if item.id in targets]
        if not dropped:
            raise LibraryError("没有找到要删除的文件")
        if len(remaining) == len(current.files):
            raise LibraryError("删除后文件没有变化")

        specs = [
            FileSpec(
                path=self._file_path(item),
                role=item.role,
                name=item.name,
                sha256=item.sha256,
            )
            for item in remaining
            if self._file_path(item) is not None
        ]
        message = note or (
            f"删除了 {len(dropped)} 个文件："
            + "、".join(item.name for item in dropped[:3])
            + ("…" if len(dropped) > 3 else "")
        )
        return self.create_version(
            document_id, files=specs or None,
            text=None if specs else message,
            note=message, origin="manual", force=True,
        )

    def add_note_version(
        self, document_id: int, text: str, *, note: str = "", author: str = ""
    ) -> tuple[Version, bool]:
        """给（笔记类）文档追加一段文本内容。"""
        return self.create_version(
            document_id, text=text, note=note, author=author, origin="note"
        )

    def restore_version(
        self,
        document_id: int,
        version_id: int,
        *,
        note: str = "",
        author: str = "",
        force: bool = False,
    ) -> tuple[Version, bool]:
        """把某个历史版本的内容恢复为最新版本。

        实现上是**新建**一个版本（内容引用旧版本的 blob，不复制数据），
        并用 ``version_links(kind='restore')`` 指向被恢复的版本 —— 这样流程视图里
        能清楚看到「v7 是基于 v3 恢复的」，历史永远不改写（版本库的基本纪律）。
        """
        target = self.get_version(version_id)
        if target is None:
            raise LibraryError(f"版本不存在：{version_id}")
        if target.document_id != document_id:
            raise LibraryError("该版本不属于此文档")

        # 引用模式下要先确认「那个时期的文件」还在、还是当初那份。
        # 否则"恢复"会静默地恢复成别的内容——比报错危险得多。
        if not force:
            check = self.check_version_files(version_id)
            if check["missing"] or check["changed"]:
                problems = check["missing"] + check["changed"]
                detail = "；".join(
                    f"{item['name']}（{item.get('reason', '')}）" for item in problems[:3]
                )
                raise LibraryError(
                    f"v{target.seq} 的原始文件已不在原处或已被就地覆盖：{detail}。"
                    "内容无法从库里恢复；若只想恢复「引用了哪些文件」这个事实，"
                    "可用 force=True（界面会先提示）。"
                )

        specs = [
            FileSpec(
                path=self._file_path(item),
                role=item.role,
                name=item.name,
            )
            for item in target.files
        ]
        new_version, deduplicated = self.create_version(
            document_id,
            files=specs,
            note=note or f"恢复到 v{target.seq}",
            author=author,
            origin="restore",
            force=True,
        )
        if not deduplicated:
            with self.db.transaction():
                self.db.insert(
                    "INSERT INTO version_links(from_version_id, to_version_id, kind, label, "
                    "created_at) VALUES(?,?,?,?,?)",
                    (new_version.id, target.id, "restore", f"恢复到 v{target.seq}", utcnow_iso()),
                )
                self._log_event(
                    "version_restored", document_id=document_id, version_id=new_version.id,
                    message=f"v{new_version.seq} ← v{target.seq}",
                )
        return new_version, deduplicated

    def delete_version(self, version_id: int, *, keep_last: bool = True) -> None:
        """

        规矩说清楚，免得删出麻烦：

        * 一稿都不剩是不允许的（``keep_last``）—— 事件至少要留一稿；
        * 删掉的那一稿如果有子稿，子稿会**接到被删稿的父稿上**，链不会断；
        * 删的正好是"当前最新稿"时，当前稿回退到剩下的最后一稿；
        * **用户磁盘上的原文件不动**，库里只是不再引用它（块由 ``gc`` 回收）。
        """
        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        document = self.get_document(version.document_id)
        if document is None:
            raise LibraryError("版本所属事件不存在")

        rows = self.db.query(
            "SELECT COUNT(*) AS n FROM versions WHERE document_id = ?",
            (version.document_id,),
        )
        if keep_last and rows and rows[0]["n"] <= 1:
            raise LibraryError("这是这个事件唯一的一稿，删了事件就空了，所以没删")

        with self.db.transaction():
            # 子稿接到祖父上，链不断
            self.db.execute(
                "UPDATE versions SET parent_version_id = ? WHERE parent_version_id = ?",
                (version.parent_version_id, version_id),
            )
            self.db.execute("DELETE FROM version_text WHERE version_id = ?", (version_id,))
            self.db.execute("DELETE FROM versions WHERE id = ?", (version_id,))
            if document.current_version_id == version_id:
                latest = self.db.query_one(
                    "SELECT id FROM versions WHERE document_id = ? ORDER BY seq DESC LIMIT 1",
                    (version.document_id,),
                )
                self.db.execute(
                    "UPDATE documents SET current_version_id = ?, updated_at = ? WHERE id = ?",
                    (latest["id"] if latest else None, utcnow_iso(), version.document_id),
                )
            # 删掉的正好是**最大号**时，把 high-water 降回"剩下的最大号"——
            # 我希望是从4开始"）。删中间一稿（1,2,3,4 里删 2）则 high-water 仍是 4。
            remaining_max = int(
                self.db.scalar(
                    "SELECT IFNULL(MAX(seq), 0) FROM versions WHERE document_id = ?",
                    (version.document_id,),
                )
                or 0
            )
            self.db.set_meta(
                self._seq_meta_key(version.document_id), str(remaining_max)
            )
        self._log_event(
            "version_delete", document_id=version.document_id, version_id=version_id,
            message=f"删除 v{version.seq}",
        )
        # 删稿之后回收库里没人再引用的副本（"从页面上删除就是从库里删除"）
        self.gc_blobs(dry_run=False)

    def set_review_state(
        self,
        version_id: int,
        state: str,
        *,
        reviewer: str = "",
        comment: str = "",
    ) -> None:
        """设置版本的审阅状态，并留下一条审阅记录（Web 端「确认版本」的落点）。"""
        if state not in REVIEW_STATES:
            raise LibraryError(f"非法审阅状态：{state}")
        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        with self.db.transaction():
            self.db.execute(
                "UPDATE versions SET review_state = ? WHERE id = ?", (state, version_id)
            )
            self.db.insert(
                "INSERT INTO reviews(version_id, reviewer, decision, comment, created_at) "
                "VALUES(?,?,?,?,?)",
                (version_id, reviewer, state, comment, utcnow_iso()),
            )
            self._log_event(
                "version_reviewed",
                document_id=version.document_id,
                version_id=version_id,
                message=f"v{version.seq} → {REVIEW_STATE_LABELS.get(state, state)}"
                        + (f"（{reviewer}）" if reviewer else ""),
            )

    def list_reviews(self, version_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM reviews WHERE version_id = ? ORDER BY id", (version_id,)
        )
        return [dict(row) for row in rows]

    def get_version(self, version_id: int | None) -> Version | None:
        if not version_id:
            return None
        row = self.db.query_one("SELECT * FROM versions WHERE id = ?", (version_id,))
        if row is None:
            return None
        version = Version.from_row(row)
        version.files = self.version_files(version.id)
        return version

    def version_files(self, version_id: int) -> list[VersionFile]:
        rows = self.db.query(
            """
            SELECT vf.*, b.rel_path AS rel_path
              FROM version_files vf JOIN blobs b ON b.id = vf.blob_id
             WHERE vf.version_id = ?
             ORDER BY vf.seq
            """,
            (version_id,),
        )
        return [VersionFile.from_row(row) for row in rows]

    def version_history(self, document_id: int) -> list[Version]:
        """按时间（seq）升序返回全部版本，含文件集合。

        两条 SQL 查完（版本一条、这一文档所有稿的文件一条），不再"每稿
        ``get_version`` + ``version_files`` 各查一次"：N 稿就是 1+2N 条，
        事件页/版本图上每次刷新都跑一遍。返回的对象与字段跟以前完全一致。
        """
        versions = [
            Version.from_row(row)
            for row in self.db.query(
                "SELECT * FROM versions WHERE document_id = ? ORDER BY seq", (document_id,)
            )
        ]
        if not versions:
            return []
        by_id = {version.id: version for version in versions}
        placeholders = ",".join("?" for _ in versions)
        for row in self.db.query(
            f"""
            SELECT vf.*, b.rel_path AS rel_path
              FROM version_files vf JOIN blobs b ON b.id = vf.blob_id
             WHERE vf.version_id IN ({placeholders})
             ORDER BY vf.version_id, vf.seq
            """,
            list(by_id),
        ):
            version_file = VersionFile.from_row(row)
            owner = by_id.get(version_file.version_id)
            if owner is not None:
                owner.files.append(version_file)
        return versions

    def latest_version(self, document_id: int) -> Version | None:
        document = self.get_document(document_id)
        if document is None:
            return None
        return self.get_version(document.current_version_id)

    def version_links(self, document_id: int) -> list[dict[str, Any]]:
        """文档内的语义边（restore/branch/merge）。"""
        rows = self.db.query(
            """
            SELECT vl.* FROM version_links vl
              JOIN versions v ON v.id = vl.from_version_id
             WHERE v.document_id = ?
             ORDER BY vl.id
            """,
            (document_id,),
        )
        return [dict(row) for row in rows]

    def export_version(self, version_id: int, dest_dir: str | Path | None = None) -> list[Path]:
        """把某个版本的文件导出到磁盘，返回落地路径列表。

        默认导出到 ``<库根>/exports/<文档标题>/``，文件名使用**原始文件名**，
        重名时自动追加序号，绝不覆盖已有文件。
        """
        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        document = self.get_document(version.document_id)
        title = document.title if document else f"doc{version.document_id}"

        target_dir = (
            Path(dest_dir).expanduser()
            if dest_dir is not None
            else self.root / "exports" / safe_filename(title)
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        if len(version.files) > 1:
            target_dir = target_dir / f"v{version.seq}"
            target_dir.mkdir(parents=True, exist_ok=True)

        written: list[Path] = []
        for item in version.files:
            target = unique_path(target_dir / safe_filename(item.name))
            # **必须按"这一稿记的那个路径"解析**（引用模式是 location）：
            # 拿 blobs.rel_path 会解析到"最先登记这份内容的那条路径"，
            # 文件改名/搬走并重新绑定之后，导出要么失败、要么导出的是别人那份。
            source = self._file_path(item)
            if source is None or not source.is_file():
                raise LibraryError(f"这一稿的文件已不在原处，导不了：{item.name}")
            shutil_copy(source, target)
            written.append(target)
        return written

    def blob_path(self, version_id: int, file_id: int | None = None) -> Path | None:
        """取某版本主文件（或指定文件）在 blob 库中的真实路径，供预览/打开。

        **明确指定了 ``file_id`` 却在这一稿里找不到时，返回 ``None``** ——
        "双击上一版的文件，打开的是这一版的第一个文件"）。
        """
        version = self.get_version(version_id)
        if version is None:
            return None
        if file_id is None:
            target = version.primary_file
        else:
            target = next((f for f in version.files if f.id == file_id), None)
            if target is None:
                return None
        if target is None:
            return None
        return self._file_path(target)

    # ------------------------------------------------------------ 内容与比较

    def get_version_text(self, version_id: int, refresh: bool = False) -> str | None:
        """取版本的文本内容（结果缓存进 ``version_text``）。

        返回 ``None`` 表示这份文件抽不出文本（二进制、或缺少对应解析库）。
        缓存为空字符串也意味着「已尝试过且无文本」，以免反复解析大文件。

        但缓存**必须核对它是为哪一份文件抽的**（``file_id``）：引用模式下
        文件被挪走时抽不出文本，那次失败会被固化成空，之后文件回来了、
        重新绑定了，缓存里那句"没有文本"还在 —— 全文检索和比较从此永远
        拿不到这份内容。``file_id`` 和现在的主文件对不上就重新抽一次。
        """
        from . import diffing

        if not refresh:
            row = self.db.query_one(
                "SELECT text, file_id FROM version_text WHERE version_id = ?", (version_id,)
            )
            if row is not None:
                version = self.get_version(version_id)
                primary = version.primary_file if version is not None else None
                cached_file = row["file_id"]
                if (cached_file is not None and primary is not None
                        and int(cached_file) == primary.id):
                    return row["text"] or None

        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        primary = version.primary_file
        text: str | None = None
        if primary is not None:
            text = diffing.extract_text(
                self._file_path(primary), primary.name, primary.mime
            )
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO version_text(version_id, file_id, text, extracted_at) "
                "VALUES(?,?,?,?) ON CONFLICT(version_id) DO UPDATE SET "
                "file_id = excluded.file_id, text = excluded.text, "
                "extracted_at = excluded.extracted_at",
                (version_id, primary.id if primary else None, text or "", utcnow_iso()),
            )
        return text

    def _diff_guard(self, old_version_id: int, role: str | None = None):
        """比较前的守门：旧稿的文件若已不可比，就如实说明，不做假比较。

        为什么必须有这道门：文件留在原处（引用模式）时，旧稿记录的是
        "当时那个路径 + 当时的指纹"。如果那个文件后来被**就地覆盖**，
        两侧解析出来的是同一个路径、同样的字节 —— 直接比较会得出

        **只 stat，不读内容**（``deep=False``）：判断"旧稿那份文件还在不在、
        还是不是那一份"，存在性和大小就够了。这里原来跑的是逐字节 sha256，
        于是旧稿上有几个 GB（含 GB 级压缩包）的事件，每切一稿都要把那些字节全读一遍
        ——实测接近一分钟，而结论和只 stat 完全一样。
        真要"逐字节证明没变"，那是 ``check_version_files(deep=True)`` 的事，
        只由用户显式触发（界面上的「校验文件」、CLI），不放在自动路径上。
        """
        from . import diffing

        old_version = self.get_version(old_version_id)
        new_version = None
        if old_version is not None:
            new_version = self.get_version(
                self.db.scalar("SELECT current_version_id FROM documents WHERE id = ?",
                               (old_version.document_id,)) or 0
            )
        # **零成本的铁证**：两稿解析到同一个路径、但登记的指纹不一样
        # —— 说明那份文件被就地覆盖过，历史内容已经没了，直接如实说"不可比"。
        # 只比大小抓不到"同长度覆盖"（"AAAA" → "BBBB" 大小一样），
        # 而那时候做出来的 diff 会是一句"文本内容完全一致"，纯属假结论。
        if old_version is not None and new_version is not None:
            for old_file, new_file in zip(old_version.files, new_version.files):
                if (old_file.sha256 and new_file.sha256
                        and old_file.sha256 != new_file.sha256
                        and self._file_path(old_file) == self._file_path(new_file)):
                    return diffing.DiffResult(
                        kind="binary",
                        summary=f"上一稿的内容已不可比：{old_file.name}"
                                "（这份文件被就地覆盖过，历史内容已经没了）",
                        details=[
                            f"{old_file.name}：现在这个路径上放的是另一份内容",
                            "文件留在原处时，库里保存的只是「当时那份内容」的记录；",
                            "同一个路径被覆盖之后，就没法再拿它当「当时那份」来比了。",
                            "要留住历史内容，归档时请用「复制进库」。",
                        ],
                    )

        check = self.check_version_files(old_version_id)
        broken = check["missing"] + check["changed"]
        if role:
            allowed = {
                item.id for item in self.get_version(old_version_id).files
                if item.role == role
            }
            broken = [item for item in broken if item["file_id"] in allowed]
        if not broken:
            return None
        names = "、".join(item["name"] for item in broken[:3])
        return diffing.DiffResult(
            kind="binary",
            summary=f"上一稿的内容已不可比：{names}"
                    + ("（文件已被就地覆盖或移走）" if len(broken) <= 3 else " 等文件已不可比"),
            details=[
                f"{item['name']}：{item.get('reason', '不可读')}" for item in broken[:5]
            ]
            + [
                "文件留在原处时，库里保存的是「当时那个路径、当时的大小与指纹」；",
                "现在这个路径上的文件已经变样（大小对不上）或不见了，",
                "所以没法再把它当成「当时那份内容」来比。要留住历史内容，归档时请用「复制进库」。",
            ],
        )

    def diff_versions(self, old_version_id: int, new_version_id: int,
                      allow_large: bool = False):
        """比较两个版本（文本 diff 或二进制摘要），返回 ``DiffResult``。

            不自动进行版本对比，耗时过长，把界面卡死"）—— 只想看结果的界面会传
            ``False``，拿到一个"已跳过"的结果并给用户一个"在后台比较"的入口；
            用户点了之后再把 ``allow_large=True`` 传进来。
        """
        from . import diffing

        old = self.get_version(old_version_id)
        new = self.get_version(new_version_id)
        if old is None or new is None:
            raise LibraryError("要比较的版本不存在")

        # 新一稿还没有登记任何文件（用户先建了空稿、文件还没拖进来）：
        # 直接说清楚，别继续往下走 —— 那条路会给出"删除了 X"这种结论，
        # 而用户根本什么都没删（实测：建完空稿后时间轴上就写着"删除了…"）。
        if not new.files:
            if not old.files:
                return diffing.DiffResult(
                    kind="binary", summary="两稿都还没有文件", details=[],
                )
            return diffing.DiffResult(
                kind="binary",
                summary=f"这一稿还没有文件（上一稿的 {len(old.files)} 个文件都还没登记进来）",
                details=[
                    "把文件拖进对应的方框、或右键文件卡片「加入到这一稿…」，它就会出现。",
                    "在这之前，这些文件仍然能在历史稿和上一稿里看到。",
                ],
            )

        if not allow_large:
            reason = diffing.oversized_reason(
                (old.primary_file.size if old.primary_file else 0),
                (new.primary_file.size if new.primary_file else 0),
            )
            if reason:
                return diffing.DiffResult(kind="binary", summary=reason,
                                          details=[reason], skipped=True)

        if (guard := self._diff_guard(old_version_id)) is not None:
            return guard

        old_file = old.primary_file
        new_file = new.primary_file
        result = diffing.diff_files(
            self._file_path(old_file) if old_file else None,
            self._file_path(new_file) if new_file else None,
            old_name=old_file.name if old_file else "",
            new_name=new_file.name if new_file else "",
            old_size=old.size,
            new_size=new.size,
            old_mime=old.mime,
            new_mime=new.mime,
            old_sha256=old_file.sha256 if old_file else "",
            new_sha256=new_file.sha256 if new_file else "",
        )

        old_names = {item.name for item in old.files}
        new_names = {item.name for item in new.files}
        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)
        if added:
            result.details.append("新增附件：" + "、".join(added))
        if removed:
            result.details.append("移除附件：" + "、".join(removed))
        return result

    def diff_stage(self, old_version_id: int, new_version_id: int, role: str,
                   allow_large: bool = False):
        """比较两个版本里**同一个环节**的内容（环节级版本流程用）。

        环节里的文件可能有多个：能抽文本的做文本 diff，
        其余按"新增/移除/换成了另一份"给结论 —— 对应界面上的"这一稿的
        输出文件换了哪一份"。
        """
        from . import diffing

        old = self.get_version(old_version_id)
        new = self.get_version(new_version_id)
        if old is None or new is None:
            raise LibraryError("要比较的版本不存在")

        if not allow_large:
            old_files_all = [item for item in old.files if item.role == role]
            new_files_all = [item for item in new.files if item.role == role]
            reason = diffing.oversized_reason(
                max((item.size for item in old_files_all), default=0),
                max((item.size for item in new_files_all), default=0),
            )
            if reason:
                return diffing.DiffResult(kind="binary", summary=reason,
                                          details=[reason], skipped=True)

        if (guard := self._diff_guard(old_version_id, role=role)) is not None:
            return guard

        old_files = [item for item in old.files if item.role == role]
        new_files = [item for item in new.files if item.role == role]
        old_by_name = {item.name: item for item in old_files}
        new_by_name = {item.name: item for item in new_files}

        role_label = ROLE_LABELS.get(role, role)
        # 这一稿还没有登记这个环节的文件（空稿就是典型）：如实说，
        # 不要给"移除：X"—— 那是"用户把它删了"的意思，与事实不符
        if not new_files:
            if not old_files:
                return diffing.DiffResult(
                    kind="binary", summary=f"{role_label}：两稿都没有文件", details=[],
                )
            return diffing.DiffResult(
                kind="binary",
                summary=f"{role_label}：这一稿还没有文件（上一稿有 {len(old_files)} 个）",
                details=["这一稿还没把「" + role_label + "」的文件登记进来。"],
            )

        added = sorted(set(new_by_name) - set(old_by_name))
        removed = sorted(set(old_by_name) - set(new_by_name))
        common = sorted(set(old_by_name) & set(new_by_name))
        changed = [
            name for name in common
            if old_by_name[name].sha256 != new_by_name[name].sha256
        ]

        # 单文件对单文件：**即使文件名变了也做内容 diff**（最有用的情况）。
        # 作者的实际习惯就是每稿换个带日期的文件名，只给"换了哪份文件"的结论远远不够。
        if len(old_files) == 1 and len(new_files) == 1:
            old_file, new_file = old_files[0], new_files[0]
            # 直接比这两个文件本身：不要走"整事件版本比较"，
            # 那条路会把其它环节的附件增减也报出来，在环节视图里是噪音。
            result = diffing.diff_files(
                self._file_path(old_file),
                self._file_path(new_file),
                old_name=old_file.name,
                new_name=new_file.name,
                old_size=old_file.size,
                new_size=new_file.size,
                old_mime=old_file.mime,
                new_mime=new_file.mime,
                old_sha256=old_file.sha256,
                new_sha256=new_file.sha256,
            )
            if old_file.name != new_file.name:
                result.details.insert(
                    0, f"{role_label}换了文件：{old_file.name} → {new_file.name}"
                )
            return result

        role_label = ROLE_LABELS.get(role, role)
        details: list[str] = []
        if added:
            details.append(f"新增：{'、'.join(added)}")
        if removed:
            details.append(f"移除：{'、'.join(removed)}")
        if changed:
            details.append(f"内容更新：{'、'.join(changed)}")
        if not details:
            details.append("该环节没有变化")

        old_desc = "、".join(item.name for item in old_files) or "（无）"
        new_desc = "、".join(item.name for item in new_files) or "（无）"
        return diffing.DiffResult(
            kind="text" if (len(old_files) == len(new_files) == 1) else "binary",
            summary=f"{role_label}：{old_desc} → {new_desc}"[:200],
            details=details
            + [
                f"上一稿：{len(old_files)} 个文件"
                + (f"，{human_size(sum(item.size for item in old_files))}" if old_files else ""),
                f"本稿：{len(new_files)} 个文件"
                + (f"，{human_size(sum(item.size for item in new_files))}" if new_files else ""),
            ],
        )

    def diff_file(self, old_version_id: int, new_version_id: int, file_id: int,
                  allow_large: bool = False):
        """比较某一稿里的**一份文件**和上一稿对应环节里的文件（点文件时用）。

        与 :meth:`diff_versions`（整稿）/ :meth:`diff_stage`（整环节）不同，
        这里只盯用户点的那一份：到上一稿的同一环节里找它的对应文件
        （优先同名；该环节只有一份时就比那一份），做内容 diff。
        """
        from . import diffing

        old = self.get_version(old_version_id)
        new = self.get_version(new_version_id)
        if old is None or new is None:
            raise LibraryError("要比较的版本不存在")

        target = next((item for item in new.files if item.id == file_id), None)
        if target is None:
            return diffing.DiffResult(kind="binary", summary="这份文件不在这一稿里", details=[])

        role = target.role
        role_label = ROLE_LABELS.get(role, role)
        if not allow_large:
            reason = diffing.oversized_reason(target.size, target.size)
            if reason:
                return diffing.DiffResult(kind="binary", summary=reason,
                                          details=[reason], skipped=True)

        # 配对规则与「用 Word 比较」共用一份（``counterpart_file``）：
        # 同名优先；该环节只有一份时就认那一份。两处各写一遍必然会走偏。
        old_file = self.counterpart_file(old_version_id, target)

        if old_file is None:
            return diffing.DiffResult(
                kind="binary",
                summary=f"上一稿的「{role_label}」没有这份文件（本稿新增：{target.name}）",
                details=[],
            )

        result = diffing.diff_files(
            self._file_path(old_file),
            self._file_path(target),
            old_name=old_file.name,
            new_name=target.name,
            old_size=old_file.size,
            new_size=target.size,
            old_mime=old_file.mime,
            new_mime=target.mime,
            old_sha256=old_file.sha256,
            new_sha256=target.sha256,
        )
        if old_file.name != target.name:
            result.details.insert(0, f"{role_label}换了文件：{old_file.name} → {target.name}")
        return result

    # ------------------------------------------------ 用 Word 自己的比较

    def counterpart_file(self, old_version_id: int, target: VersionFile, *,
                         cross_role: bool = False,
                         fuzzy: bool = True) -> VersionFile | None:
        """在 ``old_version_id`` 这一稿里找 ``target`` 的"对应那一份"。

        规则（环节年轮、文件比较、「用 Word 比较」都用它，别的地方要配对也走这里）：

        1. **同一个环节**里同名的那一份；
        2. 该环节只有一份文件时就认那一份；
        3. **模糊匹配**（``fuzzy``）：把日期 / 版本号 / 「提交·终版·修改」这类噪音
           去掉之后最像的那一份（``util.name_similarity``；低于门槛宁可不配）；
        4. ``cross_role=True`` 时再放宽三步（只有「用 Word 比较」用它）：
           跨环节找**同名**的；这一稿**只有一个 Word 文件**时认它；跨环节再按模糊匹配挑。

        为什么要有 2、3：用户的习惯是每稿换带日期与版本的文件名
        （``提交_v2.docx`` → ``提交-20250701终版.docx``），严格同名配对会得出
        "比较功能，匹配名字，更智能一点，现在名字有更改就匹配不上，模糊匹配"。

        （那份是「主文件」），后来才把它归到「输出文件」方框里 —— 严格按环节配对
        会让这条线断在中间。放宽带两条纪律：**配对结果要把角色显示出来**
        （菜单里写"v2 · 提交_v2.docx（主文件）"），而且模糊匹配只认"够像"的：
        菜单上写着这一对，用户按下去就是拿这两份去比，配错了比配不上更糟。
        """
        old = self.get_version(old_version_id)
        if old is None:
            return None
        same_role = [item for item in old.files if item.role == target.role]
        for item in same_role:
            if item.name == target.name:
                return item
        if len(same_role) == 1:
            return same_role[0]
        if fuzzy and (match := self._fuzzy_counterpart(target.name, same_role)):
            return match
        if not cross_role:
            return None
        for item in old.files:
            if item.name == target.name:
                return item
        from . import wordcompare

        word_files = [item for item in old.files if wordcompare.is_word_file(item.name)]
        if len(word_files) == 1:
            return word_files[0]
        if fuzzy:
            return self._fuzzy_counterpart(target.name, list(old.files))
        return None

    def _fuzzy_counterpart(self, name: str,
                           candidates: list[VersionFile]) -> VersionFile | None:
        """按"去掉日期/版本号/提交终版这类噪音之后的相似度"挑一份；不够像就返回 None。

        门槛（``NAME_MATCH_FLOOR``）**故意保守**：宁可说"配不上"让用户自己挑一份，
        也不能拿一份不相干的文件冒充"上一稿的那一份" —— 这个结论会一路带进
        比较结果里给用户看。
        """
        best: VersionFile | None = None
        best_score = 0.0
        for item in candidates:
            score = name_similarity(name, item.name)
            if score > best_score:
                best, best_score = item, score
        return best if best_score >= NAME_MATCH_FLOOR else None

    def word_compare_candidates(self, version_id: int, file_id: int) -> dict[str, Any]:
        """右键文件菜单要的信息：这份文件能不能用 Word 比、能和**哪些更早的稿**比。

        返回的字典（界面直接照它拼菜单，不再自己算业务）：

        * ``is_word``：这份文件是不是 Word 类型（不是的话菜单里整块不出现）；
        * ``word_available`` / ``word_reason``：本机 Word 能不能用（不能用时置灰 + 写明原因）；
        * ``candidates``：更早的稿里能比的那些，**从近到远**，
          每条 ``{version_id, file_id, seq, name, label, when, is_previous}``；
        * ``reason``：一个候选都没有时的人话（写进置灰项的提示里）。

        **只查存在性，不读内容**：菜单是在右键那一刻弹出来的，这里做重活
        （例如对旧稿逐字节校验）会让右键卡住。真正的"不许说谎"守门在
        :meth:`word_compare_pair` 里，那是在后台线程里跑的。
        """
        from . import wordcompare

        target_version = self.get_version(version_id)
        if target_version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        document = self.get_document(target_version.document_id)
        target = next((item for item in target_version.files if item.id == file_id), None)

        info: dict[str, Any] = {
            "version_id": version_id,
            "file_id": file_id,
            "name": target.name if target is not None else "",
            "role": target.role if target is not None else "",
            "role_label": ROLE_LABELS.get(target.role, target.role) if target else "",
            "is_word": bool(target is not None and wordcompare.is_word_file(target.name)),
            "word_available": wordcompare.available(),
            "word_reason": wordcompare.unavailable_reason(),
            "candidates": [],
            "reason": "",
        }
        if target is None:
            info["reason"] = "这一稿里已经没有这份文件了"
            return info
        if not info["is_word"]:
            info["reason"] = (
                "这份文件不是 Word 文档（.docx / .doc / .rtf…）—— "
                "用底侧「各稿」窗口里的内置比较就好"
            )
            return info
        if not info["word_available"]:
            info["reason"] = info["word_reason"]
            return info

        target_path = self._file_path(target)
        if target_path is None or not target_path.is_file():
            info["reason"] = "这份文件已不在原处（可先「重新绑定失效文件…」）"
            return info

        candidates: list[dict[str, Any]] = []
        for version in self.version_history(target_version.document_id):
            if version.seq >= target_version.seq:
                continue                      # 只往**更早**的稿比（"这一稿改了什么"）
            counterpart = self.counterpart_file(version.id, target, cross_role=True)
            if counterpart is None or not wordcompare.is_word_file(counterpart.name):
                continue
            path = self._file_path(counterpart)
            if path is None or not path.is_file():
                continue                      # 那份文件已经不在原处：不进菜单（宁可不给点）
            same_role = counterpart.role == target.role
            role_label = ROLE_LABELS.get(counterpart.role, counterpart.role)
            candidates.append({
                "version_id": version.id,
                "file_id": counterpart.id,
                "seq": version.seq,
                "name": counterpart.name,
                "role": counterpart.role,
                # 跨环节配上的那一份**必须把角色写出来**（见 counterpart_file 的说明）
                "label": f"v{version.seq} · {counterpart.name}"
                         + ("" if same_role else f"（{role_label}）"),
                "role_label": role_label,
                "same_role": same_role,
                "when": to_local_str(version.created_at, "%Y-%m-%d"),
                "is_previous": False,
            })
        candidates.reverse()                   # 近的在前
        if candidates:
            candidates[0]["is_previous"] = True
        info["candidates"] = candidates
        if not candidates:
            info["reason"] = (
                f"「{info['role_label']}」在更早的稿里没有能拿来比的 Word 文件"
                if document is not None else "没有更早的稿可以比"
            )
        return info

    def word_compare_pair(self, version_id: int, file_id: int,
                          old_version_id: int) -> dict[str, Any]:
        """要真的比了：把"用户点的那一份"和"某一稿里对应的那一份"配成一对。

        比 :meth:`word_compare_candidates` 多做了两件必须做的事：

        1. **走"不许说谎"的守门**（``_diff_guard``）：引用模式下，如果旧稿那份
           文件已经被就地覆盖/挪走，两侧解出来的是同一条路径、同样的字节 ——
           Word 会给出"两篇完全一致"这种**假结论**。宁可说"没法比"。
        2. 解析出两份文件的**真实路径**（库内副本或库外原文件），交给 Word 打开。

        :raises LibraryError: 任何"比不了"的情况，消息是可以直接给用户看的整句。
        """
        from . import wordcompare

        target_version = self.get_version(version_id)
        old_version = self.get_version(old_version_id)
        if target_version is None or old_version is None:
            raise LibraryError("要比较的版本不存在")
        if old_version.id == target_version.id:
            raise LibraryError("不能拿同一稿和它自己比")
        if old_version.seq >= target_version.seq:
            raise LibraryError(
                f"只能和更早的稿比：v{old_version.seq} 不比 v{target_version.seq} 早"
            )

        target = next((item for item in target_version.files if item.id == file_id), None)
        if target is None:
            raise LibraryError("这一稿里已经没有这份文件了")

        old_file = self.counterpart_file(old_version_id, target, cross_role=True)
        role_label = ROLE_LABELS.get(target.role, target.role)
        if old_file is None:
            raise LibraryError(
                f"v{old_version.seq} 的「{role_label}」里没有能和「{target.name}」配成一对的文件"
            )
        if not wordcompare.is_word_file(target.name) or \
                not wordcompare.is_word_file(old_file.name):
            raise LibraryError(
                f"「用 Word 比较」只对 Word 文档有效（这一对是 "
                f"{old_file.name} / {target.name}）"
            )

        # 守门：旧稿那份文件"还是不是当初那一份"（只看存在性与大小，不读内容）。
        # role 传**旧稿那份文件**所在环节：跨环节配对时，拿新稿的环节去筛就没筛到它。
        if (guard := self._diff_guard(old_version_id, role=old_file.role)) is not None:
            detail = "；".join(guard.details[:2])
            raise LibraryError(f"{guard.summary}" + (f"（{detail}）" if detail else ""))

        original_path = self._file_path(old_file)
        revised_path = self._file_path(target)
        if original_path is None or not original_path.is_file():
            raise LibraryError(f"v{old_version.seq} 的「{old_file.name}」已不在原处，比不了")
        if revised_path is None or not revised_path.is_file():
            raise LibraryError(f"「{target.name}」已不在原处，比不了")

        document = self.get_document(target_version.document_id)
        return {
            "title": document.title if document else "",
            "role": target.role,
            "role_label": role_label,
            "old_role_label": ROLE_LABELS.get(old_file.role, old_file.role),
            "same_role": old_file.role == target.role,
            "original_path": original_path,
            "revised_path": revised_path,
            "original_label": f"v{old_version.seq} · {old_file.name}",
            "revised_label": f"v{target_version.seq} · {target.name}",
            "old_version_id": old_version.id,
            "old_file_id": old_file.id,
            "version_id": target_version.id,
            "file_id": target.id,
            "old_seq": old_version.seq,
            "new_seq": target_version.seq,
        }

    def describe_version(self, version_id: int) -> dict[str, Any]:
        """版本画像：文件类型、页/表/尺寸等摘要，供详情面板显示。"""
        from . import diffing

        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        primary = version.primary_file
        profile = None
        if primary is not None:
            profile = diffing.describe_file(
                self._file_path(primary), primary.name, primary.mime
            )
        return {
            "version": version,
            "profile": profile,
            "file_count": len(version.files),
            "attachments": [item.name for item in version.files if item.role != "primary"],
        }

    # ---------------------------------------------------------------- 标签

    def add_tag(self, project_id: int, name: str, color: str = "#8a8a8a") -> int:
        clean = name.strip()
        if not clean:
            raise LibraryError("标签名不能为空")
        with self.db.transaction():
            row = self.db.query_one(
                "SELECT id FROM tags WHERE project_id = ? AND name = ? COLLATE NOCASE",
                (project_id, clean),
            )
            if row:
                return int(row["id"])
            return self.db.insert(
                "INSERT INTO tags(project_id, name, color) VALUES(?,?,?)",
                (project_id, clean, color),
            )

    def list_tags(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM tags WHERE project_id = ? ORDER BY name COLLATE NOCASE",
            (project_id,),
        )
        return [dict(row) for row in rows]

    def tag_document(self, document_id: int, tag_id: int) -> None:
        with self.db.transaction():
            self.db.execute(
                "INSERT OR IGNORE INTO document_tags(document_id, tag_id) VALUES(?,?)",
                (document_id, tag_id),
            )

    def untag_document(self, document_id: int, tag_id: int) -> None:
        with self.db.transaction():
            self.db.execute(
                "DELETE FROM document_tags WHERE document_id = ? AND tag_id = ?",
                (document_id, tag_id),
            )

    def document_tags(self, document_id: int) -> list[dict[str, Any]]:
        rows = self.db.query(
            """
            SELECT t.* FROM tags t JOIN document_tags dt ON dt.tag_id = t.id
             WHERE dt.document_id = ? ORDER BY t.name COLLATE NOCASE
            """,
            (document_id,),
        )
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ 监视目录

    def add_watch_dir(
        self, project_id: int, node_id: int, path: str | Path, recursive: bool = True
    ) -> int:
        resolved = str(Path(path).expanduser().resolve())
        if not Path(resolved).is_dir():
            raise LibraryError(f"监视目录不存在：{resolved}")
        if self.get_node(node_id) is None:
            raise LibraryError(f"节点不存在：{node_id}")
        with self.db.transaction():
            existing = self.db.query_one("SELECT id FROM watch_dirs WHERE path = ?", (resolved,))
            if existing:
                self.db.execute(
                    "UPDATE watch_dirs SET project_id = ?, node_id = ?, recursive = ?, "
                    "enabled = 1 WHERE id = ?",
                    (project_id, node_id, 1 if recursive else 0, existing["id"]),
                )
                return int(existing["id"])
            return self.db.insert(
                "INSERT INTO watch_dirs(project_id, node_id, path, recursive, enabled, "
                "created_at) VALUES(?,?,?,?,1,?)",
                (project_id, node_id, resolved, 1 if recursive else 0, utcnow_iso()),
            )

    def list_watch_dirs(self, project_id: int | None = None) -> list[dict[str, Any]]:
        if project_id is None:
            rows = self.db.query("SELECT * FROM watch_dirs ORDER BY id")
        else:
            rows = self.db.query(
                "SELECT * FROM watch_dirs WHERE project_id = ? ORDER BY id", (project_id,)
            )
        return [dict(row) for row in rows]

    def remove_watch_dir(self, watch_id: int) -> None:
        with self.db.transaction():
            self.db.execute("DELETE FROM watch_dirs WHERE id = ?", (watch_id,))

    def watch_target_for(self, path: str | Path) -> dict[str, Any] | None:
        """给定一个被监视到的文件路径，找到它应归属的监视规则（取最长前缀匹配）。"""
        resolved = Path(path).expanduser().resolve()
        best: dict[str, Any] | None = None
        best_len = -1
        for rule in self.list_watch_dirs():
            base = Path(rule["path"])
            try:
                resolved.relative_to(base)
            except ValueError:
                continue
            if rule["recursive"] == 0 and resolved.parent != base:
                continue
            if len(str(base)) > best_len:
                best, best_len = rule, len(str(base))
        return best

    # ---------------------------------------------------------------- 存档方式

    @property
    def file_mode(self) -> str:
        """库的默认纳管方式。

        **默认 ``copy``（归档时把文件复制一份进库）** —— 用户定的：
        "归档时把文件进库一定是默认开启的，不然就失去了这个软件的意义"。
        库自带内容才谈得上"哪一稿是哪一份字节"，也才能独立备份/迁移/去重；
        引用模式（文件留原处）下原文件被就地覆盖，旧版本的内容就取不回来了。

        注意两件事：

        * ``copy`` 只是**复制**，**永远不会移动或删除用户的原文件**
          （见 ``tooak.core.store.put_file``：拷贝失败就报错，原文件不动）；
        * **已经显式设过** ``file_mode`` 的老库保持原样 —— 老库当初
          （历史上默认是 ``reference``）把文件引用在库外，现在悄悄改成 copy
          会让它的下一稿忽然多出一份拷贝、占掉成倍的磁盘，还会让"哪份是真的"
          变得含糊。所以这里只在 ``meta`` 里**没有**记录时才返回新默认值。

        要改某个库的设置：``Library.open(root).set_file_mode("reference")``
        （库里存成 ``meta.file_mode``，换机器也跟着走）；命令行看不显眼的口子
        见 ``tooak file-mode``。
        """
        # 这个键**只在这个库从没被显式设置过**时才是空的 ——
        # 老库里存着 "reference" 就照旧用 reference，不替用户改主意
        mode = self.db.get_meta("file_mode") or "copy"
        return mode if mode in FILE_MODES else "copy"

    # ------------------------------------------------------------ 界面设置

    def get_ui_option(self, key: str, default: str = "") -> str:
        """读一条界面设置（存库里，换机器/重启后仍然生效）。

        放库里而不是 QSettings：这些是**这个库的**显示偏好
        （比如事件页里方框多大），跟着库走更符合直觉。
        """
        return self.db.get_meta(f"ui:{key}") or default

    def set_ui_option(self, key: str, value: str) -> None:
        self.db.set_meta(f"ui:{key}", str(value))

    def set_file_mode(self, mode: str) -> None:
        """改这个库的默认纳管方式（存进 ``meta.file_mode``，跟着库走）。

        （见的 :attr:`file_mode` 说明），一个能被随时点掉的全局开关会把
        "同一份资料在不同时间归档到不同地方"这件事变成常态。
        要改（比如某个库想沿用老办法、文件留在原处不占空间）用这两个不显眼的口子：

        * 命令行：``tooak file-mode reference``（不带模式则只打印当前值）；
        * Python：``Library.open(库目录).set_file_mode("reference")``。

        只影响**之后**的归档；已经登记过的文件行不动（它们的引用/拷贝都已经落定）。
        """
        if mode not in FILE_MODES:
            raise LibraryError(f"未知的存档方式：{mode}（可选 {list(FILE_MODES)}）")
        self.db.set_meta("file_mode", mode)

    def resolve_file_mode(self, mode: str | None) -> str:
        """把可选参数解析成实际使用的存档方式。"""
        if mode is None:
            return self.file_mode
        if mode not in FILE_MODES:
            raise LibraryError(f"未知的存档方式：{mode}（可选 {list(FILE_MODES)}）")
        return mode

    def check_version_files(self, version_id: int, deep: bool = False) -> dict[str, list]:
        """检查某个版本的文件当前是否可用。

        引用模式（文件留在原处）下的核心自检：
        返回 ``{"ok": [...], "missing": [...], "changed": [...], "unverified": [...]}``。

        * ``missing``：原文件被移走/改名/删除 —— 内容彻底取不回来；
        * ``changed``：原文件被**就地覆盖** —— 这是引用模式的固有代价，
          库仍保留着"当时那份内容的指纹"，但字节已经变成了新的。
          需要真正的历史快照时，归档时应使用 ``copy`` 模式；
        * ``unverified``：文件很大、``deep=True`` 时**跳过了逐字节校验**（只比了大小）。
          以前这类条目混在 ``ok`` 里，用户点「校验文件」看到"全部正常"，
          其实那些 GB 级文件根本没验过 —— 结论要如实分开放，别混进"已验证"。
        """
        from .util import sha256_file

        result: dict[str, list] = {"ok": [], "missing": [], "changed": [], "unverified": []}
        for item in self.version_files(version_id):
            path = self._file_path(item)
            entry = {"file_id": item.id, "name": item.name, "path": str(path) if path else ""}
            if path is None or not path.is_file():
                entry["reason"] = "文件不存在"
                result["missing"].append(entry)
                continue
            try:
                size = path.stat().st_size
            except OSError:
                entry["reason"] = "无法读取"
                result["missing"].append(entry)
                continue
            if row_size := item.size:
                if size != row_size:
                    entry["reason"] = f"大小已变（{row_size} → {size}）"
                    result["changed"].append(entry)
                    continue
            if deep and item.sha256:
                from .diffing import DEEP_CHECK_MAX_BYTES

                if size > DEEP_CHECK_MAX_BYTES:
                    # 大文件不做逐字节校验：读一遍几十 GB 只为"证明没变"不划算，
                    # 大小与修改时间已经足够说明问题（差了多少会如实写进 details）
                    entry["reason"] = "文件很大，未做逐字节校验（只比了大小）"
                    result["unverified"].append(entry)
                    continue
                digest, _ = sha256_file(path)
                if digest != item.sha256:
                    entry["reason"] = "内容已变（指纹不一致）"
                    result["changed"].append(entry)
                    continue
            result["ok"].append(entry)
        return result

    def relocate_files(
        self, old_prefix: str | Path, new_prefix: str | Path, *, dry_run: bool = False
    ) -> dict[str, Any]:
        """数据目录整体搬家后，批量改写引用路径。

        引用模式记的是绝对路径，所以「把资料盘从 D:/ 挪到 E:/」这类操作
        必须能一次性重定位，否则整库引用全部失效。只改库内记录，
        **不碰文件系统**（文件该由用户自己搬）。
        """
        old_text = str(Path(old_prefix).expanduser())
        new_text = str(Path(new_prefix).expanduser())
        if not old_text or old_text == new_text:
            raise LibraryError("新旧前缀不能相同或为空")

        # **必须按路径分隔符划边界**：只写 "前缀%" 的话，
        # 把"工程"搬家会连"工程备份"一起改写 —— 全库引用瞬间指到不存在的地方。
        # Windows 上分隔符可能是反斜杠，两种都认。
        old_base = old_text.rstrip("/\\")
        # LIKE 用了 ``ESCAPE '\'``，所以**模式里的反斜杠要写成两个**：
        # 否则 Windows 路径里的每个 "\" 都被当成转义符
        # （``C:\Users\...`` 被解成 ``C:Users...``），一条记录都匹配不上 ——
        # ``relocate`` 会打印"已改写 N 个引用"却实际上什么都没改（在 Linux 上永远发现不了）。
        # ``%`` 与 ``_`` 也是 LIKE 的元字符，一并转义。
        escaped = (old_base.replace("\\", "\\\\")
                   .replace("%", "\\%").replace("_", "\\_"))
        patterns = [escaped + "\\\\%", escaped + "/%"]
        rows: list[Any] = []
        for pattern in patterns:
            rows += self.db.query(
                "SELECT id, location FROM version_files WHERE location LIKE ? ESCAPE '\\'",
                (pattern,),
            )
        rows = [row for row in rows if _under_prefix(row["location"], old_base)]
        affected_files = len(rows)
        blob_rows: list[Any] = []
        for pattern in patterns:
            blob_rows += self.db.query(
                "SELECT id, rel_path FROM blobs WHERE source = 'reference' "
                "AND rel_path LIKE ?", (pattern,),
            )
        blob_rows = [row for row in blob_rows if _under_prefix(row["rel_path"], old_base)]
        changed = [
            (row["id"], row["location"],
             new_text.rstrip("/\\") + row["location"][len(old_base):])
            for row in rows
        ]
        if dry_run:
            return {
                "dry_run": True,
                "version_files": affected_files,
                "blobs": len(blob_rows),
                "examples": [item[2] for item in changed[:5]],
            }

        with self.db.transaction():
            for file_id, _old, new in changed:
                self.db.execute(
                    "UPDATE version_files SET location = ? WHERE id = ?", (new, file_id)
                )
            for row in blob_rows:
                self.db.execute(
                    "UPDATE blobs SET rel_path = ?, origin_path = ? WHERE id = ?",
                    (
                        new_text + row["rel_path"][len(old_text):],
                        new_text,
                        row["id"],
                    ),
                )
            self.db.execute(
                "UPDATE version_files SET source_path = ? || substr(source_path, ?) "
                "WHERE source_path LIKE ?",
                (new_text, len(old_text) + 1, old_text.rstrip("/") + "%"),
            )
            self._log_event(
                "files_relocated",
                message=f"{old_text} → {new_text}（{affected_files} 个引用）",
            )
        return {
            "dry_run": False,
            "version_files": affected_files,
            "blobs": len(blob_rows),
            "examples": [item[2] for item in changed[:5]],
        }

    def broken_references(self, deep: bool = False) -> list[dict[str, Any]]:
        """列出所有失效的引用（文件被移走/改名/就地覆盖），供重新绑定使用。"""
        report = self.verify_files(deep=deep)
        broken: list[dict[str, Any]] = []
        for item in report["missing"]:
            broken.append({**item, "status": "missing"})
        for item in report["changed"]:
            broken.append({**item, "status": "changed"})
        return broken

    def rebind_file(
        self, file_id: int, new_path: str | Path, *, force: bool = False
    ) -> dict[str, Any]:
        """把一个版本文件重新指向新位置（文件被搬运/改名后用）。

        默认要求新文件的内容与当初归档的**指纹一致** ——
        否则那不是"同一个文件挪了地方"，而是另一份内容，
        应当作为新版本归档，而不是偷偷改写历史。
        """
        row = self.db.query_one(
            """
            SELECT vf.*, v.id AS version_id, d.id AS document_id, d.title
              FROM version_files vf
              JOIN versions v ON v.id = vf.version_id
              JOIN documents d ON d.id = v.document_id
             WHERE vf.id = ?
            """,
            (file_id,),
        )
        if row is None:
            raise LibraryError(f"版本文件不存在：{file_id}")

        target = Path(new_path).expanduser()
        if not target.is_file():
            raise LibraryError(f"目标文件不存在：{target}")
        target = target.resolve()

        from .util import sha256_file

        digest, size = self.store.sha256_of(target)
        matched = (not row["sha256"]) or digest == row["sha256"]
        if not matched and not force:
            raise LibraryError(
                f"「{target.name}」的内容与 {row['title']} v{row['seq']} 里记录的指纹不一致，"
                "不是同一个文件。若确认要换成这份内容，请把它作为新版本归档。"
            )

        with self.db.transaction():
            if matched:
                update: dict[str, Any] = {"location": str(target)}
            else:
                # 强制绑定不同内容：为这份内容新建一条 blob 登记，
                # 不去改动原来那条（别的版本可能还引用着它）
                info = self.store.register_reference(target, keep_name_hint=target.name)
                update = {
                    "location": str(target),
                    "blob_id": self._register_blob(info),
                    "sha256": info.sha256,
                    "size": info.size,
                    "mime": info.mime,
                }
            sets = ", ".join(f"{key} = ?" for key in update)
            self.db.execute(
                f"UPDATE version_files SET {sets} WHERE id = ?",
                (*update.values(), file_id),
            )
            # 重新绑定之后，"这个文件抽出来是什么文本"必须重新算：
            # 之前多半是因为文件不在原处才抽出空的，缓存留着就是错的
            self.db.execute("DELETE FROM version_text WHERE file_id = ?", (file_id,))
            self._log_event(
                "file_rebound",
                document_id=row["document_id"],
                version_id=row["version_id"],
                message=f"{row['name']} → {target}"
                        + ("" if matched else "（内容已变，强制绑定）"),
            )

        return {
            "file_id": file_id,
            "document_id": row["document_id"],
            "title": row["title"],
            "version_seq": row["seq"],
            "name": row["name"],
            "path": str(target),
            "content_matched": matched,
        }

    def rebind_from_candidates(
        self, candidates: Iterable[str | Path], *, deep: bool = True,
        force: bool = False, dry_run: bool = False,
    ) -> dict[str, Any]:
        """给一批候选文件，自动把它们绑回失效的引用上。

        匹配顺序：**同名且同大小** → （deep 时）同名且指纹一致 → **指纹一致**。
        这样"整个目录被挪了位置"或"文件被改名"都能一次修好。

        :param dry_run: 只算出会怎么绑，不写库（先看清楚再动手）。
            ``content_matched`` 在预演里也是**真算一次指纹**得出的结论，
            不是"先假定一致"—— 否则预演和真跑会给出相反的答案。
        """
        from .util import sha256_file

        broken = self.broken_references()
        if not broken:
            return {"matched": [], "unmatched": [], "broken": 0}

        pool: list[tuple[Path, str, int]] = []
        digest_cache: dict[Path, str] = {}
        for item in candidates:
            path = Path(item).expanduser()
            if not path.is_file():
                continue
            resolved = path.resolve()
            try:
                size = resolved.stat().st_size
            except OSError:
                continue
            pool.append((resolved, resolved.name, size))

        def digest_of(path: Path) -> str:
            """内容指纹（带缓存，避免同一候选文件被反复读盘）。"""
            if path not in digest_cache:
                value, _ = self.store.sha256_of(path)
                digest_cache[path] = value
            return digest_cache[path]

        matched: list[dict[str, Any]] = []
        unmatched: list[dict[str, Any]] = []
        used: set[Path] = set()

        for reference in broken:
            chosen: Path | None = None
            # 1) 同名同大小
            for entry in pool:
                if entry[0] in used or entry[1] != reference["name"]:
                    continue
                if entry[2] == reference.get("size"):
                    chosen = entry[0]
                    break
            # 2) 同名校验指纹（处理"文件被重存导致大小微变"）
            if chosen is None and deep and reference.get("sha256"):
                for entry in pool:
                    if entry[0] in used or entry[1] != reference["name"]:
                        continue
                    if digest_of(entry[0]) == reference["sha256"]:
                        chosen = entry[0]
                        break
            # 3) 只按指纹找（处理"文件被改名"）
            if chosen is None and deep and reference.get("sha256"):
                for entry in pool:
                    if entry[0] in used:
                        continue
                    if digest_of(entry[0]) == reference["sha256"]:
                        chosen = entry[0]
                        break

            if chosen is None:
                unmatched.append(reference)
                continue
            used.add(chosen)
            if dry_run:
                # **预演也要真算一次指纹**：以前这里硬写 ``content_matched: True``，
                # 于是"预演说内容一致、真跑却报指纹不一致"，用户白高兴一场
                #（同名同大小但内容被换过的文件就是这么撞上的）。
                digest = digest_of(chosen)
                matched.append({
                    "file_id": reference["file_id"],
                    "document_id": reference["document_id"],
                    "title": reference["title"],
                    "version_seq": reference["version_seq"],
                    "name": reference["name"],
                    "path": str(chosen),
                    "content_matched": (not reference.get("sha256"))
                                        or digest == reference["sha256"],
                    "applied": False,
                })
                continue
            try:
                result = self.rebind_file(reference["file_id"], chosen, force=force)
            except LibraryError as error:
                unmatched.append({**reference, "reason": str(error)})
                continue
            matched.append({**result, "applied": True})

        return {
            "matched": matched,
            "unmatched": unmatched,
            "broken": len(broken),
            "dry_run": dry_run,
        }

    def relabel_text_fallbacks(self) -> int:
        """把历史数据里"由任务说明生成的兜底文本"从 primary 改成 note。

        早期迁移把没有文件的记录生成为 ``<事件名>.md`` 并标成主文件，
        界面上就会出现一个看着像伪造的文件名。这里把它纠正为「说明」环节。
        只改**路径完全吻合**的（文件名 = 事件标题 + ``.md``），不碰真正的归档文件。

        改完顺带把 ``version_text`` 里按这个 file_id 缓存的文本删掉：
        缓存是"对着当时的文件"算的，文件名/环节变了就该重新抽，
        否则检索到的还是旧名字那份的结论。
        """
        rows = self.db.query(
            """
            SELECT vf.id AS file_id, vf.name, d.title
              FROM version_files vf
              JOIN versions v ON v.id = vf.version_id
              JOIN documents d ON d.id = v.document_id
             WHERE vf.role IN ('primary', 'attachment')
            """
        )
        changed = 0
        for row in rows:
            expected = f"{row['title']}.md"
            if row["name"] != expected:
                continue
            with self.db.transaction():
                self.db.execute(
                    "UPDATE version_files SET role = 'note', name = '说明.md' WHERE id = ?",
                    (row["file_id"],),
                )
                self.db.execute(
                    "DELETE FROM version_text WHERE file_id = ?", (row["file_id"],)
                )
            changed += 1
        return changed

    def flatten_group_nodes(self, project_id: int | None = None) -> dict[str, Any]:
        """把「分类节点」这一层去掉，让事件直接挂在项目下。

        分类节点的名字会转成其下每个事件的**标签**，信息不丢。

        **必须自底向上处理**：``nodes`` 与 ``documents`` 都是级联删除，
        若先删掉上层分类节点，它下面嵌套的分类节点连同事件会一起被删掉
        """
        projects = (
            [project_id] if project_id is not None
            else [project.id for project in self.list_projects()]
        )
        moved = 0
        removed: list[str] = []
        rescued_nodes = 0

        for pid in projects:
            roots = self.node_tree(pid)
            if not roots:
                continue
            root_id = roots[0].id

            groups = [node for node in self.list_nodes(pid) if node.kind == "group"]
            if not groups:
                continue

            # 深度优先：先深后浅，保证删上层时下面已经搬空
            def depth(node) -> int:
                level, current, guard = 0, node, 0
                while current.parent_id is not None and guard < 64:
                    current = self.get_node(current.parent_id)
                    if current is None:
                        break
                    level += 1
                    guard += 1
                return level

            for group in sorted(groups, key=depth, reverse=True):
                tag_id = self.add_tag(pid, group.name)

                # 1) 事件搬到项目根节点，并打上分类名作为标签
                for document in self.list_documents(group.id, include_archived=True):
                    with self.db.transaction():
                        self.db.execute(
                            "UPDATE documents SET node_id = ?, updated_at = ? WHERE id = ?",
                            (root_id, utcnow_iso(), document.id),
                        )
                        self.db.execute(
                            "INSERT OR IGNORE INTO document_tags(document_id, tag_id) "
                            "VALUES(?,?)",
                            (document.id, tag_id),
                        )
                    moved += 1

                # 2) 分类下面的**普通节点**不能被级联删掉：提到项目根下保留结构
                for child in self.list_nodes(pid):
                    if child.parent_id == group.id and child.kind != "group":
                        with self.db.transaction():
                            self.db.execute(
                                "UPDATE nodes SET parent_id = ?, updated_at = ? WHERE id = ?",
                                (root_id, utcnow_iso(), child.id),
                            )
                        rescued_nodes += 1

                # 3) 此时该分类节点已空，可安全删除
                with self.db.transaction():
                    self.db.execute("DELETE FROM nodes WHERE id = ?", (group.id,))
                removed.append(group.name)

        return {
            "moved_documents": moved,
            "removed_nodes": removed,
            "rescued_nodes": rescued_nodes,
        }

    def verify_files(self, deep: bool = False) -> dict[str, Any]:
        """检查每个版本文件当前是否还在、内容是否还是当初那份。

        :param deep: 为 True 时重新计算 sha256 对比（大库会很慢，超过
            ``DEEP_CHECK_MAX_BYTES`` 的文件自动跳过、只比大小）；
            默认只检查存在性与大小。

        返回 ``{"total", "ok", "missing", "changed", "unverified", "unverified_count"}``，
        条目含文档标题、版本号与路径，便于界面/CLI 直接展示。

        ``unverified`` 是**没有逐字节校验**的那一批（文件很大，只比了大小）。
        以前它们被算进 ``ok``，用户看到的是"全部正常"，而那几个 GB 级文件
        根本没验过。为了不让既有调用方把两件事混起来：``ok`` 计数**不含**它们，
        并且与 ``missing``/``changed`` 一样给出条目列表（``unverified_count``
        是同一批的条数，方便只看数字的调用方）。
        """
        rows = self.db.query(
            """
            SELECT vf.id AS file_id, vf.name, vf.sha256, vf.size, vf.location,
                   b.rel_path, v.seq, d.id AS document_id, d.title
              FROM version_files vf
              JOIN blobs b ON b.id = vf.blob_id
              JOIN versions v ON v.id = vf.version_id
              JOIN documents d ON d.id = v.document_id
             ORDER BY d.title, v.seq
            """
        )
        report: dict[str, Any] = {
            "total": 0, "ok": 0, "missing": [], "changed": [],
            "unverified": [], "unverified_count": 0,
        }
        for row in rows:
            report["total"] += 1
            path = (
                Path(row["location"]) if row["location"]
                else self.store.resolve(row["rel_path"])
            )
            entry = {
                "file_id": row["file_id"],
                "document_id": row["document_id"],
                "title": row["title"],
                "version_seq": row["seq"],
                "name": row["name"],
                "size": row["size"],
                "sha256": row["sha256"],
                "path": str(path),
                "external": bool(row["location"]),
            }
            if not path.is_file():
                entry["reason"] = "文件不存在"
                report["missing"].append(entry)
                continue
            try:
                actual_size = path.stat().st_size
            except OSError:
                entry["reason"] = "无法读取"
                report["missing"].append(entry)
                continue
            if not deep:
                if row["size"] and actual_size != row["size"]:
                    entry["reason"] = f"大小已变（{row['size']} → {actual_size}）"
                    report["changed"].append(entry)
                else:
                    report["ok"] += 1
                continue

            from .diffing import DEEP_CHECK_MAX_BYTES
            from .util import sha256_file

            if actual_size > DEEP_CHECK_MAX_BYTES:
                # 几百 MB 以上的文件不逐字节校验：读一遍只为"证明没变"，
                # 代价是分钟级（库里一个事件就能有 3 GB），大小已经说明问题。
                # **但这类条目不能算进 ok** —— 那是"校验通过"的意思，
                # 用户点「校验文件」会以为全都验过了。
                entry["reason"] = "文件很大，未逐字节校验（只比了大小）"
                report["unverified"].append(entry)
                report["unverified_count"] = len(report["unverified"])
                continue
            digest, _ = sha256_file(path)
            if row["sha256"] and digest != row["sha256"]:
                entry["reason"] = "内容已变（指纹不一致）"
                report["changed"].append(entry)
            else:
                report["ok"] += 1
        return report

    # ---------------------------------------------------------------- 检索

    def search(
        self,
        query: str,
        *,
        project_id: int | None = None,
        limit: int = 200,
        include_text: bool = False,
    ) -> list[dict[str, Any]]:
        """按标题 / 文件名检索；``include_text=True`` 时附带已抽取文本的全文匹配。

        返回条目含文档、节点路径与最新版本，便于界面直接渲染成一棵树。
        """
        keyword = (query or "").strip()
        if not keyword:
            return []
        # 关键词里的 % / _ 是 LIKE 的通配符：不转义的话搜"报告_2024"会命中"报告X2024"，
        # 搜一个"%"直接返回全库
        escaped = (keyword.replace("\\", "\\\\")
                          .replace("%", "\\%").replace("_", "\\_"))
        like = f"%{escaped}%"
        # 已有 WHERE 后面，于是"已归档""别的项目"全都被 OR 放回来了
        # （`... AND 标题 LIKE ? AND 项目 = ? OR 全文命中` 的优先级就是这么走的）。
        # 占位符顺序要和 params 一一对应：匹配分支在前，项目过滤在后。
        params: list[Any] = [like, like]
        sql = """
            SELECT DISTINCT d.id AS document_id
              FROM documents d
              LEFT JOIN versions v ON v.document_id = d.id
              LEFT JOIN version_files vf ON vf.version_id = v.id
             WHERE d.status = 'active'
        """
        if include_text:
            sql += " AND ((d.title LIKE ? ESCAPE '\\' OR vf.name LIKE ? ESCAPE '\\')" \
                   " OR d.id IN (SELECT v.document_id FROM versions v " \
                   "JOIN version_text vt ON vt.version_id = v.id " \
                   "WHERE vt.text LIKE ? ESCAPE '\\'))"
            params.append(like)
        else:
            # 标题/文件名这两个占位符就是上面初始化的那两个 like，不用再加
            sql += " AND (d.title LIKE ? ESCAPE '\\' OR vf.name LIKE ? ESCAPE '\\')"
        if project_id is not None:
            sql += " AND d.node_id IN (SELECT id FROM nodes WHERE project_id = ?)"
            params.append(project_id)
        sql += " ORDER BY d.title COLLATE NOCASE LIMIT ?"
        params.append(limit)

        hits: list[dict[str, Any]] = []
        for row in self.db.query(sql, params):
            document = self.get_document(row["document_id"])
            if document is None:
                continue
            hits.append(
                {
                    "document": document,
                    "node_path": self.node_path(document.node_id),
                    "node_id": document.node_id,
                    "latest_version": self.latest_version(document.id),
                }
            )
        return hits

    # ---------------------------------------------------------- 事件与统计

    def list_events(
        self, *, document_id: int | None = None, project_id: int | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if document_id is not None:
            clauses.append("document_id = ?")
            params.append(document_id)
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self.db.query(
            f"SELECT * FROM events {where} ORDER BY id DESC LIMIT ?", params
        )
        return [dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        """库级统计，界面状态栏与 CLI 都用。"""
        counts = {
            "projects": self.db.scalar("SELECT COUNT(*) FROM projects"),
            "nodes": self.db.scalar("SELECT COUNT(*) FROM nodes"),
            "documents": self.db.scalar("SELECT COUNT(*) FROM documents WHERE status = 'active'"),
            "archived_documents": self.db.scalar(
                "SELECT COUNT(*) FROM documents WHERE status = 'archived'"
            ),
            "versions": self.db.scalar("SELECT COUNT(*) FROM versions"),
            "blobs": self.db.scalar("SELECT COUNT(*) FROM blobs"),
            "reviews": self.db.scalar("SELECT COUNT(*) FROM reviews"),
        }
        counts["referenced_files"] = self.db.scalar(
            "SELECT COUNT(*) FROM version_files WHERE location != ''"
        ) or 0
        return {
            "root": str(self.root),
            "name": self.info.name,
            "file_mode": self.file_mode,
            "sqlite_bytes": self.db.path.stat().st_size if self.db.path.exists() else 0,
            "blob_bytes": self.store.total_size(),
            "counts": {key: int(value or 0) for key, value in counts.items()},
            "schema_version": self.db.get_meta("schema_version"),
        }

    def stage_export(self, version_id: int, tag: str = ".drag") -> list[Path]:
        """把版本文件摆进导出暂存区，供「拖到桌面/邮件」使用。

        优先用**硬链接**（同盘零拷贝、不占额外空间），失败才退化为复制。
        暂存路径形如 ``<库根>/exports/.drag/v3/原始文件名``，
        这样拖出去的文件名是用户认得的原名，而不是 sha256。
        """
        import os

        version = self.get_version(version_id)
        if version is None:
            raise LibraryError(f"版本不存在：{version_id}")
        document = self.get_document(version.document_id)
        title = safe_filename(document.title if document else f"doc{version.document_id}")

        # 路径里必须带**事件身份**：两个事件同为 v1、又恰好有同名同大小的文件时，
        # 只按 v{seq} 命名会让后一份复用前一份的暂存文件 ——
        target_dir = (self.root / "exports" / tag
                      / f"doc{version.document_id}" / f"v{version.seq}")
        target_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for item in version.files:
            target = target_dir / safe_filename(item.name)
            source = self._file_path(item)
            if source is None:
                continue
            stale = False
            if target.exists():
                try:
                    stale = (target.stat().st_size != item.size
                             or _file_sha256(target)[0] != (item.sha256 or ""))
                except OSError:
                    stale = True
            if not target.exists() or stale:
                if item.location:
                    # 引用模式：外面那份是**用户的原始文件**，
                    # 绝不能硬链接出去 —— 拖到别的程序里被就地改写会连累原文件，
                    # 所以这里老老实实复制一份。
                    shutil_copy(source, target)
                else:
                    try:
                        os.link(source, target)
                    except OSError:
                        shutil_copy(source, target)
            written.append(target)
        return written

    def gc_blobs(self, dry_run: bool = True) -> list[str]:
        """回收没有任何版本引用的 blob（例如硬删除文档之后）。

        返回被删除（或将被删除）的相对路径列表。
        """
        orphans = self.db.query(
            """
            SELECT b.id, b.rel_path FROM blobs b
             WHERE NOT EXISTS (SELECT 1 FROM version_files vf WHERE vf.blob_id = b.id)
            """
        )
        removed: list[str] = []
        for row in orphans:
            removed.append(row["rel_path"])
            if not dry_run:
                with self.db.transaction():
                    self.db.execute("DELETE FROM blobs WHERE id = ?", (row["id"],))
                self.store.delete(row["rel_path"])
        return removed

    def materialize_references(self, *, dry_run: bool = False,
                               progress=None) -> dict[str, Any]:
        """把**引用模式**（文件留在库外）的残留记录**复制进库**，改成库内副本。

        文件路径里，现在库里还有残留 —— 右键「打开所在文件夹」指到的是原来目录。
        这个迁移把残留的 reference 记录逐个变成 copy：读外部文件、复制进
        ``files/<别名>/<原名>``，再把 blobs 与 version_files 改指库内路径。

        **绝不移动/删除原文件**：这里只做 ``store.put_file(..., mode='copy')``
        复制，原文件照旧留在原处（那正是本库"绝不删用户原文件"的纪律）。

        :param dry_run: True 时只统计、不落库也不复制。
        :param progress: 可选的 ``progress(done, total)`` 回调。
        :returns: ``{"dry_run", "total", "materialized", "missing"}``。
        """
        rows = self.db.query("SELECT id, rel_path FROM blobs WHERE source = 'reference'")
        total = len(rows)
        materialized = 0
        missing: list[str] = []
        for index, row in enumerate(rows):
            external = Path(row["rel_path"])
            if not external.is_file():
                # 有些残留的路径记在 version_files.location（blob.rel_path 也可能是
                # 空/旧值）：从 location 再找一次
                loc = self.db.scalar(
                    "SELECT location FROM version_files WHERE blob_id = ? "
                    "AND location != '' LIMIT 1",
                    (row["id"],),
                )
                if loc:
                    external = Path(loc)
            if not external.is_file():
                missing.append(str(external or row["rel_path"]))
                if progress:
                    progress(index + 1, total)
                continue
            materialized += 1
            if not dry_run:
                info = self.store.put_file(external, mode="copy")
                with self.db.transaction():
                    self.db.execute(
                        "UPDATE blobs SET rel_path = ?, source = ?, origin_path = ? "
                        "WHERE id = ?",
                        (info.rel_path, SOURCE_STORE, str(external.parent), row["id"]),
                    )
                    # 这一行从此走库内副本（location 清空，_file_path 落回 rel_path）
                    self.db.execute(
                        "UPDATE version_files SET location = '' WHERE blob_id = ?",
                        (row["id"],),
                    )
            if progress:
                progress(index + 1, total)
        return {
            "dry_run": dry_run,
            "total": total,
            "materialized": materialized,
            "missing": missing,
        }

    # ------------------------------------------------------------ 内部工具

    def _log_event(
        self,
        kind: str,
        *,
        message: str = "",
        project_id: int | None = None,
        node_id: int | None = None,
        document_id: int | None = None,
        version_id: int | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO events(project_id, node_id, document_id, version_id, kind, message, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (project_id, node_id, document_id, version_id, kind, message, utcnow_iso()),
        )

    def _register_blob(self, info: BlobInfo) -> int:
        """把 blob 登记到数据库（同 sha256 复用已有行，省一份磁盘）。

        照旧版 C++ 归档"一个文件一个文件夹"的逻辑，``put_file`` 已经按
        ``files/<别名>/<原名>`` 复制了一份进来；若内容（sha256）库里已有，
        这份新复制的是重复的，删掉它、复用旧记录 —— 别在 files/ 里留孤儿。
        """
        row = self.db.query_one("SELECT id FROM blobs WHERE sha256 = ?", (info.sha256,))
        if row:
            if info.source == SOURCE_STORE:
                self.store.delete(info.rel_path)   # 只删 files/ 下的库内副本
            return int(row["id"])
        return self.db.insert(
            "INSERT INTO blobs(sha256, size, ext, mime, rel_path, created_at, source, "
            "origin_path) VALUES(?,?,?,?,?,?,?,?)",
            (info.sha256, info.size, info.ext, info.mime, info.rel_path, utcnow_iso(),
             info.source, info.origin_path),
        )

    def _insert_blob(self, info: BlobInfo) -> int:
        """**无条件**新插一行 blob（= 库里再多一份实体文件）。

        与 :meth:`_register_blob` 的区别：那个按 sha256 去重（归档同一个文件两次
        不会在 ``files/`` 下留两份），这个**刻意不去重** —— 用在"把这一份复制登记到
        而不是仅仅复制链接"）。``blobs.sha256`` 从 v15 起不再是 UNIQUE，
        正是为了允许这种"同内容、两份实体"。
        """
        return self.db.insert(
            "INSERT INTO blobs(sha256, size, ext, mime, rel_path, created_at, source, "
            "origin_path) VALUES(?,?,?,?,?,?,?,?)",
            (info.sha256, info.size, info.ext, info.mime, info.rel_path, utcnow_iso(),
             info.source, info.origin_path),
        )

    def file_path(self, version_file: VersionFile | None) -> Path | None:
        """公开版：解析一个版本文件当前的真实路径（导出、打开、复制都用它）。"""
        return self._file_path(version_file)

    def _file_path(self, version_file: VersionFile | None) -> Path | None:
        """解析一个版本文件当前的真实路径。

        引用模式下用 ``location``（文件在库外，用户自己放的地方），
        库内存储时用 blob 路径。文件不存在时也返回路径，交由调用方判断。
        """
        if version_file is None:
            return None
        if version_file.location:
            return Path(version_file.location)
        if version_file.rel_path:
            return self.store.resolve(version_file.rel_path)
        return None

    def _store_spec(
        self, spec: FileSpec, document: Document, mode: str = "copy"
    ) -> tuple[BlobInfo, str, str | None]:
        """纳管一个文件：按 ``mode`` 决定是「就地引用」还是「复制/硬链接进库」。

        ``mode`` 见 ``tooak.core.store`` 的说明；兜底值与库的默认一致（``copy``：
        归档就进库），调用方一律显式传 ``resolve_file_mode(...)`` 的结果。
        """
        name = spec.display_name
        if spec.is_text:
            info = self.store.put_bytes(spec.text.encode("utf-8"), name_hint=name)
            return info, name, None

        path = spec.path
        if not path.is_file():
            if spec.reuse_blob_id:
                # 沿用上一稿的记录，只是原文件已经不在原处了：不读盘、不报错，
                # 由调用方把"这一稿没带上这份内容"如实告诉用户
                return (
                    BlobInfo(
                        sha256=spec.sha256 or "",
                        size=spec.reuse_size,
                        ext=suffix_of(name),
                        mime=spec.reuse_mime,
                        rel_path=spec.reuse_location or str(path),
                        source=SOURCE_REFERENCE,
                        origin_path=str(path.parent),
                    ),
                    name,
                    utcnow_iso_from_epoch(0),
                )
            raise LibraryError(f"文件不存在：{path}")
        if spec.reuse_blob_id and self._reusable(spec):
            # 内容没变（大小 + 修改时间都对得上）：直接用回原来的 blob 记录。
            # 这是"归档新一稿"最常见的路径，绝不该在这里把几 GB 的文件再读一遍。
            row = self.db.query_one("SELECT * FROM blobs WHERE id = ?", (spec.reuse_blob_id,))
            return (
                BlobInfo(
                    sha256=str(row["sha256"]) if row else (spec.sha256 or ""),
                    size=spec.reuse_size,
                    ext=suffix_of(name),
                    mime=spec.reuse_mime,
                    rel_path=str(row["rel_path"]) if row else str(path),
                    source=str(row["source"]) if row else SOURCE_REFERENCE,
                    origin_path=str(row["origin_path"]) if row is not None and "origin_path" in row.keys() else "",
                ),
                name,
                utcnow_iso_from_epoch(path.stat().st_mtime),
            )
        info = self.store.put_file(path, keep_name_hint=name, mode=mode)
        try:
            mtime = utcnow_iso_from_epoch(path.stat().st_mtime)
        except OSError:
            mtime = None
        return info, name, mtime

    def _reusable(self, spec: FileSpec) -> bool:
        """这份"沿用上一稿"的文件内容还是原来那份吗（只 stat，不读盘）。

        判据：大小一致 **且** 修改时间一致。两样都对不上就老老实实重新纳管
        （重新算指纹、必要时重新复制），绝不会给出错误的内容结论。
        """
        try:
            path = spec.path
            stat = path.stat()
        except OSError:
            return False
        if spec.reuse_size and stat.st_size != spec.reuse_size:
            return False
        recorded = self.db.query_one(
            "SELECT mtime_ns FROM version_files WHERE blob_id = ? AND size = ? "
            "ORDER BY id LIMIT 1",
            (spec.reuse_blob_id, spec.reuse_size),
        )
        if recorded is None or not recorded["mtime_ns"]:
            # 老记录没存 mtime：无法证明"没变"，那就按"可能变了"处理（重新纳管）
            return False
        return int(recorded["mtime_ns"]) == int(stat.st_mtime_ns)

    @staticmethod
    def _normalize_specs(
        files: Sequence[FileSpec | tuple[Path | str, str]] | None,
        text: str | None,
        document: Document,
    ) -> list[FileSpec]:
        specs: list[FileSpec] = []
        for item in files or []:
            if isinstance(item, FileSpec):
                specs.append(item)
            else:
                raw_path, role = item
                specs.append(FileSpec(path=Path(raw_path), role=role))
        if text is not None:
            # 文字内容统一叫「说明.md」，环节是 note（"说明"），
            # 不要用 <事件名>.md 这种名字 —— 那在界面上看起来像个伪造的文件
            hint = "说明.md"
            specs.append(FileSpec(path=Path(hint), role="note", name=hint, text=text))
        return specs

    def _spec_signature(self, spec: FileSpec) -> tuple[str, str, str]:
        """一个待归档文件在"版本指纹"里的表示：``(文件名, 环节, 内容指纹)``。"""
        if spec.sha256:
            return (spec.display_name, spec.role, spec.sha256)
        if spec.is_text:
            return (spec.display_name, spec.role, sha256_bytes(spec.text.encode("utf-8")))
        digest, _ = self.store.sha256_of(spec.path)
        return (spec.display_name, spec.role, digest)


def _under_prefix(path: str, prefix: str) -> bool:
    """``path`` 是否真的在 ``prefix`` 这个目录**里面**（照路径分量比，不看字符串前缀）。"""
    if not path or not prefix:
        return False
    try:
        return Path(path).parent == Path(prefix) or Path(prefix) in Path(path).parents
    except (OSError, ValueError):
        return False


def utcnow_iso_from_epoch(epoch: float) -> str:
    """把 Unix 时间戳转成库内统一的 UTC ISO 文本。"""
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def shutil_copy(source: Path, target: Path) -> None:
    """复制文件（与硬链接互为兜底）。"""
    import shutil

    target.unlink(missing_ok=True)
    shutil.copyfile(source, target)


def format_version_line(version: Version, *, with_time: bool = True) -> str:
    """在 CLI/日志里显示一行版本信息。"""
    from .util import to_local_str

    stamp = to_local_str(version.created_at) if with_time else ""
    state = REVIEW_STATE_LABELS.get(version.review_state, version.review_state)
    origin = ORIGIN_LABELS.get(version.origin, version.origin)
    return (
        f"v{version.seq:<3} {stamp:<17} {state:<4} {origin:<6} "
        f"{short_hash(version.sha256)} {version.source_name}"
    )


def parse_time(value: str | None):
    """对外暴露给界面用的小工具。"""
    return parse_iso(value)

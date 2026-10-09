"""批量导出：把一个组（或框选出来的一批事件枝）连文件带说明导成一个文件夹。

------------
"提供批量导出组里的文件包的功能、或者框选的包的数据导出的功能，
导出的是文件夹，里面分为组名ID、各环节的信息"。

于是目录长这样（``name`` 是组名 + 组 id，或框选时的一行概括）::

    <你选的目录>/<组名-id>/
        组信息.md            # 这个组是什么、谁导的、成员事件枝清单
        导出清单.csv         # 每一个文件的相对路径 / 大小 / sha256 / 原始路径
        <事件枝标题>/
            事件枝信息.md      # 状态、开始时间、年轮与说明、依赖、所属组
            输入文件/ <文件…>
            分析资料/ <文件…>
            输出文件/ <文件…>
            参考数据/ 图示/ 其他文件/ …
            说明.md          # 这个事件枝当前稿的说明（有的话）

几条硬规则（跟整个项目一致）
----------------------------
* **只读**：源文件一个字节都不改、不移动、不删除，只往目标目录里 copy；
* 就地引用的文件要是已经被用户挪走/删掉，记进 ``missing``，在清单里写明，不让整批导出失败；
* 重名文件不覆盖：同名时加上短哈希前缀（``a1b2c3d4_报告.pdf``）；
* 过程回调 ``progress(done, total, name)``，界面好在状态栏显示进度。
"""

from __future__ import annotations

import csv
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from .library import (
    DOC_STATE_LABELS,
    ROLE_LABELS,
    Library,
    VersionFile,
)
from .util import human_size, safe_filename, short_hash, to_local_str

__all__ = ["ExportResult", "export_documents", "export_group", "safe_folder_name",
           "pack_folder_to_zip"]

#: 环节顺序：导出时按这个顺序建子文件夹（跟界面上的一致）
ROLE_ORDER = ["input", "analysis", "output", "reference", "figure", "extra", "primary"]

#: 顶层文件夹名的长度上限（比文件名的 180 短：导出目录常常还嵌在很深的路径里）
_MAX_FOLDER_NAME = 80


def safe_folder_name(name: str, fallback: str = "未命名") -> str:
    """把名字收拾成一个能当文件夹名的字符串。

    直接复用 :func:`tooak.core.util.safe_filename`。这里原来自己维护了**第二套**规则，
    比 util 少了三样东西：控制字符、Windows 保留设备名（COM1/NUL/CON…）、
    以及"截断后再收尾"。导出时 ``mkdir("COM1")`` / 名字里带 ``\\x01`` 会直接抛
    ``OSError``，把**整批导出**带崩（一个事件枝名里有控制字符就全导不出来）。
    只保留"最多 80 字符"这条长度限制。
    """
    cleaned = safe_filename(name, fallback)[:_MAX_FOLDER_NAME]
    # 截断可能正好落在点/空格上；Windows 建不出以点或空格结尾的名字
    return cleaned.rstrip(" .") or fallback


@dataclass
class ExportResult:
    """导出结果（拿去给状态栏/提示框显示）。"""

    root: Path
    documents: int = 0
    files: int = 0
    bytes: int = 0
    #: 就地引用的文件已经不在了：``[(事件枝, 文件名, 原始路径)]``
    missing: list[tuple[str, str, str]] = field(default_factory=list)
    #: 没能解析出路径的文件（库里只留了引用但位置信息丢了）
    unresolved: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.files > 0

    def summary(self) -> str:
        bits = [f"导出 {self.documents} 个事件枝 / {self.files} 个文件"
                f"（{human_size(self.bytes)}）"]
        if self.missing:
            bits.append(f"{len(self.missing)} 个文件已不在原处")
        if self.unresolved:
            bits.append(f"{len(self.unresolved)} 个文件没有可用的路径")
        return "；".join(bits) + f" → {self.root}"


def _role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role or "其他文件")


def _claim_folder(folder: Path) -> Path:
    """**抢占**一个还不存在的目录，返回真正建成的那个（重名就 -2、-3…）。

    为什么不是"先查空位、再 ``mkdir(exist_ok=True)``"：两个导出任务同时跑时，
    两边都会看到"目录还不存在"、都挑中同一个名字，然后一起往里写 ——
    后写的把先写的 ``导出清单.csv`` 覆盖掉，两边都以为自己导成功了。
    ``os.mkdir``（``Path.mkdir`` 不带 ``exist_ok``）在同名目录已存在时抛
    ``FileExistsError``，**抢不到就等于知道撞了**，换下一个名字重试即可 ——
    这一步是原子的，不需要锁。
    """
    index = 1
    while True:
        candidate = folder if index == 1 else folder.with_name(f"{folder.name}-{index}")
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            index += 1
            continue
        return candidate


def _dedupe_target(target: Path, sha256: str) -> Path:
    """目录内重名兜底：先加短哈希前缀，仍然重名就继续追加序号。

    只加一次短哈希是不够的：同一个环节里放两份**内容相同**的同名文件时，
    短哈希也一样，第二次 ``copy2`` 会**静默覆盖**第一份 ——
    用户丢了一份文件而且清单里看不出来。
    """
    prefix = short_hash(sha256, 8)
    candidate = target.with_name(f"{prefix}_{target.name}")
    index = 2
    while candidate.exists():
        candidate = target.with_name(f"{prefix}_{index}_{target.name}")
        index += 1
    return candidate


def _document_folder_title(library: Library, document_id: int, index: int) -> str:
    document = library.get_document(document_id)
    title = document.title if document else f"事件枝{index}"
    return f"{index:02d}-{safe_folder_name(title, f'事件枝{index}')}"


def _version_files_by_role(library: Library, version) -> dict[str, list[VersionFile]]:
    grouped: dict[str, list[VersionFile]] = {}
    for item in version.files:
        grouped.setdefault(item.role or "primary", []).append(item)
    return grouped


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _document_report(library: Library, document_id: int) -> str:
    """一个事件枝的说明文档：状态、年轮、说明、依赖、所属组。"""
    document = library.get_document(document_id)
    if document is None:
        return "# 事件枝已不存在\n"
    lines = [f"# {document.title}", ""]
    lines.append(f"- 状态：{DOC_STATE_LABELS.get(document.state, document.state)}")
    if document.people:
        lines.append(f"- 相关人：{document.people}")
    if document.task_date:
        lines.append(f"- 任务日期：{document.task_date}")
    if document.due_date:
        lines.append(f"- 截止：{document.due_date}")
    if document.progress is not None:
        lines.append(f"- 进度：{document.progress}%")
    if document.description:
        lines.append(f"- 描述：{document.description}")
    groups = library.groups_of_document(document_id)
    if groups:
        lines.append("- 分组：" + "、".join(item["name"] for item in groups))

    current = library.get_version(document.current_version_id or 0)
    history = library.version_history(document_id)
    lines += ["", f"## 年轮（共 {len(history)} 稿，当前 "
              f"{('v' + str(current.seq)) if current else '—'}）", ""]
    for version in reversed(history):        # 从新到旧
        stamp = to_local_str(version.created_at, "%Y-%m-%d %H:%M") if version.created_at else ""
        mark = " ←当前稿" if current and version.id == current.id else ""
        lines.append(f"### v{version.seq}　{stamp}{mark}")
        if version.note:
            lines.append(f"- 说明：{version.note}")
        if version.files:
            for role in ROLE_ORDER:
                items = [f for f in version.files if (f.role or "primary") == role]
                for item in items:
                    state = f"（{item.status}）" if item.status else ""
                    lines.append(f"- {_role_label(role)}：{item.name}{state}")
        else:
            lines.append("- （这一稿没有文件）")
        lines.append("")

    links = library.document_links(document_id)
    if links:
        lines += ["## 依赖关系", ""]
        for link in links:
            kind = "依赖" if link.get("kind") == "depends_on" else str(link.get("kind"))
            lines.append(f"- {kind}：{link.get('other_title', '')}")
        lines.append("")
    return "\n".join(lines)


def _group_report(name: str, note: str, documents: list[tuple[str, str]],
                  source: str) -> str:
    lines = [f"# {name}", ""]
    lines.append(f"- 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"- 来源：{source}")
    if note:
        lines.append(f"- 说明：{note}")
    lines.append(f"- 事件枝数：{len(documents)}")
    lines += ["", "## 事件枝清单", ""]
    for folder, title in documents:
        lines.append(f"- {title}　→　`{folder}/`")
    lines.append("")
    return "\n".join(lines)


def export_documents(
    library: Library,
    document_ids: Iterable[int],
    target_dir: str | Path,
    *,
    name: str,
    note: str = "",
    source: str = "框选",
    progress: Callable[[int, int, str], None] | None = None,
) -> ExportResult:
    """把一批事件枝导成一个文件夹（含各环节的文件与说明）。

    :param name: 顶层文件夹名（组就是"组名-组id"）。
    :param target_dir: 放在哪个目录下（会自动新建 ``target_dir/name``）。
    :param progress: ``progress(已处理, 总数, 当前事件枝标题)``，可用来显示进度。
    """
    ids = [int(item) for item in document_ids]
    # 抢占目录（不覆盖别人的导出）：见 _claim_folder 的说明
    root = _claim_folder(Path(target_dir).expanduser() / safe_folder_name(name, "导出"))
    result = ExportResult(root=root)

    manifest: list[tuple[str, str, int, str, str]] = []   # 相对路径/事件枝/大小/sha/原始
    listed: list[tuple[str, str]] = []                    # (文件夹, 事件枝标题)

    for index, document_id in enumerate(ids, start=1):
        document = library.get_document(document_id)
        title = document.title if document else f"事件枝{document_id}"
        if progress is not None:
            progress(index - 1, len(ids), title)
        folder_name = _document_folder_title(library, document_id, index)
        folder = root / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        listed.append((folder_name, title))
        result.documents += 1
        _write_text(folder / "事件枝信息.md", _document_report(library, document_id))

        version = library.get_version(document.current_version_id or 0)
        if version is None or not version.files:
            continue
        note_body = ""      # 文字稿（说明）的内容：跟版本说明合并成一个文件
        for role, items in _version_files_by_role(library, version).items():
            # 文字稿（说明）不按环节复制：它本来就不是磁盘上的文件，
            # 而是库里的文本 —— 最后合并写成事件枝根目录下的「说明.md」
            if role == "note":
                for item in items:
                    text_path = library.file_path(item)
                    if text_path is not None and Path(text_path).exists():
                        try:
                            note_body = Path(text_path).read_text(
                                encoding="utf-8", errors="replace"
                            )
                        except OSError:
                            continue
                continue
            stage_folder = folder / _role_label(role)
            for item in items:
                source_path = library.file_path(item)
                if source_path is None:
                    result.unresolved.append((title, item.name))
                    continue
                source_path = Path(source_path)
                if not source_path.exists():
                    result.missing.append((title, item.name, str(source_path)))
                    continue
                stage_folder.mkdir(parents=True, exist_ok=True)
                target = stage_folder / safe_folder_name(item.name, "文件")
                if target.exists():
                    target = _dedupe_target(target, item.sha256)
                try:
                    shutil.copy2(source_path, target)
                except OSError as error:                     # 权限/占用：别让整批失败
                    result.missing.append((title, item.name, f"{source_path}（{error}）"))
                    continue
                result.files += 1
                try:
                    size = target.stat().st_size
                except OSError:
                    size = item.size or 0
                result.bytes += size
                manifest.append((
                    str(target.relative_to(root)), title, size,
                    item.sha256 or "", str(source_path),
                ))
                if progress is not None:
                    progress(index - 1, len(ids), f"{title} · {item.name}")
        parts: list[str] = []
        if note_body.strip():
            parts.append(note_body.strip())
        if version.note.strip() and version.note.strip() not in note_body:
            parts.append(f"这一稿的说明：{version.note.strip()}")
        if parts:
            _write_text(folder / "说明.md",
                        f"# {title} · 说明\n\n" + "\n\n".join(parts) + "\n")

    _write_text(root / "组信息.md", _group_report(name, note, listed, source))
    _write_manifest(root / "导出清单.csv", manifest)
    if progress is not None:
        progress(len(ids), len(ids), "完成")
    return result


def _write_manifest(path: Path, rows: list[tuple[str, str, int, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["相对路径", "事件枝", "大小(字节)", "sha256", "原始路径"])
        for row in rows:
            writer.writerow(row)


def export_group(library: Library, group_id: int, target_dir: str | Path,
                 *, progress: Callable[[int, int, str], None] | None = None,
                 ) -> ExportResult:
    """导出一个组的文件包（组名-组id 作为顶层文件夹名）。"""
    row = library.db.query_one(
        "SELECT id, project_id, name, note FROM board_groups WHERE id = ?",
        (group_id,),
    )
    if row is None:
        raise ValueError(f"组不存在：{group_id}")
    group = dict(row)
    members = library.db.query(
        "SELECT m.document_id, d.title FROM board_group_members m "
        "JOIN documents d ON d.id = m.document_id "
        "WHERE m.group_id = ? ORDER BY d.title COLLATE NOCASE",
        (group_id,),
    )
    name = f"{group.get('name') or '未命名组'}-{group_id}"
    return export_documents(
        library, [int(item["document_id"]) for item in members], target_dir,
        name=name, note=str(group.get("note") or ""),
        source=f"项目白板上的分组「{group.get('name') or '未命名'}」(id={group_id})",
        progress=progress,
    )


def pack_folder_to_zip(folder: str | Path,
                       progress: Callable[[int, int, str], None] | None = None,
                       cancel: Callable[[], bool] | None = None) -> tuple[Path, int, int]:
    """把一个文件夹打包成 zip，**就放在它旁边**（和它同级）。

    用户的原话："用户拖进来就是希望归档的，给他直接按单文档逻辑做就好" ——
    所以这里刻意**不做"临时包"**：打包出来就是用户磁盘上一个正常的 zip 文件
    （名字 <文件夹名>.zip，重名就 -2、-3），ToOak 之后按"一个文档"来对待它，
    就地引用、不复制、不删除，也不需要在库里额外存一份。

    返回 ``(zip 路径, 文件数, 总字节)``。文件夹本身**原样保留**（我们从不删用户的东西）。
    """
    source = Path(folder).expanduser()
    if not source.is_dir():
        raise ValueError(f"不是一个文件夹：{source}")
    files = [item for item in source.rglob("*") if item.is_file()]
    total = len(files)
    target = source.parent / f"{safe_folder_name(source.name, '文件夹')}.zip"
    index = 2
    while target.exists():
        target = source.parent / f"{safe_folder_name(source.name, '文件夹')}-{index}.zip"
        index += 1

    written = 0
    size = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for item in files:
            if cancel is not None and cancel():
                archive.close()
                try:
                    target.unlink()          # 取消：把半成品清掉（这是我们自己刚建的）
                except OSError:
                    pass
                raise InterruptedError("打包已取消")
            if progress is not None:
                progress(written, total, item.name)
            archive.write(item, item.relative_to(source).as_posix())
            written += 1
            try:
                size += item.stat().st_size
            except OSError:
                pass
        if not files:                        # 空文件夹也给一个合法的 zip
            archive.writestr(f"{source.name}/", "")
    if progress is not None:
        progress(total, total, target.name)
    return target, written, size

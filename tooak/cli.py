"""ToOak 命令行工具。

内核能力全部可以从命令行驱动 —— 这既是给用户的批处理入口，
也是在没有界面的机器（服务器、CI）上操作同一份库的方式。

常用示例::

    python -m tooak.cli init ~/我的库
    python -m tooak.cli project-add 示例项目
    python -m tooak.cli node-add "示例项目/结构设计"
    python -m tooak.cli add 报告.docx --node "示例项目/结构设计"
    python -m tooak.cli log 报告
    python -m tooak.cli diff 报告 --from v1 --to v3
    python -m tooak.cli review 报告 --version latest --state approved --by 张工
    python -m tooak.cli serve --port 8765
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Sequence

from .core import Library, LibraryError
from .version import version_text
from .core.library import (
    ORIGIN_LABELS,
    REVIEW_STATE_LABELS,
    Document,
    Version,
)
from .core.util import human_size, short_hash, to_local_str
from .core.versiongraph import build_version_graph

__all__ = ["main", "build_parser", "default_library_path"]

#: 纳管方式给用户看的中文说法（``file-mode`` 子命令打印用）
FILE_MODE_LABELS = {
    "copy": "归档时复制一份进库，库自带内容",
    "link": "硬链接进库，同盘零拷贝",
    "reference": "文件留在原处，库里只登记路径与指纹",
}


def default_library_path() -> Path:
    # TOOAK_LIBRARY 是现在的名字；TOAK_LIBRARY 是品牌统一前的旧名，继续认
    env = os.environ.get("TOOAK_LIBRARY") or os.environ.get("TOAK_LIBRARY")
    if env:
        return Path(env).expanduser()
    return Path.home() / "ToOakLibrary"


# ---------------------------------------------------------------------------
# 解析辅助
# ---------------------------------------------------------------------------


def resolve_node(library: Library, reference: str) -> int:
    """节点引用：支持纯数字 id，也支持「项目/子节点/孙节点」路径。"""
    text = (reference or "").strip()
    if not text:
        raise LibraryError("请指定节点")
    if text.isdigit():
        node = library.get_node(int(text))
        if node is None:
            raise LibraryError(f"节点不存在：{text}")
        return node.id

    parts = [part for part in text.replace("\\", "/").split("/") if part.strip()]
    project = library.find_project(parts[0])
    if project is None:
        raise LibraryError(f"项目不存在：{parts[0]}")

    current_ids = [node.id for node in library.node_tree(project.id)]
    current_id: int | None = current_ids[0] if current_ids else None
    for part in parts[1:]:
        found = None
        for candidate in library.list_nodes(project.id):
            if candidate.parent_id == current_id and candidate.name == part:
                found = candidate
                break
        if found is None:
            raise LibraryError(f"节点不存在：{text}（在「{part}」处中断）")
        current_id = found.id
    if current_id is None:
        raise LibraryError(f"项目「{project.name}」没有根节点")
    return current_id


def resolve_document(library: Library, reference: str, node_id: int | None = None) -> Document:
    """文档引用：支持 id、完整标题、标题前缀。"""
    text = (reference or "").strip()
    if not text:
        raise LibraryError("请指定文档")
    if text.isdigit():
        document = library.get_document(int(text))
        if document is None:
            raise LibraryError(f"文档不存在：{text}")
        return document

    hits = library.search(text)
    if node_id is not None:
        scoped = [hit for hit in hits if hit["node_id"] == node_id]
        hits = scoped or hits
    exact = [hit for hit in hits if hit["document"].title == text]
    if exact:
        return exact[0]["document"]
    if len(hits) == 1:
        return hits[0]["document"]
    if not hits:
        raise LibraryError(f"未找到文档：{text}")
    titles = "、".join(hit["document"].title for hit in hits[:5])
    raise LibraryError(f"「{text}」匹配到多份文档，请用更精确的标题：{titles}")


def resolve_version(library: Library, document: Document, reference: str | None) -> Version:
    """版本引用：``latest``/``head``/空 -> 当前版本；``v3``/``3`` -> 第 3 版。"""
    text = (reference or "").strip().lower()
    if text in ("", "latest", "head", "current"):
        version = library.latest_version(document.id)
        if version is None:
            raise LibraryError(f"文档「{document.title}」还没有任何版本")
        return version
    seq_text = text[1:] if text.startswith("v") else text
    if not seq_text.isdigit():
        raise LibraryError(f"无法识别的版本号：{reference}")
    seq = int(seq_text)
    for version in library.version_history(document.id):
        if version.seq == seq:
            return version
    raise LibraryError(f"文档「{document.title}」不存在 v{seq}")


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------


class Printer:
    """统一处理 ``--json`` 与人类可读输出。"""

    def __init__(self, as_json: bool = False):
        self.as_json = as_json

    def emit(self, payload: Any, text: str = "") -> None:
        if self.as_json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        elif text:
            print(text)


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace, printer: Printer) -> int:
    library = Library.create(args.library, name=args.name)
    printer.emit(
        {"root": str(library.root), "name": library.info.name},
        f"已创建 ToOak 库：{library.root}",
    )
    library.close()
    return 0


def cmd_stats(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        stats = library.stats()
    counts = stats["counts"]
    text = (
        f"库：{stats['name']}\n"
        f"位置：{stats['root']}\n"
        f"项目 {counts['projects']} · 节点 {counts['nodes']} · 文档 {counts['documents']} "
        f"· 版本 {counts['versions']} · 去重后的文件块 {counts['blobs']}\n"
        f"数据库 {human_size(stats['sqlite_bytes'])} · 文件库 {human_size(stats['blob_bytes'])}"
    )
    printer.emit(stats, text)
    return 0


def cmd_project_add(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        project = library.create_project(args.name, description=args.description or "")
    printer.emit({"id": project.id, "name": project.name}, f"已创建项目：{project.name}（id={project.id}）")
    return 0


def cmd_projects(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        projects = library.list_projects()
    if printer.as_json:
        printer.emit([project.__dict__ for project in projects])
        return 0
    if not projects:
        print("（还没有项目，先执行 project-add）")
        return 0
    print(f"{'id':<5}{'项目':<24}{'节点':>5}{'文档':>6}{'版本':>6}")
    for project in projects:
        print(
            f"{project.id:<5}{project.name:<24}{project.node_count:>5}"
            f"{project.document_count:>6}{project.version_count:>6}"
        )
    return 0


def cmd_node_add(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        if args.parent and args.name:
            parent_id = resolve_node(library, args.parent)
            project_node = library.get_node(parent_id)
            assert project_node is not None
            node = library.create_node(
                project_node.project_id, args.name, parent_id=parent_id, kind=args.kind
            )
        elif args.parent or args.name:
            raise LibraryError("--parent 与 --name 必须成对使用")
        else:
            # 走「项目/子节点/孙节点」路径：缺失的层级自动补建
            reference = args.path or ""
            parts = [part for part in str(reference).replace("\\", "/").split("/") if part]
            if not parts:
                raise LibraryError("请给出节点路径，例如 项目/结构设计")
            project = library.find_project(parts[0])
            if project is None:
                project = library.create_project(parts[0])
            roots = library.node_tree(project.id)
            current_id = roots[0].id if roots else None
            for part in parts[1:]:
                found = next(
                    (
                        item
                        for item in library.list_nodes(project.id)
                        if item.parent_id == current_id and item.name == part
                    ),
                    None,
                )
                if found is None:
                    found = library.create_node(
                        project.id, part, parent_id=current_id, kind=args.kind
                    )
                current_id = found.id
            node = library.get_node(current_id)
            assert node is not None

        node_path = library.node_path(node.id)

    printer.emit({"id": node.id, "path": node_path}, f"节点就绪：{node_path}（id={node.id}）")
    return 0


def cmd_tree(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        if args.project:
            project = library.find_project(args.project)
            if project is None:
                raise LibraryError(f"项目不存在：{args.project}")
            projects = [project]
        else:
            projects = library.list_projects()

        payload: list[dict[str, Any]] = []
        lines: list[str] = []
        for project in projects:
            entry = {"id": project.id, "name": project.name, "nodes": []}
            lines.append(f"■ {project.name}")
            for root_node in library.node_tree(project.id):
                # 根节点与项目同名，不再单独占一行；直接铺开它的文档与子节点
                for document in library.list_documents(root_node.id):
                    latest = library.latest_version(document.id)
                    tag = f"v{latest.seq}" if latest else "无版本"
                    state = REVIEW_STATE_LABELS.get(latest.review_state, "") if latest else ""
                    lines.append(f"  ● {document.title}  [{tag} {state}]".rstrip())
                for child in root_node.children:
                    _walk_tree(library, child, "  ", entry["nodes"], lines)
            payload.append(entry)

    printer.emit(payload, "\n".join(lines) if lines else "（库是空的）")
    return 0


def _walk_tree(library: Library, node, prefix: str, sink: list, lines: list[str]) -> None:
    documents = library.list_documents(node.id)
    marker = "▸" if documents else "·"
    lines.append(f"{prefix}{marker} {node.name}" + (f"  ({len(documents)} 文档)" if documents else ""))
    sink.append({"id": node.id, "name": node.name, "documents": [d.title for d in documents]})
    for document in documents:
        latest = library.latest_version(document.id)
        tag = f"v{latest.seq}" if latest else "无版本"
        state = REVIEW_STATE_LABELS.get(latest.review_state, "") if latest else ""
        lines.append(f"{prefix}    - {document.title}  [{tag} {state}]")
    for child in node.children:
        _walk_tree(library, child, prefix + "  ", sink[-1].setdefault("children", []), lines)


def cmd_add(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        if args.document:
            document = resolve_document(library, args.document)
            version, dedup = library.attach_files(
                document.id,
                args.files,
                note=args.note or "",
                author=args.author or "",
                as_primary=args.primary,
                file_mode=args.mode,
            )
            printer.emit(
                {"document_id": document.id, "version": version.seq, "deduplicated": dedup},
                f"{document.title}：{'内容未变化' if dedup else f'新增 v{version.seq}'}",
            )
            return 0

        if not args.node:
            raise LibraryError("请用 --node 指定目标节点（或改用 --document 给已有文档加版本）")
        node_id = resolve_node(library, args.node)
        results = []
        #: 压根没去归档的文件（不存在 / 目录里读不了）：这些是"跳过"
        skipped: list[dict[str, str]] = []
        #: 归档时报错的文件（库里拒绝、IO 出错）：这些是"失败"
        failed: list[dict[str, str]] = []
        for item in args.files:
            path = Path(item).expanduser()
            if not path.exists():
                print(f"跳过（不存在）：{path}", file=sys.stderr)
                skipped.append({"path": str(path), "reason": "不存在"})
                continue
            if path.is_dir():
                for child in sorted(path.rglob("*")):
                    if child.is_file():
                        try:
                            results.append(
                                library.archive_file(
                                    node_id, child, note=args.note or "",
                                    author=args.author or "", origin="import",
                                    file_mode=args.mode,
                                )
                            )
                        except (LibraryError, OSError) as error:
                            # OSError 也要兜住：文件在扫描之后被移走、磁盘只读……
                            # 一条文件的意外不该让整个批处理半路崩掉
                            print(f"跳过 {child.name}：{error}", file=sys.stderr)
                            failed.append({"path": str(child), "reason": str(error)})
                continue
            try:
                results.append(
                    library.archive_file(
                        node_id, path, title=args.title, note=args.note or "",
                        author=args.author or "", origin="manual",
                        file_mode=args.mode,
                    )
                )
            except (LibraryError, OSError) as error:
                print(f"跳过 {path.name}：{error}", file=sys.stderr)
                failed.append({"path": str(path), "reason": str(error)})

    payload = [
        {
            "document": item.document.title,
            "version": item.version.seq,
            "created": item.created_document,
            "deduplicated": item.deduplicated,
        }
        for item in results
    ]
    text = "\n".join(f"· {item.summary}" for item in results) or "没有归档任何文件"
    if printer.as_json:
        # 批处理要能一眼看出"哪些没进去"（--json 的调用方不该再去解析 stderr）
        printer.emit({"added": payload, "skipped": skipped + failed})
    else:
        printer.emit(payload, text)
    # 退出码：0=全都归档了；1=什么都没归档、或有文件被跳过/失败；2=部分失败（有error）
    # —— 原来无论如何都返回 0，批处理根本判断不出"什么都没归档"。
    if not results or skipped:
        return 1
    if failed:
        return 2
    return 0


def cmd_log(args: argparse.Namespace, printer: Printer) -> int:
    """打印一份文档的版本时间线（命令行版的流程视图）。"""
    with Library.open(args.library) as library:
        document = resolve_document(library, args.document)
        graph = build_version_graph(library, document.id)
        versions = library.version_history(document.id)

    if printer.as_json:
        printer.emit(graph.to_dict())
        return 0

    print(f"文档：{document.title}")
    print(f"位置：{graph.node_path}")
    span = f"（{graph.time_span_text}）" if graph.time_span_text else ""
    print(f"共 {len(versions)} 个版本{span}")
    print()
    nodes = {node.version_id: node for node in graph.nodes}
    for index, version in enumerate(versions):
        node = nodes.get(version.id)
        head = "●" if version.id == document.current_version_id else "○"
        state = REVIEW_STATE_LABELS.get(version.review_state, version.review_state)
        origin = ORIGIN_LABELS.get(version.origin, version.origin)
        files = len(version.files)
        print(
            f"{head} v{version.seq:<3} {to_local_str(version.created_at, '%Y-%m-%d %H:%M'):<17}"
            f"{state:<4} {origin:<6} {short_hash(version.sha256)}  {files} 个文件"
        )
        if node and node.change_summary:
            print(f"│     {node.change_summary}")
        if version.note:
            print(f"│     说明：{version.note}")
        if version.author:
            print(f"│     归档人：{version.author}")
        if index < len(versions) - 1:
            print("│")
    return 0


def cmd_project_graph(args: argparse.Namespace, printer: Printer) -> int:
    """打印项目全景图（文档 + 依赖关系）。"""
    from .core.projectgraph import build_project_graph

    with Library.open(args.library) as library:
        project = library.find_project(args.project) if args.project else None
        if project is None:
            projects = library.list_projects()
            if not projects:
                raise LibraryError("库里还没有项目")
            project = projects[0]
        graph = build_project_graph(library, project.id, layout=args.layout)

    if printer.as_json:
        printer.emit(graph.to_dict())
        return 0

    print(f"项目：{graph.project_name}（{graph.document_count} 份文档，"
          f"{graph.connected_count} 份有依赖关系，布局={graph.layout}）")
    if graph.cycle_count:
        print(f"注意：检测到 {graph.cycle_count} 条互相依赖（历史数据里的环）")
    print()

    by_id = {node.document_id: node for node in graph.nodes}
    columns: dict[int, list] = {}
    for node in graph.nodes:
        columns.setdefault(node.column, []).append(node)

    for column in graph.columns:
        members = sorted(columns.get(column.index, []), key=lambda item: item.row)
        if not members:
            continue
        print(f"■ {column.title}（{column.subtitle}）")
        for node in members:
            mark = "✓" if node.done else "·"
            deps = [
                edge for edge in graph.edges if edge.source_document_id == node.document_id
            ]
            print(f"  {mark} {node.title}  [v{node.latest_seq} · {node.version_count} 版 · "
                  f"{node.review_label}]")
            for edge in deps:
                target = by_id.get(edge.target_document_id)
                if target is not None:
                    print(f"      依赖 → {target.title}")
        print()
    if graph.isolated_count:
        print(f"（另有 {graph.isolated_count} 份文档没有任何依赖关系）")
    return 0


def cmd_docs(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        node_id = resolve_node(library, args.node)
        documents = library.list_documents(node_id, include_descendants=args.recursive)
        payload = []
        lines = []
        for document in documents:
            latest = library.latest_version(document.id)
            payload.append(
                {
                    "id": document.id,
                    "title": document.title,
                    "versions": document.version_count,
                    "latest": latest.seq if latest else None,
                    "updated_at": document.updated_at,
                }
            )
            lines.append(
                f"{document.id:<5}{document.title:<28}v{document.version_count:<4}"
                f"{to_local_str(document.updated_at)}"
            )
        printer.emit(payload, "\n".join(lines) if lines else "（该节点下还没有文档）")
    return 0


def cmd_diff(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        document = resolve_document(library, args.document)
        new_version = resolve_version(library, document, args.to)
        if args.from_version:
            old_version = resolve_version(library, document, args.from_version)
        else:
            history = library.version_history(document.id)
            older = [item for item in history if item.seq < new_version.seq]
            old_version = older[-1] if older else None
        if old_version is None:
            raise LibraryError("没有更早的版本可供比较")
        result = library.diff_versions(old_version.id, new_version.id)

    if printer.as_json:
        printer.emit(
            {
                "kind": result.kind,
                "summary": result.summary,
                "added": result.added,
                "removed": result.removed,
                "details": result.details,
                "unified": result.unified,
            }
        )
        return 0

    print(f"{document.title}：v{old_version.seq} → v{new_version.seq}")
    print(result.stat_text)
    for detail in result.details:
        print(f"· {detail}")
    if result.unified:
        print()
        print(result.unified)
    return 0


def cmd_export(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        document = resolve_document(library, args.document)
        version = resolve_version(library, document, args.version)
        written = library.export_version(version.id, args.dest)
    printer.emit([str(path) for path in written], "\n".join(str(path) for path in written))
    return 0


def cmd_state(args: argparse.Namespace, printer: Printer) -> int:
    """给事件打进度标记：进行中 / 已完成 / 搁置（个人管理，不是审批）。"""
    from .core.library import DOC_STATE_LABELS

    with Library.open(args.library) as library:
        document = resolve_document(library, args.document)
        library.set_document_state(document.id, args.state)
    label = DOC_STATE_LABELS.get(args.state, args.state)
    printer.emit(
        {"document_id": document.id, "title": document.title, "state": args.state},
        f"{document.title} 的状态已设为「{label}」",
    )
    return 0


def cmd_note(args: argparse.Namespace, printer: Printer) -> int:
    """给某一稿记一条说明（替代原来的"审阅意见"）。"""
    with Library.open(args.library) as library:
        document = resolve_document(library, args.document)
        version = resolve_version(library, document, args.version)
        library.add_version_note(version.id, args.text, author=args.author or "")
    printer.emit(
        {"document_id": document.id, "version": version.seq, "note": args.text},
        f"{document.title} v{version.seq} 已记录说明",
    )
    return 0


def cmd_stages(args: argparse.Namespace, printer: Printer) -> int:
    """列出事件的各个环节，以及每个环节各自的版本流程。"""
    from .core.versiongraph import build_stage_graph, list_stage_roles

    with Library.open(args.library) as library:
        document = resolve_document(library, args.document)
        summary = list_stage_roles(library, document.id)
        payload = []
        lines = [f"{document.title}（{document.version_count} 版 · {document.state_label}）"]
        for entry in summary:
            lines.append(
                f"\n■ {entry['label']}：{entry['files']} 条文件记录 / {entry['changes']} 个稿"
            )
            item = {"role": entry["role"], "label": entry["label"], "chain": []}
            if args.chains:
                graph = build_stage_graph(library, document.id, entry["role"])
                for node in graph.nodes:
                    lines.append(
                        f"   v{node.seq:<3}{to_local_str(node.created_at, '%Y-%m-%d'):<12}"
                        f"{node.change_summary:<12}{node.source_name[:56]}"
                    )
                    item["chain"].append(
                        {
                            "seq": node.seq,
                            "created_at": node.created_at,
                            "change": node.change_summary,
                            "files": node.source_name,
                        }
                    )
            payload.append(item)
    printer.emit(payload, "\n".join(lines))
    return 0


def cmd_find(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        hits = library.search(args.keyword, include_text=args.text, limit=args.limit)
    if printer.as_json:
        printer.emit(
            [
                {
                    "document_id": hit["document"].id,
                    "title": hit["document"].title,
                    "node_path": hit["node_path"],
                    "versions": hit["document"].version_count,
                }
                for hit in hits
            ]
        )
        return 0
    if not hits:
        print("没有匹配的文档")
        return 0
    for hit in hits:
        print(
            f"{hit['document'].id:<5}{hit['document'].title:<28}"
            f"{hit['document'].version_count} 版本  {hit['node_path']}"
        )
    return 0


def cmd_watch(args: argparse.Namespace, printer: Printer) -> int:
    """前台运行目录监视（自动化场景：投递即归档）。"""
    from .core.watcher import WATCHDOG_AVAILABLE, LibraryWatcher

    if not WATCHDOG_AVAILABLE:
        print("未安装 watchdog，无法使用目录监视：pip install watchdog", file=sys.stderr)
        return 2

    with Library.open(args.library) as library:
        node_id = resolve_node(library, args.node)
        node = library.get_node(node_id)
        assert node is not None
        if args.directory:
            library.add_watch_dir(node.project_id, node_id, args.directory)

    def on_outcome(outcome) -> None:
        print(outcome.summary, flush=True)

    watcher = LibraryWatcher(
        args.library, on_archived=on_outcome, archive_existing=args.scan_existing
    )
    if not watcher.start():
        print("没有可监视的目录规则", file=sys.stderr)
        return 1
    print(f"监视中（{len(watcher.rules)} 条规则），按 Ctrl+C 退出…")
    try:
        while True:
            import time

            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已停止监视")
    finally:
        watcher.stop()
    return 0


def cmd_file_mode(args: argparse.Namespace, printer: Printer) -> int:
    """查看 / 修改库的默认纳管方式。

    所以留这个不显眼的命令行口子：不带模式只打印当前值，给了模式就写进库。
    只影响**之后**的归档，已经登记好的文件行不动；``copy`` 只是复制，
    **不会移动或删除用户的原文件**。
    """
    with Library.open(args.library) as library:
        if args.mode:
            library.set_file_mode(args.mode)
        current = library.file_mode
    label = FILE_MODE_LABELS.get(current, current)
    text = f"归档方式：{current}（{label}）"
    if args.mode:
        text += f"\n已改为 {current}；之前登记过的文件行不受影响。"
    printer.emit({"file_mode": current}, text)
    return 0


def cmd_rebind(args: argparse.Namespace, printer: Printer) -> int:
    """为失效的引用重新绑定文件（文件被挪走/改名后用）。

    典型场景：你把某个工程目录整体改名了，或者把一份文件从 A 文件夹挪到了 B。
    工具的匹配顺序是「同名同大小」→「同名同指纹」→「指纹一致」，
    最后一种能处理"文件被改名"的情况。
    """
    from pathlib import Path as _Path

    with Library.open(args.library) as library:
        broken = library.broken_references(deep=args.deep)
        if not broken:
            printer.emit({"broken": 0, "matched": [], "unmatched": []},
                         "没有失效的引用，不需要重新绑定")
            return 0

        if not args.search:
            if printer.as_json:
                printer.emit({"broken": len(broken),
                              "items": [{k: v for k, v in item.items() if k != "sha256"}
                                        for item in broken]})
                return 1
            print(f"有 {len(broken)} 个失效引用：")
            for item in broken[:30]:
                print(f"  · [{item['title']} v{item['version_seq']}] {item['name']}"
                      f" —— {item.get('reason', '')}")
                print(f"    {item['path']}")
            if len(broken) > 30:
                print(f"  … 其余 {len(broken) - 30} 条省略")
            print("\n用 --search <目录> 在这些目录里找同名/同内容的文件并自动绑回。")
            return 1

        candidates: list[_Path] = []
        for root in args.search:
            base = _Path(root).expanduser()
            if base.is_file():
                candidates.append(base)
            elif base.is_dir():
                candidates.extend(
                    path for path in base.rglob("*") if path.is_file()
                )
            else:
                print(f"跳过（不存在）：{base}", file=sys.stderr)

        result = library.rebind_from_candidates(
            candidates, deep=args.deep, dry_run=not args.apply
        )

    if printer.as_json:
        printer.emit(
            {
                "broken": result["broken"],
                "dry_run": result["dry_run"],
                "matched": result["matched"],
                "unmatched": [
                    {k: v for k, v in item.items() if k != "sha256"}
                    for item in result["unmatched"]
                ],
            }
        )
        return 0

    verb = "将绑定" if result["dry_run"] else "已绑定"
    print(f"失效引用 {result['broken']} 个，{verb} {len(result['matched'])} 个：")
    for item in result["matched"]:
        suffix = "" if item["content_matched"] else "（内容与记录不同，已强制绑定）"
        print(f"  ✓ [{item['title']} v{item['version_seq']}] {item['name']} → {item['path']}{suffix}")
    if result["unmatched"]:
        print(f"\n没能匹配上 {len(result['unmatched'])} 个：")
        for item in result["unmatched"][:20]:
            print(f"  · {item['name']}（{item.get('reason', '没找到匹配的文件')}）")
    if result["dry_run"] and result["matched"]:
        print("\n（预演，未写入；确认无误后加 --apply 真正绑定）")
    return 0 if not result["unmatched"] else 1


def cmd_relocate(args: argparse.Namespace, printer: Printer) -> int:
    """资料目录整体搬家后，批量改写库里的引用路径。

    **默认只预演**（与 rebind / gc 一致）：改写的是全库的引用路径，
    手滑一次就得靠备份救 —— 想真写必须显式加 ``--apply``。
    """
    if args.apply and args.dry_run:
        raise LibraryError("--apply 与 --dry-run 不能同时给（--dry-run 本来就是默认行为）")
    with Library.open(args.library) as library:
        # 于是"号称默认预演"的这条命令**不带参数就直接改库**
        report = library.relocate_files(args.old, args.new, dry_run=not args.apply)
    if printer.as_json:
        printer.emit(report)
        return 0
    verb = "将改写" if report["dry_run"] else "已改写"
    print(f"{verb} {report['version_files']} 条版本文件引用、{report['blobs']} 条内容登记")
    for example in report["examples"]:
        print(f"  · {example}")
    if report["dry_run"]:
        print("（预演，未写入；加 --apply 才真正改写）")
    return 0


def cmd_verify(args: argparse.Namespace, printer: Printer) -> int:
    """检查版本文件是否还在、内容是否还是当初那份。

    引用模式（文件留在原处）下这是必备的自检：文件被移走/改名/就地改写后，
    库里只剩指纹，界面会标成「文件缺失」。
    """
    with Library.open(args.library) as library:
        report = library.verify_files(deep=args.deep)

    payload = {
        "total": report["total"],
        "ok": report["ok"],
        "missing": report["missing"][:200],
        "changed": report["changed"][:200],
    }
    if printer.as_json:
        printer.emit(payload)
        return 0 if not report["missing"] and not report["changed"] else 1

    print(f"检查了 {report['total']} 个版本文件：正常 {report['ok']}，"
          f"缺失 {len(report['missing'])}，内容已变 {len(report['changed'])}")
    # 大文件只比了大小、没做逐字节校验：不单独说一句的话，
    # 上面那个"正常 N 个"会被当成"全都验过了"
    if report.get("unverified"):
        print(f"未逐字节校验 {len(report['unverified'])} 个（文件很大，只比了大小）")
    for label, entries in (("缺失", report["missing"]), ("内容已变", report["changed"])):
        if not entries:
            continue
        print(f"\n{label}：")
        for entry in entries[:40]:
            print(f"  · [{entry['title']} v{entry['version_seq']}] {entry['name']}"
                  f" —— {entry.get('reason', '')}\n    {entry['path']}")
        if len(entries) > 40:
            print(f"  … 其余 {len(entries) - 40} 条省略")
    return 0 if not report["missing"] and not report["changed"] else 1


def cmd_legacy_info(args: argparse.Namespace, printer: Printer) -> int:
    """查看旧库（Symphytum/ToOak 的 data.db）里有什么，用于评估迁移。"""
    from .core.legacy import LegacyLibrary

    with LegacyLibrary(args.source) as legacy:
        collections = legacy.collections()
        files = legacy.files()
        on_disk = sum(1 for item in files.values() if item.path is not None)
        total_bytes = sum(item.size for item in files.values())

        payload = {
            "source": str(legacy.root),
            "collections": [
                {
                    "id": item.id,
                    "name": item.name,
                    "rows": item.row_count,
                    "fields": [f.name for f in item.fields],
                }
                for item in collections
            ],
            "files": {"registered": len(files), "on_disk": on_disk,
                      "bytes": total_bytes},
        }
        if printer.as_json:
            printer.emit(payload)
            return 0

        print(f"旧库：{legacy.root}")
        print(f"文件：登记 {len(files)} 个，磁盘上存在 {on_disk} 个，"
              f"合计 {total_bytes / 1024 / 1024 / 1024:.2f} GB")
        print()
        print(f"{'id':<4}{'集合（=项目）':<26}{'记录数':>6}  字段")
        for item in collections:
            names = "、".join(f.name for f in item.fields)
            print(f"{item.id:<4}{item.name:<26}{item.row_count:>6}  {names[:70]}")
        print()
        print("提示：用 `legacy-import` 迁移；`--dry-run` 可先看会导入多少东西。")
    return 0


def cmd_legacy_import(args: argparse.Namespace, printer: Printer) -> int:
    """把旧库迁移进新库。"""
    from .core.legacy import import_legacy

    names = None
    if args.collections:
        names = [item.strip() for item in args.collections.split(",") if item.strip()]

    last_report = {"stage": "", "done": 0, "total": 0}

    def on_progress(stage: str, done: int, total: int, message: str) -> None:
        if printer.as_json:
            return
        if stage == "files" and done != total and done % 100 != 0:
            return
        print(f"  [{stage}] {message}", flush=True)
        last_report.update(stage=stage, done=done, total=total)

    stats = import_legacy(
        args.source,
        args.library,
        collection_names=names,
        mode=args.mode,
        workers=args.workers,
        group_by_tag=not args.flat,
        include_files=not args.metadata_only,
        dry_run=args.dry_run,
        progress=on_progress,
    )
    if printer.as_json:
        printer.emit(
            {
                "projects": stats.projects,
                "nodes": stats.nodes,
                "documents": stats.documents,
                "versions": stats.versions,
                "tags": stats.tags,
                "links": stats.links,
                "files_stored": stats.files_stored,
                "files_reused": stats.files_reused,
                "files_missing": stats.files_missing,
                "files_referenced": stats.files_referenced,
                "bytes_stored": stats.bytes_stored,
                "bytes_referenced": stats.bytes_referenced,
                "file_mode": stats.file_mode,
                "elapsed_seconds": stats.elapsed_seconds,
                "dry_run": stats.dry_run,
                "errors": stats.errors[:20],
            }
        )
    else:
        print()
        print(stats.summary())
    return 0


def cmd_serve(args: argparse.Namespace, printer: Printer) -> int:
    from .core.api.server import serve

    serve(args.library, host=args.host, port=args.port, open_browser=args.open,
          enable_write=args.enable_write, allow_remote=args.allow_remote)
    return 0


def cmd_gc(args: argparse.Namespace, printer: Printer) -> int:
    with Library.open(args.library) as library:
        removed = library.gc_blobs(dry_run=not args.apply)
    if not removed:
        printer.emit([], "没有可回收的文件块")
        return 0
    verb = "已回收" if args.apply else "可回收（加 --apply 才真正删除）"
    text = f"{verb} {len(removed)} 个文件块：\n" + "\n".join(f"· {item}" for item in removed)
    printer.emit(removed, text)
    return 0


def cmd_materialize(args: argparse.Namespace, printer: Printer) -> int:
    """把残留的 reference（文件留在库外）记录复制进库，改成库内副本。"""
    with Library.open(args.library) as library:
        result = library.materialize_references(dry_run=not args.apply)
    lines = [
        f"reference 记录：{result['total']} 条",
        f"可进库：{result['materialized']} 条",
    ]
    if result["missing"]:
        lines.append(f"外部文件已不在：{len(result['missing'])} 条")
        lines += [f"· {item}" for item in result["missing"][:10]]
    verb = "已复制进库" if args.apply else "（加 --apply 才真正复制）"
    lines.insert(0, f"{verb} {result['materialized']} 条")
    text = "\n".join(lines)
    printer.emit(result, text)
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tooak",
        description=f"ToOak 文档管理器命令行工具（{version_text()}）",

        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--version", action="version", version=version_text())
    parser.add_argument(
        "-L", "--library", type=Path, default=default_library_path(),
        help=f"库目录（默认 {default_library_path()}，也可用环境变量 TOOAK_LIBRARY）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本处理")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="新建一个库")
    init.add_argument("--name", help="库名称")
    init.set_defaults(func=cmd_init)

    stats = subparsers.add_parser("stats", help="库统计信息")
    stats.set_defaults(func=cmd_stats)

    project_add = subparsers.add_parser("project-add", help="新建项目")
    project_add.add_argument("name")
    project_add.add_argument("--description", default="")
    project_add.set_defaults(func=cmd_project_add)

    projects = subparsers.add_parser("projects", help="列出项目")
    projects.set_defaults(func=cmd_projects)

    node_add = subparsers.add_parser("node-add", help="新建节点")
    node_add.add_argument("path", nargs="?", help="节点路径，如 项目/结构设计")
    node_add.add_argument("--parent", help="父节点引用（与 --name 搭配）")
    node_add.add_argument("--name", help="新节点名（与 --parent 搭配）")
    node_add.add_argument("--kind", default="node", help="节点类型标签")
    node_add.set_defaults(func=cmd_node_add)

    tree = subparsers.add_parser("tree", help="以树形展示项目/节点/文档")
    tree.add_argument("--project", help="仅展示指定项目")
    tree.set_defaults(func=cmd_tree)

    add = subparsers.add_parser("add", help="归档文件（同名即新版本）")
    add.add_argument("files", nargs="+", help="文件或目录（目录会递归归档）")
    add.add_argument("--node", help="目标节点，如 项目/结构设计（--document 时可省略）")
    add.add_argument("--title", help="强制指定标题（默认取文件名主名）")
    add.add_argument("--document", help="改为给已有文档加版本/附件")
    add.add_argument("--primary", action="store_true", help="替换主文件（配合 --document）")
    add.add_argument("--note", help="版本说明")
    add.add_argument("--author", help="归档人")
    add.add_argument(
        "--mode", choices=["reference", "copy", "link"],
        help="文件纳管方式（默认用库设置：新库是 copy=归档时复制进库；"
             "老库若当初设成 reference 则仍是 reference）",
    )
    add.set_defaults(func=cmd_add)

    log = subparsers.add_parser("log", help="查看文档的版本时间线")
    log.add_argument("document")
    log.set_defaults(func=cmd_log)

    project_graph = subparsers.add_parser(
        "project-graph", help="项目全景图：文档与它们之间的依赖关系"
    )
    project_graph.add_argument("--project", help="项目名（默认第一个项目）")
    project_graph.add_argument(
        "--layout", choices=["grouped", "layered"], default="grouped",
        help="grouped=按节点分列（默认）；layered=按依赖分层",
    )
    project_graph.set_defaults(func=cmd_project_graph)

    docs = subparsers.add_parser("docs", help="列出节点下的文档")
    docs.add_argument("node")
    docs.add_argument("--recursive", action="store_true", help="包含子节点")
    docs.set_defaults(func=cmd_docs)

    diff = subparsers.add_parser("diff", help="比较两个版本")
    diff.add_argument("document")
    diff.add_argument("--from", dest="from_version", help="旧版本，如 v1（默认上一个版本）")
    diff.add_argument("--to", dest="to", default="latest", help="新版本，默认 latest")
    diff.set_defaults(func=cmd_diff)

    export = subparsers.add_parser("export", help="导出某个版本（保留原始文件名）")
    export.add_argument("document")
    export.add_argument("--version", default="latest")
    export.add_argument("--dest", help="导出目录")
    export.set_defaults(func=cmd_export)

    state = subparsers.add_parser(
        "state", help="给事件打进度标记（进行中/已完成/搁置，个人管理不是审批）"
    )
    state.add_argument("document")
    state.add_argument(
        "--set", dest="state", required=True, choices=["active", "done", "on_hold"],
        help="active=进行中 done=已完成 on_hold=搁置",
    )
    state.set_defaults(func=cmd_state)

    note = subparsers.add_parser("note", help="给某一稿记一条说明")
    note.add_argument("document")
    note.add_argument("text")
    note.add_argument("--version", default="latest")
    note.add_argument("--author", help="记录人（可选）")
    note.set_defaults(func=cmd_note)

    stages = subparsers.add_parser(
        "stages", help="列出事件的各个环节及其各自的版本流程"
    )
    stages.add_argument("document")
    stages.add_argument("--chains", action="store_true", help="连每个环节的稿一起列出")
    stages.set_defaults(func=cmd_stages)

    find = subparsers.add_parser("find", help="按标题/文件名检索")
    find.add_argument("keyword")
    find.add_argument("--text", action="store_true", help="连同已抽取的正文一起检索")
    find.add_argument("--limit", type=int, default=50)
    find.set_defaults(func=cmd_find)

    watch = subparsers.add_parser("watch", help="前台运行目录监视（投递即归档）")
    watch.add_argument("--node", required=True, help="归档到哪个节点")
    watch.add_argument("--directory", help="新增一条监视目录规则")
    watch.add_argument("--scan-existing", action="store_true", help="启动时先补扫已有文件")
    watch.set_defaults(func=cmd_watch)

    serve = subparsers.add_parser("serve", help="启动 HTTP API / Web 审阅界面")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    serve.add_argument(
        "--enable-write", action="store_true",
        help="启用写接口（改进度状态 / 记说明 / 打定稿标记）—— 默认只读，"
             "网页查看端不需要凭据就能读写同一份库，所以写接口要显式开",
    )
    serve.add_argument(
        "--allow-remote", action="store_true",
        help="允许绑到非回环地址（局域网可访问）。默认拒绝：那等于把整库暴露出去",
    )
    serve.set_defaults(func=cmd_serve)

    legacy_info = subparsers.add_parser(
        "legacy-info", help="查看旧版 ToOak/Symphytum 库里有什么（迁移前先看这个）"
    )
    legacy_info.add_argument("--from", dest="source", required=True,
                             help="旧库目录（里面有 data.db 与 files/）")
    legacy_info.set_defaults(func=cmd_legacy_info)

    legacy_import = subparsers.add_parser(
        "legacy-import", help="把旧库迁移进新库（保持版本时间线与文件角色）"
    )
    legacy_import.add_argument("--from", dest="source", required=True, help="旧库目录")
    legacy_import.add_argument("--collections", help="只导入这些集合，逗号分隔（默认全部）")
    legacy_import.add_argument(
        "--mode", choices=["reference", "copy", "link"], default="copy",
        help="文件纳管方式：copy=复制进新库（默认）；"
             "reference=留在原处只登记引用（最省空间，但库外文件动不得）；"
             "link=硬链接进新库（同盘零拷贝）",
    )
    legacy_import.add_argument("--workers", type=int, default=8, help="并行处理文件的线程数")
    legacy_import.add_argument("--flat", action="store_true",
                               help="不按「标签」建子节点，所有文档都挂在项目根下")
    legacy_import.add_argument("--metadata-only", action="store_true",
                               help="只导入结构与文本，跳过文件（快速预演）")
    legacy_import.add_argument("--dry-run", action="store_true", help="只统计不写库")
    legacy_import.set_defaults(func=cmd_legacy_import)

    relocate = subparsers.add_parser(
        "relocate", help="资料目录搬家后批量改写引用路径（引用模式记的是绝对路径）"
    )
    relocate.add_argument("--old", required=True, help="原来的目录前缀")
    relocate.add_argument("--new", required=True, help="新的目录前缀")
    relocate.add_argument("--apply", action="store_true", help="真正改写（默认只预演）")
    relocate.add_argument("--dry-run", action="store_true", help="只预演（默认行为）")
    relocate.set_defaults(func=cmd_relocate)

    rebind = subparsers.add_parser(
        "rebind", help="为失效引用重新绑定文件（文件被挪走/改名后）"
    )
    rebind.add_argument("--search", nargs="*", help="在这些目录/文件里找匹配的文件")
    rebind.add_argument("--deep", action="store_true", help="按内容指纹匹配（能处理改名）")
    rebind.add_argument("--apply", action="store_true",
                        help="真正绑定（不给 --apply 时只预演）")
    rebind.set_defaults(func=cmd_rebind)

    verify = subparsers.add_parser(
        "verify", help="检查版本文件是否还在（引用模式下文件留在原处，需要定期自检）"
    )
    verify.add_argument("--deep", action="store_true",
                        help="重新计算 sha256 对比内容（大库很慢，默认只查存在性与大小）")
    verify.set_defaults(func=cmd_verify)

    gc = subparsers.add_parser("gc", help="回收无引用的文件块")
    gc.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    gc.set_defaults(func=cmd_gc)

    materialize = subparsers.add_parser(
        "materialize", help="把残留的引用模式文件复制进库（原文件不动）"
    )
    materialize.add_argument("--apply", action="store_true", help="真正复制（默认只预览）")
    materialize.set_defaults(func=cmd_materialize)

    # 留一个不显眼的命令行口子，方便老库保持 reference 或事后改回来
    file_mode = subparsers.add_parser(
        "file-mode", help="查看 / 修改库的默认归档方式（copy=归档时复制进库，默认）"
    )
    file_mode.add_argument(
        "mode", nargs="?", choices=["copy", "link", "reference"],
        help="不给就只打印当前值；reference=文件留在原处只登记引用（老库的历史默认）",
    )
    file_mode.set_defaults(func=cmd_file_mode)

    return parser


def _force_utf8_streams() -> None:
    """把标准输出/错误换成 UTF-8。

    中文 Windows 控制台默认是 cp936（GBK），而 ``tree`` 的 ``▸``、``rebind`` 的 ``✓``
    都不在 GBK 里 —— 实测 ``PYTHONIOENCODING=gbk`` 下命令打印到一半就
    以 UnicodeEncodeError + exit 1 收场。errors="replace" 兜住个别编码不了的字符，
    宁可显示成 `?` 也不能让一条只读命令崩掉。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:          # 被重定向成非文本流（少数嵌入环境）
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    printer = Printer(as_json=getattr(args, "json", False))

    try:
        # init 原来在 try 之外：建库失败（比如目录已存在）会直接把 traceback 甩给用户，
        # 而不是一句"错误：…" + 退出码 1
        return int(args.func(args, printer) or 0)
    except (LibraryError, OSError, sqlite3.Error, UnicodeEncodeError) as error:
        # OSError 覆盖 FileNotFoundError / FileExistsError / PermissionError：
        # export 目标已存在之类的常见失败原来会甩一整段 traceback
        print(f"错误：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

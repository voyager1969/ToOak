"""HTTP JSON API + 内置 Web 审阅页。

定位
----
桌面端负责「归档」（拖拽、就地编辑、目录监视），Web 端负责「审阅确认」——
别人在浏览器里看版本时间线、比较两个版本、点「确认此版本」。
两边**共用同一个内核**：同一个库目录、同一套 Library 语义、同一份版本图数据，
因此不存在「同步冲突」这种问题（旧版 Symphytum 里那一整套 sync 复杂度在这里不需要）。

实现
----
只用标准库（http.server），不引入 FastAPI/Flask 等依赖：
本服务是单机本地服务，并发量极低，够用且部署更省事。
每个请求开一个独立的 Library 会话（独立 SQLite 连接），天然线程安全。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import socket
import threading
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse, urlsplit

from ..library import (
    DOC_STATE_LABELS,
    REVIEW_STATE_LABELS,
    ROLE_LABELS,
    Library,
    LibraryError,
    Version,
)
from ..util import human_size, short_hash, to_local_str
from ..projectgraph import build_project_graph
from ..versiongraph import build_stage_graph, build_version_graph, list_stage_roles
from ...resource import resource_path

__all__ = ["serve", "create_server", "ToakAPIServer"]

#: 审阅页 HTML —— 用 resource_path：打包成 exe 后它在 sys._MEIPASS 里
_HTML_PATH = resource_path("tooak", "core", "api", "review.html")

#: 流式下载的分块大小（1MB：够摊平 syscall 开销，又不会在内存里堆整份文件）
_CHUNK = 1 << 20


@dataclass
class Response:
    """统一响应对象：JSON、二进制下载或文本页面。"""

    payload: Any = None
    status: int = 200
    content_type: str = "application/json; charset=utf-8"
    raw: bytes | None = None
    headers: dict[str, str] = field(default_factory=dict)
    #: 直接来自磁盘的文件：**流式**发（``raw`` 是"整份读进内存"，大文件会顶爆内存）
    file: Path | None = None

    def body(self) -> bytes:
        if self.raw is not None:
            return self.raw
        return json.dumps(self.payload, ensure_ascii=False, default=str).encode("utf-8")


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------

#: 只读接口：默认就注册（查看端要用的就是这些）
_ROUTES: list[tuple[re.Pattern[str], tuple[str, ...], Callable]] = []
#: 写接口：**默认不注册**，``serve --enable-write`` 才挂上去。
#: 本服务是单机无凭据的：任何能打开这个端口的人都能改你的库，
#: 而"看版本"和"改状态"根本不是一回事，不该顺带一起开放。
_WRITE_ROUTES: list[tuple[re.Pattern[str], tuple[str, ...], Callable]] = []


def route(pattern: str, methods: tuple[str, ...] = ("GET",), *,
          write: bool = False) -> Callable:
    compiled = re.compile(pattern)

    def decorator(func: Callable) -> Callable:
        ( _WRITE_ROUTES if write else _ROUTES).append((compiled, methods, func))
        return func

    return decorator


def _document_payload(library: Library, document_id: int) -> dict[str, Any]:
    document = library.get_document(document_id)
    if document is None:
        raise LibraryError(f"文档不存在：{document_id}")
    node = library.get_node(document.node_id)
    project = library.get_project(node.project_id) if node else None
    latest = library.latest_version(document.id)
    return {
        "id": document.id,
        "title": document.title,
        "kind": document.kind,
        "status": document.status,
        "description": document.description,
        "version_count": document.version_count,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "node_id": document.node_id,
        "node_path": library.node_path(document.node_id),
        "project_name": project.name if project else "",
        "state": document.state,
        "state_label": DOC_STATE_LABELS.get(document.state, document.state),
        "people": document.people,
        "progress": document.progress,
        "stages": list_stage_roles(library, document.id),
        "tags": library.document_tags(document.id),
        "latest": _version_payload(library, latest) if latest else None,
    }


def _version_payload(library: Library, version: Version | None) -> dict[str, Any] | None:
    if version is None:
        return None
    return {
        "id": version.id,
        "seq": version.seq,
        "label": version.label,
        "created_at": version.created_at,
        "time_text": to_local_str(version.created_at),
        "source_name": version.source_name,
        "sha256": version.sha256,
        "short_sha": short_hash(version.sha256),
        "size": version.size,
        "size_text": human_size(version.size),
        "mime": version.mime,
        "author": version.author,
        "note": version.note,
        "origin": version.origin,
        "review_state": version.review_state,
        "review_label": REVIEW_STATE_LABELS.get(version.review_state, version.review_state),
        "parent_version_id": version.parent_version_id,
        "files": [
            {
                "id": item.id,
                "name": item.name,
                "role": item.role,
                "role_label": ROLE_LABELS.get(item.role, item.role),
                "size": item.size,
                "size_text": human_size(item.size),
                "sha256": item.sha256,
                "short_sha": item.short_sha,
                "mime": item.mime,
            }
            for item in version.files
        ],
        "reviews": library.list_reviews(version.id),
    }


# ---------------------------------------------------------------------------
# 只读接口
# ---------------------------------------------------------------------------


@route(r"/api/health")
def api_health(root: Path, params: dict, query: dict, body: dict) -> Response:
    with Library.open(root) as library:
        stats = library.stats()
    return Response({"ok": True, "library": stats["name"], "counts": stats["counts"]})


@route(r"/api/projects")
def api_projects(root: Path, params: dict, query: dict, body: dict) -> Response:
    with Library.open(root) as library:
        projects = library.list_projects()
        return Response(
            [
                {
                    "id": project.id,
                    "name": project.name,
                    "description": project.description,
                    "color": project.color,
                    "node_count": project.node_count,
                    "document_count": project.document_count,
                    "version_count": project.version_count,
                }
                for project in projects
            ]
        )


@route(r"/api/projects/(?P<project_id>\d+)/tree")
def api_project_tree(root: Path, params: dict, query: dict, body: dict) -> Response:
    project_id = int(params["project_id"])
    with Library.open(root) as library:
        project = library.get_project(project_id)
        if project is None:
            raise LibraryError(f"项目不存在：{project_id}")

        def node_payload(node) -> dict[str, Any]:
            return {
                "id": node.id,
                "name": node.name,
                "kind": node.kind,
                "documents": [
                    {
                        "id": document.id,
                        "title": document.title,
                        "version_count": document.version_count,
                        "latest_review_state": document.latest_review_state,
                        "updated_at": document.updated_at,
                    }
                    for document in library.list_documents(node.id)
                ],
                "children": [node_payload(child) for child in node.children],
            }

        return Response(
            {
                "id": project.id,
                "name": project.name,
                "nodes": [node_payload(root_node) for root_node in library.node_tree(project_id)],
            }
        )


@route(r"/api/projects/(?P<project_id>\d+)/graph")
def api_project_graph(root: Path, params: dict, query: dict, body: dict) -> Response:
    """项目全景图：文档 + 依赖关系（Web 端可直接用同一份数据画 SVG）。

    ``?layout=grouped|layered`` 选择「按节点分列」或「按依赖分层」。
    """
    project_id = int(params["project_id"])
    layout = query.get("layout", "grouped")
    with Library.open(root) as library:
        if library.get_project(project_id) is None:
            raise LibraryError(f"项目不存在：{project_id}")
        graph = build_project_graph(library, project_id, layout=layout)
        return Response(graph.to_dict())


@route(r"/api/documents/(?P<document_id>\d+)")
def api_document(root: Path, params: dict, query: dict, body: dict) -> Response:
    with Library.open(root) as library:
        return Response(_document_payload(library, int(params["document_id"])))


@route(r"/api/documents/(?P<document_id>\d+)/versions")
def api_document_versions(root: Path, params: dict, query: dict, body: dict) -> Response:
    document_id = int(params["document_id"])
    with Library.open(root) as library:
        versions = library.version_history(document_id)
        return Response([_version_payload(library, version) for version in versions])


@route(r"/api/documents/(?P<document_id>\d+)/stages")
def api_document_stages(root: Path, params: dict, query: dict, body: dict) -> Response:
    """列出事件的各个环节（输入/分析/输出…）及各自的稿数。"""
    document_id = int(params["document_id"])
    with Library.open(root) as library:
        return Response(list_stage_roles(library, document_id))


@route(r"/api/documents/(?P<document_id>\d+)/stage-graph")
def api_document_stage_graph(root: Path, params: dict, query: dict, body: dict) -> Response:
    """某个**环节**的版本流程：这个环节在不同时期分别是哪些文件。"""
    document_id = int(params["document_id"])
    role = query.get("role", "")
    if not role:
        raise LibraryError("请给出 role（环节），例如 ?role=output")
    with Library.open(root) as library:
        graph = build_stage_graph(library, document_id, role)
        return Response(graph.to_dict())


@route(r"/api/documents/(?P<document_id>\d+)/stage-diff")
def api_stage_diff(root: Path, params: dict, query: dict, body: dict) -> Response:
    """比较某个**环节**在相邻两稿里的内容（?a=&b=&role=）。"""
    old_id, new_id = query.get("a"), query.get("b")
    role = query.get("role", "")
    if not old_id or not new_id or not role:
        raise LibraryError("请给出 a（旧稿版本 id）、b（新稿版本 id）与 role（环节）")
    document_id = int(params["document_id"])
    with Library.open(root) as library:
        # URL 里的 document_id 必须和这两稿真的对得上：不校验的话
        # /api/documents/999999/stage-diff?a=..&b=.. 照样 200 ——
        # 前端按"这个文档的这一稿"来渲染，拿到别的文档的差异只会张冠李戴
        for version_id in (old_id, new_id):
            version = library.get_version(int(version_id))
            if version is None:
                raise LibraryError(f"版本不存在：{version_id}")
            if version.document_id != document_id:
                raise LibraryError(
                    f"版本 {version_id} 不属于文档 {document_id}（它属于文档 "
                    f"{version.document_id}）"
                )
        result = library.diff_stage(int(old_id), int(new_id), role)
    return Response(
        {
            "kind": result.kind,
            "summary": result.summary,
            "added": result.added,
            "removed": result.removed,
            "details": result.details,
            "unified": result.unified,
        }
    )


@route(r"/api/documents/(?P<document_id>\d+)/state", methods=("POST",), write=True)
def api_document_state(root: Path, params: dict, query: dict, body: dict) -> Response:
    """打进度标记：active 进行中 / done 已完成 / on_hold 搁置（个人管理，不是审批）。"""
    state = str(body.get("state", "")).strip()
    if state not in DOC_STATE_LABELS:
        raise LibraryError(f"未知状态：{state}（可选 {list(DOC_STATE_LABELS)}）")
    document_id = int(params["document_id"])
    with Library.open(root) as library:
        library.set_document_state(document_id, state)
        return Response(_document_payload(library, document_id))


@route(r"/api/versions/(?P<version_id>\d+)/note", methods=("POST",), write=True)
def api_version_note(root: Path, params: dict, query: dict, body: dict) -> Response:
    """给某一稿记一条说明（替代原来的"审阅意见"）。"""
    note = str(body.get("note", "")).strip()
    if not note:
        raise LibraryError("说明不能为空")
    with Library.open(root) as library:
        version_id = int(params["version_id"])
        library.add_version_note(version_id, note, author=str(body.get("author", "")))
        return Response(_version_payload(library, library.get_version(version_id)))


@route(r"/api/documents/(?P<document_id>\d+)/graph")
def api_document_graph(root: Path, params: dict, query: dict, body: dict) -> Response:
    document_id = int(params["document_id"])
    with Library.open(root) as library:
        graph = build_version_graph(library, document_id)
        return Response(graph.to_dict())


@route(r"/api/versions/(?P<version_id>\d+)")
def api_version(root: Path, params: dict, query: dict, body: dict) -> Response:
    with Library.open(root) as library:
        version = library.get_version(int(params["version_id"]))
        if version is None:
            raise LibraryError(f"版本不存在：{params['version_id']}")
        payload = _version_payload(library, version)
        assert payload is not None
        payload["document"] = _document_payload(library, version.document_id)
        profile = library.describe_version(version.id)["profile"]
        payload["profile"] = (
            {
                "name": profile.name,
                "ext": profile.ext,
                "size_text": profile.size_text,
                "mime": profile.mime,
                "detail": profile.detail,
            }
            if profile
            else None
        )
        return Response(payload)


@route(r"/api/versions/(?P<version_id>\d+)/text")
def api_version_text(root: Path, params: dict, query: dict, body: dict) -> Response:
    with Library.open(root) as library:
        text = library.get_version_text(int(params["version_id"]))
        return Response({"text": text, "extractable": text is not None})


@route(r"/api/versions/(?P<version_id>\d+)/content")
def api_version_content(root: Path, params: dict, query: dict, body: dict) -> Response:
    """下载某版本的主文件（用**原始文件名**，URL 编码后放在 Content-Disposition）。

    这里**不把文件读进内存**：引用模式下一个主文件可能是 2.9 GB，
    ``read_bytes`` + 一次性写 socket 会先把整份塞进内存（还要再复制一份进 socket 缓冲），
    大库上直接就是 MemoryError。交给 handler 流式发送（并支持 Range 断点/分段）。
    """
    with Library.open(root) as library:
        version = library.get_version(int(params["version_id"]))
        if version is None:
            raise LibraryError(f"版本不存在：{params['version_id']}")
        primary = version.primary_file
        if primary is None:
            raise LibraryError("该版本没有主文件")
        # 用 file_path()（引用模式 = 那个文件当初登记的路径；库内 = blob 路径）：
        # 直接读 blobs.rel_path 会读到"**最先**登记这份内容的那条路径"——
        # 文件改名/搬走并重新绑定之后，要么读出别人那份，要么 404
        path = library.file_path(primary)
        if path is None or not path.is_file():
            raise LibraryError(f"这一稿的主文件已不在原处：{primary.name}")
        filename = quote(primary.name)
        return Response(
            file=path,
            content_type=primary.mime or "application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
                "X-ToOak-Filename": filename,
            },
        )


@route(r"/api/diff")
def api_diff(root: Path, params: dict, query: dict, body: dict) -> Response:
    old_id = query.get("a") or query.get("from")
    new_id = query.get("b") or query.get("to")
    if not old_id or not new_id:
        raise LibraryError("请给出 a（旧版本 id）与 b（新版本 id）")
    with Library.open(root) as library:
        result = library.diff_versions(int(old_id), int(new_id))
    return Response(
        {
            "kind": result.kind,
            "summary": result.summary,
            "added": result.added,
            "removed": result.removed,
            "details": result.details,
            "unified": result.unified,
        }
    )


@route(r"/api/search")
def api_search(root: Path, params: dict, query: dict, body: dict) -> Response:
    keyword = query.get("q", "")
    with Library.open(root) as library:
        hits = library.search(keyword, include_text=query.get("text") == "1")
        return Response(
            [
                {
                    "document_id": hit["document"].id,
                    "title": hit["document"].title,
                    "node_path": hit["node_path"],
                    "version_count": hit["document"].version_count,
                }
                for hit in hits
            ]
        )


# ---------------------------------------------------------------------------
# 写接口（审阅确认）
# ---------------------------------------------------------------------------


@route(r"/api/versions/(?P<version_id>\d+)/review", methods=("POST",), write=True)
def api_review(root: Path, params: dict, query: dict, body: dict) -> Response:
    state = str(body.get("state", "")).strip()
    if state not in REVIEW_STATE_LABELS:
        raise LibraryError(f"非法审阅状态：{state}（可选 {list(REVIEW_STATE_LABELS)}）")
    with Library.open(root) as library:
        version_id = int(params["version_id"])
        library.set_review_state(
            version_id,
            state,
            reviewer=str(body.get("reviewer", "")).strip(),
            comment=str(body.get("comment", "")).strip(),
        )
        version = library.get_version(version_id)
        return Response(_version_payload(library, version))


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------


@route(r"/")
@route(r"/review")
def page_review(root: Path, params: dict, query: dict, body: dict) -> Response:
    html = _HTML_PATH.read_text(encoding="utf-8") if _HTML_PATH.exists() else "<h1>审阅页缺失</h1>"
    return Response(raw=html.encode("utf-8"), content_type="text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in _LOOPBACK_HOSTS


def _local_host_names() -> set[str]:
    """本机的名字与网卡地址（远程模式下用来填 Host 白名单）。"""
    names = {"localhost"}
    try:
        names.add(socket.gethostname().lower())
        names.add(socket.getfqdn().lower())
        for info in socket.getaddrinfo(socket.gethostname(), None):
            names.add(str(info[4][0]).lower())
    except OSError:
        pass
    return names


#: Range 解析的结果标记：``_BAD_RANGE`` = 语法合法但范围不满足（→ 416）
_BAD_RANGE = object()
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _parse_range(header: str | None, size: int):
    """解析 ``Range: bytes=a-b``。

    返回 ``(start, end)``（闭区间）、``None``（没有/不支持 → 整份发）或 ``_BAD_RANGE``。
    只认单区间：多区间要 multipart 响应，本地查看端用不上。
    """
    if not header:
        return None
    match = _RANGE_RE.fullmatch(header.strip())
    if match is None:
        return None                     # 不认识的写法就当没提，整份发更安全
    raw_start, raw_end = match.group(1), match.group(2)
    if not raw_start and not raw_end:
        return _BAD_RANGE
    if not raw_start:                   # bytes=-N：最后 N 个字节
        length = int(raw_end)
        if length <= 0:
            return _BAD_RANGE
        return max(size - length, 0), size - 1
    start = int(raw_start)
    if start >= size:
        return _BAD_RANGE
    end = int(raw_end) if raw_end else size - 1
    if end < start:
        return _BAD_RANGE
    return start, min(end, size - 1)


class ToakAPIServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], library_root: Path, *,
                 enable_write: bool = False, allow_remote: bool = False):
        super().__init__(address, _RequestHandler)
        self.library_root = Path(library_root)
        #: 是否挂上写接口（默认不挂：查看端只需要读）
        self.enable_write = bool(enable_write)
        #: 用户是否明确同意绑到非回环地址
        self.allow_remote = bool(allow_remote)
        self.host_names = _local_host_names() if self.allow_remote else set()

    def routes(self) -> list[tuple[re.Pattern[str], tuple[str, ...], Callable]]:
        return _ROUTES + (_WRITE_ROUTES if self.enable_write else [])


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "ToOak/0.1"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------------- 分发

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._respond(Response(payload={"ok": True}))

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = {key: value[0] for key, value in parse_qs(parsed.query).items()}

        if not self._host_allowed():
            # DNS rebinding：恶意网页把自己的域名指到 127.0.0.1，浏览器就把请求
            # 送进这个服务（浏览器不设防，只有服务端能挡）。Host 不是我们自己的名字就拒。
            self._respond(Response({"error": "Host 不被允许"}, status=403))
            return

        routes = self.server.routes()
        for pattern, methods, func in routes:
            match = pattern.fullmatch(path)
            if not match or method not in methods:
                continue
            try:
                body = self._read_json() if method == "POST" else {}
                response = func(self.server.library_root, match.groupdict(), query, body)
            except LibraryError as error:
                self._respond(Response({"error": str(error)}, status=400))
                return
            except (ValueError, TypeError, KeyError) as error:
                # int("abc") 这类**参数**问题：是客户端给错了，不是服务端坏了
                logging.warning("参数错误 %s %s: %s", method, path, error)
                self._respond(Response({"error": "请求参数不合法"}, status=400))
                return
            except Exception:  # 兜底：绝不让服务因单个请求崩掉
                # 只回固定文案：以前回的是 f"{type(error).__name__}: {error}"，
                # 而 OSError 的消息里就带着**绝对路径**（"…/library/blobs/xx"），
                # 等于把用户磁盘结构白送给任何一个能连上端口的人
                logging.exception("接口内部错误 %s %s", method, path)
                self._respond(Response({"error": "服务器内部错误"}, status=500))
                return
            self._respond(response)
            return

        if self._matches_write_route(path, method):
            # 路径存在但写接口没开：405（"这个资源不允许这个操作"）比 404 更能说明问题
            self._respond(Response(
                {"error": "写接口未启用（用 serve --enable-write 启动才挂写路由）"},
                status=405,
            ))
            return

        self._respond(Response({"error": f"未知接口：{method} {path}"}, status=404))

    def _matches_write_route(self, path: str, method: str) -> bool:
        return any(pattern.fullmatch(path) and method in methods
                   for pattern, methods, _func in _WRITE_ROUTES)

    def _host_allowed(self) -> bool:
        """Host 头白名单：只认我们自己的名字/地址。"""
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return True                    # HTTP/1.0 客户端可能不带 Host
        host = (urlsplit("//" + raw).hostname or "").lower()
        if not host:
            return False
        bound = str(self.server.server_address[0]).lower()
        allowed = {"127.0.0.1", "localhost", "::1"}
        if bound not in ("0.0.0.0", "::", ""):
            allowed.add(bound)
        if self.server.allow_remote:
            # 显式同意远程访问：按 IP/机器名访问是常态，白名单里补上本机地址，
            # 免得"开了远程却全被 403 挡掉"
            allowed |= self.server.host_names
        return host in allowed

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _respond(self, response: Response) -> None:
        if response.file is not None:
            self._respond_file(response)
            return
        payload = response.body()
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def _respond_file(self, response: Response) -> None:
        """把磁盘上的文件**流式**发给客户端（一次只占一个 1MB 块）。"""
        path = Path(response.file)
        try:
            size = path.stat().st_size
        except OSError:
            self._respond(Response({"error": "文件已不在原处"}, status=404))
            return

        start, end, status = 0, size - 1, 200
        wanted = _parse_range(self.headers.get("Range"), size)
        if wanted is _BAD_RANGE:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if wanted is not None:
            start, end = wanted
            status = 206

        length = max(end - start + 1, 0)
        self.send_response(status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(length))
        # 声明支持 Range：播放器/下载器据此断点续传，不必整份重下
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Cache-Control", "no-store")
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command == "HEAD":
            return
        with path.open("rb") as handle:
            if status == 206:
                handle.seek(start)
            if start == 0 and length == size:
                shutil.copyfileobj(handle, self.wfile, _CHUNK)     # 整份：直接抄
                return
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(_CHUNK, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def log_message(self, fmt: str, *args) -> None:  # 默认日志太吵，收敛成一行
        if self.path.startswith("/api/"):
            print(f"[api] {self.address_string()} {fmt % args}")


def create_server(
    library_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    enable_write: bool = False,
    allow_remote: bool = False,
) -> ToakAPIServer:
    """创建（但不启动）服务器，便于测试与嵌入。

    :param enable_write: 是否挂上写接口（默认**不挂**：查看端只需要读，
        ``POST /api/...`` 会得到 405）。
    :param allow_remote: 是否允许绑到非回环地址。``--host 0.0.0.0`` 会把整库
        暴露到局域网（没有凭据、没有 TLS），所以要显式同意。
    """
    if not _is_loopback(host) and not allow_remote:
        raise LibraryError(
            f"拒绝绑定到非回环地址 {host}：那会把整库暴露给同网段的任何人。"
            "确实需要时加 serve --allow-remote"
        )
    root = Path(library_root).expanduser().resolve()
    Library.open(root).close()  # 提前校验库是否存在，避免起来后才报错
    return ToakAPIServer((host, port), root, enable_write=enable_write,
                         allow_remote=allow_remote)


def serve(
    library_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    *,
    enable_write: bool = False,
    allow_remote: bool = False,
) -> None:
    server = create_server(library_root, host, port, enable_write=enable_write,
                           allow_remote=allow_remote)
    url = f"http://{host}:{port}/"
    print(f"ToOak Web 审阅端：{url}")
    print(f"库：{server.library_root}")
    print("模式：" + ("可写（--enable-write）" if server.enable_write else "只读"))
    if not _is_loopback(host):
        print(f"⚠ 正在监听 {host}：同网的机器都能访问这个库（--allow-remote 是你显式开的）")
    print("按 Ctrl+C 停止…")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.shutdown()
        server.server_close()

"""投递式自动归档：监视目录里出现的文件，自动变成文档的新版本。

为什么需要它
------------
文档管理器最痛的问题是「改了一份文档，管理器不知道」。桌面端用一个监视器解决：
把文件丢进被监视的目录（或者直接在被监视目录里改文档），
内核比对内容哈希后自动生成新版本 —— 内容没变则不产生噪音版本。

线程模型
--------
watchdog 的事件回调跑在它自己的线程里，归档也在后台线程完成，
每个后台任务使用**独立的 Library 会话**（独立 SQLite 连接），
回调结果通过 ``on_archived`` 交给调用方，由调用方负责切回 UI 线程。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .library import ArchiveResult, Library
from .util import is_subpath

try:  # watchdog 是可选的：没装时监视功能关闭，其余功能不受影响
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    WATCHDOG_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object  # type: ignore[assignment,misc]
    Observer = None  # type: ignore[assignment]

__all__ = ["WatchOutcome", "LibraryWatcher", "WATCHDOG_AVAILABLE"]

#: 临时文件、编辑器交换文件等一律忽略，否则会归档出一堆垃圾版本
IGNORED_SUFFIXES = {
    ".tmp", ".temp", ".part", ".partial", ".crdownload", ".download",
    ".swp", ".swx", ".swo", ".lock", ".lck", ".bak~", ".~lock",
    # 库自己的 SQLite 文件（监视目录覆盖到库时，tooak.db-wal 每次事务都在变）
    ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite-wal", ".sqlite-shm",
}
IGNORED_PREFIXES = (".", "~$", "~")
#: 目录名黑名单：库自己的目录（files/exports）+ 系统垃圾目录。
#: 库目录另有"库根之内一律跳过"的硬守卫（见 :meth:`LibraryWatcher._enqueue`），
#: 这里列出名字是为了让"库在别处、但监视目录里有个同名目录"也不产生噪音。
IGNORED_DIR_NAMES = {"files", "blobs", "exports", "node_modules", "System Volume Information"}


@dataclass
class WatchOutcome:
    """一次监视归档的结果（成功与失败都用它回报）。"""

    path: Path
    node_id: int
    ok: bool = True
    message: str = ""
    result: ArchiveResult | None = None

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"归档失败：{self.path.name}（{self.message}）"
        if self.result is None:
            return f"已处理：{self.path.name}"
        return f"{self.result.document.title} · {self.result.summary}"


class LibraryWatcher:
    """按库里的 ``watch_dirs`` 规则监视目录并自动归档。"""

    def __init__(
        self,
        library_root: str | Path,
        *,
        on_archived: Callable[[WatchOutcome], None] | None = None,
        settle_seconds: float = 1.0,
        max_wait_seconds: float = 120.0,
        archive_existing: bool = False,
    ):
        self.root = Path(library_root)
        self.on_archived = on_archived
        self.settle_seconds = settle_seconds
        self.max_wait_seconds = max_wait_seconds
        self.archive_existing = archive_existing

        self._observer: Any = None
        self._handlers: list[Any] = []
        self._rules: list[dict[str, Any]] = []
        self._pending: set[str] = set()
        #: 在飞的归档线程（stop 时等它们收尾，保证"停了就真的停了"）
        self._threads: list[threading.Thread] = []
        #: 停止信号：等待文件写完的长循环每次采样都会看它一眼，置位后立刻放弃这次归档。
        #: **每一轮 start() 都换一个新对象**：上一轮里还在收尾的线程抱着旧信号不放
        #: （旧信号已被置位 → 它照样会退出），不会被新一轮"清掉信号"重新唤醒去归档。
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._running = False

    # ---------------------------------------------------------------- 生命周期

    @property
    def running(self) -> bool:
        return self._running

    @property
    def rules(self) -> list[dict[str, Any]]:
        return list(self._rules)

    def start(self) -> bool:
        """启动监视；返回是否真的启动（watchdog 缺失、无规则、一个目录都没挂上时为 False）。"""
        if self._running:
            return True
        if not WATCHDOG_AVAILABLE:
            return False

        with Library.open(self.root) as library:
            self._rules = [rule for rule in library.list_watch_dirs() if rule["enabled"]]
        if not self._rules:
            return False

        self._stop_event = threading.Event()
        self._observer = Observer()
        mounted = 0
        for rule in self._rules:
            if is_subpath(rule["path"], self.root):
                # 规则目录就在库里面（用户把库目录本身设成了监视目录）：
                # 挂了必然自喂 —— 归档写 blobs/ 与 tooak.db-wal 又会触发归档。
                # 这种规则直接不挂，而不是等事件来了再丢（省掉一整条噪声链路）。
                continue
            handler = _RuleEventHandler(self, rule)
            try:
                self._observer.schedule(
                    handler, rule["path"], recursive=bool(rule["recursive"])
                )
            except OSError:
                # 目录不存在 / 没权限：跳过这一条规则，别让整次启动失败
                continue
            self._handlers.append(handler)
            mounted += 1
        if mounted == 0:
            # 一个目录都没挂上就报"启动成功"，用户会以为文件会自己归档，
            # 实际上什么都不会发生（而且没有任何提示）—— 这种情况如实返回 False。
            self._observer = None
            return False
        self._observer.daemon = True
        self._observer.start()
        self._running = True

        if self.archive_existing:
            # 监视目录里可能已经有历史文件，先补扫一遍（内容相同不会产生新版本）
            threading.Thread(target=self._scan_existing, daemon=True).start()
        return True

    def stop(self) -> None:
        self._running = False
        # 先置停止信号：正在等"文件写完"的线程会立刻放弃这次归档
        # （否则 stop() 返回后它们还能继续归档，最长 max_wait_seconds = 120 秒）
        self._stop_event.set()
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass
            self._observer = None
        self._handlers.clear()
        # 再等在飞的文件线程收尾：它们已经收到停止信号，正常情况会立刻退出。
        # 每个线程最多等 2 秒、整体最多 5 秒 —— 不能为了"等干净"把界面卡住。
        with self._lock:
            threads = [item for item in self._threads if item.is_alive()]
        deadline = time.monotonic() + 5.0
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=min(2.0, remaining))

    def reload_rules(self) -> bool:
        """规则变化后重启监视。"""
        # 早期写法是 `self.start() if was_running or True else False` —— 恒真，
        # 等于每次重载都无条件启动，用户关掉的监视会被悄悄打开。
        self.stop()
        return self.start()

    # ---------------------------------------------------------------- 内部逻辑

    def _scan_existing(self) -> None:
        for rule in self._rules:
            base = Path(rule["path"])
            pattern = "**/*" if rule["recursive"] else "*"
            for path in base.glob(pattern):
                if path.is_file():
                    self._enqueue(path, rule)

    def _enqueue(self, path: Path, rule: dict[str, Any]) -> None:
        """把一个候选文件排进处理队列（同一路径只处理一次）。"""
        # 库目录之内的文件一律不归档：归档会往 blobs/ 写 blob、往库根写 tooak.db-wal，
        # 而那些写入本身就落在被监视的目录里 → 又触发事件 → 又归档，
        # 这里按**路径**拦，而不是把"包含库"的整条规则丢掉 ——
        # 那种规则往往正是用户真实要监视的目录（例如整个 Documents），
        # 丢掉它等于静默地什么都不监视；只挡库内路径既治了自喂、也不误伤。
        if is_subpath(path, self.root):
            return

        name = path.name
        if not name or name.startswith(IGNORED_PREFIXES):
            return
        if path.suffix.lower() in IGNORED_SUFFIXES:
            return
        if name.endswith("~"):
            return
        # 目录名黑名单：点开头的目录（.git/.cache/…）与系统垃圾目录。
        # 只看**规则目录之下**的那几级：用户如果真的就把一个点开头的目录
        # 设成了监视目录（``~/.文档投递``），不该把它的内容全丢掉。
        base = Path(rule["path"])
        try:
            inner = path.relative_to(base).parts[:-1]
        except ValueError:
            inner = path.parts[:-1]
        for part in inner:
            if part.startswith(".") or part in IGNORED_DIR_NAMES:
                return

        key = str(path)
        with self._lock:
            if key in self._pending:
                return
            self._pending.add(key)

        thread = threading.Thread(target=self._process, args=(path, rule), daemon=True)
        thread.start()
        # 记下在飞的线程，stop() 好等它们收尾（顺手丢掉已经结束的，列表不会无限长）
        with self._lock:
            self._threads = [item for item in self._threads if item.is_alive()]
            self._threads.append(thread)

    def _process(self, path: Path, rule: dict[str, Any]) -> None:
        node_id = int(rule["node_id"])
        stop_event = self._stop_event     # 本次归档绑定"当前这一轮"的停止信号
        try:
            if not self._wait_until_stable(path, stop_event):
                # 两种情况都会走到这里：窗口内没稳定（多半还在写入），或监视已被停止。
                # 两种都不能归档 —— 半截文件会被当成正式版本记进库，
                # 用户拿到的是一个"打不开的副本 + 假的指纹"，比不归档更糟。
                message = ("监视已停止，本次跳过" if stop_event.is_set()
                           else f"文件似乎还在写入（等待 {self.max_wait_seconds:.0f} 秒仍未稳定），已跳过")
                self._emit(WatchOutcome(path=path, node_id=node_id, ok=False,
                                        message=message))
                return
            with Library.open(self.root) as library:
                result = library.archive_file(
                    node_id, path, origin="watch", note="由监视目录自动归档"
                )
            self._emit(WatchOutcome(path=path, node_id=node_id, ok=True, result=result))
        except Exception as error:
            # 这里必须兜**所有**异常，不能只兜 (LibraryError, OSError)：
            # 后台线程里漏出去的异常会静默杀掉这个线程（也没有 traceback 给用户看），
            # 表现就是"拖进去的文件没归档，界面上什么都没发生"。
            # sqlite3.OperationalError: database is locked、IntegrityError、
            # UnicodeDecodeError 都属于这一类，现在都如实回报给界面。
            self._emit(
                WatchOutcome(path=path, node_id=node_id, ok=False,
                             message=f"{type(error).__name__}: {error}")
            )
        finally:
            with self._lock:
                self._pending.discard(str(path))

    def _wait_until_stable(self, path: Path,
                           stop_event: threading.Event | None = None) -> bool:
        """等文件写完（大文件拷贝、另存为都会先出现一个不断增长的文件）。

        判据：连续两次采样大小相同、且修改时间已静止 ``settle_seconds``。

        **只有真的等到稳定才返回 True**：等到超时（或监视被停止）一律返回 False。
        于是"还在写的几十 GB 文件"被当成已稳定，库里记下半截文件的指纹与截断副本。
        """
        stop_event = stop_event if stop_event is not None else self._stop_event
        deadline = time.monotonic() + self.max_wait_seconds
        last_size = -1
        stable_since: float | None = None

        while time.monotonic() < deadline:
            if stop_event.is_set():
                return False        # 停止时立刻放弃，不要"把这一单做完再停"
            try:
                stat = path.stat()
            except OSError:
                return False
            if stat.st_size == last_size:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= self.settle_seconds:
                    return path.is_file()
            else:
                last_size = stat.st_size
                stable_since = None
            time.sleep(0.2)
        return False

    def _emit(self, outcome: WatchOutcome) -> None:
        if self.on_archived is None:
            return
        try:
            self.on_archived(outcome)
        except Exception:
            # 回调是调用方的事，监视器不能被它拖垮
            pass


class _RuleEventHandler(FileSystemEventHandler):  # type: ignore[misc]
    """把 watchdog 事件翻译成候选文件。"""

    def __init__(self, watcher: LibraryWatcher, rule: dict[str, Any]):
        super().__init__()
        self._watcher = watcher
        self._rule = rule

    # watchdog 在各平台的回调名固定为这几个
    def on_created(self, event):  # noqa: D102
        if not event.is_directory:
            self._watcher._enqueue(Path(event.src_path), self._rule)

    def on_modified(self, event):  # noqa: D102
        if not event.is_directory:
            self._watcher._enqueue(Path(event.src_path), self._rule)

    def on_moved(self, event):  # noqa: D102
        if not event.is_directory:
            self._watcher._enqueue(Path(event.dest_path), self._rule)

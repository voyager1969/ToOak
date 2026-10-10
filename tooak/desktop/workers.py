"""后台任务：归档、对比等重活放到工作线程，界面不卡。

------------------------------
这里最初用 ``QThread`` 子类 + 信号，实测会出现**段错误**；
改成 ``threading.Thread`` + Python 定义的 QObject「信号桥」后，
在「多线程运行期间主线程触发 GC」时依然会崩 ——
根因是 shiboken 在析构 Python 定义的 QObject 子类时会调用 C++ 虚函数，
而 GC 的时机不可控。

最终方案：**工作线程只往线程安全队列里放事件枝，界面用 QTimer 轮询取走。**
好处是：

* 工作线程完全不碰 Qt 对象，没有跨线程信号、没有对象生命周期纠缠；
* 事件是普通 dataclass，GC 只回收纯 Python 对象，不会触发 Qt 虚函数；
* 反向取消/关闭只需等线程结束，逻辑直白。

为什么必须后台执行：一份几百 MB 的图纸做 sha256 + 复制要几秒到几十秒，
在 GUI 线程里做会直接冻住窗口。每个任务使用**独立的 Library 会话**
（独立 SQLite 连接），与界面线程的连接互不干扰。
"""

from __future__ import annotations

import itertools
import queue
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core import Library, LibraryError

__all__ = [
    "ArchiveJob",
    "DiffJob",
    "FileCopyJob",
    "LegacyImportJob",
    "WordCompareJob",
    "ArchiveProgress",
    "ArchiveFinished",
    "DiffFinished",
    "FileCopyFinished",
    "WordCompareFinished",
    "ImportProgress",
    "ImportFinished",
    "drain",
]

_ids = itertools.count(1)


# ---------------------------------------------------------------------------
# 事件枝
# ---------------------------------------------------------------------------


@dataclass
class ArchiveProgress:
    """单个文件处理完毕。"""

    job_id: int
    done: int
    total: int
    message: str


@dataclass
class ArchiveFinished:
    """整批归档结束。"""

    job_id: int
    ok: int
    failed: int
    summaries: list[str] = field(default_factory=list)
    #: 这一批里**新建出来的**事件 id（拖到项目上时每个文件一个事件枝）——
    #: 主窗口据此"新建后就跳转到新事件枝"
    created: list[int] = field(default_factory=list)


@dataclass
class DiffFinished:
    """两个版本的比较完成。"""

    job_id: int
    old_version_id: int
    new_version_id: int
    result: Any = None


@dataclass
class WordCompareFinished:
    """「用 Word 比较」结束（成功或失败都发这一条）。"""

    job_id: int
    ok: bool = False
    #: 生成的那份带修订标记的 docx（成功时）
    output: Any = None
    revisions: int = 0
    #: 成功了给一句"比完了"，失败了给一句人能看懂的原因
    message: str = ""
    error: str = ""
    #: "v2 · 提交_v2.docx → v3 · 提交_v3.docx"，界面提示里写清楚比的是哪两份
    labels: str = ""
    #: 这次是谁干的（``Word`` / ``WPS``）—— 装了两套 Office 的机器上要写出来
    engine: str = ""


@dataclass
class ImportProgress:
    """旧库迁移进度。"""

    job_id: int
    done: int
    total: int
    message: str


@dataclass
class ImportFinished:
    """旧库迁移完成。"""

    job_id: int
    stats: Any = None


@dataclass
class ExportProgress:
    """导出进度（复制文件可能很久，得有进度看）。"""

    job_id: int
    done: int
    total: int
    message: str


@dataclass
class ExportFinished:
    """导出结束：``result`` 是 ``ExportResult``，失败时 ``error`` 有值。"""

    job_id: int
    result: Any = None
    error: str = ""


@dataclass
class ZipProgress:
    """打包文件夹的进度。"""

    job_id: int
    done: int
    total: int
    message: str


@dataclass
class ZipFinished:
    """打包结束：成功时 ``path`` 是 zip 路径。"""

    job_id: int
    path: Any = None
    files: int = 0
    bytes: int = 0
    error: str = ""


def drain(target: "queue.Queue") -> list:
    """非阻塞取走队列里现有的全部事件枝。"""
    items: list = []
    while True:
        try:
            items.append(target.get_nowait())
        except queue.Empty:
            return items


# ---------------------------------------------------------------------------
# 任务基类
# ---------------------------------------------------------------------------


class _Job:
    """后台任务：一个线程 + 一个事件枝队列。"""

    def __init__(self, library_root: str | Path):
        self.id = next(_ids)
        self.library_root = Path(library_root)
        self.events: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._guarded_run, daemon=True)
        self._error: BaseException | None = None

    # ---------------------------------------------------------------- 生命周期

    def start(self) -> None:
        self._thread.start()

    def is_running(self) -> bool:
        return self._thread.is_alive()

    def wait(self, timeout: float | None = None) -> bool:
        """等任务结束（窗口关闭时调用，避免留下半截状态）。"""
        if self._thread.is_alive():
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def drain_events(self) -> list:
        return drain(self.events)

    # ------------------------------------------------------------------ 内部

    def _guarded_run(self) -> None:
        try:
            self.run()
        except BaseException as error:  # 兜底：后台异常绝不能让进程无声挂掉
            self._error = error
            try:
                self.on_error(error)
            except BaseException:              # 钩子自己炸了也不能把兜底吃掉
                pass
            # 第二层兜底：``on_error`` 只是"子类可以顺手做点别的"的钩子，
            # 真正的"我失败了"必须由 ``report_failure`` 说出去（见它的说明）。
            # 这里再包一层 try：上报本身失败也不能让工作线程把异常抛到天上。
            try:
                self.report_failure(error)
            except BaseException:
                pass

    def run(self) -> None:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def on_error(self, error: BaseException) -> None:  # pragma: no cover
        """额外处理钩子（默认什么都不做）。

        **别在这里入队 *Finished** —— 那条路已经归 ``report_failure``，
        两边都写就会让界面收到两条完成事件枝（进度框已经关了还再来一次）。
        """

    def report_failure(self, error: BaseException) -> None:  # pragma: no cover
        """任务**异常退出**时往事件队列里补一条"带错误的 *Finished"。

        为什么基类把它单列出来、而不是只留一个空的 ``on_error``：
        ``on_error`` 一直是"子类可选实现"的钩子，而子类很容易漏实现 ——
        漏了的话异常只被存进 ``self._error``（**全项目没有任何人读它**），
        界面永远等不到 ``*Finished``：
        * 「从旧库导入」那条路上的模态进度框**没有取消键**，会永远关不掉，
          用户只能杀进程；
        * 导出 / 打包那条路上任务就这么静默消失，用户以为还在跑。
        所以每个 job **必须**实现它（说清"我失败了"，带上原因），
        ``_guarded_run`` 会在 ``on_error`` 之后再兜一层，保证一定有人被通知到。
        """
        # 谁新加了 job 又忘了实现它：至少要在终端上留个痕迹，别静默
        # （走到这里已经没别的办法通知界面了，这是最后一道网）
        print(f"[{type(self).__name__}] 任务异常退出，但没实现 report_failure：{error!r}",
              file=sys.stderr)
        raise NotImplementedError


class ArchiveJob(_Job):
    """把一批文件归档进库（新增文档或给已有文档加版本）。"""

    def __init__(
        self,
        library_root: str | Path,
        *,
        node_id: int | None = None,
        document_id: int | None = None,
        paths: list[str] | None = None,
        project_id: int | None = None,
        as_primary: bool = False,
        note: str = "",
        stage_role: str | None = None,
        stage_replace: bool = False,
        target_version_id: int | None = None,
        request: dict | None = None,
    ):
        super().__init__(library_root)
        self.node_id = node_id
        self.document_id = document_id
        #: 指定项目时，每个文件各自成为一个新事件枝（拖到项目树行上的语义）
        self.project_id = project_id
        self.paths = [Path(item) for item in (paths or [])]
        self.as_primary = as_primary
        self.note = note
        #: 指定后，文件进这个环节（拖到"输出文件"上就是这条路径）
        self.stage_role = stage_role
        #: True = 替换该环节的旧文件（换稿）；False = 追加
        self.stage_replace = stage_replace
        #: 指定后，文件加进**这一稿**（不新建版本）；None = 照旧新建一稿
        self.target_version_id = target_version_id
        #: 原始请求（主窗口据此决定归档完成后刷新哪份文档）
        self.request: dict = request or {
            "node_id": node_id,
            "document_id": document_id,
            "project_id": project_id,
            "paths": [str(item) for item in self.paths],
            "as_primary": as_primary,
        }
        self._thread.name = f"ToOakArchive-{self.id}"

    def run(self) -> None:  # noqa: D102
        ok = failed = 0
        summaries: list[str] = []
        created: list[int] = []
        total = len(self.paths)

        try:
            library = Library.open(self.library_root)
        except LibraryError as error:
            self.events.put(ArchiveFinished(self.id, 0, total,
                                            [f"打开库失败：{error}"]))
            return

        try:
            if self.target_version_id is not None:
                # 加进**已经有的那一稿**（用户先点「＋ 新增一稿」建好空稿，
                # 之后拖进来的文件都算这一稿的）—— 不新建版本。
                try:
                    version, skipped = library.add_files_to_version(
                        self.target_version_id,
                        self.paths,
                        role=self.stage_role,
                        replace_role=self.stage_replace,
                        as_primary=self.as_primary,
                    )
                    # 成功条数必须**如实**：路径不在磁盘上的那些被跳过了，
                    # 不能拿"拖进来几个"当成功数（否则界面报"成功 3 个"而库里只有 1 个）
                    ok = total - len(skipped)
                    failed = len(skipped)
                    message = f"已加进 v{version.seq}（{ok} 个文件）"
                    if skipped:
                        names = "、".join(skipped[:3]) + ("…" if len(skipped) > 3 else "")
                        message += f"，跳过 {len(skipped)} 个：{names}"
                except (LibraryError, OSError) as error:
                    failed = total
                    ok = 0
                    message = f"归档失败：{error}"
                summaries.append(message)
                self.events.put(ArchiveProgress(self.id, total, total, message))
            elif self.document_id is not None:
                # 落在文档上的多个文件属于**同一个新版本**（一批附件），
                # 不能逐个文件各建一个版本，否则会产生一串无意义的中间版本。
                try:
                    version, dedup = library.attach_files(
                        self.document_id,
                        self.paths,
                        note=self.note,
                        as_primary=self.as_primary,
                        role=self.stage_role,
                        replace_role=self.stage_replace,
                    )
                    # 成功条数必须**如实**：``attach_files`` 对不是文件的路径
                    # （文件夹、已经不存在的路径）是**静默跳过**的，
                    # 所以不能拿"拖进来几个"当成功数 —— 否则界面报"成功 3 个"
                    # 而库里其实只进了 1 个，用户对不上账。
                    ok = sum(1 for item in self.paths if Path(item).is_file())
                    failed = total - ok
                    message = (
                        f"内容未变化（仍为 v{version.seq}）"
                        if dedup
                        else f"新增 v{version.seq}（{ok} 个文件）"
                    )
                except (LibraryError, OSError) as error:
                    failed = total
                    ok = 0
                    message = f"归档失败：{error}"
                summaries.append(message)
                self.events.put(ArchiveProgress(self.id, total, total, message))
            else:
                for index, path in enumerate(self.paths, start=1):
                    try:
                        target_node = self.node_id
                        if target_node is None and self.project_id is not None:
                            # 拖到项目上：挂到项目根节点下，各自成为新事件枝
                            roots = library.node_tree(self.project_id)
                            target_node = roots[0].id if roots else None
                        if target_node is None:
                            raise LibraryError("没有指定归档目标")
                        result = library.archive_file(
                            target_node, path, note=self.note, origin="manual"
                        )
                        if result.created_document:
                            created.append(result.document.id)
                        message = result.summary
                        ok += 1
                    except (LibraryError, OSError) as error:
                        failed += 1
                        message = f"{path.name}：{error}"
                    summaries.append(message)
                    self.events.put(ArchiveProgress(self.id, index, total, message))
        finally:
            library.close()

        self.events.put(ArchiveFinished(self.id, ok, failed, summaries, created))

    def report_failure(self, error: BaseException) -> None:  # noqa: D102
        self.events.put(
            ArchiveFinished(self.id, 0, len(self.paths), [f"归档异常：{error!r}"])
        )


class DiffJob(_Job):
    """在后台计算两个版本的差异（大文件抽取/哈希可能较慢）。

    事件里带上两个版本 id：界面据此判断「结果回来时用户是不是已经切到别的版本了」，
    避免旧结果盖掉新内容。
    """

    def __init__(
        self, library_root: str | Path, old_version_id: int, new_version_id: int,
        role: str | None = None, allow_large: bool = False,
        file_id: int | None = None,
    ):
        super().__init__(library_root)
        self.old_version_id = old_version_id
        self.new_version_id = new_version_id
        #: 指定后只比较这个环节（环节年轮用），否则比较整个版本
        self.role = role
        #: 指定后只比较这一份文件（点文件时用，比 role 更细）
        self.file_id = file_id
        #: 用户明确要求"就算文件大也比"（界面上点了「在后台比较」）
        self.allow_large = allow_large
        self._thread.name = f"ToOakDiff-{self.id}"

    def run(self) -> None:  # noqa: D102
        try:
            with Library.open(self.library_root) as library:
                if self.file_id is not None:
                    result = library.diff_file(
                        self.old_version_id, self.new_version_id, self.file_id,
                        allow_large=self.allow_large,
                    )
                elif self.role:
                    result = library.diff_stage(
                        self.old_version_id, self.new_version_id, self.role,
                        allow_large=self.allow_large,
                    )
                else:
                    result = library.diff_versions(
                        self.old_version_id, self.new_version_id,
                        allow_large=self.allow_large,
                    )
        except LibraryError as error:
            from ..core.diffing import DiffResult

            result = DiffResult(kind="binary", summary=f"比较失败：{error}")
        self.events.put(
            DiffFinished(self.id, self.old_version_id, self.new_version_id, result)
        )

    def report_failure(self, error: BaseException) -> None:  # noqa: D102
        from ..core.diffing import DiffResult

        self.events.put(
            DiffFinished(
                self.id, self.old_version_id, self.new_version_id,
                DiffResult(kind="binary", summary=f"比较异常：{error!r}"),
            )
        )


class WordCompareJob(_Job):
    """在后台调用**本机 Word 自己的比较功能**（右键文件菜单的那一项）。

    为什么必须在后台：Word 的 COM 服务器启动一次实测要十几秒
    （首次/冷启动更久），放在界面线程里就是"点一下，窗口白屏十几秒"。
    这里连"配对 + 守门"一起放进来 —— 那两步要查库、要 stat 旧稿的文件，
    同样不该占用界面线程。

    配对语义（"和哪一稿比"）由内核的 ``Library.word_compare_pair`` 决定，
    这个 job 只负责：拿到配对 → 起 PowerShell 调 Word → 把结果说清楚。
    """

    def __init__(
        self,
        library_root: str | Path,
        version_id: int,
        file_id: int,
        old_version_id: int,
        *,
        engine: str = "",
        show: bool = True,
        author: str = "ToOak",
    ):
        super().__init__(library_root)
        self.version_id = version_id
        self.file_id = file_id
        self.old_version_id = old_version_id
        #: 用哪一套比（``"word"`` / ``"wps"``；``""`` = 内核按本机情况挑）。
        self.engine = engine
        #: True = 比较完把结果留在 Word 里给用户看（界面默认）
        self.show = show
        self.author = author
        self._thread.name = f"ToOakWord-{self.id}"

    def run(self) -> None:  # noqa: D102
        from ..core import wordcompare

        try:
            # 配对要在库会话里做完，然后**关掉库**再去调 Word：
            # Word 那一趟可能跑几分钟，没必要一直占着一条 SQLite 连接。
            with Library.open(self.library_root) as library:
                pair = library.word_compare_pair(
                    self.version_id, self.file_id, self.old_version_id
                )
        except LibraryError as error:
            self.events.put(WordCompareFinished(self.id, False, error=str(error)))
            return
        except Exception as error:                      # noqa: BLE001
            self.events.put(WordCompareFinished(self.id, False, error=f"比较失败：{error!r}"))
            return

        labels = f"{pair['original_label']} → {pair['revised_label']}"
        output = wordcompare.suggest_output_path(
            pair["original_path"], pair["revised_path"],
            library_root=self.library_root, title=pair["title"], stage=pair["role_label"],
        )
        try:
            result = wordcompare.compare(
                pair["original_path"], pair["revised_path"], output,
                author=self.author, show=self.show, engine=self.engine,
            )
        except wordcompare.WordCompareError as error:
            self.events.put(WordCompareFinished(
                self.id, False, error=error.message, labels=labels,
            ))
            return
        self.events.put(WordCompareFinished(
            self.id, True, output=result.output, revisions=result.revisions,
            message=f"共 {result.revisions} 处修订", labels=labels,
            engine=result.kind_label,
        ))

    def report_failure(self, error: BaseException) -> None:  # noqa: D102
        self.events.put(WordCompareFinished(
            self.id, False, error=f"调用 Word 时出错：{error!r}",
        ))


@dataclass
class FileCopyFinished:
    """「复制一份到这一稿」结束（成功或失败都发这一条）。"""

    job_id: int
    ok: bool = False
    #: 成功时的结果字典（``Library.register_file_to_version`` 的返回值）
    result: Any = None
    error: str = ""


class FileCopyJob(_Job):
    """把某一份文件**真的复制一份**（库里新的一份实体文件）登记到另一稿。

    为什么要在后台：复制要读一遍、写一遍，还要算 sha256 —— Office 文档是毫秒级，
    但库里有 GB 级的图纸与模型，实测那种文件一次要几十秒。
    放在界面线程里就是"点一下菜单，窗口白几十秒"（与归档同一条纪律）。
    """

    def __init__(self, library_root: str | Path, source_version_id: int, file_id: int,
                 target_version_id: int, *, new_name: str | None = None):
        super().__init__(library_root)
        self.source_version_id = source_version_id
        self.file_id = file_id
        self.target_version_id = target_version_id
        #: 用户在那个确认框里改过的名字；``None`` = 沿用原来的名字（名字本身由
        #: 内核的 ``normalize_new_name`` 校验，界面不替它做决定）
        self.new_name = new_name
        self._thread.name = f"ToOakCopy-{self.id}"

    def run(self) -> None:  # noqa: D102
        try:
            with Library.open(self.library_root) as library:
                result = library.register_file_to_version(
                    self.source_version_id, self.file_id, self.target_version_id,
                    new_name=self.new_name,
                )
        except LibraryError as error:
            self.events.put(FileCopyFinished(self.id, False, error=str(error)))
            return
        except Exception as error:                      # noqa: BLE001
            self.events.put(FileCopyFinished(self.id, False, error=f"复制失败：{error!r}"))
            return
        self.events.put(FileCopyFinished(self.id, True, result=result))

    def report_failure(self, error: BaseException) -> None:  # noqa: D102
        self.events.put(FileCopyFinished(self.id, False, error=f"复制出错：{error!r}"))


class LegacyImportJob(_Job):
    """把旧版 ToOak/Symphytum 库迁移进当前库（重活，必须后台跑）。"""

    def __init__(
        self,
        source_root: str | Path,
        library_root: str | Path,
        *,
        mode: str = "reference",
        workers: int = 8,
        group_by_tag: bool = True,
        include_files: bool = True,
    ):
        super().__init__(library_root)
        self.source_root = Path(source_root)
        self.mode = mode
        self.workers = workers
        self.group_by_tag = group_by_tag
        self.include_files = include_files
        self._thread.name = f"ToOakImport-{self.id}"

    def run(self) -> None:  # noqa: D102
        # import 也放进 try：它自己就会抛（模块缺失 / 依赖坏了），
        # 落在 try 外面的话异常虽然被 ``_guarded_run`` 兜住，但走的是
        # "连统计都没有"的那条路 —— 提示不如这里给的清楚
        try:
            from ..core.legacy import import_legacy

            stats = import_legacy(
                self.source_root,
                self.library_root,
                mode=self.mode,
                workers=self.workers,
                group_by_tag=self.group_by_tag,
                include_files=self.include_files,
                progress=self._report,
            )
        except Exception as error:  # 迁移失败要让人看到原因，而不是静默
            from ..core.legacy import ImportStats

            stats = ImportStats(errors=[f"迁移失败：{error!r}"])
        self.events.put(ImportFinished(self.id, stats))

    def report_failure(self, error: BaseException) -> None:  # noqa: D102
        # 连 ``ImportStats`` 都拿不到的极端情况（比如 core.legacy 自己坏了）：
        # 交一个 ``stats=None`` 也要把 ImportFinished 发出去 —— 界面上那个
        # **没有取消键**的模态进度框就靠这条事件关掉。
        try:
            from ..core.legacy import ImportStats

            stats = ImportStats(errors=[f"迁移异常：{error!r}"])
        except Exception:
            stats = None
        self.events.put(ImportFinished(self.id, stats))

    def _report(self, stage: str, done: int, total: int, message: str) -> None:
        self.events.put(ImportProgress(self.id, done, total, f"[{stage}] {message}"))


class ExportJob(_Job):
    """把一批事件枝（或一个分组）导成文件夹：**只读源文件，只往目标目录写**。

    放在后台线程里跑：项目里的文件可能几 GB，复制起来不能让界面卡住。
    """

    def __init__(self, library_root: str | Path, document_ids: list[int],
                 target_dir: str | Path, *, name: str, note: str = "",
                 source: str = "框选", group_id: int | None = None):
        super().__init__(library_root)
        self.document_ids = [int(item) for item in document_ids]
        self.target_dir = Path(target_dir)
        self.name = name
        self.note = note
        self.source = source
        self.group_id = group_id

    def run(self) -> None:
        try:
            # import 也包进 try：模块级导入失败同样要让界面收到 ExportFinished
            from ..core import Library
            from ..core.exporter import export_documents, export_group

            with Library.open(self.library_root) as library:
                if self.group_id is not None:
                    result = export_group(library, self.group_id, self.target_dir,
                                          progress=self._report)
                else:
                    result = export_documents(
                        library, self.document_ids, self.target_dir,
                        name=self.name, note=self.note, source=self.source,
                        progress=self._report,
                    )
        except Exception as error:                      # 导出失败要让人看到原因
            self.events.put(ExportFinished(self.id, None, f"{error}"))
            return
        self.events.put(ExportFinished(self.id, result))

    def report_failure(self, error: BaseException) -> None:  # noqa: D102
        self.events.put(ExportFinished(self.id, None, f"{error}"))

    def _report(self, done: int, total: int, message: str) -> None:
        self.events.put(ExportProgress(self.id, done, total, message))


class ZipJob(_Job):
    """把用户拖进来的**文件夹**打包成一个 zip（就放在文件夹旁边）。

    ToOak 不收文件夹（它要替代文件夹管理），所以只能先把文件夹变成"一个文件"。
    打包可能很久，放后台线程跑；结果路径通过 :class:`ZipFinished` 回主线程，
    由主窗口在画布上放一张素材卡片。
    """

    #: 完成后由主窗口挂上的回调：``(zip 路径, 文件数, 字节)``
    on_done = None

    def __init__(self, library_root: str | Path, folder: str | Path):
        super().__init__(library_root)
        self.folder = Path(folder)

    def run(self) -> None:
        try:
            # import 包进 try：理由同 ExportJob.run（导入失败也要有 ZipFinished）
            from ..core.exporter import pack_folder_to_zip

            path, files, size = pack_folder_to_zip(
                self.folder, progress=self._report
            )
        except Exception as error:                 # 权限、磁盘满…都要让人看到
            self.events.put(ZipFinished(self.id, None, 0, 0, f"{error}"))
            return
        self.events.put(ZipFinished(self.id, path, files, size))

    def report_failure(self, error: BaseException) -> None:  # noqa: D102
        self.events.put(ZipFinished(self.id, None, 0, 0, f"{error}"))

    def _report(self, done: int, total: int, message: str) -> None:
        self.events.put(ZipProgress(self.id, done, total, message))

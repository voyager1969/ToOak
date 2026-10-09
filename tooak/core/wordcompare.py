"""调用 **Microsoft Word 自己的比较功能**（审阅 → 比较 → 比较文档）。

为什么要走 Word 而不是自己算
---------------------------
内置的比较（``core/diffing.py``）是把 Word 正文抽成纯文本再逐行 diff。
它对"这段文字改了什么"够用，但**丢掉了 Word 里真正重要的那部分**：
格式改动、表格结构、页眉页脚、批注、编号、脚注、移动的段落……
用户是拿 Word 干活的人，他要的是 Word 里那份**带修订标记的红头文件**
（可以逐条接受/拒绝、可以给同事看）。那只有 Word 自己能生成。

为什么用 PowerShell 而不是 pywin32
---------------------------------
Word 的 COM 接口（``Application.CompareDocuments``）本来从 Python 也能调，
但那样要给这个"只依赖标准库"的内核加一个 ``pywin32`` 依赖；
而 Windows 上 ``powershell.exe`` 是自带的，它调 COM 的能力和 pywin32 一样。
于是这里的做法是：**拼一段 ASCII 的 PowerShell 脚本 → 起一个子进程去跑**，
内核一个第三方依赖都不加（``diffing.py`` 里"不引 python-docx"是同一条纪律）。

--------------------------------------
1. **路径不能拼进脚本文本**。库路径、事件名、文件名全是中文，
   而 Windows PowerShell 5.1 读 .ps1 是按 **ANSI(GBK)** 解的 ——
   脚本里写死中文路径，Word 就会去开一个不存在的文件（或者更糟：乱码路径）。
   所以路径一律通过**环境变量**传（环境变量走 Unicode，与代码页无关），
   脚本本身写成带 BOM 的 UTF-8（没有 BOM 的话，里面的中文注释会被当成 GBK 读成乱码）。
   脚本回话也不走 stdout（那还是代码页），而是写一个 UTF-8 的小文件回来。
2. **Word 启动很慢**（本机实测首次约 15 秒），绝不能放在界面线程里。
   调用的地方一律是后台线程（``desktop/workers.py`` 的 ``WordCompareJob``）。

失败时的态度
------------
"没装 Word"、"Word 的 COM 服务器起不来"、"文档受保护视图挡住"…… 这些都是
**正常会发生的事**，一律翻译成人能看懂的一句话交给界面，绝不吞掉。
（开发时实测：沙箱里跑会拿到 ``CO_E_SERVER_EXEC_FAILURE (0x80080005)`` ——
Word 起不来是因为它的用户配置目录写不进去，这种现象必须说得清。）
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .util import safe_filename, unique_path

__all__ = [
    "WORD_EXTS",
    "WordCompareError",
    "WordCompareResult",
    "is_word_file",
    "available",
    "unavailable_reason",
    "script_text",
    "compare",
    "suggest_output_path",
]

#: Word 能打开、也就能"用它自己来比较"的扩展名。
#: 刻意不含 .txt/.rtf 之外的纯文本：那些内置 diff 已经做得很好，没必要把 Word 拉起来。
WORD_EXTS = {
    "doc", "docx", "docm", "dot", "dotx", "dotm", "rtf", "odt",
}

#: Word 生成比较结果时用的格式：wdFormatDocumentDefault = 16（.docx）
_WD_FORMAT_DOCX = 16
#: wdCompareDestinationNew = 2（比较结果放进**新文档**，原文件一个字节都不动）
_WD_COMPARE_DEST_NEW = 2
#: wdGranularityWordLevel = 1（按词比较；比按字符读起来清楚）
_WD_GRANULARITY_WORD = 1

#: 默认超时：Word 首次启动 + 大文档比较，给足时间
DEFAULT_TIMEOUT = 300.0

#: ``CompareDocuments`` 的完整位置参数（除前四个之外都是"要不要比这些"的开关）。
#: 顺序照 VBA 的签名：Destination, Granularity, CompareFormatting, CompareCaseChanges,
#: CompareWhitespace, CompareTables, CompareHeaders, CompareFootnotes, CompareTextboxes,
#: CompareFields, CompareComments, CompareMoves, RevisedAuthor。
#: **格式默认不比**（CompareFormatting=False）：写论文/报告时"整段重新排了一下版"
#: 会把修订标记糊满整页，把真正的文字改动淹掉。
_COMPARE_FLAGS = "$false, $false, $false, $true, $true, $true, $true, $true, $true, $true"


class WordCompareError(RuntimeError):
    """调 Word 比较失败（没装 Word、COM 起不来、文档打不开、超时…）。

    ``message`` 是给用户看的一句话；``detail`` 是原始报错（日志/详情用）。
    """

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


@dataclass
class WordCompareResult:
    """一次 Word 比较的结果。"""

    output: Path
    #: Word 数出来的修订条数（界面上"共 N 处修订"）
    revisions: int = 0
    #: 比较用的两份文件（原文 / 改后），只用来写提示
    original: Path | None = None
    revised: Path | None = None
    #: Word 是不是被留着打开（界面上那份带修订的红头文件）
    shown: bool = False
    seconds: float = 0.0
    details: list[str] = field(default_factory=list)


def is_word_file(name: str | Path) -> bool:
    """这份文件是不是"该交给 Word 去比"的类型。"""
    return Path(str(name)).suffix.lower().lstrip(".") in WORD_EXTS


# ---------------------------------------------------------------------------
# 有没有 Word
# ---------------------------------------------------------------------------

_AVAILABLE: bool | None = None


def available(refresh: bool = False) -> bool:
    """本机能不能用 Word 的 COM 接口（Windows + Word 已注册 COM 服务器）。

    只看**注册表里有没有 ``Word.Application``**：这一步是毫秒级的，可以放心
    在右键菜单弹出时调用；真的去 New-Object 起一次 Word 要十几秒，那件事
    放在后台线程里做，失败了再说（见 :func:`compare`）。

    结果缓存：注册表不会在程序运行期间变。
    """
    global _AVAILABLE
    if _AVAILABLE is not None and not refresh:
        return _AVAILABLE
    _AVAILABLE = _probe()
    return _AVAILABLE


def _probe() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
    except ImportError:                     # pragma: no cover - 非 Windows 不会走到
        return False
    for hive, key in (
        (winreg.HKEY_CLASSES_ROOT, r"Word.Application\CurVer"),
        (winreg.HKEY_CLASSES_ROOT, r"Word.Application\CLSID"),
    ):
        try:
            with winreg.OpenKey(hive, key):
                return True
        except OSError:
            continue
    return False


def unavailable_reason() -> str:
    """用不了 Word 时，一句能写进菜单提示的人话。"""
    if sys.platform != "win32":
        return "Word 比较只在 Windows 上可用（需要本机安装 Microsoft Word）"
    return ("没有找到 Microsoft Word：这一项要调用本机 Word 的"
            "「审阅 → 比较」功能，装好 Word 之后这里就能点了")


# ---------------------------------------------------------------------------
# PowerShell 脚本（**纯 ASCII**，路径走环境变量）
# ---------------------------------------------------------------------------

#: 脚本里给 Python 看的回话格式（stdout 一行）
_OK_MARK = "TOOAK-OK"
_ERR_MARK = "TOOAK-ERR"

_SCRIPT = r"""
$ErrorActionPreference = 'Stop'

function Get-EnvText([string]$name) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if ($null -eq $value) { return '' }
    return $value
}

# 回话走一个**文件**，不走 stdout：PowerShell 5.1 往管道里写的东西用的是
# 控制台代码页（中文机器上是 GBK），而 Word 的报错里必然有中文
# —— 走 stdout 就得赌编码。写文件时显式指定 UTF-8，这边就与代码页无关了。
function Write-Report([string]$text) {
    try {
        [System.IO.File]::WriteAllText(
            $reportPath, $text, (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
    Write-Output $text
}

$origPath = Get-EnvText 'TOOAK_WORD_ORIG'
$revPath  = Get-EnvText 'TOOAK_WORD_REV'
$outPath  = Get-EnvText 'TOOAK_WORD_OUT'
$reportPath = Get-EnvText 'TOOAK_WORD_REPORT'
$author   = Get-EnvText 'TOOAK_WORD_AUTHOR'
$keepOpen = (Get-EnvText 'TOOAK_WORD_VISIBLE') -eq '1'

if (-not $origPath -or -not $revPath -or -not $outPath) {
    Write-Report 'TOOAK-ERR missing TOOAK_WORD_ORIG / TOOAK_WORD_REV / TOOAK_WORD_OUT'
    exit 2
}

$word = $null
$orig = $null
$rev = $null
$cmp = $null
$revisions = -1
$failure = ''

try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    # ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False
    # ReadOnly 很关键：原文件绝不会因为"比一次"而被改到或锁住。
    $orig = $word.Documents.Open($origPath, $false, $true, $false)
    $rev = $word.Documents.Open($revPath, $false, $true, $false)
    if ($author) {
        $cmp = $word.CompareDocuments($orig, $rev, __DEST__, __GRAN__, __FLAGS__, $author)
    } else {
        $cmp = $word.CompareDocuments($orig, $rev, __DEST__, __GRAN__, __FLAGS__)
    }
    $cmp.SaveAs2($outPath, __FORMAT__)
    $revisions = $cmp.Revisions.Count
} catch {
    $failure = $_.Exception.Message
}

if ($failure -eq '') {
    if ($keepOpen) {
        # 用户要的就是"在 Word 里看那份带修订的文件"：留着它，别关。
        try { $word.Visible = $true } catch { }
        try { $cmp.Activate() } catch { }
        Write-Report ('TOOAK-OK ' + $revisions + ' 1')
        exit 0
    }
    try { $cmp.Close(0) } catch { }
    try { $orig.Close(0) } catch { }
    try { $rev.Close(0) } catch { }
    try { $word.Quit() } catch { }
    Write-Report ('TOOAK-OK ' + $revisions + ' 0')
    exit 0
}

# 失败：能关的都关掉，别在后台留一个看不见的 Word 进程占着文档
try { if ($cmp) { $cmp.Close(0) } } catch { }
try { if ($orig) { $orig.Close(0) } } catch { }
try { if ($rev) { $rev.Close(0) } } catch { }
try { if ($word) { $word.Quit() } } catch { }
Write-Report ('TOOAK-ERR ' + $failure)
exit 1
""".replace("__DEST__", str(_WD_COMPARE_DEST_NEW)) \
   .replace("__GRAN__", str(_WD_GRANULARITY_WORD)) \
   .replace("__FLAGS__", _COMPARE_FLAGS) \
   .replace("__FORMAT__", str(_WD_FORMAT_DOCX))


def script_text() -> str:
    """返回要交给 ``powershell.exe`` 的脚本正文（可离线检查）。

    写盘时按 **UTF-8** 写（见 :func:`compare`）：脚本里有中文注释，
    而 PowerShell 5.1 对**没有 BOM** 的 .ps1 是按 ANSI 代码页解的，
    中文注释会变成乱码 —— 乱码里如果恰好出现反引号或引号，脚本就崩了。
    所以这里连同 BOM 一起写（``utf-8-sig``），别改成裸 utf-8。
    """
    return _SCRIPT


# ---------------------------------------------------------------------------
# 真正跑一次
# ---------------------------------------------------------------------------


def _powershell() -> str:
    """用哪个解释器：Windows PowerShell 5.1（Windows 自带，一定有）。

    刻意不去找 ``pwsh``：PowerShell 7 不是每台机器都装，
    而这里要的只是"能调 COM"这一件事，5.1 完全够用。
    """
    return os.environ.get("TOOAK_POWERSHELL") or "powershell.exe"


def compare(
    original: str | Path,
    revised: str | Path,
    output: str | Path | None = None,
    *,
    author: str = "ToOak",
    show: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> WordCompareResult:
    """用 Word 比较两份文档，把**带修订标记的结果**存成一份新 docx。

    :param original: 旧的那一份（Word 里的"原始文档"）
    :param revised: 新的那一份（"修订的文档"）
    :param output: 结果存哪；不给就用 :func:`suggest_output_path`
    :param author: 修订标记挂在谁名下（Word 默认是当前用户名，这里写清是 ToOak 生成的）
    :param show: True = 比较完把 Word 留着打开给用户看（界面默认）；
        False = 存完就关掉 Word（测试 / 无人值守用）
    :raises WordCompareError: 没装 Word、起不来、文档打不开、超时……一律带人话

    **两份原文件都只以只读方式打开**（``ReadOnly=True``），Word 不会改它们，
    也不会因为它们已经开在用户的 Word 里而失败。
    """
    if not available():
        raise WordCompareError(unavailable_reason())

    original_path = Path(original)
    revised_path = Path(revised)
    for label, path in (("原始", original_path), ("修订", revised_path)):
        if not path.is_file():
            raise WordCompareError(f"{label}文档已不在原处，比不了：{path.name}")

    out_path = Path(output) if output else suggest_output_path(original_path, revised_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Word 会把结果**一直开着**（show=True 时它自己占着这个文件），
    # 因此绝不能覆盖同名文件：第二次比较必须落在一个新名字上。
    out_path = unique_path(out_path)

    handle, script_name = tempfile.mkstemp(prefix="tooak-word-", suffix=".ps1")
    os.close(handle)                        # mkstemp 给的句柄要还回去，不然泄漏一个 fd
    script_file = Path(script_name)
    report_file = script_file.with_suffix(".report.txt")
    env = dict(os.environ)
    env.update({
        "TOOAK_WORD_ORIG": str(original_path),
        "TOOAK_WORD_REV": str(revised_path),
        "TOOAK_WORD_OUT": str(out_path),
        "TOOAK_WORD_REPORT": str(report_file),
        "TOOAK_WORD_AUTHOR": author or "",
        "TOOAK_WORD_VISIBLE": "1" if show else "0",
    })

    started = time.monotonic()
    report = ""
    try:
        # utf-8-sig：带 BOM 写。PowerShell 5.1 见到 BOM 才按 UTF-8 读脚本，
        # 不然按 ANSI 代码页读，脚本里的中文注释就成了乱码。
        script_file.write_text(script_text(), encoding="utf-8-sig", newline="\r\n")
        completed = _run(script_file, env, timeout)
        # 报告必须在 finally 删掉那两个临时文件**之前**读出来
        report = _read_report(report_file)
    finally:
        for leftover in (script_file, report_file):
            try:
                leftover.unlink()
            except OSError:                   # 临时文件删不掉不该让整件事失败
                pass
    seconds = time.monotonic() - started

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    answer = report or stdout          # 报告文件是主渠道，stdout 只是备份

    if (match := re.search(rf"{_OK_MARK}\s+(-?\d+)\s+([01])", answer)):
        revisions = int(match.group(1)) if int(match.group(1)) >= 0 else 0
        return WordCompareResult(
            output=out_path,
            revisions=revisions,
            original=original_path,
            revised=revised_path,
            shown=match.group(2) == "1",
            seconds=seconds,
        )

    reason = _explain(answer, stderr, completed.returncode)
    raise WordCompareError(reason, detail=_tail(answer, stderr))


def _read_report(path: Path) -> str:
    """读回脚本写的那份"回话文件"（UTF-8，没有编码猜测）。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _run(script_file: Path, env: dict, timeout: float):
    """起一个子进程跑脚本。

    ``CREATE_NO_WINDOW``：打包成窗口程序（``tooak.spec`` 是 windowed）之后，
    不遮一下会有一个黑框闪出来 —— 用户看着像中毒。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(
            [
                _powershell(), "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", str(script_file),
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
            creationflags=flags,
        )
    except subprocess.TimeoutExpired as expired:      # 大文档 / Word 卡住
        raise WordCompareError(
            f"Word 比较超时（超过 {int(timeout)} 秒还没回来）。"
            "文档很大时可以先在 Word 里手工比一次，看是不是它自己卡住了。",
            detail=str(expired),
        ) from expired
    except FileNotFoundError as error:                # 连 powershell 都没有
        raise WordCompareError(
            "起不了 PowerShell，没法调用 Word（这台机器可能被策略限制住了）。",
            detail=str(error),
        ) from error
    except OSError as error:
        raise WordCompareError("调用 Word 失败，系统不让起子进程。", detail=str(error)) from error


#: 常见的 COM 报错 → 人话。键是 HRESULT / 报错片段，值是给用户的解释。
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    (
        "80080005",
        "Word 的 COM 服务器起不来（0x80080005）。常见原因：Word 正在弹窗等回答"
        "（比如「激活 Office」或「文档恢复」），或者它的用户配置目录写不进去"
        "（受限账户 / 沙箱环境）。先手工打开一次 Word、把弹窗处理掉再试。",
    ),
    (
        "80070005",
        "系统拒绝了 Word 的自动化请求（权限不足，0x80070005）。",
    ),
    (
        "800a",
        "Word 打不开这份文档：它可能已被删除、正被独占占用，或位于受保护视图里。",
    ),
    (
        "Protected View",
        "这份文档被 Word 的「受保护视图」挡住了，Word 不让自动化去比。"
        "先在 Word 里手工打开一次、点「启用编辑」再试。",
    ),
    (
        "class not registered",
        "Word 的 COM 组件没有注册（Office 可能装得不完整）。",
    ),
)


def _explain(answer: str, stderr: str, returncode: int | None) -> str:
    """把 Word / PowerShell 的报错翻成一句人话。

    ``answer`` 是脚本写回来的那段文本（报告文件优先、stdout 兜底）。
    """
    raw = ""
    if match := re.search(rf"{_ERR_MARK}\s*(.*)", answer):
        raw = match.group(1).strip()
    if not raw:
        # 脚本压根没跑到"写报告"那一步（权限策略挡住了、PowerShell 自己崩了…）：
        # stderr 的最后一行虽然带着一堆英文壳，也比只说"失败了"强
        text = (stderr or answer).strip()
        raw = text.splitlines()[-1].strip() if text else ""

    if not raw:
        return (f"Word 比较没有成功（退出码 {returncode}），"
                "也没拿到具体原因：可以先手工打开一次 Word 确认它能正常启动。")

    for needle, hint in _ERROR_HINTS:
        if needle.lower() in raw.lower():
            return f"{hint}\n（Word 原话：{raw[:300]}）"
    return f"Word 报错：{raw[:300]}"


def _tail(answer: str, stderr: str, limit: int = 2000) -> str:
    parts = [part.strip() for part in (answer, stderr) if part and part.strip()]
    return "\n".join(parts)[-limit:]


# ---------------------------------------------------------------------------
# 结果放哪
# ---------------------------------------------------------------------------


def compare_dir(library_root: str | Path) -> Path:
    """比较结果的落点：``<库根>/exports/word-compare/``。

    刻意**不放进** ``files/``：那是库的正式存储（会被 gc、会被算进"库内副本"），
    而一份"比较结果"是看一次就完的中间产物 —— 和 ``exports/.drag`` 同一性质。
    放在库里而不是临时目录，是因为用户经常要把这份红头文件发给别人。
    """
    return Path(library_root) / "exports" / "word-compare"


def suggest_output_path(
    original: str | Path,
    revised: str | Path,
    *,
    library_root: str | Path | None = None,
    title: str = "",
    stage: str = "",
) -> Path:
    """给比较结果起个名字：``比较 <事件> <环节> v2→v3.docx``。

    带时间戳（到秒）不是为了好看，而是因为 Word 会把结果开着占住这个文件：
    同一对稿比第二次时必须落在一个新名字上。
    """
    old_name = Path(str(original)).stem
    new_name = Path(str(revised)).stem
    bits = ["比较"]
    if title:
        bits.append(safe_filename(title))
    if stage:
        bits.append(safe_filename(stage))
    bits.append(f"{safe_filename(old_name)}→{safe_filename(new_name)}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = safe_filename(" ".join(bits))[:150] + f" ({stamp}).docx"
    base = Path(library_root) / "exports" / "word-compare" if library_root \
        else Path(str(revised)).parent
    return base / filename

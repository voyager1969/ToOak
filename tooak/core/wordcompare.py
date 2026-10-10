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

import glob
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
    "ENGINES",
    "WordCompareError",
    "WordCompareResult",
    "is_word_file",
    "available",
    "engines",
    "engine_label",
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

#: ``CompareDocuments`` 的开关参数（除前四个之外都是"要不要比这些"）。
#: 顺序照 VBA 的签名：Destination, Granularity, CompareFormatting, CompareCaseChanges,
#: CompareWhitespace, CompareTables, CompareHeaders, CompareFootnotes, CompareTextboxes,
#: CompareFields, CompareComments, CompareMoves。
#: **格式默认不比**（CompareFormatting=False）：写论文/报告时"整段重新排了一下版"
#: 会把修订标记糊满整页，把真正的文字改动淹掉。
_COMPARE_FLAGS = (False, False, False, True, True, True, True, True, True, True)

#: 引擎候选（COM ProgID → 声明的种类 → 给用户看的名字）。
#: **顺序是"自动模式下的优先顺序"**：先 Word，再 WPS。
#:
#: 为什么还要按注册表核对（见 ``_probe``）：WPS 装过之后会把自己的 CLSID 挂到
#: ``Word.Application`` 上（"接管"这个入口），于是 `New-Object -ComObject Word.Application`
#: 起起来的其实是 WPS —— 而它在 `Documents.CompareDocuments` 上的签名与 Word 不同，
#: 这正是用户遇到的「找不到 CompareDocuments 的重载，参数计数为 15」的来源之一。
#: 用户后来还发现了另一半问题："我电脑上 Wps 和 Word 都有，**显示用 Word 比较，
#: 所以这里按**注册表实际指向的 exe** 决定顺序与名字，而不是看 ProgID 叫什么。
ENGINES: tuple[tuple[str, str, str], ...] = (
    ("word", "Word.Application", "Microsoft Word"),
    ("wps", "KWPS.Application", "WPS 文字"),
    ("wps", "WPS.Application", "WPS 文字"),
)


def _kind_of_exe(exe: str, declared: str) -> str:
    """按 exe 文件名认这是哪一家（认不出来就用声明的种类）。

    ``WINWORD.EXE`` → Word；路径里带 ``wps`` / ``kingsoft`` / ``金山`` → WPS。
    这一步是"菜单上写的名字对不对"的关键：装了 WPS 的机器上 ``Word.Application``
    很可能指向 ``wps.exe``，只看 ProgID 就会把 WPS 报成 Word。
    """
    name = str(exe or "").lower()
    if not name:
        return declared
    if "winword" in name:
        return "word"
    if "wps" in name or "kingsoft" in name or "金山" in name:
        return "wps"
    return declared


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
    """一次比较的结果。"""

    output: Path
    #: 程序数出来的修订条数（界面上"共 N 处修订"）
    revisions: int = 0
    #: 比较用的两份文件（原文 / 改后），只用来写提示
    original: Path | None = None
    revised: Path | None = None
    #: 是不是被留着打开（界面上那份带修订的红头文件）
    shown: bool = False
    seconds: float = 0.0
    #: 这次是**谁**干的：``"word"`` / ``"wps"`` / ``""``（未知）
    engine_kind: str = ""
    #: 程序自己报的名字与可执行文件（"Microsoft Word"、"…\WINWORD.EXE"）
    engine_name: str = ""
    engine_path: str = ""
    details: list[str] = field(default_factory=list)

    @property
    def engine_label(self) -> str:
        """界面上怎么写这次用的程序（取不到就按 kind 兜底）。"""
        if self.engine_name:
            return self.engine_name
        return {"word": "Word", "wps": "WPS 文字"}.get(self.engine_kind, "Word / WPS")

    @property
    def kind_label(self) -> str:
        """短名字（状态栏用）：``Word`` / ``WPS``。"""
        return {"word": "Word", "wps": "WPS"}.get(self.engine_kind, "") or "Word / WPS"


def is_word_file(name: str | Path) -> bool:
    """这份文件是不是"该交给 Word 去比"的类型。"""
    return Path(str(name)).suffix.lower().lstrip(".") in WORD_EXTS


# ---------------------------------------------------------------------------
# 有没有 Word
# ---------------------------------------------------------------------------

_AVAILABLE: bool | None = None
#: 探到的引擎（``(kind, ProgID, 名字)`` 列表，按会实际使用的顺序）。注册表结果缓存。
_ENGINES_FOUND: list[tuple[str, str, str]] | None = None


def _probe() -> list[tuple[str, str, str]]:
    """按注册表算出"这台机器能用哪个程序做比较、按什么顺序试"，返回
    ``[(kind, ProgID, 名字), …]``。


    * **哪个 ProgID 用哪个 CLSID，要按 COM 的规矩来**：先看 ``ProgID\\CLSID``，
      没有再跟 ``CurVer``。以前这里是反的（先 CurVer），于是"装过 WPS、它接管了
      ``Word.Application``"的机器上，探测看到的是 Word 的版本化 ProgID、
      报"能用 Word"，而 COM 真正起的是 WPS —— 用户看到的正是
      "显示用 Word 比较，打开后当时用的 wps 比较的"。
    * **指向哪个 exe 才算哪一家**：``WINWORD.EXE`` 才是 Word；路径里有 wps 的是 WPS。
    * **Word 优先，而且被接管时也要真的用上 Word**：``Word.Application`` 被别人
      接管时，Word 自己的版本化 ProgID（``CurVer`` 指的那个）往往还指着 WINWORD.EXE，
      把它作为候选排在前面 —— 这样菜单写"用 Word 比较"、点下去也确实是 Word。
    * 只保留 **exe 真的存在** 的那些（卸载残留的半截注册表项不算数）。
    """
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:                     # pragma: no cover - 非 Windows 不会走到
        return []

    found: list[tuple[str, str, str]] = []
    seen_prog: set[str] = set()

    def add(kind: str, prog_id: str) -> None:
        """能用的判据：ProgID 注册过（有 CLSID 或 CurVer）**或者**本机能找到它的 exe。

        为什么不能只认 "CLSID → LocalServer32 → exe"：**WPS 就没有 LocalServer32**
        （实测它的 ``KWPS.Application\\CLSID`` 是 ``{000209FF-0000-4b30-A977-…}``，
        里面没有 LocalServer32 / InprocServer32 / LocalServer 任何一个），
        只按那条路走就会把一台明明装着 WPS 的机器报成"没有 WPS"。
        """
        exe = _server_exe(winreg, prog_id) or _installed_exe(kind)
        registered = _registered(winreg, prog_id)
        if not exe and not registered:
            return
        real_kind = _kind_of_exe(exe, kind)
        label = "Microsoft Word" if real_kind == "word" else "WPS 文字"
        key = prog_id.lower()
        if key in seen_prog:
            return
        seen_prog.add(key)
        found.append((real_kind, prog_id, label))

    # ① 通用入口（实际是谁，按它自己的 CLSID 说话）
    generic_exe = _server_exe(winreg, "Word.Application")
    generic_kind = _kind_of_exe(generic_exe, "word")
    if generic_kind == "word":
        add("word", "Word.Application")
    else:
        # ② 通用入口被 WPS 接管了：把版本化的 Word ProgID（CurVer 那个）补进来 ——
        # 它往往还指着 WINWORD.EXE，**那才是真 Word**。这样菜单写"用 Word 比较"、
        versioned = _curver(winreg, "Word.Application")
        if versioned and versioned.lower() != "word.application":
            add("word", versioned)
        # ③ 被接管的那一刻，通用入口本身就是能用的 WPS，排在真 Word 后面
        add("wps", "Word.Application")
    # ④ WPS 自己的 ProgID
    for kind, prog_id, _label in ENGINES[1:]:
        add(kind, prog_id)
    return found


def _registered(winreg, prog_id: str) -> bool:
    """这个 ProgID 注册过没有（有 ``CLSID`` 或 ``CurVer`` 就算，看默认值没用 ——
    默认值是一句描述文字，比如 "WPS Writer Application Class"）。"""
    for path in (rf"{prog_id}\CLSID", rf"{prog_id}\CurVer", prog_id):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path):
                return True
        except OSError:
            continue
    return False


#: 常见的安装位置（注册表查不到 exe 时按这个找）。
#: 为什么要有它：WPS 的 CLSID 里没有 LocalServer32，能找到 wps.exe 就够我们
#: 判断"这台机器能用 WPS、而且它在哪个目录" —— 菜单上写的名字、回话里的说明都靠它。
_INSTALL_PATTERNS: dict[str, tuple[str, ...]] = {
    "word": (
        r"%ProgramFiles%\Microsoft Office\root\Office*\WINWORD.EXE",
        r"%ProgramFiles(x86)%\Microsoft Office\root\Office*\WINWORD.EXE",
        r"%ProgramFiles%\Microsoft Office\Office*\WINWORD.EXE",
        r"%ProgramFiles(x86)%\Microsoft Office\Office*\WINWORD.EXE",
    ),
    "wps": (
        r"%LOCALAPPDATA%\Kingsoft\WPS Office\*\office6\wps.exe",
        r"%APPDATA%\Kingsoft\WPS Office\*\office6\wps.exe",
        r"%ProgramFiles%\Kingsoft\WPS Office\*\office6\wps.exe",
        r"%ProgramFiles(x86)%\Kingsoft\WPS Office\*\office6\wps.exe",
        r"%ProgramFiles%\WPS Office\*\office6\wps.exe",
    ),
}


def _installed_exe(kind: str) -> str:
    """按常见安装位置找这个程序的 exe（取第一个存在的）；找不到返回空串。"""
    for pattern in _INSTALL_PATTERNS.get(kind, ()):
        expanded = os.path.expandvars(pattern)
        if "%" in expanded:                # 环境变量没定义（比如 32 位的 ProgramFiles(x86)）
            continue
        for hit in sorted(glob.glob(expanded), reverse=True):   # 版本号大的优先
            if Path(hit).is_file():
                return hit
    return ""


def _curver(winreg, prog_id: str) -> str:
    """``ProgID\\CurVer`` 指的版本化 ProgID（没有就空串）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\CurVer") as key:
            value, _kind = winreg.QueryValueEx(key, "")
            return str(value or "")
    except OSError:
        return ""


def _server_exe(winreg, prog_id: str) -> str:
    """这个 ProgID 实际会启动哪个 exe（注册表 ``LocalServer32``）；取不到返回空串。

    找 CLSID 的顺序**照 COM 的规矩**：先 ``ProgID\\CLSID``（``CLSIDFromProgID`` 就是
    先看它），没有再跟 ``CurVer`` 到版本化 ProgID 去要。反过来会让"被 WPS 接管的
    """
    clsid = ""
    for name in (prog_id, _curver(winreg, prog_id)):
        if not name:
            continue
        for path in (rf"{name}\CLSID", name):
            try:
                with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, path) as key:
                    value, _kind = winreg.QueryValueEx(key, "")
                    if value and str(value).startswith("{"):
                        clsid = str(value)
                        break
            except OSError:
                continue
        if clsid:
            break
    if not clsid:
        return ""

    for sub in ("LocalServer32", "InprocServer32"):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\{sub}") as key:
                value, _kind = winreg.QueryValueEx(key, "")
                return _exe_from_command(str(value or ""))
        except OSError:
            continue
    return ""


def _exe_from_command(command: str) -> str:
    """把 ``"C:\\…\\WINWORD.EXE" /Automation`` 里的 exe 抠出来。"""
    text = command.strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text.strip('"')
    return text.split(" /")[0].split(" -")[0].strip()


def engines(refresh: bool = False) -> list[tuple[str, str, str]]:
    """这台机器能用哪些程序做比较，按**会实际使用的顺序**：
    ``[("word", "Word.Application", "Microsoft Word"), …]``。

    结果缓存：注册表不会在程序运行期间变（``refresh=True`` 强制重查）。
    """
    global _ENGINES_FOUND
    if _ENGINES_FOUND is None or refresh:
        _ENGINES_FOUND = _probe()
    return list(_ENGINES_FOUND)


def available(refresh: bool = False) -> bool:
    """本机能不能用 Word / WPS 的比较功能（Windows + 有注册好的 COM 服务器）。"""
    global _AVAILABLE
    if _AVAILABLE is not None and not refresh:
        return _AVAILABLE
    found = engines(refresh=refresh)
    _AVAILABLE = bool(found)
    return _AVAILABLE


def engine_label() -> str:
    """界面上写哪一个名字：**按真正会用的那一个**写。

    只认 ``engines()`` 的第一个（那就是脚本会先用、也最可能成功的那个）：
    "显示用 Word 比较，打开后当时用的 wps 比较的"这种对不上。
    """
    found = engines()
    if not found:
        return ""
    kinds = {kind for kind, _prog, _label in found}
    first_kind = found[0][0]
    if kinds == {"word"}:
        return "Word"
    if kinds == {"wps"}:
        return "WPS 文字"
    return "Word" if first_kind == "word" else "WPS 文字"


def engine_progids() -> list[str]:
    """交给脚本的 ProgID 候选顺序（``;`` 分隔），脚本照这个顺序试。"""
    return [prog for _kind, prog, _label in engines()]


#: 两套程序的名字（界面上就那么写）
KIND_LABELS = {"word": "Word", "wps": "WPS"}


def kinds(refresh: bool = False) -> list[str]:
    """本机**装了**哪几套（``["word"]`` / ``["wps"]`` / ``["word", "wps"]`` / ``[]``）。

    在菜单里做两个吧，没有就灰掉"）：这要的是**一家一家**的答案，
    而 :func:`available` 只回答"有没有任意一套"。
    """
    found: list[str] = []
    for kind, _prog, _label in engines(refresh=refresh):
        if kind not in found:
            found.append(kind)
    return found


def has_kind(kind: str, refresh: bool = False) -> bool:
    """本机装了 ``kind`` 这一套吗（``"word"`` / ``"wps"``）。"""
    return kind in kinds(refresh=refresh)


def kind_label(kind: str) -> str:
    """``"word"`` → ``"Word"``；``"wps"`` → ``"WPS"``。"""
    return KIND_LABELS.get(kind, kind)


def kind_reason(kind: str) -> str:
    """这一套用不了时，写进置灰项的提示里的一句人话。

    "我这台机器没装它"这一个结论，以及"为什么这一项本来就是灰的"。
    """
    label = kind_label(kind)
    if sys.platform != "win32":
        return f"「用 {label} 比较」只在 Windows 上可用（这一项要调用本机安装的 {label}）"
    return (f"这台机器上没有找到 {label}：这一项要调用本机 {label} 的「审阅 → 比较」，"
            f"装上 {label} 之后这里就能点了（也可以先用底侧「各稿」窗口里的内置比较）")


def engine_hint() -> str:
    """菜单提示里那句"会用谁比、不行会换谁"。

    先用的那个就是菜单标题写的那一个，后面的是备选。
    """
    found = engines()
    if not found:
        return ""
    labels: list[str] = []
    for _kind, _prog, label in found:
        if label not in labels:
            labels.append(label)
    if len(labels) == 1:
        return f"用本机的 {labels[0]} 比较"
    return (f"优先用 {labels[0]}；它起不来或正忙时会自动换 {' / '.join(labels[1:])}"
            "（比完会在状态栏里写明这次是谁比的）")


def unavailable_reason() -> str:
    """用不了时，一句能写进菜单提示的人话。"""
    if sys.platform != "win32":
        return "Word 比较只在 Windows 上可用（需要本机安装 Microsoft Word 或 WPS）"
    return ("没有找到 Microsoft Word，也没有找到 WPS：这一项要调用本机程序的"
            "「审阅 → 比较」功能，装好 Word 或 WPS 之后这里就能点了")


# ---------------------------------------------------------------------------
# PowerShell 脚本（**纯 ASCII**，路径走环境变量）
# ---------------------------------------------------------------------------

#: 脚本里给 Python 看的回话格式（stdout 一行 + 报告文件）
_OK_MARK = "TOOAK-OK"
_ERR_MARK = "TOOAK-ERR"
#: 回话的第二行：这次是拿哪个程序比的（``kind|名字|exe 路径``）
_ENGINE_MARK = "TOOAK-ENGINE"

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
        [System.IO.File]::AppendAllText(
            $reportPath, $text + [Environment]::NewLine,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
    Write-Output $text
}

$origPath = Get-EnvText 'TOOAK_WORD_ORIG'
$revPath  = Get-EnvText 'TOOAK_WORD_REV'
$outPath  = Get-EnvText 'TOOAK_WORD_OUT'
$reportPath = Get-EnvText 'TOOAK_WORD_REPORT'
$author   = Get-EnvText 'TOOAK_WORD_AUTHOR'
$keepOpen = (Get-EnvText 'TOOAK_WORD_VISIBLE') -eq '1'
$prefer   = (Get-EnvText 'TOOAK_WORD_ENGINE').ToLower()

if (-not $origPath -or -not $revPath -or -not $outPath) {
    Write-Report 'TOOAK-ERR missing TOOAK_WORD_ORIG / TOOAK_WORD_REV / TOOAK_WORD_OUT'
    exit 2
}
if (-not $author) { $author = 'ToOak' }

# 报告文件是**追加**写的（每次调用先由 Python 删掉旧的），所以这里先清空
try { [System.IO.File]::WriteAllText($reportPath, '', (New-Object System.Text.UTF8Encoding($false))) } catch { }

# 要比哪些东西：格式、字母大小写、空白都不比（整段重排版会把修订标记糊满整页，
# 把真正的文字改动淹掉），表格 / 页眉页脚 / 脚注 / 文本框 / 域 / 批注 / 移动都比。
$flags = @($false, $false, $false, $true, $true, $true, $true, $true, $true, $true)

# 晚绑定调用：**这是这一版最关键的一行**。
# PowerShell 对 COM 会拿类型库去挑重载，参数个数对不上就直接报
#   「找不到"CompareDocuments"的重载，参数计数为:"15"」
# —— 因为 Word 2013+ 那个方法有 **16** 个参数（末尾多一个 IgnoreAllComparisonWarnings），
# 而按 VBA 老文档数出来的只有 15 个。改走 InvokeMember 就是把参数原样交给 IDispatch，
# 让程序自己去补可选参数：参数个数不再需要"正好对上某个重载"。
function Invoke-Late([object]$target, [string]$method, [object[]]$arguments) {
    return $target.GetType().InvokeMember(
        $method, [System.Reflection.BindingFlags]::InvokeMethod,
        $null, $target, $arguments)
}

# 按参数个数**由长到短**试：Word 2013+ 16 个 / 2007-2010 15 个 / 更早 13 个 /
# 最后兜底只给两个必填的，其余交给程序默认。
function Compare-Documents([object]$app, [object]$orig, [object]$rev) {
    $base = @($orig, $rev, __DEST__, __GRAN__)
    $sets = @(
        ($base + $flags + @($author, $true)),
        ($base + $flags + @($author)),
        ($base + $flags + @($true)),
        ($base + $flags[0..7] + @($author)),
        @($orig, $rev)
    )
    $last = ''
    foreach ($arguments in $sets) {
        try { return (Invoke-Late $app.Documents 'CompareDocuments' $arguments) }
        catch { $last = $_.Exception.Message }
    }
    throw ('CompareDocuments 失败：' + $last)
}

# 备选路：老 API ``Document.Compare(对方路径)`` —— 把修订合并进**当前这份文档**。
# Word 2000 起就有，WPS 也认；我们在**临时副本**上做，所以既不碰用户的原文件，
# 也不碰库里那份。
function Compare-InPlace([object]$doc, [string]$against) {
    $sets = @(
        @($against, $author, 1, $false),
        @($against, $author),
        @($against)
    )
    $last = ''
    foreach ($arguments in $sets) {
        try {
            Invoke-Late $doc 'Compare' $arguments | Out-Null
            return
        } catch { $last = $_.Exception.Message }
    }
    throw ('Document.Compare 失败：' + $last)
}

# 引擎候选：**由 Python 那边按注册表算好、通过环境变量给进来**
# （顺序就是"会先用哪个"；装了 WPS 的机器上 Word.Application 很可能被它接管，
#  所以这里不能写死 —— 见 wordcompare._probe）。
$order = @()
foreach ($prog in ((Get-EnvText 'TOOAK_WORD_ENGINES') -split ';')) {
    $clean = $prog.Trim()
    if ($clean) { $order += @{ kind = ''; prog = $clean } }
}
if ($order.Count -eq 0) {
    # 没给（手工跑脚本、或探测失败）：退回最常见的三个
    $order = @(
        @{ kind = 'word'; prog = 'Word.Application' },
        @{ kind = 'wps';  prog = 'KWPS.Application' },
        @{ kind = 'wps';  prog = 'WPS.Application' }
    )
}
if ($prefer -eq 'wps') { $order = @($order | Where-Object { $_ -match 'wps' }) + @($order | Where-Object { $_ -notmatch 'wps' }) }

# "目标程序正忙"这一类错误：等一会儿重试**同一个引擎**，而不是立刻换下一个。
# 为什么重要：Word 还开着别的文档、页面上有个模态框、上一个自动化实例还没就绪时，
# COM 会回 RPC_E_CALL_REJECTED / RPC_E_SERVERCALL_RETRYLATER。这时候直接换引擎，
# 用户就会看到"菜单写的是 Word，结果 WPS 打开了"（他报过这个）——
# 那不是真不行，只是没等它。
function Test-Busy([string]$text) {
    return ($text -match '(?i)0x80010001|0x8001010A|rejected|busy|retrylater|被拒绝|忙')
}

$app = $null
$orig = $null
$rev = $null
$cmp = $null
$tmpPath = ''
$revisions = -1
$failure = ''
$engineKind = ''
$engineName = ''
$enginePath = ''

$done = $false
foreach ($candidate in $order) {
    if ($done) { break }
    # 同一个引擎最多试 3 次（第 2、3 次是"它刚才正忙"）
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        $failure = ''
        try {
            $app = New-Object -ComObject $candidate.prog
        } catch {
            $busy = Test-Busy $_.Exception.Message
            $failure = '起不了 ' + $candidate.prog + '：' + $_.Exception.Message
            if ($busy -and $attempt -lt 3) { Start-Sleep -Milliseconds 2000; continue }
            break                        # 换下一个引擎
        }
        try {
            try { $engineName = [string]$app.Name } catch { $engineName = '' }
            try { $enginePath = [string]$app.Path } catch { $enginePath = '' }
            $engineKind = $candidate.kind
            if (-not $engineKind) {
                if ($candidate.prog -match '(?i)wps') { $engineKind = 'wps' } else { $engineKind = 'word' }
            }
            # ProgID 写着 Word 但实际是 WPS 在干活：如实报成 wps（这是用户报过的那件事）
            if (($enginePath + $engineName) -match '(?i)wps|kingsoft|金山') { $engineKind = 'wps' }
            if (($enginePath + $engineName) -match '(?i)winword') { $engineKind = 'word' }
            $app.Visible = $false
            $app.DisplayAlerts = 0
            # ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False
            # ReadOnly 很关键：原文件绝不会因为"比一次"而被改到或锁住。
            $orig = $app.Documents.Open($origPath, $false, $true, $false)
            $rev = $app.Documents.Open($revPath, $false, $true, $false)
            $cmp = $null
            $firstError = ''
            try {
                $cmp = Compare-Documents $app $orig $rev
                $cmp.SaveAs2($outPath, __FORMAT__)
                $revisions = $cmp.Revisions.Count
                $done = $true
            } catch {
                # 这条路不行（比如 WPS 没实现 CompareDocuments）：换老 API 在临时副本上比
                $firstError = $_.Exception.Message
                try { if ($orig) { $orig.Close(0) } } catch { }
                try { if ($rev) { $rev.Close(0) } } catch { }
                $orig = $null
                $rev = $null
                $tmpPath = [System.IO.Path]::Combine(
                    [System.IO.Path]::GetTempPath(),
                    ('tooak-cmp-' + [System.Guid]::NewGuid().ToString('N') + '.docx'))
                Copy-Item -LiteralPath $revPath -Destination $tmpPath -Force
                $rev = $app.Documents.Open($tmpPath, $false, $false, $false)
                try {
                    Compare-InPlace $rev $origPath
                    $revisions = $rev.Revisions.Count
                    $rev.SaveAs2($outPath, __FORMAT__)
                    $cmp = $rev
                    $done = $true
                } catch {
                    # 两条路都不行：**两句话都留着**（只说后半句会把原因指偏）
                    $failure = $firstError + ' / ' + $_.Exception.Message
                }
            }
        } catch {
            $failure = $_.Exception.Message
        }
        if ($done) { break }
        # 收摊：留着半开的文档会占住用户的文件
        try { if ($cmp) { $cmp.Close(0) } } catch { }
        try { if ($orig) { $orig.Close(0) } } catch { }
        try { if ($rev -and $rev -ne $cmp) { $rev.Close(0) } } catch { }
        try { if ($app) { $app.Quit() } } catch { }
        $app = $null
        $orig = $null
        $rev = $null
        $cmp = $null
        if ($tmpPath) { try { Remove-Item -LiteralPath $tmpPath -Force } catch { } $tmpPath = '' }
        if ((Test-Busy $failure) -and $attempt -lt 3) {
            Start-Sleep -Milliseconds 2000
            continue                     # 同一个引擎再试一次
        }
        break                            # 换下一个引擎
    }
}

if ($revisions -ge 0 -and $app) {
    # 结果文件另存之后才是用户要看的东西：让 Word 打开**结果文件**（不是临时副本）
    if ($keepOpen) {
        try {
            $shown = $app.Documents.Open($outPath, $false, $true, $false)
            if ($cmp -and ($tmpPath -ne '') -and ($cmp.FullName -ne $shown.FullName)) {
                try { $cmp.Close(0) } catch { }
            }
            try { $shown.Activate() } catch { }
        } catch { }
        try { $app.Visible = $true } catch { }
        try { if ($orig) { $orig.Close(0) } } catch { }
        Write-Report ('TOOAK-OK ' + $revisions + ' 1')
        Write-Report ('TOOAK-ENGINE ' + $engineKind + '|' + $engineName + '|' + $enginePath)
        if ($tmpPath) { try { Remove-Item -LiteralPath $tmpPath -Force } catch { } }
        exit 0
    }
    try { if ($cmp) { $cmp.Close(0) } } catch { }
    try { if ($orig) { $orig.Close(0) } } catch { }
    try { if ($rev -and $rev -ne $cmp) { $rev.Close(0) } } catch { }
    try { $app.Quit() } catch { }
    if ($tmpPath) { try { Remove-Item -LiteralPath $tmpPath -Force } catch { } }
    Write-Report ('TOOAK-OK ' + $revisions + ' 0')
    Write-Report ('TOOAK-ENGINE ' + $engineKind + '|' + $engineName + '|' + $enginePath)
    exit 0
}

# 全都失败了：能关的都关掉，别在后台留一个看不见的进程占着文档
try { if ($cmp) { $cmp.Close(0) } } catch { }
try { if ($orig) { $orig.Close(0) } } catch { }
try { if ($rev -and $rev -ne $cmp) { $rev.Close(0) } } catch { }
try { if ($app) { $app.Quit() } } catch { }
if ($tmpPath) { try { Remove-Item -LiteralPath $tmpPath -Force } catch { } }
if (-not $failure) { $failure = 'Word 与 WPS 都没能完成比较（都装了吗？）' }
Write-Report ('TOOAK-ERR ' + $failure)
exit 1
""".replace("__DEST__", str(_WD_COMPARE_DEST_NEW)) \
   .replace("__GRAN__", str(_WD_GRANULARITY_WORD)) \
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
    engine: str = "",
) -> WordCompareResult:
    """用 Word（或 WPS）比较两份文档，把**带修订标记的结果**存成一份新 docx。

    :param original: 旧的那一份（"原始文档"）
    :param revised: 新的那一份（"修订的文档"）
    :param output: 结果存哪；不给就用 :func:`suggest_output_path`
    :param author: 修订标记挂在谁名下（程序默认是当前用户名，这里写清是 ToOak 生成的）
    :param show: True = 比较完把程序留着打开给用户看（界面默认）；
        False = 存完就关掉（测试 / 无人值守用）
    :param engine: ``""`` = 自动（Word 优先，失败换 WPS）；``"word"`` / ``"wps"``
        指定优先用哪一个（两边都试，指定的排前面）
    :raises WordCompareError: 没装程序、起不来、文档打不开、超时……一律带人话

    **两份原文件都只以只读方式打开**（``ReadOnly=True``），程序不会改它们，
    也不会因为它们已经开在用户的窗口里而失败。
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
        # 指定优先用哪个引擎（""=自动：按 Python 算好的顺序，Word 优先）
        "TOOAK_WORD_ENGINE": engine or "",
        # 引擎候选 ProgID（分号分隔）：由 _probe 按注册表算出"谁真的能用、先用谁"，
        # 脚本不再写死 Word.Application —— 装了 WPS 的机器上那个入口可能已经易主
        "TOOAK_WORD_ENGINES": ";".join(engine_progids()) if not engine
        else ";".join([prog for kind, prog, _label in engines() if kind == engine]
                      or engine_progids()),
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
        kind, name, path = _parse_engine(answer)
        return WordCompareResult(
            output=out_path,
            revisions=revisions,
            original=original_path,
            revised=revised_path,
            shown=match.group(2) == "1",
            seconds=seconds,
            engine_kind=kind,
            engine_name=name,
            engine_path=path,
        )

    reason = _explain(answer, stderr, completed.returncode)
    raise WordCompareError(reason, detail=_tail(answer, stderr))


def _read_report(path: Path) -> str:
    """读回脚本写的那份"回话文件"（UTF-8，没有编码猜测）。"""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _parse_engine(answer: str) -> tuple[str, str, str]:
    """从回话里读出"这次是拿哪个程序比的"：``TOOAK-ENGINE kind|名字|exe``。"""
    match = re.search(rf"{_ENGINE_MARK}\s*([^|\r\n]*)\|([^|\r\n]*)\|([^\r\n]*)", answer)
    if not match:
        return "", "", ""
    return (match.group(1).strip(), match.group(2).strip(), match.group(3).strip())


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

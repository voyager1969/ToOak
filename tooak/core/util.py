"""通用工具：时间、字节数、文件名安全化、内容哈希。

时间约定
--------
库内所有时间统一存 UTC ISO-8601 文本（形如 ``2025-01-01T04:05:06Z``），
只在界面/CLI 展示时转成本地时间。这样跨时区、跨设备同步时排序不会错乱。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path

__all__ = [
    "utcnow_iso",
    "to_iso",
    "parse_iso",
    "to_local_str",
    "human_size",
    "human_time_ago",
    "safe_filename",
    "stem_of",
    "suffix_of",
    "unique_path",
    "sha256_file",
    "sha256_bytes",
    "short_hash",
    "is_subpath",
    "normalize_name_for_match",
    "name_similarity",
]

#: 文件系统非法字符（Windows 侧最严格，直接按 Windows 规则来）
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
#: Windows 保留设备名
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def utcnow_iso() -> str:
    """当前时刻的 UTC ISO-8601 字符串。"""
    return to_iso(datetime.now(timezone.utc))


def to_iso(dt: datetime) -> str:
    """把 datetime 转成库内统一的 UTC ISO-8601 文本。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime | None:
    """解析库内时间文本；无法解析时返回 None（不抛异常，方便旧库容错）。"""
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_local_str(value: str | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """把库内时间转成本地时间字符串，供界面展示。"""
    dt = parse_iso(value)
    if dt is None:
        return ""
    return dt.astimezone().strftime(fmt)


def human_size(num: int | float | None) -> str:
    """把字节数格式化成便于阅读的形式。"""
    if num is None:
        return "-"
    size = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def human_time_ago(value: str | None) -> str:
    """相对时间描述，如「3 分钟前」。"""
    dt = parse_iso(value)
    if dt is None:
        return ""
    delta = datetime.now(timezone.utc) - dt
    seconds = delta.total_seconds()
    if seconds < 0:
        return to_local_str(value)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    if seconds < 86400 * 30:
        return f"{int(seconds // 86400)} 天前"
    if delta < timedelta(days=365):
        return f"{int(seconds // (86400 * 30))} 个月前"
    return f"{int(seconds // (86400 * 365))} 年前"


def safe_filename(name: str, fallback: str = "未命名") -> str:
    """把任意文本清洗成可安全落盘的文件名（保留中文，保留开头的点）。

    只清两样东西：非法字符（含控制字符）与首尾空白。
    **刻意不 strip 点**：``.env``、``.gitignore`` 本身就是合法文件名，
    只有"整名都是点"（``.`` / ``..`` 这种有目录语义的名字）才用 ``fallback``。
    """
    cleaned = _ILLEGAL_CHARS.sub("_", str(name)).strip()
    if not cleaned.strip("."):
        cleaned = fallback
    if cleaned.split(".")[0].upper() in _RESERVED_NAMES:
        # Windows 上 CON/NUL/COM1… 是设备名，建文件一定失败 —— 加下划线让开
        cleaned = "_" + cleaned
    # **先截断、再 rstrip(" .")**：截断位置可能正好落在点或空格上，
    # 而 Windows 建不出以点/空格结尾的名字（它会静默变成另一个名字，
    # 用户看到的是"导出的文件名少了一截"）。截断后必须再收一次尾。
    return cleaned[:180].rstrip(" .") or fallback


def stem_of(path: str | Path) -> str:
    """取文件主名（不含扩展名）。"""
    return Path(str(path)).stem


def suffix_of(path: str | Path) -> str:
    """取扩展名（小写，不含点），无扩展名时返回空串。"""
    return Path(str(path)).suffix.lower().lstrip(".")


def unique_path(path: str | Path) -> Path:
    """若目标已存在，则追加 `` (2)``、`` (3)`` … 直到不冲突。"""
    target = Path(path)
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    index = 2
    while True:
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def sha256_file(path: str | Path, chunk_size: int = 1 << 20) -> tuple[str, int]:
    """流式计算文件 sha256，返回 ``(sha256, 字节数)``。

    流式是为了不在内存里放大体积，几百 MB 的图纸也不会吃满内存。
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def sha256_bytes(data: bytes) -> str:
    """计算内存中数据的 sha256。"""
    return hashlib.sha256(data).hexdigest()


def short_hash(sha: str | None, length: int = 8) -> str:
    """哈希短显示形式。"""
    return (sha or "")[:length]


def is_subpath(child: str | Path, parent: str | Path) -> bool:
    """判断 child 是否位于 parent 目录之内（用于防止把库目录归档进自己）。"""
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except (ValueError, OSError):
        return False


# ---------------------------------------------------------------------------
# 文件名"像不像同一份东西"
# ---------------------------------------------------------------------------
#
# 用户的实际习惯是**每稿都改文件名**：`提交_v2.docx` → `提交-20250701终版.docx`，
# 而库里"上一稿的那一份是哪个"要认得出是同一条线 —— 严格同名配对会让
# "比较功能，匹配名字，更智能一点，现在名字有更改就匹配不上，模糊匹配"）。
#
# 做法：先把"日期、版本号、提交/终版/修改这类状态词"这些**噪音**去掉，
# 剩下的核心词再比相似度。只做保守的相似度判断（宁可配不上，也不硬配一对
# 不相干的文件 —— 配错了比配不上更糟）。

#: 文件名里的噪音之一：日期（20250623 / 2025-06-23 / 2025年6月23日）
_NAME_DATE = re.compile(r"\d{4}\s*[-_.年]?\s*\d{1,2}\s*[-_.月]?\s*\d{1,2}\s*日?")
#: 版本号：v2 / v2.1 / ver3 / rev1 / 第2版 / 第3稿
_NAME_VERSION = re.compile(r"(?<![a-z])(?:v|ver|version|rev|r)\.?\s*\d+(?:\.\d+)*")
_NAME_EDITION = re.compile(r"第\s*\d+\s*[版稿次]")
#: 这类词本身不区分是哪一份（"提交_v2" 与 "提交_v3" 是同一份交付物的两稿）。
#: **长的写在前**：替换是逐个 ``str.replace``，先换掉"最终版"才轮得到"终版"，
#: 否则"最终版"会被啃成"最"（实测漏过一个"最"字）。
_NAME_NOISE_WORDS = (
    "最终版", "最终", "终版", "定稿", "提交", "修改", "修订", "新版", "旧版",
    "初稿", "草稿", "模板", "副本", "备份", "更新", "完成", "补充",
    "final", "draft", "copy", "revised", "updated", "new", "old",
)
#: 分隔符与括号一律忽略（"-"、"_"、"（补充）"这些不该影响"像不像"）
_NAME_SEPARATORS = re.compile(r"[\s\-_—–~·,，.。、:：;；!！?？'\"“”‘’()（）\[\]【】{}<>《》]+")


def normalize_name_for_match(name: str) -> str:
    """把文件名削成"用来判断像不像"的核心词（去掉日期、版本号、提交/终版这类噪音）。

    ``提交_v2.docx`` → ``''``；``结构设计说明-20250701终版`` → ``结构设计说明``；
    ``现场照片(2)`` → ``现场照片``。返回空串表示"这个名字剩下的全是噪音"。
    """
    text = Path(str(name)).stem.lower()
    text = _NAME_DATE.sub(" ", text)
    text = _NAME_EDITION.sub(" ", text)
    text = _NAME_VERSION.sub(" ", text)
    for word in _NAME_NOISE_WORDS:
        text = text.replace(word, " ")
    return _NAME_SEPARATORS.sub("", text)


def name_similarity(left: str, right: str) -> float:
    """两个文件名的相似度（0~1），**忽略日期 / 版本号 / 提交终版这类噪音**。

    判定口径（保守）：

    * 噪音去掉后两边都空（``提交_v2`` 对 ``提交_v3``）→ 0.9：
      它们是同一份交付物的两稿，这正是最该配上的一类；
    * 一边空、一边有内容 → 拿**原始主名**比（``提交_v2`` 对 ``报告`` → 低分）；
    * 一边包含另一边（``报告`` ⊂ ``报告终版``）→ 抬到 0.8；
    * 其余按 ``SequenceMatcher`` 的比例。
    """
    core_left = normalize_name_for_match(left)
    core_right = normalize_name_for_match(right)
    if not core_left and not core_right:
        return 0.9
    if not core_left or not core_right:
        core_left = Path(str(left)).stem.lower()
        core_right = Path(str(right)).stem.lower()
        if core_left == core_right:
            return 0.9
    ratio = SequenceMatcher(None, core_left, core_right).ratio()
    if core_left and core_right and (core_left in core_right or core_right in core_left):
        ratio = max(ratio, 0.8)
    return ratio

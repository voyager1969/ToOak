"""内容差异与文本抽取。

用途
----
1. 界面里比较同一个文档的两个版本（这是「看到它在不同时期的状态」的核心）；
2. 把文本内容抽出来缓存进 ``version_text`` 表，支撑全文检索。

设计取舍
--------
* 只依赖标准库；Office 文档本身就是 zip + XML，直接解包抽文本即可，
  不引入 python-docx / python-pptx 这类额外依赖（xlsx 优先用 openpyxl，其次退化到 XML 解析）。
* PDF 抽文本需要第三方库，缺失时**不报错**，退化为「二进制摘要」（大小、哈希变化）。
* 二进制文件（图纸、模型、压缩包）不做逐字节 diff，给出大小/哈希级别的变化说明。
"""

from __future__ import annotations

import difflib
import importlib.util
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .util import human_size, short_hash

__all__ = [
    "DiffResult",
    "FileProfile",
    "describe_file",
    "extract_text",
    "is_texty",
    "diff_texts",
    "diff_files",
    "summarize_change",
]

#: 直接按文本读取的扩展名
TEXT_EXTS = {
    "txt", "md", "markdown", "rst", "log", "csv", "tsv", "json", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "xml", "html", "htm", "css", "js", "ts", "py",
    "c", "h", "cpp", "hpp", "cc", "java", "cs", "go", "rs", "sh", "bat", "sql",
    "m", "tex", "srt", "v", "sv", "vhd", "svh", "qml", "ui", "cmake", "gradle",
}

#: Office 开放格式（本质是 zip + XML）：正文 XML 单独限长，不看整包大小
OOXML_EXTS = {"docx", "xlsx", "pptx", "odt", "ods", "odp"}

#: 解压后的单个 XML 成员上限（防止畸形文档把内存吃满）
_MAX_XML_BYTES = 32 * 1024 * 1024

#: 图片扩展名（差异以元数据形式呈现）
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp", "tif", "tiff", "svg"}

#: 明确当作二进制处理的扩展名（避免误抽文本产生噪音）
BINARY_EXTS = {
    "zip", "7z", "rar", "gz", "tar", "bz2", "xz", "exe", "dll", "so", "bin",
    "iso", "db", "sqlite", "sqlite3", "step", "stp", "iges", "igs", "stl",
    "sldprt", "sldasm", "dwg", "dxf", "prt", "catpart", "mp4", "avi", "mkv",
    "mp3", "wav", "flac", "ttf", "otf", "woff", "woff2",
}

_MAX_TEXT_BYTES = 8 * 1024 * 1024  # 单文件抽取上限，防止超大日志卡界面

#: 算"相似度"的行数上限。相似度只用来在 summary 里印一行百分比
#: （:func:`diff_texts`），而 ``SequenceMatcher.ratio()`` 在几万行上是秒级开销，
#: 不值得为了一个装饰性的数字把界面拖住。
_RATIO_MAX_LINES = 50000


@dataclass
class FileProfile:
    """一个文件的画像，用于差异面板的「这是什么文件」说明。"""

    name: str
    ext: str = ""
    size: int = 0
    mime: str = ""
    texty: bool = False
    detail: str = ""

    @property
    def size_text(self) -> str:
        return human_size(self.size)


#: 耗时过长，把界面卡死，可以单独做一个线程在后台比较，显示差异"）。
#: 自动比较是"切到哪一稿就顺手算一下"，大文件一算就是几十秒，宁可让用户点一下再算。
AUTO_DIFF_MAX_BYTES = 6 * 1024 * 1024
#: 逐字节校验（sha256）的大小上限：更大的文件只比大小与时间，避免为了"证明没变"而读一遍几十 GB
DEEP_CHECK_MAX_BYTES = 64 * 1024 * 1024


def oversized_reason(old_size: int, new_size: int) -> str:
    """要不要"先不自动比"？返回原因（空串 = 可以自动比）。"""
    from .util import human_size

    biggest = max(int(old_size or 0), int(new_size or 0))
    if biggest <= AUTO_DIFF_MAX_BYTES:
        return ""
    return (f"文件较大（{human_size(biggest)}）—— 已跳过自动比较，"
            "可以点「在后台比较」再算")


@dataclass
class DiffResult:
    """两个版本（或两份内容）的比较结果。"""

    kind: str = "binary"          # same | text | binary | mixed
    unified: str = ""             # 统一格式 diff 文本
    added: int = 0
    removed: int = 0
    old_text: str = ""
    new_text: str = ""
    summary: str = ""
    details: list[str] = field(default_factory=list)
    #: 因为"文件太大"而**没有真的比较**（界面据此给出"在后台比较"的入口）
    skipped: bool = False

    @property
    def identical(self) -> bool:
        return self.kind == "same"

    @property
    def stat_text(self) -> str:
        if self.kind == "same":
            return "内容相同"
        if self.kind == "text":
            return f"+{self.added} / -{self.removed} 行"
        return self.summary or "二进制内容变化"


# ---------------------------------------------------------------------------
# 文件画像与文本抽取
# ---------------------------------------------------------------------------


def _ext_of(name: str) -> str:
    return Path(name).suffix.lower().lstrip(".")


def is_texty(name: str, mime: str = "") -> bool:
    """判断这份文件**能不能抽出正文来比较**。

    注意口径：Office 文档（docx/xlsx/pptx）本质是 zip + XML，正文可以直接解出来，
    因此它们算"可抽文本"，不能因为 mime 是
    ``application/vnd.openxmlformats-...`` 就当二进制。
    """
    ext = _ext_of(name)
    if ext in TEXT_EXTS or ext in OOXML_EXTS:
        return True
    if ext in BINARY_EXTS or ext in IMAGE_EXTS:
        return False
    if ext == "pdf":
        # 装了 pypdf 才抽得出来，缺库时按二进制处理更诚实
        return importlib.util.find_spec("pypdf") is not None
    return mime.startswith("text/") or mime in (
        "application/json", "application/xml", "application/javascript",
    )


def describe_file(path: str | Path, name: str = "", mime: str = "") -> FileProfile:
    """给文件做一个轻量画像（不读全文）。"""
    file_path = Path(path)
    display = name or file_path.name
    ext = _ext_of(display) or _ext_of(file_path.name)
    size = file_path.stat().st_size if file_path.exists() else 0
    profile = FileProfile(
        name=display, ext=ext, size=size, mime=mime, texty=is_texty(display, mime)
    )

    try:
        if ext in IMAGE_EXTS and ext != "svg":
            from PIL import Image  # 可选依赖，缺失时跳过

            with Image.open(file_path) as image:
                profile.detail = f"{image.width}×{image.height} 像素 · {image.mode}"
        elif ext == "docx":
            profile.detail = _docx_detail(file_path)
        elif ext == "xlsx":
            profile.detail = _xlsx_detail(file_path)
        elif ext == "pdf":
            profile.detail = "PDF 文档（文本抽取需 pypdf，缺失时按二进制比较）"
        elif ext in ("step", "stp", "iges", "igs", "stl"):
            profile.detail = "三维模型文件（按二进制比较）"
        elif ext in ("zip", "7z", "rar"):
            profile.detail = "压缩包（按二进制比较）"
    except Exception:  # 画像失败不影响主流程
        profile.detail = ""

    return profile


def extract_text(path: str | Path, name: str = "", mime: str = "") -> str | None:
    """尽力把文件内容抽成纯文本；无法抽取时返回 None。

    支持：纯文本类、Word(.docx)、Excel(.xlsx)、PowerPoint(.pptx)、
    可选 PDF（装了 pypdf 才会生效）。
    """
    file_path = Path(path)
    if not file_path.is_file():
        return None
    display = name or file_path.name
    ext = _ext_of(display) or _ext_of(file_path.name)

    # 上限**与扩展名无关**：早期写成 ``size > _MAX_TEXT_BYTES and ext not in TEXT_EXTS``，
    # 等于对 .csv/.log/.json/.txt 这些"文本类"完全不设限 —— 用户库里那份 2.9 GB 的
    # 压缩包/日志会被整个读进内存（``extract_text`` 还被 ``get_version_text``
    # 和 ``/versions/<id>/text`` 调用，等于一个 HTTP 请求就能把进程吃爆）。
    # 现在的口径：非 OOXML 一律先看整文件大小。
    # OOXML（docx/xlsx/…）另算 —— 它的正文在压缩包里，包大不等于正文大
    # （一份 15 MB 的 docx 正文 XML 常常不到 1 MB，其余都是图片），
    # 对它们只看"解压出来的 XML"大小，由 :func:`_read_member` 把关。
    if ext not in OOXML_EXTS:
        try:
            if file_path.stat().st_size > _MAX_TEXT_BYTES:
                return None
        except OSError:
            return None

    try:
        if ext == "docx":
            return _extract_docx(file_path)
        if ext == "pptx":
            return _extract_pptx(file_path)
        if ext == "xlsx":
            text = _extract_xlsx(file_path)
            if text is not None:
                return text
            return _extract_ooxml_generic(file_path, ("xl/sharedStrings.xml",))
        if ext == "pdf":
            return _extract_pdf(file_path)
        if ext in ("odt", "ods", "odp"):
            return _extract_ooxml_generic(file_path, ("content.xml",))
        if is_texty(display, mime):
            raw = file_path.read_bytes()
            for encoding in ("utf-8", "utf-8-sig", "gb18030", "utf-16"):
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    return None


def _strip_xml(xml_text: str, paragraph_tags: tuple[str, ...] = ("</w:p>", "</a:p>")) -> str:
    """把 OOXML 片段转成可读文本（保留段落换行）。"""
    text = re.sub(r"<w:tab[^>]*/>", "\t", xml_text)
    for tag in paragraph_tags:
        text = text.replace(tag, "\n")
    text = re.sub(r"<w:br[^>]*/>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&apos;", "'")
    # 压缩多余空行
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _read_member(archive: zipfile.ZipFile, name: str) -> str | None:
    """按名字读一个 XML 成员，先看解压后大小再读（防止内存爆炸）。"""
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _MAX_XML_BYTES:
        return None
    return archive.read(name).decode("utf-8", errors="replace")


def _extract_docx(path: Path) -> str | None:
    with zipfile.ZipFile(path) as archive:
        xml_text = _read_member(archive, "word/document.xml")
    if xml_text is None:
        return None
    return _strip_xml(xml_text)


def _extract_pptx(path: Path) -> str | None:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = sorted(
            (n for n in archive.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda item: int(re.findall(r"\d+", item)[0]),
        )
        for index, slide_name in enumerate(names, start=1):
            xml_text = _read_member(archive, slide_name)
            if xml_text is None:
                continue
            chunks.append(f"--- 幻灯片 {index} ---\n{_strip_xml(xml_text)}")
    return "\n\n".join(chunks) if chunks else None


def _extract_xlsx(path: Path) -> str | None:
    try:
        import openpyxl
    except ImportError:
        return None
    lines: list[str] = []
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in workbook.worksheets:
            lines.append(f"--- 工作表：{sheet.title} ---")
            for row in sheet.iter_rows(values_only=True):
                if row is None:
                    continue
                cells = ["" if value is None else str(value) for value in row]
                if any(cell.strip() for cell in cells):
                    lines.append("\t".join(cells))
    finally:
        workbook.close()
    return "\n".join(lines) if lines else None


def _extract_ooxml_generic(path: Path, members: tuple[str, ...]) -> str | None:
    chunks: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        for member in members:
            if member in names:
                text = _read_member(archive, member)
                if text:
                    chunks.append(_strip_xml(text))
    return "\n".join(chunk for chunk in chunks if chunk) or None


def _extract_pdf(path: Path) -> str | None:
    try:
        from pypdf import PdfReader  # 可选
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # 兼容旧名
        except ImportError:
            return None
    reader = PdfReader(str(path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(pages).strip() or None


def _docx_detail(path: Path) -> str:
    """Word 文档的章节数/字数概览。"""
    text = _extract_docx(path) or ""
    paragraphs = [line for line in text.splitlines() if line.strip()]
    return f"{len(paragraphs)} 段落 · {len(text)} 字符"


def _xlsx_detail(path: Path) -> str:
    try:
        import openpyxl

        workbook = openpyxl.load_workbook(path, read_only=True)
        try:
            names = workbook.sheetnames
            return f"{len(names)} 个工作表：{'、'.join(names[:5])}"
        finally:
            workbook.close()
    except Exception:
        return "Excel 工作簿"


# ---------------------------------------------------------------------------
# 差异计算
# ---------------------------------------------------------------------------


def diff_texts(
    old_text: str, new_text: str, *, context: int = 3, label_old: str = "旧版本",
    label_new: str = "新版本",
) -> DiffResult:
    """文本差异（统一格式）。"""
    if old_text == new_text:
        return DiffResult(kind="same", old_text=old_text, new_text=new_text,
                          summary="文本内容完全一致")

    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    unified = "\n".join(
        difflib.unified_diff(
            old_lines, new_lines, fromfile=label_old, tofile=label_new,
            lineterm="", n=context,
        )
    )

    added = removed = 0
    for line in unified.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1

    # 相似度只是 summary 里的一行装饰；行数太多就直接给固定文案，
    # 别为了这个百分比把一次比较拖成几秒（大日志实测能到十几秒）
    if max(len(old_lines), len(new_lines)) > _RATIO_MAX_LINES:
        ratio_text = "相似度未计算（行数过多）"
    else:
        ratio = difflib.SequenceMatcher(None, old_lines, new_lines).ratio()
        ratio_text = f"相似度 {ratio:.0%}"
    return DiffResult(
        kind="text",
        unified=unified,
        added=added,
        removed=removed,
        old_text=old_text,
        new_text=new_text,
        summary=f"{ratio_text}，+{added} / -{removed} 行",
    )


def diff_files(
    old_path: str | Path | None,
    new_path: str | Path | None,
    *,
    old_name: str = "",
    new_name: str = "",
    old_size: int = 0,
    new_size: int = 0,
    old_mime: str = "",
    new_mime: str = "",
    old_sha256: str = "",
    new_sha256: str = "",
) -> DiffResult:
    """比较两份文件：能抽文本就做文本 diff，否则给二进制摘要。

    ``old_sha256`` / ``new_sha256`` 是**库里早就登记过的**指纹（归档时算过一次）。
    传进来是为了不再把文件读第二遍：这里原来对两侧各做一次 sha256，
    两份上百 MB 的 Word 光这一步就是几秒 —— 而结论只是
    在详情里印一行"哈希 2d4614dd → 5f7eae11"，没人看。
    """
    if old_path is None and new_path is None:
        return DiffResult(kind="same", summary="两侧都没有内容")

    if old_path is None or new_path is None:
        side = "旧版本" if old_path is None else "新版本"
        present = new_path if old_path is None else old_path
        name = new_name or old_name or Path(str(present)).name
        profile = describe_file(present, name)
        action = "新增文件" if old_path is None else "删除文件"
        return DiffResult(
            kind="mixed",
            summary=f"{action}：{name}（{profile.size_text}）",
            details=[profile.detail] if profile.detail else [],
        )

    old_file = Path(old_path)
    new_file = Path(new_path)
    name_old = old_name or old_file.name
    name_new = new_name or new_file.name

    old_profile = describe_file(old_file, name_old, old_mime)
    new_profile = describe_file(new_file, name_new, new_mime)

    old_text = extract_text(old_file, name_old, old_mime)
    new_text = extract_text(new_file, name_new, new_mime)

    if old_text is not None and new_text is not None:
        result = diff_texts(old_text, new_text, label_old=f"{name_old}", label_new=f"{name_new}")
        if old_profile.detail or new_profile.detail:
            result.details = [
                f"旧：{old_profile.size_text} · {old_profile.detail}".strip(" ·"),
                f"新：{new_profile.size_text} · {new_profile.detail}".strip(" ·"),
            ]
        return result

    # 二进制：只用库里登记过的指纹，**不读文件**（读一遍几十上百 MB 只为印一行哈希，不值当）
    old_sha = old_sha256
    new_sha = new_sha256
    if old_sha and new_sha and old_sha == new_sha:
        return DiffResult(kind="same", summary="二进制内容一致（两侧登记的指纹相同）")

    size_old = old_size or old_profile.size
    size_new = new_size or new_profile.size
    delta = size_new - size_old
    delta_text = f"{'+' if delta >= 0 else '-'}{human_size(abs(delta))}"
    details = [
        f"旧：{human_size(size_old)} · {old_profile.detail or old_profile.ext or '二进制'}",
        f"新：{human_size(size_new)} · {new_profile.detail or new_profile.ext or '二进制'}",
        f"大小变化 {delta_text}",
    ]
    return DiffResult(
        kind="binary",
        summary=f"二进制内容已变化（{delta_text}）",
        details=details,
    )


def summarize_change(previous, current) -> str:
    """用一句话说明一个版本相对父版本改了什么，用作流程视图中边上的标签。"""
    if previous is None:
        return "创建"
    if not current.files:
        # 空稿（用户先点「＋ 新增一稿」建出来、文件还没登记进来）：
        # 照文件集合去算会得出"-3 附件"，读起来像"这一稿删掉了三个文件"，
        # 而用户什么都没删 —— 如实说它是空的。
        return "空稿（还没有文件）"
    prev_files = {item.name: item for item in previous.files}
    cur_files = {item.name: item for item in current.files}

    added = [name for name in cur_files if name not in prev_files]
    removed = [name for name in prev_files if name not in cur_files]
    changed = [
        name
        for name, item in cur_files.items()
        if name in prev_files and prev_files[name].sha256 != item.sha256
    ]

    if current.origin == "restore":
        return "回到旧版"
    if not added and not removed and not changed:
        return "无文件变化"

    parts: list[str] = []
    if changed:
        parts.append("主文件更新" if len(changed) == 1 else f"{len(changed)} 个文件更新")
    if added:
        parts.append(f"+{len(added)} 附件")
    if removed:
        parts.append(f"-{len(removed)} 附件")
    return "、".join(parts)

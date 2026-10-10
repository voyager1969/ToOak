"""文件存储：照旧版 C++（Symphytum）的归档逻辑，把文件复制进库的 ``files/``。

三种纳管方式（``FILE_MODES``）
------------------------------
* ``copy``（**库的默认**）：复制进库内的 ``files/<别名文件夹>/<原始文件名>`` ——
  **照旧版 ``FileManager::startAddFile`` 的逻辑**：给每个文件起一个别名文件夹
  （``MD5(当前时间字符串 + 文件路径)`` 的 hex），文件夹里保留**原始文件名**。
  库自带内容，可以独立备份/迁移；"哪一稿是哪一份字节"由库自己保证。
  **只复制，绝不移动/删除用户的原文件** —— 拷贝失败就报错，原文件一个字节都不动。
* ``link``：硬链接进库（同盘零拷贝）。适合"已有大体量资料想就地纳管、
  又希望库目录自成一体"的场景；代价是原文件日后被就地改写会连带改变库内容。
* ``reference``：文件留在原处，库里只登记「绝对路径 + sha256 + 大小」。
  这是历史上的默认（所以老库仍然沿用它，见 ``Library.file_mode`` 的说明）：
  不占额外空间、不改动用户的资料目录；代价是文件被就地覆盖/移走后，
  旧版本的内容就取不回来了（只剩指纹）。引用模式下库**永远不会删除用户的原文件**
  （连 gc 也不会）。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .util import safe_filename, sha256_bytes, sha256_file, suffix_of, utcnow_iso

__all__ = ["BlobInfo", "BlobStore", "FILE_MODES"]

#: 支持的纳管方式。**顺序有意义**：第一个就是"没设置过时用的那个"
#: 错误提示里列可选项时也照这个顺序念。
FILE_MODES = ("copy", "link", "reference")

#: blob 来源标记
SOURCE_REFERENCE = "reference"
SOURCE_STORE = "store"


@dataclass(frozen=True)
class BlobInfo:
    """一次落盘/登记后的结果描述。"""

    sha256: str
    size: int
    ext: str
    mime: str
    rel_path: str
    #: 'reference' = 文件留在原处；'store' = 已在库内（复制或硬链接）
    source: str = SOURCE_STORE
    #: 引用模式下记录原文件所在目录，便于界面显示与追溯
    origin_path: str = ""

    @property
    def external(self) -> bool:
        return self.source == SOURCE_REFERENCE


class BlobStore:
    """blob 库：只负责「内容怎么存」，不关心业务语义。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.files_dir = self.root / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        # 兼容旧名字（历史代码/测试可能还在用 blobs_dir）
        self.blobs_dir = self.files_dir
        # 哈希缓存：同一文件的 sha256 只算一次。
        # 迁移旧库时尤其重要 —— 十几 GB 的资料反复哈希会让导入慢得无法接受。
        self._hash_cache: dict[tuple[str, int, int], tuple[str, int]] = {}
        self._hash_cache_limit = 20000

    # ------------------------------------------------------------ 内容指纹

    def sha256_of(self, path: str | Path) -> tuple[str, int]:
        """取文件的 ``(sha256, 字节数)``，带缓存（键含大小与纳秒级 mtime）。"""
        file_path = Path(path)
        stat = file_path.stat()
        key = (str(file_path), stat.st_size, stat.st_mtime_ns)
        cached = self._hash_cache.get(key)
        if cached is not None:
            return cached
        digest, size = sha256_file(file_path)
        if len(self._hash_cache) >= self._hash_cache_limit:
            # 满了**不整体 clear()**：清空等于让后面每一个文件再哈希一遍，
            # 而旧库迁移（legacy 预取）恰恰是"先扫一遍全库、再逐个归档"，
            # 超过 20000 个文件时会白读一遍全部资料（十几 GB）。
            # dict 保序，按插入顺序丢最早的 1/10（FIFO）——
            # 最早进来的是早已处理完的文件，最不可能再被问第二次。
            for stale in list(self._hash_cache)[: max(1, self._hash_cache_limit // 10)]:
                self._hash_cache.pop(stale, None)
        self._hash_cache[key] = (digest, size)
        return digest, size

    # ------------------------------------------------------------ 路径计算

    def _alias(self, seed: str) -> str:
        """文件夹别名：照旧版 C++ 的逻辑 = ``MD5(当前时间字符串 + 来源标识)`` 的 hex。

        旧版 ``FileManager::startAddFile`` 用 ``QCryptographicHash::hash(
        QDateTime::currentDateTime().toString().toUtf8() + 文件路径, Md5)`` 作文件夹名，
        所以每个文件都有一个独立的别名文件夹，文件名保持原样。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        return hashlib.md5((now + seed).encode("utf-8")).hexdigest()

    def rel_path_for(self, seed: str, keep_name_hint: str | None = None) -> str:
        """库内相对存储路径：``files/<别名>/<原始文件名>``（照旧版 C++）。"""
        name = safe_filename(keep_name_hint or Path(seed).name or "file")
        return f"files/{self._alias(seed)}/{name}"

    def path_for(self, rel_path: str) -> Path:
        """把登记的路径解析成真实路径。

        绝对路径 = 引用模式（文件在库外），原样返回；
        相对路径 = 库内文件，拼到库根上。
        """
        return self.resolve(rel_path)

    def resolve(self, rel_path: str) -> Path:
        candidate = Path(rel_path)
        if candidate.is_absolute():
            return candidate
        return self.root / candidate

    def is_owned(self, rel_path: str) -> bool:
        """该路径是否是**库自己建的文件**（库拥有、可安全删除）。

        为什么判据收在 ``files/`` 之下，而不是"在不在库根之下"：
        库根里完全可能有**用户自己的资料** —— 用户可以把资料就放在库目录下的
        子目录里，也可能把 ``库/exports/…`` 里导出的文件重新纳管回库
        （``exports/`` 就在库根下）。那些文件是用户的，库只是登记了引用。
        库只在 ``files/<别名>/`` 里建文件，所以只认这一棵树。

        绝对路径 = 引用模式（文件在库外），**永远不算库拥有**。
        """
        candidate = Path(rel_path)
        if candidate.is_absolute():
            return False
        try:
            (self.root / candidate).resolve().relative_to(self.files_dir.resolve())
        except (ValueError, OSError):
            return False
        return True

    # ---------------------------------------------------------------- 登记

    def register_reference(self, src: str | Path, keep_name_hint: str | None = None) -> BlobInfo:
        """**就地引用**：只登记路径与内容指纹，不复制、不移动、不改动原文件。"""
        src_path = Path(src).expanduser()
        if not src_path.is_file():
            raise FileNotFoundError(f"文件不存在：{src_path}")
        resolved = src_path.resolve()
        sha256, size = self.sha256_of(resolved)
        ext = suffix_of(keep_name_hint or resolved.name)
        return BlobInfo(
            sha256=sha256,
            size=size,
            ext=ext,
            mime=self._guess_mime(ext),
            rel_path=str(resolved),
            source=SOURCE_REFERENCE,
            origin_path=str(resolved.parent),
        )

    # ---------------------------------------------------------------- 写入

    def put_file(
        self,
        src: str | Path,
        keep_name_hint: str | None = None,
        link: bool = False,
        *,
        mode: str | None = None,
    ) -> BlobInfo:
        """把文件按指定方式纳管。

        :param mode: ``reference`` / ``link`` / ``copy``（见模块文档）。
            为 None 时按 ``link`` 兼容旧调用：``link=True`` → ``link``，否则 ``copy``。
        """
        if mode is None:
            mode = "link" if link else "copy"
        if mode not in FILE_MODES:
            raise ValueError(f"未知的纳管方式：{mode}")
        if mode == "reference":
            return self.register_reference(src, keep_name_hint)

        src_path = Path(src).expanduser()
        if not src_path.is_file():
            raise FileNotFoundError(f"文件不存在：{src_path}")

        sha256, size = self.sha256_of(src_path)
        ext = suffix_of(keep_name_hint or src_path.name)
        name = safe_filename(keep_name_hint or src_path.name)
        # 照旧版 C++：files/<别名文件夹>/<原始文件名>，别名 = MD5(时间+路径)
        rel_path = self.rel_path_for(str(src_path), name)
        target = self.path_for(rel_path)

        target.parent.mkdir(parents=True, exist_ok=True)

        if mode == "link":
            try:
                os.link(src_path, target)
                return BlobInfo(sha256, size, ext, self._guess_mime(ext), rel_path,
                                SOURCE_STORE, str(src_path.parent))
            except OSError:
                pass  # 跨设备或权限不足，回落到复制

        handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        os.close(handle)
        tmp_path = Path(tmp_name)
        try:
            shutil.copyfile(src_path, tmp_path)
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        return BlobInfo(sha256, size, ext, self._guess_mime(ext), rel_path,
                        SOURCE_STORE, str(src_path.parent))

    def put_bytes(self, data: bytes, name_hint: str = "") -> BlobInfo:
        """把内存数据（笔记正文、链接描述等）写入库（照旧版 C++ 的文件夹逻辑）。"""
        sha256 = sha256_bytes(data)
        ext = suffix_of(name_hint) or "txt"
        name = safe_filename(name_hint) or f"{sha256[:8]}.{ext}"
        rel_path = self.rel_path_for(name_hint or name, name)
        target = self.path_for(rel_path)

        target.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-")
        os.close(handle)
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_bytes(data)
            os.replace(tmp_path, target)
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

        return BlobInfo(sha256, len(data), ext, self._guess_mime(ext), rel_path)

    # ---------------------------------------------------------------- 读取

    def read_bytes(self, rel_path: str) -> bytes:
        return self.resolve(rel_path).read_bytes()

    def read_text(self, rel_path: str, encoding: str = "utf-8") -> str:
        """读出文本；编码不匹配时用替换字符兜底，不抛异常。"""
        raw = self.read_bytes(rel_path)
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            return raw.decode(encoding, errors="replace")

    def export_to(self, rel_path: str, dest: str | Path) -> Path:
        """把文件导出到目标路径（保留调用方给的原始文件名）。"""
        dest_path = Path(dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.resolve(rel_path), dest_path)
        return dest_path

    def file_exists(self, rel_path: str) -> bool:
        return self.resolve(rel_path).is_file()

    # ---------------------------------------------------------------- 维护

    def delete(self, rel_path: str) -> None:
        """删除库自己拥有的文件。

        **绝对路径一律视为库外，直接返回不删**：绝对路径就是引用模式下登记的
        用户原文件，库只记着"它在哪 + 内容指纹"，删掉就是销毁用户资料，
        没有任何补救手段（用户的文件往往是自己工程目录/共享盘里唯一的一份）。
        这道守卫必须放在**本方法内部**、不依赖调用方记得判断 ——
        gc、硬删除文档、清理缓存都会走到这里，漏一个就是数据丢失。
        库内/库外的判据见 :meth:`is_owned`。

        ``files/<别名>/`` 这一层（``rmdir`` 只在目录空时成功），绝不往上删到
        ``files/`` 本身。
        """
        if Path(rel_path).is_absolute():
            return
        if not self.is_owned(rel_path):
            return
        target = self.resolve(rel_path)
        target.unlink(missing_ok=True)
        parent = target.parent
        if parent != self.files_dir:
            try:
                parent.rmdir()          # 只删空目录；不空/不存在就抛 OSError，忽略
            except OSError:
                pass

    def iter_blobs(self) -> list[Path]:
        """库内拥有的文件（不含库外的引用）。"""
        if not self.files_dir.exists():
            return []
        return [p for p in self.files_dir.rglob("*") if p.is_file()]

    def total_size(self) -> int:
        return sum(p.stat().st_size for p in self.iter_blobs())

    def stats(self) -> dict[str, int]:
        files = self.iter_blobs()
        return {"count": len(files), "bytes": sum(p.stat().st_size for p in files)}

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def guess_mime(ext: str) -> str:
        """按扩展名猜 mime（公开版：改名之后 ext/mime 要跟着走，见 ``Library.rename_file``）。"""
        return BlobStore._guess_mime(ext)

    @staticmethod
    def _guess_mime(ext: str) -> str:
        if not ext:
            return "application/octet-stream"
        guessed, _ = mimetypes.guess_type(f"x.{ext}")
        return guessed or "application/octet-stream"

    @staticmethod
    def stamp() -> str:
        return utcnow_iso()

    @staticmethod
    def clean_name(name: str) -> str:
        return safe_filename(name)

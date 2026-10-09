"""ToOak —— 面向工程项目的文档管理器。

设计要点（详见 docs/DESIGN.md）：
  * 内核（tooak.core）不依赖任何 GUI 框架，可被桌面端、CLI、HTTP API 共同复用；
  * 库（library）由一个 SQLite 文件 + 内容寻址的 blob 目录构成，语言中立、可迁移；
  * 文档以「标题」为身份：同一节点下同名文件再次归档 -> 自动成为该文档的新版本。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

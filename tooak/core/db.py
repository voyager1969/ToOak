"""SQLite 存储层：建库、结构迁移、连接与事务。

结构设计说明
------------
* 层级：``projects`` -> ``nodes``（节点可自嵌套） -> ``documents`` -> ``versions``；
* 一个「版本」是一份**文件集合**（``version_files``），单文件时只有一个 primary，
  也可以带附件 —— 这是「多种形式的文件归档」的基础；
* 文件内容存于内容寻址的 blob 库（见 store.py），库内只存 sha256 引用，
  相同内容天然去重；
* 版本以 ``parent_version_id`` 构成 DAG，``version_links`` 额外记录
  「回到旧版 / 分支 / 合并」这类语义边，供流程视图与 Web 审阅端使用。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 15

class DatabaseError(Exception):
    """库结构层的问题（结构版本比程序新、迁移中途被打断等）。"""

SQL_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 项目（工程）：库内顶层容器
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    color       TEXT NOT NULL DEFAULT '#4a90d9',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_name
    ON projects(name COLLATE NOCASE);

-- 节点：项目内的层级结构（阶段/子系统/批次…任意语义）
CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'node',
    description TEXT NOT NULL DEFAULT '',
    sort_key    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(parent_id);
-- 同一父节点下不允许重名（parent_id 为 NULL 时用 -1 占位）
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_scope_name
    ON nodes(project_id, IFNULL(parent_id, -1), name COLLATE NOCASE);

-- 文档：身份由「标题」确定，同一节点下同名即视为同一份文档的不同时期
CREATE TABLE IF NOT EXISTS documents (
    id                 INTEGER PRIMARY KEY,
    node_id            INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    title              TEXT NOT NULL,
    kind               TEXT NOT NULL DEFAULT 'file',
    status             TEXT NOT NULL DEFAULT 'active',
    description        TEXT NOT NULL DEFAULT '',
    current_version_id INTEGER,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_scope_title
    ON documents(node_id, title COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_documents_node ON documents(node_id);

-- blob：库内实体文件（一行 = files/ 下的一个真实文件）。
--
-- ``sha256`` **刻意不加 UNIQUE**（v15 改的）：库确实按内容去重（归档同一个文件
-- 两次不会在 files/ 下留两份），但**一稿可以有自己的那一份实体副本** ——
-- 用户明确的诉求（"我要的是复制一份真实的文件，而不是仅仅复制链接"）：
-- 把某一份复制登记到另一稿时，那一稿从此有自己的字节，与原来那稿互不影响
-- （同一份内容允许两行，靠 idx_blobs_sha256 查重）。
CREATE TABLE IF NOT EXISTS blobs (
    id         INTEGER PRIMARY KEY,
    sha256     TEXT NOT NULL,
    size       INTEGER NOT NULL,
    ext        TEXT NOT NULL DEFAULT '',
    mime       TEXT NOT NULL DEFAULT '',
    rel_path   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blobs_sha256 ON blobs(sha256);

-- 版本：某文档在某一时刻的状态（一次「快照」）
CREATE TABLE IF NOT EXISTS versions (
    id                INTEGER PRIMARY KEY,
    document_id       INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    seq               INTEGER NOT NULL,
    parent_version_id INTEGER REFERENCES versions(id) ON DELETE SET NULL,
    primary_file_id   INTEGER,
    source_name       TEXT NOT NULL DEFAULT '',
    source_path       TEXT NOT NULL DEFAULT '',
    sha256            TEXT NOT NULL DEFAULT '',
    size              INTEGER NOT NULL DEFAULT 0,
    mime              TEXT NOT NULL DEFAULT '',
    file_mtime        TEXT,
    author            TEXT NOT NULL DEFAULT '',
    note              TEXT NOT NULL DEFAULT '',
    origin            TEXT NOT NULL DEFAULT 'manual',
    review_state      TEXT NOT NULL DEFAULT 'draft',
    created_at        TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_seq ON versions(document_id, seq);
CREATE INDEX IF NOT EXISTS idx_versions_parent ON versions(parent_version_id);
CREATE INDEX IF NOT EXISTS idx_versions_document ON versions(document_id);

-- 版本内的文件集合：primary 为主文件，attachment 为附件
CREATE TABLE IF NOT EXISTS version_files (
    id          INTEGER PRIMARY KEY,
    version_id  INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    blob_id     INTEGER NOT NULL REFERENCES blobs(id) ON DELETE RESTRICT,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'primary',
    seq         INTEGER NOT NULL DEFAULT 0,
    size        INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL DEFAULT '',
    mime        TEXT NOT NULL DEFAULT '',
    source_path TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_version_files_version ON version_files(version_id);
CREATE INDEX IF NOT EXISTS idx_version_files_blob ON version_files(blob_id);

-- 语义边：回到旧版 / 分支 / 合并
CREATE TABLE IF NOT EXISTS version_links (
    id              INTEGER PRIMARY KEY,
    from_version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    to_version_id   INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    label           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_version_links_from ON version_links(from_version_id);
CREATE INDEX IF NOT EXISTS idx_version_links_to ON version_links(to_version_id);

-- 审阅记录（Web 端「确认版本」落到这里）
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY,
    version_id INTEGER NOT NULL REFERENCES versions(id) ON DELETE CASCADE,
    reviewer   TEXT NOT NULL DEFAULT '',
    decision   TEXT NOT NULL,
    comment    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviews_version ON reviews(version_id);

-- 标签
CREATE TABLE IF NOT EXISTS tags (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#8a8a8a'
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_scope_name
    ON tags(project_id, name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS document_tags (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);

-- 监视目录：投递即归档（拖到目录里 = 新版本）
CREATE TABLE IF NOT EXISTS watch_dirs (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    node_id    INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    recursive  INTEGER NOT NULL DEFAULT 1,
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watch_dirs_path ON watch_dirs(path);

-- 事件流：非版本类动作（移动、重命名、批量归档…），用于流程图旁的注记
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER,
    node_id     INTEGER,
    document_id INTEGER,
    version_id  INTEGER,
    kind        TEXT NOT NULL,
    message     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_document ON events(document_id);

-- 文本抽取缓存：供全文检索与差异对比复用
CREATE TABLE IF NOT EXISTS version_text (
    version_id   INTEGER PRIMARY KEY REFERENCES versions(id) ON DELETE CASCADE,
    file_id      INTEGER,
    text         TEXT NOT NULL DEFAULT '',
    extracted_at TEXT NOT NULL
);
"""

# ---------------------------------------------------------------------------
# 结构迁移
# ---------------------------------------------------------------------------
#
# 说明：v2 的改动来自对作者旧库（Symphytum/ToOak 的 data.db）的实地研究——
# 那套库里每条记录是「一个任务」，字段包括 标题/版本/输入文件/输出文件/参考数据/
# 分析资料/图示/进度/完成/限期/任务日期/相关人/依赖任务。
# 这些语义值得在新库里原生表达，而不是塞进一个通用文本字段。

MIGRATIONS: dict[int, list[str]] = {
    2: [
        # 任务型元数据（与旧库字段一一对应）
        "ALTER TABLE documents ADD COLUMN progress INTEGER",
        "ALTER TABLE documents ADD COLUMN done INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE documents ADD COLUMN due_date TEXT",
        "ALTER TABLE documents ADD COLUMN task_date TEXT",
        "ALTER TABLE documents ADD COLUMN people TEXT NOT NULL DEFAULT ''",
        # 保留原库未建模的信息，导入不丢东西
        "ALTER TABLE documents ADD COLUMN extra TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE versions ADD COLUMN extra TEXT NOT NULL DEFAULT ''",
        # 文档之间的语义链接（旧库的「依赖任务」就落在这里）
        """
        CREATE TABLE IF NOT EXISTS document_links (
            id               INTEGER PRIMARY KEY,
            from_document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            to_document_id   INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            kind             TEXT NOT NULL DEFAULT 'depends_on',
            label            TEXT NOT NULL DEFAULT '',
            created_at       TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_document_links_from ON document_links(from_document_id)",
        "CREATE INDEX IF NOT EXISTS idx_document_links_to ON document_links(to_document_id)",
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_document_links_unique
            ON document_links(from_document_id, to_document_id, kind)
        """,
    ],
    # v3：文件**就地引用**。
    #
    # 出发点：用户的资料本来就整齐地放在自己的目录里（工程文件夹、共享盘），
    # 文档管理器真正要管的是"哪个时期是哪份文件、谁确认过、依赖什么"，
    # 而不是把十几 GB 数据再抄一份。于是默认只登记引用：
    # rel_path 存绝对路径，source='reference'，库不拥有该文件。
    # 需要"库自带副本"时可用 source='store'（复制进 blob 库）或 'link'（硬链接）。
    3: [
        "ALTER TABLE blobs ADD COLUMN source TEXT NOT NULL DEFAULT 'store'",
        "ALTER TABLE blobs ADD COLUMN origin_path TEXT NOT NULL DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS idx_blobs_source ON blobs(source)",
        # 引用模式下，同一个内容可能被多个不同路径的文件引用（去重后只有一个 blob 行），
        # 因此每个版本文件要自己记住"我当时引用的是哪个文件"。
        "ALTER TABLE version_files ADD COLUMN location TEXT NOT NULL DEFAULT ''",
    ],
    # v4：从"审批流"退回"个人管理"。
    #
    # 用户的定位很明确：这是他自己的资料管理，不需要批准/驳回这种流程语义。
    # 事件（documents）只需要一个进度状态：进行中 / 已完成 / 搁置。
    # 版本的 review_state 列保留（历史数据不动），但不再当审批用，
    # 界面上的字样与配色改为围绕事件状态展开。
    4: [
        "ALTER TABLE documents ADD COLUMN state TEXT NOT NULL DEFAULT 'active'",
        "UPDATE documents SET state = 'done' WHERE done = 1",
        "CREATE INDEX IF NOT EXISTS idx_documents_state ON documents(state)",
    ],
    # v5：版图上节点的**手工位置**。
    #
    # 不能让自动布局每次把他摆好的位置冲掉。位置按 (项目, 事件) 存。
    5: [
        """
        CREATE TABLE IF NOT EXISTS graph_positions (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            project_id  INTEGER NOT NULL,
            x           REAL NOT NULL,
            y           REAL NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_graph_positions_project ON graph_positions(project_id)",
    ],
    # v6：事件页画布的**手工排版**。
    #
    # 事件页上的每个方框（环节框、信息框）都能自己拖动、自己调大小，
    # 位置与大小按"格子"存下来 —— 沿用旧版 ToOak/Symphytum 的
    # FormLayoutMatrix 思路：视图分成格子，每个字段占 N×M 格。
    # 用户自己排的版不能被刷新冲掉。
    6: [
        """
        CREATE TABLE IF NOT EXISTS stage_layouts (
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            box_key     TEXT NOT NULL,
            col         INTEGER NOT NULL,
            row         INTEGER NOT NULL,
            units_w     INTEGER NOT NULL,
            units_h     INTEGER NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (document_id, box_key)
        )
        """,
    ],
    # v7：方框大小改成**一套模板**（所有方框一样大，存在库里 ui:event.units），
    # 所以这张表只管"每个方框摆在哪一格"，不再逐框存宽高。
    7: [
        """
        CREATE TABLE IF NOT EXISTS stage_layouts_v7 (
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            box_key     TEXT NOT NULL,
            col         INTEGER NOT NULL,
            row         INTEGER NOT NULL,
            updated_at  TEXT NOT NULL,
            PRIMARY KEY (document_id, box_key)
        )
        """,
        """
        INSERT OR IGNORE INTO stage_layouts_v7(document_id, box_key, col, row, updated_at)
        SELECT document_id, box_key, col, row, updated_at FROM stage_layouts
        """,
        "DROP TABLE stage_layouts",
        "ALTER TABLE stage_layouts_v7 RENAME TO stage_layouts",
    ],
    # 位置本来就在这张表里，这里把宽高加回来，按方框存。
    8: [
        "ALTER TABLE stage_layouts ADD COLUMN units_w INTEGER NOT NULL DEFAULT 3",
        "ALTER TABLE stage_layouts ADD COLUMN units_h INTEGER NOT NULL DEFAULT 3",
    ],
    # v9：排版从"每个事件各排一份"改成**全库共用的一个框体模板**
    # 把原来按事件存的位置与大小收成一张全局表；已有记录按更新时间取最新的一条。
    9: [
        """
        CREATE TABLE IF NOT EXISTS box_template (
            box_key    TEXT PRIMARY KEY,
            col        INTEGER NOT NULL,
            row        INTEGER NOT NULL,
            units_w    INTEGER NOT NULL,
            units_h    INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        INSERT OR REPLACE INTO box_template(box_key, col, row, units_w, units_h, updated_at)
        SELECT box_key, col, row, units_w, units_h, updated_at
          FROM stage_layouts ORDER BY updated_at
        """,
        "DROP TABLE stage_layouts",
    ],
    # 有的在说明里，统一迁移过来作为说明，不要备注。各版本的说明可以不一致"）。
    #
    # 这一版**只升版本号**，不动数据：真正的数据合并放在
    # ``Library.consolidate_version_notes()`` 里用 Python 做 ——
    # 因为它要判断"这段文字是不是已经在某一稿的说明里了"，
    # 纯 SQL 写出来又长又难查，而且这一步必须**不丢文字**。
    # 桌面端启动时会调用它（幂等，处理完就没有可做的了）。
    11: [],
    # v12：白板上"人能写的东西" —— 连线的曲度、自由说明文本框、事件分组。
    #
    # "加个组，把各事件节点框起来形成组，组里的节点有记录"。
    # 三样都是**版图自己的东西**（不是事件/环节的一部分），所以单独放表，
    # 删事件、删项目时跟着级联走，不会污染文档模型。
    12: [
        # 连线的弯曲程度（0 = 自动；正数 = 往上拱的幅度，场景像素）
        "ALTER TABLE document_links ADD COLUMN curve REAL NOT NULL DEFAULT 0",
        # 白板上的自由文本框：只描述"这里发生了什么"，不参与归档
        """
        CREATE TABLE IF NOT EXISTS board_notes (
            id         INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            x          REAL NOT NULL DEFAULT 0,
            y          REAL NOT NULL DEFAULT 0,
            width      REAL NOT NULL DEFAULT 260,
            text       TEXT NOT NULL DEFAULT '',
            color      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_board_notes_project ON board_notes(project_id)",
        # 分组：一个组 = 白板上一个大框，框住若干事件
        """
        CREATE TABLE IF NOT EXISTS board_groups (
            id          INTEGER PRIMARY KEY,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            name        TEXT NOT NULL DEFAULT '',
            note        TEXT NOT NULL DEFAULT '',
            color       TEXT NOT NULL DEFAULT '',
            sort_key    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_board_groups_project ON board_groups(project_id)",
        # 组里有哪些事件（**这就是"组里的节点记录"**）
        """
        CREATE TABLE IF NOT EXISTS board_group_members (
            group_id    INTEGER NOT NULL REFERENCES board_groups(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            added_at    TEXT NOT NULL,
            PRIMARY KEY (group_id, document_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_board_group_members_doc "
        "ON board_group_members(document_id)",
    ],
    # v13：白板上的卡片统一成一张表 —— 说明框 / 素材卡片（文件、链接）本质是同一件事
    # （"一块可以摆、可以写、可以删的东西"），只有"内容从哪来"不同。
    # 分成两张表会有两套重复代码，以后加"网页卡片""内嵌预览"又要再开一张。
    #
    # 卡片只是**待归档的引用**，转正时才建事件、归档、进分组。
    13: [
        """
        CREATE TABLE IF NOT EXISTS board_cards (
            id         INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            kind       TEXT NOT NULL DEFAULT 'note',
            x          REAL NOT NULL DEFAULT 0,
            y          REAL NOT NULL DEFAULT 0,
            w          REAL NOT NULL DEFAULT 260,
            h          REAL NOT NULL DEFAULT 0,
            title      TEXT NOT NULL DEFAULT '',
            text       TEXT NOT NULL DEFAULT '',
            path       TEXT NOT NULL DEFAULT '',
            url        TEXT NOT NULL DEFAULT '',
            origin     TEXT NOT NULL DEFAULT 'user',
            color      TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_board_cards_project ON board_cards(project_id)",
        # 老的说明框整行搬过来（kind='note'），旧表删掉
        """
        INSERT OR IGNORE INTO board_cards(id, project_id, kind, x, y, w, h, title, text,
                                          path, url, origin, color, created_at, updated_at)
        SELECT id, project_id, 'note', x, y, width, 0, '', text, '', '',
               'user', color, created_at, updated_at
          FROM board_notes
        """,
        "DROP TABLE IF EXISTS board_notes",
        # 分组：空组也要能画出来、还能拖大拖小，所以要有自己的矩形；
        # manual=1 表示"这个框的大小由用户定，别跟着成员收紧"。
        "ALTER TABLE board_groups ADD COLUMN x REAL NOT NULL DEFAULT 0",
        "ALTER TABLE board_groups ADD COLUMN y REAL NOT NULL DEFAULT 0",
        "ALTER TABLE board_groups ADD COLUMN w REAL NOT NULL DEFAULT 0",
        "ALTER TABLE board_groups ADD COLUMN h REAL NOT NULL DEFAULT 0",
        "ALTER TABLE board_groups ADD COLUMN manual INTEGER NOT NULL DEFAULT 0",
    ],
    # 单元格坐标同步翻倍，**视觉上的排布一点不变**：原来占 2 格（200px）的，
    # 现在占 4 格（4×50px）—— 位置与大小的物理尺寸保持不变，只是吸附更细。
    10: [
        # 只在**没跑过**的时候翻倍：老代码是"先执行、后写版本号"，中途被打断会重跑，
        # 再翻一倍就把用户手工摆好的版图静默改坏了（数据不可逆）
        "UPDATE box_template SET col = col * 2, row = row * 2, "
        "units_w = units_w * 2, units_h = units_h * 2 "
        "WHERE (SELECT value FROM meta WHERE key = 'migration.v10') IS NULL",
        "INSERT OR REPLACE INTO meta(key, value) VALUES('migration.v10', '1')",
    ],
    # v14：版本文件记下**当时的修改时间**（纳秒）。
    #
    # 为什么需要它：引用模式下"这一稿的文件还是不是当初那份"只能靠元数据判断。
    # 只看大小抓不到同长度覆盖；逐字节算 sha256 对 GB 级文件是分钟级代价
    # （实测一个 2.97 GB 的事件每归档一次就读一遍）。有了 mtime，
    # "大小 + 修改时间都对得上"就足以**跳过重算**；对不上就立刻重新纳管。
    14: [
        "ALTER TABLE version_files ADD COLUMN mtime_ns INTEGER NOT NULL DEFAULT 0",
    ],
    # v15：blob 的 sha256 **不再唯一** —— 一稿可以有自己的那一份实体副本。
    #
    # "你确定生成的是新文件吗？怎么都是 D:\\…\\files\\5a7893d8…"。
    # 原来 sha256 是 UNIQUE，一份内容在库里只能有一行记录、也就只能有一个实体文件，
    # 于是"复制一份到这一稿"只能让两稿指向同一个文件（用户一眼就看出来了）。
    # 现在允许同内容多行：归档那条路照旧去重（不产生垃圾副本），
    # 而"复制登记到另一稿"会**真的再存一份**（各自的 files/<别名>/<原名>）。
    #
    # SQLite 不能 DROP 掉表上的 UNIQUE 约束（它是隐式索引 sqlite_autoindex），
    # 只能重建表；而重建必须**关掉外键约束**（见 ``FK_OFF_MIGRATIONS``）：
    # ``DROP TABLE blobs`` 会先做一次隐式 DELETE，而 version_files 还引用着这些行，
    # 开着立即型外键约束时这一步直接报错（实测 ``FOREIGN KEY constraint failed``；
    # 在事务里 ``PRAGMA defer_foreign_keys`` 也救不了 —— 提交时照样判违规）。
    # 重建之后重启约束即可（``initialize`` 会在同一个事务里跑一次
    # ``PRAGMA foreign_key_check`` 自检，有问题就整体回滚）。
    15: [
        """
        CREATE TABLE IF NOT EXISTS blobs_v15 (
            id          INTEGER PRIMARY KEY,
            sha256      TEXT NOT NULL,
            size        INTEGER NOT NULL,
            ext         TEXT NOT NULL DEFAULT '',
            mime        TEXT NOT NULL DEFAULT '',
            rel_path    TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            source      TEXT NOT NULL DEFAULT 'store',
            origin_path TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        INSERT INTO blobs_v15 (id, sha256, size, ext, mime, rel_path, created_at,
                               source, origin_path)
        SELECT id, sha256, size, ext, mime, rel_path, created_at, source, origin_path
          FROM blobs
        """,
        "DROP TABLE blobs",
        "ALTER TABLE blobs_v15 RENAME TO blobs",
        "DROP INDEX IF EXISTS idx_blobs_sha256",
        "DROP INDEX IF EXISTS idx_blobs_source",
        "CREATE INDEX IF NOT EXISTS idx_blobs_sha256 ON blobs(sha256)",
        "CREATE INDEX IF NOT EXISTS idx_blobs_source ON blobs(source)",
    ],
}

#: 需要**先关掉外键约束**才能跑的迁移（只能关在事务外面，见 SQLite 的
#: ``PRAGMA foreign_keys`` 说明）。目前只有 v15：它要重建 blobs 表（去掉 sha256 UNIQUE）。
#: ``initialize`` 会在迁移事务里补一次 ``PRAGMA foreign_key_check`` 自检。
FK_OFF_MIGRATIONS = {15}


class Database:
    """一个库对应一个 SQLite 连接；线程内使用，不跨线程共享。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), timeout=20.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.initialize()

    # ---------------------------------------------------------------- 结构

    def initialize(self) -> None:
        """建表并迁移到最新结构版本（幂等）。

        全新库与已有库走的是同一条路径：先建基础表（IF NOT EXISTS），
        再按 ``MIGRATIONS`` 逐版本补齐 —— 不留「新库有列、老库没列」的分叉。

        **一个版本一个事务**：迁移跑到一半被打断（断电、OOM、任务管理器结束进程）
        于是"跑了一半"的库下次启动会重跑该版本：``DROP TABLE`` 找不到表、
        ``INSERT ... SELECT`` 主键冲突、``col = col * 2`` 把坐标再翻一倍（数据静默损坏），
        而库直接打不开 —— 用户没有任何恢复入口。
        碰到"表/列已经在了"就当这一步做过（重复执行不报错）。
        """
        self.conn.executescript(SQL_SCHEMA_V1)
        current = int(self.get_meta("schema_version") or 1)
        if current > SCHEMA_VERSION:
            raise DatabaseError(
                f"这个库的结构版本是 {current}，比当前程序支持的 {SCHEMA_VERSION} 新："
                "请升级 ToOak 后再打开（用旧程序打开新库会损坏数据）。"
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            # 有的迁移（重建表）必须**关掉外键约束**：``PRAGMA foreign_keys``
            # 只在没有待提交事务时才生效，所以开关都放在事务外面。
            foreign_keys_off = version in FK_OFF_MIGRATIONS
            if foreign_keys_off:
                self.conn.execute("PRAGMA foreign_keys = OFF")
            try:
                with self.transaction():
                    for statement in MIGRATIONS.get(version, []):
                        try:
                            self.conn.execute(statement)
                        except sqlite3.OperationalError as error:
                            # 重复列 / 表已经不存在 / 表已存在：都说明这一步做过了
                            message = str(error).lower()
                            if ("duplicate column name" not in message
                                    and "no such table" not in message
                                    and "already exists" not in message):
                                raise
                    if foreign_keys_off:
                        # 重建过表的迁移要**自检外键关系**：有问题就整体回滚，
                        # 绝不让"表换了、引用断了"的库留在磁盘上
                        broken = self.query("PRAGMA foreign_key_check")
                        if broken:
                            raise DatabaseError(
                                f"迁移到 v{version} 之后外键检查没通过（{len(broken)} 处）："
                                "已回滚，库保持原样"
                            )
                    self.set_meta("schema_version", str(version))
            finally:
                if foreign_keys_off:
                    self.conn.execute("PRAGMA foreign_keys = ON")
        if current == SCHEMA_VERSION:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ------------------------------------------------------------ 基础操作

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params or ()))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> sqlite3.Cursor:
        return self.conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence[Any] | None = None) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params or ())))

    def query_one(self, sql: str, params: Sequence[Any] | None = None) -> sqlite3.Row | None:
        cursor = self.conn.execute(sql, tuple(params or ()))
        return cursor.fetchone()

    def scalar(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    def insert(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """执行 INSERT 并返回新行 id。"""
        cursor = self.conn.execute(sql, tuple(params or ()))
        return int(cursor.lastrowid or 0)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """显式事务：异常时整体回滚。

        归档一份文件往往要同时写 blobs / versions / version_files / events，
        必须保证「要么都成功，要么都不留痕」。

        * 回滚要连 ``KeyboardInterrupt``/``SystemExit`` 一起管 —— 只捕 ``Exception``
          的话，归档大文件时按 Ctrl-C 会把连接留在"打开的事务"里，
          此后这个连接**再也开不了事务**（``cannot start a transaction within
          a transaction``），而且一直握着写锁；
        * ``COMMIT`` 自己失败（磁盘满/被锁）也要回滚，否则同样留一个开着的事务；
        * 已经在事务里时直接复用外层（嵌套调用不该报错，也不该把外层 body 跳过）。
        """
        if self.conn.in_transaction:
            yield self.conn
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        else:
            try:
                self.conn.execute("COMMIT")
            except sqlite3.Error:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def close(self) -> None:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------ 维护工具

    def vacuum(self) -> None:
        """整理碎片、回收空间（对应旧版的「优化数据库大小」）。"""
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")

    def integrity_check(self) -> str:
        return str(self.scalar("PRAGMA integrity_check") or "unknown")

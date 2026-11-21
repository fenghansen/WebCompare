"""【模块】SQLite 持久化 — 模式定义、连接管理与极简查询封装

功能说明：服务全部状态（账号、身份、会话、资产、比较、分享、存储
登记、配额预留、访客工作区与站点设置）的唯一存储。以 WAL 模式与外键
约束打开数据库，提供短连接与字典化查询的最小封装；不写业务逻辑，
业务都在调用方。模式版本 v2 为基线，v3 至 v6 以附加语句增量升级。

## 流程
所属流程：账号身份与持久化（webcompare/db.py）

1. **打开与建表**：WAL/外键/缓存参数与模式版本校验、v2 至 v6 增量迁移 → 见函数 [Database]
2. **连接管理**：短连接、提交/回滚/关闭的事务边界 → 见函数 [Database.connect]
3. **查询封装**：单行/多行/执行的便捷入口 → 见函数 [Database.one]、[Database.all]、[Database.execute]
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
INSERT INTO schema_version SELECT 2 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, display_name TEXT NOT NULL, role TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS identities(
 provider TEXT NOT NULL, subject TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id),
 PRIMARY KEY(provider,subject)
);
CREATE TABLE IF NOT EXISTS local_credentials(
 user_id TEXT PRIMARY KEY REFERENCES users(id), username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
 csrf TEXT NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS assets(
 id TEXT PRIMARY KEY, path TEXT NOT NULL,
 revision TEXT NOT NULL, bytes INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS comparisons(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
 manifest TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS shares(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
 comparison_id TEXT NOT NULL REFERENCES comparisons(id), state TEXT NOT NULL,
 created_at INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
);
"""

# v3 全部为附加语句：不删不改旧列，旧库升级后 shares 历史行的 visibility 落为 personal。
SCHEMA_V3 = """
ALTER TABLE shares ADD COLUMN visibility TEXT NOT NULL DEFAULT 'personal';
CREATE TABLE IF NOT EXISTS guest_workspaces(
 user_id TEXT PRIMARY KEY REFERENCES users(id),
 created_at INTEGER NOT NULL, last_active_at INTEGER NOT NULL,
 exited_at INTEGER, first_upload_at INTEGER, purged INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS stored_files(
 path TEXT PRIMARY KEY,
 owner_id TEXT NOT NULL REFERENCES users(id),
 area TEXT NOT NULL,
 bytes INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS stored_files_owner ON stored_files(owner_id);
CREATE TABLE IF NOT EXISTS quota_reservations(
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, bytes INTEGER NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS site_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

SCHEMA_V4 = """
CREATE TABLE IF NOT EXISTS private_directories(
 user_id TEXT PRIMARY KEY REFERENCES users(id), name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS stored_directories(
 path TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL
);
"""

SCHEMA_V5 = """
CREATE TABLE IF NOT EXISTS export_jobs(
 id TEXT PRIMARY KEY,
 owner_id TEXT NOT NULL REFERENCES users(id),
 comparison_id TEXT NOT NULL REFERENCES comparisons(id) ON DELETE CASCADE,
 assets TEXT NOT NULL, state TEXT NOT NULL,
 status TEXT NOT NULL, processed_bytes INTEGER NOT NULL DEFAULT 0,
 total_bytes INTEGER NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
 reserved_bytes INTEGER NOT NULL, error TEXT,
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS export_jobs_owner ON export_jobs(owner_id);
"""

SCHEMA_V6 = """
ALTER TABLE shares ADD COLUMN config TEXT;
ALTER TABLE shares ADD COLUMN template_manifest TEXT;
"""

SCHEMA_VERSION = 6


def _schema_statements(script: str) -> list[str]:
    """功能说明：把模式脚本切成单条语句（脚本内不含字符串字面量分号）。

    调用时机：仅由 [Database._migrate_v3] 在执行 v3 附加语句前调用。

    关键步骤：
    1. 按分号切分；
    2. 去掉空白与空片段。

    输出/副作用：返回语句列表；纯函数。
    """
    return [statement.strip() for statement in script.split(";") if statement.strip()]


class Database:
    """SQLite 状态库的最小封装：构造时建表、按需增量升级并校验模式版本。"""

    def __init__(self, path: Path):
        """功能说明：打开（或创建）状态库，建基表并把模式升级到当前版本。

        调用时机：CLI 子命令、应用装配层与测试夹具在拿到状态目录后立即调用。

        关键步骤：
        1. 开短连接并把日志模式设为 WAL，执行 v2 基表脚本（幂等）；
        2. 读取 schema_version：高于当前支持版本直接报错，等于当前版本无需迁移；
        3. 版本 2 时执行 [Database._migrate_v3]，再为 v3 补上目录映射及归属表；
           新建空库同样升级到 v4，再附加 v5 的 ZIP 任务表；
        4. 版本 1 或缺失视为不受支持，提示重建或迁移。

        输出/副作用：建表/补列/建索引并提交；版本不受支持时抛 RuntimeError。
        """
        self.path = path
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            if version == 1:
                raise RuntimeError("数据库版本过低（v1）；v2 起资产按绝对路径登记，请重建或先迁移")
            if version > SCHEMA_VERSION:
                raise RuntimeError(f"数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}，请升级程序")
            if version == 2:
                self._migrate_v3(conn)
            if version <= 3:
                for statement in _schema_statements(SCHEMA_V4):
                    conn.execute(statement)
                conn.execute("UPDATE schema_version SET version=4")
            if version <= 4:
                for statement in _schema_statements(SCHEMA_V5):
                    conn.execute(statement)
                conn.execute("UPDATE schema_version SET version=5")
            if version <= 5:
                if not conn.in_transaction:
                    conn.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in conn.execute("PRAGMA table_info(shares)")}
                for statement in _schema_statements(SCHEMA_V6):
                    if statement.split()[5] not in columns:
                        conn.execute(statement)
                conn.execute("UPDATE schema_version SET version=6")

    @staticmethod
    def _migrate_v3(conn: sqlite3.Connection) -> None:
        """功能说明：把 v2 库增量升级到 v3，并原子地把模式版本写成 3。

        调用时机：仅由 [Database.__init__] 在读到版本 2 时调用。

        关键步骤：
        1. 连接切到自动提交模式并显式 BEGIN IMMEDIATE，让 DDL 与版本写入同一事务；
        2. 逐条执行 [SCHEMA_V3]；shares 已有 visibility 列时跳过该 ALTER（可重入）；
        3. 覆写 schema_version 为 3 后提交，任何异常整体回滚。

        输出/副作用：新增表、索引与 shares.visibility 列；失败不留半成品模式。
        """
        statements = _schema_statements(SCHEMA_V3)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(shares)")}
        previous = conn.isolation_level
        conn.isolation_level = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in statements:
                    if statement.upper().startswith("ALTER TABLE SHARES") and "visibility" in columns:
                        continue
                    conn.execute(statement)
                conn.execute("UPDATE schema_version SET version=3")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.isolation_level = previous

    @contextmanager
    def connect(self):
        """功能说明：提供一个事务化短连接：成功提交、异常回滚、用完关闭。

        调用时机：所有读写操作的底层入口；业务侧通常用 [Database.one] /
        [Database.all] / [Database.execute]，多语句写入时才直接使用它。

        关键步骤：
        1. 打开连接并启用外键与小页缓存；
        2. yield 给调用方执行语句；
        3. 正常结束提交，任何异常回滚并上抛，最终关闭连接。

        输出/副作用：提交调用方的写入；异常时不留半成品事务。
        """
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA cache_size=-2048")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def one(self, query: str, parameters: tuple = ()):
        """功能说明：执行查询并返回第一行字典，无结果返回 None。

        调用时机：业务侧读单行记录时（会话、用户、资产、分享等）。

        关键步骤：
        1. 经 [Database.connect] 开短连接执行参数化查询；
        2. 行转 dict 返回。

        输出/副作用：只读。
        """
        with self.connect() as conn:
            row = conn.execute(query, parameters).fetchone()
            return dict(row) if row else None

    def all(self, query: str, parameters: tuple = ()) -> list[dict]:
        """功能说明：执行查询并返回全部行的字典列表。

        调用时机：业务侧读多行记录时（账号列表、迁移导出、待清理会话等）。

        关键步骤：
        1. 经 [Database.connect] 开短连接执行参数化查询；
        2. 逐行转 dict 收集返回。

        输出/副作用：只读。
        """
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, parameters)]

    def execute(self, query: str, parameters: tuple = ()) -> None:
        """功能说明：执行单条写语句并提交。

        调用时机：业务侧的单语句写入（撤销分享、删除会话等）。

        关键步骤：
        1. 经 [Database.connect] 开短连接执行，退出时提交。

        输出/副作用：写入数据库；多语句写入请改用 [Database.connect] 保持原子性。
        """
        with self.connect() as conn:
            conn.execute(query, parameters)

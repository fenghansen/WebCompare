"""【模块】账号与身份 — 本地凭据、稳定 UUID 与访客工作区

账号以稳定 UUID 标识，登录验证本地口令，比较与分享按 UUID 归属。

访客是其中一种临时身份：没有口令，只有一条名额受限的工作区记录，
由清理任务按保留期回收（见 webcompare/lifecycle.py）。

## 流程
所属流程：账号身份与持久化（webcompare/auth.py）

1. **账号创建**：参数校验、scrypt 口令散列、UUID 分配与本地身份登记 → 见函数 [create_user]、[hash_password]
2. **登录认证**：常量时间口令校验与防时序探测 → 见函数 [LocalProvider.authenticate]、[verify_password]
4. **会话与查询工具**：会话令牌散列与公开用户信息读取 → 见函数 [token_hash]、[public_user]、[Database]
5. **访客工作区**：名额受限的临时身份创建与活动/退出标记 → 见函数 [create_guest_user]、[touch_guest_activity]、[mark_guest_exited]
6. **个人默认身份**：免登录入口与模板 CLI 共用持久所有者，优先复用旧成员或创建无口令成员 → 见函数 [personal_user]
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid

from .config import Settings
from .db import Database
from .files import FileError


GUEST_TOUCH_SECONDS = 60  # 访客活动刷新节流：距上次写入不足 60 秒就不再写库
PERSONAL_SESSION_PREFIX = "personal."  # 标识自动会话来源，开启认证后不能继续使用


def hash_password(password: str) -> str:
    """功能说明：把明文口令散列为可存储的 scrypt 字符串（含随机盐与参数）。

    调用时机：[create_user] 与 CLI reset-password 设置口令时调用。

    关键步骤：
    1. 校验口令非空且不超过 256 字符；
    2. 生成 16 字节随机盐，以 scrypt(n=16384, r=8, p=1) 计算散列；
    3. 按 `scrypt$n$r$p$盐$散列` 格式编码返回。

    输出/副作用：返回可入库的散列字符串；纯计算，无副作用。
    """
    if not password or len(password) > 256:
        raise ValueError("密码不能为空或超过 256 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """功能说明：校验明文口令是否匹配存储的 scrypt 散列，常量时间比较。

    调用时机：[LocalProvider.authenticate] 登录校验时调用。

    关键步骤：
    1. 解析存储串并核对算法与参数正是当前配置；
    2. 用相同盐重算散列，hmac.compare_digest 常量时间比较；
    3. 格式非法一律返回 False 而非抛错。

    输出/副作用：返回布尔；纯计算。
    """
    if len(password) > 256:
        return False
    try:
        algorithm, n, r, p, salt, expected = stored.split("$")
        if algorithm != "scrypt" or (n, r, p) != ("16384", "8", "1"):
            return False
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def create_user(db: Database, username: str, password: str, *, role: str = "member", display_name: str | None = None) -> str:
    """功能说明：创建本地账号：分配稳定 UUID，一次性写入用户、凭据与本地身份映射。

    调用时机：CLI create-user、演示环境引导脚本与测试夹具调用。

    关键步骤：
    1. 校验用户名（非空、无空白、≤80 字符）与角色（admin/member/viewer）；
    2. 用 [hash_password] 散列口令；
    3. 同一事务写入 users、local_credentials、identities 三表。

    输出/副作用：返回新用户 UUID；数据库新增三行；明文口令不落地。
    """
    username = username.strip()
    if not username or len(username) > 80 or any(c.isspace() for c in username):
        raise ValueError("用户名不能为空、含空白或超过 80 字符")
    if role not in {"admin", "member", "viewer"}:
        raise ValueError("角色必须为 admin/member/viewer；访客只能由 create_guest_user 创建")
    password_hash = hash_password(password)
    user_id = str(uuid.uuid4())
    with db.connect() as conn:
        conn.execute("INSERT INTO users(id,display_name,role,created_at) VALUES(?,?,?,?)", (user_id, display_name or username, role, int(time.time())))
        conn.execute("INSERT INTO local_credentials VALUES(?,?,?)", (user_id, username, password_hash))
        conn.execute("INSERT INTO identities VALUES('local',?,?)", (username.casefold(), user_id))
    return user_id


def personal_user(db: Database) -> dict:
    """功能说明：解析 Personal 默认身份，保留旧库所有者，并在新库创建无口令成员。

    调用时机：免登录的会话入口与省略 --user 的模板 CLI 共用此函数。

    关键步骤：
    1. BEGIN IMMEDIATE 串行化首次绑定，避免并发访问创建多个默认身份；
    2. 优先沿用 identities(personal, owner)；未绑定时选择最早启用且有
       本地凭据的 member/admin，以保留单人旧库中的分享与 SSH 所有权；
    3. 没有合适旧账号才创建“个人工作台”成员，不生成口令或本地凭据。

    已绑定账号被停用或降权时拒绝自动进入，不静默改绑到其他账号，避免
    所有权意外切换。已有用户、凭据和历史记录均不合并、不删除。

    输出/副作用：返回可公开的用户信息；首次绑定新增 personal 身份映射，
    无合适旧账号时同时新增一个 member；映射失效抛 FileError(403)，事务回滚。
    """
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute(
            "SELECT u.id,u.display_name,u.role,u.active,c.username "
            "FROM identities i JOIN users u ON u.id=i.user_id "
            "LEFT JOIN local_credentials c ON c.user_id=u.id "
            "WHERE i.provider='personal' AND i.subject='owner'",
        ).fetchone()
        if user is not None and (not user["active"] or user["role"] not in {"member", "admin"}):
            raise FileError("默认个人账号已停用或不再是 member/admin；请恢复该账号，或开启登录后使用其他账号", "PERSONAL_OWNER_UNAVAILABLE", 403)
        if user is None:
            user = conn.execute(
                "SELECT u.id,u.display_name,u.role,u.active,c.username "
                "FROM users u JOIN local_credentials c ON c.user_id=u.id "
                "WHERE u.active=1 AND u.role IN ('member','admin') "
                "ORDER BY u.created_at,u.rowid LIMIT 1",
            ).fetchone()
            if user is None:
                user = {"id": str(uuid.uuid4()), "display_name": "个人工作台", "role": "member", "username": None}
                conn.execute(
                    "INSERT INTO users(id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user["id"], user["display_name"], user["role"], int(time.time())),
                )
            conn.execute("INSERT INTO identities VALUES('personal','owner',?)", (user["id"],))
        return {key: user[key] for key in ("id", "display_name", "role")} | {"username": user["username"] or user["display_name"]}


def create_guest_user(db: Database, settings: Settings) -> dict:
    """功能说明：创建一个访客工作区：无名额则拒绝，成功则同事务写下账号与工作区记录。

    访客没有口令也没有外部身份，workspace_id 就是 users.id（工作区目录
    即 guest/<user_id>），因此清理时删除账号与删除工作区是同一件事。

    调用时机：server 模式 POST /api/guests 创建临时体验身份时调用（外层按
    IP 限流）。

    关键步骤：
    1. 取当前时间与随机显示名“访客-xxxx”，开 BEGIN IMMEDIATE 事务把并发
       创建串行化（同一时刻只有一个写者，名额不会被同时抢超）；
    2. 事务内 `SELECT COUNT(*) FROM guest_workspaces WHERE purged=0`，
       达到 settings.guest_max_workspaces 即抛 FileError(429, GUEST_FULL)；
    3. 同一事务写入 users(role='guest', active=1) 与 guest_workspaces
       （created_at = last_active_at = now，exited_at/first_upload_at 为空，
       purged=0），一起提交。

    输出/副作用：返回 {user_id, created_at}；数据库新增两行；名额已满抛
    FileError(429, GUEST_FULL)，失败不留半行。
    """
    now = int(time.time())
    user_id = str(uuid.uuid4())
    display_name = f"访客-{secrets.token_hex(2)}"
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        count = int(conn.execute("SELECT COUNT(*) AS n FROM guest_workspaces WHERE purged=0").fetchone()["n"])
        if count >= settings.guest_max_workspaces:
            raise FileError(f"访客名额已满（上限 {settings.guest_max_workspaces} 个），请稍后再试或登录本地账号", "GUEST_FULL", 429)
        conn.execute("INSERT INTO users(id,display_name,role,created_at) VALUES(?,?,?,?)", (user_id, display_name, "guest", now))
        conn.execute(
            "INSERT INTO guest_workspaces(user_id,created_at,last_active_at,exited_at,first_upload_at,purged) VALUES(?,?,?,NULL,NULL,0)",
            (user_id, now, now),
        )
    return {"user_id": user_id, "created_at": now}


def touch_guest_activity(db: Database, user_id: str) -> bool:
    """功能说明：刷新访客工作区的最后活动时间，用节流写避免每个请求都落库。

    调用时机：server 模式访客会话的每次自家 API 活动（依赖注入处）调用；
    匿名浏览公开分享不触发，因为它不建立也不刷新任何工作区。

    关键步骤：
    1. 取当前时间，单条 UPDATE 自带节流条件：仅当 last_active_at 距现在
       至少 [GUEST_TOUCH_SECONDS] 秒才写入；
    2. 只更新未清理（purged=0）的工作区。

    输出/副作用：返回是否真的写入；节流命中或工作区不存在时只读，不改变
    last_active_at，因此闲置判定不会被高频请求无限推迟。
    """
    now = int(time.time())
    with db.connect() as conn:
        cursor = conn.execute(
            "UPDATE guest_workspaces SET last_active_at=? WHERE user_id=? AND purged=0 AND last_active_at<=?",
            (now, user_id, now - GUEST_TOUCH_SECONDS),
        )
        return cursor.rowcount > 0


def mark_guest_exited(db: Database, user_id: str) -> bool:
    """功能说明：登记访客主动退出时间，让工作区在退出保留期后必定被清理。

    调用时机：server 模式 POST /api/guests/exit 调用（随后由调用方立即删除
    会话并清除 Cookie，会话因此立刻失效）。

    关键步骤：
    1. 单条 UPDATE 只在 exited_at 尚为空时写入当前时间；
    2. 已退出或已清理的工作区不改动，重复调用安全。

    输出/副作用：返回是否首次登记退出；只写 exited_at，不碰磁盘，也不删
    会话（会话由退出端点自行删除）。
    """
    now = int(time.time())
    with db.connect() as conn:
        cursor = conn.execute("UPDATE guest_workspaces SET exited_at=? WHERE user_id=? AND purged=0 AND exited_at IS NULL", (now, user_id))
        return cursor.rowcount > 0


class LocalProvider:
    """本地用户名口令认证提供者；用户不存在时也做等价的散列比较以防时序探测。"""

    name = "local"

    def __init__(self, db: Database):
        self.db = db
        self.dummy_hash = hash_password(secrets.token_urlsafe(24))

    def authenticate(self, subject: str, secret: str) -> str | None:
        """功能说明：校验用户名口令，成功返回用户 UUID，失败返回 None。

        调用时机：POST /api/auth/login 经线程池调用（外层有 IP 限流与并发
        信号量）。

        关键步骤：
        1. 按用户名（大小写不敏感）联查凭据与账号激活状态；
        2. 用户不存在时改用构造时的随机假散列做同等 scrypt 计算，
           让失败路径耗时一致，防用户名枚举；
        3. 仅当用户存在、已激活且口令匹配时返回 UUID。

        输出/副作用：返回用户 UUID 或 None；只读操作。
        """
        row = self.db.one("SELECT c.user_id,c.password_hash,u.active FROM local_credentials c JOIN users u ON u.id=c.user_id WHERE c.username=? COLLATE NOCASE", (subject,))
        valid = verify_password(secret, row["password_hash"] if row else self.dummy_hash)
        return row["user_id"] if row and row["active"] and valid else None


def token_hash(token: str) -> str:
    """功能说明：把会话令牌散列为存储键，数据库不保存可还原的令牌原文。

    调用时机：登录写会话、每次请求的会话查询与登出时调用。

    关键步骤：
    1. 对令牌做 SHA-256。

    输出/副作用：返回十六进制散列；纯函数。
    """
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(db: Database, user_id: str) -> dict | None:
    """功能说明：读取可安全返回给前端的用户信息（不含凭据），未激活即视为不存在。

    调用时机：会话校验、登录响应等需要当前用户身份的地方。

    关键步骤：
    1. 联查 users 与 local_credentials；
    2. 未激活返回 None；用户名缺省时回退为显示名。

    输出/副作用：返回 id/display_name/role/username 字典或 None；只读。
    """
    row = db.one("SELECT u.id,u.display_name,u.role,u.active,c.username FROM users u LEFT JOIN local_credentials c ON c.user_id=u.id WHERE u.id=?", (user_id,))
    if not row or not row.pop("active"):
        return None
    row["username"] = row["username"] or row["display_name"]
    return row

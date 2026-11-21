"""【模块】配额记账 — 预留/提交/回收与站内磁盘预算

功能说明：server 模式的上传与分享包解包都要先在这里“占额度、再落盘、最后
结清”。每个账号（member 用户、guest 工作区）有固定配额；服务整体另有一个
存在 site_settings 里的站内预算（首次按 data_root 所在分区空闲空间的 80%
初始化，之后固定不动），另外每次写入都做实时磁盘底线检查。所有记账写操作
都用 SQLite 的 BEGIN IMMEDIATE 事务串行化，并发上传因此不会把配额撑爆；
预留若超时未结清，由清理任务按 RESERVATION_TTL（1 小时）回收，配套临时文件
一并删除。浏览服务器既有文件不产生登记行、也不占配额。

## 流程
所属流程：HTTP API 与安全边界（webcompare/quota.py）

1. **额度记账**：预留 → 落盘 → 提交，异常路径回收 → 见函数 [Quota.reserve]、[Quota.commit]、[Quota.abort]
2. **已落盘补登记**：分享包解压等已落盘文件按实际字节登记 → 见函数 [Quota.record_existing]
3. **归属维护**：删除同步删行，重命名/移动不转移归属 → 见函数 [Quota.remove]、[Quota.rename]
4. **站内预算与底线**：预算固定、显式重算，实时检查分区剩余空间 → 见函数 [Quota.ensure_site_budget]、[Quota.recalc_site_budget]、[Quota.check_disk_floor]
"""
from __future__ import annotations

import secrets
import shutil
import sqlite3
import time
from pathlib import Path

from .config import Settings
from .db import Database
from .files import FileError
from .storage import Storage

RESERVATION_TTL = 3600  # 预留超 1 小时未结清由清理任务回收（配合同名 staging 文件清理）
BUDGET_KEY = "disk_budget_bytes"
BUDGET_RATIO = 0.8


class Quota:
    """配额与磁盘预算的记账员：预留结清、归属维护与空间闸门。

    功能说明：只读写 stored_files / quota_reservations / site_settings 三张表，
    不碰文件本身——文件由调用方在配额放行后落盘。额度口径：member 用户用
    member_quota_bytes，guest 工作区（owner 即访客用户）用 guest_quota_bytes，
    admin 无个人配额但仍受站内预算与磁盘底线约束；viewer 等无上传角色的账号
    按 member 口径兜底（授权层本就不给它们写入口）。
    """

    def __init__(self, settings: Settings, db: Database, storage: Storage):
        """功能说明：绑定配置、数据库与存储边界，得到配额对象。

        调用时机：应用装配层与测试夹具在 [Storage] 就绪之后构造一次。

        关键步骤：
        1. 记住 settings（配额、磁盘底线、data_root）、db（记账事务）与
           storage（校验登记区域与虚拟路径一致）。

        输出/副作用：无；不访问文件系统。
        """
        self.settings, self.db, self.storage = settings, db, storage

    def usage(self, owner_id: str) -> int:
        """功能说明：统计某账号已占用的字节数：已登记文件加未结清的预留。

        调用时机：展示额度、上传前判断上限，以及测试断言记账是否守恒。

        关键步骤：
        1. 一条 SQL 汇总 stored_files 与 quota_reservations 中该 owner 的 bytes。

        输出/副作用：返回字节数；只读。
        """
        row = self.db.one(
            "SELECT (SELECT COALESCE(SUM(bytes),0) FROM stored_files WHERE owner_id=?)"
            "+(SELECT COALESCE(SUM(bytes),0) FROM quota_reservations WHERE owner_id=?) AS used",
            (owner_id, owner_id),
        )
        return int(row["used"])

    def reserve(self, owner_id: str, nbytes: int) -> str:
        """功能说明：为一次即将进行的写入预留额度，成功返回预留 ID。

        调用时机：上传/导入在收完请求体、通过格式嗅探后调用（见 §7 上传流程），
        落盘校验失败或失败路径必须接着 [Quota.abort] 回收。

        关键步骤：
        1. 校验字节数非负，并做实时磁盘底线检查（[Quota.check_disk_floor]）；
        2. 开 BEGIN IMMEDIATE 事务，让并发预留串行化：取账号配额、该账号已用
           额度、站内预算与站内已用额度；
        3. 账号配额（member/guest）或站内预算超限即拒绝；
        4. site_settings 尚无 disk_budget_bytes 时现场按空闲空间 80% 写入后继续；
        5. 插入 quota_reservations 行并提交，返回预留 ID。

        输出/副作用：数据库新增一条预留；超配额 413、超预算或空间不足 507、
        账号不存在 404，失败不留半行。
        """
        if nbytes < 0:
            raise FileError("预留字节数不能为负", "INVALID_BYTES", 422)
        self.check_disk_floor(nbytes)
        reservation_id = secrets.token_urlsafe(16)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            limit = self._limit(conn, owner_id)
            used = self._owner_usage(conn, owner_id)
            if limit is not None and used + nbytes > limit:
                raise FileError(f"超出个人配额：已用 {used} 字节，本次 {nbytes} 字节将超过上限 {limit} 字节", "QUOTA_EXCEEDED", 413)
            budget = self._site_budget(conn)
            total = self._site_usage(conn)
            if total + nbytes > budget:
                raise FileError(f"站内空间预算不足：已用 {total} 字节，预算 {budget} 字节", "SITE_BUDGET_EXCEEDED", 507)
            conn.execute("INSERT INTO quota_reservations(id,owner_id,bytes,created_at) VALUES(?,?,?,?)", (reservation_id, owner_id, nbytes, int(time.time())))
        return reservation_id

    def commit(self, reservation_id: str, virtual_path: str, area: str, owner_id: str, nbytes: int) -> None:
        """功能说明：把预留结清为已登记文件：同一事务里复查额度并写存储登记。

        调用时机：上传用 os.replace 原子提交文件之后调用；此时文件已落盘，
        登记失败（撞名/超限）由调用方负责删除文件。

        关键步骤：
        1. 校验虚拟路径与登记区域一致、字节数非负，并做磁盘底线检查；
        2. 开 BEGIN IMMEDIATE 事务取预留行：缺失 404、归属不符 403；
        3. 以“该账号总用量减去本预留”为基数加上实际字节复查账号配额，站内预算
           同类复查（实际字节与预留在额不等时按实际值重算，不让差额漏记账）；
        4. 目标虚拟路径已在 stored_files 中即 409 NAME_EXISTS，绝不覆盖；
        5. 写 stored_files 行并删除预留行，一起提交。

        输出/副作用：stored_files 多一行、quota_reservations 少一行；失败整体
        回滚且预留保留（调用方应接着 [Quota.abort] 回收）。
        """
        self._check_area(virtual_path, area)
        if nbytes < 0:
            raise FileError("登记字节数不能为负", "INVALID_BYTES", 422)
        self.check_disk_floor(nbytes)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner_id,bytes FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise FileError("预留不存在或已被回收，请重新上传", "RESERVATION_NOT_FOUND", 404)
            if row["owner_id"] != owner_id:
                raise FileError("预留不属于该账号", "PATH_FORBIDDEN", 403)
            limit = self._limit(conn, owner_id)
            used = self._owner_usage(conn, owner_id) - row["bytes"]
            if limit is not None and used + nbytes > limit:
                raise FileError(f"超出个人配额：已用 {used} 字节，本次 {nbytes} 字节将超过上限 {limit} 字节", "QUOTA_EXCEEDED", 413)
            budget = self._site_budget(conn)
            total = self._site_usage(conn) - row["bytes"]
            if total + nbytes > budget:
                raise FileError(f"站内空间预算不足：已用 {total} 字节，预算 {budget} 字节", "SITE_BUDGET_EXCEEDED", 507)
            self._insert_file(conn, virtual_path, area, owner_id, nbytes)
            conn.execute("DELETE FROM quota_reservations WHERE id=?", (reservation_id,))

    def abort(self, reservation_id: str) -> None:
        """功能说明：回收一笔预留，让未完成的写入不再占用额度。

        调用时机：上传的每条失败路径（嗅探不符、沙箱校验失败、目标撞名）以及
        清理任务回收过期预留时调用。

        关键步骤：
        1. 删除 quota_reservations 中的该行；
        2. 行不存在也视为成功，重复回收安全。

        输出/副作用：删除预留行、释放额度；幂等。
        """
        self.db.execute("DELETE FROM quota_reservations WHERE id=?", (reservation_id,))

    def record_reserved(self, reservation_id: str, virtual_path: str, area: str, owner_id: str, nbytes: int) -> None:
        """功能说明：在既有预留的覆盖范围内登记一个已落盘文件（批量导入逐条结清）。

        调用时机：[import_sharepack] 先按包总字节 [Quota.reserve] 一次，再对每条
        落盘文件调用本方法登记；全部完成后由调用方 [Quota.abort] 回收预留。

        关键步骤：
        1. 校验虚拟路径与登记区域一致、字节数非负；
        2. 开 BEGIN IMMEDIATE 事务取剩余预留：缺失 404、归属不符 403、单次登记
           超出剩余额 413；
        3. 写 stored_files 行并在同一事务扣减预留字节，使逐条结清期间总占用守恒；
           撞名仍 409。

        输出/副作用：stored_files 新增一行且预留等额减少；同名 409、超出预留 413、
        预留无效 404/403。
        """
        self._check_area(virtual_path, area)
        if nbytes < 0:
            raise FileError("登记字节数不能为负", "INVALID_BYTES", 422)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT owner_id,bytes FROM quota_reservations WHERE id=?", (reservation_id,)).fetchone()
            if row is None:
                raise FileError("预留不存在或已被回收，请重新上传", "RESERVATION_NOT_FOUND", 404)
            if row["owner_id"] != owner_id:
                raise FileError("预留不属于该账号", "PATH_FORBIDDEN", 403)
            if nbytes > int(row["bytes"]):
                raise FileError("登记字节数超过剩余预留", "QUOTA_EXCEEDED", 413)
            self._insert_file(conn, virtual_path, area, owner_id, nbytes)
            conn.execute("UPDATE quota_reservations SET bytes=bytes-? WHERE id=?", (nbytes, reservation_id))

    def record_existing(self, virtual_path: str, area: str, owner_id: str, nbytes: int) -> None:
        """功能说明：把已经落盘的文件补登记进配额账（没有预留的写入）。

        调用时机：分享包解包按实际解出字节登记，或迁移/修复脚本给既有文件补账。

        关键步骤：
        1. 校验虚拟路径与登记区域一致、字节数非负，并做磁盘底线检查；
        2. 开 BEGIN IMMEDIATE 事务复查账号配额与站内预算；
        3. 同名已登记 409；否则写 stored_files 行。

        输出/副作用：stored_files 新增一行；超配额 413、超预算或空间不足 507、
        同名 409、账号不存在 404。
        """
        self._check_area(virtual_path, area)
        if nbytes < 0:
            raise FileError("登记字节数不能为负", "INVALID_BYTES", 422)
        self.check_disk_floor(nbytes)
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            limit = self._limit(conn, owner_id)
            used = self._owner_usage(conn, owner_id)
            if limit is not None and used + nbytes > limit:
                raise FileError(f"超出个人配额：已用 {used} 字节，本次 {nbytes} 字节将超过上限 {limit} 字节", "QUOTA_EXCEEDED", 413)
            budget = self._site_budget(conn)
            total = self._site_usage(conn)
            if total + nbytes > budget:
                raise FileError(f"站内空间预算不足：已用 {total} 字节，预算 {budget} 字节", "SITE_BUDGET_EXCEEDED", 507)
            self._insert_file(conn, virtual_path, area, owner_id, nbytes)

    def remove(self, virtual_path: str) -> None:
        """功能说明：删除文件或目录时同步删掉存储登记，释放对应配额。

        调用时机：/api/files/delete 删除成功后调用（删除失败不要调用）。

        关键步骤：
        1. 删除该路径自身的登记行；
        2. 删除目录时连带删除其下所有登记行：用路径前缀加 “/” 边界匹配，
           substr 而不是 LIKE，避免挂载 ID 里的 _ 被当成通配符。

        输出/副作用：删除 stored_files 行；只写库，不碰文件。
        """
        with self.db.connect() as conn:
            conn.execute("DELETE FROM stored_files WHERE path=? OR substr(path,1,length(?)+1)=?||'/'", (virtual_path, virtual_path, virtual_path))
            conn.execute("DELETE FROM stored_directories WHERE path=? OR substr(path,1,length(?)+1)=?||'/'", (virtual_path, virtual_path, virtual_path))

    def rename(self, old: str, new: str) -> None:
        """功能说明：重命名或移动后改写登记路径，归属与配额不随路径转移。

        调用时机：/api/files/rename 在文件系统移动成功之后调用。

        关键步骤：
        1. 开 BEGIN IMMEDIATE 事务；
        2. 目标路径或其子树已有登记行、且不属于本次搬移范围即 409，避免搬过去撞名；
        3. 用 substr 前缀改写路径自身与整个子树，owner/area/bytes 列不动，
           因此额度仍记在原上传者头上（移动不转移归属）。

        输出/副作用：更新 stored_files.path；撞名抛 FileError(409)，无登记时静默通过。
        """
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clash = conn.execute(
                "SELECT 1 FROM stored_files WHERE (path=? OR substr(path,1,length(?)+1)=?||'/')"
                " AND NOT (path=? OR substr(path,1,length(?)+1)=?||'/') LIMIT 1",
                (new, new, new, old, old, old),
            ).fetchone()
            if clash is not None:
                raise FileError("目标位置已有登记文件，不覆盖", "NAME_EXISTS", 409)
            conn.execute("UPDATE stored_files SET path=?||substr(path,length(?)+1) WHERE path=? OR substr(path,1,length(?)+1)=?||'/'", (new, old, old, old, old))
            conn.execute("UPDATE stored_directories SET path=?||substr(path,length(?)+1) WHERE path=? OR substr(path,1,length(?)+1)=?||'/'", (new, old, old, old, old))

    def ensure_site_budget(self) -> int:
        """功能说明：确保站内预算存在：缺失时初始化写入，已有则原样返回。

        调用时机：服务每次启动时由装配层调用（重启不改变既有预算），
        [Quota.reserve] 在事务内发现缺失时也会补算。

        关键步骤：
        1. 开 BEGIN IMMEDIATE 事务，避免两个启动者同时初始化；
        2. 读 site_settings[disk_budget_bytes]，缺失时按当前空闲空间 80% 写入。

        输出/副作用：返回当前生效的预算字节数；可能新增一行 site_settings。
        """
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._site_budget(conn)

    def recalc_site_budget(self) -> int:
        """功能说明：按当前空闲空间重算站内预算并覆盖写回 site_settings。

        调用时机：CLI recalc-budget 显式重算时调用；启动时请用
        [Quota.ensure_site_budget]，日常请求不重算预算。

        关键步骤：
        1. 取 data_root 所在分区空闲空间乘 0.8 取整；
        2. INSERT OR REPLACE 写入 site_settings[disk_budget_bytes]。

        输出/副作用：返回新的预算字节数；覆盖既有值。
        """
        budget = self._compute_budget()
        with self.db.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO site_settings(key,value) VALUES(?,?)", (BUDGET_KEY, str(budget)))
        return budget

    def check_disk_floor(self, incoming: int) -> None:
        """功能说明：实时磁盘底线检查：写入 incoming 字节后剩余空间不得低于底线。

        调用时机：[Quota.reserve] 与 [Quota.commit] 在事务之前调用，是写入的第一道闸门。

        关键步骤：
        1. incoming 非正数直接放行（不新增占用）；
        2. 取 data_root 所在分区空闲空间，减去 incoming 后与 disk_min_free_bytes 比较。

        输出/副作用：低于底线抛 FileError(507)；只读检查。
        """
        if incoming <= 0:
            return
        free = self._disk_free()
        if free - incoming < self.settings.disk_min_free_bytes:
            raise FileError(f"磁盘剩余空间不足：可用 {free} 字节，写入 {incoming} 字节将低于底线 {self.settings.disk_min_free_bytes} 字节", "DISK_FLOOR", 507)

    def _limit(self, conn: sqlite3.Connection, owner_id: str) -> int | None:
        """功能说明：按账号角色给出个人配额上限：admin 无上限，其余按角色取配置值。

        调用时机：[Quota.reserve]、[Quota.commit]、[Quota.record_existing]
        在事务内调用。

        关键步骤：
        1. 查 users.role，账号不存在抛 FileError(404)；
        2. admin 返回 None（只受站内预算与磁盘底线约束）；
        3. guest 取 guest_quota_bytes，其余角色（member/viewer）取 member_quota_bytes。

        输出/副作用：返回字节数或 None；只读。
        """
        row = conn.execute("SELECT role FROM users WHERE id=?", (owner_id,)).fetchone()
        if row is None:
            raise FileError("账号不存在", "NOT_FOUND", 404)
        if row["role"] == "admin":
            return None
        return self.settings.guest_quota_bytes if row["role"] == "guest" else self.settings.member_quota_bytes

    @staticmethod
    def _owner_usage(conn: sqlite3.Connection, owner_id: str) -> int:
        """功能说明：事务内汇总某账号的已用额度：登记文件加未结清预留。

        调用时机：[Quota.reserve]、[Quota.commit]、[Quota.record_existing]
        在同一事务里做上限复查时调用。

        关键步骤：
        1. 一条 SQL 汇总 stored_files 与 quota_reservations 中该 owner 的 bytes。

        输出/副作用：返回字节数；只读。
        """
        row = conn.execute(
            "SELECT (SELECT COALESCE(SUM(bytes),0) FROM stored_files WHERE owner_id=?)"
            "+(SELECT COALESCE(SUM(bytes),0) FROM quota_reservations WHERE owner_id=?) AS used",
            (owner_id, owner_id),
        ).fetchone()
        return int(row["used"])

    @staticmethod
    def _site_usage(conn: sqlite3.Connection) -> int:
        """功能说明：事务内汇总全站已登记与已预留的字节数（含所有角色）。

        调用时机：[Quota.reserve]、[Quota.commit]、[Quota.record_existing]
        复查站内预算时调用。

        关键步骤：
        1. 一条 SQL 汇总两张表全部行的 bytes。

        输出/副作用：返回字节数；只读。
        """
        row = conn.execute("SELECT (SELECT COALESCE(SUM(bytes),0) FROM stored_files)+(SELECT COALESCE(SUM(bytes),0) FROM quota_reservations) AS used").fetchone()
        return int(row["used"])

    def _site_budget(self, conn: sqlite3.Connection) -> int:
        """功能说明：读取站内预算，缺失时按当前空闲空间 80% 初始化并写入。

        调用时机：[Quota.reserve] 与 [Quota.ensure_site_budget] 在
        BEGIN IMMEDIATE 事务内调用。

        关键步骤：
        1. 读 site_settings[disk_budget_bytes]；
        2. 已有预算不因请求重算；配置站内硬上限时取二者较小值；
        3. 缺失才计算 int(空闲空间 × 0.8) 并写入。

        输出/副作用：返回生效的预算字节数；可能新增一行 site_settings。
        """
        row = conn.execute("SELECT value FROM site_settings WHERE key=?", (BUDGET_KEY,)).fetchone()
        if row is not None:
            budget = int(row["value"])
            return min(budget, self.settings.site_quota_bytes) if self.settings.site_quota_bytes else budget
        budget = self._compute_budget()
        conn.execute("INSERT INTO site_settings(key,value) VALUES(?,?)", (BUDGET_KEY, str(budget)))
        return budget

    def _compute_budget(self) -> int:
        """功能说明：按当前空闲空间算出站内预算：空闲字节 × 0.8 向下取整。

        调用时机：[Quota._site_budget] 初始化与 [Quota.recalc_site_budget] 重算时调用。

        关键步骤：
        1. 取 data_root 所在分区空闲空间；
        2. 乘 0.8 取整返回。

        输出/副作用：返回字节数；只读。
        """
        budget = int(self._disk_free() * BUDGET_RATIO)
        return min(budget, self.settings.site_quota_bytes) if self.settings.site_quota_bytes else budget

    def _disk_free(self) -> int:
        """功能说明：取 data_root 所在分区的空闲字节数，路径还不存在时就近上溯。

        调用时机：[Quota.check_disk_floor]、[Quota._compute_budget] 调用。

        关键步骤：
        1. 从 data_root 起向上找到第一个存在的目录（内容根通常由 prepare 建好）；
        2. shutil.disk_usage 读该目录所在分区的剩余空间。

        输出/副作用：返回字节数；只读，找不到可用路径时抛 FileError(507)。
        """
        probe = self.settings.data_root
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        try:
            return int(shutil.disk_usage(probe).free)
        except OSError as exc:
            raise FileError("无法读取磁盘剩余空间", "DISK_UNAVAILABLE", 507) from exc

    def _check_area(self, virtual_path: str, area: str) -> None:
        """功能说明：校验登记的虚拟路径首段与 area 参数一致，防止记账串区。

        调用时机：[Quota.commit]、[Quota.record_existing] 在开事务之前调用。

        关键步骤：
        1. 用 [Storage.area_of] 解析虚拟路径声明的区域；
        2. 与调用方给的 area 不一致即报错（external 绝对路径一律不允许登记）。

        输出/副作用：不一致抛 FileError(422)；只读。
        """
        declared, _rel = self.storage.area_of(virtual_path)
        if declared != area:
            raise FileError("登记区域与虚拟路径不一致", "INVALID_AREA", 422)

    @staticmethod
    def _insert_file(conn: sqlite3.Connection, virtual_path: str, area: str, owner_id: str, nbytes: int) -> None:
        """功能说明：在事务内写 stored_files 行，同名已登记即报 409（绝不覆盖）。

        调用时机：[Quota.commit] 与 [Quota.record_existing] 复查通过后调用。

        关键步骤：
        1. 按虚拟路径查 stored_files，命中即抛 FileError(409, NAME_EXISTS)；
        2. 否则插入含 owner/area/bytes/created_at 的新行。

        输出/副作用：数据库新增一行存储登记；冲突抛 FileError(409)。
        """
        if conn.execute("SELECT 1 FROM stored_files WHERE path=?", (virtual_path,)).fetchone() is not None:
            raise FileError("目标位置同名已登记，不覆盖", "NAME_EXISTS", 409)
        conn.execute("INSERT INTO stored_files(path,owner_id,area,bytes,created_at) VALUES(?,?,?,?,?)", (virtual_path, owner_id, area, nbytes, int(time.time())))

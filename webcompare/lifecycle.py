"""【模块】访客生命周期与清理 — 到期工作区、过期内容与临时文件的回收

功能说明：server 模式的访客内容必须有硬性保留期，否则临时体验会变成
无限期的匿名存储。本模块把 访客内容的到期规则落成幂等的清理动作：
访客单文件满 24 小时（guest_content_ttl）即删文件与登记行；工作区在主动
退出满 10 分钟（guest_exit_ttl）或闲置满 30 分钟（guest_idle_ttl）后整块
回收——目录、存储登记、会话、比较、分享一起清掉，账号行停用（active=0），
guest_workspaces 留一行 purged=1 作为审计与名额释放凭证，外键始终完整。顺带清理过期会话、
超时未结清的配额预留与 .staging 里残留的 .part 暂存档。

清理只碰 data_root 内的 guest 区域、data_root/.staging 与本服务的数据库
状态行，绝不触碰其它路径（尤其 personal 模式的外部图片目录）；一律先判定
到期再删除，不把过期内容写回。

## 流程
所属流程：HTTP API 与安全边界（webcompare/lifecycle.py）

1. **单文件到期**：访客文件超时即删文件与登记行，配额随之释放 → 见函数 [sweep]、[_expire_guest_files]
2. **工作区回收**：退出/闲置到期后删目录、登记、会话、比较、分享与账号 → 见函数 [sweep]、[_purge_workspaces]、[_guest_dir]
3. **状态行清理**：过期会话与孤儿配额预留 → 见函数 [sweep]、[_expire_state_rows]
4. **暂存残档清理**：.staging 下超 1 小时的 .part 文件 → 见函数 [sweep]、[_clean_staging]
5. **周期调度**：后台任务定时在线程池执行清理，异常不退出 → 见函数 [janitor_loop]
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
import time
from pathlib import Path

from .config import Settings
from .db import Database
from .files import FileError
from .quota import RESERVATION_TTL
from .storage import Storage

logger = logging.getLogger(__name__)

STAGING_DIR = ".staging"  # 上传暂存目录（非内容区，见 storage 区域划分）
STAGING_TTL = 3600  # 暂存 .part 文件超 1 小时视为残留
SWEEP_INTERVAL = 60  # 后台清理间隔（秒）


def sweep(settings: Settings, db: Database, storage: Storage) -> dict:
    """功能说明：执行一轮幂等的过期回收：访客单文件、到期工作区、过期状态行与暂存残档。

    调用时机：应用装配层在 lifespan 启动前同步跑一次（重启补清理），此后由
    [janitor_loop] 每 60 秒在线程池里调用；CLI create-guest-purge 也可手动触发。

    关键步骤：
    1. 取当前时间戳，先做单文件到期（[_expire_guest_files]），再做工作区整块
       回收（[_purge_workspaces]）；
    2. 清理过期会话与超时预留（[_expire_state_rows]）；
    3. 清理 .staging 下超 1 小时的 .part 残档（[_clean_staging]）；
    4. 汇总各步计数，并把各步容忍的失败次数合计为 errors。

    输出/副作用：返回统计字典——guest_files_expired / guest_files_bytes /
    workspaces_purged / workspaces_bytes / sessions_expired /
    reservations_expired / staging_parts_removed / staging_bytes / errors；
    删除磁盘上的过期访客内容与暂存残档，删除对应数据库状态行，访客账号行
    停用（active=0）而非删除，保证 guest_workspaces 审计行外键完整。
    """
    now = int(time.time())
    with storage.mutation_lock:
        files = _expire_guest_files(settings, db, storage, now)
        workspaces = _purge_workspaces(settings, db, now)
    state = _expire_state_rows(settings, db, now)
    staging = _clean_staging(settings, now)
    errors = files.pop("errors") + workspaces.pop("errors") + state.pop("errors") + staging.pop("errors")
    return {**files, **workspaces, **state, **staging, "errors": errors}


def _guest_dir(settings: Settings, workspace_id: str) -> Path | None:
    """功能说明：把工作区标识拼成 guest 区里的真实目录，越界或非法标识一律返回 None。

    调用时机：[_purge_workspaces] 准备 rmtree 之前调用，是“只删 guest 区域”
    这条边界的最后一道校验。

    关键步骤：
    1. 标识必须非空、不含路径分隔符、不是点段、不以点开头，避免拼接逃逸；
    2. 目录不存在（含链接失效）即返回 None：没有可删的目录；
    3. 用 strict 解析真实路径并要求其父目录正是 data_root/guest，且不等于该
       区域目录本身——guest/<ws> 若是指向别处的软链接，解析后父目录不符即拒绝。

    输出/副作用：返回待删除目录的真实路径或 None；只读，不写盘。
    """
    if not workspace_id or workspace_id.startswith(".") or "/" in workspace_id or "\\" in workspace_id:
        return None
    area = settings.data_root / "guest"
    try:
        real = (area / workspace_id).resolve(strict=True)
        base = area.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return real if real.parent == base and real != base else None


def _expire_guest_files(settings: Settings, db: Database, storage: Storage, now: int) -> dict:
    """功能说明：删除已超 [Settings.guest_content_ttl] 的单个访客文件及其存储登记行。

    调用时机：由 [sweep] 每轮首先调用（单文件硬上限，先于工作区回收）。

    关键步骤：
    1. 一条 SQL 选出 area='guest' 且 created_at + guest_content_ttl < now 的
       登记行，取出虚拟路径与实际字节；
    2. 逐行经 [Storage.resolve] 定位真实路径并 unlink：文件已不存在或解析失败
       都容忍（登记行照样删），其它文件系统错误计入 errors 并记日志；
    3. 同一事务删除这批登记行，配额随之释放。

    输出/副作用：返回 guest_files_expired / guest_files_bytes / errors；
    删除磁盘文件与 stored_files 行，不触碰其它区域。
    """
    rows = db.all("SELECT path,bytes FROM stored_files WHERE area='guest' AND created_at + ? < ?", (settings.guest_content_ttl, now))
    expired: list[str] = []
    freed = errors = 0
    for row in rows:
        try:
            storage.resolve(row["path"]).unlink(missing_ok=True)
        except FileError:
            pass  # 文件已不存在或不可达：只清登记，不阻塞清理
        except OSError as exc:
            errors += 1
            logger.warning("删除过期访客文件失败，下轮重试：%s（%s）", row["path"], exc)
            continue
        expired.append(row["path"])
        freed += int(row["bytes"])
    if expired:
        with db.connect() as conn:
            conn.executemany("DELETE FROM stored_files WHERE path=?", [(path,) for path in expired])
    return {"guest_files_expired": len(expired), "guest_files_bytes": freed, "errors": errors}


def _purge_workspaces(settings: Settings, db: Database, now: int) -> dict:
    """功能说明：整块回收到期访客工作区：目录、登记、会话、比较、分享与账号。

    到期判定（仅 purged=0 的行）：exited_at 非空且 now > exited_at +
    guest_exit_ttl，或 now - last_active_at > guest_idle_ttl。

    调用时机：由 [sweep] 在单文件到期之后调用。

    关键步骤：
    1. 一条 SQL 选出全部到期工作区；
    2. 每个工作区先经 [_guest_dir] 校验目录边界后 shutil.rmtree：目录已不存在
       容忍，其它错误记日志并计入 errors，保留登记及名额待下轮重试；
    3. 同一事务统计并删除该工作区 guest/<ws>/ 前缀的 stored_files 行、其
       shares 与 comparisons、sessions、identities、local_credentials 与
       quota_reservations；
    4. 把 guest_workspaces 标为 purged=1（exited_at 为空则补记为清理时刻），
       并把 users 行停用（active=0，display_name 改写为“访客（已清理）”）——
       审计行仍引用该账号，保留行可保持外键完整；单个工作区数据库失败只记
       errors 并继续下一个。

    输出/副作用：返回 workspaces_purged / workspaces_bytes / errors；删除
    guest/<ws> 目录与相关状态行，账号行停用，guest_workspaces 留 purged=1 审计行。
    """
    rows = db.all(
        "SELECT user_id FROM guest_workspaces WHERE purged=0 AND ((exited_at IS NOT NULL AND ? > exited_at + ?) OR (? - last_active_at > ?))",
        (now, settings.guest_exit_ttl, now, settings.guest_idle_ttl),
    )
    purged = freed = errors = 0
    for row in rows:
        workspace = str(row["user_id"])
        directory = _guest_dir(settings, workspace)
        if directory is not None:
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors += 1
                logger.warning("删除访客工作区目录失败，下轮重试：%s（%s）", directory, exc)
                continue
        else:
            # 缺失目录可回收；异常软链接只删链接本身，绝不跟进外部目录。
            candidate = settings.data_root / "guest" / workspace
            try:
                if candidate.is_symlink():
                    candidate.unlink()
                elif candidate.exists():
                    errors += 1
                    continue
            except OSError:
                errors += 1
                continue
        prefix = f"guest/{workspace}"
        claimed = 0
        try:
            with db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                freed += int(conn.execute("SELECT COALESCE(SUM(bytes),0) AS n FROM stored_files WHERE area='guest' AND (path=? OR substr(path,1,length(?)+1)=?||'/')", (prefix, prefix, prefix)).fetchone()["n"])
                conn.execute("DELETE FROM stored_files WHERE area='guest' AND (path=? OR substr(path,1,length(?)+1)=?||'/')", (prefix, prefix, prefix))
                conn.execute("DELETE FROM shares WHERE owner_id=? OR comparison_id IN (SELECT id FROM comparisons WHERE owner_id=?)", (workspace, workspace))
                conn.execute("DELETE FROM comparisons WHERE owner_id=?", (workspace,))
                conn.execute("DELETE FROM stored_directories WHERE path=? OR substr(path,1,length(?)+1)=?||'/'", (prefix, prefix, prefix))
                conn.execute("DELETE FROM sessions WHERE user_id=?", (workspace,))
                conn.execute("DELETE FROM identities WHERE user_id=?", (workspace,))
                conn.execute("DELETE FROM local_credentials WHERE user_id=?", (workspace,))
                conn.execute("DELETE FROM quota_reservations WHERE owner_id=?", (workspace,))
                claimed = conn.execute("UPDATE guest_workspaces SET purged=1, exited_at=COALESCE(exited_at,?) WHERE user_id=? AND purged=0", (now, workspace)).rowcount
                # 账号行保留并停用：guest_workspaces 的 purged=1 审计行仍引用它，
                # 删行会破坏外键完整性；停用后 public_user 视为不存在，会话已删。
                conn.execute("UPDATE users SET active=0, display_name='访客（已清理）' WHERE id=?", (workspace,))
        except sqlite3.Error as exc:
            errors += 1
            logger.warning("回收访客工作区失败，下轮重试：%s（%s）", workspace, exc)
            continue
        purged += 1 if claimed else 0  # 并发清理（如 CLI 与后台任务同时跑）只计一次
    return {"workspaces_purged": purged, "workspaces_bytes": freed, "errors": errors}


def _expire_state_rows(settings: Settings, db: Database, now: int) -> dict:
    """功能说明：删除过期会话与超时未结清的配额预留。

    调用时机：由 [sweep] 在访客内容清理之后调用。

    关键步骤：
    2. 删 expires_at 已到的 sessions 行；
    3. 删 created_at 超过 [RESERVATION_TTL] 的 quota_reservations 行（孤儿预留，
       同名 .part 暂存档由 [_clean_staging] 一并清掉）。

    输出/副作用：返回 sessions_expired / reservations_expired /
    errors（本步无容忍失败，恒为 0）；只删数据库状态行。
    """
    with db.connect() as conn:
        sessions = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount
        reservations = conn.execute("DELETE FROM quota_reservations WHERE created_at < ?", (now - RESERVATION_TTL,)).rowcount
    return {"sessions_expired": sessions, "reservations_expired": reservations, "errors": 0}


def _clean_staging(settings: Settings, now: int) -> dict:
    """功能说明：删除 data_root/.staging 下 mtime 超过 1 小时的 .part 残档与 .unpack 解包目录。

    调用时机：由 [sweep] 每轮最后调用（与孤儿预留清理配套）。

    关键步骤：
    1. .staging 目录不存在直接返回零计数；
    2. 只遍历该目录的直接子项（不递归），只处理 .part 普通文件与 .unpack 目录，
       软链接一律跳过，绝不越出该目录；
    3. mtime 距今不足 [STAGING_TTL] 的条目保留（可能仍有上传在写），超时才删。

    输出/副作用：返回 staging_parts_removed / staging_bytes / errors；删除
    .staging 内的残档文件，不触碰其它路径。
    """
    staging = settings.data_root / STAGING_DIR
    removed = freed = errors = 0
    if not staging.is_dir():
        return {"staging_parts_removed": 0, "staging_bytes": 0, "errors": 0}
    for entry in staging.iterdir():
        if entry.is_symlink():
            continue
        try:
            stat = entry.stat()
            if now - stat.st_mtime <= STAGING_TTL:
                continue
            if entry.suffix == ".part" and entry.is_file():
                entry.unlink()
            elif entry.suffix == ".unpack" and entry.is_dir():
                shutil.rmtree(entry)
            else:
                continue
        except OSError as exc:
            errors += 1
            logger.warning("清理暂存残档失败：%s（%s）", entry, exc)
            continue
        removed += 1
        freed += int(stat.st_size)
    return {"staging_parts_removed": removed, "staging_bytes": freed, "errors": errors}


async def janitor_loop(stop: asyncio.Event, settings: Settings, db: Database, storage: Storage, interval: int = SWEEP_INTERVAL, *, clear_cache=None) -> None:
    """功能说明：后台清理循环：每 interval 秒在线程池执行 [sweep]。

    调用时机：应用装配层在 lifespan 启动时创建为 asyncio 任务，关闭时
    stop.set() 并等待任务结束；启动前的同步首轮清理由装配层直接调用 [sweep]。

    关键步骤：
    1. 等待 interval 秒，stop.set() 时立即结束；
    2. 到期后用 asyncio.to_thread 执行同步 [sweep]；
    3. 单轮异常记日志并等待下一轮；
    4. 有实际回收时清理媒体缓存并记录统计。

    输出/副作用：周期性删除过期访客内容与状态行；协程本身不返回值，
    取消或置位 stop 即退出。
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(1, interval))
        except TimeoutError:
            pass
        else:
            break
        try:
            stats = await asyncio.to_thread(sweep, settings, db, storage)
        except Exception:
            logger.exception("访客清理任务执行失败，将在下一轮重试")
        else:
            if clear_cache and (stats["guest_files_expired"] or stats["workspaces_purged"]):
                await clear_cache(defer_leased=True)
            if any(value for value in stats.values()):
                logger.info("访客清理任务已回收：%s", stats)

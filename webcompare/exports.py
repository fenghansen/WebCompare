"""私有 ZIP 打包任务：SQLite 状态、单任务流式压缩与有界临时缓存。"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from pathlib import Path
from contextlib import nullcontext

from .files import FileError


ZIP_TTL = 24 * 3600
ZIP_TIMEOUT = 180
LOGGER = logging.getLogger(__name__)


class ExportService:
    def __init__(self, settings, db, validate, check_disk, source_lease=None):
        self.settings, self.db = settings, db
        self.validate, self.check_disk = validate, check_disk
        self.source_lease = source_lease or nullcontext
        self.root = settings.data_dir / "exports"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.semaphore = asyncio.Semaphore(1)
        self.tasks = {}
        self.leases = {}
        self.pending_delete = set()
        self.stop = asyncio.Event()
        db.execute("UPDATE export_jobs SET status='failed',reserved_bytes=0,error=? WHERE status IN ('queued','compressing')", ("服务重启，请重新打包",))
        self.cleanup()
        self.janitor = asyncio.create_task(self._janitor())

    def path(self, job_id):
        # 文件名只取服务端生成的 ID；不接受路径片段。
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise FileError("打包任务不存在", "EXPORT_NOT_FOUND", 404)
        return self.root / f"{job_id}.zip"

    def create(self, owner_id, comparison_id, assets, state, total, max_bytes):
        self.cleanup()
        reserve = total + total // 100 + 1024**2
        self.check_disk(reserve)
        job_id, now = secrets.token_hex(16), int(time.time())
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            busy = conn.execute("SELECT COUNT(*) FROM export_jobs WHERE status IN ('queued','compressing')").fetchone()[0]
            own_busy = conn.execute("SELECT COUNT(*) FROM export_jobs WHERE owner_id=? AND status IN ('queued','compressing')", (owner_id,)).fetchone()[0]
            if busy >= 16 or own_busy >= 2:
                raise FileError("打包任务繁忙，请稍后再试", "EXPORT_BUSY", 429)
            count = conn.execute("SELECT COUNT(*) FROM export_jobs").fetchone()[0]
            own_count = conn.execute("SELECT COUNT(*) FROM export_jobs WHERE owner_id=?", (owner_id,)).fetchone()[0]
            if count >= 256 or own_count >= 64:
                raise FileError("打包任务过多，请删除旧任务后重试", "EXPORT_BUSY", 429)
            used = conn.execute("SELECT COALESCE(SUM(reserved_bytes),0) FROM export_jobs").fetchone()[0]
            # 已删除但仍在发送的包，在最后一个下载结束前继续占缓存预算。
            used += sum(path.stat().st_size for path in self.pending_delete if path.exists())
            if used + reserve > self.settings.cache_bytes:
                raise FileError("打包缓存空间不足，请删除旧压缩包后重试", "EXPORT_CACHE_FULL", 507)
            conn.execute("INSERT INTO export_jobs(id,owner_id,comparison_id,assets,state,status,total_bytes,reserved_bytes,created_at,expires_at) VALUES(?,?,?,?,?,'queued',?,?,?,?)", (job_id, owner_id, comparison_id, json.dumps(assets), json.dumps(state), total, reserve, now, now + ZIP_TTL))
        cancel = threading.Event()
        task = asyncio.create_task(self._run(job_id, max_bytes, cancel))
        self.tasks[job_id] = (task, cancel)
        task.add_done_callback(lambda _task: self.tasks.pop(job_id, None))
        return self.get(job_id)

    def get(self, job_id):
        self.path(job_id)
        row = self.db.one("SELECT * FROM export_jobs WHERE id=? AND expires_at>?", (job_id, int(time.time())))
        if not row:
            raise FileError("打包任务不存在或已到期", "EXPORT_NOT_FOUND", 404)
        return row

    @staticmethod
    def payload(row):
        from .downloads import is_file_download
        # 两类任务共用队列，但普通原文件 ZIP 不能冒充可导入的分享包。
        return {key: row[key] for key in ("id", "comparison_id", "status", "processed_bytes", "total_bytes", "bytes", "error", "created_at", "expires_at")} | {"kind": "zip", "purpose": "files" if is_file_download(row) else "share", "download_url": f"/api/exports/{row['id']}/download" if row["status"] == "ready" else None}

    async def _run(self, job_id, max_bytes, cancel):
        async with self.semaphore:
            try:
                if cancel.is_set():
                    return
                row = self.get(job_id)
                self.db.execute("UPDATE export_jobs SET status='compressing' WHERE id=?", (job_id,))
                await asyncio.to_thread(self._pack, row, max_bytes, cancel)
            except Exception as exc:
                detail = getattr(exc, "detail", None)
                message = detail.get("message") if isinstance(detail, dict) else str(exc) if isinstance(exc, (FileError, ValueError)) else "打包失败，请重新打包"
                self.db.execute("UPDATE export_jobs SET status='failed',reserved_bytes=0,error=? WHERE id=?", (message, job_id))
                self._unlink(self.path(job_id))

    def _pack(self, row, max_bytes, cancel):
        # 后台任务不受 HTTP 请求生命周期保护；整个核验和复制过程持有来源缓存租约。
        with self.source_lease():
            return self._pack_sources(row, max_bytes, cancel)

    def _pack_sources(self, row, max_bytes, cancel):
        """功能说明：在同一个受限队列中生成分享包或原文件下载包。

        调用时机：单任务信号量放行后，由后台线程执行。

        关键步骤：
        1. 共享取消、超时、磁盘检查与进度回写回调；
        2. 前后核验权限与版本，仅按服务端私有标记选择原文件 ZIP；
        3. 分享包只含当前显示页，副本状态页码归零；原任务与分享状态不修改；
        4. 校验实际缓存大小，成功发布 ready，失败删除部分 ZIP。

        输出/副作用：写入私有缓存并更新任务状态；不会同时压缩多个任务。
        """
        from .downloads import export_files_zip, is_file_download
        from .sharepack import export_zip
        path = self.path(row["id"])
        started = time.monotonic()

        def check_write(nbytes):
            if cancel.is_set() or time.monotonic() - started > ZIP_TIMEOUT:
                raise ValueError("打包已取消或超时，请重新打包")
            self.check_disk(nbytes)

        def progress(processed, _total):
            check_write(0)
            self.db.execute("UPDATE export_jobs SET processed_bytes=? WHERE id=? AND status='compressing'", (processed, row["id"]))

        try:
            items = self.validate(row, check=check_write)
            if is_file_download(row):
                export_files_zip(items, path, max_bytes=max_bytes, progress=progress, check_write=check_write)
            else:
                state = json.loads(row["state"])
                state["group_index"] = 0
                export_zip(items, state, path, max_bytes=max_bytes, progress=progress, check_write=check_write)
            self.validate(row)
            check_write(0)
            size = path.stat().st_size
            if size > row["reserved_bytes"]:
                raise ValueError("压缩包超出预留缓存空间")
            with self.db.connect() as conn:
                cursor = conn.execute("UPDATE export_jobs SET status='ready',bytes=?,reserved_bytes=?,processed_bytes=total_bytes,error=NULL WHERE id=? AND status='compressing' AND expires_at>?", (size, size, row["id"], int(time.time())))
                if cursor.rowcount != 1:
                    raise ValueError("打包任务已删除或到期")
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def remove(self, job_id):
        """功能说明：撤销任务、清理缓存，并回收仅为原文件任务创建的比较记录。

        调用时机：用户删除任务、到期清理或生命周期回收时调用。

        关键步骤：
        1. 通知正在压缩的线程停止，再删除任务行；
        2. 原文件任务的专用比较没有被其他分享使用时一并删除；
        3. ZIP 有活动下载租约时延迟删除，继续计入缓存预算。

        输出/副作用：移除数据库任务与可回收文件；不删除源媒体或普通比较。
        """
        from .downloads import is_file_download
        row = self.db.one("SELECT * FROM export_jobs WHERE id=?", (job_id,))
        running = self.tasks.get(job_id)
        if running:
            running[1].set()
        self.db.execute("DELETE FROM export_jobs WHERE id=?", (job_id,))
        if row and is_file_download(row):
            self.db.execute("DELETE FROM comparisons WHERE id=? AND NOT EXISTS (SELECT 1 FROM shares WHERE comparison_id=?) AND NOT EXISTS (SELECT 1 FROM export_jobs WHERE comparison_id=?)",
                            (row["comparison_id"], row["comparison_id"], row["comparison_id"]))
        self._unlink(self.path(job_id))

    def pin(self, path):
        self.leases[path] = self.leases.get(path, 0) + 1
        return path

    def unpin(self, path):
        count = self.leases.get(path, 0) - 1
        if count > 0:
            self.leases[path] = count
        else:
            self.leases.pop(path, None)
            if path in self.pending_delete:
                self.pending_delete.remove(path)
                path.unlink(missing_ok=True)

    def _unlink(self, path):
        if path in self.leases:
            self.pending_delete.add(path)
        else:
            path.unlink(missing_ok=True)

    def cleanup(self):
        now = int(time.time())
        for row in self.db.all("SELECT id FROM export_jobs WHERE expires_at<=?", (now,)):
            self.remove(row["id"])
        rows = self.db.all("SELECT * FROM export_jobs")
        keep = set()
        for row in rows:
            path = self.path(row["id"])
            if row["status"] == "ready":
                try:
                    self.validate(row)
                    if not path.is_file():
                        raise ValueError("压缩包已清理，请重新打包")
                except Exception:
                    self.db.execute("UPDATE export_jobs SET status='failed',reserved_bytes=0,error=? WHERE id=?", ("原文件已变更、到期或不可访问，请重新打包", row["id"]))
                    self._unlink(path)
                    continue
            if row["status"] in {"ready", "queued", "compressing"}:
                keep.add(path)
        for path in self.root.glob("*.zip"):
            if path not in keep:
                self._unlink(path)

    async def _janitor(self):
        while not self.stop.is_set():
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                try:
                    self.cleanup()
                except Exception:
                    LOGGER.exception("ZIP 缓存清理失败，将在下次重试")

    async def close(self):
        self.stop.set()
        await self.janitor
        tasks = list(self.tasks.values())
        for _task, cancel in tasks:
            cancel.set()
        await asyncio.gather(*(task for task, _cancel in tasks), return_exceptions=True)

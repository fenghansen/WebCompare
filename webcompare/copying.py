"""【模块】媒体文件粘贴 — 粘贴时才执行的有界、只增不覆盖文件复制

功能说明：把文件区剪贴板保存的路径快照在用户按下粘贴后展开成真实文件。
来源统一通过 FileCatalog 授权、版本核验和物化；目标只接受当前账号可写的
本机目录，SSH 连接保持只读。复用下载收集器的数量、扫描及字节界限，但不
建立下载任务或比较记录。RAW 固定邻接侧车一起复制，冲突时隔离到新目录。

## 流程
所属流程：HTTP API 与安全边界（webcompare/copying.py）

1. **有界预检**：验证来源、目标、版本、字节和 RAW 邻接信息 → 见函数 [copy_files]
2. **安全落盘**：临时文件完整复制后排他提交，同名编号且不覆盖 → 见函数 [_copy_bytes]
3. **事务回收**：失败时移除本次文件、目录和配额预留 → 见函数 [_TargetTree.rollback]
"""
from __future__ import annotations

import errno
import hashlib
import os
import secrets
import shutil
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .downloads import DOWNLOAD_CHUNK, _safe_segment, prepare_file_download
from .files import FileCatalog, FileError, revision

_COPY_LOCK = threading.RLock()
_RAW = {".raw", ".bayer", ".npy"}
COPY_TIMEOUT = 180.0


@dataclass
class _Entry:
    raw: str
    source: object
    version: str
    size: int
    identity_time_ns: int | None
    group: str | None
    parts: tuple[str, ...]
    media: bool = True
    anchor: tuple[str, str, str] | None = None
    digest: str | None = None


def _changed():
    return FileError("复制期间源文件或目标目录已改变，请重新选择", "SOURCE_CHANGED", 409)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise FileError("文件粘贴超过处理时间，请缩小选择范围后重试", "COPY_TIMEOUT", 503)


def _missing(exc):
    return isinstance(exc, OSError) and exc.errno == errno.ENOENT or isinstance(exc, FileError) and exc.code == "NOT_FOUND"


def _signature(st):
    return st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev, getattr(st, "st_ctime_ns", None)


def _identity_time(st):
    # Windows lstat/fstat can disagree on ctime; both expose creation time.
    return getattr(st, "st_birthtime_ns" if os.name == "nt" else "st_ctime_ns", None)


def _json_digest(path: Path, size: int) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size or size > 2 * 1024**2:
            raise _changed()
        content = stream.read(size + 1)
        if len(content) != size or _signature(os.fstat(stream.fileno())) != _signature(before):
            raise _changed()
        return hashlib.sha256(content).hexdigest()


class _TargetTree:
    def __init__(self, root, raw, authorize_write, storage, quota, user, deadline, verify_source, disk_floor):
        self.root, self.raw, self.authorize_write = root, raw, authorize_write
        self.deadline = deadline
        self.verify_source = verify_source
        self.disk_floor = disk_floor
        self.storage, self.quota, self.user = storage, quota, user
        self.directory_mode = storage.settings.content_dir_mode if storage else 0o700
        self.file_mode = storage.settings.content_file_mode if storage else 0o600
        self.fds, self.identities = {}, {}
        self.created_dirs, self.created_files, self.records = [], [], []
        self._open(root)

    def _open(self, path):
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if os.open in os.supports_dir_fd and os.name == "posix":
            descriptor = os.open(path.anchor, flags)
            try:
                for part in path.parts[1:]:
                    child = os.open(part, flags, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
            except BaseException:
                os.close(descriptor)
                raise
            self.fds[path] = descriptor
            st = os.fstat(descriptor)
        else:
            # Keep the same canonical-path checks on platforms without dir_fd.
            st = path.stat()
            self.fds[path] = None
        self.identities[path] = (st.st_dev, st.st_ino)

    def validate(self, directory):
        _check_deadline(self.deadline)
        st = directory.lstat()
        if not stat.S_ISDIR(st.st_mode) or directory.resolve() != directory or (st.st_dev, st.st_ino) != self.identities[directory]:
            raise _changed()
        if directory != self.root:
            self.validate(directory.parent)
        self.authorize_write(directory / "__copy_probe__.png", mkdir=True)

    def exists(self, directory, name):
        if os.path.lexists(directory / name):
            return True
        if self.storage:
            virtual = self.storage.virtual_for(directory / name)
            return bool(self.storage.db.one("SELECT path FROM stored_files WHERE path=? UNION ALL SELECT path FROM stored_directories WHERE path=? LIMIT 1", (virtual, virtual)))
        return False

    def unique(self, directory, name, *, folder=False):
        original = name
        for number in range(1, 10000):
            if number > 1:
                suffix = "" if folder else Path(original).suffix
                stem = original[:-len(suffix)] if suffix else original
                name = f"{stem}~{number}{suffix}"
            if not self.exists(directory, name):
                return name
        raise FileError("目标目录同名项目过多", "NAME_EXISTS", 409)

    def directory(self, parent, name, *, unique=False):
        self.validate(parent)
        if unique:
            name = self.unique(parent, name, folder=True)
        path = parent / name
        if path in self.fds:
            return path
        virtual = self.authorize_write(path, mkdir=True)
        fd = self.fds[parent]
        if fd is None:
            path.mkdir(mode=self.directory_mode)
        else:
            os.mkdir(name, mode=self.directory_mode, dir_fd=fd)
        self.created_dirs.append(path)
        self._open(path)
        if self.fds[path] is not None:
            os.fchmod(self.fds[path], self.directory_mode)
        else:
            path.chmod(self.directory_mode)
        if self.storage:
            self.storage.db.execute("INSERT INTO stored_directories(path,owner_id,created_at) VALUES(?,?,?)", (virtual, self.user["id"], int(time.time())))
        return path

    def unlink(self, directory, name, identity=None):
        fd = self.fds[directory]
        try:
            st = (directory / name).lstat() if fd is None else os.stat(name, dir_fd=fd, follow_symlinks=False)
            if identity is not None and (st.st_dev, st.st_ino) != identity:
                return
            if fd is None:
                (directory / name).unlink()
            else:
                os.unlink(name, dir_fd=fd)
        except FileNotFoundError:
            pass

    def rollback(self):
        """功能说明：回收本次排他创建的目标，不删除既有文件或被替换的外部文件。

        调用时机：复制、源版本复核或配额记账的任一步骤失败。

        关键步骤：
        1. 按设备号与 inode 删除本次文件，回收已登记的配额。
        2. 逆序删除本次创建且仍为空的目录，并移除目录登记。

        输出/副作用：只清理本次生成的本机产物；源文件不受影响。
        """
        for directory, name, identity in reversed(self.created_files):
            try:
                self.unlink(directory, name, identity)
            except OSError:
                pass
        if self.quota:
            for virtual in reversed(self.records):
                self.quota.remove(virtual)
        for path in reversed(self.created_dirs):
            try:
                parent_fd = self.fds[path.parent]
                if parent_fd is None:
                    path.rmdir()
                else:
                    os.rmdir(path.name, dir_fd=parent_fd)
            except OSError:
                continue
            if self.quota:
                self.quota.remove(self.storage.virtual_for(path))

    def close(self):
        for fd in self.fds.values():
            if fd is not None:
                os.close(fd)


def _copy_bytes(local: Path, entry: _Entry, tree: _TargetTree, directory: Path, name: str):
    """功能说明：把已物化源的原字节复制到排他临时文件，再无覆盖提交。

    调用时机：所有来源、目标和额度预检完成后逐个文件调用。

    关键步骤：
    1. 以 NOFOLLOW 打开普通源文件，保存描述符签名并按预期大小分块复制。
    2. 前后核验描述符，JSON 同时计算内容摘要；完整临时文件用排他硬链接提交。
    3. 记录本次目标 inode，任何退出路径移除临时名称。

    输出/副作用：新增一个完整目标文件；不覆盖既有文件，不改变源内容。
    """
    tree.validate(directory)
    temporary = ".copy-" + secrets.token_hex(12)
    parent_fd = tree.fds[directory]
    source_fd = target_fd = None
    try:
        source_fd = os.open(local, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size != entry.size:
            raise _changed()
        if isinstance(entry.source, Path) and revision(entry.source, st=before) != entry.version:
            raise _changed()
        if entry.identity_time_ns is not None and _identity_time(before) != entry.identity_time_ns:
            raise _changed()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        target_fd = os.open(directory / temporary, flags, 0o600) if parent_fd is None else os.open(temporary, flags, 0o600, dir_fd=parent_fd)
        digest = hashlib.sha256() if not entry.media and entry.source.suffix.lower() == ".json" else None
        with os.fdopen(source_fd, "rb") as incoming, os.fdopen(target_fd, "wb") as outgoing:
            source_fd = target_fd = None
            remaining = entry.size
            while remaining:
                _check_deadline(tree.deadline)
                chunk = incoming.read(min(DOWNLOAD_CHUNK, remaining))
                if not chunk:
                    raise _changed()
                outgoing.write(chunk)
                if digest:
                    digest.update(chunk)
                remaining -= len(chunk)
            if incoming.read(1) or _signature(os.fstat(incoming.fileno())) != _signature(before):
                raise _changed()
            outgoing.flush()
            if os.name == "posix":
                os.fchmod(outgoing.fileno(), tree.file_mode)
            else:
                (directory / temporary).chmod(tree.file_mode)
        tree.validate(directory)
        tree.verify_source(entry)
        tree.authorize_write(directory / name)
        recorded = False
        try:
            if parent_fd is None:
                os.link(directory / temporary, directory / name, follow_symlinks=False)
                current = (directory / name).lstat()
            else:
                os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV}:
                raise
            # FAT and some FUSE mounts accept writes but not hard links. Keep
            # no-overwrite semantics with an exclusive leaf; record its inode
            # immediately so even a failed fallback removes its partial file.
            if shutil.disk_usage(tree.root).free - entry.size < tree.disk_floor:
                raise FileError("目标磁盘剩余空间不足", "DISK_FLOOR", 507) from exc
            output_fd = os.open(directory / name, flags, 0o600) if parent_fd is None else os.open(name, flags, 0o600, dir_fd=parent_fd)
            current = os.fstat(output_fd)
            tree.created_files.append((directory, name, (current.st_dev, current.st_ino)))
            recorded = True
            input_fd = None
            try:
                input_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                input_fd = os.open(directory / temporary, input_flags) if parent_fd is None else os.open(temporary, input_flags, dir_fd=parent_fd)
                with os.fdopen(input_fd, "rb") as incoming, os.fdopen(output_fd, "wb") as outgoing:
                    input_fd = output_fd = None
                    while chunk := incoming.read(DOWNLOAD_CHUNK):
                        _check_deadline(tree.deadline)
                        tree.validate(directory)
                        outgoing.write(chunk)
                    outgoing.flush()
                    if os.name == "posix":
                        os.fchmod(outgoing.fileno(), tree.file_mode)
                    else:
                        (directory / name).chmod(tree.file_mode)
            finally:
                if input_fd is not None:
                    os.close(input_fd)
                if output_fd is not None:
                    os.close(output_fd)
        if not recorded:
            tree.created_files.append((directory, name, (current.st_dev, current.st_ino)))
        entry.digest = digest.hexdigest() if digest else None
        tree.validate(directory)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if target_fd is not None:
            os.close(target_fd)
        tree.unlink(directory, temporary)


def copy_files(catalog: FileCatalog, user: dict, paths: list[str], destination: str, recursive: bool = True,
               media_kind: str = "image", *, storage=None, quota=None) -> dict:
    """功能说明：在粘贴时把有界媒体快照复制到账号可写的本机目录。

    调用时机：POST /api/file-copies 在线程池调用；Ctrl+C 只保存前端快照，不调用这里。

    关键步骤：
    1. 复用统一来源授权与下载有界收集，不创建比较或下载任务，拒绝 SSH 目标与递归进自身。
    2. 收集固定普通 RAW 侧车并计入字节上限；预留配额与磁盘空间，冲突时编号或隔离 RAW。
    3. 有限块复制原字节，重复核验来源和目标；成功登记配额，失败回收本次所有产物。

    输出/副作用：返回 copied、total_bytes、paths；只新增目标文件，不修改来源或覆盖既有文件。
    """
    if user.get("role") not in {"member", "admin"}:
        raise FileError("当前账号不能粘贴文件；访客只能导入分享包", "COPY_FORBIDDEN", 403)
    if destination.lower().startswith("ssh://"):
        raise FileError("SSH 文件来源为只读，不能向远端粘贴", "SSH_READ_ONLY", 403)
    deadline = time.monotonic() + COPY_TIMEOUT
    owner_id = user["id"]
    settings = catalog.settings
    limit = 1024**3 if user["role"] == "admin" else 100 * 1024**2
    collection_limits = {}
    if storage and settings.profile == "intranet":
        limit = quota.ensure_site_budget() if user["role"] == "admin" else settings.member_quota_bytes
        collection_limits = {"max_files": 50_000, "max_entries": 500_000, "max_directories": 100_000}

    def authorize(raw):
        _check_deadline(deadline)
        if storage:
            return storage.resolve(storage.authorize(user, raw, "read"))
        catalog.authorize(raw, owner_id)
        return catalog.resolve(raw)

    if storage:
        probe = storage.authorize(user, destination.rstrip("/") + "/__copy_probe__.png", "mkdir")
        dest_raw = probe.rsplit("/", 1)[0]
        dest = storage.resolve(dest_raw)
    else:
        dest = catalog.resolve(destination)
        dest_raw = str(dest)
    if not isinstance(dest, Path) or not dest.is_dir():
        raise FileError("粘贴目标必须是本机目录", "INVALID_DESTINATION")

    def write_path(path, *, mkdir=False):
        _check_deadline(deadline)
        if not path.is_relative_to(dest):
            raise FileError("目标路径越出所选目录", "PATH_ESCAPE", 403)
        raw = dest_raw.rstrip("/") + "/" + path.relative_to(dest).as_posix()
        return storage.authorize(user, raw, "mkdir" if mkdir else "write") if storage else str(path)

    def verify(entry):
        current = authorize(entry.raw)
        attrs = current.lstat()
        if not stat.S_ISREG(attrs.st_mode) or revision(current, st=attrs) != entry.version:
            raise _changed()
        if entry.identity_time_ns is not None and _identity_time(attrs) != entry.identity_time_ns:
            raise _changed()
        return current

    lock = storage.mutation_lock if storage else _COPY_LOCK
    reservation = None
    tree = None
    with lock, catalog.cache_lease():
        try:
            roots = [(authorize(raw), raw) for raw in paths]
            roots = [(root, raw, root.is_dir()) for root, raw in roots]
            for root, _raw, is_directory in roots:
                if is_directory:
                    try:
                        dest.relative_to(root)
                    except (ValueError, TypeError):
                        pass
                    else:
                        raise FileError("不能把目录复制到自身或其子目录", "COPY_INTO_SELF")
            try:
                manifest, snapshots, _total = prepare_file_download(catalog, owner_id, paths, recursive, media_kind, authorize, limit, persist=False, **collection_limits)
            except FileError as exc:
                if exc.code.startswith("DOWNLOAD_"):
                    raise FileError("粘贴媒体超过数量、大小或扫描上限，请缩小选择范围", exc.code.replace("DOWNLOAD_", "COPY_"), exc.status) from exc
                if exc.code == "EMPTY_DOWNLOAD":
                    raise FileError("所选路径中没有可粘贴的媒体文件", "EMPTY_COPY", exc.status) from exc
                if exc.code == "INVALID_DOWNLOAD_FILE":
                    raise FileError("粘贴来源必须是普通媒体文件或目录", "INVALID_COPY_FILE", exc.status) from exc
                if "下载" in str(exc):
                    raise FileError(str(exc).replace("下载", "粘贴"), exc.code, exc.status) from exc
                raise
            entries = []
            total = 0
            metadata_seen = set()
            for saved in snapshots:
                asset = manifest["assets"][saved["id"]]
                source = authorize(asset["path"])
                attrs = source.lstat()
                group = None
                parts = (source.name,)
                for root, _raw, is_directory in roots:
                    if not is_directory and str(root) == str(source):
                        break
                    if is_directory:
                        try:
                            relative = source.relative_to(root)
                        except (ValueError, TypeError):
                            continue
                        group = str(root)
                        parts = (_safe_segment(root.name or "files"), *relative.parts)
                        break
                else:
                    raise FileError("目录中的来源链接越出所选范围，请直接选择原文件", "PATH_ESCAPE", 403)
                main = _Entry(asset["path"], source, saved["revision"], saved["bytes"], _identity_time(attrs), group, parts)
                verify(main)
                entries.append(main)
                total += main.size
                if source.suffix.lower() in _RAW:
                    for name in dict.fromkeys(("raw.json", source.name + ".json", "meta.json", "frames.jsonl")):
                        candidate = source.parent / name
                        try:
                            meta_st = candidate.lstat()
                        except (OSError, FileError) as exc:
                            if _missing(exc):
                                continue
                            raise
                        if not stat.S_ISREG(meta_st.st_mode):
                            continue
                        maximum = 64 * 1024**2 if name == "frames.jsonl" else 2 * 1024**2
                        if meta_st.st_size > maximum:
                            raise FileError("RAW 邻接元数据超过读取上限", "RAW_METADATA_LIMIT")
                        raw = catalog._register_path(candidate)
                        metadata_key = (group, parts[:-1], raw)
                        if group and metadata_key in metadata_seen:
                            continue
                        metadata_seen.add(metadata_key)
                        checked = authorize(raw)
                        entries.append(_Entry(raw, checked, revision(checked, st=meta_st), meta_st.st_size,
                                              _identity_time(meta_st), group, (*parts[:-1], name), False,
                                              (main.raw, main.version, name)))
                        total += meta_st.st_size
                if total > limit:
                    raise FileError("媒体与 RAW 元数据超过粘贴大小上限", "COPY_TOO_LARGE", 413)
            if shutil.disk_usage(dest).free - total < settings.disk_min_free_bytes:
                raise FileError("目标磁盘剩余空间不足", "DISK_FLOOR", 507)
            if quota:
                reservation = quota.reserve(owner_id, total)
            tree = _TargetTree(dest, dest_raw, write_path, storage, quota, user, deadline, verify, settings.disk_min_free_bytes)
            groups, bundles, sidecars, results = {}, {}, set(), []
            actual_total = 0
            for entry in entries:
                verify(entry)
                if entry.group:
                    if entry.group not in groups:
                        groups[entry.group] = tree.directory(dest, entry.parts[0], unique=True)
                    directory = groups[entry.group]
                    relative = entry.parts[1:]
                    for segment in relative[:-1]:
                        directory = tree.directory(directory, segment)
                else:
                    directory = dest
                    if entry.anchor:
                        directory = bundles.get(entry.anchor[0], dest)
                    elif entry.source.suffix.lower() in _RAW:
                        adjacent = [meta for meta in entries if meta.anchor and meta.anchor[0] == entry.raw]
                        if adjacent and any(tree.exists(dest, value) for value in [entry.source.name, *[meta.parts[-1] for meta in adjacent]]):
                            directory = tree.directory(dest, _safe_segment(entry.source.stem + "-files"), unique=True)
                        bundles[entry.raw] = directory
                name = entry.parts[-1]
                if not entry.media:
                    identity = (directory, name, entry.raw)
                    if identity in sidecars:
                        continue
                    sidecars.add(identity)
                name = tree.unique(directory, name) if entry.media else name
                if not entry.media and tree.exists(directory, name):
                    raise FileError("RAW 元数据目标冲突，请选择空目录", "RAW_COPY_CONFLICT", 409)
                local = catalog.materialize(entry.anchor[0], entry.anchor[1], allow_video=True).parent / entry.anchor[2] if entry.anchor else catalog.materialize(entry.raw, entry.version, allow_video=True)
                if shutil.disk_usage(dest).free - (total - actual_total) < settings.disk_min_free_bytes:
                    raise FileError("目标磁盘剩余空间不足", "DISK_FLOOR", 507)
                # Authorize the absent leaf before placement. In the member
                # area an existing unregistered leaf is intentionally protected
                # as legacy/admin content until record_reserved records it.
                virtual = write_path(directory / name)
                _copy_bytes(local, entry, tree, directory, name)
                verify(entry)
                if quota:
                    area, _rel = storage.area_of(virtual)
                    tree.records.append(virtual)
                    quota.record_reserved(reservation, virtual, area, owner_id, entry.size)
                actual_total += entry.size
                if entry.media:
                    results.append(storage.display_path(virtual) if storage else str(directory / name))
            for entry in entries:
                verify(entry)
                if entry.digest is not None:
                    local = catalog.materialize(entry.anchor[0], entry.anchor[1], allow_video=True).parent / entry.anchor[2]
                    if _json_digest(local, entry.size) != entry.digest:
                        raise _changed()
            for directory in tree.identities:
                tree.validate(directory)
            if quota:
                quota.abort(reservation)
                reservation = None
            return {"copied": len(results), "total_bytes": actual_total, "paths": results}
        except OSError as exc:
            if tree:
                tree.rollback()
            raise FileError("复制失败：文件或目标目录不可写或已改变", "COPY_FAILED", 422) from exc
        except BaseException:
            if tree:
                tree.rollback()
            raise
        finally:
            if quota and reservation:
                quota.abort(reservation)
            if tree:
                tree.close()

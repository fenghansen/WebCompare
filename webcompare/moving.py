"""本机同作用域移动：原子改名，不复制媒体，不改变归属或配额。"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import stat
import sys

from .files import FileError


def authorize_subtree(storage, user, virtual):
    """沿用成员区创建者权限，不能借父目录移动其他用户或旧未登记内容。"""
    if user["role"] != "member" or not virtual.startswith("member/"):
        return
    for table in ("stored_files", "stored_directories"):
        if storage.db.one(f"SELECT 1 FROM {table} WHERE (path=? OR substr(path,1,length(?)+1)=?||'/') AND owner_id!=? LIMIT 1",
                          (virtual, virtual, virtual, user["id"])):
            raise FileError("文件夹内含其他用户的内容，只有管理员可以整块改写", "PATH_FORBIDDEN", 403)
    source = storage.resolve(virtual)
    if source.is_dir():
        for entry in source.rglob("*"):
            storage.authorize(user, storage.virtual_for(entry), "write")


def _scope(storage, virtual):
    area, rel = storage.area_of(virtual)
    return area, rel.split("/", 1)[0] if rel and area in {"private", "guest", "mount"} else None


def _identity(st):
    return st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode)


def _open_parent(path):
    """逐层 NOFOLLOW 打开父目录，后续 rename 使用已授权目录的描述符。"""
    if os.name != "posix" or os.open not in os.supports_dir_fd:
        return None, _identity(path.lstat())
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, _identity(os.fstat(descriptor))
    except BaseException:
        os.close(descriptor)
        raise


def _rename_noreplace(source, target, source_fd, target_fd):
    """Linux 用 RENAME_NOREPLACE；Windows 的 rename 本身拒绝覆盖。"""
    if sys.platform == "linux":
        function = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
        if function is None:
            raise FileError("当前系统不支持无覆盖移动", "MOVE_UNSUPPORTED", 503)
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        old = os.fsencode(source.name if source_fd is not None else source)
        new = os.fsencode(target.name if target_fd is not None else target)
        if function(source_fd if source_fd is not None else -100, old,
                    target_fd if target_fd is not None else -100, new, 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
    elif os.name == "nt":
        os.rename(source, target)
    else:
        raise FileError("当前系统不支持无覆盖移动", "MOVE_UNSUPPORTED", 503)


def _rename_metadata(connection, old, new):
    for table in ("stored_files", "stored_directories"):
        connection.execute(f"UPDATE {table} SET path=?||substr(path,length(?)+1) WHERE path=? OR substr(path,1,length(?)+1)=?||'/'",
                           (new, old, old, old, old))


def move_files(catalog, user, paths, destination=None, *, new_path=None, storage=None, quota=None):
    """整体移动选中根项目；同区保留 owner/area/bytes，失败时回退本批改名。"""
    if storage is None or quota is None:
        raise FileError("personal 模式不提供移动接口", "MOVE_FORBIDDEN", 403)
    if any(path.lower().startswith("ssh://") for path in [*paths, destination or "", new_path or ""]):
        raise FileError("SSH 来源保持只读，不能移动远端文件", "SSH_READ_ONLY", 403)
    if not paths or len(paths) > 500 or new_path is not None and len(paths) != 1:
        raise FileError("一次最多移动 500 个选中项目", "INVALID_MOVE", 422)
    parents, completed, plans = {}, [], []

    def parent(path):
        if path not in parents:
            parents[path] = _open_parent(path)
        return parents[path][0]

    def validate_parent(path):
        descriptor, expected = parents[path]
        if path.resolve() != path or _identity(path.lstat()) != expected:
            raise FileError("移动期间目录已改变，请刷新后重试", "SOURCE_CHANGED", 409)
        if descriptor is not None and _identity(os.fstat(descriptor)) != expected:
            raise FileError("移动期间目录已改变，请刷新后重试", "SOURCE_CHANGED", 409)

    def current(path):
        descriptor = parents[path.parent][0]
        return path.lstat() if descriptor is None else os.stat(path.name, dir_fd=descriptor, follow_symlinks=False)

    with storage.mutation_lock, catalog.cache_lease():
        try:
            roots = []
            for raw in paths:
                old = storage.authorize(user, raw, "write")
                declared = Path(raw).expanduser() if storage._absolute(raw) else storage._physical(storage._split(raw))
                if declared.is_symlink():
                    raise FileError("请移动原文件，不能移动来源符号链接", "PATH_ESCAPE", 403)
                source = storage.resolve(old)
                st = source.lstat()
                if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
                    raise FileError("只能移动普通文件或文件夹", "INVALID_MOVE_FILE", 422)
                old = storage.virtual_for(source)
                if not any(item[1] == source for item in roots):
                    roots.append((old, source, st))
            roots = [item for item in roots if not any(item[1] != other[1] and stat.S_ISDIR(other[2].st_mode)
                     and item[1].is_relative_to(other[1]) for other in roots)]
            dest = None
            if new_path is None:
                dest = storage.resolve(storage.authorize(user, destination or "", "read"))
                if not dest.is_dir():
                    raise FileError("移动目标必须是已有本机目录", "INVALID_DESTINATION", 422)
            targets = set()
            for old, source, st in roots:
                raw_target = new_path if new_path is not None else storage.virtual_for(dest / source.name)
                new = storage.authorize(user, raw_target, "mkdir" if stat.S_ISDIR(st.st_mode) else "write")
                target = storage._verified(new, allow_missing=True)
                new = storage.virtual_for(target)
                if _scope(storage, old) != _scope(storage, new):
                    raise FileError("不允许跨区域或用户作用域移动，请改用复制", "CROSS_AREA", 403)
                if stat.S_ISDIR(st.st_mode) and target.is_relative_to(source):
                    raise FileError("不能把文件夹移动到自身或子文件夹", "MOVE_INTO_SELF", 422)
                if target in targets or os.path.lexists(target):
                    raise FileError("目标位置已存在，不合并或覆盖", "NAME_EXISTS", 409)
                targets.add(target)
                if not target.parent.is_dir():
                    raise FileError("目标目录不存在", "NOT_FOUND", 404)
                if new.startswith("member/") and new.count("/") == 1 and not stat.S_ISDIR(st.st_mode):
                    raise FileError("成员区根目录只允许放文件夹", "FOLDERS_ONLY", 403)
                authorize_subtree(storage, user, old)
                source_fd, target_fd = parent(source.parent), parent(target.parent)
                if st.st_dev != target.parent.stat().st_dev:
                    raise FileError("跨文件系统不能直接移动，请使用复制", "CROSS_FILESYSTEM", 409)
                plans.append((old, new, source, target, _identity(st), source_fd, target_fd))
            with storage.db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                for _old, new, *_rest in plans:
                    for table in ("stored_files", "stored_directories"):
                        if connection.execute(f"SELECT 1 FROM {table} WHERE path=? OR substr(path,1,length(?)+1)=?||'/' LIMIT 1",
                                              (new, new, new)).fetchone():
                            raise FileError("目标位置已有登记内容，不覆盖", "NAME_EXISTS", 409)
                for old, new, source, target, identity, source_fd, target_fd in plans:
                    validate_parent(source.parent)
                    validate_parent(target.parent)
                    storage.authorize(user, old, "write")
                    storage.authorize(user, new, "mkdir" if source.is_dir() else "write")
                    if _identity(current(source)) != identity:
                        raise FileError("移动期间来源已改变，请刷新后重试", "SOURCE_CHANGED", 409)
                    _rename_noreplace(source, target, source_fd, target_fd)
                    completed.append((old, new, source, target, identity, source_fd, target_fd))
                    if _identity(current(target)) != identity:
                        raise FileError("移动期间来源已改变，请刷新后重试", "SOURCE_CHANGED", 409)
                    _rename_metadata(connection, old, new)
            return {"moved": len(plans), "paths": [item[1] if new_path is not None else storage.display_path(item[1]) for item in plans]}
        except BaseException as error:
            for _old, _new, source, target, identity, source_fd, target_fd in reversed(completed):
                try:
                    validate_parent(source.parent)
                    validate_parent(target.parent)
                    if _identity(current(target)) != identity:
                        raise FileError("移动目标已被外部修改", "SOURCE_CHANGED", 409)
                    _rename_noreplace(target, source, target_fd, source_fd)
                except (OSError, FileError) as rollback_error:
                    raise FileError("移动回退受外部文件变更阻止，请刷新目录并联系管理员", "MOVE_ROLLBACK_FAILED", 500) from rollback_error
            if isinstance(error, OSError):
                if error.errno in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise FileError("目标位置已存在，不合并或覆盖", "NAME_EXISTS", 409) from error
                if error.errno == errno.EXDEV:
                    raise FileError("跨文件系统不能直接移动，请使用复制", "CROSS_FILESYSTEM", 409) from error
                if isinstance(error, PermissionError):
                    raise FileError("文件或目标目录不可写", "PATH_FORBIDDEN", 403) from error
                raise FileError("移动失败：文件或目录不可写或已改变", "MOVE_FAILED", 422) from error
            raise
        finally:
            for descriptor, _identity_value in parents.values():
                if descriptor is not None:
                    os.close(descriptor)

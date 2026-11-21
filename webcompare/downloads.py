"""【模块】原文件下载 — 有界收集媒体并流式打成只含原字节的 ZIP

功能说明：文件选择器可选多个文件或目录，本模块在授权回调限定的读取
边界内生成最多 500 个媒体的下载清单，再复用私有导出队列打包。目录扫描
同时限制媒体数、目录数与总条目数，避免没有媒体的巨大目录耗尽工作线程。
压缩包只包含安全相对名称和原文件，不写 manifest、网页或服务器绝对路径。

## 流程
所属流程：原文件下载（webcompare/downloads.py）

1. **有界收集**：逐路径授权、按媒体类型展开并登记版本 → 见函数 [prepare_file_download]
2. **安全命名**：保留相对层级并确定性处理重名 → 见函数 [_archive_name]、[_unique_name]
3. **原字节打包**：有限块复制、核验文件描述符版本并清理失败产物 → 见函数 [export_files_zip]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from .files import FileCatalog, FileError, natural_key, revision
from .storage import WINDOWS_RESERVED_NAMES

MAX_DOWNLOAD_FILES = 500
MAX_DOWNLOAD_ENTRIES = 10_000
MAX_DOWNLOAD_DIRECTORIES = 5_000
DOWNLOAD_CHUNK = 1024 * 1024
FILES_ONLY_STATE_KEY = "_webcompare_files_only"


def is_file_download(row: dict) -> bool:
    """功能说明：识别仅由服务端创建的原文件下载任务。

    调用时机：导出队列分流、源文件核验和下载附件命名时调用。

    关键步骤：
    1. 读取任务私有 state，要求专用标记的值为整数 1；
    2. 普通分享请求的 ViewState 禁止额外字段，不能注入此顶层标记。

    输出/副作用：返回布尔值；只读取已登记任务，不修改状态。
    """
    state = json.loads(row["state"])
    return type(state.get(FILES_ONLY_STATE_KEY)) is int and state[FILES_ONLY_STATE_KEY] == 1


def _safe_segment(value: str) -> str:
    """功能说明：把单个原文件名转换为各平台可安全解出的相对路径段。

    调用时机：下载清单为文件或目录生成 ZIP 成员名时调用。

    关键步骤：
    1. 替换分隔符、控制字符及 Windows 保留字符，去除危险首尾点空格；
    2. 对保留设备名加前缀，超长名称以短摘要收尾并保留后缀。

    输出/副作用：返回不超过 160 UTF-8 字节的非空文件名片段。
    """
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", value).strip(". ") or "file"
    if cleaned.split(".", 1)[0].rstrip(" .").upper() in WINDOWS_RESERVED_NAMES:
        cleaned = "_" + cleaned
    if len(cleaned.encode("utf-8")) > 160:
        suffix = PurePosixPath(cleaned).suffix.encode("utf-8")[:16].decode("utf-8", errors="ignore")
        prefix = cleaned.encode("utf-8")[:120].decode("utf-8", errors="ignore")
        cleaned = f"{prefix}~{hashlib.sha256(value.encode()).hexdigest()[:12]}{suffix}"
    return cleaned


def _archive_name(parts: tuple[str, ...]) -> str:
    """功能说明：为选中媒体保留合理目录层级，并限制 ZIP 名称的额外开销。

    调用时机：有界扫描得到一个媒体文件后调用。

    关键步骤：
    1. 净化每个路径段并用 ZIP 的正斜杠连接；
    2. 总名称超过 480 UTF-8 字节时，把中间目录压成稳定摘要。

    输出/副作用：返回安全相对成员名，不含服务器路径前缀。
    """
    safe = [_safe_segment(part) for part in parts]
    name = "/".join(safe)
    if len(name.encode("utf-8")) > 480:
        name = "/".join((safe[0], "path-" + hashlib.sha256(name.encode()).hexdigest()[:12], safe[-1]))
    return name


def _unique_name(name: str, used: set[str]) -> str:
    """功能说明：确定性解决不同来源的同名文件及文件/目录前缀冲突。

    调用时机：按用户选择顺序及来源内自然顺序登记 ZIP 成员时调用。

    关键步骤：
    1. 对大小写折叠后的完整路径和各级前缀检查冲突；
    2. 在冲突路径段后追加 ~2、~3 等编号，保留媒体后缀。

    输出/副作用：返回唯一名称，并把最终文件路径写入 used。
    """
    parts = list(PurePosixPath(name).parts)
    for index in range(len(parts)):
        original, sequence = parts[index], 1
        while True:
            candidate = "/".join(parts[:index + 1]).casefold()
            is_leaf = index == len(parts) - 1
            conflict = candidate in used or is_leaf and any(path.startswith(candidate + "/") for path in used)
            if not conflict:
                break
            sequence += 1
            suffix = PurePosixPath(original).suffix if is_leaf else ""
            stem = original[:-len(suffix)] if suffix else original
            parts[index] = f"{stem}~{sequence}{suffix}"
    final = "/".join(parts)
    used.add(final.casefold())
    return final


def prepare_file_download(catalog: FileCatalog, owner_id: str, paths: list[str], recursive: bool,
                          media_kind: str, authorize: Callable[[str], Path], max_bytes: int, *, persist: bool = True,
                          max_files: int | None = None, max_entries: int | None = None,
                          max_directories: int | None = None) -> tuple[dict, list[dict], int]:
    """功能说明：把选中的文件和目录变成有数量、大小与扫描界限的私有下载清单。

    调用时机：POST /api/file-downloads 在工作线程中完成权限预检后调用；
    文件粘贴只借用有界快照，传 persist=False，不建立下载比较记录。

    关键步骤：
    1. 每个来源及每个展开子项调用授权回调，拒绝越界路径与目录中的符号链接；
    2. 全任务累计扫描条目、目录、唯一媒体数和原字节，任何超限立即报错，绝不截断；
    3. 按选择顺序和来源内自然排序生成安全相对名，重复选中的同一文件只保留一次；
    4. 用现有资产版本算法登记媒体，并插入队列外键要求的私有比较记录。

    输出/副作用：返回 (比较清单, 任务资产快照, 总原字节)，写入 assets 与
    comparisons；不解码媒体、不改变原文件，不放宽普通比较的来源数限制。
    """
    catalog._validate_media_kind(media_kind)
    max_files = MAX_DOWNLOAD_FILES if max_files is None else max_files
    max_entries = MAX_DOWNLOAD_ENTRIES if max_entries is None else max_entries
    max_directories = MAX_DOWNLOAD_DIRECTORIES if max_directories is None else max_directories
    if not 1 <= len(paths) <= max_files:
        raise FileError(f"一次最多选择 {max_files} 个文件或目录", "DOWNLOAD_TOO_MANY")
    collected: list[tuple[Path, tuple[str, ...], os.stat_result]] = []
    seen_files: set[Path] = set()
    seen_sources: set[Path] = set()
    scanned = directories = total = 0

    def add(path: Path, parts: tuple[str, ...]) -> None:
        """功能说明：累计一个唯一普通媒体，超过全任务文件数或大小即拒绝。

        调用时机：来源本身是文件或扫描找到媒体时调用。

        关键步骤：
        1. 检查常规文件、版本和大小，去除重复路径；
        2. 在追加前执行文件数和原字节上限检查。

        输出/副作用：更新本次收集的内存清单与有界计数。
        """
        nonlocal total
        if path in seen_files:
            return
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode):
            raise FileError("下载来源必须是普通媒体文件", "INVALID_DOWNLOAD_FILE")
        if len(collected) >= max_files:
            raise FileError(f"一次下载最多 {max_files} 个媒体文件，请缩小选择范围", "DOWNLOAD_TOO_MANY")
        total += st.st_size
        if total > max_bytes:
            raise FileError("所选原文件超过下载大小上限", "DOWNLOAD_TOO_LARGE", 413)
        seen_files.add(path)
        collected.append((path, parts, st))

    try:
        for raw in paths:
            if "\x00" in raw or ".." in PurePosixPath(raw).parts:
                raise FileError("下载路径包含非法片段", "INVALID_PATH")
            source = authorize(raw)
            if source in seen_sources:
                continue
            seen_sources.add(source)
            if source.is_file():
                if source.name.startswith(".") or not catalog._is_media(source, media_kind):
                    raise FileError("仅可下载所选类型的媒体原文件", "UNSUPPORTED_FORMAT")
                add(source, (source.name,))
                continue
            if not source.is_dir():
                raise FileError("下载来源必须是媒体文件或目录", "INVALID_DOWNLOAD_FILE")
            start = len(collected)
            if raw.startswith("ssh://"):
                # SFTP 的展开由远端目录层执行；保留统一授权、原字节大小和 ZIP 命名边界。
                # 不能把 URI 交给本机 scandir，否则会误当成控制机上的路径。
                for _key, candidate in catalog.collect(str(source), recursive, media_kind):
                    checked = authorize(str(candidate))
                    add(checked, (source.name or "files", *candidate.relative_to(source).parts))
                collected[start:] = sorted(collected[start:], key=lambda item: (natural_key("/".join(item[1])), "/".join(item[1])))
                continue
            stack = [source]
            while stack:
                directory = stack.pop()
                directories += 1
                if directories > max_directories:
                    raise FileError("扫描目录过多，请缩小选择范围", "DOWNLOAD_SCAN_LIMIT")
                with os.scandir(directory) as scan:
                    for entry in scan:
                        scanned += 1
                        if scanned > max_entries:
                            raise FileError("扫描条目过多，请缩小选择范围", "DOWNLOAD_SCAN_LIMIT")
                        if entry.name.startswith("."):
                            continue
                        candidate = Path(entry.path)
                        if entry.is_symlink():
                            # 不跟随目录内链接；否则同区循环链接也能绕过扫描边界。
                            if catalog._is_media(candidate, media_kind) or entry.is_dir():
                                raise FileError("下载目录包含符号链接，请直接选择原文件", "PATH_ESCAPE", 403)
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        is_media = entry.is_file(follow_symlinks=False) and catalog._is_media(candidate, media_kind)
                        if not (is_dir and recursive or is_media):
                            continue
                        relative = candidate.relative_to(source)
                        # 使用来源的原始声明路径，不能先 virtual_for 链接目标再授权。
                        checked = authorize(raw.rstrip("/") + "/" + relative.as_posix())
                        if checked != candidate:
                            raise FileError("扫描期间来源路径已改变", "SOURCE_CHANGED", 409)
                        if is_dir:
                            stack.append(checked)
                            if directories + len(stack) > max_directories:
                                raise FileError("扫描目录过多，请缩小选择范围", "DOWNLOAD_SCAN_LIMIT")
                        else:
                            add(checked, (source.name or "files", *relative.parts))
            collected[start:] = sorted(collected[start:], key=lambda item: (natural_key("/".join(item[1])), "/".join(item[1])))
    except OSError as exc:
        raise FileError("部分下载来源不可读取或已改变，请重新选择", "PATH_FORBIDDEN", 403) from exc
    if not collected:
        raise FileError("所选路径中没有可下载的媒体文件", "EMPTY_DOWNLOAD")
    assets, snapshots, used_names = {}, [], set()
    try:
        for path, parts, st in collected:
            registered, rev = catalog._register_path(path), _stat_revision(st)
            if _stat_revision(path.lstat()) != rev:
                raise FileError("扫描期间源文件已改变，请重新选择", "SOURCE_CHANGED", 409)
            asset_id = hashlib.sha256(f"{registered}\0{rev}".encode()).hexdigest()[:40]
            asset = {"id": asset_id, "name": path.name, "path": registered, "revision": rev, "bytes": st.st_size}
            assets[asset_id] = asset
            snapshots.append({"id": asset_id, "revision": rev, "bytes": asset["bytes"], "archive_name": _unique_name(_archive_name(parts), used_names)})
    except OSError as exc:
        raise FileError("扫描期间源文件已改变，请重新选择", "SOURCE_CHANGED", 409) from exc
    # 复核登记后的总量，确保队列预留与真正的资产快照一致。
    total = sum(asset["bytes"] for asset in assets.values())
    if total > max_bytes:
        raise FileError("所选原文件超过下载大小上限", "DOWNLOAD_TOO_LARGE", 413)
    manifest = {"id": secrets.token_urlsafe(18), "created_at": int(time.time()), "sources": [], "combine": "concat", "recursive": recursive,
                "media_kind": media_kind, "images": list(assets), "assets": assets}
    if persist:
        with catalog.db.connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO assets VALUES(?,?,?,?)", [(a["id"], a["path"], a["revision"], a["bytes"]) for a in assets.values()])
            conn.execute("INSERT INTO comparisons VALUES(?,?,?,?)", (manifest["id"], owner_id, json.dumps(manifest, ensure_ascii=False), manifest["created_at"]))
    return manifest, snapshots, total


def _stat_revision(st: os.stat_result) -> str:
    """功能说明：以现有资产算法为已打开的文件描述符计算版本指纹。

    调用时机：原文件 ZIP 复制前后，对 fstat 结果调用。

    关键步骤：
    1. 对大小、mtime 纳秒、inode 和设备号按与 revision 相同格式取摘要。

    输出/副作用：返回版本字符串，不重新跟随路径或符号链接。
    """
    return hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}:{st.st_ino}:{st.st_dev}".encode()).hexdigest()[:32]


def export_files_zip(assets: list[tuple[dict, Path]], output: Path, *, max_bytes: int,
                     progress=None, check_write=None) -> dict[str, int]:
    """功能说明：将下载清单逐个流式写成仅包含原媒体的私有 ZIP。

    调用时机：ExportService 的单任务队列处理原文件下载时在工作线程调用。

    关键步骤：
    1. 复核文件数、安全相对名、常规文件与快照大小，禁止覆盖已有输出；
    2. O_NOFOLLOW 打开每个来源，以 fstat 核对登记版本，每次最多读 1 MiB；
    3. 每块执行取消/超时/磁盘检查并更新进度，完成后复核描述符与路径版本；
    4. 任一步失败都删除临时 ZIP，不发布部分内容。

    输出/副作用：成功生成 output 并返回文件数与 ZIP 字节；原文件不变，
    包内不附加清单、HTML 或绝对路径。
    """
    if not 1 <= len(assets) <= MAX_DOWNLOAD_FILES or max_bytes < 0:
        raise FileError("下载清单超出限制", "DOWNLOAD_TOO_MANY")
    plans, names, total = [], set(), 0
    for asset, path in assets:
        name = asset["archive_name"]
        rel = PurePosixPath(name)
        if (not name or len(name.encode("utf-8")) > 512 or rel.is_absolute() or "\\" in name
                or any(part in {"", ".", ".."} or part.startswith(".") or any(ord(c) < 32 for c in part) for part in name.split("/"))
                or name.casefold() in names or not (FileCatalog.is_image(path) or FileCatalog.is_video(path))):
            raise FileError("下载成员名或媒体类型非法", "INVALID_DOWNLOAD_FILE")
        names.add(name.casefold())
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or st.st_size != asset["bytes"] or _stat_revision(st) != asset["revision"]:
            raise FileError("源文件已改变，请重新打包", "SOURCE_CHANGED", 409)
        total += st.st_size
        if total > max_bytes:
            raise FileError("所选原文件超过下载大小上限", "DOWNLOAD_TOO_LARGE", 413)
        plans.append((asset, path, name))
    if progress:
        progress(0, total)
    created, processed = False, 0
    try:
        with Path(output).open("xb") as raw:
            created = True
            with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
                for asset, path, name in plans:
                    if check_write:
                        check_write(0)
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                    with os.fdopen(fd, "rb") as source:
                        st = os.fstat(source.fileno())
                        if not stat.S_ISREG(st.st_mode) or _stat_revision(st) != asset["revision"]:
                            raise FileError("源文件已改变，请重新打包", "SOURCE_CHANGED", 409)
                        actual = 0
                        with archive.open(name, "w") as target:
                            while chunk := source.read(DOWNLOAD_CHUNK):
                                actual += len(chunk)
                                if actual > asset["bytes"] or processed + len(chunk) > max_bytes:
                                    raise FileError("源文件大小已改变，请重新打包", "SOURCE_CHANGED", 409)
                                if check_write:
                                    check_write(len(chunk))
                                target.write(chunk)
                                processed += len(chunk)
                                if progress:
                                    progress(processed, total)
                        if (actual != asset["bytes"] or _stat_revision(os.fstat(source.fileno())) != asset["revision"]
                                or revision(path) != asset["revision"] or path.is_symlink()):
                            raise FileError("源文件已改变，请重新打包", "SOURCE_CHANGED", 409)
        return {"files": len(plans), "bytes": Path(output).stat().st_size}
    except BaseException:
        if created:
            Path(output).unlink(missing_ok=True)
        raise

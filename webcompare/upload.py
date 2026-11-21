"""【模块】上传管线 — 单图流式上传与分享包导入的服务端入口

功能说明：server 模式的两条写入口都收在这里：一是成员/管理员上传单张
PNG、JPEG 或静态 WebP（访客不允许，访客只能导入分享包），二是任何人把 webcompare 分享
zip 导入到自己的可写区域。两条路径共用同一套顺序：先按角色做写授权 →
把请求体流式落到 <data_root>/.staging 下的一次性文件（0600，边收边限长）→
嗅探魔数 → 向配额层预留额度 → 用注入的校验函数（默认是沙箱完整解码）确认
这是张真图片 → 原子搬进目标区域 → 结清记账。任何一步失败都会回收预留并
删掉临时文件，所以失败的上传既不留半份文件、也不占额度。

本模块刻意不 import fastapi：请求体以 AsyncIterator[bytes] 传入，由 app 层
用 request.stream() 适配，因此这里可以被单测直接驱动。目标目录必须是已经
存在且经 [Storage.authorize] 核验过的区域目录——上传不自动建目录，避免
“写路径自造”绕过区域边界。

## 流程
所属流程：上传与分享包（webcompare/upload.py）

1. **请求体落盘**：字节流写临时文件并限长，超限即删档 413 → 见函数 [stage_stream]
2. **格式嗅探**：读文件头判断图片魔数 → 见函数 [sniff_image]
3. **单图上传**：净化文件名 → 写授权 → 落盘 → 预留 → 沙箱校验 → 原子提交 → 记账 → 见函数 [upload_image]
4. **分享包导入**：流式收 zip → 严格解包 → 逐张校验 → 改名落盘 → 补登记 → 见函数 [import_sharepack]
"""
from __future__ import annotations

import asyncio
import errno
import os
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .config import Settings
from .files import FileError, revision
from .image_formats import IMAGE_FORMATS, image_format_from_header
from .quota import Quota
from .sandbox import validate_image
from .sharepack import SharepackTooLarge, strict_import
from .storage import Storage

STAGING_DIRNAME = ".staging"
PART_SUFFIX = ".part"
UPLOAD_MAX_BYTES = 256 * 1024**2
SHAREPACK_MAX_BYTES = 1024**3
MAX_NAME_LENGTH = 200
FILE_PERMISSION = 0o600
DIR_PERMISSION = 0o700


def _unlink(path: Path) -> None:
    """功能说明：尽力删除一个文件，不存在或删不掉都不抛错。

    调用时机：上传与导入的每条失败路径做兜底清理时调用。

    关键步骤：
    1. unlink(missing_ok=True)，任何 OSError 都咽掉。

    输出/副作用：可能删除文件系统上的一个文件；纯兜底，无返回值。
    """
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def _staging_root(settings: Settings) -> Path:
    """功能说明：给出该配置下的临时落盘根：<data_root>/.staging。

    调用时机：[upload_image] 与 [import_sharepack] 收请求体之前调用。

    关键步骤：
    1. 取 data_root 下的隐藏目录 .staging；
    2. 该目录不属于任何可访问区域（[Storage.classify] 判为 external），
       用户无法通过虚拟路径访问。

    输出/副作用：返回路径对象；不创建目录（由 [stage_stream] 创建）。
    """
    return Path(settings.data_root) / STAGING_DIRNAME


def default_validator(settings: Settings) -> Callable[[Path], dict]:
    """功能说明：给出默认的图片校验函数：按当前配置跑沙箱完整解码。

    调用时机：调用方没有注入 validate 时由 [upload_image] 与 [import_sharepack]
    现场构造；app 层通常直接注入同一实现，以便统一走 MediaService 或测试替身。

    关键步骤：
    1. 用 settings.max_pixels 作为像素上限、settings.sandbox 作为沙箱模式；
    2. 返回一个只吃 Path 的闭包，内部即 [validate_image]。

    输出/副作用：返回 callable(Path) -> dict；本身不解码任何文件。
    """

    def validate(path: Path) -> dict:
        return validate_image(path, settings.max_pixels, mode=settings.sandbox)

    return validate


async def stage_stream(
    body: AsyncIterator[bytes],
    staging_dir: Path,
    max_bytes: int,
    check_write: Callable[[int], None] | None = None,
) -> tuple[Path, int]:
    """功能说明：把请求体字节流写成一个临时文件，边收边限长。

    调用时机：[upload_image] 与 [import_sharepack] 收到请求体后立即调用；
    app 层用 request.stream() 适配成 AsyncIterator[bytes] 传进来。

    关键步骤：
    1. 校验上限非负；确保 staging 目录存在并收紧到 0700；
    2. 以随机 token 命名 <token>.part，用 O_EXCL + 0600 创建，绝不覆盖既有文件；
    3. 逐个 chunk 在写入前调用可选 check_write，再累计限长并写盘；超过 max_bytes
       即抛 FileError(413, UPLOAD_TOO_LARGE)；
    4. 任何异常（含请求中断）都关闭并删除这个临时文件后再抛出。

    输出/副作用：成功返回 (临时文件路径, 字节数)；失败不留文件。
    临时文件由调用方负责在成功或失败后清理。
    """
    if max_bytes < 0:
        raise ValueError("max_bytes 不能为负")
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True, mode=DIR_PERMISSION)
    if os.name == "posix":
        try:
            staging.chmod(DIR_PERMISSION)
        except OSError:
            pass
    path = staging / f"{secrets.token_urlsafe(16)}{PART_SUFFIX}"
    handle = None
    total = 0
    try:
        handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSION), "wb")
        async for chunk in body:
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise FileError(f"上传内容超过上限：最多 {max_bytes} 字节", "UPLOAD_TOO_LARGE", 413)
            if check_write is not None:
                check_write(len(chunk))
            handle.write(chunk)
        handle.flush()
        return path, total
    except BaseException:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
            handle = None
        _unlink(path)
        raise
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def sniff_image(path: Path) -> str | None:
    """功能说明：读取前 12 字节识别 PNG、JPEG、WebP 容器。

    调用时机：[upload_image] 收完请求体、进沙箱校验之前先做一次廉价嗅探，
    挡住明显不是图片的上传（省掉一次解码子进程）。

    关键步骤：
    1. 读前 12 字节，读取失败抛 FileError(422)；
    2. 由共用魔数检查识别格式，完整解码由沙箱负责。

    输出/副作用：返回格式串或 None；只读，不改文件。
    """
    try:
        with Path(path).open("rb") as handle:
            return image_format_from_header(handle.read(12))
    except OSError as exc:
        raise FileError(f"无法读取上传内容：{exc}", "DECODE_ERROR", 422) from exc


async def upload_image(
    body: AsyncIterator[bytes],
    *,
    settings: Settings,
    storage: Storage,
    quota: Quota,
    user: dict,
    target_dir: str,
    filename: str,
    validate: Callable[[Path], dict] | None = None,
    media_kind: str = "image",
) -> dict:
    """功能说明：完整执行一次媒体上传：授权 → 落盘 → 预留 → 校验 → 提交 → 记账。

    调用时机：POST /api/uploads?dir=<虚拟目录>&name=<文件名>（raw body）；
    validate 由 app 层注入，缺省用 [default_validator]（即沙箱校验）。

    关键步骤：
    1. 访客直接拒绝（GUEST_SHAREPACK_ONLY 403）；文件名净化：取 basename、去反斜杠、
       拒绝空名/点开头/超 200 字节/NUL，后缀按 media_kind 限定为图片或视频；
    2. storage.authorize(user, "<dir>/<文件名>", "write") 对目标文件路径做写授权
       （挂载根只禁止对根自身的删/改名，往 rw 挂载根内放新文件是合法的内部管理），
       再解析目录并要求其已经存在（不自动建目录）；
    3. 上限取“256 MiB”与“角色配额余量”的较小值，[stage_stream] 流式收进
       <data_root>/.staging；
    4. 图片 [sniff_image] 或视频容器头必须与扩展名一致，不符即删档 422；临时文件补上目标扩展名，
       quota.reserve(上传者, 实际字节) 预留额度；
    5. 图片在线程池跑注入的 validate（默认起子进程完整校验）；视频不在服务端解码；失败即
       回收预留并删档；
    6. 目标虚拟路径已存在（能 resolve 或已有 stored_files 行）即 409 不覆盖；
    7. [ _place_file ] 把临时文件搬进目标目录，再 quota.commit 写 stored_files
       （owner=上传者、area 由 [Storage.area_of] 判定）；提交失败则删掉刚落盘的文件。

    输出/副作用：成功返回 {path, name, bytes, width, height}，视频追加 revision；任何失败路径都会
    回收预留与临时文件，不留半份状态。
    """
    role = str((user or {}).get("role") or "")
    if role == "guest":
        raise FileError("访客只能导入分享包，不能直接上传图片", "GUEST_SHAREPACK_ONLY", 403)
    owner_id = str((user or {}).get("id") or "")
    checked = validate or default_validator(settings)
    name = _sanitize_name(filename, media_kind)
    # 授权对象是目标文件路径而非目录本身：挂载根禁止的是对根自身的删除/改名，
    # 往 rw 挂载根里放新文件属于“管理挂载内部内容”，按文件路径授权即可放行。
    storage.area_of(target_dir.rstrip("/"))  # 目录写法本身必须合法（区域与作用域齐全）
    virtual = storage.authorize(user, f"{target_dir.rstrip('/')}/{name}", "write")
    directory = virtual.rsplit("/", 1)[0]
    if directory == "member":
        raise FileError("成员区根目录只允许放文件夹", "FOLDERS_ONLY", 403)
    real_dir = storage.resolve(directory)
    if not real_dir.is_dir():
        raise FileError("上传目标不是目录", "NOT_FOUND", 404)
    max_bytes = _budget(settings, quota, user, UPLOAD_MAX_BYTES)
    staged, size = await stage_stream(body, _staging_root(settings), max_bytes, quota.check_disk_floor)
    reservation: str | None = None
    try:
        if media_kind == "video":
            # 仅确认容器头；解复用和解码交给浏览器，云端不启动视频解码任务。
            with staged.open("rb") as stream:
                head = stream.read(32)
            suffix = PurePosixPath(name).suffix.lower()
            valid = (len(head) >= 12 and head[4:8] == b"ftyp") if suffix in {".mp4", ".mov", ".m4v"} else head.startswith(b"\x1a\x45\xdf\xa3")
            if not valid:
                raise FileError("视频容器头与扩展名不符", "UNSUPPORTED_FORMAT", 422)
        else:
            sniffed = sniff_image(staged)
            if sniffed is None:
                raise FileError("上传内容不是 PNG、JPEG 或 WebP 图片", "UNSUPPORTED_FORMAT", 422)
            if sniffed != IMAGE_FORMATS[PurePosixPath(name).suffix.lower()]:
                raise FileError("文件内容与扩展名不符（疑似伪装格式）", "UNSUPPORTED_FORMAT", 422)
        # 沙箱要求“后缀与魔数一致”，而 stage_stream 落的是 <token>.part，
        # 因此在调用校验函数之前先补上声明的扩展名（同目录改名，代价极小）。
        staged = _align_staging_suffix(staged, name)
        reservation = quota.reserve(owner_id, size)
        placed: Path | None = None
        try:
            meta = {} if media_kind == "video" else await asyncio.to_thread(checked, staged)
            with storage.mutation_lock:
                virtual = storage.authorize(user, virtual, "write")
                directory = virtual.rsplit("/", 1)[0]
                real_dir = storage.resolve(directory)
                if not real_dir.is_dir():
                    raise FileError("上传目标不是目录", "NOT_FOUND", 404)
                if role == "guest":
                    _check_guest_workspace(storage, owner_id)
                if _virtual_exists(storage, quota, virtual):
                    raise FileError("目标位置已有同名文件，不覆盖", "NAME_EXISTS", 409)
                dest = real_dir / name
                quota.check_disk_floor(size)
                _place_file(staged, dest, mode=settings.content_file_mode)
                placed = dest
                area, _rel = storage.area_of(virtual)
                quota.commit(reservation, virtual, area, owner_id, size)
                reservation = None
            meta = dict(meta or {})
            return {
                "path": virtual,
                "name": name,
                "bytes": size,
                "width": int(meta.get("width") or 0),
                "height": int(meta.get("height") or 0),
                **({"revision": revision(dest)} if media_kind == "video" else {}),
            }
        except BaseException:
            if placed is not None:
                with storage.mutation_lock:
                    _unlink(placed)
            raise
        finally:
            if reservation is not None:
                quota.abort(reservation)
    finally:
        _unlink(staged)


async def import_sharepack(
    body: AsyncIterator[bytes],
    *,
    settings: Settings,
    storage: Storage,
    quota: Quota,
    user: dict,
    target_dir: str,
    validate: Callable[[Path], dict] | None = None,
) -> dict:
    """功能说明：把上传的分享 zip 严格解包、逐张校验后落进用户的可写目录。

    调用时机：POST /api/import-sharepack?dir=<虚拟目录>（raw body zip）；
    访客只能写自己的 guest 工作区，由 [Storage.authorize] 的角色矩阵自然保证，
    这里不写角色特例。validate 由 app 层注入，缺省用 [default_validator]。

    关键步骤：
    1. 以目标文件路径探针做写授权（挂载根内部导入因此按 rw 放行）+ 解析目录
       （必须已存在）；请求体与解包上限取“1 GiB”与“角色配额余量”的较小值，
       [stage_stream] 流式收 zip 到 <data_root>/.staging；
    2. [strict_import] 解到 .staging/<token>.unpack（条目白名单、禁链接、限总量），
       违规包统一转成 FileError(422, INVALID_SHAREPACK)；
    3. 逐张跑注入的 validate（默认沙箱完整解码），任一张失败即整批拒绝并清理；
    4. 按解出总字节 quota.reserve 一次，再逐条落盘：撞名自动改名“名-N.扩展”
       （N 从 2 递增，仍不覆盖任何既有文件）；
    5. 每落一条就用 quota.record_reserved 在预留覆盖内登记（owner=导入者），全部成功后
       回收预留；中途失败则删掉已落盘文件并同步删登记行，不让磁盘与账目脱节。

    输出/副作用：成功返回 {imported: [{path, name, bytes}], skipped_html, state}；
    失败清理 staging 与已落盘文件、回收预留。
    """
    owner_id = str((user or {}).get("id") or "")
    checked = validate or default_validator(settings)
    # 与 upload_image 同理：以目标文件路径做写授权，rw 挂载根目录内部导入因此放行。
    storage.area_of(target_dir.rstrip("/"))  # 目录写法本身必须合法（区域与作用域齐全）
    probe = storage.authorize(user, f"{target_dir.rstrip('/')}/__probe__.png", "write")
    directory = probe.rsplit("/", 1)[0]
    if directory == "member":
        raise FileError("成员区根目录只允许放文件夹", "FOLDERS_ONLY", 403)
    real_dir = storage.resolve(directory)
    if not real_dir.is_dir():
        raise FileError("导入目标不是目录", "NOT_FOUND", 404)
    hard_cap = _budget(settings, quota, user, SHAREPACK_MAX_BYTES)
    staged_zip, _zip_bytes = await stage_stream(body, _staging_root(settings), SHAREPACK_MAX_BYTES, quota.check_disk_floor)
    workdir = staged_zip.parent / f"{staged_zip.stem}.unpack"
    reservation: str | None = None
    placed: list[Path] = []
    try:
        try:
            manifest = await asyncio.to_thread(strict_import, staged_zip, workdir, hard_cap, quota.check_disk_floor)
        except SharepackTooLarge as exc:
            code = "QUOTA_EXCEEDED" if hard_cap < SHAREPACK_MAX_BYTES else "UPLOAD_TOO_LARGE"
            raise FileError(f"分享包解压后的图片超过允许上限 {hard_cap} 字节", code, 413) from exc
        except ValueError as exc:
            raise FileError(f"分享包不合法：{exc}", "INVALID_SHAREPACK", 422) from exc
        items = manifest["images"]
        if not items:
            raise FileError("分享包内没有可导入的图片", "EMPTY_SHAREPACK", 422)
        for item in items:
            await asyncio.to_thread(checked, Path(str(item["staged"])))
        total = sum(int(item["bytes"]) for item in items)
        used: set[str] = set()
        plans = [(item, _unique_name(real_dir, storage, quota, directory, str(item["name"]), used)) for item in items]
        reservation = quota.reserve(owner_id, total)
        imported: list[dict[str, Any]] = []
        try:
            with storage.mutation_lock:
                for item, name in plans:
                    virtual = storage.authorize(user, f"{directory}/{name}", "write")
                    current_dir = storage.resolve(virtual.rsplit("/", 1)[0])
                    if not current_dir.is_dir():
                        raise FileError("导入目标不是目录", "NOT_FOUND", 404)
                    if str((user or {}).get("role")) == "guest":
                        _check_guest_workspace(storage, owner_id)
                    if _virtual_exists(storage, quota, virtual):
                        raise FileError("目标位置已有同名文件，不覆盖", "NAME_EXISTS", 409)
                    dest = current_dir / name
                    size = int(item["bytes"])
                    quota.check_disk_floor(size)
                    _place_file(Path(str(item["staged"])), dest, mode=settings.content_file_mode)
                    placed.append(dest)
                    area, _rel = storage.area_of(virtual)
                    # reserve 总额在每条登记后同步递减，避免双重计账并限制单笔提交额。
                    quota.record_reserved(reservation, virtual, area, owner_id, size)
                    imported.append({"path": virtual, "name": name, "bytes": size})
                quota.abort(reservation)
                reservation = None
        except BaseException:
            with storage.mutation_lock:
                for entry, dest in zip(imported, placed):
                    quota.remove(entry["path"])
                    _unlink(dest)
                for dest in placed[len(imported):]:
                    _unlink(dest)
            raise
        finally:
            if reservation is not None:
                quota.abort(reservation)
        return {"imported": imported, "skipped_html": int(manifest["skipped_html"]), "state": manifest["state"]}
    finally:
        _unlink(staged_zip)
        shutil.rmtree(workdir, ignore_errors=True)


def _sanitize_name(filename: str, media_kind: str = "image") -> str:
    """功能说明：把客户端给的文件名净化成可安全落盘的单个文件名。

    调用时机：[upload_image] 一进来就调用，早于任何授权与落盘动作。

    关键步骤：
    1. 反斜杠统一成斜杠后取 basename（挡目录拼接与 Windows 分隔符）；
    2. 拒绝空名、"."、".."、以点开头的隐藏名、含 NUL、超过 200 字节；
    3. 后缀必须属于当前媒体类型，否则 422。

    输出/副作用：返回净化后的文件名；非法抛 FileError(422)。
    """
    raw = str(filename or "").strip().replace("\\", "/")
    name = PurePosixPath(raw).name
    if not name or name in {".", ".."}:
        raise FileError("文件名不能为空", "INVALID_FILENAME", 422)
    if len(name) > MAX_NAME_LENGTH:
        raise FileError(f"文件名过长：最多 {MAX_NAME_LENGTH} 个字符", "INVALID_FILENAME", 422)
    if name.startswith("."):
        raise FileError("文件名不能以点开头", "INVALID_FILENAME", 422)
    if "\x00" in name:
        raise FileError("文件名含非法字符", "INVALID_FILENAME", 422)
    if any(c in name for c in '<>:"|?*') or name.endswith((".", " ")) or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        raise FileError("文件名不符合跨平台命名规则", "INVALID_FILENAME", 422)
    suffixes = {".mp4", ".mov", ".m4v", ".webm", ".mkv"} if media_kind == "video" else IMAGE_FORMATS
    if PurePosixPath(name).suffix.lower() not in suffixes:
        raise FileError("文件扩展名不属于当前媒体类型", "UNSUPPORTED_FORMAT", 422)
    return name


def _check_guest_workspace(storage: Storage, owner_id: str) -> None:
    """提交访客文件前确认 janitor 尚未过期或清理其工作区，不刷新活动时间。"""
    row = storage.db.one(
        "SELECT exited_at,last_active_at,purged FROM guest_workspaces WHERE user_id=?",
        (owner_id,),
    )
    now = int(time.time())
    if (
        row is None
        or row["purged"]
        or (row["exited_at"] is not None and now > int(row["exited_at"]) + storage.settings.guest_exit_ttl)
        or now - int(row["last_active_at"]) > storage.settings.guest_idle_ttl
    ):
        raise FileError("访客工作区已失效，不能提交上传内容", "LOGIN_REQUIRED", 401)


def _align_staging_suffix(staged: Path, name: str) -> Path:
    """功能说明：把 <token>.part 改名成带目标扩展名的临时文件名。

    调用时机：[upload_image] 在魔数嗅探通过之后、调用校验函数之前调用。

    关键步骤：
    1. 取净化后文件名的媒体后缀；
    2. 同目录改名 os.replace(part → <token><后缀>)：沙箱的“后缀必须与魔数一致”
       检查因此成立，同时保持同分区（后续仍然能原子搬到目标位置）。

    输出/副作用：返回改名后的路径；改名失败抛 OSError（由调用方回收临时文件）。
    """
    suffix = PurePosixPath(name).suffix.lower()
    if staged.suffix.lower() == suffix:
        return staged
    target = staged.with_name(f"{staged.stem}{suffix}")
    os.replace(staged, target)
    return target


def _budget(settings: Settings, quota: Quota, user: dict, hard_cap: int) -> int:
    """功能说明：算本次写入的字节上限：硬上限与角色配额余量取小。

    调用时机：[upload_image] 与 [import_sharepack] 在收请求体之前调用。

    关键步骤：
    1. 按角色取个人配额（admin 无个人上限、guest 用访客配额、其余用成员配额）；
    2. 用 [Quota.usage] 取“已登记 + 未结清预留”的已用量，算出余量；
    3. 返回 max(0, min(hard_cap, 余量))：余量为 0 时任何字节都会在收流阶段被 413 拦下。

    输出/副作用：返回字节数；只读，不做预留（真正的额度仲裁仍在 [Quota.reserve]）。
    """
    role = str((user or {}).get("role") or "")
    if role == "admin":
        limit: int | None = None
    elif role == "guest":
        limit = settings.guest_quota_bytes
    else:
        limit = settings.member_quota_bytes
    if limit is None:
        return hard_cap
    owner_id = str((user or {}).get("id") or "")
    return max(0, min(hard_cap, limit - quota.usage(owner_id)))


def _virtual_exists(storage: Storage, quota: Quota, virtual: str) -> bool:
    """功能说明：判断虚拟路径是否已被占用：磁盘上能解析出来，或已有存储登记行。

    调用时机：[upload_image] 落盘之前、[_unique_name] 逐个候选名试名时调用。

    关键步骤：
    1. 先试 [Storage.resolve]（存在且不越界即视为占用，越界/不存在都算空闲）；
    2. 再查 stored_files 是否有该路径的行（文件被删但账未销的情况）。

    输出/副作用：返回布尔；只读。
    """
    try:
        storage.resolve(virtual)
        return True
    except FileError:
        pass
    return quota.db.one("SELECT 1 FROM stored_files WHERE path=?", (virtual,)) is not None


def _unique_name(real_dir: Path, storage: Storage, quota: Quota, directory: str, name: str, used: set[str]) -> str:
    """功能说明：为导入的图片挑一个不冲突的文件名：撞名就改成“名-N.扩展”。

    调用时机：[import_sharepack] 落盘之前为每条清单项调用一次。

    关键步骤：
    1. 从原名开始，依次尝试“名-2”“名-3”…；
    2. 候选名在本次批次内已用过、磁盘上已存在、或已有存储登记行都视为冲突；
    3. 试到 9999 仍冲突即报 409（异常大的目录不再无限试名）。

    输出/副作用：返回可用的文件名，并把它登记进本批次的 used 集合；无可用名抛 409。
    """
    stem = Path(name).stem
    suffix = Path(name).suffix
    candidate = name
    index = 2
    while candidate.casefold() in used or (real_dir / candidate).exists() or _virtual_exists(storage, quota, f"{directory}/{candidate}"):
        if index > 9999:
            raise FileError("目标目录同名文件过多，无法自动改名", "NAME_EXISTS", 409)
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    used.add(candidate.casefold())
    return candidate


def _place_file(staged: Path, dest: Path, *, mode: int = FILE_PERMISSION) -> None:
    """功能说明：把 staging 临时文件搬进目标位置，全程保证不覆盖既有文件。

    调用时机：[upload_image] 与 [import_sharepack] 在目标名确定后调用。

    关键步骤：
    1. 首选 os.link 硬链接落位：同一文件系统上原子完成，目标已存在即 EEXIST，
       从机制上杜绝“并发同名互相覆盖、失败清理误删对方文件”的竞态；
    2. 目标在 rw 挂载点上（跨文件系统）时 os.link 报 EXDEV：退化为以
       O_CREAT|O_EXCL 直接创建目标文件并流式复制——同样不存在即失败、绝不覆盖，
       复制中途失败删掉半份目标再抛错；
    3. 落位成功后删除 staging 源文件（等价于移动）。

    输出/副作用：目标位置出现新文件、staging 文件消失；目标已存在抛
    FileError(409, NAME_EXISTS)，其它失败抛 OSError。
    """
    if os.name == "posix":
        # 激活继承的本机管理 ACL；临时目录仍保持 0700。
        staged.chmod(mode)
    try:
        os.link(staged, dest)
    except FileExistsError as exc:
        raise FileError("目标位置同名文件已存在，不覆盖", "NAME_EXISTS", 409) from exc
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        try:
            with os.fdopen(os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_PERMISSION), "wb") as target:
                with staged.open("rb") as source:
                    shutil.copyfileobj(source, target)
                if os.name == "posix":
                    os.fchmod(target.fileno(), mode)
        except FileExistsError as exists:
            raise FileError("目标位置同名文件已存在，不覆盖", "NAME_EXISTS", 409) from exists
        except BaseException:
            _unlink(dest)
            raise
    _unlink(staged)

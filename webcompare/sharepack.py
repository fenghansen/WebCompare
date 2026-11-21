"""【模块】分享包 — zip 导出与安全解包

功能说明：把当前页的 PNG、JPEG 或静态 WebP 原图和 manifest 打包成 zip 分享包，可在另一个
WebCompare 实例解包浏览。导出包不包含独立 HTML。
manifest.json 协议刻意宽松：读取方忽略未知字段、缺失字段用默认值，
跨版本兼容靠“字段只增不改”。CLI 的 import_zip 走宽松解包（解成人可浏览的
目录），server 模式的上传接口则走严格解包 [strict_import]：它把外来的 zip
当成完全不可信输入，逐条审视条目类型、路径与后缀，只把 manifest 引用的
图片扁平化落到一次性 staging 目录，HTML 一律丢弃不解出。

## 流程
所属流程：分享包（webcompare/sharepack.py）

1. **导出打包**：原图 + manifest 流式写成 zip → 见函数 [export_zip]
2. **安全解包**：拒绝越界条目后解到指定目录 → 见函数 [import_zip]
3. **严格解包**：条目白名单 + 结构校验 + 限长流式解出到 staging → 见函数 [strict_import]
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .files import revision
from .image_formats import IMAGE_FORMATS, image_format_from_header, is_animated_webp

MAX_IMAGES = 64
MAX_BYTES = 1024 * 1024 * 1024

MANIFEST_NAME = "manifest.json"
MAX_ENTRIES = MAX_IMAGES + 2  # manifest + view.html + 最多 64 张图
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_EXTRACT_CHUNK = 1024 * 1024
_ALLOWED_SUFFIXES = {*IMAGE_FORMATS, ".json", ".html"}
_NESTED_SUFFIXES = (".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar")
_HTML_SUFFIXES = (".html",)


class SharepackTooLarge(ValueError):
    """解包后的图片总量超过调用方允许的字节数。"""


def export_zip(
    assets: list[tuple[dict[str, Any], Path]],
    state: dict[str, Any],
    output: Path,
    *,
    max_bytes: int = MAX_BYTES,
    progress: Callable[[int, int], None] | None = None,
    check_write: Callable[[int], None] | None = None,
) -> dict[str, int]:
    """功能说明：把若干资产的原图字节打成 zip 分享包。

    调用时机：POST /api/export-zip 经线程池调用；输出为临时文件，由响应
    发送完毕后删除。

    关键步骤：
    1. 预检图片数、普通源文件、净化文件名与 stat 总量，超限在创建 output 前拒绝；
    2. 逐张以 1 MiB 块写入原图，复核实际字节及本次读取前后的文件状态；
    3. 写不含绝对路径或外部 URL 的 manifest.json；
    4. 可选回报原图进度，并在每块原图/manifest 写入前调用磁盘检查。

    输出/副作用：生成 output 指定的 zip 文件。
    """
    output = Path(output)
    if len(assets) > MAX_IMAGES:
        raise ValueError(f"分享包最多 {MAX_IMAGES} 张图")
    if max_bytes < 0:
        raise ValueError("max_bytes 不能为负")
    if output.exists():
        raise FileExistsError(f"输出文件已存在：{output}")

    plans = []
    total = 0
    for index, (asset, raw_path) in enumerate(assets):
        path = Path(raw_path)
        try:
            source_stat = path.lstat()
            size = source_stat.st_size
        except OSError as exc:
            raise ValueError(f"分享图片源文件无法读取：{path}") from exc
        if not stat.S_ISREG(source_stat.st_mode):
            raise ValueError(f"分享图片源路径不是普通文件：{path}")
        name = str(asset.get("name") or path.name)
        suffix = PurePosixPath(name).suffix.lower()
        if (
            name != PurePosixPath(name).name
            or "\\" in name
            or "\x00" in name
            or not name
            or name.startswith(".")
            or len(name) > 200
            or suffix not in IMAGE_FORMATS
            or IMAGE_FORMATS.get(path.suffix.lower()) != IMAGE_FORMATS[suffix]
        ):
            raise ValueError(f"分享图片文件名或格式不受支持：{name!r}")
        total += size
        if total > max_bytes:
            raise ValueError("文件尺寸过大不可zip打包分享")
        plans.append((path, size, revision(path, st=source_stat), f"images/{index:02d}_{name}", name))

    _reject_absolute_strings(state)
    images_meta = [{"file": arc, "name": name} for _path, _size, _revision, arc, name in plans]
    manifest = {"format": 1, "app": "webcompare", "created_at": int(time.time()), "state": state, "images": images_meta}
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=1).encode("utf-8")
    if progress is not None:
        progress(0, total)

    created = False
    processed = 0
    try:
        with output.open("xb") as raw:
            created = True
            with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as zf:
                for path, expected_size, expected_revision, arc, _name in plans:
                    if path.is_symlink():
                        raise ValueError(f"分享图片源路径不能是符号链接：{path}")
                    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as source:
                        opened_stat = os.fstat(source.fileno())
                        if not stat.S_ISREG(opened_stat.st_mode):
                            raise ValueError(f"分享图片源路径不是普通文件：{path}")
                        if revision(path, st=opened_stat) != expected_revision:
                            raise ValueError(f"分享图片源文件在打包期间发生变化：{path}")
                        if path.suffix.lower() == ".webp":
                            header = source.read(21)
                            if image_format_from_header(header) != "webp":
                                raise ValueError(f"分享图片的文件内容与 WebP 扩展名不符：{path.name}")
                            if is_animated_webp(header):
                                raise ValueError(f"分享包不支持动画 WebP：{path.name}")
                            source.seek(0)
                        actual_size = 0
                        with zf.open(arc, "w") as target:
                            while chunk := source.read(_EXTRACT_CHUNK):
                                actual_size += len(chunk)
                                if processed + len(chunk) > max_bytes:
                                    raise ValueError("文件尺寸过大不可zip打包分享")
                                if actual_size > expected_size:
                                    raise ValueError(f"分享图片源文件大小已变化：{path}")
                                if check_write is not None:
                                    check_write(len(chunk))
                                target.write(chunk)
                                processed += len(chunk)
                                if progress is not None:
                                    progress(processed, total)
                        if actual_size != expected_size:
                            raise ValueError(f"分享图片源文件大小已变化：{path}")
                        if revision(path, st=os.fstat(source.fileno())) != expected_revision:
                            raise ValueError(f"分享图片源文件在打包期间发生变化：{path}")
                if check_write is not None:
                    check_write(len(manifest_bytes))
                zf.writestr(MANIFEST_NAME, manifest_bytes)
        return {"images": len(images_meta), "bytes": output.stat().st_size}
    except BaseException:
        if created:
            output.unlink(missing_ok=True)
        raise


def import_zip(archive: Path, output: Path) -> dict[str, Any]:
    """功能说明：把分享 zip 安全解包到指定目录，供目标实例按普通目录浏览。

    调用时机：CLI `webcompare import-zip ARCHIVE DIR`。

    关键步骤：
    1. 校验条目无绝对路径与 ..；
    2. 输出目录必须不存在（防覆盖）；
    3. manifest 缺失/未知字段容忍，只读 images 清单做摘要。

    输出/副作用：新建输出目录并解出全部内容。
    """
    if output.exists():
        raise FileExistsError(f"输出目录已存在：{output}")
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            rel = PurePosixPath(info.filename)
            if rel.is_absolute() or ".." in rel.parts:
                raise ValueError(f"分享包含越界条目：{info.filename}")
        output.mkdir(parents=True)
        zf.extractall(output)
        try:
            manifest = json.loads(zf.read("manifest.json"))
            count = len(manifest.get("images", []))
            fmt = manifest.get("format", "?")
        except KeyError:
            count, fmt = 0, "无 manifest（按普通目录解包）"
    return {"images": count, "format": fmt, "output": str(output)}


def strict_import(
    archive: Path,
    staging_dir: Path,
    max_bytes: int = MAX_BYTES,
    check_write: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """功能说明：把不可信的分享 zip 严格解包到一次性 staging 目录，只留经过审查的图片。

    调用时机：server 模式 POST /api/import-sharepack 收完请求体后，由
    webcompare.upload.import_sharepack 调用；CLI/单机浏览仍走宽松的 [import_zip]。

    关键步骤：
    1. 目标目录必须为空或不存在（本函数创建之，权限 0700），解包只落在它里面；
    2. 逐条审查 entry：数量上限、绝对路径/“..”段/反斜杠/空名、符号链接与硬链接
       （只接受普通文件或模式为 0）、嵌套压缩包后缀、图片及根 manifest/view 白名单、
       忽略大小写的重复路径——任何违规都抛 ValueError 并带上中文原因；
    3. 读出 manifest.json（体积上限内），要求 format==1、images 是列表且每项
       指向包内真实存在的 images/ 下相对路径；递归扫描所有字符串字段，
       出现 “://” 或以 “/” 开头即拒绝（禁外部 URL 与服务器绝对路径）；
    4. 只按 manifest 的清单逐条流式解出图片，扁平化落到 staging/images/<basename>，
       运行总量超过 max_bytes 立即中止；根 view.html 只计数不解出。

    输出/副作用：返回 {"images": [{"name", "staged", "bytes"}], "state", "skipped_html"}；
    任何失败都会删除整个 staging 目录再抛 ValueError，绝不留下半份解包结果。
    """
    staging = Path(staging_dir)
    if staging.exists():
        if not staging.is_dir():
            raise ValueError(f"解包目标已存在且不是目录：{staging}")
        if any(staging.iterdir()):
            raise ValueError(f"解包目标必须为空目录：{staging}")
    try:
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            staging.chmod(0o700)
        return _strict_extract(Path(archive), staging, max_bytes, check_write)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _strict_extract(
    archive: Path,
    staging: Path,
    max_bytes: int = MAX_BYTES,
    check_write: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """功能说明：严格解包的执行体：条目审查 → manifest 校验 → 限长流式解出。

    调用时机：只由 [strict_import] 在 staging 目录就绪后调用。

    关键步骤：
    1. 打开 zip，[ _check_entries ] 逐条审查并统计被跳过的 HTML 条目；
    2. [ _read_manifest ] 读取并校验清单，得到要落盘的图片条目；
    3. [ _extract_images ] 逐条流式解出图片并累计字节数。

    输出/副作用：返回结构化解包结果；越界/畸形条目抛 ValueError。
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            _check_entries(infos)
            skipped_html = sum(1 for info in infos if PurePosixPath(info.filename).suffix.lower() in _HTML_SUFFIXES)
            entries, manifest = _read_manifest(zf, infos)
            images = _extract_images(zf, entries, staging, max_bytes, check_write)
    except ValueError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
        raise ValueError(f"分享包无法读取：{exc}") from exc
    return {"images": images, "state": manifest.get("state") or None, "skipped_html": skipped_html}


def _check_entries(infos: list[zipfile.ZipInfo]) -> None:
    """功能说明：逐条审查 zip 条目：数量、路径写法、文件类型、后缀与重复名。

    调用时机：[_strict_extract] 打开 zip 后、读 manifest 之前调用。

    关键步骤：
    1. 条目总数不得超过 MAX_ENTRIES（64 张图 + manifest + view.html）；
    2. 每条走 [_entry_problem]：空名、空字节、反斜杠、绝对路径、空段/点段、
       非普通文件（符号链接、硬链接、目录等 unix 模式位）、嵌套压缩包后缀、
       白名单外后缀都是拒绝理由；
    3. 路径按 casefold 去重，忽略大小写的重名（如在 Windows 上会互相覆盖）即拒绝。

    输出/副作用：全部通过则返回；否则抛 ValueError（中文原因），不写盘。
    """
    if len(infos) > MAX_ENTRIES:
        raise ValueError(f"分享包条目过多：{len(infos)} 个，最多 {MAX_ENTRIES} 个")
    seen: set[str] = set()
    for info in infos:
        problem = _entry_problem(info)
        if problem is not None:
            raise ValueError(f"分享包含非法条目 {info.filename!r}：{problem}")
        folded = info.filename.casefold()
        if folded in seen:
            raise ValueError(f"分享包含重复条目（忽略大小写）：{info.filename}")
        seen.add(folded)


def _entry_problem(info: zipfile.ZipInfo) -> str | None:
    """功能说明：判断单个 zip 条目是否违规，违规返回中文原因，合规返回 None。

    调用时机：[_check_entries] 对每条条目调用。

    关键步骤：
    1. 名字检查：非空、不含空字节与反斜杠、不是绝对路径、按 / 切段后没有
       空段/“.”/“..”；
    2. 类型检查：external_attr 高 16 位的 unix 模式类型位只允许 0（非 unix
       工具写的包）或 0o100000（普通文件），符号链接 0o120000、目录 0o040000
       等一律拒绝——同时挡住符号链接逃逸与硬链接；
    3. 后缀检查：先拒绝嵌套压缩包，再要求图片后缀，或根 manifest.json/view.html。

    输出/副作用：返回原因字符串或 None；纯计算，不访问文件系统。
    """
    name = info.filename
    if not name:
        return "条目名为空"
    if "\x00" in name:
        return "条目名含空字节"
    if "\\" in name:
        return "条目名含反斜杠"
    if name.startswith("/"):
        return "条目是绝对路径"
    for segment in name.split("/"):
        if segment in {"", ".", ".."}:
            return f"条目路径含非法段 {segment!r}"
    mode = (info.external_attr >> 16) & 0xFFFF
    kind = stat.S_IFMT(mode)
    if kind not in (0, stat.S_IFREG):
        return f"条目不是普通文件（unix 模式 {oct(mode)}）"
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in _NESTED_SUFFIXES:
        return f"条目是嵌套压缩包（{suffix}）"
    if suffix not in _ALLOWED_SUFFIXES:
        return f"条目后缀 {suffix or '（无）'} 不在白名单内"
    if suffix == ".json" and name != MANIFEST_NAME:
        return "只允许根目录 manifest.json"
    if suffix == ".html" and name != "view.html":
        return "只允许根目录 view.html"
    return None


def _read_manifest(zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> tuple[list[tuple[str, zipfile.ZipInfo]], dict[str, Any]]:
    """功能说明：读取并校验 manifest.json，产出要落盘的图片条目清单。

    调用时机：[_strict_extract] 在条目审查通过后调用。

    关键步骤：
    1. 必须存在 manifest.json 且体积不超过 MAX_MANIFEST_BYTES，解析成 JSON 对象，
       format 严格等于 1；
    2. 递归扫描清单里所有字符串（键与值），含 “://” 或以 “/” 开头即拒绝；
    3. images 必须是列表且不超过 MAX_IMAGES 项，每项是带 file 字段的对象，
       file 必须是 images/ 下的相对路径（无 “..”、不以 “/” 开头、后缀是
       png/jpg/jpeg/webp）且该条目真实存在于包内；
    4. 扁平化落盘会抹掉包内子目录，因此解包后的文件名（basename）也必须
       互不冲突（忽略大小写）。

    输出/副作用：返回 ([(条目名, ZipInfo)], manifest)；任何不合规抛 ValueError。
    """
    by_name = {info.filename: info for info in infos}
    info = by_name.get(MANIFEST_NAME)
    if info is None:
        raise ValueError("分享包缺少 manifest.json")
    if info.file_size > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest.json 过大：{info.file_size} 字节")
    with zf.open(info) as handle:
        raw = handle.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest.json 过大：超过 {MAX_MANIFEST_BYTES} 字节")
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"manifest.json 不是合法 JSON：{exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json 必须是 JSON 对象")
    if manifest.get("format") != 1:
        raise ValueError(f"分享包格式版本不受支持：{manifest.get('format')!r}")
    _reject_absolute_strings(manifest)
    items = manifest.get("images")
    if not isinstance(items, list):
        raise ValueError("manifest.json 缺少 images 列表")
    if len(items) > MAX_IMAGES:
        raise ValueError(f"分享包图片过多：{len(items)} 张，最多 {MAX_IMAGES} 张")
    entries: list[tuple[str, zipfile.ZipInfo]] = []
    basenames: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"manifest images 第 {index + 1} 项不是对象")
        rel = item.get("file")
        if not isinstance(rel, str) or not rel:
            raise ValueError(f"manifest images 第 {index + 1} 项缺少 file 字段")
        tail = rel[len("images/") :] if rel.startswith("images/") else ""
        if not tail or rel.startswith("/") or "\\" in tail or any(segment in {"", ".", ".."} for segment in tail.split("/")):
            raise ValueError(f"manifest 引用的路径必须是 images/ 下的相对路径：{rel}")
        if PurePosixPath(rel).suffix.lower() not in IMAGE_FORMATS:
            raise ValueError(f"manifest 引用的文件不是 PNG、JPEG 或 WebP：{rel}")
        if rel not in by_name:
            raise ValueError(f"manifest 引用的条目不存在于包内：{rel}")
        base = PurePosixPath(rel).name
        if base.casefold() in basenames:
            raise ValueError(f"解包后文件名冲突（忽略大小写）：{base}")
        basenames.add(base.casefold())
        entries.append((rel, by_name[rel]))
    referenced = {rel for rel, _info in entries}
    unreferenced = [
        info.filename for info in infos
        if PurePosixPath(info.filename).suffix.lower() in IMAGE_FORMATS
        and info.filename not in referenced
    ]
    if unreferenced:
        raise ValueError(f"分享包含未被 manifest 引用的图片：{unreferenced[0]}")
    return entries, manifest


def _reject_absolute_strings(value: Any, path: str = "manifest") -> None:
    """功能说明：递归扫描 manifest 的所有字符串，拒绝外部 URL 与绝对路径。

    调用时机：[_read_manifest] 在 format 校验之后调用。

    关键步骤：
    1. 字典扫描键与值，列表逐项递归，字符串直接判定；
    2. 含 “://”（外部 URL）或以 “/” 开头（服务器绝对路径）即拒绝，并报出
       出问题的字段路径便于排查。

    输出/副作用：全部干净则返回；命中即抛 ValueError。
    """
    if isinstance(value, str):
        if "://" in value:
            raise ValueError(f"{path} 含外部 URL，已拒绝：{value[:80]}")
        if value.startswith("/"):
            raise ValueError(f"{path} 含绝对路径，已拒绝：{value[:80]}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_absolute_strings(key, f"{path}.{key}")
            _reject_absolute_strings(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_absolute_strings(item, f"{path}[{index}]")


def _extract_images(
    zf: zipfile.ZipFile,
    entries: list[tuple[str, zipfile.ZipInfo]],
    staging: Path,
    max_bytes: int = MAX_BYTES,
    check_write: Callable[[int], None] | None = None,
) -> list[dict[str, Any]]:
    """功能说明：把 manifest 引用的图片逐条流式解出到 staging/images/ 下。

    调用时机：[_strict_extract] 在 manifest 校验通过后调用。

    关键步骤：
    1. 建立 staging/images（0700），落盘名一律取 manifest 路径的 basename，
       不复制包内子目录结构；文件用 O_EXCL 创建（0600），绝不覆盖既有文件；
    2. 逐条 zf.open 定长分块读出，每块写前检查调用方字节上限与磁盘底线；
    3. 解压中途抛出的 CRC/格式错误都转成 ValueError，让上层整包拒绝。

    输出/副作用：staging/images/ 下新增图片文件；返回 [{name, staged, bytes}]；
    超限或损坏抛 ValueError（由 [strict_import] 负责删除整个 staging）。
    """
    out_dir = staging / "images"
    out_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    results: list[dict[str, Any]] = []
    total = 0
    for rel, info in entries:
        base = PurePosixPath(rel).name
        dest = out_dir / base
        written = 0
        try:
            with zf.open(info) as src:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as dst:
                    while True:
                        chunk = src.read(_EXTRACT_CHUNK)
                        if not chunk:
                            break
                        if total + len(chunk) > max_bytes:
                            raise SharepackTooLarge(f"分享包解压总量超过上限 {max_bytes} 字节")
                        if check_write is not None:
                            check_write(len(chunk))
                        written += len(chunk)
                        total += len(chunk)
                        dst.write(chunk)
        except ValueError:
            raise
        except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
            raise ValueError(f"分享包条目无法解压：{rel}（{exc}）") from exc
        results.append({"name": base, "staged": str(dest), "bytes": written})
    return results

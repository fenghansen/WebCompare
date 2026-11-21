"""【模块】文件目录与媒体清单 — 绝对路径只读扫描、版本指纹与扁平列表生成

功能说明：把本机或 SSH 远端的图片、视频目录变成可比较的数据。member 可直接
浏览任何服务账号可读的绝对路径（无需预登记根目录；
viewer 仍只能看分享）。比较模型是扁平图片列表：目录来源按自然排序展开，
依次拼接或逐位交错成一条列表，前端按布局切片成页。资产以内容版本指纹
登记，源文件变动可被事后检测。服务全程只读，不修改源文件。
server 模式由可选的 [Storage] 边界接管路径语义：入站路径在装配层先授权
再解析成真实路径喂给本模块，资产与清单来源按虚拟路径登记（不含数据根，
迁移后照旧可用），浏览输出再换算回虚拟路径下发。

## 流程
所属流程：文件目录与媒体清单（webcompare/files.py）

1. **路径解析**：绝对路径规范化与存在性校验 → 见函数 [resolve_path]
2. **目录浏览**：名称筛选、排序与分页的目录列表 → 见函数 [FileCatalog.browse]
3. **媒体收集**：递归扫描、自然排序与数量上限 → 见函数 [FileCatalog.collect]、[natural_key]
4. **清单生成**：拼接/交错成扁平媒体列表、登记资产版本指纹（server 模式按虚拟路径）并入库 → 见函数 [FileCatalog.create_comparison]、[revision]
5. **资产核验**：每次读取前复核源文件版本，变更即拒绝 → 见函数 [FileCatalog.asset]
6. **统一来源访问**：本机与远端共用解析、归属、物化和缓存租约接口 → 见函数 [FileCatalog.resolve]、[FileCatalog.authorize]、[FileCatalog.materialize]、[FileCatalog.cache_lease]
"""
from __future__ import annotations

import hashlib
import fnmatch
import json
import os
import re
import secrets
import stat
import time
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .config import Settings
from .db import Database
from .image_formats import IMAGE_FORMATS

if TYPE_CHECKING:
    from .ssh import SSHManager
    from .storage import Storage


class FileError(Exception):
    def __init__(self, message: str, code: str = "INVALID_PATH", status: int = 422):
        super().__init__(message)
        self.code, self.status = code, status


def natural_key(value: str):
    """功能说明：把文件名拆成文本/数字段元组，供“图_2 排在 图_10 前”的自然排序。

    调用时机：目录浏览排序与分组键排序时，作为 sort key 反复调用。

    关键步骤：
    1. 按数字片段切分字符串；
    2. 数字段转整数、文本段转小写，组成可比较的元组序列。

    输出/副作用：返回排序键元组；纯函数，无副作用。
    """
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold()) for part in re.split(r"(\d+)", value))


def revision(path: Path, *, st=None) -> str:
    """当前文件版本，用于缓存失效和一次读写期间的一致性检查。"""
    st = path.stat() if st is None else st
    return hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}:{st.st_ino}:{st.st_dev}".encode()).hexdigest()[:32]


def resolve_path(raw: str) -> Path:
    """功能说明：把用户提交的绝对路径规范化为真实路径，是所有文件访问的闸门。

    调用时机：浏览、收集、资产核验等任何触及文件系统的操作之前。

    关键步骤：
    1. 拒绝空值、超长、含 NUL 与非绝对路径（支持 ~ 展开）；
    2. resolve 到真实路径并要求存在（符号链接随文件系统语义放行，
       读边界即服务账号本身的文件系统权限）。

    输出/副作用：返回存在的绝对路径；失败抛 FileError（404/422），不写盘。
    """
    if not raw or len(raw) > 4096 or "\x00" in raw:
        raise FileError("只接受服务器上的绝对路径")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise FileError("只接受服务器上的绝对路径")
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FileError("文件或目录不存在或无法访问", "NOT_FOUND", 404) from exc


class FileCatalog:
    """服务器文件系统的只读目录：路径解析、扫描、配对与资产核验。"""

    def __init__(self, settings: Settings, db: Database, storage: Storage | None = None, remote: SSHManager | None = None):
        """功能说明：绑定配置与数据库；server 模式再绑定存储边界（资产按虚拟路径登记）。

        调用时机：[create_app] 装配层与测试夹具在配置与数据库就绪后构造一次；
        storage 缺省为 None，personal 模式行为与改造前完全一致。

        关键步骤：
        1. 记住 settings、db 与可选的 storage（虚拟路径换算与解析的闸门）。

        输出/副作用：无；不访问文件系统。
        """
        self.settings, self.db, self.storage, self.remote = settings, db, storage, remote

    def _remote_access(self):
        """只检查能力是否已装配，不为分派重复建立 SFTP 通道或查询远端属性。"""
        if self.remote is None:
            raise FileError("当前模式不提供 SSH 远程目录", "SSH_UNAVAILABLE", 403)
        return self.remote

    def resolve(self, raw: str):
        """功能说明：把本机绝对路径或稳定的 SSH 来源解析为对应的只读路径。

        调用时机：浏览、选图、资产核验及下载在完成身份授权后调用。

        关键步骤：
        1. SSH 来源仅交给 personal 实例装配的连接管理器；
        2. 本机来源继续使用原有绝对路径校验。

        输出/副作用：返回本机 Path 或远端 RemotePath；不更改源文件。
        """
        if raw.startswith("ssh://"):
            return self._remote_access().resolve(raw)
        return resolve_path(raw)

    def authorize(self, raw: str, owner_id: str) -> None:
        """统一来源归属闸门；本机仍依服务账号权限，SSH 再限定连接所有者。"""
        if raw.startswith("ssh://"):
            self._remote_access().authorize(raw, owner_id)

    def materialize(self, raw: str, expected_revision: str | None = None, *, allow_video: bool = False) -> Path:
        """为解码、复制或导出提供本机路径；已授权的 server 虚拟路径沿用存储解析。"""
        if raw.startswith("ssh://"):
            return self._remote_access().materialize(raw, expected_revision, allow_video=allow_video)
        path = self.storage.resolve(raw) if self.storage is not None else resolve_path(raw)
        if expected_revision is not None and revision(path) != expected_revision:
            raise FileError("源文件已改变，请重新读取目录", "SOURCE_CHANGED", 409)
        return path

    def cache_lease(self):
        """统一来源缓存租约；请求或后台打包持有期间，清理不能删除正在读取的源。"""
        return self.remote.cache_lease() if self.remote is not None else nullcontext()

    @staticmethod
    def is_image(path: Path) -> bool:
        return path.suffix.lower() in IMAGE_FORMATS or path.suffix.lower() in {".bayer", ".raw", ".npy"}

    @staticmethod
    def is_video(path: Path) -> bool:
        return path.suffix.lower() in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

    @classmethod
    def _is_media(cls, path: Path, media_kind: str) -> bool:
        return cls.is_video(path) if media_kind == "video" else cls.is_image(path)

    @staticmethod
    def _media_name(media_kind: str) -> str:
        return "视频" if media_kind == "video" else "图片"

    @staticmethod
    def _validate_media_kind(media_kind: str) -> None:
        if media_kind not in {"image", "video"}:
            raise FileError("媒体类型必须是图片或视频", "INVALID_MEDIA_KIND")

    def browse(self, raw: str, offset: int, limit: int, q: str = "", sort: str = "name", order: str = "asc", media_kind: str = "image") -> dict:
        """功能说明：列出绝对路径目录的直接子项（目录与所选类型的媒体），供前端来源选择器分页展示。

        调用时机：前端浏览目录时，GET /api/browse 经线程池调用。

        关键步骤：
        1. 经 [resolve_path] 解析并确认是目录；
        2. 扫描时跳过隐藏项，区分目录/媒体并记录大小与修改时间；
        3. q 含 * 时按大小写不敏感 glob 匹配名称或媒体 stem；否则做名称子串过滤，再按名称（自然序）/大小/
           修改时间排序（目录始终先于媒体）；
        4. 超出一万项报错，提示选择更具体目录；分页返回。

        输出/副作用：返回条目清单、总数与截断标记；只读操作。
        """
        self._validate_media_kind(media_kind)
        if raw.startswith("ssh://"):
            return self._remote_access().browse(raw, offset, limit, q, sort, order, media_kind)
        directory = resolve_path(raw)
        if not directory.is_dir():
            raise FileError("此路径不是目录")
        entries = []
        try:
            with os.scandir(directory) as scan:
                for item in scan:
                    if item.name.startswith("."):
                        continue
                    path = Path(item.path)
                    try:
                        kind = "directory" if item.is_dir() else media_kind if item.is_file() and self._is_media(path, media_kind) else None
                        if kind:
                            st = item.stat()
                            entries.append({"name": item.name, "path": str(path), "kind": kind, "bytes": st.st_size if kind != "directory" else 0, "mtime": int(st.st_mtime)})
                        if len(entries) > 10_000:
                            raise FileError("目录项目过多，请选取更具体的目录", "TOO_MANY_FILES")
                    except OSError:
                        continue
        except PermissionError as exc:
            raise FileError("服务账号无法读取此目录", "PATH_FORBIDDEN", 403) from exc
        if q:
            needle = q.casefold()
            if "*" in q:
                entries = [e for e in entries if fnmatch.fnmatchcase(e["name"].casefold(), needle) or e["kind"] == media_kind and fnmatch.fnmatchcase(Path(e["name"]).stem.casefold(), needle)]
            else:
                entries = [e for e in entries if needle in e["name"].casefold()]
        key = {"name": lambda e: (natural_key(e["name"]), e["name"]), "size": lambda e: e["bytes"], "mtime": lambda e: e["mtime"]}[sort]
        entries.sort(key=lambda e: key(e), reverse=order == "desc")
        entries.sort(key=lambda e: e["kind"] != "directory")  # 稳定排序：目录恒在前
        parent = str(directory.parent)
        result = {"path": str(directory), "parent": None if parent == str(directory) else parent, "entries": entries[offset:offset+limit], "count": len(entries), "truncated": len(entries) > offset + limit}
        if self.storage is not None:
            result = self._virtualize_listing(directory, result)
        return result

    def _virtualize_listing(self, directory: Path, result: dict) -> dict:
        """功能说明：server 模式把浏览结果里的真实路径换算回虚拟路径后下发。

        调用时机：仅 [FileCatalog.browse] 在绑定了 [Storage] 时调用（输入的
        raw 已是装配层授权并解析后的真实路径）。

        关键步骤：
        1. 目录自身与每个条目的 path 经 [Storage.virtual_for] 换算为虚拟路径；
           external（admin 浏览服务器绝对路径）原样保留绝对路径；
        2. 父级换算后若落在区域之外（典型：区域根的父级是数据根本身），
           置为 None，让前端回到根清单而不是拿到不可浏览的绝对路径。

        输出/副作用：返回改写后的浏览结果；纯计算，不写盘。
        """
        current = self.storage.virtual_for(directory)
        parent = result["parent"]
        if parent is not None:
            candidate = self.storage.virtual_for(Path(parent))
            parent = None if not current.startswith(("/", "~")) and candidate.startswith(("/", "~")) else candidate
        entries = [{**entry, "path": self.storage.virtual_for(Path(entry["path"]))} for entry in result["entries"]]
        return {**result, "path": self.storage.display_path(current), "parent": self.storage.display_path(parent) if parent else None, "entries": [{**entry, "path": self.storage.display_path(entry["path"])} for entry in entries]}

    def collect(self, raw: str, recursive: bool, media_kind: str = "image") -> list[tuple[str, Path]]:
        """功能说明：把一路来源（单个媒体或目录）展开成 (配对键, 路径) 序列。

        调用时机：[FileCatalog.create_comparison] 对每一路来源各调用一次。

        关键步骤：
        1. 解析来源；单文件直接返回，目录则深度优先遍历（可递归）；
        2. 跳过隐藏项，只收所选媒体类型；
        3. 配对键取相对来源目录的去扩展名路径；目录数与媒体数超上限即报错；
        4. 按自然排序键排序返回。

        输出/副作用：返回有序的 (键, 路径) 列表；只读操作。
        """
        self._validate_media_kind(media_kind)
        if raw.startswith("ssh://"):
            return self._remote_access().collect(raw, recursive, media_kind)
        media_name = self._media_name(media_kind)
        source = resolve_path(raw)
        if source.is_file():
            if not self._is_media(source, media_kind):
                if media_kind == "video":
                    raise FileError("视频来源只支持 MP4/MOV/M4V/WebM/MKV", "UNSUPPORTED_FORMAT")
                raise FileError("图片来源支持 PNG/JPEG/WebP/Bayer RAW/二维 NPY", "UNSUPPORTED_FORMAT")
            return [(source.stem, source)]
        if not source.is_dir():
            raise FileError(f"来源必须是{media_name}或目录")
        result: list[tuple[str, Path]] = []
        stack = [source]
        visited = 0
        while stack:
            directory = stack.pop()
            visited += 1
            if visited > 5000:
                raise FileError("目录层级项目过多，请缩小选择范围", "TOO_MANY_FILES")
            try:
                with os.scandir(directory) as scan:
                    for entry in scan:
                        if entry.name.startswith("."):
                            continue
                        path = Path(entry.path)
                        if entry.is_dir() and recursive:
                            stack.append(path)
                        elif entry.is_file() and self._is_media(path, media_kind):
                            key = path.relative_to(source).with_suffix("").as_posix()
                            result.append((key, path))
                            if len(result) > self.settings.max_files_per_source:
                                if media_kind == "video":
                                    raise FileError(f"单路最多 {self.settings.max_files_per_source} 个视频，请选择更具体目录", "TOO_MANY_FILES")
                                raise FileError(f"单路最多 {self.settings.max_files_per_source} 张，请选择更具体目录", "TOO_MANY_FILES")
            except PermissionError as exc:
                raise FileError("部分子目录不可读取，请调整来源或关闭递归", "PATH_FORBIDDEN", 403) from exc
        return sorted(result, key=lambda item: (natural_key(item[0]), item[1].name))

    def create_comparison(self, owner_id: str, sources: list[dict], combine: str, recursive: bool, groups: list[dict] | None = None, media_kind: str = "image", *, persist: bool = True, image_order: list[str] | None = None) -> dict:
        """功能说明：把有界来源（默认最多 500 路）展开成扁平媒体列表，生成比较清单并入库。

        调用时机：用户在文件管理页点击“对比/序列对比”后，POST
        /api/comparisons 经线程池调用。server 模式下装配层已把来源路径授权
        并解析成真实路径，本函数经 [Storage.virtual_for] 换算回虚拟路径登记。

        关键步骤：
        1. 逐路调用 [FileCatalog.collect] 展开所选媒体序列（自然排序），空来源报错；
        2. concat 按来源顺序依次拼接；interleave 逐位交错（A0,B0,A1,B1…，
           长度不齐时跳过耗尽来源，不塞空位）；
        3. 普通来源按绝对路径去重；规则组仅在组内去重，跨组保留对位槽；
        4. 为每个媒体计算资产 ID（登记路径+版本指纹的散列；server 模式的登记
           路径是虚拟路径，资产 ID 因此不绑死数据根的绝对位置）；
        5. 普通比较保留每路来源的资产归属，供显示顺序转换使用；不改原始序列；
        6. 图片或视频至少需要 1 个；资产登记与清单一并写入 SQLite。

        输出/副作用：写入 assets 与 comparisons 表；返回媒体清单、资产表
        和来源标签（server 模式下 sources[].path 与 assets[].path 均为虚拟
        路径）。普通比较按布局切页，规则组按组内相同位置对齐。
        """
        self._validate_media_kind(media_kind)
        media_name = self._media_name(media_kind)
        if image_order and (self.storage is not None or groups is not None):
            raise FileError("image_order 仅供 personal 普通清单使用", "INVALID_IMAGE_ORDER")
        group_paths: list[tuple[str, list[Path]]] | None = None
        source_paths: list[Path] = []
        if groups is not None:
            if sources:
                raise FileError("分组模式不能同时指定 sources", "MIXED_GROUPS_SOURCES")
            if not groups:
                raise FileError("至少需要一个分组", "EMPTY_GROUPS")
            if len(groups) > 32:
                raise FileError("最多支持 32 个分组", "TOO_MANY_GROUPS")
            group_paths = []
            source_seen: set[str] = set()
            catalogs_by_path: dict[str, list[tuple[str, Path]]] = {}
            for group in groups:
                label = group["label"].strip()
                rules = group["rules"]
                if not label:
                    raise FileError("分组名称不能为空", "EMPTY_GROUP_LABEL")
                if not rules:
                    raise FileError(f"分组“{label}”至少需要一条匹配规则", "EMPTY_GROUP_RULES")
                if len(rules) > self.settings.max_sources:
                    raise FileError(f"每个分组最多支持 {self.settings.max_sources} 条规则", "TOO_MANY_GROUP_RULES")
                matched: list[Path] = []
                used_paths: set[str] = set()
                for rule in rules:
                    source = self.resolve(rule["path"])
                    names = rule.get("names")
                    if names is not None and not source.is_dir():
                        raise FileError(f"精确选择{media_name}时来源必须是目录", "GROUP_NAMES_REQUIRE_DIRECTORY")
                    if str(source) not in source_seen:
                        source_seen.add(str(source))
                        source_paths.append(source)
                    pattern = rule["pattern"].casefold()
                    if str(source) not in catalogs_by_path:
                        catalogs_by_path[str(source)] = self.collect(str(source), recursive, media_kind)
                    for _key, path in catalogs_by_path[str(source)]:
                        selected = names is None or path.relative_to(source).as_posix() in names
                        if selected and (fnmatch.fnmatchcase(path.name.casefold(), pattern) or fnmatch.fnmatchcase(path.stem.casefold(), pattern)):
                            absolute = str(path)
                            if absolute not in used_paths:
                                used_paths.add(absolute)
                                matched.append(path)
                if not matched:
                    raise FileError(f"分组“{label}”没有匹配到{media_name}", "EMPTY_GROUP_MATCH")
                group_paths.append((label, matched))
            ordered_paths = [path for _label, paths in group_paths for path in paths]
            if media_kind == "image" and not ordered_paths:
                raise FileError("至少需要一张图片", "TOO_FEW_IMAGES")
            normalized = [{"path": self._register_path(path), "label": (path.name or str(path))[:120]} for path in source_paths]
        else:
            if not sources:
                raise FileError(f"至少需要一个{media_name}来源", "EMPTY_SOURCES")
            if len(sources) > self.settings.max_sources:
                raise FileError(f"最多支持 {self.settings.max_sources} 个{media_name}来源", "TOO_MANY_SOURCES")
            catalogs = [self.collect(s["path"], recursive, media_kind) for s in sources]
            if any(not c for c in catalogs):
                formats = "MP4/MOV/M4V/WebM/MKV" if media_kind == "video" else "PNG/JPEG/WebP/Bayer RAW/二维 NPY"
                raise FileError(f"有来源未找到 {formats} {media_name}，请重新选择", "EMPTY_SOURCE")
            if combine == "interleave":
                ordered = [item for index in range(max(len(c) for c in catalogs)) for items in catalogs for item in items[index:index + 1]]
            else:
                ordered = [item for items in catalogs for item in items]
            ordered_paths = [path for _key, path in ordered]
            normalized = [{"path": self._register_path_text(source["path"]), "label": (source.get("label") or PurePosixPath(source["path"]).name or source["path"])[:120]} for source in sources]
        order_rank: dict[str, int] = {}
        collected_paths = {str(path) for path in ordered_paths} if image_order else set()
        for raw in image_order or []:
            if raw.startswith("ssh://"):
                # SSH 顺序采用完整来源 URI，不能交给本机 Path 丢失连接与根路径。
                if len(raw) > 4096 or "\x00" in raw or self.remote is None:
                    raise FileError("image_order 的 SSH 路径无效", "INVALID_IMAGE_ORDER")
                absolute = raw if raw in collected_paths else str(self.resolve(raw))
                order_rank.setdefault(absolute, len(order_rank))
                continue
            path = Path(raw).expanduser()
            if len(raw) > 4096 or "\x00" in raw or not path.is_absolute():
                raise FileError("image_order 只接受服务器上的绝对路径", "INVALID_IMAGE_ORDER")
            try:
                absolute = str(path)
                if absolute not in collected_paths:
                    absolute = str(path.resolve(strict=False))
            except (OSError, RuntimeError) as exc:
                raise FileError("image_order 路径无法解析", "INVALID_IMAGE_ORDER") from exc
            order_rank.setdefault(absolute, len(order_rank))
        if order_rank:
            # 顺序只作用于来源已选中的文件；移除的路径忽略，新结果按收集顺序追加。
            ordered_paths.sort(key=lambda path: order_rank.get(str(path), len(order_rank)))
        seen: set[str] = set()
        assets: dict[str, dict] = {}
        asset_ids_by_path: dict[str, str] = {}
        images: list[str] = []
        for path in ordered_paths:
            absolute = str(path)
            if group_paths is None and absolute in seen:
                continue
            seen.add(absolute)
            if absolute not in asset_ids_by_path:
                # 目录扫描提供的属性可以直接用于登记，避免远端逐文件再开连接。
                # 实际读图仍重新在线核验，登记快照不承担后续有效性保证。
                st = getattr(path, "snapshot_stat", None) or path.stat()
                rev = revision(path, st=st)
                registered = self._register_path(path)
                asset_id = hashlib.sha256(f"{registered}\0{rev}".encode()).hexdigest()[:40]
                asset_ids_by_path[absolute] = asset_id
                assets[asset_id] = {"id": asset_id, "name": path.name, "path": registered, "revision": rev, "bytes": st.st_size}
            asset_id = asset_ids_by_path[absolute]
            images.append(asset_id)
        if media_kind == "image" and not images:
            raise FileError("至少需要一张图片", "TOO_FEW_IMAGES")
        manifest = {"id": secrets.token_urlsafe(18), "created_at": int(time.time()), "sources": normalized, "combine": combine, "recursive": recursive, "images": images, "assets": assets}
        if group_paths is None:
            manifest["source_images"] = [list(dict.fromkeys(asset_ids_by_path[str(path)]
                for _key, path in catalog if str(path) in asset_ids_by_path)) for catalog in catalogs]
        if media_kind == "video":
            manifest["media_kind"] = "video"
        if group_paths is not None:
            manifest["groups"] = []
            for label, paths in group_paths:
                group_images = [asset_ids_by_path[str(path)] for path in paths]
                manifest["groups"].append({"label": label, "images": group_images})
        if self.storage is None:
            # personal 的可编辑分享需要保留来源规则，不能从展开后的图片倒推。
            manifest["selection"] = {"sources": normalized if groups is None else [], "combine": combine,
                                     "recursive": recursive, "media_kind": media_kind}
            if groups is None:
                manifest["selection"]["image_order"] = list(order_rank)
            if groups is not None:
                manifest["selection"]["groups"] = [{**group, "rules": [
                    {**rule, "path": str(self.resolve(rule["path"]))} for rule in group["rules"]
                ]} for group in groups]
        with self.db.connect() as conn:
            conn.executemany("INSERT OR IGNORE INTO assets VALUES(?,?,?,?)", [(a["id"], a["path"], a["revision"], a["bytes"]) for a in assets.values()])
            if persist:
                conn.execute("INSERT INTO comparisons VALUES(?,?,?,?)", (manifest["id"], owner_id, json.dumps(manifest, ensure_ascii=False), manifest["created_at"]))
        return manifest

    def _register_path(self, path: Path) -> str:
        """功能说明：给出资产与清单来源的入库路径：server 模式存虚拟路径，personal 存绝对路径。

        调用时机：[FileCatalog.create_comparison] 登记资产与规范化来源时调用。

        关键步骤：
        1. 绑定了 [Storage] 时经 [Storage.virtual_for] 把真实路径换算成虚拟路径
           （external 仍落绝对路径字符串）；
        2. 未绑定（personal）时返回绝对路径字符串，与改造前一致。

        输出/副作用：返回入库路径字符串；纯计算。
        """
        if self.storage is not None:
            return self.storage.virtual_for(path)
        return str(path)

    def _register_path_text(self, raw: str) -> str:
        """功能说明：sources[].path 的入库换算：personal 原样保留用户输入，server 换成虚拟路径。

        调用时机：[FileCatalog.create_comparison] 规范化普通来源（非分组）时调用。

        关键步骤：
        1. 未绑定 [Storage]（personal）时逐字返回输入路径（现状行为）；
        2. 绑定时输入已是装配层解析好的真实路径，经 [Storage.virtual_for] 换算。

        输出/副作用：返回入库路径字符串；纯计算。
        """
        if self.storage is not None:
            return self.storage.virtual_for(Path(raw))
        return str(self.resolve(raw)) if raw.startswith("ssh://") else raw

    def asset(self, asset_id: str, *, materialize: bool = True, allow_video: bool = False) -> tuple[dict, Path]:
        """按保存路径读取当前文件；同名替换保留资产 ID，并使媒体缓存失效。"""
        asset = self.db.one("SELECT * FROM assets WHERE id=?", (asset_id,))
        if asset is None:
            raise FileError("图片不存在", "ASSET_NOT_FOUND", 404)
        # storage.resolve 内部对绝对路径同样委托 resolve_path，两种登记形态共用一条解析路径。
        path = self.storage.resolve(asset["path"]) if self.storage is not None else self.resolve(asset["path"])
        current = path.stat()
        if not stat.S_ISREG(current.st_mode):
            raise FileError("来源不是普通文件", "INVALID_SOURCE", 422)
        asset = {**asset, "revision": revision(path, st=current), "bytes": current.st_size}
        if asset["path"].startswith("ssh://") and materialize:
            # 解码器只读取本机缓存；视频 Range 与单纯版本核验不下载整段源文件。
            # 视频只会在明确的原文件下载任务中走物化；播放器始终使用 materialize=False。
            path = self.remote.materialize(asset["path"], asset["revision"], allow_video=allow_video)
        return asset, path

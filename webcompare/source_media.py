"""【模块】统一来源媒体服务 — 在保持原解码接口的同时按能力加速远端预览

功能说明：本机与 SSH 来源复用 metadata/render/pixel/validate 调用。远端已有
处理能力时回传普通图片的小预览、完整高清编码或元数据；缺少能力时读取原文件。
精确显示、取色、RAW 和差分始终保留原始数据及既有解码、颜色处理算法。

## 流程
所属流程：SSH 远端来源（webcompare/source_media.py）

1. **元数据**：按能力读取远端原始尺寸，缺少能力时本机解析 → 见函数 [SourceMediaService.metadata]
2. **预览与精确处理**：远端生成预览或整图高清，精确处理读取原图 → 见函数 [SourceMediaService.render]、[SourceMediaService.pixel]
"""
from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path

from starlette.concurrency import run_in_threadpool

from .files import revision
from .ssh import RemotePath


class SourceMediaService:
    """对既有 MediaService 的窄适配；来源分派集中在此，不复制图像算法。"""

    def __init__(self, backend, catalog, *, preview=None):
        self.backend, self.catalog = backend, catalog
        if preview is None:
            from .ssh_preview import RemotePreviewEngine
            preview = RemotePreviewEngine(catalog.remote)
        self.preview = preview
        self._source_leases = {}

    def __getattr__(self, name):
        # 生命周期、队列、缓存统计和结果租约完全沿用既有媒体服务。
        return getattr(self.backend, name)

    def pin(self, path):
        # The middleware source lease ends at response headers; keep encoded SSH bytes alive through the body.
        remote = getattr(self.catalog, "remote", None)
        if remote is not None and Path(path).resolve().is_relative_to(remote.cache_dir.resolve()):
            lease = remote.cache_lease()
            lease.__enter__()
            if not Path(path).is_file():
                lease.__exit__(None, None, None)
                raise FileNotFoundError(path)
            token = "source:" + uuid.uuid4().hex
            self._source_leases[token] = lease
            return token
        return self.backend.pin(path)

    def unpin(self, token, **kwargs):
        lease = self._source_leases.pop(token, None)
        if lease is not None:
            lease.__exit__(None, None, None)
        else:
            return self.backend.unpin(token, **kwargs)

    async def _local(self, source, expected_revision=None) -> Path:
        if isinstance(source, RemotePath):
            return await run_in_threadpool(self.catalog.materialize, str(source), expected_revision)
        return Path(source)

    async def _preview_call(self, operation, *args, **kwargs):
        cancel_event = threading.Event()
        try:
            return await run_in_threadpool(operation, *args, cancel_event=cancel_event, **kwargs)
        except asyncio.CancelledError:
            cancel_event.set()
            raise

    async def metadata(self, source, *, raw=None):
        """功能说明：读取原始图像元数据，远端普通图片优先只回传小型信息。

        调用时机：工作台以原有 metadata 接口载入一张图的真实尺寸时调用。

        关键步骤：
        1. 远端来源核验当前版本并尝试已有工具；
        2. 缺少能力或格式不适用时沿用原文件物化与本机解析。

        输出/副作用：返回既有元数据结构；不以预览尺寸代替原始尺寸。
        """
        if isinstance(source, RemotePath):
            current = await run_in_threadpool(revision, source)
            result = await self._preview_call(self.preview.metadata, source, current, self.backend.max_pixels)
            if result is not None:
                return result
        return await self.backend.metadata(await self._local(source), raw=raw,
                                           source_name=source.name if isinstance(source, RemotePath) else None)

    async def render(self, source, current_revision: str, **options):
        """功能说明：统一渲染入口，仅普通预览可使用远端已有工具生成的小图。

        调用时机：选择器缩略图或工作台 render/diff 沿用原接口调用。

        关键步骤：
        1. 普通 preview 且无整图/差分时尝试远端预览，缺少能力自动回退；
        2. 普通整图 high 尝试远端高清编码，exact、取色及差分物化原始数据；
        3. 两路差分的来源读取可并行，但不改变像素、RAW 或色彩处理。

        输出/副作用：返回既有 MediaResult；近似预览不替代原始定量数据。
        """
        diff_source = options.get("diff_source")
        if isinstance(source, RemotePath) or isinstance(diff_source, RemotePath):
            options.pop("local_lossless", None)
        if (isinstance(source, RemotePath) and options.get("quality") == "high"
                and options.get("full", False) and options.get("box") is None and diff_source is None):
            result = await self._preview_call(self.preview.high, source, current_revision, self.backend.max_pixels)
            if result is not None:
                return result
        if (isinstance(source, RemotePath) and options.get("quality", "preview") == "preview"
                and not options.get("full", False) and diff_source is None):
            small = await self._preview_call(
                self.preview.render, source, current_revision, options.get("max_size", 1024),
                box=options.get("box"), max_pixels=self.backend.max_pixels,
            )
            if small is not None:
                return await self.backend.render(small, current_revision, **options)
        if diff_source is not None:
            local, other = await asyncio.gather(
                self._local(source, current_revision), self._local(diff_source, options.get("diff_revision")),
            )
            options = {**options, "diff_source": other,
                       "diff_source_name": diff_source.name if isinstance(diff_source, RemotePath) else None}
        else:
            local = await self._local(source, current_revision)
        return await self.backend.render(local, current_revision, **options,
                                         source_name=source.name if isinstance(source, RemotePath) else None)

    async def pixel(self, source, x: int, y: int, *, raw=None):
        """功能说明：以原始来源执行既有取色；远端小预览不参与定量读数。

        调用时机：用户在工作台读取真实像素时调用。

        关键步骤：
        1. 按需取得原始文件，再交给既有解码器。

        输出/副作用：返回原有像素值结构，不使用缩略图近似值。
        """
        return await self.backend.pixel(await self._local(source), x, y, raw=raw,
                                        source_name=source.name if isinstance(source, RemotePath) else None)

    async def validate(self, source, *, raw=None):
        """完整校验始终读取原图，不以远端预览的成功代替原文件有效性。"""
        return await self.backend.validate(await self._local(source), raw=raw,
                                           source_name=source.name if isinstance(source, RemotePath) else None)

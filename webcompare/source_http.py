"""【模块】统一媒体字节响应 — 本机文件和 SSH 视频共用单段 Range 端点

功能说明：集中处理来源差异；HTTP 端点统一传入已授权路径，本机使用原有
文件响应，SSH 使用独立 SFTP 通道提供浏览器需要的字节，不改变播放器逻辑。

## 流程
所属流程：HTTP API 与安全边界（webcompare/source_http.py）

1. **视频字节响应**：当前文件、GET/HEAD 与单段 Range → 见函数 [source_file_response]
"""
from __future__ import annotations

import re
import stat
import logging
import anyio

from fastapi import Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from pathlib import Path
from starlette.concurrency import run_in_threadpool

from .files import FileCatalog, FileError, revision

LOGGER = logging.getLogger(__name__)


class SourceStreamInterrupted(ConnectionError):
    """已开始发送的来源流中断；只能中止传输，不能再尝试发送 JSON 错误响应。"""


class SourceStreamingResponse(StreamingResponse):
    """范围流在客户端取消或发送失败时也明确关闭同步来源，不依赖框架的生成器回收。"""

    def __init__(self, source_iterator, **kwargs):
        self.source_iterator = source_iterator
        super().__init__(source_iterator, **kwargs)

    async def __call__(self, scope, receive, send):
        disconnected = False

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message["type"] == "http.disconnect":
                disconnected = True
            return message

        try:
            await super().__call__(scope, observed_receive, send)
        except FileError as exc:
            if not disconnected:
                # 响应头已送出，不能交给业务异常处理器改写成 409/503 JSON。
                # 保留 Content-Length 并中止连接，让浏览器明确识别读取失败而非接受短文件。
                LOGGER.warning("来源范围流中断：%s", exc.code)
                raise SourceStreamInterrupted("来源读取中断，请重新连接后重试") from None
        finally:
            # iterate_in_threadpool 的 async generator 关闭并不会替调用方关闭同步迭代器。
            # 屏蔽请求取消，只完成资源释放；SFTP 自身的读超时约束关闭等待。
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    await run_in_threadpool(self.source_iterator.close)


class PrimedSource:
    """保留响应头之前预读的一块数据；即使响应未开始迭代，也能直接关闭原 SFTP 流。"""

    def __init__(self, source, first: bytes):
        self.source, self.first = source, first

    def __iter__(self):
        return self

    def __next__(self):
        if self.first is not None:
            first, self.first = self.first, None
            return first
        return next(self.source)

    def close(self):
        self.first = None
        self.source.close()


async def source_file_response(catalog: FileCatalog, source, request: Request):
    """功能说明：为本机或远端视频生成统一 GET/HEAD 与单段 Range 响应。

    调用时机：选择器视频封面或比较页视频请求已完成用户/分享授权后调用。

    关键步骤：
    1. 在线读取当前文件，不把整段远端视频物化到本机；
    2. 支持起止、开放结尾和后缀范围，拒绝多段或越界范围；
    3. 使用独立 SFTP 通道逐块发送，HEAD 仅返回头，断开时关闭迭代器。

    输出/副作用：返回原始字节响应；只读远端文件，不创建视频缓存。
    """
    if isinstance(source, Path):
        if "," in request.headers.get("range", ""):
            raise FileError("只支持单段字节范围", "MULTIPLE_RANGES", 416)
        return FileResponse(source, media_type="application/octet-stream", headers={
            "Cache-Control": "private, no-store", "Vary": "Cookie",
            "ETag": '"' + revision(source) + '"',
        })

    # 来源差异集中在这个适配点；HTTP 端点无需判断 URI 或改变视频播放器逻辑。
    def inspect():
        current_stat = source.stat()
        if not stat.S_ISREG(current_stat.st_mode) or not FileCatalog.is_video(source):
            raise FileError("不是可预览的视频", "NOT_VIDEO", 422)
        current = revision(source, st=current_stat)
        return current_stat.st_size, current

    size, current = await run_in_threadpool(inspect)
    etag = '"' + current + '"'
    headers = {"Cache-Control": "private, no-store", "Vary": "Cookie", "ETag": etag,
               "Accept-Ranges": "bytes"}
    start, end, status = 0, size - 1, 200
    requested = request.headers.get("range")
    if requested and request.headers.get("if-range", etag) == etag:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
        if len(requested) > 256 or not match or not any(match.groups()):
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        left, right = match.groups()
        if left:
            start = int(left)
            end = min(int(right), size - 1) if right else size - 1
        else:
            start = max(0, size - int(right))
        if size == 0 or start > end or start >= size or not left and int(right) == 0:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = max(0, end - start + 1)
    headers["Content-Length"] = str(length)
    if request.method == "HEAD" or not length:
        return Response(status_code=status, headers=headers, media_type="application/octet-stream")
    stream = catalog.remote.open_range(str(source), start, length, expected_revision=current)

    def prime():
        try:
            return next(stream)
        except StopIteration as exc:
            raise FileError("远端视频读取不完整，请重新连接后重试", "SSH_DISCONNECTED", 503) from exc

    try:
        # 首次 channel 建立和首块读取也可能失败；先完成它们，才能在发头前返回明确错误。
        # 只预读一个有界块，不预下载整个 Range，更不会预下载整个视频。
        first = await run_in_threadpool(prime)
        return SourceStreamingResponse(PrimedSource(stream, first), status_code=status, headers=headers,
                                       media_type="application/octet-stream")
    except BaseException:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(stream.close)
        raise

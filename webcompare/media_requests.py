"""媒体 HTTP 请求的生命周期与自适应画质提示；不参与访问授权。"""
from __future__ import annotations

import asyncio
import ipaddress
import os

from fastapi import HTTPException, Request


def is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def local_image_transport(request: Request | None, settings) -> bool:
    if settings.mode != "personal":
        return False
    if settings.client_transport != "auto":
        return settings.client_transport == "local"
    if request is None or os.getenv("SSH_CONNECTION") or os.getenv("SSH_TTY"):
        return False
    if any(name in request.headers for name in ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")):
        return False

    # SSH 隧道与本地 HTTP 可能完全相同；后台隧道实例可显式配置 remote。
    return bool(request.client and is_loopback(request.client.host) and is_loopback(request.url.hostname))


async def while_connected(request: Request, operation):
    """客户端离开后撤销它的媒体订阅，让 worker 丢弃无人等待的排队任务。"""
    async def disconnected():
        while True:
            if (await request.receive())["type"] == "http.disconnect":
                return

    work = asyncio.ensure_future(operation)
    watcher = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            return await work
        raise HTTPException(499, "图像请求已取消")
    finally:
        for task in (work, watcher):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, watcher, return_exceptions=True)

"""桌面私有后端：随机回环端口、逐请求令牌校验与父进程管道生命周期。"""
from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import multiprocessing
import os
from pathlib import Path
import socket
import sys
import threading
import time


class DesktopTokenGate:
    """在应用及静态资源之前检查令牌；lifespan 事件原样透传。"""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token.encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            values = [value for name, value in scope.get("headers", ())
                      if name.lower() == b"x-webcompare-desktop"]
            if len(values) != 1 or not hmac.compare_digest(values[0], self.token):
                from starlette.responses import JSONResponse

                response = JSONResponse({"detail": {"code": "DESKTOP_TOKEN_REQUIRED",
                                                    "message": "请通过桌面客户端访问"}}, status_code=403)
                return await response(scope, receive, send)
        return await self.app(scope, receive, send)


def desktop_settings(data_dir: Path):
    """桌面状态完全由显式目录决定，不继承其他 WebCompare 实例的配置。"""
    if not data_dir.is_absolute():
        raise ValueError("--data-dir 必须是绝对路径")
    token = os.environ.get("WEBCOMPARE_DESKTOP_TOKEN", "")
    if not token or len(token) > 4096 or any(ord(value) < 33 or ord(value) > 126 for value in token):
        raise ValueError("缺少有效的桌面访问令牌")
    for name in list(os.environ):
        if name.startswith("WEBCOMPARE_") and name != "WEBCOMPARE_DESKTOP_TOKEN":
            del os.environ[name]
    os.environ.pop("_WEBCOMPARE_SERVICE_LOG_DIR", None)
    os.environ.update(WEBCOMPARE_MODE="personal", WEBCOMPARE_REQUIRE_LOGIN="0",
                      WEBCOMPARE_CLIENT_TRANSPORT="local", WEBCOMPARE_DATA_DIR=str(data_dir.resolve()))
    from .config import Settings

    return Settings.from_env(), token


def _stdin_lines():
    if os.name != "nt":
        yield from sys.stdin
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(sys.stdin.fileno())
    if kernel.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
        yield from sys.stdin
        return
    kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                                    ctypes.POINTER(wintypes.DWORD)]
    kernel.PeekNamedPipe.restype = wintypes.BOOL
    buffered = b""
    while True:
        available = wintypes.DWORD()
        if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
            error = ctypes.get_last_error()
            if error in {109, 232, 233}:  # Broken/disconnected pipe is parent EOF.
                return
            raise ctypes.WinError(error)
        if not available.value:
            # A pending ReadFile locks this pipe during child CRT initialization.
            time.sleep(.05)
            continue
        data = os.read(sys.stdin.fileno(), min(available.value, 4096))
        if not data:
            return
        buffered += data
        while b"\n" in buffered:
            line, buffered = buffered.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")


def _watch_stdin(loop, server, updater):
    try:
        if sys.stdin is not None:
            for line in _stdin_lines():
                if line.strip() == "shutdown":
                    break
                if line.strip() == "update-failed":
                    updater.reject_install()
    finally:
        try:
            loop.call_soon_threadsafe(setattr, server, "should_exit", True)
        except RuntimeError:
            pass


async def _serve(settings, token):
    import uvicorn
    from .app import create_app
    from .updates import DesktopUpdater, install_message, package_kind

    class DesktopServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            if self.started and not self.should_exit:
                host, port = sockets[0].getsockname()[:2]
                print("WEBCOMPARE_DESKTOP_READY " + json.dumps(
                    {"url": f"http://{host}:{port}", "pid": os.getpid()}, separators=(",", ":")), flush=True)

    def install_ready(package, version, path):
        print(install_message(token, package, version, path), flush=True)

    updater = DesktopUpdater(settings.data_dir / "updates", package_kind(), install_ready)
    app = create_app(settings, native_desktop=True, desktop_update=updater)
    config = uvicorn.Config(DesktopTokenGate(app, token), host="127.0.0.1", port=0,
                            workers=1, loop="asyncio", http="h11", ws="none", lifespan="on",
                            log_config=None, access_log=False, proxy_headers=False,
                            timeout_keep_alive=5, timeout_graceful_shutdown=5)
    server = DesktopServer(config)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        threading.Thread(target=_watch_stdin, args=(asyncio.get_running_loop(), server, updater), daemon=True).start()
        await server.serve(sockets=[listener])


def main(argv=None):
    # PyInstaller diverts spawned media workers before parsing desktop arguments.
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(description="WebCompare 桌面私有后端")
    parser.add_argument("--data-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        settings, token = desktop_settings(args.data_dir)
        if not (settings.frontend_dir / "index.html").is_file():
            raise ValueError("前端文件缺失，请先构建前端")
        from .logging_setup import configure_logging

        configure_logging(settings.data_dir / "logs")
        asyncio.run(_serve(settings, token))
    except (ValueError, OSError) as exc:
        print(f"WebCompare 桌面后端启动失败：{exc}", file=sys.stderr, flush=True)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

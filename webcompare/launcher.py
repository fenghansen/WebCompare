"""桌面与无参数命令入口：复用已运行实例，在服务就绪后打开浏览器。"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener
import webbrowser


def browser_url(host: str, port: int) -> str:
    """监听全部地址时，浏览器仍通过本机 loopback 访问。"""
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}"


def _get_json(url: str) -> dict:
    # 本机服务探测不经过用户的 HTTP 代理。
    with build_opener(ProxyHandler({})).open(url, timeout=0.5) as response:
        value = json.load(response)
    return value if isinstance(value, dict) else {}


def _open_browser(url: str) -> None:
    logger = logging.getLogger("webcompare.serve")
    logger.info("浏览器入口：%s（在服务终端按 Ctrl+C 停止）", url)
    if os.getenv("WEBCOMPARE_NO_BROWSER") == "1" or (
        os.name != "nt" and sys.platform != "darwin"
        and not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY"))
    ):
        return
    try:
        if not webbrowser.open(url):
            logger.info("请在浏览器中打开上述地址")
    except (OSError, webbrowser.Error):
        logger.info("请在浏览器中打开上述地址")


def reuse_running_service(url: str, settings) -> bool:
    """只复用通过健康与认证能力检查的 WebCompare，不更改已有实例配置。"""
    try:
        health = _get_json(url + "/api/health")
    except (OSError, URLError, ValueError):
        return False
    try:
        providers = _get_json(url + "/api/auth/providers")
    except (OSError, URLError, ValueError):
        raise ValueError(f"{url} 已被其他服务占用，请用 serve --port 其他端口启动") from None
    if health.get("status") != "ok" or not health.get("version") or providers.get("local") is not True:
        raise ValueError(f"{url} 已被其他服务占用，请用 serve --port 其他端口启动")
    if providers.get("mode") != settings.mode or providers.get("login_required") != settings.login_required:
        raise ValueError(f"{url} 的 WebCompare 使用不同的模式或登录设置，请使用其他端口或原服务配置")
    logging.getLogger("webcompare.serve").info("使用已运行的 WebCompare：%s；保持该实例原有状态目录和配置", url)
    _open_browser(url)
    return True


def open_when_ready(url: str) -> threading.Event:
    """后台等待健康检查；无桌面的服务器仅输出地址，不阻塞服务。"""
    stop = threading.Event()

    def wait_for_service():
        deadline = time.monotonic() + 30
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                if _get_json(url + "/api/health").get("status") == "ok":
                    if not stop.is_set():
                        _open_browser(url)
                    return
            except (OSError, URLError, ValueError):
                pass
            stop.wait(0.2)

    threading.Thread(target=wait_for_service, name="webcompare-browser", daemon=True).start()
    return stop

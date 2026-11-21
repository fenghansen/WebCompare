"""【模块】服务日志 — 按本地日期追加，分流文件与终端输出

功能说明：请求、应用与媒体 worker 日志保存在同一按日期命名的目录；
终端只显示生命周期与需要关注的事件。格式化时统一省略 URL 查询参数。
仅 ``serve`` 入口启用；不重定向 stdout/stderr，账号 CLI 的一次性密码和
媒体沙箱的 RESULT/ERROR 协议不进入日志。spawn 媒体 worker 经内部环境
变量加入同一目录，各进程独立以 append 写入，不在跨日时重命名共享文件。

## 流程
所属流程：服务日志（webcompare/logging_setup.py）

1. **目录选择**：按参数、环境和部署模式确定日志位置 → 见函数 [resolve_log_dir]
2. **按日追加**：每条记录按本地日期选择文件，保留多进程追加与权限 → 见函数 [DailyFileHandler.emit]、[DailyFileHandler._open]
3. **内容与终端分流**：清理 URL 查询参数，普通请求只写文件 → 见函数 [SafeFormatter.format]、[ConsoleFilter.filter]
4. **服务装配**：替换服务专用处理器并接入 uvicorn 与告警 → 见函数 [configure_logging]
5. **子进程接入**：仅由 serve 继承的内部目录启用 worker 日志 → 见函数 [configure_worker_logging]
"""
from __future__ import annotations

import copy
import logging
import os
import re
import sys
import time
from pathlib import Path

from .config import DEFAULT_DATA_ROOT, PERSONAL_DATA_DIR, STATE_SUBDIR


_WORKER_LOG_DIR = "_WEBCOMPARE_SERVICE_LOG_DIR"
_URL_QUERY = re.compile(r"((?:https?://|/)[^\s\"'<>?]*)\?[^\s\"'<>]*")
_FORMAT = "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"


def resolve_log_dir(explicit: Path | None = None) -> Path:
    """功能说明：在完整配置校验前定位日志，以便记录启动配置错误。

    调用时机：仅 serve 启动时调用，账号等一次性命令不创建服务日志。

    关键步骤：
    1. 优先命令行目录，其次 WEBCOMPARE_LOG_DIR；
    2. 否则采用显式状态目录，server 默认内容根/.state，personal 默认个人目录；
    3. 在相应状态目录下使用 logs 并展开用户目录。

    输出/副作用：返回绝对路径；尚不创建目录或读取凭据。
    """
    chosen = explicit or os.getenv("WEBCOMPARE_LOG_DIR")
    if chosen:
        return Path(chosen).expanduser().resolve()
    if "WEBCOMPARE_DATA_DIR" in os.environ:
        data_dir = Path(os.environ["WEBCOMPARE_DATA_DIR"])
    elif os.getenv("WEBCOMPARE_MODE", "personal").strip().lower() == "server":
        data_dir = Path(os.getenv("WEBCOMPARE_DATA_ROOT", DEFAULT_DATA_ROOT)) / STATE_SUBDIR
    else:
        data_dir = Path(PERSONAL_DATA_DIR)
    return (data_dir.expanduser() / "logs").resolve()


class SafeFormatter(logging.Formatter):
    """同时清理访问记录、普通消息和异常堆栈中的 URL 查询参数。"""

    def format(self, record: logging.LogRecord) -> str:
        """功能说明：统一省略普通消息和异常堆栈中的 URL 查询参数。

        调用时机：文件及终端处理器格式化每条日志时调用。

        关键步骤：
        1. 复制记录并清空异常格式缓存，避免处理器之间共享未清理的堆栈；
        2. 完成普通格式化后省略 URL 的查询内容，保留路径用于诊断。

        输出/副作用：返回安全格式的文本，不修改调用者的原记录。
        """
        clean = copy.copy(record)
        clean.exc_text = None
        return _URL_QUERY.sub(r"\1?[redacted]", super().format(clean))


class DailyFileHandler(logging.FileHandler):
    """每条记录按其本地日期选择文件；重启及多进程都只追加，不改名。"""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        super().__init__(self._filename(time.time()), mode="a", encoding="utf-8", delay=True)

    def _filename(self, created: float) -> Path:
        day = time.strftime("%Y-%m-%d", time.localtime(created))
        return self.directory / f"webcompare-{day}.log"

    def _open(self):
        """功能说明：用受限权限打开当前日期的追加日志。

        调用时机：第一次写入或跨日重新打开文件时，由 FileHandler 调用。

        关键步骤：
        1. 以 O_APPEND 打开文件，新文件权限为 0600，不改已有文件权限；
        2. 用 UTF-8 包装描述符，无法编码的字符保留为可追查的转义。

        输出/副作用：返回文本流，可能创建当天日志文件。
        """
        descriptor = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return os.fdopen(descriptor, "a", encoding="utf-8", errors="backslashreplace")

    def emit(self, record: logging.LogRecord) -> None:
        """功能说明：按记录的本地日期选择文件，不重命名多进程共享文件。

        调用时机：日志处理器收到记录且已通过级别/过滤检查后调用。

        关键步骤：
        1. 根据记录时间确定当天文件，而不是沿用进程启动日期；
        2. 日期变化时关闭本进程旧流，交给 FileHandler 延迟打开新流并追加。

        输出/副作用：向对应日期文件追加记录；不自动删除历史日志。
        """
        filename = str(self._filename(record.created).resolve())
        if filename != self.baseFilename:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            self.baseFilename = filename
        super().emit(record)


class ConsoleFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        """功能说明：让终端保留生命周期及异常，普通请求只进入文件。

        调用时机：终端处理器接受每条候选记录时调用。

        关键步骤：
        1. 放行 warning/error 和 HTTP 4xx/5xx；
        2. INFO 仅放行 uvicorn 生命周期与服务/清理提示，其余留在文件。

        输出/副作用：返回是否打印到终端，不改变文件日志内容。
        """
        if record.levelno >= logging.WARNING:
            return True
        if record.name == "uvicorn.access":
            # uvicorn 即使记录 HTTP 失败也使用 INFO，需要单独检查状态码。
            try:
                return int(record.args[4]) >= 400
            except (IndexError, KeyError, TypeError, ValueError):
                return False
        return record.name in {"uvicorn.error", "webcompare.serve", "webcompare.lifecycle"}


def configure_logging(directory: Path, *, worker: bool = False) -> None:
    """功能说明：配置服务和媒体 worker 的双路日志输出。

    调用时机：serve 在配置校验前调用，spawn worker 在处理媒体前调用。

    关键步骤：
    1. 给按日文件与终端安装相同安全格式，终端另加筛选器；
    2. 替换根处理器并关闭本模块旧处理器，避免重复记录和文件占用；
    3. uvicorn 改为向根传播，捕获 Python 告警；serve 通过内部变量传递目录。

    输出/副作用：配置当前进程 logging，可能创建目录；uvicorn 必须使用
    log_config=None 保留配置。不会重定向 stdout/stderr 或记录账号命令输出。
    """
    directory = Path(directory).expanduser().resolve()
    file_handler = DailyFileHandler(directory)
    console_handler = logging.StreamHandler(sys.stderr)
    formatter = SafeFormatter(_FORMAT, datefmt="%Y-%m-%d %H:%M:%S")
    for handler in (file_handler, console_handler):
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        handler._webcompare_service = True
    console_handler.addFilter(ConsoleFilter())

    root = logging.getLogger()
    old_handlers = root.handlers[:]
    root.handlers = [file_handler, console_handler]
    root.setLevel(logging.INFO)
    for handler in old_handlers:
        if getattr(handler, "_webcompare_service", False):
            handler.close()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.setLevel(logging.NOTSET)
        logger.propagate = True
        logger.disabled = False
    logging.captureWarnings(True)
    if not worker:
        os.environ[_WORKER_LOG_DIR] = str(directory)


def configure_worker_logging() -> None:
    """功能说明：让 serve 派生的媒体子进程加入同一日志目录。

    调用时机：spawn worker 进入媒体循环、装配限制与解码环境前调用。

    关键步骤：
    1. 只读取内部传递的目录，不根据公开日志环境变量自行启用；
    2. 目录存在时装配 worker 日志，独立调用媒体模块时保持原行为。

    输出/副作用：可能配置子进程 logging，不处理媒体请求载荷。
    """
    directory = os.getenv(_WORKER_LOG_DIR)
    if directory:
        configure_logging(Path(directory), worker=True)

"""Native desktop shell. The backend and browser never share a JavaScript credential."""
from __future__ import annotations

import argparse
from datetime import datetime
import errno
import html
import http.client
import json
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from . import __version__
from .updates import (INSTALL_MESSAGE, PACKAGE_NAMES, PORTABLE, UpdateError, activate_portable,
                      installer_command, launch_updated_process, package_kind, parse_install_message,
                      preferred_portable, prepared_portable, version_key)

HEADER = "X-WebCompare-Desktop"
READY = "WEBCOMPARE_DESKTOP_READY "
PREFERENCE_KEYS = ("wc-language", "wc-theme", "wc-recent-paths", "wc-treew", "wc-railw", "wc-title-scale",
                   "wc-overlay-titles", "wc-overlay-details", "wc-overlay-coordinates",
                   "wc-listmode", "wc-bookmarks", "wc-bookmarks-seeded")
MAX_PREFERENCE_BYTES = 256 * 1024
SHARE_ID = re.compile(r"[0-9a-f]{32}")
INSTANCE_REQUEST = re.compile(rb"WC1 ([0-9a-f]{64}) (wake|open ([0-9a-f]{32}))\n")
INSTANCE_TIMEOUT = 45


def is_backend_url(url: str, origin: str | None) -> bool:
    try:
        value, expected = urlsplit(url), urlsplit(origin or "")
        return (value.scheme == "http" and value.hostname == "127.0.0.1" and value.port == expected.port
                and expected.scheme == "http" and expected.hostname == "127.0.0.1"
                and value.username is None and value.password is None)
    except (TypeError, ValueError):
        return False


def allowed_download(url: str, origin: str) -> bool:
    return is_backend_url(url, origin) or url.startswith("blob:" + origin + "/")


def authenticated_headers(headers: dict, url: str, origin: str, token: str) -> dict:
    values = {name: value for name, value in headers.items() if name.lower() != HEADER.lower()}
    if is_backend_url(url, origin):
        values[HEADER] = token
    return values


def parse_ready(line: str, expected_pid: int) -> str | None:
    if not line.startswith(READY):
        return None
    try:
        value = json.loads(line[len(READY):])
        match = re.fullmatch(r"http://127\.0\.0\.1:([1-9]\d{0,4})/?", value["url"])
        if (not match or int(match[1]) > 65535 or type(value["pid"]) is not int
                or value["pid"] != expected_pid or expected_pid <= 0):
            raise ValueError
    except (TypeError, ValueError, KeyError):
        raise ValueError("本地服务返回了不受信任的启动信息。") from None
    return value["url"].rstrip("/")


def default_user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    # Keep desktop state separate from browser/server profiles.
    return base.expanduser().resolve() / "WebCompare Native"


def preferences_only(value) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("桌面偏好格式无效。")
    result = {key: value[key] for key in PREFERENCE_KEYS if key in value}
    if any(not isinstance(item, str) for item in result.values()):
        raise ValueError("桌面偏好值必须是文本。")
    if len(json.dumps({"version": 1, "values": result}, ensure_ascii=False).encode("utf-8")) + 1 > MAX_PREFERENCE_BYTES:
        raise ValueError("桌面偏好超过 256 KiB，已保留上次保存值。")
    return result


def read_preferences(path: Path, log=lambda message: None) -> dict[str, str]:
    try:
        if path.stat().st_size > MAX_PREFERENCE_BYTES:
            raise ValueError("偏好文件过大。")
        data = json.loads(path.read_text(encoding="utf-8"))
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("偏好版本不受支持。")
        return preferences_only(data["values"])
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError, KeyError):
        log("无法读取桌面偏好，使用默认值。")
        return {}


def write_preferences(path: Path, values: dict) -> None:
    content = json.dumps({"version": 1, "values": preferences_only(values)}, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def preference_script(origin: str, values: dict | None = None) -> str:
    keys = json.dumps(PREFERENCE_KEYS)
    guard = f"if(location.origin!=={json.dumps(origin)})throw new Error('页面来源已改变');"
    if values is not None:
        return ("(()=>{" + guard + f"const values={json.dumps(preferences_only(values))};for(const key of {keys})"
                "{if(Object.hasOwn(values,key))localStorage.setItem(key,values[key]);else localStorage.removeItem(key)}return true})()")
    return ("(()=>{" + guard + f"const values={{}};for(const key of {keys}){{const v=localStorage.getItem(key);"
            f"if(v!==null){{if(v.length>{MAX_PREFERENCE_BYTES})throw new Error('桌面偏好过大');values[key]=v}}}}return values}})()")


def evaluate_json(window, expression):
    # run_js executes native script directly; evaluate_js wraps eval and violates CSP.
    script = "JSON.stringify((()=>{try{return {value:(" + expression + ")}}catch(error){return {error:String(error)}}})())"
    # GTK passes a character count as WebKit byte length; keep injected text ASCII.
    script = "".join(char if ord(char) < 128 else json.dumps(char)[1:-1] for char in script)
    raw = window.run_js(script)
    if not isinstance(raw, str):
        raise RuntimeError("页面脚本没有返回有效结果。")
    value = json.loads(raw)
    if "error" in value:
        raise RuntimeError(value["error"])
    return value.get("value")


class DesktopLog:
    def __init__(self, filename: Path):
        self.filename = filename
        self.lock = threading.Lock()
        filename.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def __call__(self, message: str) -> None:
        try:
            with self.lock:
                if self.filename.exists() and self.filename.stat().st_size > 5 * 1024 * 1024:
                    os.replace(self.filename, self.filename.with_suffix(".log.1"))
                fd = os.open(self.filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}\n")
        except OSError:
            pass


def read_instance_frame(connection, deadline):
    frame = bytearray()
    while len(frame) <= 128:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        connection.settimeout(remaining)
        part = connection.recv(129 - len(frame))
        if not part:
            return bytes(frame)
        frame.extend(part)
    raise ValueError("桌面控制消息过长。")


class SingleInstance:
    """An OS lock owns state and protects the loopback command credential."""
    def __init__(self, state_dir: Path, wake=lambda: None, open_share=lambda _id: b"unavailable\n"):
        self.state_dir, self.wake, self.open_share = state_dir, wake, open_share
        self.file = None
        self.listener = None
        self.stopped = threading.Event()
        self._close_lock = threading.Lock()
        self._locked = False
        self._nonce = b""

    def _try_lock(self) -> bool:
        if self.file is None:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self.state_dir / "native-instance.lock"
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            self.file = os.fdopen(os.open(path, flags, 0o600), "r+b", buffering=0)
            if os.name != "nt":
                info = os.fstat(self.file.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                    raise PermissionError("桌面实例锁必须是当前用户的常规文件。")
                if stat.S_IMODE(info.st_mode) != 0o600:
                    os.fchmod(self.file.fileno(), 0o600)
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            return False
        self._locked = True
        # The first byte is locked on Windows. Clear old endpoint metadata before publishing a new listener.
        self.file.truncate(1)
        return True

    def _release_lock(self):
        if self.file:
            try:
                if self._locked:
                    self.file.truncate(1)
            finally:
                self.file.close()
                self.file = None
                self._locked = False

    def _request(self, command, deadline):
        try:
            self.file.seek(1)
            value = json.loads(self.file.read(1024))
            port, pid = value["port"], value["pid"]
            if type(port) is not int or not 0 < port < 65536 or type(pid) is not int or pid <= 0:
                return None
            legacy = set(value) == {"pid", "port"}
            nonce = value.get("nonce")
            if not legacy and (not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{64}", nonce) is None):
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            with socket.create_connection(("127.0.0.1", port), timeout=min(remaining, .5)) as connection:
                if legacy:
                    if command == b"wake":
                        connection.sendall(b"wake\n")
                        return b"ok\n"
                    return b"legacy\n"
                connection.sendall(b"WC1 " + nonce.encode("ascii") + b" " + command + b"\n")
                connection.shutdown(socket.SHUT_WR)
                return read_instance_frame(connection, min(deadline, time.monotonic() + 1))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def acquire(self) -> bool:
        deadline = time.monotonic() + 3
        try:
            while time.monotonic() < deadline:
                if self._try_lock():
                    self.listener = socket.socket()
                    self.listener.bind(("127.0.0.1", 0))
                    self.listener.listen(8)
                    self.listener.settimeout(.2)
                    self._nonce = secrets.token_hex(32).encode("ascii")
                    self.file.seek(1)
                    self.file.write(json.dumps({"pid": os.getpid(), "port": self.listener.getsockname()[1],
                                                "nonce": self._nonce.decode("ascii")}).encode())
                    threading.Thread(target=self._listen, daemon=True).start()
                    return True
                if self._request(b"wake", deadline) == b"ok\n":
                    return False
                time.sleep(.1)
            raise RuntimeError("另一个 WebCompare 窗口正在启动，请稍后重试。")
        except BaseException:
            self.close()
            raise

    def _listen(self):
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
                with connection:
                    request = INSTANCE_REQUEST.fullmatch(read_instance_frame(connection, time.monotonic() + .5))
                    if not request or not secrets.compare_digest(request[1], self._nonce):
                        continue
                    try:
                        if request[3] is None:
                            self.wake()
                            response = b"ok\n"
                        else:
                            response = self.open_share(request[3].decode("ascii"))
                    except Exception:
                        response = b"unavailable\n"
                    connection.sendall(response)
            except (OSError, ValueError):
                pass

    def close(self):
        with self._close_lock:
            self.stopped.set()
            if self.listener:
                self.listener.close()
            self._release_lock()


def desktop_command(state_dir: Path | None = None):
    if getattr(sys, "frozen", False):
        preferred = preferred_portable(state_dir / "backend" / "updates", package_kind(), __version__) if state_dir else None
        return [str(preferred) if preferred else sys.executable]
    if os.name == "nt":
        local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        installed = local_app_data / "Programs" / "WebCompare" / "WebCompare.exe"
        launcher = str(installed) if installed.is_file() else shutil.which("WebCompare.exe")
    else:
        launcher = shutil.which("webcompare-desktop")
        if launcher is None and Path("/usr/bin/webcompare-desktop").is_file():
            launcher = "/usr/bin/webcompare-desktop"
    if launcher:
        return [launcher]
    entry = Path(__file__).resolve().parents[1] / "desktop" / "native_entry.py"
    if entry.is_file():
        return [sys.executable, str(entry)]
    raise RuntimeError("找不到 WebCompare 桌面版，请安装后使用分享 ID 打开。")


def open_desktop_share(state_dir: Path, share_id: str) -> None:
    """Ask the desktop instance for this profile to open a saved share."""
    if not isinstance(share_id, str) or SHARE_ID.fullmatch(share_id) is None:
        raise ValueError("分享 ID 必须是 32 位小写十六进制字符。")
    state_dir = state_dir.expanduser().resolve()
    instance = SingleInstance(state_dir)
    deadline = time.monotonic() + INSTANCE_TIMEOUT
    child = None
    try:
        while time.monotonic() < deadline:
            if instance._try_lock():
                if child is None:
                    command = [*desktop_command(state_dir), "--user-data-dir", str(state_dir)]
                    child = launch_updated_process(command)
                instance._release_lock()
                if child.poll() not in (None, 0):
                    raise RuntimeError("WebCompare 桌面窗口启动失败，请查看桌面日志。")
            else:
                reply = instance._request(b"open " + share_id.encode("ascii"), deadline)
                if reply == b"ok\n":
                    return
                if reply == b"legacy\n":
                    raise RuntimeError("当前 WebCompare 窗口版本较旧，请关闭它并用新版打开分享。")
                if reply == b"unavailable\n":
                    raise RuntimeError("WebCompare 桌面窗口当前无法打开分享，请查看桌面日志。")
            time.sleep(.1)
        raise RuntimeError("等待 WebCompare 桌面窗口接受分享请求超时，请查看桌面日志。")
    finally:
        instance.close()


class Backend:
    def __init__(self, data_dir: Path, log=lambda message: None, *, startup_timeout=30, shutdown_timeout=6):
        self.data_dir, self.log = data_dir, log
        self.startup_timeout, self.shutdown_timeout = startup_timeout, shutdown_timeout
        self.origin = None
        self.process = None
        self._token = secrets.token_hex(32)
        self._stopping = threading.Event()
        self._stop_lock = threading.Lock()
        self.on_exit = lambda: None
        self.on_install = lambda *_update: None

    def request_headers(self, url: str, headers: dict) -> dict:
        return authenticated_headers(headers, url, self.origin, self._token)

    def _log(self, message):
        self.log(str(message).replace(self._token, "[REDACTED]"))

    def start(self):
        if self.process:
            raise RuntimeError("本地服务已经启动。")
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = Path(__file__).resolve().parents[1]
        command = ([sys.executable, "--backend"] if getattr(sys, "frozen", False) else
                   [sys.executable, str(root / "desktop" / "native_entry.py"), "--backend"])
        env = {name: value for name, value in os.environ.items()
               if not name.startswith(("WEBCOMPARE_", "_WEBCOMPARE_")) and name not in {"PYTHONHOME", "PYTHONPATH"}}
        env.update(WEBCOMPARE_DESKTOP_TOKEN=self._token, PYTHONUNBUFFERED="1")
        self.process = subprocess.Popen([*command, "--data-dir", str(self.data_dir)], cwd=root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        ready = queue.Queue()

        def read(stream, stdout=False):
            while True:
                line = stream.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    while line and not line.endswith("\n"):
                        line = stream.readline(65537)
                    self._log("后端日志行过长，已省略。")
                    if stdout:
                        ready.put(ValueError("本地服务的启动信息过长。"))
                    continue
                if stdout:
                    if line.startswith(INSTALL_MESSAGE):
                        try:
                            self.on_install(*parse_install_message(line.rstrip("\r\n"), self._token))
                        except (UpdateError, ValueError, TypeError, OSError, RuntimeError, subprocess.SubprocessError):
                            self._log("无法准备更新安装。")
                            try:
                                self.process.stdin.write("update-failed\n")
                                self.process.stdin.flush()
                            except (OSError, ValueError):
                                pass
                        continue
                    try:
                        origin = parse_ready(line.rstrip("\r\n"), self.process.pid)
                        if origin:
                            ready.put(origin)
                            continue
                    except ValueError as error:
                        ready.put(error)
                        continue
                self._log("后端：" + line.rstrip("\r\n"))
            if stdout:
                ready.put(RuntimeError("本地服务在启动或运行时退出。"))
                if not self._stopping.is_set():
                    self._terminate_tree(force=True)
                    self.on_exit()

        threading.Thread(target=read, args=(self.process.stderr,), daemon=True).start()
        threading.Thread(target=read, args=(self.process.stdout, True), daemon=True).start()
        deadline = time.monotonic() + self.startup_timeout
        try:
            value = ready.get(timeout=self.startup_timeout)
            if isinstance(value, Exception):
                raise value
            self.origin = value
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("本地服务已经退出。")
                try:
                    parsed = urlsplit(self.origin)
                    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=1)
                    try:
                        connection.request("GET", "/api/health", headers={HEADER: self._token})
                        if connection.getresponse().status == 200:
                            self._log(f"本地服务已就绪，PID={self.process.pid}。")
                            return self.origin
                    finally:
                        connection.close()
                except OSError:
                    pass
                time.sleep(.1)
            raise RuntimeError("本地服务未通过健康检查，请查看桌面日志。")
        except queue.Empty:
            self.stop()
            raise RuntimeError("本地服务启动超时，请查看桌面日志。") from None
        except BaseException:
            self.stop()
            raise

    def _terminate_tree(self, force=False):
        if not self.process:
            return
        try:
            if os.name == "nt":
                if self.process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=5)
            else:
                os.killpg(self.process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def stop(self):
        with self._stop_lock:
            self._stopping.set()
            if not self.process:
                return
            if self.process.poll() is None:
                try:
                    self.process.stdin.write("shutdown\n")
                    self.process.stdin.flush()
                    self.process.stdin.close()
                except (OSError, ValueError):
                    pass
                try:
                    self.process.wait(timeout=self.shutdown_timeout)
                except subprocess.TimeoutExpired:
                    self._log("本地服务停止超时，回收本次拥有的进程。")
                    self._terminate_tree()
                    try:
                        self.process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        self._terminate_tree(force=True)
                        self.process.wait(timeout=3)
            self._terminate_tree(force=True)


def find_gtk_webview(native):
    if hasattr(native, "get_context") and hasattr(native, "get_user_content_manager"):
        return native
    if hasattr(native, "get_children"):
        for child in native.get_children():
            found = find_gtk_webview(child)
            if found is not None:
                return found
    return None


def status_html(title: str, detail: str) -> str:
    return ("<!doctype html><html lang=zh-CN><meta charset=utf-8>"
            "<meta http-equiv=Content-Security-Policy content=\"default-src 'none'; style-src 'unsafe-inline'\">"
            "<style>html{background:#111;color:#eee;font:16px system-ui}body{max-width:680px;margin:15vh auto;padding:28px}"
            "h1{font-size:26px;color:#83b5ff}p{white-space:pre-wrap;line-height:1.8;overflow-wrap:anywhere}</style>"
            f"<body><h1>{html.escape(title)}</h1><p>{html.escape(detail)}</p></body></html>")


def stream_download(uri, origin, headers, choose_destination, cancel, register=lambda connection, sock: None,
                    unregister=lambda connection: None):
    """Save only a complete same-origin response; existing files survive failure/cancel."""
    from email.message import Message
    if not is_backend_url(uri, origin):
        raise ValueError("已阻止不受信任的下载地址。")
    if cancel.is_set():
        return False
    connection = http.client.HTTPConnection("127.0.0.1", urlsplit(origin).port, timeout=5)
    temporary = None
    cancel_socket = None
    try:
        connection.connect()
        # HTTP/1.0 closes the connection's wrapper while its response still reads.
        cancel_socket = connection.sock.dup()
        register(connection, cancel_socket)
        if cancel.is_set():
            return False
        target = urlsplit(uri)
        connection.request("GET", target.path + ("?" + target.query if target.query else ""), headers=headers)
        response = connection.getresponse()
        # Redirects are deliberately not followed with a private credential.
        if response.status != 200:
            raise RuntimeError("下载未获授权、已重定向或文件尚未准备好。")
        expected = response.getheader("Content-Length")
        expected = int(expected) if expected is not None else None
        if expected is not None and expected < 0:
            raise ValueError("下载长度无效。")
        disposition = Message()
        disposition["content-disposition"] = response.getheader("Content-Disposition", "")
        destination = choose_destination(Path(disposition.get_filename() or "webcompare-download").name)
        if destination is None or cancel.is_set():
            return False
        destination = Path(destination)
        temporary = destination.with_name(f".{destination.name}.{secrets.token_hex(6)}.part")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        count = 0
        with os.fdopen(fd, "wb") as stream:
            while not cancel.is_set():
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                count += len(chunk)
                stream.write(chunk)
        if cancel.is_set():
            return False
        if expected is not None and count != expected:
            raise RuntimeError("文件传输不完整，请重新下载。")
        os.replace(temporary, destination)
        return True
    except OSError:
        if cancel.is_set():
            return False
        raise
    finally:
        unregister(connection)
        connection.close()
        if cancel_socket is not None:
            cancel_socket.close()
        if temporary:
            temporary.unlink(missing_ok=True)


class NativeDesktop:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.log = DesktopLog(state_dir / "logs" / "native-desktop.log")
        self.preferences_path = state_dir / "preferences.json"
        self.backend = Backend(state_dir / "backend", self.log)
        self.backend.on_exit = self._backend_exited
        self.backend.on_install = self._request_install
        self.update_root = state_dir / "backend" / "updates"
        self.update_command = None
        self.window = None
        self.phase = "starting"
        self.closed = threading.Event()
        self.ready = threading.Event()
        self._shutdown_started = False
        self._context_factories = {}
        self._contexts = []
        self._download_cancel = threading.Event()
        self._download_lock = threading.Lock()
        self._downloads = {}
        self._download_errors = []
        self._engine_error = None
        self.instance = SingleInstance(state_dir, self.wake, self._open_share)

    def wake(self):
        if self.window and self.window.events.shown.is_set():
            self.window.restore()
            self.window.show()
            # The WinForms implementation of show() already activates the form on its UI thread.
            if sys.platform == "linux":
                from gi.repository import GLib
                GLib.idle_add(self.window.native.present)

    def _open_share(self, share_id):
        if self.phase in {"error", "stopping"} or self._shutdown_started:
            return b"unavailable\n"
        if not self.ready.is_set():
            return b"starting\n"
        self.window.load_url(f"{self.backend.origin}/s/{share_id}")
        self.wake()
        return b"ok\n"

    def _backend_exited(self):
        if self.phase not in {"ready", "restoring", "loading"}:
            return
        self.phase = "error"
        self.log("本地服务意外退出。")
        self.window.load_html(status_html("本地服务意外退出", f"请关闭窗口后重新打开。\n日志位置：{self.log.filename}"), base_uri="about:blank")

    def _loaded(self):
        try:
            if not is_backend_url(self.window.get_current_url(), self.backend.origin):
                return
            if self.phase == "restoring":
                evaluate_json(self.window, preference_script(self.backend.origin, read_preferences(self.preferences_path, self.log)))
                self.phase = "loading"
                self.window.load_url(self.backend.origin + "/")
            elif self.phase == "loading":
                self.phase = "ready"
                self.ready.set()
                try:
                    activate_portable(self.update_root, package_kind(), __version__)
                except (OSError, ValueError, TypeError, RuntimeError, UpdateError):
                    self.log("未能保存新版便携程序的位置。")
        except Exception as error:
            self.log(f"恢复桌面偏好失败：{error}")
            self.phase = "error"
            self.window.load_html(status_html("无法载入 WebCompare", f"请关闭后重试。\n日志位置：{self.log.filename}"), base_uri="about:blank")

    def save_preferences(self):
        if self.phase != "ready":
            return
        captured = queue.Queue()
        def capture():
            try:
                if not is_backend_url(self.window.get_current_url(), self.backend.origin):
                    captured.put(None)
                    return
                value = evaluate_json(self.window, preference_script(self.backend.origin))
                captured.put(value if is_backend_url(self.window.get_current_url(), self.backend.origin) else None)
            except Exception as error:
                captured.put(error)
        threading.Thread(target=capture, daemon=True).start()
        try:
            values = captured.get(timeout=.8)
        except queue.Empty:
            raise RuntimeError("读取桌面偏好超时，已保留上次保存值。") from None
        if isinstance(values, Exception):
            raise values
        if values is not None:
            write_preferences(self.preferences_path, values)

    def _closing(self):
        if self.closed.is_set():
            return
        if not self._shutdown_started:
            self._shutdown_started = True
            threading.Thread(target=self.shutdown, daemon=True).start()
        return False

    def _request_install(self, package: str, version: str, path: Path):
        if (self._shutdown_started or self.phase != "ready" or package != package_kind()
                or version_key(version) <= version_key(__version__)):
            raise UpdateError("当前窗口无法启动更新。")
        if package in PORTABLE:
            expected = prepared_portable(self.update_root, package, version)
        else:
            expected = self.update_root / "downloads" / PACKAGE_NAMES[package].format(version=version)
        if (path != expected or not path.is_file() or path.is_symlink()
                or not path.resolve().is_relative_to(self.update_root.resolve())):
            raise UpdateError("更新文件不在桌面更新目录中。")
        self.update_command = installer_command(package, path, self.state_dir)
        self._closing()

    def shutdown(self):
        try:
            try:
                self.save_preferences()
            except Exception as error:
                self.log(f"保存桌面偏好失败：{error}")
            self.phase = "stopping"
            self._close_downloads()
            self.backend.stop()
        finally:
            self.instance.close()
            self.closed.set()
            if self.window:
                self.window.destroy()

    def _install_gtk_context_factories(self):
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("WebKit2", "4.1")
        gi.require_version("Soup", "3.0")
        from gi.repository import GLib, WebKit2
        directory = Path(__file__).resolve().parent / "native"
        def wrap(factory):
            def create(*args, **kwargs):
                context = factory(*args, **kwargs)
                if context not in self._contexts:
                    # WebView construction can spawn a process, before before_show.
                    context.set_sandbox_enabled(True)
                    if not context.get_sandbox_enabled():
                        raise RuntimeError("系统 WebKit 无法启用进程隔离。")
                    context.add_path_to_sandbox(str(directory), True)
                    context.set_web_extensions_directory(str(directory))
                    context.set_web_extensions_initialization_user_data(
                        GLib.Variant("(ss)", (self.backend.origin, self.backend._token)))
                    self._contexts.append(context)
                return context
            return create
        for name in ("new_ephemeral", "new_with_website_data_manager", "get_default"):
            original = getattr(WebKit2.WebContext, name)
            self._context_factories[name] = original
            setattr(WebKit2.WebContext, name, staticmethod(wrap(original)))

    def _restore_gtk_context_factories(self):
        if self._context_factories:
            from gi.repository import WebKit2
            for name, original in self._context_factories.items():
                setattr(WebKit2.WebContext, name, original)
            self._context_factories.clear()

    def _engine_initialized(self, renderer):
        expected = "edgechromium" if sys.platform == "win32" else "gtkwebkit2"
        if renderer != expected:
            self._engine_error = RuntimeError("需要安装 Microsoft Edge WebView2 Runtime。" if sys.platform == "win32"
                else "需要安装 GTK3 / WebKitGTK 4.1 运行库。")
            return False

    def _gtk_setup(self):
        view = find_gtk_webview(self.window.native)
        if view is None:
            raise RuntimeError("无法取得 GTK WebKit 视图。")
        self.native_view = view
        context = view.get_context()
        if context not in self._contexts or not context.get_sandbox_enabled():
            raise RuntimeError("原生请求扩展未在 WebKit 进程创建前配置。")
        from webview.platforms.gtk import BrowserView
        browser = BrowserView.instances[self.window.uid]
        view.disconnect_by_func(browser.on_navigation)
        view.connect("decide-policy", self._gtk_policy)
        view.connect("permission-request", lambda _view, request: (request.deny(), True)[1])
        view.connect("enter-fullscreen", lambda _view: (self.window.native.fullscreen(), True)[1])
        view.connect("leave-fullscreen", lambda _view: (self.window.native.unfullscreen(), True)[1])
        settings = view.get_settings()
        settings.set_property("allow-file-access-from-file-urls", False)
        settings.set_property("allow-universal-access-from-file-urls", False)
        settings.set_property("enable-media-stream", False)
        context.connect("download-started", self._gtk_download)

    def _gtk_policy(self, view, decision, kind):
        from gi.repository import WebKit2
        if kind in {WebKit2.PolicyDecisionType.NAVIGATION_ACTION, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION}:
            action = decision.get_navigation_action()
            uri = action.get_request().get_uri()
            if is_backend_url(uri, self.backend.origin):
                if kind == WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION or action.get_frame_name() == "_blank":
                    decision.ignore()
                    self.window.load_url(uri)
                else:
                    decision.use()
            elif uri.startswith("blob:") and allowed_download(uri, self.backend.origin) and is_backend_url(view.get_uri(), self.backend.origin):
                decision.download()
            elif uri == "about:blank" and self.phase == "error":
                decision.use()
            else:
                decision.ignore()
                self.log("已阻止不受信任的导航。")
            return True
        if kind == WebKit2.PolicyDecisionType.RESPONSE:
            if not allowed_download(decision.get_request().get_uri(), self.backend.origin):
                decision.ignore()
            elif not decision.is_mime_type_supported():
                decision.download()
            else:
                decision.use()
            return True
        return False

    def _gtk_choose_destination(self, suggested):
        test_directory = getattr(self, "_smoke_download_dir", None)
        if test_directory:
            return test_directory / Path(suggested).name
        from gi.repository import GLib, Gtk
        dialog = Gtk.FileChooserDialog(title="保存文件", transient_for=self.window.native,
            action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons("取消", Gtk.ResponseType.CANCEL, "保存", Gtk.ResponseType.ACCEPT)
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_destroy_with_parent(True)
        dialog.set_current_name(Path(suggested).name or "download")
        folder = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD)
        if folder:
            dialog.set_current_folder(folder)
        try:
            return Path(dialog.get_filename()) if dialog.run() == Gtk.ResponseType.ACCEPT else None
        finally:
            dialog.destroy()

    def _gtk_download(self, _context, download):
        uri = download.get_request().get_uri()
        if not allowed_download(uri, self.backend.origin):
            download.cancel()
            self.log("已阻止不受信任的下载。")
            return
        if is_backend_url(uri, self.backend.origin):
            # WebKit's download IPC bypasses WebPage::send-request. Keep auth native.
            download.cancel()
            manager = self.native_view.get_context().get_cookie_manager()
            def cookies_ready(cookie_manager, result):
                try:
                    cookies = cookie_manager.get_cookies_finish(result)
                    header = "; ".join(cookie.get_name() + "=" + cookie.get_value() for cookie in cookies)
                    threading.Thread(target=self._gtk_stream_download, args=(uri, header), daemon=True).start()
                except Exception:
                    self._download_failed("无法读取下载会话，请重新下载。")
            manager.get_cookies(uri, None, cookies_ready)
            return
        def destination(item, suggested):
            path = self._gtk_choose_destination(suggested)
            if path:
                item.set_destination(path.resolve().as_uri())
            else:
                item.cancel()
            return True
        download.connect("decide-destination", destination)

    def _download_failed(self, message):
        self.log(message)
        self._download_errors.append(message)
        if self._download_cancel.is_set() or getattr(self, "_smoke_download_dir", None):
            return
        from gi.repository import GLib, Gtk
        def show():
            if self.closed.is_set():
                return False
            dialog = Gtk.MessageDialog(transient_for=self.window.native, destroy_with_parent=True,
                message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE, text="文件下载失败")
            dialog.format_secondary_text(message)
            dialog.connect("response", lambda item, _response: item.destroy())
            dialog.show()
            return False
        GLib.idle_add(show)

    def _close_downloads(self):
        self._download_cancel.set()
        with self._download_lock:
            active = list(self._downloads.items())
        for connection, (sock, _thread) in active:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        deadline = time.monotonic() + 3
        for _connection, (_sock, thread) in active:
            if thread is not threading.current_thread():
                thread.join(max(0, deadline - time.monotonic()))

    def _gtk_stream_download(self, uri, cookies):
        from gi.repository import GLib
        def choose_destination(suggested):
            selected = []
            done = threading.Event()
            def choose():
                try:
                    if not self._download_cancel.is_set():
                        selected.append(self._gtk_choose_destination(suggested))
                finally:
                    done.set()
                return False
            GLib.idle_add(choose)
            while not done.wait(.1):
                if self._download_cancel.is_set():
                    return None
            return selected[0] if selected else None
        def register(connection, sock):
            with self._download_lock:
                self._downloads[connection] = (sock, threading.current_thread())
        def unregister(connection):
            with self._download_lock:
                self._downloads.pop(connection, None)
        try:
            if stream_download(uri, self.backend.origin, self.backend.request_headers(uri, {"Cookie": cookies}),
                               choose_destination, self._download_cancel, register, unregister):
                self.log("文件下载完成。")
        except (OSError, ValueError, RuntimeError, http.client.HTTPException) as error:
            self._download_failed(str(error) if isinstance(error, RuntimeError) else "文件下载中断，请重新下载。")

    def _windows_setup(self):
        control = self.window.native.webview
        control.NavigationStarting += self._windows_navigation
        def initialized(_sender, args):
            if not args.IsSuccess:
                return
            from Microsoft.Web.WebView2.Core import (CoreWebView2PermissionState, CoreWebView2WebResourceContext,
                CoreWebView2WebResourceRequestSourceKinds)
            core = control.CoreWebView2
            browser = self.window.native.browser
            try:
                # pywebview registers the older overload, which misses some request sources.
                core.RemoveWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All)
                core.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All,
                    CoreWebView2WebResourceRequestSourceKinds.All)
            except Exception as error:
                self.log(f"无法设置 WebView2 请求过滤器：{error}")
                self._engine_error = RuntimeError("无法启用请求保护，请更新 Microsoft Edge WebView2 Runtime。")
                self.phase = "error"
                browser.load_html(status_html("无法载入 WebCompare", str(self._engine_error)), "about:blank")
                return
            core.NewWindowRequested -= browser.on_new_window_request
            core.NewWindowRequested += self._windows_new_window
            core.FrameNavigationStarting += self._windows_navigation
            core.WebResourceRequested += self._windows_request
            core.DownloadStarting -= browser.on_download_starting
            def download(sender, event):
                if not allowed_download(str(event.DownloadOperation.Uri), self.backend.origin):
                    event.Cancel = True
                elif getattr(self, "_smoke_download_dir", None):
                    event.ResultFilePath = str(self._smoke_download_dir / Path(str(event.ResultFilePath)).name)
                else:
                    browser.on_download_starting(sender, event)
            core.DownloadStarting += download
            def permission(_sender, event):
                event.State = CoreWebView2PermissionState.Deny
                event.Handled = True
            core.PermissionRequested += permission
            def fullscreen(_sender, _event):
                if bool(core.ContainsFullScreenElement) != bool(self.window.native.is_fullscreen):
                    self.window.toggle_fullscreen()
            core.ContainsFullScreenElementChanged += fullscreen
            browser.load_url(self.backend.origin + "/api/health")
        control.CoreWebView2InitializationCompleted += initialized
        if control.CoreWebView2 is not None:
            initialized(control, type("Ready", (), {"IsSuccess": True})())

    def _windows_navigation(self, _sender, event):
        uri = str(event.Uri)
        if uri == "about:blank" and self.phase in {"restoring", "error"}:
            return
        if not is_backend_url(uri, self.backend.origin):
            event.Cancel = True
            self.log("已阻止不受信任的导航。")

    def _windows_new_window(self, _sender, event):
        event.Handled = True
        uri = str(event.Uri)
        if is_backend_url(uri, self.backend.origin):
            self.window.load_url(uri)

    def _windows_request(self, _sender, event):
        request = event.Request
        request.Headers.RemoveHeader(HEADER)
        if is_backend_url(str(request.Uri), self.backend.origin):
            request.Headers.SetHeader(HEADER, self.backend._token)

    def run(self, started=None):
        if not self.instance.acquire():
            self.instance.close()
            return 0
        try:
            if sys.platform not in {"linux", "win32"}:
                raise RuntimeError("桌面版目前支持 Windows 和 Linux。")
            if sys.platform == "linux" and not (Path(__file__).parent / "native" / "libwebcompare-webkit.so").is_file():
                raise RuntimeError("缺少 Linux 原生请求扩展，请重新安装完整桌面包。")
            self.backend.start()
            if sys.platform == "linux":
                self._install_gtk_context_factories()
            import webview
            webview.settings.update(ALLOW_DOWNLOADS=sys.platform == "win32", ALLOW_FILE_URLS=False,
                OPEN_EXTERNAL_LINKS_IN_BROWSER=False, OPEN_DEVTOOLS_IN_DEBUG=False, REMOTE_DEBUGGING_PORT=None)
            initial_url = None if sys.platform == "win32" else self.backend.origin + "/api/health"
            self.window = webview.create_window("WebCompare", initial_url, width=1440,
                height=960, min_size=(800, 600), background_color="#111111", text_select=True)
            self.phase = "restoring"
            self.window.events.initialized += self._engine_initialized
            self.window.events.before_show += self._windows_setup if sys.platform == "win32" else self._gtk_setup
            self.window.events.loaded += self._loaded
            self.window.events.closing += self._closing
            def gui_started():
                if started:
                    started(self)
            webview.start(gui_started, gui="edgechromium" if sys.platform == "win32" else "gtk", debug=False,
                private_mode=False, storage_path=str(self.state_dir / "webview"))
            if self._engine_error:
                raise self._engine_error
        finally:
            self.phase = "stopping"
            self._close_downloads()
            self.backend.stop()
            self.instance.close()
            self.closed.set()
            self._restore_gtk_context_factories()
        if self.update_command:
            launch_updated_process(self.update_command)
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="WebCompare 轻量桌面客户端")
    parser.add_argument("--user-data-dir", type=Path, default=default_user_data_dir(), help="独立桌面状态目录")
    parser.add_argument("--no-update-launch", action="store_true", help="从当前便携目录启动，跳过已安装的新版")
    parser.add_argument("--smoke-output", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    state_dir = args.user_data_dir.expanduser().resolve()
    try:
        if not args.no_update_launch and not args.smoke_output:
            preferred = preferred_portable(state_dir / "backend" / "updates", package_kind(), __version__)
            if preferred:
                try:
                    launch_updated_process(installer_command(package_kind(), preferred, state_dir))
                    return 0
                except OSError:
                    DesktopLog(state_dir / "logs" / "native-desktop.log")("新版启动失败，继续打开当前版本。")
        app = NativeDesktop(state_dir)
        if args.smoke_output:
            from .native_smoke import application_smoke
            app.run(lambda instance: application_smoke(instance, args.smoke_output))
            return 0 if json.loads(args.smoke_output.read_text(encoding="utf-8")).get("passed") else 1
        return app.run()
    except Exception as error:
        message = f"{error}\n日志位置：{state_dir / 'logs' / 'native-desktop.log'}"
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "无法启动 WebCompare", 0x10)
        elif sys.platform == "linux":
            try:
                import gi
                gi.require_version("Gtk", "3.0")
                from gi.repository import Gtk
                dialog = Gtk.MessageDialog(message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
                                           text="无法启动 WebCompare")
                dialog.format_secondary_text(message)
                dialog.run()
                dialog.destroy()
            except Exception:
                print("无法启动 WebCompare：" + message, file=sys.stderr)
        return 2

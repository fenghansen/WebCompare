"""Official releases and per-profile native update jobs."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
from http.client import HTTPException
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zipfile

from . import __version__

RELEASE_API = "https://api.github.com/repos/fenghansen/WebCompare/releases/latest"
DOWNLOAD = "https://github.com/fenghansen/WebCompare/releases/download/"
CDN = {"release-assets.githubusercontent.com", "objects.githubusercontent.com"}
PACKAGE_NAMES = {
    "windows-zip": "webcompare-desktop-{version}-windows-x64.zip",
    "windows-setup": "webcompare-desktop-{version}-windows-x64-setup.exe",
    "linux-tar": "webcompare-desktop-{version}-linux-x64.tar.gz",
    "linux-deb": "webcompare-desktop_{version}_amd64.deb",
}
PORTABLE = {"windows-zip": "WebCompare.exe", "linux-tar": "webcompare-desktop"}
VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
DIGEST = re.compile(r"[a-f0-9]{64}")
HOUR = 3600
CHECK_RETRY_SECONDS = 10
INSTALL_MESSAGE = "WEBCOMPARE_DESKTOP_INSTALL "


class UpdateError(Exception):
    pass


def version_key(text: str):
    # Local builds may use the existing PEP 440 rc version convention.
    match = VERSION.fullmatch(text.split("+", 1)[0])
    if match:
        return (*map(int, match.groups()), True)
    parts = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)(?:a|b|rc|\.dev)[0-9]+", text)
    if parts and VERSION.fullmatch(parts[1]):
        return (*map(int, parts[1].split(".")), False)
    raise UpdateError("无法识别当前版本。")


class ReleaseRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            before, after = urlsplit(req.full_url), urlsplit(newurl)
            allowed = (before.hostname != "api.github.com" and after.scheme == "https"
                       and after.netloc in CDN and not after.fragment)
        except ValueError:
            allowed = False
        if not allowed:
            raise UpdateError("发行附件的下载目的地无效。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def official_open(url: str):
    # A fresh request carries no WebCompare headers, cookies, or credentials.
    return build_opener(ReleaseRedirect()).open(Request(url, headers={
        "Accept": "application/vnd.github+json" if url == RELEASE_API else "application/octet-stream",
        "User-Agent": "WebCompare-Update",
    }), timeout=15)


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Release:
    version: str
    tag: str
    notes: str
    assets: list

    def asset(self, name: str) -> Asset:
        matches = [item for item in self.assets if isinstance(item, dict) and item.get("name") == name]
        if len(matches) != 1:
            raise UpdateError("此版本缺少当前安装方式所需的发行附件。")
        item = matches[0]
        url = DOWNLOAD + f"{self.tag}/{name}"
        digest = item.get("digest")
        size = item.get("size")
        if (item.get("state") != "uploaded" or item.get("browser_download_url") != url
                or type(size) is not int or not 0 < size < 2**31
                or not isinstance(digest, str) or not digest.startswith("sha256:")
                or not DIGEST.fullmatch(digest[7:])):
            raise UpdateError("发行附件缺少有效的大小或 SHA-256。")
        return Asset(name, url, size, digest[7:])


def load_release() -> Release | None:
    try:
        with official_open(RELEASE_API) as result:
            raw = result.read(2 * 1024**2 + 1)
        if len(raw) > 2 * 1024**2:
            raise UpdateError("发行版本信息过大。")
        value = json.loads(raw)
    except HTTPError as error:
        error.close()
        if error.code == 404:
            return None
        raise UpdateError("GitHub 暂未提供版本信息，请稍后再试。") from None
    except (OSError, ValueError, HTTPException):
        raise UpdateError("暂时无法读取官方发行版本。") from None
    if not isinstance(value, dict) or value.get("draft") is not False or value.get("prerelease") is not False:
        raise UpdateError("未收到公开稳定版的信息。")
    tag = value.get("tag_name")
    assets = value.get("assets")
    if not isinstance(tag, str) or not VERSION.fullmatch(tag.removeprefix("v")) or not isinstance(assets, list):
        raise UpdateError("官方发行版本信息格式不受支持。")
    notes = value.get("body")
    return Release(tag.removeprefix("v"), tag, notes[:4000] if isinstance(notes, str) else "", assets)


_release_lock = threading.Lock()
_next_lookup = 0.0
_last_lookup = float("-inf")
_cached_release = None
_cached_error = ""


def latest_release(*, refresh=False) -> Release | None:
    global _next_lookup, _last_lookup, _cached_release, _cached_error
    with _release_lock:
        now = time.monotonic()
        if now >= _next_lookup or (refresh and now >= _last_lookup + CHECK_RETRY_SECONDS):
            try:
                _cached_release = load_release()
                _cached_error = ""
            except UpdateError as error:
                _cached_error = str(error)
            _last_lookup = time.monotonic()
            _next_lookup = _last_lookup + (CHECK_RETRY_SECONDS if _cached_error else HOUR)
        if _cached_error:
            raise UpdateError(_cached_error)
        return _cached_release


def latest_summary() -> dict:
    release = latest_release()
    phase = ("available" if version_key(release.version) > version_key(__version__) else "current") if release else "idle"
    return {"current": __version__, "latest": release.version if release else None,
            "notes": release.notes if release else "", "phase": phase}


def matching_file(path: Path, asset: Asset) -> bool:
    if path.is_symlink() or not path.is_file() or path.stat().st_size != asset.size:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == asset.sha256


def read_asset(asset: Asset, stream, progress=lambda _percent: None):
    size = 0
    digest = hashlib.sha256()
    with official_open(asset.url) as source:
        if source.status != 200:
            raise UpdateError("发行附件的下载响应无效。")
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > asset.size:
                raise UpdateError("下载大小与发行记录不一致。")
            digest.update(chunk)
            stream.write(chunk)
            progress(size * 100 // asset.size)
    if size != asset.size or digest.hexdigest() != asset.sha256:
        raise UpdateError("下载大小或 SHA-256 与发行记录不一致。")


def download_package(release: Release, asset: Asset, directory: Path, progress) -> Path:
    sums = release.asset("SHA256SUMS")
    if sums.size > 65536:
        raise UpdateError("发行校验清单过大。")
    content = io.BytesIO()
    read_asset(sums, content)
    matches = []
    for line in content.getvalue().decode("utf-8").splitlines():
        checksum, separator, name = line.partition(" ")
        if separator and name.strip().removeprefix("*") == asset.name:
            matches.append(checksum)
    if matches != [asset.sha256]:
        raise UpdateError("SHA256SUMS 与发行附件的摘要不一致。")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / asset.name
    if matching_file(destination, asset):
        return destination
    # Clean up only our own partial file. Interrupted copies never become installable.
    descriptor, name = tempfile.mkstemp(prefix=f".{asset.name}.", suffix=".part", dir=directory)
    partial = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            read_asset(asset, stream, progress)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def package_kind() -> str | None:
    if not getattr(sys, "frozen", False) or platform.machine().lower() not in {"amd64", "x86_64"}:
        return None
    location = Path(sys.executable).resolve().parent
    if not (location / "bundle-manifest.json").is_file():
        return None
    marker = location / ".webcompare-install-kind"
    try:
        if not marker.is_symlink() and marker.stat().st_size < 32:
            installed = marker.read_text(encoding="ascii").strip()
            if (sys.platform, installed) in {("linux", "linux-deb"), ("win32", "windows-setup")}:
                return installed
    except (OSError, UnicodeError):
        pass
    if sys.platform == "win32":
        installer = any(path.with_suffix(".dat").is_file() for path in location.glob("unins*.exe"))
        return "windows-setup" if installer else "windows-zip"
    if sys.platform == "linux":
        return "linux-deb" if location == Path("/opt/webcompare-desktop") else "linux-tar"
    return None


def portable_executable(directory: Path, version: str, package: str) -> Path:
    executable = directory / PORTABLE[package]
    manifest_path = directory / "bundle-manifest.json"
    if (not executable.is_file() or executable.is_symlink() or manifest_path.is_symlink()
            or manifest_path.stat().st_size > 2 * 1024**2):
        raise UpdateError("便携包缺少可启动的程序。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    target = "linux" if package == "linux-tar" else "win32"
    if (manifest.get("version"), manifest.get("platform"), manifest.get("architecture")) != (version, target, "x64"):
        raise UpdateError("便携包的版本或平台与发行记录不符。")
    return executable


def write_preference(path: Path, value: str):
    descriptor, filename = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    pending = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as stream:
            stream.write(value)
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)


def prepared_portable(root: Path, package: str, version: str) -> Path:
    directory = root / "versions" / version
    stamp = directory / ".update-sha256"
    if (directory.is_symlink() or stamp.is_symlink() or stamp.stat().st_size != 64
            or not DIGEST.fullmatch(stamp.read_text(encoding="ascii"))):
        raise UpdateError("便携更新目录尚未完成校验。")
    return portable_executable(directory, version, package)


def preferred_portable(root: Path, package: str | None, current: str) -> Path | None:
    if package not in PORTABLE:
        return None
    try:
        marker = root / "preferred-version"
        if marker.is_symlink() or marker.stat().st_size > 32:
            return None
        version = marker.read_text(encoding="ascii")
        if VERSION.fullmatch(version) and version_key(version) > version_key(current):
            path = prepared_portable(root, package, version)
            if path != Path(sys.executable).resolve() and path.resolve().is_relative_to(root.resolve()):
                return path
    except (OSError, ValueError, TypeError, RuntimeError, UpdateError):
        return None
    return None


def activate_portable(root: Path, package: str | None, version: str):
    if package not in PORTABLE or not VERSION.fullmatch(version):
        return
    path = root / "versions" / version / PORTABLE[package]
    if Path(sys.executable).resolve() == path and prepared_portable(root, package, version) == path:
        if preferred_portable(root, package, version) is None:
            write_preference(root / "preferred-version", version)


def unpack_portable(archive: Path, root: Path, version: str, package: str, digest: str) -> Path:
    top = f"webcompare-desktop-{version}"
    versions = root / "versions"
    versions.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = versions / version
    if target.exists() or target.is_symlink():
        stamp = target / ".update-sha256"
        if not target.is_symlink() and stamp.is_file() and stamp.read_text(encoding="ascii") == digest:
            return portable_executable(target, version, package)
        raise UpdateError("新版目录已有内容，请从发行页手动安装。")
    with tempfile.TemporaryDirectory(prefix=".unpack-", dir=versions) as stage:
        bundle = Path(stage) / top
        seen = set()
        total = 0

        def validate(name: str, size: int):
            nonlocal total
            path = PurePosixPath(name)
            if (not path.parts or path.parts[0] != top or ".." in path.parts or "\\" in name
                    or ":" in name or len(path.parts) > 40):
                raise UpdateError("便携包的文件路径无效。")
            if package == "windows-zip" and any(
                part.endswith((" ", ".")) or any(char in part for char in '<>"|?*')
                or re.fullmatch(r"(?i:con|nul|aux|prn|com[1-9]|lpt[1-9])", part.split(".")[0])
                for part in path.parts
            ):
                raise UpdateError("Windows 便携包的文件路径无效。")
            key = path.as_posix().casefold() if package == "windows-zip" else path.as_posix()
            total += size
            if key in seen or len(seen) >= 50000 or total > 4 * 1024**3:
                raise UpdateError("便携包的文件结构无效。")
            seen.add(key)

        if package == "linux-tar":
            def check_tar(member, destination):
                item = tarfile.data_filter(member, destination)
                if item.issym() or item.islnk():
                    base = Path(destination) / (PurePosixPath(item.name).parent if item.issym() else ".")
                    if not (base / item.linkname).resolve().is_relative_to(bundle):
                        raise UpdateError("便携包的链接离开了程序目录。")
                return item

            with tarfile.open(archive, "r:gz") as content:
                for member in content:
                    validate(member.name, member.size)
                content.extractall(stage, members=content.members, filter=check_tar)
        else:
            with zipfile.ZipFile(archive) as content:
                for info in content.infolist():
                    validate(info.filename, info.file_size)
                    if stat.S_ISLNK(info.external_attr >> 16):
                        raise UpdateError("Windows 便携包包含不受支持的链接。")
                content.extractall(stage)
        portable_executable(bundle, version, package)
        (bundle / ".update-sha256").write_text(digest, encoding="ascii")
        # The verified directory becomes visible only as a complete version.
        bundle.rename(target)
    return portable_executable(target, version, package)


def installer_command(package: str, path: Path, state_dir: Path) -> list[str]:
    if package in PORTABLE:
        return [str(path), "--user-data-dir", str(state_dir)]
    if package == "windows-setup":
        return [str(path)]
    if package == "linux-deb":
        opener, mime = shutil.which("xdg-open"), shutil.which("xdg-mime")
        try:
            if opener and mime:
                handler = subprocess.run([mime, "query", "default", "application/vnd.debian.binary-package"],
                                         env=launcher_environment(), capture_output=True, text=True,
                                         timeout=5, check=False)
                if handler.returncode == 0 and handler.stdout.strip():
                    return [opener, str(path)]
        except (OSError, subprocess.SubprocessError):
            pass
        raise UpdateError("请在终端用 sudo apt install 安装已验证的本地 DEB。")
    raise UpdateError("当前安装方式请从发行页手动更新。")


def launcher_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("WEBCOMPARE_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if getattr(sys, "frozen", False) and sys.platform == "linux":
        original = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    return env


def launch_updated_process(command: list[str], *, keep_streams=False):
    options = dict(env=launcher_environment())
    if not keep_streams:
        options.update(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        import ctypes

        set_dll_directory = ctypes.windll.kernel32.SetDllDirectoryW
        set_dll_directory.argtypes = [ctypes.c_wchar_p]
        if getattr(sys, "frozen", False):
            set_dll_directory(None)
        try:
            flags = 0 if keep_streams else subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            return subprocess.Popen(command, **options, creationflags=flags)
        finally:
            if getattr(sys, "frozen", False):
                set_dll_directory(str(sys._MEIPASS))
    else:
        return subprocess.Popen(command, **options, start_new_session=not keep_streams)


def install_message(token: str, package: str, version: str, path: Path) -> str:
    content = json.dumps([package, version, str(path)], ensure_ascii=True, separators=(",", ":"))
    signature = hmac.digest(token.encode(), content.encode(), "sha256").hex()
    return INSTALL_MESSAGE + signature + " " + content


def parse_install_message(line: str, token: str):
    signature, _, content = line.removeprefix(INSTALL_MESSAGE).partition(" ")
    expected = hmac.digest(token.encode(), content.encode(), "sha256").hex()
    if not DIGEST.fullmatch(signature) or not hmac.compare_digest(expected, signature):
        raise UpdateError("更新指令校验失败。")
    package, version, path = json.loads(content)
    if package not in PACKAGE_NAMES or not VERSION.fullmatch(version) or not Path(path).is_absolute():
        raise UpdateError("更新指令格式无效。")
    return package, version, Path(path)


class DesktopUpdater:
    def __init__(self, root: Path, package: str | None, install_ready, current=__version__):
        self.root, self.install_ready = root, install_ready
        self.current, self.package = current, package
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.release = None
        self.file = None
        self.preferences = root / "automatic"
        try:
            automatic = self.preferences.read_text(encoding="ascii").strip() != "0"
        except (OSError, UnicodeError):
            automatic = True
        self.state = dict(current=current, latest=None, phase="idle", automatic=automatic, package=package)
        if package is None:
            self.state.update(phase="error", error="当前运行方式请从发行页手动更新。")

    def snapshot(self):
        with self.lock:
            return self.state.copy()

    def start(self):
        def periodic():
            while not self.stopped.is_set():
                if self.snapshot()["automatic"]:
                    self.check(refresh=False)
                self.stopped.wait(HOUR)

        if self.package is not None:
            threading.Thread(target=periodic, daemon=True).start()

    def close(self):
        self.stopped.set()

    def set_automatic(self, value: bool):
        with self.lock:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_preference(self.preferences, "1" if value else "0")
            self.state["automatic"] = value
        return self.check(refresh=False) if value else self.snapshot()

    def _run(self, phase, job, allowed=None):
        with self.lock:
            before = self.state["phase"]
            if (self.stopped.is_set() or self.package is None
                    or before in {"checking", "downloading", "installing"}
                    or (allowed is not None and before not in allowed)):
                return self.snapshot()
            self.state.update(phase=phase)
            self.state.pop("error", None)
            self.state.pop("progress", None)
            self.state.pop("manual_path", None)
            threading.Thread(target=self._try_job, args=(job,), daemon=True).start()
            return self.snapshot()

    def _try_job(self, job):
        try:
            job()
        except UpdateError as error:
            with self.lock:
                self.state.update(phase="error", error=str(error))
        except (OSError, ValueError, TypeError, RuntimeError, EOFError, HTTPException, tarfile.TarError, zipfile.BadZipFile,
                subprocess.SubprocessError):
            with self.lock:
                self.state.update(phase="error", error="更新未完成，请重试或从发行页手动安装。")

    def check(self, *, refresh=True):
        return self._run("checking", lambda: self._check(refresh=refresh))

    def _check(self, *, refresh=False):
        release = latest_release(refresh=refresh)
        newer = release is not None and version_key(release.version) > version_key(self.current)
        with self.lock:
            self.release = release if newer else None
            self.file = None
            self.state.update(latest=release.version if release else None,
                              notes=release.notes if newer else "",
                              phase=("available" if newer else "current") if release else "idle")
            if newer and self.state["automatic"] and not self.stopped.is_set():
                self.state.update(phase="downloading", progress=0)
            else:
                return
        self._download()

    def download(self):
        return self._run("downloading", self._download, {"available", "error"})

    def _download(self):
        if self.release is None:
            raise UpdateError("请先检查更新。")
        asset = self.release.asset(PACKAGE_NAMES[self.package].format(version=self.release.version))

        def progress(percent):
            if self.stopped.is_set():
                raise UpdateError("桌面已关闭。")
            with self.lock:
                self.state["progress"] = percent

        path = download_package(self.release, asset, self.root / "downloads", progress)
        with self.lock:
            self.file = path
            self.state.update(phase="ready", progress=100)

    def install(self):
        return self._run("installing", self._install, {"ready"})

    def reject_install(self):
        with self.lock:
            if self.state["phase"] == "installing":
                self.state.update(phase="error", error="无法启动安装程序，请重试或手动安装。")

    def _install(self):
        release = self.release
        asset = release.asset(PACKAGE_NAMES[self.package].format(version=release.version))
        if self.file is None or not matching_file(self.file, asset):
            raise UpdateError("已下载的附件校验失败，请重新下载。")
        path = self.file
        if self.package in PORTABLE:
            path = unpack_portable(path, self.root, release.version, self.package, asset.sha256)
        else:
            try:
                installer_command(self.package, path, self.root.parent.parent)
            except UpdateError as error:
                if self.package != "linux-deb":
                    raise
                with self.lock:
                    self.state.update(phase="error", error=str(error), manual_path=str(path))
                return
        if not self.stopped.is_set():
            self.install_ready(self.package, release.version, path)

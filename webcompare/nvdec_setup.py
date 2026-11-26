"""本机 NVDEC 依赖资格及应用私有 wheel 安装。"""
from __future__ import annotations

import ctypes
import hashlib
import importlib
import os
import platform
import re
import struct
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from http.client import HTTPException
from importlib.metadata import version
from pathlib import Path
from ssl import SSLError
from threading import Lock
from time import monotonic
from urllib.error import URLError
from urllib.request import urlopen

PYAV_VERSION = "19.0.2"
_activation_lock = Lock()


@dataclass(frozen=True)
class _Wheel:
    url: str
    bytes: int
    sha256: str


# The official cp312-abi3 wheels are usable by the same service process, including frozen builds.
_WHEELS = {
    "linux": _Wheel(
        "https://files.pythonhosted.org/packages/e1/7f/9f1237feb894afa472ef834aa1fbfe4d6398f28dc7e86a1b418650204c36/"
        "av-19.0.2-cp312-abi3-manylinux_2_28_x86_64.whl",
        34_990_159, "9c1da7aad0fa6c13b4a5c1e7985be0d13f8526a96284573acaf3d55ead6d3fe8",
    ),
    "win32": _Wheel(
        "https://files.pythonhosted.org/packages/32/5a/743a83666ded197df8b9957e2800bef9a39d52b23be2ca211bbd14b5fb22/"
        "av-19.0.2-cp312-abi3-win_amd64.whl",
        28_150_949, "3ce5a1d7b916d13c16ac58f1756f0fee4af2d53f02bc1403e18550b50d375a22",
    ),
}


class NVDECInstallError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code = status, code


def _enabled() -> bool:
    return os.getenv("WEBCOMPARE_NVDEC", "auto").lower() not in {"0", "false", "off"}


@lru_cache(maxsize=1)
def _driver_available() -> bool:
    try:
        if sys.platform == "win32":
            load, libraries = ctypes.WinDLL, ("nvcuda.dll", "nvcuvid.dll")
            options = {"winmode": 0x800}
        elif sys.platform == "linux":
            load, libraries = ctypes.CDLL, ("libcuda.so.1", "libnvcuvid.so.1")
            options = {}
        else:
            return False
        # Resolve the host's libraries so the frozen app never bundles a build machine GPU driver.
        cuda, _video = (load(library, **options) for library in libraries)
        count = ctypes.c_int()
        return cuda.cuInit(0) == 0 and cuda.cuDeviceGetCount(ctypes.byref(count)) == 0 and count.value > 0
    except (AttributeError, OSError):
        return False


@lru_cache(maxsize=1)
def _pyav_available() -> bool:
    def supported(value: str) -> bool:
        release = re.fullmatch(r"19\.(\d+)\.(\d+)(?:\.post\d+)?(?:\+[a-zA-Z0-9.]+)?", value)
        return bool(release and (int(release[1]), int(release[2])) >= (0, 2))

    try:
        if not supported(version("av")):
            return False
        import av

        return supported(av.__version__) and any(
            config.device_type.name == "cuda" for config in av.Codec("h264", "r").hardware_configs
        )
    except (ImportError, ValueError, AttributeError, OSError, RuntimeError):
        return False


def nvdec_installed() -> bool:
    return _enabled() and _driver_available() and _pyav_available()


def _compatible_wheel() -> _Wheel | None:
    from importlib.machinery import EXTENSION_SUFFIXES

    if (sys.implementation.name != "cpython" or sys.version_info < (3, 12) or
            struct.calcsize("P") != 8 or platform.machine().lower() not in {"amd64", "x86_64"} or
            any(re.match(r"\.cp(?:ython-)?\d+[td]+[-_]", suffix) or suffix == "_d.pyd"
                for suffix in EXTENSION_SUFFIXES)):
        return None
    if sys.platform == "linux":
        try:
            libc, _, number = (os.confstr("CS_GNU_LIBC_VERSION") or "").partition(" ")
        except (OSError, ValueError):
            return None
        match = re.match(r"(\d+)\.(\d+)", number)
        if libc != "glibc" or not match or (int(match[1]), int(match[2])) < (2, 28):
            return None
    return _WHEELS.get(sys.platform)


@contextmanager
def _installation_lock(path: Path):
    # Closing the descriptor releases the lock after normal exit or process failure.
    with path.open("a+b") as lock:
        try:
            if os.name == "nt":
                import msvcrt

                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise NVDECInstallError(409, "NVDEC_INSTALL_BUSY", "另一个窗口正在安装 NVIDIA 硬件解码组件") from exc
        yield


def _extract_wheel(wheel: _Wheel, stage: Path) -> Path:
    archive = stage / "av.whl"
    digest, downloaded = hashlib.sha256(), 0
    deadline = monotonic() + 120
    try:
        with urlopen(wheel.url, timeout=12) as response, archive.open("xb") as output:
            if response.geturl() != wheel.url:
                raise NVDECInstallError(502, "NVDEC_INSTALL_FAILED", "PyAV 下载地址与官方二进制包不一致")
            while chunk := response.read(1024**2):
                if monotonic() >= deadline:
                    raise TimeoutError
                downloaded += len(chunk)
                if downloaded > wheel.bytes:
                    break
                output.write(chunk)
                digest.update(chunk)
    except (URLError, TimeoutError, ConnectionError, HTTPException, SSLError) as exc:
        raise NVDECInstallError(503, "NVDEC_INSTALL_FAILED", "无法从 PyPI 下载 PyAV，请检查网络后重试") from exc
    if downloaded != wheel.bytes or digest.hexdigest() != wheel.sha256:
        raise NVDECInstallError(502, "NVDEC_INSTALL_FAILED", "PyAV 二进制包大小或校验和不匹配")
    target = stage / "package"
    with zipfile.ZipFile(archive) as wheel_file:
        wheel_file.extractall(target)
    return target


class NVDECSetup:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.wheel = _compatible_wheel()
        self.target = data_dir / f"nvdec-pyav-{PYAV_VERSION}-{sys.platform}-{sys.implementation.cache_tag}"
        self._activation_failed = False
        if _enabled():
            self._activate()

    def _activate(self) -> None:
        if self._activation_failed or self.target.is_symlink() or not self.target.is_dir():
            return
        path = str(self.target)
        with _activation_lock:
            if self._activation_failed or path in sys.path or "av" in sys.modules:
                return
            sys.path.insert(0, path)
            importlib.invalidate_caches()
            _pyav_available.cache_clear()
            if not _pyav_available():
                sys.path.remove(path)
                self._activation_failed = True
                _pyav_available.cache_clear()

    def status(self) -> str:
        if not _enabled():
            return "disabled"
        if not _driver_available():
            return "no_gpu"
        self._activate()
        if _pyav_available():
            return "ready"
        if (self.wheel is None or "av" in sys.modules or self.target.exists() or self.target.is_symlink() or
                not os.access(self.data_dir, os.W_OK)):
            return "unsupported"
        return "installable"

    def _needs_install(self) -> bool:
        status = self.status()
        if status == "ready":
            return False
        if status == "installable":
            return True
        detail = {
            "disabled": "此服务已关闭 NVIDIA 硬件解码",
            "no_gpu": "当前服务无法访问 NVIDIA GPU 或视频解码驱动",
            "unsupported": "此平台无法自动安装 PyAV，可使用浏览器或手动安装受支持的版本",
        }
        raise NVDECInstallError(422, "NVDEC_INSTALL_UNAVAILABLE", detail[status])

    def install(self) -> None:
        if not self._needs_install():
            return
        try:
            with _installation_lock(self.data_dir / ".nvdec-install.lock"):
                if self._needs_install():
                    with tempfile.TemporaryDirectory(prefix=".nvdec-", dir=self.data_dir) as workspace:
                        assert self.wheel is not None
                        _extract_wheel(self.wheel, Path(workspace)).rename(self.target)
                    self._activate()
                    if not _pyav_available():
                        raise NVDECInstallError(422, "NVDEC_INSTALL_UNAVAILABLE", "PyAV 二进制包与服务 Python 或系统库不兼容")
        except (OSError, zipfile.BadZipFile) as exc:
            raise NVDECInstallError(502, "NVDEC_INSTALL_FAILED", "无法在应用状态目录安装 PyAV") from exc

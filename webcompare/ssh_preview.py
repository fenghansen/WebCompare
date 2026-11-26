"""【模块】SSH 小预览 — 可选复用远端已有图像工具，减少原图传输

功能说明：在已认证的 SSH 连接上优先使用已有 Python/Pillow，缺少时尝试已有
ImageMagick；普通 8-bit PNG/JPEG/WebP 优先只传元数据、小预览或完整尺寸高清 JPEG。
工具不可用、文件不支持、命令失败或超时均返回 None，由统一来源层回退到原有
本地处理。像素限制与源版本改变明确报错，精确处理、ICC 色彩转换、RAW 和 ROI 留给本地流程。
不安装软件、不使用 sudo、不联网、不在远端创建临时文件。

## 流程
所属流程：SSH 远端来源（webcompare/ssh_preview.py）

1. **元数据与能力**：按认证来源身份缓存工具能力并校验尺寸、位深 → 见函数 [RemotePreviewEngine.metadata]
2. **缩小预览**：执行静态脚本并有界接收小 PNG，复用私密缓存预算 → 见函数 [RemotePreviewEngine.render]
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import tempfile
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from .files import revision
from .image_formats import IMAGE_FORMATS
from .media import MediaResult
from .ssh import RemotePath, SSHError, SSHManager, _check_exec_cancelled

# These scripts are static programs. User paths are data on stdin or quoted
# positional arguments, and never become source code or executable options.
_PYTHON_SCRIPT = r'''# webcompare-remote-preview-v1
import io, json, math, os, signal, struct, sys, warnings
if sys.version_info < (3, 7):
    sys.exit(69)
try:
    from PIL import Image, ImageOps
except ImportError:
    sys.exit(69)
Image.MAX_IMAGE_PIXELS = None
warnings.simplefilter("ignore")
def sig(value):
    return (value.st_size, value.st_mtime_ns, value.st_ino, value.st_dev)
try:
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        sys.exit(74)
    request = json.loads(raw)
    # Bound the remote process itself too: closing an SSH channel alone is not
    # a portable guarantee that a decoder has stopped consuming CPU.
    seconds = min(60, max(1, int(math.ceil(request.get("timeout", 12)))))
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: os._exit(75))
        signal.alarm(seconds)
    try:
        import resource
        def lower_limit(kind, soft, hard):
            current_soft, current_hard = resource.getrlimit(kind)
            if current_soft != resource.RLIM_INFINITY:
                soft = min(soft, current_soft)
            if current_hard != resource.RLIM_INFINITY:
                hard = min(hard, current_hard)
            resource.setrlimit(kind, (min(soft, hard), hard))
        lower_limit(resource.RLIMIT_CPU, seconds, seconds + 1)
    except (ImportError, AttributeError, OSError, ValueError):
        pass
    path = request["path"]
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        sys.exit(74)
    with open(path, "rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != request["size"] or int(before.st_mtime) != request["mtime"]:
            sys.exit(73)
        header = stream.read(32)
        stream.seek(0)
        if header.startswith(b"\x89PNG\r\n\x1a\n") and (len(header) < 25 or header[24] != 8):
            sys.exit(70)
        if header[8:16] == b"WEBPVP8X" and len(header) > 20 and header[20] & 2:
            sys.exit(70)
        with Image.open(stream, formats=("PNG", "JPEG", "WEBP")) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or image.mode not in {"L", "LA", "RGB", "RGBA", "P"} or getattr(image, "n_frames", 1) != 1:
                sys.exit(70)
            width, height = image.size
            max_pixels = request["max_pixels"]
            if width <= 0 or height <= 0 or (max_pixels is not None and width * height > max_pixels):
                sys.exit(72)
            alpha = "A" in image.mode or "transparency" in image.info
            bands = {"L": 1, "LA": 2, "RGB": 3, "RGBA": 4}.get(image.mode, 4 if alpha else 3)
            if alpha and bands in {1, 3}:
                bands += 1
            orientation = image.getexif().get(274, 1)
            if orientation in {5, 6, 7, 8}:
                width, height = height, width
            metadata = {"width": width, "height": height, "bands": bands,
                        "format": image.format.lower(), "has_alpha": alpha,
                        "has_icc": bool(image.info.get("icc_profile")), "bit_depth": 8}
            if request["operation"] == "metadata":
                output = json.dumps(metadata, separators=(",", ":")).encode("utf-8")
            elif request["operation"] in {"render", "high"}:
                if request["operation"] == "high" and metadata["has_icc"]:
                    sys.exit(70)
                image = ImageOps.exif_transpose(image)
                image = image.convert("RGBA" if alpha else "RGB")
                if request["operation"] == "render":
                    maximum = request["max_size"]
                    if not isinstance(maximum, int) or not 1 <= maximum <= 4096:
                        sys.exit(74)
                    image.thumbnail((maximum, maximum), getattr(Image, "Resampling", Image).LANCZOS)
                # Metadata is stripped just as in the local preview encoder.
                image.info.clear()
                result = io.BytesIO()
                if request["operation"] == "high" and not alpha:
                    image.save(result, format="JPEG", quality=95, subsampling=0)
                else:
                    image.save(result, format="PNG", compress_level=6)
                output = result.getvalue()
            else:
                sys.exit(74)
        if sig(os.fstat(stream.fileno())) != sig(before) or sig(os.stat(path)) != sig(before):
            sys.exit(73)
    sys.stdout.buffer.write(output)
except SystemExit:
    raise
except Exception:
    sys.exit(71)
'''

_MAGICK_SCRIPT = r'''# webcompare-remote-imagemagick-v1
file=$1
operation=$2
maximum=$3
case "$file" in /*) ;; *) exit 74 ;; esac
if command -v magick >/dev/null 2>&1; then
    tool=magick
elif command -v identify >/dev/null 2>&1 && command -v convert >/dev/null 2>&1; then
    tool=legacy
else
    exit 69
fi
if [ "$operation" = metadata ]; then
    if [ "$tool" = magick ]; then
        magick identify -limit memory 128MiB -limit map 128MiB -limit disk 0 -limit time 12 -ping -format '%m|%w|%h|%z|%[channels]|%[profiles]|%[orientation]' - < "$file"
    else
        identify -limit memory 128MiB -limit map 128MiB -limit disk 0 -limit time 12 -ping -format '%m|%w|%h|%z|%[channels]|%[profiles]|%[orientation]' - < "$file"
    fi
elif [ "$operation" = render ]; then
    case "$maximum" in ''|*[!0-9]*) exit 74 ;; esac
    if [ "$tool" = magick ]; then
        magick -limit memory 128MiB -limit map 128MiB -limit disk 0 -limit time 12 - -auto-orient -thumbnail "${maximum}x${maximum}>" -strip -define png:color-type=6 -depth 8 png:- < "$file"
    else
        convert -limit memory 128MiB -limit map 128MiB -limit disk 0 -limit time 12 - -auto-orient -thumbnail "${maximum}x${maximum}>" -strip -define png:color-type=6 -depth 8 png:- < "$file"
    fi
else
    exit 74
fi
'''

_MISSING = object()
_MAX_OUTPUT = 16 * 1024**2
_MAX_HIGH_OUTPUT = 32 * 1024**2


class RemotePreviewEngine:
    """已认证 SSH 来源的可选元数据与小预览引擎；None 表示原流程回退。"""

    def __init__(self, remote: SSHManager):
        self.remote = remote
        self.timeout = 12.0
        self._lock = threading.RLock()
        self._backends: OrderedDict[tuple, str | None] = OrderedDict()
        self._metadata: OrderedDict[tuple, dict] = OrderedDict()

    def _check(self, source: RemotePath, expected_revision: str, cancel_event=None):
        _check_exec_cancelled(cancel_event)
        attrs = source.stat()
        _check_exec_cancelled(cancel_event)
        if revision(source, st=attrs) != expected_revision:
            raise SSHError("远端源文件已改变，请重新读取目录", "SOURCE_CHANGED", 409,
                           {"connection_id": source.connection_id})
        return attrs

    @contextmanager
    def _resource_lock(self, key, cancel_event):
        lock = self.remote._resource_lock(key)
        while not lock.acquire(timeout=.05):
            _check_exec_cancelled(cancel_event)
        try:
            _check_exec_cancelled(cancel_event)
            yield
        finally:
            lock.release()

    def _remember_backend(self, key: tuple, backend: str | None):
        with self._lock:
            self._backends[key] = backend
            self._backends.move_to_end(key)
            while len(self._backends) > 256:
                self._backends.popitem(last=False)

    def _run(self, source: RemotePath, expected_revision: str, attrs, operation: str, max_pixels: int | None, max_size: int = 0, *, cancel_event=None):
        _check_exec_cancelled(cancel_event)
        identity = (source.connection_id, attrs.st_dev)
        with self._lock:
            backend = self._backends.get(identity, _MISSING)
        if backend is None:
            return None
        choices = [backend] if backend is not _MISSING else ["python3", "python", "imagemagick"]
        request = {"path": source.path, "operation": operation, "size": attrs.st_size,
                   "mtime": int(attrs.st_mtime), "max_pixels": max_pixels, "max_size": max_size, "timeout": self.timeout}
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > 16384:
            return None
        for candidate in choices:
            _check_exec_cancelled(cancel_event)
            # For WebP, use the Python animation check or the local decoder.
            # ImageMagick is only used for metadata and bounded thumbnails.
            if candidate == "imagemagick" and (operation == "high" or source.suffix.lower() == ".webp"):
                return None
            argv = (["sh", "-c", _MAGICK_SCRIPT, "webcompare-preview", source.path, operation, str(max_size)]
                    if candidate == "imagemagick" else [candidate, "-c", _PYTHON_SCRIPT])
            try:
                result = self.remote.execute(source, argv, b"" if candidate == "imagemagick" else payload,
                                             expected_revision=expected_revision, timeout=self.timeout,
                                             max_stdout=8192 if operation == "metadata" else _MAX_HIGH_OUTPUT if operation == "high" else _MAX_OUTPUT,
                                             max_stderr=4096, cancel_event=cancel_event)
            except SSHError as exc:
                if exc.code == "SSH_EXEC_UNAVAILABLE":
                    self._remember_backend(identity, None)
                    return None
                if exc.code in {"SSH_EXEC_TIMEOUT", "SSH_EXEC_BUSY", "SSH_EXEC_OUTPUT_LIMIT", "SSH_EXEC_FAILED"}:
                    return None
                raise
            if result.exit_status == 0:
                self._remember_backend(identity, candidate)
                return candidate, result.stdout
            if result.exit_status == 72:
                raise SSHError("图片超过像素上限", "PIXEL_LIMIT", 422)
            if result.exit_status == 73:
                raise SSHError("远端源文件在处理期间已改变", "SOURCE_CHANGED", 409,
                               {"connection_id": source.connection_id})
            if result.exit_status in {70, 71, 75}:
                self._remember_backend(identity, candidate)
                return None
            # Missing interpreter/module/tool is a capability miss. Other tool
            # failures also fall back locally; no stderr enters an API or log.
            if backend is not _MISSING:
                return None
        self._remember_backend(identity, None)
        return None

    @staticmethod
    def _decode_metadata(backend: str, content: bytes, max_pixels: int | None) -> dict | None:
        try:
            if backend == "imagemagick":
                parts = content.decode("utf-8").split("|")
                if len(parts) != 7:
                    return None
                fmt, width, height, depth, channels, profiles, orientation = parts
                channel = channels.casefold()
                bands = {"gray": 1, "graya": 2, "grayalpha": 2, "srgb": 3, "rgb": 3, "srgba": 4, "rgba": 4}.get(channel)
                if bands is None or int(depth) != 8:
                    return None
                width, height = int(width), int(height)
                if orientation.casefold() in {"lefttop", "righttop", "rightbottom", "leftbottom"}:
                    width, height = height, width
                data = {"width": width, "height": height, "bands": bands, "format": fmt.lower(),
                        "has_alpha": bands in {2, 4}, "has_icc": "icc" in profiles.casefold(), "bit_depth": 8}
            else:
                data = json.loads(content)
            if (not isinstance(data, dict) or data.get("format") not in IMAGE_FORMATS.values() or data.get("bit_depth") != 8
                    or type(data.get("width")) is not int or type(data.get("height")) is not int
                    or data.get("bands") not in {1, 2, 3, 4}
                    or type(data.get("has_alpha")) is not bool or type(data.get("has_icc")) is not bool):
                return None
            if (data["width"] <= 0 or data["height"] <= 0
                    or (max_pixels is not None and data["width"] * data["height"] > max_pixels)):
                raise SSHError("图片超过像素上限", "PIXEL_LIMIT", 422)
            return {key: data[key] for key in ("width", "height", "bands", "format", "has_alpha", "has_icc", "bit_depth")}
        except (ValueError, TypeError, KeyError, UnicodeError):
            return None

    def metadata(self, source: RemotePath, expected_revision: str, max_pixels: int | None, *, cancel_event=None) -> dict | None:
        """功能说明：优先在远端读取普通图片元数据，避免先下载完整大 PNG。

        调用时机：统一来源媒体层请求 SSH 资产元数据时。

        关键步骤：
        1. 核验版本并按连接的认证来源身份查询工具能力。
        2. 执行有界静态脚本，校验尺寸、通道、8-bit 位深与 EXIF 显示方向。
        3. 缓存有界成功结果，命中时仍实时复核源版本。

        输出/副作用：返回 MediaService 兼容字典；能力不足返回 None，不下载原图。
        """
        if source.suffix.lower() not in IMAGE_FORMATS or (max_pixels is not None and max_pixels <= 0):
            return None
        key = (str(source), expected_revision, max_pixels)
        with self.remote.cache_lease(), self._resource_lock("preview-metadata:" + repr(key), cancel_event):
            attrs = self._check(source, expected_revision, cancel_event)
            with self._lock:
                cached = self._metadata.get(key)
                if cached is not None:
                    self._metadata.move_to_end(key)
                    return dict(cached)
            result = self._run(source, expected_revision, attrs, "metadata", max_pixels, cancel_event=cancel_event)
            if result is None:
                return None
            metadata = self._decode_metadata(result[0], result[1], max_pixels)
            self._check(source, expected_revision, cancel_event)
            if metadata is not None:
                with self._lock:
                    self._metadata[key] = dict(metadata)
                    while len(self._metadata) > 256:
                        self._metadata.popitem(last=False)
            return metadata

    @staticmethod
    def _valid_png(data: bytes, maximum: int) -> bool:
        if len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return False
        width, height = struct.unpack(">II", data[16:24])
        return 0 < width <= maximum and 0 < height <= maximum and data[24] == 8 and data[25] in {2, 6}

    def _store(self, target: Path, content: bytes):
        self.remote._reserve(len(content))
        reserved = True
        temporary = None
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".remote-preview-", dir=target.parent)
            with self.remote._cache_lock:
                self.remote._temporary_paths.add(Path(temporary))
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            with self.remote._cache_lock:
                os.replace(temporary, target)
                self.remote._temporary_paths.discard(Path(temporary))
                temporary = None
                self.remote._release(len(content))
                reserved = False
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
                with self.remote._cache_lock:
                    self.remote._temporary_paths.discard(Path(temporary))
            if reserved:
                self.remote._release(len(content))

    def render(self, source: RemotePath, expected_revision: str, max_size: int, box=None, max_pixels: int | None = 24_000_000, *, cancel_event=None) -> Path | None:
        """功能说明：只传回普通图片缩小后的 PNG，保留精确原图流程供本地调用。

        调用时机：统一来源层处理普通 preview 请求；ICC、ROI、high、exact、diff 留给原流程。

        关键步骤：
        1. 确认媒体类型、版本与像素上限，ROI 暂不远端处理。
        2. 按来源版本和尺寸合并请求，执行远端工具并有界验证 PNG 头。
        3. 按现有 SSH 缓存预算原子保存小图，返回前再次核验版本。

        输出/副作用：返回私密本地小 PNG；工具缺失或处理失败返回 None。
        """
        if source.suffix.lower() not in IMAGE_FORMATS or box is not None or type(max_size) is not int or not 1 <= max_size <= 4096:
            return None
        identity = hashlib.sha256(json.dumps([str(source), expected_revision, max_size, max_pixels, "preview-v1"], ensure_ascii=False).encode()).hexdigest()
        target = self.remote.cache_dir / "previews" / (identity + ".png")
        with self.remote.cache_lease(), self._resource_lock("preview-render:" + identity, cancel_event):
            attrs = self._check(source, expected_revision, cancel_event)
            metadata = self.metadata(source, expected_revision, max_pixels, cancel_event=cancel_event)
            # Remote resize/strip cannot substitute for the local ICC-to-sRGB
            # color pipeline. Preserve fidelity by falling back for ICC input.
            if metadata is None or metadata["has_icc"]:
                return None
            if target.is_file():
                self._check(source, expected_revision, cancel_event)
                return target
            result = self._run(source, expected_revision, attrs, "render", max_pixels, max_size, cancel_event=cancel_event)
            if result is None or not self._valid_png(result[1], max_size):
                return None
            self._check(source, expected_revision, cancel_event)
            self._store(target, result[1])
            self._check(source, expected_revision, cancel_event)
            return target

    @staticmethod
    def _valid_high(data: bytes, metadata: dict) -> bool:
        width, height = metadata["width"], metadata["height"]
        if metadata["has_alpha"]:
            return (RemotePreviewEngine._valid_png(data, max(width, height))
                    and struct.unpack(">II", data[16:24]) == (width, height)
                    and data[25] == 6)
        if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
            return False
        offset = 2
        while offset + 4 <= len(data) and data[offset] == 0xFF:
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                return False
            marker = data[offset]
            offset += 1
            if marker in {0xDA, 0xD9}:
                return False
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                return False
            if marker in {0xC0, 0xC1, 0xC2}:
                return (length >= 8 and data[offset + 2] == 8
                        and int.from_bytes(data[offset + 3:offset + 5], "big") == height
                        and int.from_bytes(data[offset + 5:offset + 7], "big") == width
                        and data[offset + 7] == 3)
            offset += length
        return False

    def high(self, source: RemotePath, expected_revision: str, max_pixels: int | None = 24_000_000, *, cancel_event=None) -> MediaResult | None:
        """Transmit one full-size JPEG (or alpha PNG), without downloading/re-encoding the original."""
        if source.suffix.lower() not in IMAGE_FORMATS or (max_pixels is not None and max_pixels <= 0):
            return None
        identity = hashlib.sha256(json.dumps([str(source), expected_revision, max_pixels, "high-v1"],
                                            ensure_ascii=False).encode()).hexdigest()
        with self.remote.cache_lease(), self._resource_lock("high-render:" + identity, cancel_event):
            attrs = self._check(source, expected_revision, cancel_event)
            metadata = self.metadata(source, expected_revision, max_pixels, cancel_event=cancel_event)
            if metadata is None or metadata["has_icc"]:
                return None
            suffix = ".png" if metadata["has_alpha"] else ".jpg"
            target = self.remote.cache_dir / "previews" / (identity + suffix)
            if not target.is_file():
                result = self._run(source, expected_revision, attrs, "high", max_pixels, cancel_event=cancel_event)
                if result is None or not self._valid_high(result[1], metadata):
                    return None
                self._check(source, expected_revision, cancel_event)
                self._store(target, result[1])
            self._check(source, expected_revision, cancel_event)
            return MediaResult(target, "image/png" if metadata["has_alpha"] else "image/jpeg",
                               metadata["width"], metadata["height"], "high", identity)

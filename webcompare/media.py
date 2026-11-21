"""【模块】媒体解码子进程 — 有界磁盘缓存与子进程隔离的图片解码/渲染

功能说明：HTTP 进程刻意不 import pyvips；personal 在唯一的 spawn
worker 内解码，配置 sandbox 时 worker 只排队调度，每次实际解码都在
独立 bubblewrap 内执行；libvips 缓存与线程被限制在小上限内。
HTTP 侧只保留异步门面：相同请求按缓存键合并，结果 PNG/JPEG/WebP 落盘成
有界 LRU 缓存，流式响应期间用 pin 防止被清理。The HTTP process
deliberately has no pyvips import; one spawned worker serializes decoding,
using a fresh namespace process for every sandboxed operation.

## 流程
所属流程：媒体解码子进程（webcompare/media.py）

1. **异步门面**：HTTP 侧的元数据/渲染/像素接口与请求合并 → 见函数 [MediaService.metadata]、[MediaService.render]、[MediaService.pixel]
2. **队列与 worker 管理**：提交请求、spawn 子进程、结果回收与超时处置 → 见函数 [MediaService._submit]、[MediaService._ensure_worker]
3. **磁盘缓存与钉住**：LRU 缓存维护、流式响应期间防清理 → 见函数 [MediaService.pin]、[MediaService.unpin]
4. **worker 解码循环**：子进程内的 libvips 加载、校验与编码 → 见函数 [_worker_main]、[_worker_render]
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import multiprocessing as mp
import os
import queue
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .image_formats import IMAGE_FORMATS, is_animated_webp

try:  # pragma: no cover - 仅非 POSIX 平台走这里
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]


_RENDER_CACHE_VERSION = "5"
_METADATA_CACHE_MAX_ITEMS = 256

# 实测取值依据（本机 72 核 / Linux，pyvips 3.2 binary wheel，24 MP 为默认像素上限）：
# - worker 在 import pyvips 之前固定 VIPS_CONCURRENCY=1；不固定时 libvips 按核数
#   起线程，仅一张 2400×1800 PNG 的 stats() 就能把 VmSize 顶到 2.5 GiB，768 MiB
#   的 RLIMIT_AS 反而会让线程创建失败。
# - 单线程实测 VmPeak：24 MP PNG 解码 ≈ 194 MiB、24 MP JPEG ≈ 217 MiB、两张 24 MP
#   做 int 差分并编码 ≈ 337 MiB、隔行（无法流式、需整图读入）24 MP 差分 ≈ 351 MiB。
#   768 MiB 相对最坏实测留了一倍以上余量，仍能拦住失控的放大解码。
_WORKER_RLIMIT_AS_BYTES = 768 * 1024**2
_WORKER_RLIMIT_NOFILE = 256
_WORKER_DISC_THRESHOLD_BYTES = 16 * 1024 * 1024


@dataclass(slots=True)
class MediaResult:
    path: Path
    mime: str
    width: int
    height: int
    quality: str
    etag: str


@dataclass(slots=True)
class MediaError(Exception):
    message: str
    code: str = "DECODE_ERROR"
    status: int = 422

    def __str__(self) -> str:
        return self.message


@dataclass(slots=True)
class _WorkerError:
    message: str
    code: str
    status: int


def _stat_signature(path: Path) -> tuple[int, int, int]:
    try:
        st = path.stat()
    except FileNotFoundError as exc:
        raise MediaError("source file does not exist", "SOURCE_MISSING", 422) from exc
    except OSError as exc:
        raise MediaError(f"cannot stat source: {exc}", "SOURCE_ERROR", 422) from exc
    return (int(st.st_size), int(st.st_mtime_ns), int(getattr(st, "st_ino", 0)))


def _worker_stat(path: str) -> tuple[int, int, int]:
    st = os.stat(path)
    return (int(st.st_size), int(st.st_mtime_ns), int(getattr(st, "st_ino", 0)))


class _WorkerMediaError(Exception):
    def __init__(self, message: str, code: str = "DECODE_ERROR", status: int = 422):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _worker_error(exc: BaseException) -> _WorkerError:
    from .raw import RawError
    logger = logging.getLogger(__name__)
    if isinstance(exc, RawError):
        logger.warning("媒体请求失败 [%s]: %s", exc.code, exc.message)
        return _WorkerError(exc.message, exc.code, exc.status)
    if isinstance(exc, _WorkerMediaError):
        logger.warning("媒体请求失败 [%s]: %s", exc.code, exc.message)
        return _WorkerError(exc.message, exc.code, exc.status)
    logger.error("媒体 worker 发生异常", exc_info=(type(exc), exc, exc.__traceback__))
    return _WorkerError(str(exc) or "image decode failed", "DECODE_ERROR", 422)


def _worker_apply_limits(*, memory_limit: bool = True) -> None:
    """为解码进程设置句柄上限；隔离解码另保留 768 MiB 地址空间上限。

    调用时机：[_worker_main] 在 import pyvips 之前调用——rlimits 必须先于
    解码库初始化生效，否则已经映射的地址空间不受约束。

    关键步骤：
    1. RLIMIT_NOFILE=256：libvips 同时打开的源图、临时文件与缓存文件的宽松上限；
    2. RLIMIT_AS=768 MiB：按本机实测峰值（见文件顶部注释）留一倍以上余量，
       只兜住失控的放大解码，正常 24 MP 解码/差分渲染不受影响；
    3. 不设 RLIMIT_CPU：合法的大图渲染本来就要跑较长时间，硬性 CPU 上限会误杀；
       单请求的兜底是 HTTP 侧 30 秒 worker 超时（见 [MediaService._submit]）；
    4. 平台不支持或设置失败时打 warning 继续，不让 worker 因设限失败而不可用。

    输出/副作用：修改本进程资源上限；不写文件。
    """
    if resource is None:  # pragma: no cover - 仅非 POSIX 平台
        return
    limits = [("RLIMIT_NOFILE", resource.RLIMIT_NOFILE, _WORKER_RLIMIT_NOFILE)]
    if memory_limit:
        limits.append(("RLIMIT_AS", resource.RLIMIT_AS, _WORKER_RLIMIT_AS_BYTES))
    for name, which, value in limits:
        try:
            resource.setrlimit(which, (value, value))
        except (OSError, ValueError) as exc:  # pragma: no cover - 取决于平台
            logging.getLogger(__name__).warning("媒体 worker 无法设置 %s: %s", name, exc)


def _worker_main(requests: Any, results: Any, max_pixels: int, vips_tmp_dir: str,
                 sandbox_mode: str | None = None, personal: bool = False) -> None:
    """功能说明：媒体 worker 子进程的主循环，独占 libvips 并串行处理解码请求。

    调用时机：由 [MediaService._ensure_worker] 以 spawn 方式启动一次；
    进程内 import pyvips 是有意的边界，HTTP 进程不持有本地解码库。
    收到 None 哨兵或进程被终止时退出。

    sandbox_mode 非 None 时仅监督一次性媒体进程，不在本进程加载 pyvips；
    Linux 要求实际 namespace，Windows auto/none 用一次性子进程。
    personal 仅由可信 personal 门面显式传入；沙箱内部调用默认仍受资源限制。

    关键步骤：
    1. 加载前设置 VIPS_CONCURRENCY=1、磁盘缓冲阈值与临时目录等环境变量，
       并调用 [_worker_apply_limits] 设好句柄与虚拟内存上限，再把 libvips
       缓存压到小上限；加载失败则对所有请求回同一份错误；
    2. 循环取请求：metadata 读尺寸/格式信息，validate 额外用 stats() 驱动
       完整解码，pixel 取单点像素，render 交给 [_worker_render] 解码编码；
    3. 每个请求处理前后做源签名比对，发现源文件在处理中被改动即放弃结果；
    4. 任何异常都转成结构化错误回传，不让 worker 因单个请求崩溃。

    输出/副作用：结果经多进程队列回传给 HTTP 进程；渲染产物写入缓存
    目录；除缓存文件外不修改文件系统。
    """
    from .logging_setup import configure_worker_logging

    configure_worker_logging()
    if sandbox_mode is not None:
        from .media_sandbox import run_media

        _worker_apply_limits()
        while True:
            request = requests.get()
            if request is None:
                return
            request_id, command, payload = request
            try:
                raw_work = command == "raw_convert" or payload.get("raw_metadata") is not None or payload.get("diff_raw_metadata") is not None
                result = run_media(command, payload, max_pixels, Path(vips_tmp_dir), mode=sandbox_mode,
                                   timeout=120.0 if raw_work else 30.0)
                results.put((request_id, result, None))
            except BaseException as exc:
                results.put((request_id, None, _worker_error(exc)))
    try:
        # These must be set before libvips is loaded.  The explicit API calls
        # below cover builds that ignore the environment variables.
        os.environ["VIPS_CONCURRENCY"] = "1"
        # NumPy/native RAW helpers are loaded lazily after the worker limits.
        # BLAS defaults can otherwise reserve hundreds of MiB of thread stacks.
        for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            os.environ[name] = "1"
        os.environ["VIPS_DISC_THRESHOLD"] = str(_WORKER_DISC_THRESHOLD_BYTES)
        os.environ["VIPS_DISC_DIR"] = vips_tmp_dir
        os.environ["TMPDIR"] = vips_tmp_dir
        os.environ["TEMP"] = vips_tmp_dir
        os.environ["TMP"] = vips_tmp_dir
        os.makedirs(vips_tmp_dir, exist_ok=True)
        _worker_apply_limits(memory_limit=not personal)
        import pyvips  # type: ignore[import-not-found]

        pyvips.concurrency_set(1)
        pyvips.cache_set_max(0 if personal else 32)
        if not personal:
            pyvips.cache_set_max_mem(16 * 1024 * 1024)
    except BaseException as exc:
        err = _worker_error(exc)
        while True:
            request = requests.get()
            if request is None:
                return
            results.put((request[0], None, err))

    def fail(request_id: str, exc: BaseException) -> None:
        results.put((request_id, None, _worker_error(exc)))

    while True:
        request = requests.get()
        if request is None:
            return
        request_id, command, payload = request
        try:
            if command == "raw_convert":
                results.put((request_id, _worker_raw_convert(pyvips, payload, max_pixels), None))
            elif command == "metadata":
                path = str(payload["source"])
                expected = tuple(payload["source_stat"])
                _worker_check_stat(path, expected)
                image, meta = _load_image(pyvips, path, max_pixels, raw_metadata=payload.get("raw_metadata"))
                _worker_check_stat(path, expected)
                results.put((request_id, meta, None))
            elif command == "validate":
                path = str(payload["source"])
                expected = tuple(payload["source_stat"])
                _worker_check_stat(path, expected)
                image, meta = _load_image(pyvips, path, max_pixels, raw_metadata=payload.get("raw_metadata"))
                image.stats()  # 强制完整解码：确认数据没有截断或损坏
                _worker_check_stat(path, expected)
                results.put((request_id, meta, None))
            elif command == "pixel":
                path = str(payload["source"])
                expected = tuple(payload["source_stat"])
                _worker_check_stat(path, expected)
                image, meta = _load_image(pyvips, path, max_pixels, raw_metadata=payload.get("raw_metadata"))
                x, y = int(payload["x"]), int(payload["y"])
                if x < 0 or y < 0 or x >= image.width or y >= image.height:
                    raise _WorkerMediaError("pixel is outside source image", "INVALID_ARGUMENT", 422)
                values = _pixel_values(image, x, y)
                _worker_check_stat(path, expected)
                results.put((request_id, {"x": x, "y": y, "values": values}, None))
            elif command == "render":
                result = _worker_render(pyvips, payload, max_pixels)
                results.put((request_id, result, None))
            else:
                raise _WorkerMediaError("unknown media operation", "INVALID_ARGUMENT", 422)
        except BaseException as exc:
            fail(request_id, exc)
        finally:
            if personal:
                image = None
                # Derived files are reusable; decoded image graphs do not survive a request.
                pyvips.cache_set_max(0)


def _worker_check_stat(path: str, expected: tuple[int, int, int]) -> None:
    try:
        current = _worker_stat(path)
    except FileNotFoundError as exc:
        raise _WorkerMediaError("source file does not exist", "SOURCE_CHANGED", 409) from exc
    except OSError as exc:
        raise _WorkerMediaError(f"cannot stat source: {exc}", "SOURCE_ERROR", 422) from exc
    if current != expected:
        raise _WorkerMediaError("source changed while processing", "SOURCE_CHANGED", 409)


def _loader_format(image: Any, path: str) -> str:
    loader = ""
    try:
        loader = str(image.get("vips-loader"))
    except Exception:
        pass
    if loader in {"pngload", "jpegload", "webpload"}:
        return loader.removesuffix("load")
    if not loader and (fmt := IMAGE_FORMATS.get(Path(path).suffix.lower())):
        return fmt
    raise _WorkerMediaError("only PNG, JPEG and WebP images are supported", "UNSUPPORTED_FORMAT", 422)


def _load_image(pyvips: Any, path: str, max_pixels: int, *, access: str = "sequential", raw_metadata: dict | None = None) -> tuple[Any, dict[str, Any]]:
    try:
        image = pyvips.Image.new_from_file(path, access=access, fail=True)
    except BaseException as exc:
        raise _WorkerMediaError(f"cannot decode image: {exc}", "DECODE_ERROR", 422) from exc
    fmt = "raw" if raw_metadata is not None else _loader_format(image, path)
    if fmt == "webp":
        with open(path, "rb") as stream:
            animated = is_animated_webp(stream.read(32))
        if animated or (image.get_typeof("n-pages") and image.get("n-pages") > 1):
            raise _WorkerMediaError("动画 WebP 暂不支持，请使用静态 WebP", "UNSUPPORTED_FORMAT", 422)
    interpretation = str(getattr(image, "interpretation", "")).lower()
    if "cmyk" in interpretation:
        raise _WorkerMediaError("CMYK JPEG is not supported", "UNSUPPORTED_FORMAT", 422)
    if image.format != "uchar":
        raise _WorkerMediaError("only 8-bit PNG/JPEG/WebP images are supported", "UNSUPPORTED_BIT_DEPTH", 422)
    if image.width <= 0 or image.height <= 0 or image.width * image.height > max_pixels:
        raise _WorkerMediaError("image exceeds the pixel limit", "PIXEL_LIMIT", 422)
    if image.bands not in {1, 2, 3, 4}:
        raise _WorkerMediaError("unsupported image channels", "UNSUPPORTED_FORMAT", 422)
    if raw_metadata is not None:
        if (Path(path).suffix.lower() != ".v" or int(image.width) != raw_metadata["width"]
                or int(image.height) != raw_metadata["height"] or image.bands != 3):
            raise _WorkerMediaError("RAW RGB cache is invalid", "CACHE_ERROR", 503)
        return image, raw_metadata
    try:
        image = image.autorot()
    except Exception:
        # Older libvips builds may not provide autorot; the decoded image is
        # still usable, but normal modern builds always take this path.
        pass
    if image.width * image.height > max_pixels:
        raise _WorkerMediaError("image exceeds the pixel limit", "PIXEL_LIMIT", 422)
    source_bands = int(image.bands)
    has_alpha = source_bands in {2, 4}
    return image, {
        "width": int(image.width),
        "height": int(image.height),
        "bands": source_bands,
        "format": fmt,
        "has_alpha": has_alpha,
        "has_icc": bool(image.get_typeof("icc-profile-data")) if hasattr(image, "get_typeof") else False,
        "bit_depth": 8,
    }


def _rgb_image(image: Any) -> Any:
    bands = int(image.bands)
    if bands == 1:
        return image.bandjoin([image, image]).copy(interpretation="srgb")
    if bands == 2:
        gray = image.extract_band(0)
        # Difference is defined over RGB only.  Ignore the alpha plane rather
        # than accidentally producing a four-channel difference.
        return gray.bandjoin([gray, gray]).copy(interpretation="srgb")
    if bands == 3:
        return image.copy(interpretation="srgb")
    if bands == 4:
        return image.extract_band(0, n=3).copy(interpretation="srgb")
    raise _WorkerMediaError("unsupported image channels", "UNSUPPORTED_FORMAT", 422)


def _rgba_or_rgb_image(image: Any) -> Any:
    bands = int(image.bands)
    if bands in {3, 4}:
        return image.copy(interpretation="srgb")
    if bands == 1:
        return image.bandjoin([image, image]).copy(interpretation="srgb")
    if bands == 2:
        gray = image.extract_band(0)
        alpha = image.extract_band(1)
        return gray.bandjoin([gray, gray, alpha]).copy(interpretation="srgb")
    raise _WorkerMediaError("unsupported image channels", "UNSUPPORTED_FORMAT", 422)


def _pixel_values(image: Any, x: int, y: int) -> list[int]:
    values = [max(0, min(255, int(round(value)))) for value in image.getpoint(x, y)]
    if len(values) == 1:
        return [values[0], values[0], values[0]]
    if len(values) == 2:
        return [values[0], values[0], values[0], values[1]]
    return values[:4]


def _validate_box(box: Any, width: int, height: int) -> tuple[int, int, int, int] | None:
    if box is None:
        return None
    if not isinstance(box, (tuple, list)) or len(box) != 4:
        raise _WorkerMediaError("box must contain x, y, width, height", "INVALID_ARGUMENT", 422)
    x, y, w, h = (int(v) for v in box)
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise _WorkerMediaError("crop box is outside source image", "INVALID_ARGUMENT", 422)
    return x, y, w, h


def _resize(image: Any, max_size: int) -> Any:
    largest = max(int(image.width), int(image.height))
    if largest <= max_size:
        return image
    return image.resize(float(max_size) / largest, kernel="lanczos3")


def _worker_raw_convert(pyvips: Any, payload: dict[str, Any], max_pixels: int) -> dict[str, Any]:
    """Decode RAW once into a lossless native VIPS cache, with no PNG encode."""
    from .raw import demosaic_bilinear, load_raw_array

    source, expected = str(payload["source"]), tuple(payload["source_stat"])
    _worker_check_stat(source, expected)
    metadata = payload["raw_metadata"]
    width, height = metadata["width"], metadata["height"]
    if width * height > max_pixels:
        raise _WorkerMediaError("RAW exceeds the pixel limit", "PIXEL_LIMIT", 422)
    resolved = metadata["raw"]["resolved"]
    array = load_raw_array(source, metadata)
    rgb = demosaic_bilinear(array, resolved["bayer"], black_level=resolved["black_level"],
                            white_level=resolved["white_level"], gain=resolved["gain"],
                            gamma=resolved["gamma"], white_balance=tuple(resolved["white_balance"]),
                            black_levels=resolved.get("black_levels"))
    del array
    output = Path(payload["output"])
    temporary = output.with_name(f".{output.stem}.tmp.{os.getpid()}.{time.time_ns()}.v")
    try:
        image = pyvips.Image.new_from_memory(rgb.tobytes(), width, height, 3, "uchar").copy(interpretation="srgb")
        image.write_to_file(str(temporary))
        _worker_check_stat(source, expected)
        os.replace(temporary, output)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        # Release memory-backed image nodes before serving the next RAW. The
        # reusable display is now a memory-mappable disk file.
        pyvips.cache_set_max(0)
        pyvips.cache_set_max(32)
    return {"path": str(output), "width": width, "height": height}


def _worker_render(pyvips: Any, payload: dict[str, Any], max_pixels: int) -> dict[str, Any]:
    """功能说明：worker 内执行一次渲染：解码源图、可选差分、裁剪缩放、编码落盘。

    调用时机：[_worker_main] 收到 render 请求时调用，每次请求一次。

    关键步骤：
    1. 加载源图并校验签名；若带 diff_source，再加载基准图，转 RGB 后
       先做整型减法取绝对值（避免无符号下溢掩盖差异），再裁剪缩放；
       否则按是否含 alpha 转 RGB/RGBA；
    2. 校验裁剪框必须在图内；非整图模式按 max_size 上限缩小；
    3. exact 与含透明通道的 high 输出 PNG，普通 high 输出 JPEG（Q=95），
       RAW high 沿用编码设置，preview 输出 WebP（Q=82、effort=0），
       均先写同目录临时文件、剥离 ICC 等元数据；
    4. 编码后再次校验源签名，用 os.replace 原子替换为目标缓存文件。

    输出/副作用：缓存目录新增一个 PNG/JPEG/WebP；返回路径、MIME 与尺寸；
    中途源变更抛 409，编码失败抛 503，参数非法抛 422。
    """
    path = str(payload["source"])
    expected = tuple(payload["source_stat"])
    _worker_check_stat(path, expected)
    diff_path = payload.get("diff_source")
    random_png_access = diff_path is not None and str(payload["quality"]) != "exact"
    image_access = "random" if random_png_access and Path(path).suffix.lower() == ".png" else "sequential"
    image, source_metadata = _load_image(pyvips, path, max_pixels, access=image_access, raw_metadata=payload.get("raw_metadata"))
    fast_thumbnail = (str(payload["quality"]) == "preview" and diff_path is None
                      and payload.get("box") is None and not payload.get("full", False)
                      and payload.get("raw_metadata") is None
                      and source_metadata["format"] == "jpeg" and not source_metadata["has_icc"])
    if diff_path is not None:
        diff_expected = tuple(payload["diff_stat"])
        _worker_check_stat(str(diff_path), diff_expected)
        other_path = str(diff_path)
        other_access = "random" if random_png_access and Path(other_path).suffix.lower() == ".png" else "sequential"
        other, _ = _load_image(pyvips, other_path, max_pixels, access=other_access, raw_metadata=payload.get("diff_raw_metadata"))
        left = _rgb_image(image)
        right = _rgb_image(other)
        if left.width != right.width or left.height != right.height:
            raise _WorkerMediaError("source images have different dimensions", "DIMENSION_MISMATCH", 422)
        # Widen before subtraction so unsigned underflow cannot hide a
        # difference (for example 1 - 10 must become 9).  abs is intentionally
        # computed before crop and resize.
        image = left.cast("int").subtract(right.cast("int")).abs().cast("uchar").copy(interpretation="srgb")
    elif fast_thumbnail:
        # JPEG shrink-on-load avoids decoding full resolution for picker thumbnails.
        maximum = int(payload["max_size"])
        image = pyvips.Image.thumbnail(path, maximum, height=maximum, size="down", fail_on="warning")
        image = _rgba_or_rgb_image(image)
    else:
        image = _rgba_or_rgb_image(image)

    box = _validate_box(payload.get("box"), int(image.width), int(image.height))
    if box is not None:
        image = image.crop(box[0], box[1], box[2], box[3])
    # Full-image exact mode is decoded and encoded once, then the browser can
    # perform every pan/zoom locally.  Preview and ROI requests keep the
    # bounded max-size path used by low-bandwidth clients.
    if not bool(payload.get("full", False)) and not fast_thumbnail:
        max_size_value = int(payload["max_size"])
        image = _resize(image, max_size_value)
    output = str(payload["output"])
    # libvips selects the saver from the final suffix.  Keep the original
    # extension on the temporary path while still keeping it in the same
    # directory for an atomic replace.
    output_path = Path(output)
    tmp = str(output_path.with_name(f".{output_path.stem}.tmp.{os.getpid()}.{time.time_ns()}{output_path.suffix}"))
    quality = str(payload["quality"])
    try:
        if quality == "exact":
            image.write_to_file(tmp, compression=int(payload.get("png_compression", 6)), strip=True)
            mime = "image/png"
        elif quality == "high":
            if payload.get("encoding") == "jpeg":
                image.write_to_file(tmp, Q=95, subsample_mode="off", strip=True)
                mime = "image/jpeg"
            elif payload.get("encoding") == "png":
                image.write_to_file(tmp, compression=int(payload.get("png_compression", 6)), strip=True)
                mime = "image/png"
            else:
                image.write_to_file(tmp, Q=95, effort=0, strip=True)
                mime = "image/webp"
        else:
            image.write_to_file(tmp, Q=82, effort=int(payload.get("preview_effort", 0)), strip=True)
            mime = "image/webp"
        _worker_check_stat(path, expected)
        if diff_path is not None:
            _worker_check_stat(str(diff_path), tuple(payload["diff_stat"]))
        os.replace(tmp, output)
    except _WorkerMediaError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise _WorkerMediaError(f"cannot encode image: {exc}", "CACHE_ERROR", 503) from exc
    return {"path": output, "mime": mime, "width": int(image.width), "height": int(image.height), "quality": quality}


class MediaService:
    """HTTP 媒体门面；sandbox=None 保留 personal 行为，其他合法模式均强制命名空间。"""

    def __init__(self, cache_dir: Path, cache_bytes: int = 2 * 1024**3, max_pixels: int = 24_000_000, *, disk_min_free_bytes: int = 0, sandbox: str | None = None, max_pending: int = 16, preview_effort: int = 0, png_compression: int = 6, raw_high_format: str = "jpeg", allow_source_passthrough: bool = False):
        if not 0 <= preview_effort <= 6 or not 0 <= png_compression <= 9 or raw_high_format not in {"jpeg", "webp"}:
            raise ValueError("invalid media encoder settings")
        self.preview_effort, self.png_compression, self.raw_high_format = int(preview_effort), int(png_compression), raw_high_format
        self.cache_dir = Path(cache_dir).resolve()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_bytes = int(cache_bytes)
        self.max_pixels = int(max_pixels)
        self.disk_min_free_bytes = int(disk_min_free_bytes)
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self.max_pending = max_pending
        if sandbox is not None:
            from .sandbox import SANDBOX_MODES

            if sandbox not in SANDBOX_MODES:
                raise ValueError("sandbox must be auto, namespace, none or None")
        self.sandbox = sandbox
        self.allow_source_passthrough = allow_source_passthrough and sandbox is None
        if self.cache_bytes < 0 or self.max_pixels <= 0:
            raise ValueError("cache_bytes must be non-negative and max_pixels must be positive")
        self._ctx = mp.get_context("spawn")
        self._requests: Any | None = None
        self._results: Any | None = None
        self._process: mp.Process | None = None
        self._reader: threading.Thread | None = None
        self._stop_reader = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._pending_keys: dict[str, str] = {}
        self._request_keys: dict[str, str] = {}
        self._queued: OrderedDict[str, tuple[str, dict[str, Any]]] = OrderedDict()
        self._waiters: dict[str, int] = {}
        self._active_request: str | None = None
        self._aborting_worker = False
        self._inflight = 0
        self._closed = False
        self._lock = asyncio.Lock()
        self._cache_index: OrderedDict[Path, tuple[int, float]] = OrderedDict()
        self._metadata_cache: OrderedDict[tuple[str, tuple[int, int, int]], dict[str, Any]] = OrderedDict()
        self._leases: dict[str, Path] = {}
        self._discard_on_unpin: set[Path] = set()
        self.worker_timeout = 30.0
        self._vips_tmp_dir = self.cache_dir / ".vips-tmp"
        self._sandbox_tmp_dir: Path | None = None
        self._thumbnail_task = None
        self._thumbnail_stop = asyncio.Event()
        self._thumbnail_wake = asyncio.Event()
        self._thumbnail_pending = OrderedDict()
        self._thumbnail_items = iter(())
        self._thumbnail_rescan = False
        self._thumbnail_generation = 0
        self._scan_cache()

    def start_thumbnail_warmup(self, existing, resolve, available) -> None:
        """仅 server 启用：空闲时逐张预制，复用相同沙箱及 LRU 缓存。"""
        self._thumbnail_items = iter(existing())
        self._thumbnail_task = asyncio.create_task(self._warm_thumbnails(existing, resolve, available))

    def queue_thumbnail(self, item) -> None:
        if self._closed or self._thumbnail_task is None:
            return
        if item not in self._thumbnail_pending and len(self._thumbnail_pending) >= 256:
            self._thumbnail_pending.popitem(last=False)
            # 大批上传的溢出项仍在 stored_files 中，排空后分页补扫。
            self._thumbnail_rescan = True
        self._thumbnail_pending[item] = None
        self._thumbnail_wake.set()

    async def _warm_thumbnails(self, existing, resolve, available) -> None:
        while not self._thumbnail_stop.is_set():
            if self._inflight or not available() or (hasattr(os, "getloadavg") and os.getloadavg()[0] > max(1, (os.cpu_count() or 1) * 0.8)):
                try:
                    await asyncio.wait_for(self._thumbnail_stop.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass
                continue
            if self._thumbnail_pending:
                item, _ = self._thumbnail_pending.popitem(last=False)
            else:
                try:
                    item = next(self._thumbnail_items)
                except StopIteration:
                    if self._thumbnail_rescan:
                        self._thumbnail_rescan = False
                        self._thumbnail_items = iter(existing())
                        continue
                    self._thumbnail_wake.clear()
                    await self._thumbnail_wake.wait()
                    continue
                except Exception:
                    self._thumbnail_items = iter(existing())
                    await asyncio.sleep(1)
                    continue
            result = None
            generation = self._thumbnail_generation
            try:
                source, rev = resolve(item)
                result = await self.render(source, rev, max_size=160, quality="preview", idle_only=True)
                if resolve(item) != (source, rev) or generation != self._thumbnail_generation or self._thumbnail_stop.is_set():
                    self.discard(result.path)
            except Exception as exc:
                if result is not None:
                    self.discard(result.path)
                if isinstance(exc, MediaError) and exc.code == "BUSY":
                    self.queue_thumbnail(item)
                # 预制失败不阻断上传；正常取图仍走原来的完整校验与错误响应。
            await asyncio.sleep(0.05)

    def _scan_cache(self) -> None:
        for path in [*self.cache_dir.glob("*.png"), *self.cache_dir.glob("*.webp"), *self.cache_dir.glob("*.jpg"), *self.cache_dir.glob("raw-*.v")]:
            try:
                self._cache_index[path] = (path.stat().st_size, time.monotonic())
            except OSError:
                continue
        self._trim_cache()

    def _trim_cache(self, protected: Path | None = None) -> None:
        total = sum(size for size, _ in self._cache_index.values())
        while total > self.cache_bytes and self._cache_index:
            path = next(
                (
                    candidate
                    for candidate in self._cache_index
                    if candidate != protected and candidate not in self._leases.values()
                ),
                None,
            )
            if path is None:
                break
            size, _ = self._cache_index.pop(path)
            try:
                path.unlink()
            except OSError:
                pass
            total -= size

    def _touch_cache(self, path: Path) -> None:
        if not Path(path).resolve().is_relative_to(self.cache_dir):
            return
        try:
            size = path.stat().st_size
        except OSError:
            return
        self._cache_index.pop(path, None)
        self._cache_index[path] = (size, time.monotonic())
        self._trim_cache(protected=path)

    def pin(self, path: Path) -> str | None:
        """功能说明：钉住一个渲染缓存文件，防止它在 HTTP 流式响应期间被 LRU 清理。

        调用时机：/api 渲染端点拿到 MediaResult 后、FileResponse 开始发送前
        立即调用；必须同步执行，抢在其他请求触发清理之前。

        关键步骤：
        1. 校验缓存文件，可信本机 PNG 原路径无需缓存租约；
        2. 生成一次性租约 token 并登记，顺带刷新其 LRU 位置。

        输出/副作用：返回租约 token；调用方必须在响应结束后调用
        [MediaService.unpin]，否则该文件长期占用缓存额度。
        """
        candidate = Path(path).resolve()
        try:
            candidate.relative_to(self.cache_dir)
        except ValueError as exc:
            if self.allow_source_passthrough and candidate.is_file():
                return None
            raise ValueError("only media cache files can be pinned") from exc
        if not candidate.exists():
            raise FileNotFoundError(candidate)
        token = hashlib.sha256(f"{candidate}:{time.time_ns()}".encode()).hexdigest()[:32]
        self._leases[token] = candidate
        self._touch_cache(candidate)
        return token

    def unpin(self, token: str | None, *, protected: Path | None = None) -> None:
        """功能说明：解除 [MediaService.pin] 的租约，让缓存文件重新参与 LRU 清理。

        调用时机：LeasedFileResponse 发送完成（或中途断开）后的 finally 中。

        关键步骤：
        1. 按 token 删除租约；
        2. 立即执行一次缓存清理。

        输出/副作用：缓存可能回收文件；token 不存在时静默忽略。
        """
        path = self._leases.pop(str(token), None)
        if path in self._discard_on_unpin:
            self.discard(path)
        self._trim_cache(protected=protected)

    def discard(self, path: Path) -> None:
        """失效图片的派生文件立即清理；正在发送的文件在最后一个租约结束后删除。"""
        path = Path(path)
        if not path.resolve().is_relative_to(self.cache_dir):
            return
        if path in self._leases.values():
            self._discard_on_unpin.add(path)
            return
        path.unlink(missing_ok=True)
        self._discard_on_unpin.discard(path)
        self._cache_index.pop(path, None)

    def _ensure_worker(self) -> None:
        """功能说明：确保媒体 worker 子进程存活，不存活则以 spawn 重建一条新代。

        调用时机：[MediaService._submit] 提交新请求前、且当前没有在途的
        同键请求时调用。

        关键步骤：
        1. 服务已关闭则拒绝；旧代进程死亡时先回收其队列与读取线程；
        2. 以 spawn 上下文创建请求/结果队列（上限 max_pending）；
        3. 启动以 [_worker_main] 为目标的守护子进程；
        4. 启动结果读取线程，把队列结果桥接回 asyncio future。

        输出/副作用：新建子进程、队列与守护线程；服务关闭后调用抛 503。
        """
        if self._process is not None and self._process.is_alive():
            return
        if self._closed:
            raise MediaError("media service is closed", "SERVICE_CLOSED", 503)
        # A dead generation may still have a reader blocked on its old result
        # queue.  Reap it before replacing queue/process references.
        if self._process is not None:
            self._dispose_worker_sync()
        if self.sandbox is not None:
            from .sandbox import _namespace_available

            if os.name == "nt":
                if self.sandbox == "namespace":
                    raise MediaError("Windows 不支持 bubblewrap 命名空间隔离", "SANDBOX_FAILED", 503)
            elif not _namespace_available():
                raise MediaError("完整媒体解码隔离不可用，拒绝解码", "SANDBOX_FAILED", 503)
            self._sandbox_tmp_dir = Path(tempfile.mkdtemp(prefix=".media-sandbox-", dir=self.cache_dir))
        self._loop = asyncio.get_running_loop()
        self._requests = self._ctx.Queue(maxsize=self.max_pending)
        self._results = self._ctx.Queue()
        self._stop_reader.clear()
        self._process = self._ctx.Process(
            target=_worker_main,
            args=(self._requests, self._results, self.max_pixels, str(self._sandbox_tmp_dir or self._vips_tmp_dir), self.sandbox)
                 + ((True,) if self.sandbox is None else ()),
            daemon=True,
        )
        self._process.start()
        self._reader = threading.Thread(target=self._result_loop, name="webcompare-media-results", daemon=True)
        self._reader.start()

    def _result_loop(self) -> None:
        assert self._results is not None
        while not self._stop_reader.is_set():
            try:
                item = self._results.get(timeout=0.2)
            except (queue.Empty, EOFError, OSError):
                process = self._process
                if process is not None and not process.is_alive():
                    self._fail_pending(MediaError("media worker stopped", "WORKER_FAILED", 503))
                    return
                continue
            if item is None:
                return
            request_id, result, error = item
            if error is not None:
                value: Any = MediaError(error.message, error.code, error.status)
            else:
                value = result
            self._resolve_from_thread(request_id, value)

    def _resolve_from_thread(self, request_id: str, value: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._resolve, request_id, value)
        except RuntimeError:
            pass

    def _fail_pending(self, error: MediaError) -> None:
        for request_id in list(self._pending):
            self._resolve_from_thread(request_id, error)

    def _resolve(self, request_id: str, value: Any) -> None:
        future = self._pending.pop(request_id, None)
        if future is None:
            return
        key = self._request_keys.pop(request_id, None)
        if key is not None:
            self._pending_keys.pop(key, None)
        self._inflight = max(0, self._inflight - 1)
        self._queued.pop(request_id, None)
        if self._active_request == request_id:
            self._active_request = None
        # Index an active operation even after its final HTTP waiter disconnects.
        if isinstance(value, dict) and value.get("path"):
            output = Path(value["path"])
            if output.parent == self.cache_dir:
                self._touch_cache(output)
        if not future.done():
            if isinstance(value, MediaError):
                future.set_exception(value)
            else:
                future.set_result(value)
        self._dispatch_next()

    def _dispatch_next(self) -> None:
        # Keep waiting work here, where the last cancelled subscriber can remove it.
        if self._closed or self._aborting_worker or self._active_request is not None or not self._queued:
            return
        if self._process is None or not self._process.is_alive():
            return
        request_id, (command, payload) = self._queued.popitem(last=False)
        self._active_request = request_id
        assert self._requests is not None
        self._requests.put_nowait((request_id, command, payload))

    def _dispose_worker_sync(self) -> None:
        """Stop one worker generation and close only its queues/readers."""
        self._stop_reader.set()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        process = self._process
        if process is not None:
            try:
                process.join(timeout=0.2)
            except Exception:
                pass
            if process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1.0)
        for q in (self._requests, self._results):
            if q is not None:
                try:
                    q.close()
                    q.join_thread()
                except Exception:
                    pass
        self._reader = None
        self._process = None
        self._requests = None
        self._results = None
        if self._sandbox_tmp_dir is not None:
            shutil.rmtree(self._sandbox_tmp_dir, ignore_errors=True)
            self._sandbox_tmp_dir = None

    async def _abort_worker(self, error: MediaError) -> None:
        self._aborting_worker = True
        try:
            for request_id in list(self._pending):
                self._resolve(request_id, error)
            await asyncio.to_thread(self._dispose_worker_sync)
            self._stop_reader.clear()
        finally:
            self._aborting_worker = False

    async def _submit(self, key: str, command: str, payload: dict[str, Any], *, idle_only: bool = False) -> Any:
        """功能说明：把一个媒体请求提交给 worker 并等待结果，相同键的请求共享同一 future。

        调用时机：[MediaService.metadata]、[MediaService.render]、
        [MediaService.pixel] 的每次调用。

        关键步骤：
        1. 在锁内按缓存键查重：已有同键在途请求则直接共享其 future；
        2. 在途达到 max_pending 或队列满时抛 429（HTTP 侧转 Retry-After 退避）；
        3. 父进程保留等待队列，每次只投递一个活跃请求给 worker；
        4. 同键订阅共享 future；最后一个订阅取消时删除尚未执行的请求；
        5. 超时则中止整条 worker 代并给所有等待者返回 503。

        输出/副作用：向 worker 队列投递请求；成功返回结果字典，
        失败抛 MediaError（429/503/409/422）。
        """
        if self._closed:
            raise MediaError("media service is closed", "SERVICE_CLOSED", 503)
        async with self._lock:
            if self._aborting_worker:
                raise MediaError("media worker is restarting; retry later", "BUSY", 503)
            if idle_only and self._inflight:
                raise MediaError("media worker is busy", "BUSY", 429)
            existing_id = self._pending_keys.get(key)
            existing = self._pending.get(existing_id) if existing_id is not None else None
            if existing is not None:
                future = existing
                request_id = existing_id
            else:
                if self._inflight >= self.max_pending:
                    raise MediaError("media queue is full; retry later", "BUSY", 429)
                self._ensure_worker()
                assert self._requests is not None
                loop = asyncio.get_running_loop()
                request_id = hashlib.sha256(f"{key}:{time.time_ns()}".encode()).hexdigest()[:24]
                future = loop.create_future()
                self._pending[request_id] = future
                self._pending_keys[key] = request_id
                self._request_keys[request_id] = key
                self._inflight += 1
                self._queued[request_id] = (command, payload)
                self._dispatch_next()
            self._waiters[request_id] = self._waiters.get(request_id, 0) + 1
        try:
            # A caller cancelling its HTTP request must not cancel the shared
            # worker future used by other callers with the same cache key.
            raw_work = command == "raw_convert" or payload.get("raw_metadata") is not None or payload.get("diff_raw_metadata") is not None
            timeout = max(self.worker_timeout, 120.0) if raw_work else self.worker_timeout
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError as exc:
            await self._abort_worker(MediaError("media worker timed out", "WORKER_TIMEOUT", 503))
            raise MediaError("media worker timed out", "WORKER_TIMEOUT", 503) from exc
        finally:
            remaining = self._waiters.get(request_id, 1) - 1
            if remaining:
                self._waiters[request_id] = remaining
            else:
                self._waiters.pop(request_id, None)
                if request_id in self._queued:
                    self._queued.pop(request_id)
                    self._pending.pop(request_id, None)
                    self._request_keys.pop(request_id, None)
                    self._pending_keys.pop(key, None)
                    self._inflight = max(0, self._inflight - 1)
                    future.cancel()
                elif not future.done():
                    # The single running operation may finish and populate cache.
                    future.add_done_callback(lambda done: None if done.cancelled() else done.exception())

    def _key(self, command: str, payload: dict[str, Any]) -> str:
        data = {"command": command, **payload}
        return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()

    async def _raw_context(self, source: Path, raw: dict | None, source_name: str | None = None):
        from .raw import RawError, is_raw, prepare_raw
        if not is_raw(source):
            return None
        try:
            return await asyncio.to_thread(prepare_raw, source, raw, self.max_pixels, source_name=source_name)
        except RawError as exc:
            raise MediaError(exc.message, exc.code, exc.status) from exc

    def _check_raw_context(self, context) -> None:
        if context is None:
            return
        from .raw import RawError, check_context
        try:
            check_context(context)
        except RawError as exc:
            raise MediaError(exc.message, exc.code, exc.status) from exc

    async def _display_source(self, source: Path, raw: dict | None, *, idle_only: bool = False, source_name: str | None = None):
        """Return a leased full RGB cache, or the unchanged static source."""
        source = Path(source)
        context = await self._raw_context(source, raw, source_name)
        if context is None:
            return source, None, None, None
        output = self.cache_dir / f"raw-{context['key']}.v"
        # The worker sees only whitelisted resolved values, never paths to
        # metadata neighbours. This remains true inside the namespace sandbox.
        if not output.is_file() or output.stat().st_size < context["metadata"]["width"] * context["metadata"]["height"] * 3:
            needed = context["metadata"]["width"] * context["metadata"]["height"] * 3 + 1024**2
            if self.disk_min_free_bytes and shutil.disk_usage(self.cache_dir).free - needed < self.disk_min_free_bytes:
                raise MediaError("磁盘剩余空间不足以生成 RAW RGB 缓存", "DISK_FLOOR", 507)
            payload = {"source": str(source), "source_stat": tuple(context["dependencies"][0]["stat"]),
                       "raw_metadata": context["metadata"], "output": str(output)}
            await self._submit(context["key"], "raw_convert", payload, idle_only=idle_only)
        try:
            self._check_raw_context(context)
        except MediaError:
            self.discard(output)
            raise
        self._touch_cache(output)
        lease = self.pin(output)
        return output, context["metadata"], lease, context

    async def metadata(self, source: Path, *, raw: dict | None = None, source_name: str | None = None) -> dict[str, Any]:
        """功能说明：读取源图的尺寸、通道、格式、alpha/ICC 等元数据，不解码全图给调用方。

        调用时机：GET /api/assets/{id}/metadata；前端载入分组后为每路各调一次。

        关键步骤：
        1. 在 HTTP 进程内计算源签名（大小/mtime/inode）；
        2. 按完整路径与源签名复用最近 256 个成功结果，命中时再复核签名；
        3. 未命中经 [MediaService._submit] 交给 worker 读取，再缓存独立副本。

        输出/副作用：返回元数据字典；不产生缓存文件。
        """
        source = Path(source)
        context = await self._raw_context(source, raw, source_name)
        if context is not None:
            self._check_raw_context(context)
            return context["metadata"]
        if self._closed:
            raise MediaError("media service is closed", "SERVICE_CLOSED", 503)
        stat = _stat_signature(source)
        cache_key = (str(source.absolute()), stat)
        cached = self._metadata_cache.get(cache_key)
        if cached is not None:
            if _stat_signature(source) != stat:
                raise MediaError("source changed while processing", "SOURCE_CHANGED", 409)
            self._metadata_cache.move_to_end(cache_key)
            return dict(cached)
        payload = {"source": str(source), "source_stat": stat}
        result = await self._submit(self._key("metadata", payload), "metadata", payload)
        if _stat_signature(source) != stat:
            raise MediaError("source changed while processing", "SOURCE_CHANGED", 409)
        self._metadata_cache[cache_key] = dict(result)
        self._metadata_cache.move_to_end(cache_key)
        while len(self._metadata_cache) > _METADATA_CACHE_MAX_ITEMS:
            self._metadata_cache.popitem(last=False)
        return dict(result)

    async def validate(self, source: Path, *, raw: dict | None = None, source_name: str | None = None) -> dict[str, Any]:
        """功能说明：完整解码一张源图并返回元数据，供上传前校验不可信图片。

        调用时机：需要完整校验媒体时调用；配置 sandbox 的服务始终在
        bubblewrap 内解码，隔离不可用即拒绝，不退化为普通 worker 解码。

        关键步骤：
        1. 在 HTTP 进程内计算源签名（大小/mtime/inode）；
        2. 经 [MediaService._submit] 交给 worker：先做与 [MediaService.metadata]
           完全相同的格式/位深/像素校验，再用 stats() 驱动全图解码；
        3. 解码中发现截断或损坏即回 422 DECODE_ERROR。

        输出/副作用：返回元数据字典；不产生缓存文件。
        """
        source, metadata, lease, context = await self._display_source(Path(source), raw, source_name=source_name)
        try:
            payload = {"source": str(source), "source_stat": _stat_signature(source)}
            if metadata is not None:
                payload["raw_metadata"] = metadata
            result = await self._submit(self._key("validate", payload), "validate", payload)
            self._check_raw_context(context)
            return result
        finally:
            if lease is not None:
                self.unpin(lease)

    async def pixel(self, source: Path, x: int, y: int, *, raw: dict | None = None, source_name: str | None = None) -> dict[str, Any]:
        """功能说明：读取源图单个像素的通道值，供前端取色器展示真实像素。

        调用时机：GET /api/assets/{id}/pixel；用户在工作台悬停取色时调用。

        关键步骤：
        1. 计算源签名并携带坐标提交给 worker；
        2. worker 校验坐标在图内并返回值。

        输出/副作用：返回 {x, y, values}；坐标越界抛 422。
        """
        source, metadata, lease, context = await self._display_source(Path(source), raw, source_name=source_name)
        try:
            payload = {"source": str(source), "source_stat": _stat_signature(source), "x": int(x), "y": int(y)}
            if metadata is not None:
                payload["raw_metadata"] = metadata
            result = await self._submit(self._key("pixel", payload), "pixel", payload)
            self._check_raw_context(context)
            return result
        finally:
            if lease is not None:
                self.unpin(lease)

    async def render(
        self, source: Path, revision: str, *, box: tuple[int, int, int, int] | None = None,
        max_size: int = 1024, quality: str = "preview", full: bool = False,
        diff_source: Path | None = None, diff_revision: str | None = None,
        idle_only: bool = False, raw: dict | None = None,
        source_name: str | None = None, diff_source_name: str | None = None,
        local_lossless: bool = False,
    ) -> MediaResult:
        """Render display sources, reusing one bounded RGB cache per RAW tone."""
        if quality not in {"preview", "high", "exact"} or not isinstance(max_size, int) or not 1 <= max_size <= 2048 or full and quality == "preview":
            raise MediaError("invalid quality/max_size/full combination", "INVALID_ARGUMENT", 422)
        if self._closed:
            raise MediaError("media service is closed", "SERVICE_CLOSED", 503)
        if (self.allow_source_passthrough and local_lossless and quality == "exact" and full
                and box is None and diff_source is None and source_name is None
                and Path(source).suffix.lower() in IMAGE_FORMATS):
            source = Path(source)
            source_stat = _stat_signature(source)
            metadata = await self.metadata(source)
            if _stat_signature(source) != source_stat:
                raise MediaError("source changed while processing", "SOURCE_CHANGED", 409)
            if metadata["format"] in IMAGE_FORMATS.values() and metadata["bit_depth"] == 8:
                key = self._key("source-native-v1", {"source": str(source), "source_stat": source_stat,
                                                     "revision": revision})
                return MediaResult(source, "image/" + metadata["format"], metadata["width"],
                                   metadata["height"], quality, key)
        if (self.allow_source_passthrough and quality == "exact" and full and box is None
                and diff_source is None and source_name is None):
            source = Path(source)
            source_stat = _stat_signature(source)
            dimensions = self._passthrough_png(source)
            if dimensions is not None:
                if _stat_signature(source) != source_stat:
                    raise MediaError("source changed while processing", "SOURCE_CHANGED", 409)
                key = self._key("source-png", {"source": str(source), "source_stat": source_stat,
                                               "revision": revision})
                return MediaResult(source, "image/png", *dimensions, quality, key)
        leases, contexts = [], []
        result = None
        try:
            source, metadata, lease, context = await self._display_source(Path(source), raw, idle_only=idle_only, source_name=source_name)
            if lease is not None:
                leases.append(lease)
                contexts.append(context)
            diff_metadata = None
            if diff_source is not None:
                diff_source, diff_metadata, lease, context = await self._display_source(Path(diff_source), raw, idle_only=idle_only, source_name=diff_source_name)
                if lease is not None:
                    leases.append(lease)
                    contexts.append(context)
            result = await self._render_display(source, revision, box=box, max_size=max_size,
                                                quality=quality, full=full, diff_source=diff_source,
                                                diff_revision=diff_revision, idle_only=idle_only,
                                                raw_metadata=metadata, diff_raw_metadata=diff_metadata,
                                                local_lossless=local_lossless and self.sandbox is None
                                                and source_name is None and diff_source_name is None)
            try:
                for context in contexts:
                    self._check_raw_context(context)
            except MediaError:
                self.discard(result.path)
                raise
            return result
        finally:
            for lease in leases:
                self.unpin(lease, protected=result.path if result is not None else None)

    def _passthrough_png(self, source: Path) -> tuple[int, int] | None:
        # Only RGB8/RGBA8 PNG without color/EXIF metadata agrees with the stripped sRGB display.
        if source.suffix.lower() != ".png":
            return None
        try:
            size = source.stat().st_size
            with source.open("rb") as stream:
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    return None
                dimensions = None
                has_data = False
                for _ in range(16384):
                    header = stream.read(8)
                    if len(header) != 8:
                        return None
                    length, kind = int.from_bytes(header[:4], "big"), header[4:]
                    if stream.tell() + length + 4 > size:
                        return None
                    if dimensions is None:
                        if kind != b"IHDR" or length != 13:
                            return None
                        data = stream.read(13)
                        width, height = int.from_bytes(data[:4], "big"), int.from_bytes(data[4:8], "big")
                        if (data[8] != 8 or data[9] not in {2, 6} or data[10:12] != b"\x00\x00"
                                or data[12] not in {0, 1} or width < 1 or height < 1
                                or width * height > self.max_pixels):
                            return None
                        dimensions = (width, height)
                    elif kind == b"pHYs" and length == 9 and not has_data:
                        stream.seek(length, 1)  # Physical resolution does not change decoded pixels.
                    elif kind == b"IDAT":
                        has_data = True
                        stream.seek(length, 1)
                    elif kind == b"IEND" and length == 0 and has_data:
                        return dimensions if stream.tell() + 4 == size else None
                    else:
                        return None
                    stream.seek(4, 1)
        except OSError:
            return None
        return None

    async def _render_display(
        self,
        source: Path,
        revision: str,
        *,
        box: tuple[int, int, int, int] | None = None,
        max_size: int = 1024,
        quality: str = "preview",
        full: bool = False,
        diff_source: Path | None = None,
        diff_revision: str | None = None,
        idle_only: bool = False,
        raw_metadata: dict | None = None,
        diff_raw_metadata: dict | None = None,
        local_lossless: bool = False,
    ) -> MediaResult:
        """功能说明：渲染图片或差分为缓存 PNG/JPEG/WebP，并返回可流式发送的结果。

        调用时机：GET /api/assets/{id}/render 与 /api/diff；前端载入分组、
        切换画质或请求原图时调用。

        关键步骤：
        1. 校验 quality/max_size/full 参数组合；
        2. 以全部渲染参数与源签名生成缓存键，命中缓存则直接读文件头
           取尺寸返回（PNG/JPEG/WebP 头解析，不引入图像库）；
        3. 未命中则经 [MediaService._submit] 交 worker 执行 [_worker_render]；
        4. 结果纳入 LRU 缓存索引并按字节上限清理。

        输出/副作用：可能新增缓存文件并清理最旧的缓存；返回 MediaResult
        （路径/MIME/尺寸/质量/ETag 键），调用方负责 pin 后再流式发送。
        """
        source = Path(source)
        source_stat = _stat_signature(source)
        diff_stat = None
        if diff_source is not None:
            diff_source = Path(diff_source)
            diff_stat = _stat_signature(diff_source)
        encoding = "png" if quality == "exact" else "webp"
        if quality == "high":
            if raw_metadata is not None or diff_raw_metadata is not None:
                encoding = self.raw_high_format
            elif diff_source is not None:
                encoding = "jpeg"
            else:
                metadata = await self.metadata(source)
                encoding = "png" if metadata["has_alpha"] else "jpeg"
        ext = "jpg" if encoding == "jpeg" else encoding
        payload: dict[str, Any] = {
            "render_version": _RENDER_CACHE_VERSION,
            "source": str(source),
            "source_stat": source_stat,
            "revision": str(revision),
            "box": tuple(box) if box is not None else None,
            "max_size": max_size,
            "quality": quality,
            "full": bool(full),
            "diff_source": str(diff_source) if diff_source is not None else None,
            "diff_stat": diff_stat,
            "diff_revision": str(diff_revision) if diff_revision is not None else None,
        }
        if quality == "preview":
            payload["preview_effort"] = self.preview_effort
        if encoding == "png":
            # Loopback full-size lossless display avoids zlib work; remote traffic keeps compression.
            payload["png_compression"] = 0 if local_lossless and quality == "exact" and full else self.png_compression
        if raw_metadata is not None:
            payload["raw_metadata"] = raw_metadata
        if diff_raw_metadata is not None:
            payload["diff_raw_metadata"] = diff_raw_metadata
        if quality == "high":
            payload.update(encoding=encoding, encoding_version="2")
        key = self._key("render", payload)
        output = self.cache_dir / f"{key}.{ext}"
        payload["output"] = str(output)
        if output.exists():
            self._touch_cache(output)
            # A cached render was keyed by source stat and all render arguments.
            # The current stat was captured immediately above, so it is safe.
            dimensions = self._cached_dimensions(output, quality)
            if dimensions is not None:
                return MediaResult(output, "image/" + encoding, dimensions[0], dimensions[1], quality, key)
            # Unknown/corrupt headers are regenerated by the bounded worker;
            # never return a result with fabricated zero dimensions.
            try:
                output.unlink()
            except OSError:
                pass
            self._cache_index.pop(output, None)
        if self.disk_min_free_bytes and shutil.disk_usage(self.cache_dir).free - self.max_pixels * 4 * (3 if diff_source else 2) < self.disk_min_free_bytes:
            raise MediaError("磁盘剩余空间不足以生成媒体缓存", "DISK_FLOOR", 507)
        result = await self._submit(key, "render", payload, idle_only=idle_only)
        result_path = Path(result["path"])
        self._touch_cache(result_path)
        return MediaResult(result_path, result["mime"], int(result["width"]), int(result["height"]), result["quality"], key)

    def _cached_dimensions(self, path: Path, quality: str) -> tuple[int, int] | None:
        # Read dimensions from the encoded header without importing an image
        # library in the parent. PNG/JPEG and the VP8X/VP8/VP8L forms emitted
        # by libwebp are recognised.
        try:
            with path.open("rb") as handle:
                data = handle.read(64 * 1024)
            if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
                return (int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big"))
            if data[:2] == b"\xff\xd8":
                offset = 2
                while offset + 4 <= len(data) and data[offset] == 0xFF:
                    while offset < len(data) and data[offset] == 0xFF:
                        offset += 1
                    if offset >= len(data):
                        break
                    marker = data[offset]
                    offset += 1
                    if marker in {0xD9, 0xDA}:
                        break
                    length = int.from_bytes(data[offset:offset + 2], "big")
                    if length < 2 or offset + length > len(data):
                        break
                    if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 8:
                        return int.from_bytes(data[offset + 5:offset + 7], "big"), int.from_bytes(data[offset + 3:offset + 5], "big")
                    offset += length
            if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
                kind = data[12:16]
                if kind == b"VP8X" and len(data) >= 30:
                    return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
                if kind == b"VP8L" and len(data) >= 25:
                    bits = int.from_bytes(data[21:25], "little")
                    return (1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF))
                if kind == b"VP8 " and len(data) >= 30:
                    return (int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(data[28:30], "little") & 0x3FFF)
        except OSError:
            pass
        return None

    def cache_stats(self) -> dict[str, int]:
        """功能说明：统计磁盘缓存当前占用（字节数/文件数），供设置页展示。

        调用时机：GET /api/cache。

        关键步骤：
        1. 累加缓存索引中各文件大小与条目数。

        输出/副作用：返回 {bytes, files}；只读。
        """
        return {"bytes": sum(size for size, _ in self._cache_index.values()), "files": len(self._cache_index)}

    async def clear_cache(self, *, defer_leased: bool = False) -> dict[str, int]:
        """功能说明：清空元数据与磁盘缓存（正在流式发送的租约文件除外）。

        调用时机：POST /api/cache/clear；设置页“清理缓存”。

        关键步骤：
        1. 遍历缓存索引与缓存目录中的 png/webp/gif，跳过被 pin 的文件；
        2. 仅在文件删除成功或已不存在时移除对应索引。

        输出/副作用：删除缓存文件；返回 {removed, bytes}。
        """
        self._metadata_cache.clear()
        self._thumbnail_generation += 1
        self._thumbnail_pending.clear()
        self._thumbnail_items = iter(())
        self._thumbnail_rescan = False
        removed = total = 0
        leased = set(self._leases.values())
        paths = set(self._cache_index)
        for pattern in ("*.webp", "*.png", "*.gif", "*.jpg", "raw-*.v"):
            paths.update(self.cache_dir.glob(pattern))
        for path in paths:
            if path in leased:
                if defer_leased:
                    self._discard_on_unpin.add(path)
                continue
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                self._cache_index.pop(path, None)
                continue
            except OSError:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                self._cache_index.pop(path, None)
            except OSError:
                continue
            else:
                self._cache_index.pop(path, None)
                removed += 1
                total += size
        return {"removed": removed, "bytes": total}

    async def close(self) -> None:
        if self._closed:
            return
        if self._thumbnail_task is not None:
            self._thumbnail_stop.set()
            self._thumbnail_wake.set()
            await self._thumbnail_task
        self._closed = True
        for request_id in list(self._pending):
            self._resolve(request_id, MediaError("media service is closed", "SERVICE_CLOSED", 503))
        await asyncio.to_thread(self._dispose_worker_sync)

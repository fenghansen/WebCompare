"""本机 NVDEC 拉帧。调用方完成路径授权，并串行使用每个 reader。"""
from __future__ import annotations

import struct
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path

import numpy as np

from .host_memory import MEMORY_RESERVE_BYTES, available_memory_bytes

FRAME_HEADER = struct.Struct("<qHHI")  # PTS 微秒、宽、高、I420 payload 字节数。
_IDENTITY_DISPLAY_MATRIX = struct.pack("=9i", 1 << 16, 0, 0, 0, 1 << 16, 0, 0, 0, 1 << 30)
_memory_lock = threading.Lock()
_pending_bytes = 0


class NVDECError(RuntimeError):
    """当前容器或硬件无法供给可用的解码帧。"""


class NVDECMemoryError(NVDECError):
    """继续解码和封包将占用预留的可用物理内存。"""


@contextmanager
def _reserve_host_memory(required: int):
    global _pending_bytes
    with _memory_lock:
        available = available_memory_bytes()
        if available is not None and available - _pending_bytes - required < MEMORY_RESERVE_BYTES:
            raise NVDECMemoryError("可用物理内存不足以继续 NVDEC 解码并保留 512 MiB")
        _pending_bytes += required
    try:
        yield
    finally:
        with _memory_lock:
            _pending_bytes -= required


def _cuda(device: str | None, *, primary: bool = True):
    from av.codec.hwaccel import HWAccel

    return HWAccel("cuda", device=device, allow_software_fallback=False,
                   options={"primary_ctx": "1"} if primary else None)


def _open_input(path: Path, hwaccel=None):
    import av

    return av.open(str(path), mode="r", options={"protocol_whitelist": "file", "format_whitelist": "mov,matroska,webm"},
                   hwaccel=hwaccel)


def _open_nvdec(path: Path, device: str | None):
    try:
        from av import FFmpegError
    except ImportError as exc:
        raise NVDECError("未安装可供 NVDEC 使用的 PyAV") from exc
    try:
        return _open_input(path, _cuda(device))
    except FFmpegError:
        # A caller in this process can already have different primary context flags.
        return _open_input(path, _cuda(device, primary=False))


def _preview_height(width: int, height: int, preview_width: int) -> int:
    return max(1, (height * preview_width + width // 2) // width)


def _open_resized_nvdec(path: Path, device: str | None, preview_width: int):
    import av

    # The container supplies headers and timestamps; the explicit CUVID decoder
    # owns the single hardware context and resizes before downloading the frame.
    with ExitStack() as cleanup:
        container = _open_input(path)
        cleanup.callback(container.close)
        videos = container.streams.video
        if len(videos) != 1:
            return None
        stream = videos[0]
        source = stream.codec_context
        if (source.name != "h264" or not source.extradata or not source.format or
                source.format.name not in {"yuv420p", "yuvj420p", "nv12"} or
                not 0 < preview_width < source.width):
            return None
        matrix = source.coded_side_data.get("display_matrix")
        if matrix is not None and matrix != _IDENTITY_DISPLAY_MATRIX:
            return None
        height = _preview_height(source.width, source.height, preview_width)
        dimensions = ((preview_width + 1) & ~1, (height + 1) & ~1)
        decoder = av.CodecContext.create("h264_cuvid", "r", hwaccel=_cuda(device))
        decoder.extradata = source.extradata
        decoder.options = {"resize": f"{dimensions[0]}x{dimensions[1]}"}
        decoder.open()
        if "resize" in decoder.options or not decoder.is_hwaccel:
            return None
        cleanup.pop_all()
        return container, decoder, dimensions


def _decode_resized(container, stream, decoder):
    source_geometry = None
    for packet in container.demux(stream):
        if (packet.has_sidedata("display_matrix") and
                bytes(packet.get_sidedata("display_matrix")) != _IDENTITY_DISPLAY_MATRIX):
            raise NVDECError("NVDEC 暂不支持带显示变换的视频")
        for frame in decoder.decode(packet if packet.size else None):
            geometry = (decoder.coded_width, decoder.coded_height, decoder.sample_aspect_ratio)
            if source_geometry is not None and geometry != source_geometry:
                raise NVDECError("NVDEC 暂不支持中途改变分辨率的视频")
            source_geometry = geometry
            if not frame.time_base:
                frame.time_base = stream.time_base
            yield frame


def _planes(width: int, height: int) -> tuple[tuple[int, int], ...]:
    chroma = ((width + 1) // 2, (height + 1) // 2)
    return (width, height), chroma, chroma


def _payload_bytes(width: int, height: int) -> int:
    size = sum(stride * rows for stride, rows in _planes(width, height))
    if not (0 < width <= 0xFFFF and 0 < height <= 0xFFFF and size <= 0xFFFFFFFF):
        raise NVDECError("视频尺寸超出 I420 帧头可表示的范围")
    return size


def _host_frame_bytes(width: int, height: int) -> int:
    # 下载或缩放后的 YUV420 帧可能按行宽和高度对齐。
    return ((width + 63) // 64 * 64) * ((height + 31) // 32 * 32) * 3 // 2


def _pts_us(frame) -> int:
    base = frame.time_base
    if frame.pts is None or not base or base.numerator <= 0 or base.denominator <= 0:
        raise NVDECError("NVDEC 帧缺少有效时间戳")
    value, remainder = divmod(frame.pts * base.numerator * 1_000_000, base.denominator)
    # 和浏览器 Math.round 一致，恰好半微秒时向 +∞ 取整。
    return value + (remainder * 2 >= base.denominator)


def _pack_frame(frame, pts: int, width: int, height: int, size: int) -> memoryview:
    if (width, height) != (frame.width, frame.height):
        # libswscale 将主机上的 NV12 同时缩放并拆成 Y/U/V。
        frame = frame.reformat(width=width, height=height, format="yuv420p", interpolation="AREA",
                               src_color_range=frame.color_range, dst_color_range=frame.color_range, threads=1)
        planes = tuple((plane, 0, 1) for plane in frame.planes)
    else:
        y, uv = frame.planes
        planes = ((y, 0, 1), (uv, 0, 2), (uv, 1, 2))
    packet = bytearray(FRAME_HEADER.size + size)
    FRAME_HEADER.pack_into(packet, 0, pts, width, height, size)
    offset = FRAME_HEADER.size
    for (plane, start, step), (row_bytes, rows) in zip(planes, _planes(width, height), strict=True):
        if plane.line_size < row_bytes * step:
            raise NVDECError("NVDEC 帧行宽无效")
        shape = (rows, row_bytes)
        source = np.ndarray(shape, dtype=np.uint8, buffer=plane, offset=start, strides=(plane.line_size, step))
        dest = np.ndarray(shape, dtype=np.uint8, buffer=packet, offset=offset)
        np.copyto(dest, source)
        offset += row_bytes * rows
    return memoryview(packet).toreadonly()


class NVDECReader:
    """按原始容器 PTS 拉取紧凑 8-bit I420 帧。"""

    def __init__(self, path: Path, start_us: int, device: str | None = None, *, preview_width: int | None = None):
        if type(start_us) is not int or not 0 <= start_us < 2**63:
            raise NVDECError("NVDEC 起始时间戳无效")
        if preview_width is not None and (type(preview_width) is not int or preview_width <= 0):
            raise NVDECError("NVDEC 预览宽度无效")
        self._path, self._device = path, device
        self._container = None
        self._codec = None
        self._frames = None
        self._start_us = start_us
        self._preview_width = preview_width
        self._resized = None
        self._last_pts = None
        self._last_frame = None
        self._color = None
        self._initialize(preview_width)

    def _initialize(self, preview_width: int | None) -> None:
        try:
            with _reserve_host_memory(0):
                if preview_width is not None:
                    try:
                        self._resized = _open_resized_nvdec(self._path, self._device, preview_width)
                    except MemoryError:
                        raise
                    except Exception:
                        self._resized = None
                if self._resized is None:
                    self._container = _open_nvdec(self._path, self._device)
                else:
                    self._container, self._codec, _ = self._resized
            videos = self._container.streams.video
            if len(videos) != 1:
                raise NVDECError("NVDEC 需要单一视频轨")
            stream = videos[0]
            base, ratio = stream.time_base, stream.sample_aspect_ratio
            if not base or base.numerator <= 0 or base.denominator <= 0:
                raise NVDECError("视频轨缺少有效时基")
            if ratio and ratio.numerator != ratio.denominator:
                raise NVDECError("NVDEC 暂不支持非方形像素视频")
            codec = stream.codec_context
            if self._codec is None:
                self._codec = codec
            if not self._codec.is_hwaccel:
                raise NVDECError("视频轨未启用 NVIDIA 硬件解码")
            self._width, self._height = codec.width, codec.height
            self._size = _payload_bytes(self._width, self._height)
            self._decoded_size = (self._width, self._height) if self._resized is None else self._resized[2]
            if self._resized is None:
                self._host_bytes = _host_frame_bytes(max(self._width, codec.coded_width),
                                                     max(self._height, codec.coded_height))
            else:
                self._host_bytes = _host_frame_bytes(*self._decoded_size)
            self._allocation = self._host_bytes + self._size + FRAME_HEADER.size
            start_us = self._start_us
            offset = start_us * base.denominator // (1_000_000 * base.numerator)
            if start_us and (stream.start_time is None or offset > stream.start_time):
                self._container.seek(offset, backward=True, any_frame=False, stream=stream)
            if self._resized is None:
                self._frames = self._container.decode(stream)
            else:
                self._frames = _decode_resized(self._container, stream, self._codec)
        except Exception as exc:
            self.close()
            if isinstance(exc, NVDECError):
                raise
            raise NVDECError("无法初始化 NVIDIA NVDEC") from exc

    def accepts_preview_width(self, preview_width: int | None) -> bool:
        """同 reader 只能继续提供同一硬件预览尺寸，完整解码可用 CPU 缩放。"""
        if self._resized is not None:
            return preview_width == self._preview_width
        return self._preview_width is not None or preview_width is None or preview_width >= self._width

    def _output(self, preview_width: int | None) -> tuple[int, int, int, int]:
        if preview_width is not None and (type(preview_width) is not int or preview_width <= 0):
            raise NVDECError("NVDEC 预览宽度无效")
        if self._resized is not None and not self.accepts_preview_width(preview_width):
            raise NVDECError("NVDEC 预览尺寸变更需要重新打开解码器")
        if preview_width is None or preview_width >= self._width:
            return self._width, self._height, self._size, self._allocation - self._host_bytes
        width = preview_width
        height = _preview_height(self._width, self._height, width)
        size = _payload_bytes(width, height)
        work_bytes = 0 if (width, height) == self._decoded_size else _host_frame_bytes(width, height)
        return width, height, size, work_bytes + size + FRAME_HEADER.size

    def current_frame(self, preview_width: int | None = None) -> memoryview | None:
        """以新的输出尺寸返回最近解码的一帧。"""
        if self._last_frame is None:
            return None
        width, height, size, allocation = self._output(preview_width)
        try:
            with _reserve_host_memory(allocation):
                return _pack_frame(self._last_frame, self._last_pts, width, height, size)
        except NVDECError:
            raise
        except Exception as exc:
            raise NVDECError("无法转换最近解码的 NVDEC 帧") from exc

    def next_frame(self, preview_width: int | None = None) -> memoryview | None:
        """返回独立的 16 字节帧头和紧凑 Y/U/V 数据。EOF 时释放解码器。"""
        if self._frames is None:
            return None
        width, height, size, allocation = self._output(preview_width)
        try:
            with _reserve_host_memory(self._host_bytes + allocation):
                for frame in self._frames:
                    pts = _pts_us(frame)
                    if pts < self._start_us:
                        continue
                    if self._last_pts is not None and pts <= self._last_pts:
                        raise NVDECError("NVDEC 帧时间戳重复或逆序")
                    if not self._codec.is_hwaccel or frame.format.name != "nv12":
                        raise NVDECError("NVDEC 没有返回 8-bit NV12 硬件解码帧")
                    matrix = frame.side_data.get("DISPLAYMATRIX")
                    if frame.rotation or (matrix is not None and bytes(matrix) != _IDENTITY_DISPLAY_MATRIX):
                        raise NVDECError("NVDEC 暂不支持带显示变换的视频")
                    if (frame.width, frame.height) != self._decoded_size:
                        raise NVDECError("NVDEC 暂不支持中途改变分辨率的视频")
                    color = (frame.color_range, frame.colorspace, frame.color_primaries, frame.color_trc)
                    if self._color is not None and color != self._color:
                        raise NVDECError("NVDEC 暂不支持中途改变色彩参数的视频")
                    packet = _pack_frame(frame, pts, width, height, size)
                    self._last_pts, self._color = pts, color
                    self._last_frame = frame
                    return packet
        except NVDECMemoryError:
            raise
        except Exception as exc:
            resized = self._resized is not None
            self.close()
            if not resized:
                if isinstance(exc, NVDECError):
                    raise
                raise NVDECError("NVIDIA NVDEC 解码失败") from exc
        else:
            self.close()
            return None
        if self._last_pts is not None:
            self._start_us = self._last_pts + 1
        self._color = None
        self._initialize(None)
        return self.next_frame(preview_width)

    def close(self) -> None:
        frames, self._frames = self._frames, None
        container, self._container = self._container, None
        self._codec = None
        self._resized = None
        self._last_frame = None
        try:
            if frames is not None:
                frames.close()
        finally:
            if container is not None:
                container.close()

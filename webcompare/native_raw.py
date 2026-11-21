"""可选 AX RAW10 C 内核：只加载包内动态库，缺失时交由 NumPy 路径处理。"""
from __future__ import annotations

import ctypes
import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _library():
    root = Path(__file__).resolve().parent / "native"
    for name in ("_raw10.so", "_raw10.dll", "_raw10.dylib"):
        path = root / name
        if not path.is_file():
            continue
        lib = ctypes.CDLL(str(path))
        lib.wc_raw10_abi.restype = ctypes.c_int
        if lib.wc_raw10_abi() != 1:
            raise RuntimeError("RAW10 native ABI mismatch; rebuild the package-local library")
        lib.wc_unpack_ax10.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint16), ctypes.c_size_t,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
        lib.wc_unpack_ax10.restype = ctypes.c_int
        if hasattr(lib, "wc_demosaic_rgb8"):
            lib.wc_demosaic_rgb8.argtypes = [ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint, ctypes.c_uint,
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_float),
                ctypes.POINTER(ctypes.c_float), ctypes.POINTER(ctypes.c_uint16)]
            lib.wc_demosaic_rgb8.restype = ctypes.c_int
        return lib
    return None


def available() -> bool:
    return os.getenv("WEBCOMPARE_RAW_NATIVE", "1") != "0" and _library() is not None


def unpack_ax_lsb10(payload, width: int, height: int, stride_pixels: int):
    """返回独立 H×W uint16；库未安装/显式禁用时返回 None，不修改输入。

    由 RAW 解码层在像素预算、元数据和源版本检查后调用。C 层再次校验
    payload 与输出的精确长度；ctypes 调用释放 GIL，无逐像素 Python 循环。
    """
    import numpy as np

    if any(type(v) is not int or not 0 < v <= 0xFFFFFFFF for v in (width, height, stride_pixels)):
        raise ValueError("invalid RAW10 dimensions")
    if stride_pixels < width or stride_pixels % 4:
        raise ValueError("RAW10 stride must be >= width and divisible by four")
    packed = np.frombuffer(payload, dtype=np.uint8)
    if packed.size != height * (stride_pixels // 4) * 5:
        raise ValueError("RAW10 payload length mismatch")
    if not available():
        return None
    out = np.empty((height, width), dtype=np.uint16)
    code = _library().wc_unpack_ax10(
        packed.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), packed.size,
        out.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)), out.size,
        width, height, stride_pixels)
    if code != 0:
        raise ValueError("native RAW10 rejected dimensions or payload")
    return out


@lru_cache(maxsize=16)
def _gamma_thresholds(gamma: float):
    """Find the exact float32 inputs at each NumPy round-to-even transition."""
    import numpy as np

    levels = np.arange(1, 256)
    values = np.power((levels.astype(np.float64) - .5) / 255, gamma).astype(np.float32)

    def transfer(x):
        x = x.copy()
        np.power(x, 1 / gamma, out=x)
        x *= 255
        return np.rint(x)

    for _ in range(64):
        previous = np.nextafter(values, np.float32(-np.inf))
        too_low = transfer(values) < levels
        too_high = transfer(previous) >= levels
        if not (too_low.any() or too_high.any()):
            bins = np.searchsorted(values, np.arange(257, dtype=np.float32) / 256,
                                   side="right").astype(np.uint16)
            return values, bins
        values[too_low] = np.nextafter(values[too_low], np.float32(np.inf))
        values[too_high] = previous[too_high]
    # An unusual NumPy/libm implementation must use the reference transfer.
    return None


def demosaic_bilinear(array, bayer: str, *, black_level: float, white_level: float,
                      gain: float, gamma: float, white_balance: tuple[float, float, float],
                      black_levels: tuple[float, float, float, float] | None = None):
    """同一双线性算法的融合 C 实现；Gamma 量化沿用 NumPy float32 边界。"""
    import numpy as np

    if not available() or not hasattr(_library(), "wc_demosaic_rgb8"):
        return None
    levels = (black_level,) * 4 if black_levels is None else tuple(black_levels)
    if (array.ndim != 2 or min(array.shape) < 2 or len(white_balance) != 3
            or len(levels) != 4
            or bayer not in ("RGGB", "BGGR", "GRBG", "GBRG")
            or not np.isfinite([*levels, white_level, gain, gamma, *white_balance]).all()
            or white_level <= max(levels) or gain <= 0 or gamma <= 0 or min(white_balance) <= 0):
        raise ValueError("invalid native Bayer display parameters")
    thresholds = _gamma_thresholds(float(gamma))
    if thresholds is None:
        return None
    m = np.array(array, dtype=np.float32, order="C", copy=True)
    # Phase order is spatial TL, TR, BL, BR, independent of the Bayer pattern.
    # Keep the existing scalar operation order and feed the same normalized
    # float32 mosaic into the C interpolation/transfer kernel.
    planes = [(m, levels[0])] if len(set(levels)) == 1 else [
        (m[index // 2::2, index % 2::2], level) for index, level in enumerate(levels)]
    for plane, level in planes:
        np.subtract(plane, level, out=plane)
        np.maximum(plane, 0, out=plane)
        np.multiply(plane, gain / (white_level - level), out=plane)
        np.clip(plane, 0, 1, out=plane)
    if not np.isfinite(m).all():
        raise ValueError("non-finite Bayer display values")
    height, width = m.shape
    output = np.empty((height, width, 3), dtype=np.uint8)
    cfa = (ctypes.c_int * 4)(*("RGB".index(c) for c in bayer))
    wb = np.asarray(white_balance, dtype=np.float64) / 4
    wb = wb.astype(np.float32)
    values, bins = thresholds
    result = _library().wc_demosaic_rgb8(
        m.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        output.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)), width, height, cfa,
        wb.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        values.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
        bins.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)))
    if result:
        raise ValueError("native demosaic rejected image layout")
    return output

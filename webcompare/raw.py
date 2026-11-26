"""Safe RAW layout resolution and full resolution Bayer display conversion.

Only the HTTP process reads fixed, adjacent RAW/CaptureAX metadata names. It passes
the selected numeric layout to the media worker, so isolated decoders never
discover or open neighbouring files. NumPy is imported only during conversion.
The signed source remains untouched; clipping belongs to the RGB display copy.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import stat
import struct
from pathlib import Path
from typing import Any

RAW_SUFFIXES = frozenset({".bayer", ".raw", ".npy"})
_LAYOUT_KEYS = ("width", "height", "stride_pixels", "bit_depth", "packing", "cfa", "bayer_pattern")
_METADATA_LIMIT = 2 * 1024**2
_JOURNAL_LIMIT = 64 * 1024**2
_CONVERSION_VERSION = "2"
_GENERIC_KEYS = ("bayer", "packing", "width", "height", "stride_pixels", "bit_depth",
                 "black_level", "black_levels", "white_level", "white_balance")
_TONE_KEYS = ("black_level", "black_levels", "white_level", "white_balance")


class RawError(Exception):
    def __init__(self, message: str, code: str = "INVALID_RAW", status: int = 422):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_SUFFIXES


def _signature(path: Path) -> tuple[int, int, int]:
    try:
        stat = path.stat()
    except OSError as exc:
        raise RawError("RAW 源文件或解析信息已改变，请重新读取目录", "SOURCE_CHANGED", 409) from exc
    return int(stat.st_size), int(stat.st_mtime_ns), int(getattr(stat, "st_ino", 0))


def check_context(context: dict[str, Any]) -> None:
    for dependency in context["dependencies"]:
        if _signature(Path(dependency["path"])) != tuple(dependency["stat"]):
            raise RawError("RAW 源文件或解析信息已改变，请重新读取目录", "SOURCE_CHANGED", 409)


def _no_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def _layout(record: Any) -> dict[str, Any]:
    """Whitelist layout fields; never expose unrelated session data."""
    if not isinstance(record, dict):
        return {}
    output = {}
    for key in _LAYOUT_KEYS:
        value = record.get(key)
        if key in {"width", "height", "stride_pixels", "bit_depth"}:
            if type(value) is int and 0 < value <= 100000:
                output[key] = value
        elif isinstance(value, str) and len(value) <= 32:
            output[key] = value
    return output


def _sidecar(source: Path, name: str, dependencies: list[dict]) -> Path | None:
    candidate = source.parent / name
    # A sidecar is a fixed regular neighbour, never a user/manifest supplied
    # path, symlink, or route out of the authorised source directory.
    if candidate.is_symlink() or not candidate.is_file():
        return None
    dependencies.append({"path": str(candidate), "stat": _signature(candidate)})
    return candidate


def _open_sidecar(path: Path, expected: tuple[int, int, int]):
    """Open the snapshotted regular file; reject link/replacement races."""
    descriptor = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        current = os.fstat(descriptor)
        signature = (int(current.st_size), int(current.st_mtime_ns), int(getattr(current, "st_ino", 0)))
        if path.is_symlink() or not stat.S_ISREG(current.st_mode) or signature != tuple(expected):
            raise RawError("RAW 解析信息在读取前已改变", "SOURCE_CHANGED", 409)
        stream = os.fdopen(descriptor, "r", encoding="utf-8")
        descriptor = None
        return stream
    except OSError as exc:
        raise RawError("RAW 解析信息无法安全读取", "RAW_METADATA_INVALID") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_json_sidecar(path: Path, dependency: dict) -> Any:
    if dependency["stat"][0] > _METADATA_LIMIT:
        raise RawError(f"{path.name} 过大，RAW 元数据最多 2 MiB", "RAW_METADATA_LIMIT")
    try:
        with _open_sidecar(path, dependency["stat"]) as stream:
            return json.loads(stream.read(_METADATA_LIMIT + 1), parse_constant=_no_constant)
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise RawError(f"{path.name} 无法解析为有效 JSON", "RAW_METADATA_INVALID") from exc


def _validated_fields(record: dict, keys: tuple, label: str, warnings: list[str], *, strict: bool) -> dict:
    from .models import RawOptions
    from pydantic import ValidationError

    output = {}
    for key in keys:
        if record.get(key) is None:
            continue
        try:
            output[key] = RawOptions.model_validate({key: record[key]}).model_dump(mode="json")[key]
        except ValidationError as exc:
            if strict:
                raise RawError(f"{label} 中 {key} 不合法，请使用有限数值及正确的通道数量", "RAW_METADATA_INVALID") from exc
            warnings.append(f"{label} 的 {key} 不合法，已忽略该自动参数。")
    return output


def _read_generic(source: Path, dependencies: list[dict], warnings: list[str]) -> tuple[list[tuple[str, dict]], list[str]]:
    layers, origins = [], []
    for name in ("raw.json", source.name + ".json"):
        path = _sidecar(source, name, dependencies)
        if path is None:
            continue
        record = _read_json_sidecar(path, dependencies[-1])
        schema = record.get("schema") if isinstance(record, dict) else None
        if schema != "webcompare.raw.v1":
            if isinstance(schema, str) and schema.startswith("webcompare.raw."):
                raise RawError(f"{name} 的 schema 版本不支持，应为 webcompare.raw.v1", "RAW_METADATA_INVALID")
            warnings.append(f"已忽略 {name}：未声明 schema=webcompare.raw.v1，未将其作为 RAW 参数。")
            continue
        label = ("目录侧车 " if name == "raw.json" else "文件侧车 ") + name
        selected = _validated_fields(record, _GENERIC_KEYS, label, warnings, strict=True)
        if "black_level_applied" in record:
            if type(record["black_level_applied"]) is not bool:
                raise RawError(f"{name} 的 black_level_applied 必须是 true 或 false", "RAW_METADATA_INVALID")
            selected["black_level_applied"] = record["black_level_applied"]
        layers.append((label, selected))
        origins.append(name)
    return layers, origins


def _capture_tone(record: Any, label: str, warnings: list[str]) -> dict:
    if not isinstance(record, dict):
        return {}
    selected = {key: record[key] for key in _TONE_KEYS if key in record}
    black_source = str(record.get("black_level_source") or "").lower()
    if any(token in black_source for token in ("engineering", "placeholder", "unknown", "占位")):
        if selected.get("black_level") is not None or selected.get("black_levels") is not None:
            warnings.append(f"{label} 的黑位来源为工程占位或未知，未作为真实 BL 使用。")
        selected.pop("black_level", None)
        selected.pop("black_levels", None)
    for key in ("white_level", "white_balance"):
        origin = str(record.get(key + "_source") or "").lower()
        if any(token in origin for token in ("engineering", "placeholder", "unknown", "占位")):
            if selected.get(key) is not None:
                warnings.append(f"{label} 的 {key} 为占位或未知，已忽略。")
            selected.pop(key, None)
    if any(key.startswith("sdk_") and ("black" in key or "white" in key) for key in record):
        warnings.append("SDK reported 黑白位未用于显示：其 DN 单位和相位顺序未经核验，不推测移位或缩放。")
    return _validated_fields(selected, _TONE_KEYS, label, warnings, strict=False)


def _read_capture(source: Path, dependencies: list[dict], warnings: list[str], *, layout_required: bool, source_name: str) -> tuple[list[tuple[str, dict]], list[str]]:
    layers: list[tuple[str, dict]] = []
    origins: list[str] = []
    capture_app = False
    meta_path = _sidecar(source, "meta.json", dependencies)
    if meta_path is not None:
        try:
            manifest = _read_json_sidecar(meta_path, dependencies[-1])
        except RawError as exc:
            if layout_required or exc.code == "SOURCE_CHANGED":
                raise
            warnings.append("已忽略无法读取的 CaptureAX meta.json；布局已显式指定，自动 BL/WB 将使用其他来源或回退。")
            manifest = None
        if isinstance(manifest, dict):
            capture_app = manifest.get("app") == "CaptureAX"
            if capture_app:
                label = "CaptureAX meta.json requested"
                tone = _capture_tone(manifest.get("requested"), label, warnings)
                if tone:
                    layers.append((label, tone))
            record = manifest.get("frame_metadata")
            label = ("CaptureAX " if capture_app else "") + "meta.json frame_metadata"
            selected = _layout(record)
            if capture_app:
                selected.update(_capture_tone(record, label, warnings))
            if selected:
                layers.append((label, selected))
            frames = manifest.get("frames")
            if isinstance(frames, list):
                for record in frames[:10000]:
                    if isinstance(record, dict) and record.get("file") == source_name:
                        label = ("CaptureAX " if capture_app else "") + f"meta.json frames[{source_name}]"
                        selected = _layout(record)
                        if capture_app:
                            selected.update(_capture_tone(record, label, warnings))
                        if selected:
                            layers.append((label, selected))
                        break
            if layers:
                origins.append("meta.json")
    # The full ledger is authoritative for the selected physical frame. A
    # bounded streaming scan avoids loading a whole capture journal into RAM.
    if source.suffix.lower() != ".npy":
        journal = _sidecar(source, "frames.jsonl", dependencies)
        if journal is not None:
            try:
                if dependencies[-1]["stat"][0] > _JOURNAL_LIMIT:
                    raise RawError("frames.jsonl 过大，请显式填写 RAW 解析参数", "RAW_METADATA_LIMIT")
                with _open_sidecar(journal, dependencies[-1]["stat"]) as stream:
                    for index in range(100001):
                        line = stream.readline(65537)
                        if not line:
                            break
                        if index >= 100000 or len(line) > 65536:
                            raise RawError("RAW 逐帧信息超出读取上限，请显式填写参数", "RAW_METADATA_LIMIT")
                        record = json.loads(line, parse_constant=_no_constant)
                        if isinstance(record, dict) and record.get("file") == source_name:
                            label = ("CaptureAX " if capture_app or record.get("app") == "CaptureAX" else "") + f"frames.jsonl [{source_name}]"
                            selected = _layout(record)
                            if capture_app or record.get("app") == "CaptureAX":
                                selected.update(_capture_tone(record, label, warnings))
                            if selected:
                                layers.append((label, selected))
                            origins.append("frames.jsonl")
                            break
            except (OSError, ValueError, UnicodeError, RawError) as exc:
                if layout_required or isinstance(exc, RawError) and exc.code == "SOURCE_CHANGED":
                    if isinstance(exc, RawError):
                        raise
                    raise RawError("frames.jsonl 无法解析，请显式填写 RAW 参数", "RAW_METADATA_INVALID") from exc
                warnings.append("已忽略无法读取的 CaptureAX frames.jsonl；布局已显式指定，自动 BL/WB 将回退。")
    return layers, origins


def _npy_layout(source: Path) -> dict[str, Any]:
    """Read a bounded primitive NPY header without importing NumPy in HTTP."""
    try:
        with source.open("rb") as stream:
            if stream.read(6) != b"\x93NUMPY":
                raise RawError("不是有效的 NPY 数组")
            version = tuple(stream.read(2))
            if version == (1, 0):
                length_data = stream.read(2)
                length = struct.unpack("<H", length_data)[0]
            elif version in {(2, 0), (3, 0)}:
                length_data = stream.read(4)
                length = struct.unpack("<I", length_data)[0]
            else:
                raise RawError("不支持的 NPY 文件版本")
            if not 0 < length <= 16384:
                raise RawError("NPY header 超出安全读取上限")
            header = stream.read(length)
            if len(header) != length:
                raise RawError("NPY header 已截断")
            info = ast.literal_eval(header.decode("utf-8" if version == (3, 0) else "latin1"))
            offset = stream.tell()
    except RawError:
        raise
    except (OSError, ValueError, UnicodeError, SyntaxError, struct.error, RecursionError) as exc:
        raise RawError("无法读取 NPY header") from exc
    if not isinstance(info, dict) or set(info) != {"descr", "fortran_order", "shape"}:
        raise RawError("NPY header 字段不合法")
    shape, dtype = info["shape"], info["descr"]
    if (not isinstance(shape, tuple) or len(shape) != 2
            or any(type(value) is not int or not 2 <= value <= 100000 for value in shape)
            or type(info["fortran_order"]) is not bool or not isinstance(dtype, str)
            or re.fullmatch(r"[<>=|]?(?:[ui][1248]|f[248])", dtype) is None):
        raise RawError("NPY 只接受不含 pickle 的二维整数或浮点 Bayer 数组", "UNSUPPORTED_RAW_ARRAY")
    itemsize = int(dtype[-1])
    if source.stat().st_size != offset + shape[0] * shape[1] * itemsize:
        raise RawError("NPY 数据长度与声明尺寸不符")
    return {"height": shape[0], "width": shape[1], "dtype": dtype, "shape": list(shape)}


def prepare_raw(source: Path, options: dict | None, max_pixels: int | None, *, source_name: str | None = None) -> dict[str, Any] | None:
    # SSH cache names can differ on Windows; frame ledgers keep the original name.
    if not is_raw(source):
        return None
    expected = _signature(source)
    try:
        context = _prepare_raw(source, options, max_pixels, source_name=source_name)
    except RawError:
        if _signature(source) != expected:
            raise RawError("RAW 源文件在读取期间已改变", "SOURCE_CHANGED", 409)
        raise
    if _signature(source) != expected:
        raise RawError("RAW 源文件在读取期间已改变", "SOURCE_CHANGED", 409)
    return context


def _pick(key: str, layers: list[tuple[str, dict]]) -> tuple[Any, str | None]:
    for label, record in reversed(layers):
        value = record.get(key)
        if key == "bayer" and value is None:
            value = record.get("cfa") or record.get("bayer_pattern")
        if value is not None and value != "auto":
            return value, label
    return None, None


def _black_from_layers(layers: list[tuple[str, dict]]) -> tuple[Any, Any, str | None]:
    applied = None
    for label, record in reversed(layers):
        if applied is None and "black_level_applied" in record:
            applied = record["black_level_applied"]
            if applied:
                return 0.0, None, label + "（black_level_applied=true，源已扣黑）"
        # Four measured phases take precedence over a coexisting scalar
        # placeholder in the same record. A higher layer wins as a whole.
        if record.get("black_levels") is not None:
            levels = record["black_levels"]
            return sum(levels) / 4.0, levels, label
        if record.get("black_level") is not None:
            return record["black_level"], None, label
    return None, None, None


def _prepare_raw(source: Path, options: dict | None, max_pixels: int | None, *, source_name: str | None = None) -> dict[str, Any] | None:
    """Resolve one layout and snapshot every file that can affect auto values."""
    if not is_raw(source):
        return None
    from .models import RawOptions
    from pydantic import ValidationError

    try:
        params = RawOptions.model_validate(options or {}).model_dump(mode="json")
    except ValidationError as exc:
        raise RawError("RAW 参数不合法", "INVALID_ARGUMENT") from exc
    dependencies = [{"path": str(source), "stat": _signature(source)}]
    warnings: list[str] = []
    parameter_sources: dict[str, str] = {}
    npy = _npy_layout(source) if source.suffix.lower() == ".npy" else None
    generic, generic_origins = _read_generic(source, dependencies, warnings)

    def preliminary(key):
        value = params[key]
        return value if value is not None and value != "auto" else _pick(key, generic)[0]

    layout_required = preliminary("bayer") is None or npy is None and any(
        preliminary(key) is None for key in ("packing", "width", "height"))
    needs_capture = layout_required or preliminary("bit_depth") is None
    if npy is None and params["stride_pixels"] is None and _pick("stride_pixels", generic)[0] is None:
        needs_capture = True
    if params["black_level"] is None and params["black_levels"] is None and _black_from_layers(generic)[0] is None:
        needs_capture = True
    if params["white_level"] is None and _pick("white_level", generic)[0] is None:
        needs_capture = True
    if params["white_balance"] is None and _pick("white_balance", generic)[0] is None:
        needs_capture = True
    capture, capture_origins = _read_capture(source, dependencies, warnings, layout_required=layout_required, source_name=source_name or source.name) if needs_capture else ([], [])
    layers = capture + generic
    origins = capture_origins + generic_origins
    resolved = dict(params)
    for key in ("bayer", "packing", "width", "height", "stride_pixels", "bit_depth"):
        if params[key] is not None and params[key] != "auto":
            parameter_sources[key] = "手动参数"
        else:
            resolved[key], origin = _pick(key, layers)
            if origin is not None:
                parameter_sources[key] = origin
    if npy is not None:
        for key in ("width", "height"):
            if resolved[key] is not None and resolved[key] != npy[key]:
                raise RawError("显式 RAW 宽高与 NPY shape 不一致")
            resolved[key] = npy[key]
            parameter_sources[key] = "NPY header shape"
        resolved["stride_pixels"] = resolved["width"]
        resolved["packing"] = "npy"
        parameter_sources["stride_pixels"] = "NPY header shape（无额外行 padding）"
        parameter_sources["packing"] = "NPY header dtype"
    else:
        if resolved["stride_pixels"] is None:
            resolved["stride_pixels"] = resolved["width"]
            parameter_sources["stride_pixels"] = "回退：使用图像宽度"
    resolved["bayer"] = str(resolved["bayer"] or "").upper()
    if resolved["bit_depth"] is None:
        resolved["bit_depth"] = 10
        parameter_sources["bit_depth"] = "回退：假定 10 bit"
        warnings.append("未提供 RAW 位深，暂按 10 bit 解析；请按源数据实际位深设置。")
    if resolved["bayer"] not in {"RGGB", "BGGR", "GRBG", "GBRG"} or any(resolved[key] is None for key in ("width", "height", "packing")):
        raise RawError("RAW 缺少解析信息，请设置 Bayer、宽高与 packing 后重试", "RAW_METADATA_REQUIRED")
    width, height, stride, depth = (resolved[key] for key in ("width", "height", "stride_pixels", "bit_depth"))
    if (any(type(value) is not int for value in (width, height, stride, depth))
            or min(width, height) < 2 or stride < width or stride > 100000 or not 1 <= depth <= 16):
        raise RawError("RAW 宽高、stride 或位深不合法")
    if max_pixels is not None and (width * height > max_pixels or stride * height > max_pixels + height * 512):
        raise RawError("RAW 超出像素或 stride 内存上限", "PIXEL_LIMIT")
    if npy is None:
        packing = resolved["packing"]
        if packing in {"ax_lsb10", "mipi_raw10"}:
            if depth != 10 or stride % 4:
                raise RawError("RAW10 要求 bit_depth=10 且 stride_pixels 可被 4 整除")
            expected = height * (stride // 4) * 5
        elif packing in {"uint16le", "float32le"}:
            expected = height * stride * (2 if packing == "uint16le" else 4)
        else:
            raise RawError("未知 RAW packing，请显式指定解析方式", "RAW_METADATA_REQUIRED")
        if dependencies[0]["stat"][0] != expected:
            raise RawError("RAW 文件长度与宽高/stride/packing 不符，请调整解析参数")
    if params["black_level"] is not None:
        black, levels, black_origin = params["black_level"], None, "手动 scalar 黑位"
    elif params["black_levels"] is not None:
        levels = params["black_levels"]
        black, black_origin = sum(levels) / 4.0, "手动四相黑位（左上/右上/左下/右下）"
    else:
        black, levels, black_origin = _black_from_layers(layers)
        if black is None:
            black, levels, black_origin = 0.0, None, "回退：缺少真实 BL，使用 0 DN"
            warnings.append("未提供真实 RAW 黑位，自动 BL 回退为 0 DN；可手动设置或提供 metadata。")
    resolved["black_level"], resolved["black_levels"] = black, levels
    parameter_sources["black_level"] = black_origin + ("（四相均值，仅用于表示）" if levels is not None else "")
    parameter_sources["black_levels"] = black_origin if levels is not None else "未使用四相 BL：" + black_origin
    for key in ("white_level", "white_balance"):
        if params[key] is not None:
            resolved[key], origin = params[key], "手动参数"
        else:
            resolved[key], origin = _pick(key, layers)
        if resolved[key] is None:
            if key == "white_level":
                resolved[key], origin = float((1 << depth) - 1), f"回退：{depth} bit 满量程"
                warnings.append(f"未提供 RAW 白位，自动 white_level 使用 {depth} bit 满量程 {resolved[key]:g} DN。")
            else:
                resolved[key], origin = [1.0, 1.0, 1.0], "回退：缺少 WB，使用 RGB 单位增益"
                warnings.append("未提供线性 RGB 白平衡增益，自动 WB 回退为 [1,1,1]，色彩未做白平衡。")
        parameter_sources[key] = origin
    parameter_sources["gain"] = "手动显示增益" if "gain" in (options or {}) else "默认显示增益 1"
    parameter_sources["gamma"] = "手动显示 Gamma" if "gamma" in (options or {}) else "默认显示 Gamma 2.2"
    effective_levels = levels if levels is not None else [black]
    if resolved["white_level"] <= max(effective_levels):
        raise RawError("RAW white_level 必须大于 black_level 和每个四相黑位")
    for value in effective_levels:
        scale = resolved["gain"] / (resolved["white_level"] - value)
        if not math.isfinite(scale) or scale > 3.4e38:
            raise RawError("RAW 黑白位间隔过小，显示增益超出范围")
    dtype = npy["dtype"] if npy else "<f4" if resolved["packing"] == "float32le" else "<u2"
    raw_meta = {"resolved": resolved, "source_dtype": dtype, "source_shape": [height, width],
                "metadata_source": list(dict.fromkeys(origins + (["npy header"] if npy else []))),
                "parameter_sources": parameter_sources, "warnings": list(dict.fromkeys(warnings))}
    metadata = {"width": width, "height": height, "bands": 3, "format": "raw", "has_alpha": False, "has_icc": False, "bit_depth": 8, "raw": raw_meta}
    key_data = {"version": _CONVERSION_VERSION, "source": str(source), "dependencies": dependencies, "resolved": resolved}
    key = hashlib.sha256(json.dumps(key_data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    context = {"key": key, "metadata": metadata, "dependencies": dependencies}
    check_context(context)
    return context


def unpack_raw(data: bytes, resolved: dict):
    """Decode explicitly declared rows, retaining only visible pixel columns."""
    import numpy as np

    width, height, stride = (resolved[k] for k in ("width", "height", "stride_pixels"))
    packing = resolved["packing"]
    if packing in {"uint16le", "float32le"}:
        dtype = "<u2" if packing == "uint16le" else "<f4"
        expected = height * stride * np.dtype(dtype).itemsize
        if len(data) != expected:
            raise RawError("RAW 数据长度已改变", "SOURCE_CHANGED", 409)
        array = np.frombuffer(data, dtype=dtype).reshape(height, stride)[:, :width].copy()
        if packing == "uint16le" and np.any(array > (1 << resolved["bit_depth"]) - 1):
            raise RawError("RAW 数值超出声明位深")
        return array
    if len(data) != height * (stride // 4) * 5:
        raise RawError("RAW 数据长度已改变", "SOURCE_CHANGED", 409)
    if packing == "ax_lsb10":
        try:
            from . import native_raw
        except ImportError:
            native_raw = None
        native = getattr(native_raw, "unpack_ax_lsb10", None)
        if native is not None:
            result = native(data, width, height, stride)
            if result is not None:
                return result
    packed = np.frombuffer(data, dtype=np.uint8).reshape(height, stride // 4, 5).astype(np.uint16)
    output = np.empty((height, stride // 4, 4), dtype=np.uint16)
    if packing == "ax_lsb10":
        output[..., 0] = packed[..., 0] | ((packed[..., 1] & 3) << 8)
        output[..., 1] = (packed[..., 1] >> 2) | ((packed[..., 2] & 15) << 6)
        output[..., 2] = (packed[..., 2] >> 4) | ((packed[..., 3] & 63) << 4)
        output[..., 3] = (packed[..., 3] >> 6) | (packed[..., 4] << 2)
    elif packing == "mipi_raw10":
        for index in range(4):
            output[..., index] = (packed[..., index] << 2) | ((packed[..., 4] >> (2 * index)) & 3)
    else:
        raise RawError("不支持的 RAW packing")
    return output.reshape(height, stride)[:, :width].copy()


def load_raw_array(source: str, metadata: dict):
    import numpy as np

    resolved = metadata["raw"]["resolved"]
    if resolved["packing"] == "npy":
        try:
            array = np.load(source, mmap_mode="r", allow_pickle=False)
        except (OSError, ValueError) as exc:
            raise RawError("无法安全读取 NPY 数组") from exc
        if (array.ndim != 2 or list(array.shape) != metadata["raw"]["source_shape"]
                or array.dtype.kind not in "uif" or array.dtype.hasobject):
            raise RawError("NPY 数组 shape/dtype 已改变", "SOURCE_CHANGED", 409)
    else:
        array = unpack_raw(Path(source).read_bytes(), resolved)
    # Check in bounded row blocks, including signed float inputs, before any
    # display clipping can conceal NaN or infinity.
    for start in range(0, array.shape[0], 128):
        if not np.isfinite(array[start:start + 128]).all():
            raise RawError("RAW 数组含 NaN 或 Infinity，无法显示")
    return array


def demosaic_bilinear(array, bayer: str, *, black_level: float, white_level: float,
                      gain: float, gamma: float, white_balance: tuple[float, float, float],
                      black_levels: tuple[float, float, float, float] | None = None):
    """Full size bilinear RGB8 display, preserving every measured CFA sample."""
    import numpy as np

    try:
        from . import native_raw
    except ImportError:
        native_raw = None
    native = getattr(native_raw, "demosaic_bilinear", None)
    if native is not None:
        result = native(array, bayer, black_level=black_level, white_level=white_level,
                        gain=gain, gamma=gamma, white_balance=white_balance, black_levels=black_levels)
        if result is not None:
            return result
    mosaic = array.astype(np.float32, copy=True)
    if black_levels is None or all(value == black_levels[0] for value in black_levels):
        scalar = black_level if black_levels is None else black_levels[0]
        np.subtract(mosaic, scalar, out=mosaic)
        np.maximum(mosaic, 0, out=mosaic)
        np.multiply(mosaic, gain / (white_level - scalar), out=mosaic)
    else:
        for index, level in enumerate(black_levels):
            y, x = divmod(index, 2)
            phase = mosaic[y::2, x::2]
            np.subtract(phase, level, out=phase)
            np.maximum(phase, 0, out=phase)
            np.multiply(phase, gain / (white_level - level), out=phase)
    # Finite high-range integer/float sources may overflow float32 during
    # display transfer. Saturate only this display copy before interpolation.
    np.clip(mosaic, 0, 1, out=mosaic)
    height, width = mosaic.shape
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    for channel, color in enumerate("RGB"):
        plane = np.zeros((height, width), dtype=np.float32)
        for index, name in enumerate(bayer):
            if name == color:
                y, x = divmod(index, 2)
                plane[y::2, x::2] = mosaic[y::2, x::2]
        padded = np.pad(plane, 1, mode="reflect")
        del plane
        result = padded[1:-1, 1:-1].copy()
        result *= 4
        if color == "G":
            for view in (padded[:-2, 1:-1], padded[2:, 1:-1], padded[1:-1, :-2], padded[1:-1, 2:]):
                result += view
        else:
            for view in (padded[:-2, 1:-1], padded[2:, 1:-1], padded[1:-1, :-2], padded[1:-1, 2:]):
                result += view * 2
            for view in (padded[:-2, :-2], padded[:-2, 2:], padded[2:, :-2], padded[2:, 2:]):
                result += view
        del padded
        result *= white_balance[channel] / 4
        np.clip(result, 0, 1, out=result)
        np.power(result, 1 / gamma, out=result)
        result *= 255
        np.rint(result, out=result)
        rgb[..., channel] = result.astype(np.uint8)
    return rgb

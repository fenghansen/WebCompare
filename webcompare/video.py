"""【模块】视频离线拆帧 — 把有界本地视频导入为普通 PNG 序列目录

功能说明：用 ffprobe/ffmpeg 把一个小视频离线拆成 PNG 帧目录，生成的目录
与普通图片目录走同一套分组对位逻辑。刻意不支持 URL/stdin，也不是在线
播放器或后台服务；建议在服务进程之外运行，避免与服务竞争内存。

## 流程
所属流程：视频离线拆帧（webcompare/video.py）

1. **导入入口**：参数校验、本地路径限制与输出目录保护 → 见函数 [import_video]
2. **探针与流校验**：ffprobe 读取流元数据、拒绝高bit/HDR → 见函数 [_probe]、[_validate_stream]
3. **解码执行**：ffmpeg 拆帧、PTS 透传与中断处置 → 见函数 [_run_checked]、[_parse_showinfo]
4. **状态落盘**：sequence.json 原子写入与失败/中断记录 → 见函数 [_write_json]、[_failure_status]
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from pathlib import Path
from typing import Any


class VideoImportError(RuntimeError):
    """A video could not be inspected or imported."""


_PTS_RE = re.compile(
    r"\bn:\s*(?P<n>\d+).*?\bpts:\s*(?P<pts>-?\d+)"
    r".*?\bpts_time:\s*(?P<pts_time>[-+0-9.eE]+|N/A)"
)
_URI_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_HIGH_BIT_PIX_FMT_RE = re.compile(
    r"(?:^|[a-z])(?:9|10|12|14|16|32|48|64)(?:le|be)?$"
)
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "smpte428", "pq", "hlg"}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    """功能说明：把 sequence.json 状态原子写入输出目录。

    调用时机：导入开始、探针完成、成功完成与失败/中断时各写一次。

    关键步骤：
    1. 写同目录临时文件；
    2. os.replace 原子替换，读者永远看到完整 JSON。

    输出/副作用：创建/替换输出目录里的 sequence.json。
    """

    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _failure_status(payload: dict[str, Any], message: str, status: str = "failed") -> None:
    """功能说明：把失败/中断状态尽力写入 sequence.json，让半成品目录可诊断。

    调用时机：[import_video] 任何失败分支在抛出异常之前调用。

    关键步骤：
    1. 在状态负载上标记 status 与 error；
    2. 尝试原子写盘；磁盘满等失败时静默放弃——真正的异常由调用方抛出。

    输出/副作用：可能更新 sequence.json；不抛新异常。
    """
    payload["status"] = status
    payload["error"] = message
    try:
        _write_json(Path(payload["sequence_json"]), payload)
    except OSError:
        # The useful exception is raised by the caller.  If the disk is full,
        # there may be no room even for the failure record.
        pass


def _run_checked(command: list[str]) -> tuple[int, str, str]:
    """功能说明：以无 shell、无继承 stdin 的方式运行 ffmpeg，并妥善处理中断。

    调用时机：[import_video] 启动解码时调用一次。

    关键步骤：
    1. 以 DEVNULL stdin、捕获 stdout/stderr 启动子进程；ffmpeg 不存在时
       转成带安装提示的 VideoImportError；
    2. 等待结束；收到键盘中断时先 terminate、必要时 kill，不让解码器
       继续往半成品序列里写帧。

    输出/副作用：返回 (退出码, stdout, stderr)；KeyboardInterrupt 会
    在终止子进程后继续向上抛。
    """

    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise VideoImportError(
            "ffmpeg is not installed or is not on PATH; install ffmpeg and ffprobe first"
        ) from exc
    try:
        stdout, stderr = process.communicate()
    except KeyboardInterrupt:
        # Do not leave a decoder writing into the partial sequence after the
        # caller has cancelled the import.
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    return process.returncode, stdout, stderr


def _probe(source: Path) -> dict[str, Any]:
    """功能说明：用 ffprobe 读取首个视频流的元数据（编码、尺寸、像素格式、色彩与帧率）。

    调用时机：[import_video] 在解码前调用一次。

    关键步骤：
    1. 以 JSON 输出运行 ffprobe（无 shell、无继承 stdin）；
    2. 非零退出、无流或结构非法都转成 VideoImportError。

    输出/副作用：返回视频流元数据字典；只读操作。
    """
    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        (
            "stream=codec_name,codec_long_name,width,height,pix_fmt,"
            "bits_per_raw_sample,color_range,color_space,color_transfer,"
            "color_primaries,time_base,avg_frame_rate,r_frame_rate,"
            "duration,nb_frames"
        ),
        "-of",
        "json",
        str(source),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise VideoImportError(
            "ffprobe is not installed or is not on PATH; install ffmpeg and ffprobe first"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "ffprobe failed"
        raise VideoImportError(f"cannot inspect video with ffprobe: {detail}")
    try:
        streams = json.loads(completed.stdout).get("streams", [])
        stream = streams[0]
    except (ValueError, IndexError, TypeError, AttributeError) as exc:
        raise VideoImportError("ffprobe returned no video stream metadata") from exc
    if not isinstance(stream, dict):
        raise VideoImportError("ffprobe returned invalid video stream metadata")
    return stream


def _validate_stream(stream: dict[str, Any]) -> None:
    """功能说明：校验视频流是否在支持范围内——常见 8-bit SDR、尺寸不超过 24 MP。

    调用时机：[import_video] 在 [_probe] 之后、启动解码之前调用。

    关键步骤：
    1. 校验宽高有效且总像素不超过 2400 万；
    2. 按 bits_per_raw_sample 与像素格式名拒绝 9bit 以上高位深；
    3. 按色彩传输函数拒绝 HDR（PQ/HLG 等）。

    输出/副作用：不合规抛 VideoImportError；无其他副作用。
    """
    try:
        width = int(stream.get("width", 0))
        height = int(stream.get("height", 0))
    except (TypeError, ValueError) as exc:
        raise VideoImportError("video has invalid dimensions") from exc
    if width <= 0 or height <= 0:
        raise VideoImportError("video has no usable video dimensions")
    if width * height > 24_000_000:
        raise VideoImportError("video exceeds the 24 MP image limit")

    pix_fmt = str(stream.get("pix_fmt") or "").lower()
    try:
        raw_bits = int(stream.get("bits_per_raw_sample") or 0)
    except (TypeError, ValueError):
        raw_bits = 0
    if raw_bits > 8 or _HIGH_BIT_PIX_FMT_RE.search(pix_fmt):
        raise VideoImportError("only common 8-bit SDR video is supported; high-bit-depth video was rejected")
    if any(token in pix_fmt for token in ("p010", "p012", "p016", "10le", "10be", "12le", "12be")):
        raise VideoImportError("only common 8-bit SDR video is supported; high-bit-depth video was rejected")

    transfer = str(stream.get("color_transfer") or "").lower()
    if transfer in _HDR_TRANSFERS:
        raise VideoImportError("HDR video is not supported; convert it to 8-bit SDR first")


def _parse_showinfo(stderr: str) -> list[dict[str, Any]]:
    """功能说明：从 ffmpeg showinfo 滤镜的 stderr 日志解析每帧的 PTS 时间戳。

    调用时机：[import_video] 解码结束后调用，把帧文件与源时间戳对齐。

    关键步骤：
    1. 逐行匹配 n/pts/pts_time 字段；
    2. 按解码顺序返回时间戳列表（VFR 源不做 CFR 转换）。

    输出/副作用：返回 [{"pts", "pts_time"}...]；纯函数。
    """
    frames: list[dict[str, Any]] = []
    for line in stderr.splitlines():
        match = _PTS_RE.search(line)
        if not match:
            continue
        pts_text = match.group("pts_time")
        frames.append(
            {
                "pts": int(match.group("pts")),
                "pts_time": None if pts_text == "N/A" else float(pts_text),
            }
        )
    return frames


def _source_is_local(raw: str) -> bool:
    if raw == "-" or raw.lower() in {"stdin", "pipe:0", "pipe:1"}:
        return False
    # Keep Windows drive paths local while rejecting http:, rtmp:, file:, etc.
    if re.match(r"^[A-Za-z]:[\\/]", raw):
        return True
    return not bool(_URI_RE.match(raw)) and "://" not in raw


def import_video(
    source: Path,
    output: Path,
    *,
    start: float = 0,
    duration: float | None = None,
    max_frames: int = 100,
) -> dict[str, Any]:
    """功能说明：把一个有界本地视频离线拆成 PNG 序列目录（含 sequence.json 清单）。

    帧时间戳取自 ffmpeg showinfo 滤镜；输出不做 -r/CFR 转换，VFR 输入保留
    原始解码时间戳。返回字典与写入的 sequence.json 内容一致。

    调用时机：CLI 的 `webcompare import-video` 子命令；建议在服务进程之外
    运行，避免与在线服务竞争内存。

    关键步骤：
    1. 校验 start/duration/max_frames 为有限数值、来源为本地路径
       （拒绝 URL/stdin），输出目录必须不存在（防覆盖）；
    2. 先写 running 状态的 sequence.json，再 [_probe] + [_validate_stream]；
    3. 构造 ffmpeg 命令：-copyts 保 PTS、单线程、-frames:v 硬上限、
       showinfo 记录时间戳、PNG 编码输出；
    4. 解码前后各比对一次源文件签名，混合字节的结果不允许发布；
    5. 结束后核对帧数上限与时间戳数量，写 complete 状态清单；
    6. 任何失败/中断都先经 [_failure_status] 落盘再抛出。

    输出/副作用：新建输出目录与 frame_*.png、sequence.json；失败时目录
    保留可诊断的失败状态；源视频只读。
    """

    if (
        isinstance(start, bool)
        or not isinstance(start, (int, float))
        or not math.isfinite(float(start))
        or start < 0
    ):
        raise ValueError("start must be a non-negative number")
    if duration is not None and (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration <= 0
    ):
        raise ValueError("duration must be a positive number when provided")
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or max_frames <= 0:
        raise ValueError("max_frames must be a positive integer")

    raw_source = os.fspath(source)
    if not _source_is_local(raw_source):
        raise ValueError("video source must be a local filesystem path, not a URL or stdin")
    source_path = Path(source).expanduser()
    try:
        source_path = source_path.resolve(strict=True)
        source_stat = source_path.stat()
    except OSError as exc:
        raise VideoImportError(f"video source is not readable: {source}") from exc
    if not source_path.is_file():
        raise VideoImportError(f"video source is not a regular file: {source}")

    output_path = Path(output).expanduser()
    try:
        output_path.mkdir()
    except FileExistsError as exc:
        raise FileExistsError(f"refusing to overwrite existing output directory: {output_path}") from exc
    except OSError as exc:
        raise VideoImportError(f"cannot create output directory {output_path}: {exc}") from exc

    sequence_json = output_path / "sequence.json"
    payload: dict[str, Any] = {
        "schema": 1,
        "status": "running",
        "sequence_json": str(sequence_json),
        "source": {
            "path": str(source_path),
            "size": source_stat.st_size,
            "mtime_ns": source_stat.st_mtime_ns,
        },
        "start": float(start),
        "duration": None if duration is None else float(duration),
        "max_frames": max_frames,
        "frames": [],
    }
    try:
        _write_json(sequence_json, payload)
    except OSError as exc:
        raise VideoImportError(f"cannot write import status in {output_path}: {exc}") from exc

    try:
        stream = _probe(source_path)
        _validate_stream(stream)
        payload["source"].update(
            {
                "width": int(stream["width"]),
                "height": int(stream["height"]),
            }
        )
        payload["decode"] = {
            key: stream.get(key)
            for key in (
                "codec_name",
                "codec_long_name",
                "pix_fmt",
                "bits_per_raw_sample",
                "color_range",
                "color_space",
                "color_transfer",
                "color_primaries",
                "time_base",
                "avg_frame_rate",
                "r_frame_rate",
                "duration",
                "nb_frames",
            )
        }
        _write_json(sequence_json, payload)
    except Exception as exc:
        _failure_status(payload, str(exc))
        if isinstance(exc, (VideoImportError, ValueError)):
            raise
        raise VideoImportError(str(exc)) from exc

    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-ss",
        str(float(start)),
        "-copyts",
        "-i",
        str(source_path),
    ]
    if duration is not None:
        # With -copyts, -to is an absolute source timestamp.  This preserves
        # source PTS while still making duration a hard end bound.
        command.extend(["-to", str(float(start) + float(duration))])
    command.extend(
        [
            "-map",
            "0:v:0",
            "-threads",
            "1",
            "-frames:v",
            str(max_frames),
            "-fps_mode",
            "passthrough",
            "-vf",
            "showinfo",
            "-start_number",
            "0",
            "-c:v",
            "png",
            str(output_path / "frame_%06d.png"),
        ]
    )

    # Probe and decode are separate subprocesses.  Refuse to start decoding
    # if the source changed between them, and check again after ffmpeg exits so
    # a completed-looking sequence can never be published from mixed bytes.
    try:
        current_stat = source_path.stat()
    except OSError:
        current_stat = None
    if current_stat is None or (
        current_stat.st_size != source_stat.st_size
        or current_stat.st_mtime_ns != source_stat.st_mtime_ns
        or getattr(current_stat, "st_ino", 0) != getattr(source_stat, "st_ino", 0)
    ):
        message = "video source changed before decoding"
        _failure_status(payload, message)
        raise VideoImportError(message)

    try:
        returncode, _stdout, stderr = _run_checked(command)
    except KeyboardInterrupt:
        _failure_status(payload, "video import interrupted", "interrupted")
        raise
    except Exception as exc:
        _failure_status(payload, str(exc))
        raise

    try:
        current_stat = source_path.stat()
    except OSError:
        current_stat = None
    if current_stat is None or (
        current_stat.st_size != source_stat.st_size
        or current_stat.st_mtime_ns != source_stat.st_mtime_ns
        or getattr(current_stat, "st_ino", 0) != getattr(source_stat, "st_ino", 0)
    ):
        message = "video source changed during decoding"
        _failure_status(payload, message)
        raise VideoImportError(message)

    files = sorted(output_path.glob("frame_*.png"))
    timestamps = _parse_showinfo(stderr)
    if returncode != 0:
        detail = next((line.strip() for line in reversed(stderr.splitlines()) if line.strip()), "ffmpeg failed")
        message = f"ffmpeg failed with exit code {returncode}: {detail}"
        _failure_status(payload, message)
        raise VideoImportError(message)
    if len(files) > max_frames:
        message = "ffmpeg produced more frames than the requested hard limit"
        _failure_status(payload, message)
        raise VideoImportError(message)
    # showinfo may inspect one additional frame while the image2 muxer drains
    # its encoder.  The emitted files are the authoritative hard limit; their
    # timestamps are the first corresponding showinfo records.
    if len(files) > len(timestamps):
        message = f"could not preserve PTS for all decoded frames ({len(files)} files, {len(timestamps)} timestamps)"
        _failure_status(payload, message)
        raise VideoImportError(message)
    timestamps = timestamps[: len(files)]
    if not files:
        message = "video interval produced no frames"
        _failure_status(payload, message)
        raise VideoImportError(message)

    payload["frames"] = [
        {
            "index": index,
            "file": path.name,
            "pts": timestamps[index]["pts"],
            "pts_time": timestamps[index]["pts_time"],
        }
        for index, path in enumerate(files)
    ]
    payload["status"] = "complete"
    payload.pop("error", None)
    try:
        _write_json(sequence_json, payload)
    except OSError as exc:
        # The PNGs remain inspectable, but completion cannot be claimed when
        # its manifest was not durably written.
        _failure_status(payload, f"cannot finalize sequence.json: {exc}")
        raise VideoImportError(f"cannot finalize sequence.json: {exc}") from exc
    return payload


__all__ = ["VideoImportError", "import_video"]

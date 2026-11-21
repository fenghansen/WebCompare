"""媒体请求的一次性子进程：Linux 用命名空间，Windows auto 用临时目录与受限环境。"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

from .media import _WorkerMediaError, _worker_main
from .sandbox import BWRAP_PREFIX, _namespace_available, _runtime_args


SAFE_ENV = {
    "PATH": "/usr/bin:/bin", "HOME": "/tmp", "VIPS_CONCURRENCY": "1",
    "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
    "PYTHONDONTWRITEBYTECODE": "1",
    "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
}


def _namespace_args(payload: dict[str, Any], max_pixels: int, output_dir: Path) -> tuple[list[str], dict[str, Any]]:
    mapped = dict(payload)
    argv = [*BWRAP_PREFIX, *_runtime_args(), "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/input"]
    for field in ("source", "diff_source"):
        if not payload.get(field):
            continue
        source = Path(payload[field]).resolve(strict=True)
        if not source.is_file():
            raise _WorkerMediaError("source is not a regular file", "SOURCE_ERROR", 422)
        inside = f"/input/{field}{source.suffix.lower()}"
        argv.extend(("--ro-bind", str(source), inside))
        mapped[field] = inside
    argv.extend(("--bind", str(output_dir), "/output", "--chdir", "/tmp"))
    if "output" in mapped:
        mapped["output"] = "/output/result" + Path(payload["output"]).suffix
    for name, value in SAFE_ENV.items():
        argv.extend(("--setenv", name, value))
    argv.extend((sys.executable, "-m", "webcompare.media_sandbox", str(max_pixels)))
    return argv, mapped


def _tail(handle: BinaryIO) -> str:
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - 65536))
    return handle.read(65536).decode("utf-8", "replace")


def run_media(command: str, payload: dict[str, Any], max_pixels: int, output_root: Path, *, mode: str = "auto", timeout: float = 30.0) -> dict[str, Any]:
    """串行 worker 调用；Linux namespace 失败即拒绝，Windows auto 使用一次性子进程。"""
    windows = os.name == "nt"
    if windows and mode == "namespace":
        raise _WorkerMediaError("Windows 不支持 bubblewrap 命名空间隔离", "SANDBOX_FAILED", 503)
    isolated = not windows
    if isolated and not _namespace_available():
        raise _WorkerMediaError("完整媒体解码隔离不可用，拒绝解码", "SANDBOX_FAILED", 503)
    with tempfile.TemporaryDirectory(prefix="request-", dir=output_root) as directory:
        work = Path(directory)
        if isolated:
            argv, mapped = _namespace_args(payload, max_pixels, work)
            env = SAFE_ENV
        else:
            mapped = dict(payload)
            mapped["output"] = str(work / ("result" + Path(payload["output"]).suffix)) if payload.get("output") else None
            if mapped.get("output") is None:
                mapped.pop("output", None)
            vips_tmp = work / "tmp"
            vips_tmp.mkdir()
            argv = [sys.executable, "-m", "webcompare.media_sandbox", str(max_pixels), str(vips_tmp)]
            env = _process_env(work)
        stdout, stderr = work / "stdout.log", work / "stderr.log"
        try:
            # 通过父进程预先打开的句柄读回，避免子进程把日志路径换成宿主文件链接。
            with stdout.open("w+b") as out, stderr.open("w+b") as err:
                completed = subprocess.run(argv, input=json.dumps({"command": command, "payload": mapped}).encode(), stdout=out, stderr=err, env=env, cwd=work if windows else None, timeout=timeout, check=False)
                text = _tail(out if completed.returncode == 0 else err)
        except subprocess.TimeoutExpired as exc:
            raise _WorkerMediaError("媒体解码超时，已终止隔离进程", "WORKER_TIMEOUT", 503) from exc
        except OSError as exc:
            raise _WorkerMediaError(f"媒体隔离无法启动：{exc}", "SANDBOX_FAILED", 503) from exc
        prefix = "RESULT " if completed.returncode == 0 else "ERROR "
        lines = [line[len(prefix):] for line in text.splitlines() if line.startswith(prefix)]
        try:
            result = json.loads(lines[-1]) if lines else None
        except (ValueError, IndexError):
            result = None
        if not isinstance(result, dict):
            raise _WorkerMediaError(f"媒体隔离进程未返回有效结果（返回码 {completed.returncode}）", "SANDBOX_FAILED", 503)
        if completed.returncode != 0:
            status = result.get("status", 422)
            if type(status) is not int or status not in {409, 422, 503}:
                raise _WorkerMediaError("媒体隔离错误字段不被接受", "SANDBOX_FAILED", 503)
            raise _WorkerMediaError(str(result.get("message") or "media decode failed"), str(result.get("code") or "DECODE_ERROR"), status)
        if command in {"metadata", "validate", "render", "raw_convert"}:
            width, height = result.get("width"), result.get("height")
            if type(width) is not int or type(height) is not int or width <= 0 or height <= 0 or width * height > max_pixels:
                raise _WorkerMediaError("媒体隔离尺寸字段不被接受", "SANDBOX_FAILED", 503)
        if command in {"metadata", "validate"}:
            allowed_formats = {"raw"} if payload.get("raw_metadata") else {"png", "jpeg", "webp"}
            if (result.get("format") not in allowed_formats or type(result.get("bands")) is not int
                    or result["bands"] not in {1, 2, 3, 4} or result.get("bit_depth") != 8
                    or type(result.get("has_alpha")) is not bool or type(result.get("has_icc")) is not bool):
                raise _WorkerMediaError("媒体隔离元数据字段不被接受", "SANDBOX_FAILED", 503)
            checked = {key: result[key] for key in ("width", "height", "bands", "format", "has_alpha", "has_icc", "bit_depth")}
            if result.get("format") == "raw":
                # Resolved metadata was prepared by the HTTP process. Do not
                # accept arbitrary extra strings/paths from the decoder.
                expected = payload["raw_metadata"]
                if any(checked[key] != expected[key] for key in checked):
                    raise _WorkerMediaError("RAW 隔离元数据与请求不符", "SANDBOX_FAILED", 503)
                checked["raw"] = expected["raw"]
            return checked
        if command == "pixel":
            values = result.get("values")
            if (result.get("x") != payload["x"] or result.get("y") != payload["y"] or not isinstance(values, list)
                    or len(values) not in {3, 4} or any(type(value) is not int or not 0 <= value <= 255 for value in values)):
                raise _WorkerMediaError("媒体隔离像素字段不被接受", "SANDBOX_FAILED", 503)
            return {"x": payload["x"], "y": payload["y"], "values": values}
        if command in {"render", "raw_convert"}:
            # 子进程的 path 不可信：只接收约定名称的普通产物，再搬至父进程指定缓存。
            output = Path(payload["output"])
            rendered = work / ("result" + output.suffix)
            if rendered.is_symlink() or not rendered.is_file():
                raise _WorkerMediaError("媒体隔离进程缺少渲染产物", "SANDBOX_FAILED", 503)
            if not 0 < rendered.stat().st_size <= max_pixels * 4 + 1024**2:
                raise _WorkerMediaError("媒体隔离渲染字段不被接受", "SANDBOX_FAILED", 503)
            if command == "raw_convert":
                expected = payload["raw_metadata"]
                if output.suffix != ".v" or (width, height) != (expected["width"], expected["height"]):
                    raise _WorkerMediaError("RAW 隔离缓存字段不被接受", "SANDBOX_FAILED", 503)
                os.replace(rendered, output)
                return {"path": str(output), "width": width, "height": height}
            expected_mime = {"exact": "image/png", "preview": "image/webp"}.get(payload["quality"])
            if payload["quality"] == "high":
                expected_mime = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}.get(payload.get("encoding"))
            if (expected_mime is None or result.get("mime") != expected_mime
                    or result.get("quality") != payload["quality"]):
                raise _WorkerMediaError("媒体隔离渲染字段不被接受", "SANDBOX_FAILED", 503)
            os.replace(rendered, output)
            return {"path": str(output), "mime": result["mime"], "width": width, "height": height, "quality": payload["quality"]}
        raise _WorkerMediaError("unknown media operation", "INVALID_ARGUMENT", 422)


def _process_env(work: Path) -> dict[str, str]:
    """给 Windows 解码子进程保留 DLL 搜索与临时目录所需变量，不继承服务密钥。"""
    env = {name: os.environ[name] for name in ("PATH", "SYSTEMROOT", "WINDIR") if os.environ.get(name)}
    env.update({
        "HOME": str(work), "TEMP": str(work), "TMP": str(work),
        "VIPS_CONCURRENCY": "1", "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
        "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
    })
    return env


def _main() -> int:
    try:
        try:
            import resource
        except ImportError:
            resource = None

        max_pixels = int(sys.argv[1])
        # 编码和 libvips 单个临时文件均有尺寸上限；目录在请求结束后整体回收。
        if resource is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (max_pixels * 4 + 1024**2,) * 2)
        request = json.load(sys.stdin)
        requests, results = queue.SimpleQueue(), queue.SimpleQueue()
        requests.put(("single", request["command"], request["payload"]))
        requests.put(None)
        _worker_main(requests, results, max_pixels, sys.argv[2] if len(sys.argv) > 2 else "/output/tmp")
        _, result, error = results.get()
        if error is not None:
            print("ERROR " + json.dumps({"message": error.message, "code": error.code, "status": error.status}), file=sys.stderr, flush=True)
            return 1
        print("RESULT " + json.dumps(result), flush=True)
        return 0
    except BaseException as exc:
        print("ERROR " + json.dumps({"message": str(exc), "code": "SANDBOX_FAILED", "status": 503}), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())

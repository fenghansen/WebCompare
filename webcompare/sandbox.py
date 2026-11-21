"""【模块】上传解码沙箱 — 一次性子进程、文件白名单与资源限制。

Linux 的 namespace/auto 优先使用 bubblewrap：独立用户、挂载、网络与 PID
命名空间，只读暴露 Python 运行库、本站解码模块与待验证文件。namespace
不可用即拒绝；auto/none 可退化为一次性子进程。Windows 的 auto/none 使用
一次性子进程，不提供文件/网络隔离或 POSIX resource 限制；显式 namespace 拒绝。
公网生产使用 Linux namespace。

## 流程
所属流程：解码沙箱（webcompare/sandbox.py）

1. **可用性探测**：bubblewrap 完整命名空间实际试启动 → 见函数 [_namespace_available]
2. **父进程入口**：受限子进程、超时与结构化结果 → 见函数 [validate_image]
3. **子进程内核**：rlimits 与完整解码 → 见函数 [_main]、[_decode_checked]
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .files import FileError
from .image_formats import IMAGE_FORMATS, image_format_from_header, is_animated_webp

try:  # pragma: no cover - 仅非 POSIX 平台走这里
    import resource
except ImportError:  # 沙箱本身是 Linux 能力，缺 resource 时按“无法设限”降级并告警
    resource = None  # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

BWRAP_PREFIX = ("bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL")
RESULT_PREFIX = "RESULT "
ERROR_PREFIX = "ERROR "
SANDBOX_MODES = ("auto", "namespace", "none")
META_KEYS = ("width", "height", "bands", "format", "has_alpha", "has_icc", "bit_depth")

# 实测取值依据（本机 72 核 / Linux，pyvips 3.2 binary wheel）：
# - 子进程必须在 import pyvips 之前把 VIPS_CONCURRENCY 设为 1，否则 libvips 按
#   核数起线程，仅 2400×1800 PNG 的 stats() 就能把 VmSize 推到 2.5 GiB，768 MiB
#   会在建线程时报 “Resource temporarily unavailable”。
# - 单线程下 VmPeak 实测：2400×1800 PNG ≈ 194 MiB；24 MP（6000×4000）PNG ≈
#   194 MiB、JPEG ≈ 217 MiB；24 MP 双图 int 差分渲染 ≈ 337 MiB；隔行扫描
#   （无法流式、需整图读入）的 24 MP 差分 ≈ 351 MiB。768 MiB 相对最坏实测仍有
#   一倍以上余量，同时仍能卡住失控的放大解码。
RLIMIT_AS_BYTES = 768 * 1024**2
RLIMIT_CPU_SECONDS = 20
RLIMIT_NOFILE = 64
PROBE_TIMEOUT = 5.0

_NAMESPACE_PROBE: bool | None = None


def _runtime_args() -> list[str]:
    """只读暴露 Python 运行库与解码模块；不绑定服务配置、内容根或状态目录。"""
    args = []
    for path in (Path("/usr"), Path("/lib"), Path("/lib64"), Path(sys.base_prefix), Path(sys.prefix), Path(__file__).resolve().parent):
        if path.exists():
            args.extend(("--ro-bind", str(path), str(path)))
    return args


def _namespace_available(probe_timeout: float = PROBE_TIMEOUT) -> bool:
    """实际探测 bubblewrap 的用户、文件、网络、PID 隔离能力，进程内缓存结果。"""
    global _NAMESPACE_PROBE
    if _NAMESPACE_PROBE is None:
        if os.name != "posix":
            _NAMESPACE_PROBE = False
            return False
        try:
            completed = subprocess.run(
                [*BWRAP_PREFIX, *_runtime_args(), "/usr/bin/true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=probe_timeout, check=False,
            )
            _NAMESPACE_PROBE = completed.returncode == 0
            if not _NAMESPACE_PROBE:
                LOGGER.warning("bubblewrap 隔离不可用：%s", completed.stderr.decode("utf-8", "replace").strip()[:200])
        except (OSError, subprocess.SubprocessError) as exc:
            _NAMESPACE_PROBE = False
            LOGGER.warning("bubblewrap 隔离不可用：%s", exc)
    return _NAMESPACE_PROBE


def _child_env() -> dict[str, str]:
    """构造一次性解码子进程环境；Windows 保留 Python/libvips 所需的系统路径。"""
    env = {
        "VIPS_CONCURRENCY": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    }
    if os.name == "nt":
        for name in ("PATH", "SYSTEMROOT", "WINDIR"):
            if value := os.environ.get(name):
                env[name] = value
        temp = tempfile.gettempdir()
        env.update({"HOME": temp, "TEMP": temp, "TMP": temp})
    else:
        env.update({"PATH": "/usr/bin:/bin", "HOME": "/tmp"})
    return env


def validate_image(path: Path, max_pixels: int, *, mode: str = "auto", timeout: float = 15.0) -> dict:
    """完整解码待提交图片；namespace 强制完整隔离，退化路径也不在 HTTP 进程解析。"""
    if mode not in SANDBOX_MODES:
        raise ValueError("沙箱模式必须是 auto、namespace 或 none")
    if not isinstance(max_pixels, int) or max_pixels <= 0:
        raise ValueError("像素上限必须是正整数")
    source = Path(path).resolve()
    if not source.is_file():
        raise FileError("待校验文件不存在或不是普通文件", "DECODE_ERROR", 422)
    isolated = mode != "none" and _namespace_available()
    if mode == "namespace" and not isolated:
        raise FileError("完整解码隔离不可用，拒绝上传", "SANDBOX_FAILED", 503)
    return _validate_in_namespace(source, max_pixels, timeout, isolated=isolated)


def _validate_in_namespace(source: Path, max_pixels: int, timeout: float, *, isolated: bool = True) -> dict:
    """一次性子进程完整解码，环境只保留运行库变量，超时杀掉整个解码进程。"""
    env = _child_env()
    if isolated:
        inside = f"/input/image{source.suffix.lower()}"
        argv = [*BWRAP_PREFIX, *_runtime_args(), "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--dir", "/input", "--ro-bind", str(source), inside, "--chdir", "/tmp"]
        for name, value in env.items():
            argv.extend(("--setenv", name, value))
        argv.extend((sys.executable, "-m", "webcompare.sandbox", inside, str(max_pixels)))
    else:
        limits = "有 rlimits 与超时" if resource is not None else "有超时，但没有 OS 资源上限"
        LOGGER.warning("图片校验退化为独立子进程：%s，也没有文件和网络隔离", limits)
        argv = [sys.executable, "-m", "webcompare.sandbox", str(source), str(max_pixels)]
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, env=env)
    except subprocess.TimeoutExpired as exc:
        raise FileError(f"沙箱校验超时（{timeout} 秒），已终止解码", "SANDBOX_FAILED", 503) from exc
    except OSError as exc:
        raise FileError(f"沙箱无法启动：{exc}", "SANDBOX_FAILED", 503) from exc
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        failure = _parse_error_line(stderr)
        if failure is not None:
            raise failure
        detail = stderr.strip().splitlines()[-1][:200] if stderr.strip() else "无错误输出"
        raise FileError(f"沙箱子进程异常退出（返回码 {completed.returncode}）：{detail}", "SANDBOX_FAILED", 503)
    return _parse_result_line(completed.stdout.decode("utf-8", "replace"))


def _parse_result_line(stdout: str) -> dict:
    """功能说明：从子进程 stdout 中取最后一行 RESULT 前缀的 JSON，规整成元数据字典。

    调用时机：[_validate_in_namespace] 在子进程零退出后调用。

    关键步骤：
    1. 倒序找第一个以 RESULT 开头的行；
    2. 解析 JSON 并要求七个字段齐全、数值型字段为整数、format 为非空字符串；
    3. 任何缺失或不合法都视为沙箱结果不可信。

    输出/副作用：返回固定字段的元数据字典；缺失/畸形抛 FileError(503)。
    """
    for line in reversed(stdout.splitlines()):
        if not line.startswith(RESULT_PREFIX):
            continue
        try:
            data = json.loads(line[len(RESULT_PREFIX) :])
        except ValueError as exc:
            raise FileError(f"沙箱结果无法解析：{exc}", "SANDBOX_FAILED", 503) from exc
        if not isinstance(data, dict):
            raise FileError("沙箱结果不是对象", "SANDBOX_FAILED", 503)
        meta: dict = {}
        for key in META_KEYS:
            if key not in data:
                raise FileError(f"沙箱结果缺少字段 {key}", "SANDBOX_FAILED", 503)
            meta[key] = data[key]
        try:
            meta = {
                "width": int(meta["width"]),
                "height": int(meta["height"]),
                "bands": int(meta["bands"]),
                "format": str(meta["format"]),
                "has_alpha": bool(meta["has_alpha"]),
                "has_icc": bool(meta["has_icc"]),
                "bit_depth": int(meta["bit_depth"]),
            }
        except (TypeError, ValueError) as exc:
            raise FileError(f"沙箱结果字段类型非法：{exc}", "SANDBOX_FAILED", 503) from exc
        if meta["width"] <= 0 or meta["height"] <= 0 or not meta["format"]:
            raise FileError("沙箱结果字段取值非法", "SANDBOX_FAILED", 503)
        return meta
    raise FileError("沙箱子进程没有输出校验结果", "SANDBOX_FAILED", 503)


def _parse_error_line(stderr: str) -> FileError | None:
    """功能说明：从子进程 stderr 中还原结构化错误（中文原因 + 错误码 + 状态码）。

    调用时机：[_validate_in_namespace] 在子进程非零退出后调用。

    关键步骤：
    1. 倒序找第一个 ERROR 前缀的行，解析其 JSON；
    2. 取 message/code/status，缺省分别落到“沙箱校验失败”/DECODE_ERROR/422；
    3. 没有可解析的 ERROR 行时返回 None，由调用方统一按沙箱故障处理。

    输出/副作用：返回 [FileError] 或 None；不做任何清理动作。
    """
    for line in reversed(stderr.splitlines()):
        line = line.strip()
        if not line.startswith(ERROR_PREFIX):
            continue
        try:
            data = json.loads(line[len(ERROR_PREFIX) :])
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        message = str(data.get("message") or "沙箱校验失败")
        code = str(data.get("code") or "DECODE_ERROR")
        try:
            status = int(data.get("status") or 422)
        except (TypeError, ValueError):
            status = 422
        return FileError(message, code, status)
    return None


def _suffix_format(path: Path) -> str:
    """功能说明：按扩展名给出期望格式，限定为 PNG、JPEG 和 WebP。

    调用时机：[_decode_checked] 解码前的第一道检查。

    关键步骤：
    1. 取小写后缀；
    2. 不在白名单即抛 FileError(422)。

    输出/副作用：返回解码格式；不支持的后缀抛 FileError(422)。
    """
    fmt = IMAGE_FORMATS.get(path.suffix.lower())
    if fmt is None:
        raise FileError("只支持 PNG、JPEG 和静态 WebP 图片", "UNSUPPORTED_FORMAT", 422)
    return fmt


def _check_magic(path: Path, expected: str) -> None:
    """功能说明：校验文件魔数与扩展名一致，挡住“改后缀”的伪装文件。

    调用时机：[_decode_checked] 在解码前调用。

    关键步骤：
    1. 读取文件头，核对魔数与期望格式；
    2. 拒绝 WebP 扩展头标记为动画的文件。

    输出/副作用：一致则返回；不一致或无文件内容抛 FileError(422)。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(21)
    except OSError as exc:
        raise FileError(f"无法读取待校验文件：{exc}", "DECODE_ERROR", 422) from exc
    if image_format_from_header(head) != expected:
        raise FileError("文件魔数与扩展名不符（疑似伪装格式）", "UNSUPPORTED_FORMAT", 422)
    if expected == "webp" and is_animated_webp(head):
        raise FileError("不支持动画 WebP", "UNSUPPORTED_FORMAT", 422)


def _decode_checked(pyvips: object, path: Path, max_pixels: int) -> dict:
    """功能说明：沙箱内的校验内核：完整解码一张静态常规图片并产出元数据。

    调用时机：[_main] 的完整隔离与受限子进程路径共用，
    两条路径的判定口径必须完全一致。

    关键步骤：
    1. 后缀白名单与魔数一致检查（[_suffix_format]、[_check_magic]）；
    2. new_from_file(access="sequential", fail=True) 打开，loader 必须是
       pngload/jpegload/webpload 且与扩展名对应；
    3. 拒绝 CMYK 与非 8 位（format != "uchar"）；
    4. 宽高必须为正且乘积不超过 max_pixels（先卡上限再解码，避免像素炸弹）；
    5. 通道数限于 1/2/3/4，按 EXIF 方向校正（autorot），再复核一次像素上限；
    6. image.stats() 强制全图解码，确认数据完整无截断——这一步才是真正的
       “完整解码”，前面的 new_from_file 只读文件头。

    输出/副作用：返回与 [MediaService.metadata] 同构的元数据字典；格式/位深/
    像素/损坏等问题抛 FileError(422)。
    """
    fmt = _suffix_format(path)
    _check_magic(path, fmt)
    try:
        image = pyvips.Image.new_from_file(str(path), access="sequential", fail=True)  # type: ignore[attr-defined]
    except BaseException as exc:
        raise FileError(f"图片解码失败：{exc}", "DECODE_ERROR", 422) from exc
    loader = ""
    try:
        loader = str(image.get("vips-loader"))
    except Exception:
        loader = ""
    if loader != f"{fmt}load":
        raise FileError("文件实际格式与扩展名不符或不受支持", "UNSUPPORTED_FORMAT", 422)
    if fmt == "webp" and image.get_typeof("n-pages") and image.get("n-pages") > 1:
        raise FileError("不支持动画 WebP", "UNSUPPORTED_FORMAT", 422)
    interpretation = str(getattr(image, "interpretation", "")).lower()
    if "cmyk" in interpretation:
        raise FileError("不支持 CMYK 图片", "UNSUPPORTED_FORMAT", 422)
    if image.format != "uchar":
        raise FileError("只支持 8 位 PNG、JPEG 和静态 WebP 图片", "UNSUPPORTED_FORMAT", 422)
    if image.width <= 0 or image.height <= 0:
        raise FileError("图片尺寸非法", "PIXEL_LIMIT", 422)
    if image.width * image.height > max_pixels:
        raise FileError(f"图片像素数超出上限：{image.width}×{image.height} 超过 {max_pixels}", "PIXEL_LIMIT", 422)
    if image.bands not in {1, 2, 3, 4}:
        raise FileError("图片通道数不受支持", "UNSUPPORTED_FORMAT", 422)
    try:
        image = image.autorot()
    except Exception:
        # 老版本 libvips 没有 autorot 时仍可继续，尺寸与后续渲染口径略有差异。
        pass
    if image.width * image.height > max_pixels:
        raise FileError(f"图片像素数超出上限：{image.width}×{image.height} 超过 {max_pixels}", "PIXEL_LIMIT", 422)
    try:
        image.stats()
    except BaseException as exc:
        raise FileError(f"图片数据不完整或已损坏：{exc}", "DECODE_ERROR", 422) from exc
    bands = int(image.bands)
    return {
        "width": int(image.width),
        "height": int(image.height),
        "bands": bands,
        "format": fmt,
        "has_alpha": bands in {2, 4},
        "has_icc": bool(image.get_typeof("icc-profile-data")) if hasattr(image, "get_typeof") else False,
        "bit_depth": 8,
    }


def _apply_limits() -> None:
    """功能说明：给沙箱子进程设资源上限：虚拟内存、CPU 时间、句柄数与写出。

    调用时机：[_main] 一开始、import pyvips 之前调用（rlimits 必须在解码
    库初始化之前生效，否则已经映射的地址空间不受约束）。

    关键步骤：
    1. 逐项 setrlimit：RLIMIT_AS=768 MiB、RLIMIT_CPU=20 秒、RLIMIT_NOFILE=64、
       RLIMIT_FSIZE=0（禁止写出任何文件）；
    2. 平台不支持某项时打 warning 继续（例如非 POSIX 无 resource 模块）。

    输出/副作用：修改本进程的资源上限；可能输出 warning 到 stderr。
    """
    if resource is None:  # pragma: no cover - 非 POSIX 平台
        print("WARN 当前平台不支持 resource 模块，沙箱未设置 rlimits", file=sys.stderr, flush=True)
        return
    limits = (
        ("RLIMIT_AS", resource.RLIMIT_AS, RLIMIT_AS_BYTES),
        ("RLIMIT_CPU", resource.RLIMIT_CPU, RLIMIT_CPU_SECONDS),
        ("RLIMIT_NOFILE", resource.RLIMIT_NOFILE, RLIMIT_NOFILE),
        ("RLIMIT_FSIZE", resource.RLIMIT_FSIZE, 0),
    )
    for name, which, value in limits:
        try:
            resource.setrlimit(which, (value, value))
        except (OSError, ValueError) as exc:  # pragma: no cover - 取决于平台
            print(f"WARN 无法设置 {name}: {exc}", file=sys.stderr, flush=True)


def _main() -> int:
    """功能说明：`python -m webcompare.sandbox` 的子进程入口：设限、断网环境内完整解码、输出结果。

    调用时机：由 [validate_image] 经 `bwrap … python -m webcompare.sandbox <path> <max_pixels>` 调用一次；退出即消亡。

    关键步骤：
    1. 解析 argv（路径与像素上限），不合法即退出码 2；
    2. 先在 /tmp 下 mkdtemp 出 0700 空目录并 chdir 进去（顺序在设限之前：
       RLIMIT_FSIZE=0 会让 tempfile 的“写 4 字节测试可写性”失败，实测表现为
       FileNotFoundError: No usable temporary directory found），再调用
       [_apply_limits] 设 rlimits，让解码过程中任何相对路径写入都落在
       一次性目录里；
    3. 把 VIPS_CONCURRENCY 固定为 1（必须在 import pyvips 之前），否则 libvips
       按核数起线程会撑爆 RLIMIT_AS；
    4. import pyvips 并调用 [_decode_checked] 完整解码校验；
    5. 成功把元数据以 `RESULT {json}` 打到 stdout 最后一行并返回 0；失败把
       `ERROR {message,code,status}` 打到 stderr 并返回非零；
    6. 退出前删除一次性工作目录。

    输出/副作用：stdout/stderr 一行结构化输出；退出码 0（校验通过）/1（文件
    被拒或沙箱故障）/2（参数错误）；临时目录被清理，子进程内禁写文件。
    """
    if len(sys.argv) != 3:
        print("ERROR 用法：python -m webcompare.sandbox <path> <max_pixels>", file=sys.stderr, flush=True)
        return 2
    try:
        max_pixels = int(sys.argv[2])
    except ValueError:
        print("ERROR 像素上限必须是整数", file=sys.stderr, flush=True)
        return 2
    source = Path(sys.argv[1])
    workdir = Path(tempfile.mkdtemp(prefix="webcompare-sandbox-", dir=tempfile.gettempdir()))
    try:
        os.chdir(workdir)
        _apply_limits()
        os.environ["VIPS_CONCURRENCY"] = "1"
        try:
            import pyvips  # type: ignore[import-not-found]
        except BaseException as exc:
            raise FileError(f"图片解码库不可用：{exc}", "SANDBOX_FAILED", 503) from exc
        meta = _decode_checked(pyvips, source, max_pixels)
        print(f"{RESULT_PREFIX}{json.dumps(meta, ensure_ascii=False)}", flush=True)
        return 0
    except FileError as exc:
        payload = {"message": str(exc), "code": exc.code, "status": exc.status}
        print(f"{ERROR_PREFIX}{json.dumps(payload, ensure_ascii=False)}", file=sys.stderr, flush=True)
        return 1
    except BaseException as exc:  # 沙箱自身出问题时也要给父进程结构化结果
        payload = {"message": f"沙箱内解码异常：{exc}", "code": "SANDBOX_FAILED", "status": 503}
        print(f"{ERROR_PREFIX}{json.dumps(payload, ensure_ascii=False)}", file=sys.stderr, flush=True)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":  # pragma: no cover - 子进程入口
    sys.exit(_main())

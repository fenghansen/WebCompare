"""【模块】安装与前端构建 — pip/uv 安装时准备可直接运行的工作台

功能说明：封装 setuptools 的 PEP 517/660 钩子，让普通 wheel、源码包和
editable 安装共用一套前端准备逻辑。Node/npm 仅在需要重新构建时加入临时
构建环境，不成为服务运行依赖；发布包通过源码和产物指纹复用完整前端。

## 流程
所属流程：安装与前端构建（webcompare_build.py）

1. **有效性检查**：核对源码指纹与每个静态产物，决定是否需要 Node → 见函数 [_source_digest]、[_frontend_ready]
2. **临时构建依赖**：按有效性检查声明固定版本的构建工具 → 见函数 [_requirements]
3. **准备前端**：在独立 Node 环境执行锁定依赖安装与构建，并复制 wheel 静态资源 → 见函数 [_prepare_frontend]
4. **生成安装包**：将准备好的前端交给 setuptools，保持原有安装参数 → 见函数 [build_wheel]、[build_editable]、[build_sdist]
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil

from setuptools import build_meta as _backend

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"
STAMP = ".webcompare-build.json"
NODE_REQUIREMENT = "nodejs-wheel-binaries==24.19.0"


def _source_digest():
    """功能说明：计算影响前端构建的源码内容指纹。

    调用时机：检查已有产物，或成功构建后写入新的指纹记录时调用。

    关键步骤：
    1. 收集前端根配置文件、src 和 public，排除依赖目录与生成目录；
    2. 按路径排序，将相对路径、分隔符和文件内容累计到 SHA-256。

    输出/副作用：返回摘要字符串；不依赖文件时间戳，不修改源码。
    """
    digest = hashlib.sha256()
    files = [p for p in FRONTEND.iterdir() if p.is_file()]
    for folder in ("src", "public"):
        files.extend(p for p in (FRONTEND / folder).rglob("*") if p.is_file())
    for path in sorted(files):
        digest.update(path.relative_to(FRONTEND).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _frontend_ready():
    """功能说明：确认已构建前端可以直接用于当前安装。

    调用时机：构建依赖查询和实际生成安装包之前调用。

    关键步骤：
    1. 安装时若只带预构建前端，检查入口和 JavaScript 是否存在；
    2. 源码目录同时核对源码摘要、入口登记和全部产物摘要；
    3. 指纹缺失、格式错误或文件改变时返回 False，交由构建钩子重建。

    输出/副作用：返回是否可复用；本函数只读，不下载或构建。
    """
    # 已构建的静态站点可直接安装。
    if not (FRONTEND / "package.json").is_file():
        return (DIST / "index.html").is_file() and any((DIST / "assets").glob("*.js"))
    try:
        stamp = json.loads((DIST / STAMP).read_text(encoding="utf-8"))
        return (
            stamp["source"] == _source_digest()
            and "index.html" in stamp["files"]
            and all((DIST / name).is_file() and hashlib.sha256((DIST / name).read_bytes()).hexdigest() == value
                    for name, value in stamp["files"].items())
        )
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _prepare_frontend():
    """功能说明：保证 checkout 前端与 wheel 内置静态资源均完整。

    调用时机：build_wheel、build_editable、build_sdist 进入 setuptools 前调用。

    关键步骤：
    1. 失效时使用临时构建环境提供的 Node/npm 执行 npm ci 与前端构建；
    2. 校验入口并记录源码及所有产物的内容摘要，后续可复用；
    3. 用当前 dist 更新包内 static，排除仅供源码构建判断的指纹文件。

    输出/副作用：可能下载 npm 依赖、重建 dist 与 static；失败直接终止安装，
    不让缺少工作台的 Python 包被当作安装成功。
    """
    if not _frontend_ready():
        import nodejs_wheel
        # 生命周期脚本也必须用临时环境的同一版本 Node，避免宿主机缺少或版本过旧。
        node_root = Path(nodejs_wheel.__file__).parent
        node_bin = node_root if os.name == "nt" else node_root / "bin"
        env = {**os.environ, "PATH": str(node_bin) + os.pathsep + os.environ.get("PATH", "")}
        print("WebCompare: installing frontend dependencies and building the UI", flush=True)
        nodejs_wheel.npm(["ci", "--no-audit", "--no-fund"], cwd=FRONTEND, env=env, check=True)
        nodejs_wheel.npm(["run", "build"], cwd=FRONTEND, env=env, check=True)
        if not (DIST / "index.html").is_file():
            raise RuntimeError("Frontend build did not produce index.html")
        files = {p.relative_to(DIST).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(DIST.rglob("*")) if p.is_file() and p.name != STAMP}
        (DIST / STAMP).write_text(json.dumps({"source": _source_digest(), "files": files}), encoding="utf-8")
    # wheel 脱离 checkout 后读取包内 static；editable 仍优先读取 checkout/dist。
    packaged = ROOT / "webcompare" / "static"
    if packaged.exists():
        shutil.rmtree(packaged)
    shutil.copytree(DIST, packaged, ignore=shutil.ignore_patterns(STAMP))


def _requirements(base, config_settings):
    """功能说明：在 setuptools 原有要求上按需追加 Node 构建依赖。

    调用时机：pip/uv 的 PEP 517/660 构建依赖查询阶段调用。

    关键步骤：
    1. 保留 setuptools 根据 config_settings 返回的原有依赖；
    2. 仅前端不能复用时追加固定版本 Node，不修改项目运行依赖。

    输出/副作用：返回构建依赖清单；此时不安装依赖、不执行 npm。
    """
    requirements = base(config_settings)
    if not _frontend_ready():
        requirements.append(NODE_REQUIREMENT)
    return requirements


def get_requires_for_build_wheel(config_settings=None):
    return _requirements(_backend.get_requires_for_build_wheel, config_settings)


def get_requires_for_build_editable(config_settings=None):
    return _requirements(_backend.get_requires_for_build_editable, config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return _requirements(_backend.get_requires_for_build_sdist, config_settings)


prepare_metadata_for_build_wheel = _backend.prepare_metadata_for_build_wheel
prepare_metadata_for_build_editable = _backend.prepare_metadata_for_build_editable


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    """准备静态资源后生成普通 wheel，原样转交构建选项及预生成元数据目录。"""
    _prepare_frontend()
    return _backend.build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    """准备 checkout 前端后生成 editable wheel，保留 setuptools 的可编辑安装行为。"""
    _prepare_frontend()
    return _backend.build_editable(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    """先准备可复用前端再生成源码包，保证离开 Git checkout 后仍有完整安装入口。"""
    _prepare_frontend()
    return _backend.build_sdist(sdist_directory, config_settings)

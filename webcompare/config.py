"""【模块】运行配置 — 环境变量解析与启动前校验

功能说明：服务的全部可调项（部署模式、内容根与状态目录、快捷目录与
挂载点、配额与访客保留期、缓存与像素上限、前端目录）集中在此。启动时
统一解析环境变量并做一致性校验，让错误配置在启动时就失败，而不是运行
中出错。server 模式额外硬校验 HTTPS 身份参数、内容区目录与挂载边界；
personal 默认免登录，也可显式要求使用已有账号登录。

## 流程
所属流程：命令行入口（webcompare/config.py）

1. **环境解析**：读取 WEBCOMPARE_* 环境变量并做格式校验 → 见函数 [Settings.from_env]
2. **启动前校验**：状态目录收紧权限、快捷目录检查、缓存的限额检查，
   server 模式再加内容区创建与挂载边界检查 → 见函数 [Settings.prepare]、[Settings._prepare_server]
3. **身份硬校验**：server 模式要求至少一个启用中的管理员，数据库就绪后
   由装配层调用 → 见函数 [Settings.validate_server_identity]
4. **前端定位**：源码安装优先使用 checkout，普通 wheel 使用包内静态资源 → 见函数 [_default_frontend_dir]
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .db import Database

PERSONAL_DATA_DIR = "~/.local/share/webcompare"
DEFAULT_DATA_ROOT = "~/WebCmpData"
STATE_SUBDIR = ".state"
MODES = ("personal", "server")
MOUNT_ACCESS = ("ro", "rw")
SANDBOX_MODES = ("auto", "namespace", "none")
CONTENT_DIRS = ("public", "private", "member", "guest")


def _default_frontend_dir() -> Path:
    """功能说明：为源码安装与普通 wheel 选择可运行的默认前端目录。

    调用时机：构造 Settings 的 frontend_dir 字段默认值时调用；显式配置仍优先。

    关键步骤：
    1. checkout 的 frontend/dist 已有入口时优先使用它，便于 editable 开发；
    2. 否则返回包内 static，由安装构建钩子随 wheel 分发。

    输出/副作用：返回目录路径，不创建文件；serve 再检查入口是否存在。
    """
    package = Path(__file__).resolve().parent
    checkout_dist = package.parent / "frontend" / "dist"
    return checkout_dist if (checkout_dist / "index.html").is_file() else package / "static"


def _default_data_dir() -> Path:
    """功能说明：状态目录默认值：遵循 WEBCOMPARE_DATA_DIR，否则落到个人数据目录。

    调用时机：构造 [Settings] 时由字段默认值求值；[Settings.prepare] 也用它
    判断调用方是否显式指定过状态目录。

    关键步骤：
    1. 读取环境变量 WEBCOMPARE_DATA_DIR；
    2. 展开 ~ 得到路径对象。

    输出/副作用：返回路径；纯函数，不创建目录。
    """
    return Path(os.getenv("WEBCOMPARE_DATA_DIR", PERSONAL_DATA_DIR)).expanduser()


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """功能说明：读取整数型环境变量并做下限校验，供容量与保留期配置使用。

    调用时机：[Settings.from_env] 组装配置项时逐项调用。

    关键步骤：
    1. 环境变量缺失或为空时直接返回默认值；
    2. 解析为整数，非整数或小于下限即报错。

    输出/副作用：返回整数；解析失败抛 ValueError 使启动失败。
    """
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if value < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return value


def _origin(value: str, name: str, scheme: str = "https") -> str:
    """功能说明：校验一个不带子路径的 origin 地址（server 模式的硬性要求）。

    调用时机：[Settings.from_env] 与 [Settings._prepare_server] 校验
    WEBCOMPARE_BASE_URL 时调用。

    关键步骤：
    1. 用 urlsplit 拆分，要求协议与调用方给定的一致；
    2. 要求有主机名，且没有路径、查询、片段与用户名。

    输出/副作用：返回原地址；不合法抛 ValueError。
    """
    parsed = urlsplit(value)
    if parsed.scheme != scheme or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError(f"{name} 必须是完整的 {scheme.upper()} origin，不带子路径")
    return value


def _valid_id(value: str) -> bool:
    """功能说明：校验快捷目录 ID 与挂载 ID 的字符集：非空且只含字母数字、- 和 _。

    调用时机：[Settings.prepare] 与 [Settings._prepare_server] 校验键名时调用。

    关键步骤：
    1. 空串直接判否；
    2. 去掉 - 与 _ 后要求剩余字符全是字母或数字。

    输出/副作用：返回布尔；纯函数。
    """
    return bool(value) and value.replace("-", "").replace("_", "").isalnum()


def _contains(outer: Path, inner: Path) -> bool:
    """功能说明：判断路径包含关系：inner 等于 outer 或位于 outer 之下。

    调用时机：[Settings._prepare_server] 校验挂载与内容根、状态目录、
    其他挂载是否互相包含时调用。

    关键步骤：
    1. 先比相等；
    2. 再看 inner 的祖先链里是否有 outer。

    输出/副作用：返回布尔；纯路径比较，不访问文件系统。
    """
    return outer == inner or outer in inner.parents


@dataclass(frozen=True)
class Mount:
    """服务器挂载点：把 data_root 之外的一个目录以 `mount/<id>/…` 暴露给成员。

    功能说明：id 是虚拟路径中的名字（字符集同快捷目录），path 是物理目录，
    access 为 ro（成员只读）或 rw（成员可读写）。挂载不复制文件，只在授权
    与配额层生效：读写都落在原目录上，移除挂载只收回授权而不删文件。
    """

    id: str
    path: Path
    access: str


def _parse_mounts(raw: str) -> dict[str, Mount]:
    """功能说明：解析 WEBCOMPARE_MOUNTS 的 JSON 描述，得到挂载点映射。

    调用时机：[Settings.from_env] 组装配置时调用。

    关键步骤：
    1. 空串返回空映射（personal 模式默认无挂载）；
    2. 要求顶层是对象，每项是含 path 的对象，access 只能是 ro 或 rw（缺省 ro）；
    3. 逐项构造 [Mount]，路径只展开 ~，存在性与边界留给 [Settings.prepare]。

    输出/副作用：返回 ID → Mount 的字典；结构非法抛 ValueError。
    """
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError("WEBCOMPARE_MOUNTS 不是合法 JSON") from exc
    if not isinstance(data, dict):
        raise ValueError('WEBCOMPARE_MOUNTS 必须是 {"挂载ID": {"path": "…", "access": "ro"|"rw"}} 的 JSON 对象')
    mounts: dict[str, Mount] = {}
    for key, spec in data.items():
        if not isinstance(spec, dict) or not str(spec.get("path", "")).strip():
            raise ValueError(f"挂载 {key} 必须提供 path")
        access = str(spec.get("access", "ro")).strip().lower()
        if access not in MOUNT_ACCESS:
            raise ValueError(f"挂载 {key} 的 access 必须是 ro 或 rw")
        mounts[str(key)] = Mount(id=str(key), path=Path(str(spec["path"])).expanduser(), access=access)
    return mounts


@dataclass
class Settings:
    """运行配置：部署模式、内容根与状态目录、快捷目录映射、挂载点与各项资源上限。"""

    mode: str = "personal"
    require_login: bool = False  # personal 默认免登录；server 始终要求认证
    data_dir: Path = field(default_factory=_default_data_dir)
    data_root: Path = field(default_factory=lambda: Path(os.getenv("WEBCOMPARE_DATA_ROOT", DEFAULT_DATA_ROOT)).expanduser())
    roots: dict[str, Path] = field(default_factory=dict)  # 侧栏快捷方式，可空；member 可浏览任意绝对路径
    mounts: dict[str, Mount] = field(default_factory=dict)  # server 模式：挂载点 ID → 物理目录与读写权限
    profile: str = "intranet"
    client_transport: str = "auto"
    base_url: str = ""
    cache_bytes: int = 2 * 1024**3
    max_pixels: int = 24_000_000
    preview_effort: int = 0
    png_compression: int = 6
    raw_high_format: str = "jpeg"
    max_sources: int = 500
    max_files_per_source: int = 2000
    session_days: int = 7
    member_quota_bytes: int = 1536 * 1024**2
    guest_quota_bytes: int = 200 * 1024**2
    guest_max_workspaces: int = 10
    guest_exit_ttl: int = 600
    guest_idle_ttl: int = 1800
    guest_content_ttl: int = 86400
    disk_min_free_bytes: int = 512 * 1024**2
    site_quota_bytes: int = 0
    thumbnail_prewarm: bool = True
    content_group_access: bool = False
    sandbox: str = "auto"
    frontend_dir: Path = field(default_factory=_default_frontend_dir)

    @classmethod
    def from_env(cls) -> "Settings":
        """功能说明：从 WEBCOMPARE_* 环境变量构建配置，并做格式级校验。

        调用时机：[main] 与 [create_app] 启动时各调用一次。

        关键步骤：
        1. 解析 WEBCOMPARE_MODE 为 personal 或 server、WEBCOMPARE_REQUIRE_LOGIN
           为 0 或 1（默认 0）；server 始终要求认证，其默认状态目录在
           [Settings.prepare] 落到 <data_root>/.state；
        2. 解析 WEBCOMPARE_ROOTS（侧栏快捷方式）与 WEBCOMPARE_MOUNTS（挂载点）
           两个 JSON 对象，逐项校验取值；
        3. 校验 WEBCOMPARE_PROFILE 为 cloud/intranet；
        4. 校验 WEBCOMPARE_BASE_URL 为完整 HTTP(S) origin（不带子路径）；
        5. 读取内容根、成员/访客配额、访客名额与保留期、磁盘底线、沙箱模式
           与站点访问地址；
        6. 换算缓存上限并解析前端目录。

        输出/副作用：返回 Settings；格式非法时抛 ValueError，进程启动失败。
        """
        mode = os.getenv("WEBCOMPARE_MODE", "personal").strip().lower()
        if mode not in MODES:
            raise ValueError("WEBCOMPARE_MODE 必须是 personal 或 server")
        require_login = os.getenv("WEBCOMPARE_REQUIRE_LOGIN", "0")
        if require_login not in {"0", "1"}:
            raise ValueError("WEBCOMPARE_REQUIRE_LOGIN 必须是 0 或 1")
        roots = json.loads(os.getenv("WEBCOMPARE_ROOTS", "{}"))
        if not isinstance(roots, dict):
            raise ValueError("WEBCOMPARE_ROOTS 必须是根目录 ID 到路径的 JSON 对象")
        mounts = _parse_mounts(os.getenv("WEBCOMPARE_MOUNTS", ""))
        profile = os.getenv("WEBCOMPARE_PROFILE", "intranet")
        if profile not in {"cloud", "intranet"}:
            raise ValueError("WEBCOMPARE_PROFILE 必须是 cloud 或 intranet")
        base = os.getenv("WEBCOMPARE_BASE_URL", "").rstrip("/")
        if base:
            parsed = urlsplit(base)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError("WEBCOMPARE_BASE_URL 必须是完整的 HTTP(S) origin，不带子路径")
        sandbox = os.getenv("WEBCOMPARE_SANDBOX", "auto").strip().lower()
        if sandbox not in SANDBOX_MODES:
            raise ValueError("WEBCOMPARE_SANDBOX 必须是 auto、namespace 或 none")
        prewarm = os.getenv("WEBCOMPARE_THUMBNAIL_PREWARM", "0" if profile == "cloud" else "1")
        if prewarm not in {"0", "1"}:
            raise ValueError("WEBCOMPARE_THUMBNAIL_PREWARM 必须是 0 或 1")
        group_access = os.getenv("WEBCOMPARE_CONTENT_GROUP_ACCESS", "0")
        if group_access not in {"0", "1"}:
            raise ValueError("WEBCOMPARE_CONTENT_GROUP_ACCESS 必须是 0 或 1")
        return cls(
            mode=mode,
            require_login=require_login == "1",
            data_root=Path(os.getenv("WEBCOMPARE_DATA_ROOT", DEFAULT_DATA_ROOT)).expanduser(),
            roots={str(key): Path(value).expanduser().resolve() for key, value in roots.items()},
            mounts=mounts,
            profile=profile,
            client_transport=os.getenv("WEBCOMPARE_CLIENT_TRANSPORT", "auto").strip().lower(),
            base_url=base,
            cache_bytes=int(os.getenv("WEBCOMPARE_CACHE_MIB", "512" if profile == "cloud" else "2048")) * 1024**2,
            preview_effort=_env_int("WEBCOMPARE_PREVIEW_EFFORT", 0),
            png_compression=_env_int("WEBCOMPARE_PNG_COMPRESSION", 6),
            raw_high_format=os.getenv("WEBCOMPARE_RAW_HIGH_FORMAT", "jpeg").strip().lower(),
            member_quota_bytes=_env_int("WEBCOMPARE_MEMBER_QUOTA_MIB", 1536, 1) * 1024**2,
            guest_quota_bytes=_env_int("WEBCOMPARE_GUEST_QUOTA_MIB", 200, 1) * 1024**2,
            guest_max_workspaces=_env_int("WEBCOMPARE_GUEST_MAX_WORKSPACES", 10, 1),
            guest_exit_ttl=_env_int("WEBCOMPARE_GUEST_EXIT_TTL", 600, 0),
            guest_idle_ttl=_env_int("WEBCOMPARE_GUEST_IDLE_TTL", 1800, 0),
            guest_content_ttl=_env_int("WEBCOMPARE_GUEST_CONTENT_TTL", 86400, 0),
            disk_min_free_bytes=_env_int("WEBCOMPARE_DISK_MIN_FREE_MIB", 8192 if profile == "cloud" else 512, 0) * 1024**2,
            site_quota_bytes=_env_int("WEBCOMPARE_SITE_QUOTA_MIB", 4096 if profile == "cloud" else 0, 0) * 1024**2,
            thumbnail_prewarm=prewarm == "1",
            content_group_access=group_access == "1",
            sandbox=sandbox,
            frontend_dir=Path(os.getenv("WEBCOMPARE_FRONTEND_DIR", str(cls().frontend_dir))).resolve(),
        )

    @property
    def login_required(self) -> bool:
        """有效登录要求：personal 可选，server 不受免登录开关影响。"""
        return self.mode == "server" or self.require_login

    def prepare(self) -> None:
        """功能说明：启动前的一致性校验与目录准备。

        调用时机：[main]、[create_app] 与迁移子命令在拿到配置后立即调用。

        关键步骤：
        1. 校验部署模式取值合法；
        2. server 模式且未显式指定状态目录（WEBCOMPARE_DATA_DIR 未设置且仍是
           默认值时）把状态目录改为 <data_root>/.state；
        3. 解析并创建状态目录，权限收紧为 0700；
        4. 解析各快捷目录并校验 ID 字符集，目录可暂时离线；
        5. 校验缓存下限（≥32 MiB）；
        6. server 模式追加内容根、内容区与挂载边界检查，见 [Settings._prepare_server]。

        输出/副作用：创建状态目录（server 模式还创建内容区）；配置非法抛
        ValueError，进程启动失败。
        """
        if self.mode not in MODES:
            raise ValueError("WEBCOMPARE_MODE 必须是 personal 或 server")
        if self.client_transport not in {"auto", "local", "remote"}:
            raise ValueError("WEBCOMPARE_CLIENT_TRANSPORT 必须是 auto、local 或 remote")
        if self.mode == "server" and "WEBCOMPARE_DATA_DIR" not in os.environ and self.data_dir == _default_data_dir():
            self.data_dir = self.data_root / STATE_SUBDIR
        self.data_dir = self.data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.data_dir.chmod(0o700)
        self.roots = {key: path.resolve() for key, path in self.roots.items()}
        for key in self.roots:
            if not _valid_id(key):
                raise ValueError("根目录 ID 只允许字母数字、- 和 _")
        if not 0 <= self.preview_effort <= 6 or not 0 <= self.png_compression <= 9 or self.raw_high_format not in {"jpeg", "webp"}:
            raise ValueError("编码配置需要 preview effort 0–6、PNG compression 0–9、RAW high format jpeg/webp")
        if self.cache_bytes < 32 * 1024**2:
            raise ValueError("缓存限额至少 32 MiB")
        if self.mode == "server":
            self._prepare_server()

    def _prepare_server(self) -> None:
        """功能说明：server 模式专属的启动硬校验与内容区目录准备。

        调用时机：由 [Settings.prepare] 在通用校验之后、仅 server 模式调用。

        关键步骤：
        1. 校验 WEBCOMPARE_BASE_URL 为 HTTPS origin 并检查沙箱模式；
        2. 解析并创建内容根 data_root，默认 0700；启用本机组/ACL 管理时为 0770；
        3. 在内容根下创建 public/private/member/guest 四个内容区，使用相同权限；
        4. 解析挂载点：ID 字符集、access 取值、目录存在性；
        5. 拒绝挂载与内容根或状态目录互相包含，也拒绝挂载之间互相包含。

        输出/副作用：创建内容根与内容区目录；配置非法抛 ValueError。
        """
        if not self.base_url:
            raise ValueError("server 模式必须配置 WEBCOMPARE_BASE_URL")
        _origin(self.base_url, "WEBCOMPARE_BASE_URL")
        if self.sandbox not in SANDBOX_MODES:
            raise ValueError("WEBCOMPARE_SANDBOX 必须是 auto、namespace 或 none")
        self.data_root = self.data_root.resolve()
        self.data_root.mkdir(parents=True, exist_ok=True, mode=self.content_dir_mode)
        if os.name == "posix":
            self.data_root.chmod(self.content_dir_mode)
        for name in CONTENT_DIRS:
            area = self.data_root / name
            if area.is_symlink():
                raise ValueError(f"内容区 {name} 不能是符号链接")
            area.mkdir(exist_ok=True, mode=self.content_dir_mode)
            if os.name == "posix":
                area.chmod(self.content_dir_mode)
        mounts: dict[str, Mount] = {}
        for key, mount in self.mounts.items():
            if not _valid_id(key):
                raise ValueError("挂载 ID 只允许字母数字、- 和 _")
            if mount.access not in MOUNT_ACCESS:
                raise ValueError(f"挂载 {key} 的 access 必须是 ro 或 rw")
            path = mount.path.resolve()
            if not path.is_dir():
                raise ValueError(f"挂载 {key} 不存在或不是目录")
            for label, reserved in (("内容根", self.data_root), ("状态目录", self.data_dir)):
                if _contains(path, reserved) or _contains(reserved, path):
                    raise ValueError(f"挂载 {key} 与{label}不能互相包含")
            mounts[key] = Mount(id=key, path=path, access=mount.access)
        for key, mount in mounts.items():
            for other_key, other in mounts.items():
                if key != other_key and (_contains(mount.path, other.path) or _contains(other.path, mount.path)):
                    raise ValueError(f"挂载 {key} 与挂载 {other_key} 不能互相包含")
        self.mounts = mounts

    def validate_server_identity(self, db: Database) -> None:
        """功能说明：server 模式的账号硬校验：库里必须至少有一个启用中的 admin。

        调用时机：应用装配层在数据库打开之后调用（[Settings.prepare] 执行时
        数据库尚未就绪，因此这项检查与它分开放）。

        关键步骤：
        1. personal 模式直接返回，无额外要求；
        2. 查询 users 表是否存在 role=admin 且 active=1 的行；
        3. 不存在即抛 ValueError，提示先用 CLI 创建管理员账号。

        输出/副作用：只读检查；不满足时抛 ValueError 使启动失败。
        """
        if self.mode != "server":
            return
        if db.one("SELECT u.id FROM users u JOIN local_credentials c ON c.user_id=u.id WHERE u.role='admin' AND u.active=1 LIMIT 1") is None:
            raise ValueError("server 模式需要至少一个启用中的管理员账号，请先运行 webcompare create-user --role admin 创建")

    @property
    def content_dir_mode(self) -> int:
        return 0o770 if self.content_group_access else 0o700

    @property
    def content_file_mode(self) -> int:
        return 0o660 if self.content_group_access else 0o600

    @property
    def secure_cookie(self) -> bool:
        return self.base_url.startswith("https://")

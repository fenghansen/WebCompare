"""【模块】SSH 远端文件 — 个人模式的 SFTP 只读浏览、缓存与视频范围读取

功能说明：在当前 WebCompare 进程内连接用户选择的 SSH 主机，把远端目录转换成
稳定的 ssh://连接ID/路径。数据库仅保存连接配置与已确认的主机指纹；密码、上传
私钥与口令只在内存中存活。每次操作独立打开 SFTP channel，图片按需下载到私密
有界缓存，视频按范围读取，不要求远端安装 WebCompare。
资产版本同时绑定认证后的实际主机名、端口、账号与主机密钥指纹；同一连接配置
改指向其他来源时，旧资产与图片缓存不会沿用。
SFTP v3 通常只有秒级 mtime，且没有可用的 inode；因此大文件同大小、同秒改写
不能可靠辨别。小 JSON 侧车每次读取并比较内容摘要，避免沿用这一时间戳盲区。

## 流程
所属流程：SSH 远端来源（webcompare/ssh.py）

1. **连接与主机验证**：归属隔离、显式指纹确认与仅内存认证 → 见函数 [SSHManager.connect]、[SSHManager.disconnect]
2. **远端路径与目录**：规范 URI、独立 SFTP channel 与有界扫描 → 见函数 [SSHManager.resolve]、[SSHManager.browse]、[SSHManager.collect]
3. **图片与元数据缓存**：有界原子下载与前后版本复核 → 见函数 [SSHManager.materialize]
4. **视频范围读取**：独立 channel、有限块读取与关闭资源 → 见函数 [SSHManager.open_range]、[source_file_response]
5. **统一媒体入口**：普通预览按能力加速，精确数据沿用本机算法 → 见函数 [SourceMediaService.metadata]、[SourceMediaService.render]、[SourceMediaService.pixel]
6. **可选远端预览**：仅使用已有工具，静态脚本与有界输出 → 见函数 [RemotePreviewEngine.metadata]、[RemotePreviewEngine.render]、[SSHManager.execute]
"""
from __future__ import annotations

import base64
import errno
import fnmatch
import getpass
import hashlib
import io
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import stat
import tempfile
import threading
import time
from dataclasses import dataclass, field
from contextlib import contextmanager, suppress
from pathlib import Path, PurePosixPath
from typing import Callable, Iterator, TypeVar
from urllib.parse import quote, unquote, urlsplit

import paramiko

from .config import Settings
from .db import Database
from .downloads import _safe_segment
from .files import FileCatalog, FileError, natural_key
from .storage import Storage

_METADATA_LIMIT = 2 * 1024**2
_JOURNAL_LIMIT = 64 * 1024**2
_SCAN_LIMIT = 100_000
_BROWSE_LIMIT = 10_000
_DIRECTORY_LIMIT = 5_000
_CHUNK = 256 * 1024
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_T = TypeVar("_T")


class SSHError(FileError):
    """可安全返回给浏览器的 SSH 错误；extra 仅包含主机验证信息。"""

    def __init__(self, message: str, code: str = "SSH_INVALID_CONNECTION", status: int = 422, extra: dict | None = None):
        super().__init__(message, code, status)
        self.extra = extra or {}


def _check_exec_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise SSHError("远端预览请求已取消", "SSH_EXEC_CANCELLED", 499)


def is_remote(raw: object) -> bool:
    """功能说明：识别远端 URI 前缀，供装配层分派路径。

    调用时机：本地路径进入 pathlib 之前调用。

    关键步骤：
    1. 只把 ssh:// 开头的字符串视为远端路径。

    输出/副作用：返回布尔值，不访问远端。
    """
    return isinstance(raw, str) and raw.lower().startswith("ssh://")


def _fingerprint(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode("ascii").rstrip("=")


def _uri(connection_id: str, path: str) -> str:
    return "ssh://" + connection_id + quote(path, safe="/")


def _parse(raw: str) -> tuple[str, str]:
    if not isinstance(raw, str) or not raw or len(raw) > 16384 or "\x00" in raw or not is_remote(raw):
        raise SSHError("SSH 路径无效", "INVALID_PATH")
    try:
        parsed = urlsplit(raw)
        path = unquote(parsed.path, errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise SSHError("SSH 路径无效", "INVALID_PATH") from exc
    if not _ID.fullmatch(parsed.netloc) or parsed.query or parsed.fragment or "\x00" in path or not path.startswith("/"):
        raise SSHError("SSH 路径必须包含连接 ID 与绝对路径", "INVALID_PATH")
    return parsed.netloc, posixpath.normpath("/" + path.lstrip("/"))


@dataclass(frozen=True)
class RemoteStat:
    """SFTP 属性的本地 stat 兼容视图；mtime 纳秒值不代表服务器具备纳秒精度。"""

    st_size: int
    st_mtime: float
    st_mtime_ns: int
    st_mode: int
    st_ino: int
    st_dev: int
    st_uid: int = 0
    st_gid: int = 0
    st_atime: float = 0
    st_ctime: float = 0


def _stat(attrs, connection_id: str, path: str, source_namespace: str) -> RemoteStat:
    mtime = float(attrs.st_mtime or 0)
    inode = getattr(attrs, "st_ino", None)
    # SFTP v3 无 inode；不能把链接别名当作不同 inode 伪造身份。
    inode = int(inode) if inode is not None else 0
    return RemoteStat(int(attrs.st_size or 0), mtime, int(mtime * 1_000_000_000), int(attrs.st_mode or 0), inode,
                      int(hashlib.sha256(f"ssh-v2:{connection_id}:{source_namespace}".encode()).hexdigest()[:15], 16),
                      int(getattr(attrs, "st_uid", None) or 0), int(getattr(attrs, "st_gid", None) or 0),
                      float(getattr(attrs, "st_atime", None) or 0), mtime)


def _revision(st: RemoteStat) -> str:
    return hashlib.sha256(f"{st.st_size}:{st.st_mtime_ns}:{st.st_ino}:{st.st_dev}".encode()).hexdigest()[:32]


def _signature(attrs) -> tuple[int, int, int]:
    return int(attrs.st_size or 0), int(attrs.st_mtime or 0), int(attrs.st_mode or 0)


@dataclass(frozen=True)
class RemotePath:
    """绑定连接 ID 的远端 POSIX 路径；故意不实现本地文件系统转换协议。"""

    manager: "SSHManager" = field(repr=False, compare=False)
    connection_id: str
    path: str
    snapshot_stat: RemoteStat | None = field(default=None, compare=False, repr=False)

    def __str__(self) -> str:
        return _uri(self.connection_id, self.path)

    @property
    def name(self) -> str:
        return PurePosixPath(self.path).name

    @property
    def suffix(self) -> str:
        return PurePosixPath(self.path).suffix

    @property
    def stem(self) -> str:
        return PurePosixPath(self.path).stem

    @property
    def parent(self) -> "RemotePath":
        return RemotePath(self.manager, self.connection_id, str(PurePosixPath(self.path).parent))

    def __truediv__(self, child: str) -> "RemotePath":
        value = str(child)
        if "\x00" in value:
            raise SSHError("SSH 路径无效", "INVALID_PATH")
        return RemotePath(self.manager, self.connection_id, posixpath.normpath(posixpath.join(self.path, value)))

    def relative_to(self, other: "RemotePath") -> PurePosixPath:
        if not isinstance(other, RemotePath) or self.connection_id != other.connection_id:
            raise ValueError("不同 SSH 连接的路径不能互相转换")
        return PurePosixPath(self.path).relative_to(PurePosixPath(other.path))

    def stat(self) -> RemoteStat:
        return self.manager._with_sftp(self.connection_id, lambda sftp: _stat(sftp.stat(self.path), self.connection_id, self.path, sftp._webcompare_source_namespace))

    def lstat(self) -> RemoteStat:
        return self.manager._with_sftp(self.connection_id, lambda sftp: _stat(sftp.lstat(self.path), self.connection_id, self.path, sftp._webcompare_source_namespace))

    def is_file(self) -> bool:
        return stat.S_ISREG(self.stat().st_mode)

    def is_dir(self) -> bool:
        return stat.S_ISDIR(self.stat().st_mode)


@dataclass(frozen=True)
class SSHExecResult:
    """有界远端命令结果；stderr 仅计数并丢弃，不能流入 API 或日志。"""

    stdout: bytes
    exit_status: int


@dataclass
class _Connection:
    owner_id: str
    config: dict
    credentials: dict = field(default_factory=dict, repr=False)
    client: object | None = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    disabled: bool = False


class _HostPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, config: dict, known, confirmation: str | None):
        self.config, self.known, self.confirmation = config, known, confirmation
        self.fingerprint = None

    def missing_host_key(self, client, hostname, key):
        current = _fingerprint(key)
        self.fingerprint = current
        previous = self.config.get("host_fingerprint")
        known_keys = []
        for name in self.known[1]:
            values = self.known[0].lookup(name)
            if values:
                known_keys.extend(values.values())
        known_match = any(candidate == key for candidate in known_keys)
        old_key = next((candidate for candidate in known_keys if candidate.get_name() == key.get_name()), None)
        if previous and previous != current:
            changed = True
        else:
            changed = bool(not previous and old_key is not None and old_key != key)
        if self.confirmation == current:
            return
        if not changed and (previous == current or known_match):
            return
        extra = {"host": self.config["host"], "port": self.config["port"], "fingerprint": current, "key_type": key.get_name()}
        old = previous if previous != current else None
        if old is None and old_key is not None and old_key != key:
            old = _fingerprint(old_key)
        if old:
            extra["previous_fingerprint"] = old
        code = "SSH_HOST_KEY_CHANGED" if changed else "SSH_HOST_KEY_UNKNOWN"
        message = "SSH 主机密钥已变化，请核对新旧指纹后确认" if changed else "首次连接此 SSH 主机，请核对并确认主机指纹"
        raise SSHError(message, code, 409, extra)


class SSHManager:
    """个人模式的 SSH 配置、内存连接、私密缓存与只读远端操作入口。"""

    def __init__(self, settings: Settings, db: Database):
        """功能说明：为 personal 模式创建配置表及权限收紧的 SSH 缓存。

        调用时机：应用装配层启动时调用一次。

        关键步骤：
        1. 拒绝 server 模式；创建独立配置表，不改变全局 schema 版本。
        2. 创建仅当前服务账号可访问的缓存，准备连接锁与容量锁。

        输出/副作用：创建 SQLite 附加表与私密缓存目录；不连接主机。
        """
        if settings.mode != "personal":
            raise SSHError("SSH 文件来源仅在 personal 模式启用", "SSH_PERSONAL_ONLY", 403)
        self.settings, self.db = settings, db
        self.cache_dir = settings.data_dir / "ssh-cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache_dir.chmod(0o700)
        self.db.execute("CREATE TABLE IF NOT EXISTS ssh_connections(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, config TEXT NOT NULL, created_at INTEGER NOT NULL)")
        self._states: dict[str, _Connection] = {}
        self._lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._cache_locks: dict[str, threading.RLock] = {}
        self._reserved = 0
        self._temporary_paths: set[Path] = set()
        self._cache_users = 0
        self._sidecar_versions: dict[tuple[str, str], tuple] = {}
        self._closed = False
        self._exec_slots = threading.BoundedSemaphore(2)

    def _load(self, connection_id: str, owner_id: str | None = None) -> _Connection:
        with self._lock:
            if self._closed:
                raise SSHError("SSH 连接管理器已关闭", "SSH_DISCONNECTED", 503)
            row = self.db.one("SELECT * FROM ssh_connections WHERE id=?", (connection_id,))
            if row is None:
                raise SSHError("SSH 连接不存在", "SSH_CONNECTION_NOT_FOUND", 404)
            if owner_id is not None and row["owner_id"] != owner_id:
                raise SSHError("无权使用此 SSH 连接", "SSH_CONNECTION_FORBIDDEN", 403)
            state = self._states.get(connection_id)
            if state is None:
                state = _Connection(row["owner_id"], json.loads(row["config"]))
                self._states[connection_id] = state
            return state

    @staticmethod
    def _active(state: _Connection) -> bool:
        if state.client is None:
            return False
        transport = state.client.get_transport()
        return bool(transport is not None and transport.is_active() and transport.is_authenticated())

    def _profile(self, connection_id: str, state: _Connection) -> dict:
        config = state.config
        profile = {key: config[key] for key in ("label", "host", "port", "username", "auth_method")}
        profile.update(id=connection_id, status="connected" if self._active(state) else "disconnected",
                       home=_uri(connection_id, config.get("home", "/")), root=_uri(connection_id, "/"))
        if config.get("key_path"):
            profile["key_path"] = config["key_path"]
        if config.get("host_fingerprint"):
            profile["host_fingerprint"] = config["host_fingerprint"]
        return profile

    def list(self, owner_id: str) -> list[dict]:
        """功能说明：列出当前账号的连接配置及活连接状态。

        调用时机：文件选择器加载 SSH 来源时调用。

        关键步骤：
        1. 只查询 owner_id 对应的配置，结合内存 Transport 状态构造 profile。

        输出/副作用：不返回凭据，不为列表展示自动连接远端。
        """
        return [self._profile(row["id"], self._load(row["id"], owner_id))
                for row in self.db.all("SELECT id FROM ssh_connections WHERE owner_id=? ORDER BY created_at,id", (owner_id,))]

    def _configuration(self, spec: dict, previous: dict | None = None) -> dict:
        config = dict(previous or {})
        for name in ("host", "port", "username", "label", "auth_method", "key_path"):
            if name in spec and spec[name] is not None:
                config[name] = spec[name]
        host = str(config.get("host", "")).strip()
        if not host or len(host) > 255 or any(character.isspace() for character in host) or any(character in host for character in "\x00/@\\") or host.startswith("-"):
            raise SSHError("请填写有效的 SSH 主机名、地址或配置别名")
        lookup = self._ssh_config(host)
        try:
            port = int(config.get("port") or lookup.get("port", 22))
        except (TypeError, ValueError) as exc:
            raise SSHError("SSH 端口必须为 1–65535 的整数") from exc
        if not 1 <= port <= 65535:
            raise SSHError("SSH 端口必须为 1–65535 的整数")
        username = str(config.get("username") or lookup.get("user") or getpass.getuser()).strip()
        if not username or len(username) > 255 or "\x00" in username or "\n" in username:
            raise SSHError("SSH 用户名无效")
        method = str(config.get("auth_method", "auto"))
        if method not in {"auto", "password", "key"}:
            raise SSHError("SSH 认证方式必须是 auto、password 或 key")
        label = str(config.get("label") or host).strip()
        if not label or len(label) > 120 or "\x00" in label:
            raise SSHError("SSH 连接名称无效")
        key_path = str(config.get("key_path") or "").strip()
        if key_path and (len(key_path) > 4096 or "\x00" in key_path):
            raise SSHError("SSH 私钥路径无效")
        config.update(host=host, port=port, username=username, auth_method=method, label=label)
        if key_path:
            config["key_path"] = str(Path(key_path).expanduser())
        else:
            config.pop("key_path", None)
        return config

    @staticmethod
    def _ssh_config(host: str) -> dict:
        path = Path.home() / ".ssh" / "config"
        if not path.is_file():
            return {}
        try:
            parsed = paramiko.SSHConfig()
            with path.open(encoding="utf-8-sig") as stream:
                parsed.parse(stream)
            return parsed.lookup(host)
        except (OSError, ValueError, paramiko.SSHException) as exc:
            raise SSHError("本机 SSH 配置无法解析", "SSH_CONFIG_INVALID") from exc

    def connect(self, owner_id: str, spec: dict, connection_id: str | None = None) -> dict:
        """功能说明：验证主机与凭据，建立连接并保存无秘密的个人连接配置。

        调用时机：用户新增连接、重新认证或确认主机指纹后调用。

        关键步骤：
        1. 读取并校验所属配置，读取本机 SSH alias 与认证方式。
        2. 已知主机按 known_hosts 或已保存指纹验证，未知或变化的密钥要求显式确认。
        3. 连接成功后保存配置；密码、私钥正文与口令只保留在进程内存。

        输出/副作用：返回不含秘密的 profile；失败时保留原连接与配置。
        """
        if not isinstance(spec, dict) or not owner_id:
            raise SSHError("SSH 连接参数无效")
        previous = self._load(connection_id, owner_id) if connection_id else None
        config = self._configuration(spec, previous.config if previous else None)
        credentials = dict(previous.credentials if previous else {})
        for name in ("password", "private_key", "passphrase"):
            if name in spec:
                value = spec[name]
                if value is not None and not isinstance(value, str):
                    raise SSHError("SSH 认证参数无效")
                if value is not None and len(value) > (1024 * 1024 if name == "private_key" else 16384):
                    raise SSHError("SSH 认证参数过长")
                if value:
                    credentials[name] = value
                else:
                    credentials.pop(name, None)
        confirmation = spec.get("host_fingerprint")
        if confirmation is not None and (not isinstance(confirmation, str) or len(confirmation) > 128):
            raise SSHError("SSH 主机指纹无效")
        try:
            client, home, fingerprint = self._connect_client(config, credentials, confirmation)
        except SSHError as exc:
            if connection_id:
                exc.extra.setdefault("connection_id", connection_id)
            raise
        config = self._authenticated_config(config, client, home, fingerprint)
        connection_id = connection_id or secrets.token_hex(16)
        with self._lock:
            if self._closed:
                client.close()
                raise SSHError("SSH 连接管理器已关闭", "SSH_DISCONNECTED", 503)
            state = previous or _Connection(owner_id, config)
            with state.lock:
                old = state.client
                self.db.execute("INSERT INTO ssh_connections(id,owner_id,config,created_at) VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET config=excluded.config",
                                (connection_id, owner_id, json.dumps(config, ensure_ascii=False), int(time.time())))
                state.config, state.credentials, state.client, state.disabled = config, credentials, client, False
                self._states[connection_id] = state
                if old is not None:
                    old.close()
        return self._profile(connection_id, state)

    @staticmethod
    def _open_sftp(client):
        # Channel reads need their own deadline; SSH keepalive does not bound a
        # silent SFTP server. A setup watchdog also bounds invoke_subsystem's
        # event wait, which Paramiko does not cover with channel.settimeout().
        transport = client.get_transport()
        if transport is None:
            raise SSHError("SSH 连接已断开", "SSH_DISCONNECTED", 503)
        channel = transport.open_session(timeout=10)
        channel.settimeout(15)
        watchdog = threading.Timer(15, channel.close)
        watchdog.daemon = True
        watchdog.start()
        try:
            channel.invoke_subsystem("sftp")
            sftp = paramiko.SFTPClient(channel)
            sftp._webcompare_source_namespace = client._webcompare_source_namespace
            return sftp
        except BaseException:
            channel.close()
            raise
        finally:
            watchdog.cancel()

    @staticmethod
    def _authenticated_config(config: dict, client, home: str, fingerprint: str) -> dict:
        """功能说明：保存本次认证确认的来源身份，不混入密码、私钥或口令。

        调用时机：显式连接和自动重连均在 SSH 及 SFTP 建立成功后调用。

        关键步骤：
        1. 从已认证客户端取得实际目标主机名与稳定的来源命名空间。
        2. 复制无秘密配置，更新 home、指纹与来源身份。

        输出/副作用：返回新配置；每个 SFTP channel 自带同一来源身份，逐文件 stat 不重读 SSH 配置。
        """
        return {**config, "home": home, "host_fingerprint": fingerprint,
                "actual_hostname": client._webcompare_actual_hostname,
                "source_namespace": client._webcompare_source_namespace}

    def _connect_client(self, config: dict, credentials: dict, confirmation: str | None = None):
        lookup = self._ssh_config(config["host"])
        if lookup.get("proxyjump") and str(lookup["proxyjump"]).lower() != "none":
            raise SSHError("此连接使用 ProxyJump；请改用本机 SSH 配置中的 ProxyCommand 后连接", "SSH_CONFIG_UNSUPPORTED")
        hostname = str(lookup.get("hostname") or config["host"])
        port = config["port"]
        known = paramiko.HostKeys()
        for filename in (Path.home() / ".ssh" / "known_hosts", Path("/etc/ssh/ssh_known_hosts")):
            try:
                if filename.is_file():
                    known.load(str(filename))
            except (OSError, ValueError, paramiko.SSHException) as exc:
                raise SSHError("本机 known_hosts 无法安全读取", "SSH_CONFIG_INVALID") from exc
        names = [hostname, config["host"]] if port == 22 else [f"[{hostname}]:{port}", f"[{config['host']}]:{port}"]
        policy = _HostPolicy(config, (known, names), confirmation)
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(policy)
        proxy = None
        try:
            command = lookup.get("proxycommand")
            if command and str(command).lower() != "none":
                command = str(command).replace("%h", hostname).replace("%p", str(port)).replace("%r", config["username"])
                proxy = paramiko.ProxyCommand(command)
            method = config["auth_method"]
            pkey = self._private_key(credentials["private_key"], credentials.get("passphrase")) if credentials.get("private_key") else None
            key_files = [config["key_path"]] if config.get("key_path") else lookup.get("identityfile")
            if method == "password" and not credentials.get("password"):
                raise SSHError("请重新填写 SSH 密码", "SSH_AUTH_REQUIRED")
            if method == "key" and pkey is None and not key_files:
                raise SSHError("请重新提供 SSH 私钥或本机私钥路径", "SSH_AUTH_REQUIRED")
            if method == "password":
                key_files, pkey = None, None
            elif key_files:
                existing = [str(Path(value).expanduser()) for value in key_files if Path(value).expanduser().is_file()]
                if method == "key" and not existing and pkey is None:
                    raise SSHError("本机 SSH 私钥不存在或不可读取", "SSH_AUTH_FAILED")
                key_files = existing or None
            client.connect(hostname, port=port, username=config["username"], password=credentials.get("password"),
                           pkey=pkey, key_filename=key_files, passphrase=credentials.get("passphrase"),
                           allow_agent=method == "auto", look_for_keys=method == "auto", sock=proxy,
                           timeout=10, banner_timeout=10, auth_timeout=10, channel_timeout=10)
            transport = client.get_transport()
            if transport is None:
                raise SSHError("SSH 连接未建立", "SSH_DISCONNECTED", 503)
            transport.set_keepalive(30)
            fingerprint = _fingerprint(transport.get_remote_server_key())
            # Bind version identity to the authenticated endpoint, rather than
            # the reusable profile ID. Capture it on the client/channel so a
            # concurrent profile edit cannot relabel an older SFTP operation.
            identity = json.dumps([hostname, port, config["username"], fingerprint], ensure_ascii=False, separators=(",", ":"))
            client._webcompare_actual_hostname = hostname
            client._webcompare_source_namespace = hashlib.sha256(identity.encode("utf-8")).hexdigest()
            with self._open_sftp(client) as sftp:
                home = sftp.normalize(".")
            return client, posixpath.normpath("/" + home.lstrip("/")), fingerprint
        except SSHError:
            client.close()
            if proxy is not None:
                proxy.close()
            raise
        except (paramiko.AuthenticationException, paramiko.PasswordRequiredException) as exc:
            client.close()
            if proxy is not None:
                proxy.close()
            raise SSHError("SSH 认证失败，请检查用户名、密码或私钥", "SSH_AUTH_FAILED") from exc
        except (OSError, EOFError, ValueError, paramiko.SSHException) as exc:
            client.close()
            if proxy is not None:
                proxy.close()
            raise SSHError("无法连接 SSH 主机，请检查网络与连接配置", "SSH_DISCONNECTED", 503) from exc

    @staticmethod
    def _private_key(value: str, passphrase: str | None):
        for kind in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return kind.from_private_key(io.StringIO(value), password=passphrase)
            except (ValueError, TypeError, paramiko.SSHException):
                continue
        raise SSHError("SSH 私钥或私钥口令无效", "SSH_AUTH_FAILED")

    def disconnect(self, owner_id: str, connection_id: str) -> None:
        """功能说明：关闭自己的活连接并清除内存认证材料。

        调用时机：用户点击断开时调用。

        关键步骤：
        1. 验证配置归属，关闭 Transport，清空密码、私钥与口令。
        2. 保留无秘密配置，暂停自动重连直到用户再次点击连接。

        输出/副作用：不修改远端文件，不删除已保存的连接。
        """
        state = self._load(connection_id, owner_id)
        with state.lock:
            if state.client is not None:
                state.client.close()
            state.client, state.credentials, state.disabled = None, {}, True

    def delete(self, owner_id: str, connection_id: str) -> None:
        self.disconnect(owner_id, connection_id)
        with self._lock:
            self.db.execute("DELETE FROM ssh_connections WHERE id=? AND owner_id=?", (connection_id, owner_id))
            self._states.pop(connection_id, None)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            states = list(self._states.values())
        for state in states:
            with state.lock:
                if state.client is not None:
                    state.client.close()
                state.client, state.credentials = None, {}

    def _client(self, connection_id: str):
        state = self._load(connection_id)
        with state.lock:
            if state.disabled:
                raise SSHError("SSH 连接已断开，请先重新连接", "SSH_AUTH_REQUIRED")
            if not self._active(state):
                if state.client is not None:
                    state.client.close()
                state.client = None
                client, home, fingerprint = self._connect_client(state.config, state.credentials)
                config = self._authenticated_config(state.config, client, home, fingerprint)
                if config != state.config:
                    try:
                        self.db.execute("UPDATE ssh_connections SET config=? WHERE id=?", (json.dumps(config, ensure_ascii=False), connection_id))
                    except BaseException:
                        client.close()
                        raise
                    state.config = config
                state.client = client
            return state, state.client

    def _invalidate(self, state: _Connection, client) -> None:
        with state.lock:
            if state.client is client:
                state.client.close()
                state.client = None

    @staticmethod
    def _operation_error(exc):
        if isinstance(exc, OSError) and exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return SSHError("远端文件或目录不存在", "NOT_FOUND", 404)
        if isinstance(exc, OSError) and exc.errno in {errno.EACCES, errno.EPERM}:
            return SSHError("SSH 账号无权读取此路径", "PATH_FORBIDDEN", 403)
        return None

    def _with_sftp(self, connection_id: str, operation: Callable) -> _T:
        for attempt in range(2):
            sftp = None
            try:
                state, client = self._client(connection_id)
                sftp = self._open_sftp(client)
                return operation(sftp)
            except SSHError as exc:
                exc.extra.setdefault("connection_id", connection_id)
                raise
            except (OSError, EOFError, paramiko.SSHException) as exc:
                error = self._operation_error(exc)
                if error is not None:
                    error.extra["connection_id"] = connection_id
                    raise error from exc
                self._invalidate(state, client)
                if attempt:
                    raise SSHError("SSH 连接已中断，请重新连接后重试", "SSH_DISCONNECTED", 503, {"connection_id": connection_id}) from exc
            finally:
                if sftp is not None:
                    with suppress(OSError, EOFError, paramiko.SSHException):
                        sftp.close()
        raise SSHError("SSH 连接已中断", "SSH_DISCONNECTED", 503)

    def authorize(self, raw: str, owner_id: str) -> str:
        """功能说明：在读远端之前确认 URI 所属连接归当前账号所有。

        调用时机：来源输入和重新绑定资产路径的前置授权检查。

        关键步骤：
        1. 解析 URI 并查询所属配置，不发起网络请求。

        输出/副作用：返回词法规范的 URI；跨账号抛 403。
        """
        connection_id, path = _parse(str(raw))
        self._load(connection_id, owner_id)
        return _uri(connection_id, path)

    def resolve(self, raw: str, owner_id: str | None = None) -> RemotePath:
        """功能说明：验证所属连接并把 URI 规范为存在的远端绝对路径。

        调用时机：浏览、收集与资产访问进入 SFTP 前调用；owner_id 缺省仅供已授权资产入口。

        关键步骤：
        1. 解析不含主机与用户名的连接 ID URI，按需验证归属。
        2. 使用 SFTP normalize 解析真实 POSIX 路径并要求存在。

        输出/副作用：返回 RemotePath；不使用本地 pathlib 解析远端 URI。
        """
        connection_id, path = _parse(str(raw))
        self._load(connection_id, owner_id)
        def resolve(sftp):
            canonical = posixpath.normpath("/" + sftp.normalize(path).lstrip("/"))
            sftp.stat(canonical)
            return RemotePath(self, connection_id, canonical)
        return self._with_sftp(connection_id, resolve)

    @staticmethod
    def _list_entries(sftp, path: str, maximum: int = _SCAN_LIMIT):
        # Drain directory prefetch before issuing stat on the same SFTP channel.
        # Interleaving a stat with listdir_iter's pending requests can consume
        # each other's responses in Paramiko. The accumulator remains bounded.
        entries = []
        for item in sftp.listdir_iter(path, read_aheads=8):
            if len(entries) >= maximum:
                raise SSHError("扫描项目过多，请缩小选择范围", "TOO_MANY_FILES")
            entries.append(item)
        return entries

    def browse(self, raw: str, offset: int, limit: int, q: str = "", sort: str = "name", order: str = "asc", media_kind: str = "image") -> dict:
        """功能说明：有界列举远端目录，提供与本地选择器一致的筛选、排序和分页。

        调用时机：个人文件选择器浏览 SSH 来源时调用。

        关键步骤：
        1. 解析目录，跳过隐藏项与不属于所选媒体类型的文件，按权限跟随可读链接。
        2. 对扫描项和可见条目施加上限，再按自然名称、大小或时间排序。
        3. 目录恒在前，返回稳定 URI 与分页数据。

        输出/副作用：返回 BrowseResult 形状的字典；仅只读扫描远端。
        """
        FileCatalog._validate_media_kind(media_kind)
        if sort not in {"name", "size", "mtime"} or order not in {"asc", "desc"} or offset < 0 or limit < 1:
            raise SSHError("目录分页或排序参数无效", "INVALID_PATH")
        directory = self.resolve(raw)
        def scan(sftp):
            if not stat.S_ISDIR(sftp.stat(directory.path).st_mode):
                raise SSHError("此路径不是目录", "INVALID_PATH")
            entries = []
            scanned = 0
            for item in self._list_entries(sftp, directory.path):
                scanned += 1
                if scanned > _SCAN_LIMIT:
                    raise SSHError("目录项目过多，请选取更具体的目录", "TOO_MANY_FILES")
                name = item.filename
                if name.startswith(".") or name in {".", ".."} or "/" in name or "\x00" in name:
                    continue
                path = directory / name
                mode = item.st_mode or 0
                if stat.S_ISLNK(mode):
                    try:
                        item = sftp.stat(path.path)
                        mode = item.st_mode or 0
                    except OSError:
                        continue
                kind = "directory" if stat.S_ISDIR(mode) else media_kind if stat.S_ISREG(mode) and FileCatalog._is_media(path, media_kind) else None
                if kind:
                    entries.append({"name": name, "path": str(path), "kind": kind, "bytes": int(item.st_size or 0) if kind != "directory" else 0, "mtime": int(item.st_mtime or 0)})
                    if len(entries) > _BROWSE_LIMIT:
                        raise SSHError("目录项目过多，请选取更具体的目录", "TOO_MANY_FILES")
            if q:
                needle = q.casefold()
                if "*" in q:
                    entries = [item for item in entries if fnmatch.fnmatchcase(item["name"].casefold(), needle) or item["kind"] == media_kind and fnmatch.fnmatchcase(PurePosixPath(item["name"]).stem.casefold(), needle)]
                else:
                    entries = [item for item in entries if needle in item["name"].casefold()]
            key = {"name": lambda item: (natural_key(item["name"]), item["name"]), "size": lambda item: item["bytes"], "mtime": lambda item: item["mtime"]}[sort]
            entries.sort(key=key, reverse=order == "desc")
            entries.sort(key=lambda item: item["kind"] != "directory")
            return {"path": str(directory), "parent": None if directory.parent == directory else str(directory.parent),
                    "entries": entries[offset:offset + limit], "count": len(entries), "truncated": len(entries) > offset + limit}
        return self._with_sftp(directory.connection_id, scan)

    def collect(self, raw: str, recursive: bool, media_kind: str = "image") -> list[tuple[str, RemotePath]]:
        """功能说明：把远端单个媒体或目录展开成自然排序的配对键与路径。

        调用时机：创建比较清单时每一路 SSH 来源调用一次。

        关键步骤：
        1. 校验来源媒体类型，单文件直接返回。
        2. 目录扫描跳过隐藏项，按规范目录去重避免链接循环，对扫描项与媒体数施加上限。
        3. 以相对来源目录的去扩展名路径配对并按自然序返回。

        输出/副作用：返回路径列表，不下载媒体、不写远端。
        """
        FileCatalog._validate_media_kind(media_kind)
        source = self.resolve(raw)
        def scan(sftp):
            attrs = sftp.stat(source.path)
            if stat.S_ISREG(attrs.st_mode):
                if not FileCatalog._is_media(source, media_kind):
                    raise SSHError("视频来源只支持 MP4/MOV/M4V/WebM/MKV" if media_kind == "video" else "图片来源支持 PNG/JPEG/WebP/Bayer RAW/二维 NPY", "UNSUPPORTED_FORMAT")
                return [(source.stem, RemotePath(self, source.connection_id, source.path, _stat(attrs, source.connection_id, source.path, sftp._webcompare_source_namespace)))]
            if not stat.S_ISDIR(attrs.st_mode):
                raise SSHError("来源必须是媒体文件或目录", "INVALID_PATH")
            result = []
            stack = [source]
            visited = set()
            scanned = 0
            while stack:
                directory = stack.pop()
                canonical = sftp.normalize(directory.path)
                if canonical in visited:
                    continue
                visited.add(canonical)
                if len(visited) > _DIRECTORY_LIMIT:
                    raise SSHError("目录层级项目过多，请缩小选择范围", "TOO_MANY_FILES")
                for item in self._list_entries(sftp, directory.path, _SCAN_LIMIT - scanned):
                    scanned += 1
                    if scanned > _SCAN_LIMIT:
                        raise SSHError("扫描项目过多，请缩小选择范围", "TOO_MANY_FILES")
                    name = item.filename
                    if name.startswith(".") or "/" in name or "\x00" in name:
                        continue
                    path = directory / name
                    mode = item.st_mode or 0
                    if stat.S_ISLNK(mode):
                        try:
                            item = sftp.stat(path.path)
                            mode = item.st_mode or 0
                        except OSError:
                            continue
                    if stat.S_ISDIR(mode) and recursive:
                        stack.append(path)
                        if len(stack) + len(visited) > _DIRECTORY_LIMIT:
                            raise SSHError("目录层级项目过多，请缩小选择范围", "TOO_MANY_FILES")
                    elif stat.S_ISREG(mode) and FileCatalog._is_media(path, media_kind):
                        snapshot = RemotePath(self, path.connection_id, path.path, _stat(item, path.connection_id, path.path, sftp._webcompare_source_namespace))
                        result.append((path.relative_to(source).with_suffix("").as_posix(), snapshot))
                        if len(result) > self.settings.max_files_per_source:
                            raise SSHError(f"单路最多 {self.settings.max_files_per_source} 个媒体，请选择更具体目录", "TOO_MANY_FILES")
            return sorted(result, key=lambda item: (natural_key(item[0]), item[1].name))
        return self._with_sftp(source.connection_id, scan)

    @contextmanager
    def cache_lease(self):
        """功能说明：在解码、响应发送或导出期间保护已物化的远端源文件。

        调用时机：调用方在 materialize 前进入，并在最后一次使用本地 Path 后退出。

        关键步骤：
        1. 进入时增加全缓存读租约，避免返回的路径被清理。
        2. 正常或异常退出均减少租约计数。

        输出/副作用：租约期间 clear_cache 抛 SSH_CACHE_BUSY，不删除缓存文件。
        """
        with self._cache_lock:
            self._cache_users += 1
        try:
            yield
        finally:
            with self._cache_lock:
                self._cache_users -= 1

    def cache_stats(self) -> dict[str, int]:
        """功能说明：统计私密 SSH 缓存的文件数与磁盘字节数。

        调用时机：设置页显示缓存或判断清理结果时调用。

        关键步骤：
        1. 在容量锁内列举缓存中的普通文件并累加大小。

        输出/副作用：返回 bytes、files；不修改缓存。
        """
        with self._cache_lock:
            sizes = []
            for path in self.cache_dir.rglob("*"):
                try:
                    if path.is_file() and not path.is_symlink() and path not in self._temporary_paths:
                        sizes.append(path.stat().st_size)
                except FileNotFoundError:
                    continue
            return {"bytes": sum(sizes), "files": len(sizes), "max_bytes": self.settings.cache_bytes}

    def clear_cache(self) -> dict[str, int]:
        """功能说明：安全清理 SSH 缓存，活动下载或读租约存在时暂缓。

        调用时机：用户显式清理缓存时调用；物化源的使用者应持有 cache_lease。

        关键步骤：
        1. 检查全缓存读租约，以非阻塞方式尝试全部资源锁。
        2. 忙时抛 SSH_CACHE_BUSY；空闲时删除缓存文件及空目录，清空侧车版本索引。

        输出/副作用：返回 removed、bytes；只删除本机缓存，不触碰远端。
        """
        acquired = []
        with self._cache_lock:
            if self._cache_users or self._reserved:
                raise SSHError("SSH 文件正在读取，请稍后重试清理缓存", "SSH_CACHE_BUSY", 409)
            try:
                for lock in self._cache_locks.values():
                    if not lock.acquire(blocking=False):
                        raise SSHError("SSH 文件正在读取，请稍后重试清理缓存", "SSH_CACHE_BUSY", 409)
                    acquired.append(lock)
                removed = total = 0
                for path in sorted(self.cache_dir.rglob("*"), key=lambda value: len(value.parts), reverse=True):
                    try:
                        if path.is_symlink() or path.is_file():
                            size = path.lstat().st_size
                            path.unlink()
                            removed += 1
                            total += size
                        elif path.is_dir():
                            path.rmdir()
                    except OSError:
                        continue
                self._sidecar_versions.clear()
                return {"removed": removed, "bytes": total}
            finally:
                for lock in reversed(acquired):
                    lock.release()

    def _resource_lock(self, raw: str):
        with self._cache_lock:
            return self._cache_locks.setdefault(raw, threading.RLock())

    def _reserve(self, count: int) -> None:
        with self._cache_lock:
            used = self.cache_stats()["bytes"]
            if count < 0 or used + self._reserved + count > self.settings.cache_bytes:
                raise SSHError("SSH 图片缓存已达容量上限，请清理缓存或提高缓存限额", "SSH_CACHE_FULL", 507)
            if shutil.disk_usage(self.cache_dir).free - self._reserved - count < self.settings.disk_min_free_bytes:
                raise SSHError("本机磁盘空间不足，无法缓存 SSH 图片", "INSUFFICIENT_STORAGE", 507)
            self._reserved += count

    def _release(self, count: int) -> None:
        with self._cache_lock:
            self._reserved -= count

    @staticmethod
    def _changed():
        return SSHError("远端源文件或解析信息已改变，请重新读取目录", "SOURCE_CHANGED", 409)

    def _download(self, sftp, remote: str, target: Path, before) -> None:
        count = int(before.st_size or 0)
        self._reserve(count)
        temporary = None
        reserved = True
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary = tempfile.mkstemp(prefix=".download-", dir=target.parent)
            with self._cache_lock:
                self._temporary_paths.add(Path(temporary))
            with os.fdopen(descriptor, "wb") as output, sftp.open(remote, "rb") as source:
                remaining = count
                while remaining:
                    chunk = source.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise self._changed()
                    output.write(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise self._changed()
                output.flush()
                if _signature(sftp.stat(remote)) != _signature(before):
                    raise self._changed()
            with self._cache_lock:
                os.replace(temporary, target)
                self._temporary_paths.discard(Path(temporary))
                temporary = None
                self._release(count)
                reserved = False
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
                with self._cache_lock:
                    self._temporary_paths.discard(Path(temporary))
            if reserved:
                self._release(count)

    def _sidecars(self, sftp, source: RemotePath, target: Path) -> None:
        for name in dict.fromkeys(("raw.json", source.name + ".json", "meta.json", "frames.jsonl")):
            remote = posixpath.join(source.parent.path, name)
            local = target.parent / (target.name + ".json" if name == source.name + ".json" else name)
            try:
                before = sftp.lstat(remote)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    local.unlink(missing_ok=True)
                    self._sidecar_versions.pop((str(source), name), None)
                    continue
                raise
            if not stat.S_ISREG(before.st_mode or 0):
                local.unlink(missing_ok=True)
                continue
            size = int(before.st_size or 0)
            maximum = _JOURNAL_LIMIT if name == "frames.jsonl" else _METADATA_LIMIT
            if size > maximum:
                raise SSHError("RAW 解析信息超过读取上限，请显式填写 RAW 参数", "RAW_METADATA_LIMIT")
            signature = _signature(before)
            identity = (str(source), name)
            if name == "frames.jsonl" and local.is_file() and self._sidecar_versions.get(identity) == signature:
                continue
            # JSON 总是有界读取，即使大小和秒级时间戳均未变化。
            if name == "frames.jsonl":
                self._download(sftp, remote, local, before)
                if _signature(sftp.lstat(remote)) != signature:
                    raise self._changed()
            else:
                with sftp.open(remote, "rb") as stream:
                    content = stream.read(maximum + 1)
                if len(content) != size or len(content) > maximum or _signature(sftp.lstat(remote)) != signature:
                    raise self._changed()
                digest = hashlib.sha256(content).digest()
                if not local.is_file() or hashlib.sha256(local.read_bytes()).digest() != digest:
                    self._reserve(len(content))
                    temporary = None
                    reserved = True
                    try:
                        descriptor, temporary = tempfile.mkstemp(prefix=".metadata-", dir=target.parent)
                        with self._cache_lock:
                            self._temporary_paths.add(Path(temporary))
                        with os.fdopen(descriptor, "wb") as output:
                            output.write(content)
                        with self._cache_lock:
                            os.replace(temporary, local)
                            self._temporary_paths.discard(Path(temporary))
                            temporary = None
                            self._release(len(content))
                            reserved = False
                    finally:
                        if temporary is not None:
                            Path(temporary).unlink(missing_ok=True)
                            with self._cache_lock:
                                self._temporary_paths.discard(Path(temporary))
                        if reserved:
                            self._release(len(content))
            self._sidecar_versions[identity] = signature

    def materialize(self, raw: str | RemotePath, expected_revision: str | None = None, *, allow_video: bool = False) -> Path:
        """功能说明：把远端图片与固定 RAW 邻接元数据按需放入私密缓存。

        调用时机：媒体解码器需要本地文件时调用；视频播放使用范围流，
        明确导出原视频时可传 allow_video=True，并由调用方持有缓存读租约。

        关键步骤：
        1. 按 URI 合并并发请求，检查版本、缓存预算与磁盘保留空间。
        2. 按有限块原子下载缺少的源文件，传输前后复核 SFTP 属性。
        3. RAW/NPY 仅同步固定普通邻接侧车；JSON 每次比较内容摘要。

        输出/副作用：返回本地 Path，Windows 缓存采用兼容文件名；缓存不驱逐活动文件，达到预算时明确拒绝。
        """
        source = raw if isinstance(raw, RemotePath) and raw.manager is self else self.resolve(str(raw))
        if FileCatalog.is_video(source) and not allow_video:
            raise SSHError("SSH 视频请使用范围流读取", "SSH_VIDEO_STREAM_REQUIRED")
        with self._resource_lock(str(source)):
            def download(sftp):
                before = sftp.stat(source.path)
                if not stat.S_ISREG(before.st_mode or 0):
                    raise SSHError("此 SSH 来源不是普通文件", "INVALID_PATH")
                version = _revision(_stat(before, source.connection_id, source.path, sftp._webcompare_source_namespace))
                if expected_revision is not None and version != expected_revision:
                    raise self._changed()
                identity = hashlib.sha256(str(source).encode()).hexdigest()
                name = source.name
                if os.name == "nt" and ("\\" in name or Storage._windows_unsafe_segment(name)):
                    name = _safe_segment(name)
                target = self.cache_dir / identity / version / name
                if not target.is_file() or target.stat().st_size != int(before.st_size or 0):
                    self._download(sftp, source.path, target, before)
                if source.suffix.lower() in {".raw", ".bayer", ".npy"}:
                    self._sidecars(sftp, source, target)
                if _signature(sftp.stat(source.path)) != _signature(before):
                    raise self._changed()
                return target
            return self._with_sftp(source.connection_id, download)

    def open_range(self, raw: str | RemotePath, start: int, length: int, expected_revision: str | None = None) -> Iterator[bytes]:
        """功能说明：按偏移和长度读取远端视频，持续使用有限大小的数据块。

        调用时机：HTTP 层完成 Range/HEAD 解析后调用，响应关闭时应关闭此迭代器。

        关键步骤：
        1. 检查范围和源版本，独立打开 SFTP channel 与文件句柄。
        2. seek 后每次最多读取 256 KiB，断线时最多重连一次并按剩余偏移续读。
        3. 完成、异常或取消均关闭句柄与 channel，传输结束复核源属性。

        输出/副作用：返回 bytes 迭代器，不整段缓存视频。
        """
        if not isinstance(start, int) or not isinstance(length, int) or start < 0 or length < 0:
            raise SSHError("SSH 文件读取范围无效", "INVALID_RANGE", 416)
        source = raw if isinstance(raw, RemotePath) and raw.manager is self else self.resolve(str(raw))
        return self._range_stream(source, start, length, expected_revision)

    def _range_stream(self, source: RemotePath, start: int, length: int, expected_revision: str | None) -> Iterator[bytes]:
        remaining, cursor = length, start
        version = expected_revision
        for attempt in range(2):
            sftp = stream = None
            try:
                state, client = self._client(source.connection_id)
                sftp = self._open_sftp(client)
                before = sftp.stat(source.path)
                if not stat.S_ISREG(before.st_mode or 0) or start + length > int(before.st_size or 0):
                    raise SSHError("SSH 文件读取范围超出文件大小", "INVALID_RANGE", 416)
                current = _revision(_stat(before, source.connection_id, source.path, sftp._webcompare_source_namespace))
                if version is not None and current != version:
                    raise self._changed()
                version = current
                stream = sftp.open(source.path, "rb")
                stream.seek(cursor)
                while remaining:
                    chunk = stream.read(min(_CHUNK, remaining))
                    if not chunk:
                        raise self._changed()
                    remaining -= len(chunk)
                    cursor += len(chunk)
                    yield chunk
                if _signature(sftp.stat(source.path)) != _signature(before):
                    raise self._changed()
                return
            except SSHError as exc:
                exc.extra.setdefault("connection_id", source.connection_id)
                raise
            except (OSError, EOFError, paramiko.SSHException) as exc:
                error = self._operation_error(exc)
                if error is not None:
                    error.extra["connection_id"] = source.connection_id
                    raise error from exc
                self._invalidate(state, client)
                if attempt:
                    raise SSHError("SSH 视频读取已中断，请重新连接后重试", "SSH_DISCONNECTED", 503, {"connection_id": source.connection_id}) from exc
            finally:
                if stream is not None:
                    with suppress(OSError, EOFError, paramiko.SSHException):
                        stream.close()
                if sftp is not None:
                    with suppress(OSError, EOFError, paramiko.SSHException):
                        sftp.close()


    def execute(self, raw: str | RemotePath, argv: list[str], stdin: bytes = b"", *, expected_revision: str | None = None,
                timeout: float = 12, max_stdout: int = 16 * 1024**2, max_stderr: int = 4096,
                cancel_event: threading.Event | None = None) -> SSHExecResult:
        """功能说明：复用已认证连接，执行无安装依赖的有界远端预览命令。

        调用时机：可选远端预览引擎需要调用已有 Python/Pillow 或 ImageMagick 时。

        关键步骤：
        1. 验证参数，以 shlex.quote 构造 argv；限制 stdin、并发与运行时间。
        2. 同一认证客户端分别打开 SFTP 核验通道与命令通道，前后校验来源版本。
        3. 仅收集有界 stdout，stderr 有界计数后丢弃；所有路径关闭通道。

        输出/副作用：返回 stdout 与退出码；不写远端临时文件、不安装工具，不返回 stderr。
        """
        if (not argv or len(argv) > 32 or any(not isinstance(value, str) or "\x00" in value for value in argv)
                or sum(len(value) for value in argv) > 65536 or not isinstance(stdin, bytes) or len(stdin) > 32768
                or not 0 < timeout <= 60 or not 0 < max_stdout <= 32 * 1024**2 or not 0 <= max_stderr <= 65536):
            raise SSHError("远端预览命令参数无效", "SSH_EXEC_INVALID")
        source = raw if isinstance(raw, RemotePath) and raw.manager is self else self.resolve(str(raw))
        slot_deadline = time.monotonic() + min(timeout, 2)
        while True:
            _check_exec_cancelled(cancel_event)
            if self._exec_slots.acquire(timeout=min(.05, max(0, slot_deadline - time.monotonic()))):
                break
            if time.monotonic() >= slot_deadline:
                raise SSHError("远端预览任务繁忙", "SSH_EXEC_BUSY", 503, {"connection_id": source.connection_id})
        channel = sftp = watchdog = None
        watchdog_stop = threading.Event()
        try:
            _check_exec_cancelled(cancel_event)
            state, client = self._client(source.connection_id)
            sftp = self._open_sftp(client)
            before = sftp.stat(source.path)
            version = _revision(_stat(before, source.connection_id, source.path, sftp._webcompare_source_namespace))
            if expected_revision is not None and version != expected_revision:
                raise self._changed()
            transport = client.get_transport()
            channel = transport.open_session(timeout=min(timeout, 10))
            channel.settimeout(min(timeout, 5))
            deadline = time.monotonic() + timeout

            def watch_channel():
                while not watchdog_stop.wait(min(.05, max(0, deadline - time.monotonic()))):
                    if time.monotonic() >= deadline or cancel_event is not None and cancel_event.is_set():
                        channel.close()
                        return

            watchdog = threading.Thread(target=watch_channel, daemon=True)
            watchdog.start()
            try:
                channel.exec_command(" ".join(shlex.quote(value) for value in argv))
            except paramiko.SSHException as exc:
                _check_exec_cancelled(cancel_event)
                if time.monotonic() >= deadline:
                    raise SSHError("远端预览执行超时", "SSH_EXEC_TIMEOUT", 503) from exc
                raise SSHError("此 SSH 连接不支持远端预览命令", "SSH_EXEC_UNAVAILABLE") from exc
            if stdin:
                channel.sendall(stdin)
            channel.shutdown_write()
            output = bytearray()
            stderr_bytes = 0
            while True:
                _check_exec_cancelled(cancel_event)
                if time.monotonic() >= deadline:
                    raise SSHError("远端预览执行超时", "SSH_EXEC_TIMEOUT", 503)
                while channel.recv_ready():
                    chunk = channel.recv(min(65536, max_stdout - len(output) + 1))
                    output.extend(chunk)
                    if len(output) > max_stdout:
                        raise SSHError("远端预览输出超过读取上限", "SSH_EXEC_OUTPUT_LIMIT", 503)
                while channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(min(4096, max_stderr - stderr_bytes + 1))
                    stderr_bytes += len(chunk)
                    if stderr_bytes > max_stderr:
                        raise SSHError("远端预览错误输出超过读取上限", "SSH_EXEC_OUTPUT_LIMIT", 503)
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    status = channel.recv_exit_status()
                    break
                time.sleep(0.005)
            after = sftp.stat(source.path)
            _check_exec_cancelled(cancel_event)
            if _signature(before) != _signature(after):
                raise self._changed()
            # The SFTP channel is bound to the same authenticated client as the
            # exec channel, so a concurrent profile edit cannot relabel output.
            if _revision(_stat(after, source.connection_id, source.path, sftp._webcompare_source_namespace)) != version:
                raise self._changed()
            return SSHExecResult(bytes(output), status)
        except SSHError as exc:
            exc.extra.setdefault("connection_id", source.connection_id)
            raise
        except (OSError, EOFError, paramiko.SSHException) as exc:
            _check_exec_cancelled(cancel_event)
            raise SSHError("远端预览命令无法完成", "SSH_EXEC_FAILED", 503, {"connection_id": source.connection_id}) from exc
        finally:
            watchdog_stop.set()
            if channel is not None:
                channel.close()
            if sftp is not None:
                with suppress(OSError, EOFError, paramiko.SSHException):
                    sftp.close()
            self._exec_slots.release()

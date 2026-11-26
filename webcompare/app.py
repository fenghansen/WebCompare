"""【模块】HTTP API 与安全边界 — FastAPI 应用组装、会话校验、分享与前端托管

功能说明：浏览器工作台访问的唯一服务端入口。组装 FastAPI 应用与安全
中间件（Origin/CSRF/请求体上限/安全响应头），把比较、媒体与分享请求
校验后分发给文件目录层与媒体解码子进程；所有跨模块调用在此汇聚。
server 模式在同一装配层追加：本地账号认证、访客
工作区会话、区域化浏览/上传/文件管理、三档可见性分享与后台清理任务；
personal 模式保留本地路径语义，并提供可选的配置分享。

## 流程
所属流程：HTTP API（webcompare/app.py）

1. **应用组装与生命周期**：创建应用、挂安全中间件、lifespan 启停媒体服务与访客清理 → 见函数 [create_app]、[janitor_loop]
2. **登录与会话**：Personal 默认补发正常 Cookie/CSRF、显式登录限流、会话和访客工作区校验 → 见函数 [personal_user]、[LocalProvider]、[create_guest_user]
4. **目录与比较**：绝对路径（personal）或授权后的虚拟路径（server）浏览并生成扁平图片清单 → 见函数 [FileCatalog]、[Storage.authorize]
5. **上传与文件管理**：raw body 单图上传与分享包导入、删除与重命名，配额同步记账 → 见函数 [upload_image]、[import_sharepack]、[Quota]
6. **媒体端点**：metadata/render/diff/pixel 委托媒体子进程，带 ETag 缓存 → 见函数 [MediaService]
7. **分享链接**：不可变分享的创建、三档可见性判定、只读恢复与撤销 → 见函数 [ViewState]
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
import shutil
import time
from collections import OrderedDict
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders

from . import __version__
from .auth import PERSONAL_SESSION_PREFIX, LocalProvider, create_guest_user, mark_guest_exited, personal_user, public_user, token_hash, touch_guest_activity
from .config import Settings
from .compression import EncodedMediaGZipMiddleware
from .copying import copy_files
from .moving import authorize_subtree, move_files
from .db import Database
from .downloads import FILES_ONLY_STATE_KEY, is_file_download, prepare_file_download
from .files import FileCatalog, FileError, resolve_path, revision
from .host_memory import available_memory_bytes
from .nvdec_sessions import NVDECSessions
from .nvdec_setup import NVDECInstallError, NVDECSetup, nvdec_installed
from .image_formats import IMAGE_FORMATS
from .source_http import source_file_response
from .media_requests import direct_loopback_request, local_image_transport, while_connected
from .models import FileCopyRequest, FileMoveRequest, ComparisonRequest, ComparisonTemplate, DeleteRequest, FileDownloadRequest, GroupOrderRequest, Login, MkdirRequest, RawOptions, RenameRequest, ReorderRequest, SharePatch, ShareRequest, SSHConnectionRequest
from .templates import create_template, refresh_template, save_template, selection_from_manifest, template_state
from .updates import DesktopUpdater, UpdateError, latest_summary
from .view_state import display_images

SHARE_VISIBILITIES = {
    "admin": frozenset({"personal", "internal", "public"}),
    "member": frozenset({"personal", "internal"}),
    "guest": frozenset({"personal", "public"}),
}
_NVDEC_FRAME_PATH = re.compile(r"/api/assets/[^/]+/nvdec/[^/]+")


def fail(status: int, code: str, message: str):
    raise HTTPException(status, {"code": code, "message": message})


def raw_parameters(value: str | None) -> dict | None:
    if value is None:
        return None
    from pydantic import ValidationError
    try:
        return RawOptions.model_validate_json(value).model_dump(mode="json")
    except ValidationError:
        fail(422, "INVALID_RAW_PARAMETERS", "RAW 参数需为有效 JSON，宽高、Bayer 与显示参数必须在允许范围内")


def page_images(manifest: dict, state: dict) -> tuple[list[str | None], int]:
    """功能说明：按整条显示顺序、分组方式与多位布局选取当前页。

    调用时机：分享和模板校验页码、分享包选择原图时调用。

    关键步骤：
    1. 普通比较先经 [display_images] 切换整条序列，再按布局格数分页；
    2. 分组比较保留 aligned/within 和自然排序，忽略普通来源的显示转置；
    3. 空槽保持原位，返回页内图片与总页数，不修改清单。

    输出/副作用：返回 (当前页图片, 总页数)；布局与来源顺序保持原样。
    """
    rows, columns = map(int, state["layout"].split("x"))
    cells = rows * columns
    page = state["group_index"]
    groups = manifest.get("groups")

    def natural(value: str):
        return tuple((1, len(part.lstrip("0") or "0"), part.lstrip("0") or "0") if part.isascii() and part.isdigit() else (0, part) for part in re.split(r"([0-9]+)", value.lower()))

    def ordered(images):
        if not state.get("natural_sort"):
            return images
        values = sorted((asset for asset in images if asset), key=lambda asset: natural(str(manifest["assets"].get(asset, {}).get("name") or manifest["assets"].get(asset, {}).get("path") or asset)))
        sequence = iter(values)
        return [next(sequence) if asset else asset for asset in images]

    if not groups:
        images = display_images(manifest, state)
        pages = max(1, (len(images) + cells - 1) // cells)
        return images[page * cells:(page + 1) * cells], pages
    if state.get("natural_sort"):
        groups = sorted(groups, key=lambda group: natural(group["label"]))
    if state.get("grouping") == "within":
        counts = [max(1, (len(group["images"]) + cells - 1) // cells) for group in groups]
        pages = sum(counts)
        local = page
        for group, count in zip(groups, counts):
            if local < count:
                return ordered(group["images"])[local * cells:(local + 1) * cells], pages
            local -= count
        return [], pages
    pages = max(1, *(len(group["images"]) for group in groups))
    return [ordered(group["images"])[page] if page < len(group["images"]) else None for group in groups], pages


class LeasedFileResponse(FileResponse):
    def __init__(self, *args, media, lease, **kwargs):
        super().__init__(*args, **kwargs)
        self.media, self.lease = media, lease

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.media.unpin(self.lease)


class SecurityGuard:
    """保留同源和响应头规则，以原生 ASGI 传递流，避免包装器把取消误作正常短 EOF。"""

    def __init__(self, app, settings: Settings, catalog: FileCatalog):
        self.app, self.settings, self.catalog = app, settings, catalog

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        source_lease = ExitStack()

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                # 来源物化和解码此时已完成；视频原字节不依赖图片缓存，持续传输不阻塞清理。
                source_lease.close()
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Referrer-Policy"] = "same-origin"
                headers["X-Frame-Options"] = "DENY"
                headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
                if request.url.path.startswith("/api/") and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            server_loopback = self.settings.mode == "server" and self.settings.client_transport != "remote"
            local_nvdec = (
                server_loopback and
                ((request.method == "POST" and request.url.path in {"/api/auth/login", "/api/local/nvdec/install"}) or
                 (request.method in {"POST", "DELETE"} and _NVDEC_FRAME_PATH.fullmatch(request.url.path))) and
                direct_loopback_request(request)
            )
            if local_nvdec:
                expected = f"{request.url.scheme}://{request.url.netloc}"
            else:
                expected = self.settings.base_url or str(request.base_url).rstrip("/")
                if (server_loopback and request.method == "POST" and request.url.path == "/api/comparisons"
                        and origin != expected and origin == f"{request.url.scheme}://{request.url.netloc}"
                        and direct_loopback_request(request)):
                    # The route must also verify the existing session, CSRF, role and media kind.
                    request.state.loopback_comparison_origin = True
                    expected = origin
                origin = origin.rstrip("/") if origin else origin
            if origin and origin != expected:
                response = JSONResponse({"detail": {"code": "ORIGIN_FORBIDDEN", "message": "请求必须来自比图服务本身"}}, status_code=403)
                return await response(scope, receive, guarded_send)
            if request.headers.get("sec-fetch-site") == "cross-site":
                response = JSONResponse({"detail": {"code": "ORIGIN_FORBIDDEN", "message": "不允许跨站修改请求"}}, status_code=403)
                return await response(scope, receive, guarded_send)
        if request.url.path == "/api/cache/clear":
            return await self.app(scope, receive, guarded_send)
        # 后台导出另有完整任务租约；请求在发头前保护来源，异常时也确定退出。
        with source_lease:
            source_lease.enter_context(self.catalog.cache_lease())
            await self.app(scope, receive, guarded_send)


class BodyLimit:
    """Bound JSON before framework buffering, including chunked requests."""
    def __init__(self, app, limit: int = 16 * 1024**2, exempt: tuple = ()):
        self.app, self.limit, self.exempt = app, limit, tuple(exempt)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await self.app(scope, receive, send)
        if any(scope.get("path", "").startswith(prefix) for prefix in self.exempt):
            return await self.app(scope, receive, send)
        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                response = JSONResponse({"detail": {"code": "BODY_TOO_LARGE", "message": "请求过大"}}, status_code=413)
                return await response(scope, receive, send)
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class DesktopUpdateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DesktopUpdatePreferences(DesktopUpdateCommand):
    automatic: StrictBool


def create_app(settings: Settings | None = None, *, native_desktop: bool = False,
               desktop_update: DesktopUpdater | None = None) -> FastAPI:
    """功能说明：组装并返回 WebCompare 的 FastAPI 应用（uvicorn 应用工厂）。

    调用时机：CLI 的 serve 子命令经 `uvicorn webcompare.app:create_app
    --factory` 调用一次；测试里每个用例也以独立临时目录调用一次。

    关键步骤：
    1. 读取并校验配置，打开 SQLite；server 模式先校验管理员存在
       （[Settings.validate_server_identity]）、初始化站内预算
       （[Quota.ensure_site_budget]）并同步跑一轮访客清理（[sweep]，重启补清理）；
    2. 构建文件目录 [FileCatalog]（server 模式附带 [Storage] 边界，资产按
       虚拟路径登记）与本地认证提供者 [LocalProvider]；
    3. 挂 GZip 与请求体上限中间件（server 模式豁免 /api/uploads 与
       /api/import-sharepack 两条流式端点），再加统一安全中间件：非 GET
       请求校验 Origin/Sec-Fetch-Site，响应统一加 nosniff、CSP 等安全头，
       /api/ 默认 no-store；
    4. 注册依赖注入 require_session（会话 Cookie + CSRF 双重校验；server
       模式对访客追加工作区有效性复核与活动节流刷新）、optional_session
       （公开分享的匿名入口）与 require_member（viewer 角色只读分享）；
       显式开启认证及 server 模式均拒绝带来源前缀的自动个人 Cookie；
    5. 注册路由：登录/登出/会话、providers 与访客工作区（server
       限定）、根目录浏览、比较清单、上传/导入/文件管理（server 限定）、
       资产 metadata/render/pixel/diff、分享创建/列表/改档/恢复/撤销；
       Personal 免登录的 /api/auth/session 经 [personal_user] 解析持久身份，
       再签发标准会话；其他端点继续使用相同 Cookie、CSRF 与 Origin 校验；
    6. 媒体端点先经 read_asset 做分享可见性/角色/区域鉴权与源变更检测，
       再交给 [MediaService]，渲染结果带 ETag 与 pin 保护流式返回；
    7. lifespan 启动时创建媒体解码子进程服务（server 模式先同步补跑一轮
       清理并启动 [janitor_loop] 后台任务），关闭时停任务并等待媒体服务退出；
    8. 兜底路由安全地托管 frontend/dist 静态文件与 /s/{id} 分享页。

    输出/副作用：返回就绪的 FastAPI 应用；运行期写入 SQLite（会话、比较、
    分享、存储登记、配额预留与访客工作区）与媒体缓存目录；不修改任何图片
    源文件（server 模式的写入口只落在存储区域之内）。
    """
    settings = settings or Settings.from_env()
    settings.prepare()
    db = Database(settings.data_dir / "webcompare.sqlite3")
    server = settings.mode == "server"
    max_pixels = settings.max_pixels if server else None
    storage = None
    quota = None
    share_key = None
    if server:
        from . import lifecycle
        from .quota import Quota
        from .storage import Storage
        from .upload import import_sharepack, upload_image

        settings.validate_server_identity(db)
        if settings.sandbox == "namespace":
            from .sandbox import _namespace_available
            if not _namespace_available():
                raise ValueError("server 要求完整解码隔离，但 bubblewrap 不可用")
        # 站点密钥随 SQLite 备份和迁移，保证同一分享在重启后仍生成相同短链。
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO site_settings VALUES('share_key', lower(hex(randomblob(32))))")
            share_key = bytes.fromhex(conn.execute("SELECT value FROM site_settings WHERE key='share_key'").fetchone()[0])
        storage = Storage(settings, db)
        quota = Quota(settings, db, storage)
        quota.ensure_site_budget()
    remote = None
    if not server:
        from .ssh import SSHManager
        remote = SSHManager(settings, db)
    catalog = FileCatalog(settings, db, storage, remote)
    local = LocalProvider(db)
    nvdec_setup = NVDECSetup(settings.data_dir)
    attempts: OrderedDict[str, list[float]] = OrderedDict()
    login_semaphore = asyncio.Semaphore(2)
    upload_semaphore = asyncio.Semaphore(4 if settings.profile == "intranet" else 2)
    active_uploads = 0

    def existing_thumbnails():
        cursor = ""
        while True:
            rows = db.all("SELECT path,owner_id FROM stored_files WHERE path>? ORDER BY path LIMIT 32", (cursor,))
            if not rows:
                return
            for row in rows:
                cursor = row["path"]
                if FileCatalog.is_image(Path(row["path"])):
                    yield (row["owner_id"], row["path"])

    def thumbnail_source(item):
        owner_id, path = item
        owner = public_user(db, owner_id)
        if not owner or not db.one("SELECT 1 FROM stored_files WHERE path=? AND owner_id=?", (path, owner_id)):
            fail(404, "NOT_FOUND", "预制源文件已删除或所属账号不可用")
        virtual = storage.authorize(owner, path, "read")
        reject_expired_guest_asset(virtual)
        source = storage.resolve(virtual)
        return source, revision(source)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .media import MediaService
        app.state.nvdec = NVDECSessions()
        app.state.media = MediaService(settings.data_dir / "cache", cache_bytes=settings.cache_bytes, max_pixels=max_pixels, disk_min_free_bytes=settings.disk_min_free_bytes if server else 0, sandbox="namespace" if server and settings.profile == "cloud" else settings.sandbox if server else None, max_pending=2 if settings.profile == "cloud" else 16, preview_effort=settings.preview_effort, png_compression=settings.png_compression, raw_high_format=settings.raw_high_format, allow_source_passthrough=not server)
        if remote is not None:
            from .source_media import SourceMediaService
            app.state.media = SourceMediaService(app.state.media, catalog)
        from .exports import ExportService
        app.state.exports = ExportService(settings, db, validate_export, check_export_disk, source_lease=catalog.cache_lease)
        janitor = None
        janitor_stop = None
        if server:
            # 重启补清理：每次 lifespan 进入都先同步跑一轮，再挂后台周期任务。
            cleaned = lifecycle.sweep(settings, db, storage)
            if any(cleaned.get(key, 0) for key in ("guest_files_expired", "workspaces_purged")):
                await app.state.media.clear_cache(defer_leased=True)
            janitor_stop = asyncio.Event()
            janitor = asyncio.create_task(lifecycle.janitor_loop(janitor_stop, settings, db, storage, clear_cache=app.state.media.clear_cache))
            if settings.thumbnail_prewarm:
                app.state.media.start_thumbnail_warmup(existing_thumbnails, thumbnail_source, lambda: active_uploads == 0 and not app.state.exports.tasks)
        # 保持一个无事务连接，避免每次短连接关闭都在机械盘触发 WAL checkpoint。
        with db.connect() as keeper:
            keeper.execute("SELECT version FROM schema_version").fetchall()
            if desktop_update is not None:
                desktop_update.start()
            try:
                yield
            finally:
                if desktop_update is not None:
                    desktop_update.close()
                if janitor is not None:
                    janitor_stop.set()
                    await janitor
                await app.state.nvdec.close()
                await app.state.exports.close()
                await app.state.media.close()
                if remote is not None:
                    await run_in_threadpool(remote.close)

    app = FastAPI(title="WebCompare", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.db, app.state.catalog = settings, db, catalog
    app.state.storage, app.state.quota = storage, quota
    app.state.remote = remote
    app.add_middleware(EncodedMediaGZipMiddleware, minimum_size=1024)
    app.add_middleware(BodyLimit, exempt=("/api/uploads", "/api/import-sharepack") if server else ())
    app.add_middleware(SecurityGuard, settings=settings, catalog=catalog)

    @app.exception_handler(FileError)
    async def file_error(_request, exc):
        return JSONResponse({"detail": {"code": exc.code, "message": str(exc), **getattr(exc, "extra", {})}}, status_code=exc.status)

    # Media is imported lazily here so no native decoder enters the HTTP process.
    from .media import MediaError
    @app.exception_handler(MediaError)
    async def media_error(_request, exc):
        headers = {"Retry-After": "1"} if exc.status == 429 else None
        return JSONResponse({"detail": {"code": exc.code, "message": str(exc)}}, status_code=exc.status, headers=headers)

    def workspace_payload(user: dict) -> dict:
        """访客会话附带的保留规则：工作区 ID（= 用户 ID）、配额与三档 TTL 秒数。"""
        return {"id": user["id"], "quota_bytes": settings.guest_quota_bytes, "content_ttl_seconds": settings.guest_content_ttl, "exit_ttl_seconds": settings.guest_exit_ttl, "idle_ttl_seconds": settings.guest_idle_ttl}

    def local_nvdec(request, user):
        role_ok = user['role'] == 'admin' if server else user['role'] in {'admin', 'member'}
        return role_ok and settings.client_transport != 'remote' and direct_loopback_request(request) and nvdec_installed()

    def can_install_nvdec(request: Request | None, user: dict) -> bool:
        if settings.client_transport == "remote" or not direct_loopback_request(request):
            return False
        if server:
            return user["role"] == "admin"
        if user["role"] not in {"admin", "member"}:
            return False
        # Match personal_user's existing owner, or its read-only selection on an unbound legacy store.
        owner = db.one(
            "SELECT COALESCE((SELECT user_id FROM identities WHERE provider='personal' AND subject='owner'),"
            " (SELECT u.id FROM users u JOIN local_credentials c ON c.user_id=u.id"
            " WHERE u.active=1 AND u.role IN ('member','admin') ORDER BY u.created_at,u.rowid LIMIT 1)) AS id"
        )
        return owner["id"] == user["id"]

    def with_nvdec_setup(payload: dict, request: Request | None) -> dict:
        if can_install_nvdec(request, payload["user"]):
            status = nvdec_setup.status()
            payload["nvdec_setup"] = status
            if status == "ready":
                payload["local_nvdec"] = True
        return payload

    def session_payload(user: dict, csrf: str, request: Request | None = None) -> dict:
        """登录/会话/访客创建共用载荷；前端按 login_required 决定登录与退出入口。"""
        payload = {"user": user, "csrf": csrf, "profile": settings.profile, "limits": {"max_pixels": max_pixels, "max_sources": settings.max_sources, "max_files_per_source": settings.max_files_per_source}, "mode": settings.mode, "login_required": settings.login_required, "local_images": local_image_transport(request, settings)}
        if local_nvdec(request, user):
            payload["local_nvdec"] = True
        if server and user["role"] == "guest":
            payload["workspace"] = workspace_payload(user)
        return payload

    def check_guest_workspace(user: dict) -> None:
        """访客会话的额外闸门：工作区被清理、退出超保留期或闲置超时就视同未登录。

        到期判定与 [sweep] 一致：exited_at 非空且 now > exited_at + exit_ttl，
        或 now - last_active_at > idle_ttl。放行时令牌侧节流刷新 last_active_at。
        """
        row = db.one("SELECT exited_at,last_active_at,purged FROM guest_workspaces WHERE user_id=?", (user["id"],))
        now = int(time.time())
        if not row or row["purged"]:
            fail(401, "LOGIN_REQUIRED", "访客工作区已清理，请重新进入")
        if row["exited_at"] is not None and now > int(row["exited_at"]) + settings.guest_exit_ttl:
            fail(401, "LOGIN_REQUIRED", "访客工作区已过保留期，请重新进入")
        if now - int(row["last_active_at"]) > settings.guest_idle_ttl:
            fail(401, "LOGIN_REQUIRED", "访客工作区闲置超时，请重新进入")
        touch_guest_activity(db, user["id"])

    def require_session(request: Request) -> dict:
        raw = request.cookies.get("wc_session", "")
        if not raw or len(raw) > 200:
            fail(401, "LOGIN_REQUIRED", "请先登录比图服务")
        # 自动 Cookie 仍按完整令牌散列查询；前缀只用于收紧开关后的认证边界。
        if settings.login_required and raw.startswith(PERSONAL_SESSION_PREFIX):
            fail(401, "LOGIN_REQUIRED", "已开启登录认证，请使用账号重新登录")
        session = db.one("SELECT * FROM sessions WHERE token_hash=? AND expires_at>?", (token_hash(raw), int(time.time())))
        if not session:
            fail(401, "LOGIN_REQUIRED", "登录已失效，请重新登录")
        user = public_user(db, session["user_id"])
        if not user:
            fail(401, "LOGIN_REQUIRED", "账号不可用")
        if raw.startswith(PERSONAL_SESSION_PREFIX) and user["role"] not in {"member", "admin"}:
            fail(401, "LOGIN_REQUIRED", "默认个人账号已降权，请重新确认账号状态")
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
                fail(403, "CSRF_FAILED", "会话校验失败，请刷新页面")
        if server and user["role"] == "guest":
            check_guest_workspace(user)
        return session_payload(user, session["csrf"], request)

    def optional_session(request: Request) -> dict | None:
        """公开分享允许匿名查看；失效的登录 Cookie 同样按匿名处理。"""
        try:
            return require_session(request)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            return None

    def require_member(session=Depends(require_session)):
        if session["user"]["role"] not in {"member", "admin"}:
            fail(403, "SHARE_ONLY", "此账号只能查看收到的分享链接")
        return session

    # server 模式把“能否进这个目录/用这张比图”的判定全部交给 storage.authorize
    # 与分享可见性规则，这些端点的会话门槛因此降为“已登录”；personal 模式维持
    # 原 member/admin 门槛逐字不变。媒体与分享查看端点在 server 模式允许匿名，
    # 由 read_asset/can_view_share 按可见性放行或拒绝。
    require_area_session = require_session if server else require_member
    media_session = optional_session if server else require_session

    def sign_session(response: Response, user_id: str, request: Request | None = None, *, personal: bool = False) -> dict:
        """签发统一 Cookie 与 CSRF；自动个人会话带来源前缀，其他登录保留原令牌格式。"""
        now = time.time()
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        if personal:
            raw = PERSONAL_SESSION_PREFIX + raw
        with db.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (int(now),))
            old = request.cookies.get("wc_session", "") if request is not None else ""
            if old:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(old),))
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(raw), user_id, csrf, int(now) + settings.session_days * 86400))
        response.set_cookie("wc_session", raw, max_age=settings.session_days * 86400, httponly=True, secure=settings.secure_cookie, samesite="lax", path="/")
        return with_nvdec_setup(session_payload(public_user(db, user_id), csrf, request), request)

    def owned_comparison(comparison_id: str, user: dict):
        row = db.one("SELECT * FROM comparisons WHERE id=?", (comparison_id,))
        if not row:
            fail(404, "NOT_FOUND", "比较不存在")
        if row["owner_id"] != user["id"] and user["role"] != "admin":
            fail(403, "FORBIDDEN", "此比较属于其他用户，请使用分享链接")
        return row

    def valid_share(share_id: str):
        row = db.one("SELECT s.*,COALESCE(s.template_manifest,c.manifest) AS manifest,u.display_name FROM shares s JOIN comparisons c ON c.id=s.comparison_id JOIN users u ON u.id=s.owner_id WHERE s.id=? AND s.revoked=0 AND u.active=1", (share_id,))
        if not row:
            fail(404, "SHARE_NOT_FOUND", "分享不存在或已撤销")
        return row

    def can_view_share(row: dict, user: dict | None) -> None:
        """server 模式的分享可见性闸门：public 免登录；internal 需 member/admin；personal 需 owner/admin。"""
        visibility = row["visibility"]
        if visibility == "public":
            return
        if user is None:
            fail(401, "LOGIN_REQUIRED", "请先登录比图服务")
        if visibility == "internal" and user["role"] in {"member", "admin"}:
            return
        if user["id"] == row["owner_id"] or user["role"] == "admin":
            return
        fail(403, "SHARE_FORBIDDEN", "此分享对你不可见")

    def reject_expired_guest_asset(raw_path: str) -> None:
        """guest 内容同时受工作区与单文件期限约束，清理前也立即失效。"""
        if storage._absolute(raw_path):
            raw_path = storage.virtual_for(storage.resolve(raw_path))
        area, rel = storage.area_of(raw_path)
        if area != "guest":
            return
        now = int(time.time())
        workspace = db.one("SELECT purged,exited_at,last_active_at FROM guest_workspaces WHERE user_id=?", (rel.split("/", 1)[0],))
        if not workspace or workspace["purged"] or (workspace["exited_at"] is not None and now >= int(workspace["exited_at"]) + settings.guest_exit_ttl) or now >= int(workspace["last_active_at"]) + settings.guest_idle_ttl:
            fail(404, "ASSET_EXPIRED", "访客工作区已过保留期")
        row = db.one("SELECT created_at FROM stored_files WHERE path=?", (raw_path,))
        if not row or int(row["created_at"]) + settings.guest_content_ttl <= now:
            fail(404, "ASSET_EXPIRED", "访客内容已过保留期")

    def read_asset(asset_id: str, session: dict | None, share: str | None, *, materialize: bool = False):
        if share:
            row = valid_share(share)
            if server:
                can_view_share(row, session["user"] if session else None)
            if asset_id not in json.loads(row["manifest"])["assets"]:
                fail(403, "ASSET_FORBIDDEN", "图片不在此分享中")
        elif session is None:
            fail(401, "LOGIN_REQUIRED", "请先登录比图服务")
        elif not server and session["user"]["role"] == "viewer":
            fail(403, "SHARE_REQUIRED", "查看图片需要有效的分享链接")
        saved = db.one("SELECT path FROM assets WHERE id=?", (asset_id,))
        if saved and not share and not server:
            catalog.authorize(saved["path"], session["user"]["id"])
        asset, path = catalog.asset(asset_id, materialize=materialize)
        if server:
            if not share:
                storage.authorize(session["user"], asset["path"], "read")
            reject_expired_guest_asset(asset["path"])
        return asset, path

    def authorize_read_path(user: dict, raw: str) -> str:
        """server 模式的路径闸门：authorize(read) 得规范化虚拟路径，再 resolve 成真实路径喂给目录层。"""
        return str(storage.resolve(storage.authorize(user, raw, "read")))

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "version": __version__}

    if not server:
        @app.get("/api/personal/memory")
        def personal_memory(request: Request, _session=Depends(require_session)) -> dict[str, int | None]:
            if settings.client_transport != "remote" and direct_loopback_request(request):
                return {"available_bytes": available_memory_bytes()}
            return {"available_bytes": None}

    @app.get("/api/updates/latest")
    def get_latest_update():
        try:
            return latest_summary()
        except UpdateError as error:
            fail(503, "UPDATE_CHECK_UNAVAILABLE", str(error))

    if native_desktop and desktop_update is not None:
        @app.get("/api/desktop/update")
        def desktop_update_status(_session=Depends(require_member)):
            return desktop_update.snapshot()

        @app.post("/api/desktop/update/check")
        def desktop_update_check(_body: DesktopUpdateCommand | None = None, _session=Depends(require_member)):
            return desktop_update.check()

        @app.post("/api/desktop/update/download")
        def desktop_update_download(_body: DesktopUpdateCommand | None = None, _session=Depends(require_member)):
            return desktop_update.download()

        @app.post("/api/desktop/update/install")
        def desktop_update_install(_body: DesktopUpdateCommand | None = None, _session=Depends(require_member)):
            return desktop_update.install()

        @app.post("/api/desktop/update/preferences")
        def desktop_update_preferences(body: DesktopUpdatePreferences, _session=Depends(require_member)):
            try:
                return desktop_update.set_automatic(body.automatic)
            except OSError:
                fail(503, "UPDATE_PREFERENCES_UNAVAILABLE", "无法保存自动更新设置。")

    @app.get("/api/auth/providers")
    async def providers():
        # 免登录时仍可显式登录已有账号，local 表示能力，login_required 表示入口要求。
        result = {"mode": settings.mode, "guest": server, "local": True, "login_required": settings.login_required}
        if native_desktop:
            result["native_desktop"] = True
        if server:
            result["guest_max_workspaces"] = settings.guest_max_workspaces
        return result

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        recent = [t for t in attempts.pop(ip, []) if now - t < 60]
        if len(recent) >= 10:
            attempts[ip] = recent
            fail(429, "LOGIN_RATE_LIMIT", "尝试过于频繁，请一分钟后再试")
        recent.append(now)
        attempts[ip] = recent
        while len(attempts) > 1024:
            attempts.popitem(last=False)
        async with login_semaphore:
            user_id = await run_in_threadpool(local.authenticate, body.username, body.password)
        if not user_id:
            fail(401, "BAD_CREDENTIALS", "用户名或密码不正确")
        user = public_user(db, user_id)
        if not user:
            fail(401, "BAD_CREDENTIALS", "账号不可用")
        if server and user["role"] in {"member", "admin"}:
            await run_in_threadpool(storage.ensure_private, user_id, user["username"])
        return sign_session(response, user_id, request)

    @app.get("/api/auth/session")
    async def current(request: Request, response: Response):
        """Personal 免登录只在此补发正常会话；其他 API 继续要求 Cookie 与 CSRF。"""
        try:
            return with_nvdec_setup(require_session(request), request)
        except HTTPException as exc:
            if settings.login_required or exc.status_code != 401:
                raise
        user = await run_in_threadpool(personal_user, db)
        return sign_session(response, user["id"], request, personal=True)

    @app.post("/api/local/nvdec/install")
    async def install_nvdec(request: Request, session=Depends(require_session)):
        if not can_install_nvdec(request, session["user"]):
            fail(403, "NVDEC_INSTALL_FORBIDDEN", "仅本机工作台所有者或 Server 管理员可安装 NVIDIA 硬件解码组件")
        if await request.body():
            fail(400, "NVDEC_INSTALL_BAD_REQUEST", "此安装入口无需请求正文")
        try:
            await run_in_threadpool(nvdec_setup.install)
        except NVDECInstallError as exc:
            fail(exc.status, exc.code, str(exc))
        return {"status": "ready"}

    @app.post("/api/auth/logout")
    async def logout(request: Request, response: Response, session=Depends(require_session)):
        db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(request.cookies.get("wc_session", "")),))
        response.delete_cookie("wc_session", path="/")
        return {"ok": True}

    if server:

        guest_attempts: OrderedDict[str, list[float]] = OrderedDict()

        @app.post("/api/guests")
        async def create_guest(request: Request, response: Response):
            ip = request.client.host if request.client else "unknown"
            now = time.time()
            recent = [t for t in guest_attempts.pop(ip, []) if now - t < 3600]
            if len(recent) >= 10:
                guest_attempts[ip] = recent
                fail(429, "GUEST_RATE_LIMIT", "访客创建过于频繁，请稍后再试")
            recent.append(now)
            guest_attempts[ip] = recent
            while len(guest_attempts) > 1024:
                guest_attempts.popitem(last=False)
            created = await run_in_threadpool(create_guest_user, db, settings)
            # 上传/导入不自动建目录，访客工作区继承本机内容组的目录模式。
            workspace = settings.data_root / "guest" / created["user_id"]
            workspace.mkdir(parents=True, mode=settings.content_dir_mode)
            workspace.chmod(settings.content_dir_mode)
            return sign_session(response, created["user_id"])

        @app.post("/api/guests/exit")
        async def guest_exit(request: Request, response: Response, session=Depends(require_session)):
            if session["user"]["role"] != "guest":
                fail(403, "NOT_GUEST", "只有访客会话可以退出工作区")
            mark_guest_exited(db, session["user"]["id"])
            db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash(request.cookies.get("wc_session", "")),))
            response.delete_cookie("wc_session", path="/")
            return {"ok": True}

    if not server:
        @app.get("/api/ssh/connections")
        async def ssh_connections(session=Depends(require_member)):
            return {"connections": await run_in_threadpool(remote.list, session["user"]["id"])}

        @app.post("/api/ssh/connections")
        async def ssh_add(body: SSHConnectionRequest, session=Depends(require_member)):
            if not body.host:
                fail(422, "SSH_INVALID_CONFIG", "请输入 SSH 主机或本机 SSH 配置别名")
            return await run_in_threadpool(remote.connect, session["user"]["id"], body.connection_spec())

        @app.post("/api/ssh/connections/{connection_id}/connect")
        async def ssh_connect(connection_id: str, body: SSHConnectionRequest, session=Depends(require_member)):
            return await run_in_threadpool(remote.connect, session["user"]["id"], body.connection_spec(), connection_id)

        @app.post("/api/ssh/connections/{connection_id}/disconnect")
        async def ssh_disconnect(connection_id: str, session=Depends(require_member)):
            await run_in_threadpool(remote.disconnect, session["user"]["id"], connection_id)
            return {"ok": True}

        @app.delete("/api/ssh/connections/{connection_id}")
        async def ssh_delete(connection_id: str, session=Depends(require_member)):
            await run_in_threadpool(remote.delete, session["user"]["id"], connection_id)
            return {"ok": True}

    @app.api_route("/api/video", methods=["GET", "HEAD"])
    async def browse_video(path: str, request: Request, session=Depends(require_area_session)):
        """文件选择页按需读取视频封面；授权边界与图片缩略图一致，服务端不解码。"""
        if server:
            virtual = storage.authorize(session["user"], path, "read")
            reject_expired_guest_asset(virtual)
            source = storage.resolve(virtual)
        else:
            await run_in_threadpool(catalog.authorize, path, session["user"]["id"])
            source = await run_in_threadpool(catalog.resolve, path)
        if not await run_in_threadpool(lambda: source.is_file() and FileCatalog.is_video(source)):
            fail(404, "NOT_FOUND", "不是可预览的视频")
        return await source_file_response(catalog, source, request)

    @app.get("/api/thumb")
    async def thumb(path: str, request: Request, raw: str | None = Query(None, max_length=4096), session=Depends(require_area_session)):
        if server:
            virtual = storage.authorize(session["user"], path, "read")
            reject_expired_guest_asset(virtual)
            source = storage.resolve(virtual)
        else:
            await run_in_threadpool(catalog.authorize, path, session["user"]["id"])
            source = await run_in_threadpool(catalog.resolve, path)
        if not await run_in_threadpool(lambda: source.is_file() and FileCatalog.is_image(source)):
            fail(404, "NOT_FOUND", "不是可预览的图片")
        result = await while_connected(request, app.state.media.render(source, await run_in_threadpool(revision, source), max_size=160, quality="preview", raw=raw_parameters(raw)))
        if server:
            try:
                storage.authorize(session["user"], virtual, "read")
                reject_expired_guest_asset(virtual)
            except BaseException:
                app.state.media.discard(result.path)
                raise
        return image_response(request, result)

    @app.get("/api/cache")
    async def cache_stats(session=Depends(require_member)):
        result = app.state.media.cache_stats()
        if remote is not None:
            result["sources"] = await run_in_threadpool(remote.cache_stats)
        return result

    @app.post("/api/cache/clear")
    async def cache_clear(session=Depends(require_member)):
        sources = await run_in_threadpool(remote.clear_cache) if remote is not None else None
        return {"ok": True, **await app.state.media.clear_cache(), "sources": sources}

    @app.get("/api/roots")
    async def roots(session=Depends(require_area_session)):
        if not server:
            return {"home": str(Path.home()), "shortcuts": [{"id": key, "label": key, "path": str(path)} for key, path in settings.roots.items()]}
        user = session["user"]
        return {"areas": storage.browse_roots(user), "home": str(Path.home()) if user["role"] == "admin" else None, "shortcuts": []}

    @app.get("/api/browse")
    async def browse(path: str = "", offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500), q: str = Query("", max_length=200), sort: str = Query("name", pattern="^(name|size|mtime)$"), order: str = Query("asc", pattern="^(asc|desc)$"), media_kind: str = Query("image", pattern="^(image|video)$"), session=Depends(require_area_session)):
        if not server:
            await run_in_threadpool(catalog.authorize, path, session["user"]["id"])
        target = authorize_read_path(session["user"], path) if server else path or str(Path.home())
        result = await run_in_threadpool(catalog.browse, target, offset, limit, q, sort, order, media_kind)
        if server:
            result["writable"] = storage.writable(session["user"], result["path"].rstrip("/") + "/__webcompare_write_probe__", mkdir=True)
            result["root_only_folders"] = result["path"] == "member"
            for entry in result["entries"]:
                entry["writable"] = storage.writable(session["user"], entry["path"])
        return result

    @app.post("/api/comparisons")
    async def create_comparison(body: ComparisonRequest, request: Request, session=Depends(require_area_session)):
        if getattr(request.state, "loopback_comparison_origin", False) and (
                session["user"]["role"] != "admin" or body.media_kind != "video"):
            fail(403, "ORIGIN_FORBIDDEN", "本机来源只允许管理员创建视频比较")
        groups = None if body.groups is None else [group.model_dump() for group in body.groups]
        sources = [s.model_dump() for s in body.sources]
        if remote is not None:
            await run_in_threadpool(authorize_selection, body, session["user"]["id"])
        if server:
            if body.image_order:
                fail(422, "INVALID_IMAGE_ORDER", "image_order 仅供 personal 普通清单使用")
            user = session["user"]
            sources = [{**source, "path": authorize_read_path(user, source["path"])} for source in sources]
            if groups is not None:
                groups = [{**group, "rules": [{**rule, "path": authorize_read_path(user, rule["path"])} for rule in group["rules"]]} for group in groups]
        return await run_in_threadpool(catalog.create_comparison, session["user"]["id"], sources, body.combine, body.recursive, groups, body.media_kind, image_order=body.image_order)

    if not server:
        def authorize_selection(body: ComparisonRequest, owner_id: str):
            """规则、普通来源和重排路径统一核验 SSH 归属，再进入公共目录层。"""
            paths = [source.path for source in body.sources] + body.image_order
            paths += [rule.path for group in body.groups or [] for rule in group.rules]
            for raw in paths:
                catalog.authorize(raw, owner_id)

        @app.get("/api/template-schema")
        async def template_schema(session=Depends(require_member)):
            return ComparisonTemplate.model_json_schema()

        @app.post("/api/templates")
        async def template_create(body: ComparisonTemplate, session=Depends(require_member)):
            await run_in_threadpool(authorize_selection, body.comparison, session["user"]["id"])
            return await run_in_threadpool(create_template, settings, db, session["user"], body, remote=remote)

    @app.get("/api/comparisons/{comparison_id}")
    async def comparison(comparison_id: str, session=Depends(require_session)):
        manifest = json.loads(owned_comparison(comparison_id, session["user"])["manifest"])
        if server and manifest.get("groups"):
            manifest["selection"] = selection_from_manifest(manifest)
        return manifest

    @app.put("/api/comparisons/{comparison_id}/images")
    async def reorder_images(comparison_id: str, body: ReorderRequest, session=Depends(require_member)):
        row = owned_comparison(comparison_id, session["user"])
        manifest = json.loads(row["manifest"])
        if "groups" in manifest:
            fail(422, "GROUPED_COMPARISON", "分组比较只能按整组调整顺序")
        if sorted(body.images) != sorted(manifest["images"]):
            fail(422, "INVALID_IMAGES", "图片清单与现有比较不一致")
        manifest["images"] = body.images
        if not server:
            manifest["selection"] = selection_from_manifest(manifest)
            manifest["selection"]["image_order"] = [manifest["assets"][asset_id]["path"] for asset_id in body.images]
        db.execute("UPDATE comparisons SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), comparison_id))
        return {"ok": True}

    @app.put("/api/comparisons/{comparison_id}/groups")
    async def reorder_groups(comparison_id: str, body: GroupOrderRequest, session=Depends(require_member)):
        row = owned_comparison(comparison_id, session["user"])
        manifest = json.loads(row["manifest"])
        groups = manifest.get("groups")
        if not groups:
            fail(422, "NOT_GROUPED", "此比较没有分组")
        if len(body.order) != len(groups) or sorted(body.order) != list(range(len(groups))):
            fail(422, "INVALID_GROUP_ORDER", "组顺序必须是现有分组索引的完整排列")
        manifest["groups"] = [groups[index] for index in body.order]
        manifest["images"] = [asset_id for group in manifest["groups"] for asset_id in group["images"]]
        if manifest.get("selection", {}).get("groups"):
            manifest["selection"]["groups"] = [manifest["selection"]["groups"][index] for index in body.order]
        db.execute("UPDATE comparisons SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), comparison_id))
        return manifest

    if server:

        @app.post("/api/uploads")
        async def upload_file(request: Request, dir: str = Query(min_length=1, max_length=4096), name: str = Query(min_length=1, max_length=4096), media_kind: str = Query("image", pattern="^(image|video)$"), session=Depends(require_session)):
            nonlocal active_uploads
            # 排队而非立拒：并发上传等待通道空位，30 秒等不到才 429。
            try:
                await asyncio.wait_for(upload_semaphore.acquire(), timeout=30)
            except TimeoutError:
                fail(429, "UPLOAD_BUSY", "上传排队超时，请稍后重试")
            try:
                active_uploads += 1
                result = await upload_image(request.stream(), settings=settings, storage=storage, quota=quota, user=session["user"], target_dir=dir, filename=name, media_kind=media_kind)
                if media_kind == "image":
                    app.state.media.queue_thumbnail((session["user"]["id"], result["path"]))
                return result
            finally:
                active_uploads -= 1
                upload_semaphore.release()

        @app.post("/api/import-sharepack")
        async def import_pack(request: Request, dir: str = Query(min_length=1, max_length=4096), session=Depends(require_session)):
            nonlocal active_uploads
            try:
                await asyncio.wait_for(upload_semaphore.acquire(), timeout=30)
            except TimeoutError:
                fail(429, "UPLOAD_BUSY", "上传排队超时，请稍后重试")
            try:
                active_uploads += 1
                result = await import_sharepack(request.stream(), settings=settings, storage=storage, quota=quota, user=session["user"], target_dir=dir)
                for item in result["imported"]:
                    app.state.media.queue_thumbnail((session["user"]["id"], item["path"]))
                return result
            finally:
                active_uploads -= 1
                upload_semaphore.release()

        @app.post("/api/files/mkdir")
        async def make_directory(body: MkdirRequest, session=Depends(require_session)):
            with storage.mutation_lock:
                virtual = storage.authorize(session["user"], body.path, "mkdir")
                target = storage._verified(virtual, allow_missing=True)
                if target.exists():
                    if not target.is_dir():
                        fail(409, "NAME_EXISTS", "同名文件已存在")
                    return {"ok": True, "path": virtual, "created": False}
                if not target.parent.is_dir():
                    fail(404, "NOT_FOUND", "父文件夹不存在")
                quota.check_disk_floor(4096)
                target.mkdir(mode=settings.content_dir_mode)
                target.chmod(settings.content_dir_mode)
                try:
                    db.execute("INSERT INTO stored_directories(path,owner_id,created_at) VALUES(?,?,?)", (virtual, session["user"]["id"], int(time.time())))
                except BaseException:
                    target.rmdir()
                    raise
            return {"ok": True, "path": virtual, "created": True}

        @app.post("/api/files/delete")
        async def delete_file(body: DeleteRequest, session=Depends(require_session)):
            with storage.mutation_lock:
                virtual = storage.authorize(session["user"], body.path, "write")
                target = storage.resolve(virtual)
                if target.is_dir():
                    authorize_subtree(storage, session["user"], virtual)
                    shutil.rmtree(target)
                else:
                    target.unlink()
                quota.remove(virtual)
            return {"ok": True}

        @app.post("/api/files/rename")
        async def rename_file(body: RenameRequest, session=Depends(require_session)):
            result = await run_in_threadpool(move_files, catalog, session["user"], [body.path],
                                             new_path=body.new_path, storage=storage, quota=quota)
            return {"ok": True, "path": result["paths"][0]}

    @app.api_route("/api/assets/{asset_id}/video", methods=["GET", "HEAD"])
    async def video_bytes(asset_id: str, request: Request, share: str | None = None, session=Depends(media_session)):
        """授权后流式发送原视频单段 Range；不解码、不展开帧、不创建服务器缓存。"""
        _, path = await run_in_threadpool(read_asset, asset_id, session, share, materialize=False)
        if not catalog.is_video(path):
            fail(422, "NOT_VIDEO", "资源不是视频")
        return await source_file_response(catalog, path, request)

    @app.api_route("/api/assets/{asset_id}/nvdec/{reader_id}", methods=["POST", "DELETE"])
    async def nvdec_frame(asset_id: str, reader_id: UUID, request: Request, pts_us: int = Query(0, ge=0),
                          previous_us: int | None = Query(None, ge=0), preview_width: int | None = Query(None, ge=1, le=65535),
                          share: str | None = None,
                          session=Depends(require_session)):
        if not session.get("local_nvdec"):
            fail(403, "LOCAL_NVDEC_UNAVAILABLE", "本机 NVDEC 未启用")
        key = (session["user"]["id"], asset_id, share, reader_id)
        if request.method == "DELETE":
            await app.state.nvdec.discard(key)
            return Response(status_code=204)
        asset, path = await run_in_threadpool(read_asset, asset_id, session, share, materialize=False)
        if not isinstance(path, Path) or not catalog.is_video(path):
            fail(422, "NVDEC_UNSUPPORTED_SOURCE", "本机 NVDEC 需要磁盘上的视频文件")
        from .nvdec import NVDECError, NVDECMemoryError
        try:
            frame = await app.state.nvdec.frame(key, path, asset["revision"], pts_us, previous_us, preview_width)
        except NVDECMemoryError as exc:
            fail(507, "NVDEC_MEMORY", str(exc))
        except NVDECError as exc:
            fail(422, "NVDEC_UNAVAILABLE", str(exc))
        return Response(frame, media_type="application/octet-stream", headers={"Cache-Control": "private, no-store", "Vary": "Cookie", "Content-Encoding": "identity"})

    @app.get("/api/assets/{asset_id}/metadata")
    async def metadata(asset_id: str, request: Request, share: str | None = None, raw: str | None = Query(None, max_length=4096), session=Depends(media_session)):
        _, path = await run_in_threadpool(read_asset, asset_id, session, share)
        result = await while_connected(request, app.state.media.metadata(path, raw=raw_parameters(raw)))
        return result

    def crop_box(x, y, width, height):
        values = (x, y, width, height)
        if all(v is None for v in values):
            return None
        if any(v is None for v in values) or x < 0 or y < 0 or width <= 0 or height <= 0:
            fail(422, "INVALID_ROI", "裁剪框需同时提供有效的 x/y/width/height")
        return values

    def image_response(request: Request, result):
        etag = '"' + result.etag.strip('"') + '"'
        headers = {"Cache-Control": "private, no-cache", "ETag": etag, "X-Image-Width": str(result.width), "X-Image-Height": str(result.height), "X-Image-Quality": result.quality, "Vary": "Cookie"}
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
        lease = app.state.media.pin(result.path)
        return LeasedFileResponse(result.path, media_type=result.mime, headers=headers, media=app.state.media, lease=lease)

    @app.get("/api/assets/{asset_id}/render")
    async def render(asset_id: str, request: Request, share: str | None = None, quality: str = Query("preview", pattern="^(preview|high|exact)$"), full: bool = False, max_size: int = Query(1024, ge=1, le=2048), x: int | None = None, y: int | None = None, width: int | None = None, height: int | None = None, raw: str | None = Query(None, max_length=4096), session=Depends(media_session)):
        asset, path = await run_in_threadpool(read_asset, asset_id, session, share)
        box = crop_box(x,y,width,height)
        if full and box is not None:
            fail(422, "INVALID_ARGUMENT", "整图模式不能同时指定裁剪框")
        result = await while_connected(request, app.state.media.render(path, asset["revision"], box=box, max_size=max_size, quality=quality, full=full, raw=raw_parameters(raw), local_lossless=local_image_transport(request, settings)))
        return image_response(request, result)

    @app.get("/api/diff")
    async def diff(request: Request, left: str, right: str, share: str | None = None, quality: str = Query("exact", pattern="^(preview|high|exact)$"), full: bool = False, max_size: int = Query(1024, ge=1, le=2048), x: int | None = None, y: int | None = None, width: int | None = None, height: int | None = None, raw: str | None = Query(None, max_length=4096), session=Depends(media_session)):
        a, pa = await run_in_threadpool(read_asset, left, session, share)
        b, pb = await run_in_threadpool(read_asset, right, session, share)
        box = crop_box(x,y,width,height)
        if full and box is not None:
            fail(422, "INVALID_ARGUMENT", "整图模式不能同时指定裁剪框")
        result = await while_connected(request, app.state.media.render(pa, a["revision"], box=box, max_size=max_size, quality=quality, full=full, diff_source=pb, diff_revision=b["revision"], raw=raw_parameters(raw), local_lossless=local_image_transport(request, settings)))
        return image_response(request, result)

    @app.get("/api/assets/{asset_id}/pixel")
    async def pixel(asset_id: str, request: Request, x: int = Query(ge=0), y: int = Query(ge=0), share: str | None = None, raw: str | None = Query(None, max_length=4096), session=Depends(media_session)):
        _, path = await run_in_threadpool(read_asset, asset_id, session, share)
        result = await while_connected(request, app.state.media.pixel(path, x, y, raw=raw_parameters(raw)))
        return result

    def export_items(body: ShareRequest, user: dict):
        comparison = owned_comparison(body.comparison_id, user)
        manifest = json.loads(comparison["manifest"])
        state = body.state.model_dump()
        if state["brightness_ref"] is None:
            state.pop("brightness_ref")
        if state["video_frame"] == 0 and not state["video_offsets"]:
            state.pop("video_frame")
            state.pop("video_offsets")
        if not state["panels"]:
            state.pop("panels")
        if state["grouping"] == "aligned":
            state.pop("grouping")
        if not state["natural_sort"]:
            state.pop("natural_sort")
        if not state["transposed"]:
            state.pop("transposed")
        if state["raw"] == RawOptions().model_dump():
            state.pop("raw")
        ids, _pages = page_images(manifest, state)
        items = [catalog.asset(a) for a in dict.fromkeys(ids) if a]
        authorize_export(items, user)
        if len(items) < 1:
            fail(422, "EMPTY_PAGE", "当前页没有图片")
        if len(items) > 64:
            fail(422, "EXPORT_TOO_MANY", "分享包最多 64 张图")
        for _asset, path in items:
            if path.suffix.lower() not in IMAGE_FORMATS:
                fail(422, "EXPORT_FORMAT", "分享包仅支持 PNG、JPEG 和静态 WebP 图片")
        total = sum(path.stat().st_size for _asset, path in items)
        limit = 1024**3 if user["role"] == "admin" else 100 * 1024**2
        if total > limit:
            fail(413, "EXPORT_TOO_LARGE", "文件尺寸过大不可zip打包分享")
        return comparison, state, items, total, limit

    def authorize_export(items, user):
        if not server:
            for asset, _path in items:
                catalog.authorize(asset["path"], user["id"])
        if server:
            for asset, _path in items:
                storage.authorize(user, asset["path"], "read")
                reject_expired_guest_asset(asset["path"])
                if user["role"] == "guest" and not asset["path"].startswith(f"guest/{user['id']}/"):
                    fail(403, "PATH_FORBIDDEN", "访客只能导出自己的临时内容")

    def validate_export(row, *, check=None):
        """压缩读取当前文件；成品 ZIP 按任务归属和保留期限提供下载。"""
        owner = public_user(db, row["owner_id"])
        if not owner or owner["role"] == "viewer":
            fail(403, "EXPORT_FORBIDDEN", "打包所属账号不可用")
        if row["status"] == "ready":
            if server:
                for saved in json.loads(row["assets"]):
                    asset = db.one("SELECT path FROM assets WHERE id=?", (saved["id"],))
                    if asset and asset["path"].startswith("guest/"):
                        reject_expired_guest_asset(asset["path"])
            return []
        files_only = is_file_download(row)
        items = []
        for saved in json.loads(row["assets"]):
            item = catalog.asset(saved["id"], allow_video=files_only)
            if files_only:
                # 远端版本用于任务授权，本机文件描述符版本用于实际 ZIP 复制核验。
                local_revision = revision(item[1])
                item = ({**item[0], "archive_name": saved["archive_name"], "revision": local_revision}, item[1])
            items.append(item)
        authorize_export(items, owner)
        if check is not None and server and not files_only:
            from .sandbox import validate_image
            for _asset, path in items:
                check(0)
                validate_image(path, settings.max_pixels, mode=settings.sandbox)
                check(0)
        return items

    def check_export_disk(incoming):
        if server:
            # 状态缓存可能与数据根分属不同分区，因此直接检查输出所在分区。
            if shutil.disk_usage(settings.data_dir).free - incoming < settings.disk_min_free_bytes:
                fail(507, "DISK_FLOOR", "磁盘剩余空间不足，无法打包")

    def owned_export(job_id, user):
        row = app.state.exports.get(job_id)
        if row["owner_id"] != user["id"] and user["role"] != "admin":
            fail(403, "EXPORT_FORBIDDEN", "只能访问自己的打包任务")
        return row

    @app.get("/api/quota")
    async def quota_status(session=Depends(require_area_session)):
        if not server:
            fail(404, "SERVER_ONLY", "personal 模式不启用配额")
        with db.connect() as conn:
            conn.execute("BEGIN")
            user_id = session["user"]["id"]
            used = conn.execute("SELECT COALESCE(SUM(bytes),0) FROM stored_files WHERE owner_id=?", (user_id,)).fetchone()[0]
            reserved = conn.execute("SELECT COALESCE(SUM(bytes),0) FROM quota_reservations WHERE owner_id=?", (user_id,)).fetchone()[0]
            limit = quota._limit(conn, user_id)
            result = {"used_bytes": used, "reserved_bytes": reserved, "limit_bytes": limit, "remaining_bytes": max(0, limit - used - reserved) if limit is not None else None}
            if session["user"]["role"] == "admin":
                result.update(site_budget_bytes=quota._site_budget(conn), site_used_bytes=quota._site_usage(conn))
        return result

    @app.post("/api/exports")
    async def create_export(body: ShareRequest, session=Depends(require_area_session)):
        comparison, state, items, total, limit = await run_in_threadpool(export_items, body, session["user"])
        assets = [{"id": asset["id"], "revision": asset["revision"], "bytes": path.stat().st_size} for asset, path in items]
        row = app.state.exports.create(session["user"]["id"], comparison["id"], assets, state, total, limit)
        return app.state.exports.payload(row)

    @app.post("/api/file-copies")
    async def paste_files(body: FileCopyRequest, session=Depends(require_member)):
        """粘贴时才在线程池执行有界文件复制；剪贴板写入不经过此接口。"""
        try:
            await asyncio.wait_for(upload_semaphore.acquire(), timeout=30)
        except asyncio.TimeoutError:
            fail(429, "COPY_BUSY", "文件写入任务繁忙，请稍后重试")
        try:
            return await run_in_threadpool(copy_files, catalog, session["user"], body.paths,
                                           body.destination, body.recursive, body.media_kind,
                                           storage=storage, quota=quota)
        finally:
            upload_semaphore.release()

    @app.post("/api/file-moves")
    async def move_selected_files(body: FileMoveRequest, session=Depends(require_member)):
        """同作用域整体改名，不复制、不解码，也不占用新增文件额度。"""
        return await run_in_threadpool(move_files, catalog, session["user"], body.paths,
                                       body.destination, storage=storage, quota=quota)

    @app.post("/api/file-downloads")
    async def create_file_download(body: FileDownloadRequest, session=Depends(require_area_session)):
        """功能说明：为文件选择器选中的原媒体创建私有 ZIP 下载任务。

        调用时机：用户下载单个、多个文件或选中目录时由前端 POST 调用；
        会话依赖沿用既有身份与 CSRF 校验，后续轮询/下载/删除复用 exports。

        关键步骤：
        1. 按角色设置普通用户 100 MiB、管理员 1 GiB 的原字节上限；
        2. 工作线程内有界展开路径，server 模式每项重走区域授权与访客期限；
        3. 创建带私有类型标记的队列任务，共享并发、缓存、磁盘、超时与租约；
        4. 队列拒绝时删除本次专用比较记录，避免失败请求残留无用清单。

        输出/副作用：返回与分享包一致的 ExportRecord，不直接读取整个 ZIP
        到内存；成功任务的下载附件名为 webcompare-files.zip。
        """
        user = session["user"]
        limit = 1024**3 if user["role"] == "admin" else 100 * 1024**2

        def authorize_download_path(raw):
            """功能说明：把一个下载路径转成当前身份可读取的真实路径。

            调用时机：专用收集器处理来源及每个展开子项之前调用。

            关键步骤：
            1. personal 沿用服务账号可读绝对路径，server 先区域授权再解析；
            2. guest 只允许自家区域，常规文件再核验工作区和单文件有效期。

            输出/副作用：返回真实路径；越权或到期抛统一 HTTP 错误，不写盘。
            """
            if not server:
                catalog.authorize(raw, user["id"])
                return catalog.resolve(raw)
            virtual = storage.authorize(user, raw, "read")
            if user["role"] == "guest" and virtual != f"guest/{user['id']}" and not virtual.startswith(f"guest/{user['id']}/"):
                fail(403, "PATH_FORBIDDEN", "访客只能导出自己的临时内容")
            path = storage.resolve(virtual)
            if path.is_file():
                reject_expired_guest_asset(virtual)
            return path

        manifest, assets, total = await run_in_threadpool(prepare_file_download, catalog, user["id"], body.paths,
                                                         body.recursive, body.media_kind, authorize_download_path, limit)
        try:
            row = app.state.exports.create(user["id"], manifest["id"], assets,
                                           {FILES_ONLY_STATE_KEY: 1, "media_kind": body.media_kind}, total, limit)
        except BaseException:
            db.execute("DELETE FROM comparisons WHERE id=?", (manifest["id"],))
            raise
        return app.state.exports.payload(row)

    @app.get("/api/exports/{job_id}")
    async def export_status(job_id: str, session=Depends(require_area_session)):
        return app.state.exports.payload(owned_export(job_id, session["user"]))

    @app.get("/api/exports/{job_id}/download")
    async def download_export(job_id: str, session=Depends(require_area_session)):
        row = owned_export(job_id, session["user"])
        if row["status"] != "ready":
            fail(409, "EXPORT_NOT_READY", "压缩包尚不可下载")
        await run_in_threadpool(validate_export, row)
        path = app.state.exports.path(job_id)
        if not path.is_file():
            fail(404, "EXPORT_NOT_FOUND", "压缩包已清理，请重新打包")
        lease = app.state.exports.pin(path)
        filename = "webcompare-files.zip" if is_file_download(row) else "webcompare-share.zip"
        return LeasedFileResponse(path, media_type="application/zip", filename=filename, media=app.state.exports, lease=lease)

    @app.delete("/api/exports/{job_id}")
    async def delete_export(job_id: str, session=Depends(require_area_session)):
        owned_export(job_id, session["user"])
        app.state.exports.remove(job_id)
        return {"ok": True}

    @app.post("/api/export-zip")
    async def export_zip(body: ShareRequest, session=Depends(require_area_session)):
        # 保留旧的同步下载契约，同时复用相同队列、缓存预算与权限闸门。
        result = await create_export(body, session)
        task = app.state.exports.tasks[result["id"]][0]
        await asyncio.shield(task)
        row = owned_export(result["id"], session["user"])
        if row["status"] != "ready":
            fail(422, "EXPORT_FAILED", row["error"] or "打包失败，请重新打包")
        response = await download_export(result["id"], session)
        async def remove_after_download():
            app.state.exports.remove(result["id"])
        response.background = BackgroundTask(remove_after_download)
        return response

    @app.post("/api/shares")
    async def create_share(body: ShareRequest, session=Depends(require_area_session)):
        comparison = owned_comparison(body.comparison_id, session["user"])
        if server and (body.selection_mode != "fixed" or body.page_views):
            fail(422, "INVALID_SELECTION_MODE", "server 模式使用固定分享")
        if server:
            allowed = SHARE_VISIBILITIES.get(session["user"]["role"])
            if allowed is None:
                fail(403, "SHARE_ONLY", "此账号不能创建分享")
            if body.visibility not in allowed:
                fail(422, "INVALID_VISIBILITY", "当前角色不能使用该可见性档位")
        elif body.visibility != "personal":
            fail(422, "INVALID_VISIBILITY", "personal 模式不支持可见性设置")
        manifest = json.loads(comparison["manifest"])
        if not server and body.selection_mode == "editable":
            config = ComparisonTemplate(comparison=selection_from_manifest(manifest), state=body.state,
                                        page_views=body.page_views)
            request = config.comparison
            current = await run_in_threadpool(catalog.create_comparison, session["user"]["id"],
                [source.model_dump() for source in request.sources], request.combine, request.recursive,
                None if request.groups is None else [group.model_dump() for group in request.groups],
                request.media_kind, persist=False, image_order=request.image_order)
            current["id"] = manifest["id"]
            return await run_in_threadpool(save_template, settings, db, session["user"], config, current)
        if body.page_views:
            fail(422, "INVALID_SELECTION_MODE", "各页配置请使用可编辑配置分享")
        state = body.state.model_dump()
        if state["brightness_ref"] is None:
            state.pop("brightness_ref")
        if state["video_frame"] == 0 and not state["video_offsets"]:
            state.pop("video_frame")
            state.pop("video_offsets")
        if not state["panels"]:
            state.pop("panels")
        if state["grouping"] == "aligned":
            state.pop("grouping")
        if not state["natural_sort"]:
            state.pop("natural_sort")
        if not state["transposed"]:
            state.pop("transposed")
        if state["raw"] == RawOptions().model_dump():
            state.pop("raw")
        _ids, pages = page_images(manifest, state)
        if state["group_index"] >= pages:
            fail(422, "INVALID_GROUP", "页码超出范围")
        if server:
            for asset in manifest["assets"].values():
                storage.authorize(session["user"], asset["path"], "read")
                reject_expired_guest_asset(asset["path"])
                if session["user"]["role"] == "guest" and not asset["path"].startswith(f"guest/{session['user']['id']}/"):
                    fail(403, "PATH_FORBIDDEN", "访客只能分享自己的临时内容")
        # 内容寻址：同一比较 + 同一视图状态（server 模式再含可见性档位）⇒ 同一链接，
        # 重复分享幂等不膨胀；误撤销后再次分享同状态会恢复原链接（同主重新分享视为重新授权）。
        canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        material = comparison["id"] + "\0" + canonical + ("\0" + body.visibility if server else "")
        share_id = (hmac.new(share_key, b"webcompare-share\0" + material.encode(), hashlib.sha256) if share_key is not None else hashlib.sha256(material.encode())).hexdigest()[:32]
        existing = db.one("SELECT revoked FROM shares WHERE id=?", (share_id,))
        if existing:
            if existing["revoked"]:
                db.execute("UPDATE shares SET revoked=0 WHERE id=?", (share_id,))
            return {"id": share_id, "url": f"{settings.base_url}/s/{share_id}", "selection_mode": "fixed"}
        db.execute("INSERT INTO shares(id,owner_id,comparison_id,state,created_at,visibility) VALUES(?,?,?,?,?,?)", (share_id, session["user"]["id"], comparison["id"], json.dumps(state), int(time.time()), body.visibility if server else "personal"))
        return {"id": share_id, "url": f"{settings.base_url}/s/{share_id}", "selection_mode": "fixed"}

    @app.get("/api/shares")
    async def list_shares(session=Depends(require_session)):
        user = session["user"]
        if user["role"] == "admin":
            rows = db.all("SELECT id,comparison_id,visibility,created_at,revoked,config IS NOT NULL AS template FROM shares ORDER BY created_at DESC")
        else:
            rows = db.all("SELECT id,comparison_id,visibility,created_at,revoked,config IS NOT NULL AS template FROM shares WHERE owner_id=? ORDER BY created_at DESC", (user["id"],))
        links = [{"kind": "link", "id": row["id"], "url": f"{settings.base_url}/s/{row['id']}", "comparison_id": row["comparison_id"], "visibility": row["visibility"], "created_at": row["created_at"], "revoked": bool(row["revoked"]), "selection_mode": "editable" if not server and row["template"] else "fixed"} for row in rows]
        jobs = db.all("SELECT * FROM export_jobs WHERE expires_at>?" + ("" if user["role"] == "admin" else " AND owner_id=?"), (int(time.time()),) if user["role"] == "admin" else (int(time.time()), user["id"]))
        return {"shares": sorted(links + [app.state.exports.payload(row) for row in jobs], key=lambda row: row["created_at"], reverse=True)}

    @app.patch("/api/shares/{share_id}")
    async def patch_share(share_id: str, body: SharePatch, session=Depends(require_session)):
        row = valid_share(share_id)
        user = session["user"]
        if row["owner_id"] != user["id"] and user["role"] != "admin":
            fail(403, "FORBIDDEN", "只能修改自己的分享")
        if not server and body.visibility != "personal":
            fail(422, "INVALID_VISIBILITY", "personal 模式分享不支持可见性档位")
        allowed = SHARE_VISIBILITIES.get(user["role"])
        if allowed is None:
            fail(403, "SHARE_ONLY", "此账号不能修改分享可见性")
        if body.visibility not in allowed:
            fail(422, "INVALID_VISIBILITY", "当前角色不能使用该可见性档位")
        db.execute("UPDATE shares SET visibility=? WHERE id=?", (body.visibility, share_id))
        return {"ok": True, "visibility": body.visibility}

    @app.get("/api/shares/{share_id}")
    async def get_share(share_id: str, session=Depends(media_session)):
        row = valid_share(share_id)
        if server:
            can_view_share(row, session["user"] if session else None)
        config = None
        page_views = {}
        default_page_view = None
        selection_error = None
        template = not server and row["config"] is not None
        editable = template and session["user"]["role"] in {"member", "admin"}
        if template:
            try:
                manifest, refreshed = await run_in_threadpool(refresh_template, settings, db, row, remote=remote)
            except FileError as exc:
                # 实验目录变动后仍能取回配置，让成员在选图页修正路径。
                manifest = json.loads(row["manifest"])
                refreshed = ComparisonTemplate.model_validate_json(row["config"])
                selection_error = {"code": exc.code, "message": str(exc)}
            state = template_state(refreshed).model_dump(mode="json")
            page_views = {key: view.model_dump(mode="json") for key, view in refreshed.page_views.items()}
            default_page_view = {"view": refreshed.state.view.model_dump() if refreshed.state.view else None,
                                 "panels": [panel.model_dump(mode="json") for panel in refreshed.state.panels]}
            if editable:
                config = refreshed.model_dump(mode="json")
        else:
            manifest = json.loads(row["manifest"])
            state = json.loads(row["state"])
        if server:
            for asset in manifest["assets"].values():
                reject_expired_guest_asset(asset["path"])
        # 分享页只按可见性放行；绝对路径/虚拟路径属于服务器信息，不下发。
        if not editable:
            manifest.pop("selection", None)
            for source in manifest["sources"]:
                source["path"] = ""
            for asset in manifest["assets"].values():
                asset["path"] = ""
        return {"id": row["id"], "comparison": manifest, "state": state,
                "created_at": row["created_at"], "owner_display_name": row["display_name"],
                "visibility": row["visibility"], "selection_mode": "editable" if template else "fixed",
                "editable": editable, "config": config, "page_views": page_views,
                "default_page_view": default_page_view, "selection_error": selection_error}

    @app.delete("/api/shares/{share_id}")
    async def revoke_share(share_id: str, session=Depends(require_area_session)):
        row = valid_share(share_id)
        if row["owner_id"] != session["user"]["id"] and session["user"]["role"] != "admin":
            fail(403, "FORBIDDEN", "只能撤销自己的分享")
        db.execute("UPDATE shares SET revoked=1 WHERE id=?", (share_id,))
        return {"ok": True}

    @app.api_route("/{path:path}", methods=["GET", "HEAD"])
    async def frontend(path: str):
        if path.startswith("api/"):
            fail(404, "NOT_FOUND", "接口不存在")
        root = settings.frontend_dir.resolve()
        if path in {"flow/", "downloads/"}:
            path += "index.html"
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            fail(404, "NOT_FOUND", "页面不存在")
        if candidate.is_file():
            return FileResponse(candidate, headers={"Cache-Control": "public, max-age=31536000, immutable" if path.startswith("assets/") else "no-cache"})
        if path and not path.startswith("s/"):
            fail(404, "NOT_FOUND", "页面不存在")
        index = root / "index.html"
        if not index.is_file():
            return JSONResponse({"message": "前端尚未构建，请先在 frontend 执行 npm run build"}, status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app

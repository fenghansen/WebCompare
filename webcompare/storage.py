"""【模块】存储区域与统一授权 — 虚拟路径解析、区域分类与角色权限闸门

功能说明：server 模式下所有文件访问（浏览、比图、上传、删除、重命名、
导出）都先经过这里。它把 API 与资产登记使用的虚拟路径（如
member/2026/a.png、mount/photos/a.png）映射到 data_root 与挂载点的真实
绝对路径，判定真实路径属于哪个区域，再按角色矩阵决定读或写是否放行。
解析一律用 Path.resolve(strict=True) 消除符号链接后重新分类，因此 member
区里指向 private 区的软链接、挂载点里指回状态目录的软链接都会被拒绝；
写操作先解析最近存在的祖先目录，所以“尚不存在的新文件”也受同一套边界
保护。personal 模式不启用区域虚拟路径，仅沿用绝对路径浏览语义。

## 流程
所属流程：HTTP API 与安全边界（webcompare/storage.py）

1. **虚拟路径规范化**：拒绝空段、点段、隐藏段与重复分隔符 → 见函数 [Storage._split]、[Storage.area_of]
2. **区域分类**：真实路径归属区域判定，不在任何区域即 external → 见函数 [Storage.classify]
3. **区域核验**：虚拟路径声明与真实路径归属的作用域必须一致，防符号链接逃逸 → 见函数 [Storage.resolve]、[Storage._resolve_for_write]
4. **角色授权**：读写矩阵、挂载根保护与只读挂载限制 → 见函数 [Storage.authorize]
5. **可见根清单**：按角色给出可浏览的区域与挂载入口 → 见函数 [Storage.browse_roots]
"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from threading import RLock
import re

from .config import Settings
from .db import Database
from .files import FileError, resolve_path

AREAS = ("public", "private", "member", "guest", "mount")
SCOPED_AREAS = ("private", "guest", "mount")  # 第二段限定作用域：用户 UUID、工作区、挂载 ID
PUBLIC_AREAS = ("public", "member")
MAX_VIRTUAL = 4096
WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{number}" for prefix in ("COM", "LPT") for number in (*range(1, 10), "¹", "²", "³")
}


def _relative(path: Path, base: Path) -> str | None:
    """功能说明：返回 path 相对 base 的 POSIX 相对路径，base 自身返回空串。

    调用时机：[Storage.classify] 逐个区域判定真实路径归属时调用。

    关键步骤：
    1. 用 relative_to 判断包含关系，不在其中返回 None；
    2. 结果是 “.” 时归一为空串，否则转成 POSIX 形式。

    输出/副作用：返回字符串或 None；纯计算，不访问文件系统。
    """
    try:
        rel = path.relative_to(base)
    except ValueError:
        return None
    return "" if rel == Path(".") else rel.as_posix()


class Storage:
    """server 模式的文件边界：虚拟路径映射、区域分类与角色授权矩阵。

    功能说明：构造时只绑定配置与数据库，不做校验；目录创建与挂载边界由
    [Settings.prepare] 负责。所有对外方法在越界、越权、写法非法时统一抛
    [FileError]（复用 files.py 的错误类型），由 API 层转成对应的 4xx 响应。
    """

    def __init__(self, settings: Settings, db: Database):
        """功能说明：绑定配置与数据库，得到存储边界对象。

        调用时机：应用装配层与测试夹具在 [Settings.prepare] 与 [Database]
        都就绪之后构造一次。

        关键步骤：
        1. 记住 settings（区域布局、挂载点、部署模式）与 db（列用户私有区
           与访客工作区用）。

        输出/副作用：无；不访问文件系统。
        """
        self.settings, self.db = settings, db
        # ponytail: 单进程写入与清理串行；多进程部署需文件锁。
        self.mutation_lock = RLock()

    def ensure_private(self, user_id: str, username: str) -> None:
        """成员首次登录创建独立目录；虚拟路径继续绑定 UUID，旧链接不变。"""
        with self.mutation_lock, self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT name FROM private_directories WHERE user_id=?", (user_id,)).fetchone()
            # 任意合法登录名均可用。含路径字符或保留 UUID 形状的登录名按用户 ID 存储。
            safe_name = re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,59}", username) and not re.fullmatch(r"[0-9a-fA-F-]{36}", username)
            name = row["name"] if row else (username if safe_name else user_id)
            if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,59}", name):
                raise FileError("私有目录名称无效", "PRIVATE_NAME_INVALID", 403)
            conflict = conn.execute("SELECT user_id FROM private_directories WHERE name=?", (name,)).fetchone()
            if conflict and conflict["user_id"] != user_id:
                raise FileError("该私有目录名已绑定其他账号，请联系管理员", "PRIVATE_NAME_CONFLICT", 409)
            target = self.settings.data_root / "private" / name
            legacy = self.settings.data_root / "private" / user_id
            if target.is_symlink() or legacy.is_symlink():
                raise FileError("私有目录不能是符号链接", "PATH_ESCAPE", 403)
            if not row and target != legacy and target.exists():
                raise FileError("私有目录已存在且尚未绑定，拒绝自动认领", "PRIVATE_NAME_CONFLICT", 409)
            if not row and target != legacy and legacy.exists():
                legacy.rename(target)
            target.mkdir(exist_ok=True, mode=self.settings.content_dir_mode)
            if os.name == "posix":
                target.chmod(self.settings.content_dir_mode)
            if not row:
                conn.execute("INSERT INTO private_directories(user_id,name) VALUES(?,?)", (user_id, name))

    def _member_writable(self, user_id: str, virtual: str, *, mkdir: bool = False) -> bool:
        """成员区全员可读；创建者才能改写目录及内容，未登记的旧目录归管理员。"""
        segments = virtual.split("/")
        if len(segments) == 1:
            return mkdir
        owned_parent = False
        for index in range(2, len(segments) + 1):
            candidate = "/".join(segments[:index])
            row = self.db.one("SELECT owner_id FROM stored_directories WHERE path=? UNION ALL SELECT owner_id FROM stored_files WHERE path=? LIMIT 1", (candidate, candidate))
            if row:
                if row["owner_id"] != user_id:
                    return False
                owned_parent = True
            elif self._physical(segments[:index]).exists():
                return False
            elif index == 2 and not owned_parent and not mkdir:
                return False
        return owned_parent or mkdir

    def writable(self, user: dict, path: str, *, mkdir: bool = False) -> bool:
        try:
            self.authorize(user, path, "mkdir" if mkdir else "write")
            return True
        except FileError:
            return False

    @staticmethod
    def _absolute(raw: str) -> bool:
        """功能说明：判断输入是否按服务器绝对路径处理（以 /、~ 或 Windows 绝对路径开头）。

        调用时机：[Storage.area_of]、[Storage.authorize]、[Storage._verified]
        分流前首先调用。

        关键步骤：
        1. POSIX 路径看 / 或 ~；Windows 额外识别盘符绝对路径与 UNC 路径。

        输出/副作用：返回布尔；纯函数。
        """
        return raw.startswith(("/", "~")) or os.name == "nt" and PureWindowsPath(raw).is_absolute()

    @staticmethod
    def _windows_unsafe_segment(segment: str) -> bool:
        """判断路径段是否会被 Windows 解释成 ADS、设备名或非法文件名。"""
        if not segment or segment[-1] in {".", " "} or any(ord(char) < 32 or char in '<>:"|?*' for char in segment):
            return True
        basename = segment.split(".", 1)[0].rstrip(" .").upper()
        return basename in WINDOWS_RESERVED_NAMES

    def _split(self, virtual: str) -> list[str]:
        """功能说明：把虚拟路径切成路径段，任何可疑写法一律拒绝。

        调用时机：[Storage.area_of]、[Storage.authorize]、[Storage._verified]
        处理非绝对路径时首先调用。

        关键步骤：
        1. 拒绝空值、超长、含 NUL 或反斜杠的输入；
        2. 按 / 切段，拒绝空段（涵盖前导 “./”、重复分隔符与结尾斜杠）；
        3. 拒绝点段（. 与 ..）与隐藏段（以 . 开头，涵盖保留目录 .state/.staging）。

        输出/副作用：返回路径段列表；非法抛 FileError(422)，不访问文件系统。
        """
        if not virtual or len(virtual) > MAX_VIRTUAL or "\x00" in virtual or "\\" in virtual:
            raise FileError("虚拟路径非法", "INVALID_PATH", 422)
        segments = virtual.split("/")
        for segment in segments:
            if not segment or segment in {".", ".."} or segment.startswith("."):
                raise FileError("虚拟路径含非法路径段（空段、点段或隐藏目录）", "INVALID_PATH", 422)
            if os.name == "nt" and self._windows_unsafe_segment(segment):
                raise FileError("虚拟路径含 Windows 不支持的文件名", "INVALID_PATH", 422)
        if len(segments) >= 2 and segments[0] == "private":
            row = None if self.db.one("SELECT id FROM users WHERE id=?", (segments[1],)) else self.db.one("SELECT user_id FROM private_directories WHERE name=?", (segments[1],))
            if row:
                segments[1] = row["user_id"]
        return segments

    def display_path(self, virtual: str) -> str:
        if virtual.startswith("private/"):
            head, user_id, *tail = virtual.split("/")
            row = self.db.one("SELECT name FROM private_directories WHERE user_id=?", (user_id,))
            if row:
                return "/".join((head, row["name"], *tail))
        return virtual

    @staticmethod
    def _declared(segments: list[str]) -> tuple[str, str]:
        """功能说明：由路径段得出声明的区域与区域内相对路径（纯语法判断）。

        调用时机：[Storage.area_of]、[Storage.authorize]、[Storage._verified]
        在切段之后调用。

        关键步骤：
        1. 首段必须是 public/private/member/guest/mount 之一；
        2. private、guest、mount 必须带第二段限定作用域（用户 UUID、工作区、
           挂载 ID），否则无法确定归属；
        3. 其余段拼成区域内相对路径。

        输出/副作用：返回 (area, rel)；区域未知或缺作用域抛 FileError(422)。
        """
        head = segments[0]
        if head not in AREAS:
            raise FileError("虚拟路径必须以 public/private/member/guest/mount 开头", "INVALID_PATH", 422)
        rel = "/".join(segments[1:])
        if head in SCOPED_AREAS and not rel:
            raise FileError("该区域必须写成 <区域>/<标识>/… 形式", "INVALID_PATH", 422)
        return head, rel

    @staticmethod
    def _scope(area: str, rel: str | None) -> tuple[str, str | None]:
        """功能说明：把 (区域, 区域内相对路径) 归约为权限作用域，用于判断是否越区。

        调用时机：[Storage._verified] 比对虚拟路径声明与真实路径归属时调用。

        关键步骤：
        1. private/guest/mount 的作用域取相对路径第一段（用户 UUID、工作区、
           挂载 ID），同一区域内的不同用户/工作区因此互不越界；
        2. public/member/external 只有区域本身一层。

        输出/副作用：返回 (区域, 作用域标识或 None)；纯函数。
        """
        if rel and area in SCOPED_AREAS:
            return area, rel.split("/", 1)[0]
        return area, None

    @staticmethod
    def _visible(rel: str) -> bool:
        """功能说明：判断区域内相对路径是否可见：至少一层且不含隐藏段。

        调用时机：[Storage.classify] 判定 private/guest 区域时调用。

        关键步骤：
        1. 空串（区域容器目录本身，如 data_root/private）判为不可见；
        2. 任一段以 . 开头（.state/.staging 等）判为不可见。

        输出/副作用：返回布尔；纯函数。
        """
        return bool(rel) and not any(segment.startswith(".") for segment in rel.split("/"))

    def area_of(self, virtual: str) -> tuple[str, str | None]:
        """功能说明：解析虚拟路径声明的区域与区域内相对路径，不触碰文件系统。

        调用时机：配额登记校验区域（[Quota.commit]、[Quota.record_existing]）
        与需要按路径判断归属的调用方。

        关键步骤：
        1. 绝对路径（/ 或 ~ 开头）判为 external，没有区域内相对路径；
        2. 其余走 [Storage._split] 与 [Storage._declared] 得到 (区域, 相对路径)。

        输出/副作用：返回 (area, rel)，external 返回 ("external", None)；
        写法非法抛 FileError(422)。
        """
        if self._absolute(virtual):
            return "external", None
        return self._declared(self._split(virtual))

    def classify(self, path: Path) -> tuple[str, str | None]:
        """功能说明：判定真实路径落在哪个区域，并给出区域内相对路径。

        调用时机：核验阶段（[Storage.resolve]、[Storage._verified]）与任何需要
        知道真实路径归属的调用方。

        关键步骤：
        1. 解析路径（允许尾段尚不存在）以消除符号链接；
        2. 依次比对 data_root 下的 public、member，再比对 private/<用户> 与
           guest/<工作区>（隐藏段所在路径不算任何区域）；
        3. 再比对每个挂载点物理目录，命中时区域内相对路径以挂载 ID 开头；
        4. 全不命中返回 ("external", None)：.state/.staging 与 data_root 之外的
           路径都属于这一类。

        输出/副作用：返回 (area, rel)；纯计算，不写盘，也不校验角色权限。
        """
        real = Path(path).resolve()
        root = self.settings.data_root
        for area in PUBLIC_AREAS:
            rel = _relative(real, root / area)
            if rel is not None:
                return area, rel
        for area in ("private", "guest"):
            rel = _relative(real, root / area)
            if rel is not None and self._visible(rel):
                if area == "private":
                    name, slash, tail = rel.partition("/")
                    row = self.db.one("SELECT user_id FROM private_directories WHERE name=?", (name,))
                    if row:
                        rel = row["user_id"] + (slash + tail if slash else "")
                return area, rel
        for mount in self.settings.mounts.values():
            rel = _relative(real, mount.path)
            if rel is not None:
                return "mount", f"{mount.id}/{rel}".rstrip("/")
        return "external", None

    def virtual_for(self, path: Path) -> str:
        """功能说明：把真实路径换算成入库用的虚拟路径，让资产 ID 不绑死数据根绝对位置。

        调用时机：[FileCatalog.create_comparison] 为收集到的真实图片登记资产时
        对每张图调用；迁移数据根后虚拟路径不变，资产与短链随之存活。

        关键步骤：
        1. 先 [Storage.classify] 得到 (area, rel)；
        2. public/member/private/guest 拼为 ``<area>/<rel>``，挂载拼为
           ``mount/<rel>``（classify 返回的挂载 rel 已含挂载 ID 段）；
        3. external 直接返回绝对路径字符串（admin 外部浏览资产仍按绝对路径登记）。

        输出/副作用：返回虚拟路径或绝对路径字符串；纯计算。
        """
        area, rel = self.classify(path)
        if area == "external" or rel is None:
            return str(Path(path).resolve())
        return f"{area}/{rel}" if rel else area

    def resolve(self, virtual: str) -> Path:
        """功能说明：把虚拟路径解析成存在且区域正确的真实绝对路径。

        调用时机：浏览、收集、读图、导出等任何要落到文件系统的操作，在
        [Storage.authorize] 放行之后调用（渲染前后各复核一次）。

        关键步骤：
        1. 绝对路径交给 [resolve_path] 规范化并要求存在；
        2. 虚拟路径经 [Storage._split]、[Storage._declared] 取声明区域，
           再解析到真实路径（strict，消除符号链接）；
        3. 重新 [Storage.classify] 真实路径，作用域与声明不一致即拒绝。

        输出/副作用：返回真实绝对路径；不存在/越界 404、越区 403、写法非法 422。
        """
        return self._verified(virtual, allow_missing=False)

    def authorize(self, user: dict, virtual_or_abs: str, op: str) -> str:
        """功能说明：统一授权闸门：判断角色能否以 op 访问该路径，返回规范化虚拟路径。

        调用时机：server 模式所有文件入口（roots、browse、comparisons、上传、
        删除、重命名、媒体端点、导出）在处理请求前调用；personal 模式沿用
        绝对路径浏览语义，由旧路径逻辑处理。

        关键步骤：
        1. 校验 op 为 read 或 write，否则拒绝；
        2. 绝对路径（external）按模式与角色判定，见 [Storage._authorize_absolute]；
        3. 虚拟路径切段取声明区域；对能访问挂载的角色（admin/member）校验挂载 ID
           是否还在配置里，失效挂载返回 404，其余角色不查挂载、直接按矩阵拒绝；
        4. 按角色矩阵判定权限：admin 全区域读写；member 读 public/member/自己
           的 private/所有挂载，写 member/自己的 private/rw 挂载；guest 读写自己
           工作区的 guest 区，可读 public；viewer 一律拒绝；
        5. 挂载根（mount/<ID> 本身）的写操作对任何角色都拒绝（不能删除或改名
           整个挂载）；挂载根之内的写按 ro/rw 判定；
        6. 读要求目标存在、写允许尾段尚不存在，两者都做符号链接逃逸复核；
           最后返回 (区域, 相对路径) 拼成的规范虚拟路径。

        输出/副作用：返回规范化虚拟路径供后续 [Storage.resolve]；越权 403、
        失效挂载 404、写法非法 422。
        """
        if op not in {"read", "write", "mkdir"}:
            raise FileError("操作类型必须是 read、write 或 mkdir", "INVALID_OP", 400)
        role = str((user or {}).get("role") or "")
        user_id = str((user or {}).get("id") or "")
        if self._absolute(virtual_or_abs):
            return self._authorize_absolute(role, virtual_or_abs, op)
        if self.settings.mode != "server":
            raise FileError("personal 模式不提供区域虚拟路径", "NOT_FOUND", 404)
        segments = self._split(virtual_or_abs)
        area, rel = self._declared(segments)
        scope_id = rel.split("/", 1)[0] if rel else ""
        mount = None
        if area == "mount" and role in {"admin", "member"}:
            mount = self.settings.mounts.get(scope_id)
            if mount is None:
                raise FileError("挂载点不存在或已移除", "NOT_FOUND", 404)
        readable = writable = False
        if role == "admin":
            readable = writable = True
        elif role == "member":
            own_private = bool(user_id) and area == "private" and scope_id == user_id
            readable = area in PUBLIC_AREAS or own_private or area == "mount"
            writable = area == "member" or own_private or (mount is not None and mount.access == "rw")
        elif role == "guest":
            own_guest = bool(user_id) and area == "guest" and scope_id == user_id
            readable = area == "public" or own_guest
            writable = own_guest
        if op != "read":
            if area == "mount" and rel == scope_id:
                raise FileError("挂载根不允许改写、移动或删除", "MOUNT_ROOT", 403)
            if not writable:
                raise FileError("无权写入该区域", "PATH_FORBIDDEN", 403)
        elif not readable:
            raise FileError("无权访问该区域", "PATH_FORBIDDEN", 403)
        physical = self._verified(virtual_or_abs, allow_missing=op != "read")
        canonical = self.virtual_for(physical)
        if role == "member" and area == "member" and op != "read" and not self._member_writable(user_id, canonical, mkdir=op == "mkdir"):
            raise FileError("成员区只允许创建者改写自己的文件和文件夹", "PATH_FORBIDDEN", 403)
        return canonical

    def browse_roots(self, user: dict) -> list[dict]:
        """功能说明：给出该用户可见的区域与挂载根清单，供前端来源选择器渲染。

        调用时机：GET /api/roots 处理请求时调用（server 模式）。

        关键步骤：
        1. personal 模式沿用配置的快捷目录列表，viewer 返回空；
        2. server 模式按角色组装：admin 得到 public、member、全部用户的私有区、
           未清理的访客工作区与全部挂载；member 得到 public、member、自己的
           私有区与全部挂载（rw 才可写）；guest 得到 public 与自己的工作区；
        3. 每项输出 id、中文 label、虚拟 path 与 writable 标记。

        输出/副作用：返回条目列表；只读（admin 分支查库取显示名）。
        """
        role = str((user or {}).get("role") or "")
        user_id = str((user or {}).get("id") or "")
        if self.settings.mode != "server":
            if role not in {"admin", "member"}:
                return []
            return [{"id": key, "label": key, "path": str(path), "writable": False} for key, path in self.settings.roots.items()]
        if role == "admin":
            roots = [
                {"id": "public", "label": "公共区", "path": "public", "writable": True},
                {"id": "member", "label": "成员区", "path": "member", "writable": True},
            ]
            for row in self.db.all("SELECT id,display_name FROM users WHERE active=1 AND role IN ('admin','member') ORDER BY created_at"):
                virtual = self.display_path(f"private/{row['id']}")
                directory = self.settings.data_root / virtual
                if directory.is_symlink() or not directory.is_dir():
                    continue
                roots.append({"id": f"private:{row['id']}", "label": f"私有区 · {row['display_name']}", "path": virtual, "writable": True})
            for row in self.db.all("SELECT g.user_id AS user_id,u.display_name AS display_name FROM guest_workspaces g LEFT JOIN users u ON u.id=g.user_id WHERE g.purged=0 ORDER BY g.created_at"):
                roots.append({"id": f"guest:{row['user_id']}", "label": f"访客区 · {row['display_name'] or row['user_id'][:8]}", "path": f"guest/{row['user_id']}", "writable": True})
        elif role == "member":
            roots = [
                {"id": "public", "label": "公共区", "path": "public", "writable": False},
                {"id": "member", "label": "成员区", "path": "member", "writable": True},
            ]
            if user_id:
                named = self.db.one("SELECT name FROM private_directories WHERE user_id=?", (user_id,))
                roots.append({"id": "private", "label": f"我的私有区 · {named['name']}" if named else "我的私有区", "path": self.display_path(f"private/{user_id}"), "writable": True})
        elif role == "guest":
            roots = [{"id": "public", "label": "公共区", "path": "public", "writable": False}]
            if user_id:
                roots.append({"id": "guest", "label": "我的临时工作区", "path": f"guest/{user_id}", "writable": True})
        else:
            return []
        if role in {"admin", "member"}:
            for mount in self.settings.mounts.values():
                writable = role == "admin" or mount.access == "rw"
                roots.append({"id": f"mount:{mount.id}", "label": f"挂载 · {mount.id}", "path": f"mount/{mount.id}", "writable": writable})
        return roots

    def _authorize_absolute(self, role: str, raw: str, op: str) -> str:
        """功能说明：服务器绝对路径（external）的授权：按模式与角色放行或拒绝。

        调用时机：[Storage.authorize] 遇到以 / 或 ~ 开头的输入时调用。

        关键步骤：
        1. server 模式仅 admin 可用，读写都受服务账号的 OS 权限约束（不用 root）；
        2. personal 模式沿用改造前语义：member/admin 可读、viewer 拒绝，且没有
           写入口（上传只在 server 模式提供）；
        3. 展开 ~ 后返回绝对路径字符串。

        输出/副作用：返回绝对路径字符串；越权抛 FileError(403)，路径无法展开 422。
        """
        if self.settings.mode == "server":
            if role != "admin":
                raise FileError("server 模式只有管理员可以浏览服务器绝对路径", "PATH_FORBIDDEN", 403)
        elif role not in {"admin", "member"}:
            raise FileError("只有成员账号可以浏览服务器目录", "PATH_FORBIDDEN", 403)
        elif op == "write":
            raise FileError("personal 模式不提供写入接口", "PATH_FORBIDDEN", 403)
        try:
            return str(Path(raw).expanduser())
        except RuntimeError as exc:
            raise FileError("路径无法展开", "INVALID_PATH", 422) from exc

    def _verified(self, virtual: str, allow_missing: bool) -> Path:
        """功能说明：虚拟路径的物理核验：定位真实路径、消除符号链接并复核作用域。

        调用时机：[Storage.resolve] 与 [Storage.authorize] 都要落到文件系统时调用。

        关键步骤：
        1. 绝对路径交给 [resolve_path]，存在性由它保证；
        2. 其余切段取声明区域，定位物理路径：allow_missing 时解析最近存在的
           祖先目录，否则要求路径存在（strict 解析）；
        3. 用 [Storage.classify] 给真实路径分类，作用域（区域 + 用户/工作区/
           挂载标识）与声明不一致即拒绝：member 区软链接指向 private 区、
           挂载点软链接指回 state 目录都会被挡下。

        输出/副作用：返回核验通过的真实路径；越界/越区 403、不存在 404、写法非法 422。
        """
        if self._absolute(virtual):
            return resolve_path(virtual)
        segments = self._split(virtual)
        area, rel = self._declared(segments)
        if allow_missing:
            physical = self._resolve_for_write(segments)
        else:
            try:
                physical = self._physical(segments).resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise FileError("文件或目录不存在或无法访问", "NOT_FOUND", 404) from exc
        if self._scope(*self.classify(physical)) != self._scope(area, rel):
            raise FileError("路径指向其他区域（疑似符号链接逃逸），已拒绝", "PATH_ESCAPE", 403)
        return physical

    def _physical(self, segments: list[str]) -> Path:
        """功能说明：把路径段拼成物理路径，mount 段替换成配置里的挂载目录。

        调用时机：[Storage._verified] 与 [Storage._resolve_for_write] 在核验前定位目标。

        关键步骤：
        1. public/private/member/guest 直接拼到 data_root 之下；
        2. mount/<ID>/… 换成该挂载的物理目录，ID 不在配置里抛 FileError(404)
           （失效挂载上的旧资产因此返回 404）。

        输出/副作用：返回尚未解析符号链接的物理路径；纯计算。
        """
        if segments[0] == "private" and len(segments) >= 2:
            row = self.db.one("SELECT name FROM private_directories WHERE user_id=?", (segments[1],))
            if row:
                return self.settings.data_root / "private" / row["name"] / Path(*segments[2:])
        if segments[0] != "mount":
            return self.settings.data_root.joinpath(*segments)
        mount = self.settings.mounts.get(segments[1])
        if mount is None:
            raise FileError("挂载点不存在或已移除", "NOT_FOUND", 404)
        return mount.path.joinpath(*segments[2:])

    def _resolve_for_write(self, segments: list[str]) -> Path:
        """功能说明：为写入解析物理路径：定位最近存在的祖先，尾段允许尚不存在。

        调用时机：[Storage._verified] 在 op 为 write、目标文件可能还没落盘时调用。

        关键步骤：
        1. 从完整路径向上一层一层找第一个“存在或本身是符号链接”的候选；
        2. 候选按 strict 解析（悬空链接直接 404），再把不存在的尾段拼回；
        3. 返回路径的尾段确定不存在，因此不可能藏链接，[Storage.classify] 只
           解析已存在的部分即可得出区域，安全性与读路径一致。

        输出/副作用：返回写目标真实路径；链接失效或数据根不可用抛 FileError(404)。
        """
        for index in range(len(segments), 0, -1):
            candidate = self._physical(segments[:index])
            if not candidate.exists() and not candidate.is_symlink():
                continue
            try:
                real = candidate.resolve(strict=True)
            except (OSError, RuntimeError) as exc:
                raise FileError("路径上的链接已失效或无法访问", "NOT_FOUND", 404) from exc
            return real.joinpath(*segments[index:])
        raise FileError("数据根不可用", "NOT_FOUND", 404)

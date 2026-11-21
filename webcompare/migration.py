"""【模块】整机迁移 — 图片复制校验与资产记录的原子重绑

功能说明：为源端保存路径下的当前文件导出 SHA-256 校验清单，在目标端
核对复制结果，再在同一事务里同步资产与比较清单中的版本和大小。
比较与分享按路径读取，复制后路径正确即可继续使用。资产登记路径有两种形态：personal
模式与 v2 遗留库是绝对路径（目标机的图片绝对路径必须与源端一致）；
server 模式是虚拟路径（不含数据根），经 [Storage] 解析到当前 data_root，
停机复制 data_root（含 .state）并更新配置后，资源 ID 与短链保持不变。

## 流程
所属流程：整机迁移（webcompare/migration.py）

1. **导出校验清单**：为保存路径下的当前文件记录 SHA-256 → 见函数 [export_manifest]
2. **校验并重绑**：目标端校验字节后原子更新版本指纹 → 见函数 [rebase_assets]
3. **资产路径解析**：虚拟路径经存储边界、绝对路径维持原解析 → 见函数 [_asset_path]、[Storage.resolve]
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

from .config import Settings
from .db import Database
from .files import FileError, resolve_path, revision
from .storage import Storage


def _asset_path(storage: Storage, raw: str) -> Path:
    """功能说明：把资产登记路径解析成真实路径：虚拟路径经存储边界，绝对路径维持原语义。

    调用时机：[export_manifest] 与 [rebase_assets] 逐条处理 assets 表时调用。

    关键步骤：
    1. 以 / 开头的绝对路径仍走 [resolve_path]（personal 与 v2 遗留库的形态）；
    2. 其余视为 server 模式的虚拟路径，走 [Storage.resolve]（含区域与符号
       链接复核，失效挂载上的旧资产因此按缺失跳过）。

    输出/副作用：返回存在的真实路径；失败抛 [FileError]，由调用方计数跳过。
    """
    return resolve_path(raw) if raw.startswith("/") else storage.resolve(raw)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino, stat.st_dev


def export_manifest(settings: Settings, output: Path) -> dict[str, int]:
    """功能说明：迁移源端——为已登记路径下的当前文件导出 SHA-256 校验清单。

    调用时机：CLI export-migration-manifest；要求先停止服务，避免导出期间
    数据库变化。

    关键步骤：
    1. 遍历 assets 表，逐条经 [_asset_path] 解析路径（虚拟路径走 [Storage]）
       读取当前文件；
    2. 缺失的资产跳过计数，其余计算 SHA-256；
    3. 计算期间源文件再变化则整个导出失败；
    4. 以 O_EXCL + 0600 创建清单文件，写失败不留半成品。

    输出/副作用：写出一份迁移清单 JSON；返回 {assets, stale_or_missing}
    统计；源数据只读。
    """
    settings.prepare()
    db = Database(settings.data_dir / "webcompare.sqlite3")
    storage = Storage(settings, db)
    rows = db.all("SELECT id, path, revision, bytes FROM assets ORDER BY id")
    assets = []
    skipped = 0
    for row in rows:
        try:
            path = _asset_path(storage, row["path"])
            before = _identity(path)
            if not path.is_file():
                skipped += 1
                continue
            digest = _sha256(path)
            if _identity(path) != before:
                raise RuntimeError(f"导出期间源图片发生变化：{row['path']}")
        except (FileError, FileNotFoundError):
            skipped += 1
            continue
        assets.append({**row, "source_bytes": before[0], "sha256": digest})
    payload = {"format": 1, "asset_count": len(rows), "assets": assets}
    output = output.expanduser().resolve()
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(payload, target, ensure_ascii=False, separators=(",", ":"))
            target.write("\n")
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    return {"assets": len(assets), "stale_or_missing": skipped}


def rebase_assets(settings: Settings, manifest_path: Path) -> dict[str, int]:
    """功能说明：迁移目标端——逐字节校验已复制的图片，然后原子重绑资产版本指纹。

    调用时机：CLI rebase-assets；数据库与图片都已复制到新机器、服务停止时。

    关键步骤：
    1. 校验清单格式，且清单资产总数必须与当前数据库一致；
    2. 逐条比对资产的根/路径/版本/大小，经 [_asset_path] 解析（server 模式
       虚拟路径落到当前 data_root）后对目标文件算 SHA-256 与清单比对，校验
       期间文件被改即失败；
    3. 用 BEGIN IMMEDIATE 事务复核数据库未变，更新 assets 版本指纹并
       同步改写每个比较 manifest 里对应资产的版本。

    输出/副作用：事务内更新资产与比较表；返回 {assets, comparisons,
    stale_or_missing} 统计；任何校验失败都不落任何改动。
    """
    settings.prepare()
    db = Database(settings.data_dir / "webcompare.sqlite3")
    storage = Storage(settings, db)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("format") != 1 or not isinstance(payload.get("assets"), list):
        raise ValueError("迁移清单格式不受支持")
    rows = {row["id"]: row for row in db.all("SELECT id, path, revision, bytes FROM assets")}
    if payload.get("asset_count") != len(rows):
        raise ValueError("数据库资产数量与迁移清单不符；请复制导出后的数据库")
    updates: dict[str, tuple[str, int]] = {}
    for item in payload["assets"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or item["id"] in updates:
            raise ValueError("迁移清单中的资产 ID 无效或重复")
        row = rows.get(item["id"])
        if row is None or any(item.get(key) != row[key] for key in ("path", "revision", "bytes")):
            raise ValueError(f"数据库资产与迁移清单不符：{item['id']}")
        path = _asset_path(storage, row["path"])
        before = _identity(path)
        if not path.is_file() or before[0] != item.get("source_bytes", row["bytes"]) or _sha256(path) != item.get("sha256"):
            raise ValueError(f"目标图片校验失败：{row['path']}")
        if _identity(path) != before:
            raise RuntimeError(f"校验期间目标图片发生变化：{row['path']}")
        updates[item["id"]] = revision(path), before[0]
    changed_comparisons = 0
    with sqlite3.connect(db.path, timeout=10) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = {row[0]: row[1] for row in conn.execute("SELECT id, revision FROM assets")}
        if len(current) != len(rows) or any(current.get(item["id"]) != item["revision"] for item in payload["assets"]):
            raise RuntimeError("数据库在校验后发生变化；已取消迁移")
        for asset_id, (new_revision, size) in updates.items():
            conn.execute("UPDATE assets SET revision=?,bytes=? WHERE id=?", (new_revision, size, asset_id))
        for comparison_id, raw in conn.execute("SELECT id, manifest FROM comparisons"):
            manifest = json.loads(raw)
            touched = False
            for asset_id, asset in manifest.get("assets", {}).items():
                if asset_id in updates:
                    asset["revision"], asset["bytes"] = updates[asset_id]
                    touched = True
            if touched:
                conn.execute("UPDATE comparisons SET manifest=? WHERE id=?", (json.dumps(manifest, ensure_ascii=False), comparison_id))
                changed_comparisons += 1
    return {"assets": len(updates), "comparisons": changed_comparisons, "stale_or_missing": len(rows) - len(updates)}

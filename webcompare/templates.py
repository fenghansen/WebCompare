"""personal 配置分享：保存路径规则，打开时重新选取当前实验结果。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .files import FileCatalog, FileError
from .models import ComparisonTemplate, ViewState


def selection_from_manifest(manifest: dict) -> dict:
    """复用原规则；旧比较按现有分组中的文件恢复精确选择。"""
    if "selection" in manifest:
        return manifest["selection"]
    selection = {"sources": manifest["sources"], "combine": manifest["combine"],
                 "recursive": manifest["recursive"], "media_kind": manifest.get("media_kind", "image")}
    if manifest.get("groups"):
        selection["sources"] = []
        selection["groups"] = []
        for group in manifest["groups"]:
            rules: dict[str, dict] = {}
            for asset_id in group["images"]:
                path = Path(manifest["assets"][asset_id]["path"])
                # 优先复用原来源目录，保留递归相对名字与有界的多路选择。
                source = next((Path(item["path"]) for item in manifest["sources"]
                               if path != Path(item["path"]) and path.is_relative_to(Path(item["path"]))), path.parent)
                rule = rules.setdefault(str(source), {"path": str(source), "pattern": "*", "names": []})
                name = path.relative_to(source).as_posix()
                if name not in rule["names"]:
                    rule["names"].append(name)
            selection["groups"].append({"label": group["label"], "rules": list(rules.values())})
    else:
        selection["image_order"] = [manifest["assets"][asset_id]["path"] for asset_id in manifest["images"]]
    return selection


def compact_state(state: ViewState) -> dict:
    """沿用固定分享的默认字段省略方式。"""
    result = state.model_dump(mode="json")
    defaults = ViewState().model_dump(mode="json")
    for key in ("video_frame", "video_offsets", "panels", "grouping", "natural_sort", "transposed", "raw", "brightness_ref"):
        if result[key] == defaults[key]:
            result.pop(key)
    return result


def validate_template(config: ComparisonTemplate, manifest: dict, *, clamp: bool = False) -> ComparisonTemplate:
    """确认布局和页码能展示选图；重新打开时允许图片数量缩短。"""
    from .app import page_images

    state = config.state.model_copy(deep=True)
    groups = manifest.get("groups", [])
    rows, columns = map(int, state.layout.split("x"))
    if state.grouping == "aligned" and len(groups) > rows * columns:
        raise FileError("布局格数需要容纳全部分组，请增大 layout 或使用 within", "INVALID_LAYOUT")
    _, pages = page_images(manifest, state.model_dump())
    if not clamp and (state.group_index >= pages or any(int(page) >= pages for page in config.page_views)):
        raise FileError("配置页码超出选图范围", "INVALID_GROUP")
    state.group_index = min(state.group_index, pages - 1)
    page_views = {key: view for key, view in config.page_views.items() if int(key) < pages}
    return config.model_copy(update={"state": state, "page_views": page_views})


def template_state(config: ComparisonTemplate) -> ViewState:
    """当前页覆盖公共参数；保存在配置里的公共参数仍可用于其他页。"""
    state = config.state.model_copy(deep=True)
    page_view = config.page_views.get(str(state.group_index))
    if page_view is not None:
        state.view, state.panels = page_view.view, page_view.panels
    return state


def save_template(settings, db, user: dict, config: ComparisonTemplate, manifest: dict) -> dict:
    """为 GUI 与 CLI 保存相同的配置短链；同账号同配置复用链接。"""
    if settings.mode != "personal" or user["role"] not in {"member", "admin"}:
        raise FileError("可编辑配置仅供 personal 模式的 member/admin 使用", "TEMPLATE_FORBIDDEN", 403)
    config = validate_template(config, manifest)
    payload = config.model_dump(mode="json")
    if payload["state"]["brightness_ref"] is None:
        payload["state"].pop("brightness_ref")
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    share_id = hashlib.sha256(("template\0" + user["id"] + "\0" + canonical).encode()).hexdigest()[:32]
    state = compact_state(template_state(config))
    with db.connect() as conn:
        # GUI 和多个 CLI 进程可能同时提交同一模板，共用首次写入的比较 ID。
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT comparison_id FROM shares WHERE id=?", (share_id,)).fetchone()
        if existing:
            manifest["id"] = existing["comparison_id"]
        else:
            conn.execute("INSERT OR IGNORE INTO comparisons VALUES(?,?,?,?)",
                         (manifest["id"], user["id"], json.dumps(manifest, ensure_ascii=False), manifest["created_at"]))
        conn.execute("INSERT INTO shares(id,owner_id,comparison_id,state,created_at,visibility,config,template_manifest) "
                     "VALUES(?,?,?,?,?,'personal',?,?) ON CONFLICT(id) DO UPDATE SET revoked=0,template_manifest=excluded.template_manifest",
                     (share_id, user["id"], manifest["id"], json.dumps(state), int(time.time()), canonical,
                      json.dumps(manifest, ensure_ascii=False)))
    return {"id": share_id, "url": f"{settings.base_url}/s/{share_id}", "comparison": manifest,
            "state": state, "selection_mode": "editable"}


def create_template(settings, db, user: dict, config: ComparisonTemplate, *, remote=None) -> dict:
    """本地 CLI 和 HTTP 共用的配置创建入口。"""
    if settings.mode != "personal" or user["role"] not in {"member", "admin"}:
        raise FileError("可编辑配置仅供 personal 模式的 member/admin 使用", "TEMPLATE_FORBIDDEN", 403)
    request = config.comparison
    manifest = FileCatalog(settings, db, remote=remote).create_comparison(
        user["id"], [source.model_dump() for source in request.sources], request.combine, request.recursive,
        None if request.groups is None else [group.model_dump() for group in request.groups], request.media_kind,
        persist=False, image_order=request.image_order)
    normalized = config.model_copy(update={"comparison": request.model_validate(manifest["selection"])})
    return save_template(settings, db, user, normalized, manifest)


def refresh_template(settings, db, row: dict, *, remote=None) -> tuple[dict, ComparisonTemplate]:
    """按保存的路径读取当前文件，更新媒体授权清单，不新增比较记录。"""
    config = ComparisonTemplate.model_validate_json(row["config"])
    request = config.comparison
    if remote is not None:
        # 配置分享只复用分享者的连接，不可借编辑规则切换到其他账号的凭据。
        paths = [source.path for source in request.sources] + request.image_order
        paths += [rule.path for group in request.groups or [] for rule in group.rules]
        catalog = FileCatalog(settings, db, remote=remote)
        for raw in paths:
            catalog.authorize(raw, row["owner_id"])
    manifest = FileCatalog(settings, db, remote=remote).create_comparison(
        row["owner_id"], [source.model_dump() for source in request.sources], request.combine, request.recursive,
        None if request.groups is None else [group.model_dump() for group in request.groups], request.media_kind,
        persist=False, image_order=request.image_order)
    manifest["id"] = row["comparison_id"]
    manifest["created_at"] = row["created_at"]
    config = validate_template(config, manifest, clamp=True)
    db.execute("UPDATE shares SET template_manifest=? WHERE id=? AND revoked=0",
               (json.dumps(manifest, ensure_ascii=False), row["id"]))
    return manifest, config

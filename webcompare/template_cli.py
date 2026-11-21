"""JSON 比较模板的离线命令：查看格式、生成示例和创建 personal 分享。"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from .config import Settings
from .models import ComparisonTemplate


def template_schema() -> dict:
    """返回 API 同样使用的模型 schema，不打开状态库。"""
    return ComparisonTemplate.model_json_schema()


def template_example() -> dict:
    """三路结果按文件位置对齐，每页查看不同的原图范围。"""
    return {
        "version": 1,
        "comparison": {
            "groups": [
                {"label": label, "rules": [{
                    "path": f"D:/experiments/denoise/{directory}",
                    "pattern": "*.png",
                    "names": ["scene001.png", "scene002.png"],
                }]}
                for label, directory in [("Input", "input"), ("Baseline", "baseline"), ("Ours", "ours")]
            ],
            "recursive": False,
            "media_kind": "image",
        },
        "state": {
            "group_index": 0,
            "layout": "1x3",
            "grouping": "aligned",
            "quality": "exact",
        },
        "page_views": {
            "0": {"view": {"x": 1792, "y": 1344, "width": 512, "height": 384}},
            "1": {"view": {"x": 896, "y": 640, "width": 256, "height": 256}},
        },
    }


def write_json(document: dict, output: Path | None = None) -> None:
    """打印或写入可直接作为下一条命令输入的 UTF-8 JSON。"""
    text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if output is None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        print(text, end="")
        return
    try:
        output.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"无法写入 JSON 文件：{exc}") from None


def _output_origin(value: str) -> str:
    origin = value.rstrip("/")
    try:
        parsed = urlsplit(origin)
        valid = parsed.scheme in {"http", "https"} and parsed.netloc and not (
            parsed.path or parsed.query or parsed.fragment or parsed.username
        )
        parsed.port  # 触发非法端口的校验。
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("--url 必须是完整的 HTTP(S) origin，不带子路径")
    return origin


def read_template(file: Path) -> ComparisonTemplate:
    """读取 GUI、CLI 和 API 共用的 UTF-8 JSON 比较模板。"""
    try:
        return ComparisonTemplate.model_validate_json(file.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"无法读取模板文件：{exc}") from None
    except ValidationError as exc:
        raise ValueError(f"模板 JSON 无效：\n{exc}") from None


def create_from_config(settings: Settings, config: ComparisonTemplate, username: str | None = None,
                       *, base_url: str | None = None) -> dict:
    """按当前 Personal 身份创建可编辑的比较，返回已保存的模板与清单。"""
    if settings.mode != "personal":
        raise ValueError("create-template 仅用于 personal 模式")
    if username is None and settings.login_required:
        raise ValueError("已开启登录认证；create-template 必须通过 --user 指定已有 member/admin 本地账号")
    origin = _output_origin(base_url) if base_url is not None else settings.base_url
    database_path = settings.data_dir.expanduser().resolve() / "webcompare.sqlite3"
    if username is not None and not database_path.is_file():
        raise ValueError("状态库不存在；请指定已有实例的 --data-dir，并先创建本地账号")

    from .auth import personal_user
    from .db import Database
    from .files import FileError
    from .templates import create_template

    settings.prepare()
    db = Database(database_path)
    if username is not None:
        user = db.one(
            "SELECT u.id,u.display_name,u.role,u.active,c.username "
            "FROM users u JOIN local_credentials c ON c.user_id=u.id "
            "WHERE c.username=? COLLATE NOCASE",
            (username,),
        )
        if not user or not user.pop("active"):
            raise ValueError("本地账号不存在或未启用")
        if user["role"] not in {"member", "admin"}:
            raise ValueError("创建模板需要 member 或 admin 账号")
    try:
        if username is None:
            user = personal_user(db)
        result = create_template(settings, db, user, config)
    except FileError as exc:
        raise ValueError(f"{exc.code}: {exc}") from None
    return {**result, "url": f"{origin}/s/{result['id']}"}


def create_from_file(settings: Settings, file: Path, username: str | None = None, *, base_url: str | None = None) -> dict:
    """离线创建模板：显式用户名沿用原校验，免登录 Personal 可直接使用稳定默认身份。"""
    try:
        return create_from_config(settings, read_template(file), username, base_url=base_url)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None

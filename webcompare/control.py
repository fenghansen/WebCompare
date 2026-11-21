"""外部程序创建和打开实验比较。桌面安装包、Python 命令和 Python API 共用此入口。"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import re
import sqlite3
import sys
import webbrowser

from .config import Settings
from .models import ComparisonTemplate
from .template_cli import _output_origin, create_from_config, read_template, template_example, template_schema, write_json


class WindowOpenError(RuntimeError):
    """模板已保存，但目标窗口未接受请求。result 保留可重试的 ID。"""

    def __init__(self, result: dict, cause: Exception):
        self.result = result
        super().__init__(f"比较已保存，可用 open {result['id']} 重试。打开窗口失败：{cause}")


def _profile(target: str, data_dir: str | Path | None, url: str | None, user: str | None) -> Settings:
    if target == "desktop":
        if url is not None or user is not None:
            raise ValueError("--url 和 --user 只用于 Web Personal")
        from .native_desktop import default_user_data_dir
        profile = Path(data_dir) if data_dir is not None else default_user_data_dir()
        return Settings(data_dir=profile.expanduser().resolve() / "backend", client_transport="local")
    if target != "web":
        raise ValueError("target 必须是 desktop 或 web")
    settings = Settings.from_env()
    if settings.mode != "personal":
        raise ValueError("模板控制接口仅用于 Personal")
    origin = _output_origin(url or settings.base_url or "http://127.0.0.1:7865")
    return replace(settings, data_dir=Path(data_dir or settings.data_dir).expanduser().resolve(), base_url=origin)


def _show(settings: Settings, target: str, share_id: str) -> None:
    if target == "desktop":
        from .native_desktop import open_desktop_share
        open_desktop_share(settings.data_dir.parent, share_id)
    elif not webbrowser.open(f"{settings.base_url}/s/{share_id}"):
        raise RuntimeError("当前环境没有可打开链接的浏览器")


def open_comparison(share_id: str, *, target: str = "desktop", data_dir: str | Path | None = None,
                    url: str | None = None) -> dict:
    """在指定工作台打开已保存的模板 ID。桌面使用当前用户的窗口，Web 使用浏览器。"""
    if not re.fullmatch(r"[0-9a-f]{32}", share_id):
        raise ValueError("需要 create-template 或 compare 返回的 32 位模板 ID")
    settings = _profile(target, data_dir, url, None)
    db = settings.data_dir / "webcompare.sqlite3"
    if not db.is_file():
        raise ValueError("此工作台尚未保存比较，请检查 --data-dir")
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT comparison_id FROM shares WHERE id=? AND revoked=0 AND config IS NOT NULL",
                           (share_id,)).fetchone()
    if row is None:
        raise ValueError("此工作台找不到该模板或分享已撤销")
    _show(settings, target, share_id)
    return {"id": share_id, "comparison_id": row[0], "target": target, "opened": True,
            "url": f"{settings.base_url}/s/{share_id}" if target == "web" else None}


def compare(*paths: str | Path, template: ComparisonTemplate | dict | str | Path | None = None,
            target: str = "desktop", data_dir: str | Path | None = None, open_view: bool = True,
            url: str | None = None, user: str | None = None) -> dict:
    """按来源顺序并排比较本机文件或目录，或提交包含分组、逐页视图等设置的模板。"""
    if template is not None and paths:
        raise ValueError("请选择路径列表或 JSON 模板中的一种")
    if template is None:
        if not paths:
            raise ValueError("请传入图片文件或目录，或使用 --template 提交模板")
        groups = []
        for item in paths:
            path = Path(item).expanduser().resolve(strict=True)
            groups.append({"label": path.name or str(path), "rules": [{"path": str(path), "pattern": "*"}]})
        template = {"comparison": {"groups": groups}, "state": {"layout": f"1x{len(groups)}", "quality": "exact"}}
    if isinstance(template, (str, Path)):
        config = read_template(Path(template))
    else:
        config = ComparisonTemplate.model_validate(template)
    settings = _profile(target, data_dir, url, user)
    if target == "desktop":
        settings.data_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    saved = create_from_config(settings, config, user)
    share_id = saved["id"]
    result = {"id": share_id, "comparison_id": saved["comparison"]["id"], "target": target,
              "opened": False, "image_count": len(saved["comparison"]["images"]),
              "url": saved["url"] if target == "web" else None}
    if open_view:
        try:
            _show(settings, target, share_id)
        except (RuntimeError, OSError) as exc:
            raise WindowOpenError(result, exc) from exc
        result["opened"] = True
    return result


def main(argv: list[str] | None = None) -> int:
    """供 wheel 的 console_scripts 和冻结程序调用；成功时 stdout 是 UTF-8 JSON。"""
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--target", choices=("desktop", "web"), help="目标工作台（默认 desktop）")
    common.add_argument("--data-dir", "--user-data-dir", type=Path, help="桌面用户数据目录，或 Web Personal 状态目录")
    common.add_argument("--url", help="Web Personal 站点地址，默认 http://127.0.0.1:7865")
    parser = argparse.ArgumentParser(prog="webcompare-ctl", description=__doc__, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("template-schema", "输出模板的 JSON Schema"), ("template-example", "输出可修改的 JSON 示例")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--output", type=Path, help="写入 UTF-8 文件")
    direct = commands.add_parser("compare", parents=[common], help="按文件或目录并排比较，或使用 JSON 模板")
    direct.add_argument("paths", nargs="*", help="一组图片或实验结果目录")
    direct.add_argument("--template", type=Path, help="使用完整比较模板")
    create = commands.add_parser("create-template", parents=[common], help="从完整 JSON 模板创建并打开比较")
    create.add_argument("template", type=Path)
    for command in (direct, create):
        command.add_argument("--no-open", action="store_true", help="只保存并输出 ID")
        command.add_argument("--user", help="启用账号认证的 Web Personal 需要指定已有用户名")
    opening = commands.add_parser("open", parents=[common], help="在工作台中重新打开已保存的模板")
    opening.add_argument("id")
    args = parser.parse_args(argv)
    try:
        if args.command in ("template-example", "template-schema"):
            write_json(template_example() if args.command == "template-example" else template_schema(), args.output)
            return 0
        options = {"target": getattr(args, "target", "desktop"), "data_dir": getattr(args, "data_dir", None),
                   "url": getattr(args, "url", None)}
        if args.command == "open":
            result = open_comparison(args.id, **options)
        else:
            result = compare(*getattr(args, "paths", []), template=args.template, open_view=not args.no_open,
                             user=args.user, **options)
        write_json(result)
    except WindowOpenError as exc:
        write_json(exc.result)
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

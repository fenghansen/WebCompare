"""【模块】命令行入口 — webcompare 命令的参数解析与子命令分发

功能说明：本地终端入口。解析子命令与 --data-dir，从环境
变量组装运行配置，然后分发到服务启动、账号管理、迁移或视频拆帧等具体
流程。账号密码只经终端交互或 stdin 读取，从不出现在命令行参数里。

## 流程
所属流程：命令行入口（webcompare/cli.py）

1. **参数解析与配置加载**：解析子命令、注入 --data-dir、从环境组装配置并校验 → 见函数 [main]、[Settings.from_env]、[Settings.prepare]
2. **服务启动**：传递可选 --require-login、配置按日日志，再以 uvicorn 单 worker 运行应用工厂 → 见函数 [main]、[create_app]、[configure_logging]
3. **账号管理**：创建/列出账号、重置密码并撤销会话 → 见函数 [create_user]
4. **迁移与拆包子命令**：导出迁移清单、校验重绑资产、视频离线拆帧、解包分享 zip → 见函数 [export_manifest]、[rebase_assets]、[import_video]、[import_zip]
5. **server 运维子命令**：重算站内磁盘预算、手动触发一轮访客清理 → 见函数 [Quota.recalc_site_budget]、[sweep]
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from .auth import create_user, hash_password
from .config import Settings
from .db import Database


def main():
    """功能说明：webcompare 命令的总入口，解析参数并执行一个子命令后退出。

    调用时机：由 pyproject 注册的 console script `webcompare` 触发，每次调用
    只完成一个子命令；服务子命令会常驻，其余子命令执行完即退出。

    关键步骤：
    1. 构建 argparse 子命令树：serve、create-user、list-users、reset-password、
       revoke-share、export-migration-manifest、rebase-assets、
       import-video、import-zip、recalc-budget、create-guest-purge，以及模板命令；
    2. 若带 --data-dir，写入 WEBCOMPARE_DATA_DIR 环境变量供配置读取；
    3. import-video 直接调用 [import_video] 离线拆帧并打印结果 JSON；
    4. 迁移子命令调用 [export_manifest] / [rebase_assets] 并打印摘要；
    5. recalc-budget / create-guest-purge 构造 [Storage] 后分别调用
       [Quota.recalc_site_budget] 与 [sweep]，打印预算字节数或清理统计；
    6. serve 先配置服务日志，再校验配置和前端入口，以 uvicorn 单 worker 启动
       [create_app]；账号等一次性命令不启用服务日志；
    7. 其余子命令打开 SQLite 后执行账号/分享管理并打印结果。

    输出/副作用：结果打印到 stdout；create-user --generate 的随机密码只在
    本终端显示一次；reset-password 同时撤销该用户全部会话。
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="webcompare", description="WebCompare 本机目录比图服务")
    parser.add_argument("--data-dir", type=Path, help="账号、比较和缓存目录")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="运行服务")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7865)
    serve.add_argument("--require-login", action="store_true", help="Personal 要求使用已有账号登录（server 始终要求认证）")
    serve.add_argument("--log-dir", type=Path, help="服务日志目录（默认 WEBCOMPARE_LOG_DIR 或状态目录/logs）")
    serve.add_argument("--open-browser", action="store_true", help="就绪后打开浏览器；已运行实例通过检查后直接打开")
    serve.add_argument("--client-transport", choices=["auto", "local", "remote"], help="浏览器连接方式：auto 自动判断，SSH 隧道可显式用 remote")
    create = sub.add_parser("create-user", help="创建本地账号")
    create.add_argument("username")
    create.add_argument("--role", choices=["admin", "member", "viewer"], default="member")
    create.add_argument("--display-name")
    create.add_argument("--generate", action="store_true", help="生成随机密码并仅在本终端显示一次")
    create.add_argument("--password-stdin", action="store_true")
    sub.add_parser("list-users", help="列出账号，不显示凭据")
    reset = sub.add_parser("reset-password", help="重置密码并撤销会话")
    reset.add_argument("username")
    reset.add_argument("--password-stdin", action="store_true")
    revoke = sub.add_parser("revoke-share", help="管理员从服务器撤销分享")
    revoke.add_argument("share_id")
    export = sub.add_parser("export-migration-manifest", help="为跨文件系统迁移导出图片校验清单；请先停止服务")
    export.add_argument("output", type=Path)
    rebase = sub.add_parser("rebase-assets", help="校验已复制图片并重绑定资产版本；请先停止服务")
    rebase.add_argument("manifest", type=Path)
    video = sub.add_parser("import-video", help="离线拆帧到新目录，供普通目录比较使用")
    video.add_argument("source", type=Path)
    video.add_argument("output", type=Path)
    video.add_argument("--start", type=float, default=0)
    video.add_argument("--duration", type=float)
    video.add_argument("--max-frames", type=int, default=100)
    unzip = sub.add_parser("import-zip", help="解包分享 zip 到新目录，供普通目录浏览使用")
    unzip.add_argument("archive", type=Path)
    unzip.add_argument("output", type=Path)
    sub.add_parser("recalc-budget", help="重算站内磁盘预算并打印字节数（server 模式；日常请求不重算）")
    sub.add_parser("create-guest-purge", help="立即执行一轮访客清理并打印统计 JSON（server 模式）")
    example = sub.add_parser("template-example", help="输出 personal 比较模板示例 JSON")
    example.add_argument("--output", type=Path, help="写入 UTF-8 JSON 文件；默认打印到 stdout")
    schema = sub.add_parser("template-schema", help="输出比较模板的 JSON Schema，无需启动服务")
    schema.add_argument("--output", type=Path, help="写入 UTF-8 JSON 文件；默认打印到 stdout")
    template = sub.add_parser("create-template", help="从 JSON 创建可返回选图的 personal 分享")
    template.add_argument("file", type=Path, help="比较模板 JSON 文件")
    template.add_argument("--user", help="已有且已启用的本地 member/admin 用户名；免登录 Personal 默认使用个人工作台身份")
    template.add_argument("--url", help="输出链接的 HTTP(S) origin，例如 http://localhost:7865")
    args = parser.parse_args()
    if args.command is None:
        # 无参数入口用于桌面启动；显式 serve 保留原有前台服务行为。
        args = parser.parse_args([*sys.argv[1:], "serve", "--open-browser"])
    if args.data_dir:
        os.environ["WEBCOMPARE_DATA_DIR"] = str(args.data_dir)
    if args.command in {"template-example", "template-schema"}:
        from .template_cli import template_example, template_schema, write_json
        document = template_example() if args.command == "template-example" else template_schema()
        write_json(document, args.output)
        return
    if args.command == "import-video":
        from .video import import_video
        result = import_video(args.source, args.output, start=args.start, duration=args.duration, max_frames=args.max_frames)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.command == "import-zip":
        from .sharepack import import_zip
        print(json.dumps(import_zip(args.archive, args.output), ensure_ascii=False))
        return
    if args.command in {"export-migration-manifest", "rebase-assets"}:
        from .migration import export_manifest, rebase_assets
        settings = Settings.from_env()
        result = export_manifest(settings, args.output) if args.command == "export-migration-manifest" else rebase_assets(settings, args.manifest)
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command in {"recalc-budget", "create-guest-purge"}:
        from .lifecycle import sweep
        from .quota import Quota
        from .storage import Storage
        settings = Settings.from_env()
        settings.prepare()
        db = Database(settings.data_dir / "webcompare.sqlite3")
        storage = Storage(settings, db)
        if args.command == "recalc-budget":
            print(json.dumps({"disk_budget_bytes": Quota(settings, db, storage).recalc_site_budget()}, ensure_ascii=False))
        else:
            print(json.dumps(sweep(settings, db, storage), ensure_ascii=False))
        return
    if args.command == "serve":
        from .logging_setup import configure_logging, resolve_log_dir

        # uvicorn 使用应用工厂重新读取配置；未传开关时保留调用方的环境设置。
        if args.require_login:
            os.environ["WEBCOMPARE_REQUIRE_LOGIN"] = "1"
        if args.client_transport:
            os.environ["WEBCOMPARE_CLIENT_TRANSPORT"] = args.client_transport
        log_dir = resolve_log_dir(args.log_dir)
        configure_logging(log_dir)
        logger = logging.getLogger("webcompare.serve")
        logger.info("服务日志目录：%s", log_dir)
        try:
            settings = Settings.from_env()
            settings.prepare()
            from .media_requests import is_loopback
            if settings.mode == "personal" and not settings.login_required and not is_loopback(args.host):
                logger.warning("Personal 正在 %s:%s 免密码监听。任何能连接此地址的人都能使用个人工作台，访问服务账号可读的文件。请仅在可信网络使用，或加 --require-login。", args.host, args.port)
            if not (settings.frontend_dir / "index.html").is_file():
                raise ValueError("前端文件缺失，请先构建前端或安装包含前端的发布包")
            import uvicorn

            from .launcher import browser_url, open_when_ready, reuse_running_service

            if args.open_browser:
                url = browser_url(args.host, args.port)
                if reuse_running_service(url, settings):
                    return
                browser_stop = open_when_ready(url)
            else:
                browser_stop = None

            try:
                uvicorn.run("webcompare.app:create_app", factory=True, host=args.host, port=args.port, workers=1, proxy_headers=True, forwarded_allow_ips="127.0.0.1", log_config=None, access_log=True, limit_concurrency=64, timeout_keep_alive=5)
            finally:
                if browser_stop:
                    browser_stop.set()
        except Exception:
            logger.exception("服务启动或运行失败")
            raise SystemExit(1) from None
        return
    settings = Settings.from_env()
    if args.command == "create-template":
        from .template_cli import create_from_file, write_json
        write_json(create_from_file(settings, args.file, args.user, base_url=args.url))
        return
    settings.prepare()
    db = Database(settings.data_dir / "webcompare.sqlite3")
    if args.command == "create-user":
        password = secrets.token_urlsafe(18) if args.generate else sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("密码：")
        user_id = create_user(db, args.username, password, role=args.role, display_name=args.display_name)
        print(json.dumps({"id": user_id, "username": args.username, "role": args.role}, ensure_ascii=False))
        if args.generate:
            print(f"一次性显示的初始密码：{password}")
    elif args.command == "list-users":
        print(json.dumps(db.all("SELECT u.id,u.display_name,u.role,u.active,c.username FROM users u LEFT JOIN local_credentials c ON c.user_id=u.id"), ensure_ascii=False, indent=2))
    elif args.command == "reset-password":
        row = db.one("SELECT user_id FROM local_credentials WHERE username=? COLLATE NOCASE", (args.username,))
        if not row:
            raise SystemExit("本地账号不存在")
        password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("新密码：")
        hashed = hash_password(password)
        with db.connect() as conn:
            conn.execute("UPDATE local_credentials SET password_hash=? WHERE user_id=?", (hashed, row["user_id"]))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (row["user_id"],))
        print("密码已更新，会话已撤销")
    elif args.command == "revoke-share":
        db.execute("UPDATE shares SET revoked=1 WHERE id=?", (args.share_id,))
        print("分享已撤销（若存在）")


if __name__ == "__main__":
    main()

/* Flow_vis 教学内容；源码引用可由 tools/build_flow_data.py 校验并刷新行号。 */
window.FLOW_DATA = {
  "schemaVersion": 2,
  "meta": {
    "project": "WebCompare",
    "title": "Flow_vis 项目导览",
    "description": "从场景、模块与源码理解 WebCompare。"
  },
  "refs": {
    "cli": {
      "path": "webcompare/cli.py",
      "symbol": "main",
      "label": "命令行分发与服务启动",
      "topics": "启动 参数 命令",
      "line": 32
    },
    "config": {
      "path": "webcompare/config.py",
      "symbol": "Settings.from_env",
      "label": "从环境变量读取运行配置",
      "topics": "配置 personal server",
      "line": 219
    },
    "prepare": {
      "path": "webcompare/config.py",
      "symbol": "Settings.prepare",
      "label": "检查目录与运行条件",
      "topics": "启动 配置 校验",
      "line": 297
    },
    "app": {
      "path": "webcompare/app.py",
      "symbol": "create_app",
      "label": "组装 API、依赖与生命周期",
      "topics": "FastAPI 装配 中间件",
      "line": 223
    },
    "guard": {
      "path": "webcompare/app.py",
      "symbol": "SecurityGuard",
      "label": "检查 Origin 与 CSRF 等请求边界",
      "topics": "安全 Cookie CSRF 请求",
      "line": 137
    },
    "session": {
      "path": "webcompare/app.py",
      "symbol": "create_app.require_session",
      "label": "读取并校验会话",
      "topics": "认证 会话 Cookie",
      "line": 400
    },
    "login": {
      "path": "webcompare/auth.py",
      "symbol": "LocalProvider.authenticate",
      "label": "本地用户名密码校验",
      "topics": "登录 账号 认证",
      "line": 235
    },
    "personal": {
      "path": "webcompare/auth.py",
      "symbol": "personal_user",
      "label": "个人工作台的自动会话身份",
      "topics": "personal 免登录",
      "line": 103
    },
    "guest": {
      "path": "webcompare/auth.py",
      "symbol": "create_guest_user",
      "label": "创建有生命周期的访客工作区",
      "topics": "guest 访客 配额",
      "line": 147
    },
    "browse_api": {
      "path": "webcompare/app.py",
      "symbol": "create_app.browse",
      "label": "目录浏览 API 入口",
      "topics": "目录 分页 搜索",
      "line": 722
    },
    "compare_api": {
      "path": "webcompare/app.py",
      "symbol": "create_app.create_comparison",
      "label": "比较清单 API 入口",
      "topics": "比较 清单 分组",
      "line": 735
    },
    "read_asset": {
      "path": "webcompare/app.py",
      "symbol": "create_app.read_asset",
      "label": "媒体与分享访问的授权入口",
      "topics": "资产 权限 分享 live path",
      "line": 500
    },
    "render_api": {
      "path": "webcompare/app.py",
      "symbol": "create_app.render",
      "label": "将视口、画质与当前源版本交给媒体层",
      "topics": "render ROI 图片 HTTP",
      "line": 912
    },
    "diff_api": {
      "path": "webcompare/app.py",
      "symbol": "create_app.diff",
      "label": "授权两路来源并请求差分图",
      "topics": "差分 两图 diff",
      "line": 921
    },
    "shares_api": {
      "path": "webcompare/app.py",
      "symbol": "create_app.create_share",
      "label": "保存比较与查看状态的分享",
      "topics": "分享 可见性 固定清单",
      "line": 1156
    },
    "get_share": {
      "path": "webcompare/app.py",
      "symbol": "create_app.get_share",
      "label": "按可见性恢复分享",
      "topics": "分享 恢复 访问",
      "line": 1247
    },
    "video_api": {
      "path": "webcompare/app.py",
      "symbol": "create_app.video_bytes",
      "label": "视频字节访问入口",
      "topics": "视频 Range",
      "line": 882
    },
    "catalog": {
      "path": "webcompare/files.py",
      "symbol": "FileCatalog",
      "label": "目录、来源与比较清单",
      "topics": "目录 资产 比较",
      "line": 94
    },
    "browse": {
      "path": "webcompare/files.py",
      "symbol": "FileCatalog.browse",
      "label": "过滤、排序、分页列出媒体文件",
      "topics": "目录 搜索 文件",
      "line": 170
    },
    "comparison": {
      "path": "webcompare/files.py",
      "symbol": "FileCatalog.create_comparison",
      "label": "把来源选择变成带资产 ID 的清单",
      "topics": "比较 分组 concat interleave",
      "line": 298
    },
    "asset": {
      "path": "webcompare/files.py",
      "symbol": "FileCatalog.asset",
      "label": "按保存路径取得当前文件与版本",
      "topics": "live path 同名替换 源文件",
      "line": 489
    },
    "revision": {
      "path": "webcompare/files.py",
      "symbol": "revision",
      "label": "文件状态生成缓存版本指纹",
      "topics": "缓存 revision mtime inode",
      "line": 65
    },
    "storage": {
      "path": "webcompare/storage.py",
      "symbol": "Storage.authorize",
      "label": "根据角色、区域和操作授权",
      "topics": "权限 路径 member admin guest",
      "line": 336
    },
    "resolve": {
      "path": "webcompare/storage.py",
      "symbol": "Storage.resolve",
      "label": "将虚拟路径解析为当前真实路径",
      "topics": "路径 符号链接",
      "line": 320
    },
    "verified": {
      "path": "webcompare/storage.py",
      "symbol": "Storage._verified",
      "label": "解析符号链接后重新核对作用域",
      "topics": "安全 符号链接 挂载 越界",
      "line": 477
    },
    "media": {
      "path": "webcompare/media.py",
      "symbol": "MediaService",
      "label": "媒体任务、缓存与进程管理",
      "topics": "缓存 解码 进程 pyvips",
      "line": 539
    },
    "media_render": {
      "path": "webcompare/media.py",
      "symbol": "MediaService.render",
      "label": "按来源版本、视口与参数复用渲染结果",
      "topics": "缓存 ROI render 差分",
      "line": 1102
    },
    "worker": {
      "path": "webcompare/media.py",
      "symbol": "_worker_main",
      "label": "独立子进程中执行媒体解码任务",
      "topics": "worker 进程 pyvips 限制",
      "line": 152
    },
    "raw": {
      "path": "webcompare/raw.py",
      "symbol": "prepare_raw",
      "label": "解析 RAW 格式与显示参数",
      "topics": "RAW Bayer black white gamma",
      "line": 290
    },
    "source_media": {
      "path": "webcompare/source_media.py",
      "symbol": "SourceMediaService",
      "label": "在本机与远端来源之间适配媒体读取",
      "topics": "SSH 物化 预览 来源",
      "line": 26
    },
    "source_pixel": {
      "path": "webcompare/source_media.py",
      "symbol": "SourceMediaService.pixel",
      "label": "从原始来源执行像素取值",
      "topics": "像素 原始 预览",
      "line": 133
    },
    "source_http": {
      "path": "webcompare/source_http.py",
      "symbol": "source_file_response",
      "label": "为本地与 SSH 视频返回范围字节",
      "topics": "视频 Range SFTP",
      "line": 87
    },
    "ssh": {
      "path": "webcompare/ssh.py",
      "symbol": "SSHManager",
      "label": "管理个人模式的远端连接和缓存",
      "topics": "SSH SFTP 来源 连接",
      "line": 253
    },
    "ssh_range": {
      "path": "webcompare/ssh.py",
      "symbol": "SSHManager.open_range",
      "label": "打开独立的远端范围读取通道",
      "topics": "Range SFTP 视频",
      "line": 1037
    },
    "db": {
      "path": "webcompare/db.py",
      "symbol": "Database",
      "label": "持久化账号、会话、资产、比较与分享",
      "topics": "SQLite 状态 数据库",
      "line": 123
    },
    "view_state": {
      "path": "webcompare/view_state.py",
      "symbol": "display_images",
      "label": "按显示状态展开整条图像序列",
      "topics": "顺序 页面 布局",
      "line": 79
    },
    "template": {
      "path": "webcompare/templates.py",
      "symbol": "refresh_template",
      "label": "按保存的选择配置重建比较清单",
      "topics": "模板 刷新 重新选图",
      "line": 116
    },
    "quota": {
      "path": "webcompare/quota.py",
      "symbol": "Quota",
      "label": "为上传和文件操作记账",
      "topics": "配额 磁盘 预留",
      "line": 37
    },
    "upload": {
      "path": "webcompare/upload.py",
      "symbol": "upload_image",
      "label": "分阶段校验并放置单个上传文件",
      "topics": "上传 格式 临时文件",
      "line": 185
    },
    "sandbox": {
      "path": "webcompare/sandbox.py",
      "symbol": "validate_image",
      "label": "在受限子进程中校验上传图片",
      "topics": "沙箱 上传 像素 限制",
      "line": 109
    },
    "exports": {
      "path": "webcompare/exports.py",
      "symbol": "ExportService",
      "label": "异步导出任务与下载生命周期",
      "topics": "ZIP 导出 下载 后台任务",
      "line": 21
    },
    "lifecycle": {
      "path": "webcompare/lifecycle.py",
      "symbol": "sweep",
      "label": "清理过期访客工作区和文件",
      "topics": "访客 生命周期 过期 清理",
      "line": 46
    },
    "main": {
      "path": "frontend/src/main.ts",
      "symbol": "initialize",
      "label": "初始化会话、工作台与入口路由",
      "topics": "前端 启动 认证 页面",
      "line": 417
    },
    "api": {
      "path": "frontend/src/api.ts",
      "symbol": "api",
      "label": "集中管理 HTTP 请求与参数",
      "topics": "fetch CSRF API",
      "line": 160
    },
    "viewer": {
      "path": "frontend/src/viewer.ts",
      "symbol": "Viewer",
      "label": "维护视口、加载媒体并绘制画布",
      "topics": "Canvas 缩放 平移 画质",
      "line": 24
    },
    "state": {
      "path": "frontend/src/state.js",
      "symbol": "makeViewState",
      "label": "创建独立于画布的查看状态",
      "topics": "布局 显示 顺序 参数",
      "line": 38
    },
    "share_state": {
      "path": "frontend/src/state.js",
      "symbol": "viewForShare",
      "label": "规范化可保存的查看参数",
      "topics": "分享 状态 归一化",
      "line": 292
    },
    "gpu": {
      "path": "frontend/src/gpu-effects.js",
      "symbol": "GpuEffects",
      "label": "在浏览器应用曝光、通道与显示效果",
      "topics": "WebGL 曝光 gamma 差分 显示",
      "line": 28
    },
    "video": {
      "path": "frontend/src/video.ts",
      "symbol": "VideoFrames",
      "label": "浏览器按需解码视频帧",
      "topics": "Mediabunny 视频 时间 帧",
      "line": 38
    },
    "picker": {
      "path": "frontend/src/picker-state.js",
      "symbol": "selectPickerEntry",
      "label": "选择器的单选、多选和范围选择",
      "topics": "选图 交互 Shift",
      "line": 39
    },
    "build": {
      "path": "webcompare_build.py",
      "symbol": "build_wheel",
      "label": "打包时将构建好的前端收入安装包",
      "topics": "构建 wheel 静态资源",
      "line": 148
    }
  },
  "modules": [
    {
      "id": "browser",
      "title": "浏览器工作台",
      "type": "PRESENTATION",
      "file": "frontend/src/",
      "summary": "把用户的选择与查看状态转成请求，再将结果绘制到画布。视频帧在浏览器解码。",
      "input": "鼠标与键盘操作、比较清单、媒体响应。",
      "output": "带会话的 HTTP 请求、Canvas 画面、可保存的查看状态。",
      "boundary": "旋转、镜像、曝光等显示调整与源文件分离；前端隐藏按钮不能替代服务端授权。",
      "refs": [
        "main",
        "api",
        "viewer",
        "state",
        "gpu",
        "video",
        "picker"
      ]
    },
    {
      "id": "runtime",
      "title": "启动与配置",
      "type": "RUNTIME",
      "file": "cli.py · config.py",
      "summary": "决定以哪种模式运行、使用哪个状态目录，以及如何启动单个应用实例。",
      "input": "CLI 参数、WEBCOMPARE_* 环境变量、前端构建产物。",
      "output": "校验后的 Settings、Uvicorn 服务和应用工厂。",
      "boundary": "personal 与 server 在启动时确定；账号管理命令应与服务使用同一个状态目录。",
      "refs": [
        "cli",
        "config",
        "prepare",
        "build"
      ]
    },
    {
      "id": "http",
      "title": "API 与会话",
      "type": "HTTP BOUNDARY",
      "file": "webcompare/app.py",
      "summary": "应用的装配中心。这里将认证、请求校验、路径授权和业务模块串起来。",
      "input": "浏览器请求、会话 Cookie、CSRF、分享 ID。",
      "output": "经授权的模块调用、JSON 或媒体字节响应。",
      "boundary": "媒体缓存命中也要先经过访问检查；分享只能读取其清单中的资产。",
      "refs": [
        "app",
        "guard",
        "session",
        "read_asset",
        "login",
        "personal",
        "guest"
      ]
    },
    {
      "id": "catalog",
      "title": "目录与比较清单",
      "type": "CATALOG",
      "file": "webcompare/files.py",
      "summary": "把目录、单图、分组与远端来源统一成可分页查看的比较清单。",
      "input": "已放行的来源路径、组合方式、递归与分组规则。",
      "output": "图片顺序、资产 ID、来源路径与当前文件状态。",
      "boundary": "清单保存的是选择与路径。读取时按路径取得当前内容；新增文件需要重新收集或刷新配置。",
      "refs": [
        "catalog",
        "browse",
        "comparison",
        "asset",
        "revision"
      ]
    },
    {
      "id": "paths",
      "title": "区域与路径授权",
      "type": "ACCESS POLICY",
      "file": "webcompare/storage.py",
      "summary": "在 Server 中，将 public、member、private、guest 与挂载路径映射到真实文件。",
      "input": "用户角色、虚拟路径、read / write / mkdir 操作。",
      "output": "被允许的规范路径与通过作用域检查的真实路径。",
      "boundary": "解析符号链接后重新核对所属区域；member 写成员区还要检查创建者，挂载受 ro/rw 约束。",
      "refs": [
        "storage",
        "resolve",
        "verified"
      ]
    },
    {
      "id": "media",
      "title": "媒体进程与缓存",
      "type": "MEDIA ENGINE",
      "file": "webcompare/media.py",
      "summary": "解码、裁剪、缩放、差分与取色由媒体服务协调，耗时任务交给独立子进程。",
      "input": "当前来源版本、ROI、画质、RAW 显示参数与任务取消。",
      "output": "元数据、像素值、渲染缓存及 ETag。",
      "boundary": "缓存键包含来源版本与处理参数；处理过程中源发生变化可能报 SOURCE_CHANGED，不能把旧缓存当成当前源。",
      "refs": [
        "media",
        "media_render",
        "worker",
        "raw"
      ]
    },
    {
      "id": "sources",
      "title": "本机与远端适配",
      "type": "SOURCE ADAPTER",
      "file": "source_media.py · ssh.py",
      "summary": "个人模式的 SSH 来源复用既有清单与媒体流程。图片按需要取预览或原文件；视频按范围取字节。",
      "input": "本机 Path 或已授权的 ssh:// 来源。",
      "output": "供媒体层读取的本机文件、远端预览，或视频字节流。",
      "boundary": "精确取色读取原始来源；远端视频播放不先把整个视频下载到服务端缓存。",
      "refs": [
        "source_media",
        "source_pixel",
        "source_http",
        "ssh",
        "ssh_range"
      ]
    },
    {
      "id": "state",
      "title": "状态与分享",
      "type": "PERSISTENCE",
      "file": "db.py · templates.py",
      "summary": "SQLite 保存账号、会话、资产、比较与分享。查看参数和比较清单各自有明确的数据结构。",
      "input": "规范化的查看状态、比较清单、分享可见性、模板配置。",
      "output": "可恢复的工作台状态与分享链接。",
      "boundary": "固定分享冻结选择与查看参数，路径下的文件仍按当前内容读取；个人模式配置分享可重新执行选择规则。",
      "refs": [
        "db",
        "view_state",
        "share_state",
        "shares_api",
        "get_share",
        "template"
      ]
    },
    {
      "id": "jobs",
      "title": "上传与后台任务",
      "type": "FILE LIFECYCLE",
      "file": "upload.py · quota.py",
      "summary": "Server 上传经过格式、权限、配额与图片校验；导出和访客清理各自管理任务生命周期。",
      "input": "已认证的上传、导出或清理请求。",
      "output": "完整落盘的文件、配额账目、下载任务与清理结果。",
      "boundary": "上传不覆盖同名文件。失败时回收临时文件与配额预留；访客通过分享包导入临时内容。",
      "refs": [
        "upload",
        "quota",
        "sandbox",
        "exports",
        "lifecycle"
      ]
    }
  ],
  "connections": [
    [
      "browser",
      "http"
    ],
    [
      "runtime",
      "http"
    ],
    [
      "http",
      "catalog"
    ],
    [
      "http",
      "media"
    ],
    [
      "http",
      "state"
    ],
    [
      "catalog",
      "paths"
    ],
    [
      "catalog",
      "sources"
    ],
    [
      "media",
      "sources"
    ],
    [
      "http",
      "jobs"
    ],
    [
      "jobs",
      "paths"
    ],
    [
      "jobs",
      "state"
    ]
  ],
  "scenes": [
    {
      "id": "startup",
      "icon": ">_",
      "title": "启动工作台",
      "subtitle": "入口、配置与会话",
      "tag": "先建立运行上下文",
      "mode": "Personal / Server",
      "description": "从终端命令到浏览器页面。启动成功需要后端环境、前端产物和状态目录共同就绪。",
      "takeaway": {
        "title": "运行上下文决定后续行为。",
        "text": "模式、状态目录和服务账号权限在启动时确定；前端页面只是同一个服务的交互入口。"
      },
      "steps": [
        {
          "title": "命令入口",
          "short": "解析启动参数",
          "heading": "从 CLI 确定本次要做什么",
          "layer": "CLI",
          "input": "webcompare 命令进入 main；显式 serve 启动前台服务，无参数入口会请求打开浏览器。",
          "output": "serve 读取 host、port 等参数，准备配置与日志，随后以单 worker 启动应用工厂。",
          "why": "命令行账号管理与服务共用状态目录，改变目录会进入另一套账号和比较数据。",
          "module": "runtime",
          "refs": [
            "cli"
          ]
        },
        {
          "title": "读取配置",
          "short": "确定模式与目录",
          "heading": "把环境变量变成可用的 Settings",
          "layer": "CONFIG",
          "input": "Settings.from_env 读取模式、状态目录、前端目录和资源限制。",
          "output": "Settings.prepare 校验路径和模式条件；serve 同时确认前端 index.html 已存在。",
          "why": "默认 personal；server 的区域、认证和配额规则在应用装配时启用。",
          "module": "runtime",
          "refs": [
            "config",
            "prepare"
          ]
        },
        {
          "title": "组装服务",
          "short": "API 与生命周期",
          "heading": "create_app 将各层接在一起",
          "layer": "SERVER",
          "input": "应用工厂收到 Settings，创建数据库、目录层、媒体服务及按模式启用的模块。",
          "output": "注册 API、中间件和 lifespan；启动媒体任务，退出时释放资源。",
          "why": "FastAPI 路由负责组合业务流程，文件解析和媒体解码由各自模块承担。",
          "module": "http",
          "refs": [
            "app",
            "guard"
          ]
        },
        {
          "title": "建立会话",
          "short": "自动身份或登录",
          "heading": "浏览器先知道自己是谁",
          "layer": "AUTH",
          "input": "工作台初始化时请求会话；personal 默认可取得个人工作台身份，开启登录后验证本地账号。",
          "output": "服务签发会话 Cookie 与 CSRF；server 使用本地账号，访客入口建立独立临时工作区。",
          "why": "默认 personal 免输入密码仍使用会话，后续 API 继续检查 Cookie，写请求检查 CSRF。",
          "module": "http",
          "refs": [
            "main",
            "personal",
            "login",
            "session"
          ]
        },
        {
          "title": "进入选图",
          "short": "开始一次比较",
          "heading": "前后端使用同一套资源入口",
          "layer": "BROWSER",
          "input": "浏览器取得运行模式、用户信息、来源入口与资源限制。",
          "output": "展示选图工作台，之后浏览目录、创建比较和加载媒体都经过 API。",
          "why": "安装包提供构建好的静态页面；开发时修改 TypeScript 需构建后才会被 Python 服务使用。",
          "module": "browser",
          "refs": [
            "main",
            "api"
          ]
        }
      ]
    },
    {
      "id": "browse",
      "icon": "▦",
      "title": "选图并创建比较",
      "subtitle": "从路径到资产清单",
      "tag": "一份比较是如何形成的",
      "mode": "Personal / Server",
      "description": "选择两个来源，决定组合方式，再创建比较。这里产生的是顺序明确的清单，图片内容在查看时读取。",
      "takeaway": {
        "title": "清单与内容各有生命周期。",
        "text": "同路径文件替换后，后续媒体请求读取新内容；目录新增文件不会自动加入已有固定清单，需要重新选图或刷新配置。"
      },
      "steps": [
        {
          "title": "选择来源",
          "short": "文件、目录或分组",
          "heading": "先记录用户想比较什么",
          "layer": "BROWSER",
          "input": "用户浏览目录、勾选文件，或填写分组规则；前端维护选择历史和显示顺序。",
          "output": "将来源、组合方式、递归与分组条件交给创建比较 API。",
          "why": "concat 将来源依次拼接，interleave 将来源交错；分组比较还保留每组自己的规则。",
          "module": "browser",
          "refs": [
            "picker",
            "api",
            "state"
          ]
        },
        {
          "title": "浏览与授权",
          "short": "路径先过访问边界",
          "heading": "API 为目录访问建立上下文",
          "layer": "HTTP",
          "input": "GET /api/browse 携带会话；个人模式使用服务账号可读的本机路径，也支持该用户的 SSH 来源。",
          "output": "server 先根据角色授权虚拟路径，再解析真实路径；目录层返回过滤、排序和分页后的结果。",
          "why": "浏览器看到的路径由后端解释，不能把浏览器文件选择与服务端绝对路径混为一谈。",
          "module": "http",
          "refs": [
            "browse_api",
            "storage",
            "browse"
          ]
        },
        {
          "title": "收集清单",
          "short": "排序、组合与分组",
          "heading": "将来源展开成稳定的查看顺序",
          "layer": "CATALOG",
          "input": "POST /api/comparisons 传入选择条件，FileCatalog 收集符合条件的媒体文件。",
          "output": "生成图片顺序、来源关系、分组信息和资产 ID，检查来源与文件数量限制。",
          "why": "清单决定哪些图进入比较以及如何分页，图片本体不会因此复制进 SQLite。",
          "module": "catalog",
          "refs": [
            "compare_api",
            "comparison"
          ]
        },
        {
          "title": "保存状态",
          "short": "路径与 ID 入库",
          "heading": "给比较一个可以恢复的身份",
          "layer": "DATABASE",
          "input": "目录层将来源路径和文件状态登记为资产，并生成比较 manifest。",
          "output": "SQLite 保存比较清单；后续接口使用 comparison_id 与 asset_id 找到相应记录。",
          "why": "asset_id 标识登记的来源。当前内容与缓存版本在每次媒体读取时重新取得。",
          "module": "state",
          "refs": [
            "comparison",
            "db",
            "asset"
          ]
        },
        {
          "title": "呈现页面",
          "short": "布局与按需加载",
          "heading": "Viewer 只加载当前需要的画面",
          "layer": "BROWSER",
          "input": "浏览器接收 manifest，将查看状态中的布局、页码、分组和来源顺序应用到清单。",
          "output": "Viewer 请求当前槽位的元数据与图像，再绘制到 Canvas。",
          "why": "切换布局或来源顺序属于查看状态变化，无需改写原图。",
          "module": "browser",
          "refs": [
            "viewer",
            "state",
            "view_state"
          ]
        }
      ]
    },
    {
      "id": "render",
      "icon": "◫",
      "title": "缩放、取色与差分",
      "subtitle": "按需渲染与缓存",
      "tag": "沿着一张图片的请求走",
      "mode": "Personal / Server",
      "description": "从一次视口变化出发，观察授权、当前源版本、媒体缓存与画布显示怎样串联。差分和取色也复用这条访问边界。",
      "takeaway": {
        "title": "显示参数不修改源文件。",
        "text": "预览、精确图像与像素值承担不同用途；服务端重新解析当前路径，缓存按当前来源和处理参数区分。"
      },
      "steps": [
        {
          "title": "视口变化",
          "short": "请求需要的区域",
          "heading": "Viewer 将交互变成媒体参数",
          "layer": "BROWSER",
          "input": "用户缩放、拖动、切页或选择画质，Viewer 根据当前图像和查看区域决定加载内容。",
          "output": "API 请求包含 asset_id、画质与可选 ROI；切换场景时撤销不再需要的加载。",
          "why": "先取得元数据，才知道原图坐标系与有效裁剪范围。",
          "module": "browser",
          "refs": [
            "viewer",
            "api"
          ]
        },
        {
          "title": "访问校验",
          "short": "会话或分享",
          "heading": "媒体请求先确认读取权限",
          "layer": "HTTP",
          "input": "render、pixel 或 diff 请求进入 API；有分享 ID 时检查分享状态、可见性与资产归属。",
          "output": "read_asset 确认来源访问权限；server 同时检查区域与访客内容保留期。",
          "why": "缓存命中也走授权入口。持有一个资产 ID 并不自动获得读取权限。",
          "module": "http",
          "refs": [
            "read_asset",
            "render_api",
            "diff_api"
          ]
        },
        {
          "title": "读取当前源",
          "short": "live path 与版本",
          "heading": "保存路径指向当前文件",
          "layer": "SOURCE",
          "input": "FileCatalog.asset 按登记路径解析文件，server 的虚拟路径继续经过区域与符号链接核验。",
          "output": "取得当前 revision 与大小。同名替换保留资产 ID，后续请求使用新的源版本；缺失的路径返回错误。",
          "why": "固定清单保存选择；文件内容按当前路径读取。处理过程中源变化仍可能报 SOURCE_CHANGED，重试读取当前源。",
          "module": "catalog",
          "refs": [
            "asset",
            "revision",
            "resolve",
            "verified"
          ]
        },
        {
          "title": "解码与缓存",
          "short": "媒体任务在子进程",
          "heading": "按来源版本与参数生成结果",
          "layer": "WORKER",
          "input": "媒体层接收源文件、当前版本、ROI、画质与 RAW 参数；远端来源由适配层按需要获取。",
          "output": "相同参数可复用渲染缓存；未命中时由受资源约束的子进程执行解码、裁剪和差分等操作。",
          "why": "RAW 元数据与显示参数进入同一处理上下文；取色从原始来源进行，不能用缩略预览代替精确读数。",
          "module": "media",
          "refs": [
            "media_render",
            "worker",
            "raw",
            "source_pixel"
          ]
        },
        {
          "title": "绘制与显示",
          "short": "响应回到画布",
          "heading": "把图像结果与显示效果组合",
          "layer": "CANVAS",
          "input": "服务返回图像、尺寸、质量与 ETag；像素接口返回数值，差分接口按两路当前来源计算。",
          "output": "Viewer 绘制画布，并按查看状态应用旋转、镜像、曝光和通道等显示效果。",
          "why": "浏览器显示变换与原图分离；重新分享时保存查看参数即可恢复相同的查看方式。",
          "module": "browser",
          "refs": [
            "viewer",
            "gpu",
            "share_state"
          ]
        }
      ]
    },
    {
      "id": "share",
      "icon": "◎",
      "title": "保存并打开分享",
      "subtitle": "选择、视图与可见性",
      "tag": "从当前画面到可恢复链接",
      "mode": "Personal / Server",
      "description": "分享把比较清单与查看参数连接起来。打开链接时，服务再次判断访问条件，并按保存路径读取当前源文件。",
      "takeaway": {
        "title": "分享固定查看配置，源文件仍为 live path。",
        "text": "固定分享保留选择与查看参数；个人模式还支持可重选的配置分享。源文件删除、权限变化或访客内容过期都会影响读取。"
      },
      "steps": [
        {
          "title": "整理查看状态",
          "short": "规范化布局与参数",
          "heading": "把当前查看方式变成可保存数据",
          "layer": "BROWSER",
          "input": "用户点分享，前端收集页码、布局、差分模式、视口、每格变换与 RAW 等参数。",
          "output": "viewForShare 规范化查看状态，连同比较 ID 与可见性送到 API。",
          "why": "分享保存参数和清单关系；不会把浏览器画布截图当作后续比较的原图。",
          "module": "browser",
          "refs": [
            "share_state",
            "state"
          ]
        },
        {
          "title": "校验与落库",
          "short": "检查来源与档位",
          "heading": "服务端决定这份分享能否建立",
          "layer": "HTTP",
          "input": "POST /api/shares 校验比较归属、页码与可见性；server 还逐项核对来源读取权限。",
          "output": "保存查看状态和比较关联；相同比较与相同状态的重复分享会复用相同链接。",
          "why": "server 可见性有 personal、internal、public；可选档位取决于创建者角色。",
          "module": "state",
          "refs": [
            "shares_api",
            "db"
          ]
        },
        {
          "title": "打开链接",
          "short": "重新检查访问",
          "heading": "链接携带 ID，服务端判定访问",
          "layer": "AUTH",
          "input": "浏览器打开 /s/{share_id} 后请求分享 API，服务检查分享是否存在、撤销或允许当前访问者。",
          "output": "personal 限所有者，internal 限成员与管理员，public 允许持有链接者读取；个人工作台的分享按其链接规则打开。",
          "why": "Server 的 personal 是分享可见性档位，和应用启动模式 personal 是两个不同概念。",
          "module": "http",
          "refs": [
            "get_share",
            "read_asset"
          ]
        },
        {
          "title": "恢复画面",
          "short": "清单加查看参数",
          "heading": "复原选择顺序与查看方式",
          "layer": "BROWSER",
          "input": "浏览器取得 manifest 与规范化 state，重建页码、布局、视口和各格显示参数。",
          "output": "Viewer 请求当前需要的媒体；每次请求仍以分享归属和当前文件状态为依据。",
          "why": "清单可恢复不代表路径永久存在；文件迁移、删除或访问撤销需要由服务端如实返回。",
          "module": "browser",
          "refs": [
            "viewer",
            "get_share",
            "asset"
          ]
        },
        {
          "title": "配置分享",
          "short": "个人模式可重选",
          "heading": "需要重选时保存选择规则",
          "layer": "TEMPLATE",
          "input": "personal 模式可保存可编辑的来源规则及查看配置，在打开或重选时重建当前比较。",
          "output": "refresh_template 重新执行规则，保留可用的查看状态，显示选择错误而非隐瞒失败。",
          "why": "固定清单适合明确选择，配置分享适合目录持续更新和可重选的工作流。",
          "module": "state",
          "refs": [
            "template",
            "comparison"
          ]
        }
      ]
    },
    {
      "id": "video",
      "icon": "▷",
      "title": "按需读取远端视频",
      "subtitle": "SSH、Range 与浏览器解码",
      "tag": "浏览器请求哪一段，就读取哪一段",
      "mode": "Personal · SSH",
      "description": "个人工作台可以把 SSH 视频作为普通比较来源。服务提供被授权的字节，浏览器负责解复用与视频帧解码。",
      "takeaway": {
        "title": "视频播放与图片物化是不同通路。",
        "text": "SSH 视频按范围流式读取，不先完整下载到服务端。帧同步与解码发生在浏览器；远端图片的精确取色则仍需要原始来源。"
      },
      "steps": [
        {
          "title": "连接远端来源",
          "short": "身份与主机核验",
          "heading": "建立当前用户的 SSH 来源",
          "layer": "SOURCE",
          "input": "用户在个人工作台配置 SSH 连接并确认主机身份，服务使用该连接的 SFTP 权限浏览。",
          "output": "SSHManager 管理连接、用户归属和来源 URI；浏览器复用已有选择器。",
          "why": "服务端文件权限之外，还要区分当前用户拥有的 SSH 连接。",
          "module": "sources",
          "refs": [
            "ssh",
            "catalog"
          ]
        },
        {
          "title": "登记视频清单",
          "short": "复用比较模型",
          "heading": "视频也先成为清单中的资产",
          "layer": "CATALOG",
          "input": "按 media_kind=video 浏览并创建比较，来源可能是本地路径或 ssh:// URI。",
          "output": "目录层保存资产路径与视频清单，前端据此创建 VideoFrames。",
          "why": "选中视频不会触发离线拆帧，也不需要在服务端预先展开全部帧。",
          "module": "catalog",
          "refs": [
            "comparison",
            "video"
          ]
        },
        {
          "title": "授权字节请求",
          "short": "媒体访问边界",
          "heading": "视频接口先核对来源访问",
          "layer": "HTTP",
          "input": "播放器请求 /api/assets/{asset_id}/video，可以带分享 ID 与单段 Range。",
          "output": "服务检查资产或分享权限，再交给统一来源响应函数，按请求返回字节或响应头。",
          "why": "Range 只改变读取范围，不绕过原有会话、分享和路径校验。",
          "module": "http",
          "refs": [
            "video_api",
            "read_asset",
            "source_http"
          ]
        },
        {
          "title": "读取指定范围",
          "short": "单段 SFTP 字节流",
          "heading": "按需发送，断开后释放通道",
          "layer": "STREAM",
          "input": "source_file_response 解析范围与文件版本；SSHManager.open_range 打开独立远端读取通道。",
          "output": "返回 206 与 Content-Range，HEAD 只返回头；客户端取消或传输失败时关闭来源流。",
          "why": "不支持多段范围；源变化、断线或短读需要明确报错，不能把不完整文件当成成功结果。",
          "module": "sources",
          "refs": [
            "source_http",
            "ssh_range"
          ]
        },
        {
          "title": "解码与对齐",
          "short": "浏览器视频帧",
          "heading": "用帧时间组织多路比较",
          "layer": "BROWSER",
          "input": "VideoFrames 使用浏览器视频能力按需要解码，Viewer 选择当前帧和各路时间偏移。",
          "output": "在同一画布交互中进行逐帧查看、同步比较与视频像素读取。",
          "why": "浏览器解码能力受视频编码与设备支持约束；服务端范围读取成功不等于浏览器一定能解码该编码。",
          "module": "browser",
          "refs": [
            "video",
            "viewer"
          ]
        }
      ]
    },
    {
      "id": "upload",
      "icon": "+",
      "title": "上传与内容生命周期",
      "subtitle": "区域、配额与落盘",
      "tag": "一个文件怎样完整进入工作区",
      "mode": "Server",
      "description": "成员或管理员上传图片时，先获得目标区域写权限，再经过容量、格式和图像校验。访客通过分享包导入自己的临时内容。",
      "takeaway": {
        "title": "文件与配额一同完成或回收。",
        "text": "同名目标不覆盖；访客工作区带保留期。导出与清理是独立的后台流程，不能用浏览器显示成功代替服务端任务完成。"
      },
      "steps": [
        {
          "title": "校验目标",
          "short": "角色与可写区域",
          "heading": "先确认用户能写到哪里",
          "layer": "POLICY",
          "input": "上传请求携带会话、CSRF、目标区域和文件名；服务端按目标文件路径做写授权。",
          "output": "member 只能管理获准位置和自己创建的成员区内容；guest 的单图上传入口被拒绝。",
          "why": "挂载根不能整体改写，但 rw 挂载内可按授权创建文件；成员区根只放文件夹。",
          "module": "paths",
          "refs": [
            "upload",
            "storage"
          ]
        },
        {
          "title": "暂存与预留",
          "short": "限流与容量",
          "heading": "先接收临时文件，再预留配额",
          "layer": "STAGING",
          "input": "请求体流入暂存区，写入过程中检查大小与磁盘余量，并核对内容魔数与扩展名。",
          "output": "为实际文件大小预留配额，避免并发上传各自看到相同的剩余容量。",
          "why": "临时文件不应先暴露成已成功上传的内容，配额预留也不是最终提交。",
          "module": "jobs",
          "refs": [
            "upload",
            "quota"
          ]
        },
        {
          "title": "校验图片",
          "short": "解码与像素限制",
          "heading": "在受限环境验证图片内容",
          "layer": "VALIDATION",
          "input": "已暂存的 PNG/JPEG/WebP 图片交给校验器，检查格式、尺寸与像素上限。",
          "output": "校验通过才允许落盘；沙箱方式受运行环境与配置决定。视频只核对容器头，由浏览器解码。",
          "why": "验证文件名或 MIME 不等于验证图像内容，媒体解码需有资源限制。",
          "module": "jobs",
          "refs": [
            "upload",
            "sandbox"
          ]
        },
        {
          "title": "落盘与提交",
          "short": "复核并完成记账",
          "heading": "提交前再次检查可写条件",
          "layer": "COMMIT",
          "input": "在变更锁内重查目标授权、目录状态与同名冲突，然后放置完整文件。",
          "output": "文件放置成功后提交配额；失败路径回收临时文件并取消预留，已有同名文件不会被覆盖。",
          "why": "从暂存到落盘之间目录可能变化，所以首次授权不能代替提交时的复核。",
          "module": "jobs",
          "refs": [
            "upload",
            "quota"
          ]
        },
        {
          "title": "后续生命周期",
          "short": "比较、导出与清理",
          "heading": "文件进入普通的目录和任务流程",
          "layer": "LIFECYCLE",
          "input": "成功上传后可浏览并创建比较；导出由后台任务处理，访客内容按保留期管理。",
          "output": "比较清单使用同一目录层，导出可查询状态；sweep 清理过期访客工作区和相应账目。",
          "why": "导出任务 ready 与上传接口成功是不同状态；访客退出、空闲与内容到期都有独立限制。",
          "module": "jobs",
          "refs": [
            "catalog",
            "exports",
            "lifecycle"
          ]
        }
      ]
    }
  ]
};

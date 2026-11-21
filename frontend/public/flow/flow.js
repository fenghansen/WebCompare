/* Flow_vis: buildless, read-only teaching UI. No API requests or external resources. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const languageUrl = new URL(location.href);
  let savedLanguage;
  try { savedLanguage = localStorage.getItem("wc-language"); } catch {}
  const language = ["zh", "en"].includes(savedLanguage) ? savedLanguage : languageUrl.searchParams.get("lang");
  const english = language === "en";
  const text = (zh, en) => english ? en : zh;
  const translations = english && window.FLOW_ENGLISH;
  document.documentElement.lang = text("zh-CN", "en");
  const languageToggle = $("language-toggle");
  languageToggle.textContent = text("English", "简体中文");
  languageToggle.lang = text("en", "zh-CN");
  languageToggle.addEventListener("click", () => {
    const next = text("en", "zh");
    try {
      localStorage.setItem("wc-language", next);
      location.reload();
    } catch {
      languageUrl.searchParams.set("lang", next);
      location.assign(languageUrl.href);
    }
  });
  window.addEventListener("storage", (event) => {
    if (event.key === "wc-language") location.reload();
  });

  function localize(root) {
    if (!translations) return;
    const lookup = (value) => {
      const key = value.trim().replace(/\s+/g, " ");
      return Object.hasOwn(translations.text, key) ? translations.text[key] : value;
    };
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (!node.parentElement?.closest("script, style, noscript, code")) {
        node.nodeValue = node.nodeValue.replace(/\S(?:[\s\S]*\S)?/, lookup);
      }
    }
    for (const element of root.querySelectorAll("[aria-label], [title], [placeholder], meta[name=description]")) {
      for (const attribute of ["aria-label", "title", "placeholder", "content"]) {
        if (element.hasAttribute(attribute)) element.setAttribute(attribute, lookup(element.getAttribute(attribute)));
      }
    }
  }
  localize(document);

  const source = window.FLOW_DATA;
  const data = source?.schemaVersion === 2 && translations ? {
    ...source,
    meta: { ...source.meta, ...translations.meta },
    refs: Object.fromEntries(Object.entries(source.refs).map(([key, ref]) => {
      const [label, topics] = translations.refs[key] || [ref.label, ref.topics];
      return [key, { ...ref, label, topics }];
    })),
    modules: source.modules.map((module) => ({ ...module, ...translations.modules[module.id] })),
    scenes: source.scenes.map((scene) => {
      const translated = translations.scenes[scene.id];
      if (!translated) return scene;
      return {
        ...scene,
        ...translated,
        steps: scene.steps.map((step, index) => ({ ...step, ...translated.steps[index] })),
      };
    }),
  } : source;
  const escape = (value) =>
    String(value ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  if (
    !data ||
    data.schemaVersion !== 2 ||
    !data.scenes?.length ||
    !data.modules?.length
  ) {
    $("view-scene").innerHTML = `<p class="load-error">${text(
      "导览数据未能载入。请确认 flowdata.js 与此页面在同一目录，并刷新页面。",
      "The tour data did not load. Keep flowdata.js beside this page and refresh.",
    )}</p>`;
    return;
  }
  const modules = new Map(data.modules.map((module) => [module.id, module]));
  const scenes = new Map(data.scenes.map((scene) => [scene.id, scene]));
  const viewSections = {
    scenes: "view-scene",
    architecture: "view-architecture",
    permissions: "view-permissions",
    run: "view-run",
    sources: "view-sources",
  };
  let currentView = "scenes",
    sceneId = data.scenes[0].id,
    stepIndex = 0,
    moduleId = "http",
    mode = "personal";
  let playTimer = null,
    toastTimer = null;
  const announce = (text) => {
    $("announcement").textContent = text;
  };
  function toast(text) {
    $("toast").textContent = text;
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      $("toast").hidden = true;
    }, 3000);
  }
  async function copy(value) {
    try {
      if (!navigator.clipboard?.writeText)
        throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(value);
    } catch {
      const previous = document.activeElement,
        field = document.createElement("textarea");
      field.value = value;
      field.setAttribute("readonly", "");
      field.style.cssText = "position:fixed;left:-9999px;top:0";
      document.body.appendChild(field);
      field.select();
      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch {
        /* Clipboard policies vary by browser. */
      }
      field.remove();
      previous?.focus({ preventScroll: true });
      if (!copied) {
        toast(text("浏览器未允许复制，请手动选取页面上的文件路径与符号。", "The browser blocked clipboard access. Select the path and symbol to copy them manually."));
        return;
      }
    }
    toast(text("已复制：", "Copied: ") + value);
  }
  const refText = (ref) =>
    `${ref.path}${ref.line ? ":" + ref.line : ""} # ${ref.symbol}`;
  function refsHtml(keys) {
    return (
      '<div class="source-refs">' +
      keys
        .map((key) => {
          const ref = data.refs[key];
          return /* HTML */ `<button
            type="button"
            class="source-ref"
            data-copy-ref="${escape(key)}"
            title="${text("复制 ", "Copy ")}${escape(refText(ref))}"
            aria-label="${text("复制源码位置：", "Copy source location: ")}${escape(ref.path)}${text("，", ", ")}${escape(ref.symbol)}"
          >
            <span
              ><code>${escape(ref.path)}${ref.line ? ":" + ref.line : ""}</code
              ><span class="ref-symbol">${escape(ref.symbol)}</span></span
            ><span class="copy-icon" aria-hidden="true">⧉</span>
          </button>`;
        })
        .join("") +
      "</div>"
    );
  }
  function stopPlayback() {
    clearInterval(playTimer);
    playTimer = null;
    $("play-scene").textContent = text("自动演示", "Auto-play");
    $("play-scene").setAttribute("aria-pressed", "false");
  }
  function setHash() {
    const hash =
      currentView === "scenes"
        ? `#scenes/${sceneId}/${stepIndex + 1}`
        : currentView === "architecture"
          ? `#architecture/${moduleId}`
          : currentView === "permissions"
            ? `#permissions/${mode}`
            : `#${currentView}`;
    try {
      history.replaceState(null, "", hash);
    } catch {
      /* file viewers may restrict history. */
    }
  }
  function showView(view, updateHash = true) {
    if (!viewSections[view]) view = "scenes";
    if (currentView !== view) stopPlayback();
    currentView = view;
    Object.entries(viewSections).forEach(([key, id]) => {
      $(id).hidden = key !== view;
    });
    document
      .querySelectorAll("[data-view]")
      .forEach((button) =>
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.view === view),
        ),
      );
    if (view === "architecture") requestAnimationFrame(drawConnections);
    if (updateHash) setHash();
  }
  function renderScene() {
    const scene = scenes.get(sceneId),
      step = scene.steps[stepIndex];
    $("scene-list").innerHTML = data.scenes
      .map(
        (item) =>
          /* HTML */ `<button
            type="button"
            class="scene-button"
            data-scene="${item.id}"
            aria-pressed="${item.id === sceneId}"
          >
            <span class="scene-icon" aria-hidden="true"
              >${escape(item.icon)}</span
            ><span
              ><strong>${escape(item.title)}</strong
              ><small>${escape(item.subtitle)}</small></span
            >
          </button>`,
      )
      .join("");
    $("scene-tag").textContent = scene.tag;
    $("scene-title").textContent = scene.title;
    $("scene-mode").textContent = scene.mode;
    $("scene-description").textContent = scene.description;
    $("scene-route").innerHTML = scene.steps
      .map(
        (item, index) =>
          /* HTML */ `<button
            type="button"
            class="route-step${index < stepIndex ? " complete" : ""}"
            data-step="${index}"
            ${index === stepIndex ? 'aria-current="step"' : ""}
            aria-label="${text("步骤", "Step")} ${index + 1}${text("：", ": ")}${escape(item.title)}"
          >
            <span class="step-marker" aria-hidden="true"
              >${String(index + 1).padStart(2, "0")}</span
            ><span
              ><strong>${escape(item.title)}</strong
              ><small>${escape(item.short)}</small></span
            >
          </button>`,
      )
      .join("");
    $("step-count").textContent =
      `${String(stepIndex + 1).padStart(2, "0")} / ${String(scene.steps.length).padStart(2, "0")}`;
    $("step-name").textContent = step.title;
    $("previous-step").disabled = stepIndex === 0;
    $("next-step").disabled = stepIndex === scene.steps.length - 1;
    $("step-detail").innerHTML = /* HTML */ `<div class="detail-head">
        <div>
          <h3>${escape(step.heading)}</h3>
          <p class="detail-subtitle">
            ${escape(modules.get(step.module).title)} · ${escape(step.short)}
          </p>
        </div>
        <span class="detail-layer">${escape(step.layer)}</span>
      </div>
      <div class="detail-grid">
        <div>
          <h4>这一层收到什么</h4>
          <p>${escape(step.input)}</p>
        </div>
        <div>
          <h4>完成后交付什么</h4>
          <p>${escape(step.output)}</p>
        </div>
      </div>
      <p class="why"><strong>理解这一点：</strong>${escape(step.why)}</p>
      ${refsHtml(step.refs)}`;
    localize($("step-detail"));
    $("scene-takeaway").innerHTML = /* HTML */ `<p>
      <strong>${escape(scene.takeaway.title)}</strong>${escape(
        scene.takeaway.text,
      )}
    </p>`;
  }
  function chooseScene(id, index = 0) {
    if (!scenes.has(id)) return;
    stopPlayback();
    sceneId = id;
    stepIndex = Math.max(0, Math.min(index, scenes.get(id).steps.length - 1));
    renderScene();
    showView("scenes");
    const scene = scenes.get(id);
    announce(text(
      `场景：${scene.title}。步骤 ${stepIndex + 1}，${scene.steps[stepIndex].title}。`,
      `Scenario: ${scene.title}. Step ${stepIndex + 1}, ${scene.steps[stepIndex].title}.`,
    ));
  }
  function chooseStep(index, manual = true) {
    if (manual) stopPlayback();
    stepIndex = Math.max(
      0,
      Math.min(index, scenes.get(sceneId).steps.length - 1),
    );
    renderScene();
    setHash();
    const title = scenes.get(sceneId).steps[stepIndex].title;
    announce(text(`步骤 ${stepIndex + 1}，${title}。`, `Step ${stepIndex + 1}, ${title}.`));
  }
  function renderModules() {
    $("module-nodes").innerHTML = data.modules
      .map(
        (module) =>
          /* HTML */ `<button
            type="button"
            class="module-node"
            data-module="${module.id}"
            data-id="${module.id}"
            aria-pressed="${module.id === moduleId}"
            aria-label="${text("查看模块：", "View module: ")}${escape(module.title)}"
          >
            <span class="node-type">${escape(module.type)}</span
            ><strong>${escape(module.title)}</strong
            ><small>${escape(module.file)}</small>
          </button>`,
      )
      .join("");
    renderModuleDetail();
  }
  function renderModuleDetail() {
    const module = modules.get(moduleId);
    document
      .querySelectorAll("[data-module]")
      .forEach((button) =>
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.module === moduleId),
        ),
      );
    const related = data.scenes.filter((scene) =>
      scene.steps.some((step) => step.module === moduleId),
    );
    $("module-detail").innerHTML = /* HTML */ `<p class="eyebrow">
        ${escape(module.type)}
      </p>
      <h3>${escape(module.title)}</h3>
      <p class="module-summary">${escape(module.summary)}</p>
      <dl>
        <dt>接收</dt>
        <dd>${escape(module.input)}</dd>
        <dt>交付</dt>
        <dd>${escape(module.output)}</dd>
        <dt>需要理解的边界</dt>
        <dd>${escape(module.boundary)}</dd>
      </dl>
      ${refsHtml(module.refs)}
      <div class="related-scenes">
        <span>在这些场景里继续看</span>${related
          .map(
            (scene) =>
              /* HTML */ `<button
                type="button"
                data-open-scene="${scene.id}"
                data-open-step="${scene.steps.findIndex(
                  (step) => step.module === moduleId,
                )}"
              >
                ${escape(scene.title)}
              </button>`,
          )
          .join("")}
      </div>`;
    localize($("module-detail"));
    if (currentView === "architecture") requestAnimationFrame(drawConnections);
  }
  function drawConnections() {
    if (
      currentView !== "architecture" ||
      matchMedia("(max-width:600px)").matches
    )
      return;
    const map = $("module-map"),
      rect = map.getBoundingClientRect(),
      svg = $("module-links");
    svg.setAttribute("viewBox", `0 0 ${rect.width} ${rect.height}`);
    const box = (id) =>
      map.querySelector(`[data-module="${id}"]`).getBoundingClientRect();
    svg.innerHTML = data.connections
      .map(([from, to]) => {
        const a = box(from),
          b = box(to),
          horizontal = Math.abs(a.top - b.top) < 10;
        let x1, y1, x2, y2;
        if (horizontal) {
          const right = b.left > a.left;
          x1 = (right ? a.right : a.left) - rect.left;
          y1 = a.top + a.height / 2 - rect.top;
          x2 = (right ? b.left : b.right) - rect.left;
          y2 = b.top + b.height / 2 - rect.top;
        } else {
          x1 = a.left + a.width / 2 - rect.left;
          y1 = a.bottom - rect.top;
          x2 = b.left + b.width / 2 - rect.left;
          y2 = b.top - rect.top;
        }
        const mid = (y1 + y2) / 2,
          path = horizontal
            ? `M${x1},${y1} L${x2},${y2}`
            : `M${x1},${y1} C${x1},${mid} ${x2},${mid} ${x2},${y2}`;
        return /* HTML */ `<path
          class="${from === moduleId || to === moduleId ? "active" : ""}"
          d="${path}"
          ><title>
            ${escape(modules.get(from).title)} 调用或依赖
            ${escape(modules.get(to).title)}
          </title></path
        >`;
      })
      .join("");
  }
  function renderPermissions() {
    document
      .querySelectorAll("[data-mode]")
      .forEach((button) =>
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.mode === mode),
        ),
      );
    const personal = mode === "personal";
    const paths = personal
      ? [
          [
            "用户输入",
            "/pictures/reference.png",
            "路径属于运行 Python 服务的机器，由服务账号的系统权限决定能否读取。",
          ],
          [
            "可选远端",
            "ssh://连接ID/path/image.png",
            "先选择当前用户拥有的 SSH 连接，再按远端账号的 SFTP 权限读取。",
          ],
          [
            "落到媒体层",
            "Path / RemotePath",
            "图片按需读取；精确处理需要原始来源，预览不会改变源文件。",
          ],
        ]
      : [
          [
            "前端虚拟路径",
            "private/用户ID/example.png",
            "public、member、private、guest 与 mount 是服务端管理的区域，并非客户端本机目录。",
          ],
          [
            "权限与真实路径",
            "Storage.authorize → Storage.resolve",
            "先按角色与操作授权，再消除符号链接，重新核对真实路径仍属于原来的作用域。",
          ],
          [
            "落到媒体层",
            text("当前真实文件 + 当前 revision", "Current file + revision"),
            "读取时重新核验路径；同名替换会使用当前内容。只读挂载和成员区创建者规则仍有效。",
          ],
        ];
    const rows = personal
      ? [
          [
            "默认个人身份",
            "自动建立本地会话",
            "服务账号能读的目录；可添加自己的 SSH 来源",
            "原文件以读取为主；比较与分享写入状态目录",
          ],
          [
            "启用账号登录",
            "本地用户名与密码",
            "member / admin 使用服务端放行的来源",
            "通过 --require-login 或对应环境变量启用",
          ],
          [
            "分享链接访问者",
            "有效分享链接",
            "分享清单中的资产",
            "按分享保存的布局与视图读取当前文件",
          ],
        ]
      : [
          [
            "admin",
            "本地账号",
            "管理所有区域；可读系统权限允许的绝对路径",
            "可写授权区域；挂载根仍不可整体改写",
          ],
          [
            "member",
            "本地账号",
            "public、member、自己的 private、已配置挂载",
            "自己的成员区内容、自己的 private 与 rw 挂载",
          ],
          [
            "guest",
            "访客入口",
            "public 与自己的临时 guest 工作区",
            "通过分享包导入临时内容，受配额与保留期限制",
          ],
          [
            "链接访问者",
            "public 分享无需账号",
            "仅分享清单内的资产",
            "不能据此浏览目录或改写源文件",
          ],
        ];
    $("permissions-content").innerHTML = /* HTML */ `<div class="mode-summary">
        <h3>
          ${personal
            ? "Personal：以服务账号可读的来源为工作范围"
            : "Server：本地账号、分区授权与临时访客"}
        </h3>
        <p>
          ${personal
            ? "默认直接进入个人工作台，自动建立会话。可开启本地账号登录；读取范围由服务进程与 SSH 账号权限决定，目录快捷方式不是额外的访问沙箱。"
            : "admin 和 member 使用本地账号登录；guest 使用独立的临时工作区。角色决定区域访问，配额与生命周期决定内容能保存多久。"}
        </p>
      </div>
      <div class="path-flow">
        ${paths
          .map(
            ([label, path, text]) =>
              /* HTML */ `<div class="path-card">
                <span class="path-label">${escape(label)}</span
                ><code>${escape(path)}</code>
                <p>${escape(text)}</p>
              </div>`,
          )
          .join("")}
      </div>
      <div class="table-wrap">
        <table>
          <caption class="sr-only">
            ${personal ? "Personal" : "Server"} 身份、路径与操作范围
          </caption>
          <thead>
            <tr>
              <th scope="col">身份</th>
              <th scope="col">如何进入</th>
              <th scope="col">读取范围</th>
              <th scope="col">写入与限制</th>
            </tr>
          </thead>
          <tbody>
            ${rows
              .map(
                (row) =>
                  /* HTML */ `<tr>
                    ${row
                      .map((cell, index) =>
                        index === 0
                          ? /* HTML */ `<th scope="row">${escape(cell)}</th>`
                          : /* HTML */ `<td>${escape(cell)}</td>`,
                      )
                      .join("")}
                  </tr>`,
              )
              .join("")}
          </tbody>
        </table>
      </div>
      <p class="permission-note">
        <strong>${personal ? "路径提示：" : "分享提示："}</strong>${personal
          ? "浏览器输入的绝对路径由服务端解析。远程打开工作台时，客户端与服务端不一定是同一台机器。对外提供访问时，可启用本地账号登录。"
          : "personal 可见性仅限分享所有者；internal 允许 member / admin；public 允许持有链接者。admin 可创建全部档位，member 可创建 personal / internal，guest 可创建 personal / public，并且只能分享自己的临时内容。"}
      </p>
      ${refsHtml(
        personal
          ? ["config", "personal", "login", "catalog"]
          : ["storage", "resolve", "verified", "guest", "shares_api"],
      )}`;
    localize($("permissions-content"));
  }
  function command(text) {
    return /* HTML */ `<div class="command">
      <button
        type="button"
        data-copy-command="${escape(text)}"
        aria-label="复制命令"
      >
        复制命令
      </button>
      <pre><code>${escape(text)}</code></pre>
    </div>`;
  }
  function renderRun() {
    $("run-content").innerHTML = /* HTML */ `<div class="run-grid">
        <article class="run-card">
          <h3>使用源码工作台</h3>
          <p>
            在项目根目录运行安装脚本。脚本创建项目 Python
            环境、安装依赖并构建前端；首次安装需要访问包源。
          </p>
          <p class="detail-label">Linux</p>
          ${command("bash install.sh\nbash start.sh")}
          <p class="detail-label">Windows · PowerShell</p>
          ${command(".\\install.bat\n.\\start.bat")}
          <p>
            安装后重新打开终端，可运行
            <code>webcompare</code> 启动并打开浏览器。服务终端保持打开，Ctrl+C
            停止。
          </p>
          ${refsHtml(["cli", "config"])}
        </article>
        <article class="run-card">
          <h3>修改代码后运行</h3>
          <p>
            已有 Python 3.12+、Node.js 和可用 Python
            环境时，先构建前端，再从源码启动服务。
          </p>
          ${command(
            "npm --prefix frontend ci\nnpm --prefix frontend run build\npython -m pip install -e .\npython -m webcompare.cli serve",
          )}
          <p>
            默认在 <code>http://localhost:7865</code> 访问。Python
            服务使用构建后的静态资源；修改 TypeScript 后重跑前端构建。
          </p>
          <div class="run-hint">
            状态目录保存账号、会话、比较、分享和缓存。可用
            <code>--data-dir</code> 或
            <code>WEBCOMPARE_DATA_DIR</code> 指定；服务与账号命令使用同一目录。
          </div>
          ${refsHtml(["build", "prepare", "app"])}
        </article>
      </div>
      <article class="run-card">
        <h3>运行起来后，沿着这条线排查</h3>
        <ol class="run-steps">
          <li>
            <strong>服务能否启动</strong
            ><span
              >从 <code>cli.main</code> 读参数与日志，再看
              <code>Settings.prepare</code> 的目录和配置校验。</span
            >
          </li>
          <li>
            <strong>页面能否访问</strong
            ><span
              >检查构建产物与前端目录；<code>GET /api/health</code>
              用于查看后端健康状态。</span
            >
          </li>
          <li>
            <strong>目录能否读取</strong
            ><span
              >先确定模式与当前身份，再看
              <code>FileCatalog.browse</code>；Server 继续追踪
              <code>Storage.authorize</code>。</span
            >
          </li>
          <li>
            <strong>媒体能否显示</strong
            ><span
              >从浏览器请求进入
              <code>read_asset</code
              >，核对当前路径与源文件，再进入媒体任务和渲染缓存。</span
            >
          </li>
          <li>
            <strong>结果能否恢复</strong
            ><span
              >比较清单定义选择，查看状态定义布局与视口，分享可见性定义谁能打开。</span
            >
          </li>
        </ol>
        <div class="run-hint">
          Server 在启动时选择，使用本地 admin / member
          账号并提供访客入口。它还需要显式的状态目录、内容根和服务配置；所需项由
          <code>Settings.prepare</code> 校验。
        </div>
        ${refsHtml(["cli", "config", "session", "asset", "db"])}
      </article>`;
    localize($("run-content"));
  }
  const refModules = (key) =>
    data.modules
      .filter((module) => module.refs.includes(key))
      .map((module) => module.title)
      .join(text("、", ", "));
  function renderSources() {
    const query = $("source-search").value.trim().toLocaleLowerCase();
    const found = Object.entries(data.refs)
      .filter(([key, ref]) =>
        `${ref.path} ${ref.symbol} ${ref.label} ${ref.topics} ${refModules(key)}`
          .toLocaleLowerCase()
          .includes(query),
      )
      .sort(
        (a, b) =>
          a[1].path.localeCompare(b[1].path) ||
          (a[1].line || 0) - (b[1].line || 0),
      );
    $("clear-search").hidden = !query;
    $("source-count").textContent =
      `${found.length} / ${Object.keys(data.refs).length}${text(" 个入口", " entries")}`;
    $("source-results").innerHTML = found.length
      ? found
          .map(
            ([key, ref]) =>
              /* HTML */ `<article class="source-card">
                <div class="source-card-heading">
                  <div>
                    <h3>
                      ${escape(ref.path)}${ref.line ? ":" + ref.line : ""}
                    </h3>
                    <code>${escape(ref.symbol)}</code>
                  </div>
                  <button
                    type="button"
                    data-copy-ref="${escape(key)}"
                    aria-label="${text("复制 ", "Copy ")}${escape(ref.path)} ${escape(ref.symbol)}"
                  >
                    复制
                  </button>
                </div>
                <p>${escape(ref.label)}</p>
                <p>${escape(refModules(key))}</p>
              </article>`,
          )
          .join("")
      : '<p class="empty-state">没有找到匹配的入口。可以试试文件名、符号，或“缓存”“上传”“分享”等用途。</p>';
    localize($("source-results"));
    if (currentView === "sources")
      announce(text(`找到 ${found.length} 个源码入口。`, `Source entries found: ${found.length}.`));
  }
  document.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.view) showView(button.dataset.view);
    if (button.dataset.scene) chooseScene(button.dataset.scene);
    if (button.dataset.step !== undefined)
      chooseStep(Number(button.dataset.step));
    if (button.dataset.module) {
      moduleId = button.dataset.module;
      renderModuleDetail();
      setHash();
      if (matchMedia("(max-width:900px)").matches)
        $("module-detail").scrollIntoView({
          block: "start",
          behavior: matchMedia("(prefers-reduced-motion:reduce)").matches
            ? "instant"
            : "smooth",
        });
      announce(text("模块：", "Module: ") + modules.get(moduleId).title);
    }
    if (button.dataset.mode) {
      mode = button.dataset.mode;
      renderPermissions();
      setHash();
    }
    if (button.dataset.copyRef)
      copy(refText(data.refs[button.dataset.copyRef]));
    if (button.dataset.copyCommand) copy(button.dataset.copyCommand);
    if (button.dataset.openScene) {
      chooseScene(button.dataset.openScene, Number(button.dataset.openStep));
      $("scene-title").scrollIntoView({ block: "start", behavior: "instant" });
    }
  });
  $("previous-step").addEventListener("click", () => chooseStep(stepIndex - 1));
  $("next-step").addEventListener("click", () => chooseStep(stepIndex + 1));
  $("play-scene").addEventListener("click", () => {
    if (playTimer) {
      stopPlayback();
      return;
    }
    if (stepIndex === scenes.get(sceneId).steps.length - 1) chooseStep(0);
    $("play-scene").textContent = text("暂停演示", "Pause");
    $("play-scene").setAttribute("aria-pressed", "true");
    playTimer = setInterval(() => {
      chooseStep(stepIndex + 1, false);
      if (stepIndex === scenes.get(sceneId).steps.length - 1) {
        stopPlayback();
        announce(text("本场景演示完成。", "Scenario complete."));
      }
    }, 6000);
  });
  $("scene-route").addEventListener("keydown", (event) => {
    if (
      ![
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "Home",
        "End",
      ].includes(event.key)
    )
      return;
    event.preventDefault();
    const index =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? scenes.get(sceneId).steps.length - 1
          : stepIndex + (["ArrowLeft", "ArrowUp"].includes(event.key) ? -1 : 1);
    chooseStep(index);
    $("scene-route")
      .querySelector('[aria-current="step"]')
      .focus({ preventScroll: true });
  });
  $("source-search").addEventListener("input", renderSources);
  $("clear-search").addEventListener("click", () => {
    $("source-search").value = "";
    renderSources();
    $("source-search").focus();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopPlayback();
  });
  window.addEventListener("pagehide", stopPlayback);
  new ResizeObserver(drawConnections).observe($("module-map"));
  function readHash() {
    const [view, selection, index] = location.hash.slice(1).split("/");
    if (view === "scenes" && scenes.has(selection)) {
      sceneId = selection;
      stepIndex = Math.max(
        0,
        Math.min(
          Math.trunc(Number(index) || 1) - 1,
          scenes.get(sceneId).steps.length - 1,
        ),
      );
    }
    if (view === "architecture" && modules.has(selection)) moduleId = selection;
    if (view === "permissions" && ["personal", "server"].includes(selection))
      mode = selection;
    renderScene();
    renderModules();
    renderPermissions();
    showView(view, false);
  }
  window.addEventListener("hashchange", () => {
    stopPlayback();
    readHash();
  });
  renderRun();
  renderSources();
  readHash();
})();

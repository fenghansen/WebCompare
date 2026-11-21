const downloadLanguageUrl = new URL(location.href);
let savedDownloadLanguage;
try { savedDownloadLanguage = localStorage.getItem('wc-language'); } catch {}
const downloadLanguage = ['zh', 'en'].includes(savedDownloadLanguage) ? savedDownloadLanguage : downloadLanguageUrl.searchParams.get('lang');
const downloadsEnglish = downloadLanguage === 'en';
const downloadText = (zh, en) => downloadsEnglish ? en : zh;
(() => {
  document.documentElement.lang = downloadText('zh-CN', 'en');
  const toggle = document.querySelector('#language-toggle');
  toggle.textContent = downloadText('English', '简体中文');
  toggle.lang = downloadText('en', 'zh-CN');
  toggle.addEventListener('click', () => {
    const next = downloadText('en', 'zh');
    try {
      localStorage.setItem('wc-language', next);
      location.reload();
    } catch {
      downloadLanguageUrl.searchParams.set('lang', next);
      location.assign(downloadLanguageUrl.href);
    }
  });
  window.addEventListener('storage', event => {
    if (event.key === 'wc-language') location.reload();
  });
  if (!downloadsEnglish || !window.DOWNLOADS_ENGLISH) return;
  const lookup = value => {
    const key = value.trim().replace(/\s+/g, ' ');
    return Object.hasOwn(window.DOWNLOADS_ENGLISH, key) ? window.DOWNLOADS_ENGLISH[key] : value;
  };
  const walker = document.createTreeWalker(document, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.parentElement?.closest('script, style, noscript')) {
      node.nodeValue = node.nodeValue.replace(/\S(?:[\s\S]*\S)?/, lookup);
    }
  }
  for (const element of document.querySelectorAll('[aria-label], [title], meta[name=description]')) {
    for (const attribute of ['aria-label', 'title', 'content']) {
      if (element.hasAttribute(attribute)) element.setAttribute(attribute, lookup(element.getAttribute(attribute)));
    }
  }
})();
(() => {
  const tabs = [...document.querySelectorAll('[data-platform]')];
  const select = (platform, updateLocation = false) => {
    for (const tab of tabs) {
      const active = tab.dataset.platform === platform;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
    }
    document.querySelectorAll('[data-platform-panel]').forEach(panel => { panel.hidden = panel.dataset.platformPanel !== platform; });
    if (updateLocation) history.replaceState(null, '', `#${platform}`);
  };
  const fromHash = () => ['windows', 'linux'].includes(location.hash.slice(1)) ? location.hash.slice(1) : null;
  select(fromHash() || (/Win/i.test(navigator.userAgentData?.platform || navigator.platform) ? 'windows' : 'linux'));
  tabs.forEach((tab, index) => {
    tab.addEventListener('click', () => select(tab.dataset.platform, true));
    tab.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      event.preventDefault();
      const target = tabs[event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + 1) % tabs.length];
      select(target.dataset.platform, true); target.focus();
    });
  });
  window.addEventListener('hashchange', () => { if (fromHash()) select(fromHash()); });
})();
(() => {
  const status = document.querySelector('#release-status');
  const links = [...document.querySelectorAll('[data-package]')];
  const unavailable = () => {
    document.querySelectorAll('[data-package-info]').forEach(node => {
      node.textContent = downloadText('当前站点尚未提供', 'Unavailable on this site');
    });
  };
  const enable = (link, filename) => {
    link.href = filename;
    link.removeAttribute('aria-disabled');
    link.removeAttribute('tabindex');
  };
  // SHA256SUMS names the published files; a separate request checks that each is reachable.
  const patterns = {
    'windows-setup': /^webcompare-desktop-([0-9][0-9A-Za-z.+-]*)-windows-x64-setup\.exe$/,
    'windows-zip': /^webcompare-desktop-([0-9][0-9A-Za-z.+-]*)-windows-x64\.zip$/,
    'linux-deb': /^webcompare-desktop_([0-9][0-9A-Za-z.+-]*)_amd64\.deb$/,
    'linux-tar': /^webcompare-desktop-([0-9][0-9A-Za-z.+-]*)-linux-x64\.tar\.gz$/,
    'python-wheel': /^webcompare-([0-9][0-9A-Za-z.+-]*)-py3-none-any\.whl$/
  };
  const request = async (filename, options = {}) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 8000);
    try { return await fetch(new URL(filename, location.href), { cache:'no-store', ...options, signal:controller.signal }); }
    finally { clearTimeout(timeout); }
  };
  const inspect = async filename => {
    let response = await request(filename, { method:'HEAD' });
    if (response.status === 405 || response.status === 501) {
      response = await request(filename, { headers:{ Range:'bytes=0-0' } });
    }
    const html = /text\/html|application\/xhtml/i.test(response.headers.get('Content-Type') || '');
    const length = response.headers.get('Content-Range')?.match(/\/(\d+)$/)?.[1] || response.headers.get('Content-Length');
    const bytes = length === null ? null : Number(length);
    if (response.body) await response.body.cancel();
    if (!response.ok || html || bytes === 0) return null;
    return Number.isFinite(bytes) && bytes > 0 ? `${(bytes / 1048576).toLocaleString('en-US', { maximumFractionDigits:1 })} MiB` : downloadText('大小以下载为准', 'Size shown at download');
  };
  function showPackage(link, filename, version, size, url = filename) {
    enable(link, url);
    const kind = link.dataset.package;
    document.querySelector(`[data-package-info="${kind}"]`).textContent = `v${version} · ${size}`;
    if (kind === 'linux-deb') document.querySelector('#linux-install-command').textContent = `sudo apt install ./${filename}`;
    if (kind === 'python-wheel') document.querySelector('#wheel-install-command').textContent = `python -m pip install ./${filename}`;
    if (kind === 'windows-zip') document.querySelector('#windows-checksum-command').textContent = `Get-FileHash .\\${filename} -Algorithm SHA256`;
  }
  async function showGitHubRelease() {
    try {
      const response = await request('github-release.json');
      if (!response.ok) return false;
      const { tag, files } = await response.json();
      const version = /^v?(\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?)$/.exec(tag)?.[1];
      if (!version || !files) return false;
      const source = `webcompare-desktop-${version}-corresponding-source.tar.gz`;
      if (files.source?.name !== source || !(files.source.size > 0)) return false;
      const releaseLinks = links.filter(link => link.dataset.package !== 'python-wheel' || files['python-wheel']);
      if (!releaseLinks.every(link => {
        const kind = link.dataset.package;
        return patterns[kind].exec(files[kind]?.name)?.[1] === version && files[kind].size > 0;
      })) return false;
      const base = `https://github.com/fenghansen/WebCompare/releases/download/${tag}/`;
      for (const link of releaseLinks) {
        const { name, size } = files[link.dataset.package];
        showPackage(link, name, version, `${(size / 1048576).toFixed(1)} MiB`, base + name);
      }
      if (!files['python-wheel']) {
        document.querySelector('[data-package-info="python-wheel"]').textContent = downloadText('此版本未提供 wheel', 'This release has no wheel');
      }
      document.querySelectorAll('[data-checksums]').forEach(link => enable(link, base + 'SHA256SUMS'));
      status.textContent = `GitHub Release · ${tag} · `;
      const sourceLink = document.createElement('a');
      sourceLink.href = base + source;
      sourceLink.textContent = downloadText('第三方对应源码', 'Corresponding third-party source');
      status.append(sourceLink);
      return true;
    } catch {
      return false;
    }
  }
  const load = async () => {
    unavailable();
    if (location.protocol === 'file:') {
      status.textContent = downloadText('此预览不含发布附件。可按平台说明使用已获取的源码或安装包。', 'For downloads, open GitHub Releases. Follow the platform instructions for files you already have.');
      document.documentElement.dataset.downloadsReady = 'true';
      return;
    }
    if (await showGitHubRelease()) {
      document.documentElement.dataset.downloadsReady = 'true';
      return;
    }
    try {
      const manifest = await request('SHA256SUMS');
      if (!manifest.ok || /text\/html|application\/xhtml/i.test(manifest.headers.get('Content-Type') || '')) throw new Error('No release manifest');
      const entries = new Map();
      for (const line of (await manifest.text()).split(/\r?\n/)) {
        const entry = line.match(/^[a-f0-9]{64}[ \t]+\*?([^/\\\x00-\x20]+)$/i);
        if (!entry) continue;
        for (const [kind, pattern] of Object.entries(patterns)) {
          const match = entry[1].match(pattern);
          if (match && (!entries.has(kind) || match[1].localeCompare(entries.get(kind).version, undefined, { numeric:true }) > 0)) {
            entries.set(kind, { filename:entry[1], version:match[1] });
          }
        }
      }
      if (!entries.size) throw new Error('No packages');
      document.querySelectorAll('[data-checksums]').forEach(link => enable(link, 'SHA256SUMS'));
      const available = [];
      await Promise.all(links.map(async link => {
        const entry = entries.get(link.dataset.package);
        if (!entry) return;
        try {
          const size = await inspect(entry.filename);
          if (!size) return;
          showPackage(link, entry.filename, entry.version, size);
          available.push(link.dataset.package);
        } catch {}
      }));
      const nativeCount = available.filter(kind => kind !== 'python-wheel').length;
      const wheel = available.includes('python-wheel') ? 'Python wheel · ' : '';
      status.textContent = available.length ? downloadText(
        `本站可用：${wheel}原生桌面包 ${nativeCount} 个，附 SHA-256 校验清单。`,
        `Available on this site: ${wheel}${nativeCount} desktop package${nativeCount === 1 ? '' : 's'}, with SHA-256 checksums.`,
      ) : downloadText('当前站点尚未提供可下载的发布附件。', 'This site has no downloadable release files.');
    } catch {
      status.textContent = downloadText('当前站点尚未提供发布附件。可使用已获取的源码或安装包，也可先打开在线版。', 'Find packages in GitHub Releases, run WebCompare from source, or open the online workbench.');
    }
    document.documentElement.dataset.downloadsReady = 'true';
  };
  load();
})();
(() => {
  const systemTheme=matchMedia('(prefers-color-scheme: dark)');
  let saved;
  try { saved=localStorage.getItem('wc-theme'); } catch {}
  let preference=saved==='light'||saved==='dark'?saved:'system';
  const applyTheme=() => {
    const theme=preference==='system'?(systemTheme.matches?'dark':'light'):preference;
    document.documentElement.dataset.theme=theme;
    document.querySelector('meta[name=theme-color]').setAttribute('content',theme==='dark'?'#08111f':'#f3f7fc');
    document.querySelectorAll('[data-theme-mode]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.themeMode===preference)));
  };
  document.querySelectorAll('[data-theme-mode]').forEach(button=>button.addEventListener('click',()=>{
    preference=button.dataset.themeMode;
    try { if(preference==='system')localStorage.removeItem('wc-theme');else localStorage.setItem('wc-theme',preference); } catch {}
    applyTheme();
  }));
  systemTheme.addEventListener('change',()=>{if(preference==='system')applyTheme();});
  applyTheme();
})();
(() => {
  const demo = document.querySelector('#compare-demo');
  const slider = document.querySelector('#compare-slider');
  const overlay = document.querySelector('#compare-overlay');
  const keys = new Set();
  let heldPointer = null;
  const updateOverlay = () => {
    const held = heldPointer !== null || keys.size > 0;
    demo.dataset.overlay = String(held);
    overlay.setAttribute('aria-pressed', String(held));
  };
  const clearOverlay = () => { heldPointer = null; keys.clear(); updateOverlay(); };
  const updateSplit = () => {
    demo.style.setProperty('--split', `${slider.value}%`);
    document.querySelector('#compare-position').value = `${slider.value}%`;
  };
  const dragSplit = event => {
    const bounds = demo.getBoundingClientRect();
    slider.value = String(Math.round(Math.max(0, Math.min(100, (event.clientX - bounds.left) / bounds.width * 100))));
    updateSplit();
  };
  slider.addEventListener('input', updateSplit);
  demo.addEventListener('pointerdown', event => {
    if (demo.dataset.mode !== 'split' || event.button !== 0) return;
    demo.setPointerCapture(event.pointerId);
    slider.focus({ preventScroll:true });
    dragSplit(event);
  });
  demo.addEventListener('pointermove', event => { if (demo.hasPointerCapture(event.pointerId)) dragSplit(event); });
  for (const type of ['pointerup', 'pointercancel']) demo.addEventListener(type, event => {
    if (demo.hasPointerCapture(event.pointerId)) demo.releasePointerCapture(event.pointerId);
  });
  const hints = {
    side: downloadText('并排看细节，按住叠图查看同一位置。', 'Compare side by side. Hold Overlay to check the same spot.'),
    split: downloadText('左右拖动分界线，对照同一处细节。', 'Drag the divider to compare the same spot.'),
    diff: downloadText('亮处表示差异，按住叠图查看清晰图。', 'Bright areas show differences. Hold Overlay to see the sharp image.'),
  };
  document.querySelectorAll('[data-compare-mode]').forEach(button => button.addEventListener('click', () => {
    clearOverlay();
    const mode = button.dataset.compareMode;
    demo.dataset.mode = mode;
    document.querySelectorAll('[data-compare-mode]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    for (const name of ['side', 'split', 'diff']) document.querySelector(`#compare-${name}`).hidden = name !== mode;
    document.querySelector('#compare-split-controls').hidden = mode !== 'split';
    document.querySelector('#compare-hint').textContent = hints[mode];
  }));
  overlay.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    overlay.setPointerCapture(event.pointerId);
    heldPointer = event.pointerId;
    updateOverlay();
  });
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) overlay.addEventListener(type, () => { heldPointer = null; updateOverlay(); });
  overlay.addEventListener('keydown', event => {
    if (event.key !== ' ' && event.key !== 'Enter') return;
    event.preventDefault(); keys.add(event.key); updateOverlay();
  });
  overlay.addEventListener('keyup', event => { keys.delete(event.key); updateOverlay(); });
  overlay.addEventListener('blur', clearOverlay);
  window.addEventListener('blur', clearOverlay);
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearOverlay(); });
  updateSplit();
})();

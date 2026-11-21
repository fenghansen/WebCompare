import { t } from './i18n.js';
import { api, type ApiError, type Session, type SSHAuthMethod, type SSHConnectInput, type SSHConnection } from './api';
import { isSSHError, sshLocation } from './ssh-state.js';

type Options = {
  session: () => Session | null;
  changed: () => void;
  browse: (path: string) => Promise<void>;
  connected: (connection: SSHConnection) => Promise<void>;
  disconnecting: (connection: SSHConnection) => void;
  failed: (error: unknown, path?: string) => void;
};
type Panel = { box: HTMLElement; close: () => void; alive: () => boolean; onClose: (fn: () => void) => void };
const node = <K extends keyof HTMLElementTagNameMap>(tag: K, text?: string, className?: string) => {
  const element = document.createElement(tag); if (text !== undefined) element.textContent = text; if (className) element.className = className; return element;
};
const button = (label: string, action: () => void, className = '') => {
  const element = node('button', label, className); element.type = 'button'; element.onclick = action; return element;
};
const message = (error: unknown) => error instanceof Error ? error.message : String(error);

/** 连接配置来自本人会话；凭据只留在当前表单，不写浏览器存储或连接列表。 */
export class SSHManager {
  private connections: SSHConnection[] = [];
  private lastPaths = new Map<string, string>();
  private panel: Panel | null = null;
  private epoch = 0;
  private listToken = 0;
  private listController: AbortController | null = null;
  private loading = false;
  private loadError = '';
  constructor(private options: Options) {}

  private owner() {
    const session = this.options.session();
    return session?.mode === 'personal' && ['member', 'admin'].includes(session.user.role) ? session : null;
  }
  reset() {
    ++this.epoch; ++this.listToken; this.listController?.abort(); this.panel?.close();
    this.connections = []; this.lastPaths.clear(); this.loading = false; this.loadError = '';
  }
  connection(id: string) { return this.connections.find(connection => connection.id === id); }
  rememberPath(path: string) { this.lastPaths.set(sshLocation(path)?.id || 'local', path); }
  async load() {
    const owner = this.owner(); if (!owner) return;
    const epoch = this.epoch, token = ++this.listToken, controller = new AbortController();
    this.listController?.abort(); this.listController = controller; this.loading = true; this.loadError = '';
    try {
      const result = await api.sshConnections(controller.signal);
      if (owner !== this.owner() || epoch !== this.epoch || token !== this.listToken) return;
      this.connections = result.connections;
    } catch (error) {
      if (controller.signal.aborted || owner !== this.owner() || epoch !== this.epoch || token !== this.listToken) return;
      this.loadError = message(error);
    } finally {
      if (owner === this.owner() && epoch === this.epoch && token === this.listToken) { this.loading = false; this.options.changed(); }
    }
  }
  private save(connection: SSHConnection) {
    // 使较早发出的列表请求失效，防止它把刚重连的状态覆盖成断开。
    ++this.listToken; this.listController?.abort(); this.loading = false;
    this.connections = [...this.connections.filter(item => item.id !== connection.id), connection];
    this.options.changed();
  }
  sourceBar(path: string, localHome: string) {
    const bar = node('div', undefined, 'ssh-source-bar'), location = sshLocation(path);
    bar.title = t('本机指运行 WebCompare 的机器。SSH 预览只使用远端已有工具加速，不修改原图、无需 sudo 或联网安装；能力不足时在本机处理。');
    const label = node('label', t('来源'), 'ssh-source-label'), select = node('select'); select.setAttribute('aria-label', t('文件来源'));
    const local = node('option', t('本机目录')); local.value = 'local'; select.append(local);
    for (const connection of this.connections) {
      const option = node('option', `${connection.label} · ${connection.status === 'connected' ? t('已连接') : t('未连接')}`);
      option.value = connection.id; select.append(option);
    }
    if (location && !this.connection(location.id)) { const missing = node('option', t('SSH 连接不可用')); missing.value = location.id; select.append(missing); }
    select.value = location?.id || 'local';
    select.onchange = () => {
      const destinationId = select.value;
      // 来源随成功浏览更新；取消登录时，选择框仍应与当前目录保持一致。
      select.value = location?.id || 'local';
      if (destinationId === 'local') { void this.options.browse(this.lastPaths.get('local') || localHome); return; }
      const connection = this.connection(destinationId); if (!connection) { this.showManage(); return; }
      const destination = this.lastPaths.get(connection.id) || connection.home || connection.root;
      if (connection.status === 'connected') void this.options.browse(destination);
      else this.showConnect(connection, destination);
    };
    const manage = button(t('SSH 管理'), () => this.showManage(), 'mini ssh-source-manage'); manage.setAttribute('aria-label', t('管理 SSH 连接')); manage.title = t('添加、删除、断开或重新连接 SSH；查看远端预览方式');
    label.append(select); bar.append(label, manage);
    const connection = location ? this.connection(location.id) : null;
    if (connection) {
      const reconnect = button(connection.status === 'connected' ? t('重连') : t('连接'), () => this.showConnect(connection), 'mini ssh-reconnect');
      reconnect.setAttribute('aria-label', t`重新连接 ${connection.label}`); reconnect.title = `${connection.username}@${connection.host}:${connection.port} · ${connection.status === 'connected' ? t('已连接，点击重新连接') : t('连接已断开')}`; bar.append(reconnect);
      select.title = `${connection.label} · ${connection.username}@${connection.host}:${connection.port}`;
    } else select.title = location ? t('此 SSH 连接不可用，请打开管理窗口') : t('运行 WebCompare 的机器上的目录');
    if (this.loading) { const loading = node('span', '…', 'ssh-source-loading'); loading.title = t('正在读取 SSH 连接列表'); loading.setAttribute('aria-label', t('正在读取 SSH 连接列表')); bar.append(loading); }
    if (this.loadError) { const retry = button('!', () => this.showManage(), 'mini ssh-source-error'); retry.setAttribute('aria-label', t('SSH 连接列表暂不可用，打开管理重试')); retry.title = this.loadError; bar.append(retry); }
    return bar;
  }
  errorAction(error: unknown, path?: string): HTMLElement | null {
    if (!this.owner() || !isSSHError(error)) return null;
    const detail = (error as ApiError).detail, id = typeof detail?.connection_id === 'string' ? detail.connection_id : sshLocation(path)?.id;
    const connection = id ? this.connection(id) : undefined;
    if (connection && connection.status !== 'disconnected') { connection.status = 'disconnected'; this.options.changed(); }
    return button(connection ? t`重新连接 ${connection.label}` : t('管理 SSH 连接'), () => {
      if (connection) this.showConnect(connection); else this.showManage();
    }, 'mini ssh-error-action');
  }
  private openPanel(title: string): Panel {
    this.panel?.close();
    const previous = document.activeElement, backdrop = node('div', undefined, 'dialog-backdrop ssh-dialog-backdrop'), box = node('section', undefined, 'dialog ssh-dialog');
    box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true'); box.setAttribute('aria-label', title);
    let closed = false; const cleanups: Array<() => void> = [];
    const panel: Panel = { box, alive: () => !closed && backdrop.isConnected, onClose: fn => cleanups.push(fn), close: () => {
      if (closed) return; closed = true; cleanups.forEach(fn => fn()); backdrop.remove();
      if (this.panel === panel) this.panel = null;
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    } };
    const header = node('div', undefined, 'dialog-title'), close = button('×', panel.close, 'icon-btn'); close.setAttribute('aria-label', t('关闭 SSH 窗口'));
    header.append(node('h2', title), close); box.append(header); backdrop.append(box); document.querySelector('#app')!.append(backdrop);
    backdrop.onclick = event => { if (event.target === backdrop) panel.close(); };
    backdrop.onkeydown = event => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); panel.close(); }
      if (event.key === 'Tab') {
        const controls = Array.from(box.querySelectorAll<HTMLElement>('button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href]')).filter(item => !item.closest('[hidden]'));
        const first = controls[0], last = controls.at(-1);
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    };
    this.panel = panel; queueMicrotask(() => { if (panel.alive()) box.querySelector<HTMLElement>('input,select,button')?.focus(); });
    return panel;
  }
  showManage() {
    if (!this.owner()) return;
    const panel = this.openPanel(t('SSH 远程文件')), { box } = panel;
    box.append(node('p', t('通过 SSH/SFTP 浏览远端图片和视频。预览会自动检测并使用远端已有的 Python/Pillow 或 ImageMagick 加速；能力不足时回到本机处理。不修改远端原图，无需 sudo、联网安装或在远端部署 WebCompare。')));
    box.append(node('p', t('本机指运行 WebCompare 的机器；本机 SSH 配置、密钥路径和 SSH agent 都位于这台机器。')));
    const list = node('div', undefined, 'ssh-connection-list'), feedback = node('p', '', 'ssh-feedback'); feedback.setAttribute('role', 'status');
    const paint = () => {
      if (!panel.alive()) return; list.replaceChildren();
      if (!this.connections.length) list.append(node('p', this.loading ? t('正在读取连接…') : t('还没有 SSH 连接。')));
      for (const connection of this.connections) {
        const row = node('div', undefined, 'ssh-connection-row'), info = node('div', undefined, 'ssh-connection-info');
        info.append(node('strong', connection.label), node('small', `${connection.username}@${connection.host}:${connection.port} · ${connection.status === 'connected' ? t('已连接') : t('未连接')}`)); row.append(info);
        const actions = node('div', undefined, 'row-actions');
        actions.append(button(t('浏览'), () => { panel.close(); const path = this.lastPaths.get(connection.id) || connection.home || connection.root; if (connection.status === 'connected') void this.options.browse(path); else this.showConnect(connection, path); }, 'mini'));
        actions.append(button(t('重新连接'), () => this.showConnect(connection), 'mini'));
        if (connection.status === 'connected') actions.append(button(t('断开'), () => {
          const owner = this.owner(), epoch = this.epoch; if (!owner) return;
          this.options.disconnecting(connection);
          void api.sshDisconnect(connection.id, owner.csrf).then(() => {
            if (owner !== this.owner() || epoch !== this.epoch) return;
            this.save({ ...connection, status: 'disconnected' }); paint();
            if (panel.alive()) feedback.textContent = t('连接已断开；已选来源、分组和比较仍然保留。');
          }).catch(error => { if (panel.alive()) feedback.textContent = message(error); });
        }, 'mini'));
        actions.append(button(t('删除'), () => this.confirmDelete(connection), 'mini danger')); row.append(actions); list.append(row);
      }
      if (this.loadError) feedback.textContent = this.loadError;
    };
    box.append(list, feedback); const actions = node('div', undefined, 'dialog-actions');
    actions.append(button(t('刷新列表'), () => { void this.load().then(paint); }), button(t('添加 SSH 连接'), () => this.showConnect(), 'primary')); box.append(actions);
    paint(); void this.load().then(paint);
  }
  private confirmDelete(connection: SSHConnection) {
    const panel = this.openPanel(t('删除 SSH 连接'));
    panel.box.append(node('p', t`删除“${connection.label}”的连接配置？远端文件不会删除。引用此连接的已选来源、比较和分享会保留，但需要可用的连接才能读取。`));
    const feedback = node('p', '', 'ssh-feedback'), actions = node('div', undefined, 'dialog-actions');
    actions.append(button(t('取消'), () => this.showManage()), button(t('删除连接配置'), () => {
      const owner = this.owner(), epoch = this.epoch; if (!owner) return;
      this.options.disconnecting(connection);
      void api.sshDelete(connection.id, owner.csrf).then(() => {
        if (owner !== this.owner() || epoch !== this.epoch || !panel.alive()) return;
        ++this.listToken; this.listController?.abort(); this.connections = this.connections.filter(item => item.id !== connection.id); this.lastPaths.delete(connection.id);
        this.options.changed(); this.showManage();
      }).catch(error => { if (panel.alive()) feedback.textContent = message(error); });
    }, 'danger')); panel.box.append(feedback, actions);
  }
  showConnect(connection?: SSHConnection, destination?: string) {
    const owner = this.owner(); if (!owner) return;
    const epoch = this.epoch, panel = this.openPanel(connection ? t`连接 ${connection.label}` : t('添加 SSH 连接')), { box } = panel;
    box.append(node('p', t('可填写主机地址或本机 ~/.ssh/config 中的 Host 别名。密码、上传私钥和密钥口令仅用于当前连接，不保存到磁盘；WebCompare 重启后可能需要重新认证。')));
    box.append(node('p', t('远端预览只使用已有的 Python/Pillow 或 ImageMagick 加速，能力不足时由本机处理；不修改原图，无需 sudo 或联网安装。')));
    const form = node('form', undefined, 'ssh-form'); form.autocomplete = 'off';
    const field = (label: string, input: HTMLInputElement | HTMLSelectElement, hint?: string) => {
      const row = node('label', undefined, 'ssh-field'); row.append(node('span', label), input); if (hint) row.append(node('small', hint)); form.append(row); return row;
    };
    const input = (value = '', type = 'text') => { const result = node('input'); result.type = type; result.value = value; result.autocomplete = 'off'; result.spellcheck = false; return result; };
    const label = input(connection?.label), host = input(connection?.host), port = input(connection ? String(connection.port) : '', 'number'), username = input(connection?.username);
    label.maxLength = 120; host.maxLength = 255; username.maxLength = 80; host.required = true; host.placeholder = t('example.org 或 SSH 配置别名'); port.min = '1'; port.max = '65535'; port.placeholder = t('22 或 SSH 配置值'); username.placeholder = t('留空使用本机 SSH 配置');
    field(t('连接名称（可选）'), label); field(t('主机或 SSH 配置别名'), host); field(t('端口（可选）'), port); field(t('用户名（可选）'), username);
    const method = node('select'); method.setAttribute('aria-label', t('SSH 登录方式'));
    for (const [value, caption] of [['auto', t('本机 SSH 配置 / agent / 默认密钥')], ['password', t('密码')], ['key', t('指定密钥')]]) { const option = node('option', caption); option.value = value; method.append(option); }
    method.value = connection?.auth_method || 'auto'; field(t('登录方式'), method);
    const password = input('', 'password'), passwordRow = field(t('SSH 密码'), password);
    const keyPath = input(connection?.key_path), keyPathRow = field(t('本机私钥路径（可选）'), keyPath, t('指运行 WebCompare 的机器上的文件，例如 ~/.ssh/id_ed25519；也可选择下方私钥文件。'));
    const keyFile = input('', 'file'); keyFile.accept = '.pem,.key,*'; const keyFileRow = field(t('选择私钥文件（可选）'), keyFile, t('只传输当前选择的私钥文本，不保存到连接配置。'));
    const passphrase = input('', 'password'), passphraseRow = field(t('密钥口令（可选）'), passphrase);
    password.maxLength = passphrase.maxLength = 4096; keyPath.maxLength = 4096;
    const feedback = node('p', '', 'ssh-feedback'); feedback.setAttribute('role', 'status'); feedback.setAttribute('aria-live', 'polite');
    const challenge = node('div', undefined, 'ssh-fingerprint'), actions = node('div', undefined, 'dialog-actions'), submit = node('button', connection ? t('连接') : t('连接并添加'), 'primary'); submit.type = 'submit';
    const cancel = button(t('取消'), panel.close); actions.append(cancel, submit); form.append(feedback, challenge, actions); box.append(form);
    let privateKey = '', keyReading = false, inFlight = false, request: AbortController | null = null, fileVersion = 0;
    const showMethod = () => { passwordRow.hidden = method.value !== 'password'; keyPathRow.hidden = keyFileRow.hidden = method.value !== 'key'; passphraseRow.hidden = method.value === 'password'; };
    method.onchange = () => { password.value = passphrase.value = ''; keyFile.value = ''; privateKey = ''; keyReading = false; ++fileVersion; submit.disabled = false; showMethod(); challenge.replaceChildren(); };
    form.oninput = () => challenge.replaceChildren();
    keyFile.onchange = () => {
      const file = keyFile.files?.[0], version = ++fileVersion; privateKey = ''; keyReading = false; challenge.replaceChildren();
      submit.disabled = false;
      if (!file) return;
      if (file.size > 56 * 1024) { feedback.textContent = t('私钥文件超过 56 KiB，请检查是否选择了正确文件；连接请求需为其他字段和 JSON 编码保留空间。'); keyFile.value = ''; return; }
      submit.disabled = true; keyReading = true;
      keyPath.value = '';
      void file.text().then(text => { if (panel.alive() && version === fileVersion) { privateKey = text; feedback.textContent = t('已读取所选私钥，仅用于本次连接。'); } }).catch(() => { if (panel.alive()) feedback.textContent = t('无法读取私钥文件。'); }).finally(() => { if (panel.alive() && version === fileVersion) { keyReading = false; submit.disabled = false; } });
    };
    keyPath.oninput = () => { if (keyPath.value) { ++fileVersion; privateKey = ''; keyReading = false; keyFile.value = ''; submit.disabled = false; } };
    panel.onClose(() => { request?.abort(); ++fileVersion; privateKey = ''; password.value = passphrase.value = keyFile.value = ''; });
    const connect = async (fingerprint?: string) => {
      if (inFlight || !form.reportValidity()) return;
      if (keyReading) { feedback.textContent = t('请等待私钥文件读取完成。'); return; }
      // 显式清空未选认证方式的旧内存凭据，切换密钥/密码时不能意外复用上次秘密。
      const body: SSHConnectInput = { host: host.value.trim(), label: label.value.trim(), username: username.value.trim(), auth_method: method.value as SSHAuthMethod, password: '', private_key: '', key_path: '', passphrase: '' };
      if (port.value) body.port = Number(port.value);
      if (method.value === 'password') body.password = password.value;
      if (method.value === 'key') { if (keyPath.value.trim()) body.key_path = keyPath.value.trim(); if (privateKey) body.private_key = privateKey; }
      if (method.value !== 'password' && passphrase.value) body.passphrase = passphrase.value;
      if (fingerprint) body.host_fingerprint = fingerprint;
      if (new TextEncoder().encode(JSON.stringify(body)).byteLength > 64 * 1024) { feedback.textContent = t('连接信息超过 64 KiB，请减少私钥文件或其他字段的长度。'); return; }
      inFlight = true; request = new AbortController(); feedback.textContent = t('正在连接…'); challenge.replaceChildren();
      const controls = Array.from(form.querySelectorAll<HTMLInputElement | HTMLSelectElement | HTMLButtonElement>('input,select,button')); controls.forEach(control => { if (control !== cancel) control.disabled = true; });
      try {
        const result = await (connection ? api.sshConnect(connection.id, body, owner.csrf, request.signal) : api.sshCreate(body, owner.csrf, request.signal));
        if (!panel.alive() || owner !== this.owner() || epoch !== this.epoch) return;
        this.save(result); panel.close();
        try {
          await this.options.connected(result);
          if (owner !== this.owner() || epoch !== this.epoch) return;
          if (destination !== undefined || !connection) await this.options.browse(destination || result.home || result.root);
        } catch (error) { if (owner === this.owner() && epoch === this.epoch) this.options.failed(error, destination); }
      } catch (error) {
        if (!panel.alive() || owner !== this.owner() || epoch !== this.epoch || request.signal.aborted) return;
        feedback.textContent = message(error);
        const failure = error as ApiError, detail = failure.detail;
        if ((failure.code === 'SSH_HOST_KEY_UNKNOWN' || failure.code === 'SSH_HOST_KEY_CHANGED') && typeof detail?.fingerprint === 'string') {
          const changed = failure.code === 'SSH_HOST_KEY_CHANGED', fingerprint = detail.fingerprint;
          challenge.classList.toggle('ssh-fingerprint-changed', changed);
          challenge.append(node('strong', changed ? t('主机指纹已变化') : t('首次连接：请核对主机指纹')));
          challenge.append(node('p', `${String(detail.host || host.value)}:${String(detail.port || port.value || 22)} · ${String(detail.key_type || 'SSH')}`));
          if (typeof detail.previous_fingerprint === 'string') challenge.append(node('p', t`原指纹：${detail.previous_fingerprint}`, 'ssh-fingerprint-value'));
          challenge.append(node('p', t`新指纹：${fingerprint}`, 'ssh-fingerprint-value'), node('p', t('请通过可信渠道与服务器管理员核对；只有明确确认后才会继续登录。')));
          const confirm = button(changed ? t('确认新指纹并连接') : t('信任此指纹并连接'), () => { void connect(fingerprint); }, changed ? 'danger' : 'primary');
          challenge.append(confirm, button(t('取消连接'), panel.close));
        }
      } finally {
        inFlight = false; if (panel.alive()) controls.forEach(control => { control.disabled = false; });
        // 请求体不作为连接状态保存，尤其不能把口令带入后续列表刷新。
        delete body.password; delete body.private_key; delete body.passphrase;
      }
    };
    form.onsubmit = event => { event.preventDefault(); void connect(); }; showMethod();
    queueMicrotask(() => { if (panel.alive()) (connection && method.value === 'password' ? password : host).focus(); });
  }
}

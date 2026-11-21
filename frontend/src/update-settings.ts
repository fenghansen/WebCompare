import "./update-settings.css";
import {version} from '../package.json';

const releases = 'https://github.com/fenghansen/WebCompare/releases';
const endpoint = '/api/desktop/update';

type Phase = 'idle' | 'checking' | 'current' | 'available' | 'downloading' | 'ready' | 'installing' | 'error';
interface Update {
  current: string;
  latest?: string;
  phase?: Phase;
  automatic?: boolean;
  package?: string;
  notes?: string;
  progress?: number;
  error?: string;
  manual_path?: string;
}

const label = (zh: string, en: string) => document.documentElement.lang.startsWith('en') ? en : zh;

async function request(url: string, csrf?: string, body?: object): Promise<Update> {
  const options: RequestInit = {credentials: 'same-origin', cache: 'no-store'};
  if (body !== undefined) {
    options.method = 'POST';
    options.headers = {'Content-Type': 'application/json', 'X-CSRF-Token': csrf || ''};
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  if (!response.ok) throw new Error('HTTP ' + response.status);
  return response.json();
}

export function renderUpdates(container: HTMLElement, desktop: boolean, csrf?: string) {
  const section = document.createElement('section');
  section.className = 'update-settings';
  const title = document.createElement('h3');
  title.textContent = label('软件更新', 'Software updates');
  const status = document.createElement('p');
  status.setAttribute('role','status');
  const autoLabel = document.createElement('label');
  const auto = document.createElement('input');
  auto.type = 'checkbox';
  autoLabel.append(auto, ' ', label('自动检查并下载新版', 'Automatically check for and download updates'));
  autoLabel.hidden = !desktop;
  const buttons = document.createElement('div');
  buttons.className = 'dialog-actions';
  const check = document.createElement('button');
  check.textContent = label('检查更新', 'Check for updates');
  const action = document.createElement('button');
  action.className = 'primary';
  action.hidden = true;
  const link = document.createElement('a');
  link.href = releases;
  link.target = '_blank';
  link.rel = 'noopener noreferrer';
  link.textContent = label('发行说明', 'Release notes');
  buttons.append(check, action, link);
  const notes = document.createElement('p');
  notes.hidden = true;
  section.append(title, status, autoLabel, buttons, notes);
  container.append(section);
  let state: Update = {current: version};
  let pending = false;
  let generation = 0;
  let timer = 0;

  function paint() {
    const latest = state.latest || '';
    const phase = state.phase || 'idle';
    const message = {
      idle: '',
      checking: label('正在检查…', 'Checking…'),
      current: label('已是最新版', 'Up to date'),
      available: label('可更新至 ', 'Update available: ') + latest,
      downloading: [label('正在下载 ', 'Downloading ') + latest, state.progress === undefined ? '' : state.progress + '%'].filter(Boolean).join(' · '),
      ready: label('已准备好安装 ', 'Ready to install ') + latest,
      installing: label('正在开始安装…', 'Starting the update…'),
      error: label(state.error || '更新未完成，可重试', state.manual_path ? 'Install the downloaded DEB with sudo apt install followed by the path below.' : 'Update did not finish. Retry or open Release notes.'),
    }[phase];
    status.textContent = [`WebCompare ${state.current || version}`, message].filter(Boolean).join(' · ');
    auto.checked = state.automatic === true;
    autoLabel.hidden = !desktop || state.package === null;
    auto.disabled = pending || state.automatic === undefined;
    check.disabled = pending || ['checking','downloading','installing'].includes(phase);
    action.hidden = !desktop || !['ready','available'].includes(phase);
    action.disabled = pending;
    action.textContent = phase === 'ready' ? label('关闭工作台并安装', 'Close and install') : label('下载更新', 'Download update');
    const detail = state.manual_path || (['available','ready'].includes(phase) ? state.notes : '');
    notes.hidden = !detail;
    notes.textContent = detail || '';
  }

  async function refresh(path: string, body?: object) {
    window.clearTimeout(timer);
    const id = ++generation;
    pending = true;
    paint();
    try {
      const result = await request(path, csrf, body);
      if (id === generation) state = result;
    } catch {
      if (id === generation) state = {...state, phase: 'error'};
    } finally {
      if (id === generation && section.isConnected) {
        pending = false;
        paint();
        if (desktop && ['checking','downloading','installing'].includes(state.phase || '')) {
          timer = window.setTimeout(() => { if (section.isConnected) void refresh(endpoint); }, 1500);
        }
      }
    }
  }
  auto.onchange = () => void refresh(endpoint + '/preferences', {automatic: auto.checked});
  check.onclick = () => void (desktop ? refresh(endpoint + '/check', {}) : refresh('/api/updates/latest'));
  action.onclick = () => {
    if (state.phase === 'available') { void refresh(endpoint + '/download', {}); return; }
    if (state.phase === 'ready') {
      void refresh(endpoint + '/install', {});
    }
  };
  paint();
  if (desktop) void refresh(endpoint);
}

let watching = false;
let announced = '';
export function watchDesktopUpdates() {
  if (watching) return;
  watching = true;
  async function check() {
    try {
      const update = await request(endpoint);
      if (update.phase !== 'ready' || !update.latest || announced === update.latest) return;
      const settings = document.querySelector<HTMLButtonElement>('button[data-update-settings]');
      if (!settings) return;
      announced = update.latest;
      const notice = document.createElement('div');
      notice.className = 'update-notice';
      notice.setAttribute('role','status');
      const open = document.createElement('button');
      open.textContent = label(`新版 ${update.latest} 已下载 · 打开设置`, `Update ${update.latest} is ready · Open settings`);
      open.onclick = () => { notice.remove(); document.querySelector<HTMLButtonElement>('button[data-update-settings]')?.click(); };
      const later = document.createElement('button');
      later.textContent = label('稍后','Later');
      later.onclick = () => notice.remove();
      notice.append(open, later);
      document.body.append(notice);
    } catch { /* A background check can wait for the next interval. */ }
  }
  void check();
  window.setInterval(() => { if (!document.hidden) void check(); }, 60_000);
}

import { serverMessage, t } from './i18n.js';
export type Role = 'admin' | 'member' | 'viewer' | 'guest';
export type MediaKind = 'image' | 'video';
export type Mode = 'personal' | 'server';
export type Visibility = 'personal' | 'internal' | 'public';
export type SelectionMode = 'fixed' | 'editable';
export type Area = { id: string; label: string; path: string; writable: boolean };
export type Workspace = { id: string; quota_bytes: number; content_ttl_seconds: number; exit_ttl_seconds: number; idle_ttl_seconds: number };
export type Session = {
  user: { id: string; username: string; display_name: string; role: Role };
  csrf: string; profile: 'cloud' | 'intranet'; mode: Mode; login_required: boolean; local_images?: boolean; local_nvdec?: boolean;
  nvdec_setup?: 'ready' | 'installable' | 'no_gpu' | 'disabled' | 'unsupported';
  limits: { max_pixels: number | null; max_sources: number; max_files_per_source: number };
  workspace?: Workspace;
};
export type Providers = { mode: Mode; guest: boolean; local: boolean; login_required: boolean; guest_max_workspaces?: number; native_desktop?: boolean };
export type Root = { id: string; label: string; path: string };
export type Roots = { home: string | null; shortcuts: Root[]; areas?: Area[] };
export type SSHAuthMethod = 'auto' | 'password' | 'key';
export type SSHConnection = { id: string; label: string; host: string; port: number; username: string; auth_method: SSHAuthMethod; status: 'connected' | 'disconnected'; home: string; root: string; key_path?: string };
export type SSHConnectInput = { host?: string; port?: number; username?: string; label?: string; auth_method?: SSHAuthMethod; password?: string; private_key?: string; key_path?: string; passphrase?: string; host_fingerprint?: string };
export type ApiError = Error & { status?: number; code?: string; detail?: Record<string, unknown> };
export type UploadResult = { revision?: string; path: string; name: string; bytes: number; width: number; height: number };
export type SharepackResult = { imported: Array<{ path: string; name: string; bytes: number }>; skipped_html: number; state: ViewState | null };
export type LinkShareRecord = { id: string; kind?: 'link'; url: string; comparison_id: string; visibility: Visibility; created_at: string; revoked: boolean; selection_mode?: SelectionMode };
export type ExportRecord = { id: string; kind: 'zip'; purpose?: 'files' | 'share'; status: 'queued' | 'compressing' | 'ready' | 'failed'; processed_bytes: number; total_bytes: number; bytes: number; error: string | null; created_at: string; download_url: string | null };
export type FileCopyResult = { copied: number; total_bytes: number; paths: string[] };
export type FileMoveResult = { moved: number; paths: string[] };
export type ShareRecord = LinkShareRecord | ExportRecord;
export type Quota = { used_bytes: number; reserved_bytes: number; limit_bytes: number | null; remaining_bytes: number | null; site_budget_bytes?: number; site_used_bytes?: number };
export type Entry = { name: string; path: string; kind: 'directory' | 'image' | 'video'; bytes?: number; mtime?: number; writable?: boolean };
export type BrowseResult = { path: string; parent: string | null; entries: Entry[]; count: number; truncated: boolean; writable?: boolean; root_only_folders?: boolean };
export type BrowseOpts = { media_kind?: MediaKind; signal?: AbortSignal; offset?: number; q?: string; sort?: string; order?: string };
export type Source = { path: string; label: string; kind?: 'image' | 'video' | 'directory' };
export type GroupRule = { path: string; pattern: string; names?: string[] | null };
export type DraftGroup = { label: string; rules: GroupRule[] };
export type Group = { label: string; images: string[] };
export type Asset = { id: string; name: string; path: string; revision: string; bytes: number };
export type ComparisonConfig = { sources: Array<{ path: string; label?: string | null }>; combine: 'concat' | 'interleave'; recursive: boolean; media_kind: MediaKind; groups?: DraftGroup[] | null; image_order?: string[] };
export type Comparison = { media_kind?: MediaKind; id: string; created_at: string; sources: Source[]; source_images?: string[][]; combine: 'concat' | 'interleave'; recursive: boolean; images: string[]; assets: Record<string, Asset>; groups?: Group[]; selection?: ComparisonConfig };
export type RawSettings = { bayer: 'auto' | 'RGGB' | 'BGGR' | 'GRBG' | 'GBRG'; packing: 'auto' | 'ax_lsb10' | 'mipi_raw10' | 'uint16le' | 'float32le'; width: number | null; height: number | null; stride_pixels: number | null; bit_depth: number | null; black_level: number | null; black_levels: number[] | null; white_level: number | null; gain: number; gamma: number; white_balance: number[] | null };
export type RawResolved = Omit<RawSettings,'bayer' | 'packing' | 'width' | 'height' | 'stride_pixels' | 'bit_depth' | 'white_level' | 'white_balance'> & {
  bayer: Exclude<RawSettings['bayer'],'auto'>; packing: Exclude<RawSettings['packing'],'auto'> | 'npy';
  width: number; height: number; stride_pixels: number; bit_depth: number; white_level: number; white_balance: number[];
};
export type RawMetadata = { resolved: RawResolved; source_dtype: string; source_shape: number[]; metadata_source: string[]; parameter_sources: Record<string,string>; warnings: string[] };
export type Metadata = { width: number; height: number; bands: number; format: 'png' | 'jpeg' | 'webp' | 'raw'; has_alpha: boolean; has_icc: boolean; bit_depth: 8; raw?: RawMetadata };
export type ViewRect = { x: number; y: number; width: number; height: number };
export type PanelState = { view: ViewRect | null; rotation: number; flip_x: boolean; flip_y: boolean; stretch: number };
export type ViewState = { version: 1; group_index: number; mode: 'split' | 'wipe' | 'diff'; direction: 1 | -1; diff_base: number; opacity: number; wipe: number; gain: number; channel: 'rgb' | 'r' | 'g' | 'b'; exposure: number; gamma: number; brightness_ref: number | null; layout: `${number}x${number}`; quality: 'auto' | 'high' | 'exact'; grouping: 'aligned' | 'within'; natural_sort: boolean; transposed?: boolean; view: ViewRect | null; panels: PanelState[]; raw?: RawSettings; video_frame?: number; video_offsets?: Record<string,number> };
export type PageView = { view: ViewRect | null; panels?: PanelState[] };
export type PageViews = Record<string, PageView>;
export type TemplateConfig = { version: 1; comparison: ComparisonConfig; state: ViewState; page_views?: PageViews };
export type Share = { id: string; comparison: Comparison; state: ViewState; created_at: string; owner_display_name: string; visibility?: Visibility; selection_mode?: SelectionMode; editable?: boolean; config?: TemplateConfig | null; page_views?: PageViews; default_page_view?: PageView | null; selection_error?: { code: string; message: string } | null };
export type RenderQuality = 'preview' | 'high' | 'exact';
type RenderOptions = { quality?: RenderQuality; full?: boolean; max_size?: number; box?: ViewRect; share?: string; signal?: AbortSignal; raw?: RawSettings };
let mediaErrorHandler: ((error: ApiError, url: string) => void) | undefined;
let mediaErrorEpoch = 0;
const mediaResponseEpoch = new WeakMap<Response, number>();
export function setMediaErrorHandler(handler: (error: ApiError, url: string) => void) { mediaErrorHandler = handler; }
export function resetMediaErrors() { ++mediaErrorEpoch; }
function assetQuery(share?: string, raw?: RawSettings) {
  const query=new URLSearchParams();
  if(share)query.set('share',share);
  if(raw)query.set('raw',JSON.stringify(raw));
  return query;
}

async function responseJson<T>(response: Response): Promise<T> {
  try { return await response.json() as T; }
  catch (cause) {
    if ((cause as Error).name === 'AbortError') throw cause;
    const error = new Error(t`服务返回了空内容或无效数据（HTTP ${response.status}）。请刷新重试；应用内预览请用系统浏览器打开可访问的服务地址。`) as Error & { status?: number; code?: string };
    error.status = response.status; error.code = 'INVALID_RESPONSE';
    throw error;
  }
}

async function request<T>(path: string, init: RequestInit = {}, retryBusy = false): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = retryBusy ? await mediaFetch(path,init.signal||undefined) : await fetch(path, { ...init, headers, credentials: 'same-origin' });
  if (response.status === 304) return undefined as T;
  if (!response.ok && retryBusy) return mediaFailure(response,t('媒体请求失败'));
  if (!response.ok) {
    let message = t`请求失败（${response.status}）`, detail: Record<string, unknown> | undefined;
    try { const json = await response.json(); message = serverMessage(json?.detail,message); if (json?.detail && typeof json.detail === 'object') detail = json.detail; } catch { /* response may be empty */ }
    // SSH 指纹挑战和断线重连需要结构化信息，不能只留下显示文案。
    const error = new Error(String(message)) as ApiError;
    error.status = response.status; error.detail = detail; error.code = typeof detail?.code === 'string' ? detail.code : undefined;
    throw error;
  }
  return responseJson<T>(response);
}

const waitFor = (milliseconds: number, signal?: AbortSignal) => new Promise<void>((resolve,reject) => {
  if(signal?.aborted){reject(new DOMException('Cancelled','AbortError'));return;}
  const aborted=()=>{clearTimeout(timer);reject(new DOMException('Cancelled','AbortError'));};
  const timer=window.setTimeout(()=>{signal?.removeEventListener('abort',aborted);resolve();},milliseconds);
  signal?.addEventListener('abort',aborted,{once:true});
});
async function mediaFetch(path:string,signal?:AbortSignal) {
  const epoch=mediaErrorEpoch;
  for(let attempt=0;;attempt++) {
    const response=await fetch(path,{credentials:'same-origin',signal});
    if(response.status!==429 || attempt>=5){mediaResponseEpoch.set(response,epoch);return response;}
    await response.body?.cancel().catch(()=>undefined);
    const retryAfter=Number(response.headers.get('Retry-After'));
    await waitFor(Number.isFinite(retryAfter)&&retryAfter>0?retryAfter*1000:250*2**attempt,signal);
  }
}
async function mediaFailure(response:Response,fallback:string):Promise<never> {
  const epoch=mediaResponseEpoch.get(response)??mediaErrorEpoch;
  let message=fallback,detail:Record<string,unknown>|undefined;
  try {const body=await response.json();message=serverMessage(body?.detail,message);if(body?.detail&&typeof body.detail==='object')detail=body.detail;}catch{/* response may be empty */}
  if(response.status===429)message=t('图像任务仍在排队，请稍候后重试');
  const error=new Error(String(message)) as ApiError;error.status=response.status;error.detail=detail;error.code=typeof detail?.code==='string'?detail.code:undefined;
  if(epoch===mediaErrorEpoch&&error.code?.startsWith('SSH_'))mediaErrorHandler?.(error,response.url);
  throw error;
}
/** 保留 Range 等解码器请求头，同时把远端认证/断线错误交给界面处理。 */
export const videoFetch:typeof fetch=async(input,init)=>{
  const epoch=mediaErrorEpoch;
  const response=await fetch(input,init);
  mediaResponseEpoch.set(response,epoch);
  if(!response.ok)return mediaFailure(response,t`视频读取失败（${response.status}）`);
  return response;
};
/** 上传与分享包导入走 raw body（body 就是文件字节），不用 multipart。 */
async function rawPost<T>(path:string,body:Blob,csrf:(string|undefined),fallback:(status:number)=>string):Promise<T> {
  const headers:Record<string,string>={'Content-Type':'application/octet-stream'};
  if(csrf)headers['X-CSRF-Token']=csrf;
  const response=await fetch(path,{method:'POST',headers,credentials:'same-origin',body});
  if(!response.ok)return mediaFailure(response,fallback(response.status));
  return responseJson<T>(response);
}
type UploadOptions = { signal?: AbortSignal; onProgress?: (loaded: number, total: number) => void };
function uploadPost<T>(path:string,body:Blob,csrf:string|undefined,fallback:(status:number)=>string,opts:UploadOptions):Promise<T> {
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest(),signal=opts.signal;
    const abort=()=>xhr.abort();
    const finish=()=>signal?.removeEventListener('abort',abort);
    xhr.open('POST',path);xhr.withCredentials=true;
    xhr.setRequestHeader('Content-Type','application/octet-stream');
    if(csrf)xhr.setRequestHeader('X-CSRF-Token',csrf);
    xhr.upload.onprogress=e=>{if(e.lengthComputable)opts.onProgress?.(Math.min(e.loaded,body.size),body.size);};
    xhr.onload=()=>{
      finish();let json:any;
      try{json=JSON.parse(xhr.responseText);}catch{/* response may be empty */}
      if(xhr.status>=200&&xhr.status<300){resolve(json as T);return;}
      const error=new Error(serverMessage(json?.detail,fallback(xhr.status))) as Error & {status?:number};
      error.status=xhr.status;reject(error);
    };
    xhr.onerror=()=>{finish();reject(new Error(t('网络中断，上传失败')));};
    xhr.onabort=()=>{finish();reject(new DOMException(t('上传已取消'),'AbortError'));};
    if(signal?.aborted){reject(new DOMException(t('上传已取消'),'AbortError'));return;}
    signal?.addEventListener('abort',abort,{once:true});xhr.send(body);
  });
}

export const api = {
  installNVDEC: (csrf: string) => request<{status:'ready'}>('/api/local/nvdec/install', {method:'POST', headers:{'X-CSRF-Token':csrf}}),
  personalMemory: () => request<{ available_bytes: number | null }>('/api/personal/memory', { cache: 'no-store' }),
  providers: () => request<Providers>('/api/auth/providers'),
  login: async (username: string, password: string) => {
    try { return await request<Session>('/api/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }); }
    catch (error) {
      if ((error as Error & { code?: string }).code !== 'INVALID_RESPONSE') throw error;
      // 转发层可能丢失响应正文；用只读请求确认已有会话，避免重复提交口令。
      try { const session = await request<Session>('/api/auth/session'); if (session?.user?.username?.toLowerCase() === username.trim().toLowerCase() && session.csrf) return session; }
      catch { /* 未建立可用会话时保留原响应错误。 */ }
      throw error;
    }
  },
  session: () => request<Session>('/api/auth/session'),
  logout: (csrf: string) => request<{ ok: true }>('/api/auth/logout', { method: 'POST', headers: { 'X-CSRF-Token': csrf } }),
  guestCreate: () => request<Session>('/api/guests', { method: 'POST' }),
  guestExit: (csrf: string) => request<{ ok: true }>('/api/guests/exit', { method: 'POST', headers: { 'X-CSRF-Token': csrf } }),
  roots: () => request<Roots>('/api/roots'),
  sshConnections: (signal?: AbortSignal) => request<{ connections: SSHConnection[] }>('/api/ssh/connections', { signal }),
  sshCreate: (body: SSHConnectInput, csrf: string, signal?: AbortSignal) => request<SSHConnection>('/api/ssh/connections', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify(body), signal }),
  sshConnect: (id: string, body: SSHConnectInput, csrf: string, signal?: AbortSignal) => request<SSHConnection>(`/api/ssh/connections/${encodeURIComponent(id)}/connect`, { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify(body), signal }),
  sshDisconnect: (id: string, csrf: string) => request<{ ok: true }>(`/api/ssh/connections/${encodeURIComponent(id)}/disconnect`, { method: 'POST', headers: { 'X-CSRF-Token': csrf } }),
  sshDelete: (id: string, csrf: string) => request<{ ok: true }>(`/api/ssh/connections/${encodeURIComponent(id)}`, { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } }),
  quota: (signal?: AbortSignal) => request<Quota>('/api/quota', { signal }),
  upload: (dir: string, name: string, body: Blob, csrf?: string, opts: UploadOptions = {}, media_kind:MediaKind='image') => uploadPost<UploadResult>(`/api/uploads?${new URLSearchParams({ dir, name, media_kind })}`, body, csrf,
    status => status === 409 ? t`${name} 同名文件已存在（不覆盖）` : status === 413 ? t`${name} 超过容量或大小限制` : status === 422 ? t`${name} 不是可用的${media_kind === 'video' ? t('视频') : t('图片（PNG、JPEG 或静态 WebP）')}` : t`上传失败（${status}）`, opts),
  importSharepack: (dir: string, body: Blob, csrf?: string) => rawPost<SharepackResult>(`/api/import-sharepack?${new URLSearchParams({ dir })}`, body, csrf,
    status => status === 413 ? t('分享包超过容量或大小限制') : status === 422 ? t('分享包格式不被接受') : t`导入失败（${status}）`),
  mkdir: (path: string, csrf: string, signal?: AbortSignal) => request<{ ok: true; path: string; created: boolean }>('/api/files/mkdir', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ path }), signal }),
  deleteFile: (path: string, csrf: string) => request<{ ok: true }>('/api/files/delete', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ path }) }),
  renameFile: (path: string, new_path: string, csrf: string) => request<{ ok: true }>('/api/files/rename', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ path, new_path }) }),
  browse: (path: string, opts: BrowseOpts = {}) => {
    const p = new URLSearchParams({ path, offset: String(opts.offset ?? 0), limit: '500' });
    if (opts.media_kind) p.set('media_kind',opts.media_kind);
    if (opts.q) p.set('q', opts.q);
    if (opts.sort) p.set('sort', opts.sort);
    if (opts.order) p.set('order', opts.order);
    return request<BrowseResult>(`/api/browse?${p}`, { signal: opts.signal });
  },
  createComparison: (sources: Source[], combine: 'concat' | 'interleave', recursive: boolean, csrf: string, groups?: DraftGroup[], media_kind:MediaKind='image', image_order:string[]=[]) => request<Comparison>('/api/comparisons', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ sources:sources.map(({path,label})=>({path,label})), combine, recursive, media_kind, ...(groups ? { groups } : {}), ...(image_order.length ? { image_order } : {}) }) }),
  comparison: (id: string, share?: string) => request<Comparison>(`/api/comparisons/${encodeURIComponent(id)}${share ? `?share=${encodeURIComponent(share)}` : ''}`),
  metadata: (id: string, share?: string, signal?: AbortSignal, raw?: RawSettings) => {
    const query=assetQuery(share,raw);
    return request<Metadata>(`/api/assets/${encodeURIComponent(id)}/metadata${query.size ? `?${query}` : ''}`, { signal }, true);
  },
  pixel: (id: string, x: number, y: number, share?: string, raw?: RawSettings, signal?: AbortSignal) => {
    const query=assetQuery(share,raw);query.set('x',String(x));query.set('y',String(y));
    return request<{ x: number; y: number; values: number[] }>(`/api/assets/${encodeURIComponent(id)}/pixel?${query}`, {signal}, true);
  },
  render: (id: string, opts: RenderOptions = {}) => {
    const query = new URLSearchParams({ quality: opts.quality || 'preview', max_size: String(opts.max_size || 1024) });
    if (opts.full) query.set('full', 'true');
    if (opts.share) query.set('share', opts.share);
    if (opts.raw) query.set('raw', JSON.stringify(opts.raw));
    if (opts.box) Object.entries(opts.box).forEach(([key, value]) => query.set(key, String(Math.round(value))));
    return mediaFetch(`/api/assets/${encodeURIComponent(id)}/render?${query}`,opts.signal).then(async (response) => {
      if (!response.ok) return mediaFailure(response,t`图像加载失败（${response.status}）`);
      return { blob: await response.blob(), width: Number(response.headers.get('X-Image-Width') || 0), height: Number(response.headers.get('X-Image-Height') || 0), quality: response.headers.get('X-Image-Quality') || opts.quality || 'preview' };
    });
  },
  diff: (left: string, right: string, opts: RenderOptions = {}) => {
    const query = new URLSearchParams({ left, right, quality: opts.quality || 'exact', max_size: String(opts.max_size || 1024) });
    if (opts.full) query.set('full', 'true');
    if (opts.share) query.set('share', opts.share);
    if (opts.raw) query.set('raw', JSON.stringify(opts.raw));
    if (opts.box) Object.entries(opts.box).forEach(([key, value]) => query.set(key, String(Math.round(value))));
    return mediaFetch(`/api/diff?${query}`,opts.signal).then(async (response) => { if (!response.ok) return mediaFailure(response,response.status === 422 ? t('差分要求两张图尺寸一致') : t`差分加载失败（${response.status}）`); return response.blob(); });
  },
  share: (comparison_id: string, state: ViewState, csrf: string, visibility: Visibility = 'personal', selection_mode: SelectionMode = 'fixed', page_views?: PageViews) => request<{ id: string; url: string }>('/api/shares', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ comparison_id, state, visibility, selection_mode, ...(selection_mode === 'editable' && page_views ? { page_views } : {}) }) }),
  shares: (signal?: AbortSignal) => request<{ shares: ShareRecord[] }>('/api/shares', { signal }),
  patchShare: (id: string, visibility: Visibility, csrf: string) => request<{ ok: true; visibility: Visibility }>(`/api/shares/${encodeURIComponent(id)}`, { method: 'PATCH', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ visibility }) }),
  createExport: (comparison_id: string, state: ViewState, csrf: string, visibility: Visibility = 'personal') => request<ExportRecord>('/api/exports', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ comparison_id, state, visibility }) }),
  createFileDownload: (paths: string[], recursive: boolean, csrf: string, media_kind: MediaKind = 'image', signal?: AbortSignal) => request<ExportRecord>('/api/file-downloads', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ paths, recursive, media_kind }), signal }),
  copyFiles: (body: { paths: string[]; destination: string; recursive: boolean; media_kind: MediaKind }, csrf: string, signal?: AbortSignal) => request<FileCopyResult>('/api/file-copies', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify(body), signal }),
  moveFiles: (body: { paths: string[]; destination: string }, csrf: string, signal?: AbortSignal) => request<FileMoveResult>('/api/file-moves', { method: 'POST', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify(body), signal }),
  exportTask: (id: string, signal?: AbortSignal) => request<ExportRecord>(`/api/exports/${encodeURIComponent(id)}`, { signal }),
  deleteExport: (id: string, csrf: string) => request<{ ok: true }>(`/api/exports/${encodeURIComponent(id)}`, { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } }),
  reorder: (comparison_id: string, images: string[], csrf: string) => request<{ ok: true }>(`/api/comparisons/${encodeURIComponent(comparison_id)}/images`, { method: 'PUT', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ images }) }),
  reorderGroups: (comparison_id: string, order: number[], csrf: string) => request<Comparison>(`/api/comparisons/${encodeURIComponent(comparison_id)}/groups`, { method: 'PUT', headers: { 'X-CSRF-Token': csrf }, body: JSON.stringify({ order }) }),
  cache: () => request<{ bytes: number; files: number }>('/api/cache'),
  clearCache: (csrf: string) => request<{ ok: true; removed: number; bytes: number }>('/api/cache/clear', { method: 'POST', headers: { 'X-CSRF-Token': csrf } }),
  getShare: (id: string) => request<Share>(`/api/shares/${encodeURIComponent(id)}`),
  deleteShare: (id: string, csrf: string) => request<{ ok: true }>(`/api/shares/${encodeURIComponent(id)}`, { method: 'DELETE', headers: { 'X-CSRF-Token': csrf } }),
};

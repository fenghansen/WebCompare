import { applyLanguage, currentLanguage, languageKey, preferredLanguage, rawMessage, serverMessage, t } from './i18n.js';
import './style.css';
import { api, resetMediaErrors, setMediaErrorHandler, type Area, type BrowseResult, type Comparison, type DraftGroup, type Entry, type ExportRecord, type LinkShareRecord, type PageView, type PageViews, type Providers, type Quota, type RawSettings, type Role, type Root, type SelectionMode, type Session, type ShareRecord, type Source, type SSHConnection, type TemplateConfig, type ViewState, type Visibility } from './api';
import { layoutOptions, layoutShape, validLayout, makeViewState, normalizeViewState, nextGroup, viewForShare, slotAssets, removeSlot, pageCount, pageInfo, orderedGroupIndices, groupLayout, cellsOf, smartLayout, displayImageIndices, sourceLanes, humanTtl, isRawPath, normalizeRawSettings, rawSettingsKey, imageDisplayQuality } from './state.js';
import { configureLocalDecoder, checkVideo, disconnectRemoteVideos, rememberVideo, forgetVideos, videoExtensions, videoPoster } from './video';
import { Viewer } from './viewer';
import { icon } from './icons';
import { createSelectionHistory, selectPickerEntry, selectVisibleEntries, toggleVisibleEntries } from './picker-state.js';
import { FILE_COPY_MIME, encodeFileCopy, readFileCopy, fileCopyRequest, fileMoveRequest } from './clipboard-state.js';
import { runBounded, prepareUploadDirectory, uploadConcurrency, imageUploadExtensions, clipboardImageExtension, checkImageUploadHeader } from './upload-state.js';
import { navigationTarget, readNavigation } from './navigation-state.js';
import { SSHManager } from './ssh';
import { resolveSSHPath, sourceName, sourceParent, sshLocation } from './ssh-state.js';
import { RailDragController, moveRailItem, type RailDragItem, type RailPlacement } from './rail_drag';
import { renderUpdates, watchDesktopUpdates } from './update-settings';

const app=document.querySelector<HTMLDivElement>('#app')!;
applyLanguage(currentLanguage(),false);
let providers:Providers|null=null;
let session:Session|null=null,comparison:Comparison|null=null,viewer:Viewer|null=null;
let decoderChoice=localStorage.getItem('wc-video-decoder')==='browser'?'browser':'auto';
let installingDecoder=false;
const temporaryComparisons=new WeakSet<Comparison>();
let shortcuts:Root[]=[],areas:Area[]=[],home='',browse:BrowseResult|null=null,selected:Source[]=[];
let pendingBrowse:string|null=null;
let mediaKind:'image'|'video'='image';
let playing=false,playTimer=0,playSpeed=1,loopVideo=false,playGeneration=0;
let state=makeViewState() as ViewState;
let rawDraft:RawSettings=normalizeRawSettings() as RawSettings,rawDraftKey='';
let shareId=location.pathname.match(/^\/s\/([^/]+)$/)?.[1];
let shareOwner='';
let shareConfig:TemplateConfig|null=null,shareEditable=false,restoreViewOnCreate=false;
let pageViews:PageViews={},pageViewsEnabled=false,defaultPageView:PageView={view:null,panels:[]};
let draftCombine:'concat'|'interleave'='concat';
let draftImageOrder:string[]=[];
let recursive=true,busy=false,browseOffset=0;
type UploadItem={file:File;path:string;state:'pending'|'uploading'|'success'|'failed'|'cancelled';loaded:number;error?:string;row?:HTMLElement};
type UploadJob={dir:string;media:'image'|'video';concurrency:number;items:UploadItem[];bytes:number;sent:number;success:number;failed:number;active:Set<UploadItem>;checked:number;phase:'checking'|'confirm'|'uploading'|'done';message:string;controller:AbortController;collapsed:boolean;frame?:number};
let uploadJob:UploadJob|null=null;
let uploadPanelJob:UploadJob|null=null;
let filter='',filterDraft='',sort:'name'|'size'|'mtime'='mtime',order:'asc'|'desc'='desc';
let pickerPathDraft:string|null=null;
let histogramOn=false,edgeDiff=false;
const systemTheme=window.matchMedia('(prefers-color-scheme: dark)');
const savedTheme=localStorage.getItem('wc-theme');
let themePreference=savedTheme==='light'||savedTheme==='dark'?savedTheme:'system';
const themeOptions:Array<[string,string]>=[['system','系统'],['light','浅色'],['dark','深色']];
applyTheme();
let quota:Quota|null=null,quotaError='';
let quotaController:AbortController|null=null,quotaTimer:number|undefined;
let sharesController:AbortController|null=null,sharesTimer:number|undefined;
let recentPaths:string[]=[];
try{recentPaths=JSON.parse(localStorage.getItem('wc-recent-paths')||'[]');}catch{recentPaths=[];}
let treeW=Number(localStorage.getItem('wc-treew'))||null;
let railW=Number(localStorage.getItem('wc-railw'))||150;
const compactScreen=window.matchMedia('(max-width:760px)');
let mobileRailOpen=false,viewToolsOpen=false;
let immersive=false;
let buildingToolbar=false;
const savedTitleScale=Number(localStorage.getItem('wc-title-scale'));
let titleScale=Number.isFinite(savedTitleScale)&&savedTitleScale>=.5?savedTitleScale:1;
const overlayOptions={titles:localStorage.getItem('wc-overlay-titles')!=='false',details:localStorage.getItem('wc-overlay-details')!=='false',coordinates:localStorage.getItem('wc-overlay-coordinates')!=='false'};
function hDrag(g:HTMLElement,get:()=>number,set:(w:number)=>void){
  g.onpointerdown=e=>{g.setPointerCapture(e.pointerId);const x0=e.clientX,w0=get();
    g.onpointermove=ev=>set(Math.max(0,Math.min(900,Math.round(w0+ev.clientX-x0))));
    g.onpointerup=g.onpointercancel=()=>{g.onpointermove=null;};};
}
function applyRailW(){
  const main=document.querySelector('.main') as HTMLElement|null;if(!main)return;
  const collapsed=railW<64||compactScreen.matches&&!mobileRailOpen;
  main.style.gridTemplateColumns=collapsed?'0 0 minmax(0,1fr)':`${railW}px 5px minmax(0,1fr)`;
  const rail=document.querySelector('.lane-rail') as HTMLElement|null;if(rail)rail.style.display=collapsed?'none':'';
}
compactScreen.addEventListener('change',()=>{if(document.querySelector('.main')){applyRailW();groups();}});
let view:'pick'|'compare'=shareId?'compare':'pick';
let listMode:'list'|'thumb'=(localStorage.getItem('wc-listmode')==='thumb'?'thumb':'list');
let browseToken=0;
let remoteRevision=0;
type DNode={path:string;name:string;open:boolean;loaded:boolean;children:DNode[];bm?:boolean;area?:Area};
let tree:DNode[]=[],rootNodes:DNode[]=[];
let bookmarks:{path:string;label:string}[]=[];
const MAX_SELECTION=500;
let draftGroups:DraftGroup[]=[],activeDraftGroup=-1,groupPattern='*_det5',newGroupName='';
let manualTarget='new';
let groupSearch='';
try{bookmarks=JSON.parse(localStorage.getItem('wc-bookmarks')||'[]');}catch{bookmarks=[];}
function saveBookmarks(){localStorage.setItem('wc-bookmarks',JSON.stringify(bookmarks));}
function treeRoots(){return bookmarks.map(b=>({path:b.path,name:b.label,open:false,loaded:false,children:[],bm:true}));}
let posterObserver:IntersectionObserver|null=null,posterController=new AbortController(),posterQueue=Promise.resolve();
const posters=new Map<string,string>();
const sshManager=new SSHManager({session:()=>session,changed:()=>{if(view==='pick')picker();},browse:path=>browseAt(path),connected:refreshSSHConnection,disconnecting:disconnectSSHConnection,failed:(cause,path)=>error(cause,path)});
setMediaErrorHandler((cause,url)=>{
  const request=new URL(url||'/',location.href),asset=request.pathname.match(/^\/api\/assets\/([^/]+)\//)?.[1]||request.searchParams.get('left');
  error(cause,request.searchParams.get('path')||comparison?.assets[decodeURIComponent(asset||'')]?.path);
});
const posterEntries=new WeakMap<Element,Entry>();
function observePoster(entry:Entry,card:HTMLElement) {
  if(!card.isConnected)return;
  const key=JSON.stringify([entry.path,entry.bytes,entry.mtime]),cached=posters.get(key);
  const display=(target:HTMLElement,url:string,name:string)=>{if(target.isConnected){const img=el('img');img.src=url;img.alt=name;target.querySelector('.tdir')?.replaceWith(img);}};
  if(cached){display(card,cached,entry.name);return;}
  const remote=sshLocation(entry.path);
  if(remote&&sshManager.connection(remote.id)?.status==='disconnected'){card.title=t('SSH 连接已断开，重新连接后可读取视频封面。');return;}
  posterEntries.set(card,entry);
  if(!posterObserver){
    const signal=posterController.signal;
    const observer=new IntersectionObserver(items=>{for(const item of items){
      if(!item.isIntersecting)continue;observer.unobserve(item.target);
      const target=item.target as HTMLElement,entry=posterEntries.get(target)!;
      posterQueue=posterQueue.catch(()=>{}).then(async()=>{
        if(signal.aborted||!target.isConnected)return;
        try{const poster=await videoPoster('/api/video?'+new URLSearchParams({path:entry.path}),signal,personalMode());if(signal.aborted)return;posters.set(JSON.stringify([entry.path,entry.bytes,entry.mtime]),poster);while(posters.size>96)posters.delete(posters.keys().next().value!);display(target,poster,entry.name);}
        catch(e){if(!signal.aborted)target.title=(e as Error).message;}
      });
    }});posterObserver=observer;
  }
  posterObserver.observe(card);
}

const isInput=(target:EventTarget|null)=>!!(target as HTMLElement)?.closest('input,select,textarea,[contenteditable=true]');
const canBrowse=()=>!!session&&session.user.role!=='viewer'&&!shareId;
const serverMode=()=>(session?.mode??providers?.mode)==='server';
const personalMode=()=>(session?.mode??providers?.mode)==='personal';
const canEditShare=()=>!!shareId&&shareEditable&&!!shareConfig&&session?.mode==='personal'&&['member','admin'].includes(session.user.role);
function accountName(current:Session) {
  const {display_name:name,role,username}=current.user;
  if(current.mode==='personal'&&!current.login_required&&username===name&&name==='个人工作台')return t('个人工作台');
  if(role==='guest'&&name.startsWith('访客-'))return t`访客-${name.slice(3)}`;
  return name;
}
function areaLabel(area:Area) {
  if(!serverMode())return area.label;
  const {id,label}=area;
  const suffix=label.split(' · ').slice(1).join(' · ');
  if(id==='public')return t('公共区');
  if(id==='member')return t('成员区');
  if(id==='guest')return t('我的临时工作区');
  if(id==='private')return suffix?t`我的私有区 · ${suffix}`:t('我的私有区');
  if(id.startsWith('private:'))return t`私有区 · ${suffix}`;
  if(id.startsWith('guest:'))return t`访客区 · ${suffix}`;
  if(id.startsWith('mount:'))return t`挂载 · ${suffix}`;
  return label;
}
function areaOf(path:string):Area|undefined {
  let best:Area|undefined;
  for(const area of areas){if(path===area.path||path.startsWith(area.path+'/'))if(!best||area.path.length>best.path.length)best=area;}
  return best;
}
/** 只决定是否显示上传 / 删除 / 重命名入口；真正授权始终由服务端 storage.authorize 判定。 */
function writableHere(path:string|null|undefined):boolean {
  if(!path||!session||!serverMode())return false;
  if(browse?.path===path&&typeof browse.writable==='boolean')return browse.writable;
  if(session.user.role==='admin')return path.startsWith('/')||!!areaOf(path);
  const area=areaOf(path);
  if(!area?.writable)return false;
  return !(area.path.startsWith('mount/')&&path===area.path);   // 挂载根只读：成员可写挂载内部，不含根本身
}
function el<K extends keyof HTMLElementTagNameMap>(tag:K,text?:string,cls?:string):HTMLElementTagNameMap[K] {
  const n=document.createElement(tag); if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;
}
function btn(text:string,fn:(ev?:MouseEvent)=>void,cls='',title='') {
  const b=el('button',text,cls);b.type='button';b.onclick=fn;if(title)b.title=title;return b;
}
function iconButton(label:string,name:Parameters<typeof icon>[0],fn:(ev?:MouseEvent)=>void,cls='',title='') {
  const b=btn('',fn,'tool-icon '+cls,title?label+' · '+title:label);b.setAttribute('aria-label',label);b.append(icon(name));return b;
}
function guideLink():HTMLAnchorElement {
  const link=el('a',t('运行导览'),'guide-link');link.href='/flow/';link.target='_blank';link.rel='noopener noreferrer';return link;
}
function downloadLink(label=t('下载')):HTMLAnchorElement {
  const link=el('a',label,'download-link');link.href='/downloads/';link.target='_blank';link.rel='noopener noreferrer';link.title=t('桌面版下载（在新标签页打开）');return link;
}
function segmented(name:string,options:Array<[string,string]>,value:string,onChange:(value:string)=>void):HTMLElement {
  const control=el('div',undefined,'segmented');control.setAttribute('role','radiogroup');control.setAttribute('aria-label',name);
  control.dataset.selected=String(options.findIndex(([key])=>key===value));
  const thumb=el('span',undefined,'segmented-thumb');thumb.setAttribute('aria-hidden','true');control.append(thumb);
  for(const [key,text] of options){
    const label=el('label'),radio=el('input');radio.type='radio';radio.name='wc-'+name;radio.value=key;radio.checked=key===value;
    radio.onchange=()=>{if(radio.checked){control.dataset.selected=String(options.findIndex(([option])=>option===key));onChange(key);}};
    label.append(radio,el('span',text));control.append(label);
  }
  return control;
}
function applyTheme() {
  const theme=themePreference==='system'?(systemTheme.matches?'dark':'light'):themePreference;
  document.documentElement.dataset.theme=theme;
  document.querySelector('meta[name=theme-color]')?.setAttribute('content',theme==='dark'?'#080808':'#f5f5f5');
  document.querySelectorAll<HTMLElement>('.theme-toggle').forEach(control=>{
    control.dataset.selected=String(themeOptions.findIndex(([key])=>key===themePreference));
    control.querySelectorAll<HTMLInputElement>('input').forEach(radio=>{radio.checked=radio.value===themePreference;});
  });
  viewer?.refreshTheme();
}
function selectTheme(next:string){
  themePreference=next;
  if(next==='system')localStorage.removeItem('wc-theme');else localStorage.setItem('wc-theme',next);
  applyTheme();
}
function themeControl():HTMLElement {
  const control=segmented(t('主题'),themeOptions.map(([key,label])=>[key,t(label)]),themePreference,selectTheme);control.classList.add('theme-toggle');
  control.title=t('跟随系统，或固定使用浅色 / 深色主题');return control;
}
systemTheme.addEventListener('change',()=>{if(themePreference==='system')applyTheme();});
function languageControl():HTMLElement {
  const select=el('select',undefined,'language-select');
  select.setAttribute('aria-label',t('界面语言'));
  for(const [value,text] of [['zh','简体中文'],['en','English']]){
    const option=el('option',text);option.value=value;option.lang=value==='zh'?'zh-CN':'en';select.append(option);
  }
  select.value=currentLanguage();
  select.onchange=()=>changeLanguage(select.value==='en'?'en':'zh');
  return labelled(t('界面语言'),select);
}
function changeLanguage(next:'zh'|'en',persist=true) {
  if(next===currentLanguage())return;
  const openSettings=app.querySelector('.settings-dialog');
  const positions=['.tree','.ftable,.thumbgrid','.lane-rail','.compare-group-list'].map(selector=>[selector,app.querySelector(selector)?.scrollTop||0] as const);
  const loginFields=[...app.querySelectorAll<HTMLInputElement>('.login form input[name]')].map(input=>input.value);
  applyLanguage(next,persist);
  if(app.querySelector('.login')){
    if(gateMessage)gate(gateMessage);else login();
    app.querySelectorAll<HTMLInputElement>('.login form input[name]').forEach((input,index)=>{input.value=loginFields[index]??'';});
    app.querySelector<HTMLSelectElement>('.language-select')?.focus();
  }else if(app.querySelector('.workspace')){
    shell(true);render(false);
    for(const [selector,top] of positions){const panel=app.querySelector(selector);if(panel)panel.scrollTop=top;}
    if(openSettings){void settings();app.querySelector<HTMLSelectElement>('.language-select')?.focus();}
  }
}
window.addEventListener('storage',event=>{
  if(event.key===languageKey&&!app.querySelector('.dialog:not(.settings-dialog)'))changeLanguage(preferredLanguage(),false);
});
function applyOverlays() {
  viewer?.setOverlays({titles:overlayOptions.titles,details:overlayOptions.details,titleScale});
  const statusbar=document.querySelector<HTMLElement>('.statusbar');if(statusbar)statusbar.hidden=!overlayOptions.coordinates;
}
function paintImmersive() {
  document.querySelector('.app')?.classList.toggle('immersive',immersive&&view==='compare');
  const full=document.querySelector('.fullscreen-toggle');if(full)full.textContent=immersive||document.fullscreenElement?t('退出全屏'):t('全屏');
}
async function fullscreen() {
  if(immersive||document.fullscreenElement) {
    immersive=false;paintImmersive();
    if(document.fullscreenElement)try{await document.exitFullscreen();}catch{/* 网页布局已退出沉浸。 */}
    return;
  }
  immersive=view==='compare';paintImmersive();
  try{await app.requestFullscreen();}catch(e){if(!immersive)error(e);} // 嵌入浏览器拒绝原生全屏时保留网页沉浸。
}
async function snapshot() {
  const source=viewer,original=comparison;
  if(mediaKind==='video'){stopVideo();await source?.settled();if(source!==viewer||original!==comparison)return;}
  viewer?.canvas.toBlob(blob=>{
    if(!blob)return;
    const a=el('a');a.href=URL.createObjectURL(blob);a.download=`webcompare-page-${state.group_index+1}.png`;a.click();
    window.setTimeout(()=>URL.revokeObjectURL(a.href),1000);
  });
}
let infoNoticeTimer:number|undefined;
function showNotice(target:HTMLElement,text:string,kind:'error'|'info',action?:HTMLElement) {
  target.setAttribute('role',kind==='error'?'alert':'status');
  target.setAttribute('aria-live',kind==='error'?'assertive':'polite');
  target.setAttribute('aria-atomic','true');
  target.hidden=false;target.replaceChildren();
  const message=el('span',text,'notice-message'),actions=el('div',undefined,'notice-actions');
  if(action){action.addEventListener('click',event=>event.stopPropagation());actions.append(action);}
  const close=btn(t('关闭'),()=>{target.replaceChildren();target.hidden=true;},'notice-close',kind==='error'?t('关闭错误提示'):t('关闭通知'));
  close.setAttribute('aria-label',kind==='error'?t('关闭错误提示'):t('关闭通知'));
  actions.append(close);target.append(message,actions);target.hidden=false;
}
function error(e:unknown,path?:string) {
  const message=e instanceof Error?e.message:String(e);
  const remotePath=path||(sshLocation(browse?.path)?browse?.path:Object.values(comparison?.assets||{}).find(asset=>sshLocation(asset.path))?.path)||selected.find(source=>sshLocation(source.path))?.path;
  const action=sshManager.errorAction(e,remotePath);
  const target=document.querySelector<HTMLElement>('.notice-error')||document.querySelector<HTMLElement>('.login-error');
  if(target)showNotice(target,message,'error',action||undefined);
}
function info(text:string){
  const target=document.querySelector<HTMLElement>('.notice-info');if(!target)return;
  if(infoNoticeTimer!==undefined)window.clearTimeout(infoNoticeTimer);
  showNotice(target,text,'info');
  infoNoticeTimer=window.setTimeout(()=>{target.replaceChildren();target.hidden=true;infoNoticeTimer=undefined;},3500);
}
function clearError(){
  if(infoNoticeTimer!==undefined){window.clearTimeout(infoNoticeTimer);infoNoticeTimer=undefined;}
  document.querySelectorAll<HTMLElement>('.notice-error,.notice-info,.login-error').forEach(n=>{n.replaceChildren();n.hidden=true;});
}
function status(text:string){const n=document.querySelector<HTMLElement>('.coordinate-status');if(n){n.textContent=text;n.title=text;}}
function fmtSize(bytes:number){return bytes>=1048576?(bytes/1048576).toFixed(1)+' MB':bytes>=1024?(bytes/1024).toFixed(0)+' KB':bytes+' B';}
function fmtQuota(bytes:number){return bytes>=1073741824?Number((bytes/1073741824).toFixed(2))+' GiB':bytes>=1048576?Number((bytes/1048576).toFixed(1))+' MiB':fmtSize(bytes);}
function paintQuota() {
  const box=document.querySelector<HTMLElement>('.account-quota');if(!box)return;
  box.replaceChildren(el('strong',t('账号空间')));
  if(!quota){box.append(el('span',quotaError||t('正在读取容量…'),'muted'));return;}
  const {used_bytes:used,reserved_bytes:reserved,limit_bytes:limit,remaining_bytes:remaining}=quota;
  box.title=t`已用 ${fmtQuota(used)} / 总量 ${limit===null?t('不设账号上限'):fmtQuota(limit)} · 剩余 ${remaining===null?t('不限'):fmtQuota(remaining)}${reserved?t` · 处理中 ${fmtQuota(reserved)}`:''}`;
  if(limit!==null){
    const progress=el('progress');progress.max=Math.max(1,limit);progress.value=Math.min(limit,used+reserved);
    progress.setAttribute('aria-label',t('账号空间使用量'));progress.setAttribute('aria-valuetext',t`已用 ${fmtQuota(used)}，总量 ${fmtQuota(limit)}，剩余 ${fmtQuota(remaining??0)}`);box.append(progress);
  }
  box.append(el('span',t`${fmtQuota(used)} / ${limit===null?t('不设账号上限'):fmtQuota(limit)} · 剩余 ${remaining===null?t('不限'):fmtQuota(remaining)}`,'quota-values'));
  if(limit===null&&Number.isFinite(quota.site_budget_bytes)&&Number.isFinite(quota.site_used_bytes))box.title+=t` · 站内用量 ${fmtQuota(quota.site_used_bytes!)} / 预算 ${fmtQuota(quota.site_budget_bytes!)}`;
  if(quotaError)box.append(el('span',t('容量更新暂不可用'),'muted'));
}
function stopQuota(){window.clearTimeout(quotaTimer);quotaTimer=undefined;quotaController?.abort();quotaController=null;quota=null;quotaError='';}
async function refreshQuota() {
  if(!session||!serverMode())return;
  const owner=session,controller=new AbortController();quotaController?.abort();quotaController=controller;
  window.clearTimeout(quotaTimer);
  try{
    const result=await api.quota(controller.signal);
    if(session!==owner||controller.signal.aborted)return;
    if(!result||!Number.isFinite(result.used_bytes)||!Number.isFinite(result.reserved_bytes)||!(result.limit_bytes===null||Number.isFinite(result.limit_bytes))||!(result.remaining_bytes===null||Number.isFinite(result.remaining_bytes)))throw new Error(t('容量数据暂不可用'));
    quota=result;quotaError='';
  }catch(e){if(session!==owner||controller.signal.aborted)return;quotaError=t('账号空间暂不可用');}
  if(session!==owner||controller.signal.aborted)return;
  paintQuota();quotaTimer=window.setTimeout(()=>void refreshQuota(),30000);
}
function fmtTime(mtime?:number){if(!mtime)return '';const d=new Date(mtime*1000),p=(v:number)=>String(v).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;}
function reset() {
  if(immersive)void fullscreen();
  resetPickerState();
  ++browseToken;++remoteRevision;pendingBrowse=null;pickerPathDraft=null;resetMediaErrors();sshManager.reset();
  stopVideo();forgetVideos();posters.clear();posterController.abort();posterObserver?.disconnect();posterObserver=null;
  stopQuota();stopShares();
  uploadJob?.controller.abort();uploadJob=null;
  viewer?.destroy();viewer=null;comparison=null;browse=null;selected=[];draftGroups=[];activeDraftGroup=-1;manualTarget='new';newGroupName='';groupSearch='';state=makeViewState() as ViewState;shareOwner='';
  shareConfig=null;shareEditable=false;restoreViewOnCreate=false;draftCombine='concat';draftImageOrder=[];pageViews={};pageViewsEnabled=false;defaultPageView={view:null,panels:[]};
  areas=[];rootNodes=[];
}
function loginForm():HTMLFormElement {
  const form=el('form');
  for(const [name,label,type] of [['username',t('用户名'),'text'],['password',t('密码'),'password']] as const) {
    const l=el('label',label),input=el('input');input.name=name;input.type=type;input.required=true;input.autocomplete=type==='password'?'current-password':'username';
    l.append(input);form.append(l);
  }
  const e=el('div','', 'login-error');e.hidden=true;form.append(e);
  const submit=el('button',t('登录'));submit.type='submit';form.append(submit);
  form.onsubmit=async event=>{
    event.preventDefault();submit.disabled=true;
    try {session=await api.login((form.elements.namedItem('username') as HTMLInputElement).value.trim(),(form.elements.namedItem('password') as HTMLInputElement).value);reset();await initialize();}
    catch(e){error(e);}finally{submit.disabled=false;}
  };
  return form;
}
function login() {
  gateMessage=null;
  app.replaceChildren();
  const main=el('main',undefined,'login'),subtitle=shareId?t('登录后打开分享的比较视图'):t('服务器本机目录 · 多路图片与帧序列');
  const controls=el('div',undefined,'login-controls');controls.append(languageControl(),themeControl());main.append(controls);
  if(providers?.mode==='server') {
    const card=el('div',undefined,'login-card');
    card.append(el('div','WC','login-mark'),el('h1','WebCompare'),el('p',subtitle));
    const form=loginForm();form.className='login-form';card.append(form);
    if(providers.guest)card.append(btn(t('访客临时体验'),guestIntro,'wide',t('创建一个临时工作区')));
    card.append(downloadLink(t('桌面版下载')));
    main.append(card);
  } else {
    const form=loginForm();form.className='login-card';
    form.prepend(el('div','WC','login-mark'),el('h1','WebCompare'),el('p',subtitle));
    main.append(form);
  }
  app.append(main);
}
let gateMessage:(()=>string)|null=null;
function gate(message:()=>string) {
  gateMessage=message;
  app.replaceChildren();
  const main=el('main',undefined,'login'),card=el('div',undefined,'login-card');
  const controls=el('div',undefined,'login-controls');controls.append(languageControl(),themeControl());main.append(controls);
  card.append(el('div','WC','login-mark'),el('h1','WebCompare'),el('p',message()));
  if(session) {
    if(session.user.role==='guest')card.append(btn(t('退出访客并登录'),()=>void exitGuest(),'wide',t('退出后临时工作区按保留时间清理')));
    else if(session.login_required) {
      const switchAccount=el('details');
      switchAccount.append(el('summary',t('切换本地账号')),loginForm());card.append(switchAccount);
    }
    card.append(btn(t('返回 WebCompare 首页'),()=>location.href='/','wide'));
  } else {
    const form=loginForm();form.className='login-form';card.append(form);
    card.append(btn(t('返回 WebCompare 首页'),()=>location.href='/','wide'));
  }
  if(serverMode())card.append(downloadLink(t('桌面版下载')));
  main.append(card);app.append(main);
}
function guestIntro() {
  const box=dialog(t('访客临时体验'));
  box.append(el('p',t('访客可以立即使用临时工作区，文件到期后自动清理。')));
  const list=el('ul',undefined,'guest-rules');
  for(const rule of [
    t('单个文件最多保留 24 小时，到期立即失效'),
    t('闲置 30 分钟未使用即清理'),
    t('点“退出并清理”后 10 分钟内清理全部内容'),
    t`同一时间最多 ${providers?.guest_max_workspaces??10} 个访客名额，满员时无法创建`,
    t('你生成的公开分享链接任何人都能查看，内容到期后链接失效'),
    t('访客工作区与本地账号分别保存内容'),
  ])list.append(el('li',rule));
  box.append(list);
  const row=el('div',undefined,'dialog-actions');
  row.append(btn(t('取消'),()=>document.querySelector('.dialog-backdrop')?.remove()),btn(t('同意并进入'),()=>void createGuest(),'primary'));
  box.append(row);
}
async function createGuest() {
  document.querySelector('.dialog-backdrop')?.remove();
  try {session=await api.guestCreate();reset();await initialize();}
  catch(e){error(e);}
}
async function exitGuest() {
  if(!session)return;
  try {await api.guestExit(session.csrf);}
  catch(e){const status=(e as Error & {status?:number}).status;if(status!==401&&status!==403){error(e);return;}}
  session=null;reset();login();
}
function guestBanner():HTMLElement|null {
  const workspace=session?.workspace;
  if(session?.user.role!=='guest'||!workspace)return null;
  const bar=el('div',undefined,'guest-banner');
  bar.append(el('span',t`访客临时空间 · 单个文件保留 ${humanTtl(workspace.content_ttl_seconds)} · 闲置 ${humanTtl(workspace.idle_ttl_seconds)} 自动清理 · 退出后 ${humanTtl(workspace.exit_ttl_seconds)} 清理 · 公开分享链接任何人都能查看`),
    btn(t('退出并清理'),()=>void exitGuest(),'danger'));
  return bar;
}
async function initialize() {
  if(!session&&!shareId)return;
  if(shareId)return openShare();
  const owner=session!,target=readNavigation(location.pathname,location.search,history.state,{mode:owner.mode,owner:owner.user.id});
  if(target)mediaKind=target.media as 'image'|'video';
  view='pick';
  shell();
  try {
    if(canBrowse())await preparePicker(target?.directory,!!target?.comparison);
    if(session!==owner)return;
    if(target?.comparison){
      const result=await api.comparison(target.comparison);
      if(session!==owner)return;
      const restoredMedia=result.media_kind||'image';
      if(restoredMedia!==mediaKind){mediaKind=restoredMedia;if(canBrowse()&&browse)await browseAt(browse.path,0,false);if(session!==owner)return;shell();}
      selected=result.sources.map(source=>({...source,kind:source.kind||sourceKind(source.path)}));
      recursive=result.selection?.recursive??true;draftCombine=result.selection?.combine||'concat';
      draftGroups=structuredClone(result.selection?.groups||[]);activeDraftGroup=draftGroups.length?0:-1;
      acceptComparison(result,(result.groups?.length?groupLayout(result.groups.length):smartLayout(result.images.length,draftCombine==='interleave'?selected.length:0)) as ViewState['layout']);
    }
    render();
  } catch(e){render();error(e);}
}
function saveNavigation() {
  if(!session||shareId)return;
  const target=navigationTarget({directory:browse?.path,comparison:view==='compare'?comparison?.id:undefined,media:mediaKind,mode:session.mode,owner:session.user.id,key:Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)});
  history.replaceState(target.state,'',target.url);
}
async function preparePicker(path?:string,preserveLocation=false) {
  const info=await api.roots();shortcuts=info.shortcuts||[];areas=info.areas||[];home=info.home||areas[0]?.path||'/';
  if(session?.mode==='personal')void sshManager.load();
  if(!localStorage.getItem('wc-bookmarks-seeded')&&shortcuts.length){bookmarks=shortcuts.map(r=>({path:r.path,label:r.label}));saveBookmarks();localStorage.setItem('wc-bookmarks-seeded','1');}
  tree=treeRoots();
  rootNodes=areas.map(area=>({path:area.path,name:area.label,open:false,loaded:false,children:[],area}));
  await browseAt(path||home,0,!preserveLocation);
  if(path&&!browse&&path!==home)await browseAt(home,0,!preserveLocation);
}
function rememberPageView() {
  if(!comparison||serverMode())return;
  pageViews[String(state.group_index)]=structuredClone({view:state.view,panels:state.panels});
}
function pageState(next:ViewState):ViewState {
  const saved=pageViews[String(next.group_index)];
  return normalizeViewState({...next,...defaultPageView,...saved},cellsOf(next.layout),pageCount(comparison,next.layout,next)) as ViewState;
}
function sourceKind(path:string):Source['kind'] {
  if(/\.(png|jpe?g|bayer|raw|npy)$/i.test(path))return 'image';
  if(/\.(mp4|mov|m4v|webm|mkv)$/i.test(path))return 'video';
  return 'directory';
}
function parentDirectory(path:string) {
  return sourceParent(path);
}
async function returnToSelection() {
  if(!canEditShare())return;
  rememberPageView();
  const selection=shareConfig!.comparison;
  mediaKind=selection.media_kind||'image';recursive=selection.recursive;draftCombine=selection.combine;
  draftImageOrder=[...(selection.image_order||[])];
  selected=selection.sources.map(source=>({path:source.path,label:source.label||sourceName(source.path),kind:sourceKind(source.path)}));
  draftGroups=structuredClone(selection.groups||[]);activeDraftGroup=draftGroups.length?0:-1;manualTarget=activeDraftGroup>=0?String(activeDraftGroup):'new';newGroupName='';groupSearch='';
  const path=draftGroups[0]?.rules[0]?.path||selected[0]?.path;
  const directory=path&&sourceKind(path)!=='directory'?parentDirectory(path):path;
  restoreViewOnCreate=true;shareConfig=null;shareEditable=false;shareOwner='';shareId=undefined;
  history.replaceState(null,'','/');
  stopVideo();viewer?.destroy();viewer=null;comparison=null;browse=null;pendingBrowse=null;++browseToken;view='pick';
  shell();render();
  try{await preparePicker(directory);}catch(e){error(e);}
}
async function openShare() {
  try {
    const share=await api.getShare(shareId!);comparison=share.comparison;mediaKind=comparison.media_kind||'image';
    const layout=share.state.grouping!=='within'&&comparison.groups?.length&&cellsOf(share.state.layout)<comparison.groups.length?groupLayout(comparison.groups.length):share.state.layout;
    state=normalizeViewState({...share.state,layout},cellsOf(layout),pageCount(comparison,layout,share.state)) as ViewState;
    shareConfig=share.config||null;shareEditable=share.editable===true&&share.selection_mode==='editable';
    pageViews=structuredClone(shareConfig?.page_views??share.page_views??{});pageViewsEnabled=Object.keys(pageViews).length>0;
    defaultPageView=structuredClone(shareConfig?{view:shareConfig.state.view,panels:shareConfig.state.panels}:share.default_page_view??{view:state.view,panels:state.panels});
    if(pageViewsEnabled)state=pageState(state);
    selected=comparison.sources.map(s=>({...s}));shareOwner=share.owner_display_name;
    view='compare';shell();render();
    if(share.selection_error)error(serverMessage(share.selection_error,t('无法恢复选图。')));
  } catch(e) {
    const status=(e as Error & {status?:number}).status;
    if(!session||status===401||status===403){gate(()=>status===403?t('你对这个分享没有访问权限。'):t('这个分享需要登录后才能查看。'));return;}
    shell();render();error(e);
  }
}
function shell(preserveViewer=false) {
  configureLocalDecoder(session?.local_nvdec && decoderChoice==='auto'?session.csrf:undefined);
  if(view!=='compare'&&immersive)void fullscreen();
  if(!preserveViewer){viewer?.destroy();viewer=null;}
  app.replaceChildren();
  const root=el('div',undefined,'app view-'+view),top=el('header',undefined,'topbar');
  if(providers?.native_desktop)top.classList.add('desktop-topbar');
  else {
    const brand=el('div',undefined,'brand');brand.append(el('span',undefined,'brand-mark'),el('span','WebCompare'));
    top.append(brand);
  }
  // 页面操作追加在媒体模式右侧，切页时保持模式开关的位置。
  const mediaSwitch=segmented(t('媒体模式'),[['image',t('图像模式')],['video',t('视频模式')]],mediaKind,value=>void switchMedia(value as 'image'|'video'));mediaSwitch.classList.add('media-toggle');top.append(mediaSwitch);
  if(mediaKind==='video')top.append(decoderControl());
  if(shareId)mediaSwitch.querySelectorAll('input').forEach(input=>input.disabled=true);
  if(canBrowse()||canEditShare()) {
    const toggle=btn('',()=>{if(shareId)void returnToSelection();else setView(view==='pick'?'compare':'pick');},'view-toggle');
    const paint=()=>{toggle.textContent=view==='pick'?t('返回对比 →'):t('‹ 返回选图');toggle.style.display=view==='pick'&&!comparison?'none':'';};
    toggle.dataset.paint='1';paint();top.append(toggle);
  }
  top.append(el('div',undefined,'selection-actions'));
  if(shareId)top.append(el('span',t('分享模板')+(shareOwner?' · '+shareOwner:''),'shared-badge'));
  if(session)top.append(el('span',accountName(session)+' · '+session.mode,'account'));
  if(session)top.append(btn(t('我的分享'),()=>showMine()));
  if(session?.mode==='personal'&&['member','admin'].includes(session.user.role)){
    const sshAccess=btn(t('SSH 连接'),()=>sshManager.showManage(),'ssh-manage',t('管理远端文件连接；断线后重新连接并恢复当前比较'));sshAccess.hidden=view==='pick';top.append(sshAccess);
  }
  if(session&&serverMode())top.append(el('div',undefined,'account-quota'));
  const settingsButton=btn(t('设置'),()=>void settings());
  if(providers?.native_desktop)settingsButton.dataset.updateSettings='';
  top.append(settingsButton);
  top.append(themeControl(),btn(t('全屏'),()=>void fullscreen(),'fullscreen-toggle',t('全屏查看（Shift+F）')),guideLink());
  if(serverMode())top.append(downloadLink());
  // 默认个人会话无需退出。旧 viewer Cookie 仍保留只读权限，并提供切回个人工作台的入口，
  // 避免升级后因既不能选图又无法退出而卡住；旧后端及开启登录的实例保留原退出流程。
  const enterPersonal=session?.login_required===false&&session.user.role==='viewer';
  if(session&&(session.login_required!==false||enterPersonal)&&session.user.role!=='guest')top.append(btn(enterPersonal?t('进入个人工作台'):t('退出'),async()=>{
    try {await api.logout(session!.csrf);}catch(e){error(e);return;}
    session=null;reset();if(enterPersonal)location.href='/';else login();
  }));
  const work=el('div',undefined,'workspace');
  const pickerBox=el('div',undefined,'picker');
  const main=el('main',undefined,'main');
  const railGutter=el('div',undefined,'gutter gutter-rail');railGutter.title=t('拖动调整预览栏宽度，拖到最左收起');
  hDrag(railGutter,()=>railW,w=>{railW=w;localStorage.setItem('wc-railw',String(w));applyRailW();groups();});
  const bottom=el('div',undefined,'toolbar-bottom'),statusbar=el('div',undefined,'statusbar');bottom.append(el('div',undefined,'groups'),statusbar);
  main.append(el('div',undefined,'toolbar'),el('div',undefined,'lane-rail'),railGutter,el('div',undefined,'canvas-wrap'),bottom);
  statusbar.append(el('span',t('双击选中 · 滚轮缩放 · 拖动平移 · Alt+单击读取像素'),'coordinate-status'),el('span',t('自适应 · 本地无损，跨设备或 SSH 高清'),'quality-status'));
  const notice=el('div','', 'notice-error');notice.hidden=true;notice.setAttribute('role','alert');notice.setAttribute('aria-live','assertive');notice.setAttribute('aria-atomic','true');
  const infoBar=el('div','','notice-info');infoBar.hidden=true;infoBar.setAttribute('role','status');infoBar.setAttribute('aria-live','polite');infoBar.setAttribute('aria-atomic','true');
  const notices=el('div',undefined,'notice-stack');notices.setAttribute('role','region');notices.setAttribute('aria-label',t('通知'));notices.append(notice,infoBar);
  work.append(pickerBox,main);
  root.append(top);
  const banner=guestBanner();
  if(banner)root.append(banner);
  root.append(notices,work);
  app.append(root);
  applyRailW();
  paintQuota();void refreshQuota();
  uploadPanel();
  viewer??=new Viewer(update,status,session?.profile||'cloud',()=>{
    // 视频每帧解码完成也会通知这里；保留控制条节点，避免按下与松开之间按钮被移除。
    if(mediaKind==='video'){
      if(playing&&viewer?.videoMemoryBlocked)stopVideo();
      if(!refreshVideoControls())toolbar();rail();paintQualityStatus();
    }else toolbar();
  },()=>{refreshRawMetadata();if(view==='compare')rail();},session?.local_images===true,personalMode(),stopVideo);main.querySelector('.canvas-wrap')!.append(viewer.canvas);
  applyOverlays();paintImmersive();
  (window as unknown as {__viewer:Viewer}).__viewer=viewer;  // QA/调试探针
  if(providers?.native_desktop)watchDesktopUpdates();
}
function setView(v:'pick'|'compare') {
  if(v==='pick'&&immersive)void fullscreen();
  view=v;
  if(v==='compare'&&document.fullscreenElement)immersive=true;
  paintImmersive();
  if(v==='pick'){viewer?.suspend();stopVideo();}
  document.querySelector('.app')?.classList.toggle('view-pick',v==='pick');
  document.querySelector('.app')?.classList.toggle('view-compare',v==='compare');
  const toggle=document.querySelector<HTMLButtonElement>('.view-toggle');
  if(toggle){toggle.textContent=v==='pick'?t('返回对比 →'):t('‹ 返回选图');toggle.style.display=v==='pick'&&!comparison?'none':'';}
  const sshAccess=document.querySelector<HTMLButtonElement>('.topbar .ssh-manage');if(sshAccess)sshAccess.hidden=v==='pick';
  if(v==='compare'){
    // Hiding the picker does not cancel its pending image requests.
    document.querySelectorAll<HTMLImageElement>('.thumbgrid img').forEach(img=>img.removeAttribute('src'));
    posterController.abort();posterObserver?.disconnect();posterObserver=null;render();
  }
  else picker();
  saveNavigation();
}
async function switchMedia(next:'image'|'video') {
  if(next===mediaKind)return;
  if(busy||uploadActive()||pickerCopy){error(t('请先完成当前文件操作，或取消上传'));document.querySelector<HTMLInputElement>(`.media-toggle input[value="${mediaKind}"]`)!.checked=true;return;}
  resetPickerState();stopVideo();const path=browse?.path||home;mediaKind=next;
  viewer?.destroy();viewer=null;comparison=null;selected=[];draftGroups=[];state=makeViewState() as ViewState;
  shareConfig=null;shareEditable=false;restoreViewOnCreate=false;draftCombine='concat';draftImageOrder=[];pageViews={};pageViewsEnabled=false;defaultPageView={view:null,panels:[]};
  browse=null;view='pick';shareId=undefined;++browseToken;history.replaceState(null,'','/');
  shell();render();if(path)await browseAt(path);
}
function stopVideo() { playing=false;viewer?.setVideoPlaying(false);++playGeneration;clearTimeout(playTimer);refreshVideoControls(); }
function frameTarget(index:number) {
  const selected=viewer?.selectedIndex,info=viewer?.videoInfo()||[];
  if(selected!==null&&selected!==undefined&&info[selected]) {
    const item=info[selected]!,video_frame=state.video_frame??0;
    const offset=Math.max(0,Math.min(item.count-1,index))-video_frame;
    update({video_frame,video_offsets:{...state.video_offsets,[item.key]:offset}});
  }else update({video_frame:Math.max(videoStart(),Math.min(videoLimit()-1,index))});
}
function videoStart() {return Math.max(0,...(viewer?.videoInfo()||[]).filter(Boolean).map(item=>-item!.offset));}
function videoLimit() {
  const items=(viewer?.videoInfo()||[]).filter(Boolean);
  return Math.max(1,Math.min(...items.map(item=>item!.count-item!.offset)));
}
function stepFrame(delta:number) {
  const selected=viewer?.selectedIndex,info=viewer?.videoInfo()||[];
  if(viewer?.videoMemoryBlocked&&info.some(item=>item&&!item.count)){update({video_frame:state.video_frame||0});return;}
  frameTarget((selected!==null&&selected!==undefined?info[selected]?.frame||0:state.video_frame||0)+delta);
}
async function playNext(generation=playGeneration,deadline=performance.now()) {
  if(!playing||!viewer)return;
  await viewer.settled();if(!playing||!viewer||generation!==playGeneration)return;
  if(viewer.videoMemoryBlocked){stopVideo();return;}
  if((state.video_frame||0)>=videoLimit()-1){if(loopVideo){update({video_frame:videoStart()});playTimer=window.setTimeout(()=>void playNext(generation),100);return;}else{stopVideo();return;}}
  const nextDeadline=Math.max(performance.now(),deadline+Math.max(16,viewer.videoDelay()/playSpeed));
  update({video_frame:(state.video_frame||0)+1});
  playTimer=window.setTimeout(()=>void playNext(generation,nextDeadline),Math.max(0,nextDeadline-performance.now()));
}
function videoControlState() {
  const ids=slotAssets(comparison,state) as (string|null)[];
  const selected=viewer?.selectedIndex??null,info=viewer?.videoInfo()||[];
  const item=(selected===null?null:info[selected])||null;
  // 控件跟随目标帧；解码异步返回上一帧时，不应把用户正在输入的帧号回拨。
  const count=item?.count||videoLimit(),current=item
    ?Math.max(0,Math.min(count-1,item.frame))
    :state.video_frame||0;
  return {ids,selected,item,count,current,key:JSON.stringify(ids)};
}
/**
 * 播放推进、解码完成与手动移帧都在原位同步；同一选格不移除任何控制节点。
 * 这样 pointerdown/up 能落在同一个按钮，输入焦点与范围滑块的指针捕获也不会丢失。
 * 可见视频列表改变时才重建目标列表。
 */
function refreshVideoControls(row=document.querySelector<HTMLElement>('.video-controls')):boolean {
  if(mediaKind!=='video'||view!=='compare')return false;
  if(!row)return false;
  const {selected,item,count,current,key}=videoControlState();if(row.dataset.context!==key)return false;
  const scope=row.querySelector<HTMLSelectElement>('.video-frame-target')!,value=selected===null?'':String(selected);
  if(scope.value!==value)scope.value=value;
  const play=row.querySelector<HTMLButtonElement>('.video-play')!,caption=playing?t('暂停'):t('播放');
  if(play.textContent!==caption)play.textContent=caption;
  play.title=playing?t('暂停全部视频'):t('播放全部视频，保留各路偏移');
  const frame=row.querySelector<HTMLInputElement>('.video-frame-index')!;
  frame.min=String(item?1:videoStart()+1);frame.max=String(count);
  if(document.activeElement!==frame)frame.value=String(current+1);
  const total=row.querySelector<HTMLElement>('.video-count')!,text=`/ ${count||'…'}`;
  if(total.textContent!==text)total.textContent=text;
  const seek=row.querySelector<HTMLInputElement>('.video-frame-progress')!;
  seek.min=String(item?0:videoStart());seek.max=String(Math.max(0,count-1));
  if(document.activeElement!==seek)seek.value=String(current);
  const offset=row.querySelector<HTMLInputElement>('.video-frame-offset')!;
  offset.parentElement!.hidden=!item;offset.disabled=!item?.count;
  if(item&&document.activeElement!==offset)offset.value=String(item.offset);
  return true;
}
function videoControls() {
  const row=el('div',undefined,'tool-row video-controls'),{ids,key}=videoControlState();
  row.dataset.context=key;
  row.setAttribute('aria-label',t('视频逐帧控制'));
  const scope=el('select',undefined,'video-frame-target');scope.setAttribute('aria-label',t('操作范围'));
  scope.title=t('选择要移帧的视频，也可双击画格选路');
  const all=el('option',t('全部联动'));all.value='';scope.append(all);
  ids.forEach((id,index)=>{
    if(!id)return;
    const name=comparison?.assets[id]?.name;
    const option=el('option',name?t`第 ${index+1} 路 · ${name}`:t`第 ${index+1} 路`);
    option.value=String(index);scope.append(option);
  });
  scope.onchange=()=>viewer?.select(scope.value===''?null:Number(scope.value));
  row.append(labelled(t('帧操作'),scope));
  const play=btn(playing?t('暂停'):t('播放'),()=>{
    if(playing)stopVideo();
    else {
      viewer?.select(null);playing=true;viewer?.setVideoPlaying(true);
      if(viewer?.videoMemoryBlocked)update({video_frame:state.video_frame||0});
      void playNext();
    }
    refreshVideoControls();
  },'video-play');
  row.append(play,btn(t('上一帧'),()=>{stopVideo();stepFrame(-1);}));
  const frame=el('input',undefined,'video-frame-index');frame.type='number';frame.setAttribute('aria-label',t('视频帧号'));frame.onchange=()=>{
    // 失焦提交时先保存用户值；暂停同步控件可能写回当前播放帧，不能覆盖这次目标。
    const target=Number(frame.value)-1;stopVideo();frameTarget(target);frame.value=String(videoControlState().current+1);
  };
  row.append(frame,el('span',undefined,'video-count'),btn(t('下一帧'),()=>{stopVideo();stepFrame(1);}));
  const seek=el('input',undefined,'video-frame-progress');seek.type='range';seek.setAttribute('aria-label',t('视频帧进度'));seek.onchange=()=>{const target=Number(seek.value);stopVideo();frameTarget(target);seek.value=String(videoControlState().current);};row.append(seek);
  const offset=el('input',undefined,'video-frame-offset');offset.type='number';offset.setAttribute('aria-label',t('当前视频帧偏移'));offset.onchange=()=>{const target=(state.video_frame||0)+Number(offset.value);stopVideo();frameTarget(target);offset.value=String(videoControlState().item?.offset??0);};row.append(labelled(t('偏移帧'),offset));
  const speed=el('select');speed.setAttribute('aria-label',t('视频播放速度'));for(const rate of [.25,.5,1,2]){const option=el('option',rate+'×');option.value=String(rate);option.selected=rate===playSpeed;speed.append(option);}speed.onchange=()=>{playSpeed=Number(speed.value);};
  const loop=el('input');loop.type='checkbox';loop.checked=loopVideo;loop.onchange=()=>{loopVideo=loop.checked;};row.append(labelled(t('速度'),speed),labelled(t('循环'),loop));
  row.append(btn(t('帧对齐归零'),()=>{stopVideo();update({video_frame:0,video_offsets:{}});}));
  refreshVideoControls(row);
  return row;
}
function render(updateViewer=true) {
  if(view==='pick')picker();
  rail();toolbar();groups();
  if(!updateViewer)viewer?.refreshLanguage();
  else if(view==='compare')viewer?.update(comparison,state,shareId);else viewer?.suspend();
  paintQualityStatus();
}
function removeComparisonSlot(slot:number) {
  if(!comparison||railPending?.comparison===comparison)return;
  const next=removeSlot(comparison,state,slot) as Comparison;if(next===comparison)return;
  stopVideo();comparison=next;temporaryComparisons.add(next);
  pageViews={};defaultPageView={view:state.view,panels:[]};
  state=normalizeViewState({...state,panels:[],diff_base:0},cellsOf(state.layout),pageCount(next,state.layout,state)) as ViewState;
  render();
}
function removalButtons() {
  const wrap=document.querySelector('.canvas-wrap');if(!wrap)return;
  let layer=wrap.querySelector<HTMLElement>('.canvas-removals');
  if(!layer){layer=el('div',undefined,'canvas-removals');wrap.append(layer);}
  layer.replaceChildren();if(view!=='compare'||!comparison)return;
  const {rows,columns}=layoutShape(state.layout);
  layer.style.gridTemplateColumns=`repeat(${columns},minmax(0,1fr))`;
  layer.style.gridTemplateRows=`repeat(${rows},minmax(0,1fr))`;
  const methods=comparison.groups?.length&&state.grouping!=='within'?orderedGroupIndices(comparison,state):null;
  slotAssets(comparison,state).forEach((id:string|null,slot:number)=>{
    if(!id&&!methods)return;
    const method=methods?comparison!.groups![methods[slot]]:null;
    const button=btn('×',()=>removeComparisonSlot(slot),'compare-remove',method?t('从本次比较移除整路方法，其余方法前移；刷新页面恢复'):t('仅从本次比较移除，刷新页面恢复'));
    button.setAttribute('aria-label',method?t`从本次比较移除方法：${method.label}`:t`从本次比较移除第 ${slot+1} 张：${comparison!.assets[id!]?.name||id}`);
    button.style.gridColumn=String(slot%columns+1);button.style.gridRow=String(Math.floor(slot/columns)+1);
    button.disabled=railPending?.comparison===comparison;layer!.append(button);
  });
}
function rawAsset(id:string) { const asset=comparison?.assets[id];return isRawPath(asset?.path)||isRawPath(asset?.name); }
function currentPageHasRaw() { return mediaKind==='image'&&slotAssets(comparison,state).some((id:string|null)=>id&&rawAsset(id)); }
function paintQualityStatus() {
  const quality=document.querySelector('.quality-status');if(!quality)return;
  if(mediaKind==='video'){
    refreshDecoderControls();
    quality.textContent=t('按原片时间戳对齐 · RGB 显示值');
    return;
  }
  const choices=new Set(slotAssets(comparison,state).filter(Boolean).map((id:string)=>imageDisplayQuality(state.quality,session?.local_images===true,comparison?.assets[id]?.path)));
  const description=choices.size>1?t('本地无损 · SSH 高清'):choices.has('exact')?t('无损 · 本地缩放和平移'):t('完整分辨率 · 高清压缩 · 本地缩放和平移');
  quality.textContent=(currentPageHasRaw()?t('RAW 显示 RGB · '):'')+(state.quality==='auto'?t('自适应 · '):'')+description;
}
function update(patch:Partial<ViewState>) {
  if(mediaKind==='video'&&['group_index','layout','grouping','natural_sort','transposed'].some(key=>key in patch))stopVideo();
  const previous=state.group_index;
  const patched={...state,...patch};
  if(patch.layout&&patch.layout!==state.layout&&!comparison?.groups?.length){ // 普通清单换布局保持当前第一张图
    const first=pageInfo(comparison,state.layout,state.group_index,state).start;
    patched.group_index=Math.floor(first/cellsOf(patch.layout));
  }else if(patch.layout&&patch.layout!==state.layout&&state.grouping==='within'&&comparison?.groups?.length){
    const page=pageInfo(comparison,state.layout,state.group_index,state);let offset=0;
    for(const index of orderedGroupIndices(comparison,patched)){if(index===page.groupIndex)break;offset+=Math.max(1,Math.ceil(comparison.groups[index].images.length/cellsOf(patch.layout)));}
    patched.group_index=offset+Math.floor(page.start/cellsOf(patch.layout));
  }
  if(patched.grouping!=='within'&&comparison?.groups?.length&&cellsOf(patched.layout)<comparison.groups.length)patched.layout=groupLayout(comparison.groups.length) as ViewState['layout'];
  const matrixChanged=['grouping','natural_sort','layout','transposed'].some(k=>patched[k as keyof ViewState]!==state[k as keyof ViewState]);
  const pageChanged=patched.group_index!==previous;
  if(pageChanged||matrixChanged)rememberPageView();
  if(matrixChanged)pageViews={};
  if(pageChanged||matrixChanged)patched.panels=[];
  state=normalizeViewState(patched,cellsOf(patched.layout),pageCount(comparison,patched.layout,patched)) as ViewState;
  if(pageViewsEnabled&&!matrixChanged&&state.group_index!==previous)state=pageState(state);
  if(view==='compare')viewer?.update(comparison,state,shareId);
  if(previous!==state.group_index||'grouping' in patch||'natural_sort' in patch||'layout' in patch||'transposed' in patch){groups();rail();}
  // 时间推进不改变工具结构；每帧重建会吞掉真实暂停点击，也会打断帧号输入与进度拖动。
  const frameOnly=mediaKind==='video'&&Object.keys(patch).every(key=>key==='video_frame'||key==='video_offsets');
  if(frameOnly){if(!refreshVideoControls())toolbar();}
  else if(Object.keys(patch).some(k=>!['view','panels','opacity','wipe','gain','exposure','gamma'].includes(k))){toolbar();rail();}
  paintQualityStatus();
  // 滑块与数值输出同步（画布拖线等不重建工具栏的变更也要反映到数值上）
  document.querySelectorAll<HTMLInputElement>('.range-wrap input[data-state-key]').forEach(input=>{
    const k=input.dataset.stateKey as 'exposure'|'gamma'|'gain'|'opacity'|'wipe';
    const v=state[k];if(v===undefined)return;
    if(document.activeElement!==input)input.value=String(v);
    const out=input.parentElement?.querySelector('output');if(out)out.textContent=String(v);
  });
}
/** 序列转置只切换整段媒体的显示遍历顺序，布局和选取清单不参与修改。 */
function transposeComparisonView() {
  if (!comparison || comparison.groups?.length || sourceLanes(comparison).filter((lane:number[])=>lane.length).length < 2) return;
  update({transposed:!state.transposed,diff_base:0});
}
// 锚点只属于当前目录、筛选、排序和分页；历史则保留跨目录选择，但不保存文件实体操作。
const selectionHistory=createSelectionHistory(60);
let pickerAnchor:string|null=null,pickerContext='',pickerOwner:Session|null=null,pickerMedia:'image'|'video'=mediaKind,pickerSelection:Source[]|null=null;
type PickerDownload={owner:Session;media:'image'|'video';controller:AbortController;record:ExportRecord|null;message:()=>string;done:boolean;timer?:number};
let pickerDownload:PickerDownload|null=null;
type PickerCopy={owner:Session;media:'image'|'video';destination:string;token:number;controller:AbortController;notice:number};
let pickerCopy:PickerCopy|null=null,pickerCopyNotice=0;
type FileClipboard=ReturnType<typeof encodeFileCopy>['descriptor'];
let pickerClipboard:FileClipboard|null=null;
function resetPickerState() {
  pickerDownload?.controller.abort();window.clearTimeout(pickerDownload?.timer);pickerDownload=null;
  pickerCopy?.controller.abort();pickerCopy=null;++pickerCopyNotice;
  pickerClipboard=null;
  selectionHistory.reset();pickerAnchor=null;pickerContext='';pickerOwner=session;pickerMedia=mediaKind;pickerSelection=selected;
}
function syncPickerState() {
  if(pickerOwner!==session||pickerMedia!==mediaKind)resetPickerState();
  // 模板恢复等外部入口可以替换 selected；不能把上一份选择历史带到新模板。
  if(pickerSelection!==selected){selectionHistory.reset();pickerAnchor=null;pickerSelection=selected;}
  const context=JSON.stringify([mediaKind,browse?.path,browseOffset,filter,sort,order,browse?.entries.map(entry=>entry.path)]);
  if(context!==pickerContext){pickerContext=context;pickerAnchor=null;}
}
function selectionLimit(){error(t`一次最多手选 ${MAX_SELECTION} 项，请先添加到组`);}
function focusPicker(){document.querySelector<HTMLElement>('.picker')?.focus({preventScroll:true});}
function changeSelection(next:Source[]) {
  selectionHistory.record(selected,next);pickerSelection=selected=next;picker();focusPicker();
}
function restoreSelection(redo=false) {
  const next=redo?selectionHistory.redo(selected):selectionHistory.undo(selected);
  if(!next)return;pickerSelection=selected=next;pickerAnchor=null;picker();focusPicker();
}
function chooseEntry(entry:Entry,event?:Pick<MouseEvent,'shiftKey'|'ctrlKey'|'metaKey'>) {
  if(!browse||pendingBrowse!==null)return;
  syncPickerState();
  const result=selectPickerEntry(selected,browse.entries,entry.path,pickerAnchor,{shift:event?.shiftKey,additive:!!(event?.ctrlKey||event?.metaKey),limit:MAX_SELECTION});
  if(result.limited){selectionLimit();return;}
  pickerAnchor=result.anchor;changeSelection(result.selection);
}
function chooseVisible(remove=false,toggle=false) {
  if(!browse||pendingBrowse!==null||filterDraft.trim()!==filter)return;
  const result=toggle?toggleVisibleEntries(selected,browse.entries,MAX_SELECTION):selectVisibleEntries(selected,browse.entries,remove,MAX_SELECTION);
  if(result.limited){selectionLimit();return;}
  pickerAnchor=null;changeSelection(result.selection);
}
function pickerEventAllowed(event:Event) {
  if(view!=='pick'||!canBrowse()||document.querySelector('.dialog-backdrop,dialog[open],[aria-modal="true"]'))return false;
  const target=event.target instanceof Element?event.target:document.activeElement;
  // 浏览器文本编辑、输入法和弹窗保留自己的复制、粘贴及撤销行为。
  if(target?.closest('input,select,textarea')||target instanceof HTMLElement&&target.isContentEditable)return false;
  const root=document.querySelector('.picker');
  if(!root)return false;
  if(target&&root.contains(target))return true;
  return (target===document.body||target===document.documentElement)&&
    (document.activeElement===document.body||document.activeElement===document.documentElement);
}
function handlePickerKeydown(event:KeyboardEvent) {
  if(event.defaultPrevented||event.isComposing||!pickerEventAllowed(event))return;
  syncPickerState();
  const key=event.key.toLowerCase(),command=(event.ctrlKey||event.metaKey)&&!event.altKey;
  if(command&&key==='a'&&!event.shiftKey){event.preventDefault();if(!event.repeat)chooseVisible(false,true);}
  else if(command&&key==='z'){event.preventDefault();restoreSelection(event.shiftKey);}
  else if(command&&key==='y'&&!event.shiftKey){event.preventDefault();restoreSelection(true);}
  else if(!event.ctrlKey&&!event.metaKey&&((key==='backspace'&&!event.altKey)||(key==='arrowup'&&event.altKey))){
    event.preventDefault();if(browse?.parent!=null&&pendingBrowse===null)void browseAt(browse.parent);
  }else if(key==='escape'&&!event.ctrlKey&&!event.metaKey&&!event.altKey){
    if(selected.length){event.preventDefault();pickerAnchor=null;changeSelection([]);}
  }else if(key==='f2'&&!event.ctrlKey&&!event.metaKey&&!event.altKey&&selected.length===1&&pendingBrowse===null){
    const entry=browse?.entries.find(entry=>entry.path===selected[0].path);
    if(entry&&(entry.writable===true||entry.writable===undefined&&writableHere(browse?.path)&&session?.user.role==='admin')){event.preventDefault();void renameEntry(entry);}
  }
}
function copyNotice(message:string) {++pickerCopyNotice;info(message);}
function canCopyDestination(path:string|null|undefined):boolean {
  return !!path&&!!session&&session.user.role!=='guest'&&!sshLocation(path)&&(!serverMode()||writableHere(path));
}
function canMoveSelection(sources:Pick<Source,'path'>[]=selected):boolean {
  return !!session&&serverMode()&&['member','admin'].includes(session.user.role)&&sources.length>0&&sources.every(source=>!sshLocation(source.path)&&writableHere(source.path)&&browse?.entries.find(entry=>entry.path===source.path)?.writable!==false);
}
function paintPickerClipboard() {
  const blocked=busy||uploadActive()||pendingBrowse!==null||!!pickerCopy;
  const copy=document.querySelector<HTMLButtonElement>('.picker-file-copy');if(copy)copy.disabled=blocked||!selected.length;
  const cut=document.querySelector<HTMLButtonElement>('.picker-file-cut');if(cut)cut.disabled=blocked||!canMoveSelection();
  const move=document.querySelector<HTMLButtonElement>('.picker-file-move');if(move)move.disabled=blocked||!canMoveSelection();
  const paste=document.querySelector<HTMLButtonElement>('.picker-file-paste');
  if(paste)paste.disabled=blocked||!pickerClipboard||!canCopyDestination(browse?.path)||(pickerClipboard.operation==='move'&&!canMoveSelection(pickerClipboard.sources));
  if(paste)paste.title=pickerClipboard?t`粘贴 ${pickerClipboard.sources.length} 项到当前目录（Ctrl/Cmd+V）`:t('粘贴到当前目录（Ctrl/Cmd+V）');
}
function selectedFileClipboard(operation:'copy'|'move') {
  if(!session||!selected.length)throw new Error(t('请先选择文件或文件夹。'));
  if(operation==='move'&&!canMoveSelection())throw new Error(t('只能移动当前账号可写的本机文件或文件夹。'));
  return encodeFileCopy(selected,{origin:location.origin,owner:session.user.id,media_kind:mediaKind,recursive,operation});
}
async function copySelectedFiles(operation:'copy'|'move') {
  try{
    const encoded=selectedFileClipboard(operation),owner=session;pickerClipboard=encoded.descriptor;paintPickerClipboard();
    let system=false;
    try{if(navigator.clipboard){await navigator.clipboard.writeText(encoded.text);system=true;}}catch{/* HTTP 内网或浏览器拒绝权限时，显式粘贴按钮仍使用本站内清单。 */}
    if(session!==owner||pickerClipboard!==encoded.descriptor)return;
    copyNotice(t`已${operation==='move'?t('剪切'):t('复制')} ${encoded.descriptor.sources.length} 项文件引用；到目标目录点击“粘贴”${system?t('或按 Ctrl/Cmd+V'):''}。${operation==='move'?t('粘贴成功前原文件保留。'):''}`);
  }catch(cause){error(cause);}
}
function handlePickerCopy(event:ClipboardEvent,operation:'copy'|'move'='copy') {
  if(event.defaultPrevented||!pickerEventAllowed(event)||!event.clipboardData||!session||!selected.length||pendingBrowse!==null)return;
  try{
    const encoded=selectedFileClipboard(operation);
    event.clipboardData.setData('text/plain',encoded.text);
    try{event.clipboardData.setData(FILE_COPY_MIME,encoded.json);}catch{/* 部分浏览器只保留标准文本格式，仍可在本站其他标签粘贴。 */}
    event.preventDefault();
    pickerClipboard=encoded.descriptor;paintPickerClipboard();
    // 这里只更新提示，不能重建 picker：重建会重新观察缩略图并意外发起网络读取。
    copyNotice(t`已${operation==='move'?t('剪切'):t('复制')} ${encoded.descriptor.sources.length} 项文件引用；到目标目录按 Ctrl/Cmd+V 或点击粘贴。${operation==='move'?t('粘贴成功前原文件保留。'):''}`);
  }catch(cause){event.preventDefault();error(cause);}
}
function removeMovedSelection(paths:string[]) {
  pickerSelection=selected=selected.filter(source=>!paths.some(path=>source.path===path||source.path.startsWith(path+'/')));
  selectionHistory.reset();pickerAnchor=null;
}
async function pasteCopiedFiles(descriptor:NonNullable<ReturnType<typeof readFileCopy>>,destination=browse?.path):Promise<boolean> {
  const moving=descriptor.operation==='move',verb=moving?t('移动'):t('复制');
  if(!session||!destination||!canCopyDestination(destination)){error(sshLocation(destination)?t('SSH 目录保持只读，请打开本机目标目录后粘贴。'):t('目标目录不可粘贴文件，请打开可写目标目录。'));return false;}
  if(moving&&!canMoveSelection(descriptor.sources)){error(t('只能移动当前账号可写的本机文件或文件夹。'));return false;}
  if(busy||uploadActive()||pendingBrowse!==null||pickerCopy){copyNotice(t('当前操作尚未结束，请稍后再粘贴。'));return false;}
  let body:ReturnType<typeof fileCopyRequest>;
  try{body=fileCopyRequest(descriptor,destination);}catch(cause){error(cause);return false;}
  const job:PickerCopy={owner:session,media:mediaKind,destination,token:browseToken,controller:new AbortController(),notice:0};
  pickerCopy=job;paintPickerClipboard();copyNotice(t`正在${verb} ${body.paths.length} 项到 ${job.destination}…`);job.notice=pickerCopyNotice;
  const current=()=>pickerCopy===job&&session===job.owner&&mediaKind===job.media&&!job.controller.signal.aborted;
  const visible=()=>current()&&view==='pick'&&browse?.path===job.destination&&browseToken===job.token&&pendingBrowse===null;
  try{
    const result=moving?await api.moveFiles(fileMoveRequest(descriptor,destination),job.owner.csrf,job.controller.signal):await api.copyFiles(body,job.owner.csrf,job.controller.signal);
    if(!current())return false;
    invalidateTree(job.destination);void refreshQuota();
    if(moving){
      body.paths.forEach((path:string)=>invalidateTree(parentDirectory(path)));removeMovedSelection(body.paths);
      if(pickerClipboard&&JSON.stringify(pickerClipboard)===JSON.stringify(descriptor))pickerClipboard=null;
    }
    if(pickerCopyNotice===job.notice)copyNotice('moved' in result?t`已移动 ${result.moved} 项，原文件已移到目标目录。`:t`已复制 ${result.copied} 个文件，共 ${fmtSize(result.total_bytes)}。同名文件已保留并使用新名称。`);
    if(moving&&view==='pick'&&browse&&pendingBrowse===null){
      const path=browse.path;await browseAt(body.paths.some((source:string)=>path===source||path.startsWith(source+'/'))?destination:path,browseOffset);
    }else if(visible())await browseAt(job.destination,browseOffset);else if(view==='pick')picker();
    return true;
  }catch(cause){
    if(current()){
      if(pickerCopyNotice===job.notice)clearError();
      error(cause,body.paths.find((path:string)=>!!sshLocation(path)));
    }
    return false;
  }finally{if(pickerCopy===job)pickerCopy=null;paintPickerClipboard();}
}
async function moveSelectedTo() {
  if(!session||!canMoveSelection()||busy||uploadActive()||pickerCopy||pendingBrowse!==null)return;
  const owner=session,descriptor=selectedFileClipboard('move').descriptor,single=descriptor.sources.length===1;
  const box=dialog(single?t('移动并重命名'):t('移动到目标文件夹')),label=el('label',single?t('完整目标路径（可同时改名）'):t('目标文件夹（须已存在，保留原名称）')),input=el('input');
  input.type='text';input.value=single?descriptor.sources[0].path:browse?.path||home;input.spellcheck=false;input.setAttribute('aria-label',single?t('完整目标路径'):t('目标文件夹'));label.append(input);
  box.append(label,el('p',(single?t('目标的上级文件夹'):t('目标文件夹'))+t('必须已存在；只移动当前区域中的可写项目，同名目标不会覆盖。'),'muted'));
  const actions=el('div',undefined,'dialog-actions'),feedback=el('p','','form-feedback');feedback.setAttribute('role','alert');
  const go=btn(t('确认移动'),async()=>{
    const target=input.value.trim();if(!target)return;go.disabled=true;
    if(!single){if(await pasteCopiedFiles(descriptor,target))box.closest('.dialog-backdrop')?.remove();go.disabled=false;return;}
    const source=descriptor.sources[0].path;
    if(source===target){feedback.textContent=t('请填写不同的目标路径。');go.disabled=false;return;}
    busy=true;paintPickerClipboard();
    try{
      await api.renameFile(source,target,owner.csrf);
      if(session!==owner)return;
      invalidateTree(parentDirectory(source));invalidateTree(parentDirectory(target));removeMovedSelection([source]);pickerClipboard=null;
      box.closest('.dialog-backdrop')?.remove();info(t('已移动到 ')+target+t('。'));
      const path=browse?.path||home;await browseAt(path===source||path.startsWith(source+'/')?parentDirectory(target):path);void refreshQuota();
    }catch(cause){feedback.textContent=cause instanceof Error?cause.message:String(cause);}finally{busy=false;go.disabled=false;paintPickerClipboard();}
  },'primary');
  actions.append(btn(t('取消'),()=>box.closest('.dialog-backdrop')?.remove()),go);box.append(feedback,actions);input.onkeydown=event=>{if(event.key==='Enter'){event.preventDefault();if(!go.disabled)go.click();}};input.focus();
}
function handlePickerPaste(event:ClipboardEvent) {
  if(event.defaultPrevented||!pickerEventAllowed(event)||!event.clipboardData)return;
  if(session){
    try{
      const descriptor=readFileCopy(event.clipboardData,{origin:location.origin,owner:session.user.id});
      if(descriptor){event.preventDefault();void pasteCopiedFiles(descriptor);return;}
    }catch(cause){event.preventDefault();error(cause);return;}
  }
  const clipboard=event.clipboardData,files=Array.from(clipboard.files);
  // 有些浏览器只暴露 DataTransferItem；不读取剪贴板文本，不申请额外剪贴板权限。
  if(!files.length)for(const item of Array.from(clipboard.items))if(item.kind==='file'){const file=item.getAsFile();if(file)files.push(file);}
  if(!files.length){if(Array.from(clipboard.types).includes('Files'))info(t('浏览器未提供可上传的剪贴板文件，请使用上传按钮。'));return;}
  if(!session||session.user.role==='guest'||!browse||!writableHere(browse.path)){info(t('当前目录不可粘贴上传，请打开可写目录并使用上传按钮。'));return;}
  if(busy||uploadActive()||pendingBrowse!==null)return;
  event.preventDefault();
  const stamp=new Date().toISOString().replace(/[-:]/g,'').replace('T','-').slice(0,15);
  const named=files.map((file,index)=>file.name?file:new File([file],t`剪贴板截图-${stamp}-${index+1}.${clipboardImageExtension(file.type)}`,{type:file.type,lastModified:file.lastModified}));
  void uploadFiles(named);
}
// 根节点处理正常焦点；窗口后备只接收空白文档焦点，顶栏、对比画布和其他控件不会被劫持。
window.addEventListener('keydown',event=>{if(!document.querySelector('.picker')?.contains(event.target as Node))handlePickerKeydown(event);});
window.addEventListener('copy',event=>{if(!document.querySelector('.picker')?.contains(event.target as Node))handlePickerCopy(event);});
window.addEventListener('cut',event=>{if(!document.querySelector('.picker')?.contains(event.target as Node))handlePickerCopy(event,'move');});
window.addEventListener('paste',event=>{if(!document.querySelector('.picker')?.contains(event.target as Node))handlePickerPaste(event);});
function currentDownload(job:PickerDownload){return pickerDownload===job&&session===job.owner&&mediaKind===job.media&&!job.controller.signal.aborted;}
function downloadFileArchive(record:ExportRecord) {
  if(!record.download_url)return;
  const anchor=el('a');anchor.href=record.download_url;anchor.download='webcompare-files.zip';document.body.append(anchor);anchor.click();anchor.remove();
}
function paintPickerDownload() {
  const job=pickerDownload,button=document.querySelector<HTMLButtonElement>('.picker-download');
  if(button)button.disabled=!selected.length||pendingBrowse!==null||!!job&&!job.done;
  const status=document.querySelector<HTMLElement>('.picker-download-status');if(!status)return;
  const message=job?.message();
  status.replaceChildren();if(!job||job.done&&!message)return;
  status.append(el('span',message));
  if(job.record?.status==='ready'&&job.record.download_url){const link=el('a',t('重新下载 ZIP'));link.href=job.record.download_url;link.download='webcompare-files.zip';status.append(link);}
  else if(!job.done)status.append(btn(t('取消等待'),()=>{job.controller.abort();window.clearTimeout(job.timer);if(pickerDownload===job){pickerDownload=null;paintPickerDownload();}},'mini',t('停止等待和自动下载')));
}
async function pollPickerDownload(job:PickerDownload,record?:ExportRecord) {
  if(!currentDownload(job))return;
  try{
    const task=record||await api.exportTask(job.record!.id,job.controller.signal);
    if(!currentDownload(job))return;job.record=task;
    if(task.status==='ready'){
      if(!task.download_url)throw new Error(t('下载任务已完成，但未返回下载地址'));
      job.done=true;job.message=()=>t('原文件 ZIP 已准备。');paintPickerDownload();downloadFileArchive(task);return;
    }
    if(task.status==='failed')throw new Error(serverMessage(task.error,t('原文件打包失败，请重试')));
    job.message=()=>task.status==='queued'?t('原文件下载正在排队…'):t`正在打包原文件${task.total_bytes?` ${Math.min(100,Math.floor(task.processed_bytes/task.total_bytes*100))}% · ${fmtSize(task.processed_bytes)} / ${fmtSize(task.total_bytes)}`:'…'}`;
    paintPickerDownload();job.timer=window.setTimeout(()=>void pollPickerDownload(job),700);
  }catch(cause){if(!currentDownload(job))return;job.done=true;job.message=()=>'';paintPickerDownload();error(cause);}
}
async function downloadSelectedFiles() {
  if(!canBrowse()||!session||!selected.length||pendingBrowse!==null||pickerDownload&&!pickerDownload.done)return;
  const job:PickerDownload={owner:session,media:mediaKind,controller:new AbortController(),record:null,message:()=>t('正在提交原文件下载任务…'),done:false};
  pickerDownload=job;paintPickerDownload();
  try{
    // ZIP 仅由独立下载按钮显式启动，文件复制快捷键不经过此处。
    const task=await api.createFileDownload(selected.map(source=>source.path),recursive,job.owner.csrf,job.media,job.controller.signal);
    if(currentDownload(job))await pollPickerDownload(job,task);
  }catch(cause){if(!currentDownload(job))return;job.done=true;job.message=()=>'';paintPickerDownload();error(cause);}
}
async function browseAt(path:string,offset=0,saveLocation=true) {
  const token=++browseToken,owner=session,revision=remoteRevision;
  pickerPathDraft=null;pendingBrowse=path;if(view==='pick')picker();
  try {const result=await api.browse(path,{offset,q:filter,sort,order,media_kind:mediaKind});if(token!==browseToken||owner!==session||revision!==remoteRevision)return;browse=result;browseOffset=offset;sshManager.rememberPath(result.path);recentPaths=[result.path,...recentPaths.filter(p=>p!==result.path)].slice(0,8);localStorage.setItem('wc-recent-paths',JSON.stringify(recentPaths));if(view==='pick'&&saveLocation)saveNavigation();}
  catch(e){if(token===browseToken&&owner===session&&revision===remoteRevision)error(e,path);}
  finally{if(token===browseToken){pendingBrowse=null;if(view==='pick')picker();}}
}
function disconnectSSHConnection(connection:SSHConnection) {
  // 先取消本页面的远端读取，再关闭 SSH，保留已显示位图、比较配置与帧位置。
  ++remoteRevision;++browseToken;pendingBrowse=null;resetMediaErrors();stopVideo();
  posterController.abort();posterObserver?.disconnect();posterObserver=null;disconnectRemoteVideos(connection.id);
}
async function refreshSSHConnection(connection:SSHConnection) {
  // 重连保留选择、分组和视图，只释放失败解码器/缓存；旧请求不得覆盖新连接结果。
  ++remoteRevision;++browseToken;pendingBrowse=null;resetMediaErrors();clearError();stopVideo();
  const invalidate=(nodes:DNode[])=>{for(const node of nodes){if(sshLocation(node.path)?.id===connection.id){node.loaded=false;node.open=false;node.children=[];}else invalidate(node.children);}};
  invalidate(tree);posters.clear();
  if(comparison&&Object.values(comparison.assets).some(asset=>sshLocation(asset.path)?.id===connection.id))viewer?.update(null,state,shareId);
  render();
  if(sshLocation(browse?.path)?.id===connection.id)await browseAt(browse!.path,browseOffset);
}
function dropSelection(path:string){pickerSelection=selected=selected.filter(source=>source.path!==path);selectionHistory.reset();pickerAnchor=null;}
const uploadActive=()=>!!uploadJob&&uploadJob.phase!=='done';
function paintUploadItem(item:UploadItem) {
  const row=item.row;if(!row)return;row.className='upload-file '+item.state;
  row.children[1].textContent=item.state==='success'?t('已上传'):item.state==='failed'?t('失败'):item.state==='cancelled'?t('未上传'):item.state==='uploading'?t('上传中'):t('待上传');row.children[2].textContent=item.error||'';
}
function scheduleUploadPanel(job:UploadJob) {
  if(job.frame!==undefined)return;
  job.frame=requestAnimationFrame(()=>{job.frame=undefined;if(uploadJob===job)uploadPanel();});
}
function uploadPanel() {
  const workspace=document.querySelector('.workspace');if(!workspace)return;
  const job=uploadJob;
  let panel=document.querySelector<HTMLElement>('.upload-panel');
  if(!job){panel?.remove();uploadPanelJob=null;return;}
  const {sent,success,failed}=job;
  const percent=job.bytes?Math.floor(sent/job.bytes*100):0;
  const caption=job.phase==='checking'?t`核对 ${job.checked}/${job.items.length}`:job.phase==='confirm'?t('等待确认'):job.phase==='done'?t('上传结果'):t`上传 ${percent}%`;
  if(!panel||uploadPanelJob!==job){
    panel?.remove();panel=el('aside',undefined,'upload-panel');panel.setAttribute('aria-label',t('上传进度'));uploadPanelJob=job;
    const head=el('div',undefined,'upload-heading');
    head.append(btn(t('收起'),()=>{job.collapsed=!job.collapsed;uploadPanel();},'upload-toggle'),el('strong'));panel.append(head);
    const body=el('div',undefined,'upload-body');
    body.append(el('p',t`${job.items.length} 个文件 · ${fmtSize(job.bytes)}（${job.bytes.toLocaleString()} 字节）`,'upload-total'),el('p',t`目标：${job.dir}`,'muted upload-target'));
    const progress=el('progress');progress.max=job.bytes||1;progress.setAttribute('aria-label',t('总上传进度'));body.append(progress);
    body.append(el('p','','upload-status'),el('p','','upload-current'),el('p','','upload-message'));
    const list=el('div',undefined,'upload-list');
    for(const item of job.items){item.row=el('div',undefined,'upload-file');item.row.append(el('span',item.path),el('small'),el('p'));paintUploadItem(item);list.append(item.row);}
    body.append(list,btn(t('取消上传'),()=>{if(job.phase==='done'){uploadJob=null;uploadPanel();picker();}else cancelUpload(job);},'upload-cancel danger'));
    panel.append(body);workspace.append(panel);
  }
  panel.classList.toggle('collapsed',job.collapsed);
  const toggle=panel.querySelector<HTMLButtonElement>('.upload-toggle')!;toggle.textContent=job.collapsed?caption+t(' · 展开'):t('收起');toggle.title=job.collapsed?t('展开上传进度'):t('收起上传进度');
  panel.querySelector('strong')!.textContent=caption;
  panel.querySelector('progress')!.value=sent;
  panel.querySelector('.upload-status')!.textContent=t`${fmtSize(sent)} / ${fmtSize(job.bytes)} · 成功 ${success} · 失败 ${failed}`;
  panel.querySelector('.upload-current')!.textContent=Array.from(job.active).map(item=>t`${item.path}：${item.loaded.toLocaleString()} / ${item.file.size.toLocaleString()} 字节${item.loaded===item.file.size?t(' · 正在保存'):''}`).join('\n');
  const message=panel.querySelector<HTMLElement>('.upload-message')!;message.textContent=t(job.message);message.className='upload-message '+(failed?'upload-failure':'muted');
  const cancel=panel.querySelector<HTMLButtonElement>('.upload-cancel')!;cancel.textContent=job.phase==='done'?t('关闭'):t('取消上传');cancel.classList.toggle('danger',job.phase!=='done');
}
function cancelUpload(job:UploadJob) {
  job.controller.abort();job.phase='done';job.message='已取消，已成功上传的文件保留。';
  for(const item of job.items)if(item.state==='pending'||item.state==='uploading'){item.state='cancelled';paintUploadItem(item);}
  job.active.clear();
  document.querySelector('.upload-confirm')?.closest('.dialog-backdrop')?.remove();uploadPanel();picker();
}
function checkName(name:string) {
  if(!name||name!==name.trim()||name==='.'||name==='..'||name.startsWith('.')||name.length>200||/[\\/\x00-\x1f\x7f]/.test(name))throw new Error(t('名称不被接受：请使用 1–200 个字符，不能以点开头或含路径分隔符、首尾空格、控制字符'));
}
async function checkImage(item:UploadItem,maxPixels:number) {
  const parts=item.path.split('/');parts.forEach(checkName);
  if(parts.at(-1)!==item.file.name)throw new Error(t('文件名与所选目录中的名称不一致'));
  if(await checkImageUploadHeader(item.file)==='jpeg'){
    let offset=2,found=false;
    for(let count=0;count<4096&&offset+4<=item.file.size;count++){
      const marker=new Uint8Array(await item.file.slice(offset,offset+4).arrayBuffer());
      if(marker[0]!==255)break;
      if(marker[1]===255){offset++;continue;}
      if(marker[1]===1||(marker[1]>=208&&marker[1]<=215)){offset+=2;continue;}
      const length=marker[2]*256+marker[3];
      if(length<2||offset+length+2>item.file.size)break;
      if(marker[1]>=192&&marker[1]<=207&&![196,200,204].includes(marker[1])){
        const frame=new Uint8Array(await item.file.slice(offset+4,offset+10).arrayBuffer());
        if(frame[0]!==8||frame[5]===4)throw new Error(t('当前只支持 8-bit RGB/灰度 JPEG，不支持 CMYK'));
        found=true;break;
      }
      if(marker[1]===218||marker[1]===217)break;
      offset+=length+2;
    }
    if(!found)throw new Error(t('JPEG 图片头无法确认，请检查文件是否损坏'));
  }
  let bitmap:ImageBitmap;
  try{bitmap=await createImageBitmap(item.file);}catch{throw new Error(t('图片无法完整打开，请检查文件是否损坏'));}
  try{if(!bitmap.width||!bitmap.height||bitmap.width*bitmap.height>maxPixels)throw new Error(t('图片尺寸超过可用范围'));}finally{bitmap.close();}
}
async function uploadFiles(files:File[],folder=false) {
  if(!files.length||pendingBrowse!==null||busy||uploadActive()||!session||session.user.role==='guest'||!browse||!writableHere(browse.path))return;
  if(!folder&&(browse.root_only_folders||browse.path==='member')){error(t('成员区根目录只可创建文件夹，请先打开自己的文件夹。'));return;}
  const csrf=session.csrf,maxPixels=session.limits.max_pixels??Infinity;
  const job:UploadJob={dir:browse.path,media:mediaKind,concurrency:uploadConcurrency(session.profile),items:files.map(file=>({file,path:folder?file.webkitRelativePath:file.name,state:'pending',loaded:0})),bytes:files.reduce((sum,file)=>sum+file.size,0),sent:0,success:0,failed:0,active:new Set(),checked:0,phase:'checking',message:'正在核对全部文件，核对通过后请确认上传。',controller:new AbortController(),collapsed:false};
  uploadJob=job;clearError();uploadPanel();picker();
  const names=new Set<string>();
  for(const item of job.items){
    if(job.controller.signal.aborted||uploadJob!==job)return;
    try{
      if(folder&&item.path.split('/').length<2)throw new Error(t('请选择完整文件夹'));
      if(names.has(item.path))throw new Error(t('所选文件中存在同名路径'));names.add(item.path);
      if(mediaKind==='video'){item.path.split('/').forEach(checkName);await checkVideo(item.file,job.controller.signal);}else await checkImage(item,maxPixels);
    }catch(e){if(job.controller.signal.aborted||uploadJob!==job)return;item.state='failed';job.failed++;item.error=e instanceof Error?e.message:String(e);paintUploadItem(item);}
    if(job.controller.signal.aborted||uploadJob!==job)return;
    job.checked++;if(uploadJob===job)uploadPanel();
  }
  if(job.controller.signal.aborted||uploadJob!==job)return;
  if(job.items.some(item=>item.state==='failed')){
    job.phase='done';job.message='存在不合规文件，本批次未上传；请查看失败原因后重新选择。';
    job.items.forEach(item=>{if(item.state==='pending'){item.state='cancelled';paintUploadItem(item);}});uploadPanel();picker();return;
  }
  job.phase='confirm';job.message='全部文件核对通过，等待确认。';uploadPanel();
  const box=dialog(t('确认上传'),()=>cancelUpload(job));box.classList.add('upload-confirm');
  box.append(el('p',t`已核对 ${files.length} 个${mediaKind==='video'?t('视频'):t('图片')}文件，总大小 ${fmtSize(job.bytes)}（${job.bytes.toLocaleString()} 字节）。`,'upload-confirm-total'),el('p',t`上传到 ${job.dir}${folder?t('，保留文件夹层级'):''}。同名文件不会覆盖。`));
  const actions=el('div',undefined,'dialog-actions');
  actions.append(btn(t('取消'),()=>cancelUpload(job)),btn(t('确认上传'),()=>{box.closest('.dialog-backdrop')?.remove();void runUpload(job,csrf);},'primary'));box.append(actions);
}
function invalidateTree(path:string) {
  const walk=(nodes:DNode[])=>nodes.forEach(node=>{if(node.path===path){node.loaded=false;node.open=false;node.children=[];}else walk(node.children);});walk(rootNodes);walk(tree);
}
async function runUpload(job:UploadJob,csrf:string) {
  if(job.controller.signal.aborted||uploadJob!==job)return;
  job.phase='uploading';job.message='';uploadPanel();
  const dirs=new Map<string,Promise<unknown>>();
  await runBounded(job.items,job.concurrency,async(item:UploadItem)=>{
    if(job.controller.signal.aborted||uploadJob!==job)return;
    item.state='uploading';job.active.add(item);paintUploadItem(item);scheduleUploadPanel(job);
    try{
      const dir=await prepareUploadDirectory(job.dir,item.path,dirs,(path:string)=>api.mkdir(path,csrf,job.controller.signal));
      if(job.controller.signal.aborted||uploadJob!==job)return;
      const saved=await api.upload(dir,item.file.name,item.file,csrf,{signal:job.controller.signal,onProgress:loaded=>{if(uploadJob!==job||job.controller.signal.aborted)return;job.sent+=loaded-item.loaded;item.loaded=loaded;scheduleUploadPanel(job);}},job.media);
      if(uploadJob!==job)return;
      if(job.media==='video')rememberVideo(saved.path,item.file,saved.revision||'');
      item.state='success';job.success++;job.sent+=item.file.size-item.loaded;item.loaded=item.file.size;
    }catch(e){
      if(job.controller.signal.aborted)item.state='cancelled';
      else{item.state='failed';job.failed++;item.error=e instanceof Error?e.message:String(e);}
    }finally{
      job.active.delete(item);paintUploadItem(item);if(uploadJob===job)scheduleUploadPanel(job);
    }
  },job.controller.signal);
  if(uploadJob!==job)return;
  job.phase='done';
  job.items.forEach(item=>{if(item.state==='pending'){item.state='cancelled';paintUploadItem(item);}});
  job.message=job.controller.signal.aborted?'已取消，已成功上传的文件保留。':'上传结束，请查看各文件结果。';
  void refreshQuota();
  invalidateTree(job.dir);uploadPanel();if(browse?.path===job.dir)await browseAt(job.dir);else if(view==='pick')picker();
}
async function createFolder() {
  if(!session||!browse||busy||uploadActive()||!writableHere(browse.path))return;
  const dir=browse.path,name=await ask(t('新建文件夹'),t('文件夹名称'),'');if(!name)return;
  try{checkName(name);const result=await api.mkdir(`${dir.replace(/\/$/,'')}/${name}`,session.csrf);invalidateTree(dir);if(browse?.path===dir)await browseAt(dir);info(result.created?t('文件夹已创建。'):t('文件夹已存在。'));}
  catch(e){error(e);}
}
async function importPack(file:File) {
  if(busy||!session||!browse)return;
  const dir=browse.path;busy=true;clearError();picker();
  try {
    const result=await api.importSharepack(dir,file,session.csrf);
    busy=false;
    void refreshQuota();
    await browseAt(dir);
    info(t`已导入 ${result.imported.length} 张图片${result.skipped_html?t`（跳过 ${result.skipped_html} 个 HTML 条目）`:''}`);
  } catch(e){busy=false;error(e);picker();}
}
function ask(title:string,label:string,initial:string):Promise<string|null> {
  return new Promise(resolve=>{
    let done=false;
    const settle=(value:string|null)=>{if(done)return;done=true;resolve(value);};
    const box=dialog(title,()=>settle(null));
    const input=el('input');input.value=initial;input.spellcheck=false;input.setAttribute('aria-label',label);
    box.append(el('p',label),input);
    const row=el('div',undefined,'dialog-actions');
    row.append(btn(t('取消'),()=>{settle(null);document.querySelector('.dialog-backdrop')?.remove();}),btn(t('确定'),()=>{settle(input.value.trim());document.querySelector('.dialog-backdrop')?.remove();},'primary'));
    box.append(row);
    input.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();settle(input.value.trim());document.querySelector('.dialog-backdrop')?.remove();}else if(e.key==='Escape'){settle(null);document.querySelector('.dialog-backdrop')?.remove();}};
    window.setTimeout(()=>{input.focus();input.select();},0);
  });
}
async function renameEntry(entry:Entry) {
  if(!session)return;
  const name=await ask(t('重命名'),t('新名称（在同一区域、同一目录内）'),entry.name);
  if(!name||name===entry.name)return;
  if(name.includes('/')||name.includes('\\')){error(t('名称不能包含路径分隔符'));return;}
  const parent=entry.path.slice(0,entry.path.lastIndexOf('/'));
  try {await api.renameFile(entry.path,parent?`${parent}/${name}`:name,session.csrf);dropSelection(entry.path);await browseAt(browse?.path||home);}
  catch(e){error(e);}
}
async function deleteEntry(entry:Entry) {
  if(!session)return;
  if(!confirm(t`删除 ${entry.name}？此操作不可撤销。`))return;
  try {await api.deleteFile(entry.path,session.csrf);void refreshQuota();dropSelection(entry.path);await browseAt(browse?.path||home);}
  catch(e){error(e);}
}
function entryActions(entry:Entry):HTMLElement {
  const box=el('span',undefined,'row-actions');
  box.append(btn(t('改名'),ev=>{ev?.stopPropagation();void renameEntry(entry);},'mini',t('重命名此项')),
    btn(t('删除'),ev=>{ev?.stopPropagation();void deleteEntry(entry);},'mini danger',t('删除此项')));
  return box;
}
function add(source:Source) {
  if(selected.some(s=>s.path===source.path))return;
  if(selected.length>=MAX_SELECTION){error(t('一次最多手选 ')+MAX_SELECTION+t(' 项，请先添加到组'));return;}
  pickerAnchor=null;changeSelection([...selected,source]);
}
function addSelectedToGroup() {
  if(!selected.length){error(t('先勾选图片或目录'));return;}
  const groupIndex=manualTarget==='new'?-1:Number(manualTarget);
  const group=groupIndex<0?{label:newGroupName.trim()||filter.trim()||t`分组 ${draftGroups.length+1}`,rules:[] as DraftGroup['rules']}:draftGroups[groupIndex];
  const rules:DraftGroup['rules']=group.rules.map(r=>({...r,names:r.names?[...r.names]:undefined}));
  for(const source of selected){
    if(source.kind==='directory'){
      if(!rules.some(r=>r.path===source.path&&r.pattern==='*'&&!r.names))rules.push({path:source.path,pattern:'*'});
      continue;
    }
    const path=parentDirectory(source.path),name=sourceName(source.path);
    let rule=rules.find(r=>r.path===path&&r.pattern==='*'&&r.names);
    if(!rule){rule={path,pattern:'*',names:[]};rules.push(rule);}
    if(!rule.names!.includes(name))rule.names!.push(name);
  }
  if(rules.length>session!.limits.max_sources){error(t`每组最多可从 ${session!.limits.max_sources} 个目录添加，请分到其他组`);return;}
  if(rules.some(r=>(r.names?.length||0)>500)){error(t('同一目录每组最多手选 500 张'));return;}
  group.rules=rules;
  if(groupIndex<0){draftGroups.push(group);activeDraftGroup=draftGroups.length-1;newGroupName='';}
  manualTarget='new';pickerAnchor=null;changeSelection([]);
}
async function toggleNode(node:DNode) {
  if(node.open){node.open=false;pickerTree();return;}
  if(!node.loaded){
    const owner=session,media=mediaKind,revision=remoteRevision;
    try {const r=await api.browse(node.path,{sort:'name',order:'asc',media_kind:mediaKind});if(owner!==session||media!==mediaKind||revision!==remoteRevision)return;node.children=r.entries.filter(e=>e.kind==='directory').map(e=>({path:e.path,name:e.name,open:false,loaded:false,children:[]}));node.loaded=true;}
    catch(e){if(owner===session&&revision===remoteRevision)error(e,node.path);return;}
  }
  node.open=true;pickerTree();
}
function selectionActions() {
  const actions=document.querySelector('.selection-actions');if(!actions)return;actions.replaceChildren();
  if(!canBrowse())return;
  if(draftGroups.length){
    const go=btn(busy?t('正在加载…'):t`对比 ${draftGroups.length} 组`,()=>void createGroups(),'primary');go.disabled=busy||draftGroups.some(g=>!g.rules.length);actions.append(go);
  }
  const go=btn(busy?t('正在加载…'):comparison?t('应用并返回对比'):t('对比 →'),()=>void create(draftCombine),'primary');go.disabled=busy||!selected.length||selected.length>session!.limits.max_sources;
  const order=el('select',undefined,'compare-order');order.setAttribute('aria-label',t('排列方式'));
  order.title=t('按来源连续：A1、A2、B1、B2；按序号交错：A1、B1、A2、B2。选择后点击对比生效。');
  for(const [value,label] of [['concat',t('按来源连续')],['interleave',t('按序号交错')]]){
    const option=el('option',label);option.value=value;order.append(option);
  }
  order.value=draftCombine;order.disabled=busy;
  order.onchange=()=>{
    const combine=order.value as typeof draftCombine;
    // 手动排图优先于排列方式；主动切换时清除旧顺序，原配置不变时继续保留。
    if(combine!==draftCombine){draftCombine=combine;draftImageOrder=[];}
  };
  actions.append(go,order);
}
function pickerTree() {
  const box=document.querySelector('.tree');if(!box)return;const scroll=box.scrollTop;box.replaceChildren();
  const renderNodes=(nodes:DNode[],depth:number,parent:HTMLElement)=>{
    nodes.forEach(node=>{
      const row=el('div',undefined,'tnode');
      row.style.paddingLeft=(6+depth*14)+'px';
      row.append(btn(node.open?'▾':'▸',()=>void toggleNode(node),'ttoggle'));
      row.append(btn(node.area?'▣ '+areaLabel(node.area):'📁 '+node.name,()=>void browseAt(node.path),'tname',node.path));
      if(node.area)row.append(el('small',node.area.writable?t('可写'):t('只读'),'area-tag'));
      if(node.bm)row.append(btn('×',()=>{bookmarks=bookmarks.filter(b=>b.path!==node.path);saveBookmarks();tree=treeRoots();pickerTree();},'icon-btn',t('移除书签')));
      parent.append(row);
      if(node.open&&node.loaded)renderNodes(node.children,depth+1,parent);
    });
  };
  if(rootNodes.length){
    box.append(el('div',t('区域 · 挂载'),'section-title'));
    renderNodes(rootNodes,0,box as HTMLElement);
    box.append(el('div',t('收藏目录'),'section-title side-section'));
  } else box.append(el('div',t('收藏目录'),'section-title'));
  renderNodes(tree,0,box as HTMLElement);
  if(!tree.length)box.append(el('p',t('收藏常用目录，下次快速打开。'),'muted pad'));
  const selectedTitle=el('div',t`已选来源 · ${selected.length}`,'section-title side-section');box.append(selectedTitle);
  if(selected.length){
    const list=el('div',undefined,'side-selected');
    selected.slice(0,24).forEach((s,i)=>{
      const row=el('div',undefined,'side-item');row.title=s.path;
      row.append(el('span',`${i+1} · ${s.label}`),btn('×',()=>{pickerAnchor=null;changeSelection(selected.filter(source=>source.path!==s.path));},'icon-btn',t('移除此来源')));list.append(row);
    });box.append(list);
    if(selected.length>24)box.append(el('p',t`另有 ${selected.length-24} 项已选，可继续取消勾选或直接添加到组。`,'muted side-note'));
  }else box.append(el('p',t('可勾选图片或目录，直接创建普通对比。'),'muted side-note'));
  const title=el('div',t('规则分组'),'section-title side-section');box.append(title);
  const make=el('div',undefined,'side-form'),name=el('input');name.placeholder=t('分组名称，例如 det5');name.value=newGroupName;name.maxLength=120;name.setAttribute('aria-label',t('新分组名称'));name.oninput=()=>{newGroupName=name.value;};
  const addGroup=()=>{const label=newGroupName.trim();if(!label)return;draftGroups.push({label,rules:[]});activeDraftGroup=draftGroups.length-1;manualTarget=String(activeDraftGroup);newGroupName='';picker();};
  name.onkeydown=e=>{if(e.key==='Enter')addGroup();};make.append(name,btn('＋',addGroup,'primary',t('新建分组')));box.append(make);
  draftGroups.forEach((group,i)=>{
    const row=el('div',undefined,'side-item group-draft'+(i===activeDraftGroup?' active':''));
    row.append(btn(t`${group.label} · ${group.rules.length} 条规则`,()=>{activeDraftGroup=i;manualTarget=String(i);picker();},'group-draft-name',group.label),btn('×',()=>{draftGroups.splice(i,1);activeDraftGroup=Math.min(activeDraftGroup,draftGroups.length-1);manualTarget='new';picker();},'icon-btn',t('删除分组')));
    box.append(row);
  });
  if(activeDraftGroup>=0){
    const group=draftGroups[activeDraftGroup];
    const pattern=el('input');pattern.placeholder=t('文件名规则，如 *_det5');pattern.value=groupPattern;pattern.setAttribute('aria-label',t('文件名规则'));pattern.oninput=()=>{groupPattern=pattern.value;};
    const addRule=()=>{if(!browse||!groupPattern.trim())return;const rule={path:browse.path,pattern:groupPattern.trim()};if(!group.rules.some(r=>r.path===rule.path&&r.pattern===rule.pattern))group.rules.push(rule);pickerTree();};
    const ruleBox=el('div',undefined,'side-rules');ruleBox.append(pattern,btn(t('将当前目录按规则加入'),addRule,'primary wide',t('规则匹配文件名或不带扩展名的名称；可切换目录继续加入同一组')));
    group.rules.forEach((rule,i)=>{
      const row=el('div',undefined,'side-item rule-item');row.title=rule.path;
      const description=el('span',`${rule.names?t`手选 ${rule.names.length} 张`:rule.pattern} · ${sourceName(rule.path)}`);
      row.append(description,btn('×',()=>{group.rules.splice(i,1);pickerTree();},'icon-btn',t('移除规则')));ruleBox.append(row);
      if(rule.names){
        const names=el('details'),summary=el('summary',t`查看 / 微调 ${rule.names.length} 张`),list=el('div',undefined,'side-rules');names.append(summary,list);
        names.ontoggle=()=>{
          if(!names.open||list.childElementCount)return;
          rule.names!.forEach(name=>{
            const item=el('div',undefined,'side-item');item.title=name;
            item.append(el('span',name),btn('×',()=>{
              rule.names=rule.names!.filter(current=>current!==name);
              if(!rule.names.length){group.rules.splice(i,1);pickerTree();return;}
              summary.textContent=t('查看 / 微调 ')+rule.names.length+t(' 张');
              description.textContent=t('手选 ')+rule.names.length+t(' 张 · ')+sourceName(rule.path);item.remove();
            },'icon-btn',t('移除此图片')));list.append(item);
          });
        };ruleBox.append(names);
      }
    });
    box.append(ruleBox);
  }
  if(!draftGroups.length)box.append(el('p',t('用 *_det5、*t1、*g65 等规则，从当前目录及其他目录收集图片。'),'muted side-note'));
  selectionActions();
  box.scrollTop=scroll;
}
function picker() {
  posterController.abort();posterController=new AbortController();posterObserver?.disconnect();posterObserver=null;
  const keepScroll=document.querySelector('.ftable,.thumbgrid')?.scrollTop||0;
  const keepTreeScroll=document.querySelector('.tree')?.scrollTop||0;
  const box=document.querySelector('.picker') as HTMLElement|null;if(!box)return;
  syncPickerState();
  const active=document.activeElement instanceof HTMLElement&&box.contains(document.activeElement)?document.activeElement:null;
  const focusKey=active?.dataset.pickerFocus;
  const caret=active instanceof HTMLInputElement?{start:active.selectionStart,end:active.selectionEnd}:null;
  // 每次重建后恢复文本光标或文件区焦点，并保留滚动；连续选择不会掉回页面顶端。
  const finish=()=>{
    const scroll=box.querySelector('.ftable,.thumbgrid');if(scroll)scroll.scrollTop=keepScroll;
    if(!active)return;
    const replacement=focusKey?Array.from(box.querySelectorAll<HTMLElement>('[data-picker-focus]')).find(item=>item.dataset.pickerFocus===focusKey):null;
    const target=replacement||box;target.focus({preventScroll:true});
    if(target instanceof HTMLInputElement&&caret?.start!=null&&caret.end!=null)try{target.setSelectionRange(caret.start,caret.end);}catch{/* checkbox/radio 没有文本光标 */}
  };
  box.replaceChildren();box.tabIndex=0;box.setAttribute('aria-label',t('文件选择区'));box.onkeydown=handlePickerKeydown;box.oncopy=event=>handlePickerCopy(event);box.oncut=event=>handlePickerCopy(event,'move');box.onpaste=handlePickerPaste;
  if(!canBrowse())return;
  if(treeW!==null)box.style.setProperty('--picker-tree-width',`${treeW}px`);
  const treeCol=el('div',undefined,'tree');
  const treeGutter=el('div',undefined,'gutter');treeGutter.title=t('拖动调整目录树宽度');
  hDrag(treeGutter,()=>treeCol.offsetWidth,w=>{treeW=w;box.style.setProperty('--picker-tree-width',`${w}px`);localStorage.setItem('wc-treew',String(w));});
  const mainCol=el('div',undefined,'picker-main');
  box.append(treeCol,treeGutter,mainCol);
  pickerTree();treeCol.scrollTop=keepTreeScroll;
  const heading=el('div',undefined,'picker-heading');heading.append(el('h1',mediaKind==='video'?t('选择视频'):t('选择图片')),el('span',mediaKind==='video'?t('目录 · 视频 · 逐帧对比'):t('目录 · 图片 · 帧序列'),'muted'));
  const currentPath=pendingBrowse??browse?.path??home,remote=sshLocation(currentPath);
  // 来源与 SSH 操作收进标题区，贴近文件视图切换，给文件列表留出主要空间。
  if(session?.mode==='personal'){heading.classList.add('has-ssh-sources');heading.append(sshManager.sourceBar(currentPath,home));}
  heading.append(segmented(t('文件视图'),[['list',t('列表视图')],['thumb',t('缩略视图')]],listMode,value=>{listMode=value as 'list'|'thumb';localStorage.setItem('wc-listmode',listMode);picker();document.querySelector<HTMLInputElement>('.picker-heading .segmented input:checked')?.focus();}));mainCol.append(heading);
  const bar=el('div',undefined,'filebar');
  const pathInput=el('input');pathInput.type='text';pathInput.value=pickerPathDraft??remote?.path??currentPath;pathInput.placeholder=remote?t('输入远端绝对路径'):serverMode()?t('输入区域中的目录路径'):t('输入服务器上的绝对路径');pathInput.setAttribute('aria-label',t('目录路径'));pathInput.title=currentPath;pathInput.spellcheck=false;pathInput.dataset.pickerFocus='path';
  // SSH 列表或目录的迟到响应会重建选择器；未提交的路径文本不能因此丢失。
  pathInput.oninput=()=>{pickerPathDraft=pathInput.value;};
  pathInput.setAttribute('list','recent-paths');
  const historyList=el('datalist');historyList.id='recent-paths';recentPaths.forEach(path=>{const option=el('option');option.value=path;historyList.append(option);});bar.append(historyList);
  const openPath=()=>void browseAt(resolveSSHPath(pathInput.value.trim(),currentPath,remote?sshManager.connection(remote.id)?.home:''));
  pathInput.onkeydown=e=>{if(e.key==='Enter')openPath();};
  bar.append(pathInput,btn(t('打开'),openPath,'primary'));
  if(browse?.parent!=null)bar.append(btn(t('↑ 上一级'),()=>void browseAt(browse!.parent!)));
  bar.append(btn(t('＋ 添加当前目录'),()=>{if(browse)add({path:browse.path,label:sourceName(browse.path),kind:'directory'});}));
  bar.append(btn(t('☆ 收藏目录'),()=>{if(browse&&!bookmarks.some(b=>b.path===browse!.path)){bookmarks.push({path:browse.path,label:sourceName(browse.path)});saveBookmarks();tree=treeRoots();pickerTree();}},'',t('把当前目录加入左侧树（保存在本浏览器）')));
  const manage=pendingBrowse===null&&!!browse&&writableHere(browse.path);
  if(manage&&browse) {
    const guest=session!.user.role==='guest';
    const mkdir=btn(t('新建文件夹'),()=>void createFolder());mkdir.disabled=busy||uploadActive();bar.append(mkdir);
    if(guest||!(browse.root_only_folders||browse.path==='member')){
      const pack=el('input');pack.type='file';pack.hidden=true;pack.accept='.zip';pack.setAttribute('aria-label',t('选择分享包'));
      pack.onchange=()=>{const file=pack.files?.[0];pack.value='';if(file)void importPack(file);};
      const importButton=btn(t('导入分享包'),()=>pack.click(),'primary import-pack',t('导入分享包（.zip）里的图片到当前目录；包内 HTML 不在本站执行'));importButton.disabled=busy||uploadActive();bar.append(pack,importButton);
      if(!guest){
        const picker=el('input');picker.type='file';picker.hidden=true;picker.accept=mediaKind==='video'?videoExtensions:imageUploadExtensions;picker.multiple=true;picker.setAttribute('aria-label',mediaKind==='video'?t('选择上传视频'):t('选择上传图片'));
        picker.onchange=()=>{const files=Array.from(picker.files||[]);picker.value='';if(files.length)void uploadFiles(files);};
        const upload=btn(mediaKind==='video'?t('上传视频'):t('上传图片'),()=>picker.click(),'',t('选择文件并确认后上传，可多选；文件区 Ctrl/Cmd+V 可粘贴浏览器提供的图片文件，未提供文件时请使用此按钮'));upload.disabled=busy||uploadActive();bar.append(picker,upload);
      }
    }
    if(!guest){
      const directory=el('input');directory.type='file';directory.hidden=true;directory.multiple=true;directory.webkitdirectory=true;directory.setAttribute('aria-label',t('选择上传文件夹'));
      directory.onchange=()=>{const files=Array.from(directory.files||[]);directory.value='';if(files.length)void uploadFiles(files,true);};
      const upload=btn(t('上传文件夹'),()=>directory.click(),'',t('选择完整文件夹，核对全部图片并确认后上传，保留子目录'));upload.disabled=busy||uploadActive();bar.append(directory,upload);
    }
  }
  mainCol.append(bar);
  if(browse?.root_only_folders||serverMode()&&browse?.path==='member')heading.querySelector('.muted')!.textContent=t('成员区根目录只存放文件夹；请创建自己的文件夹或上传整个图片文件夹。');
  else if(serverMode()&&browse&&!manage)heading.querySelector('.muted')!.textContent=t('当前目录只读，可选择图片对比。');
  const actions=el('div',undefined,'actionbar');
  const filterInput=el('input');filterInput.type='search';filterInput.placeholder=t('筛选名称，可用多个 *');filterInput.value=filterDraft;filterInput.setAttribute('aria-label',t('筛选文件名称'));filterInput.dataset.pickerFocus='filter';
  const search=()=>{filter=filterDraft.trim();void browseAt(browse?.path||home,0);};
  filterInput.onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();search();}};
  const all=btn(browseOffset||browse?.truncated?t('全选本页'):t('全选匹配'),()=>chooseVisible(),'',t('勾选当前排序、筛选页；Ctrl/Cmd+A 再按一次取消本页，最多 500 项'));
  const unselect=btn(browseOffset||browse?.truncated?t('取消本页'):t('取消全选'),()=>chooseVisible(true),'',t('取消当前搜索结果中的勾选；清空已选可移除跨目录的所有选择'));
  filterInput.oninput=()=>{filterDraft=filterInput.value;all.disabled=unselect.disabled=pendingBrowse!==null||filterDraft.trim()!==filter;};
  all.disabled=unselect.disabled=pendingBrowse!==null||filterDraft.trim()!==filter;
  const searchbar=el('div',undefined,'picker-search');
  searchbar.append(filterInput,btn(t('搜索'),search,'primary'),el('span',t`已选 ${selected.length} 项`,'muted picker-selection-count'));
  actions.append(searchbar,all,unselect);
  actions.append(btn(t('清空已选'),()=>{pickerAnchor=null;changeSelection([]);},'danger wide-danger',t('移除全部已选来源（Esc）')));
  const undo=btn(t('撤销选择'),()=>restoreSelection(),'',t('Ctrl/Cmd+Z；只恢复勾选状态，不撤销上传、改名或删除'));undo.disabled=!selectionHistory.canUndo;
  const redo=btn(t('重做选择'),()=>restoreSelection(true),'',t('Ctrl+Y 或 Ctrl/Cmd+Shift+Z'));redo.disabled=!selectionHistory.canRedo;
  actions.append(undo,redo);
  const target=el('select');target.setAttribute('aria-label',t('目标分组'));target.title=t('选择已有分组，或新建一个分组');
  const fresh=el('option',t('＋ 新分组'));fresh.value='new';target.append(fresh);
  draftGroups.forEach((group,i)=>{const option=el('option',group.label);option.value=String(i);target.append(option);});
  target.value=manualTarget;target.onchange=()=>{manualTarget=target.value;if(manualTarget!=='new'){activeDraftGroup=Number(manualTarget);pickerTree();}};
  actions.append(target,btn(t('添加到组'),addSelectedToGroup,'',t('把已勾选项加入目标分组；默认每次新建一组')));
  if(serverMode()&&session&&['member','admin'].includes(session.user.role))actions.append(btn(t('剪切'),()=>void copySelectedFiles('move'),'picker-file-cut',t('剪切所选项目（Ctrl/Cmd+X）；粘贴成功前原文件保留')),btn(t('移动到…'),()=>void moveSelectedTo(),'picker-file-move',t('单项可填写完整目标路径并改名；多项移入已有文件夹')));
  if(mediaKind==='image')actions.append(rawPanel());
  const fileActions=el('div',undefined,'file-actions');
  const copy=btn(t('复制'),()=>void copySelectedFiles('copy'),'picker-file-copy',t('复制所选文件引用（Ctrl/Cmd+C），到目标目录后粘贴'));
  fileActions.append(copy);
  fileActions.append(btn(t('粘贴'),()=>{if(pickerClipboard)void pasteCopiedFiles(pickerClipboard);},'picker-file-paste',t('粘贴本站内已暂存的文件清单；Ctrl/Cmd+V 读取系统剪贴板')));
  const download=btn(t('下载'),()=>void downloadSelectedFiles(),'picker-download',t('把选中的原文件打包下载为 ZIP；目录默认包含子目录'));
  fileActions.append(download);
  actions.append(fileActions);
  const downloadStatus=el('div',undefined,'row picker-download-status');downloadStatus.setAttribute('role','status');downloadStatus.setAttribute('aria-live','polite');
  mainCol.append(actions,downloadStatus);
  paintPickerClipboard();paintPickerDownload();
  const table=el('div',undefined,'ftable');
  const head=el('div',undefined,'frow head');
  const sortMark=(key:string)=>sort===key?(order==='asc'?' ▲':' ▼'):'';
  const headBtn=(key:'name'|'size'|'mtime',label:string)=>btn(label+sortMark(key),()=>{if(sort===key)order=order==='asc'?'desc':'asc';else{sort=key;order='asc';}void browseAt(browse?.path||home,0);},'hbtn');
  head.append(el('span',''),headBtn('name',t('名称')),headBtn('size',t('大小')),headBtn('mtime',t('修改时间')));
  table.append(head);
  if(browse && listMode==='thumb') {
    mainCol.append(head);
    const grid=el('div',undefined,'thumbgrid');
    browse.entries.forEach(entry=>{
      const card=el('div','','tcard'+(selected.some(s=>s.path===entry.path)?' checked':''));
      const check=el('input');check.type='checkbox';check.checked=selected.some(s=>s.path===entry.path);
      check.setAttribute('aria-label',t('选择 ')+entry.name);check.onclick=event=>{event.stopPropagation();chooseEntry(entry,event);};card.append(check);
      if(entry.writable===true||entry.writable===undefined&&manage&&session!.user.role==='admin'){const actions=el('div',undefined,'tcard-actions');actions.append(entryActions(entry));card.append(actions);}   // 卡片上的改名/删除按钮
      if(entry.kind==='image'){
        const img=el('img'),query=new URLSearchParams({path:entry.path});
        if(isRawPath(entry.path))query.set('raw',rawSettingsKey(state.raw));
        img.loading='lazy';img.decoding='async';img.alt=entry.name;img.src='/api/thumb?'+query;
        if(isRawPath(entry.path))img.onerror=()=>img.replaceWith(el('div',t('RAW · 设置解析参数后查看'),'raw-thumb-placeholder'));
        card.append(img);
      }
      else card.append(el('div',entry.kind==='video'?'▶':'📁','tdir'));
      card.append(el('div',entry.name,'tname2'));
      card.tabIndex=0;card.setAttribute('role','button');card.setAttribute('aria-label',entry.name);
      card.onclick=event=>entry.kind==='directory'&&!event.shiftKey?void browseAt(entry.path):chooseEntry(entry,event);
      card.onkeydown=event=>{if(event.target===card&&(event.key==='Enter'||event.key===' ')){event.preventDefault();if(entry.kind==='directory'&&event.key==='Enter'&&!event.shiftKey)void browseAt(entry.path);else chooseEntry(entry,event);}};
      grid.append(card);if(entry.kind==='video')queueMicrotask(()=>observePoster(entry,card));
    });
    if(!browse.entries.length)grid.append(el('p',filter?t('没有匹配的名称'):t('空目录'),'muted pad'));
    mainCol.append(grid);
    if(browseOffset>0||browse.truncated){
      const pages=el('div',undefined,'row');
      if(browseOffset>0)pages.append(btn(t('上一页'),()=>void browseAt(browse!.path,Math.max(0,browseOffset-500))));
      if(browse.truncated)pages.append(btn(t('下一页'),()=>void browseAt(browse!.path,browseOffset+500)));
      mainCol.append(pages);
    }
    finish();
    return;
  }
  if(browse) {
    browse.entries.forEach(entry=>{
      const row=el('div',undefined,'frow'+(selected.some(s=>s.path===entry.path)?' checked':''));
      const box=el('input');box.type='checkbox';box.checked=selected.some(s=>s.path===entry.path);
      box.setAttribute('aria-label',t('选择 ')+entry.name);
      box.onclick=event=>{event.stopPropagation();chooseEntry(entry,event);};
      const name=btn((entry.kind==='directory'?'📁 ':'▧ ')+entry.name,event=>{event?.stopPropagation();if(entry.kind==='directory'&&!event?.shiftKey)void browseAt(entry.path);else chooseEntry(entry,event);},'fname');
      name.title=entry.path;
      if(entry.writable===true||entry.writable===undefined&&manage&&session!.user.role==='admin'){const cell=el('div',undefined,'fname-cell');cell.append(name,entryActions(entry));row.append(box,cell);}
      else row.append(box,name);
      row.append(el('span',entry.kind!=='directory'?fmtSize(entry.bytes||0):'','fsize'),el('span',fmtTime(entry.mtime),'fmtime'));
      row.onclick=event=>{if(!(event.target as HTMLElement).closest('button,input'))chooseEntry(entry,event);};
      table.append(row);
    });
    if(!browse.entries.length)table.append(el('p',filter?t('没有匹配的名称'):t('空目录'),'muted pad'));
    mainCol.append(table);
    if(browseOffset>0||browse.truncated){
      const pages=el('div',undefined,'row');
      if(browseOffset>0)pages.append(btn(t('上一页'),()=>void browseAt(browse!.path,Math.max(0,browseOffset-500))));
      if(browse.truncated)pages.append(btn(t('下一页'),()=>void browseAt(browse!.path,browseOffset+500)));
      mainCol.append(pages);
    }
  } else mainCol.append(table);
  finish();
}
let railDrag:RailDragController|null=null,railDirty=false,railContext='',railRevision=0;
let railComparison:Comparison|null=null,railOwner:Session|null=null;
let railPending:{comparison:Comparison;owner:Session;token:number}|null=null,railRequest=0;
const railGroupKeys=new WeakMap<object,string>();
function railGroupKey(group:{label:string;images:string[]}) {
  let key=railGroupKeys.get(group);if(key)return key;
  let hash=2166136261;for(const character of group.label+'\0'+group.images.join('\0'))hash=Math.imul(hash^character.charCodeAt(0),16777619);
  key=(hash>>>0).toString(36);railGroupKeys.set(group,key);return key;
}
function activateRailItem(item:RailDragItem) {
  if(!comparison||view!=='compare')return;
  if(item.kind==='group'){
    const indices=orderedGroupIndices(comparison,state) as number[],index=indices.indexOf(item.index);
    let first=0;for(const original of indices){if(original===item.index)break;first+=Math.max(1,Math.ceil(comparison.groups![original].images.length/cellsOf(state.layout)));}
    update(state.grouping==='within'?{group_index:first,diff_base:0}:{diff_base:index});
  }else if(state.mode==='diff')update({diff_base:Number(item.element.dataset.railSlot)});
}
async function commitRailMove(source:RailDragItem,target:RailDragItem,placement:RailPlacement,scope:string) {
  const comp=comparison,owner=session;
  if(!comp||!owner||view!=='compare'||scope!==railContext||!canBrowse()||state.natural_sort||!source.sortable||!target.sortable||source.kind!==target.kind||railPending?.comparison===comp)return;
  const page=pageInfo(comp,state.layout,state.group_index,state),display=displayImageIndices(comp,state) as number[];
  const before=source.kind==='group'?comp.groups!.map((_,index)=>index):display.map(index=>comp.images[index]);
  const from=source.kind==='group'?source.index:page.start+Number(source.element.dataset.railSlot),to=target.kind==='group'?target.index:page.start+Number(target.element.dataset.railSlot);
  const moved=moveRailItem<number|string>(before,from,to,placement);if(!moved.changed)return;
  const job={comparison:comp,owner,token:++railRequest};railPending=job;rail();
  const current=()=>railPending===job&&comparison===comp&&session===owner&&railContext===scope&&view==='compare';
  try{
    if(source.kind==='group'){
      const order=moved.items as number[],result=temporaryComparisons.has(comp)?{...comp,groups:order.map(index=>comp.groups![index])}:await api.reorderGroups(comp.id,order,owner.csrf);if(!current())return;
      if(temporaryComparisons.has(comp))temporaryComparisons.add(result);
      comparison=result;pageViews={};
      if(state.grouping==='within'){
        let first=0;for(const original of order){if(original===page.groupIndex)break;first+=Math.max(1,Math.ceil(comp.groups![original].images.length/cellsOf(state.layout)));}
        update({group_index:first+page.groupPage,panels:[]});
      }else update({diff_base:moved.index,panels:[]});
    }else{
      const images=moved.items as string[];if(!temporaryComparisons.has(comp))await api.reorder(comp.id,images,owner.csrf);if(!current())return;
      // 手调明确提交当前显示顺序，退出转置，避免来源再次交错后与插入线位置不一致。
      comparison={...comp,images};if(temporaryComparisons.has(comp))temporaryComparisons.add(comparison);else if(!serverMode())draftImageOrder=images.map(id=>comp.assets[id].path);pageViews={};update({transposed:false,panels:[]});
    }
  }catch(cause){if(current())error(cause);}
  finally{if(railPending===job){railPending=null;if(comparison===comp&&session===owner&&view==='compare')rail();}}
}
function rail() {
  removalButtons();
  const box=document.querySelector<HTMLElement>('.lane-rail');
  if(!box){railDrag?.destroy();railDrag=null;return;}
  if(railDrag?.root!==box){railDrag?.destroy();railDrag=new RailDragController(box,{activate:activateRailItem,drop:(source,target,placement,scope)=>void commitRailMove(source,target,placement,scope),finish:()=>{if(railDirty){railDirty=false;rail();}}});}
  if(!railDrag)return;
  if(!comparison){railDrag.cancel(false);box.replaceChildren();railDrag.sync([],'','');return;}
  const comp=comparison;
  if(comp!==railComparison||session!==railOwner){railComparison=comp;railOwner=session;++railRevision;}
  const context=JSON.stringify([railRevision,view,state.group_index,state.layout,state.grouping,state.natural_sort,state.transposed,groupSearch]);
  // 解码帧、缩略图完成会频繁要求刷新；拖动期间保留捕获容器和卡片，结束后一次补画。
  if(railDrag.engaged&&railDrag.context===context){railDirty=true;return;}
  railDrag.cancel(false);railDirty=false;railContext=context;
  const previous=railDrag.measure(),scroll=box.scrollTop,groupScroll=box.querySelector('.compare-group-list')?.scrollTop||0;
  const focused=document.activeElement?.classList.contains('group-search'),caret=focused?(document.activeElement as HTMLInputElement).selectionStart:null;
  const items:RailDragItem[]=[],pending=railPending?.comparison===comp&&railPending.owner===session;
  const register=(element:HTMLElement,key:string,kind:RailDragItem['kind'],index:number,sortable:boolean)=>{
    const item={element,key,kind,index,sortable};element.dataset.railKey=key;element.dataset.railSortable=String(sortable);element.draggable=false;element.tabIndex=0;element.setAttribute('role','button');
    element.onkeydown=event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();railDrag?.select(key);activateRailItem(item);}};items.push(item);
  };
  box.replaceChildren();box.setAttribute('aria-busy',String(pending));
  if(view==='compare'){
    const heading=el('div',undefined,'rail-header'),transpose=btn(t('转置'),()=>transposeComparisonView(),'mini rail-transpose');
    transpose.disabled=!!comp.groups?.length||sourceLanes(comp,state).filter((lane:number[])=>lane.length).length<2;
    transpose.title=comp.groups?.length?t('规则组使用工具栏的分组方式切换'):t('切换来源连续与序号交错的显示顺序；布局和原清单不变');transpose.setAttribute('aria-pressed',String(!!state.transposed));transpose.classList.toggle('tool-active',!!state.transposed);
    heading.append(el('span',t('当前页'),'muted'),transpose);box.append(heading);
  }
  const ids=slotAssets(comp,state) as (string|null)[],page=pageInfo(comp,state.layout,state.group_index,state),start=page.start;
  const indices=orderedGroupIndices(comp,state) as number[],display=displayImageIndices(comp,state) as number[];
  if(comp.groups?.length){
    const search=el('input');search.type='search';search.placeholder=t('检索分组…');search.value=groupSearch;search.className='group-search';search.setAttribute('aria-label',t('检索分组'));
    const list=el('div',undefined,'compare-group-list');list.dataset.railScroll='true';const needle=groupSearch.trim().toLocaleLowerCase(),occurrences=new Map<string,number>();
    indices.forEach((original,index)=>{
      const group=comp.groups![original],stable=railGroupKey(group),occurrence=occurrences.get(stable)||0;occurrences.set(stable,occurrence+1);
      if(needle&&!group.label.toLocaleLowerCase().includes(needle)&&!group.images.some(id=>comp.assets[id]?.name.toLocaleLowerCase().includes(needle)))return;
      const active=state.grouping==='within'?original===page.groupIndex:index===state.diff_base,sortable=canBrowse()&&!state.natural_sort&&!pending;
      const row=el('div',undefined,'compare-group'+(active?' active':''));row.title=t`${group.label} · ${group.images.length} 张\n${state.grouping==='within'?t('点击查看本组'):t('点击设为差分基准')}${sortable?t('；拖动可调整整组顺序'):''}`;
      row.append(el('span',`${index+1}. ${group.label}`),el('small',String(group.images.length)));register(row,`group:${stable}:${occurrence}`,'group',original,sortable);list.append(row);
    });
    if(!list.childElementCount)list.append(el('p',t('没有匹配的组'),'muted'));
    search.oninput=()=>{groupSearch=search.value;rail();};box.append(search,list,el('div',state.grouping==='within'?t('当前组图片'):t('当前序号各组'),'section-title rail-title'));list.scrollTop=groupScroll;
    if(focused){search.focus();if(caret!==null)search.setSelectionRange(caret,caret);}
  }
  const videoInfo=comp.media_kind==='video'?viewer?.videoInfo():null;
  ids.forEach((id,i)=>{
    const item=el('div',undefined,'lane'+(state.mode==='diff'&&state.diff_base===i?' diff-base':'')),thumb=el('div',undefined,'lane-thumb');item.dataset.railSlot=String(i);
    if(id){const poster=viewer?.thumbnail(id,i);if(poster)thumb.append(poster);else if(comp.media_kind==='video')thumb.append(el('span','▶'));}
    const info=videoInfo?.[i];
    if(info&&info.id===id)thumb.title=t`第 ${info.frame+1} / ${info.count} 帧`;
    item.append(thumb);const asset=id?comp.assets[id]:null,sourceIndex=comp.groups?.length?start+i:display[start+i];
    const group=comp.groups?.[state.grouping==='within'?page.groupIndex:indices[i]],sortable=!!id&&sourceIndex>=0&&canBrowse()&&!comp.groups?.length&&!state.natural_sort&&!pending;
    const name=el('div',group?`${i+1} · ${group.label} · ${asset?.name||t('缺图')}`:`${start+i+1} · ${asset?.name||t('缺图')}`,'lane-name');name.title=asset?.path||'';item.append(name);item.title=(asset?.path||'')+(sortable?t('\n按住并拖到实线处调整顺序，松手后生效。拖动按当前显示顺序重排，并结束序列转置。'):'');
    register(item,comp.groups?.length?`asset:${group?railGroupKey(group):'missing'}:${i}:${id||'empty'}`:`asset:${info&&info.id===id?info.key:id||sourceIndex}`,'asset',sourceIndex,sortable);box.append(item);
  });
  box.scrollTop=scroll;railDrag.sync(items,context,`${session?.user.id||''}:${comp.id}`);railDrag.animateFrom(previous);
}
async function showGif() {
  const source=viewer,original=comparison;
  if(mediaKind==='video'){stopVideo();await source?.settled();if(source!==viewer||original!==comparison)return;}
  if(!comparison||!viewer)return;
  const cells=viewer.snapshotCells();
  if(cells.length<2){error(t('当前页只有一张图，无需导出 GIF'));return;}
  const box=dialog(t('导出 GIF（所见即所得，含当前缩放与裁剪）'));
  box.append(el('p',t('帧取当前页各槽的屏上画面；每帧下方/上方可标注简称，留空用文件名。')));
  const labels:string[]=[];
  cells.forEach((cell,i)=>{
    const row=el('label',undefined,'gif-row');
    row.append(el('span',`${i+1} · ${cell.name}`,'gif-name'));
    const input=el('input');input.placeholder=cell.name;input.value=cell.name;input.setAttribute('aria-label',t('第 ')+(i+1)+t(' 帧简称'));
    input.oninput=()=>{labels[i]=input.value;};
    labels[i]=cell.name;
    row.append(input);box.append(row);
  });
  const opts=el('div',undefined,'dialog-actions');
  const pos=el('select');for(const [value,label] of [['bottom',t('标注在下方')],['top',t('标注在上方')],['none',t('不标注')]] as const){const option=el('option',label);option.value=value;pos.append(option);}
  const fps=el('input');fps.type='number';fps.min='1';fps.max='10';fps.value='2';fps.title=t('帧率（帧/秒）');fps.setAttribute('aria-label',t('帧率'));
  opts.append(el('span',t('帧率'),'muted'),fps,el('span',t('标注'),'muted'),pos);box.append(opts);
  const out=el('div',undefined,'gif-out');box.append(out);
  const go=btn(t('生成 GIF'),async()=>{
    go.disabled=true;go.textContent=t('正在合成…');
    try {
      const blob=await renderGif(cells,labels,pos.value as 'bottom'|'top'|'none',Math.max(1,Math.min(10,Number(fps.value)||2)));
      const url=URL.createObjectURL(blob);
      out.replaceChildren();
      const img=el('img');img.src=url;out.append(img);
      const a=el('a',t('下载 GIF'),'primary wide');a.href=url;a.download='webcompare.gif';out.append(a);
    } catch(e){error(e);} finally{go.disabled=false;go.textContent=t('生成 GIF');}
  },'primary');
  const row=el('div',undefined,'dialog-actions');row.append(go);box.append(row);
}
async function renderGif(cells:{name:string;canvas:HTMLCanvasElement}[],labels:string[],position:'bottom'|'top'|'none',fps:number):Promise<Blob> {
  const GIF=(await import('gif.js')).default;
  const gif=new GIF({workers:2,quality:8,repeat:0,workerScript:'/gif.worker.js'});
  const maxW=760;
  for(const [i,cell] of cells.entries()) {
    const scale=Math.min(1,maxW/cell.canvas.width);
    const w=Math.max(1,Math.round(cell.canvas.width*scale)),h=Math.max(1,Math.round(cell.canvas.height*scale));
    const fontSize=Math.max(12,Math.round(h/25));
    const strip=position==='none'?0:Math.round(fontSize*1.8);
    const c=document.createElement('canvas');c.width=w;c.height=h+strip;
    const x=c.getContext('2d')!;
    x.fillStyle='#fff';x.fillRect(0,0,w,h+strip);
    x.drawImage(cell.canvas,0,position==='top'?strip:0,w,h);
    if(position!=='none') {
      x.fillStyle='#000';x.font=`${fontSize}px Arial`;x.textAlign='center';x.textBaseline='middle';
      x.fillText(labels[i]||cell.name,w/2,position==='top'?strip/2:h+strip/2,w-8);
    }
    gif.addFrame(c,{delay:Math.round(1000/fps)});
  }
  return new Promise<Blob>((resolve,reject)=>{gif.on('finished',resolve);gif.on('abort',()=>reject(new Error(t('GIF 合成中断'))));gif.render();});
}
async function create(combine:'concat'|'interleave') {
  if(busy||!session)return;busy=true;clearError();picker();
  try {
    const result=await api.createComparison(selected,combine,recursive,session.csrf,undefined,mediaKind,draftImageOrder);
    draftCombine=combine;
    acceptComparison(result,smartLayout(result.images.length,combine==='interleave'?selected.length:0) as ViewState['layout']);
  }catch(e){error(e);picker();}finally{busy=false;selectionActions();}
}
async function createGroups() {
  if(busy||!session||!draftGroups.length)return;busy=true;clearError();picker();
  try {
    const result=await api.createComparison([], 'concat',recursive,session.csrf,draftGroups,mediaKind);
    acceptComparison(result,groupLayout(result.groups!.length) as ViewState['layout']);
  }catch(e){error(e);picker();}finally{busy=false;selectionActions();}
}
function acceptComparison(result:Comparison,layout:ViewState['layout']) {
  const previous=state;
  stopVideo();comparison=result;
  draftImageOrder=[...(result.selection?.image_order||[])];
  if(restoreViewOnCreate){
    if(previous.grouping!=='within'&&result.groups?.length&&cellsOf(previous.layout)<result.groups.length)layout=groupLayout(result.groups.length) as ViewState['layout'];
    else layout=previous.layout;
    const next={...previous,layout},pages=pageCount(result,layout,next);
    state=normalizeViewState(next,cellsOf(layout),pages) as ViewState;
    pageViews=Object.fromEntries(Object.entries(pageViews).filter(([key])=>Number(key)<pages));
    if(pageViewsEnabled&&state.group_index!==previous.group_index)state=pageState(state);
  }else{
    state=makeViewState() as ViewState;state.raw=normalizeRawSettings(previous.raw) as RawSettings;state.layout=layout;
    if(mediaKind==='video')state.video_frame=0;
    pageViews={};pageViewsEnabled=false;defaultPageView={view:null,panels:[]};
  }
  shareId=undefined;shareConfig=null;shareEditable=false;document.querySelector('.shared-badge')?.remove();
  setView('compare');
}
function viewsForShare():PageViews {
  rememberPageView();
  const pages=pageCount(comparison,state.layout,state),saved:PageViews={};
  for(const [key,page] of Object.entries(pageViews)){
    if(!/^(0|[1-9]\d*)$/.test(key)||Number(key)>=pages)continue;
    const normalized=normalizeViewState({...state,...page,group_index:Number(key)},cellsOf(state.layout),pages) as ViewState;
    saved[key]={view:normalized.view,panels:normalized.panels};
  }
  return saved;
}
function stateForTemplateShare():ViewState {
  const shared=viewForShare(state,cellsOf(state.layout),pageCount(comparison,state.layout,state)) as ViewState;
  if(pageViewsEnabled){
    shared.view=structuredClone(defaultPageView.view);
    shared.panels=structuredClone(defaultPageView.panels||[]);
  }
  return shared;
}
function labelled(label:string,control:HTMLElement) {const group=el('label',undefined,'control');group.append(el('span',label),control);return group;}
function rawNumber(value:number|null|undefined) { return value===null||value===undefined||!Number.isFinite(value)?t('未返回'):String(Number(value.toPrecision(6))); }
function renderRawMetadata(box:HTMLElement) {
  const entries=viewer?.rawInfo()||[];box.replaceChildren();box.hidden=!entries.length;
  if(!entries.length)return;
  box.append(el('strong',t('当前页 RAW 实际采用值（已应用）'),'raw-effective-title'));
  for(const entry of entries){
    const row=el('div',undefined,'raw-effective-row');row.dataset.assetId=entry.id;row.dataset.slot=String(entry.index+1);
    row.append(el('strong',t`第 ${entry.index+1} 格 · ${entry.name}`));
    const raw=entry.metadata?.raw;
    if(entry.error)row.append(el('p',entry.error,'raw-warning'));
    else if(!raw)row.append(el('p',t('等待解析，尚无实际采用值。'),'raw-note'));
    else {
      const resolved=raw.resolved,phases=Array.isArray(resolved.black_levels)&&resolved.black_levels.length===4;
      const black=phases?t`[${resolved.black_levels!.map(rawNumber).join(', ')}]（左上/右上/左下/右下）`:rawNumber(resolved.black_level);
      const balance=Array.isArray(resolved.white_balance)?`[${resolved.white_balance.map(rawNumber).join(', ')}]`:t('未返回');
      row.append(el('p',t`Bayer ${resolved.bayer||t('未返回')} · 黑 ${black} · 白 ${rawNumber(resolved.white_level)} · WB ${balance}`,'raw-effective-values'));
      const sources=raw.parameter_sources||{},source=(key:string)=>sources[key]?rawMessage(sources[key]):t('未提供');
      row.append(el('p',t`来源：Bayer ${source('bayer')} · 黑 ${source(phases?'black_levels':'black_level')} · 白 ${source('white_level')} · WB ${source('white_balance')}`,'raw-effective-sources'));
      if(Array.isArray(raw.metadata_source)&&raw.metadata_source.length)row.append(el('p',t`旁路：${raw.metadata_source.join(' · ')}`,'raw-effective-sources'));
      for(const warning of raw.warnings||[])row.append(el('p',rawMessage(warning),'raw-warning'));
    }
    box.append(row);
  }
}
function refreshRawMetadata() { document.querySelectorAll<HTMLElement>('.raw-dialog .raw-effective').forEach(renderRawMetadata); }
function rawPanel(forComparison=false) {
  const button=btn(t('RAW 设置'),()=>{
    const comparing=view==='compare'&&!!comparison;
    const restore=()=>window.requestAnimationFrame(()=>document.querySelector<HTMLElement>(view==='compare'?'.toolbar .raw-settings-button':'.picker-main .raw-settings-button')?.focus({preventScroll:true}));
    const box=dialog(t('RAW 设置'),restore);box.classList.add('raw-dialog');box.setAttribute('aria-modal','true');
    const close=()=>{box.closest('.dialog-backdrop')?.remove();restore();};
    box.append(rawForm(comparing,close));
    box.closest('.dialog-backdrop')!.addEventListener('keydown',event=>{if((event as KeyboardEvent).key==='Escape'){event.preventDefault();event.stopPropagation();close();}});
    window.requestAnimationFrame(()=>box.querySelector<HTMLElement>('form select,form input')?.focus({preventScroll:true}));
  },'raw-settings raw-settings-button',t('Bayer、格式、尺寸和可视化参数；草稿保留，应用后生效'));
  button.setAttribute('aria-haspopup','dialog');return button;
}
function rawForm(forComparison:boolean,close:()=>void) {
  const key=JSON.stringify([comparison?.id,shareId,rawSettingsKey(state.raw)]);
  if(key!==rawDraftKey){rawDraft=normalizeRawSettings(state.raw) as RawSettings;rawDraftKey=key;}
  const form=el('form',undefined,'raw-form');form.setAttribute('aria-label',t('RAW 解析参数'));
  const fields=el('div',undefined,'raw-fields');
  const select=(label:string,options:ReadonlyArray<readonly [string,string]>,value:string,change:(value:string)=>void)=>{
    const input=el('select');input.setAttribute('aria-label','RAW '+label);
    for(const [code,text] of options){const option=el('option',text);option.value=code;option.selected=value===code;input.append(option);}
    input.onchange=()=>change(input.value);fields.append(labelled(label,input));
  };
  const number=(label:string,value:number|null,set:(value:number|null)=>void,min?:number,max?:number,optional=false,integer=false)=>{
    const input=el('input');input.type='number';input.step=integer?'1':'any';input.required=!optional;
    if(min!==undefined)input.min=String(min);if(max!==undefined)input.max=String(max);
    input.value=value===null?'':String(value);if(optional)input.placeholder=t('自动');
    input.setAttribute('aria-label','RAW '+label);
    input.oninput=()=>{input.setCustomValidity('');set(input.value===''?null:Number(input.value));};
    fields.append(labelled(label,input));return input;
  };
  select('Bayer',[['auto',t('自动')],['RGGB','RGGB'],['BGGR','BGGR'],['GRBG','GRBG'],['GBRG','GBRG']],rawDraft.bayer,value=>{rawDraft.bayer=value as RawSettings['bayer'];});
  select(t('格式'),[['auto',t('自动')],['ax_lsb10','AX RAW10'],['mipi_raw10','MIPI RAW10'],['uint16le','16-bit LE'],['float32le','FP32 LE']],rawDraft.packing,value=>{rawDraft.packing=value as RawSettings['packing'];});
  number(t('宽度'),rawDraft.width,value=>{rawDraft.width=value;},2,100000,true,true);
  number(t('高度'),rawDraft.height,value=>{rawDraft.height=value;},2,100000,true,true);
  number(t('行宽像素'),rawDraft.stride_pixels,value=>{rawDraft.stride_pixels=value;},2,100000,true,true);
  number(t('位深'),rawDraft.bit_depth,value=>{rawDraft.bit_depth=value;},1,16,true,true);
  const rebuild=()=>form.replaceWith(rawForm(forComparison,close));
  select(t('黑电平模式'),[['scalar',t('单值 / 自动')],['phase',t('四相手动')]],rawDraft.black_levels?'phase':'scalar',value=>{
    if(value==='phase'&&!rawDraft.black_levels){rawDraft.black_levels=Array(4).fill(rawDraft.black_level??0);rawDraft.black_level=null;}
    else if(value==='scalar'&&rawDraft.black_levels){rawDraft.black_levels=null;rawDraft.black_level=null;}
    rebuild();
  });
  let white:HTMLInputElement;
  if(rawDraft.black_levels){
    [t('左上'),t('右上'),t('左下'),t('右下')].forEach((position,i)=>number(t('黑电平 ')+position,rawDraft.black_levels![i],value=>{rawDraft.black_levels![i]=value??NaN;white.setCustomValidity('');},-1e9,1e9));
  }else number(t('黑电平'),rawDraft.black_level,value=>{rawDraft.black_level=value;white.setCustomValidity('');},-1e9,1e9,true);
  white=number(t('白电平'),rawDraft.white_level,value=>{rawDraft.white_level=value;},-1e9,1e9,true);
  const ev=number('EV',Math.log2(rawDraft.gain),value=>{rawDraft.gain=value===null?NaN:2**value;},undefined,12);
  number('γ',rawDraft.gamma,value=>{rawDraft.gamma=value??NaN;},.1,8);
  select(t('白平衡模式'),[['auto',t('自动')],['manual',t('手动')]],rawDraft.white_balance===null?'auto':'manual',value=>{
    if(value==='auto')rawDraft.white_balance=null;
    else if(rawDraft.white_balance===null)rawDraft.white_balance=[1,1,1];
    rebuild();
  });
  const wbInputs=rawDraft.white_balance?['R','G','B'].map((channel,i)=>number(t('白平衡 ')+channel,rawDraft.white_balance![i],value=>{rawDraft.white_balance![i]=value??NaN;},0,64)):[];
  const controls=el('div',undefined,'raw-actions'),apply=el('button',t('应用 RAW 设置'),'primary');apply.type='submit';
  controls.append(apply,btn(t('恢复自动'),()=>{rawDraft=normalizeRawSettings() as RawSettings;rebuild();},'',t('恢复参数草稿；点击应用后生效')),btn(t('关闭'),close,'',t('关闭窗口并保留尚未应用的草稿')));
  form.onsubmit=event=>{
    event.preventDefault();
    if(rawDraft.black_level!==null&&rawDraft.white_level!==null&&rawDraft.white_level<=rawDraft.black_level)white.setCustomValidity(t('白电平须大于黑电平'));
    if(rawDraft.black_levels&&rawDraft.white_level!==null&&rawDraft.white_level<=Math.max(...rawDraft.black_levels))white.setCustomValidity(t('白电平须大于四相黑电平'));
    if(!Number.isFinite(rawDraft.gain)||rawDraft.gain<=0)ev.setCustomValidity(t('EV 超出可用范围'));
    wbInputs.forEach((input,i)=>{if(!Number.isFinite(rawDraft.white_balance![i])||rawDraft.white_balance![i]<=0)input.setCustomValidity(t('白平衡须大于 0'));});
    if(!form.reportValidity())return;
    const raw=normalizeRawSettings(rawDraft) as RawSettings,previous=normalizeRawSettings(state.raw) as RawSettings;
    const resized=(['width','height','stride_pixels','packing'] as const).some(field=>raw[field]!==previous[field]);
    clearError();update({raw,...(resized?{view:null,panels:[]}: {})});
    rawDraftKey=JSON.stringify([comparison?.id,shareId,rawSettingsKey(state.raw)]);
    if(view==='pick')picker();
  };
  form.append(fields,controls,el('p',t('自动值来自 raw.json、文件名.json 或 CaptureAX 旁路文件；缺失时会回退，不表示已校准。仅应用后生效，增益与白平衡只用于显示。'),'raw-note'),el('p',t('RAW 的像素与差分为 8-bit 显示 RGB。PNG、JPEG 和 WebP 使用图片本身的颜色。'),'raw-note'));
  if(forComparison){const effective=el('div',undefined,'raw-effective');effective.setAttribute('role','region');effective.setAttribute('aria-label',t('当前页 RAW 实际采用值'));renderRawMetadata(effective);form.append(effective);}
  return form;
}
function toolbar() {
  // 结构切换移除正在编辑的输入框时，原生 blur/change 可能同步触发 update。
  // 由外层用最新 state 完成一次构建，避免重入后重复追加整套工具与帧控制。
  if(buildingToolbar)return;
  buildingToolbar=true;
  try{buildToolbar();if(mediaKind==='video')paintQualityStatus();}finally{buildingToolbar=false;}
}
function buildToolbar() {
  const bar=document.querySelector('.toolbar')!;bar.replaceChildren();
  const bottom=document.querySelector('.toolbar-bottom')!;
  bar.id='compare-tools';bottom.id='compare-bottom';
  const groupNav=bottom.querySelector('.groups')||el('div',undefined,'groups'),statusbar=bottom.querySelector('.statusbar')!;bottom.replaceChildren(groupNav);
  bottom.classList.toggle('tools-open',viewToolsOpen);
  const primary=el('div',undefined,'tool-row'),secondary=el('div',undefined,'tool-row view-settings');
  const fullscreenActions=el('div',undefined,'fullscreen-actions');
  if(canBrowse()||canEditShare())fullscreenActions.append(btn(t('‹ 返回选图'),()=>{if(shareId)void returnToSelection();else setView('pick');},'immersive-return'));
  fullscreenActions.append(btn(t('设置'),()=>void settings()),btn(t('退出全屏'),()=>void fullscreen(),'immersive-exit',t('退出全屏（Esc）')));
  secondary.id='view-settings';
  const modes=el('div',undefined,'tool-set'),overlays=el('div',undefined,'tool-set'),transforms=el('div',undefined,'tool-set'),exports=el('div',undefined,'tool-set');
  primary.setAttribute('aria-label',t('对比与变换'));secondary.append(el('span',t('查看'),'tool-label'));
  for(const [mode,label,title] of [['split',t('并排'),t('每格一张原图，互不影响')],['wipe',t('分割'),t('每格左半当前图、右半下一张图，分割线比例可调')],['diff',t('差分'),mediaKind==='video'||currentPageHasRaw()?t('基准格原图，其余格显示与基准的显示 RGB 绝对差，增益可调'):t('基准格原图，其余格显示与基准的原始 RGB 绝对差，增益可调')]] as const)
    {const b=btn(label,()=>update({mode}),'mode-button'+(state.mode===mode?' tool-active':''),title);b.prepend(icon(mode));b.setAttribute('aria-pressed',String(state.mode===mode));modes.append(b);}
  overlays.append(el('span',t('按住叠图'),'tool-label'));
  for(const [label,d,title] of [['←','left',t('右邻槽向左覆盖当前槽（←）')],['→','right',t('左邻槽向右覆盖当前槽（→）')],['↑','up',t('下方邻槽向上覆盖当前槽（↑）')],['↓','down',t('上方邻槽向下覆盖当前槽（↓）')],['<<','seq-next',t('当前页后一槽向左覆盖，跨行连续（快捷键 Q）')],['>>','seq-prev',t('当前页前一槽向右覆盖，跨行连续（快捷键 E）')]] as const) {
    const sequence=d==='seq-next'||d==='seq-prev';
    const b=sequence?btn(label,()=>{},'tool-icon tool-sequence'):iconButton(title,d,()=>{});
    b.setAttribute('aria-label',title);b.title=(sequence?t('按序覆盖 · '):'')+title+t('；按住生效，松开恢复');
    b.onpointerdown=event=>{b.focus();b.setPointerCapture(event.pointerId);viewer?.hold(d);};
    b.onpointerup=b.onpointercancel=()=>viewer?.hold(null);
    b.onkeydown=event=>{if(event.key===' '||event.key==='Enter'){event.preventDefault();viewer?.hold(d);}};
    b.onkeyup=event=>{if(event.key===' '||event.key==='Enter'){event.preventDefault();viewer?.hold(null);}};b.onblur=()=>viewer?.hold(null);
    overlays.append(b);
  }
  if(state.mode==='wipe')modes.append(btn(state.direction===1?t('邻图 →'):t('邻图 ←'),()=>update({direction:state.direction===1?-1:1}),'',t('切换分割所取的相邻方向')));
  if(state.mode==='diff') {
    const baseline=el('select',undefined,'diff-baseline');baseline.setAttribute('aria-label',t('差分基准'));baseline.title=t('基准路保留原图，其余各路显示与它的绝对差分');
    (slotAssets(comparison,state) as string[]).forEach((id,index)=>{const option=el('option',t`第${index+1}格 · ${comparison?.assets[id]?.name||t('缺图')}`);option.value=String(index);option.selected=state.diff_base===index;baseline.append(option);});
    baseline.onchange=()=>update({diff_base:Number(baseline.value)});
    modes.append(labelled(t('基准'),baseline));
  }
  if(comparison?.groups?.length)modes.append(iconButton(state.grouping==='within'?t('同场景分组'):t('同关键词分组'),state.grouping==='within'?'within':'aligned',()=>update({grouping:state.grouping==='within'?'aligned':'within',group_index:0,diff_base:0}),'tool-active',state.grouping==='within'?t('当前逐组查看组内图片；点击切为跨组按序号对齐'):t('当前跨组按序号对齐；点击切为逐组查看组内图片')));
  if(comparison)modes.append(iconButton(t('智能排序'),'sort',()=>update({natural_sort:!state.natural_sort,group_index:0,diff_base:0}),state.natural_sort?'tool-active':'',t('按名称中的数字顺序排列组和图片；再次点击恢复原顺序')));
  const layout=el('select',undefined,'layout-select');layout.setAttribute('aria-label',t('布局'));
  const groupCount=state.grouping==='within'?0:comparison?.groups?.length||0;
  const available=layoutOptions().filter(option=>!groupCount||option.rows*option.columns>=groupCount);
  if(!available.some(option=>option.value===state.layout)){
    const [rows,columns]=state.layout.split('x').map(Number);available.unshift({value:state.layout,rows,columns});
  }
  for(const option of available) {
    const item=el('option',t`${option.rows}行×${option.columns}列`);item.value=option.value;item.selected=state.layout===option.value;layout.append(item);
  }
  const custom=el('option',t('自定义行列…'));custom.value='custom';layout.append(custom);
  layout.onchange=()=>{if(layout.value==='custom'){layout.value=state.layout;showCustomLayout(groupCount);}else update({layout:layout.value as ViewState['layout']});};
  modes.insertBefore(labelled(t('布局'),layout),modes.children[3]||null);
  if(mediaKind==='image'){
    const selection=el('select');selection.setAttribute('aria-label',t('操作范围'));selection.title=t('双击图像框选中或取消；选中后缩放、拖动仅联动选中格与鼠标所在格');
    const all=el('option',t('全部图像'));all.value='';selection.append(all);
    slotAssets(comparison,state).forEach((id:string|null,index:number)=>{if(id){const option=el('option',t`第 ${index+1} 格`);option.value=String(index);selection.append(option);}});
    selection.value=viewer?.selectedIndex===null||viewer?.selectedIndex===undefined?'':String(viewer.selectedIndex);
    selection.onchange=()=>viewer?.select(selection.value===''?null:Number(selection.value));
    transforms.append(labelled(t('操作'),selection));
  }
  transforms.append(iconButton(t('旋转 90°'),'rotate',()=>viewer?.rotate(),'',t('顺时针旋转选中图像；未选中时旋转全部图像')),iconButton(t('左右翻转'),'flip-x',()=>viewer?.flip('x'),'',t('沿屏幕水平方向镜像')),iconButton(t('上下翻转'),'flip-y',()=>viewer?.flip('y'),'',t('沿屏幕垂直方向镜像')));
  transforms.append(iconButton(t('对齐位置'),'position',()=>viewer?.align(false),'',t('以选中图像或第一张图为参考，对齐左上角，保持各图显示大小')),iconButton(t('对齐大小'),'size',()=>viewer?.align(true),'',t('以选中图像或第一张图为参考，复制显示位置和宽高')));
  const brightnessOn=state.brightness_ref!==null;
  const brightness=iconButton(t('对齐亮度'),'brightness',()=>update({brightness_ref:brightnessOn?null:viewer?.selectedIndex??0}),brightnessOn?'tool-active':'',mediaKind==='video'?t('仅支持图像'):brightnessOn?t`已按第 ${state.brightness_ref!+1} 格的全图 p99 对齐；参考格缺图时使用首张图；点击恢复`:t('以选中格或第 1 格的全图 p99 对齐当前页亮度；再次点击恢复'));
  brightness.disabled=mediaKind==='video';brightness.setAttribute('aria-pressed',String(brightnessOn));transforms.append(brightness);
  const channel=el('select');channel.setAttribute('aria-label',t('通道'));['rgb','r','g','b'].forEach(v=>{const o=el('option',v.toUpperCase());o.value=v;o.selected=state.channel===v;channel.append(o);});channel.onchange=()=>update({channel:channel.value as ViewState['channel']});
  secondary.append(labelled(t('通道'),channel));
  secondary.append(iconButton(t('直方图'),'histogram',event=>{histogramOn=viewer?.toggleHistogram()??false;(event?.currentTarget as HTMLButtonElement)?.classList.toggle('tool-active',histogramOn);},histogramOn?'tool-active':'',mediaKind==='video'?t('从完整视频帧采样 RGB 直方图'):t('基于当前已解码的预览位图现场统计 RGB 直方图，不额外请求网络')));
  secondary.append(el('span',undefined,'separator'),iconButton(t('0 适应'),'fit',()=>viewer?.fit(),'',t('完整显示选中图像；未选中时适应全部图像，保持纵横比')),iconButton(t('F 填满'),'fill',()=>viewer?.fill(),'',t('选中图像或全部图像铺满图框，纵横比不同时居中裁边')),iconButton(t('1 原像素'),'pixel',()=>viewer?.oneToOne(),'',t('选中图像或全部图像，一个原图像素对应一个屏幕像素')));
  const quality=el('select');quality.setAttribute('aria-label',t('画质'));
  for(const [value,label] of [['auto',t('自适应')],['exact',t('无损')],['high',t('高清')]] as const){const option=el('option',label);option.value=value;option.selected=state.quality===value;quality.append(option);}
  quality.onchange=()=>update({quality:quality.value as ViewState['quality']});if(mediaKind==='image')secondary.append(labelled(t('画质'),quality));
  const edge=el('label',undefined,'icon-check');const edgeBox=el('input');edgeBox.type='checkbox';edgeBox.checked=edgeDiff;edgeBox.onchange=()=>{edgeDiff=edgeBox.checked;viewer?.setEdgeDiff(edgeDiff);};edgeBox.setAttribute('aria-label',t('边缘差分'));edge.append(edgeBox,icon('edge'));
  edge.title=t('边缘差分 · 按住叠图时，无邻槽可覆盖的边缘槽默认保持原图；勾选后显示与内侧邻居的绝对差分');overlays.append(edge);
  if(canBrowse())exports.append(iconButton(t('分享'),'share',showShare,'',t('生成分享链接，保存当前页码、布局和查看参数')));
  exports.append(btn(t('导出 PNG'),snapshot,'',t('保存当前画布，包含布局、标题、对比模式与缩放位置')),btn('GIF',showGif,'',t('把当前页各槽按屏上画面导出为 GIF（可改简称、可选标注位置）')));
  const ranges:Array<readonly [keyof Pick<ViewState,'exposure'|'gamma'|'gain'|'opacity'|'wipe'>,string,number,number,number]>=[['exposure',t('曝光'),-8,8,.1],['gamma','γ',.1,4,.05]];
  if(state.mode==='diff')ranges.push(['gain',t('差分增益'),1,128,1]);
  if(state.mode==='wipe')ranges.push(['wipe',t('分割'),0,1,.01]);
  for(const [key,label,min,max,step] of ranges) {
    const wrap=el('div',undefined,'range-wrap'),range=el('input'),value=el('output',String(state[key]));
    range.type='range';range.min=String(min);range.max=String(max);range.step=String(step);range.value=String(state[key]);range.setAttribute('aria-label',label);range.dataset.stateKey=key;
    range.oninput=()=>{value.textContent=range.value;update({[key]:Number(range.value)});};
    value.title=t('双击直接输入数值');
    value.ondblclick=()=>{
      const edit=el('input');edit.type='number';edit.min=String(min);edit.max=String(max);edit.step=String(step);edit.value=String(state[key]);edit.className='range-edit';
      edit.setAttribute('aria-label',label+t('数值'));
      const commit=()=>{const n=Number(edit.value);if(Number.isFinite(n))update({[key]:Math.max(min,Math.min(max,n))});toolbar();};
      edit.onkeydown=ev=>{if(ev.key==='Enter')commit();else if(ev.key==='Escape')toolbar();};
      edit.onblur=commit;
      value.replaceWith(edit);edit.focus();edit.select();
    };
    wrap.append(range,value);secondary.append(labelled(label,wrap));
  }
  secondary.append(btn(t('重置视图'),()=>{viewer?.select(null);update({view:null,panels:[],quality:'auto',channel:'rgb',exposure:0,gamma:1,brightness_ref:null,gain:4,wipe:.5});},'',t('还原全部图像的选中、旋转、翻转、缩放、平移、曝光、亮度对齐、通道与差分增益')));
  for(const [key,label] of [['titles',t('文件名')],['details',t('倍率/尺寸')],['coordinates',t('坐标/状态')]] as const) {
    const input=el('input');input.type='checkbox';input.checked=overlayOptions[key];input.setAttribute('aria-label',t('显示')+label);
    input.onchange=()=>{overlayOptions[key]=input.checked;localStorage.setItem('wc-overlay-'+key,String(input.checked));applyOverlays();};secondary.append(labelled(label,input));
  }
  const more=btn(t('参数'),()=>{viewToolsOpen=!viewToolsOpen;toolbar();},'view-settings-toggle',t('通道、画质、曝光与缩放'));more.setAttribute('aria-expanded',String(viewToolsOpen));more.setAttribute('aria-controls','view-settings');
  primary.append(modes,overlays,transforms,exports);bar.append(fullscreenActions,primary);if(mediaKind==='image')bar.append(rawPanel(true));bottom.append(secondary,more,statusbar);if(comparison?.media_kind==='video')bottom.append(videoControls());
}
function groups() {
  const box=document.querySelector('.groups')!;box.replaceChildren();
  if(railW<64||compactScreen.matches)box.append(btn(compactScreen.matches&&mobileRailOpen?'«':'»',()=>{if(compactScreen.matches)mobileRailOpen=!mobileRailOpen;if(railW<64)railW=150;localStorage.setItem('wc-railw',String(railW));applyRailW();groups();},'preview-toggle',compactScreen.matches&&mobileRailOpen?t('收起缩略图栏'):t('展开缩略图栏')));
  const total=comparison?.images.length||0,pages=pageCount(comparison,state.layout,state);
  box.append(btn('‹',()=>update(nextGroup(state,pages,-1)),'group-arrow',t('上一页（Ctrl+←）')));
  const page=pageInfo(comparison,state.layout,state.group_index,state),start=page.start,end=page.end;
  const label=comparison?.groups?.length?(state.grouping==='within'?`${comparison.groups[page.groupIndex]?.label||t('空组')} · ${page.groupPage+1}/${page.groupPages}`:t`第 ${state.group_index+1} 序号 · ${comparison.groups.length} 组`):total?t`图 ${start+1}–${end} / ${total}`:'';
  const pageLabel=el('span',label,'page-info');pageLabel.title=label;box.append(pageLabel);
  const jump=el('input');jump.type='number';jump.min='1';jump.max=String(pages);jump.value=String(state.group_index+1);jump.setAttribute('aria-label',t('跳转页'));
  jump.onchange=()=>update({group_index:Number(jump.value)-1});
  box.append(jump,el('span','/ '+pages+t(' 页')),btn('›',()=>update(nextGroup(state,pages,1)),'group-arrow',t('下一页（Ctrl+→）')));
}
function showCustomLayout(minimumCells=0) {
  const original=state.layout,shape=layoutShape(original),box=dialog(t('自定义布局'));
  box.classList.add('layout-dialog');box.setAttribute('aria-modal','true');
  const form=el('form'),fields=el('div',undefined,'layout-fields');
  const input=(label:string,value:number)=>{const field=el('input');field.type='number';field.min='1';field.max='64';field.step='1';field.required=true;field.value=String(value);field.setAttribute('aria-label',label);return field;};
  const rows=input(t('行数'),shape.rows),columns=input(t('列数'),shape.columns),preview=el('p',undefined,'muted');preview.setAttribute('aria-live','polite');
  fields.append(labelled(t('行'),rows),el('span','×'),labelled(t('列'),columns));
  const describe=()=>{const layout=`${rows.value}x${columns.value}`,count=Number(rows.value)*Number(columns.value);preview.textContent=validLayout(layout)?t`每页 ${count} 格 · 共 ${pageCount(comparison,layout,state)} 页`:t('请输入正整数行列（每页最多 64 格）');};
  rows.oninput=columns.oninput=describe;describe();
  const apply=el('button',t('应用'),'primary');apply.type='submit';
  form.append(fields,preview,el('p',t`每页最多 64 格，更多图片自动分页。${minimumCells?t`当前跨组对齐至少需要 ${minimumCells} 格。`:''}`,'muted'),apply);
  form.onsubmit=event=>{event.preventDefault();const layout=`${rows.value}x${columns.value}`;
    if(!validLayout(layout)){error(t('行列需为正整数，每页最多 64 格'));return;}
    if(cellsOf(layout)<minimumCells){error(t`当前跨组对齐至少需要 ${minimumCells} 格`);return;}
    box.closest('.dialog-backdrop')?.remove();update({layout:layout as ViewState['layout']});
  };
  form.onkeydown=event=>{if(event.key==='Escape'){event.preventDefault();box.closest('.dialog-backdrop')?.remove();document.querySelector<HTMLSelectElement>('.layout-select')?.focus();}};
  box.append(form);rows.focus();rows.select();
}
function dialog(title:string,onClose?:()=>void) {
  stopShares();
  document.querySelector('.dialog-backdrop')?.remove();
  const backdrop=el('div',undefined,'dialog-backdrop'),box=el('section',undefined,'dialog');
  box.setAttribute('role','dialog');box.setAttribute('aria-label',title);
  const close=()=>{stopShares();onClose?.();backdrop.remove();};
  const header=el('div',undefined,'dialog-title');header.append(el('h2',title),btn('×',close,'icon-btn'));
  box.append(header);backdrop.append(box);app.append(backdrop);
  backdrop.onclick=e=>{if(e.target===backdrop)close();};return box;
}
const VISIBILITY_LABEL:Record<Visibility,string>={personal:'个人',internal:'内部',public:'公开'};
function visibilityOptions(role:Role):Array<[Visibility,string]> {
  if(role==='admin')return[['personal',t('个人（仅自己）')],['internal',t('内部（登录成员）')],['public',t('公开（任何人）')]];
  if(role==='guest')return[['personal',t('个人（仅自己）')],['public',t('公开（任何人）')]];
  return[['personal',t('个人（仅自己）')],['internal',t('内部（登录成员）')]];
}
function shareRow(record:LinkShareRecord,reload:()=>Promise<void>):HTMLElement {
  const row=el('div',undefined,'share-row');row.dataset.kind='link';row.dataset.id=record.id;
  const href=new URL(record.url,location.origin).href;
  const link=el('span',href,'share-url');link.title=href;
  const select=el('select');select.setAttribute('aria-label',t('可见范围'));select.title=t('修改这条链接的可见范围');
  const options=visibilityOptions(session!.user.role);
  if(!options.some(([value])=>value===record.visibility))options.unshift([record.visibility,t`${t(VISIBILITY_LABEL[record.visibility])}（当前）`]);
  for(const [value,label] of options){const option=el('option',label);option.value=value;option.selected=value===record.visibility;select.append(option);}
  select.onchange=async()=>{
    const next=select.value as Visibility;
    try {const result=await api.patchShare(record.id,next,session!.csrf);record.visibility=result.visibility||next;}
    catch(e){error(e);select.value=record.visibility;}
  };
  row.append(el('strong',serverMode()?t('链接'):record.selection_mode==='editable'?t('配置'):t('固定')),link,select,
    btn(t('复制'),()=>{void navigator.clipboard?.writeText(href).catch(()=>undefined);},'',t('复制链接')),
    btn(t('撤销'),async()=>{
      if(!confirm(t('撤销这条分享链接？撤销后旧链接立即失效。')))return;
      try {await api.deleteShare(record.id,session!.csrf);await reload();}
      catch(e){error(e);}
    },'danger'));
  return row;
}
function stopShares(){window.clearTimeout(sharesTimer);sharesTimer=undefined;sharesController?.abort();sharesController=null;}
function zipRow(record:ExportRecord,remove:()=>Promise<void>):HTMLElement {
  const row=el('div',undefined,'share-row zip-row');row.dataset.kind='zip';row.dataset.id=record.id;
  const detail=el('div',undefined,'zip-detail'),status=el('span',undefined,'zip-status');status.setAttribute('role','status');
  const label=()=>record.status==='ready'?t`可下载 · ${fmtSize(record.bytes||0)}`:record.status==='failed'?t('失败'):record.status==='queued'?t('压缩中 · 排队中'):t('压缩中');
  status.textContent=label();detail.append(el('strong',record.purpose==='files'?t('原文件 ZIP'):t('ZIP 分享包')),status);
  if(record.status==='queued'||record.status==='compressing'){
    const progress=el('progress');progress.max=record.total_bytes||1;progress.value=Math.min(record.processed_bytes||0,progress.max);progress.setAttribute('aria-label',t('打包压缩进度'));
    detail.append(progress,el('small',t`已处理 ${fmtSize(record.processed_bytes||0)} / 原图总量 ${fmtSize(record.total_bytes||0)}`,'muted'));
  }
  if(record.status==='failed')detail.append(el('p',serverMessage(record.error,t('打包失败，请重新尝试。')),'zip-error'));
  row.append(detail);
  if(record.status==='ready'){
    const download=el('a',t('下载'),'zip-download');download.href=record.download_url||`/api/exports/${encodeURIComponent(record.id)}/download`;download.download=record.purpose==='files'?'webcompare-files.zip':'webcompare-share.zip';
    download.onclick=()=>{status.textContent=t('下载中 · 请在浏览器中查看下载状态');window.setTimeout(()=>{if(status.isConnected)status.textContent=label();},5000);};
    row.append(download);
  }
  row.append(btn(t('删除'),async()=>{if(!confirm(t('删除这项打包任务？')))return;try{await remove();}catch(e){error(e);}},'danger'));
  return row;
}
function shareList(box:HTMLElement,waiting=false) {
  const owner=session!,controller=new AbortController();sharesController=controller;
  const feedback=el('p',undefined,'share-feedback muted'),list=el('div',undefined,'share-list');feedback.setAttribute('role','status');box.append(feedback,list);
  let records:ShareRecord[]=[],loading=true,pending=waiting,message='',creationError='',newestTask='';
  const tasks=new Map<string,ExportRecord>(),deleted=new Set<string>();
  const alive=()=>box.isConnected&&session===owner&&!controller.signal.aborted;
  const all=()=>{const result=new Map(records.filter(record=>record.kind==='zip'?!deleted.has(record.id):!record.revoked).map(record=>[(record.kind||'link')+':'+record.id,record]));tasks.forEach(task=>result.set('zip:'+task.id,task));return Array.from(result.values());};
  const paint=()=>{
    if(!alive())return;
    feedback.textContent=pending?t('正在创建打包任务…'):creationError||message|| (loading?t('正在读取…'):'');list.replaceChildren();
    const entries=all();
    const newest=entries.findIndex(record=>record.kind==='zip'&&record.id===newestTask);
    if(newest>0)entries.unshift(...entries.splice(newest,1));
    if(!entries.length&&!loading&&!pending&&!message)list.append(el('p',t('还没有分享。'),'muted'));
    entries.forEach(record=>list.append(record.kind==='zip'?zipRow(record,async()=>{
      await api.deleteExport(record.id,owner.csrf);if(!alive())return;
      deleted.add(record.id);tasks.delete(record.id);records=records.filter(item=>!(item.kind==='zip'&&item.id===record.id));paint();void refreshQuota();await reload();
    }):shareRow(record,reload)));
  };
  const schedule=()=>{
    window.clearTimeout(sharesTimer);
    if(alive()&&all().some(record=>record.kind==='zip'&&(record.status==='queued'||record.status==='compressing')))sharesTimer=window.setTimeout(()=>void poll(),1000);
  };
  const poll=async()=>{
    if(!alive())return;
    const active=all().filter((record):record is ExportRecord=>record.kind==='zip'&&(record.status==='queued'||record.status==='compressing'));
    const results=await Promise.allSettled(active.map(record=>api.exportTask(record.id,controller.signal)));
    if(!alive())return;
    let failed=false;
    results.forEach(result=>{if(result.status==='fulfilled'){if(!deleted.has(result.value.id))tasks.set(result.value.id,result.value);}else failed=true;});
    message=failed?t('打包状态更新暂不可用，正在重试。'):'';paint();schedule();
  };
  const reload=async()=>{
    try{const result=await api.shares(controller.signal);if(!alive())return;records=result.shares||[];message='';}
    catch(e){if(!alive())return;message=t('我的分享列表暂不可用：')+(e instanceof Error?e.message:String(e));}
    if(!alive())return;loading=false;paint();schedule();
  };
  paint();void reload();
  return {reload,created:(record:ExportRecord)=>{if(!alive())return;pending=false;newestTask=record.id;tasks.set(record.id,record);paint();schedule();},failed:(e:unknown)=>{if(!alive())return;pending=false;creationError=t('打包失败：')+(e instanceof Error?e.message:String(e));paint();}};
}
function showMine(waiting=false){return shareList(dialog(t('我的分享')),waiting);}
function showShare() {
  stopVideo();
  if(!comparison||!session||!canBrowse())return;
  const managed=serverMode();
  let selectionMode:SelectionMode='fixed';
  const description=el('p',managed?t('保存当前图片清单、页码、布局和查看参数，按下方可见范围打开分享。'):t('固定分享保存当前图片清单和查看状态，查看者看不到目录路径。'));
  const box=dialog(t('创建分享模板'));box.append(description);
  if(temporaryComparisons.has(comparison))box.append(el('p',t('叉选仅影响本次页面；分享链接和 ZIP 仍使用原始图片清单。'),'muted'));

  if(!managed)box.append(segmented(t('分享方式'),[['fixed',t('固定分享（隐藏路径）')],['editable',t('可编辑配置（可返回选图）')]],selectionMode,value=>{
    selectionMode=value as SelectionMode;
    description.textContent=selectionMode==='editable'?t('保存路径、选图规则和各页查看位置，打开时读取这些路径中的当前图片。成员可以返回选图，微调后生成新的对比和链接。'):t('固定分享保存当前图片清单和查看状态，查看者看不到目录路径。');
  }));
  let visibility:Visibility='personal';
  if(managed) {
    const radios=el('div',undefined,'radio-row');
    for(const [value,label] of visibilityOptions(session.user.role)) {
      const wrap=el('label',undefined,'check'),radio=el('input');
      radio.type='radio';radio.name='wc-visibility';radio.value=value;radio.checked=value===visibility;
      radio.onchange=()=>{visibility=value;};
      wrap.append(radio,el('span',label));radios.append(wrap);
    }
    box.append(el('div',t('可见范围'),'field-label'),radios);
    if(session.user.role==='guest')box.append(el('p',t('访客内容到期后链接即失效。'),'muted'));
  }
  const url=el('input');url.readOnly=true;url.placeholder=t('点击生成链接');url.setAttribute('aria-label',t('分享链接'));box.append(url);
  let generated:string|undefined;
  const row=el('div',undefined,'dialog-actions');
  const zip=btn(t('打包分享 zip'),async()=>{
    const owner=session!,current=comparison!,shared=viewForShare(state,cellsOf(state.layout),pageCount(current,state.layout,state)) as ViewState;
    const total=Array.from(new Set(slotAssets(current,shared) as Array<string|null>)).reduce((bytes,id)=>bytes+(id?current.assets[id]?.bytes||0:0),0);
    if(total>(owner.user.role==='admin'?1073741824:104857600)){error(t('文件尺寸过大不可zip打包分享'));return;}
    const mine=showMine(true);
    try {
      const task=await api.createExport(current.id,shared,owner.csrf,visibility);if(session!==owner)return;mine.created(task);void refreshQuota();
    } catch(e){mine.failed(e);}
  },'',t('打包当前页原图和查看模板，在“我的分享”查看进度并下载'));
  if(mediaKind==='video'){zip.disabled=true;zip.title=t('视频请用分享链接或当前帧截图；ZIP 分享包暂只支持图片');}
  row.append(zip);
  const revoke=btn(t('撤销此链接'),async()=>{if(!generated)return;try{await api.deleteShare(generated,session!.csrf);url.value=t('已撤销');revoke.disabled=true;await mine.reload();}catch(e){error(e);}});revoke.disabled=true;
  const generate=btn(t('生成链接'),async()=>{
    generate.disabled=true;
    try {
      const shared=selectionMode==='editable'?stateForTemplateShare():viewForShare(state,cellsOf(state.layout),pageCount(comparison,state.layout,state)) as ViewState;
      const result=await api.share(comparison!.id,shared,session!.csrf,visibility,selectionMode,selectionMode==='editable'?viewsForShare():undefined);
      generated=result.id;url.value=new URL(result.url,location.origin).href;revoke.disabled=false;url.select();
      await navigator.clipboard?.writeText(url.value).catch(()=>undefined);
      void refreshQuota();await mine.reload();
    }
    catch(e){error(e);}finally{generate.disabled=false;}
  },'primary');
  row.append(revoke,generate);box.append(row);
  box.append(el('div',t('我的分享'),'section-title side-section'));
  const mine=shareList(box);
}
function decoderSetup() {
  return session?.nvdec_setup ?? (session?.local_nvdec?'ready':undefined);
}

function selectDecoder(value: string) {
  decoderChoice=value;localStorage.setItem('wc-video-decoder',value);
  configureLocalDecoder(value==='auto'&&session?.local_nvdec?session.csrf:undefined);
  if(view==='compare' && mediaKind==='video' && viewer) {
    stopVideo();
    viewer.suspend();
    viewer.update(comparison,state,shareId);
  }
  refreshDecoderControls();
}

async function installDecoder() {
  const owner=session;
  if(!owner || installingDecoder || decoderSetup()!=='installable')return;
  installingDecoder=true;
  refreshDecoderControls();
  const initialChoice=decoderChoice;
  try {
    await api.installNVDEC(owner.csrf);
    if(session!==owner)return;
    owner.local_nvdec=true;owner.nvdec_setup='ready';
    if(initialChoice===decoderChoice)selectDecoder('auto');
    info(t('本机硬解已安装'));
  } catch(e) {if(session===owner)error(e);}
  finally {installingDecoder=false;refreshDecoderControls();}
}

function decoderControl() {
  const group=el('div',undefined,'decoder-control');
  const setup=decoderSetup();
  if(setup!=='ready' && setup!=='installable') {
    const result=el('span',t('浏览器解码'),'decoder-browser');
    result.title=setup==='no_gpu'?t('本机未检测到可用的 NVIDIA 驱动或显卡'):
      setup==='disabled'?t('管理员已关闭本机硬解'):
      setup==='unsupported'?t('此运行环境使用浏览器解码，可在服务环境手动安装兼容的 PyAV'):t('视频由当前浏览器解码');
    group.append(result);return group;
  }
  const select=el('select',undefined,'video-decoder-choice');
  select.setAttribute('aria-label',t('视频解码'));
  select.title=t('自动优先本机 NVIDIA 硬解，每路可独立回退到浏览器。切换时暂停并保留当前帧。');
  for(const [value,text] of [['auto',t('自动')],['browser',t('仅用浏览器')]]) {
    const option=el('option',text);option.value=value;select.append(option);
  }
  select.onchange=()=>selectDecoder(select.value);
  group.append(labelled(t('解码'),select));
  if(setup==='installable')group.append(btn(t('安装并启用硬解'),()=>void installDecoder(),'decoder-install',t('从 PyPI 下载 PyAV（约 28–35 MB），安装后即可使用')));
  paintDecoderGroup(group,autoDecoderLabel());return group;
}

function autoDecoderLabel() {
  const lanes=view==='compare' && mediaKind==='video' ? viewer?.videoInfo().filter(item=>item?.count)||[] : [];
  const hardware=lanes.filter(item=>item?.decoder==='NVDEC').length;
  if(hardware)return t`自动 · NVDEC 硬解 ${hardware}/${lanes.length}`;
  if(lanes.some(item=>item?.fallback))return t('自动 · 已回退浏览器');
  return decoderSetup()==='ready' && !lanes.length?t('自动 · 优先硬解'):t('自动 · 浏览器解码');
}

function paintDecoderGroup(group:Element,automatic:string) {
  const select=group.querySelector<HTMLSelectElement>('.video-decoder-choice');
  if(!select)return;
  if(select.options[0].text!==automatic)select.options[0].text=automatic;
  if(select.value!==decoderChoice)select.value=decoderChoice;
  const install=group.querySelector<HTMLButtonElement>('.decoder-install');
  if(install){
    if(decoderSetup()==='ready'){install.remove();return;}
    install.disabled=installingDecoder;
    install.textContent=installingDecoder?t('安装中…'):t('安装并启用硬解');
  }
}

function refreshDecoderControls() {
  const automatic=autoDecoderLabel();
  document.querySelectorAll('.decoder-control').forEach(group=>paintDecoderGroup(group,automatic));
}

async function settings() {
  const box=dialog(t('设置'));box.classList.add('settings-dialog');
  box.append(languageControl());
  if(session?.nvdec_setup || session?.local_nvdec)box.append(decoderControl());
  const licenses=el('a',t('第三方软件与开源许可'));licenses.href='/licenses/NOTICE.txt';licenses.target='_blank';licenses.rel='noopener';box.append(licenses);
  const display=el('div',undefined,'display-settings');
  const showNames=el('input');showNames.type='checkbox';showNames.checked=overlayOptions.titles;showNames.setAttribute('aria-label',t('显示文件名'));
  showNames.onchange=()=>{overlayOptions.titles=showNames.checked;localStorage.setItem('wc-overlay-titles',String(showNames.checked));applyOverlays();toolbar();};
  display.append(labelled(t('显示文件名'),showNames));
  const size=el('input');size.type='number';size.min='.5';size.step='.25';size.value=String(titleScale);size.setAttribute('aria-label',t('文件名大小（倍）'));
  const range=el('input');range.type='range';range.min='1';range.max='4';range.step='.25';range.value=String(titleScale);range.setAttribute('aria-label',t('文件名大小滑块'));
  const pixels=el('output',`${Math.round(14*titleScale)} px`);
  const resize=(value:number,fromNumber=false)=>{
    if(!Number.isFinite(value)||value<.5)return;
    titleScale=value;localStorage.setItem('wc-title-scale',String(value));if(!fromNumber)size.value=String(value);range.value=String(value);pixels.textContent=`${Math.round(14*value)} px`;applyOverlays();
  };
  size.oninput=()=>{if(size.value)resize(Number(size.value),true);};size.onchange=()=>{size.value=String(titleScale);};range.oninput=()=>resize(Number(range.value));
  const sizing=el('div',undefined,'title-sizing');sizing.append(size,el('span',t('倍')),range,pixels,btn(t('恢复默认'),()=>resize(1)));
  display.append(labelled(t('文件名大小'),sizing),el('p',t('文件名按格子宽度自动换行。常用范围 1–4 倍，也可直接输入更大数值；格子放不下时在末行省略。设置保存在本浏览器。'),'muted'));
  box.append(el('div',t('图像标注'),'field-label'),display);
  renderUpdates(box,!!providers?.native_desktop,session?.csrf);
  if(!canBrowse())return;
  const cacheInfo=el('p',t('正在读取缓存统计…'));box.append(cacheInfo);
  const row=el('div',undefined,'dialog-actions');
  const clear=btn(t('清理服务器缓存'),async()=>{
    clear.disabled=true;
    try{const r=await api.clearCache(session!.csrf);cacheInfo.textContent=t`已清理 ${r.removed} 个文件，释放 ${(r.bytes/1048576).toFixed(1)} MB`;}
    catch(e){error(e);}finally{clear.disabled=false;}
  });clear.disabled=!canBrowse();
  row.append(clear);box.append(row);
  box.append(el('p',t('说明：服务器缓存保存预览图与差分结果（有上限，自动淘汰）；浏览器端缩略图/解码位图也有内存上限并自动回收。'),'muted'));
  try{const s=await api.cache();cacheInfo.textContent=t`服务器缓存：${(s.bytes/1048576).toFixed(1)} MB · ${s.files} 个文件（上限由 WEBCOMPARE_CACHE_MIB 控制）`;}
  catch(e){cacheInfo.textContent=String(e);}
}
window.addEventListener('keydown',e=>{
  if(e.key==='Escape'&&immersive&&!document.querySelector('.dialog-backdrop')){e.preventDefault();void fullscreen();return;}
  const key=e.key.toLowerCase();
  if(!comparison||view!=='compare'||document.querySelector('.dialog-backdrop'))return;
  const frameTargetShortcut=(e.target as Element|null)?.classList?.contains('video-frame-target')&&[',','.','escape'].includes(key);
  if(isInput(e.target)&&!frameTargetShortcut)return;
  if(mediaKind==='video'&&(key===','||key==='.')){e.preventDefault();stopVideo();stepFrame(key===','?-1:1);return;}
  if(key==='escape'){viewer?.select(null);return;}
  if(key==='f'&&e.shiftKey){e.preventDefault();void fullscreen();return;}
  if(['arrowleft','arrowright','arrowup','arrowdown','q','e','f','0','1'].includes(key))e.preventDefault();
  if(key.startsWith('arrow')&&(e.ctrlKey||e.metaKey)){update(nextGroup(state,pageCount(comparison,state.layout,state),key==='arrowleft'?-1:1));return;}
  const held=(({arrowleft:'left',arrowright:'right',arrowup:'up',arrowdown:'down',q:'seq-next',e:'seq-prev'}) as const)[key];
  if(held)viewer?.hold(held);
  else if(key==='f')viewer?.fill();else if(key==='0')viewer?.fit();else if(key==='1')viewer?.oneToOne();
});
window.addEventListener('keyup',e=>{if(['arrowleft','arrowright','arrowup','arrowdown','q','e'].includes(e.key.toLowerCase()))viewer?.hold(null);});
window.addEventListener('blur',()=>viewer?.hold(null));
document.addEventListener('fullscreenchange',()=>{immersive=!!document.fullscreenElement&&view==='compare';paintImmersive();});
window.addEventListener('popstate',()=>location.reload());
async function boot() {
  try {providers=await api.providers();}
  catch {providers=null;}   // 旧后端或 providers 不可用时退回 personal 交互
  if(shareId) {
    try {session=await api.session();}
    catch(e){session=null;const status=(e as Error & {status?:number}).status;if(status!==401&&status!==403)error(e);}
    await initialize();
    return;
  }
  try {
    session=await api.session();
    await initialize();
  } catch{login();}
}
void boot();

document.addEventListener('visibilitychange',()=>{if(document.hidden){stopVideo();if(comparison?.media_kind==='video')toolbar();}});

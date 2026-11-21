import { t } from './i18n.js';
import { api, type Comparison, type Metadata, type ViewState, type PanelState, type RenderQuality } from './api';
import { VideoFrames } from './video';
import { GpuEffects } from './gpu-effects.js';
import { layoutShape, makeViewState, panelPlan, slotAssets, pageInfo, orderedGroupIndices, isRawPath, rawSettingsKey, imageDisplayQuality } from './state.js';

type Rect = NonNullable<ViewState['view']>;
type Picture = { key: string; bitmap: ImageBitmap; box: Rect; width: number; height: number; used: number; quality?: RenderQuality; p99?: number; thumbnails?: Map<number, HTMLCanvasElement> };
type Context = { token: number; signal: AbortSignal; state: ViewState; share?: string; size: number; full: boolean };
type Plan = { kind: string; left: string | null; right: string | null };
type EffectSource = HTMLCanvasElement | ImageBitmap;
type Cell = { x: number; y: number; width: number; height: number };
const SERVER_CACHE_LIMIT = 96 * 1024 * 1024;
const GAP = 2;
const abortError = () => new DOMException('Cancelled', 'AbortError');
async function runLimited<T>(items:T[],limit:number,task:(item:T)=>Promise<void>,signal?:AbortSignal) {
  let cursor=0;
  await Promise.all(Array.from({length:Math.min(limit,items.length)},async()=>{
    while(cursor<items.length&&!signal?.aborted){const item=items[cursor++];await task(item);}
  }));
}

/** Image coordinates and fetch lifetime live here; transient keys never mutate saved state. */
export class Viewer {
  readonly canvas = document.createElement('canvas');
  private comparison: Comparison | null = null;
  private state = makeViewState() as ViewState;
  private share?: string;
  private held: 'left' | 'right' | 'up' | 'down' | 'seq-prev' | 'seq-next' | null = null;
  private edgeDiff = false;
  private ids: (string | null)[] = [];
  selectedIndex: number | null = null;
  private videos=new Map<string,VideoFrames>();
  private meta = new Map<string, Metadata>();
  private metadataCache = new Map<string, Metadata>();
  private cache = new Map<string, Picture>();
  private previousPage = new Set<string>();
  private suspended = false;
  private destroyed = false;
  private gpu: GpuEffects | null = null;
  private effects = new Map<string, { canvas: EffectSource; used: number }>();
  private activeEffects = new Set<string>();
  private images = new Map<string, Picture>();
  private diffs = new Map<string, Picture>();
  private diffTokens = new Map<string, number>();
  private errors = new Map<string, string>();
  private pending = new Map<string, Promise<Picture | null>>();
  private hists = new Map<string, HTMLCanvasElement>();
  private showHistogram = false;
  private overlays = { titles: true, details: true, titleScale: 1 };
  private titleCache = new Map<string, string[]>();
  private colors={canvas:'#000',surface:'#191919',text:'#999',label:'#ededed',accent:'#83b5ff',badge:'rgba(0,0,0,.42)'};
  refreshTheme() {
    const style=getComputedStyle(document.documentElement),get=(name:string)=>style.getPropertyValue(name).trim();
    this.colors={canvas:get('--canvas'),surface:get('--surface-raised'),text:get('--text-muted'),label:get('--text'),accent:get('--accent'),badge:get('--badge')};
    this.clearHistograms();this.paint();
  }
  refreshLanguage() {
    this.canvas.setAttribute('aria-label', t('图片比较工作区，双击选中或取消，滚轮缩放，拖动平移，Alt+单击读取像素'));
    this.titleCache.clear();this.paint();
  }
  private controller = new AbortController();
  /** 直方图开关：开启后基于已解码的预览位图现场统计，不额外请求网络。 */
  toggleHistogram() { this.showHistogram=!this.showHistogram; this.paint(); return this.showHistogram; }
  /** 覆显只影响画布标注，不改变图像、查看参数或网络请求。 */
  setOverlays(overlays: { titles: boolean; details: boolean; titleScale?: number }) {
    const scale=overlays.titleScale??this.overlays.titleScale;
    this.overlays={...overlays,titleScale:Number.isFinite(scale)&&scale>=.5?scale:1};this.paint();
  }
  private titleLines(ctx:CanvasRenderingContext2D,title:string,width:number,maxLines:number):string[] {
    if(width<=0||maxLines<1)return [];
    const key=JSON.stringify([ctx.font,title,width,maxLines]),cached=this.titleCache.get(key);if(cached)return cached;
    const chars=Array.from(title),lines:string[]=[];let cursor=0;
    while(cursor<chars.length&&lines.length<maxLines){
      let line='';
      while(cursor<chars.length){
        const char=chars[cursor];
        if(char==='\n'){cursor++;break;}
        if(ctx.measureText(line+char).width>width){if(!line){line=char;cursor++;}break;}
        line+=char;cursor++;
      }
      lines.push(line);
    }
    if(cursor<chars.length){
      const last=Array.from(lines.pop()!);
      while(last.length&&ctx.measureText(last.join('')+'…').width>width)last.pop();
      lines.push(last.join('')+'…');
    }
    if(this.titleCache.size>=128)this.titleCache.clear();this.titleCache.set(key,lines);return lines;
  }
  private histogram(p: Picture): HTMLCanvasElement {
    const cached=this.hists.get(p.key); if(cached) return cached;
    const c=document.createElement('canvas');c.width=160;c.height=90;
    const ctx=c.getContext('2d')!;
    ctx.drawImage(p.bitmap,0,0,160,90);
    const d=ctx.getImageData(0,0,160,90).data;
    const bins=[new Float32Array(256),new Float32Array(256),new Float32Array(256)];
    for(let i=0;i<d.length;i+=4){bins[0][d[i]]++;bins[1][d[i+1]]++;bins[2][d[i+2]]++;}
    const max=Math.max(1,...[...bins].map(b=>Math.max(...b)));
    ctx.fillStyle=this.colors.surface;ctx.fillRect(0,0,160,90);
    const colors=['#f16c78','#59ca91','#6c9fff'];
    bins.forEach((bin,ch)=>{
      const path=new Path2D();
      for(let b=0;b<256;b++){const px=b/255*160,py=90-(bin[b]/max)*86-1;b===0?path.moveTo(px,py):path.lineTo(px,py);}
      ctx.strokeStyle=colors[ch];ctx.globalAlpha=.55;ctx.stroke(path);
      path.lineTo(160,90);path.lineTo(0,90);path.closePath();
      ctx.fillStyle=colors[ch];ctx.globalAlpha=.18;ctx.fill(path);
    });
    ctx.globalAlpha=1;
    this.hists.set(p.key,c);return c;
  }
  private p99(p: Picture): number {
    if(p.p99!==undefined)return p.p99;
    const c=document.createElement('canvas');c.width=p.width;c.height=p.height;
    try {
      const ctx=c.getContext('2d',{willReadFrequently:true})!;ctx.drawImage(p.bitmap,0,0);
      const data=ctx.getImageData(0,0,p.width,p.height).data,bins=new Uint32Array(256);let count=0;
      // Full decoded display RGB, before exposure/gamma; transparent pixels have no brightness.
      for(let i=0;i<data.length;i+=4)if(data[i+3]){bins[Math.max(data[i],data[i+1],data[i+2])]++;count++;}
      const rank=Math.ceil(count*.99);let total=0;
      for(let value=0;value<256;value++){total+=bins[value];if(total>=rank)return p.p99=value;}
      return p.p99=0;
    } finally {c.width=0;c.height=0;}
  }
  private brightnessGain(p: Picture): number {
    if(this.state.brightness_ref===null||this.comparison?.media_kind==='video')return 1;
    const id=this.ids[this.state.brightness_ref]||this.ids.find(id=>id),reference=id?this.images.get(id):null;
    if(!reference)return 1;
    const target=this.p99(reference),current=this.p99(p);
    return target>0&&current>0?target/current:1;
  }
  private token = 0;
  private loading = false;
  private networkKey = '';
  private context: Context | null = null;
  private timer = 0;
  private pointerFrame = 0;
  private pendingPointer: { x: number; y: number } | null = null;
  private dragging: { x: number; y: number; views: { index: number; view: Rect; scale: number; scaleY: number }[] } | null = null;
  private wiping: { dx: number; width: number; scale: number } | null = null;
  private points: Array<Cell & { scale: number; scaleY: number; dx: number; dy: number }> = [];
  private observer: ResizeObserver;
  constructor(private change: (patch: Partial<ViewState>) => void, private status: (text: string) => void, _profile: string, private selectionChange: () => void = () => {}, private metadataChange: () => void = () => {}, private localImages = false, private personal = false) {
    this.canvas.id = 'compare-canvas';
    this.canvas.tabIndex = 0;
    this.canvas.setAttribute('aria-label', t('图片比较工作区，双击选中或取消，滚轮缩放，拖动平移，Alt+单击读取像素'));
    this.observer = new ResizeObserver(() => this.paint());
    this.observer.observe(this.canvas);
    this.refreshTheme();
    this.canvas.addEventListener('wheel', e => {
      e.preventDefault();
      const hit = this.hit(e.clientX,e.clientY); if (!hit) return;
      const factor = e.deltaY < 0 ? .85 : 1/.85;
      const r=this.canvas.getBoundingClientRect(),px=e.clientX-r.left-hit.cell.x,py=e.clientY-r.top-hit.cell.y;
      const panels=this.panels();
      for(const index of this.targets(hit.index)) {
        const view=this.viewport(index),cell=this.points[index];
        const ratio=Math.max(8,Math.min(100000,view.width*factor))/view.width;
        panels[index].view=this.clampView({x:view.x+px/cell.scale*(1-ratio),y:view.y+py/cell.scaleY*(1-ratio),width:view.width*ratio,height:view.height*ratio},index);
      }
      this.change({panels});
    }, {passive:false});
    this.canvas.addEventListener('pointerdown', e => {
      if (e.button !== 0 || e.altKey) return;
      const hit = this.hit(e.clientX,e.clientY); if (!hit) return;
      this.canvas.setPointerCapture(e.pointerId);
      if (this.state.mode==='wipe') {
        const v=this.view(hit.index);
        const cutX=hit.cell.dx+v.width*hit.cell.scale*this.state.wipe;
        const rect=this.canvas.getBoundingClientRect();
        if (Math.abs(e.clientX-rect.left-cutX)<10) { this.wiping={dx:hit.cell.dx,width:v.width,scale:hit.cell.scale}; this.canvas.classList.add('dragging'); return; }
      }
      this.dragging = {x:e.clientX,y:e.clientY,views:this.targets(hit.index).map(index=>({index,view:this.viewport(index),scale:this.points[index].scale,scaleY:this.points[index].scaleY}))};
      this.canvas.classList.add('dragging');
    });
    this.canvas.addEventListener('pointermove', e => {
      if (this.wiping || this.dragging) {
        // 指针事件可能快于显示刷新；只保留本帧最后位置，避免反复处理整幅差分图。
        // 位移相对按下时的快照计算，合并事件不会丢掉累计位移。
        this.pendingPointer={x:e.clientX,y:e.clientY};
        if(!this.pointerFrame)this.pointerFrame=requestAnimationFrame(()=>this.flushPointer());
      } else {
        const hit = this.hit(e.clientX,e.clientY);
        if (hit) this.status(t('原图坐标 (')+Math.floor(hit.wx)+', '+Math.floor(hit.wy)+t(') · 双击选中 · Alt+单击读取像素'));
      }
    });
    const release = () => { this.flushPointer();this.dragging = null; this.wiping = null; this.canvas.classList.remove('dragging'); };
    this.canvas.addEventListener('pointerup',release);
    this.canvas.addEventListener('pointercancel',release);
    this.canvas.addEventListener('lostpointercapture',release);
    this.canvas.addEventListener('dblclick',e => {
      if(e.altKey)return;
      const hit=this.hit(e.clientX,e.clientY);
      if(hit && this.ids[hit.index])this.select(this.selectedIndex===hit.index?null:hit.index);
    });
    this.canvas.addEventListener('click',async e => {
      if(!e.altKey)return;
      const hit = this.hit(e.clientX,e.clientY);
      const id = hit ? this.ids[hit.sourceIndex] : null;
      if (!hit || !id) return;
      const x = Math.floor(hit.wx), y = Math.floor(hit.wy), m = this.meta.get(id);
      if (!m || x<0 || y<0 || x>=m.width || y>=m.height) return;
      const token=this.token,signal=this.controller.signal;
      try {
        const picture=this.images.get(id),display=this.comparison?.media_kind==='video'||m.format==='raw';
        const p = this.comparison?.media_kind==='video'&&picture?{values:this.videoPixel(picture,x,y)}:await api.pixel(id,x,y,this.share,this.isRawAsset(id)?this.state.raw:undefined,signal);
        if(!signal.aborted&&token===this.token)this.status(t('槽 ')+(hit.index+1)+' · ('+x+', '+y+') '+(display?t('显示 RGB'):t('原始通道值'))+' ['+p.values.join(', ')+']');
      } catch(e) { if(!signal.aborted&&token===this.token)this.status(String(e)); }
    });
  }
  /** 截取每格完整图像区（不含覆盖标题），保留当前缩放/平移/叠图/效果。 */
  snapshotCells(): { name: string; canvas: HTMLCanvasElement }[] {
    const dpr = devicePixelRatio || 1;
    const out: { name: string; canvas: HTMLCanvasElement }[] = [];
    this.paint(false);
    this.points.forEach((cell, i) => {
      const id = this.ids[i];
      if (!id || cell.width < 2) return;
      const c = document.createElement('canvas');
      c.width = Math.max(1, Math.round(cell.width * dpr));
      c.height = Math.max(1, Math.round(cell.height * dpr));
      const x = c.getContext('2d')!;
      x.drawImage(this.canvas, Math.round(cell.x * dpr), Math.round(cell.y * dpr), c.width, c.height, 0, 0, c.width, c.height);
      out.push({ name: this.comparison?.assets[id]?.name || t`槽 ${i + 1}`, canvas: c });
    });
    this.paint();
    return out;
  }
  /** 分割线在当前画布的 x 坐标（命中测试与 QA 用）。 */
  cutLineX(index = 0): number | null {
    const cell=this.points[index]; if(!cell) return null;
    const v=this.view(index);
    return cell.dx + v.width*cell.scale*this.state.wipe;
  }
  private videoPixel(p:Picture,x:number,y:number) {
    const canvas=new OffscreenCanvas(1,1),ctx=canvas.getContext('2d')!;
    ctx.drawImage(p.bitmap,x,y,1,1,0,0,1,1);return Array.from(ctx.getImageData(0,0,1,1).data).slice(0,3);
  }
  /** The comparison rail shares the displayed bitmap: no second read or decode. */
  thumbnail(id:string,slot=0) {
    const p=this.images.get(id);if(!p)return null;const cached=p.thumbnails?.get(slot);if(cached)return cached;
    const canvas=document.createElement('canvas');canvas.width=128;canvas.height=80;
    const scale=Math.min(128/p.width,80/p.height);
    canvas.getContext('2d')!.drawImage(p.bitmap,(128-p.width*scale)/2,(80-p.height*scale)/2,p.width*scale,p.height*scale);
    (p.thumbnails??=new Map()).set(slot,canvas);return canvas;
  }
  videoInfo() { return this.ids.map(id=>id?{id,count:this.videos.get(id)?.timestamps.length||0,frame:(this.state.video_frame||0)+(this.state.video_offsets?.[id]||0)}:null); }
  rawInfo() {
    return this.ids.flatMap((id,index)=>{
      if(!id||!this.isRawAsset(id))return [];
      const key=JSON.stringify([this.share,id,this.rawKey(id,this.state)]);
      return [{index,id,name:this.comparison?.assets[id]?.name||id,metadata:this.metadataCache.get(key)||null,error:this.errors.get(id)||null}];
    });
  }
  videoDelay() {const v=this.videos.values().next().value;const i=this.state.video_frame||0;return v&&i+1<v.timestamps.length?(v.timestamps[i+1]-v.timestamps[i])*1000:100;}
  async settled() { while(this.loading)await new Promise(resolve=>setTimeout(resolve,20)); }
  /** 提交最后一次指针位置；松手同步提交，保证截图和后续操作看到最终位置。 */
  private flushPointer() {
    cancelAnimationFrame(this.pointerFrame);this.pointerFrame=0;
    const point=this.pendingPointer;this.pendingPointer=null;if(!point)return;
    if(this.wiping){
      const w=this.wiping,rect=this.canvas.getBoundingClientRect();
      this.change({wipe:Math.max(0,Math.min(1,(point.x-rect.left-w.dx)/(w.width*w.scale)))});
    }else if(this.dragging){
      const d=this.dragging;if(point.x===d.x&&point.y===d.y)return;
      const panels=this.panels();
      for(const p of d.views)panels[p.index].view=this.clampView({...p.view,x:p.view.x-(point.x-d.x)/p.scale,y:p.view.y-(point.y-d.y)/p.scaleY},p.index);
      this.change({panels});
    }
  }
  private cancelPointer() {cancelAnimationFrame(this.pointerFrame);this.pointerFrame=0;this.pendingPointer=null;}
  private clearHistograms() {
    this.hists.forEach(canvas=>{canvas.width=0;canvas.height=0;});this.hists.clear();
  }
  private releaseEffect(source: EffectSource) {
    if('close' in source)source.close();else {source.width=0;source.height=0;}
  }
  private gpuLost() {
    for(const [key,p] of this.effects)if('close' in p.canvas){this.releaseEffect(p.canvas);this.effects.delete(key);}
    this.paint();
  }
  private releasePicture(p: Picture) {
    this.gpu?.release(p.key);p.bitmap.close();p.thumbnails?.forEach(canvas=>{canvas.width=0;canvas.height=0;});p.thumbnails?.clear();
  }
  /** Return to the picker releases decoded resources; the saved view can resume. */
  suspend() {
    this.suspended=true;
    this.cancelPointer();
    this.videos.forEach(v=>v.dispose());this.videos.clear();
    this.controller.abort();++this.token;clearTimeout(this.timer);
    this.context=null;this.networkKey='';this.loading=false;
    this.dragging=null;this.wiping=null;this.held=null;
    this.cache.forEach(p=>this.releasePicture(p)); this.effects.forEach(p=>this.releaseEffect(p.canvas));
    this.gpu?.dispose();this.gpu=null;
    this.cache.clear(); this.effects.clear(); this.meta.clear(); this.metadataCache.clear(); this.images.clear(); this.diffs.clear();this.diffTokens.clear();
    this.previousPage.clear();this.activeEffects.clear();this.clearHistograms();this.titleCache.clear();this.errors.clear();this.pending.clear();this.points=[];
    this.canvas.width=0;this.canvas.height=0;
  }
  destroy() {
    this.suspend();this.destroyed=true;this.observer.disconnect();this.comparison=null;
    const debug=window as unknown as {__viewer?:Viewer};if(debug.__viewer===this)delete debug.__viewer;
  }
  update(comparison: Comparison | null, state: ViewState, share?: string) {
    if(this.destroyed)return;
    const nextIds=slotAssets(comparison,state);
    const sameComparison=comparison?.id===this.comparison?.id&&share===this.share;
    if(!sameComparison)this.suspend();
    this.suspended=false;
    const rawChanged=rawSettingsKey(state.raw)!==rawSettingsKey(this.state.raw);
    const rawVisible=nextIds.some((id:string|null)=>id&&(isRawPath(comparison?.assets[id]?.path)||isRawPath(comparison?.assets[id]?.name)));
    const resetSelection=comparison?.id!==this.comparison?.id || share!==this.share || state.layout!==this.state.layout || state.group_index!==this.state.group_index || state.grouping!==this.state.grouping || state.natural_sort!==this.state.natural_sort || JSON.stringify(nextIds)!==JSON.stringify(this.ids);
    if(comparison?.id!==this.comparison?.id || share!==this.share || JSON.stringify(nextIds)!==JSON.stringify(this.ids) || rawChanged&&rawVisible) {
      // Keep only the last viewed page for fast back/forward; no personal byte cap.
      this.previousPage=this.personal&&sameComparison&&!rawChanged?new Set([...this.images.values(),...this.diffs.values()].map(p=>p.key)):new Set();
      for(const [id,video] of this.videos)if(comparison?.id!==this.comparison?.id||share!==this.share||!nextIds.includes(id)){video.dispose();this.videos.delete(id);this.meta.delete(id);}
      this.controller.abort();++this.token;this.context=null;this.networkKey='';
      this.loading=true;
      this.images.clear();this.diffs.clear();this.diffTokens.clear();this.errors.clear();this.clearHistograms();
    }
    if(rawChanged)for(const [id,m] of this.meta)if(m.format==='raw')this.meta.delete(id);
    if(comparison?.media_kind==='video'&&(state.video_frame!==this.state.video_frame||JSON.stringify(state.video_offsets)!==JSON.stringify(this.state.video_offsets)))this.loading=true;
    this.comparison=comparison; this.state=structuredClone(state); this.share=share;
    this.ids=nextIds;
    if(resetSelection) {
      this.cancelPointer();
      this.dragging=null;this.wiping=null;this.canvas.classList.remove('dragging');
      if(this.selectedIndex!==null)this.select(null);
    }
    if(rawChanged||resetSelection)this.metadataChange();
    this.paint(); this.refresh(0);
  }
  hold(direction: 'left' | 'right' | 'up' | 'down' | 'seq-prev' | 'seq-next' | null) { if(this.held===direction)return;this.held=direction; this.paint(); this.refresh(0); }
  setEdgeDiff(on: boolean) { this.edgeDiff=on; this.paint(); void this.ensureDiffs(); }
  select(index: number | null) {
    this.selectedIndex=index!==null && this.ids[index]?index:null;
    this.paint();this.selectionChange();
    this.status(this.selectedIndex===null?t('全部图像联动 · 双击可选中一格'):t`已选中第 ${this.selectedIndex+1} 格 · 再次双击或 Esc 取消`);
  }
  private panels(): PanelState[] {
    return this.ids.map((id,i)=>({...this.state.panels[i]||{rotation:0,flip_x:false,flip_y:false,stretch:1},view:this.state.panels[i]?.view||(id && this.meta.has(id)?this.viewport(i):null)}));
  }
  private targets(mouseIndex?: number) {
    return this.ids.map((_,i)=>i).filter(i=>this.ids[i] && this.meta.has(this.ids[i]!) && (this.selectedIndex===null || i===this.selectedIndex || i===mouseIndex));
  }
  private size(index: number) {
    const id=this.ids[index],m=id?this.meta.get(id):null,rotated=(this.state.panels[index]?.rotation||0)%2;
    return {width:(rotated?m?.height:m?.width)||1,height:(rotated?m?.width:m?.height)||1};
  }
  rotate() {
    const panels=this.panels();
    for(const i of this.targets()) {
      const p=panels[i];p.rotation=(p.rotation+1)%4;
      [p.flip_x,p.flip_y]=[p.flip_y,p.flip_x];
      p.stretch=1/p.stretch;
      // 以当前屏幕方向旋转；对齐大小造成的横纵缩放也随图像旋转。
      if(p.view) {
        const v=this.viewport(i),cell=this.points[i],scale=cell.scale,scaleY=cell.scaleY;
        p.view={x:v.x*scale/scaleY,y:v.y*scaleY/scale,width:cell.width/scaleY,height:cell.height/scale};
      }
    }
    this.change({panels});
  }
  flip(axis: 'x' | 'y') {
    const panels=this.panels(),key=axis==='x'?'flip_x':'flip_y';
    for(const i of this.targets())panels[i][key]=!panels[i][key];
    this.change({panels});
  }
  fit() { this.resizeView('fit'); }
  fill() { this.resizeView('fill'); }
  private resizeView(mode: 'fit' | 'fill') {
    const panels=this.panels();
    for(const i of this.targets()) {
      const cell=this.points[i],{width:w,height:h}=this.size(i);
      if(!cell || !this.meta.has(this.ids[i]!))continue;
      const scale=(mode==='fit'?Math.min:Math.max)(cell.width/w,cell.height/h),width=cell.width/scale,height=cell.height/scale;
      panels[i].view={x:(w-width)/2,y:(h-height)/2,width,height};
      panels[i].stretch=1;
    }
    this.change({panels});
  }
  oneToOne() {
    const panels=this.panels();
    for(const i of this.targets()) {
      const v=this.viewport(i),cell=this.points[i];if(!cell || !this.meta.has(this.ids[i]!))continue;
      const width=cell.width,height=cell.height;
      panels[i].view={x:Math.round(v.x+(v.width-width)/2),y:Math.round(v.y+(v.height-height)/2),width,height};
      panels[i].stretch=1;
    }
    this.change({panels});
  }
  align(sameSize: boolean) {
    const reference=this.selectedIndex??this.ids.findIndex(id=>!!id && this.meta.has(id));
    if(reference<0 || !this.meta.has(this.ids[reference]!))return;
    const panels=this.panels(),v=this.viewport(reference),size=this.size(reference),cell=this.points[reference];
    for(const i of this.ids.map((_,i)=>i)) {
      if(i===reference || !this.ids[i] || !this.meta.has(this.ids[i]!))continue;
      const own=this.viewport(i),target=this.points[i],s=this.size(i);
      const scale=sameSize?cell.scale*size.width/s.width:target.scale,scaleY=sameSize?cell.scaleY*size.height/s.height:target.scaleY;
      panels[i].view={...own,x:v.x*cell.scale/scale,y:v.y*cell.scaleY/scaleY,width:target.width/scale,height:target.height/scaleY};
      panels[i].stretch=scaleY/scale;
    }
    this.change({panels});
  }
  private view(index = 0): Rect {
    if (this.state.panels[index]?.view) return this.state.panels[index].view!;
    if (this.state.view) return this.state.view;
    const sizes=this.ids.map((_,i)=>this.size(i));
    return {x:0,y:0,width:Math.max(1,...sizes.map(m=>m?.width||1)),height:Math.max(1,...sizes.map(m=>m?.height||1))};
  }
  /** 每格可见的世界坐标矩形；独立视图允许对齐大小时横纵缩放不同。 */
  private viewport(index: number): Rect {
    const c=this.points[index],v=this.view(index);
    return c?{x:v.x+(c.x-c.dx)/c.scale,y:v.y+(c.y-c.dy)/c.scaleY,width:c.width/c.scale,height:c.height/c.scaleY}:{...v};
  }
  /** 图像至少 1/4（取视图/图较小者）始终留在视野内。 */
  private clampView(v: Rect,index: number): Rect {
    const {width:imgW,height:imgH}=this.size(index);
    const cx=0.25*Math.min(v.width,imgW),cy=0.25*Math.min(v.height,imgH);
    return {...v,x:Math.min(imgW-cx,Math.max(cx-v.width,v.x)),y:Math.min(imgH-cy,Math.max(cy-v.height,v.y))};
  }
  private cells() {
    const count=this.ids.length;
    if (!count) return [];
    const {columns,rows}=layoutShape(this.state.layout);
    const width=(this.canvas.clientWidth-GAP*(columns+1))/columns, height=(this.canvas.clientHeight-GAP*(rows+1))/rows;
    return Array.from({length:count},(_,i)=>({x:GAP+i%columns*(width+GAP),y:GAP+Math.floor(i/columns)*(height+GAP),width:Math.max(1,width),height:Math.max(2,height)}));
  }
  private geometry(index: number,cell: Cell) {
    const v=this.view(index),stretch=this.state.panels[index]?.stretch||1;
    const scale=Math.max(cell.width/v.width,cell.height/v.height/stretch),scaleY=scale*stretch;
    return {...cell,scale,scaleY,dx:cell.x+(cell.width-v.width*scale)/2,dy:cell.y+(cell.height-v.height*scaleY)/2};
  }
  private matrix(index: number,cell: Cell): DOMMatrix {
    const v=this.view(index),g=this.geometry(index,cell),p=this.state.panels[index],size=this.size(index),id=this.ids[index],m=id?this.meta.get(id):null;
    return new DOMMatrix().translate(g.dx-v.x*g.scale,g.dy-v.y*g.scaleY).scale(g.scale,g.scaleY)
      .translate(size.width/2,size.height/2).scale(p?.flip_x?-1:1,p?.flip_y?-1:1)
      .rotate((p?.rotation||0)*90).translate(-(m?.width||1)/2,-(m?.height||1)/2);
  }
  private sourceIndex(plan: Plan,index: number,right = false) {
    if(plan.kind==='diff' || (!this.held && !right))return index;
    const cols=layoutShape(this.state.layout).columns;
    const step=this.held?{left:1,right:-1,up:cols,down:-cols,'seq-next':1,'seq-prev':-1}[this.held]:this.state.direction;
    const neighbor=index+step;
    // 同一资产可重复出现，叠图必须携带实际邻槽的显示参数。
    if(this.held==='left' && cols>1 && index%cols===cols-1 || this.held==='right' && cols>1 && index%cols===0)return index;
    return this.ids[neighbor] && this.ids[neighbor]===(right?plan.right:plan.left)?neighbor:index;
  }
  private hit(clientX: number,clientY: number) {
    const r=this.canvas.getBoundingClientRect(),x=clientX-r.left,y=clientY-r.top;
    const index=this.points.findIndex(c=>x>=c.x && x<c.x+c.width && y>=c.y && y<c.y+c.height);
    if (index<0) return null;
    const cell=this.points[index],plan=(panelPlan(this.ids,this.state.mode,this.state.direction,this.held,this.state.diff_base,layoutShape(this.state.layout).columns,this.edgeDiff) as Plan[])[index];
    const sourceIndex=this.sourceIndex(plan,index,plan.kind==='wipe' && x>=(this.cutLineX(index)??Infinity));
    const point=this.matrix(sourceIndex>=0?sourceIndex:index,cell).inverse().transformPoint({x,y});
    return {index,cell,sourceIndex,wx:point.x,wy:point.y};
  }
  private refresh(delay: number) {
    clearTimeout(this.timer);
    this.timer=window.setTimeout(()=>void this.load(),delay);
  }
  private async load() {
    if(this.suspended||this.destroyed)return;
    const state=structuredClone(this.state);
    const requestIds=new Set(this.ids.filter((id):id is string=>!!id));
    const hasRaw=[...requestIds].some(id=>this.isRawAsset(id));
    const full=true,size=1024;
    // Every quality loads the complete image once; pan/zoom stays in Canvas.
    const key=JSON.stringify([this.comparison?.id,this.share,[...requestIds],state.quality,this.localImages,size,full,hasRaw?rawSettingsKey(state.raw):null,state.video_frame,state.video_offsets]);
    if (key===this.networkKey) { await this.ensureDiffs(); return; }
    this.networkKey=key; this.controller.abort(); this.controller=new AbortController();
    const context={token:++this.token,signal:this.controller.signal,state,share:this.share,size,full};
    this.context=context;
    if(this.comparison?.media_kind==='video'&&requestIds.size>8) {
      this.videos.forEach(video=>video.dispose());this.videos.clear();this.images.clear();this.diffs.clear();
      this.errors=new Map([...requestIds].map(id=>[id,t('单页视频最多 8 路，请减少布局或切换为组内查看')]));this.loading=false;this.paint();return;
    }
    for(const id of this.images.keys())if(!requestIds.has(id))this.images.delete(id);
    // Keep the old crop of this same group visible while the new crop arrives.
    // 同一页提高清晰度时保留上一张差分，直到新差分到达；token 区分新旧结果，
    // 避免旧预览被误当作高清结果，也避免拖动触发升级后闪空。
    if(this.comparison?.media_kind==='video'){this.diffs.clear();this.diffTokens.clear();}
    this.errors.clear();this.clearHistograms();this.loading=true;
    this.paint();
    await runLimited([...requestIds],2,async id=>{
      try {
        const p=await this.picture(id,context);
        if (context.token!==this.token) return;
        if (p) this.images.set(id,p); else {this.images.delete(id);this.errors.set(id,t('查看范围之外'));}
      } catch(e) { if(context.token===this.token && (e as Error).name!=='AbortError') {this.images.delete(id);this.errors.set(id,(e as Error).message);} }
      if(context.token===this.token) {this.paint();this.metadataChange();}
    },context.signal);
    if(context.token!==this.token) return;
    this.loading=false;
    await this.ensureDiffs();
    if(context.token!==this.token) return;
    this.paint(); this.trim();if(this.comparison?.media_kind==='video')this.selectionChange();
  }
  private async getMeta(id: string,context: Context) {
    const key=JSON.stringify([context.share,id,this.rawKey(id,context.state)]);
    let m=this.comparison?.media_kind==='video'?this.meta.get(id):this.metadataCache.get(key);
    if(!m && this.comparison?.media_kind==='video') {
      let video=this.videos.get(id);
      if(!video){video=VideoFrames.asset(this.comparison.assets[id],context.share);this.videos.set(id,video);}
      await video.init();if(context.signal.aborted)throw abortError();m=video.metadata!;this.meta.set(id,m);
    }
    if(!m) {
      m=await api.metadata(id,context.share,context.signal,this.isRawAsset(id)?context.state.raw:undefined);
      if(context.signal.aborted) throw abortError();
      this.metadataCache.set(key,m);
      if(this.metadataCache.size>500)this.metadataCache.delete(this.metadataCache.keys().next().value!);
    }
    if(context.token===this.token){this.meta.set(id,m);if(m.format==='raw')this.metadataChange();}
    return m;
  }
  private isRawAsset(id: string) { const asset=this.comparison?.assets[id];return this.comparison?.media_kind!=='video'&&(isRawPath(asset?.path)||isRawPath(asset?.name)||this.meta.get(id)?.format==='raw'); }
  private rawKey(id: string,state: ViewState) { return this.isRawAsset(id)?rawSettingsKey(state.raw):null; }
  private imageQuality(id: string,state: ViewState): RenderQuality { return imageDisplayQuality(state.quality,this.localImages,this.comparison?.assets[id]?.path); }
  private async picture(id: string,context: Context): Promise<Picture|null> {
    if(context.signal.aborted) throw abortError();
    if(this.comparison?.media_kind==='video') {
      const m=await this.getMeta(id,context);
      const index=(context.state.video_frame||0)+(context.state.video_offsets?.[id]||0),key=JSON.stringify(['video',context.share,id,index]);
      const cached=this.cache.get(key);if(cached){cached.used=performance.now();return cached;}
      const pixels=this.ids.reduce((sum,id)=>{const m=id&&this.meta.get(id);return sum+(m?m.width*m.height:0);},0);
      if(pixels>24*1024**2)throw new Error(t('当前页视频分辨率总量超过 24M 像素，请减少布局格数'));
      const bitmap=await this.videos.get(id)!.frame(index,context.signal);
      if(context.signal.aborted){bitmap.close();throw abortError();}
      const picture={key,bitmap,width:bitmap.width,height:bitmap.height,box:{x:0,y:0,width:m.width,height:m.height},used:performance.now()};
      this.cache.set(key,picture);return picture;
    }
    const quality=this.imageQuality(id,context.state);
    const key=JSON.stringify(['image',context.share,id,quality,context.full,context.size,this.rawKey(id,context.state)]);
    const metadata=this.getMeta(id,context);
    const cached=this.cache.get(key); if(cached) {await metadata;cached.used=performance.now();return cached;}
    const [m,r]=await Promise.all([
      metadata,
      api.render(id,{quality,full:context.full,max_size:context.size,share:context.share,signal:context.signal,raw:this.isRawAsset(id)?context.state.raw:undefined}),
    ]);
    if(context.signal.aborted) throw abortError();
    const bitmap=await createImageBitmap(r.blob);
    if(context.signal.aborted) {bitmap.close();throw abortError();}
    const picture={key,bitmap,width:bitmap.width,height:bitmap.height,box:{x:0,y:0,width:m.width,height:m.height},used:performance.now(),quality};
    this.cache.set(key,picture); return picture;
  }
  private pairKey(left: string,right: string) { return [left,right].sort().join(':'); }
  private async diff(left: string,right: string,context: Context): Promise<Picture|null> {
    const pair=this.pairKey(left,right),pendingKey=context.token+':'+pair;
    if(this.pending.has(pendingKey)) return this.pending.get(pendingKey)!;
    const job=(async()=>{
      const [m,n]=await Promise.all([this.getMeta(left,context),this.getMeta(right,context)]);
      if(m.width!==n.width || m.height!==n.height) throw new Error(t('差分要求两图尺寸一致'));
      const quality=this.imageQuality(left,context.state)==='exact'&&this.imageQuality(right,context.state)==='exact'?'exact':'high';
      const raw=this.isRawAsset(left)||this.isRawAsset(right)?context.state.raw:undefined;
      const key=JSON.stringify(['diff',context.share,pair,quality,context.full,context.size,raw?rawSettingsKey(raw):null,context.state.video_frame,context.state.video_offsets]);
      let picture=this.cache.get(key);
      if(!picture) {
        let bitmap:ImageBitmap;
        if(this.comparison?.media_kind==='video') {
          const a=await this.picture(left,context),b=await this.picture(right,context);
          if(!a||!b)return null;
          const canvas=new OffscreenCanvas(m.width,m.height),ctx=canvas.getContext('2d')!;
          ctx.drawImage(a.bitmap,0,0);const first=ctx.getImageData(0,0,m.width,m.height);
          ctx.drawImage(b.bitmap,0,0);const second=ctx.getImageData(0,0,m.width,m.height);
          for(let i=0;i<first.data.length;i++)first.data[i]=i%4===3?255:Math.abs(first.data[i]-second.data[i]);
          ctx.putImageData(first,0,0);bitmap=canvas.transferToImageBitmap();
        } else {
          const blob=await api.diff(left,right,{quality,full:context.full,max_size:context.size,share:context.share,signal:context.signal,raw});
          if(context.signal.aborted)throw abortError();bitmap=await createImageBitmap(blob);
        }
        if(context.signal.aborted) {bitmap.close();throw abortError();}
        picture={key,bitmap,width:bitmap.width,height:bitmap.height,box:{x:0,y:0,width:m.width,height:m.height},used:performance.now(),quality};
        this.cache.set(key,picture);
      }
      picture.used=performance.now();
      if(context.token===this.token){this.diffs.set(pair,picture);this.diffTokens.set(pair,context.token);}
      return picture;
    })();
    this.pending.set(pendingKey,job);
    try { return await job; } finally { this.pending.delete(pendingKey); }
  }
  private async ensureDiffs() {
    const context=this.context; if(!context || context.signal.aborted) return;
    const plans=panelPlan(this.ids,this.state.mode,this.state.direction,this.held,this.state.diff_base,layoutShape(this.state.layout).columns,this.edgeDiff) as Plan[];
    await runLimited(plans.filter(p=>p.kind==='diff' && p.left && p.right),2,async p=>{
      const pair=this.pairKey(p.left!,p.right!);
      if(this.diffs.has(pair)&&this.diffTokens.get(pair)===context.token || this.errors.has(pair)) return;
      try { await this.diff(p.left!,p.right!,context); }
      catch(e) { if(context.token===this.token && (e as Error).name!=='AbortError') this.errors.set(pair,(e as Error).message); }
    },context.signal);
    if(context.token===this.token) {
      // A mode change does not restart image loading; retire its unused differences.
      if(this.personal) {
        const active=new Set((panelPlan(this.ids,this.state.mode,this.state.direction,this.held,this.state.diff_base,layoutShape(this.state.layout).columns,this.edgeDiff) as Plan[]).filter(p=>p.kind==='diff'&&p.left&&p.right).map(p=>this.pairKey(p.left!,p.right!)));
        for(const pair of this.diffs.keys())if(!active.has(pair)){this.diffs.delete(pair);this.diffTokens.delete(pair);}
      }
      this.paint();this.trim();
    }
  }
  private paint(showTitles = true) {
    if(this.suspended||this.destroyed)return;
    const dpr=devicePixelRatio||1,w=this.canvas.clientWidth,h=this.canvas.clientHeight;
    if(!w || !h) return;
    this.activeEffects.clear();
    if(this.canvas.width!==Math.round(w*dpr) || this.canvas.height!==Math.round(h*dpr)) {this.canvas.width=Math.round(w*dpr);this.canvas.height=Math.round(h*dpr);}
    const ctx=this.canvas.getContext('2d')!;
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.fillStyle=this.colors.canvas;ctx.fillRect(0,0,w,h);
    ctx.font='12px "Helvetica Neue",Helvetica,Arial,sans-serif'; ctx.textBaseline='middle';
    if(!this.comparison) {ctx.fillStyle=this.colors.text;ctx.textAlign='center';ctx.fillText(t('选择图片，开始比较'),w/2,h/2);ctx.textAlign='left';return;}
    const cols=layoutShape(this.state.layout).columns;
    const plans=panelPlan(this.ids,this.state.mode,this.state.direction,this.held,this.state.diff_base,cols,this.edgeDiff) as Plan[];
    let ready=true;
    this.points=this.cells().map((cell,i)=>{
      const {x,y,width,height}=cell;
      const view=this.view(i),geometry=this.geometry(i,cell),{scale,dx}=geometry;
      const plan=plans[i],ownId=this.ids[i];
      ctx.fillStyle=this.colors.canvas;ctx.fillRect(x,y,width,height);
      ctx.save();ctx.beginPath();ctx.rect(x,y,width,height);ctx.clip();
      let message='';
      const first=plan.left ? this.images.get(plan.left) : null,second=plan.right ? this.images.get(plan.right) : null;
      if(plan.kind==='missing') message=t('缺图');
      else if(plan.kind==='diff' && plan.left && plan.right) {
        const pair=this.pairKey(plan.left,plan.right),diff=this.diffs.get(pair);
        if(diff) {
          this.drawPicture(ctx,diff,i,cell,true);
          if(this.diffTokens.get(pair)!==this.context?.token){if(this.errors.has(pair))message=this.errors.get(pair)!;else ready=false;}
        }
        else {message=this.errors.get(pair)||t('计算原图差分…');if(!this.errors.has(pair)) ready=false;}
      } else if(first) {
        this.drawPicture(ctx,first,this.sourceIndex(plan,i),cell,false);
        if(plan.kind==='wipe' && second) {
          ctx.save();
          const cut=dx+view.width*scale*this.state.wipe;ctx.beginPath();ctx.rect(cut,y,x+width-cut,height);ctx.clip();
          this.drawPicture(ctx,second,this.sourceIndex(plan,i,true),cell,false);ctx.restore();
          if(plan.kind==='wipe') {const cut=dx+view.width*scale*this.state.wipe;ctx.strokeStyle=this.colors.accent;ctx.beginPath();ctx.moveTo(cut,y);ctx.lineTo(cut,y+height);ctx.stroke();}
        } else if(plan.right && !second) {message=this.errors.get(plan.right)||t('加载对应图…');if(!this.errors.has(plan.right))ready=false;}
      } else {
        message=plan.left && this.errors.get(plan.left)||(plan.left&&this.isRawAsset(plan.left)?t('RAW 解析中…'):t('加载当前页…'));
        if(plan.left && !this.errors.has(plan.left))ready=false;
      }
      if(message) {ctx.fillStyle=this.colors.text;ctx.textAlign='center';ctx.fillText(message,x+width/2,y+height/2,Math.max(10,width-16));ctx.textAlign='left';}
      ctx.restore();
      ctx.save();ctx.beginPath();ctx.rect(x,y,width,height);ctx.clip();
      const asset=ownId?this.comparison!.assets[ownId]:null,m=ownId?this.meta.get(ownId):null;
      const relation=this.held!==null?(plan.kind==='diff'?t('边界 |Δ|'):t('邻图覆盖')):
        this.state.mode==='diff'?(i===this.state.diff_base?t('基准原图'):`|Δ ${i+1}−${this.state.diff_base+1}|`):'';
      const groupIndex=this.state.grouping==='within'?pageInfo(this.comparison,this.state.layout,this.state.group_index,this.state).groupIndex:orderedGroupIndices(this.comparison,this.state)[i];
      const label=this.comparison!.groups?.[groupIndex]?.label;
      const assetName=(asset?.name||t('缺图'))+(label?' · '+label:'');
      let titleHeight=0;
      if(showTitles && this.overlays.titles) {
        ctx.save();
        const fontSize=14*this.overlays.titleScale,lineHeight=Math.ceil(fontSize*1.35);
        ctx.font=`${fontSize}px "Helvetica Neue",Helvetica,Arial,sans-serif`;
        const maxHeight=Math.min(height-6-(this.overlays.details?24:0),Math.max(lineHeight+8,height*.45));
        const title=(this.selectedIndex===i?t('✓ 已选中 · '):'')+(i+1)+' · '+(relation?relation+' · ':'')+assetName+(m?.has_icc?' · ICC':'');
        const lines=this.titleLines(ctx,title,width-20,Math.floor((maxHeight-8)/lineHeight));
        if(lines.length){
          titleHeight=lines.length*lineHeight+8;
          ctx.fillStyle=this.colors.badge;ctx.fillRect(x+3,y+3,Math.min(width-6,Math.max(...lines.map(line=>ctx.measureText(line).width))+14),titleHeight);
          ctx.shadowColor=this.colors.canvas;ctx.shadowBlur=3;
          ctx.fillStyle=this.selectedIndex===i?this.colors.accent:this.colors.label;
          lines.forEach((line,index)=>ctx.fillText(line,x+10,y+7+(index+.5)*lineHeight));
        }
        ctx.restore();
      }
      if(this.showHistogram) {
        const shown=plan.kind==='diff'&&plan.left&&plan.right?this.diffs.get(this.pairKey(plan.left,plan.right)):plan.left?this.images.get(plan.left):null;
        if(shown) ctx.drawImage(this.histogram(shown),x+4,y+titleHeight+(titleHeight?7:4));
      }
      if(m && this.overlays.details) {
        ctx.save();ctx.textBaseline='alphabetic';
        const dimensions=`${m.width}×${m.height}`,zoom=`×${scale.toFixed(scale<.1?3:2)}`,baseline=y+height-5;
        const zoomWidth=ctx.measureText(zoom).width+8;
        ctx.fillStyle=this.colors.badge;ctx.fillRect(x+3,baseline-14,zoomWidth,18);
        ctx.shadowColor=this.colors.canvas;ctx.shadowBlur=3;
        ctx.fillStyle=this.colors.accent;ctx.textAlign='left';ctx.fillText(zoom,x+7,baseline);
        const dimensionWidth=ctx.measureText(dimensions).width+10;
        ctx.fillStyle=this.colors.badge;ctx.fillRect(x+width-dimensionWidth-2,baseline-14,dimensionWidth,18);
        ctx.fillStyle=this.colors.label;ctx.textAlign='right';ctx.fillText(dimensions,x+width-7,baseline);ctx.restore();
      }
      ctx.restore();
      if(this.selectedIndex===i){ctx.strokeStyle=this.colors.accent;ctx.lineWidth=2;ctx.strokeRect(x+1,y+1,width-2,height-2);}
      return geometry;
    });
    const hasRaw=this.ids.some(id=>id&&this.isRawAsset(id));
    this.canvas.dataset.ready=String(ready && !this.loading && !!this.context && this.context.state.quality===this.state.quality && (!hasRaw||rawSettingsKey(this.context.state.raw)===rawSettingsKey(this.state.raw)));
    this.canvas.dataset.group=String(this.state.group_index);
    this.canvas.dataset.grouping=this.state.grouping;
    this.canvas.dataset.naturalSort=String(this.state.natural_sort);
    this.canvas.dataset.mode=this.held===null?this.state.mode:'hold'+this.held;
    this.canvas.dataset.diffBase=String(this.state.diff_base);
    this.canvas.dataset.brightnessRef=this.state.brightness_ref===null?'':String(this.state.brightness_ref);
    const qualities=new Set(this.ids.filter((id):id is string=>!!id).map(id=>this.imageQuality(id,this.context?.state||this.state)));
    this.canvas.dataset.quality=this.context?(qualities.size===1?[...qualities][0]:'mixed'):'';
    this.canvas.dataset.requestedQuality=this.state.quality;
    this.canvas.dataset.full=String(!!this.context?.full);
    this.canvas.dataset.raw=hasRaw?rawSettingsKey(this.state.raw):'';
    this.canvas.dataset.view=JSON.stringify(this.state.view);
    this.canvas.dataset.selected=this.selectedIndex===null?'':String(this.selectedIndex);
    // Also release effects when returning to plain RGB, which creates no new effect.
    if(this.personal)queueMicrotask(()=>this.trim());
  }
  private drawPicture(ctx: CanvasRenderingContext2D,p: Picture,index: number,cell: Cell,diff: boolean) {
    const b=p.box,matrix=this.matrix(index,cell),geometry=this.geometry(index,cell);
    p.used=performance.now();
    ctx.save();ctx.transform(matrix.a,matrix.b,matrix.c,matrix.d,matrix.e,matrix.f);
    ctx.imageSmoothingEnabled=p.quality!=='exact' || Math.min(geometry.scale,geometry.scaleY)<1;
    ctx.drawImage(this.effect(p,diff),b.x,b.y,b.width,b.height);ctx.restore();
  }
  private effect(p: Picture,diff: boolean): CanvasImageSource {
    const s=this.state,gain=diff?s.gain:this.brightnessGain(p);
    if(s.channel==='rgb' && s.exposure===0 && s.gamma===1 && gain===1) return p.bitmap;
    const key=JSON.stringify([p.key,s.channel,s.exposure,s.gamma,gain]);
    this.activeEffects.add(key);
    const old=this.effects.get(key);if(old){old.used=performance.now();return old.canvas;}
    const lookup=new Uint8ClampedArray(256),factor=2**s.exposure*gain;
    for(let i=0;i<256;i++)lookup[i]=255*Math.pow(Math.min(1,i*factor/255),1/s.gamma);
    const channel={r:0,g:1,b:2,rgb:-1}[s.channel];
    this.gpu??=new GpuEffects(()=>queueMicrotask(()=>this.gpuLost()));
    let result: EffectSource|false=this.gpu.render(p.key,p.bitmap,lookup,channel);
    if(!result) {
      result=document.createElement('canvas');result.width=p.width;result.height=p.height;
      this.cpuEffect(p,result,lookup,channel);
    }
    this.effects.set(key,{canvas:result,used:performance.now()});
    // Trim after painting, so an in-use canvas cannot be released mid-draw.
    queueMicrotask(()=>this.trim());return result;
  }
  private cpuEffect(p: Picture,c: HTMLCanvasElement,lookup: Uint8ClampedArray,channel: number) {
    const ctx=c.getContext('2d')!;ctx.drawImage(p.bitmap,0,0);
    const pixels=ctx.getImageData(0,0,c.width,c.height),data=pixels.data;
    for(let i=0;i<data.length;i+=4) {
      if(channel>=0) {const v=lookup[data[i+channel]];data[i]=v;data[i+1]=v;data[i+2]=v;}
      else {data[i]=lookup[data[i]];data[i+1]=lookup[data[i+1]];data[i+2]=lookup[data[i+2]];}
    }
    ctx.putImageData(pixels,0,0);
  }
  private trim() {
    const pinned=new Set([...this.images.values(),...this.diffs.values()].map(p=>p.key));
    for(const p of this.cache.values())for(const [slot,canvas] of p.thumbnails||[])if(this.images.get(this.ids[slot]||'')!==p){canvas.width=0;canvas.height=0;p.thumbnails!.delete(slot);}
    if(this.personal) {
      for(const [key,p] of this.effects)if(!this.activeEffects.has(key)){this.releaseEffect(p.canvas);this.effects.delete(key);}
      // During decoding, a returned bitmap may not yet be installed in images.
      if(!this.loading)for(const [key,p] of this.cache)if(!pinned.has(key)&&!this.previousPage.has(key)){this.releasePicture(p);this.cache.delete(key);}
      for(const [key,c] of this.hists)if(!this.cache.has(key)){c.width=0;c.height=0;this.hists.delete(key);}
      return;
    }
    let total=[...this.cache.values()].reduce((n,p)=>n+p.width*p.height*4,0)+[...this.effects.values()].reduce((n,p)=>n+p.canvas.width*p.canvas.height*4,0);
    // Like the decoded current page, active effects stay resident even when the
    // page exceeds the cache budget; panning must never recalculate every pixel.
    const victims=[...this.effects.entries()].filter(([key])=>!this.activeEffects.has(key)).map(([key,p])=>({used:p.used,bytes:p.canvas.width*p.canvas.height*4,remove:()=>{this.releaseEffect(p.canvas);this.effects.delete(key);}}));
    this.cache.forEach((p,key)=>{if(!pinned.has(key))victims.push({used:p.used,bytes:p.width*p.height*4,remove:()=>{this.releasePicture(p);this.cache.delete(key);}});});
    victims.sort((a,b)=>a.used-b.used);
    for(const v of victims) {if(total<=SERVER_CACHE_LIMIT)break;v.remove();total-=v.bytes;}
  }
}

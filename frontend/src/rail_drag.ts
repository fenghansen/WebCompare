export type RailDragItem={key:string;kind:'asset'|'group';index:number;element:HTMLElement;sortable:boolean};
export type RailPlacement='before'|'after';
type Point={x:number;y:number};
type RailDragOptions={activate:(item:RailDragItem)=>void;drop:(source:RailDragItem,target:RailDragItem,placement:RailPlacement,scope:string)=>void;finish:()=>void};
type Active={item:RailDragItem;viewport:HTMLElement;pointer:number;start:Point;point:Point;offset:Point;scope:string;dragging:boolean;ghost:HTMLElement|null;line:HTMLElement|null;target:RailDragItem|null;placement:RailPlacement};

/** 插入线描述的是目标卡片前/后边界；只在释放后把它转为实际数组顺序。 */
export function moveRailItem<T>(items:readonly T[],from:number,target:number,placement:RailPlacement) {
  if(from<0||target<0||from>=items.length||target>=items.length)return {items:[...items],index:from,changed:false};
  const boundary=target+(placement==='after'?1:0),index=boundary-(from<boundary?1:0);
  if(index===from)return {items:[...items],index,changed:false};
  const next=[...items],[item]=next.splice(from,1);next.splice(index,0,item);
  return {items:next,index,changed:true};
}

/** capture 绑定稳定的左栏容器，异步缩略图刷新不能替换正在捕获 pointer 的节点。 */
export class RailDragController {
  private items:RailDragItem[]=[];
  private scope='';
  private identity='';
  private active:Active|null=null;
  private frame=0;
  private selected:string|null=null;
  private readonly off: Array<()=>void>=[];
  constructor(readonly root:HTMLElement,private options:RailDragOptions) {
    const listen=(target:EventTarget,type:string,listener:EventListener)=>{target.addEventListener(type,listener);this.off.push(()=>target.removeEventListener(type,listener));};
    listen(root,'pointerdown',event=>this.down(event as PointerEvent));
    listen(root,'pointermove',event=>this.move(event as PointerEvent));
    listen(root,'pointerup',event=>this.up(event as PointerEvent));
    listen(root,'pointercancel',()=>this.cancel());
    listen(root,'lostpointercapture',()=>{if(this.active)this.cancel();});
    listen(root,'dragstart',event=>event.preventDefault());
    listen(window,'blur',()=>this.cancel());
    listen(window,'keydown',event=>{if((event as KeyboardEvent).key==='Escape'&&this.active){event.preventDefault();event.stopPropagation();this.cancel();}});
  }
  get engaged(){return this.active!==null;}
  get dragging(){return this.active?.dragging===true;}
  get context(){return this.scope;}
  get selectedKey(){return this.selected;}
  select(key:string|null) {this.selected=key;this.paintSelection();}
  sync(items:RailDragItem[],scope:string,identity:string) {
    this.items=items;this.scope=scope;
    if(this.identity!==identity){this.identity=identity;this.selected=null;}
    this.paintSelection();
  }
  private paintSelection() {
    for(const item of this.items){const chosen=item.key===this.selected;item.element.classList.toggle('rail-selected',chosen);item.element.setAttribute('aria-pressed',String(chosen));}
  }
  measure() {return new Map(this.items.map(item=>[item.key,item.element.getBoundingClientRect()]));}
  animateFrom(previous:Map<string,DOMRect>) {
    if(window.matchMedia('(prefers-reduced-motion: reduce)').matches)return;
    for(const item of this.items){const before=previous.get(item.key);if(!before)continue;const after=item.element.getBoundingClientRect(),x=before.left-after.left,y=before.top-after.top;
      if(Math.abs(x)+Math.abs(y)>1)item.element.animate([{transform:`translate(${x}px,${y}px)`},{transform:'translate(0,0)'}],{duration:180,easing:'cubic-bezier(.2,.8,.2,1)'});
    }
  }
  private down(event:PointerEvent) {
    if(event.button!==0||event.isPrimary===false||this.active)return;
    const element=event.target instanceof Element?event.target.closest<HTMLElement>('[data-rail-key]'):null;
    if(!element||!this.root.contains(element)||(event.target as Element).closest('input,button,select,textarea,[contenteditable=true]'))return;
    const item=this.items.find(item=>item.element===element);if(!item)return;
    const bounds=element.getBoundingClientRect();
    event.preventDefault();this.select(item.key);element.classList.add('rail-pressed');
    this.active={item,viewport:element.closest<HTMLElement>('[data-rail-scroll]')||this.root,pointer:event.pointerId,start:{x:event.clientX,y:event.clientY},point:{x:event.clientX,y:event.clientY},offset:{x:event.clientX-bounds.left,y:event.clientY-bounds.top},scope:this.scope,dragging:false,ghost:null,line:null,target:null,placement:'before'};
    this.root.setPointerCapture(event.pointerId);
  }
  private move(event:PointerEvent) {
    const active=this.active;if(!active||active.pointer!==event.pointerId)return;
    active.point={x:event.clientX,y:event.clientY};event.preventDefault();
    if(!active.dragging&&active.item.sortable&&Math.hypot(event.clientX-active.start.x,event.clientY-active.start.y)>=6)this.begin(active);
    if(active.dragging)this.preview(active);
  }
  private begin(active:Active) {
    active.dragging=true;this.root.classList.add('rail-dragging');active.item.element.classList.add('rail-drag-source');
    const bounds=active.item.element.getBoundingClientRect(),ghost=active.item.element.cloneNode(true) as HTMLElement;
    ghost.classList.remove('rail-pressed','rail-selected','rail-drag-source');ghost.classList.add('rail-drag-ghost');ghost.removeAttribute('data-rail-key');ghost.setAttribute('aria-hidden','true');
    ghost.style.width=`${bounds.width}px`;ghost.style.height=`${bounds.height}px`;
    // canvas 的 cloneNode 不复制视频帧像素；显式复制，虚影才与实际缩略卡一致。
    const originals=active.item.element.querySelectorAll('canvas');ghost.querySelectorAll('canvas').forEach((canvas,index)=>{const original=originals[index];canvas.width=original.width;canvas.height=original.height;canvas.getContext('2d')?.drawImage(original,0,0);});
    const line=document.createElement('div');line.className='rail-insert-line';line.setAttribute('aria-hidden','true');
    this.root.append(ghost,line);active.ghost=ghost;active.line=line;this.tick();
  }
  private preview(active:Active) {
    if(active.ghost)active.ghost.style.transform=`translate3d(${active.point.x-active.offset.x}px,${active.point.y-active.offset.y}px,0)`;
    const root=active.viewport.getBoundingClientRect(),inside=active.point.x>=root.left&&active.point.x<=root.right&&active.point.y>=root.top&&active.point.y<=root.bottom;
    const candidates=this.items.filter(item=>item.kind===active.item.kind&&item!==active.item&&item.sortable&&item.element.isConnected);
    active.target=null;if(!inside||!candidates.length){if(active.line)active.line.hidden=true;return;}
    let target=candidates[candidates.length-1],placement:RailPlacement='after';
    for(const item of candidates){const bounds=item.element.getBoundingClientRect();if(active.point.y<bounds.top+bounds.height/2){target=item;placement='before';break;}}
    active.target=target;active.placement=placement;
    if(active.line){const bounds=target.element.getBoundingClientRect();active.line.hidden=false;active.line.style.left=`${Math.max(root.left,bounds.left)}px`;active.line.style.top=`${placement==='before'?bounds.top-5:bounds.bottom+5}px`;active.line.style.width=`${Math.min(root.right,bounds.right)-Math.max(root.left,bounds.left)}px`;}
  }
  private tick=()=>{
    const active=this.active;if(!active?.dragging)return;
    if(!this.root.isConnected||this.root.getBoundingClientRect().height===0){this.cancel();return;}
    const bounds=active.viewport.getBoundingClientRect(),zone=Math.min(52,bounds.height/4),point=active.point;
    if(point.x>=bounds.left&&point.x<=bounds.right){const speed=point.y<bounds.top+zone?-Math.min(16,(bounds.top+zone-point.y)/zone*16):point.y>bounds.bottom-zone?Math.min(16,(point.y-bounds.bottom+zone)/zone*16):0;if(speed)active.viewport.scrollTop+=speed;}
    this.preview(active);this.frame=requestAnimationFrame(this.tick);
  };
  private up(event:PointerEvent) {
    const active=this.active;if(!active||active.pointer!==event.pointerId)return;
    active.point={x:event.clientX,y:event.clientY};if(active.dragging)this.preview(active);
    const target=active.target,placement=active.placement,valid=active.scope===this.scope;
    this.finish();
    if(valid){if(active.dragging&&target)this.options.drop(active.item,target,placement,active.scope);else if(!active.dragging)this.options.activate(active.item);}
    this.options.finish();
  }
  private finish() {
    const active=this.active;if(!active)return;this.active=null;cancelAnimationFrame(this.frame);this.frame=0;
    this.root.classList.remove('rail-dragging');active.item.element.classList.remove('rail-pressed','rail-drag-source');active.ghost?.remove();active.line?.remove();
    if(this.root.hasPointerCapture(active.pointer))this.root.releasePointerCapture(active.pointer);
  }
  cancel(notify=true) {if(!this.active)return;this.finish();if(notify)this.options.finish();}
  destroy() {this.cancel(false);this.off.splice(0).forEach(off=>off());}
}

import { t } from './i18n.js';
export const DEFAULT_RAW_SETTINGS = Object.freeze({
  bayer: 'auto', packing: 'auto', width: null, height: null, stride_pixels: null, bit_depth: null,
  black_level: null, black_levels: null, white_level: null, gain: 1, gamma: 2.2, white_balance: null,
});
/** Optional RAW fields stay automatic; invalid saved values cannot leak into media requests. */
export function normalizeRawSettings(input) {
  const c=input&&typeof input==='object'&&!Array.isArray(input)?input:{};
  const optional=(value,min,max,integer=false)=>Number.isFinite(value)&&value>=min&&value<=max&&(!integer||Number.isInteger(value))?value:null;
  const positive=(value,max,fallback)=>Number.isFinite(value)&&value>0?Math.min(max,value):fallback;
  const levels=Array.isArray(c.black_levels)&&c.black_levels.length===4&&c.black_levels.every(value=>Number.isFinite(value)&&Math.abs(value)<=1e9)?[...c.black_levels]:null;
  const balance=Array.isArray(c.white_balance)&&c.white_balance.length===3&&c.white_balance.every(value=>Number.isFinite(value)&&value>0&&value<=64)?[...c.white_balance]:null;
  return {
    bayer: ['auto','RGGB','BGGR','GRBG','GBRG'].includes(c.bayer)?c.bayer:'auto',
    packing: ['auto','ax_lsb10','mipi_raw10','uint16le','float32le'].includes(c.packing)?c.packing:'auto',
    width: optional(c.width,2,100000,true), height: optional(c.height,2,100000,true),
    stride_pixels: optional(c.stride_pixels,2,100000,true), bit_depth: optional(c.bit_depth,1,16,true),
    black_level: levels?null:optional(c.black_level,-1e9,1e9), black_levels: levels, white_level: optional(c.white_level,-1e9,1e9),
    gain: positive(c.gain,4096,1), gamma: clamp(c.gamma,.1,8,2.2),
    // Existing explicit unity gains stay manual. Only null/absent means auto.
    white_balance: balance,
  };
}
/** Stable settings identity for metadata, decoded images, differences and thumbnails. */
export function rawSettingsKey(input) { return JSON.stringify(normalizeRawSettings(input)); }
export function isRawPath(path) { return typeof path==='string'&&/\.(raw|bayer|npy)$/i.test(path); }
/** RAW is converted once at full resolution, then uses the existing local Canvas path. */
export function rawDisplayQuality(quality,hasRaw) { return hasRaw&&quality==='preview'?'high':quality; }
/** Adaptive quality follows each asset's transport, including mixed local/SSH pages. */
export function imageDisplayQuality(quality,localImages,path) { return quality==='exact'||quality==='high'?quality:localImages===true&&typeof path==='string'&&!path.startsWith('ssh://')?'exact':'high'; }

/** Serializable state is deliberately separate from held keys and decoded images. */
export const DEFAULT_VIEW_STATE = Object.freeze({
  version: 1, group_index: 0, mode: 'split', direction: 1, diff_base: 0,
  opacity: .5, wipe: .5, gain: 4, channel: 'rgb', exposure: 0, gamma: 1, brightness_ref: null,
  layout: '1x2', quality: 'auto', grouping: 'aligned', natural_sort: false, transposed: false, view: null, panels: Object.freeze([]), raw: DEFAULT_RAW_SETTINGS,
});
export function makeViewState() { return { ...DEFAULT_VIEW_STATE, panels: [], raw: normalizeRawSettings() }; }
export function clamp(value, min, max, fallback) { return Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback; }
/** 布局即每页格数，任意 RxC 均可，不再按来源数兜底。 */
export function validLayout(layout) {
  const match = typeof layout === 'string' && /^([1-9]\d?)x([1-9]\d?)$/.exec(layout);
  return !!match && Number(match[1]) <= 64 && Number(match[2]) <= 64 && Number(match[1]) * Number(match[2]) <= 64;
}
export function layoutShape(layout) {
  if (validLayout(layout)) { const [rows, columns] = layout.split('x').map(Number); return { rows, columns }; }
  if (layout === 'grid') return { rows: 2, columns: 2 };
  return { rows: 1, columns: 2 };
}
/** 固定布局清单，不按图片数裁剪。 */
export function layoutOptions() {
  return ['1x1','1x2','1x3','2x2','2x3','3x3','2x4','4x2','3x4','4x3'].map(value => {
    const [rows, columns] = value.split('x').map(Number);
    return { value, rows, columns };
  });
}
/** 进入对比时的智能布局：交错序列按来源数一行排开；否则 ≤4 张按 1xN/2x2，更多用默认 1x2。 */
export function smartLayout(total, sources = 0) {
  if (sources >= 2 && sources <= 8) return `1x${sources}`;
  if (sources > 8) return groupLayout(Math.min(64, sources));
  if (total <= 4) return ['1x1','1x2','1x3','2x2'][Math.max(1, total) - 1];
  return '1x2';
}
export function groupLayout(count) {
  count = Math.max(1, Math.min(64, count));
  if (count <= 3) return `1x${Math.max(1,count)}`;
  if (count <= 4) return '2x2';
  if (count <= 6) return '2x3';
  if (count <= 8) return '2x4';
  if (count <= 9) return '3x3';
  return `${Math.ceil(count/4)}x4`;
}
export function cellsOf(layout) { const s = layoutShape(layout); return s.rows * s.columns; }
/** 数字段按数值顺序，其余按小写 Unicode 码点；等键保留原序。 */
export function naturalCompare(left, right) {
  const a=String(left).toLowerCase().split(/([0-9]+)/),b=String(right).toLowerCase().split(/([0-9]+)/);
  for(let i=0;i<Math.min(a.length,b.length);i++){
    if(a[i]===b[i])continue;
    if(i%2){
      const x=a[i].replace(/^0+/,'')||'0',y=b[i].replace(/^0+/,'')||'0';
      if(x.length!==y.length)return x.length-y.length;
      if(x!==y)return x<y?-1:1;
    }else{
      const x=Array.from(a[i],c=>c.codePointAt(0)),y=Array.from(b[i],c=>c.codePointAt(0));
      for(let j=0;j<Math.min(x.length,y.length);j++)if(x[j]!==y[j])return x[j]-y[j];
      if(x.length!==y.length)return x.length-y.length;
    }
  }
  return a.length-b.length;
}
export function orderedGroupIndices(comparison, state = {}) {
  const indices=(comparison?.groups||[]).map((_,i)=>i);
  return state.natural_sort?indices.sort((a,b)=>naturalCompare(comparison.groups[a].label,comparison.groups[b].label)):indices;
}
export function orderedImages(comparison, images, state = {}) {
  if(!state.natural_sort)return images;
  const key=id=>comparison?.assets?.[id]?.name||comparison?.assets?.[id]?.path||id;
  const sorted=images.filter(Boolean).sort((a,b)=>naturalCompare(key(a),key(b)));
  let cursor=0;return images.map(id=>id?sorted[cursor++]:id); // 空槽和重复项保留，源数组不变
}
// 来源归属随清单保存；旧分享仅用登记路径匹配，不读取本地或 SSH 文件系统。
const sequenceViews = new WeakMap(), sourceLaneViews = new WeakMap();
function sourcePathKey(path) {
  if (typeof path !== 'string') return '';
  const value = (path.startsWith('ssh://') ? path : path.replace(/\\/g, '/')).replace(/\/+$/, '') || (path ? '/' : '');
  return /^[a-z]:/i.test(value) ? value.toLowerCase() : value;
}
/** 每路保留当前原清单中的相对顺序；重叠来源归第一路，避免转置重复媒体。 */
export function sourceLanes(comparison, state = {}) {
  if (!comparison) return [];
  let cache = sourceLaneViews.get(comparison);
  if (!cache || cache.images !== comparison.images || cache.sources !== comparison.sources || cache.members !== comparison.source_images || cache.assets !== comparison.assets) {
    cache = { images:comparison.images, sources:comparison.sources, members:comparison.source_images, assets:comparison.assets, values:new Map() };
    sourceLaneViews.set(comparison, cache);
  }
  const key = state.natural_sort === true;
  if (cache.values.has(key)) return cache.values.get(key);
  const sources = comparison?.sources || [], images = comparison?.images || [];
  const owners = new Map(), lanes = sources.map(() => []);
  if (Array.isArray(comparison?.source_images) && comparison.source_images.length === sources.length) {
    comparison.source_images.forEach((ids, lane) => { if (Array.isArray(ids)) for (const id of ids) if (!owners.has(id)) owners.set(id, lane); });
  }
  const paths = sources.map(source => sourcePathKey(source.path));
  images.forEach((id, index) => {
    let lane = owners.get(id);
    if (lane === undefined) {
      const path = sourcePathKey(comparison?.assets?.[id]?.path);
      lane = path ? paths.findIndex(source => source && (path === source || path.startsWith(source.endsWith('/') ? source : source + '/'))) : -1;
    }
    if (lane >= 0 && lane < lanes.length) lanes[lane].push(index);
  });
  if (state.natural_sort) for (const lane of lanes) lane.sort((a,b) => naturalCompare(comparison.assets?.[images[a]]?.name || comparison.assets?.[images[a]]?.path || images[a], comparison.assets?.[images[b]]?.name || comparison.assets?.[images[b]]?.path || images[b]));
  cache.values.set(key, lanes);
  return lanes;
}
/**
 * 序列转置改变完整显示序列，然后才分页；与物理行列完全独立。
 * 返回原清单索引让预览拖动仍能定位真实项，关闭开关则逐项恢复原顺序。
 * 以不可变清单引用缓存，避免每次画布/缩略图刷新重新遍历长序列。
 */
export function displayImageIndices(comparison, state = {}) {
  if (!comparison) return [];
  let cache = sequenceViews.get(comparison);
  if (!cache || cache.images !== comparison.images || cache.sources !== comparison.sources || cache.members !== comparison.source_images || cache.assets !== comparison.assets || cache.combine !== comparison.combine) {
    cache = { images:comparison.images, sources:comparison.sources, members:comparison.source_images, assets:comparison.assets, combine:comparison.combine, values:new Map() };
    sequenceViews.set(comparison, cache);
  }
  const transposed = state.transposed === true && !comparison.groups?.length;
  const key = `${transposed}:${state.natural_sort === true}`;
  if (cache.values.has(key)) return cache.values.get(key);
  const images = comparison.images || [], base = images.map((_,i) => i);
  let indices;
  if (transposed) {
    const lanes = sourceLanes(comparison, state), used = new Set(lanes.flat());
    indices = [];
    if (comparison.combine === 'interleave') indices = lanes.flat();
    else for(let i=0,length=Math.max(0,...lanes.map(lane=>lane.length));i<length;i++) for(const lane of lanes) if(i<lane.length) indices.push(lane[i]);
    indices.push(...base.filter(index => !used.has(index)));
  } else if (state.natural_sort) {
    const sorted = base.filter(i=>images[i]).sort((a,b)=>naturalCompare(comparison.assets?.[images[a]]?.name || comparison.assets?.[images[a]]?.path || images[a], comparison.assets?.[images[b]]?.name || comparison.assets?.[images[b]]?.path || images[b]));
    let cursor=0; indices=base.map(i=>images[i]?sorted[cursor++]:i);
  } else indices = base;
  cache.values.set(key, indices);
  return indices;
}
export function pageCount(comparison, layout, state = {}) {
  if (comparison?.groups?.length) return state.grouping==='within'
    ? comparison.groups.reduce((sum,group)=>sum+Math.max(1,Math.ceil(group.images.length/cellsOf(layout))),0)
    : Math.max(1,...comparison.groups.map(group=>group.images.length));
  return Math.max(1, Math.ceil((comparison?.images?.length || 0) / cellsOf(layout)));
}
export function pageInfo(comparison, layout, page, state = {}) {
  const cells=cellsOf(layout), groups=comparison?.groups;
  if (!groups?.length) return {start:page*cells,end:Math.min(comparison?.images?.length||0,(page+1)*cells),groupIndex:-1,groupPage:page,groupPages:pageCount(comparison,layout,state)};
  if(state.grouping==='within'){
    let offset=0;
    for(const index of orderedGroupIndices(comparison,state)){
      const group=groups[index],pages=Math.max(1,Math.ceil(group.images.length/cells));
      if(page<offset+pages)return {start:(page-offset)*cells,end:Math.min(group.images.length,(page-offset+1)*cells),groupIndex:index,groupPage:page-offset,groupPages:pages};
      offset+=pages;
    }
    return {start:0,end:0,groupIndex:-1,groupPage:0,groupPages:1};
  }
  return {start:page,end:page+1,groupIndex:-1,groupPage:page,groupPages:pageCount(comparison,layout,state)};
}
/** aligned 跨组按序号对齐；within 逐组按布局切页；自然排序只影响显示副本。 */
export function slotAssets(comparison, state) {
  const page=pageInfo(comparison,state.layout,state.group_index,state);
  if (comparison?.groups?.length){
    if(state.grouping==='within')return orderedImages(comparison,comparison.groups[page.groupIndex]?.images||[],state).slice(page.start,page.end);
    return orderedGroupIndices(comparison,state).map(index=>orderedImages(comparison,comparison.groups[index].images,state)[state.group_index]||null);
  }
  return displayImageIndices(comparison,state).slice(page.start,page.end).map(index=>comparison.images[index]);
}
/** 跨方法对齐时移除整路；普通清单和组内浏览移除单项，原清单保持完整。 */
export function removeSlot(comparison, state, slot) {
  if (!comparison || !Number.isInteger(slot) || slot < 0) return comparison;
  if (comparison.groups?.length && state.grouping!=='within') {
    const groupIndex=orderedGroupIndices(comparison,state)[slot];
    if (groupIndex===undefined) return comparison;
    const groups=comparison.groups.filter((_,i)=>i!==groupIndex);
    return {...comparison,groups,images:groups.flatMap(group=>group.images)};
  }
  if (!slotAssets(comparison,state)[slot]) return comparison;
  const page=pageInfo(comparison,state.layout,state.group_index,state);
  if (comparison.groups?.length) {
    const groupIndex=page.groupIndex;
    const group=comparison.groups[groupIndex];
    const position=page.start+slot;
    const index=displayImageIndices({...comparison,groups:[],images:group.images},{natural_sort:state.natural_sort})[position];
    const groups=comparison.groups.map((item,i)=>i===groupIndex?{...item,images:item.images.filter((_,j)=>j!==index)}:item).filter(item=>item.images.length);
    return {...comparison,groups,images:groups.flatMap(item=>item.images)};
  }
  const index=displayImageIndices(comparison,state)[page.start+slot];
  return {...comparison,images:comparison.images.filter((_,i)=>i!==index)};
}
function normalizeRect(view) {
  return view && ['x','y','width','height'].every(k => Number.isFinite(view[k])) && view.width > 0 && view.height > 0
    ? { x: clamp(view.x, -100000, 100000, 0), y: clamp(view.y, -100000, 100000, 0), width: Math.min(100000,view.width), height: Math.min(100000,view.height) } : null;
}
export function normalizeViewState(input, slots = 1, pages = 1) {
  const c = input || {}, view = normalizeRect(c.view);
  const panels = Array.isArray(c.panels) ? c.panels.slice(0,Math.min(64,slots)).map(p => ({
    view: normalizeRect(p?.view), rotation: [0,1,2,3].includes(p?.rotation) ? p.rotation : 0,
    flip_x: p?.flip_x===true, flip_y: p?.flip_y===true, stretch: clamp(p?.stretch,.0001,10000,1),
  })) : [];
  return { version: 1,
    group_index: Math.trunc(clamp(Number(c.group_index), 0, Math.max(0,pages-1), 0)),
    mode: ['split','wipe','diff'].includes(c.mode) ? c.mode : 'split',
    direction: c.direction === -1 ? -1 : 1,
    diff_base: Math.trunc(clamp(Number(c.diff_base),0,Math.max(0,slots-1),0)),
    opacity: clamp(c.opacity,0,1,.5), wipe: clamp(c.wipe,0,1,.5),
    gain: clamp(c.gain,1,128,4), exposure: clamp(c.exposure,-8,8,0), gamma: clamp(c.gamma,.1,4,1),
    brightness_ref: Number.isInteger(c.brightness_ref)?Math.max(0,Math.min(slots-1,c.brightness_ref)):null,
    channel: ['rgb','r','g','b'].includes(c.channel) ? c.channel : 'rgb',
    layout: validLayout(c.layout) ? c.layout : '1x2',
    quality: ['auto','high','exact'].includes(c.quality) ? c.quality : 'auto',
    ...(c.video_frame!==undefined?{video_frame:Math.trunc(clamp(c.video_frame,0,500000,0)),video_offsets:Object.fromEntries(Object.entries(c.video_offsets||{}).slice(0,500).filter(([,v])=>Number.isInteger(v)&&Math.abs(v)<=500000))}:{}),
    grouping: c.grouping==='within'?'within':'aligned', natural_sort: c.natural_sort===true, transposed: c.transposed===true, raw: normalizeRawSettings(c.raw), view, panels };
}
/** An absent source stays absent. 边缘槽默认保持原图（保留未覆盖的内容）；
 * edgeDiff=true 时水平边缘槽才显示与内侧邻居的差分（可选项）。
 * held 为四向或序列叠图：序列沿当前页槽位顺序跨行，不切换页。
 * @param {Array<string|null>} ids
 * @param {string} mode
 * @param {number} direction
 * @param {string|null} held
 * @param {number} diffBase
 * @param {number} columns
 * @param {boolean} edgeDiff
 */
export function panelPlan(ids, mode, direction, held = null, diffBase = 0, columns = 1, edgeDiff = false) {
  const baseline = Math.max(0,Math.min(ids.length-1,Math.trunc(Number(diffBase)||0)));
  const cols = Math.max(1, Math.trunc(Number(columns) || 1));
  const stepOf = { left: 1, right: -1, up: cols, down: -cols, 'seq-next': 1, 'seq-prev': -1 };
  return ids.map((id,i)=> {
    if (!id) return { kind:'missing', left:null, right:null };
    if (held === null && mode === 'split') return {kind:'image', left:id, right:null};
    if (held === null && mode === 'diff') {
      const other=ids[baseline];
      if (!other) return {kind:'missing',left:null,right:null};
      return i===baseline ? {kind:'image',left:id,right:null} : {kind:'diff',left:id,right:other};
    }
    if (held !== null) {
      const step = stepOf[held] ?? 1;
      const target = i + step;
      let edge = target < 0 || target >= ids.length;
      if (cols > 1 && held === 'left' && i % cols === cols-1) edge = true;   // 不跨行；单列时 ±1 即上下邻
      if (cols > 1 && held === 'right' && i % cols === 0) edge = true;
      if (edge && (held === 'up' || held === 'down')) return {kind:'image', left:id, right:null};
      if (edge) {
        if (!edgeDiff) return {kind:'image', left:id, right:null};
        const inner = ids[i - step];
        return inner ? {kind:'diff',left:id,right:inner} : {kind:'image', left:id, right:null};
      }
      const other = ids[target];
      if (!other) return {kind:'image', left:id, right:null};
      return {kind:'image', left:other, right:null};
    }
    const target = i + direction;
    const edge = target < 0 || target >= ids.length;
    if (edge) {
      if (!edgeDiff) return {kind:'image', left:id, right:null};
      const other = ids[i-direction];
      return other ? {kind:'diff',left:id,right:other} : {kind:'image', left:id, right:null};
    }
    const other = ids[target];
    if (!other) return {kind:'missing',left:null,right:null};
    return {kind:mode,left:id,right:other};
  });
}
export function viewForShare(state, slots, pages) { return normalizeViewState(state,slots,pages); }
export function nextGroup(state,count,step) { return {...state,group_index:Math.max(0,Math.min(Math.max(0,count-1),state.group_index+step))}; }
/** 秒 → 人类可读保留时长（24 小时 / 30 分钟 / 45 秒），用于访客保留规则文案。 */
export function humanTtl(seconds) {
  const value = Math.max(0, Math.trunc(Number(seconds) || 0));
  if (value >= 3600 && value % 3600 === 0) return t`${value / 3600} 小时`;
  if (value >= 60 && value % 60 === 0) return t`${value / 60} 分钟`;
  return t`${value} 秒`;
}

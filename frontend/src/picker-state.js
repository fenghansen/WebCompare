/**
 * 文件选择只存路径、名称和类型；不保存目录实体或异步任务。
 * @typedef {{path:string, label:string, kind?:'directory'|'image'|'video'}} PickerSource
 * @typedef {{path:string, name:string, kind:'directory'|'image'|'video'}} PickerEntry
 */
const snapshot=items=>items.map(item=>({...item}));
const same=(left,right)=>left.length===right.length&&left.every((item,index)=>
  item.path===right[index].path&&item.label===right[index].label&&item.kind===right[index].kind);

/** 选择历史有界；实体改名、删除和会话切换由调用方清理，不会重放文件操作。 */
export function createSelectionHistory(limit=60) {
  /** @type {PickerSource[][]} */
  const past=[],future=[];
  return {
    get canUndo(){return past.length>0;},
    get canRedo(){return future.length>0;},
    /** @param {PickerSource[]} before @param {PickerSource[]} after */
    record(before,after){
      if(same(before,after))return false;
      past.push(snapshot(before));if(past.length>limit)past.shift();future.length=0;return true;
    },
    /** @param {PickerSource[]} current @returns {PickerSource[]|null} */
    undo(current){if(!past.length)return null;future.push(snapshot(current));return snapshot(past.pop());},
    /** @param {PickerSource[]} current @returns {PickerSource[]|null} */
    redo(current){if(!future.length)return null;past.push(snapshot(current));return snapshot(future.pop());},
    reset(){past.length=0;future.length=0;},
  };
}

/**
 * 只按实际可见 entries 的顺序取范围，不按名称重新排序。
 * 普通单击延续原有切换行为并建立锚点；Shift 替换为范围，Ctrl/Cmd+Shift 追加范围。
 * @param {PickerSource[]} current
 * @param {PickerEntry[]} entries
 * @param {string} path
 * @param {string|null} anchor
 * @param {{shift?:boolean, additive?:boolean, limit?:number}} options
 */
export function selectPickerEntry(current,entries,path,anchor,{shift=false,additive=false,limit=500}={}) {
  const index=entries.findIndex(entry=>entry.path===path),start=entries.findIndex(entry=>entry.path===anchor);
  if(index<0)return {selection:snapshot(current),anchor,limited:false};
  const source=entry=>({path:entry.path,label:entry.name,kind:entry.kind});
  let next;
  if(shift&&start>=0){
    const range=entries.slice(Math.min(index,start),Math.max(index,start)+1).map(source);
    const existing=new Set(current.map(item=>item.path));
    next=additive?[...current,...range.filter(item=>!existing.has(item.path))]:range;
  }else{
    next=current.some(item=>item.path===path)?current.filter(item=>item.path!==path):[...current,source(entries[index])];
    anchor=path;
  }
  if(next.length>limit)return {selection:snapshot(current),anchor,limited:true};
  return {selection:snapshot(next),anchor,limited:false};
}

/** 当前页全选保留跨目录来源，超限整次拒绝，避免悄悄少选。 */
/** @param {PickerSource[]} current @param {PickerEntry[]} entries */
export function selectVisibleEntries(current,entries,remove=false,limit=500) {
  const visible=new Set(entries.map(entry=>entry.path)),existing=new Set(current.map(item=>item.path));
  const next=remove?current.filter(item=>!visible.has(item.path)):
    [...current,...entries.filter(entry=>!existing.has(entry.path)).map(entry=>({path:entry.path,label:entry.name,kind:entry.kind}))];
  return {selection:snapshot(next.length>limit?current:next),limited:next.length>limit};
}

/** Ctrl/Cmd+A 按当前页的实际勾选状态切换；取消本页时保留其他目录的选择。 */
export function toggleVisibleEntries(current,entries,limit=500) {
  const selected=new Set(current.map(item=>item.path));
  return selectVisibleEntries(current,entries,entries.length>0&&entries.every(entry=>selected.has(entry.path)),limit);
}

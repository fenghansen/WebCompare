import { t } from './i18n.js';
const safeId=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(value);
const safePath=value=>typeof value==='string'&&value.length>0&&value.length<=4096&&!/[\x00-\x1f\x7f]/.test(value)&&!value.split('/').includes('..');
const virtualPath=value=>safePath(value)&&!value.startsWith('/')&&!/^[A-Za-z][A-Za-z0-9+.-]*:/.test(value);

/** 只有 server 的虚拟目录可出现在 URL；绝对路径和 SSH 目录留在当前历史记录中。 */
export function navigationTarget({directory,comparison,media='image',mode,owner,key}) {
  const params=new URLSearchParams(),state={};
  if(safePath(directory)){
    if(mode==='server'&&virtualPath(directory))params.set('dir',directory);
    else{
      if(!safeId(key))throw new Error(t('目录恢复标识无效'));
      params.set('dir','@'+key);
      state.webcompare={key,directory,owner,mode};
    }
  }
  if(safeId(comparison))params.set('comparison',comparison);
  if(media==='video')params.set('media','video');
  return {url:'/'+(params.size?'?'+params:''),state};
}

/** 身份确定后再解析目录；分享链接独立恢复，历史记录也不能跨账号使用。 */
export function readNavigation(pathname,search,state,{mode,owner}) {
  if(pathname!=='/')return null;
  const params=new URLSearchParams(search);
  /** @type {{media:'image'|'video',comparison?:string,directory?:string}} */
  const target={media:params.get('media')==='video'?'video':'image'};
  const comparison=params.get('comparison');
  if(safeId(comparison))target.comparison=comparison;
  const directory=params.get('dir'),saved=state?.webcompare;
  if(directory?.startsWith('@')){
    if(saved&&directory==='@'+saved.key&&saved.owner===owner&&saved.mode===mode&&safePath(saved.directory))target.directory=saved.directory;
  }else if(mode==='server'&&virtualPath(directory))target.directory=directory;
  return target;
}

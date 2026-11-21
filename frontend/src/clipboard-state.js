import { t } from './i18n.js';
/** 剪贴板只携带来源描述符；文件内容与目录展开一律等到用户粘贴后再处理。 */
export const FILE_COPY_MIME='application/x-webcompare-file-copy+json';
export const FILE_COPY_TEXT_PREFIX='WebCompare file copy v1\n';
export const MAX_FILE_COPY_BYTES=64*1024;
const encoder=new TextEncoder();
const bytes=value=>encoder.encode(value).byteLength;

function checkedPath(path) {
  if(typeof path!=='string'||!path.length||path.length>4096||path.includes('\0'))throw new Error(t('剪贴板文件路径无效或过长。'));
  let parts=path;
  if(path.startsWith('ssh://')){
    const remote=path.match(/^ssh:\/\/([A-Za-z0-9_-]+)(\/.*)$/);
    if(!remote)throw new Error(t('剪贴板 SSH 来源无效。'));
    try{parts=decodeURIComponent(remote[2]);}catch{throw new Error(t('剪贴板 SSH 路径编码无效。'));}
  }else if(/^[A-Za-z][A-Za-z0-9+.-]*:\/\//.test(path))throw new Error(t('剪贴板来源不是 WebCompare 文件路径。'));
  if(parts.includes('\0')||parts.split('/').includes('..'))throw new Error(t('剪贴板路径不能包含上级目录跳转。'));
  return path;
}

function checkedDescriptor(value,context) {
  if(!value||value.version!==1||value.origin!==context.origin||value.owner!==context.owner)throw new Error(t('剪贴板文件来自其他站点或账号，请在当前账号重新复制。'));
  if(value.operation!==undefined&&!['copy','move'].includes(value.operation))throw new Error(t('剪贴板文件操作无效。'));
  if(!['image','video'].includes(value.media_kind)||typeof value.recursive!=='boolean'||!Array.isArray(value.sources)||!value.sources.length||value.sources.length>500)throw new Error(t('剪贴板文件清单无效或超过 500 项。'));
  const seen=new Set();
  const sources=value.sources.map(source=>{
    const path=checkedPath(source?.path),kind=source?.kind;
    if(!['directory',value.media_kind].includes(kind)||seen.has(path))throw new Error(t('剪贴板文件类型无效或包含重复来源。'));
    if(value.operation==='move'&&path.startsWith('ssh://'))throw new Error(t('SSH 来源只支持复制，不能剪切或移动。'));
    seen.add(path);return {path,kind};
  });
  return {version:1,origin:context.origin,owner:context.owner,media_kind:value.media_kind,recursive:value.recursive,sources,...(value.operation==='move'?{operation:'move'}:{})};
}

/** 可跨标签页识别的文本后备不包含账号口令、CSRF、媒体字节或授权能力。 */
export function encodeFileCopy(sources,{origin,owner,media_kind,recursive,operation='copy'}) {
  const descriptor=checkedDescriptor({version:1,origin,owner,media_kind,recursive,operation,sources:sources.map(source=>({path:source.path,kind:source.kind||media_kind}))},{origin,owner});
  const json=JSON.stringify(descriptor),text=FILE_COPY_TEXT_PREFIX+json;
  if(bytes(text)>MAX_FILE_COPY_BYTES)throw new Error(t('复制清单超过 64 KiB，请减少所选文件。'));
  return {descriptor,json,text};
}

/** 只读取这一次 paste 事件的数据，绝不以旧的内存快照覆盖用户后来复制的内容。 */
export function readFileCopy(clipboard,context) {
  const custom=clipboard.getData(FILE_COPY_MIME),text=custom?'':clipboard.getData('text/plain');
  if(!custom&&!text.startsWith(FILE_COPY_TEXT_PREFIX))return null;
  const json=custom||text.slice(FILE_COPY_TEXT_PREFIX.length);
  if(bytes(json)>MAX_FILE_COPY_BYTES)throw new Error(t('剪贴板文件清单超过 64 KiB，请重新选择后复制。'));
  let value;try{value=JSON.parse(json);}catch{throw new Error(t('剪贴板文件清单损坏，请重新复制。'));}
  return checkedDescriptor(value,context);
}

/** 目标路径在粘贴时才已知；发送前再次按真实 UTF-8 JSON 字节数检查 HTTP 上限。 */
export function fileCopyRequest(descriptor,destination) {
  const body={paths:descriptor.sources.map(source=>checkedPath(source.path)),destination:checkedPath(destination),recursive:descriptor.recursive,media_kind:descriptor.media_kind};
  if(bytes(JSON.stringify(body))>MAX_FILE_COPY_BYTES)throw new Error(t('粘贴请求超过 64 KiB，请减少所选文件或使用较短的目标路径。'));
  return body;
}

export function fileMoveRequest(descriptor,destination) {
  const copy=fileCopyRequest(descriptor,destination);
  if(copy.paths.some(path=>path.startsWith('ssh://'))||copy.destination.startsWith('ssh://'))throw new Error(t('SSH 目录不支持移动。'));
  return {paths:copy.paths,destination:copy.destination};
}

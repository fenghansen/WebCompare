/** SSH URI 是来源的持久身份；只解码路径部分用于显示，不用 URL 规则解释文件名中的 ?/#。 */
export function sshLocation(path) {
  if(typeof path!=='string')return null;
  const match=path.match(/^ssh:\/\/([^/]+)(\/.*)?$/);
  if(!match)return null;
  const remotePath=match[2]||'/';
  try{return {id:match[1],path:decodeURIComponent(remotePath)};}catch{return {id:match[1],path:remotePath};}
}

export function sshPath(id,path) {
  const absolute=path.startsWith('/')?path:'/'+path;
  // 与后端 quote(path, safe='/') 一致；% 本身必须编码，避免下一次读取改变文件名。
  const encoded=absolute.split('/').map(part=>encodeURIComponent(part).replace(/[!'()*]/g,character=>'%'+character.charCodeAt(0).toString(16).toUpperCase())).join('/');
  return `ssh://${id}${encoded}`;
}

export function sourceName(path) {
  const remote=sshLocation(path),value=remote?.path||path;
  return value.split(remote?'/':/[\\/]/).pop()||value;
}

/** 输入框展示远端绝对路径；提交时补回连接身份，也允许直接粘贴完整 URI。 */
export function resolveSSHPath(input,current,home='') {
  const location=sshLocation(current);
  if(!location||input.startsWith('ssh://'))return input;
  const remoteHome=sshLocation(home)?.path||'/';
  if(!input||input==='~')return sshPath(location.id,remoteHome);
  if(input.startsWith('~/'))return sshPath(location.id,remoteHome.replace(/\/$/,'')+'/'+input.slice(2));
  return sshPath(location.id,input);
}

/** 根目录必须保留末尾 /，防止手选根目录中的文件生成 ssh://id 形式的分组规则。 */
export function sourceParent(path) {
  const location=sshLocation(path);
  if(location){
    const split=location.path.replace(/\/$/,'').lastIndexOf('/');
    return sshPath(location.id,split>0?location.path.slice(0,split):'/');
  }
  const split=Math.max(path.lastIndexOf('/'),path.lastIndexOf('\\')),parent=path.slice(0,split);
  return /^[A-Za-z]:$/.test(parent)?parent+'\\':parent||'/';
}

export function isSSHError(error) {
  return !!error&&typeof error.code==='string'&&error.code.startsWith('SSH_');
}

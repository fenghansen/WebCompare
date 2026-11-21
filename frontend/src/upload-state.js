import { t } from './i18n.js';
export const uploadConcurrency=profile=>profile==='intranet'?4:2;
export const imageUploadExtensions='.png,.jpg,.jpeg,.webp';

export function clipboardImageExtension(type) {
  if(type==='image/webp')return 'webp';
  return type==='image/jpeg'?'jpg':'png';
}

export async function checkImageUploadHeader(file) {
  const extension=file.name.slice(file.name.lastIndexOf('.')).toLowerCase();
  if(!imageUploadExtensions.split(',').includes(extension))throw new Error(t('只接受 PNG、JPEG 或静态 WebP 图片'));
  if(file.size>256*1024**2)throw new Error(t('单张图片不能超过 256 MiB'));
  const head=new Uint8Array(await file.slice(0,32).arrayBuffer());
  if(extension==='.webp'){
    const tag=offset=>String.fromCharCode(...head.subarray(offset,offset+4));
    if(head.length<20||tag(0)!=='RIFF'||tag(8)!=='WEBP')throw new Error(t('图片内容与文件格式不符'));
    if(tag(12)==='VP8X'&&(head[20]&0x02))throw new Error(t('当前只支持静态 WebP 图片'));
    return 'webp';
  }
  if(extension==='.png'){
    if(![137,80,78,71,13,10,26,10].every((value,i)=>head[i]===value))throw new Error(t('图片内容与文件格式不符'));
    if(head[24]>8)throw new Error(t('当前只支持 8-bit PNG，不会自动量化高位深图片'));
    return 'png';
  }
  if(head[0]!==255||head[1]!==216||head[2]!==255)throw new Error(t('图片内容与文件格式不符'));
  return 'jpeg';
}

/** 同一批次只启动有限请求；取消后不再领取新文件，已启动任务由共享 signal 取消。 */
export async function runBounded(items,limit,task,signal) {
  if(!Number.isInteger(limit)||limit<1)throw new Error(t('并发数必须为正整数'));
  let next=0;
  await Promise.all(Array.from({length:Math.min(limit,items.length)},async()=>{
    while(!signal?.aborted){
      const index=next++;
      if(index>=items.length)return;
      await task(items[index],index);
    }
  }));
}

/** Promise 在请求完成前就登记，避免并发文件重复创建同一层目录。 */
export async function prepareUploadDirectory(root,relativePath,pending,create) {
  let directory=root.replace(/\/$/,'');
  for(const part of relativePath.split('/').slice(0,-1)){
    directory+='/'+part;
    if(!pending.has(directory))pending.set(directory,Promise.resolve().then(()=>create(directory)));
    await pending.get(directory);
  }
  return directory;
}

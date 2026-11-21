import { t } from './i18n.js';
/** 视频逐帧模式目前为实验功能。维护者尚未完成浏览器与桌面端的端到端人工验收。 */
import type { Input, VideoSampleSink, VideoSample } from 'mediabunny';
import type { Asset, Metadata } from './api';
import { videoFetch } from './api';

const uploaded=new Map<string,{file:File;revision:string}>();
const remoteVideoOwners=new Map<VideoFrames,string>();
export const videoExtensions='.mp4,.mov,.m4v,.webm,.mkv';
export function rememberVideo(path:string,file:File,revision:string) { uploaded.delete(path);uploaded.set(path,{file,revision});while(uploaded.size>32)uploaded.delete(uploaded.keys().next().value!); }
export function forgetVideos() { uploaded.clear(); }
export function disconnectRemoteVideos(connectionId:string) { for(const [video,owner] of remoteVideoOwners)if(owner===connectionId)video.dispose(); }
const cancelled=()=>new DOMException(t('已取消'),'AbortError');

/** 封面只读取首帧，串行调用方限制并发；不扫描整段帧索引。 */
export async function videoPoster(source:File|string,signal:AbortSignal) {
  if(!globalThis.isSecureContext||typeof VideoDecoder==='undefined')throw new Error(t('视频解码需要 HTTPS/localhost 和 WebCodecs 浏览器'));
  const {ALL_FORMATS,BlobSource,Input,UrlSource,VideoSampleSink}=await import('mediabunny');
  if(signal.aborted)throw cancelled();
  const input=new Input({source:typeof source==='string'?new UrlSource(source,{maxCacheSize:512*1024,parallelism:1,fetchFn:videoFetch,requestInit:{credentials:'same-origin'},getRetryDelay:(n,error)=>String((error as {code?:string})?.code||'').startsWith('SSH_')?null:n<3?n:null,handleUnhandledError:()=>{}}):new BlobSource(source,{maxCacheSize:512*1024}),formats:ALL_FORMATS});
  const abort=()=>input.dispose();signal.addEventListener('abort',abort,{once:true});
  try {
    const track=await input.getPrimaryVideoTrack();
    if(!track||!await track.canDecode())throw new Error(t('当前浏览器无法解码此视频'));
    const width=await track.getDisplayWidth(),height=await track.getDisplayHeight();
    if(!width||!height||Math.max(width,height)>8192||width*height>4096*2160)throw new Error(t('视频尺寸超过逐帧对比范围'));
    const sample=await new VideoSampleSink(track).getSample(Math.max(0,await track.getFirstTimestamp()));
    if(!sample)throw new Error(t('视频没有可读取的首帧'));
    try {
      const canvas=document.createElement('canvas');canvas.width=160;canvas.height=100;
      const scale=Math.min(160/sample.displayWidth,100/sample.displayHeight),w=sample.displayWidth*scale,h=sample.displayHeight*scale;
      sample.draw(canvas.getContext('2d')!,(160-w)/2,(100-h)/2,w,h);return canvas.toDataURL('image/jpeg',.75);
    } finally {sample.close();}
  } finally {signal.removeEventListener('abort',abort);input.dispose();}
}

/** 浏览器逐帧源：容器包的呈现时间戳定义帧序号，顺序访问复用解码器；不按 FPS 寻址。 */
export class VideoFrames {
  private input:Input|null=null;
  timestamps:number[]=[];
  metadata:Metadata|null=null;
  private sink:VideoSampleSink|null=null;
  private iterator:AsyncGenerator<VideoSample,void,unknown>|null=null;
  private last=-1;
  private disposed=false;
  private ready:Promise<void>|null=null;
  private serial:Promise<unknown>=Promise.resolve();
  constructor(private source:File|string,sourcePath?:string) { const owner=sourcePath?.match(/^ssh:\/\/([^/]+)\//)?.[1];if(owner)remoteVideoOwners.set(this,owner); }
  static asset(asset:Asset,share?:string) {
    const file=uploaded.get(asset.path);
    return new VideoFrames(file&&file.file.size===asset.bytes&&file.revision===asset.revision?file.file:`/api/assets/${encodeURIComponent(asset.id)}/video${share?'?share='+encodeURIComponent(share):''}`,asset.path);
  }
  init() {
    if(this.disposed)return Promise.reject(cancelled());
    // 初始化网络失败后清理半成品并允许重试；主动 dispose 的实例始终保持失效。
    return this.ready??=this.initialize().catch(error=>{this.release();this.ready=null;throw error;});
  }
  private async initialize() {
    if(!globalThis.isSecureContext||typeof VideoDecoder==='undefined')throw new Error(t('逐帧解码需要支持 WebCodecs 的浏览器及 HTTPS（本机可用 localhost）'));
    const {ALL_FORMATS,BlobSource,EncodedPacketSink,Input,UrlSource,VideoSampleSink}=await import('mediabunny');
    if(this.disposed)throw cancelled();
    const source=typeof this.source==='string'?new UrlSource(this.source,{maxCacheSize:2*1024**2,parallelism:1,fetchFn:videoFetch,requestInit:{credentials:'same-origin'},getRetryDelay:(n,error)=>String((error as {code?:string})?.code||'').startsWith('SSH_')?null:n<3?Math.min(2,n):null,handleUnhandledError:()=>{}}):new BlobSource(this.source,{maxCacheSize:2*1024**2});
    this.input=new Input({source,formats:ALL_FORMATS});
    const track=await this.input.getPrimaryVideoTrack();
    if(!track||!await track.canDecode())throw new Error(t('此浏览器不支持视频编码，请使用支持该编码的浏览器或转换文件'));
    const width=await track.getDisplayWidth(),height=await track.getDisplayHeight();
    if(!width||!height||Math.max(width,height)>8192||width*height>4096*2160)throw new Error(t('视频逐帧对比暂限 4096×2160 像素以内'));
    this.metadata={width,height,bands:3,format:'png',has_alpha:false,has_icc:false,bit_depth:8};
    // metadataOnly 只收集时间戳，避免把完整视频包和解码帧存进内存。
    for await(const packet of new EncodedPacketSink(track).packets(undefined,undefined,{metadataOnly:true})) {
      if(this.disposed)throw cancelled();
      if(this.timestamps.length>=500000)throw new Error(t('视频超过 50 万帧索引上限，请先截取所需片段'));
      if(!Number.isFinite(packet.timestamp))throw new Error(t('视频时间戳无效'));
      if(packet.timestamp>=0)this.timestamps.push(packet.timestamp);
    }
    this.timestamps.sort((a,b)=>a-b);
    if(!this.timestamps.length||this.timestamps.some((t,i)=>i>0&&t-this.timestamps[i-1]<.000001))throw new Error(t('视频无法建立唯一的逐帧时间戳索引'));
    this.sink=new VideoSampleSink(track,{optimizeForLatency:true});
  }
  /** 同一路解码严格串行，快速拖动只保留尚未过期的请求。 */
  frame(index:number,signal:AbortSignal):Promise<ImageBitmap> {
    const job=this.serial.catch(()=>{}).then(async()=>{
      await this.init();
      if(this.disposed||signal.aborted)throw cancelled();
      if(!Number.isInteger(index)||index<0||index>=this.timestamps.length)throw new Error(t`帧超出范围（1–${this.timestamps.length}）`);
      if(!this.iterator||index<=this.last||index>this.last+12) {
        await this.iterator?.return();
        this.iterator=this.sink!.samples(this.timestamps[index]);this.last=index-1;
      }
      while(this.last<index) {
        const next=await this.iterator.next();
        if(next.done)throw new Error(t('解码提前结束，容器帧索引不完整'));
        const sample=next.value;
        try {
          this.last++;
          if(Math.abs(sample.timestamp-this.timestamps[this.last])>.000002)throw new Error(t('解码帧与容器时间戳不一致，已停止以免显示错误帧'));
          if(this.disposed||signal.aborted)throw cancelled();
          if(this.last===index) {
            const canvas=new OffscreenCanvas(sample.displayWidth,sample.displayHeight);
            sample.draw(canvas.getContext('2d')!,0,0);
            return canvas.transferToImageBitmap();
          }
        } finally { sample.close(); }
      }
      throw new Error(t('视频帧读取失败'));
    });
    this.serial=job;return job;
  }
  private release() { void this.iterator?.return().catch(()=>undefined);this.iterator=null;this.input?.dispose();this.input=null;this.timestamps=[];this.metadata=null;this.sink=null;this.last=-1; }
  dispose() { this.disposed=true;remoteVideoOwners.delete(this);this.release(); }
}
export async function checkVideo(file:File,signal:AbortSignal) {
  if(!/\.(mp4|mov|m4v|webm|mkv)$/i.test(file.name))throw new Error(t('请选择 MP4/MOV/M4V/WebM/MKV 视频'));
  if(file.size>256*1024**2)throw new Error(t('单个上传视频不能超过 256 MiB'));
  await videoPoster(file,signal);
}

import { videoFetch, type ApiError } from './api';
import { MediaMemoryError } from './media-memory';

const headerBytes = 16;
const cancelled = () => new DOMException('Cancelled', 'AbortError');

/** Owns one optional local NVDEC reader and its ordered frame requests. */
export class LocalNVDEC {
  private readonly readerId = globalThis.crypto?.randomUUID?.();
  private readonly closed = new AbortController();
  private pending: Promise<void> = Promise.resolve();
  private requested = false;

  constructor(private readonly assetId: string, private readonly share: string | undefined, private readonly csrf: string) {}

  private url(query: URLSearchParams) {
    if (this.share) query.set('share', this.share);
    return `/api/assets/${encodeURIComponent(this.assetId)}/nvdec/${this.readerId}${query.size ? '?' + query : ''}`;
  }

  async frame(ptsUs: number, previousUs: number | null, width: number, height: number,
              colorSpace: VideoColorSpaceInit, signal: AbortSignal, previewWidth?: number): Promise<VideoFrame> {
    const closed = this.closed.signal;
    const ensureActive = () => { if (signal.aborted || closed.aborted) throw cancelled(); };
    ensureActive();
    if (!this.readerId || typeof VideoFrame !== 'function') throw new Error('Local NVDEC requires WebCodecs and a secure browser context');
    if (!Number.isSafeInteger(ptsUs) || (previousUs !== null && !Number.isSafeInteger(previousUs))) {
      throw new Error('Invalid local NVDEC frame timestamp');
    }
    if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 ||
        width > 0xffff || height > 0xffff ||
        (previewWidth !== undefined && (!Number.isSafeInteger(previewWidth) || previewWidth < 1))) {
      throw new Error('Invalid local NVDEC frame dimensions');
    }
    const targetWidth = Math.min(width, previewWidth ?? width);
    const targetHeight = Math.max(1, Math.floor((height * targetWidth + Math.floor(width / 2)) / width));
    const chromaPlaneBytes = Math.ceil(targetWidth / 2) * Math.ceil(targetHeight / 2);
    const payloadBytes = targetWidth * targetHeight + 2 * chromaPlaneBytes;
    if (payloadBytes > 0xffffffff) throw new Error('Invalid local NVDEC frame dimensions');
    const query = new URLSearchParams({pts_us: String(ptsUs)});
    if (previousUs !== null) query.set('previous_us', String(previousUs));
    if (previewWidth !== undefined) query.set('preview_width', String(previewWidth));
    const operation = this.pending.then(async () => {
      ensureActive();
      this.requested = true;
      // Drain an already sent POST before DELETE so a late request cannot reopen the reader.
      const response = await videoFetch(this.url(query), {method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRF-Token': this.csrf}}).catch(error => {
        if ((error as ApiError | null)?.status === 507) throw new MediaMemoryError();
        throw error;
      });
      const buffer = await response.arrayBuffer();
      ensureActive();
      if (buffer.byteLength !== headerBytes + payloadBytes) throw new Error('Invalid local NVDEC frame length');
      const header = new DataView(buffer, 0, headerBytes);
      if (header.getBigInt64(0, true) !== BigInt(ptsUs) || header.getUint16(8, true) !== targetWidth ||
          header.getUint16(10, true) !== targetHeight || header.getUint32(12, true) !== payloadBytes) {
        throw new Error('Invalid local NVDEC frame header');
      }
      const init: VideoFrameBufferInit & {transfer: ArrayBuffer[]} = {format: 'I420', codedWidth: targetWidth,
        codedHeight: targetHeight, timestamp: ptsUs, colorSpace, transfer: [buffer]};
      return new VideoFrame(new Uint8Array(buffer, headerBytes), init);
    });
    this.pending = operation.then(() => undefined, () => undefined);
    return new Promise((resolve, reject) => {
      const removeListeners = () => {
        signal.removeEventListener('abort', abort);
        closed.removeEventListener('abort', abort);
      };
      const abort = () => { removeListeners(); reject(cancelled()); };
      signal.addEventListener('abort', abort, {once: true});
      closed.addEventListener('abort', abort, {once: true});
      void operation.then(frame => {
        removeListeners();
        if (signal.aborted || closed.aborted) {
          frame.close();
          reject(cancelled());
        } else resolve(frame);
      }, error => { removeListeners(); reject(error); });
    });
  }

  close() {
    if (this.closed.signal.aborted) return;
    this.closed.abort();
    if (!this.requested) return;
    void this.pending.then(async () => {
      const response = await videoFetch(this.url(new URLSearchParams()), {method: 'DELETE',
        credentials: 'same-origin', headers: {'X-CSRF-Token': this.csrf}, keepalive: true});
      await response.body?.cancel();
    }).catch(() => undefined);
  }
}

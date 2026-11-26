import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../src/nvdec.ts', import.meta.url), 'utf8')
  .replace(/^import .* from '\.\/api';$/m, 'const videoFetch=(...args)=>globalThis.nvdecFetch(...args);')
  .replace(/^import .* from '\.\/media-memory';$/m, 'const MediaMemoryError=globalThis.NvdecMemoryError;');
const compiled = ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022}}).outputText;
class NvdecMemoryError extends Error {}
globalThis.NvdecMemoryError = NvdecMemoryError;
const {LocalNVDEC} = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
delete globalThis.NvdecMemoryError;

const colorSpace = {fullRange: false, matrix: 'bt709', primaries: 'bt709', transfer: 'bt709'};
const signal = () => new AbortController().signal;
const pulse = () => new Promise(resolve => setImmediate(resolve));

function fixture(t, fetch) {
  const previousFrame = globalThis.VideoFrame;
  const created = [];
  globalThis.VideoFrame = class {
    constructor(data, init) { Object.assign(this, {data, init, closed: false}); created.push(this); }
    close() { this.closed = true; }
  };
  globalThis.nvdecFetch = fetch;
  t.after(() => { globalThis.VideoFrame = previousFrame; delete globalThis.nvdecFetch; });
  return created;
}

function packet(pts = 0, width = 3, height = 3) {
  const n = width * height + 2 * Math.ceil(width / 2) * Math.ceil(height / 2);
  const buffer = new ArrayBuffer(16 + n);
  const header = new DataView(buffer);
  header.setBigInt64(0, BigInt(pts), true);
  header.setUint16(8, width, true);
  header.setUint16(10, height, true);
  header.setUint32(12, n, true);
  new Uint8Array(buffer, 16).fill(0xa9);
  return buffer;
}

test('use a separate reader per source and create a transferable I420 frame with the requested color and PTS', async t => {
  const requests = [];
  const pts = 2 ** 32 + 19;
  const buffer = packet(pts);
  const created = fixture(t, async (url, options) => {
    requests.push([new URL(url, 'https://cmp.test'), options]);
    return {arrayBuffer: async () => buffer};
  });
  const reader = new LocalNVDEC('test asset', 'a+b', 'csrf');
  const other = new LocalNVDEC('test asset', undefined, 'csrf');
  assert.equal(await reader.frame(pts, 0, 3, 3, colorSpace, signal()), created[0]);
  await other.frame(pts, null, 3, 3, colorSpace, signal());
  const [first, second] = requests;
  assert.match(first[0].pathname, /^\/api\/assets\/test%20asset\/nvdec\/[\da-f-]+$/);
  assert.notEqual(first[0].pathname, second[0].pathname);
  assert.deepEqual(Object.fromEntries(first[0].searchParams), {share: 'a+b', pts_us: String(pts), previous_us: '0'});
  assert.deepEqual(Object.fromEntries(second[0].searchParams), {pts_us: String(pts)});
  assert.deepEqual(first[1], {method: 'POST', credentials: 'same-origin', headers: {'X-CSRF-Token': 'csrf'}});
  assert.deepEqual(created[0].init, {format: 'I420', codedWidth: 3, codedHeight: 3, timestamp: pts, colorSpace, transfer: [buffer]});
  assert.equal(created[0].data.byteOffset, 16);
  assert.deepEqual(created[0].data, new Uint8Array(17).fill(0xa9));
});

test('request preview widths and validate the rounded height and odd I420 plane sizes', async t => {
  const requests = [];
  const outputs = [packet(0, 5, 3), packet(1, 10, 5), packet(2, 1, 1)];
  const created = fixture(t, async (url) => {
    requests.push(new URL(url, 'https://cmp.test'));
    return {arrayBuffer: async () => outputs.shift()};
  });
  const reader = new LocalNVDEC('asset', undefined, 'csrf');
  await reader.frame(0, null, 10, 5, colorSpace, signal(), 5);
  await reader.frame(1, 0, 10, 5, colorSpace, signal(), 12);
  await reader.frame(2, 1, 10, 1, colorSpace, signal(), 1);
  assert.deepEqual(requests.map(url => url.searchParams.get('preview_width')), ['5','12','1']);
  assert.deepEqual(created.map(frame => [frame.init.codedWidth, frame.init.codedHeight, frame.data.length]),
    [[5,3,27],[10,5,80],[1,1,3]]);
});

test('reject video payloads whose dimensions, timestamp, declared length or actual length differ', async t => {
  const badWidth = packet(), badHeight = packet();
  new DataView(badWidth).setUint16(8, 4, true);
  new DataView(badHeight).setUint16(10, 4, true);
  const buffers = [packet(1), badWidth, badHeight, packet(0).slice(0, -1)];
  const badPayloadLength = packet();
  new DataView(badPayloadLength).setUint32(12, 16, true);
  buffers.push(badPayloadLength);
  const created = fixture(t, async () => ({arrayBuffer: async () => buffers.shift()}));
  const reader = new LocalNVDEC('asset', undefined, 'csrf');
  while (buffers.length) {
    await assert.rejects(reader.frame(0, null, 3, 3, colorSpace, signal()), /Invalid local NVDEC frame/);
  }
  assert.equal(created.length, 0);
});

test('cancel promptly and close the server reader only after a pending frame response is drained', async t => {
  const requests = [];
  const bodyStarted = Promise.withResolvers(), body = Promise.withResolvers(), removed = Promise.withResolvers();
  const created = fixture(t, async (url, options) => {
    requests.push([url, options]);
    if (options.method === 'DELETE') { removed.resolve(); return {}; }
    return {arrayBuffer: () => { bodyStarted.resolve(); return body.promise; }};
  });
  const reader = new LocalNVDEC('asset', 'share', 'csrf');
  const caller = new AbortController();
  const active = reader.frame(0, null, 3, 3, colorSpace, caller.signal);
  const queued = reader.frame(1, null, 3, 3, colorSpace, signal());
  const abortedActive = assert.rejects(active, {name: 'AbortError'});
  const abortedQueued = assert.rejects(queued, {name: 'AbortError'});
  await bodyStarted.promise;
  caller.abort();
  await abortedActive;
  reader.close();
  reader.close();
  await abortedQueued;
  await pulse();
  assert.deepEqual(requests.map(([, options]) => options.method), ['POST']);
  body.resolve(packet());
  await removed.promise;
  assert.equal(created.length, 0);
  assert.deepEqual(requests.map(([, options]) => options.method), ['POST', 'DELETE']);
  assert.equal(requests[1][0], requests[0][0].replace('?pts_us=0&', '?'));
  assert.deepEqual(requests[1][1], {method: 'DELETE', credentials: 'same-origin', headers: {'X-CSRF-Token': 'csrf'}, keepalive: true});
  await assert.rejects(reader.frame(0, null, 3, 3, colorSpace, signal()), {name: 'AbortError'});
});

test('surface memory pressure and fall back without a request when WebCodecs is absent', async t => {
  let requests = 0;
  fixture(t, async () => {
    requests++;
    throw Object.assign(new Error('low memory'), {status: 507});
  });
  const reader = new LocalNVDEC('asset', undefined, 'csrf');
  await assert.rejects(reader.frame(0, null, 3, 3, colorSpace, signal()), NvdecMemoryError);
  globalThis.VideoFrame = undefined;
  await assert.rejects(reader.frame(0, null, 3, 3, colorSpace, signal()), /requires WebCodecs/);
  assert.equal(requests, 1);
});

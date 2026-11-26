import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import ts from 'typescript';
const source=readFileSync(new URL('../src/media-memory.ts',import.meta.url),'utf8')
  .replace("import { api } from './api';",'const api=globalThis.memoryTestApi;')
  .replace("from './i18n.js'",`from '${new URL('../src/i18n.js',import.meta.url).href}'`);
const {outputText}=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}});
let serial=0;
async function fixture(available){
  let reads=0;
  globalThis.memoryTestApi={personalMemory:async()=>{reads++;return {available_bytes:available()};}};
  const module=await import('data:text/javascript;base64,'+Buffer.from(outputText).toString('base64')+'#'+(++serial));
  return {...module,reads:()=>reads};
}
const floor=512*1024**2;
test('allocation can leave exactly 512 MiB but cannot take the byte below it',async()=>{
  const m=await fixture(()=>floor+8);
  await m.checkMediaMemory(8);
  let started=false;
  await assert.rejects(m.withMediaMemory(9,async()=>{started=true}),m.MediaMemoryError);
  assert.equal(started,false);
});
test('large media use available bytes and concurrent work shares the same reading',async()=>{
  const m=await fixture(()=>floor+1_000_000_000);
  let finish,started;
  const marker=new Promise(r=>{started=r});
  const running=m.withMediaMemory(600_000_000,async()=>{started();await new Promise(r=>{finish=r});});
  await marker;
  await assert.rejects(m.withMediaMemory(500_000_000,async()=>{}),m.MediaMemoryError);
  finish();await running;
  await m.withMediaMemory(500_000_000,async()=>{});
  assert.equal(m.reads(),2,'spent estimates must be refreshed before refusing work');
});
test('unmeasurable browser memory adds no fixed cap',async()=>{
  const m=await fixture(()=>null);
  await m.withMediaMemory(16*1024**3,async()=>{});
});

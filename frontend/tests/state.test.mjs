import assert from 'node:assert/strict';
import test from 'node:test';
import {makeViewState, panelPlan, slotAssets, slotVideoAssets, normalizeViewState, viewForShare} from '../src/state.js';

test('a comparison keeps the reference visible and pairs each other image with it', () => {
  const images = ['reference', 'noise', 'blur', 'mosaic', 'faded', 'low-light'];
  const state = {...makeViewState(), layout: '2x3'};
  const slots = slotAssets({images}, state);
  assert.deepEqual(slots, images);
  assert.deepEqual(panelPlan(slots, 'diff', 1), [
    {kind: 'image', left: 'reference', right: null},
    ...images.slice(1).map(left => ({kind: 'diff', left, right: 'reference'})),
  ]);
});

test('video shares preserve large frame indices and offsets without adding video fields to image state', () => {
  const image=normalizeViewState(makeViewState(),2,1);
  assert.equal('video_frame' in image,false);
  const video=normalizeViewState({...image,video_frame:600_000,video_offsets:{a:-500_001,b:500_010,bad:Infinity,fraction:.5,unsafe:Number.MAX_SAFE_INTEGER+1}},2,1);
  assert.equal(video.video_frame,600_000);
  assert.deepEqual(video.video_offsets,{a:-500_001,b:500_010});
  assert.deepEqual(viewForShare(video,2,1),video);
});

test('repeated video keys follow the original occurrence through grouping, sorting and sharing', () => {
  const comparison={images:['a','b','a'],assets:{a:{name:'a.mp4'},b:{name:'b.mp4'}},groups:[
    {label:'second',images:['a','b']},{label:'first',images:['a']},
  ]};
  const before=structuredClone(comparison),state={...makeViewState(),layout:'1x2',video_frame:0};
  const [first,second]=slotVideoAssets(comparison,state);
  assert.deepEqual([first.id,second.id],['a','a']);assert.notEqual(first.key,second.key);
  assert.deepEqual(slotVideoAssets(comparison,{...state,natural_sort:true}),[second,first]);
  assert.deepEqual(slotVideoAssets(comparison,{...state,grouping:'within'}),[first,{id:'b',key:'b'}]);
  const narrow=viewForShare({...state,grouping:'within',layout:'1x1',group_index:2,video_offsets:{a:1,[second.key]:0}},1,3);
  assert.deepEqual(slotVideoAssets(comparison,narrow),[second]);
  assert.deepEqual(narrow.video_offsets,{a:1,[second.key]:0});
  assert.deepEqual(comparison,before);
});

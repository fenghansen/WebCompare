import assert from 'node:assert/strict';
import test from 'node:test';
import {makeViewState, panelPlan, slotAssets} from '../src/state.js';

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

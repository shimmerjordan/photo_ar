import { test } from 'node:test'
import assert from 'node:assert/strict'
import { starHint } from '../public/scannability.js'
test('starHint：低星有可执行提示，高星安静', () => {
  assert.match(starHint(2), /换一张/)
  assert.match(starHint(3), /占满/)
  assert.equal(starHint(4), '')
  assert.equal(starHint(99), '')
  assert.match(starHint(0), /扫不出/)
})

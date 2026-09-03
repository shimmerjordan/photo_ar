/**
 * 跟踪空间尺寸的公式。**worker 与 pipeline 必须用同一个**。
 *
 * worker 按它把小图画成 tw×th，`pipeline._toTrackGray` 按它算 `_trackScale`（种子点要
 * 靠这个比例在查询空间与跟踪空间之间来回换）。两边各写一份的话，差一像素就是四角整体
 * 偏一点 —— 而那既不报错也不掉点，只表现为"贴得不太准"。所以它是一个导出的纯函数，
 * 并且在这里被钉住。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { TRACK_LONG_EDGE, trackDims } from '../public/recognize/pipeline.js'

test('trackDims：长边压到 TRACK_LONG_EDGE，等比，至少 1', () => {
  assert.deepEqual(trackDims(1280, 960), [640, 480])
  assert.deepEqual(trackDims(960, 1280), [480, 640])
  assert.deepEqual(trackDims(640, 480), [640, 480])
  assert.deepEqual(trackDims(320, 240), [320, 240])   // 本来就小：不放大
  assert.equal(Math.max(...trackDims(1280, 1)), TRACK_LONG_EDGE)
})

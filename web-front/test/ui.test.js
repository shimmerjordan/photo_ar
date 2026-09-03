/**
 * `public/ui.js` 里**不碰 DOM**的那两个小函数。
 *
 * 仓库的测试跑在裸 node 上（没有 jsdom，也没有 DOM shim —— `harness.js` 那条路是
 * 真浏览器，用来跑几何与识别的黄金样本）。所以这里只验纯逻辑，而这恰恰是把它们从
 * 组件里抽出来的理由：
 *
 * - `soundLabel`：声音按钮的标签**只能由 `video.muted` 派生**。上一版按钮在 click
 *   里自己改标签，于是重扫之后视频被重新静音，而按钮还写着「静音」—— 用户点它，
 *   声音反而关了（它以为自己在开）。
 * - `barStyle`：不定长时的样子。**不编一个假百分比**（理由与 ui.loading 同一条），
 *   而是铺满并压暗 —— 面板里一条来回扫的金条比它值得的注意力要抢眼得多。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { barStyle, soundLabel } from '../public/ui.js'

test('soundLabel 由 muted 派生', () => { assert.equal(soundLabel(true), '开声音'); assert.equal(soundLabel(false), '静音') })
test('barStyle：数值 → 缩放；null → 铺满压暗', () => {
  assert.deepEqual(barStyle(0.5), { transform: 'scaleX(0.5)', opacity: '1' })
  assert.deepEqual(barStyle(null), { transform: 'scaleX(1)', opacity: '.4' })
  assert.deepEqual(barStyle(7), { transform: 'scaleX(1)', opacity: '1' })
})
test('barStyle：负数与 NaN 不该画出反向或消失的条', () => {
  assert.deepEqual(barStyle(-1), { transform: 'scaleX(0)', opacity: '1' })
  assert.deepEqual(barStyle(Number.NaN), { transform: 'scaleX(1)', opacity: '.4' })
})

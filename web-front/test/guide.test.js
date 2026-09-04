/**
 * `public/guide.js` 的文案分档测试。
 *
 * 验的不是"字对不对"，而是**同一句话有没有在两种完全不同的处境下被复用** ——
 * 「攒到 2/3」要用户别动，「认不出来」要用户换姿势，两者混淆过一次（见 scan.js 的
 * 那段注释）。所以每一档都钉住 `key`：key 相同就意味着给出的下一步动作相同。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { guideTip } from '../public/guide.js'

test('攒证据时说「拿稳别动」并带进度', () => {
  const g = guideTip({ reason: 'weak', inliers: 33, weakRun: 1, idleMs: 0, streak: { n: 2, need: 3 } })
  assert.match(g.text, /拿稳别动/); assert.match(g.text, /2\/3/); assert.equal(g.key, 'streak')
})
test('白墙 → 光线/对准', () => {
  assert.equal(guideTip({ reason: 'no_features', idleMs: 0 }).key, 'no_features')
})
test('内点 15~39 连续 3 帧 → 再靠近', () => {
  assert.equal(guideTip({ reason: 'weak', inliers: 25, weakRun: 2, idleMs: 0 }).key, 'weak')
  const g = guideTip({ reason: 'weak', inliers: 25, weakRun: 3, idleMs: 0 })
  assert.equal(g.key, 'closer'); assert.match(g.text, /靠近/)
})
test('ambiguous → 端稳', () => {
  assert.match(guideTip({ reason: 'ambiguous', idleMs: 0 }).text, /端稳/)
})
test('30 秒没证据 → 可能不在库里', () => {
  const g = guideTip({ reason: 'weak', inliers: 5, weakRun: 0, idleMs: 30_001 })
  assert.equal(g.key, 'not_in_library')
})
test('无 reason → 默认扫描提示', () => {
  assert.equal(guideTip({}).key, 'scanning')
})
/**
 * 空库那一档**不能靠 reason 判**，这一条是给 scan.js 立的规矩（review 抓到的死角）：
 * 库是空的时候，worker 只在"这一帧有纹理但候选集为空"时才报 `empty`；对着白墙报的是
 * `no_features`。所以扫描页里出口按钮的显隐不能只跟 `guideTip` 的档走 —— 它要另记一格
 * `libEmpty`，否则空库时扫一下白墙，屏幕上唯一的出口就没了。
 */
test('空库：有纹理时 reason=empty 走空库档，白墙时仍是 no_features', () => {
  assert.equal(guideTip({ reason: 'empty' }).key, 'empty')
  assert.equal(guideTip({ reason: 'no_features' }).key, 'no_features')
})

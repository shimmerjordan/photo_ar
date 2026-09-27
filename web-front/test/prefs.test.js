// test/prefs.test.js
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
const store = new Map()
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k), clear: () => store.clear(),
}
const P = await import('../public/prefs.js')
beforeEach(() => store.clear())

test('声音缺省是开的', () => { assert.equal(P.soundOn(), true) })
test('声音能关能开', () => { P.setSoundOn(false); assert.equal(P.soundOn(), false); P.setSoundOn(true); assert.equal(P.soundOn(), true) })
test('localStorage 抛异常时照样给默认值', () => {
  const saved = globalThis.localStorage
  globalThis.localStorage = { getItem() { throw new Error('隐私模式') }, setItem() { throw new Error('x') }, removeItem() {} }
  try { assert.equal(P.soundOn(), true); assert.deepEqual(P.advanced(), { ...P.ADV_DEFAULT, overrides: {} }); P.setSoundOn(false) }
  finally { globalThis.localStorage = saved }
})
test('坏 JSON 当默认值', () => { store.set('photoar.adv.v1', '{坏'); assert.equal(P.advanced().mediaBase, '') })
test('normalizeBase 去尾斜杠、保留路径前缀、拒绝非 http', () => {
  assert.equal(P.normalizeBase(' http://192.168.1.10:8964/ '), 'http://192.168.1.10:8964')
  assert.equal(P.normalizeBase('https://nas.lan/photoar/?x=1#y'), 'https://nas.lan/photoar')
  assert.equal(P.normalizeBase(''), '')
  assert.throws(() => P.normalizeBase('ftp://x'), /http:\/\//)
  assert.throws(() => P.normalizeBase('192.168.1.10'), /http:\/\//)
})
test('单个媒体来源的增删', () => {
  P.setOverride('p1', { kind: 'url', url: 'http://x/a.mp4' })
  assert.deepEqual(P.overrideOf('p1'), { kind: 'url', url: 'http://x/a.mp4' })
  P.setOverride('p1', null)
  assert.equal(P.overrideOf('p1'), null)
})
test('变更会通知', () => { let n = 0; const off = P.onPrefsChange(() => n++); P.setSoundOn(false); off(); P.setSoundOn(true); assert.equal(n, 1) })

// ── pickLanMode（fix1 Finding 2）───────────────────────────────────────────
//
// 测试未保存的地址不该覆盖已保存地址的 lanMode。之前的 bug：测 X（没保存）成功后
// `setAdvanced({lanMode})` 直接写，把已保存地址 Y 的 lanMode 污染成 X 的写法；
// 回填 Y 再保存时 `b === a.mediaBase` 成立，保留的却是被污染过的值。
test('pickLanMode：地址清空 → null', () => {
  assert.equal(P.pickLanMode({ toSave: '', savedBase: 'http://y', savedLanMode: 'local', testedBase: 'http://x', testedLanMode: 'direct' }), null)
})
test('pickLanMode：保存的正是刚测过的地址 → 用刚测出来的写法', () => {
  assert.equal(P.pickLanMode({ toSave: 'http://x', savedBase: 'http://y', savedLanMode: 'local', testedBase: 'http://x', testedLanMode: 'direct' }), 'direct')
})
test('pickLanMode：保存的是原来那个地址、没有重新测过 → 保留原来的写法', () => {
  assert.equal(P.pickLanMode({ toSave: 'http://y', savedBase: 'http://y', savedLanMode: 'local', testedBase: null, testedLanMode: null }), 'local')
})
test('pickLanMode：复现 bug —— 测了别的地址之后，回填原地址保存，不该带上别的地址测出来的写法', () => {
  // 已保存 Y（写法 local）；输入框填 X 点了测试（测出 direct，但没保存）；
  // 现在把输入框改回 Y 再点保存：应该还是 local，不是被污染的 direct。
  const r = P.pickLanMode({ toSave: 'http://y', savedBase: 'http://y', savedLanMode: 'local', testedBase: 'http://x', testedLanMode: 'direct' })
  assert.equal(r, 'local')
})
test('pickLanMode：保存一个从没测过、也不是当前已保存的新地址 → null（等着被测出来）', () => {
  assert.equal(P.pickLanMode({ toSave: 'http://z', savedBase: 'http://y', savedLanMode: 'local', testedBase: 'http://x', testedLanMode: 'direct' }), null)
})

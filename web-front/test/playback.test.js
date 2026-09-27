// test/playback.test.js —— 假 video：play() 按剧本 resolve/reject
import { test } from 'node:test'
import assert from 'node:assert/strict'
const store = new Map()
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }
const { startPlayback, armSoundUnlock, soundUnlocker } = await import('../public/playback.js')

function fakeVideo(script) {
  const v = new EventTarget()
  v.muted = false; v.dataset = {}; v.calls = []
  v.play = () => { const r = script.shift(); v.calls.push(v.muted); return r === 'ok' ? Promise.resolve() : Promise.reject(Object.assign(new Error(r), { name: r })) }
  return v
}

test('允许有声就有声播', async () => {
  const v = fakeVideo(['ok'])
  assert.deepEqual(await startPlayback(v), { muted: false, blocked: false })
  assert.deepEqual(v.calls, [false])
})
test('有声被拦 → 退静音再播并打标记', async () => {
  const v = fakeVideo(['NotAllowedError', 'ok'])
  const r = await startPlayback(v)
  assert.equal(r.muted, true); assert.equal(r.blocked, true)
  assert.deepEqual(v.calls, [false, true]); assert.equal(v.dataset.soundBlocked, '1')
})
test('偏好关声音就直接静音播', async () => {
  store.set('photoar.sound', 'off')
  const v = fakeVideo(['ok'])
  assert.deepEqual(await startPlayback(v), { muted: true, blocked: false })
  store.delete('photoar.sound')
})
test('换源打断（AbortError）不算失败', async () => {
  const r = await startPlayback(fakeVideo(['AbortError']))
  assert.equal(r.aborted, true)
})
// ── 第一次手势自动开声 ─────────────────────────────────────────────
// 手势用 `pointerup` 而不是 `pointerdown`：按 HTML 的用户激活规则，触屏上的 pointerdown
// 不是激活事件（要到 pointerup / touchend / click / keydown 才算），在那时开声会无效、
// 甚至让 Chrome 把视频停掉 —— 而手机正是主场景（Task 7 fix1 裁决 A）。
function gesture(type, { onSound = false } = {}) {
  const ev = new Event(type)
  Object.defineProperty(ev, 'target', { value: { closest: (s) => (onSound && s === '[data-role="sound"]' ? {} : null) } })
  return ev
}
function blockedVideo() {
  const v = fakeVideo([]); v.muted = true; v.dataset.soundBlocked = '1'
  return v
}

test('第一次手势（pointerup）自动开声；一次性：第二次不再管', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  armSoundUnlock(v, { root })
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, false); assert.equal(v.dataset.soundBlocked, undefined)
  v.muted = true; v.dataset.soundBlocked = '1'
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, true)
})
test('pointerdown 不触发（触屏上它不是用户激活），也不消耗监听', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  armSoundUnlock(v, { root })
  root.dispatchEvent(gesture('pointerdown'))
  assert.equal(v.muted, true); assert.equal(v.dataset.soundBlocked, '1')
  root.dispatchEvent(gesture('pointerup'))         // 真正的手势还能开声：没被 pointerdown 白白摘掉
  assert.equal(v.muted, false)
})
test('keydown 也算一次手势', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  armSoundUnlock(v, { root })
  root.dispatchEvent(gesture('keydown'))
  assert.equal(v.muted, false)
})
test('点在声音按钮上：不代劳开声，也不消耗监听 —— 之后点别处照样开声', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  armSoundUnlock(v, { root })
  root.dispatchEvent(gesture('pointerup', { onSound: true }))
  assert.equal(v.muted, true)                       // 声音按钮自己会切，这里不插手
  assert.equal(v.dataset.soundBlocked, '1')
  root.dispatchEvent(gesture('pointerup'))          // 提示里说的「点一下页面别处」不是空话
  assert.equal(v.muted, false)
})
test('声音被别处开了（声音按钮 / 原生喇叭）：摘掉监听、清标记，之后用户自己再静音不被改回有声', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  armSoundUnlock(v, { root })
  v.muted = false; v.dispatchEvent(new Event('volumechange'))
  assert.equal(v.dataset.soundBlocked, undefined)
  v.muted = true                                    // 用户自己又按了静音
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, true)
})
test('静音着的 volumechange（起播时退静音那一下迟到的事件）不算开声，监听还在', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  armSoundUnlock(v, { root })
  v.dispatchEvent(new Event('volumechange'))
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, false)
})
test('解除之后不再管', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  const off = armSoundUnlock(v, { root })
  off()
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, true)
})

// ── soundUnlocker：三页（扫描 / 宾客 / 试播）同一套挂法 ─────────────
test('soundUnlocker：只在起播结果是「被拦」时才挂；没被拦 / 还没出来不挂', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  const u = soundUnlocker(v, { root })
  u.update(null)
  u.update({ muted: false, blocked: false })
  root.dispatchEvent(gesture('pointerup'))          // 挂早了的话，取信息 / 下前几块时的这一下就会消耗掉它
  assert.equal(v.muted, true)
  u.update({ muted: true, blocked: true })
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, false)
})
test('soundUnlocker：同一份结果不重挂；新的被拦结果（换源重起播）重挂一次', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  const u = soundUnlocker(v, { root })
  const r1 = { muted: true, blocked: true }
  u.update(r1)
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, false)
  v.muted = true; v.dataset.soundBlocked = '1'
  u.update(r1)                                      // 每条 PLAYING 都带着同一份 autoplay：不能每条都重挂
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, true)
  u.update({ muted: true, blocked: true })
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, false)
})
test('soundUnlocker：reset（换片 / 卸载）解除', () => {
  const root = new EventTarget()
  const v = blockedVideo()
  const u = soundUnlocker(v, { root })
  u.update({ muted: true, blocked: true })
  u.reset()
  root.dispatchEvent(gesture('pointerup'))
  assert.equal(v.muted, true)
})

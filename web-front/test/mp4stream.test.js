/**
 * `mp4stream.playStream`：边下边播时**只起播一次，暂停由用户说了算**（设计 §2.5，Review Focus 1）。
 *
 * 钉的是这一轮用户原话里的那个 bug：原来 for 循环每 append 一块就
 * `if (video.readyState >= 1 && video.paused) video.play()` —— 用户一按暂停，下一块到达
 * 就被播回去，直到整条下完。试播页、宾客页（原生控件）、扫描页全部中招。
 *
 * 不碰浏览器：装假 `MediaSource` / `SourceBuffer` / `URL.createObjectURL` 与一个假 video
 * （EventTarget + 几个字段），喂一个手动推块的 Response。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
const store = new Map()
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }
class FakeSB extends EventTarget {
  appendBuffer() { this.updating = true; setTimeout(() => { this.updating = false; this.dispatchEvent(new Event('updateend')); globalThis.__video.readyState = 2 }, 0) }
}
class FakeMS extends EventTarget {
  static isTypeSupported() { return true }
  constructor() { super(); this.readyState = 'closed'; setTimeout(() => { this.readyState = 'open'; this.dispatchEvent(new Event('sourceopen')) }, 0) }
  addSourceBuffer() { return new FakeSB() }
  endOfStream() { this.readyState = 'ended' }
}
globalThis.MediaSource = FakeMS
URL.createObjectURL = () => 'blob:fake'; URL.revokeObjectURL = () => {}
const { playStream } = await import('../public/mp4stream.js')

test('边下边播时用户暂停，后续数据不会把它播回去', async () => {
  const v = new EventTarget(); globalThis.__video = v
  v.readyState = 0; v.paused = true; v.muted = false; v.dataset = {}; v.plays = 0
  v.play = () => { v.plays++; v.paused = false; return Promise.resolve() }
  v.pause = () => { v.paused = true }
  let push
  const body = new ReadableStream({ start(c) { push = c } })
  const res = new Response(body, { headers: { 'content-length': '3' } })
  const events = []
  const stop = playStream(v, res, { onEvent: (n) => events.push(n) })
  await new Promise((r) => setTimeout(r, 5))
  push.enqueue(new Uint8Array([1])); await new Promise((r) => setTimeout(r, 5))
  assert.equal(v.plays, 1)
  v.pause()                                          // 用户按了暂停
  push.enqueue(new Uint8Array([2])); await new Promise((r) => setTimeout(r, 5))
  push.enqueue(new Uint8Array([3])); push.close(); await new Promise((r) => setTimeout(r, 10))
  assert.equal(v.plays, 1); assert.equal(v.paused, true)
  assert.ok(events.includes('autoplay')); assert.ok(events.includes('done'))
  stop()
})

// ── 以下用例共用的假件 ──────────────────────────────────────────────────
const wait = (ms) => new Promise((r) => setTimeout(r, ms))
/** 假 video。`load()` 照规范把 `paused` 置回 true —— 退回 `<video src>` 那条路的「要不要续播」就靠它。 */
function fakeVideo() {
  const v = new EventTarget(); globalThis.__video = v
  Object.assign(v, { readyState: 0, paused: true, muted: false, dataset: {}, plays: 0, loads: 0, src: '' })
  v.play = () => { v.plays++; v.paused = false; return Promise.resolve() }
  v.pause = () => { v.paused = true }
  v.load = () => { v.loads++; v.paused = true }
  return v
}
/** 手动推块的 Response：`ctl.enqueue / close / error`。 */
function streamed(len) {
  let ctl
  const body = new ReadableStream({ start(c) { ctl = c } })
  return { res: new Response(body, { headers: { 'content-length': String(len) } }), ctl }
}
/** 临时拿掉 MediaSource（模拟 iPhone Safari 那种没有 MSE 的浏览器）。 */
async function withoutMse(fn) {
  const saved = globalThis.MediaSource
  delete globalThis.MediaSource
  try { await fn() } finally { globalThis.MediaSource = saved }
}

test('autoplay: false 时一次都不碰 play()，也不报 autoplay', async () => {
  const v = fakeVideo()
  const { res, ctl } = streamed(2)
  const events = []
  const stop = playStream(v, res, { onEvent: (n) => events.push(n), autoplay: false })
  await wait(5)
  ctl.enqueue(new Uint8Array([1])); await wait(5)
  ctl.enqueue(new Uint8Array([2])); ctl.close(); await wait(10)
  assert.equal(v.plays, 0)
  assert.ok(!events.includes('autoplay')); assert.ok(events.includes('done'))
  stop()
})

test('起播结果原样报出来（页面据此决定「开声音」要不要金框）', async () => {
  const v = fakeVideo()
  // 有声被拦 → 静音重播成功：这正是「被拦时退静音 + 金框」那一档。
  v.play = () => { v.plays++; if (!v.muted) return Promise.reject(Object.assign(new Error('x'), { name: 'NotAllowedError' })); v.paused = false; return Promise.resolve() }
  const { res, ctl } = streamed(1)
  const got = []
  const stop = playStream(v, res, { onEvent: (n, d) => { if (n === 'autoplay') got.push(d) } })
  await wait(5)
  ctl.enqueue(new Uint8Array([1])); ctl.close(); await wait(10)
  assert.deepEqual(got, [{ muted: true, blocked: true }])
  assert.equal(v.dataset.soundBlocked, '1')
  stop()
})

test('没有 MediaSource：退回 <video src> 之后也起播一次（原来这条路上扫描页根本不会自动起播）', async () => {
  await withoutMse(async () => {
    const v = fakeVideo()
    const events = []
    const stop = playStream(v, '/api/stream/t1', { onEvent: (n) => events.push(n) })
    await wait(5)
    assert.equal(v.src, '/api/stream/t1'); assert.equal(v.loads, 1); assert.equal(v.plays, 1)
    assert.deepEqual(events, ['fallback', 'autoplay'])
    stop()
  })
})

test('卸载之后退路地址才回来：不再碰 video（扫描页的 <video> 是长驻的，下一段已经在用它）', async () => {
  await withoutMse(async () => {
    const v = fakeVideo(); v.src = 'blob:下一段'
    let give
    const events = []
    const stop = playStream(v, new Response('x'), {
      onEvent: (n) => events.push(n),
      getFallbackUrl: () => new Promise((r) => { give = r }),
    })
    stop()
    give('/api/stream/迟到的票'); await wait(5)
    assert.equal(v.src, 'blob:下一段'); assert.equal(v.loads, 0); assert.equal(v.plays, 0)
    assert.ok(!events.includes('autoplay'))
  })
})

test('边下边播半路断了退回 <video src>：用户暂停着就不替他播', async () => {
  const v = fakeVideo()
  const { res, ctl } = streamed(3)
  const events = []
  const stop = playStream(v, res, { onEvent: (n) => events.push(n), getFallbackUrl: () => '/api/stream/t2' })
  await wait(5)
  ctl.enqueue(new Uint8Array([1])); await wait(5)
  assert.equal(v.plays, 1)
  v.pause()                                          // 用户按了暂停
  ctl.error(new TypeError('network')); await wait(10)
  assert.ok(events.includes('fallback'))
  assert.equal(v.src, '/api/stream/t2'); assert.equal(v.loads, 1)
  assert.equal(v.plays, 1); assert.equal(v.paused, true)
  // 退回去的那一下不是「下完了」：不能报 done（mediaload 会据此说「正在缓冲首帧」）。
  assert.ok(!events.includes('done'))
  stop()
})

test('边下边播半路断了退回 <video src>：本来在播就接着播（load() 会把它停下）', async () => {
  const v = fakeVideo()
  const { res, ctl } = streamed(3)
  const stop = playStream(v, res, { getFallbackUrl: () => '/api/stream/t3' })
  await wait(5)
  ctl.enqueue(new Uint8Array([1])); await wait(5)
  ctl.error(new TypeError('network')); await wait(10)
  assert.equal(v.src, '/api/stream/t3')
  assert.equal(v.plays, 2); assert.equal(v.paused, false)
  stop()
})

// ── fix1 #1：换票期间用户按的暂停，不能被地址回来那一刻播回去 ─────────────────
test('半路退回换票期间用户按了暂停：地址回来后不应该把它播回去（评审 fix1 #1）', async () => {
  // 原来的 bug：`resume` 只在 `fallback()` 那一刻量一次，之后 `getFallbackUrl` 慢慢
  // 才回来（这里模拟成一个手动 resolve 的 Promise）——这段等待期间用户按的暂停，
  // `resume` 那份旧快照根本不知道，地址一回来照样 `kick()`。评审复现结果 `plays 2 paused false`。
  const v = fakeVideo()
  const { res, ctl } = streamed(3)
  let resolveUrl
  const events = []
  const stop = playStream(v, res, {
    onEvent: (n) => events.push(n),
    getFallbackUrl: () => new Promise((r) => { resolveUrl = r }),
  })
  await wait(5)
  ctl.enqueue(new Uint8Array([1])); await wait(5)
  assert.equal(v.plays, 1)
  ctl.error(new TypeError('network'))                // 触发 fallback，getFallbackUrl 还没返回
  await wait(5)
  assert.ok(events.includes('fallback'))
  v.pause()                                          // 用户在「换票」这段等待期间按了暂停
  resolveUrl('/api/stream/迟到的票')
  await wait(10)
  assert.equal(v.src, '/api/stream/迟到的票')          // 地址正常换过去
  assert.equal(v.plays, 1); assert.equal(v.paused, true)  // 但不该把它播回去
  stop()
})

// ── final-fix M2：页面隐藏时不起播（默认有声，锁屏揣兜里会出声）──────────────────
/** 临时装一个假 `document`（EventTarget + `hidden`）。Node 里本来没有 document = 视为可见。 */
async function withHiddenDoc(fn) {
  const doc = new EventTarget()
  doc.hidden = true
  globalThis.document = doc
  try { await fn(doc) } finally { delete globalThis.document }
}

test('扫到之后、第一块到达之前锁了屏：数据到了也不起播，回到前台才起播（且只起一次）', async () => {
  await withHiddenDoc(async (doc) => {
    const v = fakeVideo()
    const { res, ctl } = streamed(2)
    const events = []
    const stop = playStream(v, res, { onEvent: (n) => events.push(n) })
    await wait(5)
    ctl.enqueue(new Uint8Array([1])); await wait(5)
    ctl.enqueue(new Uint8Array([2])); ctl.close(); await wait(10)
    assert.equal(v.plays, 0)                         // 隐藏着：一次都没碰 play()
    assert.ok(!events.includes('autoplay'))
    assert.ok(events.includes('done'))               // 数据照常喂完
    doc.dispatchEvent(new Event('visibilitychange')) // 仍是 hidden 的一次切换：不算
    await wait(5)
    assert.equal(v.plays, 0)
    doc.hidden = false
    doc.dispatchEvent(new Event('visibilitychange'))
    await wait(5)
    assert.equal(v.plays, 1); assert.equal(v.paused, false)
    assert.ok(events.includes('autoplay'))
    doc.hidden = true; doc.dispatchEvent(new Event('visibilitychange'))
    doc.hidden = false; doc.dispatchEvent(new Event('visibilitychange'))
    await wait(5)
    assert.equal(v.plays, 1)                         // 已经起过播：再切前后台不再碰 play()
    stop()
  })
})

test('隐藏期间等着起播时卸载了：回到前台也不碰 video（扫描页的 <video> 可能已经在放下一段）', async () => {
  await withHiddenDoc(async (doc) => {
    const v = fakeVideo()
    const { res, ctl } = streamed(1)
    const stop = playStream(v, res)
    await wait(5)
    ctl.enqueue(new Uint8Array([1])); ctl.close(); await wait(10)
    assert.equal(v.plays, 0)
    stop()
    doc.hidden = false
    doc.dispatchEvent(new Event('visibilitychange'))
    await wait(5)
    assert.equal(v.plays, 0)
  })
})

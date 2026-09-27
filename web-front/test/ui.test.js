/**
 * `public/ui.js` 的纯逻辑，外加 `playerControls` 的标签派生。
 *
 * 仓库的测试跑在裸 node 上（没有 jsdom —— `harness.js` 那条路是真浏览器，用来跑几何与
 * 识别的黄金样本）。所以纯逻辑直接验，而 `playerControls` 用下面那个**最小 DOM 假件**：
 * 只够 `ui.h()` 建元素、挂监听、塞子节点，不解析 HTML。这也是 `playerControls` 的按钮
 * 用 `h()` 拼 `<span>` 而不走 `button()`（那个用 innerHTML）的一个原因 —— 另一个原因是
 * 标签要被反复改，手上直接拿着那个 span 比每次 `querySelector` 干净。
 *
 * - `soundLabel`：声音按钮的标签**只能由 `video.muted` 派生**。上一版按钮在 click
 *   里自己改标签，于是重扫之后视频被重新静音，而按钮还写着「静音」—— 用户点它，
 *   声音反而关了（它以为自己在开）。
 * - `playLabel`：播放/暂停同理，只能由 `video.paused` 派生。暂停能被四条与这个按钮无关的
 *   路改掉（原生控件、切后台、播完循环、换片），按钮自己记一份必然会对不上。
 * - `barStyle`：不定长时的样子。**不编一个假百分比**（理由与 ui.loading 同一条），
 *   而是铺满并压暗 —— 面板里一条来回扫的金条比它值得的注意力要抢眼得多。
 * - `setDownloadRow`：宾客页 / 试播页播放器下面那组「下载行 + 下载条」什么时候露出来。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { barStyle, playLabel, playerControls, setDownloadRow, soundLabel } from '../public/ui.js'

test('soundLabel 由 muted 派生', () => { assert.equal(soundLabel(true), '开声音'); assert.equal(soundLabel(false), '静音') })
test('playLabel 由 paused 派生', () => { assert.equal(playLabel(true), '播放'); assert.equal(playLabel(false), '暂停') })
test('barStyle：数值 → 缩放；null → 铺满压暗', () => {
  assert.deepEqual(barStyle(0.5), { transform: 'scaleX(0.5)', opacity: '1' })
  assert.deepEqual(barStyle(null), { transform: 'scaleX(1)', opacity: '.4' })
  assert.deepEqual(barStyle(7), { transform: 'scaleX(1)', opacity: '1' })
})
test('barStyle：负数与 NaN 不该画出反向或消失的条', () => {
  assert.deepEqual(barStyle(-1), { transform: 'scaleX(0)', opacity: '1' })
  assert.deepEqual(barStyle(Number.NaN), { transform: 'scaleX(1)', opacity: '.4' })
})

// ── playerControls：最小 DOM 假件 ─────────────────────────────────────
// `h()` 用到的全部：createElement / createTextNode、className、textContent、setAttribute、
// dataset、addEventListener、append，以及 `kid instanceof Node` 那一句要的全局 `Node`。
class FakeNode extends EventTarget {}
class FakeEl extends FakeNode {
  constructor(tag) {
    super()
    this.tagName = tag.toUpperCase()
    this.children = []
    this.attrs = {}
    this.dataset = {}
    this.style = {}
    this.className = ''
    this.textContent = ''
    this.hidden = false
  }
  setAttribute(k, v) { this.attrs[k] = String(v) }
  getAttribute(k) { return this.attrs[k] ?? null }
  append(...kids) { this.children.push(...kids) }
  get firstElementChild() { return this.children.find((c) => c instanceof FakeEl) ?? null }
  click() { this.dispatchEvent(new Event('click')) }
}

/** 装上假 DOM 跑 `fn`，跑完拆掉 —— 别的断言不该看见一个假的 `document`。 */
function withDom(fn) {
  globalThis.Node = FakeNode
  globalThis.document = { createElement: (t) => new FakeEl(t), createTextNode: (s) => ({ text: String(s) }) }
  try { return fn() } finally { delete globalThis.Node; delete globalThis.document }
}

/** 假 video：`paused` / `muted` 两个字段 + 会派发事件的 play()/pause()，记下 play 被调了几次。 */
function fakeVideo({ paused = true, muted = false } = {}) {
  const v = new EventTarget()
  Object.assign(v, { paused, muted, plays: 0 })
  v.play = () => {
    v.plays++
    if (v.paused) { v.paused = false; v.dispatchEvent(new Event('play')) }
    return Promise.resolve()
  }
  v.pause = () => {
    if (!v.paused) { v.paused = true; v.dispatchEvent(new Event('pause')) }
  }
  return v
}

const label = (btn) => btn.children[0].textContent

test('playerControls：.play 的标签跟着 play / pause 事件走', () => withDom(() => {
  const v = fakeVideo({ paused: false })
  const ctl = playerControls(v)
  assert.equal(label(ctl.play), '暂停')
  v.pause()
  assert.equal(label(ctl.play), '播放')
  v.play()
  assert.equal(label(ctl.play), '暂停')
  // 换源时（重扫、换照片）元素被 load() 重置成 paused，而那时不会有 `pause` 事件。
  v.paused = true
  v.dispatchEvent(new Event('emptied'))
  assert.equal(label(ctl.play), '播放')
}))

test('playerControls：.play 点一下在播放 / 暂停之间切', () => withDom(() => {
  const v = fakeVideo({ paused: true })
  const ctl = playerControls(v)
  ctl.play.click()
  assert.equal(v.paused, false)
  assert.equal(label(ctl.play), '暂停')
  ctl.play.click()
  assert.equal(v.paused, true)
  assert.equal(label(ctl.play), '播放')
}))

test('playerControls：.sound 带 data-role="sound"（playback.armSoundUnlock 靠它认出"点在声音按钮上"）', () => withDom(() => {
  const ctl = playerControls(fakeVideo())
  assert.equal(ctl.sound.getAttribute('data-role'), 'sound')
}))

test('playerControls：暂停着开声音只开声，不把视频播起来', () => withDom(() => {
  const v = fakeVideo({ paused: true, muted: true })
  const ctl = playerControls(v)
  ctl.sound.click()
  assert.equal(v.muted, false)
  assert.equal(v.paused, true)
  assert.equal(v.plays, 0)
}))

test('playerControls：在播着开声音会补一次 play()（有的浏览器开声时会把视频停下）', () => withDom(() => {
  const v = fakeVideo({ paused: false, muted: true })
  const ctl = playerControls(v)
  ctl.sound.click()
  assert.equal(v.muted, false)
  assert.equal(v.plays, 1)
}))

test('playerControls：dispose 之后事件不再改标签', () => withDom(() => {
  const v = fakeVideo({ paused: false })
  const ctl = playerControls(v)
  ctl.dispose()
  v.pause()
  assert.equal(label(ctl.play), '暂停')
}))

// ── setDownloadRow：宾客页 / 试播页播放器下面那一组 ─────────────────────
const dlRow = () => {
  const line = new FakeEl('p')
  const bar = new FakeEl('div')
  bar.append(new FakeEl('i'))
  return { line, bar }
}

test('setDownloadRow：还在下 → 行和条都露出来，条按 loaded/total', () => withDom(() => {
  const { line, bar } = dlRow()
  setDownloadRow(line, bar, { loaded: 25, total: 100, done: false }, '已下载 …')
  assert.equal(line.hidden, false)
  assert.equal(line.textContent, '已下载 …')
  assert.equal(bar.hidden, false)
  assert.equal(bar.firstElementChild.style.transform, 'scaleX(0.25)')
}))

test('setDownloadRow：不知道总长 → 条走不定长那一档，不编比例', () => withDom(() => {
  const { line, bar } = dlRow()
  setDownloadRow(line, bar, { loaded: 25, total: 0, done: false }, '已下载 …')
  assert.deepEqual(
    { t: bar.firstElementChild.style.transform, o: bar.firstElementChild.style.opacity },
    { t: 'scaleX(1)', o: '.4' })
}))

test('setDownloadRow：下完 → 只留那句话，条收起来；没有 dl → 两样都收起来', () => withDom(() => {
  const { line, bar } = dlRow()
  setDownloadRow(line, bar, { loaded: 100, total: 100, done: true }, '已存到本机，下次秒开')
  assert.equal(line.hidden, false)
  assert.equal(bar.hidden, true)
  setDownloadRow(line, bar, null, '')
  assert.equal(line.hidden, true)
  assert.equal(bar.hidden, true)
}))

/**
 * `mediaload.js` 的媒体元信息缓存（`mediaInfo` / `forgetMedia`）。
 *
 * `api.mediaOfPhoto` 在扫描页、试播页、预取、下载四处各自被调一遍 —— 同一张照片在
 * 一次会话里的元信息不会变，四次网络往返换来的是同一份 JSON。这里钉的是**缓存语义**：
 * 同一个 id 只发一次请求，`forgetMedia` 之后必须真的失效（不然换视频之后旧元信息会
 * 一直粘着，播放的是换掉之前那一份地址）。
 *
 * 这个模块不引入任何 mock 框架 —— `mediaInfo` 接受一个可选 `fetcher` 参数，
 * 默认才是 `api.mediaOfPhoto`，测试直接传一个计数用的假函数进去。
 *
 * 后半截钉的是本轮（2026-09-26）用户原话里的两条：
 * - **边下边播时没有下载进度**：原来 `if (playing) return` —— 一起播就不再报下载进度。
 *   现在起播之后每次下载进度变化都推一次 `PLAYING`（带 `dl`），下完再推一次 `dl.done`；
 * - 顺序「单个来源 → 本机缓存 → 进行中的任务 → 新任务」：看过的第二次零网络、
 *   预取正在下的那段不下第二遍、本机文件来源没了要说清楚去哪重选。
 * 那几条经 `mediastore._setEnv` 注入假 fetch / 假 caches，再装假 MediaSource 与假 video。
 */
import { test, describe, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { Stage, dlText, forgetMedia, loadPhotoVideo, mediaInfo, stageName } from '../public/mediaload.js'
import * as M from '../public/mediastore.js'

describe('mediaInfo：会话内缓存', () => {
  test('同一个 photoId 连调两次，只发一次请求', async () => {
    let calls = 0
    const fetcher = async (id) => { calls++; return { url: `/v1/asset/${id}/stream` } }
    const a = await mediaInfo('p1', fetcher)
    const b = await mediaInfo('p1', fetcher)
    assert.equal(calls, 1)
    assert.equal(a, b) // 同一个对象，不是同值的两份
  })

  test('forgetMedia(id) 之后再调，发第二次请求', async () => {
    let calls = 0
    const fetcher = async (id) => { calls++; return { url: `/v1/asset/${id}/stream/${calls}` } }
    await mediaInfo('p2', fetcher)
    forgetMedia('p2')
    const info = await mediaInfo('p2', fetcher)
    assert.equal(calls, 2)
    assert.equal(info.url, '/v1/asset/p2/stream/2')
  })

  test('不同 photoId 各自缓存，互不影响', async () => {
    let calls = 0
    const fetcher = async (id) => { calls++; return { id } }
    await mediaInfo('a', fetcher)
    await mediaInfo('b', fetcher)
    await mediaInfo('a', fetcher)
    assert.equal(calls, 2)
  })

  test('forgetMedia() 不带参数清空全部', async () => {
    let calls = 0
    const fetcher = async (id) => { calls++; return { id } }
    await mediaInfo('x', fetcher)
    await mediaInfo('y', fetcher)
    forgetMedia()
    await mediaInfo('x', fetcher)
    await mediaInfo('y', fetcher)
    assert.equal(calls, 4)
  })
})

describe('loadPhotoVideo：真正走链路，不只是单测 mediaInfo', () => {
  /**
   * 回归：`loadPhotoVideo` 函数体内原来有个同名局部变量 `let mediaInfo = null`
   * （挂媒体元信息用的），Task 6 把 `api.mediaOfPhoto(photoId)` 改成
   * `mediaInfo(photoId)` 时，这一句解析到的是**那个局部变量**而不是模块级导出的
   * `mediaInfo` 函数 —— 局部变量此刻还是 `null`，于是每次都抛
   * `TypeError: mediaInfo is not a function`，被 try/catch 吞成 `Stage.ERROR`
   * 「取视频信息失败」，三个页面（扫描/试播/宾客）的视频全加不出来。
   *
   * 只单测 `mediaInfo` 这个函数本身测不出这个问题——那个函数是好的，坏的是
   * `loadPhotoVideo` 内部对它的引用被局部变量遮蔽。所以这里必须真的调
   * `loadPhotoVideo`。用最小的假 `video`（只要有 `add/removeEventListener`）
   * 和一个返回 `{missing: true}` 的假 `fetcher`：这条分支在 `cachedStream`
   * 之前就 return，不会碰到 Node 里不存在的 Cache API。
   */
  test('info.missing 时报 UNAVAILABLE 而不是 ERROR', async () => {
    const fakeVideo = { addEventListener() {}, removeEventListener() {} }
    const stages = []
    const fetcher = async () => ({ missing: true })

    // ⚠️ `stop` 不能在 `onStage` 回调里同步调用：`Stage.INFO`（以及这里测的
    // `Stage.ERROR`/`UNAVAILABLE`）在 `loadPhotoVideo` **返回之前**就可能同步触发
    // （没有真正的网络延迟时，`await` 之前的同步部分与 `catch` 分支都在同一个
    // 调用栈里跑完）。那时 `const stop = loadPhotoVideo(...)` 这一行还没执行完，
    // 在回调里调用它会撞 TDZ（`ReferenceError: Cannot access 'stop' before
    // initialization`），这个错误又被外层 `.catch` 接住变成**第二条** `Stage.ERROR`
    // ——踩过一次，与这里要测的那个 bug 无关，纯粹是测试自己的时序问题。
    // 所以清理挪到 `await` 之后再做。
    let stop
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('5 秒内没有等到终局阶段')), 5000)
      stop = loadPhotoVideo(fakeVideo, 'video-info-test-missing', {
        fetcher,
        onStage: (s) => {
          stages.push(s.stage)
          if (s.stage === Stage.UNAVAILABLE || s.stage === Stage.ERROR) {
            clearTimeout(timer)
            resolve()
          }
        },
      })
    })
    stop?.()

    assert.deepEqual(stages, [Stage.INFO, Stage.UNAVAILABLE],
      `期望 INFO → UNAVAILABLE，实际收到 ${stages.join(' → ')}` +
      '（收到 ERROR 就是那个变量遮蔽的 bug 复现了）')
  })
})

describe('dlText / stageName：下载那一行说什么', () => {
  test('dlText 各档', () => {
    assert.equal(dlText(null), '')
    assert.equal(dlText({ fromCache: true, done: true }), '本机已有')
    assert.equal(dlText({ loaded: 3355443, total: 8493465, done: false, source: 'origin' }), '已下载 3.2 / 8.1 MB')
    assert.equal(dlText({ loaded: 3355443, total: 0, done: false, source: 'lan' }), '经局域网 · 已下载 3.2 MB')
    assert.equal(dlText({ loaded: 1, total: 1, done: true, source: 'origin' }), '已存到本机，下次秒开')
    assert.match(dlText({ loaded: 1, total: 1, done: true, cacheFailed: '浏览器存储空间不够' }), /没存进本机/)
  })

  test('经局域网下载有自己的阶段名', () => {
    assert.equal(stageName(Stage.DOWNLOAD, { via: 'lan' }), '正在经局域网下载视频')
    assert.equal(stageName(Stage.DOWNLOAD, { fromCache: true }), '正在从本机取视频')
  })
})

// ── loadPhotoVideo × mediastore 的假件 ──────────────────────────────────
const store = new Map()
const ADV_KEY = 'photoar.adv.v1'
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }
class FakeSB extends EventTarget {
  appendBuffer(chunk) {
    this.updating = true
    globalThis.__video.appended.push(...chunk)
    setTimeout(() => { this.updating = false; this.dispatchEvent(new Event('updateend')); globalThis.__video.readyState = 2 }, 0)
  }
}
class FakeMS extends EventTarget {
  static isTypeSupported() { return true }
  constructor() { super(); this.readyState = 'closed'; setTimeout(() => { this.readyState = 'open'; this.dispatchEvent(new Event('sourceopen')) }, 0) }
  addSourceBuffer() { return new FakeSB() }
  endOfStream() { this.readyState = 'ended' }
}
globalThis.MediaSource = FakeMS
URL.createObjectURL = () => 'blob:fake'; URL.revokeObjectURL = () => {}

/**
 * 假 video。`play()` **同步**派发 `playing` —— 比真浏览器还早（真的是排一个任务），
 * 正好是「`playing` 事件先于 play() 的 promise」那条时序的最坏情形。
 */
function fakeVideo() {
  const v = new EventTarget(); globalThis.__video = v
  Object.assign(v, { readyState: 0, paused: true, muted: false, dataset: {}, plays: 0, loads: 0, src: '', appended: [], duration: NaN, currentTime: 0 })
  v.play = () => { v.plays++; v.paused = false; v.dispatchEvent(new Event('playing')); return Promise.resolve() }
  v.pause = () => { v.paused = true }
  v.load = () => { v.loads++; v.paused = true }
  return v
}
function fakeCaches() {
  const all = new Map()
  const open = async (name) => {
    if (!all.has(name)) all.set(name, new Map())
    const m = all.get(name)
    return {
      match: async (k) => (m.has(k) ? m.get(k).clone() : undefined),
      put: async (k, r) => { m.set(k, new Response(await r.arrayBuffer(), { headers: r.headers })) },
      delete: async (k) => m.delete(k),
      keys: async () => [...m.keys()].map((k) => new Request(`http://x${k}`)),
    }
  }
  return { open, delete: async (n) => all.delete(n), keys: async () => [...all.keys()] }
}
/** 可控的分块响应：`gate()` 放行下一块（计数信号量，与 mediastore.test.js 同一个写法）。 */
function chunked(parts) {
  let i = 0; let credits = 0; const waiters = []
  const gate = () => { const w = waiters.shift(); if (w) w(); else credits++ }
  const body = new ReadableStream({
    async pull(c) {
      if (i >= parts.length) return c.close()
      if (credits > 0) credits--
      else await new Promise((r) => waiters.push(r))
      c.enqueue(parts[i++])
    },
  })
  const len = parts.reduce((n, p) => n + p.byteLength, 0)
  return { res: new Response(body, { headers: { 'content-length': String(len), 'content-type': 'video/mp4' } }), gate }
}
const bytes = (...xs) => new Uint8Array(xs)
const media = (id, n = 3) => ({
  assetId: id, url: `/v1/asset/${id}/stream`, via: 'nas_serve', absolute: false,
  supportsRange: true, bytes: n, durationMs: 1000, missing: false, integrity: 'ok',
})
async function until(cond, ms = 3000) {
  const t0 = Date.now()
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error('等不到条件成立')
    await new Promise((r) => setTimeout(r, 2))
  }
}

describe('loadPhotoVideo × mediastore：一段视频一个任务，起播之后进度照报', () => {
  let calls, next
  beforeEach(() => {
    store.clear()
    calls = 0
    M._setEnv({ cachesImpl: fakeCaches(), fetchImpl: async () => { calls++; return next() } })
  })

  test('起播之后下载进度照样推（PLAYING 带 dl），下完再推一次 dl.done；只起播一次', async () => {
    const parts = chunked([bytes(1), bytes(2), bytes(3)])
    next = () => parts.res
    const v = fakeVideo()
    const events = []
    const stop = loadPhotoVideo(v, 'p-ml-a', { fetcher: async () => media('ml-a'), onStage: (s) => events.push(s) })
    await until(() => calls === 1)
    parts.gate()
    await until(() => events.some((e) => e.stage === Stage.PLAYING))
    parts.gate(); await until(() => events.at(-1).dl?.loaded === 2)
    parts.gate()
    await until(() => events.at(-1).dl?.done === true)
    stop()

    const first = events.findIndex((e) => e.stage === Stage.PLAYING)
    const before = events.slice(0, first)
    const after = events.slice(first)
    assert.ok(before.some((e) => e.stage === Stage.DOWNLOAD && e.dl?.done === false && e.dl.source === 'origin'))
    // 第一条 PLAYING 就带着起播结果：`playing` 事件比 play() 的 promise 早一拍，不压这一拍的话
    // 页面在「首次到 PLAYING」那一刻拿到的 autoplay 是 null，被拦退静音时「开声音」就不会金框。
    assert.deepEqual(after[0].autoplay, { muted: false, blocked: false })
    // 旧行为（`if (playing) return`）起播后就不再报下载进度了 —— 那正是用户说的「边下边播时没有进度」。
    assert.ok(after.every((e) => e.stage === Stage.PLAYING), `起播之后不该再有别的阶段：${after.map((e) => e.stage).join(',')}`)
    assert.ok(after.some((e) => e.dl.loaded === 2 && !e.dl.done))
    assert.deepEqual(events.at(-1).dl, { loaded: 3, total: 3, done: true, fromCache: false, source: 'origin', cacheFailed: null })
    assert.equal(dlText(events.at(-1).dl), '已存到本机，下次秒开')
    assert.equal(v.plays, 1)
    assert.deepEqual(v.appended, [1, 2, 3])
    assert.equal(calls, 1)
  })

  test('看过的那段第二次直接从本机播：不走网络，dl 说本机已有', async () => {
    const parts = chunked([bytes(4, 5)])
    next = () => parts.res
    let events = []
    let stop = loadPhotoVideo(fakeVideo(), 'p-ml-b', { fetcher: async () => media('ml-b', 2), onStage: (s) => events.push(s) })
    await until(() => calls === 1)
    parts.gate()
    await until(() => events.at(-1)?.dl?.done === true)
    stop()

    const v = fakeVideo()
    events = []
    stop = loadPhotoVideo(v, 'p-ml-b', { fetcher: async () => media('ml-b', 2), onStage: (s) => events.push(s) })
    await until(() => events.some((e) => e.stage === Stage.PLAYING))
    stop()
    assert.equal(calls, 1)
    const d = events.find((e) => e.stage === Stage.DOWNLOAD)
    assert.equal(d.fromCache, true)
    assert.match(d.text, /本机已有/)
    assert.deepEqual(d.dl, { loaded: 2, total: 2, done: true, fromCache: true, source: 'cache', cacheFailed: null })
    assert.deepEqual(v.appended, [4, 5])
  })

  test('预取正在下这一段：播放挂到同一个任务上，只下一次，已收的那段先重放', async () => {
    const parts = chunked([bytes(6), bytes(7)])
    next = () => parts.res
    const key = '/v1/asset/ml-c/stream'
    const bg = M.download(key, { priority: M.Priority.BACKGROUND })
    await until(() => calls === 1)
    parts.gate()
    await until(() => bg.loaded === 1)

    const v = fakeVideo()
    const events = []
    const stop = loadPhotoVideo(v, 'p-ml-c', { fetcher: async () => media('ml-c', 2), onStage: (s) => events.push(s) })
    await until(() => events.some((e) => e.stage === Stage.DOWNLOAD))
    assert.equal(M.jobFor(key), bg)
    assert.equal(bg.priority, M.Priority.PLAY, '被播放要到了要就地提升')
    assert.equal(events.find((e) => e.stage === Stage.DOWNLOAD).dl.loaded, 1)
    parts.gate()
    await until(() => events.at(-1)?.dl?.done === true)
    stop()
    assert.equal(calls, 1)
    assert.deepEqual(v.appended, [6, 7])
  })

  test('上一段视频还在播时的 timeupdate / playing 不算这一段起播了（扫描页的 <video> 是长驻的）', async () => {
    const parts = chunked([bytes(1)])
    next = () => parts.res
    const v = fakeVideo()
    v.duration = 10; v.currentTime = 5; v.paused = false       // 上一张照片那段还在放
    let release
    const events = []
    const stop = loadPhotoVideo(v, 'p-ml-d', {
      fetcher: () => new Promise((r) => { release = () => r(media('ml-d', 1)) }),
      onStage: (s) => events.push(s),
    })
    // 取元信息那几百毫秒里，旧的那段照常报它的播放进度。
    v.dispatchEvent(new Event('timeupdate')); v.dispatchEvent(new Event('playing'))
    release()
    await until(() => events.some((e) => e.stage === Stage.DOWNLOAD))
    parts.gate()
    await until(() => events.at(-1)?.dl?.done === true)
    stop()
    const first = events.findIndex((e) => e.stage === Stage.PLAYING)
    const firstDl = events.findIndex((e) => e.stage === Stage.DOWNLOAD)
    assert.ok(firstDl >= 0 && (first < 0 || first > firstDl),
      `新的这一段先下载后播放，实际：${events.map((e) => e.stage).join(' → ')}`)
  })

  test('单个来源是本机文件但存储里没了：说清楚去哪重新选', async () => {
    store.set(ADV_KEY, JSON.stringify({ overrides: { 'p-ov-a': { kind: 'file', name: 'a.mp4', size: 2, type: 'video/mp4', at: 1 } } }))
    const events = []
    const stop = loadPhotoVideo(fakeVideo(), 'p-ov-a', { fetcher: async () => media('ov-a', 2), onStage: (s) => events.push(s) })
    await until(() => events.some((e) => e.stage === Stage.UNAVAILABLE))
    stop()
    assert.equal(events.at(-1).text, '你设的本机文件不在浏览器存储里了，去设置 → 高级设置重新选一次')
    assert.equal(calls, 0)
  })

  test('单个来源是本机文件：从 photoar-override-v1 播，不走网络（服务端元信息取不到也照播）', async () => {
    store.set(ADV_KEY, JSON.stringify({ overrides: { 'p-ov-b': { kind: 'file', name: 'b.mp4', size: 2, type: 'video/mp4', at: 1 } } }))
    await M.putBlob(M.overrideKey('p-ov-b'), new Blob([bytes(8, 9)], { type: 'video/mp4' }), M.OVERRIDE_CACHE)
    const v = fakeVideo()
    const events = []
    const stop = loadPhotoVideo(v, 'p-ov-b', {
      fetcher: async () => { throw new Error('离线') },
      onStage: (s) => events.push(s),
    })
    await until(() => events.some((e) => e.stage === Stage.PLAYING))
    stop()
    assert.equal(calls, 0)
    assert.ok(!events.some((e) => e.stage === Stage.ERROR))
    assert.deepEqual(events.find((e) => e.stage === Stage.DOWNLOAD).dl,
      { loaded: 2, total: 2, done: true, fromCache: true, source: 'file', cacheFailed: null })
    assert.deepEqual(v.appended, [8, 9])
  })

  // Task 7 截图时发现：服务端出的是分片 MP4（empty_moov），边下边喂 MSE 时 `video.duration`
  // 在 endOfStream 之前一直是 Infinity —— 播放进度于是整段下载期间都是 0，扫描页金条的亮层
  // （播放）不动、暗层（已下载）在涨，看起来像没在播。元信息里有时长，拿它当分母。
  test('边下边播时 video.duration 还是 Infinity：播放进度改用元信息的时长', async () => {
    const parts = chunked([bytes(1), bytes(2), bytes(3)])
    next = () => parts.res
    const v = fakeVideo()
    v.duration = Infinity
    const events = []
    const stop = loadPhotoVideo(v, 'p-ml-dur', { fetcher: async () => media('ml-dur'), onStage: (s) => events.push(s) })
    await until(() => calls === 1)
    parts.gate()
    await until(() => events.some((e) => e.stage === Stage.PLAYING))
    v.currentTime = 0.25                        // media() 给的 durationMs 是 1000
    v.dispatchEvent(new Event('timeupdate'))
    await until(() => events.at(-1).stage === Stage.PLAYING && events.at(-1).pct === 0.25)
    parts.gate(); parts.gate()
    await until(() => events.at(-1).dl?.done === true)
    stop()
    assert.equal(events.at(-1).pct, 0.25, '下载进度推过来的 PLAYING 也要带着真的播放进度')
  })

  // fix1 #3：任务被取消（不是真的下不下来）不该报成 CORS 提示——文不对题，
  // 用户什么都没做错，只是这段的来源刚被移除 / 缓存被清空了。
  test('单个来源是直链，播放中途任务被取消：不报 CORS，说来源被移除/缓存清空（评审 fix1 #3）', async () => {
    const url = 'https://cdn.example.com/p-ov-c.mp4'
    store.set(ADV_KEY, JSON.stringify({ overrides: { 'p-ov-c': { kind: 'url', url } } }))
    const parts = chunked([bytes(1), bytes(2)])
    next = () => parts.res
    const v = fakeVideo()
    const events = []
    const stop = loadPhotoVideo(v, 'p-ov-c', {
      fetcher: async () => { throw new Error('不该发这个请求') },
      onStage: (s) => events.push(s),
    })
    await until(() => calls === 1)
    parts.gate()
    await until(() => events.some((e) => e.stage === Stage.DOWNLOAD))
    M.jobFor(url).abort()                              // 模拟 Task 8「移除这条来源 / 清空缓存」
    await until(() => events.some((e) => e.stage === Stage.ERROR))
    stop()
    const err = events.find((e) => e.stage === Stage.ERROR)
    assert.doesNotMatch(err.text, /跨域|CORS/, `文不对题：${err.text}`)
    assert.match(err.text, /移除|清空/)
  })
})

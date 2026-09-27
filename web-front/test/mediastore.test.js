/**
 * `mediastore.js`：一段视频一个下载任务。
 *
 * 这里钉的是整轮「缓存没生效」的三条根因在新结构下各自的反面（设计 §1、§3.1）：
 * - **去重**：预取、播放、手动缓存同时要同一段 → 只下一次，半路才来的读者先拿到已收那段的重放；
 * - **续传换源**：局域网数据源半路断了 → 熔断、换默认源、带 Range 从断点续，不从头下；
 * - **固定按用户隔离**：换人登录后看不到上一个人的固定记录。
 *
 * 不打网络：`_setEnv` 注入假 fetch（可控的分块流）与假 caches（Map 实现）。
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
const store = new Map()
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }
const M = await import('../public/mediastore.js')
const N = await import('../public/netsrc.js')

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
  return { open, delete: async (n) => all.delete(n), keys: async () => [...all.keys()], _all: all }
}
/**
 * 可控的分块响应：gate() 放行下一块。
 *
 * gate 是**计数信号量**（主控 Ruling 4）：计划里的骨架只在「已经有 pull 在等」时才放行，
 * gate 早于 pull 的话那次信号就丢了、测试死锁。这里先记一张通行证，下一次 pull 直接用掉 ——
 * 只改测试辅助的时序，断言一条不动。
 */
function chunked(parts, { status = 200, headers = {} } = {}) {
  let i = 0; let credits = 0; const waiters = []
  const gate = () => { const w = waiters.shift(); if (w) w(); else credits++ }
  const body = new ReadableStream({
    async pull(c) {
      if (i >= parts.length) return c.close()
      if (credits > 0) credits--
      else await new Promise((r) => waiters.push(r))
      const p = parts[i++]
      if (p instanceof Error) return c.error(p)
      c.enqueue(p)
    },
  })
  const len = parts.filter((p) => !(p instanceof Error)).reduce((n, p) => n + p.byteLength, 0)
  return { res: new Response(body, { status, headers: { 'content-length': String(len), 'content-type': 'video/mp4', ...headers } }), gate }
}
const bytes = (...xs) => new Uint8Array(xs)
const src = (label, fn) => ({ label, resolve: async () => `/${label}`, init: {}, fn })
/** 指定地址的默认源 —— 调度那几条要区分「请求的是哪一段」。 */
const at = (url) => ({ label: 'origin', resolve: async () => url, init: {} })
const tick = () => new Promise((r) => setTimeout(r, 0))
/** 指定地址的局域网来源 —— 熔断那条要区分「请求的是哪一个任务的局域网」。 */
const lanAt = (url) => ({ label: 'lan', resolve: async () => url, init: {} })
/**
 * 把整站数据源配好（开关开、地址填了）。带 `lan` 来源的用例都要先调它：任务开跑时会现查
 * `activeBase()`，数据源已熔断 / 已关闭就跳过局域网那条（fix round 1 #3）—— 所以「局域网
 * 来源会被试」这件事本身就以「数据源配好了」为前提，和真机上一样。
 */
const ADV_KEY = 'photoar.adv.v1'
const lanOn = () => store.set(ADV_KEY, JSON.stringify({ mediaBaseOn: true, mediaBase: 'http://192.168.1.10:8964' }))

let caches
// `resetTrip`：熔断是模块级的 60 秒真实时间，前一条用例熔断过的话，后一条的局域网来源会被跳过 —— 每条从「没熔断」开始。
beforeEach(() => { store.clear(); N.resetTrip(); caches = fakeCaches(); M._setEnv({ cachesImpl: caches }); M.setUser('u1') })

test('同一个键只下一次，两个读者拿到同样的字节', async () => {
  let calls = 0
  const { res, gate } = chunked([bytes(1, 2), bytes(3)])
  M._setEnv({ cachesImpl: caches, fetchImpl: async () => { calls++; return res } })
  const a = M.download('/v1/asset/a/stream', { sources: [src('origin')] })
  const b = M.download('/v1/asset/a/stream', { sources: [src('origin')] })
  assert.equal(a, b)
  const r1 = a.response()
  await tick(); gate()
  await tick()
  const r2 = a.response()                         // 半路才来的读者先拿到重放
  gate()
  const [x, y] = await Promise.all([r1.arrayBuffer(), r2.arrayBuffer()])
  assert.deepEqual([...new Uint8Array(x)], [1, 2, 3]); assert.deepEqual([...new Uint8Array(y)], [1, 2, 3])
  await a.done
  assert.equal(calls, 1); assert.equal(a.state, 'done')
  assert.ok(await M.cached('/v1/asset/a/stream'))
})

test('局域网半路断了：熔断并从默认源带 Range 续上', async () => {
  lanOn()
  const lan = chunked([bytes(1, 2), new TypeError('network')])
  const seen = []
  M._setEnv({ cachesImpl: caches, fetchImpl: async (url, init) => {
    seen.push([url, init.headers?.Range ?? null])
    if (url === '/lan') return lan.res
    return new Response(bytes(3, 4), { status: 206, headers: { 'content-range': 'bytes 2-3/4', 'content-length': '2' } })
  } })
  const j = M.download('/v1/asset/b/stream', { sources: [src('lan'), src('origin')] })
  await tick(); lan.gate(); await tick(); lan.gate()
  await j.done
  assert.equal(j.state, 'done'); assert.equal(j.source, 'origin')
  assert.deepEqual(seen, [['/lan', null], ['/origin', 'bytes=2-']])
  const got = new Uint8Array(await (await M.cached('/v1/asset/b/stream')).arrayBuffer())
  assert.deepEqual([...got], [1, 2, 3, 4])
})

test('续传时对方回 200 全量：丢掉已收那段', async () => {
  let n = 0
  M._setEnv({ cachesImpl: caches, fetchImpl: async () => {
    n++
    if (n === 1) { const c = chunked([bytes(1, 2), new TypeError('x')], { headers: { 'content-length': '4' } }); setTimeout(() => { c.gate(); setTimeout(c.gate, 0) }, 0); return c.res }
    return new Response(bytes(1, 2, 3, 4), { status: 200 })
  } })
  const j = M.download('/v1/asset/c/stream', { sources: [src('origin')] })
  await j.done
  const got = new Uint8Array(await (await M.cached('/v1/asset/c/stream')).arrayBuffer())
  assert.deepEqual([...got], [1, 2, 3, 4])
})

test('全部来源失败：state=error，不进缓存', async () => {
  M._setEnv({ cachesImpl: caches, fetchImpl: async () => new Response('no', { status: 404 }) })
  const j = M.download('/v1/asset/d/stream', { sources: [src('origin')] })
  await j.done
  assert.equal(j.state, 'error'); assert.match(j.error, /404/)
  assert.equal(await M.cached('/v1/asset/d/stream'), null)
})

test('写缓存失败不影响 done，只标 cacheFailed', async () => {
  const broken = fakeCaches(); const open = broken.open
  broken.open = async (n) => ({ ...(await open(n)), put: async () => { throw Object.assign(new Error('full'), { name: 'QuotaExceededError' }) } })
  M._setEnv({ cachesImpl: broken, fetchImpl: async () => new Response(bytes(9), { status: 200 }) })
  const j = M.download('/v1/asset/e/stream', { sources: [src('origin')] })
  await j.done
  assert.equal(j.state, 'done'); assert.match(j.cacheFailed, /存储空间/)
})

test('固定记录按用户隔离', () => {
  M.setUser('u1'); M.pin('p1'); assert.equal(M.isPinned('p1'), true)
  M.setUser('u2'); assert.equal(M.isPinned('p1'), false); assert.equal(M.pinnedIds().size, 0)
  M.setUser('u1'); assert.equal(M.isPinned('p1'), true)
})

test('groupStates：无视频 / 已缓存 / 未缓存', async () => {
  const c = await caches.open(M.VIDEO_CACHE)
  await c.put('/v1/asset/v1/stream', new Response(bytes(1)))
  const st = await M.groupStates([
    { photoId: 'a', videoAssetId: null }, { photoId: 'b', videoAssetId: 'v1', videoBytes: 1 }, { photoId: 'c', videoAssetId: 'v2', videoBytes: 5 },
  ])
  assert.equal(st.get('a').state, 'novideo'); assert.equal(st.get('b').state, 'cached'); assert.equal(st.get('c').state, 'none')
})

// ── 以下是实现里自己拍板的几处，各钉一条 ──────────────────────────────────

test('调度：后台任务在前台跑着时排队；被播放要走时立刻提升开跑；前台下完后排队的后台接着跑', async () => {
  const gates = new Map()
  const asked = []
  M._setEnv({ cachesImpl: caches, fetchImpl: async (url) => {
    asked.push(url)
    const c = chunked([bytes(7)]); gates.set(url, c); c.gate(); return c.res
  } })
  // 先把前台任务卡住：它的来源在 resolve 这一步等着，保证「前台在跑」这个状态持续到断言做完。
  let letFgGo
  const fgSrc = { label: 'origin', resolve: () => new Promise((r) => { letFgGo = () => r('/fg') }), init: {} }
  const fg = M.download('/v1/asset/fg/stream', { sources: [fgSrc], priority: M.Priority.USER })
  const bg1 = M.download('/v1/asset/bg1/stream', { sources: [at('/bg1')], priority: M.Priority.BACKGROUND })
  const bg2 = M.download('/v1/asset/bg2/stream', { sources: [at('/bg2')], priority: M.Priority.BACKGROUND })
  await tick()
  assert.equal(fg.state, 'running'); assert.equal(bg1.state, 'queued'); assert.equal(bg2.state, 'queued')

  // 扫到了 bg1：同一个任务被提成 PLAY，立刻开跑（不等前台那个下完）。
  assert.equal(M.download('/v1/asset/bg1/stream', { priority: M.Priority.PLAY }), bg1)
  assert.equal(bg1.priority, M.Priority.PLAY)
  await bg1.done
  assert.equal(bg1.state, 'done'); assert.deepEqual(asked, ['/bg1'])
  assert.equal(bg2.state, 'queued', '前台还在跑，后台不该开')

  letFgGo()
  await fg.done
  await bg2.done
  assert.equal(bg2.state, 'done'); assert.deepEqual(asked, ['/bg1', '/fg', '/bg2'])
  assert.deepEqual(M.activeJobs(), [])
})

test('局域网连上了但迟迟不回响应头（离开家里 WiFi）：超时熔断，从默认源下完', async () => {
  lanOn()
  const seen = []
  M._setEnv({ cachesImpl: caches, lanTimeoutMs: 20, fetchImpl: (url, init) => {
    seen.push(url)
    if (url === '/lan') {
      return new Promise((_, reject) => init.signal.addEventListener('abort', () => reject(init.signal.reason ?? new Error('aborted'))))
    }
    return Promise.resolve(new Response(bytes(5, 6), { status: 200 }))
  } })
  const j = M.download('/v1/asset/t/stream', { sources: [src('lan'), src('origin')] })
  await j.done
  assert.equal(j.state, 'done'); assert.equal(j.source, 'origin'); assert.deepEqual(seen, ['/lan', '/origin'])
  M._setEnv({ lanTimeoutMs: undefined })
})

test('abort：读者收到错误，任务表里不留，下一次 download 重新开一个', async () => {
  // 假响应不认 signal（一块都不放行）：abort 必须自己把正在等的那次 read 掐掉，不能只靠 fetch 的 signal。
  const c = chunked([bytes(1), bytes(2)])
  M._setEnv({ cachesImpl: caches, fetchImpl: async () => c.res })
  const j = M.download('/v1/asset/x/stream', { sources: [src('origin')] })
  const r = j.response()
  await tick()
  j.abort()
  await j.done
  assert.equal(j.state, 'aborted')
  await assert.rejects(r.arrayBuffer())
  assert.equal(M.jobFor('/v1/asset/x/stream'), undefined)
  assert.notEqual(M.download('/v1/asset/x/stream', { sources: [] }), j)
})

test('下完之后才来要 response() 的读者：从本机缓存读，而不是拿到一段空的', async () => {
  M._setEnv({ cachesImpl: caches, fetchImpl: async () => new Response(bytes(4, 5, 6), { status: 200 }) })
  const j = M.download('/v1/asset/late/stream', { sources: [src('origin')] })
  await j.done
  const got = new Uint8Array(await j.response().arrayBuffer())
  assert.deepEqual([...got], [4, 5, 6])
})

test('进度订阅：每块一次，结束时带最终状态；onChange 收到 cache 事件', async () => {
  const { res, gate } = chunked([bytes(1), bytes(2, 3)])
  M._setEnv({ cachesImpl: caches, fetchImpl: async () => res })
  const events = []
  const off = M.onChange((e) => events.push(e.type === 'cache' ? `cache ${e.key}` : `job ${e.snap.state} ${e.snap.loaded}`))
  const j = M.download('/v1/asset/s/stream', { sources: [src('origin')] })
  const snaps = []
  j.subscribe((s) => snaps.push(`${s.state} ${s.loaded}/${s.total}`))
  gate(); gate()
  await j.done
  off()
  assert.deepEqual(snaps.slice(-3), ['running 1/3', 'running 3/3', 'done 3/3'])
  assert.equal(events.at(-1), 'cache /v1/asset/s/stream')
})

test('cacheGroup：固定 + 原图与视频各进各的缓存；失败时不取消固定', async () => {
  M._setEnv({ cachesImpl: caches, fetchImpl: async (url) => {
    if (url === '/v1/photo/g1/ref') return new Response(bytes(8), { status: 200, headers: { 'content-type': 'image/jpeg' } })
    if (url === '/v1/asset/gv/stream') return new Response(bytes(9), { status: 200 })
    return new Response('no', { status: 500 })
  } })
  const ok = await M.cacheGroup({ photoId: 'g1', videoAssetId: 'gv', videoBytes: 1 })
  assert.deepEqual(ok, { ok: true })
  assert.equal(M.isPinned('g1'), true)
  assert.ok(await M.cached(M.refKey('g1'), M.REF_CACHE))
  assert.ok(await M.cached(M.videoKey('gv')))
  const st = await M.groupStates([{ photoId: 'g1', videoAssetId: 'gv', videoBytes: 1 }])
  assert.deepEqual(st.get('g1'), { state: 'cached', pinned: true, pct: null, bytes: 1 })

  const bad = await M.cacheGroup({ photoId: 'g2', videoAssetId: 'nope', videoBytes: 1 })
  assert.equal(bad.ok, false); assert.match(bad.error, /500/)
  assert.equal(M.isPinned('g2'), true, '下一次预取会补，所以不取消固定')

  await M.removeGroup({ photoId: 'g1', videoAssetId: 'gv' })
  assert.equal(M.isPinned('g1'), false)
  assert.equal(await M.cached(M.videoKey('gv')), null)
  assert.equal(await M.cached(M.refKey('g1'), M.REF_CACHE), null)
})

test('putBlob / cachedKeySet / removeKey', async () => {
  await M.putBlob(M.overrideKey('p9'), new Blob([bytes(1, 2)], { type: 'video/mp4' }), M.OVERRIDE_CACHE)
  assert.deepEqual([...await M.cachedKeySet(M.OVERRIDE_CACHE)], ['/__override/p9'])
  const r = await M.cached(M.overrideKey('p9'), M.OVERRIDE_CACHE)
  assert.equal(r.headers.get('content-length'), '2')
  await M.removeKey(M.overrideKey('p9'), M.OVERRIDE_CACHE)
  assert.equal((await M.cachedKeySet(M.OVERRIDE_CACHE)).size, 0)
})

test('没有 Cache Storage：cached/cachedKeySet 退化成空，下载照样完成并说明没存下', async () => {
  M._setEnv({ cachesImpl: null, fetchImpl: async () => new Response(bytes(1), { status: 200 }) })
  assert.equal(await M.cached('/v1/asset/n/stream'), null)
  assert.equal((await M.cachedKeySet()).size, 0)
  const j = M.download('/v1/asset/n/stream', { sources: [src('origin')] })
  await j.done
  assert.equal(j.state, 'done'); assert.ok(j.cacheFailed)
})

// ── fix round 1（主控 Ruling 13）：评审 #2 #3 #8 各钉一条 ─────────────────────

test('abort 之后、任务还没真正收尾时来要同一段：新开一个任务，不挂到将死的那个上', async () => {
  // 场景：先「从本机移除」（abort 正在跑的后台任务），紧接着扫到这张 → 播放要的必须是一个能下完的任务，
  // 而不是挂到那个马上以 aborted 结束的任务上（那样播放就失败了）。
  const first = chunked([bytes(1), bytes(2)])   // 一块都不放行：abort 那一刻它还在 running、正等着 read
  let letPlayGo
  const playSrc = { label: 'origin', resolve: () => new Promise((r) => { letPlayGo = () => r('/again') }), init: {} }
  M._setEnv({ cachesImpl: caches, fetchImpl: async (url) => (url === '/origin' ? first.res : new Response(bytes(1, 2), { status: 200 })) })
  const key = '/v1/asset/re/stream'
  const old = M.download(key, { sources: [src('origin')], priority: M.Priority.BACKGROUND })
  await tick()
  assert.equal(old.state, 'running')
  old.abort()
  assert.equal(old.state, 'running', '前提：abort 是异步收尾的，这一刻它还没结束')
  const play = M.download(key, { sources: [playSrc], priority: M.Priority.PLAY })
  assert.notEqual(play, old)
  assert.equal(M.jobFor(key), play)
  await old.done
  assert.equal(old.state, 'aborted')
  assert.equal(M.jobFor(key), play, '将死的那个收尾时不能把新任务从表里删掉')
  letPlayGo()
  await play.done
  assert.equal(play.state, 'done')
  assert.deepEqual([...new Uint8Array(await (await M.cached(key)).arrayBuffer())], [1, 2])
})

test('熔断之前就排队的任务：开跑时数据源已熔断 / 已关闭，直接跳过局域网那条', async () => {
  // 没有这一条的话，排队中的每个任务开跑时都会先去试已经熔断的局域网 —— 真机上是每段各白等一次 8 秒超时。
  lanOn()
  const seen = []
  M._setEnv({ cachesImpl: caches, fetchImpl: async (url) => {
    seen.push(url)
    if (url.startsWith('/lan')) throw new TypeError('network')
    return new Response(bytes(1), { status: 200 })
  } })
  const fg = M.download('/v1/asset/q1/stream', { sources: [lanAt('/lan-q1'), at('/q1')], priority: M.Priority.USER })
  const bg = M.download('/v1/asset/q2/stream', { sources: [lanAt('/lan-q2'), at('/q2')], priority: M.Priority.BACKGROUND })
  assert.equal(bg.state, 'queued', '前提：前台在跑，后台排队 —— 来源列表是在熔断之前定下的')
  await Promise.all([fg.done, bg.done])
  assert.equal(fg.state, 'done'); assert.equal(bg.state, 'done'); assert.equal(bg.source, 'origin')
  assert.deepEqual(seen, ['/lan-q1', '/q1', '/q2'])

  // 用户在设置页把数据源关掉了：同一个口径（`activeBase()` 为空），已经拿到局域网来源的任务也不再试它。
  N.resetTrip()
  store.set(ADV_KEY, JSON.stringify({ mediaBaseOn: false, mediaBase: 'http://192.168.1.10:8964' }))
  const off = M.download('/v1/asset/q3/stream', { sources: [lanAt('/lan-q3'), at('/q3')] })
  await off.done
  assert.equal(off.state, 'done')
  assert.deepEqual(seen.slice(3), ['/q3'])
})

test('原图任务：响应头到达之前来要 response()，类型是图片而不是 video/mp4；写进缓存的也是', async () => {
  let release
  const held = { label: 'origin', resolve: () => new Promise((r) => { release = () => r('/ref') }), init: {} }
  // 对方没给 Content-Type（Uint8Array 的 body 不会自带类型）：只能靠按缓存名取的缺省值。
  M._setEnv({ cachesImpl: caches, fetchImpl: async () => new Response(bytes(8), { status: 200 }) })
  const key = M.refKey('p8')
  const j = M.download(key, { cacheName: M.REF_CACHE, sources: [held] })
  await tick()
  const early = j.response()
  assert.equal(early.headers.get('content-type'), 'image/jpeg')
  release()
  assert.deepEqual([...new Uint8Array(await early.arrayBuffer())], [8])
  await j.done
  assert.equal((await M.cached(key, M.REF_CACHE)).headers.get('content-type'), 'image/jpeg')
  // 视频那一头的缺省值不变。
  M._setEnv({ cachesImpl: caches, fetchImpl: async () => new Response(bytes(9), { status: 200 }) })
  const v = M.download('/v1/asset/ct/stream', { sources: [src('origin')] })
  assert.equal(v.response().headers.get('content-type'), 'video/mp4')
  await v.done
  assert.equal((await M.cached('/v1/asset/ct/stream')).headers.get('content-type'), 'video/mp4')
})

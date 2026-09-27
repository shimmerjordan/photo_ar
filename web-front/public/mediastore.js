/**
 * 本机媒体仓库：**一段视频一个下载任务**，外加 Cache Storage 的读写与「固定」记录。
 *
 * ## 为什么要有它（设计 §1：「缓存没生效」其实是三条根因）
 *
 * 1. 预取每会话只跑一次、串行；这期间扫到的视频走票据边下边播，但**不落缓存** ——
 *    看过的下次还要重下；
 * 2. 预取与播放各下各的：预取正在下 A，扫到 A 又下一遍（两倍流量、两倍时间）；
 * 3. 预取的清理只认「按新到旧装满预算」那份计划，用户想要「这几组一定在本机」没法表达。
 *
 * 三条的共同病根是**同一段字节有好几个下载者，却没有一个主人**。所以这里立一个主人：
 * 按缓存键去重的任务表。播放、预取、手动缓存、存到手机都挂在同一个任务上 —— 谁先要
 * 谁开，后来的挂上去看同一份进度；下完一律 `cache.put`，于是「看过的」自然就是「存下的」。
 *
 * ## 任务 = 内存里的块 + 任意多个读者
 *
 * 任务边收边把块记在内存里，`response()` 给每个读者一条「先重放已收的、再跟实时的」流：
 * 半路才来的播放器不用等、也不用再下一遍。下完把块拼成一个 Blob 写进缓存，然后放掉
 * 自己手里那份数组 —— 读者闭包里抓着的还在，读完随它们一起回收。下完之后才来要
 * `response()` 的读者改从缓存读（见 `_fromCache`），不会拿到一段空的。
 *
 * ## 续传换源（设计 §2.1：数据源只加速，不能成为新的故障点）
 *
 * 来源列表来自 `netsrc.sourcesFor`：数据源在前、默认源在后。一个来源断了就带
 * `Range: bytes=<已收>-` 重试（最多 3 次），再换下一个。**局域网那条一出错就熔断**
 * （`tripBase()`，60 秒内的新任务直接不走它；熔断前就排着队的任务开跑时也现查、跳过它）
 * 并立刻换默认源 —— 离开家里 WiFi、
 * 本地网络权限被拒、混合内容被拦，这几种都不会过一会儿自己好。对方不认 Range、
 * 回了 200 全量，就丢掉开头已收的那段：对读者来说字节流始终是连续的，不从头来。
 *
 * 局域网那条另有一只看门狗（`LAN_TIMEOUT_MS`）：手机已经不在家里的网络上时，发往
 * 192.168.x.x 的连接不会被拒，而是**一直挂着**（系统的 SYN 重试要一两分钟才放弃）。
 * 没有它的话，「离开家里 WiFi」的表现就是扫到照片后干等一分多钟。
 *
 * ## 调度：三档优先级
 *
 * | 档 | 谁 | 规则 |
 * |---|---|---|
 * | `PLAY` | 正在看的那一段 | 立刻开跑，不排队 |
 * | `USER` | 用户点了「缓存」 | 同时最多 2 个 |
 * | `BACKGROUND` | 预取 | 只在没有 PLAY/USER 在跑时开，同时最多 1 个 |
 *
 * 已经存在的任务被更高一档要到时就地提升（排队中的立刻按新档调度）。后台任务**已经开跑的
 * 不打断**：它最多一个，而预取是一段下完才排下一段的，让路发生在段与段之间。
 *
 * ## 固定（pin）
 *
 * 用户点「缓存」的组记在 `localStorage['photoar.pins.v1.<userId>']`：预取的预算与清理
 * 都绕开它们（见 prefetch.js 的 `keepSet`）。**按用户分键**是为了换人登录同一台手机时
 * 看不到上一个人的固定记录 —— 而缓存里上一个人才有权限的那些，由预取的清理按当前用户的
 * 授权清单删掉。隐私模式下 localStorage 会抛，退回这次会话内有效的内存记录。
 *
 * ## 测试钩子
 *
 * `fetch` / `caches` 都经 `env` 取（`_setEnv` 仅测试用），默认**现用现取**
 * `globalThis.fetch` / `globalThis.caches` —— 模块在 Node 里 import 时不碰任何浏览器全局。
 */
import { diagAlways } from './diag.js'
import { activeBase, sourcesFor, tripBase } from './netsrc.js'
import { mb } from './ui.js'

export const VIDEO_CACHE = 'photoar-media-v1'
export const REF_CACHE = 'photoar-ref-v1'
export const THUMB_CACHE = 'photoar-thumb-v1'
export const OVERRIDE_CACHE = 'photoar-override-v1'
export const videoKey = (assetId) => `/v1/asset/${assetId}/stream`
export const refKey = (photoId) => `/v1/photo/${photoId}/ref`
export const thumbKey = (photoId) => `/v1/photo/${photoId}/thumb`
export const overrideKey = (photoId) => `/__override/${photoId}`
export const Priority = { PLAY: 0, USER: 1, BACKGROUND: 2 }
/** `cacheFailed` 的「写满了」那一档。预取拿它判断要不要停手（后面每一段都会下完再被拒）。 */
export const STORAGE_FULL = '浏览器存储空间不够'
const NO_CACHE = '这个浏览器环境没有本机缓存（要 https 才有）'
/**
 * 对方没给 Content-Type、或者响应头还没到就有人来要 `response()` 时的缺省类型，**按缓存名取**：
 * 原图缓存里装的是 JPEG，一律缺省成 video/mp4 的话，原图那一路的读者（以及写进缓存的那条）
 * 拿到的是错的类型。
 */
const defaultTypeOf = (cacheName) => (cacheName === REF_CACHE ? 'image/jpeg' : 'video/mp4')

/** 同一个来源最多试几次（第一次 + 两次续传）。 */
const ATTEMPTS = 3
/** 同一来源重试前歇一下：`n × 这个数` 毫秒。立刻重试在网络抖的那一秒里多半还是失败。 */
const RETRY_BASE_MS = 300
/**
 * 局域网来源多久没动静就放弃（等响应头、以及两块之间）。取 8 秒而不是设置页「测试」
 * 用的 4 秒：NAS 的机械盘休眠后起转要五到十秒，那不是「连不上」，不该为它熔断一分钟。
 */
const LAN_TIMEOUT_MS = 8000
const USER_SLOTS = 2
const BACKGROUND_SLOTS = 1

// ── 环境（测试注入）─────────────────────────────────────────────────────
const env = { fetchImpl: undefined, cachesImpl: undefined, now: undefined, lanTimeoutMs: undefined }
/** 仅测试用。给了的键覆盖，给 `undefined` 恢复默认；`cachesImpl: null` = 模拟没有 Cache Storage。 */
export function _setEnv(o = {}) {
  for (const k of Object.keys(env)) if (k in o) env[k] = o[k]
}
const doFetch = (url, init) => (env.fetchImpl ? env.fetchImpl(url, init) : globalThis.fetch(url, init))
const cachesApi = () => (env.cachesImpl !== undefined ? env.cachesImpl : globalThis.caches) ?? null
const now = () => (env.now ? env.now() : Date.now())
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function openCache(name) {
  const cs = cachesApi()
  if (!cs) return null
  try {
    return await cs.open(name)
  } catch {
    return null
  }
}

const cacheErrorText = (e) => (e?.name === 'QuotaExceededError' ? STORAGE_FULL : String(e?.message ?? e))

// ── 变更通知 ────────────────────────────────────────────────────────────
const listeners = new Set()
/**
 * 订阅一切变化。`evt` 是下面三种之一，页面按 `type` 取自己关心的、其余忽略：
 * - `{type:'job', snap}` —— 某个任务的状态/进度变了（每收一块一次，页面自己节流）；
 * - `{type:'cache', key, cacheName}` —— 缓存里多了/少了一条（`key` 为 null = 整个缓存清空）；
 * - `{type:'pin', photoId, pinned}` —— 固定记录变了（「自动缓存」点成「已缓存」时没有任何下载，
 *   没有这一种的话页面收不到重画的理由）。
 */
export function onChange(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}
function emit(evt) {
  for (const fn of [...listeners]) {
    try { fn(evt) } catch { /* 一个页面的监听炸了不该连累下载本身 */ }
  }
}

// ── 固定记录 ────────────────────────────────────────────────────────────
let user = 'anon'
const memPins = new Map()   // userId → string[]：localStorage 用不了时（隐私模式）的会话内退路
const pinsKey = (u) => `photoar.pins.v1.${u}`

/** 登录拿到 `me` 之后、预取开跑之前调。固定记录从此按这个人读写。 */
export function setUser(userId) {
  user = String(userId ?? '') || 'anon'
}

/**
 * **每次现读**，不在内存里缓存一份：两个标签页各开一份的话，内存那份会在对方改过之后
 * 继续写回旧值，把对方刚固定的那组冲掉。几十个 id 的 JSON，读一次的成本可以忽略。
 */
function readPins() {
  let raw
  try {
    raw = globalThis.localStorage.getItem(pinsKey(user))
  } catch {
    return new Set(memPins.get(user) ?? [])
  }
  try {
    const v = raw ? JSON.parse(raw) : []
    return new Set(Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [])
  } catch {
    return new Set()   // 坏 JSON 当没固定过，不让一条脏数据把整页读挂
  }
}
function writePins(set) {
  const arr = [...set]
  memPins.set(user, arr)
  try {
    globalThis.localStorage.setItem(pinsKey(user), JSON.stringify(arr))
  } catch { /* 隐私模式：这次会话里仍然有效 */ }
}
function setPinned(photoId, on) {
  const id = String(photoId)
  const s = readPins()
  if (s.has(id) === on) return
  on ? s.add(id) : s.delete(id)
  writePins(s)
  emit({ type: 'pin', photoId: id, pinned: on })
}
export const pinnedIds = () => readPins()
export const isPinned = (photoId) => readPins().has(String(photoId))
export const pin = (photoId) => setPinned(photoId, true)
export const unpin = (photoId) => setPinned(photoId, false)

// ── 任务表与调度 ────────────────────────────────────────────────────────
/** key → Job。只放排队中 / 在跑 / 刚下完正在写缓存的；结束（任何一种）就删。 */
const jobs = new Map()
let seq = 0

export const jobFor = (key) => jobs.get(key)
export const activeJobs = () => [...jobs.values()]

/**
 * 要一段字节。同一个 `key` 已经有任务就返回那个任务（按需提升优先级），否则新开一个。
 *
 * 已有任务时 `sources` / `cacheName` 以**先来的那个**为准：同一个键的来源本来就由
 * `sourcesFor(key)` 唯一决定，后来者传进来的只会是同一份。
 *
 * **已经被 `abort()` 的不复用**：abort 是异步收尾的（要等正在读的那次 read 被掐掉），
 * 这期间它还在表里。典型场景是先「从本机移除」、紧接着扫到这张 —— 播放要是挂到这个
 * 将死的任务上，拿到的就是一个以 aborted 结束的流，播放失败。所以新开一个顶替它；
 * 旧的收尾时 `_settle` 只删「表里仍是自己」的那条，不会把新任务删掉。
 */
export function download(key, { cacheName = VIDEO_CACHE, sources, priority = Priority.USER } = {}) {
  const had = jobs.get(key)
  if (had && !had._aborted) {
    if (priority < had.priority) {
      had.priority = priority
      schedule()
    }
    return had
  }
  const job = new Job(key, cacheName, sources ?? sourcesFor(key), priority)
  jobs.set(key, job)
  schedule()
  return job
}

function schedule() {
  const all = [...jobs.values()]
  const running = all.filter((j) => j.state === 'running')
  const busy = (p) => running.filter((j) => j.priority === p).length
  const queued = all.filter((j) => j.state === 'queued').sort((a, b) => a.priority - b.priority || a.seq - b.seq)
  for (const j of queued) {
    const go = j.priority === Priority.PLAY
      || (j.priority === Priority.USER && busy(Priority.USER) < USER_SLOTS)
      || (j.priority >= Priority.BACKGROUND && busy(Priority.PLAY) + busy(Priority.USER) === 0 &&
        busy(Priority.BACKGROUND) < BACKGROUND_SLOTS)
    if (go) {
      running.push(j)
      j._start()
    }
  }
}

/** 206 以 Content-Range 的 `/总长` 为准；200 就是 Content-Length。压缩过的长度不是字节数，当不知道。 */
function totalOf(res, start) {
  const cr = /\/(\d+)\s*$/.exec(res.headers.get('content-range') ?? '')
  if (cr) return Number(cr[1])
  const enc = res.headers.get('content-encoding')
  if (enc && enc !== 'identity') return 0
  const len = Number(res.headers.get('content-length'))
  if (!Number.isFinite(len) || len <= 0) return 0
  return res.status === 206 ? start + len : len
}

class Job {
  constructor(key, cacheName, sources, priority) {
    Object.assign(this, { key, cacheName, sources, priority, seq: ++seq })
    this.state = 'queued'
    this.loaded = 0
    this.total = 0          // 0 = 还不知道（不定长）
    this.source = this.error = this.cacheFailed = null
    this.contentType = ''
    this.chunks = []
    this._subs = new Set()
    this._aborted = this._settled = this._released = false
    this._ac = this._reader = this._lastErr = this._tick = this._tickResolve = null
    /** 永远 resolve —— 结束后看 `state`。 */
    this.done = new Promise((r) => { this._resolveDone = r })
  }

  snap() {
    const { key, state, loaded, total, source, error, cacheFailed } = this
    return { key, state, loaded, total, source, error, cacheFailed }
  }

  subscribe(fn) {
    this._subs.add(fn)
    return () => this._subs.delete(fn)
  }

  _emit() {
    const s = this.snap()
    for (const fn of [...this._subs]) {
      try { fn(s) } catch { /* 同 emit */ }
    }
    emit({ type: 'job', snap: s })
  }

  /** 下一块到达或状态变化时 resolve。所有读者共用同一个 promise。 */
  _nextTick() {
    return (this._tick ??= new Promise((r) => { this._tickResolve = r }))
  }
  _wake() {
    const r = this._tickResolve
    this._tick = this._tickResolve = null
    r?.()
  }

  /** 取消。**是所有人的取消** —— 只是不想看了的一方应该退订 / cancel 自己那条流，而不是调这个。 */
  abort() {
    if (this.state !== 'queued' && this.state !== 'running') return
    this._aborted = true
    if (this.state === 'queued') return this._end('aborted')
    this._ac?.abort()
    // 有的流不认 signal（或者 signal 到得比 read 晚）：直接掐掉正在等的那次 read。
    this._reader?.cancel().catch(() => {})
  }

  _start() {
    this._startedAt = now()
    this._run().catch((e) => {
      this.error = `下载出错：${e?.message ?? e}`
      this._end('error')
    })
  }

  async _run() {
    this.state = 'running'
    this._emit()
    for (const src of this.sources) {
      for (let attempt = 0; attempt < ATTEMPTS; attempt++) {
        if (attempt > 0) await sleep(RETRY_BASE_MS * attempt)
        if (this._aborted) return this._end('aborted')
        // 来源列表是**建任务时**定下的，而排队的任务可能过好一阵才开跑：这期间别的任务已经
        // 把局域网熔断了（或者用户在设置页关掉了数据源），再试它只会每段各白等一次看门狗超时
        // （8 秒）。所以每次要试局域网之前现查一次 —— 口径就是 `sourcesFor` 给不给它的那个口径，
        // 连换票那一下也省掉。放在重试循环里：同一来源重试的间隙里被熔断的，也不再试。
        if (src.label === 'lan' && !activeBase()) break
        let url
        try {
          url = await src.resolve()
        } catch (e) {
          this._lastErr = e   // 票没换到 → 下一个来源
          break
        }
        const next = await this._attempt(src, url)
        if (next === 'finish') return this._finish()
        if (next === 'aborted') return this._end('aborted')
        if (next === 'nextSource') break
      }
    }
    this.error = String(this._lastErr?.message ?? this._lastErr ?? '下载失败')
    this._end('error')
  }

  /** 对一个来源发一次请求并读到底。返回 `finish | retry | nextSource | aborted`。 */
  async _attempt(src, url) {
    if (this._aborted) return 'aborted'   // 换票那一下（`resolve`）期间被取消的
    const lan = src.label === 'lan'
    const ac = new AbortController()
    this._ac = ac
    let timer = null
    const watch = () => {
      if (!lan) return
      clearTimeout(timer)
      const ms = env.lanTimeoutMs ?? LAN_TIMEOUT_MS
      timer = setTimeout(() => {
        const why = new DOMException(`局域网数据源 ${Math.round(ms / 100) / 10} 秒没有动静`, 'TimeoutError')
        ac.abort(why)
        this._reader?.cancel(why).catch(() => {})
      }, ms)
    }
    const fail = (e) => {
      if (this._aborted) return 'aborted'
      this._lastErr = e
      if (!lan) return 'retry'
      tripBase()   // 局域网不通 → 熔断，立刻换默认源（断点续传）
      diagAlways(`局域网数据源出错（${e?.message ?? e}），熔断 60 秒，已收 ${mb(this.loaded)}MB 从默认源续上`)
      return 'nextSource'
    }
    try {
      watch()
      const headers = this.loaded > 0 ? { Range: `bytes=${this.loaded}-` } : {}
      let res
      try {
        res = await doFetch(url, { ...src.init, headers, signal: ac.signal })
      } catch (e) {
        return fail(e)
      }
      if (res.status === 416 && this.total && this.loaded >= this.total) return 'finish'
      if (!res.ok || !res.body) {
        res.body?.cancel().catch(() => {})
        this._lastErr = new Error(`HTTP ${res.status}`)
        return [401, 403, 404].includes(res.status) ? 'nextSource' : 'retry'
      }
      // 206 从哪一字节开始：比已收的靠后就是对不上（中间会缺一截），靠前就丢掉重叠的那段。
      // 200 是对方不认 Range、从头给了全量：丢掉开头已收的那段。
      let start = 0
      if (res.status === 206) {
        start = Number(/bytes (\d+)-/.exec(res.headers.get('content-range') ?? '')?.[1])
        if (!Number.isFinite(start) || start > this.loaded) {
          res.body.cancel().catch(() => {})
          this._lastErr = new Error('续传位置对不上')
          return 'retry'
        }
      }
      let skip = this.loaded - start
      const t = totalOf(res, start)
      if (t) this.total = t
      this.contentType ||= res.headers.get('content-type') || defaultTypeOf(this.cacheName)
      this.source = src.label
      this._emit()
      const reader = res.body.getReader()
      this._reader = reader
      try {
        for (;;) {
          watch()
          const { done, value } = await reader.read()
          if (done) break
          let chunk = value
          if (skip > 0) {
            const d = Math.min(skip, chunk.byteLength)
            skip -= d
            chunk = chunk.subarray(d)
            if (!chunk.byteLength) continue
          }
          this.chunks.push(chunk)
          this.loaded += chunk.byteLength
          this._wake()
          this._emit()
        }
      } catch (e) {
        return fail(e)
      } finally {
        this._reader = null
      }
      if (this._aborted) return 'aborted'
      if (ac.signal.aborted) return fail(ac.signal.reason)   // 看门狗掐的：read 被 cancel 成了 done
      if (this.total && this.loaded < this.total) {
        this._lastErr = new Error('连接提前结束')
        return 'retry'
      }
      return 'finish'
    } finally {
      clearTimeout(timer)
      if (this._ac === ac) this._ac = null
    }
  }

  /**
   * 下完：先让读者收尾（播放不等写缓存），再写缓存，**写完才从任务表里删** ——
   * 反过来的话，写缓存那几百毫秒里来要同一段的人既找不到任务、缓存里也还没有，会再下一遍。
   * 写失败（多半是配额满了）只标 `cacheFailed`，状态照样是 done：播放已经拿到全部字节了。
   */
  async _finish() {
    this.state = 'done'
    this._wake()
    const type = this.contentType || defaultTypeOf(this.cacheName)
    const c = await openCache(this.cacheName)
    if (!c) {
      this.cacheFailed = NO_CACHE
    } else {
      try {
        const blob = new Blob(this.chunks, { type })
        await c.put(this.key, new Response(blob, { headers: { 'Content-Type': type, 'Content-Length': String(this.loaded) } }))
      } catch (e) {
        this.cacheFailed = cacheErrorText(e)
      }
    }
    this._settle()
    if (!this.cacheFailed) emit({ type: 'cache', key: this.key, cacheName: this.cacheName })
  }

  _end(state) {
    if (this._settled) return
    this.state = state
    if (state === 'aborted') this.error ??= '下载已取消'
    this._wake()
    this._settle()
  }

  _settle() {
    if (this._settled) return
    this._settled = true
    if (jobs.get(this.key) === this) jobs.delete(this.key)
    this.chunks = []          // 读者已经抓住旧数组；这里只放掉任务自己那份引用
    this._released = true
    this._emit()
    diagAlways(`下载 ${this.key} ${this.state}` +
      (this.source ? ` via=${this.source}` : '') +
      ` ${mb(this.loaded)}MB ${now() - (this._startedAt ?? now())}ms` +
      (this.error ? ` ${this.error}` : '') +
      (this.cacheFailed ? ` 没存进本机：${this.cacheFailed}` : ''))
    this._resolveDone()
    schedule()
  }

  /** body 是「已收的重放 + 实时后续」；total 已知时带 Content-Length。 */
  response() {
    const headers = { 'Content-Type': this.contentType || defaultTypeOf(this.cacheName) }
    if (this.total) headers['Content-Length'] = String(this.total)
    return new Response(this._released ? this._fromCache() : this._live(), { headers })
  }

  _live() {
    const buf = this.chunks   // 抓住数组本身：结束后任务放掉引用，读者手里这份还在
    const job = this
    let i = 0
    let gone = false
    return new ReadableStream({
      async pull(ctl) {
        for (;;) {
          if (gone) return
          if (i < buf.length) return ctl.enqueue(buf[i++])
          if (job.state === 'done') return ctl.close()
          if (job.state === 'error' || job.state === 'aborted') return ctl.error(new Error(job.error ?? '下载中断'))
          await job._nextTick()
        }
      },
      cancel() { gone = true },
    })
  }

  /** 任务已经放掉内存里的块了：下完的从缓存读；没存下 / 没下完的如实报错。 */
  _fromCache() {
    const job = this
    let inner = null
    return new ReadableStream({
      async pull(ctl) {
        try {
          if (!inner) {
            const r = job.state === 'done' ? await cached(job.key, job.cacheName) : null
            if (!r?.body) {
              throw new Error(job.state === 'done'
                ? `这段下完了但没存进本机（${job.cacheFailed ?? '缓存里找不到'}），要重新下`
                : job.error ?? '下载中断')
            }
            inner = r.body.getReader()
          }
          const { done, value } = await inner.read()
          if (done) ctl.close()
          else ctl.enqueue(value)
        } catch (e) {
          ctl.error(e)
        }
      },
      cancel(reason) { return inner?.cancel(reason) },
    })
  }
}

// ── 缓存读写 ────────────────────────────────────────────────────────────
/** 命中返回 Response（每次 match 都是新的一份，可直接消费）；未命中 / 环境不支持返回 null。 */
export async function cached(key, cacheName = VIDEO_CACHE) {
  const cs = cachesApi()
  if (!cs || !key) return null
  try {
    const c = await cs.open(cacheName)
    return (await c.match(key)) ?? null
  } catch {
    return null
  }
}

/** 缓存里有哪些键（pathname）。一次 `keys()` 顶得上逐条 `match`，列表页一屏几十张时要紧。 */
export async function cachedKeySet(cacheName = VIDEO_CACHE) {
  const c = await openCache(cacheName)
  if (!c) return new Set()
  try {
    return new Set((await c.keys()).map((r) => new URL(r.url).pathname))
  } catch {
    return new Set()
  }
}

export async function removeKey(key, cacheName = VIDEO_CACHE) {
  const c = await openCache(cacheName)
  if (!c) return false
  let hit = false
  try {
    hit = await c.delete(key)
  } catch { /* 删不掉就下次再删 */ }
  if (hit) emit({ type: 'cache', key, cacheName })
  return hit
}

/** 整个缓存清空（缓存页的「清空」按钮）。 */
export async function clearCache(cacheName) {
  const cs = cachesApi()
  if (!cs) return
  try {
    await cs.delete(cacheName)
  } catch { /* 同上 */ }
  emit({ type: 'cache', key: null, cacheName })
}

/**
 * 把一份现成的字节写进缓存（单个来源「本机文件」用）。**失败会抛**，且写满时换成人话 ——
 * 设置页要据此告诉用户「没存下」，而不是存了一条记录却没有字节。
 */
export async function putBlob(key, blob, cacheName = OVERRIDE_CACHE) {
  const cs = cachesApi()
  if (!cs) throw new Error(NO_CACHE)
  const type = blob.type || 'application/octet-stream'
  try {
    const c = await cs.open(cacheName)
    await c.put(key, new Response(blob, { headers: { 'Content-Type': type, 'Content-Length': String(blob.size) } }))
  } catch (e) {
    throw new Error(cacheErrorText(e), { cause: e })
  }
  emit({ type: 'cache', key, cacheName })
}

// ── 组：一张照片的原图 + 视频 ───────────────────────────────────────────
const idOf = (photo) => String(photo?.photoId ?? photo?.id ?? '')

/** `video` 为 null = 这张没配视频（或服务端太老、`/v1/photos` 没给 `videoAssetId`）。 */
export function groupKeys(photo) {
  const id = idOf(photo)
  return {
    video: photo?.videoAssetId ? videoKey(photo.videoAssetId) : null,
    ref: refKey(id),
    thumb: thumbKey(id),
  }
}

/**
 * 用户点了「缓存」：先固定（之后预取的预算与清理都绕开它），再把原图与视频拉进各自的缓存。
 *
 * 下载失败**不取消固定**：用户的意思是「这组要在本机」，这次网络不行不改变那个意思 ——
 * 下一次预取会先补固定组。缩略图不在这里下：它归预取管（每张都有，几十 KB）。
 */
export async function cacheGroup(photo, { priority = Priority.USER } = {}) {
  const id = idOf(photo)
  if (!id) return { ok: false, error: '这张照片没有 id' }
  await requestPersist()   // 失败无妨：只是让浏览器在空间紧张时别先清我们
  pin(id)
  const { video, ref } = groupKeys(photo)
  const want = [[ref, REF_CACHE]]
  if (video) want.push([video, VIDEO_CACHE])
  const started = []
  for (const [key, cacheName] of want) {
    if (!jobFor(key) && (await cached(key, cacheName))) continue   // 已经在本机了
    started.push(download(key, { cacheName, priority }))
  }
  await Promise.all(started.map((j) => j.done))
  const bad = started.find((j) => j.state !== 'done')
  if (bad) return { ok: false, error: bad.error ?? '下载中断' }
  // 下完了但没写进缓存：对「缓存到本机」这个动作来说就是没成功。
  const unsaved = started.find((j) => j.cacheFailed)
  if (unsaved) return { ok: false, error: unsaved.cacheFailed }
  return { ok: true }
}

/**
 * 从本机移除一组：取消固定，删原图与视频。还在下的（非播放）任务先取消 ——
 * 否则它下完会把刚删的那段又写回去。**正在播放的那段不取消**：用户在看，
 * 它下完会以「自动缓存」的身份留下，由下一次预取的清理按预算决定去留。
 */
export async function removeGroup(photo) {
  const id = idOf(photo)
  if (id) unpin(id)
  const { video, ref } = groupKeys(photo)
  for (const [key, cacheName] of [[ref, REF_CACHE], [video, VIDEO_CACHE]]) {
    if (!key) continue
    const j = jobFor(key)
    if (j && j.priority !== Priority.PLAY) {
      j.abort()
      await j.done
    }
    await removeKey(key, cacheName)
  }
}

/**
 * 每组的缓存状态，给媒体页 / 宾客页的缓存按钮。按视频判断（大件是它，原图几 MB）。
 * `pct` 只在下载中且总长已知时有值；`bytes` 是视频大小（`videoBytes`），用来算占用。
 */
export async function groupStates(photos) {
  const have = await cachedKeySet(VIDEO_CACHE)
  const pins = pinnedIds()
  const out = new Map()
  for (const p of photos ?? []) {
    const id = idOf(p)
    if (!id) continue
    const pinned = pins.has(id)
    const { video } = groupKeys(p)
    const vb = Number(p.videoBytes)
    const bytes = Number.isFinite(vb) && vb > 0 ? vb : null
    if (!video) {
      out.set(id, { state: 'novideo', pinned, pct: null, bytes: null })
      continue
    }
    const j = jobFor(video)
    if (j) {
      out.set(id, { state: 'downloading', pinned, pct: j.total ? Math.min(1, j.loaded / j.total) : null, bytes: bytes ?? (j.total || null) })
    } else {
      out.set(id, { state: have.has(video) ? 'cached' : 'none', pinned, pct: null, bytes })
    }
  }
  return out
}

// ── 存储配额 ────────────────────────────────────────────────────────────
/** 整个站点的用量与配额（浏览器给的估值，含 HTTP 缓存与 IndexedDB）。问不到返回 null。 */
export async function storageEstimate() {
  try {
    const e = await globalThis.navigator?.storage?.estimate?.()
    if (!e) return null
    return { usage: Number(e.usage) || 0, quota: Number(e.quota) || 0 }
  } catch {
    return null
  }
}

/**
 * 申请「持久存储」：没有它的话浏览器在空间紧张时可以不打招呼地清掉整个站点的缓存，
 * 用户固定的那些也不例外。先问 `persisted()`：已经是了就不再打扰（Firefox 会弹窗）。
 */
export async function requestPersist() {
  const st = globalThis.navigator?.storage
  if (!st?.persist) return false
  try {
    if (await st.persisted?.()) return true
    return Boolean(await st.persist())
  } catch {
    return false
  }
}

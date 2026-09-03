/**
 * 登录后的后台预取：把视频与缩略图提前拉进 Cache Storage，命中照片时**立刻**能播。
 *
 * ## 为什么值得做
 *
 * 视频是扫描路径上唯一的大件（单条上限 16.24MiB）。婚礼现场的网络是最差情况 ——
 * 几十个人挤同一个 AP，而视频恰恰在「认出来了」那个最有仪式感的瞬间才开始下载。
 * 宾客在进门等位时打开页面登录，那段安静的时间正好把他那几段视频拉完。
 *
 * ## 取多少：按**空间**，不是按张数
 *
 * 上一版的策略是「宾客全取、管理员取最新 8 张」。两头都不对：
 *
 * - 张数与占用没有关系。8 段可能是 20MB 也可能是 130MB。
 * - 宾客那一头**根本没有上限**。授权 30 张就下 30 段，写满配额之后
 *   `cache.put` 会开始失败 —— 而那是**无声**的，表现只是"有的视频还是要等"。
 *
 * 现在两种角色共用同一条路：预算由 [budget] 按浏览器给的配额算，照片按
 * `createdAt` **倒序**遍历，累加到装不下为止。
 *
 * ### 「最新入库的替换最老的」是淘汰的自然结果
 *
 * 这一点值得显式写下来：计划本身就是"最新的、装得下的那些"，被预算挤出去的老条目
 * 自然落在计划之外，而 [staleKeys] 下一轮就把它们删了。**不需要单独写一个淘汰器** ——
 * 多写一个就多一处"两边算的不是同一个集合"的可能。
 *
 * ## 为什么缓存键是流地址而不是票据地址
 *
 * 播放走的是票据（`/api/stream/<票>`，一次性），拿它当键的话永远匹配不上第二次。
 * 流地址（`/v1/asset/<id>/stream`)是**稳定**的：换视频会换 asset id、也就换了地址，
 * 所以旧缓存自然失效，不需要主动作废逻辑。预取用 `fetch(credentials)` 直连流地址 ——
 * 票据体系是为 `<video>` 标签发明的（它带不了 HttpOnly cookie），`fetch` 没有那个毛病。
 *
 * ## 为什么不用 Service Worker
 *
 * `<video src>` 不查 Cache Storage，正统做法是 SW 拦截。但这个项目的播放**本来就不走**
 * `<video src>`（安卓平台媒体组件的两个坑，见 mp4stream.js 顶部那张表）—— 它走页面自己
 * 的 `fetch` + MediaSource。所以只需要在那条 fetch 之前先问一句 Cache Storage
 * （`cachedStream`），SW 的整套生命周期一个都不用背。
 *
 * 缩略图那一半有同样的毛病：`<img src>` 也不查 Cache Storage。所以它走
 * [cachedThumbUrl] —— 命中就换成一个 `blob:` 地址。**调用方必须负责 revoke**，
 * 理由写在那个函数上。
 *
 * ## 克制的部分
 *
 * - 串行下载 + 每段之间歇 300ms：不跟正在跑的扫描抢带宽；
 * - `saveData`（省流量模式）时整个不跑；
 * - 失败静默跳过：预取是优化，它的任何失败都不该打扰界面 —— 但每一步都进 diag，
 *   调试模式下看得到。
 * - 每次会话只跑一遍（登录后触发）。
 */
import * as api from './api.js'
import { diagAlways, short } from './diag.js'
import { mediaInfo } from './mediaload.js'
import { mb } from './ui.js'

export const CACHE_NAME = 'photoar-media-v1'
/**
 * 缩略图单独一个缓存，不与视频混。
 *
 * 混在一起的话「清掉视频腾地方」会把缩略图一起清掉，而那几十 KB 一张的东西正是
 * 媒体页秒开图的全部依据 —— 用最不值钱的空间换最显眼的体验，不该被大件的淘汰波及。
 */
export const THUMB_CACHE_NAME = 'photoar-thumb-v1'

/** 预算取浏览器配额的这个比例。 */
export const BUDGET_FRACTION = 0.25
/** 预算的下限与上限。 */
export const BUDGET_MIN = 64 * 1024 * 1024
export const BUDGET_MAX = 1024 * 1024 * 1024
/** 问不到配额时用这个数。 */
export const BUDGET_FALLBACK = 256 * 1024 * 1024
/** 缩略图那一半的预算：视频预算的这个比例（缩略图几十 KB 一张，够几百张了）。 */
export const THUMB_BUDGET_FRACTION = 0.05

/**
 * 剩余预算低于这个数就不必再问下一段了 —— 转码后的视频没有比这更小的。
 * 它同时是"别为了塞进最后 200KB 而把整条列表问一遍"的止损点。
 */
const BUDGET_FLOOR = 1024 * 1024

let started = false
const status = {
  state: '未开始', planned: 0, done: 0, skipped: 0, failed: 0,
  tooBig: 0, bytes: 0, budget: 0, thumbs: 0,
}

/** 给缓存页显示用的快照。 */
export const prefetchStatus = () => ({ ...status })

/**
 * 这台机器上能给预取用多少空间。
 *
 * **按配额自适应而不是写死 MB**：写死的数字在 32GB 和 512GB 的手机上是同一个，
 * 必然对其中一头是错的（要么把小手机塞满，要么在大手机上白白只用一点点）。
 *
 * `navigator.storage.estimate()` 报的 `quota` 是**整个源**的（含 HTTP 缓存、
 * IndexedDB、我们自己的 Cache Storage），而且各家浏览器给的是个估值 ——
 * 所以只取四分之一，并且两头都钳住。问不到就用一个保守的固定值。
 *
 * 两种角色共用这一个函数，是刻意的：这样"管理员限额"和"宾客别撑爆手机"是同一条
 * 代码，不会出现改了一边忘了另一边。
 */
export async function budget() {
  let quota = 0
  try {
    quota = (await navigator.storage?.estimate?.())?.quota ?? 0
  } catch { /* 有的浏览器在隐私模式下会抛 */ }
  if (!Number.isFinite(quota) || quota <= 0) return BUDGET_FALLBACK
  return clamp(Math.floor(quota * BUDGET_FRACTION), BUDGET_MIN, BUDGET_MAX)
}

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

/**
 * 候选顺序：有视频的，按入库时间**从新到旧**。纯函数，好测。
 *
 * 倒序是「最新入库的替换最老的」这条策略的**唯一实现处** —— 预算在别处切，
 * 而"先来的先占"这件事全靠这里的顺序。
 */
export function pickPlan(photos) {
  return (photos ?? [])
    .filter((p) => p.hasVideo)
    .sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0))
}

/**
 * 按预算从倒序列表里切出装得下的那些。
 *
 * `infoOf(photo)` 返回 `{url, bytes}` 或 null（没视频 / 取不到 / 不能缓存）。
 * 它是异步的，因为大小只有 `/v1/photo/<id>/media` 知道 —— `/v1/photos` 不返回字节数。
 * 注入进来是为了让这条策略能在没有网络的情况下测。
 *
 * ## 装不下的那一段是 `continue` 而不是 `break`
 *
 * 停下来看着更省事，但那样一段异常大的视频排在最前面就会把**后面全部**挡掉 ——
 * 明明还有 60MB 空着，却一段都没缓存。所以跳过它继续试下一段；真正的止损点是
 * 剩余预算低于 [BUDGET_FLOOR]，那时后面不可能再有装得下的。
 */
export async function planWithinBudget(photos, limitBytes, infoOf) {
  const items = []
  let used = 0
  let tooBig = 0
  for (const p of pickPlan(photos)) {
    if (limitBytes - used < BUDGET_FLOOR) break
    let info = null
    try {
      info = await infoOf(p)
    } catch { /* 取不到就当没有，预取的任何一步失败都不该打扰界面 */ }
    if (!info?.url || !Number.isFinite(info.bytes) || info.bytes <= 0) continue
    if (used + info.bytes > limitBytes) { tooBig++; continue }
    used += info.bytes
    items.push({ photo: p, url: info.url, bytes: info.bytes })
  }
  return { items, used, tooBig }
}

/**
 * 现有缓存键里已经不在计划里的那些 —— 该删。纯函数，好测。
 *
 * 多出来的有三种，删法相同：授权被撤、视频被换（换视频会换 asset id）、
 * 以及**被预算挤出去的老条目**。最后那一种就是「最新替换最老」的落地方式。
 */
export function staleKeys(existingPaths, wantedPaths) {
  const want = new Set(wantedPaths)
  return existingPaths.filter((p) => !want.has(p))
}

/**
 * 预取缓存里有这段视频吗。命中返回 Response（每次 match 都是新的一份，可直接消费），
 * 未命中或环境不支持返回 null —— 调用方退回票据那条路，行为与没有预取时完全一样。
 */
export async function cachedStream(streamPath) {
  return await matchIn(CACHE_NAME, streamPath)
}

async function matchIn(cacheName, path) {
  if (!globalThis.caches || !path) return null
  try {
    const c = await caches.open(cacheName)
    return (await c.match(path)) ?? null
  } catch {
    return null
  }
}

/**
 * 本机那份缩略图，换成一个能直接喂给 `<img src>` 的 `blob:` 地址。命中不了返回 null。
 *
 * **调用方必须在卸载时 `URL.revokeObjectURL`** —— 不 revoke 的话那份解码后的图会一直
 * 挂在文档上，媒体页来回进出几十次就是几十份。这个责任没法由这里承担：它不知道
 * 那个地址被用到什么时候。
 */
export async function cachedThumbUrl(photoId) {
  const res = await matchIn(THUMB_CACHE_NAME, api.thumbUrl(photoId))
  if (!res) return null
  try {
    return URL.createObjectURL(await res.blob())
  } catch {
    return null
  }
}

/** 预取了几段。给缓存页显示。 */
export async function prefetchedCount() {
  if (!globalThis.caches) return 0
  try {
    const c = await caches.open(CACHE_NAME)
    return (await c.keys()).length
  } catch {
    return 0
  }
}

/** 清空预取缓存（视频与缩略图都清）。给缓存页的按钮。 */
export async function clearPrefetched() {
  if (!globalThis.caches) return
  await caches.delete(CACHE_NAME)
  await caches.delete(THUMB_CACHE_NAME)
  status.state = '已清空'
  status.planned = status.done = status.skipped = status.failed = 0
  status.tooBig = status.bytes = status.thumbs = 0
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/**
 * 登录后调一次。立即返回，工作在后台慢慢做。
 *
 * `isAdmin` **不再影响取多少** —— 预算对两种角色是同一套（见 [budget]）。留着这个参数
 * 只为了写进 diag：排查"这台手机怎么没预取"时，第一件要确认的事仍然是"他是谁"。
 *
 * @param opts.delayMs 起跑前先让路（默认 4s：让扫描页的引擎与相机先就位）
 */
export function startPrefetch({ isAdmin, delayMs = 4000 } = {}) {
  if (started) return
  started = true
  if (!globalThis.caches) {
    status.state = '此环境不支持（无 Cache Storage）'
    return
  }
  if (navigator.connection?.saveData) {
    status.state = '省流量模式，跳过'
    diagAlways('预取：saveData 开着，不跑')
    return
  }
  setTimeout(() => {
    run(Boolean(isAdmin)).catch((e) => {
      status.state = `失败：${e?.message ?? e}`
      diagAlways(`预取整体失败（不影响使用）：${e?.message ?? e}`)
    })
  }, delayMs)
}

async function run(isAdmin) {
  status.state = '取照片列表…'
  const photos = await api.photos()
  const limit = await budget()
  status.budget = limit
  diagAlways(`预取：预算 ${mb(limit)}MB（${isAdmin ? '管理员' : '宾客'}，两种角色同一套）`)

  // 缩略图先做：它便宜（几十 KB 一张）而且回报最直接 —— 媒体页立刻有图。
  // 放在视频之前是因为视频那一步可能要几分钟，而用户多半在那期间就点开媒体页了。
  await prefetchThumbs(photos, Math.floor(limit * THUMB_BUDGET_FRACTION))

  status.state = '算计划…'
  const { items, used, tooBig } = await planWithinBudget(photos, limit, async (p) => {
    const info = await mediaInfo(p.photoId ?? p.id)
    // `absolute` 的（网盘直链）不缓存：那种地址十几分钟就失效，缓存下来的是一份
    // 过期的重定向而不是视频。`missing` 的更不用说。
    if (!info?.url || info.missing || info.absolute) return null
    return { url: info.url, bytes: Number(info.bytes) }
  })
  status.planned = items.length
  status.tooBig = tooBig
  status.bytes = used
  diagAlways(`预取：计划 ${items.length} 段 / ${mb(used)}MB` +
    (tooBig ? `，${tooBig} 段装不下被跳过` : ''))

  const cache = await caches.open(CACHE_NAME)
  for (const it of items) {
    const pid = it.photo.photoId ?? it.photo.id
    try {
      if (await cache.match(it.url)) {
        status.skipped++
        continue
      }
      status.state = `下载中 ${status.done + 1}/${items.length}`
      const res = await fetch(it.url, { credentials: 'same-origin' })
      if (!res.ok) {
        status.failed++
        diagAlways(`预取 ${short(pid)} 失败：HTTP ${res.status}`)
        continue
      }
      await cache.put(it.url, res)
      status.done++
      diagAlways(`预取 ${short(pid)} 完成（${mb(it.bytes)}MB）`)
    } catch (e) {
      // 配额写满时 `cache.put` 抛的是 QuotaExceededError。预算算错了才会走到这里 ——
      // 说出来，否则表现只是"有几段还是要等"，而那与网络慢分不开。
      status.failed++
      diagAlways(`预取 ${short(pid)} 失败：${e?.name === 'QuotaExceededError' ? '浏览器配额写满了' : e?.message ?? e}`)
    }
    await sleep(300)
  }

  // 清掉不在计划里的旧条目：撤了授权 / 换了视频 / **被预算挤出去的老的**。
  try {
    const keys = await cache.keys()
    const stale = staleKeys(keys.map((r) => new URL(r.url).pathname), items.map((i) => i.url))
    for (const path of stale) await cache.delete(path)
    if (stale.length) diagAlways(`预取：清掉 ${stale.length} 段（授权变了、换了视频、或被更新的挤出去）`)
  } catch { /* 清不掉就下次再清，不值得报错 */ }

  status.state = `完成：新取 ${status.done}，已有 ${status.skipped}` +
    (status.failed ? `，失败 ${status.failed}` : '') +
    (status.tooBig ? `，${status.tooBig} 段超预算` : '')
  diagAlways(`预取${status.state}`)
}

/**
 * 缩略图全量预取。
 *
 * **不跟视频抢那份预算**（它有自己的一小份），也不因为超了就停整个预取 ——
 * 几十 KB 一张的东西，几百张才吃掉视频预算的 5%。
 */
async function prefetchThumbs(photos, limitBytes) {
  let cache
  try {
    cache = await caches.open(THUMB_CACHE_NAME)
  } catch {
    return
  }
  const wanted = []
  let used = 0
  status.state = '取缩略图…'
  for (const p of photos ?? []) {
    const pid = p.photoId ?? p.id
    if (!pid) continue
    const url = p.refThumbUrl ?? api.thumbUrl(pid)
    wanted.push(url)
    if (used >= limitBytes) continue
    try {
      if (await cache.match(url)) { status.thumbs++; continue }
      const res = await fetch(url, { credentials: 'same-origin' })
      if (!res.ok) continue
      // clone 一份来量大小：`put` 会消费掉 body，量完再 put 就是一个已经读干净的流。
      const size = Number(res.headers.get('content-length')) || 0
      await cache.put(url, res.clone())
      used += size
      status.thumbs++
    } catch { /* 单张失败无所谓，媒体页会退回网络地址 */ }
  }
  try {
    const keys = await cache.keys()
    for (const path of staleKeys(keys.map((r) => new URL(r.url).pathname), wanted)) {
      await cache.delete(path)
    }
  } catch { /* 同上 */ }
  diagAlways(`预取：缩略图 ${status.thumbs} 张（${mb(used)}MB）`)
}

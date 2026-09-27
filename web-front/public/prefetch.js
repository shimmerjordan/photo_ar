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
 * ### 固定的组先扣掉，再按新到旧装剩下的
 *
 * 用户点过「缓存」的组（mediastore 的 pin）是他明说「一定要在本机」的：它们**不进**
 * 按新到旧的计划、也**永不**被这里自动删。预算 = 总预算 − 固定组的视频大小，剩下的
 * 才给自动计划。固定的超过了总预算也照样保留 —— 那是用户的决定，自动部分就是 0。
 *
 * ### 「最新入库的替换最老的」是淘汰的自然结果
 *
 * 这一点值得显式写下来：计划本身就是"最新的、装得下的那些"，被预算挤出去的老条目
 * 自然落在计划之外，而清理（[keepSet] 算出该留的，其余删）把它们删了。**不需要单独写
 * 一个淘汰器** —— 多写一个就多一处"两边算的不是同一个集合"的可能。
 *
 * ## 为什么缓存键是流地址而不是票据地址
 *
 * 播放时真正发出去的常是票据地址（`/api/stream/<票>`）。票 10 分钟内有效、可以重复用、
 * 也不绑来源 —— 但**每换一次就是一个新地址**，拿它当键的话，下一次换来的票永远匹配
 * 不上这一次存下的。流地址（`/v1/asset/<id>/stream`）是**稳定**的：换视频会换 asset id、
 * 也就换了地址，所以旧缓存自然失效，不需要主动作废逻辑。至于字节实际从哪来（整站数据源
 * 换票直取 / 默认源带 cookie），那是 netsrc.js 与 mediastore.js 的事，键始终是这个流地址。
 *
 * ## 下载交给 mediastore，不自己 fetch
 *
 * 上一版预取自己 `fetch` + `cache.put`，于是预取正在下 A 时扫到 A，播放又下一遍
 * （设计 §1 的根因之二）。现在每一段都是 `mediastore.download(key, BACKGROUND)`：
 * 播放已经在下的就挂上去等它，预取正在下的被播放要到时就地提升成播放优先级。
 * 缩略图例外，见 [prefetchThumbs]。
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
 * - 后台优先级 + 一段下完才排下一段、段间歇 300ms：播放与用户手动缓存在跑的时候
 *   后台任务不开（mediastore 的调度），不跟正在跑的扫描抢带宽；
 * - `saveData`（省流量模式）时整个不跑；
 * - 失败静默跳过：预取是优化，它的任何失败都不该打扰界面 —— 但每一步都进 diag，
 *   调试模式下看得到。
 * - 每次会话只跑一遍（登录后触发）。
 */
import * as api from './api.js'
import { diagAlways, short } from './diag.js'
import { mediaInfo } from './mediaload.js'
import {
  Priority, REF_CACHE, STORAGE_FULL, THUMB_CACHE, VIDEO_CACHE,
  activeJobs, cached, cachedKeySet, clearCache, download, groupKeys, isPinned, jobFor, pinnedIds,
  refKey, removeKey, thumbKey, videoKey,
} from './mediastore.js'
import { mb } from './ui.js'

export const CACHE_NAME = VIDEO_CACHE
/**
 * 缩略图单独一个缓存，不与视频混。
 *
 * 混在一起的话「清掉视频腾地方」会把缩略图一起清掉，而那几十 KB 一张的东西正是
 * 媒体页秒开图的全部依据 —— 用最不值钱的空间换最显眼的体验，不该被大件的淘汰波及。
 */
export const THUMB_CACHE_NAME = THUMB_CACHE

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
  tooBig: 0, bytes: 0, budget: 0, thumbs: 0, pinned: 0,
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
const pidOf = (p) => String(p?.photoId ?? p?.id ?? '')

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
 * 它可以是异步的：新服务端的 `/v1/photos` 每行直接带 `videoAssetId` / `videoBytes`，
 * 老服务端只有 `/v1/photo/<id>/media` 知道大小（见 [run] 里的 `infoOf`）。
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

const videoKeyOf = (p) => (p?.videoAssetId ? videoKey(p.videoAssetId) : null)

/**
 * 清理时该**留下**的键。纯函数，**清理口径的唯一实现处**（视频与原图两个缓存都用它）。
 *
 * 留三种：当前用户授权清单里被固定的组、这一轮的自动计划、正在下的任务。其余一律删。
 *
 * - 固定记录要与 `photos`（当前用户能看的那些）**取交集**：固定过但已经看不到的
 *   （撤了授权，或者是上一个人留下的）不算。换人登录同一台手机时，缓存里不该留
 *   上一个人才有权限的视频 —— 这条就是那句话的实现。
 * - 正在下的必须留：它可能是用户刚点的「缓存」、或者正在播放的那段，删了它的旧条目
 *   不要紧，但不能在它写进去之后的下一轮之前把它当成"多出来的"。
 *
 * @param keyOf 从一张照片取要留的键。默认取视频键；原图缓存传 `(p) => refKey(p.photoId)`。
 */
export function keepSet({ photos = [], pinned = new Set(), planKeys = [], running = [], keyOf = videoKeyOf } = {}) {
  const keep = new Set([...planKeys, ...running])
  for (const p of photos ?? []) {
    if (!pinned.has(pidOf(p))) continue
    const k = keyOf(p)
    if (k) keep.add(k)
  }
  return keep
}

/**
 * 预取缓存里有这段视频吗。命中返回 Response（每次 match 都是新的一份，可直接消费），
 * 未命中或环境不支持返回 null —— 调用方退回票据那条路，行为与没有预取时完全一样。
 */
export async function cachedStream(streamPath) {
  return await cached(streamPath, VIDEO_CACHE)
}

/**
 * 本机那份缩略图，换成一个能直接喂给 `<img src>` 的 `blob:` 地址。命中不了返回 null。
 *
 * **调用方必须在卸载时 `URL.revokeObjectURL`** —— 不 revoke 的话那份解码后的图会一直
 * 挂在文档上，媒体页来回进出几十次就是几十份。这个责任没法由这里承担：它不知道
 * 那个地址被用到什么时候。
 */
export async function cachedThumbUrl(photoId) {
  const res = await cached(thumbKey(photoId), THUMB_CACHE)
  if (!res) return null
  try {
    return URL.createObjectURL(await res.blob())
  } catch {
    return null
  }
}

/** 预取了几段。给缓存页显示。 */
export async function prefetchedCount() {
  return (await cachedKeySet(VIDEO_CACHE)).size
}

/**
 * 清空预取缓存（视频与缩略图整个清掉，**连固定的组一起**）。现在只给缓存页的「清空全部」
 * 用 —— 它会先停掉正在跑的任务、再清固定记录。原来单独的「清空预取」按钮已经删掉：
 * 直接调它会连用户点过「缓存」的组一起删，「只清自动的」请走 `prefetch.keepSet` 那个口径。
 */
export async function clearPrefetched() {
  await clearCache(VIDEO_CACHE)
  await clearCache(THUMB_CACHE)
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
 * 固定记录按用户读（mediastore 的 `setUser`），所以**调这个之前必须先 `setUser`** ——
 * app.js 在拿到 `me` 之后、装外壳之前做了。
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
    diagAlways('预取：saveData 开着，不下载，只清掉这个人看不到的')
    // 省流量也得清：换人登录同一台手机时，缓存里不能留上一个人才有权限的视频。
    // 但只删「当前授权清单之外」的 —— 按预算挤掉还看得到的那些，在省流量模式下等于逼他重下。
    setTimeout(() => {
      api.photos()
        .then((photos) => cleanup(photos, photos.map(videoKeyOf).filter(Boolean)))
        .catch(() => { /* 同 run：清不掉下次再清 */ })
    }, delayMs)
    return
  }
  setTimeout(() => {
    run(Boolean(isAdmin)).catch((e) => {
      status.state = `失败：${e?.message ?? e}`
      diagAlways(`预取整体失败（不影响使用）：${e?.message ?? e}`)
    })
  }, delayMs)
}

/**
 * 一张照片的视频键与大小。新服务端直接从 `/v1/photos` 的行上拿（不必逐张打 `/media`）；
 * 老服务端没有这两个字段，退回原来那条 `mediaInfo`。
 */
async function infoOf(p) {
  if (p.videoAssetId) return { url: videoKey(p.videoAssetId), bytes: Number(p.videoBytes) }
  const info = await mediaInfo(pidOf(p))
  // `absolute` 的（网盘直链）不缓存：那种地址十几分钟就失效，缓存下来的是一份
  // 过期的重定向而不是视频。`missing` 的更不用说。
  if (!info?.url || info.missing || info.absolute) return null
  return { url: info.url, bytes: Number(info.bytes) }
}

/**
 * 仅测试用：直接跑一轮 [run]。`startPrefetch` 每页只跑一次、还隔着起跑延迟，
 * 测不了循环本身（「每段下载前现查」那两条就钉在循环里）。
 */
export const _runForTest = (isAdmin = false) => run(isAdmin)

async function run(isAdmin) {
  status.state = '取照片列表…'
  const photos = await api.photos()
  const limit = await budget()
  // 固定记录与当前用户的授权清单取交集：看不到了的固定不占预算、也不下。
  const pins = pinnedIds()
  const pinnedPhotos = photos.filter((p) => pins.has(pidOf(p)))
  const pinnedIdSet = new Set(pinnedPhotos.map(pidOf))
  const pinnedBytes = pinnedPhotos.reduce((n, p) => n + (Number(p.videoBytes) || 0), 0)
  const autoLimit = Math.max(0, limit - pinnedBytes)
  status.budget = limit
  status.pinned = pinnedPhotos.length
  diagAlways(`预取：预算 ${mb(limit)}MB（${isAdmin ? '管理员' : '宾客'}，两种角色同一套）` +
    (pinnedPhotos.length ? `，固定 ${pinnedPhotos.length} 组占 ${mb(pinnedBytes)}MB，自动部分剩 ${mb(autoLimit)}MB` : ''))

  status.state = '算计划…'
  const { items, used, tooBig } = await planWithinBudget(
    photos.filter((p) => !pinnedIdSet.has(pidOf(p))), autoLimit, infoOf)
  status.planned = items.length
  status.tooBig = tooBig
  status.bytes = used + pinnedBytes
  diagAlways(`预取：计划 ${items.length} 段 / ${mb(used)}MB` +
    (tooBig ? `，${tooBig} 段装不下被跳过` : ''))

  // 清理放在**下载之前**：换人登录时上一个人的视频越早删越好，而且腾出来的空间正是
  // 接下来要写的。计划已经算好了，删的口径不依赖下载结果。
  await cleanup(photos, items.map((i) => i.url))

  // 缩略图在视频之前：它便宜（几十 KB 一张）而且回报最直接 —— 媒体页立刻有图。
  // 视频那一步可能要几分钟，而用户多半在那期间就点开媒体页了。
  await prefetchThumbs(photos, Math.floor(limit * THUMB_BUDGET_FRACTION))

  // 固定组在前（用户明说要的，原图 + 视频），自动计划在后。
  const queue = []
  for (const p of pinnedPhotos) {
    const k = groupKeys(p)
    queue.push({ key: k.ref, cacheName: REF_CACHE, photo: p, pinned: true })
    if (k.video) queue.push({ key: k.video, cacheName: VIDEO_CACHE, photo: p, pinned: true })
  }
  for (const it of items) queue.push({ key: it.url, cacheName: VIDEO_CACHE, photo: it.photo, pinned: false })

  for (let n = 0; n < queue.length; n++) {
    const { key, cacheName, photo, pinned } = queue[n]
    const pid = pidOf(photo)
    // 下面两条都是**每段下载前现查**，不在进循环前拍快照：这个循环一跑就是好几分钟，期间
    // 用户可能扫到计划里的某段（播放下完、写进缓存、任务随即从表里删掉）、点了「缓存」、
    // 或者把一个固定组「从本机移除」。拿开头那份快照判断的话，前两种会被预取再下一遍
    // （Review Focus 3 的退化），最后一种会被下回缓存 —— 用户刚删的又出现了。
    if (pinned && !isPinned(pid)) {
      diagAlways(`预取 ${short(pid)}：运行中被取消固定了，不下`)
      continue
    }
    // 有任务在跑（播放 / 手动缓存正在下这段）就不问缓存，直接交给下面的 `download` 挂上去
    // 等它下完：这样后台不会趁它还在下就开下一段，跟它抢带宽。
    if (!jobFor(key) && await cached(key, cacheName)) {
      status.skipped++
      continue
    }
    status.state = `下载中 ${n + 1}/${queue.length}`
    // 播放或手动缓存已经在下这一段的话，`download` 返回的就是那个任务 —— 挂上去等它，不下第二遍。
    const job = download(key, { cacheName, priority: Priority.BACKGROUND })
    await job.done
    if (job.state !== 'done') {
      status.failed++
      diagAlways(`预取 ${short(pid)} 失败：${job.error ?? job.state}`)
    } else if (job.cacheFailed) {
      // 配额写满时 `cache.put` 抛的是 QuotaExceededError。预算算错了才会走到这里 ——
      // 说出来，否则表现只是"有几段还是要等"，而那与网络慢分不开。写满了就停手：
      // 后面每一段都会下完再被拒，白白花流量。
      status.failed++
      diagAlways(`预取 ${short(pid)} 下完了但没存下：${job.cacheFailed}`)
      if (job.cacheFailed === STORAGE_FULL) {
        diagAlways('预取：浏览器存储写满了，剩下的不下了')
        break
      }
    } else {
      status.done++
      diagAlways(`预取 ${short(pid)} 完成（${mb(job.loaded)}MB，via=${job.source}）`)
    }
    await sleep(300)
  }

  status.state = `完成：新取 ${status.done}，已有 ${status.skipped}` +
    (status.failed ? `，失败 ${status.failed}` : '') +
    (status.tooBig ? `，${status.tooBig} 段超预算` : '') +
    (status.pinned ? `，固定 ${status.pinned} 组` : '')
  diagAlways(`预取${status.state}`)
}

/**
 * 删掉不该留的：视频缓存留「固定 ∪ 计划 ∪ 正在下」，原图缓存只留固定的（口径见 [keepSet]）。
 *
 * 固定记录与正在跑的任务都在这一刻**现取**，不用 `run` 开头那份：从开头到这里可能已经
 * 过了几秒到几十秒（老服务端要逐张问 `/media`），期间用户刚点的「缓存」不能被当成多余的删掉。
 */
async function cleanup(photos, planKeys) {
  const pinned = pinnedIds()
  const running = activeJobs().map((j) => j.key)
  const sweep = async (cacheName, keep) => {
    const stale = staleKeys([...await cachedKeySet(cacheName)], [...keep])
    for (const k of stale) await removeKey(k, cacheName)
    return stale.length
  }
  try {
    const v = await sweep(VIDEO_CACHE, keepSet({ photos, pinned, planKeys, running }))
    const r = await sweep(REF_CACHE, keepSet({ photos, pinned, running, keyOf: (p) => refKey(pidOf(p)) }))
    if (v || r) diagAlways(`预取：清掉 ${v} 段视频、${r} 张原图（授权变了、换了视频、取消了固定、或被更新的挤出去）`)
  } catch { /* 清不掉就下次再清，不值得报错 */ }
}

/**
 * 缩略图全量预取。
 *
 * **不跟视频抢那份预算**（它有自己的一小份），也不因为超了就停整个预取 ——
 * 几十 KB 一张的东西，几百张才吃掉视频预算的 5%。
 *
 * **不走 mediastore / 整站数据源**，就是同源直取：几十 KB 的东西，经数据源要先在隧道上
 * 换一张票（一次往返），换票的那一下就比直接取回来还慢，数据源在这里只会添麻烦。
 */
async function prefetchThumbs(photos, limitBytes) {
  let cache
  try {
    cache = await caches.open(THUMB_CACHE)
  } catch {
    return
  }
  const wanted = []
  let used = 0
  status.state = '取缩略图…'
  for (const p of photos ?? []) {
    const pid = pidOf(p)
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

/**
 * 登录后的后台预取：把视频提前拉进 Cache Storage，命中照片时**立刻**能播。
 *
 * ## 为什么值得做
 *
 * 视频是扫描路径上唯一的大件（单条上限 16.24MiB）。婚礼现场的网络是最差情况 ——
 * 几十个人挤同一个 AP，而视频恰恰在「认出来了」那个最有仪式感的瞬间才开始下载。
 * 宾客在进门等位时打开页面登录，那段安静的时间正好把他那几段视频拉完。
 *
 * ## 谁取多少（这是这个模块唯一的策略点）
 *
 * - **宾客（viewer）：全部。** 他的授权集就是他会扫到的那几张（典型是个位数），
 *   全取也就是几十 MB。
 * - **管理员：最新 N 张。** 他看得到全库（可能上千段视频），全取既装不下也没意义 ——
 *   管理员扫的基本是刚入库的那几张（在验刚配的视频）。
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
 * ## 克制的部分
 *
 * - 串行下载 + 每段之间歇 300ms：不跟正在跑的扫描抢带宽；
 * - `saveData`（省流量模式）时整个不跑；
 * - 失败静默跳过：预取是优化，它的任何失败都不该打扰界面 —— 但每一步都进 diag，
 *   调试模式下看得到。
 * - 每次会话只跑一遍（登录后触发）。
 */
import * as api from './api.js'
import { diagAlways } from './diag.js'

export const CACHE_NAME = 'photoar-media-v1'

/** 管理员预取最新几张。宾客不看这个数（全取）。 */
export const ADMIN_LIMIT = 8

let started = false
const status = { state: '未开始', planned: 0, done: 0, skipped: 0, failed: 0 }

/** 给缓存页显示用的快照。 */
export const prefetchStatus = () => ({ ...status })

/**
 * 从照片列表挑出要预取的。纯函数，好测。
 *
 * @param photos `/v1/photos` 的行（要 `hasVideo` / `createdAt`）
 * @param isAdmin 管理员只取最新 `limit` 张 —— 他看得到全库，全取装不下也没意义
 */
export function pickPlan(photos, isAdmin, limit = ADMIN_LIMIT) {
  const withVideo = (photos ?? []).filter((p) => p.hasVideo)
  if (!isAdmin) return withVideo
  return [...withVideo]
    .sort((a, b) => (b.createdAt ?? 0) - (a.createdAt ?? 0))
    .slice(0, limit)
}

/**
 * 现有缓存键里已经不在计划里的那些 —— 该删。纯函数，好测。
 *
 * 宾客：计划=全部授权，多出来的 = 授权被撤或视频被换（换视频会换 asset id）。
 * 管理员：计划=最新 N，多出来的是更老的 —— 删掉正好把占用钉在 N 段以内。
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
  if (!globalThis.caches) return null
  try {
    const c = await caches.open(CACHE_NAME)
    return (await c.match(streamPath)) ?? null
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

/** 清空预取缓存。给缓存页的按钮。 */
export async function clearPrefetched() {
  if (!globalThis.caches) return
  await caches.delete(CACHE_NAME)
  status.state = '已清空'
  status.planned = status.done = status.skipped = status.failed = 0
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

/**
 * 登录后调一次。立即返回，工作在后台慢慢做。
 *
 * @param opts.isAdmin 决定取全部还是最新 N
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
  const plan = pickPlan(photos, isAdmin)
  status.planned = plan.length
  diagAlways(`预取：计划 ${plan.length} 段（${isAdmin ? `管理员，最新 ${ADMIN_LIMIT}` : '宾客，全部'}）`)
  const cache = await caches.open(CACHE_NAME)
  const wanted = []

  for (const p of plan) {
    const pid = p.photoId ?? p.id
    try {
      // 顺手把缩略图捂热（HTTP 缓存，照片库页立刻有图）。它不进 Cache Storage ——
      // `<img>` 不查那里，走浏览器自己的 HTTP 缓存才有用。
      if (p.refThumbUrl) fetch(p.refThumbUrl, { credentials: 'same-origin' }).catch(() => {})

      const info = await api.mediaOfPhoto(pid)
      if (!info?.url || info.missing || info.absolute) continue
      wanted.push(info.url)
      if (await cache.match(info.url)) {
        status.skipped++
        continue
      }
      status.state = `下载中 ${status.done + 1}/${plan.length}`
      const res = await fetch(info.url, { credentials: 'same-origin' })
      if (!res.ok) {
        status.failed++
        diagAlways(`预取 ${pid?.slice(0, 8)} 失败：HTTP ${res.status}`)
        continue
      }
      await cache.put(info.url, res)
      status.done++
      diagAlways(`预取 ${pid?.slice(0, 8)} 完成（${info.bytes ?? '?'} 字节）`)
    } catch (e) {
      status.failed++
      diagAlways(`预取 ${pid?.slice(0, 8)} 失败：${e?.message ?? e}`)
    }
    await sleep(300)
  }

  // 清掉不在计划里的旧条目（撤了授权 / 换了视频 / 管理员滚出最新 N 之外的）。
  try {
    const keys = await cache.keys()
    const stale = staleKeys(keys.map((r) => new URL(r.url).pathname), wanted)
    for (const path of stale) await cache.delete(path)
    if (stale.length) diagAlways(`预取：清掉 ${stale.length} 段过期缓存`)
  } catch { /* 清不掉就下次再清，不值得报错 */ }

  status.state = `完成：新取 ${status.done}，已有 ${status.skipped}` +
    (status.failed ? `，失败 ${status.failed}` : '')
  diagAlways(`预取${status.state}`)
}

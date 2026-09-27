/**
 * 「全部缓存」：把一批组**串行**交给 `mediastore.cacheGroup`，可以中途停。
 *
 * ## 为什么是一个模块级的单例，而不是各页自己写一个循环
 *
 * 媒体页顶上和缓存页底部各有一个「全部缓存」。两页各跑各的循环会出三种怪事：
 * 在媒体页点了、切到缓存页看进度，那边的按钮不知道已经在跑，再点一次就开了第二批；
 * 两批的「停止」各停各的；离开媒体页时循环要么跟着卸载停掉（用户以为还在下），要么
 * 变成一个没人能停的孤儿。所以批次只有一个，归这里管；页面只订阅它（`onBulkChange`）、
 * 按它画按钮（「全部缓存」↔「停止」）。**离开页面不停**：单张的「缓存」离开页面也照样
 * 下完（任务在 mediastore 里），全部缓存是同一件事的批量版。
 *
 * ## 为什么串行
 *
 * mediastore 给用户档留了两个并发槽，而一组是原图 + 视频两个任务：一次交一组正好占满。
 * 一次全交出去的话几十个任务挤在同一档里按排队顺序下，同一组的原图和视频不保证挨着 ——
 * 半路停掉（或者写满）时留下的是一堆半组。一组一组来，停下时已经做完的每一组都是完整的。
 *
 * ## 「停止」停的是还没开始的那些
 *
 * 正在下的那组照常下完：它多半已经下了一半，丢掉是白下；而且它的任务可能正被播放器
 * 或预取共用着（mediastore 的去重），这里没有资格替它们取消。
 *
 * ## 写满就收手
 *
 * `cacheGroup` 报「浏览器存储空间不够」之后，后面每一组都会先完整下完、再在写缓存那一步
 * 被拒 —— 接着跑只是白白耗流量。所以这一档直接停，结果里带 `full: true` 让页面说清楚。
 */
import { STORAGE_FULL, cacheGroup as storeCacheGroup, storageEstimate } from './mediastore.js'
import { cacheSummary, singleNeed, spaceShort } from './cachelabel.js'
import { bytes, confirmDanger } from './ui.js'

const idOf = (p) => String(p?.photoId ?? p?.id ?? '')

/** 正在跑的那一批；null = 没在跑。 */
let cur = null
/** 上一批的结果（跑完之后页面据此说一句「缓存了 N 组，M 组没成」）。 */
let last = null
const listeners = new Set()

/**
 * 页面画按钮用的快照。`finished` = 已经结束（成功 + 失败）的组数，`currentId` = 正在做的那组。
 * 不在跑时只有 `{running:false, last}`。
 */
export function bulkState() {
  if (!cur) return { running: false, last }
  const { total, done, failed, stopping, currentId } = cur
  return { running: true, total, done, failed, finished: done + failed, stopping, currentId, last }
}

export function onBulkChange(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function emit() {
  const s = bulkState()
  for (const fn of [...listeners]) {
    try { fn(s) } catch { /* 一个页面的监听炸了不该连累这一批 */ }
  }
}

/** 停：正在做的那组做完就收手。没在跑时什么也不做。 */
export function stopCacheAll() {
  if (!cur || cur.stopping) return
  cur.stopping = true
  emit()
}

/**
 * 串行缓存 `photos`。已经有一批在跑就**不开第二批**，直接返回那一批的 promise ——
 * 「全部缓存」按钮在跑的时候本来就换成了「停止」，走到这里只可能是两页各点了一下。
 *
 * 永远 resolve：`{total, done, failed, stopped, full, errors:[{photoId, title, error}]}`。
 *
 * @param opts.cacheGroup 仅测试注入；默认 mediastore 的。
 */
export function cacheAll(photos, { cacheGroup = storeCacheGroup } = {}) {
  if (cur) return cur.promise
  const list = [...(photos ?? [])]
  const run = { total: list.length, done: 0, failed: 0, stopping: false, currentId: null, full: false, errors: [] }
  cur = run
  run.promise = (async () => {
    for (const p of list) {
      if (run.stopping) break
      run.currentId = idOf(p)
      emit()
      let r
      try {
        r = await cacheGroup(p)
      } catch (e) {
        r = { ok: false, error: String(e?.message ?? e) }
      }
      if (r?.ok) {
        run.done++
      } else {
        run.failed++
        run.errors.push({ photoId: idOf(p), title: p?.title || '（未命名）', error: r?.error ?? '没缓存成' })
        if (r?.error === STORAGE_FULL) { run.full = true; break }
      }
    }
    const finished = run.done + run.failed
    last = {
      total: run.total, done: run.done, failed: run.failed,
      stopped: run.stopping && finished < run.total, full: run.full, errors: run.errors,
    }
    cur = null
    emit()
    return last
  })()
  emit()
  return run.promise
}

/**
 * 按钮上的那一下：算出要做哪些组 → 空间预检（不够就先问）→ 开跑。
 *
 * 返回 null = 没有要做的、或者用户在确认框里说了不；否则是 `cacheAll` 的结果。
 * `states` 是页面手上那份 `groupStates` 的结果 —— 用它而不是这里再查一遍，是为了让
 * 「做哪些组」与用户此刻屏幕上看到的状态是同一份。
 */
export async function startCacheAll(photos, states, {
  confirm = confirmDanger, estimate = storageEstimate, cacheGroup = storeCacheGroup,
} = {}) {
  if (cur) return cur.promise
  const sum = cacheSummary(photos, states)
  if (!sum.todo.length) return null
  const short = spaceShort(sum.need, sum.need > 0 ? await estimate() : null)
  if (short) {
    const unknown = sum.needUnknown ? `（另有 ${sum.needUnknown} 组不知道多大）` : ''
    const ok = confirm(`全部缓存大概要 ${bytes(short.need)}${unknown}，浏览器还给这个站点留了 ${bytes(short.left)}，多半存不全。` +
      '\n\n仍然开始？存满之后会自动停下，已经存好的那些不受影响。')
    if (!ok) return null
  }
  return cacheAll(sum.todo, { cacheGroup })
}

/**
 * 单张卡片「缓存」按下去之前的空间预检（fix1 Finding 3：设计 §4 要求点缓存先 estimate，
 * 不够先说 —— 之前只有「全部缓存」做了这一步，单张的完全没做）。与 `startCacheAll` 同一套
 * 依赖注入方式（测试可插桩 `estimate` / `confirm`），算法是 `singleNeed` 那一份；媒体页的
 * 卡片与宾客页 `view.js` 的「本机缓存」木牌都调它，不在两处各写一遍。
 *
 * 返回 true = 可以继续（够、或者问不到配额、或者用户在确认框里说了继续）；
 * false = 用户说了不 —— 调用方应该原样返回，不要发起下载。
 */
export async function precheckSingle(photo, st, { confirm = confirmDanger, estimate = storageEstimate } = {}) {
  const need = singleNeed(photo, st)
  const short = spaceShort(need, await estimate())
  if (!short) return true
  return confirm(`这一组大概要 ${bytes(short.need)}，浏览器还给这个站点留了 ${bytes(short.left)}，多半存不下。` +
    '\n\n仍然缓存？存满之后会自动停下，之前存好的不受影响。')
}

/** 仅测试用：丢掉进行中的批次与上一批的结果。 */
export function _resetForTest() {
  cur = null
  last = null
  listeners.clear()
}

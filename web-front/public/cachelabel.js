/**
 * 「本机缓存」按钮与状态行说什么、点了做什么。**纯函数，不碰 DOM** —— 媒体页的卡片与宾客页的
 * 「本机缓存」木牌共用这一份，node 里能直接测（`test/cachelabel.test.js`）。
 *
 * 输入是 `mediastore.groupStates` 的一项：`{state:'none'|'downloading'|'cached'|'novideo', pinned, pct, bytes}`。
 *
 * ## 为什么「已缓存」分两种
 *
 * 缓存里的一组可能是用户点「缓存」固定下来的，也可能是预取 / 看过之后**自动**存下的。后者
 * 会被预取的清理按空间预算删掉（mediastore.js 顶部「固定」一节），前者永远不会。两种都
 * 写「已缓存」的话，用户以为那组一定在，到了没网的现场却发现被清了 —— 所以自动的那种
 * 单独叫「自动缓存」，点一下是**固定**它（不是重下），固定了的点一下才是移除。
 *
 * ## 后半：木牌那一行的数、「全部缓存」要做哪些组
 *
 * 媒体页顶上与缓存页的「本机缓存」木牌、以及两处的「全部缓存」按钮共用 `cacheSummary`。
 * 放在这里而不是各页自己数，是因为**「全部缓存」做哪些组就是这张表的 `todo`**：两页各数
 * 一遍的话，迟早一边把自动缓存的算进去、另一边不算，同一个按钮在两页上做的不是一件事。
 */
import { bytes } from './ui.js'

/**
 * 按钮：`text` 按钮上的字；`kind` 视觉档（`''` 常规、`'ok'` 已办妥、`'off'` 不可用）；
 * `action` 点了做什么（`'cache'` | `'pin'` | `'remove'` | `null` = 不可点）。
 *
 * 状态还没查出来（`st` 为空）按「没缓存」处理：宁可让人点一下「缓存」（下载任务本身去重，
 * 已经在本机的会直接跳过），也不给一个点不动的按钮。
 */
export function cacheButtonLabel(st) {
  switch (st?.state) {
    case 'novideo': return { text: '无视频', kind: 'off', action: null }
    // 下载中不可点：进度就在按钮上，再点一次既不会更快、也没有「取消」这个意思。
    case 'downloading': return { text: st.pct == null ? '下载中' : `下载 ${Math.round(st.pct * 100)}%`, kind: '', action: null }
    case 'cached': return st.pinned
      ? { text: '已缓存', kind: 'ok', action: 'remove' }
      : { text: '自动缓存', kind: '', action: 'pin' }
    default: return { text: '缓存', kind: '', action: 'cache' }
  }
}

/** 状态行（宾客页木牌里按钮上面那一句）。按钮只有几个字，「会不会被清掉」要在这里说全。 */
export function cacheStateText(st) {
  switch (st?.state) {
    case 'novideo': return '这张没配视频'
    case 'downloading': return st.pct == null ? '下载中' : `下载中 ${Math.round(st.pct * 100)}%`
    case 'cached': return st.pinned
      ? '已缓存（固定在本机，不会被自动清掉）'
      : '已缓存（自动存下的，空间紧张时可能被清掉）'
    default: return '未缓存'
  }
}

const idOf = (p) => String(p?.photoId ?? p?.id ?? '')
const hasVideo = (p) => Boolean(p?.videoAssetId)
const sizeOf = (st) => (Number.isFinite(st?.bytes) && st.bytes > 0 ? st.bytes : null)

/**
 * 原图没有服务端报的大小字段（不像 `videoBytes`），空间预检却少不了它 —— `cacheGroup` 每一组
 * 都会去查原图在不在本机，不在就补一份（fix1 Finding 3）。查得到大小的只有视频，所以原图按
 * 经验值打一个固定的保守常量：宁可把「够不够」估得紧一点，也不要因为完全不算它而让用户以为
 * 够、实际却存不下。
 */
export const REF_BYTES_ESTIMATE = 4 * 1024 * 1024

/**
 * 一屏照片的缓存汇总。`states` 是 `groupStates(photos)` 的结果（`Map<photoId, st>`）。
 *
 * - `total`（M）只数**有视频**的组：没视频的那组没东西可缓存，算进去的话「3 / 5 组」永远到不了头。
 * - `cached`（N）固定的与自动的都算：两种都真的在本机，扫到都秒开。
 * - `bytes` 只加已缓存那些组的视频大小；有几组大小未知（老服务端没给 `videoBytes`）记在
 *   `bytesUnknown`，文案据此写「至少」—— 少报比编一个数强。
 * - `todo` 是「全部缓存」要交给 `cacheGroup` 的那一批：**除了已固定且已缓存的，每一组有视频的**。
 *   自动缓存的也在里面：它会被预取的清理按预算挤掉，而「全部缓存」的意思是「都要在本机」——
 *   过一遍 cacheGroup 就是把它固定下来（视频已在，不重下，只补原图）。下载中的挂上去等它。
 * - `need` 是 todo 那一批要占的空间（空间预检用）：**每一组都打底 `REF_BYTES_ESTIMATE`**
 *   （原图在不在本机没法从 `groupStates` 便宜地查出来，`cacheGroup` 会去补，所以不管视频
 *   缓没缓存都保守算上），视频**还不在本机**的那些再加上它的大小；大小未知的数在 `needUnknown`。
 *
 * 状态还没查出来（`states` 里没有）按没缓存算，理由同 `cacheButtonLabel`。
 */
export function cacheSummary(photos, states) {
  const out = { total: 0, cached: 0, bytes: 0, bytesUnknown: 0, todo: [], need: 0, needUnknown: 0 }
  for (const p of photos ?? []) {
    if (!hasVideo(p)) continue
    const st = states?.get(idOf(p))
    out.total++
    const size = sizeOf(st) ?? (Number(p.videoBytes) > 0 ? Number(p.videoBytes) : null)
    const isCached = st?.state === 'cached'
    if (isCached) {
      out.cached++
      if (size == null) out.bytesUnknown++
      else out.bytes += size
    }
    if (isCached && st.pinned) continue
    out.todo.push(p)
    out.need += REF_BYTES_ESTIMATE
    if (isCached) continue
    if (size == null) out.needUnknown++
    else out.need += size
  }
  return out
}

/**
 * 单张卡片「缓存」按下去要多大空间（空间预检用）：`cacheSummary` 的单组版，同一套算法 ——
 * 原图打底 `REF_BYTES_ESTIMATE`，视频只在还没缓存时再加。媒体页的卡片与宾客页 `view.js` 的
 * 「本机缓存」木牌共用它（配 `cacheall.precheckSingle` 一起用，别把这套算法在两处各写一遍）。
 *
 * 大小未知时那一部分按 0 算（不把 `null` 加进去变成 `NaN`）—— 宁可少估，也不能让单张缓存
 * 因为一个查不到的数而永远弹不出「够不够」的判断。
 */
export function singleNeed(photo, st) {
  const isCached = st?.state === 'cached'
  const size = sizeOf(st) ?? (Number(photo?.videoBytes) > 0 ? Number(photo.videoBytes) : null)
  return REF_BYTES_ESTIMATE + (isCached ? 0 : (size ?? 0))
}

/** 木牌上那一句：`已缓存 N / M 组 · 占用 X`。 */
export function summaryText(sum) {
  if (!sum?.total) return '这些照片都没配视频，没有要缓存的'
  const head = `已缓存 ${sum.cached} / ${sum.total} 组`
  if (!sum.cached) return head
  return `${head} · 占用${sum.bytesUnknown ? '至少' : ''} ${bytes(sum.bytes)}`
}

/**
 * 空间预检：浏览器给这个站点剩下的 < 需要 × 1.1 时返回 `{need, left}`，否则 null。
 *
 * 1.1 是给估值留的余量：`estimate()` 各家都是估的，而写到 99% 时失败的那一段已经下完了。
 * 问不到配额（`est` 为空、`quota` 为 0）时**不挡人**：拦下一个本来存得下的人，比让他试一次更糟 ——
 * 真写满了 mediastore 会报「浏览器存储空间不够」，「全部缓存」也会就此收手。
 */
export function spaceShort(need, est, factor = 1.1) {
  if (!(need > 0) || !est || !(est.quota > 0)) return null
  const left = Math.max(0, est.quota - (est.usage || 0))
  return left < need * factor ? { need, left } : null
}

/**
 * 缓存页每一组那一行的标签。
 *
 * 「固定 · 本机还没有」要单独说：固定了但缓存里没有 = 上次没下完、或者被浏览器清了。
 * 用户以为「固定」就等于「在本机」，到了没网的现场才发现不在 —— 这一行就是提前告诉他。
 */
export function groupTag(st) {
  switch (st?.state) {
    case 'downloading': return st.pct == null ? '下载中' : `下载中 ${Math.round(st.pct * 100)}%`
    case 'cached': return st.pinned ? '固定' : '自动'
    default: return st?.pinned ? '固定 · 本机还没有' : ''
  }
}

/** 缓存页列哪些组：本机有东西的（已缓存 / 下载中）加上被固定的。没沾过本机的不列，免得一页全是空行。 */
export function listedGroups(photos, states) {
  return (photos ?? []).filter((p) => {
    if (!hasVideo(p)) return false
    const st = states?.get(idOf(p))
    return st?.state === 'cached' || st?.state === 'downloading' || Boolean(st?.pinned)
  })
}

/**
 * 「全部缓存」一批跑完之后木牌上那一句（输入是 `cacheall.cacheAll` 的结果）。停下 / 写满 / 有失败
 * 要各说各的 —— 它们要用户做的事不一样：写满了接着点「全部缓存」只会再撞一次，得先去清。
 */
export function bulkResultText(r) {
  if (!r) return ''
  const parts = [`缓存好 ${r.done} 组`]
  if (r.failed) parts.push(`${r.failed} 组没成`)
  let tail = ''
  if (r.full) tail = '：浏览器存储空间不够，已停下。去「管理缓存」清掉一些再来'
  else if (r.stopped) tail = `：已停止，还有 ${r.total - r.done - r.failed} 组没开始`
  return parts.join('，') + tail
}

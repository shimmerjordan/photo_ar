/**
 * 存到手机。**Android 那边「保存到相册」的网页等价物 —— 但只能做到"存进下载目录"。**
 *
 * ## 四条路，按「本机有没有、有没有人在下、要不要走数据源」分（设计 §3.1）
 *
 * | 情况 | 走哪条 | 为什么 |
 * |---|---|---|
 * | 本机缓存里有（视频 `photoar-media-v1` / 原图 `photoar-ref-v1`） | 读缓存 → `blob:` → `<a download>` | **零网络**。婚礼现场那张网正是最该省的东西 |
 * | 有进行中的下载任务（预取 / 「缓存」按钮 / 正在播放） | 挂上去等它下完 → `blob:` | 同一段不下第二遍（mediastore 的去重）；进度跟着那个任务走 |
 * | 开了整站数据源，或这张视频设了单个来源 | 开一个下载任务（顺便落缓存）→ `blob:` | 数据源只能经 `fetch` 走（跨源票据，见 netsrc.js），浏览器下载器用不上它；单个来源本来就不在服务端 |
 * | 其余 | 直接 `<a download href="/v1/…">` | 交给浏览器自己的下载器：不占内存、有系统级的下载通知、断了能续 |
 *
 * 全部走 blob 看着统一，但那意味着把一段 16MB 的视频整条读进内存再交出去 —— 换来的
 * 只是"进度条长在我们页面上"。浏览器的下载器在这件事上比我们做得好，**而且 iOS Safari
 * 对 `blob:` 的 `download` 支持有坑**（可能变成新标签打开而不是存文件）。所以只在
 * 字节已经（或者必然要）经过我们自己手里的时候才用它。
 *
 * ## 挂到别人任务上的那条路，任务失败了怎么办（fix1 #2）
 *
 * 「有进行中的下载任务」那一档不是我们主动开的口（多半是预取），它最终失败**不该
 * 连累这次「存到手机」**——退回「其余」那一档，交给浏览器自己的下载器（旧代码在
 * 挂任务这回事出现之前本来就是这么成功的）。真正**我们自己开的口**（数据源 / 单个
 * 来源直链）失败了才应该让调用方知道；直链那条套用与播放同一句 CORS 提示
 * （`overrideUrlFailText`，与 mediaload.js 共用，别写两份容易走样的文案）。
 *

 * ## 为什么图片要先发一次 HEAD
 *
 * 入库允许 JPEG / PNG / WebP，服务端按扩展名给 `Content-Type`（`_photo_ref` 的注释写明
 * 了"不能一律写 image/jpeg"）。而 `download` 属性给的文件名里那个后缀是**我们**写的 ——
 * 一律写 `.jpg` 的话，一张 PNG 存进相册就是一个打不开的文件。HEAD 在服务端会正确地
 * 不发响应体（`httpd.py` 里 `req.method == "HEAD"` 那一支），所以这一问是廉价的。
 * 走 blob 的那几条路不用问：字节在手上，类型就在 blob 上。
 *
 * ## 这一层做不到的事，必须由界面说出来
 *
 * 网页存下来的东西落在**浏览器的下载目录**，不是相册。安卓上多数相册应用会扫到
 * `Download/`，但那是相册的行为、不是我们能保证的。调用方要把这句话写在按钮旁边 ——
 * 用户点了「存到手机」之后在相册里找不到，会以为没存成。
 */
import * as api from './api.js'
import { mediaInfo, overrideUrlFailText } from './mediaload.js'
import {
  OVERRIDE_CACHE, Priority, REF_CACHE, VIDEO_CACHE, cached, download, jobFor, overrideKey, refKey, videoKey,
} from './mediastore.js'
import { activeBase, overrideSources } from './netsrc.js'
import { overrideOf } from './prefs.js'

/**
 * `Content-Type` → 文件名后缀。纯函数，好测。
 *
 * 认不出来的类型退回 `fallback` 而不是留空：没有后缀的文件在安卓上点开是"未知格式"，
 * 而那与"下载坏了"分不开。
 */
export function extFromContentType(ct, fallback = '.jpg') {
  const type = String(ct ?? '').split(';')[0].trim().toLowerCase()
  return {
    'image/jpeg': '.jpg',
    'image/jpg': '.jpg',
    'image/png': '.png',
    'image/webp': '.webp',
    'image/heic': '.heic',
    'image/avif': '.avif',
    'video/mp4': '.mp4',
    'video/webm': '.webm',
    'video/quicktime': '.mov',
  }[type] ?? fallback
}

/**
 * 拼一个下载文件名。纯函数，好测。
 *
 * 标题是**用户填的**，可能带斜杠、冒号、emoji、或者干脆是空的。这些字符里有一部分在
 * 安卓/Windows 上是非法文件名，而浏览器对非法名的处理各不相同（有的静默改名、有的
 * 整个不下）。所以在这里清一遍，清空了就退回 photoId 的前 8 位 —— 那至少是个能对上
 * 号的名字。
 */
export function safeFileName(title, photoId, ext) {
  const base = String(title ?? '')
    // 路径分隔符、Windows 的保留字符、以及控制字符。**斜杠必须清**：
    // `download="a/b.jpg"` 在部分浏览器上会被整个丢弃，表现是"点了没反应"。
    // 连字符与下划线不在这张表里 —— 那是标题里正常的字，清掉只会让文件名认不出。
    .replace(/[/\\<>:"|?*\x00-\x1f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    // 太长的名字在某些文件系统上会被截断到一半，连后缀都没了。
    .slice(0, 60)
  return `${base || `照片-${String(photoId ?? '').slice(0, 8)}`}${ext}`
}

/**
 * 触发一次下载。`revoke` 为真时在下一轮事件循环里回收 blob 地址。
 *
 * 锚点要真的进文档再点：Safari 对游离节点上的 `click()` 不一定响应。
 */
function triggerDownload(href, filename, { revoke = false } = {}) {
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  // 立刻 revoke 会让下载拿不到内容（点击是异步派发的）。给一分钟足够浏览器把字节读走，
  // 不 revoke 的话那十几 MB 会一直挂在文档上。
  if (revoke) setTimeout(() => URL.revokeObjectURL(href), 60_000)
}

/** 本机那份 / 下载任务下完的那份，存成文件。 */
function saveBlob(blob, filename) {
  triggerDownload(URL.createObjectURL(blob), filename, { revoke: true })
}

/**
 * 等一个下载任务下完，拿到整段 Blob。进度跟着**任务**报（它可能是预取早就开始下的，
 * 已经收了一半 —— 从 0 数起的话进度条会先瞬间冲到一半）。
 *
 * `response()` 在任务开跑之后立刻要：它给的是「已收重放 + 实时后续」，就算下完之后没写进
 * 缓存（配额满），我们手里这条也还是完整的。
 */
async function blobOfJob(job, onProgress) {
  const off = onProgress ? job.subscribe((s) => onProgress({ loaded: s.loaded, total: s.total })) : null
  onProgress?.({ loaded: job.loaded, total: job.total })
  try {
    return await job.response().blob()
  } finally {
    off?.()
  }
}

/** 下载任务那条路的结果句：顺便落缓存成没成要说实话（「下次秒开」不是每次都成立）。 */
function jobResult(job) {
  if (job.cacheFailed) return `已下完，去下载目录里找（没存进本机：${job.cacheFailed}）`
  return '已下完，去下载目录里找；本机也留了一份，下次秒开'
}

/**
 * 按「本机缓存 → 进行中的任务 → 该开任务就开」取一段字节。都不是返回 null（交给浏览器下载器）。
 *
 * @param open 为真时本机没有、也没人在下就开一个任务（开了数据源 / 单个来源）
 */
async function viaStore(key, { cacheName, sources, open, onProgress }) {
  const hit = await cached(key, cacheName)
  if (hit) return { blob: await readWithProgress(hit, onProgress), local: true }
  if (!jobFor(key) && !open) return null
  // 用户点的：已有的任务就地提到 USER（排着队的后台任务不必等预取让路）。
  const job = download(key, { cacheName, sources, priority: Priority.USER })
  try {
    const blob = await blobOfJob(job, onProgress)
    await job.done
    return { blob, local: false, job }
  } catch (e) {
    // `open` 是假的：这段字节不是我们主动要下的，只是恰好挂到了别人的任务上（比如
    // 正在预取）——那个任务失败不该连累「存到手机」，退回浏览器自己的下载器，旧代码
    // （没有挂任务这回事时）在这种情况下本来就能成功（fix1 #2）。
    // `open` 是真的：这次下载是我们自己开的口（数据源 / 单个来源直链），失败要让
    // 调用方知道——直链那条会在上层套一句与播放一致的 CORS 提示。
    if (!open) return null
    throw e
  }
}

/**
 * 存这张照片的**原图**（不是缩略图）。
 *
 * @returns 一句给用户看的结果
 */
export async function savePhotoImage(photoId, title) {
  const got = await viaStore(refKey(photoId), { cacheName: REF_CACHE, open: Boolean(activeBase()) })
  if (got) {
    saveBlob(got.blob, safeFileName(title, photoId, extFromContentType(got.blob.type, '.jpg')))
    return got.local ? '本机已有这张原图，没走网络' : jobResult(got.job)
  }
  const url = api.refUrl(photoId)
  let ct = null
  try {
    const head = await fetch(url, { method: 'HEAD', credentials: 'same-origin' })
    if (head.status === 404) throw new Error('这张照片的原图不在服务器上了')
    if (!head.ok) throw new Error(`服务器拒绝了（HTTP ${head.status}）`)
    ct = head.headers.get('content-type')
  } catch (e) {
    // HEAD 失败**不等于**下载会失败（有的中间层不转发 HEAD）。照下不误，只是后缀
    // 只能猜 —— 但如果是 404/403 那种明确的拒绝，上面已经抛出去了。
    if (/原图不在|服务器拒绝/.test(e.message)) throw e
  }
  triggerDownload(url, safeFileName(title, photoId, extFromContentType(ct, '.jpg')))
  return '已交给浏览器下载，去下载目录里找'
}

/**
 * 存这张照片配的那段视频。
 *
 * 本机已有时**一个字节都不走网络** —— 那正是预取 / 手动缓存真正兑现的时刻。
 * 设了单个来源的，存的是**那一份**（用户在这台手机上看到的就是它）。
 *
 * @param onProgress `({loaded, total})`，只在字节经过我们手里的那几条路上有（走浏览器
 *   下载器时进度在系统通知里，我们看不到也不该假装看得到）
 * @returns 一句给用户看的结果
 */
export async function savePhotoVideo(photoId, title, { onProgress } = {}) {
  const ov = overrideOf(photoId)
  if (ov?.kind === 'file') {
    const res = await cached(overrideKey(photoId), OVERRIDE_CACHE)
    if (!res) throw new Error('你设的本机文件不在浏览器存储里了，去设置 → 高级设置重新选一次')
    const blob = await readWithProgress(res, onProgress)
    saveBlob(blob, safeFileName(title, photoId, extFromContentType(blob.type || ov.type, '.mp4')))
    return '这段就是你设的本机文件，没走网络'
  }
  if (ov?.kind === 'url' && ov.url) {
    // 与播放同一个键（直链地址本身，见 mediaload.js）：播过的这里直接是本机那份。
    // 失败时套用与播放同一句提示（`overrideUrlFailText`，不是原始的任务错误文案），
    // 别在两处各写一份容易走样的 CORS 措辞（fix1 #2）。
    const got = await viaStore(ov.url, { cacheName: OVERRIDE_CACHE, sources: overrideSources(ov), open: true, onProgress })
      .catch((e) => { throw new Error(overrideUrlFailText(e?.message)) })
    saveBlob(got.blob, safeFileName(title, photoId, extFromContentType(got.blob.type, '.mp4')))
    return got.local ? '本机已有这段视频，没走网络' : jobResult(got.job)
  }

  const info = await mediaInfo(photoId)
  if (!info?.url) throw new Error('这张照片还没有配视频')
  if (info.missing) throw new Error('视频文件不在服务器上了')
  if (info.absolute) {
    // 网盘直链。`download` 属性对跨源地址无效（浏览器会忽略它并直接导航），
    // 所以这条路给不出"存文件"的保证 —— 说清楚比默默打开一个播放页要好。
    throw new Error('这段视频存在网盘上，网页版没法直接存到手机')
  }

  // 后缀写死 `.mp4`，**不猜也不问**：能播的那份 asset 一定是 `transcode.py` 产的
  // 分片 MP4（MediaSource 只吃 fMP4，见 mp4stream.js）。拿 `nasPath` 的扩展名反而会错 ——
  // 那是**原始**文件的名字，而下下来的是转码后的那一份。
  const filename = safeFileName(title, photoId, '.mp4')
  // 与播放 / 预取同一个缓存键（mediastore 的 videoKey）；老服务端没给 assetId 就退回 url。
  const key = info.assetId ? videoKey(info.assetId) : info.url
  const got = await viaStore(key, { cacheName: VIDEO_CACHE, open: Boolean(activeBase()), onProgress })
  if (got) {
    saveBlob(got.blob, filename)
    return got.local ? '本机已有这段视频，没走网络' : jobResult(got.job)
  }
  triggerDownload(info.url, filename)
  return '已交给浏览器下载，去下载目录里找'
}

/**
 * 把一个 Response 读成 Blob，边读边报进度。
 *
 * 读缓存本来很快，报进度是为了**大文件在慢机器上也有东西在动** —— 一个 16MB 的 blob
 * 在老手机上组装起来也要几百毫秒，那段时间界面不能是死的。
 *
 * 类型缺省按手上这份的类型来，不再一律写 video/mp4：原图也走这里。
 */
async function readWithProgress(res, onProgress) {
  const total = Number(res.headers.get('content-length')) || 0
  const type = res.headers.get('content-type') || 'application/octet-stream'
  if (!res.body || !onProgress) return await res.blob()
  const reader = res.body.getReader()
  const chunks = []
  let loaded = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    chunks.push(value)
    loaded += value.byteLength
    onProgress({ loaded, total })
  }
  return new Blob(chunks, { type })
}

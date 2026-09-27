/**
 * 用 `MediaSource` 播视频，而不是把地址交给 `<video src>`。
 *
 * ## 为什么必须绕这一圈（真机上逐个变量测出来的）
 *
 * 在安卓上，**`<video src=…>` 的请求不是浏览器自己的网络栈发的**。同一个页面里
 * 对同一个地址各发一次 `fetch` 和 `<video>`，服务端看到的 `User-Agent` 是两个 ——
 * 一个是浏览器，一个是安卓平台的媒体组件（MediaExtractor）。那个组件是独立的
 * HTTP 客户端，于是踩了两个坑，而它们**互相独立**：
 *
 * | 坑 | 表现 | 证据 |
 * |---|---|---|
 * | 拿不到 `HttpOnly` 会话 cookie | 后端每 3 秒一个 401、连十次；页面上 `readyState=0` 不动 | 后端日志；同页 `fetch` 是 206 |
 * | 有独立 TLS 栈，不认自签证书 | 同上，一声不响 | 同一文件走 http 能播、走自签 https 播不了 |
 *
 * 第一个坑用「媒体票据」修掉了（见 `api.playableUrl`）。**第二个修不掉** ——
 * 它不认浏览器里点的「继续访问」，也不认用户装的 CA（安卓 7 起用户 CA 不在
 * 平台组件的信任库里），而手机没 root 就装不了系统 CA。
 *
 * 所以只剩一条路：**别让那个组件碰网络**。页面自己 `fetch`（浏览器的网络栈，
 * 证书与 cookie 都没问题），把字节喂给 `MediaSource` —— 那条路由 Chromium 自己的
 * ChunkDemuxer 解封装，全程不经过平台组件。真机实测在 `https://<自签>` 上直接播通。
 *
 * 试过但不行的：`blob:` URL（先 fetch 回来再喂 `<video src>`）—— 那个组件连 blob:
 * 都不认，36ms 直接报 `EDGE_DEMUXER_ERROR_MEDIA_EXTRACTOR_FAILED`。
 *
 * ## 代价：必须是分片 MP4
 *
 * `MediaSource` 只吃 fMP4（`moof`/`mdat` 片段）。普通的 `moov+mdat` 喂进去是 12ms
 * 一个 sourcebuffer 错误。所以 `transcode.py` 改成了产 fMP4，存量文件由
 * `tools/fragment_playable.py` 无损重封装过一遍。
 *
 * ## 边下边播，不是下完再播
 *
 * 一次性 `arrayBuffer()` 再 append 的话，14MB 在 Tailscale 上实测要 40 秒 ——
 * 那还不如原来的坏法。所以这里读 `body.getReader()`，**边收边喂**：第一个片段
 * 进去 `readyState` 就到 1，能起播了。
 *
 * ## 只起播一次，暂停由用户说了算
 *
 * 第一次有数据可播（`readyState >= 1`）时调**一次** `playback.startPlayback`（有声优先、
 * 被拦退静音，结果经 `autoplay` 事件报出去），之后不再碰 `play()`。视频追上下载进度时
 * 浏览器自己进 `waiting`，新数据 append 进来会自动续播 —— 前提是用户没暂停。
 *
 * 这条是踩出来的（2026-09-26 用户原话「边下边播时不能暂停」）：原来 for 循环里每 append
 * 一块就 `if (video.readyState >= 1 && video.paused) video.play()`，本意是「第一块进去就
 * 起播」，实际效果是用户一按暂停、下一块到达就被播回去，直到整条下完。试播页、宾客页
 * （原生控件）、扫描页全部中招。`test/mp4stream.test.js` 钉着它。
 *
 * 退回 `<video src>` 的两条路（没有 MSE / 半路出错）同样起播一次 —— 原来这两条路上
 * 扫描页根本不会自动起播。半路退回时 `load()` 会把视频停下，所以**本来在播的接着播、
 * 用户暂停着的不替他播**。
 *
 * ### fix1：换票期间按的暂停也算数
 *
 * 上面「要不要续播」的 `resume` 原来只在 `fallback()` 那一刻量一次，可 `getFallbackUrl`
 * 是异步的（换票据 / 把整段读成 `blob:`），这段等待期间用户按的暂停，那份旧快照根本
 * 不知道 —— 地址一回来照样 `kick()`（评审复现：`plays 2 paused false`）。所以
 * `playDirect` 在真正换 `src` 之前（`video.load()` 会把 `paused` 强制置回 true，
 * 必须在这之前量）**再确认一次当下的暂停状态**，两次快照都说「该续」才续。
 *
 * ### 页面隐藏时不起播，回到前台再起（final-fix M2）
 *
 * 扫到照片之后、第一块数据到达之前锁屏 / 切走，扫描页的 `onVis` 只能暂停**当时已经在播**
 * 的视频 —— 还没起播的那段它管不着，随后数据一到 `kick()` 照样起播。而现在默认有声，
 * 结果就是手机在口袋里出声。所以起播这一下统一在这里把关：`document.hidden` 时只记一个
 * 「等回前台再起」，`visibilitychange` 变可见时才真起播（仍然只起一次）。三个页面都经过
 * 这里起播，一处管住全部。`document` 惰性取 `globalThis.document`：Node 测试里没有
 * document，视为可见。
 */
import { startPlayback } from './playback.js'

/** 我们发的是 H.264 High + AAC-LC（见 transcode.py 的常量）。 */
const MIME = 'video/mp4; codecs="avc1.640028,mp4a.40.2"'

/** 这个浏览器能不能走 MSE 这条路。 */
export function canStream() {
  return typeof MediaSource !== 'undefined' && MediaSource.isTypeSupported(MIME)
}

/**
 * 把 `url` 的内容流进 `video`。
 *
 * @param onEvent 可选 `(name, detail)` —— 打点用。哪条路走通了、为什么退回去了，
 *   不报出来的话手机上无从判断（这一整个模块的存在理由就是一个只在手机上出现的问题）。
 *
 *   事件名：`fallback` / `progress` / `done` / `autoplay`。**`progress` 是给界面用的，不只是打点**：
 *   识别命中之后那几秒里唯一有真百分比的一步就是它（见 mediaload.js 的四个阶段）。
 *   分母来自 `Content-Length` —— 拿不到时 `total` 是 0，调用方要按"不定长"处理而不是
 *   拿它当分母（除以 0 会得到 Infinity，进度条会直接跳满）。
 *   `autoplay` 的 detail 是 `startPlayback` 的返回值（`{muted, blocked}` / `{failed}` /
 *   `{aborted}`）：页面据此决定「开声音」要不要金框。被**我们自己**的退回（换 src）打断的
 *   那一次不报 —— 退回那条路紧接着会再起播一次、报真结果。
 *   `done` 只在整条真的喂完时报；退回 `<video src>` 那一下不算「下完了」。
 * @param autoplay 默认 true。false = 一次都不碰 `play()`（由调用方或用户自己点播放）。
 * @returns 卸载函数。**必须调** —— 它要中止还在跑的 fetch（或者退订下载任务给的那条流），
 *   并让之后才回来的异步结果（退路地址、起播结果）不再碰 `video`：扫描页的 `<video>` 是
 *   长驻的，卸载之后它多半已经在放下一段了。
 */
/**
 * @param src 流地址（票据 URL），或一个 Response：本机缓存命中的那份、下载任务给的
 *   「已收重放 + 实时后续」那条（见 mediastore.js 的 `response()`）、单个来源的本机文件。
 *   Response 走的是完全相同的喂流路径 —— Cache Storage 每次 match 给的都是新的一份，
 *   直接消费即可。
 * @param getFallbackUrl 只在 `src` 是 Response 时用到：MSE 这条路走不通要退回
 *   `<video src>` 时，Response 没有可用的地址，调它换一个（服务端媒体是去取一张票，
 *   单个来源是把整段读成 `blob:`，见 mediaload.js）。
 */
export function playStream(video, src, { onEvent, getFallbackUrl, autoplay = true } = {}) {
  const ac = new AbortController()
  /** 这条流不再往下读了：卸载了，或者已经退回 `<video src>`。 */
  let halted = false
  /** 调用方卸载了。之后回来的任何异步结果都不许再碰 `video`（见 @returns）。 */
  let unmounted = false
  let reader = null
  const halt = () => {
    if (halted) return
    halted = true
    ac.abort()
    // Response 来源（本机缓存 / 下载任务给的流）不认 signal：直接掐掉正在等的那次 read，
    // 否则循环要等下一块到了才退出，下载任务那边也一直以为这个读者还在。
    reader?.cancel().catch(() => {})
  }
  const stop = () => {
    unmounted = true
    halt()
    unwaitVisible()
  }

  /** 起过播没有。**只起一次** —— 见文件头「只起播一次，暂停由用户说了算」。 */
  let kicked = false
  /** 第几次起播。退回时换 src 会让上一次的 play() 以 AbortError 收场，那一次不报。 */
  let kicks = 0
  /**
   * 页面隐藏着、起播推迟到回前台时挂着的那个监听（见文件头「页面隐藏时不起播」）。
   * 惰性取 `globalThis.document`：模块在 Node 测试里也会被 import，那里没有 document。
   */
  let visDoc = null
  const pageHidden = () => Boolean(globalThis.document?.hidden)
  const unwaitVisible = () => {
    visDoc?.removeEventListener('visibilitychange', onVisible)
    visDoc = null
  }
  function onVisible() {
    if (pageHidden()) return                         // 还是隐藏（切了又切）：接着等
    unwaitVisible()
    if (!unmounted) kick()
  }
  const kick = () => {
    // 隐藏着：不起播，只挂一个「回前台再起」。`kicked` 保持 false —— 这段时间里的每一块
    // 数据、半路退回 `<video src>` 都会再来调 `kick()`，挂过了就什么也不做；退回那条路的
    // 「要不要续播」也照「还没起过播」算，回前台时起播的是换好之后的那个地址。
    if (pageHidden()) {
      if (!visDoc) {
        visDoc = globalThis.document
        visDoc.addEventListener('visibilitychange', onVisible)
      }
      return
    }
    kicked = true
    const n = ++kicks
    startPlayback(video).then((r) => {
      if (unmounted) return
      if (r?.aborted && n !== kicks) return
      onEvent?.('autoplay', r)
    })
  }

  const srcIsResponse = typeof src !== 'string'
  /** 退回 `<video src>` 时给它一个真能用的地址。 */
  const directUrl = async () =>
    srcIsResponse ? await Promise.resolve(getFallbackUrl?.()).catch(() => null) : src
  /**
   * 换成 `<video src>` 播。`resume` = 要不要起播：没起过播的起一次；起过播的只在
   * 「本来在播」时接着播 —— `load()` 按规范会把 `paused` 置回 true，不续的话画面就停在那儿，
   * 而用户暂停着的，不能因为我们换了条路就替他播起来。
   */
  const playDirect = (resume) => {
    directUrl().then((u) => {
      if (!u || unmounted) return
      // `resume` 是 `fallback()` 那一刻量的快照；`getFallbackUrl` 可能要等一阵子，
      // 这段时间里用户可能已经按了暂停（fix1 #1）。这里用同一个公式再量一次**当下**的
      // 暂停状态，且必须在 `video.load()` 之前 —— `load()` 之后 `paused` 一律会变 true，
      // 就分不出「用户暂停着」了。两次快照都说「该续」才续，任何一次说暂停着就不替他播。
      const stillResume = resume && (!kicked || !video.paused)
      video.src = u
      video.load()
      if (autoplay && stillResume) kick()
    })
  }

  if (!canStream()) {
    // 退回直连。在有这个毛病的安卓上它播不了，但在别的平台上它是完全正常的一条路，
    // 而"至少在能用的地方能用"胜过"哪儿都不能用"。
    onEvent?.('fallback', { why: 'no-mse' })
    playDirect(true)
    return stop
  }

  const ms = new MediaSource()
  const objectUrl = URL.createObjectURL(ms)
  video.src = objectUrl

  const fallback = (why, detail) => {
    if (halted) return
    // 必须在换 src 之前量：`load()` 之后 paused 一律是 true，就分不出「用户暂停着」了。
    const resume = !kicked || !video.paused
    onEvent?.('fallback', { why, ...detail })
    halt()
    URL.revokeObjectURL(objectUrl)
    playDirect(resume)
  }

  ms.addEventListener('sourceopen', async () => {
    if (halted) return
    let sb
    try {
      sb = ms.addSourceBuffer(MIME)
    } catch (e) {
      return fallback('addSourceBuffer', { error: String(e?.message ?? e) })
    }
    // `sequence` 不用：分片自带 baseMediaDecodeTime，`segments` 才是对的，
    // 否则拼接处的时间戳会被重排成连续的，音画对不上。
    sb.mode = 'segments'
    sb.addEventListener('error', () => fallback('sourcebuffer'))

    /** append 是异步的（`updating`），必须排队等它完成再喂下一块。 */
    const append = (chunk) => new Promise((resolve, reject) => {
      const ok = () => { sb.removeEventListener('error', bad); resolve() }
      const bad = () => { sb.removeEventListener('updateend', ok); reject(new Error('append 失败')) }
      sb.addEventListener('updateend', ok, { once: true })
      sb.addEventListener('error', bad, { once: true })
      try {
        sb.appendBuffer(chunk)
      } catch (e) {
        sb.removeEventListener('updateend', ok)
        reject(e)
      }
    })

    const t0 = performance.now()
    let bytes = 0
    try {
      const res = srcIsResponse
        ? src
        : await fetch(src, { credentials: 'same-origin', signal: ac.signal })
      if (!res.ok || !res.body) return fallback('fetch', { status: res.status })
      if (halted) return res.body.cancel().catch(() => {})
      // 分母。缓存命中的 Response 与网络来的都带 Content-Length（前者是 put 时存下的
      // 那一份响应头），拿不到就是 0 —— 调用方据此走不定长那一支。
      const total = Number(res.headers.get('content-length')) || 0
      reader = res.body.getReader()
      onEvent?.('progress', { loaded: 0, total })
      for (;;) {
        const { done: eof, value } = await reader.read()
        if (eof || halted) break
        bytes += value.byteLength
        onEvent?.('progress', { loaded: bytes, total })
        await append(value)
        // 第一次有东西可播时起播**一次**，之后不再碰 play() —— 见文件头那一节。
        // 这里原来是每块一句 `if (video.paused) video.play()`，用户的暂停因此形同虚设。
        if (autoplay && !kicked && video.readyState >= 1) kick()
      }
      if (halted) return                             // 卸载了 / 已退回 <video src>：不是「下完了」
      if (ms.readyState === 'open') ms.endOfStream()
      // 整条都喂完了还没到 readyState 1（整段只有一两块时会这样）：也起播一次，
      // play() 会等到有帧可播时才真的动。
      if (autoplay && !kicked) kick()
      onEvent?.('done', { bytes, ms: Math.round(performance.now() - t0) })
    } catch (e) {
      if (e?.name === 'AbortError') return           // 切页了，正常
      fallback('stream', { error: String(e?.message ?? e), bytes })
    }
  }, { once: true })

  return stop
}

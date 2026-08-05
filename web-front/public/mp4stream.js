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
 */

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
 * @returns 卸载函数。**必须调** —— 它要中止还在跑的 fetch，否则切页之后那 14MB
 *   还在下，而用户以为已经离开了。
 */
export function playStream(video, url, { onEvent } = {}) {
  const ac = new AbortController()
  let done = false
  const stop = () => {
    if (done) return
    done = true
    ac.abort()
  }

  if (!canStream()) {
    // 退回直连。在有这个毛病的安卓上它播不了，但在别的平台上它是完全正常的一条路，
    // 而"至少在能用的地方能用"胜过"哪儿都不能用"。
    onEvent?.('fallback', { why: 'no-mse' })
    video.src = url
    video.load()
    return stop
  }

  const ms = new MediaSource()
  const objectUrl = URL.createObjectURL(ms)
  video.src = objectUrl

  const fallback = (why, detail) => {
    if (done) return
    onEvent?.('fallback', { why, ...detail })
    stop()
    URL.revokeObjectURL(objectUrl)
    video.src = url
    video.load()
  }

  ms.addEventListener('sourceopen', async () => {
    if (done) return
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
      const res = await fetch(url, { credentials: 'same-origin', signal: ac.signal })
      if (!res.ok || !res.body) return fallback('fetch', { status: res.status })
      const reader = res.body.getReader()
      for (;;) {
        const { done: eof, value } = await reader.read()
        if (eof || done) break
        bytes += value.byteLength
        await append(value)
        // 第一块进去就能起播。**这一句是"边下边播"与"下完再播"的全部差别** ——
        // 14MB 在 Tailscale 上要 40 秒，等下完再播还不如原来的坏法。
        if (video.readyState >= 1 && video.paused) video.play().catch(() => {})
      }
      if (!done && ms.readyState === 'open') ms.endOfStream()
      onEvent?.('done', { bytes, ms: Math.round(performance.now() - t0) })
    } catch (e) {
      if (e?.name === 'AbortError') return           // 切页了，正常
      fallback('stream', { error: String(e?.message ?? e), bytes })
    }
  }, { once: true })

  return stop
}

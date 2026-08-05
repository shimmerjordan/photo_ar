/**
 * 服务端接口客户端。**对齐 Android 的 `PhotoArClient`**，同一批接口、同一套语义。
 *
 * ## 与 Android 那份的三处差别，都是平台带来的
 *
 * 1. **没有通道选择**（Android 的 `EndpointCenter` / 多端点探活）。网页就在服务器上发出来，
 *    请求全走同源相对路径 —— 探活、优先级、`viaLabel` 那一整套在这里没有对应物。
 *    这也意味着 Android 上那条「media 通道走隧道所以禁上传」的判断在这里换了依据，
 *    见 `uploadAllowed`。
 * 2. **凭证只有 cookie**。Android 用 `Authorization: Bearer`，网页用 HttpOnly cookie
 *    （服务端注释写明了理由：`<img>`/`<video>` 标签带不了请求头）。所以这里所有请求都
 *    `credentials: 'same-origin'`，而**不碰任何 token** —— 那个字符串一个字节都不该进 JS。
 * 3. **媒体是两步**。`/v1/photo/<id>/media` 返回的是**元信息 JSON**，真正的流在它的 `url`
 *    字段上。这一条踩过：直接把它塞给 `<video src>` 会让浏览器拿 JSON 去喂解封装器，
 *    报 `DEMUXER_ERROR_COULD_NOT_OPEN` 而 HTTP 是 200。所以 `mediaOfPhoto` 明确返回
 *    元信息，取流地址是调用方的下一步。
 */

/** 服务端把「没认出来」也当 200 返回，所以只有真错才抛。 */
export class ApiError extends Error {
  constructor(status, code, message, body) {
    super(message || `HTTP ${status}`)
    this.status = status
    this.code = code
    this.body = body
  }
  /** 401 = 重输可能成功（口令错）；403 = 重输没用（名字不在册 / 停用 / 无权限）。 */
  get retryable() {
    return this.status === 401
  }
}

async function req(path, { method = 'GET', body, headers, raw = false, signal } = {}) {
  const init = { method, credentials: 'same-origin', headers: { ...headers }, signal }
  if (body !== undefined) {
    if (body instanceof FormData || body instanceof Blob || body instanceof ArrayBuffer) {
      init.body = body
    } else {
      init.headers['Content-Type'] = 'application/json'
      init.body = JSON.stringify(body)
    }
  }
  const r = await fetch(path, init)
  if (raw) return r
  const text = await r.text()
  let data = null
  if (text) {
    try {
      data = JSON.parse(text)
    } catch {
      // 服务端偶尔会返回非 JSON（比如反代的错误页）。把原文带出去 —— 只说
      // "解析失败"会把唯一的线索扔掉。
      if (!r.ok) throw new ApiError(r.status, 'bad_json', text.slice(0, 300), text)
      return text
    }
  }
  if (!r.ok) throw new ApiError(r.status, data?.error, data?.message, data)
  return data
}

// ── 会话 ──────────────────────────────────────────────────────────────
export const login = (name, password) =>
  req('/v1/auth/login', { method: 'POST', body: password ? { name, password } : { name } })
export const logout = () => req('/v1/auth/logout', { method: 'POST' })
/**
 * 谁登录着。**路径是 `/v1/auth/me`，不是 `/v1/me`** —— 后者 404。
 *
 * 这一条踩过：写错路径之后 `me()` 抛 404，而 app.js 的 catch 把它当访客处理
 * （"拿不到角色时按访客，那是更安全的默认"）。于是**管理员永远看不到管理功能，
 * 而没有任何报错** —— 底栏静静地少了三个页签。安全的默认掩盖了一个 404。
 *
 * @returns `{userId, name, role, grantAll, isAdmin}`
 */
export const me = () => req('/v1/auth/me')
export const ping = () => req('/v1/ping')

// ── 照片 ──────────────────────────────────────────────────────────────
export const photos = () => req('/v1/photos').then((d) => d?.photos ?? [])
export const photoDetail = (id) => req(`/v1/photo/${id}`)
export const deletePhoto = (id) => req(`/v1/photo/${id}`, { method: 'DELETE' })

/**
 * 一张照片的媒体**元信息**（不是流）。
 *
 * @returns `{url, via, absolute, supportsRange, bytes, durationMs, missing, integrity}`
 *   真正能喂给 `<video>` 的是 `url`。
 */
export const mediaOfPhoto = (id) => req(`/v1/photo/${id}/media`)

export const thumbUrl = (id) => `/v1/photo/${id}/thumb`
export const refUrl = (id) => `/v1/photo/${id}/ref`

/**
 * 把流地址换成一个 `<video>` 真能取到的地址。**每一处喂给 `<video>` 的地址都要过这里。**
 *
 * ## 为什么不能直接用 `mediaOfPhoto().url`
 *
 * 真机实测（小米 M2012K11C / Edge for Android 150）：`<video>` 的请求**不是浏览器
 * 自己的网络栈发的** —— 同一个页面里两次请求到服务端的 `User-Agent` 都不一样，
 * 一个是浏览器，一个是安卓平台的媒体组件。而那个组件**拿不到 `HttpOnly` 的会话
 * cookie**，后端日志里是每 3 秒一次、连续十次的 `401`，而同一页 `fetch()` 同一个
 * 地址是 `206`。页面上的表现是视频永远 `readyState=0`，**一声不响，没有任何报错**。
 *
 * `/api/ticket` 用当前会话换一张短命的一次性票，`/api/stream/<票>` 不需要 cookie，
 * 服务端在转发时把真凭证补上（见 server/index.js 的「媒体票据」一节）。
 *
 * 拿不到票就退回原地址：那样在能用的浏览器上照旧工作，比整个播不了强。
 */
export async function playableUrl(streamUrl) {
  if (!streamUrl || /^[a-z]+:/i.test(streamUrl)) return streamUrl   // 绝对地址不归我们管
  try {
    const t = await req(`/api/ticket?path=${encodeURIComponent(streamUrl)}`)
    return t?.url ?? streamUrl
  } catch {
    return streamUrl
  }
}

// ── 历史 ──────────────────────────────────────────────────────────────
/** 全库识别记录。**admin only**（服务端如此，因为 `recognize_log` 里没有"谁扫的"这一列）。 */
export const history = (limit = 100) =>
  req(`/v1/history?limit=${limit}`).then((d) => d?.items ?? d?.history ?? [])

// ── 上传与入库 ────────────────────────────────────────────────────────
/**
 * 先问服务端"这个文件是不是已经有了"。
 *
 * Android 那边这一步的价值写在 §29：**上传之前就告诉他重复了**。一段 50MB 的视频传完
 * 再被拒，用户白等一分钟且不知道为什么。
 *
 * @param sha256 十六进制小写
 */
export const uploadCheck = (sha256, bytes) =>
  req(`/v1/upload/check?sha256=${sha256}&bytes=${bytes}`)

/**
 * 传一个文件到 NAS。
 *
 * 用 `XMLHttpRequest` 而不是 `fetch`：**fetch 没有上传进度**（`ReadableStream` 上传
 * 要求 HTTP/2 且各家支持不一）。而这里传的是几十 MB 的视频，没有进度条就等于卡死 ——
 * Android 那边同样显示 `已发送 / 总共` 和 `已经 N 秒`。
 *
 * @param onProgress `({loaded, total})`
 */
export function upload(file, { kind, onProgress, signal } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    const fd = new FormData()
    fd.append('file', file, file.name)
    if (kind) fd.append('kind', kind)
    xhr.open('POST', '/v1/upload')
    xhr.withCredentials = true
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress?.({ loaded: e.loaded, total: e.total })
    }
    xhr.onload = () => {
      let data = null
      try {
        data = JSON.parse(xhr.responseText)
      } catch { /* 非 JSON，下面按状态码处理 */ }
      if (xhr.status >= 200 && xhr.status < 300) resolve(data)
      else reject(new ApiError(xhr.status, data?.error, data?.message ?? xhr.responseText?.slice(0, 200), data))
    }
    xhr.onerror = () => reject(new ApiError(0, 'network', '上传中断（网络断了、或请求体超过了通道上限）'))
    xhr.onabort = () => reject(new ApiError(0, 'aborted', '上传已取消'))
    signal?.addEventListener('abort', () => xhr.abort())
    xhr.send(fd)
  })
}

/** 入库：把已经在 NAS 上的一张参考图变成一条 photo。会跑特征提取，可能几十秒。 */
export const createPhoto = (payload) => req('/v1/photo', { method: 'POST', body: payload })

/** 给已有的 photo 配（或换）视频。 */
export const attachVideo = (id, payload) =>
  req(`/v1/photo/${id}/video`, { method: 'POST', body: payload })

/** 换参考图。服务端会重算特征与质量分，见 §21。 */
export const replaceRef = (id, payload) =>
  req(`/v1/photo/${id}/ref`, { method: 'POST', body: payload })

/** 按 sha256 或路径反查已入库的照片。上传前判重用。 */
export const lookup = (params) =>
  req(`/v1/lookup?${new URLSearchParams(params)}`)

// ── 配置 ──────────────────────────────────────────────────────────────
/** web-front 转出来的识别阈值（服务端热配置）。拿不到就是空对象，不是错误。 */
export const webConfig = () => req('/api/config')

/**
 * 能不能上传。
 *
 * Android 的判据是「media 通道是不是隧道」（隧道有 100MB 请求体上限，超了 413）。
 * 网页没有通道概念，所以这里的判据换成**服务端有没有明确拒绝**：真超限时
 * `/v1/upload` 会返回 413 并说明原因（服务端见到 `CF-Ray` 头会主动 413）。
 *
 * 也就是说这里不做预判，而是**如实把失败原因显示出来**。预判需要知道当前请求是否
 * 经过隧道，而那件事在浏览器里没有可靠信号 —— 猜错的两种后果都糟：该禁的没禁（用户
 * 白等一分钟再看到 413），或者不该禁的禁了（局域网下明明能传却没有入口）。
 */
export const UPLOAD_LIMIT_NOTE =
  '经 Cloudflare 隧道时请求体上限 100MB，超了会被 413 拒掉。传大视频请连回家里的网络或开 Tailscale。'

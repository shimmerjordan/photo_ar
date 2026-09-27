/**
 * 给一个缓存键（多半是流地址，比如 `/v1/asset/<id>/stream`）算出「按顺序去哪取」的
 * 来源列表，并负责整站数据源专属的三件事：换票、`lanMode`、60 秒熔断。
 *
 * ## 为什么要有「数据源」这个概念（设计 §2.1、§2.2、§3.1）
 *
 * 默认地址（同源，cookie 鉴权）永远能用，但走的是隧道，速度受限。用户可以在设置页
 * 配一个局域网/公网直连地址（NAS 自建服务、Tailscale 等），我们管它叫「整站媒体
 * 数据源」。它只是**加速**，不能成为新的故障点：`mediastore.js` 按 `sourcesFor()`
 * 给出的顺序依次尝试，数据源失败就退回默认源续传（不从头下）；`tripBase()` 把数据源
 * 标记「暂时不可用」60 秒，期间的 `sourcesFor()` 直接不给它，省得每次下载都先失败
 * 一次再退回。
 *
 * ## 为什么数据源请求要先换票、还要分 `lanMode`
 *
 * 数据源是**跨源**的（不同主机/端口），会话 cookie 是 HttpOnly + 按源隔离，带不过去
 * 也不该带（§2.2）——所以数据源请求一律先在当前源换一张票（`api.ticketFor`），
 * 再 `credentials:'omit'` 地去数据源的 `/api/stream/<票>` 取，票本身就是凭证。
 *
 * https 页面请求 http 的私网地址会被当**混合内容**拦掉；Chromium 新版本的
 * Local Network Access 给 `fetch()` 开了口子，但要在请求里显式声明
 * `targetAddressSpace`（新枚举 `'local'`，或 PNA 时代的旧枚举 `'private'`）。
 * 我们不知道对方浏览器/服务器接受哪种写法，所以设置页「测试」时用 `probeBase()`
 * 依次试三种写法，把试出来的那种记成 `lanMode`（存在 `prefs.js` 里），运行时
 * `lanInit()` 直接按它发，不必每次都探测。只有目标是私网地址时才需要带这个选项——
 * 公网地址不存在混合内容问题，多带一个未知的 fetch 选项反而可能在不支持的浏览器上
 * 直接同步抛 `TypeError`（非法枚举值），`probeBase` 因此也把 `fetchImpl` 同步抛出的
 * 情况当失败接住，而不是让它成为一个未捕获异常。
 *
 * ## 只管「能不能发票的路径」才走数据源
 *
 * 数据源另一头的 Node 服务只给三类路径开了票据通道（Task 3：`/v1/asset/<id>/stream`、
 * `/v1/photo/<id>/ref`、`/v1/photo/<id>/thumb`）。别的路径（比如管理台的
 * `/v1/admin/media`）发了票也换不来数据，`ticketablePath` 提前把这些挡在
 * `sourcesFor` 之外，不必真的失败一次才知道。
 */
import * as api from './api.js'
import { advanced } from './prefs.js'

/** 见函数体注释：四类私网段 + 环回 + `.local`/`.localhost` + IPv6 私网前缀。 */
export function hostKind(url) {
  let h
  try {
    h = new URL(url).hostname.replace(/^\[|\]$/g, '').toLowerCase()
  } catch {
    return 'public'
  }
  if (h === 'localhost' || h.endsWith('.localhost') || h === '::1' || /^127\./.test(h)) return 'loopback'
  if (h.endsWith('.local')) return 'private'
  const m = h.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/)
  if (m) {
    const [a, b] = [Number(m[1]), Number(m[2])]
    if (a === 10) return 'private'
    if (a === 172 && b >= 16 && b <= 31) return 'private'
    if (a === 192 && b === 168) return 'private'
    if (a === 169 && b === 254) return 'private'
    if (a === 100 && b >= 64 && b <= 127) return 'private' // CGNAT / Tailscale
    return 'public'
  }
  if (/^f[cd][0-9a-f]{2}:/.test(h) || /^fe[89ab][0-9a-f]:/.test(h)) return 'private'
  return 'public'
}

/**
 * 公网地址永远不带 `targetAddressSpace`（不需要，多带反而可能在旧浏览器上出问题）；
 * 私网/环回地址按已经探测出来的 `lanMode` 决定带哪种写法，没探测过（`null`）就不带，
 * 等着这次请求按普通跨源处理——带不带都不影响正确性，只影响会不会被当混合内容拦。
 */
export function lanInit(url, mode = advanced().lanMode) {
  if (hostKind(url) === 'public') return {}
  if (mode === 'local' || mode === 'private') return { targetAddressSpace: mode }
  return {}
}

let trippedUntil = 0
/** 熔断：接下来 `ms` 毫秒内 `activeBase()` 装作没配数据源。 */
export const tripBase = (ms = 60_000) => { trippedUntil = Date.now() + ms }
export const resetTrip = () => { trippedUntil = 0 }

/** 数据源没开、没填地址、或正在熔断期内，一律当作「没有数据源」。 */
export function activeBase() {
  const a = advanced()
  if (!a.mediaBaseOn || !a.mediaBase) return ''
  if (Date.now() < trippedUntil) return ''
  return a.mediaBase
}

const TICKETABLE = /^\/v1\/(asset\/[A-Za-z0-9_-]+\/stream|photo\/[A-Za-z0-9_-]+\/(ref|thumb))$/
export const ticketablePath = (p) => TICKETABLE.test(String(p ?? ''))

/**
 * 顺序：数据源在前（更快，值得先试）、默认源在后（永远能用，兜底）。
 * `key` 本身就是默认源要用的相对路径，同时也是拿去换票的 `path`。
 */
export function sourcesFor(key) {
  const out = []
  const base = activeBase()
  if (base && ticketablePath(key)) {
    out.push({
      label: 'lan',
      resolve: async () => base + (await api.ticketFor(key)),
      init: { mode: 'cors', credentials: 'omit', cache: 'no-store', ...lanInit(base) },
    })
  }
  out.push({ label: 'origin', resolve: async () => key, init: { credentials: 'same-origin' } })
  return out
}

/**
 * 单个媒体来源（设计 §2.3，只管视频）。目前只有 `kind:'url'` 直链需要真的发网络
 * 请求——`kind:'file'`（本机文件）的字节已经复制进 `photoar-override-v1`，
 * 走的是缓存命中路径，不经过这里。
 */
export function overrideSources(ov) {
  if (ov?.kind !== 'url') return []
  return [{ label: 'url', resolve: async () => ov.url, init: { mode: 'cors', credentials: 'omit', ...lanInit(ov.url) } }]
}

/**
 * 设置页「测试」按钮：依次试三种写法（不带选项 → `local` → `private`），
 * 记住第一种成功的。全失败时给出「照着做」的提示，而不是一句"连不上"就完事——
 * 混合内容、HTTP 状态、超时、其它错误，四种情况的正确动作完全不同（见 `probeHint`）。
 */
export async function probeBase(base, { timeoutMs = 4000, fetchImpl = fetch, pageProtocol = location.protocol } = {}) {
  const pageHttps = pageProtocol === 'https:'
  const modes = hostKind(base) === 'public' ? ['plain'] : ['plain', 'local', 'private']
  let lastError, status
  for (const m of modes) {
    const t0 = Date.now()
    try {
      const res = await fetchImpl(base + '/healthz', {
        mode: 'cors',
        credentials: 'omit',
        cache: 'no-store',
        signal: AbortSignal.timeout(timeoutMs),
        ...(m === 'plain' ? {} : { targetAddressSpace: m }),
      })
      if (res.ok) return { ok: true, mode: m, ms: Date.now() - t0 }
      status = res.status
      lastError = new Error(`HTTP ${res.status}`)
    } catch (err) {
      // 不认识的 targetAddressSpace 枚举值、或网络错误，`fetchImpl` 都可能同步抛出
      // 而不是返回 rejected promise——两种都当这一种写法失败，接着试下一种。
      lastError = err
    }
  }
  return {
    ok: false,
    error: lastError?.message ?? String(lastError),
    hint: probeHint({ pageHttps, baseHttp: base.startsWith('http:'), kind: hostKind(base), lastError, status, timeoutMs }),
  }
}

/**
 * 纯函数，四档提示，逐字对应设计 §2.1/§6.3。文案本身就是测试断言的关键字来源，
 * 改文案要连着改测试。`timeoutMs` 默认取 `probeBase` 自己的默认超时（4 秒），
 * 调用方传了别的超时就照实说。
 */
export function probeHint({ pageHttps, baseHttp, lastError, status, timeoutMs = 4000 } = {}) {
  if (pageHttps && baseHttp) {
    return '当前页面是 https，浏览器把 http 地址当混合内容拦掉了。换一个带证书的局域网地址（https://），或在支持「本地网络访问」的新版 Chrome/Edge 上允许访问本地网络设备后再测。'
  }
  if (status != null && (status < 200 || status >= 300)) {
    return `连上了，但对方回了 HTTP ${status}。确认这个地址指向的是同一台 photo-ar，且版本够新（要有 /healthz 的跨源支持）。`
  }
  if (lastError?.name === 'TimeoutError' || lastError?.name === 'AbortError') {
    return `等了 ${Math.round(timeoutMs / 1000)} 秒没有回应：手机可能不在这个局域网里，或者地址/端口不对。`
  }
  return '连不上：检查地址和端口；如果浏览器弹过「访问本地网络」的询问，要选允许。'
}

/**
 * 简单测速：各来源各拿一段字节，掐表算 Mbps，供设置页显示「测试」结果的速度参考。
 * `lan` 来源可能压根不存在（没配数据源），此时报 `null` 而不是硬凑一个错误。
 *
 * 速度按**实际收到的**字节算，且非 2xx 当失败：原来按「请求了多少」去除耗时、也不看状态码，
 * 于是一个 403 的小 JSON 秒回就显示成几百 MB/s，视频比测速长度短时同样虚报。
 */
export async function speedTest(key, { bytes = 2 * 1048576, fetchImpl = fetch } = {}) {
  const sources = sourcesFor(key)
  const byLabel = Object.fromEntries(sources.map((s) => [s.label, s]))

  async function measure(src) {
    if (!src) return null
    const t0 = Date.now()
    try {
      const url = await src.resolve()
      const res = await fetchImpl(url, { ...src.init, headers: { Range: `bytes=0-${bytes - 1}` } })
      if (!res.ok) {
        res.body?.cancel().catch(() => {})
        throw new Error(`HTTP ${res.status}`)
      }
      const got = (await res.arrayBuffer()).byteLength // 掐表要算上把 body 读完的时间，不只是拿到响应头
      const ms = Math.max(1, Date.now() - t0)
      return { ms, bytes: got, mbps: (got * 8) / ms / 1000 }
    } catch (err) {
      return { error: err?.message ?? String(err) }
    }
  }

  const [lan, origin] = await Promise.all([measure(byLabel.lan), measure(byLabel.origin)])
  return { lan, origin }
}

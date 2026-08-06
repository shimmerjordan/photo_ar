#!/usr/bin/env node
/**
 * web-front：零依赖 Node 服务。四件事，一件不多。
 *
 *   1. **发静态资源**，并带上跨源隔离头（`crossOriginIsolated` 是 wasm 线程的前提）
 *   2. **反代 photo-ar 服务端的 `/v1/*` 与 `/admin`**，让页面、API、管理台同源
 *   3. **`/api/lib`**：拿用户的 cookie 去问服务端「你能看哪些照片」，再从 `data/library/`
 *      里把那些照片的 ORB 描述子打成一个包发下去
 *   4. **`/api/ticket` + `/api/stream/<票>`**：给安卓的平台媒体组件用的一次性票据
 *      （它拿不到 HttpOnly cookie，理由写在 `issueTicket` 上面）
 *
 * ## 整个服务只有一个端口，按 URI 分
 *
 *     /            网页版（宾客扫照片）        本进程发静态
 *     /api/*       上面第 3、4 条              本进程
 *     /admin       网页管理台                  反代 → photo-ar
 *     /v1/*        后端 API                    反代 → photo-ar
 *     /healthz     存活探测                    本进程
 *     /ca.crt      自测用的本地 CA             本进程
 *
 * 2026-08-05 之前 `/admin` **不在**这张表里，于是网页版「打开管理台」那个按钮
 * （`pages/admin.js` 里的 `window.open('/admin')`）点开是 404 —— 它一直假设两者同源，
 * 而那时候管理台在另一个容器的另一个端口上。合并容器同时把这条补上了。
 *
 * ## 为什么要反代，而不是让浏览器直接打服务端
 *
 * 三个都是硬理由：
 *
 * - **cookie**。服务端的网页鉴权是 HttpOnly cookie（它必须是 cookie，因为 `<img>` 和
 *   `<video>` 标签没法带请求头 —— 服务端注释里写明了这一点）。跨源的话要 SameSite=None
 *   + Secure + CORS 全套，而且 Safari 的 ITP 会拦第三方 cookie。同源就没有这些事。
 * - **COEP**。`Cross-Origin-Embedder-Policy: require-corp` 会要求页面里**每一个**跨源
 *   资源都带 CORP 头，包括视频。服务端不会给那个头，所以视频要么同源、要么放弃跨源隔离。
 * - **视频的 Range**。同源之后 `<video>` 的分段请求原样透传，不需要任何额外配置。
 *
 * 代价是所有视频流量过一遍 Node。对婚礼场景（几十人）这是流式转发，不缓冲整条视频。
 *
 * ## 环境变量
 *
 * | 变量 | 默认 | 说明 |
 * |---|---|---|
 * | `PORT` | `8964` | 监听端口。合并容器里由 entrypoint 设成对外那个端口 |
 * | `HOST` | `0.0.0.0` | 监听地址 |
 * | `PHOTOAR_UPSTREAM` | `http://127.0.0.1:8000` | photo-ar 服务端 |
 * | `PHOTOAR_LIBRARY` | `/data/library` | ORB 识别库目录（只读挂载） |
 * | `WEBFRONT_ISOLATION` | `1` | 设 `0` 关掉 COOP/COEP（排查 wasm 加载问题时用） |
 */
import { createServer } from 'node:http'
import { createServer as createTlsServer } from 'node:https'
import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { createHash, randomUUID } from 'node:crypto'
import { readFile, stat } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { extname, join, normalize, resolve } from 'node:path'
import { Library } from './library.js'

const PUBLIC = resolve(import.meta.dirname, '../public')

/**
 * 版本号。**给设置页那个"连按进调试模式"的行用的**，顺带让"线上跑的是哪一版"变成
 * 一句能看的话。
 *
 * `PHOTOAR_VERSION` 由镜像构建时注入（Dockerfile 的 ARG → ENV，CI 填 tag 或短 sha）。
 * 没注入就退回 package.json 里那个 + `-dev` —— 那正好区分"从镜像跑的"和"本地
 * `node server/index.js` 跑的"，而这两者的行为差别（有没有预压的 .br、public/ 是不是
 * 镜像里那一份）恰恰是排查时第一个要问的。
 */
const VERSION = (() => {
  const injected = (process.env.PHOTOAR_VERSION ?? '').trim()
  if (injected) return injected
  try {
    const pkg = JSON.parse(readFileSync(resolve(import.meta.dirname, '../package.json'), 'utf8'))
    return `${pkg.version}-dev`
  } catch {
    return 'unknown'
  }
})()
// 默认值与后端的 `DEFAULT_PORT` 是同一个数：合并之后这两个进程对外只有一个端口，
// 而它归这个进程管（容器里由 entrypoint 显式设 PORT，这个默认值是给
// `npm start` 那种单独跑的用法的）。
const PORT = Number(process.env.PORT ?? 8964)
const HOST = process.env.HOST ?? '0.0.0.0'
const UPSTREAM = new URL(process.env.PHOTOAR_UPSTREAM ?? 'http://127.0.0.1:8000')
const LIB_DIR = process.env.PHOTOAR_LIBRARY ?? '/data/library'
const ISOLATION = (process.env.WEBFRONT_ISOLATION ?? '1') !== '0'
const TLS_CERT = process.env.WEBFRONT_TLS_CERT
const TLS_KEY = process.env.WEBFRONT_TLS_KEY

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.wasm': 'application/wasm',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain; charset=utf-8',
  '.webmanifest': 'application/manifest+json',
  // 视频。**不能漏** —— 漏了就是 `application/octet-stream`，而我们同时发着
  // `X-Content-Type-Options: nosniff`，于是浏览器**拒绝**把它当视频解码，
  // 报的却是「格式不支持」，看不出是 MIME 的问题。
  // 正常路径上的视频走 /v1/* 反代（Content-Type 由后端给），这一条是给
  // 放在 public/ 下的视频兜底的。
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
}

/**
 * 跨源隔离（COOP/COEP）。
 *
 * ## 它现在**什么也没换来** —— 别被上一版注释骗了
 *
 * 上一版写着"开着才有 SharedArrayBuffer，也就是 wasm 线程池，单线程慢 2~3 倍"。
 * 那句话对**别的** opencv 构建成立，对我们这份不成立：`public/vendor/opencv.js` 里
 * `pthread` 与 `SharedArrayBuffer` 各出现 **0 次** —— 它本来就是单线程构建。
 *
 * 也就是说这两个头目前只有代价没有收益：`require-corp` 之下任何缺 CORP 头的跨源资源
 * 都会被拦，而浏览器控制台那条报错不会说"是 COEP 拦的"。所以 `WEBFRONT_ISOLATION=0`
 * 现在是**免费**的，排查加载问题时可以放心关掉。
 *
 * 留着默认开，是为了将来真换成多线程构建时不用再想起这件事（那时 SAB 需要它）。
 * 换构建的人请连这段注释一起改。
 */
const ISOLATION_HEADERS = ISOLATION
  ? {
      'Cross-Origin-Opener-Policy': 'same-origin',
      'Cross-Origin-Embedder-Policy': 'require-corp',
    }
  : {}

const SECURITY_HEADERS = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'same-origin',
  // 相机权限要显式允许自己 —— 有些浏览器的默认 Permissions-Policy 会挡掉 iframe 里的
  // getUserMedia，而这个页面可能被人嵌在别处打开。
  'Permissions-Policy': 'camera=(self)',
}

const library = new Library(LIB_DIR)

/**
 * 直接监听 TLS 的能力。**给"手机在局域网/Tailscale 里测试"用的。**
 *
 * 为什么需要它：`getUserMedia` 只在安全上下文里存在，而局域网的 `http://192.168.x.x`
 * 或 Tailscale 的 `http://100.x.x.x` 都不算。生产形态是前面挂一层隧道（那边自带证书），
 * 但**自测时手机上没有那一层** —— 于是要么这个进程自己说 https，要么根本没法在真机上
 * 点开相机。
 *
 * 拿证书两条路，优先第一条：
 *
 *   1. **Tailscale 的真证书**（Let's Encrypt，无警告）：先在 Tailscale 后台的 DNS 页面
 *      打开 `HTTPS Certificates`，然后
 *      `tailscale cert <机器>.<tailnet>.ts.net` 生成 `.crt` / `.key`。
 *   2. **自签**：`tools/gen-dev-cert.sh`。浏览器会警告，手机上要点"高级 → 继续"，
 *      而且**必须带 IP/DNS 的 SAN** —— 现代浏览器对没有 SAN 的证书连"继续"都不给。
 *
 * 只给 cert 不给 key（或反之）就**直接退出**，不静默回落到 http：那会让人以为
 * 配置生效了，然后在手机上对着一个打不开相机的页面查半天权限。
 */
function tlsOptions() {
  if (!TLS_CERT && !TLS_KEY) return null
  if (!TLS_CERT || !TLS_KEY) {
    console.error('[web-front] WEBFRONT_TLS_CERT 与 WEBFRONT_TLS_KEY 必须同时给。' +
      '只给一个就退出，而不是悄悄回落到 http —— 那会让你在手机上白查半天相机权限。')
    process.exit(2)
  }
  try {
    return { cert: readFileSync(TLS_CERT), key: readFileSync(TLS_KEY) }
  } catch (e) {
    console.error(`[web-front] 读不到证书：${e.message}`)
    process.exit(2)
  }
}

const tls = tlsOptions()
const handler = async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host ?? 'localhost'}`)
    if (url.pathname === '/healthz') return json(res, 200, { ok: true, upstream: UPSTREAM.origin })
    if (url.pathname === '/ca.crt') return serveCa(res)
    if (url.pathname === '/api/lib') return await serveLib(req, res, url)
    if (url.pathname === '/api/config') return await serveConfig(req, res)
    if (url.pathname === '/api/ticket') return issueTicket(req, res, url)
    if (url.pathname.startsWith('/api/stream/')) return serveTicket(req, res, url)
    if (url.pathname.startsWith('/v1/')) return proxy(req, res, url)
    // 管理台。**不加跨源隔离头** —— 那是给这个页面的 wasm 准备的（见 ISOLATION_HEADERS
    // 上面那段），而管理台是另一个应用、一个 wasm 都不用。给它加 COEP 只是凭空多一条
    // 「哪天管理台引了个外部资源就被拦掉，而控制台不会说是谁拦的」的路。
    if (url.pathname === '/admin' || url.pathname.startsWith('/admin/')) {
      return proxy(req, res, url, { isolate: false })
    }
    return await serveStatic(req, res, url)
  } catch (e) {
    console.error('[web-front] 未捕获：', e)
    if (!res.headersSent) json(res, 500, { error: 'internal', message: String(e?.message ?? e) })
    else res.end()
  }
}

const server = tls ? createTlsServer(tls, handler) : createServer(handler)

/**
 * 把本地 CA 证书发下去，好让手机装上它。
 *
 * 存在的理由是体验：不装 CA 的话，自签证书每次访问都要在 Chrome 的警告页盲打
 * `thisisunsafe`（那个页面有时连"高级 → 继续"都不给）。装一次 CA 之后，这台机器签的
 * 所有证书都被信任 —— 换 IP、加域名都不用再装。
 *
 * **MIME 必须是 `application/x-x509-ca-cert`**：Android 靠它触发"安装证书"流程，
 * 给 `text/plain` 的话浏览器只会把证书当文本显示出来，而那看起来像"下载坏了"。
 *
 * 只发**公钥**证书（`ca.crt`）。`ca.key` 在同一个目录里但永远不经这条路 —— 路径是
 * 写死的文件名，不接受任何来自请求的输入。
 */
function serveCa(res) {
  if (!TLS_CERT) {
    return json(res, 404, {
      error: 'no_ca',
      message: '这个实例没有配 TLS，也就没有本地 CA。见 tools/gen-dev-cert.sh。',
    })
  }
  // CA 与服务器证书同目录同名规则（gen-dev-cert.sh 写死的）。
  const caPath = join(TLS_CERT, '..', 'ca.crt')
  let buf
  try {
    buf = readFileSync(caPath)
  } catch {
    return json(res, 404, {
      error: 'no_ca',
      message: `找不到 ${caPath}。这个证书可能不是 tools/gen-dev-cert.sh 生成的` +
        '（比如 tailscale cert 的真证书就不需要装 CA —— 那种情况本来就不该走这条路）。',
    })
  }
  res.writeHead(200, {
    // 见上面那段：Android 靠这个 MIME 触发安装流程。
    'Content-Type': 'application/x-x509-ca-cert',
    'Content-Disposition': 'attachment; filename="photoar-dev-ca.crt"',
    'Content-Length': buf.length,
    'Cache-Control': 'no-store',
    ...SECURITY_HEADERS,
  })
  res.end(buf)
}

// ── 静态 ──────────────────────────────────────────────────────────────
/**
 * 发 `staleguard.js`，把版本占位符换成这一版的版本号。
 *
 * ETag 必须**带上版本**：同一个镜像里文件的 size/mtime 不变，而发出去的内容随
 * `PHOTOAR_VERSION` 变。沿用普通静态文件那个 `size-mtime` 的 ETag 会让升级后的
 * 浏览器拿到 304、继续用旧版本号 —— 那正好让这个探测器自己失效。
 */
function serveStaleGuard(req, res, file, st) {
  const body = Buffer.from(
    readFileSync(file, 'utf8').replaceAll('__PHOTOAR_VERSION__', VERSION),
    'utf8',
  )
  const etag = `"sg-${st.size.toString(16)}-${Buffer.from(VERSION).toString('hex')}"`
  if (req.headers['if-none-match'] === etag) {
    res.writeHead(304, { ETag: etag, ...ISOLATION_HEADERS }).end()
    return
  }
  res.writeHead(200, {
    'Content-Type': 'text/javascript; charset=utf-8',
    'Content-Length': body.length,
    ETag: etag,
    // 与别的 js 一致（no-cache + ETag）。**不能用 no-store** —— 那样它永远是新的，
    // 而"它和别的 js 一起变旧"恰恰是这个探测器的工作原理。
    'Cache-Control': 'no-cache',
    ...ISOLATION_HEADERS,
    ...SECURITY_HEADERS,
  })
  res.end(body)
}

async function serveStatic(req, res, url) {
  let rel = normalize(decodeURIComponent(url.pathname))
  if (rel === '/' || rel === '') rel = '/index.html'
  // 目录穿越：normalize 之后仍可能以 .. 开头（`/../x`），而 join 会把它带出 PUBLIC。
  const file = join(PUBLIC, rel)
  if (!file.startsWith(PUBLIC)) return json(res, 403, { error: 'forbidden' })

  let buf
  let st
  try {
    st = await stat(file)
    if (st.isDirectory()) throw Object.assign(new Error('dir'), { code: 'ENOENT' })
  } catch {
    return json(res, 404, { error: 'not_found', path: rel })
  }

  // `staleguard.js` 要带上这一版的版本号。**发的时候替换，不是构建时写死** ——
  // 版本来自运行时的 `PHOTOAR_VERSION`（镜像 ARG→ENV），而 public/ 是只读的镜像内容。
  // 它跟别的 js 走同一套缓存策略是**故意的**：浏览器手上的包旧了，这个文件也旧，
  // 于是它带的版本号就是旧的 —— 那正是探测要的信号。见 staleguard.js 的模块注释。
  if (rel === '/staleguard.js') return serveStaleGuard(req, res, file, st)

  // 预压的 brotli。见 `pickEncoding` —— 这一条把 11.4MB 的引擎变成 2.43MB。
  const enc = await pickEncoding(file, st, req.headers['accept-encoding'] ?? '')
  try {
    buf = await readFile(enc ? enc.path : file)
  } catch {
    return json(res, 404, { error: 'not_found', path: rel })
  }

  // ETag 里必须带上编码。同一个 URL 的两种字节（压过的和没压的）用同一个 ETag，
  // 中间任何一层缓存都可能把 br 的响应发给一个不接受 br 的客户端 —— 表现是二进制垃圾。
  // 下面还配着 `Vary: Accept-Encoding`，两者是一件事的两半。
  const etag = `"${st.size.toString(16)}-${Math.round(st.mtimeMs).toString(16)}${enc ? '-br' : ''}"`
  if (req.headers['if-none-match'] === etag) {
    res.writeHead(304, { ETag: etag, ...ISOLATION_HEADERS }).end()
    return
  }
  // ── 谁能 immutable：**URL 里带内容版本号的，只有这些** ──────────────
  //
  // `immutable` 的意思是"这个 URL 的内容永远不会变，连条件请求都别发"。所以它的前提
  // 是**换了内容就换 URL**。给一个没有版本号的 URL 发 immutable 是个哑雷：升级之后
  // 已经访问过的浏览器会抱着旧字节不放一年，而新旧混用的表现是"函数签名对不上"，
  // 只在部分用户身上出现。
  //
  // | 资源 | 版本号在哪 | 策略 |
  // |---|---|---|
  // | `opencv.wasm`（11.4MB） | `?v=<sha256 前12>`，由 `tools/split-wasm.mjs` 写进 opencv.js | immutable |
  // | `pixel.woff2`（175KB） | `?v=`，在 theme.css 里手写 —— 重新生成字体要一起改 | immutable |
  // | `opencv.js`（128KB） | **没有** | no-cache + ETag |
  // | 图片（19 张共 37KB） | 没有 | no-cache + ETag |
  //
  // `opencv.js` 以前也在 immutable 名单里（那时注释写着"版本换了就是另一个文件名"——
  // 而实际上并不是，它一直叫 opencv.js）。128KB 换一次条件请求是划算的：那是一个
  // RTT，而且与 wasm 的下载并发。
  const immutable = rel.endsWith('.wasm') || rel.endsWith('.woff2')
  res.writeHead(200, {
    // Content-Type 永远按**原文件**的扩展名算，不是 `.br` 的。压缩是传输层的事：
    // 浏览器解压之后要看到 `application/wasm` 才肯走 instantiateStreaming。
    'Content-Type': MIME[extname(file)] ?? 'application/octet-stream',
    'Content-Length': buf.length,
    ETag: etag,
    'Cache-Control': immutable ? 'public, max-age=31536000, immutable' : 'no-cache',
    ...(enc
      ? {
          'Content-Encoding': 'br',
          // 少了它，任何共享缓存（CDN、公司代理）都可能把压过的字节发给不接受 br 的
          // 客户端。Cloudflare 自己会按 Accept-Encoding 分键，但不能指望链路上每一层都会。
          Vary: 'Accept-Encoding',
          // 进度条要的是**解压后**的总长度。`Content-Length` 是压缩后的（2.43MB），
          // 而 `res.body.getReader()` 给出的是解压后的字节（11.4MB）—— 直接拿
          // Content-Length 当分母，进度会跑到 470%。见 orb.js 的 `prefetch`。
          'X-Uncompressed-Length': String(st.size),
        }
      : {}),
    ...ISOLATION_HEADERS,
    ...SECURITY_HEADERS,
  })
  res.end(buf)
}

/**
 * 有没有一份能用的预压产物。返回 `{path}` 或 null。
 *
 * ## 两道守卫，都不是可选的
 *
 * 1. **客户端要真的接受 br。** 不接受就发原文件 —— 慢，但对。
 * 2. **`.br` 不能比原文件旧。** 这一条防的是"改了源文件、忘了重新压"：那时服务端会
 *    发出**旧代码**，而浏览器解压得到的是完全合法的旧 JS —— 没有任何错误，只是行为
 *    不对，能查一整天。所以宁可放弃压缩也不发可疑的字节。
 *
 * 只有 `tools/split-wasm.mjs` 产出的那两个文件带 `.br`（理由写在那边）。其余静态资源
 * 要么本身压过（woff2/PNG），要么小到不值得。
 */
async function pickEncoding(file, st, accept) {
  if (!/\bbr\b/.test(accept)) return null
  try {
    const brSt = await stat(`${file}.br`)
    if (brSt.mtimeMs + 1 < st.mtimeMs) {
      console.warn(`[web-front] ${file}.br 比源文件旧，忽略它（重跑 npm run wasm:split）`)
      return null
    }
    return { path: `${file}.br`, size: brSt.size }
  } catch {
    return null
  }
}

// ── /api/config ───────────────────────────────────────────────────────
/**
 * 把服务端的识别阈值转给浏览器。
 *
 * 为什么不让浏览器硬编码：`recog.min_inliers` / `ratio` / `top_k` 在服务端是**管理台上
 * 能改的热配置**。浏览器硬编码的后果是管理员调了阈值而网页不跟 —— 表现成"同一张照片
 * App 认得出、网页认不出"，而两边日志都正常。
 *
 * 拿不到就返回空对象，让浏览器用 `consts.js` 里的源码默认值。**不要因此失败**：
 * 那个接口要 admin 权限，而访客本来就没有。
 */
async function serveConfig(req, res) {
  let thresholds = {}
  try {
    const up = await fetchUpstream('/v1/admin/config', req)
    if (up.status === 200) {
      thresholds = pickThresholds(JSON.parse(up.body.toString('utf8')))
    }
  } catch {
    /* 访客拿不到 admin 配置是正常的，用源码默认值 */
  }
  json(res, 200, { thresholds, version: VERSION, upstream: UPSTREAM.origin, isolation: ISOLATION })
}

// ── /api/lib ──────────────────────────────────────────────────────────
/**
 * 把这个用户被授权的照片打成一个识别库包。
 *
 * **授权判定不在这里做** —— 那是服务端的事（顺序有安全含义，见 library.js 的模块说明）。
 * 这里只是拿着用户的 cookie 去问 `/v1/photos`，服务端回什么就投影什么。
 */
async function serveLib(req, res, url) {
  let up
  try {
    up = await fetchUpstream('/v1/photos', req)
  } catch (e) {
    // 连不上上游必须自成一类。落到最外层的 catch 里会变成 `internal`，而那个词
    // 对排查的人零信息 —— 实测（容器里把 upstream 指向一个关着的端口）拿到的就是
    // `{"error":"internal","message":"connect ECONNREFUSED"}`，看不出是配错了地址。
    // 顺手把最常见的那个原因写进去：容器里的 127.0.0.1 是容器自己。
    return json(res, 502, {
      error: 'upstream_unreachable',
      upstream: UPSTREAM.origin,
      message: `连不上 photo-ar 服务端 ${UPSTREAM.origin}：${e.message}。` +
        (UPSTREAM.hostname === '127.0.0.1' || UPSTREAM.hostname === 'localhost'
          ? '注意：容器里的 127.0.0.1 是容器自己，PHOTOAR_UPSTREAM 要填宿主的局域网 IP 或同网络的服务名。'
          : ''),
    })
  }
  if (up.status === 401 || up.status === 403) {
    return json(res, up.status, { error: 'unauthorized', message: '请先登录' })
  }
  if (up.status !== 200) {
    return json(res, 502, { error: 'upstream', status: up.status, body: up.body.toString('utf8').slice(0, 500) })
  }

  let listed
  try {
    listed = JSON.parse(up.body.toString('utf8')).photos ?? []
  } catch (e) {
    return json(res, 502, { error: 'bad_upstream_json', message: e.message })
  }

  const photos = listed.map((p) => ({
    id: p.photoId ?? p.id,
    aspect: numOrNull(p.refAspect ?? p.aspect),
    title: p.title ?? null,
    // `/v1/photo/<id>/media` 是**元信息接口**（返回 JSON，里面才有真正的流地址）。
    // 这里不展开它：那要为每张照片各发一次请求，而只有被命中的那一张需要。
    mediaUrl: p.mediaUrl ?? (p.hasVideo === false ? null : `/v1/photo/${p.photoId ?? p.id}/media`),
    thumbUrl: p.refThumbUrl ?? `/v1/photo/${p.photoId ?? p.id}/thumb`,
  })).filter((p) => p.id)

  try {
    await library.load()
  } catch (e) {
    return json(res, 503, {
      error: 'library_unavailable',
      message: `读不到识别库 ${LIB_DIR}：${e.message}。确认它被挂进容器且里面有 slots.json/desc.bin。`,
    })
  }

  const packed = await library.pack(photos)
  // ETag 用**包的内容哈希**，与服务端 `/v1/targets/db` 那套同一个思路：授权集相同的两个
  // 用户天然共用同一份，而任何一张参考图或授权集变了，ETag 自动变。按 mtime 猜是不精确的。
  const etag = `"${createHash('sha256').update(packed.buf).digest('hex').slice(0, 32)}"`
  if (req.headers['if-none-match'] === etag) {
    res.writeHead(304, { ETag: etag, ...ISOLATION_HEADERS }).end()
    return
  }
  res.writeHead(200, {
    'Content-Type': 'application/octet-stream',
    'Content-Length': packed.buf.length,
    ETag: etag,
    // 必须 no-cache 而不是 max-age：授权集随时会被管理员改，而客户端拿着旧包会
    // "扫不出新加的照片"。no-cache + ETag 让它每次问一句、没变就 304。
    'Cache-Control': 'no-cache',
    'X-Photoar-Photos': String(packed.nPhotos),
    'X-Photoar-Skipped': String(packed.skipped.length),
    ...ISOLATION_HEADERS,
    ...SECURITY_HEADERS,
  })
  res.end(packed.buf)
}

// ── 媒体票据 ──────────────────────────────────────────────────────────
/**
 * 把「要 cookie 才能取的视频」换成一个「URL 自带凭证」的地址。
 *
 * ## 为什么必须这样
 *
 * 真机实测（小米 M2012K11C / Edge for Android 150）：`<video>` 的请求**不是浏览器
 * 自己的网络栈发的**。同一个页面里两次请求打到服务端，`User-Agent` 都不一样 ——
 * 一个是浏览器，一个是安卓平台的媒体组件（MediaExtractor）。而那个组件**拿不到
 * `HttpOnly` 的会话 cookie**：后端日志里是每 3 秒一次、连续十次的
 *
 *     GET /v1/asset/<id>/stream -> 401 (0ms)
 *
 * 而同一页里 `fetch()` 同一个地址是 206。页面上的表现是视频永远 `readyState=0`，
 * 一声不响，没有任何报错。
 *
 * 试过但**不行**的两条路：
 *   - `blob:` URL（先 fetch 回来再喂）—— 那个组件连 blob: 都不认，36ms 直接报
 *     `EDGE_DEMUXER_ERROR_MEDIA_EXTRACTOR_FAILED`。
 *   - 把 cookie 去掉 HttpOnly —— 拿会话安全换一个浏览器的怪癖，不划算。
 *
 * 所以只剩一条：**让凭证进 URL**。浏览器（带着 cookie）先来换一张票，票是一次性的
 * 随机串，媒体组件拿着票来取流，web-front 在服务端把真凭证补上去转发。
 *
 * ## 票据的安全边界
 *
 * - **短命**：10 分钟。视频最长 30 秒，10 分钟足够覆盖重连与拖动，又短到捡到也没用。
 * - **一物一票**：票里钉死了 asset 路径，换不了别的资源。
 * - **不进日志**：票是随机串，不是会话 token —— 泄漏一张票最多让人看到一段视频，
 *   而泄漏会话 token 是整个账号。
 * - **有上限**：`MAX_TICKETS` 挡住「有人狂调 /api/ticket 把内存撑爆」。
 */
const TICKET_TTL_MS = 10 * 60 * 1000
const MAX_TICKETS = 500
/** @type {Map<string, {path: string, cookie: string, auth: string|undefined, exp: number}>} */
const tickets = new Map()

function sweepTickets(now = Date.now()) {
  for (const [k, v] of tickets) if (v.exp <= now) tickets.delete(k)
  // 还超上限就从最老的开始丢（Map 保插入序）。
  while (tickets.size > MAX_TICKETS) tickets.delete(tickets.keys().next().value)
}

/**
 * `GET /api/ticket?path=/v1/asset/<id>/stream` → `{url}`。
 *
 * **这一步是带 cookie 的普通 fetch**，所以鉴权照旧由上游把关：这里先拿调用方的凭证
 * 去上游发一个 1 字节的 Range 探一下，200/206 才发票。不探的话，任何人都能拿一张票
 * 去读任意 asset —— 票本身不鉴权，它只是把已经通过的鉴权结果延长一小段时间。
 */
async function issueTicket(req, res, url) {
  const path = url.searchParams.get('path') ?? ''
  // 只允许 asset 流。别的路径没有"给媒体元素用"的需求，放开就是凭空多一个代理入口。
  if (!/^\/v1\/asset\/[A-Za-z0-9_-]+\/stream$/.test(path)) {
    return json(res, 400, { error: 'bad_path', message: '只能给 /v1/asset/<id>/stream 发票' })
  }
  const cookie = req.headers.cookie ?? ''
  const auth = req.headers.authorization
  let probe
  try {
    probe = await headUpstream(path, cookie, auth)
  } catch (e) {
    return json(res, 502, { error: 'upstream_unreachable', message: e.message })
  }
  if (probe.status === 401 || probe.status === 403) {
    return json(res, probe.status, { error: 'unauthorized', message: '这个会话没有权限取这段视频' })
  }
  if (probe.status >= 400) {
    return json(res, probe.status, { error: 'upstream_error', message: `上游 ${probe.status}` })
  }
  sweepTickets()
  const id = randomUUID().replace(/-/g, '')
  tickets.set(id, { path, cookie, auth, exp: Date.now() + TICKET_TTL_MS })
  json(res, 200, { url: `/api/stream/${id}`, expiresInMs: TICKET_TTL_MS })
}

/** 拿票取流。**不看 cookie** —— 那正是这套机制存在的理由。 */
function serveTicket(req, res, url) {
  const id = url.pathname.slice('/api/stream/'.length)
  const t = tickets.get(id)
  if (!t || t.exp <= Date.now()) {
    tickets.delete(id)
    return json(res, 404, { error: 'ticket_expired', message: '票据不存在或已过期' })
  }
  // 用票里存的凭证去上游取，把调用方自己的 cookie（多半没有）整个丢掉。
  const headers = { ...req.headers }
  delete headers.cookie
  delete headers.authorization
  delete headers['accept-encoding']
  if (t.cookie) headers.cookie = t.cookie
  if (t.auth) headers.authorization = t.auth
  proxyWith(req, res, t.path, headers)
}

/** 探一下上游认不认这个凭证。只取 1 个字节 —— 目的是鉴权，不是取内容。 */
function headUpstream(path, cookie, auth) {
  const isHttps = UPSTREAM.protocol === 'https:'
  const doRequest = isHttps ? httpsRequest : httpRequest
  return new Promise((resolve, reject) => {
    const r = doRequest({
      protocol: UPSTREAM.protocol,
      hostname: UPSTREAM.hostname,
      port: UPSTREAM.port || (isHttps ? 443 : 80),
      method: 'GET',
      path,
      headers: { host: UPSTREAM.host, range: 'bytes=0-0', ...(cookie ? { cookie } : {}), ...(auth ? { authorization: auth } : {}) },
    }, (up) => {
      up.resume()   // 必须把响应读掉，否则连接挂在那儿直到上游 30 秒超时
      resolve({ status: up.statusCode ?? 502 })
    })
    r.on('error', reject)
    r.end()
  })
}

// ── 反代 ──────────────────────────────────────────────────────────────
/**
 * 把 `/v1/*` 原样转给 photo-ar 服务端，**流式**。
 *
 * 不缓冲：视频是几 MB 到十几 MB（服务端实测单条最大 14.72MiB），缓冲整条会让 Node 的
 * 内存随并发线性涨，而婚礼现场就是并发。
 *
 * Range 与 206 原样透传 —— `<video>` 的分段请求全靠它，不透传的话进度条拖动会失效。
 */
function proxy(req, res, url, opts) {
  const headers = { ...req.headers }
  delete headers['accept-encoding'] // 不让上游压缩：我们只转发，解压再压是白付 CPU
  proxyWith(req, res, url.pathname + url.search, headers, opts)
}

/** `proxy` 的内核：路径与请求头由调用方决定（票据那条路要换掉凭证）。 */
function proxyWith(req, res, path, headers, { isolate = true } = {}) {
  const isHttps = UPSTREAM.protocol === 'https:'
  const doRequest = isHttps ? httpsRequest : httpRequest
  // Host 必须换成上游的，否则服务端按我们的 Host 生成的绝对 URL 会指回自己。
  headers = { ...headers, host: UPSTREAM.host }

  const upReq = doRequest(
    {
      protocol: UPSTREAM.protocol,
      hostname: UPSTREAM.hostname,
      port: UPSTREAM.port || (isHttps ? 443 : 80),
      method: req.method,
      path,
      headers,
    },
    (upRes) => {
      const out = { ...upRes.headers, ...(isolate ? ISOLATION_HEADERS : {}) }
      // COEP require-corp 下，**同源资源也要**这个头才能被页面用（视频、缩略图都算）。
      // 少了它，视频在跨源隔离的页面里加载失败，而控制台只说 "net::ERR_BLOCKED"。
      // 管理台那条路（isolate=false）不需要，但给了也无害 —— 它本来就只吃同源资源。
      out['Cross-Origin-Resource-Policy'] = 'same-origin'
      res.writeHead(upRes.statusCode ?? 502, out)
      upRes.pipe(res)
    },
  )
  upReq.on('error', (e) => {
    console.error('[web-front] 上游错误：', e.message)
    if (!res.headersSent) {
      json(res, 502, { error: 'upstream_unreachable', upstream: UPSTREAM.origin, message: e.message })
    } else res.destroy()
  })
  req.pipe(upReq)
}

/** 带着调用方的 cookie 去问上游一个 JSON 接口。用于 `/api/lib` 与 `/api/config`。 */
function fetchUpstream(path, req) {
  const isHttps = UPSTREAM.protocol === 'https:'
  const doRequest = isHttps ? httpsRequest : httpRequest
  return new Promise((resolve, reject) => {
    const r = doRequest(
      {
        protocol: UPSTREAM.protocol,
        hostname: UPSTREAM.hostname,
        port: UPSTREAM.port || (isHttps ? 443 : 80),
        method: 'GET',
        path,
        headers: {
          host: UPSTREAM.host,
          cookie: req.headers.cookie ?? '',
          authorization: req.headers.authorization ?? '',
          accept: 'application/json',
        },
      },
      (up) => {
        const chunks = []
        up.on('data', (c) => chunks.push(c))
        up.on('end', () => resolve({ status: up.statusCode ?? 502, body: Buffer.concat(chunks) }))
      },
    )
    r.on('error', reject)
    r.end()
  })
}

/**
 * 从 `/v1/admin/config` 的响应里挑出浏览器要用的那几个阈值。
 *
 * ## 这里曾经**一个值都没取到**，而且完全无声
 *
 * 上一版写的是 `cfg['recog.min_inliers']?.value`，也就是假设响应是一个以配置键为键的
 * 对象。而服务端返回的是 `{fields: [{key, value, label, help, …}], values: {键: 值}}`
 * —— 两层都不是那个形状。于是每一个 `pick()` 都是 undefined，`thresholds` 被清成
 * 空对象，浏览器**一直在用 `consts.js` 里的源码默认值**。
 *
 * 后果不是"少了个功能"：管理台上那三个识别参数（内点门槛、比值、Top-K）**对网页版
 * 完全无效**，改了没有任何反应；而设置页那一节还标着"服务端热配置"，显示的却是源码
 * 默认值。这个 bug 没有任何症状能把人指向这里 —— 它只是让一个旋钮变成装饰。
 *
 * 所以现在**两种形状都吃**，并且哪个都拿不到时留空（让浏览器用源码默认值）。
 * 读 `values` 优先：它就是为这种用途给的扁平映射。
 */
function pickThresholds(cfg) {
  const flat = cfg?.values && typeof cfg.values === 'object' ? cfg.values : null
  const byKey = Object.create(null)
  if (Array.isArray(cfg?.fields)) {
    for (const f of cfg.fields) if (f?.key) byKey[f.key] = f.value
  }
  const pick = (k) => {
    const v = flat && k in flat ? flat[k] : byKey[k]
    return typeof v === 'number' && Number.isFinite(v) ? v : undefined
  }
  const out = {
    minInliers: pick('recog.min_inliers'),
    ratio: pick('recog.ratio'),
    topK: pick('recog.top_k'),
    // 跨帧累积那两个。服务端 §35 就是按它们做的，浏览器这条管线现在也用同一份数字 ——
    // 两边都从这一个地方取，就不会出现"服务端调了、网页没跟上"。
    streakNeed: pick('recog.streak_need'),
    streakSoftMin: pick('recog.streak_soft_min'),
  }
  for (const k of Object.keys(out)) if (out[k] === undefined) delete out[k]
  return out
}

function json(res, status, body) {
  const buf = Buffer.from(JSON.stringify(body), 'utf8')
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': buf.length,
    'Cache-Control': 'no-store',
    ...ISOLATION_HEADERS,
    ...SECURITY_HEADERS,
  })
  res.end(buf)
}

function numOrNull(v) {
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : null
}

server.listen(PORT, HOST, () => {
  // 打**实际**端口而不是配置值：`PORT=0` 时内核会挑一个，而那时候配置值是 0 ——
  // 日志说 `:0` 等于没说。测试也靠这一行拿端口。
  const actual = server.address()?.port ?? PORT
  const scheme = tls ? 'https' : 'http'
  console.log(`[web-front] ${scheme}://${HOST === '0.0.0.0' ? '127.0.0.1' : HOST}:${actual}`)
  if (!tls) {
    // 这一行是为了省掉一次"手机上打不开相机"的排查：http 下相机根本不存在，
    // 而那与权限、与代码都无关。
    console.log('[web-front] ⚠️  http 模式：只有 localhost 能开相机。手机要么走前面那层' +
      ' https（隧道/反代），要么给 WEBFRONT_TLS_CERT/KEY 让本进程自己说 https。')
  }
  console.log(`[web-front] 上游 ${UPSTREAM.origin}`)
  console.log(`[web-front] 识别库 ${LIB_DIR}`)
  console.log(`[web-front] 跨源隔离 ${ISOLATION ? '开（wasm 线程可用）' : '关'}`)
  console.log(`[web-front] 版本 ${VERSION}`)
})

// 收到 SIGTERM 就停止接新连接并让在飞的请求跑完。容器编排靠它做优雅重启 ——
// 直接退出会让正在下视频的手机看到一个截断的流。
for (const sig of ['SIGTERM', 'SIGINT']) {
  process.on(sig, () => {
    console.log(`[web-front] 收到 ${sig}，停止接新连接`)
    server.close(() => library.close().finally(() => process.exit(0)))
    setTimeout(() => process.exit(0), 10_000).unref()
  })
}

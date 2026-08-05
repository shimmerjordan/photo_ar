#!/usr/bin/env node
/**
 * 在无头 Chrome 里跑一个测试页，把它的结论收回来当进程退出码。
 *
 * 为什么需要这么一层：这个项目里有一半代码**只能在浏览器里跑**（opencv.js 的 wasm、
 * WebGL、getUserMedia）。而其中最关键的那一条 —— ORB 描述子与服务端是否逐位一致 ——
 * 恰恰是不测就等于没做：不一致时它不报错，只是识别率归零。
 *
 * 刻意不引 puppeteer/playwright：那会带进几百 MB 的 node_modules 和一份自己下载的
 * Chromium，而我们要测的正是**本机这个 Chrome**。这里只用到「起个 http 服务 + spawn
 * 一个进程 + 收一个 POST」，标准库就够。
 *
 * 用法：
 *   node test/harness.js test/golden/orb-golden.html [--timeout 120000] [--head]
 *
 * 页面侧的约定见 test/bridge.js。
 */
import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { readFile, mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, extname, normalize, resolve } from 'node:path'

const ROOT = resolve(import.meta.dirname, '..')

/** `--proxy <base>` 时把 /api/* 与 /v1/* 转给那个地址。见静态处理里那段说明。 */
let PROXY = null

/**
 * `--cookie <值>` 注入到每个被代理的请求上。
 *
 * 为的是让测试页能在**已登录**状态下跑 —— 否则只能验到登录门，而"登录之后某一页
 * 挂载时抛异常"是这套多页结构最可能的失败，且它在页面上表现为一片空白。
 *
 * 凭证从命令行来、不进任何文件：这个仓库要进 git。
 */
let COOKIE = ''

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.wasm': 'application/wasm',
  '.css': 'text/css; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.mp4': 'video/mp4',
  '.gray': 'application/octet-stream',
  '.bin': 'application/octet-stream',
}

/**
 * 跨源隔离那两个头一律加上。
 *
 * 不是"以后可能要用"：opencv.js 的 wasm 线程池要 SharedArrayBuffer，而它只在
 * crossOriginIsolated 为真时存在。**如果只在生产环境加这两个头，测试就会在一个比
 * 生产更宽松的环境里跑** —— 于是「线程起不起来」这类问题只能到真机上才暴露。
 * 让测试环境等于生产环境，才有资格拿测试结论说话。
 */
const ISOLATION_HEADERS = {
  'Cross-Origin-Opener-Policy': 'same-origin',
  'Cross-Origin-Embedder-Policy': 'require-corp',
  'Cross-Origin-Resource-Policy': 'same-origin',
}

function chromeBinary() {
  if (process.env.CHROME) return process.env.CHROME
  for (const c of ['google-chrome', 'chromium', 'chromium-browser']) return c
}

async function main() {
  const args = process.argv.slice(2)
  const pagePath = args.find((a) => !a.startsWith('--'))
  if (!pagePath) {
    console.error('用法: node test/harness.js <页面路径，相对 web-front/> [--timeout ms] [--head]')
    process.exit(2)
  }
  const timeoutIdx = args.indexOf('--timeout')
  const timeoutMs = timeoutIdx >= 0 ? Number(args[timeoutIdx + 1]) : 180_000
  const headed = args.includes('--head')
  const proxyIdx = args.indexOf('--proxy')
  if (proxyIdx >= 0) PROXY = args[proxyIdx + 1].replace(/\/+$/, '')
  const cookieIdx = args.indexOf('--cookie')
  if (cookieIdx >= 0) COOKIE = args[cookieIdx + 1]

  let settle
  const done = new Promise((r) => (settle = r))

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, 'http://127.0.0.1')

    if (req.method === 'POST' && url.pathname === '/__log') {
      const body = await readBody(req)
      // 页面的日志走 stderr：stdout 留给最终那份 JSON 结论，好让调用方能 `| jq`。
      process.stderr.write(`[page] ${body}\n`)
      res.writeHead(204, ISOLATION_HEADERS).end()
      return
    }
    if (req.method === 'POST' && url.pathname === '/__result') {
      const body = await readBody(req)
      res.writeHead(204, ISOLATION_HEADERS).end()
      settle(body)
      return
    }

    // 把 /api/* 与 /v1/* 转给一个真的 web-front（`--proxy`）。
    //
    // 为的是让**产品页面本身**能在这个 harness 的源下跑起来 —— 那是"页面到底活没活"
    // 唯一能自动验的办法（curl 拿到 200 只证明服务器发出了字节，而加载链上任何一步的
    // 语法错/路径错/MIME 错都会让页面停在"正在准备…"，HTTP 状态码全是 200）。
    // iframe 要能被读，就必须同源，所以代理而不是让页面跨源加载。
    if (PROXY && (url.pathname.startsWith('/api/') || url.pathname.startsWith('/v1/'))) {
      try {
        const up = await proxyGet(PROXY + url.pathname + url.search, COOKIE || req.headers.cookie || '')
        res.writeHead(up.status, {
          'Content-Type': up.contentType ?? 'application/octet-stream',
          'Content-Length': up.body.length,
          ...ISOLATION_HEADERS,
        }).end(up.body)
      } catch (e) {
        res.writeHead(502, ISOLATION_HEADERS).end(`proxy failed: ${e.message}`)
      }
      return
    }

    // 静态。normalize + 前缀检查挡目录穿越 —— 这是个测试工具，但它 serve 的是仓库根，
    // 而 `..%2f..%2f/etc/passwd` 在测试工具上一样能读到东西。
    const rel = normalize(decodeURIComponent(url.pathname)).replace(/^(\.\.[/\\])+/, '')
    // 两个候选：先按仓库根找（测试页在 test/ 下），找不到再按 public/ 找。
    // 后者让产品页面的**绝对路径**（`/app.js`、`/vendor/opencv.js`）在这里也解得开 ——
    // 而它们必须是绝对路径，因为生产环境的静态根就是 public/。
    const candidates = [join(ROOT, rel), join(ROOT, 'public', rel)]
    for (const file of candidates) {
      if (!file.startsWith(ROOT)) continue
      try {
        const buf = await readFile(file)
        res.writeHead(200, {
          'Content-Type': MIME[extname(file)] ?? 'application/octet-stream',
          'Content-Length': buf.length,
          ...ISOLATION_HEADERS,
        }).end(buf)
        return
      } catch { /* 试下一个候选 */ }
    }
    res.writeHead(404, ISOLATION_HEADERS).end('not found')
  })

  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const port = server.address().port
  const pageUrl = `http://127.0.0.1:${port}/${pagePath.replace(/^\/+/, '')}`

  const profile = await mkdtemp(join(tmpdir(), 'photoar-web-'))
  const chromeArgs = [
    ...(headed ? [] : ['--headless=new']),
    `--user-data-dir=${profile}`,
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--disable-background-networking',
    // SwiftShader：无头环境没有真 GPU，而 WebGL 那部分测试要能起上下文。
    // 加 --enable-unsafe-swiftshader 是因为新版 Chrome 默认不再对软件光栅化开放 WebGL。
    '--enable-unsafe-swiftshader',
    '--use-gl=angle',
    '--use-angle=swiftshader',
    pageUrl,
  ]

  const chrome = spawn(chromeBinary(), chromeArgs, { stdio: ['ignore', 'ignore', 'pipe'] })
  let chromeErr = ''
  chrome.stderr.on('data', (d) => (chromeErr += d))
  chrome.on('error', (e) => settle(JSON.stringify({ ok: false, error: `启动 Chrome 失败: ${e.message}` })))

  const timer = setTimeout(() => {
    settle(JSON.stringify({
      ok: false,
      error: `${timeoutMs}ms 内页面没有回报结果。Chrome stderr 尾部: ${chromeErr.slice(-800)}`,
    }))
  }, timeoutMs)

  const raw = await done
  clearTimeout(timer)
  chrome.kill('SIGKILL')
  server.close()
  await rm(profile, { recursive: true, force: true }).catch(() => {})

  let parsed
  try {
    parsed = JSON.parse(raw)
  } catch {
    console.error('页面回报的不是 JSON:', raw?.slice?.(0, 500))
    process.exit(1)
  }
  console.log(JSON.stringify(parsed, null, 2))
  process.exit(parsed.ok ? 0 : 1)
}

/**
 * 转发一个 GET 给被测服务。
 *
 * 用 `node:http`/`node:https` 而不是全局 `fetch`，就为了一件事：目标可能是 https +
 * **自签证书**（手机自测那套），而 fetch 没有 per-request 关掉证书校验的口子 ——
 * 只能靠 `NODE_TLS_REJECT_UNAUTHORIZED=0` 那种全进程开关。这里打的是 127.0.0.1、
 * 中间没有网络可被中间人，所以在这一个请求上放开校验是对的；把它做成全进程的不是。
 *
 * 不放开的表现是这个代理回 502，而页面上看起来像"模块加载失败"。
 */
async function proxyGet(target, cookie) {
  const url = new URL(target)
  const isHttps = url.protocol === 'https:'
  const { request } = await import(isHttps ? 'node:https' : 'node:http')
  return new Promise((resolve, reject) => {
    const r = request(
      {
        protocol: url.protocol,
        hostname: url.hostname,
        port: url.port || (isHttps ? 443 : 80),
        path: url.pathname + url.search,
        method: 'GET',
        headers: { cookie, accept: '*/*' },
        rejectUnauthorized: false,
      },
      (up) => {
        const chunks = []
        up.on('data', (c) => chunks.push(c))
        up.on('end', () => resolve({
          status: up.statusCode ?? 502,
          contentType: up.headers['content-type'],
          body: Buffer.concat(chunks),
        }))
      },
    )
    r.on('error', reject)
    r.end()
  })
}

function readBody(req) {
  return new Promise((r) => {
    let b = ''
    req.on('data', (c) => (b += c))
    req.on('end', () => r(b))
  })
}

main()

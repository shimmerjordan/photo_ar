/**
 * `server/index.js` 的端到端测试：起一个假的 photo-ar 服务端 + 真的 web-front，
 * 然后打真实的 HTTP。
 *
 * 为什么要假上游而不是连真服务端：这里要验的是 web-front 自己的行为（反代、投影、
 * ETag、错误分类），而那些行为在上游返回 401 / 502 / 空列表时才最容易写错 —— 让真
 * 服务端进入那些状态很麻烦，而且不可重复。
 */
import { test, describe, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { spawn } from 'node:child_process'
import { access, readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'

const HERE = import.meta.dirname
const REPO = resolve(HERE, '../..')
const LIB_DIR = join(REPO, 'data/library')
const hasLib = await access(join(LIB_DIR, 'slots.json')).then(() => true, () => false)

/** 假上游的状态。测试逐条改它，不用重启。 */
const fake = {
  photosStatus: 200,
  photos: [],
  configStatus: 403,
  hits: [],
}

let upstream
let upstreamPort
let web
let webPort

function startUpstream() {
  return new Promise((r) => {
    upstream = createServer((req, res) => {
      fake.hits.push(`${req.method} ${req.url}`)
      if (req.url === '/v1/photos') {
        if (fake.photosStatus !== 200) {
          res.writeHead(fake.photosStatus, { 'Content-Type': 'application/json' })
          return res.end(JSON.stringify({ error: 'unauthorized', message: '请先登录' }))
        }
        res.writeHead(200, { 'Content-Type': 'application/json' })
        return res.end(JSON.stringify({ photos: fake.photos, total: fake.photos.length }))
      }
      if (req.url === '/v1/admin/config') {
        res.writeHead(fake.configStatus, { 'Content-Type': 'application/json' })
        return res.end(JSON.stringify({
          'recog.min_inliers': { value: 44 },
          'recog.ratio': { value: 1.6 },
          'recog.top_k': { value: 12 },
        }))
      }
      if (req.url?.startsWith('/v1/photo/') && req.url.endsWith('/media')) {
        // Range 透传：`<video>` 的分段请求全靠它，不透传的话进度条拖不动。
        const body = Buffer.from('0123456789')
        if (req.headers.range) {
          res.writeHead(206, {
            'Content-Type': 'video/mp4',
            'Content-Range': `bytes 2-4/${body.length}`,
            'Accept-Ranges': 'bytes',
          })
          return res.end(body.subarray(2, 5))
        }
        res.writeHead(200, { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes' })
        return res.end(body)
      }
      // asset 流：**只认 cookie**。这是真机上那个失败的复刻 —— 安卓平台媒体组件
      // 发的请求拿不到 HttpOnly 的会话 cookie，于是每 3 秒一个 401。
      if (req.url?.startsWith('/v1/asset/')) {
        if (!req.headers.cookie?.includes('photoar_session=')) {
          res.writeHead(401, { 'Content-Type': 'application/json' })
          return res.end(JSON.stringify({ error: 'unauthorized' }))
        }
        const body = Buffer.from('MOOVDATA-abcdefghij')
        if (req.headers.range) {
          res.writeHead(206, {
            'Content-Type': 'video/mp4',
            'Content-Range': `bytes 0-0/${body.length}`,
            'Accept-Ranges': 'bytes',
          })
          return res.end(body.subarray(0, 1))
        }
        res.writeHead(200, { 'Content-Type': 'video/mp4', 'Accept-Ranges': 'bytes' })
        return res.end(body)
      }
      if (req.url === '/v1/echo-cookie') {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        return res.end(JSON.stringify({ cookie: req.headers.cookie ?? null, host: req.headers.host }))
      }
      // 管理台。真的那份是 photo-ar 的 `_route_webui` 发的（首页 + 白名单里的几个
      // 静态文件），这里只要证明"这个前缀确实被转过来了、cookie 也在"。
      if (req.url === '/admin' || req.url?.startsWith('/admin/')) {
        res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
        return res.end(`<!doctype html><title>管理台</title>${req.url}|${req.headers.cookie ?? ''}`)
      }
      res.writeHead(404).end('nope')
    })
    upstream.listen(0, '127.0.0.1', () => {
      upstreamPort = upstream.address().port
      r()
    })
  })
}

function startWeb() {
  webPort = 0
  return new Promise((resolve_, reject) => {
    web = spawn(process.execPath, [join(HERE, '../server/index.js')], {
      env: {
        ...process.env,
        PORT: '0', // 让内核挑端口，避免测试之间抢端口
        HOST: '127.0.0.1',
        PHOTOAR_UPSTREAM: `http://127.0.0.1:${upstreamPort}`,
        PHOTOAR_LIBRARY: LIB_DIR,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    })
    let out = ''
    web.stdout.on('data', (d) => {
      out += d
      const m = /http:\/\/127\.0\.0\.1:(\d+)/.exec(out)
      if (m) {
        webPort = Number(m[1])
        resolve_()
      }
    })
    web.stderr.on('data', (d) => process.stderr.write(`[web-front] ${d}`))
    web.on('exit', (c) => { if (!webPort) reject(new Error(`web-front 退出，码 ${c}`)) })
    setTimeout(() => reject(new Error('web-front 10 秒内没有报出端口')), 10_000)
  })
}

const url = (p) => `http://127.0.0.1:${webPort}${p}`

before(async () => {
  await startUpstream()
  // PORT=0 时 Node 会打印实际端口，但 index.js 打印的是配置值。所以这里改成先探
  // 一个空闲端口再传进去 —— 比解析日志更可靠。
  await startWeb()
})

after(() => {
  web?.kill('SIGKILL')
  upstream?.close()
})

describe('静态与安全头', () => {
  test('/ 返回 index.html，并带跨源隔离头', async () => {
    const r = await fetch(url('/'))
    assert.equal(r.status, 200)
    assert.match(r.headers.get('content-type'), /text\/html/)
    // 这两个头是 SharedArrayBuffer（wasm 线程）的前提。少了它们，opencv.js 只能单线程，
    // 而全库检测本来就已经是秒级 —— 所以它们不是可选项。
    assert.equal(r.headers.get('cross-origin-opener-policy'), 'same-origin')
    assert.equal(r.headers.get('cross-origin-embedder-policy'), 'require-corp')
    assert.match(await r.text(), /photo-ar/)
  })

  // ── 缓存策略 ──────────────────────────────────────────────────────
  //
  // 这一组的由来是一次实测：同一台手机同一份代码，自签证书的 https 上打开要 **71 秒**、
  // `http://localhost` 上 **1.6 秒**，差别全在磁盘缓存（Chromium 对有证书错误的源整体
  // 禁用它）。查那件事的过程中发现 `immutable` 给错了对象 —— 见下面第一条。
  test('immutable 只给 URL 里带版本号的资源', async () => {
    // wasm 的版本号是 `?v=<内容哈希>`，由 tools/split-wasm.mjs 写进 opencv.js。
    const w = await fetch(url('/vendor/opencv.wasm'))
    assert.equal(w.status, 200)
    assert.match(w.headers.get('cache-control'), /immutable/)

    // opencv.js **没有**版本号（它一直叫 opencv.js），所以不能 immutable ——
    // 否则升级之后老浏览器会抱着旧的 128KB 配新 wasm，表现是"函数签名对不上"。
    const j = await fetch(url('/vendor/opencv.js'))
    assert.equal(j.status, 200)
    assert.equal(j.headers.get('cache-control'), 'no-cache')

    // vendor 目录本身不再是"整体 immutable"。
    const r = await fetch(url('/vendor/README.md'))
    assert.equal(r.headers.get('cache-control'), 'no-cache')
  })

  test('接受 br 时发预压的 brotli，Content-Type 仍是原文件的', async () => {
    const r = await fetch(url('/vendor/opencv.wasm'), { headers: { 'Accept-Encoding': 'br' } })
    assert.equal(r.status, 200)
    // fetch 会替我们解压，所以这里验的是响应头而不是字节数。
    assert.equal(r.headers.get('content-encoding'), 'br')
    // 解压之后浏览器要看到 application/wasm 才肯走 instantiateStreaming。
    assert.equal(r.headers.get('content-type'), 'application/wasm')
    // 少了 Vary，链路上任何共享缓存都可能把压过的字节发给不接受 br 的客户端。
    assert.match(r.headers.get('vary') ?? '', /accept-encoding/i)
    // 进度条的分母：Content-Length 是压缩后的，这个头是解压后的。
    const raw = Number(r.headers.get('x-uncompressed-length'))
    assert.ok(raw > 11_000_000, `X-Uncompressed-Length = ${raw}`)
    assert.ok((await r.arrayBuffer()).byteLength === raw, '解压后长度要等于这个头')
  })

  test('不接受 br 就发原文件（慢，但对）', async () => {
    // Node 的 fetch 总会带上 Accept-Encoding，所以用底层请求把它显式关掉。
    const { request } = await import('node:http')
    const got = await new Promise((resolve, reject) => {
      const rq = request({ hostname: '127.0.0.1', port: webPort, path: '/vendor/opencv.wasm',
        headers: { 'accept-encoding': 'identity' } }, (up) => {
        up.resume()
        resolve({ enc: up.headers['content-encoding'], len: Number(up.headers['content-length']) })
      })
      rq.on('error', reject)
      rq.end()
    })
    assert.equal(got.enc, undefined)
    assert.ok(got.len > 11_000_000, `原文件长度 ${got.len}`)
  })

  // 这一条守的是最坏的失败模式：改了源文件、忘了重新压，于是服务端发**旧代码**。
  // 浏览器解压得到的是完全合法的旧 JS —— 没有任何报错，只是行为不对，能查一整天。
  test('.br 比源文件旧时忽略它，宁可不压也不发可疑字节', async () => {
    const { writeFile, utimes, unlink } = await import('node:fs/promises')
    const base = join(HERE, '../public/__brguard.js')
    try {
      await writeFile(`${base}.br`, Buffer.from('旧的压缩产物'))
      await writeFile(base, 'export const fresh = 1\n')
      // .br 的 mtime 拨到一小时前
      const old = new Date(Date.now() - 3600_000)
      await utimes(`${base}.br`, old, old)

      const r = await fetch(url('/__brguard.js'), { headers: { 'Accept-Encoding': 'br' } })
      assert.equal(r.status, 200)
      assert.equal(r.headers.get('content-encoding'), null, '旧的 .br 不该被发出去')
      assert.match(await r.text(), /fresh = 1/)
    } finally {
      await unlink(base).catch(() => {})
      await unlink(`${base}.br`).catch(() => {})
    }
  })

  test('两种编码的 ETag 必须不同（否则缓存会串味）', async () => {
    const a = await fetch(url('/vendor/opencv.wasm'), { headers: { 'Accept-Encoding': 'br' } })
    a.body?.cancel()
    const { request } = await import('node:http')
    const plain = await new Promise((resolve, reject) => {
      const rq = request({ hostname: '127.0.0.1', port: webPort, path: '/vendor/opencv.wasm',
        headers: { 'accept-encoding': 'identity' } }, (up) => { up.resume(); resolve(up.headers.etag) })
      rq.on('error', reject)
      rq.end()
    })
    assert.notEqual(a.headers.get('etag'), plain)
    assert.match(a.headers.get('etag'), /-br"$/)
  })

  test('ES module 用正确的 MIME（错了浏览器会拒绝执行）', async () => {
    const r = await fetch(url('/recognize/consts.js'))
    assert.equal(r.status, 200)
    assert.match(r.headers.get('content-type'), /text\/javascript/)
  })

  test('目录穿越拿不到仓库里的东西', async () => {
    for (const p of ['/../server/index.js', '/..%2f..%2fpackage.json', '/../../etc/passwd']) {
      const r = await fetch(url(p))
      assert.ok(r.status === 403 || r.status === 404, `${p} 返回了 ${r.status}`)
    }
  })

  test('/healthz 不碰上游也不碰库', async () => {
    fake.hits.length = 0
    const r = await fetch(url('/healthz'))
    assert.equal(r.status, 200)
    assert.equal((await r.json()).ok, true)
    // 健康检查混进上游连通性会让"上游重启"表现成"web-front 不健康"，
    // 于是编排把好的容器也重启掉。
    assert.deepEqual(fake.hits, [])
  })
})

describe('反代', () => {
  test('cookie 与 Host 都换对了', async () => {
    const r = await fetch(url('/v1/echo-cookie'), { headers: { cookie: 'photoar_session=abc' } })
    const body = await r.json()
    assert.equal(body.cookie, 'photoar_session=abc')
    // Host 必须换成上游的，否则服务端按我们的 Host 生成的绝对 URL 会指回 web-front。
    assert.equal(body.host, `127.0.0.1:${upstreamPort}`)
  })

  test('Range 与 206 原样透传，且带 CORP 头', async () => {
    const r = await fetch(url('/v1/photo/x/media'), { headers: { range: 'bytes=2-4' } })
    assert.equal(r.status, 206)
    assert.equal(r.headers.get('content-range'), 'bytes 2-4/10')
    assert.equal(await r.text(), '234')
    // COEP require-corp 之下，同源资源也要这个头才能被页面用。少了它视频加载失败，
    // 而控制台只说 net::ERR_BLOCKED。
    assert.equal(r.headers.get('cross-origin-resource-policy'), 'same-origin')
  })

  // 合并容器（2026-08-05）之前 `/admin` 不在反代名单里，于是它落到 serveStatic
  // 上 → 404，而网页版「打开管理台」那个按钮一直假设两者同源。这一组盯着它。
  test('/admin 与 /admin/<子页> 都转给上游，cookie 跟着走', async () => {
    for (const p of ['/admin', '/admin/users', '/admin/app.js']) {
      const r = await fetch(url(p), { headers: { cookie: 'photoar_session=abc' } })
      assert.equal(r.status, 200, `${p} 返回了 ${r.status}`)
      assert.equal(await r.text(), `<!doctype html><title>管理台</title>${p}|photoar_session=abc`)
    }
  })

  test('/admin 不加跨源隔离头（那是给网页版的 wasm 的，管理台一个都不用）', async () => {
    const r = await fetch(url('/admin'))
    assert.equal(r.status, 200)
    assert.equal(r.headers.get('cross-origin-opener-policy'), null)
    assert.equal(r.headers.get('cross-origin-embedder-policy'), null)
    // 对比：/v1 那条路是要的
    const v = await fetch(url('/v1/echo-cookie'))
    assert.equal(v.headers.get('cross-origin-embedder-policy'), 'require-corp')
  })

  test('/administrator 这类前缀撞车不会被误转给上游', async () => {
    // `startsWith('/admin')` 会把它也吞掉 —— 判据必须是 `=== '/admin'` 或 `'/admin/'`。
    fake.hits.length = 0
    const r = await fetch(url('/administrator'))
    assert.equal(r.status, 404)
    assert.deepEqual(fake.hits, [])
  })

  test('上游不通时给出可行动的 502，而不是挂住', async () => {
    // 关掉上游再打一次
    await new Promise((r) => upstream.close(r))
    const r = await fetch(url('/v1/echo-cookie'))
    assert.equal(r.status, 502)
    const body = await r.json()
    assert.equal(body.error, 'upstream_unreachable')
    assert.match(body.upstream, /127\.0\.0\.1/)
    // 重新起回来，后面的测试还要用
    await startUpstream()
    web.kill('SIGKILL')
    await startWeb()
  })
})

describe('/api/config', () => {
  test('拿不到 admin 配置时返回空阈值，而不是失败', async () => {
    fake.configStatus = 403
    const r = await fetch(url('/api/config'))
    assert.equal(r.status, 200)
    // 访客本来就没有 admin 权限。这里失败的话整个页面起不来，而浏览器用源码默认值
    // 完全能工作。
    assert.deepEqual((await r.json()).thresholds, {})
  })

  test('拿到了就转出来（管理台改的阈值必须能传到浏览器）', async () => {
    fake.configStatus = 200
    const r = await fetch(url('/api/config'))
    const { thresholds } = await r.json()
    assert.equal(thresholds.minInliers, 44)
    assert.equal(thresholds.ratio, 1.6)
    assert.equal(thresholds.topK, 12)
    fake.configStatus = 403
  })
})

describe('/api/lib', { skip: !hasLib && '没有 data/library/' }, () => {
  test('未登录时把 401 如实传下去', async () => {
    fake.photosStatus = 401
    const r = await fetch(url('/api/lib'))
    assert.equal(r.status, 401)
    assert.equal((await r.json()).error, 'unauthorized')
    fake.photosStatus = 200
  })

  test('空授权集也要给出一个合法的包（页面靠 nPhotos=0 提示"还没有照片"）', async () => {
    fake.photos = []
    const r = await fetch(url('/api/lib'))
    assert.equal(r.status, 200)
    const buf = Buffer.from(await r.arrayBuffer())
    assert.equal(buf.subarray(0, 4).toString('latin1'), 'PARL')
    assert.equal(buf.readUInt32LE(8), 0)
    assert.equal(r.headers.get('x-photoar-photos'), '0')
  })

  test('按服务端给的授权集投影，字段映射对得上 /v1/photos 的形状', async () => {
    const slots = JSON.parse(await readFile(join(LIB_DIR, 'slots.json'), 'utf8'))
    const live = slots.photo_ids.filter((x) => x !== '')
    assert.ok(live.length > 0, '真实库里没有活的 slot，这条测不了')

    fake.photos = live.map((id, i) => ({
      photoId: id,
      title: `照片${i}`,
      refAspect: 1.5,
      refThumbUrl: `/v1/photo/${id}/thumb`,
      hasVideo: i % 2 === 0,
    }))
    const r = await fetch(url('/api/lib'))
    assert.equal(r.status, 200)
    const buf = Buffer.from(await r.arrayBuffer())
    assert.equal(buf.readUInt32LE(8), live.length)
    const jsonBytes = buf.readUInt32LE(28)
    const meta = JSON.parse(buf.subarray(32, 32 + jsonBytes).toString('utf8'))
    assert.equal(meta.photos.length, live.length)
    assert.equal(meta.photos[0].title, '照片0')
    assert.equal(meta.photos[0].aspect, 1.5)
    // hasVideo=false 的那些不该有 mediaUrl —— 页面要靠它显示"还没配视频"而不是
    // 去请求一个 404 的地址。
    assert.equal(meta.photos[0].mediaUrl, `/v1/photo/${live[0]}/media`)
    if (live.length > 1) assert.equal(meta.photos[1].mediaUrl, null)
  })

  test('ETag 按内容算，授权集不变就 304', async () => {
    const r1 = await fetch(url('/api/lib'))
    const etag = r1.headers.get('etag')
    assert.ok(etag, '没有 ETag：客户端每次都要重下整个库')
    const r2 = await fetch(url('/api/lib'), { headers: { 'if-none-match': etag } })
    assert.equal(r2.status, 304)
  })

  test('授权集变了 ETag 必须变（否则用户扫不出新加的照片）', async () => {
    const before = (await fetch(url('/api/lib'))).headers.get('etag')
    fake.photos = fake.photos.slice(0, Math.max(1, fake.photos.length - 1))
    const after = (await fetch(url('/api/lib'))).headers.get('etag')
    assert.notEqual(after, before)
  })

  test('上游连不上时给 502 + 可行动的原因，而不是笼统的 internal', async () => {
    // 这一条是容器里实测出来的：上游指向一个关着的端口时，原来返回
    // {"error":"internal","message":"connect ECONNREFUSED"} —— 排查的人看不出是配错地址。
    await new Promise((r) => upstream.close(r))
    const r = await fetch(url('/api/lib'))
    assert.equal(r.status, 502)
    const body = await r.json()
    assert.equal(body.error, 'upstream_unreachable')
    assert.match(body.message, /连不上/)
    await startUpstream()
    web.kill('SIGKILL')
    await startWeb()
  })

  test('库里没有的照片 id 被如实报成 skipped，而不是静默丢掉', async () => {
    fake.photos = [{ photoId: 'ffffffffffffffffffffffffffffffff', refAspect: 1.5 }]
    const r = await fetch(url('/api/lib'))
    assert.equal(r.headers.get('x-photoar-skipped'), '1')
    const buf = Buffer.from(await r.arrayBuffer())
    const meta = JSON.parse(buf.subarray(32, 32 + buf.readUInt32LE(28)).toString('utf8'))
    assert.equal(meta.skipped[0].reason, 'not_in_library')
  })
})

/**
 * 媒体票据。**这一组盯的是真机上那个只在手机上出现的失败。**
 *
 * 小米 M2012K11C / Edge for Android 150 上实测：`<video>` 的请求不是浏览器自己的网络栈
 * 发的（同一页两次请求的 `User-Agent` 都不一样），而那个安卓平台媒体组件**拿不到
 * `HttpOnly` 的会话 cookie** —— 后端日志里是每 3 秒一次、连续十次的 401，
 * 而页面上视频永远 `readyState=0`，一声不响。
 *
 * 所以这些用例全部**故意不带 cookie 去取流**：那正是手机上真实发生的事。
 */
describe('媒体票据', () => {
  const RAW = '/v1/asset/7b6ca9e51b604eeab48c1eb1f674e69e/stream'
  const ask = (path, headers = {}) =>
    fetch(url(`/api/ticket?path=${encodeURIComponent(path)}`), { headers })

  test('没有会话就换不到票', async () => {
    const r = await ask(RAW)
    assert.equal(r.status, 401)
  })

  test('带着会话能换到票，且票不是会话 token 本身', async () => {
    const r = await ask(RAW, { cookie: 'photoar_session=SECRET-TOKEN' })
    assert.equal(r.status, 200)
    const body = await r.json()
    assert.match(body.url, /^\/api\/stream\/[0-9a-f]{32}$/)
    // 票据泄漏最多丢一段视频；会话 token 泄漏是整个账号。两者绝不能是同一个串。
    assert.ok(!body.url.includes('SECRET-TOKEN'))
  })

  test('**不带 cookie**也能凭票取到流（这正是手机上做不到的那件事）', async () => {
    const t = await (await ask(RAW, { cookie: 'photoar_session=SECRET-TOKEN' })).json()
    const r = await fetch(url(t.url))          // 注意：一个头都不带
    assert.equal(r.status, 200)
    assert.equal(r.headers.get('content-type'), 'video/mp4')
    assert.equal(await r.text(), 'MOOVDATA-abcdefghij')
  })

  test('凭票取流时 Range 照样透传（拖进度条要用）', async () => {
    const t = await (await ask(RAW, { cookie: 'photoar_session=SECRET-TOKEN' })).json()
    const r = await fetch(url(t.url), { headers: { Range: 'bytes=0-0' } })
    assert.equal(r.status, 206)
    assert.equal(r.headers.get('content-range'), 'bytes 0-0/19')
  })

  test('伪造的票是 404，不是 500 也不是放行', async () => {
    const r = await fetch(url('/api/stream/deadbeefdeadbeefdeadbeefdeadbeef'))
    assert.equal(r.status, 404)
  })

  test('票只能钉在 asset 流上 —— 不能拿它当万能代理', async () => {
    // 放开路径的话，任何人都能用一张票去读 /v1/photos、/v1/admin/*。
    for (const bad of ['/v1/photos', '/v1/admin/config', '/v1/asset/x/stream/../../photos', '/etc/passwd']) {
      const r = await ask(bad, { cookie: 'photoar_session=SECRET-TOKEN' })
      assert.equal(r.status, 400, `${bad} 不该发得出票`)
    }
  })

  test('上游拒绝（401/403）时不发票 —— 票不能凭空放大权限', async () => {
    const r = await ask(RAW, { cookie: 'nothing=here' })
    assert.equal(r.status, 401)
  })
})

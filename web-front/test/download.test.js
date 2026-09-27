/**
 * 存到手机（download.js）与装载阶段（mediaload.js）里的纯函数。
 *
 * 这两组各自守着一个**只在真机上才现形**的错：
 *
 * - 文件名：标题是用户填的，带斜杠的话 `download="a/b.jpg"` 在部分浏览器上会被整个
 *   丢弃 —— 表现是"点了没反应"，而代码里一切正常。
 * - 后缀：入库允许 PNG/WebP，一律写 `.jpg` 的话存进相册就是一张打不开的图。
 * - 进度分母：`Content-Length` 拿不到时是 0，拿它当分母得到的是 Infinity ——
 *   进度条会直接跳满然后停在那儿，看起来像"卡在 100%"。
 *
 * 末尾一组钉「存到手机」接进 mediastore 之后的四条路（设计 §3.1）：本机有 → 零网络；
 * 正在下 → 等它、不下第二遍；开了数据源 → 走任务（顺便落缓存）；都不是 → 照旧交给
 * 浏览器下载器。假 `document`（只记录被点的锚点）+ `mediastore._setEnv` 注入的假 fetch/caches。
 */
import { test, describe, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { extFromContentType, safeFileName, savePhotoImage, savePhotoVideo } from '../public/download.js'
import { Stage, mediaInfo, stageName, stagePct, stageText } from '../public/mediaload.js'
import * as M from '../public/mediastore.js'

describe('extFromContentType', () => {
  test('认得出入库允许的那几种图片', () => {
    assert.equal(extFromContentType('image/jpeg'), '.jpg')
    assert.equal(extFromContentType('image/png'), '.png')
    assert.equal(extFromContentType('image/webp'), '.webp')
  })

  test('带参数与大小写都要吃下 —— 服务端可能发 `image/JPEG; charset=binary`', () => {
    assert.equal(extFromContentType('image/JPEG; charset=binary'), '.jpg')
    assert.equal(extFromContentType('  Image/Png  '), '.png')
  })

  test('认不出来的退回 fallback，而不是留空', () => {
    // 没有后缀的文件在安卓上点开是"未知格式"，那与"下载坏了"分不开。
    assert.equal(extFromContentType('application/octet-stream'), '.jpg')
    assert.equal(extFromContentType(null, '.mp4'), '.mp4')
    assert.equal(extFromContentType(undefined, '.mp4'), '.mp4')
  })
})

describe('safeFileName', () => {
  test('斜杠必须清 —— 带斜杠的 download 属性会被整个丢弃', () => {
    const n = safeFileName('婚礼/第一支舞', 'abc12345', '.mp4')
    assert.ok(!n.includes('/'), n)
    assert.ok(!n.includes('\\'), n)
  })

  test('Windows 保留字符也清', () => {
    const n = safeFileName('a:b"c|d?e*f<g>h', 'x', '.jpg')
    for (const c of ':"|?*<>') assert.ok(!n.includes(c), `${c} 没清掉：${n}`)
  })

  test('连字符与下划线保留 —— 那是标题里正常的字', () => {
    assert.equal(safeFileName('婚礼-第一支舞_终版', 'x', '.mp4'), '婚礼-第一支舞_终版.mp4')
  })

  test('空标题退回 photoId 前 8 位，至少是个能对上号的名字', () => {
    assert.equal(safeFileName('   ', 'abcdef1234567890', '.mp4'), '照片-abcdef12.mp4')
    assert.equal(safeFileName(null, 'abcdef1234567890', '.jpg'), '照片-abcdef12.jpg')
  })

  test('超长标题截断，且后缀一定还在', () => {
    const n = safeFileName('长'.repeat(200), 'x', '.jpg')
    assert.ok(n.endsWith('.jpg'), n)
    assert.ok(n.length <= 64, `太长了：${n.length}`)
  })

  test('连续空白压成一个 —— 清完保留字符之后会留下一串空格', () => {
    assert.equal(safeFileName('a  //  b', 'x', '.jpg'), 'a b.jpg')
  })
})

describe('stagePct：只有下载那一步有真百分比', () => {
  test('下载：loaded / total', () => {
    assert.equal(stagePct(Stage.DOWNLOAD, { loaded: 50, total: 200 }), 0.25)
  })

  test('没有分母时返回 null，而不是 Infinity', () => {
    // 这一条防的是"卡在 100%"：total=0 时 loaded/0 是 Infinity，
    // clamp 之后条子直接铺满然后不动了。
    assert.equal(stagePct(Stage.DOWNLOAD, { loaded: 50, total: 0 }), null)
    assert.equal(stagePct(Stage.DOWNLOAD, { loaded: 50 }), null)
  })

  test('超过 100% 也钳住 —— 分块解压时 loaded 会超过 Content-Length', () => {
    assert.equal(stagePct(Stage.DOWNLOAD, { loaded: 300, total: 200 }), 1)
  })

  test('其余阶段一律 null（不定长），不编数字', () => {
    for (const s of [Stage.INFO, Stage.TICKET, Stage.BUFFER, Stage.UNAVAILABLE]) {
      assert.equal(stagePct(s, { loaded: 1, total: 2 }), null, `${s} 不该有百分比`)
    }
  })
})

describe('stageText', () => {
  test('命中预取缓存时明说「本机已有」', () => {
    // 不说的话用户会以为"今天网络真好"，而那正是预取兑现的时刻 —— 值得说出来。
    assert.match(stageText(Stage.DOWNLOAD, { fromCache: true, loaded: 1, total: 2 }), /本机已有/)
  })

  test('有分母时报 已收到 / 总共', () => {
    const t = stageText(Stage.DOWNLOAD, { loaded: 1048576, total: 8388608 })
    assert.equal(t, '1.0 / 8.0 MB')
  })

  test('没有分母时报绝对量，不报百分比', () => {
    assert.equal(stageText(Stage.DOWNLOAD, { loaded: 2097152, total: 0 }), '已收到 2.0 MB')
  })

  test('每个非播放阶段都有一句能读的话', () => {
    for (const s of [Stage.INFO, Stage.TICKET, Stage.BUFFER]) {
      assert.ok(stageText(s).length > 2, `${s} 没有文案`)
    }
  })
})

describe('stageName', () => {
  test('每个非播放阶段都有名字', () => {
    for (const s of [Stage.INFO, Stage.TICKET, Stage.DOWNLOAD, Stage.BUFFER]) {
      assert.ok(stageName(s).length > 2, `${s} 没有阶段名`)
    }
  })

  test('下载档：名字与数字是两句不同的话', () => {
    // 这条分工是三个页面各踩过一次的坑：只显示 `stageText`（`3.2 / 8.1 MB`）读不出
    // 这是在下载视频（宾客页），而名字与数字两处都写 `stageText` 会把同一句显示两遍
    // （试播页）。所以名字必须与数字**不相等**，两者拼起来才是一行完整的话。
    assert.equal(stageName(Stage.DOWNLOAD), '正在下载视频')
    assert.notEqual(stageName(Stage.DOWNLOAD), stageText(Stage.DOWNLOAD, { loaded: 1, total: 2 }))
  })

  test('播放与终局没有阶段名 —— 那几档的话由页面自己说', () => {
    for (const s of [Stage.PLAYING, Stage.UNAVAILABLE, Stage.ERROR]) {
      assert.equal(stageName(s), '', `${s} 不该有阶段名`)
    }
  })

  test('缓存命中时名字不再是「正在下载视频」', () => {
    // 「正在下载视频」与 stageText 同一档给出的「本机已有，秒开」自相矛盾——
    // 一句说在下，一句说已经有了。三个页面都拼过这两句，见 mediaload.js 的说明。
    assert.notEqual(stageName(Stage.DOWNLOAD, { fromCache: true }), '正在下载视频')
    assert.ok(stageName(Stage.DOWNLOAD, { fromCache: true }).length > 2)
  })
})

// ── 存到手机 × mediastore 的假件 ──────────────────────────────────────────
const store = new Map()
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }
function fakeCaches() {
  const all = new Map()
  const open = async (name) => {
    if (!all.has(name)) all.set(name, new Map())
    const m = all.get(name)
    return {
      match: async (k) => (m.has(k) ? m.get(k).clone() : undefined),
      put: async (k, r) => { m.set(k, new Response(await r.arrayBuffer(), { headers: r.headers })) },
      delete: async (k) => m.delete(k),
      keys: async () => [...m.keys()].map((k) => new Request(`http://x${k}`)),
    }
  }
  return { open, delete: async (n) => all.delete(n), keys: async () => [...all.keys()] }
}
const bytes = (...xs) => new Uint8Array(xs)
const media = (id, n) => ({ assetId: id, url: `/v1/asset/${id}/stream`, via: 'nas_serve', absolute: false, bytes: n, missing: false })
async function until(cond, ms = 3000) {
  const t0 = Date.now()
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error('等不到条件成立')
    await new Promise((r) => setTimeout(r, 2))
  }
}
/**
 * 装一个只记录「点了哪个锚点」的假 document，跑完 `fn` 再拆掉。
 *
 * 顺带拦掉 `triggerDownload` 那只 60 秒后 revoke 的定时器：不拦的话这个测试进程要多挂
 * 一分钟才退出（Node 等事件循环空了才走）。只拦 60000ms 这一种，别的定时器照常。
 */
async function withDom(fn) {
  const clicks = []
  const realSetTimeout = globalThis.setTimeout
  globalThis.document = {
    createElement: () => ({ style: {}, click() { clicks.push({ href: this.href, download: this.download }) }, remove() {} }),
    body: { appendChild() {} },
  }
  globalThis.setTimeout = (f, ms, ...a) => (ms === 60_000 ? 0 : realSetTimeout(f, ms, ...a))
  try {
    return { result: await fn(), clicks }
  } finally {
    globalThis.setTimeout = realSetTimeout
    delete globalThis.document
  }
}

describe('存到手机：本机有就零网络、正在下就等它、开了数据源就顺便缓存', () => {
  let caches, calls, next, realFetch
  beforeEach(() => {
    store.clear()
    caches = fakeCaches()
    calls = []
    M._setEnv({ cachesImpl: caches, fetchImpl: async (url, init) => { calls.push(url); return next(url, init) } })
    realFetch = globalThis.fetch
  })
  const restoreFetch = () => { globalThis.fetch = realFetch }

  test('视频已在本机缓存：一个字节都不走网络', async () => {
    await mediaInfo('p-dl-a', async () => media('dl-a', 2))
    await (await caches.open(M.VIDEO_CACHE)).put('/v1/asset/dl-a/stream',
      new Response(bytes(1, 2), { headers: { 'content-type': 'video/mp4', 'content-length': '2' } }))
    const { result, clicks } = await withDom(() => savePhotoVideo('p-dl-a', '第一支舞'))
    assert.equal(result, '本机已有这段视频，没走网络')
    assert.deepEqual(calls, [])
    assert.equal(clicks.length, 1)
    assert.match(clicks[0].href, /^blob:/)
    assert.equal(clicks[0].download, '第一支舞.mp4')
  })

  test('视频正在被预取：等那个任务下完再存，不另下一遍，进度跟着任务走', async () => {
    await mediaInfo('p-dl-b', async () => media('dl-b', 2))
    let ctl
    next = () => new Response(new ReadableStream({ start(c) { ctl = c } }), { headers: { 'content-length': '2', 'content-type': 'video/mp4' } })
    M.download('/v1/asset/dl-b/stream', { priority: M.Priority.BACKGROUND })
    await until(() => ctl)
    const seen = []
    const saving = withDom(() => savePhotoVideo('p-dl-b', '敬酒', { onProgress: (x) => seen.push(x) }))
    await until(() => seen.length > 0)
    ctl.enqueue(bytes(3)); ctl.enqueue(bytes(4)); ctl.close()
    const { result, clicks } = await saving
    assert.equal(calls.length, 1, '只下了一次')
    assert.equal(clicks.length, 1)
    assert.match(clicks[0].href, /^blob:/)
    assert.equal(clicks[0].download, '敬酒.mp4')
    assert.ok(seen.some((x) => x.loaded === 2 && x.total === 2), JSON.stringify(seen))
    assert.match(result, /下载目录/)
  })

  test('开了数据源：走下载任务（经局域网）再存 blob，顺便落进本机缓存', async () => {
    store.set('photoar.adv.v1', JSON.stringify({ mediaBaseOn: true, mediaBase: 'http://192.168.1.10:8964' }))
    await mediaInfo('p-dl-c', async () => media('dl-c', 2))
    globalThis.fetch = async () => new Response(JSON.stringify({ url: '/api/stream/T1' }), { headers: { 'content-type': 'application/json' } })
    next = () => new Response(bytes(5, 6), { headers: { 'content-length': '2', 'content-type': 'video/mp4' } })
    try {
      const { result, clicks } = await withDom(() => savePhotoVideo('p-dl-c', '入场'))
      assert.deepEqual(calls, ['http://192.168.1.10:8964/api/stream/T1'])
      assert.equal(clicks.length, 1)
      assert.match(clicks[0].href, /^blob:/)
      assert.ok(await M.cached('/v1/asset/dl-c/stream'), '下下来的那份要留在本机缓存里')
      assert.match(result, /本机/)
    } finally {
      restoreFetch()
    }
  })

  test('原图已在本机缓存：从 photoar-ref-v1 存，后缀跟着 Content-Type 走', async () => {
    await (await caches.open(M.REF_CACHE)).put('/v1/photo/p-img-a/ref',
      new Response(bytes(7), { headers: { 'content-type': 'image/webp', 'content-length': '1' } }))
    const { result, clicks } = await withDom(() => savePhotoImage('p-img-a', '合影'))
    assert.deepEqual(calls, [])
    assert.equal(clicks[0].download, '合影.webp')
    assert.match(clicks[0].href, /^blob:/)
    assert.match(result, /本机已有/)
  })

  test('原图：本机没有、没开数据源 → 照旧交给浏览器下载器（HEAD 问一次后缀）', async () => {
    const heads = []
    globalThis.fetch = async (url, init) => { heads.push([url, init.method]); return new Response(null, { headers: { 'content-type': 'image/png' } }) }
    try {
      const { result, clicks } = await withDom(() => savePhotoImage('p-img-b', '合影'))
      assert.deepEqual(heads, [['/v1/photo/p-img-b/ref', 'HEAD']])
      assert.deepEqual(calls, [])
      assert.deepEqual(clicks, [{ href: '/v1/photo/p-img-b/ref', download: '合影.png' }])
      assert.equal(result, '已交给浏览器下载，去下载目录里找')
    } finally {
      restoreFetch()
    }
  })

  // ── fix1 #2：挂到别人的任务上，那个任务最终失败不该直接抛错 ──────────────
  test('视频正在被预取，但那个任务最终失败：退回浏览器下载器（旧代码这种情况本来能成功，fix1 #2）', async () => {
    await mediaInfo('p-dl-fail', async () => media('dl-fail', 2))
    next = () => { throw new Error('network down') }        // 每次来源都连不上
    M.download('/v1/asset/dl-fail/stream', { priority: M.Priority.BACKGROUND })
    await until(() => calls.length >= 1)                     // 任务已经在跑（挂得上去）
    const { result, clicks } = await withDom(() => savePhotoVideo('p-dl-fail', '失败重下'))
    assert.match(result, /已交给浏览器下载/, `不该抛错，实际：${result}`)
    assert.equal(clicks.length, 1)
    assert.equal(clicks[0].href, '/v1/asset/dl-fail/stream')
  })

  test('单个来源是直链，下载失败时套用与播放一致的 CORS 提示，不是抛原始错误（fix1 #2）', async () => {
    store.set('photoar.adv.v1', JSON.stringify({ overrides: { 'p-ov-fail': { kind: 'url', url: 'https://cdn.example.com/v.mp4' } } }))
    next = () => { throw new Error('network down') }
    await assert.rejects(
      () => savePhotoVideo('p-ov-fail', '失败'),
      (e) => {
        assert.match(e.message, /对方服务器要允许跨域（CORS）/, `文案没套用，实际：${e.message}`)
        return true
      },
    )
  })
})

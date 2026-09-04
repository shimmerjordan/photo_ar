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
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { extFromContentType, safeFileName } from '../public/download.js'
import { Stage, stageName, stagePct, stageText } from '../public/mediaload.js'

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

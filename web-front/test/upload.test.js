/**
 * 上传的**线上契约**。这一整个文件是为一个 bug 写的。
 *
 * 素材页的上传从来没成功过，而且**没有任何迹象**：
 *
 * 1. 服务端见到 `CF-Ray` 就一律 413（不看体积），网页的正常路径恰好就是隧道 ——
 *    于是所有人看到的都是那句「超过 100MB 上限」，包括传 5MB 的时候。
 * 2. 413 修掉之后才露出第二层：`upload()` 发的是 `FormData` 且不带 `?name=`，
 *    而服务端要的是 `?name=<纯文件名>` + **原始字节**（它直接 `stream_to(dst)`）。
 *    → 400 `missing_name`；就算补上名字，落地的也是一个把 multipart 边界写进去的坏文件。
 * 3. `uploadCheck()` 发的是 `GET ?sha256=&bytes=`，而路由是 **POST + JSON body**
 *    且必须带 `name`。调用方把它包在空 `catch {}` 里（"判重失败不该阻止上传"），
 *    所以这一层**从来没报过错，也从来没工作过**。
 *
 * 三条都是"客户端和服务端各自都自洽、合起来对不上"，单看任何一边都发现不了。所以这里
 * 拦的不是逻辑，是**报文形状** —— 方法、路径、query、body 的类型。
 *
 * 服务端那一侧的对应断言在 `tests/server/test_app.py`（`test_upload_*`）。
 */
import { test, describe, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { upload, uploadCheck, uploadName } from '../public/api.js'

describe('uploadName：清成服务端肯收的纯文件名', () => {
  test('干净的名字原样通过', () => {
    assert.equal(uploadName('IMG_1234.jpg'), 'IMG_1234.jpg')
    assert.equal(uploadName('婚礼-迎宾.mp4'), '婚礼-迎宾.mp4')
  })

  test('切掉目录成分', () => {
    // 服务端对带路径的名字是**拒**不是洗（app.py 的 `_upload`），所以必须在这边切。
    assert.equal(uploadName('Camera/IMG_1234.jpg'), 'IMG_1234.jpg')
    assert.equal(uploadName('/a/b/c.mp4'), 'c.mp4')
  })

  test('反斜杠也切', () => {
    // 服务端在 posix 上跑：`Path('a\\b.jpg').name` 还是它自己，能过校验，
    // 然后落地成一个带反斜杠的文件名。能用，但没人想要那个名字。
    assert.equal(uploadName('C:\\Users\\x\\v.mp4'), 'v.mp4')
  })

  test('去掉开头的点 —— 服务端见到就是 400 bad_name', () => {
    assert.equal(uploadName('.hidden.jpg'), 'hidden.jpg')
    assert.equal(uploadName('..\\..\\etc\\passwd'), 'passwd')
  })

  test('清空了给个兜底名，不发空 name', () => {
    // 空 name 在服务端是 400 missing_name。给兜底名让上传能成，比让它失败好 ——
    // 用户随后能在照片库里改标题，但传不上去就什么都没有。
    assert.equal(uploadName(''), 'upload.bin')
    assert.equal(uploadName('...'), 'upload.bin')
    assert.equal(uploadName('   '), 'upload.bin')
    assert.equal(uploadName(null), 'upload.bin')
  })
})

describe('uploadCheck 的报文形状', () => {
  let seen
  const realFetch = globalThis.fetch

  beforeEach(() => {
    seen = null
    globalThis.fetch = async (path, init) => {
      seen = { path, init }
      return new Response(JSON.stringify({ name: 'a.jpg', matches: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    }
  })
  afterEach(() => { globalThis.fetch = realFetch })

  test('是 POST + JSON body，不是 GET + query', async () => {
    await uploadCheck('a.jpg', 'f'.repeat(64), 123)
    assert.equal(seen.path, '/v1/upload/check')
    assert.equal(seen.init.method, 'POST')
    assert.ok(!seen.path.includes('?'), '参数不该在 query 上')
    assert.deepEqual(JSON.parse(seen.init.body), {
      name: 'a.jpg', sha256: 'f'.repeat(64), bytes: 123,
    })
  })

  test('name 是必填的 —— 少了它服务端 400', async () => {
    // 这一条盯的就是原来那个签名 `uploadCheck(sha256, bytes)`：它压根没有 name。
    await uploadCheck('Camera/a.jpg', null, 1)
    const body = JSON.parse(seen.init.body)
    assert.equal(body.name, 'a.jpg', 'name 要先过 uploadName')
  })

  test('算不出 sha256 时不发 sha256 字段（服务端只做按名字那一半）', async () => {
    // http 页面上 `crypto.subtle` 不存在。发 `sha256: null` 会被服务端的
    // `bad_sha256` 校验挡下来，那样连"按名字"那一半都没了。
    await uploadCheck('a.jpg', null, 1)
    assert.ok(!('sha256' in JSON.parse(seen.init.body)))
  })
})

describe('upload 的报文形状', () => {
  const realXHR = globalThis.XMLHttpRequest
  let sent

  class FakeXHR {
    constructor() { this.upload = {}; this.status = 201; this.responseText = '{"path":"/nas/a.jpg"}' }
    open(method, url) { sent = { method, url } }
    send(body) { sent.body = body; queueMicrotask(() => this.onload()) }
  }

  beforeEach(() => { sent = null; globalThis.XMLHttpRequest = FakeXHR })
  afterEach(() => { globalThis.XMLHttpRequest = realXHR })

  test('文件名走 ?name=，请求体是文件本身', async () => {
    const file = new File(['xx'], 'IMG_1.jpg')
    await upload(file)
    assert.equal(sent.method, 'POST')
    assert.equal(sent.url, '/v1/upload?name=IMG_1.jpg')
    // **不是 FormData。** 服务端 `stream_to(dst)` 把请求体原样写进目标文件，
    // multipart 的边界和头会一起进去，落地一个坏文件。
    assert.ok(sent.body instanceof File || sent.body instanceof Blob, '要发原始文件')
    assert.ok(!(sent.body instanceof FormData))
  })

  test('中文名与空格要 URL 编码，否则 query 解析错位', async () => {
    await upload(new File(['x'], '迎宾 视频.mp4'))
    assert.equal(sent.url, `/v1/upload?name=${encodeURIComponent('迎宾 视频.mp4')}`)
  })

  test('调用方能指定名字 —— 撞名时要改传成服务端给的建议名', async () => {
    await upload(new File(['x'], 'a.jpg'), { name: 'a-2.jpg' })
    assert.equal(sent.url, '/v1/upload?name=a-2.jpg')
  })

  test('指定的名字同样过一遍 uploadName', async () => {
    await upload(new File(['x'], 'a.jpg'), { name: '../a-2.jpg' })
    assert.equal(sent.url, '/v1/upload?name=a-2.jpg')
  })
})

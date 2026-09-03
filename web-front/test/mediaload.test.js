/**
 * `mediaload.js` 的媒体元信息缓存（`mediaInfo` / `forgetMedia`）。
 *
 * `api.mediaOfPhoto` 在扫描页、试播页、预取、下载四处各自被调一遍 —— 同一张照片在
 * 一次会话里的元信息不会变，四次网络往返换来的是同一份 JSON。这里钉的是**缓存语义**：
 * 同一个 id 只发一次请求，`forgetMedia` 之后必须真的失效（不然换视频之后旧元信息会
 * 一直粘着，播放的是换掉之前那一份地址）。
 *
 * 这个模块不引入任何 mock 框架 —— `mediaInfo` 接受一个可选 `fetcher` 参数，
 * 默认才是 `api.mediaOfPhoto`，测试直接传一个计数用的假函数进去。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { forgetMedia, mediaInfo } from '../public/mediaload.js'

describe('mediaInfo：会话内缓存', () => {
  test('同一个 photoId 连调两次，只发一次请求', async () => {
    let calls = 0
    const fetcher = async (id) => { calls++; return { url: `/v1/asset/${id}/stream` } }
    const a = await mediaInfo('p1', fetcher)
    const b = await mediaInfo('p1', fetcher)
    assert.equal(calls, 1)
    assert.equal(a, b) // 同一个对象，不是同值的两份
  })

  test('forgetMedia(id) 之后再调，发第二次请求', async () => {
    let calls = 0
    const fetcher = async (id) => { calls++; return { url: `/v1/asset/${id}/stream/${calls}` } }
    await mediaInfo('p2', fetcher)
    forgetMedia('p2')
    const info = await mediaInfo('p2', fetcher)
    assert.equal(calls, 2)
    assert.equal(info.url, '/v1/asset/p2/stream/2')
  })

  test('不同 photoId 各自缓存，互不影响', async () => {
    let calls = 0
    const fetcher = async (id) => { calls++; return { id } }
    await mediaInfo('a', fetcher)
    await mediaInfo('b', fetcher)
    await mediaInfo('a', fetcher)
    assert.equal(calls, 2)
  })

  test('forgetMedia() 不带参数清空全部', async () => {
    let calls = 0
    const fetcher = async (id) => { calls++; return { id } }
    await mediaInfo('x', fetcher)
    await mediaInfo('y', fetcher)
    forgetMedia()
    await mediaInfo('x', fetcher)
    await mediaInfo('y', fetcher)
    assert.equal(calls, 4)
  })
})

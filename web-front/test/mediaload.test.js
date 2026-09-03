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
import { Stage, forgetMedia, loadPhotoVideo, mediaInfo } from '../public/mediaload.js'

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

describe('loadPhotoVideo：真正走链路，不只是单测 mediaInfo', () => {
  /**
   * 回归：`loadPhotoVideo` 函数体内原来有个同名局部变量 `let mediaInfo = null`
   * （挂媒体元信息用的），Task 6 把 `api.mediaOfPhoto(photoId)` 改成
   * `mediaInfo(photoId)` 时，这一句解析到的是**那个局部变量**而不是模块级导出的
   * `mediaInfo` 函数 —— 局部变量此刻还是 `null`，于是每次都抛
   * `TypeError: mediaInfo is not a function`，被 try/catch 吞成 `Stage.ERROR`
   * 「取视频信息失败」，三个页面（扫描/试播/宾客）的视频全加不出来。
   *
   * 只单测 `mediaInfo` 这个函数本身测不出这个问题——那个函数是好的，坏的是
   * `loadPhotoVideo` 内部对它的引用被局部变量遮蔽。所以这里必须真的调
   * `loadPhotoVideo`。用最小的假 `video`（只要有 `add/removeEventListener`）
   * 和一个返回 `{missing: true}` 的假 `fetcher`：这条分支在 `cachedStream`
   * 之前就 return，不会碰到 Node 里不存在的 Cache API。
   */
  test('info.missing 时报 UNAVAILABLE 而不是 ERROR', async () => {
    const fakeVideo = { addEventListener() {}, removeEventListener() {} }
    const stages = []
    const fetcher = async () => ({ missing: true })

    // ⚠️ `stop` 不能在 `onStage` 回调里同步调用：`Stage.INFO`（以及这里测的
    // `Stage.ERROR`/`UNAVAILABLE`）在 `loadPhotoVideo` **返回之前**就可能同步触发
    // （没有真正的网络延迟时，`await` 之前的同步部分与 `catch` 分支都在同一个
    // 调用栈里跑完）。那时 `const stop = loadPhotoVideo(...)` 这一行还没执行完，
    // 在回调里调用它会撞 TDZ（`ReferenceError: Cannot access 'stop' before
    // initialization`），这个错误又被外层 `.catch` 接住变成**第二条** `Stage.ERROR`
    // ——踩过一次，与这里要测的那个 bug 无关，纯粹是测试自己的时序问题。
    // 所以清理挪到 `await` 之后再做。
    let stop
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('5 秒内没有等到终局阶段')), 5000)
      stop = loadPhotoVideo(fakeVideo, 'video-info-test-missing', {
        fetcher,
        onStage: (s) => {
          stages.push(s.stage)
          if (s.stage === Stage.UNAVAILABLE || s.stage === Stage.ERROR) {
            clearTimeout(timer)
            resolve()
          }
        },
      })
    })
    stop?.()

    assert.deepEqual(stages, [Stage.INFO, Stage.UNAVAILABLE],
      `期望 INFO → UNAVAILABLE，实际收到 ${stages.join(' → ')}` +
      '（收到 ERROR 就是那个变量遮蔽的 bug 复现了）')
  })
})

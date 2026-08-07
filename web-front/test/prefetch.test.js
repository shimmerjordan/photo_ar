/**
 * 登录后台预取（prefetch.js）。
 *
 * 这里钉的是**策略与缓存语义**，不是网络：
 * - 谁取多少（宾客全部 / 管理员最新 N）—— 这是模块唯一的策略点，错了要么把
 *   管理员的手机塞满，要么宾客到现场发现自己的视频没预取；
 * - 过期判定（staleKeys）—— 删错方向的话，要么缓存无限膨胀，要么把刚取的删了；
 * - 环境不支持时的退化 —— 必须返回 null 让调用方走原路，而不是抛出去把播放搞死。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { ADMIN_LIMIT, cachedStream, pickPlan, staleKeys } from '../public/prefetch.js'

describe('pickPlan：谁取多少', () => {
  const photo = (id, createdAt, hasVideo = true) => ({ photoId: id, createdAt, hasVideo })

  test('宾客取全部有视频的 —— 他的授权集就是他会扫到的那几张', () => {
    const photos = [photo('a', 1), photo('b', 2), photo('c', 3, false), photo('d', 4)]
    const plan = pickPlan(photos, false)
    assert.deepEqual(plan.map((p) => p.photoId), ['a', 'b', 'd'])
  })

  test('没配视频的不取 —— 预取的就是视频，没视频没什么可取', () => {
    assert.deepEqual(pickPlan([photo('x', 1, false)], false), [])
  })

  test('管理员只取最新 N 张（他看得到全库，全取装不下也没意义）', () => {
    const photos = Array.from({ length: 20 }, (_, i) => photo(`p${i}`, i))
    const plan = pickPlan(photos, true)
    assert.equal(plan.length, ADMIN_LIMIT)
    // 最新的在前：createdAt 最大的那几张
    assert.equal(plan[0].photoId, 'p19')
    assert.equal(plan.at(-1).photoId, `p${20 - ADMIN_LIMIT}`)
  })

  test('createdAt 缺失时按 0 处理，不抛 —— 老服务端的行可能没有这个字段', () => {
    const plan = pickPlan([photo('a', undefined), photo('b', 5)], true)
    assert.equal(plan[0].photoId, 'b')
  })

  test('photos 为空/未定义时给空计划，不抛', () => {
    assert.deepEqual(pickPlan([], false), [])
    assert.deepEqual(pickPlan(undefined, true), [])
  })
})

describe('staleKeys：该删哪些', () => {
  test('不在计划里的旧条目要删（撤了授权 / 换了视频 / 滚出最新 N）', () => {
    const existing = ['/v1/asset/a/stream', '/v1/asset/b/stream', '/v1/asset/c/stream']
    const wanted = ['/v1/asset/b/stream']
    assert.deepEqual(staleKeys(existing, wanted), ['/v1/asset/a/stream', '/v1/asset/c/stream'])
  })

  test('全在计划里 = 一个都不删', () => {
    const keys = ['/v1/asset/a/stream']
    assert.deepEqual(staleKeys(keys, keys), [])
  })
})

describe('cachedStream：环境退化', () => {
  test('没有 Cache Storage（http 非 localhost 就是这样）→ null，走原路', async () => {
    // node 里本来就没有 globalThis.caches —— 正好就是要测的环境。
    assert.equal(globalThis.caches, undefined)
    assert.equal(await cachedStream('/v1/asset/x/stream'), null)
  })

  test('caches.open 抛也返回 null —— 预取的任何失败都不该打断播放', async () => {
    globalThis.caches = { open: async () => { throw new Error('配额炸了') } }
    try {
      assert.equal(await cachedStream('/v1/asset/x/stream'), null)
    } finally {
      delete globalThis.caches
    }
  })

  test('命中时返回 match 的结果', async () => {
    const fake = new Response('bytes')
    globalThis.caches = { open: async () => ({ match: async () => fake }) }
    try {
      assert.equal(await cachedStream('/v1/asset/x/stream'), fake)
    } finally {
      delete globalThis.caches
    }
  })
})

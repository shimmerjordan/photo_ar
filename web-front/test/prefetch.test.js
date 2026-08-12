/**
 * 登录后台预取（prefetch.js）。
 *
 * 这里钉的是**策略与缓存语义**，不是网络：
 * - 顺序（pickPlan 倒序）—— 「最新入库的替换最老的」这条策略的唯一实现处，
 *   反了的话新入库的那几段永远排在最后，也就永远进不了缓存；
 * - 预算（budget 的钳位）—— 高了撑爆配额（而 `cache.put` 失败是**无声**的），
 *   低了等于没预取；
 * - 按预算切（planWithinBudget）—— 尤其是"一段超大的不该把后面全挡住"；
 * - 过期判定（staleKeys）—— 删错方向的话，要么缓存无限膨胀，要么把刚取的删了；
 * - 环境不支持时的退化 —— 必须返回 null 让调用方走原路，而不是抛出去把播放搞死。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import {
  BUDGET_FALLBACK, BUDGET_MAX, BUDGET_MIN, budget, cachedStream,
  pickPlan, planWithinBudget, staleKeys,
} from '../public/prefetch.js'

const MB = 1024 * 1024

/**
 * 换掉全局 `navigator`，用完还原。
 *
 * **必须走 `defineProperty`**：Node 22 起 `globalThis.navigator` 是一个只有 getter 的
 * 内置属性，直接赋值抛 `Cannot set property navigator`。
 */
async function withNavigator(fake, fn) {
  const prev = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  Object.defineProperty(globalThis, 'navigator', { value: fake, configurable: true, writable: true })
  try {
    return await fn()
  } finally {
    if (prev) Object.defineProperty(globalThis, 'navigator', prev)
    else delete globalThis.navigator
  }
}

const withQuota = (quota, fn) =>
  withNavigator({ storage: { estimate: async () => ({ quota }) } }, fn)

describe('pickPlan：候选顺序', () => {
  const photo = (id, createdAt, hasVideo = true) => ({ photoId: id, createdAt, hasVideo })

  test('按入库时间从新到旧 —— 这是「最新替换最老」的全部实现', () => {
    const photos = [photo('old', 1), photo('new', 9), photo('mid', 5)]
    assert.deepEqual(pickPlan(photos).map((p) => p.photoId), ['new', 'mid', 'old'])
  })

  test('没配视频的不进候选 —— 预取的就是视频', () => {
    assert.deepEqual(pickPlan([photo('x', 1, false)]), [])
  })

  test('两种角色同一条路：pickPlan 不再看 isAdmin', () => {
    // 上一版签名是 pickPlan(photos, isAdmin, limit)，管理员被切成最新 8 张。
    // 现在切法由预算决定，这个函数只负责排序 —— 多传的参数必须被忽略而不是改变结果。
    const photos = Array.from({ length: 20 }, (_, i) => photo(`p${i}`, i))
    assert.equal(pickPlan(photos, true).length, 20)
    assert.equal(pickPlan(photos, false).length, 20)
  })

  test('createdAt 缺失时按 0 处理，不抛 —— 老服务端的行可能没有这个字段', () => {
    const plan = pickPlan([photo('a', undefined), photo('b', 5)])
    assert.equal(plan[0].photoId, 'b')
  })

  test('photos 为空/未定义时给空计划，不抛', () => {
    assert.deepEqual(pickPlan([]), [])
    assert.deepEqual(pickPlan(undefined), [])
  })
})

describe('budget：按配额自适应，两头钳住', () => {
  test('取配额的四分之一', async () => {
    await withQuota(800 * MB, async () => assert.equal(await budget(), 200 * MB))
  })

  test('小存储手机上不低于下限 —— 低到装不下一段视频等于没预取', async () => {
    await withQuota(40 * MB, async () => assert.equal(await budget(), BUDGET_MIN))
  })

  test('大存储手机上不超过上限 —— 我们没有理由占人家 30GB', async () => {
    await withQuota(500 * 1024 * MB, async () => assert.equal(await budget(), BUDGET_MAX))
  })

  test('问不到配额（老浏览器）用保守的固定值，不抛', async () => {
    await withNavigator({}, async () => assert.equal(await budget(), BUDGET_FALLBACK))
  })

  test('estimate 抛的时候也退回固定值（隐私模式下会抛）', async () => {
    const boom = { storage: { estimate: async () => { throw new Error('隐私模式') } } }
    await withNavigator(boom, async () => assert.equal(await budget(), BUDGET_FALLBACK))
  })

  test('配额报 0 / NaN 时也退回固定值，而不是算出一个 0 预算', async () => {
    // 0 预算的表现是"预取一段都不取"，而那与"预取坏了"分不开 —— 必须当成问不到。
    await withQuota(0, async () => assert.equal(await budget(), BUDGET_FALLBACK))
    await withQuota(Number.NaN, async () => assert.equal(await budget(), BUDGET_FALLBACK))
  })
})

describe('planWithinBudget：按空间切', () => {
  const photo = (id, createdAt) => ({ photoId: id, createdAt, hasVideo: true })
  /** 每段固定 10MB 的假 infoOf。 */
  const sized = (map) => async (p) => ({ url: `/v1/asset/${p.photoId}/stream`, bytes: map[p.photoId] })

  test('装满就停，且装的是最新的那几段', async () => {
    const photos = [photo('a', 1), photo('b', 2), photo('c', 3), photo('d', 4)]
    const sizes = { a: 30 * MB, b: 30 * MB, c: 30 * MB, d: 30 * MB }
    const { items, used } = await planWithinBudget(photos, 70 * MB, sized(sizes))
    // 倒序是 d, c, b, a；70MB 只装得下两段。
    assert.deepEqual(items.map((i) => i.photo.photoId), ['d', 'c'])
    assert.equal(used, 60 * MB)
  })

  test('一段超大的不该把后面全挡住 —— 跳过它继续试', async () => {
    // 这一条防的是 `break` 写法：那样 200MB 那段排在最前面，明明还剩 90MB 空着，
    // 后面三段小的却一段都进不来。
    const photos = [photo('huge', 9), photo('a', 3), photo('b', 2)]
    const sizes = { huge: 200 * MB, a: 20 * MB, b: 20 * MB }
    const { items, tooBig } = await planWithinBudget(photos, 100 * MB, sized(sizes))
    assert.deepEqual(items.map((i) => i.photo.photoId), ['a', 'b'])
    assert.equal(tooBig, 1)
  })

  test('剩余预算低于 1MB 就不再问下一段 —— 转码后的视频没有比这更小的', async () => {
    const photos = Array.from({ length: 50 }, (_, i) => photo(`p${i}`, i))
    let asked = 0
    const infoOf = async (p) => {
      asked++
      return { url: `/v1/asset/${p.photoId}/stream`, bytes: 4 * MB }
    }
    await planWithinBudget(photos, 8 * MB + 512 * 1024, infoOf)
    // 装下两段之后只剩 512KB，低于止损线 —— 不该把剩下 48 张全问一遍。
    assert.equal(asked, 2, `问了 ${asked} 次，应当在装满后立刻停`)
  })

  test('infoOf 返回 null（没视频 / 网盘直链 / 文件不在了）就跳过，不计入预算', async () => {
    const photos = [photo('gone', 3), photo('ok', 2)]
    const infoOf = async (p) => (p.photoId === 'gone' ? null : { url: '/v1/asset/ok/stream', bytes: 5 * MB })
    const { items, used } = await planWithinBudget(photos, 100 * MB, infoOf)
    assert.deepEqual(items.map((i) => i.photo.photoId), ['ok'])
    assert.equal(used, 5 * MB)
  })

  test('infoOf 抛也只是跳过那一张 —— 预取的任何失败都不该打断整轮', async () => {
    const photos = [photo('bad', 3), photo('ok', 2)]
    const infoOf = async (p) => {
      if (p.photoId === 'bad') throw new Error('502')
      return { url: '/v1/asset/ok/stream', bytes: 5 * MB }
    }
    const { items } = await planWithinBudget(photos, 100 * MB, infoOf)
    assert.deepEqual(items.map((i) => i.photo.photoId), ['ok'])
  })

  test('bytes 缺失或为 0 的跳过 —— 记不了账的东西不能进预算', async () => {
    const photos = [photo('nobytes', 3)]
    const { items } = await planWithinBudget(photos, 100 * MB, async () => ({ url: '/x', bytes: 0 }))
    assert.deepEqual(items, [])
  })
})

describe('staleKeys：该删哪些', () => {
  test('不在计划里的旧条目要删（撤了授权 / 换了视频 / 被更新的挤出去）', () => {
    const existing = ['/v1/asset/a/stream', '/v1/asset/b/stream', '/v1/asset/c/stream']
    const wanted = ['/v1/asset/b/stream']
    assert.deepEqual(staleKeys(existing, wanted), ['/v1/asset/a/stream', '/v1/asset/c/stream'])
  })

  test('全在计划里 = 一个都不删', () => {
    const keys = ['/v1/asset/a/stream']
    assert.deepEqual(staleKeys(keys, keys), [])
  })

  test('「最新替换最老」是这两个函数合起来的结果，不需要单独的淘汰器', async () => {
    // 这一条把两半接起来验一遍：老的进不了计划（预算切掉），于是它落在 wanted 之外，
    // 于是 staleKeys 把它列出来删。这正是策略的全部实现。
    const photos = [
      { photoId: 'old', createdAt: 1, hasVideo: true },
      { photoId: 'new', createdAt: 9, hasVideo: true },
    ]
    const infoOf = async (p) => ({ url: `/v1/asset/${p.photoId}/stream`, bytes: 40 * MB })
    const { items } = await planWithinBudget(photos, 50 * MB, infoOf)
    assert.deepEqual(items.map((i) => i.photo.photoId), ['new'])
    const existing = ['/v1/asset/old/stream']   // 上一轮缓存的
    assert.deepEqual(staleKeys(existing, items.map((i) => i.url)), ['/v1/asset/old/stream'])
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

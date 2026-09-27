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
import { keepSet, _runForTest } from '../public/prefetch.js'
import { refKey, pin, unpin, setUser, VIDEO_CACHE } from '../public/mediastore.js'

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

describe('keepSet：清理口径（固定 / 计划 / 正在下）', () => {
  test('清理口径：固定的、计划里的、正在下的都留；换了人之后别人的全删', () => {
    const photos = [{ photoId: 'a', videoAssetId: 'va' }, { photoId: 'b', videoAssetId: 'vb' }]
    const keep = keepSet({ photos, pinned: new Set(['a', 'zz']), planKeys: ['/v1/asset/vb/stream'], running: ['/v1/asset/vx/stream'] })
    assert.deepEqual([...keep].sort(), ['/v1/asset/va/stream', '/v1/asset/vb/stream', '/v1/asset/vx/stream'])
  })

  test('原图缓存同一个口径：只留当前用户授权清单里、且被固定的那些', () => {
    // `zz` 是固定过、但这个人已经看不到的照片（撤了授权，或者是上一个人固定的）——
    // 它不在 photos 里，就一定不在留下的集合里：缓存里不该留别人才有权限的东西。
    const photos = [{ photoId: 'a' }, { photoId: 'b' }]
    const keep = keepSet({ photos, pinned: new Set(['a', 'zz']), running: [], keyOf: (p) => refKey(p.photoId) })
    assert.deepEqual([...keep], ['/v1/photo/a/ref'])
  })

  test('固定了但没视频的照片不产生视频键', () => {
    const keep = keepSet({ photos: [{ photoId: 'a', videoAssetId: null }], pinned: new Set(['a']) })
    assert.deepEqual([...keep], [])
  })
})

describe('run：每段下载前现查「已在本机 / 还固定着」（fix round 1 #1）', () => {
  // 循环要跑好几分钟，期间播放 / 手动缓存 / 从本机移除都可能改掉「这一段该不该下」。
  // 上一版在进循环前拍一次快照，于是：播放刚下完并缓存的那段被预取再下一遍；运行中被移除的
  // 固定组又被下回缓存。这两条各钉一次。

  /** 假 Cache Storage（Map 实现），与 mediastore.test.js 的同形。 */
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

  /**
   * 装好 run 需要的全局（fetch / caches），跑一轮，还原。`asked` 只记视频与原图的请求 ——
   * 照片列表与缩略图不算（缩略图一律回 404，跟这里要钉的无关）。
   * `onFetch(url, caches)` 在对应请求发出时被调，用来模拟「这一刻用户 / 播放做了什么」。
   */
  async function runWith(photos, onFetch) {
    const cs = fakeCaches()
    const asked = []
    const prevFetch = globalThis.fetch
    globalThis.caches = cs
    globalThis.fetch = async (url) => {
      const u = String(url)
      if (u === '/v1/photos') return new Response(JSON.stringify({ photos }), { status: 200 })
      if (u.endsWith('/thumb')) return new Response('', { status: 404 })
      asked.push(u)
      await onFetch?.(u, cs)
      return new Response(new Uint8Array([1, 2]), { status: 200 })
    }
    try {
      await _runForTest()
    } finally {
      globalThis.fetch = prevFetch
      delete globalThis.caches
    }
    return asked
  }

  test('轮到某段之前它已经进了本机缓存（播放刚下完 / 手动缓存）：不再下第二遍', async () => {
    setUser('run-cached')
    const photos = [
      { photoId: 'a', createdAt: 2, hasVideo: true, videoAssetId: 'va', videoBytes: 2 * MB },
      { photoId: 'b', createdAt: 1, hasVideo: true, videoAssetId: 'vb', videoBytes: 2 * MB },
    ]
    // 预取在下 a 的时候，播放把 b 下完并写进了缓存（任务随即从任务表删掉）。
    const asked = await runWith(photos, async (u, cs) => {
      if (u === '/v1/asset/va/stream') await (await cs.open(VIDEO_CACHE)).put('/v1/asset/vb/stream', new Response(new Uint8Array([9])))
    })
    assert.deepEqual(asked, ['/v1/asset/va/stream'])
  })

  test('运行中被「从本机移除」（取消固定）的组：还没轮到的那几段不再下回来', async () => {
    setUser('run-unpin')
    pin('p1'); pin('p2')
    const photos = [
      { photoId: 'p1', createdAt: 2, hasVideo: true, videoAssetId: 'v1', videoBytes: 2 * MB },
      { photoId: 'p2', createdAt: 1, hasVideo: true, videoAssetId: 'v2', videoBytes: 2 * MB },
    ]
    const asked = await runWith(photos, async (u) => {
      if (u === '/v1/photo/p1/ref') unpin('p2')
    })
    assert.deepEqual(asked, ['/v1/photo/p1/ref', '/v1/asset/v1/stream'])
  })
})

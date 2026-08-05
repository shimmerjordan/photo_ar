/**
 * 跨帧证据累积。**每一条测试都对应一个具体的失败方式，包括那个已知代价。**
 *
 * 规则与服务端 `photoar.streak` 是同一套（软门槛 30 / 要 3 帧 / 每帧比值 ≥2.0 /
 * 间隔窗口 2s），所以这里也顺带钉住"两边的数字没分叉"。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { STREAK_DEFAULTS, Streak } from '../public/recognize/streak.js'

const DET = [0.05, 20.0]
/** 一个候选。`h` 必须有 —— 没有单应矩阵的候选不能当证据。 */
const cand = (photoId, inliers, det = 1.0) => ({ photoId, inliers, det, h: new Float32Array(9) })

/** 连续喂 n 帧同一张，返回最后一次的结果。 */
function feed(s, n, { photoId = 'A', inliers = 33, gap = 600, t0 = 1000, runnerUp = null } = {}) {
  let got = null
  for (let i = 0; i < n; i++) {
    const results = [cand(photoId, inliers)]
    if (runnerUp !== null) results.push(cand('B', runnerUp))
    got = s.offer(results, t0 + i * gap, DET)
  }
  return got
}

describe('攒够就命中', () => {
  test('连续 3 帧、内点 33、只有一个候选 → 命中', () => {
    // 这就是真机上那个场景：内点 30~38、runner-up 个位数、门槛 40 永远过不了。
    const s = new Streak()
    assert.equal(feed(s, 2), null, '两帧还不够')
    const got = feed(new Streak(), 3)
    assert.ok(got)
    assert.equal(got.photoId, 'A')
    assert.equal(got.inliers, 33)
  })

  test('命中之后链清空 —— 一次累积不能让后面每一帧都命中', () => {
    const s = new Streak()
    assert.ok(feed(s, 3))
    assert.equal(s.progress.n, 0)
    // 紧接着再来一帧不该又命中
    assert.equal(s.offer([cand('A', 33)], 5000, DET), null)
  })

  test('progress 报得出攒到第几帧（界面靠它说「再举稳一会儿」）', () => {
    const s = new Streak()
    feed(s, 2)
    assert.deepEqual(s.progress, { n: 2, need: 3, photoId: 'A' })
  })
})

describe('不算证据的都要**断链**，不是忽略', () => {
  // 这一组是整条规则的安全边界。忽略而不是断链的话，「举着手机晃过去偶尔扫到」
  // 也会被攒成命中 —— 而那不是「用户在看这张照片」。
  test('内点低于软门槛 → 断链', () => {
    const s = new Streak()
    feed(s, 2)
    s.offer([cand('A', 29)], 3000, DET)   // 29 < softMin 30
    assert.equal(s.progress.n, 0)
  })

  test('中间夹一帧什么都没看到（空候选）→ 断链', () => {
    const s = new Streak()
    feed(s, 2)
    s.offer([], 3000, DET)
    assert.equal(s.progress.n, 0)
  })

  test('比值不够（有个势均力敌的对手）→ 断链', () => {
    const s = new Streak()
    // 33 / 20 = 1.65 < ratio 2.0
    assert.equal(feed(s, 3, { runnerUp: 20 }), null)
    assert.equal(s.progress.n, 0)
  })

  test('行列式越界 → 断链（那是矩阵退化，不是分数不够）', () => {
    const s = new Streak()
    feed(s, 2)
    s.offer([cand('A', 35, 1e6)], 3000, DET)
    assert.equal(s.progress.n, 0)
  })

  test('候选没有单应矩阵 → 不算证据', () => {
    const s = new Streak()
    const bad = { photoId: 'A', inliers: 35, det: 1, h: null }
    assert.equal(s.offer([bad], 1000, DET), null)
    assert.equal(s.progress.n, 0)
  })
})

describe('换目标与超时', () => {
  test('中途换成另一张 → 从头开始攒那一张', () => {
    const s = new Streak()
    feed(s, 2, { photoId: 'A' })
    const got = s.offer([cand('B', 33)], 3000, DET)
    assert.equal(got, null)
    assert.deepEqual(s.progress, { n: 1, need: 3, photoId: 'B' })
  })

  test('间隔超过窗口 → 断链（这是"偶尔扫到"与"一直在看"的分界）', () => {
    const s = new Streak()
    feed(s, 2, { gap: 600 })
    const got = s.offer([cand('A', 33)], 1000 + 600 + STREAK_DEFAULTS.windowMs + 1, DET)
    assert.equal(got, null)
    assert.equal(s.progress.n, 1, '那一帧本身仍然是一份新证据')
  })

  test('时间倒流 → 断链，而不是算出一个负的间隔', () => {
    // `performance.now()` 在某些机型上跨挂起会回跳。
    const s = new Streak()
    feed(s, 2, { t0: 5000 })
    s.offer([cand('A', 33)], 100, DET)
    assert.equal(s.progress.n, 1)
  })
})

describe('已知代价', () => {
  test('**能稳定误配的那一类挡不住** —— 这条测试把代价钉在这里', () => {
    // 单帧门槛 40 原本挡住了真实误识别（服务端记录的 34 条真实误识别最大 39）。
    // 累积把 30~39 放进来了，挡它的变成"连续 3 帧 + 每帧比值 ≥2"。
    //
    // 所以一张库外照片与库内某张几何上真的相似时（每帧都真的很像、比值也高），
    // 累积会把它认成命中。这**不是 bug，是明知的取舍**：漏检每次扫描都在发生，
    // 而稳定误配要求库里恰好有一张高度相似的照片，而入库路径上的去重闸门在挡这一类。
    //
    // 这条测试存在的意义是：哪天有人想"顺手把累积调松一点"，先看见这个代价。
    const s = new Streak()
    const got = feed(s, 3, { inliers: 38, runnerUp: 5 })
    assert.ok(got, '几何上稳定相似的库外照片会被累积认成命中')
    assert.equal(got.inliers, 38, '而它的内点数低于单帧门槛 40')
  })
})

describe('参数', () => {
  test('默认值与服务端 streak.py 的常量一致', async () => {
    // 两边分叉的表现是"服务端调了阈值、网页版行为不变"，而那没有任何症状。
    const src = await (await import('node:fs/promises')).readFile(
      new URL('../../src/photoar/streak.py', import.meta.url), 'utf8').catch(() => null)
    if (!src) return // 只 checkout 了 web-front 时跳过
    // Python 允许数字里带下划线（`2_000`），正则要把它去掉再转 —— 上一版没去，
    // 于是 `2_000` 被 parse 成 2，测试报"2000 !== 2"，看起来像常量真的不一致。
    const num = (name) => Number(
      (new RegExp(`^${name}\\s*=\\s*([0-9_.]+)`, 'm').exec(src)?.[1] ?? '').replaceAll('_', ''))
    assert.equal(STREAK_DEFAULTS.softMin, num('STREAK_SOFT_MIN_INLIERS'))
    assert.equal(STREAK_DEFAULTS.need, num('STREAK_NEED'))
    assert.equal(STREAK_DEFAULTS.ratio, num('STREAK_RATIO'))
    assert.equal(STREAK_DEFAULTS.windowMs, num('STREAK_WINDOW_MS'))
  })

  test('configure 可以逐帧改（那两个是服务端热配置）', () => {
    const s = new Streak()
    s.configure({ need: 2 })
    assert.ok(feed(s, 2), 'need=2 时两帧就够')
    // need 有下界 2：设成 1 就等于取消累积，而那时该直接调低 minInliers。
    s.configure({ need: 1 })
    assert.equal(s.need, 2)
  })

  test('比值那一条比单帧判定更严', async () => {
    // 累积放宽了绝对分数，就得在相对分数上收紧，否则两条都松。
    const { thresholds } = await import('../public/recognize/consts.js')
    assert.ok(STREAK_DEFAULTS.ratio > thresholds.ratio,
      `累积比值 ${STREAK_DEFAULTS.ratio} 应当严于单帧 ${thresholds.ratio}`)
  })
})

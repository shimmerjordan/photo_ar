/**
 * 自适应预测滤波器。**这一份测试盯的是"改坏了会怎样"，而每一条都对应一个真实症状。**
 *
 * 参数是仿真定的（`test/sim/predict.mjs`，噪声与延迟用真机实测值），所以这里不重复
 * 扫参 —— 这里验的是性质：静止时不外推、运动时补得上、丢锁能干净重置。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import {
  LEAD_MAX_MS, LEAD_SCALE, QuadFilter, SPEED_REF, TAU_FAST_MS, TAU_SLOW_MS, VEL_TAU_MS,
} from '../public/render/quadfilter.js'

/** 一个四角：左上、右上、左下、右下，都平移 (dx,dy)。 */
const quadAt = (dx = 0, dy = 0) => Float32Array.from([
  0.3 + dx, 0.3 + dy, 0.7 + dx, 0.3 + dy, 0.3 + dx, 0.7 + dy, 0.7 + dx, 0.7 + dy,
])

const out = () => new Float32Array(8)

describe('参数本身', () => {
  test('静止时的时间常数必须明显大于运动时的', () => {
    // 这是整个设计的前提。写反了的表现是"静止时抖、运动时糊"，两头都差。
    assert.ok(TAU_SLOW_MS > TAU_FAST_MS * 3, `${TAU_SLOW_MS} vs ${TAU_FAST_MS}`)
  })

  test('速度分界要显著高于噪声造成的假速度', () => {
    // 真机实测：位置噪声 0.52‰，观测间隔 51ms → 假速度约 0.52e-3*√2/51 ≈ 0.0144e-3/ms。
    // 分界低于它的话，静止时的噪声会被当成运动，自适应就白做了。
    const fakeSpeed = (0.52e-3 * Math.SQRT2) / 51
    assert.ok(SPEED_REF > fakeSpeed * 2, `SPEED_REF=${SPEED_REF} 假速度=${fakeSpeed}`)
  })

  test('延迟补偿是**欠**补偿', () => {
    // 补满会因为速度估计自身的滞后而过冲，表现是果冻感。仿真里 0.7 优于 1.0。
    assert.ok(LEAD_SCALE > 0 && LEAD_SCALE < 1, String(LEAD_SCALE))
  })
})

describe('静止', () => {
  test('第一个观测直接落上去，不从别处飘过来', () => {
    const f = new QuadFilter()
    const q = quadAt()
    f.observe(q, 1000, 0)
    const got = f.at(1000, out())
    for (let i = 0; i < 8; i++) assert.ok(Math.abs(got[i] - q[i]) < 1e-6, `第 ${i} 个坐标`)
  })

  test('喂同一个四角很多次之后**完全不外推**', () => {
    const f = new QuadFilter()
    const q = quadAt()
    for (let t = 0; t < 3000; t += 50) f.observe(q, t, 88)
    const got = f.at(3000, out())
    // 速度是 0 ⇒ motion 是 0 ⇒ lead 乘 0。哪怕 obsAge 是 88ms，也不该有任何位移。
    for (let i = 0; i < 8; i++) assert.ok(Math.abs(got[i] - q[i]) < 1e-9, `第 ${i} 个坐标偏了 ${got[i] - q[i]}`)
    assert.equal(f.motion, 0)
  })

  test('静止时噪声被明显压掉（这是 tau_slow 的全部用途）', () => {
    // 真值恒定，观测带噪声。比较"直接用观测"与"滤波输出"的位置标准差。
    let seed = 7
    const rnd = () => ((seed = (seed * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff - 0.5) * 2
    const f = new QuadFilter()
    const noisy = []
    const filtered = []
    for (let t = 0; t < 6000; t += 51) {
      const n = quadAt(rnd() * 1e-3, rnd() * 1e-3)
      f.observe(n, t, 88)
      if (t < 1000) continue // 起步那一段在追赶，不算
      noisy.push(n[0])
      filtered.push(f.at(t, out())[0])
    }
    const sd = (a) => {
      const m = a.reduce((x, y) => x + y, 0) / a.length
      return Math.sqrt(a.reduce((x, y) => x + (y - m) ** 2, 0) / a.length)
    }
    const before = sd(noisy)
    const after = sd(filtered)
    assert.ok(after < before * 0.5, `压噪比 ${(after / before).toFixed(2)}（before=${before.toExponential(2)} after=${after.toExponential(2)}）`)
  })
})

describe('运动', () => {
  /** 匀速平移，每 dt 一个观测，观测测的是 age 之前的位置。 */
  function ramp({ vx = 0.2e-3, dt = 51, age = 88, seconds = 4 } = {}) {
    const f = new QuadFilter()
    let last = null
    for (let t = 0; t <= seconds * 1000; t += dt) {
      f.observe(quadAt(vx * (t - age)), t, age)
      last = t
    }
    return { f, t: last, truthDx: vx * last, vx }
  }

  test('匀速运动时把管线延迟补回来（这就是"跟得上"）', () => {
    const { f, t, truthDx } = ramp()
    const got = f.at(t, out())
    const naive = quadAt(0.2e-3 * (t - 88))   // 不补偿会停在这里
    const truth = quadAt(truthDx)
    const errFiltered = Math.abs(got[0] - truth[0])
    const errNaive = Math.abs(naive[0] - truth[0])
    // 至少把误差砍掉一半。补满 100% 是不可能的（刻意欠补偿 + 速度估计有滞后）。
    assert.ok(errFiltered < errNaive * 0.5,
      `补偿后误差 ${errFiltered.toExponential(2)} vs 不补偿 ${errNaive.toExponential(2)}`)
  })

  test('**不许过冲**：匀速时不能冲到真值前面去', () => {
    // 过冲的表现是果冻感 —— 视频冲到照片前面再被拉回来。比"跟不上"更难看，
    // 所以这一条是硬约束，而 LEAD_SCALE<1 就是为它留的余量。
    const { f, t, truthDx } = ramp()
    const got = f.at(t, out())
    assert.ok(got[0] <= quadAt(truthDx)[0] + 1e-6, `冲过了 ${(got[0] - quadAt(truthDx)[0]).toExponential(2)}`)
  })

  test('运动越快补得越多（分档是连续的，不是开关）', () => {
    const slow = ramp({ vx: 0.02e-3 })
    const fast = ramp({ vx: 0.4e-3 })
    assert.ok(fast.f.motion > slow.f.motion)
    // 慢速时几乎不外推，快速时接近全额
    assert.ok(slow.f.motion < 0.5, `慢速 motion=${slow.f.motion.toFixed(2)}`)
    assert.ok(fast.f.motion > 0.9, `快速 motion=${fast.f.motion.toFixed(2)}`)
  })

  test('观测间隔变了跟踪精度不该变（帧率无关）', () => {
    // ⚠️ 不能直接比两个采样率下的**位置** —— 它们的最后一个观测落在不同时刻
    // （dt=33 时是 3993ms，dt=66 时是 3960ms），真值本身就差了 0.2e-3×33 = 6.6‰。
    // 上一版就是这么比的，于是这条测试在测一件它自己造出来的差异。
    //
    // 帧率无关性要看的是**相对各自时刻真值的误差**：指数平滑按 dt 算 alpha，
    // 所以采样稀一点只该让噪声抑制变差，不该让跟踪偏一个系统性的量。
    const errAt = (dt) => {
      const r = ramp({ dt })
      const got = r.f.at(r.t, out())[0]
      return Math.abs(got - quadAt(r.truthDx)[0])
    }
    const e33 = errAt(33)
    const e66 = errAt(66)
    assert.ok(Math.abs(e33 - e66) < 3e-3,
      `33ms 误差 ${e33.toExponential(2)}，66ms 误差 ${e66.toExponential(2)}`)
    // 而且两者都该显著小于"完全不补偿"的误差（0.2e-3 × 88ms = 17.6‰）
    for (const [name, e] of [['33ms', e33], ['66ms', e66]]) {
      assert.ok(e < 0.2e-3 * 88 * 0.6, `${name} 的误差 ${e.toExponential(2)} 太大`)
    }
  })
})

describe('外推的上界', () => {
  // 这一组是真机上抓出来的缺陷，而仿真暴露不了它（那边观测间隔固定 51ms）。
  test('观测稀疏时外推量被夹住，不线性增长', () => {
    const f = new QuadFilter()
    // 先建立一个速度
    for (let t = 0; t < 2000; t += 51) f.observe(quadAt(0.3e-3 * t), t, 90)
    const at2000 = f.at(2000, out())[0]
    // 然后 500ms 不来新观测（真机上跟踪变慢、或主线程被卡住时就是这样）
    const at2500 = f.at(2500, out())[0]
    const drift = at2500 - at2000
    // 没有上界的话这 500ms 会被原样乘进外推量，位移 0.3e-3 × 500 × 0.7 = 105‰。
    // 有上界之后总外推量不超过 LEAD_MAX_MS，所以多出来的位移必须很小。
    assert.ok(drift < 0.3e-3 * LEAD_MAX_MS, `500ms 没有新观测，四角飘了 ${(drift * 1000).toFixed(1)}‰`)
  })

  test('上界要小于一个不健康的观测间隔（否则它形同不存在）', () => {
    // 真机健康状态下观测间隔 51~93ms；不健康时量到过 208ms。
    // 上界必须落在这两者之间，否则要么常态被夹（跟不上）、要么不健康时不生效。
    assert.ok(LEAD_MAX_MS >= 93 && LEAD_MAX_MS < 208, `LEAD_MAX_MS=${LEAD_MAX_MS}`)
  })

  test('健康状态下上界不该生效（不能顺手把正常情况也夹了）', () => {
    const f = new QuadFilter()
    for (let t = 0; t < 3000; t += 51) f.observe(quadAt(0.3e-3 * t), t, 90)
    // 观测刚到，lead ≈ (90 + 0 + 25) × 1 × 0.7 = 80.5ms < 120，不该被夹。
    const capped = new QuadFilter({ leadMax: 1e9 })
    for (let t = 0; t < 3000; t += 51) capped.observe(quadAt(0.3e-3 * t), t, 90)
    assert.ok(Math.abs(f.at(3000, out())[0] - capped.at(3000, out())[0]) < 1e-6,
      '健康状态下有没有上界应当没有区别')
  })
})

describe('边界与重置', () => {
  test('没有观测时 at() 返回 null，不返回一堆 0', () => {
    // 返回 0 的话四角会落在画面角上，然后视频在那儿闪一下 —— 而调用方分不出
    // "还没有数据"和"数据是 0"。
    assert.equal(new QuadFilter().at(0, out()), null)
  })

  test('reset 之后像新的一样（丢锁重扫不能带着旧速度）', () => {
    const f = new QuadFilter()
    for (let t = 0; t < 2000; t += 50) f.observe(quadAt(0.3e-3 * t), t, 88)
    assert.ok(f.motion > 0.5)
    f.reset()
    assert.equal(f.at(3000, out()), null)
    assert.equal(f.motion, 0)
    // 带着旧速度的表现是：重新锁上那一刻视频从一个错的位置飞过来。
    const q = quadAt(0.9)
    f.observe(q, 3000, 88)
    const got = f.at(3000, out())
    assert.ok(Math.abs(got[0] - q[0]) < 1e-6)
  })

  test('长度不对的输入被忽略，不写坏内部状态', () => {
    const f = new QuadFilter()
    f.observe(quadAt(), 0, 0)
    const before = Array.from(f.pos)
    f.observe(new Float32Array([1, 2, 3]), 100, 0)
    f.observe(null, 200, 0)
    assert.deepEqual(Array.from(f.pos), before)
  })

  test('out 缓冲长度不对时返回 null，而不是写越界', () => {
    const f = new QuadFilter()
    f.observe(quadAt(), 0, 0)
    assert.equal(f.at(0, new Float32Array(4)), null)
    assert.equal(f.at(0, null), null)
  })

  test('时钟回跳不该把四角甩出去', () => {
    // `performance.now()` 在某些机型上跨挂起会回跳。`Math.max(0, …)` 兜着。
    const f = new QuadFilter()
    for (let t = 0; t < 1000; t += 50) f.observe(quadAt(0.2e-3 * t), t, 88)
    const got = f.at(500, out())   // 比 lastObsAt 还早
    for (const v of got) assert.ok(Number.isFinite(v))
  })
})

describe('与仿真的一致性', () => {
  test('仿真里定的那组参数就是代码里用的那组', async () => {
    // 参数是仿真扫出来的。两边分叉的表现是"仿真说好了，真机没变化"——
    // 而那会让人去怀疑真机测量，而不是怀疑参数没同步。
    const src = await (await import('node:fs/promises')).readFile(
      new URL('./sim/predict.mjs', import.meta.url), 'utf8')
    assert.match(src, /posTauSlow/, '仿真里应当有 posTauSlow 这一族参数')
    // 代码里的这四个数必须出现在仿真的网格里（否则仿真没验过它们）
    for (const [name, v] of [['TAU_SLOW_MS', TAU_SLOW_MS], ['TAU_FAST_MS', TAU_FAST_MS],
                             ['VEL_TAU_MS', VEL_TAU_MS]]) {
      assert.match(src, new RegExp(`\\b${v}\\b`), `${name}=${v} 不在仿真的网格里`)
    }
  })
})

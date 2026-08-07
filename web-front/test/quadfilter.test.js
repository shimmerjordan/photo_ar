/**
 * 自适应预测滤波器。**这一份测试盯的是"改坏了会怎样"，而每一条都对应一个真实症状。**
 *
 * 参数是仿真定的（`test/sim/predict.mjs`，噪声与延迟用真机实测值），所以这里不重复
 * 扫参 —— 这里验的是性质：静止时不外推、运动时补得上、丢锁能干净重置。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import {
  CORRECTION_DONE, CORRECTION_MAX_MS, CORRECTION_TAU_MS, JUMP_CONFIRM, JUMP_GATE,
  LEAD_MAX_MS, LEAD_SCALE, QuadFilter, SPEED_REF, TAU_FAST_MS, TAU_SLOW_MS, TELEPORT,
  VEL_TAU_MS,
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

describe('跳变门（重锚纠正与 RANSAC 毛刺）', () => {
  // 场景常量：51ms 观测间隔（真机健康值）。
  const DT = 51
  const settle = (f, q, t0, n = 30) => {
    for (let i = 0; i < n; i++) f.observe(q, t0 + i * DT)
    return t0 + n * DT
  }

  test('单帧毛刺被整个吞掉 —— 输出纹丝不动', () => {
    // 大角度下 RANSAC 偶尔给一帧明显错的四角。没有门的话它按 tauFast 直接穿过，
    // 表现是视频"啪"地闪到别处又闪回来。
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    const before = Float32Array.from(f.at(t, out()))
    f.observe(quadAt(0.08, 0.08), t += DT)          // 毛刺：8% 画幅
    f.observe(quadAt(0, 0), t += DT)                 // 下一帧回到原位
    const after = f.at(t, out())
    for (let i = 0; i < 8; i++) {
      assert.ok(Math.abs(after[i] - before[i]) < 0.005,
        `毛刺漏过去了：坐标 ${i} 动了 ${Math.abs(after[i] - before[i])}`)
    }
    assert.equal(f.rejected, 1)
  })

  test('持续的新位置（重锚纠正）会被认下，但是**滑**过去不是跳过去', () => {
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    const target = quadAt(0.06, 0)                   // 6% 画幅的纠正，真机重锚的典型量
    const steps = []
    for (let i = 0; i < 12; i++) {
      f.observe(target, t += DT)
      steps.push(f.at(t, out())[0])
    }
    // 确认期（JUMP_CONFIRM 帧）内不动
    assert.ok(Math.abs(steps[0] - 0.3) < 0.005, `确认期就动了：${steps[0]}`)
    // 认下之后单步位移有界：一步跨完 6% 就是"跳"。CORRECTION_TAU=120ms、DT=51ms
    // → 单步最多走 1-exp(-51/120) ≈ 35% 的剩余距离。
    for (let i = 1; i < steps.length; i++) {
      assert.ok(Math.abs(steps[i] - steps[i - 1]) < 0.06 * 0.45,
        `第 ${i} 步跳了 ${Math.abs(steps[i] - steps[i - 1])}`)
    }
    // 但最终要**到**：600ms 内收敛到目标附近（纠正不能变成永远追不上）。
    assert.ok(Math.abs(steps.at(-1) - 0.36) < 0.01, `没滑到位：${steps.at(-1)}`)
    assert.equal(f.correcting, false, '收敛后要回到正常模式')
  })

  test('滑行期间外推关闭 —— 纠正的斜率不能被当成运动甩出去', () => {
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    const target = quadAt(0.06, 0)
    // 第 1 帧进候选、第 2 帧确认并走第一步滑行 —— 此刻还没收敛，正在滑。
    for (let i = 0; i < 2; i++) f.observe(target, t += DT)
    assert.ok(f.correcting, '此刻应当在滑行')
    // at() 在滑行中输出的就是 pos 本身（vel=0 → lead=0），绝不越过目标。
    let p = f.at(t + 100, out())
    assert.ok(p[0] <= 0.36 + 1e-6, `外推把纠正甩过头了：${p[0]}`)
    // 收敛之后（回到正常模式）外推重新热身，同样不该越过一个静止的目标。
    for (let i = 0; i < 10; i++) f.observe(target, t += DT)
    p = f.at(t + 100, out())
    assert.ok(p[0] <= 0.36 + 0.005, `收敛后甩过头：${p[0]}`)
  })

  test('特别大的位移（换了地方）直接切换，不花半秒爬过去', () => {
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    const far = quadAt(0.3, 0.3)                     // 30% 画幅 >> TELEPORT
    for (let i = 0; i < JUMP_CONFIRM; i++) f.observe(far, t += DT)
    const p = f.at(t, out())
    assert.ok(Math.abs(p[0] - 0.6) < 1e-6, `该瞬移没瞬移：${p[0]}`)
  })

  test('正常运动不受门影响 —— 连贯的快速移动照常跟', () => {
    // 门比较的是观测与**预测**的偏差。匀速运动下预测跟得住，永远不该触发。
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    const SPEED = 0.2e-3                             // 20%/s，4 倍于 SPEED_REF 的全速
    let x = 0
    for (let i = 0; i < 40; i++) {
      x += SPEED * DT
      f.observe(quadAt(x, 0), t += DT)
    }
    assert.equal(f.rejected, 0, '连贯运动被误判成毛刺')
    const p = f.at(t, out())
    // 跟得上：滞后小于 2.5% 画幅（无门时的既有水平）
    assert.ok(Math.abs(p[0] - (0.3 + x)) < 0.025, `跟丢了：${p[0]} vs ${0.3 + x}`)
  })

  test('门的参数彼此自洽', () => {
    assert.ok(JUMP_GATE < TELEPORT, '瞬移门必须高于跳变门')
    assert.ok(JUMP_CONFIRM >= 2, '至少两帧确认，否则单帧毛刺直接穿门')
    assert.ok(CORRECTION_TAU_MS > 0 && CORRECTION_TAU_MS <= 300,
      '滑行要快到不觉得追不上（≤300ms 量级）')
    assert.ok(CORRECTION_DONE < JUMP_GATE, '收敛线必须低于门线，否则滑行一步就退出')
    assert.ok(CORRECTION_MAX_MS >= CORRECTION_TAU_MS * 4,
      '硬时限至少给纯指数逼近 4 个 tau（余量 <2%），否则正常纠正会被中途打断')
  })
})

describe('correction 标（来源已知的重锚纠正 —— 真机抓出来的门下漏洞）', () => {
  // 真机实测（2026-08-07）：大角度场景的重锚纠正是 2%~2.5% 画幅，在 3.5% 的门
  // **下面** —— 从正常路径穿过后被"快跟 + 外推"一两帧整步应用，录屏里每 2 秒
  // "啪"一次。修法是 pipeline 给重锚落地帧打标，滤波器不猜幅度直接滑。
  const DT = 51
  const settle = (f, q, t0, n = 30) => {
    for (let i = 0; i < n; i++) f.observe(q, t0 + i * DT)
    return t0 + n * DT
  }

  test('门下的纠正（2.5%）带标就滑，不再一两帧穿过', () => {
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    const target = quadAt(0.025, 0)                  // 真机量到的典型重锚纠正
    const steps = []
    f.observe(target, t += DT, 0, { correction: true })
    steps.push(f.at(t, out())[0])
    for (let i = 0; i < 11; i++) {                   // 后续是普通跟踪帧（不带标）
      f.observe(target, t += DT)
      steps.push(f.at(t, out())[0])
    }
    // 没有确认期（来源已知），第一步就开始动，但每一步只走剩余距离的一部分 ——
    // 这就是"滑"：tau=120ms 下，观测间隔 ≤120ms 时单步 ≤ 1-exp(-1) ≈ 63% 的剩余。
    // （对照修前行为：快跟 + 外推在一两帧内把剩余走完 = 100%。）
    let prev = 0.3
    let framesToArrive = 0
    for (const [i, s] of steps.entries()) {
      const remaining = 0.325 - prev
      assert.ok(s - prev <= remaining * 0.65 + 1e-9,
        `第 ${i} 步走了剩余的 ${((s - prev) / remaining * 100).toFixed(0)}%（跳）`)
      prev = s
      if (framesToArrive === 0 && 0.325 - s <= CORRECTION_DONE) framesToArrive = i + 1
    }
    // 多帧滑完（≥3 帧才贴住），而且要**到**（不能变成永远追不上），全程不过冲。
    assert.ok(framesToArrive >= 3, `${framesToArrive} 帧就贴住了 —— 那是跳不是滑`)
    assert.ok(Math.abs(steps.at(-1) - 0.325) < 0.005, `没滑到位：${steps.at(-1)}`)
    for (const s of steps) assert.ok(s <= 0.325 + 1e-6, `过冲：${s}`)
  })

  test('比收敛线还小的纠正不进滑行 —— 没有可见性，别为它关外推', () => {
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    f.observe(quadAt(CORRECTION_DONE / 2, 0), t += DT, 0, { correction: true })
    assert.equal(f.correcting, false)
  })

  test('带标但超过瞬移门（15%）照样直接切换', () => {
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    f.observe(quadAt(0.3, 0.3), t += DT, 0, { correction: true })
    const p = f.at(t, out())
    assert.ok(Math.abs(p[0] - 0.6) < 1e-6, `该瞬移没瞬移：${p[0]}`)
    assert.equal(f.correcting, false)
  })

  test('滑行有硬时限 —— 目标一直在动时不无限期关着外推', () => {
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    // 纠正落地的同时用户开始挥手机：目标持续移动，永远滑不进收敛线。
    let x = 0.025
    f.observe(quadAt(x, 0), t += DT, 0, { correction: true })
    assert.ok(f.correcting, '带标的门下纠正应当进滑行')
    for (let i = 0; i < 30 && f.correcting; i++) {
      x += 0.2e-3 * DT                               // 20%/s 持续运动
      f.observe(quadAt(x, 0), t += DT)
    }
    assert.equal(f.correcting, false, '硬时限没生效')
    assert.ok(t - 30 * DT <= CORRECTION_MAX_MS + 3 * DT, '退出得太晚')
  })

  test('滑行尾巴不再交回"快跟"—— 收敛线以内才退出', () => {
    // 第一版滑到门线（3.5%）就退出，剩下的尾巴被自适应低通快跟 + 外推又甩成一次
    // 小跳。现在滑到 CORRECTION_DONE 才退。
    const f = new QuadFilter()
    let t = settle(f, quadAt(0, 0), 0)
    const target = quadAt(0.06, 0)                   // 大纠正：走门 + 确认那条路
    for (let i = 0; i < 2; i++) f.observe(target, t += DT)
    assert.ok(f.correcting)
    // 滑到与目标只差 1%（已在门内、但在收敛线外）时必须**还在滑**。
    while (f.correcting) {
      f.observe(target, t += DT)
      const gap = Math.abs(f.at(t, out())[0] - 0.36)
      if (gap < 0.01 && gap > CORRECTION_DONE) {
        assert.ok(f.correcting || gap <= CORRECTION_DONE,
          `尾巴 ${gap} 被交回快跟（旧行为）`)
      }
      if (t > 5000) break
    }
    assert.ok(Math.abs(f.at(t, out())[0] - 0.36) <= CORRECTION_DONE + 1e-6,
      '退出滑行时必须已经贴住目标')
  })
})

#!/usr/bin/env node
/**
 * 四角滤波器的仿真台：在**延迟**和**抖动**两个对立指标上比较几种做法。
 *
 * ## 为什么要仿真，而不是在手机上试
 *
 * 这两个指标天生对立（滤得越狠越不抖、也越滞后），所以调参是在找帕累托前沿，
 * 而不是找一个"更好的数"。在真机上试的问题是：**没有真值**。手机不动时抖动可测但
 * 延迟不可测（静态时滞后为零）；手机动起来时延迟能感觉到但测不出来（不知道照片
 * 此刻真实在哪）。
 *
 * 仿真给出真值。输入是两组**从真机量来的**数字（见 `MEASURED`），所以它不是凭空造的
 * 运动 —— 噪声幅度、管线延迟、跟踪节奏都是实测值。
 *
 * ## 指标
 *
 * - **滞后 RMS**：滤波输出与"这一刻的真值"的距离。这是用户看到的"视频落在照片后面"。
 * - **抖动 RMS**：输出的逐帧一阶差分。这是用户看到的"视频在照片上抖"。
 *
 * 两个都用画幅比例（‰）表示，与真机分析脚本同一个量纲，可以直接对照。
 *
 * 用法：node test/sim/predict.mjs [--csv]
 */

/** 从真机量来的（小米 M2012K11C / Edge 150，静止对着一张打印照片，82 秒窗口）。 */
const MEASURED = {
  /** 跟踪一帧要多久（中位）。 */
  trackMs: 44,
  /** 相邻两次跟踪结果的间隔（中位）—— 被 trackMs 主导，不是 SEND_INTERVAL_MS。 */
  resultIntervalMs: 51,
  /** 四角画出去那一刻的陈旧度（中位）。滤波器要补的就是它。 */
  quadAgeMs: 88,
  /** 渲染帧间隔（中位）。 */
  frameMs: 16.5,
  /** 原始四角的逐帧抖动 RMS，画幅比例。 */
  noiseRms: 0.52e-3,
}

const SIM_SECONDS = 20

/**
 * 合成一条**静止与运动交替**的手持轨迹。
 *
 * ## 为什么必须交替，不能只有运动
 *
 * 滞后和抖动这两个指标在**不同的场景里**才可见：静止时看得见抖动、看不见滞后；
 * 运动时反过来（运动模糊 + 注意力在动的东西上，抖动根本注意不到）。拿一条一直在动的
 * 轨迹去评"抖动"，量到的其实是"跟得紧不紧"，于是任何延迟补偿都会被记成"更抖"——
 * 而那正是上一版仿真给出的假结论。
 *
 * 所以：5 秒静止（只有生理性微抖）、5 秒运动，交替。**滞后只在运动段算，抖动只在
 * 静止段算。** 一个滤波器要两边都好才算好。
 *
 * 运动段三成分叠加，每一段对应一种真实成分：慢速平移（对准照片）、中频摆动（手腕，
 * 约 1.5Hz）、高频微抖（生理性，约 8Hz）。不用单一正弦：单频信号会让外推看起来比实际
 * 好很多（相位可完美预测），而真实手抖是宽带的 —— 外推在高频上会**放大**噪声，
 * 那正是要暴露的代价。
 */
const SEG_MS = 5000
export function isMoving(tMs) {
  return Math.floor(tMs / SEG_MS) % 2 === 1
}
function truth(tMs) {
  const t = tMs / 1000
  // 生理性微抖：静止段也有，幅度小
  const micro = [
    0.0008 * Math.sin(8.0 * 2 * Math.PI * t + 0.3),
    0.0006 * Math.cos(7.3 * 2 * Math.PI * t + 2.0),
  ]
  if (!isMoving(tMs)) {
    // 静止段真值是**常数** —— 手机架在桌上时它确实不动，所以输出里任何变化都是噪声。
    // 这一点很重要：上一版给静止段也加了生理性微抖，于是"抖动"这个指标里混进了真实
    // 运动，把低通的抑噪能力算高了。手持的微抖属于运动段。
    const seg = Math.floor(tMs / SEG_MS)
    return [0.5 + ((0.02 * seg) % 0.1), 0.5 + ((0.01 * seg) % 0.1)]
  }
  return [
    0.5 + 0.10 * Math.sin(0.35 * 2 * Math.PI * t) + 0.020 * Math.sin(1.5 * 2 * Math.PI * t + 1.1) + micro[0] * 5,
    0.5 + 0.06 * Math.cos(0.27 * 2 * Math.PI * t) + 0.015 * Math.cos(1.7 * 2 * Math.PI * t + 0.4) + micro[1] * 5,
  ]
}

/** 确定性伪随机（不用 Math.random：仿真要可复现，跑两次得同一个数）。 */
let seed = 20260805
function rnd() {
  seed = (seed * 1103515245 + 12345) & 0x7fffffff
  return seed / 0x7fffffff
}
function gauss(sigma) {
  // Box-Muller。两个均匀数换一个正态数，够用。
  const u = Math.max(1e-9, rnd())
  const v = rnd()
  return sigma * Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v)
}

/**
 * 造一条"观测流"：每 `resultIntervalMs` 来一个测量值，而它测的是
 * `quadAgeMs` **之前**那一刻的真值（管线延迟），并带上测量噪声。
 */
function observations() {
  const out = []
  for (let t = 0; t <= SIM_SECONDS * 1000; t += MEASURED.resultIntervalMs) {
    const [x, y] = truth(t - MEASURED.quadAgeMs)
    out.push({ t, x: x + gauss(MEASURED.noiseRms), y: y + gauss(MEASURED.noiseRms) })
  }
  return out
}

// ── 被比较的几种滤波器 ────────────────────────────────────────────────
//
// 每个都是 `{ name, make() -> { push(obs), at(tMs) -> [x,y] } }`：
// `push` 收一个观测，`at` 问"现在该画在哪"。

/** 现状：一阶低通朝目标靠近，每渲染帧一次。没有任何延迟补偿。 */
const lowpass = (tau) => ({
  name: `低通 tau=${tau}`,
  make() {
    let cur = null
    let target = null
    let lastT = 0
    return {
      push(o) { target = [o.x, o.y] },
      at(t) {
        if (!target) return null
        if (!cur) cur = target.slice()
        const dt = lastT ? t - lastT : 0
        lastT = t
        const a = tau <= 0 ? 1 : 1 - Math.exp(-dt / tau)
        cur[0] += (target[0] - cur[0]) * a
        cur[1] += (target[1] - cur[1]) * a
        return cur
      },
    }
  },
})

/** 只外推，不平滑：位置 + 速度 × 需要补的时间。 */
const extrapolate = (leadMs, velTau) => ({
  name: `纯外推 lead=${leadMs} velTau=${velTau}`,
  make() {
    let last = null
    let vel = [0, 0]
    return {
      push(o) {
        if (last) {
          const dt = Math.max(1, o.t - last.t)
          const vx = (o.x - last.x) / dt
          const vy = (o.y - last.y) / dt
          const a = velTau <= 0 ? 1 : 1 - Math.exp(-dt / velTau)
          vel[0] += (vx - vel[0]) * a
          vel[1] += (vy - vel[1]) * a
        }
        last = o
      },
      at(t) {
        if (!last) return null
        const ahead = Math.min(leadMs, t - last.t + leadMs)
        return [last.x + vel[0] * ahead, last.y + vel[1] * ahead]
      },
    }
  },
})

/**
 * 低通 + 外推：先把观测低通掉噪声，再按平滑过的速度外推。
 *
 * 这是双指数（Holt）的形状：一个状态跟位置、一个状态跟速度，两个各自有时间常数。
 * `lead` 是要补的总时间 —— 管线延迟 + 低通自己的滞后。
 */
const holt = (posTau, velTau, leadMs) => ({
  name: `低通+外推 pos=${posTau} vel=${velTau} lead=${leadMs}`,
  make() {
    let pos = null
    let vel = [0, 0]
    let lastObsT = 0
    let lastRenderT = 0
    return {
      push(o) {
        if (!pos) { pos = [o.x, o.y]; lastObsT = o.t; return }
        const dt = Math.max(1, o.t - lastObsT)
        lastObsT = o.t
        const ap = posTau <= 0 ? 1 : 1 - Math.exp(-dt / posTau)
        const av = velTau <= 0 ? 1 : 1 - Math.exp(-dt / velTau)
        const prev = pos.slice()
        pos[0] += (o.x - pos[0]) * ap
        pos[1] += (o.y - pos[1]) * ap
        const vx = (pos[0] - prev[0]) / dt
        const vy = (pos[1] - prev[1]) / dt
        vel[0] += (vx - vel[0]) * av
        vel[1] += (vy - vel[1]) * av
      },
      at(t) {
        if (!pos) return null
        lastRenderT = t
        // 外推到"现在"：观测本身已经落后 lead，再加上从收到它到现在又过去的时间。
        const ahead = leadMs + (t - lastObsT)
        return [pos[0] + vel[0] * ahead, pos[1] + vel[1] * ahead]
      },
    }
  },
})

/**
 * **自适应**：按当前测得的速度决定补多少延迟、平滑多狠。
 *
 * 这是上面那张表逼出来的结论：滞后与抖动沿一条线换，说明用**一个**参数去平衡两个
 * 只在不同场景里可见的指标，本身就是错的问法。
 *
 * 规则两条，都只看一个量 —— 速度：
 *
 *  - **慢（≈静止）**：不外推、重平滑。静止时外推没有东西可补（速度是噪声），
 *    而它会把速度噪声乘上 lead 直接注入位置 —— 那就是"静止时视频在抖"。
 *  - **快（在动）**：全额外推、轻平滑。此时滞后是唯一可见的缺陷。
 *
 * `speedRef` 是"多快算在动"的分界，单位是画幅比例/毫秒。取值由噪声定：位置噪声
 * `noiseRms` 在相邻两次观测（间隔 dt）之间造成的假速度约 `noiseRms*√2/dt`，
 * 所以分界必须显著高于它，否则静止时的噪声会被当成运动。
 */
const adaptive = ({ posTauSlow, posTauFast, velTau, leadMs, speedRef }) => ({
  name: `自适应 pos=${posTauSlow}/${posTauFast} vel=${velTau} lead=${leadMs} ref=${speedRef}`,
  make() {
    let pos = null
    let vel = [0, 0]
    let lastObsT = 0
    return {
      push(o) {
        if (!pos) { pos = [o.x, o.y]; lastObsT = o.t; return }
        const dt = Math.max(1, o.t - lastObsT)
        lastObsT = o.t
        const speed = Math.hypot(vel[0], vel[1])
        const k = Math.min(1, speed / speedRef)
        const posTau = posTauSlow + (posTauFast - posTauSlow) * k
        const ap = posTau <= 0 ? 1 : 1 - Math.exp(-dt / posTau)
        const av = velTau <= 0 ? 1 : 1 - Math.exp(-dt / velTau)
        const prev = pos.slice()
        pos[0] += (o.x - pos[0]) * ap
        pos[1] += (o.y - pos[1]) * ap
        const vx = (pos[0] - prev[0]) / dt
        const vy = (pos[1] - prev[1]) / dt
        vel[0] += (vx - vel[0]) * av
        vel[1] += (vy - vel[1]) * av
      },
      at(t) {
        if (!pos) return null
        const speed = Math.hypot(vel[0], vel[1])
        const k = Math.min(1, speed / speedRef)
        const ahead = (leadMs + (t - lastObsT)) * k
        return [pos[0] + vel[0] * ahead, pos[1] + vel[1] * ahead]
      },
    }
  },
})

/**
 * 与 `adaptive` 同一套规则，但 **lead 不写死**：用"这个观测有多老"（渲染时刻减去观测
 * 时刻）加上平滑器自己的时间常数。
 *
 * 为什么这样更好：写死的 88 是**这台手机、这个库、这一刻**量出来的。换台快的手机
 * 跟踪 15ms、换个大库跟踪 120ms，写死的数就一边补过头、一边补不够 —— 而补过头比
 * 补不够更难看（视频冲到照片前面去，然后被拉回来，是一种"果冻"感）。
 *
 * 而"这个观测有多老"是运行时**免费就有**的量（`now - quadAt`，渲染循环里本来就在算），
 * 它自动包含了跟踪耗时、送帧节流、以及 rAF 的相位。加上 `posTau` 是补平滑器自身的
 * 一阶滞后（一阶低通对匀速输入的稳态相位延迟正好等于 tau）。
 */
const autoLead = ({ posTauSlow, posTauFast, velTau, speedRef, leadScale = 1 }) => ({
  name: `自测lead pos=${posTauSlow}/${posTauFast} vel=${velTau} ref=${speedRef} scale=${leadScale}`,
  make() {
    let pos = null
    let vel = [0, 0]
    let lastObsT = 0
    let obsAge = 0
    let curTau = posTauSlow
    return {
      push(o) {
        // 观测自带"它测的是多久之前"。仿真里就是 quadAgeMs；真实代码里是
        // 渲染时刻减 quadAt，同一个量。
        obsAge = MEASURED.quadAgeMs
        if (!pos) { pos = [o.x, o.y]; lastObsT = o.t; return }
        const dt = Math.max(1, o.t - lastObsT)
        lastObsT = o.t
        const k = Math.min(1, Math.hypot(vel[0], vel[1]) / speedRef)
        curTau = posTauSlow + (posTauFast - posTauSlow) * k
        const ap = curTau <= 0 ? 1 : 1 - Math.exp(-dt / curTau)
        const av = velTau <= 0 ? 1 : 1 - Math.exp(-dt / velTau)
        const prev = pos.slice()
        pos[0] += (o.x - pos[0]) * ap
        pos[1] += (o.y - pos[1]) * ap
        vel[0] += ((pos[0] - prev[0]) / dt - vel[0]) * av
        vel[1] += ((pos[1] - prev[1]) / dt - vel[1]) * av
      },
      at(t) {
        if (!pos) return null
        const k = Math.min(1, Math.hypot(vel[0], vel[1]) / speedRef)
        // 要补的：观测本身的陈旧度 + 从收到它到现在 + 平滑器自身的滞后
        const ahead = (obsAge + (t - lastObsT) + curTau) * k * leadScale
        return [pos[0] + vel[0] * ahead, pos[1] + vel[1] * ahead]
      },
    }
  },
})

// ── 跑 ───────────────────────────────────────────────────────────────
export function run(filters) {
  const obs = observations()
  const results = []
  for (const f of filters) {
    const inst = f.make()
    let oi = 0
    const moveErr = []
    const staticOut = []
    const moveOut = []
    for (let t = 0; t <= SIM_SECONDS * 1000; t += MEASURED.frameMs) {
      while (oi < obs.length && obs[oi].t <= t) inst.push(obs[oi++])
      const got = inst.at(t)
      if (!got) continue
      // 段切换后的第一秒不算：那一秒里滤波器在追赶，两个指标都不代表稳态。
      const intoSeg = t % SEG_MS
      if (intoSeg < 1000) continue
      const [tx, ty] = truth(t)
      if (isMoving(t)) {
        moveErr.push(Math.hypot(got[0] - tx, got[1] - ty))
        moveOut.push(got.slice())
      } else {
        // **按段分组**。不分组的话，不同静止段之间的位置偏移（我故意让每段停在不同
        // 位置，免得掩盖零点漂移）会把方差整个主导掉 —— 上一版就是这样，于是所有
        // 滤波器的"抖动"都是同一个 22‰，那个数其实是段间距离。
        staticOut.push([...got, Math.floor(t / SEG_MS)])
      }
    }
    /**
     * 抖动 = **位置标准差**，不是逐帧差分。
     *
     * 差分对"每 N 帧才更新一次"的序列不公平：那种序列的差分是 0,0,大跳，RMS 被系统性
     * 低估 √N 倍。而我们要比较的两个东西恰好一个每帧更新（平滑输出）、一个每 3 帧更新
     * （原始观测）。标准差没有这个偏置，而且它就是用户看到的"抖动幅度"。
     *
     * 静止段的真值是常数，所以标准差直接就是噪声幅度。
     */
    const stdOf = (arr) => {
      if (arr.length < 2) return 0
      const mx = arr.reduce((a, p) => a + p[0], 0) / arr.length
      const my = arr.reduce((a, p) => a + p[1], 0) / arr.length
      const v = arr.reduce((a, p) => a + (p[0] - mx) ** 2 + (p[1] - my) ** 2, 0) / arr.length
      return Math.sqrt(v)
    }
    /** 每个静止段各自算标准差，再按均方合并。 */
    const jitterOf = (arr) => {
      const bySeg = new Map()
      for (const p of arr) {
        const k = p[2] ?? 0
        if (!bySeg.has(k)) bySeg.set(k, [])
        bySeg.get(k).push(p)
      }
      const vs = [...bySeg.values()].filter((g) => g.length > 10).map(stdOf)
      if (!vs.length) return 0
      return Math.sqrt(vs.reduce((a, b) => a + b * b, 0) / vs.length)
    }
    results.push({
      name: f.name,
      // 滞后只在**运动段**算 —— 静止时没有滞后可言。
      lagRms: Math.sqrt(moveErr.reduce((a, b) => a + b * b, 0) / Math.max(1, moveErr.length)),
      lagMax: moveErr.length ? Math.max(...moveErr) : 0,
      // 抖动只在**静止段**算 —— 运动段的"逐帧变化"主要是真实运动，不是抖。
      jitterRms: jitterOf(staticOut),
      moveJitter: stdOf(moveOut),
    })
  }
  return results
}

const FILTERS = [
  lowpass(60),          // 现状
  lowpass(0),           // 完全不平滑
  holt(60, 150, 88),    // 无条件外推
  extrapolate(88, 120),
]

/** 自适应那一族的参数网格。 */
function grid() {
  const out = []
  for (const posTauSlow of [120, 180, 220]) {
    for (const posTauFast of [25, 40]) {
      for (const velTau of [100, 150]) {
        for (const speedRef of [0.04e-3, 0.06e-3, 0.1e-3]) {
          for (const leadScale of [0.7, 0.85, 1.0]) {
            out.push(autoLead({ posTauSlow, posTauFast, velTau, speedRef, leadScale }))
          }
        }
      }
    }
  }
  for (const posTauSlow of [90, 120, 160, 220]) {
    for (const posTauFast of [25, 40, 60]) {
      for (const velTau of [100, 150, 220]) {
        for (const leadMs of [88, 110]) {
          for (const speedRef of [0.05e-3, 0.1e-3, 0.2e-3]) {
            out.push(adaptive({ posTauSlow, posTauFast, velTau, leadMs, speedRef }))
          }
        }
      }
    }
  }
  return out
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const rows = run(FILTERS)
  const base = rows[0]
  console.log(`仿真：${SIM_SECONDS}s 手持运动，观测每 ${MEASURED.resultIntervalMs}ms 一次、` +
    `落后 ${MEASURED.quadAgeMs}ms、噪声 ${(MEASURED.noiseRms * 1000).toFixed(2)}‰\n`)
  console.log('滤波器'.padEnd(50), '运动段滞后‰', '静止段抖动‰', '滞后峰值‰', ' 相对现状')
  for (const r of rows) {
    const rel = `滞后 ${(100 * r.lagRms / base.lagRms - 100).toFixed(0)}%  抖动 ${(100 * r.jitterRms / base.jitterRms - 100).toFixed(0)}%`
    console.log(
      r.name.padEnd(50),
      (r.lagRms * 1000).toFixed(2).padStart(11),
      (r.jitterRms * 1000).toFixed(3).padStart(12),
      (r.lagMax * 1000).toFixed(1).padStart(9),
      ' ', r === base ? '（基线）' : rel,
    )
  }

  // 网格搜索 + 帕累托前沿。**两个指标都不比现状差**的才列出来 —— 那是唯一
  // 不需要"用户更在意哪一个"这种主观判断就能下的结论。
  const g = run(grid())
  const better = g.filter((r) => r.lagRms < base.lagRms && r.jitterRms <= base.jitterRms * 1.02)
  better.sort((a, b) => a.lagRms - b.lagRms)
  console.log(`\n网格 ${g.length} 组，其中 ${better.length} 组**两个指标都不差于现状**（抖动允许 +2%）：`)
  for (const r of better.slice(0, 10)) {
    console.log('  ' + r.name.padEnd(50),
      `滞后 ${(r.lagRms * 1000).toFixed(2)}‰ (${(100 * r.lagRms / base.lagRms - 100).toFixed(0)}%)`,
      ` 抖动 ${(r.jitterRms * 1000).toFixed(3)}‰ (${(100 * r.jitterRms / base.jitterRms - 100).toFixed(0)}%)`,
      ` 峰值 ${(r.lagMax * 1000).toFixed(1)}‰`)
  }
  if (!better.length) {
    console.log('  （一组都没有 —— 说明这一族做不到帕累托改进，要换思路）')
    g.sort((a, b) => a.lagRms - b.lagRms)
    for (const r of g.slice(0, 5)) console.log('  最低滞后：' + r.name, (r.lagRms*1000).toFixed(2), (r.jitterRms*1000).toFixed(3))
  }
}

export { MEASURED, truth, lowpass, extrapolate, holt }

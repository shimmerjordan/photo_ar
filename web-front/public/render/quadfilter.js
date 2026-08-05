/**
 * 四角的**自适应预测滤波器**：静止时重平滑、运动时外推补延迟。
 *
 * ## 它替掉了什么，为什么
 *
 * 上一版是一条一阶低通（`approach` + `smoothingAlpha`，tau=60ms）朝最新四角靠近。
 * 两个真机实测把它否掉了（小米 M2012K11C / Edge 150，架住对着一张打印照片 82 秒）：
 *
 * 1. **它几乎没削掉抖动。** 静止时原始四角的位置标准差 1.01‰ 画幅，平滑后 0.90‰ ——
 *    **只削掉 11%**。原因是噪声的时间尺度（每 51ms 来一个新观测）与 tau=60ms 相当，
 *    而一阶低通只压得住比 tau 快得多的成分。
 * 2. **它却付了三分之一的滞后。** 一阶低通对匀速输入的稳态相位延迟正好等于 tau，
 *    也就是在管线本身的 88ms 之上又加 60ms。总计约 148ms —— 手持转动 30°/s 时是
 *    4.4° 的角误差，屏幕上就是"视频落在照片后面"。
 *
 * 而管线延迟本身（88ms 中位：跟踪 44ms + 送帧节流 + rAF 相位）是量出来的，不是猜的。
 *
 * ## 为什么"自适应"是这里唯一的正解
 *
 * 滞后和抖动只在**不同场景**里可见：静止时看得见抖动、看不见滞后；运动时反过来
 * （运动模糊 + 注意力在动的东西上）。所以拿一个固定参数去平衡它们，无论怎么调都是在
 * 一条帕累托前沿上左右挪 —— `test/sim/predict.mjs` 里那张表就是这个形状。
 *
 * 按**速度**分档之后两个指标可以同时改善：
 *
 * | | 静止 | 运动 |
 * |---|---|---|
 * | 平滑时间常数 | [TAU_SLOW_MS]（重，真正压噪声） | [TAU_FAST_MS]（轻，跟得紧） |
 * | 外推 | **不外推** | 补 [LEAD_SCALE] 的延迟 |
 *
 * 静止时不外推是关键：那时速度全是噪声，而外推会把速度噪声乘上 lead 直接注入位置 ——
 * 那就是"视频明明没动却在抖"。
 *
 * 仿真结果（`test/sim/predict.mjs`，20 秒静止/运动交替，噪声与延迟都用真机实测值）：
 * **运动段滞后 -36%，静止段抖动 -31%** —— 两个都好，不是交换。
 *
 * ## lead 为什么不写死
 *
 * 它 = 这个观测有多老（渲染时刻减 `quadAt`，渲染循环里本来就在算）+ 平滑器自身的
 * 时间常数。写死一个 88 的话，换台快手机（跟踪 15ms）会补过头，而**补过头比补不够
 * 更难看** —— 视频冲到照片前面再被拉回来，是一种果冻感。
 *
 * `LEAD_SCALE = 0.7` 是刻意的欠补偿：速度估计自己有一个 [VEL_TAU_MS] 的滞后，
 * 补满 100% 在加速段会过冲。仿真里 0.7 比 1.0 好（滞后 -36% vs -33%）。
 */

/** 静止时的位置平滑时间常数（ms）。大 = 更稳。 */
export const TAU_SLOW_MS = 180

/** 运动时的位置平滑时间常数（ms）。小 = 跟得紧。 */
export const TAU_FAST_MS = 25

/** 速度自己的平滑时间常数（ms）。太小则速度噪声大，太大则加速跟不上。 */
export const VEL_TAU_MS = 100

/**
 * "多快算在动"的分界，单位是**画幅比例每毫秒**。
 *
 * 0.05‰/ms = 每秒走 5% 画幅。取值下界由噪声定：位置噪声 0.52‰ 在相邻两次观测
 * （51ms）之间造成的假速度约 0.52‰×√2/51 ≈ 0.014‰/ms，所以分界必须显著高于它 ——
 * 否则静止时的噪声会被当成运动，自适应就白做了。0.05 是 3.5 倍。
 */
export const SPEED_REF = 0.05e-3

/** 延迟补偿的比例。刻意小于 1，理由见模块 docstring 最后一段。 */
export const LEAD_SCALE = 0.7

/**
 * 一个四角滤波器。**有状态**，一次锁定用一个实例，丢锁就 `reset()`。
 *
 * 坐标是归一化图像坐标（0..1 是整幅相机图，可越界），8 个一组 = 四个角。
 * 四个角**各自独立**滤波：它们的噪声是相关的，但联合建模（比如滤单应矩阵的 8 个
 * 自由度）在这里没有收益 —— 独立滤波之后 `unitSquareH` 照样能解出单应矩阵，而
 * 联合建模会引入"矩阵参数的平滑"与"角点的平滑"不是同一件事这种难查的偏差。
 */
export class QuadFilter {
  constructor(opts = {}) {
    this.tauSlow = opts.tauSlow ?? TAU_SLOW_MS
    this.tauFast = opts.tauFast ?? TAU_FAST_MS
    this.velTau = opts.velTau ?? VEL_TAU_MS
    this.speedRef = opts.speedRef ?? SPEED_REF
    this.leadScale = opts.leadScale ?? LEAD_SCALE
    this.reset()
  }

  reset() {
    /** 平滑后的位置，8 个。null = 还没收到过观测。 */
    this.pos = null
    /** 每个坐标的速度（画幅比例/ms），8 个。 */
    this.vel = new Float32Array(8)
    /** 上一次观测的时刻。 */
    this.lastObsAt = 0
    /** 当前用的位置时间常数，`at()` 要拿它算 lead。 */
    this.tau = this.tauSlow
    /** 上一次观测**测的是多久之前** —— 也就是要补的管线延迟。 */
    this.obsAge = 0
    return this
  }

  /** 现在这一档的"运动程度"，0=静止 1=全速。给诊断用。 */
  get motion() {
    let s = 0
    for (let i = 0; i < 8; i += 2) s = Math.max(s, Math.hypot(this.vel[i], this.vel[i + 1]))
    return Math.min(1, s / this.speedRef)
  }

  /**
   * 收一个新观测。
   *
   * @param quad 长度 8 的归一化四角
   * @param atMs 收到它的时刻（`performance.now()`）
   * @param ageMs 这个四角**测的是多久之前**的画面。渲染循环里就是 `now - quadAt`；
   *   给 0 表示"就是现在"（那时外推只补平滑器自身的滞后）。
   */
  observe(quad, atMs, ageMs = 0) {
    if (!quad || quad.length !== 8) return this
    this.obsAge = Math.max(0, ageMs)
    if (!this.pos) {
      // 第一次直接落上去：没有速度可估，而"从画面中心慢慢飘过来"比直接出现难看得多。
      this.pos = Float32Array.from(quad)
      this.vel.fill(0)
      this.lastObsAt = atMs
      return this
    }
    const dt = Math.max(1, atMs - this.lastObsAt)
    this.lastObsAt = atMs
    // 用**上一帧算出的**运动程度来选这一帧的时间常数。用当前观测反算会引入一个隐式的
    // 一阶环路（tau 影响 pos，pos 影响 vel，vel 又影响 tau），调起来不可预测。
    const k = this.motion
    this.tau = this.tauSlow + (this.tauFast - this.tauSlow) * k
    const ap = this.tau <= 0 ? 1 : 1 - Math.exp(-dt / this.tau)
    const av = this.velTau <= 0 ? 1 : 1 - Math.exp(-dt / this.velTau)
    for (let i = 0; i < 8; i++) {
      const prev = this.pos[i]
      this.pos[i] += (quad[i] - prev) * ap
      const v = (this.pos[i] - prev) / dt
      this.vel[i] += (v - this.vel[i]) * av
    }
    return this
  }

  /**
   * "现在"该把四角画在哪。
   *
   * @param nowMs 当前时刻
   * @param out 长度 8 的输出缓冲（原地写，避免每帧分配）
   * @returns out，或 null（还没有观测）
   */
  at(nowMs, out) {
    if (!this.pos || !out || out.length !== 8) return null
    const k = this.motion
    // 要补的三段：观测本身的陈旧度、从收到它到现在、平滑器自身的一阶滞后。
    // 乘 k 是"静止时不外推"，乘 leadScale 是刻意欠补偿。
    const ahead = (this.obsAge + Math.max(0, nowMs - this.lastObsAt) + this.tau) * k * this.leadScale
    for (let i = 0; i < 8; i++) out[i] = this.pos[i] + this.vel[i] * ahead
    return out
  }
}

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
 *
 * 而 [LEAD_MAX_MS] 是硬上界。它是**真机上抓出来的**，仿真暴露不了 —— 那边观测间隔
 * 固定 51ms，而真机上跟踪一慢观测间隔就掉到 208ms，外推量算到 234ms，结果是
 * "平滑后的抖动比原始还大一倍"。详见那个常量。
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
 * 异常观测门：新观测偏离**预测位置**超过画幅的这个比例，就不直接跟。
 *
 * ## 它防的是"跳变"，与平滑是两件事
 *
 * 自适应低通（本文件原本的全部内容，One-Euro 那一族）处理的是**连续**信号上的
 * 噪声与滞后。它对不连续无能为力：一次 51ms 间隔上 5% 画幅的跳（tauFast=25ms 时
 * ap≈0.87）基本原样穿过 —— 用户看到的就是视频"啪"地跳到新位置。真机上两类事件
 * 正好长这样：
 *
 * 1. **定期重锚**（pipeline.REANCHOR_MS）：外观重匹配把漂移一口气纠正回来。角度
 *    越大漂移越大，纠正也就越大 —— "大角度场景跳变"的主要来源。
 * 2. **低内点下的 RANSAC 抖动**：大角度下内点少，单应解在帧间跳来跳去，偶尔给出
 *    一帧明显错的四角。
 *
 * 跟踪器的标准配方（Kalman 验证门 + 持续性确认，Vuforia/ARCore 系的位姿平滑同理）：
 * 离谱的观测先**扣下**（可能是单帧毛刺），下一帧还在同一个新位置才认（是真纠正），
 * 认了之后**慢速滑过去**而不是瞬移 —— 用户不可能急速调整姿势，视频也不该。
 *
 * 门取 3.5% 画幅：正常跟踪的帧间残差（预测误差）在千分位。取显著高于它的值，
 * 宁可多确认一帧也别放跳变过去。
 *
 * ## 门只负责**来源不明**的跳；重锚纠正走 `correction` 标（真机抓出来的漏洞）
 *
 * 第一版指望这个门把重锚纠正也拦下来。真机复测（2026-08-07，同一台小米架住对着
 * 打印照片）否掉了：那个场景的重锚纠正是 **2%~2.5% 画幅** —— 在门下面。穿过正常
 * 路径后，单帧台阶把速度估计瞬间拉满（k→1、tau→tauFast、外推开到 vel×ahead），
 * 一两帧内整步应用 —— 录屏里每 2 秒一次的"啪"就是它。而把门压到 2% 以下又会
 * 在急加速时误伤连贯运动。
 *
 * 正解是**来源感知**：pipeline 明确知道哪一帧的四角建立在刚换过的种子上
 * （`corrected` 标，见 pipeline._track），那样的观测**不论幅度**直接进滑行 ——
 * 不用猜、不用确认。门保留，管的是没打标的不连续（RANSAC 单帧毛刺）。
 */
export const JUMP_GATE = 0.035

/** 连续几帧落在同一个新位置才认。2 = 一帧毛刺被丢弃、真纠正多等 ~50ms（不可见）。 */
export const JUMP_CONFIRM = 2

/**
 * 认下纠正之后滑过去的时间常数（ms）。120ms ≈ 三四帧滑完 —— 足够快到不觉得"追不上"，
 * 足够慢到不觉得"跳"。滑行期间速度清零、外推关闭：纠正是**不连续**，把它的斜率喂进
 * 速度估计会让外推把下一段甩过头。
 */
export const CORRECTION_TAU_MS = 120

/**
 * 纠正滑行的**收敛线**（画幅比例）：滑到与目标差这么点，就交还正常滤波。
 *
 * 第一版用 JUMP_GATE（3.5%）当退出线 —— 结果剩下的 3.5% 以内交回自适应低通去
 * "快跟"，正好把纠正的尾巴又变成一次小跳。收敛线要显著高于静止噪声（~0.1%）、
 * 显著低于可察觉的位移（~1%），0.5% 取中间。
 */
export const CORRECTION_DONE = 0.005

/**
 * 纠正滑行的**硬时限**（ms）。滑行期间速度清零、外推关闭，用户若恰好在挥手机，
 * 长滑行 = 长滞后 —— 到时限就交还正常滤波，它自己会跟上。取 5×CORRECTION_TAU_MS：
 * 纯指数逼近到这时余量已 <1%。
 */
export const CORRECTION_MAX_MS = 600

/**
 * 瞬移门：纠正大到这个程度（15% 画幅）就直接跳过去。那已经不是"贴合偏了"，是
 * "完全换了个位置"（比如照片挪了、或重锚发现之前锁错了地方）—— 从错误位置花半秒
 * 爬过去比一次干脆的切换更难看。
 */
export const TELEPORT = 0.15

/**
 * 外推量的**硬上界**（ms）。超过它就不再往前补 —— 宁可落后，不能飞出去。
 *
 * ## 这一条是真机上抓出来的，仿真暴露不了
 *
 * 外推量 = 观测的陈旧度 + 从收到它到现在 + 平滑器的 tau。中间那一项在健康状态下是
 * 几十毫秒，但它**没有上界**：跟踪一慢下来（真机上量到观测间隔从 51ms 掉到 208ms）、
 * 或者主线程被别的东西卡住，它就线性增长。实测那一段的外推量算到了 **234ms**，
 * 结果是"平滑后的抖动比原始还大一倍"——视频在照片上晃。
 *
 * 仿真里观测间隔是固定的 51ms，所以这条路径永远走不到。**固定采样率的仿真测不出
 * 采样率变化引起的问题** —— 这是那份仿真的已知盲区，写在这里。
 *
 * 120ms 的依据：健康状态下观测间隔是 51~93ms（真机，取决于跟踪耗时），而外推超过
 * 一个观测间隔就已经是在赌下一帧了。给到约 1.5 个间隔，再多就是猜。
 */
export const LEAD_MAX_MS = 120

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
    this.leadMax = opts.leadMax ?? LEAD_MAX_MS
    this.jumpGate = opts.jumpGate ?? JUMP_GATE
    this.jumpConfirm = opts.jumpConfirm ?? JUMP_CONFIRM
    this.correctionTau = opts.correctionTau ?? CORRECTION_TAU_MS
    this.correctionDone = opts.correctionDone ?? CORRECTION_DONE
    this.correctionMax = opts.correctionMax ?? CORRECTION_MAX_MS
    this.teleport = opts.teleport ?? TELEPORT
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
    /** 门外的候选新位置（可能是毛刺，也可能是纠正的第一帧）。 */
    this.pending = null
    this.pendingN = 0
    /** 正在向一个已确认的纠正目标滑（见 CORRECTION_TAU_MS）。 */
    this.correcting = false
    /** 这次滑行从什么时候开始 —— CORRECTION_MAX_MS 的硬时限从这里起算。 */
    this.correctingSince = 0
    /** 被丢弃的毛刺帧数，给诊断看。 */
    this.rejected = 0
    return this
  }

  /** 观测与预测位置的最大角点距离（画幅比例）。门控判据。 */
  _deviation(quad, dtMs) {
    let worst = 0
    for (let i = 0; i < 8; i += 2) {
      const px = this.pos[i] + this.vel[i] * dtMs
      const py = this.pos[i + 1] + this.vel[i + 1] * dtMs
      worst = Math.max(worst, Math.hypot(quad[i] - px, quad[i + 1] - py))
    }
    return worst
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
   * @param opts `{correction: true}` = 这个观测是**已知的纠正**（pipeline 在重锚
   *   补种子后的第一个四角上打的标）。不论幅度直接进滑行 —— 门只该管来源不明的跳。
   */
  observe(quad, atMs, ageMs = 0, opts = null) {
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

    // ── 来源已知的纠正：不用猜、不用确认，直接滑（见 JUMP_GATE 注释里的真机漏洞）──
    if (opts?.correction && !this.correcting) {
      const d = this._deviation(quad, atMs - this.lastObsAt)
      if (d > this.teleport) {
        // 完全换了位置：干脆切换，别从错误位置爬过去。
        this.pos.set(quad)
        this.vel.fill(0)
        this.pending = null
        this.pendingN = 0
        this.lastObsAt = atMs
        return this
      }
      if (d > this.correctionDone) {
        this.correcting = true
        this.correctingSince = atMs
        // 纠正是不连续，斜率不能进速度估计（外推会把下一段甩过头）。
        this.vel.fill(0)
        this.pending = null
        this.pendingN = 0
      }
      // 比收敛线还小的纠正没有可见性，交给正常路径按普通观测消化。
    }

    // ── 跳变门（见 JUMP_GATE 的注释：来源不明的不连续 —— RANSAC 毛刺等）────
    if (!this.correcting) {
      const d = this._deviation(quad, atMs - this.lastObsAt)
      if (d > this.jumpGate) {
        // 离谱的观测不直接跟。先看它是不是与上一帧的候选一致 —— 一致就是真纠正。
        const nearPending = this.pending &&
          Math.max(
            ...[0, 2, 4, 6].map((i) =>
              Math.hypot(quad[i] - this.pending[i], quad[i + 1] - this.pending[i + 1])),
          ) < this.jumpGate
        if (nearPending) {
          this.pendingN++
          if (this.pendingN >= this.jumpConfirm) {
            this.pending = null
            this.pendingN = 0
            if (d > this.teleport) {
              // 完全换了位置：干脆切换，别从错误位置爬过去。
              this.pos.set(quad)
              this.vel.fill(0)
              this.lastObsAt = atMs
              return this
            }
            // 确认是纠正：滑过去。速度清零 —— 纠正是不连续，把它的斜率喂进速度
            // 估计会让外推把下一段甩过头。
            this.correcting = true
            this.correctingSince = atMs
            this.vel.fill(0)
            // 落到下面的正常路径，用 correctionTau 走第一步。
          } else {
            this.lastObsAt = atMs
            return this
          }
        } else {
          // 新的候选。这一帧不动 —— 单帧毛刺在这里被整个吞掉。
          this.pending = Float32Array.from(quad)
          this.pendingN = 1
          this.rejected++
          this.lastObsAt = atMs
          return this
        }
      } else {
        this.pending = null
        this.pendingN = 0
      }
    }

    this.lastObsAt = atMs
    if (this.correcting) {
      // 纠正滑行：固定时间常数的纯指数逼近，速度保持 0（外推关闭，见上）。
      const ap = 1 - Math.exp(-dt / this.correctionTau)
      for (let i = 0; i < 8; i++) this.pos[i] += (quad[i] - this.pos[i]) * ap
      // 滑到收敛线（CORRECTION_DONE，不是门线 —— 门线退出会把尾巴交回"快跟"，
      // 又变一次小跳）就回到正常模式，速度从零重新热身（velTau ≈ 100ms）。
      // 硬时限兜底：用户若正在挥手机，目标一直在动，永远滑不进收敛线 ——
      // 到时限交还正常滤波，它自己跟得上。
      if (this._deviation(quad, 0) <= this.correctionDone ||
          atMs - this.correctingSince > this.correctionMax) this.correcting = false
      return this
    }

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
    // 乘 k 是"静止时不外推"，乘 leadScale 是刻意欠补偿，最后**夹到上界**。
    // 上界那一步不是防御性编程：没有它，观测一稀疏外推量就线性增长，
    // 真机上量到过 234ms —— 那时输出的抖动比原始观测还大一倍。
    const raw = (this.obsAge + Math.max(0, nowMs - this.lastObsAt) + this.tau) * k * this.leadScale
    const ahead = Math.min(raw, this.leadMax)
    for (let i = 0; i < 8; i++) out[i] = this.pos[i] + this.vel[i] * ahead
    return out
  }
}

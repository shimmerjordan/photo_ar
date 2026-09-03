/**
 * 送帧节奏。**这个文件回答一个问题：现在这一帧值不值得算。**
 *
 * 改造前节奏恒等于 worker 的最大吞吐（16ms 地板 + 2 帧在途）——不管手机是在动、
 * 静止看视频、还是扣在桌上朝天花板。后两种是真实现场最长的两段时间，而它们恰恰
 * 不需要每秒二十次光流 / 全库检测：静止时四角不动，检测不到时下一帧大概率也检测不到。
 *
 * 判据只用**已经有的量**：`QuadFilter.motion`（0 静止 → 1 全速）、上一条结果的
 * `reason`、以及距上一份证据多久（命中 / 累积链在攒 / 四角还在）。任一条抬头立刻回满速，
 * 因为这个函数每次 `maybeSend` 都重新算，没有滞后状态。
 */
export const PACE = Object.freeze({
  /** 满速地板。与改造前的 `SEND_INTERVAL_MS` 相同。 */
  FAST: 16,
  /** 锁定且静止。四角每 100ms 校一次足够 —— `LEAD_MAX_MS` 是 120，静止段外推量≈0。 */
  STILL: 100,
  /** 10 秒认不出来：三分之一速。 */
  SLOW: 300,
  /** 30 秒认不出来、或画面里根本没纹理：约 1.4 次/秒。累积链的窗口随之放宽，见 streak.streakWindow。 */
  IDLE: 700,
})

/** `QuadFilter.motion` 低于它算静止。速度 8% 的 speedRef，肉眼刚能看出在动的量级。 */
export const MOTION_STILL = 0.08
export const IDLE_SLOW_MS = 10_000
export const IDLE_MS = 30_000

/**
 * @param locked 已锁定且手上有四角
 * @param motion `QuadFilter.motion`，缺省当作 1（在动）
 * @param idleMs 距上一份证据多久
 * @param reason 上一条 worker 结果的 reason
 */
export function sendInterval({ locked = false, motion = 1, idleMs = 0, reason = '' } = {}) {
  if (locked) return motion < MOTION_STILL ? PACE.STILL : PACE.FAST
  if (reason === 'no_features') return PACE.IDLE
  if (idleMs >= IDLE_MS) return PACE.IDLE
  if (idleMs >= IDLE_SLOW_MS) return PACE.SLOW
  return PACE.FAST
}

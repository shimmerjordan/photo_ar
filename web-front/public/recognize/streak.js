/**
 * 跨帧证据累积：连续几帧都指向同一张，就当成一次命中。
 *
 * ## 这是 `photoar.streak` 的浏览器版，而且**这边才是它真正该待的地方**
 *
 * 服务端那份的模块 docstring 里专门解释过"为什么状态在服务端而不是客户端"，理由是
 * 信息泄漏：客户端累积要求服务端把**未命中时的最佳猜测**（photoId）回给客户端，而
 * `weak` 那一支不跑授权检查，直接回 photoId 就是一次泄漏。
 *
 * **那条理由在网页版这边不成立。** 网页版的识别整个跑在浏览器里，而浏览器手上的库
 * （`/api/lib`）本来就**只包含这个用户被授权的那些照片** —— 它能猜到的每一个 photoId
 * 都是它已经有权看的。所以这里没有新增的暴露面，而累积放在客户端还白拿两样好处：
 * 不占服务端内存、也不需要"同一个客户端稳定是同一个 key"这种约定。
 *
 * ## 为什么必须有它（这条是量出来的）
 *
 * 真机实测（小米 M2012K11C，架住对着一张打印照片，96 次检测）：内点数
 * **中位 30、最大 38**，而门槛是 40 —— **一次都没到过**，分布整个贴在门槛下面。
 * 分布是 `20-29: 36 帧, 30-39: 53 帧, 40+: 0 帧`。照片确实被匹配上了（30 内点不是
 * 噪声，runner-up 只有个位数），只是视角偏、照片在画面里偏小，永远差那几分。
 *
 * 用户看到的就是"认不出来"。而连续三帧都指向同一张、每帧都是压倒性的第一名，
 * 这件事本身就是单帧判定完全没用上的一份免费证据。
 *
 * ## 它新增的误识别面
 *
 * 必须写在前面：单帧门槛 40 原本挡住了真实误识别（服务端 `verify.MIN_INLIERS` 记录的
 * 34 条真实误识别 p95=36、**最大 39**），而这里把 30~39 放进来了。挡住它的不再是门槛，
 * 是"连续 [need] 帧 + 每帧比值 ≥ [ratio]"。
 *
 * 所以挡不住的恰好是**能稳定误配的那一类**：一张库外照片与库内某张几何上真的相似，
 * 它每一帧都真的很像，连续性和比值都拦不住。这与服务端是同一个已知代价，
 * 而入库路径上的去重闸门（`dedup`）本来就在挡这一类。
 *
 * 命中带专门的 `reason: 'streak'`，好让它在诊断日志里与单帧命中分得开 —— 不这么做的话
 * 这条路带来的误识别会混进单帧命中里，永远量不出来。
 */

/** 参数默认值。**与服务端 `streak.py` 里那几个常量是同一组数**。 */
export const STREAK_DEFAULTS = {
  /** 进入累积的**软**门槛。低于它说明"没看清"而不是"差一点"，不算证据。 */
  softMin: 30,
  /** 要连续几帧。 */
  need: 3,
  /** 每一帧都必须是压倒性的第一名。2.0 比单帧判定的 1.5 更严 —— 累积放宽了绝对
   *  分数，就得在相对分数上收紧，否则两条都松。 */
  ratio: 2.0,
  /**
   * 相邻两帧的最大间隔（ms）。超了算断链。
   *
   * 2 秒：浏览器这边一次检测在真机上量到 560ms（有词表）到 620ms（无词表），
   * 所以 2 秒容得下 3~4 帧，而"举着手机晃过去偶尔扫到"那种不连续的擦碰攒不起来。
   */
  windowMs: 2000,
}

/**
 * 一条累积链。浏览器里只有一个客户端（它自己），所以不需要服务端那套 key + LRU。
 */
export class Streak {
  constructor(opts = {}) {
    this.configure(opts)
    /** 链上每一帧：`{photoId, inliers, at}`。攒够就清空。 */
    this.chain = []
  }

  /**
   * 更新参数。**每帧都可以调** —— 这几个数是服务端的热配置（管理台上能改），
   * 而这个对象是长生命周期的。构造时定死的话，改配置要么不生效、要么得重建对象
   * 而把攒了一半的链丢掉。
   */
  configure({ softMin, need, ratio, windowMs } = {}) {
    this.softMin = Number.isFinite(softMin) ? softMin : (this.softMin ?? STREAK_DEFAULTS.softMin)
    this.need = Number.isFinite(need) ? Math.max(2, need) : (this.need ?? STREAK_DEFAULTS.need)
    this.ratio = Number.isFinite(ratio) ? ratio : (this.ratio ?? STREAK_DEFAULTS.ratio)
    this.windowMs = Number.isFinite(windowMs) ? windowMs : (this.windowMs ?? STREAK_DEFAULTS.windowMs)
    return this
  }

  reset() {
    this.chain.length = 0
    return this
  }

  /** 链上现在攒了几帧、指着哪张。给诊断用。 */
  get progress() {
    return { n: this.chain.length, need: this.need, photoId: this.chain.at(-1)?.photoId ?? null }
  }

  /**
   * 把**未命中**那一帧的候选分数交进来。
   *
   * @param results `verifyPair` 的原样输出数组（不必排序），每项要有 `{photoId, inliers, det}`
   * @param nowMs 这一帧的时刻
   * @param det 一对 `[detMin, detMax]`，行列式的合法区间。累积不能绕过它 ——
   *   行列式越界意味着矩阵已经退化，那不是"差几分"，是"算错了"。
   * @returns 攒够了返回 `{photoId, inliers, top}`，否则 null
   */
  offer(results, nowMs, det) {
    const frame = this._evidence(results, nowMs, det)
    if (!frame) {
      // 这一帧不算证据 —— 链要**断掉**而不是忽略。忽略的话"举着手机晃过去偶尔扫到"
      // 也会被攒成命中，而那不是"用户在看这张照片"。
      this.chain.length = 0
      return null
    }
    const last = this.chain.at(-1)
    if (last && (last.photoId !== frame.photoId
      || nowMs - last.at > this.windowMs
      // 时间倒流（挂起唤醒、NTP 校时）当成断链：负间隔算不出"连续"。
      || nowMs < last.at)) {
      this.chain.length = 0
    }
    this.chain.push(frame)
    if (this.chain.length < this.need) return null
    // 攒够了。清空 —— 下一次要重新攒，否则一次累积会让后面每一帧都"命中"。
    const inliers = Math.max(...this.chain.map((f) => f.inliers))
    this.chain.length = 0
    return { photoId: frame.photoId, inliers, top: frame.top }
  }

  /** 这一帧算不算一份证据。 */
  _evidence(results, nowMs, [detMin, detMax]) {
    if (!results || results.length === 0) return null
    let top = null
    let runnerUp = 0
    for (const r of results) {
      if (!r || !Number.isFinite(r.inliers)) continue
      if (!top || r.inliers > top.inliers) {
        if (top) runnerUp = Math.max(runnerUp, top.inliers)
        top = r
      } else if (r.inliers > runnerUp) {
        runnerUp = r.inliers
      }
    }
    if (!top || !top.h) return null
    // 绝对分数：够"差一点"，但不够"看清了"。
    if (top.inliers < this.softMin) return null
    // 行列式：累积**不能**绕过它。越界是矩阵退化，不是分数不够。
    if (!(top.det >= detMin && top.det <= detMax)) return null
    // 相对分数：必须是压倒性的第一名。runnerUp 为 0（只有一个候选）时这一条自动通过 ——
    // 那正是"毫无争议"的极端情形。
    if (runnerUp > 0 && top.inliers < this.ratio * runnerUp) return null
    return { photoId: top.photoId, inliers: top.inliers, at: nowMs, top }
  }
}

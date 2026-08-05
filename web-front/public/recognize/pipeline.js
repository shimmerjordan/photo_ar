/**
 * 检测 / 跟踪状态机。**这是路线 A 的核心，也是与 Android 那条第二贴合路最大的差别。**
 *
 * ## 为什么必须分两档
 *
 * 实测（桌面 headless Chrome，单线程 wasm，`test/golden/`）：
 *
 * | | 成本 | 折算 |
 * |---|---|---|
 * | 提特征（4000 点 @1280） | 56ms | |
 * | 单候选配对（4000×300 Hamming crossCheck） | 45.6ms | |
 * | 单候选 RANSAC | 9.7ms | |
 * | **全库检测（Top-20）** | ≈ 56 + 20×55 = **1.2s** | 手机 2~5s |
 * | **光流跟踪 83 个点 + 重解单应** | **9.5ms** | 105 FPS，手机 25~50 FPS |
 *
 * 也就是说「每帧重跑识别」在浏览器里是 9 FPS（桌面），根本贴不住。Android 那边靠服务端
 * 每 400ms 一发来当跟踪，真机实测往返 1~2.5 秒，四个角一到手就过期 —— `ScreenQuad.TTL_MS`
 * 被迫从 1.2s 一路放到 4s。把那套原样搬过来只是把"网络慢"换成"CPU 慢"。
 *
 * 所以：**命中那一帧把内点留下当种子，之后每帧只做光流 + 重解单应。** 跟踪帧不提特征、
 * 不做 4000×300 的配对。这一档实测比全套精排快 10.9 倍。
 *
 * ## 状态与迁移
 *
 * ```
 *   SCANNING ──检测命中──→ LOCKED ──跟踪成功──→ LOCKED（每帧，9.5ms）
 *      ↑                      │
 *      └──连续跟踪失败 N 次────┘
 * ```
 *
 * 迁回 SCANNING 的判据是**连续**失败，不是单次：手指遮住一半、镜头晃出去一帧都会让光流
 * 掉点，而那些是常态（Android 那边实测约 40% 的帧认不出来，原因就是手指压边缘 + 覆膜
 * 反光）。单次失败就放手会让视频每隔几秒断一次。
 */
import { OrbExtractor, opencv } from './orb.js'
import { candidateDocs } from './library.js'
import { thresholds } from './consts.js'
import { decideWith, normalizedQuad, ransacPair, verifyPair } from './verify.js'

export const SCANNING = 'scanning'
export const LOCKED = 'locked'

/**
 * 只把主线程用得上的那几个字段挑出来。
 *
 * `lib.photos[i]` 上挂着 300 个关键点（2.4KB）和描述子（9.6KB）—— 那是识别用的，主线程
 * 一个字节都不需要。而这个对象要**每帧**跨 Worker 边界回去（跟踪档 30fps），带着它就是
 * 每秒 360KB 的结构化克隆，纯浪费。第一版就是这么写的，症状是测试输出 110KB。
 */
function photoMeta(p) {
  return p && { id: p.id, title: p.title, aspect: p.aspect, mediaUrl: p.mediaUrl, thumbUrl: p.thumbUrl }
}

/**
 * 连续多少次跟踪失败才放手回到检测。
 *
 * 5 帧 @ 30fps ≈ 170ms。取这个量级是因为：单帧掉点是常态（遮挡、反光、运动模糊），
 * 而"照片真的被拿走了"会连续失败。太小 → 视频反复中断；太大 → 照片移开后视频还贴在
 * 空气上，且要等到窗口耗尽才重新检测（那期间用户对准新照片是没反应的）。
 */
export const MAX_TRACK_MISSES = 5

/**
 * 光流窗口与金字塔层数。
 *
 * 21×21 / 3 层是 OpenCV 教程的默认组合，实测在 1280 长边上保住 83/83 个种子点。
 * **别为了省时间调小**：窗口小了在运动模糊上先掉点，而掉点的表现是单应矩阵越解越飘 ——
 * 视频慢慢滑走，看起来像"跟踪不准"而不是"跟丢了"。
 */
const FLOW_WIN = 21
const FLOW_LEVELS = 3

/** 重解单应至少要剩多少个点。低于它这一帧就算跟丢 —— 4 点是数学下限，太接近下限的解不可信。 */
const MIN_TRACK_POINTS = 12

/**
 * 补种子：光流的点掉到「初始的这个比例」以下就对锁定那张重新匹配一次。
 *
 * ## 为什么必须有这一步
 *
 * 光流**只会丢点，不会长点**：每帧有几个点因为遮挡、运动模糊、走出画面而失配，而
 * `_track` 用存活的点替换种子 —— 于是种子数单调递减，几十帧后必然耗尽。
 *
 * 真机日志（用户提供）把这条量得很清楚，1 秒内：
 *   `27 → 24 → 24 → 20 → 17 → 15 → 13 → quad_implausible → homography_lost(1) → 放手`
 *
 * 注意起点只有 **27** 个内点，而合成 fixture 上是 83 —— 真实场景（手持、覆膜反光、
 * 打印质量）的匹配质量低得多，所以经不起任何衰减。**补种子不是优化，是这条路能不能
 * 站住的前提。**
 *
 * 取 0.6 而不是更低：等到快没了再补，那时候单应矩阵已经在飘（用户日志里
 * `quad_implausible` 就是点太少导致几何退化），补回来也补不回那几帧的观感。
 */
const RESEED_RATIO = 0.6

/**
 * 补种子的绝对下限。种子本来就少（比如 20 个）时，比例判据会要求掉到 12 才补，
 * 而那已经贴在 `MIN_TRACK_POINTS` 上了。所以两个判据取较大者。
 */
const RESEED_FLOOR = 20

/**
 * 两次补种子之间至少隔多久。
 *
 * 补种子要提一次特征 + 一次单候选匹配（桌面实测约 100ms，手机按 2~4 倍外推）。
 * 没有冷却的话，一旦场景本身就只能给出十几个点，它会每帧都触发 —— 跟踪帧从 16ms
 * 变成 300ms，比丢跟踪更难看。
 */
const RESEED_COOLDOWN_MS = 400

/**
 * 补种子时的内点门槛。**比 `minInliers` 低是刻意的。**
 *
 * 这一步不问"是哪张照片"——那件事在检测阶段已经做过了，而且 `decideWith` 的 ratio
 * 检验也已经把它和别的候选分开过。这里只问"这张照片的几何还对得上吗"，所以不需要
 * 区分度，只需要够解一个可信的单应矩阵。
 *
 * Android 那边有一个同样用途的第三个下限（`verify.REFRESH_MIN_INLIERS`），但它的具体
 * 数值不在现有源码里，所以这里取 `MIN_TRACK_POINTS + 4 = 16` 并说明理由：它必须高于
 * 跟踪的存活下限（否则补完立刻又判丢），又要明显低于识别门槛 40。
 */
const RESEED_MIN_INLIERS = MIN_TRACK_POINTS + 4

export class Pipeline {
  /**
   * @param lib `library.unpack()` 的产物
   * @param opts.queryLongEdge 处理长边（默认 consts 的 1280）
   * @param opts.nFeatures 特征预算（默认 4000）
   */
  constructor(lib, opts = {}) {
    this.lib = lib
    this.cv = opencv()
    this.extractor = new OrbExtractor(opts)
    this.state = SCANNING
    this.locked = null      // {photoId, doc, quad, inliers}
    this.misses = 0
    this.stats = { detects: 0, tracks: 0, lastDetectMs: 0, lastTrackMs: 0 }

    // 跟踪状态。gray 是**上一帧**的灰度图，光流要用它。
    this._prevGray = new this.cv.Mat()
    this._curGray = new this.cv.Mat()
    this._small = new this.cv.Mat()
    this._prevPts = null    // cv.Mat CV_32FC2，跟踪种子在查询空间的坐标
    this._refPts = null     // Float32Array，与种子一一对应的参考侧坐标
    this._querySize = null  // [w, h] 查询侧特征空间，四角换算要用
    this._refSize = null
    this._seedCount = 0     // 命中那一帧拿到多少个种子。补种子的判据是它的比例
    this._lastReseedAt = 0
  }

  /**
   * 喂一帧。
   *
   * @param imageData 相机帧的 `ImageData`（RGBA）
   * @returns `{state, quad, photoId, inliers, reason, ms}`。`quad` 为 null 表示这一帧
   *   没有可用的贴合几何（调用方应当继续用上一份，直到 TTL 过期）。
   */
  push(imageData) {
    const t0 = performance.now()
    const out = this.state === LOCKED ? this._track(imageData) : this._detect(imageData)
    out.ms = Math.round(performance.now() - t0)
    out.state = this.state
    return out
  }

  /** 强制回到检测。用户点"重新扫描"、或者上层判断该放手时调。 */
  reset() {
    this.state = SCANNING
    this.locked = null
    this.misses = 0
    this._prevPts?.delete()
    this._prevPts = null
    this._refPts = null
  }

  _detect(imageData) {
    const cv = this.cv
    this.stats.detects++
    const src = this._matFrom(imageData)
    try {
      const query = this.extractor.extract(src)
      if (query.count === 0) return { quad: null, reason: 'no_features' }

      // 查询侧特征空间的尺寸。**不是**相机帧的尺寸 —— 四角要按真正提特征的那张图换算，
      // 而 OrbExtractor 内部把长边缩到了 queryLongEdge。弄混就差一倍（§35.3 点名的
      // 三处之一）。
      this._querySize = this._queryDims(imageData.width, imageData.height)
      this._refSize = [this.lib.refLongEdge, 0] // 高度按命中那张照片的比例算，见下面

      const docs = candidateDocs(this.lib, query.desc, query.count, thresholds.topK)
      const results = []
      for (const doc of docs) {
        const ref = this.lib.photos[doc]
        const r = verifyPair(query, ref, ref.id)
        r.doc = doc
        results.push(r)
      }
      const decision = decideWith(results, thresholds)
      if (!decision.matched) {
        return { quad: null, reason: decision.reason, inliers: decision.inliers }
      }

      const top = decision.top
      const photo = this.lib.photos[top.doc]
      // 参考侧高度：从照片的宽高比反推。库里存的 pts 在 640 长边的空间里，而那张图
      // 的短边是多少只有 aspect 知道。aspect 缺失时退回 3:2（最常见的相纸比例）——
      // 它只影响四角的形状，不影响是否命中。
      const aspect = Number.isFinite(photo.aspect) && photo.aspect > 0 ? photo.aspect : 1.5
      this._refSize = aspect >= 1
        ? [this.lib.refLongEdge, Math.round(this.lib.refLongEdge / aspect)]
        : [Math.round(this.lib.refLongEdge * aspect), this.lib.refLongEdge]

      const quad = normalizedQuad(top.h, this._refSize, this._querySize)
      this._seedTracking(src, query, top)
      this.state = LOCKED
      this.misses = 0
      const meta = photoMeta(photo)
      this.locked = { photoId: photo.id, doc: top.doc, inliers: top.inliers, photo: meta, quad }
      return { quad, photoId: photo.id, inliers: top.inliers, reason: 'ok', photo: meta, fresh: true }
    } finally {
      src.delete()
    }
  }

  /**
   * 把命中那一帧的内点存成跟踪种子。
   *
   * 只取**内点**而不是全部匹配：外点本来就是错的配对，拿它们去跟踪等于给下一帧的 RANSAC
   * 喂噪声。而 RANSAC 在外点占比高时会拟合出"数值正常但几何错"的矩阵 —— 表现是视频突然
   * 跳到画面另一处，比不贴更难解释。
   */
  _seedTracking(src, query, top) {
    const cv = this.cv
    this._toGray(src)
    this._curGray.copyTo(this._prevGray)

    // 重跑一次配对拿 mask 太贵，所以这里用 top.h 自己筛：把 query 点投到 ref 空间，
    // 重投影误差在 RANSAC 阈值内的就是内点。判据与 findHomography 内部一致
    // （`RANSAC_REPROJ`），所以筛出来的集合与它的 mask 只差数值末位。
    const h = top.h
    const src2 = []
    const dst2 = []
    const m = top.matches
    if (!m) {
      // verifyPair 没把配对带出来时退化成"用全部配对"，宁可多几个外点也别丢掉跟踪能力。
      this._prevPts?.delete()
      this._prevPts = null
      this._refPts = null
      return
    }
    const thr2 = 3.0 * 3.0
    for (let i = 0; i < m.count; i++) {
      const x = m.src[i * 2], y = m.src[i * 2 + 1]
      const X = h[0] * x + h[1] * y + h[2]
      const Y = h[3] * x + h[4] * y + h[5]
      const W = h[6] * x + h[7] * y + h[8]
      if (!Number.isFinite(W) || Math.abs(W) < 1e-12) continue
      const dx = X / W - m.dst[i * 2]
      const dy = Y / W - m.dst[i * 2 + 1]
      if (dx * dx + dy * dy <= thr2) {
        src2.push(x, y)
        dst2.push(m.dst[i * 2], m.dst[i * 2 + 1])
      }
    }
    this._prevPts?.delete()
    const n = src2.length / 2
    if (n < MIN_TRACK_POINTS) {
      this._prevPts = null
      this._refPts = null
      return
    }
    const pm = new cv.Mat(n, 1, cv.CV_32FC2)
    pm.data32F.set(src2)
    this._prevPts = pm
    this._refPts = new Float32Array(dst2)
    this._seedCount = n
  }

  _track(imageData) {
    const cv = this.cv
    this.stats.tracks++
    if (!this._prevPts || !this._refPts) {
      this.state = SCANNING
      return { quad: null, reason: 'no_seed' }
    }
    const src = this._matFrom(imageData)
    const nextPts = new cv.Mat()
    const status = new cv.Mat()
    const err = new cv.Mat()
    try {
      this._toGray(src)
      cv.calcOpticalFlowPyrLK(this._prevGray, this._curGray, this._prevPts, nextPts, status, err,
        new cv.Size(FLOW_WIN, FLOW_WIN), FLOW_LEVELS)

      // 跟丢的点必须剔掉。留着它们等于给 RANSAC 喂随机坐标。
      const n = this._prevPts.rows
      const keptSrc = []
      const keptRef = []
      for (let i = 0; i < n; i++) {
        if (!status.data[i]) continue
        keptSrc.push(nextPts.data32F[i * 2], nextPts.data32F[i * 2 + 1])
        keptRef.push(this._refPts[i * 2], this._refPts[i * 2 + 1])
      }
      const kept = keptSrc.length / 2
      if (kept < MIN_TRACK_POINTS) return this._miss('flow_lost', kept)

      // 重解单应。门槛用 4（数学下限）而不是 minInliers：这一步不是"认出是哪张"，
      // 那件事已经在检测阶段做过了。这里只问"这些点还在不在同一个平面上"。
      const r = ransacPair(new Float32Array(keptSrc), new Float32Array(keptRef), 4)
      if (!r.h || r.inliers < MIN_TRACK_POINTS) return this._miss('homography_lost', r.inliers)

      const quad = normalizedQuad(r.h, this._refSize, this._querySize)
      if (!quad) return this._miss('quad_implausible', r.inliers)

      // 跟踪成功：把这一帧变成下一帧的"上一帧"，并且**用光流的结果替换种子**。
      // 不替换的话种子永远是命中那一帧的坐标，几帧之后光流的搜索窗口就跟不上了。
      this._curGray.copyTo(this._prevGray)
      this._prevPts.delete()
      const pm = new cv.Mat(kept, 1, cv.CV_32FC2)
      pm.data32F.set(keptSrc)
      this._prevPts = pm
      this._refPts = new Float32Array(keptRef)

      this.misses = 0
      this.locked.quad = quad
      this.locked.inliers = r.inliers

      // 点数掉到阈值以下就补种子。**在跟踪成功的这一帧做**，而不是等它失败 ——
      // 失败之后再补，中间那几帧的四角已经飘过了（真机日志里的 `quad_implausible`
      // 就是点太少导致的几何退化）。
      // 判据用 **RANSAC 内点数**而不是光流存活数。
      //
      // 第一版用 `kept`，结果补种子一次都没触发过 —— 而测试正好抓到了：噪声序列上
      // 内点从 81 掉到 32（触发线 49）而 `kept` 还有七十几个。原因是光流的 `status`
      // 只说"这个点跟到了"，不说"它跟对了"：噪声下它会把点跟到一个几何上对不上的位置，
      // 于是 RANSAC 把它剔成外点。**真机日志里衰减的那个数字（27→13）就是内点数**，
      // 所以判据必须是它。
      const quality = Math.min(kept, r.inliers)
      const floor = Math.max(RESEED_FLOOR, Math.round(this._seedCount * RESEED_RATIO))
      let reseeded = 0
      if (quality < floor && performance.now() - this._lastReseedAt > RESEED_COOLDOWN_MS) {
        reseeded = this._reseed(src)
      }
      return {
        quad, photoId: this.locked.photoId, inliers: r.inliers, reason: 'ok',
        photo: this.locked.photo, tracked: kept,
        ...(reseeded ? { reseeded } : {}),
      }
    } finally {
      src.delete(); nextPts.delete(); status.delete(); err.delete()
    }
  }

  /**
   * 对**已经锁定的那一张**重新匹配一次，用新内点补种子。
   *
   * 只跟一个候选比（不是 Top-20），所以成本是全库检测的 1/6 左右。门槛用
   * `RESEED_MIN_INLIERS` 而不是 `minInliers`，理由写在那个常量上。
   *
   * @returns 补到多少个种子；0 表示这次没补上（不算失败，跟踪照旧继续用旧种子）。
   */
  _reseed(src) {
    this._lastReseedAt = performance.now()
    const photo = this.lib.photos[this.locked.doc]
    if (!photo) return 0
    const query = this.extractor.extract(src)
    if (query.count === 0) return 0
    const r = verifyPair(query, photo, photo.id, RESEED_MIN_INLIERS)
    if (!r.h || r.inliers < RESEED_MIN_INLIERS || !r.matches) return 0
    // 复用命中那一帧的同一段筛选逻辑：只留重投影误差在 RANSAC 阈值内的点。
    this._seedTracking(src, query, r)
    return this._seedCount
  }

  _miss(reason, inliers) {
    this.misses++
    if (this.misses >= MAX_TRACK_MISSES) {
      const photoId = this.locked?.photoId
      this.reset()
      return { quad: null, reason, inliers, gaveUp: true, photoId }
    }
    // 还没到放手的次数：这一帧没有新几何，但**不撤掉锁定** —— 上层继续用上一份四角，
    // 直到 TTL 过期。这正是 `screenquad.TTL_MS` 那个窗口要盖住的东西。
    return { quad: null, reason, inliers, photoId: this.locked?.photoId }
  }

  _matFrom(imageData) {
    const cv = this.cv
    const m = new cv.Mat(imageData.height, imageData.width, cv.CV_8UC4)
    m.data.set(imageData.data)
    return m
  }

  /** 把当前帧缩到查询空间并转灰度，结果放 `_curGray`。跟踪与检测共用同一个坐标系。 */
  _toGray(src) {
    const cv = this.cv
    const [w, h] = this._querySize ?? this._queryDims(src.cols, src.rows)
    if (src.cols !== w || src.rows !== h) {
      cv.resize(src, this._small, new cv.Size(w, h), 0, 0,
        w < src.cols ? cv.INTER_AREA : cv.INTER_LINEAR)
      cv.cvtColor(this._small, this._curGray, cv.COLOR_RGBA2GRAY)
    } else {
      cv.cvtColor(src, this._curGray, cv.COLOR_RGBA2GRAY)
    }
  }

  /** 相机帧尺寸 → 查询侧特征空间尺寸。与 `pyparity.resizedSize` 同一条公式。 */
  _queryDims(w, h) {
    const longEdge = this.extractor.longEdge
    const longest = Math.max(w, h)
    if (longest === longEdge) return [w, h]
    const s = longEdge / longest
    const pr = (x) => {
      const f = Math.floor(x), d = x - f
      return d > 0.5 ? f + 1 : d < 0.5 ? f : (f % 2 === 0 ? f : f + 1)
    }
    return [Math.max(1, pr(w * s)), Math.max(1, pr(h * s))]
  }

  delete() {
    this.extractor.delete()
    this._prevGray?.delete()
    this._curGray?.delete()
    this._small?.delete()
    this._prevPts?.delete()
  }
}

/**
 * 几何校验、命中判定、以及四个角。**服务端 `photoar.verify` + `photoar.quad` 的对译。**
 *
 * 三条判定缺一不可（服务端 spec §8.3，一字不改地搬过来）：
 *
 *   1. 内点数 >= minInliers
 *   2. 单应矩阵行列式落在 [detMin, detMax]
 *   3. 第一名内点数 >= ratio × 第二名内点数
 *
 * 第 3 条在**全部候选**之间比，不只在通过前两条的候选之间。理由是服务端注释里那句：
 * 若第二名 24 分（未过阈值）而第一名 26 分，二者其实无法区分，只在通过者之间比会把
 * 它当作"唯一通过者"直接放行，制造误识别。
 *
 * 行列式用**带符号**值而非绝对值：负行列式意味着镜像变换，而实体照片经相机成像永远
 * 不会镜像，因此负值必须判否。
 */
import { opencv } from './orb.js'
import {
  MIN_MATCHES_FOR_HOMOGRAPHY,
  RANSAC_CONFIDENCE,
  RANSAC_MAX_ITERS,
  RANSAC_REPROJ,
  thresholds,
} from './consts.js'

/**
 * Hamming + crossCheck 配对，产出对应的点对。
 *
 * 用 opencv.js 的 `BFMatcher` 而不是自己写 popcount 循环：服务端就是
 * `cv2.BFMatcher(NORM_HAMMING, crossCheck=True)`，同一份 C++ 代码编译出来的 wasm
 * 才谈得上"配对结果一致"。自己写的话 crossCheck 的平局处理、以及等距离时取哪一个，
 * 都会与它分叉 —— 而分叉只表现为内点数少几个。
 *
 * @returns `{count, src: Float32Array(count*2), dst: Float32Array(count*2)}`
 *   src 取自 query，dst 取自 ref —— 与服务端 `verify_pair` 里
 *   `src = query.pts[queryIdx]` / `dst = ref.pts[trainIdx]` 同向。**这个方向决定了
 *   单应矩阵是 query → ref**，而 `normalizedQuad` 依赖它，反了会得到一个数值正常
 *   但几何完全错的四边形。
 */
export function matchHamming(query, ref) {
  const cv = opencv()
  if (query.count < MIN_MATCHES_FOR_HOMOGRAPHY || ref.count < MIN_MATCHES_FOR_HOMOGRAPHY) {
    return { count: 0, src: new Float32Array(0), dst: new Float32Array(0) }
  }
  const cols = query.descCols ?? 32
  const qMat = new cv.Mat(query.count, cols, cv.CV_8U)
  qMat.data.set(query.desc)
  const rMat = new cv.Mat(ref.count, cols, cv.CV_8U)
  rMat.data.set(ref.desc)

  const bf = new cv.BFMatcher(cv.NORM_HAMMING, true)
  const dm = new cv.DMatchVector()
  bf.match(qMat, rMat, dm)

  const n = dm.size()
  const src = new Float32Array(n * 2)
  const dst = new Float32Array(n * 2)
  for (let i = 0; i < n; i++) {
    const m = dm.get(i)
    src[i * 2] = query.pts[m.queryIdx * 2]
    src[i * 2 + 1] = query.pts[m.queryIdx * 2 + 1]
    dst[i * 2] = ref.pts[m.trainIdx * 2]
    dst[i * 2 + 1] = ref.pts[m.trainIdx * 2 + 1]
  }
  dm.delete(); bf.delete(); qMat.delete(); rMat.delete()
  return { count: n, src, dst }
}

/** 3×3 行列式，行优先。 */
function det3(h) {
  return (
    h[0] * (h[4] * h[8] - h[5] * h[7]) -
    h[1] * (h[3] * h[8] - h[5] * h[6]) +
    h[2] * (h[3] * h[7] - h[4] * h[6])
  )
}

/**
 * 已配好的点对 → 单应矩阵 → 内点数与行列式。`verify.ransac_pair` 的对译。
 *
 * `confidence` **必须显式传**：opencv.js 的 `findHomography` 要给 `maxIters` 就得先给它，
 * 而服务端那行没写（用 Python 默认 0.995）。见 `consts.RANSAC_CONFIDENCE`。
 *
 * @returns `{inliers, det, ok, h: Float64Array(9) | null}`，h 的方向是 **query → ref**。
 */
export function ransacPair(src, dst, minInliers = thresholds.minInliers) {
  const cv = opencv()
  const n = src.length / 2
  const fail = { inliers: 0, det: 0, ok: false, h: null }
  if (n < MIN_MATCHES_FOR_HOMOGRAPHY) return fail

  const sm = new cv.Mat(n, 1, cv.CV_32FC2)
  sm.data32F.set(src)
  const dm = new cv.Mat(n, 1, cv.CV_32FC2)
  dm.data32F.set(dst)
  const mask = new cv.Mat()
  let H = null
  try {
    H = cv.findHomography(sm, dm, cv.RANSAC, RANSAC_REPROJ, mask, RANSAC_MAX_ITERS, RANSAC_CONFIDENCE)
    if (!H || H.empty() || mask.empty()) return fail

    let inliers = 0
    const md = mask.data
    for (let i = 0; i < md.length; i++) if (md[i]) inliers++

    const h = new Float64Array(9)
    h.set(H.data64F.subarray(0, 9))
    const d = det3(h)
    const ok = inliers >= minInliers && d >= thresholds.detMin && d <= thresholds.detMax
    return { inliers, det: d, ok, h }
  } finally {
    sm.delete(); dm.delete(); mask.delete(); H?.delete()
  }
}

/**
 * 一次候选精排：配对 + RANSAC。`verify.verify_pair` 的对译。
 *
 * 比服务端多带一个 `matches` 字段（配对好的点对）。服务端不需要它 —— 它算完 inliers
 * 就把点对扔了。而这里**必须留着**：命中之后的跟踪要用内点当光流种子
 * （见 `pipeline._seedTracking`），而重新跑一遍配对是 45.6ms（实测），恰好是最贵的
 * 那一步。留下来是零成本的，重算不是。
 */
export function verifyPair(query, ref, photoId, minInliers = thresholds.minInliers) {
  const m = matchHamming(query, ref)
  if (m.count < MIN_MATCHES_FOR_HOMOGRAPHY) {
    return { photoId, inliers: 0, det: 0, ok: false, h: null, matchCount: m.count, matches: null }
  }
  return { photoId, ...ransacPair(m.src, m.dst, minInliers), matchCount: m.count, matches: m }
}

/**
 * 三条判定。`verify.decide_with` 的对译。
 *
 * @returns `{matched, photoId, inliers, reason}`，reason ∈ 'ok'|'empty'|'weak'|'ambiguous'|'forbidden'
 */
export function decideWith(results, t = thresholds) {
  if (!results.length) return { matched: false, photoId: null, inliers: 0, reason: 'empty' }
  const ranked = [...results].sort((a, b) => b.inliers - a.inliers)
  const top1 = ranked[0]
  // 这里重新算前两条判定，不复用 result.ok：ok 是按**默认**阈值算的，而热配置可能改过。
  if (!(top1.inliers >= t.minInliers && top1.det >= t.detMin && top1.det <= t.detMax)) {
    return { matched: false, photoId: null, inliers: top1.inliers, reason: 'weak' }
  }
  const runnerUp = ranked.length > 1 ? ranked[1].inliers : 0
  if (top1.inliers < t.ratio * runnerUp) {
    return { matched: false, photoId: null, inliers: top1.inliers, reason: 'ambiguous', runnerUp }
  }
  return { matched: true, photoId: top1.photoId, inliers: top1.inliers, reason: 'ok', top: top1 }
}

// ─────────────────────────────────────────────────────────────────────────────
// 四个角：`photoar.quad` 的对译
// ─────────────────────────────────────────────────────────────────────────────

/** 归一化坐标的容许范围。0..1 是画面本身；放到 -4..5 是给「照片比画面大得多、只有
 *  中间一块在画面里」留的余量。再远就不是取景问题，而是矩阵已经退化了。 */
export const COORD_MIN = -4.0
export const COORD_MAX = 5.0
/** 面积占画面的比例区间。下限防退化成一条线（视频会画成一道亮线，比不画难看得多）；
 *  上限只防数值爆炸，故意很宽 —— 手机贴到照片上时四边形确实能比画面大好几倍。 */
export const MIN_AREA_FRAC = 0.002
export const MAX_AREA_FRAC = 60.0
/** 齐次分量 w 的下限。四个角的 w 必须同号且不接近 0，理由见 `plausibleQuad`。 */
export const MIN_ABS_W = 1e-6

/**
 * 四个角（归一化后）像不像一次真实的取景。
 *
 * 只做**能把垃圾挡住**的那几条，不做"看起来更合理"的收紧：这个四边形是贴合的唯一依据，
 * 判严了就是「明明认出来了却不贴」。
 *
 * 刻意**没有**凸性检查：w 是 (x,y) 的仿射函数，四个角的 w 同号就意味着整个凸包上的 w
 * 同号，也就是不跨越无穷远线 —— 而不跨越无穷远线的投影变换必然保凸。留一段永远为真的
 * 检查比不写更糟，它会让人以为凸性是被独立守着的。
 *
 * 也**没有**镜像检查：镜像的行列式为负，而 `detMin` 是正数，镜像走不到这里。
 */
export function plausibleQuad(pts) {
  if (!pts || pts.length !== 8) return false
  for (const v of pts) {
    if (!Number.isFinite(v) || v < COORD_MIN || v > COORD_MAX) return false
  }
  // 鞋带公式。上面那段说明了它不自交，所以这个面积就是真面积。
  let area = 0
  for (let i = 0; i < 4; i++) {
    const j = (i + 1) % 4
    area += pts[i * 2] * pts[j * 2 + 1] - pts[j * 2] * pts[i * 2 + 1]
  }
  area = Math.abs(area) / 2
  return area >= MIN_AREA_FRAC && area <= MAX_AREA_FRAC
}

/**
 * 3×3 逆矩阵（伴随矩阵法）。
 *
 * 服务端用 `np.linalg.inv`（LU 分解）。两者在数值上有差异，但量级在 1e-12 相对误差，
 * 而四个角随后要除以 1280 量级的尺寸再 round 到 6 位小数 —— 差异被吃掉好几个数量级。
 * 用伴随法是为了不必为一次求逆建两个 Mat（这条路径每帧都走）。
 */
function inv3(h) {
  const d = det3(h)
  if (!Number.isFinite(d) || Math.abs(d) < 1e-300) return null
  const a = h
  const out = new Float64Array(9)
  out[0] = (a[4] * a[8] - a[5] * a[7]) / d
  out[1] = (a[2] * a[7] - a[1] * a[8]) / d
  out[2] = (a[1] * a[5] - a[2] * a[4]) / d
  out[3] = (a[5] * a[6] - a[3] * a[8]) / d
  out[4] = (a[0] * a[8] - a[2] * a[6]) / d
  out[5] = (a[2] * a[3] - a[0] * a[5]) / d
  out[6] = (a[3] * a[7] - a[4] * a[6]) / d
  out[7] = (a[1] * a[6] - a[0] * a[7]) / d
  out[8] = (a[0] * a[4] - a[1] * a[3]) / d
  for (const v of out) if (!Number.isFinite(v)) return null
  return out
}

/**
 * 照片在查询帧里的四个角，归一化到 0..1。`quad.normalized_quad` 的对译。
 *
 * ## 坐标系（这是最容易出错的地方）
 *
 * 单应矩阵两侧各在一个**特征空间**里，而两侧的长边**不一样**：查询侧 1280、参考侧 640。
 * 弄混就差一倍 —— 视频只盖住照片左上角的四分之一，而看起来就像"贴合不准"。
 *
 * 出参归一化是刻意的：相机帧在到这里之前被缩过（`orb.js` 缩到 1280），而渲染要用的是
 * **显示尺寸**。给像素就得同时约定"哪一层的像素"；给比例则任何一层都对得上。
 *
 * 出参允许落在 0..1 之外：照片凑得很近时会有角在画面外，那是**正常**的，视频照样要画。
 *
 * @param h `ransacPair` 给出的矩阵，方向 **query → ref**
 * @param refSize `[宽, 高]` 参考侧特征空间的像素
 * @param querySize `[宽, 高]` 查询侧特征空间的像素
 * @returns `[x0,y0, x1,y1, x2,y2, x3,y3]`，参考图的左上→右上→右下→左下；不合格时 null
 */
export function normalizedQuad(h, refSize, querySize) {
  if (!h || h.length !== 9) return null
  const [rw, rh] = refSize
  const [qw, qh] = querySize
  if (!(rw > 0 && rh > 0 && qw > 0 && qh > 0)) return null
  for (const v of h) if (!Number.isFinite(v)) return null

  const inv = inv3(h)
  if (!inv) return null

  const corners = [[0, 0], [rw, 0], [rw, rh], [0, rh]]
  const pts = new Float64Array(8)
  const ws = new Float64Array(4)
  for (let i = 0; i < 4; i++) {
    const [cx, cy] = corners[i]
    const X = inv[0] * cx + inv[1] * cy + inv[2]
    const Y = inv[3] * cx + inv[4] * cy + inv[5]
    const W = inv[6] * cx + inv[7] * cy + inv[8]
    if (!Number.isFinite(X) || !Number.isFinite(Y) || !Number.isFinite(W)) return null
    ws[i] = W
    pts[i * 2] = X
    pts[i * 2 + 1] = Y
  }
  // 同号且不接近 0。异号意味着这个四边形被无穷远线切开了（一半在相机前、一半在相机后），
  // 此时四个角连出来的形状和真实投影毫无关系 —— 而它在数值上完全正常，不查这一条就会
  // 偶发地画出一块翻转的视频，且没有任何报错。
  let pos = 0
  for (const w of ws) {
    if (Math.abs(w) < MIN_ABS_W) return null
    if (w > 0) pos++
  }
  if (pos !== 0 && pos !== 4) return null

  const out = new Array(8)
  for (let i = 0; i < 4; i++) {
    out[i * 2] = pts[i * 2] / ws[i] / qw
    out[i * 2 + 1] = pts[i * 2 + 1] / ws[i] / qh
  }
  if (!plausibleQuad(out)) return null
  // round 到 6 位：与服务端一致，且这个数下游要拿去算贴图矩形，完整十进制展开只会让
  // 日志难读。
  return out.map((v) => Math.round(v * 1e6) / 1e6)
}

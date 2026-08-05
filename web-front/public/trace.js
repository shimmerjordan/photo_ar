/**
 * 数值化打桩：把跟踪与贴合每一帧的状态记成**数字**，供离线分析。
 *
 * ## 为什么不用现成的诊断日志
 *
 * `diag.js` 那块是给人看的：中文、折叠、按关键度分区。它回答"刚才发生了什么"，
 * 但回答不了"四角抖了多少像素"、"贴合比真实姿态晚多少毫秒"、"一分钟里丢锁几次"——
 * 那些要的是可以求方差、求分位数的数组。把它们从中文日志里正则抠出来是自找麻烦。
 *
 * 所以这里是一个**定长环形缓冲 + 纯数字**的记录器，`window.__trace` 暴露给 CDP，
 * 一条 `Runtime.evaluate` 就能把整段轨迹拉到分析脚本里。
 *
 * ## 为什么默认就开着
 *
 * 每帧几次数组写入，代价小到量不出来（记录一帧是 ~14 个 float 的存入，
 * 而同一帧里我们刚做完一次 4.9MB 的 `getImageData`）。而它要解决的问题是
 * **只在真机上、只在真实光照和手抖下才出现**的那一类 —— 那种问题里，"回去把打桩
 * 打开再复现一次"往往就是复现不了。所以宁可一直记着。
 *
 * 环大小按 60 秒 @ 60fps 定（3600 帧）。再长没有意义：分析要的是一段稳定的窗口，
 * 而不是全部历史。
 */

/** 一条记录多少个字段。改它必须同时改 `FIELDS` 与 `push*`。 */
const STRIDE = 17

/**
 * 字段名，顺序与 `STRIDE` 里的下标一一对应。
 *
 * 用平坦的 Float64Array 而不是对象数组：3600 个小对象会给 GC 添活，而 GC 停顿正好
 * 会污染我们要测的那个量（帧间隔）。
 */
export const FIELDS = [
  't',          // performance.now()
  'kind',       // 0=render 1=worker结果
  'fps',        // 当前 fps（render 帧才有）
  'quadAge',    // 这一帧用的四角有多老（ms）；-1 = 没有四角
  'drew',       // 1=真画了视频面片 0=没画
  'dt',         // 与上一 render 帧的间隔（ms）
  // 平滑后的四角，前两个角就够看抖动（四个角高度相关，存两个省一半空间）
  'sx0', 'sy0', 'sx1', 'sy1',
  // 原始（未平滑）四角的同两个角 —— 与上面配对就能分出"传感器噪声"和"平滑残留"
  'rx0', 'ry0',
  // worker 结果专用
  'state',      // 0=scanning 1=locked
  'ms',         // 这一次检测/跟踪耗时
  'inliers',    // 内点数 / 光流存活点数
  'flags',      // 位：1=fresh 2=gaveUp 4=reseeded 8=有四角
  // render 帧专用：这一帧抓帧花了多久（0 = 这一帧没抓）。
  // 它是"渲染卡顿"的主要嫌疑 —— 1280×960 的 getImageData 是 4.9MB 的
  // YUV→RGBA 软转 + 拷贝，而它在主线程上、每 ~51ms 一次。
  'grabMs',
]

const CAP = 3600
const buf = new Float64Array(CAP * STRIDE)
let head = 0
let count = 0
let enabled = true

/** 事件计数器。比逐帧记录更适合"一分钟里丢了几次锁"这种问题。 */
const counters = Object.create(null)
export function bump(name, by = 1) {
  counters[name] = (counters[name] ?? 0) + by
}

function slot() {
  const at = (head % CAP) * STRIDE
  head++
  if (count < CAP) count++
  return at
}

/** 记一个 render 帧。`smooth`/`raw` 是长度 8 的四角，可为 null。 */
export function traceRender(t, { fps = 0, quadAge = -1, drew = 0, dt = 0, smooth = null, raw = null, grabMs = 0 } = {}) {
  if (!enabled) return
  const i = slot()
  buf[i] = t
  buf[i + 1] = 0
  buf[i + 2] = fps
  buf[i + 3] = quadAge
  buf[i + 4] = drew
  buf[i + 5] = dt
  buf[i + 6] = smooth ? smooth[0] : NaN
  buf[i + 7] = smooth ? smooth[1] : NaN
  buf[i + 8] = smooth ? smooth[2] : NaN
  buf[i + 9] = smooth ? smooth[3] : NaN
  buf[i + 10] = raw ? raw[0] : NaN
  buf[i + 11] = raw ? raw[1] : NaN
  buf[i + 12] = NaN
  buf[i + 13] = NaN
  buf[i + 14] = NaN
  buf[i + 15] = 0
  buf[i + 16] = grabMs
}

/** 记一条 worker 结果。 */
export function traceResult(t, m) {
  if (!enabled) return
  const i = slot()
  buf[i] = t
  buf[i + 1] = 1
  for (let k = 2; k <= 11; k++) buf[i + k] = NaN
  buf[i + 12] = m.state === 'locked' ? 1 : 0
  buf[i + 13] = m.ms ?? NaN
  buf[i + 14] = m.inliers ?? NaN
  buf[i + 15] = (m.fresh ? 1 : 0) | (m.gaveUp ? 2 : 0) | (m.reseeded ? 4 : 0) | (m.quad ? 8 : 0)
  buf[i + 16] = NaN
  if (m.gaveUp) bump('gaveUp')
  if (m.reseeded) bump('reseeded')
  if (m.fresh) bump('fresh')
  bump(m.state === 'locked' ? 'trackResults' : 'detectResults')
  if (m.reason) bump(`reason:${m.reason}`)
}

/**
 * 把环里的记录按时间顺序倒出来。**返回普通数组** —— 它要过一次
 * `Runtime.evaluate` 的 JSON 序列化，而 TypedArray 序列化出来是 `{0:…,1:…}`。
 */
export function dump() {
  const n = count
  const start = count < CAP ? 0 : head % CAP
  const rows = new Array(n)
  for (let k = 0; k < n; k++) {
    const i = ((start + k) % CAP) * STRIDE
    const row = new Array(STRIDE)
    for (let f = 0; f < STRIDE; f++) {
      const v = buf[i + f]
      row[f] = Number.isNaN(v) ? null : v
    }
    rows[k] = row
  }
  return { fields: FIELDS, rows, counters: { ...counters }, at: performance.now() }
}

export function reset() {
  head = 0
  count = 0
  for (const k of Object.keys(counters)) delete counters[k]
}

export function setEnabled(on) {
  enabled = Boolean(on)
}

// 挂到 window 上给 CDP 用。**只挂读的接口** —— 这不是一个可以从控制台改行为的开关。
if (typeof window !== 'undefined') {
  window.__trace = { dump, reset, fields: FIELDS, setEnabled }
}

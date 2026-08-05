/**
 * 与服务端 Python 逐位对齐的那几个小函数。**唯一那一份。**
 *
 * 这个文件存在的理由和 `photoar.xfeat.canvas_size` 的注释是同一条：一个换算公式在两处
 * 各写一遍，不一致不会报错，只会让识别率静默下降。所以运行时和 `test/golden/` 共用
 * 这一份 —— golden 验的就是它。
 */

/**
 * Python 内置 `round()` 的语义：**round-half-to-even**（银行家舍入）。
 *
 * `Math.round` 是 half-up，两者只在 `.5` 上分道扬镳。看起来是个末位问题，实际后果是
 * 整条识别静默失效：`features.resize_to_long_edge` 用 `int(round(w * scale))` 决定缩放后
 * 的图有多大，差一个像素就是**另一张图** —— 每个 ORB 关键点的位置都会动，描述子全部
 * 不可比。而它只在 scale 恰好把某一边推到 .5 上时发生，也就是绝大多数分辨率下两边看起来
 * 完全一致，偏偏在某几个相机档位上归零。
 *
 * 只处理有限正数：这里所有调用点都是像素尺寸。负数与 NaN 直接交给调用方，不在这里
 * 静默兜底 —— 兜底会把「上游算出了 NaN」这件事藏起来。
 */
export function pyRound(x) {
  const f = Math.floor(x)
  const d = x - f
  if (d > 0.5) return f + 1
  if (d < 0.5) return f
  return f % 2 === 0 ? f : f + 1
}

/**
 * `photoar.features.resize_to_long_edge` 的对译：把长边缩（或放）到 `longEdge`。
 *
 * @returns `{h, w, scale}`，或 `null` 表示长边已经等于目标、不需要 resize
 *   （与 Python 那边 `return img` 那一支对应 —— 那一支**不做任何插值**，
 *   而 `cv.resize` 到同尺寸也不完全是恒等，所以这个 null 必须被调用方尊重）。
 *
 * ⚠️ **故意不禁止放大**。帧比 `longEdge` 小的时候会被放大，而实测放大是**有收益**的：
 * 它把查询侧的尺度对回入库侧（入库时照片铺满 640，手持时照片只占画面一小块）。
 * 理由完整写在 `backend.QUERY_LONG_EDGE` 的注释里。
 */
export function resizedSize(h, w, longEdge) {
  const longest = Math.max(h, w)
  if (longest === longEdge) return null
  const scale = longEdge / longest
  return { h: Math.max(1, pyRound(h * scale)), w: Math.max(1, pyRound(w * scale)), scale }
}

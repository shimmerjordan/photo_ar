/**
 * 四个角 → 屏幕上那块透视变形的视频。**Android `ScreenQuad.kt` 的逐行对译。**
 *
 * 纯函数，不碰 DOM 也不碰 GL —— 与 Kotlin 那份不碰 Android API 是同一个理由：这层几何
 * 是唯一能自动验的部分，往下就是 GL，顶点交出去之后错了没有任何报错。
 *
 * ## 三个坐标系，别搞混
 *
 * - **归一化图像坐标**：`quad` 用的。原点左上、x 向右、y 向下、0..1 是整幅相机图。
 *   可以超出 0..1（照片比画面大）。
 * - **NDC**：GL 的裁剪空间，原点在中心、y 向上、-1..1。
 * - **照片 uv**：照片自己那个矩形，(0,0) 左上、(1,1) 右下，v 向下。与图像坐标同向。
 *
 * Android 那边图像坐标 → NDC 是交给 ARCore 的 `Frame.transformCoordinates2d`（只有
 * session 知道显示旋转与裁切）。**Web 上没有这个东西**，所以那一步在这里由
 * [imageToNdc] 自己做 —— 它要处理的是「相机图的宽高比 ≠ 画布的宽高比」时那道 cover
 * 裁切，见那个函数的说明。
 */

/**
 * 四个角多久算过期（毫秒）。
 *
 * Android 那边这个值是 4000ms，一路从 1.2s 放宽上来的，而放宽的原因**在 Web 版不存在**：
 * 那边四个角来自服务端，真机实测往返 1~2.5 秒（每帧 93~103KB 上行、与视频抢管子），
 * 于是"一到手就已经用掉大半个窗口"。
 *
 * 这里四个角是本地算的，而且锁定之后走的是光流跟踪（实测桌面 9.5ms/帧）。所以这个窗口
 * 只需要盖住「跟踪断了、正在重新检测」那段空档，取 **1 秒**：
 *
 * - 太短 → 一次跟踪失败就把视频撤掉，表现成闪；
 * - 太长 → 照片已经移出画面很久，视频还贴在原地，也就是"贴在空气上"。
 *
 * ⚠️ 与 Android 不同的是，这里过期窗口内的贴图是**完全静止**的（没有世界跟踪去插值）。
 * 所以别靠调大它来解决"跟踪不稳"—— 那只会让贴在空气上的时间变长。跟踪不稳要在跟踪侧修。
 */
export const TTL_MS = 1_000

/**
 * 平滑的时间常数（毫秒）。见 [smoothingAlpha]。
 *
 * Android 那边是 120ms，因为它要把 400ms 一次的台阶抹成连续移动。这里跟踪是每帧的，
 * 台阶本来就很小，所以取 **60ms** —— 只压掉光流的抖动，不明显加重延迟。
 *
 * **不能靠它去压识别本身的抖动**：那是把两件事混在一起，识别抖就该在识别侧修。
 */
/**
 * ⚠️ **`SMOOTH_TAU_MS` / `smoothingAlpha` / `approach` 已经不在渲染路径上了**
 * （2026-08-05）。渲染改用 `render/quadfilter.js` 的自适应预测滤波器 —— 真机实测
 * 这条一阶低通只削掉 11% 的抖动却付了 33% 的滞后，理由与数字写在那个文件里。
 *
 * 留着它们不是为了兼容：`approach` 是一个正确的、被 4 条测试钉住的纯函数，而
 * `quadfilter` 内部的位置更新就是它的逐元素形式。删掉等于把那几条测试一起删掉，
 * 而它们验的是"帧率无关的指数逼近"这条性质本身 —— 那条性质新滤波器同样依赖。
 */
export const SMOOTH_TAU_MS = 60

/** 归一化坐标的容许范围。与服务端 `photoar.quad` 的 COORD_MIN/MAX 同一个理由。 */
export const COORD_MIN = -4
export const COORD_MAX = 5
/** 面积占画面的比例区间。同上，与服务端一致。 */
export const MIN_AREA_FRAC = 0.002
export const MAX_AREA_FRAC = 60
/** 齐次分量的下限。低于它就认为这个四边形跨越了无穷远线，见 [clipVertices]。 */
export const MIN_ABS_W = 1e-6

/**
 * 这四个角像不像一次真实的取景。
 *
 * `verify.normalizedQuad` 已经判过同样几条了，**这里再判一遍不是多余的**：这四个数最终
 * 会变成顶点坐标，而 GL 对一块面积为 0 或者坐标是 1e9 的四边形不会报任何错 —— 它只会
 * 画出一道亮线或者铺满全屏，而这两种表现都会被当成「页面坏了」。判据的成本是四个乘法。
 *
 * @param q 8 个数，归一化图像坐标，顺序左上→右上→右下→左下。
 */
export function plausible(q) {
  if (!q || q.length !== 8) return false
  for (const v of q) {
    if (!Number.isFinite(v) || v < COORD_MIN || v > COORD_MAX) return false
  }
  let area = 0
  for (let i = 0; i < 4; i++) {
    const j = (i + 1) % 4
    area += q[i * 2] * q[j * 2 + 1] - q[j * 2] * q[i * 2 + 1]
  }
  area = Math.abs(area) / 2
  return area >= MIN_AREA_FRAC && area <= MAX_AREA_FRAC
}

/**
 * 单位正方形 → 给定四边形的单应矩阵，行优先 9 个数。
 *
 * 对应关系是 uv 的 (0,0)→q0、(1,0)→q1、(1,1)→q2、(0,1)→q3，也就是照片的
 * 左上→右上→右下→左下。
 *
 * 退化（四点共线、两点重合）时返回 null。这是**必须判**的：那种矩阵后面每一步都算得出
 * 数来，只是数没有意义。
 *
 * 用闭式解（Heckbert）而不是解 8×8 线性方程组：单位正方形这个特例有教科书公式，而每帧
 * 都要算；通用求解要么引入一个矩阵库、要么自己写高斯消元 —— 后者在这个规模上只会更容易
 * 写错，且没有任何精度收益。
 */
export function unitSquareH(q) {
  if (!q || q.length !== 8) return null
  const x0 = q[0], y0 = q[1]
  const x1 = q[2], y1 = q[3]
  const x2 = q[4], y2 = q[5]
  const x3 = q[6], y3 = q[7]

  const sx = x0 - x1 + x2 - x3
  const sy = y0 - y1 + y2 - y3

  let g, h
  if (Math.abs(sx) < 1e-9 && Math.abs(sy) < 1e-9) {
    // 仿射（平行四边形）。不是边角情况：正对着照片拍就是这一支，而通用公式在这里的
    // 分母 den 也趋于 0，硬走过去会放大数值噪声。
    g = 0
    h = 0
  } else {
    const dx1 = x1 - x2
    const dx2 = x3 - x2
    const dy1 = y1 - y2
    const dy2 = y3 - y2
    const den = dx1 * dy2 - dy1 * dx2
    if (!Number.isFinite(den) || Math.abs(den) < 1e-12) return null
    g = (sx * dy2 - sy * dx2) / den
    h = (dx1 * sy - dy1 * sx) / den
  }

  const out = new Float32Array([
    x1 - x0 + g * x1, x3 - x0 + h * x3, x0,
    y1 - y0 + g * y1, y3 - y0 + h * y3, y0,
    g, h, 1,
  ])
  for (const v of out) if (!Number.isFinite(v)) return null
  return out
}

/**
 * 整张照片。视频面片**永远铺满照片**，所以顶点那一侧不再有第二种取值。
 *
 * 不 `Object.freeze`：对有元素的 TypedArray 调它直接抛 `TypeError`。**只读靠约定** ——
 * 这个数组是共享的，谁往里写谁就改了所有调用方看到的东西。
 */
export const FULL_RECT = new Float32Array([0, 0, 1, 1])

/**
 * 视频要裁掉哪一圈：按视频自己的比例**盖满整张照片**（`object-fit: cover`），不变形。
 *
 * ## 这条规则改过一次，两次都是用户定的
 *
 * 上一版是**内嵌**（`contain`）：原话「至少有一个维度（长或者宽）是贴合图片的，按视频
 * 最大化完整显示为准」。那时的注释还特意写了「不要顺手改成裁切填满」。
 * 2026-08-05 用户看到真机效果之后推翻了它：「需要最小包含盖住照片」。
 *
 * **代价是实打实的，改回去之前先看一眼**：竖屏视频配横着的 6 寸照片（16:9 对 3:2），
 * 裁到盖满要切掉左右各三成 —— 人像视频被切掉的正好是人。内嵌那一版没有黑边问题
 * （视频比照片小的时候，露出来的是照片本身），它换的就是这个。
 *
 * ## 裁的是**源**，不是目的地
 *
 * 内嵌那一版缩的是照片里的目的地矩形；盖满这一版目的地恒等于整张照片（[FULL_RECT]），
 * 缩的是**采样源**。两者的 span 公式相同、但作用在相反的轴上 —— 内嵌收会溢出的那条边，
 * 盖满裁会溢出的那条边。
 *
 * @param photoAspect 照片的宽/高
 * @param videoAspect 视频的宽/高。≤0 或非有限（播放器还没报 videoWidth）时不裁。
 * @returns 源图上的 `[u0, v0, u1, v1]`，**v 向下**（图像坐标系，与照片一致）
 */
export function videoCrop(photoAspect, videoAspect) {
  const full = new Float32Array([0, 0, 1, 1])
  if (!Number.isFinite(videoAspect) || videoAspect <= 0) return full
  if (!Number.isFinite(photoAspect) || photoAspect <= 0) return full
  if (videoAspect >= photoAspect) {
    // 视频比照片「宽」→ 高度顶满，左右各裁掉溢出的部分
    const span = photoAspect / videoAspect
    return new Float32Array([(1 - span) / 2, 0, (1 + span) / 2, 1])
  }
  // 视频比照片「高」→ 宽度顶满，上下各裁掉溢出的部分
  const span = videoAspect / photoAspect
  return new Float32Array([0, (1 - span) / 2, 1, (1 + span) / 2])
}

/**
 * 四个顶点的纹理坐标，8 个数。**这个函数存在的唯一理由是 v 轴的方向。**
 *
 * `texImage2D` 把图像的**首行（顶部）放在 t=0**，也就是 GL 的 t 轴与图像的 v 轴同向。
 * 而 [clipVertices] 的顶点顺序是照片的**左下、右下、左上、右上** —— 于是前两个顶点要取
 * 源图 v 较大（靠下）的那一侧。
 *
 * 这一条错过：视频面片的 uv 一直写死成 `[0,0, 1,0, 0,1, 1,1]`（没翻 v），画出来的视频
 * **上下颠倒**。相机背景那块 uv 是翻了的（就在 gl.js 里隔了几行），两处不一致却一直没人
 * 发现 —— 因为视频在手机上从来没播出来过，而桌面测试只验顶点、不验 uv。所以现在它是个
 * 能在 node 里跑的纯函数，`screenquad.test.js` 盯着它。
 *
 * @param crop [videoCrop] 给出的源矩形
 * @param out 长度 8 的输出缓冲，原地写（每帧都调）
 */
export function quadUv(crop, out) {
  if (!crop || crop.length !== 4 || !out || out.length !== 8) return false
  const u0 = crop[0], v0 = crop[1], u1 = crop[2], v1 = crop[3]
  out[0] = u0; out[1] = v1   // 照片左下 ← 源图左下
  out[2] = u1; out[3] = v1   // 照片右下 ← 源图右下
  out[4] = u0; out[5] = v0   // 照片左上 ← 源图左上
  out[6] = u1; out[7] = v0   // 照片右上 ← 源图右上
  return true
}

/**
 * 视频四个顶点的**裁剪空间**坐标，16 个数（4 顶点 × x,y,z,w）。
 *
 * 顶点顺序必须与 uv 缓冲一致（见 [quadUv]）：照片的左下、右下、左上、右上
 * （TRIANGLE_STRIP）。「左下」指照片 uv 里 v 较大的那一侧 —— 照片 uv 的 v 向下。
 *
 * ## 为什么要带 w，而不是先除完再传 xy
 *
 * 除完再传就是**仿射**插值：两个三角形各自线性插值 uv，结果是纹理沿对角线折一下。
 * 斜着看照片时那道折痕非常明显，而且它看起来像「视频变形了」而不是「插值错了」。
 * 把齐次分量原样交给 `gl_Position`，透视校正插值是光栅化器免费做的 —— 这也是这个函数
 * 存在的全部理由。
 *
 * @param h [unitSquareH] 给出的矩阵，**映到 NDC**
 * @param rect 照片 uv 里的目的地矩形。现在恒为 [FULL_RECT]（视频铺满照片，
 *   比例差异由 [videoCrop] 在源那一侧裁掉）；参数留着是因为它同时是几何测试的入口。
 * @param out 长度 16 的输出缓冲，原地写（每帧都调，不该分配）
 * @returns false 表示这一帧不能画：某个顶点的 w 太小、或者四个 w 不同号，意味着这个
 *   四边形跨越了无穷远线，连出来的形状与真实投影无关。
 */
export function clipVertices(h, rect, out) {
  if (!h || h.length !== 9 || !rect || rect.length !== 4 || !out || out.length !== 16) return false
  const u0 = rect[0], v0 = rect[1], u1 = rect[2], v1 = rect[3]
  // 纹理左下 = 照片 uv 的 (u0, v1)，理由见上面那段
  const us = [u0, u1, u0, u1]
  const vs = [v1, v1, v0, v0]

  let positives = 0
  let negatives = 0
  for (let i = 0; i < 4; i++) {
    const u = us[i]
    const v = vs[i]
    const x = h[0] * u + h[1] * v + h[2]
    const y = h[3] * u + h[4] * v + h[5]
    const w = h[6] * u + h[7] * v + h[8]
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(w)) return false
    if (Math.abs(w) < MIN_ABS_W) return false
    if (w > 0) positives++
    else negatives++
    out[i * 4] = x
    out[i * 4 + 1] = y
    out[i * 4 + 2] = 0
    out[i * 4 + 3] = w
  }
  if (positives !== 4 && negatives !== 4) return false
  if (negatives === 4) {
    // 整体取负：齐次坐标同乘 -1 是同一个点，但 GL 要求 w > 0 才能正常裁剪。
    for (let i = 0; i < 16; i++) out[i] = -out[i]
  }
  return true
}

/**
 * 朝目标逼近的比例，按时间常数算，**与帧率无关**。
 *
 * `1 - exp(-dt/tau)`：帧率翻倍时每帧走一半，两帧之后到同一个地方。用固定的「每帧走 0.2」
 * 会让平滑的快慢跟着帧率飘 —— 而这里的帧率就是相机帧率，它会因为曝光时间变化（暗处降到
 * 15fps）而变，于是同一个页面在暗处的贴合手感会不一样。
 *
 * @param dtMs 距上一帧的毫秒数。≤0（第一帧、或者时钟回跳）时返回 1，也就是直接跳到目标
 *   —— 首次贴上必须是立刻的，慢慢飘过去看起来像 bug。
 */
export function smoothingAlpha(dtMs, tauMs = SMOOTH_TAU_MS) {
  if (!(dtMs > 0) || !(tauMs > 0)) return 1
  const a = 1 - Math.exp(-dtMs / tauMs)
  return Math.min(1, Math.max(0, a))
}

/** `cur += (target - cur) * alpha`，逐元素、原地。长度不匹配时什么都不做。 */
export function approach(cur, target, alpha) {
  if (!cur || !target || cur.length !== target.length) return
  const a = Math.min(1, Math.max(0, alpha))
  for (let i = 0; i < cur.length; i++) cur[i] += (target[i] - cur[i]) * a
}

/**
 * 归一化图像坐标 → NDC。**Android 那边由 ARCore 的 `transformCoordinates2d` 承担的那一步。**
 *
 * 要处理的是一件 Android 上不存在的事：相机图的宽高比通常**不等于**画布的宽高比
 * （手机竖屏画布 9:19.5，而相机帧是 4:3），而画面是按 `object-fit: cover` 铺的 ——
 * 也就是**短边顶满、长边溢出被裁掉**。相机图上的一个点落在屏幕哪里，取决于这道裁切。
 *
 * 不处理它的后果不是"画歪一点"：视频会**整体偏移并缩放错**，而且偏移量随手机横竖屏
 * 和机型变化 —— 在一台机器上调准了，换一台又不对，最难查的那一类。
 *
 * @param q 8 个归一化图像坐标（0..1 是整幅相机图，可越界）
 * @param frameAspect 相机图的 宽/高
 * @param canvasAspect 画布的 宽/高
 * @param out 长度 8 的输出缓冲，原地写
 * @returns out
 */
export function imageToNdc(q, frameAspect, canvasAspect, out) {
  // cover：相机图按哪个方向被裁。frameAspect > canvasAspect 时图比画布"宽"，
  // 左右各裁掉 (1 - canvasAspect/frameAspect)/2 的比例；反之上下裁。
  let sx = 1
  let sy = 1
  if (frameAspect > canvasAspect) sx = frameAspect / canvasAspect
  else sy = canvasAspect / frameAspect

  for (let i = 0; i < 4; i++) {
    const u = q[i * 2]
    const v = q[i * 2 + 1]
    // 图像坐标 0..1（y 向下）→ 以画面中心为原点的 -1..1（y 向上），再乘裁切缩放。
    out[i * 2] = (u * 2 - 1) * sx
    out[i * 2 + 1] = (1 - v * 2) * sy
  }
  return out
}

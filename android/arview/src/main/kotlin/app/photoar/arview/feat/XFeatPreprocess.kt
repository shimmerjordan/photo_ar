package app.photoar.arview.feat

/**
 * XFeat 的输入预处理。**这是 `src/photoar/xfeat.py` 的 `prepare()` 的第二份实现。**
 *
 * 这个文件里不许出现任何 android.* 依赖 —— 它是端上提特征这条路上唯一能被 JVM 单测
 * 覆盖的部分，而它同时也是最容易静默出错的部分。
 *
 * ## 契约（六条，逐条与 Python 侧对齐）
 *
 *  1. **RGB**（不是 BGR）。Python 侧从 `cv2` 拿到的是 BGR，所以它要转一次；Android 侧
 *     从 `Bitmap` 拿到的是 ARGB_8888，本来就是 RGB 序，所以这里**不转**。两边最终都是
 *     RGB —— 别照抄 Python 里那句 `cvtColor(BGR2RGB)`，照抄就是把它反过来。
 *  2. 缩到**长边 = [CANVAS]（640）**，保持长宽比。
 *  3. **镜像补边**（BORDER_REFLECT_101）到 640×640，**只补右侧与下方**。
 *  4. HWC → **NCHW**，float32。
 *  5. 值域 **0..255，不除 255**。
 *  6. 另传 `size = [有效高, 有效宽]`，图内用它掩掉补边区的关键点。
 *
 * **任何一条不一致都不会报错，只会让描述子对不上、识别率静默变低。** 没有异常、没有
 * 日志、没有一个接口返回值会变 —— 表现只是「扫不太出来」。这就是为什么第 3 条要补在
 * 右下而不是四周、第 5 条不能顺手归一化、第 1 条不能照抄。
 *
 * ### 第 3 条为什么是镜像而不是补黑
 *
 * 模型第一层是 InstanceNorm，按整张画布算均值方差。补黑会把统计量拉偏，而参考图
 * （3:2）与相机帧（16:9）补黑的面积不同 —— 同一处纹理在两侧会拿到不同的描述子。
 * 镜像保留原图统计特性，且镜像边界连续、不造阶跃边。
 *
 * ### 关键点坐标在哪个坐标系里
 *
 * 画布坐标系。而补边只加在右下，所以它同时就是「缩放后图像」的坐标系 —— 与 ORB 路径
 * （`features.resize_to_long_edge` 之后的坐标）完全同一个约定，下游 RANSAC 不需要任何
 * 坐标映射。服务端 `featurebody._check_bounds` 就是按这个约定验坐标的。
 *
 * ## 重采样核**没有**要求与 OpenCV 逐位相同
 *
 * 缩放用面积平均（对应 `cv2.INTER_AREA`）或双线性（对应 `INTER_LINEAR`），语义相同，
 * 但取整规则在半像素边界上可能差 1 个灰阶（`cvRound` 是 round-half-to-even，JVM 的
 * `Math.round` 是 round-half-up）。这个差异是刻意接受的：真正要两边一致的是上面那六条
 * 契约，而 ±1 个灰阶远小于相机噪声与 JPEG 量化。
 * 跨语言的 golden 值（见 `XFeatPreprocessTest` / `tests/test_xfeat_prepare.py`）用的是
 * 「2×2 块内同值 + 缩放比恰好 0.5」的合成图，正好把取整这个变量消掉。
 */

/** 画布边长。烘死在 ONNX 图里（输入形状固定），改这里不会改模型。 */
const val CANVAS = 640

/** 关键点上限，与导出时的 `top_k` 一致。图的输出形状是 (1, 512, ...)。 */
const val TOP_K = 512

const val DESC_DIM = 64

/**
 * 一帧的像素来源。
 *
 * 抽成接口而不是直接吃 `IntArray`，是为了让真机那条路（`Bitmap.getPixels`）与单测那条
 * 路（自己造一个数组）走同一份预处理代码。`Bitmap` 是 android.*，不能出现在这个文件里。
 */
interface PixelSource {
    val width: Int
    val height: Int

    /** 返回 `0xAARRGGBB`（Android `Bitmap.getPixels` 的格式，alpha 会被忽略）。 */
    fun argbAt(x: Int, y: Int): Int
}

/** 一个 ARGB_8888 数组。真机上就是 `Bitmap.getPixels` 的产物。 */
class ArgbPixels(
    private val pixels: IntArray,
    override val width: Int,
    override val height: Int,
) : PixelSource {
    init {
        require(width > 0 && height > 0) { "尺寸非法：${width}x$height" }
        require(pixels.size >= width * height) {
            "像素数 ${pixels.size} 少于 ${width}x$height"
        }
    }

    override fun argbAt(x: Int, y: Int): Int = pixels[y * width + x]
}

/**
 * 预处理产物。
 *
 * @param nchw 3×640×640 的 float32，平面顺序 R、G、B；下标是 `c*640*640 + y*640 + x`。
 * @param validH 缩放后的有效高（补边之前）。ONNX 的 `size` 输入是 `[validH, validW]`。
 * @param validW 缩放后的有效宽。
 */
class PreparedFrame(val nchw: FloatArray, val validH: Int, val validW: Int) {
    /** ONNX 的第二个输入。int64 —— 图里那两个比较是对 int64 做的。 */
    fun sizeInput(): LongArray = longArrayOf(validH.toLong(), validW.toLong())
}

object XFeatPreprocess {

    private const val PLANE = CANVAS * CANVAS

    /**
     * 缩放后的有效区尺寸 `(validH, validW)`。
     *
     * 与 Python 侧 `xfeat.canvas_size` 逐字对应，包括 `min(CANVAS, ...)` 那一层夹取
     * （长边算完可能因为浮点四舍五入变成 641）。服务端 `featurebody._check_bounds` 用
     * 同一个公式验坐标，两边差一个像素时那道检查会去挡合法请求。
     */
    fun canvasSize(height: Int, width: Int): Pair<Int, Int> {
        require(height > 0 && width > 0) { "尺寸非法：${width}x$height" }
        val scale = CANVAS.toDouble() / maxOf(height, width)
        val nh = clampEdge(Math.round(height * scale).toInt())
        val nw = clampEdge(Math.round(width * scale).toInt())
        return nh to nw
    }

    private fun clampEdge(v: Int): Int = maxOf(1, minOf(CANVAS, v))

    fun prepare(src: PixelSource): PreparedFrame {
        val (nh, nw) = canvasSize(src.height, src.width)
        // 三个通道各自一张缩放后的平面（值域 0..255 的整数），先只填有效区。
        val out = FloatArray(3 * PLANE)
        resizeInto(src, out, nh, nw)
        pad(out, nh, nw)
        return PreparedFrame(out, nh, nw)
    }

    /**
     * 把源图缩到 nh×nw，写进 `out` 每个平面的左上角。
     *
     * 缩小走面积平均、放大走双线性，与 Python 侧那个 `if scale < 1.0` 的分岔一致。
     * 判据用**尺寸比较**而不是重算一遍 scale：`canvasSize` 里已经四舍五入过，
     * `scale < 1.0` 与 `nh < height` 在边界上可能不同意见（比如 641→640），
     * 而真正决定该用哪个核的是「像素变多了还是变少了」。
     */
    private fun resizeInto(src: PixelSource, out: FloatArray, nh: Int, nw: Int) {
        if (nh <= src.height && nw <= src.width) {
            areaInto(src, out, nh, nw)
        } else {
            bilinearInto(src, out, nh, nw)
        }
    }

    /**
     * 面积平均（对应 `cv2.INTER_AREA`）。
     *
     * 每个输出像素等于源图上矩形 `[x*sx, (x+1)*sx) × [y*sy, (y+1)*sy)` 的加权平均，
     * 权重就是重叠长度。缩放比是整数时它退化成简单的块平均，与 OpenCV 的快速路径
     * （`resizeAreaFast`）结果相同 —— golden 测试用的正是那种情形。
     *
     * 不用「取最近邻」代替：相机帧到 640 通常要缩掉 2-3 倍，最近邻会丢掉大部分高频，
     * 而 XFeat 的关键点恰恰长在高频上。那会让端上提出的特征系统性地少于服务端，
     * 且同样不报错。
     */
    private fun areaInto(src: PixelSource, out: FloatArray, nh: Int, nw: Int) {
        val sy = src.height.toDouble() / nh
        val sx = src.width.toDouble() / nw
        val area = sy * sx
        for (oy in 0 until nh) {
            val y0 = oy * sy
            val y1 = y0 + sy
            val iy0 = y0.toInt()
            val iy1 = minOf(src.height, Math.ceil(y1).toInt())
            for (ox in 0 until nw) {
                val x0 = ox * sx
                val x1 = x0 + sx
                val ix0 = x0.toInt()
                val ix1 = minOf(src.width, Math.ceil(x1).toInt())
                var accR = 0.0
                var accG = 0.0
                var accB = 0.0
                for (iy in iy0 until iy1) {
                    val wy = overlap(iy, y0, y1)
                    if (wy <= 0.0) continue
                    for (ix in ix0 until ix1) {
                        val w = wy * overlap(ix, x0, x1)
                        if (w <= 0.0) continue
                        val p = src.argbAt(ix, iy)
                        accR += w * ((p ushr 16) and 0xFF)
                        accG += w * ((p ushr 8) and 0xFF)
                        accB += w * (p and 0xFF)
                    }
                }
                val i = oy * CANVAS + ox
                // 取整到整数灰阶：Python 侧 `cv2.resize` 是 uint8 进 uint8 出，之后才
                // astype(float32)。不取整的话两边会差一个小数部分 —— 单独看无害，但它会
                // 让 golden 值对不上，于是这份契约就没有可验证的锚点了。
                out[i] = Math.round(accR / area).toFloat()
                out[PLANE + i] = Math.round(accG / area).toFloat()
                out[2 * PLANE + i] = Math.round(accB / area).toFloat()
            }
        }
    }

    /** 源图第 i 个像素与区间 [lo, hi) 的重叠长度。 */
    private fun overlap(i: Int, lo: Double, hi: Double): Double {
        val a = maxOf(lo, i.toDouble())
        val b = minOf(hi, i + 1.0)
        return if (b > a) b - a else 0.0
    }

    /**
     * 双线性放大（对应 `cv2.INTER_LINEAR`）。
     *
     * 采样中心用 `(o + 0.5) * s - 0.5` 而不是 `o * s` —— 后者是 OpenCV 在 3.x 之前那个
     * 有半像素偏移的老公式。差半个像素不会报错，但会让端上与服务端的关键点系统性错开，
     * 而关键点错开就意味着描述子采在不同位置上。
     *
     * 这条路在产品里几乎走不到（相机帧和参考图长边都远大于 640），留着是因为「几乎」
     * 不是「不会」：一张 480×320 的老照片缩略图就会走到这里。
     */
    private fun bilinearInto(src: PixelSource, out: FloatArray, nh: Int, nw: Int) {
        val sy = src.height.toDouble() / nh
        val sx = src.width.toDouble() / nw
        for (oy in 0 until nh) {
            val fy = ((oy + 0.5) * sy - 0.5).coerceAtLeast(0.0)
            val y0 = minOf(src.height - 1, fy.toInt())
            val y1 = minOf(src.height - 1, y0 + 1)
            val wy = fy - y0
            for (ox in 0 until nw) {
                val fx = ((ox + 0.5) * sx - 0.5).coerceAtLeast(0.0)
                val x0 = minOf(src.width - 1, fx.toInt())
                val x1 = minOf(src.width - 1, x0 + 1)
                val wx = fx - x0
                val p00 = src.argbAt(x0, y0)
                val p01 = src.argbAt(x1, y0)
                val p10 = src.argbAt(x0, y1)
                val p11 = src.argbAt(x1, y1)
                val i = oy * CANVAS + ox
                out[i] = lerp2(p00, p01, p10, p11, 16, wx, wy)
                out[PLANE + i] = lerp2(p00, p01, p10, p11, 8, wx, wy)
                out[2 * PLANE + i] = lerp2(p00, p01, p10, p11, 0, wx, wy)
            }
        }
    }

    private fun lerp2(
        p00: Int,
        p01: Int,
        p10: Int,
        p11: Int,
        shift: Int,
        wx: Double,
        wy: Double,
    ): Float {
        val a = ((p00 ushr shift) and 0xFF) * (1 - wx) + ((p01 ushr shift) and 0xFF) * wx
        val b = ((p10 ushr shift) and 0xFF) * (1 - wx) + ((p11 ushr shift) and 0xFF) * wx
        return Math.round(a * (1 - wy) + b * wy).toFloat()
    }

    /**
     * 补边到 640×640，**只补右侧与下方**。
     *
     * 退回 REPLICATE 的判据与 Python 侧**逐字相同**，包括它是对两个轴**一起**判的：
     *
     * ```python
     * border = REFLECT_101 if nh > 1 and nw > 1 and CANVAS-nh < nh and CANVAS-nw < nw
     *          else REPLICATE
     * ```
     *
     * 写成逐轴判断（「这个轴补得少就镜像，那个轴补得多就复制」）会在极端长宽比上与
     * Python 侧分道扬镳 —— 一张 3200×400 的全景缩下来是 640×80，补 560 行 > 80，
     * Python 整张都用 REPLICATE，而逐轴版本会在水平方向仍然用镜像（水平方向根本不用补，
     * 所以看起来"一样"）……直到遇到一张两个方向都要补且只有一个超限的图。
     *
     * `CANVAS - nh < nh` 这个条件不是保守估计，它恰好是「镜像下标不会算成负数」的充要
     * 条件：`2*(nh-1) - (CANVAS-1) >= 0` 等价于 `CANVAS < 2*nh`。
     */
    private fun pad(out: FloatArray, nh: Int, nw: Int) {
        if (nh >= CANVAS && nw >= CANVAS) return
        val reflect = nh > 1 && nw > 1 && CANVAS - nh < nh && CANVAS - nw < nw
        // 先补右侧（每一行往右延伸），再补下方（整行复制）。顺序要紧：下方补边会把
        // 已经补好的右侧一起带下去，于是右下角那个矩形自动就对了。反过来做的话，
        // 右侧补边在下半部分会去读还没填的行。
        for (c in 0 until 3) {
            val base = c * PLANE
            if (nw < CANVAS) {
                for (y in 0 until nh) {
                    val row = base + y * CANVAS
                    for (x in nw until CANVAS) {
                        out[row + x] = out[row + srcIndex(x, nw, reflect)]
                    }
                }
            }
            if (nh < CANVAS) {
                for (y in nh until CANVAS) {
                    val from = base + srcIndex(y, nh, reflect) * CANVAS
                    val to = base + y * CANVAS
                    System.arraycopy(out, from, out, to, CANVAS)
                }
            }
        }
    }

    /**
     * 越界下标折回哪里。
     *
     * REFLECT_101 是 `gfedcb|abcdefgh|gfedcba` —— **不重复边界那一行**，所以是
     * `2*(n-1) - i` 而不是 `2*n - 1 - i`（后者是 `BORDER_REFLECT`）。差一行不会报错，
     * 只会让补边区整体错开一个像素，于是 InstanceNorm 的统计量与服务端略有不同。
     */
    private fun srcIndex(i: Int, n: Int, reflect: Boolean): Int =
        if (reflect) 2 * (n - 1) - i else n - 1
}

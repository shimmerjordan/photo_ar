package app.photoar.arview

/**
 * 视频四边形的几何：多大、纹理怎么贴、淡入到第几帧。纯函数，JVM 可测。
 *
 * ## 「多大」这件事改过一次，改的理由要留着
 *
 * 原来的设计假设是「流程是原图 → 打印，打印尺寸入库时就知道」，于是把入库申报的
 * `print_width_m` 直接当四边形宽度。**这个假设不成立** —— 实际用起来照片尺寸不定，
 * 而入库时那个数经常只是个估的。
 *
 * 而它错了不只是「尺寸不准」，是**贴不上**：四边形的大小用申报值、位置用 ARCore 给的
 * `centerPose`，这是两个不同的尺度混在一起。ARCore 的位姿是从 SLAM 来的、量纲真实，
 * 所以申报宽度偏大 N% 的直接后果就是视频比照片大 N%，边缘对不齐。这个现象和「跟踪
 * 算得不准」在画面上一模一样，很容易归错因。
 *
 * 现在的做法见 [quadSize]：**尺度取 ARCore 自己量的 extentX，形状取参考图的比例**。
 * 唯一的前提是照片是矩形 —— 这个前提总成立。
 */
object Geometry {

    /**
     * **人填的**打印宽度的可信区间：2cm 到 2m。超出即为录入错误。
     *
     * 这一档窄，是因为它防的是打字错误（毫米当厘米填、多打一个 0）。
     */
    const val MIN_WIDTH_M = 0.02f
    const val MAX_WIDTH_M = 2.0f

    /**
     * **ARCore 量出来的**宽度的可信区间：1cm 到 5m。
     *
     * 比人填的那一档宽，两个方向的理由都不一样：
     *
     * - 上限放到 5m，因为照片尺寸不定，婚礼现场挂一张两米以上的大幅喷绘完全正常，
     *   而这是**测量值**不是录入值，没有打字错误这个失效模式。用 2m 卡它等于把一张
     *   真实存在的大照片判成数据错误，然后视频不显示、日志里什么都没有。
     * - 下限降到 1cm，因为 ARCore 估算尺寸的收敛期数值会偏小，卡在 2cm 会让开头
     *   几帧被丢掉，表现为视频闪一下才出来。
     */
    const val MIN_MEASURED_WIDTH_M = 0.01f
    const val MAX_MEASURED_WIDTH_M = 5.0f

    /** 长宽比的可信区间。全景照片能到 3:1，再离谱就是数据错了。 */
    const val MIN_ASPECT = 0.2f
    const val MAX_ASPECT = 5.0f

    /** 比例完全拿不到时的兜底：6 寸照片的 3:2。 */
    const val FALLBACK_ASPECT = 1.5f

    /** 淡入时长。§11.8 要求淡入，避免「贴纸感」。 */
    const val FADE_IN_MS = 300L

    /** 羽化宽度，占四边形短边的比例。§11.8 的「边缘 1-2px 羽化」在 shader 里
     *  是相对量：贴图在屏幕上的大小随距离变，用绝对像素反而会时粗时细。 */
    const val FEATHER = 0.012f

    data class QuadSize(val widthM: Float, val heightM: Float) {
        val aspect: Float get() = widthM / heightM
    }

    private fun plausible(a: Float?): Float? =
        a?.takeIf { it.isFinite() && it in MIN_ASPECT..MAX_ASPECT }

    private fun inBand(w: Float, min: Float, max: Float): Float? =
        w.takeIf { it.isFinite() && it in min..max }

    /**
     * 视频四边形该做多大。**这是贴合的关键函数。**
     *
     * @param extentX ARCore 量的物理宽度（`AugmentedImage.getExtentX`）。库里烘了宽度
     *   时它原样回显那个值；没烘时它是 ARCore 自己估出来的**真实测量**，会随着用户
     *   移动手机逐渐收敛。0 = 还没有。
     * @param extentZ 同上的高度。只用来兜底。
     * @param printWidthM 入库申报的打印宽度。**0 或非正 = 未知**，这是合法输入。
     * @param refAspect 服务端给的参考图宽高比（宽/高）。
     * @return null 表示这一帧没有任何可信尺寸，调用方应跳过绘制。
     *
     * ## 尺度：为什么优先 extentX，而不是申报的宽度
     *
     * 位姿（`centerPose`）是 ARCore 给的。四边形要和位姿**同一个尺度**，否则投影到屏幕
     * 上就是大小不匹配。`extentX` 按定义就和 `centerPose` 自洽 —— 它们出自同一次估计。
     * 申报宽度是另一个来源，一旦和 ARCore 的不一致，误差百分比会一比一变成屏幕上的
     * 错位。所以申报值只在 ARCore 还没给出 extent 的那几帧里当垫脚石。
     *
     * ## 形状：为什么优先 refAspect，而不是 extentZ
     *
     * 这里用上「照片一定是矩形」这个前提。矩形 + 已知宽高比 = 形状完全确定，而这个
     * 比例从参考图的像素尺寸算出来是**精确的**。extentZ 在 ARCore 估算尺寸的收敛期
     * 可能偏，此时若照抄 extentZ，视频会被拉长或压扁 —— 而形变比「稍大稍小」难看
     * 得多，因为人眼对人脸的比例极其敏感。
     *
     * 拆开取值的净效果：收敛期最坏情况是视频比照片略大或略小一点，**永远不会变形**。
     */
    fun quadSize(
        extentX: Float,
        extentZ: Float,
        printWidthM: Float,
        refAspect: Float?,
    ): QuadSize? {
        val w = inBand(extentX, MIN_MEASURED_WIDTH_M, MAX_MEASURED_WIDTH_M)
            ?: inBand(printWidthM, MIN_WIDTH_M, MAX_WIDTH_M)
            ?: return null
        val aspect = plausible(refAspect)
            ?: plausible(if (extentZ > 0f) extentX / extentZ else null)
            ?: FALLBACK_ASPECT
        return QuadSize(widthM = w, heightM = w / aspect)
    }

    /** 纹理坐标的缩放与偏移，(0,0)-(1,1) 之内。 */
    data class UvRect(
        val uScale: Float,
        val uOffset: Float,
        val vScale: Float,
        val vOffset: Float,
    )

    /** 整张纹理，不裁不偏。视频按自身比例摆之后就是这个（见 [videoQuad]）。 */
    val FULL_UV = UvRect(uScale = 1f, uOffset = 0f, vScale = 1f, vOffset = 0f)

    /**
     * 视频那块四边形该做多大：**按视频自己的比例，装进照片的矩形里**。
     *
     * ## 为什么从「裁切填满」改成「装进去」
     *
     * 原来是 `fillCropUv`：把视频居中裁掉一部分，正好填满照片的矩形，理由是「照片区域里
     * 出现黑边看起来就是坏了」。那个理由站不住，因为它把一件不存在的事当成了前提 ——
     * **这里没有黑边**。视频四边形是贴在相机画面上的一块半透明贴图，它比照片小的时候，
     * 露出来的是**照片本身**，不是黑色。
     *
     * 而裁切的代价是实打实的：视频和照片的比例经常不一致（竖屏拍的视频配横着的 6 寸
     * 照片，`16:9` 对 `3:2`），裁到填满要切掉视频左右各三成 —— 而人像视频被切掉的正好
     * 是人。用户的话是「视频尺寸和照片尺寸比例有可能不对，差不多就行」，那么「差不多」
     * 里最不能丢的是画面内容。
     *
     * 所以现在：等比缩放到能放进照片矩形的最大尺寸，居中，纹理整张用（[FULL_UV]）。
     * 结果是视频完整、不变形，四条边里有两条和照片对齐、另两条留出一点照片的底 ——
     * 而它仍然**贴在照片那个平面上**（位姿是同一个），这是这件事唯一的硬要求。
     *
     * @param photo 照片自己那块矩形，[quadSize] 算出来的
     * @param videoAspect 视频的像素宽高比；≤0 或非有限（ExoPlayer 还没报 videoSize）
     *   时原样返回 [photo] —— 那几帧按照片的形状铺，等比例来了再换。
     */
    fun videoQuad(photo: QuadSize, videoAspect: Float): QuadSize {
        if (!videoAspect.isFinite() || videoAspect <= 0f) return photo
        if (!photo.widthM.isFinite() || !photo.heightM.isFinite() ||
            photo.widthM <= 0f || photo.heightM <= 0f
        ) {
            return photo
        }
        // 视频比照片「宽」→ 宽度顶满，高度按视频比例缩；反之高度顶满。
        return if (videoAspect >= photo.aspect) {
            QuadSize(widthM = photo.widthM, heightM = photo.widthM / videoAspect)
        } else {
            QuadSize(widthM = photo.heightM * videoAspect, heightM = photo.heightM)
        }
    }

    /** 淡入的当前不透明度。 */
    fun fadeAlpha(elapsedMs: Long, durationMs: Long = FADE_IN_MS): Float {
        if (durationMs <= 0L) return 1f
        if (elapsedMs <= 0L) return 0f
        return (elapsedMs.toFloat() / durationMs.toFloat()).coerceAtMost(1f)
    }
}

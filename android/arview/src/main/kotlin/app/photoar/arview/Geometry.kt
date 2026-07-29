package app.photoar.arview

/**
 * 视频四边形的几何：多大、纹理怎么贴、淡入到第几帧。纯函数，JVM 可测。
 *
 * 「多大」这件事是本项目的物理尺寸红利（§11）：流程是「原图 → 打印」，打印
 * 尺寸入库时就知道，所以能把准确的物理宽度交给 ARCore，而不是让它自己估。
 */
object Geometry {

    /** 打印出来的照片不会比 2cm 窄、也不会比 2m 宽。超出即为数据错误。 */
    const val MIN_WIDTH_M = 0.02f
    const val MAX_WIDTH_M = 2.0f

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

    /**
     * 视频四边形的物理尺寸。
     *
     * @param printWidthM 入库时记下的打印宽度（§6 的 `print_width_m NOT NULL`）
     * @param refAspect 服务端给的参考图宽高比，可能缺（参考图没有宽高记录时）
     * @param arcoreAspect ARCore 自己量出来的 `extentX / extentZ`，可能还没准
     */
    fun printedSize(
        printWidthM: Float,
        refAspect: Float?,
        arcoreAspect: Float? = null,
    ): QuadSize {
        require(printWidthM.isFinite() && printWidthM > 0f) {
            "printWidthM 必须是正数，收到 $printWidthM"
        }
        val w = printWidthM.coerceIn(MIN_WIDTH_M, MAX_WIDTH_M)
        val aspect = plausible(refAspect) ?: plausible(arcoreAspect) ?: FALLBACK_ASPECT
        return QuadSize(widthM = w, heightM = w / aspect)
    }

    private fun plausible(a: Float?): Float? =
        a?.takeIf { it.isFinite() && it in MIN_ASPECT..MAX_ASPECT }

    /** 纹理坐标的缩放与偏移，(0,0)-(1,1) 之内。 */
    data class UvRect(
        val uScale: Float,
        val uOffset: Float,
        val vScale: Float,
        val vOffset: Float,
    )

    /**
     * 视频比例与照片比例不一致时怎么贴：**居中裁切填满**，不留黑边。
     *
     * 照片区域里出现黑边看起来就是坏了（用户看到的是一张实体照片"活"起来，
     * 而不是一个播放器窗口）。拉伸变形比黑边更糟，所以只能裁。
     */
    fun fillCropUv(quadAspect: Float, videoAspect: Float): UvRect {
        if (!quadAspect.isFinite() || !videoAspect.isFinite() ||
            quadAspect <= 0f || videoAspect <= 0f
        ) {
            // 拿不到视频真实比例时（ExoPlayer 还没报 videoSize）先整张铺满，
            // 等 onVideoSizeChanged 之后再算一次。
            return UvRect(1f, 0f, 1f, 0f)
        }
        return if (videoAspect > quadAspect) {
            // 视频更宽 → 切左右
            val s = quadAspect / videoAspect
            UvRect(uScale = s, uOffset = (1f - s) / 2f, vScale = 1f, vOffset = 0f)
        } else {
            // 视频更高 → 切上下
            val s = videoAspect / quadAspect
            UvRect(uScale = 1f, uOffset = 0f, vScale = s, vOffset = (1f - s) / 2f)
        }
    }

    /** 淡入的当前不透明度。 */
    fun fadeAlpha(elapsedMs: Long, durationMs: Long = FADE_IN_MS): Float {
        if (durationMs <= 0L) return 1f
        if (elapsedMs <= 0L) return 0f
        return (elapsedMs.toFloat() / durationMs.toFloat()).coerceAtMost(1f)
    }
}

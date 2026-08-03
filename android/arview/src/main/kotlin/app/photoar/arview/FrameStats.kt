package app.photoar.arview

/**
 * GL 线程每帧花了多久，攒一秒报一次。纯 Kotlin，JVM 可测。
 *
 * ## 为什么要量，而不是按经验猜
 *
 * 「相机很卡顿」有好几个互不相干的成因，而它们在屏幕上一模一样：
 *
 * - GL 线程自己慢（抓帧、YUV 转换、JPEG 编码都在它上面）
 * - 相机档位挑到了 60fps 而这台机器的 SLAM 跟不上（`ArSessionHolder.applyCameraConfig`）
 * - 别的线程抢 CPU（端上提特征的 ONNX 推理、视频解码）
 * - 整机降频（发热）
 * - 装的是 debuggable 包 —— 这一条影响的是**整个 App**，包括没有相机的设置页
 *
 * 这几条的修法毫不相干，而且第一条能不能排除**只有帧耗时能回答**：GL 线程平均 6ms、
 * 最大 12ms 就说明渲染是健康的，卡在别处；平均 30ms 就说明确实在这里。
 *
 * 之前两轮排查「贴不上」的教训就是这个：没有数就只能从外部现象反推，而反推是猜。
 *
 * ## 为什么把「抓帧」单独算一档
 *
 * 抓帧不是每帧都做 —— 每 400ms 一次（`ScanController.FRAME_INTERVAL_MS`），但那一次
 * 要做 YUV→NV21→JPEG。混进平均值里它会被 24 帧摊薄成看不见，而它恰好是 GL 线程上
 * 唯一一段与渲染无关的重活。所以它有自己的计数和最大值。
 */
class FrameStats(private val windowNs: Long = WINDOW_NS) {

    companion object {
        const val WINDOW_NS = 1_000_000_000L

        /**
         * 超过多少毫秒算一次卡帧。
         *
         * 16.7ms = 60fps 的一帧。相机被配到 60fps（见 `Frames.pickCameraOption`），
         * `updateMode = BLOCKING` 让渲染跟着相机走，所以这就是这里的预算线。
         * 屏幕是 120Hz 也不改这个数：显示刷得再快，内容也只有 60 份。
         */
        const val JANK_MS = 16.7f
        private const val JANK_NS = (JANK_MS * 1_000_000f).toLong()
    }

    /**
     * 窗口起点。**必须有单独的 [started] 而不是拿 0 当「还没开始」** ——
     * `System.nanoTime()` 的零点没有任何保证，它可以恰好返回 0（测试里更是家常便饭）。
     * 拿 0 当哨兵的后果是那一帧不断把窗口起点往后推，于是这一行报得越来越晚，
     * 而报出来的 fps 偏高。
     */
    private var windowStartNs = 0L
    private var started = false
    private var frames = 0
    private var sumNs = 0L
    private var maxNs = 0L
    private var jank = 0

    private var grabs = 0
    private var grabSumNs = 0L
    private var grabMaxNs = 0L

    /** 抓帧（YUV→NV21→JPEG）花了多久。不是每帧都有。 */
    fun grab(durNs: Long) {
        grabs++
        grabSumNs += durNs
        if (durNs > grabMaxNs) grabMaxNs = durNs
    }

    /**
     * 记一帧。
     *
     * @return 攒够一个窗口时返回那一行，否则 null。**返回 null 的那些帧一个字符串都
     *   不拼** —— 这个方法本身在 GL 线程上每帧都跑，它自己不能成为开销。
     */
    fun frame(durNs: Long, nowNs: Long): String? {
        if (!started) {
            started = true
            windowStartNs = nowNs
        }
        frames++
        sumNs += durNs
        if (durNs > maxNs) maxNs = durNs
        if (durNs > JANK_NS) jank++
        if (nowNs - windowStartNs < windowNs) return null

        val elapsedMs = (nowNs - windowStartNs) / 1_000_000f
        val fps = if (elapsedMs > 0f) frames * 1000f / elapsedMs else 0f
        val avgMs = if (frames > 0) sumNs / frames / 1_000_000f else 0f
        val line = StringBuilder()
            .append("GL ").append("%.0f".format(fps)).append("fps")
            .append(" 均").append("%.1f".format(avgMs))
            .append(" 峰").append("%.1f".format(maxNs / 1_000_000f))
            .append("ms 卡").append(jank).append("/").append(frames)
        if (grabs > 0) {
            line.append(" ｜抓帧×").append(grabs)
                .append(" 均").append("%.1f".format(grabSumNs / grabs / 1_000_000f))
                .append(" 峰").append("%.1f".format(grabMaxNs / 1_000_000f)).append("ms")
        }
        reset(nowNs)
        return line.toString()
    }

    private fun reset(nowNs: Long) {
        started = true
        windowStartNs = nowNs
        frames = 0
        sumNs = 0L
        maxNs = 0L
        jank = 0
        grabs = 0
        grabSumNs = 0L
        grabMaxNs = 0L
    }
}

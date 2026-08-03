package app.photoar.arview

/**
 * 调试模式下屏幕左上角那块滚动日志。纯 Kotlin，JVM 可测。
 *
 * ## 为什么要它，而不是继续用一行文字 / logcat
 *
 * 上一版只有一行 —— ARCore 对目标图的看法（`ArSessionHolder.diagnose`）。那一行能回答
 * 「ARCore 有没有看见这张图」，但**回答不了「卡在哪一步」**：一次扫描要穿过抽帧 → 识别
 * → 装目标 → 找图 → 取地址 → 播放器就绪 → 贴合，其中任何一步不动，屏幕上看到的都是
 * 「什么都没发生」。而那一行只覆盖最后一步。
 *
 * logcat 能覆盖全部，但代价是每次都要连电脑。这个 App 出问题的场合恰好是**在外面**
 * （手持真实照片、真实光线），那时手里只有手机 —— 所以这块日志要能截图发出来。
 *
 * ## 去重是必须的，不是优化
 *
 * 有几条是按帧或按秒重复的（贴不上时 ARCore 那行、GL 帧耗时）。不去重的话，十几行的
 * 窗口会在两秒内被同一句话填满，把前面「装目标失败」那种只出现一次的关键行顶出去 ——
 * 而那一行恰恰是唯一有信息量的。所以连续相同的文本折叠成 `×N`，并把时间戳更新成最后
 * 一次 —— 「这句话现在还在刷」和「它两分钟前刷过」是两回事。
 */
class DiagLog(private val capacity: Int = MAX_LINES) {

    companion object {
        /**
         * 保留多少行。
         *
         * 16：竖屏 8sp 等宽字体大约能放 18-20 行而不盖住画面中间那块照片区域 ——
         * 这块日志是排查「贴不上」用的，把被贴的东西盖住就本末倒置了。
         */
        const val MAX_LINES = 16
    }

    private class Entry(val text: String, var atMs: Long, var repeats: Int)

    private val lines = ArrayDeque<Entry>()
    private var startMs = -1L

    /** 现在有几行（折叠后的）。测试与「有没有内容」判断用。 */
    val size: Int get() = lines.size

    /**
     * 记一行。[atMs] 是任意单调时钟的毫秒数，第一次调用的那个值当作零点。
     *
     * 空白文本直接丢掉：调用方常常在拼字符串，拼出空串意味着那个分支没东西可说，
     * 而一行空白会占掉窗口里的一格。
     */
    fun add(atMs: Long, text: String) {
        val t = text.trim()
        if (t.isEmpty()) return
        if (startMs < 0L) startMs = atMs
        val last = lines.lastOrNull()
        if (last != null && last.text == t) {
            last.repeats++
            last.atMs = atMs
            return
        }
        lines.addLast(Entry(t, atMs, 1))
        while (lines.size > capacity) lines.removeFirst()
    }

    /** 最新的在最后一行 —— 和 logcat 一个方向，眼睛不用重新适应。 */
    fun render(): String = lines.joinToString("\n") { e ->
        val stamp = stamp(e.atMs - startMs)
        if (e.repeats > 1) "$stamp ${e.text} ×${e.repeats}" else "$stamp ${e.text}"
    }

    fun clear() {
        lines.clear()
        startMs = -1L
    }

    /**
     * `m:ss.d`，相对这次扫描开始。
     *
     * 用相对时间而不是墙上时钟：要看的量是「两步之间隔了多久」，而绝对时间要在脑子里
     * 做一次减法。十分之一秒的精度够 —— 比这更细的事（帧耗时）由那一行自己带毫秒数。
     */
    private fun stamp(deltaMs: Long): String {
        val d = if (deltaMs < 0L) 0L else deltaMs
        val tenths = d / 100L
        return "%d:%02d.%d".format(tenths / 600L, (tenths / 10L) % 60L, tenths % 10L)
    }
}

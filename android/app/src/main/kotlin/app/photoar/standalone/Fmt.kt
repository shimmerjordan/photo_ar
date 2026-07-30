package app.photoar.standalone

import app.photoar.arview.ApiParseException
import app.photoar.arview.NetErrorKind
import app.photoar.arview.net.HttpFailure
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * 界面上要显示的那些格式化与校验。
 *
 * 单独一个文件、且**不许出现 android.\***：这里全是「差一位就错」的东西（打印宽度
 * 换算、面包屑、错误文案分流），只有能在 JVM 单测里跑才盯得住。界面代码本身在真机
 * 上肉眼可验，这些不行。
 */
object Fmt {

    /** 常用相纸。给的是纸张两边的毫米数，谁是宽由照片方向决定（见 [presetMm]）。 */
    enum class Paper(val label: String, val shortMm: Double, val longMm: Double) {
        P3("3寸", 62.0, 89.0),
        P5("5寸", 89.0, 127.0),
        P6("6寸", 102.0, 152.0),
        P7("7寸", 127.0, 178.0),
        A4("A4", 210.0, 297.0),
    }

    /**
     * §17「App 里给常用尺寸预设」。
     *
     * `print_width_m` 是**参考图水平方向**的物理宽度（ARCore `addImage` 的第三个
     * 参数），所以横着的照片取长边、竖着的取短边。方向从缩略图的像素尺寸来 ——
     * 图还没下下来时按横向算，这也是绝大多数打印照片的情形。
     */
    fun presetMm(paper: Paper, landscape: Boolean): Double =
        if (landscape) paper.longMm else paper.shortMm

    /**
     * 手输的毫米数。范围放到 10–2000：小于名片、大于 A0 的都是笔误，而这个值
     * 直接进 ARCore 的物理宽度，填错不会报错，只会让跟踪一直飘。
     */
    fun parseWidthMm(text: String): Double? {
        val s = text.trim().removeSuffix("mm").removeSuffix("毫米").trim()
        val v = s.toDoubleOrNull() ?: return null
        if (!v.isFinite() || v < 10.0 || v > 2000.0) return null
        return v
    }

    /** 毫米数写进输入框。整数就不带小数点 —— 相纸尺寸都是整毫米。 */
    fun mmText(v: Double): String =
        if (v == Math.floor(v) && !v.isInfinite()) v.toLong().toString()
        else String.format(Locale.US, "%.1f", v)

    /** 服务端存的是米，界面上一律说毫米：打印尺寸没人拿米说。 */
    fun widthMm(printWidthM: Float): String {
        if (!printWidthM.isFinite() || printWidthM <= 0f) return "未知"
        // float 存的米数换回毫米带噪声（`0.089f * 1000 = 88.9999…`），先舍到 0.1mm
        // 再交给 mmText 判整 —— 否则 6寸 会显示成「151.9 mm」这种看着像出了错的值。
        val mm = Math.round(printWidthM * 1000.0 * 10.0) / 10.0
        return mmText(mm) + " mm"
    }

    fun bytes(n: Long): String = when {
        n < 0 -> "未知"
        n < 1024 -> "$n B"
        n < 1024L * 1024 -> String.format(Locale.US, "%.1f KB", n / 1024.0)
        n < 1024L * 1024 * 1024 -> String.format(Locale.US, "%.1f MB", n / (1024.0 * 1024))
        else -> String.format(Locale.US, "%.2f GB", n / (1024.0 * 1024 * 1024))
    }

    /** 服务端所有时间戳都是毫秒（`db.now_ms()`）。0 表示没有。 */
    fun time(ms: Long, tz: TimeZone = TimeZone.getDefault()): String {
        if (ms <= 0L) return "—"
        val f = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.US)
        f.timeZone = tz
        return f.format(Date(ms))
    }

    fun timeShort(ms: Long, tz: TimeZone = TimeZone.getDefault()): String {
        if (ms <= 0L) return "—"
        val f = SimpleDateFormat("MM-dd HH:mm:ss", Locale.US)
        f.timeZone = tz
        return f.format(Date(ms))
    }

    /** §8.1：eval-img 质量分 < 75 服务端直接拒绝，所以 75 是这条标尺的底。 */
    fun qualityLabel(score: Int): String = when {
        score >= 90 -> "很好"
        score >= 80 -> "够用"
        score >= 75 -> "偏低"
        else -> "不达标"
    }

    /**
     * 面包屑。根目录列表没有路径；有路径时按 `/` 切开，空段丢掉。
     *
     * 不做 `..` 化简，理由与 [app.photoar.arview.CatalogParse.joinPath] 相同 ——
     * 这里只负责显示，路径的合法性由服务端 `safepath` 一家说了算。
     */
    fun crumbs(path: String?): List<String> =
        if (path.isNullOrBlank()) emptyList()
        else path.split('/').filter { it.isNotEmpty() }

    /** 面包屑最后一段，用作标题。根目录给「NAS」。 */
    fun dirTitle(path: String?): String = crumbs(path).lastOrNull() ?: "NAS"

    /**
     * 异常 → 一句人话。
     *
     * 401 必须单独说：其它错误重试有意义，令牌错了重试一万次也一样，文案要把人
     * 直接指到设置里那一行去（同 [app.photoar.arview.net.HttpProber] 的判断）。
     */
    fun errText(e: Throwable): String = when {
        e is HttpFailure && e.kind == NetErrorKind.UNAUTHORIZED ->
            "令牌不对，去「设置」里改（${e.status}）"
        e is HttpFailure && e.kind == NetErrorKind.TIMEOUT -> "服务器没回话（超时）"
        e is HttpFailure && e.kind == NetErrorKind.TRANSPORT -> "连不上服务器：${e.message}"
        e is HttpFailure && e.kind == NetErrorKind.SERVER_ERROR -> "服务端出错：${e.message}"
        e is HttpFailure -> e.message ?: "请求失败"
        e is ApiParseException -> "响应看不懂：${e.message}"
        else -> e.message?.takeIf { it.isNotBlank() } ?: e.javaClass.simpleName
    }

    /** 入库耗时。几十秒起步是正常的（要跑 eval-img + ORB + ffmpeg）。 */
    fun elapsed(ms: Long): String = when {
        ms < 1000 -> "$ms ms"
        ms < 60_000 -> String.format(Locale.US, "%.1f 秒", ms / 1000.0)
        else -> "${ms / 60_000} 分 ${(ms % 60_000) / 1000} 秒"
    }
}

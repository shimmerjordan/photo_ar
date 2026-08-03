package app.photoar.standalone

import app.photoar.arview.ApiParseException
import app.photoar.arview.AuthPolicy
import app.photoar.arview.NetErrorKind
import app.photoar.arview.cache.CacheSync
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
            "登录已失效，回「设置」里重新登录（${e.status}）"
        e is HttpFailure && e.kind == NetErrorKind.TIMEOUT -> "服务器没回话（超时）"
        e is HttpFailure && e.kind == NetErrorKind.TRANSPORT -> "连不上服务器：${e.message}"
        e is HttpFailure && e.kind == NetErrorKind.SERVER_ERROR -> "服务端出错：${e.message}"
        e is HttpFailure -> e.message ?: "请求失败"
        e is ApiParseException -> "响应看不懂：${e.message}"
        else -> e.message?.takeIf { it.isNotBlank() } ?: e.javaClass.simpleName
    }

    /**
     * 登录失败 → 一句人话。
     *
     * 与 [errText] 分开：登录接口上 401 与 403 的下一步动作正好相反（重输 / 别再试），
     * 而 [errText] 把两者都指向「回设置里重新登录」—— 在登录界面上那句话是个循环。
     * 分岔判据交给 [AuthPolicy.loginMessage]（在 `:arview` 里，有单测）。
     */
    fun loginErr(e: Throwable): String = when (e) {
        is HttpFailure -> AuthPolicy.loginMessage(e.kind, e.code, e.message)
        is ApiParseException -> AuthPolicy.loginMessage(
            NetErrorKind.BAD_RESPONSE,
            null,
            e.message,
        )
        else -> AuthPolicy.loginMessage(NetErrorKind.TRANSPORT, null, e.message)
    }

    /** 有效期那一行。与 [time] 同一个格式，单独一个名字只为读起来清楚。 */
    fun dateTime(ms: Long): String = time(ms)

    /**
     * 服务端预建离线识别库那一步的结果，说成人话。
     *
     * 五种正常结局都要有各自的一句话：把 [CacheSync.TargetsStatus.BUILDING] 和
     * [CacheSync.TargetsStatus.FAILED] 归成同一句「没同步上」，用户就没法知道自己
     * 该「过一会儿再来」还是「去找管理员」—— 而前者按一下就好了。
     */
    fun prebuiltStatus(r: CacheSync.TargetsResult): String = when (r.status) {
        CacheSync.TargetsStatus.SKIPPED -> "离线识别库：这次没查"
        CacheSync.TargetsStatus.UP_TO_DATE -> "离线识别库已是最新（覆盖 ${r.count} 张）"
        CacheSync.TargetsStatus.DOWNLOADED ->
            "离线识别库已更新（覆盖 ${r.count} 张，${bytes(r.bytes)}）"
        CacheSync.TargetsStatus.BUILDING ->
            "服务端还在建离线识别库，过一会儿再同步一次就好"
        CacheSync.TargetsStatus.EMPTY -> "你还没有被授权任何照片，离线识别库是空的"
        CacheSync.TargetsStatus.FAILED ->
            "离线识别库这次没拿到（${r.detail ?: "原因未知"}），" +
                "扫描不受影响：认不出来时会自动问服务端"
    }

    /**
     * 有照片没进预建库时的那句提醒。没有就返回 null。
     *
     * 必须说出来：ARCore 单个库最多 1000 张，超出的那些**永远**得联网才认得出，而这件
     * 事在界面上没有任何别的痕迹 —— 用户只会觉得「有几张照片时好时坏」。同时要说清它
     * 不是坏了（联网照样认），否则这句话只会制造焦虑。
     */
    fun overflowNote(overflow: Int, maxTargets: Int): String? {
        if (overflow <= 0) return null
        val cap = if (maxTargets > 0) "$maxTargets" else "上限"
        return "有 $overflow 张照片没进离线识别库（ARCore 单个库最多 $cap 张，" +
            "服务端留的是最近入库的那些）。它们联网时照样能扫出来，只是要多等一次往返。"
    }

    /** 入库耗时。几十秒起步是正常的（要跑 eval-img + ORB + ffmpeg）。 */
    fun elapsed(ms: Long): String = when {
        ms < 1000 -> "$ms ms"
        ms < 60_000 -> String.format(Locale.US, "%.1f 秒", ms / 1000.0)
        else -> "${ms / 60_000} 分 ${(ms % 60_000) / 1000} 秒"
    }
}

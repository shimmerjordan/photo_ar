package app.photoar.standalone

import android.content.Context
import android.content.SharedPreferences
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue

/**
 * 运行时的调试模式开关。入口是设置页里连点版本号 [TAPS_TO_ENABLE] 下。
 *
 * 探活状态点（顶栏那颗「Tailscale 296ms」）这类东西挂在它下面：常态界面上它是噪声 ——
 * 用它的人是我，不是宾客。
 *
 * 为什么不用 `BuildConfig.DEBUG`：真机只认 debug 签名（decisions.md §9），所以两种包
 * 在这台机器上无从区分；而真正需要看探活的场合恰好是**在外面**扫不出来的时候，那时
 * 手里就是已经装好的这个包，重装一个 debug 版不是选项。
 *
 * 单例 + Compose state：读的地方分散在顶栏和设置页两处，改了要立刻重组。
 */
object DebugMode {

    /** 连点几下算数。够多到不会误触，又不至于点不出来 —— 安卓「开发者选项」也是这个数。 */
    const val TAPS_TO_ENABLE = 10

    // ⚠️ **跨模块契约**：这两个字符串在 `:arview` 的 `ArScanActivity.debugEnabled()`
    // 里又读了一遍（那边引不到这个对象 —— 它在 `:app`，而 `:app` 依赖 `:arview`，
    // 反向引用会成环；搬下去也不行，这里用了 Compose state 而 `:arview` 没有 Compose）。
    // 改这两个名字要一起改，否则扫描界面上那行 AR 诊断会静默不显示。
    private const val PREFS = "photoar_debug"
    private const val KEY_ENABLED = "enabled"

    private var prefs: SharedPreferences? = null

    /** Compose 读这个。 */
    var enabled by mutableStateOf(false)
        private set

    /** 还差几下。只在 [enabled] 为 false 时有意义，用来给「已经点了几下」一点反馈。 */
    var tapsLeft by mutableIntStateOf(TAPS_TO_ENABLE)
        private set

    /** 幂等；[MainActivity] 一起来就调。 */
    fun init(context: Context) {
        if (prefs != null) return
        val p = context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        prefs = p
        enabled = p.getBoolean(KEY_ENABLED, false)
    }

    /**
     * 版本号被点了一下。
     *
     * @return 这一下是否刚好把调试模式打开（调用方据此提示一句）。
     */
    fun tap(): Boolean {
        if (enabled) return false
        tapsLeft -= 1
        if (tapsLeft > 0) return false
        set(true)
        return true
    }

    fun set(on: Boolean) {
        enabled = on
        tapsLeft = TAPS_TO_ENABLE
        prefs?.edit()?.putBoolean(KEY_ENABLED, on)?.apply()
    }
}

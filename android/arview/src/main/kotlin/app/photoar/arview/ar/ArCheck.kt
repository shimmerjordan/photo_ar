package app.photoar.arview.ar

import android.content.Context
import android.util.Log
import com.google.ar.core.ArCoreApk

/**
 * 问 ARCore 能不能用。**只查，不装** —— 装的事在 [ArCoreRuntime]。
 *
 * 三条路按优先级排，[state] 里就是这个顺序：
 *
 *  1. **内嵌运行时**（[ArCoreEmbeddedRuntime]）：运行时的库和资源就在我们包里，
 *     在本进程直接加载起来，`com.google.ar.core` 不需要存在。这是目标路径 ——
 *     宾客只装一个 APK，中途不再弹「还要装一个应用」。
 *  2. **系统那份**：真装了而且够新，就用它，它会跟着 Play 更新。
 *  3. **装我们包里那份**：上面两条都不成时的退路，会弹系统安装器。
 *
 * 这里刻意不调 `ArCoreApk.requestInstall()`：那个方法内部是 deep-link 到
 * `market://details?id=com.google.ar.core`，而这个 App 的宾客手机大多没有 Play
 * 商店，那一步只会静默失败。
 *
 * 兜底不是可选项：这个 App 的目标是「扫到照片就能看视频」，没 AR 也应该能看，
 * 只是没有贴合效果（§11.9）。
 */
object ArCheck {

    private const val TAG = "ArCheck"

    /**
     * 当前运行时状态。**必须在主线程调**（ArCoreApk 的要求）。
     *
     * 首次调用是异步的，会先返回 [ArRuntimeState.CHECKING]，得再问一次 ——
     * 复查的节奏由 [ArInstallPolicy] 定。
     */
    fun state(context: Context): ArRuntimeState {
        // 先看内嵌运行时。它成了的话 `checkAvailability()` 的答案就不算数了 ——
        // 那个方法查的是 PackageManager 里有没有 com.google.ar.core，而内嵌方案
        // 的全部意义就是**不需要**那个包存在（见 ArCoreEmbeddedRuntime）。
        //
        // start() 幂等且非阻塞，放在这里而不是让 Activity 显式调，是因为「谁需要
        // 知道 AR 在不在」和「谁该把运行时准备好」本来就是同一个问题；分开写迟早
        // 会有一条路径忘了准备。
        ArCoreEmbeddedRuntime.start(context)
        when (ArCoreEmbeddedRuntime.phase()) {
            ArCoreEmbeddedRuntime.Phase.EMBEDDED -> return ArRuntimeState.INSTALLED
            // 还在解 dex。复用 CHECKING 那条轮询路径：语义（还没结论，等一下再问）
            // 和界面提示（「正在准备 AR 组件…」）恰好都是对的。
            ArCoreEmbeddedRuntime.Phase.PREPARING -> return ArRuntimeState.CHECKING
            // SYSTEM：系统那份够新，本来就该走原生查询。
            // FAILED：注入没成，行为退回到改这一版之前 —— 照旧问、照旧装。
            ArCoreEmbeddedRuntime.Phase.SYSTEM,
            ArCoreEmbeddedRuntime.Phase.FAILED,
            -> Unit
        }

        val avail = try {
            ArCoreApk.getInstance().checkAvailability(context)
        } catch (e: Throwable) {
            // 改过 framework 的 ROM 上这里会直接抛。别让它把 App 带走，也别当成
            // 「不支持」—— 抛异常说明的是「问不出来」，而问不出来的最常见原因就是
            // 没有 Google 框架，恰好是装本地那份运行时能解决的情况。
            Log.w(TAG, "checkAvailability 抛异常，按「查不出来」处理", e)
            return ArRuntimeState.UNKNOWN
        }
        return when (avail) {
            ArCoreApk.Availability.SUPPORTED_INSTALLED -> ArRuntimeState.INSTALLED
            ArCoreApk.Availability.SUPPORTED_NOT_INSTALLED -> ArRuntimeState.NOT_INSTALLED
            ArCoreApk.Availability.SUPPORTED_APK_TOO_OLD -> ArRuntimeState.TOO_OLD
            ArCoreApk.Availability.UNSUPPORTED_DEVICE_NOT_CAPABLE ->
                ArRuntimeState.DEVICE_NOT_CAPABLE
            ArCoreApk.Availability.UNKNOWN_CHECKING -> ArRuntimeState.CHECKING
            // ERROR 和 TIMED_OUT 对决策没区别，都是「问不到 Google 的机型档案服务」
            ArCoreApk.Availability.UNKNOWN_ERROR,
            ArCoreApk.Availability.UNKNOWN_TIMED_OUT,
            -> ArRuntimeState.UNKNOWN
        }
    }

    /** 有没有相机权限。没有的话连兜底模式都开不了。 */
    fun hasCamera(context: Context): Boolean =
        context.checkSelfPermission(android.Manifest.permission.CAMERA) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
}

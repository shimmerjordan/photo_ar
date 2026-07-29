package app.photoar.arview.ar

import android.app.Activity
import android.content.Context
import com.google.ar.core.ArCoreApk

/**
 * ARCore 在不在。三种结果，对应三种界面（§11.9）：
 *
 * - [READY]      有，直接开会话
 * - [INSTALLING] 用户同意装 ARCore，这次 Activity 会被重启，下一轮再问
 * - [ABSENT]     没有也装不上（老机型 / 不支持 / 用户拒绝）→ 全屏兜底模式
 *
 * 兜底不是可选项：小米那套 AR 只在支持的机型上有，而这个 App 的目标是
 * 「扫到照片就能看视频」，没 AR 也应该能看，只是没有贴合效果。
 */
enum class ArAvailability { READY, INSTALLING, ABSENT }

object ArCheck {

    /**
     * 查询并在需要时发起安装请求。
     *
     * 必须在 `onResume` 里调用：`requestInstall` 会启动 Play Store 的安装流程，
     * 把当前 Activity 挂起，回来时走的是 onResume 而不是 onCreate。
     *
     * @param userRequestedInstall 只有第一次问的时候传 true。第二次还传 true 会
     *   陷入「弹窗 → 重启 → 弹窗」的循环，这是 ARCore 官方样例里踩过的坑。
     */
    fun check(activity: Activity, userRequestedInstall: Boolean): ArAvailability {
        val avail = try {
            ArCoreApk.getInstance().checkAvailability(activity)
        } catch (e: Throwable) {
            // 某些改过 framework 的 ROM 上这里会直接抛，别让它把 App 带走
            return ArAvailability.ABSENT
        }
        if (avail.isTransient) {
            // 还在查（首次调用要问一次 Play 服务）。当作正在安装：调用方会在
            // 下一帧或下一次 onResume 再问一遍。
            return ArAvailability.INSTALLING
        }
        if (!avail.isSupported) return ArAvailability.ABSENT
        return try {
            when (ArCoreApk.getInstance().requestInstall(activity, userRequestedInstall)) {
                ArCoreApk.InstallStatus.INSTALLED -> ArAvailability.READY
                ArCoreApk.InstallStatus.INSTALL_REQUESTED -> ArAvailability.INSTALLING
                else -> ArAvailability.ABSENT
            }
        } catch (e: Throwable) {
            ArAvailability.ABSENT
        }
    }

    /** 有没有相机权限。没有的话连兜底模式都开不了。 */
    fun hasCamera(context: Context): Boolean =
        context.checkSelfPermission(android.Manifest.permission.CAMERA) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED
}

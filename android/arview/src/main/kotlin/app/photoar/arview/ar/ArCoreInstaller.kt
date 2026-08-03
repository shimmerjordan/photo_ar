package app.photoar.arview.ar

import android.app.Activity
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInstaller
import android.net.Uri
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.util.Log
import androidx.core.content.FileProvider
import java.io.File
import java.io.IOException
import java.io.OutputStream

/**
 * 装我们自己包里带的那份 ARCore 运行时。
 *
 * 为什么不用 `ArCoreApk.requestInstall()`：它内部是 deep-link 到
 * `market://details?id=com.google.ar.core`，宾客手机没有 Play 商店那一步直接失败。
 * 我们把运行时打进了 assets（见 `build.gradle.kts` 的说明），这里用
 * [PackageInstaller] 自己装。
 *
 * 两条路，[install] 先试、[installLegacy] 垫后：
 *
 * - [install]（[PackageInstaller] 会话）：72 MiB 是**流**进会话的，不落临时文件 ——
 *   `openWrite()` 给的就是一个 OutputStream，从 `AssetManager` 直接倒过去。而且有
 *   状态回执。能用的时候是更好的那条。
 * - [installLegacy]（`ACTION_VIEW` + APK 的 MIME）：MIUI 的安装器在
 *   `InstallStart.onCreate` 里第一件事就是「只要 `sessionId != -1` 且
 *   `SDK_INT <= 34`，一律拒绝」（真机日志：`MIUIPI_InstallStart: blocked session
 *   install because sdk version too low`，反编译确认是 `if-gt SDK_INT, 0x22` 那一跳），
 *   跟我们的 targetSdk、未知来源授权、用户点不点全都无关。老式 Intent 的 sessionId
 *   是 -1，正好跳过那段判断 —— 代价是要在 cache 里落一份 72 MiB，而且没有回执。
 *
 * 没有做 ROM 嗅探：会话失败了才降级，所以在会话可用的机器上不落盘、也不会因为
 * MIUI 改了那个判断就失效。
 */
object ArCoreRuntime {

    private const val TAG = "ArCoreRuntime"

    /** 资产文件名。由 gradle 任务保证是这个固定名字，与 ARCore 版本号无关。 */
    private const val ASSET = "arcore.apk"

    /** 老式安装的暂存目录，与 `res/xml/arcore_paths.xml` 里声明的 path 一致。 */
    private const val STAGE_DIR = "ar"

    /** FileProvider 的 authority 后缀，与清单里的 `${applicationId}` + 这段一致。 */
    private const val AUTHORITY = ".arcore.fileprovider"

    private const val MIME_APK = "application/vnd.android.package-archive"

    const val PACKAGE = "com.google.ar.core"

    /** 安装状态回调的广播 action。加了包名限定，不对外。 */
    const val ACTION_STATUS = "app.photoar.arview.ARCORE_INSTALL_STATUS"

    /** 结果回调。true = 装成功；失败时第二个参数是系统给的原因（可能为 null）。 */
    private var pending: ((Boolean, String?) -> Unit)? = null

    private val main = Handler(Looper.getMainLooper())

    /**
     * 包里到底有没有那份 APK。
     *
     * 不假设「构建脚本保证有」就等于运行时一定读得到：assets 可能被裁、
     * 将来 `:arview` 可能被塞进别的外壳。读不到就是没有，按没有走兜底。
     */
    fun bundled(context: Context): Boolean = try {
        context.assets.open(ASSET).close()
        true
    } catch (e: IOException) {
        Log.w(TAG, "包里没有内置 ARCore 运行时，只能走兜底", e)
        false
    }

    /** 有没有「安装未知来源应用」的授权。API 26 以下不需要这个授权。 */
    fun canInstallPackages(context: Context): Boolean =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            context.packageManager.canRequestPackageInstalls()
        } else {
            true
        }

    /**
     * 把用户送去开「允许安装未知来源」。
     *
     * 这个开关是**按应用**的，所以 Intent 必须带我们自己的包名 —— 不带的话某些 ROM
     * 只会打开一个总列表，用户得自己从几十个应用里找到我们。
     */
    fun requestInstallPermission(activity: Activity) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val intent = Intent(
            Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
            Uri.parse("package:${activity.packageName}"),
        )
        try {
            activity.startActivity(intent)
        } catch (e: Exception) {
            // 有些 ROM 没有这个页面。退到应用详情页，用户还能自己找到那个开关。
            Log.w(TAG, "打不开未知来源设置页，退到应用详情", e)
            try {
                activity.startActivity(
                    Intent(
                        Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:${activity.packageName}"),
                    )
                )
            } catch (ignored: Exception) {
                Log.w(TAG, "连应用详情页都打不开", ignored)
            }
        }
    }

    /**
     * 开始安装。立刻返回，写入在后台线程做；结果通过 [onDone] 回到主线程。
     *
     * 中途系统会弹一个安装确认框（[PackageInstaller.STATUS_PENDING_USER_ACTION]），
     * 用户在那个框里花的时间不计入调用方的任何超时预算 —— 调用方的重试计数应该在
     * `onResume` 里清零。
     */
    fun install(context: Context, onDone: (Boolean, String?) -> Unit) {
        pending = onDone
        val app = context.applicationContext
        // 名字带上用途：这个线程会占着几秒钟，出问题时 traces 里要看得懂是谁。
        Thread({ installBlocking(app) }, "arcore-install").start()
    }

    /** Activity 走了就别再回调 —— 那个 lambda 捕获着它。 */
    fun cancelPending() {
        pending = null
    }

    internal fun onInstallFinished(ok: Boolean, message: String?) {
        val cb = pending ?: return
        pending = null
        main.post { cb(ok, message) }
    }

    /**
     * 老式安装：把资产落到 cache 里，交给系统安装器。
     *
     * 立刻返回，落盘在后台线程做（72 MiB 写主线程会卡住取景画面）；[onLaunched] 回到
     * 主线程，只报「安装界面拉起来了没」—— **装成没成没有回执**，系统安装器是另一个
     * 进程的界面。调用方靠 `onResume` 重新查 [ArCheck.state] 判断结果，所以那边必须
     * 给一段有上限的宽限期（[ArInstallPolicy.MAX_CHECKS]），不能一回来就下结论。
     *
     * [onLaunched] 收到 false 表示这条路也走不通，调用方该兜底了。
     */
    fun installLegacy(activity: Activity, onLaunched: (Boolean) -> Unit) {
        val app = activity.applicationContext
        Thread({
            val apk = try {
                stageApk(app)
            } catch (e: Throwable) {
                Log.w(TAG, "内置安装包落地失败", e)
                null
            }
            // 回主线程再拉界面。这里捕获着 activity，最多活到落盘结束（几百毫秒）——
            // 调用方在回调里自己判 isFinishing/isDestroyed。
            main.post { onLaunched(apk != null && launchInstaller(activity, apk)) }
        }, "arcore-stage").start()
    }

    private fun launchInstaller(activity: Activity, apk: File): Boolean = try {
        // 必须是 content:// —— targetSdk ≥ 24 传 file:// 会直接抛
        // FileUriExposedException。authority 跟着 applicationId 走（清单里用
        // ${applicationId} 占位），所以 :arview 被塞进任何外壳都不会撞车。
        val uri = FileProvider.getUriForFile(activity, "${activity.packageName}$AUTHORITY", apk)
        activity.startActivity(
            Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, MIME_APK)
                // 不给这个 flag，安装器读不到我们 cache 里的文件
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        )
        true
    } catch (e: Throwable) {
        Log.w(TAG, "老式安装也起不来", e)
        false
    }

    /**
     * 把资产拷成一个真实文件。
     *
     * 先写 `.part` 再改名：中途没电/被杀留下的半个文件不能被下一次当成好的用 ——
     * 那会让安装器报一个「安装包已损坏」，而真正的原因在上一次运行里。
     */
    private fun stageApk(context: Context): File {
        val dir = File(context.cacheDir, STAGE_DIR).apply { mkdirs() }
        val apk = File(dir, ASSET)
        val expected = assetLength(context)
        // 已经有一份完整的就别再写 72 MiB。用户第二次点进来时这一步是白干的。
        if (apk.isFile && expected > 0 && apk.length() == expected) return apk
        val part = File(dir, "$ASSET.part")
        part.outputStream().use { writeAsset(context, it) }
        if (!part.renameTo(apk)) {
            part.delete()
            throw IOException("改名失败：${part.absolutePath}")
        }
        return apk
    }

    /**
     * 删掉 [stageApk] 落下的那 72 MiB。
     *
     * 只在确认装好了之后调 —— 装的过程中删会把安装器正在读的文件抽掉。
     */
    fun clearStagedApk(context: Context) {
        val apk = File(File(context.cacheDir, STAGE_DIR), ASSET)
        if (apk.isFile && !apk.delete()) Log.w(TAG, "删不掉暂存的安装包：$apk")
    }

    private fun installBlocking(context: Context) {
        val installer = context.packageManager.packageInstaller
        var sessionId = -1
        try {
            val params = PackageInstaller.SessionParams(
                PackageInstaller.SessionParams.MODE_FULL_INSTALL
            )
            params.setAppPackageName(PACKAGE)

            // 资产长度：拿得到就告诉安装器，它能一次把空间要够，避免中途
            // ENOSPC。拿不到（见下）就传 -1，边写边扩。
            val length = assetLength(context)
            if (length > 0) params.setSize(length)

            sessionId = installer.createSession(params)
            installer.openSession(sessionId).use { session ->
                session.openWrite("arcore", 0, length).use { out ->
                    writeAsset(context, out)
                    session.fsync(out)
                }
                session.commit(statusSender(context, sessionId))
            }
        } catch (e: Throwable) {
            Log.w(TAG, "安装会话失败", e)
            if (sessionId >= 0) {
                try {
                    installer.abandonSession(sessionId)
                } catch (ignored: Exception) {
                    Log.w(TAG, "放弃会话也失败了", ignored)
                }
            }
            onInstallFinished(false, e.message)
        }
    }

    private fun writeAsset(context: Context, out: OutputStream) {
        context.assets.open(ASSET).use { ins ->
            val buf = ByteArray(1 shl 16)
            while (true) {
                val n = ins.read(buf)
                if (n < 0) break
                out.write(buf, 0, n)
            }
        }
        out.flush()
    }

    /**
     * 资产的真实长度。
     *
     * `openFd` 只对**未压缩**的资产有效 —— app 模块必须声明
     * `androidResources { noCompress.add("apk") }`。没声明的话这里会抛，
     * 我们退回 -1（边写边扩）并留一条日志：功能不受影响，只是慢一点、
     * 而且日志里能看出是哪个外壳漏了那行配置。
     */
    private fun assetLength(context: Context): Long = try {
        context.assets.openFd(ASSET).use { it.length }
    } catch (e: IOException) {
        Log.i(TAG, "资产是压缩存放的（app 模块少了 noCompress \"apk\"），长度未知")
        -1L
    }

    private fun statusSender(context: Context, sessionId: Int) =
        PendingIntent.getBroadcast(
            context,
            sessionId,
            Intent(ACTION_STATUS).setPackage(context.packageName),
            // FLAG_MUTABLE 在 API 31+ 是必需的：系统要往这个 Intent 里塞
            // EXTRA_STATUS，不可变的 PendingIntent 它塞不进去，于是我们永远
            // 收不到结果、界面永远卡在「正在安装」。
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_MUTABLE
            } else {
                PendingIntent.FLAG_UPDATE_CURRENT
            },
        ).intentSender
}

/**
 * 接安装结果。清单里注册，不导出。
 */
class ArCoreInstallReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != ArCoreRuntime.ACTION_STATUS) return
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, Int.MIN_VALUE)
        val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT)
                if (confirm == null) {
                    // 拿不到确认框就没法继续。这时候必须回调失败，否则界面会
                    // 一直等一个不会到来的结果 —— 静默的等待比一句「不支持」更糟。
                    Log.w(TAG, "系统要用户确认，但没给 EXTRA_INTENT")
                    ArCoreRuntime.onInstallFinished(false, "系统未提供安装确认界面")
                    return
                }
                // 广播的 context 不是 Activity，必须新开任务栈。
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                try {
                    context.startActivity(confirm)
                } catch (e: Exception) {
                    Log.w(TAG, "拉起安装确认框失败", e)
                    ArCoreRuntime.onInstallFinished(false, e.message)
                }
            }

            PackageInstaller.STATUS_SUCCESS -> ArCoreRuntime.onInstallFinished(true, null)

            else -> {
                Log.w(TAG, "安装失败：status=$status message=$message")
                ArCoreRuntime.onInstallFinished(false, message)
            }
        }
    }

    private companion object {
        const val TAG = "ArCoreInstallRecv"
    }
}

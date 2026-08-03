package app.photoar.arview.ui

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.graphics.Typeface
import android.opengl.GLSurfaceView
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.Gravity
import android.view.SurfaceView
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import android.os.Build
import app.photoar.arview.DiagLog
import app.photoar.arview.EndpointCenter
import app.photoar.arview.Hit
import app.photoar.arview.NoticeKind
import app.photoar.arview.ScanEvent
import app.photoar.arview.ScanRuntime
import app.photoar.arview.ScanState
import app.photoar.arview.ar.ArAction
import app.photoar.arview.ar.ArCheck
import app.photoar.arview.ar.ArCoreRuntime
import app.photoar.arview.ar.ArInstallContext
import app.photoar.arview.ar.ArInstallPolicy

/**
 * 扫描界面。布局用代码写，不用 XML：整个界面就是「一层相机 + 一条提示 + 一个
 * 退出按钮」，而这个 Activity 声明在 **library** 的清单里，外壳把 `:arview`
 * 依赖进来就自动有它，不需要再抄一遍资源。
 *
 * 端点**不**从 Intent 传：Phase 3 起由 [EndpointCenter] 现取现用。这不是为了少
 * 传三个参数 —— 扫描过程中可能换网（走出局域网、Tailscale 刚连上），而 Intent
 * 里的值是启动那一刻的快照，换网后 `endpoints()` 必须能给出新的那一条。
 */
class ArScanActivity : Activity(), ScanRuntime.Listener {

    companion object {
        private const val TAG = "ArScanActivity"

        private const val REQ_CAMERA = 1001

        // 与 `app.photoar.standalone.pixel.PixelPalette` 一一对应。见 buildUi 里那段
        // 注释：跨模块引不过来（会成环），所以这五个值是**手抄**的，改配色要一起改。
        private const val PIXEL_INK = 0xFFE8EAF0.toInt()
        private const val PIXEL_PANEL = 0xE6171A21.toInt() // 带一点透明，相机画面透出来
        private const val PIXEL_EDGE = 0xFF3A4150.toInt()
        private const val PIXEL_AMBER = 0xFFFFC46B.toInt()
        private const val PIXEL_AMBER_DEEP = 0xFF6B4A12.toInt()
        private const val PIXEL_ON_AMBER = 0xFF2A1A00.toInt()

        /**
         * 从「开始安装」到「必须有结论」的上限。
         *
         * 只在前台计时（见 onPause），所以它量的是**写入 + 系统处理**，不含用户在
         * 确认框里犹豫的时间。72 MiB 在慢机上写十几秒是正常的，60s 给足余量；
         * 超了就说明那条状态广播不会来了。
         */
        private const val INSTALL_WATCHDOG_MS = 60_000L

        fun start(context: Context) {
            context.startActivity(intent(context))
        }

        fun intent(context: Context): Intent = Intent(context, ArScanActivity::class.java)
    }

    private lateinit var center: EndpointCenter
    private var runtime: ScanRuntime? = null

    private lateinit var root: FrameLayout
    private lateinit var notice: TextView
    private lateinit var exitButton: Button
    private lateinit var saveButton: Button

    /**
     * 调试日志。只在调试模式下建出来（版本号连点 10 次开）。
     *
     * 放在**左上角**而不是跟着底部那条提示：底部那条是给用户看的（「晃一下手机」），
     * 这一块是给排查看的十几行状态 —— 两者叠在一起会把前者挤掉，而前者是用户唯一
     * 能照着做的东西。
     */
    private var diagView: TextView? = null

    /** 非空 ⇔ 调试模式开着。滚动窗口与折叠都在它里面，见 [DiagLog]。 */
    private var diagLog: DiagLog? = null

    /**
     * 当前锁住的那张照片。从 [ScanEvent.Matched] 拿，退出目标时清掉。
     *
     * 从事件里拿而不是去问状态机：`ScanController.current` 是私有的，而这个事件本来
     * 就带着完整的 [Hit]。开一个 getter 等于给状态机的内部状态多一个外部读者。
     */
    private var lastHit: Hit? = null
    private var lastTitle: String? = null
    private var saving = false
    private var glView: GLSurfaceView? = null

    private val main = Handler(Looper.getMainLooper())
    private var clearNotice: Runnable? = null

    /**
     * 现在是不是前台。**接线必须知道这件事**：AR 的接入是异步的（复查定时器、安装
     * 回调），落地的时候 `onResume` 早就跑完了，没人再去 resume 那个刚建好的运行时。
     * 于是 [ScanRuntime] 的 `wantScanning` 一直是 false，GL 线程照样每帧 update ——
     * 而会话是 paused 的，实测 121 次/秒的 `AR_ERROR_SESSION_PAUSED`，画面全黑不恢复。
     */
    private var resumed = false

    // ---- AR 运行时的接入状态。四个「只做一次」的闸门，每个都对应一种转不出来的圈：
    // 重复复查、重复会话安装、重复老式安装、重复送去设置页。
    private var arChecks = 0
    private var arSessionAttempted = false
    private var arLegacyAttempted = false
    private var arPermissionAsked = false
    private var arInstalling = false
    private var arRecheck: Runnable? = null
    private var arWatchdog: Runnable? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        center = EndpointCenter.get(this)
        center.watchNetwork()
        buildUi()
        if (!center.configured) {
            showNotice("还没配置服务器地址、也没登录", sticky = true)
            return
        }
        if (!center.loggedIn) {
            // 凭证已过期。**在这里就拦住**，不要开始扫描：不拦的话第一帧就是 401，
            // 用户看到相机亮了一下又被弹回去 —— 而这条路在管理员会话（12 小时）上
            // 每天都会走到一次。
            showNotice("登录已过期，回设置里重新登录", sticky = true)
            finish()
            return
        }
        // 进扫描先探一次：从别的界面过来时可能已经探过（有节流兜着），但「直接
        // 从桌面图标进扫描」这条路上还没有任何探活结果，endpoints() 会退回第一个
        // 候选 —— 在外时那就是打不通的 LAN。
        center.refreshAsync()
        if (!ArCheck.hasCamera(this)) {
            requestPermissions(arrayOf(Manifest.permission.CAMERA), REQ_CAMERA)
        }
    }

    /**
     * 像素风的一块面板：实心底 + 2dp 直角硬边框。
     *
     * 用 `GradientDrawable` 而不是 XML 里的 shape：这个 Activity 声明在 **library**
     * 的清单里，整屏布局都是代码写的（外壳只要 include 这个模块就自动有它，不用抄
     * 一份资源）。为一个 drawable 破例引入 res/ 会让"这一屏不带资源"这条约定失效。
     */
    private fun pixelPanel(fill: Int, stroke: Int): android.graphics.drawable.Drawable =
        android.graphics.drawable.GradientDrawable().apply {
            shape = android.graphics.drawable.GradientDrawable.RECTANGLE
            setColor(fill)
            setStroke(dp(2), stroke)
            cornerRadius = 0f // 直角。像素画里没有抗锯齿的曲线。
        }

    /** 像素风的按钮：琥珀底、深色字、直角、等宽。 */
    private fun pixelButton(label: String, onClick: () -> Unit): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            typeface = Typeface.MONOSPACE
            setTextColor(PIXEL_ON_AMBER)
            background = pixelPanel(PIXEL_AMBER, PIXEL_AMBER_DEEP)
            stateListAnimator = null // 默认那套是抬起的阴影，而阴影是模糊的
            minHeight = dp(48) // Material 的触摸目标下限
            setPadding(dp(16), dp(8), dp(16), dp(8))
            visibility = View.GONE
            setOnClickListener { onClick() }
        }

    private fun buildUi() {
        root = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            layoutParams = ViewGroup.LayoutParams(MATCH, MATCH)
        }

        // 这一屏是原生 View（不是 Compose），所以像素风的那套值要在这里再写一遍。
        // 三个值必须和 `pixel.PixelPalette` 对上：琥珀 #FFC46B、面板 #171A21、
        // 边框 2dp 直角。对不上的后果是外壳一套风格、扫描界面另一套，而用户在这两屏
        // 之间来回跳。**没有办法在编译期检查这件事** —— `:arview` 在 `:app` 下层，
        // 引不到那个包（引了会成环）。所以改配色时这里要一起改。
        notice = TextView(this).apply {
            setTextColor(PIXEL_INK)
            textSize = 15f
            typeface = Typeface.MONOSPACE
            setPadding(dp(16), dp(10), dp(16), dp(10))
            background = pixelPanel(PIXEL_PANEL, PIXEL_EDGE)
            visibility = View.GONE
        }
        exitButton = pixelButton("退出这张") { runtime?.controller?.exitTarget() }
        saveButton = pixelButton(SAVE_IDLE_TEXT) { onSaveClicked() }
        // 两个按钮并排。竖排的话底部这一条会占掉画面下半部分 —— 而画面正中间是
        // 用户要对准照片的地方。
        val actions = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_HORIZONTAL
            addView(saveButton, LinearLayout.LayoutParams(WRAP, WRAP))
            addView(exitButton, LinearLayout.LayoutParams(WRAP, WRAP))
        }
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            addView(notice, LinearLayout.LayoutParams(MATCH, WRAP))
            addView(actions, LinearLayout.LayoutParams(WRAP, WRAP))
        }
        root.addView(
            bar,
            FrameLayout.LayoutParams(MATCH, WRAP, Gravity.BOTTOM).apply {
                bottomMargin = dp(24)
            },
        )
        if (debugEnabled()) {
            diagLog = DiagLog()
            diagView = TextView(this).apply {
                setTextColor(PIXEL_AMBER)
                textSize = 8f
                // 等宽：每行的时间戳要对齐成一列，否则十几行下来眼睛得逐行找起点。
                typeface = Typeface.MONOSPACE
                setPadding(dp(6), dp(4), dp(6), dp(4))
                setBackgroundColor(Color.argb(150, 0, 0, 0))
                // 半透明而不是实底：这块日志占了屏幕上四分之一，而它盖住的正是用户
                // 要对准照片的那一片。透一点，至少还看得见照片在不在框里。
                text = "调试日志：等第一行…"
            }
            root.addView(
                diagView,
                // 左上角、宽度只占 3/4：右上角留给系统状态栏那些图标，而且这块日志
                // 每行都不长，铺满整宽只是把背景色摊得更大。
                FrameLayout.LayoutParams(MATCH, WRAP, Gravity.TOP or Gravity.START).apply {
                    marginEnd = dp(72)
                },
            )
        }
        setContentView(root)
    }

    /**
     * ARCore 的可用性只能在 onResume 里问：装运行时和要授权都会把这个 Activity
     * 挂起，回来走的是 onResume 而不是 onCreate。
     */
    override fun onResume() {
        super.onResume()
        resumed = true
        if (!ArCheck.hasCamera(this)) return
        // 复查预算清零。用户刚可能在系统安装框或设置页里待了半分钟，那段时间
        // 不该算在我们的超时里 —— 否则「看完确认框回来」直接就是兜底。
        arChecks = 0
        if (runtime == null) {
            if (arInstalling) {
                // 装到一半被切走又切回来。别重新决策，等回调；但要重新武装看门狗，
                // 因为它在 onPause 里被撤了。
                armInstallWatchdog()
            } else {
                // 这一步可能**当场**就把运行时建出来（状态已经是 INSTALLED，或者
                // 直接兜底），那时候 resume 由 setup 自己做了 —— 所以下面走 else，
                // 不能无条件再 resume 一次。
                evaluateAr()
            }
        } else {
            runtime?.onResume()
        }
    }

    override fun onPause() {
        super.onPause()
        resumed = false
        // 复查和看门狗都只在前台跑。系统安装框一弹，我们就 pause 了 ——
        // 这时候让看门狗继续跑，就会在用户正读确认框的时候判它超时。
        arRecheck?.let { main.removeCallbacks(it) }
        arRecheck = null
        arWatchdog?.let { main.removeCallbacks(it) }
        arWatchdog = null
        runtime?.onPause()
    }

    override fun onDestroy() {
        super.onDestroy()
        // 那个回调 lambda 捕获着这个 Activity，不清就是一条泄漏
        ArCoreRuntime.cancelPending()
        runtime?.destroy()
        runtime = null
    }

    // ---- 接 AR 运行时 ----

    /**
     * 查一次状态、按 [ArInstallPolicy] 走一步。
     *
     * 每一步都必须落到一个**有下文**的分支上：要么开 AR、要么兜底、要么安排了
     * 下一次复查。原来那版在 `UNKNOWN_CHECKING` 时只显示「正在准备 AR 组件…」
     * 就 return，没人再问第二次 —— 于是永久停在那句提示上。
     */
    private fun evaluateAr() {
        val state = ArCheck.state(this)
        val ctx = ArInstallContext(
            state = state,
            bundled = ArCoreRuntime.bundled(this),
            sessionAttempted = arSessionAttempted,
            legacyAttempted = arLegacyAttempted,
            canInstallPackages = ArCoreRuntime.canInstallPackages(this),
            permissionAsked = arPermissionAsked,
            checks = arChecks,
        )
        val action = ArInstallPolicy.decide(ctx)
        // 宾客的手机兜底了，这一行是现场唯一能分清「机型不支持」「没装上」「查不出来」
        // 的线索 —— 界面上那句话是故意含糊的，日志不能也含糊。ArInstallContext 是
        // data class，整条打出来就能离线复现这一步决策。
        Log.i(TAG, "AR 决策：$action ← $ctx")
        ArInstallPolicy.notice(action, state)?.let {
            // 只有兜底那句是「说完就完」，其余几句都在描述一件还在进行的事
            showNotice(it, sticky = action != ArAction.FALLBACK)
        }
        when (action) {
            ArAction.START_AR -> {
                // 确认装上了才删暂存的那 72 MiB。兜底的时候**不删** —— 老式安装没有
                // 回执，兜底那一刻系统安装器可能还在后台读这个文件；而且留着的话
                // 下次进来 stageApk 直接命中，省一次 72 MiB 的写。
                ArCoreRuntime.clearStagedApk(this)
                setup(arReady = true)
            }
            ArAction.FALLBACK -> setup(arReady = false)
            ArAction.RECHECK -> {
                arChecks++
                val r = Runnable {
                    arRecheck = null
                    if (runtime == null && !arInstalling) evaluateAr()
                }
                arRecheck = r
                main.postDelayed(r, ArInstallPolicy.POLL_MS)
            }
            ArAction.GRANT_INSTALL_PERMISSION -> {
                arPermissionAsked = true
                ArCoreRuntime.requestInstallPermission(this)
            }
            ArAction.INSTALL_BUNDLED -> startArInstall()
            ArAction.INSTALL_BUNDLED_LEGACY -> startArInstallLegacy()
        }
    }

    private fun startArInstall() {
        arSessionAttempted = true
        arInstalling = true
        // 「不用联网」是这句话里最有用的信息：宾客现场大概率没网，而这一步真的不需要
        showNotice("正在安装 AR 组件（约 72 MB，不用联网）…", sticky = true)
        armInstallWatchdog()
        ArCoreRuntime.install(this) { ok, message ->
            arInstalling = false
            arWatchdog?.let { main.removeCallbacks(it) }
            arWatchdog = null
            if (!ok && message != null) {
                // 失败原因进日志，界面上只给结论 —— 系统给的那串英文对宾客没意义
                Log.w(TAG, "ARCore 运行时安装失败：$message")
            }
            if (runtime == null) {
                // 装成功也要重新查一遍而不是直接开 AR：装上了但机型不在档案里，
                // 状态会是 DEVICE_NOT_CAPABLE，这时候硬开会话只会崩。
                arChecks = 0
                evaluateAr()
            }
        }
    }

    /**
     * 老式安装 —— 会话安装被 ROM 拦掉之后的那条退路。
     *
     * 和 [startArInstall] 有两处**故意**不一样：
     * - 不设 `arInstalling`、不武装看门狗。没有回执可等，看门狗就没有能等的东西；
     *   而设了 `arInstalling` 反而会让 `onResume` 不再决策 —— 而这条路的判定**只能**
     *   靠 `onResume` 回来重查（宽限期在 [ArInstallPolicy] 那边，有上限）。
     * - 界面没拉起来就立刻重新决策。这时候两条路都试过了，policy 会给兜底。
     */
    private fun startArInstallLegacy() {
        arLegacyAttempted = true
        showNotice("正在安装 AR 组件（约 72 MB，不用联网）…", sticky = true)
        ArCoreRuntime.installLegacy(this) { launched ->
            // 落盘要几百毫秒，这期间用户可能已经退出去了
            if (isFinishing || isDestroyed || runtime != null) return@installLegacy
            if (launched) return@installLegacy
            Log.w(TAG, "老式安装界面也没起来，转兜底")
            arChecks = 0
            evaluateAr()
        }
    }

    /**
     * 安装的看门狗。
     *
     * 没有它的话，只要那条状态广播丢一次（ROM 拦了、PendingIntent 被回收），
     * 界面就永远停在「正在安装」——这正是这一轮要修掉的那类静默等待。
     */
    private fun armInstallWatchdog() {
        arWatchdog?.let { main.removeCallbacks(it) }
        val r = Runnable {
            arWatchdog = null
            if (!arInstalling || runtime != null) return@Runnable
            arInstalling = false
            Log.w(TAG, "ARCore 运行时安装超时，转兜底")
            arChecks = 0
            evaluateAr()
        }
        arWatchdog = r
        main.postDelayed(r, INSTALL_WATCHDOG_MS)
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        val granted = grantResults.isNotEmpty() &&
            grantResults[0] == android.content.pm.PackageManager.PERMISSION_GRANTED
        when (requestCode) {
            REQ_CAMERA ->
                if (ArCheck.hasCamera(this)) {
                    // 权限刚给到，onResume 已经跑过了，这里补一次接线
                    onResume()
                } else {
                    showNotice("没有相机权限，扫不了照片", sticky = true)
                }
            // 只有 API 24-28 会走到这里（29+ 存相册不需要权限）
            REQ_SAVE_STORAGE ->
                if (granted) lastHit?.let { startSave(it) }
                else showNotice("没有存储权限，存不到相册", sticky = false)
        }
    }

    private fun setup(arReady: Boolean) {
        val rt = ScanRuntime(
            activity = this,
            endpoints = { center.endpoints() },
            arAvailable = arReady,
            listener = this,
            viaLabel = { center.viaLabel() },
            onEndpointRefresh = { center.requestRefresh() },
            // 凭证失效 → 直接退出扫描界面。外壳的 Activity 在栈下面，退回去就落在
            // 设置页那条路上；这里不 startActivity 是因为 `:arview` 不许依赖 `:app`
            // （settings.gradle.kts 里那条约束），拿不到外壳的 Activity 类。
            onNeedLogin = { main.post { finish() } },
            onDeviceFeatures = center.config.onDeviceFeatures,
            // 判 diagLog 而不是再调一次 debugEnabled()：两者必须同真同假，否则运行时
            // 会一路打点然后全部丢掉（或者反过来，界面上一行都不出）。
            diagnostics = diagLog != null,
        )
        runtime = rt
        onDiagnostic(if (arReady) "AR 模式" else "无 ARCore，全屏兜底")
        if (arReady) {
            val gl = GLSurfaceView(this)
            glView = gl
            root.addView(gl, 0, FrameLayout.LayoutParams(MATCH, MATCH))
            rt.attachAr(gl)
        } else {
            // 没有 ARCore：一路相机预览打底，命中后视频盖在上面全屏播（§11.9）
            val preview = SurfaceView(this)
            val video = SurfaceView(this).apply { visibility = View.GONE }
            root.addView(preview, 0, FrameLayout.LayoutParams(MATCH, MATCH))
            root.addView(
                video,
                1,
                FrameLayout.LayoutParams(MATCH, MATCH, Gravity.CENTER),
            )
            rt.attachFallback(preview, video)
            // 兜底的那句提示不在这里说：原因只有 evaluateAr 知道（硬件不支持 /
            // 没装上），它已经按 ArInstallPolicy.notice 给过了。在这里再补一句
            // 就会盖掉更准确的那句。
        }
        // 接完线立刻 resume —— 这一步**不能**指望 Activity.onResume。
        //
        // 走到这里的路有三条：onResume 里同步决策出来的、复查定时器落地的、安装
        // 回调落地的。后两条都发生在 onResume 跑完之后，那时候没人再来 resume，
        // 于是 ScanRuntime.wantScanning 一直是 false：GL 线程照样每帧 update，但
        // 会话没 resume 过 —— 真机实测 121 次/秒的 AR_ERROR_SESSION_PAUSED，界面
        // 在前台、屏幕是亮的，画面却是死的。而且这两条恰恰是**常见**路径：
        // checkAvailability 第一次几乎总是 CHECKING。
        //
        // 判 resumed 而不是无条件调：onCreate 里探相机权限那条路会在 onResume 之前
        // 就走到 setup，那时候 resume 了也会被紧随的 onPause 撤掉。
        if (resumed) rt.onResume()
    }

    // ---- ScanRuntime.Listener ----

    /**
     * 调试日志的一行。只在调试模式下显示（版本号连点 10 次开）。
     *
     * 常态下不显示：它是一屏状态名和数字，对宾客毫无意义。但排查「卡在哪一步」时它是
     * 唯一的依据 —— 所以要能一键看到，而不是只能连 adb。
     *
     * 折叠与时间戳都在 [DiagLog] 里，这里只负责把渲染结果塞进 TextView。**每行都重新
     * `render()` 一遍整块文本**：一秒十几行，重拼十几个短字符串远不到能看见的开销，
     * 而换成 append 就要自己处理「折叠时改的是最后一行」，那是两份状态。
     */
    override fun onDiagnostic(line: String) {
        val log = diagLog ?: return
        log.add(System.currentTimeMillis(), line)
        diagView?.text = log.render()
    }

    override fun onScanEvent(event: ScanEvent) {
        when (event) {
            is ScanEvent.Notice -> {
                val text = Notices.text(event.kind, event.detail)
                if (event.kind == NoticeKind.CLEARED || text == null) {
                    hideNotice()
                } else {
                    showNotice(text, sticky = !Notices.transient(event.kind))
                }
            }
            is ScanEvent.StateChanged -> {
                // 只有锁住某张照片的时候才给这两个按钮，扫描状态下它们没有意义
                val locked = when (event.to) {
                    ScanState.IDLE, ScanState.SCANNING -> false
                    else -> true
                }
                exitButton.visibility = if (locked) View.VISIBLE else View.GONE
                saveButton.visibility = if (locked) View.VISIBLE else View.GONE
                if (!locked) {
                    lastHit = null
                    lastTitle = null
                    resetSaveButton()
                }
            }
            is ScanEvent.Matched -> {
                lastHit = event.hit
                lastTitle = event.hit.title
                resetSaveButton()
            }
        }
    }

    // ---- 保存到相册 ----

    private fun resetSaveButton() {
        saving = false
        saveButton.isEnabled = true
        saveButton.text = SAVE_IDLE_TEXT
    }

    private fun onSaveClicked() {
        if (saving) return
        val hit = lastHit ?: return
        // API 24-28 要 WRITE_EXTERNAL_STORAGE；29+ 走 MediaStore，不需要任何权限
        // （清单里那条带了 maxSdkVersion="28"）。
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q && !hasLegacyStoragePermission()) {
            requestPermissions(
                arrayOf(android.Manifest.permission.WRITE_EXTERNAL_STORAGE),
                REQ_SAVE_STORAGE,
            )
            return
        }
        startSave(hit)
    }

    private fun hasLegacyStoragePermission(): Boolean =
        checkSelfPermission(android.Manifest.permission.WRITE_EXTERNAL_STORAGE) ==
            android.content.pm.PackageManager.PERMISSION_GRANTED

    private fun startSave(hit: Hit) {
        val rt = runtime ?: return
        saving = true
        // 置灰 + 改字，否则用户会连点，而每一下都是一次几 MB 的下载。
        saveButton.isEnabled = false
        saveButton.text = "保存中…"
        rt.saveToGallery(hit, lastTitle) { outcome ->
            resetSaveButton()
            showNotice(Notices.saveResult(outcome), sticky = false)
        }
    }

    override fun onFatal(message: String) {
        showNotice(message, sticky = true)
    }

    // ---- 提示条 ----

    private fun showNotice(text: String, sticky: Boolean) {
        clearNotice?.let { main.removeCallbacks(it) }
        clearNotice = null
        notice.text = text
        notice.visibility = View.VISIBLE
        if (!sticky) {
            val r = Runnable { hideNotice() }
            clearNotice = r
            main.postDelayed(r, 4_000L)
        }
    }

    private fun hideNotice() {
        clearNotice?.let { main.removeCallbacks(it) }
        clearNotice = null
        notice.visibility = View.GONE
    }

    /**
     * 调试模式开着吗。
     *
     * ⚠️ **跨模块契约**：这两个字符串必须与 `app.photoar.standalone.DebugMode` 里的
     * `PREFS` / `KEY_ENABLED` 逐字一致（那边是写入方，入口是设置页连点版本号 10 下）。
     *
     * 为什么不直接引那个对象：它在 `:app` 模块里，而这个 Activity 在 `:arview` ——
     * `:app` 依赖 `:arview`，反过来引会成环。而把 `DebugMode` 搬下来也不行，它用的是
     * Compose state，而 `:arview` 没有 Compose（这个界面是纯 View 写的）。
     *
     * 两处字符串对不上的后果很轻（调试行不显示），但会让人以为「开关没生效」，
     * 所以两边都留了指向对方的注释。
     */
    private fun debugEnabled(): Boolean =
        getSharedPreferences("photoar_debug", Context.MODE_PRIVATE)
            .getBoolean("enabled", false)

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}

private const val SAVE_IDLE_TEXT = "保存到相册"
private const val REQ_SAVE_STORAGE = 4711

private const val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
private const val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT

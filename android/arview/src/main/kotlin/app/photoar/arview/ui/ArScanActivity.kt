package app.photoar.arview.ui

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.opengl.GLSurfaceView
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.Gravity
import android.view.SurfaceView
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.TextView
import app.photoar.arview.EndpointCenter
import app.photoar.arview.NoticeKind
import app.photoar.arview.ScanEvent
import app.photoar.arview.ScanRuntime
import app.photoar.arview.ScanState
import app.photoar.arview.ar.ArAvailability
import app.photoar.arview.ar.ArCheck

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
        private const val REQ_CAMERA = 1001

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
    private var glView: GLSurfaceView? = null

    private val main = Handler(Looper.getMainLooper())
    private var askedArInstall = false
    private var clearNotice: Runnable? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        center = EndpointCenter.get(this)
        center.watchNetwork()
        buildUi()
        if (!center.configured) {
            showNotice("还没配置服务器地址和令牌", sticky = true)
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

    private fun buildUi() {
        root = FrameLayout(this).apply {
            setBackgroundColor(Color.BLACK)
            layoutParams = ViewGroup.LayoutParams(MATCH, MATCH)
        }

        notice = TextView(this).apply {
            setTextColor(Color.WHITE)
            textSize = 15f
            setPadding(dp(16), dp(10), dp(16), dp(10))
            setBackgroundColor(Color.argb(180, 0, 0, 0))
            visibility = View.GONE
        }
        exitButton = Button(this).apply {
            text = "退出这张"
            visibility = View.GONE
            setOnClickListener { runtime?.controller?.exitTarget() }
        }
        val bar = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            addView(notice, LinearLayout.LayoutParams(MATCH, WRAP))
            addView(exitButton, LinearLayout.LayoutParams(WRAP, WRAP))
        }
        root.addView(
            bar,
            FrameLayout.LayoutParams(MATCH, WRAP, Gravity.BOTTOM).apply {
                bottomMargin = dp(24)
            },
        )
        setContentView(root)
    }

    /**
     * ARCore 的可用性只能在 onResume 里问：`requestInstall` 会把 Activity 挂起
     * 去装 ARCore，回来走的是 onResume。第一次问允许弹安装框，之后不再弹，
     * 否则会陷入「弹框 → 重启 → 弹框」。
     */
    override fun onResume() {
        super.onResume()
        if (!ArCheck.hasCamera(this)) return
        if (runtime == null) {
            when (val avail = ArCheck.check(this, userRequestedInstall = !askedArInstall)) {
                ArAvailability.INSTALLING -> {
                    askedArInstall = true
                    showNotice("正在准备 AR 组件…", sticky = true)
                    return
                }
                ArAvailability.READY, ArAvailability.ABSENT -> setup(avail == ArAvailability.READY)
            }
        }
        runtime?.onResume()
    }

    override fun onPause() {
        super.onPause()
        runtime?.onPause()
    }

    override fun onDestroy() {
        super.onDestroy()
        runtime?.destroy()
        runtime = null
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQ_CAMERA) return
        if (ArCheck.hasCamera(this)) {
            // 权限刚给到，onResume 已经跑过了，这里补一次接线
            onResume()
        } else {
            showNotice("没有相机权限，扫不了照片", sticky = true)
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
        )
        runtime = rt
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
            showNotice("这台设备不支持 AR，识别后将全屏播放", sticky = false)
        }
    }

    // ---- ScanRuntime.Listener ----

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
                // 只有锁住某张照片的时候才给「退出这张」，扫描状态下它没有意义
                exitButton.visibility = when (event.to) {
                    ScanState.IDLE, ScanState.SCANNING -> View.GONE
                    else -> View.VISIBLE
                }
            }
            is ScanEvent.Matched -> Unit
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

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()
}

private const val MATCH = ViewGroup.LayoutParams.MATCH_PARENT
private const val WRAP = ViewGroup.LayoutParams.WRAP_CONTENT

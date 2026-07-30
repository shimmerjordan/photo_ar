package app.photoar.arview

import android.app.Activity
import android.opengl.GLSurfaceView
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.SurfaceView
import app.photoar.arview.ar.ArSessionHolder
import app.photoar.arview.ar.TargetLoader
import app.photoar.arview.camera.Camera2Source
import app.photoar.arview.gl.ArRenderer
import app.photoar.arview.media.VideoPlayer
import app.photoar.arview.media.VideoTexture
import app.photoar.arview.net.HttpFailure
import app.photoar.arview.net.PhotoArClient
import app.photoar.arview.net.UrlTransport
import java.util.concurrent.Executors

/**
 * 把 [ScanController] 接到真实世界上：ARCore / 相机 / 网络 / ExoPlayer。
 *
 * 这一层刻意不含任何判断逻辑 —— 「什么时候抽帧、命中之后做什么、丢失多久算放弃」
 * 全在状态机里，那是能被 JVM 单测覆盖的部分。这里只有搬运和线程切换，所以真机
 * 上要排查的东西被压到了最小。
 *
 * **三个线程**：
 * - 主线程：状态机、播放器、界面
 * - GL 线程：ARCore 推进、渲染、抓帧、`session.configure()`
 * - 一个网络线程：识别、media 元数据、imgdb 下载
 *
 * `session.configure()` 特意排到 GL 线程上执行：它与 `session.update()` 并发时
 * 行为没有保证，而 update 就在 GL 线程上跑。
 */
class ScanRuntime(
    private val activity: Activity,
    private val endpoints: () -> Endpoints,
    val arAvailable: Boolean,
    private val listener: Listener,
    /** 当前 api 通道的名字，写进 `X-PhotoAR-Endpoint`（服务端记进识别历史）。 */
    private val viaLabel: () -> String? = { null },
    /** 状态机连续失败 2 次时重新探活（§9.2）。默认空实现，单机壳里可以不接。 */
    private val onEndpointRefresh: () -> Unit = {},
) : ScanEffects {

    private companion object {
        const val TAG = "ScanRuntime"
        const val TICK_MS = 100L
    }

    interface Listener {
        fun onScanEvent(event: ScanEvent)

        /** 相机 / 会话这类硬失败，界面要给出可操作的提示。 */
        fun onFatal(message: String)
    }

    private val main = Handler(Looper.getMainLooper())
    private val net = Executors.newSingleThreadExecutor { r ->
        Thread(r, "photoar-net").apply { isDaemon = true }
    }

    private val transport = UrlTransport()
    private val client = PhotoArClient(transport, endpoints, viaLabel)

    private val targetLoader = TargetLoader(client, activity.cacheDir)

    private val ar = if (arAvailable) ArSessionHolder(activity) else null
    private val videoTexture = if (arAvailable) VideoTexture() else null
    private var renderer: ArRenderer? = null
    private var glView: GLSurfaceView? = null

    private var camera: Camera2Source? = null
    private var videoView: SurfaceView? = null

    val controller = ScanController(this, Clock { System.currentTimeMillis() }, arAvailable)

    private val player = VideoPlayer(
        context = activity,
        endpoints = endpoints,
        onReady = { controller.onPlayerReady() },
        onEnded = { controller.onPlaybackEnded() },
        onError = { controller.onPlayerError(it) },
        onVideoSize = { w, h ->
            renderer?.videoAspect = if (h > 0) w.toFloat() / h else 0f
            videoView?.let { fitVideoView(it, w, h) }
        },
    )

    private var glReady = false
    private var wantScanning = false

    private val ticker = object : Runnable {
        override fun run() {
            controller.tick()
            main.postDelayed(this, TICK_MS)
        }
    }

    // ---- 接线 ----

    /** AR 模式。返回的 view 要被放进布局里。 */
    fun attachAr(view: GLSurfaceView) {
        val holder = ar ?: error("arAvailable=false 时不能用 AR 模式")
        val tex = videoTexture!!
        glView = view
        val r = ArRenderer(holder, tex, rendererHost)
        renderer = r
        view.preserveEGLContextOnPause = true
        view.setEGLContextClientVersion(2)
        view.setEGLConfigChooser(8, 8, 8, 8, 16, 0)
        view.setRenderer(r)
        view.renderMode = GLSurfaceView.RENDERMODE_CONTINUOUSLY
        holder.create()?.let { listener.onFatal("ARCore 会话建不起来：$it") }
    }

    /** 全屏兜底模式：一路相机预览 + 一路视频。 */
    fun attachFallback(preview: SurfaceView, video: SurfaceView) {
        videoView = video
        val c = Camera2Source(activity, cameraHost)
        camera = c
        player.attach(video)
        glReady = true // 兜底模式没有 GL 初始化这一步
        preview.holder.addCallback(object : android.view.SurfaceHolder.Callback {
            override fun surfaceCreated(h: android.view.SurfaceHolder) {
                c.start(h.surface)
                if (wantScanning) controller.start()
            }

            override fun surfaceChanged(h: android.view.SurfaceHolder, f: Int, w: Int, ht: Int) = Unit

            override fun surfaceDestroyed(h: android.view.SurfaceHolder) {
                c.stop()
            }
        })
    }

    // ---- 生命周期 ----

    fun onResume() {
        wantScanning = true
        // GL 初始化会走 onSurfaceCreated → onGlReady；第二次 onResume 因为
        // preserveEGLContextOnPause 不会再走一遍，所以下面还要自己判一次。
        glView?.onResume()
        main.removeCallbacks(ticker)
        main.post(ticker)
        if (glReady) resumeAndScan()
    }

    /** GL 资源和会话都到位之后才真正开扫。 */
    private fun resumeAndScan() {
        if (arAvailable) {
            val holder = ar ?: return
            if (!holder.resume()) {
                listener.onFatal("相机打不开，可能被别的应用占用")
                return
            }
        }
        controller.start()
    }

    fun onPause() {
        wantScanning = false
        main.removeCallbacks(ticker)
        controller.stop()
        // 顺序很重要：先停会话再停 GL，否则 GL 线程可能在会话没了之后还 update
        ar?.pause()
        glView?.onPause()
        camera?.stop()
    }

    fun destroy() {
        main.removeCallbacks(ticker)
        player.release()
        ar?.destroy()
        camera?.stop()
        net.shutdownNow()
        // 纹理只能在 GL 线程上删；Activity 已经在关了，走不到就随进程一起没
        glView?.queueEvent { videoTexture?.release() }
    }

    // ---- ScanEffects ----

    override fun captureFrame(seq: Long) {
        val r = renderer
        val c = camera
        when {
            r != null -> r.requestCapture(seq)
            c != null -> c.requestCapture(seq)
            else -> controller.onFrameFailed(seq)
        }
    }

    override fun recognize(seq: Long, jpeg: ByteArray) {
        net.execute {
            try {
                val outcome = client.recognize(jpeg)
                main.post { controller.onRecognized(seq, outcome) }
            } catch (e: HttpFailure) {
                main.post { controller.onRecognizeFailed(seq, e.kind, e.message) }
            } catch (e: Throwable) {
                main.post {
                    controller.onRecognizeFailed(seq, NetErrorKind.TRANSPORT, e.message)
                }
            }
        }
    }

    override fun loadTarget(hit: Hit) {
        val holder = ar ?: return
        net.execute {
            var fallbackReason: String? = null
            val target = try {
                targetLoader.load(hit) { fallbackReason = it }
            } catch (e: Throwable) {
                main.post { controller.onTargetFailed(hit.photoId, e.message) }
                return@execute
            }
            // configure 排到 GL 线程，避免与 update 并发
            onGl {
                val err = when (target) {
                    is TargetLoader.Target.Imgdb ->
                        holder.loadTargetFromImgdb(hit.photoId, target.bytes)
                    is TargetLoader.Target.Thumb ->
                        holder.loadTargetFromBitmap(hit.photoId, target.bitmap, hit.printWidthM)
                }
                main.post {
                    if (err != null) {
                        controller.onTargetFailed(hit.photoId, err)
                    } else {
                        renderer?.setTarget(hit.printWidthM, hit.refAspect)
                        controller.onTargetLoaded(hit.photoId, fallbackReason != null)
                    }
                }
            }
        }
    }

    override fun fetchMedia(hit: Hit) {
        net.execute {
            try {
                val info = client.media(hit)
                main.post { controller.onMedia(hit.photoId, info) }
            } catch (e: Throwable) {
                main.post { controller.onMediaFailed(hit.photoId, e.message) }
            }
        }
    }

    override fun releaseTarget() {
        renderer?.let {
            it.showVideo = false
            it.resetVideoFade()
        }
        videoTexture?.markStale()
        videoView?.visibility = android.view.View.GONE
        ar?.let { holder -> onGl { holder.clearTarget() } }
    }

    override fun preparePlayer(hit: Hit, media: MediaInfo) {
        val url = media.resolvedUrl(endpoints()) ?: run {
            controller.onMediaFailed(hit.photoId, "没有可播的地址")
            return
        }
        videoTexture?.let { tex ->
            tex.markStale()
            tex.surface?.let { player.attach(it) }
        }
        renderer?.resetVideoFade()
        player.prepare(url)
    }

    override fun playVideo() {
        renderer?.showVideo = true
        videoView?.visibility = android.view.View.VISIBLE
        player.play()
    }

    override fun pauseVideo() {
        player.pause()
    }

    override fun releasePlayer() {
        renderer?.showVideo = false
        videoView?.visibility = android.view.View.GONE
        player.release()
    }

    override fun requestEndpointRefresh() {
        // 交给 EndpointCenter：它是异步 + 带节流的，所以这里直接调不会卡主线程，
        // 也不会因为状态机每 2 次失败就请求一次而变成探活风暴。
        Log.i(TAG, "连续失败，请求重新探活 endpoint")
        onEndpointRefresh()
    }

    override fun emit(event: ScanEvent) {
        listener.onScanEvent(event)
    }

    // ---- 回调 ----

    private val rendererHost = object : ArRenderer.Host {
        override fun onTrackingChanged(photoId: String?, isTracking: Boolean) {
            main.post { controller.onTracking(photoId, isTracking) }
        }

        override fun onFrameReady(seq: Long, jpeg: ByteArray) {
            main.post { controller.onFrame(seq, jpeg) }
        }

        override fun onFrameFailed(seq: Long) {
            main.post { controller.onFrameFailed(seq) }
        }

        override fun onGlReady() {
            main.post {
                glReady = true
                // 相机纹理是刚在 GL 线程上分配的，会话 resume 时才会认它
                if (wantScanning) resumeAndScan()
            }
        }
    }

    private val cameraHost = object : Camera2Source.Host {
        override fun onFrameReady(seq: Long, jpeg: ByteArray) {
            main.post { controller.onFrame(seq, jpeg) }
        }

        override fun onFrameFailed(seq: Long) {
            main.post { controller.onFrameFailed(seq) }
        }

        override fun onCameraError(message: String) {
            main.post { listener.onFatal(message) }
        }
    }

    private fun onGl(block: () -> Unit) {
        val view = glView
        if (view != null) view.queueEvent(block) else main.post(block)
    }

    /** 兜底模式下按视频比例摆 SurfaceView，避免拉伸。 */
    private fun fitVideoView(view: SurfaceView, w: Int, h: Int) {
        val parent = view.parent as? android.view.View ?: return
        val pw = parent.width
        val ph = parent.height
        if (pw <= 0 || ph <= 0 || w <= 0 || h <= 0) return
        val scale = minOf(pw.toFloat() / w, ph.toFloat() / h)
        view.layoutParams = view.layoutParams.apply {
            width = (w * scale).toInt()
            height = (h * scale).toInt()
        }
        view.requestLayout()
    }
}

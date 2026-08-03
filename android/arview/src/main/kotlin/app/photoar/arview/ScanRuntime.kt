package app.photoar.arview

import android.app.Activity
import android.opengl.GLSurfaceView
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.SurfaceView
import app.photoar.arview.ar.ArSessionHolder
import app.photoar.arview.ar.LocalTargetDb
import app.photoar.arview.ar.TargetDbSource
import app.photoar.arview.ar.TargetLoader
import app.photoar.arview.cache.MergedLocalIndex
import app.photoar.arview.cache.ModelCache
import app.photoar.arview.cache.OfflineCache
import app.photoar.arview.cache.ServerTargetsStore
import app.photoar.arview.cache.TargetsSnapshot
import app.photoar.arview.cache.localMedia
import app.photoar.arview.camera.Camera2Source
import app.photoar.arview.feat.FeatureExtractor
import app.photoar.arview.feat.FeatureFailure
import app.photoar.arview.feat.FeaturePathPolicy
import app.photoar.arview.feat.FeaturesRequest
import app.photoar.arview.feat.OnnxFeatureExtractor
import app.photoar.arview.feat.RecognizePath
import app.photoar.arview.gl.ArRenderer
import app.photoar.arview.media.MediaSaver
import app.photoar.arview.media.SaveNaming
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
    /**
     * 凭证失效，要把用户送回登录界面。
     *
     * 默认空实现只是为了不逼每个调用点都接一遍。**但不接的后果是真实的**：token 过期
     * 之后扫描会停下来、提示一句「凭证不对」，然后没有任何下一步 —— 而管理员会话只有
     * 12 小时，这是每天都会发生一次的事。
     */
    private val onNeedLogin: () -> Unit = {},
    /**
     * 识别走不走端上提特征。从 [EndpointConfig.onDeviceFeatures] 来。
     *
     * 默认 false（也就是现状那条路）：端上推理在开发机上没法真机验证，默认打开等于
     * 把一条没验过的路径设成所有人的默认行为。
     */
    onDeviceFeatures: Boolean = false,
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

    /**
     * 只跑本地多图库的重建（[installLocalDb]）。
     *
     * **不能和 [net] 共用**：[net] 是单线程的，而重建 200 张缩略图要跑好几秒的特征
     * 提取。共用的话每 400ms 一次的识别请求会整整排在它后面 —— 表现为「刚同步完，
     * 举起手机头几秒怎么都认不出来」，而且不报任何错。
     *
     * 单线程即可：重建是幂等的，同时排两次没有意义。
     */
    private val dbWork = Executors.newSingleThreadExecutor { r ->
        Thread(r, "photoar-localdb").apply { isDaemon = true }
    }

    private val transport = UrlTransport()
    private val client = PhotoArClient(transport, endpoints, viaLabel)

    private val targetLoader = TargetLoader(client, activity.cacheDir)

    /** 离线缓存（Phase 4）。和「缓存管理」页共用同一个实例，见 [OfflineCache]。 */
    private val cache = OfflineCache.of(activity.filesDir)

    /** 端上提特征的开关与失败回退。纯逻辑，见 [FeaturePathPolicy]。 */
    private val featurePath = FeaturePathPolicy(onDeviceFeatures)

    private val modelCache = ModelCache(activity.filesDir)

    /**
     * 端上提特征专用的线程：下模型、建会话、跑推理、关会话。
     *
     * **不能和 [net] 共用**（同 [dbWork] 那条理由）：模型第一次要下 4.31MB，而 [net]
     * 是单线程的 —— 那两分钟里每 400ms 一次的识别请求会整整排在它后面，表现为「打开
     * 这个开关之后就再也扫不出来了」。
     *
     * 也**必须**是单线程的：ONNX 会话的创建、使用、销毁因此天然有序，不需要锁，也不
     * 可能在 [destroy] 时关掉一个正在 run 的会话。
     */
    private val featWork = Executors.newSingleThreadExecutor { r ->
        Thread(r, "photoar-feat").apply { isDaemon = true }
    }

    /**
     * 提特征器。第一次真正需要时才建（模型可能要先下 4.31MB，不该挡在启动路径上）。
     *
     * `@Volatile`：在 [featWork] 上赋值，在状态机的主线程回调（[recognize]）里读。
     */
    @Volatile
    private var cachedExtractor: FeatureExtractor? = null

    /** 已经排了一次「把模型准备好」。见 [prepareExtractorAsync]。 */
    private val preparing = java.util.concurrent.atomic.AtomicBoolean(false)

    /**
     * 服务端预建的整库目标在本地那一份。**没有 arAvailable 判断** —— 它只是两个文件，
     * 而「这台机器认不认得 ARCore」与「磁盘上有没有那份库」是两件事。
     */
    private val serverTargets = ServerTargetsStore(cache)

    private val localDb = if (arAvailable) LocalTargetDb(cache, serverTargets) else null

    /**
     * 预建库的 manifest。离线命中时那些「预建库里有、端侧没缓存」的照片靠它查元数据。
     *
     * 每次装库时重读一遍（用户可能刚在缓存管理页同步过），所以是 `@Volatile`：
     * 在 [dbWork] 上写，在主线程的 [LocalIndex] 回调里读。
     *
     * **初值刻意是 null，不在这里读一次**：1000 条的 `targets.json` 有一两百 KB，解析它
     * 是几十毫秒，而这个字段是在构造 [ScanRuntime] 时初始化的 —— 也就是打开扫描界面那
     * 一下的主线程上。等 [installLocalDb] 在后台读也不损失什么：库还没装进 session 之前，
     * ARCore 本来就不会报出任何目标。
     */
    @Volatile
    private var targetsSnapshot: TargetsSnapshot? = null

    private val ar = if (arAvailable) ArSessionHolder(activity) else null
    private val videoTexture = if (arAvailable) VideoTexture() else null
    private var renderer: ArRenderer? = null
    private var glView: GLSurfaceView? = null

    private var camera: Camera2Source? = null
    private var videoView: SurfaceView? = null

    val controller = ScanController(
        this,
        Clock { System.currentTimeMillis() },
        arAvailable,
        // ARCore 从多图库里认出来的名字就是 photoId（Phase 2 定的）。拿它去两个元数据
        // 来源里查：端侧缓存索引（带本地视频路径）优先，然后是预建库的 manifest ——
        // 后者覆盖的照片可以比端侧缓存多得多（1000 vs 200）。合并规则与它的理由都在
        // [MergedLocalIndex]。
        localIndex = MergedLocalIndex(
            cached = { id -> cache.byId(id) },
            snapshot = { targetsSnapshot },
        ),
    )

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
    ).apply {
        // AR 模式循环，全屏兜底不循环。两者不同的理由写在 VideoPlayer.looping 上：
        // 兜底模式靠「播完」这个事件退回扫描，那是它唯一的出口。
        looping = arAvailable
    }

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
            // 会话压根没建起来时不能往下走到那句「相机被占用」。
            //
            // [ArSessionHolder.resume] 的第一行就是 `session ?: return false`，于是
            // 「`Session()` 构造失败」和「相机真被别的 App 占着」在这里长得一模一样。
            // 而前者的原因（版本闸门没过、设备标定档案读不到、UnavailableException 的
            // 原文）[attachAr] 已经报过一次准确的了 —— 再报一次只会把它盖掉，然后
            // 屏幕上留着一句指向完全错误方向的话：去关别的相机应用，关到天亮也没用。
            if (holder.session == null) return
            if (!holder.resume()) {
                listener.onFatal("相机打不开，可能被别的应用占用")
                return
            }
            installLocalDb(holder)
        }
        controller.start()
    }

    /**
     * 把离线多图库装进会话（§11.3 / Phase 4，Phase 6 起优先装服务端预建的那一份）。
     *
     * 挑库/建库和装库分在两个线程上：挑库可能要 `deserialize` 一次、建库要跑几秒的特征
     * 提取，放 GL 线程上就是启动扫描时预览卡住；而装库会 `session.configure()`，必须在
     * GL 线程。那一步走 [dbWork] 而不是 [net]，理由见那里 —— 和识别请求共线程会把识别
     * 整整堵住几秒。
     *
     * **一个字节都不下**：预建库的下载在「缓存管理」页那条同步里（`CacheSync`）。这条
     * 路上用户正举着手机等画面，不能在这里等一次 6MB 的传输。
     *
     * 装不上不影响扫描 —— 离线识别没了就退回「每 400ms 问一次服务端」，也就是
     * Phase 2/3 的行为。所以这里基本只记日志；唯一要弹提示的是「预建库装不上、已退回
     * 端上现建」，因为那件事在界面上唯一的表现是「框比以前抖」。
     */
    private fun installLocalDb(holder: ArSessionHolder) {
        val db = localDb ?: return
        dbWork.execute {
            val session = holder.session ?: return@execute
            // manifest 可能刚被一次同步换掉，每次装库前重读一遍。
            targetsSnapshot = serverTargets.snapshot()
            val prepared = try {
                db.prepare(session)
            } catch (e: Throwable) {
                Log.w(TAG, "挑/建离线库炸了（不影响联网扫描）", e)
                null
            }
            prepared?.rebuild?.let { r ->
                if (r.failure != null) {
                    Log.w(TAG, "端上现建库失败：${r.failure}")
                } else {
                    Log.i(TAG, "端上现建库：${r.accepted} 张可认，${r.rejected.size} 张被拒")
                }
            }
            prepared?.serverFailure?.let { why ->
                Log.w(TAG, "服务端预建库装不上，已退回端上现建：$why")
                main.post { controller.onTargetsDbFallback() }
            }
            Log.i(TAG, "离线库来源：${prepared?.source ?: "无"}")
            onGl { db.install(holder)?.let { Log.w(TAG, "离线库装载失败：$it") } }
        }
    }

    fun onPause() {
        wantScanning = false
        main.removeCallbacks(ticker)
        controller.stop()
        // 顺序很重要：先停会话再停 GL，否则 GL 线程可能在会话没了之后还 update
        ar?.pause()
        glView?.onPause()
        camera?.stop()
        localDb?.onSessionGone()
        // 这一轮扫到过哪些照片（lastSeenAt）在这里落盘 —— markSeen 自己不写盘，
        // 因为扫描时每帧都可能命中。丢一次的代价只是排序略旧一点。
        flushCache()
    }

    private fun flushCache() {
        net.execute {
            try {
                cache.flush()
            } catch (e: Throwable) {
                Log.w(TAG, "缓存索引落盘失败", e)
            }
        }
    }

    fun destroy() {
        main.removeCallbacks(ticker)
        player.release()
        ar?.destroy()
        camera?.stop()
        net.shutdownNow()
        dbWork.shutdownNow()
        // ONNX 会话只在 featWork 上被创建和使用，所以关它也排在那条线程上 —— 队列是
        // FIFO 的，这个任务必然排在任何在飞的推理之后。
        //
        // 用 shutdown() 而不是 shutdownNow()：后者不会执行已排队的任务，于是那份会话
        // 一直挂在进程上。ONNX 会话的内存是 native 的，**不受 GC 管** —— 反复进出扫描
        // 界面（每次 new 一个 ScanRuntime）会真的一路涨上去。
        featWork.execute {
            cachedExtractor?.close()
            cachedExtractor = null
        }
        featWork.shutdown()
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

    /**
     * 两条路（传 JPEG / 传端上特征）在这里分岔。
     *
     * 分岔点放在这一层而不是状态机里是刻意的：状态机不该知道「识别」有两种实现，
     * 它只管什么时候抽帧、命中之后做什么 —— 那才是能被单测覆盖的部分。
     *
     * **模型没准备好时这一帧照常走 JPEG，而不是等**。第一次要下 4.31MB（超时给到
     * 2 分钟），而 [ScanController.RECOGNIZE_WATCHDOG_MS] 只有 4 秒 —— 让它排在
     * 识别前面的话，前几十帧全部超时，状态机会连续报「网络不稳」并反复重新探活，
     * 而真实情况是模型正在下。所以准备工作在另一条线程上做，扫描一秒都不停。
     */
    override fun recognize(seq: Long, jpeg: ByteArray) {
        if (featurePath.path == RecognizePath.FEATURES) {
            val ready = cachedExtractor
            if (ready != null) {
                // 推理与这条路的上传都排在 featWork 上：ONNX 会话的创建、使用、销毁
                // 因此全在同一条线程上，不需要任何锁，也不可能在 destroy 时把一个
                // 正在 run 的会话关掉。
                featWork.execute { deliver(seq) { recognizeByFeatures(ready, jpeg) } }
                return
            }
            prepareExtractorAsync()
        }
        net.execute { deliver(seq) { client.recognize(jpeg) } }
    }

    /** 跑一次识别并把结果 / 失败送回状态机。两条路共用。 */
    private fun deliver(seq: Long, block: () -> RecognizeOutcome) {
        try {
            val outcome = block()
            main.post { controller.onRecognized(seq, outcome) }
        } catch (e: HttpFailure) {
            main.post {
                if (e.kind == NetErrorKind.UNAUTHORIZED) {
                    controller.onUnauthorized(e.message)
                } else {
                    controller.onRecognizeFailed(seq, e.kind, e.message)
                }
            }
        } catch (e: Throwable) {
            main.post {
                controller.onRecognizeFailed(seq, NetErrorKind.TRANSPORT, e.message)
            }
        }
    }

    /**
     * 端上提特征那条路。任何一步不成就**静默回退**并把这一帧按传 JPEG 发出去。
     *
     * 回退发生在这一帧之内，不让状态机看到一次失败：状态机的 `netFailures` 计数是
     * 「网络不好」的判据，把「端上推理没跑起来」记进去会触发一次没必要的重新探活，
     * 而那要 1.5 秒。
     */
    private fun recognizeByFeatures(
        extractor: FeatureExtractor,
        jpeg: ByteArray,
    ): RecognizeOutcome {
        val extracted = try {
            extractor.extract(jpeg)
        } catch (e: Throwable) {
            // Throwable 而不是 Exception：ORT 在 native 层出问题时抛的是 Error
            // （UnsatisfiedLinkError 之类），只 catch Exception 会让进程直接崩。
            noteFeatureFailure(FeatureFailure.INFER_FAILED, e.message)
            return client.recognize(jpeg)
        }
        if (extracted.features.isEmpty) {
            // 一个有效关键点都没有（白墙）。不发请求 —— 服务端只会回一次未命中，
            // 而那一次往返在 400ms 的节奏里是纯浪费。也不算失败。
            return RecognizeOutcome.NoMatch("no_features", 0)
        }
        return try {
            client.recognizeFeatures(
                FeaturesRequest.body(
                    extracted.features,
                    extracted.height,
                    extracted.width,
                )
            ).also { featurePath.onSuccess() }
        } catch (e: HttpFailure) {
            // 400 = 服务端不接受端上特征（后端是 orb、或者描述子校验没过）。那是永久性的，
            // 回退掉。其它错误（超时、5xx、401）是这条路和另一条路共有的，原样抛出去让
            // 状态机按网络问题处理 —— 那种情况下换成传 JPEG 一样会失败。
            if (e.status == 400) {
                noteFeatureFailure(FeatureFailure.SERVER_REJECTED, e.message)
                client.recognize(jpeg)
            } else {
                throw e
            }
        }
    }

    /**
     * 把模型和 ONNX 会话准备好。异步，最多同时排一次。
     *
     * 排一次就够：失败之后 [FeaturePathPolicy] 会把这条路关掉（[FeatureFailure.fatal]
     * 的那三种一次就关），于是 [recognize] 不再走到这里。没有这个闸门的话，模型下载
     * 的那两分钟里每 400ms 会排一个新的下载任务。
     */
    private fun prepareExtractorAsync() {
        if (cachedExtractor != null || !preparing.compareAndSet(false, true)) return
        featWork.execute {
            try {
                val outcome = ModelCache.Outcome()
                val model = modelCache.ensure(client, outcome)
                if (model == null) {
                    noteFeatureFailure(
                        outcome.failure ?: FeatureFailure.MODEL_UNAVAILABLE,
                        outcome.detail,
                    )
                    return@execute
                }
                cachedExtractor = try {
                    OnnxFeatureExtractor.open(model)
                } catch (e: Throwable) {
                    noteFeatureFailure(FeatureFailure.LOAD_FAILED, e.message)
                    null
                }
            } finally {
                preparing.set(false)
            }
        }
    }

    private fun noteFeatureFailure(kind: FeatureFailure, detail: String?) {
        Log.w(TAG, "端上提特征回退（$kind）：$detail")
        if (featurePath.onFailure(kind)) {
            main.post { controller.onFeatureFallback(featurePath.message()) }
        }
    }

    override fun loadTarget(hit: Hit) {
        val holder = ar ?: return
        net.execute {
            var fallbackReason: String? = null
            val target = try {
                targetLoader.load(hit) { fallbackReason = it }
            } catch (e: Throwable) {
                main.post {
                    // 401 走 onUnauthorized 而不是 onTargetFailed：后者的提示是
                    // 「这张照片的目标装不上」，会让人去查照片和 .imgdb，而真正的原因
                    // 是登录过期了（imgdb 与识别用的是同一个 token）。
                    if (unauthorized(e)) {
                        controller.onUnauthorized(e.message)
                    } else {
                        controller.onTargetFailed(hit.photoId, e.message)
                    }
                }
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
                        // 尺寸已经在 Matched 那里设过了（见 emit）
                        controller.onTargetLoaded(hit.photoId, fallbackReason != null)
                    }
                }
            }
        }
    }

    /**
     * 缓存优先（Phase 4）。
     *
     * 本地那份视频就是从同一个地址下下来的字节，画质完全一样，而且省掉一次
     * media 元数据 RTT 和整段流传输 —— 命中到出画会明显快一截。断网时它还是
     * 唯一能播的东西。
     *
     * 这里在主线程上摸了两下磁盘（文件存在 + 长度）。是刻意的：两个 stat 是
     * 微秒级，而为它开一趟线程切换会让「本地命中秒出画」这件事白白多一帧延迟。
     */
    override fun fetchMedia(hit: Hit) {
        val cached = cache.byId(hit.photoId)
        cache.localVideoUrl(hit.photoId)?.let { url ->
            controller.onMedia(
                hit.photoId,
                localMedia(url, cached?.videoBytes ?: 0L, cached?.videoDurationMs),
            )
            return
        }
        net.execute {
            try {
                val info = client.media(hit)
                main.post { controller.onMedia(hit.photoId, info) }
            } catch (e: Throwable) {
                main.post {
                    // 「没缓存 + 此刻没网」和「视频坏了」要分开报：前者用户联网就好，
                    // 后者不能。判据就是这次失败是不是网络层的。401 又是第三种（登录
                    // 过期），它必须排在最前面 —— 否则那条路会报「视频不可播」，
                    // 而用户会去 NAS 上找那个视频文件。
                    if (unauthorized(e)) {
                        controller.onUnauthorized(e.message)
                    } else if (offlineFailure(e)) {
                        controller.onMediaNotCached(hit.photoId)
                    } else {
                        controller.onMediaFailed(hit.photoId, e.message)
                    }
                }
            }
        }
    }

    /**
     * 把这张照片的原图与视频存进系统相册。全程在网络线程上跑，结果 post 回主线程。
     *
     * 原图走 `/v1/photo/<id>/ref` 而**不是** `refThumbUrl`：后者是缩略图，存下来是一张
     * 糊的，而且这个错误不报任何错 —— 用户要打开相册才发现。
     *
     * 视频优先用本地缓存那一份（离线命中时它本来就在磁盘上），省一趟下载。
     *
     * 部分成功是**正常结果**，不是异常：只有照片没有视频的条目本来就存在，而视频
     * 存失败（配额满）时那张照片已经进相册了，回滚它没有任何好处。所以返回的是
     * 「存了哪几样 + 出了什么错」，由界面如实说明。
     */
    fun saveToGallery(hit: Hit, title: String?, done: (SaveOutcome) -> Unit) {
        net.execute {
            val saver = MediaSaver(activity)
            var image: String? = null
            var video: String? = null
            val problems = mutableListOf<String>()

            try {
                val ref = client.downloadRef(hit.photoId)
                val name = SaveNaming.displayName(title, hit.photoId, ref.mime)
                    ?: throw MediaSaver.SaveFailed("不支持的图片类型：${ref.mime}")
                saver.save(MediaSaver.Kind.IMAGE, ref.bytes, ref.mime, name)
                image = name
            } catch (t: Throwable) {
                problems += "照片：${t.message ?: t.javaClass.simpleName}"
            }

            try {
                val bytes = videoBytesForSave(hit)
                if (bytes == null) {
                    // 没有视频不是错误，这张照片本来就可以只有图。
                } else {
                    val name = SaveNaming.displayName(title, hit.photoId, "video/mp4")
                        ?: throw MediaSaver.SaveFailed("不支持的视频类型")
                    saver.save(MediaSaver.Kind.VIDEO, bytes, "video/mp4", name)
                    video = name
                }
            } catch (t: Throwable) {
                problems += "视频：${t.message ?: t.javaClass.simpleName}"
            }

            val outcome = SaveOutcome(image, video, problems.toList())
            main.post { done(outcome) }
        }
    }

    /**
     * 相册要存的视频字节；这张照片没有视频时返回 null。
     *
     * 先看本地缓存：离线命中时视频本来就在磁盘上，再从网上拉一遍纯属浪费（而且在
     * 没网的现场会直接失败）。缓存没有才走 [PhotoArClient.downloadMedia] —— 复用它
     * 而不是自己拼 URL，是因为 media 通道的那套规则（absolute 时跳前缀、跳前缀时
     * 不带 Authorization）已经在那边，抄一遍迟早有一边是错的。
     */
    private fun videoBytesForSave(hit: Hit): ByteArray? {
        cache.videoFile(hit.photoId).takeIf { it.isFile && it.length() > 0 }?.let {
            return it.readBytes()
        }
        val info = client.media(hit)
        if (!info.playable) return null
        return client.downloadMedia(info)
    }

    private fun offlineFailure(e: Throwable): Boolean {
        val kind = (e as? HttpFailure)?.kind ?: return true // 连 HttpFailure 都不是：更底层的 IO
        return kind == NetErrorKind.TRANSPORT || kind == NetErrorKind.TIMEOUT
    }

    /**
     * 这次失败是不是「凭证不被接受」。
     *
     * 要看 `cause` 一层：`TargetLoader.LoadFailed` 会把 imgdb 与缩略图两次失败的原因
     * 拼成一句话再抛，原始的 [HttpFailure] 就藏在下面。只看最外层的话，token 过期在
     * 装目标那条路上会永远被报成「目标装不上」。
     */
    private fun unauthorized(e: Throwable): Boolean {
        var t: Throwable? = e
        var depth = 0
        while (t != null && depth < 4) {
            if ((t as? HttpFailure)?.kind == NetErrorKind.UNAUTHORIZED) return true
            t = t.cause
            depth++
        }
        return false
    }

    override fun releaseTarget() {
        renderer?.let {
            it.showVideo = false
            it.resetVideoFade()
        }
        videoTexture?.markStale()
        videoView?.visibility = android.view.View.GONE
        ar?.let { holder ->
            onGl {
                holder.clearTarget()
                // §15：clearTarget 把库清空了，本地多图库必须装回去 ——
                // 不装的话退出第一张照片之后离线识别就没了，而且是静默没的。
                localDb?.reinstall(holder)?.let { Log.w(TAG, "本地库装回失败：$it") }
            }
        }
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

    override fun requestLogin() {
        Log.i(TAG, "凭证失效，请求重新登录")
        onNeedLogin()
    }

    override fun emit(event: ScanEvent) {
        // lastSeenAt 是「最近 200 张」的排序键（CachePlanner.rank）。在线命中也要记：
        // 不记的话排序退化成入库时间，墙上那张天天扫的会被刚打印的一批顶掉，
        // 而「常扫照片离线可用」正是这份缓存的出口条件。
        if (event is ScanEvent.Matched) {
            cache.markSeen(event.hit.photoId, System.currentTimeMillis())
            // 四边形的物理尺寸在这里设，而不是在 loadTarget 里：离线命中根本不调
            // loadTarget（换库会重置 session），设在那边就会让离线命中的视频按上
            // 一张照片的尺寸去贴。Matched 在两条路上都会走到，而且更早。
            renderer?.setTarget(event.hit.printWidthM, event.hit.refAspect)
        }
        // 「这次离线命中的跟踪质量如何」只有这一层知道（装的是哪一份库）。状态机不该
        // 知道「多图库有两种」—— 它只管什么时候算命中。所以这一句在这里补，而不是让
        // 状态机多带一个参数。
        if (event is ScanEvent.Notice &&
            event.kind == NoticeKind.LOCAL_HIT &&
            event.detail == null
        ) {
            listener.onScanEvent(event.copy(detail = localHitText()))
            return
        }
        listener.onScanEvent(event)
    }

    /** 见 [NoticeKind.LOCAL_HIT]：两份库的跟踪质量不是一档，提示得说实话。 */
    private fun localHitText(): String = when (localDb?.source) {
        TargetDbSource.SERVER -> "离线识别（服务端预建库），跟踪质量与联网时相同"
        // 端上现建那份用的是 640px 缩略图，特征比原图少
        TargetDbSource.LOCAL -> "离线识别（本地缓存），贴合可能略有偏差"
        // 一份都没装还报出离线命中，只可能是库和索引不同步。别承诺质量。
        null -> "离线识别（本地缓存）"
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

/**
 * 一次「保存到相册」的结果。
 *
 * 三个字段都可能同时有值：**部分成功是正常结果，不是异常**。只有照片没有视频的
 * 条目本来就存在；而视频存失败（配额满、断网）时照片已经进相册了，回滚它对用户
 * 没有任何好处。所以这里如实带出"存了哪几样、哪几样没成"，由界面照实说。
 *
 * @param imageName 存进相册的照片文件名；null = 没存成或没存
 * @param videoName 同上，视频
 * @param problems 每一样失败的原因，给人看的中文
 */
data class SaveOutcome(
    val imageName: String?,
    val videoName: String?,
    val problems: List<String>,
) {
    val savedAnything: Boolean get() = imageName != null || videoName != null
}

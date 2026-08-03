package app.photoar.arview.gl

import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.util.Log
import app.photoar.arview.FrameStats
import app.photoar.arview.Geometry
import app.photoar.arview.PoseFilter
import app.photoar.arview.ar.ArSessionHolder
import app.photoar.arview.camera.FrameGrabber
import app.photoar.arview.media.VideoTexture
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.CameraNotAvailableException
import com.google.ar.core.exceptions.NotYetAvailableException
import javax.microedition.khronos.egl.EGLConfig
import javax.microedition.khronos.opengles.GL10

/**
 * GL 线程。每帧做四件事：推进 ARCore、画相机背景、把视频最新一帧搬进纹理、
 * 在照片上画那块视频。
 *
 * **线程边界**：所有状态判断都在主线程的 [app.photoar.arview.ScanController] 里，
 * 这里只负责「报告」和「按吩咐画」。跨线程的量都是 `@Volatile` 的单个字段，
 * 没有锁 —— 渲染线程被锁住一次就是一次掉帧。
 *
 * ## 贴合的两道处理，都在这里
 *
 * 画视频用的不是 ARCore 每帧给的原始位姿，中间隔着两层：
 *
 * - **[PoseFilter]**：对照片位姿做低通。压掉 ARCore 逐帧重估带来的毫米级抖动，
 *   而因为被滤的量在世界坐标里是个常量，这一层几乎不付延迟（详见那个类的注释）。
 * - **滑行窗口 [COAST_MS]**：FULL_TRACKING 断了不立刻判丢，继续用最后那个位姿贴一
 *   小会儿。斜视和 `getUpdatedTrackables` 的空档都是几十到几百毫秒的事，没有这层
 *   缓冲它们会被判成「丢失目标」。
 *
 * 相机位姿（view / projection）**一帧都不滤**。手机的运动必须零延迟地反映到画面上，
 * 滤它才是真的会「拖影」。
 */
class ArRenderer(
    private val ar: ArSessionHolder,
    private val videoTexture: VideoTexture,
    private val host: Host,
) : GLSurfaceView.Renderer {

    private companion object {
        const val TAG = "ArRenderer"
        const val NEAR = 0.05f
        const val FAR = 30f

        /**
         * 丢掉 FULL_TRACKING 之后还继续贴多久（毫秒）。
         *
         * 斜视时 ARCore 认不出图案（透视压缩 + 反光），会降级到 LAST_KNOWN_POSE；
         * `getUpdatedTrackables` 也不保证每帧都带上这张图。没有这个窗口，这些空档会被
         * 直接判成丢失 —— 视频暂停、弹提示、再恢复，表现为一动就闪。
         *
         * 上限存在的理由：滑行期间用的是世界跟踪推出来的位姿，而世界跟踪会漂；照片真被
         * 拿走时也只能靠超时发现（ARCore 不会告诉你「这张图不见了」，它只会一直报
         * LAST_KNOWN_POSE）。
         *
         * ## 2 秒 → 3 秒
         *
         * 原来那 2 秒是按「斜视 + `getUpdatedTrackables` 空档」定的，那些是几十到几百
         * 毫秒的事。但真机上用户的持握方式带来了一类更长的空档：**一只手拿着照片、手指
         * 压在边缘**，加上覆膜反光 —— ARCore 会连续好几百毫秒认不出图案，而挪一下手指或者
         * 换个角度避开反光又要一两秒。2 秒的窗口在这种情况下会反复到点，表现是视频每隔
         * 几秒暂停一下、弹一句「照片离开画面」，比彻底不显示更烦人。
         *
         * 3 秒仍然远短于「走开之后一块视频悬在空气里」会让人觉得出错的时长。而且它只影响
         * **已经贴上过**的那段 —— 还没贴上时归 `ScanController.TRACKING_HELP_MS` 管。
         *
         * **这仍然是真机上第一个要调的旋钮。** 还有「一动就闪」就往大调；出现「视频黏在
         * 空气里」就往小调。
         */
        const val COAST_MS = 3_000L
        const val COAST_NS = COAST_MS * 1_000_000L

        /** 贴不上时诊断日志的最小间隔（1 秒）。每帧一行会把 logcat 刷满。 */
        const val DIAG_INTERVAL_NS = 1_000_000_000L
    }

    /** 渲染线程 → 主线程。实现里必须 post 到主线程，不能直接碰状态机。 */
    interface Host {
        fun onTrackingChanged(photoId: String?, isTracking: Boolean)
        fun onFrameReady(seq: Long, jpeg: ByteArray)
        fun onFrameFailed(seq: Long)

        /** GL 资源就绪、相机纹理已分配，此时才能 `session.resume()`。 */
        fun onGlReady()

        /**
         * 给调试界面的一行。**不参与任何判定。**
         *
         * 走 Host 而不是只写 logcat：让用户在手机上截个图就能告诉我，比教他连 adb 快
         * 得多 —— 而现场出问题时手里只有手机。
         */
        fun onDiagnostic(line: String)
    }

    private val background = CameraBackground()
    private val quad = VideoQuad()
    private val grabber = FrameGrabber()

    private val model = FloatArray(16)
    private val view = FloatArray(16)
    private val projection = FloatArray(16)

    /** 主线程写：非 0 表示要抓一帧，值是 seq。 */
    @Volatile
    private var captureSeq: Long = 0

    /** 主线程写：视频的像素宽高比；0 表示还不知道。 */
    @Volatile
    var videoAspect: Float = 0f

    /** 主线程写：服务端给的印刷宽度与参考图比例。 */
    @Volatile
    private var printWidthM: Float = 0f

    @Volatile
    private var refAspect: Float? = null

    /** 主线程写：视频该不该出现在画面上。 */
    @Volatile
    var showVideo: Boolean = false

    /**
     * 上一次上报给状态机的 (目标名, 是否在跟踪)。
     *
     * **必须把名字一起比，不能只比布尔。** 扫描阶段 session 里装的是本地多图库，
     * [ArSessionHolder.trackedImage] 会返回**任意**一张正在被跟踪的图。只比布尔的
     * 后果是：先看到照片 A（上报一次），而 A 恰好不在端侧缓存索引里、或刚被拉黑，
     * 于是状态机留在 SCANNING；此时镜头转向照片 B，B 被跟踪、布尔仍然是 true ——
     * **不上报**，B 的离线命中就永远不会触发，直到跟踪先掉成 false 再回来。
     * 用户看到的现象是"这张照片扫不出来"，而日志里一切正常。
     */
    private var lastReported: Pair<String?, Boolean>? = null
    private var lastQuadW = 0f
    private var lastQuadH = 0f
    private var firstVideoFrameAt = 0L

    /**
     * 位姿低通。**只在 GL 线程上碰。** 主线程要让它复位得走 [targetGen]。
     *
     * 滤的是照片在世界坐标里的位姿（`centerPose`），不是相机位姿 —— view/projection
     * 一帧都不滤，所以手机动起来毫无延迟。理由写在 [PoseFilter] 的类注释里。
     */
    private val poseFilter = PoseFilter()

    /** 正在贴的那张图的名字。null = 现在没有可贴的位姿。GL 线程独占。 */
    private var activeName: String? = null

    /** 最近一次 FULL_TRACKING 的时刻（nanoTime）。0 = 还没有过。GL 线程独占。 */
    private var lastFullAtNs = 0L

    /**
     * ARCore 自己量的物理尺寸。滑行期间没有 image 可问，所以缓存下来。
     *
     * 这两个数是四边形大小的**首选**来源，不是兜底 —— 理由在 [Geometry.quadSize]。
     */
    private var lastExtentX = 0f
    private var lastExtentZ = 0f

    /** 打印宽度与 ARCore extentX 的对比只在换目标后报一次，不然每帧一行日志。 */
    private var widthCheckDone = false

    /** 上一次打诊断的时刻（nanoTime）。0 = 还没打过。GL 线程独占。 */
    private var lastDiagNs = 0L

    /**
     * 主线程写：要不要往调试界面报东西。
     *
     * 关着的时候连字符串都不拼（见 [FrameStats.frame] 的注释）。这不是省内存 ——
     * 这些行是**在渲染线程上**拼的，而它每 16.7ms 就要交一帧。
     */
    @Volatile
    var diagnostics: Boolean = false

    /** GL 帧耗时。只在 GL 线程上碰。 */
    private val stats = FrameStats()

    private val rawT = FloatArray(3)
    private val rawQ = FloatArray(4)

    fun requestCapture(seq: Long) {
        captureSeq = seq
    }

    /**
     * 换目标时**不碰** [poseFilter]。
     *
     * 两个理由，缺一条都不成立：
     *
     * 1. 线程。这里是主线程，滤波器是 GL 线程每帧读写的对象，内部十几个字段。跨线程
     *    reset 会被读成半新半旧的位姿 —— 视频飞到一个不存在的地方。本文件的约定是
     *    跨线程只传 `@Volatile` 的单个字段。
     * 2. 不需要。GL 线程自己按**图名**复位（见 [onDrawFrame]），换目标必然换名字。
     *    曾经加过一个 `@Volatile` 版本号来通知，那是多余的，而且有害：它会在换目标
     *    那一帧多报一次「丢失」，离线命中路径因此白等一两帧才起播。
     */
    fun setTarget(printWidthM: Float, refAspect: Float?) {
        this.printWidthM = printWidthM
        this.refAspect = refAspect
        lastQuadW = 0f
        lastQuadH = 0f
    }

    /** 换视频/退出目标时调，让淡入从头再来。 */
    fun resetVideoFade() {
        firstVideoFrameAt = 0L
    }

    override fun onSurfaceCreated(gl: GL10?, config: EGLConfig?) {
        GLES20.glClearColor(0f, 0f, 0f, 1f)
        background.createOnGlThread()
        quad.createOnGlThread()
        videoTexture.createOnGlThread()
        ar.cameraTextureId = background.textureId
        host.onGlReady()
    }

    override fun onSurfaceChanged(gl: GL10?, width: Int, height: Int) {
        GLES20.glViewport(0, 0, width, height)
        // 会话必须知道视口，否则 transformCoordinates2d 给出的纹理坐标是旧方向的
        ar.session?.setDisplayGeometry(0, width, height)
    }

    /**
     * 只做计时，正事全在 [drawFrame] 里。
     *
     * 拆成两个方法是因为 [drawFrame] 有六处提前 return（会话没建好、相机不可用、
     * 没在跟踪、视频还没帧…），而计时要**每条路都算上** —— 恰恰是「早早 return 的
     * 那些帧」能证明慢的不是渲染。用 try/finally 而不是在每个 return 前补一句：
     * 补漏一处，那一路就从统计里消失，而消失的方向永远是「看起来更快」。
     *
     * 计时不含 `session.update()` 之外的等待吗？含。`updateMode = BLOCKING` 会让
     * update 卡到下一帧相机图像到达为止，所以这里量出来的 fps 就是相机的实际帧率，
     * 而「均」里包含那段等待。判断卡不卡要看**峰值**和卡帧数，不是均值。
     */
    override fun onDrawFrame(gl: GL10?) {
        if (!diagnostics) {
            drawFrame()
            return
        }
        val startNs = System.nanoTime()
        try {
            drawFrame()
        } finally {
            val endNs = System.nanoTime()
            stats.frame(endNs - startNs, endNs)?.let { host.onDiagnostic(it) }
        }
    }

    private fun drawFrame() {
        GLES20.glClear(GLES20.GL_COLOR_BUFFER_BIT or GLES20.GL_DEPTH_BUFFER_BIT)
        val session = ar.session ?: return

        val frame = try {
            session.update()
        } catch (e: CameraNotAvailableException) {
            Log.w(TAG, "相机不可用", e)
            return
        } catch (e: Throwable) {
            Log.w(TAG, "session.update 失败", e)
            return
        }

        background.updateTexCoords(frame)
        background.draw()

        maybeCapture(frame)

        val tracked = ar.trackedImage(frame)
        val nowNs = System.nanoTime()

        // 换图（含首次锁定）只认 FULL 的那一帧。
        //
        // 不认 LAST_KNOWN 是有意的：扫描阶段 session 里是本地多图库，画面里可能同时
        // 有两张照片，其中一张只有 LAST_KNOWN。让它抢走 activeName 会把正在滑行的
        // 那张顶掉，于是两张互相打断，谁都贴不稳。
        if (tracked != null && tracked.full && tracked.image.name != activeName) {
            dropPose()
            activeName = tracked.image.name
        }

        if (tracked != null && tracked.full && tracked.image.name == activeName) {
            val pose = tracked.image.centerPose
            pose.getTranslation(rawT, 0)
            pose.getRotationQuaternion(rawQ, 0)
            poseFilter.update(rawT, rawQ, nowNs)
            lastFullAtNs = nowNs
            lastExtentX = tracked.image.extentX
            lastExtentZ = tracked.image.extentZ
            maybeCheckWidth(tracked.image)
        }

        // 滑行：FULL 丢了也继续贴，但两道闸都得成立 ——
        //  1. 还在时限内（[COAST_MS]）
        //  2. 相机的世界跟踪本身没丢。丢了的话「照片在世界里的位姿」这个前提就不成立，
        //     此时继续贴就是原注释担心的「贴在空气上」。
        val worldOk = frame.camera.trackingState == TrackingState.TRACKING
        val effective = poseFilter.hasPose && worldOk &&
            lastFullAtNs != 0L && (nowNs - lastFullAtNs) <= COAST_NS

        // 贴不上时每秒打一行 ARCore 的原话。理由见 `ArSessionHolder.diagnose` ——
        // 一句话版：「贴不上」有三个修法毫不相干的原因，而不问 ARCore 就只能猜，
        // 而我已经猜错过两次。
        //
        // 只在**没贴上**时打，而且限一秒一行：贴上之后这行没有信息量，而每帧一行会把
        // logcat 刷满，反而看不到别的。
        if (!effective && showVideo && diagnostics) {
            if (nowNs - lastDiagNs >= DIAG_INTERVAL_NS) {
                lastDiagNs = nowNs
                val line = ar.diagnose(frame)
                Log.i(TAG, "贴不上：$line")
                host.onDiagnostic(line)
            }
        } else if (effective) {
            lastDiagNs = 0L
        }

        // 上报的名字必须在 dropPose 之前取。
        val reportName = if (effective) activeName else null
        val now = reportName to effective
        if (lastReported != now) {
            lastReported = now
            host.onTrackingChanged(reportName, effective)
        }
        if (!effective) {
            dropPose()
            return
        }

        // 视频每帧都搬，哪怕现在不显示 —— 不搬的话 SurfaceTexture 的队列会满，
        // 解码器随后卡住，等到要显示时是一段冻结画面。
        val gotNew = videoTexture.updateIfAvailable()
        if (gotNew && firstVideoFrameAt == 0L) firstVideoFrameAt = System.nanoTime()
        if (!showVideo || !videoTexture.hasFrame) return

        // 尺度优先取 ARCore 量的 extentX（与 centerPose 同一个尺度），申报的
        // printWidthM 只在它还没给出来时垫一下；未知时 printWidthM 就是 0，合法。
        val photo = Geometry.quadSize(lastExtentX, lastExtentZ, printWidthM, refAspect) ?: return
        // 视频按自己的比例装进照片那块矩形：不裁、不变形，露出来的是照片本身。
        // 理由见 [Geometry.videoQuad] —— 一句话版：比例对不上时该让的是「填满」，
        // 不是「画面内容」。
        val size = Geometry.videoQuad(photo, videoAspect)
        if (size.widthM != lastQuadW || size.heightM != lastQuadH) {
            lastQuadW = size.widthM
            lastQuadH = size.heightM
            quad.setSize(size.widthM, size.heightM)
        }

        // 滤波后的位姿，不是 `centerPose.toMatrix()`。滑行帧里根本没有 image 可问。
        poseFilter.toMatrix(model)
        val camera = frame.camera
        camera.getViewMatrix(view, 0)
        camera.getProjectionMatrix(projection, 0, NEAR, FAR)

        val elapsedMs = (System.nanoTime() - firstVideoFrameAt) / 1_000_000L
        quad.draw(
            model = model,
            view = view,
            projection = projection,
            textureId = videoTexture.textureId,
            stMatrix = videoTexture.stMatrix,
            uv = Geometry.FULL_UV,
            alpha = Geometry.fadeAlpha(elapsedMs),
        )
    }

    /** 放掉当前位姿：滤波器、滑行计时、缓存的比例全清。只在 GL 线程调。 */
    private fun dropPose() {
        activeName = null
        lastFullAtNs = 0L
        lastExtentX = 0f
        lastExtentZ = 0f
        widthCheckDone = false
        poseFilter.reset()
    }

    /**
     * 锁上目标之后报一次尺寸来源，只报一次（不然每帧一行）。
     *
     * 这行日志是排查「贴不上」时唯一能把两类原因分开的东西：
     *
     * - 两个数**接近** → 库里烘了宽度，ARCore 在照抄它。此时如果画面上大小还是不对，
     *   说明烘进去的那个数和实际照片不符 —— 要么重新入库（不填宽度，让 ARCore 自己
     *   量），要么填对。
     * - 申报是 0 → 未知，ARCore 在自己估。`extentX` 是它的**测量值**，会随手机移动
     *   收敛，所以开头一两秒大小微调是正常的，不是 bug。
     * - 两个数**差很多**且申报非 0 → ARCore 没在用我们烘的值。最可能是这张目标走了
     *   缩略图降级路径（`ArSessionHolder.loadTargetFromBitmap`）而那边没传宽度。
     */
    private fun maybeCheckWidth(image: com.google.ar.core.AugmentedImage) {
        if (widthCheckDone) return
        widthCheckDone = true
        val declared = printWidthM
        val declaredText =
            if (declared > 0f) "${"%.1f".format(declared * 100f)}cm" else "未知(ARCore 自己量)"
        val line = "贴上了 ${image.name.take(8)} 申报$declaredText " +
            "ARCore ${"%.1f".format(image.extentX * 100f)}×" +
            "${"%.1f".format(image.extentZ * 100f)}cm"
        Log.i(TAG, "$line（四边形用的是 ARCore 那个）")
        if (diagnostics) host.onDiagnostic(line)
    }

    private fun maybeCapture(frame: com.google.ar.core.Frame) {
        val seq = captureSeq
        if (seq == 0L) return
        captureSeq = 0
        var image: android.media.Image? = null
        try {
            image = frame.acquireCameraImage()
            // 这一段是 GL 线程上唯一与渲染无关的重活（YUV→NV21→JPEG，1280×960）。
            // 单独计时的理由见 [FrameStats]：混进每帧均值里它会被 24 帧摊薄成看不见。
            val t0 = if (diagnostics) System.nanoTime() else 0L
            val jpeg = grabber.toJpeg(image)
            if (diagnostics) stats.grab(System.nanoTime() - t0)
            host.onFrameReady(seq, jpeg)
        } catch (e: NotYetAvailableException) {
            // 这一帧的 CPU 图像还没到，下一轮再试；不算失败
            host.onFrameFailed(seq)
        } catch (e: Throwable) {
            Log.w(TAG, "抓帧失败", e)
            host.onFrameFailed(seq)
        } finally {
            // 必须关：acquireCameraImage 有并发上限，漏一个就再也拿不到帧
            image?.close()
        }
    }
}

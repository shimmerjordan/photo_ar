package app.photoar.arview.gl

import android.opengl.GLES20
import android.opengl.GLSurfaceView
import android.util.Log
import app.photoar.arview.Geometry
import app.photoar.arview.ar.ArSessionHolder
import app.photoar.arview.camera.FrameGrabber
import app.photoar.arview.media.VideoTexture
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
    }

    /** 渲染线程 → 主线程。实现里必须 post 到主线程，不能直接碰状态机。 */
    interface Host {
        fun onTrackingChanged(photoId: String?, isTracking: Boolean)
        fun onFrameReady(seq: Long, jpeg: ByteArray)
        fun onFrameFailed(seq: Long)

        /** GL 资源就绪、相机纹理已分配，此时才能 `session.resume()`。 */
        fun onGlReady()
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

    private var lastReportedTracking: Boolean? = null
    private var lastQuadW = 0f
    private var lastQuadH = 0f
    private var firstVideoFrameAt = 0L

    fun requestCapture(seq: Long) {
        captureSeq = seq
    }

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

    override fun onDrawFrame(gl: GL10?) {
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

        val image = ar.trackedImage(frame)
        val isTracking = image != null
        if (lastReportedTracking != isTracking) {
            lastReportedTracking = isTracking
            host.onTrackingChanged(image?.name, isTracking)
        }
        if (image == null) return

        // 视频每帧都搬，哪怕现在不显示 —— 不搬的话 SurfaceTexture 的队列会满，
        // 解码器随后卡住，等到要显示时是一段冻结画面。
        val gotNew = videoTexture.updateIfAvailable()
        if (gotNew && firstVideoFrameAt == 0L) firstVideoFrameAt = System.nanoTime()
        if (!showVideo || !videoTexture.hasFrame) return

        val width = printWidthM
        if (width <= 0f) return
        val arcoreAspect = if (image.extentZ > 0f) image.extentX / image.extentZ else null
        val size = try {
            Geometry.printedSize(width, refAspect, arcoreAspect)
        } catch (e: IllegalArgumentException) {
            return
        }
        if (size.widthM != lastQuadW || size.heightM != lastQuadH) {
            lastQuadW = size.widthM
            lastQuadH = size.heightM
            quad.setSize(size.widthM, size.heightM)
        }

        image.centerPose.toMatrix(model, 0)
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
            uv = Geometry.fillCropUv(size.widthM / size.heightM, videoAspect),
            alpha = Geometry.fadeAlpha(elapsedMs),
        )
    }

    private fun maybeCapture(frame: com.google.ar.core.Frame) {
        val seq = captureSeq
        if (seq == 0L) return
        captureSeq = 0
        var image: android.media.Image? = null
        try {
            image = frame.acquireCameraImage()
            host.onFrameReady(seq, grabber.toJpeg(image))
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

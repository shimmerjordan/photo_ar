package app.photoar.arview.camera

import android.content.Context
import android.graphics.ImageFormat
import android.hardware.camera2.CameraCaptureSession
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraDevice
import android.hardware.camera2.CameraManager
import android.hardware.camera2.CaptureRequest
import android.hardware.camera2.params.StreamConfigurationMap
import android.media.ImageReader
import android.os.Handler
import android.os.HandlerThread
import android.util.Log
import android.util.Size
import android.view.Surface
import app.photoar.arview.Frames
import java.util.concurrent.atomic.AtomicLong

/**
 * 无 ARCore 机型的相机源（§11.9 / §13 的兜底路径）。
 *
 * 没有 ARCore 就没有 `Frame.acquireCameraImage()`，但「扫到照片就能看视频」这件
 * 事不该因为机型不支持 AR 而消失 —— 只是没有贴合效果，识别出来之后全屏播。
 * 所以这里用 Camera2 自己开一路预览 + 一路 YUV，喂给同一个状态机。
 *
 * 不引 CameraX：需要的只有「一个预览 Surface + 一个 ImageReader」，Camera2 直写
 * 大约就是这么多代码，而 CameraX 会带进来一整套 lifecycle/executor 依赖。
 */
class Camera2Source(
    private val context: Context,
    private val host: Host,
) {

    private companion object {
        const val TAG = "Camera2Source"
    }

    interface Host {
        fun onFrameReady(seq: Long, jpeg: ByteArray)
        fun onFrameFailed(seq: Long)
        fun onCameraError(message: String)
    }

    private val grabber = FrameGrabber()
    private val pending = AtomicLong(0)

    private var thread: HandlerThread? = null
    private var handler: Handler? = null
    private var device: CameraDevice? = null
    private var session: CameraCaptureSession? = null
    private var reader: ImageReader? = null
    private var previewSurface: Surface? = null

    /** 预览分辨率，供界面按比例摆放 SurfaceView。开起来之后才有值。 */
    var previewSize: Size? = null
        private set

    fun requestCapture(seq: Long) {
        pending.set(seq)
    }

    /** 调用方要先确认有相机权限。 */
    fun start(preview: Surface) {
        if (device != null) return
        previewSurface = preview
        val t = HandlerThread("photoar-cam").apply { start() }
        thread = t
        handler = Handler(t.looper)
        val mgr = context.getSystemService(Context.CAMERA_SERVICE) as CameraManager
        val id = pickBackCamera(mgr) ?: run {
            host.onCameraError("找不到后置摄像头")
            return
        }
        val size = pickSize(mgr, id)
        previewSize = size
        val r = ImageReader.newInstance(size.width, size.height, ImageFormat.YUV_420_888, 2)
        r.setOnImageAvailableListener({ onImage(it) }, handler)
        reader = r
        try {
            @Suppress("MissingPermission")
            mgr.openCamera(id, stateCallback, handler)
        } catch (e: SecurityException) {
            host.onCameraError("没有相机权限")
        } catch (e: Exception) {
            host.onCameraError(e.message ?: "打开相机失败")
        }
    }

    fun stop() {
        session?.let { runCatching { it.close() } }
        session = null
        device?.let { runCatching { it.close() } }
        device = null
        reader?.close()
        reader = null
        previewSurface = null
        thread?.quitSafely()
        thread = null
        handler = null
        pending.set(0)
    }

    private val stateCallback = object : CameraDevice.StateCallback() {
        override fun onOpened(camera: CameraDevice) {
            device = camera
            configure(camera)
        }

        override fun onDisconnected(camera: CameraDevice) {
            camera.close()
            device = null
        }

        override fun onError(camera: CameraDevice, error: Int) {
            camera.close()
            device = null
            host.onCameraError("相机错误 $error")
        }
    }

    private fun configure(camera: CameraDevice) {
        val preview = previewSurface ?: return
        val yuv = reader?.surface ?: return
        val targets = listOf(preview, yuv)
        val cb = object : CameraCaptureSession.StateCallback() {
            override fun onConfigured(s: CameraCaptureSession) {
                session = s
                val req = camera.createCaptureRequest(CameraDevice.TEMPLATE_PREVIEW).apply {
                    addTarget(preview)
                    addTarget(yuv)
                    // 照片是平的、距离几十厘米，连续对焦必须开
                    set(
                        CaptureRequest.CONTROL_AF_MODE,
                        CaptureRequest.CONTROL_AF_MODE_CONTINUOUS_PICTURE,
                    )
                }
                runCatching { s.setRepeatingRequest(req.build(), null, handler) }
                    .onFailure { host.onCameraError(it.message ?: "启动预览失败") }
            }

            override fun onConfigureFailed(s: CameraCaptureSession) {
                host.onCameraError("相机会话配置失败")
            }
        }
        @Suppress("DEPRECATION")
        runCatching { camera.createCaptureSession(targets, cb, handler) }
            .onFailure { host.onCameraError(it.message ?: "创建相机会话失败") }
    }

    private fun onImage(r: ImageReader) {
        // 一定要取走并关掉，哪怕这一帧不用 —— 队列满了预览就停
        val image = try {
            r.acquireLatestImage()
        } catch (e: Exception) {
            null
        } ?: return
        val seq = pending.getAndSet(0)
        try {
            if (seq != 0L) host.onFrameReady(seq, grabber.toJpeg(image))
        } catch (e: Throwable) {
            Log.w(TAG, "抓帧失败", e)
            if (seq != 0L) host.onFrameFailed(seq)
        } finally {
            image.close()
        }
    }

    private fun pickBackCamera(mgr: CameraManager): String? {
        for (id in mgr.cameraIdList) {
            val facing = mgr.getCameraCharacteristics(id)
                .get(CameraCharacteristics.LENS_FACING)
            if (facing == CameraCharacteristics.LENS_FACING_BACK) return id
        }
        return mgr.cameraIdList.firstOrNull()
    }

    /** 挑最接近「长边 640」的档位：再大只是把上行流量做大，识别不会更准。 */
    private fun pickSize(mgr: CameraManager, id: String): Size {
        val map = mgr.getCameraCharacteristics(id)
            .get(CameraCharacteristics.SCALER_STREAM_CONFIGURATION_MAP)
            ?: return Size(640, 480)
        return bestSize(map) ?: Size(640, 480)
    }

    private fun bestSize(map: StreamConfigurationMap): Size? =
        map.getOutputSizes(ImageFormat.YUV_420_888)
            ?.filter { it.width >= 320 && it.height >= 240 }
            ?.minByOrNull { kotlin.math.abs(maxOf(it.width, it.height) - Frames.LONG_EDGE) }
}

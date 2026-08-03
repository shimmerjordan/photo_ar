package app.photoar.arview.camera

import android.graphics.ImageFormat
import android.graphics.Rect
import android.graphics.YuvImage
import android.media.Image
import android.util.Log
import app.photoar.arview.Frames
import java.io.ByteArrayOutputStream

/**
 * YUV_420_888 → NV21 → JPEG。
 *
 * 走 [YuvImage.compressToJpeg] 而不是 `Bitmap`：中间少一次 ARGB_8888 的分配
 * （640x480 就是 1.2MB），而且 NV21 → JPEG 是 skia 里的直通路径。§13 的预算是
 * 每 400ms 一帧、约 50KB，这条路能在 10ms 量级跑完，不会挤到渲染。
 *
 * 缓冲区复用：抽帧是每 400ms 一次的稳定节奏，每次新分配 460KB 的 NV21 数组会
 * 让 GC 有规律地抖动，而抖动正好落在渲染线程上。
 *
 * **不做缩放。** `YuvImage` 只能裁不能缩，而裁会改变视场角、把照片裁出画面。
 * 所以尺寸只能在源头定，而且两条路都必须显式定：ARCore 默认给的 CPU 图像是
 * 640x480（[app.photoar.arview.ar.ArSessionHolder.applyCameraConfig] 把它挑到
 * ≥ `Frames.LONG_EDGE`），Camera2 兜底路径则是 `Frames.pickCameraSize`。
 * 这里不缩放意味着**源头挑错了这里救不回来** —— 识别率的上限就是相机给的像素。
 * 真拿到超大帧只是包大一点，识别照样能过，所以只记一条日志。
 *
 * 不做旋转：服务端用的是旋转不变的局部特征 + 单应性估计（见 recognizer.py），
 * 帧转不转对命中率没有影响，转一次反而白花 CPU。
 */
class FrameGrabber {

    private companion object {
        const val TAG = "FrameGrabber"
    }

    private var nv21 = ByteArray(0)
    private val jpegOut = ByteArrayOutputStream(1 shl 16)
    private var warnedSize = false

    /**
     * 把一帧压成 JPEG。**不**关闭 [image]，由调用方负责 —— ARCore 的
     * `acquireCameraImage()` 有获取上限，漏一个就再也拿不到帧。
     */
    fun toJpeg(image: Image): ByteArray {
        require(image.format == ImageFormat.YUV_420_888) {
            "只支持 YUV_420_888，收到 ${image.format}"
        }
        val w = image.width
        val h = image.height
        if (!warnedSize && maxOf(w, h) > Frames.LONG_EDGE * 3 / 2) {
            warnedSize = true
            Log.w(TAG, "相机 CPU 图像 ${w}x$h 比预期（长边 ${Frames.LONG_EDGE}）大，上行会变大")
        }
        val y = image.planes[0]
        val u = image.planes[1]
        val v = image.planes[2]

        val need = Frames.nv21Size(w, h)
        if (nv21.size < need) nv21 = ByteArray(need)
        Frames.toNv21(
            width = w,
            height = h,
            y = y.buffer,
            yRowStride = y.rowStride,
            u = u.buffer,
            uRowStride = u.rowStride,
            uPixelStride = u.pixelStride,
            v = v.buffer,
            vRowStride = v.rowStride,
            vPixelStride = v.pixelStride,
            out = nv21,
        )

        jpegOut.reset()
        YuvImage(nv21, ImageFormat.NV21, w, h, null)
            .compressToJpeg(Rect(0, 0, w, h), Frames.JPEG_QUALITY, jpegOut)
        return jpegOut.toByteArray()
    }
}

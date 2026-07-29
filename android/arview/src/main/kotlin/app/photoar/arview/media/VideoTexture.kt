package app.photoar.arview.media

import android.graphics.SurfaceTexture
import android.opengl.Matrix
import android.view.Surface
import app.photoar.arview.gl.GlUtil

/**
 * 视频的外部纹理。ExoPlayer 往 [surface] 里解码，GL 线程每帧调
 * [updateIfAvailable] 把最新一帧搬到纹理上。
 *
 * `SurfaceTexture` 的变换矩阵不能省：解码器输出的行序、裁切、旋转都编码在它
 * 里面，不套它最常见的表现是视频上下颠倒。
 *
 * 只在 GL 线程上创建和销毁 —— `attachToGLContext` 那套跨线程 API 在不同厂商
 * 的驱动上表现不一致，绕开它最省事。
 */
class VideoTexture {

    var textureId: Int = -1
        private set

    var surface: Surface? = null
        private set

    private var surfaceTexture: SurfaceTexture? = null

    /** 每帧读；[VideoQuad] 直接把它当 uniform 传进着色器。 */
    val stMatrix = FloatArray(16).also { Matrix.setIdentityM(it, 0) }

    /** 至少收到过一帧。没收到之前画出来是黑块，宁可不画。 */
    var hasFrame: Boolean = false
        private set

    fun createOnGlThread() {
        if (textureId >= 0) return
        textureId = GlUtil.createExternalTexture()
        val st = SurfaceTexture(textureId)
        surfaceTexture = st
        surface = Surface(st)
    }

    /** @return 这一帧是不是新的（用来决定要不要重画）。 */
    fun updateIfAvailable(): Boolean {
        val st = surfaceTexture ?: return false
        return try {
            st.updateTexImage()
            st.getTransformMatrix(stMatrix)
            hasFrame = true
            true
        } catch (e: RuntimeException) {
            // 纹理已经被销毁但渲染线程还跑了一帧，忽略
            false
        }
    }

    /** 换视频时清掉「有帧」的记忆，否则上一支视频的最后一帧会闪一下。 */
    fun markStale() {
        hasFrame = false
    }

    fun release() {
        surface?.release()
        surface = null
        surfaceTexture?.release()
        surfaceTexture = null
        if (textureId >= 0) {
            val ids = intArrayOf(textureId)
            android.opengl.GLES20.glDeleteTextures(1, ids, 0)
            textureId = -1
        }
        hasFrame = false
    }
}

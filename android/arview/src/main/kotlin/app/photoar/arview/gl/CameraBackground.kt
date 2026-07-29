package app.photoar.arview.gl

import android.opengl.GLES20
import com.google.ar.core.Coordinates2d
import com.google.ar.core.Frame

/**
 * 把相机图像铺满屏幕。
 *
 * 纹理坐标不是写死的：屏幕方向、相机传感器方向、预览分辨率三者的组合决定了
 * 图像该怎么摆，只有 ARCore 知道。所以顶点用固定的 NDC 全屏四边形，纹理坐标
 * 每次 `displayGeometryChanged` 之后让 `frame.transformCoordinates2d` 算。
 * 自己推这个矩阵是横屏一转就画面躺倒的经典来源。
 */
class CameraBackground {

    private companion object {
        val NDC_QUAD = floatArrayOf(
            -1f, -1f,
            +1f, -1f,
            -1f, +1f,
            +1f, +1f,
        )

        const val VERTEX_SRC = """
            attribute vec4 aPosition;
            attribute vec2 aTexCoord;
            varying vec2 vTexCoord;
            void main() {
                gl_Position = aPosition;
                vTexCoord = aTexCoord;
            }
        """

        const val FRAGMENT_SRC = """
            #extension GL_OES_EGL_image_external : require
            precision mediump float;
            varying vec2 vTexCoord;
            uniform samplerExternalOES uTexture;
            void main() {
                gl_FragColor = texture2D(uTexture, vTexCoord);
            }
        """
    }

    var textureId: Int = -1
        private set

    private var program = 0
    private var aPosition = 0
    private var aTexCoord = 0
    private var uTexture = 0

    private val quadBuf = GlUtil.floatBuffer(NDC_QUAD)
    private val texBuf = GlUtil.floatBuffer(FloatArray(8))

    fun createOnGlThread() {
        textureId = GlUtil.createExternalTexture()
        program = GlUtil.compile(VERTEX_SRC, FRAGMENT_SRC)
        aPosition = GLES20.glGetAttribLocation(program, "aPosition")
        aTexCoord = GLES20.glGetAttribLocation(program, "aTexCoord")
        uTexture = GLES20.glGetUniformLocation(program, "uTexture")
    }

    /** 每帧调用；只有在 ARCore 说几何变了的时候才真去重算。 */
    fun updateTexCoords(frame: Frame) {
        if (!frame.hasDisplayGeometryChanged() && texBuf.get(0) != 0f) return
        quadBuf.position(0)
        texBuf.position(0)
        frame.transformCoordinates2d(
            Coordinates2d.OPENGL_NORMALIZED_DEVICE_COORDINATES,
            quadBuf,
            Coordinates2d.TEXTURE_NORMALIZED,
            texBuf,
        )
        quadBuf.position(0)
        texBuf.position(0)
    }

    fun draw() {
        if (program == 0) return
        // 背景铺满整屏，深度测试和写入都关掉，省一次无意义的 clear 争用
        GLES20.glDisable(GLES20.GL_DEPTH_TEST)
        GLES20.glDepthMask(false)
        GLES20.glUseProgram(program)
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GlUtil.TEXTURE_EXTERNAL_OES, textureId)
        GLES20.glUniform1i(uTexture, 0)

        quadBuf.position(0)
        texBuf.position(0)
        GLES20.glVertexAttribPointer(aPosition, 2, GLES20.GL_FLOAT, false, 0, quadBuf)
        GLES20.glVertexAttribPointer(aTexCoord, 2, GLES20.GL_FLOAT, false, 0, texBuf)
        GLES20.glEnableVertexAttribArray(aPosition)
        GLES20.glEnableVertexAttribArray(aTexCoord)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)
        GLES20.glDisableVertexAttribArray(aPosition)
        GLES20.glDisableVertexAttribArray(aTexCoord)

        GLES20.glDepthMask(true)
        GLES20.glEnable(GLES20.GL_DEPTH_TEST)
        GlUtil.checkError("CameraBackground.draw")
    }
}

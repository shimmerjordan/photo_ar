package app.photoar.arview.gl

import android.opengl.GLES20
import android.opengl.Matrix
import app.photoar.arview.Geometry

/**
 * 贴在照片上的那块视频。
 *
 * 顶点在**照片自己的局部坐标系**里：ARCore 的 `AugmentedImage.getCenterPose()`
 * 把图片放在局部 X-Z 平面上，+Y 是图片法线（朝外），图片的「上」是 -Z。所以四个
 * 角是 (±w/2, 0, ±h/2)，模型矩阵直接用 centerPose，不用自己拼旋转。
 *
 * §11.8 的两件事都在片元着色器里：
 * - **羽化**：边缘那一圈把 alpha 拉到 0，视频与照片的接缝就不是一条硬线；
 *   照片印刷有白边、AR 位姿也有毫米级抖动，硬边会把这两样都放大成「贴歪了」。
 * - **淡入**：整体 alpha 从 0 升到 1，避免命中瞬间「啪」地出现一块画面。
 */
class VideoQuad {

    private companion object {
        const val VERTEX_SRC = """
            uniform mat4 uMvp;
            uniform vec2 uUvScale;
            uniform vec2 uUvOffset;
            uniform mat4 uStMatrix;
            attribute vec4 aPosition;
            attribute vec2 aQuadUv;
            varying vec2 vQuadUv;
            varying vec2 vTexUv;
            void main() {
                gl_Position = uMvp * aPosition;
                vQuadUv = aQuadUv;
                vec2 cropped = aQuadUv * uUvScale + uUvOffset;
                vTexUv = (uStMatrix * vec4(cropped, 0.0, 1.0)).xy;
            }
        """

        const val FRAGMENT_SRC = """
            #extension GL_OES_EGL_image_external : require
            precision mediump float;
            uniform samplerExternalOES uTexture;
            uniform float uAlpha;
            uniform float uFeather;
            varying vec2 vQuadUv;
            varying vec2 vTexUv;
            void main() {
                vec3 rgb = texture2D(uTexture, vTexUv).rgb;
                // 到最近边的距离（0 在边上，0.5 在中心）
                vec2 d = min(vQuadUv, 1.0 - vQuadUv);
                float edge = min(d.x, d.y);
                float feather = smoothstep(0.0, max(uFeather, 0.0001), edge);
                gl_FragColor = vec4(rgb, uAlpha * feather);
            }
        """
    }

    private var program = 0
    private var aPosition = 0
    private var aQuadUv = 0
    private var uMvp = 0
    private var uTexture = 0
    private var uAlpha = 0
    private var uFeather = 0
    private var uUvScale = 0
    private var uUvOffset = 0
    private var uStMatrix = 0

    private val verts = FloatArray(12)
    private val vertBuf = GlUtil.floatBuffer(FloatArray(12))

    /** UV 与顶点一一对应，见下面 setSize 里的角点顺序。 */
    private val uvBuf = GlUtil.floatBuffer(
        floatArrayOf(
            0f, 0f, // 左下
            1f, 0f, // 右下
            0f, 1f, // 左上
            1f, 1f, // 右上
        )
    )

    private val mvp = FloatArray(16)
    private val modelView = FloatArray(16)

    fun createOnGlThread() {
        program = GlUtil.compile(VERTEX_SRC, FRAGMENT_SRC)
        aPosition = GLES20.glGetAttribLocation(program, "aPosition")
        aQuadUv = GLES20.glGetAttribLocation(program, "aQuadUv")
        uMvp = GLES20.glGetUniformLocation(program, "uMvp")
        uTexture = GLES20.glGetUniformLocation(program, "uTexture")
        uAlpha = GLES20.glGetUniformLocation(program, "uAlpha")
        uFeather = GLES20.glGetUniformLocation(program, "uFeather")
        uUvScale = GLES20.glGetUniformLocation(program, "uUvScale")
        uUvOffset = GLES20.glGetUniformLocation(program, "uUvOffset")
        uStMatrix = GLES20.glGetUniformLocation(program, "uStMatrix")
    }

    /** 单位是米。顺序必须与 [uvBuf] 一致：左下、右下、左上、右上。 */
    fun setSize(widthM: Float, heightM: Float) {
        val x = widthM / 2f
        val z = heightM / 2f
        // 图片的「上」是 -Z，所以 uv 的 v=0（下）对应 +z
        verts[0] = -x; verts[1] = 0f; verts[2] = +z
        verts[3] = +x; verts[4] = 0f; verts[5] = +z
        verts[6] = -x; verts[7] = 0f; verts[8] = -z
        verts[9] = +x; verts[10] = 0f; verts[11] = -z
        vertBuf.position(0)
        vertBuf.put(verts)
        vertBuf.position(0)
    }

    /**
     * @param model      照片 centerPose 的 4x4 矩阵
     * @param stMatrix   SurfaceTexture 的变换矩阵；不套它视频会上下翻转
     * @param uv         [Geometry.fillCropUv] 算出来的居中裁切
     * @param alpha      淡入进度
     */
    fun draw(
        model: FloatArray,
        view: FloatArray,
        projection: FloatArray,
        textureId: Int,
        stMatrix: FloatArray,
        uv: Geometry.UvRect,
        alpha: Float,
    ) {
        if (program == 0 || alpha <= 0f) return
        Matrix.multiplyMM(modelView, 0, view, 0, model, 0)
        Matrix.multiplyMM(mvp, 0, projection, 0, modelView, 0)

        GLES20.glUseProgram(program)
        // 半透明边缘要混合；深度写入关掉，否则羽化区会把背景挖出洞
        GLES20.glEnable(GLES20.GL_BLEND)
        GLES20.glBlendFunc(GLES20.GL_SRC_ALPHA, GLES20.GL_ONE_MINUS_SRC_ALPHA)
        GLES20.glDepthMask(false)

        GLES20.glActiveTexture(GLES20.GL_TEXTURE0)
        GLES20.glBindTexture(GlUtil.TEXTURE_EXTERNAL_OES, textureId)
        GLES20.glUniform1i(uTexture, 0)
        GLES20.glUniformMatrix4fv(uMvp, 1, false, mvp, 0)
        GLES20.glUniformMatrix4fv(uStMatrix, 1, false, stMatrix, 0)
        GLES20.glUniform1f(uAlpha, alpha)
        GLES20.glUniform1f(uFeather, Geometry.FEATHER)
        GLES20.glUniform2f(uUvScale, uv.uScale, uv.vScale)
        GLES20.glUniform2f(uUvOffset, uv.uOffset, uv.vOffset)

        vertBuf.position(0)
        uvBuf.position(0)
        GLES20.glVertexAttribPointer(aPosition, 3, GLES20.GL_FLOAT, false, 0, vertBuf)
        GLES20.glVertexAttribPointer(aQuadUv, 2, GLES20.GL_FLOAT, false, 0, uvBuf)
        GLES20.glEnableVertexAttribArray(aPosition)
        GLES20.glEnableVertexAttribArray(aQuadUv)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP, 0, 4)
        GLES20.glDisableVertexAttribArray(aPosition)
        GLES20.glDisableVertexAttribArray(aQuadUv)

        GLES20.glDepthMask(true)
        GLES20.glDisable(GLES20.GL_BLEND)
        GlUtil.checkError("VideoQuad.draw")
    }
}

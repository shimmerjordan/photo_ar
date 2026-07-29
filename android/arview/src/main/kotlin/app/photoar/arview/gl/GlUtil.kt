package app.photoar.arview.gl

import android.opengl.GLES20
import android.util.Log
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.nio.FloatBuffer

/**
 * GLES 2.0 的一点点胶水。
 *
 * 这里手写渲染而不用 SceneView/Filament，是因为整个场景只有两个四边形（相机背景
 * 一个、视频一个），而 §11.8 的边缘羽化 + 淡入需要自定义片元着色器 —— Filament
 * 的自定义材质要用 matc 预编译成 .filamat 塞进 assets，为一个四边形背上 ~10MB
 * 的运行时和一套构建期工具链不值得。外部纹理（SurfaceTexture → ExoPlayer）在
 * 裸 GLES 下也是最直的一条路。
 */
object GlUtil {

    private const val TAG = "GlUtil"

    fun compile(vertexSrc: String, fragmentSrc: String): Int {
        val vs = shader(GLES20.GL_VERTEX_SHADER, vertexSrc)
        val fs = shader(GLES20.GL_FRAGMENT_SHADER, fragmentSrc)
        val program = GLES20.glCreateProgram()
        GLES20.glAttachShader(program, vs)
        GLES20.glAttachShader(program, fs)
        GLES20.glLinkProgram(program)
        val status = IntArray(1)
        GLES20.glGetProgramiv(program, GLES20.GL_LINK_STATUS, status, 0)
        if (status[0] == 0) {
            val log = GLES20.glGetProgramInfoLog(program)
            GLES20.glDeleteProgram(program)
            throw RuntimeException("着色器链接失败：$log")
        }
        // 链接完就能删，program 已经持有编译结果
        GLES20.glDeleteShader(vs)
        GLES20.glDeleteShader(fs)
        return program
    }

    private fun shader(type: Int, src: String): Int {
        val id = GLES20.glCreateShader(type)
        GLES20.glShaderSource(id, src)
        GLES20.glCompileShader(id)
        val status = IntArray(1)
        GLES20.glGetShaderiv(id, GLES20.GL_COMPILE_STATUS, status, 0)
        if (status[0] == 0) {
            val log = GLES20.glGetShaderInfoLog(id)
            GLES20.glDeleteShader(id)
            throw RuntimeException("着色器编译失败：$log\n$src")
        }
        return id
    }

    fun floatBuffer(values: FloatArray): FloatBuffer =
        ByteBuffer.allocateDirect(values.size * 4)
            .order(ByteOrder.nativeOrder())
            .asFloatBuffer()
            .apply {
                put(values)
                position(0)
            }

    /** 建一个外部纹理（相机背景 / 视频都用这种）。 */
    fun createExternalTexture(): Int {
        val ids = IntArray(1)
        GLES20.glGenTextures(1, ids, 0)
        val id = ids[0]
        val target = 0x8D65 // GL_TEXTURE_EXTERNAL_OES
        GLES20.glBindTexture(target, id)
        // 外部纹理不支持 mipmap 也不支持 REPEAT，参数写错会直接黑屏
        GLES20.glTexParameteri(target, GLES20.GL_TEXTURE_MIN_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(target, GLES20.GL_TEXTURE_MAG_FILTER, GLES20.GL_LINEAR)
        GLES20.glTexParameteri(target, GLES20.GL_TEXTURE_WRAP_S, GLES20.GL_CLAMP_TO_EDGE)
        GLES20.glTexParameteri(target, GLES20.GL_TEXTURE_WRAP_T, GLES20.GL_CLAMP_TO_EDGE)
        GLES20.glBindTexture(target, 0)
        return id
    }

    fun checkError(where: String) {
        var e = GLES20.glGetError()
        while (e != GLES20.GL_NO_ERROR) {
            Log.e(TAG, "$where: GL 错误 0x${Integer.toHexString(e)}")
            e = GLES20.glGetError()
        }
    }

    const val TEXTURE_EXTERNAL_OES = 0x8D65
}

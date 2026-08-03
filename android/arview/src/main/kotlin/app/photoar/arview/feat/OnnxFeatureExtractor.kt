package app.photoar.arview.feat

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.io.File
import java.nio.FloatBuffer
import java.nio.LongBuffer

/**
 * 端上跑 XFeat。
 *
 * **这个文件是本次改动里唯一在这里跑不起来、也验不了的部分**（它要 ONNX Runtime 的
 * native 库和一台真机）。所以它刻意做得没有任何判断逻辑：预处理在 [XFeatPreprocess]、
 * 后处理在 [XFeatDecode]、请求体在 [FeaturesRequest]、什么时候用它/失败怎么办在
 * [FeaturePathPolicy] —— 那四个都是纯 Kotlin 且有单测。这里只剩「把张量喂给 ORT，
 * 把三个输出拿回来」。
 *
 * 抽成 [FeatureExtractor] 接口是为了让上面那条链路能在 JVM 里用一个假实现走通。
 */
interface FeatureExtractor {
    /**
     * 一帧 JPEG → 有效特征。
     *
     * @throws Exception 任何失败。调用方按 [FeatureFailure.INFER_FAILED] 处理
     *   （静默回退，见 [FeaturePathPolicy]）。
     */
    fun extract(jpeg: ByteArray): Extracted

    fun close()

    /** 特征 + 这一帧的原始尺寸（请求体里要带）。 */
    class Extracted(val features: ExtractedFeatures, val height: Int, val width: Int)
}

/**
 * ONNX Runtime 实现。
 *
 * 构造即加载会话（与服务端 `backend.xfeat_backend` 同一个取舍）：让「模型坏了/这台机器
 * 不支持」在打开开关的那一刻暴露，而不是等到举起手机对着照片的时候。
 *
 * 线程：`ScanRuntime` 只在它那一条网络线程上调 [extract]，所以这里不加锁。ORT 的
 * `run` 本身线程安全，但 intra-op 线程池是会话级共享的 —— 并发调用会互相抢核，
 * 在手机上比串行更慢（服务端 `xfeat.XFeatExtractor` 为此专门加了一把锁）。
 */
class OnnxFeatureExtractor private constructor(
    private val env: ai.onnxruntime.OrtEnvironment,
    private val session: ai.onnxruntime.OrtSession,
) : FeatureExtractor {

    companion object {
        /**
         * intra-op 线程数。
         *
         * 取 2 而不是「核数」：手机是大小核异构的，ORT 按核数开线程会把一部分工作排到
         * 小核上，而它的同步是等最慢的那个 —— 实测（服务端那侧同一个坑，见
         * `xfeat._default_threads`）线程开多了比开少了慢。另外这是在扫描循环里跑的，
         * 抢满 CPU 会让相机预览掉帧，那比推理慢 10ms 明显得多。
         */
        const val THREADS = 2

        /**
         * 打开一个会话。失败**抛异常**，由调用方转成 [FeatureFailure.LOAD_FAILED]。
         *
         * `Throwable` 而不是 `Exception`：ORT 加载不上 native 库时抛的是
         * `UnsatisfiedLinkError`／`NoClassDefFoundError`，两个都是 `Error` 不是
         * `Exception`。只 catch Exception 的话，一台 ABI 不匹配的机器会直接崩掉整个
         * 进程 —— 而这恰恰是最该被静默回退掉的一种失败。
         */
        fun open(model: File): OnnxFeatureExtractor {
            val env = ai.onnxruntime.OrtEnvironment.getEnvironment()
            val opts = ai.onnxruntime.OrtSession.SessionOptions().apply {
                setIntraOpNumThreads(THREADS)
                setInterOpNumThreads(1)
            }
            val session = env.createSession(model.absolutePath, opts)
            return OnnxFeatureExtractor(env, session)
        }
    }

    override fun extract(jpeg: ByteArray): FeatureExtractor.Extracted {
        val bitmap = decode(jpeg) ?: throw IllegalStateException("这一帧解不出位图")
        // 尺寸在 recycle 之前抄下来：请求体里要带这一帧的原始尺寸（服务端按它算有效区
        // 来验坐标），而位图那时已经没了。
        val frameH = bitmap.height
        val frameW = bitmap.width
        val prepared = try {
            XFeatPreprocess.prepare(bitmap.toPixels())
        } finally {
            bitmap.recycle()
        }

        // 输入张量：image (1,3,640,640) float32、size (2,) int64。形状必须逐字对上 ——
        // 图是全静态导出的（刻意的，见 tools/export_models.py），形状不符会直接抛。
        val image = ai.onnxruntime.OnnxTensor.createTensor(
            env,
            FloatBuffer.wrap(prepared.nchw),
            longArrayOf(1, 3, CANVAS.toLong(), CANVAS.toLong()),
        )
        val size = ai.onnxruntime.OnnxTensor.createTensor(
            env,
            LongBuffer.wrap(prepared.sizeInput()),
            longArrayOf(2),
        )
        try {
            session.run(mapOf("image" to image, "size" to size)).use { out ->
                val kp = floats(out, 0)
                val desc = floats(out, 1)
                val scores = floats(out, 2)
                return FeatureExtractor.Extracted(
                    XFeatDecode.decode(kp, desc, scores),
                    height = frameH,
                    width = frameW,
                )
            }
        } finally {
            image.close()
            size.close()
        }
    }

    private fun decode(jpeg: ByteArray): Bitmap? {
        val opts = BitmapFactory.Options().apply {
            // ARGB_8888：预处理按 0xAARRGGBB 取通道。RGB_565 会把每个通道压到 5/6 位，
            // 而那种精度损失直接落在描述子上。
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        return BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, opts)
    }

    private fun Bitmap.toPixels(): PixelSource {
        val buf = IntArray(width * height)
        getPixels(buf, 0, width, 0, 0, width, height)
        return ArgbPixels(buf, width, height)
    }

    /**
     * 把第 i 个输出摊成 FloatArray。
     *
     * ORT 的 Java 绑定对多维 float 张量给的是嵌套数组（`float[1][512][2]`），
     * 而 `getFloatBuffer()` 给的是同一块内存的平坦视图 —— 用后者省掉一次几十万元素的
     * 逐个装箱拷贝。
     */
    private fun floats(out: ai.onnxruntime.OrtSession.Result, i: Int): FloatArray {
        val t = out.get(i) as ai.onnxruntime.OnnxTensor
        val buf = t.floatBuffer
        val arr = FloatArray(buf.remaining())
        buf.get(arr)
        return arr
    }

    override fun close() {
        try {
            session.close()
        } catch (e: Throwable) {
            // 关会话失败没有任何补救动作，也不该把它变成一次崩溃。
        }
    }
}

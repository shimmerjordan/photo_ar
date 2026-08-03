package app.photoar.arview.cache

import app.photoar.arview.NetErrorKind
import app.photoar.arview.feat.FeatureFailure
import app.photoar.arview.net.HttpFailure
import app.photoar.arview.net.ModelFetch
import app.photoar.arview.net.PhotoArClient
import java.io.File

/**
 * 端上提特征用的 ONNX 模型的本地缓存。
 *
 * 沿用这个包里既有的三条约定（见 [PhotoCache] 与 `ar.TargetLoader`）：
 * - 只用 `java.io.File`，**不碰 android.\***，所以整条「协商 → 下载 → 落盘 → 复用」
 *   都能在 JVM 单测里用真实临时目录跑真实读写。
 * - 写文件走 tmp + rename：进程在写一半被杀，读回来的是残缺文件；而"有文件就直接
 *   `InferenceSession` 它"的话，残缺的 4MB 会变成一次 ONNX 加载失败，然后被当成
 *   "这台机器不支持"永久回退。
 * - 元数据（这里只有 ETag）单独一个小文件，坏了就当没有 —— 代价只是多下一次。
 *
 * 目录放在 `filesDir` 下面而不是 `cacheDir`：`cacheDir` 里的东西系统可以随时删，
 * 而"打开开关之后每次冷启动都重下 4.31MB"是用户看得见的流量。
 */
class ModelCache(private val dir: File) {

    companion object {
        /** [PhotoCache] 用的是 `filesDir/photoar`，模型另开一个子目录。 */
        const val DIR = "models"

        const val MODEL_NAME = "xfeat.onnx"

        /**
         * 模型文件大小的下限（字节）。
         *
         * 4.31MB 是导出产物的实际大小。这里只做一个很松的下限（1MB），用来挡住"服务端
         * 上有一个 0 字节或几百字节的占位文件"这种情况 —— 那种文件会让
         * `InferenceSession` 抛一个看不懂的异常，然后被归因成"这台机器不支持 ONNX"。
         *
         * 不校验精确大小/哈希：模型是允许被换的（换 top_k、重新导出都会变大小），
         * 写死任何一个数都会在换模型那天把这条路整体关掉。
         */
        const val MIN_BYTES = 1L shl 20
    }

    /** @param filesDir 传 `context.filesDir`。 */
    constructor(filesDir: File, sub: String = DIR) : this(File(filesDir, sub))

    val modelFile: File get() = File(dir, MODEL_NAME)

    private val etagFile: File get() = File(dir, "$MODEL_NAME.etag")

    /** 本地那份能用吗。文件在、且大得像一个真模型。 */
    val ready: Boolean get() = modelFile.isFile && modelFile.length() >= MIN_BYTES

    /** 本地那份的 ETag。没有（或者模型文件本身不在）就返回 null。 */
    fun etag(): String? {
        if (!ready) return null
        return try {
            etagFile.takeIf { it.isFile }?.readText()?.trim()?.takeIf { it.isNotEmpty() }
        } catch (e: Exception) {
            null
        }
    }

    /**
     * 确保本地有一份可用的模型。
     *
     * @return 模型文件；取不到时返回 null 并把原因放进 [outcome]。
     *
     * **拿不到从来不是错误**，所以这里不抛异常：调用方的正确反应是静默退回传 JPEG
     * （见 [app.photoar.arview.feat.FeaturePathPolicy]），而抛异常会诱导调用方把它
     * 当成一次识别失败去重试。
     */
    fun ensure(client: PhotoArClient, outcome: Outcome = Outcome()): File? {
        val local = etag()
        val fetched = try {
            client.fetchModel(local)
        } catch (e: HttpFailure) {
            // 404 model_missing：服务端上没有模型（后端是 orb 时这是**正常**状态）。
            // 网络问题：这一趟拿不到，但本地那份（如果有）照样能用。
            outcome.failure = FeatureFailure.MODEL_UNAVAILABLE
            outcome.detail = e.message
            return modelFile.takeIf { ready && e.kind != NetErrorKind.UNAUTHORIZED }
        } catch (e: Exception) {
            outcome.failure = FeatureFailure.MODEL_UNAVAILABLE
            outcome.detail = e.message
            return modelFile.takeIf { ready }
        }

        return when (fetched) {
            // 304：服务端说没变，本地那份就是对的。
            //
            // 仍然过一遍 `ready`：正常路径走不到这里而本地没文件（`etag()` 只在 ready
            // 时才返回非 null，没有 ETag 就不会带 If-None-Match，服务端也就不会 304）。
            // 但一个坏掉的反向代理**会**无条件 304，而那时返回一个不存在的路径会变成
            // 一次 ONNX 加载失败 —— 于是被归因成"这台机器不支持"并永久回退。
            is ModelFetch.NotModified -> modelFile.takeIf { ready }
            is ModelFetch.Fresh -> {
                if (fetched.bytes.size < MIN_BYTES) {
                    outcome.failure = FeatureFailure.MODEL_UNAVAILABLE
                    outcome.detail = "服务端给的模型只有 ${fetched.bytes.size} 字节"
                    return null
                }
                if (!store(fetched.bytes, fetched.etag)) {
                    outcome.failure = FeatureFailure.MODEL_UNAVAILABLE
                    outcome.detail = "模型写不进 $dir"
                    return null
                }
                modelFile
            }
        }
    }

    /**
     * 落盘。
     *
     * ETag 在**模型文件改名成功之后**才写。反过来的话，一次写模型失败会留下一个指向旧
     * 模型（或者根本不存在的模型）的新 ETag —— 于是下一次协商拿到 304，而本地那份是错
     * 的或者没有。这种不一致自己不会好，只能靠用户清数据。
     */
    fun store(bytes: ByteArray, etag: String?): Boolean {
        if (!dir.isDirectory && !dir.mkdirs()) return false
        val tmp = File(dir, "$MODEL_NAME.tmp")
        try {
            tmp.writeBytes(bytes)
            if (!tmp.renameTo(modelFile)) {
                // 同目录内 rename 失败基本只有权限/满盘两种，直接覆写也大概率失败，
                // 但试一次比不试好（覆写不是原子的，所以只在 rename 走不通时才用）。
                tmp.delete()
                modelFile.writeBytes(bytes)
            }
        } catch (e: Exception) {
            tmp.delete()
            return false
        }
        try {
            if (etag.isNullOrBlank()) etagFile.delete() else etagFile.writeText(etag)
        } catch (e: Exception) {
            // ETag 写不下只影响下次要不要重下，不影响这次能不能用。
            etagFile.delete()
        }
        return true
    }

    /** 清掉本地那份（用户在设置里关掉开关时可以顺手腾出 4MB）。 */
    fun clear() {
        modelFile.delete()
        etagFile.delete()
    }

    /** [ensure] 的出参。用一个可变对象而不是返回 Pair，让常路（成功）读起来干净。 */
    class Outcome {
        var failure: FeatureFailure? = null
        var detail: String? = null
    }
}

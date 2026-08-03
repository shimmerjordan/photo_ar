package app.photoar.arview.feat

/**
 * ONNX 输出的后处理，以及 `POST /v1/recognize/features` 的请求体。
 *
 * 纯 Kotlin，没有 android.*，所以整条「张量 → 请求体」的链路能在 JVM 单测里逐字节验。
 * 服务端那边的解析与校验在 `src/photoar/server/featurebody.py`，它会拒掉字节序反了、
 * 没归一化、坐标越界这几类错 —— 也就是说这里写错**会**被发现，但发现的地方是运行时的
 * 一个 400，所以还是在这里测掉更好。
 */

/**
 * 从图里裁出来的有效特征。
 *
 * @param count 有效关键点数（`scores > 0` 的槽位数），可能远小于 [TOP_K]。
 * @param keypoints 长度 `count * 2`，`(x, y)` 交错，画布坐标系。
 * @param descriptors 长度 `count * DESC_DIM`，行优先，**已 L2 归一化**（图里做的）。
 */
class ExtractedFeatures(
    val count: Int,
    val keypoints: FloatArray,
    val descriptors: FloatArray,
) {
    init {
        require(keypoints.size == count * 2) {
            "关键点长度 ${keypoints.size} 与 count=$count 不符"
        }
        require(descriptors.size == count * DESC_DIM) {
            "描述子长度 ${descriptors.size} 与 count=$count 不符"
        }
    }

    val isEmpty: Boolean get() = count == 0

    companion object {
        val EMPTY = ExtractedFeatures(0, FloatArray(0), FloatArray(0))
    }
}

object XFeatDecode {

    /**
     * 丢掉填充槽位。
     *
     * `scores <= 0` 的那些是有效峰值不足 [TOP_K] 时补上的，**坐标是 topk 在等值上的任意
     * 选择** —— 也就是说它们是真实存在的坐标值，但与图像内容无关。不丢的话，它们会带着
     * 一批无意义的描述子进入互近邻匹配，稀释真实匹配、抬高 RANSAC 的外点比例。
     * 这与官方实现的 `valid = scores > 0` 是同一条规则（也与 `xfeat.decode` 一致）。
     *
     * @param keypoints ONNX 输出 `keypoints`，形状 (1, 512, 2) 摊平。
     * @param descriptors ONNX 输出 `descriptors`，形状 (1, 512, 64) 摊平。
     * @param scores ONNX 输出 `scores`，形状 (1, 512) 摊平。
     */
    fun decode(
        keypoints: FloatArray,
        descriptors: FloatArray,
        scores: FloatArray,
    ): ExtractedFeatures {
        val slots = scores.size
        require(keypoints.size >= slots * 2) {
            "keypoints 长度 ${keypoints.size} 不够 $slots 个槽位"
        }
        require(descriptors.size >= slots * DESC_DIM) {
            "descriptors 长度 ${descriptors.size} 不够 $slots 个槽位"
        }
        var n = 0
        for (i in 0 until slots) if (scores[i] > 0f) n++
        if (n == 0) return ExtractedFeatures.EMPTY
        val pts = FloatArray(n * 2)
        val desc = FloatArray(n * DESC_DIM)
        var w = 0
        for (i in 0 until slots) {
            if (scores[i] <= 0f) continue
            pts[w * 2] = keypoints[i * 2]
            pts[w * 2 + 1] = keypoints[i * 2 + 1]
            System.arraycopy(descriptors, i * DESC_DIM, desc, w * DESC_DIM, DESC_DIM)
            w++
        }
        return ExtractedFeatures(n, pts, desc)
    }
}

object FeaturesRequest {

    /**
     * 请求体 JSON。
     *
     * `width` / `height` 传的是**这一帧的尺寸**（缩放之前的），服务端按同一个公式算出
     * 有效区来验坐标。传缩放后的尺寸也对（`canvasSize` 对成比例缩放不敏感），但传原始
     * 尺寸更少一层推理。
     *
     * 手写 JSON 而不是用 `JSONObject`：描述子那两个字段是十几万字符的 base64，
     * `JSONObject.toString()` 会为它们再复制两遍字符串（一次进 map、一次出）。手写用一个
     * 预估好容量的 `StringBuilder` 一次成型 —— 这是每 400ms 一次的热路径。
     * 安全性上没有取舍：两个值都是 base64（字符集里没有需要转义的字符），两个是整数。
     */
    fun body(features: ExtractedFeatures, height: Int, width: Int): String {
        val kp = Base64Le.encodeFloats(features.keypoints)
        val ds = Base64Le.encodeFloats(features.descriptors)
        return StringBuilder(kp.length + ds.length + 96)
            .append("{\"width\":").append(width)
            .append(",\"height\":").append(height)
            .append(",\"keypoints\":\"").append(kp)
            .append("\",\"descriptors\":\"").append(ds)
            .append("\"}")
            .toString()
    }
}

/**
 * float32 小端 → base64。
 *
 * **为什么自己写一个 base64**：`java.util.Base64` 要 API 26，而本模块 minSdk 是 24
 * （ARCore 的下限）；`android.util.Base64` 从 API 8 就有，但它是 android.*，而这个模块
 * 刻意没开 `unitTests.isReturnDefaultValues` —— 单测里一碰它就抛异常。也就是说用它等于
 * 放弃对这一段的验证，而「字节序」和「编码」正是这条路上最容易静默出错的两件事。
 * 编码器本身是二十行确定性代码，自己写比放弃测试便宜得多。
 */
object Base64Le {

    private val ALPHABET =
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/".toCharArray()

    /**
     * 每个 float 按 **IEEE 754 小端** 4 字节写出，再 base64。
     *
     * 小端是契约（服务端用 `np.frombuffer(dtype="<f4")` 读）。显式移位取字节而不是靠
     * `ByteBuffer` 的默认序：`ByteBuffer.allocate()` 的默认是**大端**，而 JVM 跑在小端
     * 机器上这件事完全不影响它 —— 也就是说「忘了设 order」会在两边都不报错的情况下把
     * 每个 float 的字节读反，然后表现为识别率归零。
     */
    fun encodeFloats(values: FloatArray): String {
        val bytes = ByteArray(values.size * 4)
        var i = 0
        for (v in values) {
            val bits = java.lang.Float.floatToRawIntBits(v)
            bytes[i++] = (bits and 0xFF).toByte()
            bytes[i++] = ((bits ushr 8) and 0xFF).toByte()
            bytes[i++] = ((bits ushr 16) and 0xFF).toByte()
            bytes[i++] = ((bits ushr 24) and 0xFF).toByte()
        }
        return encode(bytes)
    }

    /** 标准 base64（带 `=` 填充，不换行）。 */
    fun encode(data: ByteArray): String {
        if (data.isEmpty()) return ""
        val out = StringBuilder((data.size + 2) / 3 * 4)
        var i = 0
        while (i + 2 < data.size) {
            val n = ((data[i].toInt() and 0xFF) shl 16) or
                ((data[i + 1].toInt() and 0xFF) shl 8) or
                (data[i + 2].toInt() and 0xFF)
            out.append(ALPHABET[(n ushr 18) and 0x3F])
            out.append(ALPHABET[(n ushr 12) and 0x3F])
            out.append(ALPHABET[(n ushr 6) and 0x3F])
            out.append(ALPHABET[n and 0x3F])
            i += 3
        }
        when (data.size - i) {
            1 -> {
                val n = (data[i].toInt() and 0xFF) shl 16
                out.append(ALPHABET[(n ushr 18) and 0x3F])
                out.append(ALPHABET[(n ushr 12) and 0x3F])
                out.append("==")
            }

            2 -> {
                val n = ((data[i].toInt() and 0xFF) shl 16) or
                    ((data[i + 1].toInt() and 0xFF) shl 8)
                out.append(ALPHABET[(n ushr 18) and 0x3F])
                out.append(ALPHABET[(n ushr 12) and 0x3F])
                out.append(ALPHABET[(n ushr 6) and 0x3F])
                out.append('=')
            }
        }
        return out.toString()
    }
}

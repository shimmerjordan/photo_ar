package app.photoar.arview.feat

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * ONNX 输出的后处理 + 请求体编码。
 *
 * 这两件事在服务端那边有对应的校验（`featurebody.py` 会拒掉字节序反了、长度对不上、
 * 没归一化），所以写错**会**被发现 —— 但发现的地方是运行时的一个 400，而 400 出现在
 * 真机上、每 400ms 一次。在这里测掉便宜得多。
 */
class FeaturesRequestTest {

    // ---- 丢填充槽位 ----

    @Test
    fun `丢掉 scores 小于等于 0 的槽位`() {
        // 那些是有效峰值不足 512 时补上的，坐标是 topk 在等值上的**任意**选择 ——
        // 不丢的话它们会带着一批与图像内容无关的描述子进入互近邻匹配。
        val slots = 4
        val kp = floatArrayOf(1f, 2f, 3f, 4f, 5f, 6f, 7f, 8f)
        val desc = FloatArray(slots * DESC_DIM) { it.toFloat() }
        val scores = floatArrayOf(0.9f, 0f, 0.5f, -1f)

        val out = XFeatDecode.decode(kp, desc, scores)
        assertEquals(2, out.count)
        assertTrue(out.keypoints.contentEquals(floatArrayOf(1f, 2f, 5f, 6f)))
        // 第 0 与第 2 个槽位的描述子行被原样搬过来
        assertEquals(0f, out.descriptors[0], 0f)
        assertEquals((2 * DESC_DIM).toFloat(), out.descriptors[DESC_DIM], 0f)
    }

    @Test
    fun `一个有效点都没有时给空结果而不是抛异常`() {
        // 一面白墙上确实提不出关键点。那是未命中这个正常状态。
        val out = XFeatDecode.decode(
            FloatArray(4),
            FloatArray(2 * DESC_DIM),
            floatArrayOf(0f, -0.1f),
        )
        assertEquals(0, out.count)
        assertTrue(out.isEmpty)
    }

    @Test
    fun `全部有效时一个都不丢`() {
        val n = TOP_K
        val out = XFeatDecode.decode(
            FloatArray(n * 2) { it.toFloat() },
            FloatArray(n * DESC_DIM) { 1f },
            FloatArray(n) { 0.3f },
        )
        assertEquals(n, out.count)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `输出长度对不上立刻拒绝`() {
        // 图是全静态导出的，长度不符只可能是我们自己把摊平搞错了。
        XFeatDecode.decode(FloatArray(2), FloatArray(DESC_DIM), FloatArray(4))
    }

    // ---- base64 ----

    @Test
    fun `base64 与 JDK 的实现逐字节相同`() {
        // 自己写一个是因为 java.util.Base64 要 API 26 而 minSdk 是 24，
        // android.util.Base64 又是 android.*（单测里一碰就抛）。既然自己写，
        // 就必须证明它与标准实现相同。
        val rnd = java.util.Random(7)
        for (len in 0..64) {
            val data = ByteArray(len).also { rnd.nextBytes(it) }
            assertEquals(
                "长度 $len",
                java.util.Base64.getEncoder().encodeToString(data),
                Base64Le.encode(data),
            )
        }
    }

    @Test
    fun `三种尾巴长度的填充都对`() {
        assertEquals("QQ==", Base64Le.encode(byteArrayOf(0x41)))
        assertEquals("QUI=", Base64Le.encode(byteArrayOf(0x41, 0x42)))
        assertEquals("QUJD", Base64Le.encode(byteArrayOf(0x41, 0x42, 0x43)))
        assertEquals("", Base64Le.encode(ByteArray(0)))
    }

    @Test
    fun `高位字节不会被符号扩展`() {
        // Kotlin 的 Byte 是有符号的。忘了 `and 0xFF` 的话 0x80 以上的字节会变成负数，
        // 移位之后污染相邻的两个 6 bit 组 —— 而低位字节的数据看起来完全正常。
        val data = byteArrayOf(-1, -128, 127, 0)
        assertEquals(java.util.Base64.getEncoder().encodeToString(data), Base64Le.encode(data))
    }

    @Test
    fun `float32 按小端写出`() {
        // 契约：服务端用 np.frombuffer(dtype="<f4") 读。ByteBuffer 的默认序是**大端**，
        // 而 JVM 跑在小端机器上完全不影响它 —— 也就是说「忘了设 order」两边都不报错，
        // 只表现为识别率归零。
        val bytes = java.util.Base64.getDecoder().decode(Base64Le.encodeFloats(floatArrayOf(1.0f)))
        // 1.0f 的 IEEE754 是 0x3F800000，小端写出来是 00 00 80 3F
        assertEquals(4, bytes.size)
        assertEquals(0x00, bytes[0].toInt() and 0xFF)
        assertEquals(0x00, bytes[1].toInt() and 0xFF)
        assertEquals(0x80, bytes[2].toInt() and 0xFF)
        assertEquals(0x3F, bytes[3].toInt() and 0xFF)
    }

    @Test
    fun `float 往返精确`() {
        val values = floatArrayOf(0f, -0f, 1f, -1f, 3.1415927f, 1e-30f, 1e30f, 255f)
        val bytes = java.util.Base64.getDecoder().decode(Base64Le.encodeFloats(values))
        val buf = java.nio.ByteBuffer.wrap(bytes).order(java.nio.ByteOrder.LITTLE_ENDIAN)
        for (v in values) {
            assertEquals(
                java.lang.Float.floatToRawIntBits(v),
                java.lang.Float.floatToRawIntBits(buf.float),
            )
        }
    }

    // ---- 请求体 ----

    @Test
    fun `请求体的四个字段与服务端契约一致`() {
        val f = ExtractedFeatures(
            2,
            floatArrayOf(10f, 20f, 30f, 40f),
            FloatArray(2 * DESC_DIM) { 0.125f },
        )
        val o = JSONObject(FeaturesRequest.body(f, height = 480, width = 640))
        assertEquals(640, o.getInt("width"))
        assertEquals(480, o.getInt("height"))
        // 关键点：2 个点 × 2 个 float32 = 16 字节 → base64 是 24 个字符
        assertEquals(24, o.getString("keypoints").length)
        // 描述子：2 × 64 × 4 = 512 字节 → base64 是 684 个字符（512/3 向上取整 ×4）
        assertEquals(684, o.getString("descriptors").length)
    }

    @Test
    fun `请求体是合法 JSON 且没有多余字段`() {
        // 手写 JSON（为了避开两次字符串复制）最容易出的错就是括号/逗号 —— 那会让
        // 服务端返回 bad_json，而客户端会把它当成一次识别失败。
        val f = ExtractedFeatures(0, FloatArray(0), FloatArray(0))
        val o = JSONObject(FeaturesRequest.body(f, 100, 200))
        assertEquals(setOf("width", "height", "keypoints", "descriptors"), o.keys().asSequence().toSet())
        assertEquals("", o.getString("keypoints"))
        assertEquals("", o.getString("descriptors"))
    }

    @Test
    fun `满编 512 点的请求体大小落在服务端上限之内`() {
        // 服务端 MAX_FEATURES_BYTES = 368640。这条测试就是拦住「顺手把 TOP_K 调大」
        // 之后请求全部 413。
        val f = ExtractedFeatures(
            TOP_K,
            FloatArray(TOP_K * 2) { 1f },
            FloatArray(TOP_K * DESC_DIM) { 0.1f },
        )
        val body = FeaturesRequest.body(f, 720, 1280)
        assertTrue("实际 ${body.length} 字符", body.length in 170_000..200_000)
        assertTrue(body.toByteArray(Charsets.UTF_8).size < 368_640)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `长度与 count 不符时立刻拒绝`() {
        ExtractedFeatures(3, FloatArray(4), FloatArray(3 * DESC_DIM))
    }
}

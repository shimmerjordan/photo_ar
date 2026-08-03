package app.photoar.arview.cache

import app.photoar.arview.Endpoints
import app.photoar.arview.NetErrorKind
import app.photoar.arview.feat.FeatureFailure
import app.photoar.arview.net.HttpFailure
import app.photoar.arview.net.HttpReply
import app.photoar.arview.net.HttpTransport
import app.photoar.arview.net.PhotoArClient
import java.io.File
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 模型缓存。用真实临时目录跑真实读写 —— 这一层出错的样子（残缺文件被当成有效模型、
 * ETag 与实际内容对不上）只有真读写才验得出来，和 [PhotoCacheTest] 同一个理由。
 */
class ModelCacheTest {

    private lateinit var root: File
    private lateinit var cache: ModelCache

    /** 大于 [ModelCache.MIN_BYTES] 的一份"模型"。 */
    private fun bigBytes(fill: Byte = 7): ByteArray =
        ByteArray((ModelCache.MIN_BYTES + 16).toInt()) { fill }

    @Before
    fun setUp() {
        root = File.createTempFile("photoar-model", "").let {
            it.delete()
            it.mkdirs()
            it
        }
        cache = ModelCache(root, "models")
    }

    @After
    fun tearDown() {
        root.deleteRecursively()
    }

    private class FakeTransport : HttpTransport {
        var status = 200
        var body = ByteArray(0)
        var headers: Map<String, String> = emptyMap()
        var failure: HttpFailure? = null
        val requests = ArrayList<Map<String, String>>()

        override fun get(
            url: String,
            headers: Map<String, String>,
            timeoutMs: Int,
        ): HttpReply {
            requests += headers
            failure?.let { throw it }
            return HttpReply(status, body, this.headers)
        }

        override fun postJpeg(
            url: String,
            field: String,
            jpeg: ByteArray,
            headers: Map<String, String>,
            timeoutMs: Int,
        ): HttpReply = throw UnsupportedOperationException()

        override fun postJson(
            url: String,
            json: String,
            headers: Map<String, String>,
            timeoutMs: Int,
        ): HttpReply = throw UnsupportedOperationException()
    }

    private fun client(t: HttpTransport) = PhotoArClient(
        t,
        { Endpoints("http://nas:8964", "http://nas:8080", "tok") },
    )

    // ---- ready 判定 ----

    @Test
    fun `一开始什么都没有`() {
        assertFalse(cache.ready)
        assertNull(cache.etag())
    }

    @Test
    fun `落盘之后就 ready`() {
        assertTrue(cache.store(bigBytes(), "\"e1\""))
        assertTrue(cache.ready)
        assertEquals("\"e1\"", cache.etag())
        assertArrayEquals(bigBytes(), cache.modelFile.readBytes())
    }

    @Test
    fun `太小的文件不算 ready`() {
        // 服务端上放了一个 0 字节或几百字节的占位文件时，把它交给 InferenceSession
        // 会抛一个看不懂的异常，然后被归因成「这台机器不支持 ONNX」并永久回退。
        cache.store(bigBytes(), null)
        cache.modelFile.writeBytes(ByteArray(1024))
        assertFalse(cache.ready)
        assertNull("模型不 ready 时不该报出 ETag", cache.etag())
    }

    @Test
    fun `没有 ETag 时 store 也算成功`() {
        assertTrue(cache.store(bigBytes(), null))
        assertTrue(cache.ready)
        assertNull(cache.etag())
    }

    @Test
    fun `换一份模型会把旧的 ETag 也换掉`() {
        cache.store(bigBytes(1), "\"old\"")
        cache.store(bigBytes(2), "\"new\"")
        assertEquals("\"new\"", cache.etag())
        assertEquals(2.toByte(), cache.modelFile.readBytes()[0])
    }

    @Test
    fun `新模型没带 ETag 时旧 ETag 必须被删掉`() {
        // 留着旧 ETag 的后果：下一次协商拿到 304，而本地那份已经是新的了 ——
        // 反过来也一样错。宁可多下一次。
        cache.store(bigBytes(1), "\"old\"")
        cache.store(bigBytes(2), null)
        assertNull(cache.etag())
    }

    @Test
    fun `clear 把两个文件都删掉`() {
        cache.store(bigBytes(), "\"e\"")
        cache.clear()
        assertFalse(cache.ready)
        assertNull(cache.etag())
    }

    @Test
    fun `不留 tmp 残留`() {
        cache.store(bigBytes(), "\"e\"")
        val leftovers = File(root, "models").list()!!.filter { it.endsWith(".tmp") }
        assertTrue("tmp 没清掉：$leftovers", leftovers.isEmpty())
    }

    // ---- ensure：协商 + 下载 ----

    @Test
    fun `第一次 ensure 会下载并落盘`() {
        val t = FakeTransport().apply {
            body = bigBytes()
            headers = mapOf("etag" to "\"v1\"")
        }
        val out = ModelCache.Outcome()
        val file = cache.ensure(client(t), out)
        assertNotNull(file)
        assertTrue(cache.ready)
        assertEquals("\"v1\"", cache.etag())
        assertNull(out.failure)
        assertFalse("第一次没有本地 ETag，不该带这个头", t.requests[0].containsKey("If-None-Match"))
    }

    @Test
    fun `第二次 ensure 带上 ETag 并接受 304`() {
        cache.store(bigBytes(), "\"v1\"")
        val t = FakeTransport().apply { status = 304 }
        val file = cache.ensure(client(t))
        assertEquals(cache.modelFile, file)
        assertEquals("\"v1\"", t.requests[0]["If-None-Match"])
        assertEquals("304 不该覆盖本地那份", 7.toByte(), cache.modelFile.readBytes()[0])
    }

    @Test
    fun `本地没有文件却收到 304 时不返回一个不存在的路径`() {
        // 正常路径走不到（没有 ETag 就不会带 If-None-Match），但一个坏掉的反向代理
        // 会无条件 304。返回一个不存在的路径会变成一次 ONNX 加载失败，然后被归因成
        // 「这台机器不支持」并永久回退。
        val t = FakeTransport().apply { status = 304 }
        assertNull(cache.ensure(client(t)))
    }

    @Test
    fun `服务端换了模型时本地会被替换`() {
        cache.store(bigBytes(1), "\"v1\"")
        val t = FakeTransport().apply {
            body = bigBytes(2)
            headers = mapOf("etag" to "\"v2\"")
        }
        cache.ensure(client(t))
        assertEquals(2.toByte(), cache.modelFile.readBytes()[0])
        assertEquals("\"v2\"", cache.etag())
    }

    // ---- ensure：各种拿不到 ----

    @Test
    fun `服务端没有模型时返回 null 并给出原因`() {
        // 404 model_missing 是**正常**部署状态（后端是 orb 时根本不需要模型）。
        // 调用方的正确反应是静默退回传 JPEG，所以这里不抛异常。
        val t = FakeTransport().apply {
            status = 404
            body = """{"error":"model_missing","message":"没有 xfeat.onnx"}""".toByteArray()
        }
        val out = ModelCache.Outcome()
        assertNull(cache.ensure(client(t), out))
        assertEquals(FeatureFailure.MODEL_UNAVAILABLE, out.failure)
        assertTrue(out.detail!!.contains("xfeat.onnx"))
    }

    @Test
    fun `没网但本地有一份时仍然可用`() {
        // 断网不该让一个已经下好模型的用户退回慢路径。
        cache.store(bigBytes(), "\"v1\"")
        val t = FakeTransport().apply {
            failure = HttpFailure(NetErrorKind.TRANSPORT, null, "网络不通")
        }
        val out = ModelCache.Outcome()
        assertEquals(cache.modelFile, cache.ensure(client(t), out))
        // 原因照样记下来（日志要看），但不影响返回
        assertEquals(FeatureFailure.MODEL_UNAVAILABLE, out.failure)
    }

    @Test
    fun `没网且本地没有就是拿不到`() {
        val t = FakeTransport().apply {
            failure = HttpFailure(NetErrorKind.TIMEOUT, null, "超时")
        }
        assertNull(cache.ensure(client(t)))
    }

    @Test
    fun `401 时不用本地那份`() {
        // 凭证失效是要把用户送去重新登录的信号，不该被一次"用本地缓存继续跑"盖住 ——
        // 那会让扫描继续跑下去，而每一次识别请求都是 401。
        cache.store(bigBytes(), "\"v1\"")
        val t = FakeTransport().apply {
            status = 401
            body = """{"error":"unauthorized"}""".toByteArray()
        }
        assertNull(cache.ensure(client(t)))
    }

    @Test
    fun `服务端给了一个太小的文件时不落盘`() {
        val t = FakeTransport().apply { body = ByteArray(512) }
        val out = ModelCache.Outcome()
        assertNull(cache.ensure(client(t), out))
        assertFalse("残缺文件不能进缓存", cache.ready)
        assertEquals(FeatureFailure.MODEL_UNAVAILABLE, out.failure)
        assertTrue(out.detail!!.contains("512"))
    }

    @Test
    fun `拿到 0 字节时不落盘`() {
        val t = FakeTransport().apply { body = ByteArray(0) }
        assertNull(cache.ensure(client(t)))
        assertFalse(cache.ready)
    }

    @Test
    fun `写不进去时如实返回失败`() {
        // 目录被占成一个普通文件（磁盘满、权限问题的可观测替身）。
        val dir = File(root, "blocked")
        dir.writeText("我是个文件不是目录")
        val blocked = ModelCache(root, "blocked")
        assertFalse(blocked.store(bigBytes(), null))

        val t = FakeTransport().apply { body = bigBytes() }
        val out = ModelCache.Outcome()
        assertNull(blocked.ensure(client(t), out))
        assertEquals(FeatureFailure.MODEL_UNAVAILABLE, out.failure)
    }

    @Test
    fun `坏掉的 ETag 文件不影响模型可用`() {
        cache.store(bigBytes(), "\"v1\"")
        File(root, "models/${ModelCache.MODEL_NAME}.etag").writeText("   ")
        assertTrue(cache.ready)
        assertNull("空白 ETag 当没有", cache.etag())
        // 于是下一次会全量下一份，而不是拿一个空 ETag 去协商
        val t = FakeTransport().apply { body = bigBytes(9) }
        cache.ensure(client(t))
        assertFalse(t.requests[0].containsKey("If-None-Match"))
        assertEquals(9.toByte(), cache.modelFile.readBytes()[0])
    }
}

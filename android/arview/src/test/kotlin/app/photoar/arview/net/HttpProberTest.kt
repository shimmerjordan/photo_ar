package app.photoar.arview.net

import app.photoar.arview.Clock
import app.photoar.arview.NetErrorKind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class HttpProberTest {

    /** 只记 GET，其它两个动作探活用不到。 */
    private class PingTransport(
        var status: Int = 200,
        var body: String = """{"ok":true,"version":"0.1.0","serverTime":1730000000000}""",
        var failure: HttpFailure? = null,
    ) : HttpTransport {
        var url: String? = null
        var headers: Map<String, String> = emptyMap()
        var timeoutMs: Int = -1
        var calls = 0

        override fun get(url: String, headers: Map<String, String>, timeoutMs: Int): HttpReply {
            calls++
            this.url = url
            this.headers = headers
            this.timeoutMs = timeoutMs
            failure?.let { throw it }
            return HttpReply(status, body.toByteArray())
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

    /** 每次读时钟前进 [step] 毫秒，用来断言测出来的耗时。 */
    private class StepClock(private var now: Long = 1_000L, private val step: Long = 0L) : Clock {
        override fun nowMs(): Long {
            val v = now
            now += step
            return v
        }
    }

    @Test
    fun `探活打的是 v1 ping 并带上 Bearer 令牌`() {
        val t = PingTransport()
        HttpProber(t, { "sekrit" }, StepClock()).ping("http://10.0.0.9:8770", 1_500)
        assertEquals("http://10.0.0.9:8770/v1/ping", t.url)
        assertEquals("Bearer sekrit", t.headers["Authorization"])
        assertEquals(1_500, t.timeoutMs)
    }

    @Test
    fun `令牌是每次现取的`() {
        // 用户在设置里改了令牌之后，下一次探活必须用新的 —— 构造时抓一份快照
        // 会让「改完令牌立刻刷新」永远显示旧的失败原因。
        var token = "old"
        val t = PingTransport()
        val prober = HttpProber(t, { token }, StepClock())
        prober.ping("http://a", 1_500)
        assertEquals("Bearer old", t.headers["Authorization"])
        token = "new"
        prober.ping("http://a", 1_500)
        assertEquals("Bearer new", t.headers["Authorization"])
    }

    @Test
    fun `base 末尾带斜杠也拼得对`() {
        val t = PingTransport()
        HttpProber(t, { "x" }, StepClock()).ping("http://10.0.0.9:8770/", 1_500)
        assertEquals("http://10.0.0.9:8770/v1/ping", t.url)
    }

    @Test
    fun `通了返回耗时`() {
        val t = PingTransport()
        val ms = HttpProber(t, { "x" }, StepClock(now = 5_000L, step = 37L)).ping("http://a", 1_500)
        assertEquals(37L, ms)
    }

    @Test
    fun `时钟回拨也不会给出负耗时`() {
        val clock = object : Clock {
            private val values = longArrayOf(10_000L, 9_000L)
            private var i = 0
            override fun nowMs(): Long = values[i++]
        }
        assertEquals(0L, HttpProber(PingTransport(), { "x" }, clock).ping("http://a", 1_500))
    }

    @Test
    fun `连不上返回 null 而不是抛异常`() {
        // 在外时 LAN 必然连不上，这是最常走的一条路，不该表现得像异常。
        val t = PingTransport(failure = HttpFailure(NetErrorKind.TIMEOUT, null, "超时"))
        assertNull(HttpProber(t, { "x" }, StepClock()).ping("http://10.0.0.9:8770", 1_500))
    }

    @Test
    fun `URL 不合法也算不通`() {
        val t = PingTransport(
            failure = HttpFailure(NetErrorKind.TRANSPORT, null, "URL 不可用"),
        )
        assertNull(HttpProber(t, { "x" }, StepClock()).ping("好像不是个地址", 1_500))
    }

    @Test
    fun `401 抛出「令牌不对」而不是当成不通`() {
        // 令牌填错时四条通道会全部探不通；只说「不通」会让人去查路由和防火墙，
        // 而问题在设置里那一行。
        val t = PingTransport(status = 401, body = """{"error":"unauthorized"}""")
        try {
            HttpProber(t, { "wrong" }, StepClock()).ping("http://a", 1_500)
            fail("应该抛 ProbeFailed")
        } catch (e: ProbeFailed) {
            assertTrue(e.message!!.contains("令牌不对"))
            assertTrue(e.message!!.contains("401"))
        }
    }

    @Test
    fun `403 也归到令牌不对`() {
        try {
            HttpProber(PingTransport(status = 403), { "x" }, StepClock()).ping("http://a", 1_500)
            fail("应该抛 ProbeFailed")
        } catch (e: ProbeFailed) {
            assertTrue(e.message!!.contains("令牌不对"))
        }
    }

    @Test
    fun `404 说清是「这个地址上没有 photo-ar-server」`() {
        // 端口打错、反代规则没配到都会走到这里，说「不通」会让人查错方向。
        try {
            HttpProber(PingTransport(status = 404), { "x" }, StepClock()).ping("http://a", 1_500)
            fail("应该抛 ProbeFailed")
        } catch (e: ProbeFailed) {
            assertTrue(e.message!!.contains("photo-ar-server"))
        }
    }

    @Test
    fun `5xx 说是服务端出错`() {
        try {
            HttpProber(PingTransport(status = 502), { "x" }, StepClock()).ping("http://a", 1_500)
            fail("应该抛 ProbeFailed")
        } catch (e: ProbeFailed) {
            assertTrue(e.message!!.contains("服务端出错"))
            assertTrue(e.message!!.contains("502"))
        }
    }

    @Test
    fun `其它状态码原样报出来`() {
        try {
            HttpProber(PingTransport(status = 418), { "x" }, StepClock()).ping("http://a", 1_500)
            fail("应该抛 ProbeFailed")
        } catch (e: ProbeFailed) {
            assertEquals("HTTP 418", e.message)
        }
    }

    @Test
    fun `探活不解析响应体`() {
        // §7 要求 ping「极轻」。响应体是什么都不该影响判定 —— 服务端将来往里
        // 加字段，或者反代插了一段 HTML，都不该让一条通着的通道变成不通。
        val t = PingTransport(status = 200, body = "not json at all")
        assertEquals(5L, HttpProber(t, { "x" }, StepClock(step = 5L)).ping("http://a", 1_500))
    }
}

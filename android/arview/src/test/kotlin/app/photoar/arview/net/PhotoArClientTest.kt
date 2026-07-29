package app.photoar.arview.net

import app.photoar.arview.Endpoints
import app.photoar.arview.Hit
import app.photoar.arview.NetErrorKind
import app.photoar.arview.RecognizeOutcome
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class PhotoArClientTest {

    private class Recorded(
        val method: String,
        val url: String,
        val headers: Map<String, String>,
        val timeoutMs: Int,
        val bodySize: Int,
    )

    private class FakeTransport(
        var status: Int = 200,
        var body: String = "{}",
        /** 非 null 时直接抛出，用来模拟连接层错误。 */
        var failure: HttpFailure? = null,
    ) : HttpTransport {
        val calls = ArrayList<Recorded>()

        override fun get(url: String, headers: Map<String, String>, timeoutMs: Int): HttpReply {
            calls += Recorded("GET", url, headers, timeoutMs, 0)
            failure?.let { throw it }
            return HttpReply(status, body.toByteArray())
        }

        override fun postJpeg(
            url: String,
            field: String,
            jpeg: ByteArray,
            headers: Map<String, String>,
            timeoutMs: Int,
        ): HttpReply {
            calls += Recorded("POST", url, headers, timeoutMs, jpeg.size)
            failure?.let { throw it }
            return HttpReply(status, body.toByteArray())
        }

        val last: Recorded get() = calls.last()
    }

    private val endpoints = Endpoints(
        apiBase = "http://10.0.0.9:8770",
        mediaBase = "http://10.0.0.9:8080",
        token = "secret-token",
    )

    private fun client(
        t: HttpTransport,
        via: String? = null,
    ) = PhotoArClient(t, { endpoints }, { via })

    private fun hit() = Hit(
        photoId = "abc",
        inliers = 40,
        printWidthM = 0.152f,
        refAspect = 1.5f,
        imgdbUrl = "/v1/photo/abc/imgdb",
        refThumbUrl = "/v1/photo/abc/thumb",
        mediaUrl = "/v1/photo/abc/media",
        refStale = false,
        latencyMs = 60,
    )

    // ---- 请求构造 ----

    @Test
    fun `识别打到 api 通道并带上 Bearer`() {
        val t = FakeTransport(body = """{"matched":false,"latencyMs":1}""")
        client(t).recognize(ByteArray(1234))
        assertEquals("POST", t.last.method)
        assertEquals("http://10.0.0.9:8770/v1/recognize", t.last.url)
        assertEquals("Bearer secret-token", t.last.headers["Authorization"])
        assertEquals(1234, t.last.bodySize)
    }

    @Test
    fun `识别超时门限是 2 秒`() {
        val t = FakeTransport(body = """{"matched":false}""")
        client(t).recognize(ByteArray(1))
        assertEquals(PhotoArClient.RECOGNIZE_TIMEOUT_MS, t.last.timeoutMs)
        assertEquals(2_000, t.last.timeoutMs)
    }

    @Test
    fun `禁用压缩以免 JPEG 被再压一遍`() {
        val t = FakeTransport(body = """{"matched":false}""")
        client(t).recognize(ByteArray(1))
        assertEquals("identity", t.last.headers["Accept-Encoding"])
    }

    @Test
    fun `有通道标签时写进 X-PhotoAR-Endpoint`() {
        val t = FakeTransport(body = """{"matched":false}""")
        client(t, via = "tailscale").recognize(ByteArray(1))
        assertEquals("tailscale", t.last.headers["X-PhotoAR-Endpoint"])
    }

    @Test
    fun `没有通道标签时不带这个头`() {
        val t = FakeTransport(body = """{"matched":false}""")
        client(t).recognize(ByteArray(1))
        assertTrue(!t.last.headers.containsKey("X-PhotoAR-Endpoint"))
    }

    @Test
    fun `每次调用都重新取 endpoints`() {
        // Phase 3 的 EndpointResolver 会在网络切换时换掉它，客户端不能缓存
        val t = FakeTransport(body = """{"matched":false}""")
        var base = "http://lan:8770"
        val c = PhotoArClient(t, { endpoints.copy(apiBase = base) }, { null })
        c.recognize(ByteArray(1))
        base = "http://tunnel:443"
        c.recognize(ByteArray(1))
        assertEquals("http://lan:8770/v1/recognize", t.calls[0].url)
        assertEquals("http://tunnel:443/v1/recognize", t.calls[1].url)
    }

    @Test
    fun `media 用命中里给的相对路径`() {
        val t = FakeTransport(body = """{"url":"/v1/asset/x/stream","supportsRange":true}""")
        val m = client(t).media(hit())
        assertEquals("GET", t.last.method)
        assertEquals("http://10.0.0.9:8770/v1/photo/abc/media", t.last.url)
        assertTrue(m.playable)
    }

    @Test
    fun `imgdb 走 api 通道`() {
        // imgdb 是小包且与识别同源，不该走媒体通道
        val t = FakeTransport(body = "BINARY")
        val data = client(t).download("/v1/photo/abc/imgdb")
        assertEquals("http://10.0.0.9:8770/v1/photo/abc/imgdb", t.last.url)
        assertEquals(PhotoArClient.DOWNLOAD_TIMEOUT_MS, t.last.timeoutMs)
        assertEquals("BINARY", String(data))
    }

    // ---- 状态码映射 ----

    @Test
    fun `401 映射成 UNAUTHORIZED`() {
        assertKind(NetErrorKind.UNAUTHORIZED, 401, """{"message":"bad token"}""")
    }

    @Test
    fun `403 也映射成 UNAUTHORIZED`() {
        assertKind(NetErrorKind.UNAUTHORIZED, 403, "{}")
    }

    @Test
    fun `500 映射成 SERVER_ERROR`() {
        assertKind(NetErrorKind.SERVER_ERROR, 500, "{}")
    }

    @Test
    fun `503 映射成 SERVER_ERROR`() {
        assertKind(NetErrorKind.SERVER_ERROR, 503, "")
    }

    @Test
    fun `404 映射成 BAD_RESPONSE`() {
        assertKind(NetErrorKind.BAD_RESPONSE, 404, "{}")
    }

    @Test
    fun `错误里带上服务端的 message`() {
        val t = FakeTransport(status = 401, body = """{"message":"token 无效"}""")
        try {
            client(t).recognize(ByteArray(1))
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertTrue("实际：${e.message}", e.message!!.contains("token 无效"))
            assertTrue("要能看出是哪个接口", e.message!!.contains("/v1/recognize"))
            assertEquals(401, e.status)
        }
    }

    @Test
    fun `响应不是 JSON 时映射成 BAD_RESPONSE`() {
        val t = FakeTransport(status = 200, body = "<html>proxy error</html>")
        try {
            client(t).recognize(ByteArray(1))
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
        }
    }

    @Test
    fun `契约破了也是 BAD_RESPONSE 而不是未命中`() {
        // matched=true 却没有 photoId：静默当未命中会变成永远识别不出来的 bug
        val t = FakeTransport(status = 200, body = """{"matched":true,"printWidthM":0.152}""")
        try {
            client(t).recognize(ByteArray(1))
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
        }
    }

    @Test
    fun `传输层的错误原样传出去`() {
        val t = FakeTransport()
        t.failure = HttpFailure(NetErrorKind.TIMEOUT, null, "超时")
        try {
            client(t).recognize(ByteArray(1))
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.TIMEOUT, e.kind)
        }
    }

    @Test
    fun `未命中不抛异常`() {
        val t = FakeTransport(body = """{"matched":false,"reason":"few_inliers","latencyMs":41}""")
        val out = client(t).recognize(ByteArray(1))
        assertEquals("few_inliers", (out as RecognizeOutcome.NoMatch).reason)
    }

    private fun assertKind(expected: NetErrorKind, status: Int, body: String) {
        val t = FakeTransport(status = status, body = body)
        try {
            client(t).recognize(ByteArray(1))
            fail("HTTP $status 应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(expected, e.kind)
        }
    }
}

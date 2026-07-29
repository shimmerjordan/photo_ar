package app.photoar.arview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ApiParseTest {

    // ---- URL 拼接 ----

    @Test
    fun `相对路径套上前缀`() {
        assertEquals(
            "http://10.0.0.9:8770/v1/recognize",
            Endpoints.joinUrl("http://10.0.0.9:8770", "/v1/recognize"),
        )
    }

    @Test
    fun `前缀带尾斜杠也不会拼出双斜杠`() {
        assertEquals(
            "http://10.0.0.9:8770/v1/recognize",
            Endpoints.joinUrl("http://10.0.0.9:8770/", "/v1/recognize"),
        )
    }

    @Test
    fun `相对路径不带头斜杠也能拼`() {
        assertEquals(
            "http://a/v1/x",
            Endpoints.joinUrl("http://a", "v1/x"),
        )
    }

    @Test
    fun `已经是绝对 URL 就原样返回`() {
        val abs = "https://cdn.example.com/x.mp4?sig=1"
        assertEquals(abs, Endpoints.joinUrl("http://10.0.0.9:8770", abs))
    }

    // ---- recognize ----

    @Test
    fun `解析命中`() {
        val json = """
            {"matched":true,"photoId":"3f2a","inliers":47,"printWidthM":0.152,
             "refAspect":1.4986,"imgdbUrl":"/v1/photo/3f2a/imgdb",
             "refThumbUrl":"/v1/photo/3f2a/thumb","mediaUrl":"/v1/photo/3f2a/media",
             "refStale":false,"latencyMs":63}
        """.trimIndent()
        val out = ApiParse.recognize(json) as RecognizeOutcome.Matched
        assertEquals("3f2a", out.hit.photoId)
        assertEquals(47, out.hit.inliers)
        assertEquals(0.152f, out.hit.printWidthM, 1e-6f)
        assertEquals(1.4986f, out.hit.refAspect!!, 1e-4f)
        assertEquals("/v1/photo/3f2a/media", out.hit.mediaUrl)
        assertFalse(out.hit.refStale)
        assertEquals(63, out.hit.latencyMs)
    }

    @Test
    fun `解析未命中`() {
        val out = ApiParse.recognize("""{"matched":false,"reason":"few_inliers","latencyMs":41}""")
        out as RecognizeOutcome.NoMatch
        assertEquals("few_inliers", out.reason)
        assertEquals(41, out.latencyMs)
    }

    @Test
    fun `未命中且没有 reason`() {
        val out = ApiParse.recognize("""{"matched":false,"latencyMs":38}""")
        assertNull((out as RecognizeOutcome.NoMatch).reason)
    }

    @Test
    fun `JSON null 的 reason 解析成 Kotlin null 而不是字符串 null`() {
        // Android 自带 org.json 的 optString(name, null) 会给出 "null"，
        // 这条用例把两边行为的差别钉住。
        val out = ApiParse.recognize("""{"matched":false,"reason":null,"latencyMs":1}""")
        assertNull((out as RecognizeOutcome.NoMatch).reason)
    }

    @Test
    fun `缺 matched 字段按未命中处理`() {
        assertTrue(ApiParse.recognize("""{"latencyMs":1}""") is RecognizeOutcome.NoMatch)
    }

    @Test
    fun `缺 refAspect 时为 null`() {
        val out = ApiParse.recognize(
            """{"matched":true,"photoId":"a","printWidthM":0.152,"latencyMs":1}"""
        ) as RecognizeOutcome.Matched
        assertNull(out.hit.refAspect)
    }

    @Test
    fun `refAspect 为 JSON null 时为 null`() {
        val out = ApiParse.recognize(
            """{"matched":true,"photoId":"a","printWidthM":0.152,"refAspect":null,"latencyMs":1}"""
        ) as RecognizeOutcome.Matched
        assertNull(out.hit.refAspect)
    }

    @Test
    fun `缺 URL 字段时按 photoId 推出默认路径`() {
        val out = ApiParse.recognize(
            """{"matched":true,"photoId":"abc","printWidthM":0.152,"latencyMs":1}"""
        ) as RecognizeOutcome.Matched
        assertEquals("/v1/photo/abc/imgdb", out.hit.imgdbUrl)
        assertEquals("/v1/photo/abc/thumb", out.hit.refThumbUrl)
        assertEquals("/v1/photo/abc/media", out.hit.mediaUrl)
    }

    @Test(expected = ApiParseException::class)
    fun `命中但没有 photoId 是解析错误`() {
        // 不能当未命中：未命中会被静默重试，契约破了必须能看见
        ApiParse.recognize("""{"matched":true,"printWidthM":0.152,"latencyMs":1}""")
    }

    @Test(expected = ApiParseException::class)
    fun `命中但 printWidthM 为零是解析错误`() {
        ApiParse.recognize("""{"matched":true,"photoId":"a","printWidthM":0,"latencyMs":1}""")
    }

    @Test(expected = ApiParseException::class)
    fun `命中但缺 printWidthM 是解析错误`() {
        ApiParse.recognize("""{"matched":true,"photoId":"a","latencyMs":1}""")
    }

    @Test(expected = ApiParseException::class)
    fun `响应不是 JSON 时报解析错误`() {
        ApiParse.recognize("<html>502 Bad Gateway</html>")
    }

    // ---- media ----

    @Test
    fun `解析 nas_serve 的媒体信息`() {
        val json = """
            {"url":"/v1/asset/deadbeef/stream","via":"nas_serve","absolute":false,
             "supportsRange":true,"bytes":1548392,"durationMs":12400,"missing":false,
             "nasPath":"/share/Video/2019/IMG_0421.mov"}
        """.trimIndent()
        val m = ApiParse.media(json)
        assertTrue(m.playable)
        assertFalse(m.absolute)
        assertTrue(m.supportsRange)
        assertEquals(1_548_392L, m.bytes)
        assertEquals(12_400L, m.durationMs)
        assertEquals(
            "http://10.0.0.9:8080/v1/asset/deadbeef/stream",
            m.resolvedUrl(ep("http://10.0.0.9:8080")),
        )
    }

    @Test
    fun `direct_link 的绝对 URL 不再套前缀`() {
        val json = """
            {"url":"https://cdn.example.com/a.mp4?sig=1","via":"direct_link",
             "absolute":true,"supportsRange":true,"bytes":0}
        """.trimIndent()
        val m = ApiParse.media(json)
        assertEquals("https://cdn.example.com/a.mp4?sig=1", m.resolvedUrl(ep("http://10.0.0.9:8080")))
    }

    @Test
    fun `没有 absolute 字段时按 via 兜底`() {
        val m = ApiParse.media("""{"url":"https://x/a.mp4","via":"direct_link"}""")
        assertTrue(m.absolute)
    }

    @Test
    fun `文件丢了不可播`() {
        val m = ApiParse.media("""{"missing":true,"nasPath":"/share/Video/x.mov","url":null}""")
        assertFalse(m.playable)
        assertTrue(m.missing)
        assertEquals("/share/Video/x.mov", m.nasPath)
        assertNull(m.resolvedUrl(ep("http://a")))
    }

    @Test
    fun `没有关联视频时不可播`() {
        val m = ApiParse.media("""{"url":null,"reason":"no_asset"}""")
        assertFalse(m.playable)
        assertEquals("no_asset", m.reason)
    }

    @Test
    fun `空字符串的 url 也当没有`() {
        assertFalse(ApiParse.media("""{"url":""}""").playable)
    }

    @Test
    fun `时长未知时为 null`() {
        assertNull(ApiParse.media("""{"url":"/a","durationMs":null}""").durationMs)
        assertNull(ApiParse.media("""{"url":"/a"}""").durationMs)
    }

    @Test
    fun `不支持 Range 默认为 false`() {
        assertFalse(ApiParse.media("""{"url":"/a"}""").supportsRange)
    }

    // ---- 错误消息 ----

    @Test
    fun `错误消息优先取 message`() {
        assertEquals("token 无效", ApiParse.errorMessage(401, """{"message":"token 无效"}"""))
    }

    @Test
    fun `没有 message 时取 error`() {
        assertEquals("unauthorized", ApiParse.errorMessage(401, """{"error":"unauthorized"}"""))
    }

    @Test
    fun `body 不是 JSON 时退回状态码`() {
        assertEquals("HTTP 502", ApiParse.errorMessage(502, "<html>bad gateway</html>"))
        assertEquals("HTTP 500", ApiParse.errorMessage(500, null))
        assertEquals("HTTP 500", ApiParse.errorMessage(500, "   "))
    }

    private fun ep(mediaBase: String) = Endpoints(
        apiBase = "http://10.0.0.9:8770",
        mediaBase = mediaBase,
        token = "t",
    )
}

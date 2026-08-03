package app.photoar.arview.net

import app.photoar.arview.Endpoints
import app.photoar.arview.Hit
import app.photoar.arview.NetErrorKind
import app.photoar.arview.RecognizeOutcome
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
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
        /** 只有 JSON 请求有；识别的 JPEG 体不留内容。 */
        val jsonBody: String? = null,
    )

    private class FakeTransport(
        var status: Int = 200,
        var body: String = "{}",
        /** 非 null 时直接抛出，用来模拟连接层错误。 */
        var failure: HttpFailure? = null,
        /** 响应头（键小写）。只有模型下载的 ETag 协商用得到。 */
        var headers: Map<String, String> = emptyMap(),
    ) : HttpTransport {
        val calls = ArrayList<Recorded>()

        override fun get(url: String, headers: Map<String, String>, timeoutMs: Int): HttpReply {
            calls += Recorded("GET", url, headers, timeoutMs, 0)
            failure?.let { throw it }
            return reply()
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
            return reply()
        }

        override fun postJson(
            url: String,
            json: String,
            headers: Map<String, String>,
            timeoutMs: Int,
        ): HttpReply {
            calls += Recorded("POST", url, headers, timeoutMs, json.length, json)
            failure?.let { throw it }
            return reply()
        }

        private fun reply() = HttpReply(status, body.toByteArray(), headers)

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

    // ---- 外壳侧接口（Phase 3）----

    @Test
    fun `ping 打的是 v1 ping 且不抛异常只返回布尔`() {
        val t = FakeTransport(body = """{"ok":true}""")
        assertTrue(client(t).ping())
        assertEquals("http://10.0.0.9:8770/v1/ping", t.last.url)

        val bad = FakeTransport(status = 401, body = "{}")
        assertFalse(client(bad).ping())
    }

    @Test
    fun `photos 走 api 通道`() {
        val t = FakeTransport(body = """{"photos":[],"total":0}""")
        client(t).photos()
        assertEquals("http://10.0.0.9:8770/v1/photos", t.last.url)
        assertEquals(PhotoArClient.META_TIMEOUT_MS, t.last.timeoutMs)
    }

    @Test
    fun `fsList 不给路径时不带 query`() {
        val t = FakeTransport(body = """{"path":null,"parent":null,"entries":[]}""")
        client(t).fsList(null)
        assertEquals("http://10.0.0.9:8770/v1/fs/list", t.last.url)
    }

    @Test
    fun `fsList 的路径被 URL 编码`() {
        val t = FakeTransport(body = """{"path":"/a","parent":null,"entries":[]}""")
        client(t).fsList("/share/我的 照片/2024")
        val url = t.last.url
        // 空格与中文都不能裸着进 query；斜杠编成 %2F 由服务端 parse_qs 还原
        assertTrue("裸空格没被编码：$url", !url.contains(" "))
        assertTrue(url.startsWith("http://10.0.0.9:8770/v1/fs/list?path="))
        assertTrue(url.contains("%2Fshare%2F"))
    }

    @Test
    fun `fsThumb 走 api 通道且用下载超时`() {
        val t = FakeTransport(body = "jpegbytes")
        client(t).fsThumb("/share/a.jpg")
        assertEquals(PhotoArClient.DOWNLOAD_TIMEOUT_MS, t.last.timeoutMs)
        assertTrue(t.last.url.startsWith("http://10.0.0.9:8770/v1/fs/thumb?path="))
    }

    @Test
    fun `history 带 limit`() {
        val t = FakeTransport(body = """{"entries":[]}""")
        client(t).history(limit = 30)
        assertEquals("http://10.0.0.9:8770/v1/history?limit=30", t.last.url)
    }

    @Test
    fun `createPhoto 发 JSON 体并用入库超时`() {
        val t = FakeTransport(status = 201, body = """{"photoId":"p1","qualityScore":88}""")
        val out = client(t).createPhoto("/share/a.jpg", "/share/v.mp4", 152.0, "外婆生日")
        assertEquals("POST", t.last.method)
        assertEquals("http://10.0.0.9:8770/v1/photo", t.last.url)
        // 入库要跑 build-db + ffmpeg，超时必须比 META 长得多
        assertEquals(PhotoArClient.INGEST_TIMEOUT_MS, t.last.timeoutMs)
        assertEquals(180_000, PhotoArClient.INGEST_TIMEOUT_MS)
        val body = JSONObject(t.last.jsonBody!!)
        assertEquals("/share/a.jpg", body.getString("refPath"))
        assertEquals("/share/v.mp4", body.getString("videoPath"))
        assertEquals(152.0, body.getDouble("printWidthMm"), 1e-9)
        assertEquals("外婆生日", body.getString("title"))
        assertEquals("p1", out.photoId)
        assertEquals(88, out.qualityScore)
    }

    @Test
    fun `createPhoto 不带视频和标题时这两个字段不出现`() {
        val t = FakeTransport(status = 201, body = """{"photoId":"p1"}""")
        client(t).createPhoto("/share/a.jpg", null, 152.0, null)
        val body = JSONObject(t.last.jsonBody!!)
        assertFalse(body.has("videoPath"))
        assertFalse(body.has("title"))
    }

    @Test
    fun `质量分不达标是 HttpFailure 并带上服务端说的理由`() {
        val t = FakeTransport(
            status = 422,
            body = """{"code":"low_quality","message":"质量分 61 < 75：纹理太少"}""",
        )
        try {
            client(t).createPhoto("/share/a.jpg", null, 152.0, null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
            assertTrue(e.message!!.contains("质量分 61"))
        }
    }

    @Test
    fun `attachVideo 打到照片的 video 子路径`() {
        val t = FakeTransport(body = """{"photoId":"p1","transcoded":true}""")
        val out = client(t).attachVideo("p1", "/share/v.mp4")
        assertEquals("http://10.0.0.9:8770/v1/photo/p1/video", t.last.url)
        assertEquals("/share/v.mp4", JSONObject(t.last.jsonBody!!).getString("videoPath"))
        assertTrue(out.transcoded)
    }

    @Test
    fun `外壳接口的解析错误也归到 BAD_RESPONSE`() {
        val t = FakeTransport(body = "<html>反代把它换成登录页了</html>")
        try {
            client(t).photos()
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
        }
    }

    @Test
    fun `外壳接口也带 via 头`() {
        val t = FakeTransport(body = """{"entries":[]}""")
        client(t, via = "tailscale").history()
        assertEquals("tailscale", t.last.headers["X-PhotoAR-Endpoint"])
    }

    // ---- 登录（Phase 5：服务端换成用户体系）----

    private val loginOk = """{"token":"sess-1","userId":"u1","name":"管理员",
        "role":"admin","grantAll":false,"expiresAt":1700000000000}"""

    @Test
    fun `登录打到 auth login 并且只带名字与口令`() {
        val t = FakeTransport(body = loginOk)
        val r = client(t).login("管理员", "pw")
        assertEquals("POST", t.last.method)
        assertEquals("http://10.0.0.9:8770/v1/auth/login", t.last.url)
        val body = JSONObject(t.last.jsonBody!!)
        assertEquals("管理员", body.getString("name"))
        assertEquals("pw", body.getString("password"))
        assertEquals("sess-1", r.token)
    }

    @Test
    fun `登录不带 Authorization`() {
        // 手上那个 token 很可能正是过期的那一个。带上它换不到任何东西，却让「登录」
        // 这个唯一能修好一切的请求多一个失败可能（反代可能自己校验 bearer）。
        val t = FakeTransport(body = loginOk)
        client(t).login("管理员", "pw")
        assertFalse(t.last.headers.containsKey("Authorization"))
    }

    @Test
    fun `访客留空口令时不发 password 字段`() {
        // 不发与发空串在服务端对 viewer 行为相同，但 admin 那边的报错文案不一样
        // （不发 → "管理员登录必须输口令"，更好懂）。
        val t = FakeTransport(body = loginOk)
        client(t).login("小明", null)
        assertFalse(JSONObject(t.last.jsonBody!!).has("password"))
        client(t).login("小明", "")
        assertFalse(JSONObject(t.last.jsonBody!!).has("password"))
    }

    @Test
    fun `登录超时比普通接口长`() {
        // 服务端验口令要跑一次 scrypt，而那一步串在写锁后面。
        val t = FakeTransport(body = loginOk)
        client(t).login("管理员", "pw")
        assertEquals(PhotoArClient.LOGIN_TIMEOUT_MS, t.last.timeoutMs)
        assertTrue(PhotoArClient.LOGIN_TIMEOUT_MS > PhotoArClient.META_TIMEOUT_MS)
    }

    @Test
    fun `登录 401 是 BAD_CREDENTIALS 而不是 UNAUTHORIZED`() {
        // 这两个的下一步动作正好相反：一个「重输一次」，一个「别再试了」。
        val t = FakeTransport(
            status = 401,
            body = """{"error":"bad_credentials","message":"口令不对"}""",
        )
        try {
            client(t).login("管理员", "wrong")
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_CREDENTIALS, e.kind)
            assertEquals("bad_credentials", e.code)
            assertTrue(e.message!!.contains("口令不对"))
        }
    }

    @Test
    fun `登录 403 unknown_user 是 FORBIDDEN 且带上 code`() {
        val t = FakeTransport(
            status = 403,
            body = """{"error":"unknown_user","message":"没有这个用户：'小名'"}""",
        )
        try {
            client(t).login("小名", null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.FORBIDDEN, e.kind)
            assertEquals("unknown_user", e.code)
            assertFalse("重试无意义", e.kind.retryable)
        }
    }

    @Test
    fun `登录 403 account_disabled 也是 FORBIDDEN`() {
        val t = FakeTransport(
            status = 403,
            body = """{"error":"account_disabled","message":"账号已停用"}""",
        )
        try {
            client(t).login("小明", null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.FORBIDDEN, e.kind)
            assertEquals("account_disabled", e.code)
        }
    }

    @Test
    fun `登录 5xx 仍然是 SERVER_ERROR（重试有意义）`() {
        val t = FakeTransport(status = 503, body = "{}")
        try {
            client(t).login("小明", null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.SERVER_ERROR, e.kind)
            assertTrue(e.kind.retryable)
        }
    }

    @Test
    fun `普通接口的 403 仍然映射成 UNAUTHORIZED`() {
        // 既有行为不能变：普通接口上 401 和 403 对用户都是「换个身份登录」。
        assertKind(NetErrorKind.UNAUTHORIZED, 403, """{"error":"admin_only"}""")
    }

    @Test
    fun `普通接口的错误也把 code 带出来`() {
        val t = FakeTransport(status = 400, body = """{"error":"unsupported_backend"}""")
        try {
            client(t).recognizeFeatures("{}")
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals("unsupported_backend", e.code)
            assertEquals(400, e.status)
        }
    }

    @Test
    fun `me 打到 auth me`() {
        val t = FakeTransport(body = """{"userId":"u1","name":"小明","role":"viewer"}""")
        val m = client(t).me()
        assertEquals("http://10.0.0.9:8770/v1/auth/me", t.last.url)
        assertEquals("Bearer secret-token", t.last.headers["Authorization"])
        assertEquals("小明", m.name)
    }

    @Test
    fun `登出失败不抛异常`() {
        // 用户点的是「退出登录」，本地那份凭证无论如何都要清掉。抛出去的话没网的
        // 用户就退不出登录了 —— 那是个说不通的状态。
        val t = FakeTransport()
        t.failure = HttpFailure(NetErrorKind.TRANSPORT, null, "网络不通")
        assertFalse(client(t).logout())

        val bad = FakeTransport(status = 401, body = "{}")
        assertFalse(client(bad).logout())

        val ok = FakeTransport(status = 204, body = "")
        assertTrue(client(ok).logout())
        assertEquals("http://10.0.0.9:8770/v1/auth/logout", ok.last.url)
    }

    // ---- 端上提特征（Phase 5）----

    @Test
    fun `recognizeFeatures 打到 recognize features 并用识别超时`() {
        val t = FakeTransport(body = """{"matched":false,"latencyMs":9}""")
        client(t).recognizeFeatures("""{"width":640}""")
        assertEquals("POST", t.last.method)
        assertEquals("http://10.0.0.9:8770/v1/recognize/features", t.last.url)
        assertEquals(PhotoArClient.RECOGNIZE_TIMEOUT_MS, t.last.timeoutMs)
        assertEquals("""{"width":640}""", t.last.jsonBody)
        assertEquals("Bearer secret-token", t.last.headers["Authorization"])
    }

    @Test
    fun `recognizeFeatures 与 recognize 共用同一份响应解析`() {
        // 服务端保证两条路的响应形状完全一致，所以命中解析必须是同一份代码 ——
        // 各写一份会长出不同的容错，然后表现为「换了路径之后偶发解析失败」。
        val body = """{"matched":true,"photoId":"p1","inliers":88,"printWidthM":0.152,
            "refAspect":1.5,"latencyMs":31}"""
        val a = client(FakeTransport(body = body)).recognize(ByteArray(1))
        val b = client(FakeTransport(body = body)).recognizeFeatures("{}")
        assertEquals(a, b)
        assertEquals("p1", (b as RecognizeOutcome.Matched).hit.photoId)
    }

    @Test
    fun `recognizeFeatures 的契约破了也是 BAD_RESPONSE`() {
        val t = FakeTransport(body = """{"matched":true}""")
        try {
            client(t).recognizeFeatures("{}")
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
        }
    }

    // ---- 模型下载 ----

    @Test
    fun `fetchModel 打到 model xfeat 并带上 If-None-Match`() {
        val t = FakeTransport(status = 200, body = "onnxbytes")
        client(t).fetchModel("\"etag-1\"")
        assertEquals("GET", t.last.method)
        assertEquals("http://10.0.0.9:8770/v1/model/xfeat", t.last.url)
        assertEquals("\"etag-1\"", t.last.headers["If-None-Match"])
        assertEquals(PhotoArClient.MODEL_TIMEOUT_MS, t.last.timeoutMs)
    }

    @Test
    fun `fetchModel 没有本地 etag 时不带这个头`() {
        val t = FakeTransport(status = 200, body = "onnxbytes")
        client(t).fetchModel(null)
        assertFalse(t.last.headers.containsKey("If-None-Match"))
        client(t).fetchModel("  ")
        assertFalse(t.last.headers.containsKey("If-None-Match"))
    }

    @Test
    fun `fetchModel 的 304 不是失败`() {
        // 304 不在 200..299 里，走 check() 会被当成失败 —— 而它恰恰是最常见的正常结果。
        val t = FakeTransport(status = 304, body = "")
        assertTrue(client(t).fetchModel("\"e\"") is ModelFetch.NotModified)
    }

    @Test
    fun `fetchModel 200 把字节与 ETag 一起带回来`() {
        val t = FakeTransport(status = 200, body = "MODEL", headers = mapOf("etag" to "\"e2\""))
        val r = client(t).fetchModel(null) as ModelFetch.Fresh
        assertEquals("MODEL", String(r.bytes))
        assertEquals("\"e2\"", r.etag)
    }

    @Test
    fun `fetchModel 的 ETag 头大小写不敏感`() {
        val t = FakeTransport(status = 200, body = "M", headers = mapOf("etag" to "\"x\""))
        assertEquals("\"x\"", (client(t).fetchModel(null) as ModelFetch.Fresh).etag)
    }

    @Test
    fun `服务端没有模型时是 404 并带上 model_missing`() {
        // 客户端要按 code 判断「服务端没有模型」→ 静默退回传 JPEG，而不是弹错误。
        val t = FakeTransport(
            status = 404,
            body = """{"error":"model_missing","message":"服务端没有 xfeat.onnx"}""",
        )
        try {
            client(t).fetchModel(null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
            assertEquals("model_missing", e.code)
        }
    }

    // ---- 整库目标（Phase 6）----

    @Test
    fun `targetsManifest 打到 targets manifest 且不做条件请求`() {
        // 服务端对这个接口是 no-store 且不带 ETag（标题 / hasVideo 刻意不在版本号里），
        // 所以带 If-None-Match 只会换回一个「改了但看不到」的 304。
        val t = FakeTransport(body = """{"version":"v1","count":0,"targets":[]}""")
        val m = client(t).targetsManifest()
        assertEquals("GET", t.last.method)
        assertEquals("http://10.0.0.9:8770/v1/targets/manifest", t.last.url)
        assertFalse(t.last.headers.containsKey("If-None-Match"))
        assertEquals("Bearer secret-token", t.last.headers["Authorization"])
        assertEquals("v1", m.version)
    }

    @Test
    fun `targetsDb 200 带回字节与版本`() {
        val t = FakeTransport(
            status = 200,
            body = "IMGDB",
            headers = mapOf("etag" to "\"ab12cd34\""),
        )
        val r = client(t).targetsDb(null) as TargetsDbFetch.Fresh
        assertEquals("http://10.0.0.9:8770/v1/targets/db", t.last.url)
        assertEquals(PhotoArClient.TARGETS_TIMEOUT_MS, t.last.timeoutMs)
        assertEquals("IMGDB", String(r.bytes))
        // 存的是**去掉引号**的那个值，好让它与 manifest 里的 version 直接相等 ——
        // 那是客户端唯一能自己验证「元数据与字节是配好的」的判据。
        assertEquals("ab12cd34", r.version)
    }

    @Test
    fun `targetsDb 带上本地版本做条件请求`() {
        val t = FakeTransport(status = 304, body = "")
        client(t).targetsDb("ab12cd34")
        assertEquals("\"ab12cd34\"", t.last.headers["If-None-Match"])
        // 存下来的值本来就带引号时也不能变成 ""ab""
        client(t).targetsDb("\"ab12cd34\"")
        assertEquals("\"ab12cd34\"", t.last.headers["If-None-Match"])
    }

    @Test
    fun `targetsDb 没有本地版本时不带条件头`() {
        val t = FakeTransport(status = 200, body = "X")
        client(t).targetsDb(null)
        assertFalse(t.last.headers.containsKey("If-None-Match"))
        client(t).targetsDb("  ")
        assertFalse(t.last.headers.containsKey("If-None-Match"))
    }

    @Test
    fun `targetsDb 的 304 不是失败`() {
        val t = FakeTransport(status = 304, body = "")
        assertTrue(client(t).targetsDb("v1") is TargetsDbFetch.NotModified)
    }

    @Test
    fun `targetsDb 的 503 targets_building 是正常状态并带出 Retry-After`() {
        // 服务端把建库放到后台线程 + 503，是为了不让这个请求撞上隧道的响应超时。
        // 客户端把它当失败的话，用户会看到一次「服务器挂了」，而其实过 5 秒就好。
        val t = FakeTransport(
            status = 503,
            body = """{"error":"targets_building","message":"正在构建","version":"v9",
                "retryAfterS":5}""",
            headers = mapOf("retry-after" to "5"),
        )
        val r = client(t).targetsDb("v1") as TargetsDbFetch.Building
        assertEquals(5, r.retryAfterS)
        assertEquals("v9", r.version)
    }

    @Test
    fun `Retry-After 头被剥掉时退回体里那个值`() {
        val t = FakeTransport(
            status = 503,
            body = """{"error":"targets_building","retryAfterS":7}""",
        )
        assertEquals(7, (client(t).targetsDb(null) as TargetsDbFetch.Building).retryAfterS)
    }

    @Test
    fun `Retry-After 是 HTTP 日期时当成没有`() {
        // 规范允许日期格式。解不出秒数不是错误 —— 退避策略自己有默认值。
        val t = FakeTransport(
            status = 503,
            body = """{"error":"targets_building"}""",
            headers = mapOf("retry-after" to "Wed, 21 Oct 2026 07:28:00 GMT"),
        )
        assertNull((client(t).targetsDb(null) as TargetsDbFetch.Building).retryAfterS)
    }

    @Test
    fun `反代自己发的 503 仍然是失败而不是正在建`() {
        // 这是这条路上最要紧的一条：把 Cloudflare / nginx 的 503 当成「正在建」的话，
        // 一次真实的服务中断会被显示成「库正在建」，然后用户一直等着。
        val t = FakeTransport(status = 503, body = "<html>503 Service Unavailable</html>")
        try {
            client(t).targetsDb(null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.SERVER_ERROR, e.kind)
        }
    }

    @Test
    fun `targetsDb 的 404 no_targets 是空而不是失败`() {
        // 一张照片都没被授权（新部署 / 授权被撤）。重试无意义，而本地那份预建库
        // 应该删掉 —— 两件事都和「网络出错」不一样。
        val t = FakeTransport(
            status = 404,
            body = """{"error":"no_targets","message":"你还没有被授权任何照片"}""",
        )
        assertTrue(client(t).targetsDb("v1") is TargetsDbFetch.Empty)
    }

    @Test
    fun `别的 404 仍然是失败`() {
        val t = FakeTransport(status = 404, body = """{"error":"not_found"}""")
        try {
            client(t).targetsDb(null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
        }
    }

    @Test
    fun `targetsDb 的 401 照常映射成 UNAUTHORIZED`() {
        val t = FakeTransport(status = 401, body = """{"message":"登录已过期"}""")
        try {
            client(t).targetsDb(null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.UNAUTHORIZED, e.kind)
        }
    }

    @Test
    fun `targetsDb 拿到 0 字节算失败`() {
        // 存进去的话，下一次 deserialize 会失败，而那个失败会被归因成「服务端的
        // arcoreimg 比端上的 ARCore 新」并永久退回端上现建。
        val t = FakeTransport(status = 200, body = "")
        try {
            client(t).targetsDb(null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
        }
    }

    @Test
    fun `targetsDb 的 ETag 弱校验前缀被剥掉`() {
        val t = FakeTransport(status = 200, body = "X", headers = mapOf("etag" to "W/\"v7\""))
        assertEquals("v7", (client(t).targetsDb(null) as TargetsDbFetch.Fresh).version)
    }

    @Test
    fun `fetchModel 拿到 0 字节算失败`() {
        // 0 字节写进缓存的话，下一次会拿它去 InferenceSession，然后被归因成
        // 「这台机器不支持 ONNX」并永久回退。
        val t = FakeTransport(status = 200, body = "")
        try {
            client(t).fetchModel(null)
            fail("应该抛出")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.BAD_RESPONSE, e.kind)
        }
    }
}

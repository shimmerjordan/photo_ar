package app.photoar.arview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
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

    @Test
    fun `命中但 printWidthM 为零或缺失，按未知处理而不是解析错误`() {
        // 这两条原来都抛 ApiParseException。改了是因为「照片实际尺寸不知道」是常态，
        // 而不知道并不妨碍贴合 —— 四边形的尺度取 ARCore 量的 extentX（Geometry.quadSize）。
        // 当成解析错误的后果是这张照片在客户端整条被丢掉：服务端认得出来，手机上却
        // 什么都不发生，而且日志里看起来像是「没识别到」。
        for (json in listOf(
            """{"matched":true,"photoId":"a","printWidthM":0,"latencyMs":1}""",
            """{"matched":true,"photoId":"a","latencyMs":1}""",
            """{"matched":true,"photoId":"a","printWidthM":-1,"latencyMs":1}""",
        )) {
            val hit = (ApiParse.recognize(json) as RecognizeOutcome.Matched).hit
            assertEquals("a", hit.photoId)
            assertEquals("未知统一归成 0：$json", 0f, hit.printWidthM, 1e-9f)
        }
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

    // ---- 登录响应 ----

    @Test
    fun `登录响应的全部字段`() {
        val r = ApiParse.login(
            """{"token":"abc.def","userId":"u1","name":"管理员","role":"admin",
               "grantAll":false,"expiresAt":1700000000000}""",
        )
        assertEquals("abc.def", r.token)
        assertEquals("u1", r.userId)
        assertEquals("管理员", r.name)
        assertEquals("admin", r.role)
        assertFalse(r.grantAll)
        assertEquals(1_700_000_000_000L, r.expiresAt)
        assertTrue(r.isAdmin)
    }

    @Test
    fun `运维凭证的 userId 与 expiresAt 都是 JSON null`() {
        // 服务端那条路（PHOTOAR_TOKEN）如实返回 null：它不对应任何一个人，也没有
        // 服务端状态可过期。两个都必须当合法取值，否则那条路在客户端崩掉。
        val r = ApiParse.login(
            """{"token":"t","userId":null,"name":"[运维凭证]","role":"admin",
               "grantAll":true,"expiresAt":null}""",
        )
        assertNull(r.userId)
        assertNull("null 不能变成 0 —— 那会被读成 1970 年就过期", r.expiresAt)
        assertTrue(r.grantAll)
    }

    @Test
    fun `登录响应没有 token 是契约破了而不是登录失败`() {
        // 当成登录失败会提示「口令不对」，而真正的原因是反代指到了别的服务上。
        try {
            ApiParse.login("""{"name":"小明","role":"viewer"}""")
            fail("应该抛出")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("token"))
        }
    }

    @Test
    fun `登录响应没有 name 也是解析错误`() {
        try {
            ApiParse.login("""{"token":"t"}""")
            fail("应该抛出")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("name"))
        }
    }

    @Test
    fun `角色缺失时按访客处理而不是管理员`() {
        // 猜错的方向必须是「权限更小」。
        val r = ApiParse.login("""{"token":"t","name":"小明"}""")
        assertEquals(LoginResult.ROLE_VIEWER, r.role)
        assertFalse(r.isAdmin)
    }

    // ---- me ----

    @Test
    fun `me 的字段`() {
        val m = ApiParse.me(
            """{"userId":"u2","name":"小明","role":"viewer","grantAll":true,"isAdmin":false}""",
        )
        assertEquals("u2", m.userId)
        assertEquals("小明", m.name)
        assertTrue(m.grantAll)
        assertFalse(m.isAdmin)
    }

    @Test
    fun `me 缺 isAdmin 时按 role 兜底`() {
        assertTrue(ApiParse.me("""{"name":"管理员","role":"admin"}""").isAdmin)
        assertFalse(ApiParse.me("""{"name":"小明","role":"viewer"}""").isAdmin)
    }

    // ---- 整库目标的 manifest（Phase 6）----

    /** 服务端 `targets.TargetStore.manifest` 的形状，照抄不简化。 */
    private val manifestJson = """
        {"version":"ab12cd34ef567890","count":2,"overflow":3,"maxTargets":1000,
         "building":false,
         "targets":[
           {"photoId":"p1","printWidthM":0.152,"refAspect":1.5,"fitMode":"contain",
            "title":"外婆生日","hasVideo":true,
            "mediaUrl":"/v1/photo/p1/media","imgdbUrl":"/v1/photo/p1/imgdb"},
           {"photoId":"p2","printWidthM":0.089,"refAspect":null,"fitMode":"cover",
            "title":null,"hasVideo":false,
            "mediaUrl":"/v1/photo/p2/media","imgdbUrl":"/v1/photo/p2/imgdb"}]}
    """.trimIndent()

    @Test
    fun `manifest 把版本与两条目标解出来`() {
        val m = ApiParse.targetsManifest(manifestJson)
        assertEquals("ab12cd34ef567890", m.version)
        assertEquals(2, m.count)
        assertEquals(3, m.overflow)
        assertEquals(1000, m.maxTargets)
        assertFalse(m.building)
        assertEquals(listOf("p1", "p2"), m.targets.map { it.photoId })
        val p1 = m.targets[0]
        assertEquals(0.152f, p1.printWidthM, 1e-6f)
        assertEquals(1.5f, p1.refAspect!!, 1e-6f)
        assertEquals("contain", p1.fitMode)
        assertEquals("外婆生日", p1.title)
        assertTrue(p1.hasVideo)
        assertEquals("/v1/photo/p1/media", p1.mediaUrl)
        assertEquals("/v1/photo/p1/imgdb", p1.imgdbUrl)
    }

    @Test
    fun `manifest 里 refAspect 与 title 的 null 是合法取值`() {
        // 服务端刻意让这两个键「总是在」，值可能是 null（尺寸探不到 / 没起标题）。
        // 用 optString 读的话，Android 那个 org.json 会把 JSON null 读成字符串 "null"，
        // 于是这张照片的标题在界面上就是「null」。
        val p2 = ApiParse.targetsManifest(manifestJson).targets[1]
        assertNull(p2.refAspect)
        assertNull(p2.title)
        assertFalse(p2.hasVideo)
    }

    @Test
    fun `manifest 没有 version 就是解析失败`() {
        // version 是「这份元数据和那个库文件是配好的」的唯一判据。静默给个空串的话，
        // 每次 ETag 协商都会拿到 200，全体客户端每次同步重下整个库。
        try {
            ApiParse.targetsManifest("""{"count":0,"targets":[]}""")
            fail("应该抛出")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("version"))
        }
    }

    @Test
    fun `manifest 没有 targets 数组也是解析失败`() {
        try {
            ApiParse.targetsManifest("""{"version":"aa"}""")
            fail("应该抛出")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("targets"))
        }
    }

    @Test
    fun `空 manifest 是合法的`() {
        // 新部署、或者一个还没被授权任何照片的 viewer。服务端会给一个确定的版本号
        // （空清单的哈希），所以 ETag 语义在这个状态下仍然成立。
        val m = ApiParse.targetsManifest("""{"version":"e3b0c442","count":0,"targets":[]}""")
        assertEquals("e3b0c442", m.version)
        assertEquals(0, m.count)
        assertTrue(m.targets.isEmpty())
    }

    @Test
    fun `printWidthM 不可用的那一条保留，宽度记 0`() {
        // 原来是跳过。跳过的代价是这张照片**永远进不了端侧库**，离线命中对它彻底失效，
        // 每次都得往服务端跑一趟 —— 而宽度未知完全能正常贴合（Geometry.quadSize）。
        val m = ApiParse.targetsManifest(
            """{"version":"v1","count":3,"targets":[
                 {"photoId":"good","printWidthM":0.1},
                 {"photoId":"zero","printWidthM":0},
                 {"photoId":"missing"}]}""",
        )
        assertEquals(listOf("good", "zero", "missing"), m.targets.map { it.photoId })
        assertEquals(0.1f, m.targets[0].printWidthM, 1e-9f)
        assertEquals(0f, m.targets[1].printWidthM, 1e-9f)
        assertEquals(0f, m.targets[2].printWidthM, 1e-9f)
        // count 是**服务端对那个库的陈述**，不被本地解析结果改写
        assertEquals(3, m.count)
    }

    @Test
    fun `manifest 里缺 URL 时按约定补出来`() {
        val m = ApiParse.targetsManifest(
            """{"version":"v1","targets":[{"photoId":"p9","printWidthM":0.1}]}""",
        )
        assertEquals("/v1/photo/p9/media", m.targets[0].mediaUrl)
        assertEquals("/v1/photo/p9/imgdb", m.targets[0].imgdbUrl)
    }

    @Test
    fun `manifest 的 building 为真时如实解出来`() {
        // 客户端据此知道「现在去拿 db 会 503」，那个 503 于是不需要猜原因。
        val m = ApiParse.targetsManifest(
            """{"version":"v1","count":1,"building":true,"targets":[]}""",
        )
        assertTrue(m.building)
    }

    // ---- error code ----

    @Test
    fun `错误码单独取出来`() {
        assertEquals(
            "unknown_user",
            ApiParse.errorCode("""{"error":"unknown_user","message":"没有这个用户"}"""),
        )
    }

    @Test
    fun `没有错误码时返回 null 而不是空串`() {
        // 空串会被 `code == null` 的分支漏掉，然后走进「按 code 分岔」的默认支。
        assertNull(ApiParse.errorCode("""{"message":"就一句话"}"""))
        assertNull(ApiParse.errorCode("""{"error":null}"""))
        assertNull(ApiParse.errorCode("""{"error":""}"""))
        assertNull(ApiParse.errorCode("<html>nginx</html>"))
        assertNull(ApiParse.errorCode(null))
        assertNull(ApiParse.errorCode("   "))
    }

    private fun ep(mediaBase: String) = Endpoints(
        apiBase = "http://10.0.0.9:8770",
        mediaBase = mediaBase,
        token = "t",
    )
}

package app.photoar.arview.cache

import app.photoar.arview.Endpoints
import app.photoar.arview.FakeClock
import app.photoar.arview.NetErrorKind
import app.photoar.arview.net.HttpFailure
import app.photoar.arview.net.HttpReply
import app.photoar.arview.net.HttpTransport
import app.photoar.arview.net.PhotoArClient
import java.io.File
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 一整轮同步。用假 transport 把「下到第 3 条断网」「token 过期」「某张图坏了」
 * 这些真机上要拔网线才造得出的情况全跑一遍。
 */
class CacheSyncTest {

    private lateinit var root: File
    private lateinit var cache: PhotoCache
    private lateinit var transport: FakeTransport
    private lateinit var client: PhotoArClient
    private val clock = FakeClock()

    /**
     * 按 URL 后缀分派的假 transport。
     *
     * 服务端那几个响应的形状照 §7 抄，不简化 —— 简化过的 JSON 测不出解析层的问题。
     */
    private class FakeTransport : HttpTransport {
        /** photoId → 是否有视频。列表接口按它生成响应。 */
        val photos = LinkedHashMap<String, Boolean>()
        var printWidthM = 0.152
        var refStale = false

        /** 命中这些子串的请求抛错。 */
        val failOn = ArrayList<Pair<String, HttpFailure>>()

        val gets = ArrayList<String>()
        var thumbBytes = 60_000
        var videoBytes = 1_500_000
        var mediaDurationMs: Long? = 12_400L
        var mediaMissing = false
        var mediaAbsolute = false

        // ---- 整库目标（Phase 6）。默认没人问，因为 CacheSync 的 targets 默认是 null ----

        /** 服务端那套的版本号。 */
        var targetsVersion = "v1"
        var targetsOverflow = 0
        var targetsMaxTargets = 1000

        /** manifest 里那条的标题。用来验「304 时元数据照样更新」。 */
        var targetsTitle = "外婆生日"

        /** 库接口还要回几次 503（模拟服务端正在建）。 */
        var buildingRounds = 0

        /** 非 null 时 db 接口的 ETag 用这个值 —— 用来造「manifest 与库配不上」。 */
        var dbEtag: String? = null

        var targetsDbBytes = 4_096

        /** 非 null 时 db 接口直接回这个状态码与体。 */
        var dbFailure: Pair<Int, String>? = null

        /** db 接口每次收到的 If-None-Match。 */
        val dbConditions = ArrayList<String?>()

        override fun get(url: String, headers: Map<String, String>, timeoutMs: Int): HttpReply {
            gets.add(url)
            failOn.firstOrNull { url.contains(it.first) }?.let { throw it.second }
            // 「/stream」要排在「/media」前面：绝对直链的主机名里就带 media，
            // 按包含关系匹配会把视频请求当成元数据请求，返回一段 JSON 当视频。
            return when {
                url.endsWith("/v1/photos") -> json(photosJson())
                url.endsWith("/v1/targets/manifest") -> json(manifestJson())
                url.endsWith("/v1/targets/db") -> targetsDb(headers)
                url.endsWith("/stream") || url.endsWith(".mp4") ->
                    HttpReply(200, ByteArray(videoBytes) { 2 })
                url.endsWith("/media") -> json(mediaJson())
                url.endsWith("/thumb") -> HttpReply(200, ByteArray(thumbBytes) { 1 })
                else -> HttpReply(404, """{"error":"not found"}""".toByteArray())
            }
        }

        private fun manifestJson(): String {
            val items = photos.keys.joinToString(",") { id ->
                """
                {"photoId":"$id","printWidthM":$printWidthM,"refAspect":1.5,
                 "fitMode":"contain","title":"$targetsTitle",
                 "hasVideo":${photos[id]},
                 "mediaUrl":"/v1/photo/$id/media","imgdbUrl":"/v1/photo/$id/imgdb"}
                """.trimIndent()
            }
            return """
                {"version":"$targetsVersion","count":${photos.size},
                 "overflow":$targetsOverflow,"maxTargets":$targetsMaxTargets,
                 "building":${buildingRounds > 0},"targets":[$items]}
            """.trimIndent()
        }

        private fun targetsDb(headers: Map<String, String>): HttpReply {
            dbConditions.add(headers["If-None-Match"])
            dbFailure?.let { (status, body) -> return HttpReply(status, body.toByteArray()) }
            if (buildingRounds > 0) {
                buildingRounds--
                return HttpReply(
                    503,
                    """{"error":"targets_building","version":"$targetsVersion",
                        "retryAfterS":5}""".toByteArray(),
                    mapOf("retry-after" to "5"),
                )
            }
            val etag = dbEtag ?: targetsVersion
            if (headers["If-None-Match"] == "\"$etag\"") {
                return HttpReply(304, ByteArray(0), mapOf("etag" to "\"$etag\""))
            }
            return HttpReply(
                200,
                ByteArray(targetsDbBytes) { 9 },
                mapOf("etag" to "\"$etag\""),
            )
        }

        private fun json(s: String) = HttpReply(200, s.toByteArray())

        private fun photosJson(): String {
            val items = photos.entries.joinToString(",") { (id, hasVideo) ->
                """
                {"photoId":"$id","title":"$id","printWidthM":$printWidthM,
                 "qualityScore":88,"refAspect":1.5,
                 "refThumbUrl":"/v1/photo/$id/thumb","hasVideo":$hasVideo,
                 "refStale":$refStale,"createdAt":1730000000000}
                """.trimIndent()
            }
            return """{"photos":[$items],"total":${photos.size}}"""
        }

        private fun mediaJson(): String {
            val url = if (mediaAbsolute) "http://media.example.com/x.mp4" else "/v1/asset/aa/stream"
            return """
                {"url":${if (mediaMissing) "null" else "\"$url\""},
                 "via":"${if (mediaAbsolute) "direct_link" else "nas_serve"}",
                 "absolute":$mediaAbsolute,"supportsRange":true,
                 "bytes":$videoBytes,
                 "durationMs":${mediaDurationMs ?: "null"},
                 "missing":$mediaMissing,
                 "nasPath":"/share/Video/x.mov"}
            """.trimIndent()
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

    @Before
    fun setUp() {
        root = File.createTempFile("photoar-sync", "").let {
            it.delete()
            it.mkdirs()
            it
        }
        cache = PhotoCache(root).load()
        transport = FakeTransport()
        client = PhotoArClient(
            transport = transport,
            endpoints = {
                Endpoints(
                    apiBase = "https://ar.example.com",
                    mediaBase = "http://192.168.1.9:8848",
                    token = "tok",
                )
            },
        )
    }

    @After
    fun tearDown() {
        root.deleteRecursively()
    }

    private fun sync(
        spec: CacheSpec = CacheSpec(),
        rebuild: (List<CachedPhoto>) -> CacheSync.RebuildResult = {
            CacheSync.RebuildResult(accepted = it.size)
        },
        /** 非 null 时才跑「拉服务端预建整库目标」那一步（默认关，见 [CacheSync]）。 */
        targets: ServerTargetsStore? = null,
    ): CacheSync.Result = CacheSync(
        client,
        cache,
        clock,
        spec,
        targets = targets,
        // 单测里不真睡：503 那条路要重试好几轮，真睡就是把一条用例拖成半分钟。
        sleep = { slept.add(it) },
        rebuildTargetDb = rebuild,
    ).sync()

    /** 每次「等一会儿再问」的毫秒数。503 那条路的退避靠它验。 */
    private val slept = ArrayList<Long>()

    // ---- 首轮 ----

    @Test
    fun `首轮把缩略图和视频都下下来`() {
        transport.photos["a"] = true
        transport.photos["b"] = false

        val r = sync()

        assertEquals(2, r.thumbsDownloaded)
        assertEquals(1, r.videosDownloaded)
        assertEquals(emptyList<String>(), r.failed)
        assertNull(r.stoppedBy)
        assertEquals(60_000L * 2 + 1_500_000L, r.bytesDownloaded)
        assertEquals(2, cache.stats().withThumb)
        assertEquals(1, cache.stats().withVideo)
    }

    @Test
    fun `首轮之后索引已经落盘`() {
        transport.photos["a"] = false
        sync()
        // 不 flush 的话进程被杀就白下了一轮
        assertEquals(1, PhotoCache(root).load().stats().withThumb)
    }

    @Test
    fun `视频时长从 media 响应里带下来`() {
        // 时长是给界面显示用的；缓存命中时不再请求 media，这里不记就永远没有了
        transport.photos["a"] = true
        sync()
        assertEquals(12_400L, cache.byId("a")!!.videoDurationMs)
    }

    @Test
    fun `第二轮什么都不做`() {
        transport.photos["a"] = true
        sync()
        transport.gets.clear()

        val r = sync()
        assertTrue(r.plan.empty)
        assertFalse(r.didWork)
        // 只打了一次列表接口，没有多余下载
        assertEquals(listOf("https://ar.example.com/v1/photos"), transport.gets)
    }

    @Test
    fun `缩略图排在视频前面下`() {
        // 中途断网时先保住「能认出来」，视频只影响「认出来之后有没有东西放」
        transport.photos["a"] = true
        transport.photos["b"] = true
        sync()
        val firstVideo = transport.gets.indexOfFirst { it.contains("/media") }
        val lastThumb = transport.gets.indexOfLast { it.contains("/thumb") }
        assertTrue("视频在缩略图之前下了", lastThumb < firstVideo)
    }

    @Test
    fun `视频走 media 通道而不是 api 通道`() {
        // 一条视频 1.5–3MB，从隧道拉是给 Cloudflare 白送流量
        transport.photos["a"] = true
        sync()
        val stream = transport.gets.single { it.contains("/stream") }
        assertTrue(stream, stream.startsWith("http://192.168.1.9:8848"))
    }

    @Test
    fun `直链视频不套 mediaBase 前缀`() {
        transport.photos["a"] = true
        transport.mediaAbsolute = true
        sync()
        assertTrue(transport.gets.any { it == "http://media.example.com/x.mp4" })
    }

    // ---- 单条失败不中断整轮 ----

    @Test
    fun `某张缩略图 404 只跳过它自己`() {
        transport.photos["good"] = false
        transport.photos["bad"] = false
        transport.failOn += "bad/thumb" to HttpFailure(NetErrorKind.BAD_RESPONSE, 404, "没有")

        val r = sync()
        assertEquals(1, r.thumbsDownloaded)
        assertEquals(listOf("bad"), r.failed)
        assertNull(r.stoppedBy)
        assertTrue(cache.byId("good")!!.usableAsTarget)
        // 失败的那条留在索引里但字节为 0，下一轮会重排进重下
        assertFalse(cache.byId("bad")!!.usableAsTarget)
    }

    @Test
    fun `失败过的照片下一轮会重试`() {
        transport.photos["bad"] = false
        transport.failOn += "bad/thumb" to HttpFailure(NetErrorKind.BAD_RESPONSE, 500, "转码中")
        sync()

        transport.failOn.clear()
        val r = sync()
        assertEquals(1, r.thumbsDownloaded)
        assertTrue(cache.byId("bad")!!.usableAsTarget)
    }

    @Test
    fun `视频取不到时缩略图仍然算成功`() {
        transport.photos["a"] = true
        transport.failOn += "/media" to HttpFailure(NetErrorKind.SERVER_ERROR, 500, "转码失败")

        val r = sync()
        assertEquals(1, r.thumbsDownloaded)
        assertEquals(0, r.videosDownloaded)
        assertEquals(listOf("a"), r.failed)
        // 离线识别不依赖视频
        assertTrue(cache.byId("a")!!.usableAsTarget)
    }

    @Test
    fun `服务端说视频不可用时不写出零字节文件`() {
        // 写出 0 字节的 mp4 会让 videoCached 变假真 —— 不，会让它为 false，
        // 但文件留着就是孤儿；更要紧的是别让播放器拿到一个空文件
        transport.photos["a"] = true
        transport.mediaMissing = true

        val r = sync()
        assertEquals(0, r.videosDownloaded)
        assertFalse(File(root, "offline/videos/a.mp4").exists())
        assertNull(cache.localVideoUrl("a"))
    }

    // ---- 401 与断网：立刻停 ----

    @Test
    fun `令牌无效时立刻停不再白试 199 条`() {
        (1..5).forEach { transport.photos["p$it"] = false }
        transport.failOn += "/thumb" to HttpFailure(NetErrorKind.UNAUTHORIZED, 401, "未授权")

        val r = sync()
        assertNotNull(r.stoppedBy)
        assertTrue(r.stoppedBy!!.contains("令牌"))
        assertEquals(1, r.failed.size)
        // 关键：只打了一次 thumb，而不是五次
        assertEquals(1, transport.gets.count { it.contains("/thumb") })
    }

    @Test
    fun `断网时立刻停`() {
        (1..5).forEach { transport.photos["p$it"] = false }
        transport.failOn += "/thumb" to HttpFailure(NetErrorKind.TRANSPORT, null, "unreachable")

        val r = sync()
        assertTrue(r.stoppedBy!!.contains("连不上"))
        assertEquals(1, transport.gets.count { it.contains("/thumb") })
    }

    @Test
    fun `超时也立刻停`() {
        (1..3).forEach { transport.photos["p$it"] = false }
        transport.failOn += "/thumb" to HttpFailure(NetErrorKind.TIMEOUT, null, "timeout")
        assertTrue(sync().stoppedBy!!.contains("超时"))
    }

    @Test
    fun `中途断网时已下好的部分保住`() {
        transport.photos["a"] = false
        sync()
        transport.photos["b"] = false
        transport.failOn += "b/thumb" to HttpFailure(NetErrorKind.TRANSPORT, null, "unreachable")

        val r = sync()
        assertNotNull(r.stoppedBy)
        assertTrue(cache.byId("a")!!.usableAsTarget)
        assertEquals(1, PhotoCache(root).load().stats().withThumb)
    }

    @Test
    fun `拉列表就失败时直接抛`() {
        // 这一步没有「部分成功」可言，静默返回空结果会让界面显示「同步完成」
        transport.failOn += "/v1/photos" to HttpFailure(NetErrorKind.TRANSPORT, null, "unreachable")
        try {
            sync()
            org.junit.Assert.fail("该抛 HttpFailure")
        } catch (e: HttpFailure) {
            assertEquals(NetErrorKind.TRANSPORT, e.kind)
        }
    }

    // ---- 删除与淘汰 ----

    @Test
    fun `服务端删掉的照片本轮就清掉`() {
        transport.photos["a"] = true
        transport.photos["b"] = false
        sync()

        transport.photos.remove("a")
        val r = sync()
        assertEquals(1, r.photosDropped)
        assertNull(cache.byId("a"))
        assertFalse(File(root, "offline/thumbs/a.jpg").exists())
        assertFalse(File(root, "offline/videos/a.mp4").exists())
    }

    @Test
    fun `视频超预算时淘汰掉的只是视频`() {
        transport.photos["a"] = true
        transport.photos["b"] = true
        transport.videoBytes = 1_000_000
        // 同步两轮：未知大小第一轮按 min(单条上限, 预算) 估（10MB 的预算只排得下
        // 一条），下完拿到实际 1MB 这个样本，第二轮才收敛到「放得下十条」。
        // 这不是缺陷而是刻意的——空缓存时宁可少下一轮，也不要一次排下装不下的量
        // 然后下完就被淘汰（白吃流量）。见 CachePlanner.estimateVideoBytes。
        sync(CacheSpec(maxVideoBytes = 10_000_000))
        sync(CacheSpec(maxVideoBytes = 10_000_000))
        assertEquals(2, cache.stats().withVideo)

        // 预算收紧到只放得下一条
        val r = sync(CacheSpec(maxVideoBytes = 1_500_000))
        assertEquals(1, r.videosDropped)
        assertEquals(0, r.photosDropped)
        assertEquals(1, cache.stats().withVideo)
        // 缩略图一张都没少 —— 离线识别能力不受影响
        assertEquals(2, cache.stats().withThumb)
    }

    @Test
    fun `先删后下不会在磁盘上短暂超预算`() {
        // 顺序反了，在只剩几十兆的手机上那一下就是写失败
        transport.photos["old"] = true
        transport.videoBytes = 1_000_000
        sync(CacheSpec(maxVideoBytes = 10_000_000))

        transport.photos["new"] = true
        val gets = transport.gets
        gets.clear()
        sync(CacheSpec(maxVideoBytes = 1_200_000))
        // 淘汰发生在任何下载之前：这一轮里 new 根本不该被下（预算已被 old 占满）
        assertEquals(0, gets.count { it.contains("/stream") })
    }

    @Test
    fun `换了视频会把旧的扔掉重下`() {
        transport.photos["a"] = true
        sync()
        val before = cache.byId("a")!!.videoBytes

        // printWidthM 变了 → changedOnServer 为真
        transport.printWidthM = 0.102
        transport.videoBytes = 999_000
        val r = sync()
        assertEquals(1, r.videosDropped)
        assertEquals(1, r.videosDownloaded)
        assertEquals(999_000L, cache.byId("a")!!.videoBytes)
        assertTrue(before != cache.byId("a")!!.videoBytes)
        assertEquals(0.102f, cache.byId("a")!!.printWidthM, 0f)
    }

    // ---- ARCore 库重建 ----

    @Test
    fun `新下了缩略图就重建库`() {
        transport.photos["a"] = false
        var built: List<CachedPhoto>? = null
        CacheSync(client, cache, clock, CacheSpec()) {
            built = it
            CacheSync.RebuildResult(accepted = it.size)
        }.sync()
        assertEquals(listOf("a"), built!!.map { it.photoId })
    }

    @Test
    fun `什么都没变时不重建库`() {
        transport.photos["a"] = false
        sync()
        var builds = 0
        CacheSync(client, cache, clock, CacheSpec()) {
            builds++
            CacheSync.RebuildResult(accepted = it.size)
        }.sync()
        // 重建 200 张要几秒，无事可做时白跑一遍不可接受
        assertEquals(0, builds)
    }

    @Test
    fun `被 ARCore 拒掉的记进索引下轮不再进库`() {
        transport.photos["good"] = false
        transport.photos["bad"] = false
        val r = CacheSync(client, cache, clock, CacheSpec()) {
            CacheSync.RebuildResult(rejected = listOf("bad"), accepted = it.size - 1)
        }.sync()

        assertEquals(1, r.rejected)
        assertFalse(cache.byId("bad")!!.usableAsTarget)
        assertTrue(cache.byId("good")!!.usableAsTarget)

        // 下一轮建库时 bad 不在名单里
        transport.photos["extra"] = false
        var built: List<CachedPhoto>? = null
        CacheSync(client, cache, clock, CacheSpec()) {
            built = it
            CacheSync.RebuildResult(accepted = it.size)
        }.sync()
        assertFalse(built!!.any { it.photoId == "bad" })
    }

    @Test
    fun `建库名单只含真下到缩略图的那些`() {
        transport.photos["good"] = false
        transport.photos["bad"] = false
        transport.failOn += "bad/thumb" to HttpFailure(NetErrorKind.BAD_RESPONSE, 404, "没有")

        var built: List<CachedPhoto>? = null
        CacheSync(client, cache, clock, CacheSpec()) {
            built = it
            CacheSync.RebuildResult(accepted = it.size)
        }.sync()
        assertEquals(listOf("good"), built!!.map { it.photoId })
    }

    // ---- 进度 ----

    @Test
    fun `进度回调数得上要下的总数`() {
        transport.photos["a"] = true
        transport.photos["b"] = false
        val steps = ArrayList<Pair<Int, Int>>()
        CacheSync(client, cache, clock, CacheSpec()) { CacheSync.RebuildResult(accepted = it.size) }
            .sync { done, total, _ -> steps.add(done to total) }

        // 两张缩略图 + 一条视频
        assertEquals(3, steps.first().second)
        assertEquals(listOf(0, 1, 2), steps.map { it.first })
    }

    @Test
    fun `记下这一轮花了多久`() {
        transport.photos["a"] = false
        val slow = object : app.photoar.arview.Clock {
            private var n = 1_000L
            override fun nowMs(): Long {
                n += 500
                return n
            }
        }
        val r = CacheSync(client, cache, slow, CacheSpec()) {
            CacheSync.RebuildResult(accepted = it.size)
        }.sync()
        assertTrue(r.elapsedMs > 0)
    }

    // ---- 服务端预建的整库目标（Phase 6）----

    private fun store() = ServerTargetsStore(cache)

    @Test
    fun `没接 store 时这一步整个不跑`() {
        // 既有那批用例全走这条路，所以它们的请求序列一个字都不该变。
        transport.photos["a"] = false
        val r = sync()
        assertEquals(CacheSync.TargetsStatus.SKIPPED, r.prebuilt.status)
        assertFalse(transport.gets.any { it.contains("/v1/targets") })
    }

    @Test
    fun `首轮把预建库下下来并落盘`() {
        transport.photos["a"] = false
        transport.photos["b"] = true
        val s = store()
        val r = sync(targets = s)

        assertEquals(CacheSync.TargetsStatus.DOWNLOADED, r.prebuilt.status)
        assertEquals("v1", r.prebuilt.version)
        assertEquals(2, r.prebuilt.count)
        assertEquals(4_096L, r.prebuilt.bytes)
        assertEquals(4_096L, s.bytes)
        assertNotNull("必须能装 —— 库字节和元数据都在", s.installable())
        // manifest 也整份存下来了：那些「预建库有、端侧没缓存」的照片靠它查元数据
        assertEquals(listOf("a", "b"), s.snapshot()!!.entries.map { it.photoId })
    }

    @Test
    fun `manifest 排在库字节之前取`() {
        // manifest 那个请求在服务端会顺手把构建踢起来（它自己不等）。反过来的话第一次
        // 一定是 503，而那时候构建才刚开始 —— 白等一个 Retry-After 周期。
        transport.photos["a"] = false
        sync(targets = store())
        val manifestAt = transport.gets.indexOfFirst { it.endsWith("/v1/targets/manifest") }
        val dbAt = transport.gets.indexOfFirst { it.endsWith("/v1/targets/db") }
        assertTrue("manifest 没排在前面", manifestAt in 0 until dbAt)
    }

    @Test
    fun `预建库排在缩略图之前下`() {
        // 它是离线识别真正的地基（服务端拿原图建的），缩略图只是它装不上时的退路。
        transport.photos["a"] = false
        sync(targets = store())
        val dbAt = transport.gets.indexOfFirst { it.endsWith("/v1/targets/db") }
        val thumbAt = transport.gets.indexOfFirst { it.endsWith("/thumb") }
        assertTrue("预建库该排在缩略图前面", dbAt in 0 until thumbAt)
    }

    @Test
    fun `第二轮带上版本号换回 304，不重下字节`() {
        transport.photos["a"] = false
        val s = store()
        sync(targets = s)
        transport.gets.clear()
        transport.dbConditions.clear()

        val r = sync(targets = s)
        assertEquals(CacheSync.TargetsStatus.UP_TO_DATE, r.prebuilt.status)
        assertEquals(listOf("\"v1\""), transport.dbConditions)
        assertEquals(4_096L, s.bytes)
    }

    @Test
    fun `304 时元数据照样更新`() {
        // manifest 是 no-store 的、每次现取，而标题 / hasVideo / overflow 刻意不在版本号
        // 里（改个标题不该让全体客户端重下几 MB）。不更新的话，一张照片补了视频这件事
        // 在离线那条路上永远看不到。
        transport.photos["a"] = false
        val s = store()
        sync(targets = s)

        transport.targetsTitle = "改过的名字"
        transport.photos["a"] = true
        sync(targets = s)

        val entry = s.snapshot()!!.entry("a")!!
        assertEquals("改过的名字", entry.title)
        assertTrue(entry.hasVideo)
        assertEquals("v1", s.snapshot()!!.version)
    }

    @Test
    fun `版本变了就换一份新的库`() {
        transport.photos["a"] = false
        val s = store()
        sync(targets = s)

        transport.targetsVersion = "v2"
        transport.targetsDbBytes = 8_192
        val r = sync(targets = s)

        assertEquals(CacheSync.TargetsStatus.DOWNLOADED, r.prebuilt.status)
        assertEquals("v2", s.snapshot()!!.version)
        assertEquals(8_192L, s.bytes)
    }

    @Test
    fun `服务端正在建时按 Retry-After 等一会儿再问`() {
        transport.photos["a"] = false
        transport.buildingRounds = 2
        val r = sync(targets = store())

        assertEquals(CacheSync.TargetsStatus.DOWNLOADED, r.prebuilt.status)
        assertEquals("两次 503 就该等两次", listOf(5_000L, 5_000L), slept)
    }

    @Test
    fun `一直在建也不阻断这一轮同步`() {
        // 「正在建」是服务端那边的正常状态。等到上限就报一句「过一会儿再来」，而缩略图
        // 和视频照样下完 —— 离线识别退回端上现建那一档，功能不丢。
        transport.photos["a"] = true
        transport.buildingRounds = 99
        val r = sync(targets = store())

        assertEquals(CacheSync.TargetsStatus.BUILDING, r.prebuilt.status)
        assertNull("这不是失败，不该停在半路", r.stoppedBy)
        assertEquals(1, r.thumbsDownloaded)
        assertEquals(1, r.videosDownloaded)
        assertTrue("退避要有上限，同步不能变成不会结束的按钮", slept.size in 1..10)
    }

    @Test
    fun `一张都没授权时把本地那份删掉`() {
        // 留着就是「已经没权限看的照片，这台手机还能离线认出来」。
        transport.photos["a"] = false
        val s = store()
        sync(targets = s)
        assertTrue(s.bytes > 0)

        transport.dbFailure = 404 to """{"error":"no_targets","message":"还没有被授权"}"""
        val r = sync(targets = s)

        assertEquals(CacheSync.TargetsStatus.EMPTY, r.prebuilt.status)
        assertEquals(0L, s.bytes)
        assertNull(s.snapshot())
    }

    @Test
    fun `库的 ETag 与 manifest 版本对不上时不配到一起`() {
        // 两个请求之间管理员可能入了十张照片。配错的后果是端上认出一张照片却查到错的
        // 尺寸 —— 服务端那边费很大劲堵死的就是这个方向。
        transport.photos["a"] = false
        transport.dbEtag = "另一个版本"
        val s = store()
        val r = sync(targets = s)

        assertEquals(CacheSync.TargetsStatus.FAILED, r.prebuilt.status)
        assertNull("宁可这一份不落盘", s.snapshot())
        // 按服务端注释说的「重取一遍 manifest」，所以 manifest 被问了两次
        assertEquals(2, transport.gets.count { it.endsWith("/v1/targets/manifest") })
        // 其余部分照样完成
        assertEquals(1, r.thumbsDownloaded)
    }

    @Test
    fun `预建库拿不到不影响缩略图和视频`() {
        transport.photos["a"] = true
        transport.dbFailure = 500 to """{"error":"targets_build_failed","message":"arcoreimg 没了"}"""
        val r = sync(targets = store())

        assertEquals(CacheSync.TargetsStatus.FAILED, r.prebuilt.status)
        assertNull(r.stoppedBy)
        assertEquals(1, r.thumbsDownloaded)
        assertEquals(1, r.videosDownloaded)
    }

    @Test
    fun `预建库那一步拿到 401 就立刻停整轮`() {
        // 剩下两百条会用同一个坏 token 各失败一次，白等半分钟还刷一屏错误。
        transport.photos["a"] = true
        transport.failOn.add(
            "/v1/targets" to HttpFailure(NetErrorKind.UNAUTHORIZED, 401, "token 无效"),
        )
        val r = sync(targets = store())

        assertNotNull(r.stoppedBy)
        assertEquals(CacheSync.TargetsStatus.FAILED, r.prebuilt.status)
        assertEquals(0, transport.gets.count { it.endsWith("/thumb") })
    }

    @Test
    fun `断网时也是立刻停，且不删已经缓存好的东西`() {
        transport.photos["a"] = true
        val s = store()
        sync(targets = s)
        val bytesBefore = s.bytes
        transport.failOn.add(
            "/v1/targets" to HttpFailure(NetErrorKind.TRANSPORT, null, "连不上"),
        )
        val r = sync(targets = s)

        assertEquals("连不上服务端", r.stoppedBy)
        assertEquals("本地那份还在，离线识别照样能用", bytesBefore, s.bytes)
    }

    @Test
    fun `overflow 被如实带出来`() {
        // ARCore 单个库最多 1000 张，超出的那些永远得联网才认得出。这个数是界面上
        // 唯一能解释「有几张照片时好时坏」的东西。
        transport.photos["a"] = false
        transport.targetsOverflow = 37
        val r = sync(targets = store())
        assertEquals(37, r.prebuilt.overflow)
        assertEquals(1000, r.prebuilt.maxTargets)
    }
}

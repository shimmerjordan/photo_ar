package app.photoar.arview.cache

import app.photoar.arview.ApiParseException
import app.photoar.arview.PhotoSummary
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 缓存索引的编解码与元数据比对。
 *
 * 这一层为什么值得单测：它只在**断网**时起作用，而断网那条路在真机上跑一遍要
 * 先攒够缓存再拔网线，验一个字段写错要好几分钟。
 */
class CacheIndexTest {

    private fun summary(
        photoId: String = "a".repeat(32),
        title: String? = "外婆家门口",
        printWidthM: Float = 0.152f,
        refAspect: Float? = 1.5f,
        hasVideo: Boolean = true,
        refStale: Boolean = false,
        createdAt: Long = 1_730_000_000_000L,
    ): PhotoSummary = PhotoSummary(
        photoId = photoId,
        title = title,
        printWidthM = printWidthM,
        qualityScore = 88,
        refAspect = refAspect,
        refThumbUrl = "/v1/photo/$photoId/thumb",
        hasVideo = hasVideo,
        refStale = refStale,
        createdAt = createdAt,
    )

    // ---- 编解码 ----

    @Test
    fun `一条完整的条目原样往返`() {
        val e = CachedPhoto(
            photoId = "b".repeat(32),
            title = "外婆家门口",
            printWidthM = 0.152f,
            refAspect = 1.5f,
            refStale = true,
            hasServerVideo = true,
            thumbBytes = 44_031L,
            videoBytes = 1_548_392L,
            videoDurationMs = 12_400L,
            createdAt = 1_730_000_000_000L,
            lastSeenAt = 1_730_000_600_000L,
            targetRejected = false,
        )
        assertEquals(e, CacheIndexCodec.parse(CacheIndexCodec.encode(listOf(e))).single())
    }

    @Test
    fun `可空字段缺失也往返`() {
        val e = CachedPhoto.seed(summary(title = null, refAspect = null))
        val back = CacheIndexCodec.parse(CacheIndexCodec.encode(listOf(e))).single()
        assertNull(back.title)
        assertNull(back.refAspect)
        assertEquals(e, back)
    }

    @Test
    fun `时间戳是毫秒级不能丢精度`() {
        // JSON 里存成 double 会把 1730000000123 舍成 1730000000000 —— 排序键
        // （lastSeenAt）丢了精度就意味着「最近扫的」顺序会错。
        val e = CachedPhoto.seed(summary(createdAt = 1_730_000_000_123L))
            .copy(lastSeenAt = 1_730_000_000_987L)
        val back = CacheIndexCodec.parse(CacheIndexCodec.encode(listOf(e))).single()
        assertEquals(1_730_000_000_123L, back.createdAt)
        assertEquals(1_730_000_000_987L, back.lastSeenAt)
    }

    @Test
    fun `空索引解出空列表`() {
        assertEquals(emptyList<CachedPhoto>(), CacheIndexCodec.parse(CacheIndexCodec.encode(emptyList())))
    }

    @Test(expected = ApiParseException::class)
    fun `不是 JSON 就抛`() {
        CacheIndexCodec.parse("<html>404</html>")
    }

    @Test(expected = ApiParseException::class)
    fun `版本对不上整份丢掉`() {
        // 不做迁移是刻意的：缓存是纯派生数据，重建的代价是局域网里重下几百个缩略图。
        CacheIndexCodec.parse("""{"version":${CACHE_INDEX_VERSION + 1},"photos":[]}""")
    }

    @Test(expected = ApiParseException::class)
    fun `没有版本号也算对不上`() {
        CacheIndexCodec.parse("""{"photos":[]}""")
    }

    @Test(expected = ApiParseException::class)
    fun `没有 photos 数组就抛`() {
        CacheIndexCodec.parse("""{"version":$CACHE_INDEX_VERSION}""")
    }

    @Test
    fun `没有 photoId 的条目跳过而不是整份丢掉`() {
        // 一条坏条目不该让整个缓存作废：其余 199 张还是好的。
        val json = """
            {"version":$CACHE_INDEX_VERSION,"photos":[
              {"printWidthM":0.152},
              {"photoId":"ok","printWidthM":0.152}
            ]}
        """.trimIndent()
        assertEquals(listOf("ok"), CacheIndexCodec.parse(json).map { it.photoId })
    }

    @Test
    fun `打印宽度不可用的条目保留，宽度归成 0`() {
        // 原来是跳过，理由是「printWidthM 会被原样传给 addImage，为 0 不报错，只会让
        // 视频一直飘」。现在 0 是**受支持的状态**：`LocalTargetDb` 见到 0 会改用不带
        // 宽度的 addImage 重载，由 ARCore 自己量物理尺寸。跳过的真实代价是这张照片
        // 永远进不了端侧库，离线命中对它失效。
        val json = """
            {"version":$CACHE_INDEX_VERSION,"photos":[
              {"photoId":"zero","printWidthM":0},
              {"photoId":"neg","printWidthM":-0.152},
              {"photoId":"missing"},
              {"photoId":"ok","printWidthM":0.152}
            ]}
        """.trimIndent()
        val parsed = CacheIndexCodec.parse(json)
        assertEquals(listOf("zero", "neg", "missing", "ok"), parsed.map { it.photoId })
        // 负数也要归成 0，不能原样留着 —— 负宽度传到哪一层都是错的
        assertEquals(listOf(0f, 0f, 0f, 0.152f), parsed.map { it.printWidthM })
    }

    @Test
    fun `字节数为负当成零`() {
        val json = """
            {"version":$CACHE_INDEX_VERSION,"photos":[
              {"photoId":"x","printWidthM":0.152,"thumbBytes":-5,"videoBytes":-9}
            ]}
        """.trimIndent()
        val e = CacheIndexCodec.parse(json).single()
        assertEquals(0L, e.thumbBytes)
        assertEquals(0L, e.videoBytes)
        // 关键：负数没被 videoCached 读成「有缓存」，否则会一直播一个不存在的文件
        assertFalse(e.videoCached)
    }

    // ---- 派生属性 ----

    @Test
    fun `视频字节为零就是没缓存`() {
        val e = CachedPhoto.seed(summary())
        assertFalse(e.videoCached)
        assertTrue(e.copy(videoBytes = 1).videoCached)
    }

    @Test
    fun `没有缩略图或被拒过都不能进本地库`() {
        val base = CachedPhoto.seed(summary()).copy(thumbBytes = 44_031L)
        assertTrue(base.usableAsTarget)
        assertFalse(base.copy(thumbBytes = 0L).usableAsTarget)
        assertFalse(base.copy(targetRejected = true).usableAsTarget)
    }

    // ---- 服务端变更判定：/v1/photos 不给 updatedAt ----

    @Test
    fun `元数据一致时不算变过`() {
        val s = summary()
        assertFalse(CachedPhoto.seed(s).changedOnServer(s))
    }

    @Test
    fun `打印宽度变了算变过`() {
        // 这个值要传给 addImage，必须跟着改
        assertTrue(CachedPhoto.seed(summary()).changedOnServer(summary(printWidthM = 0.102f)))
    }

    @Test
    fun `参考图陈旧标记变了算变过`() {
        // refStale 是服务端 mtime+sha256 校验的结论 —— 它翻了说明参考图文件动过
        assertTrue(CachedPhoto.seed(summary()).changedOnServer(summary(refStale = true)))
    }

    @Test
    fun `视频有无变了算变过`() {
        assertTrue(CachedPhoto.seed(summary(hasVideo = true)).changedOnServer(summary(hasVideo = false)))
        assertTrue(CachedPhoto.seed(summary(hasVideo = false)).changedOnServer(summary(hasVideo = true)))
    }

    @Test
    fun `只改了标题不算变过`() {
        // 改个名不用重下任何字节。判成变过会让每次改标题都触发一轮重下 + 重建库。
        assertFalse(CachedPhoto.seed(summary()).changedOnServer(summary(title = "换个名字")))
    }

    @Test
    fun `覆盖元数据时保留已下字节与最后扫到时间`() {
        val cached = CachedPhoto.seed(summary()).copy(
            thumbBytes = 44_031L,
            videoBytes = 1_548_392L,
            videoDurationMs = 12_400L,
            lastSeenAt = 1_730_000_600_000L,
        )
        val next = cached.withServerMeta(summary(title = "新名字", printWidthM = 0.102f))
        assertEquals("新名字", next.title)
        assertEquals(0.102f, next.printWidthM, 0f)
        assertEquals(44_031L, next.thumbBytes)
        assertEquals(1_548_392L, next.videoBytes)
        assertEquals(12_400L, next.videoDurationMs)
        // lastSeenAt 是排序键，被服务端元数据冲掉会让常扫的照片掉出前 200
        assertEquals(1_730_000_600_000L, next.lastSeenAt)
    }

    @Test
    fun `重下缩略图之后清掉被拒标记`() {
        // 换了一张特征更好的参考图，「ARCore 嫌它不够」这个结论是对旧图下的
        val cached = CachedPhoto.seed(summary()).copy(thumbBytes = 40_000L, targetRejected = true)
        val next = cached.refreshedFrom(summary(refStale = true), thumbBytes = 51_002L)
        assertFalse(next.targetRejected)
        assertEquals(51_002L, next.thumbBytes)
    }

    @Test
    fun `覆盖元数据本身不清被拒标记`() {
        // refreshedFrom 才清。withServerMeta 会在没有重下字节时被用到，
        // 那时候「被拒过」仍然成立，清掉就会每次同步都去撞同一个拒绝。
        val cached = CachedPhoto.seed(summary()).copy(targetRejected = true)
        assertTrue(cached.withServerMeta(summary(title = "新名字")).targetRejected)
    }

    // ---- 本地命中 ----

    @Test
    fun `本地命中的内点数是负一而不是零`() {
        // 0 会被读成「内点数为零却命中了」，那是 bug 的样子
        assertEquals(LOCAL_HIT_INLIERS, CachedPhoto.seed(summary()).toHit().inliers)
        assertTrue(LOCAL_HIT_INLIERS < 0)
    }

    @Test
    fun `本地命中带上物理宽度和参考图陈旧标记`() {
        val e = CachedPhoto.seed(summary(printWidthM = 0.102f, refStale = true))
        val h = e.toHit()
        assertEquals(0.102f, h.printWidthM, 0f)
        // 离线时参考图变过同样要提示（§13）：缓存里的特征只会更旧
        assertTrue(h.refStale)
        // 离线命中没有网络往返，延迟是 0 而不是「未知」
        assertEquals(0, h.latencyMs)
    }

    @Test
    fun `本地视频源是绝对地址`() {
        // absolute=false 会让 resolvedUrl 给 file:// 套上 mediaEndpoint 前缀
        val m = localMedia("file:///data/videos/x.mp4", 1_548_392L, 12_400L)
        assertTrue(m.absolute)
        assertTrue(m.playable)
        assertFalse(m.missing)
        // 本地文件随便 seek —— 报 false 会让播放器禁掉进度条
        assertTrue(m.supportsRange)
        assertEquals("cache", m.via)
    }

    @Test
    fun `本地视频源不经过 mediaBase`() {
        val m = localMedia("file:///data/videos/x.mp4", 1L, null)
        val endpoints = app.photoar.arview.Endpoints(
            apiBase = "https://ar.example.com",
            mediaBase = "http://192.168.1.9:8848",
            token = "t",
        )
        assertEquals("file:///data/videos/x.mp4", m.resolvedUrl(endpoints))
    }

    // ---- 统计 ----

    @Test
    fun `占用统计把三种字节分开算`() {
        val entries = listOf(
            CachedPhoto.seed(summary(photoId = "a")).copy(thumbBytes = 40_000L, videoBytes = 1_000_000L),
            CachedPhoto.seed(summary(photoId = "b")).copy(thumbBytes = 50_000L),
            CachedPhoto.seed(summary(photoId = "c")).copy(targetRejected = true),
        )
        val s = CacheStats.of(entries, targetBytes = 2_000_000L)
        assertEquals(3, s.photos)
        assertEquals(2, s.withThumb)
        assertEquals(1, s.withVideo)
        assertEquals(90_000L, s.thumbBytes)
        assertEquals(1_000_000L, s.videoBytes)
        assertEquals(1, s.rejected)
        // 本地 ARCore 库也占空间，「缓存管理」那页得把它算进去
        assertEquals(90_000L + 1_000_000L + 2_000_000L, s.totalBytes)
    }

    @Test
    fun `空缓存的统计全是零`() {
        val s = CacheStats.of(emptyList(), targetBytes = 0L)
        assertEquals(0, s.photos)
        assertEquals(0L, s.totalBytes)
    }
}

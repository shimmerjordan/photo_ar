package app.photoar.arview.cache

import app.photoar.arview.ApiParseException
import app.photoar.arview.TargetEntry
import app.photoar.arview.TargetsManifest
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * 服务端预建整库目标在端上那一半：元数据落盘、能不能装的判据、两个元数据来源的合并、
 * 以及 503 的退避。
 *
 * 这些用例盯的全是「不报错的坏」：
 *
 * 1. **库字节在、元数据不在**（写库成功、写元数据失败）→ 这一份必须算不可用。当成可用
 *    的话，那 800 张「预建库里有、端侧没缓存」的照片会认出来却查不到尺寸。
 * 2. **装不上要记在 version 上**。记成一个与版本无关的开关，那个状态自己永远不会好；
 *    改成删文件，则每次同步重下几 MB 再失败一次。
 * 3. **manifest 与库版本对不上时不许配到一起**。那正是服务端费很大劲堵死的方向
 *    （db 里有而 manifest 里没有 = 端上认出来却贴错尺寸）。
 * 4. **合并的优先级**：缓存条目优先（它带本地视频），manifest 兜住缓存没覆盖的那些 ——
 *    后者漏掉的表现是「明明在库里的照片扫不出来」。
 */
class ServerTargetsTest {

    private lateinit var root: File
    private lateinit var cache: PhotoCache
    private lateinit var store: ServerTargetsStore

    @Before
    fun setUp() {
        root = File.createTempFile("photoar-targets", "").let {
            it.delete()
            it.mkdirs()
            it
        }
        cache = PhotoCache(root).load()
        store = ServerTargetsStore(cache)
    }

    // ---- 编解码 ----

    @Test
    fun `快照写下去读回来一模一样`() {
        val s = snapshot(version = "ab12", count = 2, overflow = 3, maxTargets = 1000)
        assertTrue(store.store(ByteArray(64) { 7 }, manifestOf(s)))

        val back = store.snapshot()!!
        assertEquals("ab12", back.version)
        assertEquals(2, back.count)
        assertEquals(3, back.overflow)
        assertEquals(1000, back.maxTargets)
        assertFalse(back.rejected)
        assertEquals(listOf("p1", "p2"), back.entries.map { it.photoId })
        assertEquals(0.152f, back.entry("p1")!!.printWidthM, 1e-6f)
        assertEquals("外婆生日", back.entry("p1")!!.title)
        assertNull(back.entry("p2")!!.title)
        assertNull(back.entry("p2")!!.refAspect)
    }

    @Test
    fun `整份 manifest 都存下来，断网时也查得到元数据`() {
        // 那 800 张「预建库覆盖到、端侧缓存没有」的照片，离线命中时只有这里查得到
        // printWidthM 与 mediaUrl。只存一个 version 的话它们全查不到。
        store.store(ByteArray(8), manifestOf(snapshot(count = 2)))
        val fresh = ServerTargetsStore(PhotoCache(root).load())
        assertEquals(2, fresh.snapshot()!!.entries.size)
    }

    @Test
    fun `元数据格式版本对不上就整份丢掉`() {
        cache.targetsMetaFile.parentFile!!.mkdirs()
        cache.targetsMetaFile.writeText("""{"version":999,"targetsVersion":"x","targets":[]}""")
        assertNull(store.snapshot())
    }

    @Test
    fun `元数据写坏了当成没有，不抛异常`() {
        // 它是纯派生数据，读不回来的正确反应是重下，不是让扫描页起不来。
        cache.targetsMetaFile.parentFile!!.mkdirs()
        cache.targetsMetaFile.writeText("{ 半个 JSON")
        assertNull(store.snapshot())
    }

    @Test
    fun `没有 targetsVersion 的元数据不算一份`() {
        try {
            ServerTargetsCodec.parse("""{"version":$TARGETS_META_VERSION,"targets":[]}""")
            org.junit.Assert.fail("应该抛出")
        } catch (e: ApiParseException) {
            assertTrue(e.message!!.contains("targetsVersion"))
        }
    }

    @Test
    fun `宽度不可用的条目照样进快照，宽度记 0`() {
        // 同 CacheIndexCodec：0 = 未知，是受支持的状态（ARCore 自己量），不是坏数据。
        val json = """
            {"version":$TARGETS_META_VERSION,"targetsVersion":"v1","count":2,
             "targets":[{"photoId":"ok","printWidthM":0.1},
                        {"photoId":"bad","printWidthM":0}]}
        """.trimIndent()
        val entries = ServerTargetsCodec.parse(json).entries
        assertEquals(listOf("ok", "bad"), entries.map { it.photoId })
        assertEquals(listOf(0.1f, 0f), entries.map { it.printWidthM })
    }

    // ---- 能不能装 ----

    @Test
    fun `库字节和元数据都在才算能装`() {
        assertNull("空的时候什么都没有", store.installable())
        store.store(ByteArray(32), manifestOf(snapshot()))
        assertNotNull(store.installable())
    }

    @Test
    fun `库在而元数据不在时不算能装`() {
        // 写库成功、写元数据失败会造出这个状态。没有 version 就没法做 ETag 协商，
        // 也没法查那些照片的元数据 —— 一个「能装但查不到尺寸」的库比没有库更糟。
        store.store(ByteArray(32), manifestOf(snapshot()))
        cache.targetsMetaFile.delete()
        assertNull(store.installable())
        assertTrue("库字节还在", store.bytes > 0)
    }

    @Test
    fun `元数据在而库字节不在时也不算能装`() {
        store.store(ByteArray(32), manifestOf(snapshot()))
        cache.serverTargetDbFile.delete()
        assertNull(store.installable())
        assertNotNull("元数据还读得出来，只是不能装", store.snapshot())
    }

    @Test
    fun `0 字节不写盘`() {
        assertFalse(store.store(ByteArray(0), manifestOf(snapshot())))
        assertNull(store.snapshot())
    }

    // ---- 装不上：记在 version 上 ----

    @Test
    fun `记下装不上之后这一份不再算能装，但字节留着`() {
        store.store(ByteArray(32), manifestOf(snapshot(version = "v1")))
        assertTrue(store.markRejected("v1"))

        assertNull("下次 prepare 不该再试它", store.installable())
        assertTrue("字节留着，好让 ETag 协商继续 304，不用重下几 MB", store.bytes > 0)
        assertTrue(store.snapshot()!!.rejected)
    }

    @Test
    fun `换一个版本上来就自动再试一次`() {
        store.store(ByteArray(32), manifestOf(snapshot(version = "v1")))
        store.markRejected("v1")
        // 服务端换了库（有人入库 / 换了 arcoreimg）
        store.store(ByteArray(48), manifestOf(snapshot(version = "v2")))
        assertNotNull("新版本必须重新试一次", store.installable())
        assertFalse(store.snapshot()!!.rejected)
    }

    @Test
    fun `版本对不上的失败报告不生效`() {
        // 一条迟到的失败报告不该把刚换上来的新版本打成坏的。
        store.store(ByteArray(32), manifestOf(snapshot(version = "v2")))
        assertFalse(store.markRejected("v1"))
        assertNotNull(store.installable())
    }

    // ---- 只更新元数据（304 那条路）----

    @Test
    fun `版本相同时可以只更新元数据`() {
        // manifest 是每次现取的，而标题 / hasVideo / overflow 刻意不在版本号里。
        // 不更新的话，一张照片补了视频这件事在离线那条路上永远看不到。
        store.store(ByteArray(32), manifestOf(snapshot(version = "v1", count = 2)))
        val next = manifestOf(
            snapshot(version = "v1", count = 2, overflow = 9).copy(
                entries = listOf(entry("p1", title = "改了名"), entry("p2", hasVideo = true)),
            ),
        )
        assertTrue(store.refreshMeta(next))

        val back = store.snapshot()!!
        assertEquals("改了名", back.entry("p1")!!.title)
        assertTrue(back.entry("p2")!!.hasVideo)
        assertEquals(9, back.overflow)
    }

    @Test
    fun `版本对不上时不把新元数据配到旧库上`() {
        // 那正是「db 里有而 manifest 里没有」那类不一致 —— 端上认出来却贴错尺寸。
        store.store(ByteArray(32), manifestOf(snapshot(version = "v1")))
        assertFalse(store.refreshMeta(manifestOf(snapshot(version = "v2"))))
        assertEquals("v1", store.snapshot()!!.version)
    }

    @Test
    fun `只更新元数据不会把装不上那个标记擦掉`() {
        // 擦掉的话，每次同步之后的第一次扫描都会再白试一遍装载。
        store.store(ByteArray(32), manifestOf(snapshot(version = "v1")))
        store.markRejected("v1")
        store.refreshMeta(manifestOf(snapshot(version = "v1")))
        assertTrue(store.snapshot()!!.rejected)
    }

    @Test
    fun `本地什么都没有时不能只更新元数据`() {
        assertFalse(store.refreshMeta(manifestOf(snapshot())))
        assertNull(store.snapshot())
    }

    @Test
    fun `clear 把库和元数据一起删掉`() {
        // 授权被撤（404 no_targets）时用：留着就是「已经没权限看的照片，这台手机
        // 还能离线认出来」。
        store.store(ByteArray(32), manifestOf(snapshot()))
        store.clear()
        assertEquals(0L, store.bytes)
        assertNull(store.snapshot())
    }

    @Test
    fun `全清缓存会把预建库一起带走`() {
        store.store(ByteArray(32), manifestOf(snapshot()))
        cache.clearAll()
        assertEquals(0L, store.bytes)
        assertNull(store.snapshot())
    }

    @Test
    fun `预建库的字节数算进占用统计`() {
        store.store(ByteArray(4096), manifestOf(snapshot()))
        val s = cache.stats()
        assertEquals(4096L, s.serverTargetBytes)
        assertEquals(4096L, s.totalBytes)
    }

    // ---- 两个元数据来源的合并 ----

    private fun cached(
        photoId: String,
        thumbBytes: Long = 60_000L,
        rejected: Boolean = false,
    ) = CachedPhoto(
        photoId = photoId,
        title = "缓存里的",
        printWidthM = 0.2f,
        refAspect = 1.33f,
        refStale = false,
        hasServerVideo = true,
        thumbBytes = thumbBytes,
        videoBytes = 1_000L,
        videoDurationMs = 3_000L,
        createdAt = 1L,
        lastSeenAt = 2L,
        targetRejected = rejected,
    )

    @Test
    fun `缓存里有就优先用缓存的元数据`() {
        // 缓存那条带着 videoBytes / videoDurationMs，而 fetchMedia 正是靠它直接给出
        // file:// 地址的。用 manifest 那份会把「本地有视频」这件事丢掉。
        val idx = MergedLocalIndex(
            cached = { cached(it) },
            snapshot = { snapshot(count = 2) },
        )
        val hit = idx.lookup("p1")!!
        assertEquals(0.2f, hit.printWidthM, 1e-6f)
        assertEquals(LOCAL_HIT_INLIERS, hit.inliers)
    }

    @Test
    fun `预建库里有但端侧没缓存也算命中`() {
        // 这一条是整个合并的重点：预建库能覆盖 1000 张，端侧缓存默认 200 张。中间
        // 那 800 张当成「没认出来」的表现是「这张照片扫不出来」，而它明明在库里。
        val idx = MergedLocalIndex(cached = { null }, snapshot = { snapshot() })
        val hit = idx.lookup("p1")!!
        assertEquals("p1", hit.photoId)
        assertEquals(0.152f, hit.printWidthM, 1e-6f)
        assertEquals("/v1/photo/p1/media", hit.mediaUrl)
        assertEquals(LOCAL_HIT_INLIERS, hit.inliers)
    }

    @Test
    fun `缓存条目没缩略图时退到 manifest`() {
        // 缓存里那条对**端上现建**那份库没用（建不进去），但服务端拿原图建的库里有它。
        val idx = MergedLocalIndex(
            cached = { cached(it, thumbBytes = 0L) },
            snapshot = { snapshot() },
        )
        assertEquals(0.152f, idx.lookup("p1")!!.printWidthM, 1e-6f)
    }

    @Test
    fun `被 ARCore 拒过的照片仍然能靠预建库命中`() {
        // targetRejected 是「端上现算的特征不够」，那个结论对服务端拿原图建的库不成立。
        val idx = MergedLocalIndex(
            cached = { cached(it, rejected = true) },
            snapshot = { snapshot() },
        )
        assertNotNull(idx.lookup("p1"))
    }

    @Test
    fun `两个来源都没有就不算命中`() {
        // 库和索引理论上同步。真不同步时宁可多一次网络往返，也不要拿一条没有元数据的
        // 命中往下走 —— 那会让视频按上一张照片的尺寸去贴。
        val idx = MergedLocalIndex(cached = { null }, snapshot = { snapshot() })
        assertNull(idx.lookup("不在库里"))
    }

    @Test
    fun `没有预建库时行为与改动之前完全一致`() {
        val idx = MergedLocalIndex(cached = { cached(it) }, snapshot = { null })
        assertNotNull(idx.lookup("p1"))

        val none = MergedLocalIndex(cached = { cached(it, thumbBytes = 0L) }, snapshot = { null })
        assertNull("没缩略图、又没预建库 → 端上那份库里没有它", none.lookup("p1"))
    }

    @Test
    fun `快照会被重新读取而不是记死`() {
        // 用户在缓存管理页同步过一次之后，扫描页那个 LocalIndex 必须看到新的那一份。
        var current: TargetsSnapshot? = null
        val idx = MergedLocalIndex(cached = { null }, snapshot = { current })
        assertNull(idx.lookup("p1"))
        current = snapshot()
        assertNotNull(idx.lookup("p1"))
    }

    // ---- 503 的退避 ----

    @Test
    fun `按 Retry-After 退避`() {
        val w = TargetsBuildWait()
        assertEquals(5_000L, w.nextDelayMs(5))
        assertEquals(3_000L, w.nextDelayMs(3))
        assertEquals(8, w.waitedS)
        assertEquals(2, w.attempts)
    }

    @Test
    fun `没有 Retry-After 时用默认值`() {
        val w = TargetsBuildWait(defaultDelayS = 5)
        assertEquals(5_000L, w.nextDelayMs(null))
        assertEquals(5_000L, w.nextDelayMs(0))
        assertEquals(5_000L, w.nextDelayMs(-1))
    }

    @Test
    fun `单次等待有上限`() {
        // 服务端（或者一个坏掉的代理）给个 3600，不能真等一小时。
        val w = TargetsBuildWait(maxDelayS = 15, maxTotalWaitS = 100)
        assertEquals(15_000L, w.nextDelayMs(3600))
    }

    @Test
    fun `次数有上限`() {
        val w = TargetsBuildWait(maxAttempts = 3, defaultDelayS = 1, maxDelayS = 1)
        assertNotNull(w.nextDelayMs(1))
        assertNotNull(w.nextDelayMs(1))
        assertNotNull(w.nextDelayMs(1))
        assertNull("等到上限就得放弃，同步不能变成一个不会结束的按钮", w.nextDelayMs(1))
    }

    @Test
    fun `总时长有上限，且最后一次不会超出预算`() {
        val w = TargetsBuildWait(maxAttempts = 10, defaultDelayS = 5, maxDelayS = 10, maxTotalWaitS = 12)
        assertEquals(10_000L, w.nextDelayMs(10))
        // 只剩 2 秒预算，不能再等 10 秒
        assertEquals(2_000L, w.nextDelayMs(10))
        assertNull(w.nextDelayMs(10))
        assertEquals(12, w.waitedS)
    }

    // ---- 造数据 ----

    private fun entry(
        photoId: String,
        printWidthM: Float = 0.152f,
        refAspect: Float? = 1.5f,
        title: String? = "外婆生日",
        hasVideo: Boolean = true,
    ) = TargetEntry(
        photoId = photoId,
        printWidthM = printWidthM,
        refAspect = refAspect,
        fitMode = "contain",
        title = title,
        hasVideo = hasVideo,
        mediaUrl = "/v1/photo/$photoId/media",
        imgdbUrl = "/v1/photo/$photoId/imgdb",
    )

    private fun snapshot(
        version: String = "ab12",
        count: Int = 2,
        overflow: Int = 0,
        maxTargets: Int = 1000,
    ) = TargetsSnapshot(
        version = version,
        count = count,
        overflow = overflow,
        maxTargets = maxTargets,
        rejected = false,
        entries = listOf(entry("p1"), entry("p2", printWidthM = 0.089f, refAspect = null, title = null, hasVideo = false)),
    )

    private fun manifestOf(s: TargetsSnapshot) = TargetsManifest(
        version = s.version,
        count = s.count,
        overflow = s.overflow,
        maxTargets = s.maxTargets,
        building = false,
        targets = s.entries,
    )
}

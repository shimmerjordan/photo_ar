package app.photoar.arview.cache

import app.photoar.arview.PhotoSummary
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 缓存计划：留谁、淘汰谁、重下谁。
 *
 * 这是 Phase 4 里唯一「错了不会报错」的地方 —— 算错只表现为某张照片离线时认不出来，
 * 或者缓存悄悄涨到几个 G。所以判定全塞进纯函数，用例都在这儿。
 */
class CachePlanTest {

    private val MB = 1024L * 1024

    private fun summary(
        photoId: String,
        hasVideo: Boolean = false,
        createdAt: Long = 1_000L,
        printWidthM: Float = 0.152f,
        refStale: Boolean = false,
    ): PhotoSummary = PhotoSummary(
        photoId = photoId,
        title = photoId,
        printWidthM = printWidthM,
        qualityScore = 88,
        refAspect = 1.5f,
        refThumbUrl = "/v1/photo/$photoId/thumb",
        hasVideo = hasVideo,
        refStale = refStale,
        createdAt = createdAt,
    )

    private fun cached(
        photoId: String,
        thumbBytes: Long = 40_000L,
        videoBytes: Long = 0L,
        lastSeenAt: Long = 0L,
        hasServerVideo: Boolean = videoBytes > 0,
        createdAt: Long = 1_000L,
        printWidthM: Float = 0.152f,
        refStale: Boolean = false,
        targetRejected: Boolean = false,
    ): CachedPhoto = CachedPhoto(
        photoId = photoId,
        title = photoId,
        printWidthM = printWidthM,
        refAspect = 1.5f,
        refStale = refStale,
        hasServerVideo = hasServerVideo,
        thumbBytes = thumbBytes,
        videoBytes = videoBytes,
        videoDurationMs = if (videoBytes > 0) 12_400L else null,
        createdAt = createdAt,
        lastSeenAt = lastSeenAt,
        targetRejected = targetRejected,
    )

    // ---- 预算校验 ----

    @Test(expected = IllegalArgumentException::class)
    fun `照片上限必须为正`() {
        CacheSpec(maxPhotos = 0)
    }

    @Test
    fun `视频预算可以是零`() {
        // 「只离线识别、不缓存视频」是个合理配置：识别靠缩略图，视频回家再看
        CacheSpec(maxVideoBytes = 0)
    }

    // ---- 排序：最近扫到的在前 ----

    @Test
    fun `按最后扫到的时间倒序`() {
        val server = listOf(summary("a"), summary("b"), summary("c"))
        val local = mapOf(
            "a" to cached("a", lastSeenAt = 100L),
            "b" to cached("b", lastSeenAt = 300L),
            "c" to cached("c", lastSeenAt = 200L),
        )
        assertEquals(listOf("b", "c", "a"), CachePlanner.rank(server, local).map { it.photoId })
    }

    @Test
    fun `没扫过的按入库时间倒序垫后`() {
        // 冷启动时所有 lastSeenAt 都是 0，入库时间是唯一可用的信号
        val server = listOf(summary("old", createdAt = 100L), summary("new", createdAt = 900L))
        assertEquals(listOf("new", "old"), CachePlanner.rank(server, emptyMap()).map { it.photoId })
    }

    @Test
    fun `扫过一次的排在所有没扫过的前面`() {
        // 这条是整个排序键的理由：挂在墙上天天扫的老照片，不该被刚打印的一批挤掉
        val server = listOf(
            summary("刚入库", createdAt = 9_000L),
            summary("墙上那张", createdAt = 100L),
        )
        val local = mapOf("墙上那张" to cached("墙上那张", lastSeenAt = 1L))
        assertEquals(
            listOf("墙上那张", "刚入库"),
            CachePlanner.rank(server, local).map { it.photoId },
        )
    }

    @Test
    fun `入库时间相同时按 id 定序`() {
        // 顺序若随环境变化，「缓存了哪 200 张」会时不时抖一下，且无法复现
        val server = listOf(summary("z"), summary("a"), summary("m"))
        assertEquals(listOf("a", "m", "z"), CachePlanner.rank(server, emptyMap()).map { it.photoId })
    }

    // ---- 缩略图：加 / 补 / 重下 ----

    @Test
    fun `全新的服务端列表全部要下缩略图并建库`() {
        val plan = CachePlanner.plan(listOf(summary("a"), summary("b")), emptyList())
        assertEquals(listOf("a", "b"), plan.addThumb.sorted())
        assertTrue(plan.rebuildTarget)
        assertEquals(2, plan.downloads)
        assertFalse(plan.empty)
    }

    @Test
    fun `什么都没变时计划是空的`() {
        // 这一条决定「同步」按钮在无事可做时不会白跑一趟网络
        val server = listOf(summary("a"), summary("b"))
        val local = listOf(cached("a"), cached("b"))
        val plan = CachePlanner.plan(server, local)
        assertTrue(plan.empty)
        assertFalse(plan.rebuildTarget)
    }

    @Test
    fun `上次缩略图没下成会补下`() {
        val plan = CachePlanner.plan(listOf(summary("a")), listOf(cached("a", thumbBytes = 0L)))
        assertEquals(listOf("a"), plan.addThumb)
        assertTrue(plan.rebuildTarget)
    }

    @Test
    fun `被 ARCore 拒过的不再反复下`() {
        // 缩略图下下来了、ARCore 说特征不够、于是没进库 —— 这不是下载失败，
        // 重下一遍结果一样。每次同步都撞一次，200 张就是几秒钟白花。
        val plan = CachePlanner.plan(
            listOf(summary("a")),
            listOf(cached("a", thumbBytes = 0L, targetRejected = true)),
        )
        assertTrue(plan.empty)
    }

    @Test
    fun `打印宽度变了要重下并重建库`() {
        val plan = CachePlanner.plan(
            listOf(summary("a", printWidthM = 0.102f)),
            listOf(cached("a", printWidthM = 0.152f)),
        )
        assertEquals(listOf("a"), plan.refreshThumb)
        assertTrue(plan.addThumb.isEmpty())
        assertTrue(plan.rebuildTarget)
    }

    @Test
    fun `参考图陈旧标记变了要重下`() {
        val plan = CachePlanner.plan(
            listOf(summary("a", refStale = true)),
            listOf(cached("a", refStale = false)),
        )
        assertEquals(listOf("a"), plan.refreshThumb)
    }

    // ---- 上限与淘汰 ----

    @Test
    fun `超出上限的截掉`() {
        val server = (1..5).map { summary("p$it", createdAt = it.toLong()) }
        val plan = CachePlanner.plan(server, emptyList(), CacheSpec(maxPhotos = 3))
        // 入库时间倒序取前三
        assertEquals(listOf("p5", "p4", "p3"), plan.addThumb)
    }

    @Test
    fun `挤出上限的本地条目整条删掉`() {
        val server = listOf(summary("keep", createdAt = 9_000L), summary("evict", createdAt = 1L))
        val local = listOf(cached("keep"), cached("evict", videoBytes = 1 * MB))
        val plan = CachePlanner.plan(server, local, CacheSpec(maxPhotos = 1))
        assertEquals(listOf("evict"), plan.dropPhoto)
        // 整条删掉时视频跟着走，不该在 dropVideo 里重复列一遍
        assertTrue(plan.dropVideo.isEmpty())
        assertTrue(plan.rebuildTarget)
    }

    @Test
    fun `服务端删掉的照片本地也删掉`() {
        val plan = CachePlanner.plan(listOf(summary("a")), listOf(cached("a"), cached("gone")))
        assertEquals(listOf("gone"), plan.dropPhoto)
        assertTrue(plan.rebuildTarget)
    }

    @Test
    fun `服务端一张都没有时本地清空`() {
        // 拉取失败会抛异常，走不到这里 —— 空列表就是真的空
        val plan = CachePlanner.plan(emptyList(), listOf(cached("a"), cached("b")))
        assertEquals(listOf("a", "b"), plan.dropPhoto.sorted())
    }

    // ---- 视频：预算与 LRU ----

    @Test
    fun `有视频的照片按预算下视频`() {
        val server = (1..3).map { summary("p$it", hasVideo = true, createdAt = it.toLong()) }
        val plan = CachePlanner.plan(server, emptyList(), CacheSpec(maxVideoBytes = 10 * MB))
        assertEquals(listOf("p3", "p2", "p1"), plan.addVideo)
    }

    @Test
    fun `没有视频的照片不下视频`() {
        val server = listOf(summary("a", hasVideo = false))
        assertTrue(CachePlanner.plan(server, emptyList()).addVideo.isEmpty())
    }

    @Test
    fun `预算放不下就少缓存几条而不是超支`() {
        // 未知大小按 §12 的上限 3MB 估。7MB 的预算只放得下 2 条。
        val server = (1..5).map { summary("p$it", hasVideo = true, createdAt = it.toLong()) }
        val plan = CachePlanner.plan(server, emptyList(), CacheSpec(maxVideoBytes = 7 * MB))
        assertEquals(listOf("p5", "p4"), plan.addVideo)
        // 预算紧张时留下的是排在前面（= 最近扫到）的那几条
        assertEquals(2 * CachePlanner.ESTIMATED_VIDEO_BYTES <= 7 * MB, true)
    }

    @Test
    fun `视频预算为零时一条都不下`() {
        val server = listOf(summary("a", hasVideo = true))
        val plan = CachePlanner.plan(server, emptyList(), CacheSpec(maxVideoBytes = 0))
        assertTrue(plan.addVideo.isEmpty())
        // 但缩略图照下 —— 离线识别只要缩略图
        assertEquals(listOf("a"), plan.addThumb)
    }

    @Test
    fun `超预算时先淘汰最久没扫的视频`() {
        val server = (1..3).map { summary("p$it", hasVideo = true) }
        val local = listOf(
            cached("p1", videoBytes = 4 * MB, lastSeenAt = 500L),
            cached("p2", videoBytes = 4 * MB, lastSeenAt = 100L),
            cached("p3", videoBytes = 4 * MB, lastSeenAt = 900L),
        )
        val plan = CachePlanner.plan(server, local, CacheSpec(maxVideoBytes = 9 * MB))
        assertEquals(listOf("p2"), plan.dropVideo)
        // 只删视频文件，索引条目留着 —— 缩略图便宜且是离线识别的地基
        assertTrue(plan.dropPhoto.isEmpty())
        // 刚淘汰的不能立刻又下回来，否则每次同步都在删了又下
        assertTrue(plan.addVideo.isEmpty())
        // 视频存亡不影响 ARCore 库
        assertFalse(plan.rebuildTarget)
    }

    @Test
    fun `最后扫到时间相同时按 id 淘汰以保证确定性`() {
        val server = listOf(summary("b", hasVideo = true), summary("a", hasVideo = true))
        val local = listOf(cached("a", videoBytes = 4 * MB), cached("b", videoBytes = 4 * MB))
        val plan = CachePlanner.plan(server, local, CacheSpec(maxVideoBytes = 5 * MB))
        assertEquals(listOf("a"), plan.dropVideo)
    }

    @Test
    fun `已缓存的视频不会为了给没缓存的腾地方被删`() {
        // 「已在本地」比「排得靠前」更值钱：删了再下是两倍流量换零收益
        val server = listOf(
            summary("新的", hasVideo = true, createdAt = 9_000L),
            summary("老的", hasVideo = true, createdAt = 1L),
        )
        val local = listOf(cached("老的", videoBytes = 3 * MB, lastSeenAt = 5L))
        val plan = CachePlanner.plan(server, local, CacheSpec(maxVideoBytes = 3 * MB))
        assertTrue(plan.dropVideo.isEmpty())
        assertTrue(plan.addVideo.isEmpty())
    }

    @Test
    fun `换了视频要把旧的那份扔掉再重下`() {
        // 不扔的话缓存命中时会一直播旧的那条，而且因为 videoCached 为 true
        // 永远不会去重下 —— 这是个不报错的错
        val server = listOf(summary("a", hasVideo = true, printWidthM = 0.102f))
        val local = listOf(cached("a", videoBytes = 2 * MB, printWidthM = 0.152f))
        val plan = CachePlanner.plan(server, local, CacheSpec(maxVideoBytes = 100 * MB))
        assertEquals(listOf("a"), plan.dropVideo)
        assertEquals(listOf("a"), plan.addVideo)
        assertEquals(listOf("a"), plan.refreshThumb)
    }

    @Test
    fun `撤掉视频后本地那份也删掉且不重下`() {
        val server = listOf(summary("a", hasVideo = false))
        val local = listOf(cached("a", videoBytes = 2 * MB, hasServerVideo = true))
        val plan = CachePlanner.plan(server, local)
        assertEquals(listOf("a"), plan.dropVideo)
        assertTrue(plan.addVideo.isEmpty())
        assertTrue(plan.dropPhoto.isEmpty())
    }

    @Test
    fun `补了视频的老照片会去下视频`() {
        val server = listOf(summary("a", hasVideo = true))
        val local = listOf(cached("a", hasServerVideo = false))
        val plan = CachePlanner.plan(server, local)
        assertEquals(listOf("a"), plan.addVideo)
        assertTrue(plan.dropVideo.isEmpty())
    }

    @Test
    fun `预算按未淘汰的部分算而不是按全量`() {
        // 三条各 4MB、预算 9MB：淘汰 1 条剩 8MB 就够了，不该连着淘汰第二条
        val server = (1..3).map { summary("p$it", hasVideo = true) }
        val local = (1..3).map { cached("p$it", videoBytes = 4 * MB, lastSeenAt = it.toLong()) }
        val plan = CachePlanner.plan(server, local, CacheSpec(maxVideoBytes = 9 * MB))
        assertEquals(1, plan.dropVideo.size)
    }

    @Test
    fun `恰好等于预算不淘汰`() {
        val server = listOf(summary("a", hasVideo = true))
        val local = listOf(cached("a", videoBytes = 4 * MB))
        val plan = CachePlanner.plan(server, local, CacheSpec(maxVideoBytes = 4 * MB))
        assertTrue(plan.dropVideo.isEmpty())
    }

    // ---- 计划的派生属性 ----

    @Test
    fun `下载数只数要下的不数要删的`() {
        val plan = CachePlan(
            addThumb = listOf("a"),
            refreshThumb = listOf("b"),
            addVideo = listOf("c"),
            dropVideo = listOf("d"),
            dropPhoto = listOf("e"),
        )
        assertEquals(3, plan.downloads)
        assertFalse(plan.empty)
    }

    @Test
    fun `只重建库也不算空计划`() {
        // 库文件丢了（用户清了数据）时会出现：没有任何下载，但必须重建
        assertFalse(CachePlan(rebuildTarget = true).empty)
        assertEquals(0, CachePlan(rebuildTarget = true).downloads)
    }

    // ---- 视频源选择 ----

    @Test
    fun `缓存里有就用缓存哪怕在线`() {
        // 本地文件起播快、不吃流量，而服务端 resolve 每次都要现取直链（§10）
        assertEquals(MediaSource.LOCAL_CACHE, chooseMediaSource(videoCached = true, online = true))
        assertEquals(MediaSource.LOCAL_CACHE, chooseMediaSource(videoCached = true, online = false))
    }

    @Test
    fun `没缓存但在线就走网络`() {
        assertEquals(MediaSource.NETWORK, chooseMediaSource(videoCached = false, online = true))
    }

    @Test
    fun `没缓存又没网是没有而不是坏了`() {
        // 界面上要说清是「视频没缓存」，不是「视频坏了」—— 前者用户能自己解决
        assertEquals(MediaSource.NONE, chooseMediaSource(videoCached = false, online = false))
    }
}

package app.photoar.arview.cache

import app.photoar.arview.PhotoSummary
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
 * 缓存落盘。用真实临时目录跑真实读写 —— `java.io.File` 在 JVM 单测里是可用的，
 * 而这一层出错的样子（字节记账与磁盘不符、索引写坏了整份作废）只有真读写才验得出来。
 */
class PhotoCacheTest {

    private lateinit var root: File
    private lateinit var cache: PhotoCache

    @Before
    fun setUp() {
        root = File.createTempFile("photoar-cache", "").let {
            it.delete()
            it.mkdirs()
            it
        }
        cache = PhotoCache(root).load()
    }

    @After
    fun tearDown() {
        root.deleteRecursively()
    }

    private fun summary(photoId: String, hasVideo: Boolean = false): PhotoSummary = PhotoSummary(
        photoId = photoId,
        title = photoId,
        printWidthM = 0.152f,
        qualityScore = 88,
        refAspect = 1.5f,
        refThumbUrl = "/v1/photo/$photoId/thumb",
        hasVideo = hasVideo,
        refStale = false,
        createdAt = 1_730_000_000_000L,
    )

    private fun bytes(n: Int, fill: Byte = 7): ByteArray = ByteArray(n) { fill }

    // ---- 空缓存 ----

    @Test
    fun `第一次跑是空缓存而不是崩`() {
        assertEquals(emptyList<CachedPhoto>(), cache.entries())
        assertEquals(0, cache.stats().photos)
        assertEquals(0L, cache.stats().totalBytes)
    }

    @Test
    fun `索引文件写坏了当成空缓存`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.flush()
        File(root, "offline/index.json").writeText("{ 这不是 JSON")

        val reopened = PhotoCache(root).load()
        assertEquals(emptyList<CachedPhoto>(), reopened.entries())
        // 索引作废后那些文件永远不会被引用，留着就是永久孤儿
        assertFalse(File(root, "offline/thumbs/a.jpg").exists())
    }

    @Test
    fun `版本对不上时连文件一起清掉`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.flush()
        File(root, "offline/index.json")
            .writeText("""{"version":${CACHE_INDEX_VERSION + 9},"photos":[]}""")

        val reopened = PhotoCache(root).load()
        assertEquals(0, reopened.stats().photos)
        assertFalse(File(root, "offline/thumbs/a.jpg").exists())
    }

    // ---- 写入与记账 ----

    @Test
    fun `字节数取磁盘实际长度`() {
        // 拿下载 buffer 的 size 记账会在写盘只写一半时给出「已缓存 480MB」
        // 而实际播不出来的假象
        val e = cache.putThumb(CachedPhoto.seed(summary("a")), bytes(1234))
        assertEquals(1234L, e.thumbBytes)
        assertEquals(1234L, File(root, "offline/thumbs/a.jpg").length())
    }

    @Test
    fun `写视频记下时长`() {
        val e = cache.putVideo(CachedPhoto.seed(summary("a", hasVideo = true)), bytes(5000), 12_400L)
        assertEquals(5000L, e.videoBytes)
        assertEquals(12_400L, e.videoDurationMs)
        assertTrue(e.videoCached)
    }

    @Test
    fun `写完之后没有残留的 tmp 文件`() {
        // tmp 留在缩略图目录里会被 purgeOrphans 当成孤儿反复删，更糟的是
        // 它的名字去掉扩展名之后可能撞上真实 photoId
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.putVideo(CachedPhoto.seed(summary("b", hasVideo = true)), bytes(100))
        val leftovers = root.walkTopDown().filter { it.name.endsWith(".tmp") }.toList()
        assertEquals(emptyList<File>(), leftovers)
    }

    @Test
    fun `重下缩略图时清掉被拒标记`() {
        val rejected = CachedPhoto.seed(summary("a")).copy(targetRejected = true)
        cache.put(rejected)
        val next = cache.putThumb(rejected, bytes(200), refreshed = true)
        assertFalse(next.targetRejected)
    }

    @Test
    fun `普通补下不动被拒标记`() {
        val rejected = CachedPhoto.seed(summary("a")).copy(targetRejected = true)
        assertTrue(cache.putThumb(rejected, bytes(200), refreshed = false).targetRejected)
    }

    // ---- 往返 ----

    @Test
    fun `索引写盘之后能原样读回来`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.putVideo(cache.byId("a")!!, bytes(2000), 9_000L)
        cache.markSeen("a", 1_730_000_600_000L)
        cache.flush()

        val e = PhotoCache(root).load().byId("a")!!
        assertEquals(100L, e.thumbBytes)
        assertEquals(2000L, e.videoBytes)
        assertEquals(9_000L, e.videoDurationMs)
        assertEquals(1_730_000_600_000L, e.lastSeenAt)
    }

    @Test
    fun `没有 flush 的改动不会出现在下次启动里`() {
        // 这条是刻意的：markSeen 在扫描时每帧都可能调，写盘会掉帧
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.flush()
        cache.markSeen("a", 1_730_000_600_000L)
        assertEquals(0L, PhotoCache(root).load().byId("a")!!.lastSeenAt)
    }

    // ---- 对账：以磁盘为准 ----

    @Test
    fun `文件被外部删掉时字节数归零好让下轮重下`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.putVideo(cache.byId("a")!!, bytes(2000))
        cache.flush()
        File(root, "offline/thumbs/a.jpg").delete()
        File(root, "offline/videos/a.mp4").delete()

        val e = PhotoCache(root).load().byId("a")!!
        assertEquals(0L, e.thumbBytes)
        assertEquals(0L, e.videoBytes)
        assertNull(e.videoDurationMs)
        assertFalse(e.usableAsTarget)
    }

    @Test
    fun `文件长度与索引不符时以磁盘为准`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.flush()
        File(root, "offline/thumbs/a.jpg").writeBytes(bytes(55))
        assertEquals(55L, PhotoCache(root).load().byId("a")!!.thumbBytes)
    }

    @Test
    fun `索引里没有的文件是孤儿要删掉`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.flush()
        File(root, "offline/videos").mkdirs()
        File(root, "offline/thumbs/orphan.jpg").writeBytes(bytes(100))
        File(root, "offline/videos/orphan.mp4").writeBytes(bytes(100))

        PhotoCache(root).load()
        assertFalse(File(root, "offline/thumbs/orphan.jpg").exists())
        assertFalse(File(root, "offline/videos/orphan.mp4").exists())
        assertTrue(File(root, "offline/thumbs/a.jpg").exists())
    }

    // ---- 本地视频地址 ----

    @Test
    fun `本地视频地址是 file 协议`() {
        cache.putVideo(CachedPhoto.seed(summary("a", hasVideo = true)), bytes(2000))
        val url = cache.localVideoUrl("a")
        assertNotNull(url)
        assertTrue(url!!.startsWith("file:"))
        assertTrue(url.endsWith("a.mp4"))
    }

    @Test
    fun `没缓存视频时没有本地地址`() {
        cache.put(CachedPhoto.seed(summary("a", hasVideo = true)))
        assertNull(cache.localVideoUrl("a"))
        assertNull(cache.localVideoUrl("从来没有过的 id"))
    }

    @Test
    fun `记账说有文件却没了时不给地址并把账改对`() {
        // 交给 ExoPlayer 一个不存在的 file:// 会报「解码失败」，
        // 那个提示会让人以为视频坏了，而实际只是没缓存
        cache.putVideo(CachedPhoto.seed(summary("a", hasVideo = true)), bytes(2000))
        File(root, "offline/videos/a.mp4").delete()
        assertNull(cache.localVideoUrl("a"))
        assertFalse(cache.byId("a")!!.videoCached)
    }

    // ---- 删除 ----

    @Test
    fun `只删视频时缩略图和索引条目留着`() {
        // 这个区分是整份缓存设计里最要紧的一条：缩略图是离线识别的地基
        cache.putThumb(CachedPhoto.seed(summary("a", hasVideo = true)), bytes(100))
        cache.putVideo(cache.byId("a")!!, bytes(2000))
        cache.dropVideo("a")

        val e = cache.byId("a")!!
        assertEquals(100L, e.thumbBytes)
        assertEquals(0L, e.videoBytes)
        assertTrue(e.usableAsTarget)
        assertTrue(File(root, "offline/thumbs/a.jpg").exists())
        assertFalse(File(root, "offline/videos/a.mp4").exists())
    }

    @Test
    fun `整条删掉时文件和索引都不留`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.putVideo(cache.byId("a")!!, bytes(2000))
        cache.dropPhoto("a")
        assertNull(cache.byId("a"))
        assertFalse(File(root, "offline/thumbs/a.jpg").exists())
        assertFalse(File(root, "offline/videos/a.mp4").exists())
    }

    @Test
    fun `只清视频保留离线识别能力`() {
        (1..3).forEach {
            cache.putThumb(CachedPhoto.seed(summary("p$it", hasVideo = true)), bytes(100))
            cache.putVideo(cache.byId("p$it")!!, bytes(2000))
        }
        cache.clearVideos()

        assertEquals(3, cache.stats().photos)
        assertEquals(3, cache.stats().withThumb)
        assertEquals(0, cache.stats().withVideo)
        assertEquals(0L, cache.stats().videoBytes)
        // clearVideos 自己 flush 过，重启后仍然是清过的
        assertEquals(0, PhotoCache(root).load().stats().withVideo)
    }

    @Test
    fun `全清之后目录还在可以继续写`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.targetDbFile.writeBytes(bytes(500))
        cache.clearAll()

        assertEquals(0, cache.stats().photos)
        assertFalse(cache.targetDbFile.exists())
        // 清完还能马上写 —— 否则「全清」之后要重启 App 才能同步
        cache.putThumb(CachedPhoto.seed(summary("b")), bytes(100))
        assertEquals(100L, cache.byId("b")!!.thumbBytes)
    }

    // ---- 标记 ----

    @Test
    fun `记扫到时间只对索引里有的照片生效`() {
        cache.put(CachedPhoto.seed(summary("a")))
        assertTrue(cache.markSeen("a", 999L))
        assertEquals(999L, cache.byId("a")!!.lastSeenAt)
        assertFalse(cache.markSeen("不在索引里", 999L))
    }

    @Test
    fun `被 ARCore 拒过之后不再算可用目标`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.markRejected("a")
        assertFalse(cache.byId("a")!!.usableAsTarget)
        assertEquals(1, cache.stats().rejected)
    }

    // ---- 统计 ----

    @Test
    fun `统计把 ARCore 库也算进去`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        cache.targetDbFile.parentFile!!.mkdirs()
        cache.targetDbFile.writeBytes(bytes(4300))
        assertEquals(4300L, cache.stats().targetBytes)
        assertEquals(4400L, cache.stats().totalBytes)
    }

    // ---- 本地库过期判定的输入（newestThumbMs）----
    //
    // LocalTargetDb 拿它比 local.imgdb 的 mtime。这三条锁的是那个判定的**输入**：
    // 判定本身要 ARCore，JVM 里跑不了，所以只能把「什么会让它变、什么不会」钉在这里。

    @Test
    fun `一张缩略图都没有时返回 0`() {
        assertEquals(0L, cache.newestThumbMs())
    }

    @Test
    fun `下了缩略图之后有时间`() {
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        assertTrue(cache.newestThumbMs() > 0L)
    }

    @Test
    fun `只更新 lastSeenAt 并落盘 不会让缩略图时间变新`() {
        // 这一条是复查里找到的那个 bug 的回归：过期判定原先比的是 index.json 的
        // mtime，而 markSeen + flush 每次扫描结束都会重写索引 —— 于是每次启动扫描
        // 都白重建一遍 200 张的 ARCore 库（几秒），且不报任何错。
        cache.putThumb(CachedPhoto.seed(summary("a")), bytes(100))
        val before = cache.newestThumbMs()
        cache.markSeen("a", 1_700_000_000_000L)
        cache.flush()
        assertEquals(before, cache.newestThumbMs())
    }

    @Test
    fun `清视频不影响缩略图时间`() {
        // 清视频之后离线识别必须照样可用 —— 库不该因此重建，也不该失效。
        val e = cache.putThumb(CachedPhoto.seed(summary("a", hasVideo = true)), bytes(100))
        cache.putVideo(e, bytes(500))
        val before = cache.newestThumbMs()
        cache.clearVideos()
        assertEquals(before, cache.newestThumbMs())
    }
}

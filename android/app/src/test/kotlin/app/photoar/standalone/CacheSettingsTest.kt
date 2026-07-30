package app.photoar.standalone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 缓存上限的钳位。
 *
 * 存在的理由只有一条：[app.photoar.arview.cache.CacheSpec] 的构造带 `require`，
 * 而这两个数是从 SharedPreferences 读出来的 —— 读到 0 或读到一个已经从选项里去掉
 * 的旧值，都会在进入设置页时抛异常。所以这里逐个盯住「不合法的输入不许抛」。
 */
class CacheSettingsTest {

    @Test
    fun `默认值本身就在选项里`() {
        assertTrue(CacheSettings.DEFAULT_PHOTOS in CacheSettings.PHOTO_OPTIONS)
        assertTrue(CacheSettings.DEFAULT_VIDEO_MB in CacheSettings.VIDEO_MB_OPTIONS)
    }

    @Test
    fun `选项里的值原样返回`() {
        CacheSettings.PHOTO_OPTIONS.forEach {
            assertEquals(it, CacheSettings.selectedPhotos(it))
        }
        CacheSettings.VIDEO_MB_OPTIONS.forEach {
            assertEquals(it, CacheSettings.selectedVideoMb(it))
        }
    }

    @Test
    fun `prefs 里存了 0 也不会抛 而是回到最小一档`() {
        // CacheSpec 的 require(maxPhotos > 0) 是崩在启动路径上的，这条是主要防线
        val spec = CacheSettings.spec(0, 0)
        assertEquals(CacheSettings.PHOTO_OPTIONS.first(), spec.maxPhotos)
        assertEquals(CacheSettings.VIDEO_MB_OPTIONS.first() * 1024L * 1024L, spec.maxVideoBytes)
    }

    @Test
    fun `负数也不会抛`() {
        val spec = CacheSettings.spec(-100, -1)
        assertTrue(spec.maxPhotos > 0)
        assertTrue(spec.maxVideoBytes > 0)
    }

    @Test
    fun `超出上限的值收到最大一档`() {
        val spec = CacheSettings.spec(99999, 99999)
        assertEquals(CacheSettings.PHOTO_OPTIONS.max(), spec.maxPhotos)
        assertEquals(CacheSettings.VIDEO_MB_OPTIONS.max() * 1024L * 1024L, spec.maxVideoBytes)
    }

    @Test
    fun `选项之间的值取最近的一档`() {
        // 100 和 200 之间：120 更靠 100
        assertEquals(100, CacheSettings.selectedPhotos(120))
        // 180 更靠 200
        assertEquals(200, CacheSettings.selectedPhotos(180))
        assertEquals(256, CacheSettings.selectedVideoMb(300))
        assertEquals(512, CacheSettings.selectedVideoMb(500))
    }

    @Test
    fun `MB 换成字节不会溢出`() {
        val spec = CacheSettings.spec(200, CacheSettings.VIDEO_MB_OPTIONS.max())
        // 2048MB = 2GB，用 Int 乘会溢出成负数，那时 require(maxVideoBytes >= 0) 会炸
        assertEquals(2048L * 1024 * 1024, spec.maxVideoBytes)
        assertTrue(spec.maxVideoBytes > Int.MAX_VALUE.toLong() / 2)
    }
}

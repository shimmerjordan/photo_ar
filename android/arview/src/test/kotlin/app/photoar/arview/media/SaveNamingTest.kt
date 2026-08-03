package app.photoar.arview.media

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SaveNamingTest {

    @Test
    fun `扩展名按 MIME 来`() {
        assertEquals("jpg", SaveNaming.extensionOf("image/jpeg"))
        assertEquals("png", SaveNaming.extensionOf("image/png"))
        assertEquals("mp4", SaveNaming.extensionOf("video/mp4"))
        // 带参数的 Content-Type 也要认
        assertEquals("jpg", SaveNaming.extensionOf("image/jpeg; charset=binary"))
        assertEquals("jpg", SaveNaming.extensionOf("IMAGE/JPEG"))
    }

    @Test
    fun `不认识的 MIME 返回 null，不猜一个 jpg`() {
        // 猜错的后果是相册里一个打不开的文件，而用户会以为是照片本身坏了。
        assertNull(SaveNaming.extensionOf("application/octet-stream"))
        assertNull(SaveNaming.extensionOf(null))
        assertNull(SaveNaming.displayName("婚礼照", "abc12345", "application/pdf"))
    }

    @Test
    fun `文件名带标题和 photoId 前八位`() {
        assertEquals(
            "婚礼照-60340931.jpg",
            SaveNaming.displayName("婚礼照", "603409313b2f4681bf6a06398e090ed2", "image/jpeg"),
        )
    }

    @Test
    fun `同一张照片重复保存得到同名文件，不是第二份`() {
        val a = SaveNaming.displayName("合照", "aaaaaaaa1111", "image/jpeg")
        val b = SaveNaming.displayName("合照", "aaaaaaaa1111", "image/jpeg")
        assertEquals(a, b)
        // 而不同照片即使标题一样也不撞名 —— 一场婚礼里「合照」会重复很多次
        val c = SaveNaming.displayName("合照", "bbbbbbbb2222", "image/jpeg")
        assertTrue("同标题不同照片必须不同名：$a / $c", a != c)
    }

    @Test
    fun `路径分隔符和保留字符被换掉`() {
        // '/' 不换的话 MediaStore 会当成路径：插入「成功」但文件在别的目录里
        val messy = "a/b" + '\\' + "c:d*e?f" + '"' + "g<h>i|j"
        val n = SaveNaming.displayName(messy, "id123456", "image/jpeg")!!
        assertTrue("不能含路径分隔符：$n", !n.contains('/') && !n.contains('\\'))
        for (ch in listOf(':', '*', '?', '"', '<', '>', '|')) {
            assertTrue("不能含 $ch：$n", !n.contains(ch))
        }
        assertTrue(n.endsWith(".jpg"))
    }

    @Test
    fun `控制字符也被换掉`() {
        val n = SaveNaming.displayName("标" + '\u0007' + "题", "id123456", "image/jpeg")!!
        assertTrue("不能含控制字符：${n.toList()}", n.none { it.code in 0..0x1F })
    }

    @Test
    fun `没有标题时用兜底名`() {
        assertEquals(
            "photoar-id123456.jpg",
            SaveNaming.displayName(null, "id123456", "image/jpeg"),
        )
        assertEquals(
            "photoar-id123456.jpg",
            SaveNaming.displayName("   ", "id123456", "image/jpeg"),
        )
    }

    @Test
    fun `超长标题被截断，但仍带上 id 和扩展名`() {
        val long = "很".repeat(200)
        val n = SaveNaming.displayName(long, "id123456", "image/jpeg")!!
        assertTrue("截断后应该短得多：${n.length}", n.length < 60)
        assertTrue(n.endsWith("-id123456.jpg"))
    }
}

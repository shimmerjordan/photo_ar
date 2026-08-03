package app.photoar.arview.ui

import app.photoar.arview.NoticeKind
import app.photoar.arview.SaveOutcome
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 提示文案。这里要钉住的不是措辞，是三件会真的骗到用户的事：
 *
 * 1. **每一种 [NoticeKind] 都有话说**。漏一个的表现是「屏幕上闪过一个空提示」。
 * 2. **[NoticeKind.LOCAL_HIT] 不许无条件承诺「贴合可能略有偏差」**。装的是服务端预建库
 *    时那句话是假的（它和联网命中一模一样），而这次改动的全部意义就是把质量提上来。
 * 3. **会自己好的才自动消失**。硬故障消失掉，用户就对着一个没有解释的空白画面等下去。
 */
class NoticesTest {

    @Test
    fun `除了 CLEARED 每一种都有一句人话`() {
        NoticeKind.entries.forEach { kind ->
            val text = Notices.text(kind, null)
            if (kind == NoticeKind.CLEARED) {
                assertNull("CLEARED 就是清掉提示", text)
            } else {
                assertNotNull("$kind 没有文案", text)
                assertTrue("$kind 的文案是空的", text!!.isNotBlank())
            }
        }
    }

    @Test
    fun `离线命中的文案由调用方按库来源填`() {
        // 只有 ScanRuntime 知道此刻装的是哪一份库（服务端预建 vs 端上现建），而两者的
        // 跟踪质量不是一档。状态机不该知道「多图库有两种」，所以这句话是 detail。
        assertEquals(
            "离线识别（服务端预建库），跟踪质量与联网时相同",
            Notices.text(NoticeKind.LOCAL_HIT, "离线识别（服务端预建库），跟踪质量与联网时相同"),
        )
    }

    @Test
    fun `离线命中没给来源时不承诺质量以外的东西`() {
        // 兜底那句按端上现建写：猜错方向必须是「别承诺质量」。
        val text = Notices.text(NoticeKind.LOCAL_HIT, null)!!
        assertTrue(text.contains("离线识别"))
        assertTrue("兜底要保守", text.contains("偏差"))
        assertEquals("空白 detail 等于没给", text, Notices.text(NoticeKind.LOCAL_HIT, "  "))
    }

    @Test
    fun `预建库装不上的提示不像报错`() {
        // 离线识别没消失，只是降了一档。说成故障会让人以为扫不出来了。
        val text = Notices.text(NoticeKind.TARGETS_DB_FALLBACK, "deserialize 失败")!!
        assertTrue(text.contains("离线识别"))
        // 原因（版本不匹配）对用户没有意义，能做的事在服务端 —— 细节留给日志
        assertFalse("异常原文不该甩到屏幕上", text.contains("deserialize"))
        assertTrue("这是一条一眼看完就够的提示", Notices.transient(NoticeKind.TARGETS_DB_FALLBACK))
    }

    @Test
    fun `自己不会好的提示不自动消失`() {
        listOf(
            NoticeKind.UNAUTHORIZED,
            NoticeKind.ASSET_MISSING,
            NoticeKind.VIDEO_UNPLAYABLE,
            NoticeKind.REF_STALE,
            NoticeKind.VIDEO_NOT_CACHED,
        ).forEach { assertFalse("这条在这次扫描里不会自己变好：$it", Notices.transient(it)) }
    }

    // ---- 保存到相册的结果文案 ----

    @Test
    fun `照片和视频都存成了`() {
        val t = Notices.saveResult(SaveOutcome("a.jpg", "a.mp4", emptyList()))
        assertEquals("已保存照片和视频到相册", t)
    }

    @Test
    fun `只有照片没有视频不算失败`() {
        val t = Notices.saveResult(SaveOutcome("a.jpg", null, emptyList()))
        assertEquals("已保存照片到相册", t)
    }

    @Test
    fun `部分成功要说清存了什么、什么没成`() {
        // 只说"保存失败"是错的：照片已经在相册里了，用户按第二次只会得到同名的
        // 第二份，而真正没成的是视频。
        val t = Notices.saveResult(SaveOutcome("a.jpg", null, listOf("视频：网络超时")))
        assertEquals("已保存照片到相册；但视频：网络超时", t)
    }

    @Test
    fun `全都失败时只报错`() {
        val t = Notices.saveResult(
            SaveOutcome(null, null, listOf("照片：401", "视频：401")),
        )
        assertEquals("保存失败：照片：401；视频：401", t)
    }

    @Test
    fun `既没存成也没报错时如实说，不显示空话`() {
        // 走到这里说明有分支忘了记 problems。宁可显示一句奇怪但真实的话，
        // 也不要显示"已保存到相册"而相册里什么都没有。
        val t = Notices.saveResult(SaveOutcome(null, null, emptyList()))
        assertEquals("没有可保存的内容", t)
    }
}

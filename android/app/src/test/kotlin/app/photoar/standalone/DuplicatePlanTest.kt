package app.photoar.standalone

import app.photoar.arview.LookupPhoto
import app.photoar.arview.LookupResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 重复上传之后的说法与动作。
 *
 * 八种组合里有几种的正确说法完全相反（「没什么要改的」 vs 「要不要换」），而它们在手机上
 * 各走一遍是不现实的 —— 尤其「照片入过库 + 已有视频 + 这次也挑了视频」那一种。
 */
class DuplicatePlanTest {

    private fun lookup(
        videoPath: String? = null,
        title: String? = "婚礼合照",
        photoId: String = "pid1",
        hasPhoto: Boolean = true,
    ) = LookupResult(
        path = "/media/photos/a.jpg",
        exists = true,
        kind = "image",
        photo = if (!hasPhoto) null else LookupPhoto(
            photoId = photoId,
            title = title,
            videoPath = videoPath,
            qualityScore = 80,
        ),
        usedByPhotos = emptyList(),
    )

    // ------------------------------------------------ 这次没挑视频

    @Test
    fun `只传了照片_库里那张已有视频_说清现状就够了`() {
        val out = DuplicatePlan.of(lookup(videoPath = "/media/videos/old.mp4"), null)
        assertNull("没有可做的动作", out.action)
        assertTrue(out.message.contains("已经在库里"))
        assertTrue("要说出它配的是哪段", out.message.contains("old.mp4"))
    }

    @Test
    fun `只传了照片_库里那张没视频_要指出这一点并说去哪配`() {
        // 这是最要紧的信息：那张照片扫到之后什么都不会播，而人以为已经配好了。
        val out = DuplicatePlan.of(lookup(videoPath = null), null)
        assertNull(out.action)
        assertTrue(out.message.contains("没有"))
        assertTrue("要说去哪儿配", out.message.contains("历史"))
    }

    // ------------------------------------------------ 这次挑了视频

    @Test
    fun `库里那张没视频_这次挑了_就是补上_不是替换`() {
        val out = DuplicatePlan.of(lookup(videoPath = null), "/media/videos/new.mp4")
        val a = out.action
        assertTrue(a is DuplicatePlan.Action.AttachVideo)
        a as DuplicatePlan.Action.AttachVideo
        assertEquals("pid1", a.photoId)
        assertEquals("/media/videos/new.mp4", a.videoPath)
        // 文案里必须说清「不会覆盖任何东西」，否则人不敢点
        assertTrue(a.confirm.contains("不会覆盖"))
    }

    @Test
    fun `库里那张已有别的视频_这次挑了另一段_是替换`() {
        val out = DuplicatePlan.of(
            lookup(videoPath = "/media/videos/old.mp4"), "/media/videos/new.mp4"
        )
        val a = out.action
        assertTrue(a is DuplicatePlan.Action.ReplaceVideo)
        a as DuplicatePlan.Action.ReplaceVideo
        assertEquals("pid1", a.photoId)
        assertEquals("/media/videos/new.mp4", a.videoPath)
        // 一张照片只能配一段视频 —— 这一点必须在确认文案里
        assertTrue("要说清是替换", a.confirm.contains("替换"))
        assertTrue("要说旧的那段去哪了", a.confirm.contains("old.mp4"))
        assertTrue("要说别的照片不受影响", a.confirm.contains("不受影响"))
        assertTrue("要说文件本身不删", a.confirm.contains("不删"))
    }

    @Test
    fun `同一张照片配同一段视频再传一遍_什么都不用做`() {
        // 「我忘了传过没有」的典型情形。报一个错会让人以为出了问题。
        val out = DuplicatePlan.of(
            lookup(videoPath = "/media/videos/same.mp4"), "/media/videos/same.mp4"
        )
        assertNull("已经是要的样子了，没有动作", out.action)
        assertTrue(out.message.contains("不用再做"))
    }

    @Test
    fun `尾随斜杠不影响同一个文件的判断`() {
        val out = DuplicatePlan.of(
            lookup(videoPath = "/media/videos/same.mp4/"), "/media/videos/same.mp4"
        )
        assertNull(out.action)
    }

    // ------------------------------------------------ 边界

    @Test
    fun `反查说这个文件不是任何照片的参考图时_如实说_不编原因`() {
        // 服务端说入过库、反查却查不到 —— 多半是刚被别人删了。
        val out = DuplicatePlan.of(lookup(hasPhoto = false), "/media/videos/new.mp4")
        assertNull(out.action)
        assertTrue(out.message.contains("查不到"))
        assertTrue("要给出下一步", out.message.contains("刷新"))
    }

    @Test
    fun `没有标题时用无标题占位_不显示空引号`() {
        for (t in listOf(null, "", "   ")) {
            val out = DuplicatePlan.of(lookup(title = t), null)
            assertTrue("title=$t 时应有占位：${out.message}", out.message.contains("(无标题)"))
        }
    }

    @Test
    fun `文案里用文件名而不是完整路径`() {
        val out = DuplicatePlan.of(
            lookup(videoPath = "/media/videos/2026/婚礼/迎宾视频.mp4"), null
        )
        assertTrue(out.message.contains("迎宾视频.mp4"))
        assertTrue("完整路径会把一行撑得没法读", !out.message.contains("/media/videos/2026"))
    }

    @Test
    fun `shortName 的边界`() {
        assertEquals("a.mp4", DuplicatePlan.shortName("/x/y/a.mp4"))
        assertEquals("a.mp4", DuplicatePlan.shortName("a.mp4"))
        assertEquals("y", DuplicatePlan.shortName("/x/y/"))
        // 全是斜杠时不能返回空串（界面上会变成一段空白）
        assertEquals("/", DuplicatePlan.shortName("/"))
    }

    @Test
    fun `消息永远非空`() {
        // 界面直接把它显示出来，空串会变成一个没有内容的横幅。
        val cases = listOf(
            lookup(videoPath = null),
            lookup(videoPath = "/v/a.mp4"),
            lookup(hasPhoto = false),
        )
        for (l in cases) {
            for (picked in listOf(null, "/v/b.mp4")) {
                assertTrue(DuplicatePlan.of(l, picked).message.isNotBlank())
            }
        }
    }
}

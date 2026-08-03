package app.photoar.standalone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 上传历史的编解码。
 *
 * 重点全在**坏存档**上：解析失败绝不能抛。历史只是个便利视图（真相在服务端的
 * `/v1/photo/<id>`），为它把整个素材页搞崩是完全不值的交换。
 */
class HistoryCodecTest {

    private fun e(
        photoId: String = "abc123",
        photoName: String = "a.jpg",
        videoName: String = "a.mp4",
        title: String = "合照",
        at: Long = 1_700_000_000_000L,
    ) = UploadHistory.Entry(photoId, photoName, videoName, title, at)

    @Test
    fun `往返`() {
        val list = listOf(e(), e(photoId = "def456", photoName = "b.jpg"))
        assertEquals(list, HistoryCodec.decode(HistoryCodec.encode(list)))
    }

    @Test
    fun `空列表往返`() {
        assertEquals(emptyList<UploadHistory.Entry>(), HistoryCodec.decode(HistoryCodec.encode(emptyList())))
    }

    @Test
    fun `没配视频时视频名是空串`() {
        val list = listOf(e(videoName = ""))
        val back = HistoryCodec.decode(HistoryCodec.encode(list))
        assertEquals("", back[0].videoName)
    }

    @Test
    fun `中文与特殊字符的文件名`() {
        // 相册给的名字什么样都有。JSON 转义要真的成立。
        val list = listOf(e(photoName = "婚礼 \"合照\".jpg", title = "第一天\n第二段"))
        assertEquals(list, HistoryCodec.decode(HistoryCodec.encode(list)))
    }

    // ---------------------------------------------------------------- 坏存档

    @Test
    fun `null 与空串给空列表`() {
        assertEquals(emptyList<UploadHistory.Entry>(), HistoryCodec.decode(null))
        assertEquals(emptyList<UploadHistory.Entry>(), HistoryCodec.decode(""))
        assertEquals(emptyList<UploadHistory.Entry>(), HistoryCodec.decode("   "))
    }

    @Test
    fun `整份不是 JSON 时给空列表_不抛`() {
        assertEquals(emptyList<UploadHistory.Entry>(), HistoryCodec.decode("这不是 json"))
        assertEquals(emptyList<UploadHistory.Entry>(), HistoryCodec.decode("{"))
    }

    @Test
    fun `是 JSON 但不是数组时给空列表`() {
        assertEquals(emptyList<UploadHistory.Entry>(), HistoryCodec.decode("""{"a":1}"""))
    }

    @Test
    fun `坏掉的那一条被跳过_其余留下`() {
        // 部分可读时保住能读的部分。整份丢掉会让人以为「历史清空了」。
        val raw = """[{"photoId":"good1"},"字符串不是对象",{"photoId":"good2"},123]"""
        val out = HistoryCodec.decode(raw)
        assertEquals(listOf("good1", "good2"), out.map { it.photoId })
    }

    @Test
    fun `没有 photoId 的条目被丢掉`() {
        // photoId 是问服务端的钥匙。没有它这条记录什么也做不了，留着只会在界面上
        // 变成一个点了没反应的条目。
        val raw = """[{"photoName":"a.jpg"},{"photoId":"","photoName":"b.jpg"},{"photoId":"ok"}]"""
        assertEquals(listOf("ok"), HistoryCodec.decode(raw).map { it.photoId })
    }

    @Test
    fun `缺字段的条目用默认值补齐`() {
        val out = HistoryCodec.decode("""[{"photoId":"x"}]""")
        assertEquals(1, out.size)
        assertEquals("x", out[0].photoId)
        assertEquals("", out[0].photoName)
        assertEquals("", out[0].videoName)
        assertEquals("", out[0].title)
        assertEquals(0L, out[0].at)
    }

    @Test
    fun `at 是字符串时不炸`() {
        // 手改过存档、或者以后换了格式。optLong 会给 0，那是「不知道时间」而不是崩。
        val out = HistoryCodec.decode("""[{"photoId":"x","at":"昨天"}]""")
        assertEquals(1, out.size)
        assertEquals(0L, out[0].at)
    }

    @Test
    fun `很长的存档也能解`() {
        val list = (1..UploadHistory.MAX).map { e(photoId = "id$it") }
        val back = HistoryCodec.decode(HistoryCodec.encode(list))
        assertEquals(UploadHistory.MAX, back.size)
        assertTrue(back.first().photoId == "id1")
    }
}

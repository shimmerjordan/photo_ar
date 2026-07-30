package app.photoar.standalone

import app.photoar.arview.ApiParseException
import app.photoar.arview.NetErrorKind
import app.photoar.arview.net.HttpFailure
import java.util.TimeZone
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 外壳里那些「差一位就错」的东西。界面本身在真机上肉眼可验，这些不行 ——
 * 打印宽度填错不会报错，只会让 AR 里的视频一直飘。
 */
class FmtTest {

    private val utc = TimeZone.getTimeZone("UTC")

    // ---- 打印宽度：这一组是全文件里最要紧的 ----

    @Test
    fun `横着的照片取长边`() {
        // print_width_m 是参考图**水平方向**的物理宽度（ARCore addImage 的第三个
        // 参数）。6寸相纸 102×152，横着放时水平方向是 152。
        assertEquals(152.0, Fmt.presetMm(Fmt.Paper.P6, landscape = true), 0.0)
        assertEquals(297.0, Fmt.presetMm(Fmt.Paper.A4, landscape = true), 0.0)
    }

    @Test
    fun `竖着的照片取短边`() {
        assertEquals(102.0, Fmt.presetMm(Fmt.Paper.P6, landscape = false), 0.0)
        assertEquals(210.0, Fmt.presetMm(Fmt.Paper.A4, landscape = false), 0.0)
    }

    @Test
    fun `每种相纸的短边都不比长边长`() {
        Fmt.Paper.entries.forEach {
            assertTrue("${it.label} 的两边填反了", it.shortMm < it.longMm)
        }
    }

    @Test
    fun `毫米数照原样解出来`() {
        assertEquals(152.0, Fmt.parseWidthMm("152")!!, 0.0)
        assertEquals(152.5, Fmt.parseWidthMm(" 152.5 ")!!, 0.0)
    }

    @Test
    fun `带单位也认`() {
        // 预设那几颗按钮填进去的是纯数字，但手输时带上「mm」是很自然的事
        assertEquals(152.0, Fmt.parseWidthMm("152mm")!!, 0.0)
        assertEquals(152.0, Fmt.parseWidthMm("152 毫米")!!, 0.0)
    }

    @Test
    fun `范围外的当没填`() {
        // 这个值直接进 ARCore 的物理宽度，填错不报错只是一直飘 —— 所以宁可不让提交
        assertNull(Fmt.parseWidthMm("9"))
        assertNull(Fmt.parseWidthMm("2001"))
        assertNull(Fmt.parseWidthMm("0"))
        assertNull(Fmt.parseWidthMm("-152"))
    }

    @Test
    fun `不是数字的当没填`() {
        assertNull(Fmt.parseWidthMm(""))
        assertNull(Fmt.parseWidthMm("   "))
        assertNull(Fmt.parseWidthMm("六寸"))
        assertNull(Fmt.parseWidthMm("152mmm"))
    }

    @Test
    fun `整毫米不带小数点`() {
        assertEquals("152", Fmt.mmText(152.0))
        assertEquals("152.5", Fmt.mmText(152.5))
    }

    @Test
    fun `米转毫米`() {
        assertEquals("152 mm", Fmt.widthMm(0.152f))
        // 0.089f * 1000 = 88.9999…：不先舍到 0.1mm 就会显示成「89.0 mm」，
        // 而 0.152f 会变成「151.9 mm」—— 看着像服务端存错了。
        assertEquals("89 mm", Fmt.widthMm(0.089f))
        // 非整毫米保留一位：25.4 和 25 在跟踪上不是一回事
        assertEquals("25.4 mm", Fmt.widthMm(0.0254f))
    }

    @Test
    fun `宽度缺失或为零时说未知而不是 0 mm`() {
        assertEquals("未知", Fmt.widthMm(0f))
        assertEquals("未知", Fmt.widthMm(-1f))
        assertEquals("未知", Fmt.widthMm(Float.NaN))
    }

    // ---- 字节与时间 ----

    @Test
    fun `字节数分档`() {
        assertEquals("512 B", Fmt.bytes(512))
        assertEquals("1.0 KB", Fmt.bytes(1024))
        assertEquals("4.3 KB", Fmt.bytes(4403))
        assertEquals("1.0 MB", Fmt.bytes(1024L * 1024))
        assertEquals("1.00 GB", Fmt.bytes(1024L * 1024 * 1024))
    }

    @Test
    fun `字节数为负说未知`() {
        assertEquals("未知", Fmt.bytes(-1))
    }

    @Test
    fun `时间戳按毫秒解`() {
        // 服务端所有时间戳都是 db_now_ms()。按秒解会显示成 1970 年。
        assertEquals("2024-10-27 03:33", Fmt.time(1730000000000L, utc))
        assertEquals("10-27 03:33:20", Fmt.timeShort(1730000000000L, utc))
    }

    @Test
    fun `没有时间时给破折号`() {
        // 服务端对缺失的时间给 0，显示成 1970-01-01 会让人以为数据坏了
        assertEquals("—", Fmt.time(0L, utc))
        assertEquals("—", Fmt.timeShort(0L, utc))
        assertEquals("—", Fmt.time(-1L, utc))
    }

    @Test
    fun `耗时分档`() {
        assertEquals("800 ms", Fmt.elapsed(800))
        assertEquals("42.5 秒", Fmt.elapsed(42_500))
        assertEquals("2 分 5 秒", Fmt.elapsed(125_000))
    }

    // ---- 质量分：75 是服务端的硬闸门 ----

    @Test
    fun `质量分档位以 75 为底`() {
        assertEquals("不达标", Fmt.qualityLabel(74))
        assertEquals("偏低", Fmt.qualityLabel(75))
        assertEquals("够用", Fmt.qualityLabel(80))
        assertEquals("很好", Fmt.qualityLabel(90))
        assertEquals("很好", Fmt.qualityLabel(100))
    }

    // ---- 路径 ----

    @Test
    fun `面包屑切开路径`() {
        assertEquals(listOf("share", "照片", "2024"), Fmt.crumbs("/share/照片/2024"))
    }

    @Test
    fun `末尾斜杠不产生空段`() {
        assertEquals(listOf("share", "照片"), Fmt.crumbs("/share/照片/"))
    }

    @Test
    fun `根目录列表没有面包屑`() {
        assertEquals(emptyList<String>(), Fmt.crumbs(null))
        assertEquals(emptyList<String>(), Fmt.crumbs(""))
        assertEquals(emptyList<String>(), Fmt.crumbs("/"))
    }

    @Test
    fun `面包屑不化简两个点`() {
        // 与 CatalogParse.joinPath 同一个理由：路径合法性由服务端 safepath 独家判定，
        // 客户端自己化简可能把一个本该被拒的路径洗白。这里只管显示。
        assertEquals(listOf("share", "..", "etc"), Fmt.crumbs("/share/../etc"))
    }

    @Test
    fun `目录标题取最后一段`() {
        assertEquals("2024", Fmt.dirTitle("/share/照片/2024"))
        assertEquals("NAS", Fmt.dirTitle(null))
    }

    // ---- 错误文案：401 必须单独说 ----

    @Test
    fun `令牌错了指到设置里去`() {
        // 归成一句「连接失败」会让人去查路由和防火墙，而问题在设置页那一行。
        val text = Fmt.errText(HttpFailure(NetErrorKind.UNAUTHORIZED, 401, "/v1/photos → 未授权"))
        assertTrue(text.contains("令牌"))
        assertTrue(text.contains("设置"))
        assertTrue(text.contains("401"))
    }

    @Test
    fun `超时和连不上分开说`() {
        assertTrue(Fmt.errText(HttpFailure(NetErrorKind.TIMEOUT, null, "超时")).contains("超时"))
        assertTrue(
            Fmt.errText(HttpFailure(NetErrorKind.TRANSPORT, null, "unreachable"))
                .contains("连不上"),
        )
    }

    @Test
    fun `服务端出错带上原文`() {
        val text = Fmt.errText(HttpFailure(NetErrorKind.SERVER_ERROR, 500, "/v1/photo → 转码失败"))
        assertTrue(text.contains("服务端出错"))
        assertTrue(text.contains("转码失败"))
    }

    @Test
    fun `响应解析失败也说人话`() {
        assertTrue(Fmt.errText(ApiParseException("photos 里有一项没有 photoId")).contains("photoId"))
    }

    @Test
    fun `没有 message 的异常退回类名`() {
        // 不能返回空串：那会让出错页变成一片空白，看着像加载中卡住了。
        assertEquals("IllegalStateException", Fmt.errText(IllegalStateException()))
        assertEquals("IllegalStateException", Fmt.errText(IllegalStateException("  ")))
    }
}

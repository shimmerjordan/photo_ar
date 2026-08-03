package app.photoar.standalone

import app.photoar.arview.ApiParseException
import app.photoar.arview.NetErrorKind
import app.photoar.arview.cache.CacheSync
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
    fun `凭证失效指到设置里去重新登录`() {
        // 归成一句「连接失败」会让人去查路由和防火墙，而问题在设置页那一块。
        //
        // Phase 5 改了措辞而不是改了要求：服务端从「一个预共享 token」换成了用户体系，
        // 401 的最常见原因是**登录过期**（管理员 12 小时、访客 30 天），而不是「令牌
        // 填错了」。三条断言的强度不变 —— 要说清是登录问题、要指出去哪里解决、
        // 要带上状态码。
        val text = Fmt.errText(HttpFailure(NetErrorKind.UNAUTHORIZED, 401, "/v1/photos → 未授权"))
        assertTrue(text.contains("登录"))
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

    // ---- 登录失败（Phase 5）----

    @Test
    fun `登录失败不能用 errText 那句「回设置里重新登录」`() {
        // 那句话在登录界面上是个循环：用户已经在设置里，正对着登录框。
        val e = HttpFailure(
            NetErrorKind.BAD_CREDENTIALS,
            401,
            "/v1/auth/login → 口令不对",
            "bad_credentials",
        )
        val text = Fmt.loginErr(e)
        assertTrue(text.contains("口令"))
        assertTrue("不该再指回设置", !text.contains("回「设置」"))
    }

    @Test
    fun `名字不在册与口令错给不同文案`() {
        val unknown = Fmt.loginErr(
            HttpFailure(NetErrorKind.FORBIDDEN, 403, "没有这个用户", "unknown_user"),
        )
        val bad = Fmt.loginErr(
            HttpFailure(NetErrorKind.BAD_CREDENTIALS, 401, "口令不对", "bad_credentials"),
        )
        assertTrue(unknown != bad)
        assertTrue("要说清重试没用", unknown.contains("再试一次也不会成"))
    }

    @Test
    fun `不是 HttpFailure 的异常也有话说`() {
        assertTrue(Fmt.loginErr(java.io.IOException("socket closed")).isNotBlank())
        assertTrue(Fmt.loginErr(ApiParseException("不是 JSON")).isNotBlank())
        assertTrue(Fmt.loginErr(IllegalStateException()).isNotBlank())
    }

    // ---- 离线识别库（Phase 6）----

    @Test
    fun `预建库的每一种结局都有各自的一句话`() {
        // 归成同一句「没同步上」的话，用户没法知道该「过一会儿再来」还是「去找管理员」——
        // 而前者按一下就好了。
        val texts = CacheSync.TargetsStatus.entries.map { s ->
            Fmt.prebuiltStatus(CacheSync.TargetsResult(status = s, count = 3, bytes = 4096))
        }
        assertEquals("有两种说的是同一句话", texts.size, texts.toSet().size)
        texts.forEach { assertTrue(it.isNotBlank()) }
    }

    @Test
    fun `正在建要说清过一会儿再来`() {
        val text = Fmt.prebuiltStatus(
            CacheSync.TargetsResult(status = CacheSync.TargetsStatus.BUILDING),
        )
        assertTrue(text.contains("再同步"))
    }

    @Test
    fun `预建库没拿到时要说清扫描不受影响`() {
        // 它失败时其余部分是全好的，而且认不出来会自动落回服务端识别。不说的话，
        // 一句「离线识别库没拿到」看起来像整个功能坏了。
        val text = Fmt.prebuiltStatus(
            CacheSync.TargetsResult(
                status = CacheSync.TargetsStatus.FAILED,
                detail = "连不上服务端",
            ),
        )
        assertTrue(text.contains("连不上服务端"))
        assertTrue(text.contains("扫描不受影响"))
    }

    @Test
    fun `更新成功时报出张数与体积`() {
        val text = Fmt.prebuiltStatus(
            CacheSync.TargetsResult(
                status = CacheSync.TargetsStatus.DOWNLOADED,
                count = 137,
                bytes = 6L * 1024 * 1024,
            ),
        )
        assertTrue(text.contains("137"))
        assertTrue(text.contains("6.0 MB"))
    }

    @Test
    fun `没有照片被挤掉时不说话`() {
        // 「有 0 张没进库」是纯噪声。
        assertNull(Fmt.overflowNote(0, 1000))
        assertNull(Fmt.overflowNote(-1, 1000))
    }

    @Test
    fun `有照片被挤掉时说清张数与它不是坏了`() {
        val note = Fmt.overflowNote(37, 1000)!!
        assertTrue(note.contains("37"))
        assertTrue("上限是这件事唯一的原因", note.contains("1000"))
        assertTrue("必须说清联网照样能扫", note.contains("联网"))
    }

    @Test
    fun `不知道上限时也能说`() {
        // maxTargets 是服务端给的，老服务端可能不给。这句话的重点是「有 N 张没进去」。
        val note = Fmt.overflowNote(2, 0)!!
        assertTrue(note.contains("2"))
        assertTrue(note.contains("上限"))
    }
}

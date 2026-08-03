package app.photoar.arview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class GeometryTest {

    // ---- 四边形尺寸：尺度取 ARCore、形状取参考图 ----
    //
    // 这一组测试钉住的是「贴合」的核心决定。参数顺序是
    // quadSize(extentX, extentZ, printWidthM, refAspect)。

    @Test
    fun `六寸照片按 3 比 2 算出高度`() {
        val q = Geometry.quadSize(0.152f, 0.101f, 0.152f, 1.5f)!!
        assertEquals(0.152f, q.widthM, 1e-6f)
        assertEquals(0.152f / 1.5f, q.heightM, 1e-6f)
    }

    @Test
    fun `竖版照片的比例小于 1`() {
        val q = Geometry.quadSize(0.102f, 0.153f, 0.102f, 2f / 3f)!!
        assertTrue("竖版应该比宽度高", q.heightM > q.widthM)
    }

    @Test
    fun `尺度优先用 ARCore 量的，不用申报的`() {
        // 申报 30cm、ARCore 量到 15cm。四边形必须是 15cm —— 位姿是 ARCore 给的，
        // 尺度不跟它一致就会在屏幕上大一倍。这是「不贴合」的直接原因。
        val q = Geometry.quadSize(0.15f, 0.10f, 0.30f, 1.5f)!!
        assertEquals(0.15f, q.widthM, 1e-6f)
    }

    @Test
    fun `宽度未知时完全靠 ARCore 量的那个值`() {
        val q = Geometry.quadSize(0.22f, 0.146f, 0f, 1.5f)!!
        assertEquals(0.22f, q.widthM, 1e-6f)
        assertEquals(0.22f / 1.5f, q.heightM, 1e-6f)
    }

    @Test
    fun `ARCore 还没给出 extent 时用申报宽度垫着`() {
        val q = Geometry.quadSize(0f, 0f, 0.152f, 1.5f)!!
        assertEquals(0.152f, q.widthM, 1e-6f)
    }

    @Test
    fun `两个宽度都没有就不画`() {
        // 返回 null，调用方跳过这一帧。宁可不画也不要按兜底值画一个错的大小。
        assertEquals(null, Geometry.quadSize(0f, 0f, 0f, 1.5f))
        assertEquals(null, Geometry.quadSize(Float.NaN, 0f, Float.NaN, 1.5f))
    }

    @Test
    fun `形状优先用 refAspect，不用 ARCore 的 extentZ`() {
        // extentX/extentZ = 2.0，但参考图是 1.5。必须按 1.5 —— 收敛期 extentZ 会偏，
        // 照抄它会把视频拉变形，而变形比略大略小难看得多（人脸比例极敏感）。
        val q = Geometry.quadSize(0.20f, 0.10f, 0f, 1.5f)!!
        assertEquals(0.20f / 1.5f, q.heightM, 1e-6f)
    }

    @Test
    fun `缺 refAspect 时退回 ARCore 量的比例`() {
        val q = Geometry.quadSize(0.152f, 0.152f / 1.25f, 0.152f, null)!!
        assertEquals(0.152f / 1.25f, q.heightM, 1e-4f)
    }

    @Test
    fun `两个比例都拿不到时用 3 比 2 兜底`() {
        val q = Geometry.quadSize(0.152f, 0f, 0.152f, null)!!
        assertEquals(0.152f / Geometry.FALLBACK_ASPECT, q.heightM, 1e-6f)
    }

    @Test
    fun `离谱的比例不采用`() {
        // 服务端算 refAspect 用的是参考图的像素宽高，元数据坏了会给出 0.001
        val q = Geometry.quadSize(0.152f, 0.152f / 1.5f, 0.152f, 0.001f)!!
        assertEquals(0.152f / 1.5f, q.heightM, 1e-4f)
    }

    @Test
    fun `非有限的比例不采用`() {
        val q = Geometry.quadSize(0.152f, 0f, 0.152f, Float.NaN)!!
        assertEquals(0.152f / Geometry.FALLBACK_ASPECT, q.heightM, 1e-6f)
    }

    @Test
    fun `离谱的测量值被弃用，落回申报宽度`() {
        // 收敛早期 ARCore 可能给出荒唐的数。不夹取而是弃用：夹取会破坏
        // extentX 与 centerPose 的自洽，那正是这套设计要避免的事。
        val q = Geometry.quadSize(999f, 0f, 0.152f, 1.5f)!!
        assertEquals(0.152f, q.widthM, 1e-6f)
    }

    @Test
    fun `两米以上的大幅照片不算数据错误`() {
        // 测量值的上限比人填的那一档宽（婚礼现场挂三米喷绘很正常）。用 2m 卡它
        // 会把一张真实存在的大照片判成坏数据，然后视频不显示、日志里什么都没有。
        val q = Geometry.quadSize(3.0f, 2.0f, 0f, 1.5f)
        assertTrue("3 米宽应该被接受", q != null)
        assertEquals(3.0f, q!!.widthM, 1e-6f)
    }

    @Test
    fun `人填的宽度仍然守着窄区间`() {
        // ARCore 没给 extent 时，申报值走的是窄档 —— 它防的是打字错误。
        assertEquals(null, Geometry.quadSize(0f, 0f, 50f, 1.5f))
        assertEquals(null, Geometry.quadSize(0f, 0f, 0.0001f, 1.5f))
    }

    // ---- 视频那块矩形（装进去，不裁切） ----

    @Test
    fun `比例一致时视频正好等于照片`() {
        val photo = Geometry.QuadSize(0.15f, 0.10f)
        val v = Geometry.videoQuad(photo, 1.5f)
        assertEquals(0.15f, v.widthM, 1e-6f)
        assertEquals(0.10f, v.heightM, 1e-6f)
    }

    @Test
    fun `十六比九的视频贴到三比二的照片上宽度顶满`() {
        val photo = Geometry.QuadSize(0.15f, 0.10f) // 3:2
        val v = Geometry.videoQuad(photo, 16f / 9f)
        assertEquals("视频更宽，宽度顶满", 0.15f, v.widthM, 1e-6f)
        assertEquals(0.15f / (16f / 9f), v.heightM, 1e-6f)
        assertTrue("高度必须比照片矮，露出的是照片本身", v.heightM < photo.heightM)
    }

    @Test
    fun `竖版视频贴到横版照片上高度顶满`() {
        val photo = Geometry.QuadSize(0.15f, 0.10f)
        val v = Geometry.videoQuad(photo, 9f / 16f)
        assertEquals(0.10f, v.heightM, 1e-6f)
        assertEquals(0.10f * (9f / 16f), v.widthM, 1e-6f)
        assertTrue(v.widthM < photo.widthM)
    }

    @Test
    fun `永远装得进照片、且比例永远是视频的`() {
        // 「不变形」是这个函数唯一不能违反的性质：人眼对人脸比例极其敏感，
        // 拉扁一点点就比小一圈难看得多。
        val photo = Geometry.QuadSize(0.15f, 0.10f)
        for (v in listOf(0.4f, 0.5f, 0.75f, 1f, 1.33f, 1.5f, 1.78f, 2.35f, 3f)) {
            val q = Geometry.videoQuad(photo, v)
            assertTrue("$v 超出照片宽", q.widthM <= photo.widthM + 1e-6f)
            assertTrue("$v 超出照片高", q.heightM <= photo.heightM + 1e-6f)
            assertEquals("$v 变形了", v, q.aspect, 1e-4f)
        }
    }

    @Test
    fun `恰好有一个维度和照片贴满，另一个不超出`() {
        // 用户对「差不多」的定义，逐字：「至少有一个维度（长或者宽）是贴合图片的，
        // 按视频最大化完整显示为准」。也就是说这个函数必须同时满足三件事：
        //   1. 有一条边和照片严丝合缝（不是缩在中间留四条边）
        //   2. 另一条边不超出照片
        //   3. 视频完整、不变形（比例是视频自己的）
        // 三条里少任何一条都是另一种取舍，所以整条钉住。
        val photo = Geometry.QuadSize(0.15f, 0.10f)
        for (v in listOf(0.4f, 0.6f, 0.75f, 1f, 1.5f, 1.78f, 2.35f, 3f)) {
            val q = Geometry.videoQuad(photo, v)
            val wFlush = kotlin.math.abs(q.widthM - photo.widthM) < 1e-6f
            val hFlush = kotlin.math.abs(q.heightM - photo.heightM) < 1e-6f
            assertTrue("$v：一条边都没贴满（${q.widthM}×${q.heightM}）", wFlush || hFlush)
            assertTrue("$v 超出了照片", q.widthM <= photo.widthM + 1e-6f && q.heightM <= photo.heightM + 1e-6f)
            assertEquals("$v 变形了", v, q.aspect, 1e-4f)
        }
    }

    @Test
    fun `视频尺寸还没报上来时按照片的形状铺`() {
        val photo = Geometry.QuadSize(0.15f, 0.10f)
        assertEquals(photo, Geometry.videoQuad(photo, 0f))
        assertEquals(photo, Geometry.videoQuad(photo, Float.NaN))
    }

    // ---- 淡入 ----

    @Test
    fun `淡入从 0 到 1`() {
        assertEquals(0f, Geometry.fadeAlpha(0), 1e-6f)
        assertEquals(0.5f, Geometry.fadeAlpha(150, 300), 1e-6f)
        assertEquals(1f, Geometry.fadeAlpha(300, 300), 1e-6f)
        assertEquals("不能超过 1", 1f, Geometry.fadeAlpha(9999, 300), 1e-6f)
        assertEquals("负数当 0", 0f, Geometry.fadeAlpha(-5, 300), 1e-6f)
    }
}

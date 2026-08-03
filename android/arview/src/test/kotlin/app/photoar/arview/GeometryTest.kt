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

    // ---- 纹理裁切 ----

    @Test
    fun `比例一致时整张铺满`() {
        val uv = Geometry.fillCropUv(1.5f, 1.5f)
        assertEquals(1f, uv.uScale, 1e-6f)
        assertEquals(1f, uv.vScale, 1e-6f)
        assertEquals(0f, uv.uOffset, 1e-6f)
        assertEquals(0f, uv.vOffset, 1e-6f)
    }

    @Test
    fun `十六比九的视频贴到三比二的照片上切左右`() {
        val uv = Geometry.fillCropUv(quadAspect = 1.5f, videoAspect = 16f / 9f)
        assertEquals(1.5f / (16f / 9f), uv.uScale, 1e-6f)
        assertEquals(1f, uv.vScale, 1e-6f)
        assertEquals("裁切必须居中", (1f - uv.uScale) / 2f, uv.uOffset, 1e-6f)
    }

    @Test
    fun `竖版视频贴到横版照片上切上下`() {
        val uv = Geometry.fillCropUv(quadAspect = 1.5f, videoAspect = 9f / 16f)
        assertEquals(1f, uv.uScale, 1e-6f)
        assertEquals((9f / 16f) / 1.5f, uv.vScale, 1e-6f)
        assertEquals((1f - uv.vScale) / 2f, uv.vOffset, 1e-6f)
    }

    @Test
    fun `裁切后的可见区域始终在 0 到 1 之间`() {
        val cases = listOf(0.5f, 0.75f, 1f, 1.33f, 1.5f, 1.78f, 2.35f)
        for (q in cases) for (v in cases) {
            val uv = Geometry.fillCropUv(q, v)
            assertTrue("$q/$v", uv.uOffset >= -1e-6f && uv.uOffset + uv.uScale <= 1f + 1e-6f)
            assertTrue("$q/$v", uv.vOffset >= -1e-6f && uv.vOffset + uv.vScale <= 1f + 1e-6f)
        }
    }

    @Test
    fun `视频尺寸还没报上来时整张铺满`() {
        val uv = Geometry.fillCropUv(1.5f, 0f)
        assertEquals(1f, uv.uScale, 1e-6f)
        assertEquals(1f, uv.vScale, 1e-6f)
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

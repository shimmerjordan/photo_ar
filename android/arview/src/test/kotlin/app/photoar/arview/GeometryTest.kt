package app.photoar.arview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class GeometryTest {

    @Test
    fun `六寸照片按 3 比 2 算出高度`() {
        val q = Geometry.printedSize(0.152f, 1.5f)
        assertEquals(0.152f, q.widthM, 1e-6f)
        assertEquals(0.152f / 1.5f, q.heightM, 1e-6f)
    }

    @Test
    fun `竖版照片的比例小于 1`() {
        val q = Geometry.printedSize(0.102f, 2f / 3f)
        assertTrue("竖版应该比宽度高", q.heightM > q.widthM)
    }

    @Test
    fun `缺 refAspect 时退回 ARCore 量的比例`() {
        val q = Geometry.printedSize(0.152f, null, arcoreAspect = 1.25f)
        assertEquals(0.152f / 1.25f, q.heightM, 1e-6f)
    }

    @Test
    fun `两个比例都拿不到时用 3 比 2 兜底`() {
        val q = Geometry.printedSize(0.152f, null, null)
        assertEquals(0.152f / Geometry.FALLBACK_ASPECT, q.heightM, 1e-6f)
    }

    @Test
    fun `离谱的比例不采用`() {
        // 服务端算 refAspect 用的是参考图的像素宽高，元数据坏了会给出 0.001
        val q = Geometry.printedSize(0.152f, 0.001f, arcoreAspect = 1.5f)
        assertEquals(0.152f / 1.5f, q.heightM, 1e-6f)
    }

    @Test
    fun `非有限的比例不采用`() {
        val q = Geometry.printedSize(0.152f, Float.NaN, null)
        assertEquals(0.152f / Geometry.FALLBACK_ASPECT, q.heightM, 1e-6f)
    }

    @Test
    fun `宽度被夹在可信区间内`() {
        assertEquals(Geometry.MIN_WIDTH_M, Geometry.printedSize(0.0001f, 1.5f).widthM, 1e-6f)
        assertEquals(Geometry.MAX_WIDTH_M, Geometry.printedSize(50f, 1.5f).widthM, 1e-6f)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `宽度为零直接报错`() {
        Geometry.printedSize(0f, 1.5f)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `宽度为 NaN 直接报错`() {
        Geometry.printedSize(Float.NaN, 1.5f)
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

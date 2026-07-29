package app.photoar.arview

import java.nio.ByteBuffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class FramesTest {

    // ---- 缩放 ----

    @Test
    fun `长边缩到 640`() {
        val s = Frames.targetSize(1920, 1080)
        assertEquals(640, s.width)
        assertEquals(360, s.height)
    }

    @Test
    fun `竖版按高度缩`() {
        val s = Frames.targetSize(1080, 1920)
        assertEquals(640, s.height)
        assertEquals(360, s.width)
    }

    @Test
    fun `已经不超过 640 就原样返回`() {
        val s = Frames.targetSize(640, 480)
        assertEquals(640, s.width)
        assertEquals(480, s.height)
    }

    @Test
    fun `绝不放大`() {
        val s = Frames.targetSize(320, 240)
        assertEquals(320, s.width)
        assertEquals(240, s.height)
    }

    @Test
    fun `缩放结果是偶数`() {
        // 1440x1079 缩到长边 640 → 高度 479.5，不取偶数会给下游的 YUV 转换
        // 埋一个半行的坑
        val s = Frames.targetSize(1440, 1079)
        assertEquals(0, s.width % 2)
        assertEquals(0, s.height % 2)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `零尺寸报错`() {
        Frames.targetSize(0, 480)
    }

    // ---- NV21 打包 ----

    @Test
    fun `无补齐时逐字节正确`() {
        val w = 4
        val h = 2
        val y = ByteBuffer.wrap(byteArrayOf(1, 2, 3, 4, 5, 6, 7, 8))
        val u = ByteBuffer.wrap(byteArrayOf(10, 11))
        val v = ByteBuffer.wrap(byteArrayOf(20, 21))
        val out = ByteArray(Frames.nv21Size(w, h))
        Frames.toNv21(w, h, y, 4, u, 2, 1, v, 2, 1, out)
        // Y 平面原样，然后是 V U 交错
        assertEquals(
            listOf<Byte>(1, 2, 3, 4, 5, 6, 7, 8, 20, 10, 21, 11),
            out.toList(),
        )
    }

    @Test
    fun `rowStride 大于 width 时把补齐字节丢掉`() {
        val w = 2
        val h = 2
        // 每行 4 字节，后 2 字节是补齐（相机为对齐加的）
        val y = ByteBuffer.wrap(byteArrayOf(1, 2, 99, 99, 3, 4, 99, 99))
        val u = ByteBuffer.wrap(byteArrayOf(10, 98, 98, 98))
        val v = ByteBuffer.wrap(byteArrayOf(20, 98, 98, 98))
        val out = ByteArray(Frames.nv21Size(w, h))
        Frames.toNv21(w, h, y, 4, u, 4, 1, v, 4, 1, out)
        assertEquals(listOf<Byte>(1, 2, 3, 4, 20, 10), out.toList())
        assertTrue("补齐字节不该出现在结果里", !out.contains(99))
    }

    @Test
    fun `pixelStride 为 2 的交错平面采样正确`() {
        val w = 4
        val h = 2
        val y = ByteBuffer.wrap(ByteArray(8) { (it + 1).toByte() })
        // 真机上最常见的形态：U 与 V 共用一段内存，交错存放，V 起点在 U 前一字节
        val uv = byteArrayOf(20, 10, 21, 11)
        val u = ByteBuffer.wrap(uv, 1, 3).slice()
        val v = ByteBuffer.wrap(uv, 0, 4).slice()
        val out = ByteArray(Frames.nv21Size(w, h))
        Frames.toNv21(w, h, y, 4, u, 4, 2, v, 4, 2, out)
        assertEquals(
            listOf<Byte>(1, 2, 3, 4, 5, 6, 7, 8, 20, 10, 21, 11),
            out.toList(),
        )
    }

    @Test(expected = IllegalArgumentException::class)
    fun `输出缓冲太小报错`() {
        Frames.toNv21(
            4, 2,
            ByteBuffer.wrap(ByteArray(8)), 4,
            ByteBuffer.wrap(ByteArray(2)), 2, 1,
            ByteBuffer.wrap(ByteArray(2)), 2, 1,
            ByteArray(5),
        )
    }

    @Test(expected = IllegalArgumentException::class)
    fun `奇数宽高报错`() {
        Frames.toNv21(
            3, 2,
            ByteBuffer.wrap(ByteArray(6)), 3,
            ByteBuffer.wrap(ByteArray(2)), 2, 1,
            ByteBuffer.wrap(ByteArray(2)), 2, 1,
            ByteArray(Frames.nv21Size(4, 2)),
        )
    }

    @Test
    fun `不改动输入缓冲的位置`() {
        val y = ByteBuffer.wrap(ByteArray(8))
        val u = ByteBuffer.wrap(ByteArray(2))
        val v = ByteBuffer.wrap(ByteArray(2))
        Frames.toNv21(4, 2, y, 4, u, 2, 1, v, 2, 1, ByteArray(12))
        // 用绝对 get 读，否则同一帧被读两次（比如加了本地缓存索引之后）就空了
        assertEquals(0, y.position())
        assertEquals(0, u.position())
        assertEquals(0, v.position())
    }
}

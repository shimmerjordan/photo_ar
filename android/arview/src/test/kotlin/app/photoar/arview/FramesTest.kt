package app.photoar.arview

import java.nio.ByteBuffer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class FramesTest {

    // ---- 缩放 ----

    @Test
    fun `发帧长边是 1280 而不是 640`() {
        // 这个常量要和服务端的 `backend.QUERY_LONG_EDGE`(1280，处理长边) +
        // `QUERY_N_FEATURES`(4000) 一起看。实测（bench/simcam.py，用户的真实婚礼照
        // + 手机拍的真实桌面场景，5 个视角取「全部过门槛」）：
        //   发帧 640  / 服务端处理 640  / 4000 → 一档都不全过（现状，真机扫不出来）
        //   发帧 1280 / 服务端处理 640  / 4000 → 一档都不全过 ← 只改这一半 = 白付流量
        //   发帧 640  / 服务端处理 1280 / 4000 → 0.5
        //   发帧 1280 / 服务端处理 1280 / 4000 → 0.4；粗排 Top-20 命中 5/20 → 20/20
        // 主导变量是服务端处理长边；这个常量把 0.5 推到 0.4，退回 640 也只掉到 0.5。
        assertEquals(1280, Frames.LONG_EDGE)
    }

    @Test
    fun `长边缩到 1280`() {
        val s = Frames.targetSize(1920, 1080)
        assertEquals(1280, s.width)
        assertEquals(720, s.height)
    }

    @Test
    fun `竖版按高度缩`() {
        val s = Frames.targetSize(1080, 1920)
        assertEquals(1280, s.height)
        assertEquals(720, s.width)
    }

    @Test
    fun `已经不超过长边就原样返回`() {
        val s = Frames.targetSize(1280, 960)
        assertEquals(1280, s.width)
        assertEquals(960, s.height)
    }

    @Test
    fun `绝不放大`() {
        val s = Frames.targetSize(640, 480)
        assertEquals(640, s.width)
        assertEquals(480, s.height)
    }

    @Test
    fun `缩放结果是偶数`() {
        // 2880x2159 缩到长边 1280 → 高度 959.5，不取偶数会给下游的 YUV 转换
        // 埋一个半行的坑
        val s = Frames.targetSize(2880, 2159)
        assertEquals(0, s.width % 2)
        assertEquals(0, s.height % 2)
    }

    // ---- 相机档位挑选 ----
    //
    // 这一段本来内联在 Camera2Source.pickSize 里（`minByOrNull { abs(长边 - LONG_EDGE) }`
    // 外加两处硬编码的 `Size(640, 480)` 回退）。抽出来的理由不是美观：`targetSize`
    // **绝不放大**，所以相机给多大就决定了识别能看到多少像素 —— 相机仍出 640 的话，
    // 服务端提 4000 个特征是空转。而那个 480p 回退在长边升到 1280 之后就变成了
    // 「静默降级回旧行为」：不报错、不留日志、识别率掉回 0。

    @Test
    fun `有正好等于长边的档位就选它`() {
        val s = Frames.pickCameraSize(
            listOf(size(640, 480), size(1280, 720), size(1920, 1080)),
        )
        assertEquals(Frames.Size(1280, 720), s)
    }

    @Test
    fun `没有正好的就选大的那边最接近的`() {
        // 960 与 1600 到 1280 的距离一样远（都是 320），但 960 只能靠插值放大补到
        // 1280，那是凭空造像素；1600 缩到 1280 是真信息。所以必须偏大的那个。
        val s = Frames.pickCameraSize(listOf(size(960, 540), size(1600, 900)))
        assertEquals(Frames.Size(1600, 900), s)
    }

    @Test
    fun `全都小于长边时取最大的那个而不是回退到 480p`() {
        val s = Frames.pickCameraSize(listOf(size(640, 480), size(800, 600)))
        assertEquals(Frames.Size(800, 600), s)
    }

    @Test
    fun `太小的档位直接排除`() {
        val s = Frames.pickCameraSize(listOf(size(176, 144), size(320, 240)))
        assertEquals(Frames.Size(320, 240), s)
    }

    @Test
    fun `一个可用档位都没有时返回 null 让调用方去报错`() {
        // 返回 null 而不是替它编一个 640x480：编出来的档位相机未必支持，
        // 真机上表现为配置会话失败，排查时完全看不出是这里替它决定的。
        assertEquals(null, Frames.pickCameraSize(listOf(size(176, 144))))
        assertEquals(null, Frames.pickCameraSize(emptyList()))
    }

    private fun size(w: Int, h: Int) = Frames.Size(w, h)

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

    // ---- 档位选择：尺寸优先，再在同尺寸里取最高帧率 ----

    @Test
    fun `同尺寸里取最高帧率`() {
        val got = Frames.pickCameraOption(
            listOf(
                Frames.CameraOption(Frames.Size(1440, 1080), 30),
                Frames.CameraOption(Frames.Size(1440, 1080), 60),
            )
        )
        assertEquals(Frames.CameraOption(Frames.Size(1440, 1080), 60), got)
    }

    @Test
    fun `不为了帧率牺牲 CPU 图像尺寸`() {
        // 某些机型只在 640x480 上给 60fps。挑它的话跟踪很稳，但**永远认不出照片**
        // （实测处理长边 640 时一档都不全过）—— 识别是硬约束，帧率是择优。
        val got = Frames.pickCameraOption(
            listOf(
                Frames.CameraOption(Frames.Size(640, 480), 60),
                Frames.CameraOption(Frames.Size(1440, 1080), 30),
            )
        )
        assertEquals(Frames.CameraOption(Frames.Size(1440, 1080), 30), got)
    }

    @Test
    fun `尺寸相同的选择规则与 pickCameraSize 一致`() {
        // 尺寸那一半的规则不重复实现，全部委托给 pickCameraSize；这里钉住这个委托关系。
        val sizes = listOf(Frames.Size(640, 480), Frames.Size(1280, 960), Frames.Size(1920, 1440))
        val want = Frames.pickCameraSize(sizes)
        val got = Frames.pickCameraOption(sizes.map { Frames.CameraOption(it, 30) })
        assertEquals(want, got?.size)
    }

    @Test
    fun `一个可用档位都没有时返回 null`() {
        assertNull(Frames.pickCameraOption(emptyList()))
        // 全部低于最小边长
        assertNull(
            Frames.pickCameraOption(listOf(Frames.CameraOption(Frames.Size(160, 120), 60)))
        )
    }

    @Test
    fun `没有达标尺寸时退而取最大的，帧率仍在同尺寸里择优`() {
        val got = Frames.pickCameraOption(
            listOf(
                Frames.CameraOption(Frames.Size(640, 480), 30),
                Frames.CameraOption(Frames.Size(800, 600), 30),
                Frames.CameraOption(Frames.Size(800, 600), 60),
            )
        )
        assertEquals(Frames.CameraOption(Frames.Size(800, 600), 60), got)
    }
}

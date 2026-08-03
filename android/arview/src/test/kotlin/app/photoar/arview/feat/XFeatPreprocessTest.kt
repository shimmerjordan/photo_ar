package app.photoar.arview.feat

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 预处理契约的 Kotlin 一侧。**另一侧在 `tests/test_xfeat_prepare.py`。**
 *
 * [GOLDEN_LANDSCAPE_SUM] / [GOLDEN_PORTRAIT_SUM] 与那几个定点值是从 Python 侧
 * `xfeat.prepare()` 的真实输出取的，并在那个文件里逐字重复了一遍。一份改了另一份不改，
 * 两边各有一条测试红 —— 这是这条路上唯一能自动发现「两份实现走偏了」的机制。
 *
 * 为什么必须有这个机制：描述子对不上**不会报错**，只会让识别率静默变低。没有 golden
 * 的话，「补边补错了一边」这种改动会一路走到真机，然后表现为「扫不太出来」。
 *
 * ## 合成图为什么长这样
 *
 * 每个 2×2 块内四个像素完全相同，且长边正好是 640 的两倍。于是缩放比恰好 0.5、块平均
 * 恰好等于块值（整数），任何正确的面积平均实现都必然给出同一个结果 —— 把
 * `cvRound`（round-half-to-even）与 `Math.round`（round-half-up）这个差异消掉。
 * 三个通道取不同的线性函数，所以「RGB 与 BGR 反了」会立刻表现成数值不对。
 */
class XFeatPreprocessTest {

    private companion object {
        const val GOLDEN_LANDSCAPE_SUM = 156_668_672L
        const val GOLDEN_PORTRAIT_SUM = 156_683_520L
        const val PLANE = CANVAS * CANVAS
    }

    /** 与 Python 侧 `block_image` 逐字对应，只是这里直接产出 ARGB。 */
    private fun blockImage(height: Int, width: Int): ArgbPixels {
        val px = IntArray(width * height)
        for (y in 0 until height) {
            val v = y / 2
            for (x in 0 until width) {
                val u = x / 2
                val b = (17 * u + 5 * v) % 256
                val g = (31 * u + 11 * v) % 256
                val r = (7 * u + 23 * v) % 256
                px[y * width + x] = (0xFF shl 24) or (r shl 16) or (g shl 8) or b
            }
        }
        return ArgbPixels(px, width, height)
    }

    /** 缩放后画布上 (v, u) 处应有的 (R, G, B)，与 Python 侧 `expect_rgb` 相同。 */
    private fun expectRgb(u: Int, v: Int) = Triple(
        (7 * u + 23 * v) % 256,
        (31 * u + 11 * v) % 256,
        (17 * u + 5 * v) % 256,
    )

    private fun mirror(i: Int, n: Int) = if (i < n) i else 2 * (n - 1) - i

    private fun at(p: PreparedFrame, c: Int, y: Int, x: Int) = p.nchw[c * PLANE + y * CANVAS + x]

    private fun sum(p: PreparedFrame): Long {
        var s = 0L
        for (v in p.nchw) s += v.toLong()
        return s
    }

    // ---- 形状与值域 ----

    @Test
    fun `形状是 3 乘 640 乘 640`() {
        val p = XFeatPreprocess.prepare(blockImage(720, 1280))
        assertEquals(3 * PLANE, p.nchw.size)
        assertEquals(360, p.validH)
        assertEquals(640, p.validW)
        assertTrue(p.sizeInput().contentEquals(longArrayOf(360, 640)))
    }

    @Test
    fun `值域是 0 到 255 而不是 0 到 1`() {
        // 契约第 5 条：**不除 255**。除了不会报错 —— InstanceNorm 抹掉全局尺度，
        // 模型在 0..1 上照样输出「像样」的描述子，只是与库里那批不在同一个空间。
        val p = XFeatPreprocess.prepare(blockImage(720, 1280))
        var max = 0f
        var min = Float.MAX_VALUE
        for (v in p.nchw) {
            if (v > max) max = v
            if (v < min) min = v
        }
        assertTrue("最大值只有 $max，像是被归一化过了", max > 1f)
        assertTrue(min >= 0f && max <= 255f)
    }

    @Test
    fun `整数取值 —— Python 侧是 uint8 resize 之后才转 float32`() {
        val p = XFeatPreprocess.prepare(blockImage(800, 1200))
        for (i in 0 until 5000) {
            val v = p.nchw[i]
            assertEquals("下标 $i 不是整数：$v", v, Math.round(v).toFloat())
        }
    }

    // ---- 通道顺序 ----

    @Test
    fun `第 0 个平面是 R`() {
        // 契约第 1 条。Python 侧从 cv2 拿到 BGR 要转一次；这边从 Bitmap 拿到的本来就是
        // RGB 序，所以**不转** —— 照抄那句 cvtColor 就是把它反过来。
        val p = XFeatPreprocess.prepare(blockImage(720, 1280))
        val (r, g, b) = expectRgb(23, 17)
        assertEquals(r.toFloat(), at(p, 0, 17, 23), 0f)
        assertEquals(g.toFloat(), at(p, 1, 17, 23), 0f)
        assertEquals(b.toFloat(), at(p, 2, 17, 23), 0f)
        assertNotEquals("R 和 B 相等的话这条测试对「反了」是瞎的", r, b)
    }

    // ---- 缩放尺寸 ----

    @Test
    fun `canvasSize 与 Python 侧逐个取值一致`() {
        // 这些数对在 tests/test_xfeat_prepare.py 的参数化表里也有一份。
        assertEquals(360 to 640, XFeatPreprocess.canvasSize(720, 1280))
        assertEquals(640 to 360, XFeatPreprocess.canvasSize(1280, 720))
        assertEquals(427 to 640, XFeatPreprocess.canvasSize(800, 1200))
        assertEquals(640 to 640, XFeatPreprocess.canvasSize(640, 640))
        assertEquals(512 to 640, XFeatPreprocess.canvasSize(400, 500))
        assertEquals(80 to 640, XFeatPreprocess.canvasSize(400, 3200))
    }

    @Test
    fun `长边永远不超过画布`() {
        // 浮点四舍五入可能把长边算成 641。夹取那一层不是防御性的：641 会直接越界。
        for (h in 1..40) {
            for (w in intArrayOf(1, 7, 639, 640, 641, 1279, 1280, 4000)) {
                val (nh, nw) = XFeatPreprocess.canvasSize(h, w)
                assertTrue("$h x $w -> $nh x $nw", nh in 1..CANVAS && nw in 1..CANVAS)
                assertTrue("长边必须是 640", maxOf(nh, nw) == CANVAS)
            }
        }
    }

    // ---- 补边 ----

    @Test
    fun `横图只补下方，最后一列仍是真实内容`() {
        // 契约第 3 条。补成四边居中的话这一列会变成镜像内容，关键点坐标也整体平移，
        // 而服务端那道坐标检查会因此把整批请求判成越界。
        val p = XFeatPreprocess.prepare(blockImage(720, 1280))
        for (x in intArrayOf(0, 313, 639)) {
            val (r, _, _) = expectRgb(x, 0)
            assertEquals("第 0 行 x=$x 不是真实内容", r.toFloat(), at(p, 0, 0, x), 0f)
        }
    }

    @Test
    fun `下方补边是镜像且不重复边界那一行`() {
        val p = XFeatPreprocess.prepare(blockImage(720, 1280))
        val nh = p.validH
        // REFLECT_101：紧贴边界的第一行补边等于**倒数第二**行。
        // 用 BORDER_REFLECT（会重复边界）的话它等于倒数第一行。
        for (x in intArrayOf(0, 77, 639)) {
            assertEquals(at(p, 0, nh - 2, x), at(p, 0, nh, x), 0f)
            assertNotEquals(at(p, 0, nh - 1, x), at(p, 0, nh, x))
        }
        for (y in intArrayOf(nh, nh + 137, CANVAS - 1)) {
            for (x in intArrayOf(3, 400, 639)) {
                assertEquals("y=$y", at(p, 1, mirror(y, nh), x), at(p, 1, y, x), 0f)
            }
        }
    }

    @Test
    fun `竖图只补右侧`() {
        val p = XFeatPreprocess.prepare(blockImage(1280, 720))
        val nw = p.validW
        assertEquals(360, nw)
        for (x in intArrayOf(nw, nw + 91, CANVAS - 1)) {
            for (y in intArrayOf(0, 200, 639)) {
                assertEquals("x=$x", at(p, 2, y, mirror(x, nw)), at(p, 2, y, x), 0f)
            }
        }
        // 下方一行都不补：最后一行必须是真实内容
        val (r, _, _) = expectRgb(12, CANVAS - 1)
        assertEquals(r.toFloat(), at(p, 0, CANVAS - 1, 12), 0f)
    }

    @Test
    fun `极端长宽比退回 REPLICATE，判据与 Python 侧相同`() {
        // `CANVAS - nh >= nh` 时镜像下标会算成负数。这条路真实存在：
        // 一张 3200×400 的全景缩下来就是 640×80，补 560 行 > 80。
        val p = XFeatPreprocess.prepare(blockImage(400, 3200))
        val nh = p.validH
        assertEquals(80, nh)
        assertTrue("判据真的成立，测试没跑偏", CANVAS - nh >= nh)
        for (y in intArrayOf(nh, nh + 200, CANVAS - 1)) {
            for (x in intArrayOf(0, 300, 639)) {
                assertEquals("y=$y", at(p, 0, nh - 1, x), at(p, 0, y, x), 0f)
            }
        }
    }

    @Test
    fun `正方形一点都不用补`() {
        val p = XFeatPreprocess.prepare(blockImage(640, 640))
        assertEquals(640, p.validH)
        assertEquals(640, p.validW)
        // 这张图不缩放（缩放比是 1），所以画布 (y, x) 就是源像素 (y, x)，
        // 而它属于第 (x/2, y/2) 个 2×2 块 —— 与 0.5 缩放那几条用的映射不同。
        val (r, _, _) = expectRgb(639 / 2, 639 / 2)
        assertEquals(r.toFloat(), at(p, 0, 639, 639), 0f)
    }

    @Test
    fun `永远只有一个方向要补边`() {
        // 长边**恒**被缩成正好 640（`canvasSize` 里那个 round 对最大那一边算的就是
        // round(640.0)），所以「右下角那个既补右又补下的矩形」在这条管线里不存在。
        // 这条测试把这个不变量钉住 —— 它是 `pad()` 里「先补右、再整行往下复制」这个
        // 顺序能成立的前提，也是补边区不可能出现未填 0 的原因。
        for ((h, w) in listOf(720 to 1280, 1280 to 720, 800 to 1200, 400 to 3200, 640 to 640)) {
            val p = XFeatPreprocess.prepare(blockImage(h, w))
            assertTrue(
                "${w}x$h 的有效区是 ${p.validW}x${p.validH}，两个方向都要补",
                p.validH == CANVAS || p.validW == CANVAS,
            )
            // 补边区里不该有任何未填的 0（真实内容里可能有 0，所以只数补边区）
            var zeros = 0
            for (y in p.validH until CANVAS) for (x in 0 until CANVAS) {
                if (at(p, 0, y, x) == 0f && at(p, 1, y, x) == 0f && at(p, 2, y, x) == 0f) zeros++
            }
            for (y in 0 until p.validH) for (x in p.validW until CANVAS) {
                if (at(p, 0, y, x) == 0f && at(p, 1, y, x) == 0f && at(p, 2, y, x) == 0f) zeros++
            }
            val padded = CANVAS * CANVAS - p.validH * p.validW
            assertTrue("${w}x$h 的补边区有 $zeros / $padded 个全零点", zeros * 4 < padded + 4)
        }
    }

    // ---- 放大 ----

    @Test
    fun `比画布小的图走放大而不是崩掉`() {
        // 产品里几乎走不到（相机帧和参考图长边都远大于 640），但「几乎」不是「不会」：
        // 一张 480×320 的老照片缩略图就会走到这里。
        val p = XFeatPreprocess.prepare(blockImage(320, 480))
        assertEquals(427 to 640, XFeatPreprocess.canvasSize(320, 480))
        assertEquals(427, p.validH)
        assertEquals(640, p.validW)
        for (v in p.nchw) assertTrue(v in 0f..255f)
    }

    // ---- 跨语言 golden ----

    @Test
    fun `golden 校验和与 Python 侧逐字相同`() {
        // 取和而不是逐值比：640×640×3 个数没法写进两份源码，而和对「通道顺序反了」
        // 「补边补在了上方」「值域除了 255」这三类错全都敏感。
        assertEquals(GOLDEN_LANDSCAPE_SUM, sum(XFeatPreprocess.prepare(blockImage(720, 1280))))
        assertEquals(GOLDEN_PORTRAIT_SUM, sum(XFeatPreprocess.prepare(blockImage(1280, 720))))
        assertNotEquals(
            "两个和相等的话说明实现对补边方向不加区分",
            GOLDEN_LANDSCAPE_SUM,
            GOLDEN_PORTRAIT_SUM,
        )
    }

    @Test
    fun `golden 定点与 Python 侧逐字相同`() {
        val land = XFeatPreprocess.prepare(blockImage(720, 1280))
        assertEquals(40f, at(land, 0, 17, 23), 0f) // R，真实内容
        assertEquals(206f, at(land, 1, 359, 639), 0f) // G，有效区最后一行最后一列
        assertEquals(250f, at(land, 2, 639, 639), 0f) // B，下方补边（镜像回第 79 行）
        assertEquals(202f, at(land, 0, 500, 300), 0f) // R，下方补边（镜像回第 218 行）

        val port = XFeatPreprocess.prepare(blockImage(1280, 720))
        assertEquals(254f, at(port, 1, 359, 639), 0f) // G，右侧补边（镜像回第 79 列）
        assertEquals(186f, at(port, 2, 639, 639), 0f) // B，右侧补边
        assertEquals(32f, at(port, 0, 500, 300), 0f) // R，真实内容（x=300 < nw=360）
    }

    // ---- 输入校验 ----

    @Test(expected = IllegalArgumentException::class)
    fun `像素数不够时立刻拒绝`() {
        ArgbPixels(IntArray(10), 100, 100)
    }

    @Test(expected = IllegalArgumentException::class)
    fun `零尺寸立刻拒绝`() {
        ArgbPixels(IntArray(0), 0, 0)
    }

    @Test
    fun `alpha 被忽略`() {
        // Bitmap.getPixels 给的是 ARGB_8888。alpha 混进任何一个通道都会静默改掉描述子。
        val opaque = blockImage(720, 1280)
        val transparent = ArgbPixels(
            IntArray(1280 * 720) { i -> opaque.argbAt(i % 1280, i / 1280) and 0x00FFFFFF },
            1280,
            720,
        )
        assertEquals(
            sum(XFeatPreprocess.prepare(opaque)),
            sum(XFeatPreprocess.prepare(transparent)),
        )
    }
}

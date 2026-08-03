package app.photoar.standalone

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 打印尺寸预设。
 *
 * 这些数字直接决定 ARCore 能不能一认出图案就贴上（物理尺寸未知时它要靠视差自己量，
 * 需要用户晃动手机 —— 那正是「认出来了但没在画面里找到」最常见的成因）。所以下面钉的是
 * **数字本身对不对**，以及横竖两个方向没有搞反。
 */
class PrintSizeTest {

    @Test
    fun `默认是不知道`() {
        // 用户不知道时让他猜一个不如让 ARCore 自己量。
        assertEquals(0.0, PrintSize.UNKNOWN.widthMm, 0.0)
        assertFalse(PrintSize.UNKNOWN.known)
        assertEquals("不知道要排第一个（它是默认值）", PrintSize.UNKNOWN, PrintSize.ORDER.first())
    }

    @Test
    fun `冲印尺寸的毫米数`() {
        // 6 寸 = 4R = 152×102mm，5 寸 = 3R = 127×89mm。填错的后果是 ARCore 按错的
        // 尺度算位姿 —— 而四边形大小取的是 extentX，所以错了也不会在画面上直接看出来，
        // 只会让检测更难收敛。
        assertEquals(152.0, PrintSize.SIX_INCH_LANDSCAPE.widthMm, 0.0)
        assertEquals(102.0, PrintSize.SIX_INCH_PORTRAIT.widthMm, 0.0)
        assertEquals(127.0, PrintSize.FIVE_INCH_LANDSCAPE.widthMm, 0.0)
        assertEquals(89.0, PrintSize.FIVE_INCH_PORTRAIT.widthMm, 0.0)
    }

    @Test
    fun `A4 的毫米数`() {
        assertEquals(297.0, PrintSize.A4_LANDSCAPE.widthMm, 0.0)
        assertEquals(210.0, PrintSize.A4_PORTRAIT.widthMm, 0.0)
    }

    @Test
    fun `横放一定比竖放宽`() {
        // 这一条防的是最容易犯的错：把两个方向的数字对调。对调之后 ARCore 会按一个
        // 差 50% 的尺度去算，而画面上看不出来（尺寸取 extentX），只会「贴不上」。
        val pairs = listOf(
            PrintSize.SIX_INCH_LANDSCAPE to PrintSize.SIX_INCH_PORTRAIT,
            PrintSize.FIVE_INCH_LANDSCAPE to PrintSize.FIVE_INCH_PORTRAIT,
            PrintSize.A4_LANDSCAPE to PrintSize.A4_PORTRAIT,
        )
        for ((landscape, portrait) in pairs) {
            assertTrue(
                "${landscape.label} 应该比 ${portrait.label} 宽",
                landscape.widthMm > portrait.widthMm,
            )
        }
    }

    @Test
    fun `每个预设的宽度都落在服务端接受的区间里`() {
        // 服务端是 20–2000 毫米（`batch.WIDTH_MIN_MM` / `WIDTH_MAX_MM`，与端上
        // Geometry 的 0.02–2.0 米对齐）。一个超出区间的预设会在入库时被拒，
        // 而用户只是点了一个我们自己给的按钮。
        for (s in PrintSize.entries.filter { it.known }) {
            assertTrue("${s.label} 太小：${s.widthMm}", s.widthMm >= 20.0)
            assertTrue("${s.label} 太大：${s.widthMm}", s.widthMm <= 2000.0)
        }
    }

    @Test
    fun `每个预设都有标签和提示`() {
        for (s in PrintSize.entries) {
            assertTrue("${s.name} 没有标签", s.label.isNotBlank())
            assertTrue("${s.name} 没有提示", s.hint.isNotBlank())
        }
    }

    @Test
    fun `不知道那一条的提示要说清代价`() {
        // 它不是「随便选选」——不填意味着扫的时候要多做一个动作。
        assertTrue(PrintSize.UNKNOWN.hint.contains("晃"))
    }

    @Test
    fun `按宽度反查预设`() {
        assertEquals(PrintSize.SIX_INCH_LANDSCAPE, PrintSize.match(152.0))
        // 冲印尺寸本来就有裁切公差，±3mm 内算同一种纸
        assertEquals(PrintSize.SIX_INCH_LANDSCAPE, PrintSize.match(150.0))
        assertEquals(PrintSize.A4_PORTRAIT, PrintSize.match(211.0))
    }

    @Test
    fun `反查超出容差时给 null_不四舍五入到最近的`() {
        // 把一张 A5（148mm 宽）说成 6 寸横（152）刚好在容差外；而说成 A4 会差 60mm。
        // 硬凑到最近的那个比说「自定义」糟得多 —— 那会让 ARCore 拿一个错的尺度去算。
        assertNull(PrintSize.match(60.0))
        assertNull(PrintSize.match(400.0))
        assertNull(PrintSize.match(0.0))
    }

    @Test
    fun `反查不会命中不知道那一条`() {
        // UNKNOWN 的 widthMm 是 0，如果不排除它，`match(0.0)` 会返回 UNKNOWN，
        // 而调用方拿它当「我认出这是哪种纸」用。
        assertNull(PrintSize.match(0.0))
    }
}

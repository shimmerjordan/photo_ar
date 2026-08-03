package app.photoar.standalone

import app.photoar.standalone.pixel.PixelBitmap
import app.photoar.standalone.pixel.PixelIcons
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 像素图的解析与那套原创图标。纯 Kotlin，不碰 Compose，所以是 JVM 单测。
 *
 * 这些用例守的都是**不报错的错**：位图多带 8 个缩进空格、某张图行数不对、两个页签
 * 用了同一张图 —— 界面照样画出来，只是尺寸不一样、或者两个按钮长得一样，而没有任何
 * 一步会失败。
 */
class PixelArtTest {

    // ---- 解析 ----

    @Test
    fun `脱掉源码里的共同缩进`() {
        // 这是第一版真实的 bug：`"""` 里的内容带着 8 个缩进空格，于是 16 格的图变成
        // 24 列，而 PixelIcon 按 min(宽/列, 高/行) 算格子 —— 格子小三分之一、图还偏右。
        // 一个字符都不报错。
        val b = PixelBitmap.of(
            """
            ##..
            ..##
            """
        )
        assertEquals(2, b.height)
        assertEquals(4, b.width)
        assertTrue(b.isOn(0, 0))
        assertFalse(b.isOn(0, 2))
        assertTrue(b.isOn(1, 2))
    }

    @Test
    fun `图内部的空格保住了`() {
        // trimIndent 只脱**共同**前缀。图里用来占位的空格是图的一部分 ——
        // 全 trim 掉的话每一行都会左对齐，图就散了。
        val b = PixelBitmap.of(
            """
            .#..
            ..#.
            """
        )
        assertFalse(b.isOn(0, 0))
        assertTrue(b.isOn(0, 1))
        assertTrue(b.isOn(1, 2))
    }

    @Test
    fun `1 和井号都算亮`() {
        val b = PixelBitmap.of("1#.")
        assertTrue(b.isOn(0, 0))
        assertTrue(b.isOn(0, 1))
        assertFalse(b.isOn(0, 2))
    }

    @Test
    fun `越界当灭，不抛`() {
        // 短行按左对齐补空（写图时不必给每行数空格），所以越界必须是"灭"而不是崩。
        val b = PixelBitmap.of("##")
        assertFalse(b.isOn(0, 99))
        assertFalse(b.isOn(99, 0))
        assertFalse(b.isOn(-1, 0))
    }

    @Test
    fun `空图不崩`() {
        val b = PixelBitmap.of("")
        assertEquals(0, b.height)
        assertEquals(0, b.width)
        assertEquals(0, b.litCount())
    }

    @Test
    fun `亮格子计数`() {
        assertEquals(3, PixelBitmap.of("##.\n.#.").litCount())
    }

    // ---- 那套图标 ----

    @Test
    fun `每张图标都是 16 乘 16`() {
        // 行数不同的图标在同一个 24dp 框里格子大小就不同，摆在一排会有的粗有的细。
        for ((name, b) in PixelIcons.all) {
            assertEquals("$name 的行数", PixelIcons.GRID, b.height)
            assertEquals("$name 的列数", PixelIcons.GRID, b.width)
        }
    }

    @Test
    fun `每张图标都有足够的内容`() {
        // 下限 20 格：比这更少的图在 24dp 上是几个孤立的点，认不出是什么。
        // 上限 160 格（占 62%）：再满就是一个实心块，形状读不出来。
        for ((name, b) in PixelIcons.all) {
            val lit = b.litCount()
            assertTrue("$name 只有 $lit 格，太空", lit >= 20)
            assertTrue("$name 有 $lit 格，太满", lit <= 160)
        }
    }

    @Test
    fun `没有两张图标是一样的`() {
        // 改造前底栏的「扫一扫」和「照片」用的是**同一个** Icons.Filled.Home ——
        // 两个页签长得一样，只能靠文字区分。那种重复不会报错，所以钉一条。
        val seen = mutableMapOf<List<String>, String>()
        for ((name, b) in PixelIcons.all) {
            val prev = seen.put(b.rows, name)
            assertTrue("$name 和 $prev 是同一张图", prev == null)
        }
    }

    @Test
    fun `设计成左右对称的那几张必须真的对称`() {
        // 像素画里差一列的不对称是最典型的手抖：看着"有点歪"但说不出哪里，
        // 而在 24dp 上恰好是肉眼刚好能察觉的量级。这几张的构图本来就是对称的
        // （房子、上传、齿轮、加号），所以可以机械地验。
        //
        // 「照片」「返回」「管理」「扫一扫」**故意**不对称：叠放的相框、指向左边的
        // 箭头、错开的滑块、四角取景框，对称了反而不对。
        for (name in listOf("Home", "Upload", "Settings", "Add")) {
            val b = PixelIcons.all.getValue(name)
            for (r in 0 until b.height) {
                for (c in 0 until b.width / 2) {
                    val mirror = b.width - 1 - c
                    assertEquals(
                        "$name 第 $r 行的第 $c 列与第 $mirror 列不对称",
                        b.isOn(r, c),
                        b.isOn(r, mirror),
                    )
                }
            }
        }
    }
}

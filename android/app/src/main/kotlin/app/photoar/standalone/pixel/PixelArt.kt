package app.photoar.standalone.pixel

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.layout.size
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import kotlin.math.floor

/**
 * 像素画的最小件：一张**用字符串写的位图**，画成一格一格的实心方块。
 *
 * ## 为什么是字符串而不是图片资源
 *
 * 三个理由，第一个是决定性的：
 *
 * 1. **像素画不能被缩放。** PNG 放大会插值成糊的，SVG 缩放会在格子边缘留半透明的
 *    抗锯齿。这里每一格都是按当前尺寸**现算**的整数像素矩形，所以 16dp 和 200dp 下
 *    都是硬边 —— 而这正是像素风唯一不能妥协的地方。
 * 2. 图在代码里看得见。`"..####.."` 这一行就是那一行像素，改图不用开编辑器、
 *    diff 里看得出改了什么。
 * 3. 不进 res/，于是同一份图既能画在 Compose 里，也能被脚本转成 launcher 的
 *    vector drawable（`tools/gen_launcher_icon.py`）—— 应用图标和界面里的图标
 *    是同一份源，不会哪天只改了一边。
 *
 * ## 对齐到整数像素是这个文件的全部难点
 *
 * 直接 `drawRect(x = col * cell)` 会在 cell 不是整数时让相邻两格之间出现半像素缝
 * （背景透过来，看起来像描边坏了）。做法是**每一格的边界各自向下取整**，然后
 * 用「下一格的左边界」当这一格的右边界 —— 相邻格于是共享同一条边，永远不留缝，
 * 代价是某些格子比邻居宽一个物理像素（肉眼看不出，而缝看得出来）。
 */
@JvmInline
value class PixelBitmap(val rows: List<String>) {

    val height: Int get() = rows.size

    /** 最长那一行的长度。短行按左对齐补空 —— 写图时不必给每行数空格。 */
    val width: Int get() = rows.maxOfOrNull { it.length } ?: 0

    fun isOn(row: Int, col: Int): Boolean {
        val line = rows.getOrNull(row) ?: return false
        val ch = line.getOrNull(col) ?: return false
        return ch == ON || ch == '1'
    }

    /** 亮着的格子数。给测试和「这张图是不是空的」用。 */
    fun litCount(): Int {
        var n = 0
        for (r in 0 until height) for (c in 0 until width) if (isOn(r, c)) n++
        return n
    }

    companion object {
        const val ON = '#'

        /**
         * 从多行字符串建一张图。
         *
         * **`trimIndent()` 是必须的，不是顺手。** 源码里的图是缩进 8 个空格写的
         * （`"""` 里的内容包含缩进），不脱掉的话每一行都变成 8 空格 + 16 格 = 24 列，
         * 而 [PixelIcon] 按 `min(宽/列, 高/行)` 算格子 —— 于是格子按 24 列算、比该有的
         * 小三分之一，图还被推到右边去。而且**它不报错**：图照样画出来，只是小一圈、
         * 偏一点，摆在一排时"有的图标看起来不一样大"。
         *
         * `trimIndent` 只脱掉**共同**前缀，所以图内部用来占位的空格一个不少 ——
         * 那些正是图的一部分。首尾空行一起去掉，这样能在三引号后面直接换行写第一排。
         */
        fun of(art: String): PixelBitmap =
            PixelBitmap(
                art.trimIndent().lines().dropWhile { it.isBlank() }.dropLastWhile { it.isBlank() }
            )
    }
}

/**
 * 把一张 [PixelBitmap] 画成 [size] 见方的图标。
 *
 * @param tint 亮格子的颜色。像素图标是单色的 —— 多色会让它在深浅底上都不好看，
 *   而这个 App 的界面固定深色、扫描界面是相机画面（明暗不定）。
 */
@Composable
fun PixelIcon(
    bitmap: PixelBitmap,
    size: Dp,
    tint: Color,
    modifier: Modifier = Modifier,
) {
    // remember 住格子坐标：这些图标出现在底栏里，每次重组都重算一遍纯浪费。
    val rows = remember(bitmap) { bitmap.height }
    val cols = remember(bitmap) { bitmap.width }
    Canvas(modifier.size(size)) {
        if (rows == 0 || cols == 0) return@Canvas
        // 以短边为基准，长边居中：图标位图通常是方的，但不强求。
        val cell = minOf(this.size.width / cols, this.size.height / rows)
        val originX = (this.size.width - cell * cols) / 2f
        val originY = (this.size.height - cell * rows) / 2f
        for (r in 0 until rows) {
            val top = originY + floor(r * cell)
            val bottom = originY + floor((r + 1) * cell)
            for (c in 0 until cols) {
                if (!bitmap.isOn(r, c)) continue
                val left = originX + floor(c * cell)
                val right = originX + floor((c + 1) * cell)
                drawRect(
                    color = tint,
                    topLeft = Offset(left, top),
                    size = Size(right - left, bottom - top),
                )
            }
        }
    }
}

/** 底栏、顶栏那一档的图标尺寸。24dp 是 Material 的图标尺寸，跟着它走。 */
val PixelIconSize: Dp = 24.dp

/** 首页那颗大按钮里的图标。 */
val PixelIconSizeLarge: Dp = 64.dp

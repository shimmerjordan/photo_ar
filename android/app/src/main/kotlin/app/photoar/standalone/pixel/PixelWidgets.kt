package app.photoar.standalone.pixel

import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.unit.dp

/**
 * 像素风的斜面：上/左一条亮边、下/右一条暗边 = 凸起；反过来 = 按下。
 *
 * ## 为什么用它替掉阴影
 *
 * Material 靠 tonal elevation + 阴影表达层级，而阴影是**模糊**的 —— 像素画里不存在
 * 模糊。斜面是同一件事在像素画里的表达方式（红白机时代的按钮全是这么画的），
 * 而且它比阴影更明确：亮暗两条边的方向直接说明"这一格是凸的还是凹的"。
 *
 * `pressed` 取反而不是只改颜色：**按下时形状要变**。只变颜色的话，在像素风的直角
 * 界面上按下去几乎看不出来（没有圆角的形变、没有阴影的收缩可借），而"点了没反应"
 * 是最容易被当成卡顿的一类问题。
 *
 * 画在 [drawBehind] 里而不是用四个 `border`：四条边要**不同颜色**，而 `Modifier.border`
 * 只能一次给一种，分四次叠加会在角上重叠成第五种颜色。
 */
fun Modifier.pixelBevel(
    pressed: Boolean = false,
    light: Color = PixelPalette.BevelLight,
    dark: Color = PixelPalette.BevelDark,
): Modifier = drawBehind {
    val t = PixelEdge.toPx()
    val topLeft = if (pressed) dark else light
    val bottomRight = if (pressed) light else dark
    drawRect(topLeft, Offset.Zero, Size(size.width, t))
    drawRect(topLeft, Offset.Zero, Size(t, size.height))
    drawRect(bottomRight, Offset(0f, size.height - t), Size(size.width, t))
    drawRect(bottomRight, Offset(size.width - t, 0f), Size(t, size.height))
}

/**
 * 一块面板：面板底色 + 2dp 硬边框，没有圆角也没有阴影。
 *
 * 代替 Material 的 `Card`（那个自带圆角与 tonal elevation）。用在"这一组东西属于
 * 一起"的场合 —— 而不是每一行都套一个，那是把卡片当默认布局用。
 */
@Composable
fun PixelPanel(
    modifier: Modifier = Modifier,
    highlighted: Boolean = false,
    content: @Composable ColumnScope.() -> Unit,
) {
    Column(
        modifier
            .fillMaxWidth()
            .background(if (highlighted) PixelPalette.PanelHi else PixelPalette.Panel)
            .border(PixelEdge, if (highlighted) PixelPalette.Amber else PixelPalette.Edge)
            .padding(PixelSpace.x4),
        content = content,
    )
}

/**
 * 像素风的按钮：实心底 + 斜面，按下时斜面翻转、内容跟着挪 2dp。
 *
 * 不套 Material 的 `Button`，因为那个自带 ripple —— ripple 是一圈扩散的**圆**，
 * 在直角像素界面上是最突兀的一处。这里把 indication 关掉，改用斜面翻转给反馈。
 *
 * **可访问性一样不少**：`role = Role.Button` 让 TalkBack 照旧报"按钮"，`enabled`
 * 参与点击语义，最小高度 48dp 是 Material 的触摸目标下限。
 */
@Composable
fun PixelButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    primary: Boolean = true,
    content: @Composable () -> Unit,
) {
    val interaction = remember { MutableInteractionSource() }
    val pressed by interaction.collectIsPressedAsState()
    val down = pressed && enabled
    val bg = when {
        !enabled -> PixelPalette.Panel
        primary -> PixelPalette.Amber
        else -> PixelPalette.PanelHi
    }
    Box(
        modifier
            .defaultMinSize(minHeight = 48.dp)
            .background(bg)
            .pixelBevel(pressed = down)
            .clickable(
                interactionSource = interaction,
                indication = null,
                enabled = enabled,
                role = Role.Button,
                onClick = onClick,
            )
            .padding(
                start = PixelSpace.x4 + if (down) PixelEdge else 0.dp,
                top = PixelSpace.x2 + if (down) PixelEdge else 0.dp,
                end = PixelSpace.x4,
                bottom = PixelSpace.x2,
            ),
        contentAlignment = Alignment.Center,
    ) {
        content()
    }
}

/**
 * 一行「图标 + 文字」。
 *
 * 图标与文字之间是 [PixelSpace.x2]（8dp）而不是随手一个 6dp：整套界面对齐到 4dp
 * 网格，理由见 [PixelSpace]。
 */
@Composable
fun PixelLabel(
    bitmap: PixelBitmap,
    text: String,
    tint: Color = MaterialTheme.colorScheme.onSurface,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier,
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.Start,
    ) {
        PixelIcon(bitmap, PixelIconSize, tint)
        Box(Modifier.size(PixelSpace.x2))
        Text(text, style = MaterialTheme.typography.labelLarge, color = tint)
    }
}

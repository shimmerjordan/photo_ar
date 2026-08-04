package app.photoar.standalone.pixel

import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.RowScope
import androidx.compose.material3.ButtonColors
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.ProgressIndicatorDefaults
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp

/**
 * Material 组件的直角包装。**同名**，所以调用点一个字都不用改，只换 import。
 *
 * ## 为什么必须有这个文件（换 `Shapes` 是不够的）
 *
 * [PhotoArPixelTheme] 把 `Shapes` 五个槽全设成 0dp，于是输入框、卡片、对话框都变成了
 * 直角 —— 但**按钮没有**。Material 3 的 `Button` 形状来自 `ButtonDefaults.shape`，
 * 它解析的是 shape token `CornerFull`，而 `CornerFull` 在 Material 内部是**硬编码**成
 * `CircleShape` 的，不读 `MaterialTheme.shapes`。也就是说主题里没有任何旋钮能把按钮
 * 变方。
 *
 * 这件事在代码里完全看不出来（`Shapes(...)` 五个槽都填了，读起来像是全覆盖了），
 * 只有装到真机上看一眼才发现按钮还是药丸 —— 而药丸是像素风里最不该出现的形状。
 *
 * ## 为什么是同名包装，而不是逐点传 shape
 *
 * 76 个调用点。逐点插 `shape = RectangleShape` 一是churn 太大，二是有真实的编译风险：
 * `onClick` 在有些地方是位置参数，具名参数插到位置参数前面在 Kotlin 里是错误。
 * 同名包装只改 20 行 import，且以后新写的界面**自动**是直角的（用错的那个会因为
 * import 不同而一眼看出来）。
 *
 * 参数表是按实际调用点裁的（普查过：只用到 onClick / modifier / enabled / shape /
 * colors）。故意**不**把 Material 的全部参数抄一遍 —— 抄一遍就得跟着 Material 版本
 * 升级维护默认值，而漏掉一个默认值的表现是"某个按钮的高度/边框和别处不一样"。
 * 真需要某个参数时加一行，编译器会告诉你。
 *
 * ## 没包进来的
 *
 * - `Switch`：Material 3 的开关是轨道 + 圆形滑块，形状不可配。做成方的要自己重画一个
 *   （连带丢掉它的拖拽手势和无障碍语义），不值得。
 * - `Card` / `AlertDialog` / `OutlinedTextField` / `FilterChip`：它们读的是
 *   `MaterialTheme.shapes`，已经被主题变成直角了，不需要包装。
 */
@Composable
fun Button(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    shape: Shape = RectangleShape,
    colors: ButtonColors = ButtonDefaults.buttonColors(),
    contentPadding: PaddingValues = ButtonDefaults.ContentPadding,
    content: @Composable RowScope.() -> Unit,
) = androidx.compose.material3.Button(
    onClick = onClick,
    modifier = modifier,
    enabled = enabled,
    shape = shape,
    colors = colors,
    contentPadding = contentPadding,
    content = content,
)

@Composable
fun OutlinedButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    shape: Shape = RectangleShape,
    colors: ButtonColors = ButtonDefaults.outlinedButtonColors(),
    contentPadding: PaddingValues = ButtonDefaults.ContentPadding,
    content: @Composable RowScope.() -> Unit,
) = androidx.compose.material3.OutlinedButton(
    onClick = onClick,
    modifier = modifier,
    enabled = enabled,
    shape = shape,
    colors = colors,
    contentPadding = contentPadding,
    content = content,
)

@Composable
fun TextButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    shape: Shape = RectangleShape,
    colors: ButtonColors = ButtonDefaults.textButtonColors(),
    contentPadding: PaddingValues = ButtonDefaults.TextButtonContentPadding,
    content: @Composable RowScope.() -> Unit,
) = androidx.compose.material3.TextButton(
    onClick = onClick,
    modifier = modifier,
    enabled = enabled,
    shape = shape,
    colors = colors,
    contentPadding = contentPadding,
    content = content,
)

/**
 * 进度条的直角版本。
 *
 * Material 3 从 1.2 起给进度条两端加了圆角（`strokeCap = Round`）并在已完成/未完成
 * 之间留一道 `gapSize` 的圆头空隙。那是三处圆弧，在一屏都是直角的界面里很刺眼，
 * 而进度条恰好出现在上传这条最常走的路上。
 *
 * `gapSize = 0.dp` 一并去掉：留着的话空隙两侧仍是圆头，只把长度改成 0 是去不掉的。
 */
@Composable
fun LinearProgressIndicator(
    progress: () -> Float,
    modifier: Modifier = Modifier,
    color: Color = ProgressIndicatorDefaults.linearColor,
    trackColor: Color = ProgressIndicatorDefaults.linearTrackColor,
    strokeCap: StrokeCap = StrokeCap.Butt,
    gapSize: Dp = 0.dp,
) = androidx.compose.material3.LinearProgressIndicator(
    progress = progress,
    modifier = modifier,
    color = color,
    trackColor = trackColor,
    strokeCap = strokeCap,
    gapSize = gapSize,
)

/** 不确定进度（转来转去那种）的重载。调用点里有一处不给 `progress`。 */
@Composable
fun LinearProgressIndicator(
    modifier: Modifier = Modifier,
    color: Color = ProgressIndicatorDefaults.linearColor,
    trackColor: Color = ProgressIndicatorDefaults.linearTrackColor,
    strokeCap: StrokeCap = StrokeCap.Butt,
) = androidx.compose.material3.LinearProgressIndicator(
    modifier = modifier,
    color = color,
    trackColor = trackColor,
    strokeCap = strokeCap,
)

package app.photoar.standalone.pixel

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Shapes
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em

/**
 * 像素风的主题。**Material 3 的结构一点不动，只换表达方式。**
 *
 * ## 这条界线是刻意的
 *
 * 像素风是「长什么样」，不是「怎么用」。所以换掉的只有三样：形状（圆角 → 直角）、
 * 字族（无衬线 → 等宽）、配色（一组有限的饱和色）。而 Material 的**色角色**
 * （primary / surface / onSurface…）、**排版角色**（Display/Title/Body/Label）、
 * **组件**（NavigationBar / Card / Button / Snackbar）、48dp 触摸目标、系统返回手势
 * 一样都不碰。
 *
 * 反过来做（自己画一套控件）会失掉三件真东西：无障碍（TalkBack 认 Material 的语义）、
 * 系统设置（字号缩放、去动画）、以及「安卓用户一眼知道怎么用」。
 * 像素风的代价不该由这些来付。
 *
 * ## 为什么直角是这里最有价值的一行
 *
 * `Shapes` 里全部换成 0dp 圆角之后，**每一个** Card / Button / TextField /
 * Chip / Dialog / Snackbar 都跟着变成直角 —— 包括我没有逐个改过的那些屏。像素画里
 * 没有抗锯齿的曲线，圆角是这套风格里最刺眼的那一处不一致，而一行就能全清掉。
 *
 * ## 配色：保留原来那个琥珀色，不重新发明
 *
 * 主色 [Amber] 就是改造前的 `primary`（#FFC46B）。这不是省事：那是这个 App 已经
 * 建立起来的识别色，而「换风格」不等于「换品牌」。像素风该表达在形状、图标、
 * 边框上，不该表达在「顺手换个更像素的绿」上 —— 而那个绿（Game Boy 的豆绿 /
 * 终端绿）恰好是所有像素风 UI 的第一反射，撞上去反而更没有辨识度。
 *
 * 底色比原来更暗（#0B0C10 而不是 #121316）：这套风格靠 2dp 的硬边框而不是阴影来
 * 分层，而边框要读得出来，底色和面板色之间就得有足够的落差。
 *
 * 其余四个是**有限调色板**的其余部分，每个都有唯一职责，不当装饰用：
 * 洋红报错要人看、青色表示"选中/信息"、绿色表示成功、红色是硬失败。
 * 这正是像素画的做法 —— 一套 tile 只有几种颜色，每种颜色都有含义。
 */
object PixelPalette {
    /** 底：比原来更暗，好让 2dp 硬边框读得出来。 */
    val Ground = Color(0xFF0B0C10)

    /** 面板：卡片、底栏、输入框的底。和 [Ground] 差一档，靠边框而不是阴影分层。 */
    val Panel = Color(0xFF171A21)

    /** 更高一层的面板（选中项、按下的格子）。 */
    val PanelHi = Color(0xFF232833)

    val Ink = Color(0xFFE8EAF0)
    val Dim = Color(0xFF9AA1B2)

    /** 主色。改造前就是它 —— 见类注释里那段「不重新发明」。 */
    val Amber = Color(0xFFFFC46B)

    /** 主色的暗侧。按下时的斜面、以及 primaryContainer。 */
    val AmberDeep = Color(0xFF6B4A12)

    /** 在主色上的字。深棕而不是纯黑：纯黑配琥珀在小字号上会发灰。 */
    val OnAmber = Color(0xFF2A1A00)

    val Magenta = Color(0xFFFF6B9D)
    val Cyan = Color(0xFF6BD5FF)
    val Green = Color(0xFF7CE38B)
    val Red = Color(0xFFFF5C5C)

    /** 斜面的亮边（上、左）。半透明白，所以在任何面板色上都成立。 */
    val BevelLight = Color(0x40FFFFFF)

    /** 斜面的暗边（下、右）。 */
    val BevelDark = Color(0x66000000)

    /** 边框。像素风里的分层靠它，不靠阴影。 */
    val Edge = Color(0xFF3A4150)
}

/**
 * 斜面 / 边框的粗细。
 *
 * 2dp 而不是 1dp：1dp 在 3x 屏上是 3 物理像素，看着像"细描边"而不是"这一格是凸起的"。
 * 而斜面要读成凸起，亮暗两条边都得有厚度。也不能更粗 —— 4dp 在 48dp 高的按钮上
 * 会吃掉六分之一。
 */
val PixelEdge = 2.dp

/**
 * 间距。**4dp 的整数倍，一格也不许出现 5dp / 6dp / 10dp。**
 *
 * 像素画的一切都对齐到网格，界面也该这样。这不只是洁癖：混着用 6dp 和 8dp 的界面
 * 在像素风里会显出来（直角把对不齐的边暴露得很清楚，圆角会藏住），
 * 而"看起来差一点点但说不出哪里"是最难修的一类问题。
 */
object PixelSpace {
    val x1 = 4.dp
    val x2 = 8.dp
    val x3 = 12.dp
    val x4 = 16.dp
    val x6 = 24.dp
    val x8 = 32.dp
    val x12 = 48.dp
}

private val Scheme = darkColorScheme(
    primary = PixelPalette.Amber,
    onPrimary = PixelPalette.OnAmber,
    primaryContainer = PixelPalette.AmberDeep,
    onPrimaryContainer = PixelPalette.Amber,
    secondary = PixelPalette.Cyan,
    onSecondary = PixelPalette.OnAmber,
    secondaryContainer = PixelPalette.PanelHi,
    onSecondaryContainer = PixelPalette.Ink,
    tertiary = PixelPalette.Magenta,
    background = PixelPalette.Ground,
    onBackground = PixelPalette.Ink,
    surface = PixelPalette.Ground,
    onSurface = PixelPalette.Ink,
    surfaceVariant = PixelPalette.Panel,
    onSurfaceVariant = PixelPalette.Dim,
    surfaceContainer = PixelPalette.Panel,
    surfaceContainerHigh = PixelPalette.PanelHi,
    outline = PixelPalette.Edge,
    outlineVariant = PixelPalette.Edge,
    error = PixelPalette.Red,
    onError = PixelPalette.OnAmber,
)

/**
 * 全直角。**这一行改的是整个 App。**
 *
 * 用 `RoundedCornerShape(0.dp)` 而不是 `RectangleShape`：`Shapes` 的字段类型是
 * `CornerBasedShape`（组件内部要按角半径做插值），而 `RectangleShape` 只是 `Shape`，
 * 给不进去。0dp 的圆角矩形在渲染上与直角完全相同。
 *
 * Material 的每个组件都从这里取形状，所以我没有逐个改过的那些屏也跟着变直角了。
 * 详见类注释里那一段。
 */
private val Square = RoundedCornerShape(0.dp)

private val PixelShapes = Shapes(
    extraSmall = Square,
    small = Square,
    medium = Square,
    large = Square,
    extraLarge = Square,
)

/**
 * 等宽字族铺满整个排版表。
 *
 * 为什么不塞一个真正的点阵字体文件：一是许可（能用的点阵中文字体几乎没有），二是
 * **中文**。这个 App 的界面全是中文，而点阵字体的中文覆盖要么没有、要么在 12sp 下
 * 糊成一团 —— 那是拿可读性换风格，方向错了。系统等宽字体在字形上已经足够"方"，
 * 配上直角边框和像素图标，风格是成立的。
 *
 * 字号一个都不改，仍然是 Material 的那一套 sp —— 跟随系统字号缩放这件事不能丢。
 * 只加了一点正字距：等宽字挤在一起时中文的可读性会掉。
 */
private fun pixelTypography(): Typography {
    val base = Typography()
    fun TextStyle.pixel(weight: FontWeight? = null) = copy(
        fontFamily = FontFamily.Monospace,
        fontWeight = weight ?: fontWeight,
        letterSpacing = 0.02.em,
    )
    return Typography(
        displayLarge = base.displayLarge.pixel(FontWeight.Bold),
        displayMedium = base.displayMedium.pixel(FontWeight.Bold),
        displaySmall = base.displaySmall.pixel(FontWeight.Bold),
        headlineLarge = base.headlineLarge.pixel(FontWeight.Bold),
        headlineMedium = base.headlineMedium.pixel(FontWeight.Bold),
        headlineSmall = base.headlineSmall.pixel(FontWeight.Bold),
        titleLarge = base.titleLarge.pixel(FontWeight.Bold),
        titleMedium = base.titleMedium.pixel(FontWeight.Bold),
        titleSmall = base.titleSmall.pixel(FontWeight.Bold),
        bodyLarge = base.bodyLarge.pixel(),
        bodyMedium = base.bodyMedium.pixel(),
        bodySmall = base.bodySmall.pixel(),
        labelLarge = base.labelLarge.pixel(FontWeight.Bold),
        labelMedium = base.labelMedium.pixel(FontWeight.Bold),
        labelSmall = base.labelSmall.pixel(),
    )
}

/** 深色固定：扫描界面是全屏相机（黑底），外壳跟着深色才不会在两者之间闪白。 */
@Composable
fun PhotoArPixelTheme(content: @Composable () -> Unit) {
    MaterialTheme(
        colorScheme = Scheme,
        shapes = PixelShapes,
        typography = pixelTypography(),
        content = content,
    )
}

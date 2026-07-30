package app.photoar.standalone

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import app.photoar.arview.EndpointCenter
import app.photoar.arview.Resolution

/**
 * 外壳的唯一 Activity。
 *
 * §5.8 原本写的是 Flutter 外壳，这里改成 Kotlin + Compose，理由见
 * `docs/superpowers/plans/2026-07-30-phase3-shell.md`：ARCore 只有 Android，
 * Flutter 的跨平台收益是零，代价却是把 §7 契约在 Dart 里再实现一遍（Kotlin 侧
 * 已经有 243 个单测盯着它），而 §5.7 的 EndpointResolver 本来就在 Android 侧。
 *
 * 界面栈自己维护（[Shell]），不引 navigation-compose：一共六页、没有深链、没有
 * 跨页参数序列化的需求，一个 `mutableStateListOf<Route>` 比一套路由 DSL 更好读。
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // §9.2 的第二个触发时机。注册一次跟着进程活着。
        EndpointCenter.get(this).watchNetwork()
        setContent { PhotoArTheme { AppRoot() } }
    }
}

private val Scheme = darkColorScheme(
    primary = Color(0xFFFFC46B),
    onPrimary = Color(0xFF3A2600),
    primaryContainer = Color(0xFF54400F),
    onPrimaryContainer = Color(0xFFFFE0B2),
    secondary = Color(0xFFB8C7D9),
    background = Color(0xFF121316),
    onBackground = Color(0xFFE6E6E9),
    surface = Color(0xFF121316),
    onSurface = Color(0xFFE6E6E9),
    surfaceVariant = Color(0xFF2A2C31),
    onSurfaceVariant = Color(0xFFB9BCC4),
)

/** 深色固定：扫描界面是全屏相机（黑底），外壳跟着深色才不会在两者之间闪白。 */
@Composable
fun PhotoArTheme(content: @Composable () -> Unit) {
    MaterialTheme(colorScheme = Scheme, content = content)
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppRoot() {
    val context = LocalContext.current
    val shell = remember { Shell(context) }
    val resolution = rememberResolution(shell.center)

    // §9.2 的第一个触发时机。没配过就直接落在设置页 —— 空列表加一个「连不上」
    // 比「去填地址」难懂得多。
    LaunchedEffect(Unit) {
        if (shell.center.configured) shell.center.refreshAsync() else shell.tab(Route.Settings)
    }

    BackHandler(enabled = shell.current != shell.currentRoot) { shell.pop() }

    val route = shell.current
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(titleOf(route)) },
                navigationIcon = {
                    if (!route.root) {
                        IconButton(onClick = { shell.pop() }) {
                            @Suppress("DEPRECATION")
                            Icon(Icons.Filled.ArrowBack, contentDescription = "返回")
                        }
                    }
                },
                actions = {
                    if (route == Route.Photos) {
                        IconButton(onClick = { shell.push(Route.Browse(Pick.IMAGE, null)) }) {
                            Icon(Icons.Filled.Add, contentDescription = "关联新照片")
                        }
                    }
                    ViaChip(resolution) { shell.center.refreshAsync(force = true) }
                },
            )
        },
        bottomBar = {
            if (route.root) {
                NavigationBar {
                    NavigationBarItem(
                        selected = route == Route.Photos,
                        onClick = { shell.tab(Route.Photos) },
                        icon = { Icon(Icons.Filled.Home, null) },
                        label = { Text("照片") },
                    )
                    NavigationBarItem(
                        selected = route == Route.History,
                        onClick = { shell.tab(Route.History) },
                        icon = {
                            @Suppress("DEPRECATION")
                            Icon(Icons.Filled.List, null)
                        },
                        label = { Text("历史") },
                    )
                    NavigationBarItem(
                        selected = route == Route.Settings,
                        onClick = { shell.tab(Route.Settings) },
                        icon = { Icon(Icons.Filled.Settings, null) },
                        label = { Text("设置") },
                    )
                }
            }
        },
    ) { pad ->
        Box(Modifier.padding(pad)) {
            when (route) {
                Route.Photos -> PhotosScreen(shell)
                Route.History -> HistoryScreen(shell)
                Route.Settings -> SettingsScreen(shell, resolution)
                is Route.Detail -> PhotoDetailScreen(shell, route.photoId)
                is Route.Browse -> BrowseScreen(shell, route)
                Route.Create -> CreateScreen(shell)
            }
        }
    }
}

private fun titleOf(route: Route): String = when (route) {
    Route.Photos -> "照片库"
    Route.History -> "识别历史"
    Route.Settings -> "设置"
    is Route.Detail -> "照片详情"
    is Route.Browse -> when (route.pick) {
        Pick.IMAGE -> "挑一张照片 · " + Fmt.dirTitle(route.dir)
        else -> "挑一段视频 · " + Fmt.dirTitle(route.dir)
    }
    Route.Create -> "关联新照片"
}

/**
 * 顶栏右上角那颗状态点：当前 api 走的哪条通道、多少毫秒。点一下强制重探。
 *
 * 这不是装饰。在外面扫不出来时，第一个要回答的问题就是「现在走的是哪条」——
 * 没有它就只能去设置页翻探活结果。
 */
@Composable
private fun ViaChip(resolution: Resolution?, onClick: () -> Unit) {
    val (text, color) = when {
        resolution == null -> "探活中…" to MaterialTheme.colorScheme.onSurfaceVariant
        resolution.offline -> "离线" to MaterialTheme.colorScheme.error
        else -> {
            val api = resolution.api!!
            "${api.candidate.name} ${api.latencyMs}ms" to MaterialTheme.colorScheme.primary
        }
    }
    Box(
        Modifier
            .padding(end = 8.dp)
            .clip(RoundedCornerShape(10.dp))
            .background(MaterialTheme.colorScheme.surfaceVariant)
            .clickable(onClick = onClick)
            .padding(horizontal = 10.dp, vertical = 5.dp),
    ) {
        Text(text = text, style = MaterialTheme.typography.labelSmall, color = color)
    }
}

/** 订阅探活结果。回调是主线程来的（[EndpointCenter] 里 post 过），可以直接写 state。 */
@Composable
private fun rememberResolution(center: EndpointCenter): Resolution? {
    var r by remember { mutableStateOf(center.resolution) }
    DisposableEffect(center) {
        val listener: (Resolution) -> Unit = { r = it }
        center.addListener(listener)
        onDispose { center.removeListener(listener) }
    }
    return r
}

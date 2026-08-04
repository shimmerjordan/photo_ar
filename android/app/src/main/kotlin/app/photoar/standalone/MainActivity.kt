package app.photoar.standalone

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Home
import androidx.compose.material.icons.filled.List
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FabPosition
import androidx.compose.material3.FloatingActionButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationBarItemDefaults
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
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.foundation.layout.Column
import androidx.compose.ui.Alignment
import app.photoar.arview.Resolution
import app.photoar.standalone.pixel.PhotoArPixelTheme
import app.photoar.standalone.pixel.PixelBitmap
import app.photoar.standalone.pixel.PixelButton
import app.photoar.standalone.pixel.PixelIcon
import app.photoar.standalone.pixel.PixelIconSize
import app.photoar.standalone.pixel.PixelIcons
import app.photoar.arview.ar.ArCoreEmbeddedRuntime
import app.photoar.arview.ui.ArScanActivity

/**
 * 外壳的唯一 Activity。
 *
 * §5.8 原本写的是 Flutter 外壳，这里改成 Kotlin + Compose：ARCore 只有 Android，
 * Flutter 的跨平台收益是零，代价却是把 §7 契约在 Dart 里再实现一遍（Kotlin 侧
 * 已经有 243 个单测盯着它），而 §5.7 的 EndpointResolver 本来就在 Android 侧。
 *
 * 界面栈自己维护（[Shell]），不引 navigation-compose：一共六页、没有深链、没有
 * 跨页参数序列化的需求，一个 `mutableStateListOf<Route>` 比一套路由 DSL 更好读。
 */
class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // 铺到系统栏底下去。这不是为了好看：不铺的话底部那条手势区的颜色归 framework
        // 的 navigationBarColor 管，而 MIUI 把它给成了**白色** —— 深色界面下面吊一条
        // 16dp 的纯白（实测 #FFFFFF）。铺过去之后那块由 NavigationBar 自己画（M3 的
        // NavigationBar 默认就带 navigationBars inset 的内边距），白条就没了。
        //
        // 显式 dark 而不用默认的 `auto`：auto 按**系统**深浅色模式决定图标颜色，而这个
        // App 是固定深色的（见 PhotoArTheme），系统在浅色模式时 auto 会给深色图标，
        // 于是状态栏图标在我们的深底上看不见。
        enableEdgeToEdge(
            statusBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
            navigationBarStyle = SystemBarStyle.dark(android.graphics.Color.TRANSPARENT),
        )

        DebugMode.init(this)

        // §9.2 的第二个触发时机。注册一次跟着进程活着。
        EndpointCenter.get(this).watchNetwork()

        // 内嵌的 ARCore 运行时首启要把 5.7 MiB 的 dex 从 assets 拷到 codeCacheDir。
        // 放在进程一起来就开，是因为「无感」的字面意思就是这步不该被用户等到 ——
        // 登录、翻照片的那几秒后台线程早解完了，真进扫描页时一问就是现成的。
        // 幂等且非阻塞，所以 ArCheck.state() 里那次调用照旧留着：哪条路径先到都算。
        ArCoreEmbeddedRuntime.start(this)

        setContent { PhotoArTheme { AppRoot() } }
    }
}

/**
 * 外壳的主题。**实现搬到了 [app.photoar.standalone.pixel.PhotoArPixelTheme]**，
 * 这里只留一个转发。
 *
 * 留这个名字而不是让 20 个调用点跟着改：它是 `setContent { PhotoArTheme { ... } }`
 * 那一处的入口名，而"这个 App 长什么样"的决定全在 pixel 那个包里 —— 换风格时
 * 只该动那一个包。
 */
@Composable
fun PhotoArTheme(content: @Composable () -> Unit) = PhotoArPixelTheme(content)


@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AppRoot() {
    val context = LocalContext.current
    val shell = remember { Shell(context) }
    val resolution = rememberResolution(shell.center)

    // 登录状态的版本号。凭证不是 Compose 的 State（存在 SharedPreferences 里），
    // 所以登录/登出之后靠它触发重组。
    var authRev by remember { mutableStateOf(0) }
    val gated = remember(authRev) {
        NavPolicy.needsGate(
            hasUsableEndpoint = shell.center.config.candidates.any { it.usable },
            phase = shell.center.authPhase(),
        )
    }
    val isAdmin = remember(authRev) { shell.center.config.auth?.isAdmin == true }

    // §9.2 的第一个触发时机。改造前这里在没配过的时候把人扔到设置页 —— 现在那件事
    // 由登录蒙版做（它的第一步就是问地址），所以这里只剩「配过就探一次活」。
    LaunchedEffect(Unit) {
        if (shell.center.configured) shell.center.refreshAsync()
    }

    // 角色定了之后校正落地页。两种情况都要：刚登录进来（栈里还是那个占位值），
    // 以及同一台手机换人登录（管理员登出、访客进来，而界面还停在「素材」页 ——
    // 那一页上每个按钮都会 403）。
    LaunchedEffect(gated, isAdmin) {
        if (!gated) {
            val want = NavPolicy.tabAfterRoleChange(tabOf(shell.currentRoot), isAdmin)
            shell.tab(routeOf(want))
        }
    }

    if (gated) {
        LoginGate(shell) { authRev++ }
        return
    }

    BackHandler(enabled = shell.current != shell.currentRoot) { shell.pop() }

    val route = shell.current
    val tabs = NavPolicy.tabsFor(isAdmin)
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(titleOf(route)) },
                navigationIcon = {
                    if (!route.root) {
                        IconButton(onClick = { shell.pop() }) {
                            PixelIcon(
                                bitmap = PixelIcons.Back,
                                size = PixelIconSize,
                                tint = MaterialTheme.colorScheme.onSurface,
                                modifier = Modifier.semantics { contentDescription = "返回" },
                            )
                        }
                    }
                },
                actions = {
                    // 「＋」现在切到「素材」页，而不是打开 NAS 浏览器：入库只有一条路
                    // 了（手机里挑一张照片 + 一段视频，一次传完就是一组映射）。
                    if (route == Route.Photos) {
                        IconButton(onClick = { shell.tab(Route.Media) }) {
                            PixelIcon(
                                bitmap = PixelIcons.Add,
                                size = PixelIconSize,
                                tint = MaterialTheme.colorScheme.primary,
                                modifier = Modifier.semantics {
                                    contentDescription = "传一组新素材"
                                },
                            )
                        }
                    }
                    // 探活状态点只给调试模式看，常态是噪声。开关在设置页「关于」里。
                    if (DebugMode.enabled) {
                        ViaChip(resolution) { shell.center.refreshAsync(force = true) }
                    }
                },
            )
        },
        // 访客不给 FAB：他的首页整个就是那颗「扫一扫」（200dp，比 FAB 大得多），
        // 再悬一颗在上面是同一个动作两个入口，而且正好压在那个大按钮上。
        floatingActionButton = { if (route.root && isAdmin) ScanButton() },
        floatingActionButtonPosition = FabPosition.Center,
        bottomBar = {
            if (route.root) {
                NavigationBar {
                    for (tab in tabs) {
                        val target = routeOf(tab)
                        NavigationBarItem(
                            selected = route == target,
                            onClick = { shell.tab(target) },
                            icon = {
                                // 选中时用主色、没选中用 Dim：Material 的
                                // NavigationBarItem 自己会给 icon 上色，但那套色是
                                // 按 secondaryContainer 那一档来的，而这里的图标是
                                // Canvas 画的、不吃 LocalContentColor。所以显式给。
                                PixelIcon(
                                    bitmap = iconOf(tab),
                                    size = PixelIconSize,
                                    tint = if (route == target) {
                                        MaterialTheme.colorScheme.primary
                                    } else {
                                        MaterialTheme.colorScheme.onSurfaceVariant
                                    },
                                )
                            },
                            label = { Text(labelOf(tab)) },
                            // 选中指示器画成透明。
                            //
                            // Material 3 在选中项的图标后面画一个**药丸形**的
                            // activeIndicator，它的形状来自 `NavigationBarTokens.
                            // ActiveIndicatorShape`（= shape token `CornerFull`），而
                            // `CornerFull` 在 Material 内部硬编码成 `CircleShape`，
                            // 不读 `MaterialTheme.shapes` —— 主题里没有任何旋钮能把它
                            // 变方，`indicatorColor` 是唯一的出口。
                            //
                            // 去掉它不丢信息：选中态已经由图标与文字的主色表达（上面
                            // 那个 tint，以及 selectedTextColor），而那是无障碍上更可靠
                            // 的一层 —— 药丸只是同一件事的第二种说法。
                            colors = NavigationBarItemDefaults.colors(
                                indicatorColor = Color.Transparent,
                            ),
                        )
                    }
                }
            }
        },
    ) { pad ->
        Box(Modifier.padding(pad)) {
            when (route) {
                Route.ScanHome -> ScanHomeScreen(shell)
                Route.Photos -> PhotosScreen(shell)
                Route.Media -> MediaScreen(shell)
                Route.Admin -> AdminScreen(shell)
                Route.History -> HistoryScreen(shell)
                Route.Settings -> SettingsScreen(shell, resolution) { authRev++ }
                Route.AdminWeb -> AdminWebScreen(shell.center)
                is Route.Detail -> PhotoDetailScreen(shell, route.photoId)
                is Route.Play -> PlayScreen(shell, route.photoId)
                Route.Cache -> CacheScreen(shell)
            }
        }
    }
}

/** 页签 → 路由。两者是一一对应的，但 [Tab] 是纯逻辑（可测），[Route] 带 Compose。 */
private fun routeOf(tab: Tab): Route = when (tab) {
    Tab.SCAN -> Route.ScanHome
    Tab.PHOTOS -> Route.Photos
    Tab.MEDIA -> Route.Media
    Tab.ADMIN -> Route.Admin
    Tab.SETTINGS -> Route.Settings
}

/**
 * 根路由 → 页签。
 *
 * 只在 `shell.currentRoot` 上调用，所以参数一定是个根。`else` 那一支给 PHOTOS 是
 * 因为 Kotlin 要求 when 穷尽 —— 不是「非根路由也当照片页」，而是那些分支到不了。
 */
private fun tabOf(root: Route): Tab = when (root) {
    Route.ScanHome -> Tab.SCAN
    Route.Media -> Tab.MEDIA
    Route.Admin -> Tab.ADMIN
    Route.Settings -> Tab.SETTINGS
    else -> Tab.PHOTOS
}

private fun labelOf(tab: Tab): String = when (tab) {
    Tab.SCAN -> "扫一扫"
    Tab.PHOTOS -> "照片"
    Tab.MEDIA -> "素材"
    Tab.ADMIN -> "管理"
    Tab.SETTINGS -> "设置"
}

/**
 * 页签图标。
 *
 * 只用 Material 自带的那几个基础图标，没有引 material-icons-extended —— 那个包
 * 会给 APK 加几 MB，而这个 App 已经因为内嵌 ARCore 到 186 MB 了。
 */
@Suppress("DEPRECATION")
/**
 * 页签图标。全部是自己画的 16×16 像素图（[PixelIcons]），不是 Material 的矢量图。
 *
 * 改造前 SCAN 与 PHOTOS **用的是同一个** `Icons.Filled.Home` —— 底栏上两个页签
 * 长得一样，靠文字区分。现在一个是房子、一个是相框叠。
 */
private fun iconOf(tab: Tab): PixelBitmap = when (tab) {
    Tab.SCAN -> PixelIcons.Home
    Tab.PHOTOS -> PixelIcons.Photos
    Tab.MEDIA -> PixelIcons.Upload
    Tab.ADMIN -> PixelIcons.Admin
    Tab.SETTINGS -> PixelIcons.Settings
}

private fun titleOf(route: Route): String = when (route) {
    Route.ScanHome -> "photoar"
    Route.Photos -> "照片库"
    Route.Media -> "素材"
    Route.Admin -> "管理"
    Route.History -> "识别历史"
    Route.Settings -> "设置"
    Route.AdminWeb -> "管理台"
    is Route.Detail -> "照片详情"
    is Route.Play -> "试播"
    Route.Cache -> "离线缓存"
}

/**
 * 「扫一扫」。这个 App 只有一个主动作，它就是那个。
 *
 * 位置在底栏正上方居中（[FabPosition.Center]），而不是原来的右下角：右下角是「次要
 * 动作」的位置，而这里没有比它更主要的动作 —— 用户不是来管理照片库的，是来扫照片的。
 *
 * 尺寸自己写 76dp 而不用 `LargeFloatingActionButton`（96dp）：96 的圆盖在三格底栏
 * 上会把中间那个「历史」压掉一半。76 刚好落在底栏上沿之上。
 */
@Composable
private fun ScanButton() {
    val context = LocalContext.current
    // 不用 FloatingActionButton：那个是圆的，而圆是这套风格里唯一不能出现的形状
    // （像素画没有抗锯齿的曲线）。方块 + 斜面同样是"悬在上面的主操作"，
    // 而且它给的按下反馈是斜面翻转，比 ripple 更贴这套风格。
    PixelButton(
        onClick = { ArScanActivity.start(context) },
        modifier = Modifier.size(76.dp),
    ) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            PixelIcon(
                bitmap = PixelIcons.Scan,
                size = 32.dp,
                tint = MaterialTheme.colorScheme.onPrimary,
            )
            Text(
                "扫一扫",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onPrimary,
            )
        }
    }
}

/**
 * 顶栏右上角那颗状态点：当前 api 走的哪条通道、多少毫秒。点一下强制重探。
 *
 * 只在 [DebugMode] 下出现。它对宾客没有意义，但在外面扫不出来时，第一个要回答的问题
 * 就是「现在走的是哪条」—— 所以留着，只是收到调试模式后面。
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
            .clip(RectangleShape)
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

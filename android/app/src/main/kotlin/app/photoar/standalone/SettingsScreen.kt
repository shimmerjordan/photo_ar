package app.photoar.standalone

import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import app.photoar.arview.AuthPhase
import app.photoar.arview.AuthPolicy
import app.photoar.arview.AuthState
import app.photoar.arview.EndpointCandidate
import app.photoar.arview.EndpointUse
import app.photoar.arview.Probed
import app.photoar.arview.Resolution
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * §9.1 的那份配置，可编辑。
 *
 * 每条通道后面都把**探活的原因原文**贴出来（[Probed.error]），不做二次加工：
 * 「令牌不对（401）」「这个地址上没有 photo-ar-server（404）」「不通」是三个完全
 * 不同的下一步动作，归成一句「连接失败」等于把排查信息扔了。
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun SettingsScreen(shell: Shell, resolution: Resolution?) {
    val center = shell.center
    var cfg by remember { mutableStateOf(center.config) }
    var saving by remember { mutableStateOf(false) }
    var note by remember { mutableStateOf<String?>(null) }

    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp)
            // 88 而不是 32：悬在底栏上方的「扫一扫」（[MainActivity] 的 ScanButton，
            // 76dp 的圆）不算进 Scaffold 的 innerPadding 里，滚到底时会盖住最后一行 ——
            // 实测把「开源地址」那行的链接压掉了一半。
            .padding(bottom = 88.dp),
    ) {
        AccountSection(shell)

        Section("通道")
        Text(
            text = "地址留空的通道不参与探活，但留在这里，拿到地址填一格就能用。" +
                "「适合」决定它承担哪条：接口请求走 api，视频流走 media。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        cfg.candidates.forEachIndexed { i, c ->
            CandidateCard(
                c = c,
                probed = probedFor(resolution, c),
                onChange = { next ->
                    cfg = cfg.copy(
                        candidates = cfg.candidates.toMutableList().also { it[i] = next },
                    )
                },
                onDelete = if (cfg.candidates.size > 1) {
                    {
                        cfg = cfg.copy(
                            candidates = cfg.candidates.toMutableList().also { it.removeAt(i) },
                        )
                    }
                } else {
                    null
                },
            )
        }

        OutlinedButton(
            onClick = {
                cfg = cfg.copy(
                    candidates = cfg.candidates + EndpointCandidate(
                        name = "通道${cfg.candidates.size + 1}",
                        base = "",
                        prefer = listOf(EndpointUse.API, EndpointUse.MEDIA),
                    ),
                )
            },
            modifier = Modifier.padding(top = 8.dp),
        ) {
            Text("＋ 添加通道")
        }

        Section("现在走的是")
        KeyValue("api", describe(resolution?.api))
        KeyValue("media", describe(resolution?.media))
        KeyValue(
            "上传",
            // §9.4：media 走隧道时 Cloudflare 有 100MB 请求体上限，服务端也会按
            // cf-ray 头回 413，所以入口直接不给。
            if (center.uploadAllowed()) "可以（media 不是隧道）" else "不可以（media 走隧道或还没探活）",
        )

        note?.let {
            Text(
                text = it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(top = 12.dp),
            )
        }

        Row(
            Modifier
                .fillMaxWidth()
                .padding(top = 20.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Button(
                onClick = {
                    saving = true
                    note = null
                    // 只把**候选列表**写回去，凭证那几个字段从 `center.config` 现取。
                    //
                    // 不能直接存 `cfg`：它是进这一页时的快照，而这期间用户可能在上面
                    // 那个登录表单里登录过（token 变了）。存快照会把刚拿到的 token
                    // 覆盖成旧的那个 —— 表现是「登录成功了，改一下地址又变成未登录」。
                    center.save(center.config.copy(candidates = cfg.candidates)) { r ->
                        saving = false
                        note = if (r.offline) "保存了，但一条都没通" else "保存了，探活完成"
                        Thumbs.clear()
                    }
                },
                enabled = !saving,
            ) {
                Text(if (saving) "探活中…" else "保存并探活")
            }
            OutlinedButton(
                onClick = {
                    saving = true
                    center.refreshAsync(force = true) {
                        saving = false
                        note = "重新探活完成"
                    }
                },
                enabled = !saving,
            ) {
                Text("只重探")
            }
        }

        Section("离线缓存")
        Text(
            text = "把最近扫到的照片和视频存到手机上，没网也能扫（§15）。" +
                "缓存多少、什么时候同步都在那一页里。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedButton(
            onClick = { shell.push(Route.Cache) },
            modifier = Modifier.padding(top = 8.dp),
        ) {
            Text("管理离线缓存")
        }

        if (DebugMode.enabled) {
            FeaturePathSection(shell)

            Text(
                text = "会话令牌明文存在 SharedPreferences 里，没上 Keystore：这台机器的门槛是锁屏，" +
                    "而拿到 root 的人一样能读出 Keystore 解出来的明文。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 20.dp),
            )
        }

        AboutSection()
    }
}

/**
 * 「关于」。常态下这一屏最底下唯一的东西，也是进调试模式的唯一入口。
 *
 * 版本号连点 [DebugMode.TAPS_TO_ENABLE] 下开调试 —— 抄安卓「开发者选项」那套交互，
 * 因为它是用户已经会的：不用解释，也不会被误触。
 */
@Composable
private fun AboutSection() {
    val context = LocalContext.current
    val version = remember {
        runCatching {
            context.packageManager.getPackageInfo(context.packageName, 0).versionName
        }.getOrNull() ?: "未知"
    }
    var hint by remember { mutableStateOf<String?>(null) }

    Section("关于")

    Row(
        Modifier
            .fillMaxWidth()
            .clickable {
                if (DebugMode.enabled) return@clickable
                hint = if (DebugMode.tap()) {
                    "调试模式开了：顶栏多一颗探活状态点。"
                } else if (DebugMode.tapsLeft <= 5) {
                    // 前几下不给反馈，不然「点着玩」的人都会点出来
                    "再点 ${DebugMode.tapsLeft} 下进入调试模式"
                } else {
                    null
                }
            }
            .padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = "版本",
            modifier = Modifier.width(92.dp),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(text = version, style = MaterialTheme.typography.bodyMedium)
    }

    Row(
        Modifier
            .fillMaxWidth()
            .clickable {
                runCatching {
                    context.startActivity(
                        Intent(Intent.ACTION_VIEW, Uri.parse(SOURCE_URL))
                            // 从 Compose 的 LocalContext 起 Activity：这个 context 是
                            // Activity 本身，理论上不用这个 flag，但 MIUI 上偶发把它
                            // 当 application context 处理然后抛 AndroidRuntime。
                            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                    )
                }.onFailure { hint = "这台机器上没有能打开网页的应用" }
            }
            .padding(vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = "开源地址",
            modifier = Modifier.width(92.dp),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            text = SOURCE_URL,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.primary,
        )
    }

    hint?.let {
        Text(
            text = it,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.padding(top = 4.dp),
        )
    }

    if (DebugMode.enabled) {
        OutlinedButton(
            onClick = {
                DebugMode.set(false)
                hint = "调试模式关了。"
            },
            modifier = Modifier.padding(top = 8.dp),
        ) {
            Text("关闭调试模式")
        }
    }
}

private const val SOURCE_URL = "https://github.com/shimmerjordan/photo_ar"

/**
 * 账号那一块：没登录时是登录表单，登录了就显示是谁 + 一个退出。
 *
 * ⚠️ 这里只有渲染。「凭证怎么存、什么时候算过期、错误码怎么映射成文案」三件事全在
 * `:arview` 的 [AuthPolicy] 里，有单测盯着 —— Compose 在这个项目里跑不起来也验不了，
 * 把判断留在这里等于放弃验证。
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun AccountSection(shell: Shell) {
    val center = shell.center
    val scope = rememberCoroutineScope()

    // 这几个 state 要能被登录/登出改，所以用 `phase` 当重组的触发器。
    var phase by remember { mutableStateOf(center.authPhase()) }
    var name by remember { mutableStateOf(center.config.auth?.name ?: "") }
    var password by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    fun refresh() {
        phase = center.authPhase()
    }

    Section("账号")
    KeyValue("当前", AuthPolicy.describe(center.config, System.currentTimeMillis()))

    if (phase == AuthPhase.ACTIVE || phase == AuthPhase.EXPIRING_SOON) {
        center.config.auth?.expiresAt?.let {
            KeyValue("有效期至", Fmt.dateTime(it))
        }
        if (phase == AuthPhase.EXPIRING_SOON) {
            Banner("登录即将过期，建议现在就重新登录一次。", Tone.WARN)
        }
        OutlinedButton(
            onClick = {
                busy = true
                scope.launch {
                    // 登出先打服务端（作废那条 session），再清本地。反过来的话本地已经
                    // 没有 token 了，那个请求会 401。
                    withContext(Dispatchers.IO) { shell.client.logout() }
                    center.saveCredentials(AuthPolicy.applyLogout(center.config))
                    password = ""
                    busy = false
                    refresh()
                    Thumbs.clear()
                }
            },
            enabled = !busy,
            modifier = Modifier.padding(top = 8.dp),
        ) {
            Text(if (busy) "退出中…" else "退出登录")
        }
        return
    }

    if (phase == AuthPhase.UNKNOWN_TOKEN) {
        Banner(
            "这台机器上有一个旧版手填的令牌，还能用。想换成用户登录就在下面登录一次。",
            Tone.WARN,
        )
        OutlinedButton(
            onClick = {
                busy = true
                error = null
                scope.launch {
                    val r = runCatching { withContext(Dispatchers.IO) { shell.client.me() } }
                    busy = false
                    r.fold(
                        onSuccess = {
                            // 认领这个 token：问服务端它是谁，把答案存下来。
                            // 这比让用户对着「不知道归属」发愁好 —— 而它也顺手验证了
                            // 那个 token 还活着。
                            center.saveCredentials(
                                center.config.copy(auth = AuthState.of(it)),
                            )
                            refresh()
                        },
                        onFailure = { e -> error = Fmt.errText(e) },
                    )
                }
            },
            enabled = !busy,
            modifier = Modifier.padding(top = 6.dp),
        ) {
            Text(if (busy) "查询中…" else "查一下这个令牌是谁的")
        }
    }
    if (phase == AuthPhase.EXPIRED) {
        Banner("登录已过期，重新登录一次。", Tone.BAD)
    }

    OutlinedTextField(
        value = name,
        onValueChange = { name = it },
        label = { Text("名字") },
        singleLine = true,
        modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
        value = password,
        onValueChange = { password = it },
        label = { Text("口令") },
        singleLine = true,
        visualTransformation = if (showPassword) {
            VisualTransformation.None
        } else {
            PasswordVisualTransformation()
        },
        trailingIcon = {
            TextButton(onClick = { showPassword = !showPassword }) {
                Text(if (showPassword) "隐藏" else "显示")
            }
        },
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 6.dp),
    )
    Text(
        text = "访客留空，管理员必填。账号由管理员在管理台建 —— 输一个没建过的名字是登不进来的。",
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )

    error?.let { Banner(it, Tone.BAD) }

    Button(
        onClick = {
            busy = true
            error = null
            scope.launch {
                val result = runCatching {
                    withContext(Dispatchers.IO) {
                        shell.client.login(name.trim(), password.ifEmpty { null })
                    }
                }
                busy = false
                result.fold(
                    onSuccess = {
                        center.saveCredentials(AuthPolicy.applyLogin(center.config, it))
                        password = ""
                        name = it.name
                        error = null
                        refresh()
                        // 换了人 = 可见的照片可能完全不同，缩略图缓存是按 URL 存的，
                        // URL 没变但内容的可见性变了。
                        Thumbs.clear()
                        shell.libraryChanged()
                    },
                    onFailure = { e -> error = Fmt.loginErr(e) },
                )
            }
        },
        enabled = !busy && name.isNotBlank(),
        modifier = Modifier.padding(top = 10.dp),
    ) {
        Text(if (busy) "登录中…" else "登录")
    }
}

/**
 * 端上提特征的开关。
 *
 * 默认关，而且文案要说清「为什么默认关」—— 这条路在开发机上无法真机验证，不该悄悄
 * 变成所有人的默认行为。
 */
@Composable
private fun FeaturePathSection(shell: Shell) {
    val center = shell.center
    var on by remember { mutableStateOf(center.config.onDeviceFeatures) }

    Section("端上提特征（实验）")
    Row(verticalAlignment = Alignment.CenterVertically) {
        Switch(
            checked = on,
            onCheckedChange = {
                on = it
                center.saveCredentials(center.config.copy(onDeviceFeatures = it))
            },
        )
        Text(
            text = if (on) "开：传描述子" else "关：传整帧 JPEG",
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.padding(start = 12.dp),
        )
    }
    Text(
        text = "开了之后由手机跑 XFeat 提特征，只把 512×64 的描述子传上去，服务端不再推理。" +
            "需要服务端的识别后端是 xfeat，模型会在第一次扫描时下载（约 4.3MB）并缓存。" +
            "任何一步不成会自动改回传整帧，功能不丢、只是慢一点。" +
            "默认关是因为端上推理还没在真机上验过。",
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
private fun CandidateCard(
    c: EndpointCandidate,
    probed: Probed?,
    onChange: (EndpointCandidate) -> Unit,
    onDelete: (() -> Unit)?,
) {
    Card(Modifier.fillMaxWidth().padding(top = 10.dp)) {
        Column(Modifier.padding(12.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(
                    value = c.name,
                    onValueChange = { onChange(c.copy(name = it)) },
                    label = { Text("名字") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(0.55f),
                )
                Switch(
                    checked = c.enabled,
                    onCheckedChange = { onChange(c.copy(enabled = it)) },
                    modifier = Modifier.padding(start = 12.dp),
                )
                if (onDelete != null) {
                    TextButton(onClick = onDelete) { Text("删除") }
                }
            }

            OutlinedTextField(
                value = c.base,
                onValueChange = { onChange(c.copy(base = it.trim())) },
                label = { Text("地址") },
                placeholder = { Text("http://10.0.0.9:8964") },
                singleLine = true,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 6.dp),
            )

            FlowRow(
                Modifier
                    .fillMaxWidth()
                    .padding(top = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalArrangement = Arrangement.spacedBy(4.dp),
            ) {
                EndpointUse.entries.forEach { use ->
                    FilterChip(
                        selected = use in c.prefer,
                        onClick = {
                            val next = if (use in c.prefer) c.prefer - use else c.prefer + use
                            onChange(c.copy(prefer = next))
                        },
                        label = { Text("适合 ${use.key}") },
                    )
                }
                FilterChip(
                    selected = c.tunnel,
                    onClick = { onChange(c.copy(tunnel = !c.tunnel)) },
                    label = { Text("是隧道") },
                )
            }

            Text(
                text = statusOf(probed),
                style = MaterialTheme.typography.labelSmall,
                color = when {
                    probed == null -> MaterialTheme.colorScheme.onSurfaceVariant
                    probed.reachable -> MaterialTheme.colorScheme.primary
                    else -> MaterialTheme.colorScheme.error
                },
                modifier = Modifier.padding(top = 6.dp),
            )
        }
    }
}

/**
 * 把探活结果对回候选。
 *
 * 按 name+base 匹配而不是按下标：用户改了地址还没点保存时，[Resolution] 里的还是
 * 旧列表，按下标会把上一条的「通了 23ms」显示到一个刚改过的地址上。
 */
private fun probedFor(resolution: Resolution?, c: EndpointCandidate): Probed? =
    resolution?.probed?.firstOrNull { it.candidate.name == c.name && it.candidate.base == c.base }

private fun statusOf(probed: Probed?): String = when {
    probed == null -> "改过了，保存后再探"
    probed.reachable -> "通 · ${probed.latencyMs}ms"
    else -> probed.error ?: "不通"
}

private fun describe(p: Probed?): String =
    if (p == null) "还没有（离线或没探活）" else "${p.candidate.name} · ${p.candidate.base}"

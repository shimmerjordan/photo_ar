package app.photoar.standalone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import app.photoar.arview.AuthPhase
import app.photoar.arview.AuthPolicy
import app.photoar.arview.EndpointCandidate
import app.photoar.arview.EndpointCenter
import app.photoar.arview.EndpointUse
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 登录蒙版：没登录之前，除了这一屏什么都看不到。
 *
 * ## 为什么是「蒙版」而不是「设置页里的一块」
 *
 * 改造前登录埋在设置页的账号区里，App 一打开落在照片库。后果分两种人：
 *
 * - **宾客**：打开是一个空列表加一句连不上，而他要做的事（登录）在第三个页签里往下
 *   滚两屏。没有人会找到那里。
 * - **访客账号登进来之后**：底栏那三个页签有两个的接口是 admin only（`/v1/history`
 *   整个是），点进去只有 403。也就是说改造前**访客的界面本来就是坏的**。
 *
 * 蒙版把「你是谁」提到最前面，后面的界面才有可能按角色给对。
 *
 * ## 两步：先地址，再账号
 *
 * 全新装机两样都没有。合成一屏会是四个输入框加一段解释，而其中两个（地址）跟用户
 * 心里的「登录」毫无关系。分步之后，绝大多数人（地址已经配好，或者由管理员帮着配过）
 * 只会看到第二步。
 *
 * 哪一步由 [NavPolicy.gateStep] 决定，那是纯函数、有测试。
 */
@Composable
fun LoginGate(shell: Shell, onEntered: () -> Unit) {
    val center = shell.center
    // 地址存进去之后 `center.config` 会变，而它不是 Compose 的 State，所以用一个
    // 版本号当重组触发器。
    var rev by remember { mutableStateOf(0) }
    // 「改地址」按钮的显式覆盖。**不能**靠 `hasEndpoint` 来退回第一步：地址已经存下
    // 来了，那个判断仍然是 true，于是点了没反应 —— 而这个按钮存在的场景正是「地址
    // 填错了，每次登录都失败」，没反应等于把人堵死在这一屏。
    var forceEndpoint by remember { mutableStateOf(false) }
    val step = remember(rev, forceEndpoint) {
        if (forceEndpoint) GateStep.ENDPOINT else NavPolicy.gateStep(hasEndpoint(center))
    }

    Surface(Modifier.fillMaxSize()) {
        Column(
            Modifier
                // safeDrawing 而不是 systemBars：这一屏是全屏的（外面没有 Scaffold
                // 给 innerPadding），刻痕屏上不避开的话标题会被挖孔压住。
                .safeDrawingPadding()
                // 键盘弹起来时要能滚到被盖住的那个输入框。imePadding 单独加一次是
                // 因为 verticalScroll 自己不知道键盘的存在。
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 24.dp, vertical = 32.dp),
        ) {
            Text("photoar", style = MaterialTheme.typography.headlineMedium)
            Spacer(Modifier.height(4.dp))
            Text(
                text = when (step) {
                    GateStep.ENDPOINT -> "先告诉它服务端在哪"
                    GateStep.LOGIN -> "用管理员给你的名字登录"
                },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(28.dp))

            when (step) {
                GateStep.ENDPOINT -> EndpointStep(center) { forceEndpoint = false; rev++ }
                GateStep.LOGIN -> LoginStep(shell, onEntered) { forceEndpoint = true }
            }
        }
    }
}

/** 有没有一条能用的通道。[EndpointCenter.configured] 还要求 token 非空，这里只看地址。 */
private fun hasEndpoint(center: EndpointCenter): Boolean =
    center.config.candidates.any { it.usable }

/**
 * 第一步：填地址。
 *
 * 只给**一个**输入框，而不是把设置页那套「多通道 + 适合什么 + 是不是隧道」搬过来。
 * 第一次装机时只有一条路可走（家里的地址），多通道是后来才有意义的事 —— 而它在
 * 设置页里一直都在。
 */
@Composable
private fun EndpointStep(center: EndpointCenter, onSaved: () -> Unit) {
    var base by remember { mutableStateOf(center.config.candidates.firstOrNull()?.base ?: "") }
    var busy by remember { mutableStateOf(false) }
    var note by remember { mutableStateOf<String?>(null) }

    OutlinedTextField(
        value = base,
        onValueChange = { base = it; note = null },
        label = { Text("服务端地址") },
        placeholder = { Text("http://192.168.1.10:8964") },
        singleLine = true,
        // Uri 键盘：带 `/` 和 `.`，而且不自动大写 —— 默认键盘会把 http 变成 Http。
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
        modifier = Modifier.fillMaxWidth(),
    )
    Text(
        text = "填服务端的地址和端口。默认端口是 8964，家里网络内直接写内网 IP；" +
            "在外面用就填管理员给的那个域名。",
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 6.dp),
    )

    note?.let {
        Text(
            text = it,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.error,
            modifier = Modifier.padding(top = 12.dp),
        )
    }

    Button(
        onClick = {
            val trimmed = base.trim().trimEnd('/')
            if (trimmed.isEmpty()) {
                note = "地址不能为空。"
                return@Button
            }
            if (!trimmed.startsWith("http://") && !trimmed.startsWith("https://")) {
                // 自己补 http:// 是错的：补错了会得到一个「连不上」，而人看不出是
                // 因为协议猜错。让他补，顺手也把「这是个 URL」讲清楚了。
                note = "地址要以 http:// 或 https:// 开头。"
                return@Button
            }
            busy = true
            note = null
            // 保留既有候选里的其余项（老装机可能配过好几条），只改/补第一条。
            val rest = center.config.candidates.drop(1)
            val first = (center.config.candidates.firstOrNull() ?: DEFAULT_CANDIDATE)
                .copy(base = trimmed, enabled = true)
            center.save(center.config.copy(candidates = listOf(first) + rest)) { r ->
                busy = false
                if (r.offline) {
                    // 探不通仍然**放他进下一步**：地址可能是对的而此刻网络不通
                    // （比如还没连上家里的 Wi-Fi）。挡在这里等于让人以为地址填错了。
                    note = "这个地址现在探不通。可以先继续登录，" +
                        "但如果登录也失败，先检查地址和网络。"
                }
                onSaved()
            }
        },
        enabled = !busy,
        modifier = Modifier.padding(top = 20.dp),
    ) {
        Text(if (busy) "探活中…" else "保存并继续")
    }
}

private val DEFAULT_CANDIDATE = EndpointCandidate(
    name = "家里",
    base = "",
    prefer = listOf(EndpointUse.API, EndpointUse.MEDIA),
)

/** 第二步：名字 + 口令。 */
@Composable
private fun LoginStep(shell: Shell, onEntered: () -> Unit, onBackToEndpoint: () -> Unit) {
    val center = shell.center
    val scope = rememberCoroutineScope()
    var name by remember { mutableStateOf(center.config.auth?.name ?: "") }
    var password by remember { mutableStateOf("") }
    var showPassword by remember { mutableStateOf(false) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    if (center.authPhase() == AuthPhase.EXPIRED) {
        Text(
            text = "上次的登录已经过期了，重新登录一次。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.error,
            modifier = Modifier.padding(bottom = 12.dp),
        )
    }

    fun submit() {
        if (name.isBlank() || busy) return
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
                    // 换了人 = 可见的照片可能完全不同，而缩略图缓存是按 URL 存的
                    // （URL 没变，内容的可见性变了）。
                    Thumbs.clear()
                    shell.libraryChanged()
                    onEntered()
                },
                onFailure = { e -> error = Fmt.loginErr(e) },
            )
        }
    }

    OutlinedTextField(
        value = name,
        onValueChange = { name = it; error = null },
        label = { Text("名字") },
        singleLine = true,
        keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
        modifier = Modifier.fillMaxWidth(),
    )
    OutlinedTextField(
        value = password,
        onValueChange = { password = it; error = null },
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
        // 键盘上的回车直接登录 —— 这一屏只有一个动作。
        keyboardOptions = KeyboardOptions(
            keyboardType = KeyboardType.Password,
            imeAction = ImeAction.Done,
        ),
        keyboardActions = KeyboardActions(onDone = { submit() }),
        modifier = Modifier
            .fillMaxWidth()
            .padding(top = 10.dp),
    )
    Text(
        text = "宾客留空口令，管理员必填。账号由管理员建 —— 输一个没建过的名字是登不进来的。",
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 6.dp),
    )

    error?.let {
        Text(
            text = it,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.error,
            modifier = Modifier.padding(top = 12.dp),
        )
    }

    Row(
        Modifier.padding(top = 20.dp),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Button(onClick = { submit() }, enabled = !busy && name.isNotBlank()) {
            Text(if (busy) "登录中…" else "登录")
        }
        // 回到第一步。必须有：地址填错了的话，第二步的每一次登录都会失败，而错误
        // 信息是「连不上」—— 没有这个按钮，用户唯一的出路是清数据重装。
        OutlinedButton(onClick = { onBackToEndpoint() }, enabled = !busy) {
            Text("改地址")
        }
    }
}

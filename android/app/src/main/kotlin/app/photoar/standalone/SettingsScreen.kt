package app.photoar.standalone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
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
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.unit.dp
import app.photoar.arview.EndpointCandidate
import app.photoar.arview.EndpointUse
import app.photoar.arview.Probed
import app.photoar.arview.Resolution

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
    var showToken by remember { mutableStateOf(false) }
    var saving by remember { mutableStateOf(false) }
    var note by remember { mutableStateOf<String?>(null) }

    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp)
            .padding(bottom = 32.dp),
    ) {
        Section("访问令牌")
        OutlinedTextField(
            value = cfg.token,
            onValueChange = { cfg = cfg.copy(token = it) },
            label = { Text("PHOTOAR_TOKEN") },
            singleLine = true,
            visualTransformation = if (showToken) {
                VisualTransformation.None
            } else {
                PasswordVisualTransformation()
            },
            trailingIcon = {
                TextButton(onClick = { showToken = !showToken }) {
                    Text(if (showToken) "隐藏" else "显示")
                }
            },
            modifier = Modifier.fillMaxWidth(),
        )
        Text(
            text = "和服务端 docker-compose 里的 PHOTOAR_TOKEN 一致。所有通道共用一个。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

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
                    // save 会立刻强制重探（配置变了，上次的结果一定过期）
                    center.save(cfg) { r ->
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

        Text(
            text = "令牌明文存在 SharedPreferences 里，没上 Keystore：这台机器的门槛是锁屏，" +
                "而拿到 root 的人一样能读出 Keystore 解出来的明文。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 20.dp),
        )
    }
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

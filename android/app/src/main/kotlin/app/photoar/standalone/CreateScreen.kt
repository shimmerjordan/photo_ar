package app.photoar.standalone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
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
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.photoar.arview.CreateResult
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 入库表单：参考图 + 打印宽度 + 可选视频 + 可选标题 → `POST /v1/photo`。
 *
 * 打印宽度是这一页的全部重点。§11.1：`addImage` 传准确物理宽度时跟踪精度明显好于
 * 让 ARCore 自己估，所以 `print_width_m` 是 NOT NULL 的；填错不会报错，只会让 AR
 * 里的视频一直飘。§17 因此要求给常用尺寸预设，见 [Fmt.presetMm]。
 */
@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun CreateScreen(shell: Shell) {
    val draft = shell.draft
    if (draft == null) {
        Column(
            Modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text("没有待入库的照片")
            Button(onClick = { shell.popToRoot() }, modifier = Modifier.padding(top = 12.dp)) {
                Text("回照片库")
            }
        }
        return
    }

    val scope = rememberCoroutineScope()
    var action by remember { mutableStateOf<Action>(Action.Idle) }
    var result by remember { mutableStateOf<CreateResult?>(null) }

    val done = result
    if (done != null) {
        CreateDone(shell, done)
        return
    }

    val widthMm = Fmt.parseWidthMm(draft.widthText)

    Box(Modifier.fillMaxSize()) {
        Column(
            Modifier
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp)
                .padding(bottom = 32.dp),
        ) {
            val refKey = "fs:${draft.refPath}"
            NetImage(
                key = refKey,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(180.dp)
                    .padding(top = 12.dp),
                // 方向决定相纸预设取长边还是短边，所以要拿到真实像素尺寸
                onSize = { w, h -> draft.landscape = w >= h },
            ) { shell.client.fsThumb(draft.refPath) }

            Text(
                text = draft.refPath,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 6.dp),
            )

            Section("打印宽度")
            Text(
                text = if (draft.landscape) "这张是横的，预设取长边" else "这张是竖的，预设取短边",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            FlowRow(
                Modifier
                    .fillMaxWidth()
                    .padding(top = 6.dp),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Fmt.Paper.entries.forEach { paper ->
                    val mm = Fmt.presetMm(paper, draft.landscape)
                    val text = Fmt.mmText(mm)
                    FilterChip(
                        selected = draft.widthText.trim() == text,
                        onClick = { draft.widthText = text },
                        label = { Text("${paper.label} · $text") },
                    )
                }
            }
            OutlinedTextField(
                value = draft.widthText,
                onValueChange = { draft.widthText = it },
                label = { Text("宽度（毫米）") },
                singleLine = true,
                isError = draft.widthText.isNotBlank() && widthMm == null,
                supportingText = {
                    if (draft.widthText.isNotBlank() && widthMm == null) {
                        Text("填 10–2000 之间的毫米数")
                    }
                },
                keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Decimal),
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 8.dp),
            )

            Section("视频（可以之后再配）")
            Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
                val v = draft.videoPath
                if (v == null) {
                    OutlinedButton(
                        onClick = { shell.push(Route.Browse(Pick.VIDEO_FOR_DRAFT, null)) },
                    ) {
                        Text("从 NAS 选")
                    }
                } else {
                    NetImage(key = "fs:$v", modifier = Modifier.size(48.dp)) {
                        shell.client.fsThumb(v)
                    }
                    Text(
                        text = v,
                        style = MaterialTheme.typography.labelSmall,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier
                            .padding(horizontal = 8.dp)
                            .fillMaxWidth(0.7f),
                    )
                    TextButton(onClick = { draft.videoPath = null }) { Text("清除") }
                }
            }

            Section("标题（可选）")
            OutlinedTextField(
                value = draft.title,
                onValueChange = { draft.title = it },
                label = { Text("给这张起个名字") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )

            Button(
                onClick = {
                    val mm = widthMm ?: return@Button
                    action = Action.Running("正在入库…")
                    scope.launch {
                        action = try {
                            val r = withContext(Dispatchers.IO) {
                                shell.client.createPhoto(
                                    refPath = draft.refPath,
                                    videoPath = draft.videoPath,
                                    printWidthMm = mm,
                                    title = draft.title.ifBlank { null },
                                )
                            }
                            shell.libraryChanged()
                            result = r
                            Action.Idle
                        } catch (e: Throwable) {
                            Action.Failed(Fmt.errText(e))
                        }
                    }
                },
                enabled = widthMm != null && action !is Action.Running,
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(top = 24.dp),
            ) {
                Text("入库")
            }
            Text(
                text = "服务端会先打质量分，低于 75 直接拒绝并告诉你分数 —— " +
                    "不合格的照片留到扫不出来才发现更麻烦。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 8.dp),
            )
        }

        ActionOverlay(action) { action = Action.Idle }
    }
}

/** 入库成功。把服务端返回的分数摊开给人看，别只弹一句「成功」。 */
@Composable
private fun CreateDone(shell: Shell, r: CreateResult) {
    Column(
        Modifier
            .fillMaxSize()
            .padding(24.dp),
    ) {
        Text("入库成功", style = MaterialTheme.typography.titleLarge)
        Banner("质量分 ${r.qualityScore}（${Fmt.qualityLabel(r.qualityScore)}）", Tone.OK)
        KeyValue("自匹配", "${r.selfScore}")
        KeyValue("打印宽度", Fmt.widthMm(r.printWidthM))
        KeyValue("索引大小", Fmt.bytes(r.imgdbBytes))
        KeyValue("转码", if (r.transcoded) "转了" else "原片已合规，跳过")
        KeyValue("耗时", Fmt.elapsed(r.elapsedMs))
        KeyValue("库里现有", "${r.libraryPhotos} 张")

        Row(
            Modifier
                .fillMaxWidth()
                .padding(top = 24.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Button(onClick = {
                shell.draft = null
                shell.popToRoot()
                shell.push(Route.Detail(r.photoId))
            }) {
                Text("看详情")
            }
            OutlinedButton(onClick = {
                shell.draft = null
                shell.popToRoot()
            }) {
                Text("完成")
            }
        }
    }
}

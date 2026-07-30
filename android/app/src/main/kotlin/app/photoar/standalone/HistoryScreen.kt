package app.photoar.standalone

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.photoar.arview.HistoryEntry

/**
 * 识别历史。
 *
 * 这一页是调参用的：inliers 就是 §3 那个阈值 40 两侧的实际取值，`via` 是当时走的
 * 哪条通道。「为什么刚才那张没扫出来」在这里能看到答案 —— 未命中也记（[HistoryEntry.matched]
 * 为 false），而且未命中那几条才是有信息量的。
 */
@Composable
fun HistoryScreen(shell: Shell) {
    val fetch = rememberFetch(Unit) { shell.client.history(limit = 100) }

    LoadFrame(fetch) { entries ->
        if (entries.isEmpty()) {
            Column(
                Modifier
                    .fillMaxSize()
                    .padding(32.dp),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text("还没有识别记录", style = MaterialTheme.typography.titleMedium)
                Text(
                    text = "扫一次照片就会在这里留一条，命中和未命中都记。",
                    style = MaterialTheme.typography.bodySmall,
                    textAlign = TextAlign.Center,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }
        } else {
            LazyColumn(contentPadding = PaddingValues(vertical = 8.dp)) {
                // 不给 key：一条记录没有唯一 id，ts 会撞（同一帧的连续识别）。
                items(entries) { e -> HistoryRow(shell, e) }
            }
        }
    }
}

@Composable
private fun HistoryRow(shell: Shell, e: HistoryEntry) {
    val photoId = e.photoId
    Row(
        Modifier
            .fillMaxWidth()
            .then(
                if (photoId != null) {
                    Modifier.clickable { shell.push(Route.Detail(photoId)) }
                } else {
                    Modifier
                },
            )
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        val thumb = e.refThumbUrl
        if (thumb != null) {
            NetImage(key = thumb, modifier = Modifier.size(44.dp)) { shell.client.download(thumb) }
        } else {
            Column(
                Modifier.size(44.dp),
                verticalArrangement = Arrangement.Center,
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    text = "未命中",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }

        Column(
            Modifier
                .padding(start = 12.dp)
                .fillMaxWidth(),
        ) {
            Text(
                text = e.title ?: e.photoId ?: "没认出来",
                style = MaterialTheme.typography.bodyMedium,
                color = if (e.matched) {
                    MaterialTheme.colorScheme.onSurface
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant
                },
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                text = buildString {
                    append(Fmt.timeShort(e.ts))
                    append(" · inliers ")
                    append(e.inliers)
                    append(" · ")
                    append(e.latencyMs)
                    append("ms")
                    e.via?.let {
                        append(" · ")
                        append(it)
                    }
                },
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

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
            // bottom 88：给悬在底栏上方的「扫一扫」（76dp 的圆）留位置，
            // 否则最后一条记录被压住点不到。
            LazyColumn(
                contentPadding = PaddingValues(top = 8.dp, bottom = 88.dp),
            ) {
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
                text = e.title ?: e.photoId ?: reasonText(e),
                style = MaterialTheme.typography.bodyMedium,
                color = when {
                    e.matched -> MaterialTheme.colorScheme.onSurface
                    // ambiguous 单独标红：其余未命中是「这一帧没拍好」（下一帧就好了），
                    // 而它是「库里有两张一样的」—— 不处理的话每一帧都会这样。
                    e.ambiguous -> MaterialTheme.colorScheme.error
                    else -> MaterialTheme.colorScheme.onSurfaceVariant
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
                    // 第二名只在未命中时有意义 —— 而它恰好是 ambiguous 唯一的判据。
                    if (!e.matched) {
                        e.runnerUp?.let {
                            append(" / 第二名 ")
                            append(it)
                        }
                    }
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

/**
 * 未命中时那一行标题写什么。
 *
 * 原来一律是「没认出来」。那句话把四种毫不相干的情况归成了一句，而它们的下一步完全
 * 不同：`ambiguous` 要去清库（库里有两张一样的，不处理的话**每一帧**都会这样），
 * `weak` 是这一帧没拍好（下一帧可能就好了），`orphan` 是库和 catalog 不同步（运维），
 * `empty` 是粗排一个候选都没给（词表没训 / 库是空的）。
 */
private fun reasonText(e: HistoryEntry): String = when (e.reason) {
    "ambiguous" -> "库里有近重复，两张互相挤掉了"
    "weak" -> "内点不够（这一帧没拍好）"
    "orphan" -> "库里有、catalog 里没有"
    "empty" -> "粗排没给出候选"
    null -> "没认出来"
    else -> "没认出来（${e.reason}）"
}

package app.photoar.standalone

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.photoar.arview.FsEntry
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * NAS 文件浏览。
 *
 * 每进一层目录就 push 一页 [Route.Browse]，所以返回键就是「上一级」。服务端返回的
 * `parent` 这里不用 —— 有它反而会出现两条不一样的「回上一级」（栈里的和路径上的），
 * 两者在「从根目录列表进来」时并不重合。
 *
 * 条目顺序照抄服务端（目录优先、然后按名字），不在客户端重排：两边的排序规则一旦
 * 分叉，同一个目录在浏览器里和在服务端日志里就长得不一样了。
 */
@Composable
fun BrowseScreen(shell: Shell, route: Route.Browse) {
    val fetch = rememberFetch(route.dir) { shell.client.fsList(route.dir) }
    val scope = rememberCoroutineScope()
    var action by remember { mutableStateOf<Action>(Action.Idle) }

    fun attach(entry: FsEntry) {
        val photoId = route.photoId ?: return
        action = Action.Running("正在关联视频…")
        scope.launch {
            action = try {
                val r = withContext(Dispatchers.IO) {
                    shell.client.attachVideo(photoId, entry.path)
                }
                shell.libraryChanged()
                shell.popToDetail()
                Action.Done(r)
            } catch (e: Throwable) {
                Action.Failed(Fmt.errText(e))
            }
        }
    }

    fun onFile(entry: FsEntry) {
        when (route.pick) {
            Pick.IMAGE -> {
                shell.draft = Draft(entry.path)
                shell.push(Route.Create)
            }

            Pick.VIDEO_FOR_DRAFT -> {
                shell.draft?.videoPath = entry.path
                shell.pop()
            }

            Pick.VIDEO_FOR_PHOTO -> attach(entry)
        }
    }

    Box(Modifier.fillMaxSize()) {
        LoadFrame(fetch) { listing ->
            // 只列目录和这一趟要挑的那类文件。混着列会让「找一张照片」变成在
            // 一屏 .cr2/.txt/.db 里翻 —— 服务端 kind 已经判过，这里不该再犯。
            val wanted = if (route.pick == Pick.IMAGE) "image" else "video"
            val shown = listing.entries.filter { it.isDir || it.kind == wanted }
            val hidden = listing.entries.size - shown.size

            Column(Modifier.fillMaxSize()) {
                Text(
                    text = listing.path ?: "NAS 上开放的目录",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 6.dp),
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis,
                )
                HorizontalDivider()

                if (shown.isEmpty()) {
                    Column(
                        Modifier
                            .fillMaxSize()
                            .padding(32.dp),
                        verticalArrangement = Arrangement.Center,
                        horizontalAlignment = Alignment.CenterHorizontally,
                    ) {
                        Text(
                            text = if (hidden > 0) {
                                "这个目录里没有${if (wanted == "image") "照片" else "视频"}" +
                                    "（另有 $hidden 个其它文件）"
                            } else {
                                "空目录"
                            },
                            style = MaterialTheme.typography.bodyMedium,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                } else {
                    LazyColumn(contentPadding = PaddingValues(bottom = 24.dp)) {
                        items(shown, key = { it.path }) { e ->
                            EntryRow(shell, e) {
                                if (e.isDir) shell.push(route.copy(dir = e.path)) else onFile(e)
                            }
                        }
                        if (hidden > 0) {
                            item {
                                Text(
                                    text = "另有 $hidden 个其它类型的文件没列出来",
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                    modifier = Modifier.padding(16.dp),
                                )
                            }
                        }
                    }
                }
            }
        }

        ActionOverlay(action) { action = Action.Idle }
    }
}

@Composable
private fun EntryRow(shell: Shell, e: FsEntry, onClick: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick)
            .padding(horizontal = 12.dp, vertical = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (e.isDir) {
            Box(Modifier.size(48.dp), contentAlignment = Alignment.Center) {
                Text("📁", style = MaterialTheme.typography.titleMedium)
            }
        } else {
            // 缩略图走 /v1/fs/thumb：视频给的是第一帧（服务端 ffmpeg 抽的），
            // 这是「哪一段视频」唯一认得出来的表示。
            val key = "fs:${e.path}"
            NetImage(key = key, modifier = Modifier.size(48.dp)) { shell.client.fsThumb(e.path) }
        }

        Column(
            Modifier
                .padding(start = 12.dp)
                .fillMaxWidth(),
        ) {
            Text(
                text = e.name,
                style = MaterialTheme.typography.bodyMedium,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
            if (!e.isDir) {
                Text(
                    text = "${Fmt.bytes(e.bytes)} · ${Fmt.time(e.mtime)}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

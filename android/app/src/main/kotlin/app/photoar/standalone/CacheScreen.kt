package app.photoar.standalone

import android.content.Context
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import app.photoar.arview.Clock
import app.photoar.arview.ar.LocalTargetDb
import app.photoar.arview.cache.CacheStats
import app.photoar.arview.cache.CacheSync
import app.photoar.arview.cache.OfflineCache
import app.photoar.arview.cache.ServerTargetsStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 缓存管理（§5.8 / §15）。
 *
 * 这一页要回答三个问题，其它一概不放：
 *
 * 1. **现在能离线认出多少张** —— 不是「缓存了多少张」。有缩略图、且没被 ARCore
 *    拒过的才算，两个数在这一页上分开列，因为「怎么同步都还是差几张」的原因
 *    只能是被拒。
 * 2. **占了多少地方**，分缩略图 / 视频 / 识别库三项。视频是唯一会长到几百兆的，
 *    所以「只清视频」是主按钮而「全清」不是。
 * 3. **现在同步一次**。同步是阻塞的一长串下载，所以进度要逐条报出来 —— 没有进度
 *    的话用户会在第 40 张的时候以为卡死然后退出。
 *
 * 「只清视频」和「全清」分开是这一页最要紧的一条：缩略图是离线识别的地基（几百 KB
 * 一张），视频才占空间。清视频之后照片照样认得出，只是认出来没东西放；全清之后
 * 断网就完全用不了了。
 */
@Composable
fun CacheScreen(shell: Shell) {
    val context = LocalContext.current
    val cache = remember { OfflineCache.of(context.filesDir) }
    val prefs = remember { CachePrefs(context) }
    val scope = rememberCoroutineScope()

    val serverTargets = remember { ServerTargetsStore(cache) }

    var photos by remember { mutableStateOf(prefs.photos) }
    var videoMb by remember { mutableStateOf(prefs.videoMb) }
    var stats by remember { mutableStateOf(cache.stats()) }
    /** 本地那份服务端预建库的元数据。同步之后要重读 —— 张数和 overflow 都在里面。 */
    var prebuilt by remember { mutableStateOf(serverTargets.snapshot()) }
    // 「能离线认出多少张」按 usableAsTarget 逐条数，不用 withThumb - rejected 去减：
    // 那个减法在「被拒的那条恰好没缩略图」时会算多，而这一页最不该错的就是这个数。
    var usable by remember { mutableStateOf(cache.entries().count { it.usableAsTarget }) }
    var running by remember { mutableStateOf(false) }
    var progress by remember { mutableStateOf<String?>(null) }
    var note by remember { mutableStateOf<String?>(null) }
    var noteTone by remember { mutableStateOf(Tone.OK) }
    var confirmClearAll by remember { mutableStateOf(false) }

    fun refreshStats() {
        stats = cache.stats()
        usable = cache.entries().count { it.usableAsTarget }
        prebuilt = serverTargets.snapshot()
    }

    /**
     * 清理走 IO 线程。
     *
     * 「全清」是 `deleteRecursively` 一整个上 GB 的目录（视频上限默认 2048MB），
     * 「只清视频」是删几百个文件加写一遍索引 —— 两个都不能放主线程。放主线程不会
     * 报错，只会在慢一点的存储上直接卡出 ANR，而用户看到的是「按了没反应」。
     */
    fun clean(label: String, tone: Tone, note0: String, block: () -> Unit) {
        running = true
        note = null
        scope.launch {
            progress = label
            try {
                withContext(Dispatchers.IO) { block() }
                noteTone = tone
                note = note0
            } catch (e: Throwable) {
                noteTone = Tone.BAD
                note = "清理没成：${Fmt.errText(e)}"
            }
            running = false
            progress = null
            refreshStats()
        }
    }

    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp)
            .padding(bottom = 32.dp),
    ) {
        Section("离线可用")
        // 两个数分开列，因为它们的口径不同：预建库是服务端拿**原图**建的，能覆盖到
        // ARCore 的 1000 张上限；端上现建那份的输入是缓存里的缩略图，最多就是缓存条数。
        // 合成一个数会让「为什么我设了 200 张却能离线认出 800 张」变成没法解释的事。
        val server = prebuilt?.takeIf { it.installable && stats.serverTargetBytes > 0 }
        if (server != null) {
            KeyValue("可离线识别", "${server.count} 张（服务端预建库）")
        } else {
            KeyValue("可离线识别", "$usable 张（端上现建）")
            if (prebuilt?.rejected == true) {
                Banner(
                    "服务端预建的识别库这台手机装不上（版本不匹配，多半是服务端的 " +
                        "arcoreimg 比手机上的 ARCore 新）。已改用端上现建的那份 —— " +
                        "还能离线认，但贴合会差一点。服务端下次入库会换一版自动再试；" +
                        "更新过手机上的「ARCore」之后想立刻重试，按「全清」再同步一次。",
                    tone = Tone.WARN,
                )
            }
        }
        Fmt.overflowNote(prebuilt?.overflow ?: 0, prebuilt?.maxTargets ?: 0)?.let { Banner(it) }
        KeyValue("缓存条目", "${stats.photos} 张")
        if (stats.rejected > 0) {
            Banner(
                "有 ${stats.rejected} 张 ARCore 认不了（特征太少：纯色、糊、大面积天空）。" +
                    "它们联网时照样能扫出来 —— 服务端用原图建的库比端上现算的强。",
            )
        }
        KeyValue("已缓存视频", "${stats.withVideo} 条")

        Section("占用")
        KeyValue("缩略图", Fmt.bytes(stats.thumbBytes))
        KeyValue("视频", Fmt.bytes(stats.videoBytes))
        // 两份库分开列：稳态下只有一份非零，两个都非零说明退回过端上现建。
        KeyValue("预建识别库", Fmt.bytes(stats.serverTargetBytes))
        KeyValue("端上识别库", Fmt.bytes(stats.targetBytes))
        KeyValue("合计", Fmt.bytes(stats.totalBytes))

        Section("上限")
        Text(
            text = "留最近扫到的这么多张。排序按「本地最后扫到的时间」，" +
                "不是入库时间 —— 墙上那张天天扫的不该被刚打印的一批顶掉。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        ChipRow(
            options = CacheSettings.PHOTO_OPTIONS,
            selected = CacheSettings.selectedPhotos(photos),
            label = { "$it 张" },
            enabled = !running,
        ) {
            photos = it
            prefs.photos = it
        }
        Text(
            text = "视频预算。超了就按「最久没扫到」淘汰，只删视频、不动缩略图。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp),
        )
        ChipRow(
            options = CacheSettings.VIDEO_MB_OPTIONS,
            selected = CacheSettings.selectedVideoMb(videoMb),
            label = { if (it >= 1024) "${it / 1024} GB" else "$it MB" },
            enabled = !running,
        ) {
            videoMb = it
            prefs.videoMb = it
        }

        progress?.let {
            Text(
                text = it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 16.dp),
            )
        }
        note?.let { Banner(it, tone = noteTone, modifier = Modifier.padding(top = 12.dp)) }

        Row(
            Modifier
                .fillMaxWidth()
                .padding(top = 20.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Button(
                onClick = {
                    running = true
                    note = null
                    progress = "拉服务端列表…"
                    scope.launch {
                        val sync = CacheSync(
                            client = shell.client,
                            cache = cache,
                            clock = Clock { System.currentTimeMillis() },
                            spec = CacheSettings.spec(photos, videoMb),
                            // 建 ARCore 库要一个 ARCore Session，而这一页连相机都没开。
                            // 所以这里只把库标成过期，真正重建在下一次扫描启动时。
                            rebuildTargetDb = LocalTargetDb(cache, serverTargets).deferredRebuild(),
                            // 服务端预建库在这一步下（几 MB）。放在这里而不是扫描启动时：
                            // 那条路上用户正举着手机等画面，不能替他决定现在用流量。
                            targets = serverTargets,
                        )
                        var result: CacheSync.Result? = null
                        var failure: String? = null
                        try {
                            result = withContext(Dispatchers.IO) {
                                sync.sync { done, total, what ->
                                    // 这个回调在 IO 线程上同步触发，launch 把写 state
                                    // 搬回主线程 —— Compose 的 state 只能主线程写。
                                    scope.launch { progress = "$done / $total · $what" }
                                }
                            }
                        } catch (e: Throwable) {
                            // sync() 只有「连列表都拉不到」会抛，那一步没有部分成功可言
                            failure = Fmt.errText(e)
                        }
                        running = false
                        progress = null
                        refreshStats()
                        noteTone = if (failure == null) Tone.OK else Tone.BAD
                        note = failure?.let { "同步没成：$it" } ?: result?.let { describe(it) }
                    }
                },
                enabled = !running,
            ) {
                Text(if (running) "同步中…" else "现在同步")
            }
            OutlinedButton(
                onClick = {
                    clean(
                        "清视频…", Tone.OK,
                        "视频清了，照片照样认得出（只是认出来没东西放）",
                    ) { cache.clearVideos() }
                },
                enabled = !running,
            ) {
                Text("只清视频")
            }
            TextButton(onClick = { confirmClearAll = true }, enabled = !running) {
                Text("全清")
            }
        }

        Text(
            text = "同步只在你按这里的时候跑，没有后台自动同步：这台机器上「什么时候用流量」" +
                "该由人决定，而缓存过期一天的代价只是扫到新照片时要联网。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 20.dp),
        )
    }

    if (confirmClearAll) {
        AlertDialog(
            onDismissRequest = { confirmClearAll = false },
            title = { Text("全清缓存？") },
            text = {
                Text(
                    "清完之后断网就完全用不了了，要重新同步一次才行。" +
                        "只是想腾空间的话按「只清视频」—— 那个不影响识别。",
                )
            },
            confirmButton = {
                Button(
                    onClick = {
                        confirmClearAll = false
                        clean("全清…", Tone.WARN, "清空了。下次同步会从零重建。") {
                            cache.clearAll()
                        }
                    },
                ) {
                    Text("全清")
                }
            },
            dismissButton = {
                TextButton(onClick = { confirmClearAll = false }) { Text("算了") }
            },
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
private fun ChipRow(
    options: List<Int>,
    selected: Int,
    label: (Int) -> String,
    enabled: Boolean,
    onPick: (Int) -> Unit,
) {
    FlowRow(
        Modifier
            .fillMaxWidth()
            .padding(top = 6.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(4.dp),
    ) {
        options.forEach { v ->
            FilterChip(
                selected = v == selected,
                onClick = { onPick(v) },
                enabled = enabled,
                label = { Text(label(v)) },
            )
        }
    }
}

/**
 * 一轮同步的结果说成人话。
 *
 * [CacheSync.Result.stoppedBy] 非空要显式说「停在半路」：不说的话用户看到
 * 「下了 12 张」会以为同步完了，而实际还有 180 张没下。
 */
private fun describe(r: CacheSync.Result): String {
    val parts = ArrayList<String>()
    if (r.thumbsDownloaded > 0) parts.add("缩略图 ${r.thumbsDownloaded} 张")
    if (r.videosDownloaded > 0) parts.add("视频 ${r.videosDownloaded} 条")
    if (r.videosDropped > 0) parts.add("淘汰视频 ${r.videosDropped} 条")
    if (r.photosDropped > 0) parts.add("移出缓存 ${r.photosDropped} 张")
    val body = if (parts.isEmpty()) "已经是最新的，没什么要下的" else parts.joinToString("，")
    val size = if (r.bytesDownloaded > 0) "，共 ${Fmt.bytes(r.bytesDownloaded)}" else ""
    val failed = if (r.failed.isEmpty()) "" else "；${r.failed.size} 条没下成，下次再试"
    val stopped = r.stoppedBy?.let { "；停在半路：$it" } ?: ""
    // 预建库那一步单独一句：它是离线识别的主力，成没成必须直说 —— 而它失败时其余部分
    // 是全好的，混进上面那串「下了几张」里会看不见。
    val prebuilt = if (r.prebuilt.status == CacheSync.TargetsStatus.SKIPPED) {
        ""
    } else {
        "。" + Fmt.prebuiltStatus(r.prebuilt)
    }
    return body + size + failed + stopped + prebuilt
}

/**
 * 两个上限的落盘。
 *
 * 单独一个 prefs 文件，不跟 endpoint 那份混在一起：那份是「连得上吗」，这份是
 * 「存多少」，两者被清掉的后果完全不同（前者要重填地址，后者只是回默认值）。
 */
private class CachePrefs(context: Context) {

    private val prefs =
        context.applicationContext.getSharedPreferences("photoar_cache", Context.MODE_PRIVATE)

    var photos: Int
        get() = prefs.getInt(KEY_PHOTOS, CacheSettings.DEFAULT_PHOTOS)
        set(v) = prefs.edit().putInt(KEY_PHOTOS, v).apply()

    var videoMb: Int
        get() = prefs.getInt(KEY_VIDEO_MB, CacheSettings.DEFAULT_VIDEO_MB)
        set(v) = prefs.edit().putInt(KEY_VIDEO_MB, v).apply()

    private companion object {
        const val KEY_PHOTOS = "max_photos"
        const val KEY_VIDEO_MB = "max_video_mb"
    }
}

/** 给设置页那一行用的一句话摘要。 */
fun cacheSummary(stats: CacheStats): String =
    "可离线识别 ${stats.withThumb - stats.rejected} 张 · ${Fmt.bytes(stats.totalBytes)}"

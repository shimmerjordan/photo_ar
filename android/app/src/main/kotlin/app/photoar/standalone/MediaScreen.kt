package app.photoar.standalone

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import app.photoar.standalone.pixel.Button
import androidx.compose.material3.FilterChip
import app.photoar.standalone.pixel.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import app.photoar.standalone.pixel.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import app.photoar.standalone.pixel.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import app.photoar.arview.PhotoDetail
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 素材：把手机里的**一张照片 + 一段视频**传上去，一步建成一组映射。
 *
 * ## 为什么是「一组」而不是两个独立的上传
 *
 * 改造前这一页有两个按钮，各自把文件传到 NAS 的收件目录，然后人要自己去别处入库、再去
 * 管理台把视频配给照片 —— 三个地方、三步。而这三步之间没有任何选择：传上来的这张照片和
 * 这段视频**本来就是一对**（这正是这个 App 存在的意义）。所以现在一次操作走完：
 *
 *   传照片 → 传视频 → `POST /v1/photo {refPath, videoPath}` → 一组映射
 *
 * 视频允许留空（先把照片入库，视频晚点在下面的历史里补），但界面默认要求两个都挑 ——
 * 「传了照片但忘了配视频」的后果是扫到它什么都不播，而那时人已经不在电脑前了。
 *
 * ## 为什么没有「浏览 NAS」
 *
 * 拿掉了。这一页的素材来源是**手机相册**（婚礼当天刚拍的东西在手机里），而 NAS 上已有的
 * 文件走管理台的批量导入那条路 —— 那边有完整的路径校验和预演。App 里再放一个文件浏览器
 * 只是同一件事的第二个入口，而它还得自己处理白名单、类型判断、缩略图。
 */
@Composable
fun MediaScreen(shell: Shell) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val history = remember { UploadHistory(context) }
    val allowed = shell.center.uploadAllowed()

    var photoUri by remember { mutableStateOf<Uri?>(null) }
    var videoUri by remember { mutableStateOf<Uri?>(null) }
    var title by remember { mutableStateOf("") }
    // 打印尺寸。默认「不知道」，但填了能让 ARCore 一认出图案就贴上（见 [PrintSize]）。
    var printSize by remember { mutableStateOf(PrintSize.UNKNOWN) }
    var busy by remember { mutableStateOf(false) }
    var stage by remember { mutableStateOf("") }
    var sent by remember { mutableStateOf(0L) }
    var total by remember { mutableStateOf(0L) }
    var note by remember { mutableStateOf<String?>(null) }
    var bad by remember { mutableStateOf(false) }
    // 历史列表的版本号，改完之后 +1 触发重取。
    var historyRev by remember { mutableStateOf(0) }
    // 撞上已入库的照片时，把「那张照片现在是什么样、能做什么」摊在界面上。
    // null = 没有待处理的重复。
    var dup by remember { mutableStateOf<DuplicatePlan.Outcome?>(null) }
    // 这一趟已经跑了多少秒。入库阶段服务端不给进度，秒表是唯一能如实给出的东西。
    var elapsedSec by remember { mutableStateOf(0) }

    // busy 期间每秒 +1。`LaunchedEffect(busy)` 在 busy 变回 false 时自动取消，
    // 不需要手动停。
    LaunchedEffect(busy) {
        elapsedSec = 0
        while (busy) {
            kotlinx.coroutines.delay(1000)
            elapsedSec++
        }
    }

    val pickPhoto = rememberLauncherForActivityResult(
        ActivityResultContracts.PickVisualMedia(),
    ) { uri -> uri?.let { photoUri = it; note = null } }

    val pickVideo = rememberLauncherForActivityResult(
        ActivityResultContracts.PickVisualMedia(),
    ) { uri -> uri?.let { videoUri = it; note = null } }

    /**
     * 传一个文件，返回它在服务端的绝对路径。进度写进外面那两个 state。
     *
     * **先算哈希问一次服务端**，已经有了就整个跳过上传。原来要等 20 MB 传完才收到一句
     * 「已存在」，而手机上那是几十秒的白等；算哈希是本地读一遍文件（实测一张 2.7 MB 的
     * 手机照片约 30 ms），换掉那几十秒。
     *
     * 校验失败时**继续传**而不是中止：那一步只是优化，服务端在真正落地时还会再挡一道
     * （同名同内容复用、不同内容拒绝）。因为一次可选的优化而让整条上传路径失败是错的。
     */
    suspend fun uploadOne(uri: Uri, what: String): String {
        val meta = queryMeta(context, uri)
        val mime = context.contentResolver.getType(uri) ?: "application/octet-stream"

        stage = "检查$what 是不是已经传过…"
        sent = 0L
        total = 0L
        val check = runCatching {
            val sha = sha256Of(context, uri)
            shell.client.uploadCheck(meta.name, sha)
        }.getOrNull()
        check?.reusablePath?.let { path ->
            stage = "$what 服务端已经有了，跳过上传"
            return path
        }

        stage = "上传$what…"
        sent = 0L
        total = meta.bytes
        // 撞名但内容不同：服务端会拒。用它给的建议名，别让用户自己去改相册里的文件名。
        val name = if (check != null && check.nameTaken && !check.sameContent) {
            check.suggestedName ?: meta.name
        } else {
            meta.name
        }
        return shell.client.upload(name, mime, meta.bytes) { out ->
            context.contentResolver.openInputStream(uri)!!.use { input ->
                val buf = ByteArray(1 shl 16)
                while (true) {
                    val n = input.read(buf)
                    if (n <= 0) break
                    out.write(buf, 0, n)
                    sent += n
                }
            }
        }
    }

    /**
     * 入库撞上「这张照片已经入库了」时的处理。
     *
     * 服务端拦下重复是对的，但**不该是死胡同**：这里把那张已有照片查出来，连它现在配的
     * 视频一起摊在界面上，并给出唯一可做的那件事（换/补视频 —— 一张照片只能配一段）。
     * 判断哪种情形、说什么话由 [DuplicatePlan] 决定（纯逻辑，有测试）。
     */
    suspend fun handleDuplicate(refPath: String, videoPath: String?, e: Throwable) {
        val code = (e as? app.photoar.arview.net.HttpFailure)?.code
        if (code != "already_ingested") {
            note = Fmt.errText(e)
            bad = true
            return
        }
        val looked = runCatching {
            withContext(Dispatchers.IO) { shell.client.lookup(refPath) }
        }.getOrNull()
        if (looked == null) {
            // 反查也失败了（多半是网络）。别把原始错误吞掉。
            note = Fmt.errText(e)
            bad = true
            return
        }
        dup = DuplicatePlan.of(looked, videoPath)
        // 顺手把它加进上传历史：这张照片确实在库里，而用户刚刚表达了「我要管它」。
        // 不加的话他得靠管理台才能看到自己刚碰过的这一张。
        looked.photo?.let {
            history.add(
                UploadHistory.Entry(
                    photoId = it.photoId,
                    photoName = refPath.substringAfterLast('/'),
                    videoName = it.videoPath?.substringAfterLast('/') ?: "",
                    title = it.title ?: "",
                    at = System.currentTimeMillis(),
                ),
            )
            historyRev++
        }
    }

    fun submitPair() {
        val p = photoUri ?: return
        busy = true
        note = null
        bad = false
        dup = null
        scope.launch {
            var refPathOut: String? = null
            var videoPathOut: String? = null
            val outcome = runCatching {
                withContext(Dispatchers.IO) {
                    val photoName = queryMeta(context, p).name
                    val refPath = uploadOne(p, "照片")
                    refPathOut = refPath
                    val videoName = videoUri?.let { queryMeta(context, it).name } ?: ""
                    val videoPath = videoUri?.let { uploadOne(it, "视频") }
                    videoPathOut = videoPath
                    stage = "入库并建立映射…（要跑特征提取，可能几十秒）"
                    total = 0L
                    val created = shell.client.createPhoto(
                        refPath = refPath,
                        videoPath = videoPath,
                        // 0 = 未知，交给 ARCore 自己量（代价是要用户晃一下手机）。
                        // 知道的话务必填 —— 理由见 [PrintSize] 那段。
                        printWidthMm = printSize.widthMm,
                        title = title.ifBlank { null },
                    )
                    history.add(
                        UploadHistory.Entry(
                            photoId = created.photoId,
                            photoName = photoName,
                            videoName = videoName,
                            title = title.trim(),
                            at = System.currentTimeMillis(),
                        ),
                    )
                    created
                }
            }
            busy = false
            stage = ""
            outcome.fold(
                onSuccess = {
                    note = if (videoUri != null) {
                        "成了：照片已入库，视频已配上。质量分 ${it.qualityScore}。"
                    } else {
                        "照片已入库（质量分 ${it.qualityScore}），但**还没配视频** —— " +
                            "在下面的历史里点「配视频」补上，否则扫到它不会播任何东西。"
                    }
                    bad = false
                    photoUri = null
                    videoUri = null
                    title = ""
                    printSize = PrintSize.UNKNOWN
                    historyRev++
                    shell.libraryChanged()
                },
                onFailure = { e ->
                    val ref = refPathOut
                    if (ref != null) {
                        handleDuplicate(ref, videoPathOut, e)
                    } else {
                        // 照片都还没传上去就失败了（多半是上传本身出错），没有可反查的路径。
                        note = Fmt.errText(e)
                        bad = true
                    }
                },
            )
        }
    }

    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp)
            // 88：底栏上那颗「扫一扫」不算进 Scaffold 的 innerPadding。
            .padding(bottom = 88.dp),
    ) {
        Section("传一组")
        Text(
            text = "挑一张照片和配它的那段视频，一次传完就是一组映射。" +
                "婚礼当天刚拍的素材在手机里，而管理台跑在 NAS 上看不到它们。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        if (!allowed) {
            Banner(
                "现在不能上传：media 通道走的是隧道（有 100MB 请求体上限），" +
                    "或者还没探活完。连回家里的网络、或开 Tailscale 之后再来。",
                Tone.WARN,
            )
        }

        PickRow(
            label = "照片",
            picked = photoUri?.let { nameOf(context, it) },
            required = true,
            enabled = allowed && !busy,
            onPick = {
                pickPhoto.launch(
                    PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly),
                )
            },
            onClear = { photoUri = null },
        )
        PickRow(
            label = "视频",
            picked = videoUri?.let { nameOf(context, it) },
            required = false,
            enabled = allowed && !busy,
            onPick = {
                pickVideo.launch(
                    PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.VideoOnly),
                )
            },
            onClear = { videoUri = null },
        )

        OutlinedTextField(
            value = title,
            onValueChange = { title = it },
            label = { Text("标题（可留空）") },
            singleLine = true,
            enabled = !busy,
            modifier = Modifier
                .fillMaxWidth()
                .padding(top = 10.dp),
        )
        Text(
            text = "留空时服务端拿文件名当标题。标题只影响管理台和「保存到相册」的文件名。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 4.dp),
        )

        PrintSizePicker(printSize, enabled = !busy) { printSize = it }

        Button(
            onClick = { submitPair() },
            enabled = allowed && !busy && photoUri != null,
            modifier = Modifier.padding(top = 14.dp),
        ) {
            Text(if (busy) "处理中…" else "传上去并建立映射")
        }

        if (busy) {
            Text(
                text = stage + if (total > 0) "  ${Fmt.bytes(sent)} / ${Fmt.bytes(total)}" else "",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(top = 10.dp),
            )
            if (total > 0) {
                // 上传阶段：字节数是真的，画确定进度。
                LinearProgressIndicator(
                    progress = { (sent.toDouble() / total).toFloat().coerceIn(0f, 1f) },
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 6.dp),
                )
            } else {
                // 入库/建映射阶段：服务端**不给进度**（那是一次同步调用，中途没有可上报
                // 的百分比），所以画不确定进度条 + 秒表。
                //
                // 不编一个假百分比：一根匀速走到 90% 然后停住的条比没有条更糟 —— 它会让
                // 人以为快完了，然后开始怀疑是不是卡死。而「转着 + 已经 12 秒」如实说明
                // 「在干活、干了多久」，那正是此刻唯一能说的两件事。
                LinearProgressIndicator(
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 6.dp),
                )
                Text(
                    text = "已经 ${elapsedSec} 秒" + if (elapsedSec >= 20) {
                        "（照片大的话要久一些，别退出这一页）"
                    } else {
                        ""
                    },
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }
        }

        note?.let { Banner(it, if (bad) Tone.BAD else Tone.OK) }

        dup?.let { outcome ->
            DuplicateCard(
                shell = shell,
                outcome = outcome,
                onDone = { msg ->
                    dup = null
                    note = msg
                    bad = false
                    photoUri = null
                    videoUri = null
                    title = ""
                    historyRev++
                    Thumbs.clear()
                    shell.libraryChanged()
                },
                onDismiss = { dup = null },
            )
        }

        UploadHistorySection(shell, history, historyRev) { historyRev++ }
    }
}

/**
 * 撞上已入库照片时那张卡。
 *
 * 显示「那张照片现在是什么样」，以及唯一可做的那件事。动作是 null 时只有一个「知道了」
 * —— 那种情形（比如同一张照片配同一段视频再传一遍）本来就没什么要改的，摆一个按钮
 * 只会让人以为漏了一步。
 */
@Composable
private fun DuplicateCard(
    shell: Shell,
    outcome: DuplicatePlan.Outcome,
    onDone: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    val scope = rememberCoroutineScope()
    var busy by remember(outcome) { mutableStateOf(false) }
    var err by remember(outcome) { mutableStateOf<String?>(null) }
    // 二次确认：换视频是不可撤销的（旧关联没了），而这张卡是在一次失败之后弹出来的，
    // 人此刻的注意力不在「我要替换什么」上。
    var confirming by remember(outcome) { mutableStateOf(false) }

    fun run(photoId: String, videoPath: String, verb: String) {
        busy = true
        err = null
        scope.launch {
            val r = runCatching {
                withContext(Dispatchers.IO) { shell.client.attachVideo(photoId, videoPath) }
            }
            busy = false
            r.fold(
                onSuccess = { onDone("已经把这张照片的视频${verb}了。") },
                onFailure = { err = Fmt.errText(it) },
            )
        }
    }

    Banner(outcome.message, Tone.WARN)
    val action = outcome.action
    if (action != null) {
        val (photoId, videoPath, confirm, verb) = when (action) {
            is DuplicatePlan.Action.ReplaceVideo ->
                Quad(action.photoId, action.videoPath, action.confirm, "换成新的")
            is DuplicatePlan.Action.AttachVideo ->
                Quad(action.photoId, action.videoPath, action.confirm, "配上")
        }
        if (confirming) {
            Text(
                text = confirm,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 8.dp),
            )
        }
        Row(
            Modifier.padding(top = 8.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Button(
                onClick = {
                    if (confirming) run(photoId, videoPath, verb) else confirming = true
                },
                enabled = !busy,
            ) {
                Text(
                    when {
                        busy -> "处理中…"
                        confirming -> "确认$verb"
                        else -> verb
                    },
                )
            }
            OutlinedButton(onClick = onDismiss, enabled = !busy) { Text("不用了") }
        }
    } else {
        OutlinedButton(onClick = onDismiss, modifier = Modifier.padding(top = 8.dp)) {
            Text("知道了")
        }
    }
    err?.let { Banner(it, Tone.BAD) }
}

/** 只为上面那个 `when` 一次性解构用。Kotlin 的标准库没有四元组。 */
private data class Quad(
    val a: String,
    val b: String,
    val c: String,
    val d: String,
)

/**
 * 打印尺寸。一排可点的小标签，不是下拉框。
 *
 * 下拉框要点两次（展开、选），而这里最常见的操作是「点一下 6寸横 就走」。七个选项一排
 * 换行放得下，而且**所有选项一眼都在**——下拉框会把「不知道」以外的选项藏起来，于是
 * 大多数人根本不知道有得选，也就永远走那条要晃手机的路。
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun PrintSizePicker(
    selected: PrintSize,
    enabled: Boolean,
    onPick: (PrintSize) -> Unit,
) {
    Text(
        text = "照片印出来有多宽？",
        style = MaterialTheme.typography.labelLarge,
        modifier = Modifier.padding(top = 14.dp),
    )
    Text(
        // 这句是整个改动的重点：告诉用户填它的**好处**，而不只是「可选」。
        // 不填不是错，但会让扫描时多一步（晃手机），而那一步是「认出来了却贴不上」
        // 的最常见原因。
        text = "填了的话，扫的时候一认出来就能贴上。不填也行，但那时 ARCore 要靠你" +
            "轻轻晃动手机才能量出照片有多大。",
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(top = 2.dp),
    )
    FlowRow(
        Modifier
            .fillMaxWidth()
            .padding(top = 8.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        for (size in PrintSize.ORDER) {
            val on = size == selected
            FilterChip(
                selected = on,
                onClick = { onPick(size) },
                enabled = enabled,
                label = { Text(size.label) },
            )
        }
    }
    Text(
        text = selected.hint,
        style = MaterialTheme.typography.labelSmall,
        color = if (selected.known) {
            MaterialTheme.colorScheme.onSurfaceVariant
        } else {
            // 「不知道」那条提示带着一个代价，用强调色让它被读到。
            MaterialTheme.colorScheme.primary
        },
        modifier = Modifier.padding(top = 6.dp),
    )
}

/** 「照片 / 视频」那两行：没挑时是一个按钮，挑了之后显示文件名加一个清除。 */
@Composable
private fun PickRow(
    label: String,
    picked: String?,
    required: Boolean,
    enabled: Boolean,
    onPick: () -> Unit,
    onClear: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(top = 10.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        OutlinedButton(onClick = onPick, enabled = enabled) {
            Text(if (picked == null) "挑$label" else "换$label")
        }
        if (picked == null) {
            Text(
                text = if (required) "必填" else "可以晚点在历史里补",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            Text(
                text = picked,
                style = MaterialTheme.typography.bodySmall,
                maxLines = 2,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = onClear, enabled = enabled) { Text("清除") }
        }
    }
}

/**
 * 上传历史。
 *
 * 只显示**这台手机传上去的**那些组（本地记的 photoId），每一条的当前状态现取
 * `/v1/photo/<id>` —— 存下来就会和服务端不一致，而人分不清「界面是旧的」和「服务端
 * 真的还是旧的」。
 */
@Composable
private fun UploadHistorySection(
    shell: Shell,
    history: UploadHistory,
    rev: Int,
    onChanged: () -> Unit,
) {
    val entries = remember(rev) { history.all() }

    Section("上传历史")
    if (entries.isEmpty()) {
        Text(
            text = "还没有传过。这里会列出这台手机传上去的每一组，可以在这儿换照片或换视频。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        return
    }
    Text(
        text = "${entries.size} 组。只记这台手机传的，换了手机看不到 —— " +
            "全库的映射在管理台的「照片」页。",
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
    for (e in entries) {
        HistoryRow(shell, history, e, onChanged)
    }
}

@Composable
private fun HistoryRow(
    shell: Shell,
    history: UploadHistory,
    entry: UploadHistory.Entry,
    onChanged: () -> Unit,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var busy by remember { mutableStateOf(false) }
    var msg by remember { mutableStateOf<String?>(null) }
    var bad by remember { mutableStateOf(false) }
    // 服务端当前的样子。null = 还在取。
    var detail by remember(entry.photoId) {
        mutableStateOf<PhotoDetail?>(null)
    }
    var gone by remember(entry.photoId) { mutableStateOf(false) }
    var reload by remember(entry.photoId) { mutableStateOf(0) }

    LaunchedEffect(entry.photoId, reload) {
        val r = runCatching {
            withContext(Dispatchers.IO) { shell.client.photoDetail(entry.photoId) }
        }
        r.fold(onSuccess = { detail = it }, onFailure = { gone = true })
    }

    /** 传一个文件上去，然后用它换掉这一条的照片或视频。 */
    fun swap(uri: Uri, isPhoto: Boolean) {
        busy = true
        msg = null
        bad = false
        scope.launch {
            val outcome = runCatching {
                withContext(Dispatchers.IO) {
                    val meta = queryMeta(context, uri)
                    val mime = context.contentResolver.getType(uri)
                        ?: "application/octet-stream"
                    val path = shell.client.upload(meta.name, mime, meta.bytes) { out ->
                        context.contentResolver.openInputStream(uri)!!.use { input ->
                            input.copyTo(out, 1 shl 16)
                        }
                    }
                    if (isPhoto) {
                        // 换参考图：photoId 不变，所以授权、配的视频、标题全都留着。
                        shell.client.replaceRef(entry.photoId, path)
                        history.update(entry.photoId, photoName = meta.name)
                    } else {
                        shell.client.attachVideo(entry.photoId, path)
                        history.update(entry.photoId, videoName = meta.name)
                    }
                    meta.name
                }
            }
            busy = false
            outcome.fold(
                onSuccess = {
                    msg = if (isPhoto) "照片换成 $it 了。" else "视频换成 $it 了。"
                    bad = false
                    reload++
                    onChanged()
                    Thumbs.clear()
                    shell.libraryChanged()
                },
                onFailure = { e ->
                    msg = Fmt.errText(e)
                    bad = true
                },
            )
        }
    }

    val pickPhoto = rememberLauncherForActivityResult(
        ActivityResultContracts.PickVisualMedia(),
    ) { uri -> uri?.let { swap(it, isPhoto = true) } }
    val pickVideo = rememberLauncherForActivityResult(
        ActivityResultContracts.PickVisualMedia(),
    ) { uri -> uri?.let { swap(it, isPhoto = false) } }

    val allowed = shell.center.uploadAllowed()

    Column(Modifier.padding(top = 14.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            val thumbUrl = "/v1/photo/${entry.photoId}/thumb"
            NetImage(
                key = thumbUrl,
                modifier = Modifier.size(width = 56.dp, height = 42.dp),
            ) { shell.client.download(thumbUrl) }
            Column(Modifier.padding(start = 10.dp)) {
                Text(
                    text = entry.title.ifBlank { detail?.title ?: entry.photoName },
                    style = MaterialTheme.typography.bodyMedium,
                    maxLines = 1,
                )
                Text(
                    text = historyLine(entry, detail, gone),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 2,
                )
            }
        }
        Row(
            Modifier.padding(top = 6.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            OutlinedButton(
                onClick = {
                    pickPhoto.launch(
                        PickVisualMediaRequest(
                            ActivityResultContracts.PickVisualMedia.ImageOnly,
                        ),
                    )
                },
                enabled = allowed && !busy && !gone,
            ) {
                Text("换照片")
            }
            OutlinedButton(
                onClick = {
                    pickVideo.launch(
                        PickVisualMediaRequest(
                            ActivityResultContracts.PickVisualMedia.VideoOnly,
                        ),
                    )
                },
                enabled = allowed && !busy && !gone,
            ) {
                Text(if (detail?.videoPath == null) "配视频" else "换视频")
            }
            OutlinedButton(
                onClick = { shell.push(Route.Play(entry.photoId)) },
                enabled = !gone && detail?.videoPath != null,
            ) {
                Text("试播")
            }
        }
        if (busy) {
            Text(
                text = "上传并处理中…（换照片要重算特征，可能几十秒）",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.padding(top = 4.dp),
            )
        }
        msg?.let { Banner(it, if (bad) Tone.BAD else Tone.OK) }
    }
}

/**
 * 历史里每条的第二行。
 *
 * 抽成纯函数是为了让「照片在服务端已经没了」这个分支看得见 —— 它会发生（另一个管理员
 * 在管理台上删了，或者换了服务端），而写成一句「加载失败」会让人以为是网络问题然后
 * 一直重试。
 */
internal fun historyLine(
    entry: UploadHistory.Entry,
    detail: PhotoDetail?,
    gone: Boolean,
): String {
    if (gone) return "服务端上找不到这张照片了（可能已被删除，或者换了服务端）"
    if (detail == null) return "读取中…"
    val video = detail.videoPath?.substringAfterLast('/')
        ?: entry.videoName.ifBlank { null }
    return buildString {
        append(entry.photoName.ifBlank { "照片" })
        append(" · ")
        if (detail.videoPath != null) {
            append("视频 ${video}")
        } else {
            append("**还没配视频**")
        }
        append(" · ")
        append(Fmt.dateTime(entry.at))
    }
}

/**
 * 算一个 content:// 的 SHA-256。
 *
 * 用 SHA-256 而不是 MD5：服务端的 `asset.sha256` 那一列存的就是它，两边必须是同一个
 * 算法才能比。（MD5 在这里也够用 —— 这不是安全用途，只是内容标识 —— 但那样服务端就得
 * 再存一列，而 sha256 已经在那儿了。）
 *
 * 分块读，不把文件读进内存：视频可能几百 MB。实测一张 2.7 MB 的手机照片约 30 ms，
 * 一段 20 MB 的视频约 200 ms —— 相对它省下的那次上传可以忽略。
 */
private fun sha256Of(context: Context, uri: Uri): String {
    val digest = java.security.MessageDigest.getInstance("SHA-256")
    context.contentResolver.openInputStream(uri)!!.use { input ->
        val buf = ByteArray(1 shl 16)
        while (true) {
            val n = input.read(buf)
            if (n <= 0) break
            digest.update(buf, 0, n)
        }
    }
    // 服务端按 64 位**小写**十六进制校验（`_upload_check` 里那个正则）。
    return digest.digest().joinToString("") { "%02x".format(it) }
}

private class UploadMeta(val name: String, val bytes: Long)

private fun nameOf(context: Context, uri: Uri): String =
    runCatching { queryMeta(context, uri).name }.getOrElse { "(读不到文件名)" }

/**
 * 从 content:// 取文件名和字节数。
 *
 * 字节数**必须**准确：服务端只按 Content-Length 读体，数错了会挂住而不是报错。
 * 取不到 SIZE 时不能猜 —— 所以这里直接抛，让它变成一句能读的错误。
 */
private fun queryMeta(context: Context, uri: Uri): UploadMeta {
    context.contentResolver.query(uri, null, null, null, null)?.use { c ->
        if (c.moveToFirst()) {
            val nameIdx = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
            val sizeIdx = c.getColumnIndex(OpenableColumns.SIZE)
            val name = if (nameIdx >= 0) c.getString(nameIdx) else null
            val size = if (sizeIdx >= 0 && !c.isNull(sizeIdx)) c.getLong(sizeIdx) else -1L
            if (name != null && size >= 0) {
                return UploadMeta(sanitizeName(name), size)
            }
        }
    }
    throw IllegalStateException(
        "读不到这个文件的名字和大小，没法上传。" +
            "换一个来源试试（有些云盘类应用提供的条目不带这两项）。",
    )
}

/**
 * 文件名清一遍，对齐服务端 `_upload` 的规则：纯文件名、不能以点开头。
 *
 * 服务端那边会拒掉不合规的名字（400 `bad_name`），在这里先清是为了别让用户因为
 * 相册里一个带斜杠的文件名而完全传不上去。
 */
private fun sanitizeName(raw: String): String {
    val base = raw.substringAfterLast('/').substringAfterLast('\\')
    val cleaned = base.trim().trimStart('.')
    return cleaned.ifEmpty { "upload.bin" }
}

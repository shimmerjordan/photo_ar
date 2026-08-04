package app.photoar.standalone

import android.view.SurfaceView
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import app.photoar.standalone.pixel.Button
import androidx.compose.material3.MaterialTheme
import app.photoar.standalone.pixel.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import app.photoar.arview.MediaInfo
import app.photoar.arview.media.VideoPlayer
import kotlinx.coroutines.delay
import java.util.Locale

/**
 * 试播：把这张照片配的视频全屏放一遍，**不开相机**。
 *
 * 这一页回答的问题和扫描页不一样。扫描页答的是「贴得准不准」，需要相机、需要
 * ARCore、需要手里真有一张打印件；试播答的是「这张照片配的到底是不是那段视频」，
 * 而这个问题在刚入库完那一刻就想确认，那时人在电脑前，手里什么都没有。
 *
 * 实现上是复用 [VideoPlayer] 的 SurfaceView 重载 —— 它本来是 §5.8 写的「没有
 * ARCore 时的全屏兜底」，在这里顺带被日常路径走到了，等于那条兜底一直有人验。
 *
 * 播放链路和 AR 模式完全同一条（同一个 ExoPlayer、同一个 `Authorization` 头、同一个
 * `/v1/asset/{id}/stream` 的 206），所以这里播得出来就意味着「照片 → 索引 → 视频」
 * 整条路是通的，剩下的只有 ARCore 的位姿。
 */
@Composable
fun PlayScreen(shell: Shell, photoId: String) {
    val fetch = rememberFetch(photoId, shell.libraryRev) { shell.client.mediaOfPhoto(photoId) }
    LoadFrame(fetch) { media -> PlayBody(shell, media) }
}

@Composable
private fun PlayBody(shell: Shell, media: MediaInfo) {
    val context = LocalContext.current

    // 服务端说不能播就到此为止：再往下 prepare 只会得到一个 ExoPlayer 的错误码，
    // 而 reason 是人话（「视频文件不见了」之类）。
    val url = media.resolvedUrl(shell.center.endpoints())
    if (!media.playable || url == null) {
        Column(
            Modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Banner("播不了：" + (media.reason ?: if (media.missing) "视频文件不在了" else "没有视频"), Tone.BAD)
        }
        return
    }

    var aspect by remember { mutableStateOf(16f / 9f) }
    var duration by remember { mutableStateOf(media.durationMs ?: 0L) }
    var position by remember { mutableStateOf(0L) }
    var playing by remember { mutableStateOf(false) }
    var ended by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    // 回调都是 ExoPlayer 在主线程发的（Player.Listener 的约定），可以直接写 state。
    val player = remember {
        VideoPlayer(
            context = context,
            endpoints = { shell.center.endpoints() },
            onReady = { d -> if (d > 0) duration = d },
            onEnded = {
                ended = true
                playing = false
            },
            onError = { msg -> error = msg },
            onVideoSize = { w, h -> if (h > 0) aspect = w.toFloat() / h },
        )
    }

    // 离开这一页必须 release：ExoPlayer 持着解码器和 AudioTrack，留着就是后台
    // 一直在出声（返回键回到详情页时最明显）。
    DisposableEffect(player) { onDispose { player.release() } }

    LaunchedEffect(url) {
        player.prepare(url)
        player.play()
        playing = true
    }

    // 进度自己轮询：ExoPlayer 不发 position 变化的回调，而没有进度条的话
    // 「到底在放还是卡住了」分不出来 —— 这一页存在的意义就是回答这个。
    //
    // 读 wantsToPlay 不读 isPlaying：后者在缓冲期是 false，会把按钮从「暂停」翻回
    // 「播放」，也会盖掉点击时刚设上的乐观状态（见 [VideoPlayer.wantsToPlay]）。
    LaunchedEffect(player) {
        while (true) {
            delay(250)
            position = player.positionMs
            playing = player.wantsToPlay
        }
    }

    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp)
            // 88：给悬在底栏上的「扫一扫」留位置（详见 [PhotosScreen]）。
            .padding(bottom = 88.dp),
    ) {
        Box(
            Modifier
                .fillMaxWidth()
                .padding(top = 12.dp)
                .aspectRatio(aspect)
                // 黑底：视频还没出第一帧时 SurfaceView 是透明的，透出来的是页面
                // 背景色，看着像「白框里什么都没有」。
                .background(Color.Black),
        ) {
            AndroidView(
                factory = { ctx -> SurfaceView(ctx).also { player.attach(it) } },
                modifier = Modifier.fillMaxSize(),
            )
        }

        error?.let { Banner("播放出错：$it", Tone.BAD) }

        Row(
            Modifier
                .fillMaxWidth()
                .padding(top = 12.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Button(
                onClick = {
                    when {
                        // 播完了再按就是重播：ExoPlayer 停在末尾，直接 play() 不动，
                        // 得先回到 0（[VideoPlayer.restart]）。
                        ended -> {
                            player.restart()
                            ended = false
                            playing = true
                        }
                        playing -> {
                            player.pause()
                            playing = false
                        }
                        else -> {
                            player.play()
                            playing = true
                        }
                    }
                },
            ) {
                Text(
                    when {
                        ended -> "重播"
                        playing -> "暂停"
                        else -> "播放"
                    },
                )
            }
            Text(
                text = clock(position) + " / " + clock(duration),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }

        Section("这段视频")
        KeyValue("大小", Fmt.bytes(media.bytes))
        KeyValue("时长", media.durationMs?.let { clock(it) } ?: "—")
        KeyValue("通道", media.via ?: "—")
        KeyValue("断点续传", if (media.supportsRange) "支持（Range）" else "不支持")
        KeyValue("NAS 路径", media.nasPath ?: "—")
        if (DebugMode.enabled) {
            KeyValue("URL", url)
        }

        Text(
            text = "这里播得出来，说明「照片 → 索引 → 视频」整条链路是通的；" +
                "AR 里还剩一步位姿贴合，那步要相机。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp),
        )

        OutlinedButton(
            onClick = { shell.pop() },
            modifier = Modifier.padding(top = 12.dp),
        ) {
            Text("返回详情")
        }
    }
}

/** mm:ss。视频都是十几秒到一两分钟（§8.1 的转码上限 30 秒），不做小时位。 */
private fun clock(ms: Long): String {
    val total = (ms / 1000).coerceAtLeast(0)
    return String.format(Locale.US, "%d:%02d", total / 60, total % 60)
}

package app.photoar.standalone

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.ui.graphics.RectangleShape
import androidx.compose.material3.AlertDialog
import app.photoar.standalone.pixel.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 带令牌的网络图。
 *
 * [key] 同时是缓存键和「要不要重新加载」的判据，传相对 URL 就行。[load] 是阻塞的，
 * 这里负责切线程；一张图失败只让这一格变成「无图」，不影响整屏。
 */
@Composable
fun NetImage(
    key: String?,
    modifier: Modifier = Modifier,
    contentScale: ContentScale = ContentScale.Crop,
    onSize: ((Int, Int) -> Unit)? = null,
    load: () -> ByteArray,
) {
    var bmp by remember(key) { mutableStateOf(key?.let { Thumbs.cached(it) }) }
    var failed by remember(key) { mutableStateOf(false) }

    LaunchedEffect(key) {
        val k = key ?: return@LaunchedEffect
        if (bmp != null) {
            bmp?.let { onSize?.invoke(it.width, it.height) }
            return@LaunchedEffect
        }
        val b = try {
            withContext(Dispatchers.IO) { Thumbs.load(k) { load() } }
        } catch (e: Throwable) {
            null
        }
        if (b == null) failed = true else {
            bmp = b
            onSize?.invoke(b.width, b.height)
        }
    }

    Box(
        modifier
            .clip(RectangleShape)
            .background(MaterialTheme.colorScheme.surfaceVariant),
        contentAlignment = Alignment.Center,
    ) {
        val b = bmp
        if (b != null) {
            Image(
                bitmap = b.asImageBitmap(),
                contentDescription = null,
                modifier = Modifier.fillMaxSize(),
                contentScale = contentScale,
            )
        } else {
            Text(
                text = if (failed) "无图" else "…",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** 三态外框。出错时给重试按钮 —— 弱网下这是最常按的一个键。 */
@Composable
fun <T> LoadFrame(
    fetch: Fetch<T>,
    modifier: Modifier = Modifier,
    content: @Composable (T) -> Unit,
) {
    when (val s = fetch.state) {
        is Load.Loading -> Box(modifier.fillMaxSize(), Alignment.Center) {
            CircularProgressIndicator()
        }

        is Load.Fail -> Column(
            modifier
                .fillMaxSize()
                .padding(24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = s.message,
                textAlign = TextAlign.Center,
                color = MaterialTheme.colorScheme.error,
            )
            Spacer(Modifier.padding(8.dp))
            Button(onClick = { fetch.reload() }) { Text("重试") }
        }

        is Load.Ok -> content(s.value)
    }
}

/** 一行「标签：值」。标签定宽，多行值也能对齐。 */
@Composable
fun KeyValue(label: String, value: String, modifier: Modifier = Modifier) {
    Row(modifier.fillMaxWidth().padding(vertical = 3.dp)) {
        Text(
            text = label,
            modifier = Modifier.width(92.dp),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(text = value, style = MaterialTheme.typography.bodyMedium)
    }
}

/** 提示条。用于「参考图变了」「视频丢了」这类必须看见的状态。 */
@Composable
fun Banner(text: String, tone: Tone = Tone.WARN, modifier: Modifier = Modifier) {
    val (bg, fg) = when (tone) {
        Tone.WARN -> Color(0xFF4A3A00) to Color(0xFFFFD98A)
        Tone.BAD -> MaterialTheme.colorScheme.errorContainer to
            MaterialTheme.colorScheme.onErrorContainer
        Tone.OK -> Color(0xFF10331A) to Color(0xFF9BE7B0)
    }
    Box(
        modifier
            .fillMaxWidth()
            .padding(vertical = 4.dp)
            .clip(RectangleShape)
            .background(bg)
            .padding(horizontal = 10.dp, vertical = 8.dp),
    ) {
        Text(text = text, color = fg, style = MaterialTheme.typography.bodySmall)
    }
}

enum class Tone { OK, WARN, BAD }

/**
 * 一次性动作的遮罩。
 *
 * 入库和换视频在服务端要跑 eval-img + ORB + build-db + ffmpeg（§8.1），几十秒到
 * 几分钟。这期间必须挡住界面并说清「在干什么」，否则用户会当成卡死然后重按一次，
 * 而重按等于再转码一遍。
 */
@Composable
fun ActionOverlay(action: Action, onDismiss: () -> Unit) {
    when (action) {
        is Action.Running -> AlertDialog(
            onDismissRequest = {},
            confirmButton = {},
            title = { Text(action.what) },
            text = {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    CircularProgressIndicator(Modifier.width(20.dp))
                    Text(
                        text = "服务端在转码和建索引，别退出。",
                        modifier = Modifier.padding(start = 12.dp),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            },
        )

        is Action.Failed -> AlertDialog(
            onDismissRequest = onDismiss,
            confirmButton = { Button(onClick = onDismiss) { Text("知道了") } },
            title = { Text("没成") },
            text = { Text(action.message) },
        )

        else -> Unit
    }
}

/** 小标题。 */
@Composable
fun Section(text: String, modifier: Modifier = Modifier) {
    Text(
        text = text,
        modifier = modifier.padding(top = 16.dp, bottom = 4.dp),
        style = MaterialTheme.typography.titleSmall,
        color = MaterialTheme.colorScheme.primary,
    )
}

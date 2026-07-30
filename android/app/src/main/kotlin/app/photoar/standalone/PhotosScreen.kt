package app.photoar.standalone

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import app.photoar.arview.PhotoSummary
import app.photoar.arview.ui.ArScanActivity

/**
 * 照片库。
 *
 * 网格用 `Adaptive(112.dp)`：照片数量会到几万张（§1），格子固定列数在平板上会变成
 * 巨幅缩略图，而这个界面的用途是「认出是哪一张」，越小越密越合适。
 */
@Composable
fun PhotosScreen(shell: Shell) {
    val context = LocalContext.current
    val fetch = rememberFetch(shell.libraryRev) { shell.client.photos() }

    Box(Modifier.fillMaxSize()) {
        LoadFrame(fetch) { photos ->
            if (photos.isEmpty()) {
                Column(
                    Modifier
                        .fillMaxSize()
                        .padding(32.dp),
                    verticalArrangement = Arrangement.Center,
                    horizontalAlignment = Alignment.CenterHorizontally,
                ) {
                    Text("照片库是空的", style = MaterialTheme.typography.titleMedium)
                    Text(
                        text = "点右上角的 ＋ 从 NAS 上挑一张打印过的照片，" +
                            "配一段视频关联进来。",
                        style = MaterialTheme.typography.bodySmall,
                        textAlign = TextAlign.Center,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(top = 8.dp),
                    )
                }
            } else {
                LazyVerticalGrid(
                    columns = GridCells.Adaptive(112.dp),
                    contentPadding = PaddingValues(
                        start = 8.dp,
                        end = 8.dp,
                        top = 8.dp,
                        // 给底部的「扫一扫」留位置，否则最后一行会被压住点不到
                        bottom = 88.dp,
                    ),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    items(photos, key = { it.photoId }) { p ->
                        PhotoCell(shell, p) { shell.push(Route.Detail(p.photoId)) }
                    }
                }
            }
        }

        ExtendedFloatingActionButton(
            onClick = { ArScanActivity.start(context) },
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .padding(16.dp),
            text = { Text("扫一扫") },
            icon = {},
        )
    }
}

@Composable
private fun PhotoCell(shell: Shell, p: PhotoSummary, onClick: () -> Unit) {
    Column(Modifier.clickable(onClick = onClick)) {
        NetImage(
            key = p.refThumbUrl,
            modifier = Modifier
                .fillMaxWidth()
                // 一律按 1:1 裁：横竖混排时对齐比保真重要，真实比例在详情页看。
                .aspectRatio(1f),
        ) { shell.client.download(p.refThumbUrl) }

        Text(
            text = p.title ?: p.photoId.take(8),
            style = MaterialTheme.typography.bodySmall,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 4.dp),
        )
        Row {
            if (!p.hasVideo) Flag("无视频", MaterialTheme.colorScheme.error)
            if (p.refStale) Flag("参考图变了", MaterialTheme.colorScheme.primary)
        }
    }
}

@Composable
private fun Flag(text: String, color: androidx.compose.ui.graphics.Color) {
    Text(
        text = text,
        style = MaterialTheme.typography.labelSmall,
        color = color,
        modifier = Modifier.padding(end = 6.dp),
    )
}

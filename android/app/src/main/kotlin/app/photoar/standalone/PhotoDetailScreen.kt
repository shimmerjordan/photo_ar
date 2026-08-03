package app.photoar.standalone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import app.photoar.arview.ui.ArScanActivity

/**
 * 一张照片的全部状态。
 *
 * 这一页存在的理由是 §8.4 那三种「库和 NAS 不一致」：参考图没了、参考图变了、
 * 视频没了。它们在服务端只是三个布尔，扫的时候表现成「怎么都不识别」或者「识别了
 * 但播不出来」—— 必须有一个地方把它们直说出来。
 */
@Composable
fun PhotoDetailScreen(shell: Shell, photoId: String) {
    val context = LocalContext.current
    val fetch = rememberFetch(photoId, shell.libraryRev) { shell.client.photoDetail(photoId) }

    LoadFrame(fetch) { d ->
        Column(
            Modifier
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp)
                .padding(bottom = 24.dp),
        ) {
            val thumb = "/v1/photo/${d.photoId}/thumb"
            NetImage(
                key = thumb,
                modifier = Modifier
                    .fillMaxWidth()
                    .aspectRatio(d.refAspect ?: 1.5f)
                    .padding(top = 12.dp),
                contentScale = ContentScale.Fit,
            ) { shell.client.download(thumb) }

            if (d.refMissing) {
                Banner("参考图在 NAS 上找不到了。这张照片扫不出来，去把文件放回原处。", Tone.BAD)
            }
            if (d.refStale) {
                Banner("参考图的文件变过了（大小或时间不一致）。索引里还是旧的特征，识别率会掉，建议重新入库。")
            }
            when (d.videoMissing) {
                null -> Banner("还没关联视频。识别出来也没东西可播。")
                true -> Banner("关联的视频文件不见了。", Tone.BAD)
                false -> Unit
            }

            Section("尺寸与质量")
            KeyValue("打印宽度", Fmt.widthMm(d.printWidthM))
            KeyValue("质量分", "${d.qualityScore}（${Fmt.qualityLabel(d.qualityScore)}）")
            // 自匹配分：用参考图自己去查索引应得的分。它低说明这张图特征本来就弱，
            // 和「现场光线不好」是两回事，扫不出来时先看这个。
            KeyValue("自匹配", "${d.selfScore}")
            KeyValue("索引大小", Fmt.bytes(d.imgdbBytes))

            Section("NAS 上的文件")
            KeyValue("参考图", d.refPath ?: "—")
            KeyValue("视频", d.videoPath ?: "—")

            Section("其它")
            KeyValue("photoId", d.photoId)
            KeyValue("入库", Fmt.time(d.createdAt))
            KeyValue("更新", Fmt.time(d.updatedAt))

            // 主动作是「试播」而不是「换视频」：刚配完视频最想确认的是配对配没配错，
            // 而那件事不需要相机、也不需要手里有打印件（见 [Route.Play]）。视频不在
            // 就按不下去 —— 那种情况下点进去只会看到一个 ExoPlayer 的错误码。
            val playable = d.hasVideo && d.videoMissing == false
            Row(
                Modifier
                    .fillMaxWidth()
                    .padding(top = 20.dp),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                Button(
                    onClick = { shell.push(Route.Play(d.photoId)) },
                    enabled = playable,
                ) {
                    Text("试播")
                }
                OutlinedButton(onClick = { ArScanActivity.start(context) }) { Text("去扫这张") }
            }

            // 换视频/换照片都在「素材」页做：那两件事都要先把文件从手机传上去，而
            // 上传的进度、隧道限制、原始文件名的记录都在那一页里。这里只留一句指路，
            // 不做第二个入口 —— 同一件事两处实现，其中一处迟早落后。
            Text(
                text = "试播是全屏放一遍，不开相机；「去扫这张」才走 AR。" +
                    "要换这张的照片或视频，去底栏「素材」页的上传历史里改。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(top = 8.dp),
            )
        }
    }
}

package app.photoar.standalone

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import app.photoar.arview.ui.ArScanActivity
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * 访客的首页：整页一个「扫一扫」。
 *
 * 宾客打开这个 App 只有一件事可做，所以这一页上只有一件事。没有列表、没有筛选、
 * 没有次要动作 —— 他站在照片前面，手里举着手机，任何一个多出来的按钮都是一次
 * 「我该点哪个」。
 *
 * 底下那一行「你有 N 张照片可扫」不是装饰，它回答的是宾客唯一会问的另一个问题：
 * 「这个 App 对我有用吗」。N 是 0 的时候尤其重要 —— 那时他扫什么都不会动，而**原因
 * 不在他这边**（管理员还没给他授权），这句话是他唯一能据此去问人的线索。
 */
@Composable
fun ScanHomeScreen(shell: Shell) {
    val context = LocalContext.current
    var count by remember { mutableStateOf<Int?>(null) }
    var failed by remember { mutableStateOf(false) }

    // 跟着 libraryRev 重取：管理员刚给他授权之后，下拉不了这一页（没有列表），
    // 所以切个页签回来就得是新数字。
    LaunchedEffect(shell.libraryRev) {
        count = null
        failed = false
        val r = runCatching { withContext(Dispatchers.IO) { shell.client.photos() } }
        r.fold(
            onSuccess = { count = it.size },
            onFailure = { failed = true },
        )
    }

    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
        Column(
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.Center,
            modifier = Modifier.padding(horizontal = 32.dp),
        ) {
            // 200dp：比 FAB 的 76dp 大得多，是这一屏唯一的视觉重心。圆形而不是
            // 圆角矩形，因为它要读成「一个大按钮」而不是「一个卡片」。
            Button(
                onClick = { ArScanActivity.start(context) },
                shape = CircleShape,
                modifier = Modifier.size(200.dp),
                colors = ButtonDefaults.buttonColors(
                    containerColor = MaterialTheme.colorScheme.primary,
                    contentColor = MaterialTheme.colorScheme.onPrimary,
                ),
            ) {
                Text("扫一扫", style = MaterialTheme.typography.headlineSmall)
            }

            Spacer(Modifier.height(28.dp))
            Text(
                text = "对着照片，等它动起来",
                style = MaterialTheme.typography.bodyLarge,
                color = MaterialTheme.colorScheme.onSurface,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                text = hintOf(count, failed),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
        }
    }
}

/**
 * 底下那一行的文案。
 *
 * 抽成一个函数是为了让「N = 0 时说什么」这个决定看得见：那是唯一一个用户需要**去做
 * 别的事**的分支（找管理员），而它很容易被写成一句「暂无照片」——那句话什么也没说。
 */
internal fun hintOf(count: Int?, failed: Boolean): String = when {
    failed -> "现在连不上服务端。可以先扫，扫不出来就是网络的问题。"
    count == null -> "正在看你有几张照片…"
    count == 0 ->
        "管理员还没有把照片授权给你，现在扫任何照片都不会有反应。找他把你加上就行。"
    else -> "你有 $count 张照片可扫。"
}

package app.photoar.standalone

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import android.widget.Toast

/**
 * 管理：内嵌管理台的入口，加上两个只有 App 侧才顺手的运维页。
 *
 * 这一页是「多分一些模块」里那个**收纳管理动作**的模块。改造前这些东西散在设置页
 * （离线缓存）和底栏（识别历史）里，而它们和「改服务端地址」「看版本号」不是一类事：
 * 前者是天天用的运维动作，后者配一次就不动了。
 *
 * 用户、授权、配置、照片↔视频映射、Excel 批量导入这五件事**全部**交给内嵌的管理台，
 * 不在 Compose 里重写 —— 理由写在 [AdminWebScreen] 的 docstring 里（一句话版：那边
 * 是跟着服务端一起发版的，重写等于让同一件事有两套实现，其中一套永远慢一个版本）。
 */
@Composable
fun AdminScreen(shell: Shell) {
    val context = LocalContext.current
    Column(
        Modifier
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 16.dp)
            // 88：底栏上那颗「扫一扫」不算进 innerPadding，滚到底会盖住最后一行。
            .padding(bottom = 88.dp),
    ) {
        Section("管理台")
        Text(
            text = "用户、授权、识别参数、照片↔视频映射、Excel 批量导入都在管理台里。" +
                "在这儿打开不用再登一次 —— App 的登录和管理台是同一个会话。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Row(
            Modifier.padding(top = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Button(onClick = { shell.push(Route.AdminWeb) }) { Text("在 App 里打开") }
            // 逃生口。**必须有**：管理台是按鼠标和大屏设计的，塞进手机 WebView 之后
            // 有些东西天生不好用（多层弹窗、宽表格横向滚动）。系统浏览器有地址栏、
            // 有完整的文件选择器、有密码管理器，遇到 WebView 里点不动的东西时那是
            // 唯一的出路 —— 而没有这个按钮，用户只能去电脑上做。
            OutlinedButton(onClick = { openAdminInBrowser(context, shell) }) {
                Text("在浏览器里打开")
            }
        }
        Text(
            text = "WebView 里点不动的东西（多层弹窗、很宽的表格）用浏览器打开就好。" +
                "浏览器里要**再登一次** —— 那是另一个应用，拿不到 App 的会话。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 6.dp),
        )
        Text(
            // 这一条必须说：同一条 session，在管理台里点登出会把 App 也一起登出，
            // 而那看起来像 App 自己掉线了。
            text = "注意：在管理台里点「登出」会把 App 的登录一起作废（是同一条会话）。" +
                "想换账号就用这一条，否则别点。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(top = 8.dp),
        )

        Section("识别历史")
        Text(
            text = "全库的识别记录：什么时候、哪张、多少内点、走的哪条通道。" +
                "扫不出来时第一个该看的地方。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedButton(
            onClick = { shell.push(Route.History) },
            modifier = Modifier.padding(top = 8.dp),
        ) {
            Text("看识别历史")
        }

        Section("离线缓存")
        Text(
            text = "把照片特征和视频存到手机上，没网也能扫。出门前在这儿同步一次。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedButton(
            onClick = { shell.push(Route.Cache) },
            modifier = Modifier.padding(top = 8.dp),
        ) {
            Text("管理离线缓存")
        }
    }
}

/**
 * 用系统浏览器打开管理台。
 *
 * 拿的是当前 api 通道的地址 —— 与 [AdminWebScreen] 同一个来源，所以两条路进的是同一个
 * 服务端。取不到（没探活成功）时给一句提示而不是静默什么都不做。
 */
private fun openAdminInBrowser(context: Context, shell: Shell) {
    val base = shell.center.endpoints().apiBase.trimEnd('/')
    if (base.isBlank()) {
        Toast.makeText(
            context,
            "现在没有可用的 api 通道，去「设置」里看一眼探活结果。",
            Toast.LENGTH_LONG,
        ).show()
        return
    }
    val ok = runCatching {
        context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("$base/admin")))
    }.isSuccess
    if (!ok) {
        // 这台手机上没有能处理 http 的应用。少见但不是不可能（精简 ROM）。
        Toast.makeText(context, "这台手机上没有可用的浏览器。", Toast.LENGTH_LONG).show()
    }
}

package app.photoar.standalone

import android.annotation.SuppressLint
import android.webkit.CookieManager
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.viewinterop.AndroidView
import androidx.compose.ui.unit.dp
import app.photoar.arview.EndpointCenter

/**
 * 把 Web 管理台内嵌进 App。
 *
 * ## 为什么内嵌，而不是用 Compose 重写一遍
 *
 * 管理台已经有 1342 行 JS 在做用户、授权、配置、映射、批量导入这五件事，而且它是**跟着
 * 服务端一起发版**的 —— 服务端加一个配置项，管理台立刻就有那一行，不需要用户更新 App。
 * 在 Compose 里重写一遍换来的是「同一件事有两套实现」，而其中一套永远慢一个版本。
 *
 * 代价是这一页在离线时是白的（管理台是从服务端取的）。这个代价可以接受：管理台上每一个
 * 动作都要打服务端，离线时就算界面画出来了也一样什么都做不了。
 *
 * ## 单点登录：token 就是 cookie 的值
 *
 * App 手上是一个 Bearer token，而管理台**只认 cookie**（它刻意不把 token 存进
 * localStorage，理由写在 app.js 的文件头）。看起来要登两次。
 *
 * 但服务端下发那个会话 cookie 时，写进去的**就是同一个 token**
 * （`app.Server._session_cookie` 里 `f"{SESSION_COOKIE}={token}"`）。所以把 App 的
 * token 塞进 WebView 的 CookieManager，管理台一进去就是已登录状态 —— 不是「绕过鉴权」，
 * 是把同一份凭证换了个带法。服务端那边同一个 `_credential` 本来就认两条路。
 *
 * ⚠️ 这也意味着**管理台里的登出会作废 App 的登录**（同一条 session）。所以下面
 * 屏蔽掉了管理台的登出按钮做不到 —— 那是 Web 里的事，App 管不着 —— 只能在文案里说清。
 */
@Composable
fun AdminWebScreen(center: EndpointCenter) {
    val context = LocalContext.current
    val base = remember { apiBaseOf(center) }
    var progress by remember { mutableStateOf(0) }
    var error by remember { mutableStateOf<String?>(null) }

    // `<input type="file">` 的桥。WebView 自己没有文件选择器，得由宿主 App 转一手。
    // 悬着的回调放在 composition 外面（`remember`），因为 WebChromeClient 是在
    // `factory` 里建的，而结果是在另一个回调里回来的。
    val pending = remember { androidx.compose.runtime.mutableStateOf<
        android.webkit.ValueCallback<Array<android.net.Uri>>?>(null) }
    val fileChooser = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val cb = pending.value
        pending.value = null
        // 每个 callback **必须**被调用一次，哪怕用户取消了（那时给 null）。
        // 漏掉的话那个 <input> 从此再也打不开 —— 它一直等着上一次的结果。
        cb?.onReceiveValue(
            android.webkit.WebChromeClient.FileChooserParams.parseResult(
                result.resultCode,
                result.data,
            ),
        )
    }

    if (base == null) {
        Box(Modifier.fillMaxSize().padding(24.dp)) {
            Text(
                text = "现在没有可用的 api 通道，管理台打不开。" +
                    "去「设置」里看一眼通道地址和探活结果。",
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        return
    }

    Column(Modifier.fillMaxSize()) {
        if (progress in 1..99) {
            LinearProgressIndicator(
                progress = { progress / 100f },
                modifier = Modifier.fillMaxWidth(),
            )
        }
        error?.let {
            Text(
                text = it,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp),
            )
        }
        AndroidView(
            modifier = Modifier.fillMaxSize(),
            factory = { ctx ->
                WebView(ctx).apply {
                    // JS 是必须的：管理台整个界面都是 app.js 建的 DOM，关掉 JS 得到的
                    // 是一个空 body。
                    @SuppressLint("SetJavaScriptEnabled")
                    settings.javaScriptEnabled = true
                    // DOM storage：管理台自己不用 localStorage 存凭证（刻意的），但
                    // `<dialog>` 的 polyfill 路径和一些浏览器内部状态要它。开着不引入
                    // 凭证泄露面 —— 那里本来就没有凭证。
                    settings.domStorageEnabled = true
                    // 不允许它读本地文件与 content://。管理台不需要，而 WebView 的
                    // file 访问是历史上出过一串漏洞的地方。
                    settings.allowFileAccess = false
                    settings.allowContentAccess = false
                    settings.setSupportZoom(true)
                    settings.builtInZoomControls = true
                    settings.displayZoomControls = false

                    installSessionCookie(base, center.config.token)

                    webViewClient = object : WebViewClient() {
                        override fun shouldOverrideUrlLoading(
                            view: WebView,
                            request: android.webkit.WebResourceRequest,
                        ): Boolean {
                            // 只让它待在自己的服务端里。管理台上有一个「开源地址」
                            // 之类的外链，在这个 WebView 里打开会变成一个没有地址栏、
                            // 退不出去的浏览器 —— 交给系统浏览器才对。
                            val url = request.url.toString()
                            if (url.startsWith(base)) return false
                            runCatching {
                                view.context.startActivity(
                                    android.content.Intent(
                                        android.content.Intent.ACTION_VIEW,
                                        request.url,
                                    ),
                                )
                            }
                            return true
                        }

                        override fun onReceivedError(
                            view: WebView,
                            request: android.webkit.WebResourceRequest,
                            err: android.webkit.WebResourceError,
                        ) {
                            // 只报主文档的失败。子资源（缩略图 404）报出来只是噪声，
                            // 而管理台自己已经把缺图画成「缺图」了。
                            if (request.isForMainFrame) {
                                error = "管理台加载失败：${err.description}"
                            }
                        }
                    }
                    webChromeClient = object : android.webkit.WebChromeClient() {
                        override fun onProgressChanged(view: WebView, newProgress: Int) {
                            progress = newProgress
                        }

                        /**
                         * `<input type="file">` 在 WebView 里**默认什么都不做**。
                         *
                         * 不是「不好用」而是**完全没反应**：WebView 不像浏览器那样自带
                         * 文件选择器，宿主 App 不实现这个回调的话点下去连一点动静都没有，
                         * 也不报错。管理台「批量」页那个「选文件…」就是这么哑掉的。
                         *
                         * 交给系统的文件选择器，把结果回给 WebView。
                         */
                        override fun onShowFileChooser(
                            view: WebView,
                            callback: android.webkit.ValueCallback<Array<android.net.Uri>>,
                            params: FileChooserParams,
                        ): Boolean {
                            val launcher = fileChooser
                            // 上一次的回调如果还悬着，先喂它一个 null —— WebView 要求
                            // 每个 callback 必须被调用一次，漏掉会让那个 <input> 从此
                            // 再也打不开（它一直等着上一次的结果）。
                            pending.value?.onReceiveValue(null)
                            pending.value = callback
                            return runCatching {
                                launcher.launch(params.createIntent())
                                true
                            }.getOrElse {
                                pending.value = null
                                callback.onReceiveValue(null)
                                false
                            }
                        }
                    }
                    loadUrl("$base/admin")
                }
            },
        )
    }
}

/**
 * 当前 api 通道的 base。
 *
 * 用 api 而不是 media：管理台的每一个请求都是接口调用，而 media 那条可能是隧道
 * （§9.4，隧道上有 100MB 请求体上限，批量导入传表格倒是够，但 media 通道的选取
 * 目标是视频流，拿它发接口请求是在用错的那条路）。
 */
private fun apiBaseOf(center: EndpointCenter): String? =
    center.endpoints().apiBase.trimEnd('/').ifBlank { null }

/**
 * 把 App 的 token 作为会话 cookie 写进 WebView。
 *
 * cookie 名与服务端的 `SESSION_COOKIE` 必须逐字一致。写成常量放在这里而不是从
 * 服务端某个响应里读：它是一个契约，而契约写死在两边、由一条注释互指，比在运行时
 * 猜一个名字要好 —— 猜错的表现是「进去还要再登一次」，而没有任何错误信息。
 *
 * 不设 `Secure`：这条链路可能是 http 的内网直连（服务端那边 `PHOTOAR_COOKIE_SECURE`
 * 默认也是关的，理由写在 .env.example 里）。设了的话 http 下 WebView 会直接丢掉它，
 * 表现同上。
 */
private fun installSessionCookie(base: String, token: String) {
    if (token.isBlank()) return
    val cm = CookieManager.getInstance()
    cm.setAcceptCookie(true)
    // HttpOnly 不能由客户端设置，也不需要 —— 这个 WebView 里没有第三方脚本。
    // Path=/ 与服务端下发的那份保持一致（管理台在 /admin，接口在 /v1，两个前缀
    // 都要带上它）。
    cm.setCookie(base, "$SESSION_COOKIE=$token; Path=/; SameSite=Lax")
    cm.flush()
}

/** 与服务端 `app.SESSION_COOKIE` 逐字对应。改一边必须改另一边。 */
private const val SESSION_COOKIE = "photoar_session"

package app.photoar.arview.net

import app.photoar.arview.Clock
import app.photoar.arview.Endpoints
import app.photoar.arview.Prober
import java.io.IOException

/**
 * 「通了，但服务端不干」。与 [Prober] 约定的 null（网络层不通）分开：
 *
 * 令牌填错时四条通道会**全部**探不通，如果这时只显示「不通」，用户会去查路由、
 * 查防火墙、查 Tailscale —— 而问题在设置里那一行。所以 HTTP 层的拒绝要带原文
 * 冒出来，[app.photoar.arview.EndpointResolver] 会把它记进 `Probed.error`。
 */
class ProbeFailed(message: String) : IOException(message)

/**
 * 用 `GET /v1/ping` 探活（§9.2）。
 *
 * `/v1/ping` 需要 Bearer token（服务端 `_dispatch` 在路由之前就鉴权），所以探活
 * 必须带令牌 —— 这也是上面那个 401 分支存在的原因。
 */
class HttpProber(
    private val transport: HttpTransport,
    private val token: () -> String,
    private val clock: Clock = Clock { System.currentTimeMillis() },
) : Prober {

    override fun ping(base: String, timeoutMs: Int): Long? {
        val started = clock.nowMs()
        val reply = try {
            transport.get(
                url = Endpoints.joinUrl(base, "/v1/ping"),
                headers = mapOf(
                    "Authorization" to "Bearer ${token()}",
                    "Accept-Encoding" to "identity",
                ),
                timeoutMs = timeoutMs,
            )
        } catch (e: HttpFailure) {
            // 超时 / 连不上 / URL 不合法 —— 都是「这条通道现在用不了」，返回 null
            // 让上层写成「不通」。这是探活最常走的一条路（在外时 LAN 必然如此），
            // 不该在日志或界面上显得像异常。
            return null
        }
        val elapsed = (clock.nowMs() - started).coerceAtLeast(0L)
        if (reply.ok) return elapsed
        throw ProbeFailed(describe(reply))
    }

    private fun describe(reply: HttpReply): String = when {
        // 服务端换成用户体系之后，这里最常见的原因是**没登录或登录过期**，而不是
        // 「令牌填错了」—— 探活带的就是那个会话 token。文案要把人指到账号那一块去。
        reply.status == 401 || reply.status == 403 ->
            "没登录或登录已过期（${reply.status}）"
        reply.status >= 500 -> "服务端出错（${reply.status}）"
        // 404 通常意味着这个地址后面根本不是 photo-ar-server（打错端口、
        // 反代规则没配到），说「不通」会让人查错方向。
        reply.status == 404 -> "这个地址上没有 photo-ar-server（404）"
        else -> "HTTP ${reply.status}"
    }
}

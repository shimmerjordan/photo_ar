package app.photoar.arview

import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.Callable
import java.util.concurrent.ExecutorService
import java.util.concurrent.TimeUnit

/**
 * §9 的 endpoint 多通道解析。
 *
 * 这个文件里不许出现任何 android.* 依赖 —— §14.4 要求它「表驱动，覆盖 §9.3 全部
 * 场景」，而那只有在 JVM 单测里跑得起来才做得到。探活的真实网络调用被抽到
 * [Prober] 后面，持久化在 `:app` 里。
 */

/** 一个 endpoint 能被用来干什么（§9.1 的 `prefer` 数组元素）。 */
enum class EndpointUse(val key: String) {
    API("api"),
    MEDIA("media"),
    ;

    companion object {
        fun of(key: String): EndpointUse? =
            entries.firstOrNull { it.key.equals(key.trim(), ignoreCase = true) }
    }
}

/**
 * §9.1 候选列表里的一项。
 *
 * [prefer] 是「这条通道适合干什么」，不是优先级数字 —— 选择时看的是**成员关系**
 * 加**列表顺序**（见 [EndpointChoice.select]）。写成数组只是因为一条通道可以同时
 * 适合两种用途（LAN 就是）。
 */
data class EndpointCandidate(
    val name: String,
    val base: String,
    val prefer: List<EndpointUse> = emptyList(),
    val enabled: Boolean = true,
    /**
     * 走 Cloudflare 隧道。§9.4：隧道有 100MB 请求体上限，所以 mediaEndpoint 是
     * 隧道时要藏掉上传入口。服务端也会按 `cf-ray` 头再挡一道（返回 413），
     * 这里只是为了不让用户白等一次上传失败。
     */
    val tunnel: Boolean = false,
) {
    val usable: Boolean get() = enabled && base.isNotBlank()

    /** 写进 `X-PhotoAR-Endpoint`，服务端记进识别历史。 */
    val viaLabel: String get() = name.trim().lowercase().ifEmpty { "unnamed" }

    fun toJson(): JSONObject = JSONObject().apply {
        put("name", name)
        put("base", base)
        put("prefer", JSONArray().also { arr -> prefer.forEach { arr.put(it.key) } })
        put("enabled", enabled)
        put("tunnel", tunnel)
    }

    companion object {
        fun fromJson(o: JSONObject): EndpointCandidate {
            val name = o.optString("name", "").trim()
            val prefer = ArrayList<EndpointUse>(2)
            val arr = o.optJSONArray("prefer")
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    // 认不出来的用途直接丢掉，不要让一个笔误把整份配置弄废
                    EndpointUse.of(arr.optString(i, ""))?.let { if (it !in prefer) prefer.add(it) }
                }
            }
            return EndpointCandidate(
                name = name,
                base = o.optString("base", "").trim().trimEnd('/'),
                prefer = prefer,
                enabled = o.optBoolean("enabled", true),
                // 没有显式写 tunnel 的旧配置按名字猜一次。猜错的代价只是上传入口
                // 多显示或少显示，服务端那道 413 兜着。
                tunnel = o.optBoolean("tunnel", name.equals("tunnel", ignoreCase = true)),
            )
        }
    }
}

/** 候选列表 + token。§9.1「App 内可编辑，带默认值」的那份东西。 */
data class EndpointConfig(
    val candidates: List<EndpointCandidate>,
    val token: String,
) {
    fun toJson(): String = JSONObject().apply {
        put("token", token)
        put("endpoints", JSONArray().also { arr -> candidates.forEach { arr.put(it.toJson()) } })
    }.toString()

    companion object {
        /**
         * §9.1 的默认值。base 留空的项在界面上可见但不参与探活（[EndpointCandidate.usable]），
         * 这样用户拿到公网 IP 之后填一格就能用，不需要改代码。
         */
        val DEFAULT = EndpointConfig(
            candidates = listOf(
                EndpointCandidate("LAN", "", listOf(EndpointUse.MEDIA, EndpointUse.API)),
                EndpointCandidate("Tailscale", "", listOf(EndpointUse.MEDIA)),
                EndpointCandidate("Tunnel", "", listOf(EndpointUse.API), tunnel = true),
                EndpointCandidate("DDNS", "", listOf(EndpointUse.MEDIA, EndpointUse.API), enabled = false),
            ),
            token = "",
        )

        /**
         * 从 Phase 2 那三个死值（apiBase / mediaBase / token）造一份候选列表。
         *
         * 真机上已经装过 Phase 2 的包，升级后不该让人重新输一遍地址和令牌。两个
         * base 相同时只留一条（LAN 同时承担 api 与 media）；不同则把 media 那条
         * 单独列出来 —— Phase 2 分开配的唯一理由就是「大流量别走隧道」，那正好
         * 对应 `prefer=[media]`。
         */
        fun fromLegacy(apiBase: String, mediaBase: String, token: String): EndpointConfig {
            val api = apiBase.trim().trimEnd('/')
            val media = mediaBase.trim().trimEnd('/')
            if (api.isEmpty() && media.isEmpty()) return DEFAULT.copy(token = token)
            val both = media.isEmpty() || media == api
            val list = ArrayList<EndpointCandidate>(2)
            list.add(
                EndpointCandidate(
                    name = "已保存",
                    base = api.ifEmpty { media },
                    prefer = if (both) listOf(EndpointUse.MEDIA, EndpointUse.API)
                    else listOf(EndpointUse.API),
                ),
            )
            if (!both) {
                list.add(EndpointCandidate("已保存（媒体）", media, listOf(EndpointUse.MEDIA)))
            }
            // 默认里 base 为空的那几条一并带上：升级后设置界面仍然是「填一格就能用」。
            list.addAll(DEFAULT.candidates.filter { it.base.isEmpty() })
            return EndpointConfig(list, token)
        }

        fun parse(json: String): EndpointConfig {
            val o = try {
                JSONObject(json)
            } catch (e: Exception) {
                return DEFAULT
            }
            val arr = o.optJSONArray("endpoints") ?: return DEFAULT.copy(
                token = if (o.isNull("token")) "" else o.optString("token", ""),
            )
            val list = ArrayList<EndpointCandidate>(arr.length())
            for (i in 0 until arr.length()) {
                arr.optJSONObject(i)?.let { list.add(EndpointCandidate.fromJson(it)) }
            }
            return EndpointConfig(
                candidates = if (list.isEmpty()) DEFAULT.candidates else list,
                token = if (o.isNull("token")) "" else o.optString("token", ""),
            )
        }
    }
}

/** 一次探活的结果。[latencyMs] 只用于界面显示，不参与选择。 */
data class Probed(
    val candidate: EndpointCandidate,
    val reachable: Boolean,
    val latencyMs: Long = -1,
    /** 不通的原因，给设置界面看。 */
    val error: String? = null,
)

/** §9.2 的选择规则。纯函数，§9.3 那张表就是它的测试。 */
object EndpointChoice {

    /**
     * 「在通的 endpoint 中，api 与 media 各自独立选择：按该用途的 prefer 顺序取
     * 第一个通的；都不在 prefer 里则按列表顺序兜底」（§9.2）。
     *
     * 兜底那半句是 §9.3 第三行（在外 + Tailscale 未登录）成立的原因：偏好 media
     * 的两条都不通，media 只能落到唯一通着的 Tunnel 上。没有这条兜底，那个场景
     * 下视频就播不了了。
     */
    fun select(probed: List<Probed>, use: EndpointUse): Probed? {
        val live = probed.filter { it.reachable && it.candidate.usable }
        return live.firstOrNull { use in it.candidate.prefer } ?: live.firstOrNull()
    }
}

/** 探活一个 base。返回耗时毫秒，不通返回 null。 */
fun interface Prober {
    fun ping(base: String, timeoutMs: Int): Long?
}

/** [EndpointResolver.refresh] 的结果。都为 null 表示断网（§9.3 第四行）。 */
data class Resolution(
    val probed: List<Probed>,
    val api: Probed?,
    val media: Probed?,
) {
    val offline: Boolean get() = api == null && media == null
}

/**
 * 维护候选列表、探活、选出 api/media 两条通道（§5.7）。
 *
 * 触发时机（§9.2）：App 启动、网络变化、用户手动刷新、以及状态机连续失败 2 次
 * 之后的 [ScanController] → `requestEndpointRefresh()`。
 */
class EndpointResolver(
    private val prober: Prober,
    private val executor: ExecutorService,
    private val clock: Clock,
    initial: EndpointConfig = EndpointConfig.DEFAULT,
) {

    companion object {
        /** §9.2：探活超时 1.5s。 */
        const val PROBE_TIMEOUT_MS = 1_500

        /**
         * 两次探活之间的最小间隔。连续失败会每 2 次识别就请求一次重新探活
         * （见 [ScanController]），而探活本身要 1.5s —— 不设这个下限的话，
         * 弱网下会变成探活风暴，把本来就不通的通道压得更死。
         */
        const val MIN_INTERVAL_MS = 3_000L
    }

    @Volatile
    var config: EndpointConfig = initial
        private set

    @Volatile
    var resolution: Resolution? = null
        private set

    private var lastRefreshAt = Long.MIN_VALUE

    /** 换配置。会清掉上次的探活结果，因为它对新列表已经没有意义。 */
    fun update(newConfig: EndpointConfig) {
        config = newConfig
        resolution = null
        lastRefreshAt = Long.MIN_VALUE
    }

    /**
     * 并行探活并重算选择。
     *
     * @param force 用户手动点刷新时为 true，跳过 [MIN_INTERVAL_MS] 的节流。
     * @return 新结果；被节流挡掉时返回上一次的结果。
     */
    fun refresh(force: Boolean = false): Resolution {
        val now = clock.nowMs()
        val cached = resolution
        if (!force && cached != null && now - lastRefreshAt < MIN_INTERVAL_MS) return cached
        lastRefreshAt = now

        val list = config.candidates
        // 不可用的项不发请求，但要留在结果里 —— 设置界面得把它们也列出来。
        val tasks = list.map { c ->
            Callable {
                if (!c.usable) {
                    Probed(c, reachable = false, error = if (c.enabled) "没填地址" else "已停用")
                } else {
                    probeOne(c)
                }
            }
        }
        val probed = try {
            // §9.2「并行」：四条通道里最慢的那条决定总耗时（1.5s），而不是相加。
            executor.invokeAll(tasks, PROBE_TIMEOUT_MS * 2L, TimeUnit.MILLISECONDS)
                .mapIndexed { i, f ->
                    try {
                        f.get()
                    } catch (e: Exception) {
                        Probed(list[i], reachable = false, error = e.message ?: "探活失败")
                    }
                }
        } catch (e: InterruptedException) {
            Thread.currentThread().interrupt()
            return cached ?: Resolution(list.map { Probed(it, false, error = "已取消") }, null, null)
        }

        val next = Resolution(
            probed = probed,
            api = EndpointChoice.select(probed, EndpointUse.API),
            media = EndpointChoice.select(probed, EndpointUse.MEDIA),
        )
        resolution = next
        return next
    }

    private fun probeOne(c: EndpointCandidate): Probed {
        val t = try {
            prober.ping(c.base, PROBE_TIMEOUT_MS)
        } catch (e: Exception) {
            return Probed(c, reachable = false, error = e.message ?: e.javaClass.simpleName)
        }
        return if (t == null) Probed(c, reachable = false, error = "不通")
        else Probed(c, reachable = true, latencyMs = t)
    }

    /**
     * 给 [net.PhotoArClient] 用的当前两条通道。
     *
     * 断网时返回**第一个可用候选的地址**而不是空串：状态机已经把网络失败建模成
     * 正常状态（静默重试 + 连续 2 次后重新探活），让 URL 保持合法、失败落在
     * `TRANSPORT` 上，比抛一个「地址是空的」出去更简单。
     */
    fun endpoints(): Endpoints {
        val r = resolution
        val fallback = config.candidates.firstOrNull { it.usable }?.base ?: ""
        return Endpoints(
            apiBase = r?.api?.candidate?.base ?: fallback,
            mediaBase = r?.media?.candidate?.base ?: fallback,
            token = config.token,
        )
    }

    /** 当前 api 通道的名字，写进 `X-PhotoAR-Endpoint`。 */
    fun viaLabel(): String? = resolution?.api?.candidate?.viaLabel

    /** §9.4：mediaEndpoint 是隧道时不给上传。没探活过也不给（不知道走的是哪条）。 */
    fun uploadAllowed(): Boolean {
        val media = resolution?.media ?: return false
        return !media.candidate.tunnel
    }
}

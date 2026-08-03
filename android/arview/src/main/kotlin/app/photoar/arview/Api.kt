package app.photoar.arview

import org.json.JSONArray
import org.json.JSONObject

/**
 * §7 API 契约的客户端一侧：数据类型 + 解析 + URL 拼接。
 *
 * 这个文件里不许出现任何 android.* 依赖 —— 它是 JVM 单测能覆盖的部分。
 */

/**
 * 两条通道（§4.1）。Phase 2 只从设置里读死值，Phase 3 换成 EndpointResolver。
 *
 * [token] 是**不变**的一份快照，而服务端换成用户体系之后 token 是会过期的
 * （访客 30 天、管理员 12 小时）。这个类因此仍然只是「此刻该用哪个 token」——
 * 谁的、什么时候过期由 [AuthState] 记着，两者的关系写在那边。
 */
data class Endpoints(
    val apiBase: String,
    val mediaBase: String,
    val token: String,
) {
    fun api(relative: String): String = joinUrl(apiBase, relative)

    companion object {
        /**
         * 拼 URL。服务端「URL 一律返回相对路径」（§7），因为它不知道客户端
         * 走的哪条通道；唯一的例外是 `via == "direct_link"` 的绝对 URL，
         * 由 [MediaInfo.absolute] 显式标出，那种不能再套前缀。
         */
        fun joinUrl(base: String, relative: String): String {
            if (relative.startsWith("http://") || relative.startsWith("https://")) {
                return relative
            }
            val b = base.trimEnd('/')
            val r = if (relative.startsWith("/")) relative else "/$relative"
            return b + r
        }
    }
}

/** 一次命中。字段与 §7 的命中响应一一对应。 */
data class Hit(
    val photoId: String,
    val inliers: Int,
    val printWidthM: Float,
    val refAspect: Float?,
    /** 照片标题，可能没有。目前只用来给「保存到相册」起文件名。 */
    val title: String? = null,
    val imgdbUrl: String,
    val refThumbUrl: String,
    val mediaUrl: String,
    val refStale: Boolean,
    val latencyMs: Int,
)

sealed interface RecognizeOutcome {
    data class Matched(val hit: Hit) : RecognizeOutcome

    /** 未命中是正常状态，服务端也返回 200（§7）。[reason] 只用于日志。 */
    data class NoMatch(val reason: String?, val latencyMs: Int) : RecognizeOutcome
}

/** `GET /v1/photo/{id}/media` 的响应。 */
data class MediaInfo(
    val url: String?,
    val via: String?,
    val absolute: Boolean,
    val supportsRange: Boolean,
    val bytes: Long,
    val durationMs: Long?,
    val missing: Boolean,
    val nasPath: String?,
    val reason: String?,
) {
    /** 能不能拿去播。没有视频、文件丢了、URL 为空，三种都不能。 */
    val playable: Boolean get() = url != null && !missing

    /** 按 §7 的 `absolute` 决定要不要套 mediaEndpoint 前缀。 */
    fun resolvedUrl(endpoints: Endpoints): String? {
        val u = url ?: return null
        return if (absolute) u else Endpoints.joinUrl(endpoints.mediaBase, u)
    }
}

/**
 * 识别失败的分类。超时与其它错误的处理方式不同（§13）。
 *
 * 分类的判据始终是「**下一步该做什么**」，不是「错得有多严重」：
 * - [TIMEOUT] / [TRANSPORT] / [SERVER_ERROR] → 静默重试
 * - [UNAUTHORIZED] → 停下来，回登录界面
 * - [BAD_CREDENTIALS] → 让用户**重输**（口令打错了）
 * - [FORBIDDEN] → 别再试了，把原因显示出来
 * - [BAD_RESPONSE] → 契约破了，要能看见
 */
enum class NetErrorKind {
    TIMEOUT,

    /** 凭证不对或已过期。重试无意义，扫描停下来、用户去重新登录。 */
    UNAUTHORIZED,

    /**
     * 登录时口令不对（服务端 401 `bad_credentials`）。
     *
     * **不能复用 [UNAUTHORIZED]**，理由不是分得细一点更好看，而是两者的下一步动作
     * 正好相反：[UNAUTHORIZED] 在状态机里的语义是「重试无意义，停止扫描并送用户去
     * 登录」（见 [NoticeKind.UNAUTHORIZED]）；而登录时口令打错恰恰是**重输一次就可能
     * 成功**的那一种。归成一类的话，登录界面上「口令错了，再输一次」和「别再试了」
     * 会变成同一个信号，用户只会看到一个不解释原因的失败。
     */
    BAD_CREDENTIALS,

    /**
     * 服务端 403：名字不在册（`unknown_user`）或账号被停用（`account_disabled`）。
     *
     * 同样不能复用 [UNAUTHORIZED]：既有的 [PhotoArClient] 把 401 和 403 一起映射成
     * [UNAUTHORIZED]，对**普通接口**那是对的（两种都得回去登录）。但登录接口上 403
     * 有两个子类，且都是「重试一万次结果一样」—— 账号只能由管理员建/启用。客户端
     * 必须按 error code 给出不同文案，合并就等于把这个信息扔了。
     *
     * 具体是哪一种由 `HttpFailure.code`（服务端的 `error` 字段）带出来，这里只表达
     * 「别重试」这一件事。
     */
    FORBIDDEN,

    SERVER_ERROR,
    TRANSPORT,
    BAD_RESPONSE,
    ;

    /** 重试有没有意义。状态机与登录界面都按这个分岔。 */
    val retryable: Boolean
        get() = this == TIMEOUT || this == TRANSPORT || this == SERVER_ERROR
}

/**
 * `POST /v1/auth/login` 的响应。
 *
 * [userId] 可以是 null —— 服务端的运维凭证（`PHOTOAR_TOKEN`）换来的 Principal 就没有
 * user_id（那把钥匙不对应任何一个人）。同理 [expiresAt] 为 null 表示「没有过期时刻」，
 * 而不是「已过期」。两处都必须当成合法取值，否则用运维凭证的那条路会在客户端崩掉。
 */
data class LoginResult(
    val token: String,
    val userId: String?,
    val name: String,
    val role: String,
    val grantAll: Boolean,
    val expiresAt: Long?,
) {
    val isAdmin: Boolean get() = role == ROLE_ADMIN

    companion object {
        const val ROLE_ADMIN = "admin"
        const val ROLE_VIEWER = "viewer"
    }
}

/**
 * `GET /v1/targets/manifest` 里的一条：服务端预建的**整库**目标里那张照片的元数据。
 *
 * 字段名与 `/v1/recognize` 命中响应逐个对齐（服务端 `targets._describe` 是刻意这么
 * 做的），所以它能原样转成 [Hit]（转换在 `cache.ServerTargets`，那里才同时看得见
 * 缓存索引与 manifest 两个来源）。多一套名字就是多一条只在离线路径上才走到的分支。
 *
 * [refAspect] 与 [title] 服务端可能给 null（尺寸探不到 / 没起标题），但键总是在。
 */
data class TargetEntry(
    val photoId: String,
    val printWidthM: Float,
    val refAspect: Float?,
    /**
     * 视频贴合方式。**目前端上没有任何地方消费它** —— 留着是因为它在契约里，
     * 而漏解析一个字段的代价是以后加贴合模式时，离线那条路会静默用另一套行为。
     */
    val fitMode: String?,
    val title: String?,
    val hasVideo: Boolean,
    val mediaUrl: String,
    val imgdbUrl: String,
)

/**
 * `GET /v1/targets/manifest`：这台手机可离线识别的那一套目标。
 *
 * [version] 是服务端算的内容哈希，也就是 `GET /v1/targets/db` 的 ETag —— 客户端靠
 * 「manifest 的 version == db 的 ETag」自己验证这一对元数据与库字节是配好的。所以
 * 它必须和库字节一起存，单独存一个没有意义。
 *
 * [overflow] 是因为 ARCore 的 1000 张上限而**没进**预建库的张数。这些照片仍然能靠
 * 服务端 `/v1/recognize` 认出来（只是慢一次往返），所以它不是错误 —— 但必须让用户
 * 看得见，否则「有几张怎么都离不开网」没有任何解释。
 *
 * [building] 是服务端顺手告诉你的「现在去拿 db 会 503」。有它之后 503 就不是一次
 * 需要猜原因的失败。
 */
data class TargetsManifest(
    val version: String,
    val count: Int,
    val overflow: Int,
    val maxTargets: Int,
    val building: Boolean,
    val targets: List<TargetEntry>,
)

/** `GET /v1/auth/me` 的响应。没有 token —— 它只回答「你是谁」。 */
data class AccountInfo(
    val userId: String?,
    val name: String,
    val role: String,
    val grantAll: Boolean,
    val isAdmin: Boolean,
)

class ApiParseException(message: String) : Exception(message)

object ApiParse {

    fun recognize(json: String): RecognizeOutcome {
        val o = obj(json)
        val latency = o.optInt("latencyMs", -1)
        if (!o.optBoolean("matched", false)) {
            return RecognizeOutcome.NoMatch(str(o, "reason"), latency)
        }
        val photoId = str(o, "photoId") ?: ""
        if (photoId.isEmpty()) {
            // matched=true 但没有 photoId 的响应无法使用。当成解析错误而不是
            // 未命中：未命中会被静默重试，而这是服务端契约破了，要能看见。
            throw ApiParseException("matched=true 但没有 photoId")
        }
        // printWidthM 缺失 / 非正 / NaN 一律归成 0 = **未知**，不再当解析错误。
        //
        // 照片的实际尺寸本来就不一定知道，而不知道并不妨碍贴合：四边形的尺度取 ARCore
        // 自己量的 extentX（见 Geometry.quadSize）。原来这里抛异常，后果是一张没填宽度
        // 的照片在客户端**整条被丢掉** —— 服务端认得出来，手机上却什么都不发生。
        val printWidthM = o.optDouble("printWidthM", 0.0).toFloat()
            .takeIf { it.isFinite() && it > 0f } ?: 0f
        return RecognizeOutcome.Matched(
            Hit(
                photoId = photoId,
                inliers = o.optInt("inliers", 0),
                printWidthM = printWidthM,
                // refAspect 是可选的（参考图没有宽高记录时服务端不返回），
                // 缺了就退回 ARCore 自己量的比例，见 Geometry。
                refAspect = o.optDouble("refAspect", Double.NaN).toFloat()
                    .takeIf { it.isFinite() && it > 0f },
                title = str(o, "title")?.takeIf { it.isNotEmpty() },
                imgdbUrl = str(o, "imgdbUrl") ?: "/v1/photo/$photoId/imgdb",
                refThumbUrl = str(o, "refThumbUrl") ?: "/v1/photo/$photoId/thumb",
                mediaUrl = str(o, "mediaUrl") ?: "/v1/photo/$photoId/media",
                refStale = o.optBoolean("refStale", false),
                latencyMs = latency,
            )
        )
    }

    fun media(json: String): MediaInfo {
        val o = obj(json)
        val via = str(o, "via")
        return MediaInfo(
            url = str(o, "url")?.takeIf { it.isNotEmpty() },
            via = via,
            // 老服务端可能不带 absolute 字段，按 via 兜底判断。
            absolute = o.optBoolean("absolute", via == "direct_link"),
            supportsRange = o.optBoolean("supportsRange", false),
            bytes = o.optLong("bytes", 0L),
            durationMs = if (o.isNull("durationMs")) null else o.optLong("durationMs"),
            missing = o.optBoolean("missing", false),
            nasPath = str(o, "nasPath"),
            reason = str(o, "reason"),
        )
    }

    fun login(json: String): LoginResult {
        val o = obj(json)
        val token = str(o, "token")
        if (token.isNullOrEmpty()) {
            // 200 却没有 token 是没法用的。当解析错误而不是登录失败：后者会让界面
            // 提示「口令不对」，而真正的问题是有人把反向代理指到了别的服务上。
            throw ApiParseException("登录成功但响应里没有 token")
        }
        val name = str(o, "name")
        if (name.isNullOrEmpty()) throw ApiParseException("登录响应里没有 name")
        return LoginResult(
            token = token,
            userId = str(o, "userId"),
            name = name,
            // 角色缺省按 viewer 处理，不按 admin：猜错方向必须是「权限更小」。
            // 界面上只用它决定显示什么，真正的权限判定在服务端。
            role = str(o, "role") ?: LoginResult.ROLE_VIEWER,
            grantAll = o.optBoolean("grantAll", false),
            // 运维凭证没有过期时刻，服务端如实返回 null。用 isNull 判而不是
            // optLong 的默认值 0：0 会被当成「1970 年就过期了」，于是那条路一登录
            // 就立刻被判失效。
            expiresAt = if (o.isNull("expiresAt")) null else o.optLong("expiresAt"),
        )
    }

    /**
     * `GET /v1/targets/manifest`。
     *
     * [TargetsManifest.version] 缺失时**抛异常**而不是给个空串：它是「这份元数据和
     * 那个库文件是配好的」的唯一判据，没有它这份 manifest 存下来也没法用，而静默存
     * 一个空版本会让每次 ETag 协商都拿到 200 重下整个库。
     *
     * 单条里 `printWidthM` 不可用的**保留，宽度记 0**（= 未知）。
     *
     * 这里原来是「跳过这一条」，理由是那个值会被用来贴视频、错了只会让画面一直飘。
     * 那个理由在尺度改成取 ARCore 的 `extentX` 之后不成立了（见 `Geometry.quadSize`）：
     * 宽度未知完全能正常贴合，而跳过的代价是这张照片**永远进不了端侧库**，于是离线
     * 命中这条路对它彻底失效 —— 每次都得往服务端跑一趟。
     */
    fun targetsManifest(json: String): TargetsManifest {
        val o = obj(json)
        val version = str(o, "version")?.takeIf { it.isNotEmpty() }
            ?: throw ApiParseException("targets manifest 里没有 version")
        val arr: JSONArray = o.optJSONArray("targets")
            ?: throw ApiParseException("targets manifest 里没有 targets 数组")
        val out = ArrayList<TargetEntry>(arr.length())
        for (i in 0 until arr.length()) {
            val e = arr.optJSONObject(i) ?: continue
            val id = str(e, "photoId")?.takeIf { it.isNotEmpty() } ?: continue
            val width = e.optDouble("printWidthM", 0.0).toFloat()
                .takeIf { it.isFinite() && it > 0f } ?: 0f
            out.add(
                TargetEntry(
                    photoId = id,
                    printWidthM = width,
                    refAspect = e.optDouble("refAspect", Double.NaN).toFloat()
                        .takeIf { it.isFinite() && it > 0f },
                    fitMode = str(e, "fitMode")?.takeIf { it.isNotEmpty() },
                    title = str(e, "title")?.takeIf { it.isNotEmpty() },
                    hasVideo = e.optBoolean("hasVideo", false),
                    mediaUrl = str(e, "mediaUrl")?.takeIf { it.isNotEmpty() }
                        ?: "/v1/photo/$id/media",
                    imgdbUrl = str(e, "imgdbUrl")?.takeIf { it.isNotEmpty() }
                        ?: "/v1/photo/$id/imgdb",
                ),
            )
        }
        return TargetsManifest(
            version = version,
            // count / overflow / maxTargets 是**服务端对那个库的陈述**，不拿
            // out.size 顶替：两者不一致说明契约破了，而那件事要能被看出来，
            // 不该被一个「反正我只解析出这么多」抹平。
            count = o.optInt("count", out.size),
            overflow = o.optInt("overflow", 0).coerceAtLeast(0),
            maxTargets = o.optInt("maxTargets", 0).coerceAtLeast(0),
            building = o.optBoolean("building", false),
            targets = out,
        )
    }

    fun me(json: String): AccountInfo {
        val o = obj(json)
        val name = str(o, "name") ?: throw ApiParseException("/v1/auth/me 没有 name")
        val role = str(o, "role") ?: LoginResult.ROLE_VIEWER
        return AccountInfo(
            userId = str(o, "userId"),
            name = name,
            role = role,
            grantAll = o.optBoolean("grantAll", false),
            // 服务端会显式给 isAdmin，但按 role 兜底 —— 两个字段说的是同一件事，
            // 缺了那个还有这个。
            isAdmin = o.optBoolean("isAdmin", role == LoginResult.ROLE_ADMIN),
        )
    }

    /**
     * 错误响应里的机器可读 code（服务端的 `error` 字段）。
     *
     * 与 [errorMessage] 分开取：message 是给人看的中文，会随服务端版本改；code 是契约
     * （`bad_credentials` / `unknown_user` / `account_disabled` / `unsupported_backend`
     * / `model_missing`），客户端按它分岔。拿 message 做字符串匹配的话，服务端改一个
     * 字就会把客户端的分支静默改掉。
     */
    fun errorCode(body: String?): String? {
        if (body.isNullOrBlank()) return null
        return try {
            str(JSONObject(body), "error")?.takeIf { it.isNotEmpty() }
        } catch (_: Exception) {
            null
        }
    }

    /** 错误响应里的 message，取不到就退回 HTTP 状态码。 */
    fun errorMessage(status: Int, body: String?): String {
        if (body.isNullOrBlank()) return "HTTP $status"
        return try {
            val o = JSONObject(body)
            str(o, "message") ?: str(o, "error") ?: "HTTP $status"
        } catch (_: Exception) {
            "HTTP $status"
        }
    }

    /**
     * 取一个可能为 null 的字符串字段。
     *
     * 不用 `optString(name, null)`：Android 自带的 org.json 对 JSON null 返回
     * 字符串 `"null"`，而 Maven 上的 org.json 返回 fallback —— 单测在 JVM 上
     * 用后者，真机上跑前者，同一段代码两种行为。`isNull()` 两边一致。
     */
    private fun str(o: JSONObject, name: String): String? =
        if (o.isNull(name)) null else o.optString(name, "")

    private fun obj(json: String): JSONObject =
        try {
            JSONObject(json)
        } catch (e: Exception) {
            throw ApiParseException("响应不是 JSON：${json.take(120)}")
        }
}

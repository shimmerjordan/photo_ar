package app.photoar.arview

import org.json.JSONObject

/**
 * §7 API 契约的客户端一侧：数据类型 + 解析 + URL 拼接。
 *
 * 这个文件里不许出现任何 android.* 依赖 —— 它是 JVM 单测能覆盖的部分。
 */

/** 两条通道（§4.1）。Phase 2 只从设置里读死值，Phase 3 换成 EndpointResolver。 */
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

/** 识别失败的分类。超时与其它错误的处理方式不同（§13）。 */
enum class NetErrorKind { TIMEOUT, UNAUTHORIZED, SERVER_ERROR, TRANSPORT, BAD_RESPONSE }

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
        val printWidthM = o.optDouble("printWidthM", 0.0).toFloat()
        if (!printWidthM.isFinite() || printWidthM <= 0f) {
            throw ApiParseException("printWidthM 不可用：${o.opt("printWidthM")}")
        }
        return RecognizeOutcome.Matched(
            Hit(
                photoId = photoId,
                inliers = o.optInt("inliers", 0),
                printWidthM = printWidthM,
                // refAspect 是可选的（参考图没有宽高记录时服务端不返回），
                // 缺了就退回 ARCore 自己量的比例，见 Geometry。
                refAspect = o.optDouble("refAspect", Double.NaN).toFloat()
                    .takeIf { it.isFinite() && it > 0f },
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

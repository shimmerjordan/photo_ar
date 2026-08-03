package app.photoar.arview

import org.json.JSONObject

/**
 * 登录状态：token 归谁、什么角色、什么时候过期，以及错误码怎么变成人话。
 *
 * 这个文件里不许出现任何 android.* 依赖 —— 「凭证怎么存、什么时候算过期、错误码怎么
 * 映射成文案」这三件事全在这里，而设置界面是 Compose，在这个项目里跑不起来也验不了。
 * 把它们留在 Composable 里等于放弃验证。
 */

/**
 * 那个 token 的元数据。
 *
 * **token 本身不在这里**，它仍然是 [EndpointConfig.token]。拆成两处保存一定会有一处
 * 忘记更新，而「元数据说是小明、Authorization 头里是管理员的 token」这种不一致查起来
 * 极其痛苦。这里只放「[EndpointConfig.token] 那个 token 的附加信息」。
 *
 * 于是 `token 非空 + auth 为 null` 是一个**合法**状态：Phase 3 手填令牌的老装机升上来
 * 就是这样（那时候界面上只有一个 PHOTOAR_TOKEN 输入框）。界面必须按「来路不明但可用的
 * 令牌」显示，不能当成没登录 —— 当成没登录会把一个本来还能扫的装机变成不能扫。
 *
 * @param expiresAt 服务端给的过期时刻（epoch 毫秒）。null = 不过期（运维凭证就是），
 *   **不是**「已过期」。
 */
data class AuthState(
    val name: String,
    val role: String,
    val userId: String? = null,
    val grantAll: Boolean = false,
    val expiresAt: Long? = null,
) {
    val isAdmin: Boolean get() = role == LoginResult.ROLE_ADMIN

    fun toJson(): JSONObject = JSONObject().apply {
        put("name", name)
        put("role", role)
        // JSONObject.put(String, null) 会**删掉**这个键而不是写 JSON null，所以两者
        // 在序列化上等价 —— 但显式写出来能让「这里可以是 null」在读代码时看得见。
        userId?.let { put("userId", it) }
        put("grantAll", grantAll)
        expiresAt?.let { put("expiresAt", it) }
    }

    companion object {
        fun fromJson(o: JSONObject): AuthState = AuthState(
            name = if (o.isNull("name")) "" else o.optString("name", ""),
            role = if (o.isNull("role")) LoginResult.ROLE_VIEWER
            else o.optString("role", LoginResult.ROLE_VIEWER),
            userId = if (o.isNull("userId")) null else o.optString("userId", ""),
            grantAll = o.optBoolean("grantAll", false),
            // 见 ApiParse.login：0 会被读成「1970 年就过期」。
            expiresAt = if (o.isNull("expiresAt")) null else o.optLong("expiresAt"),
        )

        fun of(login: LoginResult): AuthState = AuthState(
            name = login.name,
            role = login.role,
            userId = login.userId,
            grantAll = login.grantAll,
            expiresAt = login.expiresAt,
        )

        /**
         * 从 `GET /v1/auth/me` 认领一个来路不明的 token。
         *
         * [expiresAt] 只能是 null —— `me` 不返回过期时刻（它只回答"你是谁"）。
         * 拿"现在 + 30 天"去填是错的：那个 token 可能明天就过期，而界面会显示它还有
         * 一个月，于是用户在门口发现扫不出来。null 的含义是"不知道什么时候过期"，
         * 而 [AuthPolicy.phaseOf] 对它的处理正好是"当它有效直到被拒"，这与实际相符。
         */
        fun of(me: AccountInfo): AuthState = AuthState(
            name = me.name,
            role = me.role,
            userId = me.userId,
            grantAll = me.grantAll,
            expiresAt = null,
        )
    }
}

/** 当前凭证处于哪个阶段。界面按它决定显示登录表单还是「已登录」那一块。 */
enum class AuthPhase {
    /** 一个 token 都没有。 */
    LOGGED_OUT,

    /** 有 token 但不知道是谁的（Phase 3 手填的、或者迁移上来的）。仍然可用。 */
    UNKNOWN_TOKEN,

    ACTIVE,

    /**
     * 快过期了。
     *
     * 单独一个阶段是因为管理员的会话只有 12 小时 —— 「出门前发现要重新登录」比
     * 「站在照片前面发现扫不出来」好太多，而后者是没有这个提示时的必然结果。
     */
    EXPIRING_SOON,

    /** 按本机时钟已经过期。 */
    EXPIRED,
    ;

    /** 这个阶段还该不该拿去发请求。过期了就别发 —— 发出去只会换回一串 401。 */
    val usable: Boolean get() = this != LOGGED_OUT && this != EXPIRED
}

/** 登录相关的纯逻辑。设置界面里只剩渲染。 */
object AuthPolicy {

    /**
     * 提前多久算「快过期」。
     *
     * 取 1 小时：管理员会话 12 小时，1 小时约等于「你现在还有空重新登录一次」；访客
     * 30 天，1 小时对它可以忽略（那条几乎永远不会进这个阶段，也不需要）。
     */
    const val EXPIRING_SOON_MS = 60 * 60 * 1000L

    fun phaseOf(config: EndpointConfig, nowMs: Long): AuthPhase {
        if (config.token.isBlank()) return AuthPhase.LOGGED_OUT
        val auth = config.auth ?: return AuthPhase.UNKNOWN_TOKEN
        val expires = auth.expiresAt ?: return AuthPhase.ACTIVE
        return when {
            expires <= nowMs -> AuthPhase.EXPIRED
            expires - nowMs <= EXPIRING_SOON_MS -> AuthPhase.EXPIRING_SOON
            else -> AuthPhase.ACTIVE
        }
    }

    /** 登录成功之后的新配置。token 与元数据在同一次赋值里换掉，不可能只换一半。 */
    fun applyLogin(config: EndpointConfig, login: LoginResult): EndpointConfig =
        config.copy(token = login.token, auth = AuthState.of(login))

    /**
     * 退出登录之后的新配置。
     *
     * token 与元数据一起清空。只清元数据的话，Authorization 头里还会带着那个 token ——
     * 服务端那边可能已经 logout 了（于是 401），也可能没有（于是「退出登录」根本没生效，
     * 而界面显示已退出）。两种都是错的。
     */
    fun applyLogout(config: EndpointConfig): EndpointConfig =
        config.copy(token = "", auth = null)

    /**
     * 登录失败的中文文案。
     *
     * 按**服务端 error code** 分岔而不是按 message 匹配字符串：code 是契约，message 是
     * 给人看的中文，服务端改一个字就会把这里的分支静默改掉。
     *
     * @param code 服务端 `error` 字段。
     * @param serverMessage 服务端的 message，作为兜底显示（它通常比我们的通用文案更具体，
     *   比如「没有这个用户：'小名'」里带着规范化之后的名字，那正是用户输错了什么的证据）。
     */
    fun loginMessage(kind: NetErrorKind, code: String?, serverMessage: String?): String =
        when (code) {
            CODE_BAD_CREDENTIALS -> "口令不对。管理员必须填口令，访客留空。"
            CODE_UNKNOWN_USER ->
                "这个名字不在册。账号只能由管理员在管理台建，再试一次也不会成 —— " +
                    "先确认名字有没有打错（全角/半角、多余空格都会被服务端规范化掉，" +
                    "所以不是那些原因）。"
            CODE_ACCOUNT_DISABLED -> "账号已被停用。得让管理员先启用，重试没用。"
            else -> when (kind) {
                // 没有 code 的 401/403：可能是反向代理自己挡下来的（登录页、Basic 认证），
                // 那时服务端那份 message 完全不相干，所以把状态说清楚更有用。
                NetErrorKind.BAD_CREDENTIALS -> "口令不对。"
                NetErrorKind.FORBIDDEN -> "服务端拒绝了这次登录：${serverMessage ?: "没说原因"}"
                NetErrorKind.UNAUTHORIZED ->
                    "凭证不被接受。如果地址前面有反向代理，先确认它没有自己拦一层认证。"
                NetErrorKind.TIMEOUT -> "连不上（超时）。换一条通道，或者检查服务端在不在。"
                NetErrorKind.TRANSPORT -> "连不上：${serverMessage ?: "网络不通"}"
                NetErrorKind.SERVER_ERROR -> "服务端出错了：${serverMessage ?: "5xx"}"
                NetErrorKind.BAD_RESPONSE ->
                    "响应看不懂：${serverMessage ?: "不是这个服务的接口"}。" +
                        "最常见的原因是地址填成了别的服务。"
            }
        }

    /** 界面上「当前登录者」那一行。 */
    fun describe(config: EndpointConfig, nowMs: Long): String {
        val auth = config.auth
        return when (phaseOf(config, nowMs)) {
            AuthPhase.LOGGED_OUT -> "未登录"
            AuthPhase.UNKNOWN_TOKEN -> "已有令牌（旧版手填的，不知道归属）"
            AuthPhase.EXPIRED -> "${auth?.name ?: "?"} · 登录已过期，请重新登录"
            AuthPhase.EXPIRING_SOON ->
                "${auth?.name ?: "?"} · ${roleText(auth?.role)} · 即将过期"
            AuthPhase.ACTIVE -> "${auth?.name ?: "?"} · ${roleText(auth?.role)}"
        }
    }

    fun roleText(role: String?): String = when (role) {
        LoginResult.ROLE_ADMIN -> "管理员"
        LoginResult.ROLE_VIEWER -> "访客"
        null -> "未知角色"
        else -> role
    }

    // 服务端 `_auth_login` 抛出的三个 code。字面量与 app.py 里一一对应。
    const val CODE_BAD_CREDENTIALS = "bad_credentials"
    const val CODE_UNKNOWN_USER = "unknown_user"
    const val CODE_ACCOUNT_DISABLED = "account_disabled"
}

package app.photoar.arview

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 登录状态的纯逻辑。
 *
 * 设置界面是 Compose，在这个项目里跑不起来也验不了 —— 所以「凭证怎么存、什么时候算
 * 过期、错误码怎么映射成文案」这三件事全被抽到 [AuthPolicy] / [AuthState] 里，
 * 这个文件就是它们的验收。Composable 里剩下的只有渲染。
 */
class AuthTest {

    private val hour = 3_600_000L
    private val now = 1_700_000_000_000L

    private fun config(
        token: String = "t",
        auth: AuthState? = AuthState(name = "小明", role = "viewer", expiresAt = now + 30 * 24 * hour),
    ) = EndpointConfig(
        candidates = listOf(EndpointCandidate("LAN", "http://10.0.0.9:8964")),
        token = token,
        auth = auth,
    )

    // ---- 阶段判定 ----

    @Test
    fun `没有 token 就是未登录`() {
        assertEquals(AuthPhase.LOGGED_OUT, AuthPolicy.phaseOf(config(token = ""), now))
        assertEquals(AuthPhase.LOGGED_OUT, AuthPolicy.phaseOf(config(token = "   "), now))
    }

    @Test
    fun `有 token 但没有元数据是「来路不明的令牌」而不是未登录`() {
        // Phase 3 手填令牌的老装机升上来就是这个状态。当成未登录会把一个本来还能扫的
        // 装机变成不能扫。
        val phase = AuthPolicy.phaseOf(config(auth = null), now)
        assertEquals(AuthPhase.UNKNOWN_TOKEN, phase)
        assertTrue("这个状态必须仍然可用", phase.usable)
    }

    @Test
    fun `expiresAt 为 null 表示不过期而不是已过期`() {
        // 运维凭证（PHOTOAR_TOKEN）没有服务端状态，服务端如实返回 null。
        val c = config(auth = AuthState(name = "[运维凭证]", role = "admin", expiresAt = null))
        assertEquals(AuthPhase.ACTIVE, AuthPolicy.phaseOf(c, now))
    }

    @Test
    fun `过期时刻已过就是 EXPIRED`() {
        val c = config(auth = AuthState("小明", "viewer", expiresAt = now - 1))
        val phase = AuthPolicy.phaseOf(c, now)
        assertEquals(AuthPhase.EXPIRED, phase)
        assertFalse("过期了就不该再拿去发请求", phase.usable)
    }

    @Test
    fun `正好到点算过期`() {
        val c = config(auth = AuthState("小明", "viewer", expiresAt = now))
        assertEquals(AuthPhase.EXPIRED, AuthPolicy.phaseOf(c, now))
    }

    @Test
    fun `一小时之内到期算即将过期`() {
        // 管理员会话只有 12 小时。「出门前发现要重新登录」比「站在照片前面发现扫不
        // 出来」好太多，而后者是没有这个阶段时的必然结果。
        val c = config(auth = AuthState("管理员", "admin", expiresAt = now + 30 * 60_000L))
        val phase = AuthPolicy.phaseOf(c, now)
        assertEquals(AuthPhase.EXPIRING_SOON, phase)
        assertTrue("还没过期，仍然能用", phase.usable)
    }

    @Test
    fun `刚好一小时零一毫秒还算正常`() {
        val c = config(auth = AuthState("管理员", "admin", expiresAt = now + AuthPolicy.EXPIRING_SOON_MS + 1))
        assertEquals(AuthPhase.ACTIVE, AuthPolicy.phaseOf(c, now))
    }

    // ---- 登录 / 登出的状态迁移 ----

    @Test
    fun `登录把 token 与元数据一起换掉`() {
        val login = LoginResult(
            token = "new-token",
            userId = "u1",
            name = "管理员",
            role = "admin",
            grantAll = false,
            expiresAt = now + 12 * hour,
        )
        val next = AuthPolicy.applyLogin(config(token = "old"), login)
        assertEquals("new-token", next.token)
        assertEquals("管理员", next.auth!!.name)
        assertEquals("admin", next.auth!!.role)
        assertEquals("u1", next.auth!!.userId)
        assertEquals(now + 12 * hour, next.auth!!.expiresAt)
        assertEquals(AuthPhase.ACTIVE, AuthPolicy.phaseOf(next, now))
        // 候选列表不能被顺手改掉
        assertEquals(config().candidates, next.candidates)
    }

    @Test
    fun `登出把 token 也清掉而不只是元数据`() {
        // 只清元数据的话，Authorization 头里还带着那个 token：服务端可能已经作废了
        // （于是 401），也可能没有（于是「退出登录」根本没生效，而界面显示已退出）。
        val next = AuthPolicy.applyLogout(config())
        assertEquals("", next.token)
        assertNull(next.auth)
        assertEquals(AuthPhase.LOGGED_OUT, AuthPolicy.phaseOf(next, now))
    }

    @Test
    fun `登出不动候选列表与端上特征开关`() {
        val c = config().copy(onDeviceFeatures = true)
        val next = AuthPolicy.applyLogout(c)
        assertEquals(c.candidates, next.candidates)
        assertTrue(next.onDeviceFeatures)
    }

    @Test
    fun `从 me 认领一个来路不明的 token 时过期时刻必须是 null`() {
        // `me` 不返回过期时刻。拿「现在 + 30 天」去填是错的：那个 token 可能明天就过期，
        // 而界面会显示它还有一个月 —— 于是用户在门口才发现扫不出来。
        val me = AccountInfo(
            userId = "u3",
            name = "小刚",
            role = "viewer",
            grantAll = true,
            isAdmin = false,
        )
        val s = AuthState.of(me)
        assertNull(s.expiresAt)
        assertEquals("小刚", s.name)
        assertEquals("u3", s.userId)
        assertTrue(s.grantAll)
        // null 的含义是「不知道什么时候过期」，而 phaseOf 对它的处理是「当它有效」
        assertEquals(AuthPhase.ACTIVE, AuthPolicy.phaseOf(config(auth = s), now))
    }

    // ---- 错误码 → 文案 ----

    @Test
    fun `口令错和名字不在册给不同文案`() {
        val bad = AuthPolicy.loginMessage(
            NetErrorKind.BAD_CREDENTIALS,
            AuthPolicy.CODE_BAD_CREDENTIALS,
            "口令不对",
        )
        val unknown = AuthPolicy.loginMessage(
            NetErrorKind.FORBIDDEN,
            AuthPolicy.CODE_UNKNOWN_USER,
            "没有这个用户：'小名'",
        )
        assertTrue(bad.contains("口令"))
        assertTrue("要说清重试没用", unknown.contains("再试一次也不会成"))
        assertTrue("两者必须是不同的话", bad != unknown)
    }

    @Test
    fun `账号停用的文案要说清得管理员先启用`() {
        val text = AuthPolicy.loginMessage(
            NetErrorKind.FORBIDDEN,
            AuthPolicy.CODE_ACCOUNT_DISABLED,
            "账号已停用：'小明'",
        )
        assertTrue(text.contains("停用"))
        assertTrue(text.contains("管理员"))
    }

    @Test
    fun `没有 code 时按 kind 兜底且每种都有话说`() {
        // 反向代理自己挡下来时不会有我们的 error 字段。此时不能给出空文案。
        for (kind in NetErrorKind.entries) {
            val text = AuthPolicy.loginMessage(kind, null, "原文")
            assertTrue("$kind 没有文案", text.isNotBlank())
        }
    }

    @Test
    fun `不认识的 code 也不会漏出空文案`() {
        val text = AuthPolicy.loginMessage(NetErrorKind.FORBIDDEN, "brand_new_code", "服务端说的")
        assertTrue(text.isNotBlank())
        assertTrue("兜底要把服务端原文带出来", text.contains("服务端说的"))
    }

    @Test
    fun `重试有没有意义的分类`() {
        assertFalse(NetErrorKind.UNAUTHORIZED.retryable)
        assertFalse(NetErrorKind.BAD_CREDENTIALS.retryable)
        assertFalse(NetErrorKind.FORBIDDEN.retryable)
        assertFalse(NetErrorKind.BAD_RESPONSE.retryable)
        assertTrue(NetErrorKind.TIMEOUT.retryable)
        assertTrue(NetErrorKind.TRANSPORT.retryable)
        assertTrue(NetErrorKind.SERVER_ERROR.retryable)
    }

    // ---- 描述文案 ----

    @Test
    fun `当前登录者那一行按阶段变`() {
        assertEquals("未登录", AuthPolicy.describe(config(token = ""), now))
        assertTrue(AuthPolicy.describe(config(auth = null), now).contains("旧版"))
        assertTrue(
            AuthPolicy.describe(config(), now).let { it.contains("小明") && it.contains("访客") },
        )
        val admin = config(auth = AuthState("管理员", "admin", expiresAt = now + hour * 5))
        assertTrue(AuthPolicy.describe(admin, now).contains("管理员"))
        val expired = config(auth = AuthState("小明", "viewer", expiresAt = now - 1))
        assertTrue(AuthPolicy.describe(expired, now).contains("过期"))
    }

    @Test
    fun `未知角色不会显示成空白`() {
        assertEquals("未知角色", AuthPolicy.roleText(null))
        assertEquals("superuser", AuthPolicy.roleText("superuser"))
    }

    // ---- 持久化 ----

    @Test
    fun `AuthState 的 JSON 往返`() {
        val a = AuthState("小明", "viewer", userId = "u9", grantAll = true, expiresAt = 12345L)
        val back = AuthState.fromJson(JSONObject(a.toJson().toString()))
        assertEquals(a, back)
    }

    @Test
    fun `AuthState 的 null 字段往返之后还是 null`() {
        val a = AuthState("[运维凭证]", "admin", userId = null, expiresAt = null)
        val back = AuthState.fromJson(JSONObject(a.toJson().toString()))
        assertNull(back.userId)
        assertNull("null 不能变成 0，否则一登录就被判过期", back.expiresAt)
        assertEquals(a, back)
    }

    @Test
    fun `EndpointConfig 带凭证一起落盘再读回来`() {
        val c = EndpointConfig(
            candidates = listOf(
                EndpointCandidate("LAN", "http://10.0.0.9:8964", listOf(EndpointUse.API)),
                EndpointCandidate("Tunnel", "https://x.example.com", tunnel = true),
            ),
            token = "tok",
            auth = AuthState("管理员", "admin", "u1", grantAll = false, expiresAt = 999L),
            onDeviceFeatures = true,
        )
        val back = EndpointConfig.parse(c.toJson())
        assertEquals(c, back)
    }

    @Test
    fun `旧配置没有 auth 与 onDeviceFeatures 两个键时按缺省读`() {
        // 升级前存下来的那份配置。auth 缺 → UNKNOWN_TOKEN（仍可用）；
        // onDeviceFeatures 缺 → false（现状那条路）。
        val old = """{"token":"legacy","endpoints":[{"name":"LAN","base":"http://a"}]}"""
        val c = EndpointConfig.parse(old)
        assertEquals("legacy", c.token)
        assertNull(c.auth)
        assertFalse(c.onDeviceFeatures)
        assertEquals(AuthPhase.UNKNOWN_TOKEN, AuthPolicy.phaseOf(c, now))
    }

    @Test
    fun `坏掉的 auth 块不会把整份配置弄废`() {
        val json = """{"token":"t","auth":123,"endpoints":[{"name":"LAN","base":"http://a"}]}"""
        val c = EndpointConfig.parse(json)
        assertEquals("t", c.token)
        assertNull("auth 不是对象就当没有", c.auth)
        assertEquals(1, c.candidates.size)
    }
}

package app.photoar.arview

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * §14.4 点名要的那份测试：「`EndpointResolver`：多 endpoint 各种通/不通组合下的
 * api/media 选择结果（表驱动，覆盖 §9.3 全部场景）」。
 */
class EndpointResolverTest {

    private val lan = EndpointCandidate(
        "LAN", "http://192.168.1.20:8964", listOf(EndpointUse.MEDIA, EndpointUse.API),
    )
    private val tailscale = EndpointCandidate(
        "Tailscale", "http://100.1.2.3:8964", listOf(EndpointUse.MEDIA),
    )
    private val tunnel = EndpointCandidate(
        "Tunnel", "https://arphoto.example.org", listOf(EndpointUse.API), tunnel = true,
    )
    private val ddns = EndpointCandidate(
        "DDNS", "", listOf(EndpointUse.MEDIA, EndpointUse.API), enabled = false,
    )
    private val all = listOf(lan, tailscale, tunnel, ddns)

    private val pool: ExecutorService = Executors.newFixedThreadPool(4)
    private var now = 1_000L

    @After
    fun tearDown() {
        pool.shutdownNow()
    }

    private fun resolver(
        candidates: List<EndpointCandidate> = all,
        reachable: Set<String>,
        token: String = "t",
    ): EndpointResolver = EndpointResolver(
        prober = { base, _ -> if (base in reachable) 12L else null },
        executor = pool,
        clock = { now },
        initial = EndpointConfig(candidates, token),
    )

    // ---- §9.3 那张表，一行一个测试 ----

    @Test
    fun `在家 同局域网 两条都走 LAN`() {
        // 在家时隧道也是通的（走出去再回来），Tailscale 通常也在线 —— 三条全通
        // 才是这一行的真实情况，而不是「只有 LAN 通」。
        val r = resolver(reachable = setOf(lan.base, tailscale.base, tunnel.base)).refresh()
        assertEquals("LAN", r.api?.candidate?.name)
        assertEquals("LAN", r.media?.candidate?.name)
        assertFalse(r.offline)
    }

    @Test
    fun `在外 Tailscale 在线 api 走隧道 media 走 Tailscale`() {
        val r = resolver(reachable = setOf(tailscale.base, tunnel.base)).refresh()
        assertEquals("Tunnel", r.api?.candidate?.name)
        assertEquals("Tailscale", r.media?.candidate?.name)
    }

    @Test
    fun `在外 Tailscale 未登录 两条都走隧道`() {
        // 这一行全靠 §9.2 的兜底半句：偏好 media 的 LAN 与 Tailscale 都不通，
        // media 只能落到唯一通着的 Tunnel 上。没有兜底这里会是 null。
        val r = resolver(reachable = setOf(tunnel.base)).refresh()
        assertEquals("Tunnel", r.api?.candidate?.name)
        assertEquals("Tunnel", r.media?.candidate?.name)
    }

    @Test
    fun `断网 两条都是空`() {
        val r = resolver(reachable = emptySet()).refresh()
        assertNull(r.api)
        assertNull(r.media)
        assertTrue(r.offline)
    }

    // ---- 选择规则本身 ----

    @Test
    fun `只有 LAN 通时 api 也走 LAN`() {
        val r = resolver(reachable = setOf(lan.base)).refresh()
        assertEquals("LAN", r.api?.candidate?.name)
        assertEquals("LAN", r.media?.candidate?.name)
    }

    @Test
    fun `只有 Tailscale 通时 api 兜底也走 Tailscale`() {
        // Tailscale 的 prefer 里没有 api，但它是唯一通的 —— 兜底规则让识别仍能用
        val r = resolver(reachable = setOf(tailscale.base)).refresh()
        assertEquals("Tailscale", r.api?.candidate?.name)
        assertEquals("Tailscale", r.media?.candidate?.name)
    }

    @Test
    fun `停用的候选即便地址通也不选`() {
        val enabledTunnel = tunnel.copy(enabled = false)
        val r = resolver(
            candidates = listOf(lan, enabledTunnel),
            reachable = setOf(tunnel.base),
        ).refresh()
        assertNull(r.api)
        assertNull(r.media)
    }

    @Test
    fun `地址留空的候选不发探活请求`() {
        var probes = 0
        val res = EndpointResolver(
            prober = { _, _ -> probes++; 5L },
            executor = pool,
            clock = { now },
            initial = EndpointConfig(listOf(lan, ddns), "t"),
        )
        res.refresh()
        assertEquals(1, probes)
    }

    @Test
    fun `列表顺序决定同偏好之间的胜负`() {
        // 两条都声明 media，谁在前谁赢 —— 顺序就是用户在设置里排的优先级
        val second = tailscale.copy(name = "Second")
        val a = resolver(
            candidates = listOf(tailscale, second),
            reachable = setOf(tailscale.base),
        ).refresh()
        assertEquals("Tailscale", a.media?.candidate?.name)

        val b = resolver(
            candidates = listOf(second, tailscale),
            reachable = setOf(tailscale.base),
        ).refresh()
        assertEquals("Second", b.media?.candidate?.name)
    }

    @Test
    fun `prefer 为空的候选只能靠兜底被选中`() {
        val plain = EndpointCandidate("Plain", "http://plain", emptyList())
        val r = resolver(
            candidates = listOf(plain, tunnel),
            reachable = setOf(plain.base, tunnel.base),
        ).refresh()
        // api 偏好命中 Tunnel；media 没人偏好，兜底取列表第一个通的 = Plain
        assertEquals("Tunnel", r.api?.candidate?.name)
        assertEquals("Plain", r.media?.candidate?.name)
    }

    @Test
    fun `探活抛异常算不通并记下原因`() {
        val res = EndpointResolver(
            prober = { _, _ -> throw IllegalStateException("DNS 挂了") },
            executor = pool,
            clock = { now },
            initial = EndpointConfig(listOf(lan), "t"),
        )
        val r = res.refresh()
        assertNull(r.api)
        assertEquals("DNS 挂了", r.probed.single().error)
    }

    @Test
    fun `不通的候选也留在 probed 里给设置界面看`() {
        val r = resolver(reachable = setOf(tunnel.base)).refresh()
        assertEquals(4, r.probed.size)
        assertEquals(listOf("LAN", "Tailscale", "Tunnel", "DDNS"), r.probed.map { it.candidate.name })
        assertEquals("不通", r.probed[0].error)
        assertEquals("已停用", r.probed[3].error)
    }

    @Test
    fun `探活超时时间是 1500ms`() {
        var seen = -1
        val res = EndpointResolver(
            prober = { _, t -> seen = t; 1L },
            executor = pool,
            clock = { now },
            initial = EndpointConfig(listOf(lan), "t"),
        )
        res.refresh()
        assertEquals(1_500, seen)
        assertEquals(1_500, EndpointResolver.PROBE_TIMEOUT_MS)
    }

    // ---- 节流 ----

    @Test
    fun `三秒内的重复探活被节流挡掉`() {
        var probes = 0
        val res = EndpointResolver(
            prober = { _, _ -> probes++; 5L },
            executor = pool,
            clock = { now },
            initial = EndpointConfig(listOf(lan), "t"),
        )
        val first = res.refresh()
        now += 2_000
        val second = res.refresh()
        assertEquals(1, probes)
        assertSame(first, second)

        now += 1_500 // 累计 3.5s，过了门限
        res.refresh()
        assertEquals(2, probes)
    }

    @Test
    fun `手动刷新跳过节流`() {
        var probes = 0
        val res = EndpointResolver(
            prober = { _, _ -> probes++; 5L },
            executor = pool,
            clock = { now },
            initial = EndpointConfig(listOf(lan), "t"),
        )
        res.refresh()
        res.refresh(force = true)
        assertEquals(2, probes)
    }

    @Test
    fun `换配置会清掉旧结果并解除节流`() {
        var probes = 0
        val res = EndpointResolver(
            prober = { _, _ -> probes++; 5L },
            executor = pool,
            clock = { now },
            initial = EndpointConfig(listOf(lan), "t"),
        )
        res.refresh()
        assertNotNull(res.resolution)

        res.update(EndpointConfig(listOf(tunnel), "t2"))
        assertNull(res.resolution) // 旧的探活结果对新列表没有意义
        res.refresh() // 没被节流挡住
        assertEquals(2, probes)
        assertEquals("Tunnel", res.resolution?.api?.candidate?.name)
    }

    // ---- endpoints() / viaLabel() / uploadAllowed() ----

    @Test
    fun `endpoints 给出两条通道和 token`() {
        val res = resolver(reachable = setOf(tailscale.base, tunnel.base), token = "sekret")
        res.refresh()
        val ep = res.endpoints()
        assertEquals(tunnel.base, ep.apiBase)
        assertEquals(tailscale.base, ep.mediaBase)
        assertEquals("sekret", ep.token)
    }

    @Test
    fun `断网时 endpoints 退回第一个可用候选而不是空串`() {
        // 空 base 会让 URL 拼出 "/v1/recognize" 这种不合法的东西；退回一个真地址，
        // 失败就落在 TRANSPORT 上，状态机本来就会静默重试。
        val res = resolver(reachable = emptySet())
        res.refresh()
        assertEquals(lan.base, res.endpoints().apiBase)
        assertEquals(lan.base, res.endpoints().mediaBase)
    }

    @Test
    fun `没探活过时 endpoints 也能用`() {
        val res = resolver(reachable = setOf(lan.base))
        assertEquals(lan.base, res.endpoints().apiBase)
        assertNull(res.viaLabel())
    }

    @Test
    fun `viaLabel 是 api 通道名字的小写`() {
        val res = resolver(reachable = setOf(tunnel.base))
        res.refresh()
        assertEquals("tunnel", res.viaLabel())
    }

    @Test
    fun `media 走隧道时不给上传`() {
        val res = resolver(reachable = setOf(tunnel.base))
        res.refresh()
        assertFalse(res.uploadAllowed())
    }

    @Test
    fun `media 走 Tailscale 时可以上传`() {
        val res = resolver(reachable = setOf(tailscale.base, tunnel.base))
        res.refresh()
        assertTrue(res.uploadAllowed())
    }

    @Test
    fun `没探活过时不给上传`() {
        assertFalse(resolver(reachable = setOf(lan.base)).uploadAllowed())
    }

    // ---- 配置序列化 ----

    @Test
    fun `配置往返不丢字段`() {
        val cfg = EndpointConfig(all, "tok")
        val back = EndpointConfig.parse(cfg.toJson())
        assertEquals(cfg, back)
    }

    @Test
    fun `解析非 JSON 退回默认配置`() {
        assertEquals(EndpointConfig.DEFAULT, EndpointConfig.parse("这不是 JSON"))
    }

    @Test
    fun `缺 endpoints 数组时保留 token 并用默认列表`() {
        val cfg = EndpointConfig.parse("""{"token":"abc"}""")
        assertEquals("abc", cfg.token)
        assertEquals(EndpointConfig.DEFAULT.candidates, cfg.candidates)
    }

    @Test
    fun `空 endpoints 数组也退回默认列表`() {
        // 用户在界面上把最后一条删掉了：给他默认列表而不是一个什么都干不了的 App
        val cfg = EndpointConfig.parse("""{"token":"a","endpoints":[]}""")
        assertEquals(EndpointConfig.DEFAULT.candidates, cfg.candidates)
    }

    @Test
    fun `解析时 base 末尾的斜杠被去掉`() {
        val cfg = EndpointConfig.parse(
            """{"token":"a","endpoints":[{"name":"X","base":"http://h:1/","prefer":["api"]}]}"""
        )
        assertEquals("http://h:1", cfg.candidates.single().base)
    }

    @Test
    fun `prefer 里认不出来的用途被丢掉而不是弄废整份配置`() {
        val cfg = EndpointConfig.parse(
            """{"token":"a","endpoints":[{"name":"X","base":"http://h","prefer":["api","typo","MEDIA"]}]}"""
        )
        assertEquals(listOf(EndpointUse.API, EndpointUse.MEDIA), cfg.candidates.single().prefer)
    }

    @Test
    fun `prefer 里重复的用途只留一份`() {
        val cfg = EndpointConfig.parse(
            """{"token":"a","endpoints":[{"name":"X","base":"http://h","prefer":["api","api"]}]}"""
        )
        assertEquals(listOf(EndpointUse.API), cfg.candidates.single().prefer)
    }

    @Test
    fun `没写 tunnel 字段时按名字猜`() {
        val cfg = EndpointConfig.parse(
            """{"token":"a","endpoints":[
                 {"name":"tunnel","base":"http://a"},
                 {"name":"LAN","base":"http://b"}]}"""
        )
        assertTrue(cfg.candidates[0].tunnel)
        assertFalse(cfg.candidates[1].tunnel)
    }

    @Test
    fun `显式写 tunnel false 能盖掉按名字的猜测`() {
        val cfg = EndpointConfig.parse(
            """{"token":"a","endpoints":[{"name":"Tunnel","base":"http://a","tunnel":false}]}"""
        )
        assertFalse(cfg.candidates.single().tunnel)
    }

    @Test
    fun `token 为 JSON null 时是空串不是字符串 null`() {
        // Android 自带 org.json 的 optString(k, null) 对 JSON null 返回 "null"
        val cfg = EndpointConfig.parse("""{"token":null,"endpoints":[]}""")
        assertEquals("", cfg.token)
    }

    @Test
    fun `默认配置的四项与 §9_1 一致`() {
        val d = EndpointConfig.DEFAULT.candidates
        assertEquals(listOf("LAN", "Tailscale", "Tunnel", "DDNS"), d.map { it.name })
        assertEquals(listOf(EndpointUse.MEDIA, EndpointUse.API), d[0].prefer)
        assertEquals(listOf(EndpointUse.MEDIA), d[1].prefer)
        assertEquals(listOf(EndpointUse.API), d[2].prefer)
        assertTrue(d[2].tunnel)
        assertFalse(d[3].enabled) // DDNS 是预留空位
    }

    @Test
    fun `viaLabel 对空名字有兜底`() {
        assertEquals("unnamed", EndpointCandidate("  ", "http://h").viaLabel)
    }

    // ---- Phase 2 三个死值的迁移 ----

    /** 迁移出来的配置在「所有填了地址的通道都通」时选到了谁。 */
    private fun resolveAllUp(cfg: EndpointConfig): Resolution =
        resolver(
            candidates = cfg.candidates,
            reachable = cfg.candidates.filter { it.usable }.map { it.base }.toSet(),
        ).refresh()

    @Test
    fun `fromLegacy 两个 base 相同时只留一条并同时承担 api 与 media`() {
        val cfg = EndpointConfig.fromLegacy("http://10.0.0.9:8770", "http://10.0.0.9:8770", "tok")
        assertEquals("tok", cfg.token)
        val real = cfg.candidates.filter { it.usable }
        assertEquals(1, real.size)
        assertEquals("http://10.0.0.9:8770", real[0].base)
        assertEquals(listOf(EndpointUse.MEDIA, EndpointUse.API), real[0].prefer)

        val r = resolveAllUp(cfg)
        assertEquals("http://10.0.0.9:8770", r.api!!.candidate.base)
        assertEquals("http://10.0.0.9:8770", r.media!!.candidate.base)
    }

    @Test
    fun `fromLegacy 媒体地址留空等同于与 api 同源`() {
        val cfg = EndpointConfig.fromLegacy("http://a", "", "tok")
        assertEquals(1, cfg.candidates.count { it.usable })
        assertEquals(listOf(EndpointUse.MEDIA, EndpointUse.API), cfg.candidates[0].prefer)
    }

    @Test
    fun `fromLegacy 两个 base 不同时拆成两条各司其职`() {
        // Phase 2 把媒体单独配出来的唯一理由就是「大流量别走隧道」，正好是
        // prefer=[media]。
        val cfg = EndpointConfig.fromLegacy("https://ar.example.com", "http://10.0.0.9:8770", "tok")
        val real = cfg.candidates.filter { it.usable }
        assertEquals(2, real.size)
        assertEquals(listOf(EndpointUse.API), real[0].prefer)
        assertEquals(listOf(EndpointUse.MEDIA), real[1].prefer)

        val r = resolveAllUp(cfg)
        assertEquals("https://ar.example.com", r.api!!.candidate.base)
        assertEquals("http://10.0.0.9:8770", r.media!!.candidate.base)
    }

    @Test
    fun `fromLegacy 带上默认里那几个空位`() {
        // 升级后设置界面仍然是「填一格就能用」，不需要自己想出要加哪几条。
        val cfg = EndpointConfig.fromLegacy("http://a", "", "tok")
        assertTrue(cfg.candidates.size > 1)
        assertTrue(cfg.candidates.drop(1).all { it.base.isEmpty() })
        assertEquals(
            EndpointConfig.DEFAULT.candidates.map { it.name },
            cfg.candidates.drop(1).map { it.name },
        )
    }

    @Test
    fun `fromLegacy 什么都没存过时就是默认值`() {
        val cfg = EndpointConfig.fromLegacy("", "", "")
        assertEquals(EndpointConfig.DEFAULT, cfg)
    }

    @Test
    fun `fromLegacy 只存过令牌时保留令牌`() {
        // Phase 2 的界面要求地址和令牌都填才让进，但 prefs 里可能只剩令牌
        // （改了地址没点保存就退出）。丢掉令牌会让人以为要重新去 NAS 上抄一遍。
        val cfg = EndpointConfig.fromLegacy("", "", "tok")
        assertEquals("tok", cfg.token)
        assertEquals(EndpointConfig.DEFAULT.candidates, cfg.candidates)
    }

    @Test
    fun `fromLegacy 去掉末尾斜杠与空白`() {
        val cfg = EndpointConfig.fromLegacy("  http://a/  ", "  ", "tok")
        assertEquals("http://a", cfg.candidates[0].base)
    }

    @Test
    fun `fromLegacy 的结果能原样存取一轮`() {
        val cfg = EndpointConfig.fromLegacy("https://ar.example.com", "http://10.0.0.9:8770", "tok")
        assertEquals(cfg, EndpointConfig.parse(cfg.toJson()))
    }
}

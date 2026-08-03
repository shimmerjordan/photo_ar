package app.photoar.arview.ar

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 内置 ARCore 运行时的决策逻辑。
 *
 * 这些用例真正防的是三种「转不出来的圈」和一种「静默的等待」——
 * 都是这个界面上出现过或差点出现的故障，不是为覆盖率凑的。
 */
class ArInstallPolicyTest {

    private fun ctx(
        state: ArRuntimeState,
        bundled: Boolean = true,
        sessionAttempted: Boolean = false,
        legacyAttempted: Boolean = false,
        canInstallPackages: Boolean = true,
        permissionAsked: Boolean = false,
        checks: Int = 0,
    ) = ArInstallContext(
        state = state,
        bundled = bundled,
        sessionAttempted = sessionAttempted,
        legacyAttempted = legacyAttempted,
        canInstallPackages = canInstallPackages,
        permissionAsked = permissionAsked,
        checks = checks,
    )

    @Test
    fun `装好了就直接开 AR`() {
        assertEquals(
            ArAction.START_AR,
            ArInstallPolicy.decide(ctx(ArRuntimeState.INSTALLED)),
        )
    }

    @Test
    fun `硬件不支持时不装 直接兜底`() {
        // 这台机器不在 ARCore 的标定档案里，装上运行时也开不了会话。
        // 唯一一个「有内置包也不该装」的分支。
        assertEquals(
            ArAction.FALLBACK,
            ArInstallPolicy.decide(ctx(ArRuntimeState.DEVICE_NOT_CAPABLE)),
        )
    }

    @Test
    fun `没装过就装内置那份`() {
        assertEquals(
            ArAction.INSTALL_BUNDLED,
            ArInstallPolicy.decide(ctx(ArRuntimeState.NOT_INSTALLED)),
        )
    }

    @Test
    fun `版本太旧也装内置那份`() {
        // 内置版本 == 客户端库要求的版本（build.gradle.kts 里的单一版本号），
        // 所以装上就能修好 TOO_OLD。
        assertEquals(
            ArAction.INSTALL_BUNDLED,
            ArInstallPolicy.decide(ctx(ArRuntimeState.TOO_OLD)),
        )
    }

    @Test
    fun `查不出来时选择装 而不是直接兜底`() {
        // UNKNOWN 的最常见原因就是连不上 Google 的机型档案服务 ——
        // 恰好是宾客手机没有 Google 框架的情况。装上本地运行时后这个查询
        // 就能在本机得到答案，所以这里必须是「装」。
        assertEquals(
            ArAction.INSTALL_BUNDLED,
            ArInstallPolicy.decide(ctx(ArRuntimeState.UNKNOWN)),
        )
    }

    @Test
    fun `包里没有内置运行时就兜底`() {
        // assets 被裁、或者被塞进了别的外壳。读不到就当没有，不能崩。
        for (state in listOf(
            ArRuntimeState.NOT_INSTALLED,
            ArRuntimeState.TOO_OLD,
            ArRuntimeState.UNKNOWN,
        )) {
            assertEquals(
                "state=$state",
                ArAction.FALLBACK,
                ArInstallPolicy.decide(ctx(state, bundled = false)),
            )
        }
    }

    // ---- 两条安装路：会话先试、老式垫后 ----

    @Test
    fun `会话装被拦了就换老式安装`() {
        // MIUI 的安装器在 InstallStart.onCreate 里对**所有** sessionId != -1 且
        // SDK_INT <= 34 的会话安装一律拒绝（真机反编译确认），跟我们的 targetSdk、
        // 未知来源授权、用户点不点全无关。所以会话失败必须有下文。
        assertEquals(
            ArAction.INSTALL_BUNDLED_LEGACY,
            ArInstallPolicy.decide(
                ctx(ArRuntimeState.NOT_INSTALLED, sessionAttempted = true)
            ),
        )
    }

    @Test
    fun `老式装完回来先给宽限期 不立刻下结论`() {
        // 老式安装没有回执：系统安装器有可能在真正装完之前就把我们切回前台，
        // 那一刻状态还是 NOT_INSTALLED。直接兜底就会把一次**成功的**安装误判成失败。
        assertEquals(
            ArAction.RECHECK,
            ArInstallPolicy.decide(
                ctx(
                    ArRuntimeState.NOT_INSTALLED,
                    sessionAttempted = true,
                    legacyAttempted = true,
                    checks = 0,
                )
            ),
        )
    }

    @Test
    fun `两条路都试过且宽限期用完才兜底 不会反复装`() {
        // 少了这个闸门：装完仍然不 READY → 又装 → 又不 READY，一个装不完的循环。
        assertEquals(
            ArAction.FALLBACK,
            ArInstallPolicy.decide(
                ctx(
                    ArRuntimeState.NOT_INSTALLED,
                    sessionAttempted = true,
                    legacyAttempted = true,
                    checks = ArInstallPolicy.MAX_CHECKS,
                )
            ),
        )
    }

    // ---- 「只做一次」的闸门 ----

    @Test
    fun `没装过先试会话安装`() {
        // 会话不落盘、有回执，能用的时候是更好的那条 —— 所以它排在前面。
        assertEquals(
            ArAction.INSTALL_BUNDLED,
            ArInstallPolicy.decide(ctx(ArRuntimeState.NOT_INSTALLED)),
        )
    }

    @Test
    fun `没授权先去要授权`() {
        assertEquals(
            ArAction.GRANT_INSTALL_PERMISSION,
            ArInstallPolicy.decide(
                ctx(ArRuntimeState.NOT_INSTALLED, canInstallPackages = false)
            ),
        )
    }

    @Test
    fun `要过一次还是没授权就兜底 不会反复送去设置页`() {
        // 少了这个闸门：送去设置页 → 用户按返回 → onResume → 又送去设置页，
        // 一个退不出来的界面。
        assertEquals(
            ArAction.FALLBACK,
            ArInstallPolicy.decide(
                ctx(
                    ArRuntimeState.NOT_INSTALLED,
                    canInstallPackages = false,
                    permissionAsked = true,
                )
            ),
        )
    }

    @Test
    fun `已经授权时不再去要授权 哪怕问过`() {
        // permissionAsked 只在「仍然没授权」时才是终止条件。用户去设置里打开了，
        // 就该继续往下装。
        assertEquals(
            ArAction.INSTALL_BUNDLED,
            ArInstallPolicy.decide(
                ctx(
                    ArRuntimeState.NOT_INSTALLED,
                    canInstallPackages = true,
                    permissionAsked = true,
                )
            ),
        )
    }

    // ---- CHECKING 的有限等待 ----

    @Test
    fun `还在查就复查`() {
        assertEquals(
            ArAction.RECHECK,
            ArInstallPolicy.decide(ctx(ArRuntimeState.CHECKING, checks = 0)),
        )
        assertEquals(
            ArAction.RECHECK,
            ArInstallPolicy.decide(
                ctx(ArRuntimeState.CHECKING, checks = ArInstallPolicy.MAX_CHECKS - 1)
            ),
        )
    }

    @Test
    fun `查太久就不等了 转去装本地那份`() {
        // 这条是这轮修的那个缺陷的回归测试：原来在 CHECKING 时只显示一句
        // 「正在准备 AR 组件…」就 return，没人安排第二次查 —— 永久停在那句上。
        // 现在等待有上限，而且上限之后有下文。
        assertEquals(
            ArAction.INSTALL_BUNDLED,
            ArInstallPolicy.decide(
                ctx(ArRuntimeState.CHECKING, checks = ArInstallPolicy.MAX_CHECKS)
            ),
        )
    }

    @Test
    fun `查太久且装不了就兜底 不会无限复查`() {
        assertEquals(
            ArAction.FALLBACK,
            ArInstallPolicy.decide(
                ctx(
                    ArRuntimeState.CHECKING,
                    bundled = false,
                    checks = ArInstallPolicy.MAX_CHECKS * 10,
                )
            ),
        )
    }

    @Test
    fun `等待上限撑得住十秒承诺`() {
        // §0.3 给用户的承诺是「识别到播放 10s」，而 AR 可用性检查在那之前。
        // 这个乘积超过 7s 就该重新想清楚，而不是让它慢慢涨上去。
        val budgetMs = ArInstallPolicy.MAX_CHECKS * ArInstallPolicy.POLL_MS
        assertTrue("AR 检查预算 ${budgetMs}ms 太长了", budgetMs <= 7_000L)
    }

    // ---- 文案 ----

    @Test
    fun `每个动作都有对应文案 除了直接开 AR`() {
        for (action in ArAction.entries) {
            val text = ArInstallPolicy.notice(action, ArRuntimeState.NOT_INSTALLED)
            if (action == ArAction.START_AR) {
                assertNull("START_AR 不该说话", text)
            } else {
                assertNotNull("$action 缺文案", text)
                assertTrue("$action 文案是空的", text!!.isNotBlank())
            }
        }
    }

    @Test
    fun `只有硬件真不支持才说这台设备不支持`() {
        // 用户拒了安装也说「设备不支持」就是在撒谎，而且堵死了他重试的念头。
        assertTrue(
            ArInstallPolicy.notice(ArAction.FALLBACK, ArRuntimeState.DEVICE_NOT_CAPABLE)!!
                .contains("不支持")
        )
        val refused = ArInstallPolicy.notice(ArAction.FALLBACK, ArRuntimeState.NOT_INSTALLED)!!
        assertTrue("拒装的文案不该说设备不支持：$refused", !refused.contains("设备不支持"))
        // 两种情况都必须告诉用户「还能看」，兜底不是报错
        assertTrue(refused.contains("全屏播放"))
    }

    @Test
    fun `没有一个输入组合会落到无下文的状态`() {
        // 穷举整个输入空间。这个 App 上出现过的两次卡死都是「某个组合没人管」，
        // 所以这里不抽样、直接全跑：decide 是纯函数，跑完也就几百次。
        var count = 0
        for (state in ArRuntimeState.entries) {
            for (bundled in listOf(true, false)) {
                for (session in listOf(true, false)) {
                    for (legacy in listOf(true, false)) {
                        for (canInstall in listOf(true, false)) {
                            for (asked in listOf(true, false)) {
                                for (checks in listOf(0, 1, ArInstallPolicy.MAX_CHECKS, 999)) {
                                    val c = ctx(
                                        state, bundled, session, legacy,
                                        canInstall, asked, checks,
                                    )
                                    val a = ArInstallPolicy.decide(c)
                                    // RECHECK 只有在还没查够的时候才允许出现 ——
                                    // 否则就是一个可以无限循环下去的分支。
                                    if (a == ArAction.RECHECK) {
                                        assertTrue(
                                            "checks=${c.checks} 还在 RECHECK：$c",
                                            c.checks < ArInstallPolicy.MAX_CHECKS,
                                        )
                                    }
                                    // 老式安装是最后一条路，它之后只能是「开 AR」
                                    // 或者「兜底」——不能再回头去试会话安装，
                                    // 那条路已经被 ROM 证明是死的。
                                    if (c.legacyAttempted) {
                                        assertTrue(
                                            "老式装过之后又要装会话：$c",
                                            a != ArAction.INSTALL_BUNDLED,
                                        )
                                    }
                                    count++
                                }
                            }
                        }
                    }
                }
            }
        }
        assertEquals(ArRuntimeState.entries.size * 2 * 2 * 2 * 2 * 2 * 4, count)
    }
}

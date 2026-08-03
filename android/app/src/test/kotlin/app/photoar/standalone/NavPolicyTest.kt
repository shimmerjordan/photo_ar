package app.photoar.standalone

import app.photoar.arview.AuthPhase
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class NavPolicyTest {

    // ---------------------------------------------------------------- 页签

    @Test
    fun `访客只有扫描和设置`() {
        assertEquals(listOf(Tab.SCAN, Tab.SETTINGS), NavPolicy.tabsFor(isAdmin = false))
    }

    @Test
    fun `访客拿不到任何管理入口`() {
        // 这条是权限边界。列举出来而不是只比长度：以后往 ADMIN_TABS 里加一个页签时，
        // 如果顺手也加进了 VIEWER_TABS，这里会红。
        val viewer = NavPolicy.tabsFor(isAdmin = false)
        for (t in listOf(Tab.PHOTOS, Tab.MEDIA, Tab.ADMIN)) {
            assertFalse("访客不该看到 $t", t in viewer)
        }
    }

    @Test
    fun `管理员有四个页签`() {
        assertEquals(
            listOf(Tab.PHOTOS, Tab.MEDIA, Tab.ADMIN, Tab.SETTINGS),
            NavPolicy.tabsFor(isAdmin = true),
        )
    }

    @Test
    fun `设置页两种角色都有`() {
        // 访客也要能改地址、看自己是谁、退出登录。
        assertTrue(Tab.SETTINGS in NavPolicy.tabsFor(isAdmin = false))
        assertTrue(Tab.SETTINGS in NavPolicy.tabsFor(isAdmin = true))
    }

    @Test
    fun `管理员没有那个整页扫描的首页`() {
        // 管理员的扫一扫是悬在底栏上的那颗 FAB（第 2 轮的要求：底部中间、更醒目），
        // 再给他一个整页的扫描首页就是同一个动作两个入口。
        assertFalse(Tab.SCAN in NavPolicy.tabsFor(isAdmin = true))
    }

    // ---------------------------------------------------------------- 蒙版

    @Test
    fun `没登录要挡`() {
        assertTrue(NavPolicy.needsGate(hasUsableEndpoint = true, phase = AuthPhase.LOGGED_OUT))
    }

    @Test
    fun `登录过期要挡`() {
        assertTrue(NavPolicy.needsGate(hasUsableEndpoint = true, phase = AuthPhase.EXPIRED))
    }

    @Test
    fun `没配地址要挡_哪怕凭证看起来是好的`() {
        // 第一版只判凭证，全新装机时会弹一个填了也登不进去的表单（没有地址可以发请求），
        // 而报错是「连不上」。
        assertTrue(NavPolicy.needsGate(hasUsableEndpoint = false, phase = AuthPhase.ACTIVE))
    }

    @Test
    fun `已登录且有地址就放进来`() {
        assertFalse(NavPolicy.needsGate(hasUsableEndpoint = true, phase = AuthPhase.ACTIVE))
    }

    @Test
    fun `快过期不挡`() {
        // 挡住等于把一个还能用的装机变成不能用的。设置页里有横幅提醒重新登录。
        assertFalse(
            NavPolicy.needsGate(hasUsableEndpoint = true, phase = AuthPhase.EXPIRING_SOON),
        )
    }

    @Test
    fun `来路不明的旧令牌不挡`() {
        // Phase 3 手填令牌的老装机升上来就是这个状态。它仍然能扫，挡住是把一个
        // 好用的装机变成不能用的。
        assertFalse(
            NavPolicy.needsGate(hasUsableEndpoint = true, phase = AuthPhase.UNKNOWN_TOKEN),
        )
    }

    @Test
    fun `蒙版先问地址再问账号`() {
        assertEquals(GateStep.ENDPOINT, NavPolicy.gateStep(hasUsableEndpoint = false))
        assertEquals(GateStep.LOGIN, NavPolicy.gateStep(hasUsableEndpoint = true))
    }

    // ---------------------------------------------------------------- 落地页

    @Test
    fun `访客登录后落在扫描页`() {
        assertEquals(Tab.SCAN, NavPolicy.landingTab(isAdmin = false))
    }

    @Test
    fun `管理员登录后落在照片库`() {
        assertEquals(Tab.PHOTOS, NavPolicy.landingTab(isAdmin = true))
    }

    @Test
    fun `落地页一定在自己的页签列表里`() {
        for (admin in listOf(true, false)) {
            val landing = NavPolicy.landingTab(admin)
            assertTrue("$landing 不在 isAdmin=$admin 的页签里", landing in NavPolicy.tabsFor(admin))
        }
    }

    // ---------------------------------------------------------------- 换人登录

    @Test
    fun `管理员登出换访客进来时_停留的管理页要换掉`() {
        // 同一台手机换人登录。不换的话那一页上每个按钮都会 403。
        assertEquals(Tab.SCAN, NavPolicy.tabAfterRoleChange(Tab.MEDIA, isAdmin = false))
        assertEquals(Tab.SCAN, NavPolicy.tabAfterRoleChange(Tab.ADMIN, isAdmin = false))
        assertEquals(Tab.SCAN, NavPolicy.tabAfterRoleChange(Tab.PHOTOS, isAdmin = false))
    }

    @Test
    fun `两种角色都有的页签不动`() {
        assertEquals(Tab.SETTINGS, NavPolicy.tabAfterRoleChange(Tab.SETTINGS, isAdmin = false))
        assertEquals(Tab.SETTINGS, NavPolicy.tabAfterRoleChange(Tab.SETTINGS, isAdmin = true))
    }

    @Test
    fun `访客换管理员进来时_扫描页要换掉`() {
        // 反方向也要成立：SCAN 不在管理员的列表里。
        assertEquals(Tab.PHOTOS, NavPolicy.tabAfterRoleChange(Tab.SCAN, isAdmin = true))
    }

    @Test
    fun `换角色后的页签一定是合法的`() {
        for (admin in listOf(true, false)) {
            for (t in Tab.entries) {
                val next = NavPolicy.tabAfterRoleChange(t, admin)
                assertTrue(
                    "从 $t 换到 isAdmin=$admin 得到了非法的 $next",
                    next in NavPolicy.tabsFor(admin),
                )
            }
        }
    }
}

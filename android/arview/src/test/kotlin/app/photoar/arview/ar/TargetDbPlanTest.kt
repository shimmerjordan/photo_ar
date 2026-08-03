package app.photoar.arview.ar

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * 装库优先级。四种输入组合全列出来，因为这个判断错了的后果全是「不报错的坏」：
 * 多重建一次库是启动扫描时白等几秒，少退回一次是离线识别静默消失。
 */
class TargetDbPlanTest {

    @Test
    fun `有预建库就装它，端上那份碰都不碰`() {
        // 这就是这次改动的全部意义：省掉 addImage 那 6 秒。哪怕端上那份是过期的、
        // 该重建的，也不重建 —— 它此刻是纯粹的退路。
        assertEquals(
            TargetDbPlan.UseServer,
            planTargetDb(TargetDbFacts(serverInstallable = true, localStale = true)),
        )
        assertEquals(
            TargetDbPlan.UseServer,
            planTargetDb(TargetDbFacts(serverInstallable = true, localStale = false)),
        )
    }

    @Test
    fun `没有预建库时退回端上现建，过期就先建`() {
        assertEquals(
            TargetDbPlan.UseLocal(rebuildFirst = true),
            planTargetDb(TargetDbFacts(serverInstallable = false, localStale = true)),
        )
    }

    @Test
    fun `端上那份还新鲜就直接用，不白建一遍`() {
        // 「过期」判的是缩略图文件时间。判错的方向若是「总是过期」，每次启动扫描都
        // 白重建一次库，而且不报错。
        assertEquals(
            TargetDbPlan.UseLocal(rebuildFirst = false),
            planTargetDb(TargetDbFacts(serverInstallable = false, localStale = false)),
        )
    }

    @Test
    fun `预建库装不上之后再问一次，就落到重建端上那份`() {
        // 调用方在 deserialize 失败之后把 serverInstallable 置成 false 再问 —— 于是
        // 重建这件事发生在**后台线程**上（prepare），而不是等到 GL 线程装库时才发现。
        assertEquals(
            TargetDbPlan.UseLocal(rebuildFirst = true),
            planTargetDb(TargetDbFacts(serverInstallable = false, localStale = true)),
        )
    }
}

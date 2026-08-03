package app.photoar.arview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class DiagLogTest {

    @Test
    fun `第一行是零点，时间戳相对它`() {
        val log = DiagLog()
        log.add(1_700_000_000_000L, "开始")
        log.add(1_700_000_001_500L, "一秒半后")
        val lines = log.render().lines()
        assertEquals("0:00.0 开始", lines[0])
        assertEquals("0:01.5 一秒半后", lines[1])
    }

    @Test
    fun `连续相同的行折叠成次数`() {
        val log = DiagLog()
        log.add(0L, "贴不上")
        log.add(1000L, "贴不上")
        log.add(2000L, "贴不上")
        assertEquals(1, log.size)
        assertEquals("0:02.0 贴不上 ×3", log.render())
    }

    @Test
    fun `折叠只看紧邻的上一行`() {
        // 中间夹了别的就不该折 —— 「A B A」表示这件事**又发生了一次**，而那正是要看的。
        val log = DiagLog()
        log.add(0L, "A")
        log.add(100L, "B")
        log.add(200L, "A")
        assertEquals(3, log.size)
    }

    @Test
    fun `折叠时时间戳跟到最后一次`() {
        // 「这句话现在还在刷」和「它两分钟前刷过」是两回事。
        val log = DiagLog()
        log.add(0L, "刷")
        log.add(30_000L, "刷")
        assertTrue(log.render().startsWith("0:30.0 "))
    }

    @Test
    fun `超过容量丢最老的`() {
        val log = DiagLog(capacity = 3)
        for (i in 1..5) log.add(i * 100L, "行$i")
        assertEquals(3, log.size)
        assertEquals(listOf("行3", "行4", "行5"), log.render().lines().map { it.substringAfter(' ') })
    }

    @Test
    fun `折叠让重复的行不会把关键行顶出去`() {
        // 这是折叠存在的理由，不是附带效果：贴不上时 ARCore 那行每秒一条，
        // 而「装目标失败」只出现一次 —— 后者被顶掉的话这块日志就没用了。
        val log = DiagLog(capacity = 3)
        log.add(0L, "装目标失败：版本不匹配")
        repeat(50) { log.add(it * 100L + 100L, "贴不上：相机=TRACKING") }
        assertTrue("关键行必须还在", log.render().contains("装目标失败"))
    }

    @Test
    fun `空白行丢掉`() {
        val log = DiagLog()
        log.add(0L, "  ")
        log.add(0L, "")
        assertEquals(0, log.size)
        assertEquals("", log.render())
    }

    @Test
    fun `分钟会进位`() {
        val log = DiagLog()
        log.add(0L, "起")
        log.add(125_400L, "两分零五点四秒")
        assertTrue(log.render().contains("2:05.4"))
    }

    @Test
    fun `clear 之后零点重新算`() {
        val log = DiagLog()
        log.add(1000L, "旧")
        log.clear()
        log.add(9_000L, "新")
        assertEquals("0:00.0 新", log.render())
    }
}

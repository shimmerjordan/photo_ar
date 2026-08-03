package app.photoar.arview

import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class FrameStatsTest {

    private val ms = 1_000_000L

    @Test
    fun `一秒之内不报`() {
        val s = FrameStats()
        assertNull(s.frame(5 * ms, 0L))
        assertNull(s.frame(5 * ms, 500 * ms))
        assertNull(s.frame(5 * ms, 999 * ms))
    }

    @Test
    fun `攒满一秒报一行，带帧率均值峰值和卡帧数`() {
        val s = FrameStats()
        // 59 帧 5ms + 1 帧 40ms，正好一秒
        repeat(59) { s.frame(5 * ms, it.toLong() * 16 * ms) }
        val line = s.frame(40 * ms, 1000 * ms)
        assertNotNull(line)
        assertTrue(line!!, line.contains("60fps"))
        assertTrue(line, line.contains("峰40.0ms"))
        assertTrue("40ms 超过 16.7ms 的预算，算一次卡帧", line.contains("卡1/60"))
    }

    @Test
    fun `报完就归零，下一个窗口不带上一个的峰值`() {
        val s = FrameStats()
        s.frame(100 * ms, 0L)
        val first = s.frame(5 * ms, 1000 * ms)
        assertTrue(first!!, first.contains("峰100.0ms"))
        repeat(2) { s.frame(5 * ms, 1000L * ms) }
        val second = s.frame(5 * ms, 2000 * ms)
        assertNotNull(second)
        assertTrue("上一个窗口的 100ms 不该再出现", second!!.contains("峰5.0ms"))
    }

    @Test
    fun `抓帧单独一档，没抓过就不出现那一段`() {
        // 混进每帧均值里它会被 24 帧摊薄成看不见 —— 而它是 GL 线程上唯一与渲染
        // 无关的重活，正是要单独看的那一项。
        val s = FrameStats()
        s.frame(5 * ms, 0L) // 开窗口
        val without = s.frame(5 * ms, 1000 * ms)
        assertTrue(without!!, !without.contains("抓帧"))

        s.grab(30 * ms)
        s.grab(10 * ms)
        val with = s.frame(5 * ms, 2000 * ms)
        assertTrue(with!!, with.contains("抓帧×2"))
        assertTrue(with, with.contains("均20.0"))
        assertTrue(with, with.contains("峰30.0ms"))
    }

    @Test
    fun `刚好等于预算线不算卡帧`() {
        val s = FrameStats()
        val exactly = (FrameStats.JANK_MS * 1_000_000f).toLong()
        s.frame(exactly, 0L)
        val line = s.frame(exactly, 1000 * ms)
        assertTrue(line!!, line.contains("卡0/2"))
    }

    @Test
    fun `窗口起点为零也照常报`() {
        // nanoTime 的零点没有保证，它可以恰好返回 0。拿 0 当「还没开始」的哨兵会让
        // 那一帧反复把窗口往后推 —— 报得越来越晚，而报出来的 fps 偏高。
        val s = FrameStats()
        assertNull(s.frame(5 * ms, 0L))
        val line = s.frame(5 * ms, 1000 * ms)
        assertNotNull(line)
        assertTrue(line!!, line.contains("卡0/2"))
    }
}

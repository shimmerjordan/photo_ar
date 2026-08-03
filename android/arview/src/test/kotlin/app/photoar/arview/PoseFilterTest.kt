package app.photoar.arview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlin.math.sqrt

/**
 * [PoseFilter] 的行为。
 *
 * 这些测试量的是**滤波器自己**的性质，不需要设备：抖动压制比、静止收敛、
 * 异常帧拒收与逃逸、四元数短弧、矩阵布局。真机上要量的是另一件事（贴合是否
 * 还有可见抖动、大角度下会不会丢），那个只能人举着手机看。
 */
class PoseFilterTest {

    private val ns = 1_000_000_000L / 30L // 30 fps

    private fun q(x: Float, y: Float, z: Float, w: Float) = floatArrayOf(x, y, z, w)
    private fun identity() = q(0f, 0f, 0f, 1f)

    /** 绕 Y 轴转 deg 度的四元数。 */
    private fun yaw(deg: Float): FloatArray {
        val h = Math.toRadians(deg.toDouble() / 2.0)
        return q(0f, kotlin.math.sin(h).toFloat(), 0f, kotlin.math.cos(h).toFloat())
    }

    // ---- 首帧 ----

    @Test
    fun `首帧直接采纳，不从零位姿插值`() {
        val f = PoseFilter()
        assertEquals(PoseFilter.Verdict.SEEDED, f.update(floatArrayOf(1f, 2f, 3f), identity(), ns))
        val t = FloatArray(3)
        f.translationInto(t)
        // 差一点都不行：从原点滑过去会让视频在命中瞬间"飞"到照片上
        assertEquals(1f, t[0], 1e-6f)
        assertEquals(2f, t[1], 1e-6f)
        assertEquals(3f, t[2], 1e-6f)
    }

    @Test
    fun `没喂过数据时 hasPose 为假且矩阵是单位阵`() {
        val f = PoseFilter()
        assertTrue(!f.hasPose)
        val m = FloatArray(16)
        f.toMatrix(m)
        for (i in 0..15) {
            val want = if (i % 5 == 0) 1f else 0f
            assertEquals("m[$i]", want, m[i], 1e-6f)
        }
    }

    // ---- 抖动压制 ----

    // ---- 延迟。用户实测报过「贴合有延迟」，这一组就是防它回来的 ----

    @Test
    fun `阶跃输入在 100ms 内收敛到 90%`() {
        // 上一版时间常数 0.32 s，100ms 时只走到约 27% —— 屏幕上就是视频拖在照片后面。
        // 这个测试按**墙钟时间**写，所以 30/60 fps 都必须过（时间常数与帧率无关）。
        for (fps in listOf(30, 60)) {
            val step = 1_000_000_000L / fps
            val f = PoseFilter()
            f.update(floatArrayOf(0f, 0f, 0f), identity(), step)
            // 2cm 的阶跃：低于速度门限（1.5 m/s × dt），不会被当异常帧拒收
            val target = 0.02f
            val frames = fps / 10 // 100 ms
            for (k in 2..(frames + 1)) {
                f.update(floatArrayOf(target, 0f, 0f), identity(), k.toLong() * step)
            }
            val got = FloatArray(3).also { f.translationInto(it) }[0]
            val pct = got / target
            assertTrue(
                "${fps}fps：100ms 后只走到 ${(pct * 100).toInt()}%，延迟太大",
                pct >= 0.90f,
            )
        }
    }

    @Test
    fun `时间常数不超过两帧`() {
        // 直接钉住 FC_MIN_T：滞后大致等于时间常数，而 30fps 一帧是 33ms。
        val tauMs = 1000f / (2f * Math.PI.toFloat() * PoseFilter.FC_MIN_T)
        assertTrue("时间常数 ${tauMs}ms 超过两帧（67ms），会看得出延迟", tauMs <= 67f)
    }

    @Test
    fun `静止目标上的抖动仍被压下去（但只剩一点点，这是拿延迟换的）`() {
        val f = PoseFilter()
        val truth = floatArrayOf(0.5f, 0f, -1.2f)
        // 确定性的伪随机抖动，±2mm —— 与实测的 ARCore 量级同阶
        var seed = 12345L
        fun jitter(): Float {
            seed = seed * 6364136223846793005L + 1442695040888963407L
            return ((seed ushr 33).toFloat() / Int.MAX_VALUE.toFloat() - 0.5f) * 0.004f
        }

        var rawErr = 0.0
        var filtErr = 0.0
        var n = 0
        for (i in 0 until 300) {
            val raw = floatArrayOf(
                truth[0] + jitter(),
                truth[1] + jitter(),
                truth[2] + jitter(),
            )
            f.update(raw, identity(), (i + 1) * ns)
            if (i < 100) continue // 前 100 帧留给收敛
            val out = FloatArray(3)
            f.translationInto(out)
            rawErr += dist(raw, truth).toDouble()
            filtErr += dist(out, truth).toDouble()
            n++
        }
        val rawRms = rawErr / n
        val filtRms = filtErr / n
        val ratio = rawRms / filtRms
        // 只剩约 1.5 倍。**这是明知故犯**：时间常数压到一帧以消掉延迟，抑制比就必然
        // 掉到这个量级（sqrt(α/(2-α))，α≈0.51 @30fps）。上一版是 4 倍多，代价是
        // 0.32 秒滞后，用户实测报了「贴合有延迟」。
        //
        // 所以这里的门槛只用来钉住"滤波器还在干活、没被写成透传"，不是质量目标。
        // 抖动如果重新变明显，正确的下一步是换机制（世界锚点），不是调高这个数 ——
        // 详见 PoseFilter 类注释。
        assertTrue(
            "压制比只有 ${"%.2f".format(ratio)} 倍（原始 $rawRms → 滤波后 $filtRms），" +
                "低于 1.3 说明滤波器基本没起作用了",
            ratio > 1.3,
        )
    }

    @Test
    fun `静止输入下滤波值收敛到真值`() {
        val f = PoseFilter()
        val truth = floatArrayOf(0.1f, 0.2f, 0.3f)
        for (i in 0 until 200) f.update(truth, identity(), (i + 1) * ns)
        val out = FloatArray(3)
        f.translationInto(out)
        assertEquals(0.1f, out[0], 1e-4f)
        assertEquals(0.2f, out[1], 1e-4f)
        assertEquals(0.3f, out[2], 1e-4f)
    }

    // ---- 异常帧 ----

    @Test
    fun `单帧大跳变被拒收，位姿不动`() {
        val f = PoseFilter()
        val home = floatArrayOf(0f, 0f, 0f)
        for (i in 1..10) f.update(home, identity(), i * ns)
        val before = FloatArray(3).also { f.translationInto(it) }

        // 跳 30cm —— 远超 MAX_JUMP_M
        val v = f.update(floatArrayOf(0.3f, 0f, 0f), identity(), 11 * ns)
        assertEquals(PoseFilter.Verdict.REJECTED, v)
        val after = FloatArray(3).also { f.translationInto(it) }
        assertEquals(before[0], after[0], 1e-7f)
    }

    @Test
    fun `连续大跳变超过容忍时间后跳过去，不会死锁在旧位置`() {
        val f = PoseFilter()
        for (i in 1..10) f.update(floatArrayOf(0f, 0f, 0f), identity(), i.toLong() * ns)

        var verdict = PoseFilter.Verdict.SEEDED
        var i = 11L
        // 一直喂到超过容忍时间。上限只是防死循环。
        while (verdict != PoseFilter.Verdict.SNAPPED && i < 200) {
            verdict = f.update(floatArrayOf(0.3f, 0f, 0f), identity(), i * ns)
            i++
        }
        assertEquals(PoseFilter.Verdict.SNAPPED, verdict)
        val out = FloatArray(3).also { f.translationInto(it) }
        assertEquals("跳过去之后应该就在新位置上", 0.3f, out[0], 1e-6f)
    }

    // ---- 与帧率无关 ----
    //
    // 相机档位从 30 提到 60 之后，这三条性质必须不变。缺任何一条都会让"提帧率"
    // 悄悄改变滤波行为：门限松一倍、逃逸口早一半、或者要重新调参。

    @Test
    fun `异常帧门限按速度算，30 和 60 fps 判定一致`() {
        // 同一个**物理速度**（3 m/s，超过 1.5 m/s 上限）在两种帧率下都该被拒
        for (fps in listOf(30, 60)) {
            val step = 1_000_000_000L / fps
            val perFrame = 3.0f / fps // 3 m/s 走一帧的距离
            val f = PoseFilter()
            for (k in 1..10) f.update(floatArrayOf(0f, 0f, 0f), identity(), k * step)
            val v = f.update(floatArrayOf(perFrame, 0f, 0f), identity(), 11 * step)
            assertEquals("${fps}fps 下 3 m/s 应判异常", PoseFilter.Verdict.REJECTED, v)
        }
        // 反向：0.5 m/s（远低于上限）在两种帧率下都该被采纳
        for (fps in listOf(30, 60)) {
            val step = 1_000_000_000L / fps
            val perFrame = 0.5f / fps
            val f = PoseFilter()
            for (k in 1..10) f.update(floatArrayOf(0f, 0f, 0f), identity(), k * step)
            val v = f.update(floatArrayOf(perFrame, 0f, 0f), identity(), 11 * step)
            assertEquals("${fps}fps 下 0.5 m/s 应正常滤波", PoseFilter.Verdict.SMOOTHED, v)
        }
    }

    @Test
    fun `拒收逃逸口按时间算，30 和 60 fps 用掉的墙钟时间接近`() {
        val elapsed = mutableMapOf<Int, Long>()
        for (fps in listOf(30, 60)) {
            val step = 1_000_000_000L / fps
            val f = PoseFilter()
            for (k in 1..10) f.update(floatArrayOf(0f, 0f, 0f), identity(), k * step)
            val startNs = 10 * step
            var i = 11L
            var v = PoseFilter.Verdict.SEEDED
            while (v != PoseFilter.Verdict.SNAPPED && i < 500) {
                v = f.update(floatArrayOf(0.3f, 0f, 0f), identity(), i * step)
                if (v == PoseFilter.Verdict.SNAPPED) elapsed[fps] = i * step - startNs
                i++
            }
            assertEquals("${fps}fps 应该最终跳过去", PoseFilter.Verdict.SNAPPED, v)
        }
        val ms30 = elapsed[30]!! / 1_000_000.0
        val ms60 = elapsed[60]!! / 1_000_000.0
        // 按帧数算的旧实现会是 133ms vs 67ms（差一倍）。按时间算，两者只差一个帧间隔。
        assertTrue(
            "30fps 用 ${ms30}ms、60fps 用 ${ms60}ms，差得太多说明还在按帧数算",
            kotlin.math.abs(ms30 - ms60) < 20.0,
        )
    }

    @Test
    fun `时间常数与帧率无关：同样墙钟时间后收敛到同一位置`() {
        // 从 0 起跳到 0.2m（低于速度门限，不会被拒），跑 0.5 秒墙钟时间。
        // 30 与 60 fps 的收敛程度应该接近 —— α 是按 dt 算的，时间常数固定。
        val results = mutableMapOf<Int, Float>()
        for (fps in listOf(30, 60)) {
            val step = 1_000_000_000L / fps
            val f = PoseFilter()
            f.update(floatArrayOf(0f, 0f, 0f), identity(), step)
            val frames = fps / 2 // 0.5 秒
            // 每帧走 0.2/frames 米，总位移 0.2m，速度 0.4 m/s（低于 1.5 上限）
            for (k in 2..(frames + 1)) {
                val x = 0.2f * (k - 1) / frames
                f.update(floatArrayOf(x, 0f, 0f), identity(), k.toLong() * step)
            }
            results[fps] = FloatArray(3).also { f.translationInto(it) }[0]
        }
        val a = results[30]!!
        val b = results[60]!!
        assertTrue(
            "30fps 收到 $a、60fps 收到 $b，差太多说明时间常数跟着帧率变了",
            kotlin.math.abs(a - b) < 0.012f,
        )
    }

    @Test
    fun `帧率翻倍时抖动抑制不变差`() {
        // 同一段墙钟时间、同样幅度的抖动，60fps 的稳态误差不应该比 30fps 大。
        // （理论上应该更小：时间窗内平均掉的样本更多。）
        fun rms(fps: Int): Double {
            val step = 1_000_000_000L / fps
            val f = PoseFilter()
            val truth = floatArrayOf(0.5f, 0f, -1.2f)
            var seed = 999L
            fun jitter(): Float {
                seed = seed * 6364136223846793005L + 1442695040888963407L
                return ((seed ushr 33).toFloat() / Int.MAX_VALUE.toFloat() - 0.5f) * 0.004f
            }
            var err = 0.0
            var n = 0
            val total = fps * 10 // 10 秒
            for (i in 0 until total) {
                f.update(
                    floatArrayOf(truth[0] + jitter(), truth[1] + jitter(), truth[2] + jitter()),
                    identity(), (i + 1).toLong() * step,
                )
                if (i < total / 3) continue
                err += dist(FloatArray(3).also { f.translationInto(it) }, truth).toDouble()
                n++
            }
            return err / n
        }
        val e30 = rms(30)
        val e60 = rms(60)
        assertTrue("30fps 误差 $e30、60fps 误差 $e60，提帧率不该让抖动变大", e60 <= e30 * 1.1)
    }

    @Test
    fun `渐进式漂移不会被拒收门放过去`() {
        // 拒收时刻意不更新 prevRaw。这个测试钉住那个决定：每帧 4cm（不超阈值）
        // 连走 10 帧，滤波器应该跟过去而不是原地不动 —— 也就是说拒收门确实只
        // 挡"跳变"，不挡真实的慢速移动。
        val f = PoseFilter()
        f.update(floatArrayOf(0f, 0f, 0f), identity(), ns)
        for (i in 2..40) {
            f.update(floatArrayOf(0.04f * (i - 1), 0f, 0f), identity(), i.toLong() * ns)
        }
        val out = FloatArray(3).also { f.translationInto(it) }
        assertTrue("应该已经跟到远处，实际 ${out[0]}", out[0] > 1.0f)
    }

    @Test
    fun `reset 之后下一帧重新当首帧`() {
        val f = PoseFilter()
        f.update(floatArrayOf(0f, 0f, 0f), identity(), ns)
        f.reset()
        assertTrue(!f.hasPose)
        val v = f.update(floatArrayOf(5f, 5f, 5f), identity(), 2 * ns)
        assertEquals(PoseFilter.Verdict.SEEDED, v)
        val out = FloatArray(3).also { f.translationInto(it) }
        assertEquals(5f, out[0], 1e-6f)
    }

    // ---- 四元数 ----

    @Test
    fun `符号相反的四元数被当成同一个旋转，夹角为零`() {
        val a = yaw(30f)
        val b = floatArrayOf(-a[0], -a[1], -a[2], -a[3])
        assertEquals(0f, PoseFilter.angleDeg(a, b), 1e-3f)
    }

    @Test
    fun `符号相反的输入不会触发拒收也不会翻面`() {
        val f = PoseFilter()
        val a = yaw(30f)
        for (i in 1..20) f.update(floatArrayOf(0f, 0f, 0f), a, i.toLong() * ns)
        val negA = floatArrayOf(-a[0], -a[1], -a[2], -a[3])
        val v = f.update(floatArrayOf(0f, 0f, 0f), negA, 21 * ns)
        assertNotEquals("符号翻转不是异常帧", PoseFilter.Verdict.REJECTED, v)
        // 滤波结果仍然表示 30 度那个旋转（可能符号相反）
        val out = FloatArray(4).also { f.rotationInto(it) }
        assertEquals(0f, PoseFilter.angleDeg(out, a), 0.5f)
    }

    @Test
    fun `slerp 走短弧`() {
        val a = yaw(0f)
        val b = yaw(90f)
        val mid = FloatArray(4)
        PoseFilter.slerp(a, b, 0.5f, mid)
        assertEquals("中点应该是 45 度", 45f, PoseFilter.angleDeg(a, mid), 0.1f)
        var sq = 0f
        for (v in mid) sq += v * v
        assertEquals("slerp 的输出必须是单位四元数", 1f, sqrt(sq), 1e-5f)
    }

    @Test
    fun `slerp 在两端取到端点`() {
        val a = yaw(10f)
        val b = yaw(80f)
        val out = FloatArray(4)
        PoseFilter.slerp(a, b, 0f, out)
        assertEquals(0f, PoseFilter.angleDeg(out, a), 1e-2f)
        PoseFilter.slerp(a, b, 1f, out)
        assertEquals(0f, PoseFilter.angleDeg(out, b), 1e-2f)
    }

    @Test
    fun `几乎重合的两个四元数不会除零`() {
        val a = yaw(10f)
        val b = yaw(10.0001f)
        val out = FloatArray(4)
        PoseFilter.slerp(a, b, 0.5f, out)
        for (v in out) assertTrue("出现了 NaN/Inf：${out.toList()}", v.isFinite())
    }

    // ---- 矩阵布局 ----

    @Test
    fun `单位旋转加平移写成列主序`() {
        val m = FloatArray(16)
        PoseFilter.writeMatrix(floatArrayOf(7f, 8f, 9f), identity(), m)
        // 平移必须在 m[12..14]（列主序）。落在 m[3,7,11] 就是行主序，视频会贴到别处
        assertEquals(7f, m[12], 1e-6f)
        assertEquals(8f, m[13], 1e-6f)
        assertEquals(9f, m[14], 1e-6f)
        assertEquals(1f, m[15], 1e-6f)
        assertEquals(0f, m[3], 1e-6f)
        assertEquals(0f, m[7], 1e-6f)
        assertEquals(0f, m[11], 1e-6f)
        assertEquals(1f, m[0], 1e-6f)
        assertEquals(1f, m[5], 1e-6f)
        assertEquals(1f, m[10], 1e-6f)
    }

    @Test
    fun `绕 Y 轴 90 度把 X 轴转到负 Z`() {
        val m = FloatArray(16)
        PoseFilter.writeMatrix(floatArrayOf(0f, 0f, 0f), yaw(90f), m)
        // 列主序下第一列 m[0..2] 就是 X 基向量被转到哪里
        assertEquals(0f, m[0], 1e-5f)
        assertEquals(0f, m[1], 1e-5f)
        assertEquals(-1f, m[2], 1e-5f)
        // 第三列：Z 轴 → +X
        assertEquals(1f, m[8], 1e-5f)
        assertEquals(0f, m[9], 1e-5f)
        assertEquals(0f, m[10], 1e-5f)
    }

    @Test
    fun `未归一化的四元数照样给出正交矩阵`() {
        val m = FloatArray(16)
        val scaled = yaw(37f).map { it * 3.7f }.toFloatArray()
        PoseFilter.writeMatrix(floatArrayOf(0f, 0f, 0f), scaled, m)
        // 第一列长度应为 1；不归一化的话矩阵会带 3.7² 的缩放，视频被放大十几倍
        val len = sqrt((m[0] * m[0] + m[1] * m[1] + m[2] * m[2]).toDouble()).toFloat()
        assertEquals(1f, len, 1e-5f)
    }

    @Test
    fun `全零四元数退化成单位旋转而不是 NaN`() {
        val m = FloatArray(16)
        PoseFilter.writeMatrix(floatArrayOf(1f, 2f, 3f), floatArrayOf(0f, 0f, 0f, 0f), m)
        for (v in m) assertTrue("出现 NaN 会让整块四边形静默消失", v.isFinite())
        assertEquals(1f, m[0], 1e-6f)
        assertEquals(1f, m[15], 1e-6f)
    }

    // ---- 自适应截止 ----

    @Test
    fun `速度越大截止频率越高，α 越大`() {
        val dt = 1f / 30f
        val still = PoseFilter.alphaOf(dt, PoseFilter.FC_MIN_T)
        // 抖动伪装出来的速度（约 0.06 m/s）几乎不该动 α —— BETA_T 取小的全部理由
        val jitterSpeed =
            PoseFilter.alphaOf(dt, PoseFilter.FC_MIN_T + PoseFilter.BETA_T * 0.06f)
        // 真有人以 0.3 m/s 挪照片
        val moving =
            PoseFilter.alphaOf(dt, PoseFilter.FC_MIN_T + PoseFilter.BETA_T * 0.3f)
        // 静止时仍留一点平滑（α<1 就是还在滤），但已经很轻 —— 见 FC_MIN_T。
        assertTrue("静止时 α 应该 <1（还在滤），实际 $still", still < 0.7f)
        assertTrue(
            "抖动伪速度不该把 α 抬太多：$still → $jitterSpeed",
            jitterSpeed < still * 1.3f,
        )
        // 运动时要接近透传 —— 这条直接对应「贴合有延迟」那个反馈。
        // 不再写成"倍数"：α 已经接近 1，倍数关系在这个区间没有意义。
        assertTrue("以 0.3 m/s 移动时 α 应接近 1（几乎不滤），实际 $moving", moving > 0.65f)
        assertTrue("移动时必须比静止时更透明：$still → $moving", moving > still)
    }

    @Test
    fun `α 随 dt 单调上升`() {
        val a1 = PoseFilter.alphaOf(1f / 60f, 1f)
        val a2 = PoseFilter.alphaOf(1f / 30f, 1f)
        assertTrue("帧间隔翻倍时单帧权重应变大", a2 > a1)
    }

    private fun dist(a: FloatArray, b: FloatArray): Float {
        var s = 0f
        for (i in 0..2) {
            val d = a[i] - b[i]
            s += d * d
        }
        return sqrt(s)
    }
}

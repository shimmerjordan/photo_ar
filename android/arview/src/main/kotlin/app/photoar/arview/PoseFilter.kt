package app.photoar.arview

import kotlin.math.abs
import kotlin.math.acos
import kotlin.math.sin
import kotlin.math.sqrt

/**
 * 目标位姿的低通滤波 + 异常帧拒收。纯函数式状态机，不碰 ARCore 类型，JVM 可测。
 *
 * ## 为什么这里**不能**重滤波 —— 一个曾经写在这里的错误论证
 *
 * 这段原来写的是：被滤的量是照片在**世界坐标系**里的位姿，照片钉在墙上不动，真值
 * 是个常量，所以重低通几乎不付延迟。据此把时间常数设成了 0.32 秒。
 *
 * **那个论证是错的，实机表现就是「贴合有延迟」。** 错在哪：
 *
 * 屏幕位置是 `projection · view · model` 三者的乘积。`view`（相机位姿）我们**不滤**，
 * `model`（照片位姿）滤。而这两个量出自 ARCore 的**同一次优化**，它们的误差是
 * **相关**的 —— ARCore 保证的是两者互相自洽，这正是 AR 内容在世界坐标本身缓慢漂移
 * 时看起来依然钉得很稳的原因。
 *
 * 只滤其中一个，就把这个相关性拆了：手机一动，ARCore 立刻更新世界估计，`view` 立刻
 * 跟上，`model` 却要等一个时间常数。屏幕上就是视频拖在照片后面，拖的量正比于
 * `时间常数 × 运动速度`。"真值是常量"这句话本身没错，错在它推不出"滤了不付代价"。
 *
 * ## 于是取舍变成：只在静止时滤
 *
 * 关键观察是这两种毛病**不会同时被看见**：
 *
 * - **延迟只在动的时候看得见**（静止时滤波器早就收敛了，没有滞后可言）
 * - **抖动只在静止的时候看得见**（动起来真实位移远大于毫米级抖动）
 *
 * 所以自适应截止频率正是对的工具，而参数要按"动起来必须透明"来定：时间常数压到
 * 约一帧（[FC_MIN_T]），速度增益调高让真实运动几乎不滤（[BETA_T]）。
 *
 * 代价要说清楚：这样一来抖动抑制只剩约 1.7 倍（30fps）/ 2.2 倍（60fps），远不如
 * 原来那个设置。**如果静止时抖动重新变得明显，正确的下一步不是把时间常数调回去**
 * —— 那只会把延迟换回来。正确的做法是换机制：用 ARCore 的世界锚点
 * （`session.createAnchor(centerPose)`）取代外部滤波，让稳定性来自 ARCore 自己对
 * 相机位姿与锚点的联合优化，那条路不需要用延迟去换。
 *
 * 真值会变的两种情况仍照旧：有人把照片挪了（罕见）靠速度自适应跟上，ARCore 重新
 * 捕获时的位姿跳变靠[异常帧拒收][update]挡掉。
 *
 * ## 为什么用 1€ 滤波而不是固定系数 EMA
 *
 * 固定 α 的 EMA 只有一个旋钮，抖动与延迟此消彼长：调到不抖，照片被挪动时就要滑
 * 好几秒才跟上；调到跟得上，静止时就还在抖。1€ 滤波让截止频率随速度上升 ——
 * 静止时截止低（不抖），真动起来时截止自动升高（不拖）。
 *
 * ## 为什么旋转必须 slerp，不能逐分量 EMA
 *
 * 四元数逐分量线性插值出来的东西**不是**一个旋转（模长不为 1），归一化之后角速度
 * 也不均匀。更要命的是符号：q 与 -q 是同一个旋转，ARCore 相邻两帧完全可能给出符号
 * 相反的四元数，此时逐分量插值会从长弧绕过去 —— 屏幕上是视频**翻一下面**。所以
 * [slerp] 里第一件事就是点积为负时取反。
 *
 * ## 与帧率无关，这是设计要求不是巧合
 *
 * 相机档位由机型决定（30 / 60，将来可能更高），所以这里**任何参数都不能以"帧"为
 * 单位**。三处都按时间表达：
 *
 * - 平滑系数 α 由 `dt` 与截止频率算出（[alphaOf]），时间常数因此固定 —— 同样的墙钟
 *   时间之后收敛到同一位置，换帧率不需要重新调参。
 * - 异常帧门限是**速度**（[MAX_JUMP_SPEED_M_S] / [MAX_JUMP_SPEED_DEG_S]），乘 `dt`
 *   得到这一帧的容许量。
 * - 拒收逃逸口是**时间**（[JUMP_TOLERANCE_MS]），不是帧数。
 *
 * 满足这三条之后，提高帧率是纯赚：时间常数不变而采样点变多，同一个时间窗里平均掉的
 * 噪声更多，抖动抑制自己变好，不用动任何参数。
 */
class PoseFilter(
    private val fcMinT: Float = FC_MIN_T,
    private val betaT: Float = BETA_T,
    private val fcMinR: Float = FC_MIN_R,
    private val betaR: Float = BETA_R,
) {

    companion object {
        /**
         * 位移的最低截止频率（Hz）。静止时就是它在起作用。
         *
         * 5 Hz ≈ 时间常数 32 ms ≈ 30fps 下的**一帧**。这个值是从「延迟必须看不见」
         * 倒推的：滞后大致就等于时间常数，一帧的滞后在手持晃动下察觉不到。
         *
         * 换来的抑制比是 `sqrt(α/(2-α))`：30 fps 下 α≈0.51 → 0.59（1.7 倍），
         * 60 fps 下 α≈0.34 → 0.45（2.2 倍）。比原来那版（4~5 倍）差很多，这是**明知
         * 故犯**的取舍 —— 理由和"如果抖动回来了该怎么办"都写在类注释
         * 「于是取舍变成：只在静止时滤」那一段。
         *
         * 这里原来是 0.5 Hz（时间常数 0.32 s），来自一个错误论证，实机表现就是用户
         * 报的「贴合有延迟」。**要往回调之前先读那一段。**
         */
        const val FC_MIN_T = 5.0f

        /**
         * 位移的速度增益，单位 Hz/(m/s)。
         *
         * 20：以 0.3 m/s 移动时截止抬到 11 Hz（α≈0.70 @30fps），基本透明；静止时抖动
         * 伪装出来的那点速度（±2 mm @30fps ≈ 0.06 m/s）只抬到 6.2 Hz，影响很小。
         *
         * ## 为什么调 β 拿不到"静止很稳 + 运动不拖"两全
         *
         * 因为两者的速度区间是**重叠**的：抖动伪速度约 0.06 m/s，而人看照片时手部移动
         * 就在 0.05–0.3 m/s。速度这一个量分不开它们。
         *
         * 上一版把 β 从 40 压到 3 正是因为这个重叠（抖动把滤波器自己撑开，压制比从
         * 4 倍掉到 1.5 倍，是实测出来的），而那个方向换来的就是现在要修的延迟。
         * 结论：因果滤波器在这个信噪结构下**不可能两全**，要两全得换机制（世界锚点，
         * 见类注释末尾）。
         */
        const val BETA_T = 20f

        /** 旋转的最低截止频率（Hz）。比位移高一点：角度上的滞后比位置上的更显眼
         *  （视频四边形会看起来"歪着追上来"），而角度抖动本身幅度更小。 */
        const val FC_MIN_R = 6.0f

        /** 旋转的速度增益，单位 Hz/(deg/s)。0.1：转到 60 deg/s 时截止抬到 12 Hz。 */
        const val BETA_R = 0.1f

        /** 导数自身的低通截止（Hz）。1€ 滤波的标准取值，用来防止"速度估计本身在抖，
         *  于是截止频率也在抖"这种自激。 */
        const val FC_DERIVATIVE = 1.0f

        /**
         * 位移跳变的容许上限，单位 **米/秒**。超过即判异常帧。
         *
         * 1.5 m/s。照片是钉着不动的，它的世界位姿不可能以这个速度移动 —— 真出现只能是
         * ARCore 的位姿估计出错（大角度下最常见）。
         *
         * ## 为什么是速度而不是「每帧多少米」
         *
         * 原来写的是 `MAX_JUMP_M = 0.05`（每帧 5 cm）。那个写法在 30 fps 下等价于此处的
         * 1.5 m/s，**但它和帧率绑死了**：相机档位从 30 提到 60 之后，5 cm/帧 就变成
         * 3 m/s，异常帧闸门无声地松了一倍 —— 该拒的跳变被放进来，而没有任何迹象。
         * 帧率是会变的（不同机型给的档位不同），所以门限必须表达成与帧率无关的物理量。
         */
        const val MAX_JUMP_SPEED_M_S = 1.5f

        /** 旋转跳变的容许上限，单位 **度/秒**。同上，360°/s（= 旧写法 12°/帧 @30fps）。 */
        const val MAX_JUMP_SPEED_DEG_S = 360f

        /**
         * 连续拒收多久之后改为**采纳**（直接跳过去，不插值）。单位毫秒。
         *
         * 没有这个逃逸口，拒收门就是个死锁：照片真被挪到新位置之后每一帧都超阈值、
         * 每一帧都被拒，视频永远贴在旧位置上，而日志里看不出任何异常。
         *
         * 133 ms（= 旧写法 4 帧 @30fps）。同样从「帧数」改成「时间」：按帧数算的话，
         * 60 fps 下这个逃逸口会在 67 ms 就触发，等于把闸门的耐心砍掉一半，零星错帧
         * 更容易被误认成「照片真的动了」而直接跳过去。
         *
         * 与上面两个速度门限是一对：真实的位置变化会持续超速，而 ARCore 的错帧是零星的。
         * 拒收期间刻意不推进时钟，所以 dt 会一直变大、速度门限跟着放宽 —— 一次真实的
         * 缓慢位移会自己变成「合理速度」被跟上，这个逃逸口只兜住突变那一类。
         */
        const val JUMP_TOLERANCE_MS = 133L

        /** 两帧间隔的夹取范围（秒）。GL 线程被别的事情堵住时 dt 会突然很大，
         *  不夹的话 α 会冲到 1（等于这一帧完全不滤），表现为偶发的一下抖动。 */
        private const val MIN_DT_S = 1f / 240f
        private const val MAX_DT_S = 1f / 10f

        private const val NS_PER_S = 1_000_000_000f
        private const val TWO_PI = 2f * Math.PI.toFloat()
        private const val RAD_TO_DEG = 180f / Math.PI.toFloat()

        /** 1€ 滤波的平滑系数：`α = 1 / (1 + τ/dt)`，`τ = 1/(2π·fc)`。 */
        internal fun alphaOf(dtS: Float, fc: Float): Float {
            if (fc <= 0f) return 0f
            val tau = 1f / (TWO_PI * fc)
            return 1f / (1f + tau / dtS)
        }

        /**
         * 两个四元数之间的夹角（度）。输入不必归一化。
         *
         * 用 `|dot|` 而不是 `dot`：q 与 -q 同一个旋转，取绝对值等于总是量短弧那一侧，
         * 否则符号翻转的相邻两帧会被算成 180° 附近的巨大跳变、每一帧都被拒收。
         */
        internal fun angleDeg(a: FloatArray, b: FloatArray): Float {
            val na = norm4(a)
            val nb = norm4(b)
            if (na == 0f || nb == 0f) return 0f
            var dot = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]) / (na * nb)
            dot = abs(dot).coerceAtMost(1f)
            return 2f * acos(dot) * RAD_TO_DEG
        }

        /** 球面线性插值，`t=0` 给 a、`t=1` 给 b。结果已归一化。 */
        internal fun slerp(a: FloatArray, b: FloatArray, t: Float, out: FloatArray) {
            var bx = b[0]
            var by = b[1]
            var bz = b[2]
            var bw = b[3]
            var dot = a[0] * bx + a[1] * by + a[2] * bz + a[3] * bw
            // 同一个旋转的两种写法。不取反就会走长弧 —— 屏幕上是视频翻面。
            if (dot < 0f) {
                bx = -bx; by = -by; bz = -bz; bw = -bw
                dot = -dot
            }
            dot = dot.coerceIn(-1f, 1f)
            // 两者几乎重合时 sin(θ) → 0，除法会炸。退化成线性插值再归一化：
            // 这个区间里两条曲线的差别远小于 float 精度。
            if (dot > 0.9995f) {
                out[0] = a[0] + (bx - a[0]) * t
                out[1] = a[1] + (by - a[1]) * t
                out[2] = a[2] + (bz - a[2]) * t
                out[3] = a[3] + (bw - a[3]) * t
            } else {
                val theta = acos(dot)
                val sinTheta = sin(theta)
                val wa = sin((1f - t) * theta) / sinTheta
                val wb = sin(t * theta) / sinTheta
                out[0] = a[0] * wa + bx * wb
                out[1] = a[1] * wa + by * wb
                out[2] = a[2] * wa + bz * wb
                out[3] = a[3] * wa + bw * wb
            }
            normalize4(out)
        }

        /**
         * 位移 + 四元数 → 列主序 4x4 模型矩阵，与 `Pose.toMatrix` 同布局。
         *
         * 列主序意味着 `m[col*4 + row]`，平移落在 m[12..14] —— 写成行主序的话视频会
         * 贴在一个被转置过的位姿上（看起来是"贴在旁边某处并且朝向不对"）。
         */
        internal fun writeMatrix(t: FloatArray, q: FloatArray, out: FloatArray) {
            val n = norm4(q)
            // 退化四元数（全 0）只可能来自没初始化的缓冲区。给单位旋转而不是 NaN：
            // NaN 会顺着 MVP 传到顶点着色器，整块四边形静默消失、没有任何报错。
            val s = if (n == 0f) 0f else 1f / n
            val x = q[0] * s
            val y = q[1] * s
            val z = q[2] * s
            val w = if (n == 0f) 1f else q[3] * s

            out[0] = 1f - 2f * (y * y + z * z)
            out[1] = 2f * (x * y + w * z)
            out[2] = 2f * (x * z - w * y)
            out[3] = 0f
            out[4] = 2f * (x * y - w * z)
            out[5] = 1f - 2f * (x * x + z * z)
            out[6] = 2f * (y * z + w * x)
            out[7] = 0f
            out[8] = 2f * (x * z + w * y)
            out[9] = 2f * (y * z - w * x)
            out[10] = 1f - 2f * (x * x + y * y)
            out[11] = 0f
            out[12] = t[0]
            out[13] = t[1]
            out[14] = t[2]
            out[15] = 1f
        }

        private fun norm4(q: FloatArray): Float =
            sqrt(q[0] * q[0] + q[1] * q[1] + q[2] * q[2] + q[3] * q[3])

        private fun normalize4(q: FloatArray) {
            val n = norm4(q)
            if (n == 0f) {
                q[0] = 0f; q[1] = 0f; q[2] = 0f; q[3] = 1f
                return
            }
            val s = 1f / n
            q[0] *= s; q[1] *= s; q[2] *= s; q[3] *= s
        }
    }

    /** 采纳这一帧的结果分类，供渲染层与日志区分「稳了」和「刚跳过一次」。 */
    enum class Verdict {
        /** 第一帧，直接采纳，没有插值。 */
        SEEDED,

        /** 正常滤波。 */
        SMOOTHED,

        /** 判为异常帧，已丢弃；对外的位姿沿用上一帧。 */
        REJECTED,

        /** 连续异常够多帧了，认定真的动了，直接跳过去。 */
        SNAPPED,
    }

    private var seeded = false
    private var lastNs = 0L

    /** 当前这串连续拒收是从什么时候开始的；0 = 没在拒收。见 [JUMP_TOLERANCE_MS]。 */
    private var rejectSinceNs = 0L

    private val filtT = FloatArray(3)
    private val filtQ = floatArrayOf(0f, 0f, 0f, 1f)

    /** 上一帧的**原始**输入。异常判定要跟原始值比，不能跟滤波值比 —— 跟滤波值比的话，
     *  滤波器自身的滞后会被算进"跳变量"里，静止时也可能误判。 */
    private val prevRawT = FloatArray(3)
    private val prevRawQ = floatArrayOf(0f, 0f, 0f, 1f)

    /** 已低通的速度估计。 */
    private val velT = FloatArray(3)
    private var velR = 0f

    private val tmpQ = FloatArray(4)

    /** 已经有一个可用位姿了吗。false 时 [toMatrix] 的输出没有意义。 */
    val hasPose: Boolean get() = seeded

    fun reset() {
        seeded = false
        lastNs = 0L
        rejectSinceNs = 0L
        velT[0] = 0f; velT[1] = 0f; velT[2] = 0f
        velR = 0f
    }

    /**
     * 送进一帧原始位姿。
     *
     * @param rawT 世界坐标位移，长度 ≥ 3
     * @param rawQ 旋转四元数 (x, y, z, w)，长度 ≥ 4，不必归一化
     * @param nowNs 单调时钟（`System.nanoTime`）
     */
    fun update(rawT: FloatArray, rawQ: FloatArray, nowNs: Long): Verdict {
        if (!seeded) {
            // 首帧直接采纳。**不能**从零位姿插值过去 —— 那会让视频从相机原点飞到
            // 照片上，命中瞬间是一段无意义的动画。
            copy3(rawT, filtT)
            copy4(rawQ, filtQ)
            normalize4(filtQ)
            copy3(rawT, prevRawT)
            copy4(rawQ, prevRawQ)
            velT[0] = 0f; velT[1] = 0f; velT[2] = 0f
            velR = 0f
            lastNs = nowNs
            rejectSinceNs = 0L
            seeded = true
            return Verdict.SEEDED
        }

        val dtS = ((nowNs - lastNs) / NS_PER_S).coerceIn(MIN_DT_S, MAX_DT_S)

        val jumpM = dist3(rawT, prevRawT)
        val jumpDeg = angleDeg(rawQ, prevRawQ)
        // 门限按 dt 换算，所以与帧率无关（理由见 MAX_JUMP_SPEED_M_S）。
        val outlier =
            jumpM > MAX_JUMP_SPEED_M_S * dtS || jumpDeg > MAX_JUMP_SPEED_DEG_S * dtS

        if (outlier) {
            if (rejectSinceNs == 0L) rejectSinceNs = nowNs
            if (nowNs - rejectSinceNs < JUMP_TOLERANCE_MS * 1_000_000L) {
                // 时钟与"上一帧原始值"都**不更新**。更新了的话，连续几帧渐进式漂移
                // 每一步都不超阈值，整体却漂走了 —— 拒收门等于没有。
                return Verdict.REJECTED
            }
        }

        val snapped = outlier
        rejectSinceNs = 0L
        lastNs = nowNs

        if (snapped) {
            // 认定真的动了。直接跳，不插值：已经连续超过 JUMP_TOLERANCE_MS 都在别处，
            // 再滑过去只是多一段错位。
            copy3(rawT, filtT)
            copy4(rawQ, filtQ)
            normalize4(filtQ)
            copy3(rawT, prevRawT)
            copy4(rawQ, prevRawQ)
            velT[0] = 0f; velT[1] = 0f; velT[2] = 0f
            velR = 0f
            return Verdict.SNAPPED
        }

        // ---- 速度估计（自身也低通，见 FC_DERIVATIVE）----
        val aD = alphaOf(dtS, FC_DERIVATIVE)
        for (i in 0..2) {
            val raw = (rawT[i] - prevRawT[i]) / dtS
            velT[i] += aD * (raw - velT[i])
        }
        velR += aD * (jumpDeg / dtS - velR)

        val speedT = sqrt(velT[0] * velT[0] + velT[1] * velT[1] + velT[2] * velT[2])
        val aT = alphaOf(dtS, fcMinT + betaT * speedT)
        val aR = alphaOf(dtS, fcMinR + betaR * abs(velR))

        for (i in 0..2) filtT[i] += aT * (rawT[i] - filtT[i])
        slerp(filtQ, rawQ, aR, tmpQ)
        copy4(tmpQ, filtQ)

        copy3(rawT, prevRawT)
        copy4(rawQ, prevRawQ)
        return Verdict.SMOOTHED
    }

    /** 把当前（滤波后的）位姿写成列主序 4x4。[hasPose] 为 false 时输出单位矩阵。 */
    fun toMatrix(out: FloatArray) {
        if (!seeded) {
            out.fill(0f)
            out[0] = 1f; out[5] = 1f; out[10] = 1f; out[15] = 1f
            return
        }
        writeMatrix(filtT, filtQ, out)
    }

    /** 只给测试与日志用。 */
    internal fun translationInto(out: FloatArray) = copy3(filtT, out)

    /** 只给测试与日志用。 */
    internal fun rotationInto(out: FloatArray) = copy4(filtQ, out)

    private fun copy3(src: FloatArray, dst: FloatArray) {
        dst[0] = src[0]; dst[1] = src[1]; dst[2] = src[2]
    }

    private fun copy4(src: FloatArray, dst: FloatArray) {
        dst[0] = src[0]; dst[1] = src[1]; dst[2] = src[2]; dst[3] = src[3]
    }

    private fun dist3(a: FloatArray, b: FloatArray): Float {
        val dx = a[0] - b[0]
        val dy = a[1] - b[1]
        val dz = a[2] - b[2]
        return sqrt(dx * dx + dy * dy + dz * dz)
    }
}

package app.photoar.arview

import java.nio.ByteBuffer

/**
 * 抽帧规格与 YUV 打包。纯函数，JVM 可测。
 *
 * §7 原本规定送去识别的帧是「长边 640px、q70 的 JPEG，约 50KB」，Phase 0 的基线
 * 也都是在 640 上跑的。**现已改为 1280**，理由见 [LONG_EDGE]。
 */
object Frames {

    /**
     * 送去识别的帧的长边。
     *
     * 服务端那边有两个配套的旋钮：`backend.QUERY_N_FEATURES`(4000) 是查询帧的 ORB
     * 特征预算（入库侧仍是 300），`backend.QUERY_LONG_EDGE`(1280) 是服务端**处理**
     * 长边。三个数字要一起看 —— 实测（bench/simcam.py + 用户的真实婚礼照 + 手机拍
     * 的真实桌面场景，5 个随机视角取「全部过门槛」）：
     *
     *   发帧    服务端处理  特征   全过的最小占比
     *   640     640         300    一档都不全过      ← 真机扫不出来就是这一行
     *   640     640         4000   一档都不全过
     *   1280    640         4000   一档都不全过      ← 只改这个常量、服务端不改的话
     *   640     1280        4000   0.5
     *   1280    1280        4000   0.4               ← 现在这一档
     *   1280    1600        4000   0.4（持平，白花 CPU）
     *   1280    1920        4000   退化回一档都不全过
     *
     * 所以**主导变量是服务端处理长边，不是这个常量**：服务端把 640 的帧放大到 1280
     * 就已经能到 0.5（放大不是凭空造信息，是把查询侧的尺度对回入库侧 —— 入库时照片
     * 铺满画面，手持时照片只占一小块）。这个常量的价值是把 0.5 再推到 0.4：真实的
     * 1280 像素比插值出来的强。粗排 Top-20 命中率也是同一档改善：5/20 → 20/20。
     *
     * 代价：单帧上行 32.9KB → 87.6KB（2.7 倍，约 220KB/s per client）。哪天要省流量，
     * 退回 640 只掉到 0.5、不会掉回「扫不出来」，因为服务端会放大。
     */
    const val LONG_EDGE = 1280
    const val JPEG_QUALITY = 70

    /** 相机档位的下限，比这更小的分辨率提不出够用的特征，直接排除。 */
    private const val MIN_EDGE_W = 320
    private const val MIN_EDGE_H = 240

    data class Size(val width: Int, val height: Int)

    /**
     * 缩到长边 [longEdge]。已经不超过就原样返回（绝不放大 —— 放大不会增加
     * 任何特征，只会让 ORB 在插值出来的像素上找角点）。
     */
    fun targetSize(width: Int, height: Int, longEdge: Int = LONG_EDGE): Size {
        require(width > 0 && height > 0) { "尺寸必须为正：${width}x$height" }
        val long = maxOf(width, height)
        if (long <= longEdge) return Size(width, height)
        val scale = longEdge.toDouble() / long
        return Size(
            width = even((width * scale).toInt()),
            height = even((height * scale).toInt()),
        )
    }

    /**
     * 从相机支持的 [candidates] 里挑一个输出档位；一个都不可用时返回 null。
     *
     * 因为 [targetSize] 绝不放大，相机给多大就封顶了识别能看到多少像素 —— 挑错
     * 这一步，服务端那 4000 个特征预算就是空转。所以规则是「宁可大一点再缩」：
     * 先要长边 ≥ [longEdge] 里最小的那个，没有才退而取最大的可用档。距离相等时
     * 偏大的一侧，因为小的那个只能靠插值补，那是凭空造像素。
     *
     * 返回 null 而不是替调用方编一个 640x480：编出来的档位相机未必支持，真机上
     * 表现为配置会话失败，排查时看不出是这里替它决定的。
     */
    fun pickCameraSize(candidates: List<Size>, longEdge: Int = LONG_EDGE): Size? {
        val usable = candidates.filter { it.width >= MIN_EDGE_W && it.height >= MIN_EDGE_H }
        if (usable.isEmpty()) return null
        return usable.filter { maxOf(it.width, it.height) >= longEdge }
            .minByOrNull { maxOf(it.width, it.height) }
            ?: usable.maxByOrNull { maxOf(it.width, it.height) }
    }

    /** 一个相机档位里我们在意的两件事：CPU 图像多大、最高能跑多少帧。 */
    data class CameraOption(val size: Size, val maxFps: Int)

    /**
     * 从相机支持的档位里挑一个：**尺寸达标是硬闸门，达标之后帧率优先**。
     *
     * ## 尺寸仍然是硬约束
     *
     * **CPU 图像尺寸决定识别能不能成。** 实测（见 [LONG_EDGE] 上面那张表）处理长边
     * 640 时「一档都不全过」，1280 才到 fill 0.4。所以长边 < [longEdge] 的档位一律
     * 不考虑 —— 某些机型只在 640×480 上给 60fps，挑它换来的是一个跟得很稳但**永远
     * 认不出照片**的 AR。
     *
     * ## 达标之后为什么改成帧率优先
     *
     * 原来是「先取达标里最小的那个尺寸，再在**同尺寸**内取最高帧率」。那一版在一类
     * 很常见的机型上会白丢一半帧率：1280×960 只有 30fps，而 1440×1080 或 1920×1440
     * 有 60fps —— 两个尺寸都达标，但原规则先把尺寸锁死在 1280×960，于是 60fps 那档
     * 从来没被看见过。
     *
     * 而帧率就是**跟随帧率**：ARCore 的位姿更新率等于相机帧率（`baseConfig` 里
     * `updateMode = BLOCKING`，渲染跟着相机走）。30 → 60 等于位姿更新翻倍、斜视掉帧
     * 之后回来的时间减半。代价只是耗电和发热，而这个场景是手持几十秒看一段短视频。
     *
     * 顺序因此是：**达标 → 最高帧率 → 同帧率里最小的尺寸**。最后那一档仍然偏小的，
     * 理由没变（大帧的上行更贵，而识别率在达标之后就饱和了）。
     *
     * ⚠️ ARCore 的 `CameraConfig.TargetFps` 只有 30 和 60 两个值，所以在 AR 那条路上
     * 这个函数实际的上限就是 60 —— 「再翻一倍到 120」不是这里能给的，要等 ARCore 加
     * 那个枚举值。这里不设上限是为了 Camera2 兜底那条路（它没有这个限制）。
     *
     * @return null = 一个可用档位都没有。**不替调用方编一个** —— 编出来的档位相机未必
     *   支持，真机上表现为配置会话失败，而排查时看不出是这里替它决定的。
     */
    fun pickCameraOption(
        candidates: List<CameraOption>,
        longEdge: Int = LONG_EDGE,
    ): CameraOption? {
        val usable = candidates.filter {
            it.size.width >= MIN_EDGE_W && it.size.height >= MIN_EDGE_H
        }
        if (usable.isEmpty()) return null
        val qualified = usable.filter { maxOf(it.size.width, it.size.height) >= longEdge }
        // 一个达标尺寸都没有时退回老规则：尺寸取最大（识别率在这一档是瓶颈，能多一个
        // 像素就多一个），再在同尺寸里挑帧率。此时「帧率优先」是错的 —— 那会为了
        // 60fps 去挑一个更小的帧，而这一档的识别本来就已经在悬崖边上。
        if (qualified.isEmpty()) {
            val want = pickCameraSize(usable.map { it.size }, longEdge) ?: return null
            return usable.filter { it.size == want }.maxByOrNull { it.maxFps }
        }
        val bestFps = qualified.maxOf { it.maxFps }
        return qualified
            .filter { it.maxFps == bestFps }
            .minByOrNull { maxOf(it.size.width, it.size.height) }
    }

    private fun even(v: Int): Int = maxOf(2, v - (v % 2))

    fun nv21Size(width: Int, height: Int): Int = width * height * 3 / 2

    /**
     * YUV_420_888 的三个平面打成 NV21（Y 全平面 + 交错的 VU）。
     *
     * 两个坑，都不报错只出花屏：
     * 1. `rowStride` 常常大于 `width`（相机为对齐补了尾字节），按 width 连续
     *    读会越读越偏，画面斜切。
     * 2. U/V 平面的 `pixelStride` 在多数机型上是 2（本来就是交错存的），
     *    按 1 读会把 U 当成 V，人脸发绿。
     * 所以两个 stride 都必须显式传进来。
     *
     * ## 按行批量拷，不逐字节
     *
     * 这个函数在 **GL 线程**上跑（`ArRenderer.maybeCapture`），每 400ms 一次，1280×960
     * 是 184 万字节。原来的写法是三层循环里 `ByteBuffer.get(index)` 一个字节一个字节地
     * 取 —— 那是 184 万次带边界检查的调用。改成每行一次 `get(byte[], off, len)`（也就是
     * 一次 memcpy）之后：
     *
     *   逐字节  883 µs/帧
     *   批量    392 µs/帧      ← 现在这样，字节输出完全一致（有测试盯着）
     *
     * 这两个数是在**桌面 JVM** 上量的（HotSpot 把逐字节那版优化得相当好），ART 上差距
     * 会更大。但也要说清楚：**这不是「相机卡顿」的成因** —— 0.9ms 每 400ms 一次，摊到
     * 帧上看不见。它只是一处白扔的 CPU，顺手收掉；卡顿的真正来源要看 [FrameStats] 报
     * 出来的帧耗时，那是量出来的，不是猜的。
     *
     * 三个平面各 `duplicate()` 一份再读：批量 `get` 会推进 position，而调用方（以及
     * `android.media.Image` 的下一个使用者）有权假设自己传进来的 buffer 没被动过。
     * duplicate 只是个几十字节的视图对象，不拷数据。
     */
    fun toNv21(
        width: Int,
        height: Int,
        y: ByteBuffer,
        yRowStride: Int,
        u: ByteBuffer,
        uRowStride: Int,
        uPixelStride: Int,
        v: ByteBuffer,
        vRowStride: Int,
        vPixelStride: Int,
        out: ByteArray,
    ) {
        require(width > 0 && height > 0) { "尺寸必须为正：${width}x$height" }
        require(width % 2 == 0 && height % 2 == 0) {
            "YUV_420_888 的宽高必须是偶数，收到 ${width}x$height"
        }
        val need = nv21Size(width, height)
        require(out.size >= need) { "out 至少要 $need 字节，只有 ${out.size}" }

        val yb = y.duplicate()
        val ub = u.duplicate()
        val vb = v.duplicate()

        var o = 0
        for (row in 0 until height) {
            yb.position(row * yRowStride)
            yb.get(out, o, width)
            o += width
        }

        // 色度平面按行读进小数组再交错。为什么不直接一次 memcpy 整块：
        // pixelStride == 2 时 U 和 V 是**共用**一段内存交错存的，看起来可以整块拷，
        // 但「先 V 后 U」（NV21）还是「先 U 后 V」（NV12）在 YUV_420_888 里没有保证，
        // 而拷错了不报错 —— 只是整幅画面的色调互补。所以仍然按 pixelStride 逐个取，
        // 只是取的对象从 ByteBuffer 换成了 ByteArray（快一个量级），而这一步的正确性
        // 完全由传进来的 stride 决定，和原来一样。
        val chromaW = width / 2
        val uRow = ByteArray((chromaW - 1) * uPixelStride + 1)
        val vRow = ByteArray((chromaW - 1) * vPixelStride + 1)
        for (row in 0 until height / 2) {
            // 最后一行可能没有完整的 stride（相机只给到最后一个有效字节），所以读多少
            // 要跟 limit 取小 —— 照 stride 读会 BufferUnderflowException。
            val uBase = row * uRowStride
            val vBase = row * vRowStride
            ub.position(uBase)
            ub.get(uRow, 0, minOf(uRow.size, ub.limit() - uBase))
            vb.position(vBase)
            vb.get(vRow, 0, minOf(vRow.size, vb.limit() - vBase))
            // NV21 是 V 在前、U 在后。写反了整幅画面的色调会互补。
            for (col in 0 until chromaW) {
                out[o++] = vRow[col * vPixelStride]
                out[o++] = uRow[col * uPixelStride]
            }
        }
    }
}

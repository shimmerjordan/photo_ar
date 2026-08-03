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
     * 从相机支持的档位里挑一个：**先定尺寸，再在同尺寸里取最高帧率**。
     *
     * ## 为什么顺序是「尺寸优先」而不是「帧率优先」
     *
     * 这两件事影响的是不同的东西，而且量级差得远：
     *
     * - **CPU 图像尺寸决定识别能不能成。** 实测（见 [LONG_EDGE] 上面那张表）处理长边
     *   640 时「一档都不全过」，1280 才到 fill 0.4。挑小了，识别直接不工作。
     * - **帧率决定贴合稳不稳、丢了多久能回来。** 30 → 60 是「更好」，不是「能不能」。
     *
     * 所以尺寸是硬约束，帧率是在硬约束满足之后的择优。反过来排的后果是：某些机型只在
     * 640×480 上提供 60fps，于是为了帧率把识别牺牲掉 —— 换来一个跟得很稳但**永远认不出
     * 照片**的 AR。
     *
     * 同尺寸内取最高帧率不设上限（不是"最多 60"）：这个 App 的场景是短视频、手持几十秒，
     * 用户明确不要省电，机型给到 90/120 就用。真实上限由相机硬件给。
     *
     * @return null = 一个可用档位都没有。**不替调用方编一个** —— 编出来的档位相机未必
     *   支持，真机上表现为配置会话失败，而排查时看不出是这里替它决定的。
     */
    fun pickCameraOption(
        candidates: List<CameraOption>,
        longEdge: Int = LONG_EDGE,
    ): CameraOption? {
        val want = pickCameraSize(candidates.map { it.size }, longEdge) ?: return null
        return candidates.filter { it.size == want }.maxByOrNull { it.maxFps }
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

        var o = 0
        for (row in 0 until height) {
            val base = row * yRowStride
            for (col in 0 until width) {
                out[o++] = y.get(base + col)
            }
        }
        // NV21 是 V 在前、U 在后。写反了整幅画面的色调会互补。
        for (row in 0 until height / 2) {
            val vBase = row * vRowStride
            val uBase = row * uRowStride
            for (col in 0 until width / 2) {
                out[o++] = v.get(vBase + col * vPixelStride)
                out[o++] = u.get(uBase + col * uPixelStride)
            }
        }
    }
}

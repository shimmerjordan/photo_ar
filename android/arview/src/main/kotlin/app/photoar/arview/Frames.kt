package app.photoar.arview

import java.nio.ByteBuffer

/**
 * 抽帧规格与 YUV 打包。纯函数，JVM 可测。
 *
 * §7 规定送去识别的帧是「长边 640px、q70 的 JPEG，约 50KB」。这个数字不是随手
 * 定的：Phase 0 的全部实测指标都是在长边 640 上跑出来的，改了就得重跑基线。
 */
object Frames {

    const val LONG_EDGE = 640
    const val JPEG_QUALITY = 70

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

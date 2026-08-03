package app.photoar.standalone

/**
 * 打印出来那张实体照片有多宽（毫米）。
 *
 * ## 为什么它回来了
 *
 * 第 5 轮把这个输入框去掉了，理由是「一个**猜的**宽度比不填更糟」：那时四边形的大小是按
 * 申报宽度算的，填错百分之几，视频就比照片大百分之几、边缘对不齐。
 *
 * 但同一轮之后四边形的尺寸改成取 **ARCore 自己量的 `extentX`** 了（`Geometry.quadSize`
 * 优先用它，申报宽度只在它还没给出来时垫一下）。也就是说**那个反对理由已经不成立**：
 * 填一个稍微不准的宽度不再影响贴合精度。
 *
 * 而它还剩一个作用，而且是关键的那个：**帮 ARCore 检测。** 物理尺寸未知时 ARCore 必须靠
 * 视差自己量出照片有多大，那需要用户挪动手机几厘米才收敛（见
 * `ArSessionHolder.loadTargetFromBitmap` 的注释）—— 而一个举着手机对准照片的人不会自发
 * 这么做。填了宽度，ARCore 一认出图案就能直接给位姿。
 *
 * 这就是「认出来了，但没在画面里找到」最常见的成因。所以这个字段是**可选但强烈建议**的，
 * 而且给了相纸预设 —— 让人从「量一下」变成「点一下」。
 *
 * ## 为什么给的是宽度而不是长边
 *
 * 服务端那一列叫 `print_width_m`，ARCore 的 `addImage` 也要 width。而照片可能是横的也可能
 * 是竖的：同一张 6 寸照片，横着放宽 152、竖着放宽 102。所以预设里两个方向都列出来，
 * 让用户按**实际摆放**挑，而不是让我们去猜方向再换算 —— 猜错的话宽高刚好对调。
 */
enum class PrintSize(
    val label: String,
    /** 0 = 未知。 */
    val widthMm: Double,
    val hint: String,
) {
    /**
     * 不知道。
     *
     * 仍然是默认值：填错一个数不会毁掉贴合（尺寸取 `extentX`），但用户如果不知道，
     * 让他猜一个不如让 ARCore 自己量。文案里要说清代价。
     */
    UNKNOWN("不知道", 0.0, "ARCore 自己量 —— 扫的时候要轻轻晃一下手机才贴得上"),

    /** 6 寸横放（4R，152×102mm）。冲印店最常见的尺寸。 */
    SIX_INCH_LANDSCAPE("6寸 横", 152.0, "常见的冲印尺寸，横着摆"),

    /** 6 寸竖放。 */
    SIX_INCH_PORTRAIT("6寸 竖", 102.0, "同一张 6 寸，竖着摆"),

    /** 5 寸横放（3R，127×89mm）。 */
    FIVE_INCH_LANDSCAPE("5寸 横", 127.0, "比 6 寸小一号，横着摆"),

    /** 5 寸竖放。 */
    FIVE_INCH_PORTRAIT("5寸 竖", 89.0, "同一张 5 寸，竖着摆"),

    /** A4 横放（297×210mm）。相册内页、海报常见。 */
    A4_LANDSCAPE("A4 横", 297.0, "A4 纸横着摆"),

    /** A4 竖放。 */
    A4_PORTRAIT("A4 竖", 210.0, "A4 纸竖着摆"),
    ;

    val known: Boolean get() = widthMm > 0.0

    companion object {
        /**
         * 界面上按这个顺序排。UNKNOWN 放第一个是因为它是默认值 —— 排在中间的话，
         * 一个不想选的人得先找到它。
         */
        val ORDER: List<PrintSize> = entries.toList()

        /**
         * 一个自己量的宽度 → 最接近的预设，用来在「自定义」输入之后回显。
         *
         * 容差 ±3mm：冲印尺寸本来就有裁切公差，152 和 150 是同一种纸。超出容差返回 null
         * （那就是真的自定义），**不四舍五入到最近的那个** —— 把一张 A5 说成 A4 比说
         * 「自定义」糟得多。
         */
        fun match(widthMm: Double): PrintSize? =
            entries.firstOrNull { it.known && kotlin.math.abs(it.widthMm - widthMm) <= 3.0 }
    }
}

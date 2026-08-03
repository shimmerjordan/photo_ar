package app.photoar.arview.media

/**
 * 存进相册时的文件名与 MIME。纯函数，JVM 可测 —— 相册那一半（MediaStore）没法测，
 * 但真正会出错的是这一半。
 */
object SaveNaming {

    /** 相册里的文件夹名。照片进 `Pictures/`、视频进 `Movies/`，都放这个子目录下。 */
    const val ALBUM = "PhotoAR"

    /**
     * 文件名里不允许出现的字符。
     *
     * `/` 会被当成路径分隔符，在 MediaStore 里表现为「插入成功但文件在别的目录」；
     * Windows 保留的那几个（`\ : * ? " < > |`）留着是因为用户会把相册同步到电脑或
     * 上传到网盘，那边会拒收或静默改名。控制字符同理。
     */
    private val ILLEGAL = Regex("""[/\\:*?"<>|\x00-\x1F]""")

    private val MIME_EXT = mapOf(
        "image/jpeg" to "jpg",
        "image/png" to "png",
        "image/webp" to "webp",
        "video/mp4" to "mp4",
        "video/quicktime" to "mov",
        "video/webm" to "webm",
    )

    /**
     * MIME → 扩展名。不认识的返回 null。
     *
     * 返回 null 而不是猜一个 `.jpg`：猜错的后果是相册里一个打不开的文件，而用户会
     * 以为是照片本身坏了。让调用方显式处理"这个类型不支持"。
     */
    fun extensionOf(mime: String?): String? =
        MIME_EXT[mime?.substringBefore(';')?.trim()?.lowercase()]

    /**
     * 相册里的显示名。
     *
     * @param title 照片标题，可能为空
     * @param photoId 兜底用，也用来去重
     * @param mime 决定扩展名
     * @return null = 这个 MIME 不支持，调用方应报错而不是硬存
     *
     * 名字里带 photoId 的前 8 位而不是只用标题：同一场婚礼里「合照」这种标题会重复，
     * 而 MediaStore 遇到重名会自己加 `(1)` —— 那种名字对用户毫无意义，而且第二次
     * 保存同一张照片时会变成两份，看不出是同一张。带上 id 之后重复保存是**同名**，
     * 语义上就是"这一张"。
     */
    fun displayName(title: String?, photoId: String, mime: String?): String? {
        val ext = extensionOf(mime) ?: return null
        val base = title?.let { ILLEGAL.replace(it, "_").trim() }
            ?.takeIf { it.isNotEmpty() }
            ?: "photoar"
        // 长名字在某些文件系统上会被截断，截断之后又可能撞名。留出扩展名和 id 的位置。
        val short = if (base.length > 40) base.substring(0, 40) else base
        val idPart = photoId.take(8).ifEmpty { "unknown" }
        return "$short-$idPart.$ext"
    }
}

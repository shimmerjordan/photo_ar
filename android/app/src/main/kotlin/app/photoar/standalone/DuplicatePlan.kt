package app.photoar.standalone

import app.photoar.arview.LookupResult

/**
 * 重复上传之后该说什么、能做什么。
 *
 * ## 为什么这是个纯函数
 *
 * 分支比看起来多：这张照片入过库没有、它现在配了视频没有、用户这次挑了视频没有 ——
 * 三个布尔八种组合，而其中有几种的正确说法完全不同（「没什么要改的」 vs 「要不要换成
 * 刚传的这段」）。写在 Composable 里的唯一验证方式是把八种情况在手机上各走一遍，而
 * 其中「照片入过库 + 已有视频 + 这次也挑了视频」那一种正是最需要说清的。
 *
 * ## 基数不对称是这里的核心
 *
 * **一张照片只能配一段视频，但一段视频可以被多张照片配。** 所以：
 *
 * - 重复的**照片**是一个真冲突：库里那一张已经占了这个参考图，只能去改它（换视频）。
 * - 重复的**视频**根本不是冲突：直接配给新照片就行，原来用它的那些照片一点不受影响。
 *
 * 这个不对称必须体现在文案里，否则用户会以为「视频重复」也需要处理。
 */
object DuplicatePlan {

    /** 界面要展示的东西。 */
    data class Outcome(
        /** 主消息。一定非空。 */
        val message: String,
        /** 能做的那件事；null = 只有信息，没有可做的动作。 */
        val action: Action?,
    )

    sealed interface Action {
        /**
         * 把库里那张照片的视频换成这次刚传的这段。
         *
         * 是「换」而不是「加」，因为一张照片只能配一段视频。[confirm] 里要把这一点
         * 说清 —— 用户以为是「加」的话，会奇怪原来那段去哪了。
         */
        data class ReplaceVideo(
            val photoId: String,
            val videoPath: String,
            val confirm: String,
        ) : Action

        /** 那张照片还没配视频，这次挑的正好可以配上。语义上是「补」，不是「换」。 */
        data class AttachVideo(
            val photoId: String,
            val videoPath: String,
            val confirm: String,
        ) : Action
    }

    /**
     * @param lookup 服务端对这张**照片**路径的反查结果。
     * @param pickedVideoPath 这次用户挑并传上去的视频在服务端的路径；null = 这次没挑视频。
     */
    fun of(lookup: LookupResult, pickedVideoPath: String?): Outcome {
        val photo = lookup.photo
            ?: // 服务端说入过库，但反查说这个文件不是任何照片的参考图。两者不一致
              // （多半是刚被另一个人删了）。如实说出来，别编一个原因。
            return Outcome(
                message = "服务端说这张照片已经入库了，但查不到是哪一张 —— " +
                    "可能刚被别人删掉了。刷新一下再传一次。",
                action = null,
            )

        val title = photo.title?.takeIf { it.isNotBlank() } ?: "(无标题)"
        val head = "这张照片已经在库里了：「$title」"
        // 绑到局部变量：`photo.videoPath` 是别的模块（:arview）里的 public 属性，
        // Kotlin 不给跨模块的智能转换。
        val current: String? = photo.videoPath

        if (pickedVideoPath == null) {
            // 这次只传了照片。没有可做的动作 —— 说清现状就够了。
            val tail = current?.let { "，配的视频是 ${shortName(it)}" }
                ?: "，而它还**没有**配视频。想配就在下面的历史里点「配视频」"
            return Outcome(message = head + tail + "。", action = null)
        }

        if (current == null) {
            return Outcome(
                message = "$head，而它还没有配视频。要把刚传的这段配给它吗？",
                action = Action.AttachVideo(
                    photoId = photo.photoId,
                    videoPath = pickedVideoPath,
                    confirm = "把 ${shortName(pickedVideoPath)} 配给「$title」。" +
                        "它原来没有视频，所以这是补上，不会覆盖任何东西。",
                ),
            )
        }

        if (samePath(current, pickedVideoPath)) {
            // 同一张照片、同一段视频再传一遍。什么都不用做 —— 这是「我忘了传过没有」
            // 的典型情形，而报一个错会让人以为出了问题。
            return Outcome(
                message = "$head，而且配的就是刚传的这段视频（${shortName(pickedVideoPath)}）。" +
                    "已经是你要的样子了，不用再做什么。",
                action = null,
            )
        }

        return Outcome(
            message = "$head，它现在配的是 ${shortName(current)}。" +
                "要换成刚传的这段吗？",
            action = Action.ReplaceVideo(
                photoId = photo.photoId,
                videoPath = pickedVideoPath,
                confirm = "把「$title」的视频从 ${shortName(current)} " +
                    "换成 ${shortName(pickedVideoPath)}。\n\n" +
                    "一张照片只能配一段视频，所以这是**替换** —— 原来那段不再和它关联。" +
                    "视频文件本身不删；别的照片如果也在用它，不受影响。",
            ),
        )
    }

    /**
     * 路径的最后一段。界面上要显示的是「哪个文件」，而完整路径
     * （`/media/videos/2026/婚礼/迎宾.mp4`）会把一行文案撑得没法读。
     */
    internal fun shortName(path: String): String =
        path.trimEnd('/').substringAfterLast('/').ifEmpty { path }

    /**
     * 两条路径是不是同一个文件。
     *
     * 只做字符串比较（去掉尾随斜杠）。**不**尝试解析符号链接或者大小写折叠：那需要
     * 文件系统，而这是客户端；而且服务端给回来的路径已经是 `roots.resolve` 之后的
     * 规范形式，所以同一个文件在这里一定是同一个字符串。
     */
    internal fun samePath(a: String, b: String): Boolean =
        a.trimEnd('/') == b.trimEnd('/')
}

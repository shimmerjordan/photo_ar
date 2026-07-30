package app.photoar.standalone

import app.photoar.arview.cache.CacheSpec

/**
 * 离线缓存的两个可调项（§5.8）。
 *
 * 和 [Fmt] 一样**不许出现 android.\***：这里唯一容易出错的地方是「存进
 * SharedPreferences 的值下次读出来不合法」——用户上次选了 512MB，某次升级把选项
 * 改了，读出来的 512 不在列表里；或者 prefs 被写进了 0。[CacheSpec] 的构造带
 * `require`，0 会直接抛，而那是在 Application 起来的路径上，表现为「一升级就
 * 闪退」。所以钳位这件事必须有单测。
 */
object CacheSettings {

    /** 缓存多少张的可选值。默认 200 —— spec §11.3 写的就是「最近 200 张」。 */
    val PHOTO_OPTIONS = listOf(50, 100, 200, 500)

    /**
     * 视频预算的可选值（MB）。默认 2048。
     *
     * **默认值跟着服务端播放规格走**：2026-07-30 规格从 15s/720p/1.5Mbps 提到
     * 30s/1080p/4Mbps，一条视频的上限从约 2.8MB 变成约 15MB（实测最坏 14.9MB）。
     * 原来的 512MB 默认在旧规格下够放一百多条，在新规格下只够 34 条 —— 而照片
     * 默认缓存 200 张，于是「照片都在本地、视频却一大半要现拉」，表现为随机
     * 某些照片扫出来要等网络。2048MB 够放约 136 条，和 200 张照片大致配套。
     *
     * 小档一个都不删（只在尾部加 4096）：[nearest] 会把不在列表里的旧值吸到最近
     * 一档，删掉 256 的话老用户选的 256 会被吸到 128（距离 128 < 512 的 256），
     * 等于替他把预算砍半，而且不通知。
     */
    val VIDEO_MB_OPTIONS = listOf(128, 256, 512, 1024, 2048, 4096)

    const val DEFAULT_PHOTOS = 200
    const val DEFAULT_VIDEO_MB = 2048

    /**
     * 存下来的两个数 → [CacheSpec]。
     *
     * 不在列表里的值**不报错，取最近的一档**：这两个数只影响缓存多少，猜错的代价
     * 是多下或少下几十兆，而为它弹一个「配置损坏」对话框是纯噪声。
     */
    fun spec(photos: Int, videoMb: Int): CacheSpec = CacheSpec(
        maxPhotos = nearest(photos, PHOTO_OPTIONS),
        maxVideoBytes = nearest(videoMb, VIDEO_MB_OPTIONS) * 1024L * 1024L,
    )

    /** 界面上要点亮哪一档。同样取最近的一档，所以总有一个是亮的。 */
    fun selectedPhotos(photos: Int): Int = nearest(photos, PHOTO_OPTIONS)

    fun selectedVideoMb(videoMb: Int): Int = nearest(videoMb, VIDEO_MB_OPTIONS)

    private fun nearest(v: Int, options: List<Int>): Int =
        options.minByOrNull { kotlin.math.abs(it - v) } ?: options.first()
}

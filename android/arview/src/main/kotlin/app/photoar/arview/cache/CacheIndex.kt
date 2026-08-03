package app.photoar.arview.cache

import app.photoar.arview.ApiParseException
import app.photoar.arview.Hit
import app.photoar.arview.MediaInfo
import app.photoar.arview.PhotoSummary
import org.json.JSONArray
import org.json.JSONObject

/**
 * 端侧缓存索引（§11.3 / Phase 4「常扫照片离线可用」）。
 *
 * 和 `Api.kt` / `Catalog.kt` 一样，**这个文件里不许出现 android.\***：缓存该留谁、
 * 该淘汰谁、索引怎么落盘，全是断网时才生效的逻辑 —— 真机上根本不好造这个场景，
 * 只有 JVM 单测盯得住。
 *
 * ## 离线识别没有用 ORB
 *
 * spec §11.3 原话是「先查本地缓存索引（最近 200 张的 ORB 描述子，约 2MB）」。
 * 实做时改了：端上不做 ORB 匹配，改成把这 200 张喂给 **ARCore 自己的多图库**
 * （`addImage` + `serialize`），让 ARCore 在本地认。理由：
 *
 * - ORB 那条路要在端上背 OpenCV（每 ABI 十几 MB 的 .so），还得把服务端
 *   `recognizer.py` 的两阶段管线用 Kotlin 重写一遍，而它的正确性只能靠真实照片验，
 *   没法单测 —— 这跟本项目其它部分的取舍完全相反。
 * - ARCore 本来就在做端侧图像识别，而且是**每帧连续**做，不是 400ms 抽一帧发出去。
 *   「离线秒识别」用它是字面意义上的成立。
 * - Phase 2 已经确认 `AugmentedImage.name` 就是 photoId，所以 ARCore 认出来的
 *   东西直接就是命中结果，不需要另一套 id 映射。
 * - 物理宽度照样能传（`addImage(name, bitmap, widthM)`），§11.7 的红利不丢。
 *
 * 代价写清楚：端上 `addImage` 用的是 640px 缩略图，特征比服务端
 * `arcoreimg build-db` 拿原图建的少，跟踪质量会差一些 —— 就是 Phase 2 那条
 * `IMGDB_FALLBACK` 降级路径。所以离线命中是「质量降一档，但完全不用网络」。
 */

/** 索引格式版本。对不上就整份丢掉重建（见 [CacheIndexCodec.parse]）。 */
const val CACHE_INDEX_VERSION = 1

/**
 * 本地命中时 [Hit.inliers] 填的值。
 *
 * 用 -1 而不是 0：0 会被读成「内点数为零却命中了」，那是 bug 的样子。-1 是
 * 「这条路没有内点数这个概念」—— ARCore 认的，不是单应性校验认的。
 */
const val LOCAL_HIT_INLIERS = -1

/**
 * 缓存里的一张照片。
 *
 * @param videoBytes 0 表示视频没缓存（可能是没关联视频，也可能是被 LRU 淘汰了，
 *   两者由 [hasServerVideo] 区分）。
 * @param lastSeenAt 本地最后一次扫到它的时间。**这是「最近 200 张」的排序键**，
 *   不是入库时间 —— 出口条件说的是「常扫照片离线可用」。
 * @param targetRejected ARCore 的 `addImage` 拒过这张（缩略图特征不够）。记下来
 *   不再重试：每次重建库都花几十毫秒去撞同一个拒绝，200 张就是几秒。
 */
data class CachedPhoto(
    val photoId: String,
    val title: String?,
    val printWidthM: Float,
    val refAspect: Float?,
    val refStale: Boolean,
    val hasServerVideo: Boolean,
    val thumbBytes: Long,
    val videoBytes: Long,
    val videoDurationMs: Long?,
    val createdAt: Long,
    val lastSeenAt: Long,
    val targetRejected: Boolean,
) {
    val videoCached: Boolean get() = videoBytes > 0

    val bytes: Long get() = thumbBytes + videoBytes

    /** 缩略图在不在。没有缩略图这条索引对离线识别毫无用处（建不进 ARCore 库）。 */
    val usableAsTarget: Boolean get() = thumbBytes > 0 && !targetRejected

    /**
     * 本地命中转成 [Hit]，好让状态机的后半段一字不改地复用。
     *
     * `refStale` 照样带上：参考图变过这件事离线时同样要提示（§13），缓存里的
     * 特征只会比服务端的更旧。
     */
    fun toHit(): Hit = Hit(
        photoId = photoId,
        inliers = LOCAL_HIT_INLIERS,
        printWidthM = printWidthM,
        refAspect = refAspect,
        imgdbUrl = "/v1/photo/$photoId/imgdb",
        refThumbUrl = "/v1/photo/$photoId/thumb",
        mediaUrl = "/v1/photo/$photoId/media",
        refStale = refStale,
        latencyMs = 0,
    )

    /**
     * 服务端那边变了没有。
     *
     * `/v1/photos` **不返回 `updatedAt`**（只有 `/v1/photo/{id}` 有），所以判定用
     * 列表真给的三个字段。它们恰好就是「需要重下字节」的全部情况：
     *
     * - `printWidthM` 变了 → 打印宽度改过，这个值要传给 `addImage`，必须跟着改
     * - `refStale` 变了 → 参考图文件动过（服务端 mtime+sha256 校验的结论）
     * - `hasVideo` 变了 → 换了 / 补了 / 撤了视频，缓存里那条已经不是它了
     *
     * `title` 变了不在其中：改个名不用重下任何字节，直接覆盖元数据即可。
     */
    fun changedOnServer(p: PhotoSummary): Boolean =
        printWidthM != p.printWidthM || refStale != p.refStale || hasServerVideo != p.hasVideo

    /** 服务端元数据覆盖到本地条目上，已下的字节数与 [lastSeenAt] 保留。 */
    fun withServerMeta(p: PhotoSummary): CachedPhoto = copy(
        title = p.title,
        printWidthM = p.printWidthM,
        refAspect = p.refAspect,
        refStale = p.refStale,
        hasServerVideo = p.hasVideo,
        createdAt = p.createdAt,
    )

    /**
     * 重下完缩略图之后的条目。
     *
     * [targetRejected] 在这里**清掉**：走到这一步说明 [changedOnServer] 为真，
     * 参考图可能换了一张，「ARCore 嫌它特征不够」这个结论是对旧图下的，作废。
     * 不清的话换一张特征更好的图上去也永远进不了本地库。
     */
    fun refreshedFrom(p: PhotoSummary, thumbBytes: Long): CachedPhoto =
        withServerMeta(p).copy(thumbBytes = thumbBytes, targetRejected = false)

    companion object {
        /** 服务端列表项 → 新的缓存条目（还没下任何字节）。 */
        fun seed(p: PhotoSummary): CachedPhoto = CachedPhoto(
            photoId = p.photoId,
            title = p.title,
            printWidthM = p.printWidthM,
            refAspect = p.refAspect,
            refStale = p.refStale,
            hasServerVideo = p.hasVideo,
            thumbBytes = 0L,
            videoBytes = 0L,
            videoDurationMs = null,
            createdAt = p.createdAt,
            // 0 = 还没扫到过。CachePlan 里排序时它自然排在扫过的后面。
            lastSeenAt = 0L,
            targetRejected = false,
        )
    }
}

/**
 * 一次本地命中要用的视频源。
 *
 * `absolute = true` 是关键：[MediaInfo.resolvedUrl] 见到 absolute 就不套
 * mediaEndpoint 前缀，于是 `file://` 能原样交给 ExoPlayer。`supportsRange = true`
 * 也是实话 —— 本地文件随便 seek。
 */
fun localMedia(fileUrl: String, bytes: Long, durationMs: Long?): MediaInfo = MediaInfo(
    url = fileUrl,
    via = "cache",
    absolute = true,
    supportsRange = true,
    bytes = bytes,
    durationMs = durationMs,
    missing = false,
    nasPath = null,
    reason = null,
)

/**
 * 缓存的占用统计，给「缓存管理」那一页显示。
 *
 * @param targetBytes 端上现建那份库（`local.imgdb`）。
 * @param serverTargetBytes 服务端预建那份库（`targets.imgdb`）。两份分开列而不是加起来：
 *   稳态下只有一份在，两个数同时非零说明退回过端上现建，而那件事用户该看得见。
 */
data class CacheStats(
    val photos: Int,
    val withThumb: Int,
    val withVideo: Int,
    val thumbBytes: Long,
    val videoBytes: Long,
    val targetBytes: Long,
    val serverTargetBytes: Long,
    val rejected: Int,
) {
    val totalBytes: Long get() = thumbBytes + videoBytes + targetBytes + serverTargetBytes

    companion object {
        fun of(
            entries: Collection<CachedPhoto>,
            targetBytes: Long,
            serverTargetBytes: Long = 0L,
        ): CacheStats = CacheStats(
            photos = entries.size,
            withThumb = entries.count { it.thumbBytes > 0 },
            withVideo = entries.count { it.videoCached },
            thumbBytes = entries.sumOf { it.thumbBytes },
            videoBytes = entries.sumOf { it.videoBytes },
            targetBytes = targetBytes,
            serverTargetBytes = serverTargetBytes,
            rejected = entries.count { it.targetRejected },
        )
    }
}

/**
 * 索引的 JSON 编解码。
 *
 * 版本对不上就**整份丢掉**，不做迁移：缓存重建的代价是重下几百个缩略图（在家
 * 走局域网几秒钟），而为一个纯派生数据写迁移代码是净负债。
 */
object CacheIndexCodec {

    fun encode(entries: Collection<CachedPhoto>): String {
        val arr = JSONArray()
        entries.forEach { e ->
            arr.put(
                JSONObject().apply {
                    put("photoId", e.photoId)
                    e.title?.let { put("title", it) }
                    put("printWidthM", e.printWidthM.toDouble())
                    e.refAspect?.let { put("refAspect", it.toDouble()) }
                    put("refStale", e.refStale)
                    put("hasServerVideo", e.hasServerVideo)
                    put("thumbBytes", e.thumbBytes)
                    put("videoBytes", e.videoBytes)
                    e.videoDurationMs?.let { put("videoDurationMs", it) }
                    put("createdAt", e.createdAt)
                    put("lastSeenAt", e.lastSeenAt)
                    put("targetRejected", e.targetRejected)
                },
            )
        }
        return JSONObject().apply {
            put("version", CACHE_INDEX_VERSION)
            put("photos", arr)
        }.toString()
    }

    /**
     * @throws ApiParseException 不是 JSON、版本对不上、或者没有 photos 数组。
     *   调用方（[PhotoCache]）把它当成「没有缓存」处理，不是崩。
     */
    fun parse(json: String): List<CachedPhoto> {
        val o = try {
            JSONObject(json)
        } catch (e: Exception) {
            throw ApiParseException("缓存索引不是 JSON：${json.take(80)}")
        }
        val v = o.optInt("version", -1)
        if (v != CACHE_INDEX_VERSION) {
            throw ApiParseException("缓存索引版本 $v，当前是 $CACHE_INDEX_VERSION，丢弃重建")
        }
        val arr = o.optJSONArray("photos") ?: throw ApiParseException("缓存索引里没有 photos")
        val out = ArrayList<CachedPhoto>(arr.length())
        for (i in 0 until arr.length()) {
            val e = arr.optJSONObject(i) ?: continue
            val id = str(e, "photoId") ?: continue // 没 id 的条目对不上任何文件，跳过
            // printWidthM 不可用 → 记 0 = 未知，条目**保留**。
            //
            // 原来是丢掉这条，理由是「它会被原样传给 addImage，宽度错了只会让视频一直
            // 飘」。宽度未知现在是受支持的状态：不传给 addImage，让 ARCore 自己量
            // （见 `ar.LocalTargetDb` 与 `Geometry.quadSize`）。而丢掉这条的代价是这张
            // 照片进不了端侧库，离线命中对它永久失效。
            val width = e.optDouble("printWidthM", 0.0).toFloat()
                .takeIf { it.isFinite() && it > 0f } ?: 0f
            out.add(
                CachedPhoto(
                    photoId = id,
                    title = str(e, "title"),
                    printWidthM = width,
                    refAspect = e.optDouble("refAspect", Double.NaN).toFloat()
                        .takeIf { it.isFinite() && it > 0f },
                    refStale = e.optBoolean("refStale", false),
                    hasServerVideo = e.optBoolean("hasServerVideo", false),
                    thumbBytes = e.optLong("thumbBytes", 0L).coerceAtLeast(0L),
                    videoBytes = e.optLong("videoBytes", 0L).coerceAtLeast(0L),
                    videoDurationMs = if (e.isNull("videoDurationMs")) {
                        null
                    } else {
                        e.optLong("videoDurationMs")
                    },
                    createdAt = e.optLong("createdAt", 0L),
                    lastSeenAt = e.optLong("lastSeenAt", 0L),
                    targetRejected = e.optBoolean("targetRejected", false),
                ),
            )
        }
        return out
    }

    /** 同 [app.photoar.arview.ApiParse]：`optString(name, null)` 在两个 org.json 实现上行为不同。 */
    private fun str(o: JSONObject, name: String): String? =
        if (o.isNull(name)) null else o.optString(name, "").takeIf { it.isNotEmpty() }
}

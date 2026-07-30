package app.photoar.arview

import org.json.JSONArray
import org.json.JSONObject

/**
 * 外壳侧要用的那部分 §7 契约：照片列表 / 详情 / NAS 浏览 / 历史 / 入库。
 *
 * 与 [Api] 一样，这个文件里不许出现 android.* —— 解析逻辑要能在 JVM 单测里覆盖。
 * 扫描路径用的类型留在 `Api.kt`，两边分开是因为它们的生命周期不同：扫描那几个
 * 每 400ms 走一遍，这几个只在界面上翻页时走。
 */

/** `GET /v1/photos` 里的一项。 */
data class PhotoSummary(
    val photoId: String,
    val title: String?,
    val printWidthM: Float,
    val qualityScore: Int,
    val refAspect: Float?,
    val refThumbUrl: String,
    val hasVideo: Boolean,
    val refStale: Boolean,
    val createdAt: Long,
)

/** `GET /v1/photo/{id}`。比列表多了 NAS 路径与完整性状态。 */
data class PhotoDetail(
    val photoId: String,
    val title: String?,
    val printWidthM: Float,
    val qualityScore: Int,
    val selfScore: Int,
    val refAspect: Float?,
    val refPath: String?,
    val refMissing: Boolean,
    val refStale: Boolean,
    val videoPath: String?,
    /** 没关联视频时是 null，而不是 false —— 「没有」和「丢了」要能分开。 */
    val videoMissing: Boolean?,
    val imgdbBytes: Long,
    val createdAt: Long,
    val updatedAt: Long,
) {
    val hasVideo: Boolean get() = videoPath != null
}

/** NAS 上的一个条目。[path] 由客户端补齐，见 [ApiParse.fsList]。 */
data class FsEntry(
    val name: String,
    val path: String,
    val isDir: Boolean,
    /** `"image"` / `"video"` / null（不认识的类型）。目录恒为 null。 */
    val kind: String?,
    val bytes: Long,
    val mtime: Long,
    /** 白名单根目录，不能再往上翻。 */
    val isRoot: Boolean,
) {
    val isImage: Boolean get() = kind == "image"
    val isVideo: Boolean get() = kind == "video"
}

/** `GET /v1/fs/list`。[path] 为 null 表示这是白名单根目录列表。 */
data class FsListing(
    val path: String?,
    val parent: String?,
    val entries: List<FsEntry>,
) {
    val atRoots: Boolean get() = path == null
}

/** `GET /v1/history` 里的一条。未命中的记录 [photoId] 为 null。 */
data class HistoryEntry(
    val ts: Long,
    val photoId: String?,
    val title: String?,
    val refThumbUrl: String?,
    val inliers: Int,
    val latencyMs: Int,
    val via: String?,
) {
    val matched: Boolean get() = photoId != null
}

/** `POST /v1/photo` 成功（201）。质量分不达标时服务端返回 4xx，走异常路径。 */
data class CreateResult(
    val photoId: String,
    val qualityScore: Int,
    val selfScore: Int,
    val imgdbBytes: Long,
    val printWidthM: Float,
    val transcoded: Boolean,
    val elapsedMs: Long,
    val libraryPhotos: Int,
)

/** `POST /v1/photo/{id}/video`。 */
data class AttachResult(
    val photoId: String,
    val videoAssetId: String?,
    val playableAssetId: String?,
    val transcoded: Boolean,
)

/** 外壳侧的解析。命名沿用 [ApiParse]，两者合起来才是完整的 §7 客户端。 */
object CatalogParse {

    fun photos(json: String): List<PhotoSummary> =
        array(obj(json), "photos").mapObjects { o ->
            PhotoSummary(
                photoId = str(o, "photoId") ?: throw ApiParseException("photos 里有一项没有 photoId"),
                title = str(o, "title"),
                printWidthM = o.optDouble("printWidthM", 0.0).toFloat(),
                qualityScore = o.optInt("qualityScore", 0),
                refAspect = aspect(o),
                refThumbUrl = str(o, "refThumbUrl")
                    ?: "/v1/photo/${str(o, "photoId")}/thumb",
                hasVideo = o.optBoolean("hasVideo", false),
                refStale = o.optBoolean("refStale", false),
                createdAt = o.optLong("createdAt", 0L),
            )
        }

    fun photoDetail(json: String): PhotoDetail {
        val o = obj(json)
        return PhotoDetail(
            photoId = str(o, "photoId") ?: throw ApiParseException("详情里没有 photoId"),
            title = str(o, "title"),
            printWidthM = o.optDouble("printWidthM", 0.0).toFloat(),
            qualityScore = o.optInt("qualityScore", 0),
            selfScore = o.optInt("selfScore", 0),
            refAspect = aspect(o),
            refPath = str(o, "refPath"),
            refMissing = o.optBoolean("refMissing", false),
            refStale = o.optBoolean("refStale", false),
            videoPath = str(o, "videoPath"),
            // 服务端在没有视频时给 null，有视频才给 true/false
            videoMissing = if (o.isNull("videoMissing")) null else o.optBoolean("videoMissing"),
            imgdbBytes = o.optLong("imgdbBytes", 0L),
            createdAt = o.optLong("createdAt", 0L),
            updatedAt = o.optLong("updatedAt", 0L),
        )
    }

    /**
     * 目录列表。
     *
     * 服务端对**子条目只给 name 不给 path**（根目录列表那次才给 path + isRoot），
     * 所以完整路径必须在这里拼出来 —— 否则每个用到条目的地方都要自己拼一遍，
     * 迟早有一处忘了。
     */
    fun fsList(json: String): FsListing {
        val o = obj(json)
        val dir = str(o, "path")
        return FsListing(
            path = dir,
            parent = str(o, "parent"),
            entries = array(o, "entries").mapObjects { e ->
                val name = str(e, "name") ?: ""
                val isDir = e.optBoolean("isDir", false)
                FsEntry(
                    name = name,
                    path = str(e, "path") ?: joinPath(dir, name),
                    isDir = isDir,
                    kind = if (isDir) null else str(e, "kind"),
                    bytes = e.optLong("bytes", 0L),
                    mtime = e.optLong("mtime", 0L),
                    isRoot = e.optBoolean("isRoot", false),
                )
            },
        )
    }

    fun history(json: String): List<HistoryEntry> =
        array(obj(json), "entries").mapObjects { o ->
            HistoryEntry(
                ts = o.optLong("ts", 0L),
                photoId = str(o, "photoId"),
                title = str(o, "title"),
                refThumbUrl = str(o, "refThumbUrl"),
                inliers = o.optInt("inliers", 0),
                latencyMs = o.optInt("latencyMs", 0),
                via = str(o, "via"),
            )
        }

    fun createResult(json: String): CreateResult {
        val o = obj(json)
        return CreateResult(
            photoId = str(o, "photoId") ?: throw ApiParseException("入库响应里没有 photoId"),
            qualityScore = o.optInt("qualityScore", 0),
            selfScore = o.optInt("selfScore", 0),
            imgdbBytes = o.optLong("imgdbBytes", 0L),
            printWidthM = o.optDouble("printWidthM", 0.0).toFloat(),
            transcoded = o.optBoolean("transcoded", false),
            elapsedMs = o.optLong("elapsedMs", 0L),
            libraryPhotos = o.optInt("libraryPhotos", 0),
        )
    }

    fun attachResult(json: String): AttachResult {
        val o = obj(json)
        return AttachResult(
            photoId = str(o, "photoId") ?: throw ApiParseException("关联响应里没有 photoId"),
            videoAssetId = str(o, "videoAssetId"),
            playableAssetId = str(o, "playableAssetId"),
            transcoded = o.optBoolean("transcoded", false),
        )
    }

    /** `POST /v1/photo` 的请求体。 */
    fun createBody(refPath: String, videoPath: String?, printWidthMm: Double, title: String?): String =
        JSONObject().apply {
            put("refPath", refPath)
            // 只关联参考图、稍后再补视频是允许的（服务端 videoPath 可选）
            if (!videoPath.isNullOrBlank()) put("videoPath", videoPath)
            put("printWidthMm", printWidthMm)
            if (!title.isNullOrBlank()) put("title", title)
        }.toString()

    fun attachBody(videoPath: String): String =
        JSONObject().apply { put("videoPath", videoPath) }.toString()

    /**
     * 拼 NAS 路径。服务端只认白名单内的规范化绝对路径，所以这里不做任何
     * `..` 化简 —— 拼出来的东西照原样发过去，让服务端的 `safepath` 判定。
     * 客户端自己化简反而可能把一个本该被拒的路径洗白。
     */
    fun joinPath(dir: String?, name: String): String {
        if (dir.isNullOrEmpty()) return name
        return if (dir.endsWith("/")) dir + name else "$dir/$name"
    }

    private fun aspect(o: JSONObject): Float? =
        o.optDouble("refAspect", Double.NaN).toFloat().takeIf { it.isFinite() && it > 0f }

    private fun str(o: JSONObject, name: String): String? =
        if (o.isNull(name)) null else o.optString(name, "").takeIf { it.isNotEmpty() }

    private fun obj(json: String): JSONObject =
        try {
            JSONObject(json)
        } catch (e: Exception) {
            throw ApiParseException("响应不是 JSON：${json.take(120)}")
        }

    private fun array(o: JSONObject, name: String): JSONArray =
        o.optJSONArray(name) ?: throw ApiParseException("响应里没有 $name 数组")

    private inline fun <T> JSONArray.mapObjects(f: (JSONObject) -> T): List<T> {
        val out = ArrayList<T>(length())
        for (i in 0 until length()) {
            // 数组里混进非对象元素就跳过。服务端不会这么干，但一个坏元素不该让
            // 整个列表打不开。
            optJSONObject(i)?.let { out.add(f(it)) }
        }
        return out
    }
}

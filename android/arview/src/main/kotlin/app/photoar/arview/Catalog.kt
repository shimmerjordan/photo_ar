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

/**
 * `GET /v1/history` 里的一条。未命中的记录 [photoId] 为 null。
 *
 * @param reason 服务端的判定原因。`ok` / `weak`（内点数或 det 不过）/ `ambiguous`
 *   （第一名没比第二名高出 `RATIO` 倍，说明库里有近重复）/ `orphan` / `empty`。
 *   旧记录是 null（这一列是后加的）。
 *
 *   **这一列是这个界面存在的理由。** 一次真实排查里 941 条记录只有 `inliers`，
 *   其中 897 条内点数 160~229（门槛 40）却判了未命中 —— 光看内点数完全分不出挡住
 *   它们的是「取景不行」还是「库里有重复」，而那两件事一件要改扫描姿势、一件要清库。
 *   真相是后者。
 *
 * @param runnerUp 第二名的内点数。`ambiguous` 的判据就是它和 [inliers] 的比值，
 *   所以只有原因没有它仍然答不出「差多少、阈值该不该动」。旧记录是 null。
 */
data class HistoryEntry(
    val ts: Long,
    val photoId: String?,
    val title: String?,
    val refThumbUrl: String?,
    val inliers: Int,
    val latencyMs: Int,
    val via: String?,
    val reason: String? = null,
    val runnerUp: Int? = null,
) {
    val matched: Boolean get() = photoId != null

    /** 未命中且原因是「库里有近重复」。这一类的修法是删掉重复的那张，不是改取景。 */
    val ambiguous: Boolean get() = !matched && reason == "ambiguous"
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

/**
 * `GET /v1/admin/lookup` 的结果：一个 NAS 路径在库里是什么身份。
 *
 * 两个字段的**基数不一样**，而这正是界面上能给出什么动作的依据：
 *
 * - [photo] 最多一个 —— 这个文件是**某一张**照片的参考图（一张照片只有一个参考图）。
 * - [usedByPhotos] 是个列表 —— 这个文件是**这些**照片配的视频（一段视频可以被多张
 *   照片用，一段迎宾视频配给几十张是正常用法）。
 *
 * 所以：重复的**照片**只能去改那一张已有的（换它的视频）；重复的**视频**根本不是问题，
 * 直接配给新照片就行。
 */
data class LookupResult(
    val path: String,
    val exists: Boolean,
    val kind: String?,
    val photo: LookupPhoto?,
    val usedByPhotos: List<LookupPhotoRef>,
)

/** 这个文件作为参考图对应的那张照片。 */
data class LookupPhoto(
    val photoId: String,
    val title: String?,
    /** 它**现在**配的视频。null = 还没配。 */
    val videoPath: String?,
    val qualityScore: Int,
)

data class LookupPhotoRef(val photoId: String, val title: String?)

/**
 * `POST /v1/upload/check` 的结果：**上传之前**问出来的重复情况。
 *
 * 两条判断分开，因为下一步动作不同：
 *
 * - [nameTaken] / [sameContent]：落地目录里有同名文件。内容一样 → 直接复用那条路径，
 *   一个字节都不用传；不一样 → 得换个名字（[suggestedName]）。
 * - [knownContent] / [matches]：这份**内容**库里已经有了。这一条比按名字有用得多 ——
 *   相册第二次导出同一张照片，文件名可能变了，内容不会变。
 */
data class UploadCheck(
    val name: String,
    val nameTaken: Boolean,
    val sameContent: Boolean,
    val existingPath: String?,
    val suggestedName: String?,
    val knownContent: Boolean,
    val matches: List<AssetIdentity>,
) {
    /**
     * 这次上传能不能整个跳过。
     *
     * 同名同内容时那条路径就是我们要的；库里已经认识这份内容时，[matches] 第一条的
     * 路径同样可用。两种都不用再传一遍。
     */
    val reusablePath: String?
        get() = when {
            nameTaken && sameContent -> existingPath
            knownContent -> matches.firstOrNull()?.path
            else -> null
        }
}

/** 一份内容在库里的身份。与服务端 `_identity_of_asset` 一一对应。 */
data class AssetIdentity(
    val assetId: String,
    val path: String?,
    val kind: String?,
    val missing: Boolean,
    /** 它是这张照片的参考图。最多一个 —— 一张照片只有一个参考图。 */
    val photo: LookupPhoto?,
    /** 这些照片把它当视频用。是列表 —— 一段视频可以被多张照片配。 */
    val usedByPhotos: List<LookupPhotoRef>,
)

/** `POST /v1/photo/<id>/ref` 的结果。换参考图之后那几个跟着图变的数。 */
data class ReplaceRefResult(
    val photoId: String,
    val qualityScore: Int,
    val selfScore: Int,
    val imgdbBytes: Long,
    val elapsedMs: Long,
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
                reason = str(o, "reason"),
                // `optInt` 拿不到时给 0，而 0 和「这条记录没有这一列」是两件事：
                // 前者是真的没有第二名（库里只有一张），后者是旧记录。
                runnerUp = if (o.has("runnerUp") && !o.isNull("runnerUp")) {
                    o.optInt("runnerUp", 0)
                } else {
                    null
                },
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

    /**
     * `/v1/upload` 的响应 → 文件在服务端的绝对路径。
     *
     * 抛异常而不是回空串：这个路径的下一步用途是拿去 `/v1/photo` 入库，空串会变成
     * 一句「refPath 不能为空」，而真正的问题在上一步。
     */
    fun uploadedPath(json: String): String =
        str(obj(json), "path") ?: throw ApiParseException("上传响应里没有 path")

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

    fun lookup(json: String): LookupResult {
        val o = obj(json)
        val p = if (o.isNull("photo")) null else o.optJSONObject("photo")
        return LookupResult(
            path = str(o, "path") ?: "",
            exists = o.optBoolean("exists", false),
            kind = str(o, "kind"),
            photo = p?.let {
                LookupPhoto(
                    photoId = str(it, "photoId")
                        ?: throw ApiParseException("lookup 的 photo 里没有 photoId"),
                    title = str(it, "title"),
                    videoPath = str(it, "videoPath"),
                    qualityScore = it.optInt("qualityScore", 0),
                )
            },
            usedByPhotos = array(o, "usedByPhotos").mapObjects { r ->
                LookupPhotoRef(
                    photoId = str(r, "photoId") ?: "",
                    title = str(r, "title"),
                )
            }.filter { it.photoId.isNotEmpty() },
        )
    }

    fun uploadCheckBody(name: String, sha256: String): String =
        JSONObject().apply {
            put("name", name)
            put("sha256", sha256)
        }.toString()

    fun uploadCheck(json: String): UploadCheck {
        val o = obj(json)
        return UploadCheck(
            name = str(o, "name") ?: "",
            nameTaken = o.optBoolean("nameTaken", false),
            sameContent = o.optBoolean("sameContent", false),
            existingPath = str(o, "existingPath"),
            suggestedName = str(o, "suggestedName"),
            knownContent = o.optBoolean("knownContent", false),
            matches = array(o, "matches").mapObjects { assetIdentity(it) },
        )
    }

    private fun assetIdentity(o: JSONObject): AssetIdentity {
        val p = if (o.isNull("photo")) null else o.optJSONObject("photo")
        return AssetIdentity(
            assetId = str(o, "assetId") ?: "",
            path = str(o, "path"),
            kind = str(o, "kind"),
            missing = o.optBoolean("missing", false),
            photo = p?.let {
                LookupPhoto(
                    photoId = str(it, "photoId") ?: "",
                    title = str(it, "title"),
                    videoPath = str(it, "videoPath"),
                    qualityScore = it.optInt("qualityScore", 0),
                )
            }?.takeIf { it.photoId.isNotEmpty() },
            usedByPhotos = array(o, "usedByPhotos").mapObjects { r ->
                LookupPhotoRef(
                    photoId = str(r, "photoId") ?: "",
                    title = str(r, "title"),
                )
            }.filter { it.photoId.isNotEmpty() },
        )
    }

    fun refBody(refPath: String): String =
        JSONObject().apply { put("refPath", refPath) }.toString()

    fun replaceRefResult(json: String): ReplaceRefResult {
        val o = obj(json)
        return ReplaceRefResult(
            photoId = str(o, "photoId")
                ?: throw ApiParseException("换参考图的响应里没有 photoId"),
            qualityScore = o.optInt("qualityScore", 0),
            selfScore = o.optInt("selfScore", 0),
            imgdbBytes = o.optLong("imgdbBytes", 0L),
            elapsedMs = o.optLong("elapsedMs", 0L),
        )
    }

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

package app.photoar.standalone

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * 上传历史：这台手机传上去的每一组「照片 + 视频」。
 *
 * ## 为什么要本地存一份，而不是从服务端列表推
 *
 * 服务端有 `/v1/photos` 和 `/v1/admin/mapping`，能列出**全库**的照片和它们配的视频。但
 * 那回答不了这一页要回答的问题：「**我刚才**传的那几组现在怎么样了」。一场婚礼后台可能
 * 有几百张，而人上传完想确认的是自己这十几组。
 *
 * 而且本地这份还记着**原始文件名**（`photo.jpg` 在服务端叫什么完全取决于相册给的名字），
 * 那是人认出「哪条是哪条」的唯一线索 —— 服务端只有标题，而标题常常是空的。
 *
 * ## 只存指针，不存内容
 *
 * 一条记录里只有 photoId + 两个文件名 + 时间。缩略图、标题、当前配的视频都**现取**
 * （`/v1/photo/<id>`）—— 存下来就会和服务端不一致，而人分不清「界面显示的是旧的」和
 * 「服务端真的还是旧的」。photoId 是唯一需要记住的东西，因为它是问服务端的钥匙。
 *
 * 存 SharedPreferences 而不是 Room：一条记录几十字节，几百条也就几十 KB，而引一个 ORM
 * 要付 schema 迁移的代价。上限 [MAX] 条，满了丢最旧的。
 */
class UploadHistory(context: Context) {

    private val prefs =
        context.getSharedPreferences("photoar_upload_history", Context.MODE_PRIVATE)

    /** 一条记录。 */
    data class Entry(
        val photoId: String,
        /** 传上去时那张照片的原始文件名。人靠它认出这是哪一条。 */
        val photoName: String,
        /** 视频的原始文件名。空串 = 这一组当时没配视频。 */
        val videoName: String,
        val title: String,
        /** 创建时刻（epoch 毫秒）。 */
        val at: Long,
    ) {
        fun toJson(): JSONObject = JSONObject().apply {
            put("photoId", photoId)
            put("photoName", photoName)
            put("videoName", videoName)
            put("title", title)
            put("at", at)
        }

        companion object {
            fun fromJson(o: JSONObject): Entry = Entry(
                photoId = o.optString("photoId", ""),
                photoName = o.optString("photoName", ""),
                videoName = o.optString("videoName", ""),
                title = o.optString("title", ""),
                at = o.optLong("at", 0L),
            )
        }
    }

    /** 最新的在前。 */
    fun all(): List<Entry> = HistoryCodec.decode(prefs.getString(KEY, null))

    fun add(entry: Entry) {
        // 同一个 photoId 只留一条：重复上传同一张照片会拿到 409 already_ingested 并复用
        // 原来那个 photoId（见服务端 `ingest_photo`），那时该更新既有那条而不是并列两条
        // —— 两条指向同一张照片，在其中一条上换了视频，另一条显示的就是错的。
        val next = (listOf(entry) + all().filter { it.photoId != entry.photoId }).take(MAX)
        save(next)
    }

    /** 换过照片/视频之后更新那一条的文件名。找不到就什么都不做。 */
    fun update(photoId: String, photoName: String? = null, videoName: String? = null) {
        val next = all().map {
            if (it.photoId != photoId) {
                it
            } else {
                it.copy(
                    photoName = photoName ?: it.photoName,
                    videoName = videoName ?: it.videoName,
                )
            }
        }
        save(next)
    }

    fun remove(photoId: String) {
        save(all().filter { it.photoId != photoId })
    }

    fun clear() {
        prefs.edit().remove(KEY).apply()
    }

    private fun save(entries: List<Entry>) {
        prefs.edit().putString(KEY, HistoryCodec.encode(entries)).apply()
    }

    companion object {
        private const val KEY = "entries"

        /**
         * 最多留多少条。
         *
         * 200 条约 20 KB，SharedPreferences 是整份读写的，这个量级无所谓。定上限是为了
         * 别让它无界增长 —— 一场婚礼几十组，但这个 App 会被用好几场。
         */
        const val MAX = 200
    }
}

/**
 * 历史记录的编解码。纯函数，与 Android 无关，所以能测。
 *
 * 单独拆出来的理由和 [Auth] 一样：「坏掉的存档要怎么处理」这件事只有在这里能验。
 * 而它必须**永不抛异常** —— 存档解析失败把整个素材页变成一个崩溃，比丢掉历史糟得多，
 * 而历史本来就只是个便利视图（真相在服务端）。
 */
object HistoryCodec {

    fun encode(entries: List<UploadHistory.Entry>): String {
        val arr = JSONArray()
        entries.forEach { arr.put(it.toJson()) }
        return arr.toString()
    }

    /**
     * 解出历史。任何形式的坏数据都返回能解出来的那部分，绝不抛。
     *
     * 具体地：整份 JSON 坏了 → 空列表；某一条坏了或者没有 photoId → 跳过那一条留下其余。
     * photoId 是问服务端的钥匙，没有它这条记录什么也做不了，留着只会在界面上变成一个
     * 点了没反应的条目。
     */
    fun decode(raw: String?): List<UploadHistory.Entry> {
        if (raw.isNullOrBlank()) return emptyList()
        val arr = try {
            JSONArray(raw)
        } catch (e: Exception) {
            return emptyList()
        }
        val out = ArrayList<UploadHistory.Entry>(arr.length())
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val e = UploadHistory.Entry.fromJson(o)
            if (e.photoId.isBlank()) continue
            out.add(e)
        }
        return out
    }
}

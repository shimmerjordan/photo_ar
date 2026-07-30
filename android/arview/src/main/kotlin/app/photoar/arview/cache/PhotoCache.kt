package app.photoar.arview.cache

import java.io.File

/**
 * 缓存落盘（§11.3 / Phase 4）。
 *
 * 只用 `java.io.File`，**不碰 android.\***，所以 JVM 单测里给个临时目录就能跑真实
 * 读写 —— 字节记账错了、原子写没生效、索引写坏了读不回来，这些在真机上都表现为
 * 「过几天缓存莫名其妙不对了」，非常难查。
 *
 * ## 磁盘布局
 *
 * ```
 * <root>/offline/
 *   index.json          索引（[CacheIndexCodec]）
 *   thumbs/<id>.jpg     参考缩略图 —— 离线识别的地基
 *   videos/<id>.mp4     视频，LRU 淘汰的就是这些
 *   local.imgdb         ARCore 多图库（LocalTargetDb 写）
 * ```
 *
 * 视频一律叫 `.mp4`：服务端转码后的产物就是 H.264/AAC 的 mp4（§8.1），扩展名对
 * ExoPlayer 也只是提示。
 *
 * ## 字节记账为什么以磁盘为准
 *
 * 索引里的 `thumbBytes` / `videoBytes` 是**文件实际长度**，写完之后从 `File.length()`
 * 读回来填，不是拿下载 buffer 的 size 记的。原因：写盘可能只写成一半（配额满），
 * 那时候记账数字对、文件是残的，缓存管理页会显示「已缓存 480MB」而实际播不出来。
 */
class PhotoCache(root: File) {

    private val dir = File(root, "offline")
    private val thumbDir = File(dir, "thumbs")
    private val videoDir = File(dir, "videos")

    /** 索引文件。整份读写，200 条约 30KB —— 增量更新的复杂度换不到什么。 */
    private val indexFile = File(dir, "index.json")

    /** ARCore 多图库。文件名不带 hash：它总是「当前这 200 张」的函数。 */
    val targetDbFile: File = File(dir, "local.imgdb")

    /**
     * 内存里的一份索引，按 photoId 索引。
     *
     * 用 LinkedHashMap 保留插入顺序，好让 [entries] 的结果稳定 —— 顺序不稳会让
     * 「缓存了哪 200 张」在两次同步之间无理由地抖。
     */
    private val index = LinkedHashMap<String, CachedPhoto>()

    private var loaded = false

    // ---- 索引 ----

    /**
     * 从磁盘读索引。读不出来（第一次跑、版本变了、文件写坏了）就当成空缓存，
     * 不抛 —— 缓存是纯派生数据，读不回来的正确反应是重建，不是让 App 起不来。
     */
    @Synchronized
    fun load(): PhotoCache {
        if (loaded) return this
        loaded = true
        index.clear()
        val json = try {
            if (indexFile.isFile) indexFile.readText() else null
        } catch (e: Exception) {
            null
        }
        if (json != null) {
            try {
                CacheIndexCodec.parse(json).forEach { index[it.photoId] = it }
            } catch (e: Exception) {
                // 版本对不上或文件坏了：连带把散落的文件一起清掉，否则它们永远
                // 不会被任何索引条目引用，成了永久占空间的孤儿。
                index.clear()
                purgeOrphans()
            }
        }
        // 索引在、文件不在（用户清了应用数据但索引侥幸留着，或者写盘失败）：
        // 把字节数归零，让下一次 plan() 把它们排进重下。
        reconcile()
        return this
    }

    @Synchronized
    fun entries(): List<CachedPhoto> = index.values.toList()

    /**
     * 缩略图目录里最新的文件时间，0 表示一张都没有。
     *
     * [app.photoar.arview.ar.LocalTargetDb] 用它判断本地 ARCore 库过不过期。
     *
     * **为什么不是 `index.json` 的时间**：索引每次扫描结束都会因为 `lastSeenAt`
     * （见 [markSeen]）被重写一遍，而那件事跟「库里该放哪些图」毫无关系。用索引
     * 时间判过期的后果是**每次启动扫描都白重建一遍库** —— 200 张 `addImage` 要几秒，
     * 而且那几秒和识别请求挤在一起，表现为「刚举起手机那几秒怎么都认不出来」。
     *
     * 缩略图文件动过才是真的要重建，而「哪些缩略图」正是库的全部输入。
     */
    @Synchronized
    fun newestThumbMs(): Long =
        thumbDir.listFiles()?.filter { it.isFile }?.maxOfOrNull { it.lastModified() } ?: 0L

    @Synchronized
    fun byId(photoId: String): CachedPhoto? = index[photoId]

    @Synchronized
    fun stats(): CacheStats = CacheStats.of(
        index.values,
        targetBytes = if (targetDbFile.isFile) targetDbFile.length() else 0L,
    )

    /**
     * 索引写盘。tmp + rename，理由同 TargetLoader：进程在写一半被杀，读回来的
     * 是残缺 JSON —— 那会让整份缓存作废，几百个缩略图白下。
     */
    @Synchronized
    fun flush() {
        dir.mkdirs()
        val json = CacheIndexCodec.encode(index.values)
        val tmp = File(dir, "index.json.tmp")
        try {
            tmp.writeText(json)
            if (!tmp.renameTo(indexFile)) {
                tmp.delete()
                indexFile.writeText(json)
            }
        } catch (e: Exception) {
            tmp.delete()
            throw e
        }
    }

    // ---- 单条读写 ----

    fun thumbFile(photoId: String): File = File(thumbDir, "$photoId.jpg")

    fun videoFile(photoId: String): File = File(videoDir, "$photoId.mp4")

    /** 缓存里那条视频的 `file://` 地址，没缓存就 null。 */
    @Synchronized
    fun localVideoUrl(photoId: String): String? {
        val e = index[photoId] ?: return null
        if (!e.videoCached) return null
        val f = videoFile(photoId)
        // 记账说有、文件却没了：以磁盘为准，并且顺手把账改对
        if (!f.isFile || f.length() <= 0) {
            index[photoId] = e.copy(videoBytes = 0L, videoDurationMs = null)
            return null
        }
        return f.toURI().toString()
    }

    /**
     * 写缩略图，返回落盘后的实际字节数。
     *
     * @param refreshed 这是「服务端那边变过」触发的重下 —— 见
     *   [CachedPhoto.refreshedFrom]，被拒标记要清掉。
     */
    @Synchronized
    fun putThumb(entry: CachedPhoto, bytes: ByteArray, refreshed: Boolean = false): CachedPhoto {
        thumbDir.mkdirs()
        val f = thumbFile(entry.photoId)
        writeAtomic(f, bytes)
        val next = if (refreshed) {
            entry.copy(thumbBytes = f.length(), targetRejected = false)
        } else {
            entry.copy(thumbBytes = f.length())
        }
        index[entry.photoId] = next
        return next
    }

    @Synchronized
    fun putVideo(entry: CachedPhoto, bytes: ByteArray, durationMs: Long? = null): CachedPhoto {
        videoDir.mkdirs()
        val f = videoFile(entry.photoId)
        writeAtomic(f, bytes)
        val next = entry.copy(videoBytes = f.length(), videoDurationMs = durationMs ?: entry.videoDurationMs)
        index[entry.photoId] = next
        return next
    }

    /** 只删视频文件，索引条目留着。这个区分是整份缓存设计里最要紧的一条。 */
    @Synchronized
    fun dropVideo(photoId: String) {
        videoFile(photoId).delete()
        index[photoId]?.let { index[photoId] = it.copy(videoBytes = 0L, videoDurationMs = null) }
    }

    /** 整条删掉：文件 + 索引。 */
    @Synchronized
    fun dropPhoto(photoId: String) {
        thumbFile(photoId).delete()
        videoFile(photoId).delete()
        index.remove(photoId)
    }

    @Synchronized
    fun put(entry: CachedPhoto) {
        index[entry.photoId] = entry
    }

    /**
     * 记一次「刚扫到」。**这是「最近 200 张」的排序键**（见 [CachePlanner.rank]），
     * 所以命中时一定要调，不管是在线命中还是离线命中。
     *
     * 不在这里 [flush]：扫描时每帧都可能命中，写盘会掉帧。由调用方在扫描结束时
     * 统一落盘 —— 掉一次 lastSeenAt 的代价只是排序略旧一点。
     */
    @Synchronized
    fun markSeen(photoId: String, nowMs: Long): Boolean {
        val e = index[photoId] ?: return false
        index[photoId] = e.copy(lastSeenAt = nowMs)
        return true
    }

    /** ARCore 说这张的特征不够。记下来别再反复试（见 [CachedPhoto.targetRejected]）。 */
    @Synchronized
    fun markRejected(photoId: String) {
        index[photoId]?.let { index[photoId] = it.copy(targetRejected = true) }
    }

    // ---- 清理 ----

    /** 只清视频，缩略图和索引留着 —— 离线识别照样可用，只是认出来没视频放。 */
    @Synchronized
    fun clearVideos() {
        videoDir.listFiles()?.forEach { it.delete() }
        index.keys.toList().forEach { id ->
            index[id]?.let { index[id] = it.copy(videoBytes = 0L, videoDurationMs = null) }
        }
        flush()
    }

    /** 全清，含 ARCore 库。下一次同步会从零重建。 */
    @Synchronized
    fun clearAll() {
        dir.deleteRecursively()
        index.clear()
        dir.mkdirs()
    }

    // ---- 内部 ----

    /**
     * 索引与磁盘对账。以**磁盘**为准：文件没了就把字节数归零，让 plan() 排进重下。
     *
     * 反方向（文件在、索引没有）由 [purgeOrphans] 管。
     */
    private fun reconcile() {
        index.keys.toList().forEach { id ->
            val e = index[id] ?: return@forEach
            var next = e
            if (e.thumbBytes > 0) {
                val f = thumbFile(id)
                if (!f.isFile || f.length() <= 0) next = next.copy(thumbBytes = 0L)
                else if (f.length() != e.thumbBytes) next = next.copy(thumbBytes = f.length())
            }
            if (e.videoBytes > 0) {
                val f = videoFile(id)
                if (!f.isFile || f.length() <= 0) {
                    next = next.copy(videoBytes = 0L, videoDurationMs = null)
                } else if (f.length() != e.videoBytes) {
                    next = next.copy(videoBytes = f.length())
                }
            }
            if (next != e) index[id] = next
        }
        purgeOrphans()
    }

    /** 没有任何索引条目引用的文件。留着就是永久占空间的孤儿。 */
    private fun purgeOrphans() {
        listOf(thumbDir, videoDir).forEach { d ->
            d.listFiles()?.forEach { f ->
                val id = f.name.substringBeforeLast('.')
                if (id !in index) f.delete()
            }
        }
    }

    private fun writeAtomic(f: File, bytes: ByteArray) {
        val tmp = File(f.parentFile, f.name + ".tmp")
        try {
            tmp.writeBytes(bytes)
            if (!tmp.renameTo(f)) {
                tmp.delete()
                f.writeBytes(bytes)
            }
        } finally {
            if (tmp.exists()) tmp.delete()
        }
    }
}

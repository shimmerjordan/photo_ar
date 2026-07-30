package app.photoar.arview.cache

import app.photoar.arview.PhotoSummary

/**
 * 缓存该留谁、该淘汰谁 —— 纯函数，不碰磁盘也不碰网络（§11.3 / Phase 4）。
 *
 * 分成「算计划」和「执行计划」两步是刻意的：断网、缓存满、服务端删了照片、
 * 参考图变过，这几种情况全都在这里判，而它们在真机上都不好造。执行那半边
 * （[CacheSync]）只剩下按列表下载和删文件。
 */

/** 缓存预算。默认值来自 spec §15 的「最近 200 张」。 */
data class CacheSpec(
    /** 索引里最多留多少张。ARCore 单个库上限 1000，200 是 spec 定的。 */
    val maxPhotos: Int = 200,
    /**
     * 视频缓存的字节预算。§12 一条 15 秒 720p 约 1.5–3MB，512MB 够放 200 条还有余，
     * 而手机上 512MB 是个不会让人皱眉的数字。
     */
    val maxVideoBytes: Long = 512L * 1024 * 1024,
) {
    init {
        require(maxPhotos > 0) { "maxPhotos 必须为正" }
        require(maxVideoBytes >= 0) { "maxVideoBytes 不能为负" }
    }
}

/**
 * 一次同步要做的事。每个列表都是 photoId。
 *
 * @param addThumb 新条目 / 缩略图还没下 → 要下缩略图。
 * @param refreshThumb 服务端 `updatedAt` 变了 → 重下缩略图，并重建 ARCore 库。
 * @param addVideo 视频还没缓存、预算也放得下 → 要下视频。
 * @param dropVideo 只删视频文件，索引条目留着 —— 缩略图便宜且是离线识别的地基，
 *   视频才是占空间的那个。这个区分是整份计划里最要紧的一条。
 * @param dropPhoto 整条不要了：服务端删了它，或者被挤出最近 200 张。
 * @param rebuildTarget 要不要重建 ARCore 多图库。
 */
data class CachePlan(
    val addThumb: List<String> = emptyList(),
    val refreshThumb: List<String> = emptyList(),
    val addVideo: List<String> = emptyList(),
    val dropVideo: List<String> = emptyList(),
    val dropPhoto: List<String> = emptyList(),
    val rebuildTarget: Boolean = false,
) {
    val empty: Boolean
        get() = addThumb.isEmpty() && refreshThumb.isEmpty() && addVideo.isEmpty() &&
            dropVideo.isEmpty() && dropPhoto.isEmpty() && !rebuildTarget

    val downloads: Int get() = addThumb.size + refreshThumb.size + addVideo.size
}

object CachePlanner {

    /**
     * 排出「最近 200 张」。
     *
     * 排序键：**本地最后扫到的时间降序，没扫过的按服务端入库时间降序垫后**。
     *
     * 为什么不直接用入库时间：出口条件是「常扫照片离线可用」。刚入库的一批照片
     * 通常是刚打印出来准备送人的，而挂在墙上天天被扫的那几张可能是三年前入的库。
     * 按入库时间排会把后者挤出去，正好挤掉最该留的。
     *
     * 冷启动时所有 `lastSeenAt` 都是 0，这时入库时间就是唯一可用的信号，于是它
     * 自然成了种子顺序。
     */
    fun rank(server: List<PhotoSummary>, local: Map<String, CachedPhoto>): List<PhotoSummary> =
        server.sortedWith(
            compareByDescending<PhotoSummary> { local[it.photoId]?.lastSeenAt ?: 0L }
                .thenByDescending { it.createdAt }
                // photoId 兜底是为了让顺序**完全确定**：两张同一秒入库又都没扫过的
                // 照片，若顺序随环境变化，「缓存了哪 200 张」就会时不时抖一下。
                .thenBy { it.photoId },
        )

    /**
     * @param server `GET /v1/photos` 的结果。空列表意味着服务端一张都没有 ——
     *   那确实该把本地清空（照片被删干净了），所以不做「空就当没拉到」的特判：
     *   拉取失败会抛异常，根本走不到这里。
     */
    fun plan(
        server: List<PhotoSummary>,
        local: Collection<CachedPhoto>,
        spec: CacheSpec = CacheSpec(),
    ): CachePlan {
        val byId = local.associateBy { it.photoId }
        val keep = rank(server, byId).take(spec.maxPhotos)
        val keepIds = keep.mapTo(LinkedHashSet()) { it.photoId }

        val addThumb = ArrayList<String>()
        val refreshThumb = ArrayList<String>()
        var rebuild = false

        keep.forEach { p ->
            val cached = byId[p.photoId]
            when {
                cached == null -> {
                    addThumb.add(p.photoId)
                    rebuild = true
                }
                // 见 CachedPhoto.changedOnServer：/v1/photos 不给 updatedAt，
                // 判定靠 printWidthM / refStale / hasVideo 三个字段。
                cached.changedOnServer(p) -> {
                    refreshThumb.add(p.photoId)
                    // 被拒过的照片在参考图变了之后值得再试一次 —— 换了张图，
                    // 特征够不够是重新算的。
                    rebuild = true
                }
                cached.thumbBytes <= 0L && !cached.targetRejected -> {
                    // 上次下缩略图没成（断网、写盘失败），补下
                    addThumb.add(p.photoId)
                    rebuild = true
                }
                else -> Unit
            }
        }

        // 掉出前 200 或服务端已删除的，整条清掉
        val dropPhoto = local.map { it.photoId }.filter { it !in keepIds }
        if (dropPhoto.isNotEmpty()) rebuild = true

        val videos = planVideos(keep, byId, keepIds, refreshThumb.toSet(), spec)

        return CachePlan(
            addThumb = addThumb,
            refreshThumb = refreshThumb,
            addVideo = videos.add,
            dropVideo = videos.drop,
            dropPhoto = dropPhoto,
            rebuildTarget = rebuild,
        )
    }

    private class VideoPlan(val add: List<String>, val drop: List<String>)

    /**
     * 视频那一半：先扔掉已经作废的，再按 [CacheSpec.maxVideoBytes] 淘汰，最后看还能加谁。
     *
     * 已经缓存的比没缓存的优先 —— 一条已经在本地的视频不该为了给别人腾地方被删掉，
     * 除非它自己就是最旧的那个。
     */
    private fun planVideos(
        keep: List<PhotoSummary>,
        byId: Map<String, CachedPhoto>,
        keepIds: Set<String>,
        stale: Set<String>,
        spec: CacheSpec,
    ): VideoPlan {
        val dropVideo = LinkedHashSet<String>()

        // 掉出 keep 的那些视频由 dropPhoto 一起删掉整条，不在这里重复列。
        val cachedInKeep = byId.values.filter { it.videoCached && it.photoId in keepIds }

        // 服务端那边动过的（换了视频、撤了视频、参考图变了）：本地那份不再是它，
        // 先扔掉。不扔的话「换了视频」在缓存命中时会一直播旧的那条，而且因为
        // videoCached 为 true，永远不会去重下 —— 这是个不报错的错。
        cachedInKeep.filter { it.photoId in stale }.forEach { dropVideo.add(it.photoId) }

        var used = cachedInKeep.filter { it.photoId !in dropVideo }.sumOf { it.videoBytes }

        val evictable = cachedInKeep
            .filter { it.photoId !in dropVideo }
            .sortedWith(
                // 最旧的先淘汰。lastSeenAt 相同（比如都没扫过）时按 id，保证确定性。
                compareBy<CachedPhoto> { it.lastSeenAt }.thenBy { it.photoId },
            )
        var i = 0
        while (used > spec.maxVideoBytes && i < evictable.size) {
            val victim = evictable[i++]
            dropVideo.add(victim.photoId)
            used -= victim.videoBytes
        }
        // 被 LRU 淘汰掉的这一批不能立刻又下回来，否则每次同步都在删了又下。
        // 服务端那边动过的可以重下 —— 那正是重下的理由。
        val evicted = dropVideo.filter { it !in stale }.toSet()

        val addVideo = ArrayList<String>()
        // 按 keep 的顺序（= 最近扫到的在前）来加，预算用完就停。这样预算紧张时
        // 留下的是常扫的那几条，而不是碰巧先遍历到的。
        keep.forEach { p ->
            if (!p.hasVideo) return@forEach
            if (p.photoId in evicted) return@forEach
            val cached = byId[p.photoId]
            if (cached != null && cached.videoCached && p.photoId !in dropVideo) return@forEach
            // 大小未知（还没下过），按 §12 的上限 3MB 估。估小了会超预算一点，
            // 估大了会少缓存几条 —— 宁可少缓存，超预算是要占用户空间的。
            if (used + ESTIMATED_VIDEO_BYTES > spec.maxVideoBytes) return@forEach
            used += ESTIMATED_VIDEO_BYTES
            addVideo.add(p.photoId)
        }

        return VideoPlan(add = addVideo, drop = dropVideo.toList())
    }

    /** §12：720p / ≤1.5Mbps / ≤15 秒 → 约 1.5–3MB。取上限估。 */
    const val ESTIMATED_VIDEO_BYTES = 3L * 1024 * 1024
}

/**
 * 本地命中时视频从哪来。
 *
 * 缓存里有就一律用缓存，**即便此刻在线** —— 本地文件起播快、不吃流量，而且
 * 服务端的 `resolve` 每次都要现取直链（§10）。在线的唯一好处是拿到最新的那份，
 * 而视频换了会让 `updatedAt` 变，下一次同步会重下。
 */
enum class MediaSource {
    LOCAL_CACHE,
    NETWORK,

    /** 没缓存又没网。界面上要说清是「视频没缓存」，不是「视频坏了」。 */
    NONE,
}

fun chooseMediaSource(videoCached: Boolean, online: Boolean): MediaSource = when {
    videoCached -> MediaSource.LOCAL_CACHE
    online -> MediaSource.NETWORK
    else -> MediaSource.NONE
}

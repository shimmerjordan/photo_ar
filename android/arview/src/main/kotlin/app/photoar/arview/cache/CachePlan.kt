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
     * 视频缓存的字节预算。§12 一条 30 秒 1080p ≤4Mbps 约 15MB（实测最坏 14.9MB），
     * 2048MB 够放约 136 条 —— 和 [maxPhotos] 默认的 200 张大致配套。
     *
     * 这个数**必须跟着服务端播放规格改**：2026-07-30 规格提档后，原来的 512MB
     * 从「够放两百条还有余」变成只够 34 条。少了不会报错，表现是随机某些照片
     * 扫出来要等网络（视频被挤出去了，缩略图还在，所以照片认得出、画面出不来）。
     */
    val maxVideoBytes: Long = 2048L * 1024 * 1024,
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
        val estimate = estimateVideoBytes(
            cachedInKeep.filter { it.photoId !in dropVideo }, spec.maxVideoBytes,
        )
        // 按 keep 的顺序（= 最近扫到的在前）来加，预算用完就停。这样预算紧张时
        // 留下的是常扫的那几条，而不是碰巧先遍历到的。
        keep.forEach { p ->
            if (!p.hasVideo) return@forEach
            if (p.photoId in evicted) return@forEach
            val cached = byId[p.photoId]
            if (cached != null && cached.videoCached && p.photoId !in dropVideo) return@forEach
            if (used + estimate > spec.maxVideoBytes) return@forEach
            used += estimate
            addVideo.add(p.photoId)
        }

        return VideoPlan(add = addVideo, drop = dropVideo.toList())
    }

    /**
     * 还没下过的那些视频，一条按多少字节估。
     *
     * **优先用已缓存视频的实际平均值**，只有一条都没缓存过时才退回上限
     * [MAX_VIDEO_BYTES]。原因是两个方向都有真实代价，而固定值必须往一边偏：
     * - 估小了（旧代码写死 3MB，而 §12 改档后一条上限 16.2MiB，实测常见 5-10MiB）
     *   会一次排下 5 倍装不下的量，真下完就超预算，下次同步再被 LRU 淘汰掉一批 ——
     *   表现是「反复下了又删」，白吃流量。
     * - 估大了（一律按 16.2MiB）会少缓存一半：2048MB 预算只排 126 条，而实际
     *   放得下约 250 条，用户的预算白闲着一半，表现是「明明设了 2G，还是老要等网络」。
     *
     * 用实际平均值就两边都不偏，而且这个数**已经在手上**（[CachedPhoto.videoBytes]），
     * 不需要服务端多返回一个字段。首次同步（没有样本）时取上限，那是唯一该保守的
     * 时刻 —— 空缓存下超预算是直接多占用户几百 MB。
     *
     * 平均值偏小导致的超预算是**暂时的**：下一次同步开头的 LRU 会把 used 削回预算内。
     *
     * `min(上限, 预算)` 那一步不是保险，是**防死锁**：预算比一条上限还小时（预算是
     * 自由参数，界面上最小一档 128MB 撞不到，但代码里 10MB 是合法值），按上限估会
     * 一条都排不下 → 永远没有样本 → 永远按上限估。哪怕实际一条只有 1MB、放得下
     * 十条，也一条都不缓存，而且不报错。取 min 让第一轮至少下一条把样本拿到手，
     * 第二轮就收敛到实际大小，且从头到尾没有超过预算。
     *
     * 估值**永远不为 0**：0 会让任何预算看起来装得下无限条（预算判据
     * `used + estimate > maxVideoBytes` 在两边都是 0 时恒假），而
     * `maxVideoBytes = 0`（把视频缓存关掉）经过上面那个 min 正好会走到那里 ——
     * 表现是「关掉视频缓存，反而把每条视频都下下来」。
     */
    internal fun estimateVideoBytes(cached: List<CachedPhoto>, maxVideoBytes: Long): Long {
        val known = cached.filter { it.videoCached && it.videoBytes > 0 }
        if (known.isEmpty()) return minOf(MAX_VIDEO_BYTES, maxVideoBytes).coerceAtLeast(1L)
        return (known.sumOf { it.videoBytes } / known.size).coerceAtLeast(1L)
    }

    /**
     * §12 一条播放版的字节上限（= 服务端 `transcode.MAX_PLAYABLE_BYTES`）。
     *
     * 30 秒 × (4000k 视频 + 128k 音频) ÷ 8 × 1.1 余量。**这个数必须跟服务端一起改**：
     * 服务端不会发出超过它的播放版，所以它是唯一安全的「未知大小」上界。
     */
    const val MAX_VIDEO_BYTES = 17_028_000L
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

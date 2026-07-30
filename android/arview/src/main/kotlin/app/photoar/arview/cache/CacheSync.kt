package app.photoar.arview.cache

import app.photoar.arview.Clock
import app.photoar.arview.net.HttpFailure
import app.photoar.arview.NetErrorKind
import app.photoar.arview.PhotoSummary
import app.photoar.arview.net.PhotoArClient

/**
 * 执行一份 [CachePlan]：按列表下载、删文件、重建本地 ARCore 库（§11.3 / Phase 4）。
 *
 * 「算计划」和「执行计划」分开之后，这一层只剩下按列表办事 —— 但它仍有两件不显然的
 * 事，都在下面单独说明：**单条失败不能中断整轮**，以及 **401 必须立刻停**。
 *
 * 同样不碰 `android.*`：假一个 [PhotoArClient] 的 transport 就能在 JVM 里跑完整一轮
 * 同步，包括「下到第 3 条断网」这种真机上难造的情况。
 */
class CacheSync(
    private val client: PhotoArClient,
    private val cache: PhotoCache,
    private val clock: Clock,
    private val spec: CacheSpec = CacheSpec(),
    /**
     * 重建 ARCore 多图库。真机上是 `LocalTargetDb`，单测里给个假的。
     * 返回被 ARCore 拒掉的 photoId —— 它们会被记进索引，下次不再白试。
     */
    private val rebuildTargetDb: (List<CachedPhoto>) -> RebuildResult = { RebuildResult() },
) {

    /** @param rejected ARCore 嫌特征不够的那些。 */
    data class RebuildResult(
        val rejected: List<String> = emptyList(),
        val accepted: Int = 0,
        val failure: String? = null,
    )

    /**
     * 一轮同步的结果。
     *
     * @param stoppedBy 非 null 表示中途停了（401 或没网），这时候 [plan] 里剩下的
     *   条目下一轮会重来 —— 所以「部分完成」不需要额外记状态。
     */
    data class Result(
        val plan: CachePlan,
        val thumbsDownloaded: Int = 0,
        val videosDownloaded: Int = 0,
        val bytesDownloaded: Long = 0,
        val videosDropped: Int = 0,
        val photosDropped: Int = 0,
        val failed: List<String> = emptyList(),
        val rejected: Int = 0,
        val targetsInDb: Int = 0,
        val stoppedBy: String? = null,
        val elapsedMs: Long = 0,
    ) {
        val didWork: Boolean
            get() = thumbsDownloaded > 0 || videosDownloaded > 0 ||
                videosDropped > 0 || photosDropped > 0
    }

    /** 进度回调，给「缓存管理」页显示「12 / 47」。在调用线程上同步触发。 */
    fun interface Progress {
        fun onStep(done: Int, total: Int, what: String)
    }

    /**
     * 拉服务端列表 → 算计划 → 执行。
     *
     * 整个过程是**同步阻塞**的，由调用方放到后台线程。没有内建并发：视频一条 3MB，
     * 并发下载在家用千兆下没什么收益，而串行意味着「下到一半退出」的状态天然一致。
     *
     * @throws HttpFailure 拉列表就失败了（这一步没有「部分成功」可言）。
     */
    fun sync(progress: Progress? = null): Result {
        val started = clock.nowMs()
        cache.load()
        val server = client.photos()
        val plan = CachePlanner.plan(server, cache.entries(), spec)
        return execute(plan, server, started, progress)
    }

    private fun execute(
        plan: CachePlan,
        server: List<PhotoSummary>,
        startedMs: Long,
        progress: Progress?,
    ): Result {
        val byId = server.associateBy { it.photoId }
        var thumbs = 0
        var videos = 0
        var bytes = 0L
        val failed = ArrayList<String>()
        var stoppedBy: String? = null

        // 先删再下：预算是靠删腾出来的，顺序反了会在磁盘上短暂地超预算 ——
        // 在只剩几十兆的手机上，那一下就是写失败。
        plan.dropVideo.forEach { cache.dropVideo(it) }
        plan.dropPhoto.forEach { cache.dropPhoto(it) }

        val total = plan.downloads
        var done = 0

        // 缩略图排在视频前面：它们是离线识别的地基，而视频只影响「认出来之后
        // 有没有东西放」。中途断网时先保住能认。
        val thumbJobs = plan.addThumb.map { it to false } + plan.refreshThumb.map { it to true }
        for ((id, refreshed) in thumbJobs) {
            val summary = byId[id] ?: continue
            progress?.onStep(done, total, "缩略图 $id")
            // 已有条目就把服务端元数据覆盖上去（保住 lastSeenAt 这个排序键），
            // 没有就从服务端列表项起一条新的。
            val base = cache.byId(id)?.withServerMeta(summary) ?: CachedPhoto.seed(summary)
            try {
                val data = client.download(summary.refThumbUrl)
                if (data.isEmpty()) throw HttpFailure(NetErrorKind.BAD_RESPONSE, null, "缩略图是 0 字节")
                cache.putThumb(base, data, refreshed = refreshed)
                thumbs++
                bytes += data.size
            } catch (e: Exception) {
                // 单条失败不写坏索引，但元数据还是要更新 —— 否则下一轮 plan() 会
                // 因为「服务端变过」把它再排一次，而这次失败与元数据无关。
                // 字节数保持原样（可能是 0），所以它下一轮仍会被排进重下。
                cache.put(base)
                failed.add(id)
                stoppedBy = fatalReason(e)
                if (stoppedBy != null) break
            }
            done++
        }

        if (stoppedBy == null) {
            for (id in plan.addVideo) {
                val summary = byId[id] ?: continue
                progress?.onStep(done, total, "视频 $id")
                val entry = cache.byId(id) ?: CachedPhoto.seed(summary)
                try {
                    val info = client.media(entry.toHit())
                    val data = client.downloadMedia(info)
                    cache.putVideo(entry, data, info.durationMs)
                    videos++
                    bytes += data.size
                } catch (e: Exception) {
                    failed.add(id)
                    stoppedBy = fatalReason(e)
                    if (stoppedBy != null) break
                }
                done++
            }
        }

        // 库在最后重建，用**这一刻**索引里真有缩略图的那些 —— 上面失败的几条自然
        // 就不在里面，而不需要在失败分支里各自维护一份名单。
        var rejected = 0
        var accepted = 0
        if (plan.rebuildTarget || thumbs > 0) {
            val usable = cache.entries().filter { it.usableAsTarget }
            val r = rebuildTargetDb(usable)
            r.rejected.forEach { cache.markRejected(it) }
            rejected = r.rejected.size
            accepted = r.accepted
        } else {
            accepted = cache.entries().count { it.usableAsTarget }
        }

        cache.flush()

        return Result(
            plan = plan,
            thumbsDownloaded = thumbs,
            videosDownloaded = videos,
            bytesDownloaded = bytes,
            videosDropped = plan.dropVideo.size,
            photosDropped = plan.dropPhoto.size,
            failed = failed,
            rejected = rejected,
            targetsInDb = accepted,
            stoppedBy = stoppedBy,
            elapsedMs = clock.nowMs() - startedMs,
        )
    }

    /**
     * 这个错误值不值得放弃整轮。
     *
     * - **401/403**：token 错了，剩下 199 条会用同一个错的 token 各失败一次，
     *   白等半分钟还刷一屏错误。立刻停。
     * - **连不上 / 超时**：网没了。同理，接着试是纯浪费。
     * - 其它（404、500、某张图坏了）：只是这一条的问题，跳过继续。
     */
    private fun fatalReason(e: Exception): String? {
        val f = e as? HttpFailure ?: return null
        return when (f.kind) {
            NetErrorKind.UNAUTHORIZED -> "令牌无效（${f.status}）"
            NetErrorKind.TIMEOUT -> "网络超时"
            NetErrorKind.TRANSPORT -> "连不上服务端"
            else -> null
        }
    }
}

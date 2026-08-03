package app.photoar.arview.cache

import app.photoar.arview.Clock
import app.photoar.arview.net.HttpFailure
import app.photoar.arview.NetErrorKind
import app.photoar.arview.PhotoSummary
import app.photoar.arview.net.PhotoArClient
import app.photoar.arview.net.TargetsDbFetch

/**
 * 执行一份 [CachePlan]：拉服务端预建的整库目标、按列表下载、删文件、重建本地 ARCore
 * 库（§11.3 / Phase 4）。
 *
 * 「拉预建库」这一步为什么在这里而不是扫描启动时：它是**下载**，而这一页刻意没有后台
 * 自动同步（「什么时候用流量该由人决定」，见 `CacheScreen`）。扫描启动时只做装库，
 * 一个字节都不下 —— 那条路上用户正举着手机等画面。
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
     * 服务端预建整库目标的落盘处。null 表示这一步整个跳过。
     *
     * 可注入（而不是从 [cache] 里自己造一个）的理由与 [rebuildTargetDb] 一样：这一步
     * 要打两个新接口、写两个新文件，而既有那批测试盯的是「下载 / 淘汰 / 断网」那半边。
     * 默认关掉之后，那些用例的请求序列一个字都不变。
     */
    private val targets: ServerTargetsStore? = null,
    /**
     * 服务端说「正在建」时的等待。默认真睡；单测注入一个只记账的。
     *
     * 注入的是**睡觉这个动作**而不是一个时钟：这里要的就是「把这条线程停一会儿」，
     * 而 [clock] 只回答「现在几点」。
     */
    private val sleep: (Long) -> Unit = { Thread.sleep(it) },
    /**
     * 重建 ARCore 多图库。真机上是 `LocalTargetDb`，单测里给个假的。
     * 返回被 ARCore 拒掉的 photoId —— 它们会被记进索引，下次不再白试。
     *
     * **排在最后一个参数**是刻意的：既有的用例用尾随 lambda 传它
     * （`CacheSync(client, cache, clock, spec) { … }`），挪到中间会让那种写法静默绑到
     * 另一个函数参数上 —— 编译得过的那些会变成「重建回调根本没被接上」。
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
     * 服务端预建整库目标那一步的结局。
     *
     * 前四种都是**正常**的（服务端那边的契约就有这几种状态），只有 [FAILED] 是真出错。
     * 而即便 [FAILED]，整轮同步照样算成功 —— 缩略图和视频都下好了，离线识别只是退回
     * 端上现建那一档。
     */
    enum class TargetsStatus {
        /** 没接 store，这一步没跑。 */
        SKIPPED,

        /** 304：本地那份就是最新的。稳态下最常见。 */
        UP_TO_DATE,

        /** 200：换上了新的一份。 */
        DOWNLOADED,

        /** 503：等到上限了还在建。下次同步再来。 */
        BUILDING,

        /** 404：一张照片都没被授权，本地那份已经删掉。 */
        EMPTY,

        FAILED,
    }

    /**
     * 预建库那一步的结果。
     *
     * @param version 服务端那套的版本号。拿到 manifest 就有，即使库没下成。
     * @param count 预建库覆盖多少张。**这个数可以比缓存条目多**（端侧默认留 200 张，
     *   预建库到 1000 张），中间那些照片认出来之后靠 manifest 查元数据。
     * @param overflow 因为 ARCore 的 1000 张上限而没进预建库的张数。非 0 要让用户看见 ——
     *   那几张永远得联网才认得出，而没有任何别的地方会解释这件事。
     */
    data class TargetsResult(
        val status: TargetsStatus = TargetsStatus.SKIPPED,
        val version: String? = null,
        val count: Int = 0,
        val overflow: Int = 0,
        val maxTargets: Int = 0,
        val bytes: Long = 0,
        val detail: String? = null,
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
        /** 服务端预建整库目标那一步。见 [TargetsResult]。 */
        val prebuilt: TargetsResult = TargetsResult(),
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

        // 预建库排在缩略图之前：它是离线识别真正的地基（服务端拿原图建的，覆盖到
        // 1000 张），而缩略图只是它装不上时的退路。中途被用户切走时，先保住好的那份。
        val targetsOutcome = syncTargets(progress, total)
        if (targetsOutcome.stoppedBy != null) {
            // 401 / 没网：剩下两百条会用同一个坏 token 或同一条断线各失败一次。
            // 这里直接返回而不是继续 —— 与下面那两个循环的 fatalReason 是同一条策略。
            //
            // 落盘一次再走：上面那两行 drop 已经把文件删了，不 flush 的话索引里还记着
            // 它们。那种不一致下一次 load 时会被 reconcile 修回来（以磁盘为准），但
            // 中间那段时间「缓存管理」页上的数字是错的。
            cache.flush()
            return Result(
                plan = plan,
                videosDropped = plan.dropVideo.size,
                photosDropped = plan.dropPhoto.size,
                targetsInDb = cache.entries().count { it.usableAsTarget },
                prebuilt = targetsOutcome.result,
                stoppedBy = targetsOutcome.stoppedBy,
                elapsedMs = clock.nowMs() - startedMs,
            )
        }

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
        //
        // 注意它排在下面那句 `cache.flush()` 前面，而 [LocalTargetDb.stale] 判过期时
        // **不看** index.json 的时间（看缩略图的，见 PhotoCache.newestThumbMs）——
        // 两件事必须一起成立。若哪天把过期判定改回索引时间，这里刚建好的库会被下一句
        // flush 立刻判成过期，于是每轮同步都白建一次，而且不报错。
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
            prebuilt = targetsOutcome.result,
            stoppedBy = stoppedBy,
            elapsedMs = clock.nowMs() - startedMs,
        )
    }

    private class TargetsOutcome(val result: TargetsResult, val stoppedBy: String? = null)

    /**
     * 把服务端预建的整库目标同步下来。
     *
     * 顺序是 **manifest 先、db 后**，这不是随意的：manifest 那个请求在服务端会
     * **顺手把构建踢起来**（它自己不等）。反过来先要 db 的话，第一次一定是 503，
     * 而那时候构建才刚开始 —— 白等一个 `Retry-After` 周期。
     *
     * 拿到新字节时要核一遍「ETag == manifest 的 version」：两个请求之间管理员可能入了
     * 十张照片，那时候手上这份 manifest 描述的是另一套照片。配错的后果是端上认出一张
     * 照片却查不到（或者查到错的）尺寸 —— 服务端那边费很大劲堵的就是这个方向。对不上
     * 就按服务端注释说的「重取一遍 manifest」，还对不上就这轮算了，下次同步会一致。
     *
     * 整条路上**任何一步失败都不影响这轮同步的其余部分**（除了 401 / 断网那种会让后面
     * 全部失败的）：离线识别退回端上现建那一档，功能不丢。
     */
    private fun syncTargets(progress: Progress?, total: Int): TargetsOutcome {
        val store = targets ?: return TargetsOutcome(TargetsResult())
        progress?.onStep(0, total, "离线识别库")
        val wait = TargetsBuildWait()
        try {
            var manifest = client.targetsManifest()
            val info = { s: TargetsStatus, bytes: Long, detail: String? ->
                TargetsResult(
                    status = s,
                    version = manifest.version,
                    count = manifest.count,
                    overflow = manifest.overflow,
                    maxTargets = manifest.maxTargets,
                    bytes = bytes,
                    detail = detail,
                )
            }
            while (true) {
                when (val got = client.targetsDb(store.snapshot()?.version)) {
                    is TargetsDbFetch.NotModified -> {
                        // 库字节没变，但 manifest 是每次现取的（标题 / hasVideo /
                        // overflow 刻意不在版本号里）—— 元数据照样要更新，否则一张照片
                        // 补了视频这件事在离线那条路上永远看不到。
                        store.refreshMeta(manifest)
                        return TargetsOutcome(info(TargetsStatus.UP_TO_DATE, store.bytes, null))
                    }

                    is TargetsDbFetch.Fresh -> {
                        val v = got.version
                        if (v != null && v != manifest.version) {
                            manifest = client.targetsManifest()
                            if (v != manifest.version) {
                                return TargetsOutcome(
                                    info(
                                        TargetsStatus.FAILED,
                                        0,
                                        "版本在取元数据与取库之间变了（有人正在入库），下次同步再来",
                                    ),
                                )
                            }
                        }
                        // v == null 表示 ETag 被中间的代理剥了。仍然按 manifest 的版本
                        // 存下来：那是手上唯一的版本陈述，而配错的代价只是某张照片的
                        // 元数据旧一轮（下一次同步就一致了）。不存的话每次同步都会重下
                        // 这几 MB，而且永远不会有 304。
                        val ok = store.store(got.bytes, manifest)
                        return TargetsOutcome(
                            if (ok) {
                                info(TargetsStatus.DOWNLOADED, got.bytes.size.toLong(), null)
                            } else {
                                info(TargetsStatus.FAILED, 0, "离线识别库写不进磁盘（空间不够？）")
                            },
                        )
                    }

                    is TargetsDbFetch.Building -> {
                        val delay = wait.nextDelayMs(got.retryAfterS)
                            ?: return TargetsOutcome(
                                info(
                                    TargetsStatus.BUILDING,
                                    store.bytes,
                                    "服务端还在建离线识别库（已等 ${wait.waitedS} 秒），" +
                                        "过一会儿再同步一次",
                                ),
                            )
                        progress?.onStep(0, total, "服务端正在建离线识别库，等 ${delay / 1000} 秒")
                        sleep(delay)
                    }

                    is TargetsDbFetch.Empty -> {
                        // 一张都没被授权（新部署，或者授权被撤了）。本地那份必须删 ——
                        // 留着就是「已经没权限看的照片，这台手机还能离线认出来」。
                        store.clear()
                        return TargetsOutcome(info(TargetsStatus.EMPTY, 0, null))
                    }
                }
            }
        } catch (e: Exception) {
            val fatal = fatalReason(e)
            return TargetsOutcome(
                TargetsResult(status = TargetsStatus.FAILED, detail = e.message),
                stoppedBy = fatal,
            )
        }
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

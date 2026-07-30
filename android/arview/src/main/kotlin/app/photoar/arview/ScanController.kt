package app.photoar.arview

/**
 * §11 的客户端状态机。
 *
 * 纯 Kotlin：没有 android.*、没有线程、没有网络、时间从 [Clock] 来。所有输入都是
 * 显式的方法调用，所有输出都走 [ScanEffects]。Phase 2 里真机之外唯一能被验证的
 * 东西就是它，所以它必须是可测的 —— AR 渲染与播放器那层薄到没有判断逻辑。
 *
 * **线程约定**：全部方法只能在同一个线程上调（Activity 用主线程 Handler 串
 * 起来）。内部没有任何同步。
 */

enum class ScanState { IDLE, SCANNING, LOADING_TARGET, TRACKING, PLAYING, PAUSED }

enum class NoticeKind {
    /** 连续扫 5 秒没命中（§13）。 */
    AIM_AT_PHOTO,

    /** 连续 3 次识别请求失败（§13），已请求重新探活。 */
    NETWORK_SLOW,

    /** token 不对。这个不是瞬时故障，重试无意义，扫描直接停。 */
    UNAUTHORIZED,

    /** imgdb 与 thumb 兜底都失败，这张照片被短期拉黑。 */
    TARGET_LOAD_FAILED,

    /** 走了 thumb 端上现场 addImage 的降级路径（§13）。 */
    IMGDB_FALLBACK,

    /** 目标装好了但 10 秒没在画面里找到，回到扫描。 */
    TARGET_NOT_FOUND,

    /** 关联的视频文件已不在 NAS 上（§13）。 */
    ASSET_MISSING,

    /** 参考图内容变过，特征可能已过期（§13）。 */
    REF_STALE,

    /** 服务端说不支持 Range，播放器要禁掉 seek（§13）。 */
    NO_SEEK,

    /** 视频 404 或解不开。保留 AR 跟踪框，不崩（§13）。 */
    VIDEO_UNPLAYABLE,

    /** 丢失跟踪，已暂停并保留播放位置。 */
    TRACKING_LOST,

    /**
     * 离线命中：ARCore 从本地多图库认出来的，没走网络（Phase 4 / §11.3）。
     *
     * 要提示，因为这条路的跟踪质量比服务端预建的 `.imgdb` 低一档 —— 本地库是用
     * 640px 缩略图现建的。用户看到框抖得比平时厉害时，这条提示能解释为什么。
     */
    LOCAL_HIT,

    /**
     * 认出来了，但视频既没缓存、此刻又没网（Phase 4）。
     *
     * 和 [VIDEO_UNPLAYABLE] 分开是刻意的：「没缓存」用户能自己解决（联网 / 同步
     * 一次），「坏了」不能。归成一句会让人去查 NAS 上那个文件。
     */
    VIDEO_NOT_CACHED,

    /** 清掉当前提示。 */
    CLEARED,
}

sealed interface ScanEvent {
    data class StateChanged(val from: ScanState, val to: ScanState) : ScanEvent
    data class Matched(val hit: Hit) : ScanEvent
    data class Notice(val kind: NoticeKind, val detail: String? = null) : ScanEvent
}

fun interface Clock {
    fun nowMs(): Long
}

/**
 * 状态机要做的副作用。实现在 [ScanRuntime]（相机 / 网络 / ARCore / ExoPlayer）。
 *
 * `releaseTarget` / `releasePlayer` / `pauseVideo` 必须幂等：状态机在几条不同
 * 的路径上都会调它们（用户退出、跟踪丢失超时、token 失效、Activity 暂停），
 * 不保证此刻真有东西可以释放。
 */
interface ScanEffects {
    /** 请求抓一帧。异步，抓到后回 [ScanController.onFrame]。 */
    fun captureFrame(seq: Long)

    /** 送去识别。异步，回 [ScanController.onRecognized] 或 [ScanController.onRecognizeFailed]。 */
    fun recognize(seq: Long, jpeg: ByteArray)

    /** 下载 .imgdb 并 `session.configure()`；失败时按 §13 降级用 thumb 现场构建。 */
    fun loadTarget(hit: Hit)

    /**
     * 取视频地址。
     *
     * Phase 4 起先看缓存：缓存里有就直接回一个 `file://` 的 [MediaInfo]（见
     * `cache.localMedia`），不请求 `GET /v1/photo/{id}/media`。缓存里没有又没网时
     * 调 [ScanController.onMediaNotCached]，别用 [ScanController.onMediaFailed] ——
     * 两件事的提示文案不一样。
     */
    fun fetchMedia(hit: Hit)

    /**
     * 卸掉当前目标。
     *
     * **Phase 4 起还要把本地多图库装回 session**：[loadTarget] 用单张 `.imgdb`
     * 做过 `session.configure()`，那一下会把本地库顶掉。不装回去的话，退出这张
     * 照片之后离线识别就没了 —— 而且是静默没的，表现为「昨天还能离线认，今天不行」。
     */
    fun releaseTarget()
    fun preparePlayer(hit: Hit, media: MediaInfo)
    fun playVideo()
    fun pauseVideo()
    fun releasePlayer()

    /** §13：连续失败要触发 endpoint 重新探活（Phase 3 的 EndpointResolver）。 */
    fun requestEndpointRefresh()

    fun emit(event: ScanEvent)
}

/**
 * 本地缓存索引的查询口（Phase 4 / §11.3）。
 *
 * 扫描期间 ARCore 的 session 里装的是本地多图库，它认出来的 `AugmentedImage.name`
 * 就是 photoId（Phase 2 定的）。状态机拿这个 id 来问一句「缓存里有它吗」，有就是
 * 一次离线命中。真机上实现是 `PhotoCache`，单测里给个 map。
 *
 * 返回 null 表示「不认识」—— 状态机会继续按 400ms 的节奏走服务端识别。
 */
fun interface LocalIndex {
    fun lookup(photoId: String): Hit?
}

class ScanController(
    private val fx: ScanEffects,
    private val clock: Clock,
    /** false 表示机型不支持 ARCore：§13 要求退化成「识别后全屏播放」，功能不丢。 */
    private val arAvailable: Boolean = true,
    /** 默认「本地什么都没有」，于是行为与 Phase 2/3 完全一致。 */
    private val localIndex: LocalIndex = LocalIndex { null },
) {

    companion object {
        /** §11.2：每 400ms 抽一帧。 */
        const val FRAME_INTERVAL_MS = 400L

        /** §13：连续 5 秒没命中就提示对准。 */
        const val AIM_HINT_MS = 5_000L

        /** §13：连续 3 次识别失败 → 提示 + 重新探活。 */
        const val NET_FAIL_LIMIT = 3

        /** §11.6：持续丢失跟踪超过 10 秒视为已转向另一张照片，回到扫描。 */
        const val LOST_GIVEUP_MS = 10_000L

        /** 抓帧请求多久没回就当它丢了。抓帧在 GL 线程上，偶发失败是正常的。 */
        const val CAPTURE_WATCHDOG_MS = 1_500L

        /**
         * 识别请求多久没回就当它超时。§13 说的是 2s，这里留到 4s：真正的
         * 超时由 HTTP 层报（那条更准），这个只是兜住「回调根本没来」。
         */
        const val RECOGNIZE_WATCHDOG_MS = 4_000L

        /** 装载目标（下 imgdb + configure）的上限。 */
        const val TARGET_LOAD_TIMEOUT_MS = 8_000L

        /** 装载失败的照片拉黑多久。不拉黑会「命中→失败→立刻又命中」死循环。 */
        const val BLOCKLIST_MS = 30_000L
    }

    var state: ScanState = ScanState.IDLE
        private set

    /** 当前锁定的照片。TRACKING/PLAYING/PAUSED 期间非空。 */
    var current: Hit? = null
        private set

    private var seq = 0L
    private var pendingCaptureSeq: Long? = null
    private var captureRequestedAt = 0L
    private var inFlightSeq: Long? = null
    private var inFlightSince = 0L
    private var lastCaptureAt: Long? = null

    private var scanningSince = 0L
    private var aimNoticeShown = false
    private var netFailures = 0

    private var loadingSince = 0L
    private var everTracked = false
    private var tracking = false
    private var notTrackingSince: Long? = null
    private var playerReady = false
    private var media: MediaInfo? = null

    private val blocked = HashMap<String, Long>()

    // ---- 外部输入 ----

    fun start() {
        if (state != ScanState.IDLE) return
        netFailures = 0
        setState(ScanState.SCANNING)
    }

    fun stop() {
        if (state == ScanState.IDLE) return
        resetTarget()
        pendingCaptureSeq = null
        inFlightSeq = null
        lastCaptureAt = null
        setState(ScanState.IDLE)
    }

    /**
     * 心跳。抽帧节流、各种看门狗、丢失跟踪的 10 秒判定都靠它推进。
     * 由渲染循环或一个 100ms 的 Handler 调都可以，不要求固定间隔。
     */
    fun tick() {
        val now = clock.nowMs()
        expireBlocklist(now)
        when (state) {
            ScanState.SCANNING -> tickScanning(now)
            ScanState.LOADING_TARGET ->
                if (now - loadingSince > TARGET_LOAD_TIMEOUT_MS) {
                    current?.let { onTargetFailed(it.photoId, "装载超时") }
                }
            ScanState.TRACKING, ScanState.PAUSED -> tickWaitingForTracking(now)
            ScanState.PLAYING, ScanState.IDLE -> Unit
        }
    }

    private fun tickScanning(now: Long) {
        inFlightSeq?.let {
            if (now - inFlightSince > RECOGNIZE_WATCHDOG_MS) {
                onRecognizeFailed(it, NetErrorKind.TIMEOUT, "识别请求没有回调")
            }
        }
        pendingCaptureSeq?.let {
            if (now - captureRequestedAt > CAPTURE_WATCHDOG_MS) pendingCaptureSeq = null
        }
        if (pendingCaptureSeq == null && inFlightSeq == null) {
            val last = lastCaptureAt
            if (last == null || now - last >= FRAME_INTERVAL_MS) {
                val s = ++seq
                pendingCaptureSeq = s
                captureRequestedAt = now
                lastCaptureAt = now
                fx.captureFrame(s)
            }
        }
        if (!aimNoticeShown && now - scanningSince >= AIM_HINT_MS) {
            aimNoticeShown = true
            notice(NoticeKind.AIM_AT_PHOTO)
        }
    }

    private fun tickWaitingForTracking(now: Long) {
        val since = notTrackingSince ?: return
        if (now - since <= LOST_GIVEUP_MS) return
        // §11.6：只有两个恢复抽帧的条件 —— 用户主动退出，或持续丢失跟踪
        // 超过 10 秒（视为已转向另一张照片）。
        if (!everTracked) notice(NoticeKind.TARGET_NOT_FOUND)
        resetTarget()
        setState(ScanState.SCANNING)
    }

    fun onFrame(seq: Long, jpeg: ByteArray) {
        if (seq != pendingCaptureSeq) return // 过期的帧，丢掉
        pendingCaptureSeq = null
        if (state != ScanState.SCANNING) return
        inFlightSeq = seq
        inFlightSince = clock.nowMs()
        fx.recognize(seq, jpeg)
    }

    fun onFrameFailed(seq: Long) {
        if (seq == pendingCaptureSeq) pendingCaptureSeq = null
    }

    fun onRecognized(seq: Long, outcome: RecognizeOutcome) {
        if (seq != inFlightSeq) return
        inFlightSeq = null
        netFailures = 0
        if (state != ScanState.SCANNING) return
        when (outcome) {
            is RecognizeOutcome.NoMatch -> Unit // §13：静默继续下一帧
            is RecognizeOutcome.Matched -> {
                // 刚刚装载失败过的照片当成未命中，否则会立刻再命中同一张。
                if (isBlocked(outcome.hit.photoId, clock.nowMs())) return
                acceptHit(outcome.hit)
            }
        }
    }

    fun onRecognizeFailed(seq: Long, kind: NetErrorKind, detail: String? = null) {
        if (seq != inFlightSeq) return
        inFlightSeq = null
        if (kind == NetErrorKind.UNAUTHORIZED) {
            // token 错了，每 400ms 重试一次只会刷日志。停下来让人去改设置。
            notice(NoticeKind.UNAUTHORIZED, detail)
            stop()
            return
        }
        netFailures++
        if (netFailures >= NET_FAIL_LIMIT) {
            netFailures = 0
            notice(NoticeKind.NETWORK_SLOW, detail)
            fx.requestEndpointRefresh()
        }
        // 其余情况静默重试：§13 明确「不阻塞相机预览」。
    }

    /**
     * @param alreadyTracking 目标此刻**已经**在画面里被跟踪着 —— 离线命中就是这种
     *   情况：是 ARCore 先认出来才有这次命中的，不存在「装好了但还没找到」的空窗，
     *   所以那 10 秒的 [NoticeKind.TARGET_NOT_FOUND] 判定不该启动。
     */
    fun onTargetLoaded(
        photoId: String,
        usedThumbFallback: Boolean = false,
        alreadyTracking: Boolean = false,
    ) {
        if (photoId != current?.photoId || state != ScanState.LOADING_TARGET) return
        if (usedThumbFallback) notice(NoticeKind.IMGDB_FALLBACK)
        everTracked = alreadyTracking || !arAvailable
        tracking = alreadyTracking || !arAvailable
        // AR 模式下此刻还没找到图，10 秒判定从现在开始算；全屏兜底模式没有
        // 「跟踪」这件事，所以不设。
        notTrackingSince = if (arAvailable && !alreadyTracking) clock.nowMs() else null
        setState(ScanState.TRACKING)
        maybeStartPlayback()
    }

    fun onTargetFailed(photoId: String, detail: String? = null) {
        if (photoId != current?.photoId) return
        blocked[photoId] = clock.nowMs() + BLOCKLIST_MS
        notice(NoticeKind.TARGET_LOAD_FAILED, detail)
        resetTarget()
        setState(ScanState.SCANNING)
    }

    fun onMedia(photoId: String, info: MediaInfo) {
        val hit = current ?: return
        if (photoId != hit.photoId) return
        media = info
        if (!info.playable) {
            // §13：提示文件已不在 NAS 上，但**继续跟踪** —— 用户还能看到框，
            // 也才有地方放「重新指定」的入口。
            notice(NoticeKind.ASSET_MISSING, info.nasPath ?: info.reason)
            return
        }
        if (!info.supportsRange) notice(NoticeKind.NO_SEEK)
        fx.preparePlayer(hit, info)
    }

    fun onMediaFailed(photoId: String, detail: String? = null) {
        if (photoId != current?.photoId) return
        notice(NoticeKind.VIDEO_UNPLAYABLE, detail)
    }

    /**
     * 视频没缓存、此刻又没网（Phase 4）。
     *
     * 跟踪照旧 —— 用户还能看到框，也才有地方放「联网后再看」这句话。
     */
    fun onMediaNotCached(photoId: String) {
        if (photoId != current?.photoId) return
        notice(NoticeKind.VIDEO_NOT_CACHED)
    }

    fun onPlayerReady() {
        playerReady = true
        maybeStartPlayback()
    }

    fun onPlayerError(detail: String? = null) {
        playerReady = false
        notice(NoticeKind.VIDEO_UNPLAYABLE, detail)
        // §13：显示提示叠加层，保留 AR 跟踪框，不崩。退回 TRACKING 而不是
        // 退出目标 —— 视频坏了不代表这张照片认错了。
        if (state == ScanState.PLAYING || state == ScanState.PAUSED) {
            fx.pauseVideo()
            setState(ScanState.TRACKING)
        }
    }

    fun onPlaybackEnded() {
        if (!arAvailable) {
            // 全屏兜底模式播完就回到扫描。AR 模式下播放器是循环的，
            // 正常不会走到这里。
            exitTarget()
        } else if (state == ScanState.PLAYING) {
            fx.playVideo()
        }
    }

    /**
     * ARCore 每帧的跟踪状态。[photoId] 为 null 表示画面里没有任何被跟踪的图。
     */
    fun onTracking(photoId: String?, isTracking: Boolean) {
        // Phase 4：扫描期间 session 里装的是本地多图库（§11.3），所以 ARCore 在这个
        // 状态下认出来的东西**就是一次离线命中** —— 不用等那 400ms 一轮的服务端识别，
        // 也不用有网。这是 spec 里「常扫照片离线可用」真正落地的地方。
        if (state == ScanState.SCANNING) {
            if (photoId != null && isTracking) tryLocalHit(photoId)
            return
        }
        // §11 最后一段：session.configure() 换库会短暂重置 session，
        // LOADING_TARGET 期间的跟踪中断必须容忍，不能误判成「丢失」。
        if (state == ScanState.LOADING_TARGET || state == ScanState.IDLE) return
        val hit = current ?: return
        if (photoId != null && photoId != hit.photoId) return

        if (isTracking) {
            tracking = true
            everTracked = true
            notTrackingSince = null
            if (state == ScanState.PAUSED) {
                fx.playVideo() // §11.9：恢复 → 续播（位置由播放器保留）
                setState(ScanState.PLAYING)
            } else {
                maybeStartPlayback()
            }
        } else {
            tracking = false
            if (notTrackingSince == null) notTrackingSince = clock.nowMs()
            if (state == ScanState.PLAYING) {
                fx.pauseVideo()
                notice(NoticeKind.TRACKING_LOST)
                setState(ScanState.PAUSED)
            }
        }
    }

    /** 用户主动退出当前照片（§11.6 的第一个恢复条件）。 */
    fun exitTarget() {
        if (state == ScanState.IDLE || state == ScanState.SCANNING) return
        resetTarget()
        setState(ScanState.SCANNING)
    }

    // ---- 内部 ----

    /**
     * 离线命中。
     *
     * ARCore 报的名字就是 photoId（Phase 2 定的），但只有缓存索引里确实有这一条时
     * 才当命中 —— 库和索引理论上同步，真出现不一致（索引被清了、库还在 session 里）
     * 时宁可继续走服务端那条路，也不要拿一条没有元数据的命中往下走。
     */
    private fun tryLocalHit(photoId: String) {
        if (isBlocked(photoId, clock.nowMs())) return
        val hit = localIndex.lookup(photoId) ?: return
        acceptHit(hit, local = true)
    }

    /**
     * @param local 离线命中。两处不同：**不调 [ScanEffects.loadTarget]**，以及直接
     *   进入「已在跟踪」。
     *
     *   不调 loadTarget 是硬要求而不是省一次网络：那个方法会用单张 `.imgdb` 做
     *   `session.configure()`，而换库会重置 session —— 把此刻正被跟踪的这张图弄丢，
     *   于是刚认出来就立刻「丢失跟踪」。库已经在 session 里了，什么都不用做。
     */
    private fun acceptHit(hit: Hit, local: Boolean = false) {
        current = hit
        media = null
        playerReady = false
        everTracked = false
        tracking = false
        notTrackingSince = null
        loadingSince = clock.nowMs()
        // §11.6：命中后立即停止抽帧与识别请求。靠 state 不再是 SCANNING 实现。
        pendingCaptureSeq = null
        inFlightSeq = null
        fx.emit(ScanEvent.Matched(hit))
        if (hit.refStale) notice(NoticeKind.REF_STALE)
        setState(ScanState.LOADING_TARGET)
        fx.fetchMedia(hit)
        when {
            local -> onTargetLoaded(hit.photoId, alreadyTracking = true)
            arAvailable -> fx.loadTarget(hit)
            // 不支持 ARCore：没有目标库要装，直接进入「已装载」。
            else -> onTargetLoaded(hit.photoId)
        }
        if (local) notice(NoticeKind.LOCAL_HIT)
    }

    private fun maybeStartPlayback() {
        if (state == ScanState.TRACKING && playerReady && tracking) {
            fx.playVideo()
            setState(ScanState.PLAYING)
        }
    }

    private fun resetTarget() {
        fx.releasePlayer()
        fx.releaseTarget()
        current = null
        media = null
        playerReady = false
        tracking = false
        everTracked = false
        notTrackingSince = null
    }

    private fun setState(next: ScanState) {
        if (next == state) return
        val prev = state
        state = next
        if (next == ScanState.SCANNING) {
            scanningSince = clock.nowMs()
            aimNoticeShown = false
            lastCaptureAt = null // 立刻抽第一帧，不等 400ms
        }
        if (prev == ScanState.SCANNING && aimNoticeShown) notice(NoticeKind.CLEARED)
        fx.emit(ScanEvent.StateChanged(prev, next))
    }

    private fun notice(kind: NoticeKind, detail: String? = null) {
        fx.emit(ScanEvent.Notice(kind, detail))
    }

    private fun isBlocked(photoId: String, now: Long): Boolean {
        val until = blocked[photoId] ?: return false
        return until > now
    }

    private fun expireBlocklist(now: Long) {
        if (blocked.isEmpty()) return
        val it = blocked.entries.iterator()
        while (it.hasNext()) if (it.next().value <= now) it.remove()
    }
}

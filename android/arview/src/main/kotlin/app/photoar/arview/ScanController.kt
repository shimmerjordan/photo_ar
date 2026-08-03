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

    /**
     * 凭证不被接受：没登录、登录过期（访客 30 天 / 管理员 12 小时）、或者被停用。
     *
     * 这个不是瞬时故障，重试无意义，扫描直接停 —— 并且要把用户**送回登录界面**
     * （[ScanEffects.requestLogin]）。只提示不导航的话，用户看到一句「凭证不对」而
     * 手机还举在照片前面，没有任何下一步可做。
     */
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
     * 认出来了、框也画上了，但超过 [ScanController.HIT_TO_PLAY_BUDGET_MS] 还没出画。
     *
     * **只提示，不放弃**，两个理由：
     *
     * 1. 识别是对的、跟踪是好的，此刻放弃等于扔掉一次正确的命中；
     * 2. 放弃会回到 SCANNING，而照片还在镜头前 —— 下一帧立刻又命中同一张，
     *    变成「命中→放弃→命中」的循环，比一直等更糟。
     *
     * 所以这条提示的作用是**把无声的等待变成有声的等待**：在它之前，播放器卡在缓冲
     * 时界面上一个字都不说，用户只能猜是不是自己没对准。
     *
     * 与 [VIDEO_NOT_CACHED] / [VIDEO_UNPLAYABLE] / [ASSET_MISSING] 互斥：那三条已经
     * 说清了视频侧出了什么事，再叠一句「有点慢」会把更具体的信息盖掉。
     */
    VIDEO_SLOW,

    /**
     * 离线命中：ARCore 从装在 session 里的多图库认出来的，没走网络（Phase 4 / §11.3）。
     *
     * 要提示，因为跟踪质量取决于**装的是哪一份库**：服务端预建那份（原图建的）和联网
     * 命中完全一样，端上现建那份（640px 缩略图）低一档。用户看到框抖得比平时厉害时，
     * 这条提示能解释为什么 —— 所以具体是哪一份由 `detail` 带出来（`ScanRuntime` 填，
     * 只有它知道此刻装的是谁）。
     */
    LOCAL_HIT,

    /**
     * 服务端预建的整库目标装不上，已退回端上现建那份（Phase 6）。
     *
     * 最可能的原因是服务端的 `arcoreimg` 比这台手机上的 ARCore 新（见
     * `ar.ArSessionHolder.deserializeDb`）。必须提示而不是只记日志：离线识别还在，
     * 但降了一档 —— 而这件事在界面上唯一的表现就是「框比以前抖」。不说的话，一个
     * 运维层面的版本问题会被当成「这个 App 越来越不准了」。
     *
     * 与 [IMGDB_FALLBACK] 分开：那条是**某一张照片**的单目标库取不到（每次命中都可能
     * 发生、下一次就好了），这条是**整台手机**的离线识别降档（要运维去处理）。
     */
    TARGETS_DB_FALLBACK,

    /**
     * 认出来了，但视频既没缓存、此刻又没网（Phase 4）。
     *
     * 和 [VIDEO_UNPLAYABLE] 分开是刻意的：「没缓存」用户能自己解决（联网 / 同步
     * 一次），「坏了」不能。归成一句会让人去查 NAS 上那个文件。
     */
    VIDEO_NOT_CACHED,

    /**
     * 端上提特征没走通，已静默回退到上传整帧（`feat.FeaturePathPolicy`）。
     *
     * 要提示，但措辞不能像报错：功能一点没丢，只是走了慢一点的那条路。不提示的话，
     * 用户打开了那个开关却完全不知道它其实没生效 —— 而这条路的全部意义就是更快。
     */
    FEATURES_FALLBACK,

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

    /**
     * 把用户送回登录界面。
     *
     * 与 [NoticeKind.UNAUTHORIZED] 那条提示是**两件事**：提示是「告诉他发生了什么」，
     * 这个是「带他去能解决的地方」。只有前者的话，扫描界面上会出现一句解释加一个死
     * 胡同 —— 而这条路径在 token 过期时是必然会走到的（管理员会话只有 12 小时）。
     *
     * 实现必须**幂等**：几条并发的请求可能同时拿到 401。状态机自己也做了一次去重
     * （见 [ScanController.onUnauthorized]），两层都有是因为去重那一层依赖
     * 「[ScanController.start] 之后才会再报一次」，而实现方可能在别处也调它。
     */
    fun requestLogin()

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

        /**
         * **命中到出画的总预算。**
         *
         * 需求给的容忍度是「从对准照片到视频开始播放 10 秒」。离线那条路（服务端预建
         * 库已装上）不成问题：ARCore 一认出来就是命中，视频又在本地，通常 1 秒内出画。
         * 有风险的是**服务端兜底那条路**（超 1000 张、或预建库装不上），它由若干段
         * 串起来 —— 而单段超时无论怎么调都不会自动加起来小于 10 秒，所以这里立一条
         * 总预算当**兜底闸门**，让各段的数只负责「诊断得准」，不负责「加起来够」。
         *
         * 兜底路最坏情况的账（都是本文件与 `net.PhotoArClient` 里的常量）：
         *
         * | 段 | 上限 | 说明 |
         * |---|---|---|
         * | 抽帧 | 1.5s | [CAPTURE_WATCHDOG_MS]，GL 线程偶发失败 |
         * | 识别一次 | 2.5s | [RECOGNIZE_WATCHDOG_MS]，HTTP 层是 2s |
         * | 装单张目标 | 4s | [TARGET_LOAD_TIMEOUT_MS]，imgdb 4.3KB + configure |
         * | 取视频地址 | 2.5s | `MEDIA_TIMEOUT_MS`，与上一段**并行** |
         * | ARCore 找到图 | 4s | [TARGET_FIND_TIMEOUT_MS] |
         * | 播放器就绪 | 剩下的 | 没有单独的超时，全靠这条总预算 |
         *
         * 抽帧与识别（4s）在命中**之前**，所以预算 6s 留给命中之后，总计 10s 打平。
         *
         * **它只在一个地方是唯一的闸门**：`everTracked && 播放器一直不就绪`。那种情况
         * `notTrackingSince` 是 null，[tickWaitingForTracking] 第一行就返回了 —— 在这条
         * 预算之前，那是一个**一个超时都没有的无限等待**：框画在照片上、视频永远不来、
         * 界面上一个字都不说。其余情况都有更准的单段超时先触发。
         *
         * 超预算的动作是**只提示、不放弃**（见 [NoticeKind.VIDEO_SLOW]）。
         */
        const val HIT_TO_PLAY_BUDGET_MS = 6_000L

        /**
         * 目标装好之后，多久没在画面里找到就放弃、回到扫描。
         *
         * **与 [LOST_GIVEUP_MS] 分开**（原来共用 10s）。两者的正确答案本来就不同：
         *
         * - 这一条是「还没找到」：屏幕上什么都没有，纯死等，而且整段都算在命中到出画
         *   的 10 秒里 —— 应该短。放弃的代价也很低：回到扫描后 400ms 就再试一次。
         * - [LOST_GIVEUP_MS] 是「播过之后丢了」：用户很可能只是在挪手机，放弃要连播放
         *   位置一起丢 —— 应该长。
         *
         * 共用一个数的结果是「还没找到」白占 10 秒，直接把总预算吃穿。
         */
        const val TARGET_FIND_TIMEOUT_MS = 4_000L

        /** §11.6：**播过之后**持续丢失跟踪超过 10 秒，视为已转向另一张照片，回到扫描。 */
        const val LOST_GIVEUP_MS = 10_000L

        /** 抓帧请求多久没回就当它丢了。抓帧在 GL 线程上，偶发失败是正常的。 */
        const val CAPTURE_WATCHDOG_MS = 1_500L

        /**
         * 识别请求多久没回就当它超时。
         *
         * §13 说的门限是 2s，真正的超时由 HTTP 层报（那条更准，还带得上错误类型），
         * 这个只兜住「回调根本没来」。所以它只需要比 HTTP 那 2s 多一点余量 ——
         * **原来是 4s，收到 2.5s**：多出来的 1.5 秒不换任何东西，只是在超时那一路上
         * 让下一次尝试晚 1.5 秒开始。
         */
        const val RECOGNIZE_WATCHDOG_MS = 2_500L

        /**
         * 装载目标（下 imgdb + `session.configure()`）的上限。**原来是 8s，收到 4s。**
         *
         * 够做什么：一次 imgdb 下载（`DOWNLOAD_TIMEOUT_MS` 3s）+ GL 线程上 deserialize
         * 与 configure（毫秒级）。
         *
         * 不够做什么：**imgdb 与缩略图两次都从零下载**（3s + 3s）。这是刻意的取舍 ——
         * 那条双失败的路要花 6 秒以上，而它换来的只是一次「跟踪质量低一档」的降级命中。
         * 与其占掉大半个预算，不如放弃、回扫描，400ms 后再试一次（`TargetLoader` 会把
         * 已下到的那份写进磁盘缓存，重试通常就不再走网络了）。
         */
        const val TARGET_LOAD_TIMEOUT_MS = 4_000L

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

    /** 这一轮扫描已经请求过重新登录。见 [onUnauthorized]。 */
    private var loginRequested = false

    private var loadingSince = 0L
    private var everTracked = false
    private var tracking = false
    private var notTrackingSince: Long? = null
    private var playerReady = false
    private var media: MediaInfo? = null

    /** 本次命中的起点，[HIT_TO_PLAY_BUDGET_MS] 从这里算。 */
    private var hitAt = 0L

    /** 本次命中有没有真的出过画。出过之后总预算就不再有意义（承诺已经兑现）。 */
    private var everPlayed = false

    /**
     * 已经就视频侧报过一句更具体的提示（没缓存 / 文件不在 / 播不了）。
     *
     * 用来压掉 [NoticeKind.VIDEO_SLOW]：那三条都已经解释了为什么没出画，再补一句
     * 「有点慢」只会盖掉更有用的信息。
     */
    private var videoProblemReported = false

    /** [NoticeKind.VIDEO_SLOW] 每次命中只报一次。 */
    private var slowNoticeShown = false

    private val blocked = HashMap<String, Long>()

    // ---- 外部输入 ----

    fun start() {
        if (state != ScanState.IDLE) return
        netFailures = 0
        // 重新开扫 = 用户已经处理过上一次的凭证问题（重新登录了，或者干脆没登录就
        // 又点了开始）。所以这个闸门在这里放开，而不是在 stop() 里 —— stop() 也会被
        // onUnauthorized 自己调，在那里清掉就等于每条 401 都报一次。
        loginRequested = false
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
            ScanState.TRACKING -> {
                // 顺序要紧：先查预算再查跟踪。反过来的话，「找不到图」那一路会先被
                // resetTarget 清掉 current，预算这边就再也拿不到 photoId 了。
                tickPlayBudget(now)
                tickWaitingForTracking(now)
            }
            ScanState.PAUSED -> tickWaitingForTracking(now)
            ScanState.PLAYING, ScanState.IDLE -> Unit
        }
    }

    /**
     * 命中到出画的总预算（[HIT_TO_PLAY_BUDGET_MS]）。
     *
     * 只在**框已经画上、视频还没来**这一种情况下是唯一的闸门 —— 其余情况都有更准的
     * 单段超时先触发（见 [HIT_TO_PLAY_BUDGET_MS] 的注释里那张表）。所以这里不放弃、
     * 不改状态，只报一句 [NoticeKind.VIDEO_SLOW]。
     */
    private fun tickPlayBudget(now: Long) {
        if (everPlayed || slowNoticeShown) return
        if (!everTracked) return // 还没找到图：交给 TARGET_FIND_TIMEOUT_MS，那条的提示更准
        if (videoProblemReported) return // 已经说过更具体的原因了
        if (now - hitAt <= HIT_TO_PLAY_BUDGET_MS) return
        slowNoticeShown = true
        notice(NoticeKind.VIDEO_SLOW)
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
        // 「还没找到过」与「播过之后丢了」用不同的上限，理由见 TARGET_FIND_TIMEOUT_MS。
        val limit = if (everTracked) LOST_GIVEUP_MS else TARGET_FIND_TIMEOUT_MS
        if (now - since <= limit) return
        // §11.6：只有两个恢复抽帧的条件 —— 用户主动退出，或持续丢失跟踪
        // 超过上限（视为已转向另一张照片）。
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
            onUnauthorized(detail)
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
     * 端上提特征回退到传 JPEG（Phase 5）。
     *
     * 只报一句提示，**不动状态机的任何状态**：这条路回退之后功能完全一样（只是慢一点），
     * 把它算进 `netFailures` 会触发一次没必要的重新探活（1.5 秒），而网络根本没问题。
     * 报几遍由 `feat.FeaturePathPolicy` 决定（每个进程一次），这里不去重。
     */
    fun onFeatureFallback(detail: String? = null) {
        notice(NoticeKind.FEATURES_FALLBACK, detail)
    }

    /**
     * 服务端预建的整库目标装不上，已退回端上现建那份（Phase 6）。
     *
     * 和 [onFeatureFallback] 一样：**只报一句提示，不动状态机的任何状态**。离线识别
     * 还在（只是降一档），把它算进 `netFailures` 会触发一次没必要的重新探活，而网络
     * 根本没问题 —— 这件事发生在装库那一步，一个字节都没走网。
     *
     * 报几遍由调用方控制（每次会话起来时装一次库，所以自然就是每次进扫描页一次）。
     */
    fun onTargetsDbFallback(detail: String? = null) {
        notice(NoticeKind.TARGETS_DB_FALLBACK, detail)
    }

    /**
     * 任何一条请求拿到 401（或 403）：凭证不被接受。
     *
     * **单独一个入口**，不是只在 [onRecognizeFailed] 里判：401 还会从 imgdb 下载和
     * media 元数据那两条路上来（它们与识别用的是同一个 token）。而那两条现在分别报
     * [NoticeKind.TARGET_LOAD_FAILED] 与 [NoticeKind.VIDEO_UNPLAYABLE] —— 那两句提示
     * 会让人去查这张照片和 NAS 上那个视频文件，而真正的原因是登录过期了。
     * 服务端把管理员会话定成 12 小时，所以这不是边角情况，是每天都会发生一次的事。
     *
     * 每次 [start] 之后只报一次：扫描时每 400ms 一个请求，几条并发的请求会同时拿到
     * 401，逐条报就是一串重复提示加一串导航请求。
     */
    fun onUnauthorized(detail: String? = null) {
        if (loginRequested) {
            // 已经报过了，但仍然要保证扫描是停着的 —— 万一是在 stop() 之后又有一条
            // 迟到的请求回来，而此时状态机已经被重新 start 过。
            if (state != ScanState.IDLE) stop()
            return
        }
        loginRequested = true
        // 顺序：先提示（说明为什么），再停（别再发请求了），最后导航。
        // 反过来的话 stop() 里的 setState 会先发一次 CLEARED，把提示擦掉。
        notice(NoticeKind.UNAUTHORIZED, detail)
        stop()
        fx.requestLogin()
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
            videoProblemReported = true
            notice(NoticeKind.ASSET_MISSING, info.nasPath ?: info.reason)
            return
        }
        if (!info.supportsRange) notice(NoticeKind.NO_SEEK)
        fx.preparePlayer(hit, info)
    }

    fun onMediaFailed(photoId: String, detail: String? = null) {
        if (photoId != current?.photoId) return
        videoProblemReported = true
        notice(NoticeKind.VIDEO_UNPLAYABLE, detail)
    }

    /**
     * 视频没缓存、此刻又没网（Phase 4）。
     *
     * 跟踪照旧 —— 用户还能看到框，也才有地方放「联网后再看」这句话。
     */
    fun onMediaNotCached(photoId: String) {
        if (photoId != current?.photoId) return
        videoProblemReported = true
        notice(NoticeKind.VIDEO_NOT_CACHED)
    }

    fun onPlayerReady() {
        playerReady = true
        maybeStartPlayback()
    }

    fun onPlayerError(detail: String? = null) {
        playerReady = false
        videoProblemReported = true
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
        hitAt = loadingSince
        everPlayed = false
        videoProblemReported = false
        slowNoticeShown = false
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
        // hitAt / everPlayed / videoProblemReported / slowNoticeShown 不在这里清：
        // 它们都是「本次命中」的属性，由 acceptHit 统一置位。这里清了没坏处，但
        // 会让「哪里负责初始化」变成两个地方 —— 而漏一个字段就是一次静默的错判。
    }

    private fun setState(next: ScanState) {
        if (next == state) return
        val prev = state
        state = next
        // 出过画就说明承诺已经兑现，总预算不再有意义（此后是 TRACKING_LOST 那套）。
        if (next == ScanState.PLAYING) everPlayed = true
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

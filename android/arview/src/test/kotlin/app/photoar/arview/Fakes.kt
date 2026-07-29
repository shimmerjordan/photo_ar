package app.photoar.arview

/** 可手动推进的时钟。状态机里所有时间判定都走它，所以测试里没有 sleep。 */
class FakeClock(var now: Long = 1_000_000L) : Clock {
    override fun nowMs(): Long = now

    fun advance(ms: Long) {
        now += ms
    }
}

/** 记录副作用调用顺序的假实现。 */
class FakeEffects : ScanEffects {
    val calls = ArrayList<String>()
    val events = ArrayList<ScanEvent>()

    /** 最近一次 captureFrame 的 seq。 */
    var lastCaptureSeq: Long? = null
        private set

    var preparedMedia: MediaInfo? = null
        private set

    override fun captureFrame(seq: Long) {
        lastCaptureSeq = seq
        calls += "captureFrame"
    }

    override fun recognize(seq: Long, jpeg: ByteArray) {
        calls += "recognize"
    }

    override fun loadTarget(hit: Hit) {
        calls += "loadTarget:${hit.photoId}"
    }

    override fun fetchMedia(hit: Hit) {
        calls += "fetchMedia:${hit.photoId}"
    }

    override fun releaseTarget() {
        calls += "releaseTarget"
    }

    override fun preparePlayer(hit: Hit, media: MediaInfo) {
        preparedMedia = media
        calls += "preparePlayer"
    }

    override fun playVideo() {
        calls += "playVideo"
    }

    override fun pauseVideo() {
        calls += "pauseVideo"
    }

    override fun releasePlayer() {
        calls += "releasePlayer"
    }

    override fun requestEndpointRefresh() {
        calls += "requestEndpointRefresh"
    }

    override fun emit(event: ScanEvent) {
        events += event
    }

    fun notices(): List<NoticeKind> =
        events.filterIsInstance<ScanEvent.Notice>().map { it.kind }

    fun states(): List<ScanState> =
        events.filterIsInstance<ScanEvent.StateChanged>().map { it.to }

    fun clear() {
        calls.clear()
        events.clear()
    }

    fun count(call: String): Int = calls.count { it == call }
}

fun hit(
    photoId: String = "a".repeat(32),
    printWidthM: Float = 0.152f,
    refAspect: Float? = 1.5f,
    refStale: Boolean = false,
): Hit = Hit(
    photoId = photoId,
    inliers = 47,
    printWidthM = printWidthM,
    refAspect = refAspect,
    imgdbUrl = "/v1/photo/$photoId/imgdb",
    refThumbUrl = "/v1/photo/$photoId/thumb",
    mediaUrl = "/v1/photo/$photoId/media",
    refStale = refStale,
    latencyMs = 63,
)

fun media(
    url: String? = "/v1/asset/deadbeef/stream",
    supportsRange: Boolean = true,
    missing: Boolean = false,
    absolute: Boolean = false,
): MediaInfo = MediaInfo(
    url = url,
    via = if (absolute) "direct_link" else "nas_serve",
    absolute = absolute,
    supportsRange = supportsRange,
    bytes = 1_548_392L,
    durationMs = 12_400L,
    missing = missing,
    nasPath = "/share/Video/2019/IMG_0421.mov",
    reason = null,
)

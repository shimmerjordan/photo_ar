package app.photoar.arview

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * §11 状态机 + §13 错误处理的行为。
 *
 * 这些用例是 Phase 2 里唯一不需要真机就能验证的东西，所以每一条都对应规格里的
 * 一句话，而不是对应实现里的一个分支。
 */
class ScanControllerTest {

    private lateinit var clock: FakeClock
    private lateinit var fx: FakeEffects
    private lateinit var c: ScanController

    @Before
    fun setUp() {
        clock = FakeClock()
        fx = FakeEffects()
        c = ScanController(fx, clock, arAvailable = true)
    }

    // ---- 抽帧节流（§11.2）----

    @Test
    fun `启动后进入扫描并在第一个 tick 就抽帧`() {
        c.start()
        assertEquals(ScanState.SCANNING, c.state)
        assertEquals(0, fx.count("captureFrame"))
        c.tick()
        assertEquals(1, fx.count("captureFrame"))
    }

    @Test
    fun `抽帧间隔是 400ms`() {
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.NoMatch("low_inliers", 41))

        clock.advance(399)
        c.tick()
        assertEquals("还没到 400ms 不该再抽", 1, fx.count("captureFrame"))
        clock.advance(1)
        c.tick()
        assertEquals(2, fx.count("captureFrame"))
    }

    @Test
    fun `同一时刻只允许一个识别请求在飞`() {
        c.start()
        c.tick()
        c.onFrame(fx.lastCaptureSeq!!, ByteArray(1))
        clock.advance(10_000)
        c.tick()
        // 请求还在飞（虽然过了看门狗时限，但那一步会先把它判成超时再重抽）
        assertEquals(1, fx.count("recognize"))
    }

    @Test
    fun `过期的帧被丢掉`() {
        c.start()
        c.tick()
        val stale = fx.lastCaptureSeq!! - 1
        c.onFrame(stale, ByteArray(1))
        assertEquals(0, fx.count("recognize"))
    }

    @Test
    fun `抽帧失败后下一个 tick 重抽`() {
        c.start()
        c.tick()
        c.onFrameFailed(fx.lastCaptureSeq!!)
        clock.advance(400)
        c.tick()
        assertEquals(2, fx.count("captureFrame"))
    }

    @Test
    fun `抽帧回调始终不来时看门狗会放行下一次抽帧`() {
        c.start()
        c.tick()
        clock.advance(ScanController.CAPTURE_WATCHDOG_MS + 1)
        c.tick()
        assertEquals(2, fx.count("captureFrame"))
    }

    @Test
    fun `识别回调始终不来时算一次失败并继续抽帧`() {
        c.start()
        c.tick()
        c.onFrame(fx.lastCaptureSeq!!, ByteArray(1))
        clock.advance(ScanController.RECOGNIZE_WATCHDOG_MS + 1)
        c.tick()
        assertEquals(2, fx.count("captureFrame"))
        assertEquals(ScanState.SCANNING, c.state)
    }

    // ---- 未命中与提示（§13）----

    @Test
    fun `连续 5 秒未命中提示对准照片且只提示一次`() {
        c.start()
        c.tick()
        clock.advance(4_999)
        c.tick()
        assertFalse(fx.notices().contains(NoticeKind.AIM_AT_PHOTO))
        clock.advance(1)
        c.tick()
        clock.advance(5_000)
        c.tick()
        assertEquals(1, fx.notices().count { it == NoticeKind.AIM_AT_PHOTO })
    }

    @Test
    fun `命中后清掉对准提示`() {
        c.start()
        c.tick()
        clock.advance(5_000)
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        assertTrue(fx.notices().contains(NoticeKind.CLEARED))
    }

    // ---- 命中 → 装载 → 跟踪 → 播放 ----

    @Test
    fun `命中后立刻停止抽帧与识别`() {
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        assertEquals(ScanState.LOADING_TARGET, c.state)
        fx.clear()
        repeat(4) {
            clock.advance(500)
            c.tick()
        }
        // §11.6：命中后立即停止抽帧与识别请求
        assertEquals(0, fx.count("captureFrame"))
        assertEquals(0, fx.count("recognize"))
    }

    @Test
    fun `命中同时取媒体信息与装载目标`() {
        matchOnce()
        assertTrue(fx.calls.contains("fetchMedia:${hit().photoId}"))
        assertTrue(fx.calls.contains("loadTarget:${hit().photoId}"))
    }

    @Test
    fun `目标装好进入跟踪`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        assertEquals(ScanState.TRACKING, c.state)
    }

    @Test
    fun `跟踪上且播放器就绪才开始播`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onMedia(hit().photoId, media())
        assertTrue(fx.calls.contains("preparePlayer"))
        c.onPlayerReady()
        assertEquals("还没跟踪上不该播", ScanState.TRACKING, c.state)
        c.onTracking(hit().photoId, true)
        assertEquals(ScanState.PLAYING, c.state)
        assertEquals(1, fx.count("playVideo"))
    }

    @Test
    fun `先跟踪上后播放器就绪也能播`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onTracking(hit().photoId, true)
        assertEquals(ScanState.TRACKING, c.state)
        c.onMedia(hit().photoId, media())
        c.onPlayerReady()
        assertEquals(ScanState.PLAYING, c.state)
    }

    @Test
    fun `装载期间的跟踪中断被忽略`() {
        matchOnce()
        c.onTracking(null, false)
        c.onTracking(hit().photoId, false)
        // §11 最后一段：configure 会短暂重置 session，这期间不能误判成丢失
        assertEquals(ScanState.LOADING_TARGET, c.state)
        assertEquals(0, fx.count("pauseVideo"))
    }

    @Test
    fun `跟踪到别的照片时忽略`() {
        playing()
        c.onTracking("b".repeat(32), false)
        assertEquals(ScanState.PLAYING, c.state)
    }

    // ---- 丢失跟踪（§11.6 / §11.9）----

    @Test
    fun `丢失跟踪暂停并保留位置`() {
        playing()
        c.onTracking(hit().photoId, false)
        assertEquals(ScanState.PAUSED, c.state)
        assertEquals(1, fx.count("pauseVideo"))
        assertTrue(fx.notices().contains(NoticeKind.TRACKING_LOST))
        assertEquals("暂停不能释放播放器，否则位置就丢了", 0, fx.count("releasePlayer"))
    }

    @Test
    fun `恢复跟踪续播`() {
        playing()
        c.onTracking(hit().photoId, false)
        fx.clear()
        c.onTracking(hit().photoId, true)
        assertEquals(ScanState.PLAYING, c.state)
        assertEquals(1, fx.count("playVideo"))
    }

    @Test
    fun `丢失跟踪不到上限不放弃`() {
        playing()
        c.onTracking(hit().photoId, false)
        clock.advance(ScanController.LOST_GIVEUP_MS)
        c.tick()
        assertEquals(ScanState.PAUSED, c.state)
    }

    @Test
    fun `丢失跟踪超过上限回到扫描`() {
        playing()
        c.onTracking(hit().photoId, false)
        clock.advance(ScanController.LOST_GIVEUP_MS + 1)
        c.tick()
        assertEquals(ScanState.SCANNING, c.state)
        assertEquals(1, fx.count("releaseTarget"))
        assertEquals(1, fx.count("releasePlayer"))
        assertNull(c.current)
    }

    @Test
    fun `目标装好但一直没找到图会回到扫描`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        clock.advance(ScanController.TARGET_FIND_TIMEOUT_MS + 1)
        c.tick()
        assertEquals(ScanState.SCANNING, c.state)
        assertTrue(fx.notices().contains(NoticeKind.TARGET_NOT_FOUND))
    }

    @Test
    fun `没找到图的上限比播过之后丢失的短`() {
        // 两个上限故意不一样：还没找到图时用户在「对准」，等久了不如早点放他重来；
        // 播过之后是「手挪开了」，放弃等于扔掉进度。这条测试把这层语义钉住 ——
        // 合成一个常量的话，两边必然有一边是错的。
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        clock.advance(ScanController.TARGET_FIND_TIMEOUT_MS)
        c.tick()
        assertEquals("到上限那一刻还不放弃", ScanState.TRACKING, c.state)

        val other = controllerWith(null)
        other.start()
        other.tick()
        val seq = fx.lastCaptureSeq!!
        other.onFrame(seq, ByteArray(1))
        other.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        other.onTargetLoaded(hit().photoId)
        other.onTracking(hit().photoId, true)
        other.onMedia(hit().photoId, media())
        other.onPlayerReady()
        other.onTracking(hit().photoId, false)
        clock.advance(ScanController.TARGET_FIND_TIMEOUT_MS + 1)
        other.tick()
        assertEquals("播过之后用的是更长的那个上限", ScanState.PAUSED, other.state)
    }

    // ---- 命中到出画的总预算 ----

    @Test
    fun `框在画面上但播放器一直不就绪会提示慢`() {
        // 这条是唯一由总预算兜住的洞：everTracked 之后 notTrackingSince 是 null，
        // tickWaitingForTracking 第一行就返回 —— 在它之前用户会对着一个空框无限等。
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onTracking(hit().photoId, true)
        fx.clear()
        clock.advance(ScanController.HIT_TO_PLAY_BUDGET_MS + 1)
        c.tick()
        assertTrue(NoticeKind.VIDEO_SLOW in fx.notices())
        assertEquals("提示而不放弃：照片还在镜头前，放弃只会立刻又命中", ScanState.TRACKING, c.state)
    }

    @Test
    fun `提示慢只说一次`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onTracking(hit().photoId, true)
        clock.advance(ScanController.HIT_TO_PLAY_BUDGET_MS + 1)
        c.tick()
        fx.clear()
        clock.advance(ScanController.HIT_TO_PLAY_BUDGET_MS)
        c.tick()
        assertFalse(NoticeKind.VIDEO_SLOW in fx.notices())
    }

    @Test
    fun `已经报过更具体的视频问题就不再提示慢`() {
        // 「视频播不了」已经说清了原因，再叠一句「有点慢」会把它盖掉。
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onTracking(hit().photoId, true)
        c.onMediaNotCached(hit().photoId)
        fx.clear()
        clock.advance(ScanController.HIT_TO_PLAY_BUDGET_MS + 1)
        c.tick()
        assertFalse(NoticeKind.VIDEO_SLOW in fx.notices())
    }

    @Test
    fun `播过之后暂停久了不提示慢`() {
        // PAUSED 是用户把手挪开了，不是加载慢。
        playing()
        c.onTracking(hit().photoId, false)
        fx.clear()
        clock.advance(ScanController.HIT_TO_PLAY_BUDGET_MS + 1)
        c.tick()
        assertFalse(NoticeKind.VIDEO_SLOW in fx.notices())
    }

    @Test
    fun `还没找到图时不提示慢`() {
        // 这一路有 TARGET_NOT_FOUND，那句话更准（说的是「再对准一下」）。
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        fx.clear()
        clock.advance(ScanController.TARGET_FIND_TIMEOUT_MS + 1)
        c.tick()
        assertFalse(NoticeKind.VIDEO_SLOW in fx.notices())
        assertTrue(NoticeKind.TARGET_NOT_FOUND in fx.notices())
    }

    @Test
    fun `用户主动退出回到扫描`() {
        playing()
        c.exitTarget()
        assertEquals(ScanState.SCANNING, c.state)
        assertEquals(1, fx.count("releaseTarget"))
    }

    @Test
    fun `回到扫描后立刻抽下一帧`() {
        playing()
        c.exitTarget()
        fx.clear()
        c.tick()
        assertEquals("刚回到扫描不该再等 400ms", 1, fx.count("captureFrame"))
    }

    // ---- 装载失败与拉黑 ----

    @Test
    fun `装载失败回到扫描并把这张照片短期拉黑`() {
        matchOnce()
        c.onTargetFailed(hit().photoId, "imgdb 与 thumb 都失败")
        assertEquals(ScanState.SCANNING, c.state)
        assertTrue(fx.notices().contains(NoticeKind.TARGET_LOAD_FAILED))

        // 立刻又命中同一张：必须当成未命中，否则「命中→失败→命中」死循环
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        assertEquals(ScanState.SCANNING, c.state)
    }

    @Test
    fun `拉黑到期后可以重新尝试同一张`() {
        matchOnce()
        c.onTargetFailed(hit().photoId)
        clock.advance(ScanController.BLOCKLIST_MS + 1)
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        assertEquals(ScanState.LOADING_TARGET, c.state)
    }

    @Test
    fun `装载超时按失败处理`() {
        matchOnce()
        clock.advance(ScanController.TARGET_LOAD_TIMEOUT_MS + 1)
        c.tick()
        assertEquals(ScanState.SCANNING, c.state)
        assertTrue(fx.notices().contains(NoticeKind.TARGET_LOAD_FAILED))
    }

    @Test
    fun `走了 thumb 兜底会提示`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId, usedThumbFallback = true)
        assertTrue(fx.notices().contains(NoticeKind.IMGDB_FALLBACK))
        assertEquals(ScanState.TRACKING, c.state)
    }

    // ---- 媒体相关的 §13 分支 ----

    @Test
    fun `视频文件不在 NAS 上时提示但继续跟踪`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onMedia(hit().photoId, media(missing = true))
        assertTrue(fx.notices().contains(NoticeKind.ASSET_MISSING))
        assertEquals(0, fx.count("preparePlayer"))
        c.onTracking(hit().photoId, true)
        assertEquals("没视频也要留着跟踪框", ScanState.TRACKING, c.state)
    }

    @Test
    fun `没有关联视频时同样提示`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onMedia(hit().photoId, media(url = null))
        assertTrue(fx.notices().contains(NoticeKind.ASSET_MISSING))
    }

    @Test
    fun `服务端不支持 Range 时提示禁用 seek`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onMedia(hit().photoId, media(supportsRange = false))
        assertTrue(fx.notices().contains(NoticeKind.NO_SEEK))
        assertTrue(fx.calls.contains("preparePlayer"))
    }

    @Test
    fun `参考图过期会提示但仍然识别`() {
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.Matched(hit(refStale = true)))
        assertTrue(fx.notices().contains(NoticeKind.REF_STALE))
        assertEquals(ScanState.LOADING_TARGET, c.state)
    }

    @Test
    fun `视频播不了时保留跟踪不崩`() {
        playing()
        fx.clear()
        c.onPlayerError("Source error 404")
        assertTrue(fx.notices().contains(NoticeKind.VIDEO_UNPLAYABLE))
        assertEquals(ScanState.TRACKING, c.state)
        // 不能又自动开播，否则 404 会被无限重试
        c.onTracking(hit().photoId, true)
        assertEquals(ScanState.TRACKING, c.state)
        assertEquals(0, fx.count("playVideo"))
    }

    @Test
    fun `取媒体信息失败只提示不改状态`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onMediaFailed(hit().photoId, "HTTP 500")
        assertTrue(fx.notices().contains(NoticeKind.VIDEO_UNPLAYABLE))
        assertEquals(ScanState.TRACKING, c.state)
    }

    @Test
    fun `迟到的媒体信息属于上一张照片时被忽略`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onMedia("b".repeat(32), media())
        assertEquals(0, fx.count("preparePlayer"))
    }

    // ---- 网络失败（§13）----

    @Test
    fun `连续三次识别失败提示网络慢并请求重新探活`() {
        c.start()
        repeat(2) { failOnce() }
        assertFalse(fx.notices().contains(NoticeKind.NETWORK_SLOW))
        failOnce()
        assertTrue(fx.notices().contains(NoticeKind.NETWORK_SLOW))
        assertEquals(1, fx.count("requestEndpointRefresh"))
        assertEquals("提示过就重新计数，不能每帧都刷", ScanState.SCANNING, c.state)
    }

    @Test
    fun `一次成功就把失败计数清零`() {
        c.start()
        repeat(2) { failOnce() }
        clock.advance(400)
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.NoMatch(null, 40))
        failOnce()
        assertFalse(fx.notices().contains(NoticeKind.NETWORK_SLOW))
    }

    @Test
    fun `token 不对时停止扫描而不是无限重试`() {
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognizeFailed(seq, NetErrorKind.UNAUTHORIZED, "需要 Bearer token")
        assertTrue(fx.notices().contains(NoticeKind.UNAUTHORIZED))
        assertEquals(ScanState.IDLE, c.state)
        fx.clear()
        clock.advance(10_000)
        c.tick()
        assertEquals(0, fx.count("captureFrame"))
    }

    @Test
    fun `停止后可以再启动`() {
        playing()
        c.stop()
        assertEquals(ScanState.IDLE, c.state)
        c.start()
        assertEquals(ScanState.SCANNING, c.state)
    }

    @Test
    fun `锁定目标后迟到的识别结果被忽略`() {
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        fx.clear()
        // 同一个 seq 的结果又来一次（重试逻辑出错时会发生）
        c.onRecognized(seq, RecognizeOutcome.Matched(hit("c".repeat(32))))
        assertEquals(hit().photoId, c.current?.photoId)
        assertEquals(0, fx.count("loadTarget:${"c".repeat(32)}"))
    }

    // ---- 不支持 ARCore 的兜底（§13）----

    @Test
    fun `不支持 ARCore 时命中后直接全屏播放`() {
        val fallback = ScanController(fx, clock, arAvailable = false)
        fallback.start()
        fallback.tick()
        val seq = fx.lastCaptureSeq!!
        fallback.onFrame(seq, ByteArray(1))
        fallback.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        assertEquals("没有目标库要装", 0, fx.count("loadTarget:${hit().photoId}"))
        assertEquals(ScanState.TRACKING, fallback.state)
        fallback.onMedia(hit().photoId, media())
        fallback.onPlayerReady()
        assertEquals("不需要等跟踪", ScanState.PLAYING, fallback.state)
    }

    @Test
    fun `不支持 ARCore 时不会因为没跟踪上而超时回扫描`() {
        val fallback = ScanController(fx, clock, arAvailable = false)
        fallback.start()
        fallback.tick()
        val seq = fx.lastCaptureSeq!!
        fallback.onFrame(seq, ByteArray(1))
        fallback.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        clock.advance(60_000)
        fallback.tick()
        assertEquals(ScanState.TRACKING, fallback.state)
    }

    @Test
    fun `全屏兜底播完回到扫描`() {
        val fallback = ScanController(fx, clock, arAvailable = false)
        fallback.start()
        fallback.tick()
        val seq = fx.lastCaptureSeq!!
        fallback.onFrame(seq, ByteArray(1))
        fallback.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        fallback.onMedia(hit().photoId, media())
        fallback.onPlayerReady()
        fallback.onPlaybackEnded()
        assertEquals(ScanState.SCANNING, fallback.state)
    }

    @Test
    fun `AR 模式下播完会续播`() {
        playing()
        fx.clear()
        c.onPlaybackEnded()
        assertEquals(1, fx.count("playVideo"))
        assertEquals(ScanState.PLAYING, c.state)
    }

    // ---- 离线命中（§15 / Phase 4）----

    @Test
    fun `扫描时 ARCore 认出缓存里的照片就是一次命中`() {
        // 这一条是「常扫照片离线可用」的全部：没有 recognize 请求，没有网络，
        // 光靠 session 里那份本地多图库就走完了识别。
        val local = controllerWith(hit())
        local.start()
        local.onTracking(hit().photoId, true)

        assertEquals(ScanState.TRACKING, local.state)
        assertEquals(1, fx.events.filterIsInstance<ScanEvent.Matched>().size)
        assertTrue(NoticeKind.LOCAL_HIT in fx.notices())
        assertEquals("视频还是要问的，只是可能问到缓存", 1, fx.count("fetchMedia:${hit().photoId}"))
    }

    @Test
    fun `离线命中不去装单张目标库`() {
        // 换库要 session.configure()，那会重置 session 把此刻正跟踪的这张图弄丢 ——
        // 于是刚认出来就立刻「丢失跟踪」。库已经在里面了，什么都不该做。
        val local = controllerWith(hit())
        local.start()
        local.onTracking(hit().photoId, true)
        assertEquals(0, fx.count("loadTarget:${hit().photoId}"))
    }

    @Test
    fun `离线命中不会误报认出来但没找到`() {
        val local = controllerWith(hit())
        local.start()
        local.onTracking(hit().photoId, true)
        fx.clear()
        clock.advance(30_000)
        local.tick()
        assertFalse(NoticeKind.TARGET_NOT_FOUND in fx.notices())
        assertEquals(ScanState.TRACKING, local.state)
    }

    @Test
    fun `离线命中后播放器一就绪就播`() {
        // 不用再等一次 onTracking：是 ARCore 先跟踪上才有这次命中的
        val local = controllerWith(hit())
        local.start()
        local.onTracking(hit().photoId, true)
        local.onMedia(hit().photoId, media())
        local.onPlayerReady()
        assertEquals(ScanState.PLAYING, local.state)
    }

    @Test
    fun `缓存里没有的照片不算命中扫描继续`() {
        // 库和索引理论上同步。真不同步时宁可继续走服务端那条路，
        // 也不要拿一条没有元数据的命中往下走。
        val local = controllerWith(null)
        local.start()
        local.onTracking(hit().photoId, true)
        assertEquals(ScanState.SCANNING, local.state)
        assertEquals(0, fx.events.filterIsInstance<ScanEvent.Matched>().size)
        clock.advance(400)
        local.tick()
        assertEquals("照旧抽帧问服务端", 1, fx.count("captureFrame"))
    }

    @Test
    fun `装库失败被拉黑的照片也不走离线命中`() {
        val local = controllerWith(hit())
        local.start()
        local.tick()
        val seq = fx.lastCaptureSeq!!
        local.onFrame(seq, ByteArray(1))
        local.onRecognized(seq, RecognizeOutcome.Matched(hit()))
        local.onTargetFailed(hit().photoId, "imgdb 版本不匹配")
        fx.clear()

        local.onTracking(hit().photoId, true)
        assertEquals("拉黑期内一样不认", ScanState.SCANNING, local.state)
        assertFalse(NoticeKind.LOCAL_HIT in fx.notices())
    }

    @Test
    fun `离线命中期间不再抽帧`() {
        val local = controllerWith(hit())
        local.start()
        local.onTracking(hit().photoId, true)
        fx.clear()
        clock.advance(10_000)
        local.tick()
        assertEquals(0, fx.count("captureFrame"))
    }

    @Test
    fun `离线命中的参考图过期照样提示`() {
        val local = controllerWith(hit(refStale = true))
        local.start()
        local.onTracking(hit().photoId, true)
        assertTrue(NoticeKind.REF_STALE in fx.notices())
    }

    @Test
    fun `视频没缓存时给出能自己解决的提示并继续跟踪`() {
        // 和 VIDEO_UNPLAYABLE 分开是刻意的：这条用户联网就好，那条不能
        val local = controllerWith(hit())
        local.start()
        local.onTracking(hit().photoId, true)
        local.onMediaNotCached(hit().photoId)
        assertTrue(NoticeKind.VIDEO_NOT_CACHED in fx.notices())
        assertEquals(ScanState.TRACKING, local.state)
    }

    @Test
    fun `已经在跟踪别的照片时不再接受新的离线命中`() {
        val local = controllerWith(hit())
        local.start()
        local.onTracking(hit().photoId, true)
        fx.clear()
        // 画面里同时进来另一张缓存里的照片
        local.onTracking("b".repeat(32), true)
        assertEquals(hit().photoId, local.current?.photoId)
        assertEquals(0, fx.events.filterIsInstance<ScanEvent.Matched>().size)
    }

    @Test
    fun `没跟踪上的报告不会触发离线命中`() {
        val local = controllerWith(hit())
        local.start()
        local.onTracking(hit().photoId, false)
        assertEquals(ScanState.SCANNING, local.state)
        local.onTracking(null, true)
        assertEquals(ScanState.SCANNING, local.state)
    }

    // ---- 辅助 ----

    /** 本地缓存里只有 [found] 这一条（null 表示缓存是空的）。 */
    private fun controllerWith(found: Hit?): ScanController = ScanController(
        fx,
        clock,
        arAvailable = true,
        localIndex = LocalIndex { id -> found?.takeIf { it.photoId == id } },
    )

    /** 走到 LOADING_TARGET。 */
    private fun matchOnce() {
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.Matched(hit()))
    }

    /** 走到 PLAYING。 */
    private fun playing() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        c.onMedia(hit().photoId, media())
        c.onPlayerReady()
        c.onTracking(hit().photoId, true)
        assertEquals(ScanState.PLAYING, c.state)
    }

    private fun failOnce() {
        clock.advance(400)
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognizeFailed(seq, NetErrorKind.TIMEOUT, "超时")
    }

    // ---- 凭证失效 → 回登录界面（Phase 5）----

    /**
     * 服务端从「一个预共享 token」换成用户体系之后，401 的最常见原因是**登录过期**
     * （管理员会话 12 小时、访客 30 天）。所以这条路不再是边角情况，是每天都会走到
     * 一次的事 —— 而它必须把用户送到能解决的地方，不能只提示一句。
     */
    @Test
    fun `识别拿到 401 时停止扫描并请求重新登录`() {
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognizeFailed(seq, NetErrorKind.UNAUTHORIZED, "会话过期")

        assertEquals(ScanState.IDLE, c.state)
        assertTrue(fx.notices().contains(NoticeKind.UNAUTHORIZED))
        assertEquals(1, fx.count("requestLogin"))
    }

    @Test
    fun `401 之后不再抽帧`() {
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognizeFailed(seq, NetErrorKind.UNAUTHORIZED)
        val before = fx.count("captureFrame")
        repeat(10) {
            clock.advance(400)
            c.tick()
        }
        assertEquals("停了就是停了，每 400ms 重试一次只会刷日志", before, fx.count("captureFrame"))
    }

    @Test
    fun `401 不会触发重新探活`() {
        // 探活要 1.5 秒，而这次失败与网络毫无关系。
        c.start()
        c.tick()
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognizeFailed(seq, NetErrorKind.UNAUTHORIZED)
        assertEquals(0, fx.count("requestEndpointRefresh"))
    }

    @Test
    fun `提示排在 stop 之前所以不会被 CLEARED 擦掉`() {
        // stop() 里的 setState 会在离开 SCANNING 时发一次 CLEARED（当提示已显示过）。
        // 反过来的顺序下，UNAUTHORIZED 会先被清掉再显示，或者干脆看不到。
        c.start()
        clock.advance(ScanController.AIM_HINT_MS)
        c.tick() // 先触发一次 AIM_AT_PHOTO，让 aimNoticeShown 成立
        assertTrue(fx.notices().contains(NoticeKind.AIM_AT_PHOTO))
        fx.clear()

        c.onUnauthorized("过期了")
        val notices = fx.notices()
        assertEquals(
            "UNAUTHORIZED 必须排在 CLEARED 前面",
            listOf(NoticeKind.UNAUTHORIZED, NoticeKind.CLEARED),
            notices,
        )
    }

    @Test
    fun `并发的几条 401 只报一次`() {
        // 扫描时每 400ms 一个请求，几条在飞的请求会同时拿到 401。逐条报就是一串重复
        // 提示加一串导航请求。
        c.start()
        repeat(5) { c.onUnauthorized("过期了") }
        assertEquals(1, fx.notices().count { it == NoticeKind.UNAUTHORIZED })
        assertEquals(1, fx.count("requestLogin"))
    }

    @Test
    fun `重新登录后再开扫，下一次 401 会重新报`() {
        c.start()
        c.onUnauthorized()
        fx.clear()
        c.start() // 用户重新登录了，回来继续扫
        c.onUnauthorized()
        assertEquals(1, fx.notices().count { it == NoticeKind.UNAUTHORIZED })
        assertEquals(1, fx.count("requestLogin"))
    }

    @Test
    fun `已经停了之后迟到的 401 不会把状态弄乱`() {
        c.start()
        c.onUnauthorized()
        assertEquals(ScanState.IDLE, c.state)
        c.onUnauthorized() // 一条迟到的请求回来
        assertEquals(ScanState.IDLE, c.state)
    }

    @Test
    fun `锁住某张照片时拿到 401 也要退出并请求登录`() {
        // imgdb 与 media 那两条路和识别用同一个 token，所以 401 会在 TRACKING /
        // PLAYING 期间冒出来。此前它们分别报「目标装不上」「视频播不了」—— 那两句
        // 提示会让人去查照片和 NAS 上的视频文件。
        playing()
        fx.clear()
        c.onUnauthorized("imgdb 401")
        assertEquals(ScanState.IDLE, c.state)
        assertTrue(fx.notices().contains(NoticeKind.UNAUTHORIZED))
        assertEquals(1, fx.count("requestLogin"))
        // 退出时该释放的东西照样释放
        assertTrue(fx.calls.contains("releasePlayer"))
        assertTrue(fx.calls.contains("releaseTarget"))
        assertNull(c.current)
    }

    // ---- 端上提特征回退（Phase 5）----

    @Test
    fun `端上提特征回退只提示一句，不动状态机`() {
        // 回退之后功能完全一样（只是慢一点）。把它算进 netFailures 会触发一次没必要
        // 的重新探活（1.5 秒），而网络根本没问题。
        c.start()
        c.tick()
        val before = c.state
        c.onFeatureFallback("取不到端上模型，已改回上传整帧识别。")

        assertEquals(before, c.state)
        assertEquals(0, fx.count("requestEndpointRefresh"))
        assertEquals(0, fx.count("requestLogin"))
        val notice = fx.events.filterIsInstance<ScanEvent.Notice>()
            .last { it.kind == NoticeKind.FEATURES_FALLBACK }
        assertTrue(notice.detail!!.contains("改回上传整帧"))
    }

    @Test
    fun `回退之后扫描继续`() {
        c.start()
        c.tick()
        // 走完一轮（抽帧 → 识别 → 未命中），状态机才回到「可以抽下一帧」
        val seq = fx.lastCaptureSeq!!
        c.onFrame(seq, ByteArray(1))
        c.onRecognized(seq, RecognizeOutcome.NoMatch(null, 5))

        c.onFeatureFallback("推理出错")
        val before = fx.count("captureFrame")
        clock.advance(400)
        c.tick()
        assertEquals("扫描不能因为回退而停", before + 1, fx.count("captureFrame"))
    }

    // ---- 预建离线库装不上（Phase 6）----

    @Test
    fun `预建库装不上只报一句提示，不动状态机`() {
        // 离线识别还在（退回端上现建那份），只是降了一档。把它算进 netFailures 会触发
        // 一次没必要的重新探活，而这件事发生在装库那一步 —— 一个字节都没走网。
        c.start()
        c.tick()
        val before = c.state
        val captures = fx.count("captureFrame")

        c.onTargetsDbFallback("版本不匹配")

        assertEquals(before, c.state)
        assertEquals(0, fx.count("requestEndpointRefresh"))
        assertEquals(0, fx.count("requestLogin"))
        assertEquals("不该影响抽帧节奏", captures, fx.count("captureFrame"))
        val notice = fx.events.filterIsInstance<ScanEvent.Notice>()
            .last { it.kind == NoticeKind.TARGETS_DB_FALLBACK }
        assertEquals("版本不匹配", notice.detail)
    }

    @Test
    fun `预建库装不上之后离线命中照样成立`() {
        // 退回端上现建那份之后，那份库里的照片仍然要能离线认出来 —— 否则这条回退
        // 路径等于「离线识别没了」，而它的全部意义就是不让那件事发生。
        val local = controllerWith(hit())
        local.start()
        local.onTargetsDbFallback("版本不匹配")
        local.onTracking(hit().photoId, true)
        assertEquals(ScanState.TRACKING, local.state)
        assertTrue(NoticeKind.LOCAL_HIT in fx.notices())
    }
}

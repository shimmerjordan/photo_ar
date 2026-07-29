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
    fun `丢失跟踪不到 10 秒不放弃`() {
        playing()
        c.onTracking(hit().photoId, false)
        clock.advance(10_000)
        c.tick()
        assertEquals(ScanState.PAUSED, c.state)
    }

    @Test
    fun `丢失跟踪超过 10 秒回到扫描`() {
        playing()
        c.onTracking(hit().photoId, false)
        clock.advance(10_001)
        c.tick()
        assertEquals(ScanState.SCANNING, c.state)
        assertEquals(1, fx.count("releaseTarget"))
        assertEquals(1, fx.count("releasePlayer"))
        assertNull(c.current)
    }

    @Test
    fun `目标装好但一直没找到图 10 秒后回到扫描`() {
        matchOnce()
        c.onTargetLoaded(hit().photoId)
        clock.advance(10_001)
        c.tick()
        assertEquals(ScanState.SCANNING, c.state)
        assertTrue(fx.notices().contains(NoticeKind.TARGET_NOT_FOUND))
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

    // ---- 辅助 ----

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
}

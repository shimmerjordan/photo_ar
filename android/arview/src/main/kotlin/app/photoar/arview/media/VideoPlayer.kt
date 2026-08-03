package app.photoar.arview.media

import android.content.Context
import android.view.Surface
import android.view.SurfaceView
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.VideoSize
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
import app.photoar.arview.Endpoints

/**
 * ExoPlayer 的薄封装。只暴露状态机需要的动作（准备 / 播 / 暂停 / 放开），
 * 以及三个回调。
 *
 * 鉴权走 HTTP 头而不是 URL 里的 query token：媒体 URL 会被 ExoPlayer 记进
 * 内部缓存键、也会出现在日志里，token 跟着到处跑不合适。
 */
class VideoPlayer(
    private val context: Context,
    private val endpoints: () -> Endpoints,
    private val onReady: (durationMs: Long) -> Unit,
    private val onEnded: () -> Unit,
    private val onError: (message: String) -> Unit,
    private val onVideoSize: (width: Int, height: Int) -> Unit,
) {

    private var player: ExoPlayer? = null

    /**
     * AR 模式下循环播放，全屏兜底模式播完就结束。
     *
     * 由 [ScanRuntime] 按 `arAvailable` 设一次。两种模式必须不同：
     *
     * - **AR 模式**：视频贴在照片上，用户举着手机看多久就该放多久，播完停住只会
     *   留一帧静止画面在照片上，看起来像卡死了。
     * - **全屏兜底**：`ScanController.onPlaybackEnded` 靠"播完"这个事件退回扫描，
     *   那是它**唯一**的出口。这边开循环就再也回不去扫描了。
     */
    var looping: Boolean = false
        set(value) {
            field = value
            player?.repeatMode = repeatMode()
        }

    private fun repeatMode(): Int =
        if (looping) Player.REPEAT_MODE_ONE else Player.REPEAT_MODE_OFF

    private val listener = object : Player.Listener {
        override fun onPlaybackStateChanged(state: Int) {
            val p = player ?: return
            when (state) {
                Player.STATE_READY -> onReady(
                    if (p.duration > 0) p.duration else 0L
                )
                Player.STATE_ENDED -> onEnded()
                else -> Unit
            }
        }

        override fun onPlayerError(error: PlaybackException) {
            onError(error.errorCodeName + "：" + (error.message ?: ""))
        }

        override fun onVideoSizeChanged(videoSize: VideoSize) {
            if (videoSize.width > 0 && videoSize.height > 0) {
                onVideoSize(videoSize.width, videoSize.height)
            }
        }
    }

    // DefaultHttpDataSource / DefaultMediaSourceFactory 在 media3 里标了
    // @UnstableApi。这里显式 opt-in 而不是把整个类标成 unstable，否则要求
    // 会一路传染到 ScanRuntime 和 Activity。
    @OptIn(UnstableApi::class)
    private fun ensure(): ExoPlayer {
        player?.let { return it }
        val http = DefaultHttpDataSource.Factory()
            .setAllowCrossProtocolRedirects(true)
            .setConnectTimeoutMs(6_000)
            .setReadTimeoutMs(10_000)
            .setDefaultRequestProperties(
                mapOf("Authorization" to "Bearer ${endpoints().token}")
            )
        val p = ExoPlayer.Builder(context)
            .setMediaSourceFactory(DefaultMediaSourceFactory(http))
            .build()
        p.addListener(listener)
        p.repeatMode = repeatMode()
        p.playWhenReady = false
        player = p
        return p
    }

    /** 渲染到外部纹理（AR 模式）。 */
    fun attach(surface: Surface) {
        ensure().setVideoSurface(surface)
    }

    /** 渲染到普通 SurfaceView（无 ARCore 的全屏兜底）。 */
    fun attach(view: SurfaceView) {
        ensure().setVideoSurfaceView(view)
    }

    fun prepare(url: String) {
        val p = ensure()
        p.setMediaItem(MediaItem.fromUri(url))
        p.prepare()
    }

    fun play() {
        val p = player ?: return
        // 播完之后 `play()` **什么都不会发生** —— ExoPlayer 停在 STATE_ENDED，播放
        // 位置就在末尾，不会自己回头。要先 seek。
        //
        // 这个隐病一直在：`ScanController.onPlaybackEnded` 在 AR 模式下调的正是
        // `fx.playVideo()`，本意是"循环"，实际是无事发生。它被两件事一起盖住了 ——
        // 那段代码的注释写着「AR 模式下播放器是循环的，正常不会走到这里」，而播放器
        // 那边偏偏设的是 REPEAT_MODE_OFF。两处各自看都像对的。
        //
        // 现在 AR 模式走 REPEAT_MODE_ONE（无缝，也不再触发 ENDED），这里是兜底；
        // 但兜底必须真的能兜住，否则下次又是一个"看起来有、实际没有"的循环。
        if (p.playbackState == Player.STATE_ENDED) p.seekTo(0)
        p.play()
    }

    fun pause() {
        player?.pause()
    }

    /**
     * 回到开头重放。
     *
     * 不用重新 [prepare]：那会丢掉已经缓冲的数据、重新建一次 HTTP 连接，十几秒的
     * 视频等于整段重新拉一遍。中间那一两秒是 buffering，[isPlaying] 为 false，
     * 界面上的播放按钮会退回「播放」，看着像按了没反应（实测过）。
     */
    fun restart() {
        val p = player ?: return
        p.seekTo(0)
        p.play()
    }

    /** 幂等：状态机的多条路径都会调它，且不保证之前 prepare 过。 */
    fun release() {
        val p = player ?: return
        player = null
        p.removeListener(listener)
        p.release()
    }

    /** 只在没有 Range 支持时用（§7 的 `supportsRange=false`）：禁掉拖动。 */
    val positionMs: Long get() = player?.currentPosition ?: 0L

    val isPlaying: Boolean get() = player?.isPlaying == true

    /**
     * 「要它播」而不是「此刻画面在动」。
     *
     * [isPlaying] 在缓冲和 seek 期间是 false，拿它驱动播放/暂停按钮，就会在缓冲那
     * 一两秒把按钮翻成「播放」—— 按钮该跟着用户的意图走，卡顿归进度条去表达。
     */
    val wantsToPlay: Boolean get() = player?.playWhenReady == true
}

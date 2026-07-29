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

    /** 播完不自动重播；重播由状态机决定（§11.6）。 */
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
        p.repeatMode = Player.REPEAT_MODE_OFF
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
        player?.play()
    }

    fun pause() {
        player?.pause()
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
}

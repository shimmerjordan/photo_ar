package app.photoar.arview.ar

import android.app.Activity
import android.graphics.Bitmap
import android.util.Log
import android.util.Size
import app.photoar.arview.Frames
import com.google.ar.core.AugmentedImage
import com.google.ar.core.AugmentedImageDatabase
import com.google.ar.core.CameraConfig
import com.google.ar.core.CameraConfigFilter
import com.google.ar.core.Config
import com.google.ar.core.Frame
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.CameraNotAvailableException
import com.google.ar.core.exceptions.UnavailableException
import java.io.ByteArrayInputStream
import java.util.EnumSet

/**
 * ARCore 会话的生命周期与配置。
 *
 * §11.7 的红利：印刷尺寸在入库时就知道，服务端 `arcoreimg build-db` 已经把物理
 * 宽度烘进了 `.imgdb`（见 quality.build_single_target_db），所以客户端
 * `deserialize` 出来的库自带正确尺寸 —— 四边形一上来就是对的大小，也不会在跟踪
 * 过程中忽大忽小。目标名就是 photoId。
 *
 * 只有 `.imgdb` 下不来时才退回 [loadTargetFromBitmap]，用缩略图在端上现算特征。
 */
class ArSessionHolder(private val activity: Activity) {

    companion object {
        private const val TAG = "ArSession"

        /**
         * 反序列化一份 `.imgdb`。**全工程唯一一处** `deserialize`。
         *
         * 单目标（服务端入库时给那张照片建的那份）与整库多目标（`GET /v1/targets/db`
         * 下来的那份）是同一个 ARCore API，也是同一个坑：服务端的 `arcoreimg` 比端上的
         * ARCore 新时它会抛。抄第二份的后果是两条路各自长出不同的错误处理，然后只有
         * 一条记得退回去 —— 而漏掉的那条表现为「离线识别静默消失」。
         *
         * 只要 [Session] 拿原生上下文，**不碰 `configure()`**，所以能在后台线程调 ——
         * 整库那条路正是先在后台线程上试装一次，确认能装才排到 GL 线程去 configure
         * （见 [LocalTargetDb.prepare]）。
         */
        fun deserializeDb(session: Session, bytes: ByteArray): AugmentedImageDatabase =
            ByteArrayInputStream(bytes).use { AugmentedImageDatabase.deserialize(session, it) }
    }

    var session: Session? = null
        private set

    /** GL 线程分配好相机纹理后写进来；会话每次 resume 前都要重设一遍。 */
    var cameraTextureId: Int = -1

    /** 当前注册的单张目标 photoId，null 表示没有单张目标。 */
    var loadedPhotoId: String? = null
        private set

    /**
     * session 里装的是本地多图库（Phase 4，[LocalTargetDb]）。
     *
     * 这时候 [trackedImage] 认**任何**一张 —— 扫描阶段谁进画面都算命中，
     * 而不是像单张目标那样只认指定的那个 photoId。
     */
    var multiImageLoaded = false
        private set

    /**
     * ARCore 实际给出的 CPU 图像尺寸（[CameraConfig.getImageSize]），会话建成后才有值。
     *
     * 暴露出来是为了能验证 [applyCameraConfig] 到底生效没有：识别率的主导变量是
     * 「照片在送去识别的那帧里占了多少像素」，而这个尺寸就是它的上限。真机上扫不出来
     * 时，先看这个值 —— 是 640x480 就说明档位没挑上，跟阈值、跟照片都没关系。
     */
    var cpuImageSize: Size? = null
        private set

    private var paused = true

    /**
     * @return 失败原因，成功返回 null。
     *
     * 传的不一定是 `activity` 本身：走内嵌运行时那条路时，
     * [ArCoreEmbeddedRuntime.sessionContext] 会包一层只 override `getClassLoader()`
     * 的 wrapper，把 native 用来问「系统装了 ARCore 哪一版」的那个类换掉 ——
     * 不换就恒定拿到 -1，然后 `AR_UNAVAILABLE_ARCORE_NOT_INSTALLED`。
     * 系统已装够新的那份时它原样返回 `activity`，原生路径一点不动。
     */
    fun create(): String? {
        if (session != null) return null
        return try {
            val s = Session(ArCoreEmbeddedRuntime.sessionContext(activity))
            applyCameraConfig(s)
            s.configure(baseConfig(s, null))
            session = s
            null
        } catch (e: UnavailableException) {
            e.message ?: e.javaClass.simpleName
        } catch (e: Throwable) {
            e.message ?: e.javaClass.simpleName
        }
    }

    fun resume(): Boolean {
        val s = session ?: return false
        if (!paused) return true // 重复 resume 会抛，onResume 与 onGlReady 都可能到这里
        return try {
            if (cameraTextureId >= 0) s.setCameraTextureName(cameraTextureId)
            s.resume()
            paused = false
            true
        } catch (e: CameraNotAvailableException) {
            Log.w(TAG, "相机被别的 App 占着", e)
            false
        }
    }

    fun pause() {
        if (paused) return
        paused = true
        session?.pause()
    }

    fun destroy() {
        pause()
        session?.close()
        session = null
        loadedPhotoId = null
        multiImageLoaded = false
    }

    /**
     * 装载本地多图库（Phase 4）。库由 [LocalTargetDb] 建好，这里只负责 configure ——
     * 会话配置的策略（关平面、关深度、BLOCKING）只在 [baseConfig] 一个地方。
     *
     * @return 失败原因，成功返回 null。
     */
    fun loadLocalDb(db: AugmentedImageDatabase): String? {
        val s = session ?: return "会话不存在"
        return try {
            s.configure(baseConfig(s, db))
            loadedPhotoId = null
            multiImageLoaded = true
            null
        } catch (e: Throwable) {
            multiImageLoaded = false
            "本地库装载失败：${e.message ?: e.javaClass.simpleName}"
        }
    }

    /**
     * 装载服务端预建的 `.imgdb`。
     *
     * `configure()` 会把会话内部状态重置一下，这一瞬间 `update()` 可能返回没有
     * 任何可跟踪对象的帧 —— 状态机因此专门有个 LOADING_TARGET 状态（§11），
     * 在这期间不把「没跟踪到」当成丢失。
     *
     * @return 失败原因，成功返回 null。
     */
    fun loadTargetFromImgdb(photoId: String, imgdb: ByteArray): String? {
        val s = session ?: return "会话不存在"
        return try {
            val db = deserializeDb(s, imgdb)
            s.configure(baseConfig(s, db))
            loadedPhotoId = photoId
            // 单张目标把多图库顶掉了：ARCore 一个 session 只有一个库
            multiImageLoaded = false
            null
        } catch (e: Throwable) {
            // 版本不匹配（服务端的 arcoreimg 比端上的 ARCore 新）会走到这里
            loadedPhotoId = null
            "imgdb 装载失败：${e.message ?: e.javaClass.simpleName}"
        }
    }

    /**
     * `.imgdb` 拿不到时的降级：用参考缩略图在端上现算特征。
     *
     * @param widthM 打印物理宽度；**0 或非正 = 未知**，此时走不带宽度的注册，由 ARCore
     *   自己量（见下面那段）。
     */
    fun loadTargetFromBitmap(photoId: String, bitmap: Bitmap, widthM: Float): String? {
        val s = session ?: return "会话不存在"
        return try {
            val db = AugmentedImageDatabase(s)
            // 带物理宽度注册，等价于 .imgdb 里烘好的那个值（§11.7）。
            //
            // 宽度未知时**不能传 0**：ARCore 会当真，按 0 米宽算位姿，结果是废的。
            // 不带宽度是它专门支持的用法 —— 自己从 SLAM 量出物理尺寸，代价是要用户
            // 稍微动一下手机才收敛，好处是量出来的是真值而不是我们的猜测。
            if (widthM > 0f) {
                db.addImage(photoId, bitmap, widthM)
            } else {
                db.addImage(photoId, bitmap)
            }
            s.configure(baseConfig(s, db))
            loadedPhotoId = photoId
            multiImageLoaded = false
            null
        } catch (e: Throwable) {
            // 最常见的是图片特征太少（纯色、严重模糊），ARCore 直接拒收
            loadedPhotoId = null
            "缩略图注册失败：${e.message ?: e.javaClass.simpleName}"
        }
    }

    /**
     * 清空单张目标。
     *
     * **Phase 4 起调用方紧接着要把本地多图库装回来**（[LocalTargetDb.reinstall]）——
     * 这里只把库清空，离线识别就跟着没了，而且是静默没的。
     */
    fun clearTarget() {
        val s = session ?: return
        if (loadedPhotoId == null) return
        loadedPhotoId = null
        multiImageLoaded = false
        try {
            s.configure(baseConfig(s, null))
        } catch (e: Throwable) {
            Log.w(TAG, "清空目标失败", e)
        }
    }

    /**
     * 一帧里那张目标图，连同「这一帧是不是真看见了它」。
     *
     * @param full `true` = FULL_TRACKING，ARCore 这一帧真的在图上量到了位姿；
     *   `false` = LAST_KNOWN_POSE，图当前认不出来，位姿是靠**世界跟踪**推出来的。
     */
    data class Tracked(val image: AugmentedImage, val full: Boolean)

    /**
     * 当前帧里那张目标图。TRACKING 状态下两种 trackingMethod 都返回，用 [Tracked.full]
     * 区分 —— 用哪个、用多久由渲染层的滑行窗口决定（见 `ArRenderer.COAST_MS`）。
     *
     * 装的是单张目标就只认那一个 photoId；装的是本地多图库（扫描阶段）就认任何一张 ——
     * 那正是离线命中的入口。同时有 FULL 和 LAST_KNOWN 时优先给 FULL。
     *
     * ## 为什么不再只认 FULL_TRACKING
     *
     * 这里原来只认 FULL_TRACKING，理由是「PAUSED 时 ARCore 仍会用上次的位姿继续报，
     * 拿它贴视频会贴在空气上」。那个担心对 **PAUSED** 是对的（下面照旧挡掉），但
     * 对 LAST_KNOWN_POSE 是错的，而这两件事被混成了一条判断：
     *
     * LAST_KNOWN_POSE 的语义是「这一帧图案匹配不上，但我用 SLAM 知道它在哪」。
     * 照片钉在墙上不动，所以只要相机的世界跟踪还正常，这个位姿就是**对的** ——
     * 它本来就是 ARCore 为这种情况设计的输出。而「图案匹配不上」在斜视时几乎必然
     * 发生（透视压缩 + 高光），于是原来那条判断把**大角度**一律当成丢失：视频暂停、
     * 弹一次 TRACKING_LOST，用户看到的就是「角度大一点就丢目标」。
     *
     * 剩下的风险（照片被拿走了、世界跟踪自己漂了）由渲染层的两道闸挡：滑行有时限，
     * 且只在 `frame.camera.trackingState == TRACKING` 时滑行。
     */
    fun trackedImage(frame: Frame): Tracked? {
        val want = loadedPhotoId
        if (want == null && !multiImageLoaded) return null
        var lastKnown: AugmentedImage? = null
        for (img in frame.getUpdatedTrackables(AugmentedImage::class.java)) {
            if (want != null && img.name != want) continue
            // PAUSED / STOPPED 照旧挡掉：那才是「见过但现在不在画面里」。
            if (img.trackingState != TrackingState.TRACKING) continue
            when (img.trackingMethod) {
                AugmentedImage.TrackingMethod.FULL_TRACKING ->
                    return Tracked(img, full = true)
                AugmentedImage.TrackingMethod.LAST_KNOWN_POSE ->
                    if (lastKnown == null) lastKnown = img
                // NOT_TRACKING：状态说 TRACKING、方法说没跟上，自相矛盾。当没有。
                else -> Unit
            }
        }
        return lastKnown?.let { Tracked(it, full = false) }
    }

    /**
     * 把 ARCore 的 **CPU 图像**档位挑到长边 ≥ [Frames.LONG_EDGE]。
     *
     * 为什么必须显式挑：ARCore 默认给的 CPU 图像是 **640x480**，而
     * `frame.acquireCameraImage()` 拿到的就是它 —— 送去识别的那帧的像素上限。
     * [Frames.targetSize] 绝不放大、[app.photoar.arview.camera.FrameGrabber] 也不缩放，
     * 所以默认档位一路封到底：服务端把查询侧特征预算抬到 4000
     * （`backend.QUERY_N_FEATURES`）在 640x480 的帧上根本提不出那么多有效特征，
     * 等于白算。实测这一档「一个种子都不全过」，也就是真机扫不出来的那一行。
     *
     * `Frames.LONG_EDGE` 只被 Camera2 兜底路径用到，改那个常量对**有** ARCore 的机型
     * （也就是绝大多数用户）没有任何作用 —— 这一段才是 AR 路径上对应的那一半。
     *
     * 挑不到更大的档位就保持默认，但**必须留日志**：静默停在 640x480 的话，现象是
     * 「怎么优化都扫不出来」，而日志里什么都看不到。
     */
    private fun applyCameraConfig(s: Session) {
        val chosen = runCatching {
            // 30 **和** 60 都要问。
            //
            // 这里原来只问 TARGET_FPS_30，于是所有 60fps 档位在查询阶段就被过滤掉了 ——
            // 后面挑得再仔细也只能在 30fps 里挑。而帧率是这个 App 里少见的「纯赚」项：
            // ARCore 的跟踪更新率就是相机帧率（`baseConfig` 里 updateMode = BLOCKING，
            // 渲染跟着相机走），翻倍等于位姿更新翻倍、斜视掉帧后回来的时间减半，而代价
            // 只是耗电和发热 —— 而这个场景是手持几十秒看一段短视频，本来就不需要省电。
            //
            // 不写 EnumSet.allOf：TargetFps 里就这两个值，但 allOf 会让「新版本加了
            // TARGET_FPS_120 就自动启用」这件事变成隐式的。要启用就显式加一行。
            val filter = CameraConfigFilter(s)
                .setTargetFps(
                    EnumSet.of(
                        CameraConfig.TargetFps.TARGET_FPS_30,
                        CameraConfig.TargetFps.TARGET_FPS_60,
                    )
                )
                // baseConfig 里 depthMode 是 DISABLED，带深度传感器的档位只会白耗电
                .setDepthSensorUsage(EnumSet.of(CameraConfig.DepthSensorUsage.DO_NOT_USE))
            val configs = s.getSupportedCameraConfigs(filter)
            if (configs.isEmpty()) return@runCatching null
            val want = Frames.pickCameraOption(
                configs.map {
                    Frames.CameraOption(
                        size = Frames.Size(it.imageSize.width, it.imageSize.height),
                        maxFps = it.fpsRange.upper,
                    )
                },
            ) ?: return@runCatching null
            // 反查回 CameraConfig：setCameraConfig 只接受 getSupportedCameraConfigs
            // 原样返回的对象，自己 new 一个会抛。
            configs.firstOrNull {
                it.imageSize.width == want.size.width &&
                    it.imageSize.height == want.size.height &&
                    it.fpsRange.upper == want.maxFps
            }
        }.getOrElse {
            Log.w(TAG, "查询相机档位失败，保持默认 CPU 图像尺寸", it)
            null
        }
        if (chosen == null) {
            Log.w(TAG, "没挑到长边 ≥ ${Frames.LONG_EDGE} 的 CPU 图像档位，保持默认（识别率会明显偏低）")
        } else {
            runCatching { s.setCameraConfig(chosen) }
                .onFailure { Log.w(TAG, "设置相机档位失败，保持默认", it) }
        }
        cpuImageSize = runCatching { s.cameraConfig.imageSize }.getOrNull()
        // 帧率也打出来。不打的话「到底跑没跑到 60」在真机上无从判断 —— 而这两个数
        // 恰好是一对取舍（见 Frames.pickCameraOption），只看一个会误判成另一个的问题。
        val fps = runCatching { s.cameraConfig.fpsRange }.getOrNull()
        Log.i(
            TAG,
            "ARCore CPU 图像尺寸 = $cpuImageSize（期望长边 ${Frames.LONG_EDGE}）｜帧率 = $fps",
        )
    }

    private fun baseConfig(s: Session, db: AugmentedImageDatabase?): Config =
        Config(s).apply {
            // 只要贴图，不要平面、不要深度、不要光照估计 —— 这部分是手机在算，
            // 能省一点省一点
            planeFindingMode = Config.PlaneFindingMode.DISABLED
            lightEstimationMode = Config.LightEstimationMode.DISABLED
            depthMode = Config.DepthMode.DISABLED
            // 照片是平的、距离几十厘米，自动对焦必须开，定焦会一直糊
            focusMode = Config.FocusMode.AUTO
            // BLOCKING：让 update() 卡到有新相机帧为止，GLSurfaceView 的连续渲染
            // 就被相机帧率（30fps）自然限住，而不是 60fps 空转重画同一帧。
            updateMode = Config.UpdateMode.BLOCKING
            // setter 不接受 null；新建的 Config 本来就没有库，所以「清空」＝不设。
            if (db != null) augmentedImageDatabase = db
        }
}

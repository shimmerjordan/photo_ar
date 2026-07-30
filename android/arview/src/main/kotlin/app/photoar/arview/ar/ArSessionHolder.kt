package app.photoar.arview.ar

import android.app.Activity
import android.graphics.Bitmap
import android.util.Log
import com.google.ar.core.AugmentedImage
import com.google.ar.core.AugmentedImageDatabase
import com.google.ar.core.Config
import com.google.ar.core.Frame
import com.google.ar.core.Session
import com.google.ar.core.TrackingState
import com.google.ar.core.exceptions.CameraNotAvailableException
import com.google.ar.core.exceptions.UnavailableException
import java.io.ByteArrayInputStream

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

    private companion object {
        const val TAG = "ArSession"
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

    private var paused = true

    /** @return 失败原因，成功返回 null。 */
    fun create(): String? {
        if (session != null) return null
        return try {
            val s = Session(activity)
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
            val db = ByteArrayInputStream(imgdb).use {
                AugmentedImageDatabase.deserialize(s, it)
            }
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

    /** `.imgdb` 拿不到时的降级：用参考缩略图在端上现算特征。 */
    fun loadTargetFromBitmap(photoId: String, bitmap: Bitmap, widthM: Float): String? {
        val s = session ?: return "会话不存在"
        return try {
            val db = AugmentedImageDatabase(s)
            // 带物理宽度注册，等价于 .imgdb 里烘好的那个值（§11.7）
            db.addImage(photoId, bitmap, widthM)
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
     * 当前帧里那张目标图，只在 FULL_TRACKING 时返回。
     *
     * 装的是单张目标就只认那一个 photoId；装的是本地多图库（扫描阶段）就认任何一张 ——
     * 那正是离线命中的入口。
     */
    fun trackedImage(frame: Frame): AugmentedImage? {
        val want = loadedPhotoId
        if (want == null && !multiImageLoaded) return null
        for (img in frame.getUpdatedTrackables(AugmentedImage::class.java)) {
            if (want != null && img.name != want) continue
            // PAUSED 表示「见过但现在不在画面里」，ARCore 仍会用上次的位姿继续
            // 报，拿它贴视频会贴在空气上。所以只认 TRACKING + FULL_TRACKING。
            if (img.trackingState == TrackingState.TRACKING &&
                img.trackingMethod == AugmentedImage.TrackingMethod.FULL_TRACKING
            ) {
                return img
            }
        }
        return null
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

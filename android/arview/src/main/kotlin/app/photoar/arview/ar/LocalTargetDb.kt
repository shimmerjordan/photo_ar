package app.photoar.arview.ar

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import app.photoar.arview.cache.CacheSync
import app.photoar.arview.cache.CachedPhoto
import app.photoar.arview.cache.PhotoCache
import com.google.ar.core.AugmentedImageDatabase
import com.google.ar.core.Session
import java.io.ByteArrayInputStream
import java.io.File
import java.io.FileOutputStream

/**
 * 本地多图库：把缓存里那 200 张缩略图建成一个 ARCore `AugmentedImageDatabase`，
 * 序列化到 `local.imgdb`，扫描开始时装进 session（§11.3 / Phase 4）。
 *
 * 建库为什么和「装库」分开、为什么不在同步时就建 —— 见下面两段。
 *
 * ## 建库需要 Session，而同步时可能没有
 *
 * `AugmentedImageDatabase(session)` 的构造要一个 [Session]，而「缓存管理」页点
 * 「现在同步」时相机根本没开，也可能连相机权限都还没给。为此单独建一个 Session
 * 只为了建库是错的：那要相机权限、要 ARCore 装着，而这两件事跟「把文件下下来」
 * 毫无关系。
 *
 * 所以顺序反过来：同步只负责把缩略图下齐，然后调 [invalidate] 把库标成「过期」；
 * 真正建库发生在**下一次扫描启动时**（[ensureInstalled]），那时候 session 一定在。
 * 代价是刚同步完的第一次扫描要多花几百毫秒建库 —— 一次性的，且发生在用户举起
 * 手机对准照片之前的那段时间里。
 *
 * ## 过期判定用文件时间而不是另存一个标记
 *
 * `local.imgdb` 比 `index.json` 旧就是过期。多一个「dirty 标记文件」意味着多一个
 * 会和现实不一致的状态 —— 标记写成功而库没写成功、或者反过来，都会让离线识别
 * 静默失效。文件 mtime 是内核维护的，不会漏。
 */
class LocalTargetDb(private val cache: PhotoCache) {

    private companion object {
        const val TAG = "LocalTargetDb"

        /**
         * 解码后的长边上限，与 [TargetLoader.THUMB_MAX_EDGE] 一致（640）。
         *
         * 不特意调小：`addImage` 的特征质量直接决定离线跟踪稳不稳，而这一步的耗时
         * 只在建库时付一次。
         */
        const val MAX_EDGE = 640

        /**
         * 一次建库最多喂多少张。
         *
         * ARCore 官方对库容量的建议上限是 1000 张，而 `CacheSpec.maxPhotos` 默认 200，
         * 正常到不了这里。留着是因为「缓存条数」是用户可调的（§5.8），而 addImage
         * 每张几十毫秒 —— 1000 张就是半分钟的黑屏。
         */
        const val MAX_IMAGES = 1000
    }

    private val indexFile = File(cache.targetDbFile.parentFile, "index.json")

    /** 当前装进 session 的那份库对应的文件时间戳，0 表示还没装过。 */
    private var installedStamp = 0L

    /** 库不存在或比索引旧。 */
    val stale: Boolean
        get() {
            val db = cache.targetDbFile
            if (!db.isFile || db.length() <= 0) return true
            if (!indexFile.isFile) return false // 索引都没有，库里那些是仅存的信息
            return db.lastModified() < indexFile.lastModified()
        }

    /**
     * 把库标成过期。同步下完缩略图后调，真正重建推迟到下次扫描（见类文档）。
     *
     * 直接删掉文件而不是改时间戳：残留一份内容已经不对的库，比没有库更糟 ——
     * 它会让「认出来了但元数据是旧的」这种错误看起来像识别正常。
     */
    fun invalidate() {
        cache.targetDbFile.delete()
        installedStamp = 0L
    }

    /**
     * 给 [CacheSync] 的重建回调。**它只标记，不建库**。
     *
     * 返回的 `accepted` 是「有缩略图、且没被 ARCore 拒过」的条数，也就是下一次扫描
     * 建库时会去喂的那些 —— 不是「已经进库的条数」。这个区分在界面上说清楚：
     * 那一页写的是「可离线识别 N 张」，而不是「库里 N 张」。
     */
    fun deferredRebuild(): (List<CachedPhoto>) -> CacheSync.RebuildResult = { usable ->
        invalidate()
        CacheSync.RebuildResult(accepted = usable.size)
    }

    /**
     * 库过期就重建。**重（200 张约几百毫秒），要在后台线程调**。
     *
     * 只需要 [Session] 拿原生上下文，不碰 `configure()` —— 所以能和 GL 线程上的
     * `session.update()` 并发跑。装库那一步（[install]）才必须回 GL 线程。
     *
     * 这就是建/装分成两个方法的全部原因：合成一个「确保装好」会把几百毫秒的
     * 特征提取压到 GL 线程上，表现为启动扫描时预览卡住一下。
     *
     * @return null 表示不需要重建。
     */
    fun rebuildIfStale(session: Session): CacheSync.RebuildResult? {
        if (!stale) return null
        return rebuild(session)
    }

    /**
     * 把当前的库文件装进 session。**必须在 GL 线程调**（会 `session.configure()`）。
     *
     * 幂等：库文件没变、且此刻 session 里装的确实是多图库，就什么都不做 ——
     * 每次 `configure()` 都会重置 session，白装一次就是白丢几帧跟踪。
     *
     * @return 失败原因，成功或「无事可做」返回 null。
     */
    fun install(holder: ArSessionHolder): String? {
        val session = holder.session ?: return "会话不存在"
        val db = cache.targetDbFile
        if (!db.isFile || db.length() <= 0) return null // 一张可用的都没有，正常
        if (db.lastModified() == installedStamp && holder.multiImageLoaded) return null
        val stamp = db.lastModified()
        val loaded = try {
            ByteArrayInputStream(db.readBytes()).use {
                AugmentedImageDatabase.deserialize(session, it)
            }
        } catch (e: Throwable) {
            // 版本不匹配（换了 ARCore 版本）会走到这里。删掉重建，别每次都试。
            Log.w(TAG, "本地库读不回来，删掉重建", e)
            invalidate()
            return "本地库读取失败：${e.message ?: e.javaClass.simpleName}"
        }
        val failure = holder.loadLocalDb(loaded)
        if (failure == null) installedStamp = stamp
        return failure
    }

    /**
     * 把本地库装回 session，跳过「装过就不装」的判断。
     *
     * 用在退出某张照片之后：那期间 session 里装的是那张的单图 `.imgdb`，
     * [ArSessionHolder.clearTarget] 把库清成了空 —— 本地库虽然「装过」，但已经不在
     * session 里了。不装回去的话，退出第一张照片之后离线识别就静默消失。
     */
    fun reinstall(holder: ArSessionHolder): String? {
        installedStamp = 0L
        return install(holder)
    }

    /** session 关掉时调：下次起来要重新装。 */
    fun onSessionGone() {
        installedStamp = 0L
    }

    /**
     * 建库并序列化到 [PhotoCache.targetDbFile]。
     *
     * 被 ARCore 拒掉的那些会记进索引（[PhotoCache.markRejected]），下次不再白试 ——
     * `addImage` 每张几十毫秒，200 张里有 20 张纯色照片就是半秒的无效开销，
     * 而且每次建库都付。
     */
    private fun rebuild(session: Session): CacheSync.RebuildResult {
        val usable = cache.entries().filter { it.usableAsTarget }.take(MAX_IMAGES)
        if (usable.isEmpty()) {
            // 一张可用的都没有：把旧库删掉，别让上一次的残留继续认。
            invalidate()
            return CacheSync.RebuildResult()
        }
        val rejected = ArrayList<String>()
        var accepted = 0
        val db = try {
            AugmentedImageDatabase(session)
        } catch (e: Throwable) {
            return CacheSync.RebuildResult(failure = "建库失败：${e.message ?: e.javaClass.simpleName}")
        }
        usable.forEach { e ->
            val bitmap = decode(cache.thumbFile(e.photoId)) ?: run {
                // 文件坏了。不算「被 ARCore 拒」—— 那个标记是永久的，而这里
                // 重下一次就好，交给下一轮 reconcile 发现字节数不对。
                Log.w(TAG, "缩略图解不出来：${e.photoId}")
                return@forEach
            }
            try {
                db.addImage(e.photoId, bitmap, e.printWidthM)
                accepted++
            } catch (t: Throwable) {
                // 最常见的是特征太少（纯色、严重模糊），ARCore 直接拒收
                rejected.add(e.photoId)
            } finally {
                bitmap.recycle()
            }
        }
        rejected.forEach { cache.markRejected(it) }
        if (rejected.isNotEmpty()) cache.flush()
        if (accepted == 0) {
            invalidate()
            return CacheSync.RebuildResult(rejected = rejected, accepted = 0)
        }
        val failure = serialize(db)
        return CacheSync.RebuildResult(
            rejected = rejected,
            accepted = if (failure == null) accepted else 0,
            failure = failure,
        )
    }

    /** tmp + rename，理由同 [TargetLoader]：写一半的库读回来会让整份缓存的识别失效。 */
    private fun serialize(db: AugmentedImageDatabase): String? {
        val target = cache.targetDbFile
        val tmp = File(target.parentFile, target.name + ".tmp")
        return try {
            target.parentFile?.mkdirs()
            FileOutputStream(tmp).use { db.serialize(it) }
            if (!tmp.renameTo(target)) {
                tmp.delete()
                return "库写盘失败：改名不成功"
            }
            null
        } catch (e: Throwable) {
            tmp.delete()
            "库写盘失败：${e.message ?: e.javaClass.simpleName}"
        } finally {
            if (tmp.exists()) tmp.delete()
        }
    }

    private fun decode(f: File): Bitmap? {
        if (!f.isFile || f.length() <= 0) return null
        return try {
            val bytes = f.readBytes()
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
            val longEdge = maxOf(bounds.outWidth, bounds.outHeight)
            if (longEdge <= 0) return null
            var sample = 1
            while (longEdge / (sample * 2) >= MAX_EDGE) sample *= 2
            val opts = BitmapFactory.Options().apply {
                inSampleSize = sample
                // ARCore 的 addImage 只吃 ARGB_8888 / 灰度，RGB_565 会被拒
                inPreferredConfig = Bitmap.Config.ARGB_8888
            }
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)
        } catch (e: Throwable) {
            null
        }
    }
}

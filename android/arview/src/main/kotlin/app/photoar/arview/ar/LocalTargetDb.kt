package app.photoar.arview.ar

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import app.photoar.arview.cache.CacheSync
import app.photoar.arview.cache.CachedPhoto
import app.photoar.arview.cache.PhotoCache
import app.photoar.arview.cache.ServerTargetsStore
import com.google.ar.core.AugmentedImageDatabase
import com.google.ar.core.Session
import java.io.File
import java.io.FileOutputStream

/**
 * 扫描期间 session 里装的那个多图库（§11.3 / Phase 4）。
 *
 * 两个来源，**优先服务端预建的那一份**：
 *
 * 1. `targets.imgdb` —— 服务端 `arcoreimg` 拿**原图**建的整库多目标，`GET /v1/targets/db`
 *    下来的（见 [ServerTargetsStore]）。装它只要一次 `deserialize`（10-20ms，官方数字，
 *    5MB 的库），覆盖到 ARCore 的 1000 张上限，跟踪质量和联网命中时完全一样。
 * 2. `local.imgdb` —— 端上拿 640px 缩略图现建的那份（下面那一堆 `addImage`）。
 *
 * 第 2 条**一行不删**，因为第 1 条有一个真实的失败模式：服务端的 `arcoreimg` 比端上的
 * ARCore 新时 `deserialize` 会抛（见 [ArSessionHolder.deserializeDb]）。那时候必须还有
 * 东西可以退回去 —— 否则离线识别是**静默**消失的，表现为「昨天还能离线认，今天不行」，
 * 而两条日志都不会有人看到。
 *
 * 优先级判断本身在 [planTargetDb]（纯函数，JVM 单测覆盖）；这个类只负责执行。
 *
 * 建库为什么和「装库」分开、为什么不在同步时就建、为什么试装要排在建库之前 —— 见下面
 * 三段。
 *
 * ## 建库需要 Session，而同步时可能没有
 *
 * `AugmentedImageDatabase(session)` 的构造要一个 [Session]，而「缓存管理」页点
 * 「现在同步」时相机根本没开，也可能连相机权限都还没给。为此单独建一个 Session
 * 只为了建库是错的：那要相机权限、要 ARCore 装着，而这两件事跟「把文件下下来」
 * 毫无关系。
 *
 * 所以顺序反过来：同步只负责把缩略图下齐，然后调 [invalidate] 把库标成「过期」；
 * 真正建库发生在**下一次扫描启动时**（[prepare]），那时候 session 一定在。
 * 代价是刚同步完的第一次扫描要多花几百毫秒建库 —— 一次性的，且发生在用户举起
 * 手机对准照片之前的那段时间里。
 *
 * ## 过期判定用文件时间而不是另存一个标记
 *
 * `local.imgdb` 比**最新的那张缩略图**旧就是过期。多一个「dirty 标记文件」意味着
 * 多一个会和现实不一致的状态 —— 标记写成功而库没写成功、或者反过来，都会让离线
 * 识别静默失效。文件 mtime 是内核维护的，不会漏。
 *
 * 比的是缩略图而**不是 `index.json`**：索引每次扫描结束都会因为 `lastSeenAt` 被重写
 * 一遍（见 [PhotoCache.markSeen]），拿它判过期等于每次启动扫描都白重建一次库。
 * 详见 [PhotoCache.newestThumbMs]。
 *
 * 这条只管端上那份。服务端那份的新旧**本地判不了**（它的输入是服务端那套照片），
 * 只能靠 version 去问服务端要 304 还是 200 —— 理由写在 [ServerTargetsStore] 那边。
 *
 * ## 服务端那份要在后台线程上先试装一次
 *
 * [prepare] 会真的 `deserialize` 一遍服务端那份，成功才认它。看着多余（[install] 里
 * 还要再来一次），但它是这套优先级能成立的前提：**「装不上」只有真的解一次才知道，而
 * 退回端上现建要跑几秒的 `addImage`，那件事绝不能发生在 GL 线程上**。等到 [install]
 * （GL 线程）才发现装不上的话，只剩两个选择 —— 在 GL 线程上卡几秒建库，或者放弃离线
 * 识别。多花的那 10-20ms 是一次性的，且发生在用户举起手机之前。
 *
 * 也刻意**不**把试装出来的那个 `AugmentedImageDatabase` 留着给 [install] 复用：跨两次
 * `configure()` 复用同一个原生对象，ARCore 没说过行不行，而真机上验证不了。宁可多解
 * 一次。
 */
class LocalTargetDb(
    private val cache: PhotoCache,
    private val server: ServerTargetsStore = ServerTargetsStore(cache),
) {

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

    /** 当前装进 session 的那份库对应的文件时间戳，0 表示还没装过。 */
    private var installedStamp = 0L

    /** 装进 session 的是哪个文件。和 [installedStamp] 一起做「装过就不重装」的判断。 */
    private var installedFile: File? = null

    /**
     * [prepare] 选中的那个文件。null 表示**一份可用的都没有**。
     *
     * `@Volatile`：在后台线程（`dbWork`）上写，在 GL 线程上读。
     */
    @Volatile
    private var chosen: File? = null

    /**
     * [prepare] 跑过了。
     *
     * 和「[chosen] 非空」不是一回事，而这个区分是必须的：prepare 跑完发现一份都没有时
     * [chosen] 也是 null，那时候 [install] **不该**再去兜底挑一遍 —— 否则一份已知装不上
     * 的预建库会在每次退出照片（[reinstall]）时被重试一次，而那是在 GL 线程上。
     */
    @Volatile
    private var prepared = false

    /** 此刻装的是哪一份。null = 一份都没有。界面上要用它区分跟踪质量。 */
    @Volatile
    var source: TargetDbSource? = null
        private set

    /**
     * 一次 [prepare] 的结果。
     *
     * @param source 选中了哪一份，null 表示一份可用的都没有（扫描仍能走服务端识别）。
     * @param rebuild 端上现建那条路真的建了库时的结果，没建就是 null。
     * @param serverFailure 服务端那份装不上的原因。非 null 时**已经退回**端上现建，
     *   并且这个版本被记成 rejected（下次不再白试，见 [ServerTargetsStore.markRejected]）。
     *   调用方要据此报一条提示 —— 离线识别降了一档这件事不能只写在日志里。
     */
    data class Prepared(
        val source: TargetDbSource?,
        val rebuild: CacheSync.RebuildResult? = null,
        val serverFailure: String? = null,
    )

    /** 端上那份库不存在，或者有缩略图比它新。 */
    val stale: Boolean
        get() {
            val db = cache.targetDbFile
            if (!db.isFile || db.length() <= 0) return true
            val newest = cache.newestThumbMs()
            // 一张缩略图都没有：没有任何输入能重建，库里那些是仅存的信息
            if (newest == 0L) return false
            return db.lastModified() < newest
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
        installedFile = null
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
     * 挑出这次要装的那一份，需要时把它准备好。**重（可能几秒），要在后台线程调**。
     *
     * 只需要 [Session] 拿原生上下文，不碰 `configure()` —— 所以能和 GL 线程上的
     * `session.update()` 并发跑。装库那一步（[install]）才必须回 GL 线程。
     *
     * 这就是建/装分成两个方法的全部原因：合成一个「确保装好」会把几百毫秒到几秒的
     * 特征提取压到 GL 线程上，表现为启动扫描时预览卡住一下。
     *
     * 完整的决策树（每一步为什么，见类文档与 [planTargetDb]）：
     *
     * ```
     * 服务端那份能装（字节在 + 元数据在 + 这个版本没被拒过）？
     *   ├─ 是 → deserialize 试一下
     *   │        ├─ 成功 → 用它（SERVER）。端上那份碰都不碰，也不重建。
     *   │        └─ 失败 → 把这个版本记成 rejected，带着原因往下走 ↓
     *   └─ 否 →
     *        端上那份过期（含不存在）？→ 是就先 addImage 重建一遍
     *        重建后有文件？
     *          ├─ 有 → 用它（LOCAL）
     *          └─ 没有（一张可用缩略图都没有）→ 一份都没有，扫描走服务端识别
     * ```
     */
    fun prepare(session: Session): Prepared {
        val snapshot = server.installable()
        var serverFailure: String? = null
        if (planTargetDb(TargetDbFacts(snapshot != null, stale)) is TargetDbPlan.UseServer) {
            val err = validateServer(session)
            if (err == null) {
                chosen = server.dbFile
                source = TargetDbSource.SERVER
                prepared = true
                return Prepared(TargetDbSource.SERVER)
            }
            // 版本不匹配（服务端的 arcoreimg 比端上的 ARCore 新）走到这里。记在
            // **这个版本**上而不是删文件：删了下次同步要重下 6MB，然后再失败一次。
            Log.w(TAG, "服务端预建库装不上，退回端上现建：$err")
            snapshot?.let { server.markRejected(it.version) }
            serverFailure = err
        }
        // ---- 端上现建那条路，与预建库无关，一行没删 ----
        val plan = planTargetDb(TargetDbFacts(serverInstallable = false, localStale = stale))
        val rebuild = if (plan is TargetDbPlan.UseLocal && plan.rebuildFirst) {
            rebuild(session)
        } else {
            null
        }
        val local = cache.targetDbFile
        val ok = local.isFile && local.length() > 0
        chosen = if (ok) local else null
        source = if (ok) TargetDbSource.LOCAL else null
        prepared = true
        return Prepared(source, rebuild, serverFailure)
    }

    /**
     * 服务端那份能不能装。**要在后台线程调**（会 deserialize，但不 configure）。
     *
     * @return 失败原因，能装返回 null。
     */
    private fun validateServer(session: Session): String? {
        return try {
            val bytes = server.dbFile.readBytes()
            if (bytes.isEmpty()) return "服务端预建库是 0 字节"
            ArSessionHolder.deserializeDb(session, bytes)
            null
        } catch (e: Throwable) {
            e.message ?: e.javaClass.simpleName
        }
    }

    /**
     * 把 [prepare] 选中的那份库装进 session。**必须在 GL 线程调**（会 `session.configure()`）。
     *
     * 幂等：库文件没变、且此刻 session 里装的确实是多图库，就什么都不做 ——
     * 每次 `configure()` 都会重置 session，白装一次就是白丢几帧跟踪。
     *
     * @return 失败原因，成功或「无事可做」返回 null。
     */
    fun install(holder: ArSessionHolder): String? {
        val session = holder.session ?: return "会话不存在"
        // prepare 还没跑到（会话刚起、dbWork 还在排队）时的兜底：只在两个**现成**文件
        // 里挑，绝不重建 —— 这里是 GL 线程。
        val db = (if (prepared) chosen else fallbackChoice()) ?: return null
        if (!db.isFile || db.length() <= 0) return null // 一张可用的都没有，正常
        if (db == installedFile && db.lastModified() == installedStamp && holder.multiImageLoaded) {
            return null
        }
        val stamp = db.lastModified()
        val loaded = try {
            ArSessionHolder.deserializeDb(session, db.readBytes())
        } catch (e: Throwable) {
            val why = e.message ?: e.javaClass.simpleName
            Log.w(TAG, "库读不回来：$db", e)
            if (db == server.dbFile) {
                // prepare 里已经试装成功过，走到这里说明文件在这中间被换了。记住这个
                // 版本，下一次 prepare 就会退回端上现建 —— 但**不在这里**重建（GL 线程）。
                chosen = null
                source = null
                server.snapshot()?.let { server.markRejected(it.version) }
                return "服务端预建库读取失败：$why"
            }
            // 端上那份读不回来（换了 ARCore 版本）：删掉，下次 prepare 会重建。
            invalidate()
            return "本地库读取失败：$why"
        }
        val failure = holder.loadLocalDb(loaded)
        if (failure == null) {
            installedStamp = stamp
            installedFile = db
            // [source] 说的是「此刻 session 里装的是哪一份」，所以按**真的装进去**的那个
            // 文件设，而不是沿用 prepare 挑的那个 —— 兜底路径（prepare 还没跑完）装的
            // 可能是另一份，而这个值决定离线命中时那句提示怎么说跟踪质量。
            source = if (db == server.dbFile) TargetDbSource.SERVER else TargetDbSource.LOCAL
        }
        return failure
    }

    /**
     * [prepare] 没来得及跑时，在两个现成文件里挑一个。
     *
     * 与 [prepare] 的差别只有一处：**不重建**。所以它可能什么都挑不出来（端上那份被
     * 上一次同步标成过期删掉了），那时候这一次扫描就先没有离线识别 —— 而 prepare 正在
     * 后台跑，它跑完会再装一次。
     */
    private fun fallbackChoice(): File? = when {
        server.installable() != null -> server.dbFile
        cache.targetDbFile.let { it.isFile && it.length() > 0 } -> cache.targetDbFile
        else -> null
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
        installedFile = null
        return install(holder)
    }

    /**
     * session 关掉时调：下次起来要重新装。
     *
     * [chosen] 也一起清掉：库对象是绑在那个 session 上 deserialize 出来的，而下一次
     * 起来时服务端那份可能已经被一次同步换掉了 —— 必须重新 [prepare] 一遍。
     */
    fun onSessionGone() {
        installedStamp = 0L
        installedFile = null
        chosen = null
        source = null
        prepared = false
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
                // 宽度未知（0）时必须走**不带宽度**的那个重载，不能传 0 —— 传了 ARCore
                // 会当真，把这张图当成 0 米宽，位姿直接是废的。不带宽度是它专门支持的
                // 用法：ARCore 自己从 SLAM 量出物理尺寸，`getExtentX` 返回那个测量值。
                if (e.printWidthM > 0f) {
                    db.addImage(e.photoId, bitmap, e.printWidthM)
                } else {
                    db.addImage(e.photoId, bitmap)
                }
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

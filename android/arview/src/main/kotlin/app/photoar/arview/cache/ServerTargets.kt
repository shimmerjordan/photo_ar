package app.photoar.arview.cache

import app.photoar.arview.ApiParseException
import app.photoar.arview.Hit
import app.photoar.arview.LocalIndex
import app.photoar.arview.TargetEntry
import app.photoar.arview.TargetsManifest
import java.io.File
import org.json.JSONArray
import org.json.JSONObject

/**
 * 服务端预建的**整库**多目标 `.imgdb` 在端上的那一半：存哪、什么时候能用、认出来
 * 之后去哪查元数据。
 *
 * ## 为什么要下一整个库，而不是端上现建
 *
 * 端上现建（[app.photoar.arview.ar.LocalTargetDb] 的 `addImage` 那条路）有三笔代价：
 * `addImage` 每张约 30ms（ARCore 官方数字，200 张就是 6 秒）；特征提自 640px 缩略图，
 * 跟踪质量比服务端拿原图建的低一档（[app.photoar.arview.NoticeKind.LOCAL_HIT] 提示的
 * 就是这件事）；还被端上缓存的条数上限（默认 200）框着。下一整个库则是「6MB 传输 +
 * `deserialize` 10-20ms」（同样是官方量级，5MB 的库），而且能到 ARCore 的 1000 张上限。
 * 也就是说这不是省一点时间，是差两个数量级。
 *
 * 端上现建那条路**一行不删**：服务端的 `arcoreimg` 比端上的 ARCore 新时
 * `deserialize` 会抛（见 `ArSessionHolder.loadTargetFromImgdb` 的注释），那时候必须
 * 还能退回去 —— 否则离线识别是**静默**消失的。
 *
 * ## 过期判定：本地判不了，所以不判
 *
 * `local.imgdb` 拿文件时间就能判过期（它的输入就在本地：那些缩略图）。服务端那份
 * 不行 —— 它的输入是服务端那套照片，本地没有任何东西能反映它变了没有。所以这里
 * **不做**任何本地过期判定，只做一件事：把 version 和库字节存在一起，下次同步时把
 * version 当 `If-None-Match` 发出去，让服务端回答 304 还是 200。这与
 * [ModelCache] 对模型的处理是同一条约定，理由也一样。
 *
 * 推论：**刷新只发生在用户按「现在同步」的时候**。这一页刻意没有后台自动同步（见
 * `CacheScreen`），所以不在这里悄悄加一条。
 *
 * ## 装不上的那一份要记住，但只记住这一个版本
 *
 * 版本不匹配是**这台手机 × 这个版本**的属性，不是文件坏了。所以失败记在 version 上
 * （[TargetsSnapshot.rejected]）：下次扫描不再白试，而 ETag 协商照样 304（不重下
 * 6MB），等服务端换出一个新版本时自动再试一次。
 *
 * 换成「删掉文件」的话，每次同步都会重下一遍那 6MB 然后在扫描时再失败一次；换成
 * 「记一个与 version 无关的开关」的话，那个状态自己永远不会好，只能靠用户清数据。
 *
 * 已知的一处不完美：**手机上的 ARCore 升级了**（于是本来读不了的库现在能读了），版本号
 * 不会因此变，所以那一份仍然被跳过。没有为它加自动重试 —— 「每次扫描启动都白试一次
 * deserialize」是一个每天付、只在极少数那一天有用的代价。用户侧的出路是「缓存管理 →
 * 全清」（那会把 `targets.json` 一起删掉，下一次同步重下一份新的、`rejected` 为假的），
 * 而服务端那边任何一次入库也会换出新版本自动再试。
 */

/** [TargetsSnapshot] 的落盘格式版本。对不上就整份丢掉（同 [CACHE_INDEX_VERSION]）。 */
const val TARGETS_META_VERSION = 1

/**
 * 本地存着的那份预建库的全部元数据。
 *
 * @param version 服务端算的内容哈希，也就是 `GET /v1/targets/db` 的 ETag。
 * @param rejected 这个版本在这台机器上 `deserialize` 失败过。见类文档最后一节。
 * @param entries manifest 里那些照片。**它是端上的第二个元数据来源** —— 预建库能覆盖
 *   到 1000 张，而端侧缓存默认只留 200 张，中间那 800 张认出来之后只有这里查得到
 *   printWidthM / mediaUrl。
 */
data class TargetsSnapshot(
    val version: String,
    val count: Int,
    val overflow: Int,
    val maxTargets: Int,
    val rejected: Boolean,
    val entries: List<TargetEntry>,
) {
    private val byId: Map<String, TargetEntry> = entries.associateBy { it.photoId }

    fun entry(photoId: String): TargetEntry? = byId[photoId]

    /** 这一份能不能拿去装。[rejected] 的那个版本不行，空版本号也不行。 */
    val installable: Boolean get() = version.isNotEmpty() && !rejected

    companion object {
        fun of(m: TargetsManifest, rejected: Boolean = false): TargetsSnapshot = TargetsSnapshot(
            version = m.version,
            count = m.count,
            overflow = m.overflow,
            maxTargets = m.maxTargets,
            rejected = rejected,
            entries = m.targets,
        )
    }
}

/**
 * manifest 里的一条 → [Hit]，好让状态机的后半段一字不改地复用（同
 * [CachedPhoto.toHit]）。
 *
 * 扩展函数而不是 [TargetEntry] 自己的方法：那个类在 `Api.kt` 里，而
 * [LOCAL_HIT_INLIERS] 在这个包。让 api 层反过来依赖 cache 层，是为了一个转换函数
 * 把依赖方向弄成环。
 *
 * [Hit.refStale] 只能填 false：manifest **不带**这个字段。不是漏了 —— 预建库的
 * version 里含着每张参考图的 sha256，所以「库是这些参考图的函数」这件事由版本号保证，
 * 而 `refStale`（服务端记的内容 vs 文件现状）在这条路上无从得知。代价是这 800 张
 * 里若有参考图动过的，离线命中时不会有 REF_STALE 提示；下一次同步版本会变，库跟着换。
 */
fun TargetEntry.toHit(): Hit = Hit(
    photoId = photoId,
    inliers = LOCAL_HIT_INLIERS,
    printWidthM = printWidthM,
    refAspect = refAspect,
    imgdbUrl = imgdbUrl,
    refThumbUrl = "/v1/photo/$photoId/thumb",
    mediaUrl = mediaUrl,
    refStale = false,
    latencyMs = 0,
)

/**
 * 离线命中的元数据从哪来 —— 端侧缓存索引与预建库 manifest 两个来源合一。
 *
 * ARCore 只会报出**装进 session 那个库里**的名字，所以这里不需要知道此刻装的是哪一份：
 * 报出来的一定在库里，要做的只是「把这个 photoId 的元数据找出来」。
 *
 * 优先级：
 *
 * 1. **缓存索引里有、且这条自己可用**（有缩略图、没被 ARCore 拒过）→ 用它。它带着
 *    `videoBytes` / `videoDurationMs`，而 `ScanRuntime.fetchMedia` 正是靠缓存那条
 *    记录直接给出 `file://` 地址的 —— 用 manifest 那份会丢掉本地视频这件事。
 * 2. **manifest 里有** → 用它。这一条把「预建库覆盖到了、但端侧没缓存」的照片接了
 *    起来：认出来算命中，视频没缓存就按 [app.photoar.arview.NoticeKind.VIDEO_NOT_CACHED]
 *    走（那条提示明确说这是用户联网就能解决的情况），而**不是**当成没认出来 —— 后者
 *    的表现是「这张照片扫不出来」，而它明明在库里。
 *    顺带也覆盖了另一种真实情况：某张照片的缩略图被端上 ARCore 拒过
 *    （[CachedPhoto.targetRejected]），但服务端拿原图建的库里有它。
 * 3. 都没有 → null，继续走服务端 `/v1/recognize`。库和索引理论上同步，真出现不一致时
 *    宁可多一次网络往返，也不要拿一条没有元数据的命中往下走（那会让视频按上一张照片
 *    的尺寸去贴）。
 *
 * 两个来源都用 lambda 取，而不是直接持 [PhotoCache] 和一个快照：`snapshot` 会在
 * 「现在同步」之后变，而这个对象在扫描页的整个生命周期里只建一次。
 */
class MergedLocalIndex(
    private val cached: (String) -> CachedPhoto?,
    private val snapshot: () -> TargetsSnapshot?,
) : LocalIndex {

    override fun lookup(photoId: String): Hit? {
        cached(photoId)?.takeIf { it.usableAsTarget }?.let { return it.toHit() }
        return snapshot()?.entry(photoId)?.toHit()
    }
}

/**
 * `targets.json` 的编解码。
 *
 * 存整份 manifest 而不只存 version：那 800 张「预建库里有、端侧没缓存」的照片，离线
 * 命中时只有这里查得到元数据。断网时也要能查，所以它必须在磁盘上。
 *
 * 版本对不上就整份丢掉、不做迁移 —— 同 [CacheIndexCodec]，代价只是重下一次库。
 */
object ServerTargetsCodec {

    fun encode(s: TargetsSnapshot): String {
        val arr = JSONArray()
        s.entries.forEach { e ->
            arr.put(
                JSONObject().apply {
                    put("photoId", e.photoId)
                    put("printWidthM", e.printWidthM.toDouble())
                    e.refAspect?.let { put("refAspect", it.toDouble()) }
                    e.fitMode?.let { put("fitMode", it) }
                    e.title?.let { put("title", it) }
                    put("hasVideo", e.hasVideo)
                    put("mediaUrl", e.mediaUrl)
                    put("imgdbUrl", e.imgdbUrl)
                },
            )
        }
        return JSONObject().apply {
            put("version", TARGETS_META_VERSION)
            put("targetsVersion", s.version)
            put("count", s.count)
            put("overflow", s.overflow)
            put("maxTargets", s.maxTargets)
            put("rejected", s.rejected)
            put("targets", arr)
        }.toString()
    }

    /**
     * @throws ApiParseException 不是 JSON、格式版本对不上、或者没有 targetsVersion。
     *   调用方（[ServerTargetsStore]）把它当成「本地没有这一份」处理。
     */
    fun parse(json: String): TargetsSnapshot {
        val o = try {
            JSONObject(json)
        } catch (e: Exception) {
            throw ApiParseException("targets 元数据不是 JSON：${json.take(80)}")
        }
        val v = o.optInt("version", -1)
        if (v != TARGETS_META_VERSION) {
            throw ApiParseException("targets 元数据版本 $v，当前是 $TARGETS_META_VERSION，丢弃重下")
        }
        val targetsVersion = str(o, "targetsVersion")
            ?: throw ApiParseException("targets 元数据里没有 targetsVersion")
        val arr = o.optJSONArray("targets") ?: JSONArray()
        val out = ArrayList<TargetEntry>(arr.length())
        for (i in 0 until arr.length()) {
            val e = arr.optJSONObject(i) ?: continue
            val id = str(e, "photoId") ?: continue
            // 同 CacheIndexCodec：宽度不可用 → 0 = 未知，条目保留。
            val width = e.optDouble("printWidthM", 0.0).toFloat()
                .takeIf { it.isFinite() && it > 0f } ?: 0f
            out.add(
                TargetEntry(
                    photoId = id,
                    printWidthM = width,
                    refAspect = e.optDouble("refAspect", Double.NaN).toFloat()
                        .takeIf { it.isFinite() && it > 0f },
                    fitMode = str(e, "fitMode"),
                    title = str(e, "title"),
                    hasVideo = e.optBoolean("hasVideo", false),
                    mediaUrl = str(e, "mediaUrl") ?: "/v1/photo/$id/media",
                    imgdbUrl = str(e, "imgdbUrl") ?: "/v1/photo/$id/imgdb",
                ),
            )
        }
        return TargetsSnapshot(
            version = targetsVersion,
            count = o.optInt("count", out.size),
            overflow = o.optInt("overflow", 0).coerceAtLeast(0),
            maxTargets = o.optInt("maxTargets", 0).coerceAtLeast(0),
            rejected = o.optBoolean("rejected", false),
            entries = out,
        )
    }

    /** 同 [app.photoar.arview.ApiParse]：`optString(name, null)` 在两个 org.json 实现上行为不同。 */
    private fun str(o: JSONObject, name: String): String? =
        if (o.isNull(name)) null else o.optString(name, "").takeIf { it.isNotEmpty() }
}

/**
 * 预建库那两个文件的读写。这一层薄到只有 IO：判断都在上面那些纯类里。
 *
 * 只用 `java.io.File`（同 [PhotoCache]、[ModelCache]），所以「写一半被杀」「元数据坏了」
 * 「版本对不上」这些在真机上要拔电源才造得出的情况，JVM 单测里给个临时目录就能跑。
 */
class ServerTargetsStore(private val cache: PhotoCache) {

    val dbFile: File get() = cache.serverTargetDbFile

    private val metaFile: File get() = cache.targetsMetaFile

    /** 库字节数，0 表示没有。 */
    val bytes: Long get() = if (dbFile.isFile) dbFile.length() else 0L

    /**
     * 本地那份的元数据。读不出来（第一次跑、格式变了、写坏了）就是 null —— 当成
     * 「没有这一份」，不抛：它是纯派生数据，读不回来的正确反应是重下。
     */
    fun snapshot(): TargetsSnapshot? {
        val json = try {
            if (metaFile.isFile) metaFile.readText() else null
        } catch (e: Exception) {
            null
        } ?: return null
        return try {
            ServerTargetsCodec.parse(json)
        } catch (e: Exception) {
            null
        }
    }

    /**
     * 现在能装的那一份。库字节和元数据必须**同时**在，且这个版本没被拒过。
     *
     * 库在而元数据不在是可能的（写库成功、写元数据失败），那时候这一份不能用：没有
     * version 就没法做 ETag 协商，也没法查那 800 张的元数据 —— 一个「能装但查不到
     * 元数据」的库比没有库更糟，它会让每次离线命中都拿不到尺寸。
     */
    fun installable(): TargetsSnapshot? = snapshot()?.takeIf { it.installable && bytes > 0 }

    /**
     * 落盘。**先库、后元数据**（理由见 [PhotoCache.targetsMetaFile]）。
     *
     * @return 成功没有。失败不抛：这一步失败只意味着「这次没更新上，下次再来」，
     *   而扫描本来就还有端上现建那条路和服务端识别那条路。
     */
    fun store(bytes: ByteArray, manifest: TargetsManifest): Boolean {
        if (bytes.isEmpty()) return false
        val dir = dbFile.parentFile
        if (dir != null && !dir.isDirectory && !dir.mkdirs()) return false
        if (!writeAtomic(dbFile, bytes)) return false
        return writeMeta(TargetsSnapshot.of(manifest))
    }

    /**
     * manifest 变了但库字节没变（304）时，只更新元数据。
     *
     * 必须支持这条路：manifest 是 `no-store` 的、每次现取，而标题 / hasVideo /
     * overflow **刻意不在版本号里**（改个标题不该让全体客户端重下 6MB）。不更新的话，
     * 一张照片补了视频、或者改了标题，在离线那条路上永远看不到。
     *
     * 版本对不上就什么都不做并返回 false：那说明服务端换过库而我们手上这份是旧的
     * （中间有人入库了），此时把新 manifest 配到旧库上正是「db 里有的 manifest 里
     * 没有」那类不一致 —— 服务端那边费了很大劲堵死的就是它。下一次同步会一起换掉。
     */
    fun refreshMeta(manifest: TargetsManifest): Boolean {
        val current = snapshot() ?: return false
        if (current.version != manifest.version) return false
        return writeMeta(TargetsSnapshot.of(manifest, rejected = current.rejected))
    }

    /**
     * 记下「这个版本在这台机器上装不上」。
     *
     * 只在 version 对得上时才记：一条迟到的失败报告不该把刚换上来的新版本打成坏的。
     */
    fun markRejected(version: String): Boolean {
        val current = snapshot() ?: return false
        if (current.version != version || current.rejected) return false
        return writeMeta(current.copy(rejected = true))
    }

    /** 库和元数据一起删。授权被撤（404 `no_targets`）时用。 */
    fun clear() {
        dbFile.delete()
        metaFile.delete()
    }

    private fun writeMeta(s: TargetsSnapshot): Boolean =
        writeAtomic(metaFile, ServerTargetsCodec.encode(s).toByteArray(Charsets.UTF_8))

    /** tmp + rename，理由同 [PhotoCache.flush]：写一半的库读回来会让离线识别整份失效。 */
    private fun writeAtomic(f: File, bytes: ByteArray): Boolean {
        val tmp = File(f.parentFile, f.name + ".tmp")
        return try {
            tmp.writeBytes(bytes)
            if (!tmp.renameTo(f)) {
                tmp.delete()
                f.writeBytes(bytes)
            }
            true
        } catch (e: Exception) {
            tmp.delete()
            false
        } finally {
            if (tmp.exists()) tmp.delete()
        }
    }
}

/**
 * 服务端说「正在建」（503）时的重试节奏。纯类，时间由调用方注入的 sleep 执行。
 *
 * 为什么要在一轮同步里等而不是直接放弃：服务端的 manifest 请求**顺手就把构建踢起来
 * 了**，所以这时候等的是一个确定会完成的事情。直接放弃的话用户得再按一次「现在同步」，
 * 而他没有任何理由知道该这么做。
 *
 * 为什么必须有上限：真实建库耗时**没有被测量过**（`arcoreimg` 是闭源二进制，服务端
 * 那边的注释写着这件事）。它可能是几秒也可能是几十秒，甚至可能因为磁盘满而永远建不
 * 出来。没有上限的等待会把「同步」变成一个不会结束的按钮。
 *
 * 三条都是上限：单次等待（服务端给个 3600 也不能真等一小时）、次数、总时长。
 */
class TargetsBuildWait(
    private val maxAttempts: Int = 5,
    private val defaultDelayS: Int = 5,
    private val maxDelayS: Int = 15,
    private val maxTotalWaitS: Int = 45,
) {

    init {
        require(maxAttempts > 0) { "maxAttempts 必须为正" }
        require(defaultDelayS > 0) { "defaultDelayS 必须为正" }
        require(maxDelayS >= defaultDelayS) { "maxDelayS 不能小于 defaultDelayS" }
    }

    var attempts = 0
        private set

    var waitedS = 0
        private set

    /**
     * 下一次该等多少毫秒。null 表示别再等了。
     *
     * @param retryAfterS 服务端给的 `Retry-After`。null（头被代理剥了、或者是 HTTP-date
     *   格式）时用 [defaultDelayS] —— 服务端那边选的也是 5 秒，且注释里写明「猜小了
     *   客户端多问几次，猜大了用户干等，所以宁可偏小」。
     */
    fun nextDelayMs(retryAfterS: Int?): Long? {
        if (attempts >= maxAttempts) return null
        if (waitedS >= maxTotalWaitS) return null
        val asked = retryAfterS?.takeIf { it > 0 } ?: defaultDelayS
        // 总时长上限也一起夹：剩 3 秒预算时等 15 秒是把上限当没有。
        val delay = minOf(asked, maxDelayS, maxTotalWaitS - waitedS)
        if (delay <= 0) return null
        attempts++
        waitedS += delay
        return delay * 1000L
    }
}

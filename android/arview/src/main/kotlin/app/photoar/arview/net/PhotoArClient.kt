package app.photoar.arview.net

import app.photoar.arview.AccountInfo
import app.photoar.arview.ApiParse
import app.photoar.arview.ApiParseException
import app.photoar.arview.AttachResult
import app.photoar.arview.CatalogParse
import app.photoar.arview.CreateResult
import app.photoar.arview.Endpoints
import app.photoar.arview.FsListing
import app.photoar.arview.HistoryEntry
import app.photoar.arview.Hit
import app.photoar.arview.LoginResult
import app.photoar.arview.LookupResult
import app.photoar.arview.MediaInfo
import app.photoar.arview.NetErrorKind
import app.photoar.arview.PhotoDetail
import app.photoar.arview.PhotoSummary
import app.photoar.arview.RecognizeOutcome
import app.photoar.arview.ReplaceRefResult
import app.photoar.arview.TargetsManifest
import app.photoar.arview.UploadCheck
import org.json.JSONObject

/** [PhotoArClient.fetchModel] 的结果。304 与 200 对调用方是两种完全不同的动作。 */
sealed interface ModelFetch {
    /** 服务端说本地那份还是最新的。 */
    data object NotModified : ModelFetch

    data class Fresh(val bytes: ByteArray, val etag: String?) : ModelFetch
}

/**
 * [PhotoArClient.targetsDb] 的结果。
 *
 * 做成一个 sealed 类型而不是「成功返回字节 + 其它情况抛异常」，是因为服务端那边有
 * **三种非 200 的正常状态**，而它们各自的下一步动作都不一样：
 *
 * - [NotModified]（304）：本地那份就是最新的。这是稳态下最常见的结果。
 * - [Building]（503）：库正在建。服务端把「按需建库」这件事做成了后台线程 + 503，
 *   所以这是正常状态而不是失败 —— 按 `Retry-After` 再来一次就好。
 * - [Empty]（404 `no_targets`）：这个人一张照片都没被授权。重试一万次结果一样，
 *   而本地留着的那份预建库应该删掉（授权被撤了）。
 *
 * 用异常表达它们的话，调用方只能靠 `HttpFailure.status` 反推，而「反推出来的状态」
 * 会随着任何一次错误映射的调整静默改语义。真正的失败（超时、5xx、凭证不对）仍然
 * 走 [HttpFailure]。
 */
sealed interface TargetsDbFetch {
    /** 新字节。[version] 是响应的 ETag 去掉引号，与 manifest 里的 `version` 同一个值。 */
    data class Fresh(val bytes: ByteArray, val version: String?) : TargetsDbFetch

    data object NotModified : TargetsDbFetch

    /** @param retryAfterS 服务端给的 `Retry-After`（秒）。拿不到时是 null。 */
    data class Building(val version: String?, val retryAfterS: Int?) : TargetsDbFetch

    /** 一张照片都没被授权。不是失败，但也没有任何字节可下。 */
    data object Empty : TargetsDbFetch
}

/**
 * §7 各接口的客户端。逻辑全在这里，网络细节在 [HttpTransport]，所以能在 JVM
 * 单测里用假 transport 覆盖状态码映射、URL 拼接、via 头这些容易错的地方。
 *
 * [endpoints] 是每次调用时取的（Phase 3 的 EndpointResolver 会在网络变化时换
 * 掉它），不要缓存返回值。
 */
class PhotoArClient(
    private val transport: HttpTransport,
    private val endpoints: () -> Endpoints,
    /** 写进 `X-PhotoAR-Endpoint`，服务端把它记进识别历史（lan / tailscale / tunnel）。 */
    private val viaLabel: () -> String? = { null },
) {

    companion object {
        /** §13：识别请求超时门限是 2s。 */
        const val RECOGNIZE_TIMEOUT_MS = 2_000

        const val META_TIMEOUT_MS = 4_000

        /**
         * 视频元数据（`GET /v1/photo/{id}/media`）专用的超时。
         *
         * **不复用 [META_TIMEOUT_MS]**：这一条在**命中到出画**那条热路径上，被
         * [app.photoar.arview.ScanController.HIT_TO_PLAY_BUDGET_MS] 那 6 秒总预算罩着；
         * 而目录列表、ping、logout 那几条都是用户在等着看列表的场景，慢一点不影响
         * 任何承诺。合成一个数的话，要么热路径超预算，要么把目录接口一起卡紧。
         *
         * 2.5s 的依据：响应是几百字节的 JSON，纯 RTT。走 Cloudflare 隧道时实测
         * 单程约 200–400ms，2.5s 留了 3 倍余量。
         */
        const val MEDIA_TIMEOUT_MS = 2_500

        /**
         * 登录的超时。
         *
         * 比 [META_TIMEOUT_MS] 长：服务端验口令要跑一次 scrypt（16MiB / 约 50ms），而
         * 那一步在一台 3GB 的 NAS 上是串在写锁后面的，同时有人在入库时可能排队几秒。
         * 4 秒会把「服务端在算」判成「网络不通」，而登录失败一次的观感代价远大于多等
         * 几秒。
         */
        const val LOGIN_TIMEOUT_MS = 15_000

        /**
         * 单目标 .imgdb（约 4.3KB）与参考缩略图（约 60KB）的下载超时。
         *
         * **原来是 10s，收到 3s。** 10s 是按「连走隧道都够」定的，但它没考虑这条路
         * 在热路径上要串两次：`TargetLoader` 先取 imgdb，失败才退回缩略图 ——
         * 10s × 2 = 20 秒，而命中到出画的承诺是 10 秒。
         *
         * 3s 的依据是**下限带宽**而不是余量：4.3KB / 3s = 11kbps，60KB / 3s = 160kbps。
         * 比 160kbps 还慢的链路上，这个 App 的视频本来也播不了。
         */
        const val DOWNLOAD_TIMEOUT_MS = 3_000

        /**
         * 缓存视频用的超时。一条 3MB 的视频在 1Mbps 的上行下要 24 秒 —— 用
         * [DOWNLOAD_TIMEOUT_MS] 的 10s 会把「网慢」判成失败，而缓存是后台活儿，
         * 慢一点没人等。
         */
        const val CACHE_VIDEO_TIMEOUT_MS = 60_000

        /**
         * 「保存到相册」取原图用的超时。
         *
         * 20 秒：原图是几 MB（不是 imgdb 那种几十 KB），[DOWNLOAD_TIMEOUT_MS] 的 3 秒
         * 在隧道上必然超时。也不给 [CACHE_VIDEO_TIMEOUT_MS] 的 60 秒 —— 那是**后台**
         * 缓存，没人等；这条是用户按了按钮站在那儿看着，一分钟不给回应比失败更糟。
         */
        const val SAVE_TIMEOUT_MS = 20_000

        /**
         * 入库要跑 eval-img + ORB + build-db + ffmpeg 转码（§8.1），一条视频几十秒
         * 很正常。这条超时不是「网络慢」而是「服务端在干活」，所以给到 3 分钟 ——
         * 比它更短会在 N5095 上把正常的长视频入库判成失败。
         */
        const val INGEST_TIMEOUT_MS = 180_000

        /** 模型 4.31MB，1Mbps 上行要 35 秒。后台活儿，给到 2 分钟。 */
        const val MODEL_TIMEOUT_MS = 120_000

        /**
         * 整库目标 `.imgdb` 的下载超时。
         *
         * 1000 个目标的库约 6MB（官方量级），在 1Mbps 的上行下要将近一分钟。和模型
         * 一样是「按一次同步」的后台活儿，没人在等，所以给到 2 分钟 —— 比它短会把
         * 「网慢」判成失败，而这条路失败的代价是整台手机的离线识别降一档。
         */
        const val TARGETS_TIMEOUT_MS = 120_000
    }

    // ---- 登录 ----

    /**
     * `POST /v1/auth/login`：访客只输名字，管理员名字 + 口令。
     *
     * **不带 Authorization**。这是唯一免鉴权的接口，而此刻手上那个 token 很可能正是
     * 过期或作废的那一个 —— 带上它换不到任何东西，却让「登录」这个唯一能修好一切的
     * 请求多一个失败可能（某些反向代理会对 bearer 做自己的校验）。
     *
     * 超时用 [LOGIN_TIMEOUT_MS] 而不是 [META_TIMEOUT_MS]：服务端验口令要跑一次 scrypt
     * （约 50ms，且是串在一把写锁上的），4 秒在隧道上偏紧 —— 而登录超时的代价是用户
     * 重输一遍口令。
     */
    fun login(name: String, password: String? = null): LoginResult {
        val ep = endpoints()
        val body = JSONObject().apply {
            put("name", name)
            // 访客留空时**不传这个字段**，而不是传空串。服务端对 viewer 是
            // 「pwd_hash 非空才验口令」，两者行为相同；但 admin 那边的报错文案
            // 不一样（不传 → "管理员登录必须输口令"，更好懂）。
            if (!password.isNullOrEmpty()) put("password", password)
        }.toString()
        val reply = transport.postJson(
            ep.api("/v1/auth/login"),
            body,
            headers(ep, authorize = false),
            LOGIN_TIMEOUT_MS,
        )
        checkLogin(reply)
        return parse { ApiParse.login(reply.text()) }
    }

    /** `GET /v1/auth/me`：当前 token 是谁的。用来验证一个存下来的 token 还活着。 */
    fun me(): AccountInfo = getJson("/v1/auth/me", ApiParse::me)

    /**
     * `POST /v1/auth/logout`。
     *
     * 失败**不抛异常**：本地那份凭证无论如何都要清掉（用户点的是「退出登录」），而
     * 服务端那条 session 行留着最多多活到过期。抛出去的话，一个没网的用户就退不出
     * 登录了 —— 那是个说不通的状态。
     *
     * @return 服务端有没有确认。false 只用于界面上加一句「服务端没收到，那条会话会
     *   自己过期」，不影响本地已经退出这件事。
     */
    fun logout(): Boolean {
        val ep = endpoints()
        return try {
            transport.postJson(
                ep.api("/v1/auth/logout"),
                "{}",
                headers(ep),
                META_TIMEOUT_MS,
            ).ok
        } catch (e: Exception) {
            false
        }
    }

    /**
     * 登录接口专用的状态码映射。
     *
     * **不复用 [check]**：那里把 401 与 403 一起映射成 [NetErrorKind.UNAUTHORIZED]，
     * 对普通接口是对的（两种都得回去登录）。但在登录接口上这两个的下一步动作正好相反 ——
     * 401 `bad_credentials` 是「重输一次就可能成」，403 是「重试一万次结果一样」。
     * 合并的代价是家里人对着一个永远不可能对的输入框反复输名字。
     */
    private fun checkLogin(reply: HttpReply) {
        if (reply.ok) return
        val text = reply.text()
        val code = ApiParse.errorCode(text)
        val msg = ApiParse.errorMessage(reply.status, text)
        val kind = when {
            reply.status == 401 -> NetErrorKind.BAD_CREDENTIALS
            reply.status == 403 -> NetErrorKind.FORBIDDEN
            reply.status >= 500 -> NetErrorKind.SERVER_ERROR
            else -> NetErrorKind.BAD_RESPONSE
        }
        throw HttpFailure(kind, reply.status, "/v1/auth/login → $msg", code)
    }

    fun recognize(jpeg: ByteArray): RecognizeOutcome {
        val ep = endpoints()
        val reply = transport.postJpeg(
            url = ep.api("/v1/recognize"),
            field = "frame",
            jpeg = jpeg,
            headers = headers(ep),
            timeoutMs = RECOGNIZE_TIMEOUT_MS,
        )
        check(reply, "/v1/recognize")
        return parse { ApiParse.recognize(reply.text()) }
    }

    /**
     * `POST /v1/recognize/features`：端上提特征那条路。
     *
     * 响应解析**共用** [ApiParse.recognize] —— 服务端保证两条路的响应形状完全一致
     * （`_decide_and_respond`）。各写一份的话，两边会慢慢长出不同的容错，然后表现为
     * 「换了路径之后偶发解析失败」。
     *
     * 超时沿用 [RECOGNIZE_TIMEOUT_MS]：这条路上传的字节更多（约 180KB vs 50KB），但
     * 服务端不用跑推理，端到端预算是同一个 2s。
     */
    fun recognizeFeatures(body: String): RecognizeOutcome {
        val ep = endpoints()
        val reply = transport.postJson(
            ep.api("/v1/recognize/features"),
            body,
            headers(ep),
            RECOGNIZE_TIMEOUT_MS,
        )
        check(reply, "/v1/recognize/features")
        return parse { ApiParse.recognize(reply.text()) }
    }

    /** 用 [MEDIA_TIMEOUT_MS] 而不是 [META_TIMEOUT_MS]：这条在命中到出画的热路径上。 */
    fun media(hit: Hit): MediaInfo = mediaAt(hit.mediaUrl)

    /**
     * 按 photoId 取同一份视频信息。
     *
     * 存在的理由是「试播」：详情页手里只有 id，没有 [Hit]（那一页没扫过任何东西），
     * 而 §7 的这条 URL 本来就是 id 拼出来的 —— [Hit.mediaUrl] 也是服务端这么拼的。
     * 不复用 [media] 是因为伪造一个 [Hit] 需要填七个和这件事无关的字段。
     */
    fun mediaOfPhoto(photoId: String): MediaInfo = mediaAt("/v1/photo/$photoId/media")

    private fun mediaAt(relativeUrl: String): MediaInfo {
        val ep = endpoints()
        val reply = transport.get(ep.api(relativeUrl), headers(ep), MEDIA_TIMEOUT_MS)
        check(reply, relativeUrl)
        return parse { ApiParse.media(reply.text()) }
    }

    /** 原始参考图 + 它的 Content-Type。 */
    class RefImage(val bytes: ByteArray, val mime: String)

    /**
     * 取**原始**参考图（`/v1/photo/<id>/ref`），给「保存到相册」用。
     *
     * 与 [download] 分开有两个实打实的理由，不是为了好看：
     *
     * 1. **要 Content-Type。** 存进相册是按 MIME 建条目的，一律当 jpeg 处理的话，
     *    一张 PNG 存进去就是个打不开的文件，而且不报任何错。
     * 2. **超时不同。** [DOWNLOAD_TIMEOUT_MS] 的 3 秒是给 imgdb / 缩略图那种几十 KB
     *    的小包定的；原图可能是几 MB，在隧道上 3 秒必然超时，表现为"保存老是失败"。
     *
     * 不用 `refThumbUrl`：那是缩略图，存下来是一张糊的，而用户要打开相册才发现。
     */
    fun downloadRef(photoId: String): RefImage {
        val ep = endpoints()
        val url = "/v1/photo/$photoId/ref"
        val reply = transport.get(ep.api(url), headers(ep), SAVE_TIMEOUT_MS)
        check(reply, url)
        return RefImage(
            bytes = reply.body,
            // 服务端一定会给，兜底到 jpeg 只是为了不因为一个缺失的头就整个失败。
            mime = reply.header("Content-Type")?.substringBefore(';')?.trim()
                ?: "image/jpeg",
        )
    }

    /** 下 imgdb 或 thumb。两个都走 **api** 通道：它们是小包，且与识别同源。 */
    fun download(relativeUrl: String): ByteArray {
        val ep = endpoints()
        val reply = transport.get(ep.api(relativeUrl), headers(ep), DOWNLOAD_TIMEOUT_MS)
        check(reply, relativeUrl)
        return reply.body
    }

    /**
     * 把一条视频整个拉下来存缓存（Phase 4）。
     *
     * 走 **media** 通道而不是 api：一条视频 1.5–3MB，从隧道拉是给 Cloudflare
     * 白送流量，而在家时 mediaBase 就是局域网直连。[MediaInfo.absolute] 为真
     * （`via == "direct_link"`）时 [MediaInfo.resolvedUrl] 会跳过前缀，那条 URL
     * 自带签名，不该再带 Authorization —— 所以请求头是按 absolute 分岔的。
     *
     * @throws HttpFailure 视频不可播（服务端说 missing）、或者取不到。
     */
    fun downloadMedia(media: MediaInfo): ByteArray {
        val ep = endpoints()
        if (!media.playable) {
            throw HttpFailure(NetErrorKind.BAD_RESPONSE, null, "视频不可用：${media.reason ?: "missing"}")
        }
        val url = media.resolvedUrl(ep)
            ?: throw HttpFailure(NetErrorKind.BAD_RESPONSE, null, "视频没有地址")
        val h = if (media.absolute) emptyMap() else headers(ep)
        val reply = transport.get(url, h, CACHE_VIDEO_TIMEOUT_MS)
        check(reply, "视频下载")
        if (reply.body.isEmpty()) {
            throw HttpFailure(NetErrorKind.BAD_RESPONSE, null, "视频下载：拿到 0 字节")
        }
        return reply.body
    }

    /**
     * `GET /v1/model/xfeat`：端上提特征要用的 ONNX 模型（4.31MB）。
     *
     * 用 ETag 协商而不是「有就不下」：模型是**可以被换掉**的（运维换一份重启服务），
     * 而换了之后库里的描述子也跟着变了 —— 手机上留着旧模型的后果是描述子对不上、
     * 识别率静默下降。一次 304 的代价是一个空响应。
     *
     * 超时用 [MODEL_TIMEOUT_MS]：4.31MB 在 1Mbps 的上行下要 35 秒，而这是「第一次打开
     * 开关时下一次」的后台活儿，没人在等。
     *
     * @param etag 本地那份的 ETag，没有就传 null。
     * @throws HttpFailure 服务端没有模型时是 404 → [NetErrorKind.BAD_RESPONSE]，
     *   `code` 是 `model_missing`。调用方应当据此静默退回传 JPEG。
     */
    fun fetchModel(etag: String? = null): ModelFetch {
        val ep = endpoints()
        val h = HashMap(headers(ep))
        etag?.takeIf { it.isNotBlank() }?.let { h["If-None-Match"] = it }
        val reply = transport.get(ep.api("/v1/model/xfeat"), h, MODEL_TIMEOUT_MS)
        // 304 要在 check() 之前判掉：它不在 200..299 里，会被当成失败。
        if (reply.status == 304) return ModelFetch.NotModified
        check(reply, "/v1/model/xfeat")
        if (reply.body.isEmpty()) {
            throw HttpFailure(NetErrorKind.BAD_RESPONSE, null, "模型下载：拿到 0 字节")
        }
        return ModelFetch.Fresh(reply.body, reply.header("ETag"))
    }

    // ---- 端上离线识别的整库目标 ----

    /**
     * `GET /v1/targets/manifest`：这台手机能离线认出哪些照片，以及它们的元数据。
     *
     * 服务端对这个接口是 `no-store` 且**不带 ETag**（manifest 里有标题 / fitMode /
     * hasVideo，而它们刻意不在版本号里，改个标题不该让全体客户端重下整个库）。所以
     * 这里也不做任何条件请求 —— 每次都现取一份，代价是几十 KB 的 JSON。
     */
    fun targetsManifest(): TargetsManifest =
        getJson("/v1/targets/manifest", ApiParse::targetsManifest)

    /**
     * `GET /v1/targets/db`：整库多目标 `.imgdb` 的字节。
     *
     * @param version 本地那份的版本（= 上一次的 ETag 去掉引号）。带上它才有 304。
     *
     * 三种非 200 的正常状态在 [check] **之前**判掉 —— 它们都不在 200..299 里，交给
     * check() 会变成异常，而其中最常见的那个（304）恰恰是稳态下的正常结果。
     *
     * 503 要求错误码是 `targets_building` 才算「正在建」：反向代理自己发的 503
     * （Cloudflare、nginx 上游挂了）与它无法从状态码上区分，而两者的下一步完全不同 ——
     * 一个是「等 5 秒再来」，一个是「服务端出问题了」。不看错误码的话，一次真实的服务
     * 中断会被报成「库正在建」，然后用户就一直等着。
     */
    fun targetsDb(version: String? = null): TargetsDbFetch {
        val ep = endpoints()
        val h = HashMap(headers(ep))
        version?.takeIf { it.isNotBlank() }?.let { h["If-None-Match"] = "\"${it.trim('"')}\"" }
        val reply = transport.get(ep.api("/v1/targets/db"), h, TARGETS_TIMEOUT_MS)
        if (reply.status == 304) return TargetsDbFetch.NotModified
        val code = if (reply.ok) null else ApiParse.errorCode(reply.text())
        if (reply.status == 503 && code == "targets_building") return building(reply)
        if (reply.status == 404 && code == "no_targets") return TargetsDbFetch.Empty
        check(reply, "/v1/targets/db")
        if (reply.body.isEmpty()) {
            // 0 字节写进缓存的话，下一次 deserialize 会失败，而那个失败会被归因成
            // 「服务端的 arcoreimg 比端上的 ARCore 新」并退回端上现建 —— 一个真实
            // 原因是「下了个空文件」的问题于是永远查不到。
            throw HttpFailure(NetErrorKind.BAD_RESPONSE, null, "整库目标：拿到 0 字节")
        }
        return TargetsDbFetch.Fresh(reply.body, unquote(reply.header("ETag")))
    }

    private fun building(reply: HttpReply): TargetsDbFetch.Building {
        val body = try {
            JSONObject(reply.text())
        } catch (_: Exception) {
            null
        }
        // 头优先于体：`Retry-After` 是 HTTP 的契约，中间还可能有代理改它。体里那个
        // 是服务端顺手多给的一份，用来兜住「头被剥掉了」。
        val fromHeader = reply.header("Retry-After")?.trim()?.toIntOrNull()?.takeIf { it > 0 }
        val fromBody = body?.optInt("retryAfterS", 0)?.takeIf { it > 0 }
        return TargetsDbFetch.Building(
            version = body?.let { if (it.isNull("version")) null else it.optString("version", "") }
                ?.takeIf { it.isNotEmpty() },
            retryAfterS = fromHeader ?: fromBody,
        )
    }

    /**
     * ETag → version。服务端发的是 `"<version>"`；反代可能在前面加弱校验前缀 `W/`。
     *
     * 存不带引号的那个值，是为了它能和 manifest 里的 `version` **直接相等** ——
     * 那是客户端唯一能自己验证「这份元数据与这些字节是配好的」的判据。
     */
    private fun unquote(etag: String?): String? =
        etag?.trim()?.removePrefix("W/")?.trim('"')?.takeIf { it.isNotEmpty() }

    // ---- 外壳侧（§7 剩下那半边）----

    fun ping(): Boolean {
        val ep = endpoints()
        return transport.get(ep.api("/v1/ping"), headers(ep), META_TIMEOUT_MS).ok
    }

    fun photos(): List<PhotoSummary> = getJson("/v1/photos", CatalogParse::photos)

    fun photoDetail(photoId: String): PhotoDetail =
        getJson("/v1/photo/$photoId", CatalogParse::photoDetail)

    /** [path] 为 null 时返回白名单根目录列表（§7）。 */
    /**
     * 这个 NAS 路径在库里是什么身份。
     *
     * 用途是**重复上传不该是死胡同**：`POST /v1/photo` 回 409 `already_ingested` 时，
     * 拿这个接口问出「那张照片是哪一张、现在配的是哪段视频」，好让用户接着决定要不要换。
     */
    fun lookup(path: String): LookupResult =
        getJson("/v1/admin/lookup?path=" + urlEncode(path), CatalogParse::lookup)

    fun fsList(path: String?): FsListing {
        val url = if (path.isNullOrEmpty()) "/v1/fs/list"
        else "/v1/fs/list?path=" + urlEncode(path)
        return getJson(url, CatalogParse::fsList)
    }

    /** 文件选择器的缩略图（长边 320）。走 api 通道：小包，且与列表同源。 */
    fun fsThumb(path: String): ByteArray = download("/v1/fs/thumb?path=" + urlEncode(path))

    fun history(limit: Int = 50): List<HistoryEntry> =
        getJson("/v1/history?limit=$limit", CatalogParse::history)

    /** 关联 NAS 上已有的文件（§7 `POST /v1/photo`）。质量分不达标会抛 [HttpFailure]。 */
    fun createPhoto(
        refPath: String,
        videoPath: String?,
        printWidthMm: Double,
        title: String?,
    ): CreateResult = postJson(
        "/v1/photo",
        CatalogParse.createBody(refPath, videoPath, printWidthMm, title),
        INGEST_TIMEOUT_MS,
        CatalogParse::createResult,
    )

    /** 给已有照片补 / 换视频。 */
    fun attachVideo(photoId: String, videoPath: String): AttachResult = postJson(
        "/v1/photo/$photoId/video",
        CatalogParse.attachBody(videoPath),
        INGEST_TIMEOUT_MS,
        CatalogParse::attachResult,
    )

    /**
     * 换掉这张照片的**参考图**，photoId 不变。
     *
     * 服务端会重算质量分、特征、自匹配分，重建 imgdb 与缩略图，并原地替换识别库里那个
     * slot —— 所以**授权、配的视频、标题、打印宽度全都留着**（走「删掉重建」的话授权会
     * 被级联删除）。用 `INGEST_TIMEOUT_MS` 而不是 META：这条路上的活和入库一样重
     * （arcoreimg + 20 次扰动查询），几秒到几十秒。
     */
    fun replaceRef(photoId: String, refPath: String): ReplaceRefResult = postJson(
        "/v1/photo/$photoId/ref",
        CatalogParse.refBody(refPath),
        INGEST_TIMEOUT_MS,
        CatalogParse::replaceRefResult,
    )

    /**
     * **上传之前**问一次：这个文件是不是已经在服务端了。
     *
     * 一次请求几百字节，换掉的是「传完 20 MB 才收到一句已存在」那几十秒白等。
     * 哈希在本地算（见 `MediaScreen` 里的 `sha256Of`）。
     *
     * 超时用 [META_TIMEOUT_MS]：服务端这一步只做一次哈希比对和两次索引查询，不碰重活。
     */
    fun uploadCheck(name: String, sha256: String): UploadCheck = postJson(
        "/v1/upload/check",
        CatalogParse.uploadCheckBody(name, sha256),
        META_TIMEOUT_MS,
        CatalogParse::uploadCheck,
    )

    /**
     * 把一个文件传到 NAS 上的 `PHOTOAR_UPLOAD_DIR`，返回它在服务端的绝对路径。
     *
     * @param name 纯文件名，不能带路径分隔符（服务端会拒）。
     * @param length 字节数，必须准确 —— 服务端只按 Content-Length 读体，数错了会挂住
     *   而不是报错（见 [HttpTransport.postStream]）。
     * @param write 往输出流里写内容。调用方负责关掉自己的输入流。
     *
     * 走的是**流式**上传，不把文件读进内存：一段婚礼视频几百 MB，读成 ByteArray
     * 会在手机上 OOM。
     *
     * ⚠️ 这个接口在隧道上会被服务端按 `cf-ray` 头挡掉（413，§9.4 的 100MB 上限）。
     * 调用之前应该先看 `EndpointCenter.uploadAllowed()`，别让用户白等一次失败。
     */
    fun upload(
        name: String,
        mime: String,
        length: Long,
        write: (java.io.OutputStream) -> Unit,
    ): String {
        val ep = endpoints()
        val reply = transport.postStream(
            url = ep.api("/v1/upload?name=" + urlEncode(name)),
            contentType = mime,
            length = length,
            headers = headers(ep),
            // 连接超时用普通的接口超时（连不上就是连不上），读超时由 postStream
            // 自己按上传的量级定。
            timeoutMs = META_TIMEOUT_MS,
            write = write,
        )
        check(reply, "/v1/upload")
        return parse { CatalogParse.uploadedPath(reply.text()) }
    }

    private inline fun <T> getJson(relative: String, mapper: (String) -> T): T {
        val ep = endpoints()
        val reply = transport.get(ep.api(relative), headers(ep), META_TIMEOUT_MS)
        check(reply, relative)
        return parse { mapper(reply.text()) }
    }

    private inline fun <T> postJson(
        relative: String,
        body: String,
        timeoutMs: Int,
        mapper: (String) -> T,
    ): T {
        val ep = endpoints()
        val reply = transport.postJson(ep.api(relative), body, headers(ep), timeoutMs)
        check(reply, relative)
        return parse { mapper(reply.text()) }
    }

    /**
     * NAS 路径进 query string。`URLEncoder` 是 form 编码，会把空格变成 `+`，
     * 而服务端按 `parse_qs` 解 —— `+` 在 query 里确实解回空格，所以这里是对的。
     * 斜杠被编成 `%2F` 也没问题，同样由 `parse_qs` 还原。
     */
    private fun urlEncode(s: String): String =
        java.net.URLEncoder.encode(s, "UTF-8")

    /** @param authorize false 只用于登录（那是唯一免鉴权的接口，见 [login]）。 */
    private fun headers(ep: Endpoints, authorize: Boolean = true): Map<String, String> {
        val h = HashMap<String, String>(4)
        if (authorize) h["Authorization"] = "Bearer ${ep.token}"
        h["Accept-Encoding"] = "identity"
        viaLabel()?.let { h["X-PhotoAR-Endpoint"] = it }
        return h
    }

    private fun check(reply: HttpReply, what: String) {
        if (reply.ok) return
        val text = reply.text()
        val msg = ApiParse.errorMessage(reply.status, text)
        // 401 与其它错误的差别是「重试有没有意义」：token 错了重试一万次也一样，
        // 状态机看到 UNAUTHORIZED 会停下来让人去重新登录。
        //
        // 403 在**普通接口**上仍然映射成 UNAUTHORIZED，与 401 同处理。它在这里的含义
        // 是「这个身份不够」（admin_only、path_denied），而用户能做的事和过期一样：
        // 换一个身份登录。登录接口自己那条路分得更细，见 [checkLogin]。
        val kind = when {
            reply.status == 401 || reply.status == 403 -> NetErrorKind.UNAUTHORIZED
            reply.status >= 500 -> NetErrorKind.SERVER_ERROR
            else -> NetErrorKind.BAD_RESPONSE
        }
        throw HttpFailure(kind, reply.status, "$what → $msg", ApiParse.errorCode(text))
    }

    private inline fun <T> parse(block: () -> T): T =
        try {
            block()
        } catch (e: ApiParseException) {
            throw HttpFailure(NetErrorKind.BAD_RESPONSE, null, e.message ?: "响应解析失败")
        }
}

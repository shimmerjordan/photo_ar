package app.photoar.arview.net

import app.photoar.arview.ApiParse
import app.photoar.arview.ApiParseException
import app.photoar.arview.AttachResult
import app.photoar.arview.CatalogParse
import app.photoar.arview.CreateResult
import app.photoar.arview.Endpoints
import app.photoar.arview.FsListing
import app.photoar.arview.HistoryEntry
import app.photoar.arview.Hit
import app.photoar.arview.MediaInfo
import app.photoar.arview.NetErrorKind
import app.photoar.arview.PhotoDetail
import app.photoar.arview.PhotoSummary
import app.photoar.arview.RecognizeOutcome

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

        /** 单目标 .imgdb 实测约 4.3KB、缩略图约 60KB，10s 连走隧道都够。 */
        const val DOWNLOAD_TIMEOUT_MS = 10_000

        /**
         * 入库要跑 eval-img + ORB + build-db + ffmpeg 转码（§8.1），一条视频几十秒
         * 很正常。这条超时不是「网络慢」而是「服务端在干活」，所以给到 3 分钟 ——
         * 比它更短会在 N5095 上把正常的长视频入库判成失败。
         */
        const val INGEST_TIMEOUT_MS = 180_000
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

    fun media(hit: Hit): MediaInfo {
        val ep = endpoints()
        val reply = transport.get(ep.api(hit.mediaUrl), headers(ep), META_TIMEOUT_MS)
        check(reply, hit.mediaUrl)
        return parse { ApiParse.media(reply.text()) }
    }

    /** 下 imgdb 或 thumb。两个都走 **api** 通道：它们是小包，且与识别同源。 */
    fun download(relativeUrl: String): ByteArray {
        val ep = endpoints()
        val reply = transport.get(ep.api(relativeUrl), headers(ep), DOWNLOAD_TIMEOUT_MS)
        check(reply, relativeUrl)
        return reply.body
    }

    // ---- 外壳侧（§7 剩下那半边）----

    fun ping(): Boolean {
        val ep = endpoints()
        return transport.get(ep.api("/v1/ping"), headers(ep), META_TIMEOUT_MS).ok
    }

    fun photos(): List<PhotoSummary> = getJson("/v1/photos", CatalogParse::photos)

    fun photoDetail(photoId: String): PhotoDetail =
        getJson("/v1/photo/$photoId", CatalogParse::photoDetail)

    /** [path] 为 null 时返回白名单根目录列表（§7）。 */
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

    private fun headers(ep: Endpoints): Map<String, String> {
        val h = HashMap<String, String>(3)
        h["Authorization"] = "Bearer ${ep.token}"
        h["Accept-Encoding"] = "identity"
        viaLabel()?.let { h["X-PhotoAR-Endpoint"] = it }
        return h
    }

    private fun check(reply: HttpReply, what: String) {
        if (reply.ok) return
        val msg = ApiParse.errorMessage(reply.status, reply.text())
        // 401 与其它错误的差别是「重试有没有意义」：token 错了重试一万次也一样，
        // 状态机看到 UNAUTHORIZED 会停下来让人去改设置。
        val kind = when {
            reply.status == 401 || reply.status == 403 -> NetErrorKind.UNAUTHORIZED
            reply.status >= 500 -> NetErrorKind.SERVER_ERROR
            else -> NetErrorKind.BAD_RESPONSE
        }
        throw HttpFailure(kind, reply.status, "$what → $msg")
    }

    private inline fun <T> parse(block: () -> T): T =
        try {
            block()
        } catch (e: ApiParseException) {
            throw HttpFailure(NetErrorKind.BAD_RESPONSE, null, e.message ?: "响应解析失败")
        }
}

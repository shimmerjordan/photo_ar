package app.photoar.arview.net

import app.photoar.arview.NetErrorKind
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL

/**
 * HTTP 回复。[body] 对识别响应是 JSON，对 imgdb/thumb 是二进制。
 *
 * [headers] 带默认值是为了不动既有的三个假 transport（它们都只构造 status + body）。
 * 目前唯一用到它的是模型下载的 ETag 协商 —— 那条路必须能读到响应头，否则「服务端换
 * 了一份模型」在手机上永远看不出来。键一律小写，取值走 [header]。
 */
class HttpReply(
    val status: Int,
    val body: ByteArray,
    val headers: Map<String, String> = emptyMap(),
) {
    fun text(): String = String(body, Charsets.UTF_8)
    val ok: Boolean get() = status in 200..299

    /** 大小写不敏感取头。HTTP 头名不区分大小写，而 ETag 的写法各家服务器都不同。 */
    fun header(name: String): String? = headers[name.lowercase()]
}

class HttpFailure(
    val kind: NetErrorKind,
    val status: Int?,
    message: String,
    /**
     * 服务端错误体里的 `error` 字段（机器可读的 code）。
     *
     * 单独带出来而不是让调用方从 message 里 substring：message 是给人看的中文，
     * 改一个字就会把按它分岔的逻辑静默改掉。
     */
    val code: String? = null,
) : IOException(message)

/**
 * 三个动作：GET、POST 一张 JPEG（识别）、POST 一段 JSON（入库 / 关联）。抽成
 * 接口是为了让 [PhotoArClient] 能在 JVM 单测里跑（真机上换成 [UrlTransport]）。
 */
interface HttpTransport {
    fun get(url: String, headers: Map<String, String>, timeoutMs: Int): HttpReply

    fun postJpeg(
        url: String,
        field: String,
        jpeg: ByteArray,
        headers: Map<String, String>,
        timeoutMs: Int,
    ): HttpReply

    fun postJson(
        url: String,
        json: String,
        headers: Map<String, String>,
        timeoutMs: Int,
    ): HttpReply

    /**
     * 流式 POST：请求体由 [write] 往流里写，不在内存里囤。
     *
     * 这个方法是给**上传**用的，而上传的体可能是一段几百 MB 的视频 —— 先读成
     * ByteArray 再发会在手机上直接 OOM（`/v1/upload` 服务端那侧上限是 2 GiB）。
     *
     * [length] 必须是准确的字节数：服务端是手写的 `http.server`，只按 Content-Length
     * 读体，不支持 chunked。数错了的后果不是报错而是**挂住**（服务端等不到声明的
     * 字节数）。
     *
     * ## 为什么给了默认实现
     *
     * 测试里有四个假 transport（`PhotoArClientTest`、`CacheSyncTest`、`ModelCacheTest`、
     * `HttpProberTest`），它们全都不上传。加成抽象方法要在四个地方各写一个空实现，
     * 而那四个空实现没有任何一处会被调用。默认实现直接抛，语义是「这个 transport
     * 不支持上传」—— 真要测上传的假 transport 自己覆盖它。
     */
    fun postStream(
        url: String,
        contentType: String,
        length: Long,
        headers: Map<String, String>,
        timeoutMs: Int,
        write: (java.io.OutputStream) -> Unit,
    ): HttpReply = throw UnsupportedOperationException(
        "这个 HttpTransport 不支持流式上传（${this::class.java.simpleName}）",
    )
}

/**
 * `HttpURLConnection` 实现。不引 OkHttp —— 需要的只有一个 GET 和一个 multipart
 * POST，而 ExoPlayer 自带的 `DefaultHttpDataSource` 已经把 Range + 断点那部分
 * 做了，App 侧再多一个 HTTP 栈没有收益。
 */
/**
 * 上传时等响应的超时。
 *
 * 10 分钟。它等的不是网络往返，是**服务端处理完**：`/v1/upload` 落盘之后没别的事，
 * 但请求体本身可能是几百 MB，而 `readTimeout` 从发出请求头就开始算。给小了的表现是
 * 「上传总是失败，但文件其实传上去了」—— 最难查的一类症状。
 */
private const val UPLOAD_READ_TIMEOUT_MS = 10 * 60 * 1000

class UrlTransport : HttpTransport {

    override fun get(url: String, headers: Map<String, String>, timeoutMs: Int): HttpReply =
        run(url, "GET", headers, timeoutMs, null)

    override fun postJpeg(
        url: String,
        field: String,
        jpeg: ByteArray,
        headers: Map<String, String>,
        timeoutMs: Int,
    ): HttpReply {
        val boundary = "----photoar" + java.lang.Long.toHexString(System.nanoTime())
        val head = (
            "--$boundary\r\n" +
                "Content-Disposition: form-data; name=\"$field\"; filename=\"frame.jpg\"\r\n" +
                "Content-Type: image/jpeg\r\n\r\n"
            ).toByteArray(Charsets.UTF_8)
        val tail = "\r\n--$boundary--\r\n".toByteArray(Charsets.UTF_8)
        val body = ByteArray(head.size + jpeg.size + tail.size)
        head.copyInto(body, 0)
        jpeg.copyInto(body, head.size)
        tail.copyInto(body, head.size + jpeg.size)
        return run(
            url,
            "POST",
            headers + ("Content-Type" to "multipart/form-data; boundary=$boundary"),
            timeoutMs,
            body,
        )
    }

    override fun postJson(
        url: String,
        json: String,
        headers: Map<String, String>,
        timeoutMs: Int,
    ): HttpReply = run(
        url,
        "POST",
        headers + ("Content-Type" to "application/json; charset=utf-8"),
        timeoutMs,
        json.toByteArray(Charsets.UTF_8),
    )

    override fun postStream(
        url: String,
        contentType: String,
        length: Long,
        headers: Map<String, String>,
        timeoutMs: Int,
        write: (java.io.OutputStream) -> Unit,
    ): HttpReply {
        val conn = try {
            URL(url).openConnection() as HttpURLConnection
        } catch (e: Exception) {
            throw HttpFailure(NetErrorKind.TRANSPORT, null, "URL 不可用：$url（${e.message}）")
        }
        try {
            conn.requestMethod = "POST"
            conn.connectTimeout = timeoutMs
            // readTimeout **不**用 timeoutMs：上传一段视频要几十秒到几分钟，而
            // timeoutMs 是按「一次接口调用」定的。这里的读超时是等**响应**，而响应
            // 只有在整个体发完之后才会来 —— 用接口的超时值会在上传还没发完时就
            // 判超时，表现是「每次上传都失败，但文件其实传上去了」。
            conn.readTimeout = UPLOAD_READ_TIMEOUT_MS
            conn.useCaches = false
            conn.doOutput = true
            headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
            conn.setRequestProperty("Content-Type", contentType)
            // 固定长度且**不缓冲**。用 setFixedLengthStreamingMode(Long) 而不是
            // int 那个重载：视频超过 2 GiB 时 int 会溢出成负数。
            conn.setFixedLengthStreamingMode(length)
            conn.outputStream.use(write)
            val status = conn.responseCode
            val stream: InputStream? =
                if (status in 200..299) conn.inputStream else conn.errorStream
            val data = stream?.use { readAll(it) } ?: ByteArray(0)
            return HttpReply(status, data, responseHeaders(conn))
        } catch (e: SocketTimeoutException) {
            throw HttpFailure(NetErrorKind.TIMEOUT, null, "上传超时：$url")
        } catch (e: HttpFailure) {
            throw e
        } catch (e: Exception) {
            throw HttpFailure(NetErrorKind.TRANSPORT, null, e.message ?: e.toString())
        } finally {
            conn.disconnect()
        }
    }

    private fun run(
        url: String,
        method: String,
        headers: Map<String, String>,
        timeoutMs: Int,
        body: ByteArray?,
    ): HttpReply {
        val conn = try {
            URL(url).openConnection() as HttpURLConnection
        } catch (e: Exception) {
            throw HttpFailure(NetErrorKind.TRANSPORT, null, "URL 不可用：$url（${e.message}）")
        }
        try {
            conn.requestMethod = method
            conn.connectTimeout = timeoutMs
            conn.readTimeout = timeoutMs
            conn.useCaches = false
            conn.instanceFollowRedirects = true
            headers.forEach { (k, v) -> conn.setRequestProperty(k, v) }
            if (body != null) {
                conn.doOutput = true
                // 固定长度，别让它自己用 chunked：手写的服务端只按
                // Content-Length 读体（spec §7 的 multipart 就一个字段）。
                conn.setFixedLengthStreamingMode(body.size)
                conn.outputStream.use { it.write(body) }
            }
            val status = conn.responseCode
            val stream: InputStream? =
                if (status in 200..299) conn.inputStream else conn.errorStream
            val data = stream?.use { readAll(it) } ?: ByteArray(0)
            return HttpReply(status, data, responseHeaders(conn))
        } catch (e: SocketTimeoutException) {
            throw HttpFailure(NetErrorKind.TIMEOUT, null, "超时：$url")
        } catch (e: HttpFailure) {
            throw e
        } catch (e: Exception) {
            throw HttpFailure(NetErrorKind.TRANSPORT, null, e.message ?: e.toString())
        } finally {
            conn.disconnect()
        }
    }

    /**
     * 响应头，键统一小写。
     *
     * `headerFields` 里会有一个 **key 为 null** 的条目（那是状态行 "HTTP/1.1 200 OK"），
     * 直接 `associate` 会 NPE 掉整个请求 —— 而它只在真机上才出现，本地假 transport
     * 永远不会有。所以这里显式跳过 null 键。
     */
    private fun responseHeaders(conn: HttpURLConnection): Map<String, String> {
        val out = HashMap<String, String>(8)
        for ((key, values) in conn.headerFields) {
            if (key == null) continue
            values?.lastOrNull()?.let { out[key.lowercase()] = it }
        }
        return out
    }

    private fun readAll(input: InputStream): ByteArray {
        val out = ByteArrayOutputStream(1 shl 15)
        val buf = ByteArray(1 shl 14)
        while (true) {
            val n = input.read(buf)
            if (n < 0) break
            out.write(buf, 0, n)
        }
        return out.toByteArray()
    }
}

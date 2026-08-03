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
}

/**
 * `HttpURLConnection` 实现。不引 OkHttp —— 需要的只有一个 GET 和一个 multipart
 * POST，而 ExoPlayer 自带的 `DefaultHttpDataSource` 已经把 Range + 断点那部分
 * 做了，App 侧再多一个 HTTP 栈没有收益。
 */
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

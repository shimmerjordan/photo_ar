package app.photoar.arview.net

import app.photoar.arview.NetErrorKind
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.net.HttpURLConnection
import java.net.SocketTimeoutException
import java.net.URL

/** HTTP 回复。[body] 对识别响应是 JSON，对 imgdb/thumb 是二进制。 */
class HttpReply(val status: Int, val body: ByteArray) {
    fun text(): String = String(body, Charsets.UTF_8)
    val ok: Boolean get() = status in 200..299
}

class HttpFailure(
    val kind: NetErrorKind,
    val status: Int?,
    message: String,
) : IOException(message)

/**
 * 只有两个动作：GET 和 POST 一张 JPEG。抽成接口是为了让 [PhotoArClient] 能在
 * JVM 单测里跑（真机上换成 [UrlTransport]）。
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
            return HttpReply(status, data)
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

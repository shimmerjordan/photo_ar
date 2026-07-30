package app.photoar.standalone

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.LruCache

/**
 * 缩略图内存缓存。
 *
 * 为什么不用 Coil/Glide：图要带 `Authorization: Bearer`，任何第三方加载器都得为此
 * 塞一个拦截器，而我们已经有 [app.photoar.arview.net.PhotoArClient.download]。这里
 * 需要的全部东西就是「一层 LruCache + decodeByteArray」，写出来比引一个库还短。
 *
 * 8MB 上限：服务端缩略图长边 320（列表）/ 640（参考图回退），一张 320 的 ARGB_8888
 * 解出来约 400KB，够放二十来张 —— 一屏网格的量。真正的磁盘缓存是 Phase 4 的事。
 */
object Thumbs {

    private const val MAX_BYTES = 8 * 1024 * 1024

    private val cache = object : LruCache<String, Bitmap>(MAX_BYTES) {
        override fun sizeOf(key: String, value: Bitmap): Int = value.byteCount
    }

    fun cached(key: String): Bitmap? = cache.get(key)

    /**
     * 取图。[fetch] 是阻塞的（走 HttpURLConnection），调用方负责放到 IO 线程上。
     *
     * 解不出来时返回 null 而不是抛：一张缩略图坏了不该让整个列表变成错误页 ——
     * 服务端 `thumb` 对不认识的格式会给 415，那一格留空就好。
     */
    fun load(key: String, fetch: () -> ByteArray): Bitmap? {
        cache.get(key)?.let { return it }
        val bytes = fetch()
        // inPreferredConfig 不动：RGB_565 能省一半内存，但相纸照片上的色带很明显。
        val bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: return null
        cache.put(key, bmp)
        return bmp
    }

    /** 换了服务器或者重新入库之后旧图就不作数了。 */
    fun clear() = cache.evictAll()
}

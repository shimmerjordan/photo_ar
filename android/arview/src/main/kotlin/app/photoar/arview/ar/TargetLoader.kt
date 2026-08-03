package app.photoar.arview.ar

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Log
import app.photoar.arview.Hit
import app.photoar.arview.net.PhotoArClient
import java.io.File

/**
 * 取目标图数据：`.imgdb` 优先，拿不到退回参考缩略图。
 *
 * 两级缓存都落磁盘。`.imgdb` 是照片内容的函数（服务端给了 ETag +
 * `Cache-Control: immutable`），所以一旦下过就永远有效 —— 同一张照片第二次扫
 * 不再走网络，命中到出画的时间少一个 RTT。
 *
 * 缓存文件名用 photoId，不做 LRU：单目标 imgdb 约 4.3KB，一万张也才 43MB，
 * 而 Phase 4 会给视频那块加 LRU，那才是占空间的。
 */
class TargetLoader(
    private val client: PhotoArClient,
    cacheRoot: File,
) {

    private companion object {
        const val TAG = "TargetLoader"

        /** 缩略图解码后的长边上限。ARCore 对参考图分辨率不敏感，300px 足够。 */
        const val THUMB_MAX_EDGE = 640
    }

    private val dir = File(cacheRoot, "target").apply { mkdirs() }

    sealed interface Target {
        /** 服务端预建的库，物理宽度已烘在里面。 */
        data class Imgdb(val bytes: ByteArray) : Target

        /** 降级：端上现算特征，宽度得自己传。 */
        data class Thumb(val bitmap: Bitmap) : Target
    }

    /**
     * @param cause 原始失败。**必须传**：`ScanRuntime` 靠它认出「其实是 401」——
     *   token 过期时两条下载都会失败，而只有一句拼好的字符串的话，那种情况会被报成
     *   「这张照片的目标装不上」，用户于是去查照片和 .imgdb 文件。
     */
    class LoadFailed(message: String, cause: Throwable? = null) : Exception(message, cause)

    /**
     * @param onFallback imgdb 走不通、改用缩略图时回调一次（界面上要提示，
     *   因为这条路的跟踪质量会差一些）。
     * @throws LoadFailed 两条路都失败。
     */
    fun load(hit: Hit, onFallback: (String) -> Unit): Target {
        val imgdbFile = File(dir, "${hit.photoId}.imgdb")
        readCached(imgdbFile)?.let { return Target.Imgdb(it) }

        val imgdbError = try {
            val bytes = client.download(hit.imgdbUrl)
            if (bytes.isEmpty()) throw LoadFailed("imgdb 是空的")
            writeCached(imgdbFile, bytes)
            return Target.Imgdb(bytes)
        } catch (e: Exception) {
            e.message ?: e.javaClass.simpleName
        }

        Log.w(TAG, "imgdb 取不到，退回缩略图：$imgdbError")
        onFallback(imgdbError)

        val thumbFile = File(dir, "${hit.photoId}.jpg")
        val thumbBytes = readCached(thumbFile) ?: try {
            client.download(hit.refThumbUrl).also { writeCached(thumbFile, it) }
        } catch (e: Exception) {
            throw LoadFailed("imgdb 与缩略图都取不到（$imgdbError / ${e.message}）", e)
        }
        val bitmap = decode(thumbBytes) ?: run {
            // 缓存里那份可能是上次写坏的，删掉让下次重下
            thumbFile.delete()
            throw LoadFailed("缩略图解不出来")
        }
        return Target.Thumb(bitmap)
    }

    private fun decode(bytes: ByteArray): Bitmap? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        val longEdge = maxOf(bounds.outWidth, bounds.outHeight)
        if (longEdge <= 0) return null
        var sample = 1
        while (longEdge / (sample * 2) >= THUMB_MAX_EDGE) sample *= 2
        val opts = BitmapFactory.Options().apply {
            inSampleSize = sample
            // ARCore 的 addImage 只吃 ARGB_8888 / 灰度，RGB_565 会被拒
            inPreferredConfig = Bitmap.Config.ARGB_8888
        }
        return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, opts)
    }

    private fun readCached(f: File): ByteArray? =
        try {
            if (f.isFile && f.length() > 0) f.readBytes() else null
        } catch (e: Exception) {
            null
        }

    private fun writeCached(f: File, bytes: ByteArray) {
        try {
            // 先写临时文件再改名：进程被杀在写一半，下次读到的是残缺文件而
            // 缓存又是「一旦有就永远有效」，那张照片就永久坏了
            val tmp = File(f.parentFile, f.name + ".tmp")
            tmp.writeBytes(bytes)
            if (!tmp.renameTo(f)) {
                tmp.delete()
                f.writeBytes(bytes)
            }
        } catch (e: Exception) {
            Log.w(TAG, "写缓存失败（不影响本次）", e)
        }
    }
}

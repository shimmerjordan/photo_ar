package app.photoar.arview.media

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import java.io.File
import java.io.IOException

/**
 * 把字节写进系统相册。
 *
 * 两条路，按系统版本分：
 *
 * - **API 29+（Android 10 起）**：MediaStore + `RELATIVE_PATH`，**不需要任何权限**。
 *   这是分区存储之后唯一正确的写法。
 * - **API 24-28**：分区存储之前，只能往公共目录写文件再通知 MediaStore 扫描，
 *   而那需要 `WRITE_EXTERNAL_STORAGE` 运行时权限。
 *
 * 两条路都保留是因为 minSdk 是 24。清单里那条权限带了 `maxSdkVersion="28"`，
 * 所以 29+ 的机器上系统根本不会显示这个权限 —— 不写 maxSdkVersion 的话，Android 10+
 * 的用户会在应用信息里看到一个"存储"权限，而 App 实际上从不使用它。
 */
class MediaSaver(private val context: Context) {

    /** 存到哪一类。决定 MediaStore 的集合与公共目录。 */
    enum class Kind { IMAGE, VIDEO }

    class SaveFailed(message: String, cause: Throwable? = null) : IOException(message, cause)

    /**
     * @return 写进相册后的 Uri
     * @throws SaveFailed 写不进去（无权限、配额满、MIME 不支持…）
     */
    fun save(kind: Kind, bytes: ByteArray, mime: String, displayName: String): Uri =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            saveViaMediaStore(kind, bytes, mime, displayName)
        } else {
            saveLegacy(kind, bytes, displayName)
        }

    private fun saveViaMediaStore(
        kind: Kind,
        bytes: ByteArray,
        mime: String,
        displayName: String,
    ): Uri {
        val collection = when (kind) {
            Kind.IMAGE -> MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
            Kind.VIDEO -> MediaStore.Video.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        }
        val dir = when (kind) {
            Kind.IMAGE -> Environment.DIRECTORY_PICTURES
            Kind.VIDEO -> Environment.DIRECTORY_MOVIES
        }
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, displayName)
            put(MediaStore.MediaColumns.MIME_TYPE, mime)
            put(MediaStore.MediaColumns.RELATIVE_PATH, "$dir/${SaveNaming.ALBUM}")
            // IS_PENDING：写完之前别让相册看见这个条目。不置的话，相册 App 会在写到
            // 一半时把它列出来，用户点进去是一张残图，而重新进相册它又好了 —— 这种
            // 现象几乎不可能复现，也就查不出来。
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val resolver = context.contentResolver
        val uri = resolver.insert(collection, values)
            ?: throw SaveFailed("相册拒绝创建条目（$displayName）")
        try {
            resolver.openOutputStream(uri)?.use { it.write(bytes) }
                ?: throw SaveFailed("打不开相册条目的输出流（$displayName）")
        } catch (t: Throwable) {
            // 写失败必须把占位条目删掉。留着的话相册里是一个 0 字节、永远 pending
            // 的幽灵条目，用户看不到、也删不掉。
            runCatching { resolver.delete(uri, null, null) }
            if (t is SaveFailed) throw t
            throw SaveFailed("写入相册失败：${t.message ?: t.javaClass.simpleName}", t)
        }
        values.clear()
        values.put(MediaStore.MediaColumns.IS_PENDING, 0)
        resolver.update(uri, values, null, null)
        return uri
    }

    @Suppress("DEPRECATION")
    private fun saveLegacy(kind: Kind, bytes: ByteArray, displayName: String): Uri {
        val dir = when (kind) {
            Kind.IMAGE -> Environment.DIRECTORY_PICTURES
            Kind.VIDEO -> Environment.DIRECTORY_MOVIES
        }
        val folder = File(Environment.getExternalStoragePublicDirectory(dir), SaveNaming.ALBUM)
        if (!folder.exists() && !folder.mkdirs()) {
            throw SaveFailed("建不出目录：$folder（可能没有存储权限）")
        }
        val out = File(folder, displayName)
        try {
            out.writeBytes(bytes)
        } catch (t: Throwable) {
            throw SaveFailed("写文件失败：${t.message ?: t.javaClass.simpleName}", t)
        }
        // 不通知的话文件在那儿但相册里看不到，直到下次开机重扫 —— 表现为"保存成功
        // 但相册里没有"。
        android.media.MediaScannerConnection.scanFile(
            context, arrayOf(out.absolutePath), null, null,
        )
        return Uri.fromFile(out)
    }
}

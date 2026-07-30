package app.photoar.arview.cache

import java.io.File

/**
 * 进程内唯一的 [PhotoCache]。
 *
 * 为什么必须唯一：扫描页和「缓存管理」页都要读写同一份 `index.json`。两个实例各
 * 持一份内存索引，谁最后 `flush()` 谁赢 —— 表现为「刚同步完的 47 张，回到扫描页
 * 又变回 0 张」，而且不报任何错。[PhotoCache] 内部所有方法都是 `@Synchronized` 的，
 * 所以共用一个实例本身是线程安全的。
 *
 * 根目录用 `filesDir` 而不是 `cacheDir`：`cacheDir` 里的东西系统可以随时删，而
 * 离线缓存被删掉的后果正好是「离线时用不了」—— 那是这份缓存存在的唯一理由。
 * （单目标 `.imgdb` 那份短期缓存仍在 `cacheDir`，见 TargetLoader —— 它丢了只是
 * 多一次下载。）
 */
object OfflineCache {

    /** `filesDir` 下的子目录名。 */
    const val DIR = "photoar"

    private var instance: PhotoCache? = null

    /** @param filesDir 传 `context.filesDir`。 */
    @Synchronized
    fun of(filesDir: File): PhotoCache {
        instance?.let { return it }
        val c = PhotoCache(File(filesDir, DIR)).load()
        instance = c
        return c
    }

    /** 单测用：把单例清掉。 */
    @Synchronized
    fun reset() {
        instance = null
    }
}

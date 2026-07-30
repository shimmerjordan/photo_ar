package app.photoar.arview

import android.content.Context
import android.content.SharedPreferences

/**
 * [EndpointConfig] 的落盘。整份配置序列化成一条 JSON 存在 SharedPreferences 里 ——
 * 候选列表是变长的，拆成 `endpoint0Base` / `endpoint0Prefer` … 这类平铺键会在
 * 「删掉中间一条」时留下孤儿键。
 *
 * 令牌一起存在这里，没有加密。这是刻意的：Keystore 只能防「拿到了文件但没有屏幕
 * 解锁」的情况，而这是一台私有 NAS 的访问令牌，威胁模型里没有那一项；引入
 * Keystore 会换来「换机 / 恢复备份后令牌解不开」这个真实故障。
 */
class EndpointStore(context: Context) {

    companion object {
        /** 与 Phase 2 的 SetupActivity 用同一份 prefs，迁移才读得到旧值。 */
        const val PREFS = "photoar"
        private const val KEY_CONFIG = "endpointConfig"

        // Phase 2 的三个平铺键。迁移之后不删 —— 万一要降级回 Phase 2 的包。
        private const val LEGACY_API = "apiBase"
        private const val LEGACY_MEDIA = "mediaBase"
        private const val LEGACY_TOKEN = "token"
    }

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    /**
     * 读配置。没存过就走 Phase 2 迁移，两者都没有则用 §9.1 的默认值。
     *
     * 迁移**不**在这里落盘：用户可能只是打开一次设置界面看看就退出，那时候把
     * 迁移结果写进去也没坏处，但「读操作不写盘」让这个类少一个失败点。真正的
     * 写入发生在用户第一次点保存时。
     */
    fun load(): EndpointConfig {
        val json = prefs.getString(KEY_CONFIG, null)
        if (!json.isNullOrBlank()) return EndpointConfig.parse(json)
        return EndpointConfig.fromLegacy(
            apiBase = prefs.getString(LEGACY_API, "") ?: "",
            mediaBase = prefs.getString(LEGACY_MEDIA, "") ?: "",
            token = prefs.getString(LEGACY_TOKEN, "") ?: "",
        )
    }

    fun save(config: EndpointConfig) {
        prefs.edit().putString(KEY_CONFIG, config.toJson()).apply()
    }
}

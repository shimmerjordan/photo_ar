plugins {
    id("com.android.application") version "8.7.3" apply false
    id("com.android.library") version "8.7.3" apply false
    id("org.jetbrains.kotlin.android") version "2.1.0" apply false
    // Kotlin 2.x 起 Compose 编译器插件跟着 Kotlin 版本走，不再单独对表。
    id("org.jetbrains.kotlin.plugin.compose") version "2.1.0" apply false
}

import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "app.photoar.standalone"
    compileSdk = 35

    defaultConfig {
        applicationId = "app.photoar"
        minSdk = 24
        targetSdk = 35
        versionCode = 1
        versionName = "0.3.0"

        // 模拟器那两套原生库（x86 / x86_64）真机一个也用不到，但它们不便宜：
        // 实测每个 ABI 里 libonnxruntime.so 单独就 19.4 MiB，两套 ≈ 39 MiB。
        //
        // 默认**全带**：本机跑 emulator 调 AR 兜底路径要用 x86_64，这是日常调试
        // 手段，不能因为发版的事把它砍掉。发版和 CI 传 -Pphotoar.deviceAbiOnly
        // 只留真机两套。
        //
        // 用 gradleProperty 而不是 buildType 区分：debug 也可能要出真机包
        // （真机只认 debug 签名，见 decisions.md §9），所以维度是「给谁装」，
        // 不是「哪个 buildType」。
        // 判值而不是判 isPresent：`-Pphotoar.deviceAbiOnly=false` 也是「存在」，
        // 那样写会让一个明确说「别过滤」的命令行反而触发过滤。裸写 `-P名字`
        // （值为空串）仍然算开。
        val deviceAbiOnly = providers.gradleProperty("photoar.deviceAbiOnly")
            .map { it != "false" }
            .getOrElse(false)
        if (deviceAbiOnly) {
            ndk {
                abiFilters += listOf("arm64-v8a", "armeabi-v7a")
            }
        }
    }

    // ---- 签名：一台机器上、以及 CI 上，所有变体都用**同一个**密钥 ----
    //
    // 解决的是一个具体的坑：Android 拒绝用不同签名的 APK 覆盖已装的那个
    // （`INSTALL_FAILED_UPDATE_INCOMPATIBLE`），而 release 原来沿用 `debug` key ——
    // 那把钥匙是 AGP 在 `~/.android/debug.keystore` 里**每台机器各自生成**的。于是：
    //
    //   · CI runner 上没有那个文件，AGP 每次现生成一把新的 → 每次 CI 出的包签名都不同
    //   · 本地包与 CI 包签名不同 → 真机上两边换着装必须先卸载（数据全丢）
    //
    // 所以把一把**固定的密钥连同口令一起提交进仓库**（keystore/photoar-release.jks）。
    // 口令写在源码里不是疏忽：这把钥匙的用途就是「让任何人构建出的包能互相覆盖安装」，
    // 它保护的东西是零 —— 上应用市场要另配一把（见下面 `upload`），那把才需要保密。
    //
    // 优先级：`android/key.properties` 里的上架密钥 > 提交进仓库的固定密钥 > debug。
    // 最后那一档只在「checkout 里没有那个 jks」时才会走到，而它是不能覆盖安装的那一档，
    // 所以要留一行警告 —— 静默降级回去的话，症状是「装不上」而原因在几层之外。
    val uploadProps = rootProject.file("key.properties")
    val hasUploadKey = uploadProps.isFile
    val stableKeystore = rootProject.file("keystore/photoar-release.jks")
    val hasStableKey = stableKeystore.isFile
    if (!hasUploadKey && !hasStableKey) {
        logger.warn(
            "[photoar] ⚠️ 找不到 keystore/photoar-release.jks，退回 debug 密钥 —— " +
                "这台机器出的包与别处出的包**不能互相覆盖安装**。"
        )
    }

    signingConfigs {
        if (hasUploadKey) {
            // 顶部的 import 由下面那一行提供 —— `java.util.Properties` 在
            // build.gradle.kts 里必须显式 import（脚本的默认 import 集不含 java.util）。
            val props = Properties()
            uploadProps.inputStream().use { props.load(it) }
            create("upload") {
                storeFile = rootProject.file(props.getProperty("storeFile"))
                storePassword = props.getProperty("storePassword")
                keyAlias = props.getProperty("keyAlias")
                keyPassword = props.getProperty("keyPassword")
            }
        }
        if (hasStableKey) {
            create("stable") {
                storeFile = stableKeystore
                storePassword = "photoarsideload"
                keyAlias = "photoar"
                keyPassword = "photoarsideload"
            }
        }
    }

    val sharedSigningConfig = when {
        hasUploadKey -> signingConfigs.getByName("upload")
        hasStableKey -> signingConfigs.getByName("stable")
        else -> signingConfigs.getByName("debug")
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
        // **每个变体都用同一把钥匙**，debug 也不例外。
        //
        // 用 configureEach 而不是逐个点名：将来加一个 buildType 会自动跟上，
        // 而漏掉一个的后果正是这段注释要防的那件事 —— 而且它不报错，只在真机上
        // 表现成「装不上，要先卸载」。验证方式：
        //   cd android && ./gradlew :app:signingReport
        // 所有变体的 SHA1 必须一样。
        configureEach {
            signingConfig = sharedSigningConfig
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = JavaVersion.VERSION_17.toString()
    }

    buildFeatures {
        buildConfig = false
        compose = true
    }

    // :arview 把 ARCore 运行时拆开塞进了我们的 assets（见 arview/build.gradle.kts
    // 的 ArcoreUnpackTask）。有两类东西必须以未压缩形态落在 APK 里：
    //
    //  1. `*.uncompressed` —— 谷歌自己的运行时包里这 13 个文件（18.4 MiB 的 TPU
    //     计算图、3.8 MiB 的 VIO 模型等）就是 STORED，而同目录的 .tflite 是
    //     DEFLATE。后缀名就是它和 native 之间的契约：那边用 AAsset_getBuffer
    //     直接拿指针，压了要先整份解压到堆上，白占十几兆常驻内存。
    //  2. `dex.jar` —— 首启要整份拷到 codeCacheDir 给 DexClassLoader。压了就多
    //     一次 inflate，而 dex 压缩率不到 5%，纯亏。
    //
    // 注意 noCompress 是**应用级**开关，library 里声明无效 —— 所以将来 Phase 3
    // 的 Flutter 外壳把 :arview include 进去时，那个 app 模块也得补这一段。
    // 忘了不会报错，只会变慢并多占内存。
    androidResources {
        noCompress.add("uncompressed")
        noCompress.add("jar")
    }
}

dependencies {
    implementation(project(":arview"))

    // BOM 统一对齐 compose 各模块版本，下面就不再写版本号。
    val compose = platform("androidx.compose:compose-bom:2024.12.01")
    implementation(compose)
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-graphics")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.9.3")

    // 缩略图不用 Coil：图要带 Authorization 头，第三方加载器都得为此塞
    // 拦截器，而我们已经有 PhotoArClient.download() —— 自己配一个 LruCache
    // 比引一个库更短，也少一份依赖。
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
}

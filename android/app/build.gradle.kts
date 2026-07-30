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
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // 沿用 debug key：这个包只装自己的机器，不上应用市场。
            signingConfig = signingConfigs.getByName("debug")
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

plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "app.photoar.arview"
    compileSdk = 35

    defaultConfig {
        // ARCore 自身要求 24。§5.6 的依赖里最高的下限就是它。
        minSdk = 24
        consumerProguardFiles("consumer-rules.pro")
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
    }

    // 故意【不】开 unitTests.isReturnDefaultValues。开了以后 android.util.Log
    // 之类的调用会静默返回默认值，纯逻辑类里混进 Android 依赖也测得过 ——
    // 而这个模块的测试价值全在「状态机与几何是纯的、能在 JVM 上跑」。
    // 让它抛异常，混进去就立刻暴露。
}

dependencies {
    implementation("com.google.ar:core:1.54.0")
    implementation("androidx.media3:media3-exoplayer:1.5.1")
    implementation("androidx.media3:media3-datasource:1.5.1")

    testImplementation("junit:junit:4.13.2")
    // android.jar 里的 org.json 是会抛异常的存根，JVM 单测必须自带一份真的。
    // testImplementation 的顺序在 mockable android.jar 之前，所以能盖住它。
    testImplementation("org.json:json:20240303")
}

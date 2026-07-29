pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "photo-ar-android"

// :arview 是全部 AR 逻辑所在的 library；:app 只是个能单独装机的启动壳。
// Phase 3 的 Flutter 外壳会用相对路径把 :arview include 进它自己的构建，
// 所以这里必须保持 :arview 不依赖 :app 的任何东西。
include(":arview")
include(":app")

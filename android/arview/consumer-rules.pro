# ARCore 的 native 层通过 JNI 反查这些类，混淆掉会在运行时抛 UnsatisfiedLink。
-keep class com.google.ar.core.** { *; }

# 同样是 native 按名字 + 签名反查的（GetStaticMethodID），只是这份是我们自己写的
# 替身，顶掉 SessionCreateJniHelper 的版本号检查。见 ArCoreEmbeddedRuntime。
# 类名可以被改（native 拿的是我们主动交出去的 Class 对象），四个方法名不能。
-keepclassmembers class app.photoar.arview.ar.ArCoreEmbeddedRuntime$EmbeddedApkInfo {
    public static <methods>;
}

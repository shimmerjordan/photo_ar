# ARCore 的 native 层通过 JNI 反查这些类，混淆掉会在运行时抛 UnsatisfiedLink。
-keep class com.google.ar.core.** { *; }

package app.photoar.arview.ar

import android.app.Application
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import android.os.Build
import android.os.IBinder
import android.util.Log
import dalvik.system.DexClassLoader
import java.io.File

/**
 * 让 ARCore 的运行时**在我们自己的进程里**跑起来，不要求 `com.google.ar.core`
 * 被安装 —— 于是宾客只装一个 APK，中途不再弹「还要装一个应用」。
 *
 * ## 为什么这件事做得到
 *
 * ARCore 分两半：我们包里的客户端桩（`libarcore_sdk_jni.so` + `libarcore_sdk_c.so`，
 * 加起来 168 KB）和真正干活的 `libarcore_c.so`（arm64 36.7 MB，SLAM/VIO/图像跟踪
 * 全在里面）。桩去找那 36.7 MB 的方式，是**JNI 按类名反射**调一座 Java 桥：
 *
 * ```
 *   Session 静态块  System.loadLibrary("arcore_sdk_jni")
 *        ↓
 *   libarcore_sdk_c.so  LoadSymbolsDynamite()        ← 只导入 dlsym，没有 dlopen
 *        ↓ FindClass + GetStaticMethodID
 *   com.google.vr.dynamite.client.DynamiteClient
 *       .loadNativeRemoteLibrary(ctx, "com.google.ar.core", "arcore_c") → long
 *        ↓ createPackageContext("com.google.ar.core")        ← 唯一要求「已安装」的一步
 *        ↓ 远程 ClassLoader.loadClass(...).newInstance() as IBinder
 *   com.google.vr.dynamite.LoadedInstanceCreator
 *        ↓ queryLocalInterface(...)                          ← 同进程才返回真对象
 *   INativeLibraryLoader.initializeAndLoadNativeLibrary("arcore_c") → dlopen handle
 *        ↓ dlsym × 537 个 Ar* 符号
 * ```
 *
 * 三件事让「换掉这座桥」成立，都是从 aar 的字节码和 ELF 里直接读出来的：
 *
 *  1. **不是跨进程 IPC。** `queryLocalInterface` 只在同进程返回真对象（跨进程返回
 *     null，得走 Stub.asInterface 的 proxy）；而 `libarcore_c.so` 自己就导出了
 *     `Java_com_google_vr_dynamite_NativeLibraryLoader_nativeDlopen`。那 36.7 MB
 *     本来就是被 dlopen 进**调用方进程**跑的，Play 服务那个包只是它的存放处。
 *  2. **`DynamiteClient` 在整个 aar 里没有任何 Java 调用方**（193 个类的字节码里
 *     零命中）。它纯粹是给 native 反射用的桥 —— 也就是说，它的行为只由它自己的
 *     内部状态决定，没有别处的 Java 代码依赖它「必须真的去查 PackageManager」。
 *  3. **它内部有一个 static 缓存**：`ArrayMap<TargetLibraryInfo, RemoteLibraryLoader>`。
 *     `RemoteLibraryLoader` 的 remote Context 和 creator 都是**惰性字段** ——
 *     非空就直接返回，不再调 `createPackageContext`。
 *
 * 所以：在 [com.google.ar.core.Session] 创建之前，往那个缓存里塞一条**已经填好**的
 * `RemoteLibraryLoader`，把它的 Context 换成我们自造的（ClassLoader 指向 assets 里
 * 那份运行时的 dex），native 走到 Dynamite 时就命中缓存，`createPackageContext`
 * 那一行根本不会执行。
 *
 * ## 运行时的三块料分别在哪
 *
 * 构建期把运行时 APK 拆开（见 arview/build.gradle.kts 的 ArcoreUnpackTask），
 * 三块料各走各的路，运行期只有 dex 需要解压：
 *
 * | 料 | 去处 | 谁负责解出来 |
 * |---|---|---|
 * | `lib/<abi>/` 下的 so | 我们的 jniLibs | 不用解，linker 直接从 apk 里 mmap（见 [nativeSearchPath]） |
 * | `assets/` 下全部（tflite 模型、`packed_profiles/` 设备标定库） | 我们的 assets | 不用解，native 走 `AAssetManager_fromJava` 按名字取 |
 * | `classes*.dex` | assets 里的一个 jar | [ensureDexJar]，首启一次 |
 *
 * 上面刻意不写 `lib/<abi>/` 加通配号 加 `.so` 这种写法：**Kotlin 的块注释会嵌套**，
 * 注释里只要出现斜杠紧跟星号，就开了一层内层注释，于是本段结尾的关闭符只关掉
 * 内层，剩下半个文件全变注释 —— 而编译器一声不响。这个坑在 build.gradle.kts 里
 * 真踩过一次，表现是「android 块从未执行、compileSdk 报没设」，查了很久。
 *
 * assets 直接合并进我们自己的 assets，是为了绕开 `AssetManager.addAssetPath` ——
 * 那是隐藏 API，targetSdk 越高越可能被拦。合并之后假 Context 的 `getAssets()`
 * 直接返回我们 app 自己的 AssetManager 就行，一行反射都不需要。
 *
 * ## 类加载的委派方向（这里错了就 ClassCastException）
 *
 * [DexClassLoader] 的 parent **必须**是我们 app 的 ClassLoader，而且不能反转委派。
 * 运行时的 dex 里**也有**一份 `com.google.vr.dynamite.client.*`；靠 parent-first，
 * `LoadedInstanceCreator` 引用的 `ILoadedInstanceCreator` 会解析到 **aar 提供的
 * 那一份**，也就是我们这边 `queryLocalInterface` 之后 checkcast 的同一个类。
 * 反过来（child-first）两边各拿一份同名接口，转型必炸。
 *
 * ## 失败就回退，不要把 App 带走
 *
 * 每一步都可能因为换了 ARCore 版本、混淆名变了、ROM 限制反射而失败。所有异常
 * 收在这里，[install] 只返回一个枚举 —— 调用方拿到 [Result.FAILED] 就照旧走
 * [ArCoreInstaller] 那条「装 Play 服务」的老路，AR 用不了也还有 §11.9 的兜底。
 */
object ArCoreEmbeddedRuntime {

    private const val TAG = "ArEmbedded"

    /** native 侧硬编码的包名，见 libarcore_sdk_c.so 的字符串表。 */
    private const val RUNTIME_PACKAGE = "com.google.ar.core"

    /**
     * 宿主包名。构建期已经把 `libarcore_c.so` 里独立的 [RUNTIME_PACKAGE] 串
     * 换成了它（见 arview/build.gradle.kts 的 `rehostPackageName`），所以这两个
     * 常量必须一致 —— [start] 会核对。
     *
     * 注意 [RUNTIME_PACKAGE] 仍然有用：Dynamite 那条路的包名是 Java 侧传的，
     * 走的是 [primeDynamiteCache] 预热的缓存，和 so 里的串是两回事。
     */
    private const val HOST_PACKAGE = "app.photoar"

    /** native 侧硬编码的库名（`arcore_c` → `libarcore_c.so`），同上。 */
    private const val RUNTIME_LIB = "arcore_c"

    /** 运行时 dex 里的实现类。aar 的 RemoteLibraryLoader 就是按这个名字反射的。 */
    private const val CREATOR_CLASS = "com.google.vr.dynamite.LoadedInstanceCreator"

    /** AIDL 描述符。`queryLocalInterface` 用它取本地对象。 */
    private const val CREATOR_IFACE = "com.google.vr.dynamite.client.ILoadedInstanceCreator"

    /** 被 native 反射的那座桥。这个名字不会被混淆 —— native 按字面找它。 */
    private const val DYNAMITE_CLIENT = "com.google.vr.dynamite.client.DynamiteClient"

    /**
     * native 侧写死的那个「问问系统装了哪版」的类名。注意是**斜杠**形式 ——
     * native 把这个字符串原样 `NewStringUTF` 交给 `ClassLoader.loadClass`，
     * 没做 `'/'→'.'` 替换（ART 的 DotToDescriptor 只换点，所以斜杠形式照样能解）。
     * 两种写法都拦，是因为换一版 ARCore 它可能改成点号形式。
     */
    private const val HELPER_SLASH = "com/google/ar/core/SessionCreateJniHelper"
    private const val HELPER_DOT = "com.google.ar.core.SessionCreateJniHelper"

    /** aar 的 manifest 声明的下限，[EmbeddedApkInfo.getMinApkVersion] 原样读它。 */
    private const val META_MIN_VERSION = "com.google.ar.core.min_apk_version"

    /**
     * 内嵌那份运行时自己的 versionCode，取自 core aar 的 AndroidManifest
     * （`versionCode="260890400" versionName="1.54.260890400"`）。
     *
     * 这个数不是随便填的「够大就行」：native 在选 SDK 版本字符串时还要拿它跟
     * 171127001 / 180214000 比一次（见 libarcore_sdk_c.so 的 b7a8 附近的两个
     * `csel`），报真实值才会落到 "1.54" 那一支 —— 而 "1.54" 正是我们这份 aar
     * 的 SDK 版本，也就是说**诚实的值恰好也是唯一正确的值**。
     */
    private const val EMBEDDED_VERSION_CODE = 260_890_400

    /** 构建期塞进 assets 的 dex 包（一个 zip，里面是 classes*.dex）。 */
    private const val DEX_ASSET = "arcore_rt/dex.jar"

    /**
     * aar 声明的运行时版本下限，见 core-*.aar 的 AndroidManifest：
     * `com.google.ar.core.min_apk_version`。系统装的那份低于这个数，用它反而会
     * 在 `Session` 构造时抛 `UnavailableApkTooOldException`，不如用内置的。
     */
    private const val MIN_RUNTIME_VERSION = 260_760_000L

    enum class Phase {
        /** 还在解 dex / 反射。首启才会看到，几百毫秒。 */
        PREPARING,

        /** 注入成功，`Session` 可以直接建，不需要装任何东西。 */
        EMBEDDED,

        /** 系统已经装了够新的运行时，让它走原生路径 —— 它能跟着 Play 更新。 */
        SYSTEM,

        /** 注入失败。调用方回退到 [ArCoreInstaller]。原因已打进 logcat。 */
        FAILED,
    }

    @Volatile
    private var phase = Phase.PREPARING

    private var started = false

    /**
     * 真正的 Application。[remoteAwareApplication] 要包的就是它。
     *
     * 存在的理由是「拿不到第二次」：那个方法只有 [SessionContext] 会调，而它手上
     * 唯一的 Context 是自己 —— 沿着 `applicationContext` 往上走会撞回自己的 override。
     * 这里在 [start] 入口处、还没有任何 wrapper 的时候把它记下来。
     */
    @Volatile
    private var realApp: Context? = null

    /**
     * 开始准备内嵌运行时。**幂等、非阻塞**，可以在主线程调。
     *
     * 为什么非阻塞：首启要把 40 MB 的 dex 从 assets 拷到 codeCacheDir，几百毫秒 ——
     * 放在主线程就是一次可见的卡顿，而且恰好卡在用户刚点开扫描的那一刻。
     *
     * 调用方不需要等：[phase] 在准备期间是 [Phase.PREPARING]，[ArCheck.state] 把它
     * 映射成 [ArRuntimeState.CHECKING]，于是复用了 [ArInstallPolicy] 现成的
     * 「RECHECK 轮询」—— 8 × 800ms 的宽限期本来是留给 ARCore 那个异步查询的，
     * 对解 dex 来说绰绰有余。
     *
     * **必须在建 [com.google.ar.core.Session] 之前完成**。符号表是一次性填的
     * （native 侧 `number_of_symbols_loaded` 只打一次），`Session` 已经加载过符号
     * 之后再改缓存没有任何意义 —— 所以调用方要等到 [phase] 不是 PREPARING 才建。
     */
    fun start(context: Context) {
        val app = context.applicationContext
        realApp = app
        synchronized(this) {
            if (started) return
            started = true
        }
        // 构建期把 so 里的 `com.google.ar.core` 换成了 HOST_PACKAGE（见
        // arview/build.gradle.kts 的 rehostPackageName）。两处写死的包名要是对不上，
        // native 会去查一个不存在的包，然后带着 pending exception 继续 JNI ——
        // ART 直接 abort 进程，而崩溃点在 `Session()` 里，和包名毫无关系。
        // 在这里先说出来，比在那种日志里反推便宜得多。
        if (app.packageName != HOST_PACKAGE) {
            Log.w(
                TAG,
                "包名对不上：实际 ${app.packageName}，so 里补的是 $HOST_PACKAGE。" +
                    "改过 applicationId？把 build.gradle.kts 的 HOST_PACKAGE 一起改。",
            )
        }
        // 裸 Thread 而不是线程池：这件事一个进程只做一次，池子的意义是复用。
        Thread({
            phase = try {
                doInstall(app)
            } catch (e: Throwable) {
                // 反射链上任何一环变了都到这儿。不抛：AR 用不了不是致命的，
                // 把 App 带走才是。
                Log.w(TAG, "内嵌运行时注入失败，回退到安装 Play 服务那条路", e)
                Phase.FAILED
            }
        }, "arcore-embed").start()
    }

    /** 当前相位。见 [start]。 */
    fun phase(): Phase = phase

    /**
     * 建 [com.google.ar.core.Session] 时该传的 Context。**全工程只有
     * [ArSessionHolder.create] 调它。**
     *
     * ## 为什么建会话还要再包一层
     *
     * [primeDynamiteCache] 只解决了「那 36.7 MB 从哪 dlopen」。在走到 Dynamite
     * 之前，native 还先问了一句「系统装的是哪一版」，而那一问是死路：
     *
     * ```
     *   SessionCreateJniHelper.getArCoreApkVersionCode(ctx)
     *       → getPackageInfo("com.google.ar.core")  → NameNotFoundException
     *       → -1
     *   native: "APK version code: -1 from package com.google.ar.core"
     *   native: if (v == -1) return AR_UNAVAILABLE_ARCORE_NOT_INSTALLED   ← 就死在这
     * ```
     *
     * 这一支在 libarcore_sdk_c.so 里是 `cmn w27,#1` 紧跟 `b.eq`，**排在所有版本
     * 比较之前**。所以「把 manifest 里的 `min_apk_version` 改小」那种一行改法是
     * 无效的 —— -1 根本不参与比较。唯一的出路是换掉这个类。
     *
     * ## 换类为什么不用同名、不用改构建
     *
     * native 拿这个类的方式不是 JNI `FindClass`（那样只能靠类路径顺序，得把原类
     * 从 aar 的 classes.jar 里剔出去），而是：
     *
     * ```
     *   e274: GetObjectClass(ctx) → GetMethodID("getClassLoader")
     *         → CallObjectMethod                      ← 虚调，override 能生效
     *   e3f0: FindClass("java/lang/ClassLoader") → GetMethodID("loadClass")
     *         → CallObjectMethod(loader, loadClass, "com/google/ar/core/…")
     * ```
     *
     * 也就是说**它用的是我们递给 `Session` 的那个 Context 上的 ClassLoader**，
     * 而 `Session(Context, Set)` 的字节码把 Context 原样传给了
     * `nativeCreateSessionAndWrapperWithFeatures`（没有 `getApplicationContext()`、
     * 没有包装）。于是一个只 override `getClassLoader()` 的 ContextWrapper 就够。
     *
     * 而 `GetStaticMethodID` 只按**方法名 + 签名**匹配，从不校验类名，所以替身
     * [EmbeddedApkInfo] 可以老老实实待在我们自己的包里 —— 不需要伪装成
     * `com.google.ar.core.SessionCreateJniHelper`，也就不会跟 aar 里那份撞名。
     *
     * ## 系统装了就原样返回
     *
     * [Phase.SYSTEM] 时返回 `base` 本身：那条路径下 native 问到的是真实版本号，
     * 一切照常，没有理由去动它。同理 [Phase.FAILED]／[Phase.PREPARING] 也不包 ——
     * 包了反而会骗 native 说「装了」，然后死在后面真正缺料的那一步，日志更难看。
     */
    fun sessionContext(base: Context): Context =
        if (phase == Phase.EMBEDDED) SessionContext(base) else base

    private fun doInstall(app: Context): Phase {
        // 系统那份够新就用系统的：它会跟着 Play 更新，设备标定档案也是最新的，
        // 而我们内置的那份钉死在构建时的版本。
        systemRuntimeVersion(app)?.let { v ->
            if (v >= MIN_RUNTIME_VERSION) {
                Log.i(TAG, "系统已装 ARCore 运行时 $v，走原生路径")
                return Phase.SYSTEM
            }
            // 太老。不卸载也不升级它 —— 那要用户点授权，正是我们想避开的。
            // 内置那份照样注入：我们完全绕开 createPackageContext，两者不打架。
            Log.i(TAG, "系统那份 ARCore 太老（$v < $MIN_RUNTIME_VERSION），改用内置的")
        }

        val dexJar = ensureDexJar(app)
        val loader = DexClassLoader(
            dexJar.absolutePath,
            // API 26 起这个参数被忽略（ART 自己管 oat 缓存），但签名要求非 null 才
            // 保险 —— 传 codeCacheDir 而不是 null，老机器上也有个正经地方放。
            File(app.codeCacheDir, "arcore_rt/oat").apply { mkdirs() }.absolutePath,
            // 库搜索路径，见 [nativeSearchPath]。运行期一个 .so 都不用自己解压，
            // 但**不能只传 nativeLibraryDir** —— 那个目录在真机上是空的。
            nativeSearchPath(app),
            // parent 必须是我们的 ClassLoader，见类注释里「委派方向」那一段。
            javaClass.classLoader,
        )

        val creator = newCreator(loader)
        val remoteContext = RuntimeContext(app, loader)
        primeDynamiteCache(remoteContext, creator)

        Log.i(TAG, "内嵌 ARCore 运行时注入完成，不需要安装 $RUNTIME_PACKAGE")
        return Phase.EMBEDDED
    }

    /**
     * `libarcore_c.so` 的搜索路径。
     *
     * 运行时那边 `com.google.vr.dynamite.NativeLibraryLoader` 是这么找库的
     * （dexdump 反汇编 `initializeAndLoadNativeLibrary`）：
     *
     * ```
     * 000e: const-class v2, NativeLibraryLoader  // 它自己的类
     * 0026: v7 = v2.getClassLoader()             // ＝下面那个 DexClassLoader
     * 002a: instanceof BaseDexClassLoader        // DexClassLoader 是它子类，走快路
     * 0032: v7.findLibrary("arcore_c")           // 只看 librarySearchPath，不委派 parent
     * 0070: nativeDlopen(路径)                    // 拿到就 dlopen，不做 File.exists()
     * ```
     *
     * 所以这条搜索路径是唯一入口。**只传 `nativeLibraryDir` 是错的**：AGP 从
     * minSdk 23 起默认 `extractNativeLibs=false`，系统装包时不解压 so 而是直接从
     * apk 里 mmap —— 真机上那个目录是空的（`ls .../lib/arm64` 只有 `.` 和 `..`），
     * findLibrary 返回 null，于是 `Failed to find native library: arcore_c`。
     *
     * 补的是 `<apk>!/lib/<abi>` 这种 zip 内形式：`DexPathList` 认它
     * （建成 `NativeLibraryElement(zip, dir)`），条目是 **Stored** 时返回
     * `<apk>!/lib/<abi>/libarcore_c.so`，linker 能直接从包里 mmap。我们的 so
     * 全是 Stored 且 16 KiB 页对齐（AGP 的 `zipalign -p` 保证的），已逐个核过。
     *
     * `nativeLibraryDir` 仍排在最前：哪天有人开了 `useLegacyPackaging`，
     * 解压出来的那份就该先命中。
     */
    private fun nativeSearchPath(app: Context): String {
        val info = app.applicationInfo
        val paths = mutableListOf(info.nativeLibraryDir)
        // 挑哪套 abi：nativeLibraryDir 的末段就是系统给这个进程选的指令集
        // （arm64 / arm / x86_64 / x86），据此只取位数匹配的那组 —— findLibrary
        // 只看条目在不在，不校验 ABI，混进去会把 64 位的 so 喂给 32 位进程。
        val abis = when (File(info.nativeLibraryDir).name) {
            "arm64", "x86_64" -> Build.SUPPORTED_64_BIT_ABIS
            "arm", "x86" -> Build.SUPPORTED_32_BIT_ABIS
            else -> Build.SUPPORTED_ABIS
        }
        val apks = listOfNotNull(info.sourceDir) + (info.splitSourceDirs?.toList() ?: emptyList())
        for (apk in apks) for (abi in abis) paths += "$apk!/lib/$abi"
        Log.i(TAG, "so 搜索路径：${paths.joinToString(" | ")}")
        return paths.joinToString(File.pathSeparator)
    }

    private fun systemRuntimeVersion(app: Context): Long? = try {
        val info = app.packageManager.getPackageInfo(RUNTIME_PACKAGE, 0)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            info.longVersionCode
        } else {
            @Suppress("DEPRECATION")
            info.versionCode.toLong()
        }
    } catch (_: PackageManager.NameNotFoundException) {
        null
    }

    // ------------------------------------------------------------------
    // dex：唯一需要落到磁盘的一块
    // ------------------------------------------------------------------

    /**
     * 把 assets 里的 dex 包解到私有目录。已经解过就直接用。
     *
     * 判据不是「文件在不在」—— 升级 App 换了 ARCore 版本、或者上次解压中途被杀，
     * 都长得像「文件在」。判据是构建期写下的 stamp（`<字节数> <ARCore版本>`）：
     * 文本一致**且**磁盘上那份长度确实对得上，才认。内容哈希要把 40 MB 读一遍，
     * 每次冷启都付这个钱不值得。
     *
     * 先写 `.part` 再 rename，是为了「解了一半被杀」不会留下一个看起来完整的 jar。
     */
    private fun ensureDexJar(app: Context): File {
        val dir = File(app.codeCacheDir, "arcore_rt").apply { mkdirs() }
        val out = File(dir, "dex.jar")
        val marker = File(dir, "dex.jar.stamp")

        val stamp = app.assets.open("$DEX_ASSET.stamp").use {
            it.readBytes().decodeToString().trim()
        }
        val expectedLen = stamp.substringBefore(' ').toLong()
        if (out.isFile && out.length() == expectedLen &&
            marker.isFile && marker.readText().trim() == stamp
        ) {
            return out
        }

        val part = File(dir, "dex.jar.part")
        part.delete()
        app.assets.open(DEX_ASSET).use { ins ->
            part.outputStream().buffered().use { ins.copyTo(it) }
        }
        // 顺序要紧：先 rename 出 jar，再写 marker。反了的话「写完 marker 就被杀」
        // 会留下一个「stamp 说好了、jar 却是旧的」的状态，而那种状态下次会被当成
        // 命中缓存直接用。
        out.delete()
        check(part.renameTo(out)) { "重命名失败：$part → $out" }
        // dex 必须只读，否则 API 26+ 的 ART 会拒绝加载（可写 dex 视为可被篡改）。
        out.setWritable(false, false)
        marker.writeText(stamp)
        Log.i(TAG, "解出运行时 dex：${out.length()} 字节（$stamp）")
        return out
    }

    // ------------------------------------------------------------------
    // 反射：照抄 aar 里 RemoteLibraryLoader 的那两步
    // ------------------------------------------------------------------

    /**
     * 从运行时的 dex 里拿 `ILoadedInstanceCreator`。
     *
     * 这两行是照抄 aar 里 `RemoteLibraryLoader.c(ClassLoader)` 的字节码 ——
     * 无参构造、转 IBinder、`queryLocalInterface`。一模一样，只是 ClassLoader
     * 换成了我们的 [DexClassLoader]，而不是 `createPackageContext` 给的那个。
     */
    private fun newCreator(loader: ClassLoader): Any {
        val binder = loader.loadClass(CREATOR_CLASS)
            .getDeclaredConstructor()
            .newInstance() as IBinder
        return requireNotNull(binder.queryLocalInterface(CREATOR_IFACE)) {
            // 只会在「同名接口来自两个 ClassLoader」时发生 —— 也就是委派方向错了。
            "queryLocalInterface 返回 null：$CREATOR_IFACE 的类身份不一致"
        }
    }

    /**
     * 往 `DynamiteClient` 的 static 缓存里塞一条已经填好的 loader。
     *
     * 混淆名（`f` = TargetLibraryInfo、`e` = RemoteLibraryLoader）**不硬编码**，
     * 全部从 `getRemoteLibraryLoaderFromInfo` 的签名倒推 —— 那个方法名 native
     * 不依赖，但 R8 也没动它；真被混淆了就按「唯一的 private static 单参方法」
     * 兜住。字段同理按**类型**找，不按名字。这样换一版 ARCore 大概率还能用。
     */
    private fun primeDynamiteCache(remoteContext: Context, creator: Any) {
        val dc = Class.forName(DYNAMITE_CLIENT, false, javaClass.classLoader)

        val factory = dc.declaredMethods.firstOrNull {
            it.name == "getRemoteLibraryLoaderFromInfo"
        } ?: dc.declaredMethods.single {
            java.lang.reflect.Modifier.isStatic(it.modifiers) &&
                java.lang.reflect.Modifier.isPrivate(it.modifiers) &&
                it.parameterCount == 1 &&
                it.returnType != Void.TYPE
        }
        val infoClass = factory.parameterTypes[0]   // TargetLibraryInfo
        val loaderClass = factory.returnType       // RemoteLibraryLoader

        // 缓存本身。ArrayMap 是 android.util 的，JVM 单测里没有 —— 所以这个方法
        // 不进单测，靠真机验（见 decisions.md）。
        val cacheField = dc.declaredFields.single {
            java.lang.reflect.Modifier.isStatic(it.modifiers) &&
                java.util.Map::class.java.isAssignableFrom(it.type)
        }.apply { isAccessible = true }

        @Suppress("UNCHECKED_CAST")
        val cache = cacheField.get(null) as MutableMap<Any, Any>

        // key 的两个字段就是 native 传下来的那两个字符串，顺序见
        // DynamiteClient.loadNativeRemoteLibrary 的字节码：new f(包名, 库名)。
        val info = infoClass
            .getDeclaredConstructor(String::class.java, String::class.java)
            .apply { isAccessible = true }
            .newInstance(RUNTIME_PACKAGE, RUNTIME_LIB)

        // 正常路径下 loader 是惰性的：两个字段都 null，用到时才去 createPackageContext
        // 和反射 creator。我们把两个字段直接填上，那两步就永远不会执行。
        val loader = loaderClass
            .getDeclaredConstructor(infoClass)
            .apply { isAccessible = true }
            .newInstance(info)

        loaderClass.declaredFields.single { it.type == Context::class.java }
            .apply { isAccessible = true }
            .set(loader, remoteContext)

        loaderClass.declaredFields.single { it.type.name == CREATOR_IFACE }
            .apply { isAccessible = true }
            .set(loader, creator)

        cache[info] = loader
    }

    // ------------------------------------------------------------------

    /**
     * 冒充「远程包的 Context」。只需要换一样东西：ClassLoader。
     *
     * `getAssets()` 刻意**不**重写 —— 运行时那 43 MB assets 在构建期就合并进了
     * 我们自己的 assets，native 拿 `AAssetManager_fromJava(getAssets())` 按名字
     * 取（`packed_profiles/profiles.toc`、各种 `.tflite`），路径不带包名，所以
     * 我们自己的 AssetManager 就是对的那个。
     *
     * `getPackageName()` 同样不改：native 只用它做日志和取自己的 files 目录，
     * 冒充成 `com.google.ar.core` 反而会让它去写一个我们没有权限的路径。
     */
    private class RuntimeContext(
        base: Context,
        private val loader: ClassLoader,
    ) : ContextWrapper(base) {
        override fun getClassLoader(): ClassLoader = loader

        override fun createPackageContext(name: String, flags: Int): Context =
            fakeRemotePackage(this, name, "RuntimeContext")
                ?: super.createPackageContext(name, flags)

        // `getApplicationContext()` 刻意**不**重写。试过，会把 Dynamite 打回
        // `handle=0`（`Dynamite failed to load remote library`）：Dynamite 那条路要的
        // 是这个 Context 的 **ClassLoader**，而 applicationContext 换成我们的假
        // Application 之后，`getClassLoader()` 委派给真 Application ——
        // 于是 [DexClassLoader] 丢了，运行时那些类一个都找不到。
        // 需要那个逃生口的是 [SessionContext] 那条链，不是这条。
    }

    /**
     * 接住 `createPackageContext("com.google.ar.core")`。
     *
     * 这一步**不在 Dynamite 那条链上**，所以 [primeDynamiteCache] 管不到它：运行时
     * dex 里的 `com.google.ar.core.services.CalibrationContentResolver`（在运行时 APK
     * 的 `classes2.dex` 里，也就是我们解出来塞进 assets 的那份 `dex.jar`，**由我们
     * 自己的 [DexClassLoader] 加载**）会直接拿手里的 Context 去 `createPackageContext`，
     * 为的是用远程包的 AssetManager 读设备标定档案。包没装就是 `NameNotFoundException`，
     * 然后 native 一路报到 `Failed to create calibration config and device profile`
     * → `AR_ERROR_FATAL`，`Session()` 构造直接抛。
     *
     * 返回 `self` 就够，不必造一个新 Context：标定档案那 51 个文件
     * （`packed_profiles/profiles.toc` 加 9 个 `profiles_0000N.dat`）在构建期已经并进
     * 我们自己的 assets，而这两个 wrapper 都没重写 `getAssets()` —— 它委派给 base，
     * 给出的正是那一份。同理 `getResources()`、`getFilesDir()` 也都还是我们的。
     *
     * 只认 [RUNTIME_PACKAGE] 这一个名字：别的包名照旧走 `super`，该抛就抛。冒充成
     * 「任何包都存在」会把真正的配置错误变成更下游的怪毛病。
     */
    private fun fakeRemotePackage(self: Context, name: String?, who: String): Context? {
        if (name != RUNTIME_PACKAGE) return null
        Log.i(TAG, "$who.createPackageContext($name)：包没装，用我们自己的 assets 顶上")
        return self
    }

    /**
     * `getApplicationContext()` 是上面那个 override 的**逃生口**，必须一起堵。
     *
     * 真机日志（2026-07-31）里这条链清清楚楚：
     * ```
     * ArEmbedded: SessionContext.getApplicationContext() 被取走了
     * …
     * at android.content.ContextWrapper.createPackageContext(ContextWrapper.java:1012)
     * at CalibrationContentResolver.readDeviceProfileForFingerprint(PG:37)
     * ```
     * `ContextWrapper.getApplicationContext()` 委派给 base，给出的是**真正的**
     * Application —— 我们那些 override 一个都不在上面。所以
     * [CalibrationContentResolver] 先把 wrapper 剥掉，再去问包，于是
     * [fakeRemotePackage] 压根没被走到。
     *
     * 挡法是让 applicationContext 也是我们的一层。三个细节决定了它必须长这样：
     *
     * - **继承 [Application] 而不是 [ContextWrapper]**：拿 applicationContext 的代码
     *   有权把它 `as Application`（注册 lifecycle 回调就要），返回别的类型是把一个
     *   清楚的失败换成一个隐蔽的 ClassCastException。
     * - **lifecycle 回调转发给真 Application**：我们这个是 `new` 出来的裸对象，系统
     *   不认识它，注册进来的回调永远不会被调 —— 静默失效比崩溃更难查。
     * - **`getApplicationContext()` 返回自己**：不然剥一层之后又回到真 Application，
     *   等于什么都没做。
     *
     * 单例：applicationContext 可能被当 map key 或者拿去比 `==`（ARCore 内部有没有这么
     * 干不好说），每次 new 一个新的会让那种代码行为漂移。
     *
     * 包的是 [realApp]（[start] 一进来就存下的那个）而**不是**参数的
     * `base.applicationContext`。后者写过一版，真机上是 `StackOverflowError`：
     * 传进来的 `base` 就是 [SessionContext] 自己，取它的 applicationContext 又回到
     * 这个方法 —— 8 MB 栈打满，然后 `Session()` 死在一个和 Dynamite 毫无关系的地方
     * （`handle=0`，看着像 so 加载失败）。要真 Application 就必须从别处拿。
     */
    @Volatile
    private var fakeApp: Context? = null

    private fun remoteAwareApplication(): Context? {
        fakeApp?.let { return it }
        // start() 没跑过就没有真 Application 可包 —— 那种情况下 phase 也到不了
        // EMBEDDED，[SessionContext] 压根不会被建出来。
        val real = realApp ?: return null
        return synchronized(this) {
            fakeApp ?: RemoteAwareApplication(real).also { fakeApp = it }
        }
    }

    private class RemoteAwareApplication(private val real: Context) : Application() {

        init {
            attachBaseContext(real)
        }

        override fun getApplicationContext(): Context = this

        override fun createPackageContext(name: String, flags: Int): Context =
            fakeRemotePackage(this, name, "RemoteAwareApplication")
                ?: super.createPackageContext(name, flags)

        override fun registerActivityLifecycleCallbacks(callback: ActivityLifecycleCallbacks?) {
            (real as? Application)?.registerActivityLifecycleCallbacks(callback)
        }

        override fun unregisterActivityLifecycleCallbacks(callback: ActivityLifecycleCallbacks?) {
            (real as? Application)?.unregisterActivityLifecycleCallbacks(callback)
        }
    }

    // ------------------------------------------------------------------
    // 版本闸门：换掉 native 问「装了哪版」的那个类。见 [sessionContext]
    // ------------------------------------------------------------------

    /**
     * 递给 `Session` 的 Context。只换 ClassLoader，别的一律委派给 Activity。
     *
     * 副作用只有一个：`ctx instanceof Activity` 从此为假。而这恰好**不改变行为**——
     * 唯一在意它的是 `useProjectedApk`，它在手机上本来就恒假（`xr_projected` 要
     * API 34 的 `ActivityInfo.requiredDisplayCategory`，在低版本上反射直接抛
     * `NoSuchFieldException`，catch 之后也是返回 false）。[EmbeddedApkInfo] 里那个
     * 直接 `false` 的实现就是照这个结论写的。
     */
    private class SessionContext(base: Context) : ContextWrapper(base) {
        private val loader = HelperLoader(base.classLoader)
        override fun getClassLoader(): ClassLoader = loader

        // 递给 `Session()` 的就是这一个，而 native 的
        // `nativeCreateSessionAndWrapperWithFeatures` 拿着它去调
        // `CalibrationContentResolver.readDeviceProfile` —— 见 [fakeRemotePackage]。
        override fun createPackageContext(name: String, flags: Int): Context =
            fakeRemotePackage(this, name, "SessionContext")
                ?: super.createPackageContext(name, flags)

        // 真机日志证明 `CalibrationContentResolver` 走的正是这一条：它先剥
        // applicationContext 再问包。见 [remoteAwareApplication]。
        override fun getApplicationContext(): Context =
            remoteAwareApplication() ?: super.getApplicationContext()
    }

    /**
     * 只拦一个类名，其余全部交给 parent。
     *
     * parent 是 app 自己的 ClassLoader，所以 native 后面要找的
     * `ArCoreApkJniAdapter`、各种回调类都照旧解析得到 —— 这层只是在委派链最前面
     * 插了一次「这个名字换成我们的」。
     */
    private class HelperLoader(parent: ClassLoader) : ClassLoader(parent) {
        override fun loadClass(name: String, resolve: Boolean): Class<*> =
            if (name == HELPER_SLASH || name == HELPER_DOT) {
                EmbeddedApkInfo::class.java
            } else {
                super.loadClass(name, resolve)
            }
    }

    /**
     * `SessionCreateJniHelper` 的替身。四个方法的名字和签名逐字照抄原类
     * （`javap -s` 核过），因为 native 是按 `GetStaticMethodID(名, 签名)` 找的。
     *
     * 类名可以不一样，方法名一个字都不能差。
     *
     * 每个方法都注明了「原实现在手机上会返回什么」——**替身的目标不是绕过检查，
     * 而是在运行时确实存在于本进程内的前提下，回答与事实相符的那个值**。
     */
    internal object EmbeddedApkInfo {

        /**
         * 原实现：`ctx instanceof Activity` 为假就返回 false；为真则去反射
         * API 34 才有的 `requiredDisplayCategory`，低版本抛 `NoSuchFieldException`
         * → 也是 false。手机上恒假，所以这里直接 false。
         *
         * 那条 true 分支是给 XR 投屏设备的，它会改用另一个包名和另一套签名校验，
         * 跟内嵌运行时是两回事。
         */
        @JvmStatic
        fun useProjectedApk(context: Context): Boolean = false

        /**
         * 原实现：读**自己** manifest 的 `com.google.ar.core.min_apk_version`
         * （aar 合并进来的那条 meta-data），读不到抛 RuntimeException。
         *
         * 这里照抄，只把「抛异常」换成落回 [MIN_RUNTIME_VERSION] —— 那个常量和
         * meta-data 是同一个数（260760000），而抛异常会让 native 侧
         * `ExceptionCheck` 失败，把整个 `Session` 构造带走。已知正确答案的时候
         * 没有理由去炸。
         */
        @JvmStatic
        fun getMinApkVersion(context: Context): Int {
            val meta = try {
                context.packageManager
                    .getApplicationInfo(context.packageName, PackageManager.GET_META_DATA)
                    .metaData
            } catch (e: Throwable) {
                Log.w(TAG, "读不到自己的 meta-data，用内置下限", e)
                null
            }
            return if (meta != null && meta.containsKey(META_MIN_VERSION)) {
                meta.getInt(META_MIN_VERSION)
            } else {
                MIN_RUNTIME_VERSION.toInt()
            }
        }

        /**
         * 原实现：拿 `com.google.ar.core` 的签名跟内置的 Google 证书比。
         * **包不存在时它返回 true**（`getPackageInfo` 抛 NameNotFoundException →
         * catch 分支 `iconst_1 ireturn`，字节码核过），所以在我们这条路上，
         * 原实现本来就会返回 true —— 这里不是放宽，是复刻。
         *
         * 它防的是「有人装了个假的 com.google.ar.core 来劫持那 36.7 MB」。而我们
         * 的运行时就在自己的 APK 里，由我们自己的签名保护，那个威胁模型不适用。
         */
        @JvmStatic
        fun checkApkSignature(context: Context): Boolean = true

        /**
         * 原实现：`getPackageInfo("com.google.ar.core").versionCode`，没装就
         * 打一行 `Could not load application package metadata for` 然后返回 -1 ——
         * 而 -1 就是 native 那句 `AR_UNAVAILABLE_ARCORE_NOT_INSTALLED` 的来源。
         *
         * 走到这里说明 [phase] 是 [Phase.EMBEDDED]，运行时**确实在这个进程里**
         * （dex 已解、`libarcore_c.so` 在包里能 dlopen、Dynamite 缓存已填），
         * 只是不以「一个已安装的包」的形式存在。所以报内嵌那份的真实版本号。
         *
         * 系统那份太老时（[doInstall] 里日志说「改用内置的」）也照样报内嵌的号：
         * 缓存指向的确实是内嵌那份，报系统那个旧号反而是错的。
         */
        @JvmStatic
        fun getArCoreApkVersionCode(context: Context): Int = EMBEDDED_VERSION_CODE
    }
}

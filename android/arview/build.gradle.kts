import java.net.HttpURLConnection
import java.net.URI
import java.security.MessageDigest
import java.util.zip.CRC32
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream

plugins {
    id("com.android.library")
    id("org.jetbrains.kotlin.android")
}

// ---------------------------------------------------------------------------
// 内置的 ARCore 运行时（Google Play Services for AR）
// ---------------------------------------------------------------------------
//
// 为什么要内置：宾客手机大多没有 Google Play 商店，而 `ArCoreApk.requestInstall()`
// 内部就是 deep-link 到 `market://details?id=com.google.ar.core` —— 没商店那一步
// 直接失败。中国区官方渠道（小米/华为等应用商店）虽然有这个包，但支持机型列表冻结
// 在 2020 年前后，K40 及以后一台都不在里面。所以运行时跟着我们的包一起走。
//
// **版本号只有这一处。** 客户端库比运行时新，会让 `checkAvailability()` 报
// SUPPORTED_APK_TOO_OLD，我们随后装的又是内置那份旧 APK，装完还是 TOO_OLD ——
// 「装了又装」的死循环。让两者共用同一个变量，那个循环就构造不出来。
val arcoreVersion = "1.54.0"

// 运行时 APK 的 sha256，按版本钉死。换版本必须同时补一行，没补就构建失败 ——
// 而不是「下载一个没人核验过的 72 MiB 二进制、再装进用户手机」。
//
// 补之前先核签名（必须是 Google 原签，不能二次打包 —— 改了签名后续 Play 更新会被拒）：
//   apksigner verify --print-certs <apk>
//   → CN=Android, OU=Android, O=Google Inc.
val arcoreRuntimeSha256 = mapOf(
    "1.54.0" to "109fd1c70843f8753124bc93f9e843c9ef61a7f450fe346166ff42fb96eac67e",
)

// 下载缓存**故意放在 build/ 外面**：`./gradlew clean` 不该导致重下 72 MiB。
// 已在 .gitignore 里排除 —— 这个二进制不进版本库。
val arcoreCacheDir = layout.projectDirectory.dir("../.arcore")

// 宿主 APK 的 applicationId。会被写进运行时 so 顶掉 `com.google.ar.core`，
// 理由见 ArcoreUnpackTask.rehostPackageName。
//
// 这里是写死的常量而不是读 :app 的 applicationId —— library 模块看不见谁在用它，
// 而 AGP 也不保证 library 的任务能拿到 application 变体的属性。代价是两处要一致，
// 所以 `ArCoreEmbeddedRuntime.start` 在运行期核对了一次：不一致会记 warn，而不是
// 等到会话建到一半 abort。
val HOST_PACKAGE = "app.photoar"

/**
 * 下载并校验运行时 APK。整个构建只有这一个任务碰缓存文件 ——
 * 拷进 assets 的活儿交给下面按 variant 展开的任务，避免 debug/release
 * 并行时两边同时往同一个缓存文件里写。
 */
abstract class ArcoreDownloadTask : DefaultTask() {

    @get:Input
    abstract val version: Property<String>

    /** 期望的 sha256。它进 inputs，所以换 sha 一定会重跑校验。 */
    @get:Input
    abstract val sha256: Property<String>

    @get:OutputFile
    abstract val apk: RegularFileProperty

    @TaskAction
    fun run() {
        val expected = sha256.get()
        val ver = version.get()
        val target = apk.get().asFile
        val url = "https://github.com/google-ar/arcore-android-sdk/releases/download/" +
            "$ver/Google_Play_Services_for_AR_$ver.apk"

        if (expected.isEmpty()) {
            error(
                "ARCore $ver 的运行时 APK 没登记 sha256。\n" +
                    "先下载并核对签名：\n  $url\n" +
                    "  apksigner verify --print-certs <apk>   # 必须 O=Google Inc.\n" +
                    "再把 sha256 补进 arview/build.gradle.kts 的 arcoreRuntimeSha256。"
            )
        }

        // 缓存命中的判据是**内容**而不是「文件在不在」：手动放进来的、上个版本
        // 留下的、下载到一半的，都长得像「文件在」。
        if (target.isFile && digest(target) == expected) {
            logger.lifecycle("ARCore 运行时 $ver 已缓存且校验通过")
            return
        }

        target.parentFile.mkdirs()
        // 先落 .part 再改名：构建被 Ctrl-C 打断时不会留下一个「看起来完整」的缓存。
        val part = File(target.parentFile, target.name + ".part")
        part.delete()
        logger.lifecycle("下载 ARCore 运行时 $ver（约 72 MiB）…")
        download(url, part)

        val got = digest(part)
        if (got != expected) {
            part.delete()
            error(
                "ARCore 运行时 sha256 不符，已丢弃。\n" +
                    "  期望 $expected\n  实得 $got\n" +
                    "要么下载被截断，要么上游换了文件 —— 两种都不能装进用户手机。"
            )
        }
        target.delete()
        check(part.renameTo(target)) { "重命名失败：$part → $target" }
        logger.lifecycle("ARCore 运行时校验通过")
    }

    private fun download(url: String, into: File) {
        // 手动跟重定向：GitHub 的 release 下载会 302 到
        // objects.githubusercontent.com，跨主机的自动跟随不总是可靠。
        var location = url
        var opened: HttpURLConnection? = null
        for (hop in 0..5) {
            val c = URI(location).toURL().openConnection() as HttpURLConnection
            c.instanceFollowRedirects = false
            c.connectTimeout = 30_000
            c.readTimeout = 120_000
            val code = c.responseCode
            if (code == 200) {
                opened = c
                break
            }
            if (code in intArrayOf(301, 302, 303, 307, 308)) {
                location = c.getHeaderField("Location")
                    ?: error("HTTP $code 但没给 Location：$location")
                c.disconnect()
                continue
            }
            c.disconnect()
            error("下载 ARCore 运行时失败，HTTP $code：$location")
        }
        val conn = opened ?: error("重定向超过 5 跳，放弃：$url")

        val total = conn.contentLengthLong
        var done = 0L
        var nextTick = 16L shl 20
        try {
            conn.inputStream.use { ins ->
                into.outputStream().buffered().use { outs ->
                    val buf = ByteArray(1 shl 16)
                    while (true) {
                        val n = ins.read(buf)
                        if (n < 0) break
                        outs.write(buf, 0, n)
                        done += n
                        if (done >= nextTick) {
                            val of = if (total > 0) " / ${total shr 20} MiB" else ""
                            logger.lifecycle("  …${done shr 20} MiB$of")
                            nextTick += 16L shl 20
                        }
                    }
                }
            }
        } finally {
            conn.disconnect()
        }
    }

    private fun digest(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        f.inputStream().buffered().use { ins ->
            val buf = ByteArray(1 shl 16)
            while (true) {
                val n = ins.read(buf)
                if (n < 0) break
                md.update(buf, 0, n)
            }
        }
        return md.digest().joinToString("") { "%02x".format(it) }
    }
}

/**
 * 把运行时 APK **拆开**，三块料各归各位 —— 不是整包塞进 assets。
 *
 * 为什么要拆：整包塞进去只能走「PackageInstaller 装第二个应用」那条路，
 * 而需求是「无感放在一个安装流程里」。拆开之后运行时的库和资源就是**我们自己
 * 包里**的东西，[ArCoreEmbeddedRuntime] 直接在本进程把它加载起来，
 * `com.google.ar.core` 根本不需要存在。
 *
 * 三块料的去处（为什么这么分，见 ArCoreEmbeddedRuntime 的类注释）：
 *
 * | 料 | 这里输出到 | 谁在运行期解出来 |
 * |---|---|---|
 * | `lib/<abi>/` 下的 so | jniLibs（[jniDir]） | 系统装包时自动解，运行期零成本 |
 * | `assets/` 下全部 | assets（[assetDir]） | 不用解，native 按名字从 AssetManager 取 |
 * | `classes*.dex` | assets 里的 `arcore_rt/dex.jar` | 首启解一次到 codeCacheDir |
 *
 * 顺带丢掉了运行时 APK 里的 `res/`（3.7 MB）和它自己的 manifest/签名 —— 那些是
 * 「一个独立应用」才需要的东西，我们只要它的计算核心。
 *
 * 体积：拆开重打 ≈ 70.4 MB（assets 41.4 + 两套 lib 26.7 + dex 2.3），比原来整包
 * 不压缩塞进去的 75.3 MB 还小一点，而且设备上不再需要另外 100 MB 装第二个应用。
 *
 * 将来能省的：assets 里 18.4 MB 的 TPU 计算图、10.2 MB 语义分割、4.8 MB 人脸、
 * 2.5 MB 深度、1.1 MB 光照估计，我们只做图像跟踪，一个都用不到；真正必需的只有
 * `packed_profiles/`（2.2 MB 设备标定）、`deepio_*`（3.8 MB VIO 模型）和几个
 * textproto。先全搬求能跑通，裁减是之后的事 —— 少一个文件就少一种「某台设备上
 * 突然初始化失败」的可能，得有真机覆盖再动。
 */
abstract class ArcoreUnpackTask : DefaultTask() {

    @get:InputFile
    abstract val apk: RegularFileProperty

    /** 只为让换版本时 stamp 跟着变，运行期据此判断要不要重解 dex。 */
    @get:Input
    abstract val version: Property<String>

    /**
     * 宿主 APK 的包名。会被写进 `libarcore_c.so`，顶掉里面硬编的
     * `com.google.ar.core` —— 见 [rehostPackageName]。
     */
    @get:Input
    abstract val hostPackage: Property<String>

    /** 由 AGP 的 addGeneratedSourceDirectory 注入，不要手动 set。 */
    @get:OutputDirectory
    abstract val assetDir: DirectoryProperty

    /** 同上。 */
    @get:OutputDirectory
    abstract val jniDir: DirectoryProperty

    @TaskAction
    fun run() {
        val assets = assetDir.get().asFile
        val jni = jniDir.get().asFile
        // Gradle 不会替我们清 @OutputDirectory。不清的话，上一版 ARCore 的
        // 模型文件会留在里面一起打进包 —— 体积白涨，而且新旧模型混着更难查。
        assets.deleteRecursively()
        jni.deleteRecursively()
        val rtDir = File(assets, "arcore_rt").apply { mkdirs() }

        val dexEntries = sortedMapOf<String, ByteArray>()
        var assetCount = 0
        var soCount = 0
        var patchedPackages = 0

        ZipFile(apk.get().asFile).use { zip ->
            for (entry in zip.entries()) {
                if (entry.isDirectory) continue
                val name = entry.name
                when {
                    // 运行时 APK 自己的 `assets/dexopt/baseline.prof` 必须丢掉。
                    //
                    // 那是 ARCore 给**它自己那个应用**的 ART 预编译画像，而 AGP 在
                    // release 变体里会往**同一个路径**生成我们自己的那一份 —— 于是
                    // `packageRelease` 直接失败：
                    //   Zip file ... already contains entry 'assets/dexopt/baseline.prof'
                    // debug 变体不生成，所以这个冲突到出第一个 release 包才炸出来。
                    //
                    // 丢掉它没有代价：这份画像描述的是 ARCore 自己 dex 里的热方法，而
                    // 我们是用 DexClassLoader 在运行期加载那个 dex 的（见
                    // ArCoreEmbeddedRuntime），assets/dexopt 那条路对它本来就不适用。
                    name == "assets/dexopt/baseline.prof" ||
                        name == "assets/dexopt/baseline.profm" -> Unit
                    name.startsWith("assets/") -> {
                        val out = File(assets, name.removePrefix("assets/"))
                        out.parentFile.mkdirs()
                        zip.getInputStream(entry).use { ins ->
                            out.outputStream().buffered().use { ins.copyTo(it) }
                        }
                        assetCount++
                    }
                    // lib/<abi>/xxx.so → jniLibs 要的正是 <abi>/xxx.so 这个形状
                    name.startsWith("lib/") && name.endsWith(".so") -> {
                        val out = File(jni, name.removePrefix("lib/"))
                        out.parentFile.mkdirs()
                        zip.getInputStream(entry).use { ins ->
                            out.outputStream().buffered().use { ins.copyTo(it) }
                        }
                        if (out.name == RUNTIME_SO) {
                            patchedPackages += rehostPackageName(out)
                        }
                        soCount++
                    }
                    name.matches(Regex("classes\\d*\\.dex")) -> {
                        dexEntries[name] = zip.getInputStream(entry).readBytes()
                    }
                }
            }
        }

        check(dexEntries.isNotEmpty()) { "运行时 APK 里没找到 classes*.dex" }
        check(soCount > 0) { "运行时 APK 里没找到 lib/*/*.so" }
        // packed_profiles 是设备标定库，缺了它 ARCore 在任何设备上都起不来。
        // 单独断言而不是只数总数：assets 里绝大多数是我们用不到的 ML 模型，
        // 总数对得上不代表关键那份在。
        check(File(assets, "packed_profiles").isDirectory) {
            "运行时 APK 的 assets 里没有 packed_profiles/（设备标定库）"
        }
        // 补丁没打上就等于会话建不起来（native 会去查一个不存在的包，然后带着
        // pending exception 继续 JNI 调用，ART 直接 abort 整个进程）。这里必须
        // 硬失败：让它安静通过，代价是每次真机跑到一半崩，而日志指向的地方
        // 和原因毫无关系。
        check(patchedPackages > 0) {
            "$RUNTIME_SO 里没找到可替换的 $RUNTIME_PACKAGE 串 —— " +
                "ARCore 换版本后布局变了？见 rehostPackageName"
        }

        // DexClassLoader 的 dexPath 认「一个 zip，根目录下摆 classes*.dex」——
        // 也就是标准 multidex jar。不直接给三个裸 .dex 是因为那要靠 ':' 拼路径，
        // 各版本 ART 对裸 dex 的接受程度不一，jar 这条路是文档保证的。
        //
        // 内部用 STORED：dex 本来就紧凑，压了省不到 5%，却让每次加载多一次 inflate。
        val dexJar = File(rtDir, "dex.jar")
        ZipOutputStream(dexJar.outputStream().buffered()).use { zos ->
            zos.setMethod(ZipOutputStream.STORED)
            for ((name, bytes) in dexEntries) {
                val e = ZipEntry(name).apply {
                    method = ZipEntry.STORED
                    size = bytes.size.toLong()
                    compressedSize = bytes.size.toLong()
                    crc = CRC32().apply { update(bytes) }.value
                    // STORED 要求 time 确定，否则同样输入产生不同 jar，破坏构建缓存
                    time = 0L
                }
                zos.putNextEntry(e)
                zos.write(bytes)
                zos.closeEntry()
            }
        }

        // 运行期靠这个小文件判断「已经解出来的那份还算不算数」。
        // 为什么不用 assets.openFd().length：那要求 dex.jar 在 APK 里未压缩，
        // 一旦哪天 noCompress 漏了就抛 FileNotFoundException，而失败模式是整个
        // 内嵌运行时静默失效 —— 太隐晦。一行文本谁都能读，跟压缩设置无关。
        File(rtDir, "dex.jar.stamp").writeText("${dexJar.length()} ${version.get()}\n")

        logger.lifecycle(
            "ARCore 运行时已拆开：assets $assetCount 项、so $soCount 个、" +
                "dex ${dexEntries.size} 个（${dexJar.length() / 1024} KiB jar）、" +
                "包名补丁 $patchedPackages 处"
        )
    }

    /**
     * 把 so 里硬编的 `com.google.ar.core` 换成宿主包名，返回替换处数。
     *
     * ## 为什么要动二进制
     *
     * 运行时的其它「我要找我自己的包」都走 Context，我们用假 Context 接住了
     * （见 `ArCoreEmbeddedRuntime`）。但有一处不走：native 直接
     * `context.getPackageManager().getPackageInfo("com.google.ar.core", 0)`，
     * 拿到 `NameNotFoundException` 之后**不检查 pending exception** 就接着
     * `GetObjectClass` —— ART 于是 abort 掉整个进程。
     *
     * 那条路没有 Java 侧的落点可打：`PackageManager` 是抽象类，95 个抽象方法，
     * `Proxy` 包不了，手写委派子类要 95 个转发方法；包内部那个 `IPackageManager`
     * 倒是接口，但它是 hidden API，Android 9+ 反射会被拦。
     *
     * 换个方向：让它查的那个包**真的存在**。运行时现在跑在我们进程里、so 和
     * assets 都在我们 APK 里 —— 「运行时所在的包」本来就是我们，改成宿主包名
     * 是把这个事实说对，不是骗它。查到的 `sourceDir` 指向我们的 APK，而那里面
     * 确实有它要的东西。
     *
     * ## 为什么这样改是安全的
     *
     * - 只改**独立**串（前后都是 `\0`）。`com.google.ar.core.apps.car`、
     *   `content://com.google.ar.core.services.arcorecontentprovider/`、
     *   `/data/data/com.google.ar.core/files/...` 这些都带后缀，自动排除在外 ——
     *   一次只动一个变量。
     * - 长度不变：写完补 `\0` 填满原来的 18 字节。ELF 的节表、符号表、重定位
     *   全不用动，文件大小一个字节都不差。
     * - so 没有自校验，我们本来就在从自己的 APK 里加载它（Dynamite 那条路也不验签）。
     *
     * 前提是宿主包名不长于 `com.google.ar.core`（18 字节）。真长过了这里会硬失败，
     * 而不是悄悄截断成一个查不到的包名。
     */
    private fun rehostPackageName(so: File): Int {
        val host = hostPackage.get().toByteArray(Charsets.US_ASCII)
        val needle = RUNTIME_PACKAGE.toByteArray(Charsets.US_ASCII)
        check(host.size <= needle.size) {
            "宿主包名 ${hostPackage.get()} 比 $RUNTIME_PACKAGE 长，" +
                "原地替换会撑坏 so 的布局；要么换个短包名，要么改用别的办法"
        }

        val bytes = so.readBytes()
        var hits = 0
        var i = 0
        while (true) {
            val at = bytes.indexOf(needle, from = i)
            if (at < 0) break
            i = at + needle.size
            // 独立串：前一个字节是 `\0`（串池的边界），后一个也是。带后缀的那些
            // （.apps.car / .services... ）在这里被挡掉。
            val isolated = at > 0 && bytes[at - 1] == 0.toByte() &&
                i < bytes.size && bytes[i] == 0.toByte()
            if (!isolated) continue
            host.copyInto(bytes, at)
            bytes.fill(0, at + host.size, at + needle.size)
            hits++
        }
        if (hits > 0) so.writeBytes(bytes)
        return hits
    }

    private companion object {
        /** 只补这一个；`libarcore_sdk_c.so` 那对存根不查包。 */
        const val RUNTIME_SO = "libarcore_c.so"
        const val RUNTIME_PACKAGE = "com.google.ar.core"

        /**
         * [ByteArray] 没有子数组查找，标准库的 `indexOf` 只认单个元素。
         *
         * 写在 companion 里而不是顶层扩展：build script 的顶层声明会编译成脚本类
         * 的成员，任务类一引用就成了 non-static inner class，Gradle 直接拒绝实例化。
         */
        fun ByteArray.indexOf(needle: ByteArray, from: Int): Int {
            outer@ for (start in from..size - needle.size) {
                for (k in needle.indices) {
                    if (this[start + k] != needle[k]) continue@outer
                }
                return start
            }
            return -1
        }
    }
}

val arcoreDownload = tasks.register<ArcoreDownloadTask>("arcoreRuntimeDownload") {
    group = "arcore"
    description = "下载并按 sha256 校验内置的 Google Play Services for AR 运行时"
    version.set(arcoreVersion)
    sha256.set(arcoreRuntimeSha256[arcoreVersion] ?: "")
    apk.set(arcoreCacheDir.file("Google_Play_Services_for_AR_$arcoreVersion.apk"))
}

// 用 addGeneratedSourceDirectory 而不是 sourceSets["main"].assets.srcDir(task)：
// 后者走 AGP 的 `Project.file()` 语义，不保证把任务依赖连上 —— 一旦没连上，
// 构建成功但包里没有 ARCore，而且不报错。这个 API 是文档保证会连的那个。
//
// 一个任务同时喂 assets 和 jniLibs 两个源集：addGeneratedSourceDirectory 允许对
// 同一个任务调两次，只要指向不同的 DirectoryProperty。拆成两个任务反而要把 APK
// 读两遍（各 75 MB）。
androidComponents {
    onVariants { variant ->
        val suffix = variant.name.replaceFirstChar { it.uppercase() }
        val unpack = tasks.register<ArcoreUnpackTask>("generate${suffix}ArcoreRuntime") {
            group = "arcore"
            description = "把 ARCore 运行时 APK 拆成 assets + jniLibs + dex.jar"
            apk.set(arcoreDownload.flatMap { it.apk })
            version.set(arcoreVersion)
            hostPackage.set(HOST_PACKAGE)
        }
        variant.sources.assets?.addGeneratedSourceDirectory(unpack, ArcoreUnpackTask::assetDir)
        variant.sources.jniLibs?.addGeneratedSourceDirectory(unpack, ArcoreUnpackTask::jniDir)
    }
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
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.20.0")
    // 版本跟着上面的 arcoreVersion 走，别在这里写死 —— 见那里的死循环说明。
    implementation("com.google.ar:core:$arcoreVersion")
    implementation("androidx.media3:media3-exoplayer:1.5.1")
    implementation("androidx.media3:media3-datasource:1.5.1")
    // 只为了 FileProvider：老式安装必须交 content:// URI 出去（见 ArCoreInstaller）
    implementation("androidx.core:core:1.13.1")

    testImplementation("junit:junit:4.13.2")
    // android.jar 里的 org.json 是会抛异常的存根，JVM 单测必须自带一份真的。
    // testImplementation 的顺序在 mockable android.jar 之前，所以能盖住它。
    testImplementation("org.json:json:20240303")
}

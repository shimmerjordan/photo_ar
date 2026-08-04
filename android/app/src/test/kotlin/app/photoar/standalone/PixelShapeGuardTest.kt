package app.photoar.standalone

import java.io.File
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * 守「界面里不许出现圆角」这条，靠扫源码而不是靠看界面。
 *
 * ## 为什么需要这一条
 *
 * 像素风那一轮我在报告里写过「`Shapes` 五个槽全换 0dp，一处改动让整个 App 变直角」。
 * 装到真机上一看：**按钮还是药丸**。原因是 Material 3 的 `Button` 形状来自
 * `ButtonDefaults.shape`（shape token `CornerFull`），而 `CornerFull` 在 Material 内部
 * 硬编码成 `CircleShape`，根本不读 `MaterialTheme.shapes`。同一个坑还有底栏选中项的
 * activeIndicator。
 *
 * 修法是在 [app.photoar.standalone.pixel] 里放**同名**包装、只改 import。而这个修法有
 * 一个很具体的失效模式：以后新写一个界面，IDE 自动补的是
 * `import androidx.compose.material3.Button` —— 药丸就悄悄回来了，**编译通过、测试全绿、
 * 只有装到手机上看才发现**。所以这条测试扫源码，不扫运行时。
 *
 * ## 为什么是读文件而不是别的办法
 *
 * 这件事在 JVM 单测里没有别的抓法：Compose 的形状是运行期从主题解析出来的，
 * 不跑 UI 就拿不到；而跑 UI 要 Robolectric 或者连真机，代价远大于这条约束的价值。
 * 读源码文本很糙，但它守的正是「有没有人写了那一行 import」这件纯文本的事。
 */
class PixelShapeGuardTest {

    private fun sourceRoot(): File {
        // Gradle 跑单测时工作目录是模块目录（android/app）。
        val dir = File("src/main/kotlin/app/photoar/standalone")
        // 换构建器/换 IDE 的 runner 时目录可能不同。找不到就跳过，而不是失败 ——
        // 一条因为环境而红的测试会被人加 @Ignore，那样它就永远不再守任何东西了。
        assumeTrue("找不到源码目录（工作目录是 ${File(".").absolutePath}）", dir.isDirectory)
        return dir
    }

    private fun kotlinFiles(): List<File> =
        sourceRoot().walkTopDown().filter { it.isFile && it.extension == "kt" }.toList()

    @Test
    fun `不许直接 import material3 的按钮与进度条`() {
        // 这几个的形状来自 Material 内部硬编码的 token，主题改不动，只能用包装。
        val banned = listOf("Button", "OutlinedButton", "TextButton", "LinearProgressIndicator")
        val offenders = mutableListOf<String>()
        for (f in kotlinFiles()) {
            if (f.path.contains("/pixel/")) continue // 包装自己要 import 真身
            for (name in banned) {
                if (f.readLines().any { it.trim() == "import androidx.compose.material3.$name" }) {
                    offenders += "${f.name} -> $name"
                }
            }
        }
        assertTrue(
            "这些地方绕过了 pixel 包装，界面上会出现药丸形按钮：$offenders\n" +
                "改成 import app.photoar.standalone.pixel.<同名> 即可，调用点不用动。",
            offenders.isEmpty(),
        )
    }

    @Test
    fun `不许出现圆角形状`() {
        // `RoundedCornerShape(0.dp)` 是允许的（`Shapes` 要求 CornerBasedShape，
        // 不接受 RectangleShape），所以只挡非零的圆角和 CircleShape。
        val offenders = mutableListOf<String>()
        val rounded = Regex("""RoundedCornerShape\(\s*(?!0\s*\.\s*dp)""")
        for (f in kotlinFiles()) {
            f.readLines().forEachIndexed { i, raw ->
                val line = raw.trim()
                // 注释里提到这些名字是可以的 —— 有几处注释正是在解释「为什么不能用它」。
                if (line.startsWith("//") || line.startsWith("*")) return@forEachIndexed
                if (rounded.containsMatchIn(line) || Regex("""\bCircleShape\b""").containsMatchIn(line)) {
                    offenders += "${f.name}:${i + 1}  $line"
                }
            }
        }
        assertTrue(
            "像素风里不该有圆角。这些行需要改成 RectangleShape：\n" +
                offenders.joinToString("\n"),
            offenders.isEmpty(),
        )
    }
}

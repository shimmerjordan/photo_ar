package app.photoar.standalone

import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** 一次请求的三态。界面上「加载中 / 出错 / 有数据」都要能画，缺一个就会出现空白页。 */
sealed interface Load<out T> {
    data object Loading : Load<Nothing>

    data class Fail(val message: String) : Load<Nothing>

    data class Ok<T>(val value: T) : Load<T>
}

/**
 * 一个可重试的取数。
 *
 * [PhotoArClient] 全是阻塞调用（HttpURLConnection），所以统一在这里切到
 * [Dispatchers.IO]；界面里不该再出现一次 withContext，漏一处就是主线程网络。
 */
class Fetch<T>(
    private val scope: CoroutineScope,
    private val block: suspend () -> T,
) {
    var state by mutableStateOf<Load<T>>(Load.Loading)
        private set

    fun reload() {
        scope.launch {
            state = Load.Loading
            state = try {
                Load.Ok(withContext(Dispatchers.IO) { block() })
            } catch (e: Throwable) {
                Load.Fail(Fmt.errText(e))
            }
        }
    }
}

/**
 * 取数并跟着 [key] 变化重取。
 *
 * key 里要带上「库版本号」（[Shell.libraryRev]）之类的东西：入库成功之后照片列表
 * 必须自己刷新，否则用户看到的还是刚才那一屏，会以为入库没成。
 */
@Composable
fun <T> rememberFetch(vararg key: Any?, block: suspend () -> T): Fetch<T> {
    val scope = rememberCoroutineScope()
    @Suppress("SpreadOperator")
    val fetch = remember(*key) { Fetch(scope, block) }
    LaunchedEffect(fetch) { fetch.reload() }
    return fetch
}

/** 一次性动作（入库、关联视频）的状态。 */
sealed interface Action {
    data object Idle : Action

    data class Running(val what: String) : Action

    data class Failed(val message: String) : Action

    data class Done<T>(val value: T) : Action
}

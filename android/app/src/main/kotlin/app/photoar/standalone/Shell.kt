package app.photoar.standalone

import android.content.Context
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import app.photoar.arview.EndpointCenter
import app.photoar.arview.net.PhotoArClient
import app.photoar.arview.net.UrlTransport

/** 界面栈上的一页。 */
sealed interface Route {
    /** 底部导航的三个根。切根会清空栈 —— 根之间没有「返回」关系。 */
    val root: Boolean get() = false

    data object Photos : Route {
        override val root: Boolean get() = true
    }

    data object History : Route {
        override val root: Boolean get() = true
    }

    data object Settings : Route {
        override val root: Boolean get() = true
    }

    data class Detail(val photoId: String) : Route

    /**
     * 试播：全屏放这张照片配的那段视频，不开相机。
     *
     * 不是「AR 的简化版」，两件事的用途不同：AR 要回答「贴得准不准」，试播回答的是
     * 「这张照片配的是不是那段视频」—— 后者在入库之后立刻就想确认，而那时人还在
     * 电脑前，手里没有打印件可扫。顺带也是 §5.8 那条没有 ARCore 时的全屏兜底路径
     * （[VideoPlayer.attach] 的 SurfaceView 重载）唯一能被日常走到的地方。
     */
    data class Play(val photoId: String) : Route

    /**
     * 离线缓存管理（§5.8）。
     *
     * 不给它一个底栏 tab：这一页是「出门前准备一次」的页面，日常不需要，
     * 而底栏的四个格子每多一个都会让最常用的那两个变窄。
     */
    data object Cache : Route

    /**
     * NAS 浏览。[dir] 为 null 是白名单根目录列表。
     *
     * 每进一层目录就 push 一页，所以系统返回键天然等于「上一级」—— 自己维护一个
     * 当前目录变量的话，返回键会一步跳出整个浏览器。
     */
    data class Browse(val pick: Pick, val dir: String?, val photoId: String? = null) : Route

    /** 入库表单。参考图与视频都在 [Shell.draft] 里。 */
    data object Create : Route
}

/** 浏览器这一趟是来挑什么的。 */
enum class Pick {
    /** 挑参考图 → 进入库表单。 */
    IMAGE,

    /** 给入库表单挑视频，挑完退回表单。 */
    VIDEO_FOR_DRAFT,

    /** 给已入库的照片换视频，挑完直接调 attach。 */
    VIDEO_FOR_PHOTO,
}

/** 入库表单的草稿。跨页存活（挑视频要离开表单再回来），所以放在 [Shell] 上。 */
class Draft(val refPath: String) {
    var widthText by mutableStateOf("")
    var title by mutableStateOf("")
    var videoPath by mutableStateOf<String?>(null)

    /** 参考图是横的吗。缩略图解出来之后填，用来算相纸预设该取长边还是短边。 */
    var landscape by mutableStateOf(true)
}

/**
 * 外壳的进程内状态：客户端、界面栈、入库草稿。
 *
 * Activity 只持有它一份（`remember` 在 Activity 的 composition 里），旋转屏幕会重建
 * —— 这个壳里没有需要跨重建保住的东西（草稿丢了重挑一次，比引一个 ViewModel +
 * SavedState 划算），唯一必须全局唯一的 [EndpointCenter] 自己是单例。
 */
class Shell(context: Context) {

    val center: EndpointCenter = EndpointCenter.get(context)

    val client = PhotoArClient(
        transport = UrlTransport(),
        endpoints = { center.endpoints() },
        viaLabel = { center.viaLabel() },
    )

    private val stack = mutableStateListOf<Route>(Route.Photos)

    val current: Route get() = stack.last()

    /** 当前所在的根，用来点亮底部导航。 */
    val currentRoot: Route get() = stack.first()

    /** 照片库的版本号。入库 / 关联成功后 +1，列表和详情跟着它重取。 */
    var libraryRev by mutableStateOf(0)
        private set

    var draft: Draft? by mutableStateOf(null)

    fun push(route: Route) {
        stack.add(route)
    }

    /** @return false 表示已经在根上，该让系统处理返回键（退出 App）。 */
    fun pop(): Boolean {
        if (stack.size <= 1) return false
        stack.removeAt(stack.size - 1)
        return true
    }

    /** 一路弹回当前的根。入库完成后回照片列表用它。 */
    fun popToRoot() {
        while (stack.size > 1) stack.removeAt(stack.size - 1)
    }

    /** 弹到栈里最后一个 [Route.Detail]；没有就回根。给「换视频」用。 */
    fun popToDetail() {
        while (stack.size > 1 && stack.last() !is Route.Detail) {
            stack.removeAt(stack.size - 1)
        }
    }

    fun tab(root: Route) {
        if (stack.size == 1 && stack.first() == root) return
        stack.clear()
        stack.add(root)
    }

    fun libraryChanged() {
        libraryRev++
        // 换过参考图的照片缩略图会变，而缓存是按 URL 存的，URL 没变。
        Thumbs.clear()
    }
}

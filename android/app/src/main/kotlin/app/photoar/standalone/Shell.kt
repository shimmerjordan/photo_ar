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
    /** 底部导航的根。切根会清空栈 —— 根之间没有「返回」关系。 */
    val root: Boolean get() = false

    /**
     * 访客的首页：整页一个「扫一扫」。
     *
     * 管理员没有这一页 —— 他的扫一扫是悬在底栏上那颗 FAB。见 [NavPolicy]。
     */
    data object ScanHome : Route {
        override val root: Boolean get() = true
    }

    data object Photos : Route {
        override val root: Boolean get() = true
    }

    /** 素材：手机 → NAS 的上传，以及浏览 NAS。 */
    data object Media : Route {
        override val root: Boolean get() = true
    }

    /** 管理：内嵌管理台入口 + 识别历史 + 离线缓存。 */
    data object Admin : Route {
        override val root: Boolean get() = true
    }

    data object Settings : Route {
        override val root: Boolean get() = true
    }

    /**
     * 识别历史。
     *
     * 从底栏的根**降级**成了「管理」页下面的一页：`/v1/history` 在服务端是 admin only
     * （它是全库的记录），而改造前它占着一个所有人都看得见的底栏格子 —— 访客点进去
     * 只有 403。
     */
    data object History : Route

    /** 内嵌的 Web 管理台。 */
    data object AdminWeb : Route

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

    // NAS 浏览（`Browse`）与入库表单（`Create`）已经移除。
    //
    // 入库现在只有一条路：「素材」页挑手机里的一张照片 + 一段视频，一次传完就是一组
    // 映射（见 [MediaScreen]）。而 NAS 上**已有**的文件走管理台的批量导入 —— 那边有
    // 完整的路径校验和执行前预演，比在手机上翻目录可靠得多。
    //
    // 两条路都指向同一个 `POST /v1/photo`，区别只在素材从哪来。留着 App 里的文件
    // 浏览器就是同一件事的第二个入口，而它还得自己处理白名单、类型判断、缩略图。
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

    // 初始那一页只是个占位：真正落在哪由 [MainActivity.AppRoot] 在知道角色之后
    // 用 `tab()` 定（访客落扫描页，管理员落照片库，见 [NavPolicy.landingTab]）。
    // 写 Photos 是因为它是管理员的落地页，而未登录时这个值根本不会被渲染 ——
    // 那时显示的是登录蒙版。
    private val stack = mutableStateListOf<Route>(Route.Photos)

    val current: Route get() = stack.last()

    /** 当前所在的根，用来点亮底部导航。 */
    val currentRoot: Route get() = stack.first()

    /** 照片库的版本号。入库 / 关联成功后 +1，列表和详情跟着它重取。 */
    var libraryRev by mutableStateOf(0)
        private set

    fun push(route: Route) {
        stack.add(route)
    }

    /** @return false 表示已经在根上，该让系统处理返回键（退出 App）。 */
    fun pop(): Boolean {
        if (stack.size <= 1) return false
        stack.removeAt(stack.size - 1)
        return true
    }

    /** 一路弹回当前的根。 */
    fun popToRoot() {
        while (stack.size > 1) stack.removeAt(stack.size - 1)
    }

    /** 弹到栈里最后一个 [Route.Detail]；没有就回根。 */
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

package app.photoar.standalone

import app.photoar.arview.AuthPhase

/**
 * 谁能看到哪几个页签，以及未登录时该不该放进来。
 *
 * 单独一个文件、纯函数、有测试，理由是这里管的是**权限边界**：写在 Composable 里的话
 * 唯一的验证方式是装机点一遍，而「访客身上少挡了一个页签」这种错在自己手机上（管理员
 * 账号）永远看不出来 —— 得拿一个访客账号登进去才会现形，而那正是最容易忘的一步。
 *
 * ## 服务端已经挡了，为什么客户端还要挡
 *
 * 服务端才是权限的真相：`/v1/history`、`/v1/fs/list`、`/v1/admin/…` 全是 admin only，
 * 访客拿到的是 403。所以这里挡的**不是安全**，是可用性 —— 改造前访客点「历史」得到的
 * 就是一个 403 报错框，而他什么都没做错。少给他一个点不动的入口，不是把风险挡在外面，
 * 是别把一条必然失败的路摆在他面前。
 */
enum class Tab {
    /** 访客的首页：整页一个「扫一扫」。 */
    SCAN,

    /** 照片库（管理员）。 */
    PHOTOS,

    /** 素材：把手机里的照片/视频传到 NAS，以及浏览 NAS。 */
    MEDIA,

    /** 管理：内嵌管理台、识别历史、离线缓存。 */
    ADMIN,

    SETTINGS,
    ;
}

object NavPolicy {

    /**
     * 访客的两个页签。
     *
     * 只有两个不是「功能少」，是**刻意**的：宾客打开这个 App 只有一件事可做。多一个
     * 入口就多一次「我该点哪个」，而他站在照片前面，手里举着手机。
     *
     * 「历史」不给访客不是因为界面挤 —— `/v1/history` 在服务端是 admin only（它是
     * **全库**的识别记录，`recognize_log` 里没有"谁扫的"这一列，给访客等于把全库
     * 照片的标题发给他）。
     */
    val VIEWER_TABS = listOf(Tab.SCAN, Tab.SETTINGS)

    /**
     * 管理员的四个页签。
     *
     * 拆成四个而不是把管理功能都塞进设置页：改造前设置页有 595 行，账号、通道、
     * 离线缓存、调试开关全在一屏里往下滚，而它们的使用频率差好几个数量级
     * （通道地址配一次就不动，入库是天天做的事）。
     */
    val ADMIN_TABS = listOf(Tab.PHOTOS, Tab.MEDIA, Tab.ADMIN, Tab.SETTINGS)

    fun tabsFor(isAdmin: Boolean): List<Tab> = if (isAdmin) ADMIN_TABS else VIEWER_TABS

    /**
     * 该不该显示登录蒙版。
     *
     * 两个条件，缺一不可：**通道地址**配了，而且**凭证**可用。
     *
     * 只判凭证是不够的（第一版就是这个错）：全新装机两样都没有，那时只弹一个用户名
     * 口令表单，人填对了也登不进去 —— 因为根本没有地址可以发那个请求，而错误信息会是
     * 「连不上」。蒙版必须先问地址。
     *
     * @param configured [EndpointCenter.configured] 那个判断的结果。注意它自己**也**
     *   要求 token 非空，所以两者有重叠；这里仍然分开传，因为「没地址」和「没登录」
     *   要显示完全不同的两屏。
     */
    fun needsGate(hasUsableEndpoint: Boolean, phase: AuthPhase): Boolean =
        !hasUsableEndpoint || !phase.usable

    /**
     * 蒙版上先显示哪一步。
     *
     * 分两步而不是把地址和账号放在同一屏：全新装机时那会是四个输入框加一段解释，
     * 而其中两个（地址）跟「登录」这件事在用户心里毫无关系。
     */
    fun gateStep(hasUsableEndpoint: Boolean): GateStep =
        if (hasUsableEndpoint) GateStep.LOGIN else GateStep.ENDPOINT

    /**
     * 登录之后落在哪一页。
     *
     * 访客落在扫描页（他就是来扫的），管理员落在照片库（登录后第一件事通常是
     * 看一眼库里有什么、或者去入库）。
     */
    fun landingTab(isAdmin: Boolean): Tab = if (isAdmin) Tab.PHOTOS else Tab.SCAN

    /**
     * 角色变了之后，原来停留的那个页签还能不能待。
     *
     * 用得到的场景是**同一台手机换人登录**：管理员登出、家里人用访客身份登进来，
     * 而界面还停在「素材」页。不换的话那一页上每个按钮都会 403。
     */
    fun tabAfterRoleChange(current: Tab, isAdmin: Boolean): Tab {
        val allowed = tabsFor(isAdmin)
        if (current in allowed) return current
        return landingTab(isAdmin)
    }
}

/** 蒙版的两步。 */
enum class GateStep {
    /** 先填服务端地址。 */
    ENDPOINT,

    /** 地址有了，填名字和口令。 */
    LOGIN,
}

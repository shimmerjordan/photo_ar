package app.photoar.arview.ar

/**
 * ARCore 运行时的状态。是 `ArCoreApk.Availability` 的镜像，故意自己定义一份：
 * 判断逻辑要能在 JVM 单测里跑，而那个枚举来自 Android 库。
 *
 * 合并了几个对决策没区别的取值 —— `UNKNOWN_ERROR` 和 `UNKNOWN_TIMED_OUT` 都是
 * [UNKNOWN]：两者的实际含义都是「问不到 Google 的机型档案服务」，而这恰好就是
 * 我们最主要的场景（宾客手机没 Google 框架）。
 */
enum class ArRuntimeState {
    /** 装了，而且版本够新。 */
    INSTALLED,

    /** 没装。 */
    NOT_INSTALLED,

    /** 装了，但比客户端库要求的旧。 */
    TOO_OLD,

    /** 这台机器的硬件/标定不在 ARCore 支持列表里 —— 装了也没用。 */
    DEVICE_NOT_CAPABLE,

    /** 还在查。ARCore 首次查询是异步的，得再问一次。 */
    CHECKING,

    /** 查不出来（出错或超时）。 */
    UNKNOWN,
}

/** 下一步该干什么。 */
enum class ArAction {
    /** 直接开 AR 会话。 */
    START_AR,

    /** 等一会儿再查一次。 */
    RECHECK,

    /** 装我们自己包里带的那份运行时：`PackageInstaller` 会话，流式写入不落盘。 */
    INSTALL_BUNDLED,

    /**
     * 同样是装内置那份，但走老式安装 Intent（`ACTION_VIEW` + APK 的 MIME）。
     *
     * 这不是「另一种风格」，是会话安装被 ROM 拦掉之后唯一的退路 —— MIUI 的安装器
     * 在 `InstallStart.onCreate` 里第一件事就是：只要 `sessionId != -1` 且
     * `SDK_INT <= 34`，一律拒绝（日志 `MIUIPI_InstallStart: blocked session install
     * because sdk version too low`），跟我们的 targetSdk、授权、用户点不点全无关。
     * 老式 Intent 的 sessionId 是 -1，正好跳过那段判断。
     */
    INSTALL_BUNDLED_LEGACY,

    /** 先把「安装未知来源应用」的权限要到手。 */
    GRANT_INSTALL_PERMISSION,

    /** 放弃 AR，走全屏兜底。 */
    FALLBACK,
}

/**
 * 决策的全部输入。没有 Context，没有 Activity —— 这是这个类存在的意义。
 *
 * @param state 当前查到的运行时状态
 * @param bundled 包里到底有没有那份 APK。构建脚本保证有，但 assets 被裁掉、
 *   或者将来有人把 `:arview` 塞进一个没配 `noCompress` 的外壳，都可能让它没有。
 *   读不到就当没有，绝不能因此崩。
 * @param sessionAttempted 这次会话里试过 [ArAction.INSTALL_BUNDLED] 了没（不管成没成）
 * @param legacyAttempted 这次会话里试过 [ArAction.INSTALL_BUNDLED_LEGACY] 了没
 * @param canInstallPackages 有没有「安装未知来源应用」的授权
 * @param permissionAsked 是不是已经把用户送去过那个设置页
 * @param checks 当前这一轮已经连续复查了几次
 */
data class ArInstallContext(
    val state: ArRuntimeState,
    val bundled: Boolean,
    val sessionAttempted: Boolean,
    val legacyAttempted: Boolean,
    val canInstallPackages: Boolean,
    val permissionAsked: Boolean,
    val checks: Int,
)

object ArInstallPolicy {

    /** 两次查询之间隔多久。 */
    const val POLL_MS = 800L

    /**
     * `CHECKING` 最多容忍几轮 = 8 × 800ms ≈ 6.4s。
     *
     * 这个数不是随便定的：§0.3 给用户的承诺是「识别到播放 10s」，而 AR 可用性检查
     * 发生在那 10s 之前。等超过 6s 还没结论，说明它在等一个永远不会回来的网络请求 ——
     * 这时候装本地那份运行时比继续等有意义。
     */
    const val MAX_CHECKS = 8

    /**
     * @return 下一步动作。纯函数：同样的输入永远同样的输出。
     */
    fun decide(ctx: ArInstallContext): ArAction = when (ctx.state) {
        ArRuntimeState.INSTALLED -> ArAction.START_AR

        // 硬件不支持是唯一「装了也没用」的情况，直接兜底。注意别把它和
        // UNKNOWN 混在一起：UNKNOWN 是「不知道」，不是「不行」。
        ArRuntimeState.DEVICE_NOT_CAPABLE -> ArAction.FALLBACK

        ArRuntimeState.CHECKING ->
            if (ctx.checks < MAX_CHECKS) ArAction.RECHECK else install(ctx)

        // TOO_OLD 装我们这份能修好，前提是「内置版本 == 客户端库要求的版本」——
        // 那个前提由 build.gradle.kts 里的单一版本号保证。如果两者脱钩，这里就会
        // 变成「装完还是 TOO_OLD、于是再装」的死循环，所以那个单一来源不是洁癖。
        ArRuntimeState.NOT_INSTALLED,
        ArRuntimeState.TOO_OLD,
        // 查不出来时选择「装」而不是「兜底」：最可能的原因就是连不上 Google 的
        // 机型档案服务，而本地装上运行时之后这个查询就能在本机得到答案。
        ArRuntimeState.UNKNOWN,
        -> install(ctx)
    }

    private fun install(ctx: ArInstallContext): ArAction = when {
        !ctx.bundled -> ArAction.FALLBACK

        // 老式安装是「交出去就没有回执」的：系统安装器有可能在真正装完之前就把
        // 我们切回前台，这时候状态还是 NOT_INSTALLED。直接兜底就会把一次**成功的**
        // 安装误判成失败，所以给它和 CHECKING 一样有上限的宽限期。
        ctx.legacyAttempted && ctx.checks < MAX_CHECKS -> ArAction.RECHECK

        // 宽限期用完还不 READY，说明这台机器装不上（机型不在档案里、用户点了取消、
        // ROM 两条都拦）。再试一次只是重复同一次失败。
        //
        // 判据只看 legacyAttempted、不看 sessionAttempted：老式是**最后**一条路，
        // 走过它就没有下一条了。写成 `session && legacy` 的话，一旦哪天有人让老式
        // 先跑，就会退回去试会话 —— 而那条路在 MIUI 上已经被证明是死的。
        ctx.legacyAttempted -> ArAction.FALLBACK

        // 授权是两条路共用的门 —— 老式安装从 API 26 起同样要 REQUEST_INSTALL_PACKAGES。
        ctx.canInstallPackages ->
            if (!ctx.sessionAttempted) ArAction.INSTALL_BUNDLED
            // 会话先试、老式垫后：会话不落盘也有回执，是能用的时候更好的那条。
            // 但它在 MIUI（SDK_INT ≤ 34）上是必然失败，所以失败必须有下文。
            else ArAction.INSTALL_BUNDLED_LEGACY

        // 同样「只问一次」。少了这个闸门就会变成：送去设置页 → 用户按返回 →
        // onResume → 又送去设置页 —— 一个退不出来的界面，比没有 AR 糟得多。
        ctx.permissionAsked -> ArAction.FALLBACK
        else -> ArAction.GRANT_INSTALL_PERMISSION
    }

    /**
     * 动作对应的界面提示。null = 不用说话。
     *
     * 措辞规则和 [app.photoar.arview.ui.Notices] 一致：说清「现在在干什么」和
     * 「用不用管」，不要把 ARCore、Play 服务这些名字甩给宾客 —— 他们只想看视频。
     *
     * 兜底那句要看 [state]：只有硬件真的不支持时才能说「这台设备不支持」，
     * 用户拒了安装也说这句就是在撒谎，而且堵死了他重试的念头。
     */
    fun notice(action: ArAction, state: ArRuntimeState): String? = when (action) {
        ArAction.START_AR -> null
        ArAction.RECHECK -> "正在准备 AR 组件…"
        // 两条路对宾客是同一件事，别把「会话 / 老式」这种实现细节说出去
        ArAction.INSTALL_BUNDLED,
        ArAction.INSTALL_BUNDLED_LEGACY,
        -> "首次使用要装一个 AR 组件，装完就不再问了"
        ArAction.GRANT_INSTALL_PERMISSION -> "需要允许安装组件，请在弹出的设置里打开"
        // 兜底不是报错：照片照样认得出、视频照样能看，少的只是贴合。
        ArAction.FALLBACK ->
            if (state == ArRuntimeState.DEVICE_NOT_CAPABLE) "这台设备不支持 AR，识别后将全屏播放"
            else "没装上 AR 组件，识别后将全屏播放"
    }
}

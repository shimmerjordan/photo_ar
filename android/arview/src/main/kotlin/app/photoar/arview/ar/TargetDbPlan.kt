package app.photoar.arview.ar

/**
 * 扫描启动时装哪一份多图库 —— **纯判断**，不碰 ARCore、不碰文件内容。
 *
 * 单独拎出来是因为这个判断错了的后果全是「不报错的坏」：多重建一次库就是启动扫描时
 * 白等 6 秒（`addImage` 每张约 30ms × 200 张）；少退回一次就是离线识别静默消失。而
 * [LocalTargetDb] 自己碰 `Bitmap` 与 `Session`，在 JVM 单测里跑不起来（`:arview`
 * 刻意没开 `unitTests.isReturnDefaultValues`）。所以决策在这里，执行在那边。
 */

/** 装的是哪一份。界面上要区分 —— 两者的跟踪质量不是一档。 */
enum class TargetDbSource {
    /** 服务端 `arcoreimg` 拿原图预建的整库（`GET /v1/targets/db`）。 */
    SERVER,

    /** 端上拿 640px 缩略图现建的那份（`local.imgdb`）。 */
    LOCAL,
}

sealed interface TargetDbPlan {
    /** 直接装服务端那份。**不重建端上那份** —— 那 6 秒正是这次改动要省掉的。 */
    data object UseServer : TargetDbPlan

    /**
     * 用端上现建那份。
     *
     * @param rebuildFirst 要先跑一遍 `addImage` 建库。**这一步几秒，必须在后台线程**，
     *   所以「要不要重建」必须在装库之前就问清楚 —— 装库在 GL 线程上，到那时候才发现
     *   需要重建就只有两个选择：在 GL 线程上卡几秒，或者放弃离线识别。
     */
    data class UseLocal(val rebuildFirst: Boolean) : TargetDbPlan
}

/**
 * 决策的输入。全是「事实」而不是文件句柄，所以这个判断能在 JVM 里跑。
 *
 * @param serverInstallable 服务端那份现在能装吗（字节在 + 元数据在 + 这个版本没被
 *   这台机器拒过）。判据在 [app.photoar.arview.cache.ServerTargetsStore.installable]。
 * @param localStale 端上那份过期了吗（不存在也算，见 [LocalTargetDb.stale]）。
 */
data class TargetDbFacts(
    val serverInstallable: Boolean,
    val localStale: Boolean,
)

/**
 * 装库优先级。
 *
 * 1. 有能装的服务端预建库 → 装它，端上那份**碰都不碰**。
 * 2. 否则用端上现建那份，过期就先重建。
 *
 * 「服务端那份装载失败」不是这里的一个分支，而是**再问一次**：调用方把
 * `serverInstallable` 置成 false（同时把这个版本记成 rejected）再调，于是自然落到
 * 第 2 条。这么安排是因为「装不上」只有真的 `deserialize` 一次才知道，而那件事有
 * 副作用（要落盘记住）—— 塞进一个纯函数里就得让它返回一串「接下来该做什么」。
 *
 * 没有「两者都没有」这个分支：那种情况走第 2 条的 `rebuildFirst=true`，重建时发现一张
 * 可用的缩略图都没有，于是不产出文件；装库那一步看到没文件就什么都不做（这是既有
 * 行为，见 [LocalTargetDb.install]）。扫描照样能用 —— 服务端 `/v1/recognize` 那条路
 * 认全库任何一张。
 */
fun planTargetDb(facts: TargetDbFacts): TargetDbPlan = when {
    facts.serverInstallable -> TargetDbPlan.UseServer
    else -> TargetDbPlan.UseLocal(rebuildFirst = facts.localStale)
}

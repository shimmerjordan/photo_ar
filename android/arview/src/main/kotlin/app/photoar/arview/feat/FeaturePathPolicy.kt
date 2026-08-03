package app.photoar.arview.feat

/**
 * 识别走哪条路，以及端上提特征失败之后怎么回退。
 *
 * 纯 Kotlin。端上推理在这里**没法真机验证**，所以能被验证的那一部分（什么时候用哪条路、
 * 失败几次之后放弃、提示报几遍）必须全在这个类里，真实的 ONNX 调用那一层薄到没有判断。
 */

/** 一帧要怎么发出去。 */
enum class RecognizePath {
    /** 现状：POST /v1/recognize，传一张 JPEG。 */
    JPEG,

    /** 端上提特征：POST /v1/recognize/features，传描述子。 */
    FEATURES,
}

/**
 * 端上提特征为什么没用上。四种的**可恢复性不同**，所以不能归成一个 boolean。
 */
enum class FeatureFailure {
    /**
     * 模型取不到（服务端 404 `model_missing`，或者下载失败）。
     *
     * 这一轮里不会自己变好（服务端得先有那个文件），所以直接放弃整个进程。
     */
    MODEL_UNAVAILABLE,

    /** ONNX 会话建不起来（ABI 不支持、内存不够、文件损坏）。同样不会自己变好。 */
    LOAD_FAILED,

    /**
     * 推理抛异常。
     *
     * 这一种**给几次机会**：单帧的解码失败、瞬时的内存压力是真实存在的，而一次异常就
     * 永久关掉这条路会让用户看到「今天怎么突然变慢了」且下次重启才恢复。
     */
    INFER_FAILED,

    /**
     * 服务端拒收描述子（400 `unsupported_backend` 等）。
     *
     * 最常见的原因是服务端跑的是 ORB 后端（或者 XFeat 模型不在、已经回退了 ORB）。
     * 重试无意义 —— 每 400ms 一次只会刷日志，而且每次都白付一次端上推理。
     */
    SERVER_REJECTED,
    ;

    /** 一次就够不够判死刑。 */
    val fatal: Boolean get() = this != INFER_FAILED
}

/**
 * 开关 + 回退。
 *
 * 两个状态刻意分开：
 * - [preferFeatures] 是**用户的持久化偏好**（存在 [app.photoar.arview.EndpointConfig] 里）。
 * - [disabledThisSession] 是**这个进程内的回退**，不落盘。
 *
 * 分开的理由：回退时顺手把偏好写成 false，等于「服务端那边修好之后，用户得自己想起来
 * 再去设置里打开一次」—— 而他根本不知道 App 悄悄替他关掉过。不落盘的话，下次启动会
 * 再试一次，成了就自己好了，不成就再回退一次（代价是一次失败的推理）。
 *
 * **线程**：[path] 在状态机的主线程上读，而 [onFailure] / [onSuccess] 在提特征那条
 * 工作线程上调（模型下载失败就是在那边发现的）。所以读的那两个字段是 `@Volatile`，
 * 改状态的三个方法是 `@Synchronized` —— [onFailure] 里的计数是读改写，不同步的话
 * 「第三次才放弃」会变成「有时候第二次、有时候第四次」。
 */
class FeaturePathPolicy(preferFeatures: Boolean = false) {

    companion object {
        /**
         * 连续多少次推理异常之后放弃。
         *
         * 3 次 = 约 1.2 秒（抽帧间隔 400ms）。取 3 而不是 1：单帧异常可能是瞬时的。
         * 取 3 而不是 10：每次异常都白付一次推理 + 一帧的延迟，而这条路的全部意义
         * 就是更快。
         */
        const val INFER_STRIKES = 3
    }

    @Volatile
    var preferFeatures: Boolean = preferFeatures
        private set

    /** 这个进程内已经回退到 JPEG。不落盘，见类注释。 */
    @Volatile
    var disabledThisSession: Boolean = false
        private set

    private var inferStrikes = 0
    private var noticed = false

    /** 最近一次导致回退的原因。界面/日志用。 */
    @Volatile
    var lastFailure: FeatureFailure? = null
        private set

    /** 这一帧该走哪条路。 */
    val path: RecognizePath
        get() = if (preferFeatures && !disabledThisSession) {
            RecognizePath.FEATURES
        } else {
            RecognizePath.JPEG
        }

    /** 已经从「用户想走特征」退成了「实际走 JPEG」。 */
    val fellBack: Boolean get() = preferFeatures && disabledThisSession

    /**
     * 用户在设置里改了开关。
     *
     * 打开时把这一轮的回退状态清掉：用户显式打开的语义是「再试一次」，而不是「保持刚才
     * 那个回退不变」—— 后者会让「服务端修好了、我去打开开关」这个动作看起来毫无反应。
     */
    @Synchronized
    fun setPreference(on: Boolean) {
        preferFeatures = on
        if (on) {
            disabledThisSession = false
            inferStrikes = 0
            noticed = false
            lastFailure = null
        }
    }

    /** 一次成功的端上提特征 + 识别。把推理的连续失败计数清零。 */
    @Synchronized
    fun onSuccess() {
        inferStrikes = 0
    }

    /**
     * 一次失败。
     *
     * @return true 表示**这一次要报一条提示**。每个进程只报一次 —— 扫描时每 400ms 一帧，
     *   逐次报就是一屏刷不完的重复提示，而用户需要知道的只有「这次走的是慢的那条路」。
     */
    @Synchronized
    fun onFailure(kind: FeatureFailure): Boolean {
        lastFailure = kind
        if (!kind.fatal) {
            inferStrikes++
            if (inferStrikes < INFER_STRIKES) return false
        }
        val first = !disabledThisSession
        disabledThisSession = true
        if (!first || noticed) return false
        noticed = true
        return true
    }

    /** 提示文案。回退是静默降级（功能不丢），所以措辞不能像报错。 */
    fun message(): String {
        val why = when (lastFailure) {
            FeatureFailure.MODEL_UNAVAILABLE -> "取不到端上模型"
            FeatureFailure.LOAD_FAILED -> "端上模型加载失败"
            FeatureFailure.INFER_FAILED -> "端上推理连续出错"
            FeatureFailure.SERVER_REJECTED -> "服务端不接受端上特征（后端可能不是 xfeat）"
            null -> "未知原因"
        }
        return "$why，已改回上传整帧识别。功能不受影响，只是慢一点。"
    }
}

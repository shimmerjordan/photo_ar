package app.photoar.arview

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.os.Handler
import android.os.Looper
import android.util.Log
import app.photoar.arview.net.HttpProber
import app.photoar.arview.net.UrlTransport
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * 进程内唯一的 endpoint 入口：持久化 + 探活 + 当前两条通道。
 *
 * 为什么是单例：§9.2 的四个触发时机（启动、网络变化、手动刷新、连续失败 2 次）
 * 分散在设置界面、扫描界面和状态机三处，而它们必须看到**同一份**探活结果。每处
 * 各自 new 一个 resolver 的话，节流（[EndpointResolver.MIN_INTERVAL_MS]）就形同
 * 虚设，弱网下会变成三倍探活。
 *
 * 线程：`worker` 单线程串行跑 refresh（它是阻塞的，最多 1.5s），`probePool` 供
 * resolver 并行 ping 四条通道。回调统一 post 回主线程。
 */
class EndpointCenter private constructor(context: Context) {

    companion object {
        private const val TAG = "EndpointCenter"

        @Volatile
        private var instance: EndpointCenter? = null

        fun get(context: Context): EndpointCenter =
            instance ?: synchronized(this) {
                instance ?: EndpointCenter(context.applicationContext).also { instance = it }
            }
    }

    private val appContext = context.applicationContext
    private val store = EndpointStore(appContext)
    private val main = Handler(Looper.getMainLooper())

    /**
     * 探活用的令牌快照。不写成 `{ resolver.config.token }` 是为了避开初始化顺序
     * 陷阱：prober 要在 resolver 之前构造出来。
     */
    @Volatile
    private var tokenSnapshot: String = ""

    private val transport = UrlTransport()

    private val probePool = Executors.newFixedThreadPool(4) { r ->
        Thread(r, "photoar-probe").apply { isDaemon = true }
    }
    private val worker = Executors.newSingleThreadExecutor { r ->
        Thread(r, "photoar-endpoint").apply { isDaemon = true }
    }

    private val resolver = EndpointResolver(
        prober = HttpProber(transport, { tokenSnapshot }),
        executor = probePool,
        clock = Clock { System.currentTimeMillis() },
        initial = store.load().also { tokenSnapshot = it.token },
    )

    /** 已经排了一次「无回调」的刷新。用来把连续触发合并成一次。 */
    private val queued = AtomicBoolean(false)

    private val listeners = CopyOnWriteArrayList<(Resolution) -> Unit>()

    private var netCallbackRegistered = false

    val config: EndpointConfig get() = resolver.config
    val resolution: Resolution? get() = resolver.resolution

    fun endpoints(): Endpoints = resolver.endpoints()

    fun viaLabel(): String? = resolver.viaLabel()

    /** §9.4：mediaEndpoint 走隧道时上传入口要藏掉。 */
    fun uploadAllowed(): Boolean = resolver.uploadAllowed()

    /** 配置有没有填到能用的程度。没填地址时界面要引导去设置，而不是让它静默失败。 */
    val configured: Boolean get() = config.candidates.any { it.usable } && config.token.isNotBlank()

    /** 当前凭证的阶段。见 [AuthPolicy.phaseOf]。 */
    fun authPhase(nowMs: Long = System.currentTimeMillis()): AuthPhase =
        AuthPolicy.phaseOf(config, nowMs)

    /**
     * 手上这个 token 现在还能不能用。
     *
     * 过期的判定用**本机时钟**，所以它可能是错的（手机时间跑偏）。这不构成安全问题
     * （真正的判定在服务端），它只是让「已经知道要过期了」的情况不必先白发一次请求
     * 换回 401。时钟偏到把有效凭证判成过期时，用户重新登录一次即可，代价可接受。
     */
    val loggedIn: Boolean get() = authPhase().usable

    /** 保存并立刻重新探活（配置变了，上次的结果一定过期）。 */
    fun save(newConfig: EndpointConfig, onDone: ((Resolution) -> Unit)? = null) {
        store.save(newConfig)
        tokenSnapshot = newConfig.token
        resolver.update(newConfig)
        refreshAsync(force = true, onDone = onDone)
    }

    /**
     * 只换凭证，**不重新探活**。
     *
     * 与 [save] 分开：探活要 1.5 秒，而登录/登出没有改任何地址 —— 上一次的探活结果
     * 仍然有效。走 [save] 的话，每次登录都要多等一次探活，而那一秒半正好落在用户刚
     * 输完口令、最期待反馈的时刻。
     *
     * `resolver.update` 会清掉探活结果（它假设配置变了），所以这里不能用它。
     */
    fun saveCredentials(newConfig: EndpointConfig) {
        store.save(newConfig)
        tokenSnapshot = newConfig.token
        resolver.replaceConfigKeepingProbes(newConfig)
    }

    /**
     * 异步探活。
     *
     * [onDone] 为 null 时会做合并：已经排了一次就直接返回。带回调的调用不合并 ——
     * 那是用户点了「刷新」在等结果，必须给他一次真实的反馈（resolver 内部的节流
     * 对 `force=true` 不生效）。
     */
    fun refreshAsync(force: Boolean = false, onDone: ((Resolution) -> Unit)? = null) {
        if (onDone == null && !queued.compareAndSet(false, true)) return
        worker.execute {
            try {
                val r = resolver.refresh(force)
                main.post {
                    onDone?.invoke(r)
                    listeners.forEach { it(r) }
                }
            } catch (e: Throwable) {
                Log.w(TAG, "探活异常：${e.message}", e)
            } finally {
                if (onDone == null) queued.set(false)
            }
        }
    }

    /** 状态机连续失败 2 次时走这里（§9.2）。带节流，可以随便调。 */
    fun requestRefresh() = refreshAsync(force = false, onDone = null)

    /** 探活结果变化的订阅，主线程回调。设置界面用它刷新那四行状态。 */
    fun addListener(l: (Resolution) -> Unit) {
        listeners.add(l)
    }

    fun removeListener(l: (Resolution) -> Unit) {
        listeners.remove(l)
    }

    /**
     * 监听网络变化（§9.2 的第二个触发时机）。
     *
     * 用 default network 回调而不是广播：从 Wi-Fi 切到蜂窝时 `onAvailable` 会再来
     * 一次，这正是「回到家进了局域网」和「出门离开局域网」两个场景要的信号。注册
     * 一次就跟着进程活着 —— 反注册的唯一时机是进程退出，那时系统自己会收。
     */
    fun watchNetwork() {
        if (netCallbackRegistered) return
        val cm = appContext.getSystemService(ConnectivityManager::class.java) ?: return
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = requestRefresh()

            override fun onLost(network: Network) = requestRefresh()
        }
        try {
            cm.registerDefaultNetworkCallback(cb)
            netCallbackRegistered = true
        } catch (e: Exception) {
            // 某些定制系统上会抛 SecurityException / TooManyRequests。探活还有
            // 其它三个触发时机，缺了这一个不该让 App 起不来。
            Log.w(TAG, "网络变化监听注册失败：${e.message}")
        }
    }
}

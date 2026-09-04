/**
 * 扫描页（首页）。相机 + 识别 + 贴合渲染。
 *
 * ## 一帧的流水
 *
 * ```
 * rAF ─┬─ 画相机背景（GL）
 *      ├─ 有活的四角？→ 平滑 → unitSquareH → clipVertices → 画视频面片
 *      └─ 该送帧了？→ getImageData → transfer 给 Worker（不等结果）
 * ```
 *
 * **渲染与识别彻底解耦**：渲染每帧都跑（相机帧率），识别爱多慢多慢。这就是为什么页面
 * 在一次几百毫秒的检测期间仍然流畅 —— 而那正是 Android 那条第二贴合路做不到的事
 * （它的四角来自网络，往返 1~2.5 秒且期间画面里什么都没有）。
 *
 * ## 卸载时必须放掉什么、以及**不该**放掉什么
 *
 * 放掉：相机流（不放的话相机灯一直亮、电量哗哗掉，而用户以为已经离开了）、rAF 循环、
 * WebGL 上下文引用。
 *
 * **不放掉 Worker**：它持有 12MB 的 wasm 实例和解析好的识别库。每次进扫描页重建一次，
 * 就是每次重新加载 + 实例化 —— 而用户在页签之间来回切是常态。所以 Worker 由外壳长驻，
 * 这里只给它发一次 `reset`（清掉跟踪状态，免得回来时还锁着上一张照片）。
 */
import { CameraError, FrameGrabber, canGrabBitmap, openCamera, stopCamera } from '../camera.js'
import { MEDIA_ERR, NETWORK_STATE, READY_STATE, diag, diagAlways, flushDiag, isDiagEnabled, short } from '../diag.js'
import { Renderer } from '../render/gl.js'
import {
  FULL_RECT, TTL_MS, clipVertices, flatQuadImage, imageToNdc, plausible, unitSquareH,
  videoCrop,
} from '../render/screenquad.js'
import { Stage, loadPhotoVideo, stageName } from '../mediaload.js'
import { button, esc, h, playerControls } from '../ui.js'
import { traceRender, traceResult } from '../trace.js'
import { QuadFilter } from '../render/quadfilter.js'
import { thresholds } from '../recognize/consts.js'
import { sendInterval } from '../pacing.js'
import { CLOSER_MIN_INLIERS, GUIDE_DEBOUNCE_MS, guideTip } from '../guide.js'

/** 送帧间隔由 pacing.sendInterval 决定，见那个文件。 */
/**
 * 允许多少帧同时在途。**这一条是"贴合帧率翻倍"的全部来源。**
 *
 * ## 改造前：抓帧与识别完全串行
 *
 * 老代码用一个布尔量 `inflight`：送出去之后一直等到结果回来才肯抓下一帧。于是时间线是
 *
 *     抓帧 6.5ms → [worker 转换 10ms + 光流 27ms，主线程干等] → 结果 → 再抓
 *
 * 两段一点都不重叠。真机实测（M2012K11C，12 秒）：**观测间隔 p50 65.6ms = 15.2Hz**，
 * 而其中真正在算的只有 ~43.5ms —— 剩下三分之一是纯等待（还要再等一个 rAF tick）。
 * 同一时刻这条链上只有一个核在动，而这台手机有八个。
 *
 * ## 现在：一帧在算，一帧在排队
 *
 * 取 **2**：一帧正在 worker 里算，一帧已经抓好排在它后面。worker 一算完立刻有下一帧
 * 可吃，周期从「抓 + 算」变成「max(抓, 算)」。
 *
 * **不要调大。** 3 及以上没有任何收益（worker 只留最新一帧，多出来的当场被丢，
 * 见 worker.js 的 `onFrame`），却要白付一次 `createImageBitmap` 的主线程时间，
 * 而那正是渲染帧预算里最贵的一笔。
 */
const MAX_INFLIGHT = 2

/**
 * 平铺续播：跟丢之后视频不撤，退到屏幕中央按照片的比例继续放。
 *
 * 跟丢在真实现场太常见（手一抖、走过去、照片被人挡了一下），而上一版的表现是"视频
 * 凭空消失、下一秒又凭空回来"—— 用户不知道它是坏了还是自己动错了。占屏宽 **70%** 是
 * 有意小于满屏：它必须一眼看出来"这是退出贴合的样子"，而不是像全屏播放。
 *
 * 贴回去走 **4 步 200ms** 的离散插值。像素风里过渡只能是 `steps()`（见 theme.css 那条
 * 硬约束），而这块几何在 GL 里，CSS 的 steps 管不到它 —— 所以那条约束在这儿是手写的。
 */
const FLAT_WIDTH = 0.7
const BLEND_MS = 200
const BLEND_STEPS = 4

/**
 * **跟丢**之后那几句。每一句都必须能照着做，而且要区分原因（见 Android 那边的教训）。
 *
 * 扫描阶段（还没锁定）的那几句**不在这里** —— 它们要按内点数、连续帧数、多久没证据
 * 分档，而一张 `reason → 文案` 的表表达不了那些条件。见 `guide.js`。
 */
const TIPS = {
  flat: '跟丢了，视频继续放；对准照片会贴回去。',
  // 点「全屏」是**用户自己要的**满屏播放，不是跟丢 —— 说"跟丢了"会让他以为点坏了。
  // 而且**不能**说"对准照片会贴回去"：满屏只能显式退出（见 onFullscreen 那条裁决），
  // 举着照片对准它什么都不会发生 —— 那句话会让用户以为满屏坏了。
  flat_full: '满屏播放中。再点一次「全屏」贴回照片，或点「重新扫描」。',
  flow_lost: '跟丢了，正在重新识别…',
  homography_lost: '跟丢了，正在重新识别…',
  quad_implausible: '角度太斜了，正一点。',
  // 跟踪本身没断，但连着几次外观重锚都对不上 —— 多半是大角度下贴偏了（见
  // pipeline.MAX_REANCHOR_FAILS）。所以说的不是"跟丢了"，而是让用户正过来重认。
  appearance_lost: '贴合可能偏了，正对照片重新识别…',
  no_seed: '重新识别…',
  forbidden: '认出来了，但这张没有授权给你。',
}

export default {
  title: '扫一扫',
  /** 全屏无顶栏：相机画面是这一页的全部内容。 */
  fullBleed: true,

  async mount(el, ctx) {
    const dom = {
      canvas: h('canvas', { class: 'gl' }),
      cam: h('video', { class: 'offscreen', playsinline: true, muted: true }),
      clip: h('video', { class: 'offscreen', playsinline: true, loop: true }),
      tip: h('div', { id: 'tip', text: '正在准备…', 'aria-live': 'polite' }),
      meta: h('div', { id: 'meta' }),
    }
    const rescan = button('重新扫描', () => resetLock(), { kind: 'ghost', iconName: 'refresh' })
    /**
     * 声音 + 全屏。**共用 `ui.playerControls`**（试播页与宾客页是原生控件，这一页没有）：
     * 声音按钮的标签由 `dom.clip.muted` 派生，而不是在 click 里自己改 —— 上一版就是
     * 自己改的，于是重扫之后视频被重新静音，按钮还写着「静音」，用户点它反而关了声音。
     *
     * 「全屏」在这一页不能是原生全屏：视频是画进 GL 的一块面片，`<video>` 元素本身是
     * 1px 的隐藏元素（见 `.offscreen`），全屏它等于全屏一个看不见的东西。所以这里换成
     * **满屏平铺** —— 同一段视频，占满屏宽、不再跟着照片走。
     *
     * ## 它必须是个**开关**，而且满屏只能显式退出
     *
     * 上一版按下去几乎看不出效果：pipeline 仍然是 LOCKED，下一帧（几十毫秒后）就有一份
     * 合格四角回来，而 `onWorkerMessage` 里「从平铺贴回去」那一支会把 `flat/flatFull`
     * 清零 —— 于是满屏当场被撤销，按钮形同不存在。
     *
     * 裁决：**满屏是用户显式要的，就只能显式退出** —— 再点一次这个按钮、点「重新扫描」、
     * 或者视频播完。所以满屏期间跟踪四角一律忽略（连 `st.quad` 都不写，见
     * `onWorkerMessage`），而这个按钮在「全屏 / 退出全屏」之间来回切。
     */
    const ctl = playerControls(dom.clip, {
      onFullscreen: () => {
        if (!st.lockedPhoto?.mediaUrl) return
        if (st.flatFull) {
          // 退出满屏，**不等于**退出整条平铺续播（见 `exitFullscreen`）。
          // 不在这儿贴回去 —— 满屏期间的四角全被忽略了（连 `st.quad` 都没写），手上
          // 没有一份能用的几何；pipeline 的 `gaveUp` 是**一次性**事件，满屏期间若已经
          // 报过那一次也被上面「进满屏」那支连同其余几何一起丢弃了，退出时不会再补
          // 一份。所以落回的不是"等四角"，而是**已经在放的 70% 平铺**：下一份跟踪
          // 四角一到，`onWorkerMessage` 里「从平铺贴回去」那支会走 4 步 blend 过渡
          // 自然贴合；四角一直不来也不会黑屏，视频已经在屏幕中央续播着。
          exitFullscreen()
          return
        }
        st.flat = true
        st.flatFull = true
        // 平铺期间**必须关掉原生 loop**，理由见 `onWorkerMessage` 里放手那一支。
        dom.clip.loop = false
        st.quad = null
        st.blendStart = 0
        st.filter.reset()
        syncFullLabel()
        tip(TIPS.flat_full)
        setPrimary(rescan)
      },
    })
    rescan.hidden = true
    ctl.hidden = true
    /**
     * 「全屏」按钮的标签。**只能由 `st.flatFull` 派生**，不在 click 里自己改 —— 满屏会被
     * 三条与这个按钮无关的路撤掉（重新扫描、换了一张照片、平铺播完退出锁定），
     * 每条都自己改一次标签的话，总有一条会漏（声音按钮就是这么错过一次的，见上面）。
     */
    const syncFullLabel = () => {
      ctl.full.querySelector('span').textContent = st.flatFull ? '退出全屏' : '全屏'
    }
    /**
     * 退出平铺（不管是 70% 还是满屏）：回到"贴合"那套状态。
     *
     * 四处调用（贴回去、`onHit` 无视频、`onHit` 换照片、重新扫描）每一处都要连着做这
     * 三件事，而漏掉 `loop` 那一件的后果是最难查的：平铺播完靠 `ended` 退出锁定，而
     * 带着 `loop` 的元素在直连回退路径上永远不会发 `ended`。
     *
     * **「退出全屏」不调这个** —— 见 `exitFullscreen`：退出满屏只退那一层，仍然
     * 留在 70% 平铺里，要等下一份四角贴回去时才真正调到这里。
     */
    function exitFlat() {
      st.flat = false
      st.flatFull = false
      // 平铺时关掉的原生 loop 要还回去 —— 贴合状态下播完要接着放（见 onVideoEnded）。
      dom.clip.loop = true
      syncFullLabel()
    }
    /**
     * 退出**满屏**，不等于退出整条平铺续播。用户点「退出全屏」时调。
     *
     * P1 修复：老代码这里调的是 `exitFlat()`，把 `st.flat` 也一并清掉、`loop` 拨回
     * `true`。若照片此时已经离开画面（满屏期间 pipeline 唯一一次 `gaveUp` 被
     * `onWorkerMessage` 的 `st.flatFull` 分支整帧丢弃，见那里的说明），退出后
     * `st.quad` 为空、`lockedPhoto` 还在：渲染循环无几何可画（黑屏只剩相机），没有
     * 70% 平铺兜底，`loop=true` 又永远等不到 `ended` 所以不会自动 `resetLock` ——
     * 用户只能重新对准或点「重新扫描」。
     *
     * 所以这里只退**满屏**这一层：`st.flatFull=false`，**保留** `st.flat=true` 与
     * `dom.clip.loop=false`，立刻退回 70% 平铺续播（渲染循环下一帧就会走 `flat`
     * 兜底那支）；下一份跟踪四角到达时，既有的「从平铺贴回去」分支会在 4 步内自然
     * 贴合并调用 `exitFlat()`。
     */
    function exitFullscreen() {
      st.flatFull = false
      syncFullLabel()
      tip(TIPS.flat)
      setPrimary(rescan)
    }
    /**
     * 开/关相机。**这个按钮同时是权限重试的入口** —— 权限弹窗被划掉、或相机被
     * 别的 App 占着时，原来唯一的出路是刷新整页（引擎白重载一遍）。现在关掉再开
     * 就是一次全新的 getUserMedia：权限还能弹的浏览器会重新弹。
     * 顺带是省电开关：拍完不看了可以把相机关掉，页面留着。
     */
    const camBtn = button('关相机', () => { st.stream ? closeCam() : openCam() }, { kind: 'ghost' })
    camBtn.hidden = true
    /**
     * 「我能扫的照片」。**这是空库与"一直扫不出来"两条死路唯一的出口。**
     *
     * 过去这两种处境下 HUD 只说一句"没有可扫的照片"/"认不出来"，然后什么都不做 ——
     * 用户站在取景器前，屏幕上没有任何可点的东西。空库那一支甚至连相机都没开。
     *
     * 去哪一页由 `browseTab` 定（空库那一支会改它）。**不能改 `browse.onclick`** ——
     * `h()` 是用 `addEventListener` 挂的，赋 `onclick` 只会再挂一个，两个都会跑。
     */
    let browseTab = 'photos'
    const browse = button('我能扫的照片', () => ctx.shell.tab(browseTab), { kind: 'ghost' })
    browse.hidden = true
    /**
     * 库里一张可扫的照片都没有。**这一格一置上，出口按钮就再也不收起来。**
     *
     * 只看 worker 结果的 reason 不够：空库时对着白墙的那一条是 `no_features`，
     * 对着任何有纹理的东西才是 `empty` —— 于是"扫一下白墙，唯一的出口就消失了"。
     * 而库是不是空的与这一帧看到了什么无关，它是这一次 mount 的事实。
     */
    let libEmpty = false

    /**
     * 同一屏只能有**一个**金框按钮 —— 金框的含义是"这一刻该点的就是它"，两个金框等于
     * 没有金框。所以主次不是各按钮自己的属性，而是这一屏的状态：由这个函数一次定完，
     * 其余全部落回 `ghost`。
     */
    const setPrimary = (btn) => {
      // 指向一个**收起来的**按钮就等于这一屏没有金框，而调用点常常判断不了那个按钮
      // 此刻在不在（`rescan` 在没锁定时是 hidden 的）。退化成"谁都不是主动作"总比
      // 把金框发给一个看不见的按钮好。
      // `closest('[hidden]')` 而不是 `btn.hidden`：声音按钮自己从不 hidden，
      // 收起来的是包着它的那个 `ctl`（见 ui.playerControls）。
      const target = btn && !btn.hidden && !btn.closest?.('[hidden]') ? btn : null
      for (const b of [rescan, ctl.sound, camBtn, browse]) b.className = b === target ? '' : 'ghost'
    }

    dom.hud = h('div', { id: 'hud' }, dom.tip, h('div', { class: 'actions' }, rescan, ctl, camBtn, browse), dom.meta)
    el.append(dom.canvas, dom.cam, dom.clip, dom.hud)

    /**
     * 换一句提示。`hit` 为真时左边"啪"地冒一颗星。
     *
     * 那颗星是这个界面唯一的庆祝动作，所以它只在**真的认出来了**时出现 —— 每一条
     * 「认不出来，靠近一点」都冒星的话，它就只是个装饰了。
     */
    const tip = (html, { hit = false } = {}) => {
      dom.tip.innerHTML = `<span>${html}</span>`
      dom.hud.classList.toggle('hit', hit)
    }

    const st = {
      quad: null, quadAt: 0, filter: new QuadFilter(), smoothOut: new Float32Array(8), lastRenderAt: 0,
      lockedPhoto: null, trackPoints: null,
      // `inflight` 是**计数**不是布尔量（见 MAX_INFLIGHT）。每送一帧 +1，
      // 每收到一条 result 或 drop -1 —— 两种回执都要算，漏一种就会永久漏名额。
      frameSeq: 0, inflight: 0, lastSentAt: 0, paused: false,
      // 距上一份证据多久 / 上一条结果说了什么 —— pacing.sendInterval 的两个输入。
      lastEvidenceAt: performance.now(), lastReason: '',
      // 引导文案那一档的三个量：连续几帧落在"看见了但不够"、当前显示的是哪一档、
      // 那一档是什么时候换上去的（防抖，见 GUIDE_DEBOUNCE_MS）。
      weakRun: 0, guideKey: '', guideAt: 0,
      // 切后台时真停相机与视频解码要记住的两件事：视频当时在不在播、相机当时开不开。
      pausedClip: false, wasCamOn: false,
      fps: { n: 0, at: 0, value: 0 }, detectMs: 0, trackMs: 0, lastGrabMs: 0,
      // 走不走 bitmap 那条路。一次失败就永久退回（见 sendFrame）。
      useBitmap: canGrabBitmap(),
      raf: 0, stream: null, renderer: null, grabber: null, alive: true, stopLoad: null,
      // 视频装载到哪一步了、以及那一步的细节数字。前者用来判"阶段变了没有"（播放进度
      // 每秒来好几次，不判的话每次都会重写一遍 tip 并重放那颗星的动画）。
      loadStage: null, loadNote: null,
      // 渲染循环每帧要用的缓冲，跨帧复用而不是每帧 `new Float32Array` —— rAF 频率下
      // 那是持续的小分配，攒起来就是 GC 停顿。`cropKey` 记着上一次算 `crop` 用的
      // 那对 aspect，比例没变就不必重跑 videoCrop（源矩形只由这两个数决定）。
      ndc: new Float32Array(8), hm: new Float32Array(9), clip: new Float32Array(16),
      crop: new Float32Array(4), cropKey: '',
      // 平铺续播：在不在平铺、是不是满屏平铺（全屏按钮）、以及那个矩形本身
      // （每帧重算，但缓冲复用；它同时是"贴回去"那段过渡的起点）。
      flat: false, flatFull: false, flatQuad: new Float32Array(8),
      blendFrom: new Float32Array(8), blendStart: 0, blendQuad: new Float32Array(8),
      // 起播那一句（含标题与内点数）。从平铺贴回去时要把它放回去 —— 不放回去的话
      // 视频已经贴在照片上了，HUD 还写着"跟丢了"。
      hitTip: '',
    }

    /**
     * 取景器底下那一行字。**按调试模式分两套内容。**
     *
     * ## 为什么要分
     *
     * 那一行原来一直显示 `库 45 张 · 无词表·全量扫描 · 24 fps · 检测 149ms ·
     * 跟踪 12ms · 四角已过期 380ms · 跟踪点 83 · 视频 1920×1080`。对着这行字的人是
     * **宾客**，他要的答案只有一个："我该怎么做才能看到视频"。上面那些数字一个都不
     * 回答这个问题，而它们挤在一起还会把真正有用的那半句（"这张照片没配视频"）盖掉。
     *
     * 所以非调试模式下只留**用户能据此行动**的那几条，而且说人话：
     *
     * | 状态 | 非调试模式 | 调试模式 |
     * |---|---|---|
     * | 一切正常 | （空） | 库/帧率/耗时/四角年龄/跟踪点/视频分辨率 |
     * | 这张没配视频 | 「这张照片还没配视频」 | `无视频` |
     * | 视频在加载 | 「视频加载中…」 | `视频加载中 rs=1` |
     * | 视频出错 | 「视频播不了」 | `视频错误 4` |
     * | 词表没训 | （空 —— 那只影响速度，不影响结果） | `无词表·全量扫描` |
     *
     * 词表那条是这里最值得说明的一个判断：它**不是**一个用户能处理的问题（要管理员去
     * 训），也不影响识别结果（只影响耗时），所以对宾客它是纯噪音。同理"库 45 张"——
     * 那个数字在首页已经说过了（"你有 N 张照片可扫"），在取景器里重复一遍只占地方。
     */
    // 每帧都可能调 meta()（fps 统计、worker 回执…），但它的净效果只是重写一个
    // `textContent` —— 250ms 内的重复调用付出的是同一次字符串拼接与同一次布局脏标记。
    // `force` 留给"这一刻的变化必须立刻可见"的几个调用点（视频出错、终局阶段…）。
    let metaAt = 0
    let metaLast = ''
    const meta = (force = false) => {
      const now = performance.now()
      if (!force && now - metaAt < 250) return
      metaAt = now
      const debug = isDiagEnabled()
      const parts = []
      const v = dom.clip

      if (debug) {
        const lib = ctx.libInfo?.()
        if (lib) {
          parts.push(`库 ${lib.nPhotos} 张`)
          if (lib.skipped?.length) parts.push(`${lib.skipped.length} 张不在识别库`)
          if (lib.hasVocab === false) parts.push('无词表·全量扫描')
        }
        if (st.fps.value) parts.push(`${st.fps.value} fps`)
        // 相机实际给到的帧率/分辨率——确认长边仍 ≥1280（见 camera.js 的 `ideal: longEdge`
        // 说明），且 Task 6 Step 2 的 `frameRate: {ideal:30,max:30}` 真的生效了。
        const camSettings = st.stream?.getVideoTracks?.()[0]?.getSettings?.()
        if (camSettings) {
          parts.push(`相机 ${camSettings.width}×${camSettings.height}@${Math.round(camSettings.frameRate ?? 0)}fps`)
        }
        parts.push(`送帧 ${sendInterval(paceArgs())}ms`)
        if (st.detectMs) parts.push(`检测 ${st.detectMs}ms`)
        if (st.trackMs) parts.push(`跟踪 ${st.trackMs}ms`)
        if (st.lockedPhoto) {
          const age = st.quadAt ? Math.round(performance.now() - st.quadAt) : null
          parts.push(st.quad ? `贴合中 ${age}ms前` : age === null ? '无四角' : `四角已过期 ${age}ms`)
          if (st.flat) parts.push(st.flatFull ? '平铺续播·满屏' : '平铺续播')
          if (st.trackPoints) parts.push(`跟踪点 ${st.trackPoints}`)
          if (st.filter?.correcting) parts.push('纠正滑行中')
          if (st.filter?.rejected) parts.push(`毛刺 ${st.filter.rejected}`)
          if (st.loadNote) parts.push(`${st.loadStage} ${st.loadNote}`)
          if (!st.lockedPhoto.mediaUrl) parts.push('无视频')
          else if (v.error) parts.push(`视频错误 ${v.error.code}`)
          else if (v.readyState < 2) parts.push(`视频加载中 rs=${v.readyState}`)
          else if (v.paused) parts.push('视频已暂停')
          else parts.push(`视频 ${v.videoWidth}×${v.videoHeight}`)
        }
      } else if (st.lockedPhoto) {
        // 认出来了但看不到画面时才说话。其余时候留空 —— 该说的话在上面那条 tip 里。
        if (!st.lockedPhoto.mediaUrl) parts.push('这张照片还没配视频')
        else if (v.error) parts.push('视频播不了')
        // 装载过程中的细节数字（`3.2 / 8.1 MB`、`本机已有，秒开`）走这里而**不是** tip：
        // tip 有 min-height: 40px，文字长度每 200ms 变一次会让整块木牌高度抖动，
        // 而用户正在读它。阶段名留在 tip 上（每阶段才换一次）。
        else if (st.loadNote) parts.push(st.loadNote)
        else if (v.readyState < 2) parts.push('视频加载中…')
        else if (v.paused) parts.push('视频已暂停')
        // 贴合准确度。**只在不稳时说话**（稳的时候这行字本身就是干扰），
        // 而且说的是"怎么办"不是数字：跟踪点少 = 角度太斜/太远/反光，
        // 这三样宾客都能自己调整。分档阈值见 fitQuality()。
        const q = fitQuality()
        if (q) parts.push(q)
      }
      const s = parts.join(' · ')
      if (s !== metaLast) { metaLast = s; dom.meta.textContent = s }
    }
    /**
     * 贴合准确度分档。判据是**跟踪内点数**（st.trackPoints，每帧更新）：
     * 它同时反映角度、距离、遮挡、反光 —— 正是贴合质量的直接决定量（内点少 →
     * RANSAC 解不稳 → 四角抖/偏）。阈值对照 pipeline 里的两个常量：
     * < 16（RESEED_MIN_INLIERS）已经贴近放手线；< 28 处在补种子频繁触发的区间。
     */
    function fitQuality() {
      const n = st.trackPoints
      if (!st.quad || n == null) return null
      if (n < 16) return '贴合很不稳 —— 正对照片、再靠近一点'
      if (n < 28) return '贴合一般 —— 角度小一点会更稳'
      return null   // 稳的时候不说话
    }

    // 连点三下**关掉**调试模式（开不了 —— 入口在设置页连按版本号，见 diag.js 的
    // `bindToggle`）。绑在这条读数上：调试时手机就在手上，跑回设置页很烦。
    ctx.bindDiagToggle?.(dom.meta)

    // ── 视频那一环的打点 ────────────────────────────────────────────
    const onVideoErr = () => {
      const e = dom.clip.error
      diagAlways(`视频 error code=${e?.code} ${MEDIA_ERR[e?.code] ?? '?'}` +
        ` msg=${e?.message || '(空)'} network=${NETWORK_STATE[dom.clip.networkState]}` +
        ` ready=${READY_STATE[dom.clip.readyState]} src=${short(dom.clip.currentSrc)}`)
      meta(true)
    }
    dom.clip.addEventListener('error', onVideoErr)
    const videoEvents = ['loadstart', 'loadedmetadata', 'canplay', 'playing', 'pause', 'waiting', 'stalled']
    const onVideoEvent = (ev) => {
      diag(() => `视频 ${ev.type} ready=${READY_STATE[dom.clip.readyState]}` +
        ` network=${NETWORK_STATE[dom.clip.networkState]}` +
        (dom.clip.videoWidth ? ` ${dom.clip.videoWidth}×${dom.clip.videoHeight}` : ''))
      meta()
    }
    for (const ev of videoEvents) dom.clip.addEventListener(ev, onVideoEvent)

    /**
     * 手动兜底循环。元素上已经有 `loop: true`（见 dom.clip 定义），但视频喂给的是
     * `MediaSource`（mp4stream.js）而不是普通 `src` —— `endOfStream()` 之后原生 `loop`
     * 在 Chromium 系上不可靠（老 bug：播完定在最后一帧，不重新播），表现就是"贴合还在，
     * 视频却像暂停了"。这里显式接管：播完就跳回 0 重新播，不管原生 loop 有没有生效。
     */
    const onVideoEnded = () => {
      // 平铺状态下播完就**退出**，不循环：那时视频已经不贴在任何东西上了，
      // 让它在屏幕中央无限重播等于把取景器一直占着。
      if (st.flat) {
        diag(() => '视频 ended（平铺续播）→ 退出锁定')
        resetLock()
        return
      }
      dom.clip.currentTime = 0
      dom.clip.play().catch(() => {})
      diag(() => '视频 ended → 手动循环')
    }
    dom.clip.addEventListener('ended', onVideoEnded)

    function resetLock() {
      ctx.worker?.postMessage({ type: 'reset' })
      st.quad = null
      st.filter.reset()
      st.lockedPhoto = null
      // 下一张锁定的照片可能是另一个 aspect —— 不清的话第一帧会误用上一张的缓存 crop。
      st.cropKey = ''
      // 先掐流再动元素：不掐的话上一段的 fetch 还在往一个已经换了 src 的
      // SourceBuffer 里喂，报的是一个跟「重新扫描」毫无关系的 append 错误。
      st.stopLoad?.()
      st.stopLoad = null
      st.loadStage = null
      st.loadNote = null
      ctx.progress?.(null, { hide: true })
      dom.clip.pause()
      dom.clip.removeAttribute('src')
      delete dom.clip.dataset.photo
      dom.clip.load()
      rescan.hidden = true
      ctl.hidden = true
      st.lastEvidenceAt = performance.now(); st.lastReason = ''
      st.weakRun = 0; st.guideKey = ''; st.guideAt = 0
      // 下一段视频是从贴合开始的，所以平铺（含满屏）连带那个按钮的标签一起撤掉。
      exitFlat()
      st.blendStart = 0; st.hitTip = ''
      setPrimary(null)
      tip(guideTip({}).text)
    }

    /**
     * 把装载阶段画到这一页合适的位置去。**三个位置按变化频率分工，这是这段代码的全部内容。**
     *
     * | 位置 | 放什么 | 变化频率 |
     * |---|---|---|
     * | `#tip`（HUD 大字） | 阶段名：`正在取视频…` → `正在下载视频…` → `内点 42。` | 每阶段一次 |
     * | `#meta`（HUD 小字） | 细节数字：`3.2 / 8.1 MB`、`本机已有，秒开` | 每 200ms |
     * | `#bar`（顶部金条） | 百分比 / 扫描动画 / **起播后接着当播放进度** | 每帧 |
     *
     * 数字**不能**放进 tip：它有 `min-height: 40px`，文字长度每 200ms 变一次会让整块
     * 木牌高度抖动 —— 而用户正在读它。
     *
     * 播放那一支要单独早退：`timeupdate` 每秒来好几次，每次都重写 tip 的话那颗庆祝的星
     * 会被反复重放（`hit` 类一摘一挂就是一次动画）。
     */
    function paintStage(s, title, inliers) {
      const changed = st.loadStage !== s.stage
      st.loadStage = s.stage
      // -1 = 不定长（走扫描动画）。**不给不定长的阶段编一个假百分比** —— 编出来的数字
      // 会让用户对剩余时间形成一个必然错误的预期。理由与 ui.js 的 loading() 同一条。
      // `label` 是这条金条的 `aria-label`：**必须换**，不换的话读屏会在播视频时
      // 一直念「加载识别引擎 40%」（那是引擎加载时留在上面的那句）。
      const bar = (pct, label) =>
        ctx.progress?.(typeof pct === 'number' ? pct * 100 : -1, { label })

      if (s.stage === Stage.PLAYING) {
        bar(s.pct, '播放进度')
        if (!changed) return
        st.loadNote = null
        ctl.hidden = false
        // 视频起播了：这一刻该点的是「开声音」（默认静音起播，见 onHit），不是「重新扫描」。
        setPrimary(ctl.sound)
        st.hitTip = `认出了 <b>${title}</b>，内点 ${inliers}。`
        tip(st.hitTip, { hit: true })
        meta()
        return
      }

      st.loadNote = s.text || null
      const terminal = s.stage === Stage.UNAVAILABLE || s.stage === Stage.ERROR
      if (terminal) {
        st.loadNote = null
        ctx.progress?.(null, { hide: true })
        // 失败这一句用不加粗的 title：加粗是"认出来了而且能看"的样子，而这里看不到。
        tip(`认出了 ${title}，但${esc(s.text)}。`, { hit: true })
      } else if (changed) {
        // **阶段名，不是 `stageText`。** 后者在下载那一档给的是数字（`0.0 / 8.1 MB`），
        // 而那串数字此刻已经在下面那行小字里了（`st.loadNote`）—— 同一句显示两遍，
        // 且 tip 上那句读起来不知道在干什么。上面那张表说的就是这条分工。
        tip(`认出了 <b>${title}</b>，${stageName(s.stage, { fromCache: s.fromCache }) || '正在加载'}…`, { hit: true })
        bar(s.pct, stageName(s.stage, { fromCache: s.fromCache }) || '加载视频')
      } else {
        bar(s.pct, stageName(s.stage, { fromCache: s.fromCache }) || '加载视频')
      }
      // 终局（不管是不是这一秒里最新的一条）必须立刻可见：往后不会再有下一条把它盖住。
      meta(terminal)
    }

    /**
     * 命中之后加载视频。链路本身在 `mediaload.js`（扫描页、试播页、宾客页三处共用），
     * 这里只负责把阶段画出来 —— 见 `paintStage`。
     *
     * 那条链路为什么绕（元信息与流是两个地址、票据、MediaSource）写在 `mediaload.js`
     * 与 `mp4stream.js` 顶部，不在这里重复。
     */
    async function onHit(m) {
      const photo = m.photo
      st.lockedPhoto = photo
      rescan.hidden = false
      // 转义一次，之后各处拼进 tip() 的 innerHTML 就不用再操心 —— photo.title 是
      // 管理员填的自由文本，一个 `<` 就会把后面的标签吃掉。
      const title = photo?.title ? `「${esc(photo.title)}」` : '这张照片'
      if (!photo?.mediaUrl) {
        // **这一支要把上一段视频整个撤掉，不只是退出平铺。** 锁定的已经是另一张（没视频的）
        // 照片了，而上一段视频还在元素里：清了平铺却不撤视频的话，新照片的四角一到，
        // 渲染循环的贴合分支就把**旧视频**贴到新照片上继续播（那条分支只看几何与
        // `readyState`，不查 mediaUrl）—— 表现是"这张照片配的是别人的视频"。
        // 所以照 `resetLock()` 里那几行来，顺序也一样：先掐流再动元素。
        exitFlat()
        st.blendStart = 0
        st.hitTip = ''
        st.stopLoad?.()
        st.stopLoad = null
        st.loadStage = null
        st.loadNote = null
        ctx.progress?.(null, { hide: true })
        dom.clip.pause()
        dom.clip.removeAttribute('src')
        delete dom.clip.dataset.photo
        dom.clip.load()
        ctl.hidden = true
        diagAlways(`命中 ${photo?.id?.slice(0, 8)} 但没有 mediaUrl（这张没配视频）`)
        tip(`认出了 ${title}，但它还没有配视频。`, { hit: true })
        // 声音/全屏刚被收起来了（没视频可放），这一屏能做的只剩「重新扫描」。
        setPrimary(rescan)
        meta(true)
        return
      }
      if (dom.clip.dataset.photo === photo.id) {
        // 同一张照片再次命中：已经在播了，别重来一遍（重来会从头播）。
        return
      }
      // 换了一张照片：上一段的平铺状态跟这一段无关（新视频要从贴合开始）。
      exitFlat()
      st.blendStart = 0
      st.hitTip = ''
      dom.clip.dataset.photo = photo.id
      diagAlways(`命中 ${photo.id?.slice(0, 8)} 内点=${m.inliers} aspect=${photo.aspect ?? 'null'} → 取媒体信息`)
      dom.clip.muted = true
      st.stopLoad?.()
      st.loadStage = null
      st.stopLoad = loadPhotoVideo(dom.clip, photo.id, {
        onStage: (s) => { if (st.alive) paintStage(s, title, m.inliers) },
        onDiag: diagAlways,
      })
    }

    function onWorkerMessage(ev) {
      const m = ev.data
      if (!st.alive) return
      if (m.type === 'error') {
        diagAlways(`Worker 错误 ${m.message}`)
        tip(`<span class="bad">${esc(m.message)}</span>`)
        return
      }
      // 丢帧回执：没有几何可用，只还名额然后立刻补一帧。
      if (m.type === 'drop') {
        st.inflight = Math.max(0, st.inflight - 1)
        maybeSend()
        return
      }
      if (m.type !== 'result') return
      traceResult(performance.now(), m)
      st.inflight = Math.max(0, st.inflight - 1)
      // **立刻补下一帧，不等 rAF。** 这一行与 MAX_INFLIGHT 一起构成流水线：
      // worker 刚腾出手，下一帧已经抓好在排队了。
      maybeSend()
      if (m.state === 'locked' && m.ms) st.trackMs = m.ms
      else if (m.ms) st.detectMs = m.ms
      st.trackPoints = m.inliers ?? null
      st.lastReason = m.reason ?? ''
      // 证据 = 命中 / 有四角 / 累积链在攒。任何一样都说明"照片就在画面里"，节奏回满速。
      if (m.fresh || m.quad || (m.streak && m.streak.n > 0)) st.lastEvidenceAt = performance.now()
      diag(() => `${m.state === 'locked' ? '跟踪' : '检测'} ${m.reason}` +
        ` 内点=${m.inliers ?? '-'} ${m.ms}ms 四角=${m.quad ? '有' : '无'}` +
        (m.streak ? ` 累积 ${m.streak.n}/${m.streak.need}` : '') +
        (m.tracked ? ` 光流存活=${m.tracked}` : '') +
        (m.reseeded ? ` 补种子→${m.reseeded}` : '') +
        (m.reanchorFails ? ` 重锚失败 ${m.reanchorFails}/3` : '') +
        (m.corrected ? ' 纠正帧' : '') +
        (m.gaveUp ? ' 放手' : ''))
      // 累积命中要与单帧命中**分得开**。不分的话，跨帧累积带来的误识别会混进单帧
      // 命中里，永远量不出来（服务端那边靠 recognize_log 的 reason 分，这边靠这一行）。
      if (m.reason === 'streak') diagAlways(`累积命中：连续 ${thresholds.streakNeed} 帧一致，内点 ${m.inliers}（单帧门槛 ${thresholds.minInliers}）`)

      // 命中就加载视频，与这一帧四角合不合格**无关**（命中那一帧算不出四角是常态）。
      if (m.fresh) onHit(m).catch((e) => diagAlways(`onHit 失败 ${e.message}`))

      /**
       * 满屏平铺期间**这一帧的几何整个扔掉** —— 连 `st.quad` 都不写。
       *
       * 满屏是用户显式要的，所以只能显式退出（再点一次「全屏」/「重新扫描」/ 视频播完，
       * 见 `onFullscreen` 那条裁决）。而 pipeline 在满屏期间照样是 LOCKED、照样每几十
       * 毫秒送回一份合格四角：一写进 `st.quad`，渲染循环下一帧就优先拿它去贴合（那是
       * `quadToDraw` 三个来源里的第一个），下面那支「从平铺贴回去」还会顺手把
       * `flat/flatFull` 清零 —— 满屏当场被撤销，按钮形同不存在。这就是上一版的 bug。
       *
       * 代价是满屏期间 `paceArgs().locked` 恒为 false（`st.quad` 一直是空的），送帧
       * 节奏回到"找照片"那一档满速。**可接受**：满屏时贴合已经不重要了，而满速扫描
       * 意味着退出满屏后能最快拿到第一份四角贴回去。
       */
      if (st.flatFull) {
        // 什么都不做，见上面。
      } else if (m.quad && plausible(m.quad)) {
        if (st.flat) {
          // 从平铺贴回去：起点是当前平铺矩形，4 步 200ms 走到跟踪四角（像素风只用 steps）。
          // `plausible` 挡的是"还没画过一帧平铺"（`flatQuad` 还是零）—— 从一个退化的
          // 四边形插过去会有 200ms 的怪形状，那种情况下直接贴上更好。
          if (plausible(st.flatQuad)) { st.blendFrom.set(st.flatQuad); st.blendStart = performance.now() }
          // 贴回照片上了 → 循环恢复（贴合状态下播完要接着放，见 onVideoEnded）。
          // 这里只会是 70% 平铺 —— 满屏那一档在上面就被挡掉了，走不到这儿。
          exitFlat()
          setPrimary(ctl.sound)
          // 把起播那一句放回去：视频已经贴回照片上了，HUD 不能还写着"跟丢了"。
          if (st.hitTip) tip(st.hitTip, { hit: true })
        }
        const at = performance.now()
        st.quad = m.quad
        st.quadAt = at
        // **这个四角测的是多久之前的画面** —— 抓帧那一刻到现在。滤波器要补的就是它。
        // `m.grabbedAt` 由 worker 原样带回（见 worker.js），拿不到就退回用跟踪耗时估。
        const age = m.grabbedAt ? Math.max(0, at - m.grabbedAt) : (m.ms ?? 0)
        // `corrected` = 这个四角是重锚纠正落地的那一帧（pipeline 打的标）——
        // 滤波器对它直接滑行而不是当成运动去跟（真机抓到的每 2 秒一次的瞬跳）。
        st.filter.observe(m.quad, at, age, m.corrected ? { correction: true } : null)
      } else if (m.gaveUp) {
        // 放手时撤掉四角但**不停视频** —— 用户可能只是手抖了一下，重新检测通常一两秒
        // 就回来。停了再播会从头开始，那比继续播难看得多。
        st.quad = null
        st.filter.reset()
        // 视频真的在放 → 退到屏幕中央继续放（平铺续播），而不是让画面空着。
        // 判据用 video 元素自己的状态：`st.flat` 一置上，`paceArgs().locked` 就回到
        // 未锁定，送帧节奏自动回满速去找照片 —— 这正是我们想要的。
        const playing = st.lockedPhoto?.mediaUrl && !dom.clip.paused && dom.clip.readyState >= 2
        if (playing) {
          if (!st.flat) {
            st.flat = true
            // **关掉原生 loop。** 平铺播完要退出锁定（见 onVideoEnded），而那靠 `ended`
            // 事件；元素上带着 `loop` 属性，在 mp4stream 的两条直连回退路径
            //（没有 MediaSource、或 addSourceBuffer 失败 → 普通 `src`）上原生 loop
            // 是生效的，于是 `ended` 永远不来，视频就在屏幕中央无限重播占着取景器。
            // MediaSource 那条路上原生 loop 不可靠才需要手动循环，两件事别搞混。
            dom.clip.loop = false
            tip(TIPS.flat)
            setPrimary(rescan)
          }
        } else {
          setPrimary(rescan)
          tip(TIPS[m.reason] ?? guideTip({}).text)
        }
      } else if (!st.lockedPhoto) {
        // 扫描阶段的引导：分档在 `guide.js`（那里能按内点/连续帧/多久没证据分，
        // 而"攒到 2/3"与"认不出来"要用户做的是**相反**的事 —— 一个别动，一个换姿势）。
        st.weakRun = (m.reason === 'weak' && (m.inliers ?? 0) >= CLOSER_MIN_INLIERS) ? st.weakRun + 1 : 0
        const g = guideTip({ reason: m.reason, inliers: m.inliers, weakRun: st.weakRun, idleMs: performance.now() - st.lastEvidenceAt, streak: m.streak })
        // 2 秒内不换句子（攒证据那句除外：进度数字要实时）。换得太快用户一句都读不完。
        const now = performance.now()
        if (g.key !== st.guideKey && (g.key === 'streak' || st.guideKey === 'streak' || now - st.guideAt >= GUIDE_DEBOUNCE_MS)) {
          st.guideKey = g.key; st.guideAt = now
          tip(g.text)
        } else if (g.key === 'streak') tip(g.text)
        /**
         * 出口按钮跟着**正在显示的那一档**（`st.guideKey`）走，不是这一帧算出来的
         * `g.key` —— 文案有 2 秒防抖，跟 `g.key` 的话按钮会在"屏上还写着靠近一点"时
         * 就已经变成了另一种状态的样子。
         *
         * 两档要出口：`not_in_library`（一直认不出来，那句话本身就指着这个按钮）与
         * **`empty`**（库里没有可扫的照片 —— verify 对空候选集给的就是这个 reason）。
         * 少了 `empty` 这一档的后果是：空库时点「开相机」，第一条 worker 结果就把这个
         * 按钮收起来，而 `setPrimary` 指着它 —— 屏幕上既没有金框也没有出口。
         */
        const showBrowse = libEmpty || st.guideKey === 'not_in_library' || st.guideKey === 'empty'
        if (showBrowse === browse.hidden) {
          browse.hidden = !showBrowse
          setPrimary(showBrowse ? browse : null)
        }
      }
      meta()
    }
    ctx.worker.addEventListener('message', onWorkerMessage)

    // ── 相机 ────────────────────────────────────────────────────────
    /**
     * 开相机。mount 时自动调一次，之后由 camBtn 反复调 —— 每次都是一次全新的
     * `getUserMedia`，这正是「不用刷新就能重新请求权限」的实现。
     *
     * 失败**不再是这一页的终局**（曾经是：整屏错误 + return，唯一出路是刷新）。
     * 错误照样整屏说清原因（含 iOS 微信那条），但按钮留着 —— 处理好权限点一下就行。
     */
    async function openCam() {
      camBtn.disabled = true
      dom.camErr?.remove()
      dom.camErr = null
      tip('正在开相机…')
      try {
        st.stream = await openCamera(dom.cam)
      } catch (e) {
        st.stream = null
        camBtn.disabled = false
        camBtn.hidden = false
        camBtn.querySelector('span').textContent = '重新开相机'
        dom.camErr = h('div', { class: 'gate-inline' },
          h('p', { class: 'bad', text: e instanceof CameraError ? e.message : `开相机失败：${e.message}` }))
        el.appendChild(dom.camErr)
        // 同 closeCam：这一屏唯一能做的事就是再点一次它。
        setPrimary(camBtn)
        tip('相机没开起来。按上面说的处理好，点「重新开相机」。')
        return
      }
      if (!st.alive) {
        stopCamera(st.stream)
        st.stream = null
        return
      }
      if (document.hidden) {
        // 等相机的这几百毫秒里页面又被切到后台了：立刻停掉，标记等回前台再开。
        stopCamera(st.stream); st.stream = null; st.wasCamOn = true
        camBtn.disabled = false
        return
      }
      camBtn.disabled = false
      camBtn.hidden = false
      camBtn.querySelector('span').textContent = '关相机'
      st.renderer ??= new Renderer(dom.canvas)
      st.grabber ??= new FrameGrabber(dom.cam)
      st.lastEvidenceAt = performance.now(); st.lastReason = ''
      // 分档恢复。openCam() 一进来就把 tip 整句改写成「正在开相机…」（见上面），
      // 成功后不能不分青红皂白地恢复成扫描引导 —— 满屏或平铺中切后台再回前台，
      // 那样会把木牌钉在「正在开相机…」上，退出说明就此消失（见 P3 记录）。
      if (st.flatFull) tip(TIPS.flat_full)
      else if (st.flat) tip(TIPS.flat)
      else if (st.hitTip) tip(st.hitTip, { hit: true })
      else tip(guideTip({}).text)
      cancelAnimationFrame(st.raf)
      st.raf = requestAnimationFrame(loop)
    }

    /** 关相机：停流、停渲染循环、把已锁定的视频也撤掉。页面与引擎都留着。 */
    function closeCam() {
      if (st.stream) stopCamera(st.stream)
      st.stream = null
      cancelAnimationFrame(st.raf)
      st.renderer?.clear()
      resetLock()
      camBtn.querySelector('span').textContent = '开相机'
      // 相机关了之后屏幕上能做的事只剩这一个（`resetLock` 刚把金框收了），
      // 而那句提示正让用户去点它 —— 所以它就是这一屏的主动作。
      setPrimary(camBtn)
      tip('相机已关。点「开相机」继续扫。')
    }

    /**
     * 空库：**不开相机，但也不能是死路。**
     *
     * 上一版只说一句"这个账号下还没有可扫的照片"就结束了 —— 没有相机画面、没有一个
     * 可点的东西，而两种人在这一刻要做的事完全不同：管理员该去传一张（素材页），
     * 宾客该去看看自己有哪些照片（照片页，也许只是这台设备的库还没同步下来）。
     * 相机按钮也留着：库是 mount 那一刻的快照，用户可能刚在别的页面传完。
     */
    const lib = ctx.libInfo?.()
    if (lib && lib.nPhotos === 0) {
      libEmpty = true
      tip(guideTip({ reason: 'empty' }).text)
      browse.hidden = false
      browse.querySelector('span').textContent = ctx.isAdmin() ? '去素材页传一张' : '看看我的照片'
      browseTab = ctx.isAdmin() ? 'media' : 'photos'
      setPrimary(browse)
      camBtn.hidden = false
      camBtn.querySelector('span').textContent = '开相机'
      meta(true)
    } else {
      await openCam()
    }

    function loop(now) {
      if (!st.alive) return
      // 相机关了就不再自续 —— closeCam 已经 cancel 过一次，这里是防「cancel 和
      // 一帧回调赛跑」的兜底：raf 回调可能已经在队里了。
      if (!st.stream) return
      st.raf = requestAnimationFrame(loop)
      const r = st.renderer
      if (!r) return
      const canvasAspect = r.resize()
      const vw = dom.cam.videoWidth
      const vh = dom.cam.videoHeight
      if (!vw || !vh) return
      r.clear()
      r.drawCamera(dom.cam, vw / vh, canvasAspect)

      const age = now - st.quadAt
      const dtFrame = st.lastRenderAt ? now - st.lastRenderAt : 0
      let drew = 0
      // 这一帧要画在哪四个角上（归一化图像坐标）。三个来源：跟踪四角、跟踪四角 +
      // 「从平铺贴回去」的过渡、平铺矩形。**统一成一个变量**是关键 —— 平铺与贴合
      // 因此走的是同一条渲染路径，两者之间才可能插值。
      let quadToDraw = null
      if (st.quad && age <= TTL_MS) {
        // 自适应预测滤波：静止时重平滑压噪声、运动时按速度外推补掉管线那 88ms。
        // 观测在 `onWorkerMessage` 里喂进去（那才是它到达的时刻），这里只问"现在画哪"。
        quadToDraw = st.filter.at(now, st.smoothOut) ?? st.quad
        // 平铺 → 贴合的 4 步过渡
        const t = now - st.blendStart
        if (st.blendStart && t < BLEND_MS) {
          const k = Math.floor(t / (BLEND_MS / BLEND_STEPS)) / BLEND_STEPS
          for (let i = 0; i < 8; i++) st.blendQuad[i] = st.blendFrom[i] + (quadToDraw[i] - st.blendFrom[i]) * k
          quadToDraw = st.blendQuad
        } else st.blendStart = 0
      } else if (st.quad && age > TTL_MS) {
        st.quad = null
        st.filter.reset()
      }
      if (!quadToDraw && st.flat && st.lockedPhoto?.mediaUrl) {
        const photoAspect = st.lockedPhoto.aspect > 0 ? st.lockedPhoto.aspect : 1.5
        quadToDraw = flatQuadImage(photoAspect, vw / vh, canvasAspect, st.flatFull ? 1.0 : FLAT_WIDTH, st.flatQuad)
      }
      // `st.lockedPhoto?.mediaUrl` 是**这一页唯一挡住"把旧视频贴到新照片上"的地方**：
      // 平铺那一支自己查过，贴合这一支不查 —— 它只看几何与 `readyState`，而元素里那段
      // 视频属于谁它无从得知。锁到一张没配视频的照片时（见 onHit 那一支）四角照样会来，
      // 于是上一段视频会被贴上去继续播。onHit 那边已经把元素撤了，这里是第二道。
      if (quadToDraw && st.lockedPhoto?.mediaUrl) {
        const ndc = imageToNdc(quadToDraw, vw / vh, canvasAspect, st.ndc)
        const hm = unitSquareH(ndc, st.hm)
        if (hm) {
          const photoAspect = st.lockedPhoto?.aspect > 0 ? st.lockedPhoto.aspect : 1.5
          const videoAspect = dom.clip.videoWidth > 0 ? dom.clip.videoWidth / dom.clip.videoHeight : 0
          // 源矩形只由这两个 aspect 决定 —— 比例没变（绝大多数帧都没变）就不必重跑
          // videoCrop 的除法与分支，直接复用上一次写好的 st.crop。
          const key = `${photoAspect}|${videoAspect}`
          if (key !== st.cropKey) { videoCrop(photoAspect, videoAspect, st.crop); st.cropKey = key }
          // 面片**恒等于整张照片**（FULL_RECT），比例对不上时裁的是源（videoCrop）——
          // 也就是 object-fit: cover。上一版反过来：缩面片、留出照片边缘。
          if (clipVertices(hm, FULL_RECT, st.clip)) {
            const ok = r.drawVideoQuad(st.clip, dom.clip, st.crop)
            drew = ok ? 1 : 0
            if (!ok) diag(() => `几何 OK 但没画：视频 ready=${READY_STATE[dom.clip.readyState]}`)
          } else {
            diag('clipVertices 拒了这一帧（四边形跨越无穷远线或退化）')
          }
        }
      }
      // 打桩：这一帧**实际用到**的几何与时序。放在渲染之后、送帧之前 ——
      // 它记的是"画出去的那一帧"，而不是"我们希望画出去的"。
      traceRender(now, {
        // 上一帧的抓帧耗时。抓帧在这行之后（渲染完才抓），所以只能记上一帧的 ——
        // 而"上一帧抓了多久"正好解释"这一帧为什么来晚了"。
        // 上一帧的抓帧耗时，**用完清零**。不清的话它在每一帧上都是非零的，
        // 于是"迟到的帧里有多少跟在抓帧后面"这个统计恒等于 100% —— 那是统计方法
        // 造出来的结论，不是数据里的。踩过一次。
        grabMs: st.lastGrabMs,
        fps: st.fps.value,
        quadAge: st.quad ? age : -1,
        drew,
        dt: dtFrame,
        smooth: st.quad ? st.smoothOut : null,
        raw: st.quad,
      })
      st.lastGrabMs = 0
      st.lastRenderAt = now
      flushDiag()

      st.fps.n++
      if (now - st.fps.at > 1000) {
        st.fps.value = Math.round((st.fps.n * 1000) / (now - st.fps.at))
        st.fps.n = 0
        st.fps.at = now
        meta()
      }

      // 这里仍留一次 `maybeSend`，但它只是**兜底**：正常节奏由 worker 的回执驱动
      // （见 onWorkerMessage 末尾）。留着是为了盖住"一帧都没在途"的冷启动与
      // 从暂停恢复那一刻 —— 那时没有任何回执会到来，只能靠渲染循环把泵重新压起来。
      maybeSend()
    }

    /**
     * 该送就送。**从两个地方调：渲染循环（兜底）与 worker 回执（正常节奏）。**
     *
     * 挂在 rAF 上是改造前的做法，它凭空加了一段等待：结果回来的时刻与下一个 rAF tick
     * 之间平均差 8ms，而整个周期才 65ms。现在回执一到就立刻补帧，不必等下一帧渲染。
     */
    /** `sendInterval` 的四个输入。`maybeSend` 与 debug 读数（meta）共用，避免写两遍。 */
    function paceArgs() {
      return {
        locked: Boolean(st.lockedPhoto && st.quad),
        motion: st.filter.motion,
        idleMs: performance.now() - st.lastEvidenceAt,
        reason: st.lastReason,
      }
    }

    function maybeSend() {
      if (!st.alive || st.paused || !st.stream || !st.grabber) return
      if (st.inflight >= MAX_INFLIGHT) return
      const now = performance.now()
      const interval = sendInterval(paceArgs())
      if (now - st.lastSentAt < interval) return
      st.lastSentAt = now
      st.inflight++
      sendFrame(now)
    }

    /**
     * 送一帧去识别。**两条路，首选把 RGBA 转换挪出主线程的那条。**
     *
     * 真机实测（1280×960）：老路 `getImageData` 在主线程上花 65ms，抓帧因此占掉主线程
     * 的 55%，22.5% 的帧迟到、其中 92% 正好跟在一次抓帧之后 —— 那就是"不丝滑"。
     * 新路主线程只付 `createImageBitmap` 的 19.4ms，剩下的 10.2ms 在 worker 上。
     *
     * `grabbedAt` 两条路都带：滤波器要知道"这个结果测的是多久之前的画面"，而那必须
     * 来自**抓帧**那一刻，不是"收到结果"减去计算耗时（后者漏掉了排队等待）。
     */
    function sendFrame(now) {
      const t0 = performance.now()
      const post = (payload, transfer) => {
        st.lastGrabMs = Math.round((performance.now() - t0) * 10) / 10
        ctx.worker.postMessage({ type: 'frame', id: ++st.frameSeq, grabbedAt: now, ...payload }, transfer)
      }
      if (st.useBitmap) {
        st.grabber.grabBitmap().then((got) => {
          // 没送出去的每一条路都要**把名额还回来** —— 名额只会被 worker 的回执减掉，
          // 而这几条根本走不到 worker。漏一条就少一个名额，漏够 MAX_INFLIGHT 次
          // 贴合就彻底停住，且没有任何报错。
          if (!st.alive) { got?.bitmap?.close(); st.inflight--; return }
          if (!got) { st.inflight--; return }
          post({ width: got.width, height: got.height, bitmap: got.bitmap }, [got.bitmap])
        }).catch((e) => {
          // 一次失败就永久退回老路。`createImageBitmap` 在某些机型/某些 track 状态下
          // 会抛，而每帧都试一次然后 fallback 等于每帧都付一次异常的代价。
          diagAlways(`createImageBitmap 失败，退回 getImageData：${e?.message ?? e}`)
          st.useBitmap = false
          st.inflight--
        })
        return
      }
      const img = st.grabber.grab()
      if (!img) { st.inflight--; return }
      const buf = img.data.buffer
      // transfer：1280×960 的 RGBA 是 4.9MB，每秒十几次，克隆不可接受。
      post({ width: img.width, height: img.height, buf }, [buf])
    }

    /**
     * 切后台 / 锁屏：**真的停**，不只是停送帧。
     *
     * 改造前只置 `st.paused`：rAF 停了、识别停了，但相机 track 还活着（摄像头硬件与 ISP
     * 继续跑、指示灯亮着），`dom.clip` 也还在解码。用户把手机揣兜里，以为已经离开了。
     *
     * 重开走 `openCam()`（权限已给过，getUserMedia 静默通过，几百毫秒黑屏）。`visSeq`
     * 挡「hidden→visible 快速切两次」：前一次重开还在 await 时后一次已经在停了。
     */
    let visSeq = 0
    const onVis = async () => {
      const seq = ++visSeq
      st.paused = document.hidden
      if (document.hidden) {
        st.pausedClip = !dom.clip.paused
        dom.clip.pause()
        if (st.stream) {
          stopCamera(st.stream)
          st.stream = null
          cancelAnimationFrame(st.raf)
          st.wasCamOn = true
        }
        return
      }
      if (!st.alive || !st.wasCamOn) return
      st.wasCamOn = false
      await openCam()
      if (seq !== visSeq || !st.alive) return
      if (st.pausedClip) dom.clip.play().catch(() => {})
    }
    document.addEventListener('visibilitychange', onVis)

    function teardown() {
      st.alive = false
      // **必须停**：不停的话切页之后那十几 MB 还在下，而用户以为已经离开了。
      st.stopLoad?.()
      // 顶部那条金条是**全局的**（引擎加载时也用它）。不收的话，从扫描页切到别的页
      // 之后它会带着上一段视频的播放进度停在那儿，而那一页跟它毫无关系。
      ctx.progress?.(null, { hide: true })
      cancelAnimationFrame(st.raf)
      document.removeEventListener('visibilitychange', onVis)
      ctx.worker.removeEventListener('message', onWorkerMessage)
      dom.clip.removeEventListener('error', onVideoErr)
      for (const ev of videoEvents) dom.clip.removeEventListener(ev, onVideoEvent)
      dom.clip.removeEventListener('ended', onVideoEnded)
      // 声音按钮的三个同步监听也挂在 dom.clip 上（见 ui.playerControls）。
      ctl.dispose()
      // **相机必须停**：不停的话相机灯一直亮、电量哗哗掉，而用户以为已经离开这一页了。
      if (st.stream) stopCamera(st.stream)
      // ResizeObserver 挂在 canvas 上，canvas 随这一页卸载，但监听器不会自己断——
      // 不 dispose 的话它会一直挂着，等下次进扫描页时 `new Renderer` 又建一个。
      st.renderer?.dispose()
      dom.clip.pause()
      dom.clip.removeAttribute('src')
      dom.clip.load()
      // Worker **留着**（它持有 12MB wasm 实例与解析好的库），只清跟踪状态 ——
      // 否则回来时还锁着上一张照片。
      ctx.worker?.postMessage({ type: 'reset' })
    }

    return teardown
  },
}

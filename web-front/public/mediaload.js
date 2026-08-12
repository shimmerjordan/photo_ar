/**
 * 「认出来了」到「画面动起来」之间那几秒，**一路把阶段报出来**。
 *
 * ## 为什么要单独一个模块
 *
 * 这条链路有四步，而它们分散在三个文件里（`api.mediaOfPhoto`、`prefetch.cachedStream`、
 * `api.playableUrl`、`mp4stream.playStream`）。三个页面各自把它们串一遍的话，
 * 「哪一步慢了」这件事就有三份不一样的答案 —— 而扫描页那一份还压在相机预览上，
 * 最不容易发现它说错了。
 *
 * 所以串联放这里，页面只管**把阶段画到自己合适的位置去**：扫描页画在 HUD 与顶部那条
 * 金条上，试播页与宾客页画在播放器下面。
 *
 * ## 四个阶段，只有一个有真百分比
 *
 * | 阶段 | 说什么 | 有百分比吗 |
 * |---|---|---|
 * | `info` | 正在取视频信息… | 没有（一次 JSON 往返，不定长） |
 * | `ticket` | 准备播放通道… | 没有（换一张票，通常几十毫秒） |
 * | `download` | 3.2 / 8.1 MB | **有** —— 唯一的一个 |
 * | `buffer` | 缓冲首帧… | 没有（解码器说了算） |
 * | `playing` | 播放进度 | 有（`currentTime / duration`） |
 *
 * **命中预取缓存时 `ticket` 整个跳过**，`download` 那一步也快到看不见 —— 那正是登录后
 * 后台预取兑现的时刻，所以它值得一句明确的「本机已有，秒开」，而不是让用户以为
 * "今天网络真好"。
 *
 * ## 为什么不给 `info` 和 `ticket` 编一个假的百分比
 *
 * 编出来的数字会让用户对剩余时间形成预期，而那个预期一定是错的。`ui.js` 的 `loading()`
 * 上写着同一条：「不显示进度百分比 —— 这些请求都没有可靠的总量，编一个数字比不给更糟」。
 * 不定长的阶段让进度条走扫描动画（`#bar.indeterminate`）：它说的是"还在动"，不说"还剩多少"。
 */
import * as api from './api.js'
import { cachedStream } from './prefetch.js'
import { playStream } from './mp4stream.js'

/** 阶段名。页面用它决定画什么，不要去比对文案 —— 文案会改。 */
export const Stage = {
  INFO: 'info',
  TICKET: 'ticket',
  DOWNLOAD: 'download',
  BUFFER: 'buffer',
  PLAYING: 'playing',
  /** 终局：这张照片没配视频 / 文件不在了 / 取不到。`text` 里是能照着做的那句话。 */
  UNAVAILABLE: 'unavailable',
  ERROR: 'error',
}

/** 一句人话 + 一个 0..1 的进度（`null` = 不定长）。纯函数，好测。 */
export function stageText(stage, { loaded = 0, total = 0, fromCache = false } = {}) {
  switch (stage) {
    case Stage.INFO: return '正在取视频信息…'
    case Stage.TICKET: return '准备播放通道…'
    case Stage.DOWNLOAD:
      if (fromCache) return '本机已有，秒开'
      if (!total) return `已收到 ${mb(loaded)} MB`   // 没有分母时报绝对量，不报百分比
      return `${mb(loaded)} / ${mb(total)} MB`
    case Stage.BUFFER: return '缓冲首帧…'
    case Stage.PLAYING: return ''
    default: return ''
  }
}

/**
 * 0..1 的进度，或 `null` 表示"这一步没有可报的总量"。纯函数，好测。
 *
 * `total` 为 0 时返回 null 而**不是** `loaded / 0` —— 后者是 Infinity，进度条会直接
 * 跳满然后停在那儿，看起来像"卡在 100%"。
 */
export function stagePct(stage, { loaded = 0, total = 0 } = {}) {
  if (stage !== Stage.DOWNLOAD) return null
  if (!total || !Number.isFinite(total)) return null
  return Math.min(1, Math.max(0, loaded / total))
}

const mb = (n) => (n / 1048576).toFixed(1)

/**
 * 把一张照片的视频装进 `video`，一路报阶段。
 *
 * @param onStage `({stage, text, pct, info, fromCache, error})` —— 每次状态变化调一次。
 *   `pct` 是 0..1 或 null（不定长）。
 * @param onDiag  可选，把技术细节送进 diag（页面各自决定要不要）
 * @returns 卸载函数。**必须调**：它要停掉还在跑的 fetch 与播放进度监听，否则切页之后
 *   那十几 MB 还在下、而用户以为已经离开了。
 */
export function loadPhotoVideo(video, photoId, { onStage, onDiag } = {}) {
  let alive = true
  let stopStream = null
  let fromCache = false
  /**
   * 已经真的在动了。
   *
   * **这个标记不是可有可无的**：`playStream` 在第一个分片到位时就起播（那是"边下边播"
   * 的全部意义），所以 `playing` 通常在 `done` 之前好几秒就到了。不判的话，等整条流
   * 下完时那一声 `done` 会把界面从「播放进度」打回「缓冲首帧…」—— 画面明明在动，
   * 字却说还在缓冲。
   */
  let playing = false
  /** 元信息一到手就挂在这里，之后每一次回调都带着它 —— 试播页要拿它显示大小与时长。 */
  let mediaInfo = null

  const say = (stage, detail = {}) => {
    if (!alive) return
    onStage?.({
      stage,
      text: detail.text ?? stageText(stage, { ...detail, fromCache }),
      pct: detail.pct !== undefined ? detail.pct : stagePct(stage, detail),
      fromCache,
      ...detail,
      // 放在展开之后并显式兜底：调用方给了就用它的，没给就用手上这一份。
      // 写在展开之前的话，`detail` 里一个 `info: undefined` 就会把它抹掉。
      info: detail.info ?? mediaInfo,
    })
  }

  // ── 播放进度 ──────────────────────────────────────────────────────
  // 首帧可播之后条子不隐藏，接着当播放进度用。视频是循环播的（scan.js 里有手动兜底
  // 循环），所以它会一遍遍走满 —— 那正好是"还在播"的信号。
  const onTimeUpdate = () => {
    if (!alive) return
    const d = video.duration
    if (!Number.isFinite(d) || d <= 0) return
    playing = true
    say(Stage.PLAYING, { pct: Math.min(1, video.currentTime / d), text: '' })
  }
  // 等 `playing` 而不是 `play()` 返回：MSE 那条路上 play() 可能在还没有可解码帧时
  // 就被调用，返回不代表真的在动。
  const onPlaying = () => {
    if (!alive) return
    playing = true
    say(Stage.PLAYING, { pct: 0, text: '' })
  }
  video.addEventListener('timeupdate', onTimeUpdate)
  video.addEventListener('playing', onPlaying)

  ;(async () => {
    say(Stage.INFO)
    let info
    try {
      info = await api.mediaOfPhoto(photoId)
    } catch (e) {
      return say(Stage.ERROR, { text: `取视频信息失败（${e.message}）`, pct: null, error: e })
    }
    if (!alive) return
    mediaInfo = info
    onDiag?.(`媒体信息 via=${info.via} absolute=${info.absolute} range=${info.supportsRange}` +
      ` bytes=${info.bytes} ${info.durationMs}ms missing=${info.missing} integrity=${info.integrity}`)

    if (info.missing) return say(Stage.UNAVAILABLE, { text: '视频文件不在了（服务端报 missing）', pct: null, info })
    if (!info.url) return say(Stage.UNAVAILABLE, { text: '服务端没给出视频地址', pct: null, info })
    if (info.integrity && info.integrity !== 'ok') {
      onDiag?.(`⚠️ integrity=${info.integrity}，视频可能不完整`)
    }
    if (info.absolute) {
      onDiag?.('⚠️ 媒体是绝对地址。跨源会被 COEP 拦，要在部署层代理成同源。')
    }

    // 预取缓存命中就直接从本机播（登录时后台拉的，见 prefetch.js）——
    // 现场网络最差的那一刻，正好是唯一不需要网络的一刻。
    const cached = await cachedStream(info.url)
    if (!alive) return
    fromCache = Boolean(cached)
    let src
    if (cached) {
      onDiag?.('视频从预取缓存播（零网络）')
      src = cached
    } else {
      // 未命中走原来的两层绕路，缺一不可（都是真机上量出来的，见 mp4stream.js 顶部那张表）：
      //   1. `playableUrl` —— 换成自带凭证的票据地址，因为媒体组件拿不到会话 cookie；
      //   2. `playStream`  —— 页面自己 fetch、经 MediaSource 喂，因为那个组件还有
      //      独立的 TLS 栈，不认自签证书。
      say(Stage.TICKET)
      src = await api.playableUrl(info.url)
      if (!alive) return
    }

    say(Stage.DOWNLOAD, { loaded: 0, total: Number(info.bytes) || 0 })
    stopStream = playStream(video, src, {
      onEvent: (name, detail) => {
        if (name === 'progress') {
          // 已经在播了就别再报下载进度：那会把播放进度顶掉，而用户看的是画面。
          if (playing) return
          // 分母优先用流自己报的 Content-Length；它是 0 时退回元信息里的 bytes ——
          // 两个都没有才真的按不定长处理。
          const total = detail.total || Number(info.bytes) || 0
          return say(Stage.DOWNLOAD, { loaded: detail.loaded, total })
        }
        onDiag?.(`流 ${name} ${JSON.stringify(detail ?? {})}`)
        // 下完了但还没起播 —— 只有这一种情况才是"在等第一帧"。已经在播的时候
        // 说「缓冲首帧」会让画面在动而字说还在等（见 `playing` 那个标记的说明）。
        if (name === 'done' && !playing) say(Stage.BUFFER)
      },
      // MSE 走不通退回 <video src> 时，缓存的 Response 给不出地址，现取一张票。
      getFallbackUrl: () => api.playableUrl(info.url),
    })
  })().catch((e) => say(Stage.ERROR, { text: `播放没起来（${e.message}）`, pct: null, error: e }))

  return () => {
    alive = false
    // 两个都要摘。`playing` 那个以前是 `{once: true}` 挂上去就不管了 —— 在扫描页上
    // 那个 `<video>` 是**长驻**的（每次命中换一段视频，元素不换），所以没起播就切走的话
    // 监听器会一直挂着，下一次命中时旧的那个会先开口。
    video.removeEventListener('timeupdate', onTimeUpdate)
    video.removeEventListener('playing', onPlaying)
    stopStream?.()
  }
}

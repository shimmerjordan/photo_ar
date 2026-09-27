/**
 * 「认出来了」到「画面动起来」之间那几秒，**一路把阶段报出来**。
 *
 * ## 为什么要单独一个模块
 *
 * 这条链路有好几步，而它们分散在好几个文件里（`api.mediaOfPhoto`、`prefs.overrideOf`、
 * `mediastore` 的缓存与下载任务、`api.playableUrl`、`mp4stream.playStream`）。三个页面各自
 * 把它们串一遍的话，「哪一步慢了」这件事就有三份不一样的答案 —— 而扫描页那一份还压在
 * 相机预览上，最不容易发现它说错了。
 *
 * 所以串联放这里，页面只管**把阶段画到自己合适的位置去**：扫描页画在 HUD 与顶部那条
 * 金条上，试播页与宾客页画在播放器下面。
 *
 * ## 去哪取（设计 §3.1 的顺序）
 *
 * 1. **单个来源**（设置 → 高级设置里给这张配的直链 / 本机文件，见 prefs.js）；
 * 2. **本机缓存**（`photoar-media-v1`：预取、手动缓存、上一次看的时候下的）—— 零网络；
 * 3. **进行中的下载任务**（预取或「缓存」按钮正在下这一段）—— 挂上去，已收的先重放，
 *    同一段不下第二遍；
 * 4. **新开一个下载任务**（`Priority.PLAY`）—— 边下边播，下完落缓存，于是「看过的」
 *    就是「存下的」。
 *
 * 例外两条走老的直连（票据 + `playStream(地址)`，不经下载任务）：`info.absolute`（网盘直链，
 * 十几分钟就失效，不值得缓存）；浏览器没有 MediaSource 且本机没有（iPhone Safari —— 那条路
 * 上 `<video src>` 自己会下，再开一个任务就是同一段下两遍）。
 *
 * ## 阶段，只有下载那一档有真百分比
 *
 * | 阶段 | 说什么 | 有百分比吗 |
 * |---|---|---|
 * | `info` | 正在取视频信息… | 没有（一次 JSON 往返，不定长） |
 * | `ticket` | 准备播放通道… | 没有（只在直连那两条例外路上有：换一张票，通常几十毫秒） |
 * | `download` | 3.2 / 8.1 MB | **有** —— 唯一的一个 |
 * | `buffer` | 缓冲首帧… | 没有（解码器说了算） |
 * | `playing` | 播放进度 | 有（`currentTime / duration`；边下边播时 duration 还是 Infinity，用元信息的时长） |
 *
 * **命中本机缓存时 `download` 那一步快到看不见** —— 那正是预取 / 手动缓存兑现的时刻，
 * 所以它值得一句明确的「本机已有，秒开」，而不是让用户以为"今天网络真好"。
 *
 * ## 每个事件都带着的两样：`dl` 与 `autoplay`
 *
 * - `dl`：**下载**的进度，与播放进度是两回事 —— `null`（还不知道 / 这条路不归我们管，
 *   比如直连那两条例外）或 `{loaded, total, done, fromCache, source, cacheFailed}`。
 *   `source` 是 `'lan' | 'origin' | 'url'`（下载任务当前用的来源）或 `'cache' | 'file'`
 *   （本机已有）；`total` 为 0 = 不知道总长；`cacheFailed` 是「下完了但没存进本机」的原因。
 *   **起播之后照样更新**：下载进度每变一次就推一次 `playing`（`pct` 仍是播放进度），
 *   下完再推一次带 `dl.done` 的。原来这里是 `if (playing) return` —— 一起播就不再报下载，
 *   进度条改当播放进度，于是「边下边播时没有下载进度」（2026-09-26 用户原话）。
 *   页面拿 `dlText(dl)` 得到那一行字。
 * - `autoplay`：首次起播的结果（`playback.startPlayback` 的返回值），`null` = 还没出来。
 *   页面据此决定「开声音」要不要金框（被拦退静音了就要）。**第一条 `playing` 保证已经
 *   带着它**：`playing` 事件比 play() 的 promise 早一拍，结果没出来之前先压着不报。
 *
 * ## 为什么不给 `info` 和 `ticket` 编一个假的百分比
 *
 * 编出来的数字会让用户对剩余时间形成预期，而那个预期一定是错的。`ui.js` 的 `loading()`
 * 上写着同一条：「不显示进度百分比 —— 这些请求都没有可靠的总量，编一个数字比不给更糟」。
 * 不定长的阶段让进度条走扫描动画（`#bar.indeterminate`）：它说的是"还在动"，不说"还剩多少"。
 */
import * as api from './api.js'
import {
  OVERRIDE_CACHE, Priority, VIDEO_CACHE, cached, download, jobFor, overrideKey, videoKey,
} from './mediastore.js'
import { canStream, playStream } from './mp4stream.js'
import { overrideSources } from './netsrc.js'
import { overrideOf } from './prefs.js'
import { mb } from './ui.js'

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

/**
 * 这一步**在干什么**。与 `stageText` 分工，而这条分工是三个页面都踩过的一个坑：
 *
 * `stageText` 报的是这一步的**细节**，而下载那一档的细节是一串数字（`3.2 / 8.1 MB`）——
 * 单独显示时它不说明自己是在下载（宾客页那一行就只剩了「3.2 / 8.1 MB」），而与阶段名
 * 并排显示时又会重复一遍（试播页的 `stageLine` 与 `detail` 曾经是同一句）。
 *
 * 所以名字与数字分成两个函数：一行里要一句完整的话就 `阶段名 + 数字`，
 * 两处分开显示就一处放名字、另一处只放数字（下载档才有）。
 */
export const stageName = (stage, { fromCache = false, via = null } = {}) => {
  // 缓存命中时不能还叫「正在下载视频」——那句话与 `stageText` 同一档给出的
  // 「本机已有，秒开」自相矛盾（一句说在下，一句说已经有了）。见调用点的三处拼接。
  if (stage === Stage.DOWNLOAD && fromCache) return '正在从本机取视频'
  // 经整站数据源（局域网）下的要说出来：配了数据源的人想知道它到底生效没有，
  // 而「快」本身说明不了是哪条路。`via` 就是事件上的 `via`（下载任务当前的来源）。
  if (stage === Stage.DOWNLOAD && via === 'lan') return '正在经局域网下载视频'
  return ({
    [Stage.INFO]: '正在取视频信息',
    [Stage.TICKET]: '准备播放通道',
    [Stage.DOWNLOAD]: '正在下载视频',
    [Stage.BUFFER]: '正在缓冲首帧',
  }[stage] ?? '')
}

/**
 * 这一步的**细节**（下载档是数字，其余档回退成阶段名那句话）+ 一个 0..1 的进度
 * （`null` = 不定长）。纯函数，好测。跟阶段名并排显示时只取下载那一档，见 `stageName`。
 */
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

/**
 * 下载那一行（`dl`，见文件头）说什么。纯函数，好测。与阶段名 / `stageText` 分开：
 * 起播之后阶段行说「正在播放」，**这一行接着说下载** —— 两件事同时在发生，挤在一行里
 * 必然有一件被顶掉（原来被顶掉的就是下载）。
 *
 * 「经局域网」只在**还在下**的时候说：它解释的是速度，下完了走哪条路就不重要了。
 */
export function dlText(dl) {
  if (!dl) return ''
  if (dl.fromCache) return '本机已有'
  if (dl.done && dl.cacheFailed) return `已下完，但没存进本机（${dl.cacheFailed}）`
  if (dl.done) return '已存到本机，下次秒开'
  const n = dl.total ? `已下载 ${mb(dl.loaded)} / ${mb(dl.total)} MB` : `已下载 ${mb(dl.loaded)} MB`
  return dl.source === 'lan' ? `经局域网 · ${n}` : n
}

/**
 * 会话内的媒体元信息缓存。key = photoId。
 *
 * `mediaOfPhoto` 在扫描页、试播页、预取、下载四处各自被调一遍 —— 同一张照片在
 * 一次会话里的元信息不会变（除非管理端换了视频），四次网络往返换来的是同一份 JSON。
 * 换视频后由 `forgetMedia(photoId)` 失效（管理动作那边调，见 shell.js 的
 * `libraryChanged`）。
 *
 * @param fetcher 可选，默认 `api.mediaOfPhoto`。留这个口子是为了测试注入 ——
 *   这个模块不引入任何 mock 框架，纯靠参数换掉真实网络调用。
 */
const _media = new Map()
export async function mediaInfo(photoId, fetcher = api.mediaOfPhoto) {
  if (_media.has(photoId)) return _media.get(photoId)
  const info = await fetcher(photoId)
  _media.set(photoId, info)
  return info
}

/** 清掉缓存。不给 `photoId` 就清空全部（库变了，任何一张的元信息都可能已经不对）。 */
export function forgetMedia(photoId) {
  photoId ? _media.delete(photoId) : _media.clear()
}

/** 单个来源选的是本机文件、但那份字节已经不在浏览器存储里了（被清了 / 换了浏览器）。 */
const NO_OVERRIDE_FILE = '你设的本机文件不在浏览器存储里了，去设置 → 高级设置重新选一次'

/**
 * 单个来源的直链**真的下不下来**时的提示：CORS 是最常见的原因。导出给 `download.js`
 * 的「存到手机」共用 —— 同一件事不能有两份措辞不一样的文案（fix1 #2）。
 *
 * 注意与下面 `follow` 里 `aborted` 那一档的区别：那是任务被取消，不是下不下来，
 * 用不上这句话（fix1 #3）。
 */
export function overrideUrlFailText(error) {
  return `单个来源的直链下不下来（${error ?? '下载失败'}）。对方服务器要允许跨域（CORS），或者改成本机文件`
}

/** 设置页存下来的单个来源是不是一条能用的记录（坏数据当没设过，走服务端那段）。 */
const usableOverride = (ov) => ov?.kind === 'file' || (ov?.kind === 'url' && Boolean(ov.url))

/**
 * 把一张照片的视频装进 `video`，一路报阶段。
 *
 * @param onStage `({stage, text, pct, info, fromCache, via, dl, autoplay, error})` —— 每次状态
 *   变化调一次。`pct` 是 0..1 或 null（不定长）。`dl` / `autoplay` 见文件头；`via` 只在
 *   下载档有（下载任务当前的来源，给 `stageName` 用）。
 * @param onDiag  可选，把技术细节送进 diag（页面各自决定要不要）
 * @param fetcher 可选，透传给 `mediaInfo`（见那个函数的说明）。默认走真实网络；
 *   测试注入一个假函数，不必打真请求也不必 mock `api.js` 模块。
 * @param autoplay 默认 true：第一次有数据可播时起播一次（有声优先，被拦退静音，见 playback.js）。
 * @returns 卸载函数。**必须调**：它要退订下载任务、停掉喂流与播放进度监听、回收 `blob:` 地址。
 *   **它不取消下载任务**：任务归 mediastore，预取 / 「缓存」按钮 / 存到手机可能也挂在上面；
 *   没人要了它也会下完落进本机缓存（「自动缓存」），下次扫到秒开 —— 这正是「看过的下次
 *   还要重下」那条根因的反面。
 */
export function loadPhotoVideo(video, photoId, { onStage, onDiag, fetcher, autoplay = true } = {}) {
  let alive = true
  let stopStream = null
  let unsubJob = null
  let fromCache = false
  /**
   * 已经真的在动了。
   *
   * **这个标记不是可有可无的**：`playStream` 在第一个分片到位时就起播（那是"边下边播"
   * 的全部意义），所以 `playing` 通常在 `done` 之前好几秒就到了。不判的话，等整条流
   * 下完时那一声 `done` 会把界面从「播放进度」打回「缓冲首帧…」—— 画面明明在动，
   * 字却说还在缓冲。它**不再**挡下载进度：起播之后下载进度改成随 `playing` 一起报（见文件头）。
   */
  let playing = false
  /**
   * 元信息一到手就挂在这里，之后每一次回调都带着它 —— 试播页要拿它显示大小与时长。
   *
   * ⚠️ **不要叫它 `mediaInfo`**：模块级也导出了一个同名函数（会话缓存那个）。这里曾经
   * 就叫这个名字，函数体内 `mediaInfo(photoId)` 那一句解析到的是**这个局部变量**
   * （此时还是 `null`），而不是外层那个函数 —— 结果是每次都抛
   * `TypeError: mediaInfo is not a function`，被下面的 try/catch 吞成一句
   * 「取视频信息失败」，三个页面的视频全加不出来。
   */
  let curInfo = null
  /** 下载进度，见文件头的 `dl`。每次变化换一个新对象（页面可能拿它跟上一份比）。 */
  let curDl = null
  /** 首次起播的结果，见文件头的 `autoplay`。 */
  let curAutoplay = null
  /**
   * 起播结果出来了没有（不自动起播就当已经出来了）。出来之前 `playing` 先压着不报：
   * `playing` 事件与 play() 的 promise 是同一个任务里先后发生的，事件在前 —— 不压的话
   * 第一条 PLAYING 的 `autoplay` 永远是 null，页面在「首次到 PLAYING」那一刻就不知道
   * 声音被拦了没有。被打断（`aborted`）也算出来了：否则用户在起播前按了一下暂停，
   * 之后的 PLAYING 就永远压着。
   */
  let kickSettled = !autoplay
  /**
   * `playStream` 已经接管 `video` 了。之前来的 `timeupdate` / `playing` 是**上一段**视频的：
   * 扫描页的 `<video>` 是长驻的，换一张照片时旧的那段在取元信息那几百毫秒里还在播，
   * 不挡的话新的这一段一开口就是「正在播放」，而且下载进度从此被当成播放中报。
   */
  let armed = false
  /** 播放进度的最近一个有效值。`duration` 还不是有限数时（MSE 早期）沿用它，不来回跳。 */
  let lastPlayPct = 0
  /** 说过终局（UNAVAILABLE / ERROR）就闭嘴：之后迟到的进度 / 退回事件不能把它盖掉。 */
  let over = false
  const blobUrls = []

  const say = (stage, detail = {}) => {
    if (!alive || over) return
    if (stage === Stage.UNAVAILABLE || stage === Stage.ERROR) over = true
    onStage?.({
      stage,
      text: detail.text ?? stageText(stage, { ...detail, fromCache }),
      pct: detail.pct !== undefined ? detail.pct : stagePct(stage, detail),
      fromCache,
      ...detail,
      // 放在展开之后并显式兜底：调用方给了就用它的，没给就用手上这一份。
      // 写在展开之前的话，`detail` 里一个 `info: undefined` 就会把它抹掉。
      info: detail.info ?? curInfo,
      dl: curDl,
      autoplay: curAutoplay,
    })
  }

  // ── 播放进度 ──────────────────────────────────────────────────────
  // 首帧可播之后条子不隐藏，接着当播放进度用。视频是循环播的（scan.js 里有手动兜底
  // 循环），所以它会一遍遍走满 —— 那正好是"还在播"的信号。
  //
  // 分母优先用元素自己的 `duration`；它还不是有限数时退回元信息的 `durationMs`。服务端出的是
  // 分片 MP4（empty_moov），边下边喂 MSE 时 `duration` 在 endOfStream 之前**一直是 Infinity**
  // —— 只认它的话，整段下载期间播放进度都是 0：扫描页金条的亮层（播放）不动、暗层（已下载）
  // 在涨，看起来像没在播（Task 7 截图时量到的）。
  const realDuration = () => {
    const d = video.duration
    return Number.isFinite(d) && d > 0 ? d : null
  }
  const playPct = () => {
    const ms = Number(curInfo?.durationMs)
    const d = realDuration() ?? (ms > 0 ? ms / 1000 : null)
    if (d) lastPlayPct = Math.min(1, video.currentTime / d)
    return lastPlayPct
  }
  const sayPlaying = () => say(Stage.PLAYING, { pct: playPct(), text: '' })
  const reportPlaying = () => {
    playing = true
    if (kickSettled) sayPlaying()
  }
  const onTimeUpdate = () => {
    if (!alive || !armed) return
    // 起播之前**只认真的时长**：换源时元素重置 currentTime 也会发一条 timeupdate（那时还没有
    // 可播的帧），拿元信息的时长放行的话，这一条就被当成"已经在播"了 —— 下载阶段从此不再报。
    // 起播之后（`playing` 事件已经来过）就不挡了：边下边播时靠它让播放进度走起来。
    if (!playing && !realDuration()) return
    reportPlaying()
  }
  // 等 `playing` 而不是 `play()` 返回：MSE 那条路上 play() 可能在还没有可解码帧时
  // 就被调用，返回不代表真的在动。
  const onPlaying = () => {
    if (!alive || !armed) return
    reportPlaying()
  }
  video.addEventListener('timeupdate', onTimeUpdate)
  video.addEventListener('playing', onPlaying)

  /**
   * 交给 `playStream`。`jobMode` = 字节来自下载任务：那时它自己的 `progress` 不算数 ——
   * 它量的是「喂进解码器多少」，而下载任务的进度才是真下载（半路才挂上来的播放器，
   * 重放那一段会在几毫秒里从 0 冲到已收，那不是网速）。
   */
  const stream = (src, { getFallbackUrl, jobMode = false } = {}) => {
    armed = true
    stopStream = playStream(video, src, {
      autoplay,
      getFallbackUrl,
      onEvent: (name, detail) => {
        if (name === 'progress') {
          // 不归下载任务管的那几条路（本机已有 / 直连）：起播之前报喂流进度，起播之后
          // 那份进度没有意义（本机的早就全在，直连的不落缓存、也不显示下载行）。
          if (jobMode || playing) return
          // 分母优先用流自己报的 Content-Length；它是 0 时退回元信息里的 bytes ——
          // 两个都没有才真的按不定长处理。
          const total = detail.total || Number(curInfo?.bytes) || 0
          return say(Stage.DOWNLOAD, { loaded: detail.loaded, total })
        }
        onDiag?.(`流 ${name} ${JSON.stringify(detail ?? {})}`)
        if (name === 'autoplay') {
          kickSettled = true
          if (!detail?.aborted) curAutoplay = detail
          if (playing) sayPlaying()
          return
        }
        // 下完了但还没起播 —— 只有这一种情况才是"在等第一帧"。已经在播的时候
        // 说「缓冲首帧」会让画面在动而字说还在等（见 `playing` 那个标记的说明）。
        // 退回 `<video src>` 同理：之后下载与缓冲都是浏览器自己的事，停在一串不再动的
        // 数字上不如直说在缓冲。
        if ((name === 'done' || name === 'fallback') && !playing) say(Stage.BUFFER)
      },
    })
  }

  /** 本机已有的一整段（缓存命中 / 单个来源的本机文件）。 */
  const playLocal = (res, { source, sizeHint = 0, getFallbackUrl }) => {
    fromCache = true
    const size = Number(res.headers.get('content-length')) || sizeHint || 0
    curDl = { loaded: size, total: size, done: true, fromCache: true, source, cacheFailed: null }
    say(Stage.DOWNLOAD, { loaded: 0, total: size })
    stream(res, { getFallbackUrl })
  }

  /**
   * 挂到下载任务上：它的每一次进度都是一个事件（见文件头的 `dl`）。
   *
   * @param sizeHint 任务还没拿到响应头时的分母（服务端元信息里的 bytes；单个来源没有）
   * @param onFail 任务最终失败时要说的话；不给 = 交给 `playStream` 的退路，这里只清掉 `dl`
   */
  const follow = (job, { sizeHint = 0, onFail } = {}) => {
    const onSnap = (snap) => {
      if (!alive) return
      if (snap.state === 'error' || snap.state === 'aborted') {
        onDiag?.(`下载任务 ${snap.state}（${snap.error ?? ''}）`)
        curDl = null      // 之后播的（如果还能播）不再是这个任务的字节，没有下载进度可报
        // 任务被取消（用户在别处移除了这条来源 / 清空了缓存，见 Task 8）不是「真的下不下来」——
        // `onFail` 那句 CORS 提示是说给下载失败听的，安在这里文不对题：用户什么都没做错，
        // 只是画面会停在这儿，得让他知道为什么（fix1 #3，评审：「静默结束之外的那一句更好」）。
        if (snap.state === 'aborted') {
          return say(Stage.ERROR, { text: '这段视频的来源刚被移除，或者缓存被清空了', pct: null })
        }
        if (onFail) return onFail(snap)
        if (playing && kickSettled) sayPlaying()
        return
      }
      curDl = {
        loaded: snap.loaded,
        total: snap.total || sizeHint || 0,
        done: snap.state === 'done',
        fromCache: false,
        source: snap.source,
        cacheFailed: snap.cacheFailed ?? null,
      }
      if (playing) {
        if (kickSettled) sayPlaying()
        return
      }
      // 下完了还没起播：那就是在等第一帧（与 playStream 的 `done` 同一个意思）。
      if (curDl.done) return say(Stage.BUFFER)
      say(Stage.DOWNLOAD, { loaded: curDl.loaded, total: curDl.total, via: snap.source })
    }
    unsubJob = job.subscribe(onSnap)
    onSnap(job.snap())      // subscribe 不推当前状态：半路挂上来的要先知道已经收了多少
  }

  /**
   * 单个来源 / 本机文件那几条路的退路：整段读成 `blob:` 给 `<video src>`（普通 MP4 喂不进
   * MSE，见设计 §2.3）。地址记下来，卸载时统一 revoke —— 不 revoke 的话那十几 MB 一直挂在文档上。
   */
  const blobUrlOf = async (getRes) => {
    const r = await getRes()
    if (!r) return null
    const u = URL.createObjectURL(await r.blob())
    if (!alive) {
      URL.revokeObjectURL(u)
      return null
    }
    blobUrls.push(u)
    return u
  }

  /** 老的直连：换票 → `playStream(地址)`。不经下载任务、不落缓存、没有 `dl`。 */
  const playDirect = async (url) => {
    // 两层绕路缺一不可（都是真机上量出来的，见 mp4stream.js 顶部那张表）：
    //   1. `playableUrl` —— 换成自带凭证的票据地址，因为媒体组件拿不到会话 cookie；
    //   2. `playStream`  —— 页面自己 fetch、经 MediaSource 喂，因为那个组件还有
    //      独立的 TLS 栈，不认自签证书。
    say(Stage.TICKET)
    const src = await api.playableUrl(url)
    if (!alive) return
    say(Stage.DOWNLOAD, { loaded: 0, total: Number(curInfo?.bytes) || 0 })
    stream(src, { getFallbackUrl: () => api.playableUrl(url) })
  }

  const playOverride = async (ov) => {
    onDiag?.(`这张设了单个来源（${ov.kind === 'file' ? `本机文件 ${ov.name ?? ''}` : `直链 ${ov.url}`}）`)
    if (ov.kind === 'file') {
      const getRes = () => cached(overrideKey(photoId), OVERRIDE_CACHE)
      const res = await getRes()
      if (!alive) return
      // 不悄悄换回服务端那段：用户明确说了「这张用我手机里这个」，换了他也不知道为什么
      // 画面不一样；告诉他去哪重选，比猜他的意思好。
      if (!res) return say(Stage.UNAVAILABLE, { text: NO_OVERRIDE_FILE, pct: null })
      return playLocal(res, { source: 'file', sizeHint: Number(ov.size), getFallbackUrl: () => blobUrlOf(getRes) })
    }
    // 直链：下下来的字节以**直链地址本身**为键存进 photoar-override-v1 —— 换了地址自然是
    // 另一条，不会播到旧地址那一份。
    const key = ov.url
    const getRes = () => cached(key, OVERRIDE_CACHE)
    const hit = await getRes()
    if (!alive) return
    if (hit) return playLocal(hit, { source: 'cache', getFallbackUrl: () => blobUrlOf(getRes) })
    const job = download(key, { cacheName: OVERRIDE_CACHE, sources: overrideSources(ov), priority: Priority.PLAY })
    follow(job, {
      // 直链最常见的失败是对方不允许跨域 —— 那不是等一会儿会好的事，得告诉用户怎么办。
      onFail: (snap) => say(Stage.ERROR, { text: overrideUrlFailText(snap.error), pct: null }),
    })
    // 退路等整段下完再给 blob：普通 MP4 喂不进 MSE，而跨源地址直接交给 `<video src>`
    // 又会被 COEP 拦、画进 WebGL 还会污染画布。
    stream(job.response(), { jobMode: true, getFallbackUrl: () => blobUrlOf(async () => job.response()) })
  }

  ;(async () => {
    say(Stage.INFO)
    const ov = overrideOf(photoId)
    const useOverride = usableOverride(ov)
    let info = null
    try {
      // `fetcher` 未给（正常调用方）时是 `undefined`，`mediaInfo` 自己的默认参数
      // （`= api.mediaOfPhoto`）接管 —— 不需要在这里再判一次。
      info = await mediaInfo(photoId, fetcher)
    } catch (e) {
      if (!useOverride) return say(Stage.ERROR, { text: `取视频信息失败（${e.message}）`, pct: null, error: e })
      // 设了单个来源的，服务端那份元信息只是给试播页显示用的：取不到照样播本机那份。
      onDiag?.(`取视频信息失败（${e.message}），但这张设了单个来源，照播`)
    }
    if (!alive) return
    curInfo = info
    if (info) {
      onDiag?.(`媒体信息 via=${info.via} absolute=${info.absolute} range=${info.supportsRange}` +
        ` bytes=${info.bytes} ${info.durationMs}ms missing=${info.missing} integrity=${info.integrity}`)
    }
    if (useOverride) return playOverride(ov)

    if (info.missing) return say(Stage.UNAVAILABLE, { text: '视频文件不在了（服务端报 missing）', pct: null, info })
    if (!info.url) return say(Stage.UNAVAILABLE, { text: '服务端没给出视频地址', pct: null, info })
    if (info.integrity && info.integrity !== 'ok') {
      onDiag?.(`⚠️ integrity=${info.integrity}，视频可能不完整`)
    }
    if (info.absolute) {
      onDiag?.('⚠️ 媒体是绝对地址。跨源会被 COEP 拦，要在部署层代理成同源。')
      return playDirect(info.url)
    }

    // 缓存键与预取 / 「缓存」按钮同一个口径（mediastore 的 videoKey）。nas_serve 时
    // `info.url` 本来就等于它；老服务端没给 assetId 就退回 `info.url`。
    const key = info.assetId ? videoKey(info.assetId) : info.url
    const toServer = () => api.playableUrl(info.url)
    const hit = await cached(key, VIDEO_CACHE)
    if (!alive) return
    if (hit) {
      // 现场网络最差的那一刻，正好是唯一不需要网络的一刻。
      onDiag?.('视频从本机缓存播（零网络）')
      return playLocal(hit, {
        source: 'cache',
        sizeHint: Number(info.bytes),
        // 没有 MSE（iPhone Safari）时退路是本机那份的 blob: —— 否则本机缓存在那种浏览器上
        // 永远用不上；有 MSE 却喂不进去，多半是这份字节本身有问题，换服务端那份（过票据）。
        getFallbackUrl: () => (canStream() ? toServer() : blobUrlOf(() => cached(key, VIDEO_CACHE))),
      })
    }
    if (!canStream()) {
      onDiag?.('这个浏览器没有 MediaSource：交给 <video src> 直连（不开下载任务，免得同一段下两遍）')
      return playDirect(info.url)
    }
    const attached = Boolean(jobFor(key))
    const job = download(key, { priority: Priority.PLAY })
    onDiag?.(attached ? `挂到进行中的下载任务上（已收 ${mb(job.loaded)}MB）` : '新开下载任务（下完落本机缓存）')
    follow(job, { sizeHint: Number(info.bytes) || 0 })
    // 退路：MSE 喂不进去 / 下载任务最终失败 → 票据地址交给 `<video src>`（媒体组件拿不到
    // cookie，服务端地址一律过 playableUrl）。
    stream(job.response(), { jobMode: true, getFallbackUrl: toServer })
  })().catch((e) => say(Stage.ERROR, { text: `播放没起来（${e.message}）`, pct: null, error: e }))

  return () => {
    alive = false
    // 两个都要摘。`playing` 那个以前是 `{once: true}` 挂上去就不管了 —— 在扫描页上
    // 那个 `<video>` 是**长驻**的（每次命中换一段视频，元素不换），所以没起播就切走的话
    // 监听器会一直挂着，下一次命中时旧的那个会先开口。
    video.removeEventListener('timeupdate', onTimeUpdate)
    video.removeEventListener('playing', onPlaying)
    unsubJob?.()
    stopStream?.()
    for (const u of blobUrls) URL.revokeObjectURL(u)
  }
}

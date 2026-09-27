/**
 * 起播（有声优先，被拦退静音）与"第一次手势自动开声"。
 *
 * ## 为什么起播不是简单的 `video.play()`
 *
 * 浏览器只保证**静音**自动播放；不静音调 `play()` 在用户没有跟页面互动过时会被
 * 拒绝（`NotAllowedError`），报错本身不是 bug —— 但对着一个刚好没抛错的 `play()`
 * 调用点，直接 `await` 会在多数场景下悄悄播不出声还不报错，页面完全看不出发生了
 * 什么。设计 §2.4 定的规矩是：先按偏好尝试有声，被拦就退静音重试，并把这件事
 * （`blocked:true`）交回调用方 —— 调用方（mp4stream.js / mediaload.js）据此在
 * UI 上把"开声音"按钮点亮成需要用户去点一下的样子。
 *
 * `video.dataset.soundBlocked = '1'` 是留给 `armSoundUnlock` 的信号：这段视频
 * 正等着一次用户手势来解锁声音。清掉这个标记（起播成功、或用户手动开了声音）
 * 是调用方或 `armSoundUnlock` 自己的事，`clearSoundBlocked` 只是把这个动作
 * 收成一个函数，不必到处重复同一行 `delete`。
 *
 * ## 为什么要分辨 `AbortError`
 *
 * 边下边播换源（默认源 ↔ 数据源熔断切换）会重新指定 `video.src`，进行中的
 * `play()` 承诺随之被浏览器判定失败、错误是 `AbortError`。这**不是**"播放失败"，
 * 是"这次调用作废了，别处正在处理"——算作失败会在换源的一瞬间给用户看一次
 * 毫无意义的错误提示。调用方看到 `aborted:true` 应该什么都不做。
 */
import { soundOn } from './prefs.js'

export function clearSoundBlocked(video) {
  delete video.dataset.soundBlocked
}

/**
 * @param video 一个 `<video>`（或测试里的假件：有 `play()`、`muted`、`dataset`）
 * @param sound 是否尝试有声播放，默认取当前偏好
 * @returns `{muted, blocked}` 正常播出；`{failed:true}` 两次都失败（非用户手势导致）；
 *          `{aborted:true}` 这次调用被换源等操作打断，不算失败
 */
export async function startPlayback(video, { sound = soundOn() } = {}) {
  video.muted = !sound
  try {
    await video.play()
    return { muted: video.muted, blocked: false }
  } catch (err) {
    if (err?.name === 'AbortError') return { aborted: true }
    if (!sound) {
      // 已经是静音播放还被拦，不是"需要用户手势"能解决的（比如根本没有可播的源）。
      return { failed: true }
    }
    // 有声被拦：这是预期内的浏览器策略，退静音重试。
    video.muted = true
    try {
      await video.play()
      video.dataset.soundBlocked = '1'
      return { muted: true, blocked: true }
    } catch (err2) {
      if (err2?.name === 'AbortError') return { aborted: true }
      return { failed: true }
    }
  }
}

/**
 * 挂一次性的"第一次手势自动开声"监听。
 *
 * ## 为什么听 `pointerup` 而不是 `pointerdown`
 *
 * 开声（`muted = false`）要在**用户激活**里做才算数。按 HTML 的用户激活规则，触屏上的
 * `pointerdown` **不是**激活事件 —— 要到 `pointerup`（非鼠标）/ `touchend` / `click` /
 * `keydown` 才算（鼠标是 `pointerdown` 就算，到它的 `pointerup` 时激活早已在了）。在没有
 * 激活时给一段静音自动播放的视频开声，Chrome 可能直接把视频暂停、或开声无效 —— 而手机正是
 * 主场景。顺带的好处：滑动页面时浏览器发的是 `pointercancel` 不是 `pointerup`，滚一下页面
 * 不会白白用掉这一次。
 *
 * ## 为什么在 capture 阶段，以及点在声音按钮上为什么**不摘**
 *
 * 要**先于**目标元素自己的 click 处理跑（声音按钮自己的开关逻辑）—— 这次手势恰好点在声音
 * 按钮（`[data-role="sound"]`）上时，由按钮自己决定开或关，这里绝不能抢先把它开了，否则按钮
 * 再把它关掉，用户看到的是"点了开声音，反而没声音"。这一下**不消耗**监听：宾客页 / 试播页的
 * `<video>` 整个标着 `data-role="sound"`（原生静音键在它的 shadow DOM 里），点一下视频暂停
 * 就把监听摘了的话，页面上那句「或点一下页面别处，就有声音」之后就成了空话。
 *
 * ## 什么时候摘
 *
 * - 手势落在别处：不管这一下有没有真的开成声（偏好可能刚被关了），它的职责只是"逮住第一次
 *   手势"，不是"每次手势都看看"。
 * - 声音已经开了（`volumechange` 且 `!muted`）：不管是谁开的 —— 声音按钮、原生喇叭。不摘的话
 *   用户之后自己按静音，下一次点页面别处又会被改回有声。只看**此刻**的 `muted` 而不是"来了
 *   一条 volumechange"：起播时 `startPlayback` 那两下 muted 切换的事件会迟到（它们是任务，
 *   起播结果是微任务），那时视频仍是静音的，不能当成"已经开声"。
 *
 * 页面不直接调这个 —— 用 `soundUnlocker`（什么时候挂由它定）。
 */
export function armSoundUnlock(video, { root = globalThis.document } = {}) {
  const off = () => {
    root.removeEventListener('pointerup', onGesture, { capture: true })
    root.removeEventListener('keydown', onGesture, { capture: true })
    video.removeEventListener('volumechange', onVolume)
  }
  function onGesture(ev) {
    if (ev.target?.closest?.('[data-role="sound"]')) return
    off()
    if (video.dataset.soundBlocked === '1' && soundOn()) {
      video.muted = false
      clearSoundBlocked(video)
    }
  }
  function onVolume() {
    if (video.muted) return
    off()
    clearSoundBlocked(video)
  }
  root.addEventListener('pointerup', onGesture, { capture: true })
  root.addEventListener('keydown', onGesture, { capture: true })
  video.addEventListener('volumechange', onVolume)
  return off
}

/**
 * 页面用的挂法（扫描 / 宾客 / 试播三页同一套）：**起播结果是「被拦、退成静音」时才挂**。
 *
 * 原来三页在建 `<video>` / 进页面时就挂上：取信息、下前几块那几秒里的任何一次触摸都会把这个
 * 一次性监听用掉，等到真被拦、页面说「点一下页面别处就有声音」时，点别处已经没反应了。
 *
 * 用法：每条 PLAYING 都 `update(s.autoplay)`（mediaload 的 `autoplay` 字段，每次起播换一个新
 * 对象）—— 同一份结果只处理一次，所以不会每条进度都重挂；**新的**被拦结果（换源重起播）重挂
 * 一次。换片 / 卸载时 `reset()`。
 */
export function soundUnlocker(video, opts) {
  let off = null
  let seen = null
  const reset = () => {
    off?.()
    off = null
    seen = null
  }
  return {
    update(autoplay) {
      if (!autoplay || autoplay === seen) return
      off?.()
      off = null
      seen = autoplay
      if (autoplay.blocked) off = armSoundUnlock(video, opts)
    },
    reset,
  }
}

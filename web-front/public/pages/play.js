/**
 * 试播：不开相机，全屏放这张照片配的那段视频。**Android `PlayScreen` 的对译。**
 *
 * ## 它不是「AR 的简化版」
 *
 * 两件事的用途不同：AR 要回答「贴得准不准」，试播回答的是「这张照片配的是不是那段
 * 视频」—— 后者在入库之后立刻就想确认，而那时人还在电脑前，手里没有打印件可扫。
 *
 * ## 这一页顺带验证了媒体那两步链路
 *
 * 装载走的是与扫描页**同一个** `mediaload.loadPhotoVideo`，所以它能在不举照片的情况下
 * 把那条链路（元信息 → 票据 → 边下边喂 MediaSource）整条验一遍 —— 包括那几个阶段
 * 报得对不对。链路本身为什么要绕成这样，写在 `mp4stream.js` 顶部那张表里。
 */
import { MEDIA_ERR, NETWORK_STATE, READY_STATE } from '../diag.js'
import { Stage, dlText, loadPhotoVideo, stageName } from '../mediaload.js'
import { soundUnlocker } from '../playback.js'
import { SOUND_BLOCKED_HINT, bytes, duration, failed, h, row, section, setBar, setDownloadRow } from '../ui.js'

export default {
  title: '试播',

  async mount(el, ctx) {
    let alive = true
    let stopLoad = null
    if (!ctx.params.id) {
      el.appendChild(failed('缺少照片 id', () => ctx.shell.pop()))
      return () => {}
    }

    // controls 交给浏览器：自绘播放条要处理拖动、缓冲区间、全屏、画中画 ——
    // 而原生控件在每个平台上都已经对了，且带无障碍。
    // 默认有声，由 playback.startPlayback 决定、被拦时退静音。有原生控件，所以不另做「开声音」
    // 按钮；`data-role="sound"` 的理由见 view.js 同一处（原生静音键在 shadow DOM 里）。
    const video = h('video', { class: 'player', controls: true, playsinline: true, 'data-role': 'sound' })
    // 「第一次手势自动开声」被拦成静音时才挂（PLAYING 里 `unlock.update`），理由同 view.js。
    const unlock = soundUnlocker(video)

    // 阶段行 + 进度条。**`.bar2` 而不是顶部那条 `#bar`**：这一页不是全屏，
    // 内容在木牌里，进度条跟着它走才不会看起来像页面级的加载。
    // 下面再一组下载行 + 下载条：起播之后阶段行说播放、这一组接着说下载（设计 §0 需求 3）。
    const stageLine = h('p', { class: 'p', text: '正在取视频信息…' })
    const bar = h('div', { class: 'bar2' }, h('i'))
    const detail = h('p', { class: 'p dim mono' })
    const dlLine = h('p', { class: 'p mono', hidden: true })
    const dlBar = h('div', { class: 'bar2', hidden: true }, h('i'))
    const errBox = h('p', { class: 'warnbox', hidden: true })
    const info = section('这段视频')

    el.append(video, stageLine, bar, detail, dlLine, dlBar, errBox, info)

    /**
     * 起播之后的阶段行。**由 `video.paused` 派生**，不是写死「正在播放」：下载进度在暂停时
     * 照样一条条推 PLAYING 过来（mediaload.js 文件头的 `dl`），写死的话用户按了暂停，这行
     * 还说在播。下完之后不再有事件，所以还要听 `play` / `pause` / `volumechange` 自己重画。
     */
    let playing = false
    /** 起播时声音被浏览器拦了（`autoplay.blocked`）。之后用户开了声就不再提。 */
    let blocked = false
    const paintPlaying = () => {
      if (!playing) return
      stageLine.textContent = video.paused ? '已暂停' : '正在播放'
      detail.textContent = blocked && video.muted ? SOUND_BLOCKED_HINT : ''
    }
    const playEvents = ['play', 'pause', 'volumechange']
    for (const ev of playEvents) video.addEventListener(ev, paintPlaying)

    // 不定长（null）时铺满并压暗而不是编一个假百分比，理由与三处调用方共用的
    // `ui.setBar` 写在一起。
    setBar(bar, null)

    const paintInfo = (i) => {
      if (!i || info.body.childElementCount) return
      info.body.append(
        row('大小', bytes(i.bytes), { mono: true }),
        row('时长', duration(i.durationMs)),
        row('通道', i.via ?? '—'),
        // 支持 Range 才能 seek。不支持时进度条拖不动，而那看起来像"播放器坏了"。
        row('断点续传', i.supportsRange ? '支持（Range）' : '不支持（拖不动进度条）'),
      )
      if (i.absolute) info.body.append(row('地址', '绝对地址（跨源会被 COEP 拦）', { bad: true }))
      if (i.integrity && i.integrity !== 'ok') {
        info.body.append(h('p', { class: 'warnbox', text: `服务端报 integrity=${i.integrity}，这段视频可能不完整。` }))
      }
    }

    stopLoad = loadPhotoVideo(video, ctx.params.id, {
      onStage: (s) => {
        if (!alive) return
        paintInfo(s.info)
        if (s.stage === Stage.PLAYING) {
          if (!playing) { playing = true; blocked = Boolean(s.autoplay?.blocked) }
          unlock.update(s.autoplay)
          paintPlaying()
          setBar(bar, s.pct)
          setDownloadRow(dlLine, dlBar, s.dl, dlText(s.dl))
          return
        }
        if (s.stage === Stage.UNAVAILABLE || s.stage === Stage.ERROR) {
          playing = false
          unlock.reset()
          stageLine.textContent = '播不了'
          detail.textContent = ''
          bar.hidden = true
          setDownloadRow(dlLine, dlBar, null, '')
          errBox.hidden = false
          errBox.textContent = s.text
          return
        }
        // **两行不能是同一句。** `s.text` 就是 `stageText(s.stage, s)`（见 mediaload 的
        // `say`），两处都写它的话「正在取视频信息…」会上下显示两遍。名字归 stageLine，
        // 数字归 detail —— 而只有下载那一档有数字（`3.2 / 8.1 MB`、`本机已有，秒开`）。
        // 下载行在起播前收着：`detail` 已经在报同一串数字了。
        stageLine.textContent = stageName(s.stage, { fromCache: s.fromCache, via: s.via ?? s.dl?.source }) || '正在加载…'
        detail.textContent = s.stage === Stage.DOWNLOAD ? s.text : ''
        setBar(bar, s.pct)
        setDownloadRow(dlLine, dlBar, null, '')
      },
    })

    video.addEventListener('error', () => {
      const e = video.error
      // 把 code 翻成名字：那四种的修法毫不相干，只报一个数字等于没报。
      errBox.hidden = false
      errBox.textContent =
        `播放出错：code=${e?.code} ${MEDIA_ERR[e?.code] ?? '?'}` +
        ` network=${NETWORK_STATE[video.networkState]} ready=${READY_STATE[video.readyState]}` +
        (e?.message ? ` msg=${e.message}` : '')
    })

    return () => {
      alive = false
      stopLoad?.()
      unlock.reset()
      for (const ev of playEvents) video.removeEventListener(ev, paintPlaying)
      // **必须停**：不停的话切页之后音频继续放，而画面已经不在了。
      video.pause()
      video.removeAttribute('src')
      video.load()
    }
  },
}

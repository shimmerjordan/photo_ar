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
import { Stage, loadPhotoVideo } from '../mediaload.js'
import { bytes, duration, h, row, section } from '../ui.js'

export default {
  title: '试播',

  async mount(el, ctx) {
    let alive = true
    let stopLoad = null

    // controls 交给浏览器：自绘播放条要处理拖动、缓冲区间、全屏、画中画 ——
    // 而原生控件在每个平台上都已经对了，且带无障碍。
    const video = h('video', { class: 'player', controls: true, playsinline: true })
    // 静音自动播：iOS 与 Chrome 都只允许静音自动播。这一页有原生控件，
    // 用户点一下就有声音，所以不需要额外的「开声音」按钮。
    video.muted = true

    // 阶段行 + 进度条。**`.bar2` 而不是顶部那条 `#bar`**：这一页不是全屏，
    // 内容在木牌里，进度条跟着它走才不会看起来像页面级的加载。
    const stageLine = h('p', { class: 'p', text: '正在取视频信息…' })
    const bar = h('div', { class: 'bar2' }, h('i'))
    const detail = h('p', { class: 'p dim mono' })
    const errBox = h('p', { class: 'warnbox', hidden: true })
    const info = section('这段视频')

    el.append(video, stageLine, bar, detail, errBox, info)

    const setBar = (pct) => {
      // -1 / null = 不定长。`#bar` 那条用 class 切扫描动画，这一条在面板里，
      // 不定长时直接铺满并压暗（见 theme.css 的 reduce-motion 分支同款处理）——
      // 面板里一条来回扫的金条比它值得的注意力要抢眼得多。
      const known = typeof pct === 'number'
      bar.firstElementChild.style.transform = `scaleX(${known ? Math.min(1, Math.max(0, pct)) : 1})`
      bar.firstElementChild.style.opacity = known ? '1' : '.4'
    }
    setBar(null)

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
          stageLine.textContent = '正在播放'
          detail.textContent = ''
          setBar(s.pct)
          return
        }
        if (s.stage === Stage.UNAVAILABLE || s.stage === Stage.ERROR) {
          stageLine.textContent = '播不了'
          detail.textContent = ''
          bar.hidden = true
          errBox.hidden = false
          errBox.textContent = s.text
          return
        }
        stageLine.textContent = STAGE_LINE[s.stage] ?? '正在加载…'
        detail.textContent = s.text
        setBar(s.pct)
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
      // **必须停**：不停的话切页之后音频继续放，而画面已经不在了。
      video.pause()
      video.removeAttribute('src')
      video.load()
    }
  },
}

const STAGE_LINE = {
  [Stage.INFO]: '正在取视频信息…',
  [Stage.TICKET]: '正在准备播放通道…',
  [Stage.DOWNLOAD]: '正在下载视频',
  [Stage.BUFFER]: '正在缓冲首帧…',
}

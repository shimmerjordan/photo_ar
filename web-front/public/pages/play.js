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
 * `/v1/photo/<id>/media` 是**元信息**接口（返回 JSON），真流在它的 `url` 上。
 * 扫描页踩过这个坑（把元信息地址直接给 `<video src>`，浏览器拿 JSON 去喂解封装器，
 * 报 `DEMUXER_ERROR_COULD_NOT_OPEN` 而 HTTP 是 200）。这一页走同一条路，所以它能在
 * 不举照片的情况下把那条链路验一遍。
 */
import * as api from '../api.js'
import { MEDIA_ERR, NETWORK_STATE, READY_STATE } from '../diag.js'
import { playStream } from '../mp4stream.js'
import { bytes, duration, failed, h, loading, row, section } from '../ui.js'

export default {
  title: '试播',

  async mount(el, ctx) {
    let alive = true
    const id = ctx.params.id
    let video = null
    let stopStream = null

    const load = async () => {
      el.innerHTML = ''
      el.appendChild(loading('正在取视频信息…'))
      let info
      try {
        info = await api.mediaOfPhoto(id)
      } catch (e) {
        if (!alive) return
        el.innerHTML = ''
        el.appendChild(failed(`播不了：${e.message}`, load))
        return
      }
      if (!alive) return
      el.innerHTML = ''

      if (info.missing) {
        el.appendChild(h('p', { class: 'warnbox', text: '视频文件不在了（服务端报 missing）。' }))
        return
      }
      if (!info.url) {
        el.appendChild(h('p', { class: 'warnbox', text: '没有视频：服务端没给出地址。' }))
        return
      }
      if (info.integrity && info.integrity !== 'ok') {
        el.appendChild(h('p', { class: 'warnbox', text: `服务端报 integrity=${info.integrity}，这段视频可能不完整。` }))
      }

      // controls 交给浏览器：自绘播放条要处理拖动、缓冲区间、全屏、画中画 ——
      // 而原生控件在每个平台上都已经对了，且带无障碍。
      // 与扫描页同一个理由：`<video>` 的请求拿不到 HttpOnly 的会话 cookie，
      // 必须换成自带凭证的票据地址。见 api.playableUrl。
      const src = await api.playableUrl(info.url)
      if (!alive) return
      video = h('video', { class: 'player', controls: true, playsinline: true })
      // 静音自动播：与扫描页同一个理由（iOS 与 Chrome 都只允许静音自动播）。
      // 这一页有原生控件，用户点一下就有声音，所以不需要额外的「开声音」按钮。
      video.muted = true
      el.appendChild(video)
      // src 由 playStream 设 —— 它可能走 MediaSource（安卓上唯一能播的一条路），
      // 也可能退回直连。两条路它都自己接管。
      stopStream = playStream(video, src, {})

      const errBox = h('p', { class: 'warnbox', hidden: true })
      el.appendChild(errBox)
      video.addEventListener('error', () => {
        const e = video.error
        // 把 code 翻成名字：那四种的修法毫不相干，只报一个数字等于没报。
        errBox.hidden = false
        errBox.textContent =
          `播放出错：code=${e?.code} ${MEDIA_ERR[e?.code] ?? '?'}` +
          ` network=${NETWORK_STATE[video.networkState]} ready=${READY_STATE[video.readyState]}` +
          (e?.message ? ` msg=${e.message}` : '')
      })

      el.appendChild(section('这段视频',
        row('大小', bytes(info.bytes), { mono: true }),
        row('时长', duration(info.durationMs)),
        row('通道', info.via ?? '—'),
        // 支持 Range 才能 seek。不支持时进度条拖不动，而那看起来像"播放器坏了"。
        row('断点续传', info.supportsRange ? '支持（Range）' : '不支持（拖不动进度条）'),
        info.absolute ? row('地址', '绝对地址（跨源会被 COEP 拦）', { bad: true }) : null))
    }

    await load()

    return () => {
      alive = false
      stopStream?.()
      // **必须停**：不停的话切页之后音频继续放，而画面已经不在了。
      if (video) {
        video.pause()
        video.removeAttribute('src')
        video.load()
      }
    }
  },
}

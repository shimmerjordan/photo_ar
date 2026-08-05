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
import { CameraError, FrameGrabber, openCamera, stopCamera } from '../camera.js'
import { MEDIA_ERR, NETWORK_STATE, READY_STATE, diag, diagAlways, flushDiag, short } from '../diag.js'
import { Renderer } from '../render/gl.js'
import {
  FULL_RECT, TTL_MS, approach, clipVertices, imageToNdc, plausible, smoothingAlpha, unitSquareH,
  videoCrop,
} from '../render/screenquad.js'
import * as api from '../api.js'
import { playStream } from '../mp4stream.js'
import { button, h } from '../ui.js'

/** 多久送一帧给 Worker。Worker 忙时会丢帧（它只处理最新一帧），所以按 ~30fps 送就够。 */
const SEND_INTERVAL_MS = 33

/** 每一句都必须**能照着做**，而且要区分原因。见 Android 那边的教训。 */
const TIPS = {
  scanning: '把整张照片放进画面，靠近一点、拿稳。',
  no_features: '画面里几乎没有纹理。对准照片，避开纯色的墙面。',
  weak: '认不出来。让照片占满画面多一些，手指别压住边缘，避开反光。',
  ambiguous: '<span class="bad">库里有两张几乎一样的照片</span>，它们会互相干扰。请让管理员删掉其中一张。',
  empty: '这个账号下还没有可扫的照片。',
  flow_lost: '跟丢了，正在重新识别…',
  homography_lost: '跟丢了，正在重新识别…',
  quad_implausible: '角度太斜了，正一点。',
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
      tip: h('div', { id: 'tip', text: '正在准备…' }),
      meta: h('div', { id: 'meta' }),
    }
    const rescan = button('重新扫描', () => resetLock(), { kind: 'ghost', iconName: 'refresh' })
    const sound = button('开声音', () => {
      dom.clip.muted = !dom.clip.muted
      sound.querySelector('span').textContent = dom.clip.muted ? '开声音' : '静音'
      if (!dom.clip.muted) dom.clip.play().catch(() => {})
    }, { kind: 'ghost' })
    rescan.hidden = true
    sound.hidden = true

    dom.hud = h('div', { id: 'hud' }, dom.tip, h('div', { class: 'actions' }, rescan, sound), dom.meta)
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
      quad: null, quadAt: 0, smooth: null, lastRenderAt: 0,
      lockedPhoto: null, trackPoints: null,
      frameSeq: 0, inflight: false, lastSentAt: 0, paused: false,
      fps: { n: 0, at: 0, value: 0 }, detectMs: 0, trackMs: 0,
      raf: 0, stream: null, renderer: null, grabber: null, alive: true, stopStream: null,
    }

    const meta = () => {
      const parts = []
      const lib = ctx.libInfo?.()
      if (lib) {
        parts.push(`库 ${lib.nPhotos} 张`)
        if (lib.skipped?.length) parts.push(`${lib.skipped.length} 张不在识别库`)
        if (lib.hasVocab === false) parts.push('无词表·全量扫描')
      }
      if (st.fps.value) parts.push(`${st.fps.value} fps`)
      if (st.detectMs) parts.push(`检测 ${st.detectMs}ms`)
      if (st.trackMs) parts.push(`跟踪 ${st.trackMs}ms`)
      if (st.lockedPhoto) {
        const age = st.quadAt ? Math.round(performance.now() - st.quadAt) : null
        parts.push(st.quad ? `贴合中 ${age}ms前` : age === null ? '无四角' : `四角已过期 ${age}ms`)
        if (st.trackPoints) parts.push(`跟踪点 ${st.trackPoints}`)
        const v = dom.clip
        if (!st.lockedPhoto.mediaUrl) parts.push('无视频')
        else if (v.error) parts.push(`视频错误 ${v.error.code}`)
        else if (v.readyState < 2) parts.push(`视频加载中 rs=${v.readyState}`)
        else if (v.paused) parts.push('视频已暂停')
        else parts.push(`视频 ${v.videoWidth}×${v.videoHeight}`)
      }
      dom.meta.textContent = parts.join(' · ')
    }
    ctx.bindDiagToggle?.(dom.meta)

    // ── 视频那一环的打点 ────────────────────────────────────────────
    const onVideoErr = () => {
      const e = dom.clip.error
      diagAlways(`视频 error code=${e?.code} ${MEDIA_ERR[e?.code] ?? '?'}` +
        ` msg=${e?.message || '(空)'} network=${NETWORK_STATE[dom.clip.networkState]}` +
        ` ready=${READY_STATE[dom.clip.readyState]} src=${short(dom.clip.currentSrc)}`)
      meta()
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

    function resetLock() {
      ctx.worker?.postMessage({ type: 'reset' })
      st.quad = null
      st.smooth = null
      st.lockedPhoto = null
      // 先掐流再动元素：不掐的话上一段的 fetch 还在往一个已经换了 src 的
      // SourceBuffer 里喂，报的是一个跟「重新扫描」毫无关系的 append 错误。
      st.stopStream?.()
      st.stopStream = null
      dom.clip.pause()
      dom.clip.removeAttribute('src')
      delete dom.clip.dataset.photo
      dom.clip.load()
      rescan.hidden = true
      sound.hidden = true
      tip(TIPS.scanning)
    }

    /**
     * 命中之后加载视频。**两步** —— `/v1/photo/<id>/media` 是元信息接口（返回 JSON），
     * 真流在它的 `url` 上。直接把前者塞给 `<video src>` 会让浏览器拿 JSON 去喂解封装器，
     * 报 `DEMUXER_ERROR_COULD_NOT_OPEN` 而 HTTP 是 200 —— 这个坑踩过。
     */
    async function onHit(m) {
      const photo = m.photo
      st.lockedPhoto = photo
      rescan.hidden = false
      const title = photo?.title ? `「${photo.title}」` : '这张照片'
      if (!photo?.mediaUrl) {
        diagAlways(`命中 ${photo?.id?.slice(0, 8)} 但没有 mediaUrl（这张没配视频）`)
        tip(`认出了 ${title}，但它还没有配视频。`, { hit: true })
        return
      }
      if (dom.clip.dataset.photo === photo.id) {
        tip(`认出了 <b>${title}</b>，内点 ${m.inliers}。`, { hit: true })
        return
      }
      dom.clip.dataset.photo = photo.id
      diagAlways(`命中 ${photo.id?.slice(0, 8)} 内点=${m.inliers} aspect=${photo.aspect ?? 'null'} → 取媒体信息`)
      tip(`认出了 <b>${title}</b>，正在取视频…`, { hit: true })

      let info
      try {
        info = await api.mediaOfPhoto(photo.id)
      } catch (e) {
        diagAlways(`媒体信息失败 ${e.status ?? ''} ${e.message}`)
        tip(`认出了 ${title}，但取视频信息失败（${e.message}）。`, { hit: true })
        return
      }
      if (!st.alive) return
      diagAlways(`媒体信息 via=${info.via} absolute=${info.absolute} range=${info.supportsRange}` +
        ` bytes=${info.bytes} ${info.durationMs}ms missing=${info.missing} integrity=${info.integrity}`)

      if (info.missing) return tip(`认出了 ${title}，但视频文件不在了（服务端报 missing）。`, { hit: true })
      if (!info.url) return tip(`认出了 ${title}，但服务端没给出视频地址。`, { hit: true })
      if (info.integrity && info.integrity !== 'ok') {
        diagAlways(`⚠️ integrity=${info.integrity}，视频可能不完整`)
      }
      if (info.absolute) {
        diagAlways('⚠️ 媒体是绝对地址。跨源会被 COEP 拦，要在部署层代理成同源。')
      }

      // 两层绕路，缺一不可（都是真机上量出来的，见 mp4stream.js 顶部那张表）：
      //   1. `playableUrl` —— 换成自带凭证的票据地址，因为媒体组件拿不到会话 cookie；
      //   2. `playStream`  —— 页面自己 fetch、经 MediaSource 喂，因为那个组件还有
      //      独立的 TLS 栈，不认自签证书。
      const src = await api.playableUrl(info.url)
      if (!st.alive) return
      dom.clip.muted = true
      st.stopStream?.()
      st.stopStream = playStream(dom.clip, src, {
        onEvent: (name, detail) => diagAlways(`流 ${name} ${JSON.stringify(detail ?? {})}`),
      })
      tip(`认出了 <b>${title}</b>，内点 ${m.inliers}。`, { hit: true })
      // 起播交给 playStream（它在第一个分片到位时就 play）。这里只负责把「开声音」
      // 露出来 —— 等 `playing` 而不是等 `play()` 返回：MSE 那条路上 play() 可能在
      // 还没有可解码帧时就被调用，返回不代表真的在动。
      dom.clip.addEventListener('playing', () => { sound.hidden = false }, { once: true })
    }

    function onWorkerMessage(ev) {
      const m = ev.data
      if (!st.alive) return
      if (m.type === 'error') {
        diagAlways(`Worker 错误 ${m.message}`)
        tip(`<span class="bad">${m.message}</span>`)
        return
      }
      if (m.type !== 'result') return
      st.inflight = false
      if (m.state === 'locked' && m.ms) st.trackMs = m.ms
      else if (m.ms) st.detectMs = m.ms
      st.trackPoints = m.inliers ?? null
      diag(() => `${m.state === 'locked' ? '跟踪' : '检测'} ${m.reason}` +
        ` 内点=${m.inliers ?? '-'} ${m.ms}ms 四角=${m.quad ? '有' : '无'}` +
        (m.tracked ? ` 光流存活=${m.tracked}` : '') +
        (m.reseeded ? ` 补种子→${m.reseeded}` : '') +
        (m.gaveUp ? ' 放手' : ''))

      // 命中就加载视频，与这一帧四角合不合格**无关**（命中那一帧算不出四角是常态）。
      if (m.fresh) onHit(m).catch((e) => diagAlways(`onHit 失败 ${e.message}`))

      if (m.quad && plausible(m.quad)) {
        st.quad = m.quad
        st.quadAt = performance.now()
      } else if (m.gaveUp) {
        // 放手时撤掉四角但**不停视频** —— 用户可能只是手抖了一下，重新检测通常一两秒
        // 就回来。停了再播会从头开始，那比继续播难看得多。
        st.quad = null
        st.smooth = null
        tip(TIPS[m.reason] ?? TIPS.scanning)
      } else if (!st.lockedPhoto) {
        tip(TIPS[m.reason] ?? TIPS.scanning)
      }
      meta()
    }
    ctx.worker.addEventListener('message', onWorkerMessage)

    // ── 相机 ────────────────────────────────────────────────────────
    const lib = ctx.libInfo?.()
    if (lib && lib.nPhotos === 0) {
      tip(TIPS.empty)
      meta()
    } else {
      try {
        tip('正在开相机…')
        st.stream = await openCamera(dom.cam)
      } catch (e) {
        // 相机开不了是这一页的**终局错误**。整屏说清原因（含 iOS 微信那条），
        // 而不是塞进底部一行会被下一句盖掉的提示。
        el.appendChild(h('div', { class: 'gate-inline' },
          h('p', { class: 'bad', text: e instanceof CameraError ? e.message : `开相机失败：${e.message}` })))
        return teardown
      }
      if (!st.alive) {
        stopCamera(st.stream)
        return teardown
      }
      st.renderer = new Renderer(dom.canvas)
      st.grabber = new FrameGrabber(dom.cam)
      tip(TIPS.scanning)
      st.raf = requestAnimationFrame(loop)
    }

    function loop(now) {
      if (!st.alive) return
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
      if (st.quad && age <= TTL_MS) {
        if (!st.smooth) st.smooth = Float32Array.from(st.quad)
        const dt = st.lastRenderAt ? now - st.lastRenderAt : 0
        approach(st.smooth, st.quad, smoothingAlpha(dt))
        const ndc = imageToNdc(st.smooth, vw / vh, canvasAspect, new Float32Array(8))
        const hm = unitSquareH(ndc)
        if (hm) {
          const photoAspect = st.lockedPhoto?.aspect > 0 ? st.lockedPhoto.aspect : 1.5
          const videoAspect = dom.clip.videoWidth > 0 ? dom.clip.videoWidth / dom.clip.videoHeight : 0
          const clip = new Float32Array(16)
          // 面片**恒等于整张照片**（FULL_RECT），比例对不上时裁的是源（videoCrop）——
          // 也就是 object-fit: cover。上一版反过来：缩面片、留出照片边缘。
          if (clipVertices(hm, FULL_RECT, clip)) {
            const drew = r.drawVideoQuad(clip, dom.clip, videoCrop(photoAspect, videoAspect))
            if (!drew) diag(() => `几何 OK 但没画：视频 ready=${READY_STATE[dom.clip.readyState]}`)
          } else {
            diag('clipVertices 拒了这一帧（四边形跨越无穷远线或退化）')
          }
        }
      } else if (st.quad && age > TTL_MS) {
        st.quad = null
        st.smooth = null
      }
      st.lastRenderAt = now
      flushDiag()

      st.fps.n++
      if (now - st.fps.at > 1000) {
        st.fps.value = Math.round((st.fps.n * 1000) / (now - st.fps.at))
        st.fps.n = 0
        st.fps.at = now
        meta()
      }

      if (!st.paused && !st.inflight && now - st.lastSentAt >= SEND_INTERVAL_MS) {
        const img = st.grabber.grab()
        if (img) {
          st.lastSentAt = now
          st.inflight = true
          const buf = img.data.buffer
          // transfer：1280×960 的 RGBA 是 4.9MB，每秒 30 次，克隆不可接受。
          ctx.worker.postMessage(
            { type: 'frame', id: ++st.frameSeq, width: img.width, height: img.height, buf }, [buf])
        }
      }
    }

    const onVis = () => { st.paused = document.hidden }
    document.addEventListener('visibilitychange', onVis)

    function teardown() {
      st.alive = false
      // **必须停**：不停的话切页之后那十几 MB 还在下，而用户以为已经离开了。
      st.stopStream?.()
      cancelAnimationFrame(st.raf)
      document.removeEventListener('visibilitychange', onVis)
      ctx.worker.removeEventListener('message', onWorkerMessage)
      dom.clip.removeEventListener('error', onVideoErr)
      for (const ev of videoEvents) dom.clip.removeEventListener(ev, onVideoEvent)
      // **相机必须停**：不停的话相机灯一直亮、电量哗哗掉，而用户以为已经离开这一页了。
      if (st.stream) stopCamera(st.stream)
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

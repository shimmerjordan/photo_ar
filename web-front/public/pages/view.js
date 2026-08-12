/**
 * 宾客看自己那一张：大图 + 视频 + 存到手机。**这是「详情」页对宾客的那一版。**
 *
 * ## 为什么不复用 `detail.js`，哪怕它长得很像
 *
 * 差别不在界面，在**接口**：详情页调 `/v1/photo/<id>`，而那个响应里有 `refPath` /
 * `videoPath`（NAS 上的绝对路径）和 `selfScore` —— 服务端会把它们发给任何有授权的人。
 * 宾客不需要那些，也不该看到 NAS 的目录结构。
 *
 * 靠 `if (isAdmin)` 在同一页里藏字段是能做到的，但那把权限判断散进了模板里：
 * 以后谁在那一页加一行 `row('自匹配', d.selfScore)` 就漏了，而漏了没有任何症状。
 * **这一页根本不调那个接口** —— 它要的标题从 `/v1/photos` 拿（那个接口本来就按授权
 * 过滤，宾客拿到的行数天然是对的）。
 *
 * ## 存到手机这件事，我们能保证的比用户以为的少
 *
 * 网页存下来的东西落在**浏览器的下载目录**，不是相册。安卓上多数相册应用会扫到
 * `Download/`，但那是相册的行为、不是我们能保证的。所以那句话必须写在按钮旁边 ——
 * 用户点完在相册里找不到，第一反应是"没存成"，然后会再点五次。
 */
import * as api from '../api.js'
import { savePhotoImage, savePhotoVideo } from '../download.js'
import { Stage, loadPhotoVideo } from '../mediaload.js'
import { cachedThumbUrl } from '../prefetch.js'
import { button, failed, framed, h, loading, section } from '../ui.js'

export default {
  title: '照片',

  async mount(el, ctx) {
    let alive = true
    let stopLoad = null
    /** 本机缩略图那份 `blob:` 地址。**必须 revoke** —— 见 prefetch.cachedThumbUrl。 */
    let thumbObjectUrl = null

    const id = ctx.params.id
    if (!id) {
      el.appendChild(h('p', { class: 'state', text: '缺少照片 id' }))
      return () => { alive = false }
    }

    const load = async () => {
      el.innerHTML = ''
      el.appendChild(loading('正在取照片…'))
      let list
      try {
        list = await api.photos()
      } catch (e) {
        if (!alive) return
        el.innerHTML = ''
        el.appendChild(failed(e.message, load))
        return
      }
      if (!alive) return
      const photo = list.find((p) => (p.photoId ?? p.id) === id)
      // 本机那份缩略图**在建 DOM 之前**问。放在后面的话 `<img>` 会先带着一个空 src
      // 进文档，那一帧渲染出来是个破图框 —— 而这一页第一眼看到的就是它。
      // 这一问很快（一次 Cache Storage 查找），而此刻屏幕上还是 loading 占位。
      if (photo) thumbObjectUrl = await cachedThumbUrl(id)
      if (!alive) return
      el.innerHTML = ''
      if (!photo) {
        // 授权被撤、照片被删、或者链接是别人的。三种情况用户能做的事一样：找管理员。
        el.appendChild(h('p', { class: 'warnbox', text: '这张照片不在你的清单里了。可能是管理员删掉了它，或者收回了授权。' }))
        return
      }
      const title = photo.title || '（未命名）'

      // ── 大图：先上缩略图，再悄悄换成原图 ────────────────────────────
      // 原图可能好几 MB，直接等它等于对着白屏。缩略图在预取时已经进了 Cache Storage，
      // 而 `<img>` 不查那里，所以命中时喂给它的是一个 blob 地址（cachedThumbUrl 的说明）。
      const img = h('img', {
        class: 'ref', alt: title,
        src: thumbObjectUrl ?? `/v1/photo/${id}/thumb?rev=${ctx.shell.libraryRev}`,
      })
      el.appendChild(framed(img))
      const full = new Image()
      full.addEventListener('load', () => { if (alive) img.src = full.src })
      // 原图取不到不是错误：缩略图还在，页面照样能用。只有下载会失败，而那时才报。
      full.src = api.refUrl(id)

      el.appendChild(h('h1', { class: 'ttl', text: title }))

      // ── 视频 ────────────────────────────────────────────────────────
      if (photo.hasVideo !== false) {
        const video = h('video', { class: 'player', controls: true, playsinline: true })
        video.muted = true
        const stageLine = h('p', { class: 'p dim' })
        const vbar = h('div', { class: 'bar2' }, h('i'))
        el.append(video, stageLine, vbar)
        const setBar = (pct) => {
          const known = typeof pct === 'number'
          vbar.firstElementChild.style.transform = `scaleX(${known ? Math.min(1, Math.max(0, pct)) : 1})`
          vbar.firstElementChild.style.opacity = known ? '1' : '.4'
        }
        setBar(null)
        stopLoad = loadPhotoVideo(video, id, {
          onStage: (s) => {
            if (!alive) return
            if (s.stage === Stage.PLAYING) {
              stageLine.textContent = ''
              setBar(s.pct)
            } else if (s.stage === Stage.UNAVAILABLE || s.stage === Stage.ERROR) {
              stageLine.textContent = `视频播不了：${s.text}`
              vbar.hidden = true
            } else {
              stageLine.textContent = `${STAGE_LINE[s.stage] ?? '正在加载…'}${s.text ? ` ${s.text}` : ''}`
              setBar(s.pct)
            }
          },
        })
      }

      // ── 存到手机 ────────────────────────────────────────────────────
      const note = h('p', { class: 'p mono' })
      const dbar = h('div', { class: 'bar2', hidden: true }, h('i'))
      const setDl = (pct) => {
        dbar.hidden = false
        dbar.firstElementChild.style.transform = `scaleX(${Math.min(1, Math.max(0, pct))})`
      }

      // 一次只让点一个：两个下载同时跑会互相抢带宽，而两条状态挤在同一行里读不出
      // 是哪一个在动。
      const actions = h('div', { class: 'actions' })
      const runSave = async (fn) => {
        for (const b of actions.children) b.disabled = true
        note.className = 'p mono'
        note.textContent = '正在准备…'
        try {
          note.textContent = await fn()
        } catch (e) {
          // 失败留在页面上（不用 toast）——它会自己消失，而这是用户唯一能拿去问人的线索。
          note.className = 'p mono bad'
          note.textContent = `没存成：${e.message}`
        } finally {
          dbar.hidden = true
          if (alive) for (const b of actions.children) b.disabled = false
        }
      }
      actions.appendChild(button('存原图', () => runSave(() => savePhotoImage(id, title)), { iconName: 'download' }))
      if (photo.hasVideo !== false) {
        actions.appendChild(button('存视频', () => runSave(() =>
          savePhotoVideo(id, title, {
            onProgress: ({ loaded, total }) => { if (total) setDl(loaded / total) },
          })), { iconName: 'download' }))
      }

      el.appendChild(section('存到手机',
        actions, dbar, note,
        h('p', { class: 'p dim', text: '存下来的文件在浏览器的下载目录里，不一定会出现在相册中 —— 安卓上多数相册会扫到「下载」文件夹，但那取决于相册应用。' }),
        h('p', { class: 'p dim', text: 'iPhone 上如果点了没反应，长按图片选「存储到照片」也能存。' })))
    }

    await load()

    return () => {
      alive = false
      stopLoad?.()
      // 不 revoke 的话那份解码后的图会一直挂着，来回进出几十次就是几十份。
      if (thumbObjectUrl) URL.revokeObjectURL(thumbObjectUrl)
    }
  },
}

const STAGE_LINE = {
  [Stage.INFO]: '正在取视频…',
  [Stage.TICKET]: '正在准备播放…',
  [Stage.DOWNLOAD]: '正在下载视频',
  [Stage.BUFFER]: '就快好了…',
}

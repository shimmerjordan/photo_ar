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
 *
 * ## 「本机缓存」与「存到手机」是两件事
 *
 * 缓存是存进**这个浏览器**（Cache Storage，mediastore.js）：没网也能在这里看、扫到秒开，但在
 * 相册里看不见；存到手机是交给浏览器的下载器，落进下载目录。宾客最常问的是"到了现场没信号
 * 还能不能看"——那是前者。所以两块木牌分开、缓存那块在上面。
 */
import * as api from '../api.js'
import { precheckSingle } from '../cacheall.js'
import { cacheButtonLabel, cacheStateText } from '../cachelabel.js'
import { savePhotoImage, savePhotoVideo } from '../download.js'
import { Stage, dlText, loadPhotoVideo, stageName } from '../mediaload.js'
import { cacheGroup, groupKeys, groupStates, onChange, pin, removeGroup } from '../mediastore.js'
import { soundUnlocker } from '../playback.js'
import { icon } from '../pixelicons.js'
import { cachedThumbUrl } from '../prefetch.js'
import {
  SOUND_BLOCKED_HINT, button, confirmDanger, esc, failed, framed, h, loading, section, setBar,
  setDownloadRow, toast,
} from '../ui.js'

/** 缓存按钮的动作在这一页怎么说（按钮上的字取自 `cacheButtonLabel`，不可点时原样用它的）。 */
const ACTION_TEXT = { cache: '缓存到本机', pin: '固定到本机', remove: '从本机移除' }

export default {
  title: '照片',

  async mount(el, ctx) {
    let alive = true
    let stopLoad = null
    /** 本机缩略图那份 `blob:` 地址。**必须 revoke** —— 见 prefetch.cachedThumbUrl。 */
    let thumbObjectUrl = null
    /** 「第一次手势自动开声」的监听（挂在 document 上，离开这一页必须摘）。 */
    let disarmSound = null
    /**
     * 播放器下面那组下载行 / 下载条收起来、之后不再画（「本机缓存」木牌点了「从本机移除」时调）。
     * 没有视频时是 null。
     */
    let forgetDlRow = null
    /** 「本机缓存」木牌对 mediastore 的订阅与节流计时。 */
    let offCache = null
    let cacheTimer = 0
    /** 被拦提示那一句的收起计时（需在卸载时撤掉）。 */
    let hintTimer = 0

    const id = ctx.params.id
    if (!id) {
      el.appendChild(failed('缺少照片 id', () => ctx.shell.pop()))
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
        src: thumbObjectUrl ?? `${api.thumbUrl(id)}?rev=${ctx.shell.libraryRev}`,
      })
      el.appendChild(framed(img))
      const full = new Image()
      full.addEventListener('load', () => { if (alive) img.src = full.src })
      // 原图取不到不是错误：缩略图还在，页面照样能用。只有下载会失败，而那时才报。
      full.src = api.refUrl(id)

      el.appendChild(h('h1', { class: 'ttl', text: title }))

      // ── 视频 ────────────────────────────────────────────────────────
      if (photo.hasVideo !== false) {
        // 默认有声，由 playback.startPlayback 决定、被拦时退静音。
        // `data-role="sound"`：原生控件的静音键在 `<video>` 的 shadow DOM 里，点它时事件的
        // target 被重定向成 `<video>` 本身。不标的话第一下点那个静音键，armSoundUnlock 先把
        // 声音开了、静音键紧跟着又把它关掉 —— 用户看到的是"点了开声音，没反应"。代价是点在
        // 视频上（比如暂停）不代劳开声；但这一下**不消耗**监听（见 playback.armSoundUnlock），
        // 之后点页面别处照样开声 —— 提示里那句「或点一下页面别处」因此是真的。
        const video = h('video', { class: 'player', controls: true, playsinline: true, 'data-role': 'sound' })
        // 被拦成静音时才挂（每条 PLAYING 交给 `unlock.update`），不在建 `<video>` 时就挂：
        // 取信息、下前几块那几秒里的一次滑动或点击会把这个一次性监听白白用掉。
        const unlock = soundUnlocker(video)
        disarmSound?.()
        disarmSound = unlock.reset
        // 阶段行（起播后只在被拦成静音时说一句）+ 阶段条（起播后 = 播放进度）；
        // 下载行 + 下载条（起播后接着说下载，见 `setDownloadRow`）。两件事同时在发生，
        // 所以是两组，不挤在一行里（设计 §0 需求 3：原来被顶掉的是下载）。
        const stageLine = h('p', { class: 'p dim' })
        const vbar = h('div', { class: 'bar2' }, h('i'))
        const dlLine = h('p', { class: 'p mono', hidden: true })
        const dlBar = h('div', { class: 'bar2', hidden: true }, h('i'))
        el.append(video, stageLine, vbar, dlLine, dlBar)
        setBar(vbar, null)
        /** 起播时声音被浏览器拦了（`autoplay.blocked`）。之后用户开了声就不再提。 */
        let blocked = false
        let playing = false
        /**
         * 用户在「本机缓存」木牌上把这一组移除了。之后的 PLAYING 还带着那个已经下完的下载任务
         * （`dl.done`），照画的话下载行会一直写着「已存到本机，下次秒开」—— 与同屏木牌上的
         * 「未缓存」自相矛盾。所以移除之后这一组不再画，木牌负责说缓存的事。
         */
        let dlForgotten = false
        forgetDlRow = () => {
          dlForgotten = true
          setDownloadRow(dlLine, dlBar, null, '')
        }
        /**
         * 被拦提示。**撤下时先占着位置、过一会儿再收**：撤它的通常正是用户点页面别处的那一下
         * （pointerup 里开声 → volumechange），而那一下的 click 在 pointerup 之后才派发、按**那时**的
         * 布局命中。立刻收掉这两行字，下面的木牌整体上移，手指底下的「固定到本机」换成了别的东西，
         * 这一下就点空了 —— fix1 用可信触屏事件在真浏览器里量到过：click 落在了按钮下面那段说明上。
         */
        let hintOn = false
        const cancelHintCollapse = () => {
          clearTimeout(hintTimer)
          hintTimer = 0
          stageLine.style.visibility = ''
        }
        const paintHint = () => {
          if (!playing) return
          if (blocked && video.muted) {
            cancelHintCollapse()
            hintOn = true
            stageLine.textContent = SOUND_BLOCKED_HINT
          } else if (hintOn) {
            hintOn = false
            stageLine.style.visibility = 'hidden'
            hintTimer = setTimeout(() => {
              hintTimer = 0
              stageLine.style.visibility = ''
              stageLine.textContent = ''
            }, 600)
          } else if (!hintTimer) {
            stageLine.textContent = ''
          }
        }
        video.addEventListener('volumechange', paintHint)
        stopLoad = loadPhotoVideo(video, id, {
          onStage: (s) => {
            if (!alive) return
            if (s.stage === Stage.PLAYING) {
              if (!playing) { playing = true; blocked = Boolean(s.autoplay?.blocked) }
              unlock.update(s.autoplay)
              paintHint()
              setBar(vbar, s.pct)
              if (dlForgotten) setDownloadRow(dlLine, dlBar, null, '')
              else setDownloadRow(dlLine, dlBar, s.dl, dlText(s.dl))
            } else if (s.stage === Stage.UNAVAILABLE || s.stage === Stage.ERROR) {
              playing = false
              unlock.reset()
              // 等着收起的提示别把这句盖掉（计时到了会清空这一行）。
              cancelHintCollapse()
              stageLine.textContent = `视频播不了：${s.text}`
              vbar.hidden = true
              setDownloadRow(dlLine, dlBar, null, '')
            } else {
              // 这一页的阶段只有一行，所以名字与数字要拼成一句完整的话：
              // 光有数字（`3.2 / 8.1 MB`）读不出这是在下载视频，而那正是宾客最想知道的。
              // 下载行在起播前**收着**：这一行已经在说下载了，两行同一串数字是重复。
              const name = stageName(s.stage, { fromCache: s.fromCache, via: s.via ?? s.dl?.source }) || '正在加载…'
              stageLine.textContent = s.stage === Stage.DOWNLOAD ? `${name} ${s.text}` : name
              setBar(vbar, s.pct)
              setDownloadRow(dlLine, dlBar, null, '')
            }
          },
        })
      }

      // ── 本机缓存（一组 = 原图 + 视频）──────────────────────────────
      // 没配视频（或服务端太老、没给 videoAssetId）就不出这块：那时按钮只能是一个点不动的
      // 「无视频」，而原图单独缓存的意义不大（它有缩略图顶着，存到手机才是用户要的）。
      if (groupKeys(photo).video) el.appendChild(cacheCard(photo, title, { onRemoved: () => forgetDlRow?.() }))

      // ── 存到手机 ────────────────────────────────────────────────────
      const note = h('p', { class: 'p mono' })
      const dbar = h('div', { class: 'bar2', hidden: true }, h('i'))
      // 下载那条一开始是收着的：没在下载时一条空槽看起来像"卡住了"。
      const setDl = (pct) => { dbar.hidden = false; setBar(dbar, pct) }

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

    /**
     * 「本机缓存」木牌：状态一句 + 一个按钮。按钮做什么由 `cacheButtonLabel` 的 `action` 定
     * （媒体页的卡片用同一张表）；这一页把动作说全：缓存到本机 / 固定到本机 / 从本机移除。
     *
     * 状态**订阅** mediastore 而不是点完自己改：同一段视频可能正被这一页的播放器、预取、
     * 媒体页的「全部缓存」同时要（它们挂在同一个下载任务上），谁动了这里都该跟着变。
     * 下载中每收一块就有一个事件，所以节流到 250ms 再去问 `groupStates`（它要翻一遍缓存键）。
     *
     * `onRemoved`：移除成功后调，让播放器下面那行「已存到本机」收起来（见 `forgetDlRow`）。
     */
    function cacheCard(photo, title, { onRemoved = null } = {}) {
      const stateLine = h('p', { class: 'p', text: '正在查本机有没有…' })
      const btn = h('button', { onclick: () => onClick() })
      const note = h('p', { class: 'p mono', hidden: true })
      const { video: videoKey, ref: refKey } = groupKeys(photo)
      /** 最近一次查到的状态（`groupStates` 的一项）。null = 还没查出来。 */
      let cur = null
      /** 点下去到下载任务真正建起来之间（要先申请持久存储、查一遍缓存）别让人再点。 */
      let busy = false

      const paint = () => {
        const lab = cacheButtonLabel(cur)
        if (cur) stateLine.textContent = cacheStateText(cur)
        // `kind` 的三档落到这一页现成的两种按钮上：常规 = 金框（这时该点它）；
        // 已办妥 / 不可用 = 夜色框（移除是次要的、也是危险的那个动作）。
        btn.className = lab.kind === '' ? '' : 'ghost'
        btn.disabled = busy || !cur || !lab.action
        btn.innerHTML = `${icon(lab.action === 'remove' ? 'trash' : 'cache')}<span>${esc(ACTION_TEXT[lab.action] ?? lab.text)}</span>`
      }
      const fail = (msg) => {
        // 失败留在木牌上（不用 toast）：它会自己消失，而这是用户唯一能拿去问人的线索。
        note.className = 'p mono bad'
        note.textContent = msg
        note.hidden = false
      }

      let refreshing = false
      let again = false
      const refresh = async () => {
        if (refreshing) { again = true; return }
        refreshing = true
        try {
          const m = await groupStates([photo])
          if (!alive) return
          cur = m.get(String(id)) ?? null
          paint()
        } catch (e) {
          fail(`查不到本机缓存：${e.message}`)
        } finally {
          refreshing = false
          if (again && alive) { again = false; refresh() }
        }
      }
      const schedule = () => {
        if (cacheTimer || !alive) return
        cacheTimer = setTimeout(() => { cacheTimer = 0; refresh() }, 250)
      }
      offCache?.()
      offCache = onChange((evt) => {
        if (evt.type === 'job' && evt.snap?.key !== videoKey && evt.snap?.key !== refKey) return
        if (evt.type === 'cache' && evt.key != null && evt.key !== videoKey && evt.key !== refKey) return
        if (evt.type === 'pin' && evt.photoId !== String(id)) return
        schedule()
      })

      const onClick = async () => {
        const { action } = cacheButtonLabel(cur)
        note.hidden = true
        if (action === 'pin') {
          // 已经在本机了，只差一个"别自动删它"的记号 —— 不重下。
          pin(id)
          toast('已固定到本机，不会被自动清掉')
          return
        }
        if (action === 'remove' && !confirmDanger(`从本机移除「${title}」的缓存？下次看要重新下载。`)) return
        // 空间预检（fix1 Finding 3）：与媒体页的卡片同一套 precheckSingle，别在这里再写一遍。
        if (action === 'cache' && !(await precheckSingle(photo, cur))) return
        if (action !== 'cache' && action !== 'remove') return
        busy = true
        paint()
        try {
          if (action === 'cache') {
            const r = await cacheGroup(photo)
            if (!alive) return
            if (r.ok) toast('已缓存到本机，没网也能看')
            else fail(`没缓存成：${r.error}`)
          } else {
            await removeGroup(photo)
            if (!alive) return
            onRemoved?.()
            toast('已从本机移除')
          }
        } catch (e) {
          if (alive) fail(`${action === 'cache' ? '没缓存成' : '没移除成'}：${e.message}`)
        } finally {
          busy = false
          if (alive) refresh()
        }
      }

      paint()
      refresh()
      return section('本机缓存',
        stateLine,
        h('div', { class: 'actions' }, btn),
        note,
        h('p', { class: 'p dim', text: '缓存 = 原图和视频各存一份在这个浏览器里：到了现场没网也能在这里看，扫到这张时秒开。它不进相册 —— 要进相册用下面的「存到手机」。' }))
    }

    await load()

    return () => {
      alive = false
      stopLoad?.()
      disarmSound?.()
      offCache?.()
      clearTimeout(cacheTimer)
      clearTimeout(hintTimer)
      // 不 revoke 的话那份解码后的图会一直挂着，来回进出几十次就是几十份。
      if (thumbObjectUrl) URL.revokeObjectURL(thumbObjectUrl)
    }
  },
}

/**
 * 设置：账号、识别参数、诊断、关于。**Android `SettingsScreen` 的对译（减掉通道那一半）。**
 *
 * ## Android 的「通道」整节在这里不存在
 *
 * 那边有多端点探活（LAN / Tailscale / Cloudflare）、每条通道声明「适合 api 还是 media」、
 * 以及「现在走的是哪条」。网页版没有对应物：页面就是服务器发出来的，请求全走同源相对
 * 路径 —— 没有可选的通道，也就没有可配的东西。
 *
 * 这不是"少做了"：那一整套的存在理由是 App 装在手机上、要自己找服务端。网页反过来，
 * 是服务端把自己发给了手机。
 *
 * ## 保留的那三样都有对应物
 *
 * - **账号**：谁登录着、什么角色、什么时候过期、登出。
 * - **识别参数**：服务端的热配置（`recog.min_inliers` 等）。**只读** —— 改它要去管理台，
 *   而那边有完整的校验。这里显示是为了让"为什么这张扫不出来"能对上一个具体的数。
 * - **调试模式**：连按版本号 7 下解锁，解锁之后才有那块页面内日志、以及扫描页上那一排
 *   技术读数。**没解锁时这一节整个不显示** —— 宾客不需要知道它存在。
 *
 * ## 「高级设置」：通道那一半以另一种形状回来了一点
 *
 * 上面说网页版没有可选的通道 —— 页面与相机确实只能走当前地址（相机要 https，隧道给的）。
 * 但**大件的字节**可以换条路：家里的 NAS 在局域网里直连比隧道快一个数量级。所以这里有：
 *
 * 1. **视频默认有声音**（`prefs.soundOn`）：三处播放都照它起播，被浏览器拦了就先静音播。
 * 2. **媒体数据源**（`prefs.advanced().mediaBase`）：视频 / 原图的下载改走这个地址，页面本身
 *    不动。它**只加速、不能成为新的故障点**：连不上 mediastore 会自动退回当前地址续传，
 *    并熔断 60 秒（netsrc.js）。所以「保存」之后要 `resetTrip()` —— 不然上一个地址留下的
 *    熔断会让新地址白白晚一分钟才生效。
 * 3. **单个媒体的来源**：某一组的视频改用直链或本机文件（设计 §2.3）。
 *
 * 默认收起：这三样对绝大多数人都不用碰，摊开放在「账号」下面只会让人以为非设不可。
 */
import * as api from '../api.js'
import { thresholds } from '../recognize/consts.js'
import { isDiagEnabled, onDebugChange, setDiagEnabled } from '../diag.js'
import { Page } from '../navpolicy.js'
import { spaceShort } from '../cachelabel.js'
import {
  OVERRIDE_CACHE, cachedKeySet, overrideKey, putBlob, removeKey, requestPersist, storageEstimate, videoKey,
} from '../mediastore.js'
import { activeBase, probeBase, resetTrip, speedTest } from '../netsrc.js'
import { icon } from '../pixelicons.js'
import { advanced, normalizeBase, overrideOf, pickLanMode, setAdvanced, setOverride, setSoundOn, soundOn } from '../prefs.js'
import { bytes, button, confirmDanger, h, row, section, toast, when } from '../ui.js'

/** 「高级设置」展开与否。存 sessionStorage：这次会话里来回切页不用每次再点开，下次打开页面又是收起的。 */
const ADV_OPEN_KEY = 'photoar.adv.open'

export default {
  title: '设置',

  async mount(el, ctx) {
    let alive = true
    const me = ctx.me()

    el.appendChild(section('账号',
      row('名字', me?.name ?? '—'),
      row('角色', me?.role === 'admin' ? '管理员' : '访客'),
      // grantAll 的人看得到全库。这一条对访客很重要 —— 它解释了"为什么我能看到这些"。
      me?.grantAll ? row('授权范围', '全部照片') : null,
      me?.expiresAt ? row('有效期至', when(me.expiresAt)) : null,
      h('div', { class: 'actions' },
        button('退出登录', async () => {
          try {
            await api.logout()
          } catch { /* 就算服务端那边失败，本地也该回到未登录 */ }
          // 整页重载而不是自己清状态：登出要作废的东西散在好几处（cookie、库包、
          // Worker、相机）。重载是唯一能保证不漏的做法，而它只发生一次。
          location.replace(location.pathname)
        }, { kind: 'danger' }))))

    // 本机缓存。**只给非管理员**：管理员的入口在「管理」页里，那是他找它的地方。
    //
    // 这一条补的是一个真空缺：`needsAdmin(Page.CACHE)` 一直是 false（那一页管的是
    // 用户自己浏览器里的东西，本来就该人人可进），但**唯一的入口在 admin 页上** ——
    // 也就是说宾客既进得去又找不到。按空间预算预取之后这一页对他更要紧了：
    // 「为什么我的视频没全存下来」的答案就在那儿。
    if (!ctx.isAdmin()) {
      el.appendChild(section('本机缓存',
        h('p', { class: 'p', text: '登录后会在后台把你能扫的那些视频先存到手机里，扫到时直接从本机播、不走网络。' }),
        h('div', { class: 'actions' },
          button('看本机缓存', () => ctx.shell.push(Page.CACHE), { kind: 'ghost', iconName: 'cache' }))))
    }

    // 有效期快到时提醒。Android 那边同样有这一条 —— 访客 30 天、管理员 12 小时，
    // 而"扫到一半掉线"是最难解释的失败。
    if (me?.expiresAt) {
      const leftMs = (me.expiresAt < 1e12 ? me.expiresAt * 1000 : me.expiresAt) - Date.now()
      if (leftMs > 0 && leftMs < 60 * 60 * 1000) {
        el.appendChild(h('p', { class: 'warnbox', text: '登录即将过期，建议现在就重新登录一次。' }))
      }
    }

    el.appendChild(advancedSection({ isAlive: () => alive }))

    el.appendChild(section('识别参数（服务端热配置，只读）',
      row('内点门槛', String(thresholds.minInliers), { mono: true }),
      row('第一名 / 第二名比值', String(thresholds.ratio), { mono: true }),
      row('候选数 Top-K', String(thresholds.topK), { mono: true }),
      h('p', { class: 'p dim', text: '改它去管理台 → 识别设置。这里显示是为了让「为什么这张扫不出来」能对上一个具体的数。' })))

    // ── 调试模式那一节：**只在解锁之后出现** ──────────────────────────
    //
    // 用 hidden 而不是"解锁时再 appendChild"：它必须出现在「关于」**上面**（关于是最后
     // 一节，而版本号在里面），而 appendChild 只会追加到末尾。先建好、藏起来，
    // 解锁时取消 hidden —— 顺序就还是对的。
    const diagRow = h('div', { class: 'actions' })
    const debugSection = section('调试模式',
      h('p', { class: 'p', text: '页面顶部会出现一块滚动日志（带「复制」按钮），扫描页上会多出内点数、帧率、四角年龄这些读数。' }),
      h('p', { class: 'p dim', text: '扫不出来、或者认出来了但视频没播时，那块日志能分开五种互不相干的原因。在扫描页那条读数上连点三下可以就地关掉它。' }),
      diagRow)
    debugSection.hidden = !isDiagEnabled()
    el.appendChild(debugSection)

    const paintDiag = () => {
      diagRow.innerHTML = ''
      const on = isDiagEnabled()
      debugSection.hidden = !on
      diagRow.appendChild(button(on ? '退出调试模式' : '打开诊断日志', () => {
        setDiagEnabled(!on)
        paintDiag()
      }, { kind: 'ghost' }))
    }
    paintDiag()
    const offWatch = onDebugChange(paintDiag)

    const about = section('关于')
    el.appendChild(about)
    about.body.appendChild(row('识别后端', '取中…'))
    // ping 是免鉴权的轻请求，用来显示后端与降级状态。`backendDegraded` 那一条很重要：
    // XFeat 模型缺失时服务会静默回退 ORB，而"换了特征却毫无变化"只有这里看得出来。
    try {
      const p = await api.ping()
      if (!alive) return
      about.body.lastChild.remove()
      about.body.appendChild(row('识别后端', p.backend ?? '—'))
      if (p.backendDegraded) {
        about.body.appendChild(h('p', { class: 'warnbox', text: '服务端报 backendDegraded：配置要的后端起不来，已回退。识别行为与预期不同。' }))
      }
      if (p.photos !== undefined) about.body.appendChild(row('库内照片', String(p.photos), { mono: true }))
    } catch {
      if (!alive) return
      about.body.lastChild.remove()
      about.body.appendChild(row('识别后端', '连不上服务端'))
    }
    // ── 版本号：连按 7 下进调试模式 ──────────────────────────────────
    //
    // 为什么是这个手势：它要**不可能被误触**（宾客在设置页上乱点点不出来），但又要
    // 在手机上、没有键盘、没有控制台的情况下做得出来。安卓设置里"连点版本号"是所有人
    // 都见过的那一个，所以不用教。
    //
    // 7 下、每下间隔不超过 1.2 秒。数字给了反馈（后三下开始提示还差几下），否则连按的人
    // 不知道自己有没有在触发什么 —— 而没有反馈的隐藏手势等于不存在。
    const version = ctx.webCfg?.().version ?? '未知'
    // 用 `<button>` 而不是 `row()` 那个纯展示的 div：这一行其实是个可交互的隐藏
    // 手势入口，键盘与读屏用户也该能找到它、知道按了会发生什么。
    const versionRow = h('button', {
      class: 'ghost row2', type: 'button', 'aria-label': '版本（连按 7 下开调试）',
    }, h('span', { class: 'k', text: '版本' }), h('span', { class: 'v mono', text: version }))
    about.body.appendChild(versionRow)
    let taps = []
    versionRow.addEventListener('click', () => {
      if (isDiagEnabled()) return toast('调试模式已经是开着的')
      const now = Date.now()
      taps = taps.filter((t) => now - t < 1200)
      taps.push(now)
      const left = 7 - taps.length
      if (left <= 0) {
        taps = []
        setDiagEnabled(true)
        toast('调试模式已打开')
        return
      }
      if (left <= 3) toast(`再按 ${left} 下`)
    })

    about.body.appendChild(h('p', { class: 'p dim' },
      h('a', { href: 'https://github.com/shimmerjordan/photo_ar', target: '_blank', rel: 'noopener', text: '开源地址' })))

    return () => { alive = false; offWatch() }
  },
}

/** 一排互斥 chip 里的一块。`aria-pressed` 让读屏知道哪块是选中的（光靠金框它看不见）。 */
const chip = (text, on, onclick) =>
  h('button', { type: 'button', class: on ? 'chip on' : 'chip', 'aria-pressed': on ? 'true' : 'false', onclick }, text)

/** 测速结果的一格：MB/s（1024 进制，与全站的 MB 同一口径），失败如实说。 */
const speedText = (m) => {
  if (!m) return '—'
  if (m.error) return `失败（${m.error}）`
  return `${(m.bytes / 1048576 / (m.ms / 1000)).toFixed(1)} MB/s`
}

/**
 * 「高级设置」木牌：一个展开按钮 + 三块（声音 / 媒体数据源 / 单个媒体的来源）。
 *
 * 照片列表（挑照片、测速要用一段已授权的视频、列表里显示标题）**展开时才取**：大多数人
 * 进设置页不点开这一节，没必要每次都多打一个 `/v1/photos`。
 */
function advancedSection({ isAlive }) {
  const body = h('div', { hidden: true })
  const toggle = h('button', { class: 'ghost', type: 'button', 'aria-expanded': 'false', onclick: () => setOpen(body.hidden) })
  const sec = section('高级设置',
    h('p', { class: 'p dim', text: '视频默认的声音、换一个更快的媒体地址、给单独某段视频换来源。都只存在这个浏览器里。' }),
    h('div', { class: 'actions' }, toggle),
    body)

  /** `api.photos()` 只取一次；失败了下次再要时重取。 */
  let photosP = null
  const getPhotos = () => (photosP ??= api.photos().catch((e) => { photosP = null; throw e }))

  // ── 1. 视频默认有声音 ────────────────────────────────────────────
  const soundChips = h('div', { class: 'chips', role: 'group', 'aria-label': '视频默认有声音' })
  const paintSound = () => {
    const on = soundOn()
    soundChips.replaceChildren(
      chip('开', on, () => { setSoundOn(true); paintSound() }),
      chip('关', !on, () => { setSoundOn(false); paintSound() }))
  }
  paintSound()

  // ── 2. 媒体数据源 ────────────────────────────────────────────────
  const baseIn = h('input', {
    type: 'url', id: 'adv-base', placeholder: 'http://192.168.1.10:8964', value: advanced().mediaBase,
    autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false',
  })
  const baseErr = h('p', { class: 'p bad', hidden: true })
  const baseNow = row('现在', '')
  const onChips = h('div', { class: 'chips', role: 'group', 'aria-label': '启用媒体数据源' })
  const testBtn = button('测试', () => runTest(), { kind: 'ghost' })
  const probeLine = h('p', { class: 'p', hidden: true })
  const speedLine = h('p', { class: 'p mono', hidden: true })
  const hintBox = h('p', { class: 'warnbox', hidden: true })
  /**
   * 「测试」连通过的那个地址与测出来的写法：保存时若正是它，就用这一份（fix1 Finding 2）。
   * 只存局部变量、不经过 `setAdvanced` —— 测的若不是当前已保存的地址，不该现在就把已保存
   * 地址的 `lanMode` 污染掉，等真正保存时再由 `pickLanMode` 决定写哪个。
   */
  let testedBase = null
  let testedLanMode = null

  const baseError = (msg) => {
    baseErr.textContent = msg
    baseErr.hidden = !msg
  }
  const paintBase = () => {
    const a = advanced()
    onChips.replaceChildren(
      chip('启用', a.mediaBaseOn, () => setOn(true)),
      chip('停用', !a.mediaBaseOn, () => setOn(false)))
    let now
    if (!a.mediaBase) now = '没配，全部走当前地址'
    else if (!a.mediaBaseOn) now = `已关（${a.mediaBase}），全部走当前地址`
    // 熔断是 netsrc 的内存状态：刚才有一次经它的下载失败了，这一分钟先走当前地址。
    else if (!activeBase()) now = `${a.mediaBase} 刚才连不上，这一分钟先走当前地址`
    else now = `视频 / 原图走 ${a.mediaBase}`
    baseNow.lastChild.textContent = now
  }

  const setOn = (on) => {
    baseError('')
    const a = advanced()
    if (on === a.mediaBaseOn) return
    if (on && !a.mediaBase) return baseError('先填地址并点「保存」')
    setAdvanced({ mediaBaseOn: on })
    if (on) resetTrip()
    paintBase()
    toast(on ? '已启用' : '已停用，全部走当前地址')
  }

  const saveBase = () => {
    baseError('')
    let b
    try {
      b = normalizeBase(baseIn.value)
    } catch (e) {
      return baseError(e.message)
    }
    const a = advanced()
    // 换了地址，上一个地址测出来的写法就不作数了（除非刚测的就是这个，或者没换、还是原来那个）。
    const lanMode = pickLanMode({
      toSave: b, savedBase: a.mediaBase, savedLanMode: a.lanMode, testedBase, testedLanMode,
    })
    // 保存即启用：填了地址点保存的人要的就是「用它」，再让他找到旁边那块「启用」是多余的一步。
    setAdvanced({ mediaBase: b, mediaBaseOn: Boolean(b), lanMode })
    resetTrip()
    baseIn.value = b
    paintBase()
    toast(b ? '已保存并启用' : '已清除，全部走当前地址')
  }

  const clearBase = () => {
    baseError('')
    setAdvanced({ mediaBase: '', mediaBaseOn: false, lanMode: null })
    resetTrip()
    baseIn.value = ''
    testedBase = null
    testedLanMode = null
    probeLine.hidden = speedLine.hidden = hintBox.hidden = true
    paintBase()
    toast('已清除，全部走当前地址')
  }

  const runTest = async () => {
    baseError('')
    let b
    try {
      b = normalizeBase(baseIn.value)
    } catch (e) {
      return baseError(e.message)
    }
    if (!b) return baseError('先填一个地址')
    testBtn.disabled = true
    probeLine.className = 'p'
    probeLine.textContent = '正在连…'
    probeLine.hidden = false
    speedLine.hidden = hintBox.hidden = true
    try {
      const r = await probeBase(b)
      if (!isAlive()) return
      if (!r.ok) {
        probeLine.className = 'p bad'
        probeLine.textContent = `连不上（${r.error}）`
        hintBox.textContent = r.hint
        hintBox.hidden = false
        return
      }
      // 立即生效只在测的正是当前已保存地址时才做（fix1 Finding 2）：测一个还没保存的新地址
      // 不该现在就把已保存地址的 lanMode 污染掉。其余情况只记本地变量，保存时由 pickLanMode 定。
      if (b === advanced().mediaBase) setAdvanced({ lanMode: r.mode })
      testedBase = b
      testedLanMode = r.mode
      probeLine.textContent = `连通（${r.ms} ms，写法 ${r.mode}）`

      // 测速：拿一段这个账号有权限的视频，两条路各取 2MB。
      speedLine.hidden = false
      speedLine.textContent = '正在测速…'
      let photos
      try {
        photos = await getPhotos()
      } catch (e) {
        speedLine.textContent = `测不了速：取照片列表失败（${e.message}）`
        return
      }
      const p = photos.find((x) => x.videoAssetId)
      if (!p) {
        speedLine.textContent = '测不了速：你能看的照片里没有配了视频的'
        return
      }
      // 刚刚连通了：之前留下的熔断不该让测速跳过它。
      resetTrip()
      // speedTest 走的是**已保存并启用**的那个地址（sourcesFor 的口径）；输入框里的还没保存时，
      // 局域网那一格测的就不是它 —— 如实说要先保存，而不是把别的地址的速度写在这里。
      const same = activeBase() === b
      const sp = await speedTest(videoKey(p.videoAssetId))
      if (!isAlive()) return
      speedLine.textContent = `局域网 ${same ? speedText(sp.lan) : '—（保存并启用后才测这一格）'} · 默认地址 ${speedText(sp.origin)}`
    } finally {
      testBtn.disabled = false
      if (isAlive()) paintBase()
    }
  }
  paintBase()

  // ── 3. 单个媒体的来源 ────────────────────────────────────────────
  const ovList = h('div', { class: 'entries' })
  const ovEmpty = h('p', { class: 'p dim', text: '还没有设过。' })
  const photoSel = h('select', { id: 'adv-ov-photo' }, h('option', { value: '', text: '正在取照片…' }))
  let ovKind = 'url'
  const kindChips = h('div', { class: 'chips', role: 'group', 'aria-label': '来源类型' })
  const urlIn = h('input', { type: 'url', id: 'adv-ov-url', placeholder: 'https://example.com/video.mp4', autocomplete: 'off', spellcheck: 'false' })
  const urlField = h('label', { class: 'field', for: 'adv-ov-url' }, h('span', { text: '视频直链' }), urlIn)
  const fileIn = h('input', { type: 'file', accept: 'video/*', id: 'adv-ov-file' })
  const fileName = h('span', { class: 'fname', text: '未选择' })
  fileIn.addEventListener('change', () => {
    const f = fileIn.files?.[0]
    fileName.textContent = f ? `${f.name}（${bytes(f.size)}）` : '未选择'
  })
  // 「选择视频」那块木牌是可见的触发点，真 input 被 CSS 收起来了（照 media.js 的写法，理由见 theme.css）。
  const fileField = h('label', { class: 'file', for: 'adv-ov-file' },
    h('span', { text: '本机视频' }), fileIn,
    h('span', { class: 'pick', text: '选择视频' }), fileName)
  const ovErr = h('p', { class: 'p bad', hidden: true })
  const ovBusy = h('p', { class: 'p dim', hidden: true })
  const ovSave = h('button', { type: 'button', onclick: () => saveOv() }, '保存')
  /** photoId → 照片（给列表显示标题）。展开后取到才有。 */
  let byId = new Map()

  const ovError = (msg) => {
    ovErr.textContent = msg
    ovErr.hidden = !msg
  }
  const titleOf = (id) => {
    const p = byId.get(id)
    return p ? (p.title || '（未命名）') : `（看不到的照片 ${String(id).slice(0, 8)}）`
  }
  const paintKind = () => {
    kindChips.replaceChildren(
      chip('直链', ovKind === 'url', () => { ovKind = 'url'; paintKind() }),
      chip('本机文件', ovKind === 'file', () => { ovKind = 'file'; paintKind() }))
    urlField.hidden = ovKind !== 'url'
    fileField.hidden = ovKind !== 'file'
  }
  paintKind()

  const paintSelect = (photos) => {
    const ovs = advanced().overrides
    photoSel.replaceChildren(
      h('option', { value: '', text: '挑一张照片…' }),
      ...photos.map((p) => {
        const id = String(p.photoId ?? p.id)
        const tail = [p.hasVideo ? '' : '没配视频', ovs[id] ? '已设来源' : ''].filter(Boolean).join('，')
        return h('option', { value: id, text: `${p.title || '（未命名）'}${tail ? `（${tail}）` : ''}` })
      }))
  }

  const paintOvList = async () => {
    // 本机文件那一种要看字节还在不在：浏览器空间紧张时可能整个清掉站点缓存，记录却还在。
    const have = await cachedKeySet(OVERRIDE_CACHE)
    if (!isAlive()) return
    const entries = Object.entries(advanced().overrides)
    ovList.replaceChildren(...entries.map(([id, ov]) => {
      const title = titleOf(id)
      const what = ov?.kind === 'url' ? `直链 ${ov.url}` : `本机文件 ${ov?.name ?? ''} ${bytes(Number(ov?.size))}`
      const gone = ov?.kind === 'file' && !have.has(overrideKey(id))
      return h('div', { class: 'entry' },
        h('div', { class: 'entry-b' },
          h('span', { class: 'entry-t', text: title }),
          h('span', { class: 'entry-m', text: what }),
          gone ? h('span', { class: 'entry-m' }, h('span', { class: 'bad', text: '浏览器里已经没有这份文件了，扫到这张会报错 —— 移除，或重新选一次' })) : null),
        button('移除', () => removeOv(id), { kind: 'ghost' }))
    }))
    ovEmpty.hidden = entries.length > 0
  }

  /**
   * 换掉 / 移除一条时**两个键都要删**：本机文件存在 `overrideKey(id)` 下，直链下下来的那份存在
   * 直链地址本身下（mediaload 的口径，换了地址自然是另一条）。只删一个的话，另一个就成了
   * 占着空间、谁也不会再读的孤儿。
   */
  const dropOvBytes = async (id, old) => {
    await removeKey(overrideKey(id), OVERRIDE_CACHE)
    if (old?.kind === 'url' && old.url) await removeKey(old.url, OVERRIDE_CACHE)
  }

  const removeOv = async (id) => {
    const old = overrideOf(id)
    const title = titleOf(id)
    // 本机文件那种移除了就得重新去相册里找，先问一句；直链移除了随时能再贴回来，不问。
    if (old?.kind === 'file' && !confirmDanger(`「${title}」改回用服务端那段视频？复制进浏览器的那份文件会删掉。`)) return
    setOverride(id, null)
    await dropOvBytes(id, old)
    if (!isAlive()) return
    toast(`「${title}」改回用服务端那段视频`)
    await paintOvList()
    getPhotos().then(paintSelect).catch(() => {})
  }

  const saveOv = async () => {
    ovError('')
    const id = photoSel.value
    if (!id) return ovError('先挑一张照片')
    const title = titleOf(id)
    const old = overrideOf(id)
    ovSave.disabled = true
    try {
      if (ovKind === 'url') {
        let u
        try {
          u = new URL(urlIn.value.trim())
        } catch {
          return ovError('直链要是完整的地址，以 http:// 或 https:// 开头')
        }
        if (u.protocol !== 'http:' && u.protocol !== 'https:') return ovError('直链要是完整的地址，以 http:// 或 https:// 开头')
        await dropOvBytes(id, old)
        setOverride(id, { kind: 'url', url: u.href })
      } else {
        const f = fileIn.files?.[0]
        if (!f) return ovError('先选一段视频')
        const short = spaceShort(f.size, await storageEstimate())
        if (short && !confirmDanger(`这段视频 ${bytes(f.size)}，浏览器还给这个站点留了 ${bytes(short.left)}，多半存不下。仍然试试？`)) return
        ovBusy.textContent = `正在把 ${f.name} 复制进浏览器存储…`
        ovBusy.hidden = false
        await requestPersist()   // 失败无妨：只是让浏览器在空间紧张时别先清我们
        // 先写字节、写成了才记记录：反过来的话写满失败时留下一条「有来源、没字节」的记录。
        await putBlob(overrideKey(id), f, OVERRIDE_CACHE)
        if (old?.kind === 'url' && old.url) await removeKey(old.url, OVERRIDE_CACHE)
        setOverride(id, { kind: 'file', name: f.name, size: f.size, type: f.type, at: Date.now() })
      }
      if (!isAlive()) return
      toast(`已保存：「${title}」的视频改用${ovKind === 'url' ? '直链' : '本机文件'}`)
      urlIn.value = ''
      fileIn.value = ''
      fileName.textContent = '未选择'
      photoSel.value = ''
      await paintOvList()
      getPhotos().then(paintSelect).catch(() => {})
    } catch (e) {
      if (isAlive()) ovError(`没存成：${e.message}`)
    } finally {
      ovSave.disabled = false
      ovBusy.hidden = true
    }
  }

  const load = async () => {
    ovError('')
    try {
      const photos = await getPhotos()
      if (!isAlive()) return
      byId = new Map(photos.map((p) => [String(p.photoId ?? p.id), p]))
      paintSelect(photos)
    } catch (e) {
      if (!isAlive()) return
      photoSel.replaceChildren(h('option', { value: '', text: '取不到照片' }))
      ovError(`取照片列表失败：${e.message}（收起再展开会重试）`)
    }
    await paintOvList()
  }

  body.append(
    h('h3', { class: 'sub', text: '视频默认有声音' }),
    soundChips,
    h('p', { class: 'p dim', text: '浏览器不允许有声自动播放时会先静音播，点一下屏幕任意处就有声音' }),

    h('h3', { class: 'sub', text: '媒体数据源' }),
    h('label', { class: 'field', for: 'adv-base' }, h('span', { text: '地址（例如家里 NAS 的局域网地址）' }), baseIn),
    baseErr,
    onChips,
    baseNow,
    h('p', { class: 'p dim', text: '页面和相机仍走当前地址。' }),
    h('p', { class: 'p dim', text: '只有视频/原图的下载改走这个地址。' }),
    h('p', { class: 'p dim', text: '连不上会自动退回，不影响使用。' }),
    h('div', { class: 'actions' }, testBtn, button('保存', () => saveBase()), button('清除', () => clearBase(), { kind: 'ghost' })),
    probeLine,
    speedLine,
    hintBox,

    h('h3', { class: 'sub', text: '单个媒体的来源' }),
    ovList,
    ovEmpty,
    h('label', { class: 'field sel', for: 'adv-ov-photo' },
      h('span', { text: '添加：哪一张照片' }), photoSel, h('span', { class: 'sel-arrow', html: icon('back') })),
    kindChips,
    urlField,
    fileField,
    ovErr,
    ovBusy,
    h('div', { class: 'actions' }, ovSave),
    h('p', { class: 'p dim', text: '直链需要对方服务器允许跨域（CORS）' }),
    h('p', { class: 'p dim', text: '本机文件会复制一份进浏览器存储；Edge 安卓版只能播分片 MP4（用「存视频」存下来的那种）' }))

  let loaded = false
  const setOpen = (open) => {
    body.hidden = !open
    toggle.textContent = open ? '收起高级设置' : '展开高级设置'
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false')
    try {
      sessionStorage.setItem(ADV_OPEN_KEY, open ? '1' : '')
    } catch { /* 隐私模式：不记，下次进来还是收起的 */ }
    // 取失败了（photosP 被清成 null）就让下一次展开重试。
    if (open && (!loaded || !photosP)) { loaded = true; load() }
  }
  let wasOpen = false
  try {
    wasOpen = sessionStorage.getItem(ADV_OPEN_KEY) === '1'
  } catch { /* 同上 */ }
  setOpen(wasOpen)
  return sec
}

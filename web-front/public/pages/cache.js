/**
 * 本机缓存。**对应 Android 的 `CacheScreen`，但语义完全不同 —— 这一点必须说清。**
 *
 * ## Android 那一页管的是什么
 *
 * 它是「出门前准备一次」的页面：把照片的参考图与视频**下载到手机存储**，好在没网的
 * 现场也能识别；还要显示服务端预建的 ARCore 目标库装不装得上（装不上就退回端上现建，
 * 贴合差一档）。那些概念在网页版一个都不存在 —— 没有 ARCore，也没有我们自己管的文件。
 *
 * ## 网页版真正的三层缓存
 *
 * | 层 | 谁管 | 清掉的后果 |
 * |---|---|---|
 * | **识别库包**（PARL） | 我们（内存） | 刷新时重新下（几 MB，带 ETag） |
 * | **看过的视频** | 浏览器 HTTP 缓存 | 下次看那段要重新下 |
 * | **wasm 编译缓存** | 浏览器（instantiateStreaming 的 code cache） | 下次进页面要重新编译整个 12MB |
 *
 * 三层都**不是我们能精确查询的** —— `Cache-Control` 与 code cache 都没有可枚举的 API。
 * 所以这一页如实显示"能查到的那部分"，并明确说清哪些查不到。**编一个数字比不给更糟**：
 * 用户会据此判断"是不是缓存坏了"。
 *
 * ## 「本机缓存的组」：我们自己管的那一层，能逐组列
 *
 * 视频与原图进的是我们自己的 Cache Storage（mediastore.js），能精确到每一组：标题、多大、
 * 固定还是自动、移除。三个整体动作的口径：
 *
 * | 按钮 | 删什么 | 不删什么 |
 * |---|---|---|
 * | 全部缓存 | —（与媒体页同一个批次，`cacheall.js`） | |
 * | 清空自动缓存 | 视频缓存里**不在**「固定 ∪ 正在下」里的 | 固定的组、正在下的 |
 * | 清空全部 | 视频、缩略图、原图、单个来源的本机文件，以及固定记录 | 识别引擎的编译缓存（下面单独一节） |
 *
 * 「清空自动缓存」留下什么，用的是预取清理的同一个口径（`prefetch.keepSet`）：两处各算
 * 一遍的话，迟早这边留下的那一段下一轮就被那边删了，或者反过来。
 */
import * as api from '../api.js'
import { bytes, button, confirmDanger, h, row, section, toast } from '../ui.js'
import { clearWasmCache } from '../recognize/wasmcache.js'
import { budget, clearPrefetched, keepSet, prefetchStatus, prefetchedCount, staleKeys } from '../prefetch.js'
import { bulkState, onBulkChange, startCacheAll, stopCacheAll } from '../cacheall.js'
import { bulkResultText, groupTag, listedGroups, summaryText, cacheSummary } from '../cachelabel.js'
import {
  OVERRIDE_CACHE, Priority, REF_CACHE, VIDEO_CACHE,
  activeJobs, cachedKeySet, clearCache, groupStates, onChange, pinnedIds, removeGroup, removeKey, unpin,
} from '../mediastore.js'
import { advanced, setOverride } from '../prefs.js'

export default {
  title: '本机缓存',

  async mount(el, ctx) {
    let alive = true
    const lib = ctx.libInfo?.()

    el.appendChild(section('识别库',
      row('这个账号可扫', lib ? `${lib.nPhotos} 张` : '还没加载'),
      lib?.skipped?.length
        ? row('不在识别库里', `${lib.skipped.length} 张`, { bad: true })
        : null,
      lib ? row('粗排词表', lib.hasVocab ? '有' : '无（全量扫描）') : null,
      h('p', { class: 'p dim', text: '库包在进页面时取一次（带 ETag，没变就 304）。它只含被授权给你的那些照片的特征 —— 不含照片本身。' }),
      lib?.skipped?.length
        ? h('p', { class: 'warnbox', text: '有照片在库里找不到特征：入库时被质量闸门或去重闸门拒了。那些照片扫任何角度都不会有反应，要管理员处理。' })
        : null))

    // ── 预取的视频 ──────────────────────────────────────────────────
    // 登录后后台拉的那些（prefetch.js）。这一层**是我们自己管的**，能精确报数 ——
    // 与下面「浏览器给的存储配额」那一节形成对照：那一节只能报整个源的总量。
    const pre = section('预取的媒体')
    el.appendChild(pre)
    const renderPrefetch = async () => {
      pre.body.innerHTML = ''
      const st = prefetchStatus()
      pre.body.appendChild(row('状态', st.state))
      const n = await prefetchedCount()
      if (!alive) return
      pre.body.appendChild(row('视频', `${n} 段`, { mono: true }))
      if (st.thumbs) pre.body.appendChild(row('缩略图', `${st.thumbs} 张`, { mono: true }))
      // 预算与已用分两行。**这一对是这一页新的主角**：它回答"为什么我的视频没全下下来"，
      // 而那是按空间记账之后用户唯一会问的问题（上一版按张数切，问题是"为什么只有 8 段"）。
      const limit = st.budget || await budget()
      if (!alive) return
      pre.body.appendChild(row('这次用掉', bytes(st.bytes), { mono: true }))
      pre.body.appendChild(row('预算上限', bytes(limit), { mono: true }))
      if (st.tooBig) {
        pre.body.appendChild(row('超预算跳过', `${st.tooBig} 段`, { mono: true, bad: true }))
      }
      pre.body.appendChild(h('p', { class: 'p dim', text: '扫到这些照片时视频直接从本机播，不走网络。按入库时间从新到旧装，装满预算为止 —— 新入库的会把最老的挤出去。' }))
      pre.body.appendChild(h('p', { class: 'p dim', text: '预算是浏览器给这个站点的配额的四分之一（钳在 64MB 到 1GB 之间）。两种角色同一套。' }))
      // 这里原来有一个「清空预取」按钮，删掉了：它调 `clearPrefetched()` 把整个视频缓存连同
      // 用户点过「缓存」的固定组一起删，也不先停掉正在跑的下载（下完又写回去）。「只清
      // 预取来的」这件事由下面木牌里的「清空自动缓存」做（固定的、正在下的都不动），真要
      // 全清走「清空全部」—— 两个都比它说得清楚，没必要留第三个口径不一样的按钮。
    }
    await renderPrefetch()
    if (!alive) return

    const offGroups = mountGroups(el, { isAlive: () => alive, afterClear: () => renderPrefetch() })

    const storage = section('浏览器给的存储配额')
    el.appendChild(storage)
    // `navigator.storage.estimate()` 是唯一能问到的数，而它是**整个源**的用量
    // （含 HTTP 缓存、IndexedDB、Cache Storage），拆不开。所以只报总量并说明。
    if (navigator.storage?.estimate) {
      try {
        const est = await navigator.storage.estimate()
        if (!alive) return
        storage.body.appendChild(row('已用', bytes(est.usage ?? 0), { mono: true }))
        storage.body.appendChild(row('上限', bytes(est.quota ?? 0), { mono: true }))
        storage.body.appendChild(h('p', { class: 'p dim', text: '这是整个站点的总量（识别引擎、看过的视频、编译缓存都在里面），浏览器不提供拆分。' }))
      } catch {
        storage.body.appendChild(row('已用', '问不到'))
      }
    } else {
      storage.body.appendChild(h('p', { class: 'p dim', text: '这个浏览器不提供存储用量查询。' }))
    }

    const engineErr = h('p', { class: 'warnbox', hidden: true })
    el.appendChild(section('识别引擎',
      h('p', { class: 'p', text: '引擎是一个 12MB 的 wasm。第一次进页面要下载 + 编译，之后浏览器用它自己的编译缓存直接加载。' }),
      h('p', { class: 'p dim', text: '进页面时顶部那条进度会说明走的是哪条：「正在下载」还是「从缓存读取」。' }),
      h('div', { class: 'actions' },
        button('清掉引擎的编译缓存', async () => {
          // 这条只清我们自己那份 IndexedDB 兜底（见 wasmcache.js）。浏览器原生的
          // code cache 没有清除 API —— 如实说出来，而不是让用户以为点了就干净了。
          engineErr.hidden = true
          try {
            await clearWasmCache()
            toast('已清掉我们那份兜底缓存')
          } catch (e) {
            // 失败留在页面上（不用 toast）—— 那会自己消失，而这是用户唯一的线索。
            engineErr.hidden = false
            engineErr.textContent = `清理没成：${e.message}`
          }
        }, { kind: 'ghost' })),
      engineErr,
      h('p', { class: 'p dim', text: '浏览器原生的 wasm 编译缓存没有清除 API。真要彻底清，用浏览器设置里的「清除站点数据」。' })))

    el.appendChild(section('彻底清空',
      h('p', { class: 'p', text: '识别库、看过的视频、编译缓存、登录状态 —— 全部清掉并重新开始。' }),
      h('div', { class: 'actions' },
        button('清空并重载', async () => {
          const files = fileOverrideIds()
          const fileNote = files.length
            ? `\n\n设置里 ${files.length} 条「本机文件」单个来源也会一起删掉，要用得重新选。`
            : ''
          if (!globalThis.confirm(`清空本站的全部缓存并重载？会退出登录。${fileNote}`)) return
          // 三样都清：Cache Storage、IndexedDB、以及登录。**顺序不重要，但都要尝试** ——
          // 任何一样失败都不该阻止其余的。
          try {
            for (const k of await caches.keys()) await caches.delete(k)
          } catch { /* 有的浏览器在非安全上下文下没有 caches */ }
          try {
            await clearWasmCache()
          } catch { /* 见上 */ }
          // 上面那一轮把 `photoar-override-v1`（本机文件的字节）也删了。记录留着的话，重载后
          // 扫到那张会报「本机文件不在了」—— 与「清空全部」同一个处理：只删 `kind:'file'`
          // 的记录（它的内容已经没了），直链记录保留（内容在对方服务器上，删缓存不影响它）。
          for (const id of files) setOverride(id, null)
          location.replace(location.pathname)
        }, { kind: 'danger', iconName: 'trash' }))))

    return () => { alive = false; offGroups() }
  },
}

/**
 * 设置里「本机文件」那一类单个来源的照片 id。「清空全部」与「清空并重载」共用：两处
 * 都会删掉 `photoar-override-v1`（那几份文件的字节），就都得连记录一起删，口径只写一份。
 */
function fileOverrideIds() {
  return Object.entries(advanced().overrides).filter(([, ov]) => ov?.kind === 'file').map(([id]) => id)
}

/**
 * 「本机缓存的组」木牌。返回卸载函数（退订 + 停计时器）。
 *
 * 行是**按 id 就地更新**的，不是每次整块重建：下载中每 250ms 刷一次，整块重建的话
 * 用户按「移除」按到一半那一行被换掉，这一下就丢了。
 */
function mountGroups(el, { isAlive, afterClear }) {
  const sec = section('本机缓存的组')
  el.appendChild(sec)
  const sumLine = h('p', { class: 'p', text: '正在取照片…' })
  const bulkLine = h('p', { class: 'p dim', hidden: true })
  const list = h('div', { class: 'entries' })
  const none = h('p', { class: 'p dim', hidden: true, text: '本机还没有缓存任何一组。去「媒体库」点卡片上的「缓存」，或者点下面的「全部缓存」。' })
  const note = h('p', { class: 'p bad', hidden: true })
  const bulkBtn = h('button', { onclick: () => onBulk() }, '全部缓存')
  const autoBtn = button('清空自动缓存', () => clearAuto(), { kind: 'ghost' })
  const allBtn = button('清空全部', () => clearAll(), { kind: 'danger', iconName: 'trash' })
  sec.body.append(sumLine, bulkLine, list, none,
    h('div', { class: 'actions' }, bulkBtn, autoBtn, allBtn),
    note,
    h('p', { class: 'p dim', text: '固定 = 你点过「缓存」的那些，预取的清理不会动它们；自动 = 预取或看过时顺手存下的，空间紧张时会被新入库的挤掉。' }))

  /** 这个账号能看的照片（`api.photos()`）；null = 还没取到。 */
  let photos = null
  let states = null
  /** photoId → { el, meta, btn }。 */
  const rows = new Map()
  const busy = new Set()
  const alive = isAlive

  const fail = (msg) => {
    // 失败留在木牌上（不用 toast）：它会自己消失，而这是用户唯一能拿去问人的线索。
    note.textContent = msg
    note.hidden = false
  }

  /** 上一批的结果只在看着它跑完的页面上说（理由见 photos.js 同名变量）。 */
  let watched = bulkState().running
  const paintBulk = () => {
    const b = bulkState()
    if (b.running) {
      bulkBtn.className = 'ghost'
      bulkBtn.disabled = b.stopping
      bulkBtn.textContent = b.stopping ? '正在停…' : '停止'
      bulkLine.hidden = false
      bulkLine.textContent = b.stopping
        ? `这一组下完就停（第 ${b.finished + 1} / ${b.total} 组）`
        : `正在缓存第 ${b.finished + 1} / ${b.total} 组…`
    } else {
      bulkBtn.className = ''
      bulkBtn.textContent = '全部缓存'
      bulkBtn.disabled = !states || !cacheSummary(photos, states).todo.length
      const t = watched ? bulkResultText(b.last) : ''
      bulkLine.hidden = !t
      bulkLine.textContent = t
    }
  }

  const rowFor = (p) => {
    const id = String(p.photoId ?? p.id)
    const title = p.title || '（未命名）'
    const meta = h('span', { class: 'entry-m mono' })
    const btn = button('移除', () => removeOne(p, title), { kind: 'ghost' })
    btn.setAttribute('aria-label', `从本机移除「${title}」`)
    const line = h('div', { class: 'entry' },
      h('div', { class: 'entry-b' }, h('span', { class: 'entry-t', text: title }), meta),
      btn)
    const r = { el: line, meta, btn }
    rows.set(id, r)
    return r
  }

  const paint = () => {
    if (!alive() || !photos || !states) return
    sumLine.textContent = summaryText(cacheSummary(photos, states))
    const listed = listedGroups(photos, states)
    const want = new Set(listed.map((p) => String(p.photoId ?? p.id)))
    for (const [id, r] of rows) if (!want.has(id)) { r.el.remove(); rows.delete(id) }
    // 顺序照 photos 的（稳定），所以已有的行相对顺序本来就对：只把新出现的插到前一行后面，
    // 不挪已有的（挪动正被按着的那一行同样会丢掉那一下）。
    let prev = null
    for (const p of listed) {
      const id = String(p.photoId ?? p.id)
      let r = rows.get(id)
      if (!r) {
        r = rowFor(p)
        if (prev) prev.after(r.el)
        else list.prepend(r.el)
      }
      prev = r.el
      const st = states.get(id)
      const size = Number(st?.bytes) > 0 ? bytes(st.bytes) : '大小未知'
      const tag = groupTag(st)
      r.meta.replaceChildren(`${size} · `, h('span', { class: st?.state === 'cached' && st.pinned ? 'ok' : (st?.state === 'none' ? 'bad' : ''), text: tag }))
      r.btn.disabled = busy.has(id)
    }
    none.hidden = listed.length > 0
    paintBulk()
  }

  let refreshing = false
  let again = false
  const refresh = async () => {
    if (!photos) return
    if (refreshing) { again = true; return }
    refreshing = true
    try {
      const m = await groupStates(photos)
      if (!alive()) return
      states = m
      paint()
    } catch (e) {
      if (alive()) fail(`查不到本机缓存：${e.message}`)
    } finally {
      refreshing = false
      if (again && alive()) { again = false; refresh() }
    }
  }
  let timer = 0
  const schedule = () => {
    if (timer || !alive()) return
    timer = setTimeout(() => { timer = 0; refresh() }, 250)
  }
  const offStore = onChange((evt) => {
    if (evt.type === 'cache' && evt.cacheName !== VIDEO_CACHE) return
    if (evt.type === 'job' && !String(evt.snap?.key ?? '').startsWith('/v1/asset/')) return
    schedule()
  })
  const offBulk = onBulkChange((b) => { if (b.running) watched = true; if (alive()) paintBulk() })

  const load = async () => {
    note.hidden = true
    sumLine.textContent = '正在取照片…'
    try {
      photos = await api.photos()
    } catch (e) {
      if (!alive()) return
      sumLine.textContent = '取不到照片列表，列不出本机缓存了哪些组。'
      fail(`取照片失败：${e.message}`)
      return
    }
    await refresh()
  }

  async function onBulk() {
    if (bulkState().running) return stopCacheAll()
    if (!photos || !states) return
    note.hidden = true
    const r = await startCacheAll(photos, states)
    if (!alive() || !r) return
    if (r.errors.length && !r.full) {
      const e0 = r.errors[0]
      fail(`「${e0.title}」没缓存成：${e0.error}${r.errors.length > 1 ? `（另有 ${r.errors.length - 1} 组也没成）` : ''}`)
    }
    toast(bulkResultText(r))
  }

  async function removeOne(p, title) {
    const id = String(p.photoId ?? p.id)
    if (!confirmDanger(`从本机移除「${title}」的缓存？`)) return
    busy.add(id)
    paint()
    try {
      await removeGroup(p)
      if (alive()) toast('已从本机移除')
    } catch (e) {
      if (alive()) fail(`「${title}」没移除成：${e.message}`)
    } finally {
      busy.delete(id)
      refresh()
    }
  }

  async function clearAuto() {
    note.hidden = true
    if (!photos) return
    // 口径与预取的清理同一个（keepSet）：固定的组（且当前账号还看得到）+ 正在下的，其余都是「自动」。
    const keep = keepSet({ photos, pinned: pinnedIds(), running: activeJobs().map((j) => j.key) })
    const drop = staleKeys([...await cachedKeySet(VIDEO_CACHE)], [...keep])
    if (!alive()) return
    if (!drop.length) return toast('没有自动缓存的视频')
    if (!confirmDanger(`清掉 ${drop.length} 段自动缓存的视频？你固定的那些不动；下次打开页面，后台预取还会按预算再存一些。`)) return
    for (const k of drop) await removeKey(k, VIDEO_CACHE)
    if (!alive()) return
    toast(`已清掉 ${drop.length} 段`)
    refresh()
    afterClear()
  }

  async function clearAll() {
    note.hidden = true
    const files = fileOverrideIds()
    const fileNote = files.length
      ? `\n\n设置里 ${files.length} 条「本机文件」单个来源也会一起删掉（复制进浏览器的那几份就在这里面），要用得重新选。`
      : ''
    if (!confirmDanger(`清空本机缓存的全部视频、原图、缩略图？固定记录也一并清掉，下次要用得重新缓存。${fileNote}`)) return
    // 先让还在跑的停下：否则它们下完会把刚清掉的又写回去。正在播的那段不动（同 removeGroup）。
    stopCacheAll()
    const running = activeJobs().filter((j) => j.priority !== Priority.PLAY)
    for (const j of running) j.abort()
    await Promise.all(running.map((j) => j.done))
    await clearPrefetched()
    await clearCache(REF_CACHE)
    await clearCache(OVERRIDE_CACHE)
    for (const id of pinnedIds()) unpin(id)
    // 本机文件那几条的字节刚刚随 OVERRIDE_CACHE 一起没了。记录留着的话，扫到那张会报
    // 「本机文件不在了」而不是播服务端那段 —— 用户已经说了「全部清掉」，就连记录一起清。
    for (const id of files) setOverride(id, null)
    if (!alive()) return
    toast('已清空')
    refresh()
    afterClear()
  }

  load()
  return () => { offStore(); offBulk(); clearTimeout(timer) }
}

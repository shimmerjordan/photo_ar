/**
 * 媒体库：**照片 + 它配的那段视频**。
 *
 * 一屏网格，每张显示缩略图 + 标题 + 两个警示徽标（无视频 / 参考图变了）+ 一个「缓存」按钮。
 * 那两个徽标不是装饰：它们各自对应一种「扫了不会有反应」的状态，而那是用户最可能
 * 来这一页查的事。
 *
 * ## 这一页对两种角色是不同的东西
 *
 * 网格本身共用（`/v1/photos` 服务端就按授权过滤，宾客拿到的行数天然是对的），
 * 差别只有两处，都在这个文件里显式分叉：
 *
 * | | 管理员 | 宾客 |
 * |---|---|---|
 * | 点一张进哪 | `DETAIL`（NAS 路径、自匹配、删除） | `VIEW`（大图、视频、存到手机） |
 * | 空了说什么 | 去「素材」页传 | 去找管理员要授权 |
 * | 徽标 | 全给 | 只给「无视频」 |
 *
 * 徽标那一条值得说明：「参考图变了」是一条**给策展人看**的运维状态（它意味着要重新
 * 入库），宾客对它无能为力，摆在他面前只会让他以为自己的照片坏了。「无视频」相反 ——
 * 它解释了"为什么我扫了没反应"，那是宾客真会问的问题。
 *
 * ## 缓存：两种角色都有，宾客最需要
 *
 * 宾客在进门等位时打开这一页，把他能扫的那几组（原图 + 视频）存进这个浏览器，到了
 * 挤满人的现场扫到就秒开。所以每张卡片一个「缓存」按钮、顶上一块「本机缓存」木牌
 * （已缓存几组、占多少、「全部缓存」、「管理缓存」）。
 *
 * - 卡片是 `div.card` 里两个并排的按钮（导航 + 缓存）：**按钮不能嵌套**，把缓存按钮塞进
 *   原来那个整张卡片的 `<button>` 里，点缓存会同时触发导航。
 * - 缓存按钮写什么、点了做什么全由 `cachelabel.cacheButtonLabel` 定（宾客页同一张表）。
 * - 状态**订阅** mediastore（`onChange`），而不是点完自己改：同一段视频可能正被预取、
 *   宾客页的播放器、「全部缓存」同时要（它们挂在同一个下载任务上），谁动了都该跟着变。
 *   下载中每收一块就有一个事件，节流到 250ms 再整屏问一次 `groupStates`（一次翻缓存键）。
 * - 「全部缓存」是 `cacheall.js` 的单例：离开这一页不停，缓存页那边看到的是同一批。
 */
import * as api from '../api.js'
import { bulkState, onBulkChange, precheckSingle, startCacheAll, stopCacheAll } from '../cacheall.js'
import { bulkResultText, cacheButtonLabel, cacheSummary, summaryText } from '../cachelabel.js'
import { VIDEO_CACHE, cacheGroup, groupStates, onChange, pin, removeGroup } from '../mediastore.js'
import { button, confirmDanger, empty, failed, h, loading, section, thumb, toast } from '../ui.js'
import { Page } from '../navpolicy.js'

/** 按钮只有两三个字；读屏与长按提示（`title`）要把「点了会怎样」说全。 */
const ACTION_HINT = {
  cache: (t) => `把「${t}」的原图和视频缓存到本机`,
  pin: (t) => `「${t}」是自动缓存的，点一下固定到本机`,
  remove: (t) => `「${t}」已缓存，点一下从本机移除`,
}

export default {
  title: '媒体库',

  async mount(el, ctx) {
    let alive = true
    const isAdmin = ctx.isAdmin()
    /** 卸载时要退订的东西（onChange / onBulkChange / 节流计时器）。重画网格时也先清一遍。 */
    let offs = []
    const cleanup = () => { for (const f of offs) f(); offs = [] }
    el.appendChild(loading('正在取照片…'))

    const render = (list) => {
      if (!alive) return
      cleanup()
      el.innerHTML = ''
      if (!list.length) {
        el.appendChild(isAdmin
          ? empty('媒体库是空的', '去「素材」页挑一张打印过的照片 + 一段视频，一次传完就是一组映射。')
          // 宾客这一句必须给出**他能做的下一步**，而"去传一张"不是（他没有那个页签，
          // 服务端也会 403）。他能做的只有一件事：找管理员。
          : empty('还没有照片给你', '管理员还没有把照片授权给你。找他要一下，之后刷新这一页就能看到。'))
        el.appendChild(button('刷新', load, { kind: 'ghost', iconName: 'refresh' }))
        return
      }

      /** photoId → groupStates 的一项。null = 还没查出来。 */
      let states = null
      /** 点下去到下载任务建起来之间（要先申请持久存储、查一遍缓存）别让人再点同一张。 */
      const busy = new Set()
      /** photoId → { btn, photo, title }：局部重画只改按钮，不重建整张网格（缩略图会闪）。 */
      const cards = new Map()

      // ── 「本机缓存」木牌 ─────────────────────────────────────────────
      const sumLine = h('p', { class: 'p', text: '正在查本机缓存…' })
      const bulkLine = h('p', { class: 'p dim', hidden: true })
      const note = h('p', { class: 'p bad', hidden: true })
      const bulkBtn = h('button', { onclick: () => onBulk() })
      const panel = section('本机缓存',
        sumLine,
        bulkLine,
        h('div', { class: 'actions' },
          bulkBtn,
          button('管理缓存', () => ctx.shell.push(Page.CACHE), { kind: 'ghost', iconName: 'cache' })),
        note)
      el.appendChild(panel)

      const fail = (msg) => {
        // 失败留在木牌上（不用 toast）：它会自己消失，而这是用户唯一能拿去问人的线索。
        note.textContent = msg
        note.hidden = false
      }

      /**
       * 这一页有没有看着一批跑过。上一批的结果（「缓存好 2 组」）只在看着它跑完的页面上说：
       * 在媒体页跑完、之后又删了几组再进来，那一句还挂着的话就和上面的「已缓存 N / M」对不上。
       */
      let watched = bulkState().running
      const paintBulk = () => {
        const b = bulkState()
        const sum = states ? cacheSummary(list, states) : null
        if (b.running) {
          bulkBtn.className = 'ghost'
          bulkBtn.disabled = b.stopping
          bulkBtn.textContent = b.stopping ? '正在停…' : '停止'
          bulkLine.hidden = false
          bulkLine.textContent = b.stopping
            ? `这一组下完就停（第 ${b.finished + 1} / ${b.total} 组）`
            : `正在缓存第 ${b.finished + 1} / ${b.total} 组…（离开这一页也会接着下）`
        } else {
          bulkBtn.className = ''
          bulkBtn.textContent = '全部缓存'
          bulkBtn.disabled = !sum || !sum.todo.length
          const t = watched ? bulkResultText(b.last) : ''
          bulkLine.hidden = !t
          bulkLine.textContent = t
        }
      }

      const paint = () => {
        if (!alive) return
        if (states) sumLine.textContent = summaryText(cacheSummary(list, states))
        for (const [id, c] of cards) {
          const st = states?.get(id) ?? null
          const lab = cacheButtonLabel(st)
          c.btn.textContent = lab.text
          // `.dl`（下载中）单独一个 class，不塞进 `lab.kind`：它只管「disabled 时要不要压灰」
          // 这一件事（fix1 Finding 6），跟 kind 决定的文字颜色档（ok / off）是两回事。
          const dl = st?.state === 'downloading' ? ' dl' : ''
          c.btn.className = `ghost card-cache${lab.kind ? ` ${lab.kind}` : ''}${dl}`
          // 状态还没查出来时也不可点：那一刻按钮写的「缓存」只是占位。
          c.btn.disabled = !states || busy.has(id) || !lab.action
          const hint = lab.action ? ACTION_HINT[lab.action](c.title) : `「${c.title}」：${lab.text}`
          c.btn.title = hint
          c.btn.setAttribute('aria-label', hint)
        }
        paintBulk()
      }

      let refreshing = false
      let again = false
      const refresh = async () => {
        if (refreshing) { again = true; return }
        refreshing = true
        try {
          const m = await groupStates(list)
          if (!alive) return
          states = m
          paint()
        } catch (e) {
          if (alive) fail(`查不到本机缓存：${e.message}`)
        } finally {
          refreshing = false
          if (again && alive) { again = false; refresh() }
        }
      }
      let timer = 0
      const schedule = () => {
        if (timer || !alive) return
        timer = setTimeout(() => { timer = 0; refresh() }, 250)
      }
      offs.push(() => { clearTimeout(timer); timer = 0 })
      offs.push(onChange((evt) => {
        // 按视频判状态（groupStates 的口径）：缩略图、原图、单个来源那几个缓存的变化不影响这一屏。
        if (evt.type === 'cache' && evt.cacheName !== VIDEO_CACHE) return
        if (evt.type === 'job' && !String(evt.snap?.key ?? '').startsWith('/v1/asset/')) return
        schedule()
      }))
      offs.push(onBulkChange((b) => { if (b.running) watched = true; if (alive) paintBulk() }))

      const onBulk = async () => {
        if (bulkState().running) return stopCacheAll()
        if (!states) return
        note.hidden = true
        // 预检（剩余空间不够要先问）与开跑都在 cacheall 里：缓存页的同名按钮走同一条。
        const r = await startCacheAll(list, states)
        if (!alive || !r) return
        if (r.errors.length && !r.full) {
          const e0 = r.errors[0]
          fail(`「${e0.title}」没缓存成：${e0.error}${r.errors.length > 1 ? `（另有 ${r.errors.length - 1} 组也没成）` : ''}`)
        }
        toast(bulkResultText(r))
      }

      const onCard = async (photo, title) => {
        const id = String(photo.photoId ?? photo.id)
        const { action } = cacheButtonLabel(states?.get(id))
        note.hidden = true
        if (action === 'pin') {
          // 已经在本机了，只差一个"别自动删它"的记号 —— 不重下。
          pin(id)
          toast('已固定到本机，不会被自动清掉')
          return
        }
        if (action === 'remove' && !confirmDanger(`从本机移除「${title}」的缓存？`)) return
        // 空间预检（fix1 Finding 3）：与「全部缓存」同一套 precheckSingle，别在这里再写一遍。
        // 放在 busy.add 之前 —— 用户在确认框里说了不的话，按钮不该先闪一下「忙」。
        if (action === 'cache' && !(await precheckSingle(photo, states?.get(id)))) return
        if (action !== 'cache' && action !== 'remove') return
        busy.add(id)
        paint()
        try {
          if (action === 'cache') {
            const r = await cacheGroup(photo)
            if (!alive) return
            if (r.ok) toast(`「${title}」已缓存到本机`)
            else fail(`「${title}」没缓存成：${r.error}`)
          } else {
            await removeGroup(photo)
            if (alive) toast('已从本机移除')
          }
        } catch (e) {
          if (alive) fail(`「${title}」${action === 'cache' ? '没缓存成' : '没移除成'}：${e.message}`)
        } finally {
          busy.delete(id)
          if (alive) refresh()
        }
      }

      // ── 网格 ─────────────────────────────────────────────────────────
      const grid = h('div', { class: 'grid' })
      for (const p of list) {
        const id = String(p.photoId ?? p.id)
        const title = p.title || '（未命名）'
        const flags = []
        // 这两条与 Android 的同一批文案。它们回答「为什么扫了没反应」——
        // 而那是这一页存在的主要理由之一。
        if (p.hasVideo === false) flags.push(h('span', { class: 'badge', text: '无视频' }))
        // 「参考图变了」只给管理员：宾客对它无能为力（要重新入库），
        // 摆在他面前只会让他以为自己的照片坏了。
        if (isAdmin && p.refStale) flags.push(h('span', { class: 'badge warn', text: '参考图变了' }))
        // 「较难扫」两种角色都给：宾客也该知道"这张可能扫不出"。
        if (p.stars <= 2) flags.push(h('span', { class: 'badge warn', text: '较难扫' }))
        const btn = h('button', { class: 'ghost card-cache', type: 'button', disabled: true, onclick: () => onCard(p, title) }, '缓存')
        cards.set(id, { btn, photo: p, title })
        grid.appendChild(h('div', { class: 'card' },
          h('button', {
            class: 'card-main', type: 'button', onclick: () => ctx.shell.push(isAdmin ? Page.DETAIL : Page.VIEW, { id }),
          },
            thumb(id, ctx.shell.libraryRev, p.title ?? '照片'),
            h('div', { class: 'card-b' },
              h('span', { class: 'card-t', text: title }),
              flags.length ? h('span', { class: 'flags' }, ...flags) : null)),
          btn))
      }
      el.appendChild(grid)
      el.appendChild(h('p', { class: 'note', text: `共 ${list.length} 张` }))
      paint()
      refresh()
    }

    const load = async () => {
      cleanup()
      el.innerHTML = ''
      el.appendChild(loading('正在取照片…'))
      try {
        render(await api.photos())
      } catch (e) {
        if (!alive) return
        el.innerHTML = ''
        el.appendChild(failed(e.message, load))
      }
    }
    await load()

    return () => { alive = false; cleanup() }
  },
}

/**
 * 外壳：路由栈、底栏、页面生命周期。**Android `Shell` 的对译。**
 *
 * ## 栈式导航，不是 hash 里塞一堆状态
 *
 * 与 Android 一样是一个**栈**：根之间没有返回关系（切根清空栈），从根推进去的页面
 * （详情、试播、历史、缓存）可以 pop 回来。hash 只反映**栈顶**，为的是浏览器的返回键
 * 和「发个链接给人」都成立。
 *
 * ## 页面的契约
 *
 * 每个页面导出 `{ title, mount(el, ctx) }`，`mount` 返回一个**卸载函数**。
 *
 * 卸载函数不是可选的礼节：扫描页持有相机流、Worker 和 rAF 循环，切页不释放的话
 * 相机灯一直亮、Worker 继续吃 CPU、电量哗哗掉 —— 而用户以为自己已经离开那一页了。
 * 所以这里的 `mount` 返回值被**当作必需**处理（没返回就在控制台点名）。
 */
import { Page, TAB_META, Tab, isRoot, landingTab, needsAdmin, tabAfterRoleChange, tabsFor } from './navpolicy.js'
import { icon } from './pixelicons.js'

const PAGES = {
  [Tab.SCAN]: () => import('./pages/scan.js'),
  [Tab.PHOTOS]: () => import('./pages/photos.js'),
  [Tab.MEDIA]: () => import('./pages/media.js'),
  [Tab.ADMIN]: () => import('./pages/admin.js'),
  [Tab.SETTINGS]: () => import('./pages/settings.js'),
  [Page.DETAIL]: () => import('./pages/detail.js'),
  [Page.PLAY]: () => import('./pages/play.js'),
  [Page.HISTORY]: () => import('./pages/history.js'),
  [Page.CACHE]: () => import('./pages/cache.js'),
}

export class Shell {
  /**
   * @param els `{topbar, title, back, view, tabbar}`
   * @param ctx 传给每个页面的上下文：`{me, isAdmin, go, back, toast, libraryRev, ...}`
   */
  constructor(els, ctx) {
    this.els = els
    this.ctx = ctx
    /** 栈。每项 `{name, params}`。 */
    this.stack = [{ name: landingTab(), params: {} }]
    this._teardown = null
    this._mountSeq = 0
    /**
     * 照片库的版本号。入库 / 换参考图 / 删除之后 +1，列表和详情跟着它重取。
     * 与 Android 的 `libraryRev` 同一个用途 —— 那边还顺手清了缩略图缓存，这里由
     * 各页面自己在 URL 上加 `?rev=` 处理（缩略图 URL 不变而内容变了）。
     */
    this.libraryRev = 0

    addEventListener('hashchange', () => this._onHash())
    this.els.back.addEventListener('click', () => this.pop())
  }

  get current() {
    return this.stack[this.stack.length - 1]
  }

  /** 当前所在的根，用来点亮底栏。 */
  get currentRoot() {
    return this.stack[0].name
  }

  /** 推一页（详情、试播…）。 */
  push(name, params = {}) {
    this.stack.push({ name, params })
    this._syncHash()
    return this._render()
  }

  /** @returns false 表示已经在根上（该让浏览器处理返回）。 */
  pop() {
    if (this.stack.length <= 1) return false
    this.stack.pop()
    this._syncHash()
    this._render()
    return true
  }

  /** 切根。清空栈 —— 根之间没有返回关系。 */
  tab(name) {
    if (this.stack.length === 1 && this.stack[0].name === name) return
    this.stack = [{ name, params: {} }]
    this._syncHash()
    this._render()
  }

  /** 库变了：列表与详情要重取。 */
  libraryChanged() {
    this.libraryRev++
    this._render()
  }

  /** 角色变了（换人登录）之后把栈收回一个还能待的页签。 */
  roleChanged(isAdmin) {
    const next = tabAfterRoleChange(this.currentRoot, isAdmin)
    this.stack = [{ name: next, params: {} }]
    this._syncHash()
    this.renderTabs(isAdmin)
    this._render()
  }

  /** 底栏。按角色渲染 —— 这是 `NavPolicy` 的全部可见结果。 */
  renderTabs(isAdmin) {
    const tabs = tabsFor(isAdmin)
    this.els.tabbar.innerHTML = ''
    for (const t of tabs) {
      const meta = TAB_META[t]
      const b = document.createElement('button')
      b.type = 'button'
      b.className = 'tab'
      b.dataset.tab = t
      // 图标 + 文字都给：Android 那边有一条测试盯着"没有两张图标是一样的"，因为
      // 改造前「扫一扫」和「照片」用了同一个 Home 图标，两个页签只能靠文字区分。
      b.innerHTML = `${icon(meta.icon)}<span>${meta.label}</span>`
      b.addEventListener('click', () => this.tab(t))
      this.els.tabbar.appendChild(b)
    }
    this._paintActive()
  }

  _paintActive() {
    for (const b of this.els.tabbar.querySelectorAll('.tab')) {
      b.classList.toggle('on', b.dataset.tab === this.currentRoot)
      b.setAttribute('aria-current', b.dataset.tab === this.currentRoot ? 'page' : 'false')
    }
  }

  /** hash → 栈。用户手打地址或按返回键时走这条。 */
  _onHash() {
    const parsed = parseHash(location.hash)
    if (!parsed) return
    const top = this.current
    if (top.name === parsed.name && sameParams(top.params, parsed.params)) return
    // 返回键：如果目标就在栈里（而且不是栈顶），弹到它。否则当成一次新的推进/切根。
    const at = this.stack.findIndex((s) => s.name === parsed.name && sameParams(s.params, parsed.params))
    if (at >= 0) {
      this.stack = this.stack.slice(0, at + 1)
    } else if (isRoot(parsed.name)) {
      this.stack = [{ name: parsed.name, params: {} }]
    } else {
      this.stack.push(parsed)
    }
    this._render()
  }

  _syncHash() {
    const h = formatHash(this.current)
    if (location.hash !== h) {
      // 用 pushState 而不是改 location.hash：后者会再触发一次 hashchange，
      // 于是 `_render` 跑两遍 —— 对扫描页来说那是一次多余的相机开关。
      history.pushState(null, '', h)
    }
  }

  async _render() {
    const seq = ++this._mountSeq
    const { name, params } = this.current

    // 卸载上一页。**先卸载再挂载**：扫描页和试播页都要用 `<video>` 与 GL 上下文，
    // 两个同时活着会抢相机（iOS 上第二次 getUserMedia 会让第一个 track 静默变 muted）。
    if (this._teardown) {
      try {
        this._teardown()
      } catch (e) {
        console.error('[shell] 卸载上一页出错', e)
      }
      this._teardown = null
    }
    this.els.view.innerHTML = ''

    // 越权直接送回落地页，而不是渲染一个必然 403 的页面。
    if (needsAdmin(name) && !this.ctx.isAdmin()) {
      this.stack = [{ name: landingTab(), params: {} }]
      this._syncHash()
      return this._render()
    }

    const loader = PAGES[name]
    if (!loader) {
      this.els.view.textContent = `没有这一页：${name}`
      return
    }

    const mod = (await loader()).default
    // 加载是异步的（动态 import）。期间用户可能又切了页 —— 那时这次挂载已经过时，
    // 必须丢掉。不判的话两页的 DOM 会同时插进去，而且旧那页的清理函数丢了。
    if (seq !== this._mountSeq) return

    this.els.title.textContent = mod.title ?? ''
    this.els.back.hidden = this.stack.length <= 1
    this.els.topbar.hidden = Boolean(mod.fullBleed)
    this.els.view.classList.toggle('full', Boolean(mod.fullBleed))
    this._paintActive()

    const teardown = await mod.mount(this.els.view, { ...this.ctx, params, shell: this })
    if (seq !== this._mountSeq) {
      // 挂载期间又切页了：立刻把刚建起来的东西拆掉。
      try {
        teardown?.()
      } catch { /* 已经在切走的路上，报错没有意义 */ }
      return
    }
    if (typeof teardown !== 'function') {
      // 不抛错 —— 少一个清理函数不该让页面打不开。但要点名，因为漏掉它的后果
      // （相机不停、Worker 不停）是"用电量莫名很大"，几乎不可能反推到这里。
      console.warn(`[shell] 页面 ${name} 的 mount() 没有返回卸载函数`)
    }
    this._teardown = typeof teardown === 'function' ? teardown : null
  }

  /** 第一次渲染。 */
  start() {
    const parsed = parseHash(location.hash)
    if (parsed && (!needsAdmin(parsed.name) || this.ctx.isAdmin())) {
      this.stack = isRoot(parsed.name)
        ? [{ name: parsed.name, params: {} }]
        : [{ name: landingTab(), params: {} }, parsed]
    }
    this._syncHash()
    return this._render()
  }
}

/** `#/photos` / `#/detail/<id>` → `{name, params}`。 */
export function parseHash(hash) {
  const raw = (hash || '').replace(/^#\/?/, '')
  if (!raw) return null
  const [name, ...rest] = raw.split('/')
  if (!name) return null
  const params = {}
  if (rest[0]) params.id = decodeURIComponent(rest[0])
  return { name, params }
}

export function formatHash({ name, params }) {
  return params?.id ? `#/${name}/${encodeURIComponent(params.id)}` : `#/${name}`
}

function sameParams(a, b) {
  return (a?.id ?? null) === (b?.id ?? null)
}

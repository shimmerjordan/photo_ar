/**
 * 页面内的调试日志。**手机上没有控制台，这是唯一能看到"卡在哪"的东西。**
 *
 * 与 Android 那边的 `DiagLog` 是同一个决定的两次实现，连折叠规则都一样 —— 而那条规则
 * 不是优化：贴不上时有些行每秒一条（渲染、识别、视频状态），不折叠的话十几行的窗口
 * 两秒就被它们填满，把「视频 error code=4」那种**只出现一次的关键行顶出去**，
 * 而那一行恰恰是唯一有信息量的。
 *
 * 三件事刻意做成这样：
 *
 * - **默认关**，`?diag=1` 或「设置 → 关于 → 连按版本号 7 下」打开。开着的时候才拼字符串
 *   （`log()` 在关闭时直接 return），因为打点密度是每帧一次。
 * - **有"复制"按钮**。这块日志的用途就是被发出来给人看，而手机上选中一段等宽小字
 *   几乎不可能。
 * - **不记完整 URL**（只记 host 和路径末段）。这块东西是要被截图发出去的，
 *   而 URL 里带着 tailnet 域名和 photoId。
 */

const MAX_LINES = 40

/**
 * 纯逻辑，node 可测。UI 无关。
 *
 * ## 两个区，以及为什么必须分开
 *
 * - **关键区**（`kind='key'`）：只出现一次的事件 —— 命中、视频错误、引擎就绪。
 *   **只增不挤**（超上限时也保留最近的 N 条），因为它们是唯一有信息量的行。
 * - **流区**（`kind='stream'`）：每帧/每次识别的高频行。**按模板聚合** ——
 *   把行里的数字抽掉当模板键，同模板的行合成一条，显示次数与每个数字的范围。
 *
 * 第一版只做「相邻相同行折叠成 ×N」，在真机上不够：`跟踪 ok 内点=23 10ms` 和
 * `跟踪 ok 内点=24 11ms` **文本不同**，所以一条都折不掉 —— 40 行窗口一秒就被刷满，
 * 用户根本来不及复制（他的原话："刷的太快我无法复制全"）。按模板聚合之后，那 47 行
 * 变成一行 `跟踪 ok 内点=# #ms 四角=有 光流存活=#  ×47  内点 12~27 · ms 10~144`。
 */
export class DiagLog {
  constructor(maxKey = 24, maxStream = 24) {
    this.maxKey = maxKey
    this.maxStream = maxStream
    this.keys = []          // {text, at}
    this.groups = new Map() // template → {tpl, count, firstAt, lastAt, mins:[], maxs:[]}
  }

  /**
   * @param kind `'key'` 进关键区（不聚合），`'stream'` 进流区（按模板聚合）。
   */
  push(text, nowMs, kind = 'stream') {
    if (kind === 'key') {
      this.keys.push({ text, at: nowMs })
      if (this.keys.length > this.maxKey) this.keys.shift()
      return
    }
    const nums = []
    // 抽掉所有数字（含小数）当模板。`内点=23` 与 `内点=24` 因此同模板。
    const tpl = text.replace(/\d+(?:\.\d+)?/g, (m) => {
      nums.push(Number(m))
      return '#'
    })
    let g = this.groups.get(tpl)
    if (!g) {
      g = { tpl, count: 0, firstAt: nowMs, lastAt: nowMs, mins: nums.slice(), maxs: nums.slice() }
      this.groups.set(tpl, g)
      // 超上限时丢**最久没更新**的那一组，而不是最先出现的：一个早就不再发生的
      // 模板留着没用，而正在刷的那些才是当前状态。
      if (this.groups.size > this.maxStream) {
        let oldest = null
        for (const [k, v] of this.groups) {
          if (!oldest || v.lastAt < oldest[1].lastAt) oldest = [k, v]
        }
        if (oldest) this.groups.delete(oldest[0])
      }
    }
    g.count++
    g.lastAt = nowMs
    for (let i = 0; i < nums.length; i++) {
      if (nums[i] < (g.mins[i] ?? Infinity)) g.mins[i] = nums[i]
      if (nums[i] > (g.maxs[i] ?? -Infinity)) g.maxs[i] = nums[i]
    }
  }

  /** 把模板里的 `#` 还原成 `min~max`（相等时只写一个数）。 */
  static render(g) {
    let i = 0
    const body = g.tpl.replace(/#/g, () => {
      const lo = g.mins[i]
      const hi = g.maxs[i]
      i++
      if (lo === undefined) return '#'
      return lo === hi ? String(lo) : `${lo}~${hi}`
    })
    return g.count > 1 ? `${body}  ×${g.count}` : body
  }

  /** 渲染用的行数组。关键区在前（它们才是要看的），流区按最后发生时间排。 */
  lines() {
    const ts = (ms) => new Date(ms).toISOString().slice(11, 23)
    const out = []
    if (this.keys.length) {
      out.push('── 关键事件 ──')
      for (const k of this.keys) out.push(`${ts(k.at)} ${k.text}`)
    }
    const gs = [...this.groups.values()].sort((a, b) => a.lastAt - b.lastAt)
    if (gs.length) {
      out.push(`── 实时（${gs.length} 类 / ${gs.reduce((n, g) => n + g.count, 0)} 条）──`)
      for (const g of gs) out.push(`${ts(g.lastAt)} ${DiagLog.render(g)}`)
    }
    return out
  }

  text() {
    return this.lines().join('\n')
  }

  clear() {
    this.keys = []
    this.groups.clear()
  }
}

const log = new DiagLog()
let enabled = false
let panel = null
let pre = null
let dirty = false

/**
 * 调试模式**要能跨刷新活着**，所以状态存 localStorage。
 *
 * 以前它只是个内存变量：刷新一次就关了。而调试模式的用法恰恰是"打开它，然后再复现
 * 一次问题" —— 复现往往就要刷新，于是每次都得重新连按一遍。
 *
 * 存储失败（隐私模式）不算错误：那时它退化成"只在这一次会话里有效"，也就是以前的行为。
 */
const DEBUG_KEY = 'photoar.debug'

function loadDebugFlag() {
  if (new URLSearchParams(location.search).get('diag') === '1') return true
  try {
    return localStorage.getItem(DEBUG_KEY) === '1'
  } catch {
    return false
  }
}

function saveDebugFlag(on) {
  try {
    on ? localStorage.setItem(DEBUG_KEY, '1') : localStorage.removeItem(DEBUG_KEY)
  } catch { /* 隐私模式：只在本次会话里有效 */ }
}

/** `?diag=1` 或上次开着就直接开。否则等连按解锁。 */
export function initDiag() {
  if (loadDebugFlag()) enable()
  installGlobalHooks()
  return { log, enable, disable, isEnabled: () => enabled, diag }
}

/**
 * 打一行。**关闭时不拼字符串** —— 调用方要用 `diag(() => \`...\`)` 的形式传惰性求值，
 * 或者传已经拼好的字符串（那时就得自己确认拼它不贵）。
 */
export function diag(msgOrFn) {
  if (!enabled) return
  const text = typeof msgOrFn === 'function' ? msgOrFn() : String(msgOrFn)
  log.push(text, Date.now(), 'stream')
  dirty = true
}

/**
 * 关键事件：**进关键区，不参与聚合、不会被高频行挤掉**。
 *
 * 无论开关都记 —— 用户想看日志的时刻，正是错误已经发生之后。开关只控制显示。
 */
export function diagAlways(msgOrFn) {
  const text = typeof msgOrFn === 'function' ? msgOrFn() : String(msgOrFn)
  log.push(text, Date.now(), 'key')
  dirty = true
  if (enabled) return
  // 有错误发生过就在面板标题上留个痕，好让人知道"这里有东西可看"。
  pendingErrors++
}
let pendingErrors = 0

export function pendingErrorCount() {
  return pendingErrors
}

function enable() {
  enabled = true
  pendingErrors = 0
  saveDebugFlag(true)
  if (!panel) buildPanel()
  panel.hidden = false
  render()
  for (const fn of watchers) fn(true)
}

function disable() {
  enabled = false
  saveDebugFlag(false)
  if (panel) panel.hidden = true
  for (const fn of watchers) fn(false)
}

/**
 * 调试模式开关的订阅者。
 *
 * 扫描页那条元信息要按模式换内容（非调试模式下只说人话，见 `pages/scan.js` 的
 * `meta()`），而它是每帧重绘的，所以其实不需要订阅。真正需要的是**设置页** ——
 * 在那一页连按解锁时，页面上的按钮要立刻跟着变，否则用户不知道解锁成功了。
 */
const watchers = new Set()
export function onDebugChange(fn) {
  watchers.add(fn)
  return () => watchers.delete(fn)
}

/** 调试模式开着没开着。名字保留 `isDiagEnabled` —— 调用点不少，而它就是同一件事。 */
export const isDiagEnabled = () => enabled
export function setDiagEnabled(on) {
  on ? enable() : disable()
}

/**
 * 连点某个元素三次**关掉**调试模式。用三连击而不是长按：长按在相机页面上会和系统
 * 手势打架。
 *
 * ⚠️ 它只能关、不能开。进调试模式的唯一入口是**设置页里连按版本号**
 * （`pages/settings.js`）—— 那是个明确的、要找一下才找得到的动作。
 *
 * 以前这里是"没开就开、开了就关"，而它绑在扫描页那条元信息上：宾客在那条字上手快点
 * 三下就掉进调试模式，看到一屏内点数和毫秒数。反过来"开着的时候能就地关掉"是真需求
 * （调试时手机就在手上，跑回设置页很烦），所以留下关的那一半。
 */
export function bindToggle(el) {
  let hits = []
  el.addEventListener('click', () => {
    if (!enabled) return
    const now = Date.now()
    hits = hits.filter((t) => now - t < 900)
    hits.push(now)
    if (hits.length >= 3) {
      hits = []
      disable()
    }
  })
}

function buildPanel() {
  panel = document.createElement('div')
  panel.id = 'diag'
  panel.hidden = true
  panel.innerHTML = `
    <div class="diag-bar">
      <span>诊断日志</span>
      <button type="button" data-act="copy">复制</button>
      <button type="button" data-act="clear">清空</button>
      <button type="button" data-act="close">关</button>
    </div>
    <pre></pre>`
  pre = panel.querySelector('pre')
  panel.querySelector('[data-act="copy"]').addEventListener('click', async (e) => {
    const btn = e.currentTarget
    const text = log.text()
    try {
      await navigator.clipboard.writeText(text)
      btn.textContent = '已复制'
    } catch {
      // clipboard API 在非安全上下文或没有用户手势时会拒。退回选中，让人手动复制 ——
      // 直接说"复制失败"等于把这块日志锁在手机里。
      const r = document.createRange()
      r.selectNodeContents(pre)
      getSelection().removeAllRanges()
      getSelection().addRange(r)
      btn.textContent = '已选中'
    }
    setTimeout(() => (btn.textContent = '复制'), 1500)
  })
  panel.querySelector('[data-act="clear"]').addEventListener('click', () => {
    log.clear()
    render()
  })
  panel.querySelector('[data-act="close"]').addEventListener('click', disable)

  const style = document.createElement('style')
  style.textContent = `
    #diag {
      position: fixed; left: 0; right: 0; top: 0; z-index: 20;
      max-height: 52vh; display: flex; flex-direction: column;
      background: rgba(11,12,16,.92); color: #e8e6e3;
      font: 11px/1.45 var(--mono, monospace);
      border-bottom: 2px solid #2a2c33;
      padding-top: env(safe-area-inset-top);
    }
    #diag[hidden] { display: none; }
    .diag-bar {
      display: flex; gap: 6px; align-items: center;
      padding: 6px 8px; border-bottom: 1px solid #2a2c33;
      color: var(--amber, #ffc46b); letter-spacing: .08em;
    }
    .diag-bar span { flex: 1; }
    .diag-bar button {
      font: inherit; min-height: 28px; padding: 4px 10px;
      background: transparent; color: var(--amber, #ffc46b);
      border: 0; border-radius: 0; box-shadow: inset 0 0 0 1px #2a2c33;
    }
    #diag pre {
      margin: 0; padding: 6px 8px; overflow: auto;
      white-space: pre-wrap; word-break: break-all;
      -webkit-user-select: text; user-select: text;
    }`
  document.head.appendChild(style)
  document.body.appendChild(panel)
}

function render() {
  if (!enabled || !pre) return
  pre.textContent = log.text()
  // 贴着底部：新的在下面，而正在发生的事才是要看的。
  pre.scrollTop = pre.scrollHeight
}

/** 每帧最多刷一次 DOM。逐行 append 会让开着日志时的帧率明显掉下去。 */
export function flushDiag() {
  if (!dirty) return
  dirty = false
  render()
}

/**
 * 全局错误钩子。**这些无论开关都记** —— 用户想看日志的时刻，正是错误已经发生之后。
 */
function installGlobalHooks() {
  addEventListener('error', (e) => {
    // 资源加载失败（img/script/video）的 event.target 是那个元素，没有 message。
    if (e.target && e.target !== window && e.target.tagName) {
      diagAlways(`资源加载失败 <${e.target.tagName.toLowerCase()}> ${short(e.target.currentSrc || e.target.src)}`)
      return
    }
    diagAlways(`JS 错误 ${e.message} @${short(e.filename)}:${e.lineno}`)
  }, true)

  addEventListener('unhandledrejection', (e) => {
    diagAlways(`未处理的 rejection ${e.reason?.message ?? e.reason}`)
  })

  // console.error/warn 也收进来。手机上看不到控制台，而库代码（含 opencv.js）
  // 出问题时只会往那里写。
  for (const level of ['error', 'warn']) {
    const orig = console[level].bind(console)
    console[level] = (...args) => {
      diagAlways(`console.${level} ${args.map(fmt).join(' ')}`)
      orig(...args)
    }
  }
}

const fmt = (v) => {
  if (typeof v === 'string') return v
  if (v instanceof Error) return `${v.name}: ${v.message}`
  try {
    return JSON.stringify(v)?.slice(0, 300) ?? String(v)
  } catch {
    return String(v)
  }
}

/** URL 只留末段。这块日志是要被发出去的，而完整 URL 里带着 tailnet 域名和 photoId。 */
export function short(u) {
  if (!u) return '(空)'
  try {
    const url = new URL(u, location.href)
    const seg = url.pathname.split('/').filter(Boolean)
    return `…/${seg.slice(-2).join('/')}`
  } catch {
    return String(u).slice(-40)
  }
}

/** `MediaError.code` → 人能读的名字。**这四个的修法完全不同**，所以必须分开显示。 */
export const MEDIA_ERR = {
  1: 'ABORTED(用户或代码中止)',
  2: 'NETWORK(传输断了：反代/Range/超时)',
  3: 'DECODE(解码失败：编码不支持或文件损坏)',
  4: 'SRC_NOT_SUPPORTED(拿不到或格式不认：401/404/Content-Type 不对)',
}

/** `HTMLMediaElement.networkState`。区分"还在找源"和"已经没源可用"。 */
export const NETWORK_STATE = { 0: 'EMPTY', 1: 'IDLE', 2: 'LOADING', 3: 'NO_SOURCE' }
/** `readyState`。<2 就没有帧可当 GL 纹理，贴合会直接不画。 */
export const READY_STATE = {
  0: 'NOTHING', 1: 'METADATA', 2: 'CURRENT_DATA', 3: 'FUTURE_DATA', 4: 'ENOUGH_DATA',
}

/**
 * 共用的小部件。**Android `Widgets.kt` / `PixelWidgets.kt` 的对译。**
 *
 * 只放**每一页都要用**的那几样：加载态、空态、错误态、区块、行、按钮、toast。
 * 更专门的东西留在各自那一页 —— 一个共用组件库长起来之后，改一处会牵动五页，
 * 而这个项目里五页的用法本来就不一样。
 *
 * 三个状态（加载 / 空 / 错误）必须各自成型，不能合成一个"没有内容"：
 * 它们要用户做的事完全不同 —— 等、去别处做点什么、重试。Android 那边的
 * `hintOf(count, failed)` 是同一个决定。
 */
import { junimo, sprite } from './art.js'
import { icon } from './pixelicons.js'

export const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag)
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue
    if (k === 'class') el.className = v
    else if (k === 'html') el.innerHTML = v
    else if (k === 'text') el.textContent = v
    else if (k.startsWith('on')) el.addEventListener(k.slice(2).toLowerCase(), v)
    else if (k === 'dataset') Object.assign(el.dataset, v)
    else el.setAttribute(k, v === true ? '' : String(v))
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)))
  }
  return el
}

/**
 * 加载中。**不显示进度百分比** —— 这些请求都没有可靠的总量，编一个数字比不给更糟。
 *
 * 配一只走路的 Junimo 而不是转圈：转圈只说明"还没好"，而这里的等待动辄十几秒。
 */
export const loading = (text = '正在加载…') =>
  h('div', { class: 'state' }, junimo(), h('p', { class: 'state-h', text }))

/**
 * 空态。
 *
 * `hint` 是必需的而不是可选的：一句「暂无数据」什么也没说，而空态恰恰是用户最需要
 * 被告知「接下来该做什么」的时刻。Android 那边每一处空态都写了下一步（照片库空了
 * 说去哪加、访客没授权说去找管理员）。
 *
 * `art` 可以换成别的物件图。默认是稻草人 —— 一个人站在空地里，正是"这儿还没有东西"。
 */
export const empty = (title, hint, art = 'scarecrow') =>
  h('div', { class: 'state' },
    sprite(art),
    h('p', { class: 'state-t', text: title }),
    h('p', { class: 'state-h', text: hint }))

/**
 * 错误态，带重试。
 *
 * 401/403 不该走这里 —— 那是「重试没用」的一类，调用方应当去登录或告知无权限。
 */
export const failed = (message, onRetry) =>
  h('div', { class: 'state' },
    sprite('cross'),
    h('p', { class: 'state-t bad', text: '出错了' }),
    h('p', { class: 'state-h', text: message }),
    onRetry && h('button', { class: 'ghost', onclick: onRetry, html: `${icon('refresh')}<span>重试</span>` }))

/**
 * 一个区块：标题 + 一块木牌。标题用来分隔使用频率差好几个数量级的东西
 * （见 Android 拆页签的理由）。
 *
 * **标题在木牌外面**，压在深色底上。两个理由：一是游戏里的分组就是这个层次感；
 * 二是它绕开了"标题该用哪种前景色"—— 它根本不在面板里，永远是暗底上的浅字。
 */
export function section(title, ...kids) {
  const body = h('div', { class: 'panel' }, ...kids)
  const sec = h('section', { class: 'sec' }, title && h('h2', { text: title }), body)
  /**
   * 木牌本身。**建好之后还要往里加东西的，必须往 `sec.body` 加，不是 `sec`。**
   *
   * 这一条踩过：`section()` 原本直接返回一个平的 `<section>`，几处页面在等到异步结果
   * 之后 `sec.appendChild(row(…))`。改成"标题 + 木牌"两层之后那些行落到了木牌**外面** ——
   * 桃色的字掉在深色底上。而 `sec.lastChild.remove()`（settings 那处"取中…"占位）
   * 删掉的更狠：它把整块木牌删了。
   */
  sec.body = body
  return sec
}

/** 一张裱起来的图。参考图、缩略大图用 —— 裸挂在深色底上像张贴纸。 */
export const framed = (img) => h('div', { class: 'framed' }, img)

/** 键值行。`mono` 给需要对齐的数字（字节数、耗时）。 */
export const row = (label, value, { mono = false, bad = false } = {}) =>
  h('div', { class: 'row2' },
    h('span', { class: 'k', text: label }),
    h('span', { class: `v${mono ? ' mono' : ''}${bad ? ' bad' : ''}`, text: value ?? '—' }))

export const button = (label, onclick, { kind = '', iconName = null, disabled = false } = {}) =>
  h('button', {
    class: kind, onclick, disabled,
    html: iconName ? `${icon(iconName)}<span>${label}</span>` : `<span>${label}</span>`,
  })

/**
 * 一条一闪而过的提示。
 *
 * 只用于「做完了」这类不需要用户回应的消息。**失败不要用 toast** —— 它会自己消失，
 * 而失败信息往往是用户唯一能据此去问人的线索（Android 那边失败都留在页面上）。
 */
export function toast(message) {
  let host = document.getElementById('toasts')
  if (!host) {
    host = h('div', { id: 'toasts' })
    document.body.appendChild(host)
  }
  const t = h('div', { class: 'toast', text: message })
  host.appendChild(t)
  setTimeout(() => t.remove(), 2600)
}

/**
 * 危险动作的二次确认。
 *
 * 用原生 `confirm` 而不是自绘弹窗：这套界面在手机上全屏，自绘弹窗要处理返回键、
 * 焦点陷阱、滚动锁定 —— 而 Android 那边每一处删除都是一个 `AlertDialog`，
 * 原生 confirm 是它在网页上最接近的等价物，且不会有"点了没反应"的风险。
 */
export const confirmDanger = (message) => globalThis.confirm(message)

/** 字节数。与 Android `Fmt.bytes` 同一个口径（1024 进制，一位小数）。 */
export function bytes(n) {
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 1024) return `${n} B`
  const u = ['KB', 'MB', 'GB', 'TB']
  let v = n / 1024
  let i = 0
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return `${v.toFixed(1)} ${u[i]}`
}

/** 毫秒 → 人读的时长。 */
export function duration(ms) {
  if (!Number.isFinite(ms) || ms <= 0) return '—'
  const s = Math.round(ms / 1000)
  return s < 60 ? `${s} 秒` : `${Math.floor(s / 60)} 分 ${String(s % 60).padStart(2, '0')} 秒`
}

/** 时间戳（秒或毫秒）→ 本地时间。服务端的 `created_at` 是秒。 */
export function when(ts) {
  if (!ts) return '—'
  const ms = ts < 1e12 ? ts * 1000 : ts
  const d = new Date(ms)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

/**
 * 缩略图。带 `rev` 是因为**换参考图之后 URL 不变而内容变了** —— Android 那边的对策是
 * 清掉 `Thumbs` 缓存，网页这边只能靠查询串把浏览器缓存打掉。
 */
export const thumb = (id, rev, alt = '') =>
  h('img', { class: 'thumb', loading: 'lazy', decoding: 'async', alt, src: `/v1/photo/${id}/thumb?rev=${rev}` })

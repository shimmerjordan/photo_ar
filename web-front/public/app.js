/**
 * 引导：登录 → 取识别库 → 起识别 Worker → 装外壳。
 *
 * 扫描本身已经搬进 `pages/scan.js` —— 这一层只负责"进得来"和"各页共用的东西"。
 *
 * ## 为什么识别 Worker 在这一层，而不在扫描页里
 *
 * 它持有 12MB 的 wasm 实例和解析好的识别库。用户在页签之间来回切是常态，而每次进
 * 扫描页重建一次就是每次重新加载 + 实例化。所以 Worker 归外壳长驻，扫描页只借用
 * （切离时给它发 `reset` 清跟踪状态，不 terminate）。
 *
 * ## 登录门只有一步
 *
 * Android 的登录蒙版有两步（先填服务端地址、再填账号），因为 App 装在手机上得自己找
 * 服务端。网页反过来 —— 它是服务端发出来的，"地址"这件事不存在。所以只有账号那一步。
 */
import * as api from './api.js'
import { junimo } from './art.js'
import { bindToggle, diagAlways, initDiag } from './diag.js'
import { Shell } from './shell.js'
import { toast } from './ui.js'

const $ = (id) => document.getElementById(id)
const els = {
  gate: $('gate'), gateMsg: $('gate-msg'), gateErr: $('gate-err'),
  name: $('name'), pw: $('pw'), pwField: $('pw-field'),
  enter: $('enter'), togglePw: $('toggle-pw'),
  app: $('app'), topbar: $('topbar'), title: $('title'), back: $('back'),
  view: $('view'), tabbar: $('tabbar'),
  bar: $('bar'), barFill: $('bar')?.firstElementChild, boot: $('boot'),
}

/**
 * 启动那一行：一只走路的 Junimo + 一句在说什么。
 *
 * 这段等待可能十几秒（12MB 的 wasm 要下要编）。放一只小绿人不是为了可爱 ——
 * 是因为**用户需要知道这台机器还活着**，而一条进度条在"正在编译"那一段是不动的
 * （编译没有可报的进度），只有它在动。
 */
const bootSprite = junimo()
els.boot?.prepend(bootSprite)
const bootText = document.createElement('span')
els.boot?.append(bootText)

const state = { me: null, worker: null, libInfo: null, shell: null }

initDiag()

// ── 进度条（引擎那 12MB 要下要编，不给进度等于卡死）────────────────────
function progress(pct, { hide = false } = {}) {
  if (!els.bar) return
  if (hide) return void (els.bar.hidden = true)
  els.bar.hidden = false
  if (typeof pct === 'number' && pct >= 0) {
    els.bar.classList.remove('indeterminate')
    // 动 transform 而不是 width：这条在相机预览之上、加载期间每 200ms 更新一次。
    els.barFill.style.transform = `scaleX(${Math.min(1, Math.max(0, pct / 100))})`
    els.bar.setAttribute('aria-valuenow', String(Math.round(pct)))
  } else {
    els.bar.classList.add('indeterminate')
    els.barFill.style.transform = ''
    els.bar.removeAttribute('aria-valuenow')
  }
}
const MB = (n) => (n / 1048576).toFixed(1)
const bootSay = (text) => { bootText.textContent = text }

/**
 * 记一笔"这次引擎是从网络来的还是缓存来的"，连续走网络就**把原因说出来**。
 *
 * ## 为什么值得为它写一个函数
 *
 * 实测过一次：同一台手机、同一份代码，`https://<自签证书的IP>` 上打开要 **71 秒**，
 * `http://localhost` 上 **1.6 秒**。差别全在缓存 —— Chromium 对**有证书错误的源整体
 * 禁用磁盘缓存**，`Cache-Control: immutable` 写了也不算，于是每次进页面都真的重下
 * 11.4MB（而且下两遍：预取一次、`instantiateStreaming` 又一次，缓存正常时第二次是
 * 0 字节命中）。
 *
 * 这件事**没有任何 API 能直接查**，而它的症状是"手机好慢"——所有人都会去怀疑手机、
 * 怀疑网络、怀疑 wasm 太大，没人会怀疑证书。所以这里用最笨也最可靠的办法：数次数。
 * 第二次还在走网络，就说明缓存没起作用，那时把原因和出路一起写在屏幕上。
 *
 * 阈值是 2 而不是 1：第一次访问本来就该走网络。
 */
const ENGINE_FETCH_KEY = 'photoar.engineNetFetches'
function noteEngineFetch(fromCache, total) {
  if (fromCache === null || fromCache === undefined) return  // 没数据，不猜
  let n = 0
  try {
    n = Number(localStorage.getItem(ENGINE_FETCH_KEY)) || 0
    localStorage.setItem(ENGINE_FETCH_KEY, String(fromCache ? 0 : n + 1))
  } catch { return }   // 隐私模式下 localStorage 会抛，那就没这条诊断，不影响使用
  if (fromCache) return
  diagAlways(`引擎走了网络（${MB(total)}MB），本机缓存连续未命中 ${n + 1} 次`)
  if (n + 1 < 2) return
  // 只在 https 上提证书：http 页面上没缓存是另一回事（而 http 下相机本来就开不了，
  // 用户会先撞到那个）。
  const why = location.protocol === 'https:'
    ? '多半是这个地址的证书不被浏览器信任（自签 / 点过"继续访问"）—— 那种情况下 ' +
      'Chromium 会对整个站点关掉磁盘缓存。换成受信任证书的地址（隧道域名）就好了。'
    : '这个地址不是 https，浏览器的缓存策略更严。'
  diagAlways(`⚠️ 每次进来都要重下 ${MB(total)}MB 引擎。${why}`)
}

// ── 登录门 ────────────────────────────────────────────────────────────
els.togglePw.addEventListener('click', () => {
  els.pwField.hidden = !els.pwField.hidden
  els.togglePw.textContent = els.pwField.hidden ? '我是管理员' : '我是访客'
})
els.enter.addEventListener('click', doLogin)
els.name.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin() })
els.pw.addEventListener('keydown', (e) => { if (e.key === 'Enter') doLogin() })

async function doLogin() {
  els.gateErr.hidden = true
  const name = els.name.value.trim()
  if (!name) return showGateError('先填名字。')
  els.enter.disabled = true
  try {
    await api.login(name, els.pw.value || undefined)
    els.gate.hidden = true
    await boot()
  } catch (e) {
    // 401 与 403 的分界线是"重输有没有可能成功"——服务端明确按这条分的，所以这里也必须
    // 区分：403 的时候把输入框留着让人反复输，是在浪费他的时间。
    if (e.retryable) showGateError(`${e.message ?? '口令不对'}。再试一次。`)
    else showGateError(`${e.message ?? '进不去'}（${e.code ?? e.status}）。这个需要管理员处理，重输没用。`)
  } finally {
    els.enter.disabled = false
  }
}

function showGateError(msg) {
  els.gateErr.textContent = msg
  els.gateErr.hidden = false
}

function showGate(msg) {
  if (msg) els.gateMsg.innerHTML = msg
  els.gate.hidden = false
  els.app.hidden = true
}

// ── 启动 ──────────────────────────────────────────────────────────────
async function boot() {
  els.gate.hidden = true
  els.app.hidden = false
  els.boot.hidden = false
  bootSay('正在取识别库…')
  progress(-1)

  let cfg = {}
  try {
    cfg = await api.webConfig()
  } catch { /* 拿不到就用 consts.js 的源码默认阈值 */ }

  let libBuf
  try {
    const r = await fetch('/api/lib', { credentials: 'same-origin' })
    if (r.status === 401 || r.status === 403) {
      progress(null, { hide: true })
      return showGate(null)
    }
    if (!r.ok) {
      const err = await r.json().catch(() => ({}))
      progress(null, { hide: true })
      return showGate(`<span class="err">取识别库失败：${err.message ?? r.status}</span>`)
    }
    libBuf = await r.arrayBuffer()
  } catch (e) {
    progress(null, { hide: true })
    return showGate(`<span class="err">取识别库失败：${e.message}</span>`)
  }

  // 谁登录着。**在库之后取**：库那一步已经证明了会话有效，而 /v1/me 失败不该阻止使用
  // （角色拿不到时按访客处理，那是更安全的默认）。
  try {
    state.me = await api.me()
  } catch (e) {
    // 按访客处理是更安全的默认，但**必须留一条**：这个 fallback 曾经掩盖过一个
    // 写错路径导致的 404，表现是"管理员登进去只有两个页签"而没有任何报错。
    diagAlways(`取不到身份（${e.status ?? ''} ${e.message}），按访客处理 —— 管理功能会看不到`)
    state.me = { role: 'viewer', isAdmin: false }
  }

  bootSay('正在加载识别引擎…')
  progress(0)
  await startWorker(libBuf, cfg.thresholds)
}

function startWorker(libBuf, thresholds) {
  return new Promise((resolve) => {
    const w = new Worker('/recognize/worker.js', { type: 'module' })
    state.worker = w
    w.onerror = (e) => {
      progress(null, { hide: true })
      bootSay(`识别引擎起不来：${e.message}`)
      diagAlways(`Worker onerror ${e.message}`)
      // 引擎起不来仍然把外壳装上 —— 除扫描外的每一页都不需要它。
      mountShell()
      resolve()
    }
    w.addEventListener('message', (ev) => {
      const m = ev.data
      if (m.type === 'progress') {
        if (m.done) {
          progress(-1)
          // 这里只说"下完了"。**不说"正在编译"** —— 那一段可能是几秒的真编译，也可能是
          // 几十毫秒的 code cache 命中，而在这个时刻还不知道是哪一种。写死成"正在编译"
          // 会让每一次刷新看起来都在重新编译（这条踩过，正是用户问出来的）。
          bootSay(m.fromCache === true
            ? '引擎从本机缓存读到，正在装配…'
            : '引擎已下载，正在装配…')
          noteEngineFetch(m.fromCache, m.total)
        } else if (m.total) {
          progress(m.pct)
          bootSay(`正在下载识别引擎 ${MB(m.loaded)} / ${MB(m.total)} MB`)
        } else {
          progress(-1)
          bootSay(`正在加载识别引擎 ${MB(m.loaded)} MB…`)
        }
        return
      }
      if (m.type === 'wasm') {
        diagAlways(`wasm ${m.name} ${JSON.stringify(m.detail ?? {})}`)
        // `streaming` 是**装配那一步实测的耗时**，也是"这次到底编译了没有"唯一的可观测量
        // —— 浏览器的 wasm code cache 是隐式的，没有任何 API 能查它命中没有。
        // 阈值取 400ms：命中时这一步是几十毫秒量级，真编译 12MB 在手机上是秒级，
        // 中间那一档空得足够宽，不会误判。
        if (m.name === 'streaming') {
          const ms = m.detail?.ms ?? 0
          bootSay(ms < 400 ? `引擎从编译缓存加载（${ms}ms）` : `引擎编译完成（${ms}ms）`)
        }
        if (m.name === 'hit') bootSay('引擎从缓存加载（跳过编译）…')
        return
      }
      if (m.type === 'error') {
        diagAlways(`Worker 错误 ${m.message}`)
        return
      }
      if (m.type === 'ready') {
        state.libInfo = { nPhotos: m.nPhotos, skipped: m.skipped, hasVocab: m.hasVocab }
        diagAlways(`引擎就绪 opencv=${m.opencvVersion} 库=${m.nPhotos}张 词表=${m.hasVocab}` +
          (m.skipped?.length ? ` 跳过${m.skipped.length}张` : ''))
        progress(null, { hide: true })
        els.boot.hidden = true
        mountShell()
        resolve()
      }
    })
    // libBuf 走 transfer：它可能有十几 MB，克隆一份纯属浪费。
    w.postMessage({ type: 'init', libBuf, thresholds }, [libBuf])
  })
}

function mountShell() {
  if (state.shell) {
    // 重新登录（换人）之后：角色可能变了，把栈收回一个还能待的页签。
    state.shell.roleChanged(isAdmin())
    return
  }
  state.shell = new Shell(
    { topbar: els.topbar, title: els.title, back: els.back, view: els.view, tabbar: els.tabbar },
    {
      me: () => state.me,
      isAdmin,
      libInfo: () => state.libInfo,
      worker: state.worker,
      toast,
      bindDiagToggle: (el) => bindToggle(el),
    },
  )
  state.shell.renderTabs(isAdmin())
  state.shell.start()
}

/**
 * 是不是管理员。
 *
 * **优先用服务端给的 `isAdmin`**，而不是自己比对 `role === 'admin'`：角色名是服务端的
 * 内部表示，而它已经把结论算好了（`prin.is_admin`）。自己比字符串等于把那个判断抄第二遍。
 */
function isAdmin() {
  return state.me?.isAdmin === true || state.me?.role === 'admin'
}

boot().catch((e) => showGate(`<span class="err">启动失败：${e.message}</span>`))

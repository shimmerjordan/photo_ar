/**
 * 量 wasm 那一步到底花了多久，以及（给单文件构建兜底的）IndexedDB 模块缓存。
 *
 * ## 「能不能直接下载编译好的」——不能，这一条是硬的
 *
 * 编译产物是**特定 CPU 架构 + 特定 V8 版本**的机器码，没有交换格式，也没有任何浏览器
 * API 能把它导入进来。`WebAssembly.Module` 虽然可结构化克隆（能 postMessage），
 * 但 **Chromium 从来不允许把它持久化进 IndexedDB**（Firefox 可以）。
 *
 * 所以在浏览器里"跳过编译"只有一条路：**浏览器自己的 code cache**。它只对
 * `compileStreaming`/`instantiateStreaming(fetch(url))` 生效 —— 浏览器要有个 URL 才能
 * 把编译结果存起来。`tools/split-wasm.mjs` 把内联的 wasm 抽成独立文件、并把加载路径
 * 改成 `instantiateStreaming`，就是为了让这条路成立。
 *
 * 真正能把编译时间再砍一截的是**把 wasm 变小**：现在这 11.9MB 是 OpenCV 的完整构建，
 * 而我们只用到 core/imgproc/features2d/calib3d。用 emsdk 自己裁一份大约 2~3MB ——
 * 编译时间大致按体积等比下降。那要引入构建步骤，见 vendor/README.md。
 *
 * ## 这个模块现在做两件事
 *
 * **一、计时（对当前 vendor 唯一有效的那件事）。** 拆分之后加载走的是
 * `instantiateStreaming`，下面的 `WebAssembly.instantiate` 拦截**根本不会被调到**。
 * 所以这里把 streaming 那两个也包起来，只为报出真实耗时 —— 否则"这次到底编译了没有"
 * 在手机上无从判断，而那正是用户会问的问题。几十毫秒 = 命中了 code cache，
 * 几秒 = 真的在编译。
 *
 * **二、IndexedDB 兜底（只对单文件构建有用）。** 万一 vendor 换回内联 wasm 的构建，
 * 加载会走 `WebAssembly.instantiate(bytes)`，那条路没有 URL、浏览器不给 code cache。
 * 那时下面这套把编译好的 Module 存进 IndexedDB 就是唯一的办法。
 *
 * ## 三个必须优雅降级的地方
 *
 * 1. **Safari 不支持把 `WebAssembly.Module` 持久化进 IndexedDB**（能结构化克隆，但
 *    写 IndexedDB 会抛 `DataCloneError`）。所以写入失败只记一笔、照常返回编译结果。
 * 2. **配额**。编译后的模块比源 wasm 大，可能撞上存储配额。同样只降级。
 * 3. **版本**。缓存键里必须带 vendor 的标识 —— 换了 opencv.js 而复用旧模块，
 *    行为是"函数签名对不上"这种极难查的错。这里用**字节长度 + 前后各 64 字节的哈希**
 *    当键：它不需要额外下载，又能在文件变化时必然改变。
 */

const DB_NAME = 'photoar-wasm'
const STORE = 'modules'
const DB_VERSION = 1

let installed = false
let stats = { hit: false, compileMs: 0, cacheMs: 0, streamMs: 0, saved: false, error: null }

export function cacheStats() {
  return { ...stats }
}

function openDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION)
    req.onupgradeneeded = () => {
      const db = req.result
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE)
    }
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

function idbGet(db, key) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly')
    const req = tx.objectStore(STORE).get(key)
    req.onsuccess = () => resolve(req.result)
    req.onerror = () => reject(req.error)
  })
}

function idbPut(db, key, value) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).put(value, key)
    tx.oncomplete = () => resolve()
    // `DataCloneError` 在这里出现（Safari 不给持久化 Module）—— 交给调用方降级。
    tx.onerror = () => reject(tx.error)
    tx.onabort = () => reject(tx.error ?? new Error('IndexedDB 事务被中止'))
  })
}

/**
 * 缓存键：字节长度 + 首尾各 64 字节的 SHA-256 前 16 hex。
 *
 * 不哈希全部 13MB：那要几十毫秒，而这条路径的全部意义就是省时间。首尾 + 长度足以在
 * 换 vendor 文件时必然改变 —— 而这里要防的是"换了 opencv.js 却复用旧模块"，不是
 * 对抗构造碰撞。
 */
async function keyOf(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes)
  const head = u8.subarray(0, 64)
  const tail = u8.subarray(Math.max(0, u8.length - 64))
  const probe = new Uint8Array(head.length + tail.length)
  probe.set(head, 0)
  probe.set(tail, head.length)
  const d = await crypto.subtle.digest('SHA-256', probe)
  const hex = [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, '0')).join('').slice(0, 16)
  return `wasm:${u8.length}:${hex}`
}

/**
 * 装上拦截。**必须在 import opencv.js 之前调**，否则那次编译已经发生了。
 *
 * @param onEvent 可选，`(name, detail)` —— 用来把命中/未命中报到 UI 上。不报的话
 *   "为什么这次快/这次慢"就无从解释，而那正是用户会问的问题。
 */
export function installWasmCache(onEvent) {
  if (installed) return
  installed = true

  // ── 计时：拆分后的 vendor 唯一会走的那条路 ────────────────────────────
  //
  // 这里**什么都不缓存**，只把耗时报出来。浏览器的 code cache 是隐式的、没有任何 API
  // 能查它命中没有 —— 唯一的信号就是这一步花了多久。而它是"刷新之后是不是又编译了"
  // 的唯一可观测量：几十毫秒说明命中了，几秒说明真在编译。
  for (const name of ['instantiateStreaming', 'compileStreaming']) {
    const fn = WebAssembly[name]
    if (typeof fn !== 'function') continue
    const bound = fn.bind(WebAssembly)
    WebAssembly[name] = async function (source, imports) {
      const t0 = performance.now()
      try {
        return await bound(source, imports)
      } finally {
        stats.streamMs = Math.round(performance.now() - t0)
        onEvent?.('streaming', { name, ms: stats.streamMs })
      }
    }
  }

  const orig = WebAssembly.instantiate.bind(WebAssembly)

  WebAssembly.instantiate = async function (src, imports) {
    // 已经是 Module 的调用原样放行 —— 那条路本来就不需要编译。
    if (!(src instanceof ArrayBuffer) && !(src instanceof Uint8Array)) {
      return orig(src, imports)
    }

    let db = null
    let key = null
    try {
      key = await keyOf(src)
      const t0 = performance.now()
      db = await openDb()
      const cached = await idbGet(db, key)
      stats.cacheMs = Math.round(performance.now() - t0)
      if (cached instanceof WebAssembly.Module) {
        stats.hit = true
        onEvent?.('hit', { key, ms: stats.cacheMs })
        const instance = await WebAssembly.instantiate(cached, imports)
        // 返回 emscripten 期待的形状：`{instance, module}`。少了 module 那一半，
        // emscripten 后面取 `result.module` 会拿到 undefined —— 而它只在某些路径上用，
        // 所以这个错会表现成"偶发的初始化失败"。
        return { instance, module: cached }
      }
    } catch (e) {
      // 读缓存失败绝不能影响加载。记下来，照常编译。
      stats.error = `读缓存失败: ${e?.name ?? e}`
      onEvent?.('read-failed', { error: String(e?.message ?? e) })
    }

    const t1 = performance.now()
    const module = await WebAssembly.compile(src)
    stats.compileMs = Math.round(performance.now() - t1)
    onEvent?.('compiled', { ms: stats.compileMs })

    // 存缓存是**尽力而为**。Safari 会在这里抛 DataCloneError（它允许结构化克隆
    // WebAssembly.Module，但不允许持久化到 IndexedDB），配额不足也会抛。
    try {
      if (db && key) {
        await idbPut(db, key, module)
        stats.saved = true
        onEvent?.('saved', { key })
      }
    } catch (e) {
      stats.error = `存缓存失败: ${e?.name ?? e}`
      onEvent?.('save-failed', { error: String(e?.message ?? e) })
    }

    const instance = await WebAssembly.instantiate(module, imports)
    return { instance, module }
  }
}

/** 清掉缓存。换 vendor 之后键本来就会变，这个是给"怀疑缓存坏了"时用的。 */
export async function clearWasmCache() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.deleteDatabase(DB_NAME)
    req.onsuccess = () => resolve(true)
    req.onerror = () => reject(req.error)
    req.onblocked = () => resolve(false) // 有别的标签页开着；不算失败
  })
}

/**
 * 测试页与 harness 之间的桥。页面侧只用这三个函数。
 *
 * 为什么不用 `console.log` + `--dump-dom`：那条路要么等不到 wasm 编译完（虚拟时间
 * 对异步 wasm 不友好），要么拿到一份还没跑完的 DOM，而**两种失败看起来都像"测试通过
 * 得很快"**。显式 POST 回报是唯一能把「跑完了」和「还没跑」分开的做法。
 */

export async function log(...parts) {
  const msg = parts.map((p) => (typeof p === 'string' ? p : JSON.stringify(p))).join(' ')
  try {
    await fetch('/__log', { method: 'POST', body: msg })
  } catch {
    /* harness 已经退出时不要再抛，否则会盖住真正的失败原因 */
  }
}

export async function report(result) {
  await fetch('/__result', { method: 'POST', body: JSON.stringify(result) })
}

/**
 * 把整页包成一次「跑完就回报」，并且**保证任何异常都会变成一次回报**。
 *
 * 没有这一层的话，一个同步抛出的错误会让页面静默停住，harness 只能等到超时 ——
 * 而超时的报错信息里没有堆栈，只有"页面没回报"。
 */
export async function runPage(fn) {
  window.addEventListener('error', (e) => report({ ok: false, error: `window.onerror: ${e.message}` }))
  window.addEventListener('unhandledrejection', (e) =>
    report({ ok: false, error: `unhandledrejection: ${e.reason?.message ?? e.reason}` }),
  )
  try {
    const out = await fn()
    await report(out)
  } catch (e) {
    await report({ ok: false, error: `${e?.message ?? e}`, stack: `${e?.stack ?? ''}`.slice(0, 2000) })
  }
}

/** opencv.js 就绪。它是 UMD + 异步 wasm，`cv` 存在不等于能用。 */
export function loadOpenCv(src = '/public/vendor/opencv.js') {
  return new Promise((resolve, reject) => {
    const s = document.createElement('script')
    s.src = src
    s.onerror = () => reject(new Error(`加载 ${src} 失败`))
    s.onload = () => {
      const mod = window.cv
      if (!mod) return reject(new Error('opencv.js 加载后 window.cv 不存在'))
      // @techstark 的构建把 Module 暴露成一个 Promise-like；两种形态都要接。
      if (typeof mod.then === 'function') mod.then(resolve, reject)
      else if (mod.getBuildInformation) resolve(mod)
      else mod.onRuntimeInitialized = () => resolve(window.cv)
    }
    document.head.appendChild(s)
  })
}

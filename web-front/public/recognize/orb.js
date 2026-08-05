/**
 * ORB 提特征。**与服务端 `photoar.features.extract` 逐位等价**，由 `test/golden/` 钉住。
 *
 * 这个模块唯一的职责是：一张图进去，`{pts: Float32Array, desc: Uint8Array, count}` 出来，
 * 而那份 desc 必须能直接和库里 `desc.bin` 的字节做 Hamming 比较。
 *
 * ## 为什么每一步都不能"顺手优化"
 *
 * 整条链上每一步都影响描述子的每一个比特，而错了**全都不报错**：
 *
 * | 步骤 | 顺手改的后果 |
 * |---|---|
 * | resize 的 round | 差一像素 = 另一张图，所有关键点位置全移（见 `pyparity.pyRound`） |
 * | INTER_AREA / INTER_LINEAR 的分界 | 像素值不同 → FAST 角点不同 |
 * | 灰度系数 | 同上（所以走 `COLOR_RGBA2GRAY`，golden 证明它等于服务端的 BGR2GRAY） |
 * | ORB 的 nfeatures / scaleFactor / nlevels | 金字塔层数与每层预算，直接改描述子 |
 * | top-N 的排序稳定性 | 截断边界上取到不同的点 |
 *
 * 所以这里没有任何"自由发挥"的余地，只有对译。
 */
import { resizedSize } from './pyparity.js'
import { installWasmCache } from './wasmcache.js'
import {
  N_LEVELS,
  QUERY_LONG_EDGE,
  QUERY_N_FEATURES,
  SCALE_FACTOR,
} from './consts.js'

/** opencv.js 的 `cv` 命名空间。`init` 之后才有值。 */
let cv = null

/**
 * 加载 opencv.js 并等它的 wasm 真的可用。
 *
 * `window.cv` 存在**不等于**能用：它是 UMD + 异步 wasm 实例化，不同构建暴露的就绪信号
 * 还不一样（Promise / `onRuntimeInitialized` / 直接可用）。三种都接，因为猜错的表现是
 * 第一次调用时抛一个和 wasm 毫无关系的 TypeError。
 */
/**
 * 大头是 `.wasm` 那 11.9MB，不是 `.js`。
 *
 * `tools/split-wasm.mjs` 把内联的 wasm 抽了出去之后，`opencv.js` 只剩 128KB，真正要等的
 * 是它旁边那个 `opencv.wasm`。所以进度必须跟着 wasm 走。
 *
 * **这一条错过一次**：拆分之后 `prefetch` 仍然拿的是 `.js`，于是进度条在 128KB 上瞬间
 * 跑满、文案切成「正在编译」，然后是十几秒静默 —— 那十几秒其实在下 12MB，而屏幕上写着
 * "正在编译"。用户合理地得出结论：每次刷新都在重新编译。
 */
function wasmUrlOf(src) {
  return src.replace(/\.js(\?.*)?$/, '.wasm$1')
}

/**
 * wasm 的 URL **从 opencv.js 里读出来，不猜**。
 *
 * ## 为什么不能猜
 *
 * `tools/split-wasm.mjs` 现在把内容哈希写进那个 URL（`opencv.wasm?v=769a4c9a7b03`），
 * 为的是让 `Cache-Control: immutable` 名副其实。而按 `.js → .wasm` 猜出来的是
 * **没有版本号**的那个 —— 两个不同的缓存键。后果：预取下一遍、`instantiateStreaming`
 * 再下一遍，冷启动付两次 2.43MB，缓存里也存两份。实测抓到过（一次加载传 4.87MB）。
 *
 * 所以这里把 opencv.js 的正文读出来，正则取 `findWasmBinary` 的返回值。那 128KB
 * **本来就要下**（下一步 `import()` 要用），所以不是多一次请求，只是把它提到前面 ——
 * 而它带 ETag，第二次进来是一次 304。
 *
 * 取不到就退回猜的那个：宁可慢（下两遍）也别不加载。
 */
function wasmUrlFrom(js, src) {
  const m = /findWasmBinary\(\)\{return\s*"([^"]+)"\}/.exec(js)
  return m ? m[1] : wasmUrlOf(src)
}

/**
 * 先把 wasm **下下来并报进度**，再交给 `import()`。
 *
 * 为什么要多这一步：动态 `import()` 与 `instantiateStreaming` 都没有进度事件，而这
 * 11.9MB 在手机 4G 上是十几秒的空白。用户看到的是一个不动的"正在加载"，那和卡死无从
 * 区分（而这个页面一旦让人觉得卡死，他就把手机放下了）。
 *
 * 下载两次的风险由 `Cache-Control: public, max-age=31536000, immutable` 挡住：`fetch`
 * 把它放进 HTTP 缓存，紧接着 opencv.js 内部的 `instantiateStreaming(fetch(…))` 命中缓存。
 * **万一没命中也只是慢，不会错** —— 所以这里不用 blob URL 那条"保证只下一次"的路：
 * blob 会丢掉浏览器对同一 URL 的 **wasm 编译缓存**，而那个损失是每次进页面都付的，
 * 正是我们最想留住的东西。
 *
 * 拿不到 `Content-Length`（被压缩传输时会没有）就报 `total: 0`，让 UI 退化成不确定进度条
 * —— 而不是显示一个假的百分比。
 */
async function prefetch(src, onProgress) {
  // 先取 opencv.js（128KB，下一步 import 也要用），从它正文里读出 wasm 的真实 URL。
  // 猜不行，理由见 `wasmUrlFrom`。
  let wasm = wasmUrlOf(src)
  try {
    wasm = wasmUrlFrom(await (await fetch(src)).text(), src)
  } catch { /* 取不到就用猜的，慢但能跑 */ }

  let res
  try {
    res = await fetch(wasm)
  } catch {
    // 预热失败不该让加载失败：instantiateStreaming 自己还会再试一次。
    return
  }
  // 没拆分过的 vendor 没有这个文件（404）。那时 wasm 内联在 js 里，进度只能不报 ——
  // 报一个 0/0 的假进度比不报更糟。
  if (!res.ok || !res.body) return
  // **分母要的是解压后的长度。** 服务端把 wasm 预压成 brotli 发（11.4MB → 2.43MB），
  // 那时 `Content-Length` 是压缩后的，而下面 reader 读到的是解压后的字节 ——
  // 直接拿 Content-Length 当分母，进度条会跑到 470%。服务端为此额外发一个
  // `X-Uncompressed-Length`（见 server/index.js 的 serveStatic）。
  const total = Number(res.headers.get('x-uncompressed-length'))
    || Number(res.headers.get('content-length'))
    || 0
  onProgress?.({ loaded: 0, total })
  let loaded = 0
  const reader = res.body.getReader()
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    loaded += value.byteLength
    onProgress?.({ loaded, total })
  }
  onProgress?.({ loaded, total: total || loaded, done: true, fromCache: cameFromCache(wasm) })
}

/**
 * 这次取到的字节是从 HTTP 缓存来的，还是真走了网络？
 *
 * ## 上一版这个判断是**错的**
 *
 * 它在 fetch **之前**查 `performance.getEntriesByName(...)`，拿"时间线上已经有过这条"
 * 当缓存命中。可 Worker 每次加载都是新的，时间线是空的 —— 于是它恒为 false，
 * 而"引擎从缓存读取"那句文案永远不会出现。这个错让一件真事被埋了很久：
 * **自签证书会让 Chromium 对整个源禁用磁盘缓存**，于是每次进页面都真的重下 11.4MB
 * （实测：同一台手机同一份代码，自签 https 上 71 秒、`http://localhost` 上 1.6 秒）。
 * 判据坏掉之后，那 71 秒看起来就只是"手机慢"。
 *
 * 正确的判据是 fetch **之后**看这条资源的 `transferSize`：Resource Timing L2 规定
 * 缓存命中时它是 0，而 `encodedBodySize` 两种情况下都是真实体积 —— 两个一起看就能把
 * "缓存命中"和"这条根本没记上时间线"分开（后者两个都是 0）。
 */
function cameFromCache(url) {
  const href = new URL(url, location.href).href
  const e = performance.getEntriesByName(href, 'resource').at(-1)
  if (!e || !e.encodedBodySize) return null   // 没数据，别猜
  return e.transferSize === 0
}

/**
 * @param opts.onProgress `({loaded, total, done})` —— 下载 opencv.js 的进度。
 */
export function init(src = '/vendor/opencv.js', opts = {}) {
  if (cv) return Promise.resolve(cv)
  // **必须在 import 之前装**：拦的是 `WebAssembly.instantiate`，而 opencv.js 一被 import
  // 就会调它。装晚了那次编译已经发生，缓存对本次刷新毫无作用。
  if (opts.wasmCache !== false) installWasmCache(opts.onWasmEvent)
  return prefetch(src, opts.onProgress).then(() => new Promise((resolve, reject) => {
    const settle = () => {
      const mod = self.cv
      if (!mod) return reject(new Error('opencv.js 加载后 self.cv 不存在'))
      const done = (m) => {
        cv = m ?? self.cv
        resolve(cv)
      }
      // 三种就绪信号都要接：不同 opencv.js 构建暴露的不一样，而猜错的表现是第一次调用时
      // 抛一个与 wasm 毫无关系的 TypeError。
      if (typeof mod.then === 'function') mod.then(done, reject)
      else if (mod.getBuildInformation) done(mod)
      else mod.onRuntimeInitialized = () => done(self.cv)
    }

    // **一条路：动态 `import()`。** 主线程与 module worker 都走它。
    //
    // ## 前两版都错了，两个教训都留着
    //
    // 第一版在 Worker 里用 `importScripts(src)` —— **module worker 里那是被规范禁止的**
    // （`Module scripts don't support importScripts()`）。而我们的 Worker 必须是 module，
    // 它自己 import 了 pipeline/library/verify 那几个 ESM。
    //
    // 第二版想用 `typeof importScripts === 'function'` 区分 classic 与 module worker ——
    // **那个判断恒为真**：`importScripts` 在 module worker 里仍然挂在
    // `WorkerGlobalScope` 原型上，只是**调用时**才抛。用它做特性检测检测不出任何东西。
    //
    // 而且那条 classic 分支本来就是死代码：这个文件是 ESM（有 import/export），
    // 只能被 `<script type="module">`、module worker 的 import、或动态 import 加载 ——
    // `importScripts` 加载不了 ESM，所以它永远不会在 classic worker 里运行。
    //
    // ## 动态 import 一个 UMD 脚本为什么成立
    //
    // 靠 opencv.js UMD 头的两个性质（查过的，不是赌）：
    //   1. root 传的是 **`globalThis`** 而不是 `this` —— ESM 里顶层 `this` 是 undefined，
    //      传 `this` 的 UMD 在这里会直接抛 TypeError；
    //   2. 分支链的最后一支是无条件的 `root.cv = factory()`，module worker 里既没有
    //      `window` 也（在那一支被求值时）不走 importScripts 分支，正好落在它上面。
    //      主线程则命中 `typeof window === 'object'` 那一支，同样挂到 `globalThis.cv`。
    //
    // 两条路径分别由 `test/golden/worker-smoke.html`（module worker）与
    // `test/golden/orb-golden.html`（主线程）钉住。
    import(/* @vite-ignore */ src).then(
      () => settle(),
      (e) => reject(new Error(`import(${src}) 失败：${e.message}`)),
    )
  }))
}

/** 已经就绪的 `cv`。给同一进程里别的模块（`verify.js`）用，避免各自再 init 一遍。 */
export function opencv() {
  if (!cv) throw new Error('orb.init() 还没完成')
  return cv
}

/** 供测试注入一个已经就绪的 cv（测试页自己加载过 opencv.js 时不必再下 13MB）。 */
export function _setOpenCv(mod) {
  cv = mod
}

/**
 * 一个可复用的 ORB 检测器 + 中间 Mat。
 *
 * 为什么要复用而不是每帧 new：`new cv.ORB()` 会在 wasm 堆上建金字塔缓冲，而这条路径是
 * **每帧**都走的（检测 2 FPS、跟踪 20+ FPS）。每帧新建再 delete 会让 wasm 堆反复涨缩，
 * emscripten 的堆不会还给系统，表现是内存单调上涨然后在手机上被浏览器杀掉。
 */
export class OrbExtractor {
  /**
   * @param opts.longEdge 处理长边。默认查询侧的 1280。
   * @param opts.nFeatures 特征预算。默认查询侧的 4000。
   */
  constructor({ longEdge = QUERY_LONG_EDGE, nFeatures = QUERY_N_FEATURES } = {}) {
    const c = opencv()
    this.longEdge = longEdge
    this.nFeatures = nFeatures
    // 只给前三个参数，与服务端 `cv2.ORB_create(nfeatures, scaleFactor, nlevels)` 一致；
    // 其余走 OpenCV 默认。两种重载都试，因为不同 opencv.js 构建暴露的签名不同，
    // 而"构造成了但参数没进去"是不报错的。
    try {
      this.orb = new c.ORB(nFeatures, SCALE_FACTOR, N_LEVELS)
    } catch {
      this.orb = new c.ORB(nFeatures, SCALE_FACTOR, N_LEVELS, 31, 0, 2, c.ORB_HARRIS_SCORE ?? 0, 31, 20)
    }
    this._small = new c.Mat()
    this._gray = new c.Mat()
    this._desc = new c.Mat()
    this._noMask = new c.Mat()
  }

  /**
   * 提特征。
   *
   * @param src 一个 `cv.Mat`，CV_8UC4（canvas 的 RGBA）或 CV_8UC3（BGR）或 CV_8UC1。
   *   **不接受 RGB**：那会让灰度系数用错通道，而结果只是"识别率低一点"。
   * @returns `{count, pts: Float32Array(count*2), desc: Uint8Array(count*32)}`
   *   pts 的坐标在**缩放后**的图像坐标系里，与服务端 `Features.pts` 同一个约定。
   */
  extract(src) {
    const c = cv
    const rs = resizedSize(src.rows, src.cols, this.longEdge)
    let work = src
    if (rs) {
      // scale < 1 缩小用 INTER_AREA，放大用 INTER_LINEAR —— `resize_to_long_edge` 的
      // 同一条分支。**放大是有收益的**，别加"禁止放大"的保护，理由见 pyparity。
      c.resize(src, this._small, new c.Size(rs.w, rs.h), 0, 0,
        rs.scale < 1 ? c.INTER_AREA : c.INTER_LINEAR)
      work = this._small
    }

    const ch = work.channels()
    if (ch === 1) {
      work.copyTo(this._gray)
    } else {
      // RGBA2GRAY 与服务端的 BGR2GRAY 逐字节等价 —— 这一条是 golden 里单独验过的
      // （`rgba_equiv`），不是推断。所以 canvas 的 RGBA 可以直接进来。
      c.cvtColor(work, this._gray, ch === 4 ? c.COLOR_RGBA2GRAY : c.COLOR_BGR2GRAY)
    }

    const kps = new c.KeyPointVector()
    this.orb.detectAndCompute(this._gray, this._noMask, kps, this._desc)
    const n = kps.size()
    if (n === 0) {
      kps.delete()
      return { count: 0, pts: new Float32Array(0), desc: new Uint8Array(0) }
    }

    // 按 response 降序取前 nFeatures。**必须显式做** —— ORB 的 nfeatures 只是目标值，
    // 服务端 `features.extract` 里那句 `np.argsort(-responses, kind="stable")[:n]`
    // 把「取最强的 N 个」变成了确定性契约，这里是它的对译。
    // ES2019 起 Array#sort 稳定，等值处保持原顺序，与 numpy 的 kind="stable" 同语义。
    const resp = new Float32Array(n)
    const xs = new Float32Array(n)
    const ys = new Float32Array(n)
    for (let i = 0; i < n; i++) {
      const k = kps.get(i)
      resp[i] = k.response
      xs[i] = k.pt.x
      ys[i] = k.pt.y
    }
    kps.delete()

    const idx = new Array(n)
    for (let i = 0; i < n; i++) idx[i] = i
    idx.sort((a, b) => resp[b] - resp[a])
    const take = Math.min(n, this.nFeatures)

    const cols = this._desc.cols
    if (!this._desc.isContinuous()) throw new Error('描述子 Mat 不连续')
    const all = this._desc.data
    const pts = new Float32Array(take * 2)
    const desc = new Uint8Array(take * cols)
    for (let d = 0; d < take; d++) {
      const s = idx[d]
      pts[d * 2] = xs[s]
      pts[d * 2 + 1] = ys[s]
      desc.set(all.subarray(s * cols, s * cols + cols), d * cols)
    }
    return { count: take, pts, desc, descCols: cols }
  }

  /** wasm 堆上的东西必须手动还。漏一个就是每帧泄一块。 */
  delete() {
    this.orb?.delete()
    this._small?.delete()
    this._gray?.delete()
    this._desc?.delete()
    this._noMask?.delete()
    this.orb = this._small = this._gray = this._desc = this._noMask = null
  }
}

/**
 * 从一个 `ImageData`（canvas 的 RGBA）建 Mat。调用方负责 `delete()`。
 *
 * 走 `ImageData` 而不是让调用方直接给 Mat，是因为相机帧唯一的来源就是
 * `drawImage` + `getImageData`，而那一步的宽高与 `data.length` 必须对得上 ——
 * 对不上时 `Mat.data.set()` 会静默截断，表现成图像下半部分是黑的。
 */
export function matFromImageData(imageData) {
  const c = opencv()
  const { width, height, data } = imageData
  if (data.length !== width * height * 4) {
    throw new Error(`ImageData 尺寸不符：${width}×${height} 应有 ${width * height * 4} 字节，实为 ${data.length}`)
  }
  const m = new c.Mat(height, width, c.CV_8UC4)
  m.data.set(data)
  return m
}

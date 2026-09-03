/**
 * 识别 Worker。**主线程只做相机与渲染，识别整件事在这里。**
 *
 * 不是"为了架构好看"：一次全库检测在桌面上实测 1.2 秒，放主线程就是页面冻结 1.2 秒
 * —— 相机预览停住、按钮没反应，用户看到的是网页卡死。而这个页面的全部价值就是那块
 * 跟着照片走的视频，卡住比不贴更糟。
 *
 * ## 消息协议
 *
 * 主线程 → Worker：
 *   `{type:'init', libBuf, opencvUrl}`   libBuf 是 PARL 包（transfer 过来）
 *   `{type:'frame', id, width, height, buf}`  buf 是 RGBA 字节（transfer 过来）
 *   `{type:'reset'}`
 * Worker → 主线程：
 *   `{type:'ready', nPhotos, skipped, hasVocab}`
 *   `{type:'result', id, state, quad, photoId, inliers, reason, ms, photo?}`
 *   `{type:'error', message}`
 *
 * **每帧的像素走 transfer 而不是拷贝**：1280×960 的 RGBA 是 4.9MB，结构化克隆一次要
 * 好几毫秒且会在两边各留一份 —— 而这条路每秒走 30 次。transfer 之后主线程那个 buffer
 * 就废了，所以主线程必须每帧新建（见 `camera.js` 里那段说明）。
 *
 * ## 只处理最新一帧
 *
 * 检测慢于相机帧率，消息会堆积。这里**不排队**：正在忙时直接丢掉新帧，只记住最后一帧。
 * 排队的后果是延迟单调增长 —— 用户已经把手机移开了，Worker 还在算三秒前那一帧，
 * 而算出来的四角会贴到一个已经不在那里的照片上。
 *
 * ## 跟踪帧只解 640
 *
 * 一帧 1280×960 的 RGBA 是 4.9MB，而 `getImageData` 要把它从 GPU 读回来 —— 那笔钱
 * 每帧都付。可跟踪帧根本不需要它：它只跑光流，而光流跑在 640 上（见
 * `pipeline.TRACK_LONG_EDGE`）。所以这里问一句 `pipeline.wantsFull()`：**只有检测帧
 * 与重锚帧**才把全图也画一张、读一遍，其余帧只读 1.2MB 的小图。
 *
 * 全图那条路一个字节都没动（`drawImage(bmp, 0, 0, msg.width, msg.height)` 原样），
 * 因为提特征的像素决定描述子的每一位。
 */
import { init, opencv } from './orb.js'
import { unpack } from './library.js'
import { Pipeline } from './pipeline.js'
import { applyServerConfig } from './consts.js'

let pipeline = null
let busy = false
let pending = null

self.onmessage = async (ev) => {
  const msg = ev.data
  try {
    if (msg.type === 'init') return await onInit(msg)
    if (msg.type === 'reset') {
      pipeline?.reset()
      return
    }
    if (msg.type === 'frame') return onFrame(msg)
  } catch (e) {
    self.postMessage({ type: 'error', message: `${e?.message ?? e}`, stack: `${e?.stack ?? ''}`.slice(0, 1500) })
  }
}

async function onInit(msg) {
  // 进度往主线程转。**节流到每 200ms 或每 5% 一次**：11.9MB 的流会给出上千个 chunk，
  // 每个都 postMessage 一次会让主线程的 rAF 被消息处理挤掉 —— 表现是加载期间进度条
  // 自己卡顿，而那正是进度条要消除的那种观感。
  let lastAt = 0
  let lastPct = -1
  await init(msg.opencvUrl ?? '/vendor/opencv.js', {
    // wasm 编译缓存的命中/未命中要报出去。它决定了这次加载是 1 秒还是 10 秒，
    // 而不报的话"为什么这次快"就没人解释得清。
    onWasmEvent: (name, detail) => self.postMessage({ type: 'wasm', name, detail }),
    onProgress: ({ loaded, total, done, fromCache }) => {
      const pct = total ? Math.floor((loaded / total) * 100) : -1
      const now = Date.now()
      if (!done && now - lastAt < 200 && pct === lastPct) return
      lastAt = now
      lastPct = pct
      self.postMessage({ type: 'progress', stage: 'engine', loaded, total, pct, done: Boolean(done), fromCache })
    },
  })
  // 下载完之后紧接着是 wasm 装配，那一段**没有进度可报**（浏览器不暴露）。但**耗时可报**：
  // `onWasmEvent('streaming', {ms})` 在那一步结束时发出去，主线程据此说清这次是命中了
  // 编译缓存（几十毫秒）还是真编译了（秒级）。那是"刷新之后是不是又编译了"唯一的
  // 可观测量 —— 浏览器的 wasm code cache 是隐式的，没有 API 能查。
  if (msg.thresholds) applyServerConfig(msg.thresholds)
  const lib = unpack(msg.libBuf)
  pipeline?.delete()
  pipeline = new Pipeline(lib)
  self.postMessage({
    type: 'ready',
    nPhotos: lib.photos.length,
    skipped: lib.skipped,
    hasVocab: Boolean(lib.vocab),
    opencvVersion: /OpenCV\s+([0-9.]+)/.exec(opencv().getBuildInformation?.() ?? '')?.[1] ?? 'n/a',
  })
  drain()
}

/**
 * 丢帧要**回执**。
 *
 * 主线程按「在途帧数」限流（见 scan.js 的 `MAX_INFLIGHT`），而它只能靠回消息来减计数。
 * 被丢掉的帧不会产生 `result` —— 不发这条的话那个名额就永久漏掉一个，几次之后主线程
 * 以为一直满着，再也不送帧了，表现是**贴合彻底停住而没有任何报错**。
 */
function ack(id) {
  self.postMessage({ type: 'drop', id })
}

function onFrame(msg) {
  if (!pipeline) {
    // 还没 init 完，丢掉 —— 补齐它没有意义，那一帧早过时了。
    // 但 bitmap **必须 close**：它持有 GPU 内存，丢引用不等于释放。
    msg.bitmap?.close()
    ack(msg.id)
    return
  }
  // 被顶掉的那一帧同理。跟踪 46ms 而送帧更快时这条每秒都在发生 ——
  // 漏一个 close 就是每秒泄漏一张 1280×960 的纹理。
  if (pending) {
    pending.bitmap?.close()
    ack(pending.id)
  }
  pending = msg
  drain()
}

/**
 * `ImageBitmap` → `ImageData`，**在 worker 线程上**，两个尺度各一张画布。
 *
 * 这一段就是从主线程搬过来的那 65ms（真机实测 1280×960；搬到这边之后主线程只付
 * `createImageBitmap` 的 19.4ms）。`drawImage(bmp, 0, 0, w, h)` 与主线程那条
 * `grab()` 是**逐字节同一条路** —— 缩放算法不能换，理由写在 `camera.js` 的
 * `grabBitmap()` 上面。**那条禁令只管全图**（它去提特征）；小图只喂光流，
 * 与描述子无关，所以它可以让 `drawImage` 缩。
 *
 * OffscreenCanvas 各复用一张：每帧新建会让 GPU 侧不停分配纹理。
 */
let offscreen = null, offctx = null
let smallCanvas = null, smallCtx = null
function canvasFor(kind, w, h) {
  const isSmall = kind === 'small'
  let c = isSmall ? smallCanvas : offscreen
  if (!c || c.width !== w || c.height !== h) {
    c = new OffscreenCanvas(w, h)
    const ctx = c.getContext('2d', { willReadFrequently: true, alpha: false })
    if (isSmall) { smallCanvas = c; smallCtx = ctx } else { offscreen = c; offctx = ctx }
  }
  return isSmall ? smallCtx : offctx
}

function drain() {
  if (busy || !pending || !pipeline) return
  const msg = pending
  pending = null
  busy = true
  try {
    let frame
    if (msg.bitmap) {
      const wantFull = pipeline.wantsFull()
      const [sw, sh] = pipeline.smallDims(msg.width, msg.height)
      try {
        // 小图**每帧都画**（1.2MB，便宜），全图只在检测/重锚帧画（4.9MB + 后面的
        // cvtColor/resize）。两张都从同一个 bitmap 画，缩放路径固定，
        // 见 pipeline._toTrackGray 里那段「必须来自同一条路」。
        const small = canvasFor('small', sw, sh)
        small.drawImage(msg.bitmap, 0, 0, sw, sh)
        const smallData = small.getImageData(0, 0, sw, sh)
        let full = null
        if (wantFull) {
          const fc = canvasFor('full', msg.width, msg.height)
          fc.drawImage(msg.bitmap, 0, 0, msg.width, msg.height)   // 与 camera.grab() 逐字节同一条路
          full = fc.getImageData(0, 0, msg.width, msg.height)
        }
        frame = { full, small: smallData, width: msg.width, height: msg.height }
      } finally {
        // **必须 close()**：ImageBitmap 持有 GPU 内存，不关就是真的泄漏。
        // 放 finally 里：上面那几步抛了（画布尺寸非法、2d 上下文丢了）同样得关，
        // 否则一次异常就泄一张 1280×960 的纹理，而这条路每秒走几十次。
        msg.bitmap.close()
      }
    } else {
      // 退化路径（没有 OffscreenCanvas）：只有全图，跟踪灰度图由 pipeline 自己缩（老路）。
      frame = {
        full: { width: msg.width, height: msg.height, data: new Uint8ClampedArray(msg.buf) },
        width: msg.width,
        height: msg.height,
      }
    }
    const out = pipeline.pushFrame(frame)
    // `grabbedAt` 原样带回。主线程的延迟补偿要知道"这个结果测的是多久之前的画面"，
    // 而那**不等于** `now - ms`：帧可能在 `pending` 里排过队（跟踪 44ms > 送帧
    // 间隔 33ms 时必然发生），排队那一段不在 ms 里。
    //
    // `usedFull` 是这一帧到底解了没解全图 —— **唯一能从外面看见「跟踪帧只解 640」
    // 有没有真的生效的量**。不报的话，`wantsFull()` 哪天恒真了（比如 `_wantFull`
    // 粘死）测试与日志全都照旧全绿，而每帧多付 4.9MB。`test/golden/worker-smoke.html`
    // 拿它断言全图帧占比。
    self.postMessage({
      type: 'result',
      id: msg.id,
      grabbedAt: msg.grabbedAt,
      usedFull: Boolean(frame.full),
      ...out,
    })
  } catch (e) {
    self.postMessage({ type: 'error', message: `识别失败：${e?.message ?? e}`, stack: `${e?.stack ?? ''}`.slice(0, 1500) })
    // 这一帧没有 result，同样要还名额 —— 否则一次识别异常就把在途计数永久扣掉一个。
    ack(msg.id)
  } finally {
    busy = false
    // 处理期间又来了帧就接着做。用 setTimeout 而不是直接递归：给消息循环一个机会把
    // 新的 frame 消息收进来，否则会一直用同一份 pending。
    if (pending) setTimeout(drain, 0)
  }
}

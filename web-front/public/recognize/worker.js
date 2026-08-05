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

function onFrame(msg) {
  if (!pipeline) return // 还没 init 完，丢掉——补齐它没有意义，那一帧早过时了
  pending = msg
  drain()
}

function drain() {
  if (busy || !pending || !pipeline) return
  const msg = pending
  pending = null
  busy = true
  try {
    const imageData = { width: msg.width, height: msg.height, data: new Uint8ClampedArray(msg.buf) }
    const out = pipeline.push(imageData)
    self.postMessage({ type: 'result', id: msg.id, ...out })
  } catch (e) {
    self.postMessage({ type: 'error', message: `识别失败：${e?.message ?? e}`, stack: `${e?.stack ?? ''}`.slice(0, 1500) })
  } finally {
    busy = false
    // 处理期间又来了帧就接着做。用 setTimeout 而不是直接递归：给消息循环一个机会把
    // 新的 frame 消息收进来，否则会一直用同一份 pending。
    if (pending) setTimeout(drain, 0)
  }
}

/**
 * 相机。**三平台的坑全部关在这个文件里。**
 *
 * 这是唯一有平台分支的模块，刻意如此：iOS / 鸿蒙 / Android 的差异不该扩散到识别和渲染
 * 里去。下面每一条都是有出处的，不是防御性编程。
 */

/**
 * 请求后置相机。
 *
 * ## 为什么是 `facingMode: {ideal:'environment'}` 而不是 `exact`
 *
 * `exact` 在没有后置相机的设备上（笔记本、部分平板）直接抛 `OverconstrainedError`，
 * 于是页面在开发机上根本打不开。`ideal` 会退回可用的那个，扫不出东西但页面是活的。
 *
 * ## 为什么要 1280
 *
 * `backend.QUERY_LONG_EDGE` 是 1280，而且服务端注释里那张表说得很清楚：**处理长边比
 * 发帧长边更主导**，但"发帧 640 + 处理 1280"只能到 0.5、"1280 + 1280"才到 0.4
 * （占比越小越好）。所以相机也要给到 1280。给 `ideal` 而不是 `min`：拿不到的时候降级
 * 使用比直接失败好，而 `orb.js` 会把小帧**放大**到 1280（那一步实测有收益，
 * 见 `pyparity.resizedSize` 的说明）。
 */
export async function openCamera(video, { longEdge = 1280 } = {}) {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new CameraError('no_api', describeNoApi())
  }
  const constraints = {
    audio: false,
    video: {
      facingMode: { ideal: 'environment' },
      width: { ideal: longEdge },
      height: { ideal: Math.round((longEdge * 3) / 4) },
    },
  }
  let stream
  try {
    stream = await navigator.mediaDevices.getUserMedia(constraints)
  } catch (e) {
    throw new CameraError(e.name ?? 'unknown', describeGumError(e))
  }

  // iOS 必须这三样一起给，少一个就是"画面不动"或者"全屏播放器盖住页面"：
  //  * playsInline —— 否则 iOS 会把 video 拉成全屏原生播放器
  //  * muted —— 没有它自动播放被策略拦掉（相机流没有音轨，但策略只看属性）
  //  * autoplay 之外还要显式 play()，因为 srcObject 变化不一定触发自动播放
  video.playsInline = true
  video.muted = true
  video.autoplay = true
  video.srcObject = stream

  await new Promise((resolve, reject) => {
    const done = () => {
      video.removeEventListener('loadedmetadata', done)
      resolve()
    }
    video.addEventListener('loadedmetadata', done)
    setTimeout(() => reject(new CameraError('timeout', '相机流 10 秒内没有报出尺寸')), 10_000)
  })
  try {
    await video.play()
  } catch (e) {
    // 有些浏览器在没有用户手势时拒绝 play()。调用方应当在一次点击里再调一遍。
    throw new CameraError('play_blocked', `相机画面没能开始播放（${e.name}）。请点一下页面再试。`)
  }
  return stream
}

export class CameraError extends Error {
  constructor(code, message) {
    super(message)
    this.code = code
  }
}

/**
 * `getUserMedia` 根本不存在时，把**真正的原因**说出来。
 *
 * 这段文案是这个页面在中国婚礼现场最可能被用到的一段，所以它必须准确：
 *
 * - **iOS 微信/QQ 等 App 内的 WebView 打不开相机**，这是 Apple 的策略：只有 Safari
 *   本体拿到 WebRTC 能力，第三方 App 的 WKWebView 没有。微信官方明确说过内页 WebRTC
 *   "暂无计划"。所以这里只有一条出路：引导用户在系统浏览器里打开。
 * - **HTTP 页面也没有** `mediaDevices`：它要求安全上下文。局域网 `http://192.168.x.x`
 *   不是安全上下文，而这件事从任何一条报错里都看不出来 —— 排查的人会一直怀疑相机权限，
 *   所以必须单独说清楚。（原生 App 走 LAN http 完全正常，凭那边的经验来的人尤其会踩。）
 */
function describeNoApi() {
  const insecure = !self.isSecureContext
  const inApp = /MicroMessenger|QQ\/|Weibo|DingTalk/i.test(navigator.userAgent)
  const ios = /iPad|iPhone|iPod/.test(navigator.userAgent) ||
    (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1)

  if (insecure) {
    // 只说"要 https"是个死胡同：手机上没有证书的人看到这句话没有下一步可走。
    // 所以把三条真能走通的路都列出来 —— 其中前两条**不需要任何证书**。
    return `这个页面不是安全上下文（当前 ${location.protocol}//${location.host}），浏览器不提供相机。` +
      '局域网的 http 地址不算安全上下文。三条出路：' +
      // 端口写当前这个而不是写死 8964：改过 PHOTOAR_PORT 的部署照着抄才不会白试一次。
      `① 用 USB 连电脑，在电脑的 chrome://inspect 里开「Port forwarding」把 ${location.port || 8964} 转过来，` +
      `手机打开 http://localhost:${location.port || 8964} —— localhost 按规范就是安全上下文，不用证书；` +
      '② 在手机浏览器的 chrome://flags（Edge 是 edge://flags）里搜 ' +
      '"Insecure origins treated as secure"，把本站地址填进去并重启浏览器；' +
      '③ 走隧道用真证书的 https 地址。'
  }
  if (inApp && ios) {
    return '微信/QQ 内置浏览器在 iPhone 上拿不到相机（这是 Apple 的限制，不是权限问题）。' +
      '请点右上角「···」→「在浏览器中打开」，用 Safari 打开本页。'
  }
  if (inApp) {
    return '当前 App 的内置浏览器没有开放相机。请点右上角菜单，选择在系统浏览器中打开本页。'
  }
  return `这个浏览器没有 navigator.mediaDevices.getUserMedia。${ios ? '请用 Safari 打开。' : '请换用较新的 Chrome / Safari / 华为浏览器。'}`
}

function describeGumError(e) {
  switch (e.name) {
    case 'NotAllowedError':
      return '相机权限被拒绝了。请在浏览器地址栏（或系统设置 → 应用权限）里允许本站使用相机，然后刷新。'
    case 'NotFoundError':
    case 'DevicesNotFoundError':
      return '这台设备上找不到可用的相机。'
    case 'NotReadableError':
    case 'TrackStartError':
      return '相机被别的程序占用了。关掉其它正在用相机的 App 再试。'
    case 'OverconstrainedError':
      return `相机不支持请求的规格（${e.constraint ?? '未知约束'}）。`
    case 'SecurityError':
      return '出于安全策略，这个页面不能使用相机。确认地址是 https。'
    default:
      return `打开相机失败：${e.name} ${e.message ?? ''}`
  }
}

/**
 * 从 video 抓一帧 RGBA。
 *
 * ## 为什么每次都新建 ArrayBuffer
 *
 * 抓到的 buffer 要 **transfer** 给识别 Worker（1280×960 的 RGBA 是 4.9MB，每秒 30 次，
 * 结构化克隆的开销与内存都不可接受）。transfer 之后这一侧的 buffer 就被"掏空"了，
 * 所以不能复用一块。`getImageData` 每次返回新的，正好符合。
 *
 * ## 为什么 canvas 要 `willReadFrequently`
 *
 * 不给这个提示时，浏览器倾向于把 canvas 放在 GPU 上，而 `getImageData` 就变成一次
 * GPU→CPU 回读 —— 实测能差好几倍，而且它不报错，只表现成"帧率上不去"。
 */
export class FrameGrabber {
  constructor(video, { longEdge = 1280 } = {}) {
    this.video = video
    this.longEdge = longEdge
    this.canvas = document.createElement('canvas')
    this.ctx = this.canvas.getContext('2d', { willReadFrequently: true, alpha: false })
    this.size = null
  }

  /** @returns `{width, height, data}`（ImageData）或 null（视频还没就绪）。 */
  grab() {
    const v = this.video
    const vw = v.videoWidth
    const vh = v.videoHeight
    if (!vw || !vh) return null

    // 抓帧就抓到查询长边，不多抓。抓 4K 再缩是白付一次 GPU 回读 + 一次 resize，
    // 而识别管线反正要把长边压到 1280。
    const s = Math.min(1, this.longEdge / Math.max(vw, vh))
    const w = Math.max(1, Math.round(vw * s))
    const h = Math.max(1, Math.round(vh * s))
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w
      this.canvas.height = h
      this.size = [w, h]
    }
    this.ctx.drawImage(v, 0, 0, w, h)
    return this.ctx.getImageData(0, 0, w, h)
  }
}

export function stopCamera(stream) {
  for (const t of stream?.getTracks() ?? []) t.stop()
}

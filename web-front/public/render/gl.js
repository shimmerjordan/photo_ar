/**
 * WebGL 渲染：相机画面铺底 + 一块按四个角透视变形的视频。
 *
 * ## 为什么整个画面都走 GL，而不是「video 元素铺底 + canvas 画视频」
 *
 * 后者看起来更省事，但那块视频要**透视变形**，而 CSS transform 只能做仿射
 * （`matrix3d` 能做透视，但它作用在整个元素上、且与 `object-fit` 的裁切叠加之后很难对齐）。
 * 更要紧的是两层各自独立合成，快速移动时会看到相机画面和视频**不同步**一帧 ——
 * 而"贴不牢"正是这个项目一直在修的那个毛病。一个 GL 上下文里画两次，同步是免费的。
 *
 * ## 视频纹理在 iOS 上的两个坑
 *
 * 1. **`display:none` 的 video 不更新纹理。** iOS 14 起 Safari 不再为离屏视频产帧，
 *    表现是纹理冻在第一帧。所以那个 video 元素必须留在布局里，用
 *    `opacity:0; pointer-events:none` 藏起来（见 `index.html`）。
 * 2. **必须 `playsinline` + `muted` 才能自动播。** 否则 iOS 要么拒绝播放、要么把它拉成
 *    全屏原生播放器盖住整个页面。
 */
import { quadUv } from './screenquad.js'

/** 不裁。`drawVideoQuad` 的兜底默认值。（不 freeze：TypedArray 冻不了，见 screenquad.js） */
const FULL_CROP = new Float32Array([0, 0, 1, 1])

const VS_CAMERA = `
attribute vec2 aPos;
attribute vec2 aUv;
varying vec2 vUv;
void main() {
  vUv = aUv;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`

const FS_TEX = `
precision mediump float;
uniform sampler2D uTex;
varying vec2 vUv;
void main() { gl_FragColor = texture2D(uTex, vUv); }`

/**
 * 视频面片的顶点着色器。
 *
 * `aClip` 是 **4 分量的裁剪空间坐标**，直接赋给 `gl_Position` —— w 原样交出去。
 * 这是整个渲染最关键的一行：先把 xy 除以 w 再传就是仿射插值，两个三角形各自线性插值 uv，
 * 结果是纹理沿对角线折一道。斜着看照片时那道折痕非常明显，而且它看起来像"视频变形了"
 * 而不是"插值错了"。把 w 留给光栅化器，透视校正插值是免费的。
 */
const VS_QUAD = `
attribute vec4 aClip;
attribute vec2 aUv;
varying vec2 vUv;
void main() {
  vUv = aUv;
  gl_Position = aClip;
}`

function compile(gl, type, src) {
  const s = gl.createShader(type)
  gl.shaderSource(s, src)
  gl.compileShader(s)
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
    throw new Error(`着色器编译失败：${gl.getShaderInfoLog(s)}`)
  }
  return s
}

function program(gl, vs, fs) {
  const p = gl.createProgram()
  gl.attachShader(p, compile(gl, gl.VERTEX_SHADER, vs))
  gl.attachShader(p, compile(gl, gl.FRAGMENT_SHADER, fs))
  gl.linkProgram(p)
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) {
    throw new Error(`着色器链接失败：${gl.getProgramInfoLog(p)}`)
  }
  return p
}

export class Renderer {
  constructor(canvas) {
    const gl = canvas.getContext('webgl', {
      alpha: false,
      antialias: false,
      // 相机预览不需要保留缓冲，而 preserveDrawingBuffer 会强制每帧多一次拷贝。
      preserveDrawingBuffer: false,
      powerPreference: 'low-power',
    })
    if (!gl) throw new Error('这个浏览器/设备开不出 WebGL 上下文')
    this.gl = gl
    this.canvas = canvas

    this.camProg = program(gl, VS_CAMERA, FS_TEX)
    this.quadProg = program(gl, VS_QUAD, FS_TEX)

    // 相机背景：铺满 NDC 的两个三角形。uv 的 v 翻转，因为纹理坐标原点在左下、
    // 而图像原点在左上。
    this.camPos = buffer(gl, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]))
    this.camUv = buffer(gl, new Float32Array([0, 1, 1, 1, 0, 0, 1, 0]))

    // 视频面片。顶点顺序与 `screenquad.clipVertices` 的输出一致：
    // 照片的左下、右下、左上、右上（TRIANGLE_STRIP）。
    //
    // uv **每帧重算**（`screenquad.quadUv`），因为它现在带着裁剪矩形 —— 视频要盖满
    // 照片，比例对不上时裁掉溢出的那一圈。写死过一版，而且 v 忘了翻，画出来上下颠倒。
    this.quadClip = gl.createBuffer()
    this.quadUv = gl.createBuffer()
    this._uv = new Float32Array(8)

    this.camTex = texture(gl)
    this.videoTex = texture(gl)
    this._clip = new Float32Array(16)
  }

  resize() {
    const c = this.canvas
    const dpr = Math.min(self.devicePixelRatio || 1, 2) // 2 以上纯浪费，手机上还会掉帧
    const w = Math.round(c.clientWidth * dpr)
    const h = Math.round(c.clientHeight * dpr)
    if (c.width !== w || c.height !== h) {
      c.width = w
      c.height = h
    }
    this.gl.viewport(0, 0, c.width, c.height)
    return c.width / c.height
  }

  /** 相机画面。`video` 必须已经在播（`readyState >= 2`），否则这一帧跳过不画。 */
  drawCamera(video, frameAspect, canvasAspect) {
    const gl = this.gl
    if (video.readyState < 2) return false
    gl.bindTexture(gl.TEXTURE_2D, this.camTex)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, video)

    // cover：短边顶满、长边溢出被裁。与 `screenquad.imageToNdc` 是同一道换算的两侧 ——
    // 那边把四个角映进来、这边把画面铺出去，**两者必须用同一个规则**，否则视频和它下面
    // 的照片会整体错位，而错位量随横竖屏变化。
    let sx = 1
    let sy = 1
    if (frameAspect > canvasAspect) sx = frameAspect / canvasAspect
    else sy = canvasAspect / frameAspect
    // 顶点在 NDC 里放大，等价于把纹理裁掉溢出的部分。
    const pos = new Float32Array([-sx, -sy, sx, -sy, -sx, sy, sx, sy])
    gl.bindBuffer(gl.ARRAY_BUFFER, this.camPos)
    gl.bufferData(gl.ARRAY_BUFFER, pos, gl.DYNAMIC_DRAW)

    gl.useProgram(this.camProg)
    bindAttr(gl, this.camProg, 'aPos', this.camPos, 2)
    bindAttr(gl, this.camProg, 'aUv', this.camUv, 2)
    gl.uniform1i(gl.getUniformLocation(this.camProg, 'uTex'), 0)
    gl.activeTexture(gl.TEXTURE0)
    gl.bindTexture(gl.TEXTURE_2D, this.camTex)
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
    return true
  }

  /**
   * 视频面片。
   *
   * @param clip16 `screenquad.clipVertices` 写好的 16 个 float（4 顶点 × xyzw）
   * @param video 视频元素，必须已经有帧（`readyState >= 2`）
   */
  /**
   * @param crop `screenquad.videoCrop` 给出的源矩形。不给就是不裁（整段视频拉满照片，
   *   比例不对时会变形）—— 所以调用方**应当**给。
   */
  drawVideoQuad(clip16, video, crop = FULL_CROP) {
    const gl = this.gl
    if (video.readyState < 2) return false
    gl.bindTexture(gl.TEXTURE_2D, this.videoTex)
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGB, gl.RGB, gl.UNSIGNED_BYTE, video)

    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadClip)
    gl.bufferData(gl.ARRAY_BUFFER, clip16, gl.DYNAMIC_DRAW)

    quadUv(crop, this._uv)
    gl.bindBuffer(gl.ARRAY_BUFFER, this.quadUv)
    gl.bufferData(gl.ARRAY_BUFFER, this._uv, gl.DYNAMIC_DRAW)

    gl.useProgram(this.quadProg)
    bindAttr(gl, this.quadProg, 'aClip', this.quadClip, 4)
    bindAttr(gl, this.quadProg, 'aUv', this.quadUv, 2)
    gl.uniform1i(gl.getUniformLocation(this.quadProg, 'uTex'), 0)
    gl.activeTexture(gl.TEXTURE0)
    gl.bindTexture(gl.TEXTURE_2D, this.videoTex)
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
    return true
  }

  clear() {
    const gl = this.gl
    gl.clearColor(0, 0, 0, 1)
    gl.clear(gl.COLOR_BUFFER_BIT)
  }
}

function buffer(gl, data) {
  const b = gl.createBuffer()
  gl.bindBuffer(gl.ARRAY_BUFFER, b)
  gl.bufferData(gl.ARRAY_BUFFER, data, gl.DYNAMIC_DRAW)
  return b
}

function bindAttr(gl, prog, name, buf, size) {
  const loc = gl.getAttribLocation(prog, name)
  gl.bindBuffer(gl.ARRAY_BUFFER, buf)
  gl.enableVertexAttribArray(loc)
  gl.vertexAttribPointer(loc, size, gl.FLOAT, false, 0, 0)
}

function texture(gl) {
  const t = gl.createTexture()
  gl.bindTexture(gl.TEXTURE_2D, t)
  // CLAMP_TO_EDGE + LINEAR 是视频纹理的唯一可用组合：视频尺寸几乎不会是 2 的幂，
  // 而 WebGL1 对非 2 的幂纹理**只允许**这两个参数。用 REPEAT 或 mipmap 会让纹理
  // 静默变成全黑 —— 不报错。
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
  return t
}

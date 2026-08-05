/**
 * 星露谷素材的组件层。
 *
 * 素材本身由 `tools/extract-art.py` 从游戏包里切出来，落在 `public/art/`。这一层只做
 * 一件事：**把"哪张图、多大、什么语义"收在一处**。散在各页里的 `<img src="/art/…">`
 * 写错一个字不会报错，只会少一张图，而缺图在深色底上几乎看不出来。
 *
 * ## 为什么物件图是 `<img>`，而导航图标是内联 SVG
 *
 * 两者的差别不是实现偏好，是**着色**：
 *
 * - 底栏图标要跟着页签状态换色（选中是墨色、未选中是暗紫），所以必须是 `currentColor`
 *   的矢量 —— 见 `pixelicons.js`。
 * - 稻草人、电视、箱子、Junimo 是**多色的画**，它们的颜色就是它们本身，换色等于换了
 *   一张图。这些用 `<img>`。
 *
 * 混着用不是不一致：一套是字，一套是画。星露谷自己也是这么分的（背包页签是小彩图，
 * 而血条旁边的数字是字）。
 */

/**
 * 物件图。尺寸写死在这里而不是让 CSS 决定：这些是像素画，**只能按整数倍放大**，
 * 而 `width:100%` 之类会给出小数尺寸 —— 那时哪怕有 `image-rendering: pixelated`，
 * 一格像素也会一会儿 3px 一会儿 4px。
 */
const SPRITES = {
  /** 稻草人：站在空荡荡的地里。照片库为空时用。 */
  scarecrow: { src: '/art/scarecrow.png', w: 48, h: 96 },
  /** 电视：素材页。 */
  tv: { src: '/art/tv.png', w: 48, h: 96 },
  /** 箱子：本机缓存。 */
  chest: { src: '/art/chest.png', w: 48, h: 96 },
  /** 星星：认出来了。 */
  star: { src: '/art/star.png', w: 32, h: 32 },
  /** 感叹号气泡：警告。 */
  bang: { src: '/art/bang.png', w: 48, h: 48 },
  /** 打叉气泡：出错了。 */
  cross: { src: '/art/cross.png', w: 48, h: 48 },
  /** 问号气泡：没找到 / 不确定。 */
  query: { src: '/art/query.png', w: 48, h: 48 },
  /** 木箭头（朝上）。方向交给 CSS 旋转 —— 四个方向一张图。 */
  arrow: { src: '/art/arrow.png', w: 20, h: 22 },
}

export const SPRITE_NAMES = Object.keys(SPRITES)

/**
 * 一张物件图。
 *
 * `alt` 默认空串并加 `aria-hidden`：这些图全部是**旁边那句话的配图**，读屏念一遍
 * "稻草人"只会打断那句话。要它有名字就显式传 `alt`。
 */
export function sprite(name, { alt = '', scale = 1, className = 'sprite' } = {}) {
  const s = SPRITES[name]
  if (!s) throw new Error(`没有这张图：${name}`)
  const img = document.createElement('img')
  img.src = s.src
  img.width = s.w * scale
  img.height = s.h * scale
  img.className = className
  img.decoding = 'async'
  img.alt = alt
  if (!alt) img.setAttribute('aria-hidden', 'true')
  return img
}

/**
 * 一只走路的 Junimo。加载态用。
 *
 * 不用 spinner：这个页面的"加载"动辄十几秒（12MB 的 wasm 要下要编），而转圈的圈
 * 只说明"还没好"。一只在原地小跑的绿团子说的是同一件事，但看十几秒不烦。
 *
 * 动画在 CSS 里（`.junimo`），`prefers-reduced-motion` 时它停在第一帧 —— 那是个站姿。
 */
export function junimo() {
  const el = document.createElement('div')
  el.className = 'junimo'
  el.setAttribute('role', 'img')
  el.setAttribute('aria-label', '加载中')
  return el
}

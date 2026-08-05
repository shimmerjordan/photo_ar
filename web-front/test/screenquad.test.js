/**
 * `public/render/screenquad.js` 的几何测试。
 *
 * 这是**整条贴合路上唯一能自动验的部分** —— 往下就是 GL，顶点交出去之后错了没有任何
 * 报错（只会画出一块位置不对、或者纹理沿对角线折一道的视频，而两种都看起来像"视频坏了"）。
 * Android 那边同样的判据有 24 条（`ScreenQuadTest`），这里是等价移植。
 *
 * 跑：`node --test test/`
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  MAX_AREA_FRAC,
  MIN_AREA_FRAC,
  approach,
  clipVertices,
  imageToNdc,
  plausible,
  smoothingAlpha,
  unitSquareH,
  FULL_RECT,
  quadUv,
  videoCrop,
} from '../public/render/screenquad.js'

/** 正对着拍：一个轴对齐的矩形。 */
const FRONTAL = [-0.5, 0.5, 0.5, 0.5, 0.5, -0.5, -0.5, -0.5]
/** 斜着拍：一个真正的透视四边形（上边比下边短）。 */
const OBLIQUE = [-0.3, 0.5, 0.3, 0.5, 0.6, -0.5, -0.6, -0.5]

test('plausible：合法四边形通过', () => {
  assert.equal(plausible(FRONTAL), true)
  assert.equal(plausible(OBLIQUE), true)
})

test('plausible：长度不对、null、NaN、Infinity 全部拒', () => {
  assert.equal(plausible(null), false)
  assert.equal(plausible([]), false)
  assert.equal(plausible([0, 0, 1, 0, 1, 1]), false)
  assert.equal(plausible([0, 0, 1, 0, 1, 1, NaN, 1]), false)
  assert.equal(plausible([0, 0, 1, 0, 1, 1, 0, Infinity]), false)
})

test('plausible：坐标越界拒（照片再大也不该跑到 1e3）', () => {
  assert.equal(plausible([-5, 0, 1, 0, 1, 1, 0, 1]), false)
  assert.equal(plausible([0, 0, 6, 0, 1, 1, 0, 1]), false)
  // 边界内侧要通过 —— 「照片比画面大得多、只有中间一块在画面里」是正常取景
  assert.equal(plausible([-3.9, -3.9, 4.9, -3.9, 4.9, 4.9, -3.9, 4.9]), false, '面积超上限')
  assert.equal(plausible([-1, -1, 2, -1, 2, 2, -1, 2]), true)
})

test('plausible：退化成一条线要拒（否则视频画成一道亮线）', () => {
  // 四点共线，面积 0
  assert.equal(plausible([0, 0, 0.5, 0, 1, 0, 0.5, 0]), false)
  // 面积刚好低于下限
  const s = Math.sqrt(MIN_AREA_FRAC) * 0.9
  assert.equal(plausible([0, 0, s, 0, s, s, 0, s]), false)
  // 刚好高于下限要通过
  const t = Math.sqrt(MIN_AREA_FRAC) * 1.1
  assert.equal(plausible([0, 0, t, 0, t, t, 0, t]), true)
})

/**
 * 面积上限与坐标范围是**两道各自都够不着对方**的闸门，这一条把它们的关系钉住。
 *
 * 坐标范围 [-4, 5] 的跨度是 9，所以轴对齐四边形的面积最大能到 81 —— 比
 * `MAX_AREA_FRAC`(60) 大，也就是面积上限**确实可达**。但边长 `sqrt(60)≈7.75` 的正方形
 * 必须横跨 -3.87..3.87 才装得下，随便从 0 开始画一个就先撞坐标范围了（第一版测试就是
 * 这么写的，于是它其实在测坐标而不是面积）。
 */
test('plausible：面积上限可达，且上限内侧要放行', () => {
  const span = Math.sqrt(MAX_AREA_FRAC)
  assert.ok(span < 9, '坐标跨度 9 必须够得着面积上限，否则这道闸门是死的')
  // 面积 64 > 60，且四个坐标都在 [-4, 5] 内
  assert.equal(plausible([-4, -4, 4, -4, 4, 4, -4, 4]), false, '面积 64 应超上限')
  // 面积 59.3 < 60，同样在范围内 —— 上限内侧必须放行（举着手机贴到照片上就是这一档）
  const s = 3.85
  assert.equal(plausible([-s, -s, s, -s, s, s, -s, s]), true, '面积 59.3 应放行')
})

test('unitSquareH：把单位正方形四角映到目标四角', () => {
  const h = unitSquareH(OBLIQUE)
  assert.ok(h, '不该退化')
  const uv = [[0, 0], [1, 0], [1, 1], [0, 1]]
  for (let i = 0; i < 4; i++) {
    const [u, v] = uv[i]
    const x = h[0] * u + h[1] * v + h[2]
    const y = h[3] * u + h[4] * v + h[5]
    const w = h[6] * u + h[7] * v + h[8]
    assert.ok(Math.abs(x / w - OBLIQUE[i * 2]) < 1e-5, `角 ${i} 的 x 不对`)
    assert.ok(Math.abs(y / w - OBLIQUE[i * 2 + 1]) < 1e-5, `角 ${i} 的 y 不对`)
  }
})

test('unitSquareH：正对着拍走仿射分支，g 与 h 恰好为 0', () => {
  const m = unitSquareH(FRONTAL)
  assert.ok(m)
  assert.equal(m[6], 0)
  assert.equal(m[7], 0)
  assert.equal(m[8], 1)
})

test('unitSquareH：退化（四点共线 / 两点重合）返回 null', () => {
  assert.equal(unitSquareH([0, 0, 1, 1, 2, 2, 3, 3]), null)
  // 三点重合于一处，非仿射分支且分母趋零
  assert.equal(unitSquareH([0, 0, 0, 0, 0, 0, 1, 1]), null)
  assert.equal(unitSquareH([0, 0, 1, 0, 1, 1]), null, '长度不对')
})

test('videoCrop：遍历比例，恰好一个维度不裁、另一个裁到刚好盖满、比例永远是视频的', () => {
  const aspects = [16 / 9, 4 / 3, 3 / 2, 1, 2 / 3, 3 / 4, 9 / 16, 1.85, 0.5625]
  for (const pa of aspects) {
    for (const va of aspects) {
      const [u0, v0, u1, v1] = videoCrop(pa, va)
      const w = u1 - u0
      const hh = v1 - v0
      // 裁的是源图的一块，不能越界
      assert.ok(u0 >= -1e-6 && v0 >= -1e-6 && u1 <= 1 + 1e-6 && v1 <= 1 + 1e-6,
        `${pa}/${va} 裁到了源图外面`)
      // 恰好一个维度整条留着 —— 这就是"最小"：多裁一点就盖不满，少裁一点就变形
      const fullW = Math.abs(w - 1) < 1e-6
      const fullH = Math.abs(hh - 1) < 1e-6
      assert.ok(fullW || fullH, `${pa}/${va} 两个维度都裁了，裁多了`)
      // 留下的这块，按视频原比例算出来的形状必须**正好是照片的形状**：
      // 它会被拉去铺满整张照片，对不上就是变形。
      const got = (w * va) / hh
      assert.ok(Math.abs(got - pa) < 1e-5, `${pa}/${va} 会被拉变形：留下的块是 ${got}`)
      // 居中裁 —— 偏一边裁掉的就不是"溢出的部分"了
      assert.ok(Math.abs(u0 - (1 - w) / 2) < 1e-6 && Math.abs(v0 - (1 - hh) / 2) < 1e-6,
        `${pa}/${va} 没居中`)
    }
  }
})

test('videoCrop：视频比例还不知道时不裁（播放器尚未报 videoWidth）', () => {
  for (const bad of [0, -1, NaN, Infinity, undefined]) {
    assert.deepEqual([...videoCrop(1.5, bad)], [0, 0, 1, 1])
  }
  for (const bad of [0, -1, NaN]) {
    assert.deepEqual([...videoCrop(bad, 1.5)], [0, 0, 1, 1])
  }
})

test('quadUv：v 必须翻过来 —— 不翻的话视频上下颠倒', () => {
  // `texImage2D` 把图像首行（顶部）放在 t=0，而 clipVertices 的第 0 个顶点是照片的
  // **左下角**。所以它要取源图 v 大的那一侧（靠下），也就是 v1。
  //
  // 这一条曾经是错的：uv 写死成 [0,0, 1,0, 0,1, 1,1]（第 0 个顶点拿到 v=0 = 图像顶部），
  // 画出来上下颠倒。而相机背景那块 uv 是翻了的，两处不一致却一直没被发现 ——
  // 因为视频在真机上从来没播出来过，桌面测试又只验顶点不验 uv。
  const out = new Float32Array(8)
  assert.ok(quadUv(FULL_RECT, out))
  assert.deepEqual([...out], [0, 1, 1, 1, 0, 0, 1, 0], '不裁时应当与相机背景同一套翻转')

  // 带裁剪：照片下边取源图的下边（v1），上边取上边（v0）。
  // 逐个近似比较而不是 deepEqual —— Float32 存不下 0.2/0.9 这些十进制小数。
  assert.ok(quadUv(new Float32Array([0.2, 0.1, 0.8, 0.9]), out))
  const want = [0.2, 0.9, 0.8, 0.9, 0.2, 0.1, 0.8, 0.1]
  for (let i = 0; i < 8; i++) {
    assert.ok(Math.abs(out[i] - want[i]) < 1e-6, `第 ${i} 个：${out[i]} ≠ ${want[i]}`)
  }

  assert.equal(quadUv(new Float32Array([0, 0, 1]), out), false, '长度不对要拒绝')
  assert.equal(quadUv(FULL_RECT, new Float32Array(4)), false, '输出缓冲长度不对要拒绝')
})

test('quadUv 与 clipVertices 的顶点顺序对得上：同一个角，位置与纹理指向同一处', () => {
  // 把单位正方形原样映到 NDC（照片正对镜头、铺满画面），然后逐个角核对：
  // 第 i 个顶点的 NDC 位置在画面的哪个角，它的 uv 就该取源图的哪个角。
  const h = unitSquareH([-1, 1, 1, 1, 1, -1, -1, -1])   // 照片四角：左上、右上、右下、左下
  const clip = new Float32Array(16)
  assert.ok(clipVertices(h, FULL_RECT, clip))
  const uv = new Float32Array(8)
  quadUv(FULL_RECT, uv)
  for (let i = 0; i < 4; i++) {
    const x = clip[i * 4] / clip[i * 4 + 3]
    const y = clip[i * 4 + 1] / clip[i * 4 + 3]
    const t = uv[i * 2 + 1]
    // 画面下方（y<0）的顶点，纹理坐标 t 必须是 1（= 图像底部）。反过来同理。
    if (y < 0) assert.ok(Math.abs(t - 1) < 1e-6, `第 ${i} 个顶点在画面下方(y=${y})，t 却是 ${t}`)
    else assert.ok(Math.abs(t) < 1e-6, `第 ${i} 个顶点在画面上方(y=${y})，t 却是 ${t}`)
    void x
  }
})
test('clipVertices：正对着拍时 w 恒为 1', () => {
  const h = unitSquareH(FRONTAL)
  const out = new Float32Array(16)
  assert.equal(clipVertices(h, [0, 0, 1, 1], out), true)
  for (let i = 0; i < 4; i++) assert.equal(out[i * 4 + 3], 1)
})

/**
 * §35.3 点名的那条：**「w 不全是 1」必须单独测**。
 *
 * 正对着拍时 w 恒为 1，所以先除完再传 xy（仿射插值）与正确实现**结果完全一样** ——
 * 别的用例一个都抓不到这个错。只有真透视的四边形能。
 */
test('clipVertices：斜着拍时 w 不全是 1（透视插值的唯一判据）', () => {
  const h = unitSquareH(OBLIQUE)
  const out = new Float32Array(16)
  assert.equal(clipVertices(h, [0, 0, 1, 1], out), true)
  const ws = [out[3], out[7], out[11], out[15]]
  assert.ok(ws.some((w) => Math.abs(w - 1) > 1e-3), `w 全是 1：${ws}`)
  assert.ok(ws.every((w) => w > 0), '同号且为正')
})

test('clipVertices：顶点顺序是纹理的左下→右下→左上→右上', () => {
  const h = unitSquareH(FRONTAL)
  const out = new Float32Array(16)
  clipVertices(h, [0, 0, 1, 1], out)
  const y = (i) => out[i * 4 + 1] / out[i * 4 + 3]
  const x = (i) => out[i * 4] / out[i * 4 + 3]
  // NDC 里 y 向上，纹理左下对应照片 uv 的 v=1（照片下沿）→ NDC 里 y 最小
  assert.ok(y(0) < y(2), '第 0 个顶点应是纹理左下（y 更小）')
  assert.ok(y(1) < y(3), '第 1 个顶点应是纹理右下')
  assert.ok(x(0) < x(1), '第 0 个在左、第 1 个在右')
  assert.ok(x(2) < x(3), '第 2 个在左、第 3 个在右')
})

test('clipVertices：uv 子矩形只缩小四边形，不改朝向', () => {
  const h = unitSquareH(FRONTAL)
  const full = new Float32Array(16)
  const inset = new Float32Array(16)
  clipVertices(h, [0, 0, 1, 1], full)
  clipVertices(h, [0.25, 0.25, 0.75, 0.75], inset)
  const span = (o, i) => Math.abs(o[4] / o[7] - o[0] / o[3])
  assert.ok(span(inset) < span(full), '内嵌之后应该更窄')
  assert.ok(inset[1] / inset[3] > full[1] / full[3], '下沿应该往上收')
})

test('clipVertices：w 太小 / 异号要拒（跨越无穷远线）', () => {
  const out = new Float32Array(16)
  // 人造一个 w 在 uv 范围内穿过 0 的矩阵
  const crossing = new Float32Array([1, 0, 0, 0, 1, 0, 2, 0, -1])
  assert.equal(clipVertices(crossing, [0, 0, 1, 1], out), false)
  // w 恒为 0
  const zeroW = new Float32Array([1, 0, 0, 0, 1, 0, 0, 0, 0])
  assert.equal(clipVertices(zeroW, [0, 0, 1, 1], out), false)
})

test('clipVertices：四个 w 全负时整体取反（GL 要求 w > 0）', () => {
  const h = unitSquareH(FRONTAL)
  const neg = new Float32Array(h.map((v) => -v))
  const out = new Float32Array(16)
  assert.equal(clipVertices(neg, [0, 0, 1, 1], out), true)
  for (let i = 0; i < 4; i++) assert.ok(out[i * 4 + 3] > 0, '取反后 w 必须为正')
  // 取反后的齐次坐标除出来应该和原来一样（同一个点）
  const ref = new Float32Array(16)
  clipVertices(h, [0, 0, 1, 1], ref)
  for (let i = 0; i < 4; i++) {
    assert.ok(Math.abs(out[i * 4] / out[i * 4 + 3] - ref[i * 4] / ref[i * 4 + 3]) < 1e-6)
  }
})

test('clipVertices：入参长度不对时返回 false 而不是抛', () => {
  assert.equal(clipVertices(new Float32Array(8), [0, 0, 1, 1], new Float32Array(16)), false)
  assert.equal(clipVertices(new Float32Array(9), [0, 0, 1], new Float32Array(16)), false)
  assert.equal(clipVertices(new Float32Array(9), [0, 0, 1, 1], new Float32Array(8)), false)
})

test('smoothingAlpha：与帧率无关 —— 帧率翻倍时两帧走到同一个地方', () => {
  const dt = 33
  const a1 = smoothingAlpha(dt)
  const a2 = smoothingAlpha(dt / 2)
  // 一帧走 a1 之后剩 (1-a1)；两个半帧之后剩 (1-a2)^2。两者应当相等。
  assert.ok(Math.abs((1 - a1) - (1 - a2) ** 2) < 1e-6)
})

test('smoothingAlpha：第一帧与时钟回跳直接跳到目标', () => {
  assert.equal(smoothingAlpha(0), 1)
  assert.equal(smoothingAlpha(-5), 1)
  assert.equal(smoothingAlpha(33, 0), 1)
})

test('smoothingAlpha：始终落在 [0,1]', () => {
  for (const dt of [1, 16, 33, 100, 1000, 1e9]) {
    const a = smoothingAlpha(dt)
    assert.ok(a >= 0 && a <= 1, `dt=${dt} → ${a}`)
  }
})

test('approach：逐元素逼近，长度不匹配时什么都不做', () => {
  const cur = new Float32Array([0, 0, 0, 0])
  approach(cur, new Float32Array([1, 2, 3, 4]), 0.5)
  assert.deepEqual([...cur], [0.5, 1, 1.5, 2])
  approach(cur, new Float32Array([1, 2]), 0.5)
  assert.deepEqual([...cur], [0.5, 1, 1.5, 2], '长度不匹配应当不动')
  const clamp = new Float32Array([0])
  approach(clamp, new Float32Array([10]), 5)
  assert.equal(clamp[0], 10, 'alpha 应被夹到 1')
})

test('imageToNdc：宽高比相同时就是普通的 y 翻转', () => {
  const out = new Float32Array(8)
  imageToNdc([0, 0, 1, 0, 1, 1, 0, 1], 4 / 3, 4 / 3, out)
  assert.deepEqual([...out], [-1, 1, 1, 1, 1, -1, -1, -1])
})

test('imageToNdc：相机图比画布宽时，左右溢出（cover 裁切）', () => {
  // 相机 4:3 铺进 9:16 的竖屏画布：短边（宽）顶满会露黑边，所以是高度顶满、左右溢出。
  const out = new Float32Array(8)
  imageToNdc([0, 0.5, 1, 0.5, 1, 0.5, 0, 0.5], 4 / 3, 9 / 16, out)
  const sx = (4 / 3) / (9 / 16)
  assert.ok(Math.abs(out[0] + sx) < 1e-6, `左沿应到 ${-sx}`)
  assert.ok(Math.abs(out[2] - sx) < 1e-6, `右沿应到 ${sx}`)
  assert.ok(Math.abs(out[1]) < 1e-6, 'v=0.5 应在垂直中心')
})

test('imageToNdc：相机图比画布高时，上下溢出', () => {
  const out = new Float32Array(8)
  imageToNdc([0.5, 0, 0.5, 1, 0.5, 1, 0.5, 0], 3 / 4, 16 / 9, out)
  const sy = (16 / 9) / (3 / 4)
  assert.ok(Math.abs(out[1] - sy) < 1e-6, `上沿应到 ${sy}`)
  assert.ok(Math.abs(out[3] + sy) < 1e-6, `下沿应到 ${-sy}`)
  assert.ok(Math.abs(out[0]) < 1e-6, 'u=0.5 应在水平中心')
})

test('imageToNdc：越界的角照样映出去（照片比画面大是正常的）', () => {
  const out = new Float32Array(8)
  imageToNdc([-0.2, -0.2, 1.2, -0.2, 1.2, 1.2, -0.2, 1.2], 1, 1, out)
  assert.ok(out[0] < -1 && out[2] > 1, '越界不该被夹')
})

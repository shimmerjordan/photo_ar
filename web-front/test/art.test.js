/**
 * 素材与配色的守门测试。
 *
 * 这一套盯的全是**不报错的失败** —— 换皮之后最可能出的岔子没有一个会抛异常：
 *
 * - 少一张图：深色底上的一块透明，跟"这里本来就没东西"看不出区别。
 * - 颜色表两处不同步：按钮的木框和它旁边的分隔线成了两个色，只有并排看才发现。
 * - 前景背景撞色：某个 `.p.dim` 落进了深色容器里，字变成一坨看不清的紫。
 * - 字号不是 12 的整数倍：点阵字的竖笔一根 1px 一根 2px。这是最难自查的一条 ——
 *   截图上看着就是"有点糊"，而人会以为是屏幕的问题。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'
import assert from 'node:assert/strict'

const ROOT = new URL('..', import.meta.url).pathname
const ART = join(ROOT, 'public', 'art')
const CSS = readFileSync(join(ROOT, 'public', 'theme.css'), 'utf8')

/** public/ 下的全部前端源码（不含 vendor：opencv.js 是 13MB 的机器生成代码）。 */
function sources(dir = join(ROOT, 'public'), out = []) {
  for (const name of readdirSync(dir)) {
    if (name === 'vendor' || name === 'art') continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) sources(p, out)
    else if (/\.(js|css|html)$/.test(name)) out.push(p)
  }
  return out
}

const SOURCE_TEXT = sources().map((f) => `/* ${f} */\n${readFileSync(f, 'utf8')}`).join('\n')

// ── 引用完整性 ────────────────────────────────────────────────────────
test('源码里引用的每一张 /art/ 图都真的在', () => {
  const refs = [...SOURCE_TEXT.matchAll(/\/art\/([\w.-]+\.(?:png|woff2))/g)].map((m) => m[1])
  assert.ok(refs.length > 10, `只找到 ${refs.length} 处引用，正则大概是失效了`)
  const missing = [...new Set(refs)].filter((f) => {
    try {
      return !statSync(join(ART, f)).isFile()
    } catch {
      return true
    }
  })
  assert.deepEqual(missing, [], `这些图被引用了但不存在：${missing.join(', ')}`)
})

test('切出来的每一张图都被用上了（没用的图就该从提取脚本里删掉）', () => {
  const manifest = JSON.parse(readFileSync(join(ART, 'manifest.json'), 'utf8'))
  const unused = Object.keys(manifest).filter((f) => !SOURCE_TEXT.includes(f))
  assert.deepEqual(unused, [], `这些图切出来了但没人用：${unused.join(', ')}`)
})

test('manifest 与磁盘上的文件一一对应', () => {
  const manifest = JSON.parse(readFileSync(join(ART, 'manifest.json'), 'utf8'))
  const onDisk = readdirSync(ART).filter((f) => f.endsWith('.png') && !f.startsWith('_'))
  assert.deepEqual(onDisk.sort(), Object.keys(manifest).sort())
})

test('九宫格的切片宽度与它的图匹配', () => {
  const manifest = JSON.parse(readFileSync(join(ART, 'manifest.json'), 'utf8'))
  for (const [file, meta] of Object.entries(manifest)) {
    if (!file.startsWith('frame')) continue
    // CSS 里写的切片数必须等于 manifest 里的。差一格，圆角就会错位 ——
    // 而错位的表现是"边框看起来有点脏"，不是任何一种报错。
    const re = new RegExp(`url\\("/art/${file.replace('.', '\\.')}"\\)\\s+(\\d+)\\s+fill`)
    const m = CSS.match(re)
    if (!m) continue                       // 只有 -s 那套在 CSS 里直接写了切片
    assert.equal(Number(m[1]), meta.slice, `${file} 的切片：CSS 写 ${m[1]}，图是 ${meta.slice}`)
    // 边框宽度也必须等于切片，否则四角会被缩放（那就不是 1:1 像素了）。
    assert.ok(meta.size[0] === meta.slice * 4, `${file} ${meta.size[0]}px 不是切片 ${meta.slice} 的 4 倍`)
  }
})

// ── 颜色两处同源 ──────────────────────────────────────────────────────
test('theme.css 的木框六色与 extract-art.py 的 WOOD 表逐个相同', () => {
  const py = readFileSync(join(ROOT, 'tools', 'extract-art.py'), 'utf8')
  const wood = py.match(/^WOOD = \{$([\s\S]*?)^\}$/m)
  assert.ok(wood, '在 extract-art.py 里找不到 WOOD 表')
  const fromPy = {}
  for (const m of wood[1].matchAll(/"(\w+)":\s*\((0x[0-9A-Fa-f]+),\s*(0x[0-9A-Fa-f]+),\s*(0x[0-9A-Fa-f]+)\)/g)) {
    fromPy[m[1]] = `#${[m[2], m[3], m[4]].map((h) => Number(h).toString(16).padStart(2, '0')).join('')}`
  }
  const NAMES = { outer: '--wood-outer', dark: '--wood-dark', mid: '--wood-mid', wood: '--wood', bevel: '--wood-bevel', face: '--face' }
  for (const [key, cssVar] of Object.entries(NAMES)) {
    const m = CSS.match(new RegExp(`${cssVar}:\\s*(#[0-9a-f]{6})`))
    assert.ok(m, `theme.css 里没有 ${cssVar}`)
    assert.equal(m[1], fromPy[key], `${cssVar} 与 WOOD["${key}"] 对不上`)
  }
})

// ── 对比度 ────────────────────────────────────────────────────────────
function lum(hex) {
  const n = hex.replace('#', '')
  const ch = [0, 2, 4].map((i) => {
    const c = parseInt(n.slice(i, i + 2), 16) / 255
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
}
const contrast = (a, b) => {
  const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p)
  return (x + 0.05) / (y + 0.05)
}
/** theme.css 的 :root 里某个变量的值。 */
const tok = (name) => {
  const m = CSS.match(new RegExp(`${name}:\\s*(#[0-9a-f]{3,8})`))
  assert.ok(m, `theme.css 里没有 ${name}`)
  return m[1]
}

test('每一对会同时出现的前景/背景都过 4.5:1', () => {
  // 这张表就是"哪种字会落在哪种底上"的完整清单。加新组件时如果引入了新的组合，
  // 这里也要加一行 —— 漏掉的那一行不会红，但那正是该盯着的地方。
  const pairs = [
    ['--ink', '--face'], ['--ink-dim', '--face'], ['--ink-bad', '--face'], ['--ink-ok', '--face'],
    ['--lit', '--face-night'], ['--lit-dim', '--face-night'], ['--lit-bad', '--face-night'], ['--gold', '--face-night'],
    ['--lit', '--bg'], ['--lit-dim', '--bg'], ['--lit-bad', '--bg'], ['--gold', '--bg'],
    ['--gold', '--bar'], ['--lit-dim', '--bar'], ['--lit', '--bar'],
  ]
  const bad = []
  for (const [f, b] of pairs) {
    const v = contrast(tok(f), tok(b))
    if (v < 4.5) bad.push(`${f} on ${b} = ${v.toFixed(2)}:1`)
  }
  assert.deepEqual(bad, [], `这些组合读不清：\n  ${bad.join('\n  ')}`)
})

test('占位符文字与正文同一档对比度（不是默认的浅灰）', () => {
  // 这一条单列，因为它是最常见的漏网之鱼：`::placeholder` 不写就是浏览器的浅灰，
  // 而它落在桃色输入框里只有 2 点几比 1。
  const m = CSS.match(/input::placeholder\s*\{[^}]*color:\s*var\((--[\w-]+)\)/)
  assert.ok(m, 'input::placeholder 没有显式指定颜色')
  assert.ok(contrast(tok(m[1]), tok('--face')) >= 4.5, '占位符在桃色输入框上不够清楚')
})

// ── 点阵字体的整数倍约束 ──────────────────────────────────────────────
test('所有字号都是 12 的整数倍', () => {
  const bad = []
  for (const m of CSS.matchAll(/font-size:\s*([\d.]+)px/g)) {
    if (Number(m[1]) % 12 !== 0) bad.push(`${m[1]}px`)
  }
  // 简写 `font: 12px/20px …` 也要查
  for (const m of CSS.matchAll(/font:\s*([\d.]+)px\//g)) {
    if (Number(m[1]) % 12 !== 0) bad.push(`${m[1]}px（简写）`)
  }
  assert.deepEqual(bad, [], `点阵字体只在 12 的整数倍上是清楚的，这些不是：${bad.join(', ')}`)
})

test('所有行高都是整数 px（不是倍数、不是小数）', () => {
  const bad = []
  for (const m of CSS.matchAll(/line-height:\s*([^;]+);/g)) {
    const v = m[1].trim()
    if (!/^\d+px$/.test(v)) bad.push(v)
  }
  for (const m of CSS.matchAll(/font:\s*[\d.]+px\/([^\s]+)\s/g)) {
    if (!/^\d+px$/.test(m[1])) bad.push(`${m[1]}（简写）`)
  }
  assert.deepEqual(bad, [], `行高写成倍数或小数会让字落在半像素上：${bad.join(', ')}`)
})

test('字距只用整数 px（em 会产生半像素偏移）', () => {
  const bad = [...CSS.matchAll(/letter-spacing:\s*([^;]+);/g)]
    .map((m) => m[1].trim())
    .filter((v) => !/^-?\d+px$/.test(v) && v !== 'normal')
  assert.deepEqual(bad, [], `字距不是整数 px：${bad.join(', ')}`)
})

// ── 像素画里没有模糊 ──────────────────────────────────────────────────
test('没有圆角、没有带模糊的阴影、没有渐变', () => {
  const offences = []
  for (const m of CSS.matchAll(/border-radius:\s*([^;]+);/g)) {
    if (m[1].trim() !== '0') offences.push(`border-radius: ${m[1].trim()}`)
  }
  // drop-shadow 的第三个参数是模糊半径，必须是 0
  for (const m of CSS.matchAll(/drop-shadow\(([^)]*)\)/g)) {
    const parts = m[1].trim().split(/\s+/)
    if (parts[2] && parts[2] !== '0') offences.push(`drop-shadow 模糊 ${parts[2]}`)
  }
  // box-shadow：inset 的第四个数、非 inset 的第三个数是模糊
  for (const m of CSS.matchAll(/box-shadow:\s*([^;]+);/g)) {
    const v = m[1].trim()
    if (v === 'none') continue
    const nums = v.match(/-?[\d.]+px/g) ?? []
    const blur = v.startsWith('inset') ? nums[2] : nums[2]
    if (blur && blur !== '0px') offences.push(`box-shadow 模糊 ${blur}`)
  }
  for (const m of CSS.matchAll(/(linear|radial|conic)-gradient/g)) offences.push(`${m[1]}-gradient`)
  assert.deepEqual(offences, [], `像素画里不该有这些：${offences.join(', ')}`)
})

// ── 字体子集 ──────────────────────────────────────────────────────────
//
// 覆盖范围由 make-font.py 在**真正做子集的那段代码里**导出成区间表。不在这里解析
// woff2：那要实现 woff2 的表变换（不是普通 brotli），而那件事跟这个项目毫无关系。
// 区间表与产物同一次生成，所以它不会脱节。
test('界面里出现的每一个字符都在字体子集里', () => {
  const cov = JSON.parse(readFileSync(join(ROOT, 'tools', 'pixel-coverage.json'), 'utf8'))
  const covered = (cp) => cov.ranges.some(([a, b]) => cp >= a && cp <= b)

  // 只查会显示给用户看的那些字：注释里的字随便写，漏了也无所谓。
  const strings = [
    ...SOURCE_TEXT.matchAll(/text:\s*[`'"]([^`'"]*)[`'"]/g),
    ...SOURCE_TEXT.matchAll(/tip\(`([^`]*)`/g),
    ...SOURCE_TEXT.matchAll(/label:\s*'([^']*)'/g),
    ...SOURCE_TEXT.matchAll(/hint:\s*'([^']*)'/g),
    ...SOURCE_TEXT.matchAll(/>([^<>{}]*[一-鿿][^<>{}]*)</g),
  ].map((m) => m[1]).join('')
  assert.ok(strings.length > 800, `只抽到 ${strings.length} 个界面字符，正则大概失效了`)

  const missing = [...new Set(strings)].filter((c) => c.trim() && !covered(c.codePointAt(0)))
  assert.deepEqual(missing, [],
    `这些字在界面上，但字体里没有（会回退到系统字，一屏两种字形）：${missing.join('')}`)
})

test('字体子集存在且体积合理', () => {
  const st = statSync(join(ART, 'pixel.woff2'))
  assert.ok(st.size > 50_000, `字体只有 ${st.size} B，子集大概漏了`)
  assert.ok(st.size < 400_000, `字体 ${st.size} B 太大了，首屏要等它`)
})

test('CSS 引用字体时带了版本串（否则改了字体也推不下去）', () => {
  // 字体走 immutable 缓存（server/index.js 里对 .woff2 的那条），没有 `?v=` 的话
  // 浏览器会抱着旧字体一整年。
  assert.match(CSS, /url\("\/art\/pixel\.woff2\?v=\d+"\)/)
})

test('标题标签不吃浏览器的默认字号', () => {
  // h1 默认 2em（24px，正好）、h2 默认 1.5em（18px，**不是 12 的倍数**）。
  // 前面那条"所有字号都是 12 的整数倍"只查显式声明，查不到"没写"这种情况 ——
  // 而这一处正是漏网之鱼：区块标题一直在 18px 下渲染，糊了一屏。
  assert.match(CSS, /h1,\s*h2,\s*h3\s*\{[^}]*font-size:\s*inherit/,
    'theme.css 里要有一条 h1,h2,h3 { font-size: inherit } 把默认字号收回来')
})

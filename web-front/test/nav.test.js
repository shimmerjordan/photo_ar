/**
 * 导航与图标。**这两样各自守着一个只能靠"装机点一遍"才发现的错。**
 *
 * - `navpolicy`：「访客身上少挡了一个页签」在管理员账号上永远看不出来，得拿访客身份
 *   登进去才会现形 —— 而那正是最容易忘的一步。
 * - `pixelicons`：底栏的「扫一扫」和「照片」曾经用了**同一个** Home 图标，两个页签长得
 *   一样、只能靠文字区分。所以这里有一条「没有两张图标是一样的」。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import {
  ADMIN_TABS, Page, TAB_META, Tab, VIEWER_TABS,
  isRoot, landingTab, needsAdmin, tabAfterRoleChange, tabsFor,
} from '../public/navpolicy.js'
import { ICON_NAMES, icon, iconCells } from '../public/pixelicons.js'
import { formatHash, parseHash } from '../public/shell.js'
import { LANDSCAPE_PORTRAIT_PAIRS, PRINT_SIZES } from '../public/printsize.js'

describe('navpolicy', () => {
  test('访客只有扫描与设置', () => {
    assert.deepEqual(tabsFor(false), [Tab.SCAN, Tab.SETTINGS])
    // 「历史」不给访客不是因为界面挤 —— /v1/history 在服务端是 admin only（全库记录）。
    assert.ok(!VIEWER_TABS.includes(Tab.ADMIN))
    assert.ok(!VIEWER_TABS.includes(Tab.PHOTOS))
    assert.ok(!VIEWER_TABS.includes(Tab.MEDIA))
  })

  test('管理员的页签包含访客的全部', () => {
    for (const t of VIEWER_TABS) assert.ok(ADMIN_TABS.includes(t), `管理员少了 ${t}`)
  })

  test('两种角色都落在扫描页（与 Android 刻意不同）', () => {
    // Android 让管理员落在照片库。网页版的入口是宾客扫码打开的链接，
    // 扫描就是它存在的理由 —— 所以两种角色都落在扫描页。
    assert.equal(landingTab(), Tab.SCAN)
  })

  test('角色降级时把栈收回一个还能待的页签', () => {
    // 场景：管理员登出、家里人用访客身份登进来，而界面还停在「素材」页。
    // 不换的话那一页上每个按钮都会 403。
    assert.equal(tabAfterRoleChange(Tab.MEDIA, false), Tab.SCAN)
    assert.equal(tabAfterRoleChange(Tab.SETTINGS, false), Tab.SETTINGS, '两边都有的页签要留着')
    assert.equal(tabAfterRoleChange(Tab.MEDIA, true), Tab.MEDIA, '还是管理员就不该动')
  })

  test('needsAdmin 与服务端的 admin-only 接口一一对应，不多不少', () => {
    for (const p of [Tab.PHOTOS, Tab.MEDIA, Tab.ADMIN, Page.DETAIL, Page.PLAY, Page.HISTORY]) {
      assert.equal(needsAdmin(p), true, `${p} 该要 admin`)
    }
    // 这三个访客必须能用：扫描是他来这里的理由，设置里有登出，缓存是他自己的浏览器。
    for (const p of [Tab.SCAN, Tab.SETTINGS, Page.CACHE]) {
      assert.equal(needsAdmin(p), false, `${p} 不该要 admin`)
    }
  })

  test('每个页签都有显示名与图标，且图标名都存在', () => {
    for (const t of Object.values(Tab)) {
      const m = TAB_META[t]
      assert.ok(m?.label, `${t} 没有显示名`)
      assert.ok(ICON_NAMES.includes(m.icon), `${t} 的图标 ${m.icon} 不存在`)
    }
  })

  test('isRoot 只认页签', () => {
    assert.equal(isRoot(Tab.SCAN), true)
    assert.equal(isRoot(Page.DETAIL), false)
  })
})

describe('hash 路由', () => {
  test('往返', () => {
    assert.deepEqual(parseHash('#/photos'), { name: 'photos', params: {} })
    assert.deepEqual(parseHash('#/detail/abc123'), { name: 'detail', params: { id: 'abc123' } })
    assert.equal(formatHash({ name: 'photos', params: {} }), '#/photos')
    assert.equal(formatHash({ name: 'detail', params: { id: 'abc' } }), '#/detail/abc')
  })

  test('空 hash 与垃圾 hash 返回 null（调用方回落地页）', () => {
    assert.equal(parseHash(''), null)
    assert.equal(parseHash('#'), null)
    assert.equal(parseHash('#/'), null)
  })

  test('id 会被百分号编码，含斜杠的 id 不会撑破路由', () => {
    const h = formatHash({ name: 'detail', params: { id: 'a/b c' } })
    assert.ok(!h.slice(9).includes('/'), `id 里的斜杠没被编码：${h}`)
    assert.equal(parseHash(h).params.id, 'a/b c')
  })
})

describe('pixelicons', () => {
  test('没有两张图标是一样的', () => {
    // Android 那边改造前「扫一扫」和「照片」用了同一个 Home 图标 —— 底栏是这个界面
    // 唯一的全局导航，两个格子长得一样等于没有导航。
    const seen = new Map()
    for (const n of ICON_NAMES) {
      const cells = iconCells(n)
      const dup = seen.get(cells)
      assert.equal(dup, undefined, `${n} 与 ${dup} 长得一样`)
      seen.set(cells, n)
    }
  })

  test('每张都在 16×16 内，且渲染成 crispEdges 的 SVG', () => {
    for (const n of ICON_NAMES) {
      const rows = iconCells(n).split('|')
      assert.ok(rows.length <= 16, `${n} 有 ${rows.length} 行`)
      for (const r of rows) assert.ok(r.length <= 16, `${n} 有 ${r.length} 列`)
      const svg = icon(n)
      // crispEdges 是这套风格的硬要求 —— 缺了它 16 格的边会被抗锯齿抹成灰。
      assert.match(svg, /shape-rendering="crispEdges"/)
      assert.match(svg, /viewBox="0 0 16 16"/)
      // 颜色跟随文字：底栏选中/未选中只改一个 CSS 变量。
      assert.match(svg, /fill="currentColor"/)
      assert.match(svg, /<rect /, `${n} 一个格子都没画`)
    }
  })

  test('每张图标都不是空的（全空格会静默渲染成看不见的按钮）', () => {
    for (const n of ICON_NAMES) {
      assert.ok(iconCells(n).includes('#'), `${n} 是空图`)
    }
  })

  test('不存在的图标名要抛，而不是渲染空白', () => {
    assert.throws(() => icon('nope'), /没有这张图标/)
  })
})

// 这一组以前还有一条"毫米数逐个对得上 PrintSize.kt"，用来盯着两个代码库里手抄的
// 同一组数字。安卓那一半 2026-08-05 下线了，那条比对也就没有另一边可比 —— 删掉，
// 而不是留一个永远走 catch 分支静默跳过的空测试（那种测试比没有更坏：它看起来在保护
// 什么）。
describe('相纸预设', () => {
  test('横放一定比竖放宽', () => {
    // 这些数字是手抄进来的。抄错（把 102 写在「6寸 横」上）不会有任何一处报错，
    // 只会让 catalog 里那一列悄悄记错。
    for (const [l, p] of LANDSCAPE_PORTRAIT_PAIRS) {
      const a = PRINT_SIZES.find((s) => s.key === l)
      const b = PRINT_SIZES.find((s) => s.key === p)
      assert.ok(a && b, `${l}/${p} 少了一个`)
      assert.ok(a.widthMm > b.widthMm, `${l}(${a.widthMm}) 应当比 ${p}(${b.widthMm}) 宽`)
    }
  })

  test('每一档都有能照着做的提示，且「不知道」那档说清楚留空没有代价', () => {
    for (const s of PRINT_SIZES) assert.ok(s.hint?.length > 4, `${s.key} 没有提示`)
    const unknown = PRINT_SIZES.find((s) => s.widthMm === 0)
    // 网页版按四个角贴，不看物理宽度。留空**真的**没有代价，那句提示必须这么说 ——
    // 上一版沿用的是安卓时代的文案（"要轻轻晃一下手机才贴得上"），那在网页上是假的。
    assert.match(unknown.hint, /不看|不影响|一样能/, '「不知道」那档要说明留空不影响识别')
  })
})

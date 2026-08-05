/**
 * `DiagLog` 的折叠逻辑。
 *
 * 值得单独测，因为它的失效方式是**静默丢掉关键行**：贴不上时有些行每秒一条，折叠错了
 * 就会把「视频 error code=4」那种只出现一次的行顶出窗口 —— 而那一行恰恰是唯一有信息量
 * 的。Android 那边同样的逻辑也有一条测试盯着（§33.3）。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { DiagLog, MEDIA_ERR, NETWORK_STATE, READY_STATE, short } from '../public/diag.js'

test('同模板的行按数字聚合成一条，并给出范围', () => {
  const d = new DiagLog()
  d.push('跟踪 ok 内点=23 10ms', 1000)
  d.push('跟踪 ok 内点=24 11ms', 1100)
  d.push('跟踪 ok 内点=12 144ms', 1200)
  const t = d.text()
  // 这三行**文本各不相同**，第一版的"相邻相同折叠"一条都折不掉 —— 真机上 40 行窗口
  // 一秒就被刷满，来不及复制。
  assert.equal(d.groups.size, 1)
  assert.match(t, /×3/)
  assert.match(t, /内点=12~24/)
  assert.match(t, /10~144ms/)
})

test('模板不同的行各自成组', () => {
  const d = new DiagLog()
  d.push('跟踪 ok 内点=23', 1)
  d.push('检测 weak 内点=8', 2)
  assert.equal(d.groups.size, 2)
})

test('数字相同时只显示一个值，不显示 12~12', () => {
  const d = new DiagLog()
  d.push('内点=20', 1)
  d.push('内点=20', 2)
  assert.match(d.text(), /内点=20\b/)
  assert.doesNotMatch(d.text(), /20~20/)
})

test('关键事件不参与聚合，也不会被高频行挤掉', () => {
  const d = new DiagLog(24, 4)
  d.push('视频 error code=4 SRC_NOT_SUPPORTED', 1, 'key')
  // 灌 500 条**各不相同**的高频行（模拟真机：每行数字都在变）
  for (let i = 0; i < 500; i++) d.push(`跟踪 ok 内点=${i % 30} ${i % 20}ms`, 100 + i)
  const t = d.text()
  assert.match(t, /code=4/, '关键行必须还在 —— 它是唯一有信息量的那条')
  assert.match(t, /关键事件/)
  assert.ok(d.groups.size <= 4, '流区受上限约束')
})

test('流区超上限时丢最久没更新的那组，不丢正在刷的', () => {
  const d = new DiagLog(24, 2)
  d.push('A 1', 100)          // 最老
  d.push('B 1', 200)
  d.push('B 2', 300)          // B 更新到 300
  d.push('C 1', 400)          // 触发淘汰
  const tpls = [...d.groups.keys()]
  assert.ok(!tpls.some((t) => t.startsWith('A')), `A 应被淘汰，实际留着 ${tpls}`)
  assert.ok(tpls.some((t) => t.startsWith('B')), 'B 刚更新过，不该被淘汰')
})

test('lines() 带毫秒时间戳（贴合问题都在几十毫秒的量级上）', () => {
  const d = new DiagLog()
  d.push('x', Date.UTC(2026, 7, 4, 12, 34, 56, 789), 'key')
  assert.ok(d.lines().some((l) => l.startsWith('12:34:56.789 x')))
})

test('clear 之后两个区都空', () => {
  const d = new DiagLog()
  d.push('x', 1, 'key')
  d.push('y 1', 2)
  d.clear()
  assert.equal(d.text(), '')
})

test('MediaError 的四个 code 都有人能读的名字，且各自不同', () => {
  // 这四种的修法毫不相干（2=传输、3=解码、4=拿不到/格式不认），
  // 只显示一个数字等于没显示。
  const names = [1, 2, 3, 4].map((c) => MEDIA_ERR[c])
  assert.equal(names.filter(Boolean).length, 4)
  assert.equal(new Set(names).size, 4)
  assert.match(MEDIA_ERR[4], /401|404|Content-Type/)
})

test('networkState / readyState 的每个取值都有名字', () => {
  for (const k of [0, 1, 2, 3]) assert.ok(NETWORK_STATE[k], `networkState ${k} 没名字`)
  for (const k of [0, 1, 2, 3, 4]) assert.ok(READY_STATE[k], `readyState ${k} 没名字`)
})

test('short() 不泄露完整 URL —— 这块日志是要被发出去的', () => {
  const s = short('https://xyz.sunfish-tench.ts.net:48082/v1/photo/d83e2f483cbd46ccbb60d79306e5ac01/media')
  assert.ok(!s.includes('sunfish-tench'), `域名泄露了：${s}`)
  assert.ok(s.includes('media'), '末段要留着，否则看不出是哪个接口')
  assert.equal(short(''), '(空)')
  assert.equal(short(null), '(空)')
})

// ── 调试模式的两条守卫 ──────────────────────────────────────────────
//
// `diag.js` 里那套开关要碰 DOM 与 localStorage，所以这里不 import 它的
// enable/disable（node 里没有 document），只**读源码**验两条约定。笨，但它守的是
// 两件在浏览器里出错时完全不响的事，而写一套 DOM stub 的成本远高于收益。
test('bindToggle 只能关调试模式，不能开', async () => {
  const src = await readFile(new URL('../public/diag.js', import.meta.url), 'utf8')
  const body = src.slice(src.indexOf('export function bindToggle'))
  const fn = body.slice(0, body.indexOf('\n}'))
  // 早退：没开着就什么都不做。少了它，宾客在扫描页那条读数上手快点三下就掉进调试模式，
  // 看到一屏内点数和毫秒数。
  assert.match(fn, /if \(!enabled\) return/, 'bindToggle 必须先判 enabled 再数点击')
  assert.match(fn, /disable\(\)/, 'bindToggle 该能关')
  assert.ok(!/\benable\(\)/.test(fn), 'bindToggle 不能开 —— 入口只有设置页连按版本号')
})

test('调试模式的开关状态要存起来（否则刷新一次就白解锁了）', async () => {
  const src = await readFile(new URL('../public/diag.js', import.meta.url), 'utf8')
  // 用法是"打开它，然后再复现一次问题"，而复现往往就要刷新。
  assert.match(src, /localStorage/, '调试模式必须持久化')
  assert.match(src, /photoar\.debug/, '键名固定，别改')
  // 隐私模式下 localStorage 会抛。抛了只该退化成"只在本次会话有效"，不能崩。
  const save = src.slice(src.indexOf('function saveDebugFlag'))
  assert.match(save.slice(0, save.indexOf('\n}')), /catch/, '存不进去不能抛出去')
})

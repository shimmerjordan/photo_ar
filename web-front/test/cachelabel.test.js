/**
 * 缓存按钮与状态行的文案（`public/cachelabel.js`）。输入是 `mediastore.groupStates` 的一项。
 *
 * 钉住它们是因为**按钮做什么由这张表决定**（`action`），而不只是写什么字：同一个「已缓存」
 * 若映射错了动作，用户点一下就把固定的那组删了，或者点「缓存」其实什么都没发生。
 * 媒体页的卡片（Task 8）与宾客页的「本机缓存」木牌共用这一份；木牌那一行的数与「全部缓存」
 * 要做哪些组（`cacheSummary` 等）在文件后半。
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  REF_BYTES_ESTIMATE, bulkResultText, cacheButtonLabel, cacheStateText, cacheSummary, groupTag, listedGroups,
  singleNeed, spaceShort, summaryText,
} from '../public/cachelabel.js'

const st = (state, extra = {}) => ({ state, pinned: false, pct: null, bytes: null, ...extra })

test('cacheButtonLabel：没配视频 → 无视频，不可点', () => {
  assert.deepEqual(cacheButtonLabel(st('novideo')), { text: '无视频', kind: 'off', action: null })
})
test('cacheButtonLabel：没缓存 → 缓存', () => {
  assert.deepEqual(cacheButtonLabel(st('none')), { text: '缓存', kind: '', action: 'cache' })
})
test('cacheButtonLabel：下载中 → 有总长报百分比，没有就只说下载中；都不可点', () => {
  assert.deepEqual(cacheButtonLabel(st('downloading', { pct: 0.424 })), { text: '下载 42%', kind: '', action: null })
  assert.deepEqual(cacheButtonLabel(st('downloading')), { text: '下载中', kind: '', action: null })
})
test('cacheButtonLabel：已缓存且固定 → 已缓存，点了是移除', () => {
  assert.deepEqual(cacheButtonLabel(st('cached', { pinned: true })), { text: '已缓存', kind: 'ok', action: 'remove' })
})
test('cacheButtonLabel：已缓存但没固定（预取 / 看过自动存下的）→ 自动缓存，点了是固定', () => {
  assert.deepEqual(cacheButtonLabel(st('cached')), { text: '自动缓存', kind: '', action: 'pin' })
})
test('cacheButtonLabel：没有状态（还没查出来）当没缓存处理，不抛', () => {
  assert.deepEqual(cacheButtonLabel(undefined), { text: '缓存', kind: '', action: 'cache' })
})

test('cacheStateText：五种状态各一句', () => {
  assert.equal(cacheStateText(st('none')), '未缓存')
  assert.equal(cacheStateText(st('downloading', { pct: 0.5 })), '下载中 50%')
  assert.equal(cacheStateText(st('downloading')), '下载中')
  assert.equal(cacheStateText(st('cached', { pinned: true })), '已缓存（固定在本机，不会被自动清掉）')
  assert.equal(cacheStateText(st('cached')), '已缓存（自动存下的，空间紧张时可能被清掉）')
  assert.equal(cacheStateText(st('novideo')), '这张没配视频')
})

// ── 木牌那一行与「全部缓存」要算的数（Task 8）─────────────────────────────
//
// 媒体页顶上的「本机缓存」木牌与缓存页共用这几个函数。钉住它们是因为**这几个数决定了按钮做什么**：
// `todo` 就是「全部缓存」要挨个交给 cacheGroup 的那一批，`need` 决定要不要先弹空间不够的确认。
// 算错了的表现不是一个数不对，而是该下的没下、或者不该弹的确认挡住了用户。

const MB = 1048576
const ph = (id, extra = {}) => ({ photoId: id, title: `照片${id}`, hasVideo: true, videoAssetId: `v${id}`, videoBytes: 100 * MB, ...extra })
/** 一屏的典型组合：固定的、自动的、没缓存的、没视频的、下载中的、大小未知的。 */
const mix = () => {
  const photos = [
    ph('a'),
    ph('b', { videoBytes: 200 * MB }),
    ph('c', { videoBytes: 300 * MB }),
    ph('d', { hasVideo: false, videoAssetId: null, videoBytes: null }),
    ph('e', { videoBytes: 400 * MB }),
    ph('f', { videoBytes: null }),
  ]
  const states = new Map([
    ['a', st('cached', { pinned: true, bytes: 100 * MB })],
    ['b', st('cached', { bytes: 200 * MB })],
    ['c', st('none', { bytes: 300 * MB })],
    ['d', st('novideo')],
    ['e', st('downloading', { pinned: true, pct: 0.5, bytes: 400 * MB })],
    ['f', st('none')],
  ])
  return { photos, states }
}

test('cacheSummary：M 只数有视频的组；N 固定与自动都算（都在本机）；占用只加已缓存的', () => {
  const { photos, states } = mix()
  const s = cacheSummary(photos, states)
  assert.equal(s.total, 5)
  assert.equal(s.cached, 2)
  assert.equal(s.bytes, 300 * MB)
  assert.equal(s.bytesUnknown, 0)
})

test('cacheSummary：全部缓存要做的 = 除了「已固定且已缓存」之外的每一组有视频的', () => {
  // 自动缓存的也要过一遍 cacheGroup：它会被预取的清理挤掉，「全部缓存」的意思是「都要在本机」——
  // 过一遍就是把它固定下来（视频已在，不重下，只补原图）。下载中的挂上去等它。
  const { photos, states } = mix()
  const s = cacheSummary(photos, states)
  assert.deepEqual(s.todo.map((p) => p.photoId), ['b', 'c', 'e', 'f'])
  // 需要的空间：视频还不在本机的（自动缓存那组不用再下）之外，todo 里每一组都打底一份原图
  // 估值（fix1 Finding 3：原图没法从 groupStates 便宜查出来，保守按 REF_BYTES_ESTIMATE 算）；
  // 大小未知的单独数出来。
  assert.equal(s.need, 700 * MB + 4 * REF_BYTES_ESTIMATE)
  assert.equal(s.needUnknown, 1)
})

test('cacheSummary：已缓存但大小未知（老服务端没给 videoBytes）→ 占用记下有几组不知道', () => {
  const photos = [ph('a', { videoBytes: null }), ph('b')]
  const states = new Map([['a', st('cached', { pinned: true })], ['b', st('cached', { pinned: true, bytes: 100 * MB })]])
  const s = cacheSummary(photos, states)
  assert.equal(s.bytes, 100 * MB)
  assert.equal(s.bytesUnknown, 1)
  assert.deepEqual(s.todo, [])
})

test('cacheSummary：状态还没查出来的组当没缓存（宁可多过一遍 cacheGroup，它自己会跳过本机已有的）', () => {
  const s = cacheSummary([ph('a')], new Map())
  assert.equal(s.cached, 0)
  assert.deepEqual(s.todo.map((p) => p.photoId), ['a'])
  assert.equal(s.need, 100 * MB + REF_BYTES_ESTIMATE)
})

test('singleNeed：单张卡片「缓存」的空间预检 —— 原图打底常量，视频只在还没缓存时再加', () => {
  const p = ph('x', { videoBytes: 100 * MB })
  assert.equal(singleNeed(p, st('none', { bytes: 100 * MB })), 100 * MB + REF_BYTES_ESTIMATE)
  // 已缓存（不管固不固定）：视频不重下，只剩原图那一份打底。
  assert.equal(singleNeed(p, st('cached', { bytes: 100 * MB })), REF_BYTES_ESTIMATE)
  assert.equal(singleNeed(p, st('cached', { pinned: true, bytes: 100 * MB })), REF_BYTES_ESTIMATE)
  // 下载中：视频还没真的在本机，照样算上。
  assert.equal(singleNeed(p, st('downloading', { bytes: 100 * MB })), 100 * MB + REF_BYTES_ESTIMATE)
  // 大小未知（没有 bytes、服务端也没给 videoBytes）：不把 NaN 混进去，只打底原图那部分。
  assert.equal(singleNeed(ph('y', { videoBytes: null }), st('none', { bytes: null })), REF_BYTES_ESTIMATE)
  // 状态还没查出来（st 为 null）当没缓存处理，理由同 cacheButtonLabel。
  assert.equal(singleNeed(p, null), 100 * MB + REF_BYTES_ESTIMATE)
})

test('summaryText：N / M 组 · 占用；一组都没缓存时不写占用；有大小未知的写「至少」；没有视频就直说', () => {
  assert.equal(summaryText({ cached: 2, total: 5, bytes: 300 * MB, bytesUnknown: 0 }), '已缓存 2 / 5 组 · 占用 300.0 MB')
  assert.equal(summaryText({ cached: 2, total: 5, bytes: 300 * MB, bytesUnknown: 1 }), '已缓存 2 / 5 组 · 占用至少 300.0 MB')
  assert.equal(summaryText({ cached: 0, total: 5, bytes: 0, bytesUnknown: 0 }), '已缓存 0 / 5 组')
  assert.equal(summaryText({ cached: 0, total: 0, bytes: 0, bytesUnknown: 0 }), '这些照片都没配视频，没有要缓存的')
})

test('spaceShort：剩余 < 需要 × 1.1 才报；问不到配额、或者不需要下任何东西时不挡人', () => {
  assert.equal(spaceShort(1000, null), null)
  assert.equal(spaceShort(1000, { usage: 0, quota: 0 }), null)          // quota 0 = 浏览器没给
  assert.equal(spaceShort(0, { usage: 0, quota: 10 }), null)
  assert.equal(spaceShort(1000, { usage: 0, quota: 1100 }), null)       // 恰好 1.1 倍：够
  assert.deepEqual(spaceShort(1000, { usage: 1, quota: 1100 }), { need: 1000, left: 1099 })
  assert.deepEqual(spaceShort(1000, { usage: 2000, quota: 1000 }), { need: 1000, left: 0 })   // 估值会超：不报负数
})

test('groupTag：缓存页每一行的标签', () => {
  assert.equal(groupTag(st('cached', { pinned: true })), '固定')
  assert.equal(groupTag(st('cached')), '自动')
  assert.equal(groupTag(st('downloading', { pct: 0.424 })), '下载中 42%')
  assert.equal(groupTag(st('downloading')), '下载中')
  // 固定了但本机没有：上次没下完，或被浏览器清了。要说出来 —— 用户以为它在。
  assert.equal(groupTag(st('none', { pinned: true })), '固定 · 本机还没有')
  assert.equal(groupTag(st('none')), '')
})

test('listedGroups：缓存页只列本机有东西（或被固定）的组，顺序照原列表', () => {
  const { photos, states } = mix()
  assert.deepEqual(listedGroups(photos, states).map((p) => p.photoId), ['a', 'b', 'e'])
  states.set('c', st('none', { pinned: true }))
  assert.deepEqual(listedGroups(photos, states).map((p) => p.photoId), ['a', 'b', 'c', 'e'])
})

test('bulkResultText：一批跑完那一句 —— 全成 / 有失败 / 停下 / 写满各说各的', () => {
  const r = (extra) => ({ total: 5, done: 5, failed: 0, stopped: false, full: false, errors: [], ...extra })
  assert.equal(bulkResultText(null), '')
  assert.equal(bulkResultText(r()), '缓存好 5 组')
  assert.equal(bulkResultText(r({ done: 3, failed: 2 })), '缓存好 3 组，2 组没成')
  assert.equal(bulkResultText(r({ done: 2, stopped: true })), '缓存好 2 组：已停止，还有 3 组没开始')
  // 写满要给出下一步（去清），而不是只说"没成"：接着点「全部缓存」只会再撞一次。
  assert.equal(bulkResultText(r({ done: 1, failed: 1, full: true })), '缓存好 1 组，1 组没成：浏览器存储空间不够，已停下。去「管理缓存」清掉一些再来')
})

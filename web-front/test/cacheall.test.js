/**
 * 「全部缓存」的串行执行器（`public/cacheall.js`）。媒体页与缓存页的同名按钮共用它。
 *
 * 钉住的是用户能感知的三件事：
 * - **串行**：一组下完才开下一组 —— 同时开几十组的话每一组都慢，而且谁都下不完；
 * - **「停止」停的是还没开始的那些**：正在下的那组照常下完（半截的字节没有用，丢了白下）；
 * - **写满就收手**：配额满了之后每一组都会下完再被拒，接着跑只是白白耗流量。
 *
 * `cacheGroup` 注入假的：这里测的是调度，不是下载（下载本身在 mediastore.test.js）。
 */
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
import { STORAGE_FULL } from '../public/mediastore.js'
import { REF_BYTES_ESTIMATE } from '../public/cachelabel.js'
import {
  _resetForTest, bulkState, cacheAll, onBulkChange, precheckSingle, startCacheAll, stopCacheAll,
} from '../public/cacheall.js'

const MB = 1048576
const ph = (id, extra = {}) => ({ photoId: id, title: `照片${id}`, hasVideo: true, videoAssetId: `v${id}`, videoBytes: 100 * MB, ...extra })
const st = (state, extra = {}) => ({ state, pinned: false, pct: null, bytes: 100 * MB, ...extra })

/** 一个每组都要手动放行的假 cacheGroup：`gate.release(id, result)` 让那一组结束。 */
function gatedCacheGroup() {
  const started = []
  const waiters = new Map()
  const fn = (p) => {
    started.push(p.photoId)
    return new Promise((resolve) => waiters.set(p.photoId, resolve))
  }
  const release = async (id, r = { ok: true }) => {
    waiters.get(id)(r)
    // 让执行器的 await 接上、开下一组
    await new Promise((r2) => setImmediate(r2))
  }
  return { fn, started, release }
}
const tick = () => new Promise((r) => setImmediate(r))

beforeEach(() => _resetForTest())

test('串行：前一组没结束，下一组不开', async () => {
  const g = gatedCacheGroup()
  const done = cacheAll([ph('a'), ph('b'), ph('c')], { cacheGroup: g.fn })
  await tick()
  assert.deepEqual(g.started, ['a'])
  await g.release('a')
  assert.deepEqual(g.started, ['a', 'b'])
  await g.release('b')
  await g.release('c')
  const r = await done
  assert.equal(r.done, 3)
  assert.equal(r.failed, 0)
  assert.equal(r.stopped, false)
})

test('停止：正在下的那组照常下完并计入，还没开始的不开', async () => {
  const g = gatedCacheGroup()
  const done = cacheAll([ph('a'), ph('b'), ph('c')], { cacheGroup: g.fn })
  await tick()
  stopCacheAll()
  assert.equal(bulkState().stopping, true)
  await g.release('a')
  const r = await done
  assert.deepEqual(g.started, ['a'])
  assert.equal(r.done, 1)
  assert.equal(r.stopped, true)
  assert.equal(bulkState().running, false)
})

test('一组失败不连累后面的；失败的带上标题和原因', async () => {
  const g = gatedCacheGroup()
  const done = cacheAll([ph('a'), ph('b')], { cacheGroup: g.fn })
  await tick()
  await g.release('a', { ok: false, error: 'HTTP 500' })
  await g.release('b')
  const r = await done
  assert.equal(r.done, 1)
  assert.equal(r.failed, 1)
  assert.deepEqual(r.errors, [{ photoId: 'a', title: '照片a', error: 'HTTP 500' }])
})

test('cacheGroup 抛异常也当这一组失败，不把整批炸掉', async () => {
  const r = await cacheAll([ph('a'), ph('b')], {
    cacheGroup: async (p) => { if (p.photoId === 'a') throw new Error('炸了'); return { ok: true } },
  })
  assert.equal(r.done, 1)
  assert.equal(r.failed, 1)
  assert.equal(r.errors[0].error, '炸了')
})

test('写满了就收手：后面的每一组都会下完再被拒，不再开', async () => {
  const g = gatedCacheGroup()
  const done = cacheAll([ph('a'), ph('b'), ph('c')], { cacheGroup: g.fn })
  await tick()
  await g.release('a', { ok: false, error: STORAGE_FULL })
  const r = await done
  assert.deepEqual(g.started, ['a'])
  assert.equal(r.full, true)
  assert.equal(r.failed, 1)
})

test('正在跑时再点一次「全部缓存」：不开第二批，拿到的是同一批的结果', async () => {
  const g = gatedCacheGroup()
  const p1 = cacheAll([ph('a')], { cacheGroup: g.fn })
  const p2 = cacheAll([ph('x'), ph('y')], { cacheGroup: g.fn })
  assert.equal(p1, p2)
  await tick()
  await g.release('a')
  await p1
  assert.deepEqual(g.started, ['a'])
})

test('bulkState / onBulkChange：页面据此把按钮换成「停止」、再换回来', async () => {
  const seen = []
  const off = onBulkChange((s) => seen.push(s.running ? `跑 ${s.finished}/${s.total}` : '停'))
  const g = gatedCacheGroup()
  const done = cacheAll([ph('a'), ph('b')], { cacheGroup: g.fn })
  assert.equal(bulkState().running, true)
  assert.equal(bulkState().total, 2)
  await tick()
  await g.release('a')
  await g.release('b')
  const r = await done
  off()
  assert.equal(seen.at(-1), '停')
  assert.ok(seen.includes('跑 1/2'))
  assert.deepEqual(bulkState().last, r)
})

test('startCacheAll：剩余空间不够先问；用户说不就一组都不开', async () => {
  const asked = []
  const calls = []
  const photos = [ph('a'), ph('b')]
  const states = new Map([['a', st('none')], ['b', st('none')]])
  const r = await startCacheAll(photos, states, {
    estimate: async () => ({ usage: 0, quota: 150 * MB }),
    confirm: (msg) => { asked.push(msg); return false },
    cacheGroup: async (p) => { calls.push(p.photoId); return { ok: true } },
  })
  assert.equal(r, null)
  assert.deepEqual(calls, [])
  assert.equal(asked.length, 1)
  // 200MB 视频 + todo 两组各打底一份 REF_BYTES_ESTIMATE（fix1 Finding 3）。
  const need = ((200 * MB + 2 * REF_BYTES_ESTIMATE) / MB).toFixed(1)
  assert.match(asked[0], new RegExp(`大概要 ${need} MB，浏览器还给这个站点留了 150\\.0 MB`))
})

test('startCacheAll：空间够就不问，直接按 todo 的顺序跑；已固定且已缓存的跳过', async () => {
  const calls = []
  const photos = [ph('a'), ph('b'), ph('c')]
  const states = new Map([['a', st('cached', { pinned: true })], ['b', st('none')], ['c', st('cached')]])
  const r = await startCacheAll(photos, states, {
    estimate: async () => ({ usage: 0, quota: 10_000 * MB }),
    confirm: () => { throw new Error('不该问') },
    cacheGroup: async (p) => { calls.push(p.photoId); return { ok: true } },
  })
  assert.deepEqual(calls, ['b', 'c'])
  assert.equal(r.done, 2)
})

test('startCacheAll：没有要做的就什么也不做（返回 null）', async () => {
  const r = await startCacheAll([ph('a')], new Map([['a', st('cached', { pinned: true })]]), {
    estimate: async () => { throw new Error('不该问配额') },
    cacheGroup: async () => { throw new Error('不该开') },
  })
  assert.equal(r, null)
})

// ── 单张卡片「缓存」的空间预检（fix1 Finding 3）─────────────────────────────
//
// 之前只有「全部缓存」做了 estimate 预检，单张卡片的「缓存」完全没做 —— 设计 §4 要求
// 点缓存就该先问一句。media 页的卡片与 view.js 的「本机缓存」木牌共用 precheckSingle。

test('precheckSingle：这一组不够先问；说不就返回 false（调用方不该发起下载）', async () => {
  const asked = []
  const ok = await precheckSingle(ph('a'), st('none'), {
    estimate: async () => ({ usage: 0, quota: 50 * MB }),
    confirm: (msg) => { asked.push(msg); return false },
  })
  assert.equal(ok, false)
  assert.equal(asked.length, 1)
  // 100MB 视频 + 打底一份 REF_BYTES_ESTIMATE。
  const need = ((100 * MB + REF_BYTES_ESTIMATE) / MB).toFixed(1)
  assert.match(asked[0], new RegExp(`这一组大概要 ${need} MB，浏览器还给这个站点留了 50\\.0 MB`))
})

test('precheckSingle：够就不问，直接放行', async () => {
  const ok = await precheckSingle(ph('a'), st('none'), {
    estimate: async () => ({ usage: 0, quota: 10_000 * MB }),
    confirm: () => { throw new Error('不该问') },
  })
  assert.equal(ok, true)
})

test('precheckSingle：问不到配额不挡人（宁可让存不下的人试一次，也不能拦下本来存得下的人）', async () => {
  const ok = await precheckSingle(ph('a'), st('none'), {
    estimate: async () => null,
    confirm: () => { throw new Error('不该问') },
  })
  assert.equal(ok, true)
})

test('precheckSingle：说了继续就返回 true', async () => {
  const ok = await precheckSingle(ph('a'), st('none'), {
    estimate: async () => ({ usage: 0, quota: 50 * MB }),
    confirm: () => true,
  })
  assert.equal(ok, true)
})

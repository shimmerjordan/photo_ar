// test/netsrc.test.js
import { test, beforeEach } from 'node:test'
import assert from 'node:assert/strict'
const store = new Map()
globalThis.localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, v), removeItem: (k) => store.delete(k) }
const N = await import('../public/netsrc.js')
const P = await import('../public/prefs.js')
beforeEach(() => { store.clear(); N.resetTrip() })

test('hostKind 分三类', () => {
  for (const u of ['http://192.168.1.10:8964', 'http://10.0.0.2', 'http://172.20.1.1', 'http://100.101.1.1', 'http://nas.local', 'http://[fd00::1]']) assert.equal(N.hostKind(u), 'private', u)
  for (const u of ['http://localhost:8964', 'http://127.0.0.1', 'http://[::1]']) assert.equal(N.hostKind(u), 'loopback', u)
  for (const u of ['https://photo.example.com', 'http://172.32.0.1', 'http://8.8.8.8', 'not a url']) assert.equal(N.hostKind(u), 'public', u)
})
test('没开数据源时只有默认源', () => {
  assert.deepEqual(N.sourcesFor('/v1/asset/a/stream').map((s) => s.label), ['origin'])
})
test('开了数据源：先局域网后默认；熔断期间跳过局域网', () => {
  P.setAdvanced({ mediaBase: 'http://192.168.1.10:8964', mediaBaseOn: true, lanMode: 'local' })
  const s = N.sourcesFor('/v1/asset/a/stream')
  assert.deepEqual(s.map((x) => x.label), ['lan', 'origin'])
  assert.equal(s[0].init.credentials, 'omit'); assert.equal(s[0].init.targetAddressSpace, 'local')
  N.tripBase(60_000)
  assert.deepEqual(N.sourcesFor('/v1/asset/a/stream').map((x) => x.label), ['origin'])
})
test('不能发票的路径不走数据源', () => {
  P.setAdvanced({ mediaBase: 'http://192.168.1.10:8964', mediaBaseOn: true })
  assert.deepEqual(N.sourcesFor('/v1/admin/media').map((x) => x.label), ['origin'])
})
test('公网地址不带 targetAddressSpace', () => {
  assert.deepEqual(N.lanInit('https://photo.example.com', 'local'), {})
})
test('probeBase：第一种写法失败、第二种成功就记住第二种', async () => {
  const seen = []
  const fetchImpl = async (url, init) => { seen.push(init.targetAddressSpace ?? 'plain'); if (!init.targetAddressSpace) throw new TypeError('Failed to fetch'); return new Response('{}', { status: 200 }) }
  const r = await N.probeBase('http://192.168.1.10:8964', { fetchImpl, pageProtocol: 'https:' })
  assert.equal(r.ok, true); assert.equal(r.mode, 'local'); assert.deepEqual(seen, ['plain', 'local'])
})
test('probeBase：全失败给混合内容提示', async () => {
  const fetchImpl = async () => { throw new TypeError('Failed to fetch') }
  const r = await N.probeBase('http://192.168.1.10:8964', { fetchImpl, pageProtocol: 'https:' })
  assert.equal(r.ok, false); assert.match(r.hint, /混合内容/)
})
test('probeHint 四档', () => {
  assert.match(N.probeHint({ pageHttps: false, baseHttp: true, kind: 'private', status: 404 }), /HTTP 404/)
  assert.match(N.probeHint({ pageHttps: false, baseHttp: true, kind: 'private', lastError: { name: 'TimeoutError' } }), /没有回应/)
  assert.match(N.probeHint({ pageHttps: false, baseHttp: true, kind: 'private', lastError: { name: 'TypeError' } }), /连不上/)
})
// 设置页「测试」那一行速度就是这两个数（Task 8）。对方回了错误页、或者视频比测速长度短时，
// 原来的算法会按「请求了 2MB」去除耗时 —— 一个 403 的小 JSON 秒回，显示成几百 MB/s 的「局域网」。
test('speedTest：对方回了错误状态就报错，不算成速度', async () => {
  const fetchImpl = async () => new Response('{"error":"forbidden"}', { status: 403 })
  const r = await N.speedTest('/v1/asset/a/stream', { fetchImpl })
  assert.equal(r.lan, null)   // 没配数据源：这一条不存在，不是失败
  assert.match(r.origin.error, /403/)
})
test('speedTest：按实际收到的字节算，视频比测速长度短时不虚报', async () => {
  const fetchImpl = async () => new Response(new Uint8Array(1000), { status: 206 })
  const r = await N.speedTest('/v1/asset/a/stream', { fetchImpl, bytes: 2 * 1048576 })
  assert.equal(r.origin.bytes, 1000)
  assert.ok(Number.isFinite(r.origin.mbps))
})

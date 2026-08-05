/**
 * 手工拼一个 PARL 包。测试用。
 *
 * 布局的权威说明在 `server/library.js` 的 `pack()` 上。这里是它的**第二份实现** ——
 * 刻意的：Node 那边写、浏览器那边读，两边各自照着同一份布局说明实现一次，拼错了
 * `library.unpack()` 会立刻抛（它校验总长度）。一份实现自己读自己写，是证不出格式契约的。
 */
export function buildParl({ id = 'goldenphoto', pts, desc, aspect = 1.5, mediaUrl = null, title = 'golden' }) {
  const count = pts.length / 2
  const json = new TextEncoder().encode(JSON.stringify({
    photos: [{ id, doc: 0, count, aspect, title, mediaUrl, thumbUrl: null }],
    nRoot: 0, flatLen: 0, nnz: 0, skipped: [], libraryMtimeMs: 0,
  }))
  const total = 32 + json.length + count * 2 * 4 + count * 32
  const buf = new ArrayBuffer(total)
  const dv = new DataView(buf)
  const u8 = new Uint8Array(buf)
  for (const [i, c] of [...'PARL'].entries()) dv.setUint8(i, c.charCodeAt(0))
  dv.setUint32(4, 1, true)      // version
  dv.setUint32(8, 1, true)      // nPhotos
  dv.setUint32(12, 0, true)     // nWords = 0 → 浏览器走全量扫描
  dv.setUint32(16, 0, true)     // nNodes
  dv.setUint32(20, 32, true)    // descDim
  dv.setUint32(24, 640, true)   // refLongEdge
  dv.setUint32(28, json.length, true)
  u8.set(json, 32)
  let p = 32 + json.length
  new Uint8Array(buf, p, count * 2 * 4).set(new Uint8Array(pts.buffer, pts.byteOffset, count * 2 * 4))
  p += count * 2 * 4
  u8.set(desc, p)
  return buf
}

export function b64Bytes(b64) {
  const s = atob(b64)
  const out = new Uint8Array(s.length)
  for (let i = 0; i < s.length; i++) out[i] = s.charCodeAt(i)
  return out
}

export function b64F32(b64) {
  const b = b64Bytes(b64)
  return new Float32Array(b.buffer, b.byteOffset, b.length / 4)
}

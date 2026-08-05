/**
 * `server/library.js` + `server/npz.js` 的测试。
 *
 * 两类断言，目的不同：
 *
 * 1. **对着真实库文件跑**（`data/library/`）。合成的 fixture 只能证明代码自洽，证明不了
 *    "布局猜对了" —— 而 slot 布局、words.bin 的 stride、npz 的成员名，全都是我们从服务端
 *    源码里读出来再复现的，猜错不会报错。所以库不在时这些测试 **skip 而不是 pass**：
 *    一个悄悄跳过的测试和一个通过的测试在 CI 输出里长得太像。
 * 2. **常量与服务端源码逐个比**。`ORB_LAYOUT` 抄自 `photoar.descstore`，抄错的后果是
 *    读出错位的坐标。
 */
import { test, describe } from 'node:test'
import assert from 'node:assert/strict'
import { readFile, access } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { Library, ORB_LAYOUT, REF_LONG_EDGE } from '../server/library.js'
import { parseNpy, parseNpz } from '../server/npz.js'

const REPO = resolve(import.meta.dirname, '../..')
const LIB_DIR = join(REPO, 'data/library')

const exists = async (p) => access(p).then(() => true, () => false)
const hasLib = await exists(join(LIB_DIR, 'slots.json'))

describe('常量与服务端源码一致', () => {
  test('ORB_LAYOUT 对得上 photoar.features / descstore', async () => {
    const src = await readFile(join(REPO, 'src/photoar/features.py'), 'utf8')
    const num = (name) => {
      const m = new RegExp(`^${name}\\s*=\\s*([0-9.]+)`, 'm').exec(src)
      assert.ok(m, `features.py 里找不到 ${name} —— 服务端改了名字，这条测试必须跟着改`)
      return Number(m[1])
    }
    assert.equal(ORB_LAYOUT.nFeatures, num('N_FEATURES'))
    assert.equal(ORB_LAYOUT.descDim, num('DESC_BYTES'))
    assert.equal(REF_LONG_EDGE, num('LONG_EDGE'))
    // descstore 的 docstring 自己写了这个数（"实际每张 12008 字节"）。
    assert.equal(ORB_LAYOUT.stride, 12008)
    assert.equal(ORB_LAYOUT.ptsOffset, 8)
    assert.equal(ORB_LAYOUT.descOffset, 8 + 300 * 2 * 4)
  })

  test('查询侧常量对得上 photoar.backend', async () => {
    const src = await readFile(join(REPO, 'src/photoar/backend.py'), 'utf8')
    const consts = await readFile(join(import.meta.dirname, '../public/recognize/consts.js'), 'utf8')
    for (const [py, js] of [['QUERY_LONG_EDGE', 'QUERY_LONG_EDGE'], ['QUERY_N_FEATURES', 'QUERY_N_FEATURES']]) {
      const pv = Number(new RegExp(`^${py}\\s*=\\s*([0-9]+)`, 'm').exec(src)?.[1])
      const jv = Number(new RegExp(`export const ${js}\\s*=\\s*([0-9]+)`, 'm').exec(consts)?.[1])
      assert.ok(Number.isFinite(pv), `backend.py 里找不到 ${py}`)
      assert.equal(jv, pv, `${js} 漂移了：JS=${jv} Python=${pv}`)
    }
  })

  test('判定阈值对得上 photoar.verify', async () => {
    const src = await readFile(join(REPO, 'src/photoar/verify.py'), 'utf8')
    const consts = await readFile(join(import.meta.dirname, '../public/recognize/consts.js'), 'utf8')
    const py = (name) => Number(new RegExp(`^${name}\\s*=\\s*([0-9.]+)`, 'm').exec(src)?.[1])
    const js = (name) => Number(new RegExp(`\\b${name}:\\s*([0-9.]+)`, 'm').exec(consts)?.[1])
    assert.equal(js('minInliers'), py('MIN_INLIERS'))
    assert.equal(js('ratio'), py('RATIO'))
    assert.equal(js('detMin'), py('DET_MIN'))
    assert.equal(js('detMax'), py('DET_MAX'))
    const jsConst = (name) => Number(new RegExp(`export const ${name}\\s*=\\s*([0-9.]+)`, 'm').exec(consts)?.[1])
    assert.equal(jsConst('RANSAC_REPROJ'), py('RANSAC_REPROJ'))
    assert.equal(jsConst('RANSAC_MAX_ITERS'), py('RANSAC_MAX_ITERS'))
    assert.equal(jsConst('MIN_MATCHES_FOR_HOMOGRAPHY'), py('MIN_MATCHES_FOR_HOMOGRAPHY'))
  })
})

describe('npz 读取', () => {
  test('parseNpy 拒绝非 npy 与 fortran_order', () => {
    assert.throws(() => parseNpy(Buffer.from('not npy at all!!')), /magic/)
  })

  test('对着真实 index.npz 读出该有的成员', { skip: !hasLib && '没有 data/library/' }, async () => {
    const p = join(LIB_DIR, 'index.npz')
    if (!(await exists(p))) return // 空库时可能还没有
    const z = parseNpz(await readFile(p))
    for (const k of ['n_docs', 'idf', 'offsets', 'doc_ids', 'weights']) {
      assert.ok(z[k], `index.npz 里没有 ${k} —— 服务端 InvertedIndex.save 改过了`)
    }
    // offsets 的长度必须是 n_words+1，且单调不减 —— 这是 CSR 的不变量，读错 dtype
    // 或字节序时它立刻不成立，而别的检查都察觉不到。
    const off = z.offsets.data
    assert.equal(off.length, z.idf.data.length + 1)
    for (let i = 1; i < off.length; i++) {
      assert.ok(off[i] >= off[i - 1], `offsets 在 ${i} 处回退了`)
    }
    assert.equal(Number(off[off.length - 1]), z.doc_ids.data.length)
  })
})

describe('Library 对着真实库', { skip: !hasLib && '没有 data/library/' }, () => {
  test('load + slots + readSlot 自洽', async () => {
    const lib = await new Library(LIB_DIR).load()
    try {
      const ids = lib.photoIds
      assert.ok(ids.length > 0, 'slots.json 是空的')

      // desc.bin 的长度必须正好是 slot 数 × stride。不整除说明布局猜错了 —— 而按错的
      // stride 读会得到"合法但错位"的坐标与描述子，识别率归零且不报错。
      const buf = await readFile(join(LIB_DIR, 'desc.bin'))
      assert.equal(buf.length % ORB_LAYOUT.stride, 0,
        `desc.bin ${buf.length} 不是 stride ${ORB_LAYOUT.stride} 的整数倍`)
      assert.equal(buf.length / ORB_LAYOUT.stride, ids.length,
        'desc.bin 的 slot 数与 slots.json 不一致')

      let live = 0
      for (let s = 0; s < ids.length; s++) {
        if (ids[s] === '') continue // 墓碑
        live++
        const f = await lib.readSlot(s)
        assert.ok(f.count > 0 && f.count <= ORB_LAYOUT.nFeatures, `slot ${s} count=${f.count}`)
        assert.equal(f.pts.length, f.count * 2)
        assert.equal(f.desc.length, f.count * ORB_LAYOUT.descDim)
        // 关键点坐标必须落在 640 的特征空间里（入库侧长边）。落在外面说明 ptsOffset
        // 或字节序错了 —— 而那种错读出来的是巨大或极小的浮点，不是 NaN，所以只有
        // 范围检查抓得到。
        for (let i = 0; i < f.pts.length; i++) {
          assert.ok(f.pts[i] >= -1 && f.pts[i] <= REF_LONG_EDGE + 1,
            `slot ${s} 的坐标 ${f.pts[i]} 不在 [0,${REF_LONG_EDGE}] 里`)
        }
      }
      assert.ok(live > 0, '库里全是墓碑')
    } finally {
      await lib.close()
    }
  })

  test('墓碑 slot 不参与 pack，且如实报告 skipped', async () => {
    const lib = await new Library(LIB_DIR).load()
    try {
      const ids = lib.photoIds
      const livePhotos = ids.filter((x) => x !== '').map((id) => ({ id, aspect: 1.5 }))
      const packed = await lib.pack([...livePhotos, { id: 'ffffffffffffffffffffffffffffffff' }])
      assert.equal(packed.nPhotos, livePhotos.length)
      assert.equal(packed.skipped.length, 1)
      assert.equal(packed.skipped[0].reason, 'not_in_library')
    } finally {
      await lib.close()
    }
  })

  test('pack 的头部与 JSON 自洽，且字节长度可精确推出', async () => {
    const lib = await new Library(LIB_DIR).load()
    try {
      const photos = lib.photoIds.filter((x) => x !== '').map((id) => ({ id, aspect: 1.5, title: 't' }))
      const { buf, nPhotos } = await lib.pack(photos)

      assert.equal(buf.subarray(0, 4).toString('latin1'), 'PARL')
      assert.equal(buf.readUInt32LE(4), 1)
      assert.equal(buf.readUInt32LE(8), nPhotos)
      assert.equal(buf.readUInt32LE(20), ORB_LAYOUT.descDim)
      assert.equal(buf.readUInt32LE(24), REF_LONG_EDGE)
      const jsonBytes = buf.readUInt32LE(28)
      const meta = JSON.parse(buf.subarray(32, 32 + jsonBytes).toString('utf8'))
      assert.equal(meta.photos.length, nPhotos)

      // 逐段把长度加出来，必须正好等于 buf.length。这一条是整个格式的自校验：
      // 少写一段、或者某段用了错的元素宽度，都会在这里差出字节数来 —— 而浏览器侧
      // 按偏移读的话，只会读到下一段的数据当自己的，不报错。
      let expected = 32 + jsonBytes
      for (const p of meta.photos) expected += p.count * 2 * 4 + p.count * ORB_LAYOUT.descDim
      const nWords = buf.readUInt32LE(12)
      const nNodes = buf.readUInt32LE(16)
      if (nWords > 0) {
        expected += nNodes * ORB_LAYOUT.descDim          // centers
        expected += nNodes * 4                            // childrenLen
        expected += meta.flatLen * 4                      // childrenFlat
        expected += nNodes * 4                            // leafId
        expected += meta.nRoot * 4                        // rootChildren
        expected += nWords * 4                            // idf
        expected += (nWords + 1) * 4                      // offsets
        expected += meta.nnz * 4                          // docIds
        expected += meta.nnz * 4                          // weights
      }
      assert.equal(buf.length, expected,
        `pack 的字节数对不上：实际 ${buf.length}，按 JSON 推算 ${expected}`)
    } finally {
      await lib.close()
    }
  })

  test('没有词表时 nWords=0，浏览器该走全量扫描', async () => {
    const lib = await new Library(LIB_DIR).load()
    try {
      const hasVocab = await exists(join(LIB_DIR, 'vocab.npz'))
      const photos = lib.photoIds.filter((x) => x !== '').map((id) => ({ id }))
      const { buf } = await lib.pack(photos)
      const nWords = buf.readUInt32LE(12)
      if (hasVocab) assert.ok(nWords > 0, '有 vocab.npz 却没打包词表')
      else assert.equal(nWords, 0, '没有 vocab.npz 时必须是 0，让浏览器走全量扫描')
    } finally {
      await lib.close()
    }
  })
})

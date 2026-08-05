/**
 * 浏览器侧的识别库：解 `PARL` 包、词汇树量化、倒排粗排。
 *
 * 三件事放一个文件，因为它们是同一份数据的三个读法，而且**必须一起换** —— 词表变了，
 * 量化出来的词 id 就换了空间，倒排表随之作废。分成三个文件只会让"一起换"变成一件要靠
 * 记性的事。
 *
 * 服务端对应物：`photoar.vocab.Vocab.words_of` 与 `photoar.index.InvertedIndex.query`。
 * 两处的平局规则都逐条对齐了，理由写在各自函数上。
 */
import { REF_LONG_EDGE } from './consts.js'

/** 解 `server/library.js` 打的那个包。布局说明在那边的 `pack()` 上。 */
export function unpack(buf) {
  const dv = new DataView(buf)
  const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3))
  if (magic !== 'PARL') throw new Error(`库包 magic 不对：${magic}`)
  const version = dv.getUint32(4, true)
  if (version !== 1) throw new Error(`库包版本 ${version} 不认识`)
  const nPhotos = dv.getUint32(8, true)
  const nWords = dv.getUint32(12, true)
  const nNodes = dv.getUint32(16, true)
  const descDim = dv.getUint32(20, true)
  const refLongEdge = dv.getUint32(24, true)
  const jsonBytes = dv.getUint32(28, true)
  const meta = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 32, jsonBytes)))

  let p = 32 + jsonBytes
  const photos = []
  for (let i = 0; i < nPhotos; i++) {
    const m = meta.photos[i]
    const nPts = m.count * 2
    // pts 是 float32，而 p 只在描述子（uint8）之后才可能不对齐。TypedArray 视图要求
    // 偏移是元素宽度的整数倍，不对齐会抛一个和布局毫无关系的 RangeError —— 所以这里
    // 走 slice（拷一份）而不是视图。300×32 的 desc 让 p 始终是 4 的倍数，但依赖那个
    // 巧合就等于让格式的正确性取决于 nFeatures 的值。
    const pts = new Float32Array(buf.slice(p, p + nPts * 4))
    p += nPts * 4
    const desc = new Uint8Array(buf.slice(p, p + m.count * descDim))
    p += m.count * descDim
    photos.push({ ...m, count: m.count, pts, desc, descCols: descDim })
  }

  let vocab = null
  let index = null
  if (nWords > 0) {
    const centers = new Uint8Array(buf.slice(p, p + nNodes * descDim)); p += nNodes * descDim
    const childrenLen = new Int32Array(buf.slice(p, p + nNodes * 4)); p += nNodes * 4
    const childrenFlat = new Int32Array(buf.slice(p, p + meta.flatLen * 4)); p += meta.flatLen * 4
    const leafId = new Int32Array(buf.slice(p, p + nNodes * 4)); p += nNodes * 4
    const rootChildren = new Int32Array(buf.slice(p, p + meta.nRoot * 4)); p += meta.nRoot * 4
    const idf = new Float32Array(buf.slice(p, p + nWords * 4)); p += nWords * 4
    const offsets = new Int32Array(buf.slice(p, p + (nWords + 1) * 4)); p += (nWords + 1) * 4
    const docIds = new Int32Array(buf.slice(p, p + meta.nnz * 4)); p += meta.nnz * 4
    const weights = new Float32Array(buf.slice(p, p + meta.nnz * 4)); p += meta.nnz * 4

    // childrenFlat 是拼接的，这里还原成每个节点的切片起点。服务端 `Vocab.load` 做的
    // 是同一件事（那边用 cursor 累加），只是这里存偏移而不是切出一堆小数组。
    const childrenOff = new Int32Array(nNodes + 1)
    for (let i = 0; i < nNodes; i++) childrenOff[i + 1] = childrenOff[i] + childrenLen[i]
    vocab = { centers, nNodes, descDim, childrenFlat, childrenOff, leafId, rootChildren, nWords }
    index = { nDocs: nPhotos, idf, offsets, docIds, weights }
  }

  if (p !== buf.byteLength) {
    // 多一段少一段都会在这里露出来。不查的话浏览器会把下一段的字节当成自己的，
    // 而那些字节全都在合法范围内 —— 表现是"粗排莫名很差"。
    throw new Error(`库包长度对不上：读到 ${p}，实际 ${buf.byteLength}`)
  }
  return { photos, vocab, index, refLongEdge: refLongEdge || REF_LONG_EDGE, skipped: meta.skipped ?? [], meta }
}

const POPCOUNT = new Uint8Array(256)
for (let i = 0; i < 256; i++) POPCOUNT[i] = (i & 1) + POPCOUNT[i >> 1]

/**
 * 一批描述子 → 词 id。`photoar.vocab.Vocab.words_of` 的对译。
 *
 * 逐层取 Hamming 距离最小的子节点，直到叶子。**平局取下标最小的那个** —— numpy 的
 * `argmin` 就是这个语义，而这里的循环用严格小于（`d < best`）来保证同一件事。改成
 * `<=` 会在平局时取最后一个，于是同一个描述子在两侧被量化成不同的词 —— 粗排结果随之
 * 不同，而且只在少数描述子上不同，表现成"召回偶尔差一点"。
 */
export function wordsOf(vocab, desc, count, cols = 32) {
  const out = new Int32Array(count)
  const { centers, childrenFlat, childrenOff, leafId, rootChildren } = vocab
  for (let i = 0; i < count; i++) {
    const base = i * cols
    let cands = rootChildren
    let candFrom = 0
    let candTo = rootChildren.length
    let node = -1
    while (candTo > candFrom) {
      let best = 1e9
      let bestNode = -1
      for (let c = candFrom; c < candTo; c++) {
        const n = cands[c]
        const cb = n * cols
        let d = 0
        for (let b = 0; b < cols; b++) d += POPCOUNT[desc[base + b] ^ centers[cb + b]]
        if (d < best) { best = d; bestNode = n }
      }
      node = bestNode
      cands = childrenFlat
      candFrom = childrenOff[node]
      candTo = childrenOff[node + 1]
    }
    out[i] = node >= 0 ? leafId[node] : 0
  }
  return out
}

/**
 * 倒排粗排。`photoar.index.InvertedIndex.query` 的对译。
 *
 * 打分是 L2 归一化 tf-idf 的余弦相似度。**平局规则必须一致**：服务端用
 * `np.lexsort((cand, -scores[cand]))`，也就是主序 -score、次序 doc 下标升序。JS 的
 * `sort` 比较器写成 `b.score - a.score || a.doc - b.doc` 是同一个语义。
 * 不写次序的话平局顺序由排序实现决定，于是 Top-K 的边界上会随机换人。
 *
 * @returns `[{doc, score}]`，最多 topK 个
 */
export function queryIndex(index, words, topK) {
  const { nDocs, idf, offsets, docIds, weights } = index
  const nWords = idf.length
  if (nDocs === 0 || words.length === 0 || topK <= 0) return []

  const qtf = new Map()
  for (const w of words) {
    if (w < 0 || w >= nWords) throw new Error(`词 id ${w} 超出 [0,${nWords})`)
    qtf.set(w, (qtf.get(w) ?? 0) + 1)
  }
  let sq = 0
  const qw = new Map()
  for (const [w, c] of qtf) {
    const v = c * idf[w]
    qw.set(w, v)
    sq += v * v
  }
  const qnorm = Math.sqrt(sq)
  // qnorm == 0 意味着查询里每个词的 idf 都是 0（全库共有词）。服务端在这里返回空，
  // 而调用方靠 `unretrievableDocs` 那条兜底把全部文档并进候选 —— 不能在这里"顺手"
  // 改成均匀打分，那会改变已实测过的排序语义。
  if (qnorm === 0) return []

  const scores = new Float32Array(nDocs)
  for (const [w, v] of qw) {
    const start = offsets[w]
    const end = offsets[w + 1]
    if (start === end) continue
    const f = v / qnorm
    for (let i = start; i < end; i++) scores[docIds[i]] += weights[i] * f
  }

  const cand = []
  for (let d = 0; d < nDocs; d++) cand.push({ doc: d, score: scores[d] })
  cand.sort((a, b) => b.score - a.score || a.doc - b.doc)
  return cand.slice(0, Math.min(topK, nDocs))
}

/**
 * 哪些 doc 从未出现在倒排表里。`InvertedIndex.unretrievable_docs` 的对译。
 *
 * 这些文档的所有词 idf 都是 0，建索引时 tf-idf 范数为 0 被整体跳过 —— 它们**无法通过
 * 任何词被检索到**，所以必须无条件并进候选集。服务端 nullvocab 那一整套推理（空词表下
 * 行为退化成全量扫描但结果正确）就建立在这条上。
 */
export function unretrievableDocs(index) {
  const present = new Uint8Array(index.nDocs)
  for (const d of index.docIds) present[d] = 1
  const out = []
  for (let d = 0; d < index.nDocs; d++) if (!present[d]) out.push(d)
  return out
}

/**
 * 候选 doc 下标。`photoar.server.library._candidate_slots` 的对译。
 *
 * 三条分支，缺一条都会静默降低召回：
 *
 * 1. **库不大于 topK 时全查。** 粗排什么也筛不掉，而 nDocs==1 时它必然返回空
 *    （唯一文档的所有词 idf 都是 0）。
 * 2. **没有词表时全查。** 服务端那边是装 NullVocab 走同一条退化路径。
 * 3. **`unretrievable` 无条件并进来。** 见上面那个函数。服务端注释里记了这条推理曾经
 *    有个洞：`topK < nDocs <= 20` 时两个兜底都不成立、候选集为空 → 每次识别必然未命中，
 *    而日志全是正常的 200。所以这里**无条件**并，不加任何前置条件。
 */
export function candidateDocs(lib, queryDesc, queryCount, topK) {
  const n = lib.photos.length
  if (n === 0) return []
  if (n <= topK || !lib.vocab || !lib.index) {
    return Array.from({ length: n }, (_, i) => i)
  }
  const words = wordsOf(lib.vocab, queryDesc, queryCount, lib.vocab.descDim)
  const docs = queryIndex(lib.index, words, topK).map((r) => r.doc)
  const seen = new Set(docs)
  for (const d of unretrievableDocs(lib.index)) if (!seen.has(d)) docs.push(d)
  return docs
}

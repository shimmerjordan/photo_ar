/**
 * 读 photo-ar 的识别库（`data/library/`），投影成浏览器要的形状。
 *
 * ## 分工：为什么这一层在 Node 而不在浏览器、也不在 Python
 *
 * - **不在 Python**：web-front 是独立容器，只 bind mount 同一个 `data/library/` 只读。
 *   这样它可以自己发版、自己回滚，不碰那个已经在跑的服务端。
 * - **不在浏览器**：库文件是给 mmap 随机读设计的定长 slot（每张 12008 B）+ 两个 npz。
 *   让浏览器直接下这些原始文件意味着下**全库**（1000 张 = 12MB desc + 2MB 词汇树），
 *   而每个用户只被授权其中几十张。这一层的职责就是**按授权集裁剪**。
 * - **授权判定不在这里**：那是服务端的事，而且顺序有安全含义（`app.py` 那段注释：
 *   先按全局口径判定、再检查授权，反过来会提高误识别率、且只对权限受限的用户提高）。
 *   web-front 只拿着用户的 cookie 去问服务端「你能看哪些」，然后按那个列表投影。
 *
 * ## 一处必须写明的语义差异：idf 按授权集重算
 *
 * 服务端的 `index.npz` 是按**全库**建的（idf = log(n_docs/df)，df 是全库的）。而浏览器
 * 手上只有授权子集，所以这里**用子集重建一份倒排索引**。
 *
 * 这不是偷懒的近似，它就是「在子集上检索」的正确定义 —— 检索范围既然是授权集，df 就该
 * 按授权集算。但要清楚它与服务端**不是同一个排序**：同一帧在服务端和在浏览器，粗排的
 * Top-20 可以不同。两边的**精排与判定完全一样**（同一套阈值、同一份 verify 对译），
 * 所以差异只体现在"候选集里有没有那张照片"上。
 *
 * 授权集不超过 top_k 时这个差异根本不存在 —— 那时候服务端自己也全查
 * （`library._candidate_slots` 的第一个分支）。婚礼场景每人几十张，多数落在这一档。
 */
import { open, readFile, stat } from 'node:fs/promises'
import { join } from 'node:path'
import { parseNpz, toNumbers } from './npz.js'

/** ORB slot 布局。与 `photoar.descstore.ORB_LAYOUT` 必须一致，由 test/library.test.js 钉住。 */
export const ORB_LAYOUT = {
  nFeatures: 300,
  descDim: 32,
  headerBytes: 8,
  get ptsBytes() { return this.nFeatures * 2 * 4 },
  get descBytes() { return this.nFeatures * this.descDim },
  get ptsOffset() { return this.headerBytes },
  get descOffset() { return this.headerBytes + this.ptsBytes },
  get stride() { return this.headerBytes + this.ptsBytes + this.descBytes },
}

/** `features.LONG_EDGE`。参考侧特征空间的长边，四角换算要用它。 */
export const REF_LONG_EDGE = 640

/** 墓碑：`slots.json` 里被删除照片那一格是空串。**下标不能动** —— 它就是 desc.bin 的偏移。 */
const RETIRED = ''

export class Library {
  /** @param dir `data/library`（ORB 后端）。 */
  constructor(dir) {
    this.dir = dir
    this._fh = null
    this._slots = null
    this._vocab = null
    this._words = null
    this._mtime = 0
  }

  /**
   * 装载（或重新装载）库。
   *
   * 每次都查 `slots.json` 的 mtime：服务端入库会重写它，而 web-front 是长驻进程。
   * 不查的话新入库的照片对网页永远不存在，且没有任何报错。
   */
  async load() {
    const slotsPath = join(this.dir, 'slots.json')
    let st
    try {
      st = await stat(slotsPath)
    } catch (e) {
      if (e.code !== 'ENOENT') throw e
      // **全新部署：一张都还没入库时，服务端根本不会创建 `library/`。**
      //
      // 这不是故障，是"库是空的"。当成错误抛出去的后果很难看：部署完、登录、点开
      // 扫一扫，迎面一句 `ENOENT ... stat '/data/library/slots.json'`，还叫人去
      // "确认它被挂进容器" —— 而挂载完全正常，只是还没入库。**每一个新部署都会
      // 撞上这一下**（2026-08-06 真撞了）。
      //
      // 与下面容忍词表缺失是同一条理由，只是那一条当初只想到了词表。
      //
      // ⚠️ 只吞 ENOENT。`slots.json` 在但读不动（权限、坏 JSON、少 photo_ids）
      // 仍然要抛 —— 那些是真故障，而"静悄悄地当成空库"会让人对着一个永远认不出
      // 东西的页面查很久。
      this._mtime = 0
      this._slots = { photo_ids: [] }
      this._vocab = null
      this._words = null
      return this
    }
    if (this._slots && st.mtimeMs === this._mtime) return this
    this._mtime = st.mtimeMs

    this._slots = JSON.parse(await readFile(slotsPath, 'utf8'))
    if (!Array.isArray(this._slots.photo_ids)) throw new Error('slots.json 里没有 photo_ids')

    // 词汇树可能不存在 —— 全新部署时词表是空的（服务端装 NullVocab，行为退化成全量扫描
    // 但结果正确）。这里同样必须容忍，否则网页在"还没训词表"的部署上直接起不来。
    this._vocab = await this._loadVocab()
    this._words = await this._loadWords()

    await this._fh?.close()
    this._fh = await open(join(this.dir, 'desc.bin'), 'r')
    return this
  }

  async close() {
    await this._fh?.close()
    this._fh = null
  }

  /** 全部 slot 的 photo_id，含墓碑（空串）。下标即 desc.bin 的 slot 号。 */
  get photoIds() {
    return this._slots.photo_ids
  }

  get mtimeMs() {
    return this._mtime
  }

  /** photo_id → slot 下标。墓碑与不存在都返回 null。 */
  slotOf(photoId) {
    if (!photoId) return null
    const i = this._slots.photo_ids.indexOf(photoId)
    return i >= 0 ? i : null
  }

  async _loadVocab() {
    try {
      const z = parseNpz(await readFile(join(this.dir, 'vocab.npz')))
      return {
        centers: z.centers.data, // (nNodes, 32) uint8
        nNodes: z.centers.shape[0],
        descDim: z.centers.shape[1],
        childrenFlat: z.children_flat.data,
        childrenLen: z.children_len.data,
        leafId: z.leaf_id.data,
        rootChildren: z.root_children.data,
        nWords: Number(toNumbers(z.n_words.data)[0]),
      }
    } catch (e) {
      if (e.code === 'ENOENT') return null
      throw e
    }
  }

  /**
   * 每个 slot 的词序列（`words.bin`）。
   *
   * 服务端没有把 words 的布局像 desc 那样声明成一份文件格式，所以这里按「总长度 ÷ slot 数」
   * 反推 stride，并要求整除。对不上就抛 —— 按错的 stride 读只会得到一堆合法范围内的
   * 错词，表现成粗排莫名很差。
   */
  async _loadWords() {
    let buf
    try {
      buf = await readFile(join(this.dir, 'words.bin'))
    } catch (e) {
      if (e.code === 'ENOENT') return null
      throw e
    }
    const n = this._slots.photo_ids.length
    if (n === 0) return { stride: 0, buf, nSlots: 0 }
    if (buf.length % n !== 0) {
      throw new Error(`words.bin 长度 ${buf.length} 不能被 slot 数 ${n} 整除，布局对不上`)
    }
    return { stride: buf.length / n, buf, nSlots: n }
  }

  /** 某个 slot 的词序列（Int32Array，长度是它的真实 count）。 */
  wordsOfSlot(slot) {
    const w = this._words
    if (!w || w.stride === 0) return new Int32Array(0)
    const base = slot * w.stride
    const count = w.buf.readUInt32LE(base)
    const maxCount = Math.floor((w.stride - 8) / 4)
    if (count > maxCount) {
      throw new Error(`slot ${slot} 的 words count=${count} 超过布局上限 ${maxCount}`)
    }
    const out = new Int32Array(count)
    for (let i = 0; i < count; i++) out[i] = w.buf.readInt32LE(base + 8 + i * 4)
    return out
  }

  /**
   * 读一个 slot 的参考特征。
   *
   * @returns `{count, pts: Float32Array, desc: Uint8Array}`，与浏览器侧
   *   `verify.matchHamming` 直接吃的形状一致。
   */
  async readSlot(slot) {
    const L = ORB_LAYOUT
    const buf = Buffer.allocUnsafe(L.stride)
    const { bytesRead } = await this._fh.read(buf, 0, L.stride, slot * L.stride)
    if (bytesRead !== L.stride) {
      throw new Error(`slot ${slot} 读到 ${bytesRead} 字节，应为 ${L.stride}（desc.bin 被截断？）`)
    }
    const count = buf.readUInt32LE(0)
    if (count > L.nFeatures) throw new Error(`slot ${slot} 的 count=${count} 超过 ${L.nFeatures}`)
    const pts = new Float32Array(count * 2)
    for (let i = 0; i < count * 2; i++) pts[i] = buf.readFloatLE(L.ptsOffset + i * 4)
    const desc = new Uint8Array(count * L.descDim)
    buf.copy(desc, 0, L.descOffset, L.descOffset + count * L.descDim)
    return { count, pts, desc }
  }

  /**
   * 把授权集打包成一个**自包含**的二进制，浏览器一次下完就能离线识别。
   *
   * 布局（全部小端，与 npy / desc.bin 同序）：
   *
   * ```
   *  0  magic 'PARL'
   *  4  uint32 version = 1
   *  8  uint32 nPhotos
   * 12  uint32 nWords        （0 = 没有词表，浏览器全量扫描）
   * 16  uint32 nNodes        （词汇树节点数，0 = 没有词表）
   * 20  uint32 descDim       （32）
   * 24  uint32 refLongEdge   （640）
   * 28  uint32 jsonBytes
   * 32  JSON（UTF-8）
   *     之后按顺序、不补齐：
   *       每张照片： float32[count*2] pts, uint8[count*32] desc
   *       词汇树：   uint8[nNodes*32] centers, int32[nNodes] childrenLen,
   *                  int32[flatLen] childrenFlat, int32[nNodes] leafId,
   *                  int32[nRoot] rootChildren
   *       倒排：     float32[nWords] idf, int32[nWords+1] offsets,
   *                  int32[nnz] docIds, float32[nnz] weights
   * ```
   *
   * 为什么是自定义二进制而不是 JSON：desc 是 300×32 的随机字节，base64 进 JSON 涨 33%
   * 且要在浏览器里逐个 atob。为什么不直接发原始 `desc.bin`：那是全库，还带墓碑和未授权的。
   *
   * @param photos `[{id, aspect, title, mediaUrl, thumbUrl}]` —— 服务端说这个用户能看的那些。
   */
  async pack(photos) {
    const L = ORB_LAYOUT
    const entries = []
    const skipped = []
    for (const p of photos) {
      const slot = this.slotOf(p.id)
      if (slot === null || this._slots.photo_ids[slot] === RETIRED) {
        // 照片在 catalog 里但不在识别库里（或已退役）。真实原因通常是入库时被质量闸门
        // 或去重闸门拒了 —— 那种照片本来就永远认不出来。静默丢掉会让人以为识别坏了，
        // 所以如实报出去，前端在"库信息"里显示。
        skipped.push({ id: p.id, reason: 'not_in_library' })
        continue
      }
      const feat = await this.readSlot(slot)
      if (feat.count === 0) {
        skipped.push({ id: p.id, reason: 'empty_slot' })
        continue
      }
      entries.push({ meta: p, slot, feat })
    }

    const inv = this._buildSubIndex(entries.map((e) => e.slot))
    const v = this._vocab
    const withVocab = Boolean(v && inv)

    const jsonBuf = Buffer.from(JSON.stringify({
      photos: entries.map((e, i) => ({
        id: e.meta.id,
        doc: i, // 倒排索引里的 doc 下标 = entries 的下标
        count: e.feat.count,
        aspect: e.meta.aspect ?? null,
        title: e.meta.title ?? null,
        // ⚠️ 这是**元信息接口**的地址（`/v1/photo/<id>/media` 返回 JSON），
        // 不是视频流本身。真正的 mp4 在它返回的 `url` 字段上。字段名沿用服务端的
        // 叫法，但前端必须走两步 —— 直接塞给 `<video src>` 会让浏览器拿 JSON 去喂
        // 解封装器，报 DEMUXER_ERROR_COULD_NOT_OPEN 而 HTTP 是 200。
        mediaUrl: e.meta.mediaUrl ?? null,
        thumbUrl: e.meta.thumbUrl ?? null,
      })),
      nRoot: withVocab ? v.rootChildren.length : 0,
      flatLen: withVocab ? v.childrenFlat.length : 0,
      nnz: withVocab ? inv.docIds.length : 0,
      skipped,
      libraryMtimeMs: this._mtime,
    }), 'utf8')

    const parts = [null] // 头占位
    const push = (b) => parts.push(b)
    for (const e of entries) {
      push(Buffer.from(e.feat.pts.buffer, e.feat.pts.byteOffset, e.feat.pts.byteLength))
      push(Buffer.from(e.feat.desc.buffer, e.feat.desc.byteOffset, e.feat.desc.byteLength))
    }
    if (withVocab) {
      push(Buffer.from(v.centers.buffer, v.centers.byteOffset, v.centers.byteLength))
      push(i32buf(v.childrenLen))
      push(i32buf(v.childrenFlat))
      push(i32buf(v.leafId))
      push(i32buf(v.rootChildren))
      push(f32buf(inv.idf))
      push(i32buf(inv.offsets))
      push(i32buf(inv.docIds))
      push(f32buf(inv.weights))
    }

    const head = Buffer.alloc(32 + jsonBuf.length)
    head.write('PARL', 0, 'latin1')
    head.writeUInt32LE(1, 4)
    head.writeUInt32LE(entries.length, 8)
    head.writeUInt32LE(withVocab ? v.nWords : 0, 12)
    head.writeUInt32LE(withVocab ? v.nNodes : 0, 16)
    head.writeUInt32LE(L.descDim, 20)
    head.writeUInt32LE(REF_LONG_EDGE, 24)
    head.writeUInt32LE(jsonBuf.length, 28)
    jsonBuf.copy(head, 32)
    parts[0] = head

    return { buf: Buffer.concat(parts), nPhotos: entries.length, skipped }
  }

  /**
   * 在给定 slot 子集上重建倒排索引。`photoar.index.InvertedIndexBuilder.build` 的对译。
   *
   * doc 下标是**子集内的序号**（0..n-1），不是库里的 slot 号 —— 浏览器只认识子集。
   */
  _buildSubIndex(slots) {
    const v = this._vocab
    if (!v || !this._words || slots.length === 0) return null
    const nWords = v.nWords
    const nDocs = slots.length

    const tfs = slots.map((slot) => {
      const tf = new Map()
      for (const w of this.wordsOfSlot(slot)) {
        if (w < 0 || w >= nWords) throw new Error(`slot ${slot} 的词 id ${w} 超出 [0,${nWords})`)
        tf.set(w, (tf.get(w) ?? 0) + 1)
      }
      return tf
    })

    const df = new Int32Array(nWords)
    for (const tf of tfs) for (const w of tf.keys()) df[w]++

    const idf = new Float32Array(nWords)
    for (let w = 0; w < nWords; w++) {
      // df == nDocs → idf == 0：这个词子集里人人都有，不携带区分信息。一篇文档如果所有
      // 词都是这种，它的 tf-idf 范数为 0、整篇进不了倒排表 —— 那是**合法**的（服务端
      // nullvocab 的整套推理建立在这条上），浏览器侧靠 `unretrievable` 兜底。
      if (df[w] > 0) idf[w] = Math.log(nDocs / df[w])
    }

    const perWord = Array.from({ length: nWords }, () => [])
    for (let doc = 0; doc < nDocs; doc++) {
      const weights = new Map()
      let sq = 0
      for (const [w, c] of tfs[doc]) {
        const val = c * idf[w]
        weights.set(w, val)
        sq += val * val
      }
      const norm = Math.sqrt(sq)
      if (norm === 0) continue
      for (const [w, val] of weights) perWord[w].push([doc, val / norm])
    }

    const offsets = new Int32Array(nWords + 1)
    for (let w = 0; w < nWords; w++) offsets[w + 1] = offsets[w] + perWord[w].length
    const nnz = offsets[nWords]
    const docIds = new Int32Array(nnz)
    const weights = new Float32Array(nnz)
    for (let w = 0; w < nWords; w++) {
      let at = offsets[w]
      for (const [doc, val] of perWord[w]) {
        docIds[at] = doc
        weights[at] = val
        at++
      }
    }
    return { nDocs, idf, offsets, docIds, weights }
  }
}

function i32buf(arr) {
  const a = arr instanceof Int32Array ? arr : Int32Array.from(arr)
  return Buffer.from(a.buffer, a.byteOffset, a.byteLength)
}
function f32buf(arr) {
  const a = arr instanceof Float32Array ? arr : Float32Array.from(arr)
  return Buffer.from(a.buffer, a.byteOffset, a.byteLength)
}

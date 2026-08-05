/**
 * 读 numpy 的 `.npy` / `.npz`。零依赖（只用 `node:zlib`）。
 *
 * ## 为什么需要它
 *
 * 库文件里有两个 npz：`vocab.npz`（词汇树）与 `index.npz`（倒排索引）。它们是
 * `np.savez_compressed` 写的，也就是 **ZIP(DEFLATE) 包着几个 .npy**。
 *
 * 换成"让服务端多导出一份 JSON"这条路更省事，但那要改 Python 服务端 —— 而 web-front
 * 是一个独立容器，只 bind mount 同一个 `data/library/` 就够，不碰那边任何一行代码。
 * 这个取舍的代价就是这个文件（约 150 行），换来的是两个服务可以各自演进。
 *
 * ## 只支持真正会遇到的那一小块
 *
 * 刻意**不做**通用 npy 读取器：不支持 fortran_order、不支持结构化 dtype、不支持
 * object 数组。库里那两个 npz 只有 uint8 / int32 / int64 / float32 的 C 序连续数组。
 * 遇到别的**直接抛**而不是尽力解释 —— 尽力解释的结果是读出一段能用但错位的数据，
 * 而那会表现成"识别率莫名很低"。
 */
import { inflateRawSync } from 'node:zlib'

const MAGIC = Buffer.from([0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59]) // \x93NUMPY

/** npy 的 dtype 串 → TypedArray 构造器。**只列真正会遇到的**，理由见模块说明。 */
const DTYPES = {
  '|u1': Uint8Array,
  '<u1': Uint8Array,
  '|i1': Int8Array,
  '<i4': Int32Array,
  '<i8': BigInt64Array,
  '<f4': Float32Array,
  '<f8': Float64Array,
  '<u4': Uint32Array,
  '|b1': Uint8Array, // bool 按字节存
}

/**
 * 解一个 `.npy` 缓冲区。
 *
 * @returns `{dtype, shape, data}`，data 是对应的 TypedArray（**不拷贝**，是 buf 上的视图
 *   —— 除非字节偏移未对齐，见下面那段）。
 */
export function parseNpy(buf) {
  if (!buf.subarray(0, 6).equals(MAGIC)) throw new Error('不是 npy：magic 不对')
  const major = buf[6]
  let headerLen
  let dataStart
  if (major === 1) {
    headerLen = buf.readUInt16LE(8)
    dataStart = 10 + headerLen
  } else if (major === 2 || major === 3) {
    headerLen = buf.readUInt32LE(8)
    dataStart = 12 + headerLen
  } else {
    throw new Error(`不支持的 npy 版本 ${major}`)
  }
  const header = buf.subarray(dataStart - headerLen, dataStart).toString('latin1')

  const descr = /'descr'\s*:\s*'([^']+)'/.exec(header)?.[1]
  const fortran = /'fortran_order'\s*:\s*(True|False)/.exec(header)?.[1]
  const shapeStr = /'shape'\s*:\s*\(([^)]*)\)/.exec(header)?.[1]
  if (!descr || !shapeStr === undefined) throw new Error(`npy 头解不开：${header}`)
  if (fortran === 'True') {
    // 列主序。库里不会有，而按 C 序去读它得到的是转置后错位的数据 —— 不报错。
    throw new Error('不支持 fortran_order=True')
  }
  const Ctor = DTYPES[descr]
  if (!Ctor) throw new Error(`不支持的 dtype ${descr}`)

  const shape = shapeStr.trim() === ''
    ? []
    : shapeStr.split(',').map((s) => s.trim()).filter((s) => s !== '').map(Number)
  const count = shape.reduce((a, b) => a * b, 1)

  // TypedArray 视图要求字节偏移是元素大小的整数倍。npy 头会被补空格对齐到 64 字节，
  // 所以正常情况下都对齐；但 v3 或手写的文件可能不对齐，那时候必须拷贝 ——
  // 直接 new Ctor(buffer, off) 会抛一个看不懂的 RangeError。
  const bytes = count * Ctor.BYTES_PER_ELEMENT
  const off = buf.byteOffset + dataStart
  const aligned = off % Ctor.BYTES_PER_ELEMENT === 0
  const data = aligned
    ? new Ctor(buf.buffer, off, count)
    : new Ctor(Uint8Array.prototype.slice.call(buf, dataStart, dataStart + bytes).buffer, 0, count)
  return { dtype: descr, shape, data }
}

/**
 * 解一个 `.npz`（ZIP 包着若干 .npy）。
 *
 * 走 **central directory** 而不是顺序扫 local file header：后者遇到 streaming 写入
 * （sizes 放在 data descriptor 里、local header 里是 0）会读出长度 0 的成员，而那
 * 表现成"某个数组莫名是空的"。central directory 里的 size 始终是真的。
 *
 * @returns `{name: {dtype, shape, data}}`，name 不含 `.npy` 后缀（与 `np.load` 一致）。
 */
export function parseNpz(buf) {
  // EOCD：从尾部往前找签名 PK\x05\x06。注释最长 65535，所以最多回扫 65557 字节。
  const EOCD_SIG = 0x06054b50
  let eocd = -1
  const from = Math.max(0, buf.length - 65557)
  for (let i = buf.length - 22; i >= from; i--) {
    if (buf.readUInt32LE(i) === EOCD_SIG) {
      eocd = i
      break
    }
  }
  if (eocd < 0) throw new Error('不是 zip：找不到 EOCD')

  let count = buf.readUInt16LE(eocd + 10)
  let cdOffset = buf.readUInt32LE(eocd + 16)
  // ZIP64：字段被写成 0xFFFF/0xFFFFFFFF 表示"看 ZIP64 记录"。1000 张照片的库不会到
  // 4GB，但**读到哨兵值还按 32 位解**会解出一个荒谬的偏移然后抛在别处，所以这里点名。
  if (count === 0xffff || cdOffset === 0xffffffff) {
    throw new Error('ZIP64 的 npz 暂不支持（库文件不该到这个规模，先查是不是文件坏了）')
  }

  const out = {}
  let p = cdOffset
  for (let i = 0; i < count; i++) {
    if (buf.readUInt32LE(p) !== 0x02014b50) throw new Error(`central directory 第 ${i} 项签名不对`)
    const method = buf.readUInt16LE(p + 10)
    const compSize = buf.readUInt32LE(p + 20)
    const nameLen = buf.readUInt16LE(p + 28)
    const extraLen = buf.readUInt16LE(p + 30)
    const commentLen = buf.readUInt16LE(p + 32)
    const localOffset = buf.readUInt32LE(p + 42)
    const name = buf.subarray(p + 46, p + 46 + nameLen).toString('utf8')
    p += 46 + nameLen + extraLen + commentLen

    // local header 的可变长字段长度可能与 central directory 里的不同，必须自己读一遍。
    if (buf.readUInt32LE(localOffset) !== 0x04034b50) throw new Error(`${name} 的 local header 签名不对`)
    const lNameLen = buf.readUInt16LE(localOffset + 26)
    const lExtraLen = buf.readUInt16LE(localOffset + 28)
    const dataStart = localOffset + 30 + lNameLen + lExtraLen
    const raw = buf.subarray(dataStart, dataStart + compSize)

    let content
    if (method === 0) content = raw                      // ZIP_STORED（np.savez）
    else if (method === 8) content = inflateRawSync(raw) // ZIP_DEFLATED（np.savez_compressed）
    else throw new Error(`${name} 用了不支持的压缩方法 ${method}`)

    out[name.replace(/\.npy$/, '')] = parseNpy(content)
  }
  return out
}

/** BigInt64Array → Number 数组。npy 的 int64 只能解成 BigInt，而下游全是普通数字。 */
export function toNumbers(arr) {
  if (arr instanceof BigInt64Array) {
    const out = new Float64Array(arr.length)
    for (let i = 0; i < arr.length; i++) {
      const v = arr[i]
      // 2^53 之外的 int64 转 Number 会静默丢精度。库里的 offsets 最大是 postings 总数
      // （百万量级），远在安全范围内 —— 但真超了必须炸，因为丢精度的表现是倒排表读串。
      if (v > 9007199254740991n || v < -9007199254740991n) {
        throw new Error(`int64 值 ${v} 超出 Number 安全范围`)
      }
      out[i] = Number(v)
    }
    return out
  }
  return arr
}

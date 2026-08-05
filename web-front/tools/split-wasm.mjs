#!/usr/bin/env node
/**
 * 把 `vendor/opencv.js` 里内联的 wasm 抽成独立的 `opencv.wasm`，并 patch 加载逻辑。
 *
 * ## 为什么这一步值得做
 *
 * `@techstark/opencv-js` 只发单文件构建：wasm 以 **latin1 字符串**内联在 JS 里，运行时
 * 由 `binaryDecode()` 解成 `Uint8Array` 再交给 `WebAssembly.instantiate(bytes, …)`。
 *
 * 那条路径上**没有 URL**，而浏览器的 wasm code cache（"编译一次，之后直接加载编译结果"）
 * 只对 `WebAssembly.instantiateStreaming(fetch(url))` 生效 —— 它要靠 URL 做键。所以
 * 单文件构建的代价不只是体积，而是**每次刷新都重新编译整个模块**。
 *
 * 抽成独立 `.wasm` 之后，emscripten 自己就会走 fetch + instantiateStreaming
 * （`wasmBinaryFile` 是字符串时它就这么做），于是**浏览器原生的 code cache 生效** ——
 * 这正是"直接下载编译好的、免去编译"的正确实现方式。自己往 IndexedDB 存
 * `WebAssembly.Module` 那条路在 Chromium 上行不通（见 `wasmcache.js` 顶部）。
 *
 * 顺带省掉 latin1 转义在 UTF-8 里的膨胀：>0x7F 的字节在源文件里占 2 字节。
 *
 * ## 用法
 *
 *   node tools/split-wasm.mjs            # 就地生成 opencv.wasm + 改写 opencv.js
 *   node tools/split-wasm.mjs --check    # 只检查，不写
 *
 * 产物：
 *   public/vendor/opencv.wasm       独立 wasm
 *   public/vendor/opencv.js         patch 过的（`findWasmBinary` 返回 URL）
 *   public/vendor/opencv.orig.js    原件备份（第一次运行时保存，之后不再覆盖）
 *
 * ⚠️ 换 vendor 版本之后要重跑这个脚本，并且**重跑 `npm run test:golden`** ——
 * 换 OpenCV 版本等于换特征空间。
 */
import { readFile, writeFile, access } from 'node:fs/promises'
import { createHash } from 'node:crypto'
import { brotliCompress, constants as zlibConst } from 'node:zlib'
import { promisify } from 'node:util'
import { join, resolve } from 'node:path'

const brotli = promisify(brotliCompress)

const VENDOR = resolve(import.meta.dirname, '../public/vendor')
const JS = join(VENDOR, 'opencv.js')
const ORIG = join(VENDOR, 'opencv.orig.js')
const WASM = join(VENDOR, 'opencv.wasm')
/**
 * patch 后 `findWasmBinary` 返回的路径。必须是**浏览器能取到的绝对路径**，
 * 而且**必须带内容版本号**（下面 `?v=` 那一段，由这个脚本按 wasm 的 sha256 填）。
 *
 * 为什么版本号不能省：这个文件按 `Cache-Control: immutable, max-age=1年` 发。
 * 没有版本号的话，升级 OpenCV 之后已经访问过的浏览器会**抱着旧 wasm 不放一年**，
 * 而新的 `opencv.js` 配旧 wasm 的表现是"函数签名对不上"——一个极难查的错，
 * 且只在部分用户身上出现（没访问过的人是好的）。
 *
 * 用 query 而不是把哈希写进文件名：`orb.js` 的 `wasmUrlOf()` 是从 js 的 URL 推 wasm 的
 * URL（`.js` → `.wasm`，query 原样带过去），改文件名要让它去别处查一次才知道叫什么，
 * 而那一次查询正好卡在关键路径上。query 参与 CDN 的缓存键，效果一样。
 */
const WASM_URL_PATH = '/vendor/opencv.wasm'

const checkOnly = process.argv.includes('--check')

/**
 * 从 `binaryDecode('` 之后开始，扫出完整的 JS 字符串字面量**源文本**（含引号）。
 *
 * 不能简单地找下一个 `'`：那段字符串里几乎必然有转义的引号和反斜杠。所以逐字符扫，
 * 遇到 `\` 就跳过它后面那个字符。这一步错了的后果是截断的 wasm —— 而截断的 wasm
 * `WebAssembly.validate` 会拒，所以下面那道校验能兜住。
 */
function scanStringLiteral(src, quoteIdx) {
  const quote = src[quoteIdx]
  if (quote !== "'" && quote !== '"' && quote !== '`') {
    throw new Error(`偏移 ${quoteIdx} 处不是引号，而是 ${JSON.stringify(quote)}`)
  }
  let i = quoteIdx + 1
  while (i < src.length) {
    const c = src[i]
    if (c === '\\') { i += 2; continue }
    if (c === quote) return src.slice(quoteIdx, i + 1)
    i++
  }
  throw new Error('字符串字面量没有结束引号')
}

async function exists(p) {
  return access(p).then(() => true, () => false)
}

async function main() {
  // 已经 patch 过就用备份当输入，好让这个脚本可重复运行（换版本时直接覆盖 opencv.js
  // 再跑一次，而不是先手动恢复）。
  const source = (await exists(ORIG)) ? ORIG : JS
  let js = await readFile(source, 'utf8')
  console.log(`输入: ${source}  ${js.length} 字符`)

  const marker = 'function findWasmBinary(){return binaryDecode('
  const at = js.indexOf(marker)
  if (at < 0) {
    if (js.includes(`findWasmBinary(){return "${WASM_URL}"`) || js.includes(`findWasmBinary(){return '${WASM_URL}'`)) {
      console.log('已经 patch 过了（findWasmBinary 返回 URL）。')
      return 0
    }
    console.error('找不到 `function findWasmBinary(){return binaryDecode(` —— vendor 的构建方式变了。')
    console.error('先确认新版本是不是本来就带分离的 .wasm（那样这个脚本就不需要了）。')
    return 1
  }

  const literalStart = at + marker.length
  const literal = scanStringLiteral(js, literalStart)
  console.log(`内联字符串字面量: ${literal.length} 字符（源文本，含转义）`)

  // 用 JS 引擎自己解转义 —— 手写反转义几乎必然在某个 \xNN / \uNNNN / \0 上出错，
  // 而出错的表现是"wasm 少了几个字节"，那会被下面的 validate 抓住但很难定位。
  // eslint-disable-next-line no-new-func
  const decoded = new Function(`return ${literal}`)()
  if (typeof decoded !== 'string') throw new Error('解出来的不是字符串')

  // `binaryDecode` 的对译：`o[i] = ~c >> 8 & c`。
  // 对 c ≤ 255：~c = -(c+1) ∈ [-256,-1]，右移 8 位（算术）得 -1，而 -1 & c === c。
  // 也就是说它对 latin1 范围是恒等映射；那个位运算是给 >255 的字符兜底的
  // （真出现的话说明源文件编码被改过，那时候产物必然过不了 validate）。
  const bytes = Buffer.allocUnsafe(decoded.length)
  let outOfRange = 0
  for (let i = 0; i < decoded.length; i++) {
    const c = decoded.charCodeAt(i)
    if (c > 255) outOfRange++
    bytes[i] = (~c >> 8) & c
  }
  if (outOfRange) {
    console.warn(`⚠️  有 ${outOfRange} 个字符码 > 255。源文件的编码可能被改过。`)
  }

  console.log(`解出 wasm: ${bytes.length} 字节`)
  const magic = bytes.subarray(0, 4).toString('hex')
  if (magic !== '0061736d') {
    console.error(`wasm magic 不对：${magic}（应为 0061736d）。抽取失败，什么都没写。`)
    return 1
  }
  const version = bytes.readUInt32LE(4)
  console.log(`wasm magic ✔  version=${version}`)
  const sha = createHash('sha256').update(bytes).digest('hex')
  console.log(`sha256=${sha}`)
  // 12 个 hex（48 bit）。这不是防碰撞用的，是"内容变了 URL 必然变"用的。
  const WASM_URL = `${WASM_URL_PATH}?v=${sha.slice(0, 12)}`
  console.log(`wasm URL = ${WASM_URL}`)

  // 真的让 WebAssembly 验一遍。截断或错位的字节流能过 magic 检查但过不了这一步 ——
  // 而那种产物在浏览器里的表现是 `CompileError` 出现在 opencv.js 内部，看不出是抽取的锅。
  if (!WebAssembly.validate(bytes)) {
    console.error('WebAssembly.validate 拒绝了抽出来的字节。抽取失败，什么都没写。')
    return 1
  }
  console.log('WebAssembly.validate ✔')

  // patch：`findWasmBinary` 返回 URL。emscripten 见到字符串就会 fetch +
  // instantiateStreaming，于是浏览器的 code cache 生效。
  // 原文形状是 `function findWasmBinary(){return binaryDecode('…')}`，所以字符串字面量
  // 之后还有 **两个** 字符要跳过：`)` 和 `}`。第一版只跳了 `)`，于是替换文本自带的 `}`
  // 加上残留的 `}` 变成两个 —— 报错是 `Unexpected token 'function'`，出现在**下一个**
  // 函数上，看不出是这里多了个括号。
  const tailAt = literalStart + literal.length
  const tail2 = js.slice(tailAt, tailAt + 2)
  if (tail2 !== ')}') {
    console.error(`字符串之后的两个字符是 ${JSON.stringify(tail2)}，不是 ')}'。结构变了，不敢改。`)
    return 1
  }
  let patched =
    js.slice(0, at) +
    `function findWasmBinary(){return "${WASM_URL}"}` +
    js.slice(tailAt + 2)

  // ── 第二处 patch：让它真的去 fetch ────────────────────────────────
  //
  // 只把 `findWasmBinary` 改成返回 URL **不够**：单文件构建里 emscripten 把 fetch 那条
  // 路径整个编译掉了，`instantiateAsync` 直接把拿到的东西当字节传给
  // `WebAssembly.instantiate` —— 报错是
  // `Argument 0 must be a buffer source or a WebAssembly.Module object`。
  //
  // 所以还要换掉 `instantiateAsync`：字符串就走
  // `WebAssembly.instantiateStreaming(fetch(url), imports)`，其余原样。
  // **streaming 那条正是浏览器 code cache 唯一认的路径** —— 这才是"免去编译"的来源。
  //
  // 返回形状必须一致：`instantiateStreaming` 给 `{instance, module}`，而下游
  // `receiveInstantiationResult` 取的就是 `result["instance"]`。原来那条
  // `WebAssembly.instantiate(bytes, imports)` 返回的也是 `{instance, module}`
  // （变量名叫 instance，容易看错）。
  const asyncMarker = 'async function instantiateAsync(binary,binaryFile,imports){'
  const asyncAt = patched.indexOf(asyncMarker)
  if (asyncAt < 0) {
    console.error('找不到 instantiateAsync —— emscripten 的加载逻辑变了，不敢改。')
    return 1
  }
  const bodyEnd = patched.indexOf('}', asyncAt + asyncMarker.length)
  const oldBody = patched.slice(asyncAt, bodyEnd + 1)
  if (!oldBody.includes('instantiateArrayBuffer')) {
    console.error(`instantiateAsync 的函数体不是预期形状：${oldBody.slice(0, 200)}`)
    return 1
  }
  patched = patched.slice(0, asyncAt) +
    asyncMarker +
    'if(typeof binaryFile==="string"){' +
      'return WebAssembly.instantiateStreaming(fetch(binaryFile,{credentials:"same-origin"}),imports)' +
    '}' +
    'return instantiateArrayBuffer(binaryFile,imports)}' +
    patched.slice(bodyEnd + 1)
  console.log('instantiateAsync 已改为 streaming ✔')

  // **写之前先验语法。** 12MB 变 128KB 之后这一步只要几十毫秒，而它能挡住的正是
  // 上面那类括号错位 —— 那种错在浏览器里报在别的函数上，极难定位。
  try {
    // eslint-disable-next-line no-new-func
    new Function(patched)
  } catch (e) {
    console.error(`patch 后的 JS 语法不合法：${e.message}`)
    console.error('什么都没写。')
    return 1
  }
  console.log('patch 后的 JS 语法 ✔')

  const savedChars = js.length - patched.length
  console.log(`patch 后的 JS: ${patched.length} 字符（省 ${(savedChars / 1048576).toFixed(2)} MB 源文本）`)

  if (checkOnly) {
    console.log('--check：没有写任何文件。')
    return 0
  }

  if (!(await exists(ORIG))) {
    await writeFile(ORIG, js, 'utf8')
    console.log(`原件已备份到 ${ORIG}`)
  }
  await writeFile(WASM, bytes)
  await writeFile(JS, patched, 'utf8')
  console.log(`已写 ${WASM}`)
  console.log(`已写 ${JS}`)
  await precompress(WASM, bytes)
  await precompress(JS, Buffer.from(patched, 'utf8'))
  console.log()
  console.log('接下来：')
  console.log('  1. npm run test:golden   ← 必跑。换加载方式不该改描述子，但这是唯一的证据')
  console.log('  2. npm run test:worker   ← 验 Worker 里也能加载')
  console.log('  3. 更新 vendor/README.md 与 sha256.txt')
  console.log()
  console.log(`wasm 的 URL 版本号已经写进 opencv.js（${WASM_URL}）——`)
  console.log('不需要手工去别处改任何东西，老浏览器会因为 URL 变了自动重下。')
  return 0
}

/**
 * 预压 brotli，产物 `<文件>.br` 提交进仓库。
 *
 * ## 为什么在这里压、而不是在服务端按请求压
 *
 * q=11 压这 11.4MB 要 **21 秒**（实测）。按请求压就是每个宾客等 21 秒 CPU，而且
 * NAS 那颗 N5095 会更慢。预压是一次性的，且它天然与 wasm 同生同灭 —— 这个脚本
 * 一次写出四个文件（.wasm / .js / 各自的 .br），不可能只更新一半。
 *
 * ## 为什么值得
 *
 * 实测 11.40MB → **2.43MB（21.3%）**。在实测那条 0.35MB/s 的链路上，30 秒变 7 秒。
 * 这是整个加载路径上最大的一块，而且不依赖任何 CDN 配置。
 *
 * ## 为什么只压这两个
 *
 * 其余的静态资源要么本身已经压过（woff2 内含 brotli、PNG）、要么小到不值得
 * （theme.css 24KB，一个 RTT 的事）。**给每个 .js 都放一个 .br 是个陷阱**：
 * 改了源文件忘了重压，服务端就会发旧代码。这两个是生成物，不存在"手改了源文件"
 * 这回事。服务端那边另有一道 mtime 守卫，见 `serveStatic`。
 *
 * q=11 而不是 q=5：差 0.47MB（2.90 → 2.43），而代价只是构建时多 21 秒。
 */
async function precompress(path, buf) {
  const out = await brotli(buf, {
    params: {
      [zlibConst.BROTLI_PARAM_QUALITY]: 11,
      [zlibConst.BROTLI_PARAM_LGWIN]: 24,
      [zlibConst.BROTLI_PARAM_SIZE_HINT]: buf.length,
    },
  })
  await writeFile(`${path}.br`, out)
  const pct = ((100 * out.length) / buf.length).toFixed(1)
  console.log(`已写 ${path}.br  ${(out.length / 1048576).toFixed(2)}MB（${pct}%）`)
}

process.exit(await main())

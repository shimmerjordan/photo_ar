/**
 * 本机偏好：声音默认值、高级设置（整站数据源 + 单个媒体来源）、以及谁在监听变更。
 *
 * ## 为什么单独成一个模块
 *
 * `playback.js`（要不要默认有声）与 `netsrc.js`（要不要走整站数据源、走哪种
 * `targetAddressSpace`）都要读同一份状态，而设置页要写它、并让已经打开的其它
 * 页面（比如正在播放的宾客页）立刻感知变更 —— 这三者若各自摸一遍 `localStorage`，
 * 键名、默认值、坏数据兜底会各写一份、迟早对不齐。所以把「读、写、通知」收在
 * 这一个模块里，其余模块只认这里导出的函数，不直接碰 `localStorage`。
 *
 * ## 为什么每个读写都包 try/catch
 *
 * 隐私模式下 `localStorage.getItem/setItem` 会直接抛异常（不是返回 null）。
 * 这里的东西都不是"必须成功"的关键路径 —— 声音偏好、数据源设置，拿不到就退回
 * 默认值，写不进去就当**这次会话里**改了、下次刷新打回默认，好过让整页崩掉。
 *
 * ## `overrides` 的形状
 *
 * 每张照片最多一条「单个媒体来源」（見设计 §2.3）：要么是 `{kind:'url', url}`
 * 直链，要么是 `{kind:'file', name, size, type, at}` —— 注意这里**不存文件本身**，
 * 那份字节复制在 Cache Storage `photoar-override-v1` 里（mediastore.js 的活）；
 * 这里只存"有没有配、配的是什么样子"的元信息，用来在设置页/媒体页显示与判断
 * 要不要去 override 缓存里找那份字节。
 */

const SOUND_KEY = 'photoar.sound'
const ADV_KEY = 'photoar.adv.v1'

/** 高级设置的默认形状。`overrides` 单独给，见 `advanced()` 里的合并说明。 */
export const ADV_DEFAULT = { mediaBase: '', mediaBaseOn: false, lanMode: null }

const listeners = new Set()

/** 订阅变更（声音开关、高级设置的任何一次 `set*`）。返回取消订阅函数。 */
export function onPrefsChange(fn) {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function notify() {
  for (const fn of listeners) {
    try { fn() } catch { /* 一个监听者炸了不该连累其它监听者 */ }
  }
}

/** 声音偏好，缺省有声（设计 §2.4：浏览器只保证静音自动播放，我们默认尝试有声）。 */
export function soundOn() {
  try {
    return localStorage.getItem(SOUND_KEY) !== 'off'
  } catch {
    return true
  }
}

export function setSoundOn(on) {
  try {
    localStorage.setItem(SOUND_KEY, on ? 'on' : 'off')
  } catch { /* 隐私模式：这次会话里仍然生效，刷新后打回默认 */ }
  notify()
}

/** 坏 JSON、非对象都当没配过，不让一条脏数据把整个高级设置读挂。 */
function loadAdvRaw() {
  let raw
  try {
    raw = localStorage.getItem(ADV_KEY)
  } catch {
    return {}
  }
  if (!raw) return {}
  try {
    const v = JSON.parse(raw)
    return v && typeof v === 'object' ? v : {}
  } catch {
    return {}
  }
}

/** `overrides` 键固定是 `photoId → {kind:'url', url} | {kind:'file', name, size, type, at}`。 */
export function advanced() {
  const raw = loadAdvRaw()
  return {
    ...ADV_DEFAULT,
    ...raw,
    overrides: raw.overrides && typeof raw.overrides === 'object' ? raw.overrides : {},
  }
}

function saveAdv(v) {
  try {
    localStorage.setItem(ADV_KEY, JSON.stringify(v))
  } catch { /* 隐私模式：这次会话里仍然生效 */ }
  notify()
}

/** 合并式写入：`patch` 里没提到的字段保持原样（设置页的每个控件只改自己那一项）。 */
export function setAdvanced(patch) {
  const cur = advanced()
  saveAdv({ ...cur, ...patch })
}

export function overrideOf(photoId) {
  return advanced().overrides[photoId] ?? null
}

/** `ov === null` 表示删除这一条（用户在设置页点了"移除"）。 */
export function setOverride(photoId, ov) {
  const cur = advanced()
  const overrides = { ...cur.overrides }
  if (ov == null) delete overrides[photoId]
  else overrides[photoId] = ov
  saveAdv({ ...cur, overrides })
}

/**
 * 把用户填的整站数据源地址收拾成"可以直接拼路径用"的样子。
 *
 * - 空串放行（= 还没配，`mediaBaseOn` 会是 false，这条不影响任何行为）。
 * - 必须是 `http:`/`https:`，否则整条地址在 `fetch` 里毫无意义 —— 直接拒收好过
 *   存一条永远连不上的地址，用户点"测试"只会得到一句看不出原因的失败。
 * - 去掉末尾 `/`：`sourcesFor` 会用 `base + path` 直接拼，留着会拼出 `//v1/...`。
 * - 保留路径前缀（NAS 可能把服务架在子路径下），丢弃 query/hash —— 那两截不该
 *   是"服务地址"的一部分，带进去只会让每次拼出来的请求地址多余地不一样。
 */
/**
 * 保存「媒体数据源」地址时，`lanMode`（`probeBase` 测出的写法）该带哪一个值。
 *
 * **fix1 Finding 2**：这一步之前是设置页自己在 `saveBase` 里拿 `advanced().lanMode` 判断的，
 * 而 `advanced().lanMode` 会被「测试」按钮（`runTest`）在测出结果的那一刻**立即**写进去 ——
 * 不管测的是不是当前已保存的地址。于是：已保存 Y（写法 local），在输入框填 X 点「测试」，
 * `lanMode` 被直接改成 X 的写法；这时哪怕用户压根没点保存，Y 的写法已经被污染了。用户如果
 * 把输入框改回 Y 再点保存，`b === a.mediaBase` 成立，"保留原来的 lanMode" 这条分支保留的
 * 却是已经被污染的值。
 *
 * 抽成纯函数是为了把"该不该写"从"已经写没写对"里剥出来单独钉住：调用方（`runTest`）现在只在
 * `toSave === savedBase`（测的正是当前生效的地址）时才允许立即写 `advanced().lanMode`；
 * 其余情况只记在本地变量（`testedBase`/`testedLanMode`）里，等真正保存时才由这个函数决定
 * 最终写哪个值 —— 局部变量不经过 `setAdvanced`，不会被这条判断污染。
 *
 * - 清空地址 → `null`。
 * - 保存的正是刚测过的那个地址 → 用刚测出来的写法（最新鲜、最可信）。
 * - 保存的是没测过、但与当前已保存地址相同的那个 → 保留已保存的写法（没理由清掉一个还有效的）。
 * - 其它（换了个从没测过的新地址）→ `null`，等下一次「测试」或下载时探出来。
 */
export function pickLanMode({ toSave, savedBase, savedLanMode, testedBase, testedLanMode }) {
  if (!toSave) return null
  if (toSave === testedBase) return testedLanMode ?? null
  if (toSave === savedBase) return savedLanMode ?? null
  return null
}

export function normalizeBase(raw) {
  const s = String(raw ?? '').trim()
  if (!s) return ''
  let u
  try {
    u = new URL(s)
  } catch {
    throw new Error('地址要以 http:// 或 https:// 开头，例如 http://192.168.1.10:8964')
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') {
    throw new Error('地址要以 http:// 或 https:// 开头，例如 http://192.168.1.10:8964')
  }
  const path = u.pathname.replace(/\/+$/, '')
  return u.origin + path
}

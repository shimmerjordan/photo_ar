/**
 * 扫描阶段的引导文案。**每一句都必须能照着做，而且要区分原因。**
 *
 * 真机上「扫不出」的主导变量是照片在取景框里的占比（`bench/simcam.py` 的结论：
 * 真实照片最小可用占比 0.5）。内点 15~39 且连续几帧，几乎只有一个解释：照片太小/太远。
 * 所以那一档说的是「再靠近」，而不是笼统的「认不出来」。
 *
 * 放在 scan.js 外面是为了能在 node 里逐档钉住：这些分档是**产品判断**（哪一种处境该给
 * 哪一个下一步动作），而它过去混在一个 `TIPS[m.reason] ?? TIPS.scanning` 的查表里 ——
 * 查表既表达不了「连续 3 帧」也表达不了「30 秒没证据」，于是那两档根本不存在。
 */
export const GUIDE_DEBOUNCE_MS = 2000
/** 内点到了这个数说明「看见了、只是不够」——再低就是根本没对上，那时说靠近没有意义。 */
export const CLOSER_MIN_INLIERS = 15
/** 连续几帧落在那一档才改口说「再靠近」。单帧偶尔弱是常态，不该为它换一句话。 */
export const CLOSER_RUN = 3
export const NOT_IN_LIBRARY_MS = 30_000

const TEXT = {
  scanning: '把整张照片放进画面，靠近一点、拿稳。',
  no_features: '画面里几乎没有纹理。对准照片，光线亮一点，避开纯色的墙面。',
  weak: '认不出来。让照片占满画面多一些，手指别压住边缘，避开反光。',
  closer: '再靠近一点，让照片占满取景框。',
  ambiguous: '端稳一点，别晃 —— 库里有两张很像的照片，需要看得更清楚。',
  not_in_library: '一直认不出来。这张照片可能不在库里 —— 看看下面「我能扫的照片」。',
  empty: '这个账号下还没有可扫的照片。',
}

export function guideTip({ reason = '', inliers = 0, weakRun = 0, idleMs = 0, streak = null } = {}) {
  if (streak && streak.n > 0) return { key: 'streak', text: `看到了，拿稳别动… ${streak.n}/${streak.need}` }
  if (reason === 'empty') return { key: 'empty', text: TEXT.empty }
  if (idleMs >= NOT_IN_LIBRARY_MS) return { key: 'not_in_library', text: TEXT.not_in_library }
  if (reason === 'no_features') return { key: 'no_features', text: TEXT.no_features }
  if (reason === 'ambiguous') return { key: 'ambiguous', text: TEXT.ambiguous }
  if (reason === 'weak' && inliers >= CLOSER_MIN_INLIERS && weakRun >= CLOSER_RUN) return { key: 'closer', text: TEXT.closer }
  if (reason === 'weak') return { key: 'weak', text: TEXT.weak }
  return { key: 'scanning', text: TEXT.scanning }
}

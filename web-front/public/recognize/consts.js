/**
 * 识别管线的全部数值常量。**唯一那一份**，且每一个都在服务端有对应物。
 *
 * ## 为什么敢把服务端的常量抄到这里
 *
 * 不敢 —— 所以有 `test/consts-parity.test.js`：它直接读 `src/photoar/*.py`，把常量解出来
 * 和这里逐个比。抄错或者服务端改了这边没跟，那条测试红。
 *
 * 没有那条测试的话，这个文件是整个 web 端最危险的地方：改一个数不会报错，只会让识别率
 * 变成另一个值，而"识别率变了"这件事在真机上要靠人反复扫才感觉得到。
 *
 * ## 哪些**不该**在这里
 *
 * 服务端把 `recog.min_inliers` / `recog.ratio` / `recog.top_k` 做成了管理台上能改的热配置
 * （`app_config` 表，`needs_restart=False`）。也就是说这三个数的**真相在服务端运行时**，
 * 不在源码里。这里的值是"服务端源码里的默认值"，浏览器启动时应当用服务端下发的值覆盖
 * —— 见 `applyServerConfig`。不覆盖的后果是管理台上调了阈值而 web 端不跟，
 * 表现成"同一张照片 App 认得出、网页认不出"。
 */

/** 入库侧（参考图）：`photoar.features` 的默认值。库里的 `desc.bin` 就是按这组算的。 */
export const REF_LONG_EDGE = 640
export const REF_N_FEATURES = 300

/** ORB 金字塔。两侧共用 —— `features._detector` 只传这三个参数。 */
export const SCALE_FACTOR = 1.2
export const N_LEVELS = 8

/** 描述子字节数。ORB 固定 256 bit。 */
export const DESC_BYTES = 32

/**
 * 查询侧：`backend.QUERY_LONG_EDGE` / `QUERY_N_FEATURES`。
 *
 * 与入库侧**故意不同**，而且这是识别率的主导变量，不是笔误。入库时照片铺满画面，
 * 300 个特征全落在照片上；手持扫描时照片只占画面的一部分（实测自然举手距离
 * 0.4~0.5），同样的预算要摊给桌面、墙面、旁边的杂物。服务端注释里那张表：
 * 「长边 640 + 300 特征」一档都不全过门槛，「1280 + 4000」才到 0.4。
 *
 * 也别以为把发帧分辨率提上去就够了 —— 那张表第二行「发帧 1280 / 处理 640」等于完全
 * 没改。**处理长边比发帧长边更主导。**
 */
export const QUERY_LONG_EDGE = 1280
export const QUERY_N_FEATURES = 4000

/** 判定阈值：`photoar.verify` 的模块常量。可被服务端热配置覆盖，见 `applyServerConfig`。 */
export const thresholds = {
  minInliers: 40,
  ratio: 1.5,
  detMin: 0.05,
  detMax: 20.0,
  topK: 20,
  // 跨帧累积那两个（见 `streak.js`）。与服务端 `streak.py` 的常量同一组数。
  streakNeed: 3,
  streakSoftMin: 30,
}

/** RANSAC。**不可配** —— 服务端也没把它做成热配置，两侧必须完全一样。 */
export const RANSAC_REPROJ = 3.0
export const RANSAC_MAX_ITERS = 200
/**
 * `cv2.findHomography` 的 `confidence` 默认值。
 *
 * 服务端那行没写它（用的是 Python 默认值），而 opencv.js 的 `findHomography` **必须**
 * 把它显式传进去才能给 `maxIters`。抄错这个数不会报错，只会让 RANSAC 的自适应终止条件
 * 变了 —— 内点数随之小幅漂移，而漂移方向不确定。
 */
export const RANSAC_CONFIDENCE = 0.995
/** `verify.MIN_MATCHES_FOR_HOMOGRAPHY`。低于它连矩阵都解不出来。 */
export const MIN_MATCHES_FOR_HOMOGRAPHY = 4

/**
 * 把服务端下发的热配置盖到 `thresholds` 上。
 *
 * 只认**已知的键**、且只接受有限数字：服务端将来多下发一个字段不该让这里静默多出一个
 * 阈值，而一个 `null` 或字符串混进来会让后面每一次比较都变成 false（也就是"永远认不出"，
 * 且没有任何报错）。
 */
export function applyServerConfig(cfg) {
  if (!cfg || typeof cfg !== 'object') return thresholds
  for (const k of ['minInliers', 'ratio', 'detMin', 'detMax', 'topK', 'streakNeed', 'streakSoftMin']) {
    const v = cfg[k]
    if (typeof v === 'number' && Number.isFinite(v)) thresholds[k] = v
  }
  return thresholds
}

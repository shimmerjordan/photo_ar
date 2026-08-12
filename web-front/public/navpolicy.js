/**
 * 谁能看到哪几个页签、登录后落在哪一页。**Android `NavPolicy` 的对译。**
 *
 * 纯函数、单独一个文件、有测试 —— 与 Android 那边同一个理由：这里管的是**权限边界**，
 * 而「访客身上少挡了一个页签」这种错在自己账号（管理员）上永远看不出来，得拿访客身份
 * 登进去才会现形，而那正是最容易忘的一步。
 *
 * ## 服务端已经挡了，为什么客户端还要挡
 *
 * 服务端才是权限的真相（`/v1/history`、`/v1/upload`、`/v1/admin/*` 都是 admin only，
 * 访客拿到 403）。这里挡的**不是安全，是可用性**：改造前访客点「历史」得到的就是一个
 * 403 报错，而他什么都没做错。少给他一个点不动的入口，不是把风险挡在外面，是别把一条
 * 必然失败的路摆在他面前。
 *
 * ## 与 Android 的一处**刻意不同**：所有人都落在扫描页
 *
 * Android 让管理员落在照片库（"登录后第一件事通常是看一眼库里有什么"）。网页版不同 ——
 * 它的入口是宾客扫码打开的一个链接，**扫描就是这个页面存在的理由**。所以
 * `landingTab` 对两种角色都返回 SCAN，而管理员的其余页签照旧。
 */

export const Tab = {
  /** 首页：内嵌的扫描视图。**所有人的落地页。** */
  SCAN: 'scan',
  /** 照片库（管理员）。 */
  PHOTOS: 'photos',
  /** 素材：把手机里的照片/视频传上去，一次传完就是一组映射。 */
  MEDIA: 'media',
  /** 管理：管理台入口 + 识别历史 + 缓存。 */
  ADMIN: 'admin',
  SETTINGS: 'settings',
}

/**
 * 访客的三个页签。
 *
 * ## 为什么从两个变成三个（2026-08-10）
 *
 * 原来只有「扫一扫 + 设置」，理由是"宾客打开这个页面只有一件事可做，多一个入口就多
 * 一次我该点哪个"。那条理由**在第二件事出现之后就不成立了** —— 宾客现在能把自己被
 * 授权的照片和视频存到手机上，而那不是站在照片前面做的事（他多半是回家路上做的），
 * 扫描页里塞不下也不该塞。
 *
 * 「媒体」这一页对两种角色是**不同的东西**：管理员看到的是全库与策展入口，宾客看到
 * 的是他自己那几张 + 两个下载按钮。分叉在 `photos.js` 里按角色做，服务端那一侧
 * `/v1/photos` 本来就按授权过滤，所以宾客看到的行数天然就是对的。
 *
 * 「历史」和「素材」仍然不给访客，而且不是因为界面挤 —— `/v1/history` 与 `/v1/upload`
 * 在服务端是 admin only（前者是**全库**的识别记录，给访客等于把全库照片的标题发给他）。
 */
export const VIEWER_TABS = [Tab.SCAN, Tab.PHOTOS, Tab.SETTINGS]

/**
 * 管理员的五个页签。
 *
 * 比 Android 多一个 SCAN —— 那边管理员的扫一扫是悬在底栏上的 FAB，而网页版把扫描
 * 做成了首页（用户要求），所以它对两种角色都是一个页签。
 */
export const ADMIN_TABS = [Tab.SCAN, Tab.PHOTOS, Tab.MEDIA, Tab.ADMIN, Tab.SETTINGS]

export function tabsFor(isAdmin) {
  return isAdmin ? ADMIN_TABS : VIEWER_TABS
}

/**
 * 登录之后落在哪一页。**两种角色都是扫描页**，理由见模块说明。
 */
export function landingTab() {
  return Tab.SCAN
}

/**
 * 角色变了之后，原来那一页还能不能待。
 *
 * 用得到的场景是**同一台手机换人登录**：管理员登出、家里人用访客身份登进来，而界面
 * 还停在「素材」页。不换的话那一页上每个按钮都会 403。
 */
export function tabAfterRoleChange(current, isAdmin) {
  const allowed = tabsFor(isAdmin)
  return allowed.includes(current) ? current : landingTab()
}

/**
 * 页签的显示名与图标（16×16 像素风，见 `pixelicons.js`）。
 *
 * ⚠️ **label 改了不要跟着改上面那些 slug。** `Tab.PHOTOS` 的值 `'photos'` 是 URL 的
 * 一部分（`#/photos`），也写在文档里、可能在谁的收藏夹里。显示名与路由分开，
 * 就是为了让"这一页叫什么"能改而不动地址。
 */
export const TAB_META = {
  [Tab.SCAN]: { label: '扫一扫', icon: 'scan' },
  // 「媒体」而不是「照片」：这一页管的是**照片和它配的那段视频**这一对，而
  // 「照片」让人以为只是个相册。旁边那个「素材」是上传入口（还没入库的东西）。
  [Tab.PHOTOS]: { label: '媒体', icon: 'photo' },
  [Tab.MEDIA]: { label: '素材', icon: 'upload' },
  [Tab.ADMIN]: { label: '管理', icon: 'admin' },
  [Tab.SETTINGS]: { label: '设置', icon: 'gear' },
}

/**
 * 非根页面（从某个页签里推进去的）。
 *
 * 与 Android 的 `Route` 一一对应。它们**不进底栏** —— Android 那边的理由同样成立：
 * 历史与缓存是「出门前准备一次」的页面，日常不需要，而底栏每多一格都会让最常用的
 * 那两个变窄。
 */
export const Page = {
  DETAIL: 'detail',   // 照片详情（admin：带 NAS 路径与策展动作）
  VIEW: 'view',       // 全屏查看 + 存到手机（宾客那一版的「详情」）
  PLAY: 'play',       // 试播：不开相机，全屏放这张照片配的视频
  HISTORY: 'history', // 识别历史（admin）
  CACHE: 'cache',     // 缓存（web 语义见那一页）
}

/** 这一页是不是底栏上的根。根之间没有「返回」关系，切根会清空栈。 */
export function isRoot(name) {
  return Object.values(Tab).includes(name)
}

/**
 * 一页需不需要 admin。
 *
 * 与服务端的 admin-only 接口一一对应，**不多不少**：多挡一页等于无谓地少给功能，
 * 少挡一页等于把 403 摆在访客面前。
 *
 * ## `DETAIL` 要 admin 而 `VIEW` 不要，是这张表里唯一需要解释的一条
 *
 * 两页看的是同一张照片，差别在**接口**：详情页调 `/v1/photo/<id>`，而那个响应里有
 * `refPath` / `videoPath`（NAS 上的绝对路径）和 `selfScore` —— 服务端会把它们发给
 * 任何有授权的人。宾客不需要那些，也不该看到 NAS 的目录结构。所以宾客那一版
 * （`VIEW`）**根本不调那个接口**，它要的标题从 `/v1/photos` 拿。
 */
export function needsAdmin(name) {
  return [Tab.MEDIA, Tab.ADMIN, Page.DETAIL, Page.PLAY, Page.HISTORY].includes(name)
}

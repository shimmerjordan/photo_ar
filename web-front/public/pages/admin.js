/**
 * 管理：管理台入口 + 识别历史 + 缓存。**Android `AdminScreen` 的对译。**
 *
 * ## 与 Android 的一处**结构性差别**：不需要内嵌 WebView
 *
 * Android 那边有两个入口（「在 App 里打开」= 内嵌 WebView，「在浏览器里打开」= 跳系统
 * 浏览器），而且要解释一堆代价：WebView 里点不动多层弹窗、浏览器里要**再登一次**
 * （那是另一条会话）、以及 §31 那个「WebView 里 `<input type=file>` 默认什么都不做」。
 *
 * **网页版这些全部不存在**：这个页面本来就在浏览器里，`/admin` 与它同源、共用同一个
 * cookie。所以只有一个入口，而且没有任何代价要解释。
 *
 * 唯一保留的那条警告是真的：**在管理台里点「登出」会把这里的登录一起作废**
 * —— 它们是同一条会话（服务端注释：token 就是 cookie 的值）。
 */
import { Page } from '../navpolicy.js'
import { button, h, section } from '../ui.js'

export default {
  title: '管理',

  async mount(el, ctx) {
    el.appendChild(section('管理台',
      h('p', { class: 'p', text: '用户、授权、识别参数、照片↔视频映射、Excel 批量导入都在管理台里。' }),
      h('p', { class: 'p dim', text: '它与这个页面同源、共用同一条会话 —— 打开不用再登一次。' }),
      h('div', { class: 'actions' },
        // 新标签页打开：管理台是一个完整的应用（表格、多层弹窗），而这里是一个
        // 全屏手机界面。同一个标签里跳走会让用户丢掉扫描页的状态。
        button('打开管理台', () => window.open('/admin', '_blank', 'noopener'), { kind: '' })),
      h('p', { class: 'p warn', text: '注意：在管理台里点「登出」会把这里的登录一起作废（是同一条会话）。想换账号才用那一条。' })))

    el.appendChild(section('识别历史',
      h('p', { class: 'p', text: '全库的识别记录：什么时候、哪张、多少内点、为什么没命中。' }),
      h('p', { class: 'p dim', text: '扫不出来时第一个该看的地方 —— 它能分开「这一帧没拍好」和「库里有近重复」。' }),
      h('div', { class: 'actions' },
        button('看识别历史', () => ctx.shell.push(Page.HISTORY), { iconName: 'clock' }))))

    el.appendChild(section('本机缓存',
      h('p', { class: 'p', text: '识别库、已看过的视频、以及识别引擎的编译缓存。' }),
      h('p', { class: 'p dim', text: '识别库和视频都由浏览器自己缓存，那一页只显示占了多少、以及清掉它。' }),
      h('div', { class: 'actions' },
        button('看缓存', () => ctx.shell.push(Page.CACHE), { iconName: 'cache' }))))

    // 这一页是纯静态的，没有请求、没有定时器、没有 DOM 之外的资源。
    return () => {}
  },
}

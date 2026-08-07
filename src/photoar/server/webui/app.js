'use strict';
/* photoar 管理台。原生 DOM + fetch，没有框架、没有构建、没有 CDN。
 *
 * ## 凭证：只靠 cookie，一个字节都不存
 *
 * 登录成功时后端下发了一个 `HttpOnly; SameSite=Lax; Path=/` 的会话 cookie
 * （见 `app.Server._session_cookie`）。这个页面**完全依赖它**，所有 fetch 只写
 * `credentials: 'same-origin'`，从不自己拼 Authorization 头。
 *
 * 为什么不把登录响应里那个明文 token 存进 localStorage：localStorage 里的东西
 * 任何一次 XSS 都能整条读走，而 HttpOnly cookie 读不到 —— 同一个 XSS 只能借着
 * 当前页面发请求，偷不走一个能离线带走、能贴到别处用的凭证。代价是页面自己不
 * 知道自己有没有登录（读不到 cookie），所以启动时要探一次 `/v1/auth/me`。
 *
 * 顺带这也是缩略图能显示的唯一办法：`<img src>` 没有任何办法带上 Authorization
 * 头（fetch 能，标签不能），所以 `/v1/photo/<id>/thumb` 这种 URL 直接写进
 * `<img>` 就行 —— 它靠的是同一个 cookie，**不是漏了鉴权**。后端那边同一个
 * `_credential` 认两条路：App 用 Bearer 头，浏览器用 cookie。
 *
 * ## 所有服务端数据一律 textContent
 *
 * 名字、照片标题、后端的报错文案全都来自库里，一律用 textContent 写进 DOM，
 * 从不 innerHTML。理由不是教条：标题默认取自文件名，而 `<img onerror=…>.jpg`
 * 是一个合法的文件名。而这个页面手上正握着一个能打所有管理接口的会话。
 */

// ============================== 基础工具 ==============================

const API = '/v1';

const $ = (id) => document.getElementById(id);

/** 建元素。text 一律走 textContent（见文件头注释）。 */
function el(tag, opts, kids) {
  const n = document.createElement(tag);
  const o = opts || {};
  for (const k in o) {
    const v = o[k];
    if (v === null || v === undefined || v === false) continue;
    if (k === 'text') n.textContent = String(v);
    else if (k === 'cls') n.className = v;
    else if (k === 'html') throw new Error('不许用 innerHTML');
    else if (k in n && k !== 'list' && typeof v !== 'object') n[k] = v;
    else n.setAttribute(k, v === true ? '' : String(v));
  }
  for (const c of kids || []) {
    if (c === null || c === undefined || c === false) continue;
    n.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return n;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

/**
 * 把 `**这样**` 的片段渲染成加粗，其余原样。
 *
 * 服务端的字段说明（appconfig.py 的 help）里有意用 `**…**` 标了最要命的那几句
 * （「填 1.0 等于**关掉**这条判定」、「**两张都永久扫不出来**」）。原样显示会出现
 * 一串星号，看起来像 bug；而这里**不解析 HTML** —— 只切字符串、只造文本节点和
 * <strong>，所以说明文字里就算有 `<script>` 也只是几个字。
 */
function richText(s) {
  const out = [];
  const parts = String(s).split('**');
  for (let i = 0; i < parts.length; i++) {
    if (!parts[i]) continue;
    // 奇数段落在一对 ** 之间。最后一对没闭合的话（服务端写漏了）当普通文字。
    const bold = i % 2 === 1 && i < parts.length - 1;
    out.push(bold ? el('strong', { text: parts[i] }) : document.createTextNode(parts[i]));
  }
  return out;
}

const ROLE_LABEL = { admin: '管理员', viewer: '访客' };
const roleLabel = (r) => ROLE_LABEL[r] || r;

// 视频贴合方式的中文说明。用词与 appconfig 里 video.fit_mode 的 help 一致，
// 不认识的值（将来多一种模式，或者有人手工改过库）原样显示，不猜。
const FIT_LABEL = { fill: '裁切填满', fit: '完整留边' };

const nf1 = (x) => (Math.round(x * 10) / 10).toFixed(1);

/** epoch 毫秒 → 本地「2026-07-31 14:03」。 */
function fmtTime(ms) {
  if (!ms) return '—';
  const d = new Date(Number(ms));
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 相对时间。「最后活跃」看的是新旧程度，绝对时间放 title 里。 */
function ago(ms) {
  if (!ms) return '从未';
  const s = Math.max(0, (Date.now() - Number(ms)) / 1000);
  if (s < 90) return '刚刚';
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.round(s / 86400)} 天前`;
  return fmtTime(ms);
}

// ============================== 网络 ==============================

class ApiError extends Error {
  constructor(status, code, message, detail) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail || {};
  }
}

/**
 * 打一个接口。失败一律抛 ApiError，`message` 是**后端返回的那句中文**。
 *
 * 后端的错误体形状是固定的 `{error, message, ...额外字段}`（见 `app._error`），
 * 额外字段有真用处（比如 unknown_photo 会带 `unknownPhotoIds`），所以整个
 * 响应体都留在 `detail` 里，不只取那两个键。
 */
async function api(method, path, body) {
  const init = {
    method,
    // 唯一的凭证来源。见文件头：token 不进 localStorage。
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' },
    cache: 'no-store',
  };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let resp;
  try {
    resp = await fetch(API + path, init);
  } catch (e) {
    // fetch 只在网络层失败时 reject。这里区分出来是因为「服务停了」和「接口拒绝了」
    // 要给完全不同的提示，混在一起会让人去查权限而实际上容器没起来。
    throw new ApiError(0, 'network', '连不上服务端：检查网络，或者容器是不是没在跑。');
  }
  let doc = null;
  if (resp.status !== 204) {
    const text = await resp.text();
    if (text) { try { doc = JSON.parse(text); } catch (e) { /* 不是 JSON，下面按状态码处理 */ } }
  }
  if (!resp.ok) {
    const code = (doc && doc.error) || `http_${resp.status}`;
    const msg = (doc && doc.message) || `HTTP ${resp.status}`;
    // 会话过期/被踢（改口令、被停用、被删）在任何一个请求上都可能发生。
    // 统一在这里翻回登录界面，否则用户看到的是四个页签一起报 401。
    if (resp.status === 401 && state.me) sessionLost();
    throw new ApiError(resp.status, code, msg, doc || {});
  }
  return doc;
}

// ============================== 状态 ==============================

const state = {
  me: null,                 // /v1/auth/me 的结果，null = 未登录
  users: null,              // /v1/admin/users
  photos: null,             // /v1/photos 的 photos 数组
  photoDetail: {},          // photoId -> /v1/photo/<id>（贴合模式只能从这里拿）
  photosGen: 0,             // 防止上一轮的详情回来写进新表格
  tab: 'users',
  grants: null,             // {userId, all, ids:Set, baseAll, baseIds:Set, q}
  config: null,             // {fields, values, edits:{}}
  mapping: null,            // {photos, videos, unmapped}
  mapDir: 'photo',          // 映射页的方向：'photo' | 'video'
  // 批量页。plan = /admin/import/parse 的结果；run = 执行进度（null = 还没跑）。
  batch: { fileName: '', plan: null, run: null, busy: false },
  mounts: null,             // {mounts, envRoots}
  inbox: null,              // {dir, files, note} —— 传上来但还没入库的素材
};

// ============================== 提示条 ==============================

/**
 * 成功的提示 4 秒后自己消失；失败的**不自动消失**，等人点掉。
 * 后端那句中文 message 是排查的全部线索，自动消失等于把它吞掉。
 *
 * @param sticky 不自动消失，由调用方拿返回值 `.remove()` 撤掉。给「正在做…」这类
 *   进度提示用：一次入库要几十秒，而 4 秒就消失的「正在入库」会让人以为已经完了、
 *   然后去点别的。
 * @return 那个节点。
 */
function toast(kind, message, code, sticky) {
  const box = $('toasts');
  const body = el('div', { cls: 'body' }, [
    el('span', { text: message }),
    code ? el('span', { cls: 'code', text: code }) : null,
  ]);
  const x = el('button', { cls: 'x', type: 'button', 'aria-label': '关闭提示', text: '×' });
  const t = el('div', { cls: `toast ${kind}`, role: kind === 'bad' ? 'alert' : 'status' }, [body, x]);
  const kill = () => { if (t.parentNode) box.removeChild(t); };
  x.addEventListener('click', kill);
  box.appendChild(t);
  if (kind !== 'bad' && !sticky) setTimeout(kill, 4000);
  return t;
}

const ok = (m) => toast('ok', m);
/** 写操作失败：把后端的中文 message 与错误码一起显示，不换成「操作失败」。 */
const fail = (e, what) => toast('bad', what ? `${what}：${e.message}` : e.message, e.code);

// ============================== 对话框 ==============================

// 老浏览器（没有 showModal）走 [open] 降级路径，CSS 里有对应的定位规则。
function openDlg(d) { if (d.showModal) d.showModal(); else d.setAttribute('open', ''); }
function closeDlg(d) { if (d.close) d.close(); else d.removeAttribute('open'); }

/**
 * 危险操作的二次确认。`lines` 要写清**后果**而不是「确定吗」——
 * 删用户会连带删掉授权、降级会清口令、停用会踢掉登录，这些都不是能猜出来的。
 */
function confirm2(title, lead, lines, yesText) {
  const d = $('dlg-confirm');
  $('dlg-confirm-title').textContent = title;
  $('dlg-confirm-lead').textContent = lead;
  const ul = $('dlg-confirm-list');
  clear(ul);
  for (const line of lines) ul.appendChild(el('li', { text: line }));
  const yes = $('dlg-confirm-yes');
  yes.textContent = yesText || '确认';
  return new Promise((resolve) => {
    const done = (v) => {
      yes.removeEventListener('click', onYes);
      $('dlg-confirm-no').removeEventListener('click', onNo);
      d.removeEventListener('cancel', onNo);
      closeDlg(d);
      resolve(v);
    };
    const onYes = () => done(true);
    const onNo = () => done(false);
    yes.addEventListener('click', onYes);
    $('dlg-confirm-no').addEventListener('click', onNo);
    d.addEventListener('cancel', onNo);
    openDlg(d);
    $('dlg-confirm-no').focus();  // 默认焦点落在「取消」上，不是危险按钮
  });
}

function showFormErr(node, e) {
  clear(node);
  node.appendChild(el('span', { text: e.message }));
  if (e.code) node.appendChild(el('span', { cls: 'code', text: e.code }));
  node.hidden = false;
}
function hideFormErr(node) { node.hidden = true; clear(node); }

// ============================== 登录 / 会话 ==============================

/** 登录失败的文案按错误码分。三种失败的下一步动作完全不同。 */
const LOGIN_HINT = {
  bad_credentials: '口令不对。管理员必须输口令；这个名字如果是访客账号，请把口令留空。',
  unknown_user: '这个名字不在册。账号只能由管理员在管理台里创建，服务端不会自动建号。',
  account_disabled: '这个账号已被停用。要用它得先让另一个管理员启用。',
  missing_name: '请输入名字。',
};

async function boot() {
  // cookie 是 HttpOnly，脚本读不到，所以「有没有登录」只能问服务端。
  try {
    const me = await api('GET', '/auth/me');
    enter(me);
  } catch (e) {
    if (e.status === 401) showGate();          // 正常的未登录
    else { showGate(); if (e.code) fail(e, '检查登录状态失败'); }
  }
}

function showGate(msg) {
  state.me = null;
  $('app').hidden = true;
  $('denied').hidden = true;
  $('mustchg').hidden = true;
  $('gate').hidden = false;
  const box = $('login-err');
  if (msg) showFormErr(box, { message: msg, code: '' }); else hideFormErr(box);
  $('login-name').focus();
}

/** 会话在某个请求上失效了（过期、被停用、被改口令踢掉）。 */
function sessionLost() {
  if (!state.me) return;
  state.users = state.photos = state.config = state.grants = null;
  state.photoDetail = {};
  showGate('会话已失效（过期，或者账号被停用/改了口令）。请重新登录。');
}

function enter(me) {
  state.me = me;
  $('gate').hidden = true;
  // 还在用源码里那个公开默认口令 —— 除了改密什么都不给做。
  //
  // 放在 isAdmin 判断**之前**：`mustChangePassword` 服务端只对 admin 算，所以
  // 走到这里为真就一定是 admin，先拦下来比进去再拦少一次界面闪烁。
  if (me.mustChangePassword) {
    $('denied').hidden = true;
    $('app').hidden = true;
    $('mustchg').hidden = false;
    $('mustchg-pw').focus();
    return;
  }
  $('mustchg').hidden = true;
  if (!me.isAdmin) {
    // 访客登录成功了，但管理台每个接口都是 admin only。直说，别让他撞四次 403。
    $('app').hidden = true;
    $('denied').hidden = false;
    $('denied-who').textContent = `${me.name}（${roleLabel(me.role)}）`;
    return;
  }
  $('denied').hidden = true;
  $('app').hidden = false;
  $('me-name').textContent = me.name;
  $('me-role').textContent = roleLabel(me.role);
  // 按**地址栏**决定开哪个分区，这样 `/admin/config` 这种收藏/别人发来的链接
  // 在登录之后会直接落在对的地方。push=true 让 `/admin`（没有分区名）也被
  // 规范化成 `/admin/users`，否则第一次「后退」会跳到一个没有分区的地址。
  showTab(tabFromPath(location.pathname));
}

$('login-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = $('login-go');
  const name = $('login-name').value;
  const pw = $('login-pw').value;
  if (!name.trim()) { showFormErr($('login-err'), { message: LOGIN_HINT.missing_name, code: '' }); return; }
  btn.disabled = true;
  btn.textContent = '登录中…';
  try {
    const body = { name };
    // 口令空着就整个不发。访客的定义就是「只输名字」，发一个空串会让请求看起来
    // 像是「试了个空口令」。
    if (pw) body.password = pw;
    await api('POST', '/auth/login', body);
    $('login-pw').value = '';
    hideFormErr($('login-err'));
    // 登录响应里也有身份，但仍然再问一次 /auth/me：那份响应带的是 grantAll 之类
    // 的字段，isAdmin 只有 /auth/me 有，而后面每个页签都要用它。
    enter(await api('GET', '/auth/me'));
  } catch (e) {
    // 已知错误码给一句能照着做的话，同时把后端原文也留着（不认识的码就只显示原文）。
    const hint = LOGIN_HINT[e.code];
    showFormErr($('login-err'), hint ? { message: hint, code: `${e.code}｜${e.message}` } : e);
    ($('login-pw')).focus();
  } finally {
    btn.disabled = false;
    btn.textContent = '登录';
  }
});

$('mustchg-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const btn = $('mustchg-go');
  const pw = $('mustchg-pw').value;
  const pw2 = $('mustchg-pw2').value;
  const err = $('mustchg-err');
  if (pw !== pw2) {
    showFormErr(err, { message: '两次输入不一样。', code: '' });
    $('mustchg-pw2').focus();
    return;
  }
  // 只拦这一个值。长度/复杂度的门槛交给服务端的 `_check_password_for_role`——
  // 在两处各写一份规则，迟早分叉成"前端放行、后端拒绝"。
  if (pw === 'admin') {
    showFormErr(err, { message: '不能还是 admin —— 那就是要你改掉的那一个。', code: '' });
    $('mustchg-pw').focus();
    return;
  }
  btn.disabled = true;
  btn.textContent = '改中…';
  try {
    // 改自己的口令走的就是管理员改用户那条接口（自己也是一行 user）。
    // 服务端 `set_password` 会顺带踢掉这个人的**全部**会话，包括当前这一个——
    // 所以改完必须重新登录，下面直接把登录框弹回来，而不是假装还在线。
    await api('PATCH', `/admin/users/${state.me.userId}`, { password: pw });
    hideFormErr(err);
    $('mustchg-pw').value = $('mustchg-pw2').value = '';
    showGate('口令已改。请用新口令重新登录。');
  } catch (e) {
    showFormErr(err, e);
    $('mustchg-pw').focus();
  } finally {
    btn.disabled = false;
    btn.textContent = '改掉并进入';
  }
});

$('logout').addEventListener('click', async () => {
  try { await api('POST', '/auth/logout'); } catch (e) { /* 退出的失败不值得挡住用户 */ }
  state.users = state.photos = state.config = state.grants = null;
  state.photoDetail = {};
  state.me = null;
  showGate();
});

$('denied-out').addEventListener('click', async () => {
  try { await api('POST', '/auth/logout'); } catch (e) { /* 同上 */ }
  state.me = null;
  showGate();
});

// ============================== 页签 ==============================

/**
 * 分区清单。
 *
 * ⚠️ 必须与服务端 `app._WEBUI_TABS` **一致**：那张清单决定 `/admin/<名字>` 会不会
 * 返回首页。这里多一项 → 那个地址刷新时 404；那边多一项 → 地址打得开但这里不认，
 * 回落到默认分区（地址栏和内容对不上）。
 */
const TABS = ['users', 'grants', 'config', 'photos', 'batch'];

// 「刷新」按钮走这个（一定重新取数据）
const LOADERS = {
  users: loadUsers, grants: loadGrants, config: loadConfig, photos: loadPhotos,
  mounts: loadMounts,
};

/**
 * 切到某个页签时走这个。
 *
 * **「数据在不在」和「这一屏画过没有」是两件事**，必须分开判 —— 授权页会顺手把
 * 照片列表取回来，于是照片页看到 `state.photos !== null` 就以为自己不用干活了，
 * 结果是一个完全空白的页签（没有表格也没有空态）。所以：有数据就重画，没数据才去取。
 *
 * 重画而不是重取还有一个好处：正在改的授权勾选、正在改的配置，切走再切回来还在。
 */
const ENTER = {
  users: () => (state.users === null ? loadUsers() : renderUsers()),
  grants: () => (
    state.grants === null || state.users === null || state.photos === null
      ? loadGrants() : renderGrants()),
  config: () => {
    if (state.config === null) loadConfig(); else renderConfig();
    // 挂载点和配置字段是同一页上的两块，各自取各自的数据。
    if (state.mounts === null) loadMounts(); else renderMounts();
  },
  photos: () => (state.photos === null ? loadPhotos() : renderPhotos()),
  // 批量页没有要取的数据（模板与导出都是 <a download>，导入要人选文件）。
  // 进来时重画是为了把上一次的执行结果留在原处 —— 那份逐行结果是「哪几行没成」
  // 的唯一记录，切走再切回来就没了的话，人得重新导一遍才能知道。
  batch: () => renderBatch(),
};

/**
 * 每个分区一个自己的 URI：`/admin/users`、`/admin/photos`…
 *
 * 用真实路径 + `history.pushState`，不用 `#hash`。理由是这些地址是要**发给别人**和
 * **收藏**的（"配置在这儿：<地址>/admin/config"），而 hash 在很多聊天软件里会被吞掉
 * 或者变成不可点的一段。代价是服务端要认这些路径（`app._WEBUI_TABS`），否则刷新会
 * 404 —— 那一句已经加上了。
 */
const BASE = '/admin';

/** 从地址栏解出当前分区。认不出就给默认的 `users`。 */
function tabFromPath(pathname) {
  const rest = pathname.slice(BASE.length).replace(/^\/+|\/+$/g, '');
  return TABS.includes(rest) ? rest : 'users';
}

/**
 * @param push 是否往历史里压一条。点页签时压（于是「后退」回上一个分区），
 *   而**响应 popstate 与首次进入时不压** —— 压了会让后退键在两个分区之间来回弹，
 *   永远退不出这个页面。
 */
function showTab(name, push = true) {
  if (!TABS.includes(name)) name = 'users';
  state.tab = name;
  for (const t of TABS) {
    const btn = document.querySelector(`.tabs button[data-tab="${t}"]`);
    const panel = $(`p-${t}`);
    const on = t === name;
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
    panel.hidden = !on;
  }
  const want = `${BASE}/${name}`;
  if (push && location.pathname !== want) {
    history.pushState({ tab: name }, '', want);
  }
  // 数据只在第一次进这个页签时取，之后靠它自己的「刷新」按钮。管理台的数据几秒钟
  // 变一次是常态，但自动轮询会把「我正在改的这一屏」刷掉。
  ENTER[name]();
}

// 后退/前进。`state.me` 为空时（还在登录界面）什么都不做 —— 那时分区面板全是隐藏的，
// 切一个出来会让登录框和主界面叠在一起。
window.addEventListener('popstate', () => {
  if (!state.me) return;
  showTab(tabFromPath(location.pathname), false);
});

/**
 * 回到这个页面时自动刷新「照片」页。
 *
 * 解决的是一个具体的困惑：**在手机上加完照片，电脑上的管理台还是旧的。** 原来这一页只在
 * 第一次进入时取数据，之后要手点「刷新」—— 而人没有理由知道这一点，他会以为是手机那边
 * 没成功。
 *
 * ## 为什么只刷这一页，而且只在「回到页面」时刷
 *
 * 不做定时轮询：`config` 与 `grants` 页上有**正在编辑的状态**（勾了一半的授权、改了没保存
 * 的字段），定时刷会把它刷掉。而照片页没有编辑态 —— 它上面每个动作都是点一下立刻提交的。
 *
 * 「回到页面」（切回这个标签页 / 从别的应用切回浏览器）正好对应用户的心理时刻：他刚在手机
 * 上做完一件事，转回电脑来看结果。`visibilitychange` 抓得到这个时刻，而且**不会**在他一直
 * 盯着这一页时反复触发。
 */
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  if (!state.me || state.tab !== 'photos') return;
  // 不显示骨架屏：这是一次背景刷新，把已经画好的表格换成一排灰条会让人以为出问题了。
  loadPhotos();
});

document.querySelector('.tabs').addEventListener('click', (ev) => {
  const btn = ev.target.closest('button[data-tab]');
  if (btn) showTab(btn.dataset.tab);
});

// tablist 的键盘约定：左右方向键换页签。
document.querySelector('.tabs').addEventListener('keydown', (ev) => {
  if (ev.key !== 'ArrowRight' && ev.key !== 'ArrowLeft') return;
  const i = TABS.indexOf(state.tab);
  const next = TABS[(i + (ev.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length];
  ev.preventDefault();
  showTab(next);
  document.querySelector(`.tabs button[data-tab="${next}"]`).focus();
});

document.addEventListener('click', (ev) => {
  const r = ev.target.closest('[data-reload]');
  if (r) LOADERS[r.dataset.reload]();
  const c = ev.target.closest('dialog [data-close]');
  if (c) closeDlg(c.closest('dialog'));
});

// ---- 加载中 / 加载失败的统一显示 ----

function skeleton(container, rows) {
  clear(container);
  for (let i = 0; i < (rows || 4); i++) container.appendChild(el('div', { cls: 'skel' }));
}

function failbox(container, e, retry) {
  clear(container);
  const btn = el('button', { cls: 'btn sm', type: 'button', text: '重试' });
  btn.addEventListener('click', retry);
  container.appendChild(el('div', { cls: 'failbox' }, [
    el('div', { cls: 'msg' }, [
      el('span', { text: e.message }),
      el('span', { cls: 'code', text: `${e.code}｜HTTP ${e.status}` }),
    ]),
    btn,
  ]));
}

function emptyBox(container, line, hint) {
  clear(container);
  container.appendChild(el('div', { cls: 'empty' }, [
    el('p', { text: line }),
    hint ? el('p', { cls: 'hint', text: hint }) : null,
  ]));
}

/** 表格外壳。窄屏靠 td 的 data-label 翻成卡片，所以每个单元格都得带它。 */
function table(headers, rows) {
  const thead = el('thead', {}, [el('tr', {}, headers.map((h) =>
    el('th', h.sr ? { scope: 'col' } : { scope: 'col', text: h.t || h },
      h.sr ? [el('span', { cls: 'sr', text: h.sr })] : [])))]);
  return el('div', { cls: 'tblwrap' }, [el('table', { cls: 'tbl' }, [thead, el('tbody', {}, rows)])]);
}
function td(label, kids, cls) {
  // kids 可以是节点、字符串、数字或它们的数组。**不能**对非数组做 String() ——
  // 那会把一个 <span> 变成字面量 "[object HTMLSpanElement]"，而且是照样渲染出来的
  // 那种失败：表格结构完好，只有单元格内容变成了一句英文。
  const list = Array.isArray(kids) ? kids : [kids];
  return el('td', { 'data-label': label, cls: cls || null }, list.map((k) => {
    if (k === null || k === undefined) return null;
    return typeof k === 'object' ? k : String(k);
  }));
}

/** 缩略图。靠 cookie 鉴权（`<img>` 带不了 Authorization 头），见文件头注释。 */
function thumb(photoId, alt) {
  const img = el('img', {
    cls: 'th', src: `${API}/photo/${photoId}/thumb`, alt: alt || '',
    loading: 'lazy', decoding: 'async', width: 56, height: 42,
  });
  // 缩略图文件缺了会 404，浏览器默认画一个碎图标 —— 换成一句能读的话。
  img.addEventListener('error', () => {
    const ph = el('span', { cls: 'th bad', text: '缺图', title: '缩略图文件读不到' });
    if (img.parentNode) img.parentNode.replaceChild(ph, img);
  });
  return img;
}

// ============================== 用户 ==============================

async function loadUsers() {
  const box = $('users-body');
  skeleton(box);
  try {
    state.users = await api('GET', '/admin/users');
    renderUsers();
  } catch (e) {
    if (e.status !== 401) failbox(box, e, loadUsers);
  }
}

function renderUsers() {
  const box = $('users-body');
  const rows = state.users.map((u) => {
    const isSelf = state.me.userId !== null && u.id === state.me.userId;

    const nameCell = td('名字', [
      el('span', {}, [
        el('span', { text: u.name }),
        isSelf ? el('span', { cls: 'tag', text: '这是你', style: 'margin-left:6px' }) : null,
      ]),
      el('span', { cls: 'sub mono', text: u.id }),
    ], 'wide');

    const roleCell = td('角色', el('span', {
      cls: u.role === 'admin' ? 'tag ok' : 'tag',
      text: roleLabel(u.role),
    }));

    const stateCell = td('状态', el('span', { cls: u.disabled ? 'tag warn' : 'tag ok' }, [
      el('span', { cls: 'dot' }), u.disabled ? '已停用' : '启用中',
    ]));

    const scopeCell = td('可见范围', el('span', {
      cls: u.grantAll ? 'tag ok' : 'tag',
      text: u.grantAll ? '全部照片' : '逐张授权',
    }));

    // 逐张授权的真实张数，即使他 grantAll 也照实显示 —— 把「可看全部」关掉之后
    // 他剩下的就是这几张，界面必须在关掉之前就能看到。
    const cntCell = td('已授权', [
      el('span', { cls: 'mono', text: String(u.grantCount) }),
      el('span', { cls: 'muted', text: ' 张' }),
    ], 'num');
    const madeCell = td('创建', el('span', { text: fmtTime(u.createdAt), title: String(u.createdAt) }));
    const seenCell = td('最后活跃', el('span', {
      cls: u.lastSeenAt ? '' : 'muted',
      text: ago(u.lastSeenAt),
      title: u.lastSeenAt ? fmtTime(u.lastSeenAt) : '还没登录过',
    }));

    const edit = el('button', { cls: 'btn sm', type: 'button', text: '编辑' });
    edit.addEventListener('click', () => openUserDialog(u));
    const grant = el('button', { cls: 'btn sm', type: 'button', text: '授权' });
    grant.addEventListener('click', () => { showTab('grants'); selectGrantUser(u.id); });
    const del = el('button', {
      cls: 'btn sm danger', type: 'button', text: '删除',
      disabled: isSelf,
      // 后端也会拒（cannot_delete_self），但让人点了才报错是把一条已知规则藏起来。
      title: isSelf ? '不能删自己：删完就登不进管理台了' : '',
    });
    del.addEventListener('click', () => deleteUser(u));

    const actCell = td('', el('span', { cls: 'acts' }, [edit, grant, del]));

    return el('tr', {}, [nameCell, roleCell, stateCell, scopeCell, cntCell, madeCell, seenCell, actCell]);
  });

  clear(box);
  box.appendChild(table(
    ['名字', '角色', '状态', '可见范围', '已授权', '创建', '最后活跃', { sr: '操作' }],
    rows,
  ));
}

async function deleteUser(u) {
  const yes = await confirm2(
    `删除「${u.name}」？`,
    '这个操作不可撤销。',
    [
      `连带删掉他的 ${u.grantCount} 条逐张授权 —— 重建同名账号拿到的是新 id，勾过的照片不会回来。`,
      '他所有设备上的登录立刻失效。',
      '只是想暂时不让他登录的话，用「编辑」里的停用，那个可以还原。',
    ],
    '删除',
  );
  if (!yes) return;
  try {
    await api('DELETE', `/admin/users/${u.id}`);
    ok(`已删除「${u.name}」`);
    if (state.grants && state.grants.userId === u.id) state.grants = null;
    await loadUsers();
  } catch (e) {
    if (e.status !== 401) fail(e, '删除失败');
  }
}

// ---- 用户对话框（新建 / 编辑共用） ----

let dlgUser = null;   // {user|null}

function openUserDialog(user) {
  dlgUser = { user: user || null };
  const isNew = !user;
  const isSelf = !isNew && state.me.userId !== null && user.id === state.me.userId;

  $('dlg-user-title').textContent = isNew ? '新建用户' : `编辑「${user.name}」`;
  $('dlg-user-lead').textContent = isNew
    ? '新建的账号默认看不到任何照片，建完去「授权」页勾。'
    : '只提交你改动过的字段。';
  hideFormErr($('dlg-user-err'));
  $('u-name-err').textContent = '';
  $('u-pw-err').textContent = '';

  $('u-name').value = isNew ? '' : user.name;

  // 角色下拉。两个角色是鉴权模型的一部分（auth.ROLES），不是热配置字段，所以在
  // 这里写死；但如果库里这一行的角色不在这两个里（手工改过库、或者未来版本降级
  // 留下的），要把它作为一个选项补进去 —— 否则打开对话框再保存会**悄悄改掉**
  // 一个我们不认识的角色。
  const sel = $('u-role');
  clear(sel);
  for (const r of ['viewer', 'admin']) {
    sel.appendChild(el('option', { value: r, text: `${roleLabel(r)}（${r}）` }));
  }
  if (!isNew && !['viewer', 'admin'].includes(user.role)) {
    sel.appendChild(el('option', { value: user.role, text: `未知角色（${user.role}）` }));
  }
  sel.value = isNew ? 'viewer' : user.role;

  // 不能把自己降级/停用（后端也拒，理由是降完没有任何 HTTP 接口能升回来）。
  // 在 UI 上就禁掉，而不是等他点完保存再报错。
  const viewerOpt = sel.querySelector('option[value="viewer"]');
  viewerOpt.disabled = isSelf;
  $('u-disabled').checked = !isNew && !!user.disabled;
  $('u-disabled').disabled = isSelf;
  $('u-disabled-field').hidden = isNew;   // 新建时没有「停用」这回事
  const selfNote = $('dlg-user-self');
  selfNote.hidden = !isSelf;
  if (isSelf) {
    selfNote.textContent = '这是你自己的账号：不能降级、停用或删除自己 —— 降完就没人能把你'
      + '升回来了（升级需要管理员身份），只能进容器改库。让另一个管理员来做。';
  }

  syncRoleFields();
  openDlg($('dlg-user'));
  $('u-name').focus();
}

/** 角色变了就跟着换口令栏的可用状态与说明。这套规则是后端强制的，不是建议。 */
function syncRoleFields() {
  const isNew = !dlgUser.user;
  const role = $('u-role').value;
  const pw = $('u-pw');
  const help = $('u-pw-help');
  const wasAdmin = !isNew && dlgUser.user.role === 'admin';

  if (role === 'viewer') {
    // 访客不设口令：登录只输名字。带口令提交后端会 400（password_not_allowed），
    // 而它不静默丢掉是对的 —— 丢掉的话管理员会以为自己设上了口令。
    pw.value = '';
    pw.disabled = true;
    help.textContent = wasAdmin
      ? '访客不设口令。降级会顺手清掉他现有的口令，并踢掉全部登录。'
      : '访客不设口令：登录只输名字。要口令的话建成管理员。';
  } else {
    pw.disabled = false;
    if (isNew) help.textContent = '管理员必填。';
    else if (wasAdmin) help.textContent = '留空＝不改。填了会立刻踢掉他所有设备上的登录。';
    else help.textContent = '升成管理员必须同时设口令 —— 管理员登录一定要验口令，没有口令的管理员谁都登不进去。';
  }
  $('u-role-help').textContent = role === 'admin'
    ? '管理员能看全库、改配置、建号删号。'
    : '访客只能看被授权的照片。';
}
$('u-role').addEventListener('change', syncRoleFields);

$('dlg-user-form').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const isNew = !dlgUser.user;
  const u = dlgUser.user;
  const name = $('u-name').value;
  const role = $('u-role').value;
  const pw = $('u-pw').value;
  const disabled = $('u-disabled').checked;
  const btn = $('u-save');

  hideFormErr($('dlg-user-err'));
  $('u-name-err').textContent = '';
  $('u-pw-err').textContent = '';

  // 先在本地挡掉几条后端一定会拒的组合，省一次往返、也把错误显示在那一栏旁边。
  if (!name.trim()) { $('u-name-err').textContent = '名字不能为空。'; $('u-name').focus(); return; }
  if (role === 'admin' && !pw && (isNew || u.role !== 'admin')) {
    $('u-pw-err').textContent = isNew ? '管理员必须设口令。' : '升成管理员必须同时设口令。';
    $('u-pw').focus();
    return;
  }

  let payload;
  if (isNew) {
    payload = { name, role };
    if (pw) payload.password = pw;
  } else {
    // 只发改动过的字段。全量提交的话，「改个名字」会连带重发 role 与 password，
    // 而后端对 role 的每一次出现都会跑升降级的那套校验。
    payload = {};
    if (name.trim() !== u.name) payload.name = name;
    if (role !== u.role) payload.role = role;
    if (pw) payload.password = pw;
    if (disabled !== !!u.disabled) payload.disabled = disabled;
    if (Object.keys(payload).length === 0) { closeDlg($('dlg-user')); return; }

    // 有后果的改动要二次确认，且把后果一条条写出来。
    const lines = [];
    if (payload.role === 'viewer' && u.role === 'admin') {
      lines.push('降成访客：他的口令会被清掉，从此只输名字登录。');
      lines.push('降级后只有其它管理员能把他升回来（升级要同时设新口令）。');
    }
    if (payload.role === 'admin' && u.role !== 'admin') {
      lines.push('升成管理员：他从此能看全库、改配置、建号删号。');
    }
    if (payload.disabled === true) lines.push('停用：他所有设备上的登录立刻失效，再登会被拒。');
    if (payload.password) lines.push('改口令：他所有设备上的登录立刻失效，需要用新口令重登。');
    if (lines.length) {
      const yes = await confirm2(`确认修改「${u.name}」？`, '这次提交会做这些事：', lines, '确认修改');
      if (!yes) return;
    }
  }

  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    if (isNew) {
      const made = await api('POST', '/admin/users', payload);
      closeDlg($('dlg-user'));
      ok(`已建号「${made.name}」。他现在还看不到任何照片 —— 去「授权」页勾。`);
    } else {
      await api('PATCH', `/admin/users/${u.id}`, payload);
      closeDlg($('dlg-user'));
      ok(`已保存「${name}」`);
      // 改的可能是自己的名字，顶栏要跟着变。
      if (state.me.userId !== null && u.id === state.me.userId && payload.name) {
        state.me.name = payload.name.trim();
        $('me-name').textContent = state.me.name;
      }
    }
    if (state.grants) state.grants = null;   // grantAll / 用户名可能变了
    await loadUsers();
  } catch (e) {
    if (e.status === 401) return;
    // 报错显示在对话框**里面**：对话框在浏览器的 top layer 上，外面的提示条会被
    // 遮罩压住，用户只会看到一个「点了没反应」的保存按钮。
    showFormErr($('dlg-user-err'), e);
    if (e.code === 'name_taken' || e.code === 'bad_name') $('u-name').focus();
    if (e.code === 'password_required' || e.code === 'password_not_allowed') $('u-pw').focus();
  } finally {
    btn.disabled = false;
    btn.textContent = '保存';
  }
});

$('new-user').addEventListener('click', () => openUserDialog(null));

// ============================== 授权 ==============================

async function loadGrants() {
  const box = $('grants-body');
  skeleton(box, 3);
  try {
    // 授权页要用户列表也要照片列表。并行拉，两个都失败一次显示。
    const [users, photos] = await Promise.all([
      state.users ? Promise.resolve(state.users) : api('GET', '/admin/users'),
      api('GET', '/photos'),
    ]);
    state.users = users;
    state.photos = photos.photos;
    if (!state.grants) state.grants = { userId: null, all: false, ids: new Set(), baseAll: false, baseIds: new Set(), q: '' };
    renderGrants();
    // 之前选中的人还在的话，把他的授权重新拉一遍
    const keep = state.grants.userId;
    if (keep && state.users.some((u) => u.id === keep)) selectGrantUser(keep);
  } catch (e) {
    if (e.status !== 401) failbox(box, e, loadGrants);
  }
}

function renderGrants() {
  const box = $('grants-body');
  clear(box);

  const sel = el('select', { id: 'g-user', 'aria-label': '选择用户' });
  sel.appendChild(el('option', { value: '', text: '— 选一个用户 —' }));
  for (const u of state.users) {
    sel.appendChild(el('option', {
      value: u.id,
      text: `${u.name}（${roleLabel(u.role)}${u.disabled ? '，已停用' : ''}）`,
    }));
  }
  sel.value = state.grants.userId || '';
  sel.addEventListener('change', () => selectGrantUser(sel.value));

  box.appendChild(el('div', { cls: 'field', style: 'max-width:420px' }, [
    el('label', { for: 'g-user', text: '用户' }), sel,
  ]));
  box.appendChild(el('div', { id: 'g-panel' }));
  renderGrantPanel();
}

async function selectGrantUser(userId) {
  const g = state.grants || (state.grants = { userId: null, all: false, ids: new Set(), baseAll: false, baseIds: new Set(), q: '' });
  g.userId = userId || null;
  g.q = '';
  const sel = $('g-user');
  if (sel) sel.value = g.userId || '';
  if (!g.userId) { g.ids = new Set(); g.baseIds = new Set(); g.all = g.baseAll = false; renderGrantPanel(); return; }
  const panel = $('g-panel');
  if (panel) skeleton(panel, 3);
  try {
    const body = await api('GET', `/admin/users/${g.userId}/grants`);
    g.all = g.baseAll = !!body.grantAll;
    g.ids = new Set(body.photoIds);
    g.baseIds = new Set(body.photoIds);
    renderGrantPanel();
  } catch (e) {
    if (e.status !== 401) failbox($('g-panel'), e, () => selectGrantUser(userId));
  }
}

function grantsDirty() {
  const g = state.grants;
  if (g.all !== g.baseAll) return true;
  if (g.ids.size !== g.baseIds.size) return true;
  for (const id of g.ids) if (!g.baseIds.has(id)) return true;
  return false;
}

function renderGrantPanel() {
  const panel = $('g-panel');
  if (!panel) return;
  const g = state.grants;
  clear(panel);

  if (!g.userId) {
    emptyBox(panel, '先选一个用户。', '管理员本来就能看全库，逐张授权对他不起作用 —— 要发授权的通常是访客。');
    return;
  }
  const user = state.users.find((u) => u.id === g.userId);
  if (!user) { emptyBox(panel, '这个用户已经不在了。', '点上面的「刷新」重新取一次列表。'); return; }

  // 管理员的照片过滤条件在服务端是 None（不过滤），所以给他勾照片是白勾。
  // 仍然允许改（数据会照实存下来），只是要说清楚现在不起作用。
  if (user.role === 'admin') {
    panel.appendChild(el('div', { cls: 'banner' }, [
      el('div', { cls: 'body' }, [
        el('b', { text: '这是管理员账号。' }),
        el('div', { text: '管理员本来就能看全库，下面勾什么都不改变他能看到什么。'
          + '这里的勾选会照实存下来，等他被降成访客时才起作用。' }),
      ]),
    ]));
  }

  // 「可看全部」开关
  const swAll = el('input', { type: 'checkbox', id: 'g-all', checked: g.all });
  swAll.addEventListener('change', () => {
    g.all = swAll.checked;
    renderGrantPanel();
  });
  panel.appendChild(el('div', { cls: 'field' }, [
    el('label', { cls: 'sw' }, [swAll, el('span', { cls: 'track' }), el('span', { cls: 'txt', text: '可看全部照片' })]),
    el('span', {
      cls: 'help',
      text: g.all
        ? '开着：他能看到全库，包括以后新入库的。下面的勾选此刻不决定他看到什么，但会被存下来 —— 关掉的那一刻立刻生效。'
        : '关着：他只能看到下面勾中的这些照片。',
    }),
  ]));

  if (!state.photos.length) {
    panel.appendChild(el('div', { cls: 'empty' }, [
      el('p', { text: '库里还没有照片，没有东西可以授权。' }),
      el('p', { cls: 'hint', text: '照片由手机 App 或批量入库脚本写入。' }),
    ]));
    return;
  }

  // 工具条：搜索 + 全选/全不选（都只作用于当前筛选结果，并把张数写在按钮上）
  const q = el('input', { type: 'search', placeholder: '按标题或 id 筛选', value: g.q, 'aria-label': '筛选照片' });
  q.addEventListener('input', () => { g.q = q.value; repaintPick(); });
  const selAll = el('button', { cls: 'btn sm', type: 'button' });
  const selNone = el('button', { cls: 'btn sm', type: 'button' });
  const count = el('span', { cls: 'count' });
  selAll.addEventListener('click', () => { for (const p of filteredPhotos()) g.ids.add(p.photoId); repaintPick(); });
  selNone.addEventListener('click', () => { for (const p of filteredPhotos()) g.ids.delete(p.photoId); repaintPick(); });
  panel.appendChild(el('div', { cls: 'picktools' }, [
    el('span', { cls: 'grow' }, [q]), selAll, selNone, count,
  ]));

  const list = el('div', { cls: 'pick', id: 'g-list' });
  panel.appendChild(list);

  // 保存栏钉在底部：勾了三十张之后还要滚回顶上找保存按钮是最容易丢改动的地方。
  const stateTxt = el('span', { cls: 'state' });
  const revert = el('button', { cls: 'btn sm', type: 'button', text: '放弃改动' });
  const save = el('button', { cls: 'btn primary', type: 'button', text: '保存授权' });
  revert.addEventListener('click', () => selectGrantUser(g.userId));
  save.addEventListener('click', () => saveGrants(save));
  panel.appendChild(el('div', { cls: 'actionbar' }, [stateTxt, revert, save]));

  const refs = { list, count, selAll, selNone, stateTxt, revert, save };
  panel._refs = refs;
  repaintPick();
}

function filteredPhotos() {
  const q = (state.grants.q || '').trim().toLowerCase();
  if (!q) return state.photos;
  return state.photos.filter((p) =>
    String(p.title || '').toLowerCase().includes(q) || p.photoId.toLowerCase().includes(q));
}

/** 只重画勾选列表与底部状态，不动搜索框（否则每敲一个字焦点就丢了）。 */
function repaintPick() {
  const panel = $('g-panel');
  const refs = panel && panel._refs;
  if (!refs) return;
  const g = state.grants;
  const shown = filteredPhotos();

  clear(refs.list);
  refs.list.classList.toggle('shadowed', g.all);
  for (const p of shown) {
    const cb = el('input', { type: 'checkbox', checked: g.ids.has(p.photoId), 'aria-label': p.title || p.photoId });
    const row = el('label', { cls: `row${g.ids.has(p.photoId) ? ' on' : ''}` }, [
      cb,
      thumb(p.photoId, ''),
      el('span', { cls: 'meta' }, [
        el('span', { cls: 't', text: p.title || '(无标题)' }),
        el('span', { cls: 's' }, [
          el('span', { cls: 'mono', text: p.photoId.slice(0, 8) }),
          ` · ${nf1(p.printWidthM * 100)} cm`,
          p.hasVideo ? ' · 有视频' : ' · 无视频',
        ]),
      ]),
    ]);
    cb.addEventListener('change', () => {
      if (cb.checked) g.ids.add(p.photoId); else g.ids.delete(p.photoId);
      row.classList.toggle('on', cb.checked);
      paintGrantCount();
      paintGrantState();
    });
    refs.list.appendChild(row);
  }
  if (!shown.length) {
    refs.list.appendChild(el('div', { cls: 'row', style: 'cursor:default' }, [
      el('span', { cls: 'muted', text: '没有匹配的照片。' }),
    ]));
  }
  refs.selAll.textContent = `全选（${shown.length}）`;
  refs.selNone.textContent = '全不选';
  paintGrantCount();
  paintGrantState();
}

/** 勾了几张 / 共几张 / 筛出几张。单独一个函数是因为勾选框的 change 只需要更新
 *  这一行和底部状态，不需要重建整个列表（重建会把滚动位置弹回顶部）。 */
function paintGrantCount() {
  const panel = $('g-panel');
  const refs = panel && panel._refs;
  if (!refs) return;
  const g = state.grants;
  const shown = filteredPhotos().length;
  refs.count.textContent = `已勾 ${g.ids.size} / 共 ${state.photos.length} 张`
    + (shown !== state.photos.length ? `，当前筛出 ${shown} 张` : '');
}

function paintGrantState() {
  const panel = $('g-panel');
  const refs = panel && panel._refs;
  if (!refs) return;
  const g = state.grants;
  const dirty = grantsDirty();
  clear(refs.stateTxt);
  if (dirty) {
    refs.stateTxt.appendChild(el('strong', { text: '有未保存的改动' }));
    refs.stateTxt.appendChild(document.createTextNode(
      `：勾 ${g.ids.size} 张（原来 ${g.baseIds.size} 张）` + (g.all !== g.baseAll ? `，可看全部 ${g.all ? '开' : '关'}` : '')));
  } else {
    refs.stateTxt.appendChild(document.createTextNode('与服务端一致。'));
  }
  refs.save.disabled = !dirty;
  refs.revert.disabled = !dirty;
}

async function saveGrants(btn) {
  const g = state.grants;
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    // 整体替换：勾选框提交的语义就是「这就是全集」，后端也是这么实现的。
    //
    // 一次把全部 id 发上去（不分页）：请求体上限是 64KB，一个 id 连引号逗号约
    // 35 字节，也就是约 1700 张封顶。家庭规模离这个数很远；真撞上了会得到一个
    // 413 而不是静默截断，所以不是无声的失败。
    const body = await api('PUT', `/admin/users/${g.userId}/grants`, {
      grantAll: g.all,
      photoIds: Array.from(g.ids),
    });
    g.all = g.baseAll = !!body.grantAll;
    g.ids = new Set(body.photoIds);
    g.baseIds = new Set(body.photoIds);
    const user = state.users.find((u) => u.id === g.userId);
    ok(`已保存${user ? `「${user.name}」` : ''}的授权：${g.ids.size} 张${g.all ? '，并且可看全部' : ''}`);
    repaintPick();
    // grantCount 变了，用户列表要跟着更新。失败也不影响这次保存本身。
    try { state.users = await api('GET', '/admin/users'); if (state.tab === 'users') renderUsers(); } catch (e) { /* 下次刷新会对上 */ }
  } catch (e) {
    if (e.status === 401) return;
    fail(e, '保存授权失败');
    // 后端会点名哪几个 photoId 不存在（unknown_photo）—— 那说明本地这份照片列表
    // 过期了（有人在别处删了照片），提示去刷新，别让人一个一个试。
    if (e.code === 'unknown_photo') {
      const bad = e.detail.unknownPhotoIds || [];
      toast('bad', `这 ${bad.length} 张照片已经不在库里了，点「刷新」重新取一次照片列表：${bad.slice(0, 5).join(', ')}`, 'unknown_photo');
    }
  } finally {
    btn.textContent = '保存授权';
    paintGrantState();
  }
}

// ============================== 配置 ==============================

// 分组标题按 key 前缀分。前缀是从字段表里现算出来的，不认识的前缀原样当标题用
// —— 后端加一组新配置时这里不需要改，最坏也就是标题显示成英文前缀。
const GROUP_LABEL = {
  recog: '识别',
  ingest: '入库',
  video: '视频',
  session: '会话',
};

async function loadConfig() {
  const box = $('config-body');
  skeleton(box, 5);
  try {
    const body = await api('GET', '/admin/config');
    state.config = { fields: body.fields, values: body.values, edits: {} };
    renderConfig();
  } catch (e) {
    if (e.status !== 401) failbox(box, e, loadConfig);
  }
}

function cfgCurrent(f) {
  const c = state.config;
  return Object.prototype.hasOwnProperty.call(c.edits, f.key) ? c.edits[f.key] : c.values[f.key];
}

function renderConfig() {
  const box = $('config-body');
  clear(box);
  const c = state.config;

  box.appendChild(el('div', { id: 'cfg-restart' }));

  // 按前缀分组，组内保持服务端给的顺序（那个顺序是 FIELDS 的声明顺序，相关的
  // 字段在声明里挨着）。
  const groups = [];
  const byPrefix = new Map();
  for (const f of c.fields) {
    const prefix = f.key.includes('.') ? f.key.split('.')[0] : '';
    if (!byPrefix.has(prefix)) { byPrefix.set(prefix, []); groups.push(prefix); }
    byPrefix.get(prefix).push(f);
  }

  for (const prefix of groups) {
    const wrap = el('div', { cls: 'group' });
    wrap.appendChild(el('h3', {}, [
      el('span', { text: GROUP_LABEL[prefix] || prefix }),
      prefix && GROUP_LABEL[prefix] ? el('span', { cls: 'k', text: `  ${prefix}.*` }) : null,
    ]));
    for (const f of byPrefix.get(prefix)) wrap.appendChild(cfgRow(f));
    box.appendChild(wrap);
  }

  const save = el('button', { cls: 'btn primary', type: 'button', text: '保存改动', id: 'cfg-save' });
  const revert = el('button', { cls: 'btn', type: 'button', text: '放弃改动', id: 'cfg-revert' });
  const stateTxt = el('span', { cls: 'state', id: 'cfg-state' });
  save.addEventListener('click', () => saveConfig(save));
  revert.addEventListener('click', () => { c.edits = {}; renderConfig(); });
  box.appendChild(el('div', { cls: 'actionbar' }, [stateTxt, revert, save]));
  paintCfgState();
}

function cfgRow(f) {
  const cur = cfgCurrent(f);
  const row = el('div', { cls: 'cfg', 'data-key': f.key });

  const bits = [];
  if (f.min !== null && f.min !== undefined) bits.push(`范围 ${f.min} – ${f.max}`);
  bits.push(`默认 ${JSON.stringify(f.default)}`);

  const reset = el('button', { cls: 'reset', type: 'button', text: '恢复默认' });
  reset.addEventListener('click', () => { setCfg(f, f.default); renderConfig(); });

  const err = el('span', { cls: 'err', id: `cfg-err-${f.key}` });

  row.appendChild(el('div', { cls: 'lab' }, [
    el('div', { cls: 't' }, [
      el('span', { text: f.label }),
      f.needsRestart ? el('span', { cls: 'tag warn', text: '改完要重启' }) : null,
    ]),
    el('div', { cls: 'k', text: f.key }),
    el('div', { cls: 'h' }, richText(f.help)),
    el('div', { cls: 'meta', text: bits.join('｜') }),
  ]));

  const ctl = el('div', { cls: 'ctl' });
  ctl.appendChild(cfgControl(f, cur, err));
  ctl.appendChild(err);
  if (JSON.stringify(cur) !== JSON.stringify(f.default)) ctl.appendChild(reset);
  row.appendChild(ctl);

  if (Object.prototype.hasOwnProperty.call(state.config.edits, f.key)) row.classList.add('dirty');
  return row;
}

/** 按 kind 出控件。kind 是服务端字段表给的，这里不认识的当文本框处理。 */
function cfgControl(f, cur, err) {
  if (f.kind === 'bool') {
    const cb = el('input', { type: 'checkbox', checked: !!cur, id: `cfg-${f.key}` });
    cb.addEventListener('change', () => { setCfg(f, cb.checked); paintCfgRow(f); });
    return el('label', { cls: 'sw' }, [
      cb, el('span', { cls: 'track' }),
      el('span', { cls: 'txt', text: cur ? '开' : '关' }),
    ]);
  }
  if (f.kind === 'enum') {
    const sel = el('select', { id: `cfg-${f.key}`, 'aria-label': f.label });
    const choices = f.choices.slice();
    // 库里的值不在 choices 里（手工改过、或者版本降级留下的）也要能显示出来，
    // 否则下拉会默默把它改成第一项。
    if (cur !== null && cur !== undefined && !choices.includes(cur)) choices.push(String(cur));
    for (const ch of choices) sel.appendChild(el('option', { value: ch, text: ch }));
    sel.value = String(cur);
    sel.addEventListener('change', () => { setCfg(f, sel.value); paintCfgRow(f); });
    return sel;
  }
  if (f.kind === 'int' || f.kind === 'float') {
    const inp = el('input', {
      type: 'number', id: `cfg-${f.key}`, value: String(cur),
      inputmode: f.kind === 'int' ? 'numeric' : 'decimal',
      // step="any"：给小数字段写死 step 会让「1.5 在 min=1.0 上是否合法」交给浏览器
      // 的浮点判断，而它对 0.1 这种步长的判定并不总和人的直觉一致。范围自己验。
      step: f.kind === 'int' ? '1' : 'any',
      min: f.min === null || f.min === undefined ? null : String(f.min),
      max: f.max === null || f.max === undefined ? null : String(f.max),
      'aria-label': f.label,
    });
    inp.addEventListener('input', () => {
      const raw = inp.value.trim();
      err.textContent = '';
      inp.removeAttribute('aria-invalid');
      if (raw === '') { err.textContent = '不能为空。'; inp.setAttribute('aria-invalid', 'true'); return; }
      let n;
      if (f.kind === 'int') {
        if (!/^-?\d+$/.test(raw)) { err.textContent = '要整数。'; inp.setAttribute('aria-invalid', 'true'); return; }
        n = parseInt(raw, 10);
      } else {
        n = Number(raw);
        if (!Number.isFinite(n)) { err.textContent = '要一个有限的数字。'; inp.setAttribute('aria-invalid', 'true'); return; }
      }
      if (f.min !== null && f.min !== undefined && n < f.min) { err.textContent = `不能小于 ${f.min}。`; inp.setAttribute('aria-invalid', 'true'); return; }
      if (f.max !== null && f.max !== undefined && n > f.max) { err.textContent = `不能大于 ${f.max}。`; inp.setAttribute('aria-invalid', 'true'); return; }
      setCfg(f, n);
      paintCfgRow(f, true);
    });
    return inp;
  }
  const inp = el('input', { type: 'text', id: `cfg-${f.key}`, value: cur === null || cur === undefined ? '' : String(cur), 'aria-label': f.label });
  inp.addEventListener('input', () => { setCfg(f, inp.value); paintCfgRow(f, true); });
  return inp;
}

function setCfg(f, value) {
  const c = state.config;
  if (JSON.stringify(value) === JSON.stringify(c.values[f.key])) delete c.edits[f.key];
  else c.edits[f.key] = value;
}

/** 改了一个字段之后只更新那一行的标记与底部状态，不整块重绘（重绘会打断输入）。 */
function paintCfgRow(f, keepFocus) {
  const row = document.querySelector(`.cfg[data-key="${f.key}"]`);
  if (row) {
    row.classList.toggle('dirty', Object.prototype.hasOwnProperty.call(state.config.edits, f.key));
    const txt = row.querySelector('.sw .txt');
    if (txt) txt.textContent = cfgCurrent(f) ? '开' : '关';
  }
  paintCfgState();
  if (!keepFocus && row) {
    // 开关/下拉这类离散控件改完可以把「恢复默认」的显隐重算一遍
    const has = JSON.stringify(cfgCurrent(f)) !== JSON.stringify(f.default);
    const ctl = row.querySelector('.ctl');
    const btn = ctl && ctl.querySelector('.reset');
    if (has && !btn) {
      const b = el('button', { cls: 'reset', type: 'button', text: '恢复默认' });
      b.addEventListener('click', () => { setCfg(f, f.default); renderConfig(); });
      ctl.appendChild(b);
    } else if (!has && btn) ctl.removeChild(btn);
  }
}

function paintCfgState() {
  const c = state.config;
  const n = Object.keys(c.edits).length;
  const st = $('cfg-state');
  const save = $('cfg-save');
  const revert = $('cfg-revert');
  if (!st) return;
  clear(st);
  const bad = document.querySelectorAll('#config-body .err:not(:empty)').length;
  if (bad) {
    st.appendChild(el('strong', { text: `有 ${bad} 个字段填得不对` }));
    st.appendChild(document.createTextNode('，改对了才能保存。'));
  } else if (n) {
    st.appendChild(el('strong', { text: `${n} 个字段有改动` }));
    const restart = Object.keys(c.edits).filter((k) => (c.fields.find((f) => f.key === k) || {}).needsRestart);
    st.appendChild(document.createTextNode(restart.length ? `，其中 ${restart.length} 个要重启容器才生效。` : '。'));
  } else {
    st.appendChild(document.createTextNode('与服务端一致。'));
  }
  if (save) save.disabled = !n || !!bad;
  if (revert) revert.disabled = !n;
}

async function saveConfig(btn) {
  const c = state.config;
  const edits = c.edits;
  if (!Object.keys(edits).length) return;
  btn.disabled = true;
  btn.textContent = '保存中…';
  try {
    // 只发改动过的 key。后端本来也只写「确实变了」的那些，并且只把其中需要重启的
    // 回给我们 —— 每次点保存都喊一句「需要重启」，喊几次之后真需要时没人会当真。
    const body = await api('PATCH', '/admin/config', edits);
    for (const k in edits) c.values[k] = edits[k];
    c.edits = {};
    renderConfig();
    ok(`已保存 ${Object.keys(edits).length} 项配置`);
    if (body.needsRestart && body.needsRestart.length) showRestartBanner(body.needsRestart);
  } catch (e) {
    if (e.status === 401) return;
    fail(e, '保存配置失败');
    // 后端的报错里带着出问题的 key（`recog.top_k 的值不合法（…）`），把它标到那一行
    // 旁边去。整批是拒绝的，所以其它字段的值还停在改动状态，可以改完再存。
    for (const f of c.fields) {
      if (e.message.includes(f.key)) {
        const box = $(`cfg-err-${f.key}`);
        if (box) box.textContent = e.message;
        const row = document.querySelector(`.cfg[data-key="${f.key}"]`);
        if (row) row.scrollIntoView({ block: 'center' });
      }
    }
    paintCfgState();
  } finally {
    btn.textContent = '保存改动';
  }
}

function showRestartBanner(keys) {
  const box = $('cfg-restart');
  if (!box) return;
  clear(box);
  const items = keys.map((k) => {
    const f = state.config.fields.find((x) => x.key === k);
    return el('li', {}, [
      el('span', { text: f ? f.label : k }),
      el('span', { text: ' ' }),
      el('code', { text: k }),
    ]);
  });
  const x = el('button', { cls: 'x', type: 'button', 'aria-label': '知道了', text: '×' });
  x.addEventListener('click', () => clear(box));
  box.appendChild(el('div', { cls: 'banner', role: 'alert' }, [
    el('div', { cls: 'body' }, [
      el('b', { text: '这些改动要重启容器才生效：' }),
      el('ul', {}, items),
      el('div', { cls: 'muted', style: 'font-size:.8125rem;margin-top:6px',
        text: '值已经写进库里了，但进程在启动时就把旧值用掉了（词汇树、描述子库布局、'
          + '会话时长都是那时定下来的）。重启前，界面显示的和实际生效的不是一回事。' }),
    ]),
    x,
  ]));
}

// ============================== 照片 ==============================

/**
 * 「照片」页。
 *
 * 这一页原来是两个页签：「照片」（只读的库清单）和「映射」（照片↔视频）。合并了，
 * 因为它们本来就是**同一份数据的两种看法** —— 分开的结果是同一行信息在两处各显示
 * 一半：这边有打印宽度和贴合模式，那边有配的视频和被授权人数，而人想问的是
 * 「这张照片现在到底怎么样」。
 *
 * 数据全部来自 `/v1/admin/mapping`（一次拿齐，含 fitMode / refStale / createdAt）
 * 加 `/v1/admin/videos`（视频侧的反查）。不再拉 `/v1/photos` —— 那个接口留给授权页，
 * 它需要的是「这个人能看到哪些」的口径。
 */
async function loadPhotos() {
  const box = $('photos-body');
  skeleton(box, 5);
  try {
    // 两个方向一起取。分开按需取的话，切一次方向要等一次网络，而这两份数据加起来
    // 就是同一批照片，来回切是这一页最常做的动作。
    const [byPhoto, byVideo, inbox] = await Promise.all([
      api('GET', '/admin/mapping'),
      api('GET', '/admin/videos'),
      // 传上来但还没入库的素材。一起取，因为「我传上去的东西在哪」和「库里有什么」
      // 是同一个问题的两半 —— 分成两次点击去看，人就找不到自己刚传的那个文件。
      api('GET', '/admin/inbox').catch(() => null),
    ]);
    state.mapping = {
      photos: byPhoto.photos,
      videos: byVideo.videos,
      unmapped: byVideo.unmapped,
    };
    state.inbox = inbox;
    renderPhotos();
  } catch (e) {
    if (e.status !== 401) failbox(box, e, loadPhotos);
  }
}

document.querySelector('#p-photos .segbar').addEventListener('click', (ev) => {
  const b = ev.target.closest('button[data-mapdir]');
  if (!b) return;
  state.mapDir = b.dataset.mapdir;
  renderPhotos();
});

function renderPhotos() {
  const box = $('photos-body');
  clear(box);
  for (const b of document.querySelectorAll('#p-photos [data-mapdir]')) {
    b.setAttribute('aria-pressed', b.dataset.mapdir === state.mapDir ? 'true' : 'false');
  }
  if (!state.mapping) return;
  if (state.mapDir === 'video') renderByVideo(box);
  else renderByPhoto(box);
}

const EMPTY_HINT = '照片由手机 App（「素材」页传一组）或管理台的「批量」页导入。';

function renderByPhoto(box) {
  const photos = state.mapping.photos;
  if (!photos.length) {
    emptyBox(box, '库里还没有照片。', EMPTY_HINT);
    return;
  }

  const rows = photos.map((p) => {
    const change = el('button', {
      cls: 'btn sm', type: 'button', text: p.videoPath ? '换视频' : '配视频',
    });
    change.addEventListener('click', () => attachVideoTo([p], change));
    const detach = el('button', { cls: 'btn sm danger', type: 'button', text: '解除' });
    detach.addEventListener('click', () => detachVideoFrom(p, detach));
    const del = el('button', { cls: 'btn sm danger', type: 'button', text: '删除' });
    del.addEventListener('click', () => deletePhoto(p, del));

    return el('tr', {}, [
      td('', thumb(p.photoId, p.title || p.photoId)),
      td('照片', [
        el('span', { text: p.title || '(无标题)' }),
        el('span', { cls: 'sub mono', text: p.refPath || p.photoId }),
        p.refMissing ? el('span', { cls: 'tag bad', text: '参考图读不到' }) : null,
        p.refStale ? el('span', { cls: 'tag bad', text: '参考图已变' }) : null,
      ], 'wide'),
      td('视频', p.videoPath
        ? [
            el('span', { cls: 'mono sub', text: p.videoPath }),
            p.videoMissing ? el('span', { cls: 'tag bad', text: '文件读不到' }) : null,
          ]
        : el('span', { cls: 'tag warn', text: '没配视频' }), 'wide'),
      // 打印物理宽度：接口给的是米，照片是用厘米量的。0 = 未知（交给 ARCore 自己量）。
      td('打印宽度', el('span', {
        cls: 'mono',
        text: p.printWidthM > 0 ? `${nf1(p.printWidthM * 100)} cm` : '未知',
        title: p.printWidthM > 0 ? `${p.printWidthM} m` : '交给 ARCore 自己量',
      }), 'num'),
      td('贴合模式', el('span', {
        cls: 'mono',
        text: FIT_LABEL[p.fitMode] ? `${p.fitMode} · ${FIT_LABEL[p.fitMode]}` : String(p.fitMode),
      })),
      td('被授权', el('span', { cls: 'mono', text: String(p.grantCount) }), 'num'),
      td('入库时间', el('span', { text: fmtTime(p.createdAt) })),
      td('操作', el('span', { cls: 'acts' }, [change, p.videoPath ? detach : null, del])),
    ]);
  });

  const withVideo = photos.filter((p) => p.videoPath).length;
  box.appendChild(el('p', { cls: 'note', text:
    `共 ${photos.length} 张，${withVideo} 张配了视频，${photos.length - withVideo} 张还没配。` }));
  box.appendChild(table(
    [{ sr: '缩略图' }, '照片', '视频', '打印宽度', '贴合模式', '被授权', '入库时间', '操作'],
    rows,
  ));
  renderInbox(box);
}

/**
 * 从库里删掉一张照片。
 *
 * ## 为什么这个按钮必须存在
 *
 * 库里进了两张同一内容的照片时，比值检验会把**两张都**判成 ambiguous —— 两张都永久
 * 扫不出来。这是真机上发生过的事：941 帧记录只命中 44 帧，内点数 160~229（门槛 40）。
 * 入库闸门现在会拦住新的，但已经进去的那一对只能靠删掉一张解开，而在有这个按钮之前
 * 唯一的出路是重建整个库。
 *
 * 确认框里如实写清「删了什么、没删什么」：参考图和视频文件都留在 NAS 上（同一段视频
 * 可能配给了别的照片），所以这不是"删文件"，是"从识别库里拿掉"。
 */
async function deletePhoto(p, btn) {
  const name = p.title || p.refPath || p.photoId;
  const yes = await confirm2(
    `从库里删掉「${name}」？`,
    '这个操作不可撤销。',
    [
      '它不再参与识别，被授权过的人也看不到了。',
      'NAS 上的参考图和视频文件**都留着** —— 同一段视频可能配给了别的照片。',
      '之后想再入库的话，同一张图可以重新传（内容哈希会认出它，不用再传一遍字节）。',
      '识别历史里那几条不动：删照片不该让「上周它扫得出来」这件事消失。',
    ],
    '删除',
  );
  if (!yes) return;
  btn.disabled = true;
  try {
    await api('DELETE', `/photo/${p.photoId}`);
    ok(`已删除「${name}」`);
    state.mapping = null;
    await loadPhotos();
  } catch (e) {
    btn.disabled = false;
    if (e.status !== 401) fail(e, '删除失败');
  }
}

/**
 * 「传上来但还没入库」那一段。
 *
 * 为什么要有：手机传上来的文件先落到落地目录，然后才入库。中间任何一步断了（入库超时、
 * 近重复被拒、或者人挑完视频就退出了），那个文件就躺在那儿，而**管理台上
 * 任何一处都看不到它**。用户看到的是「我传上去了，但哪儿都找不到」。
 *
 * 画在照片列表**下面**而不是单开一页：它回答的是同一个问题（「我的素材在哪」）的另一半。
 */
function renderInbox(box) {
  const inbox = state.inbox;
  if (!inbox || !inbox.files || !inbox.files.length) return;

  const rows = inbox.files.map((f) => {
    const use = el('button', { cls: 'btn sm primary', type: 'button',
      text: f.kind === 'image' ? '入库' : '配给照片…' });
    use.addEventListener('click', () => useInboxFile(f, use));
    return el('tr', {}, [
      td('文件', [
        el('span', { text: f.name }),
        el('span', { cls: 'sub mono', text: f.path }),
      ], 'wide'),
      td('类型', el('span', { cls: 'tag', text: f.kind === 'image' ? '图片' : '视频' })),
      td('大小', el('span', { cls: 'mono', text: bytesText(f.bytes) }), 'num'),
      td('上传时间', el('span', { text: fmtTime(f.mtime) })),
      td('操作', el('span', { cls: 'acts' }, [use])),
    ]);
  });

  box.appendChild(el('section', { cls: 'card warnish', style: 'margin-top:22px' }, [
    el('div', { cls: 'head' }, [el('div', {}, [
      el('h3', { text: `传上来但还没入库的 ${inbox.files.length} 个文件` }),
      el('p', { cls: 'note', text:
        '这些文件在服务端的落地目录里，但还没有被任何照片用起来 —— ' +
        '多半是上一次入库中途断了。图片可以直接入库；视频要挑一张照片配给它。' }),
    ])]),
    table(['文件', '类型', '大小', '上传时间', '操作'], rows),
  ]));
}

/** 把落地目录里的一个文件用起来：图片走入库，视频挑一张照片配上去。 */
async function useInboxFile(f, btn) {
  btn.disabled = true;
  try {
    if (f.kind === 'image') {
      const video = await pickFromMounts('video', '给它挑一段视频（可以取消跳过）');
      const body = { refPath: f.path };
      if (video) body.videoPath = video;
      const t = toast('ok', '正在入库…（要跑特征提取，几十秒）', null, true);
      try {
        const created = await api('POST', '/photo', body);
        ok('入库成功。');
      } catch (e) {
        await explainIngestFailure(e, f.path, video);
      } finally {
        t.remove();
      }
    } else {
      // 视频：一段视频可以配给多张照片，所以这里挑的是「配给哪几张」。
      const candidates = (state.mapping && state.mapping.photos) || [];
      if (!candidates.length) {
        toast('bad', '库里还没有照片，先入库一张再来配视频。');
        return;
      }
      const chosen = await pickPhotos(candidates, '把这段视频配给哪些照片', f.path);
      if (!chosen || !chosen.length) return;
      let done = 0;
      const failed = [];
      for (const p of chosen) {
        try {
          await api('POST', `/photo/${p.photoId}/video`, { videoPath: f.path });
          done++;
        } catch (e) {
          failed.push(`${p.title || p.photoId}：${e.message}`);
        }
      }
      if (done) ok(`已把这段视频配给 ${done} 张照片。`);
      for (const line of failed) toast('bad', line);
    }
  } finally {
    btn.disabled = false;
  }
  state.mapping = null;
  state.photos = null;
  await loadPhotos();
}

// ============================== NAS 文件选择器 ==============================

/**
 * 弹出目录浏览器，挑一个文件，返回它的容器内绝对路径（取消返回 null）。
 *
 * `wantKind` 是 `'video'` / `'image'`，不合类型的文件仍然**列出来但不可点** ——
 * 直接过滤掉的话，人会以为自己那段视频不在这个目录里，然后去别处找。
 *
 * 走 `GET /v1/fs/list`，也就是 App 的目录浏览器在用的同一个接口。子目录里的条目
 * 只有 `name`（没有 `path`），所以路径要自己拼；只有白名单根那一层的条目自带
 * `path`，因为根的真实位置不能从名字推出来。
 */
function pickPath(wantKind, title) {
  const d = $('dlg-pick');
  $('dlg-pick-title').textContent = title || '挑一个文件';
  const list = $('dlg-pick-list');
  const crumbs = $('dlg-pick-crumbs');

  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      $('dlg-pick').removeEventListener('cancel', onCancel);
      closeDlg(d);
      resolve(v);
    };
    const onCancel = () => finish(null);
    d.addEventListener('cancel', onCancel);
    // 「取消」按钮走的是全局 [data-close] 监听（它只 closeDlg，不 resolve），
    // 所以这里还要盯一次 close 事件，否则点取消之后这个 Promise 永远悬着，
    // 而调用方 `await` 在那儿不动 —— 界面看起来是「按钮没反应」。
    d.addEventListener('close', onCancel, { once: true });

    const go = async (path) => {
      clear(list);
      list.appendChild(el('div', { cls: 'skel' }));
      let body;
      try {
        body = await api('GET', path ? `/fs/list?path=${encodeURIComponent(path)}` : '/fs/list');
      } catch (e) {
        clear(list);
        list.appendChild(el('div', { cls: 'failbox' }, [
          el('div', { cls: 'msg' }, [el('span', { text: e.message })]),
        ]));
        return;
      }
      // 面包屑。根那一层不显示（没有可回退的上级）。
      clear(crumbs);
      if (body.path) {
        const root = el('button', { cls: 'crumb', type: 'button', text: '全部目录' });
        root.addEventListener('click', () => go(null));
        crumbs.appendChild(root);
        crumbs.appendChild(el('span', { cls: 'sep', text: '/' }));
        crumbs.appendChild(el('span', { cls: 'crumb cur mono', text: body.path }));
      }

      clear(list);
      if (!body.entries.length) {
        list.appendChild(el('div', { cls: 'empty' }, [el('p', { text: '这个目录是空的。' })]));
        return;
      }
      for (const e of body.entries) {
        // 根条目自带 path；子目录里的条目只有 name，得自己拼。
        const full = e.path || `${body.path}/${e.name}`;
        if (e.isDir) {
          // 类名用 fpick 而不是 pick：`.pick` 在授权页已经是个**容器**类
          // （`.pick .row` 是它的子项），复用它会让每一行都套上一层边框。
          const b = el('button', { cls: 'fpick dir', type: 'button' }, [
            el('span', { cls: 'ic', text: '📁' }),
            el('span', { cls: 'nm', text: e.name }),
          ]);
          b.addEventListener('click', () => go(full));
          list.appendChild(b);
          continue;
        }
        const usable = e.kind === wantKind;
        const b = el('button', {
          cls: `fpick file${usable ? '' : ' off'}`,
          type: 'button',
          disabled: !usable,
          title: usable ? full : `这里要挑${wantKind === 'video' ? '视频' : '图片'}，这个是${KIND_TEXT[e.kind] || '认不出的类型'}`,
        }, [
          el('span', { cls: 'ic', text: e.kind === 'video' ? '🎬' : (e.kind === 'image' ? '🖼' : '·') }),
          el('span', { cls: 'nm', text: e.name }),
          el('span', { cls: 'sz mono', text: bytesText(e.bytes) }),
        ]);
        if (usable) b.addEventListener('click', () => finish(full));
        list.appendChild(b);
      }
    };

    go(null);
    openDlg(d);
  });
}

const KIND_TEXT = { image: '图片', video: '视频' };

function bytesText(n) {
  if (typeof n !== 'number') return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${nf1(n / 1024)} KB`;
  if (n < 1024 * 1024 * 1024) return `${nf1(n / 1048576)} MB`;
  return `${nf1(n / 1073741824)} GB`;
}

// ============================== 照片：按视频看 ==============================
function renderByVideo(box) {
  const { videos, unmapped } = state.mapping;
  if (!videos.length && !unmapped.length) {
    emptyBox(box, '库里还没有照片。', EMPTY_HINT);
    return;
  }

  box.appendChild(el('p', { cls: 'note', text: `${videos.length} 段视频在用。改一段视频之前，这里能看到它影响哪几张照片。` }));

  for (const v of videos) {
    const assign = el('button', { cls: 'btn sm', type: 'button', text: '再配给别的照片…' });
    assign.addEventListener('click', () => assignVideoToPhotos(v, assign));
    const head = el('div', { cls: 'head' }, [
      el('div', {}, [
        el('h3', { cls: 'mono', text: v.path || '(路径读不到)' }),
        el('p', { cls: 'note' }, [
          el('span', { text: `${v.photos.length} 张照片在用` }),
          v.missing ? el('span', { cls: 'tag bad', text: '文件读不到' }) : null,
          typeof v.durationMs === 'number' ? el('span', { cls: 'muted', text: ` · ${nf1(v.durationMs / 1000)} 秒` }) : null,
          typeof v.bytes === 'number' ? el('span', { cls: 'muted', text: ` · ${bytesText(v.bytes)}` }) : null,
        ]),
      ]),
      el('div', { cls: 'right' }, [assign]),
    ]);
    const chips = el('div', { cls: 'chips' }, v.photos.map((p) => {
      const x = el('button', { cls: 'x', type: 'button', 'aria-label': `不再给「${p.title || p.photoId}」用这段视频`, text: '×' });
      x.addEventListener('click', () => detachVideoFrom(p, x));
      return el('span', { cls: 'chip' }, [
        thumb(p.photoId, ''),
        el('span', { cls: 'nm', text: p.title || '(无标题)' }),
        p.transcoded ? el('span', { cls: 'tag', text: '转码过', title: '这张照片播的是转码产物，不是源文件' }) : null,
        x,
      ]);
    }));
    box.appendChild(el('section', { cls: 'card' }, [head, chips]));
  }

  if (unmapped.length) {
    const chips = el('div', { cls: 'chips' }, unmapped.map((p) => {
      const b = el('button', { cls: 'chip act', type: 'button', title: '给这张配视频' }, [
        thumb(p.photoId, ''),
        el('span', { cls: 'nm', text: p.title || '(无标题)' }),
        el('span', { cls: 'plus', text: '＋' }),
      ]);
      b.addEventListener('click', () => attachVideoTo([p], b));
      return b;
    }));
    box.appendChild(el('section', { cls: 'card warnish' }, [
      el('div', { cls: 'head' }, [el('div', {}, [
        el('h3', { text: `还没配视频的 ${unmapped.length} 张` }),
        el('p', { cls: 'note', text: '扫到这些照片时不会播任何东西。点一张来配。' }),
      ])]),
      chips,
    ]));
  }
}

/** 给一批照片配同一段视频。`photos` 里每项要有 photoId 与 title。 */
async function attachVideoTo(photos, btn) {
  const path = await pickPath('video', photos.length === 1
    ? `给「${photos[0].title || photos[0].photoId}」挑视频`
    : `给 ${photos.length} 张照片挑同一段视频`);
  if (!path) return;
  btn.disabled = true;
  let done = 0;
  const failed = [];
  try {
    for (const p of photos) {
      try {
        await api('POST', `/photo/${p.photoId}/video`, { videoPath: path });
        done++;
      } catch (e) {
        // 一张失败不该中断整批 —— 剩下的照片和这一张没有关系。逐张记下来，
        // 最后一次把「哪几张没成、为什么」说完。
        failed.push(`${p.title || p.photoId}：${e.message}`);
      }
    }
  } finally {
    btn.disabled = false;
  }
  if (done) ok(`已给 ${done} 张照片配上视频。`);
  for (const line of failed) toast('bad', line);
  await loadPhotos();
  // 照片页的「有视频/无视频」标记跟着变了，缓存作废。
  state.photos = null;
  state.photoDetail = {};
}

/** 把这段视频再配给别的照片（视频侧的操作方向）。 */
async function assignVideoToPhotos(video, btn) {
  if (!video.path) {
    toast('bad', '这段视频的路径读不到，没法再配给别的照片。');
    return;
  }
  const already = new Set(video.photos.map((p) => p.photoId));
  const candidates = state.mapping.photos.filter((p) => !already.has(p.photoId));
  if (!candidates.length) {
    toast('ok', '库里每一张照片都已经在用这段视频了。');
    return;
  }
  const chosen = await pickPhotos(candidates, `把这段视频配给哪些照片`, video.path);
  if (!chosen || !chosen.length) return;
  btn.disabled = true;
  let done = 0;
  const failed = [];
  try {
    for (const p of chosen) {
      try {
        await api('POST', `/photo/${p.photoId}/video`, { videoPath: video.path });
        done++;
      } catch (e) {
        failed.push(`${p.title || p.photoId}：${e.message}`);
      }
    }
  } finally {
    btn.disabled = false;
  }
  if (done) ok(`已把这段视频配给 ${done} 张照片。`);
  for (const line of failed) toast('bad', line);
  await loadPhotos();
  state.photos = null;
  state.photoDetail = {};
}

/**
 * 多选照片。复用文件选择器那个 dialog 的外壳，列的是照片而不是文件。
 *
 * 会把**已经配过视频的**照片单独标出来：给它配新视频等于替换，而替换是不可撤销的
 * （旧的关联没了）—— 这一点在勾选之前就得看得见。
 */
function pickPhotos(photos, title, videoPath) {
  const d = $('dlg-pick');
  $('dlg-pick-title').textContent = title;
  const crumbs = $('dlg-pick-crumbs');
  const list = $('dlg-pick-list');
  clear(crumbs);
  crumbs.appendChild(el('span', { cls: 'crumb cur mono', text: videoPath }));

  return new Promise((resolve) => {
    let done = false;
    const chosen = new Set();
    const finish = (v) => {
      if (done) return;
      done = true;
      d.removeEventListener('cancel', onCancel);
      closeDlg(d);
      resolve(v);
    };
    const onCancel = () => finish(null);
    d.addEventListener('cancel', onCancel);
    d.addEventListener('close', onCancel, { once: true });

    clear(list);
    const goBtn = el('button', { cls: 'btn primary', type: 'button', disabled: true, text: '配给 0 张' });
    const paint = () => {
      goBtn.disabled = chosen.size === 0;
      goBtn.textContent = `配给 ${chosen.size} 张`;
    };
    for (const p of photos) {
      const cb = el('input', { type: 'checkbox' });
      cb.addEventListener('change', () => {
        if (cb.checked) chosen.add(p.photoId); else chosen.delete(p.photoId);
        paint();
      });
      list.appendChild(el('label', { cls: 'fpick sel' }, [
        cb,
        thumb(p.photoId, ''),
        el('span', { cls: 'nm', text: p.title || '(无标题)' }),
        p.videoPath
          ? el('span', { cls: 'tag warn', text: '会替换现有视频', title: p.videoPath })
          : null,
      ]));
    }
    goBtn.addEventListener('click', () => finish(photos.filter((p) => chosen.has(p.photoId))));
    list.appendChild(el('div', { cls: 'foot' }, [goBtn]));
    paint();
    openDlg(d);
  });
}

async function detachVideoFrom(photo, btn) {
  const yes = await confirm2(
    '解除这张照片的视频关联',
    `「${photo.title || photo.photoId}」以后扫到时不会播任何东西。`,
    [
      '视频文件本身不删，磁盘上的源文件与转码产物都留着。',
      '别的照片如果也在用这段视频，它们不受影响。',
      '想再配回来随时可以，不用重新入库。',
    ],
    '解除关联',
  );
  if (!yes) return;
  btn.disabled = true;
  try {
    await api('DELETE', `/photo/${photo.photoId}/video`);
    ok('已解除关联。');
    state.photos = null;
    state.photoDetail = {};
    await loadPhotos();
  } catch (e) {
    fail(e, '解除关联失败');
    btn.disabled = false;
  }
}

// ============================== 批量 ==============================

// 导出链接。用 <a download> 而不是 fetch + Blob：这三个接口都带
// `Content-Disposition: attachment`，浏览器会直接下载而不导航走，而 fetch 那条路
// 要自己造 Blob URL、自己起文件名、还要记得 revokeObjectURL。
for (const [id, href] of [
  ['dl-template-xlsx', '/v1/admin/export/template?format=xlsx'],
  ['dl-template-csv', '/v1/admin/export/template?format=csv'],
  ['dl-users-xlsx', '/v1/admin/export/users?format=xlsx'],
  ['dl-users-csv', '/v1/admin/export/users?format=csv'],
  ['dl-mapping-xlsx', '/v1/admin/export/mapping?format=xlsx'],
]) {
  $(id).href = href;
}

$('batch-file').addEventListener('change', async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  // 清掉 input 的值，这样同一个文件改完再选一次仍然会触发 change。
  // 不清的话人会以为「改了表格但预览没变」是缓存问题。
  ev.target.value = '';
  state.batch.fileName = file.name;
  state.batch.plan = null;
  state.batch.run = null;
  $('batch-filename').textContent = file.name;
  renderBatch();
  await parseBatchFile(file);
});

async function parseBatchFile(file) {
  const box = $('batch-body');
  skeleton(box, 3);
  let resp;
  try {
    // 原始字节直接当请求体。服务端读的就是这个（不是 multipart）—— 只有一个文件，
    // multipart 的多字段能力在这里没有用处。
    resp = await fetch(`${API}/admin/import/parse`, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Accept': 'application/json' },
      body: file,
    });
  } catch (e) {
    failbox(box, new ApiError(0, 'network', '连不上服务端：检查网络，或者容器是不是没在跑。'), () => {});
    return;
  }
  let doc = null;
  const text = await resp.text();
  if (text) { try { doc = JSON.parse(text); } catch (e) { /* 下面按状态码处理 */ } }
  if (!resp.ok) {
    const e = new ApiError(resp.status, (doc && doc.error) || `http_${resp.status}`,
      (doc && doc.message) || `HTTP ${resp.status}`, doc || {});
    if (resp.status === 401 && state.me) { sessionLost(); return; }
    failbox(box, e, () => {});
    return;
  }
  state.batch.plan = doc;
  renderBatch();
}

function renderBatch() {
  const box = $('batch-body');
  clear(box);
  $('batch-filename').textContent = state.batch.fileName || '';
  const plan = state.batch.plan;
  if (!plan) {
    if (!state.batch.run) return;   // 还没导过任何东西：三张卡片已经说明了流程
  }

  if (plan) {
    if (plan.errors.length) {
      // 整表级错误：所有行都不该执行。把服务端那几句原文列出来，它们已经指出了
      // 最可能的原因（表头被删）与当前读到的表头。
      const ul = el('ul', {}, plan.errors.map((m) => el('li', {}, [richText(m)])));
      box.appendChild(el('div', { cls: 'failbox col' }, [
        el('div', { cls: 'msg' }, [el('strong', { text: '这份表没法用：' })]),
        ul,
      ]));
      return;
    }
    box.appendChild(batchSummary(plan));
    box.appendChild(batchTable(plan));
  }
}

function batchSummary(plan) {
  const s = plan.summary;
  const run = state.batch.run;
  const goBtn = el('button', {
    cls: 'btn primary', type: 'button',
    disabled: state.batch.busy || s.okRows === 0 || (run && run.finished),
    text: run && run.finished ? '已执行完' : `执行这 ${s.okRows} 行`,
  });
  goBtn.addEventListener('click', () => runBatch());

  const stats = el('div', { cls: 'stats' }, [
    stat('可执行', s.okRows, s.okRows ? 'ok' : 'warn'),
    s.badRows ? stat('有错(跳过)', s.badRows, 'bad') : null,
    s.warnRows ? stat('有提醒', s.warnRows, 'warn') : null,
    stat('建/复用用户', s.users),
    stat('入库照片', s.photos),
    stat('配视频', s.videos),
    stat('授权', s.grants),
  ]);

  const note = s.badRows
    ? `有 ${s.badRows} 行有错，执行时会**跳过**它们，其余照做。改完那几行再导一次就行（重复的用户和照片不会建两遍）。`
    : '照片入库要跑 arcoreimg 与特征提取，视频可能要转码，所以一行可能要几秒到几十秒。**别关这个页面**：逐行执行是这个浏览器在做的，关掉就停在半路（改完再导一遍是安全的）。';

  return el('section', { cls: 'card' }, [
    el('div', { cls: 'head' }, [
      el('div', {}, [
        el('h3', { text: `预览：${s.rows} 行` }),
        el('p', { cls: 'note' }, [richText(note)]),
      ]),
      el('div', { cls: 'right' }, [goBtn]),
    ]),
    stats,
    run ? batchProgress(run) : null,
  ]);
}

function stat(label, value, kind) {
  return el('div', { cls: `stat${kind ? ' ' + kind : ''}` }, [
    el('span', { cls: 'v mono', text: String(value) }),
    el('span', { cls: 'k', text: label }),
  ]);
}

function batchProgress(run) {
  const pct = run.total ? Math.round((run.done / run.total) * 100) : 0;
  return el('div', { cls: 'progress' }, [
    el('div', { cls: 'bar' }, [el('div', { cls: 'fill', style: `width:${pct}%` })]),
    el('p', { cls: 'note', text: run.finished
      ? `执行完毕：${run.okCount} 行成功，${run.failCount} 行失败。`
      : `正在执行第 ${run.done + 1} / ${run.total} 行…` }),
  ]);
}

const ACTION_TEXT = { user: '建用户', photo: '入库', video: '配视频', grant: '授权' };

function batchTable(plan) {
  const run = state.batch.run;
  const rows = plan.rows.map((r) => {
    const result = run && run.results[r.line];
    return el('tr', { cls: r.errors.length ? 'bad' : (r.warnings.length ? 'warn' : '') }, [
      td('行', el('span', { cls: 'mono', text: String(r.line) }), 'num'),
      td('用户', [
        el('span', { text: r.userName || '—' }),
        r.userName ? el('span', { cls: 'sub', text: r.role === 'admin' ? '管理员' : '访客' }) : null,
        r.hasPassword ? el('span', { cls: 'tag', text: '带口令' }) : null,
      ]),
      td('照片', el('span', { cls: 'mono sub', text: r.photoPath || '—' }), 'wide'),
      td('视频', el('span', { cls: 'mono sub', text: r.videoPath || '—' }), 'wide'),
      td('要做', el('span', { cls: 'acts' },
        r.actions.length
          ? r.actions.map((a) => el('span', { cls: 'tag', text: ACTION_TEXT[a] || a }))
          : [el('span', { cls: 'muted', text: '无' })])),
      td('检查', batchMessages(r), 'wide'),
      td('结果', batchResult(result)),
    ]);
  });
  return table(['行', '用户', '照片', '视频', '要做', '检查', '结果'], rows);
}

function batchMessages(r) {
  if (!r.errors.length && !r.warnings.length) return el('span', { cls: 'tag ok', text: '没问题' });
  const out = [];
  for (const m of r.errors) out.push(el('p', { cls: 'msgline bad' }, [richText(m)]));
  for (const m of r.warnings) out.push(el('p', { cls: 'msgline warn' }, [richText(m)]));
  return out;
}

function batchResult(result) {
  if (!result) return el('span', { cls: 'muted', text: '—' });
  if (result.state === 'running') return el('span', { cls: 'tag', text: '进行中…' });
  if (result.state === 'skipped') return el('span', { cls: 'tag warn', text: '已跳过' });
  if (result.state === 'ok') {
    return el('span', { cls: 'acts' }, [
      el('span', { cls: 'tag ok', text: '成功' }),
      ...(result.notes || []).map((n) => el('span', { cls: 'sub', text: n })),
    ]);
  }
  return el('span', {}, [
    el('span', { cls: 'tag bad', text: '失败' }),
    el('p', { cls: 'msgline bad', text: result.error || '' }),
  ]);
}

/**
 * 逐行执行计划。
 *
 * 顺序是「建用户 → 入库照片 → 配视频 → 记下要授权的」，最后统一提交授权。
 * 授权放到最后是因为 `PUT /admin/users/<id>/grants` 是**整体替换**：同一个人在表里
 * 有三行时，每行提交一次会让后两次把前面的覆盖掉，最后只剩最后一张。
 *
 * 已存在的东西一律当成功：用户名重复回 409 `name_taken`，照片重复回 409
 * `already_ingested`（**并带上 photoId**，所以还能接着给它配视频和授权）。这让
 * 「改完出错的几行再导一遍」成为安全操作，而那正是这个界面最常见的用法。
 */
async function runBatch() {
  const plan = state.batch.plan;
  if (!plan || state.batch.busy) return;
  const todo = plan.rows.filter((r) => r.errors.length === 0 && r.actions.length > 0);
  if (!todo.length) return;

  state.batch.busy = true;
  const run = {
    total: todo.length, done: 0, okCount: 0, failCount: 0,
    finished: false, results: {},
  };
  state.batch.run = run;
  for (const r of plan.rows) {
    if (r.errors.length) run.results[r.line] = { state: 'skipped' };
  }
  renderBatch();

  // nameKey → userId。nameKey 由服务端算（见 app.py 的 `_user_json`），不在这里
  // 自己实现一遍规范化。
  const userIds = new Map();
  for (const u of await safeUsers()) userIds.set(u.nameKey, u.id);
  // refPath → photoId，跨行复用：同一张照片授权给三个人是三行，只该入库一次。
  const photoIds = new Map();
  // userId → Set(photoId)，最后统一 PUT。
  const wantGrants = new Map();

  for (const r of todo) {
    run.results[r.line] = { state: 'running' };
    renderBatch();
    const notes = [];
    try {
      let userId = null;
      if (r.userName) {
        userId = userIds.get(r.nameKey) || null;
        if (userId) {
          notes.push('用户已存在，复用');
        } else {
          try {
            const created = await api('POST', '/admin/users', {
              name: r.userName, role: r.role, password: r.password || null,
            });
            userId = created.id;
            userIds.set(created.nameKey, created.id);
          } catch (e) {
            if (e.code !== 'name_taken') throw e;
            // 竞态或者规范化没对上：重取一次用户表再找。
            for (const u of await safeUsers()) userIds.set(u.nameKey, u.id);
            userId = userIds.get(r.nameKey) || null;
            if (!userId) throw e;
            notes.push('用户已存在，复用');
          }
        }
      }

      let photoId = null;
      if (r.photoPath) {
        photoId = photoIds.get(r.photoPath) || null;
        if (!photoId) {
          const doc = { refPath: r.photoPath };
          if (r.videoPath) doc.videoPath = r.videoPath;
          if (r.title) doc.title = r.title;
          if (r.printWidthMm !== null && r.printWidthMm !== undefined) {
            doc.printWidthMm = r.printWidthMm;
          }
          try {
            const created = await api('POST', '/photo', doc);
            photoId = created.photoId;
            if (r.videoPath) notes.push('已配视频');
          } catch (e) {
            if (e.code !== 'already_ingested' || !e.detail.photoId) throw e;
            photoId = e.detail.photoId;
            notes.push('照片已入库，复用');
            // 入库那一步没走，所以视频得单独配。这里**不判断**它是不是已经配着
            // 同一段视频了：`POST /photo/<id>/video` 对同一个路径是幂等的，而
            // 多问一次 `GET /photo/<id>` 只是为了省一次同样结果的调用。
            if (r.videoPath) {
              await api('POST', `/photo/${photoId}/video`, { videoPath: r.videoPath });
              notes.push('已配视频');
            }
          }
          photoIds.set(r.photoPath, photoId);
        } else if (r.videoPath) {
          // 同一张照片在前面的行已经处理过（含视频）。计划构建时已经拦掉了
          // 「同一张照片配两段不同视频」，所以这里一定是同一段，不用再配。
          notes.push('照片本轮已处理');
        }
      }

      if (userId && photoId) {
        if (!wantGrants.has(userId)) wantGrants.set(userId, new Set());
        wantGrants.get(userId).add(photoId);
      }

      run.results[r.line] = { state: 'ok', notes };
      run.okCount++;
    } catch (e) {
      run.results[r.line] = { state: 'fail', error: `${e.message}（${e.code}）` };
      run.failCount++;
    }
    run.done++;
    renderBatch();
  }

  // 授权统一提交。**与库里现有的取并集**，不是替换 —— PUT 是整体替换，直接提交
  // 表里这几张会把这个人原有的其它授权全部抹掉，而那些授权跟这份表毫无关系。
  for (const [userId, ids] of wantGrants) {
    try {
      const cur = await api('GET', `/admin/users/${userId}/grants`);
      const merged = new Set(cur.photoIds || []);
      for (const id of ids) merged.add(id);
      if (merged.size !== (cur.photoIds || []).length) {
        await api('PUT', `/admin/users/${userId}/grants`, { photoIds: [...merged] });
      }
    } catch (e) {
      toast('bad', `授权提交失败（用户 ${userId}）：${e.message}`, e.code);
      run.failCount++;
    }
  }

  run.finished = true;
  state.batch.busy = false;
  renderBatch();
  if (run.failCount === 0) ok(`${run.okCount} 行全部执行成功。`);
  else toast('bad', `${run.okCount} 行成功，${run.failCount} 行失败。失败原因在表格的「结果」一列。`);

  // 别的页签的缓存全作废了。
  state.users = null;
  state.photos = null;
  state.photoDetail = {};
  state.grants = null;
  state.mapping = null;
}

/** 取用户表，失败时返回空数组（执行流程不该因为这一步整批中止）。 */
async function safeUsers() {
  try {
    return await api('GET', '/admin/users');
  } catch (e) {
    return [];
  }
}

// ============================== 素材挂载点 ==============================

async function loadMounts() {
  const box = $('mounts-body');
  skeleton(box, 2);
  try {
    state.mounts = await api('GET', '/admin/mounts');
    renderMounts();
  } catch (e) {
    if (e.status !== 401) failbox(box, e, loadMounts);
  }
}

const MOUNT_KIND_LABEL = { local: '本机路径', webdav: 'WebDAV' };

function renderMounts() {
  const box = $('mounts-body');
  clear(box);
  if (!state.mounts) return;
  const { mounts, envRoots } = state.mounts;

  // 环境变量给的那几个根先列出来（只读）。不列的话会出现「我明明在 compose 里配了，
  // 怎么这儿是空的」这种困惑 —— 那几个根确实在，只是不是在这里配的。
  if (envRoots && envRoots.length) {
    box.appendChild(el('p', { cls: 'note' }, [
      el('span', { text: '来自 PHOTOAR_ROOTS（只读，要改就改 compose 再重启）：' }),
      ...envRoots.map((r) => el('span', { cls: 'tag', text: `${r.name} · ${r.path}` })),
    ]));
  }

  if (!mounts.length) {
    box.appendChild(el('div', { cls: 'empty' }, [
      el('p', { text: '还没有额外的挂载点。' }),
      el('p', { cls: 'hint', text:
        '上面那几个根已经能用了。加挂载点是为了把别的位置也纳进来 —— ' +
        '比如另一个已经 mount 好的网络盘，或者一台 WebDAV 服务器。' }),
    ]));
    return;
  }

  const rows = mounts.map((m) => {
    const edit = el('button', { cls: 'btn sm', type: 'button', text: '编辑' });
    edit.addEventListener('click', () => openMountDialog(m));
    const del = el('button', { cls: 'btn sm danger', type: 'button', text: '删除' });
    del.addEventListener('click', () => deleteMount(m));
    return el('tr', {}, [
      td('名字', [
        el('span', { text: m.name }),
        m.enabled ? null : el('span', { cls: 'tag warn', text: '已停用' }),
      ]),
      td('类型', el('span', { cls: 'tag', text: MOUNT_KIND_LABEL[m.kind] || m.kind })),
      td('位置', el('span', { cls: 'mono sub', text: m.location }), 'wide'),
      td('凭证', m.username
        ? el('span', {}, [
            el('span', { text: m.username }),
            m.hasPassword ? el('span', { cls: 'tag', text: '有口令' }) : null,
          ])
        : el('span', { cls: 'muted', text: '无' })),
      td('操作', el('span', { cls: 'acts' }, [edit, del])),
    ]);
  });
  box.appendChild(table(['名字', '类型', '位置', '凭证', '操作'], rows));
}

$('new-mount').addEventListener('click', () => openMountDialog(null));

/** 新增 / 编辑共用。`m` 为 null 是新增。 */
function openMountDialog(m) {
  const d = $('dlg-mount');
  const errBox = $('dlg-mount-err');
  hideFormErr(errBox);
  $('dlg-mount-title').textContent = m ? '编辑挂载点' : '新增挂载点';
  $('dlg-mount-lead').textContent = m
    ? '改完之后立刻生效，不用重启服务。'
    : '本机路径填的是**服务端**上的绝对路径；WebDAV 填完整地址。';
  $('m-name').value = m ? m.name : '';
  $('m-kind').value = m ? m.kind : 'local';
  $('m-location').value = m ? m.location : '';
  $('m-user').value = m && m.username ? m.username : '';
  $('m-pw').value = '';
  $('m-enabled').checked = m ? m.enabled : true;
  $('m-pw-help').textContent = m && m.hasPassword
    ? '已设置口令。留空 = 不改；要清空就填一个空格再删掉（提交空字符串）。'
    : '留空 = 不设口令。服务端从不把口令发回来。';
  syncMountKind();

  const form = $('dlg-mount-form');
  const save = $('m-save');
  const onSubmit = async (ev) => {
    ev.preventDefault();
    const kind = $('m-kind').value;
    const body = {
      name: $('m-name').value.trim(),
      kind,
      location: $('m-location').value.trim(),
      // WebDAV 才有凭证。切成 local 时把它们清掉 —— 留着的话库里会有一组
      // 用不上的凭证，而下次切回 webdav 时会以为它是新填的。
      username: kind === 'webdav' ? $('m-user').value.trim() : '',
      enabled: $('m-enabled').checked,
    };
    const pw = $('m-pw').value;
    // 编辑时空口令 = 不改（不传这个字段）。新增时空口令 = 不设。
    if (pw || !m) body.password = pw;
    save.disabled = true;
    try {
      if (m) await api('PATCH', `/admin/mounts/${m.id}`, body);
      else await api('POST', '/admin/mounts', body);
      done();
      ok(m ? '挂载点已更新。' : '挂载点已添加。');
      await loadMounts();
    } catch (e) {
      showFormErr(errBox, e);
    } finally {
      save.disabled = false;
    }
  };
  const done = () => {
    form.removeEventListener('submit', onSubmit);
    closeDlg(d);
  };
  form.addEventListener('submit', onSubmit);
  d.addEventListener('close', () => form.removeEventListener('submit', onSubmit), { once: true });
  openDlg(d);
  $('m-name').focus();
}

$('m-kind').addEventListener('change', syncMountKind);

/** 类型决定了「位置」那一栏的含义与提示，以及要不要显示凭证。 */
function syncMountKind() {
  const kind = $('m-kind').value;
  const dav = kind === 'webdav';
  $('m-creds').hidden = !dav;
  $('m-location-label').textContent = dav ? '地址' : '绝对路径';
  $('m-location').placeholder = dav
    ? 'https://nas.example.com/remote.php/dav/files/me/'
    : '/media/photos';
  clear($('m-kind-help'));
  $('m-kind-help').appendChild(richText(dav
    ? '从一台 WebDAV 服务器取。添加时会先**下载到服务端**再入库。'
    : '服务端文件系统上的目录。已经用 mount/cifs 挂好的网络盘就是这种 —— ' +
      '在容器里它就是一个普通路径。**不拷贝**，直接读。'));
  clear($('m-location-help'));
  $('m-location-help').appendChild(richText(dav
    ? '群晖是 `https://<host>:5006/`，Nextcloud 是 ' +
      '`https://<host>/remote.php/dav/files/<用户名>/`。'
    : '填的是**容器内**的路径。宿主机上的目录要先在 compose 里挂进容器，' +
      '否则这里会报「路径不存在」。'));
}

async function deleteMount(m) {
  const yes = await confirm2(
    `删除挂载点「${m.name}」`,
    '以后不再从这个位置找素材。',
    [
      '已经入库的照片**不受影响** —— 它们记的是文件的绝对路径，不是挂载点。',
      '但如果那些文件只由这个挂载点覆盖着，它们会在下一次一致性检查里被标成「读不到」。',
      '磁盘上的文件一个都不删。',
    ],
    '删除',
  );
  if (!yes) return;
  try {
    await api('DELETE', `/admin/mounts/${m.id}`);
    ok('挂载点已删除。');
    await loadMounts();
  } catch (e) {
    fail(e, '删除失败');
  }
}

// ============================== 从挂载点添加照片 ==============================

/**
 * 「添加照片」：挑一张图 → 挑一段视频（可跳过）→ 入库并建立映射。
 *
 * 和 App 的「素材」页是同一件事的两条路，区别只在**素材从哪来**：那边是手机相册，
 * 这边是服务端能看到的位置（挂载点 / PHOTOAR_ROOTS）。两边都落到同一个
 * `POST /v1/photo {refPath, videoPath}`。
 */
/**
 * 打印尺寸预设。与 App 侧 `PrintSize.kt` 是同一组数，改一边要改另一边。
 *
 * 为什么要问：物理尺寸未知时 ARCore 必须靠视差自己量出照片有多大，那需要用户扫的时候
 * 挪动手机才收敛 —— 而那正是「认出来了，但没在画面里找到」最常见的成因。填了它，
 * ARCore 一认出图案就能直接给位姿。
 *
 * 填错一点不影响贴合精度：四边形的大小取的是 ARCore 自己量的 `extentX`，申报宽度只是
 * 给检测用的提示。
 */
const PRINT_SIZES = [
  { label: '不知道', mm: 0 },
  { label: '6寸 横', mm: 152 },
  { label: '6寸 竖', mm: 102 },
  { label: '5寸 横', mm: 127 },
  { label: '5寸 竖', mm: 89 },
  { label: 'A4 横', mm: 297 },
  { label: 'A4 竖', mm: 210 },
];

/** 问一下打印尺寸。返回毫米数（0 = 不知道），取消返回 null。 */
function askPrintSize() {
  const d = $('dlg-pick');
  $('dlg-pick-title').textContent = '这张照片印出来有多宽？';
  const crumbs = $('dlg-pick-crumbs');
  const list = $('dlg-pick-list');
  clear(crumbs);
  crumbs.appendChild(el('span', { cls: 'crumb cur', text:
    '填了的话，扫的时候一认出来就能贴上；不填也行，但那时要用户轻轻晃动手机才量得出来。' }));
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      d.removeEventListener('cancel', onCancel);
      closeDlg(d);
      resolve(v);
    };
    const onCancel = () => finish(null);
    d.addEventListener('cancel', onCancel);
    d.addEventListener('close', onCancel, { once: true });
    clear(list);
    for (const s of PRINT_SIZES) {
      const b = el('button', { cls: 'fpick', type: 'button' }, [
        el('span', { cls: 'ic', text: s.mm ? '📐' : '?' }),
        el('span', { cls: 'nm', text: s.label }),
        el('span', { cls: 'sz mono', text: s.mm ? `${s.mm} mm` : 'ARCore 自己量' }),
      ]);
      b.addEventListener('click', () => finish(s.mm));
      list.appendChild(b);
    }
    openDlg(d);
  });
}

$('add-photo').addEventListener('click', async () => {
  const ref = await pickFromMounts('image', '挑一张照片');
  if (!ref) return;
  const video = await pickFromMounts('video', '挑配它的那段视频（可以取消跳过）');
  const widthMm = await askPrintSize();
  if (widthMm === null) return;   // 取消 = 整个动作取消
  const body = { refPath: ref };
  if (widthMm > 0) body.printWidthMm = widthMm;
  if (video) body.videoPath = video;
  const t = toast('ok', video ? '正在入库并配视频…（要跑特征提取，几十秒）' : '正在入库…（要跑特征提取，几十秒）', null, true);
  try {
    const created = await api('POST', '/photo', body);
    ok('入库成功。' +
       (video ? '' : ' 还没配视频 —— 在下面那一行点「配视频」补上，否则扫到它不会播。'));
  } catch (e) {
    await explainIngestFailure(e, ref, video);
  } finally {
    if (t) t.remove();
  }
  state.mapping = null;
  state.photos = null;
  await loadPhotos();
});

/**
 * 入库失败时，把「这个文件在库里已经是什么」查出来说清楚。
 *
 * 这是重复上传**不该是死胡同**的那一半：`already_ingested` 只告诉你「已经入库了」，
 * 而人真正要知道的是「那张照片现在配的是哪段视频」，好接着决定要不要换。
 */
async function explainIngestFailure(e, refPath, videoPath) {
  if (e.code !== 'already_ingested') {
    fail(e, '入库失败');
    return;
  }
  let info = null;
  try {
    info = await api('GET', `/admin/lookup?path=${encodeURIComponent(refPath)}`);
  } catch (_) { /* 查不到就只报原错误 */ }
  const existing = info && info.photo;
  if (!existing) {
    fail(e, '入库失败');
    return;
  }
  const lines = [
    `这张照片已经入库了：「${existing.title || '(无标题)'}」`,
    existing.videoPath
      ? `它现在配的视频是 ${existing.videoPath}`
      : '它现在**没有**配视频',
  ];
  if (!videoPath) {
    toast('bad', lines.join('；') + '。没什么要改的。');
    return;
  }
  // 一张照片只能配一个视频，所以这里是「换」而不是「加」。
  const yes = await confirm2(
    '这张照片已经在库里了',
    lines.join('；') + '。',
    [
      `要把它的视频换成刚挑的这段吗：${videoPath}`,
      '一张照片只能配一段视频，所以这是**替换**，原来那段不再和它关联。',
      '那段旧视频的文件不删，别的照片如果也在用它，不受影响。',
    ],
    '换成新的',
  );
  if (!yes) return;
  try {
    await api('POST', `/photo/${existing.photoId}/video`, { videoPath });
    ok('已经把这张照片的视频换成新的了。');
  } catch (err) {
    fail(err, '换视频失败');
  }
}

/**
 * 从挂载点里挑一个文件，返回可以直接喂给 `POST /v1/photo` 的服务端路径。
 *
 * 第一层是挂载点列表（含 PHOTOAR_ROOTS 那几个根，它们走既有的 `/v1/fs/list`）。
 * 选中文件之后调 `fetch` —— local 直接返回原路径不拷贝，webdav 会先下载到落地目录。
 */
function pickFromMounts(wantKind, title) {
  const d = $('dlg-pick');
  $('dlg-pick-title').textContent = title;
  const crumbs = $('dlg-pick-crumbs');
  const list = $('dlg-pick-list');

  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => {
      if (done) return;
      done = true;
      d.removeEventListener('cancel', onCancel);
      closeDlg(d);
      resolve(v);
    };
    const onCancel = () => finish(null);
    d.addEventListener('cancel', onCancel);
    d.addEventListener('close', onCancel, { once: true });

    /** 第一层：挑一个来源。 */
    const showSources = async () => {
      clear(crumbs);
      clear(list);
      list.appendChild(el('div', { cls: 'skel' }));
      let doc;
      try {
        doc = state.mounts || (state.mounts = await api('GET', '/admin/mounts'));
      } catch (e) {
        clear(list);
        list.appendChild(el('div', { cls: 'failbox' }, [
          el('div', { cls: 'msg' }, [el('span', { text: e.message })]),
        ]));
        return;
      }
      clear(list);
      const sources = [
        ...(doc.envRoots || []).map((r) => ({ kind: 'root', name: r.name, path: r.path })),
        ...doc.mounts.filter((m) => m.enabled).map((m) => ({ kind: 'mount', mount: m, name: m.name })),
      ];
      if (!sources.length) {
        list.appendChild(el('div', { cls: 'empty' }, [
          el('p', { text: '没有可用的素材位置。' }),
          el('p', { cls: 'hint', text: '去「配置」页加一个挂载点。' }),
        ]));
        return;
      }
      for (const s of sources) {
        const b = el('button', { cls: 'fpick dir', type: 'button' }, [
          el('span', { cls: 'ic', text: s.kind === 'mount' && s.mount.kind === 'webdav' ? '☁' : '📁' }),
          el('span', { cls: 'nm', text: s.name }),
          el('span', { cls: 'sz mono', text: s.kind === 'mount' ? MOUNT_KIND_LABEL[s.mount.kind] : '本机' }),
        ]);
        b.addEventListener('click', () => {
          if (s.kind === 'root') browseRoot(s.path);
          else browseMount(s.mount, '');
        });
        list.appendChild(b);
      }
    };

    const crumbBar = (label, onRoot, cur) => {
      clear(crumbs);
      const home = el('button', { cls: 'crumb', type: 'button', text: '全部位置' });
      home.addEventListener('click', showSources);
      crumbs.appendChild(home);
      crumbs.appendChild(el('span', { cls: 'sep', text: '/' }));
      const up = el('button', { cls: 'crumb', type: 'button', text: label });
      up.addEventListener('click', onRoot);
      crumbs.appendChild(up);
      if (cur) {
        crumbs.appendChild(el('span', { cls: 'sep', text: '/' }));
        crumbs.appendChild(el('span', { cls: 'crumb cur mono', text: cur }));
      }
    };

    /** PHOTOAR_ROOTS 的根：走既有的 /v1/fs/list（它认绝对路径）。 */
    const browseRoot = async (abs) => {
      clear(list);
      list.appendChild(el('div', { cls: 'skel' }));
      let body;
      try {
        body = await api('GET', `/fs/list?path=${encodeURIComponent(abs)}`);
      } catch (e) {
        clear(list);
        list.appendChild(el('div', { cls: 'failbox' }, [
          el('div', { cls: 'msg' }, [el('span', { text: e.message })]),
        ]));
        return;
      }
      crumbBar('本机', () => showSources(), body.path);
      clear(list);
      paintEntries(body.entries, {
        onDir: (e) => browseRoot(`${body.path}/${e.name}`),
        onFile: (e) => finish(`${body.path}/${e.name}`),
        parent: body.parent ? () => browseRoot(body.parent) : null,
      });
    };

    /** 挂载点：走 /v1/admin/mounts/<id>/list，两种 kind 形状一样。 */
    const browseMount = async (mount, rel) => {
      clear(list);
      list.appendChild(el('div', { cls: 'skel' }));
      let body;
      try {
        const q = rel ? `?path=${encodeURIComponent(rel)}` : '';
        body = await api('GET', `/admin/mounts/${mount.id}/list${q}`);
      } catch (e) {
        clear(list);
        list.appendChild(el('div', { cls: 'failbox' }, [
          el('div', { cls: 'msg' }, [
            el('span', { text: e.message }),
            el('span', { cls: 'code', text: e.code }),
          ]),
        ]));
        return;
      }
      crumbBar(mount.name, () => browseMount(mount, ''), body.path);
      clear(list);
      paintEntries(body.entries, {
        onDir: (e) => browseMount(mount, joinRel(body.path, e.href || e.name)),
        onFile: async (e) => {
          const chosen = await fetchFromMount(mount, joinRel(body.path, e.href || e.name));
          if (chosen) finish(chosen);
        },
        parent: body.parent === null || body.parent === undefined
          ? null
          : () => browseMount(mount, body.parent),
      });
    };

    /** 画一层条目。类型不对的文件列出来但点不动（理由同 pickPath）。 */
    const paintEntries = (entries, { onDir, onFile, parent }) => {
      if (parent) {
        const up = el('button', { cls: 'fpick dir', type: 'button' }, [
          el('span', { cls: 'ic', text: '↑' }),
          el('span', { cls: 'nm', text: '上一级' }),
        ]);
        up.addEventListener('click', parent);
        list.appendChild(up);
      }
      if (!entries.length) {
        list.appendChild(el('div', { cls: 'empty' }, [el('p', { text: '这个目录是空的。' })]));
        return;
      }
      for (const e of entries) {
        if (e.isDir) {
          const b = el('button', { cls: 'fpick dir', type: 'button' }, [
            el('span', { cls: 'ic', text: '📁' }),
            el('span', { cls: 'nm', text: e.name }),
          ]);
          b.addEventListener('click', () => onDir(e));
          list.appendChild(b);
          continue;
        }
        const usable = e.kind === wantKind;
        const b = el('button', {
          cls: `fpick file${usable ? '' : ' off'}`,
          type: 'button',
          disabled: !usable,
          title: usable ? e.name
            : `这里要挑${wantKind === 'video' ? '视频' : '图片'}，这个是${KIND_TEXT[e.kind] || '认不出的类型'}`,
        }, [
          el('span', { cls: 'ic', text: e.kind === 'video' ? '🎬' : (e.kind === 'image' ? '🖼' : '·') }),
          el('span', { cls: 'nm', text: e.name }),
          el('span', { cls: 'sz mono', text: bytesText(e.bytes) }),
        ]);
        if (usable) b.addEventListener('click', () => onFile(e));
        list.appendChild(b);
      }
    };

    showSources();
    openDlg(d);
  });
}

function joinRel(base, name) {
  // WebDAV 的条目带 href（绝对且已编码），那种直接用，不能再和 base 拼。
  if (name.startsWith('/')) return name;
  return base ? `${base}/${name}` : name;
}

/** 把挂载点里的一个文件变成服务端本地路径。webdav 会先下载。 */
async function fetchFromMount(mount, rel) {
  const t = mount.kind === 'webdav'
    ? toast('ok', '正在从 WebDAV 下载…（大文件要等一会儿）', null, true)
    : null;
  try {
    const doc = await api('POST', `/admin/mounts/${mount.id}/fetch`, { path: rel });
    if (doc.copied) ok(`已下载到服务端：${doc.path}`);
    return doc.path;
  } catch (e) {
    fail(e, '取文件失败');
    return null;
  } finally {
    if (t) t.remove();
  }
}

// ============================== 起飞 ==============================

boot();

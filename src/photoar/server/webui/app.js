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
};

// ============================== 提示条 ==============================

/**
 * 成功的提示 4 秒后自己消失；失败的**不自动消失**，等人点掉。
 * 后端那句中文 message 是排查的全部线索，自动消失等于把它吞掉。
 */
function toast(kind, message, code) {
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
  if (kind !== 'bad') setTimeout(kill, 4000);
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
  showTab(state.tab);
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

const TABS = ['users', 'grants', 'config', 'photos'];

// 「刷新」按钮走这个（一定重新取数据）
const LOADERS = { users: loadUsers, grants: loadGrants, config: loadConfig, photos: loadPhotos };

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
  config: () => (state.config === null ? loadConfig() : renderConfig()),
  photos: () => (state.photos === null ? loadPhotos() : renderPhotos()),
};

function showTab(name) {
  if (!TABS.includes(name)) name = 'users';
  state.tab = name;
  for (const t of TABS) {
    const btn = document.querySelector(`.tabs button[data-tab="${t}"]`);
    const panel = $(`p-${t}`);
    const on = t === name;
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
    panel.hidden = !on;
  }
  // 数据只在第一次进这个页签时取，之后靠它自己的「刷新」按钮。管理台的数据几秒钟
  // 变一次是常态，但自动轮询会把「我正在改的这一屏」刷掉。
  ENTER[name]();
}

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

  // 按前缀分组，组内保持服务端给的顺序（那个顺序是 FIELDS 的声明顺序，有意义：
  // 「质量分下限」紧跟在「要不要检查质量」后面）。
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

async function loadPhotos() {
  const box = $('photos-body');
  skeleton(box, 4);
  try {
    const body = await api('GET', '/photos');
    state.photos = body.photos;
    renderPhotos();
  } catch (e) {
    if (e.status !== 401) failbox(box, e, loadPhotos);
  }
}

function renderPhotos() {
  const box = $('photos-body');
  clear(box);
  if (!state.photos.length) {
    emptyBox(box,
      '库里还没有照片。',
      '照片由手机 App 或批量入库脚本写入（要跑 arcoreimg 与转码），管理台只读、不做上传。');
    return;
  }

  const rows = state.photos.map((p) => el('tr', { 'data-pid': p.photoId }, [
    td('', thumb(p.photoId, p.title || p.photoId)),
    td('标题', [
      el('span', { text: p.title || '(无标题)' }),
      el('span', { cls: 'sub mono', text: p.photoId }),
    ], 'wide'),
    // 打印物理宽度：接口给的是米，照片是用厘米量的。原值放 title 里。
    td('打印宽度', el('span', { cls: 'mono', text: `${nf1(p.printWidthM * 100)} cm`, title: `${p.printWidthM} m` }), 'num'),
    td('贴合模式', el('span', { cls: 'muted fit', text: '取中…' })),
    td('质量分', el('span', { cls: 'mono', text: String(p.qualityScore) }), 'num'),
    td('状态', el('span', { cls: 'acts', style: 'justify-content:flex-start' }, [
      p.hasVideo ? el('span', { cls: 'tag ok', text: '有视频' }) : el('span', { cls: 'tag warn', text: '无视频' }),
      p.refStale ? el('span', { cls: 'tag bad', text: '参考图已变' }) : null,
    ])),
    td('入库时间', el('span', { text: fmtTime(p.createdAt) })),
  ]));

  box.appendChild(table(
    [{ sr: '缩略图' }, '标题', '打印宽度', '贴合模式', '质量分', '状态', '入库时间'],
    rows,
  ));

  // 贴合模式只在 `GET /v1/photo/<id>` 里（列表接口没有这一项），所以逐张补。
  // 限 4 个并发：家庭规模也就几十张，但一次甩出几十个请求会把单线程的服务端
  // 排满，而识别请求可能正排在后面。
  fillFitModes();
}

async function fillFitModes() {
  const gen = ++state.photosGen;
  const queue = state.photos.map((p) => p.photoId);
  const one = async (pid) => {
    let text = '?';
    let title = '';
    try {
      const d = state.photoDetail[pid] || (state.photoDetail[pid] = await api('GET', `/photo/${pid}`));
      const mode = d.fitMode;
      text = FIT_LABEL[mode] ? `${mode} · ${FIT_LABEL[mode]}` : String(mode);
      if (d.refMissing) title = '参考图文件读不到';
      if (d.videoMissing) title = (title ? title + '；' : '') + '视频文件读不到';
    } catch (e) {
      text = '取不到';
      title = e.message;
    }
    if (gen !== state.photosGen) return;   // 列表已经被刷新过，别写进新表格
    const row = document.querySelector(`#photos-body tr[data-pid="${pid}"] .fit`);
    if (!row) return;
    row.textContent = text;
    row.classList.remove('muted');
    row.classList.add('mono');
    if (title) row.title = title;
  };
  const worker = async () => { while (queue.length && gen === state.photosGen) await one(queue.shift()); };
  await Promise.all([worker(), worker(), worker(), worker()]);
}

// ============================== 起飞 ==============================

boot();

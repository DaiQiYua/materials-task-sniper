/* 任务狙击台 · 指挥中心前端逻辑
   数据契约：server/app.py 暴露的 REST + SSE
   设计约定：所有动态文本一律 textContent，禁止 innerHTML 拼接外部数据 */
'use strict';

/* ── 工具 ── */
const $ = (id) => document.getElementById(id);
const LOG_CAP = 500;

const state = {
  status: null,
  config: null,
  autoScroll: true,
  lastTasksJson: '',
  lastHistoryJson: '',
  accounts: [],            // 登录账号（凭据）
  platformAccounts: new Map(), // id -> {id,name,claimable,total,platform}（任务墙用的合并视图）
  platformByLogin: new Map(),  // cred_id -> {fetched_at, stale, fallback, items:[平台子账号], error}
  platformUnavailable: false,  // by-login 接口不可用（后端未就绪 / 网络异常）时的降级标记
  claims: {                // 领取记录整包快照（只在展开卡片 / 点「刷新」时拉，轮询不碰）
    loaded: false,
    loadedMs: 0,           // 本地拉取完成时刻（ms），用于短暂吃缓存
    fetchedAt: null,       // 后端给的 fetched_at（秒）
    byCred: new Map(),     // cred_id -> {fetchedAt, stats, items}
    errors: {},            // 上游按账号报的错：{"2":"HTTP 500"}
    error: '',             // 整包请求失败时的人话文案
  },
  claimsOpen: new Set(),   // 展开了「全部领取」的 cred_id
  lastCooldownsJson: '',   // 请求/任务冷却/待提交快照变化时才重绘账号卡
  expandedAccountId: null, // 手风琴：当前展开查看平台子账号的登录账号
  filters: { platform: null, onlyClaimable: false, platformAccountId: null },
  _platformHints: new Map(),   // code -> 上游给出的中文平台名
  _visibleCount: 0,            // 最近一次渲染后可见任务数（空态文案用）
};

const ACCENT_COLORS = ['#4cd97f', '#58a6ff', '#f0b429', '#bc8cff', '#ff8fab', '#2dd4bf'];
function colorFor(seed) {
  const s = String(seed || '');
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return ACCENT_COLORS[h % ACCENT_COLORS.length];
}
function initials(seed) {
  const s = String(seed || '?');
  return s.slice(0, 2).toUpperCase();
}

/* ── 平台（用于抢任务的子账号） ── */
const PLATFORMS = [
  { code: 'xiaohongshu', name: '小红书', short: '红', color: '#ff2e4d', text: '#ffffff' },
  { code: 'douyin', name: '抖音', short: '抖', color: '#25f4ee', text: '#04121a' },
  { code: 'kuaishou', name: '快手', short: '快', color: '#ff7a00', text: '#1a0d00' },
  { code: 'wx_channels', name: '视频号', short: '视', color: '#07c160', text: '#04240f' },
];
const PLATFORM_BY_CODE = new Map(PLATFORMS.map((p) => [p.code, p]));

/* 概览条的列数：CSS 读 --plat-cols，平台表加一项就自动多一格（窄屏另有 2 列折行规则） */
document.documentElement.style.setProperty('--plat-cols', String(PLATFORMS.length));

/* 平台计数桶：键一律从 PLATFORMS 派生，加平台只改上面那张表 */
function emptyPlatformCounts() {
  const counts = {};
  for (const p of PLATFORMS) counts[p.code] = 0;
  return counts;
}

/* 一组平台子账号按 platform 计数（未知平台的账号不计数，与概览副标题口径一致） */
function countPlatformAccounts(items) {
  const counts = emptyPlatformCounts();
  for (const a of (Array.isArray(items) ? items : [])) {
    const code = String((a && a.platform) || '').toLowerCase();
    if (counts[code] !== undefined) counts[code] += 1;
  }
  return counts;
}

/* 未知平台兜底色：低饱和中性灰蓝，不与已知平台的品牌色抢注意力 */
const UNKNOWN_PLATFORM_COLORS = ['#8b98a8', '#8f9bb3', '#94a0ad', '#8994a6', '#9aa3b0', '#8f9aa6'];

function seedHash(str) {
  const s = String(str || '');
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h;
}

function withAlpha(color, alpha) {
  const c = String(color || '').trim();
  const m = /^#([0-9a-f]{6})$/i.exec(c) || /^#([0-9a-f]{3})$/i.exec(c);
  if (!m) return c;
  let hex = m[1];
  if (hex.length === 3) hex = hex.split('').map((x) => x + x).join('');
  const r = parseInt(hex.slice(0, 2), 16);
  const g = parseInt(hex.slice(2, 4), 16);
  const b = parseInt(hex.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

function humanizePlatformCode(code) {
  const s = String(code || '').trim();
  if (!s) return '未知平台';
  // 中文等非 ASCII 名称原样返回
  if (/[^\x00-\x7F]/.test(s)) return s;
  return s
    .split(/[_\-\s]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(' ') || s;
}

function platformMetaOf(code) {
  const c = String(code || '').toLowerCase();
  const known = PLATFORM_BY_CODE.get(c);
  if (known) {
    return { code: c, name: known.name, short: known.short, color: known.color, text: known.text, known: true };
  }
  const hint = (state._platformHints && state._platformHints.get(c)) || '';
  const name = hint || humanizePlatformCode(c);
  return {
    code: c,
    name,
    short: name.charAt(0) || '?',
    color: UNKNOWN_PLATFORM_COLORS[seedHash(c) % UNKNOWN_PLATFORM_COLORS.length],
    text: '#0b1220',
    known: false,
  };
}

/* 一个任务涉及的所有平台 code（去重、小写）。t.platform 优先，
   其次看 claimEligibility.accounts 里的平台子账号。 */
function taskPlatformCodes(t) {
  const out = [];
  const add = (v) => {
    const c = String(v || '').trim().toLowerCase();
    if (c && !out.includes(c)) out.push(c);
  };
  if (!t) return out;
  add(t.platform);
  const elig = t.claimEligibility || {};
  const accounts = Array.isArray(elig.accounts) ? elig.accounts : [];
  for (const a of accounts) {
    if (a && typeof a === 'object') {
      if (a.platform) { add(a.platform); continue; }
      if (a.id != null) {
        const rec = state.platformAccounts.get(Number(a.id));
        if (rec && rec.platform) add(rec.platform);
      }
    } else if (typeof a === 'number' || typeof a === 'string') {
      const rec = state.platformAccounts.get(Number(a));
      if (rec && rec.platform) add(rec.platform);
    }
  }
  return out;
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
}

function fmtClock(epochSec) {
  const t = Number(epochSec);
  if (!isFinite(t) || t <= 0) return '—';
  return new Date(t * 1000).toTimeString().slice(0, 8);
}
function fmtAgo(ts) {
  const t = Number(ts);
  if (!isFinite(t) || t <= 0) return '';
  const d = Date.now() / 1000 - t;
  if (d < 60) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

/* ── Toast ── */
function toast(msg, kind = 'ok') {
  const t = el('div', 'toast ' + kind, msg);
  $('toastWrap').appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 260);
  }, 3200);
}

/* ── fetch 封装 ── */
async function api(path, opts = {}) {
  const init = { method: opts.method || 'GET' };
  if (opts.body != null) {
    init.body = opts.body;
    init.headers = { 'Content-Type': 'application/json' };
  }
  const res = await fetch(path, init);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 空响应体容错 */ }
  if (!res.ok) {
    const msg = (data && (data.message || data.detail)) || `请求失败（HTTP ${res.status}）`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ── 状态渲染（顶栏） ── */
const STATE_TEXT = { idle: '待命', running: '运行中', paused: '已暂停', error: '异常' };

function renderStatus(s) {
  if (!s) return;
  state.status = s;
  const st = s.state || 'idle';

  $('lamp').className = 'lamp ' + st;
  $('stateText').textContent = STATE_TEXT[st] || st;

  const reason = s.state_reason || '';
  const rEl = $('stateReason');
  rEl.className = 'state-reason st-' + st + (reason ? '' : ' hidden');
  rEl.textContent = reason;
  rEl.title = reason;

  const running = st === 'running';
  $('btnStop').classList.toggle('hidden', !running);
  $('btnStart').classList.toggle('hidden', running);
  $('btnStart').textContent = (st === 'paused' || st === 'error') ? '重新启动' : '启动狙击';

  renderTopStats(s);
  updateTokenBanner(s);
  renderOverview();   // 待提交数 / Token 有效数随状态刷新
  renderBoardEmpty(); // 空态下的轮询次数/频率随状态实时刷新

  // 冷却/待提交快照变化 → 重新绘制账号卡（状态标签与请求冷却倒计时）
  // 注意：只比较稳定字段，不能把每秒变化的 remaining_seconds 算进去。
  const reqKey = {};
  for (const [cid, rec] of Object.entries(s.request_cooldowns || {})) {
    reqKey[cid] = [rec.until, rec.reason, rec.pending_count, rec.pending_limit];
  }
  const cdKey = (s.task_cooldowns || []).map(
    (x) => [x.platform_account_id, x.task_id, x.until, x.task_type]);
  const pendKey = (s.pending_submissions || []).map(
    (x) => [x.platform_account_id, x.task_id, x.since]);
  const cdsJson = JSON.stringify({ r: reqKey, t: cdKey, p: pendKey });
  if (cdsJson !== state.lastCooldownsJson) {
    state.lastCooldownsJson = cdsJson;
    renderLoginAccounts();
  }
}

function renderTopStats(s) {
  const st = s.stats || {};
  $('tPolls').textContent = st.polls != null ? String(st.polls) : '—';
  $('tOk').textContent = st.claims_ok != null ? String(st.claims_ok) : '—';
  $('tConflict').textContent = st.claims_conflict != null ? String(st.claims_conflict) : '—';
  $('tFail').textContent = st.claims_fail != null ? String(st.claims_fail) : '—';
  // 同步时间：优先用后端上次轮询时间，否则显示当前时钟
  const syncEl = $('syncTime');
  if (syncEl) syncEl.textContent = (s && s.last_poll_at) ? fmtClock(s.last_poll_at) : fmtClock(Date.now() / 1000);
}

/* ── 账号概览指标（登录账号 / 平台子账号 / Token / 待提交） ── */
function renderOverview() {
  const accounts = state.accounts || [];
  // 平台子账号：合并所有登录账号桶，按 id 去重
  const allSubs = new Map();
  const platCounts = emptyPlatformCounts();
  for (const bucket of state.platformByLogin.values()) {
    const items = (bucket && Array.isArray(bucket.items)) ? bucket.items : [];
    for (const a of items) {
      if (!a || a.id == null) continue;
      const id = Number(a.id);
      if (!isFinite(id) || allSubs.has(id)) continue;
      allSubs.set(id, a);
      const code = String(a.platform || '').toLowerCase();
      if (platCounts[code] !== undefined) platCounts[code] += 1;
    }
  }

  // 登录账号数 + 异常数
  const abnormal = accounts.filter((c) => c && (c.last_error || tokenStatusFor(c).cls !== 'ok')).length;
  $('ovLogin').textContent = String(accounts.length);
  const loginHint = $('ovLoginHint');
  if (abnormal > 0) {
    loginHint.textContent = abnormal + ' 个异常';
    loginHint.classList.remove('hidden');
  } else {
    loginHint.classList.add('hidden');
  }

  // 平台子账号数 + 分平台分布
  $('ovSub').textContent = String(allSubs.size);
  $('ovSubHint').textContent = state.platformUnavailable
    ? '暂时无法获取'
    : PLATFORMS.map((p) => `${p.name} ${platCounts[p.code] || 0}`).join(' · ');

  // Token 有效数
  const valid = accounts.filter((c) => c && tokenStatusFor(c).cls === 'ok').length;
  $('ovToken').textContent = `${valid}/${accounts.length}`;

  // 待提交数：取所有账号 pending_submissions 去重（按 platform_account_id + task_id）
  const pendSet = new Set();
  const pendList = (state.status && state.status.pending_submissions) || [];
  for (const p of pendList) {
    pendSet.add(String(p.platform_account_id) + '|' + String(p.task_id));
  }
  $('ovPending').textContent = String(pendSet.size);

  // 账号区副标题
  const sub = $('accountSubtitle');
  if (sub) sub.textContent = `${accounts.length} 个登录账号 · ${allSubs.size} 个平台子账号`;

  // 手机端次级折叠条计数：任务 / 日志 / 领取记录
  const mst = $('mstMeta');
  if (mst) {
    const tasks = (state._tasksCache || []).length;
    const logs = $('logList') ? $('logList').children.length : 0;
    const hist = state.lastHistoryItems != null ? state.lastHistoryItems : 0;
    mst.textContent = `${tasks} / ${logs} / ${hist}`;
  }
}



function refreshStatus() {
  api('/api/status').then(renderStatus).catch(() => {});
}

/* ── 账号：登录账号（卡片内嵌平台子账号） ── */
function renderAccounts() {
  renderLoginAccounts();
}

function renderLoginAccounts() {
  const list = state.accounts || [];
  const box = $('loginList');
  box.textContent = '';

  // 默认展开第一个账号，便于一进页面就看到平台子账号
  if (state.expandedAccountId == null && list.length > 0 && state._autoExpanded !== list[0].id) {
    state.expandedAccountId = list[0].id;
    state._autoExpanded = list[0].id;
  }

  const frag = document.createDocumentFragment();

  for (const c of list) {
    const isOpen = state.expandedAccountId === c.id;
    // 每个账号各用自己的平台子账号桶计数
    const counts = platformCountsFor(c.id);
    const card = el('div', 'acct accordion' + (isOpen ? ' open' : ''));

    // ── 头部（点击展开/收起）──
    const head = el('div', 'acct-head');
    const ava = el('div', 'acct-ava ' + (c.last_error ? 'bad' : (c.last_ok_at ? 'ok' : '')));
    ava.appendChild(el('span', null, initials(c.name || c.phone || c.id)));
    const ring = el('span', 'ring');
    ava.appendChild(ring);
    head.appendChild(ava);

    const info = el('div', 'acct-info');
    const nameRow = el('div', 'acct-name');
    nameRow.appendChild(el('span', 'acct-name-text', c.name || '未命名账号'));
    nameRow.appendChild(el('span', 'acct-phone', c.phone_full || c.phone || '—'));
    info.appendChild(nameRow);

    const tokInfo = tokenStatusFor(c);
    let metaText, metaCls = '';
    if (c.last_error) {
      metaText = '最近失败：' + c.last_error;
      metaCls = ' err';
    } else {
      const segs = [`#${c.id}`, `Token ${tokInfo.text}`];
      if (c.last_ok_at) segs.push(`上次成功 ${fmtAgo(c.last_ok_at)}`);
      metaText = segs.join(' · ');
      if (tokInfo.cls === 'err') metaCls = ' err';
    }
    const metaEl = el('div', 'acct-meta' + metaCls);
    const reqUntil = requestCooldownFor(c.id);
    if (reqUntil) {
      const reqEl = el('span', 'req-cd', `请求冷却 ${fmtRemain(reqUntil - Date.now())}`);
      reqEl.dataset.cred = String(c.id);
      metaEl.appendChild(reqEl);
      metaEl.appendChild(document.createTextNode(' · '));
    }
    metaEl.appendChild(document.createTextNode(metaText));
    // 窄栏里 meta 会被单行省略号截断（新增「打开原站」按钮后更窄）：补 title，悬停可看全文
    metaEl.title = metaText;
    info.appendChild(metaEl);
    head.appendChild(info);

    // ── 头部右侧操作区（不占独立行；多个按钮收进一个容器，间距比头部主间距更紧）──
    const actions = el('div', 'acct-actions');

    const btnFetch = el('button', 'mini-btn primary', '取 Token');
    btnFetch.type = 'button';
    btnFetch.title = '重新获取该账号的 Token 与平台子账号';
    btnFetch.addEventListener('click', (e) => {
      e.stopPropagation();
      fetchTokenById(c.id, btnFetch);
    });
    actions.appendChild(btnFetch);

    // 打开原网站：纯文字按钮「原站」（设计稿要求去掉矢量图标，只保留文字）
    const btnOpen = el('button', 'mini-btn open-site', '原站');
    btnOpen.type = 'button';
    btnOpen.title = '打开原网站并自动登录（会新开一个浏览器窗口，需等几秒）';
    btnOpen.setAttribute('aria-label', '打开原网站并自动登录');
    btnOpen.addEventListener('click', (e) => {
      e.stopPropagation();
      openSiteForCred(c, btnOpen);
    });
    actions.appendChild(btnOpen);

    const btnDel = el('button', 'mini-btn danger', '删除');
    btnDel.type = 'button';
    btnDel.addEventListener('click', (e) => { e.stopPropagation(); deleteCred(c.id); });
    actions.appendChild(btnDel);
    head.appendChild(actions);

    const chev = el('span', 'acct-chev', '▾');
    head.appendChild(chev);
    head.addEventListener('click', () => {
      state.expandedAccountId = isOpen ? null : c.id;
      renderLoginAccounts();
      // 只有「展开」这一个动作会去拉领取记录；拉不到就静默，区块自己给文案
      if (!isOpen) ensureClaimsLoaded(c.id);
    });
    card.appendChild(head);

    // ── 平台概览条（每张卡常驻，避免一头沉）──
    const strip = el('div', 'acct-strip');
    for (const p of PLATFORMS) {
      const pill = el('div', 'plat-pill');
      const ico = el('span', 'plat-pill-ico', p.short);
      pill.appendChild(ico);
      pill.appendChild(el('span', 'plat-pill-name', p.name));
      const n = counts[p.code] || 0;
      const cnt = el('span', 'plat-pill-cnt', state.platformUnavailable ? '—' : String(n));
      if (n > 0) pill.classList.add('has');
      pill.appendChild(cnt);
      strip.appendChild(pill);
    }
    card.appendChild(strip);

    // ── 展开区：账号详情（点击查看）──
    if (isOpen) {
      card.appendChild(renderAccountDetail(c));
    }

    frag.appendChild(card);
  }
  box.appendChild(frag);

  $('loginEmpty').classList.toggle('hidden', list.length > 0);
  renderOverview();
}

/* 展开区只放平台子账号（身份/Token 已在头部一行内展示，避免重复） */
function renderAccountDetail(c) {
  const detail = el('div', 'acct-detail');

  const platWrap = el('div', 'acct-plat');
  const titleRow = el('div', 'acct-plat-title-row');
  titleRow.appendChild(el('div', 'acct-plat-title', '平台子账号 · 用于抢任务'));
  const refresh = el('button', 'mini-btn', '刷新');
  refresh.type = 'button';
  refresh.title = '重新拉取该账号名下的平台子账号';
  refresh.addEventListener('click', (e) => {
    e.stopPropagation();
    fetchPlatformAccountsForCred(c.id, refresh);
  });
  titleRow.appendChild(refresh);
  const sync = el('button', 'mini-btn', '核对');
  sync.type = 'button';
  sync.title = '拉取领取记录，核对任务冷却 / 待提交（没有任务下发时也能用）';
  sync.addEventListener('click', (e) => {
    e.stopPropagation();
    syncClaimsForCred(c.id, sync);
  });
  titleRow.appendChild(sync);
  platWrap.appendChild(titleRow);
  const platBox = el('div', 'acct-plat-list');
  platWrap.appendChild(platBox);
  detail.appendChild(platWrap);
  // 进行中任务 / 阶段：紧跟在平台子账号之后，是该账号下唯一的一处进行中任务列表
  detail.appendChild(renderClaimsBlock(c));
  renderPlatformSection(platBox, c.id);
  return detail;
}

/* 单个账号的 Token 状态（用 /api/credentials 的 has_token / token_exp） */
function tokenStatusFor(c) {
  const expRaw = c ? c.token_exp : null;
  let expSec = (expRaw != null && isFinite(Number(expRaw)) && Number(expRaw) > 0)
    ? Number(expRaw) : null;
  const hasFlag = (c && c.has_token != null) ? !!c.has_token : null;
  const present = hasFlag != null ? hasFlag : (expSec != null);
  if (!present) return { text: '未取 Token', cls: 'err' };
  if (expSec == null) return { text: '已获取（有效期未知）', cls: 'ok' };
  if (expSec * 1000 <= Date.now()) return { text: '已过期', cls: 'err' };
  const d = Math.max(0, Math.ceil((expSec * 1000 - Date.now()) / 86400000));
  return { text: `有效 · 余 ${d} 天`, cls: 'ok' };
}

/* 错误码 → 人话 */
function platformErrorText(code) {
  const map = {
    no_token: '该账号还没取到 Token',
    token_expired: 'Token 已过期，自动重登失败',
    network: '上游网络异常',
    rate_limited: '请求太频繁，稍后再试',
  };
  const key = String(code || '');
  return map[key] || (key || '暂未知原因');
}

/* 接口不可用（后端未就绪 / 404 / 5xx）时的统一降级文案 */
function friendlyApiError(err) {
  const msg = String((err && err.message) || '');
  if (/HTTP\s*(40[034]|5\d\d)/.test(msg)) return '暂时无法获取';
  return msg || '暂时无法获取';
}

function recomputePlatformAccounts() {
  const map = new Map();
  const tasks = state._tasksCache || [];
  for (const t of tasks) {
    const elig = t.claimEligibility || {};
    const accounts = Array.isArray(elig.accounts) ? elig.accounts : [];
    const can = !!elig.canClaim;
    for (const a of accounts) {
      const id = a.id != null ? Number(a.id) : (typeof a === 'number' ? a : null);
      if (id == null) continue;
      const rec = map.get(id) || { id, name: a.nickname || a.name || null, total: 0, claimable: 0, activeClaimCount: 0 };
      rec.total += 1;
      if (can) rec.claimable += 1;
      map.set(id, rec);
    }
  }
  // 合并所有登录账号的平台子账号：同 id 以首次写入者为准
  // （上游 fallback 可能把一条记录错标成别的平台，后来者不得覆盖已有 platform）。
  for (const bucket of state.platformByLogin.values()) {
    const items = (bucket && Array.isArray(bucket.items)) ? bucket.items : [];
    for (const rec of items) {
      const id = rec && rec.id != null ? Number(rec.id) : NaN;
      if (!isFinite(id)) continue;
      const existing = map.get(id) || { id, name: null, total: 0, claimable: 0, activeClaimCount: 0 };
      existing.name = existing.name || rec.name || null;
      if (!existing.platform && rec.platform) existing.platform = rec.platform;
      existing.platformName = existing.platformName || rec.platformName || null;
      existing.account = existing.account || rec.account || null;
      existing.verifyStatus = existing.verifyStatus || rec.verifyStatus || null;
      if (rec.activeClaimCount != null) existing.activeClaimCount = rec.activeClaimCount;
      map.set(id, existing);
    }
  }
  state.platformAccounts = map;

  // 收集上游给出的中文平台名：code -> 名称（只取第一次出现的非空名）
  const hints = new Map();
  const addHint = (code, name) => {
    const c = String(code || '').trim().toLowerCase();
    const n = String(name || '').trim();
    if (!c || !n || hints.has(c)) return;
    hints.set(c, n);
  };
  for (const t of tasks) addHint(t.platform, t.platformName);
  for (const rec of map.values()) addHint(rec.platform, rec.platformName);
  for (const bucket of state.platformByLogin.values()) {
    const items = (bucket && Array.isArray(bucket.items)) ? bucket.items : [];
    for (const rec of items) addHint(rec && rec.platform, rec && rec.platformName);
  }
  state._platformHints = hints;

  renderPlatformTabs();
}

/* ── 平台子账号状态判定 ──
   三类状态分开：
   - 请求冷却（10s，登录账号级，claim-limits）
   - 任务冷却（同任务 claimIntervalDays，通常 3 天）
   - 待提交（领取后未提交，继续领取的真实阻塞）
   任务列表只提供「上游是否可领」，不再按任务数量判定。 */
const TASK_TYPE_LABELS = { standard: '标准', long_term: '长期' };
function taskTypeLabel(t) {
  return TASK_TYPE_LABELS[String(t || '')] || String(t || '—');
}

function monitoredTaskTypes() {
  const out = [];
  const add = (t) => {
    t = String(t || '').trim();
    if (t && !out.includes(t)) out.push(t);
  };
  const cfg = state.config && state.config.task_types;
  if (Array.isArray(cfg)) cfg.forEach(add);
  for (const t of state._tasksCache || []) add(t.taskType);
  return out;
}

function parseTimeMs(raw) {
  if (raw == null || raw === '') return 0;
  const n = Number(raw);
  if (isFinite(n) && n > 0) return n < 1e12 ? n * 1000 : n;
  const ms = Date.parse(String(raw));
  return isFinite(ms) ? ms : 0;
}

function fmtClockMs(ms) {
  try { return new Date(ms).toLocaleTimeString(); } catch (_) { return '—'; }
}

function fmtRemain(ms) {
  const sec = Math.max(0, Math.ceil(ms / 1000));
  if (sec >= 86400) return `${Math.ceil(sec / 86400)}天`;
  if (sec >= 3600) return `${Math.ceil(sec / 3600)}小时`;
  if (sec >= 60) return `${Math.ceil(sec / 60)}分钟`;
  return `${sec}s`;
}

function requestCooldownFor(credId) {
  const map = (state.status && state.status.request_cooldowns) || {};
  const rec = map[String(credId)];
  if (!rec) return 0;
  const until = parseTimeMs(rec.until);
  return until > Date.now() ? until : 0;
}

/* 从任务快照 + 后端冷却记录推导「子账号 × 任务类型」 */
function deriveTypeStatus(accountId, taskType, rec) {
  const aid = Number(accountId);
  const tt = String(taskType || '');
  const out = {
    hasTasks: false,
    typeHasTasks: false,   // 当前任务列表里是否存在该类型任务（无论账号是否在名单）
    liveHasTasks: false,   // 该账号是否出现在当前任务的可领账号名单
    accountSeen: false,
    claimable: [],      // [{taskId}]
    cooling: [],        // [{taskId, untilMs, seconds}]（含历史/本地记录）
    liveCooling: [],    // 仅上游任务 nextClaimableAt 的冷却
    pending: [],        // [taskId]
    reasons: [],
  };
  const addTask = (id) => {
    if (id != null && !out.claimable.some((x) => x.taskId === id)
        && !out.cooling.some((x) => x.taskId === id)
        && !out.pending.includes(id)) out.hasTasks = true;
  };

  for (const t of state._tasksCache || []) {
    if (String(t.taskType) !== tt) continue;
    const ce = t.claimEligibility || {};
    const reason = String(ce.reason || '');
    out.typeHasTasks = true;
    for (const a of ce.accounts || []) {
      if (!a || Number(a.id) !== aid) continue;
      out.hasTasks = true;
      out.liveHasTasks = true;
      out.accountSeen = true;
      if (a.canClaim === true) out.claimable.push({ taskId: t.id });
      const until = parseTimeMs(a.nextClaimableAt);
      if (until > Date.now()) {
        out.cooling.push({ taskId: t.id, untilMs: until });
        out.liveCooling.push({ taskId: t.id, untilMs: until });
      }
      if (reason === 'pending_submission') out.pending.push(t.id);
      if (reason) out.reasons.push(reason);
    }
  }

  // 后端持久化的任务冷却 / 待提交（没任务下发时也能恢复）
  const cooldowns = (state.status && state.status.task_cooldowns) || [];
  for (const x of cooldowns) {
    if (Number(x.platform_account_id) !== aid || String(x.task_type || '') !== tt) continue;
    const until = parseTimeMs(x.until);
    if (until <= Date.now()) continue;
    out.hasTasks = true;
    if (!out.cooling.some((y) => y.taskId === x.task_id)) {
      out.cooling.push({ taskId: x.task_id, untilMs: until, seconds: x.seconds });
    }
  }
  const pending = (state.status && state.status.pending_submissions) || [];
  for (const p of pending) {
    if (Number(p.platform_account_id) !== aid || String(p.task_type || '') !== tt) continue;
    out.hasTasks = true;
    if (!out.pending.includes(p.task_id)) out.pending.push(p.task_id);
  }

  // 兜底：接口返回里内嵌的 eligibility（任务快照为空时仍可展示）
  const embedded = rec && rec.eligibility && rec.eligibility[tt];
  if (embedded) {
    out.hasTasks = out.hasTasks || !!embedded.has_tasks;
    out.typeHasTasks = out.typeHasTasks || !!embedded.live_has_tasks;
    out.liveHasTasks = out.liveHasTasks || !!embedded.live_has_tasks;
    out.accountSeen = out.accountSeen || !!embedded.live_has_tasks;
    if (embedded.claimable && !out.claimable.length) {
      for (const id of embedded.claimable_task_ids || []) out.claimable.push({ taskId: id });
    }
    for (const id of embedded.pending_task_ids || []) {
      if (!out.pending.includes(id)) out.pending.push(id);
    }
    for (const id of embedded.cooling_task_ids || []) {
      if (!out.cooling.some((x) => x.taskId === id)) {
        out.cooling.push({ taskId: id, untilMs: parseTimeMs(embedded.next_claimable_at) });
      }
    }
  }

  out.claimable = out.claimable.filter(
    (x) => !out.cooling.some((c) => c.taskId === x.taskId));
  out.pending = out.pending.filter(
    (id) => !out.claimable.some((x) => x.taskId === id));
  out.reasons = Array.from(new Set(out.reasons.filter(Boolean)));
  return out;
}

function accountTypeStatus(rec, taskType, credId) {
  const info = deriveTypeStatus(rec && rec.id, taskType, rec);
  const reqUntil = requestCooldownFor(credId);
  const aid = Number(rec && rec.id);
  const inflight = state.status && state.status.account_inflight
    ? Number(state.status.account_inflight[String(aid)] || 0) : 0;
  let status;
  if (inflight > 0) status = 'claiming';
  else if (info.pending.length) status = 'pending';
  else if (info.claimable.length) status = 'ready';
  else if (info.liveCooling.length) status = 'cooling';
  else if (!info.typeHasTasks) status = 'none';
  else status = 'blocked';
  return { status, info, reqUntil, inflight };
}

const STATUS_LABEL = {
  claiming: '领取中',
  ready: '可抢',
  pending: '待提交',
  cooling: '任务冷却',
  blocked: '不可领',
  none: '无任务',
};

/* 状态依据（tooltip / 点击核对共用） */
function statusEvidence(taskType, res) {
  const lines = [];
  if (res.inflight) {
    lines.push(`领取中：${res.inflight} 个请求在途`);
  }
  if (res.reqUntil) {
    lines.push(`请求冷却：至 ${fmtClockMs(res.reqUntil)}（剩 ${fmtRemain(res.reqUntil - Date.now())}）· claim-limits`);
  }
  const info = res.info;
  if (info.pending.length) {
    lines.push(`待提交任务：${info.pending.map((id) => '#' + id).join('、')}`);
  }
  if (info.claimable.length) {
    lines.push(`可领任务：${info.claimable.map((x) => '#' + x.taskId).join('、')}`);
  }
  if (info.cooling.length) {
    const parts = info.cooling.slice(0, 5).map((x) =>
      `#${x.taskId} 至 ${fmtClockMs(x.untilMs)}（${fmtRemain(x.untilMs - Date.now())}）`);
    lines.push(`任务冷却：${parts.join('；')}${info.cooling.length > 5 ? ' …' : ''}`);
  }
  if (!lines.length) {
    lines.push(info.typeHasTasks
      ? `任务存在但当前不可领${info.reasons.length ? '：' + info.reasons.join('、') : ''}`
      : `当前没有「${taskTypeLabel(taskType)}」任务`);
  }
  return lines;
}

/* 每秒刷新状态胶囊与请求冷却倒计时 */
function tickCooldownChips() {
  for (const chip of document.querySelectorAll('.cd-chip')) {
    const rec = chip.__rec;
    const tt = chip.dataset.type;
    const cred = chip.dataset.cred;
    if (!rec || !tt) continue;
    const res = accountTypeStatus(rec, tt, cred);
    chip.className = 'cd-chip ' + res.status;
    chip.textContent = `${taskTypeLabel(tt)} ${STATUS_LABEL[res.status] || res.status}`;
    chip.title = [`${taskTypeLabel(tt)} · ${STATUS_LABEL[res.status] || res.status}`,
      ...statusEvidence(tt, res)].join('\n');
  }
  const now = Date.now();
  for (const elx of document.querySelectorAll('.req-cd')) {
    const cred = elx.dataset.cred;
    const until = requestCooldownFor(cred);
    if (!until || until <= now) {
      elx.classList.add('hidden');
      continue;
    }
    elx.classList.remove('hidden');
    elx.textContent = `请求冷却 ${fmtRemain(until - now)}`;
    elx.title = `领取请求冷却至 ${fmtClockMs(until)}（claim-limits，10 秒级）`;
  }
}

/* 把某个登录账号自己桶里的平台子账号渲染进指定容器（卡片内嵌） */
function renderPlatformSection(box, credId) {
  box.textContent = '';
  const frag = document.createDocumentFragment();
  const selected = state.filters.platformAccountId;
  const bucket = state.platformByLogin.get(Number(credId));
  const raw = (bucket && Array.isArray(bucket.items)) ? bucket.items : [];

  // 集合以该账号的桶为准；仅从合并视图补上「进行中」等上游统计
  const entries = raw.map((a) => {
    const id = a && a.id != null ? Number(a.id) : NaN;
    const merged = state.platformAccounts.get(id) || {};
    return Object.assign({}, merged, a, { id });
  }).filter((a) => isFinite(a.id))
    .sort((a, b) => (b.activeClaimCount || 0) - (a.activeClaimCount || 0));
  for (const rec of entries) {
    const card = el('div', 'pacct nested' + (selected === rec.id ? ' selected' : ''));

    const ava = el('div', 'pacct-ava');
    const p = PLATFORMS.find((x) => x.code === rec.platform)
      || { short: (rec.platformName || '?')[0] || '?' };
    ava.textContent = p.short;
    card.appendChild(ava);

    const info = el('div', 'pacct-info');
    info.appendChild(el('div', 'pacct-id', '#' + rec.id + (rec.name ? ` · ${rec.name}` : '')));
    const tagRow = el('div', 'pacct-tags');
    if (rec.platform) {
      const plat = PLATFORMS.find((x) => x.code === rec.platform)
        || { short: rec.platformName || rec.platform, name: rec.platformName || rec.platform };
      const tag = el('span', 'plat-tag', plat.name || plat.short);
      tagRow.appendChild(tag);
    }
    if (rec.verifyStatus) {
      const vs = el('span', 'vs-tag ' + (rec.verifyStatus === 'verified' ? 'ok' : 'warn'),
        rec.verifyStatus === 'verified' ? '已认证' : rec.verifyStatus);
      tagRow.appendChild(vs);
    }
    if (rec.account) tagRow.appendChild(el('span', 'pacct-acc', '@' + rec.account));
    info.appendChild(tagRow);
    const stats = el('div', 'pacct-stats');
    const types = monitoredTaskTypes();
    if (types.length) {
      for (const tt of types) {
        const res = accountTypeStatus(rec, tt, credId);
        const chip = el('span', 'cd-chip ' + res.status,
          `${taskTypeLabel(tt)} ${STATUS_LABEL[res.status] || res.status}`);
        chip.dataset.type = tt;
        chip.dataset.cred = String(credId);
        chip.__rec = rec;
        chip.title = [`${taskTypeLabel(tt)} · ${STATUS_LABEL[res.status] || res.status}`,
          ...statusEvidence(tt, res)].join('\n');
        // 点击直接看依据（请求冷却 / 待提交 / 任务冷却 / 可领）
        chip.addEventListener('click', (e) => {
          e.stopPropagation();
          const cur = accountTypeStatus(rec, tt, credId);
          const evidence = statusEvidence(tt, cur).join(' | ');
          const kind = cur.status === 'ready' ? 'ok'
            : cur.status === 'none' || cur.status === 'blocked' ? 'warn' : 'warn';
          toast(`${taskTypeLabel(tt)} · ${STATUS_LABEL[cur.status] || cur.status} · ${evidence}`, kind);
        });
        stats.appendChild(chip);
      }
      stats.appendChild(el('span', 'dot', '·'));
    }
    const statActive = el('span', null, `进行中 ${rec.activeClaimCount != null ? rec.activeClaimCount : 0}`);
    statActive.title = '上游平台返回：该账号当前正在执行中的领取任务数（activeClaimCount）';
    stats.appendChild(statActive);
    info.appendChild(stats);
    card.appendChild(info);

    const picked = selected === rec.id;
    const chev = el('span', 'pacct-chev', picked ? '已筛选 ✓' : '只看该号 ▾');
    chev.title = picked
      ? '只看该子账号的进行中任务（不影响任务速览），再点一次取消'
      : '点击后只看该子账号的进行中任务；任务速览始终显示平台下发的全部任务';
    card.appendChild(chev);

    // 点击行：只收窄本卡片「进行中任务」的子账号作用域，任务速览不受影响
    card.addEventListener('click', () => {
      state.filters.platformAccountId = picked ? null : rec.id;
      if (!picked) ensureClaimsLoaded(credId);
      renderLoginAccounts();
    });
    frag.appendChild(card);
  }
  box.appendChild(frag);

  if (!entries.length) {
    const row = el('div', 'plat-empty-row');
    row.appendChild(el('span', 'pacct-empty', '该账号名下还没有平台子账号'));
    const btn = el('button', 'mini-btn', '立即获取');
    btn.type = 'button';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      fetchPlatformAccountsForCred(credId, btn);
    });
    row.appendChild(btn);
    box.appendChild(row);

    // 空态下显示失败原因（请求出错 / 该账号最近一次平台拉取错误 / 接口不可用）
    const reason = (bucket && bucket.error)
      || platformLastErrorFor(credId)
      || (state.platformUnavailable ? '暂时无法获取' : '');
    if (reason) {
      const hint = el('div', 'plat-empty-row');
      hint.appendChild(el('span', 'pacct-empty', '原因：' + reason));
      box.appendChild(hint);
    }
  }
}

/* 该登录账号最近一次平台拉取失败原因（来自 /api/credentials 的 last_platform_error） */
function platformLastErrorFor(credId) {
  const list = state.accounts || [];
  for (const c of list) {
    if (Number(c.id) === Number(credId)) {
      return c.last_platform_error ? platformErrorText(c.last_platform_error) : '';
    }
  }
  return '';
}

/* 某登录账号的平台子账号（id + 名称），顺序与卡片里的子账号行一致 */
function platformSubAccounts(credId) {
  const bucket = state.platformByLogin.get(Number(credId));
  const raw = (bucket && Array.isArray(bucket.items)) ? bucket.items : [];
  return raw.map((a) => {
    const id = a && a.id != null ? Number(a.id) : NaN;
    const merged = state.platformAccounts.get(id) || {};
    return { id, name: String((a && a.name) || merged.name || '') };
  }).filter((s) => isFinite(s.id));
}

/* 单个账号的平台子账号数量（卡片概览条用） */
function platformCountsFor(credId) {
  const bucket = state.platformByLogin.get(Number(credId));
  return countPlatformAccounts((bucket && Array.isArray(bucket.items)) ? bucket.items : []);
}

/* by-login 单桶归一化 */
function normalizePlatformBucket(data) {
  const items = (data && Array.isArray(data.items)) ? data.items : [];
  return {
    fetched_at: (data && data.fetched_at != null) ? Number(data.fetched_at) : null,
    stale: !!(data && data.stale),
    fallback: !!(data && data.fallback),
    items: items.filter((a) => a && isFinite(Number(a.id))),
    error: (data && data.error) ? String(data.error) : '',
  };
}

/* 拉取「按登录账号分组」的平台子账号总表，填充 state.platformByLogin */
function loadPlatformByLogin(force) {
  if (!force) {
    if (state._plPromise) return state._plPromise;
    if (state._plLoadedAt && Date.now() - state._plLoadedAt < 600) return Promise.resolve();
  }
  state._plPromise = (async () => {
    let ok = false;
    try {
      const data = await api('/api/platform-accounts/by-login');
      const items = (data && data.items) || {};
      const next = new Map();
      for (const key of Object.keys(items)) {
        const credId = Number(key);
        if (!isFinite(credId)) continue;
        next.set(credId, normalizePlatformBucket(items[key]));
      }
      state.platformByLogin = next;
      state.platformUnavailable = false;
      ok = true;
    } catch (_) {
      // 后端未就绪 / 网络异常：保留已有桶，标记为不可用并降级展示
      state.platformUnavailable = true;
    } finally {
      if (ok) state._plLoadedAt = Date.now();
      state._plPromise = null;
      recomputePlatformAccounts();
      renderLoginAccounts();
    }
  })();
  return state._plPromise;
}

/* 拉取单个登录账号名下的平台子账号（POST refresh-platform-accounts） */
async function fetchPlatformAccountsForCred(credId, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/credentials/${credId}/refresh-platform-accounts`, { method: 'POST' });
    if (r && r.ok) {
      state.platformByLogin.set(Number(credId), normalizePlatformBucket(r));
      state.platformUnavailable = false;
      const n = (r.count != null) ? Number(r.count) : (Array.isArray(r.items) ? r.items.length : 0);
      toast(`已获取该账号的平台子账号（${n} 个）`);
    } else {
      markPlatformError(credId, (r && r.error) ? platformErrorText(r.error) : '暂时无法获取');
      toast('获取平台子账号失败：' + ((r && r.error) ? platformErrorText(r.error) : '暂时无法获取'), 'err');
    }
  } catch (err) {
    const reason = friendlyApiError(err);
    markPlatformError(credId, reason);
    toast('获取平台子账号失败：' + reason, 'err');
  } finally {
    if (btn) btn.disabled = false;
    recomputePlatformAccounts();
    renderLoginAccounts();
  }
}

/* 把失败原因写进该账号的桶，空态即可显示原因小字 */
function markPlatformError(credId, reason) {
  const id = Number(credId);
  const prev = state.platformByLogin.get(id);
  state.platformByLogin.set(id, {
    fetched_at: prev ? prev.fetched_at : null,
    stale: prev ? prev.stale : false,
    fallback: prev ? prev.fallback : false,
    items: (prev && Array.isArray(prev.items)) ? prev.items : [],
    error: reason || '',
  });
}

/* fetch-token 响应内联返回的 platform_accounts 直接写入该账号的桶 */
function applyInlinePlatformAccounts(credId, r) {
  if (!r || !Array.isArray(r.platform_accounts)) return;
  const id = Number(credId);
  const prev = state.platformByLogin.get(id);
  state.platformByLogin.set(id, {
    fetched_at: prev ? prev.fetched_at : null,
    stale: false,
    fallback: false,
    items: r.platform_accounts.filter((a) => a && isFinite(Number(a.id))),
    error: r.platform_error ? platformErrorText(r.platform_error) : '',
  });
  recomputePlatformAccounts();
}

/* ── 平台子 tab（替代原分类 chips） ──
   code 来源：任务里出现的平台 + 已登录账号的平台子账号。
   顺序：已知平台按 PLATFORMS 顺序，未知平台按首次出现顺序。 */
function platformTabCodes() {
  const counts = new Map();
  const order = [];
  const add = (code, withCount) => {
    const c = String(code || '').trim().toLowerCase();
    if (!c) return;
    if (!counts.has(c)) { counts.set(c, 0); order.push(c); }
    if (withCount) counts.set(c, counts.get(c) + 1);
  };
  for (const t of state._tasksCache || []) {
    for (const c of taskPlatformCodes(t)) add(c, true);
  }
  for (const rec of state.platformAccounts.values()) if (rec) add(rec.platform, false);
  for (const bucket of state.platformByLogin.values()) {
    const items = (bucket && Array.isArray(bucket.items)) ? bucket.items : [];
    for (const rec of items) if (rec) add(rec.platform, false);
  }
  const known = PLATFORMS.map((p) => p.code).filter((c) => counts.has(c));
  const unknown = order.filter((c) => !PLATFORM_BY_CODE.has(c));
  return { codes: known.concat(unknown), counts };
}

function makePlatformTab(code) {
  const btn = el('button', 'plat-tab');
  btn.type = 'button';
  btn.dataset.platform = code;
  const meta = code === 'all' ? null : platformMetaOf(code);
  if (meta) {
    const dot = el('span', 'plat-dot');
    dot.style.background = 'var(--cyan)';
    btn.appendChild(dot);
  }
  btn.appendChild(el('span', 'plat-tab-name', meta ? meta.name : '全部'));
  btn.appendChild(el('span', 'plat-tab-n', '0'));
  return btn;
}

function renderPlatformTabs() {
  const box = $('platformTabs');
  if (!box) return false;
  const { codes, counts } = platformTabCodes();
  // 任务被清空 / 平台消失时自动落回「全部」，避免永远筛不出东西
  let reset = false;
  if (state.filters.platform && !codes.includes(state.filters.platform)) {
    state.filters.platform = null;
    reset = true;
  }
  const total = (state._tasksCache || []).length;
  const key = codes.join('|');
  if (box.__key !== key) {
    box.textContent = '';
    box.appendChild(makePlatformTab('all'));
    for (const c of codes) box.appendChild(makePlatformTab(c));
    box.__key = key;
  }
  const activeCode = state.filters.platform || 'all';
  for (const btn of box.querySelectorAll('.plat-tab')) {
    const code = btn.dataset.platform || 'all';
    const on = code === activeCode;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    const n = code === 'all' ? total : (counts.get(code) || 0);
    btn.__n = n;
    const nEl = btn.querySelector('.plat-tab-n');
    if (nEl) {
      nEl.textContent = String(n);
      nEl.classList.toggle('hidden', n === 0);
    }
    const meta = code === 'all' ? null : platformMetaOf(code);
    btn.title = meta
      ? `${meta.name}${meta.known ? '' : `（原始 code: ${code}）`} · ${n} 个任务`
      : `全部平台 · ${n} 个任务`;
  }
  return reset;
}

/* 「清除筛选」胶囊：只反映任务速览的平台筛选。
   子账号作用域只影响账号卡片里的「进行中任务」，与任务速览无关，故不在此回显。 */
function renderFilterReset() {
  const btn = $('filterReset');
  if (!btn) return;
  const parts = [];
  if (state.filters.platform) parts.push(platformMetaOf(state.filters.platform).name);
  if (!parts.length) {
    btn.classList.add('hidden');
    btn.textContent = '清除筛选';
    btn.removeAttribute('title');
    return;
  }
  btn.classList.remove('hidden');
  btn.textContent = '清除：' + parts.join(' + ');
  btn.title = '点击清除平台筛选（任务速览只受平台筛选影响）';
}

/* ── 任务卡片墙 ── */
function renderTasks(items) {
  const list = Array.isArray(items) ? items : [];
  state._tasksCache = list;

  const json = JSON.stringify(list);
  if (json === state.lastTasksJson) return; // 幂等：内容未变不重绘
  state.lastTasksJson = json;

  recomputePlatformAccounts();
  renderLoginAccounts();
  renderTaskBoard();
}

function taskMatches(t, f) {
  const elig = t.claimEligibility || {};
  const can = !!elig.canClaim;
  if (f.onlyClaimable && !can) return false;
  if (f.platform) {
    if (!taskPlatformCodes(t).includes(f.platform)) return false;
  }
  // 任务速览 = 「平台下发的任务池」：只按平台 / 可领取性筛选。
  // 绝不按「当前查看的子账号」收窄——那等于拿 claimEligibility.accounts 当可见性开关，
  // 会把平台已下发、但当前账号暂无资格领的任务（如未认证视频号，上游返回 accounts: []）
  // 整体隐藏，看起来像"平台没下发任务"。资格问题应由状态标注（可领取/不可领）表达。
  return true;
}

/* 任务墙空态：运行中显示转圈 + 轮询次数/频率，否则静默文案 */
function fmtInterval(sec) {
  sec = Number(sec);
  if (!isFinite(sec) || sec <= 0) return '—';
  if (sec < 60) return (Math.round(sec * 10) / 10) + ' 秒';
  return (Math.round(sec / 6) / 10) + ' 分钟';
}
function renderBoardEmpty() {
  const box = $('boardEmpty');
  if (!box || box.classList.contains('hidden')) return;
  const s = state.status;
  const running = !!(s && s.state === 'running');
  const all = (state._tasksCache || []).length;
  const visible = state._visibleCount || 0;
  const filteredOut = all > 0 && visible === 0;
  $('boardSpinner').classList.toggle('hidden', !running || filteredOut);

  const text = $('boardEmptyText');
  const stats = $('boardEmptyStats');
  if (filteredOut) {
    text.textContent = '当前筛选下没有任务';
    stats.textContent = `共 ${all} 个任务被平台筛选隐藏 · 点「清除筛选」可看全部`;
    return;
  }
  if (!running) {
    text.textContent = '暂无任务';
    stats.textContent = '引擎未启动 · 点「刷新」可手动拉取，启动狙击后自动轮询';
    return;
  }
  text.textContent = '等待上游轮询…';
  const polls = (s && s.stats && s.stats.polls != null) ? s.stats.polls : 0;
  const itv = (state.config && state.config.poll_interval != null) ? Number(state.config.poll_interval) : null;
  const freq = (itv && itv > 0) ? `每 ${fmtInterval(itv)} 一次` : '频率未知';
  const last = (s && s.last_poll_at) ? ` · 上次刷新 ${fmtClock(s.last_poll_at)}` : '';
  stats.textContent = '';
  const b = el('b', null, String(polls));
  stats.appendChild(document.createTextNode('已刷新 '));
  stats.appendChild(b);
  stats.appendChild(document.createTextNode(` 次 · ${freq}${last}`));
}

function renderTaskBoard() {
  const f = state.filters;
  const all = state._tasksCache || [];
  let visible = all.filter((t) => taskMatches(t, f));
  state._visibleCount = visible.length;
  // 平台 tab 重建时可能因平台消失而自动落回「全部」，此时重新过滤一次
  if (renderPlatformTabs()) {
    visible = all.filter((t) => taskMatches(t, f));
    state._visibleCount = visible.length;
  }
  renderFilterReset();

  const grid = $('taskGrid');
  grid.textContent = '';
  const frag = document.createDocumentFragment();
  let claimable = 0;

  for (const t of visible) {
    const elig = t.claimEligibility || {};
    const can = !!elig.canClaim;
    const quota = t.remainingQuota;
    const exhausted = (quota != null && Number(quota) <= 0);
    const cls = can && !exhausted ? 'claimable' : (exhausted ? 'exhausted' : 'taken');
    if (can && !exhausted) claimable += 1;

    const card = el('div', 'trow ' + cls);

    // 左侧：标题 + 元信息（平台 · 编号）
    const info = el('div', 'trow-info');
    info.appendChild(el('div', 'trow-title', t.title != null ? String(t.title) : '—'));
    const metaLine = el('div', 'trow-meta');
    const codes = taskPlatformCodes(t);
    if (codes.length) {
      const metas = codes.map((c) => platformMetaOf(c));
      const plats = metas.map((m) => m.name);
      const badge = el('span', 'trow-plat');
      badge.title = (codes.length > 1 ? plats.join(' · ') : plats[0]) + `（${codes.join(' / ')}）`;
      const dot = el('span', 'trow-plat-dot');
      dot.style.background = 'var(--cyan)';
      badge.appendChild(dot);
      badge.appendChild(el('span', null, metas[0].name));
      if (metas.length > 1) badge.appendChild(el('span', 'trow-plat-more', '+' + (metas.length - 1)));
      metaLine.appendChild(badge);
    }
    metaLine.appendChild(el('span', 'trow-id', `#${t.id != null ? t.id : '—'}`));
    info.appendChild(metaLine);
    card.appendChild(info);

    // 右侧：状态（可领取 / 冷却 / 已满）
    const accounts = Array.isArray(elig.accounts) ? elig.accounts : [];
    let stateLabel, stateTone;
    if (can && !exhausted) {
      stateLabel = '可领取'; stateTone = 'ok';
    } else if (exhausted) {
      stateLabel = '已满'; stateTone = 'off';
    } else {
      const reason = String(elig.reason || '');
      if (reason === 'cooling_down') { stateLabel = '冷却'; stateTone = 'warn'; }
      else if (reason === 'pending_submission') { stateLabel = '待提交'; stateTone = 'warn'; }
      else { stateLabel = '不可领'; stateTone = 'off'; }
    }
    const stEl = el('span', 'trow-state ' + stateTone, stateLabel);
    if (!can && !exhausted) {
      const reason = String(elig.reason || '');
      if (reason === 'cooling_down' && elig.nextClaimableAt) {
        stEl.title = '冷却至 ' + String(elig.nextClaimableAt).replace('T', ' ').slice(5, 16);
      } else if (reason) {
        stEl.title = reason;
      }
    }
    card.appendChild(stEl);

    frag.appendChild(card);
  }
  grid.appendChild(frag);

  $('boardEmpty').classList.toggle('hidden', visible.length > 0);
  renderBoardEmpty();
  $('boardMeta').textContent = all.length ? `共 ${all.length} · ${claimable} 可领` : '';
  // 同步手机端折叠条计数
  const mst = $('mstMeta');
  if (mst) {
    const logs = $('logList') ? $('logList').children.length : 0;
    const hist = state.lastHistoryItems != null ? state.lastHistoryItems : 0;
    mst.textContent = `${all.length} / ${logs} / ${hist}`;
  }
}

/* 后端 errors 里可能给字符串，也可能给带 message/detail 的对象：统一取一句人话 */
function firstTaskError(errors) {
  const list = Array.isArray(errors) ? errors.filter(Boolean) : [];
  const first = list[0];
  if (typeof first === 'string') return first.trim();
  if (first && typeof first === 'object') {
    const m = first.message || first.detail || first.error;
    if (typeof m === 'string' && m.trim()) return m.trim();
  }
  return '';
}

/* ── 任务速览 · 手动刷新 ──
   后端在引擎停止时也能向上游拉一次，所以这里不看 state.status：
   点一下就真去拉，回来把整只列表灌进 renderTasks，再报条数。 */
async function refreshTasksManual(btn) {
  if (!btn || btn.disabled) return;          // 连点保护
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = '刷新中…';
  try {
    const r = await api('/api/tasks/refresh', { method: 'POST' });
    const hasTasks = !!(r && r.tasks && Array.isArray(r.tasks.items));
    const items = hasTasks ? r.tasks.items : [];
    const ok = !!(r && r.ok);

    // 拉到任务（或明确成功）才回灌列表：失败且没带任务时不要把手上的列表清空
    if (ok || hasTasks) {
      const before = state.lastTasksJson;
      renderTasks(items);
      // renderTasks 内容未变时会跳过重绘（幂等），这里补一次，
      // 保证「共 N · M 可领」与空态文案紧跟手动刷新
      if (state.lastTasksJson === before) renderTaskBoard();
    }

    if (ok) {
      const n = (r.count != null && isFinite(Number(r.count))) ? Number(r.count) : items.length;
      toast(n > 0 ? `已手动拉取 ${n} 个任务` : '已拉取，上游暂无任务', 'ok');
    } else {
      const detail = firstTaskError(r && r.errors);
      if (detail) toast('拉取失败：' + detail, 'err');
      else toast('拉取失败：上游没有返回任务数据', 'warn');
    }
  } catch (err) {
    // api() 抛出的 message 已是中文人话；断网时 fetch 只给英文，兜一句中文
    const msg = String((err && err.message) || '').trim();
    toast(!msg || /failed to fetch|network ?error|load failed/i.test(msg)
      ? '网络异常，连不上本地服务' : msg, 'err');
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

/* ── 领取历史（卡片） ── */
const HISTORY_BADGE = {
  ok: ['ok', '成功'], fail: ['fail', '失败'], conflict: ['conflict', '慢一步'],
  ratelimit: ['ratelimit', '限速'],
};
function renderHistory(items) {
  const src = Array.isArray(items) ? items : [];
  // 最新的领取记录排在最上面（ts 为 YYYY-MM-DDTHH:MM:SS，可直接按字符串比较）
  const list = src.slice().sort((a, b) => {
    const ta = String((a && a.ts) || '');
    const tb = String((b && b.ts) || '');
    if (ta === tb) {
      const ia = Number(a && a.task_id);
      const ib = Number(b && b.task_id);
      if (isFinite(ia) && isFinite(ib) && ia !== ib) return ib - ia;
      return 0;
    }
    return ta < tb ? 1 : -1;
  });
  const json = JSON.stringify(list);
  if (json === state.lastHistoryJson) return;
  state.lastHistoryJson = json;

  const box = $('historyList');
  box.textContent = '';
  const frag = document.createDocumentFragment();

  for (const h of list) {
    const card = el('div', 'hist-card');
    const top = el('div', 'hist-top');
    top.appendChild(el('div', 'hist-title', h.title != null ? String(h.title) : (h.task_id != null ? `#${h.task_id}` : '—')));
    const map = HISTORY_BADGE[h.status];
    top.appendChild(el('span', 'hist-badge ' + (map ? map[0] : 'off'), map ? map[1] : String(h.status || '—')));
    card.appendChild(top);

    const meta = el('div', 'hist-meta');
    meta.appendChild(el('span', null, (h.ts != null ? String(h.ts).replace('T', ' ') : '—')));
    if (h.account_id != null) meta.appendChild(el('span', null, '账号 #' + h.account_id));
    if (h.commission != null) { const b = el('span'); b.appendChild(document.createTextNode('佣金 ')); b.appendChild(el('b', null, String(h.commission))); meta.appendChild(b); }
    if (h.http != null) meta.appendChild(el('span', null, 'HTTP ' + h.http));
    card.appendChild(meta);

    if (h.message != null && h.message !== '') card.appendChild(el('div', 'hist-msg', String(h.message)));
    frag.appendChild(card);
  }
  box.appendChild(frag);

  $('historyEmpty').classList.toggle('hidden', list.length > 0);
  state.lastHistoryItems = list.length;
  const mst = $('mstMeta');
  if (mst) mst.textContent = `${(state._tasksCache || []).length} / ${$('logList').children.length} / ${list.length}`;
}

/* ── 日志流 ── */
function appendLogEntry(l, animate) {
  const list = $('logList');
  const level = String(l.level || 'INFO').toUpperCase();
  const line = el('div', 'log-line lv-' + level.toLowerCase() + (animate ? ' fresh' : ''));
  line.appendChild(el('span', 'log-ts', l.ts || ''));
  line.appendChild(el('span', 'log-lv', level));
  line.appendChild(el('span', 'log-msg', l.msg != null ? String(l.msg) : ''));
  // 最新消息在最上面：新行插到列表头部，超出上限时从尾部淘汰最旧的
  list.insertBefore(line, list.firstChild);
  while (list.children.length > LOG_CAP) list.removeChild(list.lastChild);
  if (state.autoScroll) {
    const sc = $('logScroll');
    sc.scrollTop = 0;
  } else {
    $('jumpBottom').classList.remove('hidden');
  }
}
function renderLogsSnapshot(logs) {
  const list = Array.isArray(logs) ? logs : [];
  $('logList').textContent = '';
  // 后端给的是「旧 → 新」顺序，这里倒着插入，让最新一条排在最上面
  const recent = list.slice(-LOG_CAP);
  for (let i = recent.length - 1; i >= 0; i--) appendLogEntry(recent[i], false);
}
function setAutoScroll(v) {
  state.autoScroll = v;
  $('btnPauseScroll').textContent = v ? '暂停滚动' : '恢复滚动';
  $('jumpBottom').classList.toggle('hidden', v);
  if (v) $('logScroll').scrollTop = 0;
}
function onLogScroll() {
  const sc = $('logScroll');
  // 最新日志在顶部：贴着顶部才视为「跟随最新」
  const nearTop = sc.scrollTop < 48;
  if (nearTop !== state.autoScroll) setAutoScroll(nearTop);
}

/* ── Token 弹层（用户账号） ── */
let bannerDismissed = false;
function openTokenModal() {
  const tok = state.status && state.status.token;
  $('tokenHint').textContent = (tok && tok.exp)
    ? `当前过期时间：${String(tok.exp).replace('T', ' ').slice(0, 19)}`
    : '尚未配置 Token（用户账号）';
  $('tokenModal').classList.remove('hidden');
  loadCreds();
}
function closeTokenModal() { $('tokenModal').classList.add('hidden'); }
function flashSection(id) {
  const sec = $(id);
  if (!sec) return;
  sec.classList.remove('flash');
  void sec.offsetWidth;
  sec.classList.add('flash');
}
function updateTokenBanner(s) {
  const show = !!(s && s.token && !s.token.present) && !bannerDismissed;
  $('tokenBanner').classList.toggle('hidden', !show);
}

/* ── 清空所有数据（二次确认，口令「清空」） ── */
const WIPE_PHRASE = '清空';
let wipeBusy = false;          // 请求在途：防重入，且期间不允许关闭弹层
let wipeReloadTimer = null;    // 成功后延迟 reload 的定时器

function wipeCard() { return $('wipeModal').querySelector('.modal-card'); }

/* 输入是否已输入正确的确认口令 */
function wipePhraseOk() {
  const input = $('wipeConfirmInput');
  return !!input && input.value.trim() === WIPE_PHRASE;
}

/* 同步确认按钮的可用状态（进行中态由 doWipe 单独控制） */
function syncWipeState() {
  if (wipeBusy) return;
  const btn = $('wipeConfirm');
  const ok = wipePhraseOk();
  btn.disabled = !ok;
  btn.textContent = '永久清空';
  const note = $('wipeNote');
  note.className = 'wipe-note' + (ok ? ' ok' : '');
  note.textContent = ok
    ? '已输入正确，点「永久清空」立即执行'
    : '需输入『清空』两字，未输入正确前「永久清空」保持禁用。';
}

/* 打开弹层：重置输入与按钮状态，并聚焦输入框 */
function openWipeModal() {
  wipeBusy = false;
  const card = wipeCard();
  if (card) card.classList.remove('is-busy');
  const input = $('wipeConfirmInput');
  input.value = '';
  input.disabled = false;
  $('wipeCancel').disabled = false;
  syncWipeState();
  $('wipeModal').classList.remove('hidden');
  // 直接聚焦输入框（弹层内唯一的文本输入，键盘操作者立刻能输入口令）
  input.focus();
}

/* 关闭弹层：请求在途时忽略（马上会 reload，不允许中途关掉留下半态视图） */
function closeWipeModal() {
  if (wipeBusy) return;
  const modal = $('wipeModal');
  if (!modal || modal.classList.contains('hidden')) return;
  modal.classList.add('hidden');
  $('wipeConfirmInput').value = '';
  syncWipeState();
}

/* 执行清空：成功后 toast + 延迟 reload（让 SSE 快照 / 任务墙 / 账号 / 日志全部回到空态） */
async function doWipe() {
  if (wipeBusy) return;
  if (!wipePhraseOk()) { toast('请输入「清空」以确认清空操作', 'warn'); return; }

  wipeBusy = true;
  const card = wipeCard();
  if (card) card.classList.add('is-busy');
  const input = $('wipeConfirmInput');
  const btn = $('wipeConfirm');
  const cancel = $('wipeCancel');
  input.disabled = true;
  cancel.disabled = true;
  btn.disabled = true;
  btn.textContent = '正在清空…';

  try {
    const r = await api('/api/data/clear', {
      method: 'POST',
      body: JSON.stringify({ confirm: WIPE_PHRASE }),
    });
    const cleared = (r && r.cleared) || {};
    const failed = Array.isArray(cleared.failed) ? cleared.failed : [];
    if (failed.length) {
      toast(`已清空大部分数据，但有 ${failed.length} 项未能删除，正在重新加载…`, 'warn');
    } else {
      toast('所有数据已清空，正在重新加载…', 'ok');
    }
    if (wipeReloadTimer) clearTimeout(wipeReloadTimer);
    wipeReloadTimer = setTimeout(() => location.reload(), 900);
  } catch (err) {
    // 失败：撤掉进行中态，输入仍正确时重新可用（可按按钮重试）
    wipeBusy = false;
    if (card) card.classList.remove('is-busy');
    input.disabled = false;
    cancel.disabled = false;
    btn.textContent = '永久清空';
    syncWipeState();
    toast('清空失败：' + err.message, 'err');
  }
}

/* ── 凭据管理 ── */
function renderCreds(data) {
  const items = (data && data.items) || [];
  state.accounts = items;
  const list = $('credList');
  list.textContent = '';
  const frag = document.createDocumentFragment();
  for (const c of items) {
    const row = el('div', 'cred-row');
    const info = el('div', 'cred-info');
    info.appendChild(el('div', 'cred-name', c.name || '未命名账号'));
    info.appendChild(el('div', 'cred-phone', c.phone_full || c.phone || '—'));
    const metaText = c.last_error ? `最近失败：${c.last_error}`
      : (c.last_ok_at ? `上次成功：${fmtAgo(c.last_ok_at)}` : '从未取过 Token');
    info.appendChild(el('div', 'cred-meta' + (c.last_error ? ' err' : ''), metaText));
    row.appendChild(info);

    const btnFetch = el('button', 'btn btn-xs', '取 Token');
    btnFetch.type = 'button';
    btnFetch.addEventListener('click', () => fetchTokenById(c.id, btnFetch));
    row.appendChild(btnFetch);

    const btnDel = el('button', 'btn btn-xs btn-del', '删除');
    btnDel.type = 'button';
    btnDel.addEventListener('click', () => deleteCred(c.id));
    row.appendChild(btnDel);

    frag.appendChild(row);
  }
  list.appendChild(frag);
  $('credEmpty').classList.toggle('hidden', items.length > 0);
  $('btnFetchAny').disabled = items.length === 0;

  const note = $('credNote');
  if (data && data.dpapi === false) {
    note.className = 'cred-note warn';
    note.textContent = '非 Windows 环境，凭据将明文存储';
  } else {
    note.className = 'cred-note';
    note.textContent = '凭据已加密存储';
  }

  renderLoginAccounts(); // 同步左侧账号列表
}
async function loadCreds() {
  try { renderCreds(await api('/api/credentials')); } catch (_) { /* 静默 */ }
  await loadPlatformByLogin(); // 凭据列表刷新后同步「按登录账号分桶」的平台子账号
}
async function onAddCred(e) {
  e.preventDefault();
  const phone = $('credPhone').value.trim();
  const password = $('credPwd').value;
  const name = $('credName').value.trim();
  if (!/^1[3-9]\d{9}$/.test(phone)) { toast('请输入有效的 11 位手机号', 'warn'); return; }
  if (!password) { toast('请输入密码', 'warn'); return; }
  const btn = $('credAdd');
  btn.disabled = true;
  try {
    const created = await api('/api/credentials', { method: 'POST', body: JSON.stringify({ name, phone, password }) });
    $('credPhone').value = ''; $('credPwd').value = ''; $('credName').value = '';
    const newId = (created && created.id != null && isFinite(Number(created.id))) ? Number(created.id) : null;
    if (newId != null) {
      // 添加账号即自动取该账号自己的 Token + 拉取它名下的平台子账号
      state.expandedAccountId = newId;
      toast('账号已添加，正在获取其平台子账号…');
      await loadCreds();
      await fetchTokenById(newId);
    } else {
      toast('账号已添加');
      await loadCreds();
    }
  } catch (err) {
    toast('添加失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}
/* 只取该账号自己的 Token（引擎暂无轮询 Token 时后端会自动采用） */
async function fetchTokenById(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/credentials/${id}/fetch-token?activate=false`,
      { method: 'POST' });
    if (r && r.ok) {
      // 响应内联的平台子账号先入桶，保证该账号卡片立刻有数据
      applyInlinePlatformAccounts(id, r);
      if (!r.platform_accounts && r.platform_error) {
        toast('平台子账号拉取失败：' + platformErrorText(r.platform_error), 'warn');
      }
      toast(`Token 已获取（${r.phone || '账号'}）`);
      renderLoginAccounts();
      refreshStatus();
      await loadPlatformByLogin(true);
    } else {
      toast('取 Token 失败：' + ((r && r.error) || '未知错误'), 'err');
    }
  } catch (err) {
    toast('取 Token 失败：' + friendlyApiError(err), 'err');
  } finally {
    if (btn) btn.disabled = false;
    await loadCreds();
  }
}
/* 打开原网站并自动登录：后端新开一个 Edge 窗口并从上游取 token 注入。
   接口要拉起浏览器，可能几秒才返回 → 按钮先进 disabled、先给一条 warn toast。 */
async function openSiteForCred(c, btn) {
  if (!c || c.id == null) return;
  if (btn) {
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
  }
  toast('正在打开原网站…', 'warn');
  try {
    const r = await api(`/api/credentials/${c.id}/open-site`, { method: 'POST' });
    // state 为 opened / already_open / starting 都算成功，直接念后端的人话文案
    toast((r && r.message) || '已在浏览器中打开站点并自动登录', 'ok');
    // 后端打开前可能顺手刷新了 token：同步一次顶栏状态（不重绘列表，避免按钮被换掉）
    if (r && r.refreshed) refreshStatus();
  } catch (err) {
    toast('打开原网站失败：' + friendlyApiError(err), 'err');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.removeAttribute('aria-busy');
    }
  }
}
async function onFetchAny() {
  const btn = $('btnFetchAny');
  btn.disabled = true;
  try {
    const r = await api('/api/token/fetch-any', { method: 'POST' });
    if (r && r.ok) {
      toast(`Token 已获取（${r.phone || '账号'}）`);
      refreshStatus();
      await refreshAllPlatformAccounts(true);
    } else {
      const n = (r && r.attempts && r.attempts.length) || 0;
      toast(`全部失败（${n} 个账号）：` + ((r && r.error) || ''), 'err');
    }
  } catch (err) {
    toast('取 Token 失败：' + friendlyApiError(err), 'err');
  } finally {
    btn.disabled = false;
    await loadCreds();
  }
}
async function deleteCred(id) {
  if (!confirm('删除该账号？（需重新添加才能再取 Token）')) return;
  try {
    await api(`/api/credentials/${id}`, { method: 'DELETE' });
    // 该账号的平台子账号桶一并清除
    state.platformByLogin.delete(Number(id));
    if (state.expandedAccountId === Number(id)) state.expandedAccountId = null;
    toast('已删除');
    await loadCreds();
    recomputePlatformAccounts();
    renderLoginAccounts();
  } catch (err) {
    toast('删除失败：' + err.message, 'err');
  }
}

/* ── 平台子账号数据拉取：按登录账号分桶 ── */
/* 按需核对：拉该账号的领取记录，补任务冷却 / 待提交（无任务下发时也能判断） */
async function syncClaimsForCred(credId, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/credentials/${credId}/sync-claims`, { method: 'POST' });
    if (r && r.ok) {
      const n = r.updated != null ? Number(r.updated) : 0;
      const p = r.pending != null ? Number(r.pending) : 0;
      toast(`已核对领取记录：更新 ${n} 条${p ? `，待提交 ${p} 条` : ''}`);
      refreshStatus();
      await loadPlatformByLogin(true);
    } else {
      toast('核对失败：' + ((r && r.error) || '未知错误'), 'err');
    }
  } catch (err) {
    toast('核对失败：' + friendlyApiError(err), 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ── 领取记录：进行中任务 / 所处阶段 ──
   数据源 GET /api/claims（后端已按 cred 分组，stage_label 是后端给的中文，直接用，
   前端不再维护第二份阶段文案）。
   取数时机只有两个：展开账号卡片、点本区块的「刷新」。轮询 / 冷却重绘一律不碰，
   拉到的整包缓存在 state.claims，重绘卡片时直接复用，不会重复打上游。 */
const CLAIM_STAGE_TONE = {
  to_submit: 'warm', reviewing: 'warm',            // 待办：暖色
  retention: 'active', pending_settlement: 'active', // 进行中：中性青
  settled: 'ok',                                    // 完成：绿
  rejected: 'bad', abnormal: 'bad',                 // 失败：红
  expired: 'mute', cancelled: 'mute', unknown: 'mute', // 终态：灰
};

function claimStageTone(stage) {
  return CLAIM_STAGE_TONE[String(stage || '')] || 'mute';
}

/* 摘要里阶段的先后 = 生命周期顺序；中文名一律取后端给的 stage_label，前端不另写一份 */
const CLAIM_STAGE_ORDER = ['to_submit', 'reviewing', 'retention', 'pending_settlement'];

/* 某个登录账号自己的领取记录桶（按 claimed_at 倒序）；上游按账号报的错单独带回 */
function claimsBucket(credId) {
  const id = Number(credId);
  const bucket = state.claims.byCred.get(id) || null;
  const items = ((bucket && Array.isArray(bucket.items)) ? bucket.items : []).slice();
  items.sort((a, b) => (Number(b.claimed_at) || 0) - (Number(a.claimed_at) || 0));
  return {
    loaded: !!state.claims.loaded,
    stats: (bucket && bucket.stats) || {},
    items,
    error: state.claims.errors[String(id)] || '',
  };
}

/* 归一化整包响应：以 by_cred 为准；by_cred 缺失或不全时用顶层 items 自己分组兜底 */
function applyClaims(data) {
  const map = new Map();
  const byCred = (data && data.by_cred) || {};
  for (const [key, val] of Object.entries(byCred)) {
    const id = Number(key);
    if (!isFinite(id)) continue;
    map.set(id, {
      fetchedAt: (val && val.fetched_at != null) ? Number(val.fetched_at) : null,
      stats: (val && val.stats) || {},
      items: ((val && Array.isArray(val.items)) ? val.items : [])
        .filter((x) => x && typeof x === 'object'),
    });
  }
  const raw = (data && Array.isArray(data.items)) ? data.items : [];
  for (const it of raw) {
    if (!it || it.cred_id == null) continue;
    const id = Number(it.cred_id);
    if (!isFinite(id)) continue;
    if (!map.has(id)) map.set(id, { fetchedAt: null, stats: {}, items: [] });
    const bucket = map.get(id);
    const dup = bucket.items.some((x) => String(x.claim_id) === String(it.claim_id)
      && String(x.cred_id) === String(it.cred_id) && String(x.submission_id) === String(it.submission_id));
    if (!dup) bucket.items.push(it);
  }
  state.claims.byCred = map;
  state.claims.errors = (data && data.errors) || {};
  state.claims.fetchedAt = (data && data.fetched_at != null) ? Number(data.fetched_at) : null;
  state.claims.loaded = true;
  state.claims.loadedMs = Date.now();
  state.claims.error = '';
}

/* 拉整包。refresh=true 才会真的去上游（几秒）。非 refresh 的请求会合流，
   15 秒内重复展开直接吃缓存。失败时抛错，由调用方决定提示还是静默。 */
let claimsPromise = null;
async function loadClaims(refresh) {
  if (!refresh) {
    if (claimsPromise) return claimsPromise;
    if (state.claims.loaded && Date.now() - (state.claims.loadedMs || 0) < 15000) return null;
  }
  const p = (async () => {
    const data = await api('/api/claims' + (refresh ? '?refresh=true' : '?refresh=false'));
    applyClaims(data);
    return data;
  })();
  claimsPromise = p;
  try {
    return await p;
  } finally {
    if (claimsPromise === p) claimsPromise = null;
  }
}

/* 领取记录的错文案：后端未就绪时 FastAPI 会回 {detail:"Not Found"} 这类英文原文，
   直接丢给用户不成话；真上游的中文报错（后端给的 message/detail）则原样保留。 */
function claimsErrorText(err) {
  const raw = String((err && err.message) || '');
  if (/^HTTP\s*\d{3}$/i.test(raw) || /^not found$/i.test(raw)
    || /internal server error/i.test(raw) || /bad gateway/i.test(raw)
    || /service unavailable/i.test(raw)) {
    return '暂时无法获取';
  }
  return friendlyApiError(err);
}

/* 展开卡片时补一次：失败只记文案，不弹 toast（进页面不该报错） */
function ensureClaimsLoaded(credId) {
  const id = Number(credId);
  if (!isFinite(id)) return;
  if (state.claims.loaded && !state.claims.error) return;
  loadClaims(false)
    .catch((err) => { state.claims.error = claimsErrorText(err); })
    .then(() => refreshClaimsMount(id));
}

/* 只重绘某账号的领取区块，不重建整张卡片 —— 刷新/加载完不该把 hover 与展开态打掉 */
function refreshClaimsMount(credId) {
  const id = Number(credId);
  const nodes = document.querySelectorAll('[data-claims="' + id + '"]');
  for (const node of nodes) fillClaimsBlock(node, id);
}

/* 时间口径按阶段分两路（实测：上游 expires_at = claimed_at + 24h，是「领取后要提交」的
   截止，不是留存期结束 —— 真实数据里 4 天前、8 天前领取的留存期/待结算任务，expires_at
   都已过去，但任务本身仍然健康。对这两类若照 expires_at 报「已过期」会与徽标自相矛盾）：
   - 待提交 / 审核中：截止时间才是真信号，未到报「剩余」，过了报「已过期」
   - 留存期 / 待结算等：报「已领取多久」，不拿已过去的提交截止当过期
   - 非进行中：报「X前领取」 */
const CLAIM_DEADLINE_STAGES = { to_submit: 1, reviewing: 1 };

function claimTimeText(it, active) {
  const now = Date.now();
  const exp = Number(it && it.expires_at) * 1000;
  const claimed = Number(it && it.claimed_at) * 1000;
  const hasExp = isFinite(exp) && exp > 0;
  const hasClaimed = isFinite(claimed) && claimed > 0;
  const stage = String((it && it.stage) || '');

  if (active !== true) {
    return hasClaimed ? fmtRemain(Math.max(0, now - claimed)) + '前领取' : '';
  }
  if (CLAIM_DEADLINE_STAGES[stage] && hasExp) {
    return exp <= now ? '已过期' : '剩余 ' + fmtRemain(exp - now);
  }
  if (hasClaimed) return '已领取 ' + fmtRemain(Math.max(0, now - claimed));
  if (hasExp) return exp <= now ? '已过期' : '剩余 ' + fmtRemain(exp - now);
  return '';
}

/* 摘要：进行中条数 + 阶段分布。
   阶段分布一律按 items 自己聚合，不用上游 stats —— 两者口径有出入（实测 cred2 那条
   「结算异常」被上游算进了 rejectedCount），照 stats 写会和下面的逐条徽标自相矛盾。 */
function claimSummaryText(bucket, activeCount) {
  const parts = ['进行中 ' + activeCount + ' 条'];
  const groups = new Map();                     // stage -> {n, label}
  for (const it of bucket.items) {
    if (it.active !== true) continue;
    const stage = String(it.stage || 'unknown');
    const g = groups.get(stage) || { n: 0, label: it.stage_label || stage };
    g.n += 1;
    groups.set(stage, g);
  }
  const rank = (stage) => {
    const i = CLAIM_STAGE_ORDER.indexOf(stage);
    return i < 0 ? CLAIM_STAGE_ORDER.length : i;
  };
  const order = [...groups.keys()].sort((a, b) => rank(a) - rank(b));
  for (const stage of order) {
    const g = groups.get(stage);
    parts.push(g.label + ' ' + g.n);
  }
  return parts.join(' · ');
}

/* 兜底对账：明细里一条进行中都没有、上游 stats 却仍记着进行中条数时，
   单独标一行「上游统计」说明，绝不拿 stats 去改列表结论 */
function claimStatsGap(bucket, activeCount) {
  if (activeCount > 0) return '';
  const s = bucket.stats || {};
  const statActive = Number(s.toSubmitCount || 0) + Number(s.reviewingCount || 0)
    + Number(s.retentionCount || 0) + Number(s.pendingSettlementCount || 0);
  if (!(statActive > 0)) return '';
  return '上游统计仍有 ' + statActive + ' 条进行中，但明细未返回';
}

/* 一条领取记录：徽标 + 单行省略的标题，下一行是平台 / 子账号 / 时间 / 浏览 */
function claimRow(it) {
  const row = el('div', 'acct-claim-row');
  const label = it.stage_label || '未知阶段';

  const head = el('div', 'acct-claim-head');
  head.appendChild(el('span', 'acct-claim-stage tone-' + claimStageTone(it.stage), label));
  const name = it.title || '未命名任务';
  const title = el('span', 'acct-claim-title', name);
  title.title = name + '（' + label + '）';
  head.appendChild(title);
  row.appendChild(head);

  const sub = el('div', 'acct-claim-sub');
  const platName = it.platform_name || it.platform || '';
  if (platName) sub.appendChild(el('span', 'acct-claim-sub-plat', platName));
  if (it.account_nickname) {
    const acct = el('span', 'acct-claim-sub-acct', it.account_nickname);
    if (it.platform_account_id != null) {
      acct.appendChild(el('span', 'acct-claim-sub-id', ' #' + it.platform_account_id));
    }
    sub.appendChild(acct);
  } else if (it.platform_account_id != null) {
    sub.appendChild(el('span', 'acct-claim-sub-id', '#' + it.platform_account_id));
  }
  const when = claimTimeText(it, it.active === true);
  if (when) sub.appendChild(el('span', 'acct-claim-sub-time', when));
  const views = Number(it.view_count);
  if (isFinite(views) && views > 0) sub.appendChild(el('span', 'acct-claim-sub-views', '浏览 ' + views));
  if (sub.childNodes.length) row.appendChild(sub);
  return row;
}

/* 进行中记录按平台子账号分组：组序照子账号行的先后；不在子账号列表里的 id 单独成组并标记，
   绝不把这类记录丢掉（上游删号/换号时会遇到） */
function claimGroupsFor(credId, active) {
  const subs = platformSubAccounts(credId);
  const rank = new Map();
  const nameOf = new Map();
  subs.forEach((s, i) => { rank.set(s.id, i); nameOf.set(s.id, s.name); });

  const map = new Map();
  for (const it of active) {
    const raw = it.platform_account_id;
    const pid = (raw != null && isFinite(Number(raw))) ? Number(raw) : null;
    let g = map.get(pid == null ? 'none' : String(pid));
    if (!g) {
      g = { id: pid, known: pid == null ? null : rank.has(pid), name: '', items: [] };
      map.set(pid == null ? 'none' : String(pid), g);
    }
    if (!g.name) g.name = nameOf.get(pid) || String(it.account_nickname || '');
    g.items.push(it);
  }

  const list = [...map.values()];
  list.sort((a, b) => {
    const ra = (a.id != null && rank.has(a.id)) ? rank.get(a.id) : 1e6;
    const rb = (b.id != null && rank.has(b.id)) ? rank.get(b.id) : 1e6;
    return ra - rb;
  });
  return list;
}

/* 一个子账号分组：小标题（#id · 昵称，右侧条数）+ 该组的任务行 */
function claimGroupBlock(g) {
  const wrap = el('div', 'acct-claim-group');

  const head = el('div', 'acct-claim-grp-head');
  const nameText = g.id == null ? '未标记子账号' : ('#' + g.id + (g.name ? ' · ' + g.name : ''));
  const nameEl = el('span', 'acct-claim-grp-name', nameText);
  nameEl.title = nameText;
  head.appendChild(nameEl);
  if (g.known === false) {
    const note = el('span', 'acct-claim-grp-note', '不在子账号列表');
    note.title = '领取记录里这个子账号不在该账号的平台子账号列表中（可能已删号或换号）';
    head.appendChild(note);
  }
  head.appendChild(el('span', 'acct-claim-grp-count', '进行中 ' + g.items.length));
  wrap.appendChild(head);

  const list = el('div', 'acct-claim-list');
  for (const it of g.items) list.appendChild(claimRow(it));
  wrap.appendChild(list);
  return wrap;
}

/* 本卡片的作用域：state.filters.platformAccountId 指向本卡片的子账号时才生效 */
function scopedPlatAccountIdFor(credId, bucket) {
  if (state.filters.platformAccountId == null) return null;
  const want = Number(state.filters.platformAccountId);
  if (!isFinite(want)) return null;
  if (platformSubAccounts(credId).some((s) => s.id === want)) return want;
  // 子账号列表里没有、但领取记录里确实有这个 id（删号/换号）也认
  if (bucket.items.some((x) => Number(x.platform_account_id) === want)) return want;
  return null;
}

/* 填充一个领取记录区块（可在原地反复调用；只有这块 DOM 会被替换） */
function fillClaimsBlock(box, credId) {
  const id = Number(credId);
  box.textContent = '';
  box.dataset.claims = String(id);

  const bucket = claimsBucket(id);
  // 作用域：任务速览筛了本卡片的某个子账号时，本区块跟着切到该子账号视角
  const scopeId = scopedPlatAccountIdFor(id, bucket);
  const activeAll = bucket.items.filter((x) => x.active === true);
  const active = scopeId == null
    ? activeAll
    : activeAll.filter((x) => Number(x.platform_account_id) === scopeId);
  const pastAll = bucket.items.filter((x) => x.active !== true);
  // 已结束记录仍是平铺，但跟着作用域一起收窄，免得在「只看 #id」里冒出别的子账号的记录
  const past = scopeId == null
    ? pastAll
    : pastAll.filter((x) => Number(x.platform_account_id) === scopeId);

  const titleRow = el('div', 'acct-plat-title-row');
  titleRow.appendChild(el('div', 'acct-plat-title', '进行中任务'));
  const btn = el('button', 'mini-btn', '刷新');
  btn.type = 'button';
  btn.title = '向上游重新拉取领取记录（真实请求，可能要几秒）';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    refreshClaimsFor(id, btn);
  });
  titleRow.appendChild(btn);
  if (scopeId != null) {
    const chip = el('span', 'acct-claim-scope', '✕ 只看 #' + scopeId);
    chip.title = '取消筛选，显示该账号全部子账号的进行中任务';
    chip.addEventListener('click', (e) => {
      e.stopPropagation();
      state.filters.platformAccountId = null;
      renderLoginAccounts();
    });
    titleRow.appendChild(chip);
  }
  box.appendChild(titleRow);

  const fatal = state.claims.error;
  const broken = fatal || bucket.error;

  // 摘要口径跟着作用域走：筛了子账号就只说该子账号，不把账号级数字挂上去
  if (bucket.loaded && !broken && (scopeId == null || active.length > 0)) {
    const summary = scopeId == null
      ? claimSummaryText(bucket, active.length)
      : claimSummaryText({ items: active }, active.length) + ' · 该子账号';
    box.appendChild(el('div', 'acct-claim-summary', summary));
  }

  if (broken) {
    // friendlyApiError 对 404/5xx 已经给出「暂时无法获取」，别把同一句话叠两遍
    const text = fatal
      ? ('领取记录暂时无法获取' + (fatal && fatal !== '暂时无法获取' ? '：' + fatal : ''))
      : ('该账号的领取记录暂时无法获取（' + bucket.error + '）');
    box.appendChild(el('div', 'acct-claim-empty err', text));
    return;
  }
  if (!bucket.loaded) {
    box.appendChild(el('div', 'acct-claim-empty', '正在读取领取记录…'));
    return;
  }

  if (!active.length) {
    box.appendChild(el('div', 'acct-claim-empty',
      scopeId == null ? '暂无进行中任务' : '该子账号暂无进行中任务'));
  } else if (scopeId != null) {
    // 单子账号视角：不再套分组标题，列表整平（筛选提示已经在标题行上）
    const list = el('div', 'acct-claim-list');
    for (const it of active) list.appendChild(claimRow(it));
    box.appendChild(list);
  } else {
    const groups = claimGroupsFor(id, active);
    // 只有一个「没标子账号」的组时不必加标题，保持原来的平铺观感
    const grouped = groups.length > 1 || (groups.length === 1 && groups[0].id != null);
    if (grouped) {
      for (const g of groups) box.appendChild(claimGroupBlock(g));
    } else {
      const list = el('div', 'acct-claim-list');
      for (const it of active) list.appendChild(claimRow(it));
      box.appendChild(list);
    }
  }

  // 与上游 stats 的差异只在这里说一句，且明确标成「上游统计」；上游 stats 是账号级的，
  // 因此只在未筛选（账号视角）时才说，别把账号级差异挂到子账号视角上
  if (scopeId == null) {
    const gap = claimStatsGap(bucket, active.length);
    if (gap) box.appendChild(el('div', 'acct-claim-note', gap));
  }

  if (past.length > 0) {
    const open = state.claimsOpen.has(id);
    if (open) {
      const list = el('div', 'acct-claim-list acct-claim-list-past');
      for (const it of past) list.appendChild(claimRow(it));
      box.appendChild(list);
    }
    const more = el('button', 'acct-claim-more', open ? '收起' : '全部领取 ' + past.length);
    more.type = 'button';
    more.title = open ? '只看进行中的任务' : '展开已结束（已结算 / 未通过 / 已过期）的领取记录';
    more.addEventListener('click', (e) => {
      e.stopPropagation();
      if (open) state.claimsOpen.delete(id); else state.claimsOpen.add(id);
      fillClaimsBlock(box, id);
    });
    box.appendChild(more);
  }
}

/* 账号卡片展开区的领取记录外壳（样式与平台子账号面板同源，见 .acct-claims） */
function renderClaimsBlock(c) {
  const box = el('div', 'acct-claims');
  fillClaimsBlock(box, c.id);
  return box;
}

/* 「刷新」：真去上游拉一次。按钮 disabled + 文案切换，回来报条数 */
async function refreshClaimsFor(credId, btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = '刷新中…';
  }
  try {
    await loadClaims(true);
    const n = claimsBucket(credId).items.filter((x) => x.active === true).length;
    toast(n > 0 ? '已刷新 ' + n + ' 条进行中' : '已刷新，暂无进行中任务', 'ok');
  } catch (err) {
    const msg = claimsErrorText(err);
    if (!state.claims.loaded) state.claims.error = msg;   // 有旧数据就留着，只提示
    toast(msg, 'err');
  } finally {
    refreshClaimsMount(Number(credId));
  }
}

/* 全部账号一次性刷新（后端限速；前端串行等待，不并发打爆接口） */
async function refreshAllPlatformAccounts(silent) {
  try {
    const r = await api('/api/platform-accounts/refresh-all', { method: 'POST' });
    const items = (r && r.items) || {};
    const failed = Object.keys(items).filter((k) => items[k] && !items[k].ok).length;
    if (!silent || failed) {
      toast(failed ? `部分账号刷新失败（${failed} 个）` : '已刷新全部账号的平台子账号', failed ? 'warn' : 'ok');
    }
  } catch (err) {
    if (!silent) toast('刷新失败：' + friendlyApiError(err), 'err');
    state.platformUnavailable = true;
  }
  await loadPlatformByLogin(true);
}

/* ── 启动 / 停止 ── */
let acting = false;
async function act(path, okMsg) {
  if (acting) return;
  acting = true;
  try {
    await api(path, { method: 'POST' });
    if (okMsg) toast(okMsg);
    refreshStatus();
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    acting = false;
  }
}

/* ── SSE ── */
function setConnected(v) { $('connBanner').classList.toggle('hidden', v); }
function handleEvent(m) {
  switch (m.type) {
    case 'snapshot':
      if (m.status) renderStatus(m.status);
      if (m.tasks != null) renderTasks((m.tasks && m.tasks.items) || []);
      if (m.history != null) renderHistory(m.history);
      if (m.logs != null) renderLogsSnapshot(m.logs);
      break;
    case 'log': appendLogEntry(m, true); break;
    case 'status': if (m.status) renderStatus(m.status); break;
    case 'tasks': renderTasks(m.items); break;
    default: break;
  }
}
function connectSSE() {
  const es = new EventSource('/api/events');
  es.onopen = () => setConnected(true);
  es.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (_) { return; }
    setConnected(true);
    handleEvent(m);
  };
  es.onerror = () => setConnected(false);
}

/* ── 手机端视图切换（账号 / 任务 / 记录 / 设置） ── */
function setMobileView(view) {
  document.body.dataset.mview = view;
  const nav = $('mobileNav');
  if (nav) {
    nav.querySelectorAll('.mnav-item').forEach((b) => {
      b.classList.toggle('active', b.dataset.view === view);
    });
  }
  if (view === 'settings') {
    // 设置 tab 打开账号配置弹层，同时保持账号视图为底
    openTokenModal();
    document.body.dataset.mview = 'accounts';
    const nav2 = $('mobileNav');
    if (nav2) nav2.querySelectorAll('.mnav-item').forEach((b) => b.classList.toggle('active', b.dataset.view === 'accounts'));
  }
}
function toggleSecondary(force) {
  const btn = $('mobileSecondaryToggle');
  if (!btn) return;
  const open = force != null ? force : document.body.dataset.secondary !== 'open';
  document.body.dataset.secondary = open ? 'open' : 'closed';
  btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  if (open) setMobileView('tasks');
}

/* ── 主题（浅色/深色，localStorage 记忆） ── */
const THEME_KEY = 'sniper-theme';
function applyTheme(t) {
  if (t !== 'light') t = 'dark';
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(THEME_KEY, t); } catch (_) { /* 隐私模式等 */ }
}
function initTheme() {
  let saved = 'dark';
  try { saved = localStorage.getItem(THEME_KEY) || 'dark'; } catch (_) { /* ignore */ }
  document.documentElement.dataset.theme = saved === 'light' ? 'light' : 'dark';
  $('themeToggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    applyTheme(next);
  });
}

/* ── 初始化 ── */
(async function init() {
  initTheme();

  // 手机端默认视图：账号主区
  if (!document.body.dataset.mview) document.body.dataset.mview = 'accounts';

  // 日志区交互
  $('logScroll').addEventListener('scroll', onLogScroll);
  $('btnPauseScroll').addEventListener('click', () => setAutoScroll(!state.autoScroll));
  $('jumpBottom').addEventListener('click', () => setAutoScroll(true));
  $('btnClearLogs').addEventListener('click', () => { $('logList').textContent = ''; });

  // 顶栏动作 / Token（用户账号）
  $('btnStart').addEventListener('click', () => act('/api/start', '狙击已启动'));
  $('btnStop').addEventListener('click', () => act('/api/stop', '已停止'));
  $('tokenClose').addEventListener('click', closeTokenModal);
  $('btnAddAccount').addEventListener('click', openTokenModal);

  // Token 引导横幅（用户账号）
  $('bannerClose').addEventListener('click', () => { bannerDismissed = true; updateTokenBanner(state.status); });
  $('bannerCreds').addEventListener('click', () => { openTokenModal(); flashSection('secCreds'); });

  // 凭据管理（用户账号）
  $('credForm').addEventListener('submit', onAddCred);
  $('btnFetchAny').addEventListener('click', onFetchAny);

  // 清空所有数据（二次确认弹层）
  $('btnWipeData').addEventListener('click', openWipeModal);
  $('wipeClose').addEventListener('click', closeWipeModal);
  $('wipeCancel').addEventListener('click', closeWipeModal);
  $('wipeConfirm').addEventListener('click', doWipe);
  $('wipeConfirmInput').addEventListener('input', syncWipeState);
  // 点击遮罩关闭（点卡片内部不关）
  $('wipeModal').addEventListener('click', (e) => { if (e.target === $('wipeModal')) closeWipeModal(); });

  // 用户账号 Token 弹层：点击遮罩 / Esc 关闭
  $('tokenModal').addEventListener('click', (e) => { if (e.target === $('tokenModal')) closeTokenModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    // 清空确认弹层优先（层级更高、语义更重）
    if (!$('wipeModal').classList.contains('hidden')) { closeWipeModal(); return; }
    if (!$('tokenModal').classList.contains('hidden')) closeTokenModal();
  });

  // 任务筛选：平台子 tab + 仅可领取 + 清除筛选
  $('platformTabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.plat-tab');
    if (!btn) return;
    const code = btn.dataset.platform || 'all';
    state.filters.platform = (code === 'all') ? null : code;
    renderTaskBoard();
    if (btn.scrollIntoView) btn.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  });
  $('filterReset').addEventListener('click', () => {
    // 只清平台筛选：账号作用域属于账号卡片内的「进行中任务」，不在本区语义内
    state.filters.platform = null;
    renderTaskBoard();
  });
  $('onlyClaimable').addEventListener('change', (e) => { state.filters.onlyClaimable = e.target.checked; renderTaskBoard(); });
  // 任务速览「刷新」：手动向上游拉一次任务列表，不用等自动轮询
  $('btnRefreshTasks').addEventListener('click', (e) => refreshTasksManual(e.currentTarget));

  // 活动面板切换
  $('tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab'); if (!btn) return;
    const tab = btn.dataset.tab;
    $('tabs').querySelectorAll('.tab').forEach((c) => c.classList.toggle('active', c === btn));
    $('logPanel').classList.toggle('hidden', tab !== 'logs');
    $('historyPanel').classList.toggle('hidden', tab !== 'history');
  });

  // 手机端底部导航
  const mnav = $('mobileNav');
  if (mnav) {
    mnav.addEventListener('click', (e) => {
      const btn = e.target.closest('.mnav-item');
      if (!btn) return;
      setMobileView(btn.dataset.view);
    });
  }
  // 手机端次级折叠条
  const mstBtn = $('mobileSecondaryToggle');
  if (mstBtn) mstBtn.addEventListener('click', () => toggleSecondary());

  // 初始并行加载
  try {
    const [status, config, historyRes, tasksRes] = await Promise.all([
      api('/api/status'), api('/api/config'),
      api('/api/history'), api('/api/tasks'),
    ]);
    renderStatus(status);
    state.config = config;
    renderHistory((historyRes && historyRes.items) || []);
    renderTasks((tasksRes && tasksRes.items) || []);
  } catch (err) {
    toast('初始化加载失败：' + err.message, 'err');
  }
  await loadCreds();
  // 刷新页面后也立即展示每个登录账号名下的平台子账号
  await loadPlatformByLogin();
  // 默认展开的那张卡片：补一次领取记录（整包只拉一次，之后吃缓存）
  if (state.expandedAccountId != null) ensureClaimsLoaded(state.expandedAccountId);

  connectSSE();

  // SSE 的 status 事件只在启动/停止/配置变化时推送，轮询计数不会主动广播；
  // 运行中每秒拉一次 /api/status，保证空态进度（刷新次数/上次刷新）与顶栏统计实时更新。
  setInterval(() => {
    if (state.status && state.status.state === 'running') refreshStatus();
  }, 1000);

  // 冷却胶囊每秒本地倒计时：到点自动从「冷却中」变回「可抢」
  setInterval(tickCooldownChips, 1000);
})();

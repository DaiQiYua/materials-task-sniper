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
];
const PLATFORM_BY_CODE = new Map(PLATFORMS.map((p) => [p.code, p]));

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

  renderTokenBadge(s.token);
  renderTopStats(s);
  updateTokenBanner(s);
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
  $('tSkipped').textContent = st.skipped != null ? String(st.skipped) : '—';
}

function daysLeft(exp) {
  const t = Date.parse(exp);
  if (!isFinite(t)) return null;
  return Math.max(0, Math.ceil((t - Date.now()) / 86400000));
}
function renderTokenBadge(tok) {
  const badge = $('tokenBadge');
  const label = badge.querySelector('.token-label');
  if (!tok || !tok.present) {
    badge.className = 'token-badge bad';
    label.textContent = 'Token 未配置';
    return;
  }
  if (tok.valid) {
    const d = daysLeft(tok.exp);
    badge.className = 'token-badge good';
    label.textContent = d == null ? 'Token 有效' : `Token 有效 · 余 ${d} 天`;
  } else {
    badge.className = 'token-badge bad';
    label.textContent = 'Token 已过期';
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
    ava.style.background = colorFor(c.phone || c.id);
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
    info.appendChild(metaEl);
    head.appendChild(info);

    // ── 头部右侧操作按钮（不占独立行）──
    const btnFetch = el('button', 'mini-btn primary', '取 Token');
    btnFetch.type = 'button';
    btnFetch.title = '重新获取该账号的 Token 与平台子账号';
    btnFetch.addEventListener('click', (e) => {
      e.stopPropagation();
      fetchTokenById(c.id, btnFetch);
    });
    head.appendChild(btnFetch);

    const btnDel = el('button', 'mini-btn danger', '删除');
    btnDel.type = 'button';
    btnDel.addEventListener('click', (e) => { e.stopPropagation(); deleteCred(c.id); });
    head.appendChild(btnDel);

    const chev = el('span', 'acct-chev', '▾');
    head.appendChild(chev);
    head.addEventListener('click', () => {
      state.expandedAccountId = isOpen ? null : c.id;
      renderLoginAccounts();
    });
    card.appendChild(head);

    // ── 平台概览条（每张卡常驻，避免一头沉）──
    const strip = el('div', 'acct-strip');
    for (const p of PLATFORMS) {
      const pill = el('div', 'plat-pill');
      pill.style.borderColor = p.color;
      const ico = el('span', 'plat-pill-ico', p.short);
      ico.style.background = p.color; ico.style.color = p.text;
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
  $('loginCount').textContent = String(list.length);
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
      || { short: (rec.platformName || '?')[0] || '?', color: 'var(--border-strong)', text: 'var(--text)' };
    ava.style.background = p.color;
    ava.style.color = p.text;
    ava.textContent = p.short;
    card.appendChild(ava);

    const info = el('div', 'pacct-info');
    info.appendChild(el('div', 'pacct-id', '#' + rec.id + (rec.name ? ` · ${rec.name}` : '')));
    const tagRow = el('div', 'pacct-tags');
    if (rec.platform) {
      const plat = PLATFORMS.find((x) => x.code === rec.platform)
        || { short: rec.platformName || rec.platform, name: rec.platformName || rec.platform,
             color: 'var(--border-strong)', text: 'var(--text)' };
      const tag = el('span', 'plat-tag', plat.name || plat.short);
      tag.style.background = plat.color;
      tag.style.color = plat.text;
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

    card.addEventListener('click', () => {
      state.filters.platformAccountId = (selected === rec.id) ? null : rec.id;
      renderLoginAccounts();
      renderTaskBoard();
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

/* 单个账号的平台子账号数量（卡片概览条用） */
function platformCountsFor(credId) {
  const counts = { xiaohongshu: 0, douyin: 0, kuaishou: 0 };
  const bucket = state.platformByLogin.get(Number(credId));
  const items = (bucket && Array.isArray(bucket.items)) ? bucket.items : [];
  for (const a of items) {
    const code = String((a && a.platform) || '').toLowerCase();
    if (code in counts) counts[code] += 1;
  }
  return counts;
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
    btn.style.setProperty('--pt-soft', withAlpha(meta.color, 0.18));
    btn.style.setProperty('--pt-line', withAlpha(meta.color, 0.55));
    const dot = el('span', 'plat-dot');
    dot.style.background = meta.color;
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

/* 「清除筛选」胶囊：平台筛选与左侧账号筛选叠加，命中任一项就显示 */
function renderFilterReset() {
  const btn = $('filterReset');
  if (!btn) return;
  const parts = [];
  if (state.filters.platform) parts.push(platformMetaOf(state.filters.platform).name);
  if (state.filters.platformAccountId != null) parts.push(`账号 #${state.filters.platformAccountId}`);
  if (!parts.length) {
    btn.classList.add('hidden');
    btn.textContent = '清除筛选';
    btn.removeAttribute('title');
    return;
  }
  btn.classList.remove('hidden');
  btn.textContent = '清除：' + parts.join(' + ');
  btn.title = '平台与账号筛选叠加生效；点击同时清除两者';
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
  if (f.platformAccountId != null) {
    const accounts = Array.isArray(elig.accounts) ? elig.accounts : [];
    const hit = accounts.some((a) => (a.id != null ? Number(a.id) : (typeof a === 'number' ? a : NaN)) === f.platformAccountId);
    if (!hit) return false;
  }
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
    stats.textContent = `共 ${all} 个任务被平台 / 账号筛选隐藏 · 点「清除筛选」可看全部`;
    return;
  }
  if (!running) {
    text.textContent = '暂无任务';
    stats.textContent = '启动狙击后开始轮询上游';
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

    const card = el('div', 'task-card ' + cls);

    // 四角瞄准框（仅可领取）
    if (cls === 'claimable') {
      ['c-tl', 'c-tr', 'c-bl', 'c-br'].forEach((c) => card.appendChild(el('span', 'corner ' + c)));
    }

    // 顶部：标题 + 类型
    const top = el('div', 'task-top');
    const titleWrap = el('div');
    titleWrap.appendChild(el('div', 'task-title', t.title != null ? String(t.title) : '—'));
    // 元信息行：平台徽标（可能多个）+ 任务 id
    const metaLine = el('div', 'task-meta');
    const codes = taskPlatformCodes(t);
    if (codes.length) {
      const metas = codes.map((c) => platformMetaOf(c));
      const plats = metas.map((m) => m.name);
      const first = metas[0];
      const badge = el('span', 'task-plat');
      badge.title = (codes.length > 1 ? plats.join(' · ') : plats[0]) + `（${codes.join(' / ')}）`;
      const dot = el('span', 'task-plat-dot');
      dot.style.background = first.color;
      badge.appendChild(dot);
      badge.appendChild(el('span', 'task-plat-name', first.name));
      if (metas.length > 1) badge.appendChild(el('span', 'task-plat-more', '+' + (metas.length - 1)));
      metaLine.appendChild(badge);
    }
    metaLine.appendChild(el('div', 'task-id', `#${t.id != null ? t.id : '—'}`));
    titleWrap.appendChild(metaLine);
    top.appendChild(titleWrap);
    top.appendChild(el('span', 'task-type', t.taskType != null ? String(t.taskType) : '—'));
    card.appendChild(top);

    // 指标：佣金 + 剩余配额
    const metrics = el('div', 'task-metrics');
    const m1 = el('div', 'metric');
    m1.appendChild(el('span', 'metric-v accent', t.baseCommission != null ? String(t.baseCommission) : '—'));
    m1.appendChild(el('span', 'metric-k', '佣金'));
    metrics.appendChild(m1);
    const m2 = el('div', 'metric');
    const qv = el('span', 'metric-v' + (exhausted ? ' faint' : ''), quota == null ? '—' : String(quota));
    m2.appendChild(qv);
    m2.appendChild(el('span', 'metric-k', '剩余配额'));
    metrics.appendChild(m2);
    card.appendChild(metrics);

    // 可领取账号头像行
    const accounts = Array.isArray(elig.accounts) ? elig.accounts : [];
    if (accounts.length) {
      const eligWrap = el('div', 'elig');
      const max = 6;
      accounts.slice(0, max).forEach((a) => {
        const id = a.id != null ? a.id : a;
        const av = el('span', 'elig-ava', initials(a.nickname || a.name || id));
        av.style.background = colorFor(id);
        av.title = `#${id}` + (a.nickname || a.name ? ` · ${a.nickname || a.name}` : '');
        if (f.platformAccountId != null && Number(id) === f.platformAccountId) {
          av.style.outline = '2px solid var(--accent)';
          av.style.outlineOffset = '1px';
        }
        eligWrap.appendChild(av);
      });
      if (accounts.length > max) eligWrap.appendChild(el('span', 'elig-ava more', '+' + (accounts.length - max)));
      eligWrap.appendChild(el('span', 'elig-label', `${accounts.length} 个账号可领`));
      card.appendChild(eligWrap);
    }

    // 底部：领取状态
    const foot = el('div', 'task-foot');
    const badgeEl = el('span', 'claim-badge ' + (can ? 'ok' : 'off'), can ? '可领取' : '不可领');
    let note = exhausted ? '配额已空' : '';
    if (!can && !exhausted) {
      // 上游 claimEligibility 自带 reason/nextClaimableAt，展示出来便于诊断
      const reason = String(elig.reason || '');
      if (reason === 'cooling_down' && elig.nextClaimableAt) {
        note = '冷却至 ' + String(elig.nextClaimableAt).replace('T', ' ').slice(5, 16);
      } else if (reason === 'no_verified_account') {
        note = '无已验证账号';
      } else if (reason) {
        note = reason;
      }
      const accHit = accounts.find((a) => a && a.disabledReason);
      if (accHit) note = String(accHit.disabledReason);
      if (note) badgeEl.title = note;
    }
    foot.appendChild(badgeEl);
    foot.appendChild(el('span', 'elig-label', note));
    card.appendChild(foot);

    frag.appendChild(card);
  }
  grid.appendChild(frag);

  $('boardEmpty').classList.toggle('hidden', visible.length > 0);
  renderBoardEmpty();
  $('boardMeta').textContent = all.length
    ? `共 ${all.length} 个 · 显示 ${visible.length} · ${claimable} 个可领`
    : '';
}

/* ── 领取历史（卡片） ── */
const HISTORY_BADGE = {
  ok: ['ok', '成功'], fail: ['fail', '失败'], conflict: ['conflict', '慢一步'],
  ratelimit: ['ratelimit', '限速'],
};
function renderHistory(items) {
  const list = Array.isArray(items) ? items : [];
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
}

/* ── 日志流 ── */
function appendLogEntry(l, animate) {
  const list = $('logList');
  const level = String(l.level || 'INFO').toUpperCase();
  const line = el('div', 'log-line lv-' + level.toLowerCase() + (animate ? ' fresh' : ''));
  line.appendChild(el('span', 'log-ts', l.ts || ''));
  line.appendChild(el('span', 'log-lv', level));
  line.appendChild(el('span', 'log-msg', l.msg != null ? String(l.msg) : ''));
  list.appendChild(line);
  while (list.children.length > LOG_CAP) list.removeChild(list.firstChild);
  if (state.autoScroll) {
    const sc = $('logScroll');
    sc.scrollTop = sc.scrollHeight;
  } else {
    $('jumpBottom').classList.remove('hidden');
  }
}
function renderLogsSnapshot(logs) {
  const list = Array.isArray(logs) ? logs : [];
  $('logList').textContent = '';
  for (const l of list.slice(-LOG_CAP)) appendLogEntry(l, false);
}
function setAutoScroll(v) {
  state.autoScroll = v;
  $('btnPauseScroll').textContent = v ? '暂停滚动' : '恢复滚动';
  $('jumpBottom').classList.toggle('hidden', v);
  if (v) $('logScroll').scrollTop = $('logScroll').scrollHeight;
}
function onLogScroll() {
  const sc = $('logScroll');
  const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 48;
  if (nearBottom !== state.autoScroll) setAutoScroll(nearBottom);
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

  // 日志区交互
  $('logScroll').addEventListener('scroll', onLogScroll);
  $('btnPauseScroll').addEventListener('click', () => setAutoScroll(!state.autoScroll));
  $('jumpBottom').addEventListener('click', () => setAutoScroll(true));
  $('btnClearLogs').addEventListener('click', () => { $('logList').textContent = ''; });

  // 顶栏动作 / Token（用户账号）
  $('btnStart').addEventListener('click', () => act('/api/start', '狙击已启动'));
  $('btnStop').addEventListener('click', () => act('/api/stop', '已停止'));
  $('tokenBadge').addEventListener('click', openTokenModal);
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
    state.filters.platform = null;
    state.filters.platformAccountId = null;
    renderLoginAccounts();
    renderTaskBoard();
  });
  $('onlyClaimable').addEventListener('change', (e) => { state.filters.onlyClaimable = e.target.checked; renderTaskBoard(); });

  // 活动面板切换
  $('tabs').addEventListener('click', (e) => {
    const btn = e.target.closest('.tab'); if (!btn) return;
    const tab = btn.dataset.tab;
    $('tabs').querySelectorAll('.tab').forEach((c) => c.classList.toggle('active', c === btn));
    $('logPanel').classList.toggle('hidden', tab !== 'logs');
    $('historyPanel').classList.toggle('hidden', tab !== 'history');
  });

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

  connectSSE();

  // SSE 的 status 事件只在启动/停止/配置变化时推送，轮询计数不会主动广播；
  // 运行中每秒拉一次 /api/status，保证空态进度（刷新次数/上次刷新）与顶栏统计实时更新。
  setInterval(() => {
    if (state.status && state.status.state === 'running') refreshStatus();
  }, 1000);

  // 冷却胶囊每秒本地倒计时：到点自动从「冷却中」变回「可抢」
  setInterval(tickCooldownChips, 1000);
})();

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
  platformAccounts: new Map(), // id -> {id,name,claimable,total,isDefault,platform}
  fetchedPlatformAccounts: new Map(), // id -> {id,name,platform} 来自 /api/platform-accounts
  activeLoginId: null,     // 本地跟踪：最后成功取 Token 的登录账号
  expandedAccountId: null, // 手风琴：当前展开查看平台子账号的登录账号
  filters: { search: '', type: 'all', onlyClaimable: false, platformAccountId: null },
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

  // 默认展开「当前」账号（仅切换到新当前账号时自动展开，不覆盖用户手动收起）
  if (state.expandedAccountId == null && state.activeLoginId != null
      && state._lastAutoExpand !== state.activeLoginId) {
    state.expandedAccountId = state.activeLoginId;
    state._lastAutoExpand = state.activeLoginId;
  }

  const frag = document.createDocumentFragment();

  // 统计当前账号各平台子账号数量（概览条用）
  const counts = { xiaohongshu: 0, douyin: 0, kuaishou: 0 };
  for (const rec of state.platformAccounts.values()) {
    const code = (rec.platform || '').toLowerCase();
    if (code in counts) counts[code] += 1;
  }

  for (const c of list) {
    const isActive = state.activeLoginId === c.id;
    const isOpen = state.expandedAccountId === c.id;
    const card = el('div', 'acct accordion' + (isActive ? ' active' : '') + (isOpen ? ' open' : ''));

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
    nameRow.appendChild(document.createTextNode(c.name || '未命名账号'));
    if (isActive) nameRow.appendChild(el('span', 'now-tag', '当前'));
    info.appendChild(nameRow);
    info.appendChild(el('div', 'acct-phone', c.phone || '—'));
    const metaText = c.last_error ? `最近失败：${c.last_error}`
      : (c.last_ok_at ? `上次成功：${fmtAgo(c.last_ok_at)}` : '从未取过 Token');
    info.appendChild(el('div', 'acct-meta' + (c.last_error ? ' err' : ''), metaText));
    head.appendChild(info);

    // ── 头部右侧操作按钮（不占独立行）──
    if (!isActive) {
      const btnFetch = el('button', 'mini-btn primary', '设为当前');
      btnFetch.type = 'button';
      btnFetch.addEventListener('click', (e) => {
        e.stopPropagation();
        fetchTokenById(c.id, btnFetch);
      });
      head.appendChild(btnFetch);
    }
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
      const n = isActive ? counts[p.code] : null;
      const cnt = el('span', 'plat-pill-cnt', n == null ? '—' : String(n));
      if (isActive && n > 0) { pill.classList.add('has'); cnt.textContent = String(n); }
      pill.appendChild(cnt);
      strip.appendChild(pill);
    }
    card.appendChild(strip);

    // ── 展开区：账号详情（点击查看）──
    if (isOpen) {
      card.appendChild(renderAccountDetail(c, isActive));
    }

    frag.appendChild(card);
  }
  box.appendChild(frag);

  $('loginEmpty').classList.toggle('hidden', list.length > 0);
  $('loginCount').textContent = String(list.length);
}

/* 键值行（详情面板用） */
function kvRow(k, v, vClass) {
  const r = el('div', 'kv');
  r.appendChild(el('span', 'kv-k', k));
  r.appendChild(el('span', 'kv-v' + (vClass ? ' ' + vClass : ''), v));
  return r;
}

/* 展开后的账号详情：身份 + Token 状态 + 平台子账号 */
function renderAccountDetail(c, isActive) {
  const detail = el('div', 'acct-detail');

  // 身份 / Token 状态（每个账号都可查看）
  const meta = el('div', 'acct-detail-meta');
  meta.appendChild(kvRow('账号ID', '#' + c.id));
  meta.appendChild(kvRow('备注名', c.name || '—'));
  meta.appendChild(kvRow('手机号', c.phone || '—'));
  if (isActive) {
    const tok = state.status && state.status.token;
    if (tok && tok.present) {
      const d = daysLeft(tok.exp);
      meta.appendChild(kvRow('Token', tok.valid ? `有效 · 余 ${d == null ? '—' : d} 天` : '已过期', tok.valid ? 'ok' : 'err'));
    } else {
      meta.appendChild(kvRow('Token', '未配置', 'err'));
    }
  }
  meta.appendChild(kvRow('上次取 Token', c.last_ok_at ? fmtAgo(c.last_ok_at) : '从未'));
  if (c.last_error) meta.appendChild(kvRow('最近错误', c.last_error, 'err'));
  detail.appendChild(meta);

  // 平台子账号（仅当前账号可取数据）
  if (isActive) {
    const platWrap = el('div', 'acct-plat');
    const titleRow = el('div', 'acct-plat-title-row');
    titleRow.appendChild(el('div', 'acct-plat-title', '平台子账号 · 用于抢任务'));
    const refresh = el('button', 'mini-btn', '刷新');
    refresh.type = 'button';
    refresh.addEventListener('click', (e) => { e.stopPropagation(); autoFetchPlatformAccounts(); });
    titleRow.appendChild(refresh);
    platWrap.appendChild(titleRow);
    const platBox = el('div', 'acct-plat-list');
    platWrap.appendChild(platBox);
    detail.appendChild(platWrap);
    renderPlatformSection(platBox);
  } else {
    detail.appendChild(el('div', 'acct-plat-hint',
      '该账号尚未设为当前，无法读取其平台子账号。点右上「设为当前」后将自动拉取并展示。'));
  }
  return detail;
}

function recomputePlatformAccounts() {
  const map = new Map();
  const defaultId = state.config && state.config.platform_account_id;
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
  // 合并「自动拉取到的平台子账号」（带名称 / 平台归属 / 进行中任务数）
  for (const [id, rec] of state.fetchedPlatformAccounts) {
    const existing = map.get(id) || { id, name: null, total: 0, claimable: 0, activeClaimCount: 0 };
    existing.name = existing.name || rec.name || null;
    if (rec.platform) existing.platform = rec.platform;
    if (rec.platformName) existing.platformName = rec.platformName;
    if (rec.account) existing.account = rec.account;
    if (rec.verifyStatus) existing.verifyStatus = rec.verifyStatus;
    if (rec.activeClaimCount != null) existing.activeClaimCount = rec.activeClaimCount;
    map.set(id, existing);
  }
  for (const rec of map.values()) rec.isDefault = (defaultId != null && Number(defaultId) === rec.id);
  state.platformAccounts = map;
}

/* 把平台子账号渲染进指定容器（用于登录账号卡片内嵌） */
function renderPlatformSection(box) {
  box.textContent = '';
  const frag = document.createDocumentFragment();
  const selected = state.filters.platformAccountId;

  const entries = Array.from(state.platformAccounts.values())
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
    const statTotal = el('span', null, `可抢任务 ${rec.total || 0}`);
    statTotal.title = '当前任务列表中，该账号有资格领取（出现在可领取账号名单）的任务数，随轮询实时变化';
    stats.appendChild(statTotal);
    stats.appendChild(el('span', 'dot', '·'));
    const statActive = el('span', null, `进行中 ${rec.activeClaimCount != null ? rec.activeClaimCount : 0}`);
    statActive.title = '上游平台返回：该账号当前正在执行中的领取任务数（activeClaimCount）';
    stats.appendChild(statActive);
    info.appendChild(stats);
    card.appendChild(info);

    if (rec.isDefault) card.appendChild(el('span', 'pacct-default', '默认领取'));

    const defBtn = el('button', 'mini-btn' + (rec.isDefault ? ' primary' : ''),
      rec.isDefault ? '默认领取' : '设为默认');
    defBtn.type = 'button';
    defBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      setDefaultAccount(rec.isDefault ? null : rec.id);
    });
    card.appendChild(defBtn);

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
    row.appendChild(el('span', 'pacct-empty', '暂无平台子账号数据'));
    const btn = el('button', 'mini-btn', '立即获取');
    btn.type = 'button';
    btn.addEventListener('click', (e) => { e.stopPropagation(); autoFetchPlatformAccounts(); });
    row.appendChild(btn);
    box.appendChild(row);
  }
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
  if (f.type !== 'all' && t.taskType !== f.type) return false;
  const elig = t.claimEligibility || {};
  const can = !!elig.canClaim;
  if (f.onlyClaimable && !can) return false;
  if (f.search) {
    const q = f.search.toLowerCase();
    const title = String(t.title || '').toLowerCase();
    if (!title.includes(q)) return false;
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
  $('boardSpinner').classList.toggle('hidden', !running);

  const text = $('boardEmptyText');
  const stats = $('boardEmptyStats');
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
  const visible = all.filter((t) => taskMatches(t, f));

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
    const idLine = el('div', 'task-id', `#${t.id != null ? t.id : '—'}`);
    titleWrap.appendChild(idLine);
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
    info.appendChild(el('div', 'cred-phone', c.phone || '—'));
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

  // 若 token 已存在但本地未记录「当前」账号，自动选最近成功取过 Token 的账号
  if (state.activeLoginId == null && state.status && state.status.token && state.status.token.present) {
    let best = null;
    for (const c of items) {
      if (!c.last_ok_at) continue;
      if (!best || (c.last_ok_at || 0) > (best.last_ok_at || 0)) best = c;
    }
    if (best) state.activeLoginId = best.id;
  }

  renderLoginAccounts(); // 同步左侧账号列表
}
async function loadCreds() {
  try { renderCreds(await api('/api/credentials')); } catch (_) { /* 静默 */ }
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
    await api('/api/credentials', { method: 'POST', body: JSON.stringify({ name, phone, password }) });
    $('credPhone').value = ''; $('credPwd').value = ''; $('credName').value = '';
    toast('账号已添加');
    await loadCreds();
  } catch (err) {
    toast('添加失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}
async function fetchTokenById(id, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await api(`/api/credentials/${id}/fetch-token`, { method: 'POST' });
    if (r && r.ok) {
      state.activeLoginId = id;
      state.expandedAccountId = id;
      toast(`Token 已获取（${r.phone || '账号'}）· 已设为当前`);
      renderLoginAccounts();
      refreshStatus();
      autoFetchPlatformAccounts();
    } else {
      toast('取 Token 失败：' + ((r && r.error) || '未知错误'), 'err');
    }
  } catch (err) {
    toast('取 Token 失败：' + err.message, 'err');
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
      autoFetchPlatformAccounts();
    } else {
      const n = (r && r.attempts && r.attempts.length) || 0;
      toast(`全部失败（${n} 个账号）：` + ((r && r.error) || ''), 'err');
    }
  } catch (err) {
    toast('取 Token 失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
    await loadCreds();
  }
}
async function deleteCred(id) {
  if (!confirm('删除该账号？（需重新添加才能再取 Token）')) return;
  try {
    await api(`/api/credentials/${id}`, { method: 'DELETE' });
    toast('已删除');
    await loadCreds();
  } catch (err) {
    toast('删除失败：' + err.message, 'err');
  }
}

/* ── 配置：设置默认领取账号 ── */
async function setDefaultAccount(accountId) {
  try {
    const c = await api('/api/config', { method: 'PUT', body: JSON.stringify({ platform_account_id: accountId }) });
    state.config = c;
    recomputePlatformAccounts();
    renderLoginAccounts();
    toast(accountId == null ? '已清除默认领取账号' : `默认领取账号 → #${accountId}`);
  } catch (err) {
    toast('设置失败：' + err.message, 'err');
  }
}

/* ── 平台子账号数据拉取（展示在左侧账号卡片中） ── */
async function fetchPlatformAccountsFor(p) {
  try {
    const data = await api('/api/platform-accounts?platform=' + encodeURIComponent(p.code));
    const items = (data && data.items) || [];
    // 合并到缓存，刷新左栏账号卡片内嵌的平台子账号
    for (const a of items) {
      const id = a.id != null ? Number(a.id) : NaN;
      if (isNaN(id)) continue;
      const prev = state.fetchedPlatformAccounts.get(id);
      state.fetchedPlatformAccounts.set(id, {
        id,
        name: a.name || (prev && prev.name) || null,
        platform: a.platform || p.code,
        platformName: a.platformName || (prev && prev.platformName) || null,
        account: a.account || (prev && prev.account) || null,
        verifyStatus: a.verifyStatus || (prev && prev.verifyStatus) || null,
        total: a.total != null ? a.total : (prev && prev.total),
        claimable: a.claimable != null ? a.claimable : (prev && prev.claimable),
        activeClaimCount: a.activeClaimCount != null ? a.activeClaimCount : (prev && prev.activeClaimCount),
      });
    }
    recomputePlatformAccounts();
    renderLoginAccounts();
  } catch (_) { /* 拉取失败静默，保留已有缓存 */ }
}

async function autoFetchPlatformAccounts() {
  await Promise.all(PLATFORMS.map((p) => fetchPlatformAccountsFor(p)));
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

  // 用户账号 Token 弹层：点击遮罩 / Esc 关闭
  $('tokenModal').addEventListener('click', (e) => { if (e.target === $('tokenModal')) closeTokenModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('tokenModal').classList.contains('hidden')) closeTokenModal();
  });

  // 任务筛选
  $('taskSearch').addEventListener('input', (e) => { state.filters.search = e.target.value.trim(); renderTaskBoard(); });
  $('typeChips').addEventListener('click', (e) => {
    const btn = e.target.closest('.chip'); if (!btn) return;
    state.filters.type = btn.dataset.type;
    $('typeChips').querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === btn));
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
  // Token 已存在时自动拉取平台子账号（否则刷新页面后左栏看不到子平台信息）
  if (state.status && state.status.token && state.status.token.present) {
    autoFetchPlatformAccounts();
  }

  connectSSE();

  // SSE 的 status 事件只在启动/停止/配置变化时推送，轮询计数不会主动广播；
  // 运行中每秒拉一次 /api/status，保证空态进度（刷新次数/上次刷新）与顶栏统计实时更新。
  setInterval(() => {
    if (state.status && state.status.state === 'running') refreshStatus();
  }, 1000);
})();

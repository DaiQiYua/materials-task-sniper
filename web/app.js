/* 任务狙击台 · 前端逻辑
   数据契约：.slim/deepwork/api-contract.md
   安全约定：所有动态文本一律 textContent，禁止 innerHTML 拼接外部数据 */
'use strict';

/* ── 工具 ── */
const $ = (id) => document.getElementById(id);
const LOG_CAP = 500; // 日志 DOM 上限，防内存膨胀

const state = {
  status: null,
  config: null,
  autoScroll: true,
  lastTasksJson: '',
  lastHistoryJson: '',
};

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

/* ── Toast ── */
function toast(msg, kind = 'ok') {
  const t = el('div', 'toast ' + kind, msg);
  $('toastWrap').appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 260);
  }, 3200);
}

/* ── fetch 封装：统一 JSON；非 2xx 抛出后端 message ── */
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

/* ── 状态渲染（顶栏 + 遥测条） ── */
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
  renderTelemetry(s);
  updateTokenBanner(s);
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

function renderTelemetry(s) {
  const st = s.stats || {};
  $('tLastPoll').textContent = fmtClock(s.last_poll_at);
  $('tPolls').textContent = st.polls != null ? String(st.polls) : '—';
  $('tOk').textContent = st.claims_ok != null ? String(st.claims_ok) : '—';
  $('tConflict').textContent = st.claims_conflict != null ? String(st.claims_conflict) : '—';
  $('tFail').textContent = st.claims_fail != null ? String(st.claims_fail) : '—';
  $('tSkipped').textContent = st.skipped != null ? String(st.skipped) : '—';
  $('tBase').textContent = (s.upstream_base || '—').replace(/^https?:\/\//, '');
}

function refreshStatus() {
  api('/api/status').then(renderStatus).catch(() => {});
}

/* ── 任务列表 ── */
function renderTasks(items) {
  const list = Array.isArray(items) ? items : [];
  const json = JSON.stringify(list);
  if (json === state.lastTasksJson) return; // 幂等：内容未变不重绘（保护滚动位置）
  state.lastTasksJson = json;

  const tb = $('taskBody');
  tb.textContent = '';
  const frag = document.createDocumentFragment();
  let claimable = 0;

  for (const t of list) {
    const elig = t.claimEligibility || {};
    const can = !!elig.canClaim;
    if (can) claimable += 1;

    const tr = el('tr', can ? 'claimable' : '');

    const tdTitle = el('td', 'cell-title', t.title != null ? String(t.title) : '—');
    tdTitle.title = `任务 #${t.id != null ? t.id : '—'}`;
    tr.appendChild(tdTitle);

    tr.appendChild(el('td', 'num', t.baseCommission != null ? String(t.baseCommission) : '—'));

    const quota = t.remainingQuota;
    const tdQuota = el('td', 'num', quota == null ? '—' : String(quota));
    if (quota != null && quota <= 0) tdQuota.style.color = 'var(--text-faint)';
    tr.appendChild(tdQuota);

    const tdCan = el('td');
    tdCan.appendChild(el('span', 'badge ' + (can ? 'ok' : 'off'), can ? '可领取' : '不可领'));
    tr.appendChild(tdCan);

    tr.appendChild(el('td', 'cell-type', t.taskType != null ? String(t.taskType) : '—'));
    frag.appendChild(tr);
  }
  tb.appendChild(frag);

  $('taskMeta').textContent = list.length ? `${list.length} 个任务 · ${claimable} 个可领` : '';
  $('taskTable').classList.toggle('hidden', list.length === 0);
  $('taskEmpty').classList.toggle('hidden', list.length > 0);
}

/* ── 领取历史 ── */
const HISTORY_BADGE = {
  ok: ['ok', '成功'],
  fail: ['fail', '失败'],
  conflict: ['conflict', '慢一步'],
  ratelimit: ['ratelimit', '限速'],
};

function renderHistory(items) {
  const list = Array.isArray(items) ? items : [];
  const json = JSON.stringify(list);
  if (json === state.lastHistoryJson) return;
  state.lastHistoryJson = json;

  const tb = $('historyBody');
  tb.textContent = '';
  const frag = document.createDocumentFragment();

  for (const h of list) {
    const tr = el('tr');

    tr.appendChild(el('td', 'cell-time', h.ts != null ? String(h.ts).replace('T', ' ') : '—'));

    const tdTitle = el('td', 'cell-title', h.title != null ? String(h.title) : (h.task_id != null ? `#${h.task_id}` : '—'));
    tdTitle.title = h.task_id != null ? `任务 #${h.task_id}` : '';
    tr.appendChild(tdTitle);

    tr.appendChild(el('td', 'num', h.commission != null ? String(h.commission) : '—'));
    tr.appendChild(el('td', 'num', h.account_id != null ? String(h.account_id) : '—'));

    const map = HISTORY_BADGE[h.status];
    const tdSt = el('td');
    tdSt.appendChild(el('span', 'badge ' + (map ? map[0] : 'off'), map ? map[1] : String(h.status || '—')));
    tr.appendChild(tdSt);

    tr.appendChild(el('td', 'num', h.http != null ? String(h.http) : '—'));

    const tdMsg = el('td', 'cell-msg', h.message != null ? String(h.message) : '');
    tdMsg.title = h.message != null ? String(h.message) : '';
    tr.appendChild(tdMsg);

    frag.appendChild(tr);
  }
  tb.appendChild(frag);

  $('historyMeta').textContent = list.length ? `${list.length} 条记录` : '';
  $('historyTable').classList.toggle('hidden', list.length === 0);
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
  if (v) {
    const sc = $('logScroll');
    sc.scrollTop = sc.scrollHeight;
  }
}

function onLogScroll() {
  const sc = $('logScroll');
  const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 48;
  if (nearBottom !== state.autoScroll) setAutoScroll(nearBottom);
}

/* ── 配置表单 ── */
function fillConfigForm(c) {
  if (!c) return;
  state.config = c;
  $('cfgBase').value = c.base != null ? String(c.base) : '';
  $('cfgPoll').value = c.poll_interval != null ? String(c.poll_interval) : '';
  $('typeStandard').checked = Array.isArray(c.task_types) && c.task_types.includes('standard');
  $('typeLongTerm').checked = Array.isArray(c.task_types) && c.task_types.includes('long_term');
  $('cfgAccount').value = c.platform_account_id != null ? String(c.platform_account_id) : '';
  $('cfgRlMax').value = c.rate_limit_max != null ? String(c.rate_limit_max) : '';
  $('cfgRlWin').value = c.rate_limit_window != null ? String(c.rate_limit_window) : '';
  $('cfgMaxPages').value = c.max_pages != null ? String(c.max_pages) : '';
  $('cfgPageSize').value = c.page_size != null ? String(c.page_size) : '';
  $('cfgTimeout').value = c.http_timeout != null ? String(c.http_timeout) : '';
  $('cfgPolls').value = c.result_polls != null ? String(c.result_polls) : '';
}

function numVal(id, { min = null, isFloat = false } = {}) {
  let v = isFloat ? parseFloat($(id).value) : parseInt($(id).value, 10);
  if (!isFinite(v)) return null;
  if (min != null && v < min) v = min;
  return v;
}

async function onSaveConfig(e) {
  e.preventDefault();
  const types = [];
  if ($('typeStandard').checked) types.push('standard');
  if ($('typeLongTerm').checked) types.push('long_term');
  if (!types.length) { toast('请至少选择一种任务类型', 'warn'); return; }
  const base = $('cfgBase').value.trim();
  if (!base) { toast('上游地址不能为空', 'warn'); return; }

  const body = {
    base,
    poll_interval: numVal('cfgPoll', { min: 0.2, isFloat: true }),
    task_types: types,
    platform_account_id: $('cfgAccount').value.trim() || null,
    rate_limit_max: numVal('cfgRlMax', { min: 1 }),
    rate_limit_window: numVal('cfgRlWin', { min: 0.1, isFloat: true }),
    max_pages: numVal('cfgMaxPages', { min: 1 }),
    page_size: numVal('cfgPageSize', { min: 1 }),
    http_timeout: numVal('cfgTimeout', { min: 1 }),
    result_polls: numVal('cfgPolls', { min: 0 }),
  };

  const btn = $('btnSaveCfg');
  btn.disabled = true;
  try {
    const c = await api('/api/config', { method: 'PUT', body: JSON.stringify(body) });
    fillConfigForm(c); // 后端返回全量，回填规范化
    $('cfgSavedAt').textContent = '已保存 ' + new Date().toTimeString().slice(0, 5);
    toast('配置已保存，热更生效');
  } catch (err) {
    toast('保存失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

/* ── Token 弹层 ── */
function setTokenErr(msg) {
  const e = $('tokenErr');
  e.textContent = msg || '';
  e.classList.toggle('hidden', !msg);
}

function openTokenModal() {
  $('tokenInput').value = '';
  setTokenErr('');
  const tok = state.status && state.status.token;
  $('tokenHint').textContent = (tok && tok.exp)
    ? `当前过期时间：${String(tok.exp).replace('T', ' ').slice(0, 19)}`
    : '尚未配置 Token';
  $('tokenModal').classList.remove('hidden');
  loadCreds();          // 每次打开刷新凭据列表
  syncLoginView();      // 若服务端有活跃会话则恢复进度显示
  $('tokenInput').focus();
}

function flashSection(id) {
  const sec = $(id);
  if (!sec) return;
  sec.classList.remove('flash');
  void sec.offsetWidth; // 重置动画
  sec.classList.add('flash');
}

function closeTokenModal() {
  $('tokenModal').classList.add('hidden');
}

async function onSubmitToken(e) {
  e.preventDefault();
  const token = $('tokenInput').value.trim();
  if (!token) { setTokenErr('请粘贴 Token'); return; }
  const btn = $('tokenSave');
  btn.disabled = true;
  try {
    const r = await api('/api/token', { method: 'PUT', body: JSON.stringify({ token }) });
    toast(r && r.valid ? 'Token 已保存并通过校验' : 'Token 已保存');
    closeTokenModal();
    refreshStatus();
  } catch (err) {
    setTokenErr(err.message); // 400 → 后端 message 原样展示
  } finally {
    btn.disabled = false;
  }
}

/* ── Token 引导横幅 ── */
let bannerDismissed = false;

function updateTokenBanner(s) {
  const show = !!(s && s.token && !s.token.present) && !bannerDismissed;
  $('tokenBanner').classList.toggle('hidden', !show);
}

/* ── 凭据管理（账号密码取 Token） ── */
function fmtAgo(ts) {
  const t = Number(ts);
  if (!isFinite(t) || t <= 0) return '';
  const d = Date.now() / 1000 - t;
  if (d < 60) return '刚刚';
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  return `${Math.floor(d / 86400)} 天前`;
}

function renderCreds(data) {
  const items = (data && data.items) || [];
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
}

async function loadCreds() {
  try { renderCreds(await api('/api/credentials')); } catch (_) { /* 列表刷新失败静默 */ }
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
      toast(`Token 已获取（${r.phone || '账号'}）`);
      refreshStatus();
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

/* ── 浏览器登录取 Token（交互会话） ── */
const LOGIN_PHASE_TEXT = {
  opening: '正在打开浏览器…',
  waiting_login: '等待你完成登录…',
  captured: '已捕获 Token',
  closed: '会话已结束',
  error: '会话出错',
};
let loginTimer = null;

function setLoginView(active) {
  $('loginIdle').classList.toggle('hidden', active);
  $('loginActive').classList.toggle('hidden', !active);
  if (!active) stopLoginPoll();
}

function stopLoginPoll() {
  if (loginTimer) { clearInterval(loginTimer); loginTimer = null; }
}

function onSessionStatus(s) {
  const phase = (s && s.phase) || 'idle';
  if (phase === 'idle') { setLoginView(false); return; }
  setLoginView(true);
  $('loginPhase').textContent = LOGIN_PHASE_TEXT[phase] || phase;
  const elp = Number(s && s.elapsed);
  $('loginElapsed').textContent = isFinite(elp) && elp > 0 ? `已等待 ${Math.round(elp)}s` : '';
  const msg = (s && s.message) || '';
  const msgEl = $('loginMsg');
  msgEl.textContent = msg;
  msgEl.classList.toggle('hidden', !msg);

  if (phase === 'captured') {
    stopLoginPoll();
    toast('已从浏览器捕获 Token');
    closeTokenModal();
    refreshStatus();
  } else if (phase === 'closed' || phase === 'error') {
    stopLoginPoll();
    toast('登录会话：' + (msg || LOGIN_PHASE_TEXT[phase]), phase === 'error' ? 'err' : 'warn');
    setLoginView(false);
  }
}

function startLoginPoll() {
  stopLoginPoll();
  loginTimer = setInterval(async () => {
    try { onSessionStatus(await api('/api/login-session')); } catch (_) { /* 下轮再试 */ }
  }, 1000);
}

async function syncLoginView() {
  try {
    const s = await api('/api/login-session');
    if (s && s.active) { onSessionStatus(s); startLoginPoll(); }
    else setLoginView(false);
  } catch (_) { setLoginView(false); }
}

async function startLoginSession() {
  const btn = $('btnLoginStart');
  btn.disabled = true;
  try {
    await api('/api/login-session', { method: 'POST', body: JSON.stringify({ mode: 'interactive' }) });
    setLoginView(true);
    onSessionStatus({ phase: 'opening', elapsed: 0 });
    startLoginPoll();
  } catch (err) {
    toast('打开登录窗口失败：' + err.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

async function cancelLoginSession() {
  try { await api('/api/login-session', { method: 'DELETE' }); } catch (_) { /* 幂等 */ }
  setLoginView(false);
}

/* ── 启动 / 停止 ── */
let acting = false;
async function act(path, okMsg) {
  if (acting) return;
  acting = true;
  try {
    await api(path, { method: 'POST' });
    if (okMsg) toast(okMsg);
    refreshStatus(); // SSE status 事件也会推，双保险
  } catch (err) {
    toast(err.message, 'err');
  } finally {
    acting = false;
  }
}

/* ── SSE ── */
function setConnected(v) {
  $('connBanner').classList.toggle('hidden', v);
}

function handleEvent(m) {
  switch (m.type) {
    case 'snapshot':
      if (m.status) renderStatus(m.status);
      if (m.tasks != null) renderTasks((m.tasks && m.tasks.items) || []);
      if (m.history != null) renderHistory(m.history);
      if (m.logs != null) renderLogsSnapshot(m.logs);
      break;
    case 'log':
      appendLogEntry(m, true);
      break;
    case 'status':
      if (m.status) renderStatus(m.status);
      break;
    case 'tasks':
      renderTasks(m.items);
      break;
    default:
      break;
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
  // 浏览器原生自动重连；这里只负责 UI 提示
  es.onerror = () => setConnected(false);
}

/* ── 启动：先并行拉全量，再开 SSE（快照兜底防竞态） ── */
(async function init() {
  // 日志区交互
  $('logScroll').addEventListener('scroll', onLogScroll);
  $('btnPauseScroll').addEventListener('click', () => setAutoScroll(!state.autoScroll));
  $('jumpBottom').addEventListener('click', () => setAutoScroll(true));
  $('btnClearLogs').addEventListener('click', () => { $('logList').textContent = ''; });

  // Token 弹层 / 动作
  $('configForm').addEventListener('submit', onSaveConfig);
  $('btnStart').addEventListener('click', () => act('/api/start', '狙击已启动'));
  $('btnStop').addEventListener('click', () => act('/api/stop', '已停止'));
  $('tokenBadge').addEventListener('click', openTokenModal);
  $('tokenClose').addEventListener('click', closeTokenModal);
  $('tokenCancel').addEventListener('click', closeTokenModal);
  $('tokenForm').addEventListener('submit', onSubmitToken);

  // Token 引导横幅
  $('bannerClose').addEventListener('click', () => { bannerDismissed = true; updateTokenBanner(state.status); });
  $('bannerCreds').addEventListener('click', () => { openTokenModal(); flashSection('secCreds'); });
  $('bannerBrowser').addEventListener('click', () => { openTokenModal(); flashSection('secBrowser'); });

  // 凭据管理
  $('credForm').addEventListener('submit', onAddCred);
  $('btnFetchAny').addEventListener('click', onFetchAny);

  // 浏览器登录会话
  $('btnLoginStart').addEventListener('click', startLoginSession);
  $('btnLoginCancel').addEventListener('click', cancelLoginSession);
  $('tokenModal').addEventListener('click', (e) => {
    if (e.target === $('tokenModal')) closeTokenModal();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('tokenModal').classList.contains('hidden')) closeTokenModal();
  });

  // 初始并行加载
  try {
    const [status, config, historyRes, tasksRes] = await Promise.all([
      api('/api/status'),
      api('/api/config'),
      api('/api/history'),
      api('/api/tasks'),
    ]);
    renderStatus(status);
    fillConfigForm(config);
    renderHistory((historyRes && historyRes.items) || []);
    renderTasks((tasksRes && tasksRes.items) || []);
  } catch (err) {
    toast('初始化加载失败：' + err.message, 'err');
  }
  loadCreds();

  connectSSE();
})();

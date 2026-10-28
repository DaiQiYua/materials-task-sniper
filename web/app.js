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
  $('tokenInput').focus();
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

  // 配置 / Token / 动作
  $('configForm').addEventListener('submit', onSaveConfig);
  $('btnStart').addEventListener('click', () => act('/api/start', '狙击已启动'));
  $('btnStop').addEventListener('click', () => act('/api/stop', '已停止'));
  $('tokenBadge').addEventListener('click', openTokenModal);
  $('tokenClose').addEventListener('click', closeTokenModal);
  $('tokenCancel').addEventListener('click', closeTokenModal);
  $('tokenForm').addEventListener('submit', onSubmitToken);
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

  connectSSE();
})();

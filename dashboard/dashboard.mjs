import {createLimitsEditor, errorCode, request, accountView, accountPayload, registrationFields} from './model.mjs';

const root = document.getElementById('dashboard'), $ = id => root.querySelector(`#${id}`);
const {appSDK: sdk, dashboardHost: bridge} = window.$wujie?.props || {};
const pages = new Set(['overview', 'authorization', 'usage', 'account']);
const authNames = {authorized: '已授权', unconnected: '未连接', authorization_required: '需重新授权', unavailable: '暂时无法观测'};
let disposed = false, visible = true, overview = null, timer, reader, readVersion = 0;
let readFailures = 0, readsStopped = false;
let dirty = false, editor, usageCursor = null, usageNext = null, usageReader;
let account = null, editingAccount = null, accountDirty = false, accountBusy = false, currentPage = "overview";
let composing = false;
const events = new AbortController(), unsubscribe = [];
const text = (id, value) => { $(id).textContent = value ?? '—'; };
const time = value => value ? new Date(value).toLocaleString('zh-CN') : '尚无观测';
function size() { if (!disposed) bridge?.resize?.(Math.ceil(root.getBoundingClientRect().height)); }
function notice(value) { text('message', value); $('message').hidden = !value; size(); }
function denied(error) { return error.status === 401 || error.status === 403 || /DENIED|UNAUTHORIZED|SESSION_EXPIRED/.test(errorCode(error)); }
function failure(error, message) {
  if (disposed) return;
  if (denied(error)) {
    overview = null; account = null; editingAccount = null; $('business').hidden = true;
    notice('当前身份无权管理此渠道，或会话已失效。请从应用中心重新打开。');
  } else notice(message);
}
function updateEditor() {
  const state = editor?.snapshot(), pending = state?.pending;
  $('credits').disabled = $('submissions').disabled = Boolean(pending || state?.busy || !editor);
  $('save').disabled = !overview || !editor || Boolean(state?.busy);
  text('save', pending ? '重试原保存请求' : '保存设置');
  bridge?.setDirty?.(dirty || accountDirty || Boolean(pending));
}
function render() {
  if (!overview || disposed) return;
  const auth = overview.authorization, limits = overview.limits;
  $('business').hidden = false;
  text('authState', authNames[auth.status] || '待检查');
  text('authorizationBadge', authNames[auth.status] || '待检查');
  text('authorizationHeading', auth.status === 'authorized' ? '会员已连接到团队' : '检查或恢复会员连接');
  text('workspaceState', auth.workspace_matches ? '与服务器绑定一致' : '等待校验原绑定');
  renderAccount();
  text('generationState', ({paused: '新增生成已受限额暂停', channel_paused: 'Video 已暂停 Higgsfield 新增生成',
    not_verified: 'Video 渠道可用性待核验', authorization_unverified: '会员授权待检查，暂不能确认可生成',
    busy: '当前并发名额已占满', eligible: '可以提交，费用与项目权限仍按任务校验'})[overview.generation_status] || '新增生成状态待核验');
  text('submitted', `${limits.submitted_count} 条`);
  text('limitsReserved', `${limits.reserved_credits} Credits`);
  text('revision', limits.revision);
  const pending = editor?.snapshot().pending;
  if (pending) {
    $('credits').value = pending.credit_limit; $('submissions').value = pending.submission_limit;
    text('saveState', '存在结果尚未确认的保存请求。重试会使用原操作，不会另建修改。');
  } else if (!dirty) {
    $('credits').value = limits.credit_limit; $('submissions').value = limits.submission_limit;
  }
  $('check').disabled = Boolean(auth.pending_check_id);
  text('checkState', auth.pending_check_id ? '服务器正在检查，稍后自动更新。' : auth.error_code ? '暂未确认有效授权，请检查连接。' : '授权检查已完成。');
  $('accountRefresh').disabled = Boolean(auth.pending_check_id || accountBusy);
  updateEditor(); size();
}
async function refresh(automatic = false) {
  if (disposed || !visible || (automatic && readsStopped)) return false;
  if (!automatic) { readFailures = 0; readsStopped = false; }
  clearTimeout(timer); reader?.abort();
  const version = ++readVersion;
  reader = new AbortController();
  try {
    const [value, accounts] = await Promise.all([request(sdk, 'GET', '/overview', undefined, reader.signal), request(sdk, 'POST', '/accounts/search', {}, reader.signal)]);
    if (disposed || !visible || version !== readVersion) return;
    overview = value; account = accounts.items[0] || null; readFailures = 0; notice(''); render(); return true;
  } catch (error) {
    if (disposed || !visible || version !== readVersion) return;
    readFailures++;
    readsStopped = denied(error) || (error.status && error.status !== 429 && error.status < 500) || readFailures > 3;
    failure(error, readsStopped ? '自动更新已停止。已保留上次数据，请点击重新读取。' : '暂时无法更新渠道状态。已显示的数据可能过期，稍后重试。'); return false;
  } finally {
    if (!disposed && visible && version === readVersion && !readsStopped) {
      const delay = readFailures ? Math.min(600000, 5000 * 2 ** (readFailures - 1) * (.85 + Math.random() * .3)) : overview?.authorization.pending_check_id ? 2000 : 30000;
      timer = setTimeout(() => void refresh(true), delay);
    }
  }
}
const stateNames = {completed:'已完成', failed:'失败', acceptance_unknown:'受理待确认', running:'处理中', queued:'排队中'};
const amount = value => value?.amount == null ? '未知' : `${value.amount} ${value.unit}`;
async function loadUsage() {
  if (!visible || disposed) return;
  usageReader?.abort(); const activeReader = usageReader = new AbortController();
  text('usageState', '正在读取日志…'); $('usageNext').disabled = true;
  try {
    const value = await request(sdk, 'POST', '/calls/search', {
      offset: usageCursor || 0, limit: 20, search: $('logSearch').value,
      state: $('logStatus').value, media: $('logMedia').value,
    }, activeReader.signal);
    if (disposed || !visible || usageReader !== activeReader) return;
    $('usage').replaceChildren();
    text('taskCount', `${value.summary.task_count}次`);
    text('budgetTotal', amount(value.summary.budget));
    text('settlementTotal', amount(value.summary.settlement));
    for (const item of value.items) {
      const row = document.createElement('tr');
      const values = [time(item.created_at), item.account_name, '视频／生成', item.model_id,
        stateNames[item.state] || item.state, item.duration_seconds == null ? '—' : `${item.duration_seconds}秒`,
        amount(item.budget), amount(item.settlement)];
      for (const value of values) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
      const task = document.createElement('small'); task.textContent = item.task_id; row.firstChild.append(task);
      $('usage').append(row);
    }
    text('usageState', value.items.length ? '' : '暂无符合条件的调用日志。');
    usageNext = (usageCursor || 0) + value.items.length;
    $('usageNext').disabled = usageNext >= value.total;
    $('usageFirst').disabled = !usageCursor;
  } catch (error) {
    if (disposed || !visible || usageReader !== activeReader) return;
    text('usageState', '读取失败，已保留上次日志。请重新读取。'); failure(error, '日志读取失败。');
  }
  size();
}

function renderAccount() {
  const matches = account && account.name.includes($('accountSearch').value) && (!$('accountFilter').value || account.status === $('accountFilter').value);
  $('accountCard').hidden = !matches; $('accountEmpty').hidden = Boolean(matches);
  if (!account) return;
  const view = accountView(account, overview.authorization);
  for (const [id, key] of Object.entries({accountName:'name',accountStatus:'status',membershipTier:'tier',membershipPrice:'price',membershipCredits:'credits',membershipQuota:'quota',membershipExpiry:'expiry',membershipConcurrency:'concurrency',providerBalance:'providerBalance'})) text(id, view[key]);
  text('balanceStale', overview.authorization.stale && overview.authorization.last_success ? '（上次有效数据）' : '');
  $('disabledNote').hidden = account.status !== 'disabled';
  const label = account.status === 'active' ? '禁用账号' : '启用账号';
  $('accountToggle').title = label; $('accountToggle').setAttribute('aria-label', label);
  for (const id of ['accountEdit', 'accountToggle', 'accountDelete']) $(id).disabled = accountBusy;
  if (currentPage === 'account' && !editingAccount) fillAccount();
}
function fillAccount(force = false) {
  if (!account || (editingAccount && !force)) return;
  editingAccount = structuredClone(account);
  const form = $('accountForm');
  for (const field of registrationFields) form.elements.namedItem(field).value = account.registration?.[field] ?? '';
  form.elements.namedItem('name').value = account.name;
  form.elements.namedItem('status').value = account.status;
  accountDirty = false; updateEditor();
}
async function mutateAccount(method, value, source = account) {
  if (accountBusy || !source || !visible || disposed) return false;
  accountBusy = true; $('accountSave').disabled = true; renderAccount();
  try {
    const result = await request(sdk, method, '/accounts/' + encodeURIComponent(source.account_id), {expected_revision:source.revision, ...value});
    if (disposed) return false;
    account = result.status === 'archived' ? null : result;
    if (method === 'PATCH' && source === editingAccount) { editingAccount = null; accountDirty = false; fillAccount(); }
    notice(method === 'DELETE' ? '账号已删除，历史日志保留。' : '账号已保存。');
    await refresh(); return true;
  } catch (error) {
    if (!disposed) {
      const message = error.status === 409 ? '账号已被修改，请重新读取后核对。已保留填写内容。' : '保存未确认，已保留填写内容。请重新读取账号核对结果。';
      failure(error, message); text('accountSaveState', message);
    }
    return false;
  } finally {
    accountBusy = false;
    if (!disposed) { $('accountSave').disabled = false; renderAccount(); updateEditor(); }
  }
}

function route(value) {
  const incoming = value.path.replace(/^\//, '') || 'overview';
  const selected = ['log', 'logs', 'usage'].includes(incoming) ? 'usage' : ['accounts/new', 'authorization'].includes(incoming) ? 'authorization' : /^accounts\/[^/]+\/edit$/.test(incoming) || incoming === 'limits' ? 'account' : incoming;
  const page = pages.has(selected) ? selected : 'overview';
  currentPage = page;
  root.querySelectorAll('[data-page]').forEach(el => { el.hidden = el.dataset.page !== page && !(page === 'account' && el.dataset.page === 'limits'); });
  root.querySelectorAll('nav [data-route]').forEach(el => {
    if (el.dataset.route === (page === 'usage' ? 'log' : 'overview')) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current');
  });
  $('breadcrumb').hidden = !['authorization', 'account'].includes(page);
  text('breadcrumbPage', page === 'account' ? '编辑账号' : '连接会员');
  if (page === 'account') fillAccount();
  if (page === 'usage') void loadUsage();
  size();
}

if (!sdk?.app || !sdk?.theme || !sdk?.navigation?.get || !bridge) {
  notice('请从 Studio 应用中心打开 Higgsfield。'); $('reload').disabled = true;
} else {
  const context = sdk.context.get();
  const identity = [context.stationId, context.installationId, context.organizationId, context.userId].join(':');
  try {
    editor = createLimitsEditor(sdk, window.sessionStorage, `higgsfield-limits-v1:${identity}`);
  } catch { text('saveState', '无法读取保存恢复记录。请检查浏览器会话存储；限额修改已禁用。'); }
  document.body.dataset.theme = sdk.theme.get();
  unsubscribe.push(sdk.theme.subscribe(theme => { document.body.dataset.theme = theme; }));
  unsubscribe.push(sdk.navigation.subscribe(route));
  unsubscribe.push(sdk.context.subscribe(next => {
    if ([next.stationId, next.installationId, next.organizationId, next.userId].join(':') !== identity) dispose();
  }));
  root.addEventListener('compositionstart', () => { composing = true; }, {signal:events.signal});
  root.addEventListener('compositionend', () => { composing = false; }, {signal:events.signal});
  root.addEventListener('keydown', event => { if (event.key === 'Enter' && (composing || event.isComposing || event.keyCode === 229)) event.preventDefault(); }, {signal:events.signal});
  root.addEventListener('click', async event => {
    const target = event.target.closest('button');
    if (!target || target.disabled || !visible || disposed) return;
    try {
      if (target.dataset.route) await sdk.navigation.request({target: 'app', path: target.dataset.route === 'overview' ? '/' : `/${target.dataset.route}`});
      if (target.hasAttribute('data-video')) await sdk.capabilities.call('navigation.openApp', {appId: 'video-mcp-server', path: '/'});
    } catch (error) { failure(error, '无法打开目标页面，请确认应用已安装且当前身份有访问权限。'); }
  }, {signal: events.signal});
  $('reload').onclick = () => { void refresh(); if (!root.querySelector('[data-page="usage"]').hidden) void loadUsage(); };
  $('connect').onclick = () => { $('authorizationFlow').hidden = false; $('connect').setAttribute('aria-expanded', 'true'); $('flowTitle').focus({preventScroll: true}); size(); };
  $('copy').onclick = async () => {
    try { await navigator.clipboard.writeText($('authorizationRequest').value); text('copyState', '已复制，请交给本地 Skills 执行。'); }
    catch { $('authorizationRequest').focus(); $('authorizationRequest').select(); text('copyState', '已选中，请按 Ctrl+C 复制。'); }
  };
  $('check').onclick = async () => {
    if (!visible || disposed) return;
    $('check').disabled = true;
    try { await request(sdk, 'POST', '/authorization/check', {}); await refresh(); }
    catch (error) { failure(error, errorCode(error) === 'AUTHORIZATION_CHECK_THROTTLED' ? '检查过于频繁，请稍后再试。' : '暂时无法开始检查，请重新读取确认状态。'); }
    finally { if (!disposed) $('check').disabled = Boolean(overview?.authorization.pending_check_id); }
  };
  for (const id of ['credits', 'submissions']) $(id).addEventListener('input', () => { dirty = true; updateEditor(); }, {signal: events.signal});
  $('limitsForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (composing) return;
    if (!editor || !overview || !visible || disposed || editor.snapshot().busy) return;
    const operation = editor.save(overview.limits, $('credits').value, Number($('submissions').value));
    updateEditor();
    try {
      await operation;
      if (disposed) return;
      dirty = false; text('saveState', '设置已保存。'); await refresh();
    } catch (error) {
      if (disposed) return;
      if (errorCode(error) === 'LIMITS_REVISION_CONFLICT') {
        text('saveState', '设置已被其他操作更新。已保留填写值；请核对最新版本后再保存。'); await refresh();
      } else { text('saveState', editor.snapshot().pending ? '结果尚未确认，请重试原保存请求。' : '保存未完成，请检查输入并重试。'); if (denied(error)) failure(error, ''); }
    } finally { if (!disposed) updateEditor(); }
  }, {signal: events.signal});
  $('accountFilters').addEventListener('submit', event => { event.preventDefault(); if (!composing && overview) renderAccount(); }, {signal:events.signal});
  $('accountRefresh').addEventListener('click', () => $('check').click(), {signal:events.signal});
  $('accountEdit').addEventListener('click', async () => {
    if (!account) return;
    editingAccount = null;
    try { await sdk.navigation.request({target:'app', path:`/accounts/${encodeURIComponent(account.account_id)}/edit`}); }
    catch (error) { failure(error, '无法打开编辑页。'); }
  }, {signal:events.signal});
  $('accountToggle').addEventListener('click', () => { if (account) void mutateAccount('PATCH', {status:account.status === 'active' ? 'disabled' : 'active'}); }, {signal:events.signal});
  $('accountDelete').addEventListener('click', () => {
    if (account && window.confirm(`删除“${account.name}”？停止新任务，保留已有任务与日志。`)) void mutateAccount('DELETE', {});
  }, {signal:events.signal});
  $('accountForm').addEventListener('input', () => { accountDirty = true; updateEditor(); }, {signal:events.signal});
  $('accountForm').addEventListener('submit', async event => {
    event.preventDefault();
    if (composing || !editingAccount || accountBusy) return;
    const values = Object.fromEntries(new FormData($('accountForm')));
    if (await mutateAccount('PATCH', accountPayload(editingAccount, values), editingAccount)) text('accountSaveState', '会员资料已保存。');
  }, {signal:events.signal});
  $('accountReload').addEventListener('click', async () => {
    if (accountBusy || (accountDirty && !window.confirm('重新读取会替换尚未保存的会员资料，继续吗？'))) return;
    const loaded = await refresh(); if (loaded && !disposed && account) { fillAccount(true); text('accountSaveState', '已读取当前会员资料。'); }
  }, {signal:events.signal});
  $('logFilters').addEventListener('submit', event => { event.preventDefault(); if (composing) return; usageCursor = null; void loadUsage(); }, {signal: events.signal});
  $('usageFirst').onclick = () => { usageCursor = null; void loadUsage(); };
  $('usageNext').onclick = () => { usageCursor = usageNext; void loadUsage(); };
  const resize = new ResizeObserver(size); resize.observe(root);
  function dispose() {
    if (disposed) return;
    disposed = true; clearTimeout(timer); reader?.abort(); usageReader?.abort(); events.abort(); resize.disconnect();
    for (const stop of unsubscribe) stop();
    for (const id of ['reload', 'connect', 'copy', 'check', 'usageFirst', 'usageNext']) $(id).onclick = null;
    $('business').hidden = true; overview = null;
  }
  unsubscribe.push(sdk.lifecycle.subscribe(event => {
    if (event === 'dispose') { dispose(); return; }
    if (event === 'hidden') { visible = false; ++readVersion; clearTimeout(timer); reader?.abort(); usageReader?.abort(); }
    if (event === 'active') { visible = true; void refresh(true); route(sdk.navigation.get()); }
  }));
  route(sdk.navigation.get()); updateEditor(); bridge.ready(); void refresh();
}

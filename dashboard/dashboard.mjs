import {createLimitsEditor, errorCode, request, accountView, accountPayload, registrationFields, logSettlement, logQuery, observationStale, authorizationCheckState, transactionSummary, transactionNextCursor} from './model.mjs';

const root = document.getElementById('dashboard'), $ = id => root.querySelector(`#${id}`);
const {appSDK: sdk, dashboardHost: bridge} = window.$wujie?.props || {};
const pages = new Set(['overview', 'authorization', 'usage', 'account']);
const authNames = {authorized: '已授权', unconnected: '未连接', authorization_required: '需重新授权', unavailable: '暂时无法观测'};
let disposed = false, visible = true, overview = null, timer, observationTimer, reader, readVersion = 0;
let readFailures = 0, readsStopped = false;
let dirty = false, editor, usageCursor = null, usageNext = null, usageReader, usageTotal = 0, usageFilters = {};
let account = null, editingAccount = null, accountDirty = false, accountBusy = false, currentPage = "overview";
let composing = false;
let logView = 'local', transactions = null, transactionsReader, transactionPage = 0, transactionCursors = [null], transactionNext = null;
let checkStarting = false, activeCheckId = null, checkMessage = '';
let managementDenied = false;
const events = new AbortController(), unsubscribe = [];
const text = (id, value) => { $(id).textContent = value ?? '—'; };
const time = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN') : '尚无观测';
function size() { if (!disposed) bridge?.resize?.(Math.ceil(root.getBoundingClientRect().height)); }
function notice(value) { text('message', value); $('message').hidden = !value; size(); }
function stopObservationTimer() { clearTimeout(observationTimer); observationTimer = undefined; }
function updateObservationFreshness() {
  stopObservationTimer();
  if (!visible || disposed || managementDenied) return;
  const now = Date.now();
  let nextExpiry = null;
  for (const [snapshot, id] of [[overview?.authorization, 'balanceStale'], [transactions, 'transactionsStale']]) {
    const success = snapshot?.last_success, stale = observationStale(snapshot, now);
    text(id, success ? stale ? '过期观测' : '最近有效观测' : '尚无有效观测');
    if (id === 'transactionsStale') text('transactionsFreshness', success && stale ? '当前窗口已过期，请刷新官方数据。' : '');
    if (success && !stale) {
      // The inclusive 300-second window expires at the following millisecond.
      const expires = Date.parse(success.observed_at) + 300001;
      nextExpiry = nextExpiry === null ? expires : Math.min(nextExpiry, expires);
    }
  }
  if (nextExpiry !== null) observationTimer = setTimeout(updateObservationFreshness, Math.max(1, Math.min(2147483647, nextExpiry - now)));
  size();
}
function setVisible(value) {
  visible = value;
  if (visible) updateObservationFreshness(); else stopObservationTimer();
}
function denied(error) { return error.status === 401 || error.status === 403 || /DENIED|UNAUTHORIZED|SESSION_EXPIRED/.test(errorCode(error)); }
function failure(error, message) {
  if (disposed) return;
  if (denied(error)) {
    managementDenied = true; readsStopped = true; ++readVersion; clearTimeout(timer); stopObservationTimer();
    reader?.abort(); usageReader?.abort(); transactionsReader?.abort(); transactions = null;
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
  if (!overview || disposed || managementDenied) return;
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
  const checking = Boolean(checkStarting || activeCheckId || auth.pending_check_id);
  $('check').disabled = $('transactionsRefresh').disabled = checking;
  $('accountRefresh').disabled = checking || accountBusy;
  const checkState = checking ? '正在查询官方余额与流水，等待本次检查结果。' : checkMessage || '点击更新以查询官方余额与流水。';
  text('checkState', checkState); text('refreshState', checkState);
  updateEditor(); size();
}
async function refresh(automatic = false) {
  if (disposed || managementDenied || !visible || (automatic && readsStopped)) return false;
  if (!automatic) { readFailures = 0; readsStopped = false; }
  clearTimeout(timer); reader?.abort();
  const version = ++readVersion;
  reader = new AbortController();
  try {
    const [value, accounts, authorization] = await Promise.all([request(sdk, 'GET', '/overview', undefined, reader.signal), request(sdk, 'POST', '/accounts/search', {}, reader.signal),
      activeCheckId ? request(sdk, 'GET', '/authorization', undefined, reader.signal) : null]);
    if (disposed || managementDenied || !visible || version !== readVersion) return;
    if (authorization) value.authorization = authorization;
    overview = value; account = accounts.items[0] || null;
    if (!activeCheckId && value.authorization.pending_check_id) activeCheckId = value.authorization.pending_check_id;
    if (activeCheckId && value.authorization.check_id === activeCheckId && ['completed', 'failed'].includes(value.authorization.check_status)) {
      const checkedId = activeCheckId;
      const snapshot = await loadTransactions({snapshot:true});
      if (disposed || managementDenied || !visible || version !== readVersion || !overview) return;
      const outcome = authorizationCheckState(value.authorization, checkedId, snapshot);
      checkMessage = outcome.state === 'succeeded' ? '本次查询已完成：余额与流水均已更新。' :
        `本次查询已结束：余额${outcome.balance ? '已更新' : '未更新'}；流水${outcome.transactions === true ? '已更新' : outcome.transactions === false ? '未更新' : '结果未确认'}。${outcome.error_code ? `原因：${outcome.error_code}。` : ''}`;
      activeCheckId = null;
    }
    readFailures = 0; notice(''); render(); return true;
  } catch (error) {
    if (disposed || !visible || version !== readVersion) return;
    readFailures++;
    readsStopped = denied(error) || (error.status && error.status !== 429 && error.status < 500) || readFailures > 3;
    failure(error, readsStopped ? '自动更新已停止。已保留上次数据，请点击重新读取。' : '暂时无法更新渠道状态。已显示的数据可能过期，稍后重试。'); return false;
  } finally {
    if (!disposed && visible && version === readVersion && !readsStopped) {
      const delay = readFailures ? Math.min(600000, 5000 * 2 ** (readFailures - 1) * (.85 + Math.random() * .3)) : activeCheckId || overview?.authorization.pending_check_id ? 2000 : 30000;
      timer = setTimeout(() => void refresh(true), delay);
    }
  }
}
async function startCheck() {
  if (!visible || disposed || managementDenied || checkStarting || activeCheckId || overview?.authorization.pending_check_id) return;
  checkStarting = true; checkMessage = ''; render();
  try {
    const result = await request(sdk, 'POST', '/authorization/check', {}, events.signal);
    if (disposed || managementDenied || !visible) return;
    if (typeof result.check_id !== 'string' || !result.check_id) throw new Error('AUTHORIZATION_CHECK_RESPONSE_INVALID');
    activeCheckId = result.check_id;
    await refresh();
  } catch (error) {
    if (disposed || managementDenied) return;
    checkMessage = errorCode(error) === 'AUTHORIZATION_CHECK_THROTTLED' ? '查询过于频繁，请稍后再试。' : `查询尚未确认：${errorCode(error)}。请重新读取当前状态。`;
    failure(error, checkMessage);
  } finally { checkStarting = false; if (!disposed) render(); }
}

function renderTransactions() {
  if (managementDenied || disposed) return;
  const success = transactions?.last_success, items = success?.items;
  const summary = transactionSummary(items);
  text('transactionsObserved', success?.observed_at ? time(success.observed_at) : '尚无成功记录');
  text('transactionsAttempt', transactions?.observed_at ? time(transactions.observed_at) : '尚无记录');
  text('transactionsScope', transactions?.scope === 'membership_account' ? transactions.workspace_scope === 'personal' ? '当前会员账户 · 个人工作区' : transactions.workspace_scope === 'bound_workspace' ? '当前会员账户 · 已绑定工作区' : '当前会员账户 · 工作区范围未知' : '未知');
  text('transactionsSource', success ? 'Higgsfield 官方会员流水' : '尚无官方成功观测');
  text('transactionCount', summary.count == null ? '未知' : `${summary.count} 条`);
  text('transactionDebit', summary.debit == null ? '未知' : `${summary.debit} Credits`);
  text('transactionCredit', summary.credit == null ? '未知' : `${summary.credit} Credits`);
  text('transactionUnknown', summary.unknown == null ? '未知' : `${summary.unknown} 条`);
  const coverage = success?.coverage, pageSize = coverage?.page_size ?? success?.window_size;
  text('transactionsCoverage', success ? `${coverage?.kind === 'provider_page' ? '供应商返回的当前分页窗口' : '供应商近期流水窗口'}${pageSize == null ? '' : `，请求最多 ${pageSize} 条`}。金额按供应商原值汇总，仅含当前窗口，不能代替完整历史账单；未知收支方向未计入收入和支出。` : '覆盖范围尚未确认。');
  $('transactions').replaceChildren();
  for (const item of items || []) {
    const row = document.createElement('tr');
    const action = ({grant:'积分发放',spend:'积分消费',other:'其他（动作未知）'})[item.action] || '未知';
    const direction = ({credit:'收入',debit:'支出',unknown:'未知'})[item.direction] || '未知';
    for (const value of [item.created_at ? time(item.created_at) : '未知', action, direction, item.credits ?? '未知', '未提供', '未提供，待对账']) {
      const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
    }
    $('transactions').append(row);
  }
  text('transactionsState', transactions?.error_code ? `流水查询失败：${transactions.error_code}。${success ? '保留最后成功窗口，数据已过期。' : '当前流水不可用。'}` :
    transactions?.status === 'unavailable' ? `流水当前不可用。${success ? '保留最后成功窗口，数据已过期。' : '尚无成功窗口。'}` :
    !success ? '尚未取得官方流水，请刷新官方数据。' : items.length ? '当前窗口由官方查询返回；未提供稳定任务关联的记录保持待对账。' : '官方返回的当前窗口没有流水记录。');
  transactionNext = transactionNextCursor(transactions, transactionCursors.slice(0, transactionPage + 1));
  const pagination = success?.pagination;
  text('transactionsPagination', !success || pagination?.status !== 'known' ? '供应商未提供可确认的分页状态，历史范围未知。' :
    pagination.has_more === false ? '供应商已明确当前查询没有下一页；不据此推断账户全部历史范围。' :
    transactionNext ? '下一页使用供应商返回的游标，与本地调用分页独立。' : '尚无可用的下一页游标，无法确认更多历史记录。');
  text('transactionsRange', success ? `供应商第 ${transactionPage + 1} 页 · ${items?.length ?? 0} 条` : '尚未读取');
  $('transactionsPrevious').disabled = transactionPage === 0;
  $('transactionsNext').disabled = !transactionNext;
  updateObservationFreshness();
}

async function loadTransactions({page = transactionPage, snapshot = false} = {}) {
  if (!visible || disposed || managementDenied) return null;
  const cursor = transactionCursors[page];
  if (!snapshot && page > 0 && !cursor) return null;
  transactionsReader?.abort(); const activeReader = transactionsReader = new AbortController();
  for (const id of ['transactionsFirst', 'transactionsPrevious', 'transactionsNext', 'transactionsReload']) $(id).disabled = true;
  text('transactionsState', '正在读取供应商流水…');
  try {
    const result = await request(sdk, 'POST', snapshot ? '/usage/query' : '/transactions/query',
      snapshot ? {page_size:1} : {page_size:20, ...(cursor ? {cursor} : {})}, activeReader.signal);
    if (disposed || managementDenied || !visible || transactionsReader !== activeReader) return null;
    const value = snapshot ? result.provider_transactions : result;
    if (!value || !['observed', 'unavailable', 'not_collected'].includes(value.status) ||
        (value.last_success && !Array.isArray(value.last_success.items))) throw new Error('TRANSACTIONS_RESPONSE_INVALID');
    transactions = value.status !== 'observed' && transactions?.last_success ? {...value,last_success:transactions.last_success,stale:true} : value;
    if (value.status === 'observed') {
      transactionPage = snapshot ? 0 : page;
      transactionCursors = snapshot ? [null] : transactionCursors.slice(0, page + 1);
    }
    renderTransactions();
    return value;
  } catch (error) {
    if (disposed || managementDenied || !visible || transactionsReader !== activeReader) return null;
    if (transactions) transactions = {...transactions,stale:true};
    renderTransactions();
    text('transactionsState', `读取${snapshot ? '最新窗口' : `第 ${page + 1} 页`}失败：${errorCode(error)}。${transactions?.last_success ? `保留上次窗口（第 ${transactionPage + 1} 页），数据已过期。` : '当前流水不可用。'}`);
    if (denied(error)) failure(error, '');
    return null;
  } finally {
    if (!disposed && visible && transactionsReader === activeReader) {
      $('transactionsFirst').disabled = $('transactionsReload').disabled = false;
      $('transactionsPrevious').disabled = transactionPage === 0;
      $('transactionsNext').disabled = !transactionNext;
      size();
    }
  }
}

function selectLogView(value) {
  if (managementDenied || disposed) return;
  logView = value === 'provider' ? 'provider' : 'local';
  $('localLogPanel').hidden = logView !== 'local'; $('providerLogPanel').hidden = logView !== 'provider';
  $('localLogTab').setAttribute('aria-selected', String(logView === 'local'));
  $('providerLogTab').setAttribute('aria-selected', String(logView === 'provider'));
  if (logView === 'provider') void loadTransactions({snapshot:transactions === null}); else void loadUsage();
  size();
}
const stateNames = {completed:'已完成', failed:'失败', acceptance_unknown:'受理待确认', running:'处理中', queued:'排队中'};
const amount = value => value?.amount == null ? '未知' : `${value.amount} ${value.unit}`;
async function loadUsage({offset = usageCursor || 0, applyFilters = false} = {}) {
  if (!visible || disposed || managementDenied) return;
  usageReader?.abort(); const activeReader = usageReader = new AbortController();
  text('usageState', '正在读取日志…');
  for (const id of ['usageFirst', 'usagePrevious', 'usageNext']) $(id).disabled = true;
  try {
    const filters = applyFilters ? logQuery({search:$('logSearch').value, state:$('logStatus').value,
      media:$('logMedia').value, account_id:$('logAccount').value, from:$('logFrom').value, to:$('logTo').value}) : usageFilters;
    const value = await request(sdk, 'POST', '/calls/search', {offset, limit:20, ...filters}, activeReader.signal);
    if (disposed || managementDenied || !visible || usageReader !== activeReader) return;
    usageCursor = offset; usageFilters = filters; usageTotal = value.total;
    const selectedAccount = $('logAccount').value;
    $('logAccount').replaceChildren();
    for (const item of [{account_id:'', name:'全部账号'}, ...(value.accounts || [])]) {
      const option = document.createElement('option'); option.value = item.account_id; option.textContent = item.name; $('logAccount').append(option);
    }
    $('logAccount').value = selectedAccount;
    $('usage').replaceChildren();
    text('taskCount', `${value.summary.task_count}次`);
    text('budgetTotal', amount(value.summary.budget));
    text('settlementTotal', amount(value.summary.settlement));
    for (const item of value.items) {
      const row = document.createElement('tr');
      const values = [time(item.created_at), item.account_name, '视频／生成', item.model_id,
        stateNames[item.state] || item.state, item.duration_seconds == null ? '—' : `${item.duration_seconds}秒`,
        amount(item.budget), logSettlement(item)];
      for (const value of values) { const cell = document.createElement('td'); cell.textContent = value; row.append(cell); }
      const task = document.createElement('small'); task.textContent = item.task_id; row.firstChild.append(task);
      $('usage').append(row);
    }
    text('usageState', value.items.length ? '' : '暂无符合条件的调用日志。');
    usageNext = offset + value.items.length;
    text('usageRange', value.total ? `${value.items.length ? offset + 1 : 0}–${usageNext} / ${value.total}` : '0 / 0');
  } catch (error) {
    if (disposed || managementDenied || !visible || usageReader !== activeReader) return;
    text('usageState', error.message === 'INVALID_DATE_RANGE' ? '结束日期不能早于开始日期，请检查日期。' : '读取失败，已保留上次日志。请重新读取。');
    if (error.message !== 'INVALID_DATE_RANGE') failure(error, '日志读取失败。');
  } finally {
    if (!disposed && visible && usageReader === activeReader) {
      $('usageFirst').disabled = $('usagePrevious').disabled = !usageCursor;
      $('usageNext').disabled = usageNext == null || usageNext >= usageTotal;
      size();
    }
  }
}

function renderAccount() {
  if (managementDenied || disposed) return;
  text('accountCount', `${account ? 1 : 0} 个账号`);
  $('accountCard').hidden = !account; $('accountEmpty').hidden = Boolean(account);
  if (!account) return;
  const view = accountView(account, overview.authorization);
  for (const [id, key] of Object.entries({accountName:'name',accountStatus:'status',membershipTier:'tier',membershipPrice:'price',membershipCredits:'credits',membershipQuota:'quota',membershipExpiry:'expiry',membershipConcurrency:'concurrency',providerBalance:'providerBalance',providerPlan:'providerPlan'})) text(id, view[key]);
  const authorization = overview.authorization, stale = observationStale(authorization);
  text('balanceObserved', authorization.last_success?.observed_at ? time(authorization.last_success.observed_at) : '尚无成功记录');
  text('balanceAttempt', authorization.observed_at ? time(authorization.observed_at) : '尚无记录');
  text('balanceScope', authorization.workspace_matches ? '当前绑定会员工作区 · 已确认' : '当前绑定会员工作区 · 待确认');
  text('balanceState', authorization.error_code ? `余额查询未成功：${authorization.error_code}。${authorization.last_success ? '保留最后成功余额，请刷新官方数据。' : '当前余额不可用。'}` : stale && authorization.last_success ? '余额已超过观测有效期，请刷新官方数据。' : 'Higgsfield 官方余额独立于人工登记、团队限额与本地预留。');
  $('disabledNote').hidden = account.status !== 'disabled';
  const label = account.status === 'active' ? '禁用账号' : '启用账号';
  $('accountToggle').title = label; $('accountToggle').setAttribute('aria-label', label);
  for (const id of ['accountEdit', 'accountToggle', 'accountDelete']) $(id).disabled = accountBusy;
  if (currentPage === 'account' && !editingAccount) fillAccount();
  updateObservationFreshness();
}
function fillAccount(force = false) {
  if (!account || (editingAccount && !force)) return;
  editingAccount = structuredClone(account);
  const form = $('accountForm');
  for (const field of registrationFields) form.elements.namedItem(field).value = account.registration?.[field] ?? '';
  form.elements.namedItem('name').value = account.name;
  accountDirty = false; updateEditor();
}
async function mutateAccount(method, value, source = account) {
  if (accountBusy || !source || !visible || disposed || managementDenied) return false;
  accountBusy = true; $('accountSave').disabled = true; renderAccount();
  try {
    const result = await request(sdk, method, '/accounts/' + encodeURIComponent(source.account_id), {expected_revision:source.revision, ...value});
    if (disposed || managementDenied) return false;
    account = result.status === 'archived' ? null : result;
    if (method === 'PATCH' && source === editingAccount) { editingAccount = null; accountDirty = false; fillAccount(); }
    notice(method === 'DELETE' ? '账号已删除，历史日志保留。' : '账号已保存。');
    await refresh(); return true;
  } catch (error) {
    if (!disposed && !managementDenied) {
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
  const isSubpage = ['authorization', 'account'].includes(page);
  $('appTabs').hidden = isSubpage;
  $('breadcrumb').hidden = false;
  $('breadcrumbHome').disabled = !isSubpage && page === 'overview';
  $('breadcrumbOverviewSep').hidden = !isSubpage;
  $('breadcrumbOverview').hidden = !isSubpage;
  text('breadcrumbPage', page === 'account' ? '编辑账号' : page === 'authorization' ? '连接会员' : page === 'usage' ? '日志' : '概览');
  if (page === 'account') fillAccount();
  if (page === 'usage') selectLogView(logView);
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
      if (target.dataset.logView) selectLogView(target.dataset.logView);
      if (target.hasAttribute('data-video')) await sdk.capabilities.call('navigation.openApp', {appId: 'video-mcp-server', path: '/'});
    } catch (error) { failure(error, '无法打开目标页面，请确认应用已安装且当前身份有访问权限。'); }
  }, {signal: events.signal});
  $('reload').onclick = () => { void refresh(); if (!root.querySelector('[data-page="usage"]').hidden) { if (logView === 'provider') void loadTransactions({snapshot:true}); else void loadUsage(); } };
  $('connect').onclick = () => { $('authorizationFlow').hidden = false; $('connect').setAttribute('aria-expanded', 'true'); $('flowTitle').focus({preventScroll: true}); size(); };
  $('copy').onclick = async () => {
    try { await navigator.clipboard.writeText($('authorizationRequest').value); text('copyState', '已复制，请交给本地 Skills 执行。'); }
    catch { $('authorizationRequest').focus(); $('authorizationRequest').select(); text('copyState', '已选中，请按 Ctrl+C 复制。'); }
  };
  $('check').onclick = () => { void startCheck(); };
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
  $('logFilters').addEventListener('submit', event => { event.preventDefault(); if (composing) return; void loadUsage({offset:0, applyFilters:true}); }, {signal: events.signal});
  $('usageFirst').onclick = () => { void loadUsage({offset:0}); };
  $('usagePrevious').onclick = () => { void loadUsage({offset:Math.max(0, (usageCursor || 0) - 20)}); };
  $('usageNext').onclick = () => { if (usageNext != null) void loadUsage({offset:usageNext}); };
  $('transactionsReload').onclick = () => { void loadTransactions({snapshot:true}); };
  $('transactionsRefresh').onclick = () => { void startCheck(); };
  $('transactionsFirst').onclick = () => { void loadTransactions({page:0}); };
  $('transactionsPrevious').onclick = () => { if (transactionPage > 0) void loadTransactions({page:transactionPage - 1}); };
  $('transactionsNext').onclick = () => {
    if (!transactionNext) return;
    transactionCursors[transactionPage + 1] = transactionNext;
    void loadTransactions({page:transactionPage + 1});
  };
  const resize = new ResizeObserver(size); resize.observe(root);
  function dispose() {
    if (disposed) return;
    disposed = true; clearTimeout(timer); stopObservationTimer(); reader?.abort(); usageReader?.abort(); transactionsReader?.abort(); events.abort(); resize.disconnect();
    for (const stop of unsubscribe) stop();
    for (const id of ['reload', 'connect', 'copy', 'check', 'usageFirst', 'usagePrevious', 'usageNext', 'transactionsReload', 'transactionsRefresh', 'transactionsFirst', 'transactionsPrevious', 'transactionsNext']) $(id).onclick = null;
    $('business').hidden = true; overview = null; transactions = null;
  }
  unsubscribe.push(sdk.lifecycle.subscribe(event => {
    if (event === 'dispose') { dispose(); return; }
    if (event === 'hidden') { setVisible(false); ++readVersion; clearTimeout(timer); reader?.abort(); usageReader?.abort(); transactionsReader?.abort(); }
    if (event === 'active') { setVisible(true); void refresh(true); route(sdk.navigation.get()); }
  }));
  route(sdk.navigation.get()); updateEditor(); bridge.ready(); void refresh();
}

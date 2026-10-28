export function errorCode(error) {
  return error?.code || error?.message || 'REQUEST_UNAVAILABLE';
}

export function observationStale(snapshot, now = Date.now()) {
  const observed = Date.parse(snapshot?.last_success?.observed_at || '');
  const failedWithoutNewObservation = snapshot?.check_status === 'failed' && snapshot?.last_success?.check_id !== snapshot.check_id;
  return snapshot?.stale !== false || failedWithoutNewObservation || !Number.isFinite(observed) || now - observed > 300000;
}

export function authorizationCheckState(authorization, checkId, transactions) {
  if (!checkId) return {terminal:false, state:'not_checked'};
  if (authorization?.check_id !== checkId || !['completed', 'failed'].includes(authorization.check_status)) {
    return {terminal:false, state:'pending'};
  }
  const balance = authorization.status === 'authorized' && !authorization.error_code && authorization.last_success?.check_id === checkId;
  const transactionKnown = transactions?.check_id === checkId;
  const transaction = transactionKnown && transactions.status === 'observed' && !transactions.error_code && transactions.last_success?.check_id === checkId;
  return {terminal:true, state:authorization.check_status === 'failed' ? 'failed' : balance && transaction ? 'succeeded' : 'partial',
    balance, transactions:transactionKnown ? transaction : null, error_code:authorization.check_error_code || null};
}

export function transactionSummary(items) {
  if (!Array.isArray(items)) return {count:null, debit:null, credit:null, unknown:null};
  function total(direction) {
    const amounts = items.filter(item => item.direction === direction).map(item => item.credits);
    const parsed = amounts.map(value => {
      const match = typeof value === 'string' && /^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$/.exec(value);
      if (!match || Math.abs(Number(match[4] || 0)) > 1000) return null;
      const scale = (match[3] || '').length - Number(match[4] || 0);
      const coefficient = BigInt(match[1] + match[2] + (match[3] || ''));
      return {coefficient:scale < 0 ? coefficient * 10n ** BigInt(-scale) : coefficient,scale:Math.max(0,scale)};
    });
    if (parsed.some(value => value === null)) return null;
    const scale = Math.max(0, ...parsed.map(value => value.scale));
    const sum = parsed.reduce((result, value) => result + value.coefficient * 10n ** BigInt(scale - value.scale), 0n);
    const digits = (sum < 0n ? -sum : sum).toString().padStart(scale + 1, '0');
    return (sum < 0n ? '-' : '') + (scale ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}`.replace(/\.?0+$/, '') : digits);
  }
  return {count:items.length, debit:total('debit'), credit:total('credit'),
    unknown:items.filter(item => !['credit', 'debit'].includes(item.direction)).length};
}

export function transactionNextCursor(snapshot, visited = []) {
  const pagination = snapshot?.last_success?.pagination;
  if (snapshot?.status !== 'observed' || pagination?.status !== 'known' || pagination.has_more === false) return null;
  const next = pagination.next_cursor;
  return typeof next === 'string' && next.length > 0 && !visited.includes(next) ? next : null;
}

export function logSettlement(item) {
  if (item.settlement?.amount != null) return `${item.settlement.amount} ${item.settlement.unit}`;
  return ['completed', 'failed', 'rejected', 'cancelled'].includes(item.state) ? '未知' : '待结算';
}

export function logQuery(values) {
  const query = {search:values.search || '', state:values.state || '', media:values.media || '', account_id:values.account_id || ''};
  if (values.from && values.to && values.from > values.to) throw new Error('INVALID_DATE_RANGE');
  for (const [field, value] of [['created_from', values.from], ['created_before', values.to]]) {
    if (!value) continue;
    const date = new Date(`${value}T00:00:00`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value) || !Number.isFinite(date.getTime())) throw new Error('INVALID_DATE_RANGE');
    // Calendar increment includes the entire selected end date across DST changes.
    if (field === 'created_before') date.setDate(date.getDate() + 1);
    query[field] = date.toISOString();
  }
  return query;
}

export async function request(sdk, method, path, body, signal) {
  const input = {method, path};
  if (body !== undefined) input.body = body;
  const response = await sdk.app.request(input, {signal});
  if (response.status < 200 || response.status >= 300) {
    const error = new Error(response.data?.code || 'REQUEST_UNAVAILABLE');
    error.code = error.message;
    error.status = response.status;
    throw error;
  }
  return response.data;
}

export function createLimitsEditor(sdk, storage, key, uuid = () => crypto.randomUUID()) {
  let pending = null, busy = false;
  // A failed storage read must not discard an unknown previously sent write.
  const saved = storage.getItem(key);
  if (saved) {
    pending = JSON.parse(saved);
    if (!valid(pending)) throw new Error('LIMITS_RECOVERY_INVALID');
  }
  function valid(value) {
    return value && Object.keys(value).sort().join(',') ===
      'binding_id,credit_limit,operation_id,revision,submission_limit' &&
      typeof value.binding_id === 'string' && value.binding_id.length > 0 && value.binding_id.length <= 128 &&
      typeof value.operation_id === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value.operation_id) &&
      typeof value.credit_limit === 'string' && /^(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?$/.test(value.credit_limit) &&
      Number.isSafeInteger(value.revision) && value.revision > 0 &&
      Number.isSafeInteger(value.submission_limit) && value.submission_limit >= 0 && value.submission_limit <= 2147483647;
  }
  function snapshot() { return {pending: pending && structuredClone(pending), busy}; }
  async function save(limits, credits, submissions) {
    if (busy) throw new Error('LIMITS_SAVE_BUSY');
    if (!pending) {
      const next = {binding_id: limits.binding_id, revision: limits.revision, credit_limit: credits,
        submission_limit: submissions, operation_id: uuid()};
      if (!valid(next)) throw new Error('INVALID_REQUEST');
      storage.setItem(key, JSON.stringify(next));
      pending = next;
    }
    if (pending.binding_id !== limits.binding_id) throw new Error('LIMITS_BINDING_CHANGED');
    busy = true;
    try {
      const result = await request(sdk, 'PUT', '/limits', structuredClone(pending));
      storage.removeItem(key);
      pending = null;
      return result;
    } catch (error) {
      // Only a definite rejection retires the operation. A cancelled request or
      // missing response is retried with exactly the same payload and identity.
      if ([409, 422].includes(error.status) || ['LIMITS_REVISION_CONFLICT', 'IDEMPOTENCY_CONFLICT', 'INVALID_REQUEST'].includes(errorCode(error))) {
        storage.removeItem(key);
        pending = null;
      }
      throw error;
    } finally { busy = false; }
  }
  return {snapshot, save};
}

// Manual monthly entitlement and the provider's total balance have different scopes.
export function accountView(account, authorization = {}) {
  const r = account.registration || {}, known = value => value !== null && value !== undefined && value !== '';
  return {
    name: account.name, status: account.status === 'active' ? '启用' : account.status === 'disabled' ? '禁用' : '已删除',
    tier: r.membership_tier || '未登记',
    price: known(r.monthly_price_usd) ? `US$${r.monthly_price_usd}／月` : '',
    credits: known(r.remaining_credits) ? `${r.remaining_credits}${known(r.monthly_credits) ? ' / ' + r.monthly_credits : ''} Credits` : '未登记',
    quota: !known(r.remaining_credits) && known(r.monthly_credits) ? `月度配额 ${r.monthly_credits} Credits` : '',
    expiry: r.membership_expiry || '未提供',
    concurrency: `${r.video_concurrency ?? '未登记'} 视频 · ${r.image_concurrency ?? '未登记'} 图片`,
    providerBalance: known(authorization.last_success?.balance_credits) ? `${authorization.last_success.balance_credits} Credits` : '未知',
    providerPlan: authorization.last_success?.plan || '套餐未知',
  };
}
export const registrationFields = ['membership_tier', 'membership_expiry', 'monthly_price_usd',
  'remaining_credits', 'monthly_credits', 'video_concurrency', 'image_concurrency', 'note'];
export function accountPayload(account, values) {
  const registration = {};
  for (const field of registrationFields) {
    const value = values[field] ?? '';
    registration[field] = ['video_concurrency', 'image_concurrency'].includes(field)
      ? (value === '' ? null : Number(value))
      : ['monthly_price_usd', 'remaining_credits', 'monthly_credits'].includes(field) ? (value === '' ? null : value) : value;
  }
  return {expected_revision: account.revision, name: values.name.trim() || 'Higgsfield 账号', status: values.status || account.status, registration};
}

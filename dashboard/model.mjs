export function errorCode(error) {
  return error?.code || error?.message || 'REQUEST_UNAVAILABLE';
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
    tier: r.membership_tier || authorization.last_success?.plan || '未登记',
    price: known(r.monthly_price_usd) ? `US$${r.monthly_price_usd}／月` : '',
    credits: known(r.remaining_credits) ? `${r.remaining_credits}${known(r.monthly_credits) ? ' / ' + r.monthly_credits : ''} Credits` : '未登记',
    quota: !known(r.remaining_credits) && known(r.monthly_credits) ? `月度配额 ${r.monthly_credits} Credits` : '',
    expiry: r.membership_expiry || '未提供',
    concurrency: `${r.video_concurrency ?? '未登记'} 视频 · ${r.image_concurrency ?? '未登记'} 图片`,
    providerBalance: authorization.last_success ? `${authorization.last_success.balance_credits} Credits` : '未知',
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
  return {expected_revision: account.revision, name: values.name.trim() || 'Higgsfield 账号', status: values.status, registration};
}

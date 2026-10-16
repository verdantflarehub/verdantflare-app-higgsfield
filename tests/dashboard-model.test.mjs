import test from 'node:test';
import assert from 'node:assert/strict';
import {createLimitsEditor} from '../dashboard/model.mjs';

function fixture(call) {
  const values = new Map(), calls = [];
  const storage = {getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
  const sdk = {app: {request: async input => { calls.push(structuredClone(input)); return call(input); }}};
  const make = () => createLimitsEditor(sdk, storage, 'user-installation', () => 'operation-one');
  return {make, storage, values, calls};
}
const limits = {binding_id: 'team', revision: 1};

test('an unknown response survives page recreation and retries identical operation', async () => {
  let accepted = false;
  const f = fixture(async input => {
    if (!accepted) { accepted = true; throw new Error('connection lost'); }
    return {status: 200, data: {...input.body, revision: 2}};
  });
  await assert.rejects(f.make().save(limits, '4.5', 2), /connection lost/);
  const reopened = f.make();
  assert.equal(reopened.snapshot().pending.credit_limit, '4.5');
  await reopened.save(limits, '999', 999);
  assert.deepEqual(f.calls[0], f.calls[1]);
  assert.equal(f.values.size, 0);
});

test('definite conflict clears pending operation and requires refreshed revision', async () => {
  let conflict = true;
  const f = fixture(async input => conflict
    ? {status: 409, data: {code: 'LIMITS_REVISION_CONFLICT'}}
    : {status: 200, data: {...input.body, revision: input.body.revision + 1}});
  const editor = f.make();
  await assert.rejects(editor.save(limits, '4', 2), /LIMITS_REVISION_CONFLICT/);
  assert.equal(editor.snapshot().pending, null);
  conflict = false;
  await editor.save({...limits, revision: 3}, '4', 2);
  assert.equal(f.calls[1].body.revision, 3);
});

test('recovered request cannot write a different membership binding', async () => {
  const f = fixture(async () => { throw new Error('timeout'); });
  await assert.rejects(f.make().save(limits, '4', 2));
  await assert.rejects(f.make().save({...limits, binding_id: 'another'}, '4', 2), /BINDING_CHANGED/);
  assert.equal(f.calls.length, 1);
});

test('storage failure prevents write and malformed recovery is not discarded', async () => {
  let sent = false;
  const sdk = {app: {request: async () => { sent = true; }}};
  const editor = createLimitsEditor(sdk, {getItem: () => null, setItem: () => { throw new Error('storage blocked'); }},
    'key', () => 'op');
  await assert.rejects(editor.save(limits, '4', 2), /storage blocked/);
  assert.equal(sent, false);
  assert.throws(() => createLimitsEditor(sdk, {getItem: () => '{}'}, 'key'), /RECOVERY_INVALID/);
});

test('double click cannot submit another write while the original is in progress', async () => {
  let finish;
  const f = fixture(() => new Promise(resolve => { finish = resolve; }));
  const editor = f.make(), first = editor.save(limits, '4', 2);
  await assert.rejects(editor.save(limits, '4', 2), /SAVE_BUSY/);
  finish({status: 200, data: {revision: 2}});
  await first;
  assert.equal(f.calls.length, 1);
});

test('invalid amount and submission count never reach Studio', async () => {
  const f = fixture(async () => { throw new Error('should not send'); });
  for (const [credit, count] of [['NaN', 2], ['-1', 2], ['1e3', 2], ['0.0000001', 2], ['1', 1.5], ['1', -1]]) {
    await assert.rejects(f.make().save(limits, credit, count), /INVALID_REQUEST/);
  }
  assert.equal(f.calls.length, 0);
});

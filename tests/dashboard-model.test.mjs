import test from 'node:test';
import assert from 'node:assert/strict';
import {authorizationCheckState, observationStale} from '../dashboard/model.mjs';



test('refresh result requires this check to finish and both independent observations to succeed', () => {
  const auth = {check_id:'current', check_status:'completed', status:'authorized', error_code:null,last_success:{check_id:'current'}};
  const transactions = {check_id:'current', status:'observed', error_code:null,last_success:{check_id:'current'}};
  assert.equal(authorizationCheckState({...auth,check_status:'pending'},'current',transactions).terminal,false);
  assert.equal(authorizationCheckState({...auth,check_id:'previous'},'current',transactions).terminal,false);
  assert.equal(authorizationCheckState(auth,'current',transactions).state,'succeeded');
  const oldTransactions = authorizationCheckState(auth,'current',{...transactions,check_id:'previous'});
  assert.equal(oldTransactions.state,'partial'); assert.equal(oldTransactions.transactions,null);
  const failedTransactions = authorizationCheckState(auth,'current',{...transactions,status:'unavailable',error_code:'PROVIDER_TIMEOUT'});
  assert.equal(failedTransactions.balance,true); assert.equal(failedTransactions.transactions,false);
  assert.equal(failedTransactions.state,'partial');
  assert.equal(authorizationCheckState({...auth,check_status:'failed',check_error_code:'STORAGE_UNAVAILABLE'},'current',transactions).state,'failed');
  assert.equal(authorizationCheckState({...auth,last_success:{check_id:'previous'},check_status:'failed',check_error_code:'STORAGE_UNAVAILABLE'},'current',transactions).balance,false);
});

test('staleness follows last successful observation and never the most recent failed attempt', () => {
  const now = Date.parse('2026-10-28T06:10:00Z');
  const snapshot = {stale:false,observed_at:'2026-10-28T06:10:00Z',last_success:{observed_at:'2026-10-28T06:00:00Z'}};
  assert.equal(observationStale(snapshot,now),true);
  assert.equal(observationStale({...snapshot,last_success:{observed_at:'2026-10-28T06:05:00Z'}},now),false);
  assert.equal(observationStale({...snapshot,last_success:{observed_at:'2026-10-28T06:05:01Z'}},now),false);
  assert.equal(observationStale({...snapshot,stale:true,last_success:{observed_at:'2026-10-28T06:09:59Z'}},now),true);
  assert.equal(observationStale({stale:false,last_success:null},now),true);
  assert.equal(observationStale({stale:false,check_id:'new',check_status:'failed',last_success:{check_id:'old',observed_at:'2026-10-28T06:09:59Z'}},now),true);
});

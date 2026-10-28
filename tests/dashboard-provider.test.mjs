import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as model from '../dashboard/model.mjs';

const observedAt = new Date().toISOString();
const item = {action:'spend',direction:'debit',credits:'0.1',created_at:observedAt,source:'provider_transactions'};
const snapshot = (items = [item], extra = {}) => ({status:'observed',check_id:'check-one',error_code:null,observed_at:observedAt,stale:false,
  binding_id:'team',scope:'membership_account',workspace_scope:'personal',
  last_success:{check_id:'check-one',observed_at:observedAt,items,window_size:20,
    pagination:{status:'known',next_cursor:null,has_more:false},coverage:{kind:'recent_window',page_size:20}},...extra});
const authorization = {status:'authorized',check_id:'check-one',check_status:'completed',check_error_code:null,error_code:null,
  stale:false,workspace_matches:true,observed_at:observedAt,last_success:{check_id:'check-one',balance_credits:'0',plan:'Plus',observed_at:observedAt}};
const overview = {authorization,limits:{binding_id:'team',revision:1,credit_limit:'50',submission_limit:10,reserved_credits:'0',submitted_count:0}};
const account = {account_id:'team',name:'会员',status:'active',revision:1,registration:{}};

function fixture(respond, clock = null) {
  const nodes = new Map(), calls = [];
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {value:'',textContent:'',hidden:false,disabled:false,children:[],attributes:{},
      append(child){this.children.push(child);},replaceChildren(){this.children=[];},
      setAttribute(key,value){this.attributes[key]=value;},
      get firstChild(){return this.children[0];},getBoundingClientRect(){return {height:400};}});
    return nodes.get(id);
  }
  const root = {querySelector:selector=>node(selector.slice(1)),getBoundingClientRect:()=>({height:400})};
  const context = vm.createContext({...model,AbortController,structuredClone,
    ...(clock ? {Date:clock.Date,setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout} : {setTimeout:()=>0,clearTimeout(){}}),
    document:{getElementById:()=>root,createElement:()=>({textContent:'',children:[],append(child){this.children.push(child);},get firstChild(){return this.children[0];}})},
    window:{$wujie:{props:{appSDK:{app:{request:async input=>{
      calls.push(structuredClone(input)); const result = await respond(input);
      return typeof result?.status === 'number' ? result : {status:200,data:result};
    }}},dashboardHost:{resize(){},setDirty(){}}}}}});
  const source = readFileSync(new URL('../dashboard/dashboard.mjs',import.meta.url),'utf8');
  vm.runInContext(source.replace(/^import .*?;\s*/,'').split('if (!sdk?.app')[0],context);
  context.initial = {overview:structuredClone(overview),account:structuredClone(account)};
  vm.runInContext('overview = initial.overview; account = initial.account;',context);
  return {node,calls,run:source=>vm.runInContext(source,context)};
}

function fakeClock(start) {
  let now = start, sequence = 0;
  const timers = new Map();
  return {Date:class extends Date {static now(){return now;}},
    setTimeout(callback,delay){const id=++sequence;timers.set(id,{callback,due:now+delay});return id;},
    clearTimeout(id){timers.delete(id);},
    get pending(){return timers.size;},
    advance(milliseconds){
      const target=now+milliseconds;
      for (;;) {
        const next=[...timers].filter(([,timer])=>timer.due<=target).sort((a,b)=>a[1].due-b[1].due)[0];
        if(!next) break;
        timers.delete(next[0]); now=next[1].due; next[1].callback();
      }
      now=target;
    }};
}

test('provider window uses exact decimal direction totals and never infers direction from an amount', () => {
  const rows = [item,{...item,credits:'0.2'},{...item,action:'grant',direction:'credit',credits:'2'},
    {...item,action:'other',direction:'unknown',credits:'7'}];
  assert.deepEqual(model.transactionSummary(rows),{count:4,debit:'0.3',credit:'2',unknown:1});
  assert.deepEqual(model.transactionSummary([]),{count:0,debit:'0',credit:'0',unknown:0});
  assert.deepEqual(model.transactionSummary(null),{count:null,debit:null,credit:null,unknown:null});
  assert.equal(model.transactionSummary([{...item,credits:null}]).debit,null);
  assert.equal(model.transactionSummary([{...item,credits:'-2'}]).debit,'-2');
  assert.equal(model.transactionSummary([{...item,credits:'1E-7'},{...item,credits:'-0.0000002'}]).debit,'-0.0000001');
});

test('supplier snapshot renders independently from local journal and keeps unknown fields visible', async () => {
  const rows = [item,{...item,action:'other',direction:'unknown',credits:null,created_at:null}];
  const f = fixture(()=>({items:[{task_id:'not-a-supplier-transaction'}],next_cursor:'local-only',provider_transactions:snapshot(rows)}));
  f.node('taskCount').textContent='41次'; f.node('settlementTotal').textContent='未知';
  await f.run('loadTransactions({snapshot:true})');
  assert.deepEqual(f.calls[0],{method:'POST',path:'/usage/query',body:{page_size:1}});
  assert.equal(f.node('transactionCount').textContent,'2 条');
  assert.equal(f.node('transactionDebit').textContent,'0.1 Credits');
  assert.equal(f.node('transactionUnknown').textContent,'1 条');
  assert.equal(f.node('taskCount').textContent,'41次'); assert.equal(f.node('settlementTotal').textContent,'未知');
  assert.deepEqual(f.node('transactions').children[1].children.map(cell=>cell.textContent),['未知','其他（动作未知）','未知','未知','未提供','未提供，待对账']);
  assert.match(f.node('transactionsCoverage').textContent,/最多 20 条/);
  assert.equal(f.node('transactionsNext').disabled,true);
});

test('supplier cursor navigation is separate and errors preserve the previous page and rows', async () => {
  let fail = false;
  const first = snapshot(); first.last_success.pagination = {status:'known',next_cursor:'provider-next',has_more:true};
  const second = snapshot([{...item,credits:'3'}]); second.last_success.coverage.kind='provider_page';
  const f = fixture(input=>{if(fail) throw Error('NETWORK_TIMEOUT'); return input.body.cursor ? second : first;});
  await f.run('loadTransactions({page:0})');
  assert.equal(f.node('transactionsNext').disabled,false);
  f.run('transactionCursors[1] = transactionNext;');
  await f.run('loadTransactions({page:1})');
  assert.deepEqual(f.calls[1].body,{page_size:20,cursor:'provider-next'});
  assert.equal(f.run('usageCursor'),null); assert.equal(f.run('transactionPage'),1);
  const oldRows = f.node('transactions').children;
  fail = true; await f.run('loadTransactions({page:0})');
  assert.equal(f.run('transactionPage'),1);
  assert.deepEqual(f.node('transactions').children.map(row=>row.children.map(cell=>cell.textContent)),oldRows.map(row=>row.children.map(cell=>cell.textContent)));
  assert.match(f.node('transactionsState').textContent,/NETWORK_TIMEOUT.*保留上次窗口/);
  assert.equal(f.run('transactions.stale'),true);
});

test('missing or repeated supplier cursor never fabricates another page or total', async () => {
  const unknown = snapshot(Array(20).fill(item)); unknown.last_success.pagination={status:'unknown',next_cursor:null,has_more:null};
  const f = fixture(()=>unknown); await f.run('loadTransactions({page:0})');
  assert.equal(f.node('transactionsNext').disabled,true); assert.match(f.node('transactionsPagination').textContent,/历史范围未知/);
  const repeated = snapshot(); repeated.last_success.pagination={status:'known',next_cursor:'same',has_more:true};
  assert.equal(model.transactionNextCursor(repeated,['same']),null);
  assert.equal(model.transactionNextCursor(repeated,[null]),'same');
});

test('failed supplier observation retains last successful window instead of reporting empty usage', async () => {
  let failure = false;
  const f = fixture(()=>failure ? snapshot([], {status:'unavailable',last_success:null,error_code:'PROVIDER_RATE_LIMITED',stale:true}) : snapshot());
  await f.run('loadTransactions()'); failure=true; await f.run('loadTransactions()');
  assert.equal(f.node('transactions').children.length,1);
  assert.match(f.node('transactionsState').textContent,/PROVIDER_RATE_LIMITED.*保留最后成功窗口/);
});

test('202 starts a check without claiming success and a matching terminal reports partial failure separately', async () => {
  let terminal = false;
  const f = fixture(input=>{
    if(input.path === '/authorization/check') return {status:202,data:{check_id:'check-one',status:'pending'}};
    if(input.path === '/accounts/search') return {items:[account]};
    if(input.path === '/overview') return overview;
    if(input.path === '/authorization') return terminal ? {...authorization,check_status:'failed'} : {...authorization,check_status:'pending',pending_check_id:'check-one'};
    if(input.path === '/usage/query') return {provider_transactions:snapshot([], {status:'unavailable',error_code:'PROVIDER_TIMEOUT',stale:true})};
    throw Error('unexpected path');
  });
  await f.run('startCheck()');
  assert.equal(f.run('activeCheckId'),'check-one');
  assert.match(f.node('refreshState').textContent,/等待本次检查结果/);
  assert.doesNotMatch(f.node('refreshState').textContent,/均已更新/);
  assert.equal(f.calls.filter(call=>call.path === '/authorization/check').length,1);
  terminal=true; await f.run('refresh()');
  assert.equal(f.run('activeCheckId'),null);
  assert.match(f.node('refreshState').textContent,/余额已更新；流水未更新/);
  assert.equal(f.node('providerBalance').textContent,'0 Credits');
  assert.match(f.node('transactionsState').textContent,/PROVIDER_TIMEOUT/);
});

test('out-of-order provider responses cannot replace the latest window', async () => {
  let finish;
  const f=fixture(()=>new Promise(resolve=>{finish=resolve;}));
  const first=f.run('loadTransactions()'), finishFirst=finish;
  const second=f.run('loadTransactions()'); finish(snapshot([{...item,credits:'2'}])); await second;
  finishFirst(snapshot([{...item,credits:'100'}])); await first;
  assert.equal(f.node('transactionDebit').textContent,'2 Credits');
});

test('management denial hides business data and clears provider state', async () => {
  const f=fixture(()=>({status:403,data:{code:'MANAGEMENT_DENIED'}}));
  await f.run('loadTransactions()');
  assert.equal(f.node('business').hidden,true); assert.equal(f.run('transactions'),null);
});

test('a late overview success cannot reopen a view after provider management access was denied', async () => {
  const pending = new Map();
  const f=fixture(input=>input.path === '/transactions/query' ? {status:403,data:{code:'MANAGEMENT_DENIED'}} :
    new Promise(resolve=>pending.set(input.path,resolve)));
  const earlier = f.run('refresh()');
  await f.run('loadTransactions()');
  pending.get('/overview')(overview); pending.get('/accounts/search')({items:[account]});
  await earlier;
  assert.equal(f.node('business').hidden,true); assert.equal(f.run('account'),null);
  assert.equal(f.run('managementDenied'),true);
  const count=f.calls.length;
  await f.run('refresh()'); await f.run('loadUsage()'); await f.run('loadTransactions()');
  assert.equal(f.calls.length,count);
});

test('idle balance and provider windows expire independently without polling or replacing loading state', () => {
  const base=Date.parse(observedAt), clock=fakeClock(base+60000);
  const f=fixture(()=>{throw Error('no network expected');},clock);
  f.run(`transactions = ${JSON.stringify(snapshot())}; transactions.last_success.observed_at = ${JSON.stringify(new Date(base+60000).toISOString())}; readsStopped=true; renderAccount(); renderTransactions();`);
  f.node('transactionsState').textContent='正在读取供应商流水…';
  f.node('transactionsNext').disabled=true;
  const observed=f.node('transactionsObserved').textContent, rows=f.node('transactions').children;
  clock.advance(240000);
  assert.equal(f.node('balanceStale').textContent,'最近有效观测');
  clock.advance(1);
  assert.equal(f.node('balanceStale').textContent,'过期观测');
  assert.equal(f.node('transactionsStale').textContent,'最近有效观测');
  clock.advance(60000);
  assert.equal(f.node('transactionsStale').textContent,'过期观测');
  assert.match(f.node('transactionsFreshness').textContent,/已过期/);
  assert.equal(f.node('transactionsState').textContent,'正在读取供应商流水…');
  assert.equal(f.node('transactionsNext').disabled,true);
  assert.equal(f.node('transactionsObserved').textContent,observed);
  assert.equal(f.node('transactions').children,rows);
  assert.equal(f.calls.length,0); assert.equal(clock.pending,0);
});

test('local expiration preserves a failed page request and does not start another provider request', async () => {
  const clock=fakeClock(Date.parse(observedAt)); let fail=false;
  const f=fixture(()=>{if(fail)throw Error('NETWORK_TIMEOUT');return snapshot();},clock);
  await f.run('loadTransactions()'); fail=true; await f.run('loadTransactions()');
  const message=f.node('transactionsState').textContent, calls=f.calls.length;
  clock.advance(300001);
  assert.equal(f.node('transactionsState').textContent,message);
  assert.match(message,/NETWORK_TIMEOUT/);
  assert.equal(f.node('balanceStale').textContent,'过期观测');
  assert.equal(f.node('transactionsStale').textContent,'过期观测');
  assert.equal(f.calls.length,calls);
});

test('hidden views stop expiration timers and active views immediately re-evaluate retained observations', () => {
  const clock=fakeClock(Date.parse(observedAt));
  const f=fixture(()=>{throw Error('no network expected');},clock);
  f.run(`transactions = ${JSON.stringify(snapshot())}; renderTransactions();`);
  assert.equal(clock.pending,1);
  f.run('setVisible(false)'); assert.equal(clock.pending,0);
  clock.advance(300001);
  assert.equal(f.node('transactionsStale').textContent,'最近有效观测');
  f.run('setVisible(true)');
  assert.equal(f.node('transactionsStale').textContent,'过期观测');
  assert.equal(f.node('balanceStale').textContent,'过期观测');
  assert.equal(clock.pending,0); assert.equal(f.calls.length,0);
});

test('permission loss or disposal prevents expiration callbacks from updating retained data', () => {
  for (const stop of ['failure({status:403,code:"MANAGEMENT_DENIED"}, "")', 'disposed=true; updateObservationFreshness()']) {
    const clock=fakeClock(Date.parse(observedAt));
    const f=fixture(()=>{throw Error('no network expected');},clock);
    f.run(`transactions = ${JSON.stringify(snapshot())}; renderTransactions();`);
    assert.equal(clock.pending,1);
    f.run(stop); assert.equal(clock.pending,0);
    const message=f.node('transactionsStale').textContent;
    clock.advance(300001); f.run('updateObservationFreshness()');
    assert.equal(f.node('transactionsStale').textContent,message);
    assert.equal(clock.pending,0); assert.equal(f.calls.length,0);
  }
});

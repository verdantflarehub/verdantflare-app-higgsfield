import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as model from '../dashboard/model.mjs';

const bound = {account_id:'team', name:'会员', status:'active', revision:3, registration:{}};
const overview = {authorization:{status:'authorized',last_success:{balance_credits:'900',plan:'Plus'},stale:false}, limits:{binding_id:'team',revision:1,credit_limit:'50',submission_limit:10,reserved_credits:'0',submitted_count:0}};
function fixture(response) {
  const elements = new Map(), calls = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {value:'',hidden:false,disabled:false,textContent:'', attributes:{},
      setAttribute(key,value){this.attributes[key]=value;}, getBoundingClientRect(){return {height:400};},
      elements:{namedItem:name=>element('field:'+name)}});
    return elements.get(id);
  }
  const root = {querySelector:selector=>element(selector.slice(1)),getBoundingClientRect:()=>({height:400})};
  const sdk = {app:{request:async input=>{calls.push(structuredClone(input));return response(input);}}};
  const context = vm.createContext({document:{getElementById:()=>root},window:{$wujie:{props:{appSDK:sdk,dashboardHost:{resize(){},setDirty(){}}}}},
    AbortController,structuredClone,setTimeout:()=>0,clearTimeout(){}, ...model});
  const code = readFileSync(new URL('../dashboard/dashboard.mjs',import.meta.url),'utf8');
  vm.runInContext(code.replace(/^import .*?;\s*/, '').split('if (!sdk?.app')[0], context);
  const run = code=>vm.runInContext(code,context);
  context.initial = {account:structuredClone(bound),overview:structuredClone(overview)};
  run('account = initial.account; overview = initial.overview;');
  return {run,element,calls};
}

test('monthly remainder, provider total and unknown entitlement stay distinct',()=>{
  const view = model.accountView(bound,overview.authorization);
  assert.equal(view.credits,'未登记'); assert.equal(view.providerBalance,'900 Credits');
  assert.equal(view.concurrency,'未登记 视频 · 未登记 图片');
  const registered = model.accountView({...bound,registration:{remaining_credits:'0',monthly_credits:'1200',monthly_price_usd:'0',video_concurrency:0,image_concurrency:8,membership_expiry:'2026-11-14'}},overview.authorization);
  assert.equal(registered.credits,'0 / 1200 Credits'); assert.equal(registered.price,'US$0／月');
  assert.equal(registered.expiry,'2026-11-14'); assert.equal(registered.concurrency,'0 视频 · 8 图片');
});
test('account editor sends original revision and clears optional values without inventing zeros',()=>{
  const payload = model.accountPayload(bound,{name:'',status:'disabled',remaining_credits:'0',video_concurrency:'',image_concurrency:'8'});
  assert.equal(payload.expected_revision,3); assert.equal(payload.name,'Higgsfield 账号');
  assert.equal(payload.registration.remaining_credits,'0'); assert.equal(payload.registration.monthly_credits,null);
  assert.equal(payload.registration.video_concurrency,null); assert.equal(payload.registration.image_concurrency,8);
  assert.equal(payload.registration.membership_expiry,'');
});
test('background refresh does not overwrite dirty edit or advance its revision',async()=>{
  const f = fixture(input=>({status:200,data:input.path === '/overview' ? overview : {items:[{...bound,revision:4,name:'另一个修改'}]}}));
  f.run('currentPage="account"; fillAccount(); accountDirty=true;');
  f.element('field:name').value='我的修改';
  assert.equal(await f.run('refresh()'),true);
  assert.equal(f.element('field:name').value,'我的修改');
  assert.equal(f.run('editingAccount.revision'),3); assert.equal(f.run('account.revision'),4);
});
test('uncertain write is sent once and does not claim saved or drop edited values',async()=>{
  const f = fixture(()=>{throw new Error('connection lost');});
  f.run('currentPage="account"; fillAccount(); accountDirty=true;');
  f.element('field:name').value='待确认';
  assert.equal(await f.run('mutateAccount("PATCH", {name:"待确认"}, editingAccount)'),false);
  assert.equal(f.calls.length,1); assert.equal(f.calls[0].body.expected_revision,3);
  assert.equal(f.element('field:name').value,'待确认'); assert.equal(f.run('accountDirty'),true);
  assert.match(f.element('accountSaveState').textContent,/保存未确认/);
});
test('read failure preserves account and reports failure to reload caller',async()=>{
  const f = fixture(()=>({status:503,data:{code:'STORAGE_UNAVAILABLE'}}));
  assert.equal(await f.run('refresh()'),false);
  assert.equal(f.run('account.name'),'会员'); assert.match(f.element('message').textContent,/可能过期/);
});
test('management denial clears account data and hides business view',async()=>{
  const f = fixture(()=>({status:403,data:{code:'MANAGEMENT_DENIED'}}));
  await f.run('refresh()'); assert.equal(f.run('account'),null); assert.equal(f.element('business').hidden,true);
});

test('read failures stop after three automatic retries until explicit reload',async()=>{
  const f = fixture(()=>({status:503,data:{code:'STORAGE_UNAVAILABLE'}}));
  await f.run('refresh()');
  for(let i=0;i<3;i++) await f.run('refresh(true)');
  assert.equal(f.run('readsStopped'),true); const count = f.calls.length;
  await f.run('refresh(true)'); assert.equal(f.calls.length,count);
  assert.match(f.element('message').textContent,/自动更新已停止/);
  await f.run('refresh()'); assert.equal(f.calls.length,count+2); assert.equal(f.run('readFailures'),1);
});

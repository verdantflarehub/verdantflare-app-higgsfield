import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import * as model from '../dashboard/model.mjs';

function fixture(respond) {
  const nodes = new Map(), calls = [];
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, {value:'', textContent:'', disabled:false, children:[],
      append(child){this.children.push(child);}, replaceChildren(){this.children=[];},
      get firstChild(){return this.children[0];}, getBoundingClientRect(){return {height:400};}});
    return nodes.get(id);
  }
  const root = {querySelector:selector=>node(selector.slice(1)),getBoundingClientRect:()=>({height:400})};
  const context = vm.createContext({...model, AbortController,
    document:{getElementById:()=>root,createElement:()=>({children:[],append(child){this.children.push(child);},get firstChild(){return this.children[0];}})},
    window:{$wujie:{props:{appSDK:{app:{request:async input=>{calls.push(input);return {status:200,data:await respond(input)};}}},dashboardHost:{resize(){}}}}}});
  const source = readFileSync(new URL('../dashboard/dashboard.mjs',import.meta.url),'utf8');
  vm.runInContext(source.replace(/^import .*?;\s*/,'').split('if (!sdk?.app')[0],context);
  return {node,calls,run:source=>vm.runInContext(source,context)};
}
const item = {task_id:'task-1',account_name:'账号',created_at:'2026-10-17T00:00:00Z',model_id:'model',state:'completed',budget:{amount:'5',unit:'Credits'},settlement:{amount:'6',unit:'Credits'}};
const result = (items=[item],total=41)=>({items,total,accounts:[{account_id:'team',name:'账号'}],summary:{task_count:total,budget:{amount:'205',unit:'Credits'},settlement:{amount:null,unit:'Credits'}}});

test('real renderer separates budget, settlement, unknown final fees and unfinished fees',async()=>{
  const rows = [item,{...item,settlement:{amount:'0',unit:'Credits'}},{...item,settlement:null},{...item,state:'running',settlement:null}];
  const f=fixture(()=>result(rows,4)); await f.run('loadUsage()');
  assert.deepEqual(f.node('usage').children.map(row=>row.children.slice(6).map(cell=>cell.textContent)),
    [['5 Credits','6 Credits'],['5 Credits','0 Credits'],['5 Credits','未知'],['5 Credits','待结算']]);
  assert.equal(f.node('usageNext').disabled,true);
  assert.equal(f.node('usageRange').textContent,'1–4 / 4');
});
test('committed filters persist across pages and failed reads retain rows and cursor',async()=>{
  let fail=false;
  const f=fixture(()=>{if(fail)throw Error('offline');return result(Array(20).fill(item));});
  f.node('logAccount').value='team';f.node('logSearch').value='model';
  await f.run('loadUsage({offset:0,applyFilters:true})');
  f.node('logSearch').value='unsubmitted';
  await f.run('loadUsage({offset:20})');
  assert.equal(f.calls[1].body.search,'model');assert.equal(f.calls[1].body.account_id,'team');
  assert.equal(f.node('usagePrevious').disabled,false);assert.equal(f.node('usageRange').textContent,'21–40 / 41');
  const rows=f.node('usage').children;fail=true;
  await f.run('loadUsage({offset:40})');
  assert.equal(f.run('usageCursor'),20);assert.equal(f.node('usage').children,rows);
  assert.equal(f.node('usageNext').disabled,false);assert.equal(f.node('usageRange').textContent,'21–40 / 41');
  assert.match(f.node('usageState').textContent,/保留上次日志/);
});
test('stale response cannot replace latest filter results or pagination',async()=>{
  let finish;const f=fixture(()=>new Promise(resolve=>{finish=resolve;}));
  const first=f.run('loadUsage()');const finishFirst=finish;
  const second=f.run('loadUsage({offset:20})');finish(result([item],21));await second;
  finishFirst(result([item],100));await first;
  assert.equal(f.node('usageRange').textContent,'21–21 / 21');assert.equal(f.node('usageNext').disabled,true);
});
test('date filter uses local midnight and includes full end date; inverted range sends no request',async()=>{
  const q=model.logQuery({from:'2026-10-17',to:'2026-10-17'});
  assert.equal(q.created_from,new Date(2026,9,17).toISOString());
  assert.equal(q.created_before,new Date(2026,9,18).toISOString());
  const f=fixture(()=>result());f.node('logFrom').value='2026-10-18';f.node('logTo').value='2026-10-17';
  await f.run('loadUsage({offset:0,applyFilters:true})');assert.equal(f.calls.length,0);
  assert.match(f.node('usageState').textContent,/结束日期不能早于/);
});

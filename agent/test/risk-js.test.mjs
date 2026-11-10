// 使用真实QuickJS/worker验证隔离与桥接边界；业务返回为明确标记的固定样例，真实MCP由Python集成用例覆盖。
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRiskJsTool, DEMO_RISK_JS } from '../dist/risk-js.js';
import { BusinessMcpClient, BusinessToolError } from '../dist/mcp-business.js';
import { runAgent, toolStream } from '../dist/runtime.js';

const plan = {org_ids:['LS082'],indicator_ids:['npl_ratio','overdue_ratio'],history:true};
const checked = {cases:[{case_key:'LS082',evidence:[{evidence_id:'source-82',indicator_id:'npl_ratio',rule_kind:'rank'}]}],summary:{triggered:1},receipt_id:'receipt-1'};
const definitions = [
  {name:'check_risks',inputSchema:{type:'object',properties:{},additionalProperties:false}},
  {name:'search_indicators',inputSchema:{type:'object',properties:{query:{type:'string'},limit:{type:'integer'},offset:{type:'integer'}},additionalProperties:false}},
];
function fixture(overrides={}, deadline=Date.now()+10000) {
  const calls=[], trace=[], failures=[], zeros=[];
  const mcp={definitionsFor:()=>definitions,async call(name,args,signal){
    calls.push({name,args});
    if (overrides[name]) return overrides[name](args,signal);
    if(name==='check_risks')return checked;
    if(name==='search_indicators')return {items:[{id:'overdue_ratio'}]};
    if(name==='register_query_plan')return {plan_id:'plan-1',path:'/data/plans/plan-1.json',status:args.plan.org_ids.length?'ready':'zero-candidates',coverage:{triggered:0}};
    throw new Error('unexpected tool');
  }};
  const js=createRiskJsTool({mcp,deadline,signal:new AbortController().signal,trace,onFatal:e=>failures.push(e),onZero:r=>zeros.push(r)});
  return {...js,mcp,calls,trace,failures,zeros,execute:code=>js.tool.execute('test',{code})};
}

test('real QuickJS combines two reads and registers only a small plan',async()=>{
  const f=fixture(); const result=await f.execute(DEMO_RISK_JS);
  assert.deepEqual(f.calls.at(-1).args,{plan,check_receipt_id:'receipt-1'});
  assert.equal(JSON.parse(result.content[0].text).plan_id,'plan-1');
  assert.equal(f.metrics.nested_calls,2); assert.equal(f.metrics.host_calls,3);
  assert.equal(f.metrics.workers_started,1); assert.equal(f.metrics.workers_exited,1);
  assert.equal(f.trace.length,3); assert.ok(f.trace.every(entry=>entry.status==='ok'));
});
test('zero candidates terminates after a real coverage read, without an export',async()=>{
  const f=fixture({check_risks:async()=>({...checked,cases:[]})});
  const result=await f.execute(DEMO_RISK_JS);
  assert.equal(result.terminate,true); assert.equal(f.zeros[0].outcome,'zero-candidates');
  assert.deepEqual(f.calls.map(c=>c.name),['check_risks','register_query_plan']);
  assert.deepEqual(f.calls[1].args.plan.org_ids,[]);
});
test('VM cannot call noninjected capabilities and extra bridge arguments are rejected',async()=>{
  for(const code of ['await tools.execute_python({})','await tools.register_query_plan({})','await tools.check_risks({run_id:"forged"})','await tools.search_indicators({limit:"5"})']) {
    const f=fixture(); await assert.rejects(f.execute(code));
    assert.equal(f.calls.length,0); assert.equal(f.metrics.workers_started,f.metrics.workers_exited);
  }
});
test('host MCP call allowlist is enforced before connection or dispatch',async()=>{
  const client=new BusinessMcpClient({RISK_PYTHON_EXECUTABLE:'python',RISK_PROJECT_ROOT:'.'},{run_id:'test'},undefined,Date.now()+1000,['check_risks']);
  await assert.rejects(client.call('execute_python',{}),{code:'TOOL_NOT_ALLOWED'});
});
test('a script cannot catch exhausted shared quota and register a success',async()=>{
  const f=fixture({check_risks:async()=>{throw new BusinessToolError('QUERY_BUDGET_EXCEEDED','exhausted');}});
  await assert.rejects(f.execute(`try{await tools.check_risks({})}catch{};return ${JSON.stringify(plan)}`),{code:'QUERY_BUDGET_EXCEEDED'});
  assert.equal(f.calls.length,1); assert.ok(f.failures.length); assert.equal(f.trace[0].status,'error');
});
test('response, return and log size limits reject instead of silently truncating',async()=>{
  const big=fixture({check_risks:async()=>({...checked,large:'x'.repeat(70000)})});
  await assert.rejects(big.execute(DEMO_RISK_JS),{code:'JS_LIMIT'});
  for(const code of [`text('x'.repeat(17000));return ${JSON.stringify(plan)}`,`return {...${JSON.stringify(plan)},extra:'x'.repeat(17000)}`]) {
    const f=fixture(); await assert.rejects(f.execute(code),{code:'JS_LIMIT'});
    assert.equal(f.calls.length,0);
  }
});
test('32 MiB heap and task deadline stop allocations and loops, then await worker exit',async()=>{
  for(const [code,error,timeout] of [['const a=[];while(true)a.push("x".repeat(1000000))',/out of memory/,2000],['while(true){}',/aborted|timeout/,200]]) {
    const f=fixture({},Date.now()+timeout);
    await assert.rejects(f.execute(code),error);
    assert.equal(f.metrics.workers_started,f.metrics.workers_exited);
  }
});
test('unawaited host work finishes before close returns and cannot register a plan',async()=>{
  let finished=false;
  const f=fixture({check_risks:async()=>{await new Promise(resolve=>setTimeout(resolve,150));finished=true;return checked;}});
  await assert.rejects(f.execute(`tools.check_risks({});return ${JSON.stringify(plan)}`),{code:'CANCELLED'});
  assert.equal(finished,true); assert.equal(f.metrics.workers_started,f.metrics.workers_exited);
  assert.equal(f.calls.length,1);
});
test('fixed model streams compare two direct Pi turns with one real QuickJS turn for the same plan',async()=>{
  const model={id:'fixed-stream',name:'fixed-stream',api:'openai-completions',provider:'test',baseUrl:'http://127.0.0.1/unused',reasoning:false,input:['text'],contextWindow:32000,maxTokens:2000,cost:{input:0,output:0,cacheRead:0,cacheWrite:0}};
  const config={mode:'demo',env:{},model,limits:{requests:5,tools:5,timeoutMs:10000,batchTimeoutMs:10000}};
  const direct=fixture(); let directPlan; let checkResult;
  const directTools=definitions.map(definition=>({...definition,label:definition.name,description:'固定合成业务返回',parameters:definition.inputSchema,execute:async(_,args)=>{
    const data=await direct.mcp.call(definition.name,args);
    if(definition.name==='check_risks') checkResult=data;
    else {
      directPlan={org_ids:checkResult.cases.filter(item=>item.evidence.some(e=>e.indicator_id==='npl_ratio'&&e.rule_kind==='rank')).map(item=>item.case_key),indicator_ids:['npl_ratio',...data.items.filter(item=>item.id==='overdue_ratio').map(item=>item.id)],history:true};
      await direct.mcp.call('register_query_plan',{plan:directPlan,check_receipt_id:checkResult.receipt_id});
    }
    return {content:[{type:'text',text:JSON.stringify(data)}],details:{},terminate:definition.name==='search_indicators'};
  }}));
  const startedDirect=performance.now();
  const directResult=await runAgent({...config,stream:(model,ctx)=>ctx.messages.some(message=>message.role==='toolResult')?toolStream(model,'search_indicators',{query:'逾期贷款率',limit:5,offset:0}):toolStream(model,'check_risks',{})},directTools,'相同合成输入，仅测计划阶段',()=>directPlan,10000);
  const directMs=performance.now()-startedDirect;
  const js=fixture(); let jsPlan;
  const wrapped={...js.tool,execute:async(...args)=>{const result=await js.tool.execute(...args);jsPlan=js.calls.at(-1).args.plan;return {...result,terminate:true};}};
  const startedJs=performance.now();
  const jsResult=await runAgent({...config,stream:model=>toolStream(model,'risk_js',{code:DEMO_RISK_JS})},[wrapped],'相同合成输入，仅测计划阶段',()=>jsPlan,10000);
  const jsMs=performance.now()-startedJs;
  assert.deepEqual(jsResult.value,directResult.value);
  assert.equal(directResult.runtime.requests,2); assert.equal(jsResult.runtime.requests,1);
  assert.equal(js.metrics.workers_started,js.metrics.workers_exited);
  console.log('FIXED_STREAM_PLAN_COMPARISON '+JSON.stringify({business_source:'fixed fixture',model:'deterministic stream, no LLM',quickjs:'real worker',direct:{requests:2,host_calls:direct.calls.length,model_bytes:directResult.runtime.model_tool_result_bytes,ms:Math.round(directMs*100)/100},risk_js:{requests:1,host_calls:js.calls.length,model_bytes:jsResult.runtime.model_tool_result_bytes,ms:Math.round(jsMs*100)/100}}));
});

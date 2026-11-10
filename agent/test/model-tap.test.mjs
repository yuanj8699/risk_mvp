// 本地HTTP供应商回放：真实Pi/SSE/SDK重试与工具循环，不连接外网或真实模型。
import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Type, createAssistantMessageEventStream } from '@earendil-works/pi-ai';
import { stream as completionStream } from '@earendil-works/pi-ai/api/openai-completions';
import { runAgent, toolStream } from '../dist/runtime.js';
import { createModelTap, sanitize, safeError, boundedDiagnostics } from '../dist/model-tap.js';

const model = { id:'local-tap-fixture', name:'Local HTTP replay (not a real LLM)', api:'openai-completions', provider:'openai', baseUrl:'http://127.0.0.1/unused', reasoning:true, input:['text'], contextWindow:32000,maxTokens:4000,cost:{input:0,output:0,cacheRead:0,cacheWrite:0} };
const config = {mode:'model',env:{},model,apiKey:'fixture-key-not-a-credential',limits:{requests:10,tools:12,timeoutMs:10000,batchTimeoutMs:10000}};

test('local HTTP SSE records two real retries, public reasoning, usage and tool return bytes',async()=>{
  let attempts=0; let output;
  const bodies=[];
  const server=http.createServer(async(request,response)=>{
    let text='';for await(const chunk of request)text+=chunk;
    bodies.push(JSON.parse(text)); attempts++;
    if(attempts<=2){response.writeHead(503,{'Content-Type':'application/json','retry-after-ms':'1'});response.end(JSON.stringify({error:{message:'local fixture temporary failure'}}));return;}
    const final=bodies.at(-1).messages.some(message=>message.role==='tool');
    const tool=final?'complete':'lookup';
    const args=final?{}:{org_id:'LS082',api_key:config.apiKey,code:'console.log("fixture");\n'.repeat(150)};
    response.writeHead(200,{'Content-Type':'text/event-stream'});
    const emit=chunk=>response.write(`data: ${JSON.stringify({id:'fixture',object:'chat.completion.chunk',created:1,model:model.id,...chunk})}\n\n`);
    emit({choices:[{index:0,delta:{role:'assistant',reasoning_content:final?'本地回放：公开结果核对。':'公开片段'.repeat(1250),signature:'signature-must-not-be-recorded'}}]});
    emit({choices:[{index:0,delta:{tool_calls:[{index:0,id:`call-${tool}`,type:'function',function:{name:tool,arguments:JSON.stringify(args)}}]}}]});
    emit({choices:[{index:0,delta:{},finish_reason:'tool_calls'}]});
    emit({choices:[],usage:{prompt_tokens:100,completion_tokens:30,total_tokens:130,prompt_tokens_details:{cached_tokens:5},...(final?{}:{completion_tokens_details:{reasoning_tokens:10}})}});
    response.end('data: [DONE]\n\n');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  try{
    const localModel={...model,baseUrl:`http://127.0.0.1:${server.address().port}/v1`};
    const tools=[{name:'lookup',label:'lookup',description:'fixture lookup',parameters:Type.Object({org_id:Type.String(),api_key:Type.String(),code:Type.String()}),execute:async()=>({content:[{type:'text',text:JSON.stringify({evidence_id:'fixture-ev-1',value:'1.2%'})}],details:{evidence_ids:['fixture-ev-1']}})},
      {name:'complete',label:'complete',description:'fixture completion',parameters:Type.Object({}),execute:async()=>{output={completed:true};return {content:[{type:'text',text:'accepted'}],details:{},terminate:true};}}];
    const result=await runAgent({...config,model:localModel,stream:completionStream},tools,'local fixture, no real LLM',()=>output,10000,{phase:'explain',caseKey:'LS082',requestContext:()=>({fixture:'local HTTP replay'}),fatal:()=>undefined,cancel:()=>{},demoStream:completionStream});
    const requests=result.trace.filter(item=>item.event==='request');
    assert.equal(attempts,4); assert.equal(requests.length,2);
    assert.equal(requests[0].retry_count,2);assert.deepEqual(requests[0].http_attempts.map(item=>item.status_code),[503,503,200]);
    assert.equal(requests[1].retry_count,0);assert.equal(requests[1].usage.reasoning_tokens,null);assert.equal(requests[0].reasoning_content.length,5000);assert.equal(requests[0].reasoning_truncated,false);
    assert.equal(requests[0].reasoning_source,'provider_reasoning_content');assert.equal(requests[0].usage.reasoning_tokens,10);assert.equal(requests[0].usage.output_tokens,30);
    assert.ok(requests[0].request_summary.tool_names.includes('lookup'));assert.ok(requests[0].request_summary.payload_bytes>0);
    const lookup=result.trace.find(item=>item.tool==='lookup');
    assert.equal(lookup.arguments.api_key,'[REDACTED]');assert.equal(lookup.arguments.code.truncated,true);assert.ok(lookup.response_bytes>0);assert.equal(lookup.parent_id,requests[0].id);
    assert.ok(!JSON.stringify(result.trace).includes(config.apiKey));assert.ok(!JSON.stringify(result.trace).includes('signature-must-not-be-recorded'));
    const directory=join(tmpdir(),'risk-trace-panel');await mkdir(directory,{recursive:true});
    await writeFile(join(directory,'model-tap-fixture.json'),JSON.stringify({fixture:{kind:'local-http-replay',real_llm:false,description:'本地HTTP/SSE固定回放，token及reasoning为fixture返回，不是真实模型推理'},trace:result.trace,runtime:result.runtime},null,2));
  }finally{await new Promise(resolve=>server.close(resolve));}
});

test('unknown provider retries and hidden thinking stay unknown; public thinking is bounded',()=>{
  const entry={event:'request',status:'running'};
  const tap=createModelTap(entry,{...model,api:'unsupported-provider'},{messages:[]},{},true,[]);
  assert.equal(tap.options.fetch,undefined);
  tap.finish({content:[{type:'thinking',thinking:'hidden must not appear',redacted:true,thinkingSignature:'opaque-secret'},{type:'thinking',thinking:'公'.repeat(9000),thinkingSignature:'opaque-secret'}],usage:{input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{total:0}},stopReason:'stop'});
  assert.equal(entry.retry_count,null);assert.equal(entry.retry_observation,'unavailable');assert.equal(entry.usage,null);
  assert.equal(entry.reasoning_content.length,8000);assert.equal(entry.reasoning_truncated,true);assert.equal(entry.reasoning_source,'pi_thinking');assert.ok(!JSON.stringify(entry).includes('opaque-secret'));
  const args=sanitize({authorization:'Bearer private',script:'api_key="private"\n'+ 'x'.repeat(9000),path:'analysis.py'});
  assert.equal(args.authorization,'[REDACTED]');assert.equal(args.script.truncated,true);assert.ok(!JSON.stringify(args).includes('"private"'));
  const diagnostics=boundedDiagnostics({trace:Array.from({length:1100},(_,index)=>({event:'request',id:String(index),reasoning_content:'x'.repeat(8000)}))});
  assert.equal(diagnostics.truncated,true);assert.ok(diagnostics.trace.length<=1000);assert.ok(Buffer.byteLength(JSON.stringify(diagnostics))<=1024*1024);
});

test('ordinary Python failures get three total attempts; writing a correction does not reset them',async()=>{
  for(const eventuallySucceeds of [true,false]){
    let executions=0,output;
    const tools=[{name:'write_task_file',label:'write',description:'fixed correction, no files',parameters:Type.Object({}),execute:async()=>({content:[{type:'text',text:'corrected'}],details:{}})},
      {name:'execute_python',label:'execute',description:'fixed execution feedback, no Docker',parameters:Type.Object({}),execute:async()=>{executions++;const success=eventuallySucceeds&&executions===3;if(success)output={completed:true};return {content:[{type:'text',text:success?'ok':'SyntaxError: local replay'}],details:{exit_code:success?0:1},terminate:success};}}];
    const stream=(model,ctx)=>{const last=ctx.messages.filter(item=>item.role==='toolResult').at(-1);return toolStream(model,last?.toolName==='execute_python'?'write_task_file':'execute_python',{});};
    const pending=runAgent({...config,mode:'demo',stream},tools,'fixed replay',()=>output,10000);
    let trace;
    if(eventuallySucceeds)trace=(await pending).trace;
    else await assert.rejects(pending,error=>{trace=error.diagnostics.trace;return error.code==='RECOVERY_EXHAUSTED';});
    assert.equal(executions,3);
    const executionTrace=trace.filter(item=>item.tool==='execute_python');
    assert.deepEqual(executionTrace.map(item=>item.recovery_attempt),[1,2,3]);
    assert.equal(executionTrace[2].recovery_status,eventuallySucceeds?'recovered':'exhausted');
    assert.ok(trace.every(item=>item.started_at&&typeof item.duration_ms==='number'));
    assert.ok(trace.filter(item=>item.event==='request').every(item=>item.retry_count===null&&item.retry_observation==='not_applicable'&&item.usage===null));
  }
});

test('schema rejection before execute also receives three bounded correction opportunities',async()=>{
  let executed=0;
  const tool={name:'lookup',label:'lookup',description:'schema fixture',parameters:Type.Object({org_id:Type.String()},{additionalProperties:false}),execute:async()=>{executed++;return {content:[],details:{}};}};
  await assert.rejects(runAgent({...config,mode:'demo',stream:model=>toolStream(model,'lookup',{unknown:'invalid'})},[tool],'fixture',()=>undefined,10000),error=>{
    assert.equal(error.code,'RECOVERY_EXHAUSTED');assert.deepEqual(error.diagnostics.trace.filter(item=>item.tool==='lookup').map(item=>item.recovery_attempt),[1,2,3]);return true;
  });
  assert.equal(executed,0);
});

test('reasoning redacts a secret split across the cutoff and preserves valid Unicode at all limits',async()=>{
  const secret='customer-private-credential-X';
  const entry={event:'request',status:'running'};
  const tap=createModelTap(entry,model,{messages:[]},{},true,[secret]);
  await tap.options.onProviderStreamEvent({choices:[{delta:{reasoning_content:'a'.repeat(7990)+secret.slice(0,10)}}]},model);
  assert.ok(!entry.reasoning_content.includes(secret.slice(0,10)));
  await tap.options.onProviderStreamEvent({choices:[{delta:{reasoning_content:secret.slice(10)+'尾部'}}]},model);
  assert.ok(!entry.reasoning_content.includes(secret.slice(0,10)));
  const message={content:[{type:'thinking',thinking:'a'.repeat(7999)+'😀'}],usage:{input:1,output:1,reasoning:0,cacheRead:0,cacheWrite:0,totalTokens:2,cost:{total:0}},stopReason:'stop'};
  const unicode={event:'request',status:'running'};
  const unicodeTap=createModelTap(unicode,model,{messages:[]},{},true,[]);
  await unicodeTap.options.onProviderStreamEvent({usage:{completion_tokens_details:{reasoning_tokens:0}}},model);
  unicodeTap.finish(message);
  assert.equal(unicode.usage.reasoning_tokens,0);
  const values=[unicode.reasoning_content,sanitize('a'.repeat(3999)+'😀'),sanitize('a'.repeat(1999)+'😀',[],'code').preview,safeError(new Error('a'.repeat(999)+'😀')).message];
  for(const value of values){assert.equal(value.isWellFormed(),true);assert.equal(JSON.parse(Buffer.from(JSON.stringify(value),'utf8').toString('utf8')),value);}
});

test('synchronous provider failure preserves a redacted root cause in failure diagnostics',async()=>{
  await assert.rejects(runAgent({...config,stream:()=>{throw new Error(`local provider initialization failed: ${config.apiKey}`);}},[],'fixture',()=>undefined,10000),error=>{
    assert.equal(error.code,'PROVIDER_ERROR');
    assert.match(error.diagnostics.trace[0].error.message,/local provider initialization failed/);
    assert.ok(!JSON.stringify(error.diagnostics).includes(config.apiKey));
    return true;
  });
});

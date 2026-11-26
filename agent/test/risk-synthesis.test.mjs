import test from "node:test";
import assert from "node:assert/strict";
import { runRequest, toolStream } from "../dist/runtime.js";

const candidate = id => ({ org_id: id, org_name: `${id}行社`, signals: [
  { evidence_id: `${id}-signal`, dimension_id: "credit_risk", aspect: "不良变化", reason: "达到参考筛查条件" },
  { evidence_id: `${id}-other`, dimension_id: "credit_risk", aspect: "不良变化", reason: "关联信号" },
], evidence: [
  { evidence_id: `${id}-signal`, org_id: id, quality_status: "valid", value: 2 },
  { evidence_id: `${id}-other`, org_id: id, quality_status: "valid", value: 3 },
  { evidence_id: `${id}-context`, org_id: id, quality_status: "valid", value: 4, role: "context", indicator_name: "各项贷款" },
] });
const request = { operation: "synthesize_risks", budget_ms: 30000, knowledge_prompt: "参考口径", dimension_ids: ["credit_risk"],
  validation_summary: {}, candidates: [candidate("a"), candidate("b"), candidate("c")] };

test("多批次归并保留同一风险方面的信号及关联指标", async () => {
  const candidates = Array.from({ length: 82 }, (_, index) => candidate(index === 0 ? "a" : "org-" + index));
  const result = await runRequest({ ...request, candidates }, { env: {} });
  assert.equal(result.mode, "pi-demo");
  assert.deepEqual(result.issues.map(issue => issue.org_id), candidates.map(item => item.org_id));
  assert.deepEqual(result.issues[0].evidence_ids, ["a-signal", "a-other", "a-context"]);
  assert.equal(result.runtime.requests, 17);
  assert.equal(result.trace.filter(t => t.tool === "submit_risk_synthesis").length, 17);
});

test("演示按风险方面给出不同的待核原因和检查方向", async () => {
  const credit = candidate("a"), treasury = candidate("b");
  credit.signals.forEach(s => { s.aspect = "逾期贷款增加"; });
  treasury.signals.forEach(s => { s.aspect = "债券投资规模及市场风险"; });
  const result = await runRequest({ ...request, candidates: [credit, treasury] }, { env: {} });
  assert.match(result.issues[0].check_suggestion, /逾期/);
  assert.match(result.issues[1].check_suggestion, /债券/);
  assert.notEqual(result.issues[0].cause_analysis, result.issues[1].cause_analysis);
});

test("模型不能引用另一行社的真实证据", async () => {
  const streamFn = model => toolStream(model, "submit_risk_synthesis", { summary: "风险需核查", issues: [{
    org_id: "a", dimension_id: "credit_risk", aspect: "不良变化", title: "不良变化", severity: "关注",
    cause_analysis: "有待核实", check_suggestion: "核对明细", evidence_ids: ["a-signal", "b-signal"],
  }] });
  await assert.rejects(runRequest({ ...request, candidates: request.candidates.slice(0, 2) }, { env: {}, streamFn }), { code: "RECOVERY_EXHAUSTED" });
});

test("不能将缺失或跨机构观测作为触发信号", async () => {
  const bad = candidate("a");
  bad.evidence[0].value = null;
  await assert.rejects(runRequest({ ...request, candidates: [bad] }, { env: {} }), { code: "INVALID_INPUT" });
});

test("归并工具仅接受有证据引用的定量事实，允许日期、机构数量和指标名称", async () => {
  const one = { ...request, candidates: [candidate("a")] };
  const { issues } = await runRequest(one, { env: {} });
  const valid = "截至2026年9月末（2026-09-30），两家机构的五级不良和一级资本需核查，请按万元口径核对";
  for (const fact of ["1.5%", "120万元", "第78名", "0.05个百分点", "百分之二", "排名第54", "前10名", "倒数第5", "-0.3个百分点", "+2.7％", "+3", "-2", "13.8", "三个百分点", "三成", "三倍", "12笔", "30户", "余额120万"]) {
    let attempts = 0;
    const streamFn = model => toolStream(model, "submit_risk_synthesis", {
      issues: issues.map(issue => ({ ...issue, cause_analysis: attempts === 0 && fact === "+3" ? fact : valid })),
      summary: attempts++ || fact === "+3" ? valid : "需核实" + fact,
    });
    const result = await runRequest(one, { env: {}, streamFn });
    assert.equal(attempts, 2, fact);
    assert.ok(result.executive_summary.includes(valid));
  }
});

test("定性原因、银行业务用语和年月可直接提交", async () => {
  const one = { ...request, candidates: [candidate("a")] };
  const { issues: [baseIssue] } = await runRequest(one, { env: {} });
  const issue = {
    ...baseIssue,
    title: "城乡二元结构与一元化管理",
    cause_analysis: "暂不认定单一成因。待统一成本口径后，逐一分析单一成本与三元结构。",
    check_suggestion: "落实一户一策、一企一策、一笔一策，逐户核查一户多贷，逐笔复核一笔多押及单一户涉及的多笔贷款。",
  };
  const summary = "请对照2026-09 业务数据、2026-9月报及2026-09-30明细。";
  const streamFn = model => toolStream(model, "submit_risk_synthesis", { issues: [issue], summary });
  const result = await runRequest(one, { env: {}, streamFn });
  assert.deepEqual(result.issues, [issue]);
  assert.ok(result.executive_summary.includes(summary));
  assert.equal(result.runtime.requests, 1);
});

test("定量内容被拦截，模型收到命中原文及对应事项或综述位置", async () => {
  const batch = { ...request, candidates: [candidate("a"), candidate("b")] };
  const { issues } = await runRequest(batch, { env: {} });
  const valid = { issues, summary: "需继续核查" };
  const cases = [
    ["title", "不良率2.30%", "2.30%"],
    ["cause_analysis", "暂不认定单一成因，但逾期占比三成", "三成"],
    ["cause_analysis", "近三成交易仍待复核", "三成"],
    ["check_suggestion", "请核对排名第77位的依据", "排名第77位"],
    ["title", "涉及三万元", "三万元"],
    ["title", "余额150万元", "150万元"],
    ["title", "核对一百零五元的差额", "一百零五元"],
    ["cause_analysis", "差额500元", "500元"],
    ["check_suggestion", "落实一户一策，另核对12户", "12户"],
    ["cause_analysis", "涉及三户", "三户"],
    ["cause_analysis", "十一户一策落实情况待核", "十一户"],
    ["summary", "2026-09 业务数据变动为-0.5", "-0.5"],
  ];
  for (const [field, text, matched] of cases) {
    const invalid = field === "summary"
      ? { ...valid, summary: text }
      : { ...valid, issues: issues.map(issue => issue.org_id === "b" ? { ...issue, [field]: text } : issue) };
    let feedback;
    const streamFn = (model, context) => {
      const error = context.messages.find(message => message.role === "toolResult" && message.isError);
      if (error) feedback = error.content.filter(block => block.type === "text").map(block => block.text).join("");
      return toolStream(model, "submit_risk_synthesis", error ? valid : invalid);
    };
    const result = await runRequest(batch, { env: {}, streamFn });
    const location = field === "summary" ? "综述" : `事项（b / ${field}）`;
    assert.ok(feedback?.includes(location), `${text} 定位有误：${feedback}`);
    assert.ok(feedback.includes(`“${matched}”`), `${text} 命中片段有误：${feedback}`);
    assert.deepEqual(result.issues, valid.issues);
    assert.ok(result.executive_summary.includes(valid.summary));
  }
});

test("归并最多并发六批，每批五家，输出预算受模型上限约束", async () => {
  const candidates = Array.from({ length: 31 }, (_, i) => candidate("org-" + i));
  let active = 0, peak = 0;
  const sizes = [], budgets = [];
  const streamFn = async (model, context, options) => {
    const batch = JSON.parse(context.messages.at(-1).content).candidates;
    sizes.push(batch.length);
    budgets.push([options.maxTokens, model.maxTokens]);
    peak = Math.max(peak, ++active);
    await new Promise(resolve => setImmediate(resolve));
    active--;
    return toolStream(model, "submit_risk_synthesis", {
      summary: "需关注不良变化",
      issues: batch.map(c => ({ org_id: c.org_id, dimension_id: "credit_risk", aspect: "不良变化",
        title: "不良变化", severity: "关注", cause_analysis: "待核实", check_suggestion: "核对明细",
        evidence_ids: [c.signals[0].evidence_id] })),
    });
  };
  await runRequest({ ...request, candidates }, { env: {}, streamFn });
  assert.deepEqual(sizes, [5, 5, 5, 5, 5, 5, 1]);
  assert.equal(peak, 6);
  for (const [actual, limit] of budgets) assert.equal(actual, Math.min(10000, limit));
});

test("归并单次默认预算、显式覆盖和整批截止时间", async t => {
  const nativeTimer = globalThis.setTimeout;
  const delays = [];
  t.mock.method(Date, "now", () => 1700000000000);
  t.mock.method(globalThis, "setTimeout", (fn, delay, ...args) => {
    delays.push(delay);
    return nativeTimer(fn, delay, ...args);
  });
  for (const [env, budget, expected] of [
    [{}, 360000, 120000],
    [{ RISK_PI_TIMEOUT_MS: "95000" }, 360000, 95000],
    [{ RISK_PI_TIMEOUT_MS: "95000" }, 1000, 1000],
  ]) {
    delays.length = 0;
    await runRequest({ ...request, budget_ms: budget }, { env });
    assert.ok(delays.includes(expected), JSON.stringify(delays));
  }
});

test("思考模型归并请求获得一万输出 token", async () => {
  const one = { ...request, candidates: [candidate("a")] };
  const { issues } = await runRequest(one, { env: {} });
  const env = { RISK_PI_MODE: "model", RISK_PI_PROVIDER: "deepseek", RISK_PI_MODEL: "deepseek-flash",
    RISK_PI_API_KEY: "offline-test", RISK_PI_THINKING_LEVEL: "high" };
  let observed;
  const streamFn = (model, _context, options) => {
    observed = [model.reasoning, options.maxTokens];
    return toolStream(model, "submit_risk_synthesis", { issues, summary: "需核实" });
  };
  await runRequest(one, { env, streamFn });
  assert.deepEqual(observed, [true, 10000]);
});

import test from "node:test";
import assert from "node:assert/strict";
import { runRequest, toolStream } from "../dist/runtime.js";

const offline = { env: {} };
const task = {
  operation: "follow_up", context: { topic: "信用风险", as_of: "2026-02-28", organization: "甲农商银行" },
  history: [], evidence: [{id: "npl", label: "不良贷款率", text: "2026-02-28，不良贷款率为2.11%。"}],
  question: "本次不良率是多少？", smartq: { enabled: true },
};
const toolNames = result => result.trace.filter(t => t.event === "tool").map(t => t.tool);

test("归档足以回答时即使小 Q 可用也直接引用", async () => {
  const reply = await runRequest(task, offline);
  assert.deepEqual(reply.evidence_ids, ["npl"]);
  assert.deepEqual(toolNames(reply), ["read_task_evidence", "submit_follow_up"]);
  assert.equal(reply.smartq_lookup, undefined);
  for (const enabled of [true, false]) {
    const summary = await runRequest({ ...task, smartq: { enabled }, question: "请总结本次任务归档证据" }, offline);
    assert.deepEqual(summary.evidence_ids, ["npl"]);
    assert.equal(summary.smartq_lookup, undefined);
  }
});

test("下钻问题不把归档总指标当明细，换期时沿用上一问的指标、机构和日期", async () => {
  const input = { ...task, question: "那它上期按行业拆分呢？", history: [{
    question: "看看这家的情况", answer: "上一轮有记录。",
    rewritten_question: "2026年01月末，甲农商银行，查询不良贷款率（单值，按原口径取值）。",
  }], evidence: [{ id: "base", label: "不良贷款率", text: "2025-12-31，不良贷款率为1.50%。" }] };
  const result = await runRequest(input, offline);
  assert.deepEqual(toolNames(result), ["read_task_evidence", "request_smartq_lookup"]);
  const lookup = result.smartq_lookup;
  assert.equal(lookup.target_org, "甲农商银行");
  assert.equal(lookup.target_date, "2025-12-31");
  assert.equal(lookup.query_type, "detail");
  assert.equal(lookup.metric_hint, "不良贷款率");
  assert.match(lookup.rewritten_question, /2025年12月末.*甲农商银行.*不良贷款率.*行业/);
  assert.doesNotMatch(lookup.rewritten_question, /上期|它|这家|加总/);
});

test("补查后的第二次调用只引用本轮补查记录；无结果时不拿归档数字代答", async () => {
  const extra = { id: "smartq:482936212bc13210", label: "小Q补查 · 表外授信余额", text: "表外授信余额 142 万元", source: "smartq" };
  const attempted = { ...task, question: "表外授信余额呢？", smartq: { enabled: true, attempted: true, status: "ok" } };
  const result = await runRequest({ ...attempted, evidence: [...task.evidence, extra] }, offline);
  assert.deepEqual(result.evidence_ids, [extra.id]);
  assert.match(result.answer, /小 Q 实时补查，口径仅供参考/);
  assert.match(result.answer, /142 万元/);
  for (const smartq of [{ enabled: false }, { enabled: true, attempted: true, status: "empty" }]) {
    const failed = await runRequest({ ...attempted, smartq }, offline);
    assert.deepEqual(failed.evidence_ids, []);
    assert.match(failed.answer, /证据不足/);
    assert.doesNotMatch(failed.answer, /2\.11/);
    assert.equal(failed.smartq_lookup, undefined);
  }
});

test("改写区分十一类问法，使用全省规范口径，并保留统计条件", async () => {
  const context = { ...task.context, organization: "本次任务范围内的 82 家行社" };
  for (const [phrase, type] of [
    ["是多少", "single"], ["总和", "total"], ["差额", "diff"], ["占比", "ratio"],
    ["排名前5", "rank"], ["最高值", "extremum"], ["按产品分组", "detail"], ["有多少笔", "count"],
    ["同比", "yoy"], ["比上月", "mom"], ["是否一致，请校验", "check"],
  ]) {
    const question = "全省表外担保余额" + phrase + "？";
    const { smartq_lookup: lookup } = await runRequest({ ...task, context, question }, offline);
    assert.equal(lookup?.query_type, type, question);
    assert.equal(lookup.target_org, "全省合计（含宁波）");
    assert.match(lookup.rewritten_question, /2026年02月末.*全省合计（含宁波）.*表外担保余额/);
    if (type === "rank") assert.match(lookup.rewritten_question, /前5/);
    if (type === "mom") assert.match(lookup.rewritten_question, /2026年01月末/);
  }
});

test("工具拒绝绕过归档检索；关闭或已尝试时模型看不到补查工具", async () => {
  const lookup = { reason: "缺少该项", metric_hint: "表外余额", target_org: "甲农商银行",
    target_date: "2026-02-28", rewritten_question: "2026年02月末甲农商银行表外余额是多少？", query_type: "single" };
  const streamFn = model => toolStream(model, "request_smartq_lookup", lookup);
  await assert.rejects(runRequest(task, { ...offline, streamFn }), { code: "RECOVERY_EXHAUSTED" });
  for (const smartq of [{ enabled: false }, { enabled: true, attempted: true }]) {
    const examine = (model, conversation) => {
      return conversation.messages.some(m => m.role === "toolResult")
        ? toolStream(model, "submit_follow_up", { answer: "资料不足。", evidence_ids: [] })
        : toolStream(model, "read_task_evidence", { keywords: ["表外"], offset: 0 });
    };
    const result = await runRequest({ ...task, smartq }, { ...offline, streamFn: examine });
    assert.deepEqual(result.evidence_ids, []);
    const visible = result.trace.find(t => t.event === "request").request_summary.tool_names;
    assert.ok(visible.includes("read_task_evidence"));
    assert.ok(!visible.includes("request_smartq_lookup"));
  }
});

test("支持条件筛查与Excel前15%问句，开放式风险追问自主补齐监管/业务阈值且尊重用户自定义阈值", async () => {
  const allScope = { ...task.context, organization: "本次任务范围内的 82 家行社" };
  // 1. 用户明确指定阈值与前15%
  const explicitFilter = await runRequest({
    ...task, context: allScope, evidence: [], question: "不良贷款率＞1.5%的行社是哪几家？",
  }, offline);
  assert.equal(explicitFilter.smartq_lookup?.query_type, "filter");
  assert.equal(explicitFilter.smartq_lookup?.target_org, "全部行社");
  assert.match(explicitFilter.smartq_lookup?.rewritten_question, /2026年02月末.*全部行社.*不良贷款率＞1\.5%/);

  const customThreshold = await runRequest({
    ...task, context: allScope, evidence: [], question: "不良贷款率＞2.2%的行社有哪些？",
  }, offline);
  assert.equal(customThreshold.smartq_lookup?.query_type, "filter");
  assert.match(customThreshold.smartq_lookup?.rewritten_question, /2\.2%/);
  assert.doesNotMatch(customThreshold.smartq_lookup?.rewritten_question, /1\.5%/);

  const top15 = await runRequest({
    ...task, context: allScope, evidence: [], question: "本期全部行社中不良贷款余额排名前15%的行社是哪几家？",
  }, offline);
  assert.equal(top15.smartq_lookup?.query_type, "rank");
  assert.equal(top15.smartq_lookup?.target_org, "全部行社");
  assert.match(top15.smartq_lookup?.rewritten_question, /不良贷款余额.*前15%/);

  // 2. 开放式追问自主依据监管红线/预警参考线改写
  const autoNpl = await runRequest({
    ...task, context: allScope, evidence: [], question: "哪些行社不良率偏高？",
  }, offline);
  assert.equal(autoNpl.smartq_lookup?.query_type, "filter");
  assert.match(autoNpl.smartq_lookup?.rewritten_question, /五级不良贷款率＞1\.5%/);
  assert.match(autoNpl.smartq_lookup?.reason, /1\.5%.*参考预警线/);

  const autoCar = await runRequest({
    ...task, context: allScope, evidence: [], question: "有没有资本充足率不达标的行社？",
  }, offline);
  assert.equal(autoCar.smartq_lookup?.query_type, "filter");
  assert.match(autoCar.smartq_lookup?.rewritten_question, /资本充足率＜10\.5%/);
  assert.match(autoCar.smartq_lookup?.reason, /法定监管底线/);

  const autoLiq = await runRequest({
    ...task, context: allScope, evidence: [], question: "哪些行社流动性比例偏低？",
  }, offline);
  assert.equal(autoLiq.smartq_lookup?.query_type, "filter");
  assert.match(autoLiq.smartq_lookup?.rewritten_question, /流动性比例＜25%/);
});

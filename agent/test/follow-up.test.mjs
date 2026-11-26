import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { runRequest, toolStream } from "../dist/runtime.js";

const task = {
  operation: "follow_up", context: { topic: "贷款质量", organization: "甲行社", as_of: "2026-10-28" },
  question: "那上期呢？", history: [{ question: "不良率有什么记录？", answer: "查看本次任务的不良率记录。" }],
  evidence: [
    { id: "dep1", label: "存款", text: "10月28日比年初增长2%" },
    { id: "npl8", label: "五级不良贷款率", text: "8月31日 3.07%" },
    { id: "npl9", label: "五级不良贷款率", text: "9月30日 3.11%" },
  ],
};
const offline = { env: {} };

test("全辖归档超过千条时可翻到千条之后，未查询的完整归档不进入模型上下文", async () => {
  const evidence = Array.from({ length: 1205 }, (_, i) => ({ id: `row-${i}`, label: `行社记录${i}`, text: `记录值 ${i}` }));
  const streamFn = (model, conversation) => {
    const read = conversation.messages.find(m => m.role === "toolResult" && m.toolName === "read_task_evidence");
    if (!read) {
      assert.doesNotMatch(JSON.stringify(conversation.messages), /行社记录1101/);
      return toolStream(model, "read_task_evidence", { keywords: [], offset: 1100 });
    }
    const page = JSON.parse(read.content[0].text);
    return toolStream(model, "submit_follow_up", { answer: page.rows[1].text, evidence_ids: [page.rows[1].id] });
  };
  const result = await runRequest({ ...task, evidence }, { ...offline, streamFn });
  assert.deepEqual(result.evidence_ids, ["row-1101"]);
  assert.match(result.answer, /1101/);
});

test("CLI 接受超过原五兆限制的完整归档", () => {
  const evidence = Array.from({ length: 5300 }, (_, i) => ({ id: `row-${i}`, label: "记录", text: "x".repeat(1100) }));
  const input = JSON.stringify({ ...task, question: "不存在的内容", evidence });
  assert.ok(Buffer.byteLength(input) > 5 * 1024 * 1024);
  const child = spawnSync(process.execPath, ["dist/runner.js"], { input, encoding: "utf8", timeout: 30000 });
  assert.equal(child.status, 0, child.stdout || child.stderr);
  assert.match(JSON.parse(child.stdout).answer, /证据不足/);
});

test("追问沿用上一轮指标并引用实际读取的任务证据", async () => {
  const result = await runRequest(task, offline);
  assert.match(result.answer, /3\.07%/);
  assert.doesNotMatch(result.answer, /存款/);
  assert.deepEqual(result.evidence_ids, ["npl8", "npl9"]);
  assert.deepEqual(result.trace.filter(x => x.event === "tool").map(x => x.tool), ["read_task_evidence", "submit_follow_up"]);
  assert.equal(result.runtime.framework, "pi-agent-core");
  assert.equal(result.runtime.llm_connected, false);
});

test("未读证据即使属于同一任务也不能被其他检索结果引用", async () => {
  const streamFn = (model, conversation) => conversation.messages.some(m => m.role === "toolResult")
    ? toolStream(model, "submit_follow_up", { answer: "未知事实", evidence_ids: ["dep1"] })
    : toolStream(model, "read_task_evidence", { keywords: ["不良贷款率"], offset: 0 });
  await assert.rejects(runRequest(task, { ...offline, streamFn }), { code: "RECOVERY_EXHAUSTED" });
});

test("查询无证据时空引用不会接受模型声称的事实", async () => {
  const streamFn = (model, conversation) => conversation.messages.some(m => m.role === "toolResult")
    ? toolStream(model, "submit_follow_up", { answer: "无任何风险", evidence_ids: [] })
    : toolStream(model, "read_task_evidence", { keywords: [], offset: 0 });
  const result = await runRequest({ ...task, evidence: [] }, { ...offline, streamFn });
  assert.match(result.answer, /证据不足/);
  assert.deepEqual(result.evidence_ids, []);
});

test("跨期查询首屏同时包含两个业务日期的目标指标，即使机构和排名在其他记录中重复", async () => {
  const otherMetrics = ["存款余额", "不良生成率", "逾期贷款率", "资本充足率", "存款增幅"];
  const observations = otherMetrics.map((metric, i) => ({
    id: "unrelated-" + i, label: "新昌 · " + metric, text: "2026-09-30，排名72/82。",
  }));
  const compared = [
    { id: "npl30", label: "新昌 · 五级不良贷款率", text: "2026-09-30，3.11%，排名72/82。" },
    { id: "too-old", label: "新昌 · 五级不良贷款率", text: "2026-08-31，3.00%，排名72/82。" },
    { id: "npl29", label: "新昌 · 五级不良贷款率", text: "2026-09-29，3.07%。" },
  ];
  const streamFn = (model, conversation) => {
    const read = conversation.messages.find(m => m.role === "toolResult" && m.toolName === "read_task_evidence");
    if (!read) return toolStream(model, "read_task_evidence", {
      keywords: ["新昌", "不良贷款率", "2026-09-29", "2026-09-30", "排名"], offset: 0,
    });
    const page = JSON.parse(read.content[0].text);
    return toolStream(model, "submit_follow_up", {
      answer: page.rows.map(row => row.text).join("\n"), evidence_ids: page.rows.map(row => row.id),
    });
  };
  const result = await runRequest({ ...task, evidence: [...observations, ...compared] }, { ...offline, streamFn });
  assert.match(result.answer, /2026-09-29，3\.07%/);
  assert.match(result.answer, /2026-09-30，3\.11%/);
  assert.doesNotMatch(result.answer, /2026-08-31/);
  assert.deepEqual(result.evidence_ids.slice(0, 2), ["npl29", "npl30"]);
});

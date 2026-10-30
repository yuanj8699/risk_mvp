import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { runRequest, toolStream, errorStream, validateRequest } from "../dist/runtime.js";

const selection = {
  operation: "select", topic: "信用风险",
  indicators: [
    { id: "npl_ratio", name: "五级不良贷款率" },
    { id: "funds_npl_change", name: "资金业务不良余额比年初增量" },
    { id: "pure_deposit_growth", name: "纯存款余额比年初增幅" },
  ],
  mandatory_ids: ["npl_ratio"],
};
const evidence = { id: "ev-1", indicator_id: "funds_npl_balance", indicator_name: "资金业务不良余额", value: "120000", value_display: "12万元", rule_kind: "exists", reason: "有效值非零，触发存在提醒", data_date: "2026-09-30" };
const caseItem = { case_key: "LS001", evidence: [evidence] };
const context = { run_id: "test-run", topic: "信用风险", snapshot_id: "current", as_of: "2026-10-28", rule_snapshot: [], org_scope: ["LS001"], history_snapshot_ids: ["history"], source: "synthetic" };
const batch = { operation: "explain_batch", context, budget_ms: 120000, cases: [caseItem] };
const demo = { env: {} };

function scriptedSubmit(operation, args) {
  return (model, context) => context.messages.some(x => x.role === "toolResult")
    ? toolStream(model, operation === "select" ? "submit_selection" : "submit_analysis", args)
    : toolStream(model, operation === "select" ? "read_indicator_catalog" : "read_case_evidence", {});
}

test("demo uses the actual Pi Agent loop and retains mandatory indicators", async () => {
  const result = await runRequest(selection, demo);
  assert.equal(result.mode, "pi-demo");
  assert.deepEqual(result.indicator_ids, ["funds_npl_change", "npl_ratio"]);
  assert.equal(result.runtime.framework, "pi-agent-core");
  assert.equal(result.runtime.version, "1.4.0");
  assert.equal(result.runtime.llm_connected, false);
  assert.equal(result.runtime.requests, 2);
  assert.deepEqual(result.trace.filter(x => x.event === "tool").map(x => x.tool), ["read_indicator_catalog", "submit_selection"]);
  const deposits = await runRequest({ ...selection, topic: "存款扩张" }, demo);
  assert.deepEqual(deposits.indicator_ids, ["npl_ratio", "pure_deposit_growth"]);
});

// 解释阶段已改为真实MCP取数；数据库值/历史/引用/函件由Python端到端用例验证。
test("task contract fixes scope and budget while allowing one observation to trigger multiple rules", () => {
  const repeated = { ...batch, cases: [{ ...caseItem, evidence: [evidence, { ...evidence, rule_kind: "rank" }] }] };
  assert.equal(validateRequest(repeated), repeated);
  for (const invalid of [
    { ...batch, context: undefined }, { ...batch, budget_ms: 0 },
    { ...batch, cases: [{ ...caseItem, case_key: "LS002" }] },
  ]) assert.throws(() => validateRequest(invalid), { code: "INVALID_INPUT" });
});

test("unknown selected IDs and missing mandatory IDs are rejected by executed tool", async () => {
  for (const ids of [["not-in-catalog", "npl_ratio"], ["funds_npl_change"]]) {
    await assert.rejects(runRequest(selection, { ...demo, streamFn: scriptedSubmit("select", { indicator_ids: ids, reasons: ["测试选择"] }) }), { code: "BUDGET_EXCEEDED" });
  }
});

test("invalid tool output can be corrected after feedback within the same Pi loop", async () => {
  const streamFn = (model, context) => {
    const results = context.messages.filter(message => message.role === "toolResult");
    if (!results.length) return toolStream(model, "read_indicator_catalog", {});
    const corrected = results.some(message => message.isError);
    return toolStream(model, "submit_selection", { indicator_ids: corrected ? ["npl_ratio"] : ["missing"], reasons: ["根据目录纠正"] });
  };
  const result = await runRequest(selection, { ...demo, streamFn });
  assert.deepEqual(result.indicator_ids, ["npl_ratio"]);
  assert.equal(result.trace.filter(item => item.status === "error").length, 1);
  assert.equal(result.runtime.requests, 3);
});

test("result submission requires a preceding evidence/catalog read", async () => {
  const streamFn = model => toolStream(model, "submit_selection", { indicator_ids: ["npl_ratio"], reasons: ["跳过读取"] });
  await assert.rejects(runRequest(selection, { ...demo, streamFn }), { code: "BUDGET_EXCEEDED" });
});

test("repeated requests and tool calls stop at the configured budgets", async () => {
  const streamFn = model => toolStream(model, "read_indicator_catalog", {});
  await assert.rejects(runRequest(selection, { ...demo, streamFn, limits: { requests: 2 } }), { code: "BUDGET_EXCEEDED" });
  await assert.rejects(runRequest(selection, { ...demo, limits: { tools: 1 } }), { code: "BUDGET_EXCEEDED" });
});

test("unresponsive streams respect the wall-clock timeout", async () => {
  const streamFn = () => createAssistantMessageEventStream();
  await assert.rejects(runRequest(selection, { ...demo, streamFn, limits: { timeoutMs: 20 } }), { code: "TIMEOUT" });
});

test("upstream cancellation aborts an active Pi request", async () => {
  const controller = new AbortController();
  const pending = runRequest(selection, { ...demo, signal: controller.signal, streamFn: () => createAssistantMessageEventStream() });
  controller.abort();
  await assert.rejects(pending, { code: "TIMEOUT" });
});

test("provider failures are surfaced without leaking raw error content", async () => {
  const secret = "private-key-and-provider-payload";
  await assert.rejects(runRequest(selection, { ...demo, streamFn: model => errorStream(model, secret) }), err => err.code === "PROVIDER_ERROR" && !err.message.includes(secret));
  await assert.rejects(runRequest(selection, { ...demo, streamFn: () => { throw new Error(secret); } }), err => err.code === "PROVIDER_ERROR" && !err.message.includes(secret));
});

test("model configuration is explicit and never falls back to demo", async () => {
  await assert.rejects(runRequest(selection, { env: { RISK_PI_MODE: "model" } }), { code: "CONFIG_ERROR" });
  await assert.rejects(runRequest(selection, { env: { RISK_PI_MODE: "invalid" } }), { code: "CONFIG_ERROR" });
  await assert.rejects(runRequest(selection, { env: { RISK_PI_MODE: "model", RISK_PI_PROVIDER: "unknown", RISK_PI_MODEL: "missing", RISK_PI_API_KEY: "hidden" } }), { code: "CONFIG_ERROR" });
});

test("input contracts reject unknown mandatory IDs and duplicate cases", async () => {
  await assert.rejects(runRequest({ ...selection, mandatory_ids: ["missing"] }, demo), { code: "INVALID_INPUT" });
  await assert.rejects(runRequest({ ...batch, cases: [caseItem, caseItem] }, demo), { code: "INVALID_INPUT" });
});

test("CLI emits one JSON value and a nonzero exit on invalid input", () => {
  const cwd = new URL("..", import.meta.url);
  const ok = spawnSync(process.execPath, ["dist/runner.js"], { cwd, input: JSON.stringify(selection), encoding: "utf8", env: { ...process.env, RISK_PI_MODE: "demo" } });
  assert.equal(ok.status, 0, ok.stderr);
  assert.equal(JSON.parse(ok.stdout).mode, "pi-demo");
  const invalid = spawnSync(process.execPath, ["dist/runner.js"], { cwd, input: "invalid-json", encoding: "utf8" });
  assert.equal(invalid.status, 1);
  assert.equal(JSON.parse(invalid.stdout).error.code, "INVALID_INPUT");
});

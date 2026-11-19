import test from "node:test";
import assert from "node:assert/strict";
import { Type, createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import { runRequest, toolStream } from "../dist/runtime.js";
import { BusinessMcpClient } from "../dist/mcp-business.js";

const request = {
  operation: "explore", budget_ms: 5000,
  context: { run_id: "fixture", topic: "不良率", snapshot_id: "snap", as_of: "2026-09-30",
    source: "synthetic", org_scope: ["A"], history_snapshot_ids: [], rule_snapshot: [] },
};
const report = { summary: "当期观测", findings: [{ org_id: "A", statement: "不良贷款率1%", evidence_ids: ["obs-1"] }] };

// 回放本地 MCP 工具的响应；由真实 Pi 派发、收窄工具并判断是否终止。
test("成功执行、读回结果后只回复文字，允许限预算补交一次且不会重跑Python", async t => {
  let executions = 0;
  t.mock.method(BusinessMcpClient.prototype, "connect", async () => {});
  t.mock.method(BusinessMcpClient.prototype, "definitionsFor", () => [
    { name: "execute_python", inputSchema: Type.Object({}) },
    { name: "read_task_file", inputSchema: Type.Object({ path: Type.String() }) },
  ]);
  t.mock.method(BusinessMcpClient.prototype, "call", async name => {
    if (name === "execute_python") {
      executions++;
      return { execution_id: "exe", exit_code: 0 };
    }
    return { path: "result.json", truncated: false, content: JSON.stringify(report) };
  });
  const env = { RISK_PYTHON_EXECUTABLE: "python", RISK_PROJECT_ROOT: process.cwd() };
  for (const [index, fallback] of ["submit", "text", "execute_again", "budget"].entries()) {
    let turns = 0;
    const streamFn = async (model, context) => {
      turns++;
      if (turns === 1) return toolStream(model, "execute_python", {});
      if (turns === 2) return toolStream(model, "read_task_file", { path: "result.json" });
      if (turns === 4) {
        assert.ok(context.messages.some(m => m.role === "user" && String(m.content).includes("调用 submit_exploration")));
        if (fallback === "submit") return toolStream(model, "submit_exploration", {
          ...report, execution_ids: ["exe"], result_file: "result.json",
        });
        if (fallback === "execute_again") return toolStream(model, "execute_python", {});
      }
      const template = await toolStream(model, "submit_exploration", {}).result();
      const text = createAssistantMessageEventStream();
      text.push({ type: "done", reason: "stop", message: { ...template, stopReason: "stop",
        content: [{ type: "text", text: "数据分析已经完成。" }] } });
      return text;
    };
    const promise = runRequest(request, { env, streamFn, limits: { requests: fallback === "budget" ? 3 : 6 } });
    if (fallback === "submit") {
      const result = await promise;
      assert.deepEqual(result.findings, report.findings);
      assert.equal(result.runtime.requests, 4);
    } else {
      await assert.rejects(promise, { code: fallback === "budget" ? "BUDGET_EXCEEDED" : "INVALID_OUTPUT" });
    }
    assert.equal(turns, fallback === "budget" ? 3 : 4);
    assert.equal(executions, index + 1, "每个任务最多执行一次 Python");
  }
});

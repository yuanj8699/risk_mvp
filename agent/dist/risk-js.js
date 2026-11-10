/** 小范围QuickJS编排：只注入两项只读查询，校验返回计划后交宿主登记，始终等待worker实际退出。 */
import { CodemodeSandbox, renderDeclarations } from "@earendil-works/pi-codemode";
import { AjvJsonSchemaValidator } from "@modelcontextprotocol/sdk/validation/ajv";
import { Type } from "@earendil-works/pi-ai";
import { BusinessToolError } from "./mcp-business.js";
export const JS_READ_TOOLS = ["check_risks", "search_indicators"];
export const RISK_JS_LIMITS = { code: 32 * 1024, args: 32 * 1024, response: 64 * 1024, output: 16 * 1024, value: 16 * 1024, totalResponses: 256 * 1024, calls: 16, timeoutMs: 10000, heap: 32 * 1024 * 1024 };
const FATAL_CODES = new Set(["QUERY_BUDGET_EXCEEDED", "RATE_LIMITED", "QUERY_CONCURRENCY", "GLOBAL_CONCURRENCY", "EXPORT_BUDGET_EXCEEDED", "QUERY_TIMEOUT", "CANCELLED", "CONFIG_ERROR", "SOURCE_ERROR", "TOOL_NOT_ALLOWED", "JS_LIMIT"]);
const validator = new AjvJsonSchemaValidator();
const validatePlan = validator.getValidator({
    type: "object", additionalProperties: false, required: ["org_ids", "indicator_ids", "history"], properties: {
        org_ids: { type: "array", maxItems: 82, items: { type: "string", minLength: 1, maxLength: 128 } },
        indicator_ids: { type: "array", minItems: 1, maxItems: 1000, items: { type: "string", minLength: 1, maxLength: 128 } },
        history: { type: "boolean" },
    },
});
const bytes = (value) => Buffer.byteLength(typeof value === "string" ? value : JSON.stringify(value), "utf8");
function bounded(value, limit, label) {
    const size = bytes(value);
    if (size > limit)
        throw new BusinessToolError("JS_LIMIT", `${label}超过${limit}字节上限，未使用截断结果`);
    return size;
}
export function createRiskJsTool(options) {
    const metrics = { nested_calls: 0, host_calls: 0, response_bytes: 0, workers_started: 0, workers_exited: 0 };
    const definitions = options.mcp.definitionsFor(JS_READ_TOOLS);
    const schemas = new Map(definitions.map(definition => [definition.name, validator.getValidator({ ...definition.inputSchema, additionalProperties: false })]));
    // renderDeclarations的类型要求execute，但这里只生成签名，运行时使用下方有校验的readTools。
    const declarations = definitions.map(definition => ({ name: definition.name, description: definition.description, inputSchema: definition.inputSchema, execute: () => undefined }));
    const tool = {
        name: "risk_js", label: "编排只读风险查询",
        description: `在32MiB QuickJS中组合只读工具，10秒内返回{org_ids,indicator_ids,history}小计划。宿主登记为只读计划文件，后续Python只读取返回path。不能访问文件、网络或执行Python；查询额度全任务共享。\n${renderDeclarations({ tools: declarations })}`,
        parameters: Type.Object({ code: Type.String({ minLength: 1, maxLength: RISK_JS_LIMITS.code }) }, { additionalProperties: false }),
        execute: async (_, args, agentSignal) => {
            const code = args.code;
            bounded(code, RISK_JS_LIMITS.code, "JS源码");
            const remaining = Math.min(RISK_JS_LIMITS.timeoutMs, options.deadline - Date.now());
            if (remaining <= 0)
                throw new BusinessToolError("QUERY_TIMEOUT", "任务时间已耗尽");
            const limitController = new AbortController();
            const timer = setTimeout(() => limitController.abort(), remaining);
            const signal = AbortSignal.any([options.signal, limitController.signal, ...(agentSignal ? [agentSignal] : [])]);
            let fatal;
            let receiptId;
            let calls = 0, responseBytes = 0;
            const pending = new Set();
            const workers = [];
            const readTools = definitions.map(definition => ({
                ...definition,
                execute: (raw, callContext) => {
                    const operation = (async () => {
                        const started = performance.now();
                        const entry = { event: "tool", transport: "mcp", parent_tool: "risk_js", tool: definition.name, status: "error" };
                        options.trace.push(entry);
                        metrics.nested_calls++;
                        try {
                            if (++calls > RISK_JS_LIMITS.calls)
                                throw new BusinessToolError("QUERY_BUDGET_EXCEEDED", "本次JS嵌套调用达到上限");
                            bounded(raw, RISK_JS_LIMITS.args, "查询参数");
                            const checked = schemas.get(definition.name)(raw);
                            if (!checked.valid)
                                throw new Error(`查询参数不符合工具schema：${checked.errorMessage}`);
                            entry.arguments = checked.data;
                            metrics.host_calls++;
                            const result = await options.mcp.call(definition.name, checked.data, AbortSignal.any([signal, callContext.signal]));
                            const size = bytes(result);
                            entry.response_bytes = size;
                            responseBytes += size;
                            metrics.response_bytes += size;
                            bounded(result, RISK_JS_LIMITS.response, "查询响应");
                            if (responseBytes > RISK_JS_LIMITS.totalResponses)
                                throw new BusinessToolError("QUERY_BUDGET_EXCEEDED", "本次JS累计查询响应超过上限");
                            if (definition.name === "check_risks") {
                                if (typeof result.receipt_id !== "string")
                                    throw new BusinessToolError("SOURCE_ERROR", "检查结果缺少宿主覆盖回执");
                                receiptId = result.receipt_id;
                            }
                            entry.status = "ok";
                            return result;
                        }
                        catch (error) {
                            if (error instanceof BusinessToolError && FATAL_CODES.has(error.code)) {
                                fatal = error;
                                options.onFatal(error);
                            }
                            throw error;
                        }
                        finally {
                            entry.duration_ms = performance.now() - started;
                            if (callContext.signal.aborted || signal.aborted)
                                entry.status = "cancelled";
                        }
                    })();
                    pending.add(operation);
                    void operation.then(() => pending.delete(operation), () => pending.delete(operation));
                    return operation;
                },
            }));
            const sandbox = new CodemodeSandbox({
                tools: readTools, timeoutMs: remaining, memoryLimitBytes: RISK_JS_LIMITS.heap,
                onWorkerStarted(exited) { metrics.workers_started++; workers.push(exited.then(() => { metrics.workers_exited++; })); },
            });
            try {
                const result = await sandbox.execute(code, { signal });
                await Promise.allSettled([...pending]);
                if (result.calls.some(call => call.status === "cancelled")) {
                    fatal ??= new BusinessToolError("CANCELLED", "JS存在未完成或取消的查询，不登记不完整计划");
                    options.onFatal(fatal);
                }
                if (fatal)
                    throw fatal; // 即使JS捕获额度错误，也不能将之后的return登记为成功计划。
                if (!result.ok)
                    throw new Error(`risk_js ${result.error.kind}: ${result.error.message.slice(0, 2000)}`);
                bounded(result.output, RISK_JS_LIMITS.output, "JS日志");
                bounded(result.value, RISK_JS_LIMITS.value, "JS返回值");
                const parsed = validatePlan(result.value);
                if (!parsed.valid)
                    throw new Error(`补查计划格式错误：${parsed.errorMessage}`);
                if (!parsed.data.org_ids.length && !receiptId)
                    throw new Error("空候选计划必须来自本次实际check_risks覆盖回执");
                metrics.host_calls++;
                const started = performance.now();
                const entry = { event: "tool", transport: "mcp", parent_tool: "risk_js", tool: "register_query_plan", status: "error" };
                options.trace.push(entry);
                let registration;
                try {
                    registration = await options.mcp.call("register_query_plan", { plan: parsed.data, ...(receiptId ? { check_receipt_id: receiptId } : {}) }, signal);
                }
                finally {
                    entry.duration_ms = performance.now() - started;
                }
                entry.response_bytes = bytes(registration);
                bounded(registration, RISK_JS_LIMITS.response, "计划登记响应");
                if (typeof registration.plan_id !== "string" || typeof registration.path !== "string" || !["ready", "zero-candidates"].includes(String(registration.status)))
                    throw new BusinessToolError("SOURCE_ERROR", "计划登记响应不完整");
                entry.status = "ok";
                entry.plan_id = registration.plan_id;
                const zero = registration.status === "zero-candidates";
                if (zero)
                    options.onZero({ outcome: "zero-candidates", plan_id: registration.plan_id, summary: "本次规则检查没有产生补查候选；覆盖与缺失情况以宿主回执为准。", coverage: registration.coverage ?? {} });
                return { content: [{ type: "text", text: JSON.stringify(registration) }], details: { plan_id: registration.plan_id, calls: result.calls }, terminate: zero };
            }
            catch (error) {
                if (error instanceof BusinessToolError && FATAL_CODES.has(error.code))
                    options.onFatal(error);
                throw error;
            }
            finally {
                await sandbox.close();
                await Promise.allSettled([...pending]);
                await Promise.all(workers); // close只请求终止；真正退出前不能释放worker占位。
                clearTimeout(timer);
            }
        },
    };
    return { tool, metrics };
}
export const DEMO_RISK_JS = `const checked = await tools.check_risks({});
const org_ids = checked.cases.filter(c => c.evidence.some(e => e.indicator_id === "npl_ratio" && e.rule_kind === "rank")).map(c => c.case_key);
const indicator_ids = ["npl_ratio"];
if (org_ids.length) {
  const catalog = await tools.search_indicators({query:"逾期贷款率",limit:5,offset:0});
  if (catalog.items.some(item => item.id === "overdue_ratio")) indicator_ids.push("overdue_ratio");
}
return {org_ids,indicator_ids,history:true};`;

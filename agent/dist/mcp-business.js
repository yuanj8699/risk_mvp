/** 只读业务MCP客户端：绑定任务/机构，转换查询错误，并管理stdio子进程生命周期。 */
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { ErrorCode, McpError } from "@modelcontextprotocol/sdk/types.js";
export const BUSINESS_TOOLS = ["search_indicators", "query_metrics", "check_risks", "get_letter_template"];
export class BusinessToolError extends Error {
    code;
    recoverable;
    constructor(code, message, recoverable = false) {
        super(message);
        this.code = code;
        this.recoverable = recoverable;
    }
}
/** 每个行社创建独立stdio会话，范围由服务端环境绑定，模型没有进程/文件工具。 */
export class BusinessMcpClient {
    deadline;
    client = new Client({ name: "risk-pi-agent", version: "0.2.0" });
    transport;
    closing;
    definitions = [];
    constructor(env, context, orgId, deadline) {
        this.deadline = deadline;
        if (!env.RISK_PYTHON_EXECUTABLE || !env.RISK_PROJECT_ROOT) {
            throw new BusinessToolError("CONFIG_ERROR", "MCP需要RISK_PYTHON_EXECUTABLE和RISK_PROJECT_ROOT");
        }
        this.transport = new StdioClientTransport({
            command: env.RISK_PYTHON_EXECUTABLE,
            args: ["-m", "backend.mcp_server"], cwd: env.RISK_PROJECT_ROOT,
            env: {
                PYTHONUTF8: "1", PYTHONUNBUFFERED: "1",
                ...(env.RISK_DATABASE_URL ? { RISK_DATABASE_URL: env.RISK_DATABASE_URL } : {}),
                RISK_TASK_RUN_ID: context.run_id, RISK_TASK_ORG_ID: orgId,
            },
            stderr: "ignore",
        });
    }
    options(signal) {
        if (signal?.aborted || Date.now() >= this.deadline)
            throw new BusinessToolError("TIMEOUT", "MCP任务已取消或超时");
        return { signal, timeout: Math.max(1, this.deadline - Date.now()) };
    }
    async connect(signal) {
        try {
            await this.client.connect(this.transport, this.options(signal));
            const listed = await this.client.listTools({}, this.options(signal));
            if (BUSINESS_TOOLS.some(name => !listed.tools.some(tool => tool.name === name))) {
                throw new BusinessToolError("SOURCE_ERROR", "MCP业务工具不完整，请检查后端服务版本");
            }
            this.definitions = listed.tools.filter(tool => BUSINESS_TOOLS.includes(tool.name));
        }
        catch (error) {
            await this.close();
            if (error instanceof BusinessToolError)
                throw error;
            throw new BusinessToolError(signal?.aborted || Date.now() >= this.deadline ? "TIMEOUT" : "SOURCE_ERROR", "MCP业务服务连接失败或超时");
        }
    }
    /** schema/说明直接来自MCP服务，客户端只筛选白名单并绑定Pi执行入口。 */
    tools(execute) {
        return this.definitions.map(tool => ({
            name: tool.name, label: tool.title ?? tool.name, description: tool.description ?? tool.name,
            // Pi支持标准JSON Schema，MCP SDK已校验inputSchema的对象结构。
            parameters: tool.inputSchema,
            execute: async (_, args) => execute(tool.name, args),
        }));
    }
    async call(name, args, signal) {
        try {
            const result = await this.client.callTool({ name, arguments: args }, undefined, this.options(signal));
            const content = Array.isArray(result.content) ? result.content : [];
            const text = content.filter(block => block.type === "text").map(block => block.text).join("\n");
            let data = result.structuredContent;
            if (!data) {
                try {
                    data = JSON.parse(text);
                }
                catch {
                    if (result.isError && /\d+ validation errors? for|input validation error|validation failed|invalid arguments/i.test(text)) {
                        throw new BusinessToolError("INVALID_ARGUMENT", "MCP参数校验失败，请按工具schema修正输入", true);
                    }
                    throw new BusinessToolError("SOURCE_ERROR", "MCP业务服务未返回有效JSON结果");
                }
            }
            if (!data || typeof data !== "object" || Array.isArray(data))
                throw new BusinessToolError("SOURCE_ERROR", "MCP业务结果格式错误");
            const body = data;
            if (result.isError || body.error) {
                const failure = body.error;
                const code = failure?.code ?? "SOURCE_ERROR";
                const recoverable = ["INVALID_ARGUMENT", "UNKNOWN_INDICATOR", "SCOPE_VIOLATION", "VALIDATION_ERROR"].includes(code);
                throw new BusinessToolError(code, recoverable ? (failure?.message ?? "请修正工具参数后再调用") : "MCP数据源不可用或查询失败", recoverable);
            }
            return body;
        }
        catch (error) {
            if (error instanceof BusinessToolError)
                throw error;
            if (error instanceof McpError && error.code === ErrorCode.InvalidParams) {
                throw new BusinessToolError("INVALID_ARGUMENT", "MCP参数校验失败，请按工具schema修正输入", true);
            }
            throw new BusinessToolError(signal?.aborted || Date.now() >= this.deadline ? "TIMEOUT" : "SOURCE_ERROR", "MCP查询失败或超时，未使用不完整结果");
        }
    }
    /** SDK先关闭stdin，再按时限终止子进程；重复close复用同一次清理。 */
    close() {
        return this.closing ??= this.client.close();
    }
}

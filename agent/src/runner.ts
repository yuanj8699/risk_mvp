import { RiskAgentError, runRequest } from "./runtime.js";
import type { AgentErrorResponse } from "./contracts.js";

// 每个进程处理一个请求；stdout只写JSON协议，不混入日志、密钥或模型原始响应。
try {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of process.stdin) {
    bytes += chunk.length;
    if (bytes > 5 * 1024 * 1024) {
      throw new RiskAgentError("INVALID_INPUT", "Agent请求超过5MB");
    }
    chunks.push(chunk);
  }
  let request: unknown;
  try {
    request = JSON.parse(Buffer.concat(chunks).toString("utf8").replace(/^\uFEFF/, ""));
  } catch {
    throw new RiskAgentError("INVALID_INPUT", "Agent输入不是有效JSON");
  }
  process.stdout.write(JSON.stringify(await runRequest(request)) + "\n");
} catch (error) {
  const safe = error instanceof RiskAgentError
    ? error
    : new RiskAgentError("RUNTIME_ERROR", "Pi运行时执行失败；请检查已安装依赖与本地配置");
  const response: AgentErrorResponse = { error: { code: safe.code, message: safe.message } };
  process.stdout.write(JSON.stringify(response) + "\n");
  process.exitCode = 1;
}

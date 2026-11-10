/** 模型/工具观测：仅记录公开返回与有界诊断，不改变请求、传输或SDK重试策略。 */
import { createHash, randomUUID } from "node:crypto";
export const traceId = () => randomUUID();
export const jsonBytes = (value) => Buffer.byteLength(JSON.stringify(value) ?? "null", "utf8");
const secretKey = /authorization|api[-_]?key|access[-_]?token|refresh[-_]?token|password|secret|cookie|signature|credential/i;
function clip(text, limit) {
    const end = Math.min(limit, text.length);
    return text.slice(0, end && /[\uD800-\uDBFF]/.test(text[end - 1]) ? end - 1 : end);
}
function redactText(text, secrets, partialTail = false) {
    let safe = text;
    for (const secret of secrets.filter(Boolean))
        safe = safe.split(secret).join("[REDACTED]");
    if (partialTail) {
        for (const secret of secrets.filter(Boolean)) {
            for (let length = Math.min(secret.length - 1, safe.length); length > 0; length--) {
                if (safe.endsWith(secret.slice(0, length))) {
                    safe = safe.slice(0, -length) + "[REDACTED_PARTIAL]";
                    break;
                }
            }
        }
    }
    return safe.replace(/\bsk-[\w-]+/g, "[REDACTED]").replace(/(Bearer\s+)[\w.\-/+=]+/gi, "$1[REDACTED]")
        .replace(/((?:api[_-]?key|token|password|secret)\s*[=:]\s*["']?)[^\s"'&,;]+/gi, "$1[REDACTED]");
}
/** 只保存参数所需的有界预览；证据身份字段保留，源码保留摘要及可核对的hash。 */
export function sanitize(value, secrets = [], key = "", depth = 0) {
    if (secretKey.test(key))
        return "[REDACTED]";
    if (depth > 5)
        return "[DEPTH_LIMIT]";
    if (typeof value === "string") {
        const safe = redactText(value, secrets);
        if (["code", "content", "script", "source"].includes(key))
            return { preview: clip(safe, 2000), chars: value.length, truncated: safe.length > 2000, sha256: createHash("sha256").update(value).digest("hex"), language: key === "code" ? "javascript" : "text" };
        const limit = key === "public_reasoning" ? 8000 : 4000;
        return safe.length > limit ? clip(safe, limit) + `…[truncated; ${safe.length} chars]` : safe;
    }
    if (Array.isArray(value))
        return value.slice(0, 100).map(item => sanitize(item, secrets, key, depth + 1)).concat(value.length > 100 ? [`[${value.length - 100} more items]`] : []);
    if (value && typeof value === "object")
        return Object.fromEntries(Object.entries(value).slice(0, 60).map(([name, item]) => [name, sanitize(item, secrets, name, depth + 1)]));
    return value;
}
export function safeError(error, secrets = [], fallback = "TOOL_ERROR") {
    const candidate = error;
    return { code: typeof candidate?.code === "string" ? candidate.code : fallback, message: clip(String(sanitize(typeof candidate?.message === "string" ? candidate.message : String(error), secrets)), 1000) };
}
/** 成功/失败使用同一诊断上限。超量先缩短旧正文，再移除最旧条目并明确标记。 */
export function boundedDiagnostics(input) {
    const copy = JSON.parse(JSON.stringify(input));
    let trimmed = Boolean(copy.truncated);
    if (copy.trace.length > 1000) {
        copy.trace = copy.trace.slice(-1000);
        trimmed = true;
    }
    if (jsonBytes(copy) > 1024 * 1024) {
        for (const item of copy.trace) {
            if (item.reasoning_content && item.reasoning_content.length > 256) {
                item.reasoning_content = clip(item.reasoning_content, 256);
                item.reasoning_truncated = true;
            }
        }
        trimmed = true;
        while (copy.trace.length && jsonBytes(copy) > 1024 * 1024 - 128)
            copy.trace.shift();
    }
    if (trimmed)
        copy.truncated = true;
    return copy;
}
const FETCH_APIS = new Set(["openai-completions", "openai-responses", "azure-openai-responses", "anthropic-messages"]);
export function createModelTap(entry, model, context, base, live, secrets) {
    const started = performance.now();
    let providerReasoning = "", providerClipped = false, usageSeen = false;
    let reasoningTokens = null;
    // 留出最长已知密钥的完整尾部，先脱敏再截展示内容，防止截断点拆开密钥。
    const reasoningBufferLimit = 8000 + Math.max(256, ...secrets.map(secret => secret.length));
    function setReasoning(text, source, clipped = false) {
        const safe = redactText(text, secrets, true);
        entry.reasoning_source = source;
        entry.reasoning_content = clip(safe, 8000);
        entry.reasoning_truncated = clipped || safe.length > 8000;
    }
    const endpoints = [];
    entry.reasoning_content = null;
    entry.reasoning_source = null;
    entry.reasoning_truncated = false;
    entry.usage = null;
    entry.retry_count = null;
    entry.retry_observation = live ? "unavailable" : "not_applicable";
    entry.http_attempts = [];
    entry.request_summary = { message_count: context.messages.length, tool_names: [...new Set(context.messages.flatMap(message => message.role === "system" ? (message.toolsAdded ?? []).map(tool => tool.name) : []))] };
    const options = {
        ...base,
        onPayload: async (payload, actualModel) => {
            entry.request_summary.payload_bytes = jsonBytes(payload);
            return base.onPayload?.(payload, actualModel);
        },
        onResponse: async (response, actualModel) => {
            await base.onResponse?.(response, actualModel); // 不记录响应头，避免认证/会话信息落盘。
        },
        onProviderStreamEvent: async (data, actualModel) => {
            const chunk = data;
            const usages = [chunk?.usage, chunk?.response?.usage, chunk?.message?.usage, ...(chunk?.choices ?? []).map(choice => choice.usage)].filter(Boolean);
            if (usages.length)
                usageSeen = true;
            // Pi部分适配器把未提供的明细归一成0；只用原始供应商字段的实际出现值。
            for (const usage of usages) {
                const value = usage.completion_tokens_details?.reasoning_tokens ?? usage.output_tokens_details?.reasoning_tokens ?? usage.output_tokens_details?.thinking_tokens;
                if (typeof value === "number" && Number.isFinite(value))
                    reasoningTokens = value;
            }
            if (typeof chunk?.usageMetadata?.thoughtsTokenCount === "number")
                reasoningTokens = chunk.usageMetadata.thoughtsTokenCount;
            for (const choice of chunk?.choices ?? []) {
                const delta = choice.delta?.reasoning_content;
                if (typeof delta === "string" && live) {
                    const room = reasoningBufferLimit - providerReasoning.length;
                    providerReasoning += clip(delta, room);
                    if (delta.length > room)
                        providerClipped = true;
                    setReasoning(providerReasoning, "provider_reasoning_content", providerClipped);
                }
            }
            await base.onProviderStreamEvent?.(data, actualModel);
        },
    };
    // 仅Pi源码确认支持可注入fetch的HTTP适配器；WebSocket及特殊provider保留原传输。
    if (live && FETCH_APIS.has(model.api) && !String(base.transport ?? "").startsWith("websocket")) {
        const fetcher = base.fetch ?? globalThis.fetch;
        options.fetch = async (input, init) => {
            const time = performance.now();
            const endpoint = input instanceof Request ? `${input.method} ${input.url}` : `${init?.method ?? "GET"} ${String(input)}`;
            endpoints.push(endpoint);
            const attempt = { attempt: endpoints.length, started_at: new Date().toISOString(), duration_ms: 0, status_code: null, error: undefined };
            entry.http_attempts.push(attempt);
            entry.retry_observation = endpoints.every(item => item === endpoints[0]) ? "fetch" : "unavailable";
            entry.retry_count = entry.retry_observation === "fetch" ? endpoints.length - 1 : null;
            try {
                const response = await fetcher(input, init);
                attempt.status_code = response.status;
                if (!response.ok)
                    attempt.error = { code: `HTTP_${response.status}`, message: `模型HTTP请求返回${response.status}` };
                return response;
            }
            catch (error) {
                attempt.error = safeError(error, secrets, "HTTP_ERROR");
                throw error;
            }
            finally {
                attempt.duration_ms = performance.now() - time;
            }
        };
    }
    function finish(message, failure) {
        if (entry.status !== "running")
            return;
        entry.duration_ms = performance.now() - started;
        const code = failure?.code;
        entry.status = ["TIMEOUT", "CANCELLED"].includes(code ?? "") || message?.stopReason === "aborted" ? "cancelled" : failure || message?.stopReason === "error" ? "error" : "ok";
        if (failure || message?.errorMessage)
            entry.error = safeError(failure ?? new Error(message?.errorMessage), secrets, "PROVIDER_ERROR");
        if (!message)
            return;
        entry.response_bytes = jsonBytes(message);
        if (live && !entry.reasoning_source) {
            const publicThinking = message.content.filter(block => block.type === "thinking" && !block.redacted).map(block => block.type === "thinking" ? block.thinking : "").join("\n");
            if (publicThinking)
                setReasoning(publicThinking, "pi_thinking");
        }
        if (live && (usageSeen || message.usage.totalTokens > 0)) {
            const usage = message.usage;
            entry.usage = { input_tokens: usage.input, output_tokens: usage.output, reasoning_tokens: reasoningTokens, cache_read_tokens: usage.cacheRead, cache_write_tokens: usage.cacheWrite, total_tokens: usage.totalTokens, cost_total: usage.cost.total };
        }
    }
    return { options, finish };
}

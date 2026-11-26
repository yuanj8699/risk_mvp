import { Type } from "@earendil-works/pi-ai";
import { RiskAgentError, runAgent, toolStream } from "./runtime.js";
import { boundedDiagnostics } from "./model-tap.js";
const words = Type.String({ minLength: 1, maxLength: 1000 });
const arabicNumber = String.raw `\d+(?:[,，.．]\d+)*`;
const sign = "[+＋−－-]";
const chineseDigits = "零〇一二两三四五六七八九十";
const magnitude = "[万亿千百]";
const chineseNumber = `[${chineseDigits}][${chineseDigits}百千万亿]*(?:点[零一二三四五六七八九]+)?`;
const quantity = `(?:${sign}?${arabicNumber}|${chineseNumber})`;
// 只豁免这些固定用语中的“一户 / 一笔”，保留“三户”和“十一户一策”中的真实户数。
const qualitativeCount = "(?:(?<=单)一[户笔]|一户(?:一策|多贷)|一笔(?:一策|多押))";
// 有明确占比语境时，“占比三成为…”中的“三成”仍是比例。
const shareContext = "(?<=占比|占|约|近|超过|不足|达到)";
const numericFact = new RegExp([
    String.raw `(?:百分之|千分之)\s*${quantity}|${quantity}\s*(?:[%％‰]|个百分点|百分点|个?基点|倍)`,
    String.raw `(?!(?<=[单统唯逐])一成|一成不变)(?:${shareContext}${quantity}\s*成|${quantity}\s*成(?![因本员果长熟交分立为效绩就功败]))`,
    String.raw `${sign}?${arabicNumber}\s*${magnitude}*元`,
    // 汉字金额须含量级，如“三万元”“一百零五元”；“二元结构”可用于定性分析。
    String.raw `(?=[${chineseDigits}点]*\s*${magnitude})${chineseNumber}\s*${magnitude}*元`,
    String.raw `(?!${qualitativeCount})${quantity}\s*[笔户]|(?:余额|金额)\s*${quantity}\s*[万亿](?!\s*元)`,
    String.raw `(?:排名(?:第)?|位列(?:第)?|倒数(?:第)?)\s*${quantity}(?:名|位)?|第\s*${quantity}\s*(?:名|位)`,
    String.raw `(?:前|后)\s*${quantity}(?:名|位|(?=[，。、；\s]|$))`,
    String.raw `${sign}${arabicNumber}|\d+[.．]\d+`,
].join("|"));
// 自由文本可描述观察日期，具体数值交由证据呈现。
function findNumericFact(text) {
    const withoutDates = text.replace(/\b\d{4}-\d{1,2}(?:-\d{1,2})?\b/g, "日期");
    return withoutDates.match(numericFact)?.[0] ?? null;
}
function containsNumericFact(text) {
    return findNumericFact(text) !== null;
}
const issueSchema = Type.Object({
    org_id: words, dimension_id: words, aspect: words, title: words,
    severity: Type.Union([Type.Literal("重点关注"), Type.Literal("关注"), Type.Literal("提示")]),
    cause_analysis: words, check_suggestion: words,
    evidence_ids: Type.Array(Type.String({ minLength: 1 }), { minItems: 1, maxItems: 100, uniqueItems: true }),
}, { additionalProperties: false });
/** 按实际风险方面选择核查方向；原因只提出待核假设，不把方向判断冒充已查实事实。 */
function demoGuidance(aspect) {
    if (/大额/.test(aspect))
        return {
            cause: "大额贷款不良的变化需结合大额贷款余额、风险分类及集中情况核对，不能仅凭占比判断具体成因。",
            check: "核查大额贷款范围、风险分类和主要客户明细，区分余额变化与分母变化，并跟踪处置情况。",
            related: /大额|各项贷款|拨备/,
        };
    if (/关注类/.test(aspect))
        return {
            cause: "关注类贷款变化可能与风险分类迁徙和贷款规模变化有关，实际成因需结合相关余额及历史期次核实。",
            check: "核查关注类贷款分类迁徙、主要客户和到期偿还情况，并结合贷款余额变化复核占比。",
            related: /关注|各项贷款|不良/,
        };
    if (/逾期/.test(aspect))
        return {
            cause: "逾期变化需区分新增逾期、存量清收和贷款规模变动，现有筛查信号不能直接证明具体原因。",
            check: "核对逾期期限、主要客户、清收和风险分类迁徙，结合贷款余额复核逾期占比。",
            related: /逾期|各项贷款|不良/,
        };
    if (/还原/.test(aspect))
        return {
            cause: "还原指标涉及不良处置及口径调整，变化原因需结合相关余额和原始计算口径核实。",
            check: "核查还原计算公式及核销、转让等调整项目，与不良余额和贷款规模变化交叉核对。",
            related: /还原|不良|各项贷款/,
        };
    if (/不良贷款|新形成/.test(aspect))
        return {
            cause: "不良率变化可能来自不良余额变化或贷款分母变化；余额净增与新形成不良并不等同，需结合原始记录核实。",
            check: "核对不良余额、不良率、贷款余额及相关历史期次，区分新形成、清收处置和分母变化。",
            related: /不良|各项贷款|拨备/,
        };
    if (/拨备/.test(aspect))
        return {
            cause: "拨备覆盖水平可能受拨备余额与不良余额共同影响，需核对两者变化及拨备计提情况。",
            check: "复核拨备余额、不良余额及覆盖率口径，检查计提、核销和风险分类变化。",
            related: /拨备|不良/,
        };
    if (/资本/.test(aspect))
        return {
            cause: "资本充足水平受资本净额与风险加权资产共同影响，不能仅凭比率变化认定具体原因。",
            check: "核对资本净额、风险加权资产、利润留存及资产投向的变动和统计口径。",
            related: /资本|净利润|总资产/,
        };
    if (/盈利|净息差|利润/.test(aspect))
        return {
            cause: "盈利及净息差变化需结合利息收支、资产负债结构和规模变化核实，现有信号不能单独确认驱动因素。",
            check: "核对利息收入、利息支出、净利润及资产负债规模，区分规模变化和收益成本变化。",
            related: /利息|净息差|利润|总资产|存款/,
        };
    if (/债券/.test(aspect))
        return {
            cause: "债券投资规模和增速需结合资金来源、期限及持仓结构判断，规模变化本身不代表已发生损失。",
            check: "核对债券投资余额及历史变化，检查持仓品种、期限、集中度和估值情况。",
            related: /债券|资金业务|总资产/,
        };
    if (/资金/.test(aspect))
        return {
            cause: "资金业务占比或不良变化需分别核对业务资产、总资产及不良明细，不能将余额存在直接等同风险上升。",
            check: "核对资金业务资产及总资产口径、持仓结构和不良余额变化，跟踪存量资产处置情况。",
            related: /资金业务|总资产|不良/,
        };
    return { cause: "相关变化需结合对应指标、历史记录和业务明细核实，现有数据不足以确认具体原因。",
        check: "复核触发指标的口径和期次，结合关联指标及业务明细查明变化原因。", related: /贷款|存款|总资产/ };
}
function demoSubmission(candidates) {
    const issues = [];
    for (const candidate of candidates) {
        const groups = new Map();
        for (const signal of candidate.signals) {
            const key = JSON.stringify([signal.dimension_id, signal.aspect]);
            groups.set(key, [...(groups.get(key) ?? []), signal]);
        }
        for (const signals of groups.values()) {
            const first = signals[0];
            const guidance = demoGuidance(first.aspect);
            const related = candidate.evidence.filter(e => e.quality_status === "valid" && e.value !== null && e.usable !== false &&
                (e.role === "explain_change" || e.role === "context" || e.role === "impact") &&
                typeof e.indicator_name === "string" && guidance.related.test(e.indicator_name));
            const evidence_ids = [...new Set([...signals.map(s => s.evidence_id), ...related.slice(0, 3).map(e => e.evidence_id)])];
            issues.push({ org_id: candidate.org_id, dimension_id: first.dimension_id, aspect: first.aspect,
                title: containsNumericFact(first.aspect) ? "相关风险值得关注" : `${first.aspect}值得关注`, severity: signals.length > 1 ? "重点关注" : "关注",
                cause_analysis: guidance.cause, check_suggestion: guidance.check, evidence_ids });
        }
    }
    return { issues, summary: "候选机构涉及多个风险方面，建议按事项核对原始数据和业务明细，结合历史变化进一步分析。" };
}
function demoStream(candidates) {
    return model => toolStream(model, "submit_risk_synthesis", demoSubmission(candidates));
}
/** 核对机构、风险方面及证据引用。自由文本可写日期与定性概括，具体指标数值由证据呈现。 */
function validateSubmission(result, candidates) {
    const byOrg = new Map(candidates.map(c => [c.org_id, c]));
    const covered = new Set();
    const key = (org, dimension, aspect) => JSON.stringify([org, dimension, aspect]);
    for (const issue of result.issues) {
        const candidate = byOrg.get(issue.org_id);
        if (!candidate)
            throw new Error("事项引用了本批次以外的机构");
        const matching = candidate.signals.filter(s => s.dimension_id === issue.dimension_id && s.aspect === issue.aspect);
        const validIds = new Set(candidate.evidence.filter(e => e.quality_status === "valid" && e.value !== null && e.usable !== false).map(e => e.evidence_id));
        if (!matching.length || !issue.evidence_ids.some(id => matching.some(s => s.evidence_id === id)) || issue.evidence_ids.some(id => !validIds.has(id)))
            throw new Error("事项须引用同机构的有效证据及该风险方面的触发信号");
        for (const field of ["title", "cause_analysis", "check_suggestion"]) {
            const matched = findNumericFact(issue[field]);
            if (matched)
                throw new Error(`事项（${issue.org_id} / ${field}）包含定量表述“${matched}”，请用定性表述，可保留日期，通过证据引用展示具体指标数值`);
        }
        covered.add(key(issue.org_id, issue.dimension_id, issue.aspect));
    }
    const matched = findNumericFact(result.summary);
    if (matched)
        throw new Error(`综述包含定量表述“${matched}”，请用定性表述，可保留日期，通过证据引用展示具体指标数值`);
    if (candidates.some(c => c.signals.some(s => !covered.has(key(c.org_id, s.dimension_id, s.aspect)))))
        throw new Error("不能遗漏候选机构的风险方面");
}
async function synthesizeBatch(request, candidates, cfg, deadline) {
    let output;
    const tool = {
        name: "submit_risk_synthesis", label: "提交风险事项归并", description: "按机构和风险方面归并；每项引用本机构的触发证据和相关观测，不能捏造数值。",
        parameters: Type.Object({ issues: Type.Array(issueSchema, { minItems: 1, maxItems: 200 }), summary: words }, { additionalProperties: false }),
        execute: async (_, args) => {
            const result = args;
            validateSubmission(result, candidates);
            output = result;
            return { content: [{ type: "text", text: "已接受归并结果" }], details: { evidence_ids: [...new Set(result.issues.flatMap(i => i.evidence_ids))] }, terminate: true };
        },
    };
    const remainingMs = deadline - Date.now();
    const timeoutMs = cfg.env.RISK_PI_TIMEOUT_MS?.trim()
        ? cfg.limits.timeoutMs
        : Math.min(120000, Math.max(30000, remainingMs));
    return runAgent({ ...cfg, limits: { ...cfg.limits, timeoutMs } }, [tool], "分析输入的候选机构，合并同一风险方面的信号并提交事项。", () => output, remainingMs, {
        phase: "synthesize_risks", demoStream: demoStream(candidates), done: () => output !== undefined, maxTokens: 10000,
        systemPrompt: `你是银行风险分析助手。基于内置参考筛查和真实观测归并事项，关联解释指标与历史观察；明确成因尚待核实，不得认定责任或编造事实。自由文本可以使用定性概括，不填写具体指标金额、笔数、户数、比率、百分点、增幅或名次，具体数据由引用证据呈现。每批综述写一至两句。每个风险方面至少一项，每项引用该机构对应触发信号的证据，关联其他证据也只能引用同机构有效观测。请调用提交工具。\n内置知识：\n${request.knowledge_prompt}`,
        requestContext: () => ({ dimension_ids: request.dimension_ids, validation_summary: request.validation_summary, candidates }),
    });
}
export async function synthesizeRisks(request, cfg) {
    const started = performance.now(), deadline = Date.now() + request.budget_ms;
    const batches = [];
    for (let i = 0; i < request.candidates.length; i += 5)
        batches.push(request.candidates.slice(i, i + 5));
    const results = [];
    // 限制并发和单次上下文，失败时保留已经完成的诊断，不回退成演示结果。
    for (let i = 0; i < batches.length; i += 6) {
        if (Date.now() >= deadline)
            throw new RiskAgentError("TIMEOUT", "风险归并达到总时间上限", { trace: results.flatMap(r => r.trace) });
        const completed = await Promise.allSettled(batches.slice(i, i + 6).map(batch => synthesizeBatch(request, batch, cfg, deadline)));
        for (const entry of completed) {
            if (entry.status === "fulfilled")
                results.push(entry.value);
            else {
                const error = entry.reason instanceof RiskAgentError ? entry.reason : new RiskAgentError("RUNTIME_ERROR", "风险归并未完成");
                error.diagnostics = boundedDiagnostics({ trace: [...results.flatMap(r => r.trace), ...(error.diagnostics?.trace ?? [])], runtime: error.diagnostics?.runtime });
                throw error;
            }
        }
    }
    const trace = results.flatMap(r => r.trace);
    const runtime = { framework: "pi-agent-core", version: "1.4.0", provider: cfg.model.provider, model: cfg.model.id,
        requests: results.reduce((n, r) => n + r.runtime.requests, 0), tool_calls: results.reduce((n, r) => n + r.runtime.tool_calls, 0),
        mode: cfg.mode, llm_connected: cfg.mode === "model", duration_ms: performance.now() - started };
    const scope = request.validation_summary.visibility?.clipped === true ? "可见行社" : "全辖";
    return { mode: `pi-${cfg.mode}`, issues: results.flatMap(r => r.value.issues),
        executive_summary: request.candidates.length ? `${scope}筛查定位 ${request.candidates.length} 家存在参考筛查信号的行社。${[...new Set(results.map(r => r.value.summary))].join(" ")}` : "本次参考筛查未定位到命中信号的行社；缺源和缺数情况请结合口径校验摘要查看。",
        trace, runtime };
}

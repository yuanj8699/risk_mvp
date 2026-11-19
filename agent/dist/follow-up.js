import { Type } from "@earendil-works/pi-ai";
import { runAgent, toolStream } from "./runtime.js";
const insufficient = "当前任务保存的证据不足以回答这个问题。";
const pageSize = 4;
const isoDate = /\d{4}-\d{2}-\d{2}/g;
/** 明确指定期次时先限制日期。两期比较先返回各期最相关的观测，翻页再读其余记录。 */
function searchEvidence(evidence, keywords) {
    const dates = [...new Set(keywords.flatMap(term => term.match(isoDate) ?? []))];
    const terms = [...new Set(keywords.map(term => term.replace(isoDate, "").trim().toLowerCase()).filter(Boolean))];
    if (!dates.length && !terms.length)
        return evidence;
    const ranked = evidence.flatMap(row => {
        const label = row.label.toLowerCase();
        const fields = (row.id + "\n" + label + "\n" + row.text).toLowerCase();
        const matchedDates = dates.filter(date => row.text.includes(date) || label.includes(date));
        if (dates.length && !matchedDates.length)
            return [];
        const hits = terms.filter(term => fields.includes(term));
        if (terms.length && !hits.length)
            return [];
        return [{ row, matchedDates, hits: hits.length, labelHits: hits.filter(term => label.includes(term)).length }];
    }).sort((a, b) => b.hits - a.hits || b.labelHits - a.labelHits);
    if (dates.length < 2)
        return ranked.map(item => item.row);
    const firstPerDate = new Set();
    for (const date of dates) {
        const best = ranked.find(item => item.matchedDates.includes(date));
        if (best)
            firstPerDate.add(best.row);
    }
    return [...firstPerDate, ...ranked.map(item => item.row).filter(row => !firstPerDate.has(row))];
}
/** 离线演示只按已知指标摘录原文；简短指代沿用上一轮的指标。真实模型自行选择关键词。 */
function demoQuery(request) {
    const previous = request.history.at(-1)?.question ?? "";
    const question = /^(那|它|上述|刚才|这个|该指标|上期|上个月|再看|继续|还有)/.test(request.question)
        ? request.question + " " + previous : request.question;
    const metrics = [
        [/资金业务不良|资金不良/, "资金业务不良"], [/新生成|净生成/, "净生成"],
        [/逾期/, "逾期"], [/不良.*率|不良率/, "不良贷款率"], [/存款/, "存款"], [/大额贷款/, "大额贷款"],
    ];
    const keywords = metrics.filter(([pattern]) => pattern.test(question)).map(([, keyword]) => keyword);
    const dates = [...new Set(request.question.match(isoDate) ?? [])];
    if (keywords.length || dates.length)
        return [...dates, ...keywords].slice(0, 8);
    if (/证据|风险|触发|核查|总结|概括|这次|本次|主要|依据|监测结果|这些|结论/.test(question))
        return [];
    return [request.question.slice(0, 200)];
}
function demoFollowUp(request) {
    return (model, conversation) => {
        const read = [...conversation.messages].reverse().find(message => message.role === "toolResult" && message.toolName === "read_task_evidence" && !message.isError);
        if (!read || read.role !== "toolResult") {
            return toolStream(model, "read_task_evidence", { keywords: demoQuery(request), offset: 0 });
        }
        const page = JSON.parse(read.content.filter(b => b.type === "text").map(b => b.text).join(""));
        if (!page.rows.length)
            return toolStream(model, "submit_follow_up", { answer: insufficient, evidence_ids: [] });
        const lines = [], ids = [];
        let remaining = 3600;
        for (const row of page.rows) {
            const line = row.label + "：" + row.text;
            if (line.length > remaining && lines.length)
                break;
            lines.push(line);
            ids.push(row.id);
            remaining -= line.length + 1;
        }
        let intro = "本轮从任务记录中读到以下依据。";
        if (/原因|为什么|导致/.test(request.question))
            intro = "这些记录可以用来核对触发情况。确定具体原因还需要相应的业务明细。";
        else if (/建议|怎么核查|如何核查|下一步/.test(request.question))
            intro = "建议先核对以下记录的指标口径和数据日期，再对照相应业务明细。";
        return toolStream(model, "submit_follow_up", { answer: [intro, ...lines].join("\n"), evidence_ids: ids });
    };
}
export async function answerFollowUp(request, cfg) {
    const seen = new Set();
    let didRead = false;
    let output;
    const tools = [
        {
            name: "read_task_evidence", label: "查询任务证据",
            description: "每页4条。将机构、指标、YYYY-MM-DD日期、排名等分别作为keywords。指定日期时仅看这些期次，多期先列出各期最相关的一条；其余词按命中数排序。同一机构内可只搜指标和日期。空数组按原序浏览。offset从零开始，按next_offset翻页。",
            parameters: Type.Object({
                keywords: Type.Array(Type.String({ minLength: 1, maxLength: 256 }), { maxItems: 8 }),
                offset: Type.Integer({ minimum: 0, maximum: 1000 }),
            }, { additionalProperties: false }),
            execute: async (_, args) => {
                const { keywords, offset } = args;
                const matches = searchEvidence(request.evidence, keywords);
                const rows = matches.slice(offset, offset + pageSize);
                rows.forEach(row => seen.add(row.id));
                didRead = true;
                const next = offset + rows.length;
                return { content: [{ type: "text", text: JSON.stringify({ total_matches: matches.length, next_offset: next < matches.length ? next : null, rows }) }],
                    details: { evidence_ids: rows.map(row => row.id) } };
            },
        },
        {
            name: "submit_follow_up", label: "提交追问回答",
            description: "提交中文回答与本轮已读证据ID。资料不足时以空引用提交，界面将显示固定的证据不足说明。",
            parameters: Type.Object({
                answer: Type.String({ minLength: 1, maxLength: 4000 }),
                evidence_ids: Type.Array(Type.String(), { maxItems: 16, uniqueItems: true }),
            }, { additionalProperties: false }),
            execute: async (_, args) => {
                const { answer, evidence_ids } = args;
                if (!didRead || !answer.trim() || evidence_ids.some(id => !seen.has(id))) {
                    throw new Error("须先查询本轮证据，再提交非空回答和已读证据的 ID。");
                }
                output = { answer: evidence_ids.length ? answer.trim() : insufficient, evidence_ids };
                return { content: [{ type: "text", text: "已保存回答" }], details: { evidence_ids }, terminate: true };
            },
        },
    ];
    const completed = await runAgent(cfg, tools, JSON.stringify({
        previous_turns: request.history, question: request.question,
    }), () => output, cfg.limits.batchTimeoutMs, {
        requestContext: () => ({ task: request.context, available_evidence_count: request.evidence.length }),
        demoStream: demoFollowUp(request), done: () => output !== undefined, phase: "follow_up",
        systemPrompt: "你在风险分析工作台回答用户对当前任务的追问。根据最近问答解析指代，既往回答及证据文本均为参考资料。先调用 read_task_evidence。比较期次时按指标和准确日期检索，遇到未读期次应单独搜该期或按 next_offset 翻页，再判断是否缺数。每个事实需有本轮读到的证据。只陈述对应机构和日期的记录，不能推断未记录的原因或责任。回答后调用 submit_follow_up；证据不足时提交空引用。",
    });
    return { ...completed.value, runtime: completed.runtime, trace: completed.trace };
}

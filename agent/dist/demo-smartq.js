const province = "全省合计（含宁波）";
const allBanks = "全部行社";
const dateWords = /(?:(\d{4})[-年/]?)?(\d{1,2})(?:月(?:末|底|(?:(\d{1,2})日)?)|[-/](\d{1,2}))/g;
const explicitCondition = /[＞>＜<≥≤]\s*\d|(?:大于|高于|超过|小于|低于|不高于|不低于|不足)\s*\d+(?:\.\d+)?|排名前\s*\d+%?|排名后\s*\d+%?/;
const kinds = [
    ["check", /校验|核验|核对|是否一致/, "校验"],
    ["yoy", /同比|比去年同期/, "同比"],
    ["mom", /环比|(?:比|较)上个?月/, "比上月"],
    ["rank", /排名|排行|前\s*[\d一二三四五六七八九十]+%?|后\s*[\d一二三四五六七八九十]+%?|第几/, "排名"],
    ["filter", /[＞>＜<≥≤]|(?:大于|高于|超过|小于|低于|不高于|不低于|不足)\s*\d|浮亏|亏损/, "条件筛查"],
    ["extremum", /最高|最低|最大|最小|极值/, "极值"],
    ["count", /计数|笔数|户数|多少[笔家户]|几[笔家户]/, "计数"],
    ["ratio", /占比|占.{1,10}(?:比例|比重)|比重/, "占比"],
    ["diff", /差值|差额|相差|差多少|差异|相差多少|比较.*(?:与|和)/, "差值"],
    ["detail", /明细|下钻|拆[分开]|细分|分布|分组|按.{1,12}(?:分|看|列|统计)/, "明细"],
    ["total", /合计|总计|加总|总和|汇总/, "合计"],
];
const reference = /(?:它|这家|该行|刚才|这个指标|该指标|上期|上个?月|上述|前面|^那|^继续|^再看|^按)/;
const generic = /^(?:(?:这次|本次|本轮|任务|当前|主要|有哪些|有什么|查询|给我|的)\s*)*(?:证据|风险|触发|核查|总结|概括|风险情况|检查发现了什么|监测结果|结论)$/;
/** 面向开放式风险追问的监管底线与业务预警参考规则（用户未指定具体阈值时自主补齐）。 */
const domainRules = [
    {
        match: /(?:不良贷款率|不良率).*(?:偏高|过高|预警|超标|较高|哪几家|哪些行社|风险)|(?:偏高|过高|较高).*(?:不良贷款率|不良率)/,
        metric: "五级不良贷款率", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "五级不良贷款率＞1.5%的行社是哪几家，分别是多少",
        singleBankClause: "五级不良贷款率是多少",
        reason: "参照银行业不良率 1.5% 行业参考预警线（非监管红线）自主补充筛查条件。",
    },
    {
        match: /(?:不良贷款余额|不良余额).*(?:偏大|较大|前|高|哪几家|哪些行社)/,
        metric: "五级不良贷款余额", queryType: "rank", action: "排名", rate: false,
        allBanksClause: "五级不良贷款余额排名前15%的行社是哪几家，分别是多少",
        singleBankClause: "五级不良贷款余额及全省排名是多少",
        reason: "参照业务 Excel 单项指标口径（排名前15%）自主补充筛查条件。",
    },
    {
        match: /拨备覆盖率.*(?:偏低|不足|过低|告急|不达标|红线|哪几家|哪些行社)|拨备(?:不足|告急|偏低)/,
        metric: "拨备覆盖率", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "拨备覆盖率＜150%的行社是哪几家，分别是多少",
        singleBankClause: "拨备覆盖率及较年初增长点是多少",
        reason: "参照银发〔2018〕7号拨备覆盖率 150% 监管稳健线（最低差异化底线 120%）自主补充筛查条件。",
    },
    {
        match: /资本充足率.*(?:不达标|偏低|不足|红线|哪几家|哪些行社)|踩资本红线|资本(?:不达标|不足)/,
        metric: "资本充足率", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "资本充足率＜10.5%的行社是哪几家，分别是多少",
        singleBankClause: "资本充足率、一级资本充足率和核心一级资本充足率分别是多少",
        reason: "参照《商业银行资本管理办法》资本充足率 10.5% 法定监管底线自主补充筛查条件。",
    },
    {
        match: /净息差.*(?:偏低|收窄|下滑|过低|哪几家|哪些行社)|息差(?:偏低|收窄)/,
        metric: "净息差", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "净息差＜1.8%的行社是哪几家，分别是多少",
        singleBankClause: "净息差及同比增长点是多少",
        reason: "参照项目盈利监测参考线（净息差＜1.8%）自主补充筛查条件。",
    },
    {
        match: /流动性(?:比例)?.*(?:偏低|不足|不达标|红线|风险|哪几家|哪些行社)/,
        metric: "流动性比例", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "流动性比例＜25%的行社是哪几家，分别是多少",
        singleBankClause: "流动性比例及核心负债比例分别是多少",
        reason: "参照《商业银行风险监管核心指标》流动性比例 25% 法定监管底线自主补充筛查条件。",
    },
    {
        match: /(?:单一集团|大客户|客户授信)?集中度.*(?:过高|偏高|超标|红线|风险|哪几家|哪些行社)/,
        metric: "单一集团客户授信集中度", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "单一集团客户授信集中度＞10%的行社是哪几家，分别是多少",
        singleBankClause: "单一集团客户授信集中度及全部关联度分别是多少",
        reason: "参照《商业银行风险监管核心指标》单一集团客户授信集中度 10% 法定监管底线自主补充筛查条件。",
    },
    {
        match: /资金业务.*(?:占比过高|占比偏高|比例过高|风险|哪几家|哪些行社)/,
        metric: "资金业务总资产占总资产比例", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "资金业务总资产占总资产比例＞40%的行社是哪几家，分别是多少",
        singleBankClause: "资金业务总资产占总资产比例是多少",
        reason: "参照项目监测参考线（资金业务总资产占总资产比例＞40%）自主补充筛查条件。",
    },
    {
        match: /债券.*(?:浮亏|公允价值.*亏)|浮亏.*债券/,
        metric: "本币债券公允价值变动余额", queryType: "filter", action: "条件筛查", rate: false,
        allBanksClause: "本币债券公允价值变动余额＜0的行社是哪几家，分别是多少",
        singleBankClause: "本币债券公允价值变动余额及加权平均修正久期分别是多少",
        reason: "参照业务 Excel 债券市场风险问法，筛查处于公允价值浮亏状态的行社。",
    },
    {
        match: /(?:农小定位|支农支小|涉农及小微).*?(?:弱化|不足|不达标|偏低|哪几家|哪些行社)/,
        metric: "涉农及小微企业贷款占比", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "涉农及小微企业贷款占比＜70%的行社是哪几家，分别是多少",
        singleBankClause: "涉农及小微企业贷款占比及普惠贷款增速分别是多少",
        reason: "参照业务定位监测标准（涉农及小微企业贷款占比＜70%）自主补充筛查条件。",
    },
    {
        match: /(?:票贷比|承兑汇票|票据业务).*(?:过高|增长快|较快|依赖|风险|哪几家|哪些行社)/,
        metric: "票贷比", queryType: "filter", action: "条件筛查", rate: true,
        allBanksClause: "票贷比＞2%的行社是哪几家，分别是多少",
        singleBankClause: "票贷比及承兑汇票增长率分别是多少",
        reason: "参照业务定位监测标准（票贷比＞2%）自主补充筛查条件。",
    },
    {
        match: /异地贷款.*(?:风险|偏高|较多|集中|哪几家|哪些行社)/,
        metric: "异地贷款余额（不含转贴现）", queryType: "rank", action: "排名", rate: false,
        allBanksClause: "异地贷款余额（不含转贴现）排名前15%的行社是哪几家，分别是多少",
        singleBankClause: "异地贷款余额（不含转贴现）及较年初变化是多少",
        reason: "参照重点业务管理单项指标口径（排名前15%）自主补充筛查条件。",
    },
];
function monthEnd(year, month) {
    return new Date(Date.UTC(year, month, 0)).toISOString().slice(0, 10);
}
function displayDate(date) {
    const [year, month, day] = date.split("-");
    return date === monthEnd(Number(year), Number(month))
        ? `${year}年${month}月末` : `${year}年${month}月${day}日`;
}
function resolvePeriods(question, base) {
    const found = [...question.matchAll(dateWords)].map(([, y, m, d, isoDay]) => {
        const year = Number(y || base.slice(0, 4)), month = Number(m), day = Number(d || isoDay || 0);
        if (month < 1 || month > 12 || !Number.isInteger(year) || year < 1900)
            return "";
        const end = monthEnd(year, month);
        if (day > Number(end.slice(8)))
            return "";
        return day ? end.slice(0, 8) + String(day).padStart(2, "0") : end;
    });
    if (found.length)
        return found;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(base))
        return [];
    if (/上期|上个?月/.test(question)) {
        const previous = monthEnd(Number(base.slice(0, 4)), Number(base.slice(5, 7)) - 1);
        return /(?:比|较|与|和)上(?:个?月|期)/.test(question) ? [base, previous] : [previous];
    }
    return [base];
}
function queryOrg(question, scope, previous) {
    if (!/本次任务范围|全辖|全省/.test(scope))
        return scope;
    // 全辖视角可能点名某一行社；在单行社视角保留其他机构词，由宿主据原句拒绝越界。
    const named = question.match(/([\u4e00-\u9fff]{1,16}(?:农村商业银行|农村信用合作联社|农商银行|农商行))/)?.[1];
    if (named && !/各行社|家行社|全部行社|哪些行社/.test(named))
        return named.replace(/^.*(?:查询|请问|查看|对比|月末|月|日|那)/, "");
    if (/全部行社|哪些行社|哪几家|的行社|各行社|各机构|行社排名/.test(question))
        return allBanks;
    if (/全省|全辖/.test(question))
        return province;
    return previous || (/82\s*家|全省|全辖/.test(scope) ? province : "");
}
/** 保留指标、单位、阈值和下钻限定；问句中的动作、指代及已结构化的期次不作为指标关键词。 */
function subject(question, org) {
    return question.replace(dateWords, "").replaceAll(org, "")
        .replace(/这家(?:行社|机构|银行)?|该行社|该行|它(?:的)?|这个指标|该指标|刚才(?:的)?|上述(?:指标)?/g, "")
        .replace(/(?:比|较)上个?月|上期|上个?月(?:末)?|本期|当期|本次|截至|截止|到期末/g, "")
        .replace(/^(?:那[么]?|继续|再看|请问|请|帮我|查询|查看|想看|看看|一下|查|看|在?全部行社中?|在|的|为|[\s，、：])+/g, "")
        .replace(/(?:分别)?(?:是多少|有多少|为多少|多少|是什么|怎么样|如何|情况|有哪些记录|有什么记录|呢|吗|啊|[？?。 ，])+$/g, "")
        .trim();
}
export function demoLookup(request) {
    const overview = /归档|证据|总结|概括|本次检查|主要风险/.test(request.question);
    if (overview && !/率|余额|金额|贷款|存款|表外|明细|下钻|同比|占比|排名|拨备|资本|息差|流动性|集中度/.test(request.question))
        return undefined;
    const allScope = /本次任务范围|全辖|全省/.test(request.context.organization);
    let lastMetric = "", lastIntent = "", lastOrg = request.context.organization, lastDate = request.context.as_of;
    let lookup;
    for (const question of [...request.history.map(h => h.rewritten_question ?? h.question), request.question]) {
        const inherit = reference.test(question);
        const periods = resolvePeriods(question, inherit ? lastDate : request.context.as_of);
        const org = queryOrg(question, request.context.organization, inherit && lastOrg !== request.context.organization ? lastOrg : "");
        // 若用户提出开放式风险追问且未指定具体数值阈值，按银行监管/业务预警知识自主补齐问句条件
        if (!explicitCondition.test(question) && periods.length && !periods.includes("")) {
            const matchedRule = domainRules.find(rule => rule.match.test(question));
            if (matchedRule) {
                const isAll = allScope && (org === allBanks || org === province || /哪些|哪几家|全部|全省/.test(question));
                const targetOrg = isAll ? allBanks : (org || request.context.organization);
                const clause = isAll ? matchedRule.allBanksClause : matchedRule.singleBankClause;
                const rewritten = isAll
                    ? `${periods.map(displayDate).join("与")}，${allBanks}中${clause}？（${matchedRule.action}${matchedRule.rate ? "，按原口径取值" : ""}）`
                    : `${periods.map(displayDate).join("与")}，${targetOrg}，查询${clause}（单值${matchedRule.rate ? "，按原口径取值" : ""}）。`;
                lookup = {
                    rewritten_question: rewritten,
                    reason: matchedRule.reason,
                    metric_hint: matchedRule.metric,
                    target_org: targetOrg,
                    target_date: periods[0],
                    query_type: isAll ? matchedRule.queryType : "single",
                };
                lastMetric = matchedRule.metric;
                lastIntent = clause;
                lastOrg = targetOrg;
                lastDate = periods[0];
                continue;
            }
        }
        const canonical = question.replace(/（(?:单值|合计|差值|占比|排名|极值|明细|计数|同比|比上月|校验|条件筛查)(?:，按原口径取值)?）。?$/, "");
        const body = subject(org === province ? canonical.replace(/全省(?:合计)?(?:[（(]含宁波[)）])?/g, province) : canonical, org);
        const metricOnly = body
            .replace(/^按.+?(?:查看|查询|统计|看|列出)/, "")
            .replace(/^(?:校验|核对|合计|汇总|比较|与|和|哪些行社|哪几家|的)+/, "")
            .replace(/是否一致$|(?:要|需要)?(?:加总|求和)$|(?:有)?多少[笔家户]$|^(?:占比|比重)$|相比差$/, "")
            .replace(/(?:在全部行社中|在全省中)?(?:[＞>＜<≥≤]|大于|高于|超过|小于|低于|不高于|不低于|不足).*$/g, "")
            .replace(/(?:按|分)(?:行业|地区|客户|期限|产品|机构|类型|业务种类|担保方式).*|(?:在全部行社中)?(?:的)?(?:同比|环比|比上月|排名|排行|排第几|差值|差额|合计|加总|总和|汇总|明细|下钻|拆分|拆开|校验|核对|最高|最低|最大|最小|计数).*$/g, "")
            .replace(/[的，、：\s]+$/, "");
        const metric = metricOnly || (inherit ? lastMetric : "");
        lookup = undefined;
        if (!org || !periods.length || periods.includes("") || !metric || generic.test(metric))
            continue;
        const resolved = !body && inherit ? lastIntent : body.includes(metric) ? body : metric + (body ? "，" + body : "");
        // 全省合计是机构口径；率类按原值查询，不能把“合计”读作加总率值。
        const rate = /率|占比|比重|比例|息差|集中度|关联度/.test(resolved);
        const [type, , action] = kinds.find(([kind, re]) => re.test(resolved + question.replaceAll(org, "").replace(/全省合计/g, "全省")) && !(kind === "total" && rate))
            ?? ["single", /./, "单值"];
        let intent = resolved.replace(/比较(?:和|与)的|[，、]?(?:和|与)相比差$/g, "");
        if (rate)
            intent = intent.replace(/(?:要|需要)?(?:加总|求和|总和|汇总)|(?<!全省)合计/g, "");
        const rewritten = (org === allBanks && (type === "filter" || type === "rank"))
            ? `${periods.map(displayDate).join("与")}，${allBanks}中${intent.replace(/的行社(?:是哪几家|有哪些)?$/, "")}的行社是哪几家，分别是多少？（${action}${rate ? "，按原口径取值" : ""}）`
            : `${periods.map(displayDate).join("与")}，${org}，查询${intent}（${action}${rate ? "，按原口径取值" : ""}）。`;
        const reason = (type === "filter" || type === "rank")
            ? `归档缺少该条件筛查结果，按用户指定条件保留「${intent}」交小 Q 实时补查。`
            : "任务归档缺少回答该指标、期次或细分维度所需的记录。";
        if ([...rewritten].length <= 300 && [...metric].length <= 200)
            lookup = {
                rewritten_question: rewritten, reason,
                metric_hint: metric, target_org: org, target_date: periods[0], query_type: type,
            };
        lastMetric = metric;
        lastIntent = resolved;
        lastOrg = org;
        lastDate = periods[0];
    }
    return lookup;
}

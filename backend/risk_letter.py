"""按单家行社归档观测提供只读取证、规则研判和正式提示函的宿主复核。"""
from calendar import monthrange
from collections import defaultdict
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, localcontext
import operator
import re

from .analysis import AnalysisError, provider
from . import risk_knowledge as knowledge


FORBIDDEN = re.compile(
    r'[\r\n<>|]|[0-9a-f]{32}|[a-z]+_[a-z0-9_]+|TEST_METRIC_|'
    r'(?<![a-z])(?:rank|population|value|indicator|rows|evidence|usable|operator|params|snapshot)(?![a-z])|'
    r'（测试草稿）|(?:成因分析|核查建议|证据|原始指标记录)[：:]|待核】', re.I)
NUMERALS = '一二三四五六七八'
NUMBERING_PREFIX = re.compile(
    r'^\s*(?:[（(]?[一二三四五六七八九十\d]+[）)]|[一二三四五六七八九十\d]+(?:、|[.．](?!\d)))\s*')
OVERVIEW_IDS = ('TEST_METRIC_001', 'TEST_METRIC_002', 'TEST_METRIC_004',
                'TEST_METRIC_005', 'TEST_METRIC_010', 'deposit_growth')
COMPARE = {'<': operator.lt, '<=': operator.le, '>': operator.gt, '>=': operator.ge, '==': operator.eq}
PROSE_DECIMAL = re.compile(
    r'(?<![a-zA-Z0-9_.])(?P<value>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{3,})(?P<unit>%|％|个?百分点)')


def format_formal_display(value, unit, value_display):
    """从原值一次舍入到公文精度；计数等其他量纲沿用原展示。"""
    if unit not in ('%', '％', '百分点', '个百分点', '万元', '元'):
        return value_display
    amount = Decimal(str(value))
    places = 4 if unit.endswith('百分点') and 0 < amount.copy_abs() < Decimal('0.005') else 2
    with localcontext() as ctx:
        ctx.rounding = ROUND_HALF_UP
        grouping = ',' if unit in ('万元', '元') else ''
        formatted = format(amount, f'{grouping}.{places}f')
    return formatted + unit


def _formal_prose(match):
    value, unit = match.group('value', 'unit')
    display = format_formal_display(value.replace(',', ''), unit, match.group())
    return ('+' if value.startswith('+') else '') + display


def add_verified_formal_displays(result, indicators):
    """在核验通过后附加展示字段。比率之差使用百分点，情景结果保留推演语义。"""
    if result['status'] != 'ok':
        return
    op, summary = result['operator'], result['summary']
    unit = summary.get('unit')
    if op == 'consecutive_trend':
        unit = next(item['unit'] for item in indicators if item['indicator_id'] == summary['indicator_id'])

    def display(value, unit):
        return format_formal_display(value, unit, f'{value}{unit}')

    change_unit = '个百分点' if unit in ('%', '％') else unit
    if op == 'benchmark_deviation':
        summary['baseline_formal_display'] = display(summary['baseline_value'], unit)
    for row in result['rows']:
        if op == 'benchmark_deviation':
            row['formal_display'] = display(row['value'], unit)
            row['deviation_formal_display'] = display(row['deviation'], change_unit)
        elif op == 'consecutive_trend':
            row['formal_display'] = display(row['total_change'], change_unit)
            for point in row['series']:
                point['formal_display'] = display(point['value'], unit)
        elif op == 'metric_divergence':
            row['formal_display'] = '、'.join(display(row[f'value_{k}'], row[f'unit_{k}']) for k in ('a', 'b'))
        elif op == 'concentration_cr':
            row['formal_display'] = display(row['cr_pct'], '%')
            for org in row['top_orgs']:
                org['formal_display'] = display(org['value'], unit)
        elif op == 'incremental_contribution':
            row['formal_display'] = display(row['contribution_pct'], '%')
        elif op == 'bucket_distribution':
            row['formal_display'] = f"占有效行社{display(row['ratio_of_valid'], '%')}，占全部行社{display(row['ratio_of_total'], '%')}"
        elif op == 'stress_test':
            row['formal_display'] = (display(row['shocked_coverage'], '%') if 'shocked_coverage' in row
                                     else display(row['estimated_loss'], row['unit']))


def _clean(value):
    return ' '.join(re.sub(r'【[^】]*】', '', value or '').split())


def _chinese_date(day):
    return f'{day.year}年{day.month}月{day.day}日'


def _archive_metrics(case):
    """仅本行社可用实测有资格被引用。保留真实日期，不用历史值填补当期缺数。"""
    current, observed = {}, {}
    as_of = str(case.data_date)
    snapshot = case.analysis.get('snapshot_id')
    # 旧归档未单列快照 ID，月频和季频的当期可用值可能早于报告日。
    if not snapshot and hasattr(case, '_sa_instance_state'):
        from sqlalchemy import select
        from sqlalchemy.orm import object_session
        from .models import AnalysisRun
        session = object_session(case)
        if session is not None:
            snapshot = session.scalar(select(AnalysisRun.snapshot_id).where(AnalysisRun.id == case.run_id))
    for row in case.evidence:
        if (row.get('org_id') != case.org_id or not row.get('usable') or row.get('quality_status') != 'valid'
                or row.get('result_type') == 'simulated' or row.get('value') is None
                or not row.get('evidence_id') or str(row['data_date']) > as_of):
            continue
        # 展示标签不洁净不能使真实观测脱离底线检查。优先使用知识库名称和原量纲。
        name, display = _clean(row.get('indicator_name')), _clean(row.get('value_display'))
        if not name or FORBIDDEN.search(name):
            name = _clean(knowledge.INDICATORS.get(row['indicator_id'], {}).get('name'))
        if not display or FORBIDDEN.search(display):
            display = str(row['value']) + row['unit']
        if not name or FORBIDDEN.search(name) or FORBIDDEN.search(display) or FORBIDDEN.search(row['unit']):
            raise AnalysisError(f"归档实测 {row['indicator_id']} 缺少可用于公文的名称、数值或单位，请核对来源")
        rank, population = row.get('rank'), row.get('population')
        if (population != 82 or row.get('rank_scope') not in (None, '', 'province_societies')
                or type(rank) is not int or not 1 <= rank <= population):
            rank, population = None, None
        record = dict(indicator_id=row['indicator_id'], indicator_name=name, value=str(row['value']),
                      value_display=display, unit=row['unit'], data_date=str(row['data_date']),
                      rank=rank, population=population, evidence_id=row['evidence_id'])
        record['formal_display'] = format_formal_display(record['value'], record['unit'], display)
        for key in ('dimension_id', 'dimension_title', 'aspect'):
            if row.get(key):
                record[key] = _clean(row[key])
        observed[record['evidence_id']] = record
        is_current = row.get('snapshot_id') == snapshot if snapshot else record['data_date'] == as_of
        if is_current:
            current[record['indicator_id']] = record
    return current, observed


def _dimension_title(ident, hint=None):
    return knowledge.DIMENSIONS.get(ident, {}).get('title') or hint or '其他风险'


def _metric_details(row, dimension_id=None, context=None):
    spec = knowledge.INDICATORS.get(row['indicator_id'], {})
    context = context or {}
    dimensions = spec.get('dimensions') or []
    dim = dimension_id or context.get('dimension_id') or row.get('dimension_id') or next(iter(dimensions), '')
    aspect = _clean(context.get('aspect') or row.get('aspect') or spec.get('aspect') or row['indicator_name'])
    return {**row, 'dimension_id': dim, 'dimension_title': _dimension_title(dim, context.get('dimension_title') or row.get('dimension_title') or aspect),
            'aspect': aspect,
            'meaning': _clean(spec.get('meaning') or spec.get('interpretation') or spec.get('definition')),
            'check_suggestion': _clean(spec.get('check_suggestion') or context.get('check_suggestion'))}


def _scan_context(case):
    """归档事项的建议与维度可补充知识库，不能用背景指标替代已归档的触发指标。"""
    issues = case.analysis.get('issues', [])
    by_aspect = {(i['dimension_id'], i.get('aspect')): i for i in issues}
    signals = []
    seen_groups = set()
    for sig in case.analysis.get('signals', []):
        group = (sig['dimension_id'], sig.get('aspect'))
        signals.append({**by_aspect.get(group, {}), **sig})
        seen_groups.add(group)
    for issue in issues:
        if (issue['dimension_id'], issue.get('aspect')) not in seen_groups:
            signals.extend({**issue, 'evidence_id': ref} for ref in issue.get('evidence_ids', []))
    return signals


def fetch_org_metrics_for_case(case, dimension_id=None, indicator_ids=None, keyword=None, include_history=False):
    """按指标、维度和关键词交集返回实测事实；额外历史仅按该指标实际月末期次列出。"""
    current, observed = _archive_metrics(case)
    signals = defaultdict(list)
    for signal in _scan_context(case):
        signals[signal['evidence_id']].append(signal)
    series = defaultdict(list)
    if include_history:
        for point in observed.values():
            day = date.fromisoformat(point['data_date'])
            if day.day == monthrange(day.year, day.month)[1] or point == current.get(point['indicator_id']):
                series[point['indicator_id']].append(point)
    metrics, refs = [], []
    for ident, row in current.items():
        if indicator_ids is not None and ident not in indicator_ids:
            continue
        contexts = signals[row['evidence_id']]
        dimensions = {c['dimension_id'] for c in contexts} | set(knowledge.INDICATORS.get(ident, {}).get('dimensions', []))
        if row.get('dimension_id'):
            dimensions.add(row['dimension_id'])
        if dimension_id and dimension_id not in dimensions:
            continue
        context = next((c for c in contexts if not dimension_id or c['dimension_id'] == dimension_id), None)
        metric = _metric_details(row, dimension_id, context)
        if keyword and not any(keyword.casefold() in metric[key].casefold() for key in
                               ('indicator_id', 'indicator_name', 'aspect', 'dimension_title', 'meaning')):
            continue
        if include_history:
            metric['history'] = sorted(series[ident], key=lambda r: r['data_date'])
            refs.extend(r['evidence_id'] for r in metric['history'])
        metrics.append(metric)
        refs.append(row['evidence_id'])
    return {'metrics': metrics, 'evidence_ids': list(dict.fromkeys(refs))}


def _evaluate(condition, current):
    """返回命中条件的实测指标 ID；未命中用 None，合法缺数条件用空集合表示。"""
    for join in ('all', 'any'):
        if join in condition:
            matches = [_evaluate(part, current) for part in condition[join]]
            hits = [hit for hit in matches if hit is not None]
            if not hits or (join == 'all' and len(hits) != len(matches)):
                return None
            return set.union(*hits)
    ident, op = condition['indicator_id'], condition['op']
    row = current.get(ident)
    if op == 'missing':
        return set() if row is None else None
    if row is None:
        return None
    if op == 'tail':
        matched = row['rank'] is not None and row['rank'] > row['population'] - condition['value']
    else:
        matched = COMPARE[op](Decimal(row['value']), Decimal(str(condition['value'])))
    return {ident} if matched else None


def _rules_for_case(case, current):
    clues, cautions, signals = [], [], []
    refs, suppressed = [], {}
    for rule in knowledge.get_letter_regulatory_floors():
        ids = _evaluate(rule['when'], current)
        ident = rule['indicator_id']
        if ids is None or ident not in current:
            continue
        row = _metric_details(current[ident])
        related = [_metric_details(current[k]) for k in sorted(ids - {ident})]
        clues.append({**row, 'rule_id': rule['rule_id'], 'label': rule['label'],
                      'check_suggestion': rule.get('check_suggestion') or row['check_suggestion'],
                      'related_metrics': related})
        refs.extend([row['evidence_id'], *(r['evidence_id'] for r in related)])

    for guard in knowledge.get_letter_caution_guards():
        ids = _evaluate(guard['when'], current)
        if ids is None:
            continue
        facts = [_metric_details(current[k]) for k in sorted(ids)]
        cautions.append({'rule_id': guard['rule_id'], 'message': guard['message'], 'metrics': facts,
                         'forbidden_patterns': guard['forbidden_patterns']})
        refs.extend(r['evidence_id'] for r in facts)
        retain = 'retain_when' in guard and _evaluate(guard['retain_when'], current) is not None
        for ident in guard['suppress_indicators']:
            suppressed[ident] = guard['retained_label'] if retain else None

    by_ref = {r['evidence_id']: r for r in current.values()}
    seen = set()
    for sig in _scan_context(case):
        row = by_ref.get(sig['evidence_id'])
        if row is None or (row['indicator_id'] in suppressed and not suppressed[row['indicator_id']]):
            continue
        ident = row['indicator_id']
        label = suppressed.get(ident) or _clean(sig.get('reason') or sig.get('title') or sig.get('aspect') or row['indicator_name'])
        # 阈值公式可用于计算，对外描述不携带表达式控制符。
        label = label.replace('>=', '大于等于').replace('<=', '小于等于').replace('>', '大于').replace('<', '小于')
        key = sig['dimension_id'], row['evidence_id'], label
        if key in seen:
            continue
        seen.add(key)
        metric = _metric_details(row, sig['dimension_id'], sig)
        if ident in suppressed:
            metric['aspect'] = label
        signals.append({**metric, 'label': label})
        refs.append(row['evidence_id'])
    return {'mandatory_p0_clues': clues, 'scan_signals': signals, 'cautions': cautions,
            'evidence_ids': list(dict.fromkeys(refs))}


def evaluate_letter_rules(case):
    """以当前有效观测匹配知识库，输出 P0、保留的扫描线索与良性口径。"""
    current, _ = _archive_metrics(case)
    return _rules_for_case(case, current)


def _brief(case, current, observed, rules):
    day = date.fromisoformat(str(case.data_date))
    org = case.org_name.strip()
    if not org.endswith(('银行', '农商行', '行社', '联社', '信用社')):
        org += '农商银行'
    dimensions = {dim: _dimension_title(dim) for ident in current
                  for dim in knowledge.INDICATORS.get(ident, {}).get('dimensions', [])}
    for row in current.values():
        if row.get('dimension_id'):
            dim = row['dimension_id']
            dimensions[dim] = _dimension_title(dim, row.get('dimension_title') or row.get('aspect'))
    for row in rules['scan_signals']:
        dimensions[row['dimension_id']] = row['dimension_title']
    return dict(run_id=case.run_id, org_id=case.org_id, org_name=org, region_name=case.region_name,
                data_date=day.isoformat(),
                period_label=f'{day.year}年{day.month}月末' if day.day == monthrange(day.year, day.month)[1] else _chinese_date(day),
                issue_date=_chinese_date(date.today()), business_overview=[current[k] for k in OVERVIEW_IDS if k in current],
                available_dimensions=[{'dimension_id': k, 'dimension_title': v} for k, v in sorted(dimensions.items())],
                signal_overview={'p0_count': len(rules['mandatory_p0_clues']), 'scan_signal_count': len(rules['scan_signals']),
                                 'caution_count': len(rules['cautions'])},
                mandatory_p0_evidence_ids=list(dict.fromkeys(r['evidence_id'] for r in rules['mandatory_p0_clues'])),
                allowed_evidence_ids=list(observed))


def build_letter_brief(case):
    """简报携带基本盘和范围；完整白名单仅供宿主及提交工具校验。"""
    current, observed = _archive_metrics(case)
    return _brief(case, current, observed, _rules_for_case(case, current))


def validate_letter(brief, letter, read_evidence_ids, cautions=()):
    """对跨进程提交重新执行正文、实测白名单、本轮已读证据和 P0 覆盖检查。"""
    def require(condition, message):
        if not condition:
            raise AnalysisError('风险提示函：' + message)

    def fields(obj, names):
        require(isinstance(obj, dict) and set(obj) == set(names.split()), '结构字段不符：' + names)

    def text(value, empty=False):
        require(isinstance(value, str) and (empty or value.strip()) and not FORBIDDEN.search(value),
                '正文为空或包含内部标记、哈希或排版控制字符')
        for guard in cautions:
            require(not any(re.search(p, value) for p in guard['forbidden_patterns']), guard['message'])

    allowed = set(brief['allowed_evidence_ids'])
    require(isinstance(read_evidence_ids, (list, tuple, set, frozenset))
            and all(isinstance(item, str) for item in read_evidence_ids), '缺少本轮实查证据清单')
    read = set(read_evidence_ids) | {row['evidence_id'] for row in brief['business_overview']}

    def refs(ids):
        require(isinstance(ids, list) and ids and all(isinstance(ref, str) for ref in ids)
                and len(ids) == len(set(ids)), '证据须为非空且不重复的引用')
        require(set(ids) <= allowed, '引用超出本行社实测白名单')
        require(set(ids) <= read, '先查后引：含本轮尚未实查的观测')

    fields(letter, 'title recipient opening overview risk_sections suggestions closing_requirement appendix_evidence_ids')
    for key in ('title', 'recipient', 'opening', 'overview', 'closing_requirement'):
        text(letter[key], key == 'overview' and not brief['business_overview'])
    require(letter['title'] == f"关于{brief['org_name']}{brief['period_label']}经营与风险情况的风险提示函"
            and letter['recipient'] == brief['org_name'] + '：', '标题、主送机构或期次不符')
    sections, suggestions = letter['risk_sections'], letter['suggestions']
    require(isinstance(sections, list) and 1 <= len(sections) <= 4, '风险段须为1至4段')
    require(isinstance(suggestions, list) and 1 <= len(suggestions) <= 5, '整改建议须为1至5条')
    cited = set()
    for section in sections:
        fields(section, 'heading body evidence_ids')
        refs(section['evidence_ids'])
        cited.update(section['evidence_ids'])
    require(set(brief['mandatory_p0_evidence_ids']) <= cited, '正文遗漏P0底线实测引用')
    for advice in suggestions:
        fields(advice, 'heading body')
    for item in [*sections, *suggestions]:
        text(item['heading'])
        text(item['body'])
    appendix = letter['appendix_evidence_ids']
    require(isinstance(appendix, list) and 1 <= len(appendix) <= 18, '附表须含1至18项实测引用')
    refs(appendix)


def _render(brief, structured, rows):
    parts = [structured[key] for key in ('title', 'recipient', 'opening')]
    parts.append('一、值得关注的主要风险与指标表现')
    if structured['overview']:
        parts.append(structured['overview'])
    for key in ('risk_sections', 'suggestions'):
        if key == 'suggestions':
            parts.append('二、风险防范与整改工作建议')
        for index, item in enumerate(structured[key]):
            heading = NUMBERING_PREFIX.sub('', item['heading']).rstrip('。')
            parts.append(f"（{NUMERALS[index]}）{heading}。{item['body']}")
    closing = NUMBERING_PREFIX.sub('', structured['closing_requirement'])
    parts.extend([f"（{NUMERALS[len(structured['suggestions'])]}）{closing}", '特此函告。',
                  '风险管理部\n' + brief['issue_date'], '附表：主要监测指标实测情况一览表'])
    table = ['| 监测指标 | 本期实测 | 全辖排名 | 数据日期 |', '| --- | --- | --- | --- |']
    for ref in structured['appendix_evidence_ids']:
        row = rows[ref]
        rank = f"第 {row['rank']} / {row['population']} 名" if row['rank'] else '—'
        display = format_formal_display(row['value'], row['unit'], row['value_display'])
        table.append(f"| {row['indicator_name']} | {display} | {rank} | {row['data_date']} |")
    # 编号、日期及表格的结构化列不参加正文的数值替换。
    body = PROSE_DECIMAL.sub(_formal_prose, '\n\n'.join(parts[2:]))
    return '\n\n'.join([*parts[:2], body, '\n'.join(table)])


def render_structured_letter(case, structured, read_evidence_ids):
    current, observed = _archive_metrics(case)
    rules = _rules_for_case(case, current)
    brief = _brief(case, current, observed, rules)
    validate_letter(brief, structured, read_evidence_ids, rules['cautions'])
    return _render(brief, structured, observed)


def compose_risk_letter(case) -> str:
    return compose_risk_letter_with_trace(case)[0]


def compose_risk_letter_with_trace(case) -> tuple[str, dict]:
    current, observed = _archive_metrics(case)
    rules = _rules_for_case(case, current)
    if not (rules['mandatory_p0_clues'] or rules['scan_signals']):
        raise AnalysisError('当期有效观测未达到突出风险条件，不生成提示函。')
    brief = _brief(case, current, observed, rules)
    result = provider.draft_risk_letter(brief)
    validate_letter(brief, result['letter'], result['read_evidence_ids'], rules['cautions'])
    return _render(brief, result['letter'], observed), result['diagnostics']


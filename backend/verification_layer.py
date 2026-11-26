"""将冻结的原子观测转换为可溯源的核验结果。所有计算在独立副本上进行。"""
from __future__ import annotations

import operator as comparison
from bisect import bisect_left, bisect_right
from copy import deepcopy
from datetime import date
from decimal import Decimal, ROUND_CEILING, localcontext
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, TypeAdapter, ValidationError

from . import metric_validator, verification_primitives as primitives
from .risk_knowledge import INDICATORS
from .services import number


class VerificationIssue(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def block(code, message):
    raise VerificationIssue(code, message)


def insufficient(message):
    raise VerificationIssue("INSUFFICIENT_DATA", message)


def require(condition):
    if not condition:
        block("POST_VERIFY_FAILED", "输出未通过证据溯源或算子恒等复核")


def _decimal(value):
    return primitives.num(value)


def _iso_date(value):
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("日期须使用 YYYY-MM-DD")
    return value


Numeric = Annotated[Decimal, BeforeValidator(_decimal)]
MetricId = Annotated[str, Field(min_length=1)]
Day = Annotated[str, AfterValidator(_iso_date)]
PositiveInt = Annotated[int, Field(ge=1)]
Percent = Annotated[Numeric, Field(gt=0, le=100)]
Compare = Literal[">", ">=", "<", "<=", "==", "!="]


class _Params(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", validate_default=True)


class _Trend(_Params):
    indicator_id: MetricId
    n_periods: Annotated[int, Field(ge=2)]
    direction: Literal["up", "down"]
    strict: bool = True
    org_ids: list[MetricId] | None = None
    as_of: Day | None = None


class _Benchmark(_Params):
    indicator_id: MetricId
    baseline: Literal["auto", "source_province_total", "province_weighted", "median", "arithmetic_mean", "custom"] = "auto"
    custom_value: Numeric | None = None
    direction: Literal["above", "below", "both"] = "both"
    min_deviation: Annotated[Numeric, Field(ge=0)] | None = None
    allow_rate_arithmetic_mean: bool = False


class _Bucket(_Params):
    indicator_id: MetricId
    bins: Annotated[list[Numeric], Field(min_length=1)]
    labels: list[str] | None = None
    right_closed: bool = False
    include_org_ids: bool = True


class _Concentration(_Params):
    indicator_id: MetricId
    top_n: PositiveInt | Annotated[list[PositiveInt], Field(min_length=1)]
    allow_partial_denominator: bool = False


class _Increment(_Params):
    top_n: PositiveInt = 5
    mode: Literal["auto", "net", "positive_only"] = "auto"
    offset_ratio_threshold: Annotated[Numeric, Field(ge=0, le=1)] = Decimal("0.2")


class _DirectIncrement(_Increment):
    indicator_id: MetricId


class _PeriodIncrement(_Increment):
    base_indicator_id: MetricId
    from_date: Day
    to_date: Day


class _Pair(_Params):
    indicator_a: MetricId
    indicator_b: MetricId
    allow_date_mismatch: bool = False


class _Quantile(_Pair):
    rule_type: Literal["quantile_opposite"]
    a_side: Literal["top", "bottom"]
    b_side: Literal["top", "bottom"]
    a_pct: Percent
    b_pct: Percent


class _Threshold(_Pair):
    rule_type: Literal["threshold_combo"]
    a_op: Compare
    b_op: Compare
    a_value: Numeric
    b_value: Numeric


class _Spread(_Pair):
    rule_type: Literal["spread"]
    spread_op: Compare
    spread_value: Numeric


class _CreditShock(_Params):
    formula_id: Literal["npl_shock_provision_coverage"]
    org_ids: list[MetricId] | None = None
    npl_balance_indicator_id: MetricId
    provision_coverage_indicator_id: MetricId
    provision_balance_indicator_id: MetricId | None = None
    shock_type: Literal["pct", "amount"]
    shock_value: Annotated[Numeric, Field(gt=0)]
    warning_line: Numeric = Decimal("150")


class _RateShock(_Params):
    formula_id: Literal["rate_shock_bond_loss"]
    org_ids: list[MetricId] | None = None
    bond_balance_indicator_id: MetricId
    duration_indicator_id: MetricId
    shift_bp: Numeric


_PARAMS = {name: TypeAdapter(schema) for name, schema in {
    "consecutive_trend": _Trend, "benchmark_deviation": _Benchmark,
    "bucket_distribution": _Bucket, "concentration_cr": _Concentration,
    "incremental_contribution": _DirectIncrement | _PeriodIncrement,
    "metric_divergence": Annotated[_Quantile | _Threshold | _Spread, Field(discriminator="rule_type")],
    "stress_test": Annotated[_CreditShock | _RateShock, Field(discriminator="formula_id")],
}.items()}


def _parse_params(operator, params, org_ids):
    if not isinstance(operator, str) or operator not in _PARAMS:
        block("UNKNOWN_OPERATOR", "未注册的分析算子")
    try:
        model = _PARAMS[operator].validate_python(params)
    except ValidationError as exc:
        field = ".".join(map(str, exc.errors(include_input=False)[0]["loc"]))
        block("INVALID_PARAMS", f"{operator} 参数无效，请核对 {field or '必填字段及所选规则'}")
    p = model.model_dump()
    if isinstance(model, _Bucket):
        if any(a >= b for a, b in zip(model.bins, model.bins[1:])):
            block("INVALID_PARAMS", "分桶切分点须严格递增")
        if model.labels is not None and len(model.labels) != len(model.bins) + 1:
            block("INVALID_PARAMS", "标签数须等于切分点数加一")
    if isinstance(model, _Concentration):
        p["top_n"] = sorted(set(model.top_n)) if isinstance(model.top_n, list) else [model.top_n]
    if isinstance(model, _Benchmark) and model.baseline == "custom" and model.custom_value is None:
        block("INVALID_PARAMS", "自定义基准须指定 custom_value")
    if isinstance(model, _PeriodIncrement) and model.to_date <= model.from_date:
        block("INVALID_PARAMS", "期末日期须晚于期初日期")
    if isinstance(model, _Pair) and model.indicator_a == model.indicator_b:
        block("INVALID_PARAMS", "跨指标分析须选择两个不同指标")
    if p.get("org_ids") is not None:
        if set(p["org_ids"]) - set(org_ids):
            block("INVALID_PARAMS", "所选行社超出本次冻结任务范围")
        p["org_ids"] = sorted(set(p["org_ids"]))
    return p


class _Facts:
    """前两阶段。标准化后复用指标体检，仅将可引用的有效事实纳入计算索引。"""

    def __init__(self, fetched, definitions, validation):
        data = deepcopy(fetched)
        self.as_of = data.get("as_of")
        self.defs = {**INDICATORS, **definitions}
        self.orgs = {org["org_id"]: org for org in data["organizations"]}
        self.catalog = {row["indicator_id"]: row for row in data["indicators"]}
        self.clipped = bool((data.get("visibility") or {}).get("clipped"))
        if self.clipped:
            data["visibility"].setdefault("visible_note", "当前仅覆盖部分行社")
        self.latest = {}
        self.history = {}
        self.issues = {}
        self.evidence = set()
        # 外部提供的体检可能在规范化之前完成。新发现的质量问题会使其原有汇总失效。
        invalidates_accounting = False
        for key in ("history", "latest"):
            for row in data[key]:
                metric, org = row["indicator_id"], row["org_id"]
                if org not in self.orgs or metric not in self.catalog:
                    block("INVALID_FACTS", "观测机构或指标超出取数清单")
                valid_before = row.get("usable", True)
                issue = self._normalize(row, data.get("source"))
                row["usable"] = issue is None
                row["issue"] = issue
                if key == "latest" and issue:
                    self.issues[org, metric] = issue
                    invalidates_accounting |= valid_before
        for totals in data.get("source_totals", {}).values():
            for row in [totals.get("province"), *totals.get("regions", {}).values()]:
                if row is None:
                    continue
                valid_before = row.get("usable", True)
                try:
                    row["value"] = _decimal(row["value"])
                except ValueError:
                    row["usable"] = False
                if row.get("quality_status", "valid") != "valid":
                    row["usable"] = False
                invalidates_accounting |= valid_before and not row.get("usable", True)
        if validation is None or invalidates_accounting:
            validation = metric_validator.validate_fetched_metrics(data, self.defs)
        self.accounting = {item["indicator_id"]: item for item in validation["accounting_details"]}
        # 有的新鲜度检查只适用于当前值。相同期次的 latest 记录拥有最终决定权。
        for key in ("history", "latest"):
            for row in data[key]:
                org, metric, day = row["org_id"], row["indicator_id"], row["data_date"]
                period = org, metric, day
                if not row["usable"]:
                    self.history.pop(period, None)
                    if key == "latest":
                        self.issues[org, metric] = row["issue"]
                    continue
                self.evidence.add(row["evidence_id"])
                self.history[period] = row
                if key == "latest":
                    self.latest[org, metric] = row

    def _normalize(self, row, source):
        if not row.get("usable", True) or row.get("quality_status") != "valid" or row.get("value") is None:
            return row.get("issue") or "指标值缺失或质量状态不合格"
        if not isinstance(row.get("evidence_id"), str) or not row["evidence_id"]:
            return "缺少可追溯的观测 ID"
        try:
            row["value"] = _decimal(row["value"])
            _iso_date(row["data_date"])
        except (TypeError, ValueError):
            return "指标数值或期次格式无效"
        if self.as_of and row["data_date"] > self.as_of:
            return "数据期次晚于冻结日期"
        metric = row["indicator_id"]
        definition = self.defs.get(metric, {})
        expected = (definition.get("unit_definition") if source == "quickbi" else None) or definition.get("unit")
        if row["unit"] != self.unit(metric) or not metric_validator._unit_matches(row["unit"], expected):
            return "指标单位与目录或业务口径不一致"
        return None

    def unit(self, metric):
        return self.catalog.get(metric, {}).get("unit") or self.defs.get(metric, {}).get("unit")

    def kind(self, metric):
        return self.defs.get(metric, {}).get("metric_kind")

    def frequency(self, metric):
        return self.catalog.get(metric, {}).get("frequency") or self.defs.get(metric, {}).get("frequency")

    def reason(self, org, metric):
        return self.issues.get((org, metric)) or f"缺少指标 {metric} 的有效观测"


def exclude(result, ctx, org, reason):
    counts = result["coverage"]
    counts["excluded_orgs"].append({"org_id": org, "org_name": ctx.orgs[org]["org_name"], "reason": reason})
    counts["valid_orgs"] -= 1



def _current_members(ctx, metric, r):
    """按任务机构清单逐家取当期事实，缺失原因归入覆盖统计。"""
    members = []
    for org in ctx.orgs:
        row = ctx.latest.get((org, metric))
        if row is None:
            exclude(r, ctx, org, ctx.reason(org, metric))
        else:
            members.append(row)
    return members


def _trend_steps(values):
    return [current - previous for previous, current in zip(values, values[1:])]


def _matches_trend(steps, direction, strict):
    oriented = steps if direction == "up" else [-step for step in steps]
    return min(oriented) > 0 if strict else min(oriented) >= 0 and max(oriented) > 0


def _trend_window(ctx, p):
    # 子集可以限定输出机构，但期次轴始终取自全辖，不能因选中缺数机构而跳期。
    metric = p["indicator_id"]
    cutoff = min(filter(None, (p.get("as_of"), ctx.as_of)), default=None)
    axis = sorted({day for org, indicator, day in ctx.history
                   if indicator == metric and (cutoff is None or day <= cutoff)})
    return axis[-p["n_periods"]:]


def _calendar_gaps(periods, frequency):
    return primitives._calendar_gaps(periods, frequency, observation_days=True)


def consecutive_trend(ctx, p, r):
    """在固定日历窗口内逐期比较，断期的行社不回溯拼接。"""
    metric = p["indicator_id"]
    direction, strict = p["direction"], p["strict"]
    periods = _trend_window(ctx, p)
    orgs = ctx.orgs if p.get("org_ids") is None else p["org_ids"]
    r["summary"].update(indicator_id=metric, direction=direction, strict=strict,
                        target_periods=periods, matched_count=0)
    frequency = ctx.frequency(metric)
    gaps = _calendar_gaps(periods, frequency)
    if len(periods) < p["n_periods"] or gaps and frequency in ("季", "月"):
        reason = (f"仅有 {len(periods)} 个有效期次，需要 {p['n_periods']} 期"
                  if len(periods) < p["n_periods"] else f"{frequency}频期次不连续：{'、'.join(gaps)}")
        for org in orgs:
            exclude(r, ctx, org, reason)
        insufficient(reason)
    if gaps:
        r["caveats"].append("日频观测存在跨月断层，按可见观测日比较：" + "、".join(gaps))
    for org in orgs:
        observations = [ctx.history.get((org, metric, day)) for day in periods]
        missing = [day for day, row in zip(periods, observations) if row is None]
        if missing:
            exclude(r, ctx, org, f"缺失 {'、'.join(missing)} 期次，连续性中断")
            continue
        values = [row["value"] for row in observations]
        steps = _trend_steps(values)
        ids = [row["evidence_id"] for row in observations]
        r["evidence_ids"].update(ids)
        if _matches_trend(steps, direction, strict):
            series = [{"data_date": row["data_date"], "value": number(row["value"]),
                       "evidence_id": row["evidence_id"]} for row in observations]
            r["rows"].append({**ctx.orgs[org], "series": series, "step_changes": [number(s) for s in steps],
                              "total_change": number(values[-1] - values[0]), "evidence_ids": ids})
    if not r["coverage"]["valid_orgs"]:
        insufficient("没有完整覆盖目标期次的行社")
    r["rows"].sort(key=lambda row: Decimal(row["total_change"]), reverse=direction == "up")
    r["summary"]["matched_count"] = len(r["rows"])


def check_consecutive_trend(ctx, p, r):
    periods = _trend_window(ctx, p)
    metric = p["indicator_id"]
    require(r["summary"]["target_periods"] == periods and len(periods) == p["n_periods"])
    if ctx.frequency(metric) in ("季", "月"):
        require(not _calendar_gaps(periods, ctx.frequency(metric)))
    for row in r["rows"]:
        series = row["series"]
        require([point["data_date"] for point in series] == periods)
        for point in series:
            fact = ctx.history.get((row["org_id"], metric, point["data_date"]))
            require(fact is not None and fact["evidence_id"] == point["evidence_id"]
                    and fact["value"] == Decimal(point["value"]))
        values = [Decimal(point["value"]) for point in series]
        steps = _trend_steps(values)
        require([Decimal(s) for s in row["step_changes"]] == steps
                and Decimal(row["total_change"]) == values[-1] - values[0]
                and _matches_trend(steps, p["direction"], p["strict"]))
    require(r["summary"]["matched_count"] == len(r["rows"]))
    return ["period_contiguous"]


def benchmark_deviation(ctx, p, r):
    """用同日基准计算偏离；加权值与源表合计沿用既有口径体检结果。"""
    metric, method = p["indicator_id"], p["baseline"]
    kind = ctx.kind(metric)
    if ctx.clipped and method in ("auto", "source_province_total", "province_weighted"):
        block("CLIPPED_SCOPE", "当前范围已裁剪，不能使用全辖基准")
    if kind == "rate" and method == "arithmetic_mean" and not p["allow_rate_arithmetic_mean"]:
        block("INVALID_METHOD_FOR_RATE", "率类指标禁止简单平均，请使用全辖加权值、源表合计或中位数")
    members = _current_members(ctx, metric, r)
    if not members:
        insufficient("没有可用于对标的当期观测")
    # 以最近一个实际报告日为对标日。滞后的合计或加权值不可充当当期基准。
    day = max(row["data_date"] for row in members)
    if method != "custom":
        for row in members:
            if row["data_date"] != day:
                exclude(r, ctx, row["org_id"], f"数据日期与对标日 {day} 不一致")
        members = [row for row in members if row["data_date"] == day]
    accounting = ctx.accounting.get(metric, {})
    source = accounting.get("source_province_total") if accounting.get("source_total_date") == day else None
    weighted = accounting.get("weighted_value") if accounting.get("weighted_data_dates") == [day] else None
    if method == "auto":
        if kind == "rate":
            if source is not None:
                method = "source_province_total"
            elif weighted is not None:
                method = "province_weighted"
            else:
                method = "median"
                r["caveats"].append("缺少源表全辖合计及完整分子分母，已改用行社中位数对标，未使用简单平均")
        else:
            method = "arithmetic_mean"
            r["summary"]["median"] = number(primitives.median_value(members))
    if method == "source_province_total":
        if source is None:
            insufficient("缺少同日可用的源表全辖合计")
        baseline = Decimal(source)
        r["summary"]["source_total_date"] = day
    elif method == "province_weighted":
        if weighted is None:
            insufficient("缺少同日同单位的完整分子分母或有效分母合计")
        baseline = Decimal(weighted)
        for org in ctx.orgs:
            for indicator in (accounting["numerator_id"], accounting["denominator_id"]):
                fact = ctx.latest.get((org, indicator))
                if fact is None or fact["data_date"] != day:
                    insufficient("加权基准缺少可追溯的同日分子或分母观测")
                r["evidence_ids"].add(fact["evidence_id"])
    elif method == "arithmetic_mean":
        baseline = primitives.avg(members, kind=kind, allow_rate_arithmetic_mean=p["allow_rate_arithmetic_mean"])
        if kind == "rate":
            r["caveats"].append("调用方显式允许率值简单平均，基准为行社算术平均")
    elif method == "median":
        baseline = primitives.median_value(members)
    else:
        baseline = p["custom_value"]
    if ctx.clipped and method in ("median", "arithmetic_mean"):
        r["caveats"].append("该基准仅根据当前可见行社计算")
    for row in members:
        r["evidence_ids"].add(row["evidence_id"])
        value = row["value"]
        deviation = value - baseline
        side = "above" if deviation > 0 else "below" if deviation < 0 else "equal"
        if p["direction"] not in ("both", side) or abs(deviation) < (p.get("min_deviation") or 0):
            continue
        detail = {**ctx.orgs[row["org_id"]], "value": number(value), "baseline_value": number(baseline),
                  "deviation": number(deviation), "side": side, "data_date": row["data_date"],
                  "evidence_id": row["evidence_id"]}
        if kind == "amount" and baseline:
            detail["deviation_pct"] = number(deviation / abs(baseline) * 100)
        r["rows"].append(detail)
    r["rows"].sort(key=lambda row: abs(Decimal(row["deviation"])), reverse=True)
    r["summary"].update(indicator_id=metric, unit=ctx.unit(metric), baseline_method=method,
                        baseline_value=number(baseline), matched_count=len(r["rows"]))


def check_benchmark_deviation(ctx, p, r):
    metric = p["indicator_id"]
    checks = []
    if ctx.kind(metric) == "rate":
        if r["summary"]["baseline_method"] == "arithmetic_mean":
            require(p["baseline"] == "arithmetic_mean" and p["allow_rate_arithmetic_mean"])
        else:
            checks.append("rate_no_simple_average")
    baseline = Decimal(r["summary"]["baseline_value"])
    for row in r["rows"]:
        fact = ctx.latest.get((row["org_id"], metric))
        require(fact is not None and row["evidence_id"] == fact["evidence_id"] and
                Decimal(row["value"]) == fact["value"] and row["data_date"] == fact["data_date"])
        require(Decimal(row["baseline_value"]) == baseline and fact["value"] - baseline == Decimal(row["deviation"]))
    require(r["summary"]["matched_count"] == len(r["rows"]))
    return checks + ["deviation_exact"]


def _bucket_range(bins, i, right):
    return primitives._bucket_range(bins, i, right)


def bucket_distribution(ctx, p, r):
    """按开闭边界把每个有效行社唯一入桶，缺失单列。"""
    metric, bins, right = p["indicator_id"], p["bins"], p["right_closed"]
    members = _current_members(ctx, metric, r)
    valid, total = len(members), len(ctx.orgs)
    excluded = r["coverage"]["excluded_orgs"]
    r["summary"].update(indicator_id=metric, unit=ctx.unit(metric), right_closed=right,
                        valid_count=valid, total_count=total, missing_count=len(excluded),
                        missing_bucket={"count": len(excluded), "excluded_orgs": excluded})
    if not valid:
        insufficient("没有可进入数值区间的有效观测")
    buckets = [[] for _ in range(len(bins) + 1)]
    for row in members:
        buckets[primitives._bucket_index(row["value"], bins, right)].append(row)
    for i, facts in enumerate(buckets):
        interval = _bucket_range(bins, i, right)
        ids = [row["evidence_id"] for row in facts]
        count = len(facts)
        bucket = {"label": p["labels"][i] if p.get("labels") else interval,
                  "range_expr": interval, "count": count, "evidence_ids": ids,
                  "ratio_of_valid": f"{Decimal(count) * 100 / valid:.2f}",
                  "ratio_of_total": f"{Decimal(count) * 100 / total:.2f}"}
        if p["include_org_ids"]:
            bucket["org_ids"] = [row["org_id"] for row in facts]
        r["rows"].append(bucket)
        r["evidence_ids"].update(ids)


def check_bucket_distribution(ctx, p, r):
    counts = r["coverage"]
    require(sum(row["count"] for row in r["rows"]) == counts["valid_orgs"])
    require(counts["valid_orgs"] + len(counts["excluded_orgs"]) == counts["total_orgs"])
    require(r["summary"]["missing_bucket"]["count"] == len(counts["excluded_orgs"]))
    require(len(r["rows"]) == len(p["bins"]) + 1)
    # 同时核对边界归属，防止两个桶互换成员但总数恰好相等。
    locate = bisect_left if p["right_closed"] else bisect_right
    expected = [set() for _ in r["rows"]]
    for (org, metric), fact in ctx.latest.items():
        if metric == p["indicator_id"] and org in ctx.orgs:
            expected[locate(p["bins"], fact["value"])].add(fact["evidence_id"])
    for row, ids in zip(r["rows"], expected):
        require(set(row["evidence_ids"]) == ids and row["count"] == len(ids))
    return ["bucket_sum_matches_total"]


def concentration_cr(ctx, p, r):
    """金额同日排名，默认只接受全辖完整分母；边界并列使用机构号稳定排序。"""
    metric = p["indicator_id"]
    if ctx.kind(metric) != "amount":
        block("INVALID_METRIC_KIND", "集中度仅适用于金额类指标")
    if ctx.clipped:
        block("CLIPPED_SCOPE", "可见范围已裁剪，不能计算全辖集中度")
    members = _current_members(ctx, metric, r)
    if len({row["data_date"] for row in members}) > 1:
        block("INCOMPLETE_COVERAGE", "当前有效金额存在多个日期，集中度分母不能跨期相加")
    if len(members) < len(ctx.orgs):
        if not p["allow_partial_denominator"]:
            block("INCOMPLETE_COVERAGE", "缺少部分行社的有效金额，全辖分母不完整")
        r["caveats"].append("分母采用同日有效行社的可见合计，覆盖不完整")
    if not members:
        insufficient("没有可用于集中度计算的有效金额")
    if any(row["value"] < 0 for row in members):
        block("NEGATIVE_VALUE_NOT_ALLOWED", "有效行社金额含负值，不能用于集中度计算")
    total = sum((row["value"] for row in members), Decimal(0))
    if total <= 0:
        block("NON_POSITIVE_TOTAL", "有效行社合计不大于零")
    members.sort(key=lambda row: (-row["value"], row["org_id"]))
    r["evidence_ids"].update(row["evidence_id"] for row in members)
    cr_values = {}
    for n in p["top_n"]:
        k = min(n, len(members))
        tied = k < len(members) and members[k - 1]["value"] == members[k]["value"]
        if tied:
            r["caveats"].append(f"CR{n} 的边界存在并列，按机构号升序取前 {k} 家")
        if k < n:
            r["caveats"].append(f"CR{n} 请求家数超过有效家数，按实际 {k} 家计算")
        top = members[:k]
        subtotal = sum((row["value"] for row in top), Decimal(0))
        pct = number(subtotal / total * 100)
        cr_values[f"CR{n}"] = pct
        r["rows"].append({"n": n, "effective_n": k, "top_sum": number(subtotal), "total_value": number(total),
                          "cr_pct": pct, "has_boundary_tie": tied,
                          "top_orgs": [{"rank": rank, "org_id": row["org_id"],
                                        "org_name": ctx.orgs[row["org_id"]]["org_name"], "value": number(row["value"]),
                                        "share_pct": number(row["value"] / total * 100),
                                        "evidence_id": row["evidence_id"]} for rank, row in enumerate(top, 1)]})
    r["summary"].update(indicator_id=metric, unit=ctx.unit(metric), total_value=number(total), cr_values=cr_values)


def check_concentration_cr(ctx, p, r):
    ranked = sorted((fact for (org, metric), fact in ctx.latest.items() if metric == p["indicator_id"]
                     and org in ctx.orgs), key=lambda row: (-row["value"], row["org_id"]))
    total = sum((row["value"] for row in ranked), Decimal(0))
    require(total > 0 and Decimal(r["summary"]["total_value"]) == total)
    require([row["n"] for row in r["rows"]] == p["top_n"])
    last = Decimal(0)
    for row in r["rows"]:
        top = ranked[:row["n"]]
        require(len(row["top_orgs"]) == row["effective_n"] == len(top))
        for org, fact in zip(row["top_orgs"], top):
            require(org["org_id"] == fact["org_id"] and org["evidence_id"] == fact["evidence_id"]
                    and Decimal(org["value"]) == fact["value"])
        subtotal = sum((fact["value"] for fact in top), Decimal(0))
        pct = Decimal(row["cr_pct"])
        require(Decimal(row["top_sum"]) == subtotal and Decimal(row["total_value"]) == total
                and pct == subtotal / total * 100 and last <= pct <= 100)
        require(r["summary"]["cr_values"][f"CR{row['n']}"] == row["cr_pct"])
        last = pct
    return ["concentration_bounds_valid"]



def _delta_facts(ctx, p, org):
    """固定两端期次，任一端缺失时保留具体缺期原因。"""
    if p.get("indicator_id"):
        metric = p["indicator_id"]
        row = ctx.latest.get((org, metric))
        if row is None:
            return None, [], ctx.reason(org, metric)
        return row["value"], [row], None
    metric = p["base_indicator_id"]
    rows = []
    for day in (p["from_date"], p["to_date"]):
        row = ctx.history.get((org, metric, day))
        if row is None:
            return None, [], f"{metric} 缺少 {day} 的有效观测"
        rows.append(row)
    return rows[1]["value"] - rows[0]["value"], rows, None


def incremental_contribution(ctx, p, r):
    """按净增量或正增量计算贡献，先保留整组分母，再按 Top N 展示。"""
    metric = p.get("indicator_id") or p["base_indicator_id"]
    if ctx.clipped:
        block("CLIPPED_SCOPE", "可见范围已裁剪，无法计算全辖增量贡献度")
    expected_kind = "delta" if p.get("indicator_id") else "amount"
    if ctx.kind(metric) != expected_kind:
        block("INVALID_METRIC_KIND", "直接增量须选增量指标，两期作差须选金额指标")
    deltas = {}
    dates = set()
    evidence = {}
    for org in ctx.orgs:
        delta, facts, issue = _delta_facts(ctx, p, org)
        if issue:
            exclude(r, ctx, org, issue)
            continue
        dates.add(facts[-1]["data_date"])
        deltas[org] = delta
        evidence[org] = [fact["evidence_id"] for fact in facts]
        r["evidence_ids"].update(evidence[org])
    if len(dates) > 1:
        block("INCOMPLETE_COVERAGE", "增量期末日期不一致，无法加总")
    if not deltas:
        insufficient("没有可用于计算增量的有效行社")
    if len(deltas) < len(ctx.orgs):
        r["caveats"].append("增量贡献度分母仅含有效行社，缺数行社已剔除。")
    positive = sum((d for d in deltas.values() if d > 0), Decimal(0))
    negative = sum((d for d in deltas.values() if d < 0), Decimal(0))
    net = positive + negative
    gross = positive - negative
    if not gross:
        insufficient("所有有效行社增量均为零，无法计算贡献度")
    offset = not net or abs(net) < p["offset_ratio_threshold"] * gross
    mode = p["mode"]
    if mode == "auto":
        mode = "positive_only" if offset else "net"
        if offset:
            r["caveats"].append("净增量存在显著正负抵消，已切换为正增量行社对正增量合计的贡献。")
    elif mode == "net" and offset:
        r["caveats"].append("显著正负抵消可能导致按净增量计算的贡献度失真。")
    if mode == "net" and not net:
        block("ZERO_NET_TOTAL", "全辖净增量为零，无法作为贡献度分母")
    if mode == "positive_only" and not positive:
        insufficient("没有正增量行社，无法计算对正增量合计的贡献")
    denominator = net if mode == "net" else positive
    ordered = sorted((org for org, d in deltas.items() if mode == "net" or d > 0),
                     key=lambda org: (-deltas[org], org))
    for org in ordered[:p["top_n"]]:
        r["rows"].append({**ctx.orgs[org], "delta_value": number(deltas[org]),
                          "contribution_pct": number(deltas[org] / denominator * 100),
                          "evidence_ids": evidence[org]})
    r["summary"].update(effective_mode=mode, significant_offset=offset, net_total=number(net),
                        positive_total=number(positive), negative_total=number(negative),
                        top_n=p["top_n"], unit=ctx.unit(metric),
                        top_n_contribution_pct=number(sum(Decimal(row["contribution_pct"]) for row in r["rows"])))


def check_incremental_contribution(ctx, p, r):
    """在全部有效观测上复核分母和贡献度；Top N 的合计不必为 100%。"""
    s = r["summary"]
    deltas, refs = {}, {}
    for org in ctx.orgs:
        delta, facts, issue = _delta_facts(ctx, p, org)
        if not issue:
            deltas[org] = delta
            refs[org] = [row["evidence_id"] for row in facts]
    net = sum(deltas.values(), Decimal(0))
    positive = sum((d for d in deltas.values() if d > 0), Decimal(0))
    negative = net - positive
    offset = net == 0 or abs(net) < p["offset_ratio_threshold"] * (positive - negative)
    mode = p["mode"]
    if mode == "auto":
        mode = "positive_only" if offset else "net"
    require(s["effective_mode"] == mode and s["significant_offset"] == offset)
    require(list(map(Decimal, (s["positive_total"], s["negative_total"], s["net_total"]))) ==
            [positive, negative, net])
    denominator = net if mode == "net" else positive
    require(denominator != 0)
    shares = {o: d / denominator * 100 for o, d in deltas.items() if mode == "net" or d > 0}
    require(abs(sum(shares.values()) - 100) <= Decimal("0.05"))
    top_orgs = sorted(shares, key=lambda o: (-deltas[o], o))[:p["top_n"]]
    require(top_orgs == [row["org_id"] for row in r["rows"]])
    require(sum((shares[org] for org in top_orgs), Decimal(0)) == Decimal(s["top_n_contribution_pct"]))
    for row in r["rows"]:
        org = row["org_id"]
        require(Decimal(row["delta_value"]) == deltas[org] and
                Decimal(row["contribution_pct"]) == shares[org] and row["evidence_ids"] == refs[org])
    return ["contribution_sum_reconciled"]


_COMPARISONS = {">": comparison.gt, ">=": comparison.ge, "<": comparison.lt,
                "<=": comparison.le, "==": comparison.eq, "!=": comparison.ne}


def _rank_tail(values, side, pct):
    """按并列同名次排名，取前/后 K 时对边界同值全部纳入。"""
    members = ({"org_id": org, "value": value} for org, value in values.items())
    ordered = primitives.rank_rows(members, order="desc" if side == "top" else "asc")
    k = int((pct * len(ordered) / 100).to_integral_value(rounding=ROUND_CEILING))
    tie = 0 < k < len(ordered) and ordered[k - 1]["rank"] == ordered[k]["rank"]
    return {row["org_id"]: row["rank"] for row in ordered if row["rank"] <= k}, tie


def _paired_observations(ctx, p, org):
    a = ctx.latest.get((org, p["indicator_a"]))
    b = ctx.latest.get((org, p["indicator_b"]))
    if not a or not b:
        return None, ctx.reason(org, p["indicator_a"] if not a else p["indicator_b"])
    if not p["allow_date_mismatch"] and a["data_date"] != b["data_date"]:
        return None, f"两指标期次不一致（{a['data_date']}、{b['data_date']}）"
    return (a, b), None


def _paired_ranks(p, pairs):
    a, tie_a = _rank_tail({o: a["value"] for o, (a, _) in pairs.items()}, p["a_side"], p["a_pct"])
    b, tie_b = _rank_tail({o: b["value"] for o, (_, b) in pairs.items()}, p["b_side"], p["b_pct"])
    return a, b, tie_a or tie_b


def _rule_matches(p, org, a, b, ranks_a, ranks_b):
    if p["rule_type"] == "quantile_opposite":
        return org in ranks_a and org in ranks_b
    if p["rule_type"] == "spread":
        return _COMPARISONS[p["spread_op"]](a - b, p["spread_value"])
    return (_COMPARISONS[p["a_op"]](a, p["a_value"]) and
            _COMPARISONS[p["b_op"]](b, p["b_value"]))


def metric_divergence(ctx, p, r):
    """只对同机构成对观测判断背离，全辖分位的总体为有效配对行社。"""
    rule = p["rule_type"]
    if rule == "quantile_opposite" and ctx.clipped:
        block("CLIPPED_SCOPE", "可见范围已裁剪，不能计算全辖分位")
    if rule == "spread" and ctx.unit(p["indicator_a"]) != ctx.unit(p["indicator_b"]):
        block("UNIT_MISMATCH_FOR_SPREAD", "两个指标单位不一致，不能计算剪刀差")
    pairs = {}
    for org in ctx.orgs:
        pair, issue = _paired_observations(ctx, p, org)
        if issue:
            exclude(r, ctx, org, issue)
        else:
            pairs[org] = pair
            r["evidence_ids"].update(row["evidence_id"] for row in pair)
    if not pairs:
        insufficient("没有同时具备两项有效观测且满足期次要求的行社")
    if any(a["data_date"] != b["data_date"] for a, b in pairs.values()):
        r["caveats"].append("已按参数放行跨期配对，请结合行明细中的两个实际日期解读。")
    ranks_a, ranks_b = {}, {}
    if rule == "quantile_opposite":
        ranks_a, ranks_b, tie = _paired_ranks(p, pairs)
        if tie:
            r["caveats"].append("分位边界存在同值并列，按并列同入口径纳入。")
    for org, (a, b) in pairs.items():
        if not _rule_matches(p, org, a["value"], b["value"], ranks_a, ranks_b):
            continue
        row = {**ctx.orgs[org], "value_a": number(a["value"]), "unit_a": a["unit"], "date_a": a["data_date"],
               "value_b": number(b["value"]), "unit_b": b["unit"], "date_b": b["data_date"],
               "evidence_ids": [a["evidence_id"], b["evidence_id"]]}
        if rule == "spread":
            row["spread"] = number(a["value"] - b["value"])
        if rule == "quantile_opposite":
            row.update(rank_a=ranks_a[org], rank_b=ranks_b[org])
        r["rows"].append(row)
    r["summary"].update(indicator_a=p["indicator_a"], indicator_b=p["indicator_b"],
                        rule_type=rule, matched_count=len(r["rows"]), paired_org_count=len(pairs))


def check_metric_divergence(ctx, p, r):
    pairs = {}
    for org in ctx.orgs:
        pair, issue = _paired_observations(ctx, p, org)
        if not issue:
            pairs[org] = pair
    ranks_a, ranks_b = {}, {}
    if p["rule_type"] == "quantile_opposite":
        ranks_a, ranks_b, _ = _paired_ranks(p, pairs)
    matches = {org for org, (a, b) in pairs.items()
               if _rule_matches(p, org, a["value"], b["value"], ranks_a, ranks_b)}
    require(len(r["rows"]) == r["summary"]["matched_count"] == len(matches))
    require({row["org_id"] for row in r["rows"]} == matches and r["summary"]["paired_org_count"] == len(pairs))
    for row in r["rows"]:
        org = row["org_id"]
        a, b = pairs[org]
        require(row["evidence_ids"] == [a["evidence_id"], b["evidence_id"]])
        for suffix, fact in (("a", a), ("b", b)):
            require(fact["value"] == Decimal(row[f"value_{suffix}"]) and fact["unit"] == row[f"unit_{suffix}"]
                    and fact["data_date"] == row[f"date_{suffix}"])
        if p["rule_type"] == "spread":
            require(a["unit"] == b["unit"] and a["value"] - b["value"] == Decimal(row["spread"]))
        elif p["rule_type"] == "quantile_opposite":
            require((row["rank_a"], row["rank_b"]) == (ranks_a[org], ranks_b[org]))
    return ["divergence_pair_verified"]


_CREDIT_SHOCK = "npl_shock_provision_coverage"
_RATE_SHOCK = "rate_shock_bond_loss"


def _shock_metrics(p):
    if p["formula_id"] == _CREDIT_SHOCK:
        ids = [p["npl_balance_indicator_id"], p["provision_coverage_indicator_id"]]
        if p.get("provision_balance_indicator_id"):
            ids.append(p["provision_balance_indicator_id"])
        return ids
    if p["formula_id"] == _RATE_SHOCK:
        return [p["bond_balance_indicator_id"], p["duration_indicator_id"]]
    block("INVALID_PARAMS", "压力测算公式须从白名单中选择")


def _shock_inputs(ctx, org, ids, formula):
    facts = []
    for metric in ids:
        row = ctx.latest.get((org, metric))
        if not row:
            return [], ctx.reason(org, metric)
        facts.append(row)
    if len({row["data_date"] for row in facts}) != 1:
        return [], "测算所需指标日期不一致"
    if ctx.kind(ids[0]) != "amount":
        return [], "余额指标须为金额类"
    balance, second = facts[:2]
    if formula == _CREDIT_SHOCK:
        if balance["value"] <= 0 or second["value"] < 0:
            return [], "原不良余额须为正数，拨备覆盖率须为非负数"
        if second["unit"] not in ("%", "％") or ctx.kind(ids[1]) != "rate":
            return [], "拨备覆盖率须为百分比率值"
        if len(facts) == 3 and (ctx.kind(ids[2]) != "amount" or facts[2]["unit"] != balance["unit"]
                                or facts[2]["value"] < 0):
            return [], "拨备余额须为非负金额，且单位须与不良余额一致"
    else:
        if balance["value"] < 0 or second["value"] < 0:
            return [], "债券余额与修正久期须为非负数"
        if second["unit"] not in {"年", "year", "years"}:
            return [], "修正久期须以年为单位才能应用年化收益率冲击"
    return facts, None


def _shocked_values(p, facts):
    """在同行社同日期的基准观测上推演，返回推演字段和严重程度排序值。"""
    balance, second = (row["value"] for row in facts[:2])
    if p["formula_id"] == _RATE_SHOCK:
        pnl = -balance * second * (p["shift_bp"] / 10000)
        loss = max(Decimal(0), -pnl)
        return {"bond_balance": number(balance), "duration": number(second),
                "estimated_pnl": number(pnl), "estimated_loss": number(loss)}, -loss

    provision = facts[2]["value"] if len(facts) == 3 else balance * second / 100
    if p["shock_type"] == "pct":
        shocked_npl = balance * (1 + p["shock_value"] / 100)
    else:
        shocked_npl = balance + p["shock_value"]
    coverage = provision / shocked_npl * 100
    return {"npl_balance": number(balance), "current_coverage": number(second),
            "provision_balance": number(provision), "shocked_npl": number(shocked_npl),
            "shocked_coverage": number(coverage), "coverage_drop_pp": number(coverage - second),
            "breaches_warning_line": coverage < p["warning_line"]}, coverage


def stress_test(ctx, p, r):
    """白名单情景只输出测算值，按冲击后覆盖率或预计损失排序。"""
    formula = p["formula_id"]
    ids = _shock_metrics(p)
    s = r["summary"]
    s["formula_id"] = formula
    if formula == _CREDIT_SHOCK:
        r["assumptions"].append("假设冲击后拨备余额维持不变。")
        if len(ids) == 2:
            r["assumptions"].append("基准拨备余额由不良贷款余额乘以原拨备覆盖率反推。")
        s.update(shock_type=p["shock_type"], shock_value=number(p["shock_value"]),
                 warning_line=number(p["warning_line"]))
    else:
        r["assumptions"].append("假设收益率曲线平行移动，采用一阶修正久期线性近似，未计入凸性影响。")
        s["shift_bp"] = number(p["shift_bp"])
    scored = []
    orgs = ctx.orgs if p.get("org_ids") is None else sorted(set(p["org_ids"]))
    for org in orgs:
        facts, issue = _shock_inputs(ctx, org, ids, formula)
        if issue:
            exclude(r, ctx, org, issue)
            continue
        values, severity = _shocked_values(p, facts)
        evidence = [row["evidence_id"] for row in facts]
        scored.append((severity, org, {**ctx.orgs[org], **values, "unit": facts[0]["unit"],
                                       "data_date": facts[0]["data_date"], "evidence_ids": evidence}))
        r["evidence_ids"].update(evidence)
    if not scored:
        insufficient("没有同时具备测算所需有效基准观测的行社")
    r["rows"] = [row for _, _, row in sorted(scored)]
    s["valid_org_count"] = len(scored)
    if formula == _CREDIT_SHOCK:
        breaches = [row for row in r["rows"] if row["breaches_warning_line"]]
        s.update(total_breached_count=len(breaches), newly_breached_count=sum(
            Decimal(row["current_coverage"]) >= p["warning_line"] for row in breaches))
    else:
        s["total_estimated_loss"] = number(sum(Decimal(row["estimated_loss"]) for row in r["rows"]))


def check_stress_test(ctx, p, r):
    """从引用的原始基准值逐家复算，同时验证是否跨线及损失合计。"""
    require(r["result_type"] == "simulated" and bool(r["assumptions"]))
    ids = _shock_metrics(p)
    formula = p["formula_id"]
    s = r["summary"]
    orgs = ctx.orgs if p.get("org_ids") is None else set(p["org_ids"])
    expected = {}
    for org in orgs:
        facts, issue = _shock_inputs(ctx, org, ids, formula)
        if not issue:
            expected[org] = facts
    require(len(expected) == s["valid_org_count"] == len(r["rows"]))
    require(set(expected) == {row["org_id"] for row in r["rows"]})
    breaches = newly_breached = 0
    loss_sum = Decimal(0)
    for row in r["rows"]:
        facts = expected[row["org_id"]]
        require(row["evidence_ids"] == [fact["evidence_id"] for fact in facts])
        require(row["unit"] == facts[0]["unit"] and row["data_date"] == facts[0]["data_date"])
        values, _ = _shocked_values(p, facts)
        require(all(row[key] == value for key, value in values.items()))
        if formula == _CREDIT_SHOCK:
            breaches += row["breaches_warning_line"]
            newly_breached += row["breaches_warning_line"] and facts[1]["value"] >= p["warning_line"]
        else:
            loss_sum += Decimal(row["estimated_loss"])
    if formula == _CREDIT_SHOCK:
        require(s["total_breached_count"] == breaches and s["newly_breached_count"] == newly_breached)
    else:
        require(Decimal(s["total_estimated_loss"]) == loss_sum)
    return ["simulation_isolated_and_verified"]


OPERATORS = {op.__name__: op for op in (
    consecutive_trend, benchmark_deviation, bucket_distribution, concentration_cr,
    incremental_contribution, metric_divergence, stress_test,
)}
_CHECKS = {
    "consecutive_trend": check_consecutive_trend, "benchmark_deviation": check_benchmark_deviation,
    "bucket_distribution": check_bucket_distribution, "concentration_cr": check_concentration_cr,
    "incremental_contribution": check_incremental_contribution, "metric_divergence": check_metric_divergence,
    "stress_test": check_stress_test,
}


def _contract(operator, params, fetched):
    total = len(fetched["organizations"])
    return {
        "operator": operator, "params": deepcopy(params), "as_of": fetched.get("as_of"),
        "status": "ok", "result_type": "simulated" if operator == "stress_test" else "observed_derived",
        "coverage": {"total_orgs": total, "valid_orgs": total, "excluded_orgs": []},
        "summary": {}, "rows": [], "evidence_ids": set(), "checks_passed": [],
        "caveats": [], "assumptions": [], "error": None,
    }


def _finish_issue(result, exc):
    result["rows"] = []
    result["checks_passed"] = []
    if exc.code == "INSUFFICIENT_DATA":
        result["status"] = "insufficient_data"
        result["summary"]["reason"] = str(exc)
        result["caveats"].append(str(exc))
    else:
        result.update(status="blocked", error={"code": exc.code, "message": str(exc)}, summary={})
        result["evidence_ids"] = []
    return result


def _row_citations(value):
    if isinstance(value, list):
        for item in value:
            yield from _row_citations(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_id":
                yield item
            elif key == "evidence_ids":
                yield from item
            elif isinstance(item, (list, dict)):
                yield from _row_citations(item)


def _post_verify(ctx, p, result):
    refs = result["evidence_ids"]
    require(set(_row_citations(result["rows"])) <= refs <= ctx.evidence)
    counts = result["coverage"]
    scope = set(ctx.orgs if p.get("org_ids") is None else p["org_ids"])
    excluded = [row["org_id"] for row in counts["excluded_orgs"]]
    require(set(excluded) <= scope and len(set(excluded)) == len(excluded))
    require(len(scope) == counts["total_orgs"] == counts["valid_orgs"] + len(excluded))
    checks = _CHECKS[result["operator"]](ctx, p, result)
    result["checks_passed"] = ["unit_aligned", "no_null_as_zero", "evidence_traceable", *checks]


def run_verified_operator(fetched: dict, metric_definitions: dict[str, dict], operator: str,
                          params: dict, *, validation: dict | None = None) -> dict:
    """标准化、口径体检、确定性计算和后置复核，返回可直接序列化的证据包。"""
    result = _contract(operator, params, fetched)
    try:
        p = _parse_params(operator, params, (org["org_id"] for org in fetched["organizations"]))
        if p.get("org_ids") is not None:
            count = len(p["org_ids"])
            result["coverage"].update(total_orgs=count, valid_orgs=count)
        with localcontext() as arithmetic:
            # 应用快照允许 16 位整数、8 位小数，留足金额乘积和比率运算精度。
            arithmetic.prec = 64
            ctx = _Facts(fetched, metric_definitions, validation)
            OPERATORS[operator](ctx, p, result)
            _post_verify(ctx, p, result)
    except VerificationIssue as exc:
        _finish_issue(result, exc)
    result["evidence_ids"] = sorted(result["evidence_ids"])
    return result


def verify_operator_result(result: dict, fetched: dict, metric_definitions: dict[str, dict] | None = None,
                           *, validation: dict | None = None) -> dict:
    """独立复核已传输或持久化的证据包，包括排序、全体分母、排除项及来源。

    从事实重新走相同精度的流水线并检查算子恒等式，再逐字段核对外部结果。
    这样删掉一条命中行、连同修改汇总或替换为另一条真实 ID 也会被拒绝。
    """
    expected = run_verified_operator(fetched, metric_definitions or {}, result.get("operator"),
                                     result.get("params"), validation=validation)
    if expected == result:
        return expected
    return _finish_issue(expected, VerificationIssue("POST_VERIFY_FAILED", "外部结果与原始观测复核结果不一致"))

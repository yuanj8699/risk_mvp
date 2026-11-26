"""冻结观测的基础核验原语。数值保留 Decimal，缺数、单位和覆盖范围显式处理。"""
from __future__ import annotations

import operator
from bisect import bisect_left, bisect_right
from calendar import monthrange
from collections import Counter
from datetime import date, timedelta
from decimal import Context, Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from functools import wraps
from typing import Literal


class PrimitiveError(ValueError):
    """供 API、规则和沙箱按稳定错误码处理口径错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_CONTEXT = Context(prec=64)


def _precise(function):
    @wraps(function)
    def calculate(*args, **kwargs):
        with localcontext(_CONTEXT):
            return function(*args, **kwargs)
    return calculate


def _choice(value, allowed, name):
    if value not in allowed:
        raise PrimitiveError("INVALID_PARAMS", f"{name} 取值不受支持")


@_precise
def num(value) -> Decimal:
    """仅在输入边界接受浮点的十进制文本表示，不以二进制浮点做运算。"""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PrimitiveError("INVALID_NUMBER", "须为有限十进制数值")
    try:
        result = Decimal(value) if isinstance(value, (int, Decimal)) else Decimal(str(value))
    except (ValueError, InvalidOperation) as exc:
        raise PrimitiveError("INVALID_NUMBER", "无法解析十进制数值") from exc
    if not result.is_finite():
        raise PrimitiveError("INVALID_NUMBER", "数值必须有限")
    if abs(result.adjusted()) > 1000 or len(result.as_tuple().digits) > 1000:
        raise PrimitiveError("INVALID_NUMBER", "数值超过可展示精度范围")
    return result


_UNITS = {
    "元": ("金额", 0), "万元": ("金额", 4), "亿元": ("金额", 8),
    "小数": ("比率", 0), "%": ("比率", -2), "bp": ("比率", -4),
}


@_precise
def convert_unit(value, from_unit: str, to_unit: str) -> Decimal:
    source, target = _UNITS.get(from_unit), _UNITS.get(to_unit)
    if source is None or target is None or source[0] != target[0]:
        raise PrimitiveError("UNIT_CONVERSION_ERROR", f"不支持从{from_unit}换算到{to_unit}")
    parsed = num(value)
    return parsed if source == target else parsed.scaleb(source[1] - target[1])


def scale(value, from_unit: str = "元", to_unit: str = "亿元") -> Decimal:
    return convert_unit(value, from_unit, to_unit)


def _fixed(value: Decimal, digits: int, signed=False) -> str:
    if type(digits) is not int or not 0 <= digits <= 1000:
        raise PrimitiveError("INVALID_PARAMS", "小数位数须为 0 至 1000 之间的整数")
    with localcontext(_CONTEXT, rounding=ROUND_HALF_UP):
        sign = "+" if signed and value > 0 else ""
        return f"{sign}{value:.{digits}f}"


@_precise
def pct(value, *, from_ratio: bool = False, digits: int = 2) -> str:
    parsed = num(value)
    return _fixed(parsed.scaleb(2) if from_ratio else parsed, digits) + "%"


def format_value(value, unit: str = "", *, digits: int = 2,
                 target_unit: str | None = None, signed: bool = False) -> str:
    if value is None:
        return "缺失"
    parsed = num(value) if target_unit is None else convert_unit(value, unit, target_unit)
    suffix = unit if target_unit is None else target_unit
    suffix = {"百分点": "个百分点", "pp": "个百分点", "％": "%"}.get(suffix, suffix)
    return _fixed(parsed, digits, signed) + suffix


def _valid_values(rows, value_key="value"):
    pairs = [(row, value) for row in rows if (value := pick_value(row, value_key)) is not None]
    if len({row.get("unit") for row, _ in pairs}) > 1:
        raise PrimitiveError("UNIT_MISMATCH", "有效观测单位不一致")
    return pairs


def _evidence(rows):
    return list(dict.fromkeys(row["evidence_id"] for row in rows if row.get("evidence_id")))


def pick_row(rows, **criteria) -> dict | None:
    """按提供的全部键精确取行，重复事实交由调用方处理。"""
    found = None
    for row in rows:
        if all(key in row and row[key] == value for key, value in criteria.items()):
            if found is not None:
                raise PrimitiveError("AMBIGUOUS_ROW", "同一组条件匹配到多条记录")
            found = row
    return found


def _missing_reason(row, field="value"):
    if row is None:
        return "缺少记录"
    if row.get("usable") is False:
        return "记录不可用"
    if row.get("quality_status") not in (None, "valid"):
        return "质量状态不合格"
    value = row.get(field)
    if value is None or isinstance(value, str) and not value.strip():
        return f"字段 {field} 缺值"
    return None


def pick_value(row, field: str = "value", *, allow_zero_default: bool = False,
               default_if_missing=None) -> Decimal | None:
    if _missing_reason(row, field) is None:
        return num(row[field])
    if allow_zero_default is True:
        return num(0 if default_if_missing is None else default_if_missing)
    return None


def _index_unique(rows, key):
    indexed = {}
    for row in rows:
        identifier = row.get(key)
        if identifier is None:
            raise PrimitiveError("INVALID_PARAMS", f"记录缺少配对键 {key}")
        if identifier in indexed:
            raise PrimitiveError("AMBIGUOUS_ROW", f"{key} 为 {identifier} 的记录重复")
        indexed[identifier] = row
    return indexed


def join_by_org(rows_a, rows_b, *, same_date: bool = True, org_key: str = "org_id") -> dict:
    left, right = _index_unique(rows_a, org_key), _index_unique(rows_b, org_key)
    paired, excluded = [], []
    for org in dict.fromkeys((*left, *right)):
        a, b = left.get(org), right.get(org)
        reason = _missing_reason(a) or _missing_reason(b)
        if reason is None and same_date and (not a.get("data_date") or a["data_date"] != b.get("data_date")):
            reason = "配对记录日期缺失或不同"
        if reason:
            excluded.append({"org_id": org, "reason": reason})
        else:
            num(a["value"])
            num(b["value"])
            paired.append((a, b))
    return {"paired": paired, "excluded": excluded}


def _as_date(value):
    try:
        parsed = date.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise PrimitiveError("INVALID_PARAMS", f"无效日期 {value}，须为 YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise PrimitiveError("INVALID_PARAMS", f"日期 {value} 须为 YYYY-MM-DD")
    return parsed


def _calendar_gaps(periods, frequency, *, observation_days=False):
    """日历日相差一天才连续。L2 的日频趋势按观测日比较，仅报告跨多月断层。"""
    gaps = []
    for left, right in zip(periods, periods[1:]):
        a, b = _as_date(left), _as_date(right)
        months = (b.year - a.year) * 12 + b.month - a.month
        if frequency == "月":
            gap = months != 1
        elif frequency == "季":
            quarters = (b.year - a.year) * 4 + (b.month - 1) // 3 - (a.month - 1) // 3
            gap = quarters != 1
        elif frequency == "日":
            gap = months > 1 if observation_days else (b - a).days != 1
        else:
            continue
        if gap:
            gaps.append(f"{left} 至 {right}")
    return gaps


def _period_rows(rows, indicator_id, as_of):
    if as_of is not None:
        _as_date(as_of)
    selected = []
    for row in rows:
        if indicator_id is not None and row.get("indicator_id") != indicator_id:
            continue
        day = row.get("data_date")
        if day is None and _missing_reason(row):
            continue
        _as_date(day)
        if as_of is None or day <= as_of:
            selected.append(row)
    return selected


def _series_frequency(selected, frequency):
    if frequency is None:
        declared = {r["frequency"] for r in selected if r.get("frequency")}
        if len(declared) > 1:
            raise PrimitiveError("INVALID_PARAMS", "期次轴含多种频率")
        frequency = next(iter(declared), None)
    if frequency is not None:
        _choice(frequency, ("季", "月", "日"), "频率")
    return frequency


def _single_indicator(rows):
    if len({r["indicator_id"] for r in rows if r.get("indicator_id") is not None}) > 1:
        raise PrimitiveError("INVALID_PARAMS", "走势与跨期配对须指定唯一指标")


def align_periods(rows, *, indicator_id: str | None = None, frequency: str | None = None,
                  n_periods: int | None = None, as_of: str | None = None) -> dict:
    """先取全辖有效日期，再截取最近窗口并检查日历连续性。"""
    if n_periods is not None and (type(n_periods) is not int or n_periods < 1):
        raise PrimitiveError("INVALID_PARAMS", "期数须为正整数")
    selected = _period_rows(rows, indicator_id, as_of)
    frequency = _series_frequency(selected, frequency)
    axis = sorted({r["data_date"] for r in selected if pick_value(r) is not None})
    if n_periods is not None:
        axis = axis[-n_periods:]
    gaps = _calendar_gaps(axis, frequency)
    enough = bool(axis) and (n_periods is None or len(axis) == n_periods)
    verifiable = len(axis) <= 1 or frequency is not None
    return {"periods": axis, "gaps": gaps, "is_contiguous": enough and verifiable and not gaps}


def _baseline_date(current, mode):
    try:
        if mode == "mom":
            return current.replace(day=1) - timedelta(days=1)
        if mode == "prev_quarter":
            first_month = 1 + 3 * ((current.month - 1) // 3)
            return current.replace(month=first_month, day=1) - timedelta(days=1)
        if mode == "ytd_start":
            return date(current.year - 1, 12, 31)
        # 同比保留月末身份，避免闰年二月的 28 日与 29 日错配。
        last_day = monthrange(current.year - 1, current.month)[1]
        at_month_end = current.day == monthrange(current.year, current.month)[1]
        day = last_day if at_month_end else min(current.day, last_day)
        return date(current.year - 1, current.month, day)
    except (OverflowError, ValueError) as exc:
        raise PrimitiveError("INVALID_PARAMS", "基期超出日历范围") from exc


def pair_period(rows, *, indicator_id: str | None = None, mode: Literal["yoy", "mom", "ytd_start", "prev_quarter"],
                as_of: str | None = None) -> dict:
    _choice(mode, ("yoy", "mom", "ytd_start", "prev_quarter"), "基期模式")
    selected = _period_rows(rows, indicator_id, as_of)
    _single_indicator(selected)
    # 目标期有记录但全部无效时，保留该期并逐家剔除，不能回退到旧期配对。
    curr = max((r["data_date"] for r in selected), default=None)
    if curr is None:
        return {"curr_date": None, "prev_date": None, "paired": [], "excluded": []}
    prev = _baseline_date(_as_date(curr), mode).isoformat()
    if mode == "yoy":
        frequency = _series_frequency(selected, None)
        if frequency in ("月", "季"):
            # 同月/季报告日可能因节假日移动。全辖只允许一个同期日期，避免混期。
            anniversary = _as_date(prev)
            width = 3 if frequency == "季" else 1
            dates = {_as_date(r["data_date"]) for r in selected}
            candidates = [d for d in dates if d.year == anniversary.year
                          and (d.month - 1) // width == (anniversary.month - 1) // width]
            if len(candidates) > 1:
                raise PrimitiveError("AMBIGUOUS_ROW", "同比基期内有多个报告日期")
            if candidates:
                prev = candidates[0].isoformat()
    result = join_by_org((r for r in selected if r["data_date"] == prev),
                         (r for r in selected if r["data_date"] == curr), same_date=False)
    accounted = {a["org_id"] for a, _ in result["paired"]} | {r["org_id"] for r in result["excluded"]}
    for org in dict.fromkeys(r["org_id"] for r in selected):
        if org not in accounted:
            result["excluded"].append({"org_id": org, "reason": "基期与当期均缺少记录"})
    return {"curr_date": curr, "prev_date": prev, **result}


def series_of(rows, *, org_id: str, indicator_id: str | None = None, periods: list[str]) -> list[dict]:
    wanted = set(periods)
    selected = [r for r in rows if r.get("org_id") == org_id
                and (indicator_id is None or r.get("indicator_id") == indicator_id)
                and r.get("data_date") in wanted]
    _single_indicator(selected)
    indexed = _index_unique(selected, "data_date")
    points = []
    for day in periods:
        row = indexed.get(day)
        value = pick_value(row)
        if value is None:
            points.append({"data_date": day, "value": None, "evidence_id": None, "missing": True})
        else:
            points.append({**row, "value": value, "evidence_id": row.get("evidence_id"), "missing": False})
    return points


_CONDITION_OPS = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le,
                  "==": operator.eq, "!=": operator.ne}


def _filter_condition(condition):
    """将条件编译成已校验的比较函数，None 表示此行缺少比较所需的有效值。"""
    if not isinstance(condition, dict) or not isinstance(condition.get("field"), str):
        raise PrimitiveError("INVALID_PARAMS", "筛选条件须指定字段")
    field, op = condition["field"], condition.get("op")
    _choice(op, (*_CONDITION_OPS, "between", "in", "is_null", "not_null"), "筛选运算符")
    if op == "is_null":
        return lambda row: row.get(field) is None or (isinstance(row[field], str) and not row[field].strip())
    if op == "not_null":
        return lambda row: _missing_reason(row, field) is None
    if "value" not in condition:
        raise PrimitiveError("INVALID_PARAMS", "筛选条件缺少比较值")
    target = condition["value"]
    numeric = field == "value" or op in {">", "<", ">=", "<=", "between"}
    if op == "between":
        if not isinstance(target, (list, tuple)) or len(target) != 2:
            raise PrimitiveError("INVALID_PARAMS", "区间条件须指定上下界")
        low, high = map(num, target)
        inclusive = condition.get("inclusive", True)
        if low > high or not isinstance(inclusive, bool):
            raise PrimitiveError("INVALID_PARAMS", "区间上下界或边界参数无效")
        def matches(value):
            return low <= value <= high if inclusive else low < value < high
    elif op == "in":
        if not isinstance(target, (tuple, list, set, frozenset)):
            raise PrimitiveError("INVALID_PARAMS", "成员条件须提供候选集合")
        numeric = numeric or any(isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) for v in target)
        options = tuple(map(num, target)) if numeric else target
        def matches(value):
            return value in options
    else:
        numeric = numeric or isinstance(target, (int, float, Decimal)) and not isinstance(target, bool)
        operand = num(target) if numeric else target
        compare = _CONDITION_OPS[op]
        def matches(value):
            return compare(value, operand)

    def evaluate(row):
        if _missing_reason(row, field):
            return None
        return matches(num(row[field]) if numeric else row[field])
    return evaluate


def filter_where(rows, conditions: list[dict], *, logic: Literal["and", "or"] = "and") -> dict:
    _choice(logic, ("and", "or"), "条件组合方式")
    checks = [_filter_condition(condition) for condition in conditions]
    matched, excluded = [], []
    for row in rows:
        results = [check(row) for check in checks]
        if (all(results) if logic == "and" else any(results)):
            matched.append(row)
        elif None in results:
            excluded.append({"org_id": row.get("org_id"), "reason": "比较字段缺失或记录不可用"})
    return {"matched": matched, "excluded": excluded}


def find_missing(rows, expected_orgs: list[str], *, indicator_id: str | None = None) -> list[dict]:
    indexed = _index_unique((r for r in rows if indicator_id is None or r.get("indicator_id") == indicator_id),
                            "org_id")
    missing = []
    for org in dict.fromkeys(expected_orgs):
        reason = _missing_reason(indexed.get(org))
        if reason:
            missing.append({"org_id": org, "reason": reason})
    return missing


def detect_sign_flip(prev_rows, curr_rows, *, kind: Literal["any", "pos_to_neg", "neg_to_pos"] = "any") -> dict:
    _choice(kind, ("any", "pos_to_neg", "neg_to_pos"), "翻转方向")
    result = join_by_org(prev_rows, curr_rows, same_date=False)
    matched = []
    for prev, curr in result["paired"]:
        start, end = num(prev["value"]), num(curr["value"])
        if start > 0 > end:
            direction = "pos_to_neg"
        elif start < 0 < end:
            direction = "neg_to_pos"
        else:
            continue
        if kind in ("any", direction):
            matched.append({"org_id": curr["org_id"], "prev_value": start, "curr_value": end,
                            "direction": direction,
                            "evidence_ids": [r["evidence_id"] for r in (prev, curr) if r.get("evidence_id")]})
    return {"matched": matched, "excluded": result["excluded"]}


# D. 统计聚合与多维分组

def _aggregation_method(agg, kind, allow_rate_arithmetic_mean=False):
    if agg == "sum" and kind not in ("amount", "delta"):
        raise PrimitiveError("NON_ADDITIVE_KIND", "只有金额或增量指标允许求和")
    if agg == "avg" and kind == "rate" and allow_rate_arithmetic_mean is not True:
        raise PrimitiveError("INVALID_METHOD_FOR_RATE", "率类指标须按分子分母重算，算术平均需显式允许")


def _aggregate_numbers(values, agg):
    if agg == "count":
        return len(values)
    if not values:
        raise PrimitiveError("INSUFFICIENT_DATA", "没有可聚合的有效观测")
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    if agg == "median":
        return _median_sorted(sorted(values))
    total = sum(values, Decimal(0))
    return total / len(values) if agg == "avg" else total


def _median_sorted(values):
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


@_precise
def sum_values(rows, *, kind: str, value_key: str = "value") -> Decimal:
    _aggregation_method("sum", kind)
    return _aggregate_numbers([v for _, v in _valid_values(rows, value_key)], "sum")


@_precise
def avg(rows, *, kind: str, allow_rate_arithmetic_mean: bool = False,
        value_key: str = "value") -> Decimal:
    _aggregation_method("avg", kind, allow_rate_arithmetic_mean)
    return _aggregate_numbers([v for _, v in _valid_values(rows, value_key)], "avg")


@_precise
def median_value(rows, *, value_key: str = "value") -> Decimal:
    return _aggregate_numbers([v for _, v in _valid_values(rows, value_key)], "median")


@_precise
def weighted_avg(rows_num, rows_den, *, scale_100: bool = True, same_date: bool = True) -> dict:
    """以有效配对的分子和与分母和重算；参与同一次加总的各机构须共用一个单位。"""
    joined = join_by_org(rows_num, rows_den, same_date=same_date)
    included = []
    excluded = list(joined["excluded"])
    for left, right in joined["paired"]:
        if not left.get("unit") or left["unit"] != right.get("unit"):
            excluded.append({"org_id": left["org_id"], "reason": "分子分母单位缺失或不一致"})
        else:
            included.append((left, right))
    if not included:
        raise PrimitiveError("INSUFFICIENT_DATA", "没有可按同单位配对的分子和分母")
    if same_date and len({left["data_date"] for left, _ in included}) != 1:
        raise PrimitiveError("INVALID_PARAMS", "加权汇总不能混合不同机构的不同期次")
    numerator = sum_values((left for left, _ in included), kind="amount")
    denominator = sum_values((right for _, right in included), kind="amount")
    if denominator == 0:
        raise PrimitiveError("DIVISION_BY_ZERO", "加权计算的分母合计为零")
    return {"value": numerator / denominator * (100 if scale_100 else 1),
            "numerator_sum": numerator, "denominator_sum": denominator,
            "paired_count": len(included), "excluded": excluded,
            "evidence_ids": _evidence(row for pair in included for row in pair)}


def count_where(rows, conditions_or_predicate, *, logic: Literal["and", "or"] = "and") -> int:
    valid = [row for row in rows if pick_value(row) is not None]
    if callable(conditions_or_predicate):
        return sum(1 for row in valid if conditions_or_predicate(row))
    return len(filter_where(valid, conditions_or_predicate, logic=logic)["matched"])


def distinct_count(rows, *, key: str = "org_id", valid_only: bool = True) -> int:
    return len({row[key] for row in rows if row.get(key) is not None
                and (not valid_only or pick_value(row) is not None)})


def _group_name(row, key):
    name = row.get(key)
    return "未分组" if name in (None, "") else name


def group_by(rows, *, group_key: str) -> dict:
    groups = {}
    for row in rows:
        groups.setdefault(_group_name(row, group_key), []).append(row)
    return groups


def _group_summary(group, members, agg):
    # 计数只关心有效记录的条数，与单位无关；金额、均值、极值须先统一量纲。
    if agg == "count":
        pairs = [(row, value) for row in members if (value := pick_value(row)) is not None]
    else:
        pairs = _valid_values(members)
    values = [v for _, v in pairs]
    return {"group": group, "value": _aggregate_numbers(values, agg) if values or agg == "count" else None,
            "valid_count": len(values), "excluded_count": len(members) - len(values),
            "unit": pairs[0][0].get("unit") if pairs and agg != "count" else None,
            "evidence_ids": _evidence(row for row, _ in pairs)}


@_precise
def aggregate_by(rows, *, group_key: str, kind: str, agg: Literal["sum", "avg", "median", "count", "min", "max"] = "sum",
                 allow_rate_arithmetic_mean: bool = False) -> list[dict]:
    _choice(agg, ("sum", "avg", "median", "count", "min", "max"), "agg")
    _aggregation_method(agg, kind, allow_rate_arithmetic_mean)
    return [_group_summary(group, members, agg)
            for group, members in group_by(rows, group_key=group_key).items()]


@_precise
def rollup(rows, *, levels: tuple[str, ...] = ("province", "region_name", "org_id"),
           kind: str = "amount") -> dict:
    """保留父级路径以区分同名地区；自底向上加总，并与有效明细总和逐层核对。"""
    _aggregation_method("sum", kind)
    if not levels or len(set(levels)) != len(levels):
        raise PrimitiveError("INVALID_PARAMS", "汇总层级须非空且不重复")
    rows = list(rows)
    valid = _valid_values(rows)
    total = sum((value for _, value in valid), Decimal(0)) if valid else None
    leaves = {}
    for row in rows:
        path = tuple(_group_name(row, key) for key in levels)
        leaves.setdefault(path, []).append(row)
    current = {path: _group_summary(path[-1], members, "sum") for path, members in leaves.items()}
    summaries = {}
    reconciled = total is not None
    for depth in range(len(levels) - 1, -1, -1):
        summaries[levels[depth]] = [{**item, "path": path} for path, item in current.items()]
        reconciled = reconciled and sum((item["value"] for item in current.values()
                                          if item["value"] is not None), Decimal(0)) == total
        if depth == 0:
            break
        parents = {}
        for path, child in current.items():
            parents.setdefault(path[:-1], []).append(child)
        current = {}
        for path, children in parents.items():
            amounts = [item["value"] for item in children if item["value"] is not None]
            current[path] = {"group": path[-1], "value": sum(amounts, Decimal(0)) if amounts else None,
                             "valid_count": sum(item["valid_count"] for item in children),
                             "excluded_count": sum(item["excluded_count"] for item in children),
                             "unit": valid[0][0].get("unit") if amounts else None,
                             "evidence_ids": list(dict.fromkeys(eid for item in children for eid in item["evidence_ids"]))}
    return {"levels": {level: summaries[level] for level in levels}, "reconciled": reconciled}


def _ytd_edges(periods, frequency, cutoff):
    """年初至截止日的窗口须覆盖首期，以及截止日已经结束的最后一期。"""
    first, last, end = (date.fromisoformat(day) for day in (periods[0], periods[-1], cutoff))
    if frequency == "日":
        return first.month == first.day == 1 and last == end
    if frequency in ("月", "季"):
        months = 3 if frequency == "季" else 1
        # 截止日处于月/季中间时，不要求尚未结束的月份或季度已有报表。
        completed_months = end.month if end.day == monthrange(end.year, end.month)[1] else end.month - 1
        completed_periods = completed_months // months
        return first.month <= months and (last.month - 1) // months + 1 >= completed_periods
    return True


def rolling_agg(rows, *, org_id: str, indicator_id: str | None = None,
                window: int | None = None, agg: Literal["sum", "avg"] = "sum", kind: str = "amount",
                mode: Literal["rolling", "ytd"] = "rolling", frequency: str | None = None,
                as_of: str | None = None) -> dict:
    """先固定全辖窗口，再检验单家机构的每期事实；窗口不足时返回缺数原因。"""
    _choice(agg, ("sum", "avg"), "agg")
    _choice(mode, ("rolling", "ytd"), "mode")
    _aggregation_method(agg, kind)
    if mode == "rolling" and (type(window) is not int or window <= 0):
        raise PrimitiveError("INVALID_PARAMS", "滚动窗口须为正整数")
    selected = _period_rows(rows, indicator_id, as_of)
    _single_indicator(selected)
    frequency = _series_frequency(selected, frequency)
    # 已上报但全辖缺值的期次也占窗口，否则会把上一期甚至上一年当作本期。
    reported = sorted({row["data_date"] for row in selected})
    cutoff = as_of or (reported[-1] if reported else None)
    if mode == "ytd":
        periods = [day for day in reported if day[:4] == cutoff[:4]]
    else:
        periods = reported[-window:]
    result = {"status": "insufficient_data", "value": None, "periods": periods, "evidence_ids": []}
    if not periods or mode == "rolling" and len(periods) < window:
        return {**result, "reason": "目标窗口有效期数不足"}
    if frequency is None and (len(periods) > 1 or mode == "ytd"):
        return {**result, "reason": "缺少观测频率，无法核对窗口连续性"}
    if _calendar_gaps(periods, frequency) or mode == "ytd" and not _ytd_edges(periods, frequency, cutoff):
        return {**result, "reason": "目标窗口存在断期"}
    points = series_of(selected, org_id=org_id, indicator_id=indicator_id, periods=periods)
    if any(point["missing"] for point in points):
        return {**result, "reason": "机构在目标窗口存在缺失观测"}
    value = sum_values(points, kind=kind) if agg == "sum" else avg(points, kind=kind)
    return {**result, "status": "ok", "value": value, "evidence_ids": _evidence(points)}


# E. 有效总体上的排序与分布。并列按机构号稳定输出，名次按值共享。
def extremum(rows, *, mode: Literal["max", "min"] = "max", value_key: str = "value") -> dict:
    _choice(mode, ("max", "min"), "极值方向")
    pairs = _valid_values(rows, value_key)
    if not pairs:
        raise PrimitiveError("INSUFFICIENT_DATA", "没有可用于求极值的有效观测")
    select = max if mode == "max" else min
    extreme = select(value for _, value in pairs)
    selected = [dict(row) for row, value in pairs if value == extreme]
    return {"mode": mode, "value": extreme, "rows": selected, "evidence_ids": _evidence(selected)}


def _ordered_values(rows, order, value_key):
    _choice(order, ("desc", "asc"), "排序方向")
    pairs = _valid_values(rows, value_key)
    # 先定同值时的机构次序，再稳定排序数值；不对降序键取负，以免 Decimal 发生舍入。
    pairs.sort(key=lambda pair: str(pair[0].get("org_id", "")))
    pairs.sort(key=lambda pair: pair[1], reverse=order == "desc")
    return pairs


def top_n(rows, *, n: int, order: Literal["desc", "asc"] = "desc", include_ties: bool = False,
          value_key: str = "value") -> dict:
    if type(n) is not int or n < 1:
        raise PrimitiveError("INVALID_PARAMS", "前 N 名的 N 须为正整数")
    ordered = _ordered_values(rows, order, value_key)
    end = min(n, len(ordered))
    boundary_tie = end < len(ordered) and ordered[end - 1][1] == ordered[end][1]
    if boundary_tie and include_ties:
        cutoff = ordered[end - 1][1]
        while end < len(ordered) and ordered[end][1] == cutoff:
            end += 1
    selected = [dict(row) for row, _ in ordered[:end]]
    return {"rows": selected, "has_boundary_tie": boundary_tie, "effective_n": end,
            "evidence_ids": _evidence(selected)}


def rank_rows(rows, *, order: Literal["desc", "asc"] = "desc", value_key: str = "value") -> list[dict]:
    ordered = _ordered_values(rows, order, value_key)
    counts = Counter(value for _, value in ordered)
    result = []
    rank = 0
    for i, (row, value) in enumerate(ordered):
        if i == 0 or value != ordered[i - 1][1]:
            rank = i + 1
        result.append({**row, "rank": rank, "population": len(ordered), "tied": counts[value] > 1})
    return result


@_precise
def percentile(rows, p, *, value_key: str = "value") -> Decimal:
    position = num(p)
    if not 0 <= position <= 100:
        raise PrimitiveError("INVALID_PARAMS", "分位点须介于 0 和 100 之间")
    values = sorted(value for _, value in _valid_values(rows, value_key))
    if not values:
        raise PrimitiveError("INSUFFICIENT_DATA", "没有可用于计算分位数的有效观测")
    idx = (len(values) - 1) * position / 100
    lower = int(idx)
    if idx == lower:
        return values[lower]
    return values[lower] + (idx - lower) * (values[lower + 1] - values[lower])


@_precise
def percentile_rank(rows, *, org_id: str, order: Literal["desc", "asc"] = "desc") -> dict:
    entry = pick_row(rank_rows(rows, order=order), org_id=org_id)
    if entry is None:
        raise PrimitiveError("INSUFFICIENT_DATA", "指定机构缺少有效排名观测")
    rank, population = entry["rank"], entry["population"]
    return {"org_id": org_id, "rank": rank, "population": population,
            "top_pct": Decimal(rank) / population * 100}


def _bucket_index(value, bins, right_closed):
    return bisect_left(bins, value) if right_closed else bisect_right(bins, value)


def _bucket_range(bins, i, right):
    def endpoint(value):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    lower = endpoint(bins[i - 1]) if i else "-∞"
    upper = endpoint(bins[i]) if i < len(bins) else "+∞"
    left_bracket = "(" if right or i == 0 else "["
    right_bracket = "]" if right and i < len(bins) else ")"
    return f"{left_bracket}{lower}, {upper}{right_bracket}"


def bucket(rows, *, bins: list, labels: list[str] | None = None, right_closed: bool = False,
           expected_orgs: list[str] | None = None) -> dict:
    """有机构清单时按清单逐家计数；否则按输入明细逐条入桶。"""
    if not isinstance(bins, (list, tuple)) or not bins:
        raise PrimitiveError("INVALID_PARAMS", "分桶须提供至少一个切分点")
    edges = [num(edge) for edge in bins]
    if any(left >= right for left, right in zip(edges, edges[1:])):
        raise PrimitiveError("INVALID_PARAMS", "分桶切分点须严格递增")
    if labels is not None and (not isinstance(labels, (list, tuple))
                               or len(labels) != len(edges) + 1
                               or not all(isinstance(label, str) for label in labels)):
        raise PrimitiveError("INVALID_PARAMS", "每个区间须对应一个文本标签")

    missing = []
    if expected_orgs is None:
        scoped = list(rows)
    else:
        scope = dict.fromkeys(expected_orgs)
        for row in rows:
            org = row.get("org_id")
            if org in scope:
                if scope[org] is not None:
                    raise PrimitiveError("AMBIGUOUS_ROW", f"机构 {org} 有多条分桶观测")
                scope[org] = row
        scoped = [row for row in scope.values() if row is not None]
        missing.extend(org for org, row in scope.items() if row is None)

    buckets = []
    for i in range(len(edges) + 1):
        interval = _bucket_range(edges, i, right_closed)
        buckets.append({"label": labels[i] if labels is not None else interval, "range_expr": interval,
                        "org_ids": [], "count": 0, "evidence_ids": []})
    groups = [[] for _ in buckets]
    for row, value in _valid_values(scoped):
        groups[_bucket_index(value, edges, right_closed)].append(row)
    for entry, group in zip(buckets, groups):
        entry.update(count=len(group), org_ids=[row.get("org_id") for row in group],
                     evidence_ids=_evidence(group))
    missing.extend(row.get("org_id") for row in scoped if pick_value(row) is None)
    valid_count = sum(len(group) for group in groups)
    return {"buckets": buckets, "missing_bucket": {"count": len(missing), "org_ids": missing},
            "valid_count": valid_count, "total_count": valid_count + len(missing)}


def _dispersion_of(values):
    """传入同单位、非空的有效 Decimal 序列，计算总体标准差。"""
    ordered = sorted(values)
    # 先减去共同起点，同一大额基数上的细小波动也能保留在方差中。
    origin = ordered[0]
    offsets = [value - origin for value in ordered]
    mean_offset = sum(offsets, Decimal(0)) / len(ordered)
    mean = origin + mean_offset
    variance = sum(((offset - mean_offset) ** 2 for offset in offsets), Decimal(0)) / len(ordered)
    return {"count": len(ordered), "min": ordered[0], "max": ordered[-1],
            "range": ordered[-1] - ordered[0], "mean": mean, "median": _median_sorted(ordered),
            "std_dev": variance.sqrt()}


@_precise
def dispersion(rows) -> dict:
    values = [value for _, value in _valid_values(rows)]
    if not values:
        raise PrimitiveError("INSUFFICIENT_DATA", "没有可计算离散程度的有效观测")
    return _dispersion_of(values)


@_precise
def find_outliers(rows, *, k=2) -> dict:
    multiplier = num(k)
    if multiplier < 0:
        raise PrimitiveError("INVALID_PARAMS", "标准差倍数须为非负数")
    pairs = _valid_values(rows)
    if len(pairs) < 2:
        return {"outliers": [], "lower": None, "upper": None, "reason": "有效观测不足两条"}
    stats = _dispersion_of([value for _, value in pairs])
    if stats["std_dev"] == 0:
        value = pairs[0][1]
        return {"outliers": [], "lower": value, "upper": value, "reason": "有效观测的总体标准差为零"}
    radius = multiplier * stats["std_dev"]
    lower, upper = stats["mean"] - radius, stats["mean"] + radius
    selected = [dict(row) for row, value in pairs if value < lower or value > upper]
    return {"outliers": selected, "lower": lower, "upper": upper,
            "reason": None}


@_precise
def diff(a, b) -> Decimal:
    return num(a) - num(b)


def compare_text(a, b, *, unit: str = "", mode: Literal["delta", "benchmark"] = "delta", digits: int = 2) -> str:
    _choice(mode, ("delta", "benchmark"), "比较模式")
    change = diff(a, b)
    if not change:
        return "持平" if mode == "delta" else "等于基准"
    if mode == "delta":
        phrase = "增加" if change > 0 else "减少"
    else:
        phrase = "高于基准" if change > 0 else "低于基准"
    suffix = "个百分点" if unit in ("%", "％") else unit
    return phrase + format_value(change.copy_abs(), suffix, digits=digits)


@_precise
def ratio(numerator, denominator, *, as_pct: bool = True) -> Decimal:
    a, b = num(numerator), num(denominator)
    if b == 0:
        raise PrimitiveError("DIVISION_BY_ZERO", "分母不能为零")
    return a / b * (100 if as_pct else 1)


@_precise
def growth(current, baseline, *, as_pct: bool = True) -> Decimal:
    base = num(baseline)
    if base <= 0:
        raise PrimitiveError("INVALID_GROWTH_BASELINE", "计算增速要求基期值大于零")
    return (num(current) - base) / base * (100 if as_pct else 1)


@_precise
def delta_points(current_rate, baseline_rate, *, unit: Literal["pp", "bp"] = "pp") -> Decimal:
    _choice(unit, ("pp", "bp"), "变化点单位")
    return diff(current_rate, baseline_rate) * (100 if unit == "bp" else 1)


@_precise
def gap_to_target(value, target, *, direction: Literal["gte", "lte"] = "gte") -> dict:
    _choice(direction, ("gte", "lte"), "达标方向")
    actual, threshold = num(value), num(target)
    gap = actual - threshold
    met = gap >= 0 if direction == "gte" else gap <= 0
    return {"gap": gap, "met": met, "shortfall": Decimal(0) if met else abs(gap),
            "completion_pct": ratio(actual, threshold) if direction == "gte" and threshold > 0 else None}


def _tolerance(value):
    result = num(value)
    if result < 0:
        raise PrimitiveError("INVALID_PARAMS", "容差不能为负")
    return result


@_precise
def validate_sum(rows, *, expected_total, kind: str = "amount", expected_count: int | None = None,
                 tolerance=Decimal("0.01")) -> dict:
    """分别核对可加性、观测数与金额，覆盖不足时仍披露可算的分项和。"""
    expected, tol = num(expected_total), _tolerance(tolerance)
    if expected_count is not None and (type(expected_count) is not int or expected_count < 0):
        raise PrimitiveError("INVALID_PARAMS", "预期数量须为非负整数")
    rows = list(rows)
    if expected_count is None:
        expected_count = len(rows)
    members = [row for row in rows if pick_value(row) is not None]
    count = len(members)
    coverage = count == expected_count
    issues = []
    if not coverage:
        issues.append("有效观测数与预期不符")
    actual = difference = None
    try:
        actual = sum_values(members, kind=kind)
        difference = actual - expected
    except PrimitiveError as exc:
        if exc.code not in ("NON_ADDITIVE_KIND", "UNIT_MISMATCH", "INSUFFICIENT_DATA"):
            raise
        issues.append(exc.message)
    matches = difference is not None and abs(difference) <= tol
    if difference is not None and not matches:
        issues.append("分项和与预期金额的差值超过容差")
    return {"passed": coverage and matches, "kind_valid": kind in ("amount", "delta"),
            "coverage_ok": coverage, "sum_matches": matches, "valid_count": count,
            "expected_count": expected_count, "coverage_ratio": Decimal(count) / expected_count if expected_count else None,
            "actual_sum": actual, "expected_total": expected, "diff": difference, "issues": issues}


@_precise
def validate_relation(payload: dict, *, relation: Literal["sum_equals", "ratio_equals", "order", "bounds"],
                      tolerance=Decimal("0.01")) -> dict:
    """核对给定字段的金融关系。缺少关系参数报错，不能核对的零分母给出失败结果。"""
    _choice(relation, ("sum_equals", "ratio_equals", "order", "bounds"), "核对关系")
    tol = _tolerance(tolerance)
    difference = None
    message = None
    try:
        if relation == "sum_equals":
            parts = payload["parts"]
            if not isinstance(parts, (list, tuple)):
                raise PrimitiveError("INVALID_PARAMS", "分项须为数值列表")
            if parts:
                difference = sum(map(num, parts), Decimal(0)) - num(payload["total"])
            passed = difference is not None and abs(difference) <= tol
            if not parts:
                message = "缺少可核对的分项"
        elif relation == "ratio_equals":
            denominator = num(payload["denominator"])
            if denominator == 0:
                message = "分母为零，无法核对比率"
            else:
                difference = ratio(payload["numerator"], denominator) - num(payload["rate"])
            passed = difference is not None and abs(difference) <= tol
        elif relation == "order":
            left, right, op = num(payload["left"]), num(payload["right"]), payload["op"]
            _choice(op, _CONDITION_OPS, "比较符")
            difference, passed = left - right, _CONDITION_OPS[op](left, right)
        else:
            value = num(payload["value"])
            lower, upper = payload.get("min_val"), payload.get("max_val")
            if lower is None and upper is None:
                raise PrimitiveError("INVALID_PARAMS", "范围校验至少需要一个边界")
            lower = num(lower) if lower is not None else None
            upper = num(upper) if upper is not None else None
            if lower is not None and upper is not None and lower > upper:
                raise PrimitiveError("INVALID_PARAMS", "下界不能大于上界")
            passed = (lower is None or lower <= value) and (upper is None or value <= upper)
    except KeyError as exc:
        raise PrimitiveError("INVALID_PARAMS", f"关系核对缺少字段 {exc.args[0]}") from exc
    return {"passed": passed, "relation": relation, "diff": difference,
            "message": message or ("核对通过" if passed else "不满足核对关系")}

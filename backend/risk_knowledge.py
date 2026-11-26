"""内置风险知识：按函件风险方面连接首轮指标，不依赖运行时读取业务工作簿。

数据来自项目父目录的指标对照和业务定义工作簿。参考条件只用于初筛，
不是正式监管阈值或自动发函依据；未收录的指标保留定义而不虚构观测。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable


_SOURCE = json.loads(Path(__file__).with_name("risk_knowledge_data.json").read_text(encoding="utf-8"))

DIMENSIONS = _SOURCE["dimensions"]

_POINT_DIMENSION = {
    "信用风险防控压力加大": "credit_risk", "支农支小定位有待夯实": "rural_micro_positioning",
    "资金业务需加强风险管控": "treasury_risk", "风险抵补能力有所削弱": "capital_and_buffer",
    "重点业务管理": "key_business_mgmt",
}
_ALIASES = {11: "deposit_growth", 27: "npl_ratio", 40: "overdue_ratio", 74: "funds_npl_balance", 75: "funds_npl_change"}


def _id(number: int) -> str:
    return _ALIASES.get(number, f"TEST_METRIC_{number:03d}")


def _aspect(number: int) -> str:
    if number <= 12: return "基本情况"
    if number <= 18: return "资本水平"
    if number <= 31: return "不良贷款余额与不良率双升、新形成不良增加"
    if number <= 36: return "潜在信用风险：关注类贷款增加"
    if number <= 41: return "潜在信用风险：逾期贷款增加"
    if number <= 47: return "不良贷款还原率"
    if number <= 55: return "大额贷款不良风险"
    if number <= 73: return "资金业务资产占比较高"
    if number <= 75: return "资金业务不良存在及变动"
    if number <= 78: return "债券投资规模及市场风险"
    if number <= 96: return "总体盈利能力下滑"
    return "拨备覆盖率继续下滑"


def _dimension(number: int) -> str:
    if number <= 18 or number >= 90: return "capital_and_buffer"
    if number <= 55: return "credit_risk"
    return "treasury_risk"


_THRESHOLDS = {13: ("<", 10.5), 15: ("<", 8.5), 17: ("<", 7.5), 25: (">", 30),
               73: (">", 40), 78: (">", 50), 95: ("<", 1.8), 97: ("<", 150)}
_DIRECTION = {28: (">", 0), 31: (">", 0), 36: (">", 0), 41: (">", 0),
              47: (">", 0), 54: (">", 0), 91: ("<", 0), 93: ("<", 0),
              96: ("<", 0), 98: ("<", 0)}
_FRACTIONS = {27: (23, 4), 35: (32, 4), 40: (37, 4), 51: (48, 4), 55: (53, 48), 73: (70, 1)}


def _rules(row: dict) -> list[dict]:
    number = row["number"]
    rules = []
    if number in _THRESHOLDS:
        op, value = _THRESHOLDS[number]
        rules.append({"type": "threshold", "op": op, "value": value, "label": f"内置参考筛查口径：{op}{value}"})
    if number in _DIRECTION:
        op, value = _DIRECTION[number]
        rules.append({"type": "direction", "op": op, "value": value, "label": "内置参考筛查口径：变化方向需关注"})
    if number == 74:
        rules.append({"type": "exists", "op": "!=", "value": 0, "label": "内置参考筛查口径：有效资金业务不良余额非零"})
    # 只有源资料标有排名、且在此风险面用于直接判断时才使用其排名。
    if number > 12 and row.get("ranking") and number != 55:
        rules.append({"type": "rank", "op": ">=", "value": 72, "label": "内置参考筛查口径：全辖82家排名靠后（排名≥72）"})
    return rules


def _make_indicator(row: dict) -> dict:
    # 显式维度的指标采用扫描目录原生字段。原对照表仅有行号，沿用其已有映射。
    if "dimension_id" in row or "dimensions" in row:
        dimensions = row["dimensions"] if "dimensions" in row else [row["dimension_id"]]
        if not dimensions or any(d not in DIMENSIONS for d in dimensions):
            raise ValueError(f"指标 {row['name']} 的维度未在 dimensions 中声明")
        role = row.get("role", "direct_judge")
        roles = {dim: row.get("dimension_roles", {}).get(dim, role) for dim in dimensions}
        ident = row.get("indicator_id") or _id(row["number"])
        return {**row, "indicator_id": ident, "dimensions": dimensions, "dimension_roles": roles,
                "role": roles[dimensions[0]], "aspect": row.get("aspect", row["name"]),
                "aliases": row.get("aliases", []), "source_status": row.get("source_status", "available"),
                "reference_rules": row.get("reference_rules", [])}
    n = row["number"]
    ident = _id(n)
    metric_kind = "delta" if "增量" in row["name"] or "增长点" in row["name"] else "rate" if "%" in (row["unit"] or "") or "百分比" in (row["unit"] or "") else "amount"
    primary = _dimension(n)
    dimensions = [primary]
    roles = {primary: "context" if n <= 12 else "direct_judge"}
    if n <= 12:
        for dimension in ("credit_risk", "treasury_risk", "rural_micro_positioning"):
            dimensions.append(dimension)
            roles[dimension] = "explain_change" if n in (1, 2, 4, 5, 6) else "context"
    if n == 23:
        dimensions.append("capital_and_buffer")
        roles["capital_and_buffer"] = "explain_change"
    if n == 97:
        dimensions.append("credit_risk")
        roles["credit_risk"] = "impact"
    aliases = list(dict.fromkeys(x for x in [f"TEST_METRIC_{n:03d}", row.get("name"), row.get("source_name")] if x and x != ident))
    result = {"indicator_id": ident, "number": n, "name": row["name"], "aliases": aliases,
              "metric_kind": metric_kind, "role": roles[primary], "dimension_roles": roles,
              "dimensions": dimensions, "aspect": _aspect(n), "source_status": "available",
              "reference_rules": _rules(row), "definition": row.get("definition"),
              "calculation": row.get("calculation"),
              # 本地测试库的统一单位；业务原文保留在 unit_definition，供内网适配时核对。
              "unit": "元" if n in (74, 75) else "百分点" if "增长点" in row["name"] else "%" if metric_kind == "rate" else "万元",
              "unit_definition": row.get("unit"),
              "period": row.get("period"), "interpretation": row.get("interpretation"),
              "constraints": row.get("constraints"), "mapping_quality": row.get("mapping_quality"),
              "frequency": row.get("frequency"), "source_name": row.get("source_name")}
    if n in _FRACTIONS:
        numerator, denominator = _FRACTIONS[n]
        result.update(numerator_id=_id(numerator), denominator_id=_id(denominator),
                      aggregation_note="仅在分子分母统计范围、单位和期次一致时，按汇总分子/汇总分母计算参考值；人行贷款能否作为监管不良率分母仍须核实。")
    return result


INDICATORS = {spec["indicator_id"]: spec for spec in map(_make_indicator, _SOURCE["metrics"])}

# 种子演示库还有三个首轮目录外指标，作为辅助上下文，不冒充首轮指标。
for _key, _name, _dim in (("new_npl_ratio", "不良发生净生成率", "credit_risk"),
                          ("pure_deposit_growth", "纯存款余额比年初增幅", "capital_and_buffer"),
                          ("funds_npl_ratio", "资金业务不良率", "treasury_risk")):
    INDICATORS[_key] = {"indicator_id": _key, "name": _name, "aliases": [], "metric_kind": "rate", "role": "context",
                        "dimension_roles": {_dim: "context"}, "dimensions": [_dim], "aspect": "补充背景",
                        "source_status": "available", "optional_demo_indicator": True, "reference_rules": []}

# 为原函每一条对照记录标注实际 ID；复合比较显式保留两个参与指标。
# 未匹配来源的项目保留独立缺源标识，不推定名称相近的数据可以替代。
for _row in _SOURCE["risk_mapping"]:
    _dim = _row.get("dimension_id") or _POINT_DIMENSION.get(_row["point"])
    _aspect_name = _row["aspect"]
    _matches = [ident for ident, item in INDICATORS.items() if not item.get("optional_demo_indicator") and item["source_status"] == "available" and
                (item["name"] == _row["letter_indicator"] or
                 (_row["mapped_indicator"] and _row["mapped_indicator"] in item["aliases"]))]
    if _row["row"] == 56: _matches = [_id(72), _id(2)]
    if _row["row"] == 63: _matches = [_id(78), _id(2)]
    # 银行业比较仍缺少外部同口径行业数据；已有行社指标仅为其中一端。
    if _row["row"] == 7: _matches = [_id(25)]
    if _row["row"] == 10: _matches = [_id(28)]
    _row["indicator_ids"] = _matches[:]
    # 优先使用原函对照表中直接匹配的释义，组合比较不能代替单项指标解释。
    if len(_matches) == 1 and _row["row"] not in (7, 10) and _row.get("meaning"):
        INDICATORS[_matches[0]].setdefault("meaning", _row["meaning"])
    if _matches and _row["row"] not in (7, 10):
        _row["source_status"] = "available"
        continue
    _ident = f"MISSING_SOURCE_{_row['row']:03d}"
    INDICATORS[_ident] = {"indicator_id": _ident, "name": _row["letter_indicator"], "aliases": [],
                          "metric_kind": "rate" if any(x in _row["letter_indicator"] for x in ("比例", "占比", "增速", "增长率", "票贷比")) else "amount",
                          "role": "direct_judge", "dimension_roles": {_dim: "direct_judge"}, "dimensions": [_dim],
                          "aspect": _aspect_name, "source_status": "missing_source", "reference_rules": [],
                          "definition": _row["meaning"], "source_name": _row["mapped_indicator"]}
    _row["indicator_ids"].append(_ident)
    _row["source_status"] = "missing_source"


for _ident, _name in _SOURCE["quickbi_names"].items():
    INDICATORS[_ident]["quickbi_name"] = _name


def get_letter_regulatory_floors() -> list[dict]:
    """读取撰函必须披露的底线条件及对应核查建议。"""
    return _SOURCE["letter_regulatory_floors"]


def get_letter_caution_guards() -> list[dict]:
    """读取需要排除或改写的扫描信号条件及表述约束。"""
    return _SOURCE["letter_caution_guards"]


def load_quickbi_bindings(path: str | Path | None = None) -> dict[str, dict]:
    """按内网字典名和同义词精确绑定。缺源、组合项和歧义项不能参与自动取数。"""
    path = path or os.environ.get("RISK_QUICKBI_DICT_FILE")
    if not path:
        raise ValueError("请配置 RISK_QUICKBI_DICT_FILE 指向指标字典.json")
    document = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    entries = document.get("metrics", document) if isinstance(document, dict) else document
    if isinstance(entries, list):
        rows = [(row.get("name") or row.get("indicatorName"), row) for row in entries if isinstance(row, dict)]
        if len(rows) != len(entries):
            raise ValueError("指标字典包含无效条目")
    elif isinstance(entries, dict):
        rows = entries.items()
    else:
        raise ValueError("指标字典须包含 metrics 对象或指标列表")

    # 同一旧来源简称可能对应余额、增量等不同口径；内置明确绑定优先。
    preferred = {item["quickbi_name"]: ident for ident, item in INDICATORS.items() if item.get("quickbi_name")}
    names: dict[str, set[str]] = {}
    for ident, item in INDICATORS.items():
        for name in [ident, item["name"], *item["aliases"]]:
            if name and name != "【待核】":
                names.setdefault(name, set()).add(ident)
    result = {}
    for key, row in rows:
        if not isinstance(key, str) or not key.strip() or not isinstance(row, dict):
            raise ValueError("指标字典缺少有效指标名或字段")
        source = row.get("source") or {}
        cube = row.get("cubeId") or (source.get("cubeId") if isinstance(source, dict) else None)
        status = str(row.get("status") or "").strip().lower()
        kind = str(row.get("kind") or "").strip().lower()
        if not cube or kind in {"composite", "组合", "组合指标"} or status in {
            "missing", "missing_source", "no_source", "unavailable", "disabled", "pending",
            "composite", "无源", "缺源", "待确认", "未接入", "停用", "组合指标",
        }:
            continue
        synonyms = row.get("synonyms") or []
        if not isinstance(synonyms, list) or not all(isinstance(s, str) for s in synonyms):
            raise ValueError(f"指标「{key}」的 synonyms 须为名称列表")
        source_name = row.get("indicatorName") or key
        if not isinstance(cube, str) or not isinstance(source_name, str):
            raise ValueError(f"指标「{key}」的 cubeId 和 indicatorName 须为字符串")
        labels = [key, source_name, *synonyms]
        explicit = row.get("indicator_id")
        if explicit is not None:
            matches = {explicit} if isinstance(explicit, str) and explicit in INDICATORS else set()
        else:
            matches = {preferred[label] for label in labels if label in preferred}
            if not matches:
                matches = set().union(*(names.get(label, set()) for label in labels))
        if len(matches) == 1:
            ident = matches.pop()
            if ident in result:
                raise ValueError(f"指标字典重复绑定 {ident}，请核对同义词与来源")
            result[ident] = {**row, "name": key, "cubeId": cube, "indicatorName": source_name}
    return result


def get_risk_dimensions(available_indicator_ids: Iterable[str] | None = None) -> list[dict]:
    """返回风险面摘要；传入库中指标 ID 后统计当前库实际可用绑定。"""
    available = set(available_indicator_ids) if available_indicator_ids is not None else {
        ident for ident, item in INDICATORS.items() if item["source_status"] == "available" and not item.get("optional_demo_indicator")
    }
    result = []
    for ident, meta in DIMENSIONS.items():
        items = [item for item in INDICATORS.values() if ident in item["dimensions"]]
        bound = [item for item in items if item["indicator_id"] in available]
        missing = [item for item in items if not item.get("optional_demo_indicator") and
                   item["indicator_id"] not in available]
        executable = any(item["dimension_roles"][ident] == "direct_judge" for item in bound)
        result.append({"id": ident, "dimension_id": ident, **meta, "local_indicator_count": len(bound),
                       "missing_source_count": len(missing), "executable": executable,
                       "status_message": "" if executable else "待内网取数接入"})
    return result


def get_knowledge_pack(dimension_ids: Iterable[str] | None = None) -> dict:
    """提供智能体知识包，保留原始指标映射、参考条件和归并纪律。"""
    selected = set(dimension_ids) if dimension_ids is not None else set(DIMENSIONS)
    return {"dimensions": [{"id": ident, **meta} for ident, meta in DIMENSIONS.items() if ident in selected],
            "indicators": [item for item in INDICATORS.values() if selected.intersection(item["dimensions"])],
            "risk_mapping": [row for row in _SOURCE["risk_mapping"] if (row.get("dimension_id") or _POINT_DIMENSION.get(row["point"])) in selected],
            "monitor_conditions": _SOURCE["monitor_conditions"],
            "instructions": "内置参考筛查口径仅供初筛，不修改原值，不代表正式发函条件。按行社和风险方面综合归并：同一问题多指标合并并保留证据，不同问题分别说明；结合未命中指标解释分子分母、历史和背景。不得把净增量认定为新形成，不同期间、范围和单位不得混算。率类不得简单平均，缺数不得补零；缺源标记待内网取数接入。仅引用本行社真实观测的 evidence_id，不得编造数值、原因或引用。"}

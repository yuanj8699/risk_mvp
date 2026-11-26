"""核验风险扫描事实覆盖、期次和汇总口径，不把缺数或不合格值当作零。"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from .services import number


def _unit_matches(actual: str, description: str | None) -> bool:
    """业务表描述的是单位类别，不能将“金额（按源单位统一）”当作字面单位。"""
    if not description:
        return True
    if "金额" in description:
        return actual in {"元", "万元", "亿元"}
    if "百分点" in description:
        return actual in {"百分点", "个百分点"}
    if "%" in description or "百分比" in description:
        return actual in {"%", "％"}
    return actual == description


def _usable_total(row: dict | None, catalog_unit: str, expected_unit: str | None) -> bool:
    return bool(row and row["usable"] and row["value"] is not None
                and row["unit"] == catalog_unit and _unit_matches(row["unit"], expected_unit))


def validate_fetched_metrics(fetched: dict, metric_definitions: dict[str, dict]) -> dict:
    """按当前批次核验，历史期次仅用于趋势，不参与当前覆盖及汇总。"""
    orgs = fetched["organizations"]
    records = fetched["latest"]
    indicators = {row["indicator_id"]: row for row in fetched["indicators"]}
    visibility = fetched.get("visibility") or {}
    clipped = bool(visibility.get("clipped"))
    source_totals = fetched.get("source_totals", {})
    # 本地定义中的 unit 是测试库展示单位。内网按业务允许的源单位验证，再要求同指标同单位。
    expected_units = {
        key: (item.get("unit_definition") if fetched.get("source") == "quickbi" else None) or item.get("unit")
        for key, item in metric_definitions.items()
    }
    by_pair = {(row["org_id"], row["indicator_id"]): row for row in records}
    missing_values = [row for row in records if row["value"] is None or row["quality_status"] != "valid"]
    stale = [row for row in records if row["issue"] and "期次" in row["issue"]]
    unit_mismatches = []
    for row in records:
        defined_unit = expected_units.get(row["indicator_id"])
        if row["unit"] != indicators[row["indicator_id"]]["unit"] or not _unit_matches(row["unit"], defined_unit):
            unit_mismatches.append({"org_id": row["org_id"], "indicator_id": row["indicator_id"],
                                    "unit": row["unit"], "catalog_unit": indicators[row["indicator_id"]]["unit"],
                                    "expected_unit": defined_unit})
    bad_units = {(row["org_id"], row["indicator_id"]) for row in unit_mismatches}
    # 目录与业务口径冲突的事实不能进入筛查或历史解释；保留原值以便排查。
    for row in (*records, *fetched["history"]):
        expected = expected_units.get(row["indicator_id"])
        if (row["org_id"], row["indicator_id"]) in bad_units or not _unit_matches(row["unit"], expected):
            row["usable"] = False
            row["issue"] = "指标单位与目录或业务口径不一致"
    covered = {row["org_id"] for row in records if row["usable"]}
    details = []
    reminders = ["率类指标不可对行社率值简单平均；应使用同口径汇总数据或完整的分子分母重算。",
                 "增量和百分点直接读取来源列；缺失值不补零，历史记录不替代当前批次。"]
    if clipped:
        reminders.append(visibility["visible_note"])
    if not source_totals:
        reminders.append("金额按本批可见行社值进行内部加总。")
    reminders.extend(fetched.get("date_notes", []))
    if fetched["missing_source"]:
        reminders.append("部分知识库指标待内网取数接入。")
    for indicator_id, catalog in sorted(indicators.items()):
        definition = metric_definitions.get(indicator_id, {})
        kind = definition.get("metric_kind")
        if kind not in {"amount", "rate", "delta"}:
            continue
        valid = [r for r in records if r["indicator_id"] == indicator_id and r["usable"]
                 and (r["org_id"], indicator_id) not in bad_units]
        detail = {"indicator_id": indicator_id, "indicator_name": catalog["indicator_name"],
                  "metric_kind": kind, "usable_org_count": len(valid),
                  "complete": bool(orgs) and len(valid) == len(orgs) and not clipped}
        totals = source_totals.get(indicator_id, {})
        province = totals.get("province")
        expected_unit = expected_units.get(indicator_id)
        if totals:
            detail["unit"] = catalog["unit"]
            detail["source_region_totals"] = {
                code: row["value"] for code, row in totals.get("regions", {}).items()
                if _usable_total(row, catalog["unit"], expected_unit)
            }
        if _usable_total(province, catalog["unit"], expected_unit):
            detail.update(source_province_total=province["value"], source_total_date=province["data_date"])
        elif province:
            reminders.append(f"{catalog['indicator_name']}的源表全辖合计行缺值、质量或单位异常，无法用于汇总核对。")
        if kind == "amount":
            sums = defaultdict(Decimal)
            for row in valid:
                sums[row["region_name"]] += Decimal(row["value"])
            total = sum((Decimal(row["value"]) for row in valid), Decimal(0))
            dates = {row["data_date"] for row in valid}
            comparable = detail["complete"] and len(dates) == 1
            detail.update({"method": "sum", "unit": catalog["unit"], "data_dates": sorted(dates),
                           "region_totals": {region: number(value) for region, value in sorted(sums.items())} if len(dates) <= 1 else {},
                           "province_total": number(total) if comparable else None,
                           "partial_total": number(total) if not comparable and len(dates) == 1 else None,
                           "region_sum_matches_province": sum(sums.values(), Decimal(0)) == total if comparable else None,
                           "note": "按同一数据日期加总有效行社金额。"})
            if province:
                diff = None
                if comparable and "source_province_total" in detail and province["data_date"] in dates:
                    diff = total - Decimal(province["value"])
                detail.update(diff_from_source_total=number(diff),
                              member_sum_matches_source_total=abs(diff) <= Decimal("0.01") if diff is not None else None,
                              note="差额为成员和减源表全辖合计，绝对差额在 0.01 个源单位以内视为相符。")
        elif kind == "rate":
            detail.update({"method": "no_simple_average", "weighted_value": None,
                           "note": "禁止对行社率值简单平均。"})
            numerator, denominator = definition.get("numerator_id"), definition.get("denominator_id")
            if numerator and denominator:
                pairs = [(by_pair.get((org["org_id"], numerator)), by_pair.get((org["org_id"], denominator))) for org in orgs]
                valid_pairs = [(n, d) for n, d in pairs if n and d and n["usable"] and d["usable"]
                               and (n["org_id"], numerator) not in bad_units and (d["org_id"], denominator) not in bad_units
                               and n["data_date"] == d["data_date"] and n["unit"] == d["unit"]]
                dates = {n["data_date"] for n, _ in valid_pairs}
                denominator_sum = sum((Decimal(d["value"]) for _, d in valid_pairs), Decimal(0))
                detail.update({"numerator_id": numerator, "denominator_id": denominator,
                               "weighted_pair_count": len(valid_pairs), "weighted_data_dates": sorted(dates)})
                units = {n["unit"] for n, _ in valid_pairs}
                if not clipped and len(valid_pairs) == len(orgs) and len(dates) == 1 and len(units) == 1 and denominator_sum != 0:
                    numerator_sum = sum((Decimal(n["value"]) for n, _ in valid_pairs), Decimal(0))
                    detail.update({"method": "weighted_recalculation", "weighted_value": number(numerator_sum / denominator_sum * 100),
                                   "numerator_sum": number(numerator_sum), "denominator_sum": number(denominator_sum),
                                   "note": "同日同单位的完整行社分子分母加总后重算，结果为参考百分比。" + definition.get("aggregation_note", "")})
                else:
                    detail["note"] = ("当前身份仅可见部分行社，无法重算全辖率。" if clipped else
                                      "分子分母缺失、期次或单位不一致，或分母为零，无法重算全辖率。")
            if "source_province_total" in detail:
                detail["province_total"] = None if clipped else detail["source_province_total"]
                if not clipped:
                    detail.update(method="source_province_total", note="全辖基准采用源表合计行。"
                                  + ("分子分母重算值仅供参考。" if detail["weighted_value"] is not None else detail["note"]))
        else:
            detail.update({"method": "source_delta", "note": "直接采用来源的增量或百分点，不重算。"})
        details.append(detail)
    return {"total_org_count": len(orgs), "covered_org_count": len(covered), "visibility": visibility,
            "available_indicator_count": len({r["indicator_id"] for r in records if r["usable"] and (r["org_id"], r["indicator_id"]) not in bad_units}),
            "missing_source": fetched["missing_source"], "missing_source_indicators": fetched["missing_source"],
            "missing_count": len(fetched["missing"]) + len(missing_values),
            "missing_observations": fetched["missing"], "missing_value_count": len(missing_values),
            "stale_count": len(stale), "stale_observations": [{"org_id": r["org_id"], "indicator_id": r["indicator_id"], "data_date": r["data_date"], "issue": r["issue"]} for r in stale],
            "unit_mismatch_count": len(unit_mismatches), "unit_mismatches": unit_mismatches,
            "accounting_details": details, "reminders": reminders}

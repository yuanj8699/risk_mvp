"""用合成原子观测检验七类金融口径，HTTP 用实际导入的 SQLite 冻结快照。"""
from copy import deepcopy
from decimal import Decimal
import json

import pytest

from test_api_workflow import good, import_csv, row, run, societies


DAY = "2026-09-30"
DEFS = {
    "balance": {"metric_kind": "amount", "unit": "万元"},
    "change": {"metric_kind": "delta", "unit": "万元"},
    "rate": {"metric_kind": "rate", "unit": "%", "frequency": "季",
             "numerator_id": "balance", "denominator_id": "loans"},
    "loans": {"metric_kind": "amount", "unit": "万元"},
    "coverage": {"metric_kind": "rate", "unit": "%"},
    "duration": {"metric_kind": "duration", "unit": "年"},
}
CHECKS = {
    "consecutive_trend": "period_contiguous",
    "benchmark_deviation": "deviation_exact",
    "bucket_distribution": "bucket_sum_matches_total",
    "concentration_cr": "concentration_bounds_valid",
    "incremental_contribution": "contribution_sum_reconciled",
    "metric_divergence": "divergence_pair_verified",
    "stress_test": "simulation_isolated_and_verified",
}


def obs(org_id, indicator, value, day=DAY, **overrides):
    return {
        "org_id": org_id, "org_name": f"{org_id}行社", "region_name": "本地区",
        "indicator_id": indicator, "unit": DEFS[indicator]["unit"], "data_date": day,
        "value": str(value) if value is not None else None, "usable": value is not None,
        "quality_status": "valid" if value is not None else "missing", "issue": None,
        "evidence_id": f"{org_id}/{indicator}/{day}", **overrides,
    }


def batch(latest, history=(), *, orgs=None, clipped=False):
    records = [*latest, *history]
    orgs = sorted({r["org_id"] for r in records}) if orgs is None else orgs
    return {
        "as_of": DAY, "source": "test", "snapshot_id": "test-snapshot",
        "organizations": [{"org_id": k, "org_name": f"{k}行社", "region_name": "本地区"} for k in orgs],
        "indicators": [{"indicator_id": k, "indicator_name": k, "unit": DEFS[k]["unit"],
                        "frequency": DEFS[k].get("frequency", "日")} for k in sorted({r["indicator_id"] for r in records})],
        "latest": latest, "history": list(history), "missing": [], "missing_source": [],
        "source_totals": {}, "visibility": {"clipped": clipped, "visible_note": "仅含可见机构"},
    }


def verify(fetched, operator, params, *, validation=None):
    from backend.verification_layer import run_verified_operator

    before = deepcopy(fetched)
    result = run_verified_operator(fetched, DEFS, operator, params, validation=validation)
    assert fetched == before
    assert {"operator", "status", "result_type", "params", "as_of", "coverage", "summary",
            "rows", "checks_passed", "caveats", "assumptions", "error", "evidence_ids"} <= result.keys()
    json.dumps(result, allow_nan=False)
    assert result["operator"] == operator
    assert result["result_type"] == ("simulated" if operator == "stress_test" else "observed_derived")
    ids = result["evidence_ids"]
    assert ids == sorted(set(ids))
    assert set(ids) <= {r["evidence_id"] for rows in (fetched["latest"], fetched["history"]) for r in rows}
    if result["status"] == "ok":
        assert CHECKS[operator] in result["checks_passed"]
        assert result["error"] is None
    elif result["status"] == "insufficient_data":
        assert result["caveats"] and result["summary"]
    return result


def blocked(result, code):
    assert result["status"] == "blocked"
    assert result["error"]["code"] == code
    assert result["error"]["message"] and result["rows"] == []


def test_api_uses_only_frozen_history_and_has_no_write_side_effects(api):
    # 冻结后才导入同日更正值，可区分重新查全库与读取任务冻结历史。
    org = societies(api)[0]
    start, end = "2040-05-01", "2040-05-02"
    first = good(import_csv(api, [row(org, value="0.80", data_date=start)]))["snapshot_id"]
    second = good(import_csv(api, [row(org, value="1.05", data_date=end)]))["snapshot_id"]
    task = run(api, second, "核验冻结期次")
    corrected = good(import_csv(api, [row(org, value="9.99", data_date=start)]))["snapshot_id"]
    frozen = task["selection"]["task_context"]["history_snapshot_ids"]
    assert first in frozen and second in frozen and corrected not in frozen
    endpoint = f"/api/runs/{task['id']}"
    observations = good(api.get(endpoint + "/metrics", params={
        "indicator_ids": "npl_ratio", "org_id": org["id"], "history": True, "limit": 1000,
    }))["evidence"]
    expected = {r["data_date"]: r["evidence_id"] for r in observations if r["snapshot_id"] in (first, second)}
    before = [good(api.get(path)) for path in ("/api/runs", "/api/audit")]
    payload = {"operator": "consecutive_trend", "params": {"indicator_id": "npl_ratio", "n_periods": 2,
               "direction": "up", "org_ids": [org["id"]]}}
    report = good(api.post(endpoint + "/verify", json=payload))
    assert report["status"] == "ok" and report["as_of"] == end
    assert len(report["rows"]) == 1
    hit = report["rows"][0]
    assert hit["org_id"] == org["id"] and hit["total_change"] == "0.25"
    assert [(r["data_date"], r["value"], r["evidence_id"]) for r in hit["series"]] == [
        (start, "0.8", expected[start]), (end, "1.05", expected[end])]
    assert good(api.post(endpoint + "/verify", json={**payload, "indicator_ids": ["npl_ratio"]})) == report
    assert [good(api.get(path)) for path in ("/api/runs", "/api/audit")] == before


def test_api_restricted_run_keeps_authorized_orgs_and_blocks_province_baseline(api):
    a, b = societies(api)[:2]
    snapshot = good(import_csv(api, [row(a, value="1.11"), row(b, value="2.22")]))["snapshot_id"]
    scoped = good(api.post("/api/runs", json={"snapshot_id": snapshot, "org_scope": [a["id"]]}))
    endpoint = f"/api/runs/{scoped['id']}/verify"
    payload = {"operator": "benchmark_deviation", "params": {"indicator_id": "npl_ratio"}}
    blocked(good(api.post(endpoint, json=payload)), "CLIPPED_SCOPE")
    payload["params"].update(baseline="custom", custom_value="1")
    report = good(api.post(endpoint, json=payload))
    assert report["coverage"]["total_orgs"] == 1
    assert [(r["org_id"], r["deviation"]) for r in report["rows"]] == [(a["id"], "0.11")]


def test_trend_uses_one_global_axis_latest_wins_and_never_skips_a_gap():
    q4, q1, q2 = "2025-12-31", "2026-03-31", "2026-06-30"
    hist = [obs("A", "rate", 1, q1), obs("A", "rate", 2, q2),
            obs("A", "rate", 0, evidence_id="older-revision"),
            obs("B", "rate", 1, q4), obs("B", "rate", 2, q1),
            obs("C", "rate", 5, q1), obs("C", "rate", 4, q2)]
    data = batch([obs("A", "rate", 3), obs("B", "rate", 3), obs("C", "rate", 2)], hist)
    params = {"indicator_id": "rate", "n_periods": 3, "direction": "up"}
    report = verify(data, "consecutive_trend", params)
    assert report["summary"]["target_periods"] == [q1, q2, DAY]
    assert [r["org_id"] for r in report["rows"]] == ["A"]
    assert report["rows"][0]["step_changes"] == ["1", "1"]
    assert "older-revision" not in report["evidence_ids"]
    excluded, = report["coverage"]["excluded_orgs"]
    assert excluded["org_id"] == "B" and q2 in excluded["reason"]
    down = verify(data, "consecutive_trend", {**params, "direction": "down"})
    assert [(r["org_id"], r["total_change"]) for r in down["rows"]] == [("C", "-3")]
    short = verify(data, "consecutive_trend", {**params, "as_of": q1})
    assert short["status"] == "insufficient_data"
    # 全辖只收到一季度与三季度时，两个不同日期也无法构成连续两季。
    gap = batch([obs("A", "rate", 3)], [obs("A", "rate", 1, q1)])
    broken = verify(gap, "consecutive_trend", {**params, "n_periods": 2})
    assert broken["status"] != "ok" and not broken["rows"]


def test_non_strict_trend_requires_strict_change_from_first_to_last():
    history = [obs(k, "rate", 1, d) for k in ("A", "B") for d in ("2026-03-31", "2026-06-30")]
    data = batch([obs("A", "rate", 2), obs("B", "rate", 1)], history)
    params = {"indicator_id": "rate", "n_periods": 3, "direction": "up"}
    assert verify(data, "consecutive_trend", params)["rows"] == []
    assert [r["org_id"] for r in verify(data, "consecutive_trend", {**params, "strict": False})["rows"]] == ["A"]


def test_rate_benchmark_prefers_source_then_weighted_then_median():
    from backend.metric_validator import validate_fetched_metrics

    rates = [obs("A", "rate", 10), obs("B", "rate", 20), obs("C", "rate", 90)]
    amounts = [obs(k, indicator, value) for k, numerator, denominator in
               (("A", 1, 10), ("B", 2, 10), ("C", 72, 80))
               for indicator, value in (("balance", numerator), ("loans", denominator))]
    data = batch(rates + amounts)
    params = {"indicator_id": "rate", "direction": "above", "min_deviation": "10"}
    blocked(verify(data, "benchmark_deviation", {**params, "baseline": "arithmetic_mean"}), "INVALID_METHOD_FOR_RATE")
    data["source_totals"]["rate"] = {"province": obs("HQ", "rate", 78)}
    val = validate_fetched_metrics(deepcopy(data), DEFS)
    source = verify(data, "benchmark_deviation", params, validation=val)
    assert source["summary"]["baseline_method"] == "source_province_total"
    assert [(r["org_id"], r["baseline_value"], r["deviation"]) for r in source["rows"]] == [("C", "78", "12")]
    data["source_totals"].clear()
    weighted = verify(data, "benchmark_deviation", params)
    assert weighted["summary"]["baseline_method"] == "province_weighted"
    assert weighted["summary"]["baseline_value"] == "75"
    assert "rate_no_simple_average" in weighted["checks_passed"]
    amounts[-1]["quality_status"] = "rejected"
    median = verify(data, "benchmark_deviation", params)
    assert median["summary"]["baseline_method"] == "median" and median["summary"]["baseline_value"] == "20"
    assert any("中位数" in note and "简单平均" in note for note in median["caveats"])
    data["visibility"]["clipped"] = True
    for baseline in ("auto", "source_province_total", "province_weighted"):
        blocked(verify(data, "benchmark_deviation", {**params, "baseline": baseline}), "CLIPPED_SCOPE")
    custom = verify(data, "benchmark_deviation", {**params, "baseline": "custom", "custom_value": "80"})
    assert [(r["org_id"], r["deviation"]) for r in custom["rows"]] == [("C", "10")]


def test_amount_benchmark_preserves_cents_above_binary_float_precision():
    data = batch([obs("A", "balance", "1000000000000000.01"), obs("B", "balance", "1000000000000000.03")])
    report = verify(data, "benchmark_deviation", {"indicator_id": "balance"})
    assert report["summary"]["baseline_method"] == "arithmetic_mean"
    assert report["summary"]["baseline_value"] == report["summary"]["median"] == "1000000000000000.02"
    assert {r["org_id"]: r["deviation"] for r in report["rows"]} == {"A": "-0.01", "B": "0.01"}


@pytest.mark.parametrize("right,expected", [(False, [1, 1, 2]), (True, [2, 1, 1])])
def test_bucket_edges_coverage_and_missing_values(right, expected):
    data = batch([obs("A", "balance", 0), obs("B", "balance", 1), obs("C", "balance", 2),
                  obs("D", "balance", 3), obs("E", "balance", None), obs("F", "balance", 99, usable=False),
                  obs("G", "balance", 88, quality_status="rejected"), obs("H", "balance", 1, unit="元")],
                 orgs=list("ABCDEFGHI"))
    report = verify(data, "bucket_distribution", {"indicator_id": "balance", "bins": [1, 2], "right_closed": right})
    assert [r["count"] for r in report["rows"]] == expected
    assert [Decimal(r["ratio_of_valid"]) for r in report["rows"]] == [Decimal(n) * 25 for n in expected]
    assert {k for r in report["rows"] for k in r["org_ids"]} == set("ABCD")
    missing = report["summary"]["missing_bucket"]
    assert {r["org_id"] for r in missing["excluded_orgs"] if r["reason"]} == set("EFGHI")
    assert sum(expected) + missing["count"] == report["coverage"]["total_orgs"] == 9
    assert set(report["evidence_ids"]) == {r["evidence_id"] for r in data["latest"][:4]}


@pytest.mark.parametrize("operator,params", [
    ("unknown", {}),
    ("bucket_distribution", {"indicator_id": "balance", "bins": [1, "1.0"]}),
    ("bucket_distribution", {"indicator_id": "balance", "bins": [1], "labels": ["only one"]}),
    ("consecutive_trend", {"indicator_id": "balance", "n_periods": True, "direction": "up"}),
    ("stress_test", {"formula_id": "unregistered_formula"}),
])
def test_bad_requests_have_stable_errors(operator, params):
    result = verify(batch([obs("A", "balance", 1)]), operator, params)
    blocked(result, "UNKNOWN_OPERATOR" if operator == "unknown" else "INVALID_PARAMS")


def test_cr5_cr10_tie_zero_and_partial_denominator_guards():
    values = [10, 9, 8, 7, 6, 6, 4, 3, 2, 1]
    data = batch([obs(k, "balance", v) for k, v in zip("ABCDEFGHIJ", values)][::-1])
    params = {"indicator_id": "balance", "top_n": [10, 5, 12]}
    report = verify(data, "concentration_cr", params)
    cr5, cr10, cr12 = report["rows"]
    assert [r["org_id"] for r in cr5["top_orgs"]] == list("ABCDE")
    assert cr5["has_boundary_tie"] and cr5["top_sum"] == "40" and cr5["total_value"] == "56"
    assert abs(Decimal(cr5["cr_pct"]) - Decimal("71.43")) < Decimal("0.005")
    assert Decimal(cr10["cr_pct"]) == Decimal(cr12["cr_pct"]) == 100
    assert cr12["effective_n"] == 10 and any("并列" in c for c in report["caveats"])
    data["latest"].pop()
    blocked(verify(data, "concentration_cr", params), "INCOMPLETE_COVERAGE")
    partial_params = {**params, "allow_partial_denominator": True}
    partial = verify(data, "concentration_cr", partial_params)
    assert partial["summary"]["total_value"] == "46" and partial["caveats"]
    data["latest"][0]["data_date"] = "2026-08-31"
    blocked(verify(data, "concentration_cr", partial_params), "INCOMPLETE_COVERAGE")
    for metric in ("rate", "change"):
        blocked(verify(batch([obs("A", metric, 1)]), "concentration_cr", {**params, "indicator_id": metric}), "INVALID_METRIC_KIND")
    for values, code in [([-1, 2], "NEGATIVE_VALUE_NOT_ALLOWED"), ([0, 0], "NON_POSITIVE_TOTAL")]:
        blocked(verify(batch([obs(k, "balance", v) for k, v in zip("AB", values)]), "concentration_cr", params), code)
    blocked(verify(batch([obs("A", "balance", 2)], clipped=True), "concentration_cr", partial_params), "CLIPPED_SCOPE")


def test_increment_modes_and_significant_offset():
    data = batch([obs(k, "change", v) for k, v in zip("ABC", [60, 40, -95])])
    params = {"indicator_id": "change", "top_n": 1}
    auto = verify(data, "incremental_contribution", params)
    assert auto["summary"]["effective_mode"] == "positive_only" and auto["summary"]["significant_offset"]
    assert auto["summary"]["net_total"] == "5" and auto["summary"]["negative_total"] == "-95"
    assert auto["rows"][0]["org_id"] == "A" and auto["summary"]["top_n_contribution_pct"] == "60"
    assert auto["caveats"]
    net = verify(data, "incremental_contribution", {**params, "mode": "net", "top_n": 3})
    assert sum(Decimal(r["contribution_pct"]) for r in net["rows"]) == 100
    assert net["rows"][-1]["contribution_pct"] == "-1900" and net["caveats"]
    data["latest"][-1]["value"] = "-100"
    blocked(verify(data, "incremental_contribution", {**params, "mode": "net"}), "ZERO_NET_TOTAL")
    data["visibility"]["clipped"] = True
    blocked(verify(data, "incremental_contribution", params), "CLIPPED_SCOPE")
    for value, mode in [(0, "auto"), (-10, "positive_only")]:
        result = verify(batch([obs("A", "change", value)]), "incremental_contribution", {**params, "mode": mode})
        assert result["status"] == "insufficient_data"


def test_increment_two_periods_requires_both_observations():
    start = "2026-06-30"
    data = batch([obs("A", "balance", "6.25"), obs("B", "balance", 8), obs("C", "balance", 9)],
                 [obs("A", "balance", "5.25", start), obs("B", "balance", 5, start)])
    report = verify(data, "incremental_contribution", {"base_indicator_id": "balance", "from_date": start, "to_date": DAY})
    assert report["summary"]["effective_mode"] == "net"
    assert [(r["org_id"], r["delta_value"], r["contribution_pct"]) for r in report["rows"]] == [("B", "3", "75"), ("A", "1", "25")]
    assert [r["org_id"] for r in report["coverage"]["excluded_orgs"]] == ["C"]
    for r in report["rows"]:
        assert set(r["evidence_ids"]) == {f"{r['org_id']}/balance/{day}" for day in (start, DAY)}


def test_divergence_quantile_ties_inner_join_date_alignment_and_spread():
    rates = [obs(k, "rate", v) for k, v in zip("ABCDE", [8, 8, 2, 1, 10])]
    covers = [obs(k, "coverage", v) for k, v in zip("ABCD", [1, 1, 5, 9])]
    covers[-1]["data_date"] = "2026-08-31"
    data = batch(rates + covers)
    pair = {"indicator_a": "rate", "indicator_b": "coverage"}
    q = {**pair, "rule_type": "quantile_opposite", "a_side": "top", "a_pct": 20, "b_side": "bottom", "b_pct": 20}
    report = verify(data, "metric_divergence", q)
    assert {r["org_id"] for r in report["rows"]} == {"A", "B"}
    assert {r["org_id"] for r in report["coverage"]["excluded_orgs"]} == {"D", "E"}
    assert report["summary"]["paired_org_count"] == 3 and any("并列" in c for c in report["caveats"])
    combo = {**pair, "rule_type": "threshold_combo", "a_op": "<=", "a_value": "2", "b_op": ">", "b_value": 4}
    mixed = verify(data, "metric_divergence", {**combo, "allow_date_mismatch": True})
    assert {r["org_id"] for r in mixed["rows"]} == {"C", "D"} and mixed["caveats"]
    spread = {**pair, "rule_type": "spread", "spread_op": "<", "spread_value": 0}
    hit, = verify(data, "metric_divergence", spread)["rows"]
    assert (hit["org_id"], hit["spread"]) == ("C", "-3")
    assert set(hit["evidence_ids"]) == {f"C/{key}/{DAY}" for key in ("rate", "coverage")}
    conflict = batch(rates + [obs("A", "balance", 1)])
    blocked(verify(conflict, "metric_divergence", {**spread, "indicator_b": "balance"}), "UNIT_MISMATCH_FOR_SPREAD")
    data["visibility"]["clipped"] = True
    blocked(verify(data, "metric_divergence", q), "CLIPPED_SCOPE")


@pytest.mark.parametrize("shock_type,value,direct", [("pct", 20, False), ("amount", 20, True)])
def test_npl_stress_fixed_provision_and_newly_breached(shock_type, value, direct):
    records = [obs(k, indicator, v) for k, balance, coverage in (("A", 100, 168), ("B", 100, 120), ("C", 0, 200))
               for indicator, v in (("balance", balance), ("coverage", coverage))]
    params = {"formula_id": "npl_shock_provision_coverage", "npl_balance_indicator_id": "balance",
              "provision_coverage_indicator_id": "coverage", "shock_type": shock_type, "shock_value": value}
    if direct:
        # 直接拨备余额与按率反推的数值有意不同，能检出错误选用反推余额。
        params["provision_balance_indicator_id"] = "loans"
        records.extend(obs(k, "loans", v) for k, v in zip("ABC", [156, 120, 0]))
    report = verify(batch(records), "stress_test", params)
    assert report["assumptions"] and report["summary"]["total_breached_count"] == 2
    assert report["summary"]["newly_breached_count"] == 1
    assert [r["org_id"] for r in report["coverage"]["excluded_orgs"]] == ["C"]
    a = next(r for r in report["rows"] if r["org_id"] == "A")
    assert (a["shocked_npl"], a["shocked_coverage"], a["coverage_drop_pp"]) == ("120", "130" if direct else "140", "-38" if direct else "-28")
    assert a["breaches_warning_line"] and len(a["evidence_ids"]) == (3 if direct else 2)


def test_bond_stress_bp_conversion_and_nonnegative_estimated_loss():
    records = [obs(k, indicator, v) for k, duration in (("A", "3.5"), ("B", "-1"), ("C", None))
               for indicator, v in (("balance", "12000"), ("duration", duration))]
    data = batch(records)
    params = {"formula_id": "rate_shock_bond_loss", "bond_balance_indicator_id": "balance",
              "duration_indicator_id": "duration", "shift_bp": 10}
    report = verify(data, "stress_test", params)
    a, = report["rows"]
    assert (a["org_id"], a["estimated_pnl"], a["estimated_loss"]) == ("A", "-42", "42")
    assert report["summary"]["total_estimated_loss"] == "42" and report["assumptions"]
    assert {r["org_id"] for r in report["coverage"]["excluded_orgs"]} == {"B", "C"}
    rebound = verify(data, "stress_test", {**params, "shift_bp": -10})
    assert rebound["rows"][0]["estimated_pnl"] == "42" and Decimal(rebound["rows"][0]["estimated_loss"]) == 0


def test_stress_excludes_conflicting_balance_units_without_inference():
    params = {"formula_id": "npl_shock_provision_coverage", "npl_balance_indicator_id": "balance",
              "provision_coverage_indicator_id": "coverage", "provision_balance_indicator_id": "loans",
              "shock_type": "amount", "shock_value": 10}
    data = batch([obs("A", "balance", 100), obs("B", "balance", 100),
                  obs("A", "coverage", 180), obs("B", "coverage", 180),
                  obs("A", "loans", 180), obs("B", "loans", 180, unit="亿元")])
    report = verify(data, "stress_test", params)
    assert [r["org_id"] for r in report["rows"]] == ["A"]
    assert [r["org_id"] for r in report["coverage"]["excluded_orgs"]] == ["B"]
    blocked(verify(data, "stress_test", {**params, "shock_value": 0}), "INVALID_PARAMS")


@pytest.mark.parametrize("corrupt", ["unknown_id", "bucket_count", "false_deviation", "stress_loss"])
def test_independent_post_verification_rejects_corrupted_result(corrupt):
    from backend.verification_layer import verify_operator_result

    data = batch([obs("A", "balance", 100), obs("B", "balance", 200), obs("A", "duration", 2)])
    if corrupt == "false_deviation":
        report = verify(data, "benchmark_deviation", {"indicator_id": "balance"})
        report["rows"][0]["deviation"] = "0"
    elif corrupt == "stress_loss":
        report = verify(data, "stress_test", {"formula_id": "rate_shock_bond_loss", "bond_balance_indicator_id": "balance",
                                             "duration_indicator_id": "duration", "shift_bp": 100})
        report["rows"][0]["estimated_loss"] = "0"
    else:
        report = verify(data, "bucket_distribution", {"indicator_id": "balance", "bins": [150]})
        if corrupt == "bucket_count":
            report["rows"][0]["count"] += 1
        else:
            report["rows"][0]["evidence_ids"] = ["absent-observation"]
    blocked(verify_operator_result(report, data, DEFS), "POST_VERIFY_FAILED")

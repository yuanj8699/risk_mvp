"""用冻结观测核对 L1 的数值口径、缺数行为和证据范围。"""
from decimal import Decimal as D, localcontext

import pytest

import backend.verification_primitives as p


@pytest.mark.parametrize("value", [None, "", "   ", True, False, "3万元", "1%", "NaN", "sNaN",
                                   float("inf"), "-Infinity", "1e1001", "1e-1001", "1" * 1001])
def test_num_rejects_values_that_cannot_be_used_as_financial_observations(value):
    with pytest.raises(p.PrimitiveError) as error:
        p.num(value)
    assert error.value.code == "INVALID_NUMBER"
    assert str(error.value) == error.value.message


def test_units_and_display_round_once_without_binary_arithmetic():
    assert p.num("0.0165") == D("0.0165")
    assert p.num(0.1) == D("0.1")
    assert p.num("1e1000") == D("1e1000")
    assert p.convert_unit("123456.789", "元", "万元") == D("12.3456789")
    assert p.convert_unit("1.2345", "亿元", "万元") == D(12345)
    assert p.scale(1234500000) == D("12.345")
    for unit, amount in [("小数", "0.0165"), ("%", "1.65"), ("bp", "165")]:
        assert p.convert_unit(amount, unit, "%") == D("1.65")
        assert p.convert_unit("1.65", "%", unit) == D(amount)
    for source, target in [("万元", "%"), ("吨", "吨"), ("小数", "元")]:
        with pytest.raises(p.PrimitiveError) as error:
            p.convert_unit(0, source, target)
        assert error.value.code == "UNIT_CONVERSION_ERROR"
    assert p.format_value(None, "元") == "缺失"
    assert p.format_value(1234500000, "元", target_unit="亿元") == "12.35亿元"
    assert p.format_value("0.245", "百分点", signed=True) == "+0.25个百分点"
    assert p.pct("-0.01655", from_ratio=True) == "-1.66%"
    assert p.pct("1.65", digits=1) == "1.7%"


def test_calculations_keep_64_digits_under_a_low_precision_caller_context():
    with localcontext(prec=4) as caller:
        caller.clear_flags()
        with pytest.raises(p.PrimitiveError):
            p.num("无效")
        assert p.diff("123456789.123456789", "123456789.1") == D("0.023456789")
        assert p.ratio(1, 3, as_pct=False) == D("0." + "3" * 64)
        assert p.sum_values([{"value": "123456789.123456789"}, {"value": "0.000000001"}],
                            kind="amount") == D("123456789.123456790")
        assert caller.prec == 4
        assert not any(caller.flags.values())


def _observation(org, day, value, **extras):
    return {"org_id": org, "indicator_id": "loan", "data_date": day, "value": value,
            "evidence_id": f"{org}/{day}", **extras}


def test_pick_distinguishes_missing_duplicate_invalid_and_explicit_default():
    a = {"org_id": "a", "value": "0.000", "delta": "2.1", "quality_status": None}
    assert p.pick_row([a], org_id="b") is None
    assert p.pick_row([a], org_id="a") == a
    assert p.pick_row([a], absent=None) is None
    with pytest.raises(p.PrimitiveError) as error:
        p.pick_row([a, dict(a)], org_id="a")
    assert error.value.code == "AMBIGUOUS_ROW"
    assert p.pick_value(a) == 0 and p.pick_value(a, "delta") == D("2.1")
    assert p.pick_value(None, allow_zero_default="false") is None
    for missing in (None, {}, {"value": "  "}, {**a, "usable": False}, {**a, "quality_status": "stale"}):
        assert p.pick_value(missing, default_if_missing=9) is None
        assert p.pick_value(missing, allow_zero_default=True) == D(0)
        assert p.pick_value(missing, allow_zero_default=True, default_if_missing="1.25") == D("1.25")
    with pytest.raises(p.PrimitiveError) as error:
        p.pick_value({"value": "not a number"}, allow_zero_default=True)
    assert error.value.code == "INVALID_NUMBER"


def test_join_counts_both_sides_and_only_permits_explicit_cross_date_pairing():
    one, two = "2024-01-31", "2024-02-29"
    left = [_observation("a", one, 0), _observation("b", two, None), _observation("c", one, 2),
            _observation("left", one, 4), _observation("undated", None, 3)]
    right = [_observation("a", one, 2), _observation("b", two, 8), _observation("c", two, 6),
             _observation("right", two, 6), _observation("undated", None, 0)]
    for same_date, expected in ((True, ["a"]), (False, ["a", "c", "undated"])):
        result = p.join_by_org(left, right, same_date=same_date)
        assert [a["org_id"] for a, b in result["paired"] if a["org_id"] == b["org_id"]] == expected
        assert {r["org_id"] for r in result["excluded"]} == {"a", "b", "c", "left", "right", "undated"} - set(expected)
        assert all(r["reason"] for r in result["excluded"])
    repeated = [{**left[0], "custom": "a"}, {**left[1], "custom": "a"}]
    with pytest.raises(p.PrimitiveError) as error:
        p.join_by_org(repeated, [], org_key="custom")
    assert error.value.code == "AMBIGUOUS_ROW"


def test_calendar_axis_uses_all_valid_orgs_but_series_keeps_missing_points():
    dec, jan, feb, mar = "2023-12-31", "2024-01-31", "2024-02-29", "2024-03-31"
    rows = [_observation("a", feb, "0"), _observation("a", jan, "12", usable=False),
            _observation("b", jan, "2"), _observation("a", dec, "3"),
            _observation("b", mar, "4"), _observation("a", "2024-01-01", 8, indicator_id="other")]
    axis = p.align_periods(rows, indicator_id="loan", frequency="月", as_of=feb, n_periods=3)
    assert axis == {"periods": [dec, jan, feb], "gaps": [], "is_contiguous": True}
    series = p.series_of(rows, indicator_id="loan", org_id="a", periods=axis["periods"])
    assert [r["value"] for r in series] == [D(3), None, D(0)]
    assert series[1] == {"data_date": jan, "value": None, "evidence_id": None, "missing": True}
    assert series[2]["evidence_id"] == f"a/{feb}"
    gap = p.align_periods([rows[0], rows[1], rows[3]], frequency="月")
    assert gap == {"periods": [dec, feb], "gaps": [f"{dec} 至 {feb}"], "is_contiguous": False}
    assert not p.align_periods(rows, frequency="月", n_periods=10)["is_contiguous"]
    assert not p.align_periods([], frequency="月")["is_contiguous"]


@pytest.mark.parametrize("freq,start,end,continuous", [
    ("日", "2024-02-28", "2024-02-29", True), ("日", "2024-02-28", "2024-03-01", False),
    ("季", "2023-12-31", "2024-03-31", True), ("季", "2023-12-31", "2024-06-30", False),
    ("月", "2024-01-31", "2024-02-29", True), ("月", "2024-01-31", "2024-03-31", False),
])
def test_calendar_steps_cover_leap_days_and_quarter_rollover(freq, start, end, continuous):
    rows = [_observation("a", d, 1, frequency=freq) for d in (end, start)]
    result = p.align_periods(rows)
    assert result["is_contiguous"] == continuous
    assert result["gaps"] == ([] if continuous else [f"{start} 至 {end}"])


@pytest.mark.parametrize("mode,curr,prev", [
    ("mom", "2025-01-31", "2024-12-31"), ("mom", "2024-03-31", "2024-02-29"),
    ("yoy", "2025-02-28", "2024-02-29"), ("yoy", "2024-02-29", "2023-02-28"),
    ("ytd_start", "2024-08-31", "2023-12-31"), ("prev_quarter", "2024-01-31", "2023-12-31"),
    ("prev_quarter", "2024-08-31", "2024-06-30"),
])
def test_pair_period_anchors_to_calendar_not_to_previous_available_record(mode, curr, prev):
    before, after = _observation("a", prev, "9"), _observation("a", curr, "10")
    rows = [before, after, _observation("a", "2026-12-31", 50),
            _observation("b", curr, 1), _observation("b", prev, 9, quality_status="bad"),
            _observation("c", "2021-04-30", 1), _observation("a", curr, 0, indicator_id="other")]
    result = p.pair_period(rows, indicator_id="loan", mode=mode, as_of=curr)
    assert (result["curr_date"], result["prev_date"]) == (curr, prev)
    assert result["paired"] == [(before, after)]
    assert {r["org_id"] for r in result["excluded"]} == {"b", "c"}


def test_filter_where_interval_logic_membership_and_explicit_null():
    rows = [{"org_id": str(i), "value": v} for i, v in enumerate(["-1", "0.000", "1", "2", None])]
    rows.append({"org_id": "stale", "value": "1", "quality_status": "stale"})
    interval = {"field": "value", "op": "between", "value": [-1, 1]}
    member = {"field": "org_id", "op": "in", "value": ["0", "3"]}
    result = p.filter_where(rows, [interval, member])
    assert [r["org_id"] for r in result["matched"]] == ["0"]
    assert {r["org_id"] for r in result["excluded"]} == {"4", "stale"}
    result = p.filter_where(rows, [{**interval, "inclusive": False}, member], logic="or")
    assert [r["org_id"] for r in result["matched"]] == ["0", "1", "3"]
    for op, target, ids in [("==", "0", ["1"]), ("!=", 0, ["0", "2", "3"]),
                            ("<", 0, ["0"]), ("<=", 0, ["0", "1"]),
                            (">", 1, ["3"]), (">=", 1, ["2", "3"]),
                            ("in", [0, 2], ["1", "3"])]:
        result = p.filter_where(rows, [{"field": "value", "op": op, "value": target}])
        assert [r["org_id"] for r in result["matched"]] == ids
        assert {r["org_id"] for r in result["excluded"]} == {"4", "stale"}
    for op, ids in (("is_null", ["4"]), ("not_null", ["0", "1", "2", "3"])):
        result = p.filter_where(rows, [{"field": "value", "op": op}])
        assert [r["org_id"] for r in result["matched"]] == ids


def test_find_missing_and_strict_sign_flips_preserve_zero_and_citations():
    prev = [_observation(org, "2024-01-31", v) for org, v in zip("abcdef", ["1", "-1", 0, "-1", 3, 2])]
    curr = [_observation(org, "2024-02-29", v) for org, v in zip("abcde", ["-0.0001", 2, -2, "0.00", None])]
    curr += [_observation("f", "2024-02-29", -1, usable=False),
             _observation("g", "2024-02-29", -1, quality_status="bad")]
    missing = p.find_missing(curr, list("abcdefgh"), indicator_id="loan")
    assert [r["org_id"] for r in missing] == list("efgh")
    assert len({r["reason"] for r in missing}) == 4
    result = p.detect_sign_flip(prev, curr)
    assert [(r["org_id"], r["direction"], r["prev_value"], r["curr_value"]) for r in result["matched"]] == [
        ("a", "pos_to_neg", D(1), D("-0.0001")), ("b", "neg_to_pos", D(-1), D(2))]
    assert result["matched"][0]["evidence_ids"] == ["a/2024-01-31", "a/2024-02-29"]
    assert {r["org_id"] for r in result["excluded"]} == set("efg")
    assert [r["org_id"] for r in p.detect_sign_flip(prev, curr, kind="neg_to_pos")["matched"]] == ["b"]


def _amount(org, value, day="2026-03-31", **extra):
    return {"org_id": org, "value": value, "unit": "万元", "data_date": day,
            "evidence_id": f"{org}/{day}/{value}", **extra}


def test_aggregation_preserves_decimal_precision_and_excludes_invalid_rows():
    amount = "100000000000000000000000000000.12"
    rows = [{"balance": amount, "unit": "元"}, {"balance": "0.02", "unit": "元"},
            {"balance": 90, "unit": "万元", "quality_status": "stale"}, {"balance": None}]
    assert p.sum_values(rows, value_key="balance", kind="delta") == D("100000000000000000000000000000.14")
    assert p.avg(rows, value_key="balance", kind="amount") == D("50000000000000000000000000000.07")
    assert p.median_value(rows, value_key="balance") == D("50000000000000000000000000000.07")
    assert p.median_value([*rows, {"balance": "0.25", "unit": "元"}], value_key="balance") == D("0.25")
    for operation in (lambda data: p.sum_values(data, kind="delta"), p.median_value,
                      lambda data: p.avg(data, kind="amount")):
        with pytest.raises(p.PrimitiveError) as err:
            operation([{"value": None}, {"value": 10, "usable": False}])
        assert err.value.code == "INSUFFICIENT_DATA"


@pytest.mark.parametrize("operation,code", [
    (lambda data: p.sum_values(data, kind="rate"), "NON_ADDITIVE_KIND"),
    (lambda data: p.sum_values(data, kind="count"), "NON_ADDITIVE_KIND"),
    (lambda data: p.avg(data, kind="rate"), "INVALID_METHOD_FOR_RATE"),
    (lambda data: p.aggregate_by(data, group_key="province", kind="rate"), "NON_ADDITIVE_KIND"),
    (lambda data: p.aggregate_by(data, group_key="province", kind="rate", agg="avg"), "INVALID_METHOD_FOR_RATE"),
    (lambda data: p.rollup(data, kind="rate"), "NON_ADDITIVE_KIND"),
    (lambda data: p.rolling_agg(data, kind="rate", org_id="A", window=1), "NON_ADDITIVE_KIND"),
    (lambda data: p.rolling_agg(data, kind="rate", org_id="A", window=1, agg="avg"), "INVALID_METHOD_FOR_RATE"),
])
def test_rate_and_kind_guards_apply_before_insufficient_data(operation, code):
    with pytest.raises(p.PrimitiveError) as err:
        operation([])
    assert err.value.code == code


@pytest.mark.parametrize("operation", [
    lambda data: p.sum_values(data, kind="amount"), lambda data: p.avg(data, kind="delta"),
    lambda data: p.aggregate_by(data, group_key="province", kind="amount"), p.rollup,
])
def test_units_must_match_for_the_values_being_aggregated(operation):
    with pytest.raises(p.PrimitiveError) as err:
        operation([_amount("A", 1), _amount("B", 1, unit="亿元")])
    assert err.value.code == "UNIT_MISMATCH"


def test_weighted_recalculation_counts_only_usable_pairs_with_evidence():
    numerators = [_amount("A", 10), _amount("B", 30), _amount("C", 1), _amount("D", 2), _amount("E", 3)]
    denominators = [_amount("A", 100), _amount("B", 400), _amount("C", 1, day="2026-02-28"),
                    _amount("D", None), _amount("E", 3, unit="亿元"), _amount("F", 2)]
    result = p.weighted_avg(numerators, denominators)
    assert (result["numerator_sum"], result["denominator_sum"], result["value"], result["paired_count"]) == (
        D("40"), D("500"), D("8"), 2)
    assert {r["org_id"] for r in result["excluded"]} == {"C", "D", "E", "F"}
    assert set(result["evidence_ids"]) == {r["evidence_id"] for r in numerators[:2] + denominators[:2]}
    assert p.weighted_avg(numerators[:2], denominators[:2], scale_100=False)["value"] == D("0.08")
    assert p.weighted_avg(numerators[2:3], denominators[2:3], same_date=False)["value"] == D("100")


@pytest.mark.parametrize("numerators,denominators,code", [
    ([_amount("A", 1), _amount("B", 1)], [_amount("A", -2), _amount("B", 2)], "DIVISION_BY_ZERO"),
    ([_amount("A", 1)], [_amount("B", 10)], "INSUFFICIENT_DATA"),
    ([_amount("A", 1), _amount("B", 2, unit="元")],
     [_amount("A", 1), _amount("B", 2, unit="元")], "UNIT_MISMATCH"),
    ([_amount("A", 1), _amount("B", 2, day="2026-04-30")],
     [_amount("A", 1), _amount("B", 2, day="2026-04-30")], "INVALID_PARAMS"),
])
def test_weighted_recalculation_rejects_noncomparable_populations(numerators, denominators, code):
    with pytest.raises(p.PrimitiveError) as err:
        p.weighted_avg(numerators, denominators)
    assert err.value.code == code


def test_counting_and_group_statistics_keep_missing_groups_and_genuine_zeros():
    rows = [_amount("C", None), _amount("B", 0, area="东"), _amount("A", 4, area="东"),
            _amount("X", 100, area="东", usable=False), _amount(None, 6, area="东"), _amount("A", 8, area="东")]
    assert p.count_where(rows, lambda row: row["value"] >= 4) == 3
    assert p.count_where(rows, [{"field": "value", "op": ">=", "value": 0}]) == 4
    assert p.count_where(rows, [{"field": "value", "op": "is_null"}]) == 0
    assert p.distinct_count(rows) == 2 and p.distinct_count(rows, valid_only=False) == 4
    groups = p.group_by(rows, group_key="area")
    assert list(groups) == ["未分组", "东"] and groups["东"] == rows[1:]
    for method, expected in (("sum", 18), ("avg", D("4.5")), ("median", 5), ("min", 0), ("max", 8), ("count", 4)):
        absent, east = p.aggregate_by(rows, group_key="area", agg=method, kind="amount")
        assert absent["value"] == (0 if method == "count" else None)
        assert (east["value"], east["valid_count"], east["excluded_count"]) == (expected, 4, 1)
        assert set(east["evidence_ids"]) == {r["evidence_id"] for r in rows if r.get("area") and r.get("usable", True)}
    rates = [{"value": "1.2", "unit": "%"}, {"value": "1.4", "unit": "%"}]
    assert p.avg(rates, kind="rate", allow_rate_arithmetic_mean=True) == D("1.3")
    assert p.aggregate_by(rates, kind="rate", group_key="area", agg="avg",
                          allow_rate_arithmetic_mean=True)[0]["value"] == D("1.3")


def test_rollup_distinguishes_regions_with_the_same_name_under_different_parents():
    rows = [_amount("A", "0.12", province="甲", region_name="东"), _amount("B", "0.07", province="乙", region_name="东"),
            _amount("C", "0.03", province="甲", region_name="东"), _amount("D", None, province="乙")]
    result = p.rollup(rows)
    assert result["reconciled"] and list(result["levels"]) == ["province", "region_name", "org_id"]
    regions = {item["path"]: item for item in result["levels"]["region_name"]}
    assert (regions["甲", "东"]["value"], regions["乙", "东"]["value"]) == (D("0.15"), D("0.07"))
    assert regions["乙", "未分组"]["value"] is None
    a, b = result["levels"]["province"]
    assert a["valid_count"] == 2 and set(a["evidence_ids"]) == {rows[i]["evidence_id"] for i in (0, 2)}
    assert (b["valid_count"], b["excluded_count"], b["value"]) == (1, 1, D("0.07"))
    assert not p.rollup([_amount("D", None)])["reconciled"]


def test_rolling_and_ytd_respect_cutoff_calendar_and_target_org_coverage():
    from copy import deepcopy

    periods = ["2025-12-31", "2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]
    rows = [_amount("A", str(i), day, indicator_id="净增", frequency="月") for i, day in enumerate(periods)]
    rows.append(_amount("B", 5, periods[2], indicator_id="净增", frequency="月"))
    original = deepcopy(rows)
    params = dict(org_id="A", indicator_id="净增", as_of="2026-03-31", kind="delta")
    rolling = p.rolling_agg(rows, window=2, agg="avg", **params)
    ytd = p.rolling_agg(rows, mode="ytd", **params)
    assert (rolling["value"], rolling["periods"]) == (D("2.5"), periods[2:4])
    assert (ytd["status"], ytd["value"], ytd["periods"]) == ("ok", D(6), periods[1:4])
    assert set(ytd["evidence_ids"]) == {r["evidence_id"] for r in rows[1:4]}
    assert rows == original
    for incomplete in ([r for r in rows if r["data_date"] != periods[2]],
                       [r for r in rows if not (r["org_id"] == "A" and r["data_date"] == periods[2])]):
        for options in (dict(window=2), dict(mode="ytd")):
            result = p.rolling_agg(incomplete, **options, **params)
            assert (result["status"], result["value"]) == ("insufficient_data", None)
    for sample in (rows[2:], rows[:3], rows[:1]):
        assert p.rolling_agg(sample, mode="ytd", **params)["status"] == "insufficient_data"
    assert p.rolling_agg(rows, window=6, **params)["status"] == "insufficient_data"


def test_wholly_missing_final_period_never_rolls_back_to_older_value_or_year():
    rows = [_amount("A", 10, "2025-11-30"), _amount("A", 20, "2025-12-31"), _amount("A", None, "2026-01-31")]
    for options in (dict(window=2), dict(mode="ytd")):
        result = p.rolling_agg(rows, org_id="A", frequency="月", **options)
        assert result["status"] == "insufficient_data" and result["value"] is None
        assert result["periods"][-1] == "2026-01-31"


def test_unknown_frequency_and_mixed_metrics_cannot_certify_a_time_series():
    days = ["2026-01-31", "2026-03-31"]
    rows = [_observation("A", day, 10) for day in days]
    assert not p.align_periods(rows)["is_contiguous"]
    for opts in ({"window": 2}, {"mode": "ytd"}):
        assert p.rolling_agg(rows, org_id="A", **opts)["status"] == "insufficient_data"
    rows[1]["indicator_id"] = "provision"
    for operation in (lambda: p.rolling_agg(rows, org_id="A", window=2, frequency="季"),
                      lambda: p.series_of(rows, org_id="A", periods=days),
                      lambda: p.pair_period(rows, mode="mom")):
        with pytest.raises(p.PrimitiveError) as error:
            operation()
        assert error.value.code == "INVALID_PARAMS"


@pytest.mark.parametrize("frequency,prev", [("月", "2023-03-30"), ("季", "2023-02-28")])
def test_yoy_aligns_same_reporting_period_when_report_days_move(frequency, prev):
    rows = [_observation("A", day, 1, frequency=frequency) for day in (prev, "2024-03-29")]
    result = p.pair_period(rows, mode="yoy")
    assert result["prev_date"] == prev and result["paired"] == [(rows[0], rows[1])]
    with pytest.raises(p.PrimitiveError) as error:
        p.pair_period(rows + [_observation("A", "2023-03-31", 2, frequency=frequency)], mode="yoy")
    assert error.value.code == "AMBIGUOUS_ROW"


def test_tied_extrema_and_competition_rank_preserve_source_rows():
    rows = [{"org_id": org, "balance": value, "evidence_id": "ev-" + org}
            for org, value in [("B", "2.50"), ("C", "0"), ("A", "2.5"), ("D", None)]]
    rows.append({"org_id": "E", "balance": "100", "quality_status": "rejected"})
    before = [dict(row) for row in rows]
    largest = p.extremum(rows, value_key="balance")
    assert largest["value"] == D("2.5")
    assert set(largest["evidence_ids"]) == {"ev-A", "ev-B"}
    assert p.extremum(rows, mode="min", value_key="balance")["evidence_ids"] == ["ev-C"]
    ranked = p.rank_rows(rows, value_key="balance")
    assert [(r["org_id"], r["rank"], r["population"], r["tied"]) for r in ranked] == [
        ("A", 1, 3, True), ("B", 1, 3, True), ("C", 3, 3, False)]
    for include_ties, ids in [(False, ["ev-A"]), (True, ["ev-A", "ev-B"])]:
        head = p.top_n(rows, n=1, value_key="balance", include_ties=include_ties)
        assert head["evidence_ids"] == ids and head["effective_n"] == len(ids)
        assert head["has_boundary_tie"]
    bottom = p.top_n(rows, n=8, value_key="balance", order="asc")
    assert [r["org_id"] for r in bottom["rows"]] == ["C", "A", "B"]
    assert not bottom["has_boundary_tie"] and bottom["effective_n"] == 3
    assert rows == before


def test_r7_interpolation_and_rank_percentage_use_effective_population():
    rows = [{"org_id": org, "value": value} for org, value in
            [("A", "0.01"), ("B", "0.01"), ("C", "0.09"), ("D", "0.99"), ("E", None)]]
    assert [p.percentile(rows, q) for q in (0, "50", 75, 100)] == [
        D("0.01"), D("0.05"), D("0.315"), D("0.99")]
    asc = p.percentile_rank(rows, org_id="B", order="asc")
    desc = p.percentile_rank(rows, org_id="A")
    assert (asc["rank"], asc["population"], asc["top_pct"]) == (1, 4, D("25"))
    assert (desc["rank"], desc["population"], desc["top_pct"]) == (3, 4, D("75"))
    with pytest.raises(p.PrimitiveError) as exc:
        p.percentile_rank(rows, org_id="E")
    assert exc.value.code == "INSUFFICIENT_DATA"
    for q in ("-0.001", "100.001"):
        with pytest.raises(p.PrimitiveError) as exc:
            p.percentile(rows, q)
        assert exc.value.code == "INVALID_PARAMS"


@pytest.mark.parametrize(("right_closed", "expected"), [
    (False, [[], ["A"], ["B", "C"]]), (True, [["A"], ["B"], ["C"]]),
])
def test_bucket_edges_and_explicit_institution_scope_reconcile(right_closed, expected):
    rows = [{"org_id": org, "value": value} for org, value in
            [("A", "0"), ("B", "1"), ("C", "1.1"), ("D", None), ("Z", "4")]]
    rows.append({"org_id": "E", "value": "-100", "usable": False})
    result = p.bucket(rows, bins=["0.0", 1], labels=["低", "中", "高"],
                      expected_orgs=list("ABCDEF"), right_closed=right_closed)
    assert [b["org_ids"] for b in result["buckets"]] == expected
    assert [b["label"] for b in result["buckets"]] == ["低", "中", "高"]
    missing = result["missing_bucket"]
    assert set(missing["org_ids"]) == set("DEF") and missing["count"] == 3
    assert sum(b["count"] for b in result["buckets"]) == result["valid_count"] == 3
    assert result["valid_count"] + missing["count"] == result["total_count"] == 6


def test_bucket_invalid_edges_labels_and_duplicate_observations_are_rejected():
    rows = [{"org_id": "A", "value": value} for value in [2, 3]]
    for options, code in [
        ({"bins": []}, "INVALID_PARAMS"), ({"bins": [2, "2.00"]}, "INVALID_PARAMS"),
        ({"bins": [3, 2]}, "INVALID_PARAMS"), ({"bins": [2], "labels": ["低"]}, "INVALID_PARAMS"),
        ({"bins": [2], "expected_orgs": ["A"]}, "AMBIGUOUS_ROW"),
    ]:
        with pytest.raises(p.PrimitiveError) as exc:
            p.bucket(rows, **options)
        assert exc.value.code == code


def test_population_dispersion_and_strict_outlier_cutoff():
    rows = [{"value": x} for x in ["2", "4", "4", "4", "5", "5", "7", "9", None]]
    assert p.dispersion(rows) == {"count": 8, "min": D(2), "max": D(9), "mean": D(5),
                                  "range": D(7), "median": D("4.5"), "std_dev": D(2)}
    result = p.find_outliers(rows, k=1)
    assert (result["lower"], result["upper"]) == (D(3), D(7))
    assert [r["value"] for r in result["outliers"]] == ["2", "9"]
    # 边界值 9 距均值恰好两倍标准差，不算离群。
    assert p.find_outliers(rows)["outliers"] == []
    for sample in [[], [{"value": None}], [{"value": 1}], [{"value": 1}, {"value": "1.00"}]]:
        out = p.find_outliers(sample)
        assert out["outliers"] == [] and out["reason"]
    with pytest.raises(p.PrimitiveError) as exc:
        p.find_outliers(rows, k="-0.1")
    assert exc.value.code == "INVALID_PARAMS"


def test_changes_distinguish_relative_growth_from_percentage_points():
    assert p.compare_text("1.65", "1.4", unit="%") == "增加0.25个百分点"
    assert p.compare_text(0, 2, unit="元") == "减少2.00元"
    assert p.compare_text(2, 2) == "持平"
    assert p.compare_text(1, 2, unit="％", mode="benchmark") == "低于基准1.00个百分点"
    assert p.compare_text(3, 2, mode="benchmark", digits=0) == "高于基准1"
    assert p.compare_text(2, 2, mode="benchmark") == "等于基准"
    assert p.delta_points("1.65", "1.4") == D(".25")
    assert p.delta_points("1.4", "1.65", unit="bp") == D("-25")
    assert p.growth("90", "100") == D("-10")
    assert p.growth("90", "100", as_pct=False) == D("-0.10")
    assert p.ratio(4, -8) == D("-50")
    for base in [0, "-0.01"]:
        with pytest.raises(p.PrimitiveError) as error:
            p.growth(1, base)
        assert error.value.code == "INVALID_GROWTH_BASELINE"
    with pytest.raises(p.PrimitiveError) as error:
        p.ratio(1, 0)
    assert error.value.code == "DIVISION_BY_ZERO"


@pytest.mark.parametrize("value,target,direction,gap,shortfall,met,completion", [
    (8, 10, "gte", -2, 2, False, 80), (10, 10, "gte", 0, 0, True, 100),
    (0, 0, "gte", 0, 0, True, None), (-2, -1, "gte", -1, 1, False, None),
    (5, 4, "lte", 1, 1, False, None), (3, 4, "lte", -1, 0, True, None),
])
def test_target_gap_depends_on_direction(value, target, direction, gap, shortfall, met, completion):
    assert p.gap_to_target(value, target, direction=direction) == {
        "gap": gap, "shortfall": shortfall, "met": met, "completion_pct": completion}


def test_reconciliation_checks_coverage_independently_of_amount_and_type():
    rows = [{"value": "100.01", "unit": "万元"}, {"value": None, "unit": "元"}]
    partial = p.validate_sum(rows, expected_total=100, expected_count=2)
    assert partial == {"passed": False, "kind_valid": True, "coverage_ok": False, "sum_matches": True,
                       "valid_count": 1, "expected_count": 2, "coverage_ratio": D("0.5"),
                       "expected_total": 100, "actual_sum": D("100.01"), "diff": D("0.01"),
                       "issues": partial["issues"]}
    assert partial["issues"]
    assert p.validate_sum(rows, expected_total=100, expected_count=1)["passed"]
    outside = p.validate_sum(rows, expected_total=100, expected_count=1, tolerance="0.0099")
    assert outside["coverage_ok"] and not outside["sum_matches"] and not outside["passed"]
    rate = p.validate_sum(rows, kind="rate", expected_total=100)
    assert not rate["kind_valid"] and not rate["passed"] and rate["actual_sum"] is None
    assert not p.validate_sum(rows, expected_total="100.01")["coverage_ok"]
    with pytest.raises(p.PrimitiveError) as error:
        p.avg(rows, kind="rate", allow_rate_arithmetic_mean="false")
    assert error.value.code == "INVALID_METHOD_FOR_RATE"
    for invalid in [[], rows + [{"value": 10, "unit": "元"}]]:
        result = p.validate_sum(invalid, expected_total=0)
        assert not result["passed"] and result["actual_sum"] is None and result["issues"]


@pytest.mark.parametrize("relation,payload,passed", [
    ("sum_equals", {"parts": ["0.10", "0.20"], "total": "0.31"}, True),
    ("sum_equals", {"parts": ["0.10", "0.20"], "total": "0.311"}, False),
    ("sum_equals", {"parts": [], "total": 0}, False),
    ("ratio_equals", {"numerator": 3, "denominator": 8, "rate": "37.51"}, True),
    ("ratio_equals", {"numerator": 3, "denominator": 8, "rate": "0.375"}, False),
    ("ratio_equals", {"numerator": 0, "denominator": 0, "rate": 0}, False),
    ("order", {"left": 10, "op": ">=", "right": 10}, True),
    ("order", {"left": 10, "op": ">", "right": 10}, False),
    ("bounds", {"min_val": 0, "max_val": 100, "value": 100}, True),
    ("bounds", {"min_val": 0, "max_val": 100, "value": "100.001"}, False),
    ("bounds", {"min_val": 0, "value": "0"}, True),
])
def test_financial_relations(relation, payload, passed):
    result = p.validate_relation(payload, relation=relation)
    assert result["passed"] is passed and result["relation"] == relation and result["message"]
    if relation == "ratio_equals" and payload["denominator"] == 0:
        assert result["diff"] is None

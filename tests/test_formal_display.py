"""公文的展示精度独立于归档、核验和证据引用。"""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(("value", "unit", "expected"), [
    ("133.7127", "%", "133.71%"), ("226.03305", "%", "226.03%"),
    ("-1.225", "%", "-1.23%"), ("0.0049", "%", "0.00%"),
    ("0.00495", "个百分点", "0.0050个百分点"), ("-0.0049", "百分点", "-0.0049百分点"),
    ("0.005", "百分点", "0.01百分点"), ("0", "个百分点", "0.00个百分点"),
    ("105915.395", "万元", "105,915.40万元"), ("-1234.125", "元", "-1,234.13元"),
    ("77", "名", "第77名"),
])
def test_formal_display_uses_original_precision(value, unit, expected):
    from backend.risk_letter import format_formal_display

    assert format_formal_display(value, unit, "第77名" if unit == "名" else "已舍入的展示") == expected


def test_render_formats_only_rate_tokens_and_table_measurements():
    from backend.risk_letter import _render, fetch_org_metrics_for_case

    row = dict(indicator_id="ratio", indicator_name="覆盖率", value="133.7127", value_display="133.7127%",
               quality_status="valid", org_id="甲", usable=True, evidence_id="observed",
               unit="%", rank=77, population=82, data_date="2026-09-30")
    case = SimpleNamespace(org_id="甲", evidence=[row], data_date=row["data_date"], analysis={})
    metric, = fetch_org_metrics_for_case(case, include_history=True)["metrics"]
    assert metric["value"] == "133.7127" and metric["value_display"] == "133.7127%"
    assert metric["formal_display"] == metric["history"][0]["formal_display"] == "133.71%"
    content = _render({"issue_date": "2026年10月1日"}, {
        "title": "风险提示函", "recipient": "甲行：", "opening": "覆盖率226.03305%。",
        "overview": "截至2026-09-30，第77 / 82名，编号R12.3456，指标133.7127%。",
        "risk_sections": [{"heading": "跟踪92.32035个百分点的变动", "body": "小幅变动-0.00495百分点。"}],
        "suggestions": [{"heading": "复核", "body": "请于15个工作日内复核。"}],
        "closing_requirement": "核实1.005个百分点变动。", "appendix_evidence_ids": ["observed"],
    }, {"observed": row})
    assert "覆盖率226.03%。" in content
    assert "截至2026-09-30，第77 / 82名，编号R12.3456，指标133.71%。" in content
    assert "跟踪92.32个百分点的变动。小幅变动-0.0050百分点。" in content
    assert "核实1.01个百分点变动。" in content and "请于15个工作日内复核。" in content
    assert "| 覆盖率 | 133.71% | 第 77 / 82 名 | 2026-09-30 |" in content


def test_verified_benchmark_keeps_unrounded_deviation_and_formal_display(monkeypatch):
    from backend import risk_scans
    from test_verification_layer import batch, DEFS, obs

    facts = [obs("A", "rate", "133.7127"), obs("B", "rate", "133.7119")]
    fetched = batch(facts)
    monkeypatch.setattr(risk_scans, "fetched_from_scan_archive", lambda db, run: fetched)
    monkeypatch.setattr(risk_scans, "INDICATORS", DEFS)
    from backend.metric_validator import validate_fetched_metrics

    run = SimpleNamespace(id="scan", selection={"risk_scan_result": {
        "validation_summary": validate_fetched_metrics(fetched, DEFS)}})
    case = SimpleNamespace(run_id=run.id, org_id="A", evidence=facts[:1])
    result = risk_scans.run_letter_verified_operator(None, run, case, "benchmark_deviation",
                                                     {"indicator_id": "rate", "baseline": "median"})
    assert result["status"] == "ok"
    focused, = result["rows"]
    assert (focused["value"], focused["formal_display"]) == ("133.7127", "133.71%")
    assert (focused["deviation"], focused["deviation_formal_display"]) == ("0.0004", "0.0004个百分点")
    assert (result["summary"]["baseline_value"], result["summary"]["baseline_formal_display"]) == ("133.7123", "133.71%")

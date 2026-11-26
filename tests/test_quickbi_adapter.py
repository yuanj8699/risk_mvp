"""以合成字典、机构和本地 HTTP/SSE 服务验证内网协议；归档使用隔离 SQLite，Pi 使用 demo。"""
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from quickbi_protocol_cases import TestQuickBiProtocol, json_reply, olap_form, protocol_endpoint
from test_api_workflow import good


# models 在首次导入时绑定归档库，先由共享夹具设置隔离库。
pytestmark = pytest.mark.usefixtures("api")

CURRENT = "2026-09-30"
PREVIOUS = "2026-08-31"
AMOUNT = "TEST_METRIC_001"
RATE = "npl_ratio"
CUBE = "sample-cube"


def test_dictionary_binds_business_names_and_excludes_unresolved_sources(tmp_path):
    from backend.risk_knowledge import load_quickbi_bindings
    expected = {"五级不良贷款率": RATE, "逾期贷款率": "overdue_ratio", "不良发生净生成率": "new_npl_ratio",
                "资本充足率": "TEST_METRIC_013", "净息差": "TEST_METRIC_095", "拨备覆盖率": "TEST_METRIC_097"}
    rows = {name: {"cubeId": CUBE, "status": "available"} for name in expected}
    # 相同旧来源名在业务表中涉及多项不同口径，没有额外绑定时保留缺源。
    rows.update({"1000万以上对公不良贷款余额": {"cubeId": CUBE},
                 "各项贷款": {"status": "无源"}, "总资产": {"kind": "composite", "cubeId": CUBE}})
    path = tmp_path / "指标字典.json"
    path.write_text(json.dumps({"metrics": rows}, ensure_ascii=False), encoding="utf-8")
    bindings = load_quickbi_bindings(path)
    assert {entry["indicatorName"]: ident for ident, entry in bindings.items()} == expected


@pytest.fixture
def quickbi(tmp_path, monkeypatch):
    directory = {"测试全省": {"代码": "999999", "层级": "01"}}
    for i in range(11):
        directory[f"测试地区{i}"] = {"代码": f"R{i:02}", "层级": "02", "上级": "测试全省"}
    for i in range(82):
        directory[f"测试行社{i}"] = {"代码": f"B{i:03}", "层级": "03", "上级": f"测试地区{i % 11}"}
    metrics = {
        "测试资产": {"cubeId": CUBE, "indicatorName": "资产总额（源表）", "kind": "amount",
                   "unit": "万元", "synonyms": ["总资产"], "status": "available"},
        "五级不良贷款率": {"cubeId": CUBE, "indicatorName": "不良比例（源表）", "kind": "rate",
                        "unit": "%", "measureCaption": "比例列", "status": "available"},
        "逾期贷款率": {"cubeId": CUBE, "indicatorName": "逾期贷款率", "status": "disabled"},
    }
    for env, name, doc in [("RISK_QUICKBI_ORG_FILE", "机构归属.json", directory),
                           ("RISK_QUICKBI_DICT_FILE", "指标字典.json", {"metrics": metrics})]:
        target = tmp_path / name
        target.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        monkeypatch.setenv(env, str(target))
    monkeypatch.setenv("RISK_DATA_FETCHER", "quickbi")
    state = SimpleNamespace(lookups=[], queries=[], amount_total="828.21", null_member=None)
    dimensions = [("机构名称", "name"), ("机构号", "code"), ("指标名称", "metric"), ("指标单位", "unit"),
                  ("数据日期(day)", "unusedDay"), ("数据日期(month)", "reportDay"), ("干扰字段", "ignore")]
    measures = [("指标值", "value"), ("全省排名", "rank")]
    def reply(path):
        method, _, query, headers, body = requests[-1]
        if method == "GET" and path == "/openapi/v2/dataset/infoNew":
            assert query == {"datasetId": [CUBE]} and headers.get("X-Gw-Signature")
            assert not headers.get("Cookie")
            state.lookups.append(CUBE)
            return json_reply({"success": True, "data": {
                "datasetName": "测试指标长表", "dimensionList": [{"caption": n, "uid": f} for n, f in dimensions],
                "measureList": [{"caption": n, "uid": f} for n, f in measures]}})
        if method == "GET" and path == "/api/v2/user/currentUserInfo":
            return json_reply({"success": True, "data": {}})
        if method != "POST" or path != "/api/v2/olap/query":
            return json_reply({"success": False}, 404)
        limited = headers["Cookie"] == "subject=branch-test"
        fields, conditions, paging = olap_form(requests[-1], origin, cube=CUBE, csrf="csrf",
            cookie={"subject": "branch-test"} if limited else {"example": "value"},
            ticket="limited-test-ticket" if limited else None)
        if [f["fid"] for f in fields] == ["metric"]:
            assert not conditions and paging["limit"] == 12 and paging["offset"] == 0
            return json_reply({"data": {"values": [{"row": [name]} for name in ("可用乙", "可用甲", "可用乙")]}})
        assert all(c["functionalOperator"] == "in" and len(c["args"]) == 1 for c in conditions)
        values = {c["field"]["fid"]: c["args"][0]["value"] for c in conditions}
        metric = values.pop("metric")
        assert metric in {"资产总额（源表）", "不良比例（源表）"}
        date_fid, day = next(iter(values.items()), (None, None))
        state.queries.append((metric, date_fid, day))
        if date_fid == "unusedDay":
            return json_reply({"success": True, "data": {"columns": []}})
        rows = []
        if date_fid is None:
            rows = [{"reportDay": date, "unusedDay": date, "value": "1"} for date in (CURRENT, PREVIOUS)]
        elif date_fid == "reportDay" and day in ("20260930", "20260831"):
            amount = metric == "资产总额（源表）"
            for name, org in directory.items():
                code = org["代码"]
                if limited and day == "20260930" and code.startswith("B") and int(code[1:]) >= 9:
                    continue
                value = "10.10" if amount else "2.15"
                if code == "999999":
                    value = state.amount_total if amount else "2.37"
                elif code.startswith("R"):
                    value = str(Decimal("10.1") * sum(x.get("上级") == name for x in directory.values())) if amount else "2.32"
                elif code == state.null_member and amount:
                    value = None
                rows.append({"name": name, "code": code, "reportDay": day, "value": value,
                             "unit": "万元" if amount else "%", "rank": 82 if code == "B000" else None})
        # OLAP 按区域依次返回维度和度量列，和请求列表的原始位置可以不同。
        fields = sorted(fields, key=lambda f: f["areaType"] == "column")
        columns = [{"fid": f["fid"], "values": [r[f["fid"]] for r in rows]} for f in fields]
        return json_reply({"success": True, "data": {"columns": columns}})

    with protocol_endpoint(reply) as (requests, origin):
        yield state


def fetch(fetcher, *indicators, as_of=CURRENT, history=()):
    return fetcher.fetch({"as_of": as_of, "snapshot_id": as_of, "history_dates": list(history)}, indicators)


def accounting(result):
    from backend.metric_validator import validate_fetched_metrics
    from backend.risk_knowledge import INDICATORS
    summary = validate_fetched_metrics(result, INDICATORS)
    return summary, {row["indicator_id"]: row for row in summary["accounting_details"]}


def test_olap_full_scope_and_reconciliation(quickbi):
    from backend.data_fetcher import get_data_fetcher
    fetcher = get_data_fetcher()
    layout = fetcher.layout(CUBE)
    assert layout["table_mode"] == "long"
    assert layout["captions"]["干扰字段"] == {"fid": "ignore", "kind": "dimension"}
    assert layout["captions"]["指标值"] == {"fid": "value", "kind": "measure"}
    assert layout["datasetName"] == "测试指标长表"
    assert layout["date_candidates"] == ["unusedDay", "reportDay"]
    assert layout["missing"] == ["全系统平均值"]
    assert "ignore" not in layout["fields"].values()
    data = fetch(fetcher, AMOUNT, RATE, "overdue_ratio", history=[PREVIOUS])
    assert data["missing_source"] == ["overdue_ratio"] and not data["missing"]
    assert len(data["organizations"]) == 82 and len(data["latest"]) == 164
    assert len({r["evidence_id"] for r in data["history"]}) == 328
    assert {r["data_date"] for r in data["history"]} == {CURRENT, PREVIOUS}
    assert data["visibility"] == {"visible_org_count": 82, "expected_org_count": 82, "visible_note": None, "clipped": False}
    assert quickbi.lookups == [CUBE]
    assert sum(fid == "unusedDay" for _, fid, _ in quickbi.queries) == 1
    totals = data["source_totals"][AMOUNT]
    assert totals["province"]["org_id"] == "999999" and len(totals["regions"]) == 11
    summary, details = accounting(data)
    amount, rate = details[AMOUNT], details[RATE]
    assert summary["covered_org_count"] == 82 and summary["unit_mismatch_count"] == 0
    assert Decimal(amount["province_total"]) == Decimal("828.2")
    assert amount["source_province_total"] == "828.21"
    assert amount["member_sum_matches_source_total"] is True and amount["diff_from_source_total"] == "-0.01"
    assert rate["method"] == "source_province_total" and rate["province_total"] == "2.37"
    assert len(rate["source_region_totals"]) == 11
    first_ids = {(r["org_id"], r["indicator_id"]): r["evidence_id"] for r in data["latest"]}
    quickbi.amount_total = "828.22"
    repeated = fetch(fetcher, AMOUNT)
    assert all(r["evidence_id"] == first_ids[r["org_id"], AMOUNT] for r in repeated["latest"])
    amount = accounting(repeated)[1][AMOUNT]
    assert amount["member_sum_matches_source_total"] is False and amount["diff_from_source_total"] == "-0.02"


def test_live_indicators_queries_long_table(quickbi):
    from backend.data_fetcher import get_data_fetcher
    assert get_data_fetcher().live_indicators(CUBE, limit=12) == sorted(["可用甲", "可用乙"])


def test_wide_table_resolves_measures_from_live_layout(quickbi):
    from backend.data_fetcher import get_data_fetcher
    dimensions = {"机构名称": "name", "机构号": "code", "数据日期": "d", "指标单位": "other-unit"}
    measures = {"总资产": "balance-uid", "比例列": "ratio-uid", "全省排名": "other-rank"}
    def reply(path):
        if path.endswith("/infoNew"):
            return json_reply({"data": {"datasetName": "测试宽表",
                "dimensionList": [{"caption": c, "uid": f} for c, f in dimensions.items()],
                "measureList": [{"caption": c, "uid": f} for c, f in measures.items()]}})
        fields, conditions, _ = olap_form(requests[-1], origin, cube=CUBE, cookie={"example": "value"}, csrf="csrf")
        assert [f["fid"] for f in fields[:3]] == ["name", "code", "d"]
        assert len(fields) == 4 and fields[-1]["areaType"] == "column" and fields[-1]["aggregate"] == "sum"
        assert len(conditions) == 1 and conditions[0]["field"]["fid"] == "d"
        assert conditions[0]["functionalOperator"] == "in" and conditions[0]["args"] == [
            {"valueType": "string", "value": "20260930"}]
        value = {"balance-uid": "108.34", "ratio-uid": "2.15"}[fields[-1]["fid"]]
        return json_reply({"data": {"values": [{"row": ["测试行社0", "B000", "20260930", value]}]}})
    with protocol_endpoint(reply) as (requests, origin):
        fetcher = get_data_fetcher()
        assert fetcher.layout(CUBE)["table_mode"] == "wide"
        assert sorted(fetcher.live_indicators(CUBE)) == sorted(measures)
        assert len(requests) == 1
        result = fetch(fetcher, AMOUNT, RATE)
        assert {(row["indicator_id"], row["org_id"], row["value"], row["unit"]) for row in result["latest"]} == {
            (AMOUNT, "B000", "108.34", "万元"), (RATE, "B000", "2.15", "%")}


def test_nested_row_results_and_field_order():
    from backend.quickbi_adapter import _rows
    from backend.quickbi_client import QuickBiError
    fields = [("amount", {"fid": "a", "areaType": "column"}), ("name", {"fid": "n", "areaType": "row"})]
    expected = [{"name": "甲", "amount": "3.20"}]
    assert _rows({"data": {"values": [{"row": ["甲", "3.20"]}]}}, fields) == expected
    assert _rows({"data": [{"row": ["甲", "3.20"]}]}, fields) == expected
    assert _rows({"rows": [["甲", "3.20"]]}, fields) == expected
    for malformed in ({"values": [{"row": ["甲"]}]}, {"rows": [["甲", "3.20"]], "columns": []},
                      {"columns": [{"fid": "a", "values": ["3.20"]}, {"fid": "n", "values": ["甲"]}]}):
        with pytest.raises(QuickBiError):
            _rows(malformed, fields)


def test_olap_missing_date_and_missing_values(quickbi):
    from backend.data_fetcher import get_data_fetcher
    fetcher = get_data_fetcher()
    absent = fetch(fetcher, AMOUNT, as_of="2026-09-29")
    assert absent["latest"] == [] and absent["history"] == []
    assert any("2026-09-29" in msg and CURRENT in msg and PREVIOUS in msg for msg in absent["date_notes"])
    quickbi.null_member = "B003"
    data = fetch(fetcher, AMOUNT)
    row = next(r for r in data["latest"] if r["org_id"] == "B003")
    assert row["value"] is None and not row["usable"]
    summary, details = accounting(data)
    assert summary["missing_value_count"] == 1
    assert details[AMOUNT]["member_sum_matches_source_total"] is None


def test_ticket_visibility_does_not_expand_from_history(quickbi, monkeypatch):
    from backend.data_fetcher import get_data_fetcher
    monkeypatch.setenv("IDENTITY_PROVIDER", "ticket")
    monkeypatch.setenv("QB_COOKIE", "subject=branch-test")
    monkeypatch.setenv("QB_ACCESS_TICKET", "limited-test-ticket")
    data = fetch(get_data_fetcher(), AMOUNT, RATE, history=[PREVIOUS])
    visible = {f"B{i:03}" for i in range(9)}
    assert {r["org_id"] for r in data["organizations"]} == visible
    assert {r["org_id"] for r in data["history"]} == visible
    summary, details = accounting(data)
    assert summary["visibility"]["clipped"] and summary["visibility"]["visible_org_count"] == 9
    assert summary["visibility"]["visible_note"] in summary["reminders"]
    assert details[RATE]["province_total"] is None and details[RATE]["source_province_total"] == "2.37"
    assert details[AMOUNT]["member_sum_matches_source_total"] is None and not details[AMOUNT]["complete"]
    assert details[AMOUNT]["province_total"] is None and details[AMOUNT]["partial_total"] == "90.9"


def test_http_risk_scan_honors_current_identity(quickbi, api, tmp_path, monkeypatch):
    from backend import app, models, risk_scans
    from backend.services import ServiceError
    engine = create_engine(f"sqlite:///{(tmp_path / 'archive.db').as_posix()}", connect_args={"check_same_thread": False})
    @event.listens_for(engine, "connect")
    def foreign_keys(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
    try:
        models.Base.metadata.create_all(engine)
        sessions = sessionmaker(engine, expire_on_commit=False)
        for module in (app, risk_scans):
            monkeypatch.setattr(module, "SessionLocal", sessions)
        monkeypatch.setenv("RISK_PI_MODE", "demo")
        boot = good(api.get("/api/bootstrap"))
        assert boot["meta"]["data_fetcher"] == "quickbi"
        assert next(d for d in boot["risk_dimensions"] if d["id"] == "credit_risk")["executable"]
        body = {"snapshot_id": CURRENT, "dimension_ids": ["credit_risk"], "auto_generate_reports": False}
        full = good(api.post("/api/risk-scans", json=body))
        assert full["status"] == "completed" and full["summary"]["org_count"] == 82
        assert full["runtime"]["mode"] == "demo"
        assert full["executive_summary"].startswith("全辖筛查定位")
        monkeypatch.setenv("QB_COOKIE", "subject=branch-test")
        monkeypatch.setenv("IDENTITY_PROVIDER", "ticket")
        monkeypatch.setenv("QB_ACCESS_TICKET", "limited-test-ticket")
        clipped = good(api.post("/api/risk-scans", json=body))
        assert clipped["status"] == "completed" and clipped["id"] != full["id"]
        assert clipped["summary"]["org_count"] == 9 and clipped["validation_summary"]["visibility"]["clipped"]
        assert {r["org_id"] for r in clipped["society_results"]} == {f"B{i:03}" for i in range(9)}
        assert any(r["issues"] for r in clipped["society_results"])
        assert clipped["executive_summary"].startswith("可见行社筛查定位")
        archived = good(api.get(f"/api/risk-scans/{clipped['id']}"))
        assert archived["validation_summary"] == clipped["validation_summary"]
        assert all(secret not in json.dumps(archived) for secret in
                   ("limited-test-ticket", "subject=branch-test", "sample-secret", "sample-id"))
        # 断开源表后，撰函核验仍用原扫描的全辖合计与权限范围。
        query_count = len(quickbi.queries)
        with sessions() as db:
            for scan, expected_count in ((full, 82), (clipped, 9)):
                run = db.get(models.AnalysisRun, scan["id"])
                case = db.get(models.RiskCase, scan["society_results"][0]["case_id"])
                package = risk_scans.fetched_from_scan_archive(db, run)
                assert package["source_totals"][RATE]["province"]["value"] == "2.37"
                assert len(package["organizations"]) == expected_count
                result = risk_scans.run_letter_verified_operator(db, run, case, "benchmark_deviation",
                                                                 {"indicator_id": RATE})
                assert result["coverage"]["total_orgs"] == expected_count
                if expected_count == 82:
                    assert result["summary"]["baseline_method"] == "source_province_total"
                    assert result["summary"]["baseline_value"] == "2.37"
                    assert "deviation_exact" in result["checks_passed"]
                    assert [r["org_id"] for r in result["rows"]] == [case.org_id]
                    assert result["evidence_ids"] == [result["rows"][0]["evidence_id"]]
                else:
                    assert result["status"] == "blocked" and result["error"]["code"] == "CLIPPED_SCOPE"
                with pytest.raises(ServiceError, match="org_ids"):
                    risk_scans.run_letter_verified_operator(db, run, case, "consecutive_trend", {
                        "indicator_id": RATE, "n_periods": 2, "direction": "up", "org_ids": ["B001"]})
        assert len(quickbi.queries) == query_count
    finally:
        engine.dispose()

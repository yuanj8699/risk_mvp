"""Black-box regression checks for the local, synthetic-data prototype.

Run from the project root: python -m pytest -q
The suite always uses its own temporary SQLite database. It never contacts a
delivery channel and does not change the application's normal demo database.
"""

from contextlib import contextmanager
from copy import deepcopy
import asyncio
import csv
import importlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import sys
import tempfile
from threading import Event

import pytest
import httpx


COLUMNS = [
    "org_code", "indicator_id", "data_date", "value", "unit", "rank",
    "population", "rank_scope", "value_encoding",
]


@pytest.fixture(scope="session")
def api():
    with tempfile.TemporaryDirectory(prefix="risk-agent-tests-") as directory:
        previous = os.environ.get("RISK_DATABASE_URL")
        previous_mode = os.environ.get("RISK_PI_MODE")
        previous_source = os.environ.pop("RISK_SOURCE_DATABASE_URL", None)
        os.environ["RISK_PI_MODE"] = "demo"
        os.environ["RISK_DATABASE_URL"] = "sqlite:///" + str(Path(directory) / "test.db")
        os.environ["RISK_DEMO"] = "1"
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        module = importlib.import_module("backend.app")
        # Exercise the complete HTTP/ASGI route stack in one thread. The short
        # timer also keeps thread completion callbacks responsive in constrained
        # container runtimes; no application behavior is mocked or replaced.
        loop = asyncio.new_event_loop()

        async def heartbeat():
            while True:
                await asyncio.sleep(0.01)

        ticker = loop.create_task(heartbeat())
        lifecycle = module.app.router.lifespan_context(module.app)
        loop.run_until_complete(lifecycle.__aenter__())
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://testserver")

        class SynchronousASGIClient:
            def request(self, method, path, **kwargs):
                return loop.run_until_complete(client.request(method, path, **kwargs))

            def get(self, path, **kwargs):
                return self.request("GET", path, **kwargs)

            def post(self, path, **kwargs):
                return self.request("POST", path, **kwargs)

            def patch(self, path, **kwargs):
                return self.request("PATCH", path, **kwargs)

        try:
            yield SynchronousASGIClient()
        finally:
            loop.run_until_complete(client.aclose())
            loop.run_until_complete(lifecycle.__aexit__(None, None, None))
            ticker.cancel()
            loop.run_until_complete(asyncio.gather(ticker, return_exceptions=True))
            loop.close()
            module.engine.dispose()
        if previous is None:
            os.environ.pop("RISK_DATABASE_URL", None)
        else:
            os.environ["RISK_DATABASE_URL"] = previous
        if previous_mode is None:
            os.environ.pop("RISK_PI_MODE", None)
        else:
            os.environ["RISK_PI_MODE"] = previous_mode
        if previous_source is not None:
            os.environ["RISK_SOURCE_DATABASE_URL"] = previous_source


def good(response):
    assert response.status_code < 300, (response.status_code, response.text)
    return response.json()


def societies(api):
    return sorted(
        (org for org in good(api.get("/api/organizations")) if org["level"] == 3),
        key=lambda org: org["code"],
    )


def rule_for(api, kind, indicator_id=None):
    return next(
        rule for rule in good(api.get("/api/rules"))
        if rule["kind"] == kind
        and (indicator_id is None or rule["indicator_id"] == indicator_id)
    )


def editable_params(rule):
    fields = {"rank": {"cutoff"}, "ratio": {"threshold", "op"}, "exists": set()}[rule["kind"]]
    return {key: value for key, value in rule["params"].items() if key in fields}


@contextmanager
def only_rule(api, selected, params=None):
    originals = deepcopy(good(api.get("/api/rules")))
    try:
        for rule in originals:
            patch = {"enabled": rule["id"] == selected["id"]}
            if rule["id"] == selected["id"] and params is not None:
                patch["params"] = {**editable_params(rule), **params}
            good(api.patch(f"/api/rules/{rule['id']}", json=patch))
        yield
    finally:
        for rule in originals:
            good(api.patch(
                f"/api/rules/{rule['id']}",
                json={"enabled": rule["enabled"], "params": editable_params(rule)},
            ))


def row(org, indicator="npl_ratio", value="1.00", rank="", **overrides):
    currency = indicator in {"funds_npl_balance", "funds_npl_change"}
    result = {
        "org_code": org["code"], "indicator_id": indicator,
        "data_date": "2026-10-29", "value": value,
        "unit": "元" if currency else "%", "rank": rank,
        "population": "82" if rank != "" else "",
        "rank_scope": "province_societies" if rank != "" else "",
        "value_encoding": "raw" if currency else "percent_number",
    }
    result.update(overrides)
    return result


def csv_bytes(rows):
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return text.getvalue().encode("utf-8-sig")


def import_csv(api, rows, name="qa-synthetic.csv"):
    return api.post("/api/import", files={"file": (name, csv_bytes(rows), "text/csv")})


def run(api, snapshot_id, topic="信用风险"):
    return good(api.post("/api/runs", json={"snapshot_id": snapshot_id, "risk_topic": topic}))


def cases_for(api, run_id):
    return good(api.get("/api/cases", params={"run_id": run_id}))


def test_hierarchy_and_test_only_delivery(api):
    boot = good(api.get("/api/bootstrap"))
    assert boot["meta"]["agent_mode"] == "pi-demo"
    orgs = boot["organizations"]
    assert boot["meta"]["demo"] is True
    assert len([org for org in orgs if org["level"] == 2]) == 11
    assert len([org for org in orgs if org["level"] == 3]) == 82
    ids = {org["id"] for org in orgs}
    assert all(org["parent_id"] in ids for org in orgs if org["level"] > 1)
    assert {4, 5}.issubset({org["level"] for org in orgs})
    paths = good(api.get("/openapi.json"))["paths"]
    assert not any(path.endswith("/send") or "real-send" in path for path in paths)


def test_rank_includes_72_and_excludes_levels_4_5_and_is_idempotent(api):
    orgs = societies(api)
    rule = rule_for(api, "rank", "npl_ratio")
    rows = [row(org, value=f"{i / 100:.2f}", rank=str(i)) for i, org in enumerate(orgs, 1)]
    rows.extend(
        row(org, value="9.99", rank="82")
        for org in good(api.get("/api/organizations")) if org["level"] in {4, 5}
    )
    snapshot = good(import_csv(api, rows))
    with only_rule(api, rule, {"cutoff": 72}):
        first = run(api, snapshot["snapshot_id"])
        again = run(api, snapshot["snapshot_id"])
        assert again["id"] == first["id"]
        cases = cases_for(api, first["id"])
        assert len(cases) == 11
        assert {case["org_id"] for case in cases} == {org["id"] for org in orgs[71:]}
        observed_ranks = []
        for case in cases:
            detail = good(api.get(f"/api/cases/{case['id']}"))
            observed_ranks.extend(item["rank"] for item in detail["evidence"])
        assert sorted(observed_ranks) == list(range(72, 83))
        updated = good(api.patch(f"/api/rules/{rule['id']}", json={"params": {"cutoff": 73}}))
        changed = run(api, snapshot["snapshot_id"])
        assert changed["id"] != first["id"]
        assert len(cases_for(api, changed["id"])) == 10
        assert updated["version"] > rule["version"]


def test_ties_are_not_forcibly_cut_to_eleven(api):
    orgs = societies(api)
    rows = [row(org, rank=str(i)) for i, org in enumerate(orgs, 1)]
    rows[70]["rank"] = "72"  # two institutions tie at 72; supplied ranks remain authoritative
    snapshot = good(import_csv(api, rows))
    with only_rule(api, rule_for(api, "rank", "npl_ratio"), {"cutoff": 72}):
        result = run(api, snapshot["snapshot_id"])
        assert len(cases_for(api, result["id"])) == 12


def test_new_topic_updates_selection_without_removing_mandatory_checks(api):
    rows = [row(org, rank=str(i)) for i, org in enumerate(societies(api), 1)]
    snapshot = good(import_csv(api, rows))
    with only_rule(api, rule_for(api, "rank", "npl_ratio"), {"cutoff": 72}):
        credit = run(api, snapshot["snapshot_id"], "信用风险")
        deposits = run(api, snapshot["snapshot_id"], "存款扩张")
        assert credit["id"] != deposits["id"]
        assert deposits["selection"]["topic"] == "存款扩张"
        assert "pure_deposit_growth" in deposits["selection"]["indicator_ids"]
        assert "npl_ratio" in deposits["selection"]["indicator_ids"]
        assert deposits["summary"]["evaluated"] == credit["summary"]["evaluated"] == 82
        assert deposits["summary"]["triggered"] == credit["summary"]["triggered"] == 11


def test_rank_population_mismatch_cannot_be_reported_normal(api):
    rows = [row(org, rank=str(i), population="83") for i, org in enumerate(societies(api), 1)]
    snapshot = good(import_csv(api, rows))
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        result = run(api, snapshot["snapshot_id"])
        assert result["summary"]["insufficient"] == 82
        assert result["summary"]["normal"] == 0
        assert cases_for(api, result["id"]) == []


def test_nonzero_presence_distinguishes_missing_and_zero(api):
    orgs = societies(api)
    rows = [row(org, "funds_npl_balance", "0") for org in orgs]
    rows[0]["value"] = ""
    rows[2]["value"] = "120000"
    rows.append(row(orgs[2], "funds_npl_change", "-30000"))
    snapshot = good(import_csv(api, rows))
    with only_rule(api, rule_for(api, "exists")):
        result = run(api, snapshot["snapshot_id"])
        assert result["summary"]["insufficient"] == 1
        assert result["summary"]["triggered"] == 1
        assert result["summary"]["normal"] == 80
        cases = cases_for(api, result["id"])
        assert [case["org_id"] for case in cases] == [orgs[2]["id"]]
        detail = good(api.get(f"/api/cases/{cases[0]['id']}"))
        evidence = detail["evidence"]
        assert len(evidence) == 1
        assert evidence[0]["indicator_id"] == "funds_npl_balance"
        narrative = "\n".join(detail["analysis"]["facts"])
        assert "资金业务不良余额比年初增量" in narrative
        assert "减少" in narrative or "下降" in narrative


def test_unconfigured_ratio_is_explicit_then_uses_percentage_units(api):
    orgs = societies(api)
    rows = [row(org, value="0.10") for org in orgs]
    for item, value in zip(rows, ["1.49", "1.50", "1.51"]):
        item["value"] = value
    snapshot = good(import_csv(api, rows))
    rule = rule_for(api, "ratio")
    with only_rule(api, rule, {"threshold": None, "op": ">="}):
        pending = run(api, snapshot["snapshot_id"])
        assert pending["summary"]["unconfigured"] > 0
        assert pending["summary"]["normal"] == 0
        assert cases_for(api, pending["id"]) == []
        configured = good(api.patch(
            f"/api/rules/{rule['id']}",
            json={"params": {"threshold": "1.50", "op": ">="}},
        ))
        assert configured["needs_config"] is False
        evaluated = run(api, snapshot["snapshot_id"])
        assert {case["org_id"] for case in cases_for(api, evaluated["id"])} == {orgs[1]["id"], orgs[2]["id"]}


def test_large_decimal_survives_import_storage_and_evidence(api):
    expected = "123456789012345.12345678"
    org = societies(api)[0]
    snapshot = good(import_csv(api, [row(org, "funds_npl_balance", expected)]))
    with only_rule(api, rule_for(api, "exists")):
        result = run(api, snapshot["snapshot_id"])
    cases = cases_for(api, result["id"])
    assert len(cases) == 1
    detail = good(api.get(f"/api/cases/{cases[0]['id']}"))
    assert detail["evidence"][0]["value"] == expected


@pytest.mark.parametrize("change", [
    {"org_code": "UNKNOWN-ORG"},
    {"indicator_id": "UNKNOWN-METRIC"},
    {"data_date": "2026-02-30"},
    {"value": "NaN"},
    {"value": "Infinity"},
    {"unit": "元"},
    {"value_encoding": "raw"},
])
def test_invalid_import_is_atomic(api, change):
    orgs = societies(api)
    before = len(good(api.get("/api/bootstrap"))["snapshots"])
    rows = [row(orgs[0]), row(orgs[1], **change)]
    response = import_csv(api, rows)
    assert 400 <= response.status_code < 500, response.text
    assert len(good(api.get("/api/bootstrap"))["snapshots"]) == before


def test_duplicate_fact_rejected_and_xlsx_supported(api):
    org = societies(api)[0]
    duplicate = row(org, value="1.65")
    response = import_csv(api, [duplicate, duplicate])
    assert 400 <= response.status_code < 500, response.text

    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(COLUMNS)
    sheet.append([duplicate[column] for column in COLUMNS])
    stream = io.BytesIO()
    workbook.save(stream)
    imported = good(api.post(
        "/api/import",
        files={"file": ("qa-synthetic.xlsx", stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
    ))
    assert imported["row_count"] == 1


def test_invalid_rule_parameters_do_not_change_live_configuration(api):
    rank_rule = rule_for(api, "rank", "npl_ratio")
    ratio_rule = rule_for(api, "ratio")
    invalid_parameters = [
        (rank_rule, {"cutoff": 0}),
        (rank_rule, {"cutoff": 83}),
        (rank_rule, {"cutoff": 72.5}),
        (ratio_rule, {"threshold": "NaN"}),
        (ratio_rule, {"threshold": "-0.1"}),
        (ratio_rule, {"threshold": "101"}),
        (ratio_rule, {"threshold": "1.50", "op": "arbitrary_expression"}),
    ]
    for original, params in invalid_parameters:
        response = api.patch(
            f"/api/rules/{original['id']}",
            json={"params": {**editable_params(original), **params}},
        )
        assert 400 <= response.status_code < 500, response.text
        actual = next(rule for rule in good(api.get("/api/rules")) if rule["id"] == original["id"])
        assert actual["params"] == original["params"]
        assert actual["version"] == original["version"]


def test_letter_revision_approval_and_simulated_delivery(api):
    orgs = societies(api)
    rows = [row(org, "funds_npl_balance", "100" if i == 0 else "0") for i, org in enumerate(orgs)]
    snapshot = good(import_csv(api, rows))
    with only_rule(api, rule_for(api, "exists")):
        result = run(api, snapshot["snapshot_id"])
    case = cases_for(api, result["id"])[0]
    letter = good(api.post(f"/api/cases/{case['id']}/draft"))
    repeated = good(api.post(f"/api/cases/{case['id']}/draft"))
    assert repeated["id"] == letter["id"]
    base = f"/api/letters/{letter['id']}"
    revision = letter["revision"]
    assert api.post(base + "/simulate-send", json={"expected_revision": revision}).status_code in {400, 409}
    approved = good(api.post(base + "/approve", json={"expected_revision": revision}))
    assert approved["status"] == "approved"
    edited = good(api.patch(base, json={"content": letter["content"] + "\n请核对上述测试数据。", "expected_revision": revision}))
    assert edited["revision"] > revision
    assert edited["status"] == "draft"
    assert api.post(base + "/approve", json={"expected_revision": revision}).status_code == 409
    assert api.post(base + "/simulate-send", json={"expected_revision": edited["revision"]}).status_code in {400, 409}
    good(api.post(base + "/approve", json={"expected_revision": edited["revision"]}))
    delivered = good(api.post(base + "/simulate-send", json={"expected_revision": edited["revision"]}))
    repeated = good(api.post(base + "/simulate-send", json={"expected_revision": edited["revision"]}))
    assert delivered["status"] == "simulated_sent"
    assert delivered["receipt"]["channel"] == "simulation"
    assert repeated["receipt"]["id"] == delivered["receipt"]["id"]
    assert api.patch(base, json={"content": "不能修改已模拟下发版本", "expected_revision": edited["revision"]}).status_code in {400, 409}
    download = api.get(base + "/download")
    assert download.status_code == 200
    assert "请核对上述测试数据。" in download.text



def test_pi_runtime_reports_demo_mode_and_preserves_evidence(api):
    org = societies(api)[0]
    snapshot = good(import_csv(api, [row(org, rank="82")]))
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        result = run(api, snapshot["snapshot_id"])
        assert result["selection"]["mode"] == "pi-demo"
        assert result["selection"]["runtime"]["framework"] == "pi-agent-core"
        case = cases_for(api, result["id"])[0]
        detail = good(api.get(f"/api/cases/{case['id']}"))
        assert detail["analysis"]["mode"] == "pi-demo"
        assert detail["analysis"]["trace"]
        assert detail["evidence"][0]["value_display"] in "\n".join(detail["analysis"]["facts"])
        stored = next(item for item in good(api.get("/api/runs")) if item["id"] == result["id"])
        assert stored["summary"] == result["summary"]
        assert stored["summary"]["case_count"] == 1
        assert stored["summary"]["triggered"] == 1


def test_unconfigured_pi_model_retains_failed_task_and_triggered_evidence(api, monkeypatch):
    org = societies(api)[0]
    snapshot = good(import_csv(api, [row(org, rank="82")]))
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        monkeypatch.setenv("RISK_PI_MODE", "model")
        for name in ("RISK_PI_PROVIDER", "RISK_PI_MODEL", "RISK_PI_API_KEY"):
            monkeypatch.delenv(name, raising=False)
        payload = {"snapshot_id": snapshot["snapshot_id"], "risk_topic": "配置错误验证", "org_scope": [org["id"]]}
        response = api.post("/api/runs", json=payload)
        assert response.status_code == 502
        failure = response.json()["detail"]
        assert "Pi Agent" in failure["message"]
        saved = next(item for item in good(api.get("/api/runs")) if item["id"] == failure["run_id"])
        assert saved["status"] == "failed"
        assert saved["summary"]["triggered"] == 1
        cases = cases_for(api, saved["id"])
        assert len(cases) == 1 and cases[0]["org_id"] == org["id"]
        detail = good(api.get(f"/api/cases/{cases[0]['id']}"))
        assert detail["evidence"][0]["rank"] == 82
        assert api.post(f"/api/cases/{cases[0]['id']}/draft").status_code == 409
        repeated = good(api.post("/api/runs", json=payload))
        assert repeated["id"] == saved["id"] and repeated["status"] == "failed"


def test_agent_failure_retains_evidence_and_requires_explicit_retry(api, monkeypatch):
    from backend.analysis import AnalysisError, provider

    org = societies(api)[0]
    snapshot = good(import_csv(api, [row(org, rank="82")]))
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        payload = {"snapshot_id": snapshot["snapshot_id"], "risk_topic": "信用风险", "org_scope": [org["id"]]}
        calls = []

        def unavailable(cases, context):
            calls.append(context["run_id"])
            raise AnalysisError("测试模拟：Pi 分析服务不可用")

        with monkeypatch.context() as patch:
            patch.setattr(provider, "explain_batch", unavailable)
            response = api.post("/api/runs", json=payload)
            assert response.status_code == 502
            run_id = response.json()["detail"]["run_id"]
            retained = cases_for(api, run_id)
            assert len(retained) == 1
            evidence = good(api.get(f"/api/cases/{retained[0]['id']}"))["evidence"]
            assert evidence[0]["rank"] == 82
            assert api.post(f"/api/cases/{retained[0]['id']}/draft").status_code == 409
            repeated = good(api.post("/api/runs", json=payload))
            assert repeated["id"] == run_id and repeated["status"] == "failed"
            assert calls == [run_id]

        retried = good(api.post("/api/runs", json={**payload, "retry": True}))
        assert retried["id"] == run_id and retried["status"] == "completed"
        assert retried["summary"]["case_count"] == 1
        updated = cases_for(api, run_id)
        assert [case["id"] for case in updated] == [case["id"] for case in retained]
        assert good(api.get(f"/api/cases/{updated[0]['id']}"))["evidence"] == evidence
        assert good(api.post(f"/api/cases/{updated[0]['id']}/draft"))["status"] == "draft"


def test_stale_supporting_value_is_explicit_history_not_current_change(api):
    org = societies(api)[0]
    snapshot = good(import_csv(api, [row(org, rank="82"),
                                   row(org, "funds_npl_change", "-30000", data_date="2025-01-01")]))
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        result = run(api, snapshot["snapshot_id"])
        case = cases_for(api, result["id"])[0]
        detail = good(api.get(f"/api/cases/{case['id']}"))
        analysis = detail["analysis"]
        historical = [item for item in analysis["queried_evidence"]
                      if item["indicator_id"] == "funds_npl_change"]
        assert historical and all(item["data_date"] == "2025-01-01" and item["current_issue"] for item in historical)
        facts = [text for text in analysis["facts"] if "资金业务不良余额比年初增量" in text]
        assert facts and all("2025-01-01" in text and "历史" in text and "不代表本期变化" in text for text in facts)
        assert "较年初减少" not in "\n".join(facts)


def test_pi_queries_scoped_evidence_through_mcp_and_renders_it(api, monkeypatch):
    from backend.analysis import provider

    captured = {}
    original = provider.explain_batch

    def observe(cases, context):
        captured["cases"] = deepcopy(cases)
        captured["context"] = deepcopy(context)
        return original(cases, context)

    monkeypatch.setattr(provider, "explain_batch", observe)
    topic = "核查不良贷款率与逾期率，并比较同口径历史变化"
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        result = good(api.post("/api/runs", json={
            "snapshot_id": "demo-2026-10-28", "risk_topic": topic, "org_scope": ["LS082"],
        }))
        context = captured["context"]
        assert context["run_id"] == result["id"]
        assert context["topic"] == topic
        assert context["snapshot_id"] == "demo-2026-10-28"
        assert context["as_of"] == "2026-10-28"
        assert context["org_scope"] == ["LS082"]
        assert context["source"] == "synthetic"
        assert {"demo-2026-08-31", "demo-2026-09-30"} <= set(context["history_snapshot_ids"])
        assert result["selection"]["task_context"] == context
        assert len(captured["cases"]) == 1
        assert not captured["cases"][0].get("supporting")
        cases = cases_for(api, result["id"])
        assert len(cases) == 1 and cases[0]["org_id"] == "LS082"
        detail = good(api.get(f"/api/cases/{cases[0]['id']}"))
        analysis = detail["analysis"]
        assert any(event.get("tool") == "query_metrics" and event.get("transport") == "mcp"
                   for event in analysis["trace"])
        queried = analysis["queried_evidence"]
        fields = {"evidence_id", "org_id", "indicator_id", "indicator_name", "value", "unit",
                  "data_date", "snapshot_id", "source_record_id", "quality_status"}
        assert queried and all(fields <= item.keys() for item in queried)
        assert all(item["org_id"] == "LS082" and item["data_date"] <= context["as_of"] for item in queried)
        assert all(item["snapshot_id"] in context["history_snapshot_ids"] for item in queried)
        assert all(isinstance(item["value"], str) for item in queried if item["value"] is not None)
        overdue = [item for item in queried if item["indicator_id"] == "overdue_ratio"]
        assert overdue
        assert any(item["indicator_id"] == "npl_ratio" and item["data_date"] < context["as_of"] for item in queried)
        references = analysis["evidence_references"]
        cited = {ref for entries in references.values() for proposal in entries for ref in proposal["evidence_ids"]}
        assert cited & {item["evidence_id"] for item in overdue}
        known = {item["evidence_id"] for item in queried} | {item.get("evidence_id", item.get("id")) for item in detail["evidence"]}
        assert cited <= known
        letter = good(api.post(f"/api/cases/{cases[0]['id']}/draft"))
        assert "逾期贷款率" in letter["content"] and "4.04%" in letter["content"]
        assert "2026-10-28" in letter["content"]


@pytest.mark.parametrize("retry", [False, True])
def test_concurrent_run_has_one_owner_and_model_wait_uses_short_transactions(api, monkeypatch, retry):
    from backend.analysis import AnalysisError, provider
    from backend.app import app

    org = societies(api)[0]
    snapshot = good(import_csv(api, [row(org, rank="75"), row(org, "overdue_ratio", "2.75")]))
    selected = rule_for(api, "rank", "npl_ratio")
    entered, release = Event(), Event()
    original = provider.explain_batch
    captured = []

    def block(cases, context):
        captured.append((deepcopy(cases), deepcopy(context)))
        entered.set()
        if not release.wait(30):
            raise AssertionError("test did not release the simulated model wait")
        return original(cases, context)

    with only_rule(api, selected, {"cutoff": 72}):
        payload = {"snapshot_id": snapshot["snapshot_id"], "risk_topic": f"并发与冻结验证-{retry}", "org_scope": [org["id"]]}
        failed_id = None
        prior_case_ids = None
        if retry:
            def fail(cases, context):
                raise AnalysisError("测试模拟首次模型失败")
            with monkeypatch.context() as patch:
                patch.setattr(provider, "explain_batch", fail)
                failed = api.post("/api/runs", json=payload)
            assert failed.status_code == 502
            failed_id = failed.json()["detail"]["run_id"]
            prior_case_ids = [item["id"] for item in cases_for(api, failed_id)]
        monkeypatch.setattr(provider, "explain_batch", block)

        async def race():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                first = asyncio.create_task(client.post("/api/runs", json={**payload, "retry": retry}))
                try:
                    assert await asyncio.to_thread(entered.wait, 30), "analysis callback was not reached"
                    context = captured[0][1]
                    run_id = context["run_id"]
                    saved = next(item for item in good(await client.get("/api/runs")) if item["id"] == run_id)
                    assert saved["status"] == "running"
                    assert saved["selection"]["task_context"] == context
                    assert context["org_scope"] == [org["id"]]
                    frozen = next(rule for rule in context["rule_snapshot"] if rule["id"] == selected["id"])
                    assert frozen["params"]["cutoff"] == 72
                    retained = good(await client.get("/api/cases", params={"run_id": run_id}))
                    assert len(retained) == 1 and retained[0]["org_id"] == org["id"]
                    if prior_case_ids is not None:
                        assert [item["id"] for item in retained] == prior_case_ids
                    # A second request must observe the committed owner, not invoke Pi again.
                    duplicate = good(await asyncio.wait_for(client.post("/api/runs", json={**payload, "retry": retry}), 5))
                    assert duplicate["id"] == run_id and duplicate["status"] == "running"
                    assert len(captured) == 1
                    # Both writes use independent database sessions while the model is waiting.
                    edited = good(await asyncio.wait_for(client.patch(f"/api/rules/{selected['id']}", json={"params": {"cutoff": 82}}), 5))
                    assert edited["params"]["cutoff"] == 82 and edited["version"] > frozen["version"]
                    imported = good(await asyncio.wait_for(client.post("/api/import", files={
                        "file": ("late-snapshot.csv", csv_bytes([row(org, "overdue_ratio", "99.99")]), "text/csv"),
                    }), 5))
                    assert imported["snapshot_id"] not in context["history_snapshot_ids"]
                    assert captured[0][1] == context
                    release.set()
                    finished = good(await asyncio.wait_for(first, 60))
                    assert finished["id"] == run_id and finished["status"] == "completed"
                    assert finished["summary"]["triggered"] == 1
                    assert finished["summary"]["case_count"] == 1
                    assert finished["selection"]["task_context"] == context
                    case = good(await client.get(f"/api/cases/{retained[0]['id']}"))
                    assert case["evidence"][0]["rank"] == 75
                    assert case["evidence"][0]["rule_version"] == frozen["version"]
                    assert all(item["snapshot_id"] != imported["snapshot_id"] for item in case["analysis"]["queried_evidence"])
                    assert len(captured) == 1
                    if failed_id is not None:
                        assert run_id == failed_id
                finally:
                    release.set()
                    if not first.done():
                        await asyncio.wait_for(first, 60)

        asyncio.run(race())


def test_python_process_timeout_follows_ts_batch_budget_with_cleanup_margin(monkeypatch):
    from backend import analysis

    calls = []

    class Process:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def communicate(self, payload, *, timeout):
            calls[0]["payload"] = json.loads(payload)
            calls[0]["timeout"] = timeout
            return "{}", ""

    def capture(command, **kwargs):
        calls.append(kwargs)
        return Process()

    monkeypatch.setenv("RISK_PI_BATCH_TIMEOUT_MS", "23500")
    monkeypatch.setattr(analysis.subprocess, "Popen", capture)
    assert analysis.provider._request({"operation": "select"}) == {}
    assert len(calls) == 1
    assert calls[0]["timeout"] == 33.5
    assert calls[0]["payload"]["budget_ms"] == 23500
    assert calls[0]["env"]["RISK_PI_BATCH_TIMEOUT_MS"] == "23500"


def test_pi_corrects_a_real_mcp_query_error_then_completes(api, monkeypatch, tmp_path):
    """只替换模型流；Pi、MCP协议解析、数据库取证和API保存均真实执行。"""
    from backend.analysis import provider

    root = Path(__file__).resolve().parents[1]
    runtime_url = (root / "agent/dist/runtime.js").as_uri()
    script = tmp_path / "correct-mcp-query.mjs"
    script.write_text(
        'import { runRequest, toolStream } from ' + json.dumps(runtime_url) + ';\n' + r"""
import assert from "node:assert/strict";
const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const request = JSON.parse(Buffer.concat(chunks).toString("utf8"));
let corrected = false;
const read = message => JSON.parse(message.content.filter(block => block.type === "text").map(block => block.text).join(""));
const streamFn = (model, context) => {
  const results = context.messages.filter(message => message.role === "toolResult");
  const last = results.at(-1);
  if (!last) return toolStream(model, "check_risks", {});
  if (last.toolName === "check_risks") {
    assert.equal(last.isError, false);
    return toolStream(model, "query_metrics", { indicator_ids: ["NOT_A_REAL_INDICATOR"] });
  }
  if (last.toolName === "query_metrics" && last.isError) {
    assert.match(last.content.map(block => block.text ?? "").join(""), /NOT_A_REAL_INDICATOR/);
    assert.equal(corrected, false);
    corrected = true;
    return toolStream(model, "query_metrics", { indicator_ids: ["overdue_ratio"] });
  }
  assert.equal(last.isError, false);
  if (last.toolName === "query_metrics") return toolStream(model, "get_letter_template", { version: "test-v1" });
  const queried = results.find(message => message.toolName === "query_metrics" && !message.isError);
  const evidence = read(queried).evidence;
  assert.equal(evidence.length, 1);
  const proposal = [{ text: "结合本次补查的逾期率核查贷款质量变化。", evidence_ids: [evidence[0].evidence_id] }];
  return toolStream(model, "submit_analysis", { hypotheses: proposal, checks: proposal });
};
const result = await runRequest(request, { env: process.env, streamFn, limits: { requests: 8, tools: 8 } });
assert.equal(corrected, true);
process.stdout.write(JSON.stringify(result));
""", encoding="utf-8")

    def scripted_analysis(cases, context):
        response = subprocess.run(
            [shutil.which("node"), str(script)],
            input=json.dumps({"operation": "explain_batch", "cases": cases, "context": context, "budget_ms": 30000}),
            text=True, encoding="utf-8", capture_output=True, timeout=60,
            env={**os.environ, "RISK_PYTHON_EXECUTABLE": sys.executable, "RISK_PROJECT_ROOT": str(root),
                 "RISK_PI_MAX_REQUESTS": "8", "RISK_PI_MAX_TOOLS": "8"},
        )
        assert response.returncode == 0, response.stderr
        return json.loads(response.stdout)["analyses"]

    monkeypatch.setattr(provider, "explain_batch", scripted_analysis)
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        result = good(api.post("/api/runs", json={
            "snapshot_id": "demo-2026-10-28", "risk_topic": "纠正补查参数后继续分析", "org_scope": ["LS082"],
        }))
        assert result["status"] == "completed"
        case = cases_for(api, result["id"])[0]
        analysis = good(api.get(f"/api/cases/{case['id']}"))["analysis"]
        query_events = [event for event in analysis["trace"]
                        if event.get("tool") == "query_metrics" and event.get("transport") == "mcp"]
        assert [event["status"] for event in query_events] == ["error", "ok"]
        queried = analysis["queried_evidence"]
        assert len(queried) == 1 and queried[0]["indicator_id"] == "overdue_ratio"
        assert analysis["evidence_references"]["checks"][0]["evidence_ids"] == [queried[0]["evidence_id"]]
        assert "4.04%" in good(api.post(f"/api/cases/{case['id']}/draft"))["content"]

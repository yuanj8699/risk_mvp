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
import os
from pathlib import Path
import sys
import tempfile

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


def test_unconfigured_pi_model_fails_without_creating_a_run(api, monkeypatch):
    before = good(api.get("/api/runs"))
    snapshot = good(api.get("/api/bootstrap"))["snapshots"][0]
    monkeypatch.setenv("RISK_PI_MODE", "model")
    for name in ("RISK_PI_PROVIDER", "RISK_PI_MODEL", "RISK_PI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    response = api.post("/api/runs", json={"snapshot_id": snapshot["id"], "risk_topic": "配置错误验证"})
    assert response.status_code == 502
    assert "Pi Agent" in response.json()["detail"]
    assert good(api.get("/api/runs")) == before


def test_agent_batch_failure_rolls_back_and_can_be_retried(api, monkeypatch):
    from backend.analysis import AnalysisError, provider

    org = societies(api)[0]
    snapshot = good(import_csv(api, [row(org, rank="82")]))
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        before_runs = good(api.get("/api/runs"))
        before_cases = good(api.get("/api/cases"))

        def unavailable(cases):
            raise AnalysisError("测试模拟：Pi 分析服务不可用")

        with monkeypatch.context() as patch:
            patch.setattr(provider, "explain_batch", unavailable)
            response = api.post("/api/runs", json={"snapshot_id": snapshot["snapshot_id"], "risk_topic": "信用风险"})
        assert response.status_code == 502
        assert good(api.get("/api/runs")) == before_runs
        assert good(api.get("/api/cases")) == before_cases
        assert run(api, snapshot["snapshot_id"])["summary"]["case_count"] == 1


def test_stale_supporting_values_do_not_enter_pi_analysis(api):
    org = societies(api)[0]
    snapshot = good(import_csv(api, [row(org, rank="82"),
                                   row(org, "funds_npl_change", "-30000", data_date="2025-01-01")]))
    with only_rule(api, rule_for(api, "rank", "npl_ratio")):
        result = run(api, snapshot["snapshot_id"])
        case = cases_for(api, result["id"])[0]
        detail = good(api.get(f"/api/cases/{case['id']}"))
        assert "资金业务不良余额比年初增量" not in "\n".join(detail["analysis"]["facts"])

"""自动风险报告：通过正式扫描入口验证后台任务、行社互斥与失败重试。"""
from collections import Counter
from datetime import date
from threading import Event
from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


def test_select_auto_report_targets(api, monkeypatch):
    from backend.risk_scans import select_auto_report_targets

    def case(org, floors, signals=0):
        ids = ["TEST_METRIC_097", "TEST_METRIC_013", "TEST_METRIC_015", "TEST_METRIC_017"]
        evidence = [dict(org_id=org, indicator_id=key, indicator_name="实测指标", unit="%", data_date="2026-09-30",
                         usable=True, quality_status="valid", value="1", value_display="1%", evidence_id=key)
                    for key in [*ids[:floors], "deposit_growth"]]
        return SimpleNamespace(org_id=org, org_name=org, data_date="2026-09-30", evidence=evidence,
                               analysis={"signals": [dict(evidence_id="deposit_growth", dimension_id="credit_risk",
                                                          reason=f"线索{i}") for i in range(signals)]})

    def selected(cases):
        return [row["org_id"] for row in select_auto_report_targets(cases)]

    monkeypatch.delenv("RISK_AUTO_REPORT_MAX", raising=False)
    assert selected([case("C", 3), case("B", 3, 1), case("A", 3, 1), case("D", 4), case("E", 2)]) == [
        "D", "A", "B", "C"]
    assert selected(case(str(i), 3) for i in range(9)) == list("01234567")
    assert selected(case(str(i), 1) for i in range(6)) == list("01234")
    assert selected(case(str(i), 0, i) for i in range(5)) == list("432")
    assert selected([case("A", 0)]) == []
    monkeypatch.setenv("RISK_AUTO_REPORT_MAX", "0")
    assert selected([case("A", 4)]) == []


def test_scan_queues_reports_and_retries_failed_only(api, tmp_path, monkeypatch):
    from backend import data_fetcher, risk_letter, risk_scans
    from backend.models import Base, Indicator, Letter, LetterHistory, Observation, Organization, Snapshot

    # 极小合成批次使用真实扫描/归档与规则；仅模型合并、撰函使用可控的离线替身。
    engine = create_engine(f"sqlite:///{(tmp_path / 'reports.db').as_posix()}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    for module in (risk_scans, data_fetcher):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setenv("RISK_DATA_FETCHER", "local")
    monkeypatch.setenv("RISK_AUTO_REPORT_MAX", "3")
    day = date(2026, 9, 30)
    with sessions.begin() as db:
        db.add(Snapshot(id="batch", name="测试期次", data_date=day, source="test"))
        db.add(Indicator(id="TEST_METRIC_097", name="拨备覆盖率", unit="%", frequency="月", description="测试"))
        for i in range(82):
            db.add(Organization(id=str(i), code=str(i), name=f"测试{i}行", level=3))
        for org in "012":
            db.add(Observation(snapshot_id="batch", org_id=org, indicator_id="TEST_METRIC_097", value="135",
                               unit="%", data_date=day))
    monkeypatch.setattr(risk_scans.provider, "synthesize_risks", lambda *args: {"executive_summary": "测试", "issues": []})
    entered = {org: Event() for org in "012"}
    release = Event()
    calls = Counter()

    def compose(case):
        calls[case.org_id] += 1
        entered[case.org_id].set()
        assert release.wait(15)
        if case.org_id == "1" and calls["1"] == 1:
            raise risk_letter.AnalysisError("测试模型生成失败")
        return f"{case.org_name}报告", {"trace": []}

    monkeypatch.setattr(risk_letter, "compose_risk_letter_with_trace", compose)
    try:
        response = api.post("/api/risk-scans", json={"snapshot_id": "batch"})
        assert response.status_code == 200, response.text
        scan = response.json()
        url = "/api/risk-scans/" + scan["id"]
        assert scan["status"] == "completed" and scan["auto_reports"]["status"] == "running"
        assert entered["0"].wait(5) and entered["1"].wait(5)
        progress = api.get(url).json()["auto_reports"]
        assert [item["status"] for item in progress["items"]] == ["running", "running", "queued"]
        blocked = api.post(url + "/societies/0/draft")
        assert (blocked.status_code, blocked.json()["detail"]) == (409, "正在自动生成该行社风险报告，请稍候")
        assert api.post(url + "/auto-reports").json()["status"] == "running"
        release.set()
        risk_scans.shutdown_auto_reports()
        progress = api.get(url).json()["auto_reports"]
        assert (progress["status"], progress["total"], progress["completed"], progress["failed"]) == ("partial", 3, 2, 1)
        assert progress["items"][1]["error"] == "测试模型生成失败"
        assert progress["started_at"] <= progress["updated_at"]

        assert api.post(url + "/auto-reports").json()["completed"] == 2
        risk_scans.shutdown_auto_reports()
        scan = api.get(url).json()
        assert scan["auto_reports"]["status"] == "completed" and calls == {"0": 1, "1": 2, "2": 1}
        assert {org: row["revision"] for org, row in scan["letter_drafts"].items()} == dict.fromkeys("012", 1)
        assert api.post(url + "/auto-reports").json()["status"] == "completed"
        draft = api.post(url + "/societies/0/draft").json()
        with sessions.begin() as db:
            db.get(Letter, draft["id"]).status = "approved"
        assert api.post(url + "/auto-reports", json={"retry_failed_only": False}).json()["status"] == "running"
        risk_scans.shutdown_auto_reports()
        progress = api.get(url).json()["auto_reports"]
        assert [(row["org_id"], row["revision"]) for row in progress["items"]] == [("0", 1), ("1", 2), ("2", 2)]
        with sessions() as db:
            assert len(db.scalars(select(LetterHistory)).all()) == 5

        # 模型仍在生成时释放连接池，旧线程不得重开数据库写回；补跑接手未完成的草稿。
        release.clear()
        entered["1"].clear()
        entered["2"].clear()
        api.post(url + "/auto-reports", json={"retry_failed_only": False}).raise_for_status()
        assert entered["1"].wait(5) and entered["2"].wait(5)
        engine.dispose()
        release.set()
        risk_scans.shutdown_auto_reports()
        with sessions() as db:
            assert len(db.scalars(select(LetterHistory)).all()) == 5
        assert api.post(url + "/auto-reports").json()["status"] == "running"
        risk_scans.shutdown_auto_reports()
        progress = api.get(url).json()["auto_reports"]
        assert progress["completed"] == 3 and [r["revision"] for r in progress["items"]] == [1, 3, 3]
    finally:
        release.set()
        risk_scans.shutdown_auto_reports()
        engine.dispose()

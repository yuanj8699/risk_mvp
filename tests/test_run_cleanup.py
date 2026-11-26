"""通过历史清理 HTTP 接口验证外键顺序、保护条件与失败任务范围。"""
from datetime import date

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def archive(api, tmp_path, monkeypatch):
    from backend import app, models, risk_scans

    engine = create_engine(f"sqlite:///{(tmp_path / 'archive.db').as_posix()}")
    event.listen(engine, "connect", models.sqlite_settings)
    sessions = sessionmaker(engine)
    for module in (app, risk_scans):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    try:
        models.Base.metadata.create_all(engine)
        with sessions.begin() as db:
            db.add(models.Snapshot(id="snap", name="测试归档", data_date=date(2026, 9, 30), source="test"))
            db.add(models.Organization(id="org", code="org", name="甲行", level=3))
        yield sessions
    finally:
        engine.dispose()


def _add_run(db, ident, *, kind=None, status="completed", auto_status=None, letter_status="draft"):
    from backend.models import AgentTurn, AnalysisRun, Letter, LetterHistory, QueryBudgetLedger, QueryOperation, RiskCase

    selection = {"kind": kind} if kind else {}
    if auto_status:
        selection["risk_scan_result"] = {"auto_reports": {"status": auto_status}}
    db.add(AnalysisRun(id=ident, run_key=ident, snapshot_id="snap", status=status, selection=selection))
    db.flush()
    db.add(RiskCase(id=ident, run_id=ident, org_id="org", org_name="甲行", region_name="东",
                    data_date=date(2026, 9, 30), headline="测试"))
    db.flush()
    db.add(Letter(id=ident, case_id=ident, content="报告内容", evidence_hash="abc", status=letter_status))
    db.flush()
    db.add_all([
        LetterHistory(id=ident, letter_id=ident, revision=1, content="报告内容", evidence_hash="abc"),
        AgentTurn(id=ident, run_id=ident, scope=ident, turn_no=1, question="问", answer="答", evidence=[], diagnostics={}),
        QueryBudgetLedger(run_id=ident, requests_used=1),
        QueryOperation(id=ident, run_id=ident, operation="read", owner_pid=1, started_at=1, deadline_at=2, finished_at=2),
    ])


def _assert_remaining(sessions, expected):
    from backend.models import AgentTurn, AnalysisRun, Letter, LetterHistory, QueryBudgetLedger, QueryOperation, RiskCase

    with sessions() as db:
        for key in (AnalysisRun.id, RiskCase.run_id, Letter.case_id, LetterHistory.letter_id,
                    AgentTurn.run_id, QueryOperation.run_id, QueryBudgetLedger.run_id):
            assert set(db.scalars(select(key))) == expected, key


def test_single_run_delete_routes_and_kind_scope(api, archive):
    with archive.begin() as db:
        _add_run(db, "scan", kind="risk_scan")
        _add_run(db, "code", kind="code")
        _add_run(db, "legacy")

    assert api.request("DELETE", "/api/risk-scans/code").status_code == 404
    assert api.request("DELETE", "/api/code-runs/legacy").status_code == 404
    remaining = {"legacy", "code", "scan"}
    _assert_remaining(archive, remaining)
    for route, ident in (("risk-scans", "scan"), ("code-runs", "code"), ("runs", "legacy")):
        response = api.request("DELETE", f"/api/{route}/{ident}")
        assert response.status_code == 200
        remaining.remove(ident)
        _assert_remaining(archive, remaining)
        assert api.request("DELETE", f"/api/{route}/{ident}").status_code == 404


def test_cleanup_only_failed_unprotected_runs(api, archive):
    with archive.begin() as db:
        _add_run(db, "working", kind="risk_scan", status="running")
        _add_run(db, "reporting", kind="risk_scan", status="failed", auto_status="running")
        _add_run(db, "approved", kind="risk_scan", status="failed", letter_status="approved")
        _add_run(db, "sent", kind="code", status="failed", letter_status="simulated_sent")
        _add_run(db, "ready", kind="risk_scan")
        _add_run(db, "badscan", kind="risk_scan", status="failed")
        _add_run(db, "badcode", kind="code", status="failed")
        _add_run(db, "badlegacy", status="failed")

    for ident in ("working", "reporting", "approved", "sent"):
        response = api.request("DELETE", f"/api/runs/{ident}")
        assert response.status_code == 409
        if ident in {"approved", "sent"}:
            assert response.json()["detail"] == "存在已定稿或已下发的风险报告，禁止删除"
    protected = {"working", "reporting", "approved", "sent", "ready"}
    deletable = {"badscan", "badcode", "badlegacy"}
    _assert_remaining(archive, protected | deletable)
    response = api.post("/api/risk-scans/cleanup-failed")
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 3
    assert set(response.json()["deleted_ids"]) == deletable
    assert api.post("/api/risk-scans/cleanup-failed").json() == {"deleted_count": 0, "deleted_ids": []}
    _assert_remaining(archive, protected)

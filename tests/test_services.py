"""共享服务的真实SQLite契约；不连接内网，不改应用演示库。"""
from datetime import date
from decimal import Decimal
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def store(tmp_path, monkeypatch):
    # 延后导入，避免干扰API测试为backend.models设置的独立数据库。
    from backend import models as m, services as s
    from backend.seed import seed_database
    url = "sqlite:///" + str(tmp_path / "services.db")
    engine = create_engine(url)
    factory = sessionmaker(engine, expire_on_commit=False)
    m.Base.metadata.create_all(engine)
    monkeypatch.setattr(s, "SessionLocal", factory)
    with factory() as db:
        seed_database(db)
        snap = db.get(m.Snapshot, "demo-2026-10-28")
        rules = [{key: getattr(rule, key) for key in ("id", "name", "kind", "indicator_id", "enabled", "params", "version")}
                 for rule in db.scalars(select(m.Rule).order_by(m.Rule.id))]
        context = s.freeze_context(db, "service-run", snap, "信用风险", rules, ["LS082"])
        db.add(m.AnalysisRun(id="service-run", run_key="service-run", snapshot_id=snap.id,
                            selection={"task_context": context}, rule_snapshot=rules))
        db.commit()
    yield SimpleNamespace(models=m, services=s, factory=factory, context=context, url=url)
    engine.dispose()


def test_current_and_history_respect_actual_periods_and_stable_evidence(store):
    s, context = store.services, store.context
    result = s.query_metrics(context, ["npl_ratio", "funds_npl_balance"])
    rows = {x["indicator_id"]: x for x in result["evidence"]}
    assert rows["npl_ratio"]["data_date"] == "2026-10-28"
    assert rows["funds_npl_balance"]["data_date"] == "2026-09-30"
    assert all(x["usable"] for x in rows.values())
    assert s.query_metrics(context, ["npl_ratio"])["evidence"][0]["evidence_id"] == rows["npl_ratio"]["evidence_id"]
    history = s.query_metrics(context, ["funds_npl_balance"], history=True)["evidence"]
    assert [x["data_date"] for x in history] == ["2026-06-30", "2026-09-30"]
    assert history[-1]["id"] == rows["funds_npl_balance"]["id"]
    assert all(x["evidence_id"] == x["id"] == x["source_record_id"] for x in history)
    daily_history = s.query_metrics(context, ["npl_ratio"], history=True)["evidence"]
    assert len(daily_history) == 3
    assert daily_history[0]["usable"] is True
    assert daily_history[0]["current_issue"] is not None


def test_frozen_history_ignores_new_imports_and_other_sources(store):
    m, s = store.models, store.services
    before = s.query_metrics(store.context, ["npl_ratio"], history=True)
    with store.factory() as db:
        for key, source, day in [("late", "synthetic", date(2026, 10, 28)),
                                 ("foreign", "file_import", date(2026, 10, 28)),
                                 ("future", "synthetic", date(2026, 10, 29))]:
            db.add(m.Snapshot(id=key, name=key, source=source, data_date=day, status="ready"))
            db.flush()
            db.add(m.Observation(snapshot_id=key, org_id="LS082", indicator_id="npl_ratio", data_date=day,
                                 value=Decimal("99"), unit="%", quality_status="valid"))
        db.commit()
        fresh = s.freeze_context(db, "fresh", db.get(m.Snapshot, "demo-2026-10-28"), "信用风险", [], ["LS082"])
    assert "late" not in store.context["history_snapshot_ids"]
    assert "foreign" not in fresh["history_snapshot_ids"]
    assert "future" not in fresh["history_snapshot_ids"]
    assert s.query_metrics(store.context, ["npl_ratio"], history=True) == before


def test_latest_missing_value_is_not_replaced_with_old_normal_value(store):
    m, s = store.models, store.services
    with store.factory() as db:
        obs = db.scalar(select(m.Observation).where(m.Observation.snapshot_id == "demo-2026-10-28",
                                                  m.Observation.org_id == "LS082", m.Observation.indicator_id == "npl_ratio"))
        obs.value, obs.quality_status = None, "missing"
        db.commit()
    row = s.query_metrics(store.context, ["npl_ratio"])["evidence"][0]
    assert row["data_date"] == "2026-10-28"
    assert row["value"] is None and row["usable"] is False and row["quality_status"] == "missing"


def test_unavailable_frozen_snapshot_does_not_silently_shorten_history(store):
    m, s = store.models, store.services
    with store.factory() as db:
        db.get(m.Snapshot, "demo-2026-08-31").status = "unavailable"
        db.commit()
    with pytest.raises(s.ServiceError) as error:
        s.query_metrics(store.context, ["npl_ratio"], history=True)
    assert error.value.code == "SOURCE_ERROR"


def test_frozen_rules_and_context_are_visible_to_separate_session(store):
    m, s = store.models, store.services
    context = s.load_task_context("service-run", "LS082")
    initial = s.check_risks(context)
    with store.factory() as db:
        rule = db.get(m.Rule, "rank_npl_ratio")
        rule.enabled = False
        rule.version += 1
        db.commit()
    assert s.check_risks(context) == initial
    assert any(e["rule_id"] == "rank_npl_ratio" for c in initial["cases"] for e in c["evidence"])
    assert initial["summary"]["case_count"] == 1
    with pytest.raises(s.ServiceError, match="授权范围"):
        s.load_task_context("service-run", "LS081")


def test_query_boundaries_pagination_and_decimal_values(store):
    s = store.services
    first = s.query_metrics(store.context, ["npl_ratio"], history=True, limit=1)
    second = s.query_metrics(store.context, ["npl_ratio"], history=True, limit=1, offset=first["next_offset"])
    assert first["total"] == second["total"] == 3
    assert first["evidence"][0]["id"] != second["evidence"][0]["id"]
    assert isinstance(first["evidence"][0]["value"], str)
    for ids, orgs, code in [(["unknown"], None, "UNKNOWN_INDICATOR"),
                            (["npl_ratio"], ["LS001"], "SCOPE_VIOLATION"),
                            ([], None, "INVALID_ARGUMENT")]:
        with pytest.raises(s.ServiceError) as error:
            s.query_metrics(store.context, ids, org_ids=orgs)
        assert error.value.code == code
    with pytest.raises(s.ServiceError):
        s.query_metrics(store.context, ["npl_ratio"], limit=0)
    page = s.search_indicators("不良", limit=1)
    assert page["total"] > 1 and page["next_offset"] == 1
    assert {"definition", "unit", "frequency", "ranking"} <= page["items"][0].keys()


def test_template_and_read_only_tools_do_not_create_business_state(store):
    m, s = store.models, store.services
    with store.factory() as db:
        before = {table.name: len(db.execute(select(table)).all()) for table in m.Base.metadata.sorted_tables}
    s.check_risks(store.context)
    s.query_metrics(store.context, ["overdue_ratio", "npl_ratio"], history=True)
    template = s.get_letter_template()
    rendered = template["template"].format(org_name="合成行社082", data_date="2026-10-28", facts="事实", checks="建议", supplementary="")
    assert "模拟下发不构成真实发函" in rendered
    with store.factory() as db:
        after = {table.name: len(db.execute(select(table)).all()) for table in m.Base.metadata.sorted_tables}
    assert before == after


def test_stdio_mcp_reads_committed_context_and_recovers_from_bad_query(store):
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport
    root = str(Path(__file__).resolve().parents[1])
    env = {**os.environ, "RISK_DATABASE_URL": store.url, "RISK_TASK_RUN_ID": "service-run",
           "RISK_TASK_ORG_ID": "LS082", "RISK_SOURCE_DATABASE_URL": "postgresql://unused-invalid-source",
           "PYTHONUTF8": "1", "FASTMCP_LOG_LEVEL": "ERROR"}

    async def exercise():
        transport = StdioTransport(sys.executable, ["-m", "backend.mcp_server"], env=env, cwd=root, keep_alive=False)
        async with Client(transport) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert names == {"search_indicators", "query_metrics", "check_risks", "get_letter_template"}
            invalid = await client.call_tool("query_metrics", {"indicator_ids": ["unknown"]}, raise_on_error=False)
            assert invalid.is_error
            error = json.loads(invalid.content[0].text)
            assert error["error"]["code"] == "UNKNOWN_INDICATOR"
            valid = await client.call_tool("query_metrics", {"indicator_ids": ["overdue_ratio"]})
            evidence = valid.data["evidence"]
            assert len(evidence) == 1 and evidence[0]["org_id"] == "LS082"
            assert evidence[0]["snapshot_id"] == "demo-2026-10-28"
            denied = await client.call_tool("query_metrics", {"indicator_ids": ["npl_ratio"], "org_ids": ["LS001"]}, raise_on_error=False)
            assert json.loads(denied.content[0].text)["error"]["code"] == "SCOPE_VIOLATION"
    asyncio.run(exercise())

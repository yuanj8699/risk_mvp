"""真实QuickJS/MCP/SQLite计划交接；容器验收单独执行，不以宿主计算代替Docker。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import select

from test_api_workflow import api, good
from test_code_workflow import large_demo
from test_code_runner import docker_ready


@pytest.fixture(autouse=True)
def application_environment(api, monkeypatch):
    # 多模块复用已导入的API时，子进程必须连接其实际engine，不能连接另一个fixture的临时URL。
    from backend.models import engine
    monkeypatch.setenv("RISK_DATABASE_URL", str(engine.url))


def context_for(snapshot_id, scope=None, rules=True):
    from backend.models import AnalysisRun, Rule, SessionLocal, Snapshot, uid
    from backend.services import freeze_context
    with SessionLocal() as db:
        frozen = [{key: getattr(r, key) for key in ("id", "name", "kind", "indicator_id", "enabled", "params", "version")}
                  for r in db.scalars(select(Rule).order_by(Rule.id))] if rules else []
        run_id = uid()
        context = freeze_context(db, run_id, db.get(Snapshot, snapshot_id), "Code Mode验证", frozen,
                                 scope or [f"LS{i:03d}" for i in range(1, 83)])
        db.add(AnalysisRun(id=run_id, run_key=run_id, snapshot_id=snapshot_id,
                          selection={"kind": "code", "task_context": context}, rule_snapshot=frozen))
        db.commit()
    return context


def node_plan(context, directory, codemode):
    root = Path(__file__).resolve().parents[1]
    script = directory / (context["run_id"] + ".mjs")
    script.write_text(f'''
import {{BusinessMcpClient, EXPLORATION_TOOLS, PLAN_HOST_TOOL}} from {json.dumps((root / 'agent/dist/mcp-business.js').as_uri())};
import {{createRiskJsTool, DEMO_RISK_JS}} from {json.dumps((root / 'agent/dist/risk-js.js').as_uri())};
const context={json.dumps(context, ensure_ascii=False)};
const mcp=new BusinessMcpClient(process.env,context,undefined,Date.now()+30000,[...EXPLORATION_TOOLS,PLAN_HOST_TOOL]);
let plan, bytes, metrics, trace=[];
const started=performance.now();
try {{
 await mcp.connect();
 if ({str(codemode).lower()}) {{
   let fatal;
   const js=createRiskJsTool({{mcp,deadline:Date.now()+20000,signal:new AbortController().signal,trace,onFatal:e=>fatal=e,onZero:()=>{{}}}});
   const result=await js.tool.execute('test',{{code:DEMO_RISK_JS}});
   if(fatal) throw fatal;
   plan=JSON.parse(result.content.find(x=>x.type==='text').text);
   bytes=Buffer.byteLength(JSON.stringify(result.content)); metrics=js.metrics;
 }} else {{
   const checked=await mcp.call('check_risks',{{}});
   const org_ids=checked.cases.filter(c=>c.evidence.some(e=>e.indicator_id==='npl_ratio'&&e.rule_kind==='rank')).map(c=>c.case_key);
   const catalog=await mcp.call('search_indicators',{{query:'逾期贷款率',limit:5}});
   const indicator_ids=['npl_ratio',...(catalog.items.some(x=>x.id==='overdue_ratio')?['overdue_ratio']:[])];
   plan=await mcp.call(PLAN_HOST_TOOL,{{plan:{{org_ids,indicator_ids,history:true}},check_receipt_id:checked.receipt_id}});
   bytes=Buffer.byteLength(JSON.stringify([checked,catalog,plan]));
 }}
 console.log(JSON.stringify({{plan,bytes,metrics,trace,duration_ms:performance.now()-started}}));
}} finally {{await mcp.close();}}
''', encoding="utf-8")
    env = {**os.environ, "RISK_CODEMODE": "1", "RISK_PROJECT_ROOT": str(root), "RISK_PYTHON_EXECUTABLE": sys.executable}
    run = subprocess.run(["node", str(script)], cwd=root, env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=40)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def test_real_js_and_direct_plan_match_and_share_export_budget(large_demo, tmp_path, monkeypatch):
    import pyarrow.parquet as pq
    from backend import task_workspace as files
    from backend.business_operations import export_operation
    from backend.query_budget import BudgetError
    from backend.query_plans import load_query_plan
    from backend.models import QueryBudgetLedger, SessionLocal

    task_root = tmp_path / "tasks"
    monkeypatch.setenv("RISK_TASK_WORKSPACE_DIR", str(task_root))
    monkeypatch.setattr(files, "TASK_ROOT", task_root)
    monkeypatch.setenv("RISK_QUERY_MAX_REQUESTS", "3")
    direct_context, js_context = context_for(large_demo), context_for(large_demo)
    direct = node_plan(direct_context, tmp_path, False)
    js = node_plan(js_context, tmp_path, True)
    a = load_query_plan(direct_context["run_id"], direct["plan"]["plan_id"])
    b = load_query_plan(js_context["run_id"], js["plan"]["plan_id"])
    assert a["plan"] == b["plan"]
    assert len(b["plan"]["org_ids"]) == 11
    assert js["metrics"]["nested_calls"] == 2
    assert js["metrics"]["workers_started"] == js["metrics"]["workers_exited"] == 1
    assert js["bytes"] < direct["bytes"]
    exported = []
    for context, plan in [(direct_context, a), (js_context, b)]:
        data = export_operation(context, **plan["plan"])
        rows = pq.read_table(files.task_paths(context["run_id"])["data"] / f"{data['dataset_id']}.parquet").to_pylist()
        exported.append(rows)
    assert exported[0] == exported[1]
    assert len(exported[1]) == 528
    with SessionLocal() as db:
        assert db.get(QueryBudgetLedger, js_context["run_id"]).requests_used == 3
    with pytest.raises(BudgetError) as exceeded:
        export_operation(js_context, **b["plan"])
    assert exceeded.value.code == "QUERY_BUDGET_EXCEEDED"
    print(json.dumps({"direct_ms": direct["duration_ms"], "risk_js_ms": js["duration_ms"],
                      "direct_model_bytes": direct["bytes"], "risk_js_model_bytes": js["bytes"],
                      "host_read_calls_each": 2, "rows": len(exported[1]), "llm_connected": False}))


def test_zero_candidates_finishes_without_export_and_keeps_coverage(api, tmp_path, monkeypatch):
    from backend import task_workspace as files
    from backend.analysis import provider
    from backend.code_tasks import validate_code_result
    from backend.models import QueryBudgetLedger, SessionLocal
    task_root = tmp_path / "zero-tasks"
    monkeypatch.setenv("RISK_TASK_WORKSPACE_DIR", str(task_root))
    monkeypatch.setattr(files, "TASK_ROOT", task_root)
    monkeypatch.setenv("RISK_CODEMODE", "1")
    context = context_for("demo-2026-10-28", ["LS001"])
    result = validate_code_result(context["run_id"], provider.explore(context))
    assert result["outcome"] == "zero-candidates"
    assert result["coverage"]["unconfigured"] > 0
    assert result["coverage"]["can_conclude_no_risk"] is False
    assert files.list_executions(context["run_id"]) == []
    assert not list(files.task_paths(context["run_id"])["data"].glob("*.parquet"))
    with SessionLocal() as db:
        assert db.get(QueryBudgetLedger, context["run_id"]).export_rows == 0
    created = good(api.post("/api/code-runs", json={"snapshot_id": "demo-2026-10-28",
                       "org_scope": ["LS001"], "risk_topic": "验证空候选保留覆盖"}))
    assert created["status"] == "no_candidates"
    assert api.post(f"/api/code-runs/{created['id']}/draft").status_code == 409


def test_query_deadline_interrupts_sql_before_first_row_and_resets_connection(api, monkeypatch):
    from backend.business_operations import read_operation
    from backend.query_budget import BudgetError
    from backend import services
    context = context_for("demo-2026-10-28")
    monkeypatch.setenv("RISK_QUERY_TIMEOUT_SECONDS", "0.03")
    def expensive(_):
        with services.read_session() as db:
            return db.connection().exec_driver_sql(
                "SELECT count(*) FROM observations a CROSS JOIN observations b CROSS JOIN observations c").scalar()
    started = time.monotonic()
    with pytest.raises(BudgetError) as stopped:
        read_operation(context, "query_metrics", expensive)
    assert stopped.value.code == "QUERY_TIMEOUT"
    assert time.monotonic() - started < 3
    assert services.search_indicators(query="逾期贷款率")["items"]


def test_mcp_cancel_waits_for_real_sql_to_stop_before_releasing_slot(api, monkeypatch):
    import asyncio
    import threading
    from fastmcp import Client
    from backend import services, mcp_server
    from backend.models import QueryBudgetLedger, QueryOperation, SessionLocal
    context = context_for("demo-2026-10-28")
    monkeypatch.setenv("RISK_TASK_RUN_ID", context["run_id"])
    monkeypatch.setenv("RISK_QUERY_RUN_CONCURRENCY", "1")
    monkeypatch.setenv("RISK_QUERY_TIMEOUT_SECONDS", "10")
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    def wait_in_sql():
        entered.set()
        assert release.wait(5)
        return 1
    def expensive(*_args, **_kwargs):
        try:
            with services.read_session() as db:
                connection = db.connection()
                connection.connection.driver_connection.create_function("wait_in_sql", 0, wait_in_sql)
                try:
                    connection.exec_driver_sql("SELECT wait_in_sql()").scalar()
                    return connection.exec_driver_sql(
                        "SELECT count(*) FROM observations a CROSS JOIN observations b CROSS JOIN observations c").scalar()
                finally:
                    connection.connection.driver_connection.create_function("wait_in_sql", 0, None)
        finally:
            exited.set()
    monkeypatch.setattr(services, "query_metrics", expensive)

    def records():
        with SessionLocal() as db:
            return list(db.scalars(select(QueryOperation).where(QueryOperation.run_id == context["run_id"])))

    async def cancel():
        async with Client(mcp_server.mcp) as client:
            task = asyncio.create_task(client.call_tool("query_metrics", {"indicator_ids": ["npl_ratio"]}))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                # 客户端已取消，但真实SQL仍在运行；不能提前让下一次读取占用这个槽位。
                denied = await client.call_tool("search_indicators", {}, raise_on_error=False)
                assert json.loads(denied.content[0].text)["error"]["code"] == "QUERY_CONCURRENCY"
                active = records()
                assert len(active) == 1 and active[0].finished_at is None
            finally:
                release.set()
            assert await asyncio.to_thread(exited.wait, 3)
            deadline = time.monotonic() + 3
            while records()[0].finished_at is None and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert records()[0].finished_at is not None
            valid = await client.call_tool("search_indicators", {"query": "不良"})
            assert valid.data["items"]
    asyncio.run(cancel())
    with SessionLocal() as db:
        assert db.get(QueryBudgetLedger, context["run_id"]).requests_used == 2
    assert all(row.finished_at is not None for row in records())


def test_large_plan_export_reuses_files_without_recharging_physical_rows(large_demo, tmp_path, monkeypatch):
    from backend import task_workspace as files
    from backend.business_operations import export_operation
    from backend.query_plans import register_query_plan, load_query_plan
    from backend.models import QueryBudgetLedger, SessionLocal
    from backend.seed import METRICS
    monkeypatch.setattr(files, "TASK_ROOT", tmp_path / "large-plans")
    context = context_for(large_demo)
    registered = register_query_plan(context["run_id"], {"org_ids": context["org_scope"],
                           "indicator_ids": [metric[0] for metric in METRICS], "history": True})
    plan = load_query_plan(context["run_id"], registered["plan_id"])["plan"]
    first = export_operation(context, **plan)
    with SessionLocal() as db:
        cost = db.get(QueryBudgetLedger, context["run_id"])
        before = (cost.export_rows, cost.export_bytes)
    second = export_operation(context, **plan)
    assert first["row_count"] == second["row_count"] == 12054
    assert first["dataset_id"] == second["dataset_id"] and second["reused"] is True
    with SessionLocal() as db:
        cost = db.get(QueryBudgetLedger, context["run_id"])
        assert cost.requests_used == 2
        assert (cost.export_rows, cost.export_bytes) == before
        assert cost.export_rows == 12054 and cost.export_bytes > 0


def test_real_container_reads_large_registered_plan(large_demo, tmp_path, monkeypatch, docker_ready):
    from backend import task_workspace as files
    from backend.code_runner import execute_python
    from backend.query_plans import register_query_plan
    from backend.seed import METRICS
    task_root = tmp_path / "container-tasks"
    monkeypatch.setenv("RISK_TASK_WORKSPACE_DIR", str(task_root))
    monkeypatch.setattr(files, "TASK_ROOT", task_root)
    context = context_for(large_demo)
    plan = register_query_plan(context["run_id"], {"org_ids": context["org_scope"],
                              "indicator_ids": [metric[0] for metric in METRICS], "history": True})
    source = """import json
from pathlib import Path
import pandas as pd
import risk_client
plan=json.loads(Path(PLAN_PATH).read_text())
dataset=risk_client.export_metrics(**plan)
data=pd.read_parquet(dataset['path'])
Path('count.json').write_text(json.dumps({'rows':len(data),'orgs':int(data.org_id.nunique())}))
""".replace("PLAN_PATH", repr(plan["path"]))
    files.write_task_file(context["run_id"], "plan.py", source)
    result = execute_python(context["run_id"], "plan.py")
    assert result["exit_code"] == 0, result
    assert json.loads(files.read_task_file(context["run_id"], "count.json")["content"]) == {"rows": 12054, "orgs": 82}

"""隔离代码执行的业务验收：合成大数据、真实Docker轨迹、证据与草稿。

沿用独立API测试库。Docker缺失或不可用时检查应失败并说明环境问题，不能skip冒充通过。
"""
from pathlib import Path
import subprocess
import sys

import pytest

from test_api_workflow import api, good


@pytest.fixture(scope="module")
def large_demo(api):
    from backend.models import SessionLocal
    from scripts.seed_large_demo import seed_large_demo

    with SessionLocal() as db:
        latest = seed_large_demo(db)
    return latest


def test_large_seed_is_idempotent_precise_and_separate_from_default_source(large_demo):
    from sqlalchemy import func, select
    from backend.models import Observation, SessionLocal, Snapshot
    from scripts.seed_large_demo import PRECISE_AMOUNT, SOURCE, seed_large_demo

    with SessionLocal() as db:
        snapshots = list(db.scalars(select(Snapshot).where(Snapshot.source == SOURCE)))
        snapshot_ids = [item.id for item in snapshots]
        observations = list(db.scalars(select(Observation).where(Observation.snapshot_id.in_(snapshot_ids))))
        assert len(snapshots) == 24 and len(observations) == 15744
        assert max(item.data_date.isoformat() for item in snapshots) == "2026-10-28"
        assert min(item.data_date.isoformat() for item in snapshots) == "2024-11-30"
        assert len({item.id for item in observations}) == len(observations)
        # 季度观测在多个月快照重复，但真实期次与值保持一致；读取历史后应有12054条。
        keys = {(item.org_id, item.indicator_id, item.data_date) for item in observations}
        assert len(keys) == 12054
        precise = next(item for item in observations if item.org_id == "LS001" and item.indicator_id == "funds_npl_balance")
        assert precise.value == PRECISE_AMOUNT
        original_count = db.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.source == "synthetic"))
        assert original_count == 3
        assert seed_large_demo(db) == large_demo
        assert db.scalar(select(func.count()).select_from(Observation).where(Observation.snapshot_id.in_(snapshot_ids))) == 15744
        assert db.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.source == "synthetic")) == original_count


def test_large_seed_cli_requires_an_explicit_non_default_database():
    root = Path(__file__).resolve().parents[1]
    for arguments in ([], ["--database", str(root / "data/risk-demo.db")]):
        result = subprocess.run([sys.executable, "-X", "utf8", str(root / "scripts/seed_large_demo.py"), *arguments],
                                capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 2



def _committed_context(snapshot_id, org_scope):
    """为导出边界建立已提交任务；不调用模型或执行器。"""
    from uuid import uuid4
    from backend.models import AnalysisRun, SessionLocal, Snapshot
    from backend.services import freeze_context

    run_id = uuid4().hex
    with SessionLocal() as db:
        context = freeze_context(db, run_id, db.get(Snapshot, snapshot_id), "大数据导出边界验证", [], org_scope)
        db.add(AnalysisRun(id=run_id, run_key=run_id, snapshot_id=snapshot_id,
                          selection={"task_context": context}, rule_snapshot=[]))
        db.commit()
    return context


def test_large_export_is_complete_precise_and_bound_to_task(large_demo, tmp_path, monkeypatch):
    import hashlib
    import json
    import pyarrow.parquet as pq
    from backend import task_workspace as workspace
    from backend.code_tasks import validate_code_result
    from backend.seed import METRICS
    from scripts.seed_large_demo import PRECISE_AMOUNT

    monkeypatch.setattr(workspace, "TASK_ROOT", tmp_path / "tasks")
    context = _committed_context(large_demo, [f"LS{i:03d}" for i in range(1, 83)])
    manifest = workspace.export_dataset(context, [metric[0] for metric in METRICS], history=True)
    paths = workspace.task_paths(context["run_id"])
    parquet = paths["data"] / f"{manifest['dataset_id']}.parquet"
    schema_file = paths["data"] / f"{manifest['dataset_id']}.schema.json"
    rows = pq.read_table(parquet).to_pylist()
    assert manifest["row_count"] == len(rows) == 12054
    assert len({row["evidence_id"] for row in rows}) == len(rows)
    assert len({(row["org_id"], row["indicator_id"], row["data_date"]) for row in rows}) == len(rows)
    assert {row["source"] for row in rows} == {"synthetic-large"}
    assert all(row["data_date"] <= "2026-10-28" and row["snapshot_id"] in context["history_snapshot_ids"] for row in rows)
    assert all(row["id"] == row["evidence_id"] == row["source_record_id"] for row in rows)
    assert manifest["sha256"] == hashlib.sha256(parquet.read_bytes()).hexdigest()
    assert manifest["schema_sha256"] == hashlib.sha256(schema_file.read_bytes()).hexdigest()
    saved_manifest = json.loads((paths["meta"] / "exports" / f"{manifest['dataset_id']}.json").read_text(encoding="utf-8"))
    assert saved_manifest == {key: value for key, value in manifest.items() if key != "reused"}
    assert manifest["reused"] is False
    precise = next(row for row in rows if row["org_id"] == "LS001" and row["indicator_id"] == "funds_npl_balance")
    assert precise["value"] == str(PRECISE_AMOUNT)
    assert workspace.resolve_evidence(context["run_id"], [precise["evidence_id"]])[0]["value"] == str(PRECISE_AMOUNT)

    other = _committed_context(large_demo, ["LS082"])
    workspace.export_dataset(other, ["npl_ratio"], history=True)
    for rejected in (["fabricated-evidence"], [precise["evidence_id"]]):
        with pytest.raises(workspace.WorkspaceError) as failure:
            workspace.resolve_evidence(other["run_id"], rejected)
        assert failure.value.code == "UNKNOWN_EVIDENCE"
        with pytest.raises(workspace.WorkspaceError) as submission_error:
            validate_code_result(other["run_id"], {
                "summary": "拒绝伪造或跨任务引用",
                "findings": [{"org_id": "LS082", "statement": "测试发现", "evidence_ids": rejected}],
                "execution_ids": ["not-used"], "result_file": "result.json",
            })
        assert submission_error.value.code == "UNKNOWN_EVIDENCE"
    # 校验真实文件字节，而非只相信模型或文件名声称的摘要。
    parquet.write_bytes(parquet.read_bytes() + b"tampered")
    with pytest.raises(workspace.WorkspaceError) as failure:
        workspace.resolve_evidence(context["run_id"], [precise["evidence_id"]])
    assert failure.value.code == "EVIDENCE_CHANGED"


def test_code_run_preserves_failed_then_corrected_execution_and_evidence(large_demo, api, tmp_path, monkeypatch):
    """真实Docker端到端；未就绪时会明确失败，不使用执行器替身或skip。"""
    import hashlib
    import json
    from copy import deepcopy
    from backend import task_workspace as workspace
    from backend.code_tasks import validate_code_result

    task_root = tmp_path / "code-tasks"
    monkeypatch.setenv("RISK_TASK_WORKSPACE_DIR", str(task_root))
    monkeypatch.setattr(workspace, "TASK_ROOT", task_root)
    payload = {"snapshot_id": large_demo, "risk_topic": "跨82家行社、24个期次比较不良贷款率变化，保留原始证据"}
    created = good(api.post("/api/code-runs", json=payload))
    detail = good(api.get(f"/api/code-runs/{created['id']}"))
    assert detail["status"] == "completed"
    result = detail["result"]
    assert result["mode"] == "pi-demo" and result["runtime"]["llm_connected"] is False
    assert len(detail["selection"]["task_context"]["org_scope"]) == 82
    by_id = {item["execution_id"]: item for item in detail["executions"]}
    executions = [by_id[key] for key in result["execution_ids"]]
    assert len(executions) == 2
    assert executions[0]["exit_code"] != 0 and "KeyError" in executions[0]["stderr"]
    assert executions[1]["exit_code"] == 0
    assert "12054" in executions[1]["stdout"]
    exported = [item for execution in executions for item in execution["exports"]]
    assert exported and all(item["row_count"] == 12054 for item in exported)
    paths = workspace.task_paths(created["id"])
    for manifest in exported:
        file = paths["data"] / f"{manifest['dataset_id']}.parquet"
        assert hashlib.sha256(file.read_bytes()).hexdigest() == manifest["sha256"]
    work_files = {item["path"]: item for item in detail["files"]}
    assert {"analysis.py", "result.json", "outputs/institution_summary.csv"} <= work_files.keys()
    report_file = paths["work"] / result["result_file"]
    assert hashlib.sha256(report_file.read_bytes()).hexdigest() == result["result_sha256"]
    assert json.loads(report_file.read_text(encoding="utf-8")) == {"summary": result["summary"], "findings": result["findings"]}
    for finding in result["findings"]:
        evidence = workspace.resolve_evidence(created["id"], finding["evidence_ids"])
        assert evidence and all(item["org_id"] == finding["org_id"] for item in evidence)
    assert result["findings"][0]["org_id"] == "LS082"
    assert "0.9200" in result["findings"][0]["statement"]
    draft = good(api.post(f"/api/code-runs/{created['id']}/draft"))["content"]
    assert "LS082" in draft and "0.9200" in draft and result["result_sha256"] in draft
    assert all(key in draft for item in result["findings"] for key in item["evidence_ids"])

    forged = deepcopy(result)
    forged["findings"][0]["evidence_ids"] = ["not-exported-by-this-task"]
    with pytest.raises(workspace.WorkspaceError) as rejected:
        validate_code_result(created["id"], forged)
    assert rejected.value.code == "UNKNOWN_EVIDENCE"
    assert good(api.post("/api/code-runs", json=payload))["id"] == created["id"]
    assert len(good(api.get(f"/api/code-runs/{created['id']}"))["executions"]) == 2


def test_running_or_failed_code_analysis_cannot_generate_draft(large_demo, api, tmp_path, monkeypatch):
    from fastapi import HTTPException
    from backend import task_workspace as workspace
    from backend.analysis import AnalysisError, provider
    from backend.code_tasks import draft_code_report

    task_root = tmp_path / "failed-code-task"
    monkeypatch.setenv("RISK_TASK_WORKSPACE_DIR", str(task_root))
    monkeypatch.setattr(workspace, "TASK_ROOT", task_root)
    checked = []

    def unavailable(context):
        # API已提交running任务，另一个Session上的拟函路由必须拒绝。
        with pytest.raises(HTTPException) as rejected:
            draft_code_report(context["run_id"])
        assert rejected.value.status_code == 409
        checked.append(context["run_id"])
        raise AnalysisError("测试模拟：分析服务暂不可用")

    monkeypatch.setattr(provider, "explore", unavailable)
    response = api.post("/api/code-runs", json={"snapshot_id": large_demo, "risk_topic": "失败任务禁止拟函", "org_scope": ["LS001"]})
    assert response.status_code == 502
    run_id = response.json()["detail"]["run_id"]
    assert checked == [run_id]
    assert good(api.get(f"/api/code-runs/{run_id}"))["status"] == "failed"
    assert api.post(f"/api/code-runs/{run_id}/draft").status_code == 409

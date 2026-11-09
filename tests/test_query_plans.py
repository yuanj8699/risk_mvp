"""计划登记的真实数据库/文件契约：范围、零候选回执、稳定身份及篡改拒绝。"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from test_services import store


@pytest.fixture
def plans(store, tmp_path, monkeypatch):
    from backend import query_plans, task_workspace
    monkeypatch.setattr(task_workspace, "TASK_ROOT", tmp_path / "plans")
    return SimpleNamespace(store=store, api=query_plans, files=task_workspace, run_id=store.context["run_id"])


def _plan(**changes):
    return {"org_ids": ["LS082"], "indicator_ids": ["npl_ratio"], "history": True, **changes}


def _receipt(plans):
    result = plans.store.services.check_risks(plans.store.context)
    return plans.api.small_risk_check(plans.store.context, result)


def test_registration_normalizes_and_reuses_stable_plan_identity(plans):
    first = plans.api.register_query_plan(plans.run_id, _plan(
        org_ids=["LS082", "LS082"], indicator_ids=["overdue_ratio", "npl_ratio", "npl_ratio"]))
    repeated = plans.api.register_query_plan(plans.run_id, _plan(indicator_ids=["npl_ratio", "overdue_ratio"]))
    assert first == repeated and first["status"] == "ready"
    assert first["coverage"] is None
    assert first["org_count"] == 1 and first["indicator_count"] == 2
    assert "plan" not in first
    loaded = plans.api.load_query_plan(plans.run_id, first["plan_id"])
    assert loaded["plan"] == _plan(indicator_ids=["npl_ratio", "overdue_ratio"])
    path = plans.files.task_paths(plans.run_id)["data"] / "plans" / (first["plan_id"] + ".json")
    assert json.loads(path.read_text(encoding="utf-8")) == loaded["plan"]
    assert plans.files.file_sha256(path) == first["sha256"]
    assert first["path"] == f"/data/plans/{first['plan_id']}.json"


@pytest.mark.parametrize("plan,code", [
    (_plan(run_id="other-run"), "INVALID_ARGUMENT"),
    (_plan(coverage={"complete": True}), "INVALID_ARGUMENT"),
    (_plan(history=1), "INVALID_ARGUMENT"),
    (_plan(org_ids="LS082"), "INVALID_ARGUMENT"),
    (_plan(org_ids=["LS001"]), "SCOPE_VIOLATION"),
    (_plan(indicator_ids=["unknown-metric"]), "UNKNOWN_INDICATOR"),
    (_plan(indicator_ids=[]), "INVALID_ARGUMENT"),
    (_plan(org_ids=["LS082"] * 83), "INVALID_ARGUMENT"),
    (_plan(indicator_ids=["npl_ratio"] * 1001), "INVALID_ARGUMENT"),
    (_plan(indicator_ids=["x" * (16 * 1024)]), "INVALID_ARGUMENT"),
])
def test_plan_rejects_extra_fields_types_counts_and_scope(plans, plan, code):
    with pytest.raises(plans.files.WorkspaceError) as error:
        plans.api.register_query_plan(plans.run_id, plan)
    assert error.value.code == code
    assert not list(plans.files.task_paths(plans.run_id)["data"].rglob("*.json"))


def test_zero_candidates_need_real_coverage_and_keep_missing_unconfigured(plans):
    m, store = plans.store.models, plans.store
    with store.factory() as db:
        obs = db.scalar(select(m.Observation).where(m.Observation.org_id == "LS082",
                        m.Observation.indicator_id == "npl_ratio", m.Observation.snapshot_id == "demo-2026-10-28"))
        obs.value, obs.quality_status = None, "missing"
        db.commit()
    with pytest.raises(plans.files.WorkspaceError):
        plans.api.register_query_plan(plans.run_id, _plan(org_ids=[]))
    receipt = _receipt(plans)
    assert receipt["coverage"]["insufficient"] == 1
    assert receipt["coverage"]["unconfigured"] == 1
    assert receipt["coverage"]["triggered"] > 0
    registered = plans.api.register_query_plan(plans.run_id, _plan(org_ids=[]), receipt["receipt_id"])
    loaded = plans.api.load_query_plan(plans.run_id, registered["plan_id"])
    assert loaded["status"] == "zero-candidates" and loaded["plan"]["org_ids"] == []
    assert loaded["coverage"] == receipt["coverage"]
    assert loaded["coverage"]["complete"] is True
    assert loaded["coverage"]["can_conclude_no_risk"] is False
    assert not list(plans.files.task_paths(plans.run_id)["data"].glob("*.parquet"))


def test_no_risk_flag_requires_every_frozen_check_to_be_normal(plans):
    store = plans.store
    context = deepcopy(store.context)
    context["org_scope"] = ["LS001"]
    for rule in context["rule_snapshot"]:
        rule["enabled"] = rule["id"] == "rank_npl_ratio"
    with store.factory() as db:
        run = db.get(store.models.AnalysisRun, plans.run_id)
        run.selection = {"task_context": context}
        run.rule_snapshot = context["rule_snapshot"]
        db.commit()
    receipt = plans.api.small_risk_check(context, store.services.check_risks(context))
    assert receipt["cases"] == [] and receipt["summary"]["triggered"] == 0
    registered = plans.api.register_query_plan(plans.run_id, _plan(org_ids=[]), receipt["receipt_id"])
    assert registered["coverage"]["can_conclude_no_risk"] is True


def test_narrow_receipt_cannot_establish_zero_candidates_for_whole_task(plans):
    store = plans.store
    full = {**store.context, "org_scope": ["LS001", "LS082"]}
    with store.factory() as db:
        db.get(store.models.AnalysisRun, plans.run_id).selection = {"task_context": full}
        db.commit()
    narrowed = {**full, "org_scope": ["LS082"]}
    receipt = plans.api.small_risk_check(narrowed, store.services.check_risks(narrowed))
    assert receipt["coverage"]["full_scope"] is False
    assert receipt["coverage"]["complete"] is False
    with pytest.raises(plans.files.WorkspaceError, match="完整任务范围"):
        plans.api.register_query_plan(plans.run_id, _plan(org_ids=[]), receipt["receipt_id"])
    # 非空补查计划可以选择合法子集，不强迫JS复制全部任务机构或全部触发机构。
    result = plans.api.register_query_plan(plans.run_id, _plan(), receipt["receipt_id"])
    assert result["status"] == "ready" and result["org_count"] == 1


def test_incomplete_checks_or_forged_summary_cannot_mint_receipt(plans):
    original = plans.store.services.check_risks(plans.store.context)
    incomplete = deepcopy(original)
    incomplete["checks"].pop()
    fabricated = deepcopy(original)
    fabricated["summary"]["unconfigured"] = 0
    for result in (incomplete, fabricated):
        with pytest.raises(plans.files.WorkspaceError) as error:
            plans.api.small_risk_check(plans.store.context, result)
        assert error.value.code == "CHECK_RECEIPT_INVALID"


def test_cross_task_receipt_is_rejected_even_if_copied_into_meta(plans):
    receipt = _receipt(plans)
    other_context = {**plans.store.context, "run_id": "other-run"}
    with plans.store.factory() as db:
        db.add(plans.store.models.AnalysisRun(id="other-run", run_key="other-run", snapshot_id=other_context["snapshot_id"],
                                            selection={"task_context": other_context}, rule_snapshot=other_context["rule_snapshot"]))
        db.commit()
    original = plans.files.task_paths(plans.run_id)["meta"] / "check_receipts" / f"{receipt['receipt_id']}.json"
    copied = plans.files.task_paths("other-run")["meta"] / "check_receipts"
    copied.mkdir()
    (copied / original.name).write_bytes(original.read_bytes())
    with pytest.raises(plans.files.WorkspaceError) as error:
        plans.api.register_query_plan("other-run", _plan(org_ids=[]), receipt["receipt_id"])
    assert error.value.code == "SCOPE_VIOLATION"


def test_changed_plan_receipt_or_task_context_is_rejected(plans):
    receipt = _receipt(plans)
    registered = plans.api.register_query_plan(plans.run_id, _plan(org_ids=[]), receipt["receipt_id"])
    paths = plans.files.task_paths(plans.run_id)
    path = paths["data"] / "plans" / f"{registered['plan_id']}.json"
    original = path.read_bytes()
    path.write_text(json.dumps(_plan()), encoding="utf-8")
    with pytest.raises(plans.files.WorkspaceError, match="改变"):
        plans.api.load_query_plan(plans.run_id, registered["plan_id"])
    path.write_bytes(original)
    receipt_path = paths["meta"] / "check_receipts" / f"{receipt['receipt_id']}.json"
    envelope = json.loads(receipt_path.read_text(encoding="utf-8"))
    envelope["receipt"]["coverage"]["can_conclude_no_risk"] = True
    receipt_original = receipt_path.read_bytes()
    receipt_path.write_text(json.dumps(envelope), encoding="utf-8")
    with pytest.raises(plans.files.WorkspaceError, match="回执校验失败"):
        plans.api.load_query_plan(plans.run_id, registered["plan_id"])
    receipt_path.write_bytes(receipt_original)
    with plans.store.factory() as db:
        run = db.get(plans.store.models.AnalysisRun, plans.run_id)
        changed = deepcopy(run.selection)
        changed["task_context"]["topic"] = "变更后的主题"
        run.selection = changed
        db.commit()
    with pytest.raises(plans.files.WorkspaceError) as error:
        plans.api.load_query_plan(plans.run_id, registered["plan_id"])
    assert error.value.code == "SCOPE_VIOLATION"


def test_registration_is_blocked_during_container_execution(plans):
    plans.files.mark_active_execution(plans.run_id, "a" * 32, "risk-analysis-" + "a" * 32)
    try:
        with pytest.raises(plans.files.WorkspaceError) as error:
            plans.api.register_query_plan(plans.run_id, _plan())
        assert error.value.code == "SANDBOX_ACTIVE"
    finally:
        plans.files.clear_active_execution(plans.run_id, "a" * 32, "risk-analysis-" + "a" * 32)

"""宿主登记小查询计划：JS只建议机构/指标/历史选项，可信任务与覆盖回执由宿主绑定。

计划JSON写入容器只读data/plans；校验清单及真实检查回执保存在不挂载的meta。
计划不是授权或风险证据，实际导出仍须经过原有任务范围和查询额度入口。
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from sqlalchemy import select

from . import services, task_workspace as workspace
from .models import Indicator

MAX_PLAN_BYTES = 16 * 1024
MAX_ORGS = 82
MAX_INDICATORS = 1000
PLAN_FIELDS = {"org_ids", "indicator_ids", "history"}
CHECK_STATUSES = {"triggered", "normal", "insufficient", "not_applicable", "unconfigured"}


def _encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value) -> str:
    return hashlib.sha256(_encoded(value)).hexdigest()


def _directory(base: Path, name: str) -> Path:
    directory = base / name
    if workspace._is_link(directory) or not directory.resolve().is_relative_to(base.resolve()):
        raise workspace.WorkspaceError("PATH_VIOLATION", "计划目录不能经过符号链接或超出任务目录")
    directory.mkdir(exist_ok=True)
    return directory


def _read_json(path: Path, limit: int = 1024 * 1024) -> dict:
    if workspace._is_link(path) or not path.is_file() or path.stat().st_size > limit:
        raise workspace.WorkspaceError("PLAN_CHANGED", "计划或检查回执不存在、过大或不是普通文件")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise workspace.WorkspaceError("PLAN_CHANGED", "计划或检查回执无法读取") from exc
    if not isinstance(result, dict):
        raise workspace.WorkspaceError("PLAN_CHANGED", "计划或检查回执格式错误")
    return result


def _write_json(path: Path, value: dict) -> None:
    staged = path.with_suffix(".json.tmp")
    if workspace._is_link(staged) or workspace._is_link(path):
        raise workspace.WorkspaceError("PATH_VIOLATION", "计划文件不能是符号链接")
    staged.write_bytes(_encoded(value))
    staged.replace(path)


def _id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise workspace.WorkspaceError("INVALID_ARGUMENT", "计划或回执ID格式错误")
    return value


def _context(context: dict) -> dict:
    trusted = services.load_task_context(context["run_id"])
    if any(context.get(key) != value for key, value in trusted.items() if key != "org_scope"):
        raise workspace.WorkspaceError("SCOPE_VIOLATION", "检查上下文与已提交任务不一致")
    services._scope(trusted, context.get("org_scope"))
    return trusted


def _coverage(trusted: dict, checked_scope: list[str], checks: list[dict]) -> tuple[dict, dict]:
    """由真实逐项检查重新计算覆盖；JS返回的计数不参与判断。"""
    rules = {rule["id"]: rule for rule in trusted["rule_snapshot"] if rule["enabled"]}
    expected = {(org, rule_id) for org in checked_scope for rule_id in rules}
    seen, counts, triggered_orgs = set(), Counter(), set()
    for check in checks:
        key = (check.get("org_id"), check.get("rule_id"))
        status = check.get("status")
        if key not in expected or key in seen or status not in CHECK_STATUSES:
            raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "逐项检查存在越界、重复或未知状态")
        if check.get("indicator_id") != rules[key[1]]["indicator_id"]:
            raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "检查指标与冻结规则不一致")
        seen.add(key)
        counts[status] += 1
        if status == "triggered":
            triggered_orgs.add(key[0])
    if seen != expected:
        raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "逐项检查未覆盖声明的机构与冻结规则")
    summary = {key: counts[key] for key in ("triggered", "normal", "insufficient", "not_applicable", "unconfigured")}
    summary.update(evaluated=counts["triggered"] + counts["normal"], case_count=len(triggered_orgs))
    full_scope = set(checked_scope) == set(trusted["org_scope"])
    expected_full = len(trusted["org_scope"]) * len(rules)
    coverage = {
        **summary, "expected_checks": expected_full, "checked_checks": len(checks),
        "org_count": len(trusted["org_scope"]), "covered_org_count": len({key[0] for key in seen}),
        "full_scope": full_scope, "complete": full_scope and len(checks) == expected_full,
        "can_conclude_no_risk": full_scope and expected_full > 0 and counts["normal"] == expected_full,
    }
    return summary, coverage


def small_risk_check(context: dict, result: dict) -> dict:
    """仅包装宿主check_risks返回值，不重复取数或扣额度；落真实覆盖回执后返回小投影。"""
    trusted = _context(context)
    checks = result.get("checks")
    cases = result.get("cases")
    if not isinstance(checks, list) or not all(isinstance(item, dict) for item in checks) or not isinstance(cases, list):
        raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "检查结果缺少逐项明细或风险事项")
    summary, coverage = _coverage(trusted, context["org_scope"], checks)
    if not isinstance(result.get("summary"), dict) or any(type(result["summary"].get(key)) is not int or result["summary"][key] != value for key, value in summary.items()):
        raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "检查汇总与实际明细不一致")
    rules = {rule["id"]: rule for rule in trusted["rule_snapshot"] if rule["enabled"]}
    triggered = {(row["org_id"], row["rule_id"]) for row in checks if row["status"] == "triggered"}
    seen, case_keys, projected = set(), set(), []
    for case in cases:
        if not isinstance(case, dict) or case.get("case_key") not in context["org_scope"] or case["case_key"] in case_keys:
            raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "检查事项机构越界或重复")
        case_keys.add(case["case_key"])
        evidence = case.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "触发事项缺少证据")
        compact = []
        for item in evidence:
            if not isinstance(item, dict):
                raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "触发证据格式错误")
            key = (case["case_key"], item.get("rule_id"))
            evidence_id = item.get("evidence_id", item.get("id"))
            if key not in triggered or key in seen or not isinstance(evidence_id, str) or not evidence_id:
                raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "触发证据与真实检查不一致")
            rule = rules[key[1]]
            if item.get("indicator_id") != rule["indicator_id"] or item.get("rule_kind") != rule["kind"]:
                raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "证据口径与冻结规则不一致")
            if item.get("org_id", case["case_key"]) != case["case_key"]:
                raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "证据属于其他机构")
            seen.add(key)
            compact.append({"evidence_id": evidence_id, "indicator_id": rule["indicator_id"], "rule_kind": rule["kind"]})
        projected.append({"case_key": case["case_key"], "evidence": compact})
    if seen != triggered or len(cases) != summary["case_count"]:
        raise workspace.WorkspaceError("CHECK_RECEIPT_INVALID", "触发事项没有完整对应检查明细")
    normalized_checks = [{key: item[key] for key in ("org_id", "rule_id", "indicator_id", "status")}
                         for item in sorted(checks, key=lambda row: (row["org_id"], row["rule_id"]))]
    receipt = {"run_id": trusted["run_id"], "context_sha256": _hash(trusted),
               "checked_scope": sorted(set(context["org_scope"])), "checks": normalized_checks,
               "cases": sorted(projected, key=lambda item: item["case_key"]), "summary": summary, "coverage": coverage}
    receipt_hash = _hash(receipt)
    receipt_id = receipt_hash[:32]
    with workspace.task_lock(trusted["run_id"]):
        folder = _directory(workspace.task_paths(trusted["run_id"])["meta"], "check_receipts")
        _write_json(folder / f"{receipt_id}.json", {"receipt": receipt, "sha256": receipt_hash})
    return {"cases": projected, "summary": summary, "coverage": coverage, "receipt_id": receipt_id}


def _load_receipt(trusted: dict, receipt_id: str) -> dict:
    folder = _directory(workspace.task_paths(trusted["run_id"])["meta"], "check_receipts")
    envelope = _read_json(folder / f"{_id(receipt_id)}.json")
    receipt = envelope.get("receipt")
    if not isinstance(receipt, dict) or _hash(receipt) != envelope.get("sha256") or envelope["sha256"][:32] != receipt_id:
        raise workspace.WorkspaceError("PLAN_CHANGED", "检查回执校验失败")
    if receipt.get("run_id") != trusted["run_id"] or receipt.get("context_sha256") != _hash(trusted):
        raise workspace.WorkspaceError("SCOPE_VIOLATION", "检查回执不属于当前固定任务")
    services._scope(trusted, receipt["checked_scope"])
    summary, coverage = _coverage(trusted, receipt["checked_scope"], receipt["checks"])
    if summary != receipt["summary"] or coverage != receipt["coverage"]:
        raise workspace.WorkspaceError("PLAN_CHANGED", "检查回执覆盖统计不一致")
    return receipt


def _normalize_plan(plan: dict, trusted: dict) -> dict:
    if not isinstance(plan, dict) or set(plan) != PLAN_FIELDS:
        raise workspace.WorkspaceError("INVALID_ARGUMENT", "计划只接受org_ids、indicator_ids和history三个字段")
    try:
        size = len(_encoded(plan))
    except (ValueError, TypeError) as exc:
        raise workspace.WorkspaceError("INVALID_ARGUMENT", "计划必须是有效JSON") from exc
    if size > MAX_PLAN_BYTES:
        raise workspace.WorkspaceError("INVALID_ARGUMENT", "计划JSON不能超过16KiB")
    for field, maximum, minimum in (("org_ids", MAX_ORGS, 0), ("indicator_ids", MAX_INDICATORS, 1)):
        values = plan[field]
        if not isinstance(values, list) or not minimum <= len(values) <= maximum or any(not isinstance(value, str) or not value for value in values):
            raise workspace.WorkspaceError("INVALID_ARGUMENT", f"{field}须为{minimum}至{maximum}项非空ID字符串列表")
    if type(plan["history"]) is not bool:
        raise workspace.WorkspaceError("INVALID_ARGUMENT", "history须为布尔值")
    orgs = sorted(set(plan["org_ids"]))
    if set(orgs) - set(trusted["org_scope"]):
        raise workspace.WorkspaceError("SCOPE_VIOLATION", "计划机构超出当前任务范围")
    indicators = sorted(set(plan["indicator_ids"]))
    with services.read_session() as db:
        known = set(db.scalars(select(Indicator.id).where(Indicator.id.in_(indicators))))
    if set(indicators) - known:
        raise workspace.WorkspaceError("UNKNOWN_INDICATOR", "计划包含目录中不存在的指标")
    return {"org_ids": orgs, "indicator_ids": indicators, "history": plan["history"]}


def _result(manifest: dict, plan: dict, coverage: dict | None, *, include_plan: bool) -> dict:
    result = {"plan_id": manifest["plan_id"], "path": f"/data/plans/{manifest['plan_id']}.json",
              "status": "ready" if plan["org_ids"] else "zero-candidates", "org_count": len(plan["org_ids"]),
              "indicator_count": len(plan["indicator_ids"]), "coverage": coverage,
              "check_receipt_id": manifest["check_receipt_id"], "sha256": manifest["sha256"],
              "context_sha256": manifest["context_sha256"]}
    return {**result, **({"plan": plan} if include_plan else {})}


def register_query_plan(run_id: str, plan: dict, check_receipt_id: str | None = None) -> dict:
    """校验JS建议并登记稳定计划引用；空候选保持为空，绝不回退至整个任务范围。"""
    with workspace.task_lock(run_id):
        trusted = services.load_task_context(run_id)
        normalized = _normalize_plan(plan, trusted)
        receipt = _load_receipt(trusted, check_receipt_id) if check_receipt_id is not None else None
        if not normalized["org_ids"] and (receipt is None or not receipt["coverage"]["complete"]):
            raise workspace.WorkspaceError("INVALID_ARGUMENT", "空候选计划需要覆盖完整任务范围的真实检查回执")
        context_hash = _hash(trusted)
        plan_id = _hash({"context_sha256": context_hash, "plan": normalized})[:32]
        paths = workspace.task_paths(run_id)
        data_dir, meta_dir = _directory(paths["data"], "plans"), _directory(paths["meta"], "plans")
        meta_path = meta_dir / f"{plan_id}.json"
        if meta_path.exists():
            existing = load_query_plan(run_id, plan_id)
            if check_receipt_id is None or check_receipt_id == existing["check_receipt_id"]:
                return {key: value for key, value in existing.items() if key != "plan"}
        manifest = {"plan_id": plan_id, "run_id": run_id, "context_sha256": context_hash,
                    "sha256": _hash(normalized), "check_receipt_id": check_receipt_id}
        _write_json(data_dir / f"{plan_id}.json", normalized)
        _write_json(meta_path, {"manifest": manifest, "sha256": _hash(manifest)})
        return _result(manifest, normalized, receipt["coverage"] if receipt else None, include_plan=False)


def load_query_plan(run_id: str, plan_id: str) -> dict:
    """最终结果验收只读取宿主清单、实际计划文件与可信回执，不接受模型自报覆盖。"""
    with workspace.task_lock(run_id):
        trusted = services.load_task_context(run_id)
        paths = workspace.task_paths(run_id)
        plan_id = _id(plan_id)
        envelope = _read_json(_directory(paths["meta"], "plans") / f"{plan_id}.json")
        manifest = envelope.get("manifest")
        if not isinstance(manifest, dict) or _hash(manifest) != envelope.get("sha256"):
            raise workspace.WorkspaceError("PLAN_CHANGED", "计划可信清单校验失败")
        if manifest.get("run_id") != run_id or manifest.get("context_sha256") != _hash(trusted):
            raise workspace.WorkspaceError("SCOPE_VIOLATION", "计划不属于当前固定任务")
        plan_path = _directory(paths["data"], "plans") / f"{plan_id}.json"
        plan = _read_json(plan_path, MAX_PLAN_BYTES)
        expected_id = _hash({"context_sha256": _hash(trusted), "plan": plan})[:32]
        if manifest.get("plan_id") != plan_id or expected_id != plan_id or workspace.file_sha256(plan_path) != manifest.get("sha256"):
            raise workspace.WorkspaceError("PLAN_CHANGED", "只读计划文件被改变")
        receipt_id = manifest.get("check_receipt_id")
        receipt = _load_receipt(trusted, receipt_id) if receipt_id is not None else None
        if not plan["org_ids"] and (receipt is None or not receipt["coverage"]["complete"]):
            raise workspace.WorkspaceError("PLAN_CHANGED", "零候选计划缺少完整检查回执")
        return _result(manifest, plan, deepcopy(receipt["coverage"]) if receipt else None, include_plan=True)

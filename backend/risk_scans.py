"""全辖风险扫描：冻结机构和期次，规则初筛后交 Pi 归并并归档原始证据。"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
import socket
from threading import Condition, Lock
from weakref import WeakKeyDictionary, ref

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects import postgresql, sqlite

from .analysis import AnalysisError, provider, result_diagnostics
from . import verification_layer
from .data_fetcher import data_fetcher_name, get_data_fetcher
from .metric_validator import validate_fetched_metrics
from .query_budget import _owner_is_dead
from .run_cleanup import cleanup_failed_runs, delete_analysis_run
from .models import (AnalysisRun, Indicator, Letter, LetterHistory, Organization, RiskCase,
                     SessionLocal, Snapshot, lock_analysis_run, uid)
from .risk_knowledge import DIMENSIONS, INDICATORS, get_knowledge_pack, get_risk_dimensions, load_quickbi_bindings
from .risk_letter import evaluate_letter_rules
from .services import ServiceError, freeze_context

router = APIRouter(prefix="/api", tags=["全辖风险扫描"])


class ScanInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    dimension_ids: list[str] | None = None
    focus_note: str = Field(default="", max_length=500)
    retry: bool = False
    auto_generate_reports: bool = True


class AutoReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_failed_only: bool = True


def _auto_report_limit():
    try:
        limit = int(os.getenv("RISK_AUTO_REPORT_MAX", "8"))
    except ValueError:
        limit = -1
    if limit < 0:
        raise HTTPException(422, "RISK_AUTO_REPORT_MAX 必须为非负整数")
    return limit


def select_auto_report_targets(cases):
    """用撰函口径统计有效线索；先选底线集中者，再依次降低入选门槛。"""
    limit = _auto_report_limit()
    if not limit:
        return []
    ranked = []
    for case in cases:
        rules = evaluate_letter_rules(case)
        ranked.append(dict(org_id=case.org_id, org_name=case.org_name,
                           p0_count=len(rules["mandatory_p0_clues"]), signal_count=len(rules["scan_signals"])))
    ranked.sort(key=lambda row: (-row["p0_count"], -row["signal_count"], row["org_id"]))
    heavy = [row for row in ranked if row["p0_count"] >= 3]
    if heavy:
        return heavy[:limit]
    floors = [row for row in ranked if row["p0_count"]]
    if floors:
        return floors[:min(5, limit)]
    return [row for row in ranked if row["signal_count"]][:min(3, limit)]


def _store_progress(run, rule, items, started_at=None):
    now = datetime.now(timezone.utc).isoformat()
    completed = sum(i["status"] == "completed" for i in items)
    failed = sum(i["status"] == "failed" for i in items)
    if not items:
        status = "idle"
    elif completed + failed < len(items):
        status = "running"
    else:
        status = "partial" if failed else "completed"
    progress = dict(rule=rule, items=items, total=len(items), completed=completed, failed=failed, status=status,
                    started_at=started_at or (now if items else None), updated_at=now)
    result = run.selection["risk_scan_result"]
    selection = {**run.selection, "risk_scan_result": {**result, "auto_reports": progress}}
    if status != "running":
        selection.pop("auto_report_owner", None)
    run.selection = selection
    return progress


def _plan_reports(db, run, targets, *, enabled, retry_failed_only=True):
    """合并实际函件版本，既有定稿不进入重生成队列；失败的重生成仍可补跑。"""
    limit = _auto_report_limit() if enabled else 0
    rule = "未启用自动生成" if not limit else "未命中底线或有效扫描信号"
    if targets:
        if targets[0]["p0_count"] >= 3:
            rule = f"重点高风险行社（≥3项P0底线，上限{limit}家）"
        elif targets[0]["p0_count"]:
            rule = f"重点高风险行社（≥1项P0底线，上限{min(5, limit)}家）"
        else:
            rule = f"重点高风险行社（有效扫描信号，上限{min(3, limit)}家）"
    previous = run.selection["risk_scan_result"].get("auto_reports", {})
    unfinished = {i["org_id"] for i in previous.get("items", []) if i["status"] != "completed"}
    letters = {org: (version, status) for org, version, status in db.execute(
        select(RiskCase.org_id, Letter.revision, Letter.status).join(Letter, Letter.case_id == RiskCase.id)
        .where(RiskCase.run_id == run.id))}
    items, jobs = [], []
    for target in targets:
        org = target["org_id"]
        revision, state = letters.get(org, (None, None))
        regenerate = state == "draft" and (org in unfinished or not retry_failed_only)
        queued = revision is None or regenerate
        items.append(dict(org_id=org, org_name=target["org_name"], p0_count=target["p0_count"],
                          status="queued" if queued else "completed", error=None, revision=revision))
        if queued:
            jobs.append((org, regenerate))
    progress = _store_progress(run, rule, items, None if jobs else previous.get("started_at"))
    if jobs:
        run.selection = {**run.selection, "auto_report_owner": _report_owner(db.get_bind())}
    return progress, jobs


def _lock_scan(db, run_id):
    """在新的短事务内锁住扫描结果，防止多个行社更新同一 JSON 时丢失进度。"""
    run = lock_analysis_run(db, run_id)
    if not run or run.selection.get("kind") != "risk_scan":
        raise HTTPException(404, "风险扫描任务不存在")
    return run


def _mark_report(run, org_id, status, revision=None, error=None):
    progress = run.selection["risk_scan_result"]["auto_reports"]
    items = [{**item, "status": status, "revision": revision, "error": error} if item["org_id"] == org_id
             else item for item in progress["items"]]
    _store_progress(run, progress["rule"], items, progress["started_at"])


_claims_changed = Condition()
_claims = {}
_pool_lock = Lock()
_report_pools = {}
# 每个应用引擎仅保留当前连接池的令牌；dispose 或进程重启后可识别中断的旧批次。
_owner_pools = WeakKeyDictionary()


def _report_owner(bind):
    with _pool_lock:
        entry = _owner_pools.get(bind)
        if entry is None or entry[0]() is not bind.pool:
            entry = ref(bind.pool), uid()
            _owner_pools[bind] = entry
        return {"host": socket.gethostname(), "pid": os.getpid(), "token": entry[1]}


def _batch_is_live(run, progress):
    if progress.get("status") != "running":
        return False
    owner = run.selection.get("auto_report_owner")
    if not owner or owner["host"] != socket.gethostname():
        return True
    if owner["pid"] != os.getpid():
        return not _owner_is_dead(owner["pid"])
    with _pool_lock:
        return any(token == owner["token"] and pool() is bind.pool for bind, (pool, token) in _owner_pools.items())


@contextmanager
def _report_claim(run_id, org_id, *, automatic):
    key = run_id, org_id
    with _claims_changed:
        if not automatic and key in _claims:
            message = "正在自动生成该行社风险报告，请稍候" if _claims[key] else "正在生成该行社风险报告，请稍候"
            raise HTTPException(409, message)
        _claims_changed.wait_for(lambda: key not in _claims)
        _claims[key] = automatic
    try:
        yield
    finally:
        with _claims_changed:
            del _claims[key]
            _claims_changed.notify_all()


class _ReportStorageClosed(Exception):
    """原连接池已释放，后台任务不能重新打开测试库或旧配置的归档。"""


def _report_worker(sessions, bind, pool, run_id, org_id, regenerate):
    """隔离每家行社的失败；库被拆除或任务消失时结束线程内的工作。"""
    def worker_session():
        if bind.pool is not pool:
            raise _ReportStorageClosed()
        return sessions()

    try:
        with _report_claim(run_id, org_id, automatic=True):
            with worker_session() as db, db.begin():
                _mark_report(_lock_scan(db, run_id), org_id, "running")
            _generate_report(worker_session, run_id, org_id, regenerate, automatic=True)
    except _ReportStorageClosed:
        return
    except Exception as exc:
        if isinstance(exc, HTTPException) and exc.status_code == 404:
            return
        message = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
        try:
            with worker_session() as db, db.begin():
                _mark_report(_lock_scan(db, run_id), org_id, "failed", error=message)
        except (_ReportStorageClosed, HTTPException, SQLAlchemyError):
            logging.getLogger(__name__).debug("无法写回已失效的扫描任务 %s/%s", run_id, org_id, exc_info=True)


def _enqueue_reports(sessions, bind, pool, run_id, jobs):
    if not jobs:
        return
    with _pool_lock:
        dialect = bind.dialect.name
        if dialect not in _report_pools:
            _report_pools[dialect] = ThreadPoolExecutor(max_workers=2 if dialect == "sqlite" else 4,
                                                        thread_name_prefix="risk-report")
        for org, regenerate in jobs:
            _report_pools[dialect].submit(_report_worker, sessions, bind, pool, run_id, org, regenerate)


def shutdown_auto_reports():
    """应用关闭时等待已提交的行社写回，随后释放数据库和线程池。"""
    with _pool_lock:
        pools = list(_report_pools.values())
        _report_pools.clear()
    for pool in pools:
        pool.shutdown(wait=True)


def _public(run):
    return {"id": run.id, "status": run.status, "snapshot_id": run.snapshot_id,
            "created_at": run.created_at.isoformat(), "summary": run.summary,
            "selection": {key: value for key, value in run.selection.items()
                          if key not in {"archive_metadata", "auto_report_owner"}},
            **(run.selection.get("risk_scan_result") or {})}


def _run(db, run_id):
    run = db.get(AnalysisRun, run_id)
    if not run or run.selection.get("kind") != "risk_scan":
        raise HTTPException(404, "风险扫描任务不存在")
    return run


def fetched_from_scan_archive(db, run):
    """还原本次扫描的取数包；用快照身份划分当期与历史，不能用历史填补当期缺数。"""
    result = run.selection["risk_scan_result"]
    validation = result["validation_summary"]
    context = run.selection["task_context"]
    metadata = run.selection.get("archive_metadata", {})
    catalog = {item["indicator_id"]: item for item in metadata.get("indicators", [])}
    organizations, latest, history = [], [], []
    cases = db.scalars(select(RiskCase).where(RiskCase.run_id == run.id).order_by(RiskCase.org_id))
    for case in cases:
        organizations.append({"org_id": case.org_id, "org_name": case.org_name, "region_name": case.region_name})
        for row in case.evidence:
            if row["org_id"] != case.org_id:
                raise ServiceError("SOURCE_ERROR", "归档观测所属行社与风险事项不一致")
            key = row["indicator_id"]
            if key not in catalog:
                # 早期扫描未单存来源目录，从已归档的指标和知识映射恢复。
                definition = result.get("knowledge_mapping", {}).get(key, {})
                catalog[key] = {"indicator_id": key, "indicator_name": row["indicator_name"],
                                "unit": row["unit"], "frequency": row.get("frequency") or definition.get("frequency")}
            if row["snapshot_id"] == run.snapshot_id:
                latest.append(row)
            if row["data_date"] <= context["as_of"]:
                history.append(row)
    if {o["org_id"] for o in organizations} != set(context["org_scope"]):
        raise ServiceError("SOURCE_ERROR", "扫描归档行社与冻结的可见范围不一致")

    # 旧归档中，全辖源表合计及其期次仍可由原扫描的体检结果复用。
    totals = metadata.get("source_totals")
    if totals is None:
        totals = {}
        for detail in validation["accounting_details"]:
            key = detail["indicator_id"]
            if key not in catalog:
                definition = result.get("knowledge_mapping", {}).get(key, {})
                catalog[key] = {"indicator_id": key, "indicator_name": detail["indicator_name"],
                                "unit": detail.get("unit") or definition.get("unit"),
                                "frequency": definition.get("frequency")}
            if "source_province_total" in detail:
                totals[key] = {"province": {"value": detail["source_province_total"], "usable": True,
                               "unit": detail["unit"], "data_date": detail["source_total_date"]}, "regions": {}}
            for region, value in detail.get("source_region_totals", {}).items():
                totals.setdefault(key, {"province": None, "regions": {}})["regions"][region] = {
                    "value": value, "usable": True, "unit": detail["unit"], "data_date": context["as_of"]}
    return {"snapshot_id": run.snapshot_id, "as_of": context["as_of"], "source": context["source"],
            "organizations": organizations, "indicators": list(catalog.values()), "latest": latest, "history": history,
            "missing": validation["missing_observations"], "missing_source": validation["missing_source"],
            "source_totals": totals, "visibility": validation["visibility"]}


def run_letter_verified_operator(db, run, case, operator, params, focus_current_org=True):
    """先用完整归档做核验，再裁剪展示；保留全辖覆盖、分桶和集中度的统计口径。"""
    from .risk_letter import add_verified_formal_displays

    if case.run_id != run.id:
        raise ServiceError("SCOPE_VIOLATION", "风险事项不属于本次扫描")
    if params.get("org_ids") is not None:
        raise ServiceError("INVALID_ARGUMENT", "撰函核验固定使用扫描全体行社，请移除 org_ids，通过 focus_current_org 聚焦结果")
    fetched = fetched_from_scan_archive(db, run)
    result = verification_layer.run_verified_operator(
        fetched, INDICATORS, operator, params, validation=run.selection["risk_scan_result"]["validation_summary"])
    add_verified_formal_displays(result, fetched["indicators"])
    if not focus_current_org:
        return result
    own = {row["evidence_id"] for row in case.evidence}
    result["evidence_ids"] = [ref for ref in result["evidence_ids"] if ref in own]
    if operator == "bucket_distribution":
        for row in result["rows"]:
            row["evidence_ids"] = [ref for ref in row["evidence_ids"] if ref in own]
            if "org_ids" in row:
                row["org_ids"] = [org for org in row["org_ids"] if org == case.org_id]
    elif operator == "concentration_cr":
        for row in result["rows"]:
            row["top_orgs"] = [org for org in row["top_orgs"] if org["org_id"] == case.org_id]
    else:
        result["rows"] = [row for row in result["rows"] if row["org_id"] == case.org_id]
    return result


@router.get("/risk-dimensions")
def dimensions():
    with SessionLocal() as db:
        return scan_dimensions(db.scalars(select(Indicator.id)))


@router.delete("/risk-scans/{run_id}")
def delete_scan(run_id: str):
    with SessionLocal.begin() as db:
        return delete_analysis_run(db, run_id, expected_kind="risk_scan")


@router.post("/risk-scans/cleanup-failed")
def cleanup_failed():
    return cleanup_failed_runs(SessionLocal)


def scan_dimensions(local_ids):
    if data_fetcher_name() != "quickbi":
        return get_risk_dimensions(local_ids)
    try:
        return get_risk_dimensions(load_quickbi_bindings())
    except (ValueError, OSError) as exc:
        raise HTTPException(422, f"QuickBI 指标字典配置无效：{exc}") from exc


def _insert_archive_rows(db, model, rows):
    """同日并行任务可以共享本地目录与日期外键，不能覆盖已有归档。"""
    insert = sqlite.insert if db.get_bind().dialect.name == "sqlite" else postgresql.insert
    db.execute(insert(model).values(rows).on_conflict_do_nothing(index_elements=[model.id]))


def _quickbi_snapshot(db, requested):
    snapshot = db.get(Snapshot, requested)
    if snapshot and snapshot.status == "ready":
        day = snapshot.data_date
    else:
        try:
            day = date.fromisoformat(requested)
        except ValueError:
            raise HTTPException(422, "请选择有效的 QuickBI 业务日期") from None
    ident = f"quickbi:{day.isoformat()}"
    _insert_archive_rows(db, Snapshot, [dict(id=ident, data_date=day, name=f"QuickBI {day}",
                                             source="quickbi", is_demo=False, status="ready")])
    return db.get(Snapshot, ident)


def _archive_visible_orgs(db, directory, organizations):
    """给本次可见行社及其上级登记归档外键，保持内网实际机构号。"""
    members = {o["org_id"] for o in organizations}
    regions = {directory[org]["parent_id"] for org in members}
    for level, ids in ((1, {"999999"}), (2, regions), (3, members)):
        if ids:
            rows = [dict(id=code, code=code, name=directory[code]["org_name"], level=level,
                         parent_id=directory[code]["parent_id"],
                         region_id=directory[code]["parent_id"] if level == 3 else None) for code in sorted(ids)]
            _insert_archive_rows(db, Organization, rows)


def _matches(row, rule):
    if not row.get("usable") or row.get("value") is None:
        return False
    kind, op = rule.get("type"), rule.get("op")
    if kind == "rank":
        return (row.get("population") == 82 and row.get("rank_scope") == "province_societies"
                and row.get("rank") is not None and row["rank"] >= int(rule.get("value", 72)))
    value = Decimal(str(row["value"]))
    target = Decimal(str(rule.get("value", 0)))
    return {">": lambda: value > target, "<": lambda: value < target,
            ">=": lambda: value >= target, "!=": lambda: value != target}.get(op, lambda: False)()


def screen(fetched, definitions, dimension_ids, validation):
    """只对可用的当期真实值执行参考条件，按行社和风险方面组织信号。"""
    by_org = defaultdict(list)
    latest = defaultdict(dict)
    bad = {(r["org_id"], r["indicator_id"]) for r in validation["unit_mismatches"]}
    for row in fetched["latest"]:
        if (row["org_id"], row["indicator_id"]) in bad:
            row = {**row, "usable": False, "issue": "业务定义与来源单位不一致"}
        by_org[row["org_id"]].append(row)
        latest[row["org_id"]][row["indicator_id"]] = row
    history = defaultdict(list)
    current_ids = {row["evidence_id"] for row in fetched["latest"]}
    for row in fetched["history"]:
        if row["evidence_id"] not in current_ids:
            history[row["org_id"]].append(row)
    candidates = []
    for org in fetched["organizations"]:
        org_id = org["org_id"]
        signals = []
        for row in by_org[org_id]:
            definition = definitions.get(row["indicator_id"], {})
            for dimension in set(definition.get("dimensions", [])) & set(dimension_ids):
                role = definition.get("dimension_roles", {}).get(dimension, definition.get("role"))
                if role != "direct_judge":
                    continue
                for rule in definition.get("reference_rules", []):
                    if _matches(row, rule):
                        signals.append({"evidence_id": row["evidence_id"], "indicator_id": row["indicator_id"],
                                        "dimension_id": dimension, "aspect": definition.get("aspect", "风险变化"),
                                        "role": role, "reason": rule["label"]})
        # 两个现成增量都为正才提示双升，分别保留原始证据。
        a, b = latest[org_id].get("TEST_METRIC_024"), latest[org_id].get("TEST_METRIC_028")
        if "credit_risk" in dimension_ids and a and b and all(r.get("usable") and r.get("value") is not None and Decimal(r["value"]) > 0 for r in (a, b)):
            for row in (a, b):
                signals.append({"evidence_id": row["evidence_id"], "indicator_id": row["indicator_id"],
                                "dimension_id": "credit_risk", "aspect": "不良贷款余额与不良率双升、新形成不良增加",
                                "role": "direct_judge", "reason": "不良余额和不良率增量均为正（内置参考筛查口径）"})
        if signals:
            # 候选包携带触发指标和关联背景的代表期次；完整观测另存档。
            recent = defaultdict(list)
            for row in history[org_id]:
                if row.get("usable") and (org_id, row["indicator_id"]) not in bad:
                    recent[row["indicator_id"]].append(row)
            triggered = {s["evidence_id"] for s in signals}
            trigger_metrics = {s["indicator_id"] for s in signals}
            current = [r for r in by_org[org_id] if r.get("usable")]
            evidence = [r for r in current if r["evidence_id"] in triggered]
            related = [r for r in current if r["evidence_id"] not in triggered and
                       any(definitions.get(r["indicator_id"], {}).get("dimension_roles", {}).get(d) in
                           {"explain_change", "context", "impact"} for d in dimension_ids)]
            evidence += [{**r, "role": "context"} for r in related[:10]]
            for metric in trigger_metrics:
                evidence += sorted(recent[metric], key=lambda x: x["data_date"], reverse=True)[:2]
            evidence = list({r["evidence_id"]: r for r in evidence}.values())
            candidates.append({**org, "signals": signals, "evidence": evidence})
    return candidates, by_org, history


@router.post("/risk-scans")
def create_scan(data: ScanInput):
    if data.auto_generate_reports:
        _auto_report_limit()
    external = data_fetcher_name() == "quickbi"
    try:
        fetcher = get_data_fetcher()
    except (ValueError, OSError) as exc:
        raise HTTPException(422, f"取数器配置错误：{exc}") from exc
    with SessionLocal() as db:
        snapshot = _quickbi_snapshot(db, data.snapshot_id) if external else db.get(Snapshot, data.snapshot_id)
        if not snapshot or snapshot.status != "ready":
            raise HTTPException(422, "请选择可用快照")
        scope = ([org["org_id"] for org in fetcher.organizations] if external else
                 sorted(db.scalars(select(Organization.id).where(Organization.level == 3))))
        if not external and len(scope) != 82:
            raise HTTPException(422, "风险扫描要求机构目录包含全部82家三级行社")
        available = set(fetcher.bindings) if external else set(db.scalars(select(Indicator.id)))
        hints = get_risk_dimensions(available)
        selected = data.dimension_ids if data.dimension_ids is not None else [d["id"] for d in hints if d["executable"]]
        if not selected or len(selected) != len(set(selected)) or any(key not in DIMENSIONS for key in selected):
            raise HTTPException(422, "请选择有效且不重复的风险面")
        selected = sorted(selected)
        topic = " ".join(data.focus_note.split())
        key = hashlib.sha256(json.dumps({"kind": "risk_scan_v1", "snapshot": snapshot.id, "dimensions": selected,
                                         "topic": topic, "scope": scope, "agent": provider.cache_identity,
                                         "knowledge": get_knowledge_pack(selected)},
                                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        # QuickBI 同一业务日期可修订，身份的可见范围也能变化；每次请求均重新取证。
        if external:
            key = uid()
        run = db.scalar(select(AnalysisRun).where(AnalysisRun.run_key == key))
        if run:
            if run.status != "failed" or not data.retry:
                return _public(run)
            claim = db.execute(update(AnalysisRun).where(AnalysisRun.id == run.id, AnalysisRun.status == "failed").values(status="running"))
            if claim.rowcount != 1:
                db.rollback()
                db.refresh(run)
                return _public(run)
            context = run.selection["task_context"]
            run.selection = {**run.selection, "error": None, "attempt": run.selection.get("attempt", 1) + 1}
        else:
            run_id = uid()
            context = freeze_context(db, run_id, snapshot, topic, [], scope)
            if external:
                context["history_dates"] = [day.isoformat() for day in db.scalars(select(Snapshot.data_date)
                    .where(Snapshot.id.in_(context["history_snapshot_ids"])).distinct().order_by(Snapshot.data_date))]
                # 只有源表返回的机构才是本次已确认的权限范围。
                context["org_scope"] = []
            run = AnalysisRun(id=run_id, run_key=key, snapshot_id=snapshot.id, status="running", summary={},
                              rule_snapshot=[], selection={"kind": "risk_scan", "task_context": context,
                              "dimension_ids": selected, "focus_note": topic, "attempt": 1, "mode": provider.mode})
            db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            winner = db.scalar(select(AnalysisRun).where(AnalysisRun.run_key == key))
            if winner:
                return _public(winner)
            raise
        run_id, demo = run.id, snapshot.is_demo
    diagnostics = None
    try:
        mapping = {k: v for k, v in INDICATORS.items() if set(v.get("dimensions", [])) & set(selected)
                   and (not v.get("optional_demo_indicator") or k in available)}
        fetched = fetcher.fetch({**context, "org_scope": scope}, mapping.keys())
        if external:
            context = {**context, "org_scope": [org["org_id"] for org in fetched["organizations"]]}
            mapping = {key: {**item, "source_status": "available" if key in available else "missing_source"}
                       for key, item in mapping.items()}
        validation = validate_fetched_metrics(fetched, mapping)
        candidates, by_org, history = screen(fetched, mapping, selected, validation)
        knowledge = get_knowledge_pack(selected)
        # 模型每批只需要筛查口径与风险映射，不重复传输完整业务表长字段。
        prompt = json.dumps({"dimensions": knowledge["dimensions"], "instructions": knowledge["instructions"],
                             "indicators": [{k: item.get(k) for k in
                                             ("indicator_id", "name", "metric_kind", "dimension_roles", "aspect", "reference_rules")}
                                            for item in knowledge["indicators"]],
                             "risk_mapping": [{k: item.get(k) for k in
                                               ("point", "aspect", "letter_indicator", "indicator_ids", "risk_problem")}
                                              for item in knowledge["risk_mapping"]]}, ensure_ascii=False)
        if topic:
            prompt += "\n本次用户补充关注：" + topic
        model_validation = {key: validation[key] for key in ("total_org_count", "covered_org_count",
                            "available_indicator_count", "missing_source", "missing_count", "stale_count",
                            "unit_mismatch_count", "reminders")}
        if external:
            model_validation["visibility"] = fetched["visibility"]
        synthesis = provider.synthesize_risks(prompt, selected, model_validation, candidates)
        diagnostics = result_diagnostics(synthesis)
        issues = defaultdict(list)
        for issue in synthesis["issues"]:
            issues[issue["org_id"]].append(issue)
        candidate_by_org = {c["org_id"]: c for c in candidates}
        with SessionLocal() as db:
            run = _run(db, run_id)
            if external:
                _archive_visible_orgs(db, fetcher.org_directory, fetched["organizations"])
            results, cases = [], []
            direct_ids = {key for key, definition in mapping.items() if any(
                definition.get("dimension_roles", {}).get(d) == "direct_judge" for d in selected)}
            present_ids = {item["indicator_id"] for item in fetched["indicators"]}
            direct_present = direct_ids & present_ids
            for org in fetched["organizations"]:
                org_id = org["org_id"]
                org_issues = issues[org_id]
                candidate = candidate_by_org.get(org_id)
                usable_direct = {r["indicator_id"] for r in by_org[org_id] if r.get("usable") and r["indicator_id"] in direct_present}
                incomplete = bool(direct_present - usable_direct) or not direct_present or bool(direct_ids - present_ids)
                status = "risk" if org_issues else ("insufficient" if incomplete else "no_signal")
                evidence = by_org[org_id] + history[org_id]
                case = RiskCase(run_id=run_id, org_id=org_id, org_name=org["org_name"], region_name=org["region_name"],
                                data_date=snapshot.data_date, risk_types=sorted({i["dimension_id"] for i in org_issues}),
                                status="open" if org_issues else status,
                                headline=("；".join(i["title"] for i in org_issues) if org_issues else
                                          "数据不足或待内网取数接入" if incomplete else "本次未命中参考筛查条件"),
                                evidence=evidence, analysis={"issues": org_issues, "snapshot_id": run.snapshot_id,
                                "signals": candidate["signals"] if candidate else [],
                                "checks": [i["check_suggestion"] for i in org_issues]})
                db.add(case)
                db.flush()
                cases.append(case)
                case_id = case.id
                results.append({**org, "case_id": case_id, "status": status,
                                "data_incomplete": incomplete,
                                "issues": org_issues, "signals": candidate["signals"] if candidate else []})
            result = {"knowledge_mapping": mapping, "risk_dimensions": hints, "validation_summary": validation,
                      "executive_summary": synthesis["executive_summary"], "society_results": results,
                      "trace": synthesis.get("trace", []), "runtime": synthesis.get("runtime", {}),
                      "mode": synthesis.get("mode"), "is_demo": demo}
            run.selection = {**run.selection, "task_context": context, "risk_scan_result": result,
                             "archive_metadata": {"indicators": fetched["indicators"],
                                                  "source_totals": fetched["source_totals"]}}
            run.summary = {"case_count": sum(bool(r["issues"]) for r in results), "org_count": len(results),
                           "triggered": sum(len(r["signals"]) for r in results)}
            run.status = "completed"
            targets = select_auto_report_targets(cases) if data.auto_generate_reports else []
            _, jobs = _plan_reports(db, run, targets, enabled=data.auto_generate_reports)
            bind = db.get_bind()
            pool = bind.pool
            db.commit()
            response = _public(run)
    except Exception as exc:
        with SessionLocal() as db:
            run = _run(db, run_id)
            run.status = "failed"
            run.selection = {**run.selection, "error": str(exc), "error_type": type(exc).__name__,
                             "diagnostics": exc.diagnostics if isinstance(exc, AnalysisError) else diagnostics}
            db.commit()
        raise HTTPException(502, {"run_id": run_id, "message": str(exc)}) from exc
    _enqueue_reports(SessionLocal, bind, pool, run_id, jobs)
    return response


@router.get("/risk-scans")
def list_scans():
    with SessionLocal() as db:
        return [_public(r) for r in db.scalars(select(AnalysisRun).where(
            AnalysisRun.selection["kind"].as_string() == "risk_scan").order_by(AnalysisRun.created_at.desc()).limit(100))]


@router.get("/risk-scans/{run_id}")
def get_scan(run_id: str):
    with SessionLocal() as db:
        return _public(_run(db, run_id))


@router.post("/risk-scans/{run_id}/societies/{org_id}/draft")
def draft_scan(run_id: str, org_id: str, regenerate: bool = False):
    with _report_claim(run_id, org_id, automatic=False):
        return _generate_report(SessionLocal, run_id, org_id, regenerate)


@router.post("/risk-scans/{run_id}/auto-reports")
def start_auto_reports(run_id: str, data: AutoReportInput | None = None):
    sessions = SessionLocal
    with sessions() as db:
        run = _run(db, run_id)
        if run.status != "completed":
            raise HTTPException(409, "扫描尚未完成")
        current = run.selection["risk_scan_result"].get("auto_reports", {})
        if _batch_is_live(run, current):
            return current
        targets = select_auto_report_targets(db.scalars(select(RiskCase).where(RiskCase.run_id == run_id)))
    with sessions.begin() as db:
        run = _lock_scan(db, run_id)
        current = run.selection["risk_scan_result"].get("auto_reports", {})
        if _batch_is_live(run, current):
            return current
        progress, jobs = _plan_reports(db, run, targets, enabled=True,
                                       retry_failed_only=data.retry_failed_only if data else True)
        bind = db.get_bind()
        pool = bind.pool
    _enqueue_reports(sessions, bind, pool, run_id, jobs)
    return progress


def _generate_report(sessions, run_id, org_id, regenerate, *, automatic=False):
    from .risk_letter import compose_risk_letter_with_trace
    from .app import hash_json, letter_dict
    with sessions() as db:
        run = _run(db, run_id)
        if run.status != "completed":
            raise HTTPException(409, "扫描尚未完成")
        progress = run.selection["risk_scan_result"].get("auto_reports", {})
        if (not automatic and _batch_is_live(run, progress)
                and any(i["org_id"] == org_id and i["status"] in {"queued", "running"} for i in progress["items"])):
            raise HTTPException(409, "正在自动生成该行社风险报告，请稍候")
        case = db.scalar(select(RiskCase).where(RiskCase.run_id == run_id, RiskCase.org_id == org_id))
        if not case:
            raise HTTPException(404, "该行社没有生成风险事项")
        letter = db.scalar(select(Letter).where(Letter.case_id == case.id))
        existing = {**letter_dict(letter), "diagnostics": case.analysis.get("letter_diagnostics")} if letter else None
        if existing and not regenerate and not automatic:
            return existing
        if letter and regenerate and letter.status != "draft":
            raise HTTPException(409, "仅可重新生成草稿状态的函件")
        # 模型通过 MCP 独立取证；复制旧归档的快照身份后释放读连接。
        db.expunge(case)
        case.analysis = {"snapshot_id": run.snapshot_id, **case.analysis}
    if existing and not regenerate:
        with sessions() as db, db.begin():
            _mark_report(_lock_scan(db, run_id), org_id, "completed", existing["revision"])
        return existing
    clues = evaluate_letter_rules(case)
    if not (clues["mandatory_p0_clues"] or clues["scan_signals"]):
        raise HTTPException(409, "该行社本次没有触发撰函的底线或有效扫描信号")
    try:
        content, diagnostics = compose_risk_letter_with_trace(case)
    except AnalysisError as exc:
        raise HTTPException(502, str(exc)) from exc
    evidence_hash = hash_json({"triggered": case.evidence, "queried": []})
    with sessions() as db, db.begin():
        run = _lock_scan(db, run_id)
        stored_case = db.get(RiskCase, case.id)
        letter = db.scalar(select(Letter).where(Letter.case_id == case.id).with_for_update())
        if existing:
            if not letter or letter.status != "draft" or letter.revision != existing["revision"]:
                raise HTTPException(409, "函件状态或版本已变化，请刷新后重试")
            letter.content, letter.evidence_hash = content, evidence_hash
            letter.revision += 1
        elif letter:
            if regenerate:
                raise HTTPException(409, "草稿版本冲突，请刷新后重试")
            if automatic:
                _mark_report(run, org_id, "completed", letter.revision)
            return {**letter_dict(letter), "diagnostics": stored_case.analysis.get("letter_diagnostics")}
        else:
            letter = Letter(case_id=case.id, content=content, evidence_hash=evidence_hash, revision=1, status="draft")
            db.add(letter)
        db.flush()
        db.add(LetterHistory(letter_id=letter.id, revision=letter.revision, content=content, evidence_hash=evidence_hash))
        stored_case.status = "draft"
        record = {**diagnostics, "revision": letter.revision, "org_id": org_id,
                  "org_name": case.org_name, "updated_at": datetime.now(timezone.utc).isoformat()}
        stored_case.analysis = {**stored_case.analysis, "letter_diagnostics": record}
        result = run.selection["risk_scan_result"]
        drafts = {**result.get("letter_drafts", {}), org_id: record}
        run.selection = {**run.selection, "risk_scan_result": {**result, "letter_drafts": drafts}}
        if automatic:
            _mark_report(run, org_id, "completed", letter.revision)
        return {**letter_dict(letter), "diagnostics": record}

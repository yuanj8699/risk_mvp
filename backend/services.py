"""API与MCP共享的只读业务服务：固定任务、查询事实、检测规则和读取模板。

应用数据库保存导入/合成快照。本轮不读取RISK_SOURCE_DATABASE_URL，也不猜外部表结构。
只有freeze_context使用调用方已有事务；其他服务打开短只读会话，不创建表或种子数据。
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import select, func, case
from sqlalchemy.exc import SQLAlchemyError

from .models import AnalysisRun, Indicator, Observation, Organization, Rule, SessionLocal, Snapshot


class ServiceError(ValueError):
    """稳定错误码供API和MCP映射；数据库细节不暴露给模型。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@contextmanager
def read_session():
    try:
        with SessionLocal() as db:
            yield db
    except SQLAlchemyError as exc:
        raise ServiceError("SOURCE_ERROR", "应用指标快照暂不可读取，请检查数据库连接与初始化状态") from exc


def number(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def display(value: Decimal | None, unit: str) -> str:
    if value is None:
        return "缺失"
    val = Decimal(str(value))
    if unit == "%":
        return f"{val:.4f}".rstrip("0").rstrip(".") + "%"
    if unit == "元" and abs(val) >= Decimal("10000"):
        return f"{val / Decimal('10000'):,.2f}万元"
    return f"{val:,.2f}{unit}"


def observation_issue(obs, metric, snapshot) -> str | None:
    """当前值质量口径；历史查询另区分历史期次与当前新鲜度。"""
    if obs is None or obs.value is None or obs.quality_status != "valid":
        return "指标数据缺失或质量状态不合格"
    lag = (snapshot.data_date - obs.data_date).days
    allowed_lag = 120 if metric.frequency == "季" else 7
    if lag < 0 or lag > allowed_lag:
        return f"数据期次不满足测试新鲜度策略（{metric.frequency}频指标允许滞后{allowed_lag}天）"
    if obs.unit != metric.unit:
        return "指标单位与目录不一致"
    return None


def evaluate(rule, obs, metric, snapshot) -> tuple[str, str]:
    if rule.kind == "ratio" and rule.params.get("threshold") is None:
        return "unconfigured", "占比阈值尚未配置"
    issue = observation_issue(obs, metric, snapshot)
    if issue:
        return "insufficient", issue
    if rule.kind == "rank":
        if obs.rank is None or obs.population != 82 or obs.rank_scope != "province_societies" or not 1 <= obs.rank <= 82:
            return "insufficient", "缺少有效的全省82家行社同口径排名"
        cutoff = rule.params["cutoff"]
        hit = obs.rank >= cutoff
        return ("triggered" if hit else "normal"), f"全省排名{obs.rank}/{obs.population}，{'达到' if hit else '未达到'}排名≥{cutoff}的测试规则"
    if rule.kind == "ratio":
        threshold = Decimal(str(rule.params["threshold"]))
        hit = obs.value >= threshold if rule.params["op"] == ">=" else obs.value > threshold
        return ("triggered" if hit else "normal"), f"{'满足' if hit else '未满足'}占比{rule.params['op']}{number(threshold)}%的测试规则"
    hit = obs.value != 0
    return ("triggered" if hit else "normal"), "有效值非零，触发存在提醒；不据此认定风险上升" if hit else "有效值为零，未触发存在提醒"


def observation_record(obs: Observation, metric: Indicator) -> dict:
    """同一数据库观测重复取证仍使用同一身份；id保留旧页面兼容。"""
    return {
        "evidence_id": obs.id, "id": obs.id, "org_id": obs.org_id,
        "indicator_id": metric.id, "indicator_name": metric.name,
        "value": number(obs.value), "value_display": display(obs.value, obs.unit), "unit": obs.unit,
        "data_date": obs.data_date.isoformat(), "snapshot_id": obs.snapshot_id,
        "source_record_id": obs.id, "quality_status": obs.quality_status,
        "rank": obs.rank, "population": obs.population, "rank_scope": obs.rank_scope,
    }


def evidence_record(rule, obs: Observation, metric: Indicator, reason: str) -> dict:
    return {
        **observation_record(obs, metric), "rule_id": rule.id, "rule_name": rule.name,
        "rule_kind": rule.kind, "rule_version": rule.version, "reason": reason,
    }


def freeze_context(db, run_id: str, snapshot: Snapshot, topic: str, rule_snapshot: list, org_scope: list[str]) -> dict:
    """在创建任务的短事务中固定历史快照集合，后续新导入不能改变本次取证范围。"""
    if snapshot.status != "ready":
        raise ServiceError("INVALID_ARGUMENT", "数据快照尚未就绪")
    ids = db.scalars(select(Snapshot.id).where(
        Snapshot.source == snapshot.source, Snapshot.status == "ready", Snapshot.data_date <= snapshot.data_date,
    ).order_by(Snapshot.data_date, Snapshot.created_at, Snapshot.id)).all()
    if snapshot.id not in ids:
        ids.append(snapshot.id)
    return {
        "run_id": run_id, "topic": topic, "snapshot_id": snapshot.id, "as_of": snapshot.data_date.isoformat(),
        "rule_snapshot": deepcopy(rule_snapshot), "org_scope": list(org_scope),
        "history_snapshot_ids": list(ids), "source": snapshot.source,
    }


def load_task_context(run_id: str, org_id: str | None = None) -> dict:
    """MCP只能加载宿主指定的已提交任务，单行社会话不能扩大机构范围。"""
    with read_session() as db:
        run = db.get(AnalysisRun, run_id)
        context = (run.selection or {}).get("task_context") if run else None
        if not context:
            raise ServiceError("SOURCE_ERROR", "指定任务尚未提交或未保存固定任务上下文")
        context = deepcopy(context)
        if org_id is not None:
            if org_id not in context["org_scope"]:
                raise ServiceError("SCOPE_VIOLATION", "当前行社不属于任务授权范围")
            context["org_scope"] = [org_id]
        return context


def _page(limit: int, offset: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
        raise ServiceError("INVALID_ARGUMENT", "limit须为1至1000的整数，offset须为非负整数")


def search_indicators(query: str = "", limit: int = 50, offset: int = 0) -> dict:
    """目录检索不修改规则；返回的排名信息仅是目录提示，检测使用任务冻结规则。"""
    _page(limit, offset)
    if not isinstance(query, str) or len(query) > 500:
        raise ServiceError("INVALID_ARGUMENT", "query须为500字以内文本")
    with read_session() as db:
        rank_rules = {r.indicator_id: r for r in db.scalars(select(Rule).where(Rule.kind == "rank"))}
        terms = query.lower().split()
        items = []
        for metric in db.scalars(select(Indicator).order_by(Indicator.id)):
            searchable = (metric.id + metric.name + metric.description).lower()
            if terms and not any(term in searchable for term in terms):
                continue
            rule = rank_rules.get(metric.id)
            items.append({
                "id": metric.id, "name": metric.name, "description": metric.description,
                "definition": metric.description, "unit": metric.unit, "frequency": metric.frequency,
                "ranking": ({"scope": rule.params.get("scope"), "population": rule.params.get("population"),
                             "direction": rule.params.get("rank_direction")} if rule else None),
            })
        return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < len(items) else None}


def _scope(context: dict, org_ids: list[str] | None) -> list[str]:
    chosen = context["org_scope"] if org_ids is None else org_ids
    if not isinstance(chosen, list) or not chosen or any(not isinstance(x, str) for x in chosen):
        raise ServiceError("INVALID_ARGUMENT", "org_ids须为非空机构ID列表")
    if set(chosen) - set(context["org_scope"]):
        raise ServiceError("SCOPE_VIOLATION", "查询机构超出当前任务授权范围")
    return sorted(set(chosen))


def _snapshots(db, context: dict) -> dict:
    as_of = date.fromisoformat(context["as_of"])
    snapshots = {s.id: s for s in db.scalars(select(Snapshot).where(
        Snapshot.id.in_(context["history_snapshot_ids"]), Snapshot.source == context["source"],
        Snapshot.status == "ready", Snapshot.data_date <= as_of,
    ))}
    if context["snapshot_id"] not in snapshots or set(snapshots) != set(context["history_snapshot_ids"]):
        raise ServiceError("SOURCE_ERROR", "任务固定快照已不可读取，不能以不完整历史继续分析")
    return snapshots


def iter_metrics(context: dict, indicator_ids: list[str], org_ids: list[str] | None = None,
                 history: bool = True):
    """按固定任务流式读取去重观测，供大数据导出复用；不会按分页反复加载全集。

    去重/截至日范围在SQL中完成。连接在迭代结束时关闭，原始值仍为十进制字符串。
    """
    if not isinstance(indicator_ids, list) or not indicator_ids or len(indicator_ids) > 1000 or any(not isinstance(x, str) for x in indicator_ids):
        raise ServiceError("INVALID_ARGUMENT", "indicator_ids须为非空指标ID列表，最多1000项")
    if not isinstance(history, bool):
        raise ServiceError("INVALID_ARGUMENT", "history须为布尔值")
    selected_orgs = _scope(context, org_ids)
    indicator_ids = sorted(set(indicator_ids))
    as_of = date.fromisoformat(context["as_of"])
    with read_session() as db:
        metrics = {m.id: m for m in db.scalars(select(Indicator).where(Indicator.id.in_(indicator_ids)))}
        unknown = set(indicator_ids) - set(metrics)
        if unknown:
            raise ServiceError("UNKNOWN_INDICATOR", "未知指标ID：" + "、".join(sorted(unknown)))
        snapshots = _snapshots(db, context)
        priority = (case((Observation.snapshot_id == context["snapshot_id"], 1), else_=0).desc(),
                    Snapshot.data_date.desc(), Snapshot.created_at.desc(), Snapshot.id.desc(), Observation.id.desc())
        ranked = select(Observation.id.label("id"), func.row_number().over(
            partition_by=(Observation.org_id, Observation.indicator_id, Observation.data_date),
            order_by=priority,
        ).label("same_period")).join(Snapshot, Snapshot.id == Observation.snapshot_id).where(
            Observation.snapshot_id.in_(snapshots), Observation.org_id.in_(selected_orgs),
            Observation.indicator_id.in_(indicator_ids), Observation.data_date <= as_of,
        ).subquery()
        unique_ids = select(ranked.c.id).where(ranked.c.same_period == 1)
        if history:
            query = select(Observation).where(Observation.id.in_(unique_ids)).order_by(
                Observation.data_date, Observation.org_id, Observation.indicator_id)
        else:
            latest = select(Observation.id.label("id"), func.row_number().over(
                partition_by=(Observation.org_id, Observation.indicator_id),
                order_by=Observation.data_date.desc(),
            ).label("latest_period")).where(Observation.id.in_(unique_ids)).subquery()
            query = select(Observation).where(Observation.id.in_(
                select(latest.c.id).where(latest.c.latest_period == 1)
            )).order_by(Observation.org_id, Observation.indicator_id)
        for obs in db.scalars(query.execution_options(yield_per=1000)):
            metric = metrics[obs.indicator_id]
            current_issue = observation_issue(obs, metric, SimpleNamespace(data_date=as_of))
            historical_issue = observation_issue(obs, metric, SimpleNamespace(data_date=obs.data_date))
            issue = historical_issue if history else current_issue
            yield {**observation_record(obs, metric), "source": context["source"],
                   "usable": issue is None, "issue": issue, "current_issue": current_issue}


def query_metrics(context: dict, indicator_ids: list[str], org_ids: list[str] | None = None,
                  history: bool = False, limit: int = 100, offset: int = 0) -> dict:
    """小结果供MCP直接查看；大数据导出使用iter_metrics流式落盘，不进入模型上下文。"""
    _page(limit, offset)
    evidence = list(iter_metrics(context, indicator_ids, org_ids, history))
    selected_orgs = _scope(context, org_ids)
    indicator_ids = sorted(set(indicator_ids))
    found = {(e["org_id"], e["indicator_id"]) for e in evidence}
    missing = [{"org_id": org, "indicator_id": metric, "status": "missing"}
               for org in selected_orgs for metric in indicator_ids if (org, metric) not in found]
    total = len(evidence)
    return {
        "evidence": evidence[offset:offset + limit], "total": total, "offset": offset, "limit": limit,
        "next_offset": offset + limit if offset + limit < total else None,
        "coverage": {"requested_pairs": len(selected_orgs) * len(indicator_ids), "found_pairs": len(found),
                     "usable_records": sum(e["usable"] for e in evidence), "missing": missing},
        "as_of": context["as_of"], "history": history, "source": context["source"],
    }


def check_risks(context: dict) -> dict:
    """纯检测本次选定快照，不创建任务、不调Agent、不用历史值填补本批次缺数。"""
    with read_session() as db:
        snapshot = _snapshots(db, context)[context["snapshot_id"]]
        orgs = db.scalars(select(Organization).where(Organization.id.in_(_scope(context, None)), Organization.level == 3).order_by(Organization.code)).all()
        metrics = {m.id: m for m in db.scalars(select(Indicator))}
        regions = {o.id: o.name for o in db.scalars(select(Organization).where(Organization.level == 2))}
        observations = {(o.org_id, o.indicator_id): o for o in db.scalars(select(Observation).where(Observation.snapshot_id == snapshot.id))}
        rules = [SimpleNamespace(**r) for r in context["rule_snapshot"] if r["enabled"]]
        counts = {k: 0 for k in ("evaluated", "triggered", "normal", "insufficient", "not_applicable", "unconfigured", "case_count")}
        checks, cases = [], []
        for org in orgs:
            evidence = []
            for rule in rules:
                metric = metrics.get(rule.indicator_id)
                if metric is None:
                    raise ServiceError("SOURCE_ERROR", "冻结规则引用的指标已不可读取")
                obs = observations.get((org.id, rule.indicator_id))
                status, reason = evaluate(rule, obs, metric, snapshot)
                counts[status] += 1
                counts["evaluated"] += status in {"normal", "triggered"}
                checks.append({"org_id": org.id, "org_name": org.name, "rule_id": rule.id,
                               "indicator_id": metric.id, "status": status, "reason": reason})
                if status == "triggered":
                    evidence.append({**evidence_record(rule, obs, metric, reason), "source": context["source"]})
            if evidence:
                cases.append({"case_key": org.id, "org_name": org.name,
                              "region_name": regions.get(org.region_id, "未分配地区"), "evidence": evidence})
        counts["case_count"] = len(cases)
        return {"summary": counts, "checks": checks, "cases": cases}


def get_letter_template(version: str = "test-v1") -> dict:
    if version != "test-v1":
        raise ServiceError("INVALID_ARGUMENT", "仅支持test-v1模板")
    return {
        "version": version, "fields": ["org_name", "data_date", "facts", "checks", "supplementary"],
        "template": (
            "风险提示函（本地测试通用模板）\n\n致：{org_name}\n\n"
            "根据截至{data_date}纳入本批次的数据，以下指标触发当前测试规则：\n\n{facts}\n\n"
            "{supplementary}请核查以下事项：\n{checks}\n\n"
            "上述内容为指标事实与规则筛选结果，具体原因和责任尚待核查。请按正式业务流程补充核查说明。\n\n"
            "模板状态：行内正式模板尚未提供。\n环境：本地测试；模拟下发不构成真实发函。"
        ),
    }

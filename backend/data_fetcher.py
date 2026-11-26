"""风险扫描取数边界：固定全体三级机构、快照和历史范围，不用历史值填补当前缺数。

QuickBI/小Q 适配器实现同一 fetch 协议，返回 organizations、indicators、latest、
history、missing、missing_source；观测须包含 evidence_id、机构与地区名称、指标、
原始十进制字符串及展示值、单位、实际日期、排名、总体数、质量状态和快照 ID。
外部适配器必须保留真实来源标识与日期，不得对缺数造零或伪造证据。
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import date
import os
from types import SimpleNamespace
from typing import Protocol

from sqlalchemy import select

from .models import Indicator, Observation, Organization, SessionLocal, Snapshot
from .services import ServiceError, _snapshots, observation_issue, observation_record


class DataFetcher(Protocol):
    """接收 freeze_context 的冻结字典与指标 ID，返回标准快照及历史事实。"""

    def fetch(self, context: dict, indicator_ids: Iterable[str]) -> dict: ...


class LocalSnapshotFetcher:
    """应用库实现，所有期次均限定在任务创建时冻结的快照集合中。"""

    def fetch(self, context: dict, indicator_ids: Iterable[str]) -> dict:
        return self._fetch(context, indicator_ids, require_full_scope=True)

    def fetch_scoped(self, context: dict, indicator_ids: Iterable[str]) -> dict:
        """按已提交任务的原机构范围核验，局部范围须标记为裁剪。"""
        return self._fetch(context, indicator_ids, require_full_scope=False)

    def _fetch(self, context: dict, indicator_ids: Iterable[str], *, require_full_scope: bool) -> dict:
        requested = sorted(set(indicator_ids))
        as_of = date.fromisoformat(context["as_of"])
        with SessionLocal() as db:
            org_rows = list(db.scalars(select(Organization).where(Organization.level == 3).order_by(Organization.code)))
            directory_ids = {row.id for row in org_rows}
            org_ids = set(context["org_scope"])
            if require_full_scope and (len(directory_ids) != 82 or len(context["org_scope"]) != 82 or org_ids != directory_ids):
                raise ServiceError("SCOPE_VIOLATION", "风险扫描必须覆盖全部82家三级行社")
            if not org_ids or not org_ids <= directory_ids:
                raise ServiceError("SCOPE_VIOLATION", "固定任务的机构范围不完整或已不可读取")
            org_rows = [row for row in org_rows if row.id in org_ids]
            expected_count = max(82, len(directory_ids))
            clipped = len(org_ids) != expected_count
            snapshots = _snapshots(db, context)
            snapshot = snapshots[context["snapshot_id"]]
            regions = {row.id: row.name for row in db.scalars(select(Organization).where(Organization.level == 2))}
            organizations = [{"org_id": row.id, "org_name": row.name,
                              "region_name": regions.get(row.region_id or row.parent_id, "")}
                             for row in org_rows]
            by_org = {row["org_id"]: row for row in organizations}
            metrics = {row.id: row for row in db.scalars(select(Indicator).where(Indicator.id.in_(requested)))}
            missing_source = sorted(set(requested) - set(metrics))
            indicators = [{"indicator_id": row.id, "indicator_name": row.name,
                           "unit": row.unit, "frequency": row.frequency} for row in metrics.values()]
            current = []
            history_by_period = {}
            if metrics:
                query = select(Observation).where(Observation.snapshot_id.in_(snapshots),
                    Observation.org_id.in_(org_ids), Observation.indicator_id.in_(metrics))
                for obs in db.scalars(query.execution_options(yield_per=1000)):
                    metric = metrics[obs.indicator_id]
                    issue = observation_issue(obs, metric, snapshot)
                    record = {**observation_record(obs, metric), **by_org[obs.org_id],
                              "source": context["source"], "usable": issue is None, "issue": issue}
                    if obs.snapshot_id == snapshot.id:
                        current.append(record)
                    if obs.data_date > as_of:
                        continue
                    historical_issue = observation_issue(obs, metric, SimpleNamespace(data_date=obs.data_date))
                    hist_record = {**record, "usable": historical_issue is None, "issue": historical_issue,
                                   "current_issue": issue}
                    key = (obs.org_id, obs.indicator_id, obs.data_date)
                    source_snap = snapshots[obs.snapshot_id]
                    priority = (obs.snapshot_id == snapshot.id, source_snap.data_date,
                                source_snap.created_at, source_snap.id, obs.id)
                    if key not in history_by_period or priority > history_by_period[key][0]:
                        history_by_period[key] = (priority, hist_record)
            current.sort(key=lambda x: (x["org_id"], x["indicator_id"]))
            found = {(row["org_id"], row["indicator_id"]) for row in current}
            missing = [{"org_id": org, "indicator_id": metric, "status": "missing"}
                       for org in sorted(org_ids) for metric in sorted(metrics) if (org, metric) not in found]
            history = [item[1] for item in history_by_period.values()]
            history.sort(key=lambda x: (x["org_id"], x["indicator_id"], x["data_date"]))
            return {"snapshot_id": snapshot.id, "as_of": context["as_of"], "source": context["source"],
                    "organizations": organizations, "indicators": indicators, "latest": current,
                    "history": history, "missing": missing, "missing_source": missing_source,
                    "source_totals": {}, "visibility": {"expected_org_count": expected_count, "visible_org_count": len(org_ids),
                                                         "clipped": clipped, "visible_note": "固定任务仅覆盖部分行社" if clipped else None}}


def data_fetcher_name() -> str:
    mode = os.getenv("RISK_DATA_FETCHER", "local").strip().lower()
    if mode not in {"local", "quickbi"}:
        raise ValueError("RISK_DATA_FETCHER 仅支持 local 或 quickbi")
    return mode


def get_data_fetcher() -> DataFetcher:
    """每次扫描独立持有身份及元数据缓存，避免在不同会话间共享查询结果。"""
    if data_fetcher_name() == "quickbi":
        from .quickbi_adapter import QuickBiOlapFetcher
        return QuickBiOlapFetcher()
    return LocalSnapshotFetcher()

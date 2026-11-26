"""历史任务清理。锁定任务与函件后核对状态，每个任务的关联记录在同一事务删除。"""
from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (AgentTurn, AnalysisRun, Letter, LetterHistory, QueryBudgetLedger,
                     QueryOperation, RiskCase, lock_analysis_run)


def _delete_locked(db: Session, run: AnalysisRun) -> None:
    progress = (run.selection.get("risk_scan_result") or {}).get("auto_reports") or {}
    if run.status == "running" or progress.get("status") == "running":
        raise HTTPException(409, "任务或风险报告正在生成，禁止删除")
    case_ids = select(RiskCase.id).where(RiskCase.run_id == run.id)
    letters = db.execute(select(Letter.id, Letter.status).where(Letter.case_id.in_(case_ids))
                         .order_by(Letter.id).with_for_update()).all()
    if any(status in {"approved", "simulated_sent"} for _, status in letters):
        raise HTTPException(409, "存在已定稿或已下发的风险报告，禁止删除")
    if letters:
        letter_ids = [letter_id for letter_id, _ in letters]
        db.execute(delete(LetterHistory).where(LetterHistory.letter_id.in_(letter_ids)))
        db.execute(delete(Letter).where(Letter.id.in_(letter_ids)))
    for model in (RiskCase, AgentTurn, QueryOperation, QueryBudgetLedger):
        db.execute(delete(model).where(model.run_id == run.id))
    db.delete(run)


def delete_analysis_run(db: Session, run_id: str, *, expected_kind: str | None = None):
    """调用方提供独立事务；专用路由只接受所属类型，通用入口可清理任意类型。"""
    run = lock_analysis_run(db, run_id)
    if run is None or (expected_kind is not None and run.selection.get("kind") != expected_kind):
        raise HTTPException(404, "任务不存在")
    _delete_locked(db, run)
    return {"deleted_id": run_id}


def cleanup_failed_runs(sessions):
    """逐任务持锁复查，跳过受保护及已被其他请求删除、重跑的记录。"""
    with sessions() as db:
        candidates = db.scalars(select(AnalysisRun.id).where(AnalysisRun.status == "failed")
                                .order_by(AnalysisRun.id)).all()
    deleted = []
    for run_id in candidates:
        with sessions.begin() as db:
            run = lock_analysis_run(db, run_id)
            if run is None or run.status != "failed":
                continue
            try:
                _delete_locked(db, run)
            except HTTPException as exc:
                if exc.status_code == 409:
                    continue
                raise
        deleted.append(run_id)
    return {"deleted_count": len(deleted), "deleted_ids": deleted}

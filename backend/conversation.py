"""GUI 多轮追问：从当前任务留存的事实构建只读上下文，按运行及行社保存答复。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .analysis import provider
from .models import AgentTurn, AnalysisRun, RiskCase, SessionLocal, Snapshot

router = APIRouter(prefix="/api/agent/runs", tags=["Pi 对话"])


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=1000)
    case_id: str | None = Field(default=None, min_length=1, max_length=64)


def _scope(db, run_id, case_id):
    run = db.get(AnalysisRun, run_id)
    if run is None:
        raise HTTPException(404, "分析任务不存在。")
    case = None
    if case_id is not None:
        case = db.get(RiskCase, case_id)
        if case is None or case.run_id != run.id or run.selection.get("kind") == "code":
            raise HTTPException(404, "该任务中没有此风险事项。")
    return run, case


def _collect_observation(by_id, row, org_name):
    """同一观测可被多条规则引用，保留所有触发原因，跳过明确不可用的观测。"""
    if row.get("usable") is False or row.get("quality_status", "valid") != "valid":
        return
    key = row.get("evidence_id") or row.get("id")
    if not key or row.get("value_display") is None or ("value" in row and row["value"] is None):
        return
    if key not in by_id:
        label = f"{org_name} · {row['indicator_name']}"
        text = f"{row['data_date']}，{label}为{row['value_display']}。"
        by_id[key] = {"id": key, "label": label, "text": text}
    reason = row.get("reason")
    if reason:
        reason = f"{row.get('rule_name') or '触发依据'}：{reason}。"
        if reason not in by_id[key]["text"]:
            by_id[key]["text"] += reason


def _summary_record(run, counts):
    fields = {"case_count": "风险行社数", "triggered": "触发项数", "normal": "正常项数",
              "insufficient": "数据不足项数", "unconfigured": "待配置项数",
              "not_applicable": "不适用项数", "checked_checks": "已检查项数"}
    text = "，".join(f"{label} {counts[key]}" for key, label in fields.items() if key in counts)
    return {"id": f"summary:{run.id}", "label": "任务检查汇总", "text": text}


def _archived_evidence(db, run, case):
    """证据清单由宿主从已归档的任务结果生成；切换行社时不包含其他行社或任务汇总。"""
    frozen = run.selection.get("task_context") or {}
    as_of = frozen.get("as_of") or db.get(Snapshot, run.snapshot_id).data_date.isoformat()
    org_scope = frozen.get("org_scope")
    context = {"topic": frozen.get("topic", run.selection.get("topic", "")), "as_of": as_of,
               "organization": case.org_name if case else (
                   f"本次任务范围内的 {len(org_scope)} 家行社" if org_scope else "本次任务范围")}
    by_id = {}
    if run.selection.get("kind") == "code":
        result = run.selection.get("code_result") or {}
        if result.get("outcome") == "zero-candidates":
            summary = _summary_record(run, result.get("coverage", {}))
            summary["text"] = "。".join(filter(None, (result["summary"], summary["text"])))
            by_id[summary["id"]] = summary
        for row in result.get("evidence", []):
            _collect_observation(by_id, row, row.get("org_name") or row["org_id"])
        for i, finding in enumerate(result.get("findings", []), 1):
            # 归档发现与原始观测分别标注；只有能追溯到本次原始记录的发现才纳入。
            if finding["evidence_ids"] and all(key in by_id for key in finding["evidence_ids"]):
                key = f"finding:{run.id}:{i}"
                by_id[key] = {"id": key, "label": f"{finding['org_id']} · 归档分析发现",
                              "text": finding["statement"]}
    else:
        if not case:
            summary = _summary_record(run, run.summary)
            if summary["text"]:
                by_id[summary["id"]] = summary
        cases = [case] if case else db.scalars(select(RiskCase).where(
            RiskCase.run_id == run.id).order_by(RiskCase.org_id))
        for item in cases:
            for row in (*item.evidence, *item.analysis.get("queried_evidence", [])):
                if row.get("org_id", item.org_id) == item.org_id:
                    _collect_observation(by_id, row, item.org_name)
    if len(by_id) > 1000:
        raise HTTPException(422, "本次归档证据超过 1000 条，请缩小到单个行社再追问。")
    return context, list(by_id.values())


def _turns(run_id, case_id):
    return select(AgentTurn).where(AgentTurn.run_id == run_id, AgentTurn.scope == (case_id or ""))


def _public(turn):
    return {"id": turn.id, "question": turn.question, "answer": turn.answer, "evidence": turn.evidence,
            "mode": "pi-" + turn.diagnostics["runtime"]["mode"], **turn.diagnostics}


@router.get("/{run_id}/conversation")
def conversation(run_id: str, case_id: str | None = None):
    with SessionLocal() as db:
        _scope(db, run_id, case_id)
        turns = db.scalars(_turns(run_id, case_id).order_by(AgentTurn.turn_no))
        return {"turns": [_public(turn) for turn in turns]}


@router.post("/{run_id}/conversation")
def ask(run_id: str, data: Question):
    with SessionLocal() as db:
        run, case = _scope(db, run_id, data.case_id)
        if run.status not in {"completed", "no_candidates"}:
            raise HTTPException(409, "请先完成分析，再就结果追问。")
        context, evidence = _archived_evidence(db, run, case)
        recent = db.execute(_turns(run_id, data.case_id).with_only_columns(
            AgentTurn.turn_no, AgentTurn.question, AgentTurn.answer
        ).order_by(AgentTurn.turn_no.desc()).limit(6)).all()
        turn_no = recent[0].turn_no + 1 if recent else 1
        history = [{"question": turn.question, "answer": turn.answer} for turn in reversed(recent)]

    # 模型运行期间不占用数据库事务；并发回答同一轮时，仅保留先提交的完整对话。
    reply = provider.follow_up(data.question, history, context, evidence)
    with SessionLocal() as db:
        turn = AgentTurn(run_id=run_id, scope=data.case_id or "", turn_no=turn_no,
                         question=data.question, **reply)
        db.add(turn)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if db.scalar(_turns(run_id, data.case_id).where(AgentTurn.turn_no == turn_no)):
                raise HTTPException(409, "该对话已有新答复，请刷新后继续追问。") from None
            raise
        return _public(turn)

"""自主代码分析的业务边界：冻结任务、调用Pi、校验执行产物并保存可追溯结论。

代码只能在Docker运行。API和模型均不直接获得指标库写入能力；最终结论必须绑定
本任务真实导出的原始记录，以及成功执行后未被改写的结果文件。
"""
from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from urllib.parse import quote
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .analysis import provider
from .models import AnalysisRun, Organization, Rule, SessionLocal, Snapshot, uid
from .services import freeze_context, load_task_context
from . import task_workspace as workspace

router = APIRouter(prefix="/api/code-runs", tags=["自主代码分析"])


class CodeRunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    snapshot_id: str
    risk_topic: str = Field(min_length=1, max_length=500)
    org_scope: list[str] | None = None
    retry: bool = False


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    org_id: str
    statement: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class Submission(BaseModel):
    summary: str = Field(min_length=1, max_length=8000)
    findings: list[Finding] = Field(min_length=1, max_length=82)
    execution_ids: list[str] = Field(min_length=1, max_length=30)
    result_file: str


def validate_code_result(run_id: str, result: dict) -> dict:
    """校验来源/机构及成功执行产物；保留可重跑证据，不声称自动证明任意脚本逻辑。"""
    submitted = Submission.model_validate(result)
    context = load_task_context(run_id)
    ids = list(dict.fromkeys(eid for item in submitted.findings for eid in item.evidence_ids))
    evidence = workspace.resolve_evidence(run_id, ids)
    by_id = {item["evidence_id"]: item for item in evidence}
    for finding in submitted.findings:
        if finding.org_id not in context["org_scope"]:
            raise workspace.WorkspaceError("SCOPE_VIOLATION", "结论机构不属于当前任务")
        if any(by_id[eid]["org_id"] != finding.org_id for eid in finding.evidence_ids):
            raise workspace.WorkspaceError("SCOPE_VIOLATION", "结论引用了其他机构的观测")

    with workspace.task_lock(run_id):
        file = workspace.read_task_file(run_id, submitted.result_file)
        executions = [workspace.get_execution(run_id, eid) for eid in submitted.execution_ids]
        matched = any(execution["exit_code"] == 0 and any(
            item["path"] == submitted.result_file and item["sha256"] == file["sha256"]
            for item in execution["files"]
        ) for execution in executions)
        if not matched:
            raise workspace.WorkspaceError("UNVERIFIED_RESULT", "结果文件未绑定本任务的成功执行，或已被执行后改写")
        body = json.loads(file["content"])
        expected = {"summary": submitted.summary,
                    "findings": [item.model_dump() for item in submitted.findings]}
        if body != expected:
            raise workspace.WorkspaceError("UNVERIFIED_RESULT", "提交结论与脚本实际输出不一致")
    return {**result, **submitted.model_dump(), "evidence": evidence,
            "result_sha256": file["sha256"]}


def _run_dict(run: AnalysisRun) -> dict:
    return {"id": run.id, "status": run.status, "snapshot_id": run.snapshot_id,
            "created_at": run.created_at.isoformat(), "summary": run.summary,
            "selection": run.selection}


def _get_run(db, run_id: str) -> AnalysisRun:
    run = db.get(AnalysisRun, run_id)
    if not run or run.selection.get("kind") != "code":
        raise HTTPException(404, "自主分析任务不存在")
    return run


@router.get("")
def list_code_runs():
    with SessionLocal() as db:
        return [_run_dict(run) for run in db.scalars(select(AnalysisRun).where(
            AnalysisRun.selection["kind"].as_string() == "code").order_by(
            AnalysisRun.created_at.desc()).limit(100))]


@router.get("/{run_id}")
def get_code_run(run_id: str):
    with SessionLocal() as db:
        run = _get_run(db, run_id)
        result = {**_run_dict(run), "result": run.selection.get("code_result")}
    # 容器运行期间不读取可能正在变动的work文件；meta执行记录由宿主写入。
    files, workspace_error = [], None
    if result["status"] != "running":
        try:
            files = workspace.list_task_files(run_id)
        except workspace.WorkspaceError as exc:
            workspace_error = str(exc)
    return {**result, "executions": workspace.list_executions(run_id),
            "files": files, "workspace_error": workspace_error}


@router.post("")
def create_code_run(data: CodeRunInput):
    """整个机构范围共享一个分析任务；模型和Docker执行始终位于写事务之外。"""
    with SessionLocal() as db:
        snapshot = db.get(Snapshot, data.snapshot_id)
        if not snapshot or snapshot.status != "ready":
            raise HTTPException(422, "请选择可用快照")
        available = set(db.scalars(select(Organization.id).where(Organization.level == 3)))
        scope = sorted(set(data.org_scope) if data.org_scope is not None else available)
        if not scope or not set(scope) <= available:
            raise HTTPException(422, "分析范围必须是目录内的行社")
        frozen = [{key: getattr(rule, key) for key in
                   ("id", "name", "kind", "indicator_id", "enabled", "params", "version", "description")}
                  for rule in db.scalars(select(Rule).order_by(Rule.id))]
        topic = " ".join(data.risk_topic.split())
        identity = {"kind": "code-v1", "snapshot": snapshot.id, "topic": topic,
                    "scope": scope, "rules": frozen, "provider": provider.cache_identity}
        key = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        run = db.scalar(select(AnalysisRun).where(AnalysisRun.run_key == key))
        if run:
            if run.status != "failed" or not data.retry:
                return _run_dict(run)
            claim = db.execute(update(AnalysisRun).where(
                AnalysisRun.id == run.id, AnalysisRun.status == "failed").values(status="running"))
            if claim.rowcount != 1:
                db.rollback()
                db.refresh(run)
                return _run_dict(run)
            context = run.selection["task_context"]
            run.selection = {**run.selection, "error": None, "attempt": run.selection.get("attempt", 1) + 1}
        else:
            run_id = uid()
            context = freeze_context(db, run_id, snapshot, topic, frozen, scope)
            run = AnalysisRun(id=run_id, run_key=key, snapshot_id=snapshot.id, status="running",
                              summary={}, rule_snapshot=frozen,
                              selection={"kind": "code", "mode": provider.mode, "attempt": 1,
                                         "topic": topic, "task_context": context})
            db.add(run)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            winner = db.scalar(select(AnalysisRun).where(AnalysisRun.run_key == key))
            if winner:
                return _run_dict(winner)
            raise
        run_id = run.id
    try:
        result = validate_code_result(run_id, provider.explore(context))
        with SessionLocal() as db:
            run = _get_run(db, run_id)
            run.selection = {**run.selection, "code_result": result}
            run.summary = {"case_count": len(result["findings"]), "evidence_count": len(result["evidence"])}
            run.status = "completed"
            db.commit()
            return _run_dict(run)
    except Exception as exc:
        from .analysis import AnalysisError
        message = str(exc) if isinstance(exc, (AnalysisError, workspace.WorkspaceError)) else "代码分析结果未通过校验，请查看执行日志。"
        with SessionLocal() as db:
            run = _get_run(db, run_id)
            run.status = "failed"
            run.selection = {**run.selection, "error": message, "error_type": type(exc).__name__}
            db.commit()
        raise HTTPException(502, {"run_id": run_id, "message": message}) from exc


@router.get("/{run_id}/files/{path:path}")
def download_code_file(run_id: str, path: str):
    with SessionLocal() as db:
        run = _get_run(db, run_id)
        if run.status == "running":
            raise HTTPException(409, "执行期间请等待文件写入完成")
    try:
        # 在跨进程任务锁内完成读取，Response返回后不再打开可变工作区路径。
        with workspace.task_lock(run_id):
            target = workspace.resolve_work_path(run_id, path, must_exist=True)
            if target.stat().st_size > workspace.MAX_WORK_BYTES:
                raise workspace.WorkspaceError("OUTPUT_LIMIT", "文件超出工作区大小限制")
            content = target.read_bytes()
        return Response(content, media_type="application/octet-stream", headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(target.name),
        })
    except workspace.WorkspaceError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{run_id}/draft")
def draft_code_report(run_id: str):
    """生成可复核的自主分析综述草稿；不自动审批或正式下发。"""
    with SessionLocal() as db:
        run = _get_run(db, run_id)
        if run.status != "completed":
            raise HTTPException(409, "分析尚未完成，不能生成草稿")
        result = run.selection["code_result"]
        context = run.selection["task_context"]
    lines = ["自主风险分析提示函（测试草稿）", "", f"任务截至：{context['as_of']}",
             result["summary"], "", "分析发现："]
    for finding in result["findings"]:
        lines.extend([f"{finding['org_id']}：{finding['statement']}", "证据：" + "、".join(finding["evidence_ids"])])
    lines.extend(["", "原始记录："])
    for item in result["evidence"]:
        lines.append(f"{item['evidence_id']} | {item['org_id']} | {item['indicator_name']} | {item['value_display']} | {item['data_date']}")
    lines.extend(["", f"结果文件：{result['result_file']}；SHA256：{result['result_sha256']}",
                  "结论来自分析脚本与引用记录，具体原因和责任仍待人工核查。仅供测试，不构成真实发函。"])
    return {"content": "\n".join(lines)}

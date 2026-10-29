"""Local v0.1 API: deterministic monitoring, explainable evidence, simulated delivery.

Run: python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from .analysis import provider
from .models import (ROOT, Base, engine, SessionLocal, Organization, Indicator, Snapshot, Observation,
                     Rule, AnalysisRun, RiskCase, Letter, LetterHistory, AuditLog, uid, now)
from .seed import seed_database


def hash_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def timestamp(value):
    return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()


def number(value):
    if value is None:
        return None
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def display(value, unit):
    if value is None:
        return "缺失"
    val = Decimal(str(value))
    if unit == "%":
        return f"{val:.4f}".rstrip("0").rstrip(".") + "%"
    if unit == "元" and abs(val) >= Decimal("10000"):
        return f"{val / Decimal('10000'):,.2f}万元"
    return f"{val:,.2f}{unit}"


def org_dict(row):
    return {k: getattr(row, k) for k in ("id", "code", "name", "level", "parent_id", "region_id")}


def metric_dict(row):
    return {k: getattr(row, k) for k in ("id", "name", "unit", "frequency", "description")}


def rule_dict(row):
    return {**{k: getattr(row, k) for k in ("id", "name", "kind", "indicator_id", "enabled", "params", "version", "description")},
            "needs_config": row.kind == "ratio" and row.params.get("threshold") is None}


def snapshot_dict(row):
    return {"id": row.id, "name": row.name, "data_date": row.data_date.isoformat(), "source": row.source,
            "is_demo": row.is_demo, "row_count": row.row_count, "status": row.status}


def run_dict(row):
    return {"id": row.id, "status": row.status, "snapshot_id": row.snapshot_id, "created_at": timestamp(row.created_at),
            "summary": row.summary, "selection": row.selection}


def case_dict(row):
    return {"id": row.id, "run_id": row.run_id, "org_id": row.org_id, "org_name": row.org_name,
            "region_name": row.region_name, "data_date": row.data_date.isoformat(), "risk_types": row.risk_types,
            "status": row.status, "evidence_count": len(row.evidence), "headline": row.headline}


def letter_dict(row):
    return {"id": row.id, "case_id": row.case_id, "revision": row.revision, "content": row.content,
            "status": row.status, "evidence_hash": row.evidence_hash, "created_at": timestamp(row.created_at), "receipt": row.receipt}


def get_or_404(db, model, key):
    value = db.get(model, key)
    if value is None:
        raise HTTPException(404, "记录不存在")
    return value


def audit(db, action, key, details=None):
    db.add(AuditLog(action=action, entity_id=key, details=details or {}))


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_database(db)
    yield


app = FastAPI(title="行社风险提示测试版", version="0.1.0", lifespan=lifespan)


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunPayload(Payload):
    snapshot_id: str
    risk_topic: str = Field(default="", max_length=500)


class RulePayload(Payload):
    enabled: bool | None = None
    params: dict | None = None


class RevisionPayload(Payload):
    expected_revision: int = Field(ge=1)


class EditPayload(RevisionPayload):
    content: str = Field(min_length=1, max_length=30000)


@app.get("/api/health")
def health():
    return {"status": "ok", "demo": True, "real_delivery": False}


@app.get("/api/bootstrap")
def bootstrap():
    with SessionLocal() as db:
        orgs = db.scalars(select(Organization).order_by(Organization.level, Organization.code)).all()
        indicators = db.scalars(select(Indicator).order_by(Indicator.id)).all()
        rules = db.scalars(select(Rule).order_by(Rule.id)).all()
        snaps = db.scalars(select(Snapshot).order_by(Snapshot.created_at.desc(), Snapshot.data_date.desc())).all()
        latest = db.scalar(select(AnalysisRun).order_by(AnalysisRun.created_at.desc()).limit(1))
        return {
            "meta": {"app_name": "行社风险提示", "version": "0.1.0", "demo": True, "agent_mode": "template",
                     "data_date": max((s.data_date for s in snaps), default=date.today()).isoformat(),
                     "rank_note": "默认排名≥72；82家全部参评且无并列时为第72—82名，共11家；并列不截断。"},
            "summary": {"region_count": sum(o.level == 2 for o in orgs), "org_count": sum(o.level == 3 for o in orgs),
                        "indicator_count": len(indicators), "enabled_rule_count": sum(r.enabled for r in rules),
                        "case_count": db.scalar(select(func.count()).select_from(RiskCase)) or 0,
                        "letter_count": db.scalar(select(func.count()).select_from(Letter)) or 0},
            "organizations": [org_dict(o) for o in orgs], "indicators": [metric_dict(i) for i in indicators],
            "rules": [rule_dict(r) for r in rules], "snapshots": [snapshot_dict(s) for s in snaps],
            "latest_run": run_dict(latest) if latest else None,
        }


@app.get("/api/organizations")
def organizations():
    with SessionLocal() as db:
        return [org_dict(o) for o in db.scalars(select(Organization).order_by(Organization.level, Organization.code))]


@app.get("/api/indicators")
def indicators(q: str = ""):
    with SessionLocal() as db:
        items = db.scalars(select(Indicator).order_by(Indicator.id)).all()
        return [metric_dict(i) for i in items if not q or q.lower() in (i.id + i.name + i.description).lower()]


@app.get("/api/rules")
def rules():
    with SessionLocal() as db:
        return [rule_dict(r) for r in db.scalars(select(Rule).order_by(Rule.id))]


@app.patch("/api/rules/{rule_id}")
def patch_rule(rule_id: str, data: RulePayload):
    with SessionLocal() as db:
        rule = get_or_404(db, Rule, rule_id)
        params = dict(rule.params)
        changes = data.params or {}
        allowed = {"rank": {"cutoff"}, "ratio": {"threshold", "op"}, "exists": set()}[rule.kind]
        if set(changes) - allowed:
            raise HTTPException(422, "该规则含不可修改参数")
        params.update(changes)
        if rule.kind == "rank":
            cutoff = params.get("cutoff")
            if isinstance(cutoff, bool) or not isinstance(cutoff, int) or not 1 <= cutoff <= 82:
                raise HTTPException(422, "排名边界必须为1至82之间的整数")
        elif rule.kind == "ratio":
            threshold = params.get("threshold")
            if threshold is not None:
                try:
                    value = Decimal(str(threshold))
                except InvalidOperation:
                    raise HTTPException(422, "占比阈值必须是有限数值")
                if not value.is_finite() or value < 0 or value > 100:
                    raise HTTPException(422, "占比阈值必须在0至100之间，单位为%")
                params["threshold"] = number(value)
            if params.get("op") not in {">=", ">"}:
                raise HTTPException(422, "占比比较符只支持>=或>")
        enabled = rule.enabled if data.enabled is None else data.enabled
        if params != rule.params or enabled != rule.enabled:
            rule.params, rule.enabled, rule.version = params, enabled, rule.version + 1
            audit(db, "rule_updated", rule.id, {"version": rule.version, "params": params, "test_configuration": True})
            db.commit()
        return rule_dict(rule)


def evaluate(rule, obs, metric, snapshot):
    if rule.kind == "ratio" and rule.params.get("threshold") is None:
        return "unconfigured", "占比阈值尚未配置"
    if obs is None or obs.value is None or obs.quality_status != "valid":
        return "insufficient", "指标数据缺失或质量状态不合格"
    lag = (snapshot.data_date - obs.data_date).days
    allowed_lag = 120 if metric.frequency == "季" else 7
    if lag < 0 or lag > allowed_lag:
        return "insufficient", f"数据期次不满足测试新鲜度策略（{metric.frequency}频指标允许滞后{allowed_lag}天）"
    if obs.unit != metric.unit:
        return "insufficient", "指标单位与目录不一致"
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


def evidence_record(rule, obs, metric, reason):
    return {"id": uid(), "indicator_id": metric.id, "indicator_name": metric.name, "value": number(obs.value),
            "value_display": display(obs.value, obs.unit), "unit": obs.unit, "rank": obs.rank, "population": obs.population,
            "data_date": obs.data_date.isoformat(), "rule_id": rule.id, "rule_name": rule.name, "rule_kind": rule.kind,
            "rule_version": rule.version, "reason": reason, "quality_status": "valid", "source_record_id": obs.id}


@app.post("/api/runs")
def create_run(data: RunPayload):
    with SessionLocal() as db:
        snapshot = get_or_404(db, Snapshot, data.snapshot_id)
        if snapshot.status != "ready":
            raise HTTPException(409, "数据快照尚未就绪")
        active_rules = db.scalars(select(Rule).where(Rule.enabled == True).order_by(Rule.id)).all()
        all_rules = db.scalars(select(Rule).order_by(Rule.id)).all()
        frozen_rules = [rule_dict(r) for r in all_rules]
        normalized_topic = " ".join(data.risk_topic.split())
        key = hash_json({"snapshot": snapshot.id, "rules": frozen_rules, "risk_topic": normalized_topic,
                         "provider_mode": "template", "provider_version": "0.1.0"})
        existing = db.scalar(select(AnalysisRun).where(AnalysisRun.run_key == key))
        if existing:
            return run_dict(existing)
        metrics = {i.id: i for i in db.scalars(select(Indicator))}
        orgs = db.scalars(select(Organization).where(Organization.level == 3).order_by(Organization.code)).all()
        region_names = {o.id: o.name for o in db.scalars(select(Organization).where(Organization.level == 2))}
        observations = {(o.org_id, o.indicator_id): o for o in db.scalars(select(Observation).where(Observation.snapshot_id == snapshot.id))}
        selection = provider.select(normalized_topic, [metric_dict(m) for m in metrics.values()], [r.indicator_id for r in active_rules])
        counts = {k: 0 for k in ("evaluated", "triggered", "normal", "insufficient", "not_applicable", "unconfigured", "case_count")}
        run = AnalysisRun(run_key=key, snapshot_id=snapshot.id, status="running", summary=counts, selection=selection, rule_snapshot=frozen_rules)
        db.add(run)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            winner = db.scalar(select(AnalysisRun).where(AnalysisRun.run_key == key))
            if winner:
                return run_dict(winner)
            raise HTTPException(409, "同批次正在执行，请稍后重试")
        checks = []
        for org in orgs:
            evidence = []
            for rule in active_rules:
                obs, metric = observations.get((org.id, rule.indicator_id)), metrics[rule.indicator_id]
                status, reason = evaluate(rule, obs, metric, snapshot)
                counts[status] += 1
                if status in {"normal", "triggered"}:
                    counts["evaluated"] += 1
                checks.append({"org_id": org.id, "org_name": org.name, "rule_id": rule.id, "indicator_id": metric.id, "status": status, "reason": reason})
                if status == "triggered":
                    evidence.append(evidence_record(rule, obs, metric, reason))
            if not evidence:
                continue
            supporting = []
            for metric_id in selection["indicator_ids"]:
                obs = observations.get((org.id, metric_id))
                if obs and obs.value is not None:
                    supporting.append({"indicator_id": metric_id, "value": number(obs.value), "value_display": display(obs.value, obs.unit), "data_date": obs.data_date.isoformat()})
            analysis = provider.explain(evidence, supporting)
            if not snapshot.is_demo:
                analysis["limitations"] = [x for x in analysis["limitations"] if "内置数值" not in x]
                analysis["limitations"].insert(0, "本批次为用户导入数据，仍仅用于本地测试，未核验生产口径。")
            types = sorted({{"rank": "排名提醒", "ratio": "占比提醒", "exists": "存在提醒"}[e["rule_kind"]] for e in evidence})
            db.add(RiskCase(run_id=run.id, org_id=org.id, org_name=org.name, region_name=region_names.get(org.region_id, "未分配地区"),
                            data_date=snapshot.data_date, risk_types=types, status="open",
                            headline=f"{len(evidence)}项规则触发 · " + "、".join(types), evidence=evidence, analysis=analysis))
            counts["case_count"] += 1
        run.summary, run.checks, run.status = dict(counts), checks, "completed"
        audit(db, "analysis_completed", run.id, {"snapshot_id": snapshot.id, "rule_versions": {r.id: r.version for r in active_rules}})
        db.commit()
        return run_dict(run)


@app.get("/api/runs")
def list_runs():
    with SessionLocal() as db:
        return [run_dict(r) for r in db.scalars(select(AnalysisRun).order_by(AnalysisRun.created_at.desc()))]


@app.get("/api/runs/{run_id}/checks")
def get_checks(run_id: str):
    with SessionLocal() as db:
        return get_or_404(db, AnalysisRun, run_id).checks


@app.get("/api/cases")
def list_cases(run_id: str | None = None):
    with SessionLocal() as db:
        query = select(RiskCase)
        if run_id:
            query = query.where(RiskCase.run_id == run_id)
        return [case_dict(c) for c in db.scalars(query.order_by(RiskCase.org_id))]


@app.get("/api/cases/{case_id}")
def get_case(case_id: str):
    with SessionLocal() as db:
        case = get_or_404(db, RiskCase, case_id)
        letter = db.scalar(select(Letter).where(Letter.case_id == case_id))
        return {**case_dict(case), "evidence": case.evidence, "analysis": case.analysis, "letter": letter_dict(letter) if letter else None}


def render_letter(case):
    facts = "\n".join(f"{i}. {e['indicator_name']}：{e['value_display']}，数据日期{e['data_date']}。{e['reason']}。" for i, e in enumerate(case.evidence, 1))
    checks = "\n".join(f"{i}. {text}" for i, text in enumerate(case.analysis.get("checks", []), 1))
    return (f"风险提示函（本地测试通用模板）\n\n致：{case.org_name}\n\n"
            f"根据截至{case.data_date.isoformat()}纳入本批次的数据，以下指标触发当前测试规则：\n\n{facts}\n\n"
            f"请核查以下事项：\n{checks}\n\n"
            "上述内容为指标事实与规则筛选结果，具体原因和责任尚待核查。请按正式业务流程补充核查说明。\n\n"
            "模板状态：行内正式模板尚未提供。\n环境：本地测试；模拟下发不构成真实发函。")


@app.post("/api/cases/{case_id}/draft")
def draft_letter(case_id: str):
    with SessionLocal() as db:
        case = get_or_404(db, RiskCase, case_id)
        existing = db.scalar(select(Letter).where(Letter.case_id == case_id))
        if existing:
            return letter_dict(existing)
        letter = Letter(case_id=case.id, content=render_letter(case), evidence_hash=hash_json(case.evidence), revision=1, status="draft")
        db.add(letter)
        try:
            db.flush()
            db.add(LetterHistory(letter_id=letter.id, revision=1, content=letter.content, evidence_hash=letter.evidence_hash))
            case.status = "draft"
            audit(db, "letter_drafted", letter.id, {"revision": 1})
            db.commit()
        except IntegrityError:
            db.rollback()
            letter = db.scalar(select(Letter).where(Letter.case_id == case_id))
            if not letter:
                raise HTTPException(409, "函件正在创建，请稍后重试")
        return letter_dict(letter)


def ensure_revision(letter, revision):
    if letter.revision != revision:
        raise HTTPException(409, "函件版本已变化，请刷新后重试")


@app.patch("/api/letters/{letter_id}")
def edit_letter(letter_id: str, data: EditPayload):
    with SessionLocal() as db:
        letter = get_or_404(db, Letter, letter_id)
        ensure_revision(letter, data.expected_revision)
        if letter.status == "simulated_sent":
            raise HTTPException(409, "已模拟下发的版本不可修改")
        content = data.content.strip()
        if not content:
            raise HTTPException(422, "函件内容不能为空")
        result = db.execute(update(Letter).where(Letter.id == letter_id, Letter.revision == data.expected_revision, Letter.status != "simulated_sent")
                            .values(content=content, revision=data.expected_revision + 1, status="draft", approved_revision=None, approved_hash=None))
        if result.rowcount != 1:
            raise HTTPException(409, "函件已被其他操作修改")
        db.add(LetterHistory(letter_id=letter_id, revision=data.expected_revision + 1, content=content, evidence_hash=letter.evidence_hash))
        get_or_404(db, RiskCase, letter.case_id).status = "draft"
        audit(db, "letter_edited", letter_id, {"revision": data.expected_revision + 1, "approval_invalidated": True})
        db.commit()
        db.refresh(letter)
        return letter_dict(letter)


@app.post("/api/letters/{letter_id}/approve")
def approve_letter(letter_id: str, data: RevisionPayload):
    with SessionLocal() as db:
        letter = get_or_404(db, Letter, letter_id)
        ensure_revision(letter, data.expected_revision)
        if letter.status == "simulated_sent":
            return letter_dict(letter)
        result = db.execute(update(Letter).where(Letter.id == letter_id, Letter.revision == data.expected_revision, Letter.status.in_(["draft", "approved"]))
                            .values(status="approved", approved_revision=letter.revision, approved_hash=hash_json({"content": letter.content, "evidence": letter.evidence_hash, "case_id": letter.case_id})))
        if result.rowcount != 1:
            raise HTTPException(409, "当前版本无法审核")
        get_or_404(db, RiskCase, letter.case_id).status = "approved"
        audit(db, "letter_approved", letter_id, {"revision": letter.revision, "actor": "local_test_user"})
        db.commit()
        db.refresh(letter)
        return letter_dict(letter)


@app.post("/api/letters/{letter_id}/simulate-send")
def simulate_send(letter_id: str, data: RevisionPayload):
    with SessionLocal() as db:
        letter = get_or_404(db, Letter, letter_id)
        ensure_revision(letter, data.expected_revision)
        if letter.status == "simulated_sent" and letter.receipt:
            return letter_dict(letter)
        content_hash = hash_json({"content": letter.content, "evidence": letter.evidence_hash, "case_id": letter.case_id})
        if letter.status != "approved" or letter.approved_revision != letter.revision or letter.approved_hash != content_hash:
            raise HTTPException(409, "须先审核通过当前函件版本")
        receipt = {"id": f"SIM-{letter.id}-{letter.revision}", "channel": "simulation", "status": "simulated_sent", "created_at": timestamp(now())}
        result = db.execute(update(Letter).where(Letter.id == letter_id, Letter.revision == data.expected_revision, Letter.status == "approved", Letter.approved_hash == content_hash)
                            .values(status="simulated_sent", receipt=receipt))
        if result.rowcount != 1:
            db.rollback()
            db.refresh(letter)
            if letter.status == "simulated_sent":
                return letter_dict(letter)
            raise HTTPException(409, "函件状态已变化")
        get_or_404(db, RiskCase, letter.case_id).status = "simulated_sent"
        audit(db, "letter_simulated_send", letter_id, {"receipt_id": receipt["id"], "no_external_call": True})
        db.commit()
        db.refresh(letter)
        return letter_dict(letter)


@app.get("/api/letters/{letter_id}/download")
def download_letter(letter_id: str):
    with SessionLocal() as db:
        letter = get_or_404(db, Letter, letter_id)
        return Response(letter.content.encode("utf-8-sig"), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="risk-letter-{letter.id}-v{letter.revision}.txt"'})


IMPORT_COLUMNS = ["org_code", "indicator_id", "data_date", "value", "unit", "rank", "population", "rank_scope", "value_encoding"]
ALIASES = {"机构代码": "org_code", "机构号": "org_code", "指标ID": "indicator_id", "指标编号": "indicator_id", "数据日期": "data_date", "指标值": "value", "单位": "unit", "排名": "rank", "全省排名": "rank", "参评数量": "population", "排名范围": "rank_scope", "数值编码": "value_encoding"}


@app.get("/api/import/template.csv")
def import_template():
    output = io.StringIO(newline="")
    csv.writer(output).writerow(IMPORT_COLUMNS)
    return Response(output.getvalue().encode("utf-8-sig"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="indicator-import-template.csv"'})


def parse_file(content, filename):
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".csv":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = content.decode("gb18030")
            except UnicodeDecodeError:
                raise HTTPException(422, "CSV编码无法识别，请保存为UTF-8")
        rows = list(csv.reader(io.StringIO(text)))
    elif suffix == ".xlsx":
        from openpyxl import load_workbook
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if sum(item.file_size for item in archive.infolist()) > 30 * 1024 * 1024:
                    raise HTTPException(413, "Excel解压后内容过大")
            book = load_workbook(io.BytesIO(content), read_only=True, data_only=False)
            if len(book.worksheets) != 1:
                book.close()
                raise HTTPException(422, "请将待导入的指标事实整理为单个工作表")
            rows = []
            for cells in book.worksheets[0].iter_rows():
                if any(cell.data_type == "f" for cell in cells):
                    book.close()
                    raise HTTPException(422, "导入不接受公式，请先转换为已核验的数值")
                if any(isinstance(cell.value, (int, float)) and "%" in (cell.number_format or "") for cell in cells):
                    book.close()
                    raise HTTPException(422, "请将百分比单元格转为普通数值（如1.65表示1.65%），并填percent_number编码")
                rows.append([cell.value for cell in cells])
                if len(rows) > 10001:
                    book.close()
                    raise HTTPException(413, "单次导入最多10000条记录")
            book.close()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(422, "Excel文件无法读取或格式不受支持")
    else:
        raise HTTPException(422, "仅支持CSV或XLSX文件")
    rows = [row for row in rows if any(value is not None and str(value).strip() for value in row)]
    if len(rows) < 2:
        raise HTTPException(422, "文件须含表头及至少一条指标事实")
    if len(rows) > 10001:
        raise HTTPException(413, "单次导入最多10000条记录")
    headers = [ALIASES.get(str(x).strip(), str(x).strip().lower()) for x in rows[0]]
    if len(headers) != len(set(headers)):
        raise HTTPException(422, "表头存在重复字段")
    missing = set(IMPORT_COLUMNS) - set(headers)
    if missing:
        raise HTTPException(422, "缺少字段：" + "、".join(sorted(missing)))
    return headers, rows[1:]


def optional_integer(value, label, row_number):
    if value is None or not str(value).strip():
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise HTTPException(422, f"第{row_number}行{label}必须为正整数")
    if not parsed.is_finite() or parsed != parsed.to_integral_value() or parsed < 1 or parsed > 100000:
        raise HTTPException(422, f"第{row_number}行{label}必须为正整数")
    return int(parsed)


@app.post("/api/import")
async def import_data(file: UploadFile = File(...), source_kind: str = Form("file")):
    if source_kind not in {"file", "database"}:
        raise HTTPException(422, "未知的数据来源类型")
    content = await file.read(5 * 1024 * 1024 + 1)
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(413, "单次文件大小最多5MB")
    headers, rows = parse_file(content, file.filename)
    with SessionLocal() as db:
        orgs = {o.code: o for o in db.scalars(select(Organization))}
        metrics = {i.id: i for i in db.scalars(select(Indicator))}
        parsed, seen = [], set()
        for position, row in enumerate(rows, 2):
            if len(row) > len(headers) and any(v is not None and str(v).strip() for v in row[len(headers):]):
                raise HTTPException(422, f"第{position}行列数超过表头")
            item = {h: row[i] if i < len(row) else "" for i, h in enumerate(headers)}
            org_code, indicator_id = str(item["org_code"] or "").strip(), str(item["indicator_id"] or "").strip()
            if org_code not in orgs or indicator_id not in metrics:
                raise HTTPException(422, f"第{position}行机构代码或指标ID不在目录中")
            key = (org_code, indicator_id)
            if key in seen:
                raise HTTPException(422, f"第{position}行存在重复机构与指标；一个快照每指标仅接受一个期次")
            seen.add(key)
            raw_date = item["data_date"]
            try:
                day = raw_date.date() if isinstance(raw_date, datetime) else raw_date if isinstance(raw_date, date) else date.fromisoformat(str(raw_date).strip())
            except (ValueError, TypeError):
                raise HTTPException(422, f"第{position}行数据日期须为YYYY-MM-DD")
            metric = metrics[indicator_id]
            unit = str(item["unit"] or "").strip()
            if unit == "CNY":
                unit = "元"
            if unit != metric.unit:
                raise HTTPException(422, f"第{position}行单位应为{metric.unit}，不能自动猜测换算")
            encoding = str(item["value_encoding"] or "").strip()
            expected = "percent_number" if metric.unit == "%" else "raw"
            if encoding != expected:
                raise HTTPException(422, f"第{position}行数值编码应为{expected}")
            raw_value = item["value"]
            try:
                value = None if raw_value is None or not str(raw_value).strip() else Decimal(str(raw_value).strip())
            except InvalidOperation:
                raise HTTPException(422, f"第{position}行指标值须为普通数值或留空")
            if value is not None and (not value.is_finite() or abs(value) >= Decimal("10000000000000000")):
                raise HTTPException(422, f"第{position}行指标值非有限数值或超出存储范围")
            if value is not None and value.as_tuple().exponent < -8:
                raise HTTPException(422, f"第{position}行指标值精度超过8位小数")
            rank = optional_integer(item["rank"], "排名", position)
            population = optional_integer(item["population"], "参评数量", position)
            if rank is not None and population is not None and rank > population:
                raise HTTPException(422, f"第{position}行排名不能大于参评数量")
            parsed.append({"org_id": orgs[org_code].id, "indicator_id": indicator_id, "data_date": day, "value": value, "unit": unit,
                           "rank": rank, "population": population, "rank_scope": str(item["rank_scope"] or "").strip(),
                           "quality_status": "missing" if value is None else "valid"})
        snap = Snapshot(name=f"导入 · {Path(file.filename or 'data').name[:120]}", data_date=max(p["data_date"] for p in parsed),
                        source="database_import" if source_kind == "database" else "file_import", is_demo=False, row_count=len(parsed), status="ready")
        db.add(snap)
        db.flush()
        for item in parsed:
            db.add(Observation(snapshot_id=snap.id, **item))
        audit(db, "snapshot_imported", snap.id, {"row_count": len(parsed), "local_test_only": True})
        db.commit()
        org_levels = {o.id: o.level for o in orgs.values()}
        monitored_orgs = len({p["org_id"] for p in parsed if org_levels[p["org_id"]] == 3})
        warnings = ["导入数据仅用于本地测试；缺失的机构或指标会记录为无法判断。"]
        if monitored_orgs < 82:
            warnings.append(f"本次包含{monitored_orgs}/82家行社的数据，未提供的数据不会当作零值。")
        return {"snapshot_id": snap.id, "row_count": len(parsed), "warnings": warnings}


@app.get("/api/audit")
def audit_logs():
    with SessionLocal() as db:
        return [{"id": a.id, "action": a.action, "entity_id": a.entity_id, "details": a.details, "created_at": timestamp(a.created_at)}
                for a in db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(100))]


DIST = ROOT / "frontend" / "dist"
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="frontend")
else:
    @app.get("/")
    def frontend_pending():
        return JSONResponse({"message": "前端尚未构建；请执行前端构建后重启服务。", "api_docs": "/docs"})

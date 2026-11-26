"""全辖扫描的业务口径与真实导入测试库端到端回归。"""
from pathlib import Path
import re
import sqlite3

import pytest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from test_api_workflow import good


def test_knowledge_catalog_and_missing_source():
    from backend.risk_knowledge import INDICATORS, get_risk_dimensions

    initial = [item for item in INDICATORS.values() if item.get("number")]
    assert len(initial) == 62
    assert len({item["indicator_id"] for item in initial}) == 62
    assert all(item["metric_kind"] in {"amount", "rate", "delta"} and item["role"] for item in initial)
    dims = {item["id"]: item for item in get_risk_dimensions()}
    assert set(dims) == {"credit_risk", "capital_and_buffer", "treasury_risk", "rural_micro_positioning", "key_business_mgmt"}
    assert not dims["key_business_mgmt"]["executable"]
    assert dims["key_business_mgmt"]["missing_source_count"] > 0
    assert dims["rural_micro_positioning"]["missing_source_count"] > 0


def test_weighted_ratio_and_missing_amount_are_not_averaged_or_filled(api):
    from backend.metric_validator import validate_fetched_metrics

    orgs = [{"org_id": "a", "org_name": "甲", "region_name": "东"},
            {"org_id": "b", "org_name": "乙", "region_name": "西"}]
    indicators = [{"indicator_id": k, "indicator_name": k, "unit": "%" if k == "rate" else "元"}
                  for k in ("num", "den", "rate")]
    records = []
    for org, num, den, rate in ((orgs[0], "1", "10", "10"), (orgs[1], "9", "90", "99")):
        for key, value in (("num", num), ("den", den), ("rate", rate)):
            records.append({**org, "indicator_id": key, "value": value, "unit": "%" if key == "rate" else "元",
                            "data_date": "2026-09-30", "quality_status": "valid", "usable": True, "issue": None})
    fetched = {"organizations": orgs, "indicators": indicators, "latest": records, "history": [],
               "missing": [], "missing_source": []}
    definitions = {"num": {"metric_kind": "amount", "unit": "元"}, "den": {"metric_kind": "amount", "unit": "元"},
                   "rate": {"metric_kind": "rate", "unit": "%", "numerator_id": "num", "denominator_id": "den"}}
    result = validate_fetched_metrics(fetched, definitions)
    detail = {row["indicator_id"]: row for row in result["accounting_details"]}
    assert detail["rate"]["weighted_value"] == "10"
    assert detail["rate"]["weighted_value"] != "54.5"
    assert detail["num"]["province_total"] == "10"
    records[0].update(value=None, usable=False, quality_status="missing", issue="数据缺失")
    result = validate_fetched_metrics(fetched, definitions)
    detail = {row["indicator_id"]: row for row in result["accounting_details"]}
    assert result["missing_count"] == 1
    assert detail["rate"]["weighted_value"] is None
    assert detail["num"]["province_total"] is None
    assert detail["num"]["partial_total"] == "9"


def test_letter_rules_and_citations_use_only_observed_facts():
    from types import SimpleNamespace
    from backend.analysis import AnalysisError
    from backend.risk_letter import (build_letter_brief, evaluate_letter_rules,
                                     fetch_org_metrics_for_case, render_structured_letter)

    def observation(metric, name, value, ref, **overrides):
        return dict({"org_id": "A", "indicator_id": metric, "indicator_name": name, "value": value,
                     "value_display": f"{value}%", "unit": "%", "evidence_id": ref, "usable": True,
                     "quality_status": "valid", "data_date": "2026-09-30", "rank": 3,
                     "population": 82, "rank_scope": "province_societies"}, **overrides)

    rows = [observation("TEST_METRIC_097", "拨备覆盖率【待核】", "149.99", "provision"),
            observation("TEST_METRIC_097", "拨备覆盖率", "180", "previous", data_date="2026-08-31"),
            observation("TEST_METRIC_091", "净利润同比增幅", "3", "profit"),
            observation("TEST_METRIC_073", "资金业务占总资产比重", "28", "funds"),
            observation("funds_npl_balance", "资金业务不良余额", "999", "bad", quality_status="missing"),
            observation("TEST_METRIC_015", "一级资本充足率", "5", "foreign", org_id="B")]
    case = SimpleNamespace(run_id="run", org_id="A", org_name="甲", region_name="浙北",
                           data_date="2026-09-30", evidence=rows, analysis={"signals": [
                               {"dimension_id": "treasury_risk", "indicator_id": "TEST_METRIC_073",
                                "aspect": "资金业务资产占比较高", "reason": "排名靠后", "evidence_id": "funds"}],
                               "issues": []})
    brief = build_letter_brief(case)
    assert set(brief) == {"run_id", "org_id", "org_name", "region_name", "data_date", "period_label",
                          "issue_date", "business_overview", "available_dimensions", "signal_overview",
                          "mandatory_p0_evidence_ids", "allowed_evidence_ids"}
    assert brief["mandatory_p0_evidence_ids"] == ["provision"]
    assert not {"bad", "foreign"}.intersection(brief["allowed_evidence_ids"])
    rules = evaluate_letter_rules(case)
    assert [c["evidence_id"] for c in rules["mandatory_p0_clues"]] == ["provision"]
    assert len(rules["cautions"]) == 2 and not rules["scan_signals"]
    facts = fetch_org_metrics_for_case(case, keyword="拨备", include_history=True)
    assert [(m["indicator_name"], m["value"]) for m in facts["metrics"]] == [("拨备覆盖率", "149.99")]
    assert {"meaning", "aspect", "dimension_title", "rank", "population"} <= facts["metrics"][0].keys()
    assert {"provision", "previous"} <= set(facts["evidence_ids"])

    section = {"heading": "拨备水平偏低", "body": "拨备覆盖率为149.99%。", "evidence_ids": ["provision"]}
    advice = {"heading": "落实拨备补提", "body": "测算拨备缺口并明确补提时点。"}
    letter = {"title": f"关于{brief['org_name']}{brief['period_label']}经营与风险情况的风险提示函",
              "recipient": brief["org_name"] + "：", "opening": "请关注拨备水平。", "overview": "",
              "risk_sections": [section], "suggestions": [advice, {"heading": "复核", "body": "逐季复核拨备。"}],
              "closing_requirement": "请主要负责人牵头建账，15个工作日内书面报送风险管理部。",
              "appendix_evidence_ids": ["provision"]}
    read = ["provision", "profit"]
    assert "| 拨备覆盖率 | 149.99% |" in render_structured_letter(case, letter, read)
    for update in ({"risk_sections": [{**section, "evidence_ids": ["profit"]}]},
                   {"appendix_evidence_ids": ["funds"]}, {"appendix_evidence_ids": ["simulated-value"]},
                   {"appendix_evidence_ids": ["foreign"]}, {"opening": "证据：" + "a" * 32},
                   {"risk_sections": []}, {"suggestions": [advice] * 6}, {"opening": "净利润下滑。"}):
        with pytest.raises(AnalysisError):
            render_structured_letter(case, {**letter, **update}, read)


def test_letter_renders_five_suggestions_and_closing_with_single_numbering():
    from backend.risk_letter import _render

    headings = ["（一） 核实余额", "2. 测算缺口", "(3) 落实责任", "四．跟踪整改", "1.5%预警线"]
    letter = {"title": "风险提示函", "recipient": "甲行：", "opening": "请关注以下事项。", "overview": "",
              "risk_sections": [{"heading": "一、拨备水平偏低", "body": "拨备有缺口。"}],
              "suggestions": [{"heading": heading, "body": "按月复核。"} for heading in headings],
              "closing_requirement": "（一）请按期报送。", "appendix_evidence_ids": []}

    paragraphs = _render({"issue_date": "2026年9月30日"}, letter, {}).split("\n\n")
    start = paragraphs.index("一、值得关注的主要风险与指标表现") + 1
    assert paragraphs[start:paragraphs.index("特此函告。")] == [
        "（一）拨备水平偏低。拨备有缺口。", "二、风险防范与整改工作建议",
        "（一）核实余额。按月复核。", "（二）测算缺口。按月复核。",
        "（三）落实责任。按月复核。", "（四）跟踪整改。按月复核。",
        "（五）1.5%预警线。按月复核。", "（六）请按期报送。",
    ]


def test_real_82_society_scan_and_follow_up(api, tmp_path, monkeypatch):
    """复制真实导入的合成库隔离写入；Pi 使用离线 demo 而非真实模型。"""
    from backend import app, conversation, data_fetcher, risk_scans, services
    from backend.models import AnalysisRun, LetterHistory, Observation, RiskCase

    source = Path(__file__).resolve().parents[1] / "data" / "risk-testdata.db"
    target = tmp_path / "scan.db"
    with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as original, sqlite3.connect(target) as copy:
        original.backup(copy)
    engine = create_engine(f"sqlite:///{target.as_posix()}", connect_args={"check_same_thread": False})
    sessions = sessionmaker(engine, expire_on_commit=False)
    # Pi 拉起独立的 stdio MCP 子进程，两端必须读取同一个扫描归档。
    monkeypatch.setenv("RISK_DATABASE_URL", str(engine.url))
    for module in (app, conversation, data_fetcher, risk_scans, services):
        monkeypatch.setattr(module, "SessionLocal", sessions)
    monkeypatch.setenv("RISK_PI_MODE", "demo")
    snapshot = "named-batch-2026-09-30"
    assert len(good(api.get("/api/risk-dimensions"))) == 5
    assert api.post("/api/risk-scans", json={"snapshot_id": snapshot, "org_id": "LS001"}).status_code == 422
    scan = good(api.post("/api/risk-scans", json={"snapshot_id": snapshot, "auto_generate_reports": False}))
    assert scan["status"] == "completed"
    assert scan["auto_reports"]["status"] == "idle"
    assert len(scan["society_results"]) == 82
    assert scan["validation_summary"]["total_org_count"] == 82
    assert scan["executive_summary"] and scan["trace"]
    assert scan["runtime"]["mode"] == "demo"
    assert good(api.get(f"/api/risk-scans/{scan['id']}"))["id"] == scan["id"]
    assert scan["id"] not in {row["id"] for row in good(api.get("/api/runs"))}
    assert (good(api.get("/api/bootstrap"))["latest_run"] or {}).get("id") != scan["id"]
    risk_societies = [row for row in scan["society_results"] if row["issues"]]
    assert risk_societies
    with sessions() as db:
        for society in risk_societies:
            for issue in society["issues"]:
                assert issue["evidence_ids"]
                rows = list(db.scalars(select(Observation).where(Observation.id.in_(issue["evidence_ids"]))))
                assert len(rows) == len(set(issue["evidence_ids"]))
                assert all(row.org_id == society["org_id"] and row.value is not None and row.quality_status == "valid" for row in rows)
    chosen = next(row for row in risk_societies if row['org_name'] == '鹿城')
    with sessions() as db:
        run = db.get(AnalysisRun, scan["id"])
        case = db.get(RiskCase, chosen["case_id"])
        fetched = risk_scans.fetched_from_scan_archive(db, run)
        assert {"organizations", "indicators", "latest", "history", "missing", "missing_source",
                "source_totals", "visibility"} <= fetched.keys()
        assert len(fetched["organizations"]) == 82
        assert all(row["snapshot_id"] == run.snapshot_id for row in fetched["latest"])
        assert any(row["snapshot_id"] != run.snapshot_id for row in fetched["history"])
        params = {"indicator_id": "npl_ratio", "bins": ["2"], "include_org_ids": True}
        from backend.verification_layer import run_verified_operator
        full = run_verified_operator(fetched, risk_scans.INDICATORS, "bucket_distribution", params,
                                     validation=scan["validation_summary"])
        focused = risk_scans.run_letter_verified_operator(db, run, case, "bucket_distribution", params)
        assert focused["status"] == "ok" and focused["coverage"]["total_orgs"] == 82
        assert all(focused[k] == full[k] for k in ("coverage", "summary", "checks_passed", "assumptions"))
        assert [row["count"] for row in focused["rows"]] == [row["count"] for row in full["rows"]]
        assert {key for row in focused["rows"] for key in row["org_ids"]} == {case.org_id}
        assert len(focused["evidence_ids"]) == 1 and focused["evidence_ids"][0] in {
            row["evidence_id"] for row in case.evidence if row["indicator_id"] == "npl_ratio"}
    endpoint = f"/api/risk-scans/{scan['id']}/societies/{chosen['org_id']}/draft"
    draft = good(api.post(endpoint))
    content = draft["content"]
    hangzhou = next(row for row in risk_societies if row['org_name'] == '杭州联合')
    other = good(api.post(f"/api/risk-scans/{scan['id']}/societies/{hangzhou['org_id']}/draft"))['content']
    for letter, points in [(content, ('拨备覆盖率低于150%', '资本充足率', '大额贷款不良余额', '补提', '第78名')),
                           (other, ('资金业务不良余额', '净息差低于1.8%', '大额贷款不良余额', '盈利增速', '第81名', '逐笔处置'))]:
        assert all(point in letter.split('附表：')[0] for point in points)
        assert not re.search(r'[0-9a-fA-F]{32}|TEST_METRIC_|成因分析：|核查建议：|证据：|原始指标记录：|待核】', letter)
    assert '资金业务' not in content and '净利润下滑' not in other
    assert '补提安排' not in other and '逐笔处置存量资金不良' not in content
    assert "风险提示函" in content.splitlines()[0]
    for required in ("一、值得关注的主要风险与指标表现", "二、风险防范与整改工作建议", "特此函告。",
                     "15个工作日内", "| 监测指标 | 本期实测 | 全辖排名 | 数据日期 |"):
        assert required in content
    for forbidden in (chosen["issues"][0]["evidence_ids"][0], "成因分析：", "核查建议：", "证据：", "原始指标记录：",
                      "（测试草稿）", "TEST_METRIC_", "待核】"):
        assert forbidden not in content
    assert not re.search(r"[0-9a-fA-F]{32}", content)
    assert good(api.post(endpoint)) == draft
    revised = good(api.post(endpoint + "?regenerate=true"))
    assert revised["id"] == draft["id"] and revised["revision"] == 2
    drafts = good(api.get(f"/api/risk-scans/{scan['id']}"))['letter_drafts']
    archived = good(api.get(f"/api/cases/{chosen['case_id']}"))['analysis']['letter_diagnostics']
    assert archived == drafts[chosen['org_id']] == revised['diagnostics']
    assert archived['revision'] == 2 and drafts[hangzhou['org_id']]['revision'] == 1
    assert archived['org_name'] == chosen['org_name'] and archived['runtime']['mode'] == 'demo'
    assert list(dict.fromkeys(event['tool'] for event in archived['trace'] if event.get('tool'))) == [
        'check_risk_rules', 'fetch_org_metrics', 'run_verified_operator', 'submit_risk_letter']
    from backend.analysis import AnalysisError
    def failed_draft(profile):
        raise AnalysisError('单行社撰函未通过证据校验')
    with monkeypatch.context() as scoped:
        scoped.setattr(risk_scans.provider, 'draft_risk_letter', failed_draft)
        failure = api.post(endpoint + '?regenerate=true')
        assert failure.status_code == 502 and '证据校验' in failure.json()['detail']
        assert good(api.post(endpoint)) == revised
    with sessions() as db:
        case = db.get(RiskCase, chosen["case_id"])
        assert case.status == "draft"
        assert any(row["value_display"] in content.split("附表：")[0] for row in case.evidence
                   if row["usable"] and row["data_date"] == str(case.data_date) and row["org_id"] == chosen["org_id"])
        digest = app.hash_json({"triggered": case.evidence, "queried": []})
        history = db.scalars(select(LetterHistory).where(LetterHistory.letter_id == draft["id"])
                             .order_by(LetterHistory.revision)).all()
        assert [(row.revision, row.content, row.evidence_hash) for row in history] == [
            (1, content, digest), (2, revised["content"], digest)]
    url = f"/api/agent/runs/{scan['id']}/conversation"
    assert good(api.post(url, json={"question": "本次全辖扫描发现了什么？"}))["answer"]
    turn = good(api.post(url, json={"case_id": chosen["case_id"], "question": "该行社有哪些风险证据？"}))
    assert turn["answer"] and turn["evidence"]
    assert len(good(api.get(url, params={"case_id": chosen["case_id"]}))["turns"]) == 1
    engine.dispose()


def test_scan_regeneration_cannot_overwrite_concurrent_approval(api, tmp_path, monkeypatch, request):
    from datetime import date
    from backend import risk_letter, risk_scans
    from backend.models import AnalysisRun, Base, Letter, LetterHistory, Organization, RiskCase, Snapshot

    engine = create_engine(f"sqlite:///{(tmp_path / 'approval.db').as_posix()}")
    request.addfinalizer(engine.dispose)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(risk_scans, "SessionLocal", sessions)
    with sessions() as db:
        snapshot = Snapshot(id="batch", name="批次", data_date=date(2026, 9, 30), source="test")
        org = Organization(id="A", code="A", name="甲行", level=3)
        db.add_all([snapshot, org])
        run = AnalysisRun(run_key="regen-approval-race", snapshot_id=snapshot.id, status="completed",
                          selection={"kind": "risk_scan", "risk_scan_result": {}})
        db.add(run)
        db.flush()
        case = RiskCase(run_id=run.id, org_id=org.id, org_name=org.name, region_name="甲地",
                        data_date=snapshot.data_date, status="draft", headline="拨备偏低",
                        evidence=[{"org_id": org.id, "snapshot_id": snapshot.id, "data_date": str(snapshot.data_date),
                                   "indicator_id": "TEST_METRIC_097", "indicator_name": "拨备覆盖率", "evidence_id": "p",
                                   "quality_status": "valid", "usable": True, "value": "140", "unit": "%",
                                   "value_display": "140%"}], analysis={"snapshot_id": snapshot.id})
        db.add(case)
        db.flush()
        letter = Letter(case_id=case.id, revision=1, status="draft", content="送审原稿", evidence_hash="old")
        db.add(letter)
        db.commit()
        url = f"/api/risk-scans/{run.id}/societies/{org.id}/draft"
        letter_id = letter.id

    def approve_while_composing(_):
        with sessions.begin() as other:
            accepted = other.get(Letter, letter_id)
            accepted.status, accepted.approved_revision = "approved", accepted.revision
        return "稍后返回的草稿", {}

    monkeypatch.setattr(risk_letter, "compose_risk_letter_with_trace", approve_while_composing)
    assert api.post(url + "?regenerate=true").status_code == 409
    with sessions() as db:
        stored = db.get(Letter, letter_id)
        assert (stored.status, stored.revision, stored.content) == ("approved", 1, "送审原稿")
        assert not db.scalars(select(LetterHistory).where(LetterHistory.letter_id == letter_id,
                                                          LetterHistory.revision == 2)).first()


def test_new_letter_dimension_from_json(tmp_path, monkeypatch):
    """仅增加 JSON 维度、指标及底线，即可从扫描目录一路进入撰函取证。"""
    import json
    import runpy
    import shutil
    from types import SimpleNamespace
    from backend import risk_knowledge, risk_letter

    module = Path(risk_knowledge.__file__)
    config_path = module.with_name("risk_knowledge_data.json")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["dimensions"]["liquidity_risk"] = {"title": "流动性风险", "description": "关注流动性覆盖。"}
    config["metrics"].append({"indicator_id": "liquidity_coverage", "name": "流动性覆盖率",
                              "dimension_id": "liquidity_risk", "aspect": "流动性缓冲", "role": "direct_judge",
                              "unit": "%", "metric_kind": "rate", "meaning": "反映对净现金流出的覆盖程度。",
                              "check_suggestion": "复核优质流动性资产和净现金流出。",
                              "reference_rules": [{"type": "threshold", "op": "<", "value": 100,
                                                   "label": "流动性覆盖不足"}]})
    config["letter_regulatory_floors"].append({"rule_id": "coverage_floor", "indicator_id": "liquidity_coverage",
                                               "label": "流动性覆盖不足",
                                               "when": {"indicator_id": "liquidity_coverage", "op": "<", "value": 100}})
    # 隔离加载一份扩展后的真实 JSON，避免修改工作目录或污染其他用例已导入的目录。
    isolated = tmp_path / module.name
    shutil.copyfile(module, isolated)
    (tmp_path / config_path.name).write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    catalog = SimpleNamespace(**runpy.run_path(str(isolated)))
    monkeypatch.setattr(risk_letter, "knowledge", catalog)
    dimensions = {d["dimension_id"]: d for d in catalog.get_risk_dimensions(["liquidity_coverage"])}
    assert dimensions["liquidity_risk"]["executable"]
    metric, = catalog.get_knowledge_pack(["liquidity_risk"])["indicators"]
    assert metric["reference_rules"][0]["label"] == "流动性覆盖不足"
    case = SimpleNamespace(run_id="run", org_id="A", org_name="甲银行", region_name="甲地", data_date="2026-09-30",
                           evidence=[{"org_id": "A", "data_date": "2026-09-30", "evidence_id": "liquid",
                                      "indicator_id": "liquidity_coverage", "indicator_name": "liquidity_coverage",
                                      "value": "98", "value_display": "证据：98%", "unit": "%",
                                      "quality_status": "valid", "usable": True}], analysis={})
    fact, = risk_letter.fetch_org_metrics_for_case(case, "liquidity_risk")["metrics"]
    assert (fact["indicator_name"], fact["dimension_title"], fact["meaning"]) == (
        "流动性覆盖率", "流动性风险", "反映对净现金流出的覆盖程度。")
    assert fact["value_display"] == "98%"
    clue, = risk_letter.evaluate_letter_rules(case)["mandatory_p0_clues"]
    assert clue["check_suggestion"] == metric["check_suggestion"]
    assert risk_letter.build_letter_brief(case)["mandatory_p0_evidence_ids"] == ["liquid"]

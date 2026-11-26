"""用本地签名 HTTP/SSE、离线 Pi 工具循环和独立归档库验证追问补查。"""
import json
import re
from types import SimpleNamespace

import pytest

from quickbi_protocol_cases import protocol_endpoint, stream_reply
from test_agent_conversation import archives
from test_api_workflow import good


@pytest.fixture
def scope(api, archives, monkeypatch):
    from backend.models import RiskCase, SessionLocal

    runs, cases = archives
    with SessionLocal() as db:
        db.get(RiskCase, cases["LS001"]).org_name = "甲农商银行"
        db.get(RiskCase, cases["LS002"]).org_name = "乙农商银行"
        db.commit()
    monkeypatch.setenv("RISK_SMARTQ_FOLLOWUP", "auto")
    monkeypatch.setenv("QB_SMARTQ_USER_ID", "mock-reader")
    monkeypatch.setenv("QB_SMARTQ_CUBE_ID", "mock-cube")
    monkeypatch.setenv("QB_SMARTQ_INFERENCE_LLM", "QwenTest")
    monkeypatch.setenv("QB_SMARTQ_DATA_LLM", "DataTEST")
    return f"/api/agent/runs/{runs['monitor']}/conversation", cases


def chart(*rows):
    return {"chartType": "table", "metaType": [{"k": key} for key in ("机构名称", "机构号", "表外担保余额")],
            "values": [{"row": row} for row in rows]}


def test_stream_from_gap_to_scoped_history(api, scope):
    url, cases = scope
    data = {"case_id": cases["LS001"], "question": "这家行社的表外担保余额是多少？"}
    def respond(_):
        return stream_reply(("message", f"message-{len(requests)}"), ("olapResult", chart(
            ["甲农商银行", "LS001", "31.25万元"], ["乙农商银行", "LS002", "9800万元"])),
            ("errorInfo", "sample-secret upstream detail"), ("finish", {}))

    with protocol_endpoint(respond) as (requests, _):
        assert good(api.get("/api/bootstrap"))["meta"]["smartq_followup_available"]
        archive = good(api.post(url, json={**data, "question": "不良贷款率是多少？"}))
        assert archive["evidence"][0]["id"] == "LS001" and not requests
        turn = good(api.post(url, json=data))
        meta = turn["smartq"]
        assert meta["status"] == "ok" and meta["trailing_error"] == "errorInfo"
        assert meta["query_type"] == "single" and meta["reason"] and "表外担保" in meta["metric_hint"]
        assert meta["target_date"] == "2026-10-28" and meta["target_org"] == "甲农商银行"
        assert meta["cube_id"] == "mock-cube" and meta["chart_type"] == "table"
        assert meta["filtered_row_count"] == 1 and meta["record_count"] == 1
        assert "甲农商银行" in meta["rewritten_question"] and "2026" in meta["rewritten_question"]
        assert "31.25" in turn["answer"] and "仅供参考" in turn["answer"]
        assert "9800" not in json.dumps(turn, ensure_ascii=False)
        assert "sample-secret" not in json.dumps(turn)
        record, = turn["evidence"]
        assert record["source"] == "smartq" and re.fullmatch(r"smartq:[0-9a-f]{16}", record["id"])
        assert [event["tool"] for event in turn["trace"] if event["event"] == "tool"] == [
            "read_task_evidence", "request_smartq_lookup", "read_task_evidence", "submit_follow_up"]
        assert turn["runtime"]["tool_calls"] == 4
        assert good(api.get(url, params={"case_id": data["case_id"]}))["turns"][-1] == turn
        assert good(api.get(url))["turns"] == []
        method, path, _, headers, body = requests[0]
        query = json.loads(body)
        assert (method, path) == ("POST", "/openapi/v2/smartq/queryByQuestionStream")
        assert headers["X-Gw-Signature"] and headers.get("Cookie") is None
        assert query["userQuestion"] == meta["rewritten_question"] and query["cubeId"] == meta["cube_id"]
        assert (query["llmNameForInference"], query["llmNameForData"]) == ("QwenTest", "DataTEST")
        follow = good(api.post(url, json={**data, "question": "它上个月末呢？"}))
        next_query = json.loads(requests[1][-1])
        assert next_query["chatId"] == query["chatId"] and next_query["appendedMessageId"] == "message-1"
        assert follow["smartq"]["target_date"] == "2026-09-30"
        assert "表外担保" in next_query["userQuestion"] and "甲农商银行" in next_query["userQuestion"]
        other = good(api.post(url, json={**data, "case_id": cases["LS002"]}))
        assert "9800" in other["answer"]
        other_query = json.loads(requests[2][-1])
        assert other_query["chatId"] != query["chatId"] and "appendedMessageId" not in other_query


def test_scope_violation_never_reaches_sse(api, scope):
    url, cases = scope
    with protocol_endpoint(lambda _: stream_reply(("finish", {}))) as (requests, _):
        turn = good(api.post(url, json={"case_id": cases["LS001"], "question": "乙农商银行的表外担保余额是多少？"}))
        assert not requests and turn["evidence"] == []
        assert turn["smartq"]["status"] != "ok"


@pytest.mark.parametrize("events,status", [
    ((("olapResult", chart()),), "empty"),
    ((("error", "sample-secret: upstream diagnostic"),), "error"),
])
def test_failed_and_empty_sse_are_honest(api, scope, events, status):
    url, cases = scope
    with protocol_endpoint(lambda _: stream_reply(*events)) as (requests, _):
        turn = good(api.post(url, json={"case_id": cases["LS001"], "question": "表外担保余额是多少？"}))
        assert len(requests) == 1 and turn["smartq"]["status"] == status
        assert turn["smartq"]["note"] and turn["smartq"]["record_count"] == 0
        assert not turn["evidence"] and not re.search(r"\d", turn["answer"])
        assert "sample-secret" not in json.dumps(turn)


def test_unavailable_and_per_question_opt_out(api, scope, monkeypatch):
    url, cases = scope
    with protocol_endpoint(lambda _: stream_reply()) as (requests, _):
        data = {"question": "表外担保余额是多少？", "case_id": cases["LS001"]}
        turn = good(api.post(url, json={**data, "allow_smartq": False}))
        assert not turn["evidence"] and "smartq" not in turn
        monkeypatch.delenv("QB_ACCESS_SECRET")
        monkeypatch.setenv("RISK_SMARTQ_FOLLOWUP", "on")
        assert not good(api.get("/api/bootstrap"))["meta"]["smartq_followup_available"]
        turn = good(api.post(url, json=data))
        assert turn["smartq"]["status"] == "unavailable" and turn["smartq"]["note"]
        assert turn["evidence"] == [] and not requests


def test_stable_identity_and_representative_row_limit():
    from backend.smartq_followup import execute_smartq_supplement

    case = SimpleNamespace(org_name="甲农商银行", org_id="LS001")
    lookup = dict(rewritten_question="甲农商银行的表外担保余额明细有哪些？", reason="缺少担保明细",
                  metric_hint="表外担保余额", query_type="detail", target_org=case.org_name, target_date="2026-02-28")
    context = {"as_of": "2026-02-28", "organization": case.org_name}
    rows = [[case.org_name, case.org_id, str(i)] for i in range(25)]
    rows.append([case.org_name, "LS002", "conflicting identity"])
    with protocol_endpoint(lambda _: stream_reply(("olapResult", chart(*rows)))):
        from unittest.mock import patch
        with patch.dict("os.environ", {"QB_SMARTQ_USER_ID": "reader", "QB_SMARTQ_CUBE_ID": "cube",
                                       "RISK_SMARTQ_FOLLOWUP": "on"}):
            records, info = execute_smartq_supplement(lookup, context, case)
            again, _ = execute_smartq_supplement(lookup, context, case)
    assert len(records) == len({r["id"] for r in records}) == 20
    assert records == again and info["filtered_row_count"] == 1


def test_dictionary_cube_and_case_sensitive_models(tmp_path, monkeypatch):
    from backend.smartq_followup import smartq_followup_config

    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({"defaultCubeId": "main-cube", "metrics": {
        "担保余额": {"source": {"cubeId": "detail-cube"}, "synonyms": ["表外担保余额"]}}}), encoding="utf-8")
    monkeypatch.delenv("QB_SMARTQ_CUBE_ID", raising=False)
    monkeypatch.delenv("QB_SMARTQ_USER_ID", raising=False)
    for key, value in {"RISK_SMARTQ_FOLLOWUP": "auto", "QB_HOST": "http://127.0.0.1",
                       "QB_ACCESS_ID": "sample-id", "QB_ACCESS_SECRET": "sample-secret", "QB_USER_ID": "reader",
                       "RISK_QUICKBI_DICT_FILE": str(path), "QB_SMARTQ_DATA_LLM": "MixedCASE"}.items():
        monkeypatch.setenv(key, value)
    config = smartq_followup_config("表外担保余额")
    assert config["enabled"] and config["cube_id"] == "detail-cube"
    assert config["data_llm"] == "MixedCASE" and config["user_id"] == "reader"
    assert smartq_followup_config("未入字典")["cube_id"] == "main-cube"
    monkeypatch.setenv("RISK_SMARTQ_FOLLOWUP", "off")
    assert not smartq_followup_config()["enabled"]


def test_scoped_province_baseline_names_and_bare_org_codes(monkeypatch):
    from backend.smartq_followup import execute_smartq_supplement

    case = SimpleNamespace(org_name="杭州联合", org_id="TEST_LS_001")
    context = {"as_of": "2026-02-28", "scope_organizations": [
        {"org_name": case.org_name, "org_id": case.org_id, "org_code": "801000"},
        {"org_name": "余杭", "org_id": "TEST_LS_002", "org_code": "802000"},
        {"org_name": "全省合计", "org_id": "province", "org_code": "999999"},
        {"org_name": "宁波", "org_id": "region", "org_code": "202602"},
    ]}
    lookup = dict(rewritten_question="202602月末浙江杭州联合农村商业银行股份有限公司与全省合计（含宁波）"
                                     "的表外余额相差多少？", reason="归档缺少指标", metric_hint="表外余额",
                  query_type="diff", target_org=case.org_name, target_date="2026-02-28")
    result = chart(["浙江杭州联合农村商业银行股份有限公司", "801000", "10"],
                   ["全省合计", "999999", "100"], ["余杭", "802000", "75"])
    monkeypatch.setenv("RISK_SMARTQ_FOLLOWUP", "on")
    monkeypatch.setenv("QB_SMARTQ_CUBE_ID", "cube")
    monkeypatch.setenv("QB_SMARTQ_USER_ID", "reader")
    monkeypatch.delenv("RISK_QUICKBI_ORG_FILE", raising=False)
    with protocol_endpoint(lambda _: stream_reply(("olapResult", result))) as (requests, _):
        evidence, meta = execute_smartq_supplement(lookup, context, case)
        assert meta["record_count"] == 2 and meta["filtered_row_count"] == 1
        assert len(evidence) == 2 and "全省合计" in evidence[1]["text"]
        for foreign in ["余杭的表外余额", "802000的表外余额", "机构号为202602的表外余额", "999999的表外余额"]:
            evidence, meta = execute_smartq_supplement(lookup, {**context, "original_question": foreign}, case)
            assert not evidence and meta["error_code"] == "SMARTQ_SCOPE"
        assert len(requests) == 1


def test_smartq_domain_guidance_and_filter_screening(monkeypatch):
    """知识库自动按问题匹配监管底线/Excel单项问句，全辖阈值筛查保持全部行社范围并走通端到端补查。"""
    from backend.smartq_followup import (
        build_smartq_domain_guidance, execute_smartq_supplement, load_smartq_question_bank,
    )

    bank = load_smartq_question_bank()
    assert len(bank["threshold_rules"]) >= 20
    assert len(bank["excel_templates"]) >= 80

    guidance = build_smartq_domain_guidance("哪些行社不良率偏高或流动性不足？", {"as_of": "2026-09-30"})
    assert "五级不良贷款率" in guidance and "＞1.5%" in guidance
    assert "流动性比例" in guidance and "＜25%" in guidance
    assert "2026年09月末" in guidance

    lookup = {
        "rewritten_question": "2026年09月末，全部行社中五级不良贷款率＞1.5%的行社是哪几家，分别是多少？",
        "reason": "参照银行业不良率 1.5% 行业参考预警线自主补充筛查条件。",
        "metric_hint": "五级不良贷款率",
        "target_org": "全部行社",
        "target_date": "2026-09-30",
        "query_type": "filter",
    }
    chart = {"chartType": "DETAIL_TABLE", "metaType": [{"k": "机构名称"}, {"k": "五级不良贷款率"}],
             "values": [{"row": ["甲农商银行", "1.82%"]}, {"row": ["乙农商银行", "2.15%"]}]}
    monkeypatch.setenv("RISK_SMARTQ_FOLLOWUP", "on")
    monkeypatch.setenv("QB_SMARTQ_CUBE_ID", "cube-npl")
    monkeypatch.setenv("QB_SMARTQ_USER_ID", "reader")
    with protocol_endpoint(lambda _: stream_reply(("olapResult", chart))) as (requests, _):
        evidence, meta = execute_smartq_supplement(lookup, {"as_of": "2026-09-30"})
        assert meta["status"] == "ok" and meta["query_type"] == "filter"
        assert meta["target_org"] == "全部行社" and meta["record_count"] == 2
        assert not meta["rewritten_question"].startswith("全省合计")
        assert len(evidence) == 2 and "甲农商银行" in evidence[0]["text"]
        assert len(requests) == 1

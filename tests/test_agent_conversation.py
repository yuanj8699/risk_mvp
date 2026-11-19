"""验证 GUI 追问只消费当前归档、续接同范围历史，并在越界引用时不写入。"""
from datetime import date

import pytest

from test_api_workflow import api, good


@pytest.fixture
def archives(api):
    from backend.models import AnalysisRun, RiskCase, SessionLocal, uid

    runs, cases = {}, {}
    with SessionLocal() as db:
        for kind in ("monitor", "code", "empty"):
            scope = ["LS001", "LS002"]
            context = {"topic": "信用风险", "org_scope": scope, "as_of": "2026-10-28"}
            selection = {"task_context": context}
            rows = [{"id": org, "evidence_id": org, "org_id": org, "indicator_name": "不良贷款率",
                     "data_date": "2026-10-28", "value": "1.50", "value_display": "1.50%"} for org in scope]
            if kind == "code":
                selection.update(kind="code", code_result={"evidence": rows, "findings": [
                    {"org_id": "LS001", "statement": "脚本留存的发现", "evidence_ids": ["LS001"]}]})
            run = AnalysisRun(run_key=uid(), status="completed", snapshot_id="demo-2026-10-28",
                              selection=selection, summary={"case_count": 2 if kind != "empty" else 0,
                                                            "insufficient": 1})
            db.add(run)
            db.flush()
            runs[kind] = run.id
            if kind == "monitor":
                for row in rows:
                    case = RiskCase(run_id=run.id, org_id=row["org_id"], org_name=row["org_id"],
                                    region_name="地区", data_date=date(2026, 10, 28), headline="排名提醒",
                                    evidence=[dict(row, reason="全省排名达到阈值")],
                                    analysis={"queried_evidence": [dict(row, evidence_id="unusable", usable=False)]})
                    db.add(case)
                    db.flush()
                    cases[row["org_id"]] = case.id
        db.commit()
    return runs, cases


def test_scope_history_and_failed_answer(api, archives, monkeypatch):
    from backend.analysis import provider

    runs, cases = archives
    requests = []
    cited = "LS001"

    def scripted_model(payload):
        requests.append(payload)
        return {"answer": "档案显示不良率为 1.50%。", "evidence_ids": [cited],
                "trace": [], "runtime": {"mode": "demo"}}

    monkeypatch.setattr(provider, "_request", scripted_model)
    url = f"/api/agent/runs/{runs['monitor']}/conversation"
    question = {"case_id": cases["LS001"], "question": "有哪些证据？"}
    turn = good(api.post(url, json=question))
    assert turn["mode"] == "pi-demo" and turn["evidence"][0]["id"] == "LS001"
    assert "全省排名达到阈值" in turn["evidence"][0]["text"]
    assert [e["id"] for e in requests[-1]["evidence"]] == ["LS001"]
    assert requests[-1]["history"] == []
    good(api.post(url, json=dict(question, question="之前的指标是哪一期？")))
    assert requests[-1]["history"] == [{"question": question["question"], "answer": turn["answer"]}]
    cited = "LS002"
    assert api.post(url, json=question).status_code == 502
    assert len(good(api.get(url, params={"case_id": cases["LS001"]}))["turns"]) == 2
    assert good(api.get(url, params={"case_id": cases["LS002"]}))["turns"] == []
    assert good(api.get(url))["turns"] == []
    assert api.post(f"/api/agent/runs/{runs['empty']}/conversation", json=question).status_code == 404
    assert api.post(url, json=dict(question, question=" ")).status_code == 422


def test_completed_code_findings_and_missing_data_summary(api, archives, monkeypatch):
    from backend.analysis import provider

    runs, _ = archives
    def scripted_model(payload):
        record = next(r for r in payload["evidence"] if r["id"].startswith(("finding:", "summary:")))
        return {"answer": record["text"], "evidence_ids": [record["id"]], "trace": [], "runtime": {"mode": "model"}}
    monkeypatch.setattr(provider, "_request", scripted_model)
    for kind, expected in (("empty", "数据不足项数 1"), ("code", "脚本留存的发现")):
        url = f"/api/agent/runs/{runs[kind]}/conversation"
        turn = good(api.post(url, json={"question": "这次检查发现了什么？"}))
        assert expected in turn["answer"] and turn["mode"] == "pi-model"
        assert good(api.get(url))["turns"] == [turn]


def test_follow_up_through_pi_process(api, archives):
    """真实 Pi 工具循环经 HTTP 返回归档值与来源，模型流使用项目默认的离线替身。"""
    runs, cases = archives
    url = f"/api/agent/runs/{runs['monitor']}/conversation"
    turn = good(api.post(url, json={"case_id": cases["LS002"], "question": "本次不良贷款率是多少？"}))
    assert "1.50%" in turn["answer"] and [record["id"] for record in turn["evidence"]] == ["LS002"]
    assert turn["runtime"]["framework"] == "pi-agent-core" and turn["runtime"]["llm_connected"] is False
    assert turn["runtime"]["tool_calls"] >= 2 and turn["trace"]

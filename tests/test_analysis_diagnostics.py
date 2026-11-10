"""Pi诊断协议与任务持久化；真实Node进程输出固定协议样本，不调用外部模型或Docker。"""
import json

import pytest


DIAGNOSTICS = {
    "trace": [{"event": "request", "id": "request-1", "case_key": "LS001", "status": "error",
               "error": "测试模型请求失败", "reasoning_content": "供应商返回的测试思考内容",
               "reasoning_source": "provider", "usage": {"input": 10, "output": 2}}],
    "runtime": {"framework": "pi-agent-core", "requests": 1, "tool_calls": 0, "llm_connected": False},
    "truncated": False,
}


def use_node_response(tmp_path, monkeypatch, response, exit_code=0):
    from backend import analysis

    script = tmp_path / "protocol-sample.mjs"
    output = json.dumps(response, ensure_ascii=False)
    script.write_text("for await (const chunk of process.stdin) {}\n"
                      f"process.stdout.write({json.dumps(output)});\n"
                      f"process.exitCode = {exit_code};\n", encoding="utf-8")
    monkeypatch.setattr(analysis, "RUNNER_PATH", script)


def test_node_failure_envelope_preserves_trace_runtime_and_original_error(tmp_path, monkeypatch):
    from backend.analysis import AnalysisError, provider

    use_node_response(tmp_path, monkeypatch, {
        "error": {"code": "TEST_PROVIDER_ERROR", "message": "测试供应商失败"},
        "diagnostics": DIAGNOSTICS,
    }, exit_code=1)
    with pytest.raises(AnalysisError, match="测试供应商失败") as failure:
        provider._request({"operation": "select"}, budget_ms=5000)
    assert failure.value.diagnostics == DIAGNOSTICS


def test_node_success_preserves_diagnostics_without_changing_business_result(tmp_path, monkeypatch):
    from backend.analysis import provider

    result = {"indicator_ids": ["npl_ratio"], "reasons": ["必查指标"], **DIAGNOSTICS}
    use_node_response(tmp_path, monkeypatch, result)
    assert provider.select("信用风险", [{"id": "npl_ratio", "name": "不良贷款率"}], ["npl_ratio"]) == result


@pytest.mark.parametrize("diagnostics,reason", [
    ({"trace": "invalid"}, "trace必须为数组"),
    ({"trace": [{}] * 1001}, "不超过1000条"),
    ({"trace": [], "runtime": []}, "runtime必须为对象"),
    ({"trace": [], "truncated": "yes"}, "truncated必须为布尔值"),
    ({"trace": [{"reasoning_content": "x" * (1024 * 1024)}]}, "超过1MiB"),
])
def test_invalid_node_diagnostics_report_protocol_failure_and_keep_root_error(tmp_path, monkeypatch, diagnostics, reason):
    from backend.analysis import AnalysisError, provider

    use_node_response(tmp_path, monkeypatch, {
        "error": {"code": "TEST_ERROR", "message": "原始测试错误"}, "diagnostics": diagnostics,
    }, exit_code=1)
    with pytest.raises(AnalysisError, match=reason) as failure:
        provider._request({"operation": "select"}, budget_ms=5000)
    assert "原始测试错误" in str(failure.value)
    assert failure.value.diagnostics is None


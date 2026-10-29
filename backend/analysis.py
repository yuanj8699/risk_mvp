"""Bounded, explicit offline analysis and the model integration boundary.

This version never claims a real LLM connection. A XiaoQ provider can implement
AnalysisProvider after its actual request/response protocol is confirmed.
"""
from typing import Protocol


class AnalysisProvider(Protocol):
    def select(self, topic: str, indicators: list[dict], mandatory_ids: list[str]) -> dict: ...
    def explain(self, evidence: list[dict], supporting: list[dict]) -> dict: ...


class TemplateAnalysisProvider:
    mode = "template"

    def select(self, topic, indicators, mandatory_ids):
        selected = set(mandatory_ids)
        reasons = ["所有已启用且适用的规则均执行；主题筛选不删除必查指标。"]
        if any(word in topic for word in ("存款", "扩张", "流动")):
            selected.update(("deposit_growth", "pure_deposit_growth"))
            reasons.append("主题包含存款相关词，补充纯存款增幅作为辅助证据。")
        if any(word in topic for word in ("资金", "不良", "风险", "贷款", "信用")) or not topic:
            selected.update(("npl_ratio", "overdue_ratio", "new_npl_ratio", "funds_npl_change", "funds_npl_ratio"))
            reasons.append("按预置主题映射补充信用风险与资金业务指标。")
        allowed = {x["id"] for x in indicators}
        return {"mode": "template", "topic": topic, "indicator_ids": sorted(selected & allowed), "reasons": reasons,
                "limitations": ["当前使用目录匹配和模板分析，尚未接入真实大模型。", "小Q接口协议待提供；该模式不会自动产生新的阈值。"]}

    def explain(self, evidence, supporting):
        facts = [f"{e['indicator_name']}为{e['value_display']}；{e['reason']}。" for e in evidence]
        negative_change = next((x for x in supporting if x["indicator_id"] == "funds_npl_change" and x.get("value") is not None and float(x["value"]) < 0), None)
        if negative_change:
            facts.append(f"资金业务不良余额比年初增量为{negative_change['value_display']}，表示较年初减少；不能据此写成风险上升。")
        kinds = {e["rule_kind"] for e in evidence}
        checks = []
        if "rank" in kinds:
            checks.append("复核排名的参评范围、指标方向及源数据，与同口径历史值比较。")
        if "ratio" in kinds:
            checks.append("复核占比指标的分子、分母和阈值适用性，核查变动构成。")
        if "exists" in kinds:
            checks.append("核查资金业务不良余额的明细构成、处置进展及数据期次。")
        return {"mode": "template", "facts": facts,
                "hypotheses": ["仅凭汇总指标无法确认异常原因或责任；需结合行社明细和业务说明核查。"],
                "checks": checks,
                "limitations": ["内置数值与机构均为合成测试数据。", "使用通用测试函件模板，尚未替换行内正式模板。", "规则计算已执行；当前说明由模板生成，未调用小Q或其他大模型。"]}


provider: AnalysisProvider = TemplateAnalysisProvider()

"""自主分析的宿主业务入口：MCP读取与容器导出共用任务额度，内部辅助读取不重复计费。"""
from __future__ import annotations

from . import services
from .models import AnalysisRun
from .query_budget import business_operation


def read_operation(context, operation, action, *, cancelled=None):
    # 原规则监测没有代码执行能力；本次共享额度约束自主分析任务的两条取数路径。
    with services.read_session() as db:
        run = db.get(AnalysisRun, context["run_id"])
        is_code = run is not None and run.selection.get("kind") == "code"
    if not is_code:
        return action(context)
    with business_operation(context["run_id"], operation, cancelled=cancelled) as ticket:
        ticket.check_cancelled()
        result = action(context)
        ticket.check_cancelled()
        return result


def export_operation(context, indicator_ids, org_ids=None, history=True, *, cancelled=None):
    from .task_workspace import export_dataset
    with business_operation(context["run_id"], "export_metrics", cancelled=cancelled) as ticket:
        result = export_dataset(context, indicator_ids, org_ids, history, cancelled=cancelled, budget=ticket)
        ticket.check_cancelled()
        return result

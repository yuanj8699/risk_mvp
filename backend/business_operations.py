"""自主分析的宿主业务入口：MCP读取与容器导出共用任务额度，内部辅助读取不重复计费。"""
from __future__ import annotations

from contextlib import contextmanager

from . import services
from .models import AnalysisRun
from .query_budget import business_operation


@contextmanager
def read_operation_scope(context, operation, *, cancelled=None):
    """调用边界持有票据；MCP可在参数校验前准入，内部读取复用同一份额度。"""
    # 撰函与代码分析均对模型开放主动取数；沿用同一任务预算与并发票据。
    with services.read_session() as db:
        run = db.get(AnalysisRun, context["run_id"])
        budgeted = run is not None and run.selection.get("kind") in {"code", "risk_scan"}
    if not budgeted:
        yield
        return
    with business_operation(context["run_id"], operation, cancelled=cancelled) as ticket:
        ticket.check_cancelled()
        yield
        ticket.check_cancelled()


def read_operation(context, operation, action, *, cancelled=None):
    with read_operation_scope(context, operation, cancelled=cancelled):
        return action(context)


def export_operation(context, indicator_ids, org_ids=None, history=True, *, cancelled=None):
    from .task_workspace import export_dataset
    with business_operation(context["run_id"], "export_metrics", cancelled=cancelled) as ticket:
        result = export_dataset(context, indicator_ids, org_ids, history, cancelled=cancelled, budget=ticket)
        ticket.check_cancelled()
        return result

"""Pi启动的只读业务MCP进程；任务/行社范围由宿主环境绑定，不接受模型改写。

启动：后端Python解释器 -m backend.mcp_server。stdin/stdout专用于MCP协议，EOF退出。
不导入app，不建表/初始化种子，也不连接外部RISK_SOURCE_DATABASE_URL。
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import os
import threading

from anyio import CancelScope
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import Middleware

from . import services
from .business_operations import read_operation, read_operation_scope
from .query_budget import BudgetError

mcp = FastMCP("risk-business", mask_error_details=True, strict_input_validation=True)
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
READ_OPERATIONS = {"search_indicators", "query_metrics", "check_risks", "get_letter_template"}
_request_cancelled: ContextVar[threading.Event | None] = ContextVar("mcp_read_cancelled", default=None)


def _context() -> dict:
    run_id = os.getenv("RISK_TASK_RUN_ID")
    if not run_id:
        raise services.ServiceError("SOURCE_ERROR", "MCP未绑定任务身份，无法读取业务数据")
    return services.load_task_context(run_id, os.getenv("RISK_TASK_ORG_ID") or None)


class ReadBudgetMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        operation = context.message.name
        if operation not in READ_OPERATIONS:
            return await call_next(context)
        cancelled = threading.Event()
        token = _request_cancelled.set(cancelled)
        try:
            # 严格参数校验属于请求的一部分；失败也扣费，工具线程复用此票据。
            with read_operation_scope(_context(), operation, cancelled=cancelled.is_set):
                return await call_next(context)
        except (services.ServiceError, BudgetError) as exc:
            raise ToolError(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False)) from None
        finally:
            _request_cancelled.reset(token)


mcp.add_middleware(ReadBudgetMiddleware())


def _call_sync(operation, action, cancelled):
    """已知业务错误供模型纠正；其他异常由FastMCP屏蔽，避免泄露连接信息。"""
    try:
        return read_operation(_context(), operation, action, cancelled=cancelled)
    except (services.ServiceError, BudgetError) as exc:
        raise ToolError(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False)) from None


async def _call(operation, action):
    """MCP取消传到实际查询；等待工作线程退出后才让请求结束、释放并发占位。"""
    cancelled = _request_cancelled.get() or threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(_call_sync, operation, action, cancelled.is_set))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancelled.set()
        # MCP使用AnyIO级联取消；仅asyncio.shield仍会在下一次await再次被取消。
        with CancelScope(shield=True):
            try:
                await asyncio.shield(worker)
            except ToolError:
                pass  # 查询已确认退出，取消路径不再向已关闭请求发送业务错误。
        raise


@mcp.tool(annotations=READ_ONLY)
async def search_indicators(query: str = "", limit: int = 50, offset: int = 0) -> dict:
    """查指标目录、口径、单位、频率与排名信息；返回明确分页，不修改监测规则。"""
    return await _call("search_indicators", lambda _: services.search_indicators(query, limit, offset))


@mcp.tool(annotations=READ_ONLY)
async def query_metrics(indicator_ids: list[str], org_ids: list[str] | None = None,
                  history: bool = False, limit: int = 100, offset: int = 0) -> dict:
    """在固定任务内批量取证。history=False取截至日最新期次，True返回去重历史。

    org_ids默认宿主绑定机构。仅引用usable=true且quality_status=valid的实际观测；
    返回evidence_id、实际日期、原始十进制值、快照及分页覆盖，不把缺数视为零。
    """
    return await _call("query_metrics", lambda context: services.query_metrics(context, indicator_ids, org_ids, history, limit, offset))


@mcp.tool(annotations=READ_ONLY)
async def check_risks() -> dict:
    """按本次固定规则检测选定快照，返回检查明细和触发证据；不创建任务或Agent。"""
    def checked(context):
        result = services.check_risks(context)
        if os.getenv("RISK_CODEMODE") == "1":
            from .query_plans import small_risk_check
            return small_risk_check(context, result)
        return result
    return await _call("check_risks", checked)


@mcp.tool(annotations=READ_ONLY)
async def get_letter_template(version: str = "test-v1") -> dict:
    """读取指定版本函件模板与填充字段，不生成、审核或发送函件。"""
    return await _call("get_letter_template", lambda _: services.get_letter_template(version))


def _code_call(action):
    """代码工具仅允许宿主创建的运行中自主分析任务，模型不能把监测任务升级。"""
    from .models import AnalysisRun
    from .task_workspace import WorkspaceError
    try:
        context = _context()
        with services.read_session() as db:
            run = db.get(AnalysisRun, context["run_id"])
            if run.selection.get("kind") != "code" or run.status != "running":
                raise services.ServiceError("SCOPE_VIOLATION", "任务未授权代码执行或已结束")
        return action(context["run_id"])
    except (services.ServiceError, WorkspaceError, BudgetError) as exc:
        raise ToolError(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False)) from None


@mcp.tool()
def register_query_plan(plan: dict, check_receipt_id: str | None = None) -> dict:
    """宿主内部登记计划；仅Node适配器调用，不向Pi或JS注入此能力。"""
    from .query_plans import register_query_plan as register
    return _code_call(lambda run_id: register(run_id, plan, check_receipt_id))


@mcp.tool()
def write_task_file(path: str, content: str) -> dict:
    """在本任务工作区创建或修改UTF-8脚本/结果；不能写输入数据或宿主其他目录。"""
    from .task_workspace import write_task_file as write
    return _code_call(lambda run_id: write(run_id, path, content))


@mcp.tool(annotations=READ_ONLY)
def read_task_file(path: str, max_chars: int = 12000) -> dict:
    """读取少量脚本、结果摘要或错误信息；大数据留在Parquet，不直接进入模型上下文。"""
    from .task_workspace import read_task_file as read
    if not 1 <= max_chars <= 32000:
        raise ToolError('max_chars须为1至32000')
    def action(run_id):
        file = read(run_id, path)
        return {**file, "content": file["content"][:max_chars], "truncated": len(file["content"]) > max_chars}
    return _code_call(action)


@mcp.tool(annotations=READ_ONLY)
def list_task_files() -> dict:
    """列出本任务的脚本与输出文件摘要，不列出数据库、密钥或宿主执行记录。"""
    from .task_workspace import list_task_files as files
    return _code_call(lambda run_id: {"files": files(run_id)})


@mcp.tool()
def execute_python(script_path: str, timeout_seconds: int = 60) -> dict:
    """在无网络Docker容器运行任务Python脚本，返回退出码、限长日志及产物清单。

    脚本可用risk_client.export_metrics从宿主只读查询服务导出Parquet到/data，
    再用pandas.read_parquet分析。非零退出码含实际错误，允许修正脚本后再次执行。
    """
    from .code_runner import execute_python as execute
    return _code_call(lambda run_id: execute(run_id, script_path, timeout_seconds))


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)

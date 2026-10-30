"""Pi启动的只读业务MCP进程；任务/行社范围由宿主环境绑定，不接受模型改写。

启动：后端Python解释器 -m backend.mcp_server。stdin/stdout专用于MCP协议，EOF退出。
不导入app，不建表/初始化种子，也不连接外部RISK_SOURCE_DATABASE_URL。
"""
from __future__ import annotations

import json
import os

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import services

mcp = FastMCP("risk-business", mask_error_details=True, strict_input_validation=True)
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


def _context() -> dict:
    run_id = os.getenv("RISK_TASK_RUN_ID")
    if not run_id:
        raise services.ServiceError("SOURCE_ERROR", "MCP未绑定任务身份，无法读取业务数据")
    return services.load_task_context(run_id, os.getenv("RISK_TASK_ORG_ID") or None)


def _call(action):
    """已知业务错误供模型纠正；其他异常由FastMCP屏蔽，避免泄露连接信息。"""
    try:
        return action(_context())
    except services.ServiceError as exc:
        raise ToolError(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False)) from None


@mcp.tool(annotations=READ_ONLY)
def search_indicators(query: str = "", limit: int = 50, offset: int = 0) -> dict:
    """查指标目录、口径、单位、频率与排名信息；返回明确分页，不修改监测规则。"""
    return _call(lambda _: services.search_indicators(query, limit, offset))


@mcp.tool(annotations=READ_ONLY)
def query_metrics(indicator_ids: list[str], org_ids: list[str] | None = None,
                  history: bool = False, limit: int = 100, offset: int = 0) -> dict:
    """在固定任务内批量取证。history=False取截至日最新期次，True返回去重历史。

    org_ids默认宿主绑定机构。仅引用usable=true且quality_status=valid的实际观测；
    返回evidence_id、实际日期、原始十进制值、快照及分页覆盖，不把缺数视为零。
    """
    return _call(lambda context: services.query_metrics(context, indicator_ids, org_ids, history, limit, offset))


@mcp.tool(annotations=READ_ONLY)
def check_risks() -> dict:
    """按本次固定规则检测选定快照，返回检查明细和触发证据；不创建任务或Agent。"""
    return _call(services.check_risks)


@mcp.tool(annotations=READ_ONLY)
def get_letter_template(version: str = "test-v1") -> dict:
    """读取指定版本函件模板与填充字段，不生成、审核或发送函件。"""
    return _call(lambda _: services.get_letter_template(version))


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
    except (services.ServiceError, WorkspaceError) as exc:
        raise ToolError(json.dumps({"error": {"code": exc.code, "message": str(exc)}}, ensure_ascii=False)) from None


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

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


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)

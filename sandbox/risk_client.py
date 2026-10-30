"""容器内只读取数客户端：通过stdio请求宿主导出，返回Parquet路径和摘要。

不包含数据库连接、API密钥或网络功能；实际取数范围由宿主任务上下文决定。
"""
import json
import sys
import threading
from uuid import uuid4

PREFIX = "__RISK_RPC_V1__:"
_lock = threading.Lock()


def export_metrics(indicator_ids, org_ids=None, history=True):
    """导出本任务指标；pd.read_parquet(result['path'])读取，value列保持十进制字符串。"""
    request_id = uuid4().hex
    request = {"id": request_id, "method": "export_metrics", "params": {
        "indicator_ids": indicator_ids, "org_ids": org_ids, "history": history,
    }}
    with _lock:
        sys.stdout.write(PREFIX + json.dumps(request, ensure_ascii=False) + "\n")
        sys.stdout.flush()
        raw = sys.stdin.readline()
        if not raw:
            raise RuntimeError("宿主只读数据服务已关闭")
        response = json.loads(raw)
        if response.get("id") != request_id:
            raise RuntimeError("只读数据服务响应身份不匹配")
        if "error" in response:
            raise RuntimeError(response["error"]["code"] + ": " + response["error"]["message"])
        return response["result"]

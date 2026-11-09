"""共享查询额度：短事务原子准入，事务外执行，完成后释放跨进程占位。

这里只记账，不扩大任务数据范围；调用方继续使用load_task_context授权。
SQLite使用WAL与BEGIN IMMEDIATE，PostgreSQL锁单例gate行；后者尚未实库联调。
"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import errno
import math
import os
import time

from sqlalchemy import func, select, text

from .models import AnalysisRun, QueryBudgetLedger, QueryOperation, SessionLocal, uid


class BudgetError(ValueError):
    """稳定错误码供MCP/broker区分可修正参数与不可继续的额度、取消、超时。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class BudgetLimits:
    max_requests: int = 200
    rate_per_minute: int = 120
    run_concurrency: int = 2
    global_concurrency: int = 8
    export_rows: int = 100000
    export_bytes: int = 64 * 1024 * 1024
    timeout_seconds: float = 30

    @classmethod
    def from_env(cls) -> BudgetLimits:
        defaults = cls()
        fields = {
            "max_requests": "RISK_QUERY_MAX_REQUESTS",
            "rate_per_minute": "RISK_QUERY_RATE_PER_MINUTE",
            "run_concurrency": "RISK_QUERY_RUN_CONCURRENCY",
            "global_concurrency": "RISK_QUERY_GLOBAL_CONCURRENCY",
            "export_rows": "RISK_EXPORT_MAX_ROWS",
            "export_bytes": "RISK_EXPORT_MAX_BYTES",
        }
        values = {}
        for field, name in fields.items():
            try:
                values[field] = int(os.getenv(name, str(getattr(defaults, field))))
            except ValueError as exc:
                raise BudgetError("CONFIG_ERROR", f"{name}必须为正整数") from exc
            if values[field] < 1:
                raise BudgetError("CONFIG_ERROR", f"{name}必须为正整数")
        try:
            timeout = float(os.getenv("RISK_QUERY_TIMEOUT_SECONDS", str(defaults.timeout_seconds)))
        except ValueError as exc:
            raise BudgetError("CONFIG_ERROR", "RISK_QUERY_TIMEOUT_SECONDS必须为有效秒数") from exc
        if not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise BudgetError("CONFIG_ERROR", "RISK_QUERY_TIMEOUT_SECONDS必须大于0且不超过600")
        return cls(**values, timeout_seconds=timeout)


@contextmanager
def _atomic():
    """全局并发判断与占位在同一可写应用库事务内，不能先查后无锁写入。"""
    with SessionLocal() as db:
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            db.connection().exec_driver_sql("BEGIN IMMEDIATE")
        elif dialect == "postgresql":
            db.execute(text("INSERT INTO query_budget_gate (id) VALUES (1) ON CONFLICT (id) DO NOTHING"))
            db.execute(text("SELECT id FROM query_budget_gate WHERE id = 1 FOR UPDATE"))
        else:
            raise BudgetError("CONFIG_ERROR", "查询额度账本目前仅支持SQLite/PostgreSQL应用库")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise


_current: ContextVar[QueryTicket | None] = ContextVar("business_query_ticket", default=None)


def current_ticket() -> QueryTicket | None:
    """只读服务用此票据设置SQL期限；内部helper不再申请第二份逻辑查询额度。"""
    return _current.get()


@dataclass
class QueryTicket:
    run_id: str
    operation_id: str
    deadline_at: float
    _deadline: float
    _limits: BudgetLimits
    _cancelled: Callable[[], bool] | None = None
    _closed: bool = False
    # 只在事务提交后增加，供导出异常收尾判断本次charge是否已经落账。
    export_rows_charged: int = field(default=0, init=False)
    export_bytes_charged: int = field(default=0, init=False)

    def check_cancelled(self) -> None:
        """不访问数据库，可在SQLite progresshandler及读取批次间使用。"""
        if self._cancelled and self._cancelled():
            raise BudgetError("CANCELLED", "业务查询已取消")
        if time.monotonic() >= self._deadline:
            raise BudgetError("QUERY_TIMEOUT", "业务查询超过服务端执行期限")

    def remaining_seconds(self) -> float:
        self.check_cancelled()
        return max(0.0, self._deadline - time.monotonic())

    def charge_export(self, rows: int = 0, bytes: int = 0) -> None:
        """增量记录已扫描/写出的实际量；超限失败也保留消耗，复用数据不调用。

        导出开始前用(0,0)检查剩余额度，随后按有界批次扣实际量。单批超限先记账
        再报错，后续尝试不能靠失败导出反复免费扫描；不会发布半份数据集。
        """
        if type(rows) is not int or type(bytes) is not int or rows < 0 or bytes < 0:
            raise BudgetError("INVALID_ARGUMENT", "导出增量行数和字节数必须为非负整数")
        if self._closed:
            raise BudgetError("INVALID_OPERATION", "已结束的业务操作不能继续记账")
        # 正增量代表已经发生的消耗：即使此时收到取消，也先记账再退出。
        if rows == bytes == 0:
            self.check_cancelled()
        with _atomic() as db:
            operation = db.get(QueryOperation, self.operation_id)
            if operation is None or operation.finished_at is not None or operation.owner_pid != os.getpid():
                raise BudgetError("INVALID_OPERATION", "业务操作占位已失效")
            ledger = db.get(QueryBudgetLedger, self.run_id)
            ledger.export_rows += rows
            ledger.export_bytes += bytes
            exceeded = (ledger.export_rows > self._limits.export_rows or ledger.export_bytes > self._limits.export_bytes)
            exhausted = rows == bytes == 0 and (ledger.export_rows >= self._limits.export_rows or ledger.export_bytes >= self._limits.export_bytes)
        self.export_rows_charged += rows
        self.export_bytes_charged += bytes
        if exceeded or exhausted:
            raise BudgetError("EXPORT_BUDGET_EXCEEDED", "整任务累计导出行数或字节额度已耗尽")
        self.check_cancelled()

    def _finish(self) -> None:
        # 调用方离开实际操作体时才释放，不因deadline到了或发出取消信号提前释放。
        with _atomic() as db:
            operation = db.get(QueryOperation, self.operation_id)
            if operation and operation.finished_at is None and operation.owner_pid == os.getpid():
                operation.finished_at = time.time()
        self._closed = True


def _admit(run_id: str, operation: str, limits: BudgetLimits, cancelled) -> QueryTicket:
    if not isinstance(operation, str) or not operation or len(operation) > 100:
        raise BudgetError("INVALID_ARGUMENT", "业务操作名称须为1至100字文本")
    started = time.time()
    with _atomic() as db:
        if db.get(AnalysisRun, run_id) is None:
            raise BudgetError("INVALID_TASK", "任务不存在，不能申请查询额度")
        ledger = db.get(QueryBudgetLedger, run_id)
        if ledger is None:
            ledger = QueryBudgetLedger(run_id=run_id, requests_used=0, export_rows=0, export_bytes=0)
            db.add(ledger)
        if ledger.requests_used >= limits.max_requests:
            raise BudgetError("QUERY_BUDGET_EXCEEDED", "整任务累计业务请求额度已耗尽")
        recent = db.scalar(select(func.count()).select_from(QueryOperation).where(
            QueryOperation.run_id == run_id, QueryOperation.started_at >= started - 60))
        if recent >= limits.rate_per_minute:
            raise BudgetError("RATE_LIMITED", "整任务每分钟业务请求额度已耗尽")
        active = QueryOperation.finished_at.is_(None)
        local_count = db.scalar(select(func.count()).select_from(QueryOperation).where(active, QueryOperation.run_id == run_id))
        if local_count >= limits.run_concurrency:
            raise BudgetError("QUERY_CONCURRENCY", "当前任务的并发业务查询已达上限")
        global_count = db.scalar(select(func.count()).select_from(QueryOperation).where(active))
        if global_count >= limits.global_concurrency:
            raise BudgetError("GLOBAL_CONCURRENCY", "全局并发业务查询已达上限")
        operation_id = uid()
        deadline_at = started + limits.timeout_seconds
        db.add(QueryOperation(id=operation_id, run_id=run_id, operation=operation, owner_pid=os.getpid(),
                              started_at=started, deadline_at=deadline_at))
        ledger.requests_used += 1
    return QueryTicket(run_id, operation_id, deadline_at, time.monotonic() + max(0, deadline_at - time.time()), limits, cancelled)


@contextmanager
def business_operation(run_id: str, operation: str, *, cancelled: Callable[[], bool] | None = None) -> Iterator[QueryTicket]:
    """准入后的失败也计请求，拒绝准入不新建占位；嵌套helper复用当前同任务票据。"""
    parent = current_ticket()
    if parent is not None:
        if parent.run_id != run_id:
            raise BudgetError("INVALID_TASK", "嵌套业务操作不能切换任务")
        parent.check_cancelled()
        yield parent
        return
    ticket = _admit(run_id, operation, BudgetLimits.from_env(), cancelled)
    token = _current.set(ticket)
    try:
        ticket.check_cancelled()
        yield ticket
    finally:
        _current.reset(token)
        ticket._finish()


def _owner_is_dead(pid: int) -> bool:
    """仅接受操作系统确认的本机进程死亡，拒绝把权限不足或超时当成已退出。"""
    if pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID已不存在
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.ESRCH
    return False


def reclaim_dead_owners(run_id: str | None = None) -> int:
    """清理钩子：确认owner已死才结束遗留占位；deadline或取消信号不构成释放依据。"""
    reclaimed = 0
    with _atomic() as db:
        query = select(QueryOperation).where(QueryOperation.finished_at.is_(None))
        if run_id is not None:
            query = query.where(QueryOperation.run_id == run_id)
        for operation in db.scalars(query):
            if _owner_is_dead(operation.owner_pid):
                operation.finished_at = time.time()
                reclaimed += 1
    return reclaimed

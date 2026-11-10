"""Python 与 Pi Agent 的 JSON 边界。

只传递固定的指标目录和事件证据；规则、数据库写入、审批和下发仍由 Python 负责。
复用入口为 provider.select() 和 provider.explain_batch()，两者失败均抛出 AnalysisError。
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager, nullcontext
from pathlib import Path
import shutil
import subprocess
import sys
import signal
import time
from typing import Any, NotRequired, TypedDict, cast


JsonObject = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
AGENT_DIR = ROOT / "agent"
RUNNER_PATH = AGENT_DIR / "dist" / "runner.js"
PROCESS_CLEANUP_SECONDS = 10
DIAGNOSTIC_MAX_EVENTS = 1000
DIAGNOSTIC_MAX_BYTES = 1024 * 1024


def task_budget_ms() -> int:
    """Python与TS使用同一整批期限；额外10秒仅留给MCP退出清理。"""
    try:
        budget = int(os.getenv("RISK_PI_BATCH_TIMEOUT_MS", "120000"))
    except ValueError as exc:
        raise AnalysisError("RISK_PI_BATCH_TIMEOUT_MS必须是整数") from exc
    if not 1 <= budget <= 600000:
        raise AnalysisError("RISK_PI_BATCH_TIMEOUT_MS必须在1至600000之间")
    return budget


class IndicatorDefinition(TypedDict):
    """稳定指标 ID 与展示口径；可直接使用指标目录 API 的结果。"""

    id: str
    name: str
    unit: NotRequired[str]
    frequency: NotRequired[str]
    description: NotRequired[str]


class IndicatorSelection(TypedDict):
    """辅助指标选择结果；indicator_ids 同时包含全部必查指标。"""

    mode: str
    topic: str
    indicator_ids: list[str]
    reasons: list[str]
    limitations: list[str]
    runtime: JsonObject
    trace: list[JsonObject]


class AnalysisCase(TypedDict):
    """一条待分析事件；证据沿用规则检测生成的完整字段，不重新计算数值。"""

    case_key: str
    evidence: list[JsonObject]


class CaseAnalysis(TypedDict):
    """按事件保存的事实、待核查解释、建议及执行轨迹。"""

    mode: str
    facts: list[str]
    hypotheses: list[str]
    checks: list[str]
    limitations: list[str]
    evidence_references: JsonObject
    runtime: JsonObject
    trace: list[JsonObject]


class AnalysisError(RuntimeError):
    """Agent 配置、执行或协议失败；上层将其转换为可读的 API 错误。"""

    def __init__(self, message: str, *, diagnostics: JsonObject | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics


def _validate_diagnostics(value: Any) -> JsonObject:
    """只验证跨进程诊断的结构与持久化上限；脱敏和有标记的截断由Node完成。"""
    if not isinstance(value, dict) or not isinstance(value.get("trace"), list):
        raise AnalysisError("Pi Agent诊断格式错误：trace必须为数组。")
    if len(value["trace"]) > DIAGNOSTIC_MAX_EVENTS or any(not isinstance(item, dict) for item in value["trace"]):
        raise AnalysisError("Pi Agent诊断格式错误：trace必须为不超过1000条的对象数组。")
    if "runtime" in value and not isinstance(value["runtime"], dict):
        raise AnalysisError("Pi Agent诊断格式错误：runtime必须为对象。")
    if "truncated" in value and not isinstance(value["truncated"], bool):
        raise AnalysisError("Pi Agent诊断格式错误：truncated必须为布尔值。")
    try:
        size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise AnalysisError("Pi Agent诊断格式错误：内容不是有效JSON。") from exc
    if size > DIAGNOSTIC_MAX_BYTES:
        raise AnalysisError("Pi Agent诊断超过1MiB上限，Node未按协议截断。")
    return value


def result_diagnostics(result: JsonObject) -> JsonObject | None:
    """提取已完成Agent调用的轨迹，使后续业务校验失败仍可复核模型与工具调用。"""
    if "trace" not in result and "runtime" not in result:
        return None
    diagnostics = {"trace": result.get("trace", [])}
    for key in ("runtime", "truncated"):
        if key in result:
            diagnostics[key] = result[key]
    return _validate_diagnostics(diagnostics)


class ProcessCleanupError(AnalysisError):
    """未确认后代进程停止；上层不得据此清除活动容器标志。"""


@contextmanager
def _owned_node_tree(process: subprocess.Popen):
    """发送任务前建立进程树所有权；Node先退出时也回收其MCP后代，避免marker迟到写入。"""
    job = kernel = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", IoCounters),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
                (name, wintypes.DWORD) for name in ("faults", "total", "active", "terminated")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        attached = job and kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) and kernel.AssignProcessToJobObject(job, process._handle)
        if not attached:
            if job:
                kernel.CloseHandle(job)
            process.kill()
            process.wait()
            raise AnalysisError("无法建立可回收的Node进程树，未发送分析任务。")

    stopped = False

    def stop():
        nonlocal stopped
        if stopped:
            return
        stopped = True
        if os.name == "nt":
            try:
                if not kernel.TerminateJobObject(job, 1):
                    raise ProcessCleanupError("无法终止Node任务进程树，请检查宿主进程。")
                deadline = time.monotonic() + 3
                while True:
                    accounting = Accounting()
                    if not kernel.QueryInformationJobObject(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        raise ProcessCleanupError("无法确认Node任务进程树已回收。")
                    if accounting.active == 0:
                        break
                    if time.monotonic() >= deadline:
                        raise ProcessCleanupError("Node任务进程树回收超时。")
                    time.sleep(0.02)
            finally:
                kernel.CloseHandle(job)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
            except OSError as exc:
                raise ProcessCleanupError("无法终止Node任务进程组。") from exc
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired as exc:
                raise ProcessCleanupError("Node任务主进程尚未退出。") from exc
            deadline = time.monotonic() + 3
            while True:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                if time.monotonic() >= deadline:
                    raise ProcessCleanupError("Node任务进程组尚未确认退出。")
                time.sleep(0.02)

    try:
        yield stop
    finally:
        stop()


class PiAnalysisProvider:
    """通过短生命周期 Node 进程调用 Pi，实例自身不保存会话或业务状态。"""

    version = "pi-1.4.0-risk-mcp-trace-3"

    @property
    def mode(self) -> str:
        """返回展示模式；非法配置由 Node 统一拒绝，不自动降级。"""
        return "pi-" + os.getenv("RISK_PI_MODE", "demo")

    @property
    def cache_identity(self) -> dict[str, str]:
        """参与运行去重，使模式、模型或端点变化后重新分析；不包含 API 密钥。"""
        return {
            "mode": self.mode,
            "version": self.version,
            "provider": os.getenv("RISK_PI_PROVIDER", ""),
            "model": os.getenv("RISK_PI_MODEL", ""),
            "base_url": os.getenv("RISK_PI_BASE_URL", ""),
            "codemode": os.getenv("RISK_CODEMODE", "0"),
        }

    def _request(self, payload: JsonObject, budget_ms: int | None = None) -> JsonObject:
        """一次进程交换一个 JSON 请求/结果；失败保留根因并向上抛出。"""
        node = shutil.which("node")
        if not node or not RUNNER_PATH.is_file():
            raise AnalysisError(
                "Pi Agent 未安装：需要 Node.js 22.19+，"
                "请在 agent 目录执行 npm ci --ignore-scripts 和 npm run build。"
            )
        budget_ms = task_budget_ms() if budget_ms is None else budget_ms
        payload = {**payload, "budget_ms": budget_ms}
        env = {**os.environ, "RISK_PYTHON_EXECUTABLE": sys.executable,
               "RISK_PROJECT_ROOT": str(ROOT), "RISK_PI_BATCH_TIMEOUT_MS": str(budget_ms)}
        try:
            with subprocess.Popen(
                [node, str(RUNNER_PATH)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", cwd=AGENT_DIR, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            ) as process:
                def stop_standard_tree():
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                # 只有explore可启动代码容器；原指标选择/事件分析保持原有进程契约。
                ownership = _owned_node_tree(process) if payload.get("operation") == "explore" else nullcontext(stop_standard_tree)
                with ownership as stop_tree:
                    try:
                        stdout, stderr = process.communicate(json.dumps(payload, ensure_ascii=False),
                                                             timeout=budget_ms / 1000 + PROCESS_CLEANUP_SECONDS)
                    except subprocess.TimeoutExpired as exc:
                        stop_tree()
                        process.communicate()
                        raise AnalysisError("Pi任务预算及退出余量已耗尽，本次进程树已终止；可显式重试失败任务。") from exc
                    returncode = process.returncode
        except OSError as exc:
            raise AnalysisError(f"Pi Agent 进程无法启动：{exc.strerror}") from exc

        # stdout 只接受协议结果。stderr 可能含供应商响应，不直接回显给页面。
        try:
            response = json.loads(stdout)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise AnalysisError(
                f"Pi Agent 未返回有效结果（退出码 {returncode}），请检查 agent 依赖及配置。"
            ) from exc
        if returncode or not isinstance(response, dict) or "error" in response:
            error = response.get("error", "执行失败") if isinstance(response, dict) else "返回格式错误"
            if isinstance(error, dict):
                error = error.get("message", "执行失败")
            diagnostics = None
            if isinstance(response, dict) and "diagnostics" in response:
                try:
                    diagnostics = _validate_diagnostics(response["diagnostics"])
                except AnalysisError as exc:
                    raise AnalysisError(f"Pi Agent：{error}；{exc}") from exc
            raise AnalysisError(f"Pi Agent：{error}", diagnostics=diagnostics)
        result_diagnostics(response)
        return response

    def select(
        self,
        topic: str,
        indicators: list[IndicatorDefinition],
        mandatory_ids: list[str],
    ) -> IndicatorSelection:
        """按主题选择目录内指标，保留 mandatory_ids；不改变已启用的监测规则。"""
        selection = self._request({
            "operation": "select",
            "topic": topic,
            "indicators": indicators,
            "mandatory_ids": mandatory_ids,
        })
        allowed = {item["id"] for item in indicators}
        selected = selection.get("indicator_ids")
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
            raise AnalysisError("Pi Agent 指标选择结果格式错误。", diagnostics=result_diagnostics(selection))
        if not set(selected) <= allowed or not set(mandatory_ids) <= set(selected):
            raise AnalysisError("Pi Agent 返回未知指标或遗漏必查指标，已拒绝本次结果。", diagnostics=result_diagnostics(selection))
        return cast(IndicatorSelection, selection)

    def explain_batch(self, cases: list[AnalysisCase], context: JsonObject) -> dict[str, CaseAnalysis]:
        """批量分析固定证据，按 case_key 返回结果；空批次不启动 Node 进程。"""
        if not cases:
            return {}
        response = self._request({"operation": "explain_batch", "cases": cases, "context": context})
        analyses = response.get("analyses")
        if not isinstance(analyses, dict) or set(analyses) != {case["case_key"] for case in cases}:
            raise AnalysisError("Pi Agent 分析结果未覆盖本批次全部风险事项。")
        for analysis in analyses.values():
            if not isinstance(analysis, dict) or any(
                not isinstance(analysis.get(field), list)
                or any(not isinstance(item, str) for item in analysis[field])
                for field in ("facts", "hypotheses", "checks", "limitations")
            ):
                raise AnalysisError("Pi Agent 分析结果格式错误。")
            result_diagnostics(analysis)
        return cast(dict[str, CaseAnalysis], analyses)

    def explore(self, context: JsonObject) -> JsonObject:
        """整个任务范围的一次代码探索；只返回摘要、引用与执行产物身份。"""
        try:
            budget = int(os.getenv("RISK_CODE_BUDGET_MS", "300000"))
        except ValueError as exc:
            raise AnalysisError("RISK_CODE_BUDGET_MS必须是整数") from exc
        if not 1 <= budget <= 600000:
            raise AnalysisError("RISK_CODE_BUDGET_MS必须在1至600000之间")
        reaped = True
        diagnostics = None
        failure = None
        try:
            result = self._request({"operation": "explore", "context": context}, budget_ms=budget)
            diagnostics = result_diagnostics(result)
            return result
        except ProcessCleanupError:
            reaped = False
            raise
        except AnalysisError as exc:
            diagnostics = exc.diagnostics
            failure = exc
            raise
        finally:
            # _request先回收Node/MCP，再由仍存活的API宿主清理daemon托管的本任务容器。
            from .code_runner import cleanup_task_container
            if reaped:
                cleanup = cleanup_task_container(context["run_id"])
                from .query_budget import reclaim_dead_owners
                reclaim_dead_owners(context["run_id"])
                if not cleanup["removed"]:
                    message = "隔离执行未确认安全结束：" + cleanup["message"]
                    if failure is not None:
                        message = f"{failure}；{message}"
                    raise AnalysisError(message, diagnostics=diagnostics) from failure


provider = PiAnalysisProvider()

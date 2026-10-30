"""受限Python代码执行：只在无网络Linux Docker容器运行任务work内的.py文件。

宿主只解析stdout上的取数RPC，按已提交任务导出只读Parquet；模型代码永不由宿主
Python执行。输入为run_id/相对脚本路径，输出为限长日志、文件哈希和可信执行记录。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from uuid import uuid4

from . import services
from .task_workspace import (
    MAX_WORK_BYTES, MAX_WORK_FILES, WorkspaceError, export_dataset, list_task_files,
    clear_active_execution, mark_active_execution, read_task_file, save_execution,
    task_lock, task_paths, work_path, work_usage,
)

RPC_PREFIX = b"__RISK_RPC_V1__:"
MAX_LOG_BYTES = 32 * 1024
MAX_RPC_BYTES = 64 * 1024
MAX_EXPORT_REQUESTS = 16
DEFAULT_IMAGE = "risk-python-sandbox:0.1"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def _docker() -> tuple[str, str]:
    executable = shutil.which("docker")
    if not executable:
        raise WorkspaceError("SANDBOX_UNAVAILABLE", "Docker不可用；不会在宿主直接执行代码")
    image = os.getenv("RISK_SANDBOX_IMAGE", DEFAULT_IMAGE)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,200}", image):
        raise WorkspaceError("INVALID_ARGUMENT", "隔离镜像名称不合法")
    try:
        inspected = subprocess.run([executable, "image", "inspect", image, "--format", "{{.Os}}"],
                                   capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkspaceError("SANDBOX_UNAVAILABLE", "无法连接Docker或检查隔离镜像") from exc
    if inspected.returncode or inspected.stdout.strip() != "linux":
        raise WorkspaceError("SANDBOX_UNAVAILABLE", "Docker引擎未就绪或Linux隔离镜像risk-python-sandbox:0.1不可用，请启动Docker并确认镜像已构建。")
    return executable, image


def _append_log(buffer: bytearray, data: bytes) -> bool:
    available = max(0, MAX_LOG_BYTES - len(buffer))
    buffer.extend(data[:available])
    return len(data) > available


def _remove_owned_container(docker: str, name: str, execution_id: str) -> dict:
    """只删除本次随机命名且标签匹配的容器，绝不做全局Docker清理。"""
    try:
        result = subprocess.run([docker, "inspect", "--format", '{{index .Config.Labels "risk.agent.execution"}}', name],
                                capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
        if result.returncode:
            # 已退出且不存在属于正常清理；daemon故障不能当成成功。
            if "No such" in result.stderr:
                return {"removed": True}
            return {"removed": False, "message": "无法核对隔离容器状态，需要人工检查Docker"}
        if result.stdout.strip() != execution_id:
            return {"removed": False, "message": "容器所有权标签不匹配，未删除"}
        removed = subprocess.run([docker, "rm", "-f", name], capture_output=True, timeout=5, creationflags=NO_WINDOW)
        return {"removed": removed.returncode == 0,
                **({} if removed.returncode == 0 else {"message": "隔离容器清理失败，需要人工检查Docker"})}
    except (OSError, subprocess.TimeoutExpired):
        return {"removed": False, "message": "隔离容器清理超时，需要人工检查Docker"}


def _process_alive(pid: int) -> bool:
    """仅检查宿主锁的所有者是否仍存活；无法确认时保守保留锁。"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER：该PID不存在。
    try:
        code = wintypes.DWORD()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        kernel.CloseHandle(handle)


def cleanup_task_container(run_id: str) -> dict:
    """API宿主在Node/MCP进程树回收后兜底；仅处理此任务可信meta记录的容器。"""
    meta = task_paths(run_id)["meta"]
    marker_path = meta / "active-execution.json"
    cleanup = {"removed": True}
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"removed": False, "message": "任务活动容器记录不可读取，未解除锁定"}
        execution_id = marker.get("execution_id", "")
        name = marker.get("container_name", "")
        if not isinstance(execution_id, str) or not re.fullmatch(r"[a-f0-9]{32}", execution_id) or name != f"risk-analysis-{execution_id}":
            return {"removed": False, "message": "任务活动容器身份不合法，未执行清理"}
        docker = shutil.which("docker")
        if not docker:
            return {"removed": False, "message": "Docker不可用，活动容器仍须人工确认清理"}
        cleanup = _remove_owned_container(docker, name, execution_id)
        if cleanup["removed"]:
            clear_active_execution(run_id, execution_id, name)
    if cleanup["removed"]:
        lock_path = meta / "workspace.lock"
        if lock_path.exists():
            try:
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
                if owner.get("run_id") == run_id and type(owner.get("pid")) is int and owner["pid"] > 0 and not _process_alive(owner["pid"]):
                    lock_path.unlink()
                else:
                    return {"removed": False, "message": "工作区仍有活动锁或所有者不可确认，未解锁"}
            except (OSError, ValueError):
                return {"removed": False, "message": "工作区锁不可核验，请人工确认"}
    return cleanup


def execute_python(run_id: str, script_path: str, timeout_seconds: int = 60) -> dict:
    """执行现有脚本，最长60秒；记录代码失败/超时供模型修正，但不信任其输出结论。"""
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise WorkspaceError("INVALID_ARGUMENT", "执行时限须为1至60秒的整数")
    with task_lock(run_id):
        context = services.load_task_context(run_id)
        script_file = work_path(run_id, script_path, must_exist=True)
        if script_file.suffix != ".py":
            raise WorkspaceError("INVALID_ARGUMENT", "只执行工作区内的.py文件")
        read_task_file(run_id, script_path)  # 复用文本大小/编码边界，再保存实际执行字节。
        script = script_file.read_bytes()
        docker, image = _docker()
        paths = task_paths(run_id)
        execution_id = uuid4().hex
        name = f"risk-analysis-{execution_id}"
        started_at = datetime.now(timezone.utc).isoformat()
        command = [
            docker, "create", "--name", name, "--label", f"risk.agent.execution={execution_id}",
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "--memory", "512m", "--memory-swap", "512m", "--cpus", "1", "--pids-limit", "64",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--ulimit", "fsize=67108864:67108864",
            "--ulimit", "nofile=128:128", "--log-driver", "none", "--init", "--interactive",
            "--mount", f"type=bind,source={paths['data'].resolve()},target=/data,readonly",
            "--mount", f"type=bind,source={paths['work'].resolve()},target=/workspace",
            "--workdir", "/workspace", image, "python", "-u", f"/workspace/{script_path}",
        ]
        # Docker CLI继承宿主连接设置；无-e透传，容器内没有数据库或模型认证环境。
        events: queue.Queue = queue.Queue(maxsize=64)
        replies: queue.Queue[bytes] = queue.Queue(maxsize=8)
        cancelled = threading.Event()
        stdout, stderr = bytearray(), bytearray()
        truncated = {"stdout": False, "stderr": False}
        exports, rpc_count = [], 0
        rpc_busy = False
        exit_code, status, error = 1, "failed", None
        process = None
        cleanup = {"removed": False}
        begin = time.monotonic()
        workers = []

        def emit(event):
            while not cancelled.is_set():
                try:
                    events.put(event, timeout=0.1)
                    return
                except queue.Full:
                    continue

        def reader(channel, pipe):
            try:
                for chunk in iter(lambda: pipe.read1(4096), b""):
                    emit((channel, chunk))
            except (OSError, ValueError):
                pass
            finally:
                emit((channel, None))

        def writer():
            # 不可信代码可能永远不读stdin；阻塞只发生在此线程，主循环仍能按时杀容器。
            while not cancelled.is_set():
                try:
                    data = replies.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    process.stdin.write(data)
                    process.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    return

        def export_request(request):
            try:
                params = request.get("params", {})
                if request.get("method") != "export_metrics" or not isinstance(params, dict) or set(params) - {"indicator_ids", "org_ids", "history"}:
                    raise WorkspaceError("INVALID_ARGUMENT", "只允许export_metrics只读取数请求")
                manifest = export_dataset(context, params.get("indicator_ids"), params.get("org_ids"),
                                          params.get("history", True), cancelled=cancelled.is_set)
                emit(("rpc_result", {"id": request["id"], "result": manifest}))
            except (WorkspaceError, services.ServiceError) as exc:
                emit(("rpc_result", {"id": request["id"], "error": {"code": exc.code, "message": str(exc)}}))
            except Exception:
                emit(("rpc_result", {"id": request["id"], "error": {"code": "SOURCE_ERROR", "message": "只读导出未完成，请检查任务与数据源"}}))

        def reply(response):
            try:
                replies.put_nowait((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))
            except queue.Full as exc:
                raise WorkspaceError("RPC_BACKPRESSURE", "代码未及时读取取数响应，已停止本次执行") from exc

        def handle_line(line: bytes):
            nonlocal rpc_count, rpc_busy
            if not line.startswith(RPC_PREFIX):
                truncated["stdout"] |= _append_log(stdout, line)
                return
            try:
                request = json.loads(line[len(RPC_PREFIX):])
                if not isinstance(request, dict) or not isinstance(request.get("id"), str) or len(request["id"]) > 80:
                    raise ValueError
            except (UnicodeError, ValueError):
                reply({"id": None, "error": {"code": "INVALID_ARGUMENT", "message": "取数RPC格式错误"}})
                return
            rpc_count += 1
            if rpc_busy or rpc_count > MAX_EXPORT_REQUESTS:
                reply({"id": request["id"], "error": {"code": "REQUEST_LIMIT", "message": "取数请求须串行，且单次执行最多16次"}})
                return
            rpc_busy = True
            threading.Thread(target=export_request, args=(request,), daemon=True).start()

        try:
            mark_active_execution(run_id, execution_id, name)
            # create与start拆开：创建阶段父进程被回收时，不会迟到启动一个无人监督的容器。
            created = subprocess.run(command, capture_output=True, timeout=min(15, timeout_seconds), creationflags=NO_WINDOW)
            if created.returncode:
                raise WorkspaceError("SANDBOX_UNAVAILABLE", "无法创建隔离容器，请检查Docker引擎与镜像")
            process = subprocess.Popen([docker, "start", "--attach", "--interactive", name],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=NO_WINDOW)
            for channel in ("stdout", "stderr"):
                worker = threading.Thread(target=reader, args=(channel, getattr(process, channel)), daemon=True)
                worker.start()
                workers.append(worker)
            worker = threading.Thread(target=writer, daemon=True)
            worker.start()
            workers.append(worker)
            pending = b""
            closed = set()
            while True:
                if time.monotonic() - begin >= timeout_seconds:
                    status, exit_code, error = "timeout", 124, "代码执行达到时限，已请求终止隔离容器"
                    break
                count, size = work_usage(run_id)
                if count > MAX_WORK_FILES or size > MAX_WORK_BYTES:
                    status, exit_code, error = "output_limit", 1, "工作区超过100文件或64MiB输出限制"
                    break
                try:
                    channel, data = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if channel == "rpc_result":
                    rpc_busy = False
                    if "result" in data:
                        exports.append(data["result"])
                    reply(data)
                    continue
                if data is None:
                    closed.add(channel)
                elif channel == "stderr":
                    truncated["stderr"] |= _append_log(stderr, data)
                else:
                    pending += data
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        handle_line(line + b"\n")
                    if len(pending) > MAX_RPC_BYTES:
                        truncated["stdout"] |= _append_log(stdout, pending)
                        pending = b""
                if len(closed) == 2:
                    if pending:
                        handle_line(pending)
                    exit_code = process.wait(timeout=2)
                    status = "completed" if exit_code == 0 else "failed"
                    break
        except WorkspaceError as exc:
            status, exit_code, error = "failed", 1, str(exc)
        except subprocess.TimeoutExpired:
            status, exit_code, error = "timeout", 124, "隔离进程启动或退出达到时限，已请求终止容器"
        except OSError:
            error = "无法启动或读取Docker隔离进程"
        finally:
            cancelled.set()
            cleanup = _remove_owned_container(docker, name, execution_id)
            if process is not None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                for worker in workers:
                    worker.join(timeout=1)
                # 进程退出后管道已断开，reader/writer应当结束；不在仍持锁的BufferedWriter上close。
                if not any(worker.is_alive() for worker in workers):
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        pipe.close()
            if not cleanup["removed"]:
                status, exit_code, error = "cleanup_failed", 1, cleanup["message"]
            else:
                clear_active_execution(run_id, execution_id, name)
        files = []
        # 容器未确认消失时，不能读取仍由不可信代码修改的路径（包括潜在符号链接）。
        if cleanup["removed"]:
            try:
                files = list_task_files(run_id)
            except (OSError, WorkspaceError) as exc:
                status, exit_code, error = "invalid_output", 1, str(exc)
        result = {
            "execution_id": execution_id, "run_id": run_id, "script_path": script_path,
            "script_sha256": hashlib.sha256(script).hexdigest(), "image": image,
            "exit_code": exit_code, "status": status, "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"), "truncated": truncated,
            "files": files, "exports": exports, "error": error, "cleanup": cleanup,
            "started_at": started_at, "finished_at": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(time.monotonic() - begin, 3), "timeout_seconds": timeout_seconds,
        }
        save_execution(run_id, execution_id, script, result)
        return result

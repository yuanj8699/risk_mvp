"""隔离执行的真实输出验收；Docker/镜像缺失时明确跳过，不用宿主Python替代。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

import pytest

from test_services import store


@pytest.fixture
def workspace(store, tmp_path, monkeypatch):
    from backend import code_runner, task_workspace
    monkeypatch.setattr(task_workspace, "TASK_ROOT", tmp_path / "tasks")
    return store, task_workspace, code_runner


@pytest.fixture
def docker_ready():
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("未安装Docker，真实隔离执行未验证")
    try:
        ready = subprocess.run([docker, "image", "inspect", "risk-python-sandbox:0.1", "--format", "{{.Os}}"],
                               capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        pytest.skip("Docker daemon未就绪，真实隔离执行未验证")
    if ready.returncode or ready.stdout.strip() != "linux":
        pytest.skip("缺少已构建Linux隔离镜像，真实隔离执行未验证")
    return docker


def test_file_boundary_rejects_absolute_and_parent_paths(workspace):
    store, files, _ = workspace
    run_id = store.context["run_id"]
    for path in ["../outside.txt", "/tmp/outside", "C:/Windows/outside", "a/../../outside", "a\\outside"]:
        with pytest.raises(files.WorkspaceError) as error:
            files.write_task_file(run_id, path, "not written")
        assert error.value.code == "PATH_VIOLATION"
    saved = files.write_task_file(run_id, "analysis.py", "print('ok')\n")
    read = files.read_task_file(run_id, "analysis.py")
    assert read["sha256"] == saved["sha256"]
    assert read["content"] == "print('ok')\n"


def test_no_docker_never_executes_script_on_host(workspace, monkeypatch):
    store, files, runner = workspace
    run_id = store.context["run_id"]
    files.write_task_file(run_id, "analysis.py", "from pathlib import Path\nPath('host-escaped.txt').write_text('unsafe')")
    monkeypatch.setattr(runner.shutil, "which", lambda _: None)
    with pytest.raises(files.WorkspaceError) as error:
        runner.execute_python(run_id, "analysis.py")
    assert error.value.code == "SANDBOX_UNAVAILABLE"
    assert not (files.task_paths(run_id)["work"] / "host-escaped.txt").exists()


def test_unconfirmed_container_blocks_work_access_until_matching_cleanup(workspace):
    store, files, runner = workspace
    run_id = store.context["run_id"]
    files.write_task_file(run_id, "analysis.py", "print('ok')")
    files.mark_active_execution(run_id, "owned-execution", "risk-analysis-owned-execution")
    for action in [lambda: files.read_task_file(run_id, "analysis.py"),
                   lambda: files.write_task_file(run_id, "analysis.py", "print('changed')"),
                   lambda: files.list_task_files(run_id),
                   lambda: files.resolve_work_path(run_id, "analysis.py"),
                   lambda: runner.execute_python(run_id, "analysis.py")]:
        with pytest.raises(files.WorkspaceError) as error:
            action()
        assert error.value.code == "SANDBOX_ACTIVE"
    with pytest.raises(files.WorkspaceError):
        files.clear_active_execution(run_id, "wrong-owner", "risk-analysis-owned-execution")
    files.clear_active_execution(run_id, "owned-execution", "risk-analysis-owned-execution")
    assert files.read_task_file(run_id, "analysis.py")["content"] == "print('ok')"


def test_existing_fifo_is_rejected_without_opening_it(workspace):
    if not hasattr(os, "mkfifo"):
        pytest.skip("当前Windows宿主不支持POSIX FIFO，此边界需在POSIX或真实容器验收")
    store, files, _ = workspace
    run_id = store.context["run_id"]
    os.mkfifo(files.task_paths(run_id)["work"] / "pipe.txt")
    with pytest.raises(files.WorkspaceError) as error:
        files.write_task_file(run_id, "pipe.txt", "must not block")
    assert error.value.code == "PATH_VIOLATION"


def test_explore_process_owner_reaps_child_after_node_exits(tmp_path, monkeypatch):
    """真实Node后代进程验证Windows Job/Linux进程组；不是Docker替身测试。"""
    from backend import analysis, code_runner
    pid_path = tmp_path / "child.pid"
    node_script = tmp_path / "early-exit.mjs"
    node_script.write_text(
        "import {spawn} from 'node:child_process'; import fs from 'node:fs';\n"
        "for await (const chunk of process.stdin) {}\n"
        "const child=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore',windowsHide:true});\n"
        f"fs.writeFileSync({json.dumps(str(pid_path))},String(child.pid));\n"
        "process.stdout.write('{}\\n',()=>process.exit(1));\n", encoding="utf-8")
    monkeypatch.setattr(analysis, "RUNNER_PATH", node_script)
    with pytest.raises(analysis.AnalysisError):
        analysis.provider._request({"operation": "explore"}, budget_ms=5000)
    child_pid = int(pid_path.read_text())
    try:
        assert not code_runner._process_alive(child_pid)
    finally:
        if code_runner._process_alive(child_pid):
            # 仅清理由此固定测试程序记录的后代，避免失败用例残留进程。
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child_pid), "/F"], capture_output=True)
            else:
                os.kill(child_pid, 9)


def test_real_container_exports_parquet_and_enforces_isolation(workspace, docker_ready, monkeypatch):
    store, files, runner = workspace
    run_id = store.context["run_id"]
    monkeypatch.setenv("RISK_PI_API_KEY", "test-secret-must-not-enter-container")
    script = '''import json, os, socket
from pathlib import Path
import pandas as pd
import risk_client

dataset = risk_client.export_metrics(["npl_ratio", "overdue_ratio"], history=True)
frame = pd.read_parquet(dataset["path"])
checks = {"uid": os.getuid(), "rows": len(frame), "value_is_string": isinstance(frame["value"].dropna().iloc[0], str),
          "docker_socket": Path("/var/run/docker.sock").exists(), "meta_visible": Path("/meta").exists(),
          "secrets": [key for key in os.environ if key in ("RISK_DATABASE_URL", "RISK_PI_API_KEY", "RISK_SOURCE_DATABASE_URL")]}
for name, path in [("readonly_data", dataset["path"]), ("readonly_root", "/opt/newfile")]:
    try:
        Path(path).write_text("forbidden")
        checks[name] = False
    except OSError:
        checks[name] = True
try:
    socket.create_connection(("1.1.1.1", 443), timeout=1).close()
    checks["network_blocked"] = False
except OSError:
    checks["network_blocked"] = True
Path("result.json").write_text(json.dumps(checks))
print(json.dumps({"row_count": len(frame), "first_evidence_id": frame["evidence_id"].iloc[0]}))
'''
    files.write_task_file(run_id, "analysis.py", script)
    result = runner.execute_python(run_id, "analysis.py")
    assert result["exit_code"] == 0, result
    assert result["cleanup"]["removed"] is True
    checks = json.loads(files.read_task_file(run_id, "result.json")["content"])
    assert checks["uid"] == 65534 and checks["rows"] == 6 and checks["value_is_string"]
    assert checks["readonly_data"] and checks["readonly_root"] and checks["network_blocked"]
    assert checks["docker_socket"] is False and checks["meta_visible"] is False and checks["secrets"] == []
    assert result["exports"][0]["row_count"] == 6
    assert files.get_execution(run_id, result["execution_id"])["script_sha256"] == result["script_sha256"]
    repeated = runner.execute_python(run_id, "analysis.py")
    assert repeated["exit_code"] == 0 and repeated["exports"][0]["reused"] is True
    assert repeated["exports"][0]["dataset_id"] == result["exports"][0]["dataset_id"]


def test_real_container_failure_keeps_bounded_logs_and_script_history(workspace, docker_ready):
    store, files, runner = workspace
    run_id = store.context["run_id"]
    files.write_task_file(run_id, "bad.py", "print('x' * 100000)\nraise ValueError('intentional-error')\n")
    result = runner.execute_python(run_id, "bad.py")
    assert result["exit_code"] != 0 and result["cleanup"]["removed"]
    assert len(result["stdout"].encode()) <= 32 * 1024 and result["truncated"]["stdout"]
    assert "intentional-error" in result["stderr"]
    record = files.get_execution(run_id, result["execution_id"])
    assert record["exit_code"] == result["exit_code"]
    saved_script = files.task_paths(run_id)["meta"] / "executions" / result["execution_id"] / "script.py"
    assert "intentional-error" in saved_script.read_text(encoding="utf-8")


def test_real_timeout_removes_only_owned_container(workspace, docker_ready):
    store, files, runner = workspace
    run_id = store.context["run_id"]
    files.write_task_file(run_id, "slow.py", "import time\ntime.sleep(30)\n")
    result = runner.execute_python(run_id, "slow.py", timeout_seconds=2)
    assert result["status"] == "timeout" and result["exit_code"] == 124
    assert result["cleanup"]["removed"] is True
    absent = subprocess.run([docker_ready, "inspect", f"risk-analysis-{result['execution_id']}"], capture_output=True)
    assert absent.returncode != 0


def test_container_symlink_output_is_not_followed_on_host(workspace, docker_ready):
    store, files, runner = workspace
    run_id = store.context["run_id"]
    files.write_task_file(run_id, "link.py", "import os\nos.symlink('/etc/passwd', 'escape.txt')\n")
    result = runner.execute_python(run_id, "link.py")
    assert result["status"] == "invalid_output" and result["exit_code"] != 0
    with pytest.raises(files.WorkspaceError) as error:
        files.read_task_file(run_id, "escape.txt")
    assert error.value.code == "PATH_VIOLATION"


def test_rpc_flood_without_reading_stdin_cannot_block_timeout(workspace, docker_ready):
    store, files, runner = workspace
    run_id = store.context["run_id"]
    files.write_task_file(run_id, "flood.py", '''import json
for i in range(100000):
    print("__RISK_RPC_V1__:" + json.dumps({"id": str(i), "method": "export_metrics", "params": {"indicator_ids": []}}), flush=True)
while True:
    pass
''')
    started = time.monotonic()
    result = runner.execute_python(run_id, "flood.py", timeout_seconds=4)
    assert result["exit_code"] != 0 and result["cleanup"]["removed"]
    assert time.monotonic() - started < 20


def test_outer_budget_reaps_executor_then_removes_its_container(workspace, docker_ready, tmp_path, monkeypatch):
    """上层预算先于60秒脚本期限结束，实际容器必须由仍存活的API宿主收回。"""
    from backend import analysis
    store, files, runner = workspace
    run_id = store.context["run_id"]
    files.write_task_file(run_id, "slow.py", "from pathlib import Path\nimport time\nPath('started.txt').write_text('started')\ntime.sleep(60)\n")
    helper = "from backend.code_runner import execute_python; execute_python(" + repr(run_id) + ", 'slow.py', 60)"
    node_script = tmp_path / "executor.mjs"
    node_script.write_text(
        "import {spawn} from 'node:child_process';\nfor await (const chunk of process.stdin) {}\n"
        f"spawn(process.env.RISK_PYTHON_EXECUTABLE,['-c',{json.dumps(helper)}],"
        "{cwd:process.env.RISK_PROJECT_ROOT,env:process.env,stdio:'ignore',windowsHide:true});\n"
        "setInterval(()=>{},1000);\n", encoding="utf-8")
    monkeypatch.setattr(analysis, "RUNNER_PATH", node_script)
    monkeypatch.setattr(analysis, "PROCESS_CLEANUP_SECONDS", 0)
    monkeypatch.setenv("RISK_CODE_BUDGET_MS", "10000")
    monkeypatch.setenv("RISK_DATABASE_URL", store.url)
    monkeypatch.setenv("RISK_TASK_WORKSPACE_DIR", str(files.TASK_ROOT))
    marker = files.task_paths(run_id)["meta"] / "active-execution.json"
    observed = {}
    stop = threading.Event()

    def observe():
        while not stop.wait(0.05):
            if marker.exists():
                try:
                    observed.update(json.loads(marker.read_text()))
                    return
                except (ValueError, OSError):
                    continue

    watcher = threading.Thread(target=observe, daemon=True)
    watcher.start()
    try:
        with pytest.raises(analysis.AnalysisError):
            analysis.provider.explore(store.context)
    finally:
        stop.set()
        watcher.join(timeout=1)
    assert observed, "测试未进入真实容器创建阶段"
    assert files.read_task_file(run_id, "started.txt")["content"] == "started"
    assert not marker.exists()
    absent = subprocess.run([docker_ready, "inspect", observed["container_name"]], capture_output=True)
    assert absent.returncode != 0

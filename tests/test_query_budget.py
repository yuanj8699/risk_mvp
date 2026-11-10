"""共享额度的真实SQLite验收；跨进程争抢使用实际子进程，不模拟核心记账。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def store(tmp_path, monkeypatch):
    from backend import models as m, query_budget as budget

    url = "sqlite:///" + (tmp_path / "budget.db").as_posix()
    engine = create_engine(url, connect_args={"timeout": 5})
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    m.Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(budget, "SessionLocal", factory)
    monkeypatch.setenv("RISK_DATABASE_URL", url)
    for key, value in {
        "RISK_QUERY_MAX_REQUESTS": "200", "RISK_QUERY_RATE_PER_MINUTE": "120",
        "RISK_QUERY_RUN_CONCURRENCY": "2", "RISK_QUERY_GLOBAL_CONCURRENCY": "8",
        "RISK_EXPORT_MAX_ROWS": "100000", "RISK_EXPORT_MAX_BYTES": "67108864",
        "RISK_QUERY_TIMEOUT_SECONDS": "30",
    }.items():
        monkeypatch.setenv(key, value)
    from datetime import date
    with factory() as db:
        db.add(m.Snapshot(id="budget-snapshot", name="额度测试", data_date=date(2026, 10, 28), source="synthetic", status="ready"))
        db.flush()
        for key in ("run-a", "run-b"):
            db.add(m.AnalysisRun(id=key, run_key=key, snapshot_id="budget-snapshot", selection={"task_context": {"run_id": key}}))
        db.commit()
    yield SimpleNamespace(models=m, budget=budget, factory=factory, url=url)
    engine.dispose()


def state(store, run_id="run-a"):
    m = store.models
    with store.factory() as db:
        ledger = db.get(m.QueryBudgetLedger, run_id)
        operations = list(db.scalars(select(m.QueryOperation).where(m.QueryOperation.run_id == run_id)))
        return {"requests": ledger.requests_used if ledger else 0,
                "rows": ledger.export_rows if ledger else 0, "bytes": ledger.export_bytes if ledger else 0,
                "active": sum(item.finished_at is None for item in operations)}


def test_failed_request_and_nested_helper_share_cumulative_ledger(store, monkeypatch):
    budget = store.budget
    monkeypatch.setenv("RISK_QUERY_MAX_REQUESTS", "2")
    with pytest.raises(ValueError, match="业务参数错误"):
        with budget.business_operation("run-a", "js.check_risks"):
            raise ValueError("业务参数错误")
    assert state(store) == {"requests": 1, "rows": 0, "bytes": 0, "active": 0}
    with budget.business_operation("run-a", "python.export") as ticket:
        assert budget.current_ticket() is ticket
        with budget.business_operation("run-a", "shared_helper") as nested:
            assert nested is ticket
            assert state(store)["requests"] == 2
    assert budget.current_ticket() is None
    with pytest.raises(budget.BudgetError) as error:
        with budget.business_operation("run-a", "retry-with-new-js"):
            pytest.fail("已耗尽整任务额度却被重新准入")
    assert error.value.code == "QUERY_BUDGET_EXCEEDED"
    assert state(store)["requests"] == 2


def test_completed_and_failed_calls_count_toward_rolling_minute(store, monkeypatch):
    budget, m = store.budget, store.models
    monkeypatch.setenv("RISK_QUERY_RATE_PER_MINUTE", "1")
    with budget.business_operation("run-a", "first"):
        pass
    with pytest.raises(budget.BudgetError) as error:
        with budget.business_operation("run-a", "too-fast"):
            pytest.fail("分钟额度未阻止请求")
    assert error.value.code == "RATE_LIMITED"
    # 将已保存的请求时间置于窗口之外，不等待一分钟，也不模拟准入判断。
    with store.factory() as db:
        db.scalar(select(m.QueryOperation)).started_at = time.time() - 61
        db.commit()
    with budget.business_operation("run-a", "next-window"):
        pass
    assert state(store)["requests"] == 2


WORKER = r'''
import os
import sys
from backend.query_budget import business_operation, BudgetError
sys.stdin.readline()
try:
    with business_operation(sys.argv[1], "child-query"):
        print("admitted", flush=True)
        if sys.stdin.readline().strip() == "crash":
            os._exit(9)  # 真正终止持有占位的Python进程，不执行finally
except BudgetError as error:
    print(error.code, flush=True)
'''


@contextmanager
def worker(run_id):
    process = subprocess.Popen([sys.executable, "-X", "utf8", "-c", WORKER, run_id],
                               cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                               env=os.environ.copy(), creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    try:
        yield process
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


@pytest.mark.parametrize("second_run,run_limit,global_limit,expected", [
    ("run-a", "1", "8", "QUERY_CONCURRENCY"),
    ("run-b", "2", "1", "GLOBAL_CONCURRENCY"),
])
def test_real_processes_atomically_compete_for_one_slot(store, monkeypatch, second_run, run_limit, global_limit, expected):
    monkeypatch.setenv("RISK_QUERY_RUN_CONCURRENCY", run_limit)
    monkeypatch.setenv("RISK_QUERY_GLOBAL_CONCURRENCY", global_limit)
    with worker("run-a") as first, worker(second_run) as second, ThreadPoolExecutor(max_workers=2) as readers:
        for process in (first, second):
            process.stdin.write("start\n")
            process.stdin.flush()
        messages = [readers.submit(process.stdout.readline) for process in (first, second)]
        outcomes = [future.result(timeout=15).strip() for future in messages]
        assert sorted(outcomes) == sorted(["admitted", expected]), outcomes
        assert state(store, "run-a")["requests"] + state(store, "run-b")["requests"] == 1
        winner = (first, second)[outcomes.index("admitted")]
        winner.stdin.write("finish\n")
        winner.stdin.flush()
        assert winner.wait(timeout=10) == 0
    with store.budget.business_operation("run-a", "after-completion"):
        assert state(store)["active"] == 1


def test_cancel_and_deadline_do_not_release_slot_before_operation_exits(store, monkeypatch):
    budget = store.budget
    monkeypatch.setenv("RISK_QUERY_RUN_CONCURRENCY", "1")
    cancelled = False
    with budget.business_operation("run-a", "cancelled-query", cancelled=lambda: cancelled) as ticket:
        cancelled = True
        with pytest.raises(budget.BudgetError) as error:
            ticket.check_cancelled()
        assert error.value.code == "CANCELLED"
        with pytest.raises(budget.BudgetError) as error:
            ticket.charge_export(rows=3, bytes=12)
        assert error.value.code == "CANCELLED"
        assert state(store)["rows"] == 3 and state(store)["bytes"] == 12
        assert state(store)["active"] == 1
        assert budget.reclaim_dead_owners("run-a") == 0
        # 独立线程没有父票据ContextVar，必须看到占位仍被实际操作持有。
        def attempt():
            with pytest.raises(budget.BudgetError) as denied:
                with budget.business_operation("run-a", "too-early"):
                    pytest.fail("取消仅发出信号时不能释放占位")
            return denied.value.code
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(attempt).result(timeout=5) == "QUERY_CONCURRENCY"
    assert state(store)["active"] == 0
    monkeypatch.setenv("RISK_QUERY_TIMEOUT_SECONDS", "0.01")
    with budget.business_operation("run-a", "expired-query") as ticket:
        time.sleep(0.02)
        with pytest.raises(budget.BudgetError) as error:
            ticket.remaining_seconds()
        assert error.value.code == "QUERY_TIMEOUT"
        assert state(store)["active"] == 1
        assert budget.reclaim_dead_owners("run-a") == 0
    assert state(store)["active"] == 0


def test_reclaim_requires_actual_owner_process_death(store, monkeypatch):
    monkeypatch.setenv("RISK_QUERY_RUN_CONCURRENCY", "1")
    with worker("run-a") as process:
        process.stdin.write("start\n")
        process.stdin.flush()
        with ThreadPoolExecutor(max_workers=1) as reader:
            assert reader.submit(process.stdout.readline).result(timeout=15).strip() == "admitted"
        assert store.budget.reclaim_dead_owners("run-a") == 0
        process.stdin.write("crash\n")
        process.stdin.flush()
        assert process.wait(timeout=10) == 9
        assert state(store)["active"] == 1
        assert store.budget.reclaim_dead_owners("run-a") == 1
        assert store.budget.reclaim_dead_owners("run-a") == 0
    with store.budget.business_operation("run-a", "after-reclaimed"):
        pass
    assert state(store)["requests"] == 2


def test_export_failure_keeps_actual_usage_and_reuse_does_not_charge_again(store, monkeypatch):
    budget = store.budget
    monkeypatch.setenv("RISK_EXPORT_MAX_ROWS", "5")
    monkeypatch.setenv("RISK_EXPORT_MAX_BYTES", "10")
    with budget.business_operation("run-a", "export") as ticket:
        ticket.charge_export(rows=2, bytes=6)
    # 数据集复用仍是一次逻辑请求，但不发生物理扫描/写入，不调用charge。
    with budget.business_operation("run-a", "cached-export"):
        pass
    assert state(store) == {"requests": 2, "rows": 2, "bytes": 6, "active": 0}
    with pytest.raises(budget.BudgetError) as error:
        with budget.business_operation("run-a", "failed-export") as ticket:
            ticket.charge_export(rows=4, bytes=8)
    assert error.value.code == "EXPORT_BUDGET_EXCEEDED"
    assert state(store) == {"requests": 3, "rows": 6, "bytes": 14, "active": 0}
    with pytest.raises(budget.BudgetError) as error:
        with budget.business_operation("run-a", "retry-export") as ticket:
            ticket.charge_export()
    assert error.value.code == "EXPORT_BUDGET_EXCEEDED"
    assert state(store)["rows"] == 6 and state(store)["bytes"] == 14


def test_export_can_charge_on_another_connection_while_read_cursor_is_open(store):
    with store.budget.business_operation("run-a", "streaming-export") as ticket:
        with store.factory() as reader:
            rows = reader.execute(text("SELECT id FROM analysis_runs ORDER BY id")).yield_per(1)
            assert next(rows)[0] == "run-a"
            ticket.charge_export(rows=1, bytes=10)
            assert next(rows)[0] == "run-b"
            ticket.charge_export(rows=1, bytes=10)
    assert state(store) == {"requests": 1, "rows": 2, "bytes": 20, "active": 0}


@pytest.mark.parametrize("failure_kind,expected_rows,expected_code", [
    ("tail-failure", 7, None),
    ("tail-cancel", 7, "CANCELLED"),
    ("charged-limit", 1000, "EXPORT_BUDGET_EXCEEDED"),
    ("charged-cancel", 1000, "CANCELLED"),
    ("before-publish", 1722, "CANCELLED"),
])
def test_failed_export_settles_real_tail_once_before_deleting_files(
    store, tmp_path, monkeypatch, failure_kind, expected_rows, expected_code,
):
    """真实SQLite/Parquet导出，在边界注入失败；核心额度记账和文件关闭不替换。"""
    from contextlib import closing
    from backend import services, task_workspace as workspace
    from backend.seed import METRICS, seed_database

    monkeypatch.setattr(services, "SessionLocal", store.factory)
    monkeypatch.setattr(workspace, "TASK_ROOT", tmp_path / "export-tasks")
    with store.factory() as db:
        seed_database(db)
        context = services.freeze_context(db, "run-a", db.get(store.models.Snapshot, "demo-2026-10-28"),
                                          "尾批结算", [], [f"LS{i:03d}" for i in range(1, 83)])
        run = db.get(store.models.AnalysisRun, "run-a")
        run.selection = {"kind": "code", "task_context": context}
        db.commit()
    paths = workspace.task_paths("run-a")
    cancelled = False
    ticket_ref = {}
    original_failure = ValueError("真实读取到七条后的原始业务失败")
    original_rows = services.iter_metrics

    if failure_kind in {"tail-failure", "tail-cancel"}:
        def interrupted_rows(*args, **kwargs):
            nonlocal cancelled
            with closing(original_rows(*args, **kwargs)) as rows:
                for index, row in enumerate(rows, 1):
                    yield row
                    if index == 7:
                        if failure_kind == "tail-failure":
                            raise original_failure
                        cancelled = True
                        ticket_ref["ticket"].check_cancelled()
        monkeypatch.setattr(services, "iter_metrics", interrupted_rows)
    if failure_kind == "tail-failure":
        # 异常收尾记账本身也超额，仍须保留最初的业务错误而非改报预算错误。
        monkeypatch.setenv("RISK_EXPORT_MAX_ROWS", "3")
    elif failure_kind == "charged-limit":
        monkeypatch.setenv("RISK_EXPORT_MAX_ROWS", "999")

    # 记录真实文件在删除前的最终字节数，包含writer/index关闭时flush的尾部。
    deleted_sizes = {}
    original_unlink = Path.unlink
    def measured_unlink(path, *args, **kwargs):
        if path.exists() and path.is_relative_to(paths["data"].parent) and (
            path.suffix == ".parquet" or path.name.endswith((".evidence.jsonl", ".schema.json"))
        ):
            deleted_sizes[path] = path.stat().st_size
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", measured_unlink)
    if failure_kind == "before-publish":
        original_write = Path.write_text
        def cancel_after_manifest_staging(path, *args, **kwargs):
            nonlocal cancelled
            result = original_write(path, *args, **kwargs)
            if path.name.endswith(".json.tmp") and path.parent == paths["meta"] / "exports":
                cancelled = True
            return result
        monkeypatch.setattr(Path, "write_text", cancel_after_manifest_staging)

    def cancellation_requested():
        return cancelled or (failure_kind == "charged-cancel" and
                             ticket_ref.get("ticket") is not None and
                             ticket_ref["ticket"].export_rows_charged >= 1000)

    with pytest.raises(ValueError) as raised:
        with store.budget.business_operation("run-a", "export_metrics", cancelled=cancellation_requested) as ticket:
            ticket_ref["ticket"] = ticket
            workspace.export_dataset(context, [metric[0] for metric in METRICS], budget=ticket)
    if expected_code is None:
        assert raised.value is original_failure
    else:
        assert raised.value.code == expected_code
    actual = state(store)
    assert actual["requests"] == 1 and actual["active"] == 0
    assert actual["rows"] == expected_rows
    assert actual["bytes"] == sum(deleted_sizes.values()) > 0
    assert ticket_ref["ticket"].export_rows_charged == expected_rows
    assert ticket_ref["ticket"].export_bytes_charged == actual["bytes"]
    assert not list(paths["data"].iterdir())
    assert not list((paths["meta"] / "exports").iterdir())

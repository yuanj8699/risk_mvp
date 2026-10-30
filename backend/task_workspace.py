"""任务文件与只读数据导出：输入已提交run_id，输出受限工作文件或Parquet清单。

data只挂载给容器读取，work供分析脚本写结果，meta保存宿主可信清单和执行记录，
从不挂载给代码。原始Decimal值按字符串落盘，引用核验不信任代码生成的证据内容。
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import threading

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select

from . import services
from .models import Indicator, Observation, ROOT

TASK_ROOT = Path(os.getenv("RISK_TASK_WORKSPACE_DIR", str(ROOT / "data" / "task-workspaces")))
MAX_TEXT_BYTES = 1024 * 1024
MAX_WORK_BYTES = 64 * 1024 * 1024
MAX_WORK_FILES = 100
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()
_held = threading.local()


class WorkspaceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise WorkspaceError("INVALID_ARGUMENT", "任务或执行ID格式不正确")
    return value


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def task_paths(run_id: str) -> dict[str, Path]:
    """先确认任务已提交，再创建三个职责不同的目录；不接受模型指定宿主路径。"""
    run_id = _identifier(run_id)
    services.load_task_context(run_id)
    root = TASK_ROOT / run_id
    if _is_link(TASK_ROOT) or _is_link(root):
        raise WorkspaceError("PATH_VIOLATION", "任务目录不能是符号链接")
    root.mkdir(parents=True, exist_ok=True)
    paths = {key: root / key for key in ("data", "work", "meta")}
    for path in paths.values():
        if _is_link(path):
            raise WorkspaceError("PATH_VIOLATION", "任务子目录不能是符号链接")
        path.mkdir(exist_ok=True)
    # Linux宿主的容器以65534运行，仅work目录开放写入；meta不共享给容器。
    if os.name != "nt":
        paths["work"].chmod(0o777)
    return paths


def _require_inactive(run_id: str) -> None:
    if (task_paths(run_id)["meta"] / "active-execution.json").exists():
        raise WorkspaceError("SANDBOX_ACTIVE", "隔离容器尚未确认清理，暂不允许访问或重新执行工作文件")


def mark_active_execution(run_id: str, execution_id: str, container_name: str) -> None:
    """在启动容器前落宿主标志；失败后保留，阻止其他API/MCP进程读取动态路径。"""
    path = task_paths(run_id)["meta"] / "active-execution.json"
    with path.open("x", encoding="utf-8") as file:
        json.dump({"execution_id": execution_id, "container_name": container_name}, file)


def clear_active_execution(run_id: str, execution_id: str, container_name: str) -> None:
    """仅在Docker已确认清理且本次身份匹配后由执行器调用，不开放给模型。"""
    path = task_paths(run_id)["meta"] / "active-execution.json"
    marker = json.loads(path.read_text(encoding="utf-8"))
    if marker != {"execution_id": execution_id, "container_name": container_name}:
        raise WorkspaceError("SANDBOX_ACTIVE", "活动容器身份不匹配，工作区保持锁定")
    path.unlink()


@contextmanager
def task_lock(run_id: str):
    """代码运行时不允许宿主文件API同时改写脚本或跟随正在变化的链接。"""
    with _locks_guard:
        lock = _locks.setdefault(_identifier(run_id), threading.RLock())
    with lock:
        held = getattr(_held, "runs", set())
        if run_id in held:
            yield
            return
        lock_path = task_paths(run_id)["meta"] / "workspace.lock"
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise WorkspaceError("WORKSPACE_BUSY", "该任务正在执行文件操作或代码，请稍后重试") from exc
        os.write(descriptor, json.dumps({"pid": os.getpid(), "run_id": run_id}).encode("utf-8"))
        os.close(descriptor)
        _held.runs = held | {run_id}
        try:
            _require_inactive(run_id)
            yield
        finally:
            _held.runs = held
            lock_path.unlink(missing_ok=True)


def work_path(run_id: str, path: str, *, must_exist: bool = False) -> Path:
    """只接受work内普通相对路径；包括Windows盘符/UNC和容器产生的符号链接。"""
    _require_inactive(run_id)
    if not isinstance(path, str) or not path or "\x00" in path or "\\" in path or ":" in path:
        raise WorkspaceError("PATH_VIOLATION", "文件路径须为工作区内的相对路径，使用/分隔")
    relative = PurePosixPath(path)
    if relative.is_absolute() or any(part in {".", ".."} for part in path.split("/")):
        raise WorkspaceError("PATH_VIOLATION", "不接受绝对路径或目录穿越")
    base = task_paths(run_id)["work"]
    target = base.joinpath(*relative.parts)
    for parent in [target, *target.parents]:
        if parent == base.parent:
            break
        if _is_link(parent):
            raise WorkspaceError("PATH_VIOLATION", "工作文件不能经过符号链接")
    if not target.resolve().is_relative_to(base.resolve()):
        raise WorkspaceError("PATH_VIOLATION", "文件超出任务工作区")
    if must_exist and (not target.is_file() or not stat.S_ISREG(target.stat().st_mode)):
        raise WorkspaceError("FILE_NOT_FOUND", "指定工作文件不存在或不是普通文件")
    return target


def _file_info(path: Path, base: Path) -> dict:
    return {"path": path.relative_to(base).as_posix(), "size": path.stat().st_size, "sha256": file_sha256(path)}


def write_task_file(run_id: str, path: str, content: str) -> dict:
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_TEXT_BYTES:
        raise WorkspaceError("INVALID_ARGUMENT", "单个文本文件最多1MiB")
    with task_lock(run_id):
        target = work_path(run_id, path)
        try:
            mode = target.lstat().st_mode
        except FileNotFoundError:
            mode = None
        if mode is not None and not stat.S_ISREG(mode):
            raise WorkspaceError("PATH_VIOLATION", "只能覆盖普通文件，不能写入管道、设备或目录")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return _file_info(target, task_paths(run_id)["work"])


def read_task_file(run_id: str, path: str) -> dict:
    with task_lock(run_id):
        target = work_path(run_id, path, must_exist=True)
        if target.stat().st_size > MAX_TEXT_BYTES:
            raise WorkspaceError("OUTPUT_LIMIT", "文件超过1MiB，请先在隔离环境生成较小的摘要")
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("INVALID_ARGUMENT", "只读取UTF-8文本；二进制数据请在隔离环境分析") from exc
        return {**_file_info(target, task_paths(run_id)["work"]), "content": content, "truncated": False}


def list_task_files(run_id: str) -> list[dict]:
    with task_lock(run_id):
        base = task_paths(run_id)["work"]
        files = []
        for path in sorted(base.rglob("*")):
            if _is_link(path):
                raise WorkspaceError("PATH_VIOLATION", "工作区包含符号链接，不跟随读取")
            if path.is_file():
                size = path.stat().st_size
                if len(files) >= MAX_WORK_FILES or sum(item["size"] for item in files) + size > MAX_WORK_BYTES:
                    raise WorkspaceError("OUTPUT_LIMIT", "任务工作区超过100文件或64MiB输出限制")
                files.append(_file_info(path, base))
        return files


def work_usage(run_id: str) -> tuple[int, int]:
    """运行中只用lstat计数，不读取代码生成文件，也不跟随链接。"""
    count = size = 0
    for visited, path in enumerate((TASK_ROOT / _identifier(run_id) / "work").rglob("*"), 1):
        if visited > 500:
            return MAX_WORK_FILES + 1, size
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            count += 1
            size += info.st_size
            if count > MAX_WORK_FILES or size > MAX_WORK_BYTES:
                break
    return count, size


def _arrow_schema() -> pa.Schema:
    names = ("evidence_id", "id", "org_id", "indicator_id", "indicator_name", "value", "value_display", "unit",
             "data_date", "snapshot_id", "source_record_id", "quality_status", "rank_scope", "source", "issue", "current_issue")
    return pa.schema([(key, pa.string()) for key in names] + [("rank", pa.int64()), ("population", pa.int64()), ("usable", pa.bool_())])


def export_dataset(context: dict, indicator_ids: list[str], org_ids: list[str] | None = None,
                   history: bool = True, *, cancelled: Callable[[], bool] | None = None) -> dict:
    """一次流式查询完整导出；只向代码返回文件路径与摘要，不回传大数组。

    context须来自宿主已提交任务。meta中的清单和证据索引不挂载给容器，供后续引用复核。
    """
    trusted = services.load_task_context(context["run_id"])
    if any(context.get(key) != trusted.get(key) for key in trusted if key != "org_scope"):
        raise WorkspaceError("SCOPE_VIOLATION", "导出上下文与已提交任务不一致")
    if not set(context["org_scope"]) <= set(trusted["org_scope"]):
        raise WorkspaceError("SCOPE_VIOLATION", "导出机构超出已提交任务")
    paths = task_paths(context["run_id"])
    if not isinstance(indicator_ids, list) or not indicator_ids or any(not isinstance(key, str) for key in indicator_ids):
        raise WorkspaceError("INVALID_ARGUMENT", "indicator_ids须为非空指标ID列表")
    if not isinstance(history, bool):
        raise WorkspaceError("INVALID_ARGUMENT", "history须为布尔值")
    query = {"indicator_ids": sorted(set(indicator_ids)), "org_ids": services._scope(context, org_ids), "history": history}
    context_hash = _json_hash(trusted)
    dataset_id = _json_hash({"context": context_hash, "query": query})[:32]
    parquet = paths["data"] / f"{dataset_id}.parquet"
    schema_path = paths["data"] / f"{dataset_id}.schema.json"
    exports_meta = paths["meta"] / "exports"
    exports_meta.mkdir(exist_ok=True)
    evidence_index = exports_meta / f"{dataset_id}.evidence.jsonl"
    manifest_path = exports_meta / f"{dataset_id}.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            unchanged = (file_sha256(parquet) == manifest["sha256"] and
                         file_sha256(schema_path) == manifest["schema_sha256"] and
                         file_sha256(evidence_index) == manifest["evidence_index_sha256"])
        except OSError:
            unchanged = False
        if not unchanged:
            raise WorkspaceError("EVIDENCE_CHANGED", "已有任务数据集校验失败，不能复用")
        return {**manifest, "reused": True}
    # 同一任务同一查询只发布一份完整数据；锁文件仅在宿主meta中。
    export_lock = exports_meta / f"{dataset_id}.lock"
    try:
        descriptor = os.open(export_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise WorkspaceError("WORKSPACE_BUSY", "相同任务数据集正在导出，请稍后重试") from exc
    os.close(descriptor)
    schema = _arrow_schema()
    row_count, actual_snapshots = 0, set()
    try:
        with pq.ParquetWriter(parquet, schema, compression="zstd") as writer, evidence_index.open("w", encoding="utf-8") as index:
            batch = []
            for row in services.iter_metrics(context, indicator_ids, org_ids, history):
                if cancelled and cancelled():
                    raise WorkspaceError("TIMEOUT", "数据导出已取消")
                batch.append(row)
                index.write(json.dumps(row, ensure_ascii=False) + "\n")
                row_count += 1
                actual_snapshots.add(row["snapshot_id"])
                if len(batch) == 1000:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                    batch.clear()
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
        if cancelled and cancelled():
            raise WorkspaceError("TIMEOUT", "数据导出已取消")
        definitions = [item for item in services.search_indicators(limit=1000)["items"] if item["id"] in indicator_ids]
        schema_description = {"columns": [{"name": field.name, "type": str(field.type)} for field in schema],
                              "indicators": definitions, "value_encoding": "decimal_string", "query": query}
        schema_path.write_text(json.dumps(schema_description, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest = {
            "dataset_id": dataset_id, "run_id": context["run_id"], "source": context["source"], "as_of": context["as_of"],
            "path": f"/data/{parquet.name}", "schema_path": f"/data/{schema_path.name}",
            "row_count": row_count, "sha256": file_sha256(parquet), "schema_sha256": file_sha256(schema_path),
            "evidence_index_sha256": file_sha256(evidence_index),
            "query": query, "snapshot_ids": sorted(actual_snapshots), "allowed_snapshot_ids": context["history_snapshot_ids"],
            "context_sha256": context_hash, "created_at": datetime.now(timezone.utc).isoformat(),
        }
        staged_manifest = manifest_path.with_suffix(".json.tmp")
        staged_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        staged_manifest.replace(manifest_path)
        return {**manifest, "reused": False}
    except Exception:
        # 只清理由本次随机ID创建的文件；不留下看似完整的半份数据集。
        for path in (parquet, schema_path, evidence_index):
            path.unlink(missing_ok=True)
        raise
    finally:
        export_lock.unlink(missing_ok=True)


def resolve_evidence(run_id: str, evidence_ids: list[str]) -> list[dict]:
    """仅接受本任务实际导出的证据ID，并以宿主清单及原始数据库观测核验。"""
    if not isinstance(evidence_ids, list) or not evidence_ids or len(evidence_ids) > 1000 or any(not isinstance(x, str) for x in evidence_ids):
        raise WorkspaceError("INVALID_ARGUMENT", "evidence_ids须为1至1000个证据ID")
    context = services.load_task_context(run_id)
    paths = task_paths(run_id)
    requested, found = set(evidence_ids), {}
    for manifest_path in sorted((paths["meta"] / "exports").glob("*.json")):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest["context_sha256"] != _json_hash(context):
            raise WorkspaceError("EVIDENCE_CHANGED", "任务上下文已改变，导出证据不可复用")
        data_path = paths["data"] / f"{manifest['dataset_id']}.parquet"
        index_path = manifest_path.parent / f"{manifest['dataset_id']}.evidence.jsonl"
        if file_sha256(data_path) != manifest["sha256"] or file_sha256(index_path) != manifest["evidence_index_sha256"]:
            raise WorkspaceError("EVIDENCE_CHANGED", "只读导出文件校验失败")
        with index_path.open(encoding="utf-8") as index:
            for line in index:
                row = json.loads(line)
                if row["evidence_id"] in requested:
                    found[row["evidence_id"]] = row
    if requested - set(found):
        raise WorkspaceError("UNKNOWN_EVIDENCE", "引用包含本任务尚未导出的证据ID")
    with services.read_session() as db:
        snapshots = services._snapshots(db, context)
        records = db.execute(select(Observation, Indicator).join(Indicator, Observation.indicator_id == Indicator.id)
                             .where(Observation.id.in_(requested))).all()
        if len(records) != len(requested):
            raise WorkspaceError("EVIDENCE_CHANGED", "原始观测已不可读取")
        for obs, metric in records:
            saved = found[obs.id]
            current = services.observation_record(obs, metric)
            if any(saved[key] != value for key, value in current.items()) or obs.org_id not in context["org_scope"] or obs.snapshot_id not in snapshots:
                raise WorkspaceError("EVIDENCE_CHANGED", "导出证据与原始观测不一致")
            if not saved.get("usable") or obs.quality_status != "valid" or obs.value is None:
                raise WorkspaceError("INVALID_EVIDENCE", "不能引用缺失或质量不合格的观测")
    return [found[key] for key in dict.fromkeys(evidence_ids)]


def save_execution(run_id: str, execution_id: str, script: bytes, result: dict) -> None:
    """宿主保存实际执行脚本与结果，容器不能改写执行历史。"""
    directory = task_paths(run_id)["meta"] / "executions" / _identifier(execution_id)
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "script.py").write_bytes(script)
    staged_result = directory / "result.json.tmp"
    staged_result.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    staged_result.replace(directory / "result.json")


def load_execution(run_id: str, execution_id: str) -> dict:
    path = task_paths(run_id)["meta"] / "executions" / _identifier(execution_id) / "result.json"
    if not path.is_file():
        raise WorkspaceError("FILE_NOT_FOUND", "执行记录不存在")
    return json.loads(path.read_text(encoding="utf-8"))


# API下载与最终提交核验共用同一边界，不另外拼接宿主路径。
resolve_work_path = work_path
get_execution = load_execution


def list_executions(run_id: str) -> list[dict]:
    root = task_paths(run_id)["meta"] / "executions"
    items = [json.loads(path.read_text(encoding="utf-8")) for path in root.glob("*/result.json")]
    return sorted(items, key=lambda item: item["started_at"])

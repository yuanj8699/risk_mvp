"""QuickBI 长表与宽表取数器。按指标、实际日期和当前身份取数，源表合计与行社事实分别保存。"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re

from .quickbi_client import (DEFAULT_COMPONENT_ID, QuickBiClient, QuickBiError, SmartQStreamClient,
                             build_olap_param, sign_openapi_request)
from .risk_knowledge import load_quickbi_bindings
from .services import display, number


_CAPTIONS = {"机构名称": "org_name", "机构号": "org_code", "指标名称": "indicator",
             "指标值": "value", "指标单位": "unit", "全省排名": "rank", "全系统平均值": "system_average"}
_DATE = re.compile(r"数据日期(?:[（(\[]\s*(day|month|quarter|year)\s*[)）\]])?", re.IGNORECASE)


def _source_error(message: str) -> QuickBiError:
    return QuickBiError("QUICKBI_RESPONSE", message)


def parse_layout(data: dict) -> dict:
    """解析当前 cube 的 fid。副表中可缺排名等辅助列，查询所需主键、指标值和日期必须齐全。"""
    if not isinstance(data, dict):
        raise _source_error("QuickBI infoNew 响应缺少数据集。")
    fields, dates, measures = {}, [], set()
    captions = {}
    for kind in ("dimensionList", "measureList"):
        if not isinstance(data.get(kind), list):
            raise _source_error(f"QuickBI infoNew 缺少 {kind}。")
        for field in data[kind]:
            if not isinstance(field, dict):
                raise _source_error("QuickBI 字段元数据格式错误。")
            caption, fid = field.get("caption"), field.get("uid")
            if not isinstance(caption, str) or not isinstance(fid, str) or not fid:
                continue
            captions[caption] = {"fid": fid, "kind": "measure" if kind == "measureList" else "dimension"}
            if kind == "measureList":
                measures.add(fid)
            if caption in _CAPTIONS:
                key = _CAPTIONS[caption]
                if key in fields and fields[key] != fid:
                    # 同名维度和度量可共存。机构、指标名、单位取维度，值和排名优先取度量。
                    if kind != "measureList" or fields[key] in measures:
                        raise _source_error(f"QuickBI 表内字段「{caption}」对应多个 fid。")
                    if key not in {"value", "rank", "system_average"}:
                        continue
                fields[key] = fid
            match = _DATE.fullmatch(caption.strip())
            if match:
                dates.append((["", "day", "month", "quarter", "year"].index((match[1] or "").lower()), fid))
    missing = [caption for caption, key in _CAPTIONS.items() if key not in fields]
    if not dates:
        missing.append("数据日期")
    if {"indicator", "value"} <= fields.keys():
        table_mode = "long"
    elif {"org_name", "org_code"} <= fields.keys() and dates and measures:
        table_mode = "wide"
    else:
        table_mode = None
    return {**{key: data.get(key) for key in ("datasetId", "datasetName", "workspaceId", "workspaceName",
                                               "dsId", "dsName", "dsType")},
            "table_mode": table_mode, "captions": captions, "fields": fields,
            "measure_ids": measures, "missing": missing,
            "date_candidates": list(dict.fromkeys(fid for _, fid in sorted(dates, key=lambda x: x[0])))}


def _org_directory(path: str | Path | None) -> dict[str, dict]:
    if not path:
        raise QuickBiError("QUICKBI_CONFIG", "请配置 RISK_QUICKBI_ORG_FILE 指向机构归属.json。")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (ValueError, UnicodeError, OSError):
        raise QuickBiError("QUICKBI_CONFIG", "无法读取机构归属.json，请检查挂载和 JSON 编码。") from None
    if not isinstance(data, dict):
        raise QuickBiError("QUICKBI_CONFIG", "机构归属.json 应为以机构名称索引的机构目录。")
    directory = {}
    for name, row in data.items():
        if not isinstance(row, dict) or row.get("代码") in (None, "") or not name:
            raise QuickBiError("QUICKBI_CONFIG", "机构目录缺少机构名称或代码。")
        ident = str(row["代码"])
        level = str(row.get("层级", "")).zfill(2)
        if ident in directory or level not in {"01", "02", "03"}:
            raise QuickBiError("QUICKBI_CONFIG", "机构目录含重复代码或无效层级。")
        directory[ident] = {"org_id": ident, "org_name": name, "level": level,
                            "parent_id": row.get("上级"), "region_name": ""}
    counts = Counter(org["level"] for org in directory.values())
    if counts != {"01": 1, "02": 11, "03": 82} or directory.get("999999", {}).get("level") != "01":
        raise QuickBiError("QUICKBI_CONFIG", "机构目录应包含 999999 全省合计、11 个地区和 82 家行社。")
    codes = {v["org_name"]: k for k, v in directory.items()}
    for org in directory.values():
        if org["level"] == "01":
            org["parent_id"] = None
            continue
        parent = str(org["parent_id"])
        parent = directory.get(codes.get(parent, parent))
        if not parent or parent["level"] != {"02": "01", "03": "02"}[org["level"]]:
            raise QuickBiError("QUICKBI_CONFIG", "机构目录上级机构缺失或层级不匹配。")
        org["parent_id"] = parent["org_id"]
        org["region_name"] = parent["org_name"] if org["level"] == "03" else org["org_name"]
    return directory


def _rows(result: dict, fields: list[tuple[str, dict]]) -> list[dict]:
    """引擎按所有 row 字段、再所有 column 字段排列；列长度或顺序有冲突时中止。"""
    fields = sorted(fields, key=lambda item: item[1]["areaType"] == "column")
    labels = [name for name, _ in fields]
    if not isinstance(result, dict):
        raise _source_error("QuickBI OLAP 响应缺少结果对象。")
    if isinstance(result.get("data"), dict):
        result = result["data"]
    # 旧网关用单独的空 columns 表示零行；保留此语义，才能继续试探其他日期字段。
    if result.get("columns") == [] and not any(key in result for key in ("rows", "values", "metaType", "data")):
        return []
    for key in ("metaType", "columns"):
        if key in result:
            metadata = result[key]
            if not isinstance(metadata, list) or len(metadata) != len(fields):
                raise _source_error("QuickBI OLAP 结果列数与请求字段数不一致。")
            for column, (_, field) in zip(metadata, fields):
                if isinstance(column, dict) and column.get("fid", field["fid"]) != field["fid"]:
                    raise _source_error("QuickBI OLAP 结果 fid 与字段顺序不一致。")
    if "values" in result or isinstance(result.get("data"), list):
        values = result.get("values", result.get("data"))
        if not isinstance(values, list):
            raise _source_error("QuickBI OLAP values 格式错误。")
        result = {**result, "rows": [item.get("row") if isinstance(item, dict) else item for item in values]}
    if "rows" in result:
        rows = result["rows"]
        if not isinstance(rows, list) or any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
            raise _source_error("QuickBI OLAP 结果行宽度与请求字段数不一致。")
    elif isinstance(result.get("columns"), list):
        columns = result["columns"]
        vectors = []
        for column in columns:
            values = column.get("values") if isinstance(column, dict) else column
            if not isinstance(values, list):
                raise _source_error("QuickBI OLAP 结果列缺少 values 数组。")
            vectors.append(values)
        if len({len(values) for values in vectors}) != 1:
            raise _source_error("QuickBI OLAP 各列行数不一致。")
        rows = zip(*vectors)
    else:
        raise _source_error("QuickBI OLAP 响应缺少可识别的结果列。")
    return [dict(zip(labels, row)) for row in rows]


def _condition(fid: str, value: str) -> dict:
    return {"field": {"fid": fid}, "functionalOperator": "in", "args": [{"valueType": "string", "value": value}]}


def _query(client, cube: str, fields: list[tuple[str, dict]], conditions: list[dict], limit: int) -> list[dict]:
    payload = build_olap_param(cube_id=cube, fields=[field for _, field in fields], conditions=conditions,
                               component_id=os.getenv("QB_COMPONENT_ID") or DEFAULT_COMPONENT_ID, limit=limit)
    return _rows(client.olap_query(payload), fields)


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        parsed = Decimal(str(value))
    except InvalidOperation:
        raise _source_error("QuickBI 指标值不是有效的十进制数。") from None
    if not parsed.is_finite():
        raise _source_error("QuickBI 指标值不是有限数。")
    return parsed


def _observation(raw: dict, org: dict, metric: str, entry: dict, target: str, snapshot: str) -> dict:
    try:
        actual_date = date.fromisoformat(str(raw["date"])).isoformat()
    except ValueError:
        raise _source_error("QuickBI 返回的数据日期不是明确的日历日期。") from None
    if actual_date != target:
        raise _source_error(f"QuickBI 源表日期与请求期次 {target} 不一致。")
    value = _decimal(raw["value"])
    # 部分窄表的单位列使用代码 1；此时以挂载字典声明的业务单位展示，仍保留原始单位字段。
    raw_unit = raw.get("unit")
    unit = str(entry.get("unit") or "") if raw_unit in (None, "", 1, "1") else str(raw_unit)
    issue = "指标值缺失" if value is None else "指标单位缺失" if not unit else None
    rank = _decimal(raw.get("rank"))
    rank = int(rank) if rank is not None and rank == rank.to_integral_value() and 1 <= rank <= 82 else None
    ranked = rank is not None and org["level"] == "03"
    key = [entry["cubeId"], entry["indicatorName"], org["org_id"], actual_date]
    ident = "qb-" + hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()
    return {"org_id": org["org_id"], "org_name": org["org_name"], "region_name": org["region_name"],
            "source_org_name": raw["org_name"],
            "indicator_id": metric, "indicator_name": entry["indicatorName"], "value": number(value),
            "raw_unit": raw_unit, "unit": unit, "value_display": display(value, unit),
            "rank": rank if ranked else None, "population": 82 if ranked else None,
            "rank_scope": "province_societies" if ranked else None,
            "quality_status": "valid" if not issue else "missing", "usable": issue is None, "issue": issue,
            "id": ident, "evidence_id": ident, "source_record_id": ident, "data_date": actual_date,
            "snapshot_id": snapshot, "source": "quickbi"}


class QuickBiOlapFetcher:
    """按已装载的指标字典查询长表或宽表。仅缓存元数据及成功的日期 fid，身份相关的数据每次单独读取。"""

    def __init__(self, client: QuickBiClient | None = None, *, org_file=None, dict_file=None):
        self.client = client or QuickBiClient()
        self.bindings = load_quickbi_bindings(dict_file)
        self.org_directory = _org_directory(org_file or os.getenv("RISK_QUICKBI_ORG_FILE"))
        self.organizations = [{k: org[k] for k in ("org_id", "org_name", "region_name")}
                              for org in sorted(self.org_directory.values(), key=lambda r: r["org_id"])
                              if org["level"] == "03"]
        self._layouts = {}
        self.date_fid_cache = {}

    def layout(self, cube_id: str) -> dict:
        if cube_id not in self._layouts:
            self._layouts[cube_id] = parse_layout(self.client.dataset_info(cube_id))
        return self._layouts[cube_id]

    def live_indicators(self, cube_id: str, *, limit: int = 500) -> list[str]:
        """枚举当前身份可见的长表指标或宽表度量。"""
        layout = self.layout(cube_id)
        if layout["table_mode"] == "wide":
            return sorted(name for name, field in layout["captions"].items() if field["kind"] == "measure")[:limit]
        if layout["table_mode"] is None:
            raise _source_error(f"QuickBI 表缺少字段：{'、'.join(layout['missing'])}。")
        field = {"fid": layout["fields"]["indicator"], "areaType": "row"}
        rows = _query(self.client, cube_id, [("name", field)], [], limit)
        return sorted({r["name"] for r in rows if isinstance(r["name"], str) and r["name"].strip()})

    def _series(self, entry: dict, target: str, available: dict) -> tuple[list[dict], str | None]:
        cube, name = entry["cubeId"], entry["indicatorName"]
        layout = self.layout(cube)
        fields = layout["fields"]
        required = set(layout["missing"]) & {"机构号", "机构名称", "指标名称", "指标值", "数据日期"}
        if layout["table_mode"] == "wide":
            required -= {"指标值", "指标名称"}
        if required:
            raise _source_error(f"QuickBI 表「{layout['datasetName']}」缺少字段：{'、'.join(sorted(required))}。")
        candidates = list(layout["date_candidates"])
        cached = self.date_fid_cache.get(cube)
        if cached in candidates:
            candidates.remove(cached)
            candidates.insert(0, cached)
        if layout["table_mode"] == "wide":
            names = [entry.get("measureCaption") or name, *(entry.get("synonyms") or [])]
            value = next((layout["captions"][n]["fid"] for n in names
                          if layout["captions"].get(n, {}).get("kind") == "measure"), None)
            if not value:
                raise _source_error(f"QuickBI 宽表中找不到指标「{name}」对应的度量。")
            conditions = []
        else:
            value = fields["value"]
            conditions = [_condition(fields["indicator"], name)]
        value_field = ("value", {"fid": value, "areaType": "column", "aggregate": "sum"})
        valid_dates = []
        for candidate in candidates:
            selected = [(key, {"fid": fid, "areaType": "row"}) for key, fid in
                        (("org_name", fields["org_name"]), ("org_code", fields["org_code"]), ("date", candidate))]
            selected.append(value_field)
            # 长表的辅助列与指标行绑定；宽表的同名列可能属于其他度量。
            for key in (("unit", "rank") if layout["table_mode"] == "long" else ()):
                if key in fields:
                    field = {"fid": fields[key], "areaType": "row"}
                    if fields[key] in layout["measure_ids"]:
                        field.update(areaType="column", aggregate="max")
                    selected.append((key, field))
            try:
                rows = _query(self.client, cube, selected,
                              [*conditions, _condition(candidate, target.replace("-", ""))], 5000)
            except QuickBiError as exc:
                if exc.code != "NOX5200010":
                    raise
                continue
            if rows:
                self.date_fid_cache[cube] = candidate
                return rows, None
            valid_dates.append(candidate)
        if not valid_dates:
            raise _source_error(f"QuickBI 表「{layout['datasetName']}」的全部候选日期字段均已失效。")
        if (cube, name) not in available:
            dates = set()
            for candidate in valid_dates:
                rows = _query(self.client, cube, [("date", {"fid": candidate, "areaType": "row"}), value_field],
                              conditions, 200)
                dates.update(str(row["date"]) for row in rows if row.get("date") not in (None, ""))
                if any(len(day) >= 6 for day in dates):
                    break
            available[cube, name] = "、".join(sorted(dates)) or "当前身份未查到可用日期"
        return [], f"「{name}」在 {target} 无数据。可用数据日期：{available[cube, name]}。"

    def fetch(self, context: dict, indicator_ids: Iterable[str]) -> dict:
        requested = set(indicator_ids)
        current = date.fromisoformat(context["as_of"]).isoformat()
        periods = sorted({current, *(date.fromisoformat(value).isoformat() for value in context.get("history_dates", []))})
        if periods[-1] > current:
            raise QuickBiError("INVALID_ARGUMENT", "历史期次不得晚于当前扫描日期。")
        scoped = set(context.get("org_scope", [o["org_id"] for o in self.organizations]))
        indicators, latest, history, notes = [], [], [], []
        visible, totals, available_dates = set(), {}, {}
        for metric in sorted(requested & self.bindings.keys()):
            entry = self.bindings[metric]
            catalog = {"indicator_id": metric, "indicator_name": entry["indicatorName"],
                       "unit": entry.get("unit") or "", "frequency": entry.get("frequency") or ""}
            indicators.append(catalog)
            for target in periods:
                raw_rows, note = self._series(entry, target, available_dates)
                if note:
                    notes.append(note)
                seen = set()
                for raw in raw_rows:
                    code = str(raw["org_code"])
                    org = self.org_directory.get(code)
                    if not org:
                        raise _source_error("QuickBI 返回机构号未收录于挂载目录，请核对数据集与机构归属。")
                    if org["level"] == "03" and code not in scoped:
                        continue
                    if code in seen:
                        raise _source_error(f"QuickBI 指标「{entry['indicatorName']}」同一机构、期次存在重复行。")
                    seen.add(code)
                    snapshot = context["snapshot_id"] if target == current else f"quickbi:{target}"
                    row = _observation(raw, org, metric, entry, target, snapshot)
                    if not catalog["unit"] and row["unit"]:
                        catalog["unit"] = row["unit"]
                    if org["level"] == "03":
                        history.append(row)
                        if target == current:
                            visible.add(code)
                            latest.append(row)
                    elif target == current:
                        aggregate = totals.setdefault(metric, {"province": None, "regions": {}})
                        if code == "999999":
                            aggregate["province"] = row
                        else:
                            aggregate["regions"][code] = row
        organizations = [o for o in self.organizations if o["org_id"] in visible]
        # 机构级扫描以当期可见范围为准。历史中曾出现的行社不能扩大本次任务范围。
        history = [row for row in history if row["org_id"] in visible]
        found = {(r["org_id"], r["indicator_id"]) for r in latest}
        missing = [{"org_id": code, "indicator_id": i["indicator_id"], "status": "missing"}
                   for code in sorted(visible) for i in indicators if (code, i["indicator_id"]) not in found]
        count = len(organizations)
        note = (f"全量 82 家行社，当前身份仅可见 {count} 家（行级权限已裁剪）。" if count else
                "全量 82 家行社，当前返回 0 家；请核对所选期次、数据源和当前身份的行级权限。")
        return {"snapshot_id": context["snapshot_id"], "as_of": current, "source": "quickbi",
                "organizations": organizations, "indicators": indicators, "latest": latest, "history": history,
                "missing": missing, "missing_source": sorted(requested - self.bindings.keys()),
                "source_totals": totals, "date_notes": notes,
                "visibility": {"expected_org_count": 82, "visible_org_count": count, "clipped": count < 82,
                               "visible_note": note if count < 82 else None}}

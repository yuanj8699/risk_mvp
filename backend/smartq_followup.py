"""追问补查的 Python 宿主边界。仅把规范问句送入流式问数，按任务范围输出可引用记录。"""
from __future__ import annotations

import calendar
from collections import Counter
from datetime import date
from functools import lru_cache
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit

from .quickbi_client import QuickBiError, SmartQStreamClient
from .quickbi_adapter import _org_directory


PROVINCE = "全省合计（含宁波）"
_QUERY_TYPES = {"single", "total", "diff", "ratio", "rank", "extremum", "detail", "count", "yoy", "mom", "check", "filter", "other"}
_LOOKUP_FIELDS = ("rewritten_question", "reason", "metric_hint", "target_org", "target_date", "query_type")
_PROVINCE_NAME = re.compile(r"全省(?:合计|汇总|总计)(?:[（(]含宁波[）)])?|全省[（(]含宁波[）)]|全省(?!排名|位次|排位|各|所有|全部|范围|前\d)")
_BANK_BANK_PATH = Path(__file__).with_name("smartq_question_bank.json")
_DATE = re.compile(r"(?<!\d)(20\d{2})(?:年|[-/])?(0?[1-9]|1[0-2])(?:月(?:末|底|(\d{1,2})[日号])?|[-/](\d{1,2})|(?<=\d)(\d{2}))?(?!\d)")
_REFERENCE = "来源：小 Q 实时补查，口径仅供参考"
_SCOPE_NOTE = "当前追问限定单一行社，无法补查其他行社或跨机构明细。"
_SAFE_NOTES = {
    "SMARTQ_SCOPE": _SCOPE_NOTE, "SMARTQ_QUESTION": "小 Q 问句缺少有效的指标、期次或查询方式，请补充后再问。",
    "QUICKBI_CONFIG": "小 Q 配置无效，请核对内网服务配置。",
    "QUICKBI_CONNECTION": "小 Q 网络连接中断或超时，请检查内网连接。",
    "QUICKBI_SSE": "小 Q 未返回有效的流式查询结果。", "QUICKBI_RESPONSE": "小 Q 返回的数据格式无效。",
    "QUICKBI_REQUEST": "小 Q 拒绝了本次请求，请核对账号权限。",
    "NOX5200010": "小 Q 查询字段无效。", "OE10010106": "小 Q 接口未授权。",
    "AUTHX_01002": "小 Q 身份已失效。", "OE10010101": "小 Q 签名时间偏差过大。",
    "AE0130010003": "内网报表尚未开通三方嵌入。",
}


def _dict_cube(path: str, hint: str) -> str:
    """优先选名称或同义词精确匹配的 cube，然后取字典默认值或最多指标共用的主表。"""
    doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    entries = doc.get("metrics", doc) if isinstance(doc, dict) else doc
    if isinstance(entries, dict):
        items = entries.items()
    elif isinstance(entries, list):
        items = ((row.get("name"), row) for row in entries if isinstance(row, dict))
    else:
        items = ()
    counts = Counter()
    for name, row in items:
        if not isinstance(row, dict):
            continue
        source = row.get("source")
        cube = row.get("cubeId") or (source.get("cubeId") if isinstance(source, dict) else None)
        if not isinstance(cube, str) or not cube.strip() or str(row.get("status")).lower() in {
            "disabled", "unavailable", "missing", "no_source", "missing_source", "pending", "停用", "缺源", "无源", "待确认"
        }:
            continue
        aliases = row.get("synonyms")
        if hint and (hint in (name, row.get("indicatorName")) or isinstance(aliases, list) and hint in aliases):
            return cube.strip()
        counts[cube.strip()] += 1
    if isinstance(doc, dict):
        default = doc.get("defaultCubeId") or doc.get("cubeId")
        if isinstance(default, str) and default.strip():
            return default.strip()
    return counts.most_common(1)[0][0] if counts else ""


def smartq_followup_config(metric_hint: str = "") -> dict:
    """检查部署配置，不返回主机、AK、SK 或任何会话凭据。"""
    env = os.environ
    mode = env.get("RISK_SMARTQ_FOLLOWUP", "auto").strip().lower()
    cfg = {"mode": mode, "enabled": False, "cube_id": env.get("QB_SMARTQ_CUBE_ID", "").strip(),
           "user_id": env.get("QB_SMARTQ_USER_ID", "").strip() or env.get("QB_USER_ID", "").strip(),
           "inference_llm": env.get("QB_SMARTQ_INFERENCE_LLM") or "nv1",
           "data_llm": env.get("QB_SMARTQ_DATA_LLM") or "nv1", "note": ""}
    if mode not in {"auto", "on", "off"}:
        return {**cfg, "note": "RISK_SMARTQ_FOLLOWUP 应为 auto、on 或 off。"}
    if mode == "off":
        return {**cfg, "note": "小 Q 追问补查已关闭。"}
    if not cfg["cube_id"] and env.get("RISK_QUICKBI_DICT_FILE"):
        try:
            cfg["cube_id"] = _dict_cube(env["RISK_QUICKBI_DICT_FILE"], metric_hint)
        except (ValueError, OSError):
            return {**cfg, "note": "无法读取小 Q 指标字典，请检查挂载文件。"}
    host = env.get("QB_HOST") or env.get("QB_BASE_URL", "")
    required = {"QB_HOST 或 QB_BASE_URL": host, "QB_ACCESS_ID": env.get("QB_ACCESS_ID"),
                "QB_ACCESS_SECRET": env.get("QB_ACCESS_SECRET"), "QB_SMARTQ_USER_ID 或 QB_USER_ID": cfg["user_id"],
                "QB_SMARTQ_CUBE_ID 或字典数据集": cfg["cube_id"]}
    missing = [key for key, value in required.items() if not value or not value.strip()]
    if missing:
        return {**cfg, "note": "小 Q 缺少配置：" + "、".join(missing) + "。"}
    try:
        origin = urlsplit(host.rstrip("/"))
        valid = (origin.scheme in {"http", "https"} and origin.hostname and origin.port != 0
                 and not (origin.username or origin.password or origin.path or origin.query or origin.fragment))
    except ValueError:
        valid = False
    if not valid:
        return {**cfg, "note": "小 Q 服务地址格式无效。"}
    return {**cfg, "enabled": True}


def is_smartq_followup_available() -> bool:
    return smartq_followup_config()["enabled"]


@lru_cache(maxsize=1)
def load_smartq_question_bank() -> dict:
    """加载银行监管底线、业务参考预警线及源自业务 Excel 的单项指标问句知识库。"""
    if not _BANK_BANK_PATH.is_file():
        return {"threshold_rules": [], "excel_templates": []}
    return json.loads(_BANK_BANK_PATH.read_text(encoding="utf-8"))


def _score_text(query: str, *texts: str) -> int:
    q = _canonical(query)
    score = 0
    for item in texts:
        t = _canonical(item)
        if not t:
            continue
        if t in q or q in t:
            score += 6
        else:
            # 2-gram 中文词片匹配，兼顾简称与同义表达
            grams = {t[i:i + 2] for i in range(max(0, len(t) - 1)) if len(t[i:i + 2]) == 2}
            hits = sum(1 for g in grams if g in q)
            score += min(hits, 5)
    return score


def build_smartq_domain_guidance(question: str, context: dict, case=None) -> str:
    """按当前追问与范围筛选最相关的监管阈值与标准问句，交给 Agent 自主判断改写。"""
    bank = load_smartq_question_bank()
    rules = bank.get("threshold_rules") or []
    templates = bank.get("excel_templates") or []
    if not rules and not templates:
        return ""
    try:
        period = _date_label(date.fromisoformat(str(context.get("as_of", ""))))
    except ValueError:
        period = str(context.get("as_of", "")) or "本期"
    org_name = case.org_name if case is not None else "全部行社"

    scored_rules = sorted(
        ((_score_text(question, r["metric"], r["dimension_title"], *r.get("aliases", [])), idx, r)
         for idx, r in enumerate(rules)),
        key=lambda item: (-item[0], item[1]),
    )
    chosen_rules = [r for score, _, r in scored_rules if score > 0][:5]
    if not chosen_rules:
        # 开放式宽泛追问（如“有哪些高风险行社”）提供核心监管红线与高频预警线供自主选择
        core_ids = {"npl_ratio_warning", "npl_balance_top15", "provision_coverage_redline",
                    "car_redline", "liquidity_ratio_redline", "concentration_redline"}
        chosen_rules = [r for r in rules if r["id"] in core_ids][:5]

    scored_tpls = sorted(
        ((_score_text(question, t.get("letter_metric", ""), t.get("bank_metric", ""), t.get("aspect", ""), t.get("risk_point", "")), idx, t)
         for idx, t in enumerate(templates)),
        key=lambda item: (-item[0], item[1]),
    )
    chosen_tpls = [t for score, _, t in scored_tpls if score >= 2][:4]

    lines = [
        "【业务知识与监管阈值参考（供你自主判断是否补查及改写问句；用户若自定阈值则优先按用户阈值）】",
        "1. 法定监管底线与参考预警线：",
    ]
    for r in chosen_rules:
        tpl = (r["single_bank_template"] if case is not None else r["all_banks_template"]).format(
            period=period, org=org_name
        )
        drill = "、".join(r.get("drilldown_metrics", [])[:3])
        lines.append(
            f"- {r['metric']}（默认条件：{r['threshold_expr']}｜{r['basis_level']}）：示例「{tpl}」"
            + (f"；关联下钻：{drill}" if drill else "")
        )
    if chosen_tpls and case is None:
        lines.append("2. 业务 Excel 单项指标问句参考（已适配全体 82 家行社）：")
        for t in chosen_tpls:
            q_ex = t["all_banks_template"].replace("{period}", period)
            lines.append(f"- [{t['aspect']}] {q_ex}")
    text = "\n".join(lines)
    return text[:2400]


def _day(match: re.Match) -> date:
    year, month = int(match[1]), int(match[2])
    day = match[3] or match[4] or match[5] or calendar.monthrange(year, month)[1]
    return date(year, month, int(day))


def _date_label(day: date) -> str:
    suffix = "末" if day.day == calendar.monthrange(day.year, day.month)[1] else f"{day.day:02d}日"
    return f"{day.year}年{day.month:02d}月{suffix}"


def _canonical(value) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


_ORG_SUFFIX = r"(?:农村商业银行|农商银行|农商行|农村信用合作联社|农村合作银行|农信联社|农信社|信用联社|联社|行社)"


def _short_name(name):
    """识别目录简称与工商全称。地域本身是简称的一部分，不从简称中截取城市名。"""
    plain = _canonical(name)
    short = re.sub(_ORG_SUFFIX + r"(?:股份有限公司)?$", "", plain)
    if short != plain:
        return re.sub(r"^浙江省?", "", short) or plain
    return plain


def _own_codes(context, case):
    codes = {_canonical(case.org_id)}
    for org in context.get("scope_organizations", []):
        if org.get("org_id") == case.org_id and org.get("org_code"):
            codes.add(_canonical(org["org_code"]))
    return codes


def _check_scope(text, context, case):
    """排除本行社标识后检查机构目录、机构号、未入目录的显式行社名和跨机构范围词。"""
    full = re.sub(r"(?<!\d)999999(?!\d)", "全省合计", _canonical(text))
    own = _short_name(case.org_name)
    # 最长全称优先，例如“浙江杭州联合农村商业银行股份有限公司”可对应“杭州联合”。
    remainder = re.sub(r"(?:浙江省?)?" + re.escape(own) + "(?:" + _ORG_SUFFIX + r"(?:股份有限公司)?)?",
                       "", full)
    for code in _own_codes(context, case):
        remainder = remainder.replace(code, "")
    # 省级基准中的“宁波”不是在请求该市或其辖内机构。
    remaining_entities = remainder.replace(_canonical(PROVINCE), "").replace("全省合计", "")
    for org in context.get("scope_organizations", []):
        if org.get("org_id") == case.org_id or str(org.get("org_code")) == "999999":
            continue
        other = _short_name(org["org_name"])
        if other in remaining_entities or (own in other and other != own and other in full):
            raise QuickBiError("SMARTQ_SCOPE", _SCOPE_NOTE)
        for key in ("org_code", "org_id"):
            if org.get(key):
                code = re.escape(_canonical(org[key]))
                if re.search(r"(?<![a-z0-9_])" + code + r"(?![a-z0-9_]|月)", remaining_entities):
                    raise QuickBiError("SMARTQ_SCOPE", _SCOPE_NOTE)
    remainder = re.sub(r"(?:这家|本家|该家|当前|本|该)(?:银行|机构|行社)", "", remainder)
    remainder = re.sub(r"银行(?:承兑汇票|卡|间|账户|理财|存款)", "", remainder)
    if re.search(r"农商行|农商银行|商业银行|联社|农信社|信用社|行社|分行|支行以外|(?:其他|其它|别的|另一)行|"
                 r"银行|(?:各|所有|全部|其他|任意|指定)(?:家|机构)|(?:机构号|机构代码)\s*[=:为是]?\d+|"
                 r"(?<![a-z])(?:ls|test_ls)\d+", remainder):
        raise QuickBiError("SMARTQ_SCOPE", _SCOPE_NOTE)
    # 只可以用全省合计作为比较基准，单独要求全辖明细、列表或全省数值均越界。
    if re.search(r"全辖|全国|全系统(?!排名|位次)|各地市|所有地区|各地区|全省(?:各|全部|所有|前\d)", remainder) or (
        re.search(r"全省(?!排名|位次|排位)", remainder)
        and not re.search(r"相比|对比|比较|对照|基准|相差|之差|占全省|与全省.*差", remainder)
    ):
        raise QuickBiError("SMARTQ_SCOPE", _SCOPE_NOTE)


def normalize_smartq_lookup(lookup, context, case=None) -> dict:
    """校验跨进程改写，补全实际查询日期/机构，不让模型的 target_org 扩大 case 范围。"""
    if not isinstance(lookup, dict) or any(not isinstance(lookup.get(k), str) for k in _LOOKUP_FIELDS):
        raise QuickBiError("SMARTQ_QUESTION", _SAFE_NOTES["SMARTQ_QUESTION"])
    norm = {k: lookup[k].strip() for k in _LOOKUP_FIELDS}
    question = norm["rewritten_question"]
    if not 0 < len(question) <= 300 or not norm["metric_hint"] or norm["query_type"] not in _QUERY_TYPES:
        raise QuickBiError("SMARTQ_QUESTION", _SAFE_NOTES["SMARTQ_QUESTION"])
    if case is not None:
        if norm["target_org"] and _short_name(norm["target_org"]) != _short_name(case.org_name) and (
            _canonical(norm["target_org"]) not in _own_codes(context, case)
        ):
            raise QuickBiError("SMARTQ_SCOPE", _SCOPE_NOTE)
        for content in (question, context.get("original_question", ""), norm["metric_hint"]):
            _check_scope(content, context, case)
        norm["target_org"] = case.org_name
    else:
        raw_target = norm["target_org"]
        if raw_target in {"全部行社", "全省各行社", "全部82家行社"}:
            norm["target_org"] = raw_target
        else:
            norm["target_org"] = _PROVINCE_NAME.sub(PROVINCE, raw_target or PROVINCE)
    if norm["target_org"] and norm["target_org"] not in question and not (
        norm["target_org"] == PROVINCE and "全省" in question
    ) and not (
        norm["target_org"] in {"全部行社", "全省各行社", "全部82家行社"}
        and re.search(r"全部行社|各行社|全省|全辖|行社是哪几家|行社有哪些", question)
    ):
        question = f"{norm['target_org']}，{question}"
    question = _PROVINCE_NAME.sub(PROVINCE, re.sub(r"(?<!\d)999999(?!\d)", "全省合计", question))
    try:
        dates = [_day(match) for match in _DATE.finditer(question)]
        specified = _DATE.fullmatch(norm["target_date"])
        target = _day(specified) if specified else dates[0] if dates else date.fromisoformat(context["as_of"])
        if dates and target not in dates:
            raise ValueError("date mismatch")
        question = _DATE.sub(lambda match: _date_label(_day(match)), question) if dates else f"{_date_label(target)}，{question}"
    except (KeyError, ValueError, TypeError):
        raise QuickBiError("SMARTQ_QUESTION", _SAFE_NOTES["SMARTQ_QUESTION"]) from None
    if len(question) > 300:
        raise QuickBiError("SMARTQ_QUESTION", _SAFE_NOTES["SMARTQ_QUESTION"])
    return {**norm, "target_date": target.isoformat(), "rewritten_question": question}


_ORG_NAMES = {"机构名称", "机构名", "机构全称", "机构简称", "所属机构", "法人机构名称", "行社名称",
              "行社简称", "行社", "机构", "orgname", "organizationname"}
_ORG_CODES = {"机构号", "机构编号", "机构代码", "机构编码", "法人机构号", "行社代码",
              "orgcode", "orgid", "organizationcode", "organizationid"}


def _evidence(result, lookup, cube, context, case):
    fields = result["fields"]
    org_cols = {f: _canonical(f).replace("_", "").split("(", 1)[0] for f in fields}
    org_cols = {f: k for f, k in org_cols.items() if k in _ORG_NAMES | _ORG_CODES}
    metadata_fields = {"数据日期", "指标名称", "指标单位", "指标", "单位", "日期"}
    value_fields = [f for f in fields if f not in org_cols and f not in metadata_fields]
    filtered = total = 0
    rows, seen = [], set()
    if case is not None:
        own_codes = _own_codes(context, case)
        province = PROVINCE in lookup["rewritten_question"]
    for record in result["records"]:
        if case is not None and org_cols:
            # 当名称与代码同时出现时须同时匹配同一身份，不能因其中一列匹配而放行。
            own = all((_short_name(record[f]) == _short_name(case.org_name) if k in _ORG_NAMES
                       else _canonical(record[f]) in own_codes) for f, k in org_cols.items())
            benchmark = province and all(_canonical(record[f]) in (
                {"全省合计", _canonical(PROVINCE)} if k in _ORG_NAMES else {"999999"}) for f, k in org_cols.items())
            if not (own or benchmark):
                filtered += 1
                continue
        if not any(record[f] is not None and str(record[f]).strip() for f in value_fields):
            continue
        total += 1
        if len(rows) >= 20:
            continue
        payload = json.dumps([cube, lookup["target_org"], lookup["target_date"], lookup["rewritten_question"], record],
                             ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        key = "smartq:" + hashlib.sha256(payload.encode()).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        values = "，".join(f"{f}={record[f] if record[f] is not None else '缺失'}" for f in fields)
        body = f"{lookup['target_date']}，查询范围：{lookup['target_org']}。{values}"
        if len(body) > 2950:
            body = body[:2950] + "…（记录节选）"
        rows.append({"id": key, "source": "smartq", "label": ("小Q补查 · " + lookup["metric_hint"])[:300],
                     "text": f"{body}。{_REFERENCE}。"})
    return rows, {"record_count": total, "filtered_row_count": filtered, "evidence_count": len(rows),
                  "scope_filter_applied": bool(case is not None and org_cols)}


def execute_smartq_supplement(lookup, context, case=None, previous=None, *, client_factory=None):
    """运行一次只读 SSE 查询。previous 由宿主按 run/case 选取；仅同 cube 的完整指针可续接。"""
    meta = {k: lookup.get(k, "") for k in _LOOKUP_FIELDS}
    meta.update(cube_id=None, chat_id=None, message_id=None, chart_type=None, record_count=0,
                evidence_count=0, filtered_row_count=0, scope_filter_applied=False, trailing_error=None)
    cfg = smartq_followup_config(meta["metric_hint"])
    meta["cube_id"] = cfg["cube_id"] or None
    if not cfg["enabled"]:
        return [], {**meta, "status": "unavailable", "error_code": "QUICKBI_CONFIG", "note": cfg["note"]}
    try:
        # 本地扫描可能使用 TEST_LS 编号；挂载的正式机构目录按行社名称连接真实机构号。
        org_path = os.getenv("RISK_QUICKBI_ORG_FILE")
        if case is not None and org_path:
            directory = list(_org_directory(org_path).values())
            matches = [org for org in directory if org["level"] == "03"
                       and _short_name(org["org_name"]) == _short_name(case.org_name)]
            if len(matches) > 1:
                raise QuickBiError("SMARTQ_SCOPE", _SCOPE_NOTE)
            members = list(context.get("scope_organizations", []))
            for org in directory:
                members.append({**org, "org_code": org["org_id"], "org_id": case.org_id if org in matches else org["org_id"]})
            context = {**context, "scope_organizations": members}
        norm = normalize_smartq_lookup(lookup, context, case)
        meta.update(norm)
        pointer = {}
        if (previous and previous.get("status") == "ok" and previous.get("cube_id") == cfg["cube_id"]
                and previous.get("chat_id") and previous.get("message_id")):
            pointer = {"chatId": previous["chat_id"], "appendedMessageId": previous["message_id"]}
        result = (client_factory or SmartQStreamClient)().query(
            userQuestion=norm["rewritten_question"], cubeId=cfg["cube_id"], userId=cfg["user_id"],
            llmNameForInference=cfg["inference_llm"], llmNameForData=cfg["data_llm"], **pointer)
        rows, counts = _evidence(result, norm, cfg["cube_id"], context, case)
    except QuickBiError as exc:
        code = exc.code if exc.code in _SAFE_NOTES else "QUICKBI_REQUEST"
        return [], {**meta, "status": "error", "error_code": code, "note": _SAFE_NOTES[code]}
    except (OSError, HTTPException):
        return [], {**meta, "status": "error", "error_code": "QUICKBI_CONNECTION", "note": _SAFE_NOTES["QUICKBI_CONNECTION"]}
    return rows, {**meta, **counts, "status": "ok" if rows else "empty",
                  "chat_id": result["chatId"], "message_id": result.get("messageId"),
                  "chart_type": result.get("chartType"), "trailing_error": result["diagnostics"].get("trailing_error"),
                  "note": "小 Q 实时补查，口径仅供参考。" if rows else "小 Q 未返回当前范围内的有效记录。"}

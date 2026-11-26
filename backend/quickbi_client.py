"""QuickBI 的只读 HTTP 客户端。身份与签名密钥仅保存在当前调用方的内存中。"""
from __future__ import annotations

import base64
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
import hashlib
import hmac
import json
import math
import os
from http.client import HTTPException
from http.cookies import CookieError, SimpleCookie
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4

from .services import ServiceError


class QuickBiError(ServiceError):
    """向上层传递稳定的错误码与可展示说明，不回显 URL、头或服务端原文。"""


_ERRORS = {
    "AE0510150001": "QuickBI 报表 reportId 不存在，请核对 QB_REPORT_ID 配置。",
    "NOX5300008": "QuickBI 明细查询（queryDetail=true）模式下字段不能携带聚合函数。",
    "NOX5200010": "QuickBI 查询字段无效，请核对字段布局。",
    "AUTHX_01002": "QuickBI 登录态已失效或过期，请重新登录或换取票据。",
    "OE10010101": "QuickBI 签名时间超出允许的 3 分钟偏差，请校准本机时钟。",
    "OE10010106": "QuickBI 接口未授权，请核对接口路径与应用授权。",
    "AE0130010003": "QuickBI 报表未开通三方嵌入，请在开放平台的嵌入分析中启用。",
}


DEFAULT_COMPONENT_ID = "00000000-0000-0000-0000-000000000001"
COMPONENT_TYPE_DETAIL = "73"


def build_olap_param(*, cube_id: str, fields: Sequence[Mapping],
                     conditions: Sequence[Mapping] = (), report_id: str = "",
                     component_id: str = DEFAULT_COMPONENT_ID, component_name: str = "probe",
                     limit: int = 5000, offset: int = 0, query_detail: bool = False) -> dict:
    """组装字段、聚合前过滤、分页及查询配置，保留调用方传入的字段属性。"""
    if not isinstance(cube_id, str) or not cube_id.strip() or not fields:
        raise QuickBiError("QUICKBI_QUERY", "QuickBI 查询需要数据集 ID 和至少一个字段。")
    columns = []
    for field in fields:
        fid = field.get("fid") if isinstance(field, Mapping) else None
        if not isinstance(fid, str) or not fid.strip():
            raise QuickBiError("QUICKBI_QUERY", "QuickBI 查询字段需要非空 fid。")
        area = field.get("areaType", "column" if field.get("aggregate") else "row")
        if area not in ("row", "column"):
            raise QuickBiError("QUICKBI_QUERY", "QuickBI 字段 areaType 必须为 row 或 column。")
        if query_detail and "aggregate" in field:
            raise QuickBiError("NOX5300008", _ERRORS["NOX5300008"])
        column = {"guid": fid, "areaType": area, "geographicInfoModel": 0, **field}
        if area == "row":
            column.pop("aggregate", None)
        elif not query_detail:
            column.setdefault("aggregate", "sum")
        columns.append(column)
    normalized_conditions = []
    for condition in conditions:
        if not isinstance(condition, Mapping):
            raise QuickBiError("QUICKBI_QUERY", "QuickBI 查询条件必须为对象。")
        if condition.get("functionalOperator") == "in":
            args = condition.get("args")
            if not isinstance(args, (list, tuple)) or len(args) != 1:
                raise QuickBiError("QUICKBI_QUERY", "QuickBI beforeAggregateCondition 的 in 条件仅支持单值，传多值数组会静默返回 0 行。")
        normalized_conditions.append(dict(condition))
    configs = [
        ("field", {"fields": columns}),
        ("beforeAggregateCondition", {"logicalOperator": "AND", "conditions": normalized_conditions}),
        ("paging", {"limit": limit, "offset": offset, "pagedByAllDim": True}),
        ("queryConfig", {"needCount": False, "queryCount": False, "queryDetail": query_detail}),
    ]
    return {"componentId": component_id or DEFAULT_COMPONENT_ID, "componentName": component_name, "dataType": "general",
            "reportId": report_id, "configs": [{"type": kind, "cubeId": cube_id, "config": config}
                                              for kind, config in configs]}


def sign_openapi_request(method: str, uri: str, query: Mapping | None, access_id: str,
                         access_secret: str, *, nonce: str | None = None,
                         timestamp: str | None = None) -> dict[str, str]:
    """生成 v2 网关四个请求头。query 使用编码前的参数值，body 不参与签名。"""
    if not access_id or not access_secret:
        raise QuickBiError("QUICKBI_CONFIG", "请配置 QB_ACCESS_ID 和 QB_ACCESS_SECRET。")
    headers = {"X-Gw-AccessId": access_id, "X-Gw-Nonce": nonce or str(uuid4()),
               "X-Gw-Timestamp": str(timestamp or time.time_ns() // 1_000_000)}
    lines = [method.upper(), uri]
    params = sorted((k, v) for k, v in (query or {}).items() if v is not None and v != "")
    if params:
        lines.append("&".join(f"{k}={v}" for k, v in params))
    lines.extend(f"{key}:{headers[key]}" for key in sorted(headers))
    encoded = quote("\n".join(lines), safe="-_.~").encode("ascii")
    digest = hmac.digest(access_secret.encode("utf-8"), encoded, hashlib.sha256)
    headers["X-Gw-Signature"] = base64.b64encode(digest).decode("ascii")
    return headers


class _NoRedirect(HTTPRedirectHandler):
    # 登录页和跳转目标不属于取数接口，禁止携凭据跟随跳转。
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _read_json(response) -> dict:
    try:
        # 小数按原始十进制文本交给指标适配器，不经过 float。
        value = json.load(response, parse_float=str)
    except (ValueError, UnicodeError):
        raise QuickBiError("QUICKBI_RESPONSE", "QuickBI 返回了无效的 JSON 响应。") from None
    if not isinstance(value, dict):
        raise QuickBiError("QUICKBI_RESPONSE", "QuickBI 响应缺少结果对象。")
    return value


def _check_error(payload: dict, status: int = 200):
    codes = (payload.get("errorCode"), payload.get("code"))
    for code in codes:
        if isinstance(code, str) and code in _ERRORS:
            raise QuickBiError(code, _ERRORS[code]) from None
    if status == 401:
        raise QuickBiError("AUTHX_01002", _ERRORS["AUTHX_01002"]) from None
    if status >= 300 or payload.get("success") is False:
        raise QuickBiError("QUICKBI_REQUEST", f"QuickBI 拒绝请求（HTTP {status}），请核对身份与接口配置。") from None


def _http_origin(url: str) -> tuple:
    """同源比较使用实际端口，拒绝 URL 内嵌凭据和含歧义的地址。"""
    if not isinstance(url, str) or "\\" in url or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValueError
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
        raise ValueError
    return parts.scheme, parts.hostname, parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)


def _validate_origin(origin: str) -> str:
    try:
        _http_origin(origin)
        parts = urlsplit(origin)
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError
    except ValueError:
        raise QuickBiError("QUICKBI_CONFIG", "QB_HOST 必须是含协议的 QuickBI 服务源地址。") from None
    return origin.rstrip("/")


def create_smartq_ticket(works_id: str, *, svc_url: str | None = None, timeout: int = 30) -> dict:
    """向配置的出票服务提交目标 worksId，此请求独立于门户会话及 OpenAPI 签名。"""
    if not isinstance(works_id, str) or not works_id.strip():
        raise QuickBiError("QUICKBI_CONFIG", "出票需要非空 worksId。")
    base = svc_url or os.getenv("SMARTQ_SVC_URL") or os.getenv("QB_SMARTQ_TICKET_URL") or ""
    try:
        _http_origin(base)
        parts = urlsplit(base)
        if parts.query or parts.fragment:
            raise ValueError
    except ValueError:
        raise QuickBiError("QUICKBI_CONFIG", "SMARTQ_SVC_URL 必须是有效的 HTTP 或 HTTPS 出票服务地址。") from None
    path = "/api/report/createTicket"
    endpoint = base.rstrip("/")
    if not endpoint.endswith(path):
        endpoint += path
    try:
        with build_opener(_NoRedirect()).open(endpoint + "?" + urlencode({"worksId": works_id}), timeout=timeout) as response:
            payload = _read_json(response)
        _check_error(payload)
    except QuickBiError:
        raise
    except HTTPError as error:
        error.close()
        raise QuickBiError("QUICKBI_TICKET", "SmartQ 出票服务拒绝请求。") from None
    except (OSError, HTTPException, ValueError):
        raise QuickBiError("QUICKBI_TICKET", "SmartQ 出票请求失败，请检查服务配置和内网连接。") from None
    data = payload.get("data", payload)
    ticket = data.get("ticket") if isinstance(data, dict) else data
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(ticket, str) or not ticket.strip() or (url is not None and not isinstance(url, str)):
        raise QuickBiError("QUICKBI_TICKET", "SmartQ 出票响应缺少有效票据或免登地址。")
    return {"ticket": ticket, "url": url or ""}


def build_ticket_url(origin: str, works_id: str, ticket: str, *, view_type: str = "dashboard",
                     locale: str = "zh-CN") -> str:
    params = urlencode({"pageId": works_id, "accessTicket": ticket, "locale": locale})
    return f"{_validate_origin(origin)}/token3rd/{quote(view_type, safe='')}/view/pc.htm?{params}"


def open_ticket_session_http(origin: str, works_id: str, ticket: str, *, login_url: str | None = None,
                             max_hops: int = 10, timeout: int = 30, ttl_seconds: float = 300.0) -> dict:
    """逐跳接收免登 Cookie，在发送任何请求头前核对初始地址及后续跳转的源。"""
    origin = _validate_origin(origin)
    trusted = _http_origin(origin)
    cookies = SimpleCookie()
    opener = build_opener(_NoRedirect())
    try:
        url = urljoin(origin + "/", login_url) if login_url else build_ticket_url(origin, works_id, ticket)
        for _ in range(max_hops + 1):
            if _http_origin(url) != trusted:
                raise QuickBiError("QUICKBI_SESSION", "QuickBI 免登地址或重定向目标与配置的源不一致。")
            cookie_header = "; ".join(f"{key}={m.coded_value}" for key, m in cookies.items())
            request = Request(url, headers={"Cookie": cookie_header} if cookies else {})
            try:
                response = opener.open(request, timeout=timeout)
            except HTTPError as error:
                response = error
            with response:
                for header in response.headers.get_all("Set-Cookie", []):
                    parsed = SimpleCookie()
                    parsed.load(header)
                    for key, morsel in parsed.items():
                        if not morsel.value or morsel["max-age"].strip() == "0":
                            cookies.pop(key, None)
                        else:
                            cookies[key] = morsel
                if 200 <= response.status < 300:
                    break
                location = response.headers.get("Location")
                if response.status not in (301, 302, 303, 307, 308) or not location:
                    raise QuickBiError("QUICKBI_SESSION", "QuickBI 免登请求失败或跳转缺少目标地址。")
                url = urljoin(url, location)
        else:
            raise QuickBiError("QUICKBI_SESSION", "QuickBI 免登重定向次数超过上限。")
    except QuickBiError:
        raise
    except (OSError, ValueError, HTTPException, CookieError):
        raise QuickBiError("QUICKBI_SESSION", "QuickBI 免登地址、响应或内网连接异常。") from None
    tokens = {key.lower(): morsel.value for key, morsel in cookies.items()}
    csrf = tokens.get("csrf_token") or tokens.get("x-csrf-token")
    if not cookies or not csrf:
        raise QuickBiError("QUICKBI_SESSION", "QuickBI 免登响应缺少会话 Cookie 或 CSRF 令牌。")
    return {"cookie": "; ".join(f"{k}={m.coded_value}" for k, m in cookies.items()), "csrf": csrf,
            "ticket": ticket, "login_kind": "smartq_ticket", "source": "ticket-http",
            "expires_at": time.monotonic() + ttl_seconds}


class QuickBiClient:
    """每个客户端只在内存中保存当前身份的会话。"""

    def __init__(self, base_url: str | None = None, *,
                 session_provider: Callable[[], Mapping] | None = None,
                 session_exchange: Callable[[str, str], Mapping] | None = None):
        self.provider = os.getenv("IDENTITY_PROVIDER", "cookie")
        if self.provider == "dataService":
            self.data_service_sid = os.getenv("DATA_SERVICE_SID")
            raise QuickBiError("QUICKBI_DATA_SERVICE", "数据服务适配器尚未发布/启用。")
        if self.provider not in {"cookie", "ticket", "smartq_ticket"}:
            raise QuickBiError("QUICKBI_CONFIG", "IDENTITY_PROVIDER 应为 cookie、ticket、smartq_ticket 或 dataService。")
        self.origin = _validate_origin(base_url or os.getenv("QB_HOST") or os.getenv("QB_BASE_URL") or "")
        try:
            self._ttl = float(os.getenv("QB_SESSION_TTL_SECONDS", "300"))
            if not math.isfinite(self._ttl) or self._ttl < 0:
                raise ValueError
        except ValueError:
            raise QuickBiError("QUICKBI_CONFIG", "QB_SESSION_TTL_SECONDS 应为非负的有限秒数。") from None
        self.session_provider = session_provider
        self.session_exchange = session_exchange
        self._works_id = None
        self._session: dict | None = None
        self._opener = build_opener(_NoRedirect())

    def _request(self, method, path, *, query=None, body=None, signed=False, headers=None):
        params = dict(query or {})
        request_headers = dict(headers or {})
        if signed:
            params = {k: v for k, v in params.items() if v is not None and v != ""}
            request_headers.update(sign_openapi_request(method, path, params,
                                   os.getenv("QB_ACCESS_ID", ""), os.getenv("QB_ACCESS_SECRET", "")))
        url = self.origin + path + ("?" + urlencode(params) if params else "")
        data = None
        if isinstance(body, bytes):
            data = body
        elif body is not None:
            request_headers["Content-Type"] = "application/json;charset=utf-8"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            req = Request(url, data=data, headers=request_headers, method=method)
            return self._opener.open(req, timeout=120)
        except HTTPError as error:
            with error:
                try:
                    payload = _read_json(error)
                except QuickBiError:
                    payload = {}
                _check_error(payload, error.code)
        except (URLError, OSError, HTTPException):
            raise QuickBiError("QUICKBI_CONNECTION", "QuickBI 网络连接失败或超时，请检查内网连接。") from None
        except ValueError:
            # urllib 的原始报错可能回显包含换行或无法编码的凭据。
            raise QuickBiError("QUICKBI_CONFIG", "QuickBI 请求头或服务地址格式无效。") from None

    def _json(self, method, path, **kwargs):
        with self._request(method, path, **kwargs) as response:
            try:
                payload = _read_json(response)
            except (OSError, HTTPException):
                raise QuickBiError("QUICKBI_CONNECTION", "读取 QuickBI 响应时连接中断。") from None
        _check_error(payload)
        return payload

    def dataset_info(self, cube_id: str) -> dict:
        return self._json("GET", "/openapi/v2/dataset/infoNew", signed=True,
                          query={"datasetId": cube_id}).get("data")

    def create_access_ticket(self, *, worksId: str, accountName=None, accountType=None, userId=None,
                             expireTime=None, ticketNum=None, cmptId=None, globalParam=None) -> str:
        if not worksId or (accountName and accountType in ("", None)):
            raise QuickBiError("QUICKBI_CONFIG", "出票需要 worksId，指定 accountName 时必须同时传 accountType。")
        if isinstance(globalParam, dict):
            globalParam = json.dumps(globalParam, ensure_ascii=False, separators=(",", ":"))
        params = dict(worksId=worksId, accountName=accountName, accountType=accountType, userId=userId,
                      expireTime=expireTime, ticketNum=ticketNum, cmptId=cmptId, globalParam=globalParam)
        data = self._json("POST", "/openapi/v2/embed/ticket/create", query=params, signed=True).get("data")
        ticket = data.get("ticket") if isinstance(data, dict) else data
        if not isinstance(ticket, str) or not ticket:
            raise QuickBiError("QUICKBI_TICKET", "QuickBI 出票响应缺少票据。")
        return ticket

    def login_url(self, works_id: str, ticket: str) -> str:
        return build_ticket_url(self.origin, works_id, ticket)

    def clear_session_cache(self) -> None:
        self._session = None
        self._works_id = None

    def login(self, *, works_id: str | None = None, force: bool = False) -> dict:
        """返回当前报表的内存会话。切换目标、到期或强制刷新时重新获取身份。"""
        works = works_id or self._works_id or os.getenv("EMBED_WORKS_ID") or os.getenv("QB_WORKS_ID") or os.getenv("QB_REPORT_ID", "")
        cached = self._session
        if not force and cached and works == self._works_id and time.monotonic() < cached["expires_at"]:
            return cached.copy()
        if self.provider == "smartq_ticket":
            issued = create_smartq_ticket(works)
            session = open_ticket_session_http(self.origin, works, issued["ticket"],
                                               login_url=issued["url"], ttl_seconds=self._ttl)
        else:
            ticket = os.getenv("QB_ACCESS_TICKET") if self.provider == "ticket" else None
            if self.provider == "ticket" and self.session_exchange:
                if not works:
                    raise QuickBiError("QUICKBI_CONFIG", "票据换取会话需要 EMBED_WORKS_ID。")
                if force or cached is not None or not ticket:
                    ticket = self.create_access_ticket(
                        worksId=works, accountName=os.getenv("EMBED_ACCOUNT_NAME") or os.getenv("QB_ACCOUNT_NAME"),
                        accountType=os.getenv("EMBED_ACCOUNT_TYPE") or os.getenv("QB_ACCOUNT_TYPE"),
                        userId=os.getenv("QB_USER_ID"))
                headers = self._runtime_session(self.session_exchange, ticket, self.login_url(works, ticket))
                source = "session_exchange"
            else:
                if self.provider == "ticket" and not ticket:
                    raise QuickBiError("QUICKBI_CONFIG", "ticket 身份需要 session_exchange 或已换取会话的 QB_ACCESS_TICKET 与 Cookie、CSRF。")
                headers = self._runtime_session(self.session_provider) if self.session_provider else {
                    "Cookie": os.getenv("QB_COOKIE"), "X-Csrf-Token": os.getenv("QB_CSRF_TOKEN")}
                source = "session_provider" if self.session_provider else "environment"
            if not isinstance(headers, Mapping) or not all(
                isinstance(headers.get(key), str) and headers[key].strip() for key in ("Cookie", "X-Csrf-Token")
            ):
                raise QuickBiError("QUICKBI_CONFIG", "QuickBI 会话缺少 Cookie 或 X-Csrf-Token。")
            session = {"cookie": headers["Cookie"], "csrf": headers["X-Csrf-Token"], "ticket": ticket,
                       "login_kind": self.provider, "source": source, "expires_at": time.monotonic() + self._ttl}
        self._session, self._works_id = session, works
        return session.copy()

    get_session = login

    @staticmethod
    def _runtime_session(callback, *args):
        # 门户回调异常可能带完整票据 URL；业务轨迹只保留失败环节。
        try:
            return callback(*args)
        except Exception:
            raise QuickBiError("QUICKBI_SESSION", "运行时身份提供方未能建立 QuickBI 会话。") from None

    def _session_json(self, method, path, *, query=None, body=None, headers=None):
        session = self.login()

        def request(active):
            params = dict(query or {})
            if active["ticket"]:
                params["accessTicket"] = active["ticket"]
            request_headers = {**(headers or {}), "Cookie": active["cookie"], "X-Csrf-Token": active["csrf"],
                               "X-Requested-With": "XMLHttpRequest"}
            return self._json(method, path, body=body, headers=request_headers, query=params)

        try:
            return request(session)
        except QuickBiError as error:
            renewable = self.provider == "smartq_ticket" or (self.provider == "ticket" and self.session_exchange)
            if error.code != "AUTHX_01002" or not renewable:
                raise
        works = self._works_id
        self.clear_session_cache()
        return request(self.login(works_id=works, force=True))

    def whoami(self) -> dict:
        identity = self._session_json("GET", "/api/v2/user/currentUserInfo").get("data")
        if not isinstance(identity, dict):
            raise QuickBiError("QUICKBI_RESPONSE", "QuickBI 身份响应缺少用户信息对象。")
        return identity

    def check_alive(self) -> bool:
        self.whoami()
        return True

    def olap_query(self, olap_param: dict, *, report_id: str | None = None,
                   component_id: str | None = None, component_type: str = COMPONENT_TYPE_DETAIL) -> dict:
        """将查询参数编码为门户表单，并保持看板来源、URL 与表单身份一致。"""
        param = dict(olap_param)
        report = report_id or param.get("reportId") or os.getenv("QB_REPORT_ID", "")
        component = component_id or param.get("componentId") or os.getenv("QB_COMPONENT_ID") or DEFAULT_COMPONENT_ID
        if "configs" not in param:
            conditions = param.get("conditions")
            if conditions is None:
                conditions = []
                for item in param.get("filters", []):
                    if item.get("type") == "beforeAggregateCondition":
                        conditions.extend(item["config"]["conditions"])
                    else:
                        conditions.append(item)
            param = build_olap_param(cube_id=param.get("cubeId", param.get("cube_id")),
                                     fields=param.get("fields", []), conditions=conditions,
                                     component_name=param.get("componentName", "probe"),
                                     limit=param.get("limit", 5000), offset=param.get("offset", 0),
                                     query_detail=param.get("queryDetail", False))
        param.update(reportId=report, componentId=component)
        query = {"componentId": component, "reportId": report, "componentType": component_type}
        referer = self.origin + "/dashboard/view/pc.htm?" + urlencode({"pageId": report})
        headers = {"Referer": referer, "X-GW-Referer": referer, "Origin": self.origin,
                   "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"}
        form = urlencode({"olapQueryParam": json.dumps(param, ensure_ascii=False, separators=(",", ":"))})
        result = self._session_json("POST", "/api/v2/olap/query", query=query, body=form.encode("ascii"),
                                    headers=headers)
        if isinstance(result.get("data"), dict):
            return result["data"]
        if isinstance(result.get("data"), list) or any(key in result for key in ("rows", "values", "columns")):
            return result
        raise QuickBiError("QUICKBI_QUERY", "QuickBI 查询响应缺少结果数据。")

    def query_olap(self, *, cube_id, fields, conditions=(), limit=5000, offset=0, report_id=None,
                     component_id=None, component_type=COMPONENT_TYPE_DETAIL, query_detail=False) -> dict:
        """按数据集字段及过滤条件查询。"""
        return self.olap_query({"cubeId": cube_id, "fields": fields, "conditions": conditions,
                                "limit": limit, "offset": offset, "queryDetail": query_detail},
                               report_id=report_id, component_id=component_id, component_type=component_type)


def _sse_frames(response):
    """按 SSE 空行分帧。HTTP 流行迭代处理跨网络分块的 UTF-8、CRLF 及多行 data。"""
    lines = []
    for raw in response:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if lines:
                yield json.loads("\n".join(lines))
                lines.clear()
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
    if lines:
        yield json.loads("\n".join(lines))


def _olap_result(data):
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict) or not isinstance(data.get("metaType"), list) or not isinstance(data.get("values"), list):
        raise ValueError("olapResult")
    fields = [field["k"] for field in data["metaType"]]
    if not fields or not all(isinstance(key, str) and key for key in fields) or len(set(fields)) != len(fields):
        raise ValueError("metaType")
    records = []
    for value in data["values"]:
        row = value["row"]
        if not isinstance(row, list) or len(row) != len(fields):
            raise ValueError("row")
        records.append(dict(zip(fields, row)))
    return {"chartType": data.get("chartType"), "fields": fields, "records": records}


class SmartQStreamClient:
    """仅用于探索、无字典指标补查及口径参考。生产主取数始终走 olap/query。"""

    def __init__(self, client: QuickBiClient | None = None):
        self.client = client or QuickBiClient()

    def query(self, *, userId: str, userQuestion: str, cubeId: str, chatId: str | None = None,
              appendedMessageId: str | None = None, llmNameForInference: str = "nv1",
              llmNameForData: str = "nv1") -> dict:
        if not all((userId, userQuestion, cubeId)):
            raise QuickBiError("QUICKBI_CONFIG", "小 Q 问数需要 userId、userQuestion 和 cubeId。")
        body = dict(userId=userId, userQuestion=userQuestion, cubeId=cubeId, chatId=chatId or str(uuid4()),
                    llmNameForInference=llmNameForInference, llmNameForData=llmNameForData)
        if appendedMessageId:
            body["appendedMessageId"] = appendedMessageId
        counts = Counter()
        result = message_id = trailing_error = None
        path = "/openapi/v2/smartq/queryByQuestionStream"
        headers = {"Accept": "text/event-stream", "Origin": self.client.origin}
        with self.client._request("POST", path, signed=True, body=body, headers=headers) as response:
            if response.headers.get_content_type() != "text/event-stream":
                _check_error(_read_json(response))
                raise QuickBiError("QUICKBI_SSE", "小 Q 未返回 SSE 流。")
            try:
                for frame in _sse_frames(response):
                    kind = frame["type"]
                    counts[kind] += 1
                    if kind == "olapResult":
                        try:
                            result = _olap_result(frame.get("data"))
                        except (TypeError, ValueError, KeyError):
                            counts["invalid_olapResult"] += 1
                    elif kind == "message":
                        message_id = frame.get("data")
                    elif kind in {"error", "errorInfo"} and result is not None:
                        # 仅保留事件种类。服务端错误文本有时带查询细节或凭据。
                        trailing_error = kind
                    elif kind == "finish":
                        break
            except (OSError, HTTPException, ValueError, TypeError, KeyError):
                if result is None:
                    raise QuickBiError("QUICKBI_SSE", "小 Q 流中断或帧无效，未收到有效 olapResult。") from None
                trailing_error = trailing_error or "stream_interrupted"
        if result is None:
            raise QuickBiError("QUICKBI_SSE", "小 Q 未返回有效 olapResult。")
        return {**result, "messageId": message_id, "chatId": body["chatId"],
                "diagnostics": {"events": dict(counts), "trailing_error": trailing_error}}

"""本地 HTTP/SSE 协议样例。测试使用独立生成的签名向量及合成身份，不接触外网。"""
import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from threading import Thread
import time
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit


@contextmanager
def protocol_endpoint(reply):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            requests.append((self.command, url.path, parse_qs(url.query, keep_blank_values=True), self.headers, data))
            status, mime, body, *extra = reply(url.path)
            self.send_response(status)
            self.send_header("Content-Type", mime)
            for name, value in (extra[0] if extra else []):
                self.send_header(name, value)
            self.end_headers()
            for chunk in body:
                self.wfile.write(chunk)
                self.wfile.flush()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        worker.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        env = {"QB_HOST": origin, "QB_ACCESS_ID": "sample-id", "QB_ACCESS_SECRET": "sample-secret",
               "IDENTITY_PROVIDER": "cookie", "QB_COOKIE": "example=value", "QB_CSRF_TOKEN": "csrf",
               "QB_REPORT_ID": "sample-report", "QB_COMPONENT_ID": "", "QB_SESSION_TTL_SECONDS": "300"}
        try:
            with patch.dict(os.environ, env):
                yield requests, origin
        finally:
            server.shutdown()
            worker.join()


def json_reply(data, code=200):
    return code, "application/json", [json.dumps(data).encode()]


def olap_form(request, origin, *, cube, cookie, csrf, ticket=None, report="sample-report",
              component="00000000-0000-0000-0000-000000000001"):
    """从实际请求解码表单，并校验网关路由、身份及内网四段配置。"""
    method, path, query, headers, body = request
    assert (method, path) == ("POST", "/api/v2/olap/query")
    expected = {"componentId": [component], "reportId": [report], "componentType": ["73"]}
    if ticket is not None:
        expected["accessTicket"] = [ticket]
    assert query == expected
    assert headers["Content-Type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert headers["Origin"] == origin
    referer = f"{origin}/dashboard/view/pc.htm?pageId={report}"
    assert headers["Referer"] == headers["X-GW-Referer"] == referer
    assert dict(item.strip().split("=", 1) for item in headers["Cookie"].split(";")) == cookie
    assert headers["X-Csrf-Token"] == csrf and headers["X-Requested-With"] == "XMLHttpRequest"
    form = parse_qs(body.decode("ascii"), strict_parsing=True)
    assert set(form) == {"olapQueryParam"} and len(form["olapQueryParam"]) == 1
    param = json.loads(form["olapQueryParam"][0])
    assert body == urlencode({"olapQueryParam": json.dumps(param, ensure_ascii=False, separators=(",", ":"))}).encode()
    assert param["componentId"] == component and param["reportId"] == report and param["dataType"] == "general"
    configs = param["configs"]
    assert [c["type"] for c in configs] == ["field", "beforeAggregateCondition", "paging", "queryConfig"]
    assert all(c["cubeId"] == cube for c in configs)
    fields, filters, paging, settings = [c["config"] for c in configs]
    for field in fields["fields"]:
        assert field["guid"] == field["fid"] and field["geographicInfoModel"] == 0
        assert field["areaType"] in ("row", "column")
        assert ("aggregate" in field) == (field["areaType"] == "column")
    assert filters["logicalOperator"] == "AND"
    assert paging["pagedByAllDim"] is True
    assert settings == {"needCount": False, "queryCount": False, "queryDetail": False}
    return fields["fields"], filters["conditions"], paging


def stream_reply(*events):
    frames = [":keepalive\r\n\r\n"]
    for kind, data in events:
        frame = json.dumps({"type": kind, "data": data}, ensure_ascii=False)
        frames.append("data: " + frame + "\r\n\r\n")
    wire = "".join(frames).encode()
    return 200, "text/event-stream;charset=utf-8", [wire[i:i+7] for i in range(0, len(wire), 7)]


class TestQuickBiProtocol(TestCase):
    def test_v2_signature_vectors(self):
        from backend.quickbi_client import sign_openapi_request
        fixed = {"nonce": "123e4567-e89b-12d3-a456-426614174000", "timestamp": "1700000000000"}
        for query, expected in [
            ({}, "PbyJ28ss04/a0LoT88sBrpn9wrTKn03XYUw/TtBkJkw="),
            ({"z": 0, "b": "", "a": "杭州 !'()*~", "c": None}, "fsihsFB7HfmaZA4BkG+W/NZ0aH/fQjsWcoJJrfHHv54="),
        ]:
            signed = sign_openapi_request("post", "/openapi/v2/example", query, "sample-id", "sample-secret", **fixed)
            self.assertEqual(signed["X-Gw-Signature"], expected)
            self.assertEqual(set(signed), {"X-Gw-AccessId", "X-Gw-Signature", "X-Gw-Timestamp", "X-Gw-Nonce"})

    def test_ticket_exchange_uses_only_bound_session(self):
        from backend.quickbi_client import QuickBiClient, QuickBiError

        def reply(path):
            if path.endswith("/create"):
                return json_reply({"success": True, "data": {"ticket": "synthetic+/="}})
            return json_reply({"success": True, "data": {"accountName": "branch"}})

        with protocol_endpoint(reply) as (requests, origin), patch.dict(os.environ, {
                "IDENTITY_PROVIDER": "ticket", "EMBED_WORKS_ID": "works", "EMBED_ACCOUNT_NAME": "branch",
                "EMBED_ACCOUNT_TYPE": "3", "QB_ACCESS_TICKET": "", "QB_COOKIE": "owner=unused"}):
            def exchange(ticket, url):
                self.assertEqual(ticket, "synthetic+/=")
                target = urlsplit(url)
                self.assertTrue(url.startswith(origin + "/token3rd/dashboard/view/pc.htm?"))
                self.assertEqual(parse_qs(target.query), {"accessTicket": [ticket], "pageId": ["works"], "locale": ["zh-CN"]})
                return {"Cookie": "subject=branch", "X-Csrf-Token": "csrf-branch"}

            client = QuickBiClient(session_exchange=exchange)
            with self.assertRaisesRegex(QuickBiError, "accountType"):
                client.create_access_ticket(worksId="works", accountName="branch")
            self.assertFalse(requests)
            self.assertTrue(client.check_alive())
            creation, identity = requests
            self.assertEqual(creation[:3], ("POST", "/openapi/v2/embed/ticket/create",
                             {"accountName": ["branch"], "accountType": ["3"], "worksId": ["works"]}))
            self.assertEqual(creation[4], b"")
            self.assertIsNotNone(creation[3].get("X-Gw-Signature"))
            self.assertIsNone(creation[3].get("Cookie"))
            self.assertEqual(identity[:3], ("GET", "/api/v2/user/currentUserInfo", {"accessTicket": ["synthetic+/="]}))
            self.assertEqual(identity[3]["Cookie"], "subject=branch")
            self.assertEqual(identity[3]["X-Csrf-Token"], "csrf-branch")
            self.assertEqual(identity[3]["X-Requested-With"], "XMLHttpRequest")
            self.assertIsNone(identity[3].get("X-Gw-AccessId"))

    def test_ticket_optional_query_and_plain_string_response(self):
        from backend.quickbi_client import QuickBiClient
        with protocol_endpoint(lambda _: json_reply({"data": "issued"})) as (requests, _):
            ticket = QuickBiClient().create_access_ticket(worksId="page", userId="user", expireTime=15,
                ticketNum=1, cmptId="component", globalParam={"地区": "杭州"})
            self.assertEqual(ticket, "issued")
            params = requests[0][2]
            self.assertEqual(json.loads(params.pop("globalParam")[0]), {"地区": "杭州"})
            self.assertEqual(params, {"worksId": ["page"], "userId": ["user"], "expireTime": ["15"],
                                      "ticketNum": ["1"], "cmptId": ["component"]})
            self.assertEqual(requests[0][4], b"")

    def test_olap_validation_precedes_any_request(self):
        from backend.quickbi_client import QuickBiClient, QuickBiError
        field = {"fid": "amount", "areaType": "column"}
        with protocol_endpoint(lambda _: json_reply({})) as (requests, _):
            client = QuickBiClient()
            for args in ([], [{"value": "a"}, {"value": "b"}]):
                condition = {"field": {"fid": "org"}, "functionalOperator": "in", "args": args}
                with self.assertRaisesRegex(QuickBiError, "单值") as caught:
                    client.query_olap(cube_id="cube", fields=[field], conditions=[condition])
                self.assertEqual(caught.exception.code, "QUICKBI_QUERY")
            with self.assertRaisesRegex(QuickBiError, "聚合") as caught:
                client.query_olap(cube_id="cube", fields=[{**field, "aggregate": "sum"}], query_detail=True)
            self.assertEqual(caught.exception.code, "NOX5300008")
            self.assertFalse(requests)

    def test_smartq_ticket_response_shapes_and_error_redaction(self):
        from backend.quickbi_client import create_smartq_ticket, QuickBiError
        ticket = "synthetic-ticket"
        url = "/generated-login"
        replies = [{"ticket": ticket, "url": url}, {"data": {"ticket": ticket, "url": url}},
                   {"data": ticket}, {"ticket": "", "message": "private-diagnostic"}]
        with protocol_endpoint(lambda _: json_reply(replies.pop(0))) as (requests, origin):
            endpoint = origin + "/api/report/createTicket"
            for expected_url in (url, url, ""):
                self.assertEqual(create_smartq_ticket("works", svc_url=endpoint),
                                 {"ticket": ticket, "url": expected_url})
            with self.assertRaises(QuickBiError) as caught:
                create_smartq_ticket("works", svc_url=endpoint)
            self.assertNotIn("private-diagnostic", str(caught.exception))
            self.assertNotIn(origin, str(caught.exception))
            for method, path, query, headers, body in requests:
                self.assertEqual((method, path, query, body),
                                 ("GET", "/api/report/createTicket", {"worksId": ["works"]}, b""))
                for key in ("Cookie", "Authorization", "X-Gw-AccessId", "X-Gw-Signature", "X-Csrf-Token"):
                    self.assertIsNone(headers.get(key))
        for invalid in ("file:///private-origin", "https://", "http://private-origin:bad-port"):
            with self.assertRaises(QuickBiError) as caught:
                create_smartq_ticket("works", svc_url=invalid)
            self.assertNotIn("private-origin", str(caught.exception))

    def test_smartq_redirects_and_session_lifecycle(self):
        from backend.quickbi_client import QuickBiClient
        issue = 0
        def reply(path):
            nonlocal issue
            if path == "/api/report/createTicket":
                issue += 1
                return json_reply({"data": f"issued-{issue}"})
            if path.startswith("/token3rd/"):
                return 302, "text/html", [], [("Location", "/login/continue"),
                    ("Set-Cookie", f"x_login_ck=login-{issue}; Path=/"), ("Set-Cookie", "old=stale; Path=/")]
            if path == "/login/continue":
                return 302, "text/html", [], [("Location", "done"), ("Set-Cookie", "old=; Max-Age=0; Path=/"),
                    ("Set-Cookie", f"csrf_token=csrf-{issue}; Path=/")]
            if path == "/login/done":
                return 200, "text/html", []
            if path.endswith("/currentUserInfo"):
                return json_reply({"success": True, "data": {"name": "branch"}})
            if path == "/api/v2/olap/query" and issue == 1:
                return json_reply({"success": False, "code": "AUTHX_01002"})
            return 200, "application/json", [b'{"data":{"rows":[[0.1234567890123456789]]}}']

        with protocol_endpoint(reply) as (requests, origin), patch.dict(os.environ, {
                "IDENTITY_PROVIDER": "smartq_ticket", "SMARTQ_SVC_URL": origin, "EMBED_WORKS_ID": "works",
                "QB_COMPONENT_ID": "env-component"}):
            client = QuickBiClient()
            start = time.monotonic()
            session = client.login()
            self.assertEqual(session["login_kind"], "smartq_ticket")
            self.assertEqual(session["source"], "ticket-http")
            self.assertEqual(session["ticket"], "issued-1")
            self.assertEqual(session["csrf"], "csrf-1")
            self.assertTrue(start + 300 <= session["expires_at"] <= time.monotonic() + 300)
            self.assertEqual(client.get_session(), session)
            self.assertEqual(client.whoami(), {"name": "branch"})
            self.assertEqual(issue, 1)
            create, begin, middle, end, identity = requests
            self.assertEqual(create[:3], ("GET", "/api/report/createTicket", {"worksId": ["works"]}))
            self.assertIsNone(create[3].get("Cookie"))
            self.assertIsNone(create[3].get("X-Gw-Signature"))
            self.assertEqual(begin[:3], ("GET", "/token3rd/dashboard/view/pc.htm",
                                        {"pageId": ["works"], "accessTicket": ["issued-1"], "locale": ["zh-CN"]}))
            self.assertIsNone(begin[3].get("Cookie"))
            self.assertEqual({s.strip() for s in middle[3]["Cookie"].split(";")}, {"x_login_ck=login-1", "old=stale"})
            self.assertEqual({s.strip() for s in end[3]["Cookie"].split(";")}, {"x_login_ck=login-1", "csrf_token=csrf-1"})
            self.assertEqual(identity[2], {"accessTicket": ["issued-1"]})
            self.assertEqual(identity[3]["Cookie"], session["cookie"])
            self.assertEqual(identity[3]["X-Csrf-Token"], "csrf-1")

            data = client.olap_query({"cubeId": "cube", "fields": [{"fid": "数额", "areaType": "column"}]})
            self.assertEqual(data["rows"], [["0.1234567890123456789"]])
            reads = [r for r in requests if r[1] == "/api/v2/olap/query"]
            self.assertEqual((issue, len(reads)), (2, 2))
            for n, req in enumerate(reads, 1):
                fields, conditions, paging = olap_form(req, origin, cube="cube", component="env-component",
                    ticket=f"issued-{n}", cookie={"x_login_ck": f"login-{n}", "csrf_token": f"csrf-{n}"},
                    csrf=f"csrf-{n}")
                self.assertEqual(fields[0]["aggregate"], "sum")
                self.assertEqual(conditions, [])
                self.assertEqual((paging["limit"], paging["offset"]), (5000, 0))
            self.assertEqual(reads[0][4], reads[1][4])
            forced = client.login(force=True)
            self.assertEqual(forced["ticket"], "issued-3")
            with patch("time.monotonic", return_value=forced["expires_at"] + 1):
                self.assertEqual(client.login()["ticket"], "issued-4")

    def test_cross_origin_ticket_redirect_is_never_contacted(self):
        from backend.quickbi_client import QuickBiError, open_ticket_session_http
        with protocol_endpoint(lambda _: json_reply({})) as (foreign_requests, foreign_origin):
            target = foreign_origin + "/secret-target?accessTicket=secret-ticket"
            redirect = (302, "text/html", [], [("Location", target), ("Set-Cookie", "secret=session-value")])
            with protocol_endpoint(lambda _: redirect) as (requests, origin):
                for login_url in (None, target):
                    with self.assertRaises(QuickBiError) as caught:
                        open_ticket_session_http(origin, "works", "secret-ticket", login_url=login_url)
                    self.assertEqual(caught.exception.code, "QUICKBI_SESSION")
                    for secret in (origin, foreign_origin, "secret-target", "secret-ticket", "session-value"):
                        self.assertNotIn(secret, str(caught.exception))
                self.assertEqual(len(requests), 1)
                self.assertEqual(foreign_requests, [])

    def test_ticket_csrf_case_priority_and_hop_limit(self):
        from backend.quickbi_client import QuickBiError, open_ticket_session_http
        cookies = [("Set-Cookie", "X-CSRF-Token=fallback")]
        with protocol_endpoint(lambda _: (200, "text/html", [], cookies)) as (_, origin):
            for expected in ("fallback", "preferred"):
                self.assertEqual(open_ticket_session_http(origin, "works", "ticket")["csrf"], expected)
                cookies.append(("Set-Cookie", "CSRF_TOKEN=preferred"))
        redirect = (307, "text/html", [], [("Location", "/loop")])
        with protocol_endpoint(lambda _: redirect) as (requests, origin):
            with self.assertRaises(QuickBiError) as caught:
                open_ticket_session_http(origin, "works", "ticket", max_hops=2)
            self.assertEqual(caught.exception.code, "QUICKBI_SESSION")
            self.assertEqual(len(requests), 3)

    def test_auth_renewal_stops_after_one_retry_and_skips_static_sessions(self):
        from backend.quickbi_client import QuickBiClient, QuickBiError
        def reply(path):
            if path.endswith("/create"):
                return json_reply({"data": f"ticket-{len(requests)}"})
            return json_reply({"code": "AUTHX_01002"}, 403)

        for mode, dynamic in (("ticket", True), ("ticket", False), ("cookie", False)):
            with self.subTest(mode=mode, dynamic=dynamic), protocol_endpoint(reply) as (requests, _), patch.dict(
                    os.environ, {"IDENTITY_PROVIDER": mode, "EMBED_WORKS_ID": "works",
                                 "QB_ACCESS_TICKET": "" if dynamic else "static-ticket"}):
                exchange = (lambda t, _: {"Cookie": "sid=" + t, "X-Csrf-Token": "csrf"}) if dynamic else None
                with self.assertRaises(QuickBiError) as caught:
                    QuickBiClient(session_exchange=exchange).whoami()
                self.assertEqual(caught.exception.code, "AUTHX_01002")
                reads = [r for r in requests if r[1].endswith("/currentUserInfo")]
                self.assertEqual(len(reads), 2 if dynamic else 1)
                if dynamic:
                    self.assertNotEqual(reads[0][2]["accessTicket"], reads[1][2]["accessTicket"])
                else:
                    self.assertEqual(len(requests), 1)

    def test_gateway_errors_are_actionable_and_redacted(self):
        from backend.quickbi_client import QuickBiClient, QuickBiError
        for code, message in [("OE10010106", "未授权"), ("OE10010101", "校准"), ("AUTHX_01002", "登录态"),
                              ("AE0130010003", "三方嵌入"), ("AE0510150001", "QB_REPORT_ID")]:
            response = {"success": False, "errorCode": code, "message": "secret-diagnostic"}
            with protocol_endpoint(lambda _: json_reply(response, 403)), self.assertRaises(QuickBiError) as caught:
                QuickBiClient().check_alive()
            self.assertEqual(caught.exception.code, code)
            self.assertIn(message, str(caught.exception))
            self.assertNotIn("secret-diagnostic", str(caught.exception))
        with patch.dict(os.environ, {"IDENTITY_PROVIDER": "dataService", "DATA_SERVICE_SID": "sid"}):
            with self.assertRaisesRegex(QuickBiError, "数据服务适配器尚未发布/启用"):
                QuickBiClient()

    def test_sse_result_survives_trailing_errors(self):
        from backend.quickbi_client import SmartQStreamClient
        chart = {"chartType": "DETAIL_TABLE", "metaType": [{"k": "指标"}, {"k": "数值"}],
                 "values": [{"row": ["净息差", 2.37]}]}
        stream = stream_reply(("trace", "example-trace"), ("locale", "zh_CN"), ("message", "next-id"),
                              ("heartbeat", ""), ("reasoning", "内部诊断"), ("text", "净息差"),
                              ("summary", ""), ("dsl", ""), ("sql", ""), ("answer", ""),
                              ("olapResult", json.dumps(chart)), ("check", ""), ("error", "secret-diagnostic"),
                              ("errorInfo", "secret-detail"), ("finish", ""))
        with protocol_endpoint(lambda _: stream) as (requests, origin):
            result = SmartQStreamClient().query(userId="user", userQuestion="查询净息差", cubeId="cube",
                        chatId="chat", appendedMessageId="previous", llmNameForInference="Qwen3.6")
            self.assertEqual(result["records"], [{"指标": "净息差", "数值": 2.37}])
            self.assertEqual(result["chartType"], "DETAIL_TABLE")
            self.assertEqual(result["messageId"], "next-id")
            self.assertEqual(result["diagnostics"]["trailing_error"], "errorInfo")
            self.assertNotIn("secret-diagnostic", str(result))
            method, path, _, headers, body = requests[0]
            self.assertEqual((method, path), ("POST", "/openapi/v2/smartq/queryByQuestionStream"))
            self.assertEqual(headers["Origin"], origin)
            self.assertEqual(headers["Accept"], "text/event-stream")
            self.assertIsNotNone(headers.get("X-Gw-Signature"))
            self.assertIsNone(headers.get("Cookie"))
            self.assertEqual(json.loads(body), {"userId": "user", "userQuestion": "查询净息差", "cubeId": "cube",
                 "chatId": "chat", "appendedMessageId": "previous", "llmNameForInference": "Qwen3.6",
                 "llmNameForData": "nv1"})

    def test_sse_error_or_misaligned_result_fails_without_data(self):
        from backend.quickbi_client import SmartQStreamClient, QuickBiError
        chart = {"metaType": [{"k": "a"}, {"k": "b"}], "values": [{"row": [1]}]}
        for events in [(("reject", "refused"),), (("olapResult", chart), ("errorInfo", "bad"))]:
            with protocol_endpoint(lambda _: stream_reply(*events)), self.assertRaisesRegex(QuickBiError, "olapResult"):
                SmartQStreamClient().query(userId="u", userQuestion="问", cubeId="c")

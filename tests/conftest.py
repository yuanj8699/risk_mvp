"""整套测试共用隔离的 API 会话与临时数据库。"""
import asyncio
import importlib
import os
from pathlib import Path
import sys
import tempfile

import httpx
import pytest


@pytest.fixture(scope="session")
def api():
    with tempfile.TemporaryDirectory(prefix="risk-agent-tests-") as directory:
        previous = os.environ.get("RISK_DATABASE_URL")
        previous_mode = os.environ.get("RISK_PI_MODE")
        previous_source = os.environ.pop("RISK_SOURCE_DATABASE_URL", None)
        os.environ["RISK_PI_MODE"] = "demo"
        os.environ["RISK_DATABASE_URL"] = "sqlite:///" + str(Path(directory) / "test.db")
        os.environ["RISK_DEMO"] = "1"
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        module = importlib.import_module("backend.app")
        # 在同一个线程走完整 ASGI 路由；短时钟保持受限运行环境中的回调响应。
        loop = asyncio.new_event_loop()

        async def heartbeat():
            while True:
                await asyncio.sleep(0.01)

        ticker = loop.create_task(heartbeat())
        lifecycle = module.app.router.lifespan_context(module.app)
        loop.run_until_complete(lifecycle.__aenter__())
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://testserver")

        class SynchronousASGIClient:
            def request(self, method, path, **kwargs):
                return loop.run_until_complete(client.request(method, path, **kwargs))

            def get(self, path, **kwargs):
                return self.request("GET", path, **kwargs)

            def post(self, path, **kwargs):
                return self.request("POST", path, **kwargs)

            def patch(self, path, **kwargs):
                return self.request("PATCH", path, **kwargs)

        try:
            yield SynchronousASGIClient()
        finally:
            loop.run_until_complete(client.aclose())
            loop.run_until_complete(lifecycle.__aexit__(None, None, None))
            ticker.cancel()
            loop.run_until_complete(asyncio.gather(ticker, return_exceptions=True))
            loop.close()
            module.engine.dispose()
        if previous is None:
            os.environ.pop("RISK_DATABASE_URL", None)
        else:
            os.environ["RISK_DATABASE_URL"] = previous
        if previous_mode is None:
            os.environ.pop("RISK_PI_MODE", None)
        else:
            os.environ["RISK_PI_MODE"] = previous_mode
        if previous_source is not None:
            os.environ["RISK_SOURCE_DATABASE_URL"] = previous_source

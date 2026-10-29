"""Python boundary for the TypeScript Pi Agent runtime.

Only the frozen indicator catalogue and case evidence cross this boundary.
Rules, database writes, approval and delivery stay in Python.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]


class AnalysisError(RuntimeError):
    """An explicit agent configuration, execution or protocol failure."""


class PiAnalysisProvider:
    version = "pi-1.4.0-risk-1"

    @property
    def mode(self):
        return "pi-" + os.getenv("RISK_PI_MODE", "demo")

    @property
    def cache_identity(self):
        return {
            "mode": self.mode,
            "version": self.version,
            "provider": os.getenv("RISK_PI_PROVIDER", ""),
            "model": os.getenv("RISK_PI_MODEL", ""),
            "base_url": os.getenv("RISK_PI_BASE_URL", ""),
        }

    def _request(self, payload):
        node = shutil.which("node")
        runner = ROOT / "agent" / "dist" / "runner.js"
        if not node or not runner.is_file():
            raise AnalysisError("Pi Agent 未安装：需要 Node.js 22.19+，请在 agent 目录执行 npm ci --ignore-scripts 和 npm run build。")
        try:
            result = subprocess.run(
                [node, str(runner)],
                input=json.dumps(payload, ensure_ascii=False),
                text=True, encoding="utf-8", capture_output=True,
                cwd=ROOT / "agent", timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except subprocess.TimeoutExpired as exc:
            raise AnalysisError("Pi Agent 超过本批次180秒时限，执行已终止；本次运行未保存，请检查模型服务后重试。") from exc
        except OSError as exc:
            raise AnalysisError(f"Pi Agent 进程无法启动：{exc.strerror}") from exc
        try:
            response = json.loads(result.stdout)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise AnalysisError(f"Pi Agent 未返回有效结果（退出码 {result.returncode}），请检查 agent 依赖及配置。") from exc
        if result.returncode or not isinstance(response, dict) or "error" in response:
            error = response.get("error", "执行失败") if isinstance(response, dict) else "返回格式错误"
            if isinstance(error, dict):
                error = error.get("message", "执行失败")
            raise AnalysisError(f"Pi Agent：{error}")
        return response

    def select(self, topic, indicators, mandatory_ids):
        selection = self._request({"operation": "select", "topic": topic,
                                   "indicators": indicators, "mandatory_ids": mandatory_ids})
        allowed = {item["id"] for item in indicators}
        selected = selection.get("indicator_ids")
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
            raise AnalysisError("Pi Agent 指标选择结果格式错误。")
        if not set(selected) <= allowed or not set(mandatory_ids) <= set(selected):
            raise AnalysisError("Pi Agent 返回未知指标或遗漏必查指标，已拒绝本次结果。")
        return selection

    def explain_batch(self, cases):
        if not cases:
            return {}
        response = self._request({"operation": "explain_batch", "cases": cases})
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
        return analyses


provider = PiAnalysisProvider()

"""本地启动入口；默认仅监听本机，鉴权和真实下发不属于此测试版。"""
from pathlib import Path
import argparse
import os
from dotenv import dotenv_values, load_dotenv


def load_model_settings(root: Path) -> None:
    """读取本机启动配置，按明确指定的位置从服务端加载 OpenAI 密钥。"""
    load_dotenv(root / ".env", override=False, interpolate=False, encoding="utf-8-sig")
    key_file = os.environ.get("RISK_OPENAI_ENV_FILE")
    if os.environ.get("RISK_PI_MODE") != "model" or not key_file or os.environ.get("RISK_PI_API_KEY"):
        return
    if os.environ.get("RISK_PI_PROVIDER") != "openai" or os.environ.get("RISK_PI_BASE_URL"):
        raise SystemExit("引用 OpenAI 密钥文件时，请使用 openai 提供方及其默认接口。")
    key = dotenv_values(Path(key_file).expanduser(), encoding="utf-8-sig", interpolate=False).get("OPENAI_API_KEY")
    if not key:
        raise SystemExit("配置的模型密钥文件中缺少 OPENAI_API_KEY。")
    os.environ["RISK_PI_API_KEY"] = key


def main() -> None:
    """统一工作目录，使从任意位置启动时都使用同一份代码和数据。"""
    root = Path(__file__).resolve().parent
    os.chdir(root)
    load_model_settings(root)
    parser = argparse.ArgumentParser(description="行社风险提示本地测试版")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("请先运行：python -m pip install -r requirements.txt") from exc
    print(f"行社风险提示测试版：http://{args.host}:{args.port}")
    print("内置数据为合成数据。下发操作仅生成本地模拟回执。")
    uvicorn.run("backend.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()

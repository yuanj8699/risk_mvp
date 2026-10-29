"""Start the local prototype. Production identity and delivery are out of scope."""
from pathlib import Path
import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(description="行社风险提示本地测试版")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parent)
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("请先运行：python -m pip install -r requirements.txt") from exc
    print(f"行社风险提示测试版：http://{args.host}:{args.port}")
    print("内置数据为合成数据。下发操作仅生成本地模拟回执。")
    uvicorn.run("backend.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()

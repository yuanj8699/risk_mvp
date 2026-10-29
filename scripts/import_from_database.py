"""Read a canonical database view and submit it through the normal import API.

Credentials stay in environment variables. No arbitrary SQL or write operation is
accepted. The source account must already be read-only. XiaoQ/DataWorks-specific
network and authentication are deliberately not guessed.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date
import io
import os
import re

import httpx
from sqlalchemy import create_engine, text

COLUMNS = ["org_code", "indicator_id", "data_date", "value", "unit", "rank", "population", "rank_scope", "value_encoding"]


def export_view(source_url: str, view_name: str, data_date: str) -> bytes:
    date.fromisoformat(data_date)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?", view_name):
        raise ValueError("来源必须是固定的表/视图名，可包含一层schema，不能输入SQL。")
    engine = create_engine(source_url)
    try:
        with engine.connect() as connection:
            dialect = engine.dialect.name
            if dialect == "postgresql":
                connection.execute(text("SET TRANSACTION READ ONLY"))
            elif dialect == "sqlite":
                connection.execute(text("PRAGMA query_only = ON"))
            else:
                raise ValueError("测试版数据库适配器只实现SQLite/PostgreSQL；其他DataWorks引擎需实现独立适配器。")
            columns_sql = ", ".join('"' + col + '"' for col in COLUMNS)
            view_sql = ".".join('"' + part + '"' for part in view_name.split("."))
            query = text(f"SELECT {columns_sql} FROM {view_sql} WHERE data_date = :data_date LIMIT 10001")
            rows = connection.execute(query, {"data_date": data_date}).mappings().all()
            if not rows:
                raise ValueError("该日期无来源记录，未提交空批次。")
            if len(rows) > 10000:
                raise ValueError("超过测试版10000行限制，请缩小来源视图范围。")
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=COLUMNS)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: "" if row[key] is None else str(row[key]) for key in COLUMNS})
            return output.getvalue().encode("utf-8-sig")
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="通过标准视图导入测试数据")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--view", default="risk_indicator_export")
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    source_url = os.getenv("RISK_SOURCE_DATABASE_URL")
    if not source_url:
        raise SystemExit("请在环境变量RISK_SOURCE_DATABASE_URL中配置只读来源连接；不要把密码放到命令行。")
    try:
        payload = export_view(source_url, args.view, args.date)
        response = httpx.post(
            args.api.rstrip("/") + "/api/import",
            files={"file": ("database_export.csv", payload, "text/csv")},
            data={"source_kind": "database"},
            timeout=45,
        )
        response.raise_for_status()
    except (ValueError, httpx.HTTPError) as exc:
        # Do not print SQLAlchemy connection exceptions containing source secrets.
        raise SystemExit(f"导入未完成：{type(exc).__name__}。请核对日期、标准视图和应用日志。") from None
    except Exception as exc:
        raise SystemExit(f"来源读取未完成：{type(exc).__name__}。请检查只读连接与标准视图。") from None
    print(response.text)


if __name__ == "__main__":
    main()

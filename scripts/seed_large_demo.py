"""生成独立的大规模合成数据：24期 × 82家行社 × 8项指标。

仅用于代码执行/导出验收；季度指标保留真实季度日期，跨快照读取时应去重。
CLI必须显式指定SQLite文件，且拒绝写入项目默认演示数据库。
"""
from __future__ import annotations

import argparse
from calendar import monthrange
from datetime import date
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


SOURCE = "synthetic-large"
LATEST_SNAPSHOT_ID = "large-2026-10-28"
PRECISE_AMOUNT = Decimal("1234567890.12345678")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _periods() -> list[date]:
    """从2024年11月起取24个有效月末；最后一期固定为实际截至日。"""
    result = []
    for offset in range(24):
        year, month_index = divmod(2024 * 12 + 10 + offset, 12)
        month = month_index + 1
        result.append(date(year, month, monthrange(year, month)[1]))
    result[-1] = date(2026, 10, 28)
    return result


def _quarter_end(day: date) -> date:
    """返回不晚于快照日期的最近季度末，避免生成未来期次。"""
    month = ((day.month - 1) // 3 + 1) * 3
    candidate = date(day.year, month, monthrange(day.year, month)[1])
    if candidate <= day:
        return candidate
    month -= 3
    year = day.year
    if month == 0:
        year, month = year - 1, 12
    return date(year, month, monthrange(year, month)[1])


def seed_large_demo(db: Session) -> str:
    """在调用方指定的应用库追加独立source，重复执行复用相同快照与观测ID。"""
    from backend.models import Observation, Snapshot
    from backend.seed import METRICS, seed_database

    seed_database(db)
    for period, day in enumerate(_periods()):
        snapshot_id = f"large-{day.isoformat()}"
        existing = db.get(Snapshot, snapshot_id)
        if existing is not None:
            if existing.source != SOURCE:
                raise ValueError(f"快照ID已被其他数据来源使用：{snapshot_id}")
            continue
        db.add(Snapshot(
            id=snapshot_id, name=f"大规模合成数据 {day.isoformat()}", data_date=day,
            source=SOURCE, is_demo=True, row_count=82 * len(METRICS), status="ready",
        ))
        db.flush()
        quarter_day = _quarter_end(day)
        quarter_index = (quarter_day.year - 2024) * 4 + quarter_day.month // 3 - 3
        for i in range(1, 83):
            org_id = f"LS{i:03d}"
            ranks = {
                "npl_ratio": i, "overdue_ratio": (i * 17 - 1) % 82 + 1,
                "new_npl_ratio": (i * 23 - 1) % 82 + 1,
                "deposit_growth": (i * 31 - 1) % 82 + 1,
            }
            funds = PRECISE_AMOUNT if i == 1 else Decimal(i * 90000 + quarter_index * 27000) + Decimal("0.12345678")
            values = {
                "npl_ratio": Decimal("0.20") + Decimal(i) * Decimal("0.035") + Decimal(period) * Decimal("0.04"),
                "overdue_ratio": Decimal("0.35") + Decimal(ranks["overdue_ratio"]) * Decimal("0.045") + Decimal(period) * Decimal("0.025"),
                "new_npl_ratio": Decimal("0.05") + Decimal(ranks["new_npl_ratio"]) * Decimal("0.018"),
                "deposit_growth": Decimal("12") - Decimal(ranks["deposit_growth"]) * Decimal("0.17"),
                "funds_npl_balance": funds,
                "funds_npl_change": -Decimal(180000 + quarter_index * 3000) if i % 26 == 0 else Decimal(i * 1000 + quarter_index * 250),
                "pure_deposit_growth": Decimal("10") - Decimal(i) * Decimal("0.11") + Decimal(period) * Decimal("0.02"),
                "funds_npl_ratio": Decimal(i) * Decimal("0.003") + Decimal(quarter_index) * Decimal("0.001"),
            }
            for key, _, unit, frequency, _ in METRICS:
                db.add(Observation(
                    id=f"{snapshot_id}-{org_id}-{key}", snapshot_id=snapshot_id,
                    org_id=org_id, indicator_id=key,
                    data_date=quarter_day if frequency == "季" else day,
                    value=values[key], unit=unit, rank=ranks.get(key),
                    population=82 if key in ranks else None,
                    rank_scope="province_societies" if key in ranks else "", quality_status="valid",
                ))
    db.commit()
    return LATEST_SNAPSHOT_ID


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="专用合成数据SQLite文件路径")
    args = parser.parse_args()
    target = args.database.expanduser().resolve()
    if target == (PROJECT_ROOT / "data" / "risk-demo.db").resolve():
        parser.error("请使用独立数据库文件，不能写入项目默认演示库。")
    target.parent.mkdir(parents=True, exist_ok=True)
    # 必须在导入models前绑定目标，避免其全局engine连接到默认演示库。
    os.environ["RISK_DATABASE_URL"] = "sqlite:///" + target.as_posix()
    sys.path.insert(0, str(PROJECT_ROOT))
    from backend.models import Base, SessionLocal, engine
    Base.metadata.create_all(engine)
    try:
        with SessionLocal() as db:
            latest = seed_large_demo(db)
        print(json.dumps({"database": str(target), "source": SOURCE,
                          "snapshot_count": 24, "observation_count": 15744,
                          "latest_snapshot_id": latest}, ensure_ascii=False))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

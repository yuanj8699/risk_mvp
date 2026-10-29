"""Deterministic synthetic fixtures. Names are fictional; ranks are source fixtures."""
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from .models import Organization, Indicator, Snapshot, Observation, Rule

METRICS = [
    ("npl_ratio", "五级不良贷款率", "%", "日", "信用风险账面指标；1.65 表示 1.65%，定义和发函口径待行内核验。"),
    ("overdue_ratio", "逾期贷款率", "%", "日", "潜在信用风险指标；与五级不良贷款率分开维护。"),
    ("new_npl_ratio", "不良发生净生成率", "%", "日", "新增信用风险指标；不替代其他不良率口径。"),
    ("deposit_growth", "各项存款余额（人行）比年初增幅", "%", "日", "存款扩张指标；仅使用上游已给出的排名。"),
    ("funds_npl_balance", "资金业务不良余额", "元", "季", "资金业务不良存在提醒；测试中采用非空非零。"),
    ("funds_npl_change", "资金业务不良余额比年初增量", "元", "季", "辅助指标；负增量表示较年初减少，不等于风险恶化。"),
    ("pure_deposit_growth", "纯存款余额比年初增幅", "%", "日", "存款扩张辅助指标。"),
    ("funds_npl_ratio", "资金业务不良率", "%", "季", "资金业务风险辅助指标，按其实际数据期次使用。"),
]


def seed_database(db):
    if db.scalar(select(Organization.id).limit(1)):
        return
    db.add(Organization(id="HQ", code="HQ", name="测试总行", level=1, parent_id=None, region_id=None))
    db.flush()
    for i in range(1, 12):
        code = f"RG{i:02d}"
        db.add(Organization(id=code, code=code, name=f"合成地区{i:02d}", level=2, parent_id="HQ", region_id=code))
    db.flush()
    for i in range(1, 83):
        code, region = f"LS{i:03d}", f"RG{(i - 1) % 11 + 1:02d}"
        db.add(Organization(id=code, code=code, name=f"合成行社{i:03d}", level=3, parent_id=region, region_id=region))
    db.flush()
    db.add(Organization(id="BR001", code="BR001", name="合成支行（预留示例）", level=4, parent_id="LS001", region_id="RG01"))
    db.flush()
    db.add(Organization(id="LV5EX01", code="LV5EX01", name="第五级机构（预留示例）", level=5, parent_id="BR001", region_id="RG01"))
    for key, name, unit, frequency, description in METRICS:
        db.add(Indicator(id=key, name=name, unit=unit, frequency=frequency, description=description))
    db.flush()
    for key in ("npl_ratio", "overdue_ratio", "new_npl_ratio", "deposit_growth"):
        name = next(x[1] for x in METRICS if x[0] == key)
        db.add(Rule(id=f"rank_{key}", name=f"{name}排名提醒", kind="rank", indicator_id=key, enabled=True,
                    params={"cutoff": 72, "scope": "province_societies", "population": 82, "rank_direction": "1_best"},
                    version=1, description="沿用附件中的排名≥72。测试排名为合成数据，正式适用指标待核验。"))
    db.add(Rule(id="ratio_npl_ratio", name="五级不良贷款率占比提醒", kind="ratio", indicator_id="npl_ratio", enabled=True,
                params={"threshold": None, "op": ">="}, version=1, description="未提供行内占比阈值；保持待配置，不产生默认异常判断。"))
    db.add(Rule(id="exists_funds_npl_balance", name="资金业务不良存在提醒", kind="exists", indicator_id="funds_npl_balance", enabled=True,
                params={"op": "!=0"}, version=1, description="有效值非零即提醒；缺失不等于零，排名不参与筛选。"))
    days = [date(2026, 8, 31), date(2026, 9, 30), date(2026, 10, 28)]
    for period, day in enumerate(days):
        snap = Snapshot(id=f"demo-{day.isoformat()}", name=f"合成测试数据 {day.isoformat()}", data_date=day, source="synthetic", is_demo=True, row_count=82 * len(METRICS), status="ready")
        db.add(snap)
        db.flush()
        for i in range(1, 83):
            ranks = {"npl_ratio": i, "overdue_ratio": (i * 17 - 1) % 82 + 1,
                     "new_npl_ratio": (i * 23 - 1) % 82 + 1, "deposit_growth": (i * 31 - 1) % 82 + 1}
            funds = Decimal(i * 90000) if i % 13 == 0 else Decimal("0")
            values = {
                "npl_ratio": Decimal("0.20") + Decimal(i) * Decimal("0.035") + Decimal(period) * Decimal("0.04"),
                "overdue_ratio": Decimal("0.35") + Decimal(ranks["overdue_ratio"]) * Decimal("0.045"),
                "new_npl_ratio": Decimal("0.05") + Decimal(ranks["new_npl_ratio"]) * Decimal("0.018"),
                "deposit_growth": Decimal("12") - Decimal(ranks["deposit_growth"]) * Decimal("0.17"),
                "funds_npl_balance": funds,
                "funds_npl_change": (-Decimal("180000") if i % 26 == 0 else funds / 4),
                "pure_deposit_growth": Decimal("10") - Decimal(i) * Decimal("0.11"),
                "funds_npl_ratio": funds / Decimal("10000000"),
            }
            for key, _, unit, frequency, _ in METRICS:
                value = values[key]
                if period == 2 and (i, key) in {(4, "npl_ratio"), (17, "overdue_ratio"), (39, "funds_npl_balance")}:
                    value = None
                effective_day = (date(2026, 6, 30) if day < date(2026, 9, 30) else date(2026, 9, 30)) if frequency == "季" else day
                db.add(Observation(snapshot_id=snap.id, org_id=f"LS{i:03d}", indicator_id=key, data_date=effective_day,
                                   value=value, unit=unit, rank=ranks.get(key), population=82 if key in ranks else None,
                                   rank_scope="province_societies" if key in ranks else "", quality_status="missing" if value is None else "valid"))
    db.commit()

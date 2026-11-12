"""将项目提供的真实行社名称与虚构指标数据导入独立 SQLite 测试库。

CSV 的机构号、指标值、排名均为模拟。30 个跑批快照只保留每项当时最新
观测；源 CSV 中重复载入的月季历史由独立的历史期次快照提供。
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT.parent / '测试数据'
SOURCE = 'synthetic-named-62'
ALIASES = {27: 'npl_ratio', 40: 'overdue_ratio', 11: 'deposit_growth',
           74: 'funds_npl_balance', 75: 'funds_npl_change'}


def read_csv(name: str) -> list[dict[str, str]]:
    with (DATA / name).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def parse_day(value: str) -> date:
    return datetime.strptime(value, '%Y%m%d').date()


def read_input():
    """校验 CSV 机构、目录和全部批次；同一批只选各指标最新期次。"""
    orgs = read_csv('机构维度.csv')
    specs = read_csv('指标及频率.csv')
    if len(orgs) != 82 or len({o['机构号'] for o in orgs}) != 82 or len({o['机构名称'] for o in orgs}) != 82:
        raise ValueError('机构维度必须包含 82 家名称和模拟机构号均唯一的行社')
    if any(not o['机构号'].startswith('TEST_') or not o['地区号'].startswith('TEST_') for o in orgs):
        raise ValueError('机构与地区编码必须明确为 TEST 模拟编码')
    if (len({o['地区号'] for o in orgs}) != 11 or len({o['地区名称'] for o in orgs}) != 11
            or len({(o['地区号'], o['地区名称']) for o in orgs}) != 11):
        raise ValueError('机构维度必须包含 11 个地区')
    if len(specs) != 62 or len({s['函件指标编号'] for s in specs}) != 62:
        raise ValueError('指标目录必须包含 62 项唯一指标')
    org_map = {o['机构号']: o for o in orgs}
    spec_map = {s['函件指标编号']: s for s in specs}
    for s in specs:
        number = int(s['函件指标编号'].removeprefix('TEST_METRIC_'))
        s['_id'] = ALIASES.get(number, s['函件指标编号'])
        if s['测试生成频率'] not in {'日', '月', '季'}:
            raise ValueError('未知测试频率')
    if len({s['_id'] for s in specs}) != 62:
        raise ValueError('指标别名冲突')
    latest: dict[date, dict[tuple[str, str], dict]] = {}
    initial: dict[tuple[str, str, date], dict] = {}
    keys = set()
    rows = read_csv('指标测试数据.csv')
    for row in rows:
        batch, day = parse_day(row['跑批日期']), parse_day(row['数据日期'])
        oid, mid = row['机构号'], row['函件指标编号']
        if not date(2026, 9, 1) <= batch <= date(2026, 9, 30) or day > batch:
            raise ValueError('跑批或数据日期不符合范围')
        if (oid not in org_map or mid not in spec_map or row['机构名称'] != org_map[oid]['机构名称']
                or row['地区号'] != org_map[oid]['地区号'] or row['地区名称'] != org_map[oid]['地区名称']):
            raise ValueError('机构或指标不属于本次测试目录')
        if (row['指标名称'] != spec_map[mid]['指标名称'] or row['单位'] != spec_map[mid]['单位']
                or row['测试生成频率'] != spec_map[mid]['测试生成频率'] or row['数据性质'] != '虚构测试数据'):
            raise ValueError('指标名称或单位与目录不一致')
        key = (batch, day, oid, mid)
        if key in keys:
            raise ValueError('存在重复跑批观测')
        keys.add(key)
        try:
            val = Decimal(row['指标值'])
        except InvalidOperation as exc:
            raise ValueError('指标值无效') from exc
        if not val.is_finite():
            raise ValueError('指标值不是有限数')
        raw_rank, raw_pop = row['模拟排名'], row['参评数量']
        rank = int(raw_rank) if raw_rank else None
        population = int(raw_pop) if raw_pop else None
        if rank is not None and (not 1 <= rank <= 82 or population != 82 or not spec_map[mid]['来源排名标记']):
            raise ValueError('模拟排名缺少有效的82家参评范围')
        if (rank is None) != (population is None):
            raise ValueError('排名与参评数量不一致')
        item = {'org': oid, 'metric': mid, 'day': day, 'value': val,
                'rank': rank, 'population': population}
        bucket = latest.setdefault(batch, {})
        k = (oid, mid)
        if k not in bucket or day > bucket[k]['day']:
            bucket[k] = item
        if batch == date(2026, 9, 1):
            initial[(oid, mid, day)] = item
    expected = {date(2026, 9, d) for d in range(1, 31)}
    if set(latest) != expected or any(len(bucket) != 82 * 62 for bucket in latest.values()):
        raise ValueError('跑批不完整，要求30个跑批且每批覆盖82家、62项')
    history = {}
    for day in (date(2026, 6, 30), date(2026, 7, 31), date(2026, 8, 31)):
        selected = {}
        for (oid, mid, actual_day), item in initial.items():
            k = (oid, mid)
            if actual_day <= day and (k not in selected or actual_day > selected[k]['day']):
                selected[k] = item
        history[day] = selected
    if any(not bucket for bucket in history.values()):
        raise ValueError('缺少月度和季度历史观测')
    return orgs, specs, spec_map, history, latest, len(rows)


def build(database: Path, orgs, specs, spec_map, history, latest):
    """构建完全独立的测试库，历史业务期次与9月模拟跑批分别标注。"""
    os.environ['RISK_DATABASE_URL'] = 'sqlite:///' + database.as_posix()
    sys.path.insert(0, str(ROOT))
    from backend.models import Base, SessionLocal, engine, Organization, Indicator, Snapshot, Observation, Rule

    Base.metadata.create_all(engine)
    try:
        with SessionLocal.begin() as db:
            db.add(Organization(id='TEST_PROVINCE', code='TEST_PROVINCE', name='省级机构（测试占位）',
                                level=1, parent_id=None, region_id=None))
            db.flush()
            regions = {o['地区号']: o['地区名称'] for o in orgs}
            db.add_all([Organization(id=rid, code=rid, name=name, level=2, parent_id='TEST_PROVINCE', region_id=rid)
                        for rid, name in regions.items()])
            db.flush()
            db.add_all([Organization(id=o['机构号'], code=o['机构号'], name=o['机构名称'], level=3,
                                     parent_id=o['地区号'], region_id=o['地区号']) for o in orgs])
            for s in specs:
                unit = '元' if s['_id'] in {'funds_npl_balance', 'funds_npl_change'} else s['单位']
                description = f"虚构测试指标；源频率：{s['来源更新频率'] or '未注明'}；测试频率：{s['测试生成频率']}（{s['频率依据']}）；排名：{s['来源排名标记'] or '未注明'}。"
                db.add(Indicator(id=s['_id'], name=s['指标名称'], unit=unit,
                                 frequency=s['测试生成频率'], description=description))
            db.flush()
            for mid in ('npl_ratio', 'overdue_ratio'):
                name = next(s['指标名称'] for s in specs if s['_id'] == mid)
                db.add(Rule(id='rank_'+mid, name=name+'模拟排名提醒', kind='rank', indicator_id=mid, enabled=True,
                            params={'cutoff': 72, 'scope': 'province_societies', 'population': 82, 'rank_direction': '1_best'},
                            version=1, description='模拟排名规则，具体生产口径待确认。'))
            db.add(Rule(id='exists_funds_npl_balance', name='资金业务不良存在提醒', kind='exists',
                        indicator_id='funds_npl_balance', enabled=True, params={'op': '!=0'}, version=1,
                        description='有效值非零触发；数值为虚构测试数据。'))
            for prefix, snapshots in (('history', history), ('batch', latest)):
                for day, bucket in sorted(snapshots.items()):
                    sid = f'named-{prefix}-{day.isoformat()}'
                    label = '历史期次派生' if prefix == 'history' else '模拟跑批'
                    db.add(Snapshot(id=sid, name=f'真实行社名称·虚构数据 {label} {day.isoformat()}',
                                    data_date=day, source=SOURCE, is_demo=True, row_count=len(bucket), status='ready'))
                    db.flush()
                    observations = []
                    for item in bucket.values():
                        spec = spec_map[item['metric']]
                        metric_id = spec['_id']
                        converted = item['value'] * 10000 if metric_id in {'funds_npl_balance', 'funds_npl_change'} else item['value']
                        unit = '元' if metric_id in {'funds_npl_balance', 'funds_npl_change'} else spec['单位']
                        observations.append(Observation(snapshot_id=sid, org_id=item['org'], indicator_id=metric_id,
                                                        data_date=item['day'], value=converted, unit=unit,
                                                        rank=item['rank'], population=item['population'],
                                                        rank_scope='province_societies' if item['rank'] is not None else '',
                                                        quality_status='valid'))
                    db.add_all(observations)
                    db.flush()
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path, help='不存在的独立 SQLite 数据库路径')
    args = parser.parse_args()
    target = args.database.expanduser().resolve()
    if target.exists():
        parser.error('目标数据库已存在，拒绝覆盖')
    if target == (ROOT/'data'/'risk-demo.db').resolve():
        parser.error('不能修改默认演示库')
    orgs, specs, spec_map, history, latest, source_count = read_input()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.named-data-', suffix='.sqlite', dir=target.parent, delete=False) as f:
        temp = Path(f.name)
    try:
        build(temp, orgs, specs, spec_map, history, latest)
        # 同卷硬链接保证目标存在时不覆盖；完成后删去本次创建的临时名。
        os.link(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    print(json.dumps({'database': str(target), 'source': SOURCE, 'source_rows': source_count,
                      'organizations': len(orgs), 'indicators': len(specs),
                      'history_snapshots': len(history), 'batch_snapshots': len(latest),
                      'latest_snapshot': 'named-batch-2026-09-30'}, ensure_ascii=False))


if __name__ == '__main__':
    main()

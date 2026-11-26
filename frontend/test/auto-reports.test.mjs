import assert from 'node:assert/strict';
import test from 'node:test';
import { reportBanner, reportMarker } from '../src/auto-reports.ts';

const one = { org_id: 'a', org_name: '甲行', p0_count: 3, status: 'completed', revision: 1, error: null };
const two = { org_id: 'b', org_name: '乙行', p0_count: 4, status: 'running', revision: null, error: null };
const batch = { total: 2, completed: 1, failed: 0, status: 'running', items: [one, two] };

test('总览按归档逐家显示正在撰写的行社和当前进度', () => {
  assert.equal(reportBanner(batch, true).text, '风险报告自动生成中 · 1 / 2 家（正在撰写：乙行）');
  assert.equal(reportMarker(two, false), '生成中…');
  assert.equal(reportMarker(one, false), '已有报告');
  assert.equal(reportMarker(undefined, true), '已有报告');
  assert.equal(reportMarker(undefined, false), '');
});

test('历史任务按风险情况显示入口，有失败项时改为重试', () => {
  assert.equal(reportBanner(undefined, false), null);
  assert.equal(reportBanner(undefined, true).action, '自动生成重点报告');
  assert.equal(reportBanner({ ...batch, status: 'idle' }, true).action, '自动生成重点报告');
  for (const status of ['partial', 'partial_failed', 'failed']) {
    const banner = reportBanner({ ...batch, status, failed: 1 }, true);
    assert.equal(banner.action, '重试失败项');
    assert.equal(banner.text, '风险报告已生成 1 份 · 失败 1 家');
  }
});

test('完成横幅保留实际入选口径', () => {
  for (const [floors, label] of [[3, '≥3 项监管底线'], [1, '≥1 项监管底线'], [0, '有效扫描信号']]) {
    const banner = reportBanner({ ...batch, status: 'completed', completed: 2, items: [one, { ...two, p0_count: floors }] }, true);
    assert.equal(banner.text, `已自动生成 2 份重点高风险行社风险报告（${label}）`);
    assert.equal(banner.action, '');
  }
});

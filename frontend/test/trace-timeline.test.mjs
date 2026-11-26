import assert from 'node:assert/strict';
import test from 'node:test';
import { buildTraceTimeline, normalizeTraceTitle } from '../src/trace-timeline.ts';

const origin = Date.parse('2026-11-25T00:00:00Z');
function row(key, start, duration) {
  const started_at = start == null ? undefined : new Date(origin + start).toISOString();
  return { key, entry: { event: 'request', started_at, duration_ms: duration } };
}
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-7, `${actual} ≠ ${expected}`);

test('重叠事件和恰好 15 秒的间隔构成同一段，布局与真实跨度一致', () => {
  const rows = [row('last', 20000, 2000), row('nested', 1000, 500), row('first', 0, 5000)];
  const timeline = buildTraceTimeline(rows, [], 'duration');
  assert.equal(timeline.gapCount, 0);
  assert.deepEqual(timeline, buildTraceTimeline(rows, [], 'duration', false));
  close(timeline.positions.get('last').left, 20000 / 22000 * 100);
  assert.match(timeline.note, /活跃耗时 22\.00 s · 已折叠 0 处空闲（总跨度 22\.00 s）/);
});

test('跨小时轨迹压缩空闲并按活跃时长分配宽度，切换后恢复绝对坐标', () => {
  const rows = [row('second', 6150000, 60000), row('first', 0, 30000)];
  const folded = buildTraceTimeline(rows, [], 'duration');
  assert.deepEqual(folded.gaps, [{ left: 32, width: 4, label: '已折叠空闲 1 小时 42 分' }]);
  close(folded.positions.get('second').left, 36);
  close(folded.positions.get('second').width, 64);
  assert.equal(folded.note, '活跃耗时 90.00 s · 已折叠 1 处空闲（总跨度 6210.00 s）');
  const real = buildTraceTimeline(rows, [], 'duration', false);
  assert.equal(real.gapCount, 1);
  assert.deepEqual(real.gaps, []);
  close(real.positions.get('second').left, 6150000 / 6210000 * 100);
  close(real.positions.get('first').width, 30000 / 6210000 * 100);
});

test('多段总空闲带受 24% 限制，短段保留宽度，事件不越界', () => {
  const rows = [row('long', 0, 50000), ...Array.from({ length: 8 }, (_, i) => row(String(i), 80000 + 30000 * i, 1))];
  const timeline = buildTraceTimeline(rows, [], 'duration');
  close(timeline.gaps.reduce((sum, gap) => sum + gap.width, 0), 24);
  close(timeline.positions.get('0').width, 2);
  const last = timeline.positions.get('7');
  close(last.left + last.width, 100);
});

test('只记录起点的事件保留点标记，全缺开始时间不绘制坐标', () => {
  const partial = [row('a', 0), row('b', 20000)];
  const timeline = buildTraceTimeline(partial, [], 'duration');
  assert.equal(timeline.gaps.length, 1);
  assert.equal(timeline.positions.get('a').point, true);
  close(timeline.positions.get('b').left, 52);
  assert.equal(timeline.positions.get('b').width, 0);
  const missing = buildTraceTimeline([row('unknown', null, 500)], [], 'duration');
  assert.match(missing.note, /开始时间未记录/);
  assert.equal(missing.positions.size, 0);
  assert.equal(missing.gapCount, 0);
});

test('密集多段轨迹仍为长事件保留按时长分配的空间', () => {
  const rows = [row('long', 0, 60000), ...Array.from({ length: 60 }, (_, i) => row(String(i), 80000 + 20000 * i, 1))];
  const { positions } = buildTraceTimeline(rows, [], 'duration');
  assert.ok(positions.get('long').width > 35);
  assert.ok(positions.get('0').width > 0);
  const last = positions.get('59');
  close(last.left + last.width, 100);
});

test('折叠开关不影响轮次与调用次序，保留无时间戳的事件', () => {
  const rows = [row('x', null), row('y', 100), row('z', 80000)];
  const groups = [{ key: 'g', rows }];
  for (const view of ['calls', 'rounds']) {
    const timeline = buildTraceTimeline(rows, groups, view);
    assert.deepEqual(timeline, buildTraceTimeline(rows, groups, view, false));
    assert.equal(timeline.positions.size, 3);
    assert.equal(timeline.gapCount, 0);
  }
});

test('新旧报告阶段与来源展示为相同名称', () => {
  assert.equal(normalizeTraceTitle('06 提示函撰写'), '06 风险报告撰写');
  assert.equal(normalizeTraceTitle('06 风险报告撰写'), '06 风险报告撰写');
  assert.equal(normalizeTraceTitle('提示函撰写 · 柯城'), '风险报告 · 柯城');
  assert.equal(normalizeTraceTitle('风险报告 · 柯城'), '风险报告 · 柯城');
  assert.equal(normalizeTraceTitle('05 行社归并'), '05 行社归并');
});

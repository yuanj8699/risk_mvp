/** 根据归档时间绘制轨迹。折叠只压缩超过 15 秒的空闲，不改写事件的起止或次序。 */
import type { TraceGroup, TraceRow } from './trace-panel';

export type TimelineView = 'duration' | 'rounds' | 'calls';
interface TimelinePosition { left: number; width: number; point: boolean }
interface Gap { left: number; width: number; label: string }
interface Timeline {
  positions: Map<string, TimelinePosition>;
  ticks: { left: number; label: string }[];
  gaps: Gap[];
  gapCount: number;
  note: string;
}
interface Interval { key: string; start: number; end: number }
interface Segment { start: number; end: number; intervals: Interval[] }

export const recordedNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
export const traceNumber = (value: unknown) => recordedNumber(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '未记录';
export const traceDuration = (value: unknown) => recordedNumber(value) ? value < 1000 ? `${traceNumber(value)} ms` : `${traceNumber(value / 1000)} s` : '未记录';

function idleLabel(milliseconds: number) {
  const seconds = Math.round(milliseconds / 1000);
  const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds / 60) % 60;
  return '已折叠空闲 ' + [hours && hours + ' 小时', minutes && minutes + ' 分', seconds % 60 && seconds % 60 + ' 秒'].filter(Boolean).join(' ');
}

function mergeIntervals(intervals: Interval[]): Segment[] {
  const segments: Segment[] = [];
  for (const item of intervals.sort((a, b) => a.start - b.start)) {
    const last = segments.at(-1);
    if (last && item.start - last.end <= 15_000) {
      last.end = Math.max(last.end, item.end);
      last.intervals.push(item);
    } else {
      segments.push({ start: item.start, end: item.end, intervals: [item] });
    }
  }
  return segments;
}

// 给极短段（含单个时间点）预留宽度后，其余各段保持真实耗时比例。
function segmentWidths(segments: Segment[], available: number, activeSpan: number) {
  // 段数很多时保底宽度最多用掉一半预算，长段仍能按实际时长展开。
  const minimum = Math.min(2, available / (2 * segments.length));
  const widths = new Array<number>(segments.length);
  const ordered = segments.map(({ start, end }, index) => ({ index, duration: end - start })).sort((a, b) => a.duration - b.duration);
  let remaining = available, time = activeSpan;
  for (const [i, { index, duration }] of ordered.entries()) {
    const share = time ? remaining * duration / time : remaining / (ordered.length - i);
    widths[index] = Math.max(minimum, share);
    remaining -= widths[index];
    time -= duration;
  }
  return widths;
}

function durationTimeline(rows: TraceRow[], result: Timeline, fold: boolean) {
  const intervals: Interval[] = [];
  let missing = 0;
  for (const { key, entry } of rows) {
    const start = Date.parse(entry.started_at ?? '');
    const duration = recordedNumber(entry.duration_ms) ? Math.max(0, entry.duration_ms) : 0;
    if (!Number.isFinite(start) || !recordedNumber(entry.duration_ms)) missing++;
    if (Number.isFinite(start)) intervals.push({ key, start, end: start + duration });
  }
  const segments = mergeIntervals(intervals);
  if (!segments.length) {
    result.note = '开始时间未记录，无法按时长定位。可切换轮次或调用视图。';
    return;
  }
  const origin = segments[0].start, span = segments[segments.length - 1].end - origin;
  const activeSpan = segments.reduce((sum, s) => sum + s.end - s.start, 0);
  result.gapCount = segments.length - 1;
  const folded = fold && result.gapCount > 0;
  const gapWidth = folded ? Math.min(4, 24 / result.gapCount) : 0;
  const widths = folded ? segmentWidths(segments, 100 - gapWidth * result.gapCount, activeSpan) : [];
  let cursor = 0;
  segments.forEach((segment, index) => {
    const scale = folded ? widths[index] / (segment.end - segment.start || 1) : 100 / (span || 1);
    const left = folded ? cursor : (segment.start - origin) * scale;
    for (const item of segment.intervals) {
      result.positions.set(item.key, {
        left: left + (item.start - segment.start) * scale,
        width: (item.end - item.start) * scale,
        point: item.end === item.start,
      });
    }
    if (folded && index < result.gapCount) {
      cursor += widths[index];
      result.gaps.push({ left: cursor, width: gapWidth, label: idleLabel(segments[index + 1].start - segment.end) });
      cursor += gapWidth;
    }
  });
  if (!folded) result.ticks = span ? [0, .25, .5, .75, 1].map(part => ({ left: part * 100, label: traceDuration(span * part) })) : [{ left: 0, label: '已记录起点' }];
  result.note = `活跃耗时 ${(activeSpan / 1000).toFixed(2)} s · 已折叠 ${result.gaps.length} 处空闲（总跨度 ${(span / 1000).toFixed(2)} s）`;
  if (missing) result.note += ` · ${missing} 项时间信息不全`;
}

/** 时长以已记录的起点定位，其余视图沿用轮次、调用顺序的等距坐标。 */
export function buildTraceTimeline(rows: TraceRow[], groups: TraceGroup[], view: TimelineView, foldIdleGaps = true): Timeline {
  const result: Timeline = { positions: new Map(), ticks: [], gaps: [], gapCount: 0, note: '' };
  const { positions } = result;
  if (!rows.length) result.note = '未记录轨迹。';
  else if (view === 'duration') durationTimeline(rows, result, foldIdleGaps);
  else if (view === 'rounds') {
    groups.forEach((group, groupIndex) => {
      group.rows.forEach((row, index) => positions.set(row.key, {
        left: (groupIndex + .08 + index / group.rows.length * .84) / groups.length * 100,
        width: .65 / group.rows.length / groups.length * 100, point: false,
      }));
      result.ticks.push({ left: (groupIndex + .5) / groups.length * 100,
        label: group.request?.entry.request === undefined ? '未记录轮次' : `第 ${group.request.entry.request} 轮` });
    });
    result.note = '按轮次等距排列，不代表耗时。';
  } else {
    rows.forEach((row, index) => positions.set(row.key, { left: (index + .08) / rows.length * 100, width: .75 / rows.length * 100, point: false }));
    const indexes = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])];
    result.ticks = indexes.map(index => ({ left: (index + .5) / rows.length * 100, label: `第 ${index + 1} 项` }));
    result.note = '按调用顺序排列，不代表耗时。';
  }
  if (result.ticks.length > 7) result.ticks = result.ticks.filter((_, i, all) => i % Math.ceil(all.length / 6) === 0 || i === all.length - 1);
  return result;
}

/** 旧任务归档可能保存了已翻译的标题，展示时统一用当前报告名称。 */
export function normalizeTraceTitle(title: string) {
  return title.replace(/^06\s*提示函撰写(?=$|\s*·)/, '06 风险报告撰写').replace(/^提示函撰写(?=$|\s*·)/, '风险报告');
}

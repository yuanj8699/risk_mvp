/** 共用运行轨迹：展示宿主已记录的调用与模型公开信息，不从旧记录推算缺失字段。 */
import { computed, defineComponent, nextTick, ref, type PropType } from 'vue';

export interface TraceError { code?: string; message?: string }
export interface AgentTrace {
  event: string; id?: string; parent_id?: string; case_key?: string; phase?: string;
  request?: number; tool?: string; parent_tool?: string; transport?: string;
  started_at?: string; duration_ms?: number; status?: string;
  arguments?: unknown; evidence_ids?: string[]; execution_id?: string; exit_code?: number;
  error?: TraceError; response_bytes?: number;
  recoverable?: boolean; recovery_attempt?: number; recovery_limit?: number;
  recovery_status?: 'retry_available' | 'exhausted' | 'recovered';
  model_provider?: string; model_id?: string;
  request_summary?: { message_count?: number; tool_names?: string[]; payload_bytes?: number };
  reasoning_content?: string | null; reasoning_source?: string | null; reasoning_truncated?: boolean;
  usage?: { input_tokens?: number | null; output_tokens?: number | null; reasoning_tokens?: number | null;
    cache_read_tokens?: number | null; cache_write_tokens?: number | null; total_tokens?: number | null; cost_total?: number | null } | null;
  retry_count?: number | null; retry_observation?: string;
  http_attempts?: { attempt: number; started_at?: string; duration_ms?: number; status_code?: number | null; error?: TraceError }[];
}
export interface TraceRuntime {
  mode?: string; llm_connected?: boolean; provider?: string; model?: string;
  [key: string]: unknown;
}
export interface TraceDiagnostics { trace?: AgentTrace[]; runtime?: TraceRuntime; truncated?: boolean }
interface TraceRow { entry: AgentTrace; key: string; depth: number; index: number; parent?: TraceRow }
interface TraceGroup { key: string; request?: TraceRow; rows: TraceRow[] }
type TimelineView = 'duration' | 'rounds' | 'calls';
interface TimelinePosition { left: number; width: number; point: boolean }

/** 完成事件可能晚于子调用入列；通过真实parent_id恢复父子顺序，旧记录保持原始顺序。 */
export function orderTrace(trace: AgentTrace[]): TraceRow[] {
  const rows = trace.map((entry, index): TraceRow => ({ entry, index, key: `${entry.case_key ?? ''}:${entry.id ?? `legacy-${index}`}`, depth: 0 }));
  const byId = new Map(rows.filter(row => row.entry.id).map(row => [row.key, row]));
  for (const row of rows) {
    const parent = row.entry.parent_id && byId.get(`${row.entry.case_key ?? ''}:${row.entry.parent_id}`);
    if (parent && parent !== row) row.parent = parent;
  }
  const chronological = (a: TraceRow, b: TraceRow) => {
    const aTime = Date.parse(a.entry.started_at ?? ''), bTime = Date.parse(b.entry.started_at ?? '');
    return Number.isFinite(aTime) && Number.isFinite(bTime) && aTime !== bTime ? aTime - bTime : a.index - b.index;
  };
  const children = new Map<TraceRow, TraceRow[]>();
  for (const row of rows) if (row.parent) children.set(row.parent, [...(children.get(row.parent) ?? []), row]);
  const result: TraceRow[] = [], visited = new Set<TraceRow>();
  const append = (row: TraceRow, depth: number) => {
    if (visited.has(row)) return;
    visited.add(row);
    row.depth = depth;
    result.push(row);
    for (const child of (children.get(row) ?? []).sort(chronological)) append(child, depth + (row.entry.event === 'tool' ? 1 : 0));
  };
  for (const row of rows.filter(row => !row.parent).sort(chronological)) append(row, 0);
  // 不完整或循环的旧关系也保留记录，避免界面静默遗漏。
  for (const row of rows) append(row, 0);
  return result;
}

/** 仅用已记录父关系归入模型轮次；缺少关联的旧工具记录不猜测轮次。 */
export function groupTrace(rows: TraceRow[]): TraceGroup[] {
  const groups = new Map<string, TraceGroup>();
  let previous: TraceGroup | undefined;
  for (const row of rows) {
    let request: TraceRow | undefined = row;
    const visited = new Set<TraceRow>();
    while (request && request.entry.event !== 'request' && !visited.has(request)) {
      visited.add(request);
      request = request.parent;
    }
    if (request?.entry.event !== 'request') request = undefined;
    const key = request?.key ?? (previous && !previous.request && previous.rows[0].entry.case_key === row.entry.case_key
      ? previous.key : `unassigned:${row.key}`);
    if (!groups.has(key)) groups.set(key, { key, request, rows: [] });
    previous = groups.get(key)!;
    previous.rows.push(row);
  }
  return [...groups.values()];
}

export function matchesTraceSearch(entry: AgentTrace, query: string): boolean {
  const terms = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  const haystack = JSON.stringify([entry.tool, entry.arguments, entry.error, entry.evidence_ids,
    entry.model_id, entry.case_key, entry.execution_id, entry.phase]).toLocaleLowerCase();
  return terms.every(term => haystack.includes(term));
}

/** 三种坐标分别表达真实时间、已记录轮次和调用次序；缺失时间不生成虚假时段。 */
export function buildTraceTimeline(rows: TraceRow[], groups: TraceGroup[], view: TimelineView) {
  const positions = new Map<string, TimelinePosition>();
  let ticks: { left: number; label: string }[] = [];
  let note = '';
  if (!rows.length) return { positions, ticks, note: '未记录轨迹。' };
  if (view === 'duration') {
    const timed = rows.map(row => ({ row, start: Date.parse(row.entry.started_at ?? '') })).filter(item => Number.isFinite(item.start));
    if (!timed.length) return { positions, ticks, note: '开始时间未记录，无法按时长定位。可切换轮次或调用视图。' };
    const origin = Math.min(...timed.map(item => item.start));
    const end = Math.max(...timed.map(item => item.start + (recordedNumber(item.row.entry.duration_ms) ? Math.max(0, item.row.entry.duration_ms) : 0)));
    const span = end - origin;
    for (const { row, start } of timed) positions.set(row.key, {
      left: (start - origin) / (span || 1) * 100,
      width: recordedNumber(row.entry.duration_ms) ? Math.max(0, row.entry.duration_ms) / (span || 1) * 100 : 0,
      point: !recordedNumber(row.entry.duration_ms) || row.entry.duration_ms === 0,
    });
    ticks = span ? [0, .25, .5, .75, 1].map(part => ({ left: part * 100, label: traceDuration(span * part) })) : [{ left: 0, label: '已记录起点' }];
    const missing = rows.filter(row => !Number.isFinite(Date.parse(row.entry.started_at ?? '')) || !recordedNumber(row.entry.duration_ms)).length;
    note = missing ? `${missing} 项时间信息不完整；缺少时长的记录仅显示起点。` : '按已记录开始时间与耗时绘制。';
  } else if (view === 'rounds') {
    groups.forEach((group, groupIndex) => {
      group.rows.forEach((row, index) => positions.set(row.key, {
        left: (groupIndex + .08 + index / group.rows.length * .84) / groups.length * 100,
        width: .65 / group.rows.length / groups.length * 100, point: false,
      }));
      ticks.push({ left: (groupIndex + .5) / groups.length * 100,
        label: group.request?.entry.request === undefined ? '未记录轮次' : `第 ${group.request.entry.request} 轮` });
    });
    note = '按轮次分区，轮内调用等距排列，不表示耗时比例。';
  } else {
    rows.forEach((row, index) => positions.set(row.key, { left: (index + .08) / rows.length * 100, width: .75 / rows.length * 100, point: false }));
    const indexes = [...new Set([0, Math.floor((rows.length - 1) / 2), rows.length - 1])];
    ticks = indexes.map(index => ({ left: (index + .5) / rows.length * 100, label: `第 ${index + 1} 项` }));
    note = '按已保存调用次序等距排列，不表示耗时比例。';
  }
  if (ticks.length > 7) ticks = ticks.filter((_, index) => index % Math.ceil(ticks.length / 6) === 0 || index === ticks.length - 1);
  return { positions, ticks, note };
}

const recordedNumber = (value: unknown): value is number => typeof value === 'number' && Number.isFinite(value);
export const traceNumber = (value: unknown) => recordedNumber(value) ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 }) : '未记录';
export const traceDuration = (value: unknown) => recordedNumber(value) ? value < 1000 ? `${traceNumber(value)} ms` : `${traceNumber(value / 1000)} s` : '未记录';
export const traceBytes = (value: unknown) => recordedNumber(value) ? value < 1024 ? `${traceNumber(value)} B` : `${traceNumber(value / 1024)} KiB` : '未记录';

export default defineComponent({
  props: {
    trace: { type: Array as PropType<AgentTrace[]>, default: () => [] },
    runtime: { type: Object as PropType<TraceRuntime>, default: undefined },
    mode: { type: String, default: '' }, title: { type: String, default: '运行轨迹' },
    error: { type: String, default: '' }, truncated: { type: Boolean, default: false },
  },
  setup(props) {
    const filter = ref('all');
    const search = ref('');
    const view = ref<TimelineView>('duration');
    const selectedKey = ref('');
    const entryElements = new Map<string, HTMLDetailsElement>();
    const filters = [{ id: 'all', label: '全部' }, { id: 'tool', label: '工具' }, { id: 'request', label: '模型' }, { id: 'failed', label: '失败' }];
    const views = [{ id: 'duration', label: '时长' }, { id: 'rounds', label: '轮次' }, { id: 'calls', label: '调用' }];
    const lanes = [{ id: 'input', label: '请求输入' }, { id: 'request', label: '模型' }, { id: 'tool', label: '工具' }];
    const failed = (entry: AgentTrace) => entry.status === 'error' || entry.status === 'cancelled' || Boolean(entry.error) || (recordedNumber(entry.exit_code) && entry.exit_code !== 0);
    const rows = computed(() => orderTrace(props.trace));
    const groups = computed(() => groupTrace(rows.value));
    const rowNumbers = computed(() => new Map(rows.value.map((row, index) => [row.key, index + 1])));
    const rowGroups = computed(() => new Map(groups.value.flatMap(group => group.rows.map(row => [row.key, group] as const))));
    const visibleRows = computed(() => rows.value.filter(row => (filter.value === 'all' || (filter.value === 'failed' ? failed(row.entry) : row.entry.event === filter.value)) && matchesTraceSearch(row.entry, search.value)));
    const visibleGroups = computed(() => {
      const visible = new Set(visibleRows.value.map(row => row.key));
      return groups.value.map(group => ({ ...group, rows: group.rows.filter(row => visible.has(row.key)) })).filter(group => group.rows.length);
    });
    const timeline = computed(() => buildTraceTimeline(rows.value, groups.value, view.value));
    const marks = (lane: string) => visibleRows.value.filter(row => timeline.value.positions.has(row.key) && row.entry.event === (lane === 'input' ? 'request' : lane));
    const markerStyle = (row: TraceRow, lane: string) => {
      const point = timeline.value.positions.get(row.key)!;
      return { left: `${point.left}%`, width: lane === 'input' ? '4px' : `${point.width}%`,
        transform: point.left >= 99.9 ? 'translateX(-100%)' : undefined,
        top: lane === 'tool' && row.depth > 0 ? '10px' : '4px' };
    };
    const demo = computed(() => props.mode === 'pi-demo' || props.runtime?.mode === 'demo');
    const modeLabel = computed(() => demo.value ? '离线演示 · 未调用真实模型' : props.runtime?.llm_connected === false || props.mode === 'template' ? '本记录未调用真实模型' : props.mode === 'pi-model' || props.runtime?.llm_connected === true ? '模型模式' : '模型连接状态未记录');
    const count = (event: string) => props.trace.filter(entry => entry.event === event).length;
    const entryTitle = (entry: AgentTrace) => entry.event === 'request' ? entry.model_id || props.runtime?.model || '模型请求' : entry.tool || '工具调用';
    const status = (entry: AgentTrace) => ({ ok: '成功', error: '失败', cancelled: '已取消', running: '进行中' }[entry.status ?? ''] ?? (failed(entry) ? '失败' : '状态未记录'));
    const startTime = (value?: string) => {
      if (!value) return '未记录';
      const date = new Date(value);
      return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
    };
    const formatted = (value: unknown) => value === undefined ? '未记录' : typeof value === 'string' ? value : JSON.stringify(value, null, 2);
    const errorText = (value?: TraceError) => value ? [value.code, value.message].filter(Boolean).join(' · ') || '错误详情未记录' : '未记录';
    const retryText = (entry: AgentTrace) => entry.retry_observation === 'not_applicable' ? '不适用（离线演示）' : traceNumber(entry.retry_count);
    const recoveryStatus = (value?: string) => ({ retry_available: '仍可修正后重试', exhausted: '已达到尝试上限', recovered: '恢复成功' }[value ?? ''] ?? '恢复状态未记录');
    const phaseLabel = (value?: string) => ({ select: '指标选择', explain: '事件分析', explore: '自主分析' }[value ?? ''] ?? value);
    const roundTitle = (group: TraceGroup) => group.request?.entry.request === undefined ? '轮次未记录' : `第 ${group.request.entry.request} 轮`;
    const markerLabel = (row: TraceRow) => `${roundTitle(rowGroups.value.get(row.key)!)}，日志第 ${rowNumbers.value.get(row.key)} 项，${entryTitle(row.entry)}，耗时 ${traceDuration(row.entry.duration_ms)}`;
    const rowSummary = (entry: AgentTrace) => {
      if (entry.event === 'request') {
        const reasoning = entry.reasoning_content?.replace(/\s+/g, ' ').trim();
        return reasoning ? reasoning.slice(0, 150) + (reasoning.length > 150 ? '…' : '') : `消息 ${traceNumber(entry.request_summary?.message_count)} · tokens ${traceNumber(entry.usage?.total_tokens)}`;
      }
      if (entry.arguments === undefined) return '参数未记录';
      const text = JSON.stringify(entry.arguments).replace(/\s+/g, ' ');
      return text.length > 150 ? text.slice(0, 150) + '…' : text;
    };
    const registerRow = (key: string, element: unknown) => {
      if (element instanceof HTMLDetailsElement) entryElements.set(key, element);
      else entryElements.delete(key);
    };
    const locate = async (row: TraceRow) => {
      selectedKey.value = row.key;
      await nextTick();
      const element = entryElements.get(row.key);
      if (!element) return;
      element.open = true;
      element.scrollIntoView({ block: 'nearest', behavior: 'auto' });
      element.querySelector('summary')?.focus({ preventScroll: true });
    };
    return { filter, filters, search, view, views, lanes, rows, visibleRows, visibleGroups, timeline, marks, markerStyle, markerLabel, selectedKey, registerRow, locate,
      modeLabel, demo, failed, count, entryTitle, rowSummary, roundTitle, status, startTime, formatted, errorText, retryText, recoveryStatus, phaseLabel, traceNumber, traceDuration, traceBytes };
  },
  template: `
    <section class="trace-panel" :aria-label="title">
      <header class="trace-heading"><h3>{{ title }}</h3><span>{{ modeLabel }}</span></header>
      <p v-if="error" class="trace-failure" role="status">{{ error }}</p>
      <p v-if="truncated || runtime?.trace_truncated===true" class="trace-note">轨迹记录达到保存上限，以下仅为已保存部分。</p>
      <template v-if="trace.length">
        <div class="trace-toolbar"><div class="trace-views" role="group" aria-label="轨迹坐标视图"><button v-for="item in views" :key="item.id" type="button" :class="{active:view===item.id}" :aria-pressed="view===item.id" @click="view=item.id">{{ item.label }}<span>{{ item.id==='duration' ? traceDuration(runtime?.duration_ms) : item.id==='rounds' ? count('request') : count('tool') }}</span></button></div><label class="trace-search"><span>搜索</span><input v-model="search" type="search" placeholder="工具、参数、错误、证据" aria-label="搜索运行轨迹" /></label></div>
        <div class="trace-timeline" aria-label="请求输入、模型与工具时间泳道">
          <div v-for="lane in lanes" :key="lane.id" class="trace-lane"><span class="trace-lane-label"><i :class="lane.id"></i>{{ lane.label }}</span><div class="trace-lane-track"><button v-for="row in marks(lane.id)" :key="row.key" type="button" :class="['trace-lane-mark',lane.id,{'failed':lane.id!=='input' && failed(row.entry),'point':lane.id==='input' || timeline.positions.get(row.key)?.point}]" :style="markerStyle(row,lane.id)" :title="markerLabel(row)" :aria-label="'定位'+markerLabel(row)" @click="locate(row)"></button></div></div>
          <div class="trace-axis"><span v-for="(tick,index) in timeline.ticks" :key="index" :style="{left:tick.left+'%'}">{{ tick.label }}</span></div>
        </div>
        <p class="trace-timeline-note">{{ timeline.note }} 输入泳道仅标记模型请求开始。</p>
        <div class="trace-controls"><div class="trace-filters" role="group" aria-label="筛选轨迹"><button v-for="item in filters" :key="item.id" type="button" :class="{active:filter===item.id}" :aria-pressed="filter===item.id" @click="filter=item.id">{{ item.label }}</button></div><span>显示 {{ visibleRows.length }} / {{ trace.length }} 项</span></div>
        <div v-if="visibleGroups.length" class="trace-rounds">
          <section v-for="group in visibleGroups" :key="group.key" class="trace-round">
            <header class="trace-round-heading"><strong>{{ roundTitle(group) }}</strong><span v-if="group.rows[0].entry.case_key">行社 {{ group.rows[0].entry.case_key }}</span><span v-if="group.rows[0].entry.phase">{{ phaseLabel(group.rows[0].entry.phase) }}</span><small>{{ group.rows.length }} 项</small></header>
            <ol class="trace-list">
              <li v-for="row in group.rows" :key="row.key" :class="['trace-item', {'trace-child':row.depth>0, 'trace-item-failed':failed(row.entry), 'trace-item-selected':selectedKey===row.key}]">
                <details :ref="element=>registerRow(row.key,element)">
                  <summary class="trace-entry-summary"><span :class="['trace-kind',failed(row.entry)?'failed':row.entry.event]">{{ failed(row.entry)?'失败':row.entry.event==='request'?'模型':'工具' }}</span><span class="trace-entry-main"><span v-if="row.depth>0" class="trace-nested" aria-hidden="true">↳</span><strong>{{ entryTitle(row.entry) }}</strong><span v-if="row.entry.recovery_attempt!==undefined" class="trace-recovery-count" :title="'工具恢复尝试 '+row.entry.recovery_attempt+'/'+row.entry.recovery_limit">{{ row.entry.recovery_attempt }}/{{ row.entry.recovery_limit }}</span><code>{{ rowSummary(row.entry) }}</code></span><span class="trace-entry-time">{{ traceDuration(row.entry.duration_ms) }}</span><span class="trace-row-bytes" :title="row.entry.event==='tool'?'工具返回体积':'请求体积'">{{ row.entry.event==='tool' ? traceBytes(row.entry.response_bytes) : '请求 '+traceBytes(row.entry.request_summary?.payload_bytes) }}</span></summary>
              <div class="trace-entry-body">
                <dl class="trace-metadata"><div><dt>调用状态</dt><dd>{{ status(row.entry) }}</dd></div><div><dt>开始时间</dt><dd>{{ startTime(row.entry.started_at) }}</dd></div><div><dt>耗时</dt><dd>{{ traceDuration(row.entry.duration_ms) }}</dd></div><div v-if="row.entry.event==='tool'"><dt>工具返回体积</dt><dd>{{ traceBytes(row.entry.response_bytes) }}</dd></div><div v-if="row.entry.execution_id"><dt>执行记录</dt><dd><code>{{ row.entry.execution_id }}</code></dd></div><div v-if="row.entry.phase"><dt>阶段</dt><dd>{{ phaseLabel(row.entry.phase) }}</dd></div><div v-if="row.parent"><dt>上层调用</dt><dd>{{ entryTitle(row.parent.entry) }}</dd></div></dl>
                <section v-if="row.entry.recovery_attempt!==undefined || row.entry.recovery_status" class="trace-recovery"><h4>工具失败恢复</h4><p>第 {{ traceNumber(row.entry.recovery_attempt) }} 次尝试 / 上限 {{ traceNumber(row.entry.recovery_limit) }} 次 · {{ recoveryStatus(row.entry.recovery_status) }}</p><small>工具恢复尝试与 HTTP 网络重试分别记录。</small></section>
                <template v-if="row.entry.event==='request'">
                  <dl class="trace-metadata"><div><dt>模型服务</dt><dd>{{ row.entry.model_provider || runtime?.provider || '未记录' }}</dd></div><div><dt>输入 tokens</dt><dd>{{ traceNumber(row.entry.usage?.input_tokens) }}</dd></div><div><dt>输出 tokens</dt><dd>{{ traceNumber(row.entry.usage?.output_tokens) }}</dd></div><div><dt>推理 tokens</dt><dd>{{ traceNumber(row.entry.usage?.reasoning_tokens) }}</dd></div><div><dt>总 tokens</dt><dd>{{ traceNumber(row.entry.usage?.total_tokens) }}</dd></div><div><dt>缓存读取 / 写入</dt><dd>{{ traceNumber(row.entry.usage?.cache_read_tokens) }} / {{ traceNumber(row.entry.usage?.cache_write_tokens) }}</dd></div><div><dt>HTTP 真实重试次数</dt><dd>{{ retryText(row.entry) }}</dd></div></dl>
                  <details class="trace-detail"><summary>请求摘要</summary><dl class="trace-metadata"><div><dt>消息条数</dt><dd>{{ traceNumber(row.entry.request_summary?.message_count) }}</dd></div><div><dt>请求体积</dt><dd>{{ traceBytes(row.entry.request_summary?.payload_bytes) }}</dd></div></dl><p>可用工具：{{ row.entry.request_summary?.tool_names?.join('、') || '未记录' }}</p></details>
                  <details class="trace-detail"><summary>公开推理内容</summary><p class="trace-note">仅展示提供方返回的公开 reasoning / thinking 内容。</p><pre v-if="row.entry.reasoning_content">{{ row.entry.reasoning_content }}</pre><p v-else>{{ demo ? '离线演示未调用真实模型，未提供公开推理。' : '提供方未提供公开推理内容。' }}</p><p v-if="row.entry.reasoning_truncated" class="trace-note">公开推理内容达到记录上限，已截断。</p></details>
                  <details class="trace-detail"><summary>HTTP 请求尝试与重试</summary><p v-if="!row.entry.http_attempts?.length">{{ row.entry.retry_observation==='not_applicable' ? '离线演示不产生真实网络请求。' : '未记录网络请求尝试；模型调用轮次不等于重试次数。' }}</p><ol v-else class="trace-attempts"><li v-for="attempt in row.entry.http_attempts" :key="attempt.attempt"><strong>尝试 {{ attempt.attempt }}</strong><span>{{ startTime(attempt.started_at) }} · {{ traceDuration(attempt.duration_ms) }} · HTTP {{ traceNumber(attempt.status_code) }}</span><p v-if="attempt.error" class="trace-failure">{{ errorText(attempt.error) }}</p></li></ol></details>
                </template>
                <template v-else><details class="trace-detail"><summary>调用参数</summary><pre>{{ formatted(row.entry.arguments) }}</pre></details><details class="trace-detail"><summary>证据引用{{ row.entry.evidence_ids ? ' · '+row.entry.evidence_ids.length+' 条' : '' }}</summary><ul v-if="row.entry.evidence_ids?.length" class="trace-evidence"><li v-for="id in row.entry.evidence_ids" :key="id"><code>{{ id }}</code></li></ul><p v-else>{{ row.entry.evidence_ids ? '本次未记录证据引用。' : '未记录' }}</p></details></template>
                <details v-if="failed(row.entry)" class="trace-detail" open><summary>失败原因</summary><pre>{{ errorText(row.entry.error) }}</pre><p v-if="row.entry.exit_code!==undefined">退出码：{{ row.entry.exit_code }}</p></details>
                  </div>
                </details>
              </li>
            </ol>
          </section>
        </div>
        <p v-else class="trace-empty">没有匹配记录。可调整搜索词或筛选条件。</p>
      </template>
      <p v-else class="trace-empty">这条记录未保存可展示的运行轨迹。旧记录缺失的耗时、tokens 和重试信息不会补算。</p>
    </section>
  `,
});

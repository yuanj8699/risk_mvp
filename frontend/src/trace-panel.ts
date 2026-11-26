/** 共用运行轨迹：展示宿主已记录的调用与模型公开信息，不从旧记录推算缺失字段。 */
import { computed, defineComponent, nextTick, ref, type PropType } from 'vue';
import { Input } from 'tdesign-vue-next/es/input';
import SearchIcon from 'tdesign-icons-vue-next/esm/components/search';
import { smartqStatusLabel, type useConversation } from './conversation';
import type { RiskScan } from './scan-types';
import { buildTraceTimeline, normalizeTraceTitle, recordedNumber, traceNumber, traceDuration, type TimelineView } from './trace-timeline';
export { buildTraceTimeline, traceNumber, traceDuration } from './trace-timeline';
import './trace-panel.css';

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
export interface TraceRow { entry: AgentTrace; key: string; depth: number; index: number; parent?: TraceRow }
export interface TraceGroup { key: string; request?: TraceRow; rows: TraceRow[] }

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
  if (!terms.length) return true;
  const haystack = JSON.stringify(entry).toLocaleLowerCase();
  return terms.every(term => haystack.includes(term));
}

export const traceBytes = (value: unknown) => recordedNumber(value) ? value < 1024 ? `${traceNumber(value)} B` : `${traceNumber(value / 1024)} KiB` : '未记录';

type Turn = ReturnType<typeof useConversation>['turns']['value'][number];
type ArchiveEntry = AgentTrace & { caption?: string; summary?: string };
interface LogGroup { title: string; rows: TraceRow[] }

const phaseNames: Record<string, string> = {
  draft_risk_letter: '06 风险报告撰写',
  select: '指标选择', explain: '事件分析', explore: '自主分析', follow_up: '任务追问',
  synthesize_risks: '05 行社归并', dimensions: '01 内置风险面', mapping: '02 指标口径匹配',
  fetch: '03 指标取数', validation: '04 源表口径校验',
};
const roleNames: Record<string, string> = {
  context: '上下文', user: '用户', request: '助手', assistant: '助手', tool: '工具', smartq: '工具',
};
const failed = (entry: AgentTrace) => ['error', 'cancelled', 'failed'].includes(entry.status ?? '') ||
  Boolean(entry.error) || (recordedNumber(entry.exit_code) && entry.exit_code !== 0);
const fromSmartQ = (entry: AgentTrace) => entry.event === 'smartq' || /smartq/i.test(entry.tool ?? '') ||
  Boolean(entry.evidence_ids?.some(id => id.startsWith('smartq:')));
const compact = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value);
const format = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
const statusLabels: Record<string, string> = {
  ok: '成功', error: '失败', failed: '失败', cancelled: '已取消', running: '进行中',
};
const role = (entry: AgentTrace) => roleNames[entry.event] || entry.event;
const heading = (entry: ArchiveEntry) => entry.caption || entry.tool || entry.model_id || role(entry);
function outcome(entry: ArchiveEntry) {
  if (entry.summary) return entry.summary;
  if (entry.error) return [entry.error.code, entry.error.message].filter(Boolean).join(' · ') || '失败';
  const parts = [statusLabels[entry.status ?? ''] || entry.status];
  if (entry.evidence_ids?.length) parts.push('引用 ' + entry.evidence_ids.length + ' 条证据');
  if (entry.usage?.total_tokens != null) parts.push(traceNumber(entry.usage.total_tokens) + ' tokens');
  if (recordedNumber(entry.response_bytes)) parts.push(traceBytes(entry.response_bytes));
  return parts.filter(Boolean).join(' · ') || '返回未记录';
}
function argumentSummary(entry: ArchiveEntry) {
  if (entry.caption) return '';
  const args = entry.arguments ?? entry.request_summary;
  return args === undefined ? '' : compact(args)?.replace(/\s+/g, ' ').slice(0, 110);
}

// 阶段 1–4 只有业务归档。展示已存快照与校验事实，不将其标成工具调用或推算耗时。
function archiveRow(id: string, event: string, phase: string, caption: string, summary: string, data?: unknown, status?: string): TraceRow {
  const entry: ArchiveEntry = { event, phase, caption, summary, arguments: data, status };
  return { key: 'archive:' + id, index: -1, depth: 0, entry };
}
function withArchive(trace: TraceRow[], scan?: RiskScan | null, turn?: Turn) {
  if (turn) {
    const rows = [archiveRow('question', 'user', 'follow_up', turn.question, '', turn.question), ...trace];
    const supplement = turn.smartq;
    if (supplement?.rewritten_question) {
      const row = archiveRow('lookup', 'smartq', 'follow_up', '小 Q 实时补查',
        smartqStatusLabel[supplement.status] + ' · ' + supplement.record_count + ' 条 · 口径仅供参考',
        supplement, supplement.status);
      const start = rows.findIndex(row => row.entry.id?.startsWith('supplement:'));
      rows.splice(start < 0 ? rows.length : start, 0, row);
    }
    if (turn.answer) rows.push(archiveRow('answer', 'assistant', 'follow_up', turn.answer,
      '引用 ' + turn.evidence.length + ' 条证据', { answer: turn.answer, evidence: turn.evidence }));
    return rows;
  }
  if (!scan) return trace;
  const rows: TraceRow[] = [];
  if (scan.selection) rows.push(archiveRow('scope', 'context', 'dimensions', '扫描范围',
    '已选 ' + scan.selection.dimension_ids.length + ' 个风险面 · ' + scan.selection.task_context.as_of,
    { dimension_ids: scan.selection.dimension_ids, task_context: scan.selection.task_context }));
  const validation = scan.validation_summary;
  if (scan.knowledge_mapping) {
    const metrics = Object.keys(scan.knowledge_mapping);
    const missing = new Set(validation?.missing_source ?? []);
    const matched = validation ? ' · 已匹配 ' + metrics.filter(id => !missing.has(id)).length + ' 项' : '';
    rows.push(archiveRow('mapping', 'context', 'mapping', '归档指标口径',
      metrics.length + ' 项定义' + matched, scan.knowledge_mapping));
  }
  if (validation) {
    rows.push(archiveRow('fetch', 'context', 'fetch', '归档取数结果',
      validation.covered_org_count + ' 家有观测 · ' + validation.available_indicator_count + ' 项可用指标',
      { visibility: validation.visibility, covered_org_count: validation.covered_org_count, available_indicator_count: validation.available_indicator_count }));
    rows.push(archiveRow('validation', 'context', 'validation', '源表口径校验归档',
      '缺数 ' + validation.missing_count + ' 项 · 单位拦截 ' + validation.unit_mismatch_count + ' 项', validation));
  }
  rows.push(...trace);
  if (scan.executive_summary) rows.push(archiveRow('synthesis', 'assistant', 'synthesize_risks', '归并综述', scan.executive_summary, scan.executive_summary));
  for (const draft of Object.values(scan.letter_drafts ?? {})) {
    rows.push(...orderTrace((draft.trace ?? []).map(entry => ({ ...entry, phase: 'draft_risk_letter', case_key: draft.org_name }))));
  }
  return rows;
}

export default defineComponent({
  components: { TInput: Input, SearchIcon },
  props: {
    trace: { type: Array as PropType<AgentTrace[]>, default: () => [] },
    runtime: { type: Object as PropType<TraceRuntime>, default: undefined },
    scan: { type: Object as PropType<RiskScan | null>, default: undefined },
    turn: { type: Object as PropType<Turn>, default: undefined },
    round: { type: Number, default: 0 },
    mode: { type: String, default: '' }, title: { type: String, default: '执行轨迹' },
    error: { type: String, default: '' }, truncated: { type: Boolean, default: false },
  },
  setup(props) {
    const filter = ref('all'), search = ref(''), view = ref<TimelineView>('duration');
    const foldIdleGaps = ref(true);
    const selected = ref(''), expanded = ref('');
    const list = ref<HTMLElement | null>(null);
    const filters = [
      { id: 'all', label: '全部' }, { id: 'tool', label: '工具' }, { id: 'request', label: '模型' },
      { id: 'smartq', label: '仅看小Q' }, { id: 'failed', label: '失败' },
    ];
    const views = [{ id: 'duration', label: '时长' }, { id: 'rounds', label: '轮次' }, { id: 'calls', label: '调用' }] as const;
    const lanes = [{ id: 'input', label: '输入' }, { id: 'request', label: '模型' }, { id: 'tool', label: '工具' }];
    const demo = computed(() => props.mode === 'pi-demo' || props.runtime?.mode === 'demo');
    const rows = computed(() => withArchive(orderTrace(props.trace), props.scan, props.turn));
    const visible = computed(() => rows.value.filter(({ entry }) => {
      const include = filter.value === 'all' || (filter.value === 'failed' && failed(entry)) ||
        (filter.value === 'smartq' && fromSmartQ(entry)) ||
        (filter.value === 'tool' && role(entry) === '工具') || (filter.value === 'request' && entry.event === 'request');
      return include && matchesTraceSearch(entry, search.value);
    }));
    const groups = computed(() => {
      const result: LogGroup[] = [];
      for (const row of visible.value) {
        const { phase, case_key } = row.entry;
        const stage = normalizeTraceTitle(phaseNames[phase ?? ''] || phase || props.title);
        const title = props.turn ? (props.round ? '第 ' + props.round + ' 轮追问' : '本轮追问') :
          stage + (case_key && !stage.endsWith(' · ' + case_key) ? ' · ' + case_key : '');
        const last = result[result.length - 1];
        if (last?.title === title) last.rows.push(row);
        else result.push({ title, rows: [row] });
      }
      return result;
    });
    const timeline = computed(() => buildTraceTimeline(rows.value, groupTrace(rows.value), view.value, foldIdleGaps.value));
    const marks = (lane: string) => visible.value.filter(({ key, entry }) => {
      if (!timeline.value.positions.has(key)) return false;
      if (lane === 'input') return ['context', 'user', 'request'].includes(entry.event);
      if (lane === 'request') return role(entry) === '助手';
      return role(entry) === '工具';
    });
    const markStyle = (row: TraceRow, lane: string) => {
      const position = timeline.value.positions.get(row.key)!;
      return { left: position.left + '%', width: lane === 'request' && !position.point ? position.width + '%' : undefined,
        transform: position.left > 99.8 ? 'translateX(-100%)' : undefined };
    };
    const markerLabel = (row: TraceRow) => heading(row.entry) + (row.entry.duration_ms == null ? ' · 耗时未记录' : ' · ' + traceDuration(row.entry.duration_ms));
    const open = (key: string) => {
      selected.value = key;
      expanded.value = expanded.value === key ? '' : key;
    };
    const locate = async (row: TraceRow) => {
      expanded.value = selected.value = row.key;
      await nextTick();
      const button = list.value?.querySelector<HTMLButtonElement>('[data-event-key="' + CSS.escape(row.key) + '"]');
      button?.scrollIntoView({ block: 'nearest' });
      button?.focus({ preventScroll: true });
    };
    return { filter, search, view, foldIdleGaps, filters, views, lanes, list, selected, expanded, demo, rows, visible, groups,
      timeline, marks, markStyle, markerLabel, open, locate, role, heading, outcome, argumentSummary, failed, format, traceDuration, traceNumber };
  },
  template: `
    <section class="trace-workspace" :aria-label="title">
      <div class="trace-commandbar">
        <div class="trace-modes" role="group" aria-label="时间轴视图">
          <button type="button" v-for="item in views" :key="item.id" :aria-pressed="view===item.id" @click="view=item.id">{{ item.label }}</button>
        </div>
        <button v-if="view==='duration' && timeline.gapCount" type="button" class="trace-fold"
          :aria-pressed="foldIdleGaps" :title="foldIdleGaps ? '切换到真实跨度' : '折叠超过 15 秒的空闲'"
          @click="foldIdleGaps=!foldIdleGaps">{{ foldIdleGaps ? '折叠空闲' : '真实跨度' }}</button>
        <select v-model="filter" aria-label="筛选轨迹"><option v-for="item in filters" :key="item.id" :value="item.id">{{ item.label }}</option></select>
        <slot name="source"></slot>
        <div class="trace-search"><t-input size="small" v-model="search" aria-label="搜索轨迹" placeholder="搜索轨迹" clearable><template #prefix-icon><search-icon /></template></t-input></div>
      </div>
      <p v-if="error" class="trace-notice trace-warning" role="alert">{{ error }}</p>
      <p v-if="truncated || runtime?.trace_truncated" class="trace-notice trace-warning">轨迹达到归档上限，仅显示已保存事件。</p>
      <template v-if="rows.length">
        <div class="trace-map" aria-label="三轨时间轴">
          <div v-for="lane in lanes" :key="lane.id" class="trace-map-row">
            <span>{{ lane.label }}</span>
            <div class="trace-map-track">
              <span v-for="gap in timeline.gaps" :key="gap.left" class="trace-gap" :title="gap.label"
                :aria-label="gap.label" :style="{ left:gap.left+'%', width:gap.width+'%' }"></span>
              <button v-for="row in marks(lane.id)" :key="row.key" type="button"
              :class="['trace-mark', lane.id, { selected:selected===row.key }]" :style="markStyle(row,lane.id)"
              :title="markerLabel(row)" :aria-label="'定位'+markerLabel(row)" @click="locate(row)"></button></div>
          </div>
          <div class="trace-map-meta">
            <span v-if="demo">Pi 本地演示</span><span>{{ timeline.note }}</span>
            <span class="trace-count">{{ visible.length }} / {{ rows.length }} 项</span>
          </div>
        </div>
        <div ref="list" class="trace-events">
          <section v-for="group in groups" :key="group.rows[0].key" class="trace-phase">
            <h3>{{ group.title }}</h3>
            <div v-for="row in group.rows" :key="row.key" class="trace-event" :class="{chosen:selected===row.key}">
              <button type="button" class="trace-line" :data-event-key="row.key" :aria-expanded="expanded===row.key" @click="open(row.key)">
                <span class="trace-role" :class="'role-'+row.entry.event">{{ role(row.entry) }}</span>
                <span class="trace-description"><strong>{{ heading(row.entry) }}</strong>
                <span v-if="row.entry.event==='request' && row.entry.reasoning_content" class="trace-thought"><b>思考{{ row.entry.usage?.reasoning_tokens!=null ? ' · '+row.entry.usage.reasoning_tokens+' tokens' : '' }}</b> {{ row.entry.reasoning_content.trim().slice(0,120) }}</span>
                <code v-if="argumentSummary(row.entry)"> {{ argumentSummary(row.entry) }}</code>
                <span v-if="!row.entry.caption || row.entry.summary" :class="{ 'trace-warning':failed(row.entry) }"> → {{ outcome(row.entry) }}</span></span>
                <small v-if="row.entry.duration_ms!=null">{{ traceDuration(row.entry.duration_ms) }}</small>
              </button>
              <div v-if="expanded===row.key" class="trace-inspector">
                <template v-if="row.entry.caption"><h4>归档内容</h4><pre>{{ format(row.entry.arguments) }}</pre></template>
                <template v-else>
                  <dl><div><dt>开始时间</dt><dd>{{ row.entry.started_at || '未记录' }}</dd></div><div><dt>耗时</dt><dd>{{ traceDuration(row.entry.duration_ms) }}</dd></div><div v-if="row.entry.event==='request'"><dt>Token 用量</dt><dd>{{ traceNumber(row.entry.usage?.total_tokens) }}</dd></div></dl>
                  <template v-if="row.entry.arguments!==undefined"><h4>调用参数</h4><pre>{{ format(row.entry.arguments) }}</pre></template>
                  <template v-if="row.entry.request_summary"><h4>请求摘要</h4><pre>{{ format(row.entry.request_summary) }}</pre></template>
                  <template v-if="row.entry.event==='request'"><h4>思考链（公开推理）</h4><pre class="trace-reasoning">{{ row.entry.reasoning_content || (demo ? '演示模式未调用真实模型。' : '提供方未记录公开推理。') }}</pre><small v-if="row.entry.reasoning_truncated">已达到记录上限</small></template>
                  <template v-if="row.entry.evidence_ids?.length"><h4>证据引用</h4><pre>{{ format(row.entry.evidence_ids) }}</pre></template>
                  <template v-if="row.entry.usage"><h4>Token 明细</h4><pre>{{ format(row.entry.usage) }}</pre></template>
                  <template v-if="row.entry.error"><h4>错误详情</h4><pre>{{ format(row.entry.error) }}</pre></template>
                  <details class="trace-raw"><summary>完整事件 JSON</summary><pre>{{ format(row.entry) }}</pre></details>
                </template>
              </div>
            </div>
          </section>
          <p v-if="!groups.length" class="trace-notice">没有匹配的事件。调整筛选条件或搜索词后可继续查看。</p>
        </div>
      </template>
      <p v-else class="trace-notice">当前来源没有已保存的轨迹。</p>
    </section>`,
});

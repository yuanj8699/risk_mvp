/** 单页分析工作台：历史、输入、结果与轨迹共用当前任务，业务规则和写入仍由后端执行。 */
import { createApp, computed, nextTick, onMounted, ref } from 'vue';
import { api, send, ApiError } from './api';
import { useConversation, type ConversationScope } from './conversation';
import TracePanel, { type AgentTrace, type TraceDiagnostics, type TraceRuntime } from './trace-panel';
import './styles.css';

type ID = string | number;
type TaskKind = 'monitor' | 'code';
interface Organization { id: ID; name: string; level: number }
interface Snapshot { id: ID; name: string; data_date: string; is_demo: boolean; status: string }
interface Context { topic: string; snapshot_id: string; org_scope: string[]; as_of: string }
interface TraceData { trace?: AgentTrace[]; runtime?: TraceRuntime; truncated?: boolean }
interface Selection extends TraceData { mode?: string; topic?: string; error?: string; task_context?: Context; diagnostics?: TraceDiagnostics | null }
interface Summary { triggered?: number; normal?: number; insufficient?: number; unconfigured?: number; case_count?: number }
interface Run { id: ID; status: string; snapshot_id: ID; created_at: string; summary: Summary; selection: Selection }
interface Task extends Run { kind: TaskKind; key: string }
interface Evidence { id: ID; indicator_name: string; value_display: string; reason: string; data_date: string; rule_id: ID }
interface QueryEvidence { evidence_id: string; indicator_name: string; value_display: string; data_date: string; usable?: boolean }
interface Analysis extends TraceData { status?: string; mode: string; facts: string[]; hypotheses: string[]; checks: string[]; queried_evidence?: QueryEvidence[]; evidence_references?: { checks?: { evidence_ids: string[] }[]; hypotheses?: { evidence_ids: string[] }[] } }
interface Letter { id: ID; content: string; revision: number; status: string }
interface RiskCase { id: ID; org_id: ID; org_name: string; region_name: string; headline: string; evidence_count: number; evidence?: Evidence[]; analysis?: Analysis; letter?: Letter | null }
interface SourceEvidence extends QueryEvidence { org_id: string; org_name?: string; source_record_id: string }
interface Finding { org_id: string; statement: string; evidence_ids: string[] }
interface CodeResult extends TraceData { mode: string; summary: string; findings?: Finding[]; evidence?: SourceEvidence[]; result_file?: string; coverage?: { checked_checks: number; triggered: number; insufficient: number; unconfigured: number } }
interface TaskFile { path: string; size: number; sha256: string }
interface Execution { execution_id: string; script_path?: string; exit_code: number; stdout: string; stderr: string }
interface CodeDetail extends Run { result?: CodeResult | null; executions: Execution[]; files: TaskFile[]; workspace_error?: string | null }
interface Bootstrap { meta: { agent_mode: string; agent_provider?: string; agent_model?: string }; organizations: Organization[]; snapshots: Snapshot[] }
interface ImportResult { snapshot_id: ID; row_count: number; warnings: string[] }

// 两类旧接口的UTC时间编码不同；无偏移时间补Z后再统一排序和显示。
const timestamp = (value: string) => new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
const dateTime = (value: string) => timestamp(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Asia/Shanghai' });
const kindName = (kind: TaskKind) => kind === 'monitor' ? '规则监测' : '自主分析';
const agentModeName = (mode?: string) => ({ 'pi-demo': '离线演示', 'pi-model': '模型模式', template: '模板分析' }[mode ?? ''] ?? '模式未记录');
const statusName = (status: string) => ({ completed: '已完成', running: '执行中', failed: '失败', no_candidates: '无补查候选' }[status] ?? status);
const taskTitle = (task: Task) => task.selection.task_context?.topic || task.selection.topic || kindName(task.kind);
const asTask = (run: Run, kind: TaskKind): Task => ({ ...run, kind, key: `${kind}:${run.id}` });

createApp({
  components: { TracePanel },
  setup() {
    const boot = ref<Bootstrap | null>(null);
    const loading = ref(true), fatal = ref(''), error = ref(''), notice = ref('');
    const busy = ref(''), detailLoading = ref(false), sidebarOpen = ref(false), dataOpen = ref(false);
    const history = ref<Task[]>([]), historyQuery = ref('');
    const current = ref<Task | null>(null), codeDetail = ref<CodeDetail | null>(null);
    const cases = ref<RiskCase[]>([]), caseId = ref(''), selectedCase = ref<RiskCase | null>(null), caseLoading = ref(false);
    const caseListReady = ref(false);
    const view = ref<'result' | 'trace'>('result'), traceScope = ref('selection');
    const topic = ref(''), taskKind = ref<TaskKind>('monitor'), snapshotId = ref(''), scopeChoice = ref('all'), savedScope = ref<string[]>([]);
    const importFile = ref<File | null>(null), importResult = ref<ImportResult | null>(null);
    const draft = ref(''), draftUrl = ref('');
    let taskVersion = 0, caseVersion = 0;
    let generatedDraftUrl: string | null = null;

    const societies = computed(() => boot.value?.organizations.filter(org => org.level === 3) ?? []);
    const snapshots = computed(() => boot.value?.snapshots.filter(snapshot => snapshot.status === 'ready') ?? []);
    const visibleHistory = computed(() => history.value.filter(task => `${taskTitle(task)} ${kindName(task.kind)}`.toLocaleLowerCase().includes(historyQuery.value.trim().toLocaleLowerCase())));
    const modeLabel = computed(() => agentModeName(current.value ? current.value.selection.mode : boot.value?.meta.agent_mode));
    const composerMode = computed(() => {
      const config = boot.value?.meta;
      if (config?.agent_mode === 'pi-demo') return '离线演示 · 固定模型响应';
      if (config?.agent_mode !== 'pi-model') return '模型配置未记录';
      const provider = config.agent_provider === 'openai' ? 'OpenAI' : config.agent_provider;
      const model = [provider, config.agent_model].filter(Boolean).join(' / ') || '已配置模型';
      return model + ' · 将向模型发送问题与相关证据';
    });
    const heading = computed(() => current.value ? taskTitle(current.value) : busy.value === 'run' || busy.value === 'retry' ? topic.value || '重试分析' : '新分析');
    const selectedSnapshot = computed(() => snapshots.value.find(snapshot => String(snapshot.id) === snapshotId.value));
    const currentSnapshot = computed(() => snapshots.value.find(snapshot => String(snapshot.id) === String(current.value?.snapshot_id)));
    const activeScope = computed(() => scopeChoice.value === 'all' ? undefined : scopeChoice.value === 'saved' ? savedScope.value : [scopeChoice.value]);
    // 完成后输入框追问当前结果。零事项、无补查候选使用运行级归档，不绑定其他行社。
    const followUpMode = computed(() => current.value?.status === 'completed' || current.value?.status === 'no_candidates');
    const conversationScope = computed<ConversationScope | null>(() => {
      if (!current.value || !followUpMode.value) return null;
      const runId = String(current.value.id);
      if (current.value.kind === 'code') return codeDetail.value ? { runId } : null;
      if (!caseListReady.value) return null;
      if (!cases.value.length) return { runId };
      return selectedCase.value ? { runId, caseId: String(selectedCase.value.id) } : null;
    });
    const conversation = useConversation(conversationScope);
    const conversationEnd = ref<HTMLElement | null>(null);

    const traceOptions = computed(() => {
      const codeTask = current.value?.kind === 'code';
      const options = codeTask ? [{ value: 'code', label: '任务轨迹' }] : [{ value: 'selection', label: '指标选择' }];
      if (!codeTask && selectedCase.value?.analysis) options.push({ value: 'case', label: selectedCase.value.org_name });
      if (!codeTask && (current.value?.status === 'failed' || current.value?.selection.diagnostics)) options.push({ value: 'failure', label: '失败诊断' });
      conversation.turns.value.forEach((turn, i) => {
        if (turn.trace?.length) options.push({ value: 'turn:' + turn.id, label: '追问 ' + (i + 1) + ' · ' + turn.question.slice(0, 24) });
      });
      return options;
    });
    const activeTrace = computed<TraceData & { title: string; error?: string; mode?: string }>(() => {
      const task = current.value;
      if (!task) return { title: '运行轨迹' };
      if (traceScope.value.startsWith('turn:')) {
        const turn = conversation.turns.value.find(item => 'turn:' + item.id === traceScope.value);
        return { trace: turn?.trace, runtime: turn?.runtime, mode: turn?.mode, truncated: turn?.truncated, title: '追问轨迹' };
      }
      if (task.kind === 'code') {
        const source = task.status === 'failed' ? task.selection.diagnostics : codeDetail.value?.result;
        return { ...source, title: '任务轨迹', mode: task.selection.mode, error: task.status === 'failed' ? task.selection.error : undefined };
      }
      if (traceScope.value === 'failure') return { ...task.selection.diagnostics, title: '失败诊断', mode: task.selection.mode, error: task.selection.error };
      if (traceScope.value === 'case') return { ...selectedCase.value?.analysis, title: '行社轨迹' };
      return { ...task.selection, title: '指标选择' };
    });
    // 旧已完成记录没有analysis.status，仍可查看建议与拟函；明确pending/failed不能放行。
    const caseComplete = computed(() => current.value?.status === 'completed' && !!selectedCase.value?.analysis && (selectedCase.value.analysis.status === undefined || selectedCase.value.analysis.status === 'completed'));
    const canDraft = computed(() => current.value?.status === 'completed' && (current.value.kind === 'code' || caseComplete.value));
    const scopeSummary = computed(() => {
      const scope = current.value?.selection.task_context?.org_scope;
      return scope?.length === 1 ? societies.value.find(org => String(org.id) === scope[0])?.name || scope[0] : scope?.length ? `${scope.length} 家行社` : '';
    });

    async function ask() {
      if (busy.value || !conversationScope.value || conversation.reading.value ||
          conversation.readError.value || !conversation.question.value.trim()) return;
      busy.value = 'ask'; view.value = 'result';
      try {
        await nextTick();
        conversationEnd.value?.scrollIntoView({ block: 'end' });
        await conversation.sendQuestion();
      } finally {
        busy.value = '';
        await nextTick();
        conversationEnd.value?.scrollIntoView({ block: 'end' });
      }
    }

    function clearDraft() {
      draft.value = ''; draftUrl.value = '';
      if (generatedDraftUrl) URL.revokeObjectURL(generatedDraftUrl);
      generatedDraftUrl = null;
    }
    function clearResult() {
      current.value = null; codeDetail.value = null; cases.value = []; caseId.value = ''; selectedCase.value = null;
      caseLoading.value = false; caseListReady.value = false; clearDraft();
    }
    async function loadHistory() {
      const [monitor, code] = await Promise.all([api<Run[]>('/api/runs'), api<Run[]>('/api/code-runs')]);
      history.value = [...monitor.map(run => asTask(run, 'monitor')), ...code.map(run => asTask(run, 'code'))]
        .sort((a, b) => timestamp(b.created_at).getTime() - timestamp(a.created_at).getTime());
    }
    function setContext(task: Task) {
      topic.value = taskTitle(task); taskKind.value = task.kind; snapshotId.value = String(task.snapshot_id);
      const scope = task.selection.task_context?.org_scope ?? [];
      savedScope.value = [...scope];
      scopeChoice.value = scope.length === 1 ? scope[0] : scope.length && scope.length !== societies.value.length ? 'saved' : 'all';
    }
    async function loadCase(id: string) {
      const version = ++caseVersion, taskKey = current.value?.key;
      if (traceScope.value.startsWith('turn:')) traceScope.value = 'case';
      caseId.value = id; selectedCase.value = null; clearDraft(); caseLoading.value = true;
      try {
        const detail = await api<RiskCase>(`/api/cases/${encodeURIComponent(id)}`);
        if (version !== caseVersion || current.value?.key !== taskKey) return;
        selectedCase.value = detail;
        if (detail.letter) { draft.value = detail.letter.content; draftUrl.value = `/api/letters/${encodeURIComponent(String(detail.letter.id))}/download`; }
      } catch (err) {
        if (version === caseVersion && current.value?.key === taskKey) error.value = err instanceof Error ? err.message : String(err);
      } finally { if (version === caseVersion) caseLoading.value = false; }
    }
    async function openTask(task: Task, hydrateForm = true, preferredCaseId = '') {
      const version = ++taskVersion;
      ++caseVersion; clearResult(); error.value = ''; notice.value = ''; detailLoading.value = true;
      current.value = task; if (hydrateForm) setContext(task); sidebarOpen.value = false; dataOpen.value = false;
      traceScope.value = task.kind === 'code' ? 'code' : task.status === 'failed' ? 'failure' : 'selection';
      try {
        if (task.kind === 'code') {
          const detail = await api<CodeDetail>(`/api/code-runs/${encodeURIComponent(String(task.id))}`);
          if (version !== taskVersion) return;
          current.value = asTask(detail, 'code'); codeDetail.value = detail;
        } else {
          const items = await api<RiskCase[]>(`/api/cases?run_id=${encodeURIComponent(String(task.id))}`);
          if (version !== taskVersion) return;
          cases.value = items; caseListReady.value = true;
          const selected = items.find(item => String(item.id) === preferredCaseId) || items[0];
          if (selected) await loadCase(String(selected.id));
        }
      } catch (err) { if (version === taskVersion) error.value = err instanceof Error ? err.message : String(err); }
      finally { if (version === taskVersion) detailLoading.value = false; }
    }
    function chooseTask(task: Task) { if (!busy.value) { view.value = 'result'; void openTask(task); } }
    function newTask() {
      if (busy.value) return;
      ++taskVersion; ++caseVersion; clearResult(); detailLoading.value = false;
      topic.value = ''; scopeChoice.value = 'all'; savedScope.value = []; view.value = 'result';
      error.value = ''; notice.value = ''; sidebarOpen.value = false; dataOpen.value = false;
    }
    async function initialize() {
      loading.value = true; fatal.value = '';
      try {
        boot.value = await api<Bootstrap>('/api/bootstrap');
        snapshotId.value = String(snapshots.value[0]?.id ?? '');
        await loadHistory();
        if (history.value[0]) await openTask(history.value[0]);
      } catch (err) { fatal.value = err instanceof Error ? err.message : String(err); }
      finally { loading.value = false; }
    }
    async function refresh() {
      if (busy.value) return;
      busy.value = 'refresh'; error.value = '';
      const key = current.value?.key, previousView = view.value, previousCase = caseId.value, previousScope = traceScope.value;
      try {
        await loadHistory();
        const task = history.value.find(item => item.key === key);
        if (task) await openTask(task, false, previousCase);
        view.value = previousView;
        const sameConversation = previousScope.startsWith('turn:') && key === current.value?.key && previousCase === caseId.value;
        if (sameConversation || traceOptions.value.some(item => item.value === previousScope)) traceScope.value = previousScope;
      }
      catch (err) { error.value = err instanceof Error ? err.message : String(err); }
      finally { busy.value = ''; }
    }
    async function start(retry = false) {
      if (busy.value || detailLoading.value) return;
      const original = current.value;
      const context = retry ? original?.selection.task_context : undefined;
      if (retry && !context) { error.value = '旧任务缺少固定范围，请新建分析。'; return; }
      const kind = retry ? original!.kind : taskKind.value;
      const body = { snapshot_id: context ? context.snapshot_id : snapshotId.value, risk_topic: context ? context.topic : topic.value.trim(),
        ...(context ? { org_scope: context.org_scope, retry: true } : activeScope.value ? { org_scope: activeScope.value } : {}) };
      if ((!retry && !body.risk_topic) || !body.snapshot_id) { error.value = '请填写分析任务并选择数据快照。'; return; }
      ++taskVersion; ++caseVersion; clearResult(); view.value = 'result'; error.value = ''; notice.value = ''; dataOpen.value = false;
      topic.value = body.risk_topic; busy.value = retry ? 'retry' : 'run';
      try {
        let run: Run;
        try { run = await send<Run>(kind === 'code' ? '/api/code-runs' : '/api/runs', body); }
        catch (err) {
          const detail = err instanceof ApiError ? err.detail : null;
          if (detail && typeof detail === 'object' && 'run_id' in detail && typeof detail.run_id === 'string') {
            try {
              await loadHistory();
              const failed = history.value.find(item => item.key === `${kind}:${detail.run_id}`);
              if (!failed) throw new Error('未能读取失败任务，请刷新历史。');
              await openTask(failed);
            } catch (recoveryError) { throw new Error(`${(err as ApiError).message}；${recoveryError instanceof Error ? recoveryError.message : String(recoveryError)}`); }
          }
          throw err;
        }
        await loadHistory(); await openTask(asTask(run, kind));
      } catch (err) { error.value = err instanceof Error ? err.message : String(err); }
      finally { busy.value = ''; }
    }
    async function createDraft() {
      if (busy.value || !canDraft.value || !current.value) return;
      busy.value = 'draft'; error.value = '';
      try {
        if (current.value.kind === 'monitor' && selectedCase.value) {
          const letter = await send<Letter>(`/api/cases/${encodeURIComponent(String(selectedCase.value.id))}/draft`, {});
          clearDraft(); draft.value = letter.content; draftUrl.value = `/api/letters/${encodeURIComponent(String(letter.id))}/download`;
        } else {
          const result = await send<{ content: string }>(`/api/code-runs/${encodeURIComponent(String(current.value.id))}/draft`, {});
          clearDraft(); draft.value = result.content;
          generatedDraftUrl = URL.createObjectURL(new Blob(['\uFEFF', result.content], { type: 'text/plain;charset=utf-8' })); draftUrl.value = generatedDraftUrl;
        }
      } catch (err) { error.value = err instanceof Error ? err.message : String(err); }
      finally { busy.value = ''; }
    }
    async function upload() {
      if (busy.value || !importFile.value) return;
      busy.value = 'import'; error.value = ''; notice.value = '';
      try {
        const form = new FormData(); form.append('file', importFile.value);
        const result = await api<ImportResult>('/api/import', { method: 'POST', body: form });
        boot.value = await api<Bootstrap>('/api/bootstrap'); snapshotId.value = String(result.snapshot_id); importResult.value = result;
        notice.value = `已导入 ${result.row_count.toLocaleString('zh-CN')} 条记录，下次分析使用此快照。`;
      } catch (err) { error.value = err instanceof Error ? err.message : String(err); }
      finally { busy.value = ''; }
    }
    function chooseFile(event: Event) { importFile.value = (event.target as HTMLInputElement).files?.[0] ?? null; importResult.value = null; }
    const fileUrl = (path: string) => `/api/code-runs/${encodeURIComponent(String(current.value?.id))}/files/${path.split('/').map(encodeURIComponent).join('/')}`;
    const findingEvidence = (finding: Finding) => codeDetail.value?.result?.evidence?.filter(item => finding.evidence_ids.includes(item.evidence_id)) ?? [];
    const organizationName = (id: string) => societies.value.find(org => String(org.id) === id)?.name || id;
    onMounted(initialize);
    return { boot, loading, fatal, error, notice, busy, detailLoading, sidebarOpen, dataOpen, historyQuery, visibleHistory, current, codeDetail,
      cases, caseId, selectedCase, caseLoading, view, traceScope, traceOptions, activeTrace, topic, taskKind, snapshotId, scopeChoice, savedScope,
      societies, snapshots, selectedSnapshot, currentSnapshot, modeLabel, composerMode, heading, scopeSummary, importFile, importResult, draft, draftUrl, canDraft, caseComplete,
      initialize, refresh, chooseTask, newTask, loadCase, start, createDraft, chooseFile, upload, fileUrl, findingEvidence, organizationName, dateTime, kindName, statusName, taskTitle,
      followUpMode, conversationScope, ...conversation, ask, conversationEnd, agentModeName };
  },
  template: `
    <div v-if="loading" class="app-loading"><div class="loading-lines" aria-label="正在加载工作台"><span></span><span></span><span></span></div></div>
    <main v-else-if="fatal" class="blank-state"><h1>无法连接工作台</h1><p>{{ fatal }}</p><button class="button primary" @click="initialize">重新连接</button></main>
    <div v-else-if="boot" :class="['workbench',{'history-open':sidebarOpen}]">
      <button v-if="sidebarOpen" class="sidebar-scrim" aria-label="关闭任务历史" @click="sidebarOpen=false"></button>
      <aside class="history-sidebar" aria-label="任务历史">
        <div class="history-brand"><strong>Pi 风险分析</strong><button class="icon-button sidebar-close" @click="sidebarOpen=false" aria-label="关闭任务历史">关闭</button></div>
        <button class="button new-task" @click="newTask" :disabled="!!busy">新建分析</button>
        <input v-model="historyQuery" class="history-search" type="search" placeholder="搜索任务" aria-label="搜索任务历史" />
        <div class="history-heading"><span>最近任务</span><button class="button quiet" @click="refresh" :disabled="!!busy">刷新</button></div>
        <nav class="history-list" aria-label="选择历史任务"><button v-for="task in visibleHistory" :key="task.key" :class="['history-item',{active:current?.key===task.key}]" :aria-current="current?.key===task.key?'true':undefined" @click="chooseTask(task)" :disabled="!!busy"><span class="history-item-title">{{ taskTitle(task) }}</span><span class="history-item-meta"><span>{{ kindName(task.kind) }}</span><span :class="['history-status',task.status]">{{ statusName(task.status) }}</span><time>{{ dateTime(task.created_at) }}</time></span></button><p v-if="!visibleHistory.length" class="history-empty">{{ historyQuery?'没有匹配的任务':'新建一次分析，记录会保存在这里。' }}</p></nav>
        <footer class="sidebar-footer"><span class="connection-dot"></span><span>本地工作台</span></footer>
      </aside>
      <main class="workspace">
        <header class="workspace-header"><button class="icon-button history-toggle" @click="sidebarOpen=!sidebarOpen" :aria-expanded="sidebarOpen" aria-label="切换任务历史">历史</button><div class="workspace-title"><h1 :title="heading">{{ heading }}</h1><div class="workspace-meta" v-if="current"><span>{{ kindName(current.kind) }}</span><span>{{ currentSnapshot?.data_date || current.selection.task_context?.as_of }}</span><span>{{ scopeSummary }}</span><span v-if="currentSnapshot?.is_demo">模拟数据</span></div></div><span class="mode-label">{{ modeLabel }}</span><span v-if="current" :class="['status-label',current.status]">{{ statusName(current.status) }}</span></header>
        <nav class="workspace-tabs" aria-label="当前任务视图"><button :class="{active:view==='result'}" :aria-pressed="view==='result'" @click="view='result'">结果</button><button :class="{active:view==='trace'}" :aria-pressed="view==='trace'" @click="view='trace'" :disabled="!current">轨迹</button></nav>
        <section class="workspace-content" aria-live="polite">
          <div v-if="error && error!==current?.selection.error" class="notice error" role="alert"><p>{{ error }}</p><button class="button quiet" @click="error=''" aria-label="关闭错误提示">关闭</button></div>
          <div v-if="notice" class="notice success" role="status">{{ notice }}</div>
          <div v-if="busy==='run' || busy==='retry'" class="pending-task"><p class="task-message user">{{ topic }}</p><div class="assistant-message"><strong>正在分析</strong><p>正在核对数据和证据，完成后会保存结果与调用轨迹。</p><div class="loading-lines" aria-label="分析执行中"><span></span><span></span><span></span></div></div></div>
          <div v-else-if="detailLoading" class="loading-lines" aria-label="正在读取任务"><span></span><span></span><span></span></div>
          <div v-else-if="!current" class="blank-state"><p class="blank-kicker">风险分析工作台</p><h2>这次想核查什么？</h2><p>选择数据，描述关注的问题。结果、证据和轨迹都留在当前任务中。</p><div class="starter-prompts"><button @click="topic='关注不良贷款率排名靠后的行社，核对逾期贷款率及历史变化。'">核查贷款质量与历史变化</button><button @click="topic='检查资金业务是否存在不良，区分期末余额与较年初增减。'">检查资金业务不良</button></div></div>
          <template v-else-if="view==='trace'"><div class="trace-scope"><label>查看范围<select v-model="traceScope" aria-label="轨迹范围"><option v-for="item in traceOptions" :key="item.value" :value="item.value">{{ item.label }}</option></select></label><label v-if="current.kind==='monitor' && traceScope==='case' && cases.length>1">行社<select :value="caseId" @change="loadCase($event.target.value)" :disabled="caseLoading || !!busy" aria-label="轨迹行社"><option v-for="item in cases" :key="String(item.id)" :value="String(item.id)">{{ item.org_name }}</option></select></label></div><trace-panel :key="current.key+traceScope+caseId+current.status" :trace="activeTrace.trace" :runtime="activeTrace.runtime" :mode="activeTrace.mode" :error="activeTrace.error" :truncated="activeTrace.truncated" :title="activeTrace.title" /></template>
          <div v-else class="result-thread">
            <p class="task-message user">{{ taskTitle(current) }}</p>
            <section v-if="current.status==='failed'" class="assistant-message"><h2>分析未完成</h2><p class="failure-copy">{{ current.selection.error || '执行未完成，请查看调用轨迹。' }}</p><div class="inline-actions"><button class="button" @click="view='trace';traceScope=current.kind==='code'?'code':'failure'">查看失败轨迹</button><button class="button quiet" @click="start(true)" :disabled="!!busy">按原任务重试</button></div></section>
            <section v-else-if="current.status==='running'" class="assistant-message"><h2>任务仍在执行</h2><p>结果尚未就绪，可以刷新读取当前状态。</p><button class="button" @click="refresh" :disabled="!!busy">刷新任务</button></section>
            <template v-if="current.kind==='monitor'">
              <section v-if="current.summary.triggered!==undefined || cases.length || current.status==='completed'" class="assistant-message"><h2>{{ current.status==='completed' ? '监测结果' : '已保存的规则检查' }}</h2><p class="result-summary">{{ current.summary.case_count ?? cases.length }} 家行社形成风险事项，{{ current.summary.triggered ?? 0 }} 项规则触发。<span v-if="current.summary.insufficient">{{ current.summary.insufficient }} 项数据不足。</span><span v-if="current.summary.unconfigured">{{ current.summary.unconfigured }} 项阈值待配置。</span></p>
                <p v-if="current.status==='completed' && !cases.length && current.summary.case_count===0" class="subtle">本次没有触发已配置规则；数据不足和阈值待配置仍需核对。</p>
                <div v-if="cases.length" class="case-picker"><label>行社<select :value="caseId" @change="loadCase($event.target.value)" :disabled="caseLoading || !!busy" aria-label="查看行社结果"><option v-for="item in cases" :key="String(item.id)" :value="String(item.id)">{{ item.org_name }} · {{ item.evidence_count }} 项依据</option></select></label><button class="button quiet" @click="view='trace';traceScope='case'" :disabled="!selectedCase?.analysis">查看行社轨迹</button></div>
                <div v-if="caseLoading" class="loading-lines" aria-label="正在读取行社证据"><span></span><span></span></div>
                <template v-else-if="selectedCase"><div class="case-heading"><h3>{{ selectedCase.org_name }}</h3><span>{{ selectedCase.region_name }}</span></div><p>{{ selectedCase.headline }}</p><div class="analysis-copy" v-if="caseComplete"><h4>核查建议</h4><ul><li v-for="(check,index) in selectedCase.analysis.checks" :key="index">{{ check }}<details v-if="selectedCase.analysis.evidence_references?.checks?.[index]?.evidence_ids.length" class="reference-disclosure"><summary>引用 {{ selectedCase.analysis.evidence_references.checks[index].evidence_ids.length }} 条证据</summary><code>{{ selectedCase.analysis.evidence_references.checks[index].evidence_ids.join('、') }}</code></details></li></ul><details class="inline-disclosure" v-if="selectedCase.analysis.hypotheses?.length"><summary>待核查解释</summary><p v-for="(item,index) in selectedCase.analysis.hypotheses" :key="index">{{ item }}</p></details></div>
                  <details class="inline-disclosure"><summary>触发证据 · {{ selectedCase.evidence?.length || 0 }} 条</summary><div class="table-scroll"><table class="evidence-table"><thead><tr><th>指标</th><th>实际值</th><th>期次</th><th>触发依据</th></tr></thead><tbody><tr v-for="item in selectedCase.evidence" :key="String(item.id)+String(item.rule_id)"><td>{{ item.indicator_name }}<code>{{ item.id }}</code></td><td>{{ item.value_display }}</td><td>{{ item.data_date }}</td><td>{{ item.reason }}</td></tr></tbody></table></div></details>
                  <details v-if="selectedCase.analysis?.queried_evidence?.length" class="inline-disclosure"><summary>补查证据 · {{ selectedCase.analysis.queried_evidence.length }} 条</summary><div class="table-scroll"><table class="evidence-table"><thead><tr><th>指标</th><th>实际值</th><th>期次</th><th>证据 ID</th></tr></thead><tbody><tr v-for="item in selectedCase.analysis.queried_evidence" :key="item.evidence_id"><td>{{ item.indicator_name }}</td><td>{{ item.value_display }}</td><td>{{ item.data_date }}</td><td><code>{{ item.evidence_id }}</code><small v-if="item.usable===false">不可用于引用</small></td></tr></tbody></table></div></details>
                </template>
              </section>
            </template>
            <template v-else-if="codeDetail">
              <section v-if="current.status==='no_candidates'" class="assistant-message"><h2>无补查候选</h2><p>{{ codeDetail.result?.summary }}</p><p v-if="codeDetail.result?.coverage" class="subtle">检查 {{ codeDetail.result.coverage.checked_checks }} 项，触发 {{ codeDetail.result.coverage.triggered }} 项，数据不足 {{ codeDetail.result.coverage.insufficient }} 项，待配置 {{ codeDetail.result.coverage.unconfigured }} 项。</p></section>
              <section v-if="current.status==='completed' && codeDetail.result" class="assistant-message"><h2>分析结果</h2><p>{{ codeDetail.result.summary }}</p><article v-for="(finding,index) in codeDetail.result.findings" :key="index" class="finding"><h3>{{ organizationName(finding.org_id) }}</h3><p>{{ finding.statement }}</p><details class="inline-disclosure"><summary>来源证据 · {{ finding.evidence_ids.length }} 条</summary><div class="table-scroll"><table class="evidence-table"><thead><tr><th>指标</th><th>实际值</th><th>期次</th><th>原始记录</th></tr></thead><tbody><tr v-for="item in findingEvidence(finding)" :key="item.evidence_id"><td>{{ item.indicator_name }}</td><td>{{ item.value_display }}</td><td>{{ item.data_date }}</td><td><code>{{ item.source_record_id || item.evidence_id }}</code></td></tr></tbody></table></div></details></article><a v-if="codeDetail.result.result_file" class="text-link" :href="fileUrl(codeDetail.result.result_file)" download>下载结果 JSON</a></section>
              <details v-if="codeDetail.executions.length" class="inline-disclosure"><summary>代码执行记录 · {{ codeDetail.executions.length }} 次</summary><article v-for="(item,index) in codeDetail.executions" :key="item.execution_id" class="execution-entry"><h3>第 {{ index+1 }} 次 · {{ item.exit_code===0?'成功':'退出码 '+item.exit_code }}</h3><p><code>{{ item.script_path }}</code></p><pre v-if="item.stderr">{{ item.stderr }}</pre><details v-if="item.stdout"><summary>标准输出</summary><pre>{{ item.stdout }}</pre></details></article></details>
              <p v-if="codeDetail.workspace_error" class="notice warning">{{ codeDetail.workspace_error }}</p><details v-if="codeDetail.files.length" class="inline-disclosure"><summary>任务文件 · {{ codeDetail.files.length }} 个</summary><ul class="file-list"><li v-for="file in codeDetail.files" :key="file.path"><a :href="fileUrl(file.path)" download>{{ file.path }}</a><span>{{ (file.size/1024).toFixed(1) }} KiB</span></li></ul></details>
            </template>
            <section v-if="canDraft" class="draft-section"><button v-if="!draft" class="button" @click="createDraft" :disabled="!!busy">{{ busy==='draft'?'正在生成…':'生成提示函草稿' }}</button><details v-else class="inline-disclosure draft-document" open><summary>提示函草稿</summary><div class="inline-actions"><a class="text-link" :href="draftUrl" download="风险提示函草稿.txt">下载草稿</a><span class="subtle">仅供核查，未审批或下发</span></div><pre>{{ draft }}</pre></details></section>
            <section v-if="followUpMode" class="conversation" aria-label="Pi 追问记录">
              <article v-for="turn in turns" :key="turn.id" class="conversation-turn">
                <p class="task-message user">{{ turn.question }}</p>
                <div class="assistant-byline"><strong>Pi</strong><span>{{ agentModeName(turn.mode) }}</span></div>
                <p class="answer-text">{{ turn.answer }}</p>
                <details v-if="turn.evidence.length" class="inline-disclosure">
                  <summary>引用证据 · {{ turn.evidence.length }} 条</summary>
                  <ul class="answer-sources"><li v-for="source in turn.evidence" :key="source.id"><strong>{{ source.label }}</strong><p>{{ source.text }}</p><code>{{ source.id }}</code></li></ul>
                </details>
                <button v-if="turn.trace?.length" class="button quiet" @click="view='trace';traceScope='turn:'+turn.id">查看本轮轨迹</button>
              </article>
              <div v-if="busy==='ask'" class="conversation-turn" role="status">
                <p class="task-message user">{{ question }}</p><p class="subtle">Pi 正在查阅本次分析的证据…</p>
              </div>
              <div ref="conversationEnd"></div>
            </section>
          </div>
        </section>
        <footer class="composer-dock">
          <section v-if="dataOpen" id="data-options" class="data-options"><div class="data-options-heading"><strong>本次使用的数据</strong><button class="button quiet" @click="dataOpen=false">收起</button></div><div class="composer-options"><label class="field"><span>数据快照</span><select v-model="snapshotId" :disabled="!!busy" aria-label="数据快照"><option v-for="item in snapshots" :key="String(item.id)" :value="String(item.id)">{{ item.name }} · {{ item.data_date }}</option></select></label><label class="field"><span>机构范围</span><select v-model="scopeChoice" :disabled="!!busy" aria-label="机构范围"><option value="all">全部行社</option><option v-if="savedScope.length>1 && savedScope.length!==societies.length" value="saved">原任务范围 · {{ savedScope.length }} 家</option><option v-for="org in societies" :key="String(org.id)" :value="String(org.id)">{{ org.name }}</option></select></label></div><div class="import-tools"><label class="file-label">导入 CSV / XLSX<input type="file" accept=".csv,.xlsx" :disabled="!!busy" @change="chooseFile" aria-label="选择数据文件" /></label><button class="button" @click="upload" :disabled="!!busy || !importFile">{{ busy==='import'?'导入中…':'导入文件' }}</button><a class="text-link" href="/api/import/template.csv" download>下载模板</a></div><ul v-if="importResult?.warnings.length" class="import-warnings"><li v-for="item in importResult.warnings" :key="item">{{ item }}</li></ul></section>
          <form v-if="followUpMode" class="composer" @submit.prevent="ask">
            <textarea v-model="question" class="composer-input" rows="2" maxlength="1000" :disabled="!!busy || !conversationScope"
              aria-label="追问 Pi" placeholder="问 Pi：这个结论有哪些数据依据？" @keydown.ctrl.enter.prevent="ask" @keydown.meta.enter.prevent="ask"></textarea>
            <div class="composer-toolbar">
              <span class="followup-scope">{{ selectedCase?.org_name || '本次任务' }} · 依据已保存的分析记录回答</span>
              <button class="button primary run-button" type="submit" :disabled="!!busy || reading || !!readError || !conversationScope || !question.trim()">{{ busy==='ask'?'回答中…':'发送追问' }}</button>
            </div>
          </form>
          <form v-else class="composer" @submit.prevent="start()"><textarea v-model="topic" class="composer-input" rows="2" maxlength="500" :disabled="!!busy" placeholder="描述要分析的问题，例如：哪些行社的不良率排名靠后？" aria-label="分析任务" @keydown.ctrl.enter.prevent="start()" @keydown.meta.enter.prevent="start()"></textarea><div class="composer-toolbar"><button type="button" class="button quiet data-toggle" @click="dataOpen=!dataOpen" :aria-expanded="dataOpen" aria-controls="data-options" :disabled="!!busy">{{ selectedSnapshot?.data_date || '选择数据' }} · {{ scopeChoice==='all'?'全部行社':scopeChoice==='saved'?savedScope.length+' 家行社':organizationName(scopeChoice) }}</button><label class="method-select"><span class="sr-only">分析方式</span><select v-model="taskKind" :disabled="!!busy" aria-label="分析方式"><option value="monitor">规则监测</option><option value="code">自主分析</option></select></label><button class="button primary run-button" type="submit" :disabled="!!busy || detailLoading || !topic.trim() || !snapshotId">{{ busy==='run'||busy==='retry'?'分析中…':'运行分析' }}</button></div></form>
          <div v-if="followUpMode && (readError || sendError)" class="conversation-error" role="alert">
            <span>{{ readError ? '读取追问记录失败。' + readError : sendError }}</span>
            <button v-if="readError" class="button quiet" @click="loadConversation" :disabled="!!busy || reading">重新读取</button>
            <button v-else class="button quiet" @click="ask" :disabled="!!busy || !question.trim()">重新发送</button>
          </div>
          <p class="composer-note"><span>{{ composerMode }}<template v-if="!followUpMode && taskKind==='code'"> · Python 需要 Docker 环境</template></span><span>{{ followUpMode && reading ? '正在读取对话…' : 'Ctrl / ⌘ + Enter ' + (followUpMode ? '发送' : '运行') }}</span></p>
        </footer>
      </main>
    </div>
  `,
}).mount('#app');

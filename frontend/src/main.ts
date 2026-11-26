/** 单页分析工作台：历史、输入、结果与轨迹共用当前任务，业务规则和写入仍由后端执行。 */
import { createApp, computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { api, send, ApiError } from './api';
import { useConversation, type ConversationScope } from './conversation';
import TracePanel from './trace-panel';
import 'tdesign-vue-next/es/style/index.css';
import ScanSetup, { executableDimensionIds } from './scan-setup';
import OverviewPanel, { firstOverviewSociety, reconciliationState } from './overview-panel';
import { SocietyPanel, ConversationPanel, FollowupComposer } from './detail-panel';
import { Button as TButton } from 'tdesign-vue-next/es/button';
import AddIcon from 'tdesign-icons-vue-next/esm/components/add';
import CalendarIcon from 'tdesign-icons-vue-next/esm/components/calendar';
import DownloadIcon from 'tdesign-icons-vue-next/esm/components/download';
import DeleteIcon from 'tdesign-icons-vue-next/esm/components/delete';
import MenuIcon from 'tdesign-icons-vue-next/esm/components/menu-unfold';
import CloseIcon from 'tdesign-icons-vue-next/esm/components/close';
import type { AutoReports, LetterDiagnostics, RiskScan, ScanSociety } from './scan-types';
import { asTask, compareHistory, canDeleteTask, taskResource, agentModeName, statusName, taskTitle,
  type Bootstrap, type CodeDetail, type Finding, type ImportResult, type RiskCase, type Run, type Task, type TraceData } from './workbench-tasks';

import './styles.css';

createApp({
  components: { TracePanel, ScanSetup, OverviewPanel, SocietyPanel, ConversationPanel, FollowupComposer, TButton, AddIcon, CalendarIcon, DownloadIcon, DeleteIcon, MenuIcon, CloseIcon },
  setup() {
    const boot = ref<Bootstrap | null>(null);
    const loading = ref(true), fatal = ref(''), error = ref(''), notice = ref('');
    const busy = ref(''), detailLoading = ref(false), sidebarOpen = ref(false);
    const history = ref<Task[]>([]);
    const hasFailedTasks = computed(() => history.value.some(task => task.status === 'failed'));
    const current = ref<Task | null>(null), codeDetail = ref<CodeDetail | null>(null);
    const cases = ref<RiskCase[]>([]), caseId = ref(''), selectedCase = ref<RiskCase | null>(null), caseLoading = ref(false);
    const caseListReady = ref(false);
    const view = ref<'result' | 'trace' | 'report'>('result'), traceScope = ref('selection');
    const content = ref<HTMLElement | null>(null);
    watch([view, () => current.value?.key], () => content.value?.scrollTo({ top: 0 }), { flush: 'post' });
    const topic = ref(''), taskKind = ref<'risk_scan' | 'code'>('risk_scan'), snapshotId = ref('');
    const businessDate = ref(new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai' }).format(new Date()));
    const dimensionIds = ref<string[]>([]);
    const scan = shallowRef<RiskScan | null>(null), selectedSociety = shallowRef<ScanSociety | null>(null);

    const followScope = ref<'run' | 'society'>('run');
    const importFile = ref<File | null>(null), importResult = ref<ImportResult | null>(null);
    const draft = ref(''), draftUrl = ref('');
    let taskVersion = 0, caseVersion = 0;

    const societies = computed(() => boot.value?.organizations.filter(org => org.level === 3) ?? []);
    const snapshots = computed(() => boot.value?.snapshots.filter(snapshot => snapshot.status === 'ready') ?? []);
    const modeLabel = computed(() => agentModeName(current.value ? current.value.selection.mode : boot.value?.meta.agent_mode));
    const composerMode = computed(() => {
      const config = boot.value?.meta;
      if (config?.agent_mode === 'pi-demo') return '离线演示 · 固定模型响应';
      if (config?.agent_mode !== 'pi-model') return '模型配置未记录';
      const provider = config.agent_provider === 'openai' ? 'OpenAI' : config.agent_provider;
      const model = [provider, config.agent_model].filter(Boolean).join(' / ') || '已配置模型';
      return model + ' · 将向模型发送问题与相关证据';
    });
    const submitting = computed(() => busy.value === 'run' || busy.value === 'retry');
    const quickbiScan = computed(() => (current.value?.kind ?? taskKind.value) === 'risk_scan' && boot.value?.meta.data_fetcher === 'quickbi');
    const scopeLabel = computed(() => quickbiScan.value ? '当前身份可见行社' : '全体 82 家行社');
    const selectedSnapshot = computed(() => snapshots.value.find(snapshot => String(snapshot.id) === snapshotId.value));
    const currentSnapshot = computed(() => snapshots.value.find(snapshot => String(snapshot.id) === String(current.value?.snapshot_id)));
    const canStart = computed(() => !current.value && !!(quickbiScan.value ? businessDate.value : snapshotId.value) && !busy.value && !detailLoading.value &&
      (taskKind.value === 'risk_scan' ? dimensionIds.value.length > 0 : societies.value.length === 82 && !!topic.value.trim()));
    // 完成后输入框追问当前结果。零事项、无补查候选使用运行级归档，不绑定其他行社。
    const followUpMode = computed(() => current.value?.status === 'completed' || current.value?.status === 'no_candidates');
    const conversationScope = computed<ConversationScope | null>(() => {
      if (!current.value || !followUpMode.value) return null;
      const runId = String(current.value.id);
      if (current.value.kind === 'risk_scan') {
        if (!scan.value) return null;
        return followScope.value === 'society' && selectedSociety.value
          ? { runId, caseId: selectedSociety.value.case_id } : { runId };
      }
      if (current.value.kind === 'code') return codeDetail.value ? { runId } : null;
      if (!caseListReady.value) return null;
      if (!cases.value.length) return { runId };
      return selectedCase.value ? { runId, caseId: String(selectedCase.value.id) } : null;
    });
    const conversation = useConversation(conversationScope);
    const conversationEnd = ref<HTMLElement | null>(null);

    function onDrafted(runId: string, record: LetterDiagnostics) {
      if (scan.value?.id !== runId) return;
      scan.value = { ...scan.value, letter_drafts: { ...scan.value.letter_drafts, [record.org_id]: record } };
      remember(asTask(scan.value, 'risk_scan'));
    }

    const startingRun = ref(''), pollError = ref(''), pageActive = ref(true);
    const pageHidden = () => { pageActive.value = false; };
    const pageShown = () => { pageActive.value = true; };
    onMounted(() => {
      window.addEventListener('pagehide', pageHidden);
      window.addEventListener('pageshow', pageShown);
    });
    onBeforeUnmount(() => {
      pageHidden();
      window.removeEventListener('pagehide', pageHidden);
      window.removeEventListener('pageshow', pageShown);
    });

    // 队列只会更新进度和撰写轨迹。保留同一扫描的行社数组，分页和编辑区不会随轮询重置。
    async function syncReports(runId: string, signal?: AbortSignal) {
      const version = taskVersion;
      try {
        const detail = await api<RiskScan>('/api/risk-scans/' + encodeURIComponent(runId), { signal });
        const currentScan = scan.value;
        if (signal?.aborted || version !== taskVersion || currentScan?.id !== runId) return;
        pollError.value = '';
        const drafts = { ...currentScan.letter_drafts };
        let changed = false;
        for (const [org, record] of Object.entries(detail.letter_drafts ?? {})) {
          if (!drafts[org] || drafts[org].revision < record.revision || (drafts[org].revision === record.revision && drafts[org].updated_at !== record.updated_at)) {
            drafts[org] = record;
            changed = true;
          }
        }
        if (!changed && JSON.stringify(currentScan.auto_reports) === JSON.stringify(detail.auto_reports)) return;
        scan.value = { ...currentScan, auto_reports: detail.auto_reports, letter_drafts: changed ? drafts : currentScan.letter_drafts };
        remember(asTask(scan.value, 'risk_scan'));
      } catch (err) {
        if (!signal?.aborted && version === taskVersion && scan.value?.id === runId)
          pollError.value = '读取报告进度失败 · ' + (err instanceof Error ? err.message : String(err));
      }
    }
    watch(() => pageActive.value && scan.value?.auto_reports?.status === 'running' ? scan.value.id : '',
      (id, _, cleanup) => {
        if (!id) return;
        const controller = new AbortController();
        let timer: number;
        const poll = async () => {
          await syncReports(id, controller.signal);
          if (!controller.signal.aborted) timer = window.setTimeout(poll, 4000);
        };
        timer = window.setTimeout(poll, 4000);
        cleanup(() => { window.clearTimeout(timer); controller.abort(); });
      }, { flush: 'sync' });

    async function startReports() {
      const selected = scan.value, version = taskVersion;
      if (!selected || startingRun.value === selected.id || selected.auto_reports?.status === 'running') return;
      startingRun.value = selected.id;
      error.value = ''; notice.value = '';
      try {
        const progress = await send<AutoReports>('/api/risk-scans/' + encodeURIComponent(selected.id) + '/auto-reports',
          { retry_failed_only: true });
        if (version !== taskVersion || scan.value?.id !== selected.id) return;
        scan.value = { ...scan.value, auto_reports: progress };
        remember(asTask(scan.value, 'risk_scan'));
        if (progress.status === 'idle') notice.value = progress.rule || '本次没有可自动生成的重点报告。';
        else if (progress.status !== 'running') await syncReports(selected.id);
      } catch (err) {
        if (version === taskVersion) error.value = err instanceof Error ? err.message : String(err);
      } finally {
        if (startingRun.value === selected.id) startingRun.value = '';
      }
    }
    const traceOptions = computed(() => {
      const kind = current.value?.kind;
      const options = kind === 'risk_scan' ? [{ value: 'scan', label: '当前扫描' }]
        : kind === 'code' ? [{ value: 'code', label: '任务轨迹' }] : [{ value: 'selection', label: '指标选择' }];
      if (kind === 'monitor' && selectedCase.value?.analysis) options.push({ value: 'case', label: selectedCase.value.org_name });
      if (kind === 'monitor' && (current.value?.status === 'failed' || current.value?.selection.diagnostics)) options.push({ value: 'failure', label: '失败诊断' });
      if (kind === 'risk_scan') for (const draft of Object.values(scan.value?.letter_drafts ?? {})) options.push({ value: 'letter:' + draft.org_id, label: '风险报告 · ' + draft.org_name });
      conversation.turns.value.forEach((turn, i) => {
        options.push({ value: 'turn:' + turn.id, label: '第 ' + (i + 1) + ' 轮追问' });
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
      if (task.kind === 'risk_scan') {
        if (traceScope.value.startsWith('letter:')) {
          const draft = scan.value?.letter_drafts?.[traceScope.value.slice(7)];
          return { ...draft, trace: draft?.trace?.map(event => ({ ...event, phase: 'draft_risk_letter', case_key: draft.org_name })), title: draft ? '风险报告 · ' + draft.org_name : '风险报告' };
        }
        const failed = task.status === 'failed';
        const source = failed ? scan.value?.selection.diagnostics : scan.value;
        return { trace: source?.trace, runtime: source?.runtime, title: failed ? '归并失败诊断' : 'Pi 风险归并',
          mode: scan.value?.mode || task.selection.mode, error: failed ? task.selection.error : undefined,
          truncated: failed ? scan.value?.selection.diagnostics?.truncated : undefined };
      }
      if (task.kind === 'code') {
        const source = task.status === 'failed' ? task.selection.diagnostics : codeDetail.value?.result;
        return { ...source, title: '任务轨迹', mode: task.selection.mode, error: task.status === 'failed' ? task.selection.error : undefined };
      }
      if (traceScope.value === 'failure') return { ...task.selection.diagnostics, title: '失败诊断', mode: task.selection.mode, error: task.selection.error };
      if (traceScope.value === 'case') return { ...selectedCase.value?.analysis, title: '行社轨迹' };
      return { ...task.selection, title: '指标选择' };
    });
    // 旧归档可能没有 analysis.status，已完成任务仍可查看当时保存的建议。
    const caseComplete = computed(() => current.value?.status === 'completed' && !!selectedCase.value?.analysis && (selectedCase.value.analysis.status === undefined || selectedCase.value.analysis.status === 'completed'));
    const pickedDate = computed(() => current.value
      ? current.value.selection.task_context?.as_of || currentSnapshot.value?.data_date || ''
      : quickbiScan.value ? businessDate.value : selectedSnapshot.value?.data_date || '');
    const validation = computed(() => scan.value?.validation_summary);
    const visibleCount = computed(() => validation.value?.visibility?.visible_org_count ?? validation.value?.total_org_count);

    const isQuickbi = computed(() => current.value
      ? currentSnapshot.value?.source === 'quickbi' || String(current.value.snapshot_id).startsWith('quickbi:')
      : quickbiScan.value);
    const sourceLabel = computed(() => isQuickbi.value ? 'QuickBI OLAP' : (current.value ? scan.value?.is_demo ?? currentSnapshot.value?.is_demo : selectedSnapshot.value?.is_demo) ? '本地演示库' : '本地快照');
    const agreement = computed(() => scan.value ? reconciliationState(scan.value) : null);
    function historyTitle(task: Task) {
      if (task.kind !== 'risk_scan') return taskTitle(task);
      const date = task.selection.task_context?.as_of || snapshots.value.find(s => String(s.id) === String(task.snapshot_id))?.data_date || '';
      const count = task.summary.org_count;
      return [date, count ? count + ' 家行社扫描' : '全辖扫描'].filter(Boolean).join(' · ');
    }
    const heading = computed(() => {
      const task = current.value;
      if (!task) return taskKind.value === 'code' ? '新建自主代码分析' : '新建全辖扫描';
      if (task.kind !== 'risk_scan') return taskTitle(task);
      const date = pickedDate.value;
      const parsed = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
      let period = date;
      if (parsed && new Date(Number(parsed[1]), Number(parsed[2]), 0).getDate() === Number(parsed[3])) {
        period = parsed[1] + '年' + parsed[2] + '月末';
      }
      const count = visibleCount.value ?? task.summary.org_count;
      const scope = count == null ? '全辖行社' : (validation.value?.visibility?.clipped ? '可见 ' : '全体 ') + count + ' 家行社';
      return [period, scope + '风险扫描'].filter(Boolean).join(' · ');
    });
    const viewNames = { result: '总览与分析', trace: '执行轨迹', report: '风险报告' } as const;
    function exportView() {
      const region = content.value;
      if (!region || !current.value) return;
      const title = heading.value + ' · ' + viewNames[view.value];
      const lines = [title, sourceLabel.value + ' · ' + modeLabel.value, region.innerText.trim()];
      const url = URL.createObjectURL(new Blob(['\uFEFF' + lines.join('\n\n')], { type: 'text/plain;charset=utf-8' }));
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = title + '.txt'; anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }

    async function ask() {
      if (busy.value || !conversationScope.value || conversation.reading.value ||
          conversation.readError.value || !conversation.question.value.trim()) return;
      busy.value = 'ask';
      view.value = 'result';
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
      if (draftUrl.value.startsWith('blob:')) URL.revokeObjectURL(draftUrl.value);
      draft.value = ''; draftUrl.value = '';
    }
    // 自主分析报告由归档结果即时导出。
    async function previewCodeDraft() {
      const task = current.value;
      if (busy.value || task?.kind !== 'code' || task.status !== 'completed') return;
      busy.value = 'draft'; error.value = '';
      try {
        const result = await send<{ content: string }>(`/api/code-runs/${encodeURIComponent(String(task.id))}/draft`, {});
        clearDraft();
        draft.value = result.content;
        draftUrl.value = URL.createObjectURL(new Blob(['\uFEFF', result.content], { type: 'text/plain;charset=utf-8' }));
      } catch (err) { error.value = err instanceof Error ? err.message : String(err); }
      finally { busy.value = ''; }
    }
    function clearResult() {
      pollError.value = '';
      current.value = null; codeDetail.value = null; cases.value = []; caseId.value = ''; selectedCase.value = null;
      scan.value = null; selectedSociety.value = null; followScope.value = 'run';
      caseLoading.value = false; caseListReady.value = false; clearDraft();
    }
    async function loadHistory() {
      const [scans, monitor, code] = await Promise.all([
        api<RiskScan[]>('/api/risk-scans'), api<Run[]>('/api/runs'), api<Run[]>('/api/code-runs'),
      ]);
      history.value = [
        ...scans.map(run => asTask(run, 'risk_scan')),
        ...monitor.map(run => asTask(run, 'monitor')),
        ...code.map(run => asTask(run, 'code')),
      ].sort(compareHistory);
    }
    function selectSociety(society: ScanSociety | null) {
      selectedSociety.value = society;
      followScope.value = society?.issues.length ? 'society' : 'run';
    }
    function showSociety(society: ScanSociety | null, fromRow = true) {
      selectSociety(society);
      if (fromRow && society && window.matchMedia('(max-width: 800px)').matches) {
        nextTick(() => document.querySelector('.detail-column')?.scrollIntoView({ block: 'start' }));
      }
    }
    watch(() => conversationScope.value?.caseId, () => {
      if (current.value?.kind === 'risk_scan' && traceScope.value.startsWith('turn:')) traceScope.value = 'scan';
    });
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
    async function openTask(task: Task, preferredCaseId = '', receivedScan?: RiskScan) {
      const version = ++taskVersion;
      ++caseVersion; clearResult(); error.value = ''; notice.value = ''; detailLoading.value = true;
      current.value = task; sidebarOpen.value = false;
      traceScope.value = task.kind === 'risk_scan' ? 'scan' : task.kind === 'code' ? 'code' : task.status === 'failed' ? 'failure' : 'selection';
      try {
        if (task.kind === 'risk_scan') {
          const detail = receivedScan ?? await api<RiskScan>(`/api/risk-scans/${encodeURIComponent(String(task.id))}`);
          if (version !== taskVersion) return;
          scan.value = detail;
          current.value = asTask(detail, 'risk_scan');
          remember(current.value);
          selectSociety(detail.society_results?.find(item => item.case_id === preferredCaseId) ?? firstOverviewSociety(detail));
        } else if (task.kind === 'code') {
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
      resetTaskForm();
    }
    function resetTaskForm() {
      ++taskVersion; ++caseVersion; clearResult(); detailLoading.value = false;
      taskKind.value = 'risk_scan'; topic.value = ''; view.value = 'result';
      dimensionIds.value = executableDimensionIds(boot.value?.risk_dimensions ?? []);
      importFile.value = null; importResult.value = null;
      error.value = ''; notice.value = ''; sidebarOpen.value = false;
    }
    async function selectRemainingTask() {
      if (!current.value || history.value.some(item => item.key === current.value?.key)) return;
      view.value = 'result';
      if (history.value.length) await openTask(history.value[0]);
      else resetTaskForm();
    }
    async function removeHistory(task?: Task) {
      if (busy.value || (task ? !canDeleteTask(task) : !hasFailedTasks.value)) return;
      const message = task ? `确认删除「${historyTitle(task)}」及其归档？` : '确认清理失败的历史任务？已定稿或已下发的风险报告会保留。';
      if (!window.confirm(message)) return;
      busy.value = 'delete-history'; error.value = ''; notice.value = '';
      try {
        if (task) {
          await api(taskResource(task), { method: 'DELETE' });
          history.value = history.value.filter(item => item.key !== task.key);
          await selectRemainingTask();
          notice.value = '已删除历史任务';
        } else {
          const result = await api<{ deleted_count: number; deleted_ids: string[] }>('/api/risk-scans/cleanup-failed', { method: 'POST' });
          const removed = new Set(result.deleted_ids);
          history.value = history.value.filter(item => !removed.has(String(item.id)));
          try {
            await loadHistory();
          } finally {
            await selectRemainingTask();
            notice.value = `已清理 ${result.deleted_count} 条失败任务`;
          }
        }
      } catch (err) {
        error.value = err instanceof Error ? err.message : String(err);
      } finally { busy.value = ''; sidebarOpen.value = false; }
    }
    async function initialize() {
      loading.value = true; fatal.value = '';
      try {
        boot.value = await api<Bootstrap>('/api/bootstrap');
        snapshotId.value = String(snapshots.value[0]?.id ?? '');
        await loadHistory();
        const latest = history.value.find(item => item.kind === 'risk_scan' && item.status === 'completed')
          ?? history.value.find(item => item.kind === 'risk_scan');
        if (latest) await openTask(latest);
        else newTask();
      } catch (err) { fatal.value = err instanceof Error ? err.message : String(err); }
      finally { loading.value = false; }
    }
    async function refresh() {
      if (busy.value) return;
      busy.value = 'refresh'; error.value = '';
      const key = current.value?.key, previousView = view.value, previousCase = selectedSociety.value?.case_id ?? caseId.value, previousScope = traceScope.value, previousFollowScope = followScope.value;
      try {
        await loadHistory();
        const task = history.value.find(item => item.key === key);
        if (task) await openTask(task, previousCase);
        if (selectedSociety.value) followScope.value = previousFollowScope;
        view.value = previousView;
        const sameConversation = previousScope.startsWith('turn:') && key === current.value?.key &&
          previousCase === (selectedSociety.value?.case_id ?? caseId.value) && previousFollowScope === followScope.value;
        if (sameConversation || traceOptions.value.some(item => item.value === previousScope)) traceScope.value = previousScope;
      }
      catch (err) { error.value = err instanceof Error ? err.message : String(err); }
      finally { busy.value = ''; }
    }

    function remember(task: Task) {
      const items = [...history.value.filter(item => item.key !== task.key), task];
      history.value = items.sort(compareHistory);
    }

    async function displayScan(detail: RiskScan) {
      const task = asTask(detail, 'risk_scan');
      remember(task);
      await openTask(task, '', detail);
    }

    async function start(retry = false) {
      if (busy.value || detailLoading.value) return;
      const original = retry ? current.value : null;
      if (retry && (original?.kind !== 'risk_scan' || original.status !== 'failed')) return;
      if (!retry && !canStart.value) return;
      const kind = retry ? 'risk_scan' : taskKind.value;
      busy.value = retry ? 'retry' : 'run'; view.value = 'result';
      error.value = ''; notice.value = '';
      try {
        if (kind === 'risk_scan') {
          // 失败重试从归档恢复快照、勾选范围和补充说明。
          const body = original
            ? { snapshot_id: original.snapshot_id, dimension_ids: original.selection.dimension_ids,
                focus_note: original.selection.focus_note, retry: true }
            : { snapshot_id: quickbiScan.value ? businessDate.value : snapshotId.value, dimension_ids: dimensionIds.value, focus_note: topic.value.trim() };
          await displayScan(await send<RiskScan>('/api/risk-scans', body));
        } else {
          const body = { snapshot_id: snapshotId.value, risk_topic: topic.value.trim() };
          const task = asTask(await send<Run>('/api/code-runs', body), kind);
          remember(task);
          await openTask(task);
        }
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        const detail = err instanceof ApiError ? err.detail : null;
        if (detail && typeof detail === 'object' && 'run_id' in detail && typeof detail.run_id === 'string') {
          try {
            if (kind === 'risk_scan') {
              const failed = await api<RiskScan>('/api/risk-scans/' + encodeURIComponent(detail.run_id));
              await displayScan(failed);
            } else {
              await loadHistory();
              const task = history.value.find(item => item.kind === kind && item.id === detail.run_id);
              if (task) await openTask(task);
            }
          } catch (readError) {
            error.value = message + '。读取失败记录时出错：' + (readError instanceof Error ? readError.message : String(readError));
            return;
          }
        }
        error.value = message;
      } finally { busy.value = ''; }
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
    return { boot, loading, fatal, error, notice, busy, detailLoading, sidebarOpen, visibleHistory: history, current, codeDetail, content,
      cases, caseId, selectedCase, caseLoading, view, traceScope, traceOptions, activeTrace, onDrafted, topic, taskKind, snapshotId, dimensionIds, scan, selectedSociety, showSociety, followScope, canStart, submitting, quickbiScan, scopeLabel, businessDate,
      societies, snapshots, selectedSnapshot, currentSnapshot, modeLabel, composerMode, heading, validation, visibleCount, sourceLabel, pickedDate, agreement, historyTitle, viewNames, exportView, importFile, importResult, draft, draftUrl, caseComplete,
      initialize, refresh, chooseTask, newTask, removeHistory, canDeleteTask, hasFailedTasks, loadCase, start, chooseFile, upload, fileUrl, findingEvidence, organizationName, statusName, taskTitle, previewCodeDraft,
      startReports, startingRun, syncReports, pollError, selectSociety,
      followUpMode, conversationScope, ...conversation, ask, conversationEnd, agentModeName };
  },
  template: `
    <div v-if="loading" class="initial-loading" role="status">正在读取工作台…</div>
    <main v-else-if="fatal" class="initial-loading"><h1>无法连接工作台</h1><p role="alert">{{ fatal }}</p><t-button size="small" @click="initialize">重新连接</t-button></main>
    <div v-else-if="boot" class="workbench" :class="{'history-open':sidebarOpen}" @keydown.esc="sidebarOpen=false">
      <button v-if="sidebarOpen" class="sidebar-scrim" aria-label="关闭侧栏" @click="sidebarOpen=false"></button>
      <aside class="history-sidebar" aria-label="任务历史">
        <div class="brand"><i class="brand-symbol" aria-hidden="true"></i><strong>风险提示智能体</strong><button class="sidebar-close icon-action" @click="sidebarOpen=false" aria-label="收起任务历史"><close-icon/></button></div>
        <t-button class="new-task" size="small" theme="primary" variant="outline" @click="newTask" :disabled="!!busy"><template #icon><add-icon/></template>新建全辖扫描</t-button>
        <div class="history-heading"><span>任务历史</span>
          <button v-if="hasFailedTasks" @click="removeHistory()" :disabled="!!busy">清理失败</button></div>
        <nav class="history-list" aria-label="选择历史任务">
          <div v-for="task in visibleHistory" :key="task.key" class="history-row" :class="{active:current?.key===task.key}">
            <button class="history-item" :aria-current="current?.key===task.key?'page':undefined" @click="chooseTask(task)" :disabled="!!busy" :title="[historyTitle(task),task.metadata,statusName(task.status)].filter(Boolean).join(' · ')">
              <span class="history-title"><span>{{ historyTitle(task) }}</span><small v-if="task.status==='failed' || task.status==='running'">{{ statusName(task.status) }}</small></span>
              <span v-if="task.metadata" class="history-meta">{{ task.metadata }}</span>
            </button>
            <button v-if="canDeleteTask(task)" class="history-delete" :disabled="!!busy" :aria-label="'删除任务 '+historyTitle(task)+' '+task.metadata" title="删除任务" @click="removeHistory(task)"><delete-icon/></button>
          </div>
          <p v-if="!visibleHistory.length" class="history-empty">扫描完成后可在这里回看。</p>
        </nav>
        <footer class="source-status"><i :class="{clipped:validation?.visibility?.clipped, unknown:visibleCount==null}"></i><span>{{ sourceLabel }} · {{ visibleCount==null ? '待扫描' : visibleCount+'/'+(validation?.visibility?.expected_org_count ?? visibleCount)+' 可见' }}</span></footer>
      </aside>
      <main class="workspace">
        <header class="workspace-header">
          <button class="history-toggle icon-action" aria-label="切换任务历史" :aria-expanded="sidebarOpen" @click="sidebarOpen=!sidebarOpen"><menu-icon/></button>
          <h1 :title="heading">{{ heading }}</h1>
          <div class="header-tools">
            <span v-if="current && current.status!=='completed'" class="source-pill" data-tone="warning">{{ statusName(current.status) }}</span>
            <span v-if="pickedDate" class="date-stamp"><time>{{ pickedDate }}</time><calendar-icon/></span>
            <t-button v-if="current?.kind==='risk_scan' && current.status==='failed'" size="small" variant="outline" @click="start(true)" :disabled="!!busy">重试失败任务</t-button>
            <t-button v-if="current" size="small" variant="text" class="export-view" aria-label="导出当前视图" title="导出当前视图" @click="exportView" :disabled="detailLoading"><template #icon><download-icon/></template><span class="export-label">导出</span></t-button>
          </div>
        </header>
        <nav class="workspace-tabs" aria-label="当前任务视图">
          <button v-for="(name,key) in viewNames" :key="key" :class="{active:view===key}" :aria-current="view===key?'page':undefined" @click="view=key" :disabled="key!=='result' && (!current || submitting || detailLoading)">{{ name }}</button>
        </nav>
        <div v-if="error && error!==current?.selection.error" class="notice error" role="alert"><span>{{ error }}</span><button @click="error=''" aria-label="关闭错误提示"><close-icon/></button></div>
        <div v-if="notice" class="notice" role="status">{{ notice }}</div>
        <div v-if="pollError" class="notice warning" role="status">{{ pollError }}</div>
        <section ref="content" class="workspace-content" :class="{'scroll-view':!current || view==='report' || (view==='result' && current.kind!=='risk_scan')}">
          <div v-if="submitting || detailLoading" class="busy-state" role="status"><h2>{{ submitting ? '正在执行分析' : '正在读取归档' }}</h2><p>{{ submitting ? '结果将在任务完成后显示。' : '正在读取所选任务的记录。' }}</p><div class="loading-lines"><i></i><i></i></div></div>
          <form v-else-if="!current" class="setup-form" @submit.prevent="start()">
            <scan-setup v-if="taskKind==='risk_scan'" :scope-label="scopeLabel" :dimensions="boot.risk_dimensions" v-model="dimensionIds"/>
            <h2 v-else>自主代码分析</h2>
            <div class="setup-fields"><label v-if="quickbiScan">业务日<input type="date" v-model="businessDate" required aria-label="QuickBI 业务日期"></label>
              <label v-else>数据快照<select v-model="snapshotId" aria-label="数据快照"><option v-for="s in snapshots" :key="s.id" :value="String(s.id)">{{ s.name }} · {{ s.data_date }}{{ s.is_demo?' · 模拟数据':'' }}</option></select></label>
              <label>分析方式<select v-model="taskKind" aria-label="分析方式"><option value="risk_scan">全辖风险扫描</option><option value="code">自主代码分析</option></select></label></div>
            <label class="focus-note">{{ taskKind==='code' ? '分析任务' : '补充关注点（可选）' }}<textarea rows="2" maxlength="500" v-model="topic" placeholder="填写需要重点核查的问题" @keydown.ctrl.enter.prevent="start()" @keydown.meta.enter.prevent="start()"></textarea></label>
            <details v-if="!quickbiScan" class="import-section"><summary>数据导入</summary><div class="import-tools">
              <input type="file" accept=".csv,.xlsx" :disabled="!!busy" @change="chooseFile" aria-label="选择 CSV 或 XLSX 数据文件">
              <t-button type="button" size="small" variant="outline" :disabled="!!busy || !importFile" @click="upload">{{ busy==='import'?'导入中…':'导入文件' }}</t-button>
              <a href="/api/import/template.csv" download>下载模板</a></div><p v-for="w in importResult?.warnings" :key="w">{{ w }}</p></details>
            <p v-if="taskKind==='code' && societies.length!==82" class="notice">当前目录有 {{ societies.length }} 家行社，暂不能发起全辖代码分析。</p>
            <div class="setup-actions"><span>{{ composerMode }}{{ taskKind==='code'?' · Python 需要 Docker 环境':'' }}</span><t-button theme="primary" size="small" type="submit" :disabled="!canStart">{{ taskKind==='code'?'运行全辖代码分析':'扫描'+scopeLabel }}</t-button></div>
          </form>
          <template v-else-if="view==='trace'">
            <trace-panel :key="current.key+traceScope+caseId+current.status" :trace="activeTrace.trace" :runtime="activeTrace.runtime" :mode="activeTrace.mode" :error="activeTrace.error" :truncated="activeTrace.truncated" :title="activeTrace.title" :scan="traceScope==='scan'?scan:undefined" :turn="turns.find(t=>'turn:'+t.id===traceScope)" :round="turns.findIndex(t=>'turn:'+t.id===traceScope)+1">
              <template #source><select v-model="traceScope" aria-label="轨迹来源"><option v-for="item in traceOptions" :key="item.value" :value="item.value">{{ item.label }}</option></select>
                <select v-if="current.kind==='monitor' && traceScope==='case' && cases.length>1" :value="caseId" @change="loadCase($event.target.value)" :disabled="caseLoading || !!busy" aria-label="轨迹行社"><option v-for="item in cases" :key="item.id" :value="String(item.id)">{{ item.org_name }}</option></select></template>
            </trace-panel>
          </template>
          <template v-else-if="view==='report'">
            <society-panel v-if="scan" :run-id="scan.id" :society="selectedSociety" report
              :diagnostics="selectedSociety && scan.letter_drafts?.[selectedSociety.org_id]" :auto-reports="scan.auto_reports" @sync-reports="syncReports"
              @drafted="onDrafted" @letter-trace="traceScope='letter:'+$event; view='trace'"/>
            <section v-else class="legacy-report"><h2>风险报告</h2><button v-if="codeDetail && current.status==='completed'" class="button" @click="previewCodeDraft" :disabled="!!busy">{{ draft?'刷新报告':'生成风险报告' }}</button>
              <template v-if="draft"><a class="text-link" :href="draftUrl" download="风险报告.txt">下载风险报告</a><pre>{{ draft }}</pre></template>
              <p v-else>当前任务尚无风险报告。</p></section>
          </template>
          <template v-else>
            <section v-if="current.status==='failed' || current.status==='running'" class="task-state"><h2>{{ current.status==='failed'?'分析未完成':'任务仍在执行' }}</h2>
              <p>{{ current.selection.error || '结果尚未就绪，可刷新读取当前状态。' }}</p>
              <button class="button" @click="view='trace'">查看执行轨迹</button><button class="button" @click="refresh" :disabled="!!busy">刷新任务</button></section>

            <section v-if="current.kind!=='risk_scan'" class="legacy-results">
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

              <button v-if="draft || (codeDetail && current.status==='completed')" class="button" @click="view='report'">查看风险报告</button>
              <template v-if="followUpMode"><conversation-panel :turns="turns" :scope="followScope" :society-name="selectedSociety?.org_name" :busy="!!busy" :scope-switch="current.kind==='risk_scan'" @scope="followScope=$event" @trace="view='trace'; traceScope='turn:'+$event" /><div ref="conversationEnd"></div></template>
            </section>
          </template>
            <overview-panel v-if="scan && !detailLoading && !submitting" v-show="view==='result'" :key="scan.id" :scan="scan" :selected-org-id="selectedSociety?.org_id"
              :starting-reports="startingRun===scan.id" @select="showSociety" @report="selectSociety($event); view='report'" @start-reports="startReports">
              <div class="detail-column"><div class="detail-scroll">
                <society-panel v-if="view==='result'" :run-id="scan.id" :society="selectedSociety" :auto-reports="scan.auto_reports"
                  @sync-reports="syncReports" @drafted="onDrafted" @report="view='report'"/>
                <template v-if="view==='result' && followUpMode"><conversation-panel :turns="turns" :scope="followScope" :society-name="selectedSociety?.org_name" :busy="!!busy" :scope-switch="current.kind==='risk_scan'" @scope="followScope=$event" @trace="view='trace'; traceScope='turn:'+$event" /><div ref="conversationEnd"></div></template>
              </div><template v-if="view==='result' && followUpMode"><followup-composer v-model="question" :sending="busy==='ask'" :disabled="!!busy || reading || !conversationScope || !!readError" :mode="composerMode" :smartq="!!boot.meta.smartq_followup_available" :read-error="readError" :send-error="sendError" @ask="ask" @reload="loadConversation" /></template></div>
            </overview-panel>
        </section>
        <template v-if="current && followUpMode && !detailLoading && (view==='trace' || (view==='result' && !scan))"><followup-composer v-model="question" :sending="busy==='ask'" :disabled="!!busy || reading || !conversationScope || !!readError" :mode="composerMode" :smartq="!!boot.meta.smartq_followup_available" :read-error="readError" :send-error="sendError" @ask="ask" @reload="loadConversation" /></template>
      </main>
    </div>`,
}).mount('#app');

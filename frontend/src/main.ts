import { createApp, computed, onMounted, ref } from 'vue';
import { api, send } from './api';
import './styles.css';

// 类型对应后端 JSON 契约。金额/比率保留服务端字符串，页面不重新计算规则。
type ID = string | number;
interface Organization { id: ID; code: string; name: string; level: number; parent_id: ID | null; region_id: ID | null }
interface Indicator { id: string; name: string; unit: string; frequency: string; description: string }
interface Rule { id: ID; name: string; kind: 'rank' | 'ratio' | 'exists'; indicator_id: string; enabled: boolean; params: Record<string, unknown>; version: number; needs_config: boolean; description: string }
interface Snapshot { id: ID; name: string; data_date: string; source: string; is_demo: boolean; row_count: number; status: string }
interface Summary { evaluated?: number; triggered?: number; normal?: number; insufficient?: number; not_applicable?: number; unconfigured?: number; case_count?: number }
interface Selection { mode: string; topic?: string; indicator_ids: string[]; reasons: unknown[]; limitations: string[] }
interface Run { id: ID; status: string; snapshot_id: ID; created_at: string; summary: Summary; selection: Selection }
interface Evidence { id: ID; indicator_id: string; indicator_name: string; value: string | number | null; value_display: string; unit: string; rank: number | null; population: number | null; data_date: string; rule_id: ID; rule_name: string; rule_kind: string; reason: string; quality_status: string }
interface Receipt { id: ID; channel: string; status: string; created_at: string }
interface CheckResult { org_id: ID; org_name: string; rule_id: ID; indicator_id: string; status: string; reason: string }
interface Letter { id: ID; case_id: ID; revision: number; content: string; status: string; evidence_hash: string; created_at: string; receipt: Receipt | null }
interface Analysis { mode: string; facts: string[]; hypotheses: string[]; checks: string[]; limitations: string[] }
interface ImportResult { snapshot_id: ID; row_count: number; warnings: string[] }
// 列表只含摘要；打开详情后，接口才返回 evidence、analysis 与 letter。
interface RiskCase {
  id: ID;
  run_id: ID;
  org_id: ID;
  org_name: string;
  region_name: string;
  data_date: string;
  risk_types: string[];
  status: string;
  evidence_count: number;
  headline: string;
  evidence?: Evidence[];
  analysis?: Analysis;
  letter?: Letter | null;
}
interface Bootstrap { meta: { app_name: string; version: string; demo: boolean; agent_mode: string; data_date: string; rank_note: string }; summary: { region_count: number; org_count: number; indicator_count: number; enabled_rule_count: number; case_count: number; letter_count: number }; organizations: Organization[]; indicators: Indicator[]; rules: Rule[]; snapshots: Snapshot[]; latest_run: Run | null }

createApp({
  setup() {
    // boot 保存目录和概览；当前查看的运行与表单选择分开，切换快照不会改写历史结果。
    const boot = ref<Bootstrap | null>(null);
    const loading = ref(true);
    const fatal = ref('');
    const error = ref('');
    const toast = ref('');
    const busy = ref('');
    const tab = ref('monitor');
    const snapshotId = ref('');
    const topic = ref('关注贷款质量变化与资金业务不良');
    const currentRun = ref<Run | null>(null);
    const runs = ref<Run[]>([]);
    const cases = ref<RiskCase[]>([]);
    const checks = ref<CheckResult[]>([]);
    const showChecks = ref(false);
    const checkFilter = ref('insufficient');
    const regionFilter = ref('');
    const query = ref('');
    const selectedCase = ref<RiskCase | null>(null);
    const drawerTab = ref('evidence');
    const letter = ref<Letter | null>(null);
    const letterText = ref('');
    const ruleInputs = ref<Record<string, string>>({});
    const fileInput = ref<HTMLInputElement | null>(null);
    const importFile = ref<File | null>(null);
    const importResult = ref<ImportResult | null>(null);
    const orgQuery = ref('');
    const indicatorQuery = ref('');
    const nav = [
      { id: 'monitor', title: '监测工作台', subtitle: '风险发现与提示函', symbol: '◫' },
      { id: 'rules', title: '指标与规则', subtitle: '口径和触发条件', symbol: '≡' },
      { id: 'orgs', title: '机构目录', subtitle: '五级机构结构', symbol: '⌘' },
      { id: 'data', title: '数据管理', subtitle: '导入与快照', symbol: '▤' },
      { id: 'runs', title: '运行记录', subtitle: '检查范围与结果', symbol: '◷' }
    ];
    const titles: Record<string, { title: string; subtitle: string }> = {
      monitor: { title: '行社风险监测', subtitle: '从指标异常到风险提示，每一项结论都有据可查。' },
      rules: { title: '指标与监测规则', subtitle: '把业务口径配置清楚，再运行一致、可追溯的检查。' },
      orgs: { title: '机构目录', subtitle: '本期覆盖行社，机构结构预留至第五级。' },
      data: { title: '数据管理', subtitle: '导入数据后形成独立快照，已运行的证据保持不变。' },
      runs: { title: '运行记录', subtitle: '查看每一批次的检查覆盖、异常和数据缺口。' }
    };
    const pageTitle = computed(() => titles[tab.value]);
    const selectedSnapshot = computed(() => boot.value?.snapshots.find((item) => String(item.id) === snapshotId.value));
    const currentRunSnapshot = computed(() => boot.value?.snapshots.find((item) => String(item.id) === String(currentRun.value?.snapshot_id)));
    const activeSummary = computed(() => currentRun.value?.summary ?? {});
    const regions = computed(() => boot.value?.organizations.filter((org) => org.level === 2) ?? []);
    const societies = computed(() => boot.value?.organizations.filter((org) => org.level === 3) ?? []);
    const visibleCases = computed(() => cases.value.filter((item) => (!regionFilter.value || item.region_name === regionFilter.value) && (!query.value || `${item.org_name} ${item.headline} ${item.region_name}`.includes(query.value))));
    const visibleIndicators = computed(() => boot.value?.indicators.filter((indicator) => !indicatorQuery.value || `${indicator.id} ${indicator.name} ${indicator.description}`.toLowerCase().includes(indicatorQuery.value.toLowerCase())) ?? []);
    const visibleChecks = computed(() => checks.value.filter((item) => item.status === checkFilter.value));
    // letter 保存服务端已持久化版本，letterText 是编辑缓冲；未保存内容不能审核或下发。
    const letterDirty = computed(() => Boolean(letter.value && letterText.value !== letter.value.content));
    const configuredRules = computed(() => boot.value?.rules.filter((rule) => rule.enabled && !rule.needs_config).length ?? 0);
    const coverageTotal = computed(() => Number(activeSummary.value.normal ?? 0) + Number(activeSummary.value.triggered ?? 0) + Number(activeSummary.value.insufficient ?? 0));
    const coverageWidth = (key: keyof Summary) => coverageTotal.value ? `${100 * Number(activeSummary.value[key] ?? 0) / coverageTotal.value}%` : '0%';
    const number = (value: unknown) => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 2 }).format(Number(value ?? 0));
    const shortId = (value: unknown) => String(value ?? '').slice(0, 10);
    const dateTime = (value?: string) => {
      if (!value) return '—';
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
    };
    const indicatorName = (id: string) => boot.value?.indicators.find((item) => item.id === id)?.name ?? id;
    const snapshotName = (id: ID) => boot.value?.snapshots.find((item) => String(item.id) === String(id))?.name ?? `快照 ${shortId(id)}`;
    const ruleKind = (kind: string) => ({ rank: '排名', ratio: '占比', exists: '存在提醒' }[kind] ?? kind);
    const agentModeLabel = (mode?: string) => ({ 'pi-demo': 'Pi Agent · 离线演示', 'pi-model': 'Pi Agent · 模型分析', template: '模板分析' }[mode ?? ''] ?? '分析模式未知');
    const selectionMethod = (mode?: string) => mode === 'pi-demo' || mode === 'pi-model' ? 'Pi 工具调用' : mode === 'template' ? '目录匹配' : '分析模式未知';
    const agentModeDescription = (mode?: string) => ({
      'pi-demo': 'Pi Agent 执行工具调用流程，模型响应由本地固定逻辑模拟，未调用真实模型或小Q。必查规则始终完整执行。',
      'pi-model': 'Pi Agent 调用已配置的模型，通过业务工具补充指标与分析。模型不改变已生效的规则；具体原因仍需核查。',
      template: '此记录使用原有目录匹配与模板分析，未调用真实模型。'
    }[mode ?? ''] ?? '未识别此记录的分析模式，请核对运行配置。');
    const statusName = (status: string) => ({ open: '待处理', detected: '待处理', draft: '函件草稿', approved: '已审核', simulated_sent: '已模拟下发', completed: '已完成', complete: '已完成', running: '运行中', failed: '运行失败', ready: '可用' }[status] ?? status);
    const statusClass = (status: string) => status === 'simulated_sent' ? 'muted' : status === 'approved' || status === 'completed' ? 'green' : status === 'draft' ? 'blue' : status === 'failed' ? 'red' : 'amber';
    const reasonText = (value: unknown) => typeof value === 'string' ? value : value && typeof value === 'object' ? String((value as Record<string, unknown>).reason ?? (value as Record<string, unknown>).description ?? JSON.stringify(value)) : String(value);
    const children = (region: Organization) => societies.value.filter((org) => (String(org.parent_id) === String(region.id) || String(org.region_id) === String(region.id)) && (!orgQuery.value || `${org.name} ${org.code} ${region.name}`.includes(orgQuery.value)));
    const notify = (message: string) => {
      toast.value = message;
      window.setTimeout(() => {
        if (toast.value === message) toast.value = '';
      }, 4500);
    };
    // 创建、保存、审核与下发都采用接口返回的完整函件，统一同步正文和版本。
    const setLetter = (item: Letter | null) => {
      letter.value = item;
      letterText.value = item?.content ?? '';
      if (selectedCase.value) selectedCase.value.letter = item;
    };

    // 首次进入恢复最近一次结果；操作后的刷新只更新目录/概览，保留当前查看的批次。
    async function loadBootstrap(initial = false) {
      const data = await api<Bootstrap>('/api/bootstrap');
      boot.value = data;
      if (!snapshotId.value || !data.snapshots.some((item) => String(item.id) === snapshotId.value)) {
        snapshotId.value = String(data.snapshots[0]?.id ?? '');
      }
      for (const rule of data.rules) {
        ruleInputs.value[String(rule.id)] = String(rule.kind === 'rank' ? rule.params.cutoff ?? 72 : rule.params.threshold ?? '');
      }
      if (initial) {
        currentRun.value = data.latest_run;
        if (data.latest_run) {
          snapshotId.value = String(data.latest_run.snapshot_id);
          await loadCases(data.latest_run.id);
        }
      }
      runs.value = await api<Run[]>('/api/runs');
    }

    async function initialize() {
      loading.value = true;
      fatal.value = '';
      try {
        await loadBootstrap(true);
      } catch (err) {
        fatal.value = err instanceof Error ? err.message : String(err);
      } finally {
        loading.value = false;
      }
    }

    // 用户操作共享忙碌锁和错误出口，避免重复提交；失败时保留页面数据供核对或重试。
    async function action(name: string, operation: () => Promise<void>) {
      if (busy.value) return;
      busy.value = name;
      error.value = '';
      try {
        await operation();
      } catch (err) {
        error.value = err instanceof Error ? err.message : String(err);
      } finally {
        busy.value = '';
      }
    }

    async function loadCases(runId: ID) {
      cases.value = await api<RiskCase[]>(`/api/cases?run_id=${encodeURIComponent(String(runId))}`);
    }

    const runAnalysis = () => action('run', async () => {
      if (!snapshotId.value) throw new Error('请先选择一个可用数据快照。');
      const run = await send<Run>('/api/runs', { snapshot_id: snapshotId.value, risk_topic: topic.value });
      currentRun.value = run;
      showChecks.value = false;
      checks.value = [];
      await loadCases(run.id);
      await loadBootstrap();
      tab.value = 'monitor';
      regionFilter.value = '';
      query.value = '';
      notify(`监测完成：${run.summary.case_count ?? cases.value.length} 家行社形成风险事项。`);
    });
    const openRun = (run: Run) => action('history', async () => {
      currentRun.value = run;
      snapshotId.value = String(run.snapshot_id);
      showChecks.value = false;
      checks.value = [];
      await loadCases(run.id);
      tab.value = 'monitor';
      regionFilter.value = '';
      query.value = '';
    });
    const viewChecks = () => action('checks', async () => {
      if (!currentRun.value) return;
      checks.value = await api<CheckResult[]>(`/api/runs/${currentRun.value.id}/checks`);
      showChecks.value = !showChecks.value;
    });
    const openCase = (item: RiskCase) => action('case', async () => {
      const detail = await api<RiskCase>(`/api/cases/${encodeURIComponent(String(item.id))}`);
      selectedCase.value = detail;
      setLetter(detail.letter ?? null);
      drawerTab.value = 'evidence';
    });
    const closeCase = () => {
      selectedCase.value = null;
      error.value = '';
    };

    const createDraft = () => action('draft', async () => {
      if (!selectedCase.value) return;
      setLetter(await send<Letter>(`/api/cases/${selectedCase.value.id}/draft`, {}));
      drawerTab.value = 'letter';
      if (currentRun.value) await loadCases(currentRun.value.id);
      notify('提示函草稿已生成，数字与证据保持绑定。');
    });
    // expected_revision 让后端拒绝过期编辑/审核；正文保存后由后端撤销旧审批。
    const saveLetter = () => action('save-letter', async () => {
      if (!letter.value) return;
      setLetter(await send<Letter>(`/api/letters/${letter.value.id}`, {
        content: letterText.value,
        expected_revision: letter.value.revision
      }, 'PATCH'));
      if (currentRun.value) await loadCases(currentRun.value.id);
      notify('已保存新版本；修改后的函件需要重新审核。');
    });
    // 前端阻止未保存正文被审核/下发，后端仍独立校验版本与状态。
    const approveLetter = () => action('approve', async () => {
      if (!letter.value) return;
      if (letterDirty.value) throw new Error('正文有未保存的修改，请先保存后再审核。');
      setLetter(await send<Letter>(`/api/letters/${letter.value.id}/approve`, { expected_revision: letter.value.revision }));
      if (currentRun.value) await loadCases(currentRun.value.id);
      notify('当前版本已审核，可以继续模拟下发。');
    });
    const simulateSend = () => action('send', async () => {
      if (!letter.value) return;
      if (letterDirty.value) throw new Error('正文有未保存的修改，请先保存并重新审核。');
      setLetter(await send<Letter>(`/api/letters/${letter.value.id}/simulate-send`, { expected_revision: letter.value.revision }));
      if (currentRun.value) await loadCases(currentRun.value.id);
      await loadBootstrap();
      notify('模拟下发已完成。未向任何真实机构发送。');
    });
    // 只提交可编辑参数；排名范围、指标与规则版本由后端管理。
    const saveRule = (rule: Rule) => action(`rule-${rule.id}`, async () => {
      const raw = ruleInputs.value[String(rule.id)]?.trim() ?? '';
      const params: { cutoff?: number; threshold?: number | null } = {};
      if (rule.kind === 'rank') {
        const cutoff = Number(raw);
        if (!raw || !Number.isInteger(cutoff) || cutoff < 1 || cutoff > 82) throw new Error('排名起点必须是 1—82 之间的整数。');
        params.cutoff = cutoff;
      } else if (rule.kind === 'ratio') {
        // 空白表示“待配置”，不能把 Number('') 得到的 0 当作正式阈值。
        const threshold = raw === '' ? null : Number(raw);
        if (threshold !== null && (!Number.isFinite(threshold) || threshold < 0 || threshold > 100)) throw new Error('占比阈值应为 0—100 之间的百分比数值，或留空。');
        params.threshold = threshold;
      }
      await send<Rule>(`/api/rules/${rule.id}`, { params }, 'PATCH');
      await loadBootstrap();
      notify('规则已保存。请重新运行监测以使用新配置。');
    });
    const toggleRule = (rule: Rule) => action(`toggle-${rule.id}`, async () => {
      await send<Rule>(`/api/rules/${rule.id}`, { enabled: !rule.enabled }, 'PATCH');
      await loadBootstrap();
      notify('规则状态已更新。历史运行结果保持不变。');
    });
    const chooseFile = (event: Event) => {
      importFile.value = (event.target as HTMLInputElement).files?.[0] ?? null;
      importResult.value = null;
    };
    // 文件内容由导入接口统一校验，成功后只选中新快照，不自动启动监测。
    const upload = () => action('import', async () => {
      if (!importFile.value) throw new Error('请先选择 CSV 或 XLSX 文件。');
      const form = new FormData();
      form.append('file', importFile.value);
      const result = await api<ImportResult>('/api/import', { method: 'POST', body: form });
      importResult.value = result;
      await loadBootstrap();
      snapshotId.value = String(result.snapshot_id);
      notify(`已导入 ${number(result.row_count)} 条记录，形成独立数据快照。`);
    });
    onMounted(initialize);
    return { boot, loading, fatal, error, toast, busy, tab, nav, pageTitle, snapshotId, selectedSnapshot, currentRunSnapshot, topic, currentRun, runs, cases, checks, showChecks, checkFilter, visibleChecks, activeSummary, visibleCases, regions, societies, regionFilter, query, selectedCase, drawerTab, letter, letterText, letterDirty, ruleInputs, fileInput, importFile, importResult, orgQuery, indicatorQuery, visibleIndicators, configuredRules, coverageWidth, number, shortId, dateTime, indicatorName, snapshotName, ruleKind, agentModeLabel, selectionMethod, agentModeDescription, statusName, statusClass, reasonText, children, initialize, runAnalysis, openRun, viewChecks, openCase, closeCase, createDraft, saveLetter, approveLetter, simulateSend, saveRule, toggleRule, chooseFile, upload };
  },
  template: `
    <div v-if="loading" class="initial-loading"><div class="loader"></div><p>正在加载风险监测工作台…</p></div>
    <div v-else-if="fatal" class="connection-error"><div class="brand-mark">风</div><h1>暂时无法连接服务</h1><p>{{ fatal }}</p><button class="button primary" @click="initialize">重新加载</button><small>请确认 Python 服务已启动，然后通过服务地址打开页面。</small></div>
    <div v-else-if="boot" class="app-shell">
      <aside class="sidebar">
        <a class="brand" href="#" @click.prevent="tab='monitor'"><span class="brand-mark">风</span><span><strong>风险提示</strong><small>行社监测工作台</small></span></a>
        <div class="workspace-label">业务工作区 <span>TEST</span></div>
        <nav aria-label="工作区导航"><button v-for="item in nav" :key="item.id" :class="['nav-item', {active:tab===item.id}]" @click="tab=item.id;error=''" :aria-current="tab===item.id?'page':undefined"><span class="nav-symbol" aria-hidden="true">{{ item.symbol }}</span><span>{{ item.title }}<small>{{ item.subtitle }}</small></span><span v-if="item.id==='monitor' && cases.length" class="nav-count">{{ cases.length }}</span></button></nav>
        <div class="sidebar-bottom"><div class="environment-dot"></div><strong>本地测试环境</strong><p>内置数据为合成样例<br>下发流程仅作模拟</p><span class="version">v{{ boot.meta.version || '0.1.0' }}</span></div>
      </aside>

      <main class="main-content">
        <header class="topbar"><div class="breadcrumb">风险管理 <span>/</span> {{ pageTitle.title }}</div><div class="topbar-right"><span class="status-dot"></span><span>{{ agentModeLabel(boot.meta.agent_mode) }}</span><span class="test-tag">测试版</span></div></header>
        <div class="page-content">
          <div class="page-heading"><div><div class="eyebrow">RISK MONITORING</div><h1>{{ pageTitle.title }}</h1><p>{{ pageTitle.subtitle }}</p></div><div v-if="tab==='monitor'" class="heading-actions"><button class="button secondary" @click="tab='data'">导入数据</button><button class="button primary" :disabled="!!busy || !snapshotId" @click="runAnalysis"><span v-if="busy==='run'" class="button-spinner"></span>{{ busy==='run'?'正在监测…':'运行监测' }}<span v-if="busy!=='run'" aria-hidden="true">↗</span></button></div></div>
          <div v-if="error && !selectedCase" class="alert error" role="alert"><strong>操作未完成</strong><span>{{ error }}</span><button @click="error=''" aria-label="关闭错误提示">×</button></div>

          <section v-if="tab==='monitor'" class="monitor-page">
            <div class="scope-strip"><span class="scope-chip">行社级</span><span>{{ boot.summary.region_count }} 个地区</span><span class="divider"></span><span>{{ boot.summary.org_count }} 家行社</span><span class="divider"></span><span>第四、五级机构已预留</span><span class="scope-caption">本期监测范围</span></div>
            <div class="stats-grid">
              <article class="stat-card"><div class="stat-label">覆盖行社<span class="stat-symbol">◫</span></div><div class="stat-value">{{ boot.summary.org_count }}<small>家</small></div><p>全省行社范围</p></article>
              <article class="stat-card accent"><div class="stat-label">待关注行社<span class="stat-symbol">↗</span></div><div class="stat-value">{{ currentRun ? cases.length : '—' }}<small>家</small></div><p>{{ currentRun?'同一行社的多项信号合并展示':'运行监测后生成风险事项' }}</p></article>
              <article class="stat-card"><div class="stat-label">触发规则<span class="stat-symbol">≋</span></div><div class="stat-value">{{ currentRun ? number(activeSummary.triggered) : '—' }}<small>项</small></div><p>{{ configuredRules }} 条已配置规则参与检查</p></article>
              <article class="stat-card"><div class="stat-label">无法判断<span class="stat-symbol">!</span></div><div class="stat-value" :class="{warning:activeSummary.insufficient}">{{ currentRun ? number(activeSummary.insufficient) : '—' }}<small>项</small></div><p>缺数或口径不足，单独记录</p></article>
            </div>
            <section class="panel run-config"><div class="panel-title"><h2>本次监测</h2><span v-if="selectedSnapshot" :class="['tag',selectedSnapshot.is_demo?'blue':'muted']">{{ selectedSnapshot.is_demo?'合成样例':'用户导入' }}</span></div><div class="run-config-fields"><label class="field"><span>数据快照</span><select v-model="snapshotId"><option v-for="snapshot in boot.snapshots" :value="String(snapshot.id)" :key="snapshot.id">{{ snapshot.name }} · {{ snapshot.data_date }}</option></select></label><label class="field topic-field"><span>补充关注的风险主题</span><input v-model="topic" placeholder="例如：贷款质量变化、资金业务不良" maxlength="200" /></label></div><div class="config-footnote"><span>必查规则始终完整执行；主题仅用于选择辅助分析指标。</span><button class="text-button" @click="tab='rules'">查看规则 →</button></div></section>

            <section v-if="currentRun" class="panel case-panel">
              <div class="panel-title case-title"><div><h2>风险事项 <span class="count-pill">{{ cases.length }}</span></h2><p>数据截至 {{ currentRunSnapshot?.data_date || boot.meta.data_date }} · 运行 {{ shortId(currentRun.id) }}<span v-if="snapshotId!==String(currentRun.snapshot_id)"> · 下次运行将使用新选快照</span></p></div><div class="table-filters"><select v-model="regionFilter" aria-label="筛选地区"><option value="">全部地区</option><option v-for="region in regions" :key="region.id" :value="region.name">{{ region.name }}</option></select><input v-model="query" aria-label="搜索行社" placeholder="搜索行社或风险事项" /></div></div>
              <div class="table-scroll"><table class="cases-table"><thead><tr><th>行社 / 所属地区</th><th>风险信号</th><th>主要发现</th><th>证据</th><th>处理状态</th><th></th></tr></thead><tbody><tr v-for="item in visibleCases" :key="item.id" @click="openCase(item)" tabindex="0" @keydown.enter="openCase(item)"><td><strong class="org-name">{{ item.org_name }}</strong><small>{{ item.region_name }}</small></td><td><div class="tags"><span v-for="kind in item.risk_types" :key="kind" :class="['tag',kind==='存在提醒'||kind==='exists'?'amber':'blue']">{{ ruleKind(kind) }}</span></div></td><td class="headline-cell">{{ item.headline }}</td><td><span class="evidence-count">{{ item.evidence_count }}</span><small>条依据</small></td><td><span :class="['tag',statusClass(item.status)]">{{ statusName(item.status) }}</span></td><td><button class="row-open" @click.stop="openCase(item)" :aria-label="'查看'+item.org_name+'详情'">→</button></td></tr></tbody></table></div>
              <div v-if="!visibleCases.length" class="empty-state compact"><span class="empty-icon">✓</span><h3>{{ cases.length?'没有匹配的风险事项':'当前规则未形成风险事项' }}</h3><p>{{ cases.length?'试试其他地区或搜索词。':'请同时检查下方的数据不足与待配置规则，未触发不代表所有指标均已完成判断。' }}</p></div><footer class="table-footer"><span>显示 {{ visibleCases.length }} / {{ cases.length }} 家行社</span><span>同一事项的证据与函件始终保留对应版本</span></footer>
            </section>
            <section v-else class="panel empty-state"><div class="empty-orbit"><span>↗</span></div><h2>从第一批数据开始</h2><p>已准备 {{ boot.summary.org_count }} 家行社的合成数据和三类规则。<br>运行监测，查看触发依据并试用提示函流程。</p><button class="button primary" @click="runAnalysis" :disabled="!!busy">运行首次监测</button><small>占比阈值尚未配置时，不会被默认为正常。</small></section>

            <div v-if="currentRun" class="bottom-grid"><section class="panel coverage-panel"><div class="panel-title"><h2>检查覆盖</h2><span class="subtle">本次运行</span></div><div class="coverage-bar"><span class="normal" :style="{width:coverageWidth('normal')}"></span><span class="triggered" :style="{width:coverageWidth('triggered')}"></span><span class="insufficient" :style="{width:coverageWidth('insufficient')}"></span></div><div class="coverage-legend"><span><i class="normal"></i>未触发 <b>{{ number(activeSummary.normal) }}</b></span><span><i class="triggered"></i>已触发 <b>{{ number(activeSummary.triggered) }}</b></span><span><i class="insufficient"></i>无法判断 <b>{{ number(activeSummary.insufficient) }}</b></span></div><div class="coverage-note"><span>待配置 {{ number(activeSummary.unconfigured) }} 项</span><span>不适用 {{ number(activeSummary.not_applicable) }} 项</span><button class="text-button" @click="viewChecks" :disabled="!!busy">{{ showChecks?'收起待核查项':'查看待核查项' }} →</button></div></section><section class="panel selection-panel"><div class="panel-title"><h2>辅助指标选择</h2><span class="tag muted">{{ selectionMethod(currentRun.selection?.mode) }}</span></div><div class="selection-tags"><span v-for="id in (currentRun.selection?.indicator_ids || [])" :key="id">{{ indicatorName(id) }}</span><span v-if="!currentRun.selection?.indicator_ids?.length">本次未补充辅助指标</span></div><p>{{ agentModeDescription(currentRun.selection?.mode) }}</p></section></div>
            <section v-if="showChecks && currentRun" class="panel diagnostic-panel"><div class="panel-title"><div><h2>待核查的检查项</h2><p>这些记录不计为“未触发”，也不会据此直接生成风险函。</p></div><select v-model="checkFilter" aria-label="检查状态"><option value="insufficient">数据不足 / 口径不符</option><option value="unconfigured">规则待配置</option></select></div><div v-if="!visibleChecks.length" class="empty-state compact"><h3>当前类型没有待核查项</h3></div><div v-else class="table-scroll"><table><thead><tr><th>行社</th><th>指标</th><th>原因</th></tr></thead><tbody><tr v-for="item in visibleChecks" :key="String(item.org_id)+'-'+String(item.rule_id)"><td>{{ item.org_name }}</td><td>{{ indicatorName(item.indicator_id) }}</td><td class="description-cell">{{ item.reason }}</td></tr></tbody></table></div></section>
          </section>

          <section v-if="tab==='rules'" class="rules-page">
            <div class="info-banner"><span class="info-mark">i</span><p>默认使用 <strong>排名 ≥72</strong>。82 家全部参评且无并列时，对应第 72—82 名，共 11 家。占比阈值需要自行配置，所有当前设置均用于测试。</p></div>
            <div class="section-heading"><h2>监测规则</h2><span>{{ configuredRules }} 条可执行 · {{ boot.rules.filter(r=>r.needs_config).length }} 条待配置</span></div>
            <div class="rules-grid"><article class="rule-card" v-for="rule in boot.rules" :key="rule.id"><div class="rule-card-header"><span :class="['tag',rule.kind==='exists'?'amber':rule.kind==='ratio'?'purple':'blue']">{{ ruleKind(rule.kind) }}</span><button :class="['toggle',{on:rule.enabled}]" @click="toggleRule(rule)" :disabled="!!busy" role="switch" :aria-checked="rule.enabled" :aria-label="rule.name+'启用状态'"><span></span></button></div><h3>{{ rule.name }}</h3><p class="rule-metric">{{ indicatorName(rule.indicator_id) }}</p><p class="rule-description">{{ rule.description }}</p><div v-if="rule.kind==='rank'" class="rule-input-row"><span>排名 ≥</span><input v-model="ruleInputs[String(rule.id)]" type="number" min="1" max="82" step="1" :aria-label="rule.name+'排名起点'" /><span>名</span><button class="button small secondary" @click="saveRule(rule)" :disabled="!!busy">保存</button></div><div v-else-if="rule.kind==='ratio'" class="rule-input-row"><span>占比 {{ rule.params.op || '≥' }}</span><input v-model="ruleInputs[String(rule.id)]" type="number" min="0" max="100" step="0.01" placeholder="待配置" :aria-label="rule.name+'占比阈值'" /><span>%</span><button class="button small secondary" @click="saveRule(rule)" :disabled="!!busy">保存</button></div><div v-else class="exists-formula"><code>数值不为空 且 数值 ≠ 0</code><small>零值不触发，缺数单独标记</small></div><footer><span :class="['rule-state',{pending:rule.needs_config}]">{{ !rule.enabled?'已停用':rule.needs_config?'阈值待配置':'已启用' }}</span><span>版本 {{ rule.version }}</span></footer></article></div>
            <section class="panel indicator-panel"><div class="panel-title"><div><h2>指标目录</h2><p>实际数值、单位与排名从数据快照取得</p></div><input v-model="indicatorQuery" placeholder="查找指标名称或 ID" aria-label="查找指标" /></div><div class="table-scroll"><table><thead><tr><th>指标名称</th><th>指标 ID</th><th>单位</th><th>频率</th><th>说明</th></tr></thead><tbody><tr v-for="indicator in visibleIndicators" :key="indicator.id"><td><strong>{{ indicator.name }}</strong></td><td><code>{{ indicator.id }}</code></td><td>{{ indicator.unit }}</td><td>{{ indicator.frequency }}</td><td class="description-cell">{{ indicator.description }}</td></tr></tbody></table></div></section>
          </section>

          <section v-if="tab==='orgs'" class="org-page">
            <div class="hierarchy-strip"><div><b>01</b><strong>总行</strong><small>根机构</small></div><span>→</span><div><b>02</b><strong>地区</strong><small>{{ regions.length }} 个地区</small></div><span>→</span><div class="current"><b>03</b><strong>行社</strong><small>{{ societies.length }} 家 · 当前监测</small></div><span>→</span><div class="reserved"><b>04</b><strong>支行</strong><small>结构已预留</small></div><span>→</span><div class="reserved"><b>05</b><strong>第五级机构</strong><small>类型待业务确认</small></div></div>
            <div class="section-heading"><div><h2>地区与行社</h2><p>内置名称及归属为合成样例，后续按正式机构代码导入。</p></div><input v-model="orgQuery" placeholder="搜索机构名称或代码" aria-label="搜索机构" /></div>
            <div class="region-grid"><article v-for="region in regions.filter(r=>!orgQuery || children(r).length || r.name.includes(orgQuery))" :key="region.id" class="panel region-card"><header><span class="region-icon">⌘</span><div><h3>{{ region.name }}</h3><code>{{ region.code }}</code></div><span class="count-pill">{{ children(region).length }}</span></header><div class="society-list"><div v-for="org in children(region)" :key="org.id"><span>{{ org.name }}</span><code>{{ org.code }}</code></div></div></article></div>
          </section>

          <section v-if="tab==='data'" class="data-page">
            <div class="import-layout"><section class="panel import-panel"><div class="panel-title"><h2>导入指标数据</h2><span class="tag blue">CSV / XLSX</span></div><div class="upload-area"><div class="upload-icon">↑</div><h3>{{ importFile ? importFile.name : '选择数据文件' }}</h3><p>{{ importFile ? number(importFile.size / 1024)+' KB' : '使用标准列结构，每次导入生成一个新快照。' }}</p><input ref="fileInput" type="file" accept=".csv,.xlsx" @change="chooseFile" class="hidden-file" aria-label="选择 CSV 或 XLSX 文件" /><button class="button secondary" @click="fileInput?.click()" :disabled="!!busy">{{ importFile?'重新选择':'选择文件' }}</button></div><div class="import-actions"><a class="text-button" href="/api/import/template.csv" download>下载 CSV 导入模板 ↓</a><button class="button primary" @click="upload" :disabled="!importFile || !!busy">{{ busy==='import'?'正在导入…':'导入新快照' }}</button></div><div v-if="importResult" class="import-success"><strong>已导入 {{ number(importResult.row_count) }} 条记录</strong><p>新快照已选中，可以返回监测工作台运行检查。</p><ul v-if="importResult.warnings?.length"><li v-for="warning in importResult.warnings" :key="warning">{{ warning }}</li></ul><button class="text-button" @click="tab='monitor'">进入监测工作台 →</button></div></section><aside class="panel import-guide"><div class="eyebrow">DATA CONTRACT</div><h2>先对齐口径，再导入</h2><dl><dt>机构与指标</dt><dd><code>org_code</code> 和 <code>indicator_id</code> 必须与目录一致。</dd><dt>百分比数值</dt><dd>以 <code>1.65</code> 表示 1.65%，<code>value_encoding</code> 填 <code>percent_number</code>。</dd><dt>金额数值</dt><dd>单位为元，编码填 <code>raw</code>。</dd><dt>已有排名</dt><dd>保留原始排名，范围填 <code>province_societies</code>，同时提供参评数量。</dd><dt>缺失与零</dt><dd>空值表示缺失，0 表示真实零值。两者分别处理。</dd></dl><p class="note">数据库与小Q对接方式待接口确认。当前页面不会连接内网数据库。</p></aside></div>
            <section class="panel"><div class="panel-title"><h2>数据快照</h2><span class="subtle">{{ boot.snapshots.length }} 个批次</span></div><div class="table-scroll"><table><thead><tr><th>快照名称</th><th>数据日期</th><th>来源</th><th>记录数</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="snapshot in boot.snapshots" :key="snapshot.id"><td><strong>{{ snapshot.name }}</strong><small>{{ shortId(snapshot.id) }}</small></td><td>{{ snapshot.data_date }}</td><td><span :class="['tag',snapshot.is_demo?'blue':'muted']">{{ snapshot.is_demo?'合成样例':snapshot.source }}</span></td><td>{{ number(snapshot.row_count) }}</td><td><span class="tag green">{{ statusName(snapshot.status) }}</span></td><td><button class="text-button" @click="snapshotId=String(snapshot.id);tab='monitor'">使用快照 →</button></td></tr></tbody></table></div></section>
          </section>

          <section v-if="tab==='runs'" class="runs-page"><div class="info-banner"><span class="info-mark">i</span><p>相同快照、规则版本与风险主题重复运行会复用结果。修改配置或主题后重新运行，形成新的执行记录。</p></div><section class="panel"><div class="panel-title"><h2>历史监测批次</h2><span class="subtle">{{ runs.length }} 次</span></div><div v-if="!runs.length" class="empty-state compact"><span class="empty-icon">◷</span><h3>还没有运行记录</h3><p>首次监测完成后，可在这里查看覆盖情况和风险事项。</p><button class="button secondary" @click="tab='monitor'">返回工作台</button></div><div v-else class="run-list"><article v-for="run in runs" :key="run.id" class="run-item"><div class="run-item-heading"><div><strong>{{ snapshotName(run.snapshot_id) }}</strong><p>{{ dateTime(run.created_at) }} · {{ shortId(run.id) }} · {{ agentModeLabel(run.selection.mode) }}</p></div><span :class="['tag',statusClass(run.status)]">{{ statusName(run.status) }}</span></div><div class="run-metrics"><div><span>风险行社</span><b>{{ number(run.summary.case_count) }}</b></div><div><span>规则命中</span><b>{{ number(run.summary.triggered) }}</b></div><div><span>未触发</span><b>{{ number(run.summary.normal) }}</b></div><div><span>无法判断</span><b class="warning">{{ number(run.summary.insufficient) }}</b></div><div><span>待配置</span><b>{{ number(run.summary.unconfigured) }}</b></div><button class="button secondary small" @click="openRun(run)" :disabled="!!busy">查看结果 →</button></div></article></div></section></section>
          <footer class="page-footer"><span>风险提示 · 行社级测试版</span><span>内置数据为合成样例，所有下发均为模拟。</span></footer>
        </div>
      </main>

      <div v-if="selectedCase" class="drawer-backdrop" @click.self="closeCase">
        <section class="case-drawer" role="dialog" aria-modal="true" :aria-label="selectedCase.org_name+'风险详情'">
          <header class="drawer-header"><div><div class="eyebrow">RISK CASE</div><h2>{{ selectedCase.org_name }}</h2><p>{{ selectedCase.region_name }} · 数据截至 {{ selectedCase.data_date }}</p></div><button class="close-button" @click="closeCase" aria-label="关闭详情">×</button></header>
          <div class="drawer-tabs"><button :class="{active:drawerTab==='evidence'}" @click="drawerTab='evidence'">证据与分析 <span>{{ selectedCase.evidence?.length || 0 }}</span></button><button :class="{active:drawerTab==='letter'}" @click="drawerTab='letter'">风险提示函 <span v-if="letter">v{{ letter.revision }}</span></button></div>
          <div class="drawer-body">
            <div v-if="error" class="alert error" role="alert"><strong>操作未完成</strong><span>{{ error }}</span><button @click="error=''" aria-label="关闭错误提示">×</button></div>
            <div v-if="drawerTab==='evidence'">
              <div class="detail-lead"><span class="tag amber">{{ selectedCase.evidence?.length || 0 }} 条触发依据</span><p>{{ selectedCase.headline }}</p></div>
              <article v-for="evidence in selectedCase.evidence" :key="evidence.id" class="evidence-card"><header><span :class="['tag',evidence.rule_kind==='exists'?'amber':'blue']">{{ ruleKind(evidence.rule_kind) }}</span><small>{{ evidence.data_date }}</small></header><h3>{{ evidence.indicator_name }}</h3><div class="evidence-values"><strong>{{ evidence.value_display || String(evidence.value ?? '缺失') }}</strong><span v-if="evidence.rank!==null && evidence.rank!==undefined">全省第 <b>{{ evidence.rank }}</b> 名 <small>/ {{ evidence.population || 82 }} 家</small></span></div><p>{{ evidence.reason }}</p><footer><span>{{ evidence.rule_name }}</span><code>证据 {{ shortId(evidence.id) }}</code></footer></article>
              <section v-if="selectedCase.analysis" class="analysis-section"><div class="panel-title"><h3>分析与核查建议</h3><span class="tag muted">{{ agentModeLabel(selectedCase.analysis.mode) }}</span></div><div v-if="selectedCase.analysis.facts?.length" class="analysis-group"><h4>已知事实</h4><ul><li v-for="fact in selectedCase.analysis.facts" :key="fact">{{ fact }}</li></ul></div><div v-if="selectedCase.analysis.hypotheses?.length" class="analysis-group"><h4>待核查解释</h4><ul><li v-for="hypothesis in selectedCase.analysis.hypotheses" :key="hypothesis">{{ hypothesis }}</li></ul></div><div v-if="selectedCase.analysis.checks?.length" class="analysis-group"><h4>建议核查</h4><ol><li v-for="check in selectedCase.analysis.checks" :key="check">{{ check }}</li></ol></div><div v-if="selectedCase.analysis.limitations?.length" class="detail-note"><p v-for="limitation in selectedCase.analysis.limitations" :key="limitation">{{ limitation }}</p></div></section>
            </div>
            <div v-if="drawerTab==='letter'">
              <div v-if="!letter" class="empty-state letter-empty"><div class="empty-orbit"><span>▤</span></div><h3>依据当前证据生成提示函</h3><p>使用通用测试模板。正式模板接入后，可沿用同一份证据与审核流程。</p><button class="button primary" @click="createDraft" :disabled="!!busy">{{ busy==='draft'?'正在生成…':'生成提示函草稿' }}</button></div>
              <template v-else>
                <div class="letter-steps"><span class="done">1 草稿</span><i></i><span :class="{done:letter.status==='approved'||letter.status==='simulated_sent'}">2 审核</span><i></i><span :class="{done:letter.status==='simulated_sent'}">3 模拟下发</span></div>
                <div class="letter-toolbar"><div><span :class="['tag',statusClass(letter.status)]">{{ statusName(letter.status) }}</span><span class="subtle">版本 {{ letter.revision }}</span><span v-if="letterDirty" class="unsaved">有未保存修改</span></div><a v-if="!letterDirty" class="text-button" :href="'/api/letters/'+letter.id+'/download'" download>下载 ↓</a><span v-else class="subtle">保存修改后可下载</span></div>
                <label class="letter-editor-label" for="letter-editor">函件正文</label><textarea id="letter-editor" class="letter-editor" v-model="letterText" :readonly="letter.status==='simulated_sent'" spellcheck="false"></textarea>
                <p class="editor-help">修改正文后需重新审核。当前为测试模板，不包含真实发文字号与正式下发渠道。</p>
                <div v-if="letter.receipt" class="receipt-card"><span class="receipt-check">✓</span><div><h3>模拟下发已完成</h3><p>仅生成本地模拟回执，未向真实机构发送。</p><code>{{ letter.receipt.id }}</code><small>{{ dateTime(letter.receipt.created_at) }}</small></div></div>
                <div class="evidence-binding"><span>证据绑定</span><code>{{ shortId(letter.evidence_hash) }}</code><span>·</span><span>机构、规则与数据期次来自本次监测</span></div>
              </template>
            </div>
          </div>
          <footer class="drawer-footer"><template v-if="drawerTab==='evidence'"><span>事实与推断分别记录</span><button class="button primary" @click="letter?drawerTab='letter':createDraft()" :disabled="!!busy">{{ letter?'查看提示函':'生成提示函' }} →</button></template><template v-else-if="letter"><button class="button secondary" @click="saveLetter" :disabled="!!busy || !letterDirty || letter.status==='simulated_sent'">保存修改</button><div class="footer-actions"><button v-if="letter.status==='draft'" class="button primary" @click="approveLetter" :disabled="!!busy || letterDirty">审核通过</button><button v-else-if="letter.status==='approved'" class="button primary" @click="simulateSend" :disabled="!!busy || letterDirty">模拟下发</button><span v-else class="simulation-complete">✓ 已完成模拟流程</span></div></template><template v-else><span>正式模板尚未接入</span><button class="button secondary" @click="drawerTab='evidence'">返回证据</button></template></footer>
        </section>
      </div>
      <div v-if="toast" class="toast" role="status"><span>✓</span>{{ toast }}</div>
    </div>
  `
}).mount('#app');

/** 自主分析页面：提交隔离代码任务，查看已保存结论、执行纠错过程及可下载成果。 */
import { defineComponent, onMounted, ref, type PropType } from 'vue';
import { api, send, ApiError } from './api';

type SnapshotOption = { id: string | number; name: string; data_date: string };
type Finding = { org_id: string; statement: string; evidence_ids: string[] };
type SourceEvidence = {
  evidence_id: string; org_id: string; org_name?: string; indicator_name: string;
  value: string | number; value_display: string; unit: string; data_date: string; source_record_id: string;
};
type TaskFile = { path: string; size: number; sha256: string };
type Execution = {
  execution_id: string; script_path?: string; exit_code: number;
  stdout: string; stderr: string; files?: TaskFile[];
  exports?: { dataset_id: string; row_count: number }[];
};
type CodeRun = {
  id: string; status: string; snapshot_id: string; created_at: string;
  selection: { mode?: string; error?: string; task_context?: { topic: string; org_scope: string[]; history_snapshot_ids?: string[] } };
};
type CodeRunDetail = CodeRun & {
  result?: { mode: string; summary: string; findings: Finding[]; evidence: SourceEvidence[]; execution_ids: string[]; result_file: string };
  executions: Execution[]; files: TaskFile[];
};

export default defineComponent({
  props: {
    snapshots: { type: Array as PropType<SnapshotOption[]>, required: true },
    agentMode: { type: String, required: true },
  },
  setup(props) {
    const snapshotId = ref(String(props.snapshots[0]?.id ?? ''));
    const topic = ref('分析各行社信用风险的历史变化，比较不良贷款率与逾期贷款率，列出有证据支持的核查事项。');
    const runs = ref<CodeRun[]>([]);
    const current = ref<CodeRunDetail | null>(null);
    const busy = ref('');
    const error = ref('');
    const draft = ref('');
    const statusName = (status: string) => ({ running: '执行中', completed: '已完成', failed: '执行失败' }[status] ?? status);
    const fileUrl = (path: string) => `/api/code-runs/${current.value?.id}/files/${path.split('/').map(encodeURIComponent).join('/')}`;
    const time = (value: string) => new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' });
    const findingEvidence = (finding: Finding) => {
      const ids = new Set(finding.evidence_ids);
      return current.value?.result?.evidence.filter(item => ids.has(item.evidence_id)) ?? [];
    };

    async function action(name: string, operation: () => Promise<void>) {
      if (busy.value) return;
      busy.value = name;
      error.value = '';
      try { await operation(); }
      catch (err) { error.value = err instanceof Error ? err.message : String(err); }
      finally { busy.value = ''; }
    }
    async function refreshRuns() {
      runs.value = await api<CodeRun[]>('/api/code-runs');
    }
    async function selectRun(id: string) {
      current.value = await api<CodeRunDetail>(`/api/code-runs/${encodeURIComponent(id)}`);
      draft.value = '';
    }
    const reload = () => action('refresh', async () => {
      await refreshRuns();
      if (current.value) await selectRun(current.value.id);
      else if (runs.value[0]) await selectRun(runs.value[0].id);
    });
    // 失败重试沿用已保存任务的快照与主题，由后端处理任务竞争和执行记录。
    const start = (retry = false) => action('run', async () => {
      const saved = retry ? current.value : null;
      const riskTopic = (saved?.selection.task_context?.topic ?? topic.value).trim();
      if (!riskTopic) throw new Error('请填写分析任务。');
      let result: CodeRun;
      try {
        result = await send<CodeRun>('/api/code-runs', {
          snapshot_id: saved?.snapshot_id ?? snapshotId.value,
          risk_topic: riskTopic,
          ...(retry ? { retry: true, org_scope: saved?.selection.task_context?.org_scope } : {}),
        });
      } catch (err) {
        // POST失败仍可能已保存任务；按返回身份打开失败记录，直接提供原任务重试。
        const detail = err instanceof ApiError ? err.detail : null;
        if (detail && typeof detail === 'object' && 'run_id' in detail && typeof detail.run_id === 'string') {
          try {
            await refreshRuns();
            await selectRun(detail.run_id);
          } catch (reloadError) {
            const reason = reloadError instanceof Error ? reloadError.message : String(reloadError);
            throw new Error(`${(err as ApiError).message}；失败记录加载失败：${reason}`);
          }
        }
        throw err;
      }
      await refreshRuns();
      await selectRun(result.id);
    });
    const open = (id: string) => action('open', () => selectRun(id));
    const createDraft = () => action('draft', async () => {
      if (!current.value) return;
      const result = await send<{ content: string }>(`/api/code-runs/${current.value.id}/draft`, {});
      draft.value = result.content;
    });
    onMounted(reload);
    return { snapshotId, topic, runs, current, busy, error, draft, statusName, fileUrl, time, findingEvidence, reload, start, open, createDraft };
  },
  template: `
    <section class="exploration-page">
      <div class="info-banner"><span class="info-mark">i</span><p v-if="agentMode==='pi-demo'"><strong>Pi Agent · 代码执行演示</strong>：使用固定演示脚本，实际导出数据、执行计算并演示一次列名错误修正；未调用真实大模型。</p><p v-else><strong>Pi Agent · 模型代码分析</strong>：模型可编写、执行和修正分析脚本。结论保留任务证据与执行记录，原因仍需业务核查。</p></div>
      <section class="panel run-config">
        <div class="panel-title"><div><h2>开始自主分析</h2><p>按快照冻结机构与历史范围，在隔离 Python 中计算并返回摘要。</p></div><span class="tag blue">代码执行</span></div>
        <div class="run-config-fields"><label class="field"><span>数据快照</span><select v-model="snapshotId" :disabled="!!busy"><option v-for="item in snapshots" :key="item.id" :value="String(item.id)">{{ item.name }} · {{ item.data_date }}</option></select></label><label class="field topic-field"><span>分析任务</span><input v-model="topic" maxlength="500" :disabled="!!busy" aria-label="自主分析任务" /></label></div>
        <div class="config-footnote"><span>覆盖本期行社范围；任务文件、执行记录和输出可下载核对。</span><button class="button primary" @click="start(false)" :disabled="!!busy || !snapshotId || !topic.trim()">{{ busy==='run'?'正在分析…':'运行自主分析' }}</button></div>
      </section>
      <div v-if="error" class="alert error" role="alert"><strong>操作未完成</strong><span>{{ error }}</span></div>
      <div v-if="busy==='run'" class="info-banner"><span class="button-spinner"></span><p>正在导出数据、执行脚本并核验结果。完成后可查看完整执行记录。</p></div>
      <section class="panel exploration-history"><div class="panel-title"><h2>分析记录</h2><button class="text-button" @click="reload" :disabled="!!busy">刷新记录</button></div><div v-if="!runs.length" class="empty-state compact"><p>完成首个任务后，结论和执行记录会保存在这里。</p></div><div v-else class="exploration-run-list"><button v-for="run in runs" :key="run.id" :class="['exploration-run', {active:current?.id===run.id}]" @click="open(run.id)" :disabled="!!busy"><strong>{{ run.selection.task_context?.topic || '自主分析任务' }}</strong><small>{{ time(run.created_at) }} · {{ statusName(run.status) }}</small></button></div></section>
      <template v-if="current">
        <section v-if="current.status==='failed'" class="panel exploration-result"><div class="panel-title"><h2>本次执行失败</h2><button class="button secondary small" @click="start(true)" :disabled="!!busy">按原任务重试</button></div><p>{{ current.selection.error || '请查看执行记录，修正运行条件后重试。' }}</p></section>
        <section v-if="current.result" class="panel exploration-result"><div class="panel-title"><h2>分析结论</h2><span class="tag muted">{{ current.result.mode==='pi-demo'?'离线演示':'模型分析' }}</span></div><p>{{ current.result.summary }}</p><article v-for="(finding,index) in current.result.findings" :key="index" class="exploration-finding"><h3>{{ finding.org_id }}</h3><p>{{ finding.statement }}</p><details><summary>{{ finding.evidence_ids.length }} 条来源证据</summary><div class="table-scroll"><table><thead><tr><th>机构</th><th>指标</th><th>数值</th><th>数据日期</th><th>原始记录 ID</th></tr></thead><tbody><tr v-for="evidence in findingEvidence(finding)" :key="evidence.evidence_id"><td><strong>{{ evidence.org_name || evidence.org_id }}</strong><small v-if="evidence.org_name">{{ evidence.org_id }}</small></td><td>{{ evidence.indicator_name }}</td><td :title="'原始值：'+evidence.value+' '+evidence.unit">{{ evidence.value_display }}</td><td>{{ evidence.data_date }}</td><td><code>{{ evidence.source_record_id }}</code></td></tr></tbody></table></div></details></article><div class="exploration-actions"><a class="text-button" :href="fileUrl(current.result.result_file)" download>下载结果 JSON ↓</a><button class="button secondary small" @click="createDraft" :disabled="!!busy">生成综述函草稿</button></div></section>
        <section v-if="draft" class="panel exploration-result"><div class="panel-title"><h2>综述函草稿</h2><span class="tag muted">本地测试</span></div><textarea class="letter-editor" :value="draft" readonly aria-label="自主分析综述函草稿"></textarea><p class="editor-help">根据已核验结果生成，仅作本地演示，不自动审核或下发。</p></section>
        <section class="panel exploration-executions"><div class="panel-title"><h2>执行与纠错记录</h2><span class="subtle">{{ current.executions.length }} 次执行</span></div><div v-if="!current.executions.length" class="empty-state compact"><p>本任务尚无代码执行记录。</p></div><article v-for="(execution,index) in current.executions" :key="execution.execution_id" class="exploration-execution"><header><strong>第 {{ index+1 }} 次执行</strong><span :class="['tag',execution.exit_code===0?'green':'amber']">{{ execution.exit_code===0?'成功':'退出码 '+execution.exit_code }}</span></header><p><code>{{ execution.script_path || 'Python 脚本' }}</code> · {{ execution.execution_id }}</p><p v-if="execution.exports?.length">本次导出 {{ execution.exports.reduce((sum,item)=>sum+item.row_count,0).toLocaleString('zh-CN') }} 条观测记录，数据留在任务文件中。</p><details v-if="execution.stdout"><summary>标准输出</summary><pre>{{ execution.stdout }}</pre></details><details v-if="execution.stderr" :open="execution.exit_code!==0"><summary>错误与诊断</summary><pre>{{ execution.stderr }}</pre></details></article></section>
        <section class="panel"><div class="panel-title"><h2>脚本与输出文件</h2><span class="subtle">{{ current.files.length }} 个文件</span></div><div class="table-scroll"><table><thead><tr><th>文件</th><th>大小</th><th>内容校验</th><th></th></tr></thead><tbody><tr v-for="file in current.files" :key="file.path"><td><code>{{ file.path }}</code></td><td>{{ (file.size/1024).toFixed(1) }} KB</td><td><code>{{ file.sha256.slice(0,12) }}</code></td><td><a class="text-button" :href="fileUrl(file.path)" download>下载 ↓</a></td></tr></tbody></table></div></section>
      </template>
    </section>
  `,
});

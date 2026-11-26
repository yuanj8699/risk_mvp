/** 行社归档与提示函。证据定位和文档导出均使用选中行社的后端归档。 */
import { computed, defineComponent, nextTick, onBeforeUnmount, ref, watch, type PropType } from 'vue';
import { Button as TButton } from 'tdesign-vue-next/es/button';
import ChevronRightIcon from 'tdesign-icons-vue-next/esm/components/chevron-right';
import DownloadIcon from 'tdesign-icons-vue-next/esm/components/download';
import RefreshIcon from 'tdesign-icons-vue-next/esm/components/refresh';
import UserIcon from 'tdesign-icons-vue-next/esm/components/user';
import ChatIcon from 'tdesign-icons-vue-next/esm/components/chat';
import SendIcon from 'tdesign-icons-vue-next/esm/components/send';
import { reportState } from './report-state';
import { api, ApiError, send } from './api';
import { agentModeName, dateTime, type Letter } from './workbench-tasks';
import type { LetterDiagnostics, RiskScan, ScanSociety } from './scan-types';
import { smartqStatusLabel, type useConversation } from './conversation';
import './detail.css';

interface Observation {
  evidence_id: string;
  indicator_name: string;
  value_display: string;
  data_date: string;
  rank: number | null;
  population: number | null;
  rank_scope: string | null;
  usable: boolean;
  issue: string | null;
}

interface SocietyCase { evidence: Observation[]; letter: Letter | null; letter_eligible: boolean }
type Turn = ReturnType<typeof useConversation>['turns']['value'][number];

type DocumentBlock = { kind: 'title' | 'heading' | 'recipient' | 'signature' | 'paragraph'; line: string }
  | { kind: 'table'; line: string; header: string[]; rows: string[][] };

function documentLines(content: string): DocumentBlock[] {
  const blocks: DocumentBlock[] = [];
  const lines = content.split(/\r?\n/).map(line => line.trim());
  const cells = (line: string) => line.slice(1, -1).split('|').map(cell => cell.trim());
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index];
    if (!line) continue;
    if (/^\|.*\|$/.test(line) && /^\|(?:\s*:?-{3,}:?\s*\|)+$/.test(lines[index + 1] ?? '')) {
      const start = index;
      index++;
      const rows: string[][] = [];
      while (/^\|.*\|$/.test(lines[index + 1] ?? '')) rows.push(cells(lines[++index]));
      blocks.push({ kind: 'table', header: cells(line), rows, line: lines.slice(start, index + 1).join('\n') });
      continue;
    }
    const kind = !blocks.length ? 'title' : blocks.length === 1 && line.endsWith('：') ? 'recipient'
      : /^(?:[一二]、|附表：)/.test(line) ? 'heading'
      : /^(?:特此函告。|风险管理部|\d{4}年\d{1,2}月\d{1,2}日)$/.test(line) ? 'signature' : 'paragraph';
    blocks.push({ kind, line });
  }
  return blocks;
}

function documentMarkdown(content: string): string {
  return documentLines(content).map(({ kind, line }) =>
    (kind === 'title' ? '# ' : kind === 'heading' ? '## ' : '') + line
  ).join('\n\n');
}

const messageOf = (error: unknown) => error instanceof Error ? error.message : String(error);

export const SocietyPanel = defineComponent({
  name: 'SocietyPanel',
  components: { TButton, ChevronRightIcon, DownloadIcon, RefreshIcon },
  props: {
    runId: { type: String, required: true },
    society: { type: Object as PropType<ScanSociety | null>, default: null },
    report: Boolean,
    diagnostics: Object as PropType<LetterDiagnostics>,
    autoReports: Object as PropType<RiskScan['auto_reports']>,
  },
  emits: ['report', 'drafted', 'letter-trace', 'sync-reports'],
  setup(props, { emit }) {
    const record = ref<SocietyCase | null>(null), reading = ref(false);
    const action = ref(''), error = ref(''), readError = ref('');
    const autoBusyMessage = '该行社正在后台自动生成风险报告，请稍候';
    const editor = ref<{ content: string; revision: number } | null>(null);
    const conflict = ref(false);
    const autoItem = computed(() => props.autoReports?.items.find(item => item.org_id === props.society?.org_id));
    const background = computed(() => props.autoReports?.status === 'running' &&
      (autoItem.value?.status === 'queued' || autoItem.value?.status === 'running'));
    const evidenceOpen = ref(false), activeId = ref(''), rows = ref<HTMLElement | null>(null);
    const allIssues = ref(false), openCitations = ref(new Set<number>());
    const shownIssues = computed(() => {
      const issues = props.society?.issues ?? [];
      return allIssues.value ? issues : issues.slice(0, 2);
    });
    const observations = computed(() => record.value?.evidence ?? []);
    const letter = computed(() => record.value?.letter);
    const lines = computed(() => documentLines(letter.value?.content ?? ''));
    const downloadPath = computed(() => letter.value ? '/api/letters/' + encodeURIComponent(letter.value.id) + '/download' : '');
    const controls = computed(() => reportState(letter.value?.status ?? ''));
    const generatedAt = computed(() => props.diagnostics?.updated_at && props.diagnostics.revision === letter.value?.revision
      ? dateTime(props.diagnostics.updated_at) : '');
    let version = 0, readVersion = 0;
    onBeforeUnmount(() => { version++; readVersion++; });

    async function load(resetEditor = false) {
      const request = ++readVersion;
      const id = props.society?.case_id;
      readError.value = '';
      reading.value = !!id;
      if (!id) return;
      try {
        const data = await api<SocietyCase>('/api/cases/' + encodeURIComponent(id));
        if (request !== readVersion) return;
        record.value = data;
        if (resetEditor) cancelEdit();
        else if (error.value === autoBusyMessage) error.value = '';
      } catch (e) {
        if (request === readVersion) readError.value = messageOf(e);
      } finally {
        if (request === readVersion) reading.value = false;
      }
    }
    // 只在切换行社或所选行社刚生成报告时回读；同批其他行社的轮询不触碰编辑器。
    watch([() => props.society?.case_id, () => autoItem.value?.status], ([id, status], [oldId, oldStatus]) => {
      if (id !== oldId) {
        version++;
        record.value = editor.value = null;
        conflict.value = evidenceOpen.value = allIssues.value = false;
        openCitations.value.clear();
        activeId.value = action.value = error.value = '';
        void load();
      } else if (status === 'completed' && oldStatus !== 'completed') {
        void load();
      }
    }, { immediate: true });

    function acceptLetter(result: Letter) {
      if (!record.value) return;
      readVersion++;
      reading.value = false;
      readError.value = '';
      record.value.letter = result;
    }
    function beginEdit() {
      if (!letter.value || !controls.value.editable) return;
      editor.value = { content: letter.value.content, revision: letter.value.revision };
      conflict.value = false;
      error.value = '';
    }
    function cancelEdit() {
      editor.value = null;
      conflict.value = false;
      error.value = '';
    }
    async function reviseOrApprove(operation: 'save' | 'approve') {
      const current = letter.value, edit = editor.value;
      if (!current || action.value || reading.value) return;
      const path = '/api/letters/' + encodeURIComponent(current.id);
      const request = version;
      action.value = operation;
      error.value = '';
      try {
        let result: Letter;
        if (operation === 'approve') {
          if (!controls.value.approvable || !window.confirm('确认审核定稿当前报告（版本 v' + current.revision + '）？')) return;
          result = await send<Letter>(path + '/approve', { expected_revision: current.revision });
        } else {
          if (!edit?.content.trim() || !controls.value.editable) return;
          result = await send<Letter>(path, { content: edit.content, expected_revision: edit.revision }, 'PATCH');
        }
        if (request !== version) return;
        acceptLetter(result);
        cancelEdit();
      } catch (e) {
        if (request !== version) return;
        conflict.value = e instanceof ApiError && e.status === 409;
        error.value = conflict.value ? (edit
          ? '报告版本或状态已变化，本次修订尚未保存。可复制修订内容，再刷新报告查看最新版本。'
          : '报告版本或状态已变化，请刷新报告后重新审核。') : messageOf(e);
      } finally {
        if (request === version) action.value = '';
      }
    }

    async function generate(regenerate = false) {
      const society = props.society;
      if (!society || !record.value?.letter_eligible || reading.value || action.value || background.value) return;
      if (regenerate && !controls.value.regenerable) return;
      const request = version;
      error.value = '';
      action.value = 'generate';
      try {
        const path = '/api/risk-scans/' + encodeURIComponent(props.runId) + '/societies/' + encodeURIComponent(society.org_id) + '/draft';
        const runId = props.runId;
        const result = await api<Letter & { diagnostics?: LetterDiagnostics }>(path + (regenerate ? '?regenerate=true' : ''), { method: 'POST' });
        if (result.diagnostics) emit('drafted', runId, result.diagnostics);
        if (request === version) {
          acceptLetter(result);
          if (!props.report) emit('report');
        }
      } catch (e) {
        if (request === version) {
          if (e instanceof ApiError && e.status === 409 && e.message.includes('正在自动生成')) {
            error.value = autoBusyMessage;
            emit('sync-reports', props.runId);
          } else {
            error.value = '生成风险报告失败 · ' + messageOf(e);
          }
        }
      } finally {
        if (request === version) action.value = '';
      }
    }

    // 服务端下载接口返回 UTF-8 正文。保留原文及顺序并加入 Markdown 标题层级。
    async function download() {
      if (!letter.value || action.value || reading.value) return;
      const request = version;
      const filename = (props.society?.org_name ?? '') + '风险报告-v' + letter.value.revision + '.md';
      action.value = 'download';
      error.value = '';
      try {
        const response = await fetch(downloadPath.value);
        if (!response.ok) throw new Error('下载请求失败（' + response.status + '）');
        const content = await response.text();
        if (version !== request) return;
        const md = documentMarkdown(content);
        const blobUrl = URL.createObjectURL(new Blob([md], { type: 'text/markdown;charset=utf-8' }));
        const link = document.createElement('a');
        link.href = blobUrl;
        link.download = filename;
        link.click();
        window.setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
      } catch (e) {
        if (request === version) error.value = messageOf(e);
      } finally {
        if (request === version) action.value = '';
      }
    }

    async function locate(id: string) {
      activeId.value = id;
      evidenceOpen.value = true;
      await nextTick();
      const row = rows.value?.querySelector<HTMLElement>('[data-located="true"]');
      if (row) {
        row.focus({ preventScroll: true });
        row.scrollIntoView({ block: 'nearest' });
      }
    }
    function toggleCitations(index: number) {
      if (openCitations.value.has(index)) openCitations.value.delete(index);
      else openCitations.value.add(index);
    }
    return { record, reading, readError, action, error, observations, letter, lines,
      controls, generatedAt, editor, conflict, beginEdit, cancelEdit, reviseOrApprove, autoItem, background,
      downloadPath, activeId, evidenceOpen, rows, load, generate, download, locate,
      allIssues, shownIssues, openCitations, toggleCitations };
  },
  template: `
    <section class="rd-society" :class="{ 'rd-report': report }" :aria-label="report ? '风险报告' : '选中行社风险详情'">
      <p v-if="!society" class="rd-empty">请在总览中选择行社，{{ report ? '查看或生成该行社的风险报告。' : '查看归并风险事项与原始观测。' }}</p>
      <template v-else>
        <header class="rd-heading">
          <div><h2>{{ society.org_name }}</h2>
            <p>{{ society.region_name || '地区未记录' }} · {{ society.issues.length }} 项事项</p>
            <p v-if="report && letter" class="rd-version">版本 v{{ letter.revision }} ·
              <span class="rd-status" :data-status="letter.status">{{ controls.label }}</span>
              <span v-if="generatedAt"> · 生成于 {{ generatedAt }}</span>
            </p>
          </div>
          <div class="rd-actions">
            <template v-if="report && letter && editor">
              <t-button theme="primary" size="small" :disabled="reading || !!action || conflict || !editor.content.trim()" @click="reviseOrApprove('save')">{{ action === 'save' ? '正在保存…' : '保存新版本' }}</t-button>
              <t-button variant="text" size="small" :disabled="!!action" @click="cancelEdit">取消编辑</t-button>
            </template>
            <template v-else-if="report && letter">
              <a class="rd-export-word" :href="downloadPath + '.docx'" download><download-icon aria-hidden="true"/>导出 Word</a>
              <t-button v-if="controls.editable" variant="outline" size="small" :disabled="reading || !!action" @click="beginEdit">编辑修订</t-button>
              <t-button v-if="controls.approvable" variant="outline" size="small" :disabled="reading || !!action" @click="reviseOrApprove('approve')">审核定稿</t-button>
              <t-button v-if="controls.regenerable && record?.letter_eligible && !background" variant="text" size="small" :disabled="reading || !!action" @click="generate(true)">{{ action === 'generate' ? '正在生成…' : '重新生成' }}</t-button>
              <t-button variant="text" size="small" :disabled="reading || !!action" @click="download">{{ action === 'download' ? '导出中…' : '导出 Markdown' }}</t-button>
              <t-button v-if="diagnostics?.trace?.length" variant="text" size="small" @click="$emit('letter-trace', society.org_id)">查看撰函轨迹</t-button>
              <t-button variant="text" size="small" :disabled="reading || !!action" @click="load(true)"><template #icon><refresh-icon /></template>刷新报告</t-button>
            </template>
            <template v-else-if="letter">
              <t-button theme="primary" variant="outline" size="small" @click="$emit('report')">查看风险报告</t-button>
              <t-button variant="text" size="small" :disabled="reading || !!action" @click="download">导出 Markdown</t-button>
            </template>
            <t-button v-else-if="record?.letter_eligible || background" theme="primary" variant="outline" size="small" :disabled="reading || !!action || background" @click="generate()">{{ action === 'generate' ? '正在生成…' : '生成风险报告' }}</t-button>
          </div>
        </header>
        <p v-if="background" role="status" class="rd-meta">{{ autoItem.status === 'queued' ? '已加入风险报告自动生成队列。' : '正在后台自动分析并生成风险报告…' }}</p>
        <p v-if="reading && !record" role="status" class="rd-meta">正在读取归档…</p>
        <p v-if="readError" role="alert" class="rd-error">读取归档失败 · {{ readError }} <button type="button" @click="load()">重新读取</button></p>
        <p v-if="error" role="alert" class="rd-error">{{ error }} <button v-if="conflict && editor" type="button" :disabled="reading" @click="load(true)">刷新报告</button></p>
        <template v-if="report">
          <textarea v-if="editor" class="rd-editor" aria-label="风险报告正文" v-model="editor.content" maxlength="30000" :readonly="!!action" spellcheck="false"></textarea>
          <article v-else-if="letter" class="rd-document" aria-label="风险报告预览">
            <template v-for="(item, index) in lines" :key="index">
              <h2 v-if="item.kind === 'title'">{{ item.line }}</h2><h3 v-else-if="item.kind === 'heading'">{{ item.line }}</h3>
              <div v-else-if="item.kind === 'table'" class="rd-table-wrap"><table>
                <thead><tr><th v-for="(cell, col) in item.header" :key="col" scope="col">{{ cell }}</th></tr></thead>
                <tbody><tr v-for="(row, pos) in item.rows" :key="pos"><td v-for="(cell, col) in row" :key="col">{{ cell }}</td></tr></tbody>
              </table></div>
              <p v-else :class="'rd-' + item.kind">{{ item.line }}</p>
            </template>
          </article>
          <p v-else-if="record && !background" class="rd-empty">{{ record.letter_eligible ? '该行社尚无风险报告，可根据本期观测生成。' : '该行社本期未触发撰函底线或有效扫描信号。' }}</p>
        </template>
        <template v-else>
          <p v-if="society.data_incomplete" class="rd-data-note">部分直接判断指标缺数或尚待接入。</p>
          <p v-else-if="!society.issues.length" class="rd-meta">本次扫描未归并风险事项。</p>
          <article v-for="(issue, index) in shownIssues" :key="index" class="rd-issue">
            <header><h3>{{ issue.title }}</h3><span class="rd-severity">{{ issue.severity }}</span></header>
            <p><span class="rd-label">成因分析</span>{{ issue.cause_analysis }}</p>
            <p><span class="rd-label">核查建议</span>{{ issue.check_suggestion }}</p>
          </article>
          <button v-if="society.issues.length > 2" type="button" class="rd-disclosure" :aria-expanded="allIssues" @click="allIssues=!allIssues">
            <chevron-right-icon aria-hidden="true"/>{{ allIssues ? '收起其余事项' : '展开其余 ' + (society.issues.length - 2) + ' 项事项' }}
          </button>
          <div v-if="record" class="rd-evidence">
            <button type="button" class="rd-disclosure" :aria-expanded="evidenceOpen" @click="evidenceOpen=!evidenceOpen"><chevron-right-icon aria-hidden="true"/>原始指标观测 <span>{{ observations.length }}</span></button>
            <template v-if="evidenceOpen">
              <div v-if="observations.length" class="rd-table-viewport" ref="rows" role="region" tabindex="0" aria-label="原始指标观测表">
                <table><thead><tr><th>指标 / 证据 ID</th><th>实际值</th><th>期次</th><th>排名</th></tr></thead>
                  <tbody><tr v-for="row in observations" :key="row.evidence_id" tabindex="-1" :data-located="row.evidence_id === activeId">
                    <td>{{ row.indicator_name }}<small>{{ row.evidence_id }}</small></td>
                    <td>{{ row.value_display }}<small v-if="!row.usable">{{ row.issue || '不可用' }}</small></td><td>{{ row.data_date }}</td>
                    <td>{{ row.rank ?? '未提供' }}<template v-if="row.rank != null && row.population != null"> / {{ row.population }}</template><small v-if="row.rank != null">{{ row.rank_scope === 'province_societies' ? '全辖行社' : row.rank_scope }}</small></td>
                  </tr></tbody>
                </table>
              </div>
              <p v-else class="rd-meta">本次归档没有原始观测。</p>
              <p v-if="activeId && !observations.some(row => row.evidence_id === activeId)" role="status" class="rd-data-note">当前归档缺少该引用对应的原始观测。</p>
            </template>
          </div>
        </template>
      </template>
    </section>
  `,
});

/** 按范围展示归档对话及小 Q 宿主实际返回的状态；轨迹指向该轮归档。 */
export const ConversationPanel = defineComponent({
  name: 'ConversationPanel',
  components: { ChatIcon, UserIcon },
  props: {
    turns: { type: Array as PropType<Turn[]>, required: true },
    scope: { type: String as PropType<'society' | 'run'>, default: 'run' },
    societyName: String, busy: Boolean, scopeSwitch: Boolean,
  },
  emits: ['scope', 'trace'],
  setup() { return { agentModeName, smartqStatusLabel }; },
  template: `
    <section class="rd-conversation" aria-label="追问与小 Q 实时补查">
      <header class="rd-talk-heading"><h3>追问与小 Q 实时补查</h3>
        <div v-if="scopeSwitch" class="rd-scope" role="group" aria-label="追问范围">
          <button type="button" :aria-pressed="scope==='society'" :disabled="busy || !societyName" :title="societyName" @click="$emit('scope','society')">当前行社</button>
          <button type="button" :aria-pressed="scope==='run'" :disabled="busy" @click="$emit('scope','run')">全辖范围</button>
        </div>
      </header>
      <p v-if="!turns.length" class="rd-meta">可追问{{ scope==='society' && societyName ? societyName : scopeSwitch ? '全辖可见行社' : '本次任务' }}的指标、排名与风险依据。</p>
      <article v-for="turn in turns" :key="turn.id" class="rd-turn">
        <div class="rd-message"><span class="rd-avatar" aria-label="用户"><user-icon /></span><p>{{ turn.question }}</p></div>
        <aside v-if="turn.smartq" class="rd-callout" aria-label="小 Q 实时补查">
          <div class="rd-callout-head"><strong>小 Q 实时补查</strong><span>{{ smartqStatusLabel[turn.smartq.status] }} · 返回 {{ turn.smartq.record_count }} 条</span></div>
          <p v-if="turn.smartq.rewritten_question"><span>改写问句</span>{{ turn.smartq.rewritten_question }}</p>
          <p v-if="turn.smartq.reason"><span>判断依据</span>{{ turn.smartq.reason }}</p>
          <p v-if="turn.smartq.note && turn.smartq.status !== 'ok'">{{ turn.smartq.note }}</p>
          <p v-if="turn.smartq.trailing_error">流结束时收到错误，已保留有效结果。</p>
          <small>口径仅供参考，主指标口径以 QuickBI 源表为准。</small>
        </aside>
        <div class="rd-message"><span class="rd-avatar" aria-label="助手"><chat-icon /></span><div class="rd-answer"><p>{{ turn.answer }}</p><span class="rd-turn-mode">{{ agentModeName(turn.mode) }}</span></div></div>
        <footer class="rd-turn-links">
          <details v-if="turn.evidence?.length"><summary>引用证据 · {{ turn.evidence.length }} 条</summary>
            <div v-for="source in turn.evidence" :key="source.id" class="rd-turn-evidence"><strong>{{ source.label }}</strong><p>{{ source.text }}</p><code>{{ source.id }}</code></div>
          </details>
          <button type="button" v-if="turn.trace?.length" @click="$emit('trace',turn.id)">查看本轮轨迹</button>
        </footer>
      </article>
    </section>
  `,
});

/** 总览与轨迹共用的快捷输入；发送与范围绑定由现有会话控制器负责。 */
export const FollowupComposer = defineComponent({
  name: 'FollowupComposer',
  components: { SendIcon },
  props: {
    modelValue: { type: String, default: '' }, sending: Boolean, disabled: Boolean,
    mode: String, smartq: Boolean, readError: String, sendError: String,
  },
  emits: ['update:modelValue', 'ask', 'reload'],
  setup(props, { emit }) {
    const canSend = computed(() => !!props.modelValue.trim() && !props.disabled && !props.sending);
    function submit() { if (canSend.value) emit('ask'); }
    return { canSend, submit };
  },
  template: `
    <div class="rd-composer">
      <p v-if="readError || sendError" class="rd-error" role="alert">{{ readError || sendError }}
        <button type="button" v-if="readError" @click="$emit('reload')">重新读取</button>
      </p>
      <form @submit.prevent="submit">
        <textarea rows="1" maxlength="1000" aria-label="追问 Pi" :disabled="disabled || sending" :value="modelValue" @input="$emit('update:modelValue',$event.target.value)"
          placeholder="追问当前范围内的指标或风险事项…" @keydown.ctrl.enter.prevent="submit" @keydown.meta.enter.prevent="submit"></textarea>
        <button type="submit" aria-label="发送追问" title="发送追问（Ctrl / ⌘ + Enter）" :disabled="!canSend"><send-icon aria-hidden="true"/></button>
      </form>
      <div class="rd-input-meta"><span></span><span>{{ sending ? '正在回复…' : 'Ctrl / ⌘ + Enter' }}</span></div>
    </div>
  `,
});

/** 选中行社后读取当次归档。归并事项引用原始观测，报告使用后端保存的版本。 */
import { computed, defineComponent, nextTick, ref, watch, type PropType } from 'vue';
import { api } from './api';
import { reportState } from './report-state';
import type { RiskDimension, ScanSociety } from './scan-types';
import './scan-society.css';

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

interface Letter { id: string; content: string; revision: number; status: string }
interface SocietyCase { evidence: Observation[]; letter: Letter | null }

export default defineComponent({
  name: 'ScanSociety',
  props: {
    runId: { type: String, required: true },
    society: { type: Object as PropType<ScanSociety>, required: true },
    dimensions: { type: Array as PropType<RiskDimension[]>, required: true },
  },
  setup(props) {
    const record = ref<SocietyCase | null>(null);
    const loading = ref(false), creating = ref(false);
    const readError = ref(''), draftError = ref('');
    const evidenceOpen = ref(false), activeCitation = ref('');
    const rows = ref<HTMLElement | null>(null);
    const referenced = computed(() => new Set(props.society.issues.flatMap(issue => issue.evidence_ids)));
    const dimensionName = (id: string) => props.dimensions.find(item => item.id === id)?.title ?? id;
    let version = 0;

    // 快速切换行社时，迟到的详情或报告不能写到新行社。
    async function loadCase() {
      const request = ++version;
      loading.value = true;
      readError.value = '';
      try {
        const data = await api<SocietyCase>('/api/cases/' + encodeURIComponent(props.society.case_id));
        if (version === request) record.value = data;
      } catch (error) {
        if (version === request) readError.value = error instanceof Error ? error.message : String(error);
      } finally {
        if (version === request) loading.value = false;
      }
    }

    watch(() => props.society.case_id, () => {
      record.value = null;
      evidenceOpen.value = false;
      activeCitation.value = '';
      creating.value = false;
      draftError.value = '';
      void loadCase();
    }, { immediate: true });

    async function generateDraft() {
      if (!record.value || record.value.letter || creating.value || !props.society.issues.length) return;
      const request = version;
      creating.value = true;
      draftError.value = '';
      try {
        const path = '/api/risk-scans/' + encodeURIComponent(props.runId) +
          '/societies/' + encodeURIComponent(props.society.org_id) + '/draft';
        const letter = await api<Letter>(path, { method: 'POST' });
        if (version === request) record.value.letter = letter;
      } catch (error) {
        if (version === request) draftError.value = error instanceof Error ? error.message : String(error);
      } finally {
        if (version === request) creating.value = false;
      }
    }

    async function locate(id: string) {
      activeCitation.value = id;
      evidenceOpen.value = true;
      await nextTick();
      const row = rows.value?.querySelector<HTMLElement>('[data-located="true"]');
      row?.focus({ preventScroll: true });
      row?.scrollIntoView({ block: 'nearest' });
    }

    return { record, reportState, referenced, dimensionName, loading, creating, readError, draftError, evidenceOpen,
      rows, activeCitation, locate, loadCase, generateDraft };
  },
  template: `
    <section class="scan-society" aria-label="选中行社风险详情">
      <header class="society-heading">
        <h2>{{ society.org_name }}</h2>
        <span>{{ society.region_name }} · {{ society.issues.length }} 项归并事项</span>
      </header>
      <p v-if="society.data_incomplete" class="society-data-note">部分直接判断指标缺数或尚待接入。</p>
      <p v-else-if="society.status === 'no_signal'" class="society-data-note">本次未命中参考筛查条件。</p>

      <article v-for="(issue, index) in society.issues" :key="index" class="society-issue">
        <div class="society-issue-heading">
          <h3>{{ issue.title }}</h3>
          <span>{{ dimensionName(issue.dimension_id) }} · {{ issue.severity }}</span>
        </div>
        <dl>
          <dt>成因分析</dt><dd>{{ issue.cause_analysis }}</dd>
          <dt>核查建议</dt><dd>{{ issue.check_suggestion }}</dd>
        </dl>
        <details class="society-citations">
          <summary>引用证据（{{ issue.evidence_ids.length }}）</summary>
          <div><button v-for="id in issue.evidence_ids" :key="id" type="button"
            :disabled="!record" @click="locate(id)" :aria-label="'定位原始观测 ' + id"><code>{{ id }}</code></button></div>
        </details>
      </article>

      <p v-if="loading" role="status" class="society-data-note">正在读取行社归档…</p>
      <div v-else-if="readError" class="society-error" role="alert">
        读取行社归档失败 · {{ readError }}
        <button type="button" class="button" @click="loadCase">重新读取</button>
      </div>
      <template v-if="record">
        <div class="society-observations">
          <button class="society-disclosure" type="button" :aria-expanded="evidenceOpen"
            @click="evidenceOpen = !evidenceOpen">原始指标观测（{{ record.evidence.length }}）</button>
          <div v-if="evidenceOpen && record.evidence.length" class="society-evidence-scroll" ref="rows" tabindex="0" aria-label="原始指标观测表">
            <table>
              <thead><tr><th>指标名称</th><th>实际值</th><th>期次</th><th>排名</th><th>证据 ID</th></tr></thead>
              <tbody><tr v-for="row in record.evidence" :key="row.evidence_id" tabindex="-1"
                :class="{referenced: referenced.has(row.evidence_id)}" :data-located="row.evidence_id === activeCitation">
                <td data-label="指标">{{ row.indicator_name }}<small v-if="referenced.has(row.evidence_id)">事项已引用</small></td>
                <td data-label="实际值">{{ row.value_display }}<small v-if="!row.usable">{{ row.issue || '不可用' }}</small></td>
                <td data-label="期次">{{ row.data_date }}</td>
                <td data-label="排名">{{ row.rank ?? '未提供' }}<template v-if="row.rank != null && row.population != null"> / {{ row.population }}</template><small v-if="row.rank != null">{{ row.rank_scope === 'province_societies' ? '全辖行社' : row.rank_scope }}</small></td>
                <td data-label="证据 ID"><code>{{ row.evidence_id }}</code></td>
              </tr></tbody>
            </table>
          </div>
          <p v-else-if="evidenceOpen" class="society-data-note">本次归档没有原始观测。</p>
        </div>
        <section v-if="record.letter" class="society-letter">
          <header class="society-heading"><h3>风险报告 · 版本 v{{ record.letter.revision }} · {{ reportState(record.letter.status).label }}</h3>
            <a :href="'/api/letters/' + encodeURIComponent(record.letter.id) + '/download.docx'" download>导出 Word</a>
          </header>
          <pre tabindex="0" aria-label="风险报告预览">{{ record.letter.content }}</pre>
        </section>
        <template v-else-if="society.issues.length">
          <button type="button" class="button" :disabled="creating" @click="generateDraft">
            {{ creating ? '正在生成…' : '生成风险报告' }}
          </button>
          <p v-if="draftError" role="alert" class="society-error">生成风险报告失败 · {{ draftError }}</p>
        </template>
      </template>
    </section>
  `,
});

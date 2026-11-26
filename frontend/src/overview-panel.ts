/** 全辖总览只展示本次归档。等级取行社事项的最高等级，指标从同一快照的原始观测读取。 */
import { computed, defineComponent, reactive, ref, watch, type PropType } from 'vue';
import { Input as TInput } from 'tdesign-vue-next/es/input';
import { Select as TSelect } from 'tdesign-vue-next/es/select';
import SearchIcon from 'tdesign-icons-vue-next/esm/components/search';
import CheckIcon from 'tdesign-icons-vue-next/esm/components/check';
import ChevronDownIcon from 'tdesign-icons-vue-next/esm/components/chevron-down';
import ChevronLeftIcon from 'tdesign-icons-vue-next/esm/components/chevron-left';
import ChevronRightIcon from 'tdesign-icons-vue-next/esm/components/chevron-right';
import { api } from './api';
import { reportBanner, reportMarker } from './auto-reports';
import type { RiskScan, ScanSociety } from './scan-types';
import ValidationDetails from './overview-validation';
import './overview.css';

const dimensionNames: Record<string, string> = {
  credit_risk: '信用风险', capital_and_buffer: '资本与拨备', treasury_risk: '资金业务',
  rural_micro_positioning: '农小定位', key_business_mgmt: '重点业务',
};
const levels = ['重点关注', '关注', '提示', '数据不足', '未触发', '等级未记录'];
const tones: Record<string, string> = {
  '重点关注': 'priority', '关注': 'watch', '提示': 'notice', '未触发': 'clear', '数据不足': 'missing',
};

function level(s: ScanSociety) {
  if (s.status === 'risk') return levels.slice(0, 3).find(l => s.issues.some(i => i.severity === l)) ?? '等级未记录';
  return s.status === 'no_signal' ? '未触发' : '数据不足';
}
function orderedSocieties(rows: ScanSociety[] = []) {
  return [...rows].sort((a, b) =>
    levels.indexOf(level(a)) - levels.indexOf(level(b)) || b.issues.length - a.issues.length);
}
export const firstOverviewSociety = (scan: RiskScan) => orderedSocieties(scan.society_results)[0] ?? null;

/** 源表状态独立于模型模式，只有完整且逐项相符的金额勾稽才标绿。 */
export function reconciliationState(scan?: RiskScan | null) {
  const neutral = (label: string) => ({ label, tone: 'neutral' });
  if (!scan) return neutral('待校验');
  const v = scan.validation_summary;
  const quickbi = scan.snapshot_id.startsWith('quickbi:');
  if (!v) return neutral('未记录校验');
  const amounts = v.accounting_details.filter(r => r.metric_kind === 'amount');
  if (amounts.some(r => r.member_sum_matches_source_total === false) || v.unit_mismatch_count) {
    return { label: '存在口径异常', tone: 'warning' };
  }
  if (v.visibility?.clipped) return { label: '权限裁剪 · 未勾稽', tone: 'warning' };
  if (!quickbi && scan.is_demo) return neutral('本地演示');
  if (amounts.length && amounts.every(r => r.member_sum_matches_source_total === true)) {
    return { label: '999999 勾稽一致', tone: 'success' };
  }
  return neutral(quickbi ? '999999 勾稽未完成' : '源表未勾稽');
}

interface Observation {
  evidence_id: string; snapshot_id: string; indicator_id: string; indicator_name: string;
  value_display: string; data_date: string; usable: boolean;
}
const coreIds = ['npl_ratio', 'TEST_METRIC_097', 'TEST_METRIC_013', 'overdue_ratio', 'funds_npl_balance'];
const coreNames: Record<string, string> = { npl_ratio: '不良率', TEST_METRIC_097: '拨备覆盖率' };
function summarizeMetrics(rows: Observation[], society: ScanSociety, snapshotId: string) {
  const current = rows.filter(r => r.snapshot_id === snapshotId && r.usable);
  const triggerIds = new Set(society.signals.map(s => s.evidence_id));
  const basis = triggerIds.size ? current.filter(r => triggerIds.has(r.evidence_id)) : current;
  const priority = (r: Observation) => coreIds.includes(r.indicator_id) ? coreIds.indexOf(r.indicator_id) : coreIds.length;
  const metrics = basis.sort((a, b) => priority(a) - priority(b)).slice(0, 2);
  return {
    label: metrics.map(r => (coreNames[r.indicator_id] ?? r.indicator_name) + ' ' + r.value_display).join(' · ') || '无可用实测',
    title: metrics.map(r => r.indicator_name + ' ' + r.value_display + '（' + r.data_date + '）').join('\n'),
  };
}

export default defineComponent({
  name: 'OverviewPanel',
  components: { TInput, TSelect, SearchIcon, CheckIcon, ChevronDownIcon, ChevronLeftIcon, ChevronRightIcon, ValidationDetails },
  props: {
    scan: { type: Object as PropType<RiskScan>, required: true },
    selectedOrgId: String,
    startingReports: Boolean,
  },
  emits: { select: (_society: ScanSociety | null, _fromRow?: boolean) => true,
    report: (_society: ScanSociety) => true, 'start-reports': () => true },
  setup(props, { emit }) {
    const query = ref(''), district = ref('all'), risk = ref('all'), dimension = ref('');
    const page = ref(1), pageSize = 9, validationOpen = ref(false);
    const dimensions = computed(() => props.scan.risk_dimensions ?? []);
    const archivedRows = computed(() => props.scan.society_results);
    const societies = computed(() => orderedSocieties(archivedRows.value));
    const progress = computed(() => props.scan.auto_reports);
    const reportItems = computed(() => new Map(progress.value?.items.map(item => [item.org_id, item])));
    const validation = computed(() => props.scan.validation_summary);
    const visibility = computed(() => validation.value?.visibility);
    const scopeCount = computed(() => visibility.value?.expected_org_count ||
      props.scan.selection.task_context.org_scope.length || validation.value?.total_org_count);
    const counts = computed(() => {
      const totals: Record<string, number> = {};
      for (const row of societies.value) {
        const label = level(row);
        totals[label] = (totals[label] ?? 0) + 1;
      }
      return totals;
    });
    const regionOptions = computed(() => {
      const names = [...new Set(societies.value.map(s => s.region_name).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh-CN'));
      return [{ label: '全部 ' + names.length + ' 地区', value: 'all' }, ...names.map(value => ({ value, label: value }))];
    });
    const riskOptions = computed(() => [{ label: '全部等级', value: 'all' },
      ...levels.filter(l => counts.value[l]).map(value => ({ value, label: value }))]);
    const visible = computed(() => {
      const term = query.value.trim().toLocaleLowerCase();
      return societies.value.filter(s => (district.value === 'all' || s.region_name === district.value)
        && (risk.value === 'all' || level(s) === risk.value)
        && (!dimension.value || s.issues.some(i => i.dimension_id === dimension.value))
        && (s.org_name + ' ' + s.region_name).toLocaleLowerCase().includes(term));
    });
    const pageCount = computed(() => Math.ceil(visible.value.length / pageSize));
    const pageStart = computed(() => (page.value - 1) * pageSize);
    const pageRows = computed(() => visible.value.slice(pageStart.value, pageStart.value + pageSize));
    // 归档回看时，将父组件还原的行社定位到正确分页；筛选变更后改选可见行。
    const restoredIndex = societies.value.findIndex(s => s.org_id === props.selectedOrgId);
    if (restoredIndex >= 0) page.value = Math.floor(restoredIndex / pageSize) + 1;
    watch(visible, () => { page.value = 1; });
    watch(pageRows, rows => {
      if (!rows.some(r => r.org_id === props.selectedOrgId)) emit('select', rows[0] ?? null, false);
    });
    const metrics = reactive<Record<string, { label: string; title: string; hasReport?: boolean; eligible?: boolean }>>({});
    // 老归档中没有归并事项的行社也可能命中监管底线，复用已加载的行社撰写资格。
    const banner = computed(() => props.scan.status === 'completed' && reportBanner(progress.value,
      societies.value.some(s => s.status === 'risk' || s.signals.length > 0 || metrics[s.case_id]?.eligible)));
    const marker = (s: ScanSociety) => reportMarker(reportItems.value.get(s.org_id),
      Boolean(props.scan.letter_drafts?.[s.org_id] || metrics[s.case_id]?.hasReport));
    function openRow(s: ScanSociety) {
      if (marker(s)) emit('report', s);
      else emit('select', s);
    }
    // 分页只读取屏上行社，切页或切换任务即取消旧请求。只缓存摘要，避免留存整批历史观测。
    watch(pageRows, (rows, _, cleanup) => {
      const controller = new AbortController();
      cleanup(() => controller.abort());
      for (const row of rows) {
        if (metrics[row.case_id]) continue;
        void api<{evidence: Observation[]; letter?: { id: string } | null; letter_eligible?: boolean}>('/api/cases/' + encodeURIComponent(row.case_id), { signal: controller.signal })
          .then(data => { if (!controller.signal.aborted) metrics[row.case_id] = {
            ...summarizeMetrics(data.evidence, row, props.scan.snapshot_id), hasReport: !!data.letter, eligible: data.letter_eligible }; })
          .catch((e: Error) => { if (!controller.signal.aborted) metrics[row.case_id] = { label: '读取失败', title: e.message }; });
      }
    }, { immediate: true });
    const steps = computed(() => {
      const v = validation.value, scan = props.scan;
      const source = reconciliationState(scan);
      const hasGaps = v && (v.missing_count || v.missing_source.length || v.stale_count || v.unit_mismatch_count ||
        v.visibility?.clipped || source.tone === 'warning' || (scan.snapshot_id.startsWith('quickbi:') && source.tone !== 'success'));
      const missing = new Set(v?.missing_source ?? []);
      const matched = scan.knowledge_mapping ? Object.keys(scan.knowledge_mapping).filter(id => !missing.has(id)).length : undefined;
      return [
        { title: '内置风险面' + (dimensions.value.length ? '(' + dimensions.value.length + '维)' : ''), short: '风险面', state: dimensions.value.length ? 'done' : 'pending' },
        { title: '指标口径匹配' + (matched !== undefined ? '(' + matched + '项)' : ''), short: '匹配', state: matched ? 'done' : 'pending' },
        { title: scan.snapshot_id.startsWith('quickbi:') ? 'OLAP 取数' : '本地快照取数', short: '取数', state: v ? 'done' : 'pending' },
        { title: '源表口径校验', short: '校验', state: hasGaps ? 'warn' : v ? 'done' : 'pending' },
        { title: '行社归并与出函', short: '归并', state: scan.status === 'completed' ? 'current' : 'pending' },
      ];
    });
    const hitDimensions = (s: ScanSociety) => [...new Set(s.issues.map(i => dimensionNames[i.dimension_id] ??
      dimensions.value.find(d => d.id === i.dimension_id)?.title ?? i.dimension_id))].join('、') || '—';
    return { query, district, risk, dimension, dimensions, dimensionNames, regionOptions, riskOptions, level, levels, tones, counts,
      banner, progress, marker, openRow,
      societies, visible, pageRows, pageStart, pageCount, page, metrics, validation, validationOpen, visibility, scopeCount, steps, hitDimensions };
  },
  template: `
    <section class="overview-panel" aria-label="全辖扫描总览">
      <div v-if="banner" class="ov-report-status" :data-state="progress?.status" :title="progress?.rule || progress?.policy">
        <span v-if="banner.text" role="status">{{ banner.text }}</span>
        <button v-if="banner.action" type="button" :disabled="startingReports" @click="$emit('start-reports')">{{ banner.action }}</button>
        <progress v-if="progress?.status === 'running'" aria-label="风险报告生成进度" :value="progress.completed + progress.failed" :max="progress.total"></progress>
      </div>
      <ol class="ov-stages" aria-label="风险提示函五阶段">
        <li v-for="(step, index) in steps" :key="index" :data-state="step.state">
          <component :is="index === 3 ? 'button' : 'span'" :type="index === 3 ? 'button' : undefined"
            :aria-expanded="index === 3 ? validationOpen : undefined" :aria-controls="index === 3 ? 'scan-validation' : undefined"
            :title="step.title" :aria-label="step.title + '，' + ({done:'已完成',warn:'有待核验项',current:'当前阶段',pending:'尚无归档'}[step.state])"
            @click="index === 3 && (validationOpen = !validationOpen)">
            <check-icon v-if="step.state === 'done'" aria-hidden="true"/><i v-else class="ov-stage-mark" aria-hidden="true"></i>
            <b>0{{ index + 1 }}</b><span class="ov-stage-full">{{ step.title }}</span><span class="ov-stage-short">{{ step.short }}</span>
            <chevron-down-icon v-if="index === 3" class="ov-fold" :class="{open:validationOpen}" aria-hidden="true"/>
          </component>
        </li>
      </ol>
      <validation-details v-if="validationOpen" id="scan-validation" :scan="scan" @close="validationOpen=false"/>
      <div v-if="scan.status!=='completed'" class="ov-archiving">
        <p>{{ scan.status==='failed' ? '扫描未完成，阶段结果尚未归档。' : '扫描执行中，阶段结果将在归档后显示。' }}</p>
      </div>
      <div v-else class="ov-columns">
        <section class="ov-directory" aria-label="行社风险清单">
          <button class="ov-summary" type="button" aria-label="可见范围与风险汇总，展开或收起口径校验"
            aria-controls="scan-validation" :aria-expanded="validationOpen" @click="validationOpen=!validationOpen">
            <span>可见 <b>{{ visibility?.visible_org_count ?? societies.length }}/{{ scopeCount ?? '—' }}</b> 家</span>
            <span v-for="l in ['重点关注','关注','未触发','提示','数据不足','等级未记录'].filter((s,i)=>i<3 || counts[s])" :key="l">
              <i class="ov-dot" :data-tone="tones[l]"></i>{{ l }} <b>{{ counts[l] || 0 }}</b></span>
            <span>单位拦截 <b>{{ validation?.unit_mismatch_count ?? '—' }}</b> 项</span>
          </button>
          <p v-if="visibility?.clipped" class="ov-clipped">{{ visibility.visible_note || '行级权限已裁剪，仅展示可见行社。' }}</p>
          <div class="ov-toolbar" role="group" aria-label="筛选行社">
            <t-input v-model="query" class="ov-search" size="small" clearable placeholder="搜索行社…" aria-label="搜索行社名称或地区">
              <template #prefix-icon><search-icon/></template>
            </t-input>
            <t-select v-model="district" class="ov-region" size="small" :options="regionOptions" aria-label="地区筛选"/>
            <t-select v-model="risk" class="ov-level" size="small" :options="riskOptions" aria-label="风险等级筛选"/>
            <div class="ov-chips" role="group" aria-label="风险面筛选">
              <button v-for="dim in dimensions" :key="dim.id" type="button"
                :disabled="!dim.executable || !scan.selection.dimension_ids.includes(dim.id)"
                :title="!dim.executable ? dim.status_message : !scan.selection.dimension_ids.includes(dim.id) ? '本次扫描未选择该风险面' : dim.title"
                :aria-pressed="dimension === dim.id" @click="dimension = dimension === dim.id ? '' : dim.id">
                {{ dimensionNames[dim.id] || dim.title }}{{ !dim.executable ? '·待接' : !scan.selection.dimension_ids.includes(dim.id) ? '·未选' : '' }}
              </button>
            </div>
          </div>
          <div class="ov-table-scroll">
            <table class="ov-table" aria-label="全体可见行社">
              <colgroup><col class="ov-org-col"><col class="ov-region-col"><col class="ov-level-col"><col class="ov-dim-col"><col><col class="ov-count-col"></colgroup>
              <thead><tr><th>行社名称</th><th class="ov-region-cell">地区</th><th>等级</th><th class="ov-dim-cell">命中风险面</th><th>核心指标实测</th><th>事项</th></tr></thead>
              <tbody><tr v-for="s in pageRows" :key="s.case_id" :class="{chosen:selectedOrgId===s.org_id}" @click="openRow(s)">
                <th scope="row"><button type="button" :aria-pressed="selectedOrgId===s.org_id" @click.stop="openRow(s)" :title="s.org_name">
                  <span class="ov-org-name">{{ s.org_name }}</span><small v-if="marker(s)" class="ov-report-mark">{{ marker(s) }}</small>
                </button></th>
                <td class="ov-region-cell" :title="s.region_name">{{ s.region_name || '—' }}</td>
                <td :title="s.data_incomplete ? '部分直接判断指标缺数或尚待接入' : level(s)"><i class="ov-dot" :data-tone="tones[level(s)]"></i>{{ level(s) }}</td>
                <td class="ov-dim-cell" :title="hitDimensions(s)">{{ hitDimensions(s) }}</td>
                <td :title="metrics[s.case_id]?.title">{{ metrics[s.case_id]?.label ?? '读取中…' }}</td><td>{{ s.issues.length }}</td>
              </tr></tbody>
            </table>
            <p v-if="!visible.length" class="ov-empty">没有符合条件的行社。<button type="button" @click="query='';district='all';risk='all';dimension=''">清除筛选</button></p>
          </div>
          <footer class="ov-paging"><span aria-live="polite">显示 {{ pageRows.length ? pageStart+1+'–'+(pageStart+pageRows.length) : '0' }} / 共 {{ visible.length }} 家</span>
            <div v-if="pageCount>1"><button type="button" aria-label="上一页行社" :disabled="page<=1" @click="page--"><chevron-left-icon/></button>
              <span>{{ page }} / {{ pageCount }}</span><button type="button" aria-label="下一页行社" :disabled="page>=pageCount" @click="page++"><chevron-right-icon/></button></div>
          </footer>
        </section>
        <section class="ov-detail" aria-label="行社归并与追问"><slot/></section>
      </div>
    </section>`,
});

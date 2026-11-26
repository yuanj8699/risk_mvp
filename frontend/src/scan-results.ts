/** 展示扫描时冻结的汇总和口径。清单筛选只改变当前视图，不改变任务范围。 */
import { computed, defineComponent, ref, watch, type PropType } from 'vue';
import type { RiskScan, ScanSociety } from './scan-types';
import './scan-results.css';

const statusLabels = { risk: '存在风险', insufficient: '数据不足', no_signal: '暂无异常' };
type FilterStatus = keyof typeof statusLabels | 'all';

export default defineComponent({
  name: 'ScanResults',
  props: {
    scan: { type: Object as PropType<RiskScan>, required: true },
    selectedOrgId: String,
  },
  emits: { select: (_society: ScanSociety | null) => true },
  setup(props, { emit }) {
    const region = ref(''), dimension = ref(''), query = ref('');
    const societies = computed(() => props.scan.society_results ?? []);
    const dimensions = computed(() => props.scan.risk_dimensions ?? []);
    const validation = computed(() => props.scan.validation_summary);
    const visibilityNote = computed(() => validation.value?.visibility?.clipped ? validation.value.visibility.visible_note : null);
    const regionNames = computed(() => [...new Set(societies.value.map(row => row.region_name).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'zh-CN')));
    const dimensionTitle = (id: string) => dimensions.value.find(d => d.id === id)?.title ?? id;
    const metricName = (id: string) => props.scan.knowledge_mapping?.[id]?.name ?? id;
    const orgName = (id: string) => societies.value.find(s => s.org_id === id)?.org_name ?? id;
    const counts = computed(() => {
      const totals = { risk: 0, no_signal: 0, insufficient: 0 };
      for (const row of societies.value) totals[row.status]++;
      return totals;
    });
    const status = ref<FilterStatus>(counts.value.risk && !societies.value.some(s =>
      s.org_id === props.selectedOrgId && s.status !== 'risk') ? 'risk' : 'all');
    const visible = computed(() => {
      const term = query.value.trim().toLowerCase();
      return societies.value.filter(s =>
        (status.value === 'all' || status.value === s.status) &&
        (!region.value || region.value === s.region_name) &&
        (!dimension.value || s.issues.some(i => i.dimension_id === dimension.value)) &&
        s.org_name.toLowerCase().includes(term)
      );
    });
    // 避免清单筛选后继续对看不见的行社追问或生成草稿。
    watch(visible, rows => {
      if (props.selectedOrgId && !rows.some(row => row.org_id === props.selectedOrgId)) emit('select', null);
    });
    const coverage = computed(() => {
      const v = validation.value;
      return v?.total_org_count ? new Intl.NumberFormat('zh-CN', {
        style: 'percent', maximumFractionDigits: 1,
      }).format(v.covered_org_count / v.total_org_count) : '未记录';
    });
    const ratios = computed(() => validation.value?.accounting_details.filter(row => row.numerator_id) ?? []);
    const sourceRows = computed(() => validation.value?.accounting_details.filter(row =>
      row.source_province_total != null || 'member_sum_matches_source_total' in row) ?? []);
    const riskTitles = (row: ScanSociety) => [...new Set(row.issues.map(issue => dimensionTitle(issue.dimension_id)))].join('、');
    return { societies, counts, dimensions, validation, visibilityNote, coverage, ratios, sourceRows, regionNames, visible,
      statusLabels, status, region, dimension, query, riskTitles, dimensionTitle, metricName, orgName };
  },
  template: `
    <div class="scan-results">
      <section aria-label="全辖总览" class="scan-overview">
        <h2>{{ validation?.visibility?.clipped ? '可见行社扫描总览' : '全辖扫描总览' }}</h2>
        <p v-if="visibilityNote" class="scan-visibility">{{ visibilityNote }}</p>
        <p class="scan-executive">{{ scan.executive_summary }}</p>
        <ul class="scan-counts" aria-label="扫描覆盖概览">
          <li><strong>{{ scan.summary.org_count }}</strong> 家行社</li>
          <li><strong>{{ counts.risk }}</strong> 家存在风险</li>
          <li><strong>{{ counts.no_signal }}</strong> 家暂无异常</li>
          <li><strong>{{ counts.insufficient }}</strong> 家数据不足</li>
          <li><strong>{{ scan.summary.triggered }}</strong> 条触发信号</li>
        </ul>
        <div class="scan-dimension-tags" aria-label="本次分析的风险面">
          <span v-for="id in scan.selection.dimension_ids" :key="id">{{ dimensionTitle(id) }}</span>
        </div>
      </section>

      <details v-if="validation" class="scan-validation">
        <summary>口径校验与指标覆盖 <small>覆盖 {{ validation.covered_org_count }} / {{ validation.total_org_count }} 家 · {{ validation.available_indicator_count }} 项可用指标</small></summary>
        <div class="scan-validation-body">
          <p v-if="visibilityNote" class="scan-visibility">{{ visibilityNote }}</p>
          <p>有可用当期观测的行社占 {{ coverage }}。缺失或质量异常 {{ validation.missing_count }} 条，期次滞后 {{ validation.stale_count }} 条，单位不一致 {{ validation.unit_mismatch_count }} 条。</p>
          <ul class="scan-coverage-list">
            <li v-for="d in dimensions" :key="d.id">
              <strong>{{ d.title }}</strong>
              <span>{{ d.local_indicator_count }} 项已接 · {{ d.missing_source_count }} 项缺源</span>
              <em v-if="!d.executable">{{ d.status_message }}</em>
            </li>
          </ul>
          <details v-if="validation.missing_source.length" class="scan-check-detail">
            <summary>本次缺源指标 · {{ validation.missing_source.length }} 项</summary>
            <ul><li v-for="id in validation.missing_source" :key="id">{{ metricName(id) }} <code>{{ id }}</code></li></ul>
          </details>
          <details v-if="validation.unit_mismatches.length" class="scan-check-detail">
            <summary>单位不一致 · 显示前 {{ Math.min(8, validation.unit_mismatches.length) }} 条</summary>
            <ul><li v-for="r in validation.unit_mismatches.slice(0,8)" :key="r.org_id + ':' + r.indicator_id">{{ orgName(r.org_id) }} · {{ metricName(r.indicator_id) }} · 观测 {{ r.unit }} / 目录 {{ r.catalog_unit }} / 业务口径 {{ r.expected_unit || '未登记' }}</li></ul>
          </details>
          <details v-if="validation.stale_observations.length" class="scan-check-detail">
            <summary>期次滞后 · 显示前 {{ Math.min(8, validation.stale_count) }} 条</summary>
            <ul><li v-for="r in validation.stale_observations.slice(0,8)" :key="r.org_id + ':' + r.indicator_id">{{ orgName(r.org_id) }} · {{ metricName(r.indicator_id) }} · {{ r.data_date }} · {{ r.issue }}</li></ul>
          </details>
          <details v-if="sourceRows.length" class="scan-check-detail">
            <summary>源表全辖合计与成员和勾稽 · {{ sourceRows.length }} 项</summary>
            <table aria-label="源表合计核对"><thead><tr><th scope="col">指标</th><th scope="col">源表全辖合计</th><th scope="col">成员和勾稽状态</th></tr></thead>
              <tbody><tr v-for="r in sourceRows" :key="r.indicator_id">
                <th scope="row">{{ r.indicator_name }}</th>
                <td>{{ r.source_province_total != null ? r.source_province_total + (r.unit || '') : '不可用' }}</td>
                <td>{{ r.metric_kind !== 'amount' ? '不适用' : r.member_sum_matches_source_total == null ? '范围、期次或数据不足，未勾稽' : r.member_sum_matches_source_total ? '相符' : '不符' }}
                  <small v-if="r.diff_from_source_total != null">（成员和减源表 {{ r.diff_from_source_total }}{{ r.unit }}）</small>
                </td>
              </tr></tbody>
            </table>
          </details>
          <h3>{{ validation.visibility?.clipped ? '可见行社分子分母覆盖' : '全辖加权核对' }}</h3>
          <ul v-if="ratios.length" class="scan-ratios">
            <li v-for="r in ratios" :key="r.indicator_id">
              <strong>{{ r.indicator_name }} · {{ r.weighted_value != null ? r.weighted_value + '%' : '无法重算' }}</strong>
              <span>可配对 {{ r.weighted_pair_count }} / {{ validation.total_org_count }} 家</span>
              <small>{{ r.note }}</small>
            </li>
          </ul>
          <p v-else>本次扫描没有可供加权核对的指标组合。</p>
          <ul class="scan-reminders"><li v-for="note in validation.reminders.filter(note => note !== visibilityNote)" :key="note">{{ note }}</li></ul>
        </div>
      </details>

      <section aria-label="行社风险问题清单" class="scan-directory">
        <header><h2>行社风险问题清单</h2><span aria-live="polite">{{ visible.length }} / {{ societies.length }} 家</span></header>
        <div class="scan-list-filters">
          <label>状态<select v-model="status"><option value="risk">存在风险</option><option value="all">全部 {{ societies.length }} 家</option><option value="insufficient">数据不足</option><option value="no_signal">暂无异常</option></select></label>
          <label>地区<select v-model="region"><option value="">全部地区</option><option v-for="name in regionNames" :key="name" :value="name">{{ name }}</option></select></label>
          <label>风险面<select v-model="dimension"><option value="">全部风险面</option><option v-for="id in scan.selection.dimension_ids" :key="id" :value="id">{{ dimensionTitle(id) }}</option></select></label>
          <label>行社名称<input v-model="query" type="search" placeholder="搜索行社名称"></label>
        </div>
        <div v-if="visible.length" class="scan-list-scroll">
          <div aria-hidden="true" class="scan-list-grid scan-list-head"><span>行社 / 地区</span><span>状态</span><span>涉及风险面</span><span>归并事项</span></div>
          <ul class="scan-society-list"><li v-for="s in visible" :key="s.org_id">
            <button type="button" class="scan-list-grid scan-society-row" :aria-pressed="selectedOrgId===s.org_id" @click="$emit('select',s)">
              <span class="scan-society-name"><strong>{{ s.org_name }}</strong><small>{{ s.region_name || '地区未记录' }}</small></span>
              <span><span :class="['scan-status','scan-status-'+s.status]">{{ statusLabels[s.status] }}</span><small v-if="s.data_incomplete && s.status==='risk'">部分数据不足</small></span>
              <span class="scan-society-dimensions">{{ riskTitles(s) || '—' }}</span>
              <span class="scan-issue-preview"><strong>{{ s.issues.length }} 项</strong><small v-if="s.issues.length">{{ s.issues[0].title }}</small></span>
            </button>
          </li></ul>
        </div>
        <p v-else class="scan-empty-filter">没有符合当前筛选条件的行社。</p>
      </section>
    </div>
  `,
});

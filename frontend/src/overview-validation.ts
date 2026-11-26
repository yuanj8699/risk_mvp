/** 按扫描归档展开 999999 源表、成员加总、加权口径与异常观测，保留空值的含义。 */
import { computed, defineComponent, type PropType } from 'vue';
import type { RiskScan } from './scan-types';

type Validation = NonNullable<RiskScan['validation_summary']>;
type Accounting = Validation['accounting_details'][number] & {
  province_total?: string | null; partial_total?: string | null; usable_org_count?: number;
  source_total_date?: string; source_region_totals?: Record<string, string>;
  region_totals?: Record<string, string>; data_dates?: string[]; denominator_id?: string;
  numerator_sum?: string; denominator_sum?: string; weighted_data_dates?: string[];
};
export default defineComponent({
  name: 'ValidationDetails',
  props: { scan: { type: Object as PropType<RiskScan>, required: true } },
  emits: ['close'],
  setup(props) {
    const validation = computed(() => props.scan.validation_summary as
      (Validation & { missing_observations?: { org_id: string; indicator_id: string }[] }) | undefined);
    const rows = computed(() => (validation.value?.accounting_details ?? []) as Accounting[]);
    const ratios = computed(() => rows.value.filter(r => r.numerator_id));
    const orgs = computed(() => new Map(props.scan.society_results?.map(r => [r.org_id, r.org_name])));
    const name = (id: string) => props.scan.knowledge_mapping?.[id]?.name ?? id;
    const amount = (v: string | null | undefined, unit?: string) => v == null ? '—' : v + (unit || '');
    const state = (r: Accounting) => r.metric_kind !== 'amount' ? '不适用' :
      r.member_sum_matches_source_total == null ? '未勾稽' : r.member_sum_matches_source_total ? '一致' : '不符';
    return { validation, rows, ratios, orgs, name, amount, state };
  },
  template: `
    <section class="ov-validation" aria-label="源表与指标口径校验明细" tabindex="0">
      <header><h2>999999 源表勾稽与口径校验</h2><button type="button" @click="$emit('close')">收起</button></header>
      <template v-if="validation">
        <p v-if="validation.visibility?.clipped" class="ov-clipped">{{ validation.visibility.visible_note }}</p>
        <p>当期覆盖 {{ validation.covered_org_count }} / {{ validation.total_org_count }} 家 · 可用指标 {{ validation.available_indicator_count }} 项 · 缺失或质量异常 {{ validation.missing_count }} 条 · 期次滞后 {{ validation.stale_count }} 条 · 单位异常 {{ validation.unit_mismatch_count }} 条</p>
        <div class="ov-check-scroll" v-if="rows.length">
          <table class="ov-check-table" aria-label="源表全辖合计与成员和勾稽">
            <thead><tr><th>指标</th><th>999999 源表</th><th>成员加总</th><th>差额</th><th>勾稽</th><th>源表日期</th></tr></thead>
            <tbody><tr v-for="r in rows" :key="r.indicator_id">
              <th scope="row">{{ r.indicator_name }}</th><td>{{ amount(r.source_province_total,r.unit) }}</td>
              <td>{{ r.metric_kind === 'amount' ? amount(r.province_total ?? r.partial_total,r.unit) : '不适用' }}{{ r.partial_total != null ? '（部分）' : '' }}</td>
              <td>{{ amount(r.diff_from_source_total,r.unit) }}</td><td :class="{'ov-mismatch':r.member_sum_matches_source_total === false}">{{ state(r) }}</td><td>{{ r.source_total_date || '—' }}</td>
            </tr></tbody>
          </table>
        </div>
        <p v-else>本次归档没有源表核对明细。</p>
        <details v-if="ratios.length"><summary>{{ validation.visibility?.clipped ? '可见范围分子分母覆盖' : '率类指标分子分母核对' }} · {{ ratios.length }} 项</summary>
          <ul><li v-for="r in ratios" :key="r.indicator_id"><b>{{ r.indicator_name }}</b> · {{ r.weighted_value == null ? '无法重算' : '重算 ' + r.weighted_value + '%' }} · 配对 {{ r.weighted_pair_count }} / {{ validation.total_org_count }} 家<span v-if="r.numerator_sum != null"> · 分子 {{ r.numerator_sum }} / 分母 {{ r.denominator_sum }}</span> · {{ r.note }}</li></ul>
        </details>
        <details v-if="scan.risk_dimensions?.length"><summary>风险面覆盖 · {{ scan.risk_dimensions.length }} 维</summary><ul>
          <li v-for="d in scan.risk_dimensions" :key="d.id">{{ d.title }} · 已接 {{ d.local_indicator_count }} 项 · 缺源 {{ d.missing_source_count }} 项 <span v-if="!d.executable">· {{ d.status_message }}</span></li></ul></details>
        <details v-if="validation.missing_source.length"><summary>缺源指标 · {{ validation.missing_source.length }} 项</summary><ul>
          <li v-for="id in validation.missing_source" :key="id">{{ name(id) }} · {{ id }}</li></ul></details>
        <details v-if="validation.missing_observations?.length"><summary>未取到的观测 · {{ validation.missing_observations.length }} 条</summary><ul>
          <li v-for="r in validation.missing_observations" :key="r.org_id+r.indicator_id">{{ orgs.get(r.org_id) || r.org_id }} · {{ name(r.indicator_id) }}</li></ul></details>
        <details v-if="validation.unit_mismatches.length"><summary>单位不一致 · {{ validation.unit_mismatches.length }} 条</summary><ul>
          <li v-for="r in validation.unit_mismatches" :key="r.org_id+r.indicator_id">{{ orgs.get(r.org_id) || r.org_id }} · {{ name(r.indicator_id) }} · 观测 {{ r.unit }} / 目录 {{ r.catalog_unit }} / 业务口径 {{ r.expected_unit || '未登记' }}</li></ul></details>
        <details v-if="validation.stale_observations.length"><summary>期次滞后 · {{ validation.stale_count }} 条</summary><ul>
          <li v-for="r in validation.stale_observations" :key="r.org_id+r.indicator_id">{{ orgs.get(r.org_id) || r.org_id }} · {{ name(r.indicator_id) }} · {{ r.data_date }} · {{ r.issue }}</li></ul></details>
        <details v-if="scan.executive_summary"><summary>本次扫描归档摘要</summary><p>{{ scan.executive_summary }}</p></details>
        <ul class="ov-check-notes"><li v-for="note in validation.reminders.filter(n=>n!==validation.visibility?.visible_note)" :key="note">{{ note }}</li></ul>
      </template>
      <p v-else>本次归档没有记录源表勾稽或指标口径校验结果。</p>
    </section>`,
});

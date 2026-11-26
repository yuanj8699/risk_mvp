/** 新建扫描使用服务端目录展示风险面；勾选值由工作台持有，提交时与快照一同发送。 */
import { computed, defineComponent, type PropType } from 'vue';
import type { RiskDimension } from './scan-types';
import './scan-setup.css';

export const executableDimensionIds = (dimensions: RiskDimension[]) =>
  dimensions.filter(item => item.executable).map(item => item.id);

export default defineComponent({
  name: 'ScanSetup',
  props: {
    scopeLabel: { type: String, required: true },
    dimensions: { type: Array as PropType<RiskDimension[]>, required: true },
    modelValue: { type: Array as PropType<string[]>, required: true },
  },
  emits: {
    'update:modelValue': (_ids: string[]) => true,
  },
  setup(props, { emit }) {
    const selected = computed({
      get: () => props.modelValue,
      set: ids => emit('update:modelValue', ids),
    });
    // 目录中缺源风险面的说明末尾已包含接入状态，行尾单列状态时去掉重复文字。
    const focus = (item: RiskDimension) =>
      item.executable ? item.description : item.description.replace(/；待内网取数接入。$/, '。');
    return { selected, focus };
  },
  template: `
    <section class="scan-setup" aria-labelledby="scan-setup-title">
      <header class="scan-setup-header">
        <h2 id="scan-setup-title">全辖风险扫描</h2>
        <p>扫描范围为{{ scopeLabel }}。</p>
      </header>
      <fieldset class="scan-dimension-list">
        <legend>内置风险面</legend>
        <label v-for="item in dimensions" :key="item.id"
               :class="['scan-dimension-choice', { pending: !item.executable }]">
          <input type="checkbox" v-model="selected" :value="item.id" :disabled="!item.executable"
                 :aria-label="item.title" :aria-describedby="'scan-description-' + item.id + ' scan-state-' + item.id" />
          <span class="scan-dimension-text">
            <strong>{{ item.title }}</strong>
            <span :id="'scan-description-' + item.id">{{ focus(item) }}</span>
          </span>
          <small :id="'scan-state-' + item.id" class="scan-dimension-state">
            {{ item.executable ? '已接 ' + item.local_indicator_count + ' 项指标' : item.status_message }}
          </small>
        </label>
      </fieldset>
      <p v-if="!selected.length" class="scan-selection-hint" role="status">
        {{ dimensions.some(item => item.executable) ? '请勾选至少一个风险面。' : '当前没有可执行的风险面。' }}
      </p>
    </section>
  `,
});

/** 读取同一运行和行社的追问记录；视图切换后不接收旧请求的结果。 */
import { computed, ref, watch, type ComputedRef } from 'vue';
import { api, send } from './api';
import type { TraceDiagnostics } from './trace-panel';

export interface ConversationScope { runId: string; caseId?: string }
export const smartqStatusLabel = {
  ok: '已取得结果', empty: '未命中', error: '补查失败', unavailable: '补查不可用',
} as const;

interface SmartQSupplement {
  status: keyof typeof smartqStatusLabel;
  rewritten_question: string;
  reason?: string;
  record_count: number;
  trailing_error?: boolean | string | null;
  note?: string;
}

interface Turn extends TraceDiagnostics {
  id: string;
  question: string;
  answer: string;
  mode: string;
  evidence: { id: string; label: string; text: string; source?: string }[];
  smartq?: SmartQSupplement;
}

export function useConversation(scope: ComputedRef<ConversationScope | null>) {
  const path = computed(() => scope.value ? '/api/agent/runs/' + encodeURIComponent(scope.value.runId) + '/conversation' : '');
  const historyUrl = computed(() => scope.value?.caseId
    ? path.value + '?case_id=' + encodeURIComponent(scope.value.caseId)
    : path.value);
  const turns = ref<Turn[]>([]);
  // 输入留在所属任务与行社；刷新或切换历史时不会带到另一组证据上。
  const drafts = ref(new Map<string, string>());
  const question = computed({
    get: () => drafts.value.get(historyUrl.value) ?? '',
    set: (value: string) => { if (historyUrl.value) drafts.value.set(historyUrl.value, value); },
  });
  const reading = ref(false), readError = ref(''), sendError = ref('');
  let revision = 0;

  async function loadConversation() {
    const url = historyUrl.value, request = ++revision;
    turns.value = []; readError.value = ''; sendError.value = ''; reading.value = !!url;
    if (!url) return;
    try {
      const result = await api<{ turns: Turn[] }>(url);
      if (request === revision && url === historyUrl.value) turns.value = result.turns;
    } catch (err) {
      if (request === revision && url === historyUrl.value) readError.value = err instanceof Error ? err.message : String(err);
    } finally {
      if (request === revision) reading.value = false;
    }
  }
  watch(historyUrl, loadConversation);

  async function sendQuestion() {
    const url = historyUrl.value, value = question.value.trim();
    if (!url || !value || reading.value || readError.value) return;
    sendError.value = '';
    try {
      const turn = await send<Turn>(path.value, { question: value, case_id: scope.value?.caseId });
      drafts.value.delete(url);
      if (url === historyUrl.value) turns.value.push(turn);
    } catch (err) {
      if (url === historyUrl.value) sendError.value = err instanceof Error ? err.message : String(err);
    }
  }
  return { turns, question, reading, readError, sendError, loadConversation, sendQuestion };
}

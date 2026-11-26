/** 工作台任务与归档契约。三类历史共用时间和状态展示，全辖扫描优先排列。 */
import type { RiskDimension, RiskScan } from './scan-types';
import type { AgentTrace, TraceDiagnostics, TraceRuntime } from './trace-panel';

export type ID = string | number;
export type TaskKind = 'risk_scan' | 'monitor' | 'code';
export interface Organization { id: ID; name: string; level: number }
export interface Snapshot { id: ID; name: string; data_date: string; source?: string; is_demo: boolean; status: string }
export interface Context { topic: string; snapshot_id: string; org_scope: string[]; as_of: string }
export interface TraceData { trace?: AgentTrace[]; runtime?: TraceRuntime; truncated?: boolean }
export interface Selection extends TraceData { mode?: string; topic?: string; focus_note?: string; dimension_ids?: string[]; error?: string; task_context?: Context; diagnostics?: TraceDiagnostics | null; code_result?: CodeResult }
export interface Summary { org_count?: number; triggered?: number; normal?: number; insufficient?: number; unconfigured?: number; case_count?: number }
export interface Run { id: ID; status: string; snapshot_id: ID; created_at: string; summary: Summary; selection: Selection }
export interface Task extends Run { kind: TaskKind; key: string; metadata: string; autoReportsRunning: boolean }
export interface Evidence { id: ID; indicator_name: string; value_display: string; reason: string; data_date: string; rule_id: ID }
export interface QueryEvidence { evidence_id: string; indicator_name: string; value_display: string; data_date: string; usable?: boolean }
export interface Analysis extends TraceData { status?: string; mode: string; facts: string[]; hypotheses: string[]; checks: string[]; queried_evidence?: QueryEvidence[]; evidence_references?: { checks?: { evidence_ids: string[] }[]; hypotheses?: { evidence_ids: string[] }[] } }
export interface Letter { id: ID; content: string; revision: number; status: string }
export interface RiskCase { id: ID; org_id: ID; org_name: string; region_name: string; headline: string; evidence_count: number; evidence?: Evidence[]; analysis?: Analysis; letter?: Letter | null }
export interface SourceEvidence extends QueryEvidence { org_id: string; org_name?: string; source_record_id: string }
export interface Finding { org_id: string; statement: string; evidence_ids: string[] }
export interface CodeResult extends TraceData { mode: string; summary: string; findings?: Finding[]; evidence?: SourceEvidence[]; result_file?: string; coverage?: { checked_checks: number; triggered: number; insufficient: number; unconfigured: number } }
export interface TaskFile { path: string; size: number; sha256: string }
export interface Execution { execution_id: string; script_path?: string; exit_code: number; stdout: string; stderr: string }
export interface CodeDetail extends Run { result?: CodeResult | null; executions: Execution[]; files: TaskFile[]; workspace_error?: string | null }
export interface Bootstrap { meta: { data_fetcher?: 'local' | 'quickbi'; smartq_followup_available?: boolean; agent_mode: string; agent_provider?: string; agent_model?: string }; organizations: Organization[]; snapshots: Snapshot[]; risk_dimensions: RiskDimension[] }
export interface ImportResult { snapshot_id: ID; row_count: number; warnings: string[] }

// 两类旧接口的UTC时间编码不同；无偏移时间补Z后再统一排序和显示。
const timestamp = (value: string) => new Date(/[zZ]$|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
const historyClock = new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: 'Asia/Shanghai' });
export function dateTime(value: string) {
  const date = timestamp(value);
  return Number.isFinite(date.getTime()) ? historyClock.format(date).replace('/', '-') : '';
}
export const kindName = (kind: TaskKind) => ({ risk_scan: '全辖风险扫描', monitor: '规则监测', code: '自主代码分析' })[kind];
export const agentModeName = (mode?: string) => ({ 'pi-demo': '离线演示', 'pi-model': '模型模式', template: '模板分析' }[mode ?? ''] ?? '模式未记录');
export const statusName = (status: string) => ({ completed: '已完成', running: '执行中', failed: '失败', no_candidates: '无补查候选' }[status] ?? status);
export const taskTitle = (task: Task) => task.selection.task_context?.topic || task.selection.topic || kindName(task.kind);
export const canDeleteTask = (task: Task) => task.status !== 'running' && !task.autoReportsRunning;
export function taskResource(task: Task) {
  const collection = { risk_scan: 'risk-scans', monitor: 'runs', code: 'code-runs' }[task.kind];
  return `/api/${collection}/${encodeURIComponent(String(task.id))}`;
}

// 仅从当次归档提取模型；当前服务配置可能已经切换过供应商。
function modelLabel(runtime?: TraceRuntime, mode?: string) {
  if ([runtime?.mode, mode].some(value => value === 'demo' || value === 'pi-demo')) return '演示';
  const model = runtime?.model || '', provider = runtime?.provider || '';
  if (/deepseek/i.test(model) || /deepseek/i.test(provider)) return 'DeepSeek';
  if (/^openai$/i.test(provider)) return 'OpenAI';
  return model || provider || ({ 'pi-model': '模型模式', template: '模板分析' }[mode ?? ''] ?? '');
}

function scanReportLabel(scan: RiskScan | undefined) {
  if (!scan) return '';
  const progress = scan.auto_reports;
  if (progress?.status === 'running') return `生成中 ${progress.completed}/${progress.total}`;
  // 同一行社可同时出现在撰函归档与自动队列；重新生成的旧版本也仍是一份报告。
  const orgs = new Set(Object.keys(scan.letter_drafts ?? {}));
  for (const item of progress?.items ?? []) {
    if (item.revision != null || item.status === 'completed') orgs.add(item.org_id);
  }
  return orgs.size || scan.letter_drafts ? `${orgs.size} 份报告` : '';
}

// 扫描列表也返回完整归档；侧栏只持有任务元数据，详情按需读取。
export function asTask(run: Run, kind: TaskKind): Task {
  const { id, created_at, snapshot_id, status, summary, selection } = run;
  const { task_context, mode, focus_note, dimension_ids, error } = selection;
  const scan = kind === 'risk_scan' ? run as RiskScan : undefined;
  const runtime = scan?.runtime ?? selection.code_result?.runtime ?? selection.runtime ?? selection.diagnostics?.runtime;
  const metadata = [dateTime(created_at), modelLabel(runtime, scan?.mode ?? mode), scanReportLabel(scan)].filter(Boolean).join(' · ');
  return { id, created_at, snapshot_id, status, summary, kind, key: `${kind}:${id}`,
    metadata, autoReportsRunning: scan?.auto_reports?.status === 'running',
    selection: kind === 'risk_scan' ? { task_context, mode, focus_note, dimension_ids, error } : selection };
}

export function compareHistory(a: Task, b: Task) {
  return Number(b.kind === 'risk_scan') - Number(a.kind === 'risk_scan')
    || timestamp(b.created_at).getTime() - timestamp(a.created_at).getTime();
}


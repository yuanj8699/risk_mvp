/** 全辖扫描的归档字段。运行中和失败任务没有已完成扫描的结果字段。 */
import type { AgentTrace, TraceDiagnostics, TraceRuntime } from './trace-panel';

export interface RiskDimension {
  id: string;
  dimension_id: string;
  title: string;
  description: string;
  executable: boolean;
  local_indicator_count: number;
  missing_source_count: number;
  status_message: string;
}

export interface ScanIssue {
  org_id: string;
  dimension_id: string;
  title: string;
  aspect: string;
  severity: string;
  cause_analysis: string;
  check_suggestion: string;
  evidence_ids: string[];
}

export interface ScanSociety {
  org_id: string;
  org_name: string;
  region_name: string;
  case_id: string;
  status: 'risk' | 'no_signal' | 'insufficient';
  data_incomplete: boolean;
  issues: ScanIssue[];
  signals: { dimension_id: string; indicator_id: string; reason: string; evidence_id: string }[];
}

interface ObservationKey { org_id: string; indicator_id: string }

interface ValidationSummary {
  visibility?: {
    expected_org_count: number;
    visible_org_count: number;
    clipped: boolean;
    visible_note: string | null;
  };
  total_org_count: number;
  covered_org_count: number;
  available_indicator_count: number;
  missing_count: number;
  missing_value_count: number;
  missing_source: string[];
  stale_count: number;
  unit_mismatch_count: number;
  unit_mismatches: (ObservationKey & { unit: string; expected_unit?: string; catalog_unit: string })[];
  stale_observations: (ObservationKey & { data_date: string; issue: string })[];
  accounting_details: {
    indicator_id: string;
    indicator_name: string;
    metric_kind: string;
    unit?: string;
    source_province_total?: string | null;
    member_sum_matches_source_total?: boolean | null;
    diff_from_source_total?: string | null;
    numerator_id?: string;
    weighted_value?: string | null;
    weighted_pair_count?: number;
    note: string;
  }[];
  reminders: string[];
}

/** 当前归档返回 rule、updated_at 和逐家状态；历史扫描可能完全没有队列。 */
export interface AutoReportItem {
  org_id: string;
  org_name: string;
  p0_count: number;
  status: 'queued' | 'running' | 'completed' | 'failed';
  revision: number | null;
  error: string | null;
}

export interface AutoReports {
  status: 'idle' | 'running' | 'completed' | 'partial' | 'partial_failed' | 'failed';
  rule?: string;
  policy?: string;
  total: number;
  completed: number;
  failed: number;
  running_orgs?: string[];
  items: AutoReportItem[];
  started_at: string | null;
  updated_at?: string;
  finished_at?: string | null;
}

export interface RiskScan {
  auto_reports?: AutoReports;
  letter_drafts?: Record<string, LetterDiagnostics>;
  id: string;
  status: string;
  snapshot_id: string;
  created_at: string;
  summary: { org_count?: number; case_count?: number; triggered?: number };
  selection: {
    dimension_ids: string[];
    focus_note: string;
    mode: string;
    task_context: { topic: string; snapshot_id: string; org_scope: string[]; as_of: string };
    error?: string;
    diagnostics?: TraceDiagnostics | null;
  };
  knowledge_mapping?: Record<string, { name: string }>;
  risk_dimensions?: RiskDimension[];
  validation_summary?: ValidationSummary;
  executive_summary?: string;
  society_results?: ScanSociety[];
  trace?: AgentTrace[];
  runtime?: TraceRuntime;
  mode?: string;
  is_demo?: boolean;
}

export interface LetterDiagnostics extends TraceDiagnostics {
  org_id: string; org_name: string; revision: number; mode: string; updated_at: string;
}

/** Python bridge与TypeScript调用方共用的JSON契约；字段名保持与后端一致。 */
export type AgentMode = "demo" | "model";
export type ResultMode = `pi-${AgentMode}`;

export type Indicator = {
  id: string;
  name: string;
  [key: string]: unknown;
};

/** 规则引擎产生的固定事实，Agent只读，不重算金额、排名或阈值。 */
export type Evidence = {
  id: string;
  indicator_id: string;
  indicator_name: string;
  value_display: string;
  reason: string;
  rule_kind: string;
  [key: string]: unknown;
};

/** 后端冻结的任务范围；MCP服务还会按run_id从数据库加载并再次约束范围。 */
export type TaskContext = {
  run_id: string;
  topic: string;
  snapshot_id: string;
  as_of: string;
  rule_snapshot: Record<string, unknown>[];
  org_scope: string[];
  history_snapshot_ids: string[];
  source: string;
};

/** 只保存MCP真正查到的观测值；缺失/过期结果可展示缺口，但不能进入引用池。 */
export type QueriedEvidence = {
  id: string;
  evidence_id: string;
  org_id: string;
  indicator_id: string;
  indicator_name: string;
  value: string | number | null;
  value_display: string;
  data_date: string;
  snapshot_id: string;
  quality_status: string;
  usable?: boolean;
  [key: string]: unknown;
};

export type RiskCaseInput = {
  case_key: string;
  evidence: Evidence[];
};

export type SelectRequest = {
  operation: "select";
  topic: string;
  indicators: Indicator[];
  mandatory_ids: string[];
};

export type ExplainBatchRequest = {
  operation: "explain_batch";
  context: TaskContext;
  budget_ms: number;
  cases: RiskCaseInput[];
};

/** 一次探索覆盖整个冻结机构范围，数据在隔离Python中计算，不预载进模型上下文。 */
export type ExploreRequest = { operation: "explore"; context: TaskContext; budget_ms: number };
export type ExplorationFinding = { org_id: string; statement: string; evidence_ids: string[] };
export type ExplorationSubmission = { summary: string; findings: ExplorationFinding[]; execution_ids: string[]; result_file: string };
export type ZeroCandidates = { outcome: "zero-candidates"; plan_id: string; summary: string; coverage: Record<string, unknown> };
export type ExploreResult = (ExplorationSubmission | ZeroCandidates) & {
  mode: ResultMode;
  trace: AgentTrace[];
  runtime: RuntimeInfo;
};

/** 追问所用的证据及最近对话均由 Python 按选中任务和机构加载。 */
export type FollowUpEvidence = { id: string; label: string; text: string; source?: string };
export type SmartQQueryType = "single" | "total" | "diff" | "ratio" | "rank" | "extremum"
  | "detail" | "count" | "yoy" | "mom" | "check" | "filter" | "other";
export type SmartQLookupRequest = {
  rewritten_question: string; reason: string; metric_hint: string;
  target_org: string; target_date: string; query_type: SmartQQueryType;
};
export type FollowUpRequest = {
  operation: "follow_up";
  question: string;
  context: { topic: string; as_of: string; organization: string };
  history: { question: string; answer: string; rewritten_question?: string }[];
  evidence: FollowUpEvidence[];
  smartq?: { enabled: boolean; attempted?: boolean; status?: "ok" | "empty" | "error" | "unavailable";
    note?: string; rewritten_question?: string; domain_guidance?: string };
};
export type FollowUpResult = { answer: string; evidence_ids: string[]; trace: AgentTrace[]; runtime: RuntimeInfo;
  smartq_lookup?: SmartQLookupRequest };

/** 全辖初筛候选及其原始观测；归并只能引用同一机构的有效观测。 */
export type SynthesisEvidence = { evidence_id: string; org_id: string; quality_status: string; value: string | number | null; [key: string]: unknown };
export type SynthesisSignal = { evidence_id: string; dimension_id: string; aspect: string; reason: string; [key: string]: unknown };
export type SynthesisCandidate = { org_id: string; org_name: string; signals: SynthesisSignal[]; evidence: SynthesisEvidence[] };
export type SynthesizeRequest = {
  operation: "synthesize_risks"; budget_ms: number; knowledge_prompt: string; dimension_ids: string[];
  validation_summary: Record<string, unknown> & { visibility?: { clipped: boolean } };
  candidates: SynthesisCandidate[];
};
export type SynthesisIssue = { org_id: string; dimension_id: string; aspect: string; title: string; severity: string;
  cause_analysis: string; check_suggestion: string; evidence_ids: string[] };
export type SynthesizeResult = { mode: ResultMode; issues: SynthesisIssue[]; executive_summary: string; trace: AgentTrace[]; runtime: RuntimeInfo };
export type LetterMetric = { indicator_id: string; indicator_name: string; evidence_id: string; value: string | number; unit: string;
  value_display: string; formal_display?: string; data_date: string; rank: number | null; population: number | null };
export type LetterBrief = { run_id: string; org_id: string; org_name: string; region_name: string; period_label: string; data_date: string;
  issue_date: string; business_overview: LetterMetric[]; available_dimensions: {dimension_id: string; dimension_title: string}[];
  signal_overview: Record<string, unknown>; mandatory_p0_evidence_ids: string[]; allowed_evidence_ids: string[] };
export type LetterParagraph = { heading: string; body: string };
export type StructuredLetter = { title: string; recipient: string; opening: string; overview: string; risk_sections: (LetterParagraph & { evidence_ids: string[] })[]; suggestions: LetterParagraph[]; closing_requirement: string; appendix_evidence_ids: string[] };
export type DraftLetterRequest = { operation: 'draft_risk_letter'; budget_ms: number; brief: LetterBrief };
export type DraftLetterResult = { letter: StructuredLetter; read_evidence_ids: string[]; trace: AgentTrace[]; runtime: RuntimeInfo };
export type RiskAgentRequest = SelectRequest | ExplainBatchRequest | ExploreRequest | FollowUpRequest | SynthesizeRequest | DraftLetterRequest;

/** 建议须引用本事件证据；引用有效不代表原因已经查实。 */
export type Proposal = { text: string; evidence_ids: string[] };
export type AnalysisProposal = { hypotheses: Proposal[]; checks: Proposal[] };
export type SelectionProposal = { indicator_ids: string[]; reasons: string[] };

export type AgentTrace = {
  id?: string;
  parent_id?: string;
  case_key?: string;
  phase?: "select" | "explain" | "explore" | "follow_up" | "synthesize_risks" | "draft_risk_letter";
  started_at?: string;
  event: "request" | "tool";
  transport?: "mcp" | "local";
  tool?: string;
  status?: "running" | "ok" | "error" | "cancelled";
  error?: { code: string; message: string };
  recoverable?: boolean;
  recovery_attempt?: number;
  recovery_limit?: number;
  recovery_status?: "retry_available" | "exhausted" | "recovered";
  request?: number;
  arguments?: Record<string, unknown>;
  evidence_ids?: string[];
  execution_id?: string;
  exit_code?: number;
  parent_tool?: "risk_js";
  duration_ms?: number;
  response_bytes?: number;
  plan_id?: string;
  model_provider?: string;
  model_id?: string;
  llm_connected?: boolean;
  request_summary?: { message_count: number; tool_names: string[]; payload_bytes?: number };
  reasoning_content?: string | null;
  reasoning_source?: "provider_reasoning_content" | "pi_thinking" | null;
  reasoning_truncated?: boolean;
  usage?: { input_tokens: number; output_tokens: number; reasoning_tokens: number | null; cache_read_tokens: number; cache_write_tokens: number; total_tokens: number; cost_total: number } | null;
  retry_count?: number | null;
  retry_observation?: "fetch" | "unavailable" | "not_applicable";
  http_attempts?: { attempt: number; started_at: string; duration_ms: number; status_code: number | null; error?: { code: string; message: string } }[];
};

export type RuntimeInfo = {
  framework: "pi-agent-core";
  version: string;
  provider: string;
  model: string;
  requests: number;
  tool_calls: number;
  nested_tool_calls?: number;
  host_tool_calls?: number;
  model_tool_result_bytes?: number;
  mode: AgentMode;
  llm_connected: boolean;
  duration_ms?: number;
  trace_truncated?: boolean;
};
export type AgentDiagnostics = { trace: AgentTrace[]; runtime?: RuntimeInfo | Record<string, unknown>; truncated?: boolean };

type ResultMetadata = {
  mode: ResultMode;
  limitations: string[];
  trace: AgentTrace[];
  runtime: RuntimeInfo;
};

export type SelectResult = ResultMetadata & SelectionProposal & { topic: string };

export type CaseAnalysis = ResultMetadata & {
  facts: string[];
  hypotheses: string[];
  checks: string[];
  evidence_references: AnalysisProposal;
  queried_evidence: QueriedEvidence[];
};

export type ExplainBatchResult = { analyses: Record<string, CaseAnalysis> };
export type RiskAgentResult = SelectResult | ExplainBatchResult | ExploreResult | FollowUpResult | SynthesizeResult | DraftLetterResult;

/** CLI失败时返回此结构并以退出码1结束，不输出原始模型响应或认证信息。 */
export type AgentErrorResponse = { error: { code: string; message: string }; diagnostics?: AgentDiagnostics };

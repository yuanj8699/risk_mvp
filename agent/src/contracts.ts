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

export type RiskAgentRequest = SelectRequest | ExplainBatchRequest;

/** 建议须引用本事件证据；引用有效不代表原因已经查实。 */
export type Proposal = { text: string; evidence_ids: string[] };
export type AnalysisProposal = { hypotheses: Proposal[]; checks: Proposal[] };
export type SelectionProposal = { indicator_ids: string[]; reasons: string[] };

export type AgentTrace = {
  event: "request" | "tool";
  transport?: "mcp" | "local";
  tool?: string;
  status?: "ok" | "error";
  request?: number;
  arguments?: Record<string, unknown>;
  evidence_ids?: string[];
};

export type RuntimeInfo = {
  framework: "pi-agent-core";
  version: string;
  provider: string;
  model: string;
  requests: number;
  tool_calls: number;
  mode: AgentMode;
  llm_connected: boolean;
};

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
export type RiskAgentResult = SelectResult | ExplainBatchResult;

/** CLI失败时返回此结构并以退出码1结束，不输出原始模型响应或认证信息。 */
export type AgentErrorResponse = { error: { code: string; message: string } };

// Shared domain types — mirror the FastAPI models in src/backend/api/server.py

export type ArtifactKind = 'markdown' | 'json' | 'text';

export interface ArtifactScores {
  cdets_defect_score?: string;
  ai_confidence?: string;
  automation_readiness?: string;
  test_coverage_confidence?: string;
  coverage_gap?: string;
  source?: string;
  primary_ap?: string;
  sub_ap?: string;
}

export interface Priority {
  priority: string; // P1..P4
  score: number;
  rationale: string;
}

export interface ArtifactFile {
  name: string;
  kind: ArtifactKind;
  size: number;
}

export interface ArtifactSummary {
  cdets_id: string;
  headline?: string;
  file_count: number;
  updated_at?: string;
  scores: ArtifactScores;
  severity?: string;
  regression?: boolean;
  priority: Priority;
}

export interface StageTrace {
  status?: string;
  duration?: number;
  input_tokens?: number;
  output_tokens?: number;
  iterations?: number;
  error?: string | null;
  [key: string]: unknown;
}

export interface TokenTotals {
  input: number;
  output: number;
  total: number;
}

export interface ArtifactDetail extends ArtifactSummary {
  files: ArtifactFile[];
  stage_traces?: Record<string, StageTrace> | null;
  token_totals?: TokenTotals | null;
  traces_saved_at?: string | null;
}

export interface ArtifactFileContent {
  cdets_id: string;
  name: string;
  kind: ArtifactKind;
  content: string;
}

export interface AnalyzeResult {
  cdets_id: string;
  status: 'ok' | 'error';
  message: string;
  component?: string;
  primary_ap?: string;
  sub_ap?: string;
  cafy_verdict?: string;
  file_count: number;
}

export interface Stats {
  total_defects: number;
  analyzed_with_scores: number;
  avg_cdets_score?: number;
  score_grades: Record<string, number>;
  coverage_buckets: Record<string, number>;
  ap_distribution: Record<string, number>;
  coverage_gaps: Record<string, number>;
  priority_buckets: Record<string, number>;
}

export interface SimilarDefect {
  cdets_id: string;
  headline?: string;
  component?: string;
  similarity: number;
  primary_ap?: string;
  cdets_defect_score?: string;
}

export interface SimilarResponse {
  cdets_id: string;
  neighbors: SimilarDefect[];
}

export interface MarkdownResponse {
  cdets_id: string;
  markdown: string;
  error?: string;
}

export interface ChatResponse {
  cdets_id: string;
  answer: string;
  error?: string;
}

export interface LogTriageResponse {
  markdown: string;
  matched_ids: string[];
  error?: string;
}

// ─── LangGraph pipeline jobs (live progress) ───
export type JobStatus = 'queued' | 'running' | 'done' | 'error' | 'awaiting_human';
export type NodeStatus = 'running' | 'ok' | 'failed' | 'skipped' | 'awaiting_human';

export interface NodeEvent {
  node: string;
  status: NodeStatus;
  duration?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  iterations?: number | null;
  error?: string | null;
  ts: string;
}

export interface JobResult {
  elapsed_seconds?: number;
  artifact_dir?: string;
  scorecard_path?: string;
  testcase_path?: string;
  cdets_schema_path?: string;
  union_schema_path?: string;
  cdet_ai_score?: number;
  automation_readiness?: string;
  test_coverage_grade?: string;
  delivery_status?: string;
}

export interface Job {
  job_id: string;
  cdets_id: string;
  status: JobStatus;
  nodes: NodeEvent[];
  started_at: string;
  finished_at?: string | null;
  error?: string | null;
  result?: JobResult | null;
  missing_info_request?: MissingInfoRequest | null;
  review_url?: string | null;
}

export interface StartJobResponse {
  job_id: string;
  cdets_id: string;
  status: JobStatus;
  model?: string | null;
}

// ─── Human-in-the-loop ───
export type MissingFieldInputType = 'text' | 'textarea' | 'select';

export interface MissingFieldRequest {
  field: string;
  label?: string;
  why_needed?: string;
  example?: string;
  input_type?: MissingFieldInputType;
  options?: string[];
}

export interface MissingInfoRequest {
  cdets_id: string;
  cdet_ai_score?: number;
  score_threshold?: number;
  headline?: string | null;
  summary_for_reviewer?: string;
  missing_fields: MissingFieldRequest[];
  free_form_questions?: string[];
  generated_at?: string;
}

export interface ReviewRequestEnvelope {
  job_id: string;
  cdets_id: string;
  status: JobStatus;
  review_url?: string | null;
  missing_info_request: MissingInfoRequest;
}

export interface HumanInputPayload {
  fields?: Record<string, string>;
  free_form_answers?: string[];
}

// ─── Post-run summary (Defects view "Summary" tab) ───
export type ModelChoice = 'sonnet' | 'opus';

export interface SummaryBugAnalysis {
  headline?: string;
  component?: string;
  primary_ap?: string;
  sub_ap?: string;
  version?: string;
  severity?: string;
  status?: string;
  engineer?: string;
  submitted_on?: string;
  issue_url?: string;
  repro?: {
    reproducibility?: string;
    triggers?: Array<string | { name?: string; trigger?: string; details?: string }>;
    soak_required?: boolean | null;
    traffic_required?: boolean | null;
  };
  behavior?: {
    expected?: string;
    actual?: string;
    impact_severity?: string;
    impact_priority?: string;
  };
  failure_category?: string;
  rca?: {
    available?: boolean;
    root_cause?: string;
    fix_approach?: string;
    confidence?: string;
    sources?: string[];
  };
  qualification?: {
    completion_status?: string;
    ai_eligible?: boolean;
    missing_fields?: string[];
    blockers?: string[];
  };
}

export interface SummaryScorecard {
  score_value?: number | null;
  grade?: string | null;
  status?: string | null;
  required_fields?: { filled?: number; total?: number; percent?: number };
  weighted?: { earned?: number; total_applicable?: number; final_percent?: number };
  ai_confidence?: {
    overall_percent?: number | null;
    grade?: string | null;
    fields_at?: { high?: number; medium?: number; low?: number; none?: number };
  };
  automation_readiness?: {
    verdict?: string | null;
    fields_ready?: number;
    conditional?: number;
    not_ready?: number;
  };
  dt_testability?: {
    alert?: boolean;
    alert_text?: string;
    triggered_count?: number;
    triggered_list?: string[];
  };
  weakest_fields?: Array<{
    path: string;
    label?: string;
    weight?: number;
    quality?: number;
    value?: unknown;
    citation?: string;
  }>;
  blockers?: string[];
}

export interface SummaryCoverage {
  cafy_verdict?: string;
  coverage_gap?: string;
  gap_classification?: string;
  test_coverage_confidence?: number | null;
  test_coverage_grade?: string;
  coverage_classification?: string;
  existing_tests_count?: number;
  existing_verifiers_count?: number;
  new_scenarios_count?: number;
  has_techzone?: boolean;
}

export interface SummaryDelivery {
  mongo_pushed?: boolean;
  tftp_delivered?: boolean;
  email_sent?: boolean;
  delivery_status?: string;
  scorecard_path?: string;
  testcase_path?: string;
  rca_md_path?: string;
  schema_path?: string;
}

export interface RunSummary {
  version: number;
  saved_at?: string | null;
  cdets_id: string;
  artifact_dir?: string;
  model_used?: string | null;
  bug_analysis: SummaryBugAnalysis;
  scorecard: SummaryScorecard;
  coverage?: SummaryCoverage | null;
  delivery?: SummaryDelivery | null;
}

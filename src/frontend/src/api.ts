// API client for the FL Agent Console backend.

import type {
  AnalyzeResult,
  ArtifactDetail,
  ArtifactFileContent,
  ArtifactSummary,
  ChatResponse,
  HumanInputPayload,
  Job,
  LogTriageResponse,
  MarkdownResponse,
  MissingInfoRequest,
  ModelChoice,
  ReviewRequestEnvelope,
  RunSummary,
  SimilarResponse,
  StartJobResponse,
  Stats,
} from './types';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export const api = {
  getStats: () => request<Stats>('/api/stats'),
  listArtifacts: () => request<ArtifactSummary[]>('/api/artifacts'),
  getArtifact: (id: string) => request<ArtifactDetail>(`/api/artifacts/${encodeURIComponent(id)}`),
  getArtifactFile: (id: string, name: string) =>
    request<ArtifactFileContent>(
      `/api/artifacts/${encodeURIComponent(id)}/file?name=${encodeURIComponent(name)}`,
    ),
  deleteArtifact: (id: string) =>
    request<{ ok: boolean; cdets_id: string; deleted: boolean }>(
      `/api/artifacts/${encodeURIComponent(id)}`,
      { method: 'DELETE' },
    ),
  analyze: (id: string) =>
    request<AnalyzeResult>('/api/artifacts/analyze', {
      method: 'POST',
      body: JSON.stringify({ cdets_id: id }),
    }),
  similar: (id: string) =>
    request<SimilarResponse>(`/api/artifacts/${encodeURIComponent(id)}/similar`),
  remediation: (id: string) =>
    request<MarkdownResponse>(`/api/artifacts/${encodeURIComponent(id)}/remediation`),
  enrichment: (id: string) =>
    request<MarkdownResponse>(`/api/artifacts/${encodeURIComponent(id)}/enrichment`),
  chat: (id: string, question: string) =>
    request<ChatResponse>(`/api/artifacts/${encodeURIComponent(id)}/chat`, {
      method: 'POST',
      body: JSON.stringify({ cdets_id: id, question }),
    }),
  triageLog: (logText: string) =>
    request<LogTriageResponse>('/api/triage/log', {
      method: 'POST',
      body: JSON.stringify({ log_text: logText }),
    }),
  generationStatus: (id: string) =>
    request<{ cdets_id: string; generating: boolean }>(
      `/api/artifacts/${encodeURIComponent(id)}/status`,
    ),

  // ─── LangGraph pipeline jobs ───
  startJob: (id: string, opts?: { dryRun?: boolean; model?: ModelChoice | null }) =>
    request<StartJobResponse>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({
        cdets_id: id,
        dry_run: opts?.dryRun ?? false,
        model: opts?.model ?? null,
      }),
    }),
  getJob: (jobId: string) => request<Job>(`/api/jobs/${encodeURIComponent(jobId)}`),
  jobEventsUrl: (jobId: string) => `/api/jobs/${encodeURIComponent(jobId)}/events`,
  getRunSummary: (id: string) =>
    request<RunSummary>(`/api/artifacts/${encodeURIComponent(id)}/summary`),

  // ─── Human-in-the-loop ───
  getReview: (jobId: string) =>
    request<ReviewRequestEnvelope>(`/api/jobs/${encodeURIComponent(jobId)}/review`),
  getMissingInfo: (cdetsId: string) =>
    request<MissingInfoRequest>(
      `/api/artifacts/${encodeURIComponent(cdetsId)}/missing_info`,
    ),
  resumeJob: (jobId: string, payload: HumanInputPayload) =>
    request<StartJobResponse>(`/api/jobs/${encodeURIComponent(jobId)}/resume`, {
      method: 'POST',
      body: JSON.stringify({ human_input: payload }),
    }),
};

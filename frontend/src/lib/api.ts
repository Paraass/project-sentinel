const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export interface RunResponse {
  run_id: string;
  name: string | null;
  current_state: string;
  created_at: string;
  updated_at: string;
  document_count: number;
  resume_stage: string;
}

export interface ReviewItemResponse {
  id: string;
  run_id: string;
  item_type: string;
  source_reference: string;
  content: Record<string, unknown>;
  decision: string;
  decision_reason: string | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
}

export interface ReportResponse {
  id: string;
  run_id: string;
  version: number;
  content: Record<string, unknown>;
  is_current: boolean;
  created_at: string;
}

export interface ChangelogEntryResponse {
  id: string;
  report_version: number;
  summary: string;
  source_document_ids: string[];
  affected_claim_ids: string[];
  created_at: string;
}

interface ErrorResponse {
  detail: string;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`);
  } catch {
    throw new ApiError(0, "Could not reach the Sentinel backend.");
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}.`;

    try {
      const body = (await response.json()) as ErrorResponse;
      if (body?.detail) {
        detail = body.detail;
      }
    } catch {
      // Fall back to the generic HTTP error above.
    }

    throw new ApiError(response.status, detail);
  }

  return (await response.json()) as T;
}

export function getRun(runId: string): Promise<RunResponse> {
  return request<RunResponse>(`/runs/${encodeURIComponent(runId)}`);
}

export function getReviewItems(runId: string): Promise<ReviewItemResponse[]> {
  return request<ReviewItemResponse[]>(
    `/runs/${encodeURIComponent(runId)}/review-items`,
  );
}

export function getCurrentReport(runId: string): Promise<ReportResponse> {
  return request<ReportResponse>(`/runs/${encodeURIComponent(runId)}/report`);
}

export function getChangelog(
  runId: string,
): Promise<ChangelogEntryResponse[]> {
  return request<ChangelogEntryResponse[]>(
    `/runs/${encodeURIComponent(runId)}/changelog`,
  );
}

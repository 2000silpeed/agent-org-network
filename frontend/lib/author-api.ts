// Owner-side OKF authoring client (ADR 0030). The authoring loop runs owner-side:
// raw document + staged drafts stay local, only the approved index (목차) is
// published to central. These routes are the OKF 저작면 (owner-side), distinct
// from the central card builder (/builder/*).

export type AuthorStageState = "done" | "active" | "pending";

export interface AuthorStage {
  key: string;
  label: string;
  state: AuthorStageState;
}

export interface AuthorConcept {
  concept_id: string;
  title: string;
  core_question: string;
  domain: string;
  body: string;
  in_domain: boolean;
}

export interface DroppedConcept {
  concept_id: string;
  reason: string;
}

export interface AuthorRunResult {
  stages: AuthorStage[];
  concepts: AuthorConcept[];
  dropped: DroppedConcept[];
}

export type Disposition = "approved" | "edited" | "rejected";

// /author/run is stateless — publish must carry the concept content back
// (title·core_question·body·domain) so the backend can rebuild the OkfDocumentDraft.
export interface ConceptDisposition {
  concept_id: string;
  disposition: Disposition;
  title?: string;
  core_question?: string;
  body?: string;
  domain?: string;
}

export interface AuthorPublishResult {
  committed: { sha?: string; files: string[] };
  // null when every concept was rejected (nothing to publish)
  published: { agent_id: string; concept_count: number; generated_at: string } | null;
  dropped?: string[];
}

export class AuthorError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "AuthorError";
    this.status = status;
  }
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch {
    throw new AuthorError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (res.status === 401) throw new AuthorError("로그인이 필요합니다.", 401);
  if (res.status === 403) {
    throw new AuthorError("자기 담당의 지식만 저작할 수 있습니다.", 403);
  }
  if (res.status === 404) {
    throw new AuthorError("저작 백엔드가 연결되어 있지 않습니다.", 404);
  }
  if (!res.ok) throw new AuthorError(`요청 실패 (HTTP ${res.status}).`, res.status);
  return (await res.json()) as T;
}

/** POST /api/author/run — raw 문서를 staged 개념 초안으로(owner측·transient). */
export function runAuthor(agentId: string, document: string): Promise<AuthorRunResult> {
  return postJson<AuthorRunResult>("/api/author/run", {
    agent_id: agentId,
    document,
  });
}

/** POST /api/author/publish — 승인 개념을 owner git 커밋 + 목차만 중앙 배포. */
export function publishAuthor(
  agentId: string,
  concepts: ConceptDisposition[],
): Promise<AuthorPublishResult> {
  return postJson<AuthorPublishResult>("/api/author/publish", {
    agent_id: agentId,
    concepts,
  });
}

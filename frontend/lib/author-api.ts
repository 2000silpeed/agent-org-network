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
  type?: string | null;
  in_domain: boolean;
  // 실제 커밋되는 OKF 마크다운(프론트매터 + 본문) — owner가 OKF 형식을 확인.
  okf_markdown?: string;
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

// 이미 중앙에 게시된 목차(KnowledgeIndex) — owner가 "이미 만든 개념"을 보는 라이브러리.
// 본문은 없다(중앙 비소유) — id·label·core_question·domain·type만.
export interface AuthorIndexConcept {
  id: string;
  label: string;
  core_question: string;
  domain: string;
  type: string | null;
}

export interface AuthorIndexResult {
  agent_id: string;
  generated_at: string | null;
  concepts: AuthorIndexConcept[];
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

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { accept: "application/json" } });
  } catch {
    throw new AuthorError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (res.status === 401) throw new AuthorError("로그인이 필요합니다.", 401);
  if (res.status === 403) {
    throw new AuthorError("자기 담당의 지식만 볼 수 있습니다.", 403);
  }
  if (res.status === 404) {
    throw new AuthorError("저작 백엔드가 연결되어 있지 않습니다.", 404);
  }
  if (!res.ok) throw new AuthorError(`요청 실패 (HTTP ${res.status}).`, res.status);
  return (await res.json()) as T;
}

/** GET /api/author/index/{agentId} — 이미 중앙에 게시된 목차(있으면). */
export function fetchAuthorIndex(agentId: string): Promise<AuthorIndexResult> {
  return getJson<AuthorIndexResult>(`/api/author/index/${encodeURIComponent(agentId)}`);
}

// 게시 개념의 본문 포함 상세 — owner 자기 OKF 조회(본문은 owner 번들에서·중앙 비소유 무관).
export interface AuthorConceptDetail {
  concept_id: string;
  title: string;
  core_question: string;
  domain: string;
  body: string;
  type: string | null;
}

export interface ConceptEditPatch {
  title?: string;
  core_question?: string;
  body?: string;
  domain?: string;
}

async function sendJson<T>(method: "PUT" | "DELETE", path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: body !== undefined ? { "content-type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new AuthorError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (res.status === 401) throw new AuthorError("로그인이 필요합니다.", 401);
  if (res.status === 403) throw new AuthorError("자기 담당의 지식만 변경할 수 있습니다.", 403);
  if (res.status === 404) throw new AuthorError("개념을 찾을 수 없습니다.", 404);
  if (!res.ok) throw new AuthorError(`요청 실패 (HTTP ${res.status}).`, res.status);
  return (await res.json()) as T;
}

/** GET — 게시 개념의 본문 포함 상세(편집용). */
export function fetchConcept(agentId: string, conceptId: string): Promise<AuthorConceptDetail> {
  return getJson<AuthorConceptDetail>(
    `/api/author/concept/${encodeURIComponent(agentId)}/${encodeURIComponent(conceptId)}`,
  );
}

/** PUT — 개념 편집(미지정 필드 보존). 인덱스 재도출까지 백엔드가 처리. */
export function updateConcept(
  agentId: string,
  conceptId: string,
  patch: ConceptEditPatch,
): Promise<{ concept: AuthorConceptDetail; published: AuthorPublishResult["published"] }> {
  return sendJson(
    "PUT",
    `/api/author/concept/${encodeURIComponent(agentId)}/${encodeURIComponent(conceptId)}`,
    patch,
  );
}

/** DELETE — 개념 삭제(목차에서 제거·인덱스 재도출). */
export function deleteConcept(
  agentId: string,
  conceptId: string,
): Promise<{ deleted: { concept_id: string }; published: AuthorPublishResult["published"] }> {
  return sendJson(
    "DELETE",
    `/api/author/concept/${encodeURIComponent(agentId)}/${encodeURIComponent(conceptId)}`,
  );
}

// 의미 기반 near-dup 후보(ADR 0032 결정 C) — 신규 staged 개념 vs 게시 라이브러리.
// 탐지 전용(읽기 전용·중앙 0). 병합 실행은 기존 updateConcept/deleteConcept를 재사용한다.
export interface DedupCandidate {
  new_concept_id: string;
  existing_concept_id: string;
  similarity: number;
  grade: "auto_suggest" | "similar";
}

/** POST /api/author/dedup/{agentId} — 신규 staged 개념과 게시 라이브러리의 near-dup 후보. */
export function checkDedup(
  agentId: string,
  concepts: AuthorConcept[],
): Promise<{ candidates: DedupCandidate[] }> {
  return postJson(`/api/author/dedup/${encodeURIComponent(agentId)}`, {
    concepts: concepts.map((c) => ({
      concept_id: c.concept_id,
      title: c.title,
      core_question: c.core_question,
      body: c.body,
      domain: c.domain,
      type: c.type ?? null,
    })),
  });
}

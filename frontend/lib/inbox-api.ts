// /inbox real client. Mirrors web.py serializers exactly:
//   serialize_case        (web.py:231) → /inbox/cases
//   serialize_review_item (web.py:289) → /inbox/backup-reviews
//   serialize_outcome     (web.py:269) → POST /cases/{id}/concur response
//   FetchDocument result  (web.py:889) → POST /inbox/cases/{id}/document
//
// These are operational surfaces (운영 면) — internal values (domains, coverage)
// MAY be exposed here (the opposite of the /ask OrgReply invariant). All requests
// flow through the Next proxy so the session cookie scopes to the logged-in owner
// (자기 처리함만 · 미로그인 401 · 스코프 위반 403).

export interface RelevantConcept {
  id: string;
  label: string;
  core_question: string;
}

export interface CaseCandidate {
  agent_id: string;
  owner: string;
  summary?: string;
  domains?: string[];
  knowledge_sources?: string[];
  relevant_concepts?: RelevantConcept[];
}

export interface ConflictCase {
  case_id: string;
  intent: string;
  question: string;
  candidates: CaseCandidate[];
}

export type ConsensusOutcome =
  | { type: "agreed"; primary: string; intent: string }
  | { type: "still_open"; pending_owners: string[] }
  | { type: "deadlocked" };

export type BackupReviewStatus = "pending" | "approved" | "corrected" | "dismissed";

export interface BackupReview {
  type: "approve" | "correct" | "dismiss";
  by_owner: string;
  rationale: string;
  corrected_text?: string;
  sources?: string[];
}

export interface BackupReviewItem {
  item_id: string;
  owner_id: string;
  agent_id: string;
  question: string;
  backup_answer_text: string;
  ticket_id: string;
  snapshot_at: string;
  answered_at: string;
  status: BackupReviewStatus;
  review: BackupReview | null;
}

export type FetchDocumentResult =
  | { found: true; available: true; content: string }
  | { found: false; available: boolean; message: string };

export class InboxError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "InboxError";
    this.status = status;
  }
}

async function getJson<T>(path: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { headers: { accept: "application/json" } });
  } catch {
    throw new InboxError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (res.status === 401) {
    throw new InboxError("로그인이 필요합니다.", 401);
  }
  if (!res.ok) {
    throw new InboxError(`요청 실패 (HTTP ${res.status}).`, res.status);
  }
  return (await res.json()) as T;
}

/** GET /api/inbox/cases — contested cases for the session owner. */
export function getInboxCases(): Promise<ConflictCase[]> {
  return getJson<ConflictCase[]>("/api/inbox/cases");
}

/** GET /api/inbox/backup-reviews — pending backup answers for the session owner. */
export function getBackupReviews(): Promise<BackupReviewItem[]> {
  return getJson<BackupReviewItem[]>("/api/inbox/backup-reviews");
}

/** POST /api/cases/{id}/concur — vote to assign an agent as primary. */
export async function postConcur(
  caseId: string,
  onAgent: string,
  rationale = "",
): Promise<ConsensusOutcome> {
  let res: Response;
  try {
    res = await fetch(`/api/cases/${encodeURIComponent(caseId)}/concur`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ on_agent: onAgent, rationale }),
    });
  } catch {
    throw new InboxError("네트워크 오류 — 합의 전송에 실패했습니다.");
  }
  if (res.status === 401) throw new InboxError("로그인이 필요합니다.", 401);
  if (res.status === 403) {
    throw new InboxError("이 케이스의 후보 owner가 아닙니다.", 403);
  }
  if (res.status === 404) throw new InboxError("케이스를 찾을 수 없습니다.", 404);
  if (!res.ok) throw new InboxError(`합의 실패 (HTTP ${res.status}).`, res.status);
  return (await res.json()) as ConsensusOutcome;
}

/** POST /api/inbox/cases/{id}/document — on-demand fetch a candidate's concept doc. */
export async function fetchCaseDocument(
  caseId: string,
  agentId: string,
  conceptId: string,
): Promise<FetchDocumentResult> {
  let res: Response;
  try {
    res = await fetch(`/api/inbox/cases/${encodeURIComponent(caseId)}/document`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ agent_id: agentId, concept_id: conceptId }),
    });
  } catch {
    throw new InboxError("네트워크 오류 — 문서를 가져오지 못했습니다.");
  }
  if (res.status === 401) throw new InboxError("로그인이 필요합니다.", 401);
  if (res.status === 403) {
    throw new InboxError("자기 케이스 후보 문서만 열 수 있습니다.", 403);
  }
  if (!res.ok) throw new InboxError(`문서 fetch 실패 (HTTP ${res.status}).`, res.status);
  return (await res.json()) as FetchDocumentResult;
}

/* ---- 재평가 (reeval) — 처리함 세 번째 탭 · serialize_reeval_item (web.py) ---- */

export type ReevalSubjectKind = "precedent" | "answer";
export type ReevalStatus = "pending_review" | "reviewed";
// owner 처분 — KeepPrecedent·InvalidatePrecedent·SupersedePrecedent·AcknowledgeAnswer·ReAnswer
export type ReevalOutcomeKind =
  | "keep"
  | "invalidate"
  | "supersede"
  | "acknowledge"
  | "reanswer";

export interface ReevalReview {
  kind: ReevalOutcomeKind;
  by_owner: string;
  rationale: string;
  new_primary?: string;
}

export interface ReevalItem {
  item_id: string;
  owner_id: string;
  agent_id: string;
  subject_kind: ReevalSubjectKind;
  subject_ref: string;
  trigger_sha: string;
  flagged_at: string;
  status: ReevalStatus;
  question: string;
  reason: string;
  review: ReevalReview | null;
}

/** GET /api/inbox/reeval — stale 판례·답 재평가 항목(세션 owner). */
export function getReeval(): Promise<ReevalItem[]> {
  return getJson<ReevalItem[]>("/api/inbox/reeval");
}

/** POST /api/reeval/{id}/review — owner 재평가 처분(유지/재답변/무효화 등). */
export async function postReevalReview(
  itemId: string,
  kind: ReevalOutcomeKind,
  opts: { rationale?: string; newPrimary?: string } = {},
): Promise<void> {
  let res: Response;
  try {
    res = await fetch(`/api/reeval/${encodeURIComponent(itemId)}/review`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        kind,
        rationale: opts.rationale ?? "",
        new_primary: opts.newPrimary,
      }),
    });
  } catch {
    throw new InboxError("네트워크 오류 — 재평가 처분 전송에 실패했습니다.");
  }
  if (res.status === 401) throw new InboxError("로그인이 필요합니다.", 401);
  if (res.status === 403) {
    throw new InboxError("자기 재평가 항목만 처분할 수 있습니다.", 403);
  }
  if (res.status === 404) throw new InboxError("재평가 항목을 찾을 수 없습니다.", 404);
  if (!res.ok) throw new InboxError(`재평가 처분 실패 (HTTP ${res.status}).`, res.status);
}

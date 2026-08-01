/**
 * Legacy Card Owner authoring client contract retained for the RB3.4
 * `owner-frontend/` migration. Central Next must not import it: there is no
 * Central published-index API in the current installation composition.
 */
export interface PublishedIndexConcept {
  id: string;
  label: string;
  core_question: string;
  domain: string;
  type: string | null;
}

export interface PublishedIndex {
  agent_id: string;
  generated_at: string | null;
  concepts: PublishedIndexConcept[];
}

export class AuthorError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "AuthorError";
    this.status = status;
  }
}

/** GET only: approved, published index metadata; it contains no raw or draft body. */
export async function fetchPublishedIndex(agentId: string): Promise<PublishedIndex> {
  let response: Response;
  try {
    response = await fetch(`/api/author/index/${encodeURIComponent(agentId)}`, {
      headers: { accept: "application/json" },
      cache: "no-store",
    });
  } catch {
    throw new AuthorError("백엔드에 연결할 수 없습니다.");
  }
  if (!response.ok) {
    throw new AuthorError("공개된 Knowledge Index를 불러올 수 없습니다.", response.status);
  }
  return (await response.json()) as PublishedIndex;
}

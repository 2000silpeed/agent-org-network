export type OnboardingStepKind = "user" | "card" | "knowledge";
export type OnboardingStepState = "complete" | "current" | "locked";

export interface RegistryUserRow {
  user_id: string;
  email: string | null;
  manager: string | null;
  sso_link_status: "verified_email_match" | "not_current_principal";
}

export interface OnboardingStatus {
  revision: number;
  steps: Array<{
    kind: OnboardingStepKind;
    label: string;
    state: OnboardingStepState;
  }>;
  users: RegistryUserRow[];
  cards: AgentCardRow[];
  cardCapability: "available" | "unavailable";
}

export interface AgentCardRow {
  agent_id: string;
  owner: string;
  team: string;
  summary: string;
  domains: string[];
  last_reviewed_at: string;
  maintainer: string | null;
  can_answer: string[];
  cannot_answer: string[];
  approval_when: string[];
  collaborate_when: string[];
  knowledge_sources: string[];
  trust_labels: string[];
}

export type AgentCardCommand = Omit<AgentCardRow, "last_reviewed_at"> & {
  expected_revision: number;
};

export interface AgentCardResult {
  card: AgentCardRow;
  revision: number;
  replayed: boolean;
}

export interface OwnerAuthoringResult {
  run_id: string;
  stage: "AwaitingOwnerReview";
  revision: 1;
  document_count: number;
  edge_count: number;
  dropped_count: number;
}

export interface RegistryUserCommand {
  expected_revision: number;
  user_id: string;
  email: string;
  manager: string | null;
}

export interface RegistryUserResult {
  user_id: string;
  email: string;
  manager: string | null;
  revision: number;
  replayed: boolean;
}

export class OnboardingError extends Error {
  status?: number;

  constructor(message: string, status?: number) {
    super(message);
    this.name = "OnboardingError";
    this.status = status;
  }
}

async function errorOf(response: Response): Promise<OnboardingError> {
  const messages: Record<number, string> = {
    401: "로그인이 필요합니다.",
    403: "사용자 등록 권한이 없습니다.",
    409: "다른 변경과 충돌했습니다. 상태를 새로고침해 주세요.",
    422: "입력값을 확인해 주세요.",
    503: "중앙 온보딩 기능을 사용할 수 없습니다.",
  };
  return new OnboardingError(
    messages[response.status] ?? "온보딩 요청을 처리하지 못했습니다.",
    response.status,
  );
}

function parseRegistryUserResult(value: unknown): RegistryUserResult | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const keys = Object.keys(row).sort();
  if (
    JSON.stringify(keys) !==
    JSON.stringify(["email", "manager", "replayed", "revision", "user_id"])
  ) return null;
  if (
    typeof row.user_id !== "string" ||
    !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(row.user_id) ||
    typeof row.email !== "string" ||
    row.email.trim() !== row.email ||
    !row.email.includes("@") ||
    (row.manager !== null &&
      (typeof row.manager !== "string" ||
        !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(row.manager))) ||
    !Number.isSafeInteger(row.revision) ||
    (row.revision as number) < 1 ||
    typeof row.replayed !== "boolean"
  ) return null;
  return {
    user_id: row.user_id,
    email: row.email,
    manager: row.manager as string | null,
    revision: row.revision as number,
    replayed: row.replayed,
  };
}

export async function getOnboardingStatus(): Promise<OnboardingStatus> {
  let response: Response;
  let statusResponse: Response;
  try {
    [response, statusResponse] = await Promise.all([
      fetch("/api/admin/users", {
        headers: { accept: "application/json" },
        cache: "no-store",
      }),
      fetch("/api/onboarding/status", {
        headers: { accept: "application/json" },
        cache: "no-store",
      }),
    ]);
  } catch {
    throw new OnboardingError("백엔드에 연결할 수 없습니다.");
  }
  if (!response.ok) throw await errorOf(response);
  if (!statusResponse.ok) throw await errorOf(statusResponse);
  const statusValue: unknown = await statusResponse.json();
  if (typeof statusValue !== "object" || statusValue === null || Array.isArray(statusValue)) {
    throw new OnboardingError("서버 응답을 확인할 수 없습니다.", 503);
  }
  const serverStatus = statusValue as Record<string, unknown>;
  if (
    JSON.stringify(Object.keys(serverStatus).sort()) !==
      JSON.stringify(["card_capability", "cards", "revision", "steps"]) ||
    !Number.isSafeInteger(serverStatus.revision) ||
    (serverStatus.revision as number) < 1 ||
    !["available", "unavailable"].includes(serverStatus.card_capability as string) ||
    !Array.isArray(serverStatus.cards) ||
    !serverStatus.cards.every((card) => parseAgentCard(card) !== null) ||
    !Array.isArray(serverStatus.steps) ||
    serverStatus.steps.length !== 3 ||
    !serverStatus.steps.every((step, index) => {
      if (typeof step !== "object" || step === null || Array.isArray(step)) return false;
      const item = step as Record<string, unknown>;
      return (
        JSON.stringify(Object.keys(item).sort()) === JSON.stringify(["kind", "state"]) &&
        item.kind === ["user", "card", "knowledge"][index] &&
        ["complete", "current", "locked"].includes(item.state as string)
      );
    })
  ) throw new OnboardingError("서버 응답을 확인할 수 없습니다.", 503);

  const raw = (await response.json()) as Array<{
    user_id: string;
    email: string | null;
    manager: string | null;
    sso_link_status?: "verified_email_match";
  }>;
  const users = raw.map((user) => ({
    user_id: user.user_id,
    email: user.email,
    manager: user.manager,
    sso_link_status:
      user.sso_link_status === "verified_email_match"
        ? ("verified_email_match" as const)
        : ("not_current_principal" as const),
  }));
  const currentRegistered = users.some(
    (user) => user.sso_link_status === "verified_email_match",
  );

  const cards = serverStatus.cards.map((card) => parseAgentCard(card) as AgentCardRow);
  const cardComplete = cards.length > 0;
  const cardAvailable = serverStatus.card_capability === "available";
  return {
    revision: serverStatus.revision as number,
    steps: [
      {
        kind: "user",
        label: "Registry User",
        state: currentRegistered ? "complete" : "current",
      },
      {
        kind: "card",
        label: "Agent Card",
        state: currentRegistered && cardAvailable
          ? (cardComplete ? "complete" : "current")
          : "locked",
      },
      {
        kind: "knowledge",
        label: "Knowledge OKF",
        state: cardAvailable && cardComplete ? "current" : "locked",
      },
    ],
    users,
    cards,
    cardCapability: serverStatus.card_capability as "available" | "unavailable",
  };
}

export async function registerRegistryUser(
  command: RegistryUserCommand,
  idempotencyKey: string,
): Promise<RegistryUserResult> {
  let response: Response;
  try {
    response = await fetch("/api/admin/users", {
      method: "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        "idempotency-key": idempotencyKey,
      },
      body: JSON.stringify(command),
    });
  } catch {
    throw new OnboardingError("백엔드에 연결할 수 없습니다.");
  }
  if (!response.ok) throw await errorOf(response);
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    throw new OnboardingError("서버 응답을 확인할 수 없습니다.", 503);
  }
  const result = parseRegistryUserResult(raw);
  if (result === null) {
    throw new OnboardingError("서버 응답을 확인할 수 없습니다.", 503);
  }
  return result;
}

function parseAgentCard(value: unknown): AgentCardRow | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const card = value as Record<string, unknown>;
  const keys = [
    "agent_id", "approval_when", "can_answer", "cannot_answer", "collaborate_when",
    "domains", "knowledge_sources", "last_reviewed_at", "maintainer", "owner",
    "summary", "team", "trust_labels",
  ].sort();
  if (JSON.stringify(Object.keys(card).sort()) !== JSON.stringify(keys)) return null;
  const listFields = ["domains", "can_answer", "cannot_answer", "approval_when", "collaborate_when", "knowledge_sources", "trust_labels"];
  if (
    !["agent_id", "owner", "team", "summary", "last_reviewed_at"].every((key) => typeof card[key] === "string") ||
    !/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(card.agent_id as string) ||
    !(card.owner as string).trim() ||
    !(card.team as string).trim() ||
    !(card.summary as string).trim() ||
    !/^\d{4}-\d{2}-\d{2}$/.test(card.last_reviewed_at as string) ||
    (card.maintainer !== null && typeof card.maintainer !== "string") ||
    !listFields.every((key) => Array.isArray(card[key]) && (card[key] as unknown[]).every((item) => typeof item === "string"))
  ) return null;
  return card as unknown as AgentCardRow;
}

function parseAgentCardResult(value: unknown): AgentCardResult | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  if (JSON.stringify(Object.keys(row).sort()) !== JSON.stringify(["card", "replayed", "revision"])) return null;
  if (!Number.isSafeInteger(row.revision) || (row.revision as number) < 1 || typeof row.replayed !== "boolean") return null;
  const card = parseAgentCard(row.card);
  if (card === null) return null;
  return { card, revision: row.revision as number, replayed: row.replayed };
}

export async function registerAgentCard(
  command: AgentCardCommand,
  idempotencyKey: string,
): Promise<AgentCardResult> {
  let response: Response;
  try {
    response = await fetch("/api/admin/agent-cards", {
      method: "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        "idempotency-key": idempotencyKey,
      },
      body: JSON.stringify(command),
    });
  } catch {
    throw new OnboardingError("백엔드에 연결할 수 없습니다.");
  }
  if (!response.ok) throw await errorOf(response);
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    throw new OnboardingError("서버 응답을 확인할 수 없습니다.", 503);
  }
  const result = parseAgentCardResult(raw);
  if (result === null) throw new OnboardingError("서버 응답을 확인할 수 없습니다.", 503);
  return result;
}

export async function createOwnerAuthoringDraft(
  agentId: string,
  documents: File[],
  idempotencyKey: string,
): Promise<OwnerAuthoringResult> {
  const csrfProof = typeof document === "undefined"
    ? ""
    : document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith("aon_owner_csrf="))
      ?.slice("aon_owner_csrf=".length) ?? "";
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(csrfProof)) {
    throw new OnboardingError("Card Owner pairing을 다시 연결해 주세요.", 401);
  }
  const encoded = await Promise.all(documents.map(async (document) => {
    const bytes = new Uint8Array(await document.arrayBuffer());
    let binary = "";
    for (let index = 0; index < bytes.length; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    return {
      source_id: document.name,
      media_type: document.type === "text/markdown" ? "text/markdown" : "text/plain",
      content_base64: btoa(binary),
    };
  }));
  let response: Response;
  try {
    response = await fetch("/owner-api/authoring/runs", {
      method: "POST",
      headers: {
        accept: "application/json",
        "content-type": "application/json",
        "idempotency-key": idempotencyKey,
        "x-owner-csrf": csrfProof,
      },
      body: JSON.stringify({ agent_id: agentId, documents: encoded }),
    });
  } catch {
    throw new OnboardingError("Card Owner 설치에 연결할 수 없습니다.");
  }
  if (!response.ok) throw await errorOf(response);
  let raw: unknown;
  try {
    raw = await response.json();
  } catch {
    throw new OnboardingError("Card Owner 응답을 확인할 수 없습니다.", 503);
  }
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new OnboardingError("Card Owner 응답을 확인할 수 없습니다.", 503);
  }
  const row = raw as Record<string, unknown>;
  if (
    JSON.stringify(Object.keys(row).sort()) !==
      JSON.stringify(["document_count", "dropped_count", "edge_count", "revision", "run_id", "stage"]) ||
    typeof row.run_id !== "string" ||
    row.stage !== "AwaitingOwnerReview" ||
    row.revision !== 1 ||
    !["document_count", "edge_count", "dropped_count"].every(
      (key) => Number.isSafeInteger(row[key]) && (row[key] as number) >= 0,
    )
  ) throw new OnboardingError("Card Owner 응답을 확인할 수 없습니다.", 503);
  return row as unknown as OwnerAuthoringResult;
}

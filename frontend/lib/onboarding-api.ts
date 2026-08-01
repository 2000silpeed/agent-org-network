export type OnboardingStepKind = "user" | "card" | "card_owner_installation";
export type OnboardingStepState = "complete" | "current" | "locked";

export interface RegistryUserRow {
  user_id: string;
  email: string | null;
  manager: string | null;
  sso_link_status: "verified_email_match" | "unlinked";
}

export interface AgentCardSummary { agent_id: string; owner: string; team: string; summary: string; }
export interface OnboardingStatus {
  revision: number;
  card_capability: "available" | "unavailable";
  steps: Array<{ kind: OnboardingStepKind; label: string; state: OnboardingStepState }>;
  cards: AgentCardSummary[];
  card_owner_installation: { artifact: "agent-org-owner"; href: "/onboarding#card-owner-installation" };
}

export interface AgentCardRow extends AgentCardSummary {
  domains: string[]; last_reviewed_at: string; maintainer: string | null; can_answer: string[];
  cannot_answer: string[]; approval_when: string[]; collaborate_when: string[];
  knowledge_sources: string[]; trust_labels: string[];
}
export type AgentCardCommand = Omit<AgentCardRow, "last_reviewed_at"> & { expected_revision: number };
export interface AgentCardResult { card: AgentCardRow; revision: number; replayed: boolean; }
export interface RegistryUserCommand { expected_revision: number; user_id: string; email: string; manager: string | null; }
export interface RegistryUserResult { user_id: string; email: string; manager: string | null; revision: number; replayed: boolean; }

export class OnboardingError extends Error {
  constructor(message: string, public readonly status?: number) { super(message); }
}

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CARD_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const CARD_KEYS = ["agent_id", "approval_when", "can_answer", "cannot_answer", "collaborate_when", "domains", "knowledge_sources", "last_reviewed_at", "maintainer", "owner", "summary", "team", "trust_labels"].sort();

export function parseListInput(value: string): string[] {
  const entries = value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
  return Array.from(new Set(entries));
}

export function readAdmissionCsrfCookie(cookie: string): string | null {
  if (cookie.length > 8192) return null;
  const values: string[] = [];
  for (const part of cookie.split(";")) {
    const index = part.indexOf("=");
    if (index < 1 || part.slice(0, index).trim() !== "__Host-aon-central-csrf") continue;
    try { values.push(decodeURIComponent(part.slice(index + 1).trim())); } catch { return null; }
  }
  if (values.length !== 1 || !/^[A-Za-z0-9_-]{32,128}$/.test(values[0])) return null;
  return values[0];
}

export async function getOnboardingStatus(): Promise<OnboardingStatus> { return getJson("/api/onboarding/status", parseOnboardingStatus); }
export async function listRegistryUsers(): Promise<RegistryUserRow[]> { return getJson("/api/admin/users", parseRegistryUsers); }
export async function listAgentCards(): Promise<AgentCardRow[]> { return getJson("/api/admin/agent-cards", parseAgentCards); }

export async function registerRegistryUser(command: RegistryUserCommand, idempotencyKey: string): Promise<RegistryUserResult> {
  return postJson("/api/admin/users", command, idempotencyKey, parseRegistryUserResult);
}
export async function registerAgentCard(command: AgentCardCommand, idempotencyKey: string): Promise<AgentCardResult> {
  return postJson("/api/admin/agent-cards", command, idempotencyKey, parseAgentCardResult);
}

async function getJson<T>(url: string, parser: (value: unknown) => T | null): Promise<T> {
  let response: Response;
  try { response = await fetch(url, { headers: { accept: "application/json" }, credentials: "same-origin", cache: "no-store" }); } catch { throw new OnboardingError("Central 서버에 연결할 수 없습니다.", 502); }
  if (!response.ok) throw await errorOf(response);
  return parseResponse(response, parser);
}

async function postJson<T>(url: string, command: object, idempotencyKey: string, parser: (value: unknown) => T | null): Promise<T> {
  const csrf = typeof document === "undefined" ? null : readAdmissionCsrfCookie(document.cookie);
  if (csrf === null || !/^[A-Za-z0-9_-]{1,128}$/.test(idempotencyKey)) throw new OnboardingError("SSO 세션을 다시 시작한 뒤 등록해 주세요.", 401);
  let response: Response;
  try {
    response = await fetch(url, { method: "POST", credentials: "same-origin", cache: "no-store", headers: { accept: "application/json", "content-type": "application/json", "x-aon-csrf": csrf, "idempotency-key": idempotencyKey }, body: JSON.stringify(command) });
  } catch { throw new OnboardingError("Central 서버에 연결할 수 없습니다.", 502); }
  if (!response.ok) throw await errorOf(response);
  return parseResponse(response, parser);
}

async function parseResponse<T>(response: Response, parser: (value: unknown) => T | null): Promise<T> {
  let value: unknown; try { value = await response.json(); } catch { throw malformed(); }
  const parsed = parser(value); if (parsed === null) throw malformed(); return parsed;
}
async function errorOf(response: Response): Promise<OnboardingError> {
  const messages: Record<number, string> = { 401: "SSO 로그인이 필요합니다. 상단에서 로그인해 주세요.", 403: "현재 계정에는 이 등록 권한이 없습니다.", 409: "다른 변경과 충돌했습니다. 목록을 새로고침해 주세요.", 422: "입력값을 확인해 주세요.", 502: "Central 서버에 연결할 수 없습니다.", 503: "Central 등록 기능을 지금 사용할 수 없습니다." };
  return new OnboardingError(messages[response.status] ?? "등록 요청을 처리하지 못했습니다.", response.status);
}
function malformed(): OnboardingError { return new OnboardingError("서버 응답을 확인할 수 없습니다.", 503); }
function object(value: unknown): Record<string, unknown> | null { return typeof value === "object" && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : null; }
function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean { return JSON.stringify(Object.keys(value).sort()) === JSON.stringify([...keys].sort()); }
function stringList(value: unknown): value is string[] { return Array.isArray(value) && value.every((entry) => typeof entry === "string"); }
function parseUser(value: unknown): RegistryUserRow | null {
  const row = object(value); if (row === null || !exactKeys(row, ["user_id", "email", "manager", "sso_link_status"]) || typeof row.user_id !== "string" || !ID.test(row.user_id) || (row.email !== null && (typeof row.email !== "string" || row.email.trim() !== row.email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(row.email))) || (row.manager !== null && (typeof row.manager !== "string" || !ID.test(row.manager))) || (row.sso_link_status !== "verified_email_match" && row.sso_link_status !== "unlinked")) return null;
  return row as unknown as RegistryUserRow;
}
function parseRegistryUsers(value: unknown): RegistryUserRow[] | null { return Array.isArray(value) && value.every((row) => parseUser(row) !== null) ? value as RegistryUserRow[] : null; }
function parseSummary(value: unknown): AgentCardSummary | null {
  const row = object(value); if (row === null || !exactKeys(row, ["agent_id", "owner", "team", "summary"]) || typeof row.agent_id !== "string" || !CARD_ID.test(row.agent_id) || typeof row.owner !== "string" || !ID.test(row.owner) || ![row.team, row.summary].every((entry) => typeof entry === "string" && entry.trim() === entry && entry.length > 0)) return null;
  return row as unknown as AgentCardSummary;
}
function parseOnboardingStatus(value: unknown): OnboardingStatus | null {
  const row = object(value); if (row === null || !exactKeys(row, ["revision", "card_capability", "steps", "cards", "card_owner_installation"]) || !Number.isSafeInteger(row.revision) || (row.revision as number) < 1 || (row.card_capability !== "available" && row.card_capability !== "unavailable") || !Array.isArray(row.cards) || !row.cards.every((card) => parseSummary(card) !== null) || !Array.isArray(row.steps) || row.steps.length !== 3) return null;
  const kinds: OnboardingStepKind[] = ["user", "card", "card_owner_installation"];
  if (!row.steps.every((step, index) => { const item = object(step); return item !== null && exactKeys(item, ["kind", "label", "state"]) && item.kind === kinds[index] && item.label === ["Registry User", "Agent Card", "Card Owner Installation"][index] && ["complete", "current", "locked"].includes(item.state as string); })) return null;
  const [userStep, cardStep, installationStep] = row.steps.map((step) => object(step) as Record<string, unknown>);
  if (userStep.state !== "complete" || installationStep.state === "complete" || (row.card_capability === "unavailable" && (row.cards.length !== 0 || cardStep.state !== "locked" || installationStep.state !== "locked")) || (row.card_capability === "available" && ((row.cards.length > 0 && (cardStep.state !== "complete" || installationStep.state !== "current")) || (row.cards.length === 0 && (cardStep.state !== "current" || installationStep.state !== "locked"))))) return null;
  const installation = object(row.card_owner_installation); if (installation === null || !exactKeys(installation, ["artifact", "href"]) || installation.artifact !== "agent-org-owner" || installation.href !== "/onboarding#card-owner-installation") return null;
  return row as unknown as OnboardingStatus;
}
function parseAgentCard(value: unknown): AgentCardRow | null {
  const row = object(value); if (row === null || !exactKeys(row, CARD_KEYS) || parseSummary({ agent_id: row.agent_id, owner: row.owner, team: row.team, summary: row.summary }) === null || typeof row.last_reviewed_at !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(row.last_reviewed_at) || (row.maintainer !== null && (typeof row.maintainer !== "string" || !ID.test(row.maintainer))) || !["domains", "can_answer", "cannot_answer", "approval_when", "collaborate_when", "knowledge_sources", "trust_labels"].every((key) => stringList(row[key]))) return null;
  return row as unknown as AgentCardRow;
}
function parseAgentCards(value: unknown): AgentCardRow[] | null { return Array.isArray(value) && value.every((card) => parseAgentCard(card) !== null) ? value as AgentCardRow[] : null; }
function parseRegistryUserResult(value: unknown): RegistryUserResult | null { const row = object(value); return row !== null && exactKeys(row, ["user_id", "email", "manager", "revision", "replayed"]) && typeof row.user_id === "string" && ID.test(row.user_id) && typeof row.email === "string" && (row.manager === null || (typeof row.manager === "string" && ID.test(row.manager))) && Number.isSafeInteger(row.revision) && (row.revision as number) >= 1 && typeof row.replayed === "boolean" ? row as unknown as RegistryUserResult : null; }
function parseAgentCardResult(value: unknown): AgentCardResult | null { const row = object(value); return row !== null && exactKeys(row, ["card", "revision", "replayed"]) && parseAgentCard(row.card) !== null && Number.isSafeInteger(row.revision) && (row.revision as number) >= 1 && typeof row.replayed === "boolean" ? row as unknown as AgentCardResult : null; }

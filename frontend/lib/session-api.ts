// Operator/owner session client. Mirrors web.py POST /login (line 774) and
// the session-scoped operational routes. Requests go through the Next proxy
// (/api/* → FastAPI, app/api/[...path]/route.ts), so the session cookie
// (aon_session) rides along same-origin and httponly.
//
// The 6 demo users come from demo.py: root_manager is the operator/manager,
// the five *_lead users are owners (each owns one Agent Card). Passwordless
// identity *selection* (ADR 0016 결정 2) — v0 has no password, the chosen
// identity is pinned in the session cookie to block per-request impersonation.

export type DemoRole = "operator" | "owner";

export interface DemoIdentity {
  userId: string;
  label: string;
  role: DemoRole;
  /** Owner's Agent Card id, for context (operator has none). */
  agentId?: string;
  domainHint: string;
}

// Static directory of the demo registry (demo.py _USERS / _CARDS). Used only to
// render the identity picker; the backend re-validates user_id on /login (401
// if unknown), so this list is a convenience, not a trust boundary.
export const DEMO_IDENTITIES: DemoIdentity[] = [
  {
    userId: "root_manager",
    label: "root_manager",
    role: "operator",
    domainHint: "루트 매니저 · 운영 콘솔 · 에스컬레이션 큐",
  },
  {
    userId: "legal_lead",
    label: "legal_lead",
    role: "owner",
    agentId: "contract_ops",
    domainHint: "계약 검토",
  },
  {
    userId: "cs_lead",
    label: "cs_lead",
    role: "owner",
    agentId: "cs_ops",
    domainHint: "환불 · 보상",
  },
  {
    userId: "finance_lead",
    label: "finance_lead",
    role: "owner",
    agentId: "finance_ops",
    domainHint: "가격 · 보상",
  },
  {
    userId: "hr_lead",
    label: "hr_lead",
    role: "owner",
    agentId: "hr_ops",
    domainHint: "채용 · 휴가 · 평가",
  },
  {
    userId: "it_lead",
    label: "it_lead",
    role: "owner",
    agentId: "it_ops",
    domainHint: "계정 · 접근권한 · 보안",
  },
];

export function identityFor(userId: string | null): DemoIdentity | undefined {
  if (userId == null) return undefined;
  return DEMO_IDENTITIES.find((d) => d.userId === userId);
}

export class SessionError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "SessionError";
    this.status = status;
  }
}

interface LoginResponse {
  ok: boolean;
  user_id: string;
}

/** POST /api/login — pin a demo identity into the operator session cookie. */
export async function postLogin(userId: string): Promise<string> {
  let res: Response;
  try {
    res = await fetch("/api/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ user_id: userId }),
    });
  } catch {
    throw new SessionError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (res.status === 401) {
    throw new SessionError(`등록되지 않은 사용자입니다: ${userId}`, 401);
  }
  if (res.status === 403) {
    throw new SessionError("SSO 모드가 활성화되어 무비밀번호 로그인이 차단됐습니다.", 403);
  }
  if (!res.ok) {
    throw new SessionError(`로그인 실패 (HTTP ${res.status}).`, res.status);
  }
  const data = (await res.json()) as LoginResponse;
  return data.user_id;
}

/** POST /api/logout — clear the operator session. */
export async function postLogout(): Promise<void> {
  try {
    await fetch("/api/logout", { method: "POST" });
  } catch {
    // best-effort; the client clears local state regardless.
  }
}

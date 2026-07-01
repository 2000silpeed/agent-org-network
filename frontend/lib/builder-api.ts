// Central card builder client (T5.3). Mirrors web.py:
//   BuilderValidateRequest (web.py:626) → POST /builder/validate
//   validate_card_for_builder (web.py:653) — admission 검증(순수 함수)
//
// 이 화면은 카드를 폼으로 구성 → admission 검증 → YAML 미리보기까지다. **편집
// 채널은 git/PR** — 라이브 레지스트리 쓰기는 없다(CONTEXT Maintainer). 검증 통과
// YAML을 Owner가 복사→git 커밋(PR)한다. 요청은 Next 프록시(/api/* → FastAPI,
// app/api/[...path]/route.ts)를 거치므로 세션 쿠키(aon_session)가 same-origin·
// httponly로 함께 흐른다.
//
// Owner 스코프(web.py:1452, ADR 0016): 인증 ON이면 세션 신원 ≠ 카드 owner → 403,
// 미로그인 → 401. owner 필드는 UI에서 로그인 신원으로 고정한다(위조 차단).

// BuilderValidateRequest(web.py:638~650) 미러 — AgentCard 필드를 그대로 받는다.
export interface BuilderCardInput {
  agent_id: string;
  owner: string;
  team: string;
  summary: string;
  domains: string[];
  /** ISO date 문자열(YYYY-MM-DD) — pydantic이 date로 강제. */
  last_reviewed_at: string;
  maintainer: string | null;
  can_answer: string[];
  cannot_answer: string[];
  approval_when: string[];
  collaborate_when: string[];
  knowledge_sources: string[];
  trust_labels: string[];
}

// validate_card_for_builder 응답 — 통과면 yaml, 실패면 errors(둘 중 하나).
export type BuilderValidateResult =
  | { ok: true; yaml: string }
  | { ok: false; errors: string[] };

export class BuilderError extends Error {
  status?: number;
  constructor(message: string, status?: number) {
    super(message);
    this.name = "BuilderError";
    this.status = status;
  }
}

/**
 * POST /api/builder/validate — 카드 후보를 admission 규칙으로 검증한다.
 *
 * 통과면 `{ok:true, yaml}`(registry/agents/{agent_id}.yaml 텍스트), 실패면
 * `{ok:false, errors}`. 401(미로그인)·403(세션 ≠ owner)은 BuilderError로 승격해
 * LoginGate/폼이 안내하게 한다. **라이브 등록은 하지 않는다** — YAML은 git 커밋용.
 */
export async function validateCard(
  card: BuilderCardInput,
): Promise<BuilderValidateResult> {
  let res: Response;
  try {
    res = await fetch("/api/builder/validate", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(card),
    });
  } catch {
    throw new BuilderError("네트워크 오류 — 백엔드에 연결할 수 없습니다.");
  }
  if (res.status === 401) throw new BuilderError("로그인이 필요합니다.", 401);
  if (res.status === 403) {
    throw new BuilderError(
      "자기 카드만 구성할 수 있습니다. owner가 로그인 신원과 일치해야 합니다.",
      403,
    );
  }
  if (res.status === 404) {
    throw new BuilderError("빌더 백엔드가 연결되어 있지 않습니다.", 404);
  }
  if (!res.ok) throw new BuilderError(`요청 실패 (HTTP ${res.status}).`, res.status);
  return (await res.json()) as BuilderValidateResult;
}

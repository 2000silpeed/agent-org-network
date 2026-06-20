# 카드는 보수적 자기 선언만 하고, 권한(Authority)은 중앙이 소유한다

상태: accepted (2026-06-20)

Agent Card는 자기 자신에 대한 *under-claim*만 자기보고한다 — `domains`, `can_answer`, `cannot_answer`, `approval_when`, `collaborate_when` 같은 "내가 못한다 / 도움이 필요하다 / 사인을 받겠다". (사람 위계 "누가 누구의 상사인가"는 카드가 아니라 User 그래프의 `manages`에 산다 — ADR 0005.) 반면 *over-claim*에 해당하는 **상대적 권한** — Overlap 충돌에서 누가 이기는가(Authority/precedence), Manager 계층 해소 — 는 카드가 선언할 수 없고 중앙 라우팅 규칙이 소유한다.

근거: under-claim(겸양)은 틀려도 과하게 신중할 뿐이라 안전하지만, over-claim(권한 주장)은 틀리면 잘못된 답에 권위를 부여하는 사고가 된다. 기획서 14절 "자기보고된 권한은 믿지 않는다"의 구체화다.
